#!/usr/bin/env python3
"""AI Hub: a web page with what the aimon panel shows, for every PC at once, plus GPU and CPU
temperatures, quick links and the docs about the setup.

The same program runs on each PC (a systemd user service, port 8090). Each one reads its own
machine and answers /api/local; the page asks whichever hub it was opened from for /api/state,
which adds the other PCs' /api/local. So the server's page shows the main PC while that's on,
and offers to wake it when it's off.

Each PC also reports its problems (health.py: a card that stopped responding, driver errors,
containers crashing, bad log lines, full drive, heat), with what to do about them.

Settings: ~/.config/aihub/config.json (see DEFAULTS). Only standard-library Python, aimon_core
and health (next to this file) and sysmon.py (~/Documents/sysmon) are needed.
"""
import base64
import binascii
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.expanduser("~/Documents/sysmon"))
import aimon_core as core  # noqa: E402
import health  # noqa: E402
import sysmon  # noqa: E402

CONFIG = os.path.expanduser("~/.config/aihub/config.json")
DEFAULTS = {
    "name": socket.gethostname(),
    "port": 8090,
    "lan_ip": None,          # address other devices reach this PC on; links use it instead of 127.0.0.1
    "peers": [],             # [{"name", "url", "mac"}]: the other PCs' hubs; mac is for Wake-on-LAN
    "docs_dir": None,        # folder of docs this hub serves; None = the page uses docs_url instead
    "docs_url": None,        # another hub that has the docs
    "links": [],             # [{"name", "url", "note", "icon"}]: quick links, checked for an answer;
                             # icon = a file in web/icons shown on the link (e.g. "ttr-logo.png")
    "allow_reboot": False,   # True shows a Restart PC button; needs passwordless `sudo systemctl reboot`
    "pcie_caps": [],         # [{"port": "00:1c.0", "label", "gen": 1}]: slots that may be speed-capped (On/Off on the page)
    "trusted_origins": [],   # extra page addresses allowed to POST, e.g. "https://server.your-tailnet.ts.net:8090"
    "hide_rows": [],         # row keys the page leaves out, such as a model on the wrong card's Ollama
    "dictate": None,         # {"container": "open-webui", "model": "base"}: the 🎤 buttons' speech-to-text, using the
                             # Whisper model already in that container. Only the hub with the docs needs it.
    "allow": ["127.0.0.0/8", "::1/128", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12",
              "100.64.0.0/10"],   # who may use the hub at all: this PC, the home network, Tailscale
}
POLL_S = 2
LINK_POLL_S = 15
PEER_TIMEOUT = 1.5
FAILED_SHOW_S = 30
PAGE = os.path.join(HERE, "web", "index.html")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError) as e:
        print(f"aihub: using defaults ({e})", file=sys.stderr)
    cfg["allow"] = [ipaddress.ip_network(n) for n in cfg["allow"]]
    if cfg["docs_dir"]:
        cfg["docs_dir"] = os.path.expanduser(cfg["docs_dir"])
    return cfg


def cpu_limits():
    """(high, critical) °C of the CPU package from coretemp, or (None, None)."""
    for hw in os.listdir("/sys/class/hwmon"):
        base = f"/sys/class/hwmon/{hw}"
        try:
            with open(f"{base}/name") as f:
                if f.read().strip() != "coretemp":
                    continue
            vals = []
            for kind in ("max", "crit"):
                with open(f"{base}/temp1_{kind}") as f:
                    vals.append(int(f.read()) / 1000)
            return tuple(vals)
        except (OSError, ValueError):
            continue
    return None, None


