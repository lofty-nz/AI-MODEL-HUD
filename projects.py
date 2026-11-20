"""Projects tab: the list of little projects, a files server that opens a project folder in a browser,
and starting a program (or a project folder in VS Code / Files) on this PC's screen.

- The list is <docs_dir>/projects.json on the hub with the docs (the server). The page may change a
  project's name, icon, what it is, group, web links, GitHub link, hidden and pinned flags. It can never set a
  command or a folder: those come from each PC's own ~/.config/aihub/config.json ("serve",
  "launchers", "folder_roots"), so a web page can only pick from what this PC already allows.
- Served folders are on their own port (files_port, default 8091), so a project's page never shares
  the hub's address (and so can't use the hub's buttons or settings). Web file types only, no hidden
  files, nothing outside the folder.
- Programs start through `systemd-run --user --scope`, so they keep running when the hub restarts.
- Fields set by hand in projects.json and kept when the page saves: "folders", "note", the non-web places,
  "logo" (a file in web/icons shown instead of the emoji), "container" ({"pc", "key"}: the Machines row that
  runs it, so the Home page can show Off and a Start button), "check" (the address the Checker asks instead
  of the first web link, e.g. the LAN address behind an https name), "check_every" (seconds), "check_has"
  (text the checked page must hold to count as up, e.g. a model name in Strata's /health) with "other_text"
  (what the tile says when the page answered without it) and "on_demand" (off is normal: grey, not red).
"""
import html
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILE = "projects.json"
LOCK = threading.Lock()
MAX_PROJECTS = 300
MAX_LINKS = 10
LOG = os.path.expanduser("~/.local/state/aihub/launch.log")

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/markdown", ".md")
WEB_TYPES = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
             ".webp", ".ico", ".wasm", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".ogg", ".wav", ".mp4", ".webm",
             ".glb", ".gltf", ".txt", ".md", ".csv", ".pdf", ".webmanifest"}


# ---------- the list (docs hub only) ----------

def _file(docs_dir):
    return os.path.join(docs_dir, FILE)


def load(docs_dir):
    """The saved list. A damaged file raises instead of reading as empty, so a save can't wipe it."""
    try:
        with open(_file(docs_dir)) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"groups": [], "projects": []}
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ValueError("projects.json is damaged; fix it by hand (nothing was changed)")
    data.setdefault("groups", [])
    return data


def _write(docs_dir, data):
    path = _file(docs_dir)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _text(v, limit, what):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    if len(v) > limit:
        raise ValueError(f"{what} is too long (up to {limit} characters)")
    return v


def _url(v, what="Link"):
    v = str(v or "").strip()
    u = urllib.parse.urlsplit(v)
    if u.scheme not in ("http", "https") or not u.netloc or len(v) > 500 or re.search(r"\s", v):
        raise ValueError(f"{what} must be a full web address starting with http:// or https:// ({v[:60] or 'empty'})")
    return v


def _slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def save(docs_dir, req):
    """{"project": {...}} adds or changes one project; {"delete": id} removes one.
    Only the page-safe fields are taken; a project's folders, served copies and programs are kept as they were."""
    with LOCK:
        data = load(docs_dir)
        items = data["projects"]
        if req.get("delete"):
            keep = [p for p in items if p.get("id") != req["delete"]]
            if len(keep) == len(items):
                raise ValueError("That project isn't in the list")
            data["projects"] = keep
            _write(docs_dir, data)
            return {"message": "Project removed", "id": req["delete"]}
        new = req.get("project")
        if not isinstance(new, dict):
            raise ValueError("Nothing to save")
        name = _text(new.get("name"), 60, "The name")
        if not name:
            raise ValueError("Give it a name")
        old = next((p for p in items if p.get("id") == new.get("id")), None) if new.get("id") else None
        if new.get("id") and not old:
            raise ValueError("That project isn't in the list any more (reload the page)")
        if not old and len(items) >= MAX_PROJECTS:
            raise ValueError("The list is full")
        links = new.get("links") or []
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise ValueError(f"Up to {MAX_LINKS} web links")
        web = []
        for l in links:
            url = _url(l.get("url"))
            web.append({"kind": "web", "label": _text(l.get("label"), 40, "A link's name") or urllib.parse.urlsplit(url).netloc,
                        "url": url})
        gh = str(new.get("github") or "").strip()
        if gh:
            gh = _url(gh, "The GitHub link")
        p = dict(old or {"id": _slug(name, {x.get("id") for x in items})})
        p.update({"name": name, "icon": _text(new.get("icon"), 8, "The icon"),
                  "about": _text(new.get("about"), 300, "What it is"), "group": _text(new.get("group"), 40, "The group"),
                  "github": gh, "hidden": bool(new.get("hidden")), "pinned": bool(new.get("pinned"))})
        # web links first (in the order given), then the places this page can't edit, in their old order
        p["places"] = web + [x for x in (old or {}).get("places", []) if x.get("kind") != "web"]
        if old:
            items[items.index(old)] = p
        else:
            items.append(p)
        if p["group"] and p["group"] not in data["groups"]:
            data["groups"].append(p["group"])
        _write(docs_dir, data)
        return {"message": "Saved" if old else "Project added", "id": p["id"]}



