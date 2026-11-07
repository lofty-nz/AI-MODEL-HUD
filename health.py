"""Problem finder for the AI Hub: things on this PC that need a person, in plain words.

Checks (every CHECK_S seconds, on a background thread):
- a graphics card the PC has but the NVIDIA driver can't reach (fell off the bus, crashed);
- NVIDIA driver errors (Xid) in the kernel log since boot;
- floods of PCIe errors (often a loose card, riser or power cable);
- containers that keep restarting, report unhealthy, or crashed recently;
- known bad lines in container logs (out of memory, too many open files, CUDA errors);
- a nearly full drive, and cards or CPU running at their slowdown temperature.

Each problem: {id, level "error"|"warn", title, detail, fix, since, machine-readable extras}.
Problems that clear are kept in a short history (~/.local/state/aihub/problems.json) so the page
can say what happened while nobody was looking.
"""
import calendar
import json
import os
import re
import shutil
import subprocess
import threading
import time

CHECK_S = 30
LOG_HIT_KEEP_S = 6 * 3600       # a bad log line stays listed this long after it was last seen
CRASH_KEEP_S = 24 * 3600        # containers that exited with an error in the last day
AER_WARN_PER_HOUR = 100         # correctable PCIe errors are normal now and then; floods aren't
HISTORY_FILE = os.path.expanduser("~/.local/state/aihub/problems.json")
HISTORY_KEEP = 60

# What NVIDIA's Xid codes mean, for the ones a home PC actually sees.
XID = {
    13: ("warn", "a program crashed on the card (graphics engine exception)"),
    31: ("warn", "a program read memory it shouldn't have (MMU fault), usually a bug in that program"),
    32: ("warn", "a corrupted command reached the card"),
    43: (None, "a program on the card was stopped"),            # routine: Ctrl+C, a killed container
    45: (None, "a program on the card was stopped early"),      # routine
    48: ("error", "uncorrectable memory error on the card"),
    61: ("error", "the card's internal controller stopped answering"),
    62: ("error", "the card's internal controller stopped answering"),
    63: ("warn", "the card retired a bad memory page"),
    64: ("error", "the card couldn't retire a bad memory page"),
    69: ("error", "the graphics engine hit a fault"),
    79: ("error", "the card fell off the PCIe bus (it stopped responding completely)"),
    92: ("warn", "high rate of corrected memory errors on the card"),
    94: ("error", "contained memory error"),
    95: ("error", "uncontained memory error"),
    109: ("warn", "a program on the card timed out"),
    119: ("error", "the card's GSP firmware timed out"),
    120: ("error", "the card's GSP firmware hit an error"),
    154: (None, "the driver says the PC needs a restart to recover the card"),  # folded into the others
}
LOG_PATTERNS = [
    (re.compile(r"too many open files", re.I), "warn", "ran out of open files",
     "It tried to open more files at once than it's allowed. Work in progress (such as indexing documents) "
     "fails. Raise its open-file limit (ulimits: nofile) in docker-compose.yml and recreate it."),
    (re.compile(r"CUDA out of memory|OutOfMemoryError|cudaErrorMemoryAllocation|out of memory", re.I), "warn",
     "ran out of graphics memory",
     "Something else is holding the card's memory, or the job is too big for it. Unload models in AI Hub, "
     "or use a smaller model or picture size."),
    (re.compile(r"CUDA error:|no CUDA-capable device|CUDA driver version is insufficient|"
                r"cudaErrorDevicesUnavailable|Failed to initialize NVML|NVML: Unknown Error|GPU is lost", re.I), "warn",
     "lost its graphics card",
     "It can't use its card. If AI Hub also reports a card that isn't responding, restart the PC; "
     "otherwise restart the container."),
    (re.compile(r"No space left on device", re.I), "error", "ran out of disk space",
     "The drive is full. Free some space, then restart the container."),
]


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired):
        return "", ""


def short_bus(bus):
    """'00000000:03:00.0', '0000:03:00.0' or '0000:03:00' (Xid lines) -> '03:00.0'."""
    bus = bus.lower()
    if bus.count(":") == 2:
        bus = bus.split(":", 1)[1]
    return bus if "." in bus else bus + ".0"


def pci_gpus():
    """{bus '03:00.0': name} for every NVIDIA display device the PC has, working or not."""
    found = {}
    base = "/sys/bus/pci/devices"
    try:
        devices = os.listdir(base)
    except OSError:
        return found
    for dev in devices:
        try:
            with open(f"{base}/{dev}/vendor") as f:
                vendor = f.read().strip()
            with open(f"{base}/{dev}/class") as f:
                cls = f.read().strip()
        except OSError:
            continue
        if vendor == "0x10de" and cls[:6] in ("0x0300", "0x0302"):
            found[short_bus(dev)] = None
    if found:
        out, _ = run(["lspci", "-mm"], timeout=5)
        for line in out.splitlines():
            parts = re.findall(r'"([^"]*)"', line)
            bus = line.split(" ", 1)[0].lower()
            if bus in found and len(parts) >= 3:
                m = re.search(r"\[([^\]]+)\]", parts[2])
                found[bus] = m.group(1) if m else parts[2]
    return found