def gpu_limits(index):
    """(slowdown, shutdown) °C of an NVIDIA card; the same reading as the sysmon badge."""
    try:
        out = subprocess.run(["nvidia-smi", "-q", "-d", "TEMPERATURE", "-i", str(index)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    v = {}
    for line in out.splitlines():
        key, _, val = line.partition(":")
        try:
            v[key.strip()] = float(val.split()[0])
        except (ValueError, IndexError):
            pass
    if "GPU T.Limit Temp" in v and "GPU Current Temp" in v:
        limit = v["GPU Current Temp"] + v["GPU T.Limit Temp"]
        return (limit - v.get("GPU Max Operating T.Limit Temp", 0),
                limit - v.get("GPU Shutdown T.Limit Temp", -5))
    return v.get("GPU Max Operating Temp") or v.get("GPU Slowdown Temp"), v.get("GPU Shutdown Temp")


def pid_container(pid):
    """Docker container id a process runs in, or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            text = f.read()
    except OSError:
        return None
    i = text.find("docker-")
    return text[i + 7:i + 71] if i >= 0 else None


def answers(url):
    """True if anything answers HTTP at url (even with an error page)."""
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


class Local:
    """Polls this PC on a background thread and runs start and stop requests, like aimon's Monitor."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.web = {}
        self.state = {"name": cfg["name"], "online": True, "rows": [], "error": "Starting…"}
        self.pending = {}
        self.wake = threading.Event()
        self.links = {}
        self.cpu_limits = cpu_limits()
        self.gpu_limits = {}
        threading.Thread(target=self._run, daemon=True).start()
        if cfg["links"]:
            threading.Thread(target=self._check_links, daemon=True).start()

    def _gpus(self, names_by_id):
        gpus = sysmon.read_gpus() or []
        for g in gpus:
            if g["index"] not in self.gpu_limits:
                self.gpu_limits[g["index"]] = gpu_limits(g["index"])
            g["slowdown"], g["shutdown"] = self.gpu_limits[g["index"]]
            users = {}
            for pid, pname, mem in g.pop("procs"):
                who = names_by_id.get(pid_container(pid)) or pname
                users[who] = users.get(who, 0) + (mem or 0)
            g["users"] = [{"name": k, "mem": v} for k, v in sorted(users.items(), key=lambda x: -x[1])]
        return gpus

    def _run(self):
        prev = sysmon.read_cpu_times()
        while True:
            try:
                rows, error = core.read_rows(self.web), None
                rows = [r for r in rows if r["key"] not in self.cfg["hide_rows"]]
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
                rows, error = [], f"Can't reach Docker: {e}"
            for r in rows:
                self._fix_url(r, rows)
            names = {r["id"]: r["name"] for r in rows if r.get("id")}
            cur = sysmon.read_cpu_times()
            pct = sysmon.cpu_percent(prev, cur)
            prev = cur
            mem = sysmon.read_meminfo()
            disk = shutil.disk_usage("/")
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
            state = {
                "name": self.cfg["name"], "host": socket.gethostname(), "online": True,
                "time": time.time(), "uptime": uptime, "load": os.getloadavg(),
                "cpu": {"pct": pct.get("cpu"), "temp": sysmon.read_cpu_temp(), "threads": len(pct) - 1,
                        "high": self.cpu_limits[0], "crit": self.cpu_limits[1]},
                "ram": {"used": mem["MemTotal"] - mem["MemAvailable"], "total": mem["MemTotal"]},
                "swap": {"used": mem["SwapTotal"] - mem["SwapFree"], "total": mem["SwapTotal"]},
                "disk": {"used": disk.used, "total": disk.total},
                "gpus": self._gpus(names), "rows": rows, "error": error,
            }
            with self.lock:
                self.state = state
            self.wake.wait(POLL_S)
            self.wake.clear()

    def _fix_url(self, row, rows):
        """aimon's links point at 127.0.0.1; from another device they need this PC's address.
        A port Docker bound to 127.0.0.1 only can't be reached from elsewhere, so it's marked."""
        if not row.get("url"):
            return
        host = next((r for r in rows if r["kind"] == "docker" and r["name"] == row["web_host"]), None)
        bind = host and host.get("bind")
        if bind in ("127.0.0.1", "::1"):
            row["local_only"] = True
        elif self.cfg["lan_ip"]:
            row["url"] = row["url"].replace("127.0.0.1", self.cfg["lan_ip"], 1)
        row["host_off"] = bool(host and host["state"] == "off")

    def _check_links(self):
        while True:
            with ThreadPoolExecutor(8) as pool:
                ups = list(pool.map(lambda link: answers(link.get("check") or link["url"]), self.cfg["links"]))
            self.links = {link["url"]: up for link, up in zip(self.cfg["links"], ups)}
            time.sleep(LINK_POLL_S)

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
        now = time.monotonic()
        rows = []
        for r in state["rows"]:
            r = dict(r)
            p = self.pending.get(r["key"])
            if isinstance(p, str):
                r["state"], r["sub"], r["busy"] = "busy", f"{p.capitalize()}…", True
            elif p and now - p[2] < FAILED_SHOW_S:
                r["sub"], r["failed"] = f"Failed: {p[1]}", True
            rows.append(r)
        state["rows"] = rows
        return state

    def act(self, key, action):
        """Starts, stops or restarts the row `key`, or with action unload_all every loaded model."""
        rows = self.snapshot()["rows"]
        if action == "unload_all":
            loaded = [r for r in rows if r["kind"] != "docker" and r["state"] == "on"]
            for r in loaded:
                self._background(r["key"], "stopping", lambda r=r: core.stop_row(r))
            return f"Unloading {len(loaded)} model(s)"
        row = next((r for r in rows if r["key"] == key), None)
        if not row:
            raise ValueError(f"No such row: {key}")
        if row.get("busy"):
            raise ValueError(f"{row['name']} is busy")
        if action == "start" and row["kind"] != "comfy":
            work, label = (lambda: core.start_row(row)), "starting"
        elif action == "stop":
            work, label = (lambda: core.stop_row(row)), "stopping"
        elif action == "restart" and row["kind"] == "docker":
            work, label = (lambda: core.run(["docker", "restart", row["name"]], timeout=180)), "restarting"
        else:
            raise ValueError(f"Can't {action} {row['name']}")
        self._background(key, label, work)
        return f"{label.capitalize()} {row['name']}"

    def _background(self, key, label, work):
        def run_work():
            try:
                work()
                self.pending.pop(key, None)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
                self.pending[key] = ("failed", str(e) or type(e).__name__, time.monotonic())
            self.wake.set()

        self.pending[key] = label
        threading.Thread(target=run_work, daemon=True).start()


def fetch_json(url, body=None, timeout=PEER_TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def peer_state(peer):
    try:
        s = fetch_json(peer["url"].rstrip("/") + "/api/local")
        s["name"] = peer["name"]
    except (OSError, ValueError) as e:
        s = {"name": peer["name"], "online": False, "error": str(getattr(e, "reason", e))}
    s["peer"] = True
    s["can_wake"] = bool(peer.get("mac"))
    return s


def can_reboot():
    """Whether this hub may restart its PC: switched on in the config and sudo allows it without a password."""
    if not CFG.get("allow_reboot"):
        return False
    try:
        return subprocess.run(["sudo", "-n", "-l", "/usr/bin/systemctl", "reboot"], capture_output=True,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def restart_now(force):
    """Restarts this PC in 3 s. force skips waiting for programs to stop (they hang on a dead card)."""
    cmd = ["sudo", "-n", "/usr/bin/systemctl", "reboot"] + (["--force"] if force else [])
    print(f"Running: {' '.join(cmd[2:])}", flush=True)
    subprocess.run(["sync"], timeout=30)
    threading.Timer(3, lambda: subprocess.run(cmd)).start()


def reboot_soon(confirm):
    """Restarts this PC a few seconds from now, so the answer gets back to the page first."""
    if not CAN_REBOOT:
        raise ValueError(f"{CFG['name']} doesn't allow restarting from AI Hub")
    if (confirm or "").strip().lower() != CFG["name"].lower():
        raise ValueError(f"Type the PC's name ({CFG['name']}) to confirm")
    # A card that has fallen off the bus can hang a normal shutdown for good: the containers using it
    # never finish stopping. Then a forced restart (kill everything, sync and unmount the drives) is
    # the safe choice; otherwise shut down normally.
    stuck = any(p.get("kind") == "gpu_missing" for p in HEALTH.snapshot()["problems"])
    print(f"Restart requested from AI Hub at {time.strftime('%H:%M:%S')}", flush=True)
    restart_now(stuck)
    return f"{CFG['name']} is restarting. It takes a couple of minutes; this page reconnects by itself."


PCIE_STATE = os.path.expanduser("~/.local/state/aihub/pcie_caps.json")
PCIE_SPEEDS = {1: "2.5 GT/s", 2: "5 GT/s", 3: "8 GT/s", 4: "16 GT/s", 5: "32 GT/s"}


class PcieCaps:
    """Speed caps for PCIe slots with an unreliable link (config "pcie_caps": [{"port", "label", "gen"}]).

    A card whose link never settles at full speed can fall off the bus while it keeps switching speeds.
    Capping the slot's root port (Link Control 2, Target Link Speed) stops it trying the faster speeds.
    Switching it on or off is saved on this PC and applied again whenever the hub starts, so it lasts
    across restarts. Only ports in the config can be changed. Needs passwordless sudo for setpci.
    """
    CACHE_S = 10

    def __init__(self, entries):
        self.entries = {}
        for e in entries or []:
            port = str(e.get("port", "")).lower()
            if re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", port):
                self.entries[port] = {"label": e.get("label") or port, "gen": int(e.get("gen", 1))}
        self.lock = threading.Lock()
        self.errors = {}
        self.cache = (0, [])
        try:
            with open(PCIE_STATE) as f:
                self.on = {p: bool(v) for p, v in json.load(f).items() if p in self.entries}
        except (OSError, ValueError):
            self.on = {}
        for port, on in self.on.items():
            if on:
                try:
                    self._write(port, self.entries[port]["gen"])
                    print(f"PCIe cap: {port} limited to Gen{self.entries[port]['gen']}", flush=True)
                except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                    self.errors[port] = str(e)

    @staticmethod
    def _setpci(*args):
        r = subprocess.run(["sudo", "-n", "setpci", *args], capture_output=True, text=True, timeout=10)
        if r.returncode:
            raise ValueError((r.stderr or r.stdout).strip() or f"setpci failed ({r.returncode})")
        return r.stdout.strip()

    def _write(self, port, gen):
        self._setpci("-s", port, f"CAP_EXP+30.w={gen:x}:f")

    def _max_gen(self, port):
        return int(self._setpci("-s", port, "CAP_EXP+0c.l"), 16) & 0xF

    def set(self, port, on):
        port = str(port or "").lower()
        if port not in self.entries:
            raise ValueError(f"No speed setting for slot {port} on {CFG['name']}")
        with self.lock:
            gen = self.entries[port]["gen"] if on else self._max_gen(port)
            self._write(port, gen)
            self.on[port] = bool(on)
            self.errors.pop(port, None)
            os.makedirs(os.path.dirname(PCIE_STATE), exist_ok=True)
            tmp = PCIE_STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.on, f)
            os.replace(tmp, PCIE_STATE)
            self.cache = (0, [])
        label = self.entries[port]["label"]
        if on:
            return (f"{label}: limited to Gen{gen} ({PCIE_SPEEDS.get(gen, '?')}). "
                    "It stays that way after restarts until you switch it off.")
        return (f"{label}: back to full speed (Gen{gen}). The card speeds up the next time its link "
                "renegotiates, at the latest after a restart.")

    def snapshot(self):
        if not self.entries:
            return []
        if time.time() - self.cache[0] < self.CACHE_S:
            return self.cache[1]
        out = []
        for port, e in self.entries.items():
            item = {"port": port, "label": e["label"], "gen": e["gen"], "cap_speed": PCIE_SPEEDS.get(e["gen"]),
                    "on": self.on.get(port, False), "error": self.errors.get(port)}
            try:
                with open(f"/sys/bus/pci/devices/0000:{port}/current_link_speed") as f:
                    item["speed"] = f.read().split(" PCIe")[0].strip()
                with open(f"/sys/bus/pci/devices/0000:{port}/current_link_width") as f:
                    item["width"] = "x" + f.read().strip()
                item["target"] = int(self._setpci("-s", port, "CAP_EXP+30.w"), 16) & 0xF
                item["max"] = self._max_gen(port)
                item["max_speed"] = PCIE_SPEEDS.get(item["max"])
            except (OSError, ValueError, subprocess.TimeoutExpired) as err:
                item["error"] = item["error"] or str(err)
            out.append(item)
        self.cache = (time.time(), out)
        return out


SETTINGS_STATE = os.path.expanduser("~/.local/state/aihub/settings.json")


class AutoRestart:
    """Optional: restart this PC by itself when a graphics card has stopped responding (only a restart
    brings one back). It waits for the PC to go quiet first so it doesn't cut off a chat or a document
    halfway, but not for ever. Guards against a restart loop if the card is dead for good."""
    CHECK_S = 15
    IDLE_NEEDED_S = 180          # quiet this long in a row before restarting
    MAX_WAIT_S = 30 * 60         # restart anyway after waiting this long for quiet
    BOOT_GRACE_S = 300           # leave a freshly started PC alone
    MAX_PER_DAY = 3
    GPU_BUSY = 10                # % : a working card busier than this means something's running
    CPU_BUSY = 30

    def __init__(self):
        self.lock = threading.Lock()
        try:
            with open(SETTINGS_STATE) as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        self.on = bool(s.get("auto_restart"))
        self.done = [t for t in s.get("auto_restarts", []) if time.time() - t < 86400]
        self.waiting_since = None
        self.quiet_since = None
        self.status = ""
        if CAN_REBOOT:
            threading.Thread(target=self._run, daemon=True).start()

    def _save(self):
        os.makedirs(os.path.dirname(SETTINGS_STATE), exist_ok=True)
        tmp = SETTINGS_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"auto_restart": self.on, "auto_restarts": self.done}, f)
        os.replace(tmp, SETTINGS_STATE)

    def set(self, on):
        if not CAN_REBOOT:
            raise ValueError(f"{CFG['name']} doesn't allow restarting from AI Hub")
        with self.lock:
            self.on = bool(on)
            self.waiting_since = self.quiet_since = None
            self.status = ""
            self._save()
        return (f"{CFG['name']} will restart by itself when a graphics card stops responding, once it's quiet."
                if on else f"{CFG['name']} won't restart by itself any more.")

    def _busy(self, state):
        """What's keeping the PC busy right now, or None when it's quiet."""
        for g in state.get("gpus") or []:
            if (g.get("util") or 0) > self.GPU_BUSY:
                return f"the {g.get('name', 'card')} is {g['util']}% busy"
        cpu = (state.get("cpu") or {}).get("pct")
        if cpu is not None and cpu > self.CPU_BUSY:
            return f"the CPU is {round(cpu)}% busy"
        return None

    def _run(self):
        while True:
            time.sleep(self.CHECK_S)
            try:
                self._tick()
            except Exception as e:      # never let this thread die quietly
                self.status = f"Auto-restart check failed: {type(e).__name__}: {e}"

    def _tick(self):
        with self.lock:
            if not self.on:
                return
            dead = [p for p in HEALTH.snapshot()["problems"] if p.get("kind") == "gpu_missing"]
            if not dead:
                self.waiting_since = self.quiet_since = None
                self.status = ""
                return
            now = time.time()
            state = LOCAL.snapshot()
            if state.get("uptime", 0) < self.BOOT_GRACE_S:
                self.status = "Just started; giving it a few minutes first."
                return
            self.done = [t for t in self.done if now - t < 86400]
            if len(self.done) >= self.MAX_PER_DAY:
                self.status = (f"Not restarting: it already restarted by itself {len(self.done)} times in the "
                               "last 24 hours, so the card probably needs a hand. Restart it yourself after checking it.")
                return
            self.waiting_since = self.waiting_since or now
            busy = self._busy(state)
            if busy:
                self.quiet_since = None
            else:
                self.quiet_since = self.quiet_since or now
            waited = now - self.waiting_since
            quiet = now - self.quiet_since if self.quiet_since else 0
            if quiet < self.IDLE_NEEDED_S and waited < self.MAX_WAIT_S:
                left = round((self.MAX_WAIT_S - waited) / 60)
                self.status = (f"Will restart when it's quiet: {busy}. Restarting anyway in about {left} min."
                               if busy else f"Quiet for {round(quiet)} s; restarting after {self.IDLE_NEEDED_S // 60} min of quiet.")
                return
            why = "it was quiet" if quiet >= self.IDLE_NEEDED_S else "it waited 30 min for quiet"
            self.done.append(now)
            self._save()
            self.status = "Restarting now."
            names = ", ".join(p.get("name", "a card") for p in dead)
            print(f"Auto-restart at {time.strftime('%H:%M:%S')}: {names} not responding, {why}", flush=True)
            restart_now(force=True)

    def snapshot(self):
        if not CAN_REBOOT:
            return None
        return {"on": self.on, "status": self.status, "count_24h": len(self.done)}


def wake_on_lan(mac):
    """Sends a Wake-on-LAN magic packet to the whole home network."""
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"Bad MAC address: {mac}")
    packet = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(packet, ("255.255.255.255", port))