class Checker:
    """Asks each project's address whether anything answers, from the hub with the list, so the Home page can
    show a dot on every tile. Any HTTP reply counts as up (as with the hub's link checks), so an https name in
    front of a program that's off can look up: give such a project a "check" address (the program itself)."""
    POLL_S = 30

    def __init__(self, docs_dir, probe):
        self.docs_dir, self.probe = docs_dir, probe
        self.status = {}
        threading.Thread(target=self._run, daemon=True).start()

    @classmethod
    def target(cls, p):
        """(address to ask, seconds between asks) for one project, or (None, None)."""
        every = max(10.0, float(p.get("check_every") or cls.POLL_S))
        if "check" in p:   # "check": false = nothing to ask (its container's state is enough)
            return (p["check"], every) if p["check"] else (None, None)
        url = next((x.get("url") for x in p.get("places") or [] if x.get("kind") == "web" and x.get("url")), None)
        host = urllib.parse.urlsplit(url).hostname if url else None
        if not url or host in ("localhost", "::1") or (host or "").startswith("127."):
            return None, None   # a this-PC-only address means nothing when this hub asks it
        return url, every

    def _run(self):
        last = {}   # address -> (monotonic time asked, up, wall time)
        while True:
            try:
                items = load(self.docs_dir)["projects"]
            except (OSError, ValueError):
                items = []
            now, want, every = time.monotonic(), {}, {}
            for p in items:
                try:
                    url, secs = self.target(p)
                except (TypeError, ValueError):
                    continue
                if url and p.get("id"):
                    has = p.get("check_has") if isinstance(p.get("check_has"), str) and p.get("check_has") else None
                    want[p["id"]] = key = (url, has)
                    every[key] = min(every.get(key, 1e9), secs)
            due = [k for k, secs in every.items() if k not in last or now - last[k][0] >= secs - 1]
            if due:
                with ThreadPoolExecutor(8) as pool:
                    for k, res in zip(due, pool.map(self._ask, due)):
                        last[k] = (now, res, time.time())
            self.status = {pid: {**last[k][1], "at": int(last[k][2])} for pid, k in want.items() if k in last}
            time.sleep(self.POLL_S)

    def _ask(self, key):
        """{"up"} for a plain check. With "check_has" (text the page must hold, e.g. which model Strata's /health
        names), "up" means it does, and "answered" says whether the page answered at all."""
        url, has = key
        if not has:
            return {"up": bool(self.probe(url))}
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return {"up": has.lower() in r.read(65536).decode("utf-8", "replace").lower(), "answered": True}
        except urllib.error.HTTPError:
            return {"up": False, "answered": True}
        except Exception:
            return {"up": False, "answered": False}

    def snapshot(self):
        return dict(self.status)

# ---------- served folders: a second port, web files only ----------

