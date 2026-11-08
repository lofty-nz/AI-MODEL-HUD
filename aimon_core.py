"""What aimon and AI Hub both need: reading Docker containers, Ollama models and memory, and
starting and stopping them. No GTK in here, so a headless service can import it."""
import datetime
import fcntl
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
COMFY = "http://127.0.0.1:8188"
MIB, GIB = 1024 ** 2, 1024 ** 3
COMFY_LOADED = GIB       # ComfyUI's CUDA context alone takes a few hundred MB; above 1 GB it has a model loaded

# What each row is for ("quick chat", "text to image"), by row key. One file per PC, shared by the
# panel and the AI Hub, so a ✎ change in either shows in both.
LABELS = os.path.expanduser("~/.config/aimon/labels.json")
LABEL_MAX = 60
LABEL_IDEAS = ["quick chat", "complex chat", "text to image", "image to text", "reads documents",
               "document search", "speech to text", "chat website", "runs the chat models", "not AI"]


def read_labels():
    try:
        with open(LABELS) as f:
            labels = json.load(f)
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in labels.items() if v} if isinstance(labels, dict) else {}


def set_label(key, text):
    """Saves (or with empty text, removes) a row's label. Returns the label as saved."""
    key = str(key or "").strip()
    if not key:
        raise ValueError("Which row?")
    text = " ".join(str(text or "").split())[:LABEL_MAX]
    os.makedirs(os.path.dirname(LABELS), exist_ok=True)
    with open(LABELS + ".lock", "w") as lock:   # the panel and the hub may both write
        fcntl.flock(lock, fcntl.LOCK_EX)
        labels = read_labels()
        if text:
            labels[key] = text
        else:
            labels.pop(key, None)
        tmp = LABELS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(sorted(labels.items())), f, indent=1, ensure_ascii=False)
        os.replace(tmp, LABELS)
    return text