DOC_TYPES = {".md": "markdown", ".txt": "text", ".pdf": "pdf", ".html": "html", ".htm": "html",
             ".png": "image", ".jpg": "image", ".jpeg": "image"}


def list_docs(folder):
    """Docs in the folder, described by its index.json where it has an entry, newest first.
    index.json: {"files": {"<file name>": {"title", "about", "group", "live"}}, "groups": [order]}"""
    try:
        with open(os.path.join(folder, "index.json")) as f:
            index = json.load(f)
    except (OSError, ValueError):
        index = {}
    meta = index.get("files", {})
    docs = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        kind = DOC_TYPES.get(os.path.splitext(name)[1].lower())
        if name.startswith(".") or name == "index.json" or not kind or not os.path.isfile(path):
            continue
        m = meta.get(name, {})
        st = os.stat(path)
        docs.append({"file": name, "kind": kind, "size": st.st_size, "modified": st.st_mtime,
                     "title": m.get("title") or os.path.splitext(name)[0], "about": m.get("about", ""),
                     "group": m.get("group", "Other"), "live": m.get("live"),
                     "tags": m.get("tags", []), "importance": m.get("importance", "normal"),
                     "comments": m.get("comments", [])})
    # Docs that live somewhere else (a Claude Doc, a web page), with no file here.
    for link in index.get("links", []):
        docs.append({"file": None, "kind": "link", "size": None, "modified": None,
                     "tags": [], "importance": "normal", "comments": [], **link})
    docs.sort(key=lambda d: -(d["modified"] or 0))
    return {"docs": docs, "groups": index.get("groups", [])}