class FilesHandler(BaseHTTPRequestHandler):
    server_version = "AIHubFiles/1"
    serve = {}      # key -> real folder path
    allow = []      # networks allowed in (same as the hub)

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype, head=False, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _fail(self, status, msg, head):
        self._send(status, f"<!doctype html><meta charset=utf-8><title>{status}</title><p>{html.escape(msg)}</p>".encode(),
                   "text/html; charset=utf-8", head)

    def _ok_ip(self):
        ip = ipaddress.ip_address(self.client_address[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.allow)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self._ok_ip():
            return self._fail(403, "Not allowed from this address", head)
        raw = urllib.parse.unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        parts = [x for x in raw.split("/") if x]
        if not parts:
            items = "".join(f'<li><a href="/{urllib.parse.quote(k)}/">{html.escape(k)}</a></li>' for k in sorted(self.serve))
            return self._send(200, f"<!doctype html><meta charset=utf-8><title>Projects</title><ul>{items}</ul>".encode(),
                              "text/html; charset=utf-8", head)
        root = self.serve.get(parts[0])
        if not root:
            return self._fail(404, "No such project here", head)
        if any(x.startswith(".") for x in parts[1:]):
            return self._fail(404, "Not found", head)
        path = os.path.realpath(os.path.join(root, *parts[1:]))
        if path != root and not path.startswith(root + os.sep):
            return self._fail(404, "Not found", head)
        if os.path.isdir(path):
            if not raw.endswith("/"):
                return self._send(301, b"", "text/plain", head, [("Location", urllib.parse.quote(raw) + "/")])
            index = os.path.join(path, "index.html")
            if os.path.isfile(index):
                path = index
            else:
                return self._listing(path, raw, head)
        ext = os.path.splitext(path)[1].lower()
        if ext not in WEB_TYPES or not os.path.isfile(path):
            return self._fail(404, "Not found", head)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._fail(404, "Not found", head)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/manifest+json"):
            ctype += "; charset=utf-8"
        self._send(200, body, ctype, head)

    def _listing(self, folder, raw, head):
        """A folder with no index.html (e.g. a lessons folder): its pages and subfolders."""
        rows = []
        for name in sorted(os.listdir(folder), key=str.lower):
            full = os.path.join(folder, name)
            if name.startswith("."):
                continue
            if os.path.isdir(full):
                rows.append(f'<li>📁 <a href="{urllib.parse.quote(name)}/">{html.escape(name)}/</a></li>')
            elif os.path.splitext(name)[1].lower() in WEB_TYPES:
                rows.append(f'<li><a href="{urllib.parse.quote(name)}">{html.escape(name)}</a></li>')
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>{html.escape(raw)}</title><style>body{{font:15px/1.6 system-ui,sans-serif;margin:24px;"
                f"background:#0f1116;color:#e6edf2}}a{{color:#73b3fa}}</style><h1 style='font-size:18px'>"
                f"{html.escape(raw)}</h1><ul>{''.join(rows) or '<li>Nothing to open here</li>'}</ul>")
        self._send(200, page.encode(), "text/html; charset=utf-8", head)


def start_files_server(port, serve, allow):
    """Serve each configured folder at http://<this PC>:<port>/<key>/ in a background thread."""
    folders = {}
    for key, folder in (serve or {}).items():
        real = os.path.realpath(os.path.expanduser(folder))
        if re.fullmatch(r"[\w.-]+", key) and not key.startswith(".") and os.path.isdir(real):
            folders[key] = real
    if not port or not folders:
        return None
    FilesHandler.serve, FilesHandler.allow = folders, allow
    srv = ThreadingHTTPServer(("0.0.0.0", port), FilesHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="project-files").start()
    return srv


# ---------- starting things on this PC's screen ----------

def _session_env():
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XAUTHORITY", os.path.expanduser("~/.Xauthority"))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return env


def _logged_in():
    """True when this user has a desktop session open on the PC's screen (not just the login screen)."""
    try:
        out = subprocess.run(["loginctl", "show-user", str(os.getuid()), "--property=Display", "--value"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return bool(out)
    except (OSError, subprocess.SubprocessError):
        return True   # can't tell; try anyway


def _start(argv, cwd, pc_name):
    if not _logged_in():
        raise ValueError(f"Nobody is logged in on {pc_name}'s screen, so it has nowhere to open. Log in there first.")
    if shutil.which("systemd-run"):
        # its own scope, so it isn't stopped when the hub restarts (a service's children are)
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--"] + argv
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "ab") as log:
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {argv}\n".encode())
        log.flush()
        start = log.tell()
        p = subprocess.Popen(argv, cwd=cwd or os.path.expanduser("~"), env=_session_env(), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    try:
        code = p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return   # still running: started
    if code:
        try:
            with open(LOG, "rb") as f:
                f.seek(start)
                tail = f.read()[-300:].decode(errors="replace").strip().splitlines()
        except OSError:
            tail = []
        raise ValueError(f"It stopped straight away ({tail[-1] if tail else f'exit code {code}'})")


def launch(cfg, req):
    """{"run": key} starts a program from this PC's "launchers"; {"folder": path, "app": "code"|"files"} opens a
    project folder in VS Code or the file manager. Returns the message for the page."""
    pc = cfg["name"]
    if req.get("run"):
        key = str(req["run"])
        item = (cfg.get("launchers") or {}).get(key)
        if not isinstance(item, dict):
            raise ValueError(f"{pc} has no program called {key}")
        name = item.get("name") or key
        if item.get("running"):
            found = subprocess.run(["pgrep", "-f", item["running"]], capture_output=True).returncode == 0
            if found:
                return f"{name} is already running on {pc}"
        if item.get("desktop"):
            argv = ["gio", "launch", os.path.expanduser(item["desktop"])]
        elif isinstance(item.get("exec"), list) and item["exec"]:
            argv = [os.path.expanduser(str(a)) for a in item["exec"]]
        else:
            raise ValueError(f"{name} has no command set on {pc}")
        _start(argv, os.path.expanduser(item.get("cwd") or "~"), pc)
        return f"Starting {name} on {pc}"
    folder = req.get("folder")
    app = req.get("app")
    if folder and app in ("code", "files"):
        real = os.path.realpath(os.path.expanduser(str(folder)))
        roots = [os.path.realpath(os.path.expanduser(r)) for r in cfg.get("folder_roots") or []]
        if not os.path.isdir(real):
            raise ValueError(f"That folder isn't on {pc}: {folder}")
        if not any(real == r or real.startswith(r + os.sep) for r in roots) or "/." in real:
            raise ValueError(f"{pc} only opens project folders ({', '.join(cfg.get('folder_roots') or []) or 'none set'})")
        if app == "code":
            if not shutil.which("code"):
                raise ValueError(f"VS Code isn't installed on {pc}")
            _start(["code", real], real, pc)
            return f"Opening {os.path.basename(real)} in VS Code on {pc}"
        _start(["xdg-open", real], real, pc)
        return f"Opening {os.path.basename(real)} in Files on {pc}"
    raise ValueError("Nothing to start")


_RUNNING = {"at": -1e9, "keys": []}
_RUNNING_LOCK = threading.Lock()


def running_launchers(cfg):
    """Keys of this PC's launchers whose "running" pattern matches a process (asked at most every 10 s)."""
    with _RUNNING_LOCK:
        if time.monotonic() - _RUNNING["at"] < 10:
            return _RUNNING["keys"]
        keys = []
        for key, item in (cfg.get("launchers") or {}).items():
            if isinstance(item, dict) and item.get("running"):
                try:
                    if subprocess.run(["pgrep", "-f", item["running"]], capture_output=True, timeout=2).returncode == 0:
                        keys.append(key)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        _RUNNING.update(at=time.monotonic(), keys=keys)
        return keys


def local_info(cfg, files_server):
    """What this PC offers the Home page's projects (sent with the 2-second readings, so keep it small)."""
    url = cfg.get("files_url") or (f"http://{cfg.get('lan_ip') or '127.0.0.1'}:{cfg.get('files_port')}" if files_server else None)
    return {"files_url": url if files_server else None,
            "serve": sorted(FilesHandler.serve) if files_server else [],
            "launchers": {k: (v.get("name") or k) for k, v in (cfg.get("launchers") or {}).items() if isinstance(v, dict)},
            "running": running_launchers(cfg)}