def smi_gpus():
    """({bus: (uuid, name)} the driver can reach, the driver's complaint lines)."""
    out, err = run(["nvidia-smi", "--query-gpu=pci.bus_id,uuid,name", "--format=csv,noheader"])
    ok = {}
    for line in out.splitlines():
        v = [x.strip() for x in line.split(",")]
        if len(v) == 3 and ":" in v[0]:
            ok[short_bus(v[0])] = (v[1], v[2].replace("NVIDIA GeForce ", ""))
    complaints = [l.strip() for l in (out + "\n" + err).splitlines() if l.strip() and "," not in l]
    return ok, complaints


def kernel_lines():
    """[(unix time, text)] of NVIDIA and PCIe error lines in the kernel log since boot."""
    out, _ = run(["journalctl", "-k", "-b", "--no-pager", "-o", "short-unix",
                  "--grep", r"NVRM: Xid|fallen off the bus|AER: .*(Correctable|Uncorrect|Fatal)"], timeout=20)
    lines = []
    for line in out.splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            t = float(stamp)
        except ValueError:
            continue
        lines.append((t, rest.split(" kernel: ", 1)[-1]))
    return lines


def container_gpus():
    """{container name: [GPU uuids]} from Docker's device requests."""
    out, _ = run(["docker", "ps", "-aq"])
    ids = out.split()
    if not ids:
        return {}
    out, _ = run(["docker", "inspect", "--format", "{{.Name}}\t{{json .HostConfig.DeviceRequests}}", *ids])
    result = {}
    for line in out.splitlines():
        name, _, reqs = line.partition("\t")
        try:
            reqs = json.loads(reqs) or []
        except ValueError:
            continue
        uuids = [u for r in reqs for u in (r.get("DeviceIDs") or [])]
        if uuids:
            result[name.lstrip("/")] = uuids
    return result


def when(t):
    return time.strftime("%H:%M", time.localtime(t)) if time.time() - t < 20 * 3600 \
        else time.strftime("%d %b %H:%M", time.localtime(t))