# ---- Editing docs from the page: details (title, about, category, importance, tags), comments, uploads.
# Everything is kept in the docs folder's index.json, next to the hand-written titles.
UPLOAD_TYPES = {".pdf": "pdf", ".md": "markdown", ".txt": "text"}
MAX_UPLOAD = 25 * 1024 * 1024
IMPORTANCE = ("high", "normal", "low")
INDEX_LOCK = threading.Lock()


def read_index(folder):
    try:
        with open(os.path.join(folder, "index.json")) as f:
            index = json.load(f)
    except FileNotFoundError:
        index = {}
    # A damaged index.json is never overwritten: that would lose every title and comment.
    if not isinstance(index, dict):
        raise ValueError("index.json isn't a JSON object; fix it by hand first")
    index.setdefault("groups", [])
    index.setdefault("files", {})
    return index


def write_index(folder, index):
    tmp = os.path.join(folder, ".index.json.tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(folder, "index.json"))


def doc_entry(index, folder, doc):
    """The index entry to change for a doc: a file in the folder (by name) or a link (by URL)."""
    doc = str(doc or "")
    if doc and "/" not in doc and not doc.startswith(".") and doc != "index.json" \
            and os.path.splitext(doc)[1].lower() in DOC_TYPES and os.path.isfile(os.path.join(folder, doc)):
        return index["files"].setdefault(doc, {})
    for link in index.get("links", []):
        if link.get("url") == doc:
            return link
    raise ValueError("No such document")


def clean_tags(tags):
    if isinstance(tags, str):
        tags = tags.split(",")
    out = []
    for t in tags or []:
        t = " ".join(str(t).split())[:30]
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:12]