def http_json(url, body=None, timeout=2, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read()
    return json.loads(text) if text.strip() else None


STRATA_KEY = os.path.expanduser("~/.config/strata/api_key")


def strata_status(port):
    """Strata answers one question at a time. Its /status says whether it's busy and how many wait:
    {"busy", "queued", "phase", ...}, or None when it can't be read. Apps check the row before asking."""
    try:
        with open(STRATA_KEY) as f:
            key = f.read().strip()
        st = http_json(f"http://127.0.0.1:{port}/status", timeout=1, headers={"Authorization": f"Bearer {key}"})
    except (OSError, ValueError):
        return None
    return {"busy": bool(st.get("busy")), "queued": int(st.get("queued") or 0), "phase": str(st.get("phase") or "")} \
        if isinstance(st, dict) else None


def run(cmd, timeout):
    """Output of a command; raises RuntimeError with its last line of error output if it fails."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"{cmd[0]} exited with {r.returncode}")
    return r.stdout


def read_containers():
    """Every Docker container, running or not."""
    out = run(["docker", "ps", "-a", "--no-trunc", "--format",
               "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}"], timeout=5)
    containers = []
    for line in out.splitlines():
        cid, name, image, state, status = (line.split("\t") + [""] * 5)[:5]
        containers.append({"id": cid, "name": name, "image": image, "state": state, "status": status,
                           "starting": "health: starting" in status, "port": None})
    if containers:
        # docker ps lists ports only while a container runs; its port bindings are there either way.
        out = run(["docker", "inspect", "-f", "{{.Id}} {{json .HostConfig.PortBindings}}"]
                  + [c["id"] for c in containers], timeout=5)
        bindings = dict(line.split(" ", 1) for line in out.splitlines())
        for c in containers:
            bound = json.loads(bindings.get(c["id"], "null")) or {}
            ports = [b for bs in bound.values() for b in bs or [] if b.get("HostPort")]
            c["port"] = ports[0]["HostPort"] if ports else None
            c["bind"] = ports[0].get("HostIp") or "0.0.0.0" if ports else None
    return sorted(containers, key=lambda c: c["name"])


def serves_web_page(port):
    """True if the port answers with an HTML page, False if with something else, None if it doesn't answer (yet)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return r.headers.get_content_type() == "text/html"
    except urllib.error.HTTPError as e:
        return e.headers.get_content_type() == "text/html"
    except OSError:
        return None


def container_ram(cid):
    """System memory a container's processes have allocated. File cache is left out: Ollama's model
    files stay cached after loading to the GPU, which would count the whole model again."""
    try:
        with open(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.stat") as f:
            stat = dict(line.split() for line in f)
        return int(stat["anon"]) + int(stat.get("shmem", 0))
    except (OSError, ValueError, KeyError):
        return None


def read_gpu():
    """(used, total) VRAM in bytes over all cards, or None without nvidia-smi, and the VRAM each container holds."""
    try:
        cards = run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"], 5)
        apps = run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"], 5)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None, {}
    used = total = 0
    for line in cards.strip().splitlines():
        u, t = line.split(",")
        used, total = used + float(u) * MIB, total + float(t) * MIB
    per = {}
    for line in apps.strip().splitlines():
        pid, mem = [x.strip() for x in line.split(",")]
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                m = re.search(r"docker-([0-9a-f]{64})\.scope", f.read())
            mem = float(mem) * MIB
        except (OSError, ValueError):   # the process ended, or "[N/A]"
            continue
        if m:
            per[m[1]] = per.get(m[1], 0) + mem
    return (used, total), per


def read_ram():
    """(used, total) system memory in bytes."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0]) * 1024
    return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]


def unloads_in(expires_at):
    """How long Ollama will keep a model, from its expires_at (nanosecond ISO time)."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?", expires_at or "")
    if not m:
        return "loaded"
    when = datetime.datetime.fromisoformat(m[1] + ("+00:00" if m[2] in (None, "Z") else m[2]))
    left = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if left > 365 * 86400:   # keep_alive -1, which START uses
        return "loaded · stays until stopped"
    return f"loaded · unloads in {max(1, round(left / 60))} min"


def disk_size(b):
    return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def ollama_base(host):
    """URL of the Ollama API a container publishes on this machine (falls back to the standard port)."""
    return f"http://127.0.0.1:{host['port']}" if host and host.get("port") else OLLAMA


def ollama_rows(chat, host):
    """One row per installed Ollama model, loaded or not, as seen by the Ollama container `host`.
    There can be several Ollama containers (one per graphics card); each gets its own rows. The one on
    the standard port keeps the plain "ollama:<model>" keys. `chat` is the Open WebUI container, if there
    is one, whose page the standard Ollama's models link to (that's the Ollama Open WebUI chats through)."""
    base = ollama_base(host)
    main = base == OLLAMA
    tags = http_json(f"{base}/api/tags")["models"]
    loaded = {m["name"]: m for m in http_json(f"{base}/api/ps")["models"]}
    rows = []
    for m in sorted(tags, key=lambda m: m["name"]):
        ps = loaded.get(m["name"])
        rows.append({"key": f"ollama:{m['name']}" if main else f"ollama@{host['name']}:{m['name']}",
                     "kind": "ollama", "model": m["name"], "base": base,
                     "name": m["name"].removesuffix(":latest"), "sub": unloads_in(ps.get("expires_at")) if ps
                     else f"{disk_size(m['size'])} on disk", "state": "on" if ps else "off",
                     "vram": ps and ps["size_vram"], "ram": ps and ps["size"] - ps["size_vram"],
                     "tip": f"Ollama model {m['name']}\n{m['details'].get('parameter_size', '')} "
                            f"{m['details'].get('quantization_level', '')}".strip(), "level": 1})
        if chat and main and "bert" not in m["details"].get("family", ""):   # embedding models can't chat
            rows[-1].update(url=f"http://127.0.0.1:{chat['port']}/?model={urllib.parse.quote(m['name'])}",
                            web_host=chat["name"], web_port=chat["port"])
    return rows


def read_rows(web):
    """Every row in the panel, top to bottom. Raises RuntimeError or OSError when Docker can't be reached.

    `web` maps container names to whether they serve a web page. Running containers not in it yet are
    checked and added; a stopped one that has never been checked gets a link, in case it does."""
    containers = read_containers()
    _, gpu_per = read_gpu()
    for c in containers:
        if c["state"] == "running" and c["port"] and c["name"] not in web:
            page = serves_web_page(c["port"])
            if page is not None:
                web[c["name"]] = page
    chat = next((c for c in containers if "open-webui" in c["image"] and c["port"]), None)
    rows = []
    for c in containers:
        on = c["state"] == "running"
        vram = gpu_per.get(c["id"])
        status = c["status"].replace(" (healthy)", "")
        rows.append({"key": f"docker:{c['name']}", "kind": "docker", "name": c["name"], "image": c["image"],
                     "id": c["id"], "bind": c.get("bind"),
                     "sub": f"{status} · :{c['port']}" if on and c["port"] else status,
                     "state": "busy" if c["starting"] or c["state"] == "restarting" else "on" if on else "off",
                     "vram": vram, "ram": container_ram(c["id"]) if on else None,
                     "tip": f"Container {c['name']}\n{c['image']}\n{c['status']}", "level": 0})
        if c["port"] and web.get(c["name"]) is not False:
            rows[-1].update(url=f"http://127.0.0.1:{c['port']}/", web_host=c["name"], web_port=c["port"])
        if not on:
            continue
        if "strata" in c["image"] and c["port"] and not c["starting"]:
            st = strata_status(c["port"])
            if st:
                rows[-1]["strata"] = st
                if st["busy"] or st["queued"]:
                    rows[-1]["sub"] = (f"busy answering ({st['phase'] or 'working'})"
                                       + (f" · {st['queued']} waiting" if st["queued"] else "") + f" · :{c['port']}")
        if "ollama" in c["image"]:
            try:
                rows += ollama_rows(chat, c)
            except (OSError, ValueError, KeyError, TypeError):
                pass   # still starting up
        elif "comfyui" in c["image"] and vram and vram > COMFY_LOADED:
            rows.append({"key": "comfy", "kind": "comfy", "name": "Image models", "sub": "loaded in VRAM",
                         "state": "on", "vram": vram, "ram": None, "level": 1,
                         "tip": "ComfyUI loads models when a workflow runs.\nUNLOAD frees them; the container keeps running."})
    labels = read_labels()
    for r in rows:
        r["label"] = labels.get(r["key"], "")
    return rows


def start_row(row):
    if row["kind"] == "docker":
        run(["docker", "start", row["name"]], timeout=120)
    elif row["kind"] == "ollama":
        # keep_alive -1 keeps it loaded until STOP. Embedding models can't generate, so they load through embed.
        try:
            http_json(f"{row['base']}/api/generate", {"model": row["model"], "keep_alive": -1}, timeout=600)
        except urllib.error.HTTPError:
            http_json(f"{row['base']}/api/embed", {"model": row["model"], "input": "load", "keep_alive": -1}, timeout=600)


def stop_row(row):
    if row["kind"] == "docker":
        run(["docker", "stop", row["name"]], timeout=120)
    elif row["kind"] == "ollama":
        http_json(f"{row['base']}/api/generate", {"model": row["model"], "keep_alive": 0}, timeout=60)
    else:
        http_json(f"{COMFY}/interrupt", {}, timeout=10)
        http_json(f"{COMFY}/free", {"unload_models": True, "free_memory": True}, timeout=60)