class Health:
    def __init__(self, local):
        self.local = local                 # aihub.Local, for its latest GPU and CPU readings
        self.lock = threading.Lock()
        self.problems = []
        self.first_seen = {}               # problem id -> when it first appeared
        self.log_hits = {}                 # (container, pattern index) -> [count, first, last, sample]
        self.log_since = {}                # container -> unix time its logs were read up to
        self.history = self._load_history()
        threading.Thread(target=self._run, daemon=True).start()

    # ---- the loop ---------------------------------------------------------------------------

    def _run(self):
        time.sleep(3)                      # let Local take its first reading
        while True:
            try:
                found = self.check()
            except Exception as e:         # a checker bug mustn't stop the hub
                found = [{"id": "health-check", "level": "warn", "title": "The problem checker itself failed",
                          "detail": f"{type(e).__name__}: {e}", "fix": "Tell Claude; the rest of AI Hub still works."}]
            now = time.time()
            ids = {p["id"] for p in found}
            still_open = {h["id"]: h["since"] for h in self.history if not h.get("cleared")}
            for p in found:   # an open problem keeps its first time, even across a hub restart
                p["since"] = self.first_seen.setdefault(p["id"], still_open.get(p["id"]) or p.get("since") or now)
            for pid in [k for k in self.first_seen if k not in ids]:
                del self.first_seen[pid]
            changed = False
            for h in self.history:
                if not h.get("cleared") and h["id"] not in ids:
                    h["cleared"] = now
                    changed = True
            for p in found:
                if p["id"] not in still_open:
                    self.history.append({"id": p["id"], "level": p["level"], "title": p["title"], "since": p["since"]})
                    changed = True
            if changed:
                self._save_history()
            with self.lock:
                self.problems = sorted(found, key=lambda p: (p["level"] != "error", p["since"]))
            time.sleep(CHECK_S)

    def snapshot(self):
        with self.lock:
            return {"problems": list(self.problems),
                    "history": [h for h in self.history if h.get("cleared")][-15:][::-1]}

    def check(self):
        state = self.local.snapshot()
        problems = []
        problems += self.check_gpus()
        problems += self.check_containers()
        problems += self.check_logs()
        problems += self.check_disk()
        problems += self.check_temps(state)
        return problems

    # ---- graphics cards and the kernel log --------------------------------------------------

    def check_gpus(self):
        problems = []
        pci = pci_gpus()
        if not pci:
            return problems
        ok, complaints = smi_gpus()
        klines = kernel_lines()
        xids = {}                          # (bus, code) -> [count, first, last]
        aer = []
        for t, text in klines:
            m = re.search(r"Xid \(PCI:([0-9a-f:.]+)\): (\d+)", text, re.I)
            if m:
                key = (short_bus(m.group(1)), int(m.group(2)))
                x = xids.setdefault(key, [0, t, t])
                x[0] += 1
                x[2] = t
            elif "AER:" in text:
                aer.append((t, text))
        now = time.time()
        recent_aer = [a for a in aer if now - a[0] < 3600]
        uses = container_gpus()
        present_uuids = {u for u, _ in ok.values()}

        missing = [bus for bus in pci if bus not in ok]
        for bus in missing:
            name = pci[bus] or f"card at {bus}"
            fell = xids.get((bus, 79))
            affected = sorted(c for c, uuids in uses.items() if any(u not in present_uuids for u in uuids))
            detail = [f"The PC can see the {name} in its slot ({bus}), but the NVIDIA driver can't talk to it."]
            if fell:
                detail.append(f"It fell off the PCIe bus at {when(fell[1])} (driver error Xid 79): it stopped "
                              "responding completely, so the driver gave up on it.")
            elif complaints:
                detail.append("nvidia-smi says: " + "; ".join(complaints[:2]).rstrip(".") + ".")
            if affected:
                detail.append("Containers set to use it: " + ", ".join(affected) +
                              ". Anything they do on the graphics card will fail until it's back.")
            if len(recent_aer) > AER_WARN_PER_HOUR:
                detail.append(f"There have also been {len(recent_aer)} PCIe link errors in the last hour, which "
                              "points at the slot, riser or power rather than the software.")
            problems.append({
                "id": f"gpu-missing:{bus}", "level": "error", "kind": "gpu_missing", "bus": bus, "name": name,
                "title": f"{name} isn't responding",
                "detail": " ".join(detail),
                "since": fell[1] if fell else None,
                "fix": "Restart the PC; nothing short of that brings the card back. If it happens again: shut down, "
                       "reseat the card (and its power cable, if it has one), and try another slot if there is one. A card in a "
                       "slot that runs through the chipset (often the lower x4 slot) can also drop out from PCIe "
                       "power saving or from switching speeds: turn on the slot's speed limit in AI Hub, Options & help > "
                       "PC settings, or set that slot to Gen1/Gen2 in the BIOS.",
            })

        # Driver errors on cards that are still answering (the missing ones are covered above).
        for (bus, code), (count, first, last) in sorted(xids.items(), key=lambda kv: kv[1][2]):
            level, meaning = XID.get(code, ("warn", "the driver reported an error"))
            if level is None or bus in missing or now - last > 24 * 3600:
                continue
            name = (ok.get(bus) or (None, pci.get(bus) or bus))[1]
            problems.append({
                "id": f"xid:{bus}:{code}", "level": level, "since": first,
                "title": f"Graphics card error on the {name}",
                "detail": f"The NVIDIA driver logged Xid {code}: {meaning}. "
                          f"{count} time{'s' if count != 1 else ''} since the PC started, last at {when(last)}.",
                "fix": "If it was one program crashing, restarting that container is enough. If it keeps "
                       "coming back, restart the PC." if level == "warn" else
                       "Restart the PC when you can. If it comes back, the card or its power may be failing.",
            })

        if len(recent_aer) > AER_WARN_PER_HOUR and not missing:
            ports = sorted({m.group(1) for _, txt in recent_aer if (m := re.search(r"pcieport ([0-9a-f:.]+)", txt))})
            problems.append({
                "id": "pcie-errors", "level": "warn", "since": recent_aer[0][0],
                "title": "Lots of PCIe link errors",
                "detail": f"{len(recent_aer)} PCIe errors in the last hour (port {', '.join(ports) or '?'}). They were "
                          "corrected, but this many usually means a loose card, riser or power cable, and it can come "
                          "before a card drops out.",
                "fix": "Next time the PC is off, reseat the graphics cards and their power cables.",
            })
        return problems

    # ---- containers -------------------------------------------------------------------------

    def check_containers(self):
        out, _ = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
        problems = []
        crashed = []
        for line in out.splitlines():
            try:
                name, state, status = line.split("\t")
            except ValueError:
                continue
            if state == "restarting":
                problems.append({"id": f"restarting:{name}", "level": "error",
                                 "title": f"{name} keeps restarting",
                                 "detail": f"Docker says: {status}. It starts, crashes and starts again.",
                                 "fix": f"Look at its log (docker logs --tail 50 {name}) for the reason, or ask Claude."})
            elif "(unhealthy)" in status:
                problems.append({"id": f"unhealthy:{name}", "level": "warn",
                                 "title": f"{name} is running but unhealthy",
                                 "detail": f"Its own health check is failing. Docker says: {status}.",
                                 "fix": "Restart it with ↻. If it stays unhealthy, check its log."})
            elif state == "exited":
                crashed.append(name)
        if crashed:
            out, _ = run(["docker", "inspect", "--format",
                          "{{.Name}}\t{{.State.ExitCode}}\t{{.State.OOMKilled}}\t{{.State.FinishedAt}}", *crashed])
            for line in out.splitlines():
                try:
                    name, code, oom, finished = line.split("\t")
                    code = int(code)
                    ended = calendar.timegm(time.strptime(finished[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    continue
                name = name.lstrip("/")
                if time.time() - ended > CRASH_KEEP_S:
                    continue
                if oom == "true":
                    problems.append({"id": f"oom:{name}", "level": "error", "since": ended,
                                     "title": f"{name} was killed for using too much memory",
                                     "detail": f"The PC ran short of RAM and stopped it at {when(ended)}.",
                                     "fix": "Unload models you aren't using, then start it again."})
                elif code not in (0, 130, 137, 143):   # 137/143/130 = stopped on purpose
                    problems.append({"id": f"crashed:{name}", "level": "warn", "since": ended,
                                     "title": f"{name} stopped with an error",
                                     "detail": f"It exited with code {code} at {when(ended)}.",
                                     "fix": f"Start it again. If it stops again, check docker logs --tail 50 {name}."})
        return problems

    def check_logs(self):
        out, _ = run(["docker", "ps", "--format", "{{.Names}}"])
        now = time.time()
        for name in out.split():
            since = self.log_since.get(name, now - 3600)   # on first look, the last hour
            o, e = run(["docker", "logs", "--since", str(int(since)), name], timeout=15)
            self.log_since[name] = now
            for line in (o + "\n" + e).splitlines():
                for i, (pattern, *_rest) in enumerate(LOG_PATTERNS):
                    if pattern.search(line):
                        hit = self.log_hits.setdefault((name, i), [0, now, now, ""])
                        hit[0] += 1
                        hit[2] = now
                        hit[3] = line.strip()[:240]
                        break
        problems = []
        for (name, i), (count, first, last, sample) in list(self.log_hits.items()):
            if now - last > LOG_HIT_KEEP_S:
                del self.log_hits[(name, i)]
                continue
            _, level, what, fix = LOG_PATTERNS[i]
            problems.append({"id": f"log:{name}:{i}", "level": level, "since": first,
                             "title": f"{name} {what}",
                             "detail": f"Seen {count} time{'s' if count != 1 else ''} in its log, last at {when(last)}: “{sample}”",
                             "fix": fix})
        return problems

    # ---- drive and temperatures -------------------------------------------------------------

    def check_disk(self):
        problems = []
        for path, label in (("/", "system drive"), ("/var/lib/docker", "Docker's drive")):
            try:
                d = shutil.disk_usage(path)
            except OSError:
                continue
            frac = d.used / d.total
            if frac >= 0.9:
                problems.append({"id": f"disk:{label}", "level": "error" if frac >= 0.97 else "warn",
                                 "title": f"The {label} is {frac:.0%} full",
                                 "detail": f"{d.free / 1e9:.0f} GB left. Models, pictures and Docker images fill it up.",
                                 "fix": "Delete models or pictures you don't need, or run docker image prune."})
        # / and /var/lib/docker are often the same drive; keep one.
        seen, unique = set(), []
        for p in problems:
            if p["title"].split(" is ")[1] not in seen:
                seen.add(p["title"].split(" is ")[1])
                unique.append(p)
        return unique

    def check_temps(self, state):
        problems = []
        for g in state.get("gpus") or []:
            if g.get("temp") is not None and g.get("slowdown") and g["temp"] >= g["slowdown"] - 3:
                problems.append({"id": f"hot:{g['uuid']}", "level": "warn",
                                 "title": f"The {g['name']} is at {g['temp']:.0f} °C",
                                 "detail": f"It slows itself down at {g['slowdown']:.0f} °C.",
                                 "fix": "Check its fans and the case airflow, or give it less to do."})
        c = state.get("cpu") or {}
        if c.get("temp") is not None and c.get("high") and c["temp"] >= c["high"]:
            problems.append({"id": "hot:cpu", "level": "warn", "title": f"The CPU is at {c['temp']:.0f} °C",
                             "detail": f"Its 'high' mark is {c['high']:.0f} °C.",
                             "fix": "Check the CPU cooler and case fans."})
        return problems

    # ---- history ----------------------------------------------------------------------------

    def _load_history(self):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save_history(self):
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.history[-HISTORY_KEEP:], f, indent=1)
            os.replace(tmp, HISTORY_FILE)
        except OSError:
            pass