def apply_meta(index, entry, req):
    for key, limit in (("title", 200), ("about", 500)):
        if key in req:
            entry[key] = " ".join(str(req[key] or "").split())[:limit]
    if "group" in req:
        group = " ".join(str(req["group"] or "").split())[:40] or "Other"
        entry["group"] = group
        if group not in index["groups"] and group != "Other":
            index["groups"].append(group)
    if "importance" in req:
        if req["importance"] not in IMPORTANCE:
            raise ValueError("Importance must be high, normal or low")
        entry["importance"] = req["importance"]
    if "tags" in req:
        entry["tags"] = clean_tags(req["tags"])


def set_meta(folder, req):
    with INDEX_LOCK:
        index = read_index(folder)
        apply_meta(index, doc_entry(index, folder, req.get("doc")), req)
        write_index(folder, index)


def add_comment(folder, doc, text, by):
    text = str(text or "").strip()
    if not text:
        raise ValueError("The comment is empty")
    if len(text) > 10000:
        raise ValueError("Comments are limited to 10,000 characters")
    comment = {"id": os.urandom(5).hex(), "text": text, "by": by, "at": round(time.time())}
    with INDEX_LOCK:
        index = read_index(folder)
        doc_entry(index, folder, doc).setdefault("comments", []).append(comment)
        write_index(folder, index)
    return comment


def delete_comment(folder, doc, cid):
    with INDEX_LOCK:
        index = read_index(folder)
        entry = doc_entry(index, folder, doc)
        before = len(entry.get("comments", []))
        entry["comments"] = [c for c in entry.get("comments", []) if c.get("id") != cid]
        if len(entry["comments"]) == before:
            raise ValueError("That comment is already gone")
        write_index(folder, index)


def safe_doc_name(name, ext):
    """A plain file name for the docs folder: no folders, no hidden files, a known extension."""
    stem = os.path.splitext(os.path.basename(str(name or "").replace("\\", "/")))[0]
    stem = "".join(c for c in stem if c.isprintable() and c not in '/\\:*?"<>|').strip(" .")[:100]
    return (stem or "Untitled") + ext


def save_upload(folder, req):
    """A dropped PDF / Markdown / text file (base64 in "data") or a pasted text block ("text")."""
    if req.get("text") is not None:
        ext = ".md" if req.get("format", "md") == "md" else ".txt"
        raw = str(req["text"]).encode("utf-8")
        name = safe_doc_name(req.get("title") or req.get("name"), ext)
        if not raw.strip():
            raise ValueError("The text is empty")
    else:
        ext = os.path.splitext(str(req.get("name") or ""))[1].lower()
        if ext == ".markdown":
            ext = ".md"
        if ext not in UPLOAD_TYPES:
            raise ValueError("Only PDF, Markdown (.md) and text (.txt) files can be added")
        try:
            raw = base64.b64decode(str(req.get("data") or ""), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("The file didn't arrive in one piece; try again")
        name = safe_doc_name(req.get("name"), ext)
        if ext == ".pdf" and not raw.startswith(b"%PDF"):
            raise ValueError("That file isn't a PDF")
        if ext != ".pdf":
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("That file isn't plain text")
    if len(raw) > MAX_UPLOAD:
        raise ValueError("Files are limited to 25 MB")
    if not raw.strip():
        raise ValueError("That file is empty")
    with INDEX_LOCK:
        index = read_index(folder)
        stem, n = os.path.splitext(name)[0], 2
        while os.path.exists(os.path.join(folder, name)) or name in index["files"]:
            name = f"{stem} ({n}){ext}"
            n += 1
        tmp = os.path.join(folder, ".upload.tmp")
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(folder, name))
        entry = index["files"].setdefault(name, {})
        meta = {k: req[k] for k in ("title", "about", "group", "importance", "tags") if k in req}
        meta.setdefault("title", os.path.splitext(name)[0])
        apply_meta(index, entry, meta)
        write_index(folder, index)
    return name


# ---------- dictation: 🎤 on the page records, Whisper in a container writes it out ----------
MAX_AUDIO = 20 * 1024 * 1024   # about 10 minutes of browser (Opus) audio
DICTATE_LOCK = threading.Lock()   # one at a time, so a few clips can't pile up on the CPU
DICTATE_SCRIPT = r'''
import os, sys, tempfile
from faster_whisper import WhisperModel
with tempfile.NamedTemporaryFile(suffix=".audio") as f:
    f.write(sys.stdin.buffer.read())
    f.flush()
    m = WhisperModel(sys.argv[1], device="cpu", compute_type="int8", local_files_only=True,
                     download_root=os.environ.get("WHISPER_MODEL_DIR") or "/app/backend/data/cache/whisper/models")
    segs, _ = m.transcribe(f.name, language="en", vad_filter=True)
    print(" ".join(s.text.strip() for s in segs).strip())
'''


def transcribe(audio):
    """Speech to text with the faster-whisper model Open WebUI already has (no new install)."""
    conf = CFG["dictate"]
    if not conf:
        raise ValueError("Dictation isn't set up on this hub")
    if not audio:
        raise ValueError("No sound was recorded")
    with DICTATE_LOCK:
        try:
            r = subprocess.run(["docker", "exec", "-i", conf.get("container", "open-webui"), "python3", "-c",
                                DICTATE_SCRIPT, conf.get("model", "base")],
                               input=audio, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise ValueError("Writing it out took too long; try a shorter recording")
    if r.returncode:
        last = (r.stderr.decode(errors="replace").strip().splitlines() or ["unknown error"])[-1]
        raise ValueError(f"Couldn't write it out: {last[:300]}")
    return r.stdout.decode(errors="replace").strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "AIHub/1"

    def log_message(self, fmt, *args):   # quiet: the page polls every few seconds
        pass

    def allowed(self):
        ip = ipaddress.ip_address(self.client_address[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if any(ip in net for net in CFG["allow"]):
            return True
        self.send_json({"error": "Not allowed from this address"}, 403)
        return False

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")   # the main PC's page reads the server's docs list
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_json({"error": "Not found"}, 404)
            return
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=65536):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def origin_ok(self):
        """Requests from a web page must come from a hub page (any home-network address on a
        hub's port, or localhost / *.local), so another website open in a browser here can't
        drive the hub. Scripts (curl) send no Origin and only face the address check."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin.rstrip("/") in CFG["trusted_origins"]:
            return True   # the hub's own https address (Tailscale), listed in the config
        u = urllib.parse.urlsplit(origin)
        host = (u.hostname or "").lower()
        ports = {CFG["port"]} | {urllib.parse.urlsplit(p["url"]).port or 80 for p in CFG["peers"]}
        if u.scheme != "http" or (u.port or 80) not in ports:
            return False
        if host == "localhost" or host.endswith(".local"):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in net for net in CFG["allow"])

    def who(self, req):
        """Name shown on a comment: the one the page sends, else the PC it came from."""
        name = " ".join(str(req.get("by") or "").split())[:40]
        if name:
            return name
        ip = self.client_address[0].removeprefix("::ffff:")
        if ip.startswith("127.") or ip == "::1" or ip == CFG["lan_ip"]:
            return CFG["name"]
        for p in CFG["peers"]:
            if urllib.parse.urlsplit(p["url"]).hostname == ip:
                return p["name"]
        return ip

    def do_OPTIONS(self):
        # The main PC's page posts comments and uploads to the server's hub: the browser asks first.
        if not self.allowed():
            return
        if not self.origin_ok():
            self.send_json({"error": "Not allowed from that page"}, 403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def docs_post(self, path):
        folder = CFG["docs_dir"]
        if not folder:
            raise ValueError("This hub has no docs folder; use the server's hub")
        if path == "/api/docs/upload":
            req = self.body(MAX_UPLOAD * 4 // 3 + 65536)
            name = save_upload(folder, req)
            self.send_json({"ok": True, "file": name, "message": f"Added {name}"})
            return
        req = self.body()
        if path == "/api/docs/meta":
            set_meta(folder, req)
            self.send_json({"ok": True, "message": "Saved"})
        elif path == "/api/docs/comment":
            self.send_json({"ok": True, "comment": add_comment(folder, req.get("doc"), req.get("text"), self.who(req))})
        elif path == "/api/docs/comment/delete":
            delete_comment(folder, req.get("doc"), req.get("id"))
            self.send_json({"ok": True, "message": "Comment deleted"})
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_GET(self):
        if not self.allowed():
            return
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            self.send_file(PAGE, "text/html")
        elif path.startswith("/icons/") and re.fullmatch(r"[\w.-]+\.(png|svg)", path[7:]):
            self.send_file(os.path.join(HERE, "web", "icons", path[7:]))   # link logos, e.g. the TTR app's
        elif path == "/api/local":
            self.send_json(local_state())
        elif path == "/api/state":
            with ThreadPoolExecutor(max(1, len(CFG["peers"]))) as pool:
                peers = list(pool.map(peer_state, CFG["peers"]))
            links = [dict(link, up=LOCAL.links.get(link["url"])) for link in CFG["links"]]
            self.send_json({"self": CFG["name"], "machines": [local_state()] + peers, "links": links,
                            "docs_url": None if CFG["docs_dir"] else CFG["docs_url"]})
        elif path == "/api/docs":
            if not CFG["docs_dir"]:
                self.send_json({"docs": [], "groups": [], "remote": CFG["docs_url"]})
            else:
                self.send_json(list_docs(CFG["docs_dir"]))
        elif path.startswith("/docs/") and CFG["docs_dir"]:
            name = path[6:]
            # Only files the listing shows: no paths, no hidden files. Symlinks to docs kept elsewhere are fine.
            if name in {d["file"] for d in list_docs(CFG["docs_dir"])["docs"]}:
                self.send_file(os.path.join(CFG["docs_dir"], name))
            else:
                self.send_json({"error": "Not found"}, 404)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.allowed():
            return
        if not self.origin_ok():
            self.send_json({"ok": False, "error": "Not allowed from that page"}, 403)
            return
        try:
            if self.path.startswith("/api/docs/"):
                self.docs_post(self.path.split("?", 1)[0])
                return
            if self.path == "/api/dictate":   # the body is the recording itself, not JSON
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_AUDIO:
                    raise ValueError("That recording is too long (about 10 minutes at most)")
                self.send_json({"ok": True, "text": transcribe(self.rfile.read(n))})
                return
            req = self.body()
            if self.path == "/api/local/action":
                self.send_json({"ok": True, "message": LOCAL.act(req.get("key"), req.get("action"))})
            elif self.path == "/api/action":
                machine = req.get("machine")
                if machine == CFG["name"]:
                    self.send_json({"ok": True, "message": LOCAL.act(req.get("key"), req.get("action"))})
                    return
                peer = next((p for p in CFG["peers"] if p["name"] == machine), None)
                if not peer:
                    raise ValueError(f"Unknown PC: {machine}")
                try:
                    self.send_json(fetch_json(peer["url"].rstrip("/") + "/api/local/action",
                                              {"key": req.get("key"), "action": req.get("action")}, timeout=10))
                except urllib.error.HTTPError as e:
                    self.send_json(json.loads(e.read() or b"{}") or {"error": str(e)}, e.code)
            elif self.path == "/api/local/reboot":
                self.send_json({"ok": True, "message": reboot_soon(req.get("confirm"))})
            elif self.path == "/api/reboot":
                machine = req.get("machine")
                if machine == CFG["name"]:
                    self.send_json({"ok": True, "message": reboot_soon(req.get("confirm"))})
                    return
                peer = next((p for p in CFG["peers"] if p["name"] == machine), None)
                if not peer:
                    raise ValueError(f"Unknown PC: {machine}")
                # The peer checks the name against its own; pass the typed text through.
                try:
                    self.send_json(fetch_json(peer["url"].rstrip("/") + "/api/local/reboot",
                                              {"confirm": req.get("confirm")}, timeout=10))
                except urllib.error.HTTPError as e:
                    self.send_json(json.loads(e.read() or b"{}") or {"error": str(e)}, e.code)
            elif self.path == "/api/local/pcie":
                self.send_json({"ok": True, "message": PCIE.set(req.get("port"), bool(req.get("on")))})
            elif self.path == "/api/pcie":
                machine = req.get("machine")
                if machine == CFG["name"]:
                    self.send_json({"ok": True, "message": PCIE.set(req.get("port"), bool(req.get("on")))})
                    return
                peer = next((p for p in CFG["peers"] if p["name"] == machine), None)
                if not peer:
                    raise ValueError(f"Unknown PC: {machine}")
                try:
                    self.send_json(fetch_json(peer["url"].rstrip("/") + "/api/local/pcie",
                                              {"port": req.get("port"), "on": bool(req.get("on"))}, timeout=15))
                except urllib.error.HTTPError as e:
                    self.send_json(json.loads(e.read() or b"{}") or {"error": str(e)}, e.code)
            elif self.path == "/api/local/auto-restart":
                self.send_json({"ok": True, "message": AUTO.set(bool(req.get("on")))})
            elif self.path == "/api/auto-restart":
                machine = req.get("machine")
                if machine == CFG["name"]:
                    self.send_json({"ok": True, "message": AUTO.set(bool(req.get("on")))})
                    return
                peer = next((p for p in CFG["peers"] if p["name"] == machine), None)
                if not peer:
                    raise ValueError(f"Unknown PC: {machine}")
                try:
                    self.send_json(fetch_json(peer["url"].rstrip("/") + "/api/local/auto-restart",
                                              {"on": bool(req.get("on"))}, timeout=10))
                except urllib.error.HTTPError as e:
                    self.send_json(json.loads(e.read() or b"{}") or {"error": str(e)}, e.code)
            elif self.path == "/api/wake":
                peer = next((p for p in CFG["peers"] if p["name"] == req.get("machine")), None)
                if not peer or not peer.get("mac"):
                    raise ValueError("No Wake-on-LAN address for that PC")
                wake_on_lan(peer["mac"])
                self.send_json({"ok": True, "message": f"Wake signal sent to {peer['name']}"})
            else:
                self.send_json({"error": "Not found"}, 404)
        except (ValueError, OSError) as e:
            self.send_json({"ok": False, "error": str(e)}, 400)


CFG = load_config()
LOCAL = Local(CFG)
HEALTH = health.Health(LOCAL)
CAN_REBOOT = can_reboot()
PCIE = PcieCaps(CFG["pcie_caps"])
AUTO = AutoRestart()


def local_state():
    """This PC's readings plus its problem list."""
    return {**LOCAL.snapshot(), **HEALTH.snapshot(), "can_reboot": CAN_REBOOT, "pcie_caps": PCIE.snapshot(),
            "auto_restart": AUTO.snapshot()}

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), Handler)
    server.daemon_threads = True
    print(f"AI Hub '{CFG['name']}' on http://0.0.0.0:{CFG['port']}/", flush=True)
    server.serve_forever()
