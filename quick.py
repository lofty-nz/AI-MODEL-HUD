"""Quick button: a round button any page can load from the hub with one line,

    <script src="https://<hub>/quick.js" defer></script>

that pops up the 10 programs you use most (the Home page's tiles), on a PC or a phone.

- The button sits in a closed shadow root on the page, and the list is the hub's own page (/quick) in a frame,
  so the page and the button can't restyle, read or break each other. The page only gets the button; the list,
  the login and the counting stay on the hub. If the hub can't be reached, the page carries on as normal.
- The frame has to come from a hub on the *same site* as the page (same host, or the same domain), or the browser
  won't send the hub's login with it. Config "quick_hubs" says which hub that is for each address; this PC's own
  addresses are added by themselves (see aihub.py quick_rules).
- "Used" = opened from the pop-up or the Home page, or a page that has the button being opened (quick.js tells
  the hub once per page load). A program counts at most once every COUNT_GAP, and old use fades away
  (HALF_LIFE), so the list follows what you use now. Programs nobody has used yet fill the empty places:
  pinned ones first, then the groups at the top of Home, then the rest in the list's order.
- The counts are kept in <docs_dir>/quick-usage.json on the hub with the docs (the server); the other hub passes
  the calls on, as it does for the projects list.
"""
import json
import os
import threading
import time
import urllib.parse

import projects

FILE = "quick-usage.json"
TOP = 10
HALF_LIFE = 21 * 86400       # a use counts half as much three weeks later
COUNT_GAP = 30 * 60          # opening the same program again within half an hour isn't another use
MAX_ITEMS = 500
LOCK = threading.Lock()


def _file(docs_dir):
    return os.path.join(docs_dir, FILE)


def _load(docs_dir):
    """The saved counts. A damaged file is put aside (renamed) and counting starts again: it's only a tally."""
    path = _file(docs_dir)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
        raise ValueError("not a usage file")
    except FileNotFoundError:
        return {"items": {}}
    except (OSError, ValueError):
        try:
            os.replace(path, f"{path}.damaged-{int(time.time())}")
        except OSError:
            pass
        return {"items": {}}


def _write(docs_dir, data):
    path = _file(docs_dir)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def score_now(item, now=None):
    """A program's use score today: each use adds 1, and the total halves every HALF_LIFE."""
    now = time.time() if now is None else now
    try:
        return float(item.get("score", 0)) * 0.5 ** (max(0.0, now - float(item.get("at", now))) / HALF_LIFE)
    except (TypeError, ValueError):
        return 0.0


def _port(u):
    try:
        return u.port or {"http": 80, "https": 443}.get(u.scheme)
    except ValueError:
        return None


def match(items, url):
    """The project whose web address a page at `url` belongs to, or None. Same host and port (http or https
    alike), the longest matching path wins, then a place without ?query, then the earlier place and project."""
    try:
        u = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return None
    host, port = (u.hostname or "").lower(), _port(u)
    if not host:
        return None
    path = u.path or "/"
    best = None
    for i, p in enumerate(items):
        if p.get("hidden") or not p.get("id"):
            continue
        for j, pl in enumerate(p.get("places") or []):
            if pl.get("kind") != "web" or not pl.get("url"):
                continue
            try:
                v = urllib.parse.urlsplit(pl["url"])
            except ValueError:
                continue
            if (v.hostname or "").lower() != host or _port(v) != port:
                continue
            base = (v.path or "/").rstrip("/")
            if base and path != base and not path.startswith(base + "/"):
                continue
            rank = (len(base), not (v.query or v.fragment), -j, -i)
            if best is None or rank > best[0]:
                best = (rank, p["id"])
    return best[1] if best else None


def openable(p):
    """Whether a tile has somewhere to open (a web page, a served folder or a program), not only folders."""
    return any(pl.get("kind") in ("web", "files", "run") for pl in p.get("places") or [])


def record(docs_dir, pid=None, url=None, how="open"):
    """Count one use of a project, given by id (a click) or by the address of a page that has the button
    (a visit). Returns {"id", "counted"}; id is None when the address isn't one of the projects."""
    items = projects.load(docs_dir)["projects"]
    if pid is None:
        pid = match(items, url)
        if pid is None:
            return {"id": None, "counted": False}
    elif not any(p.get("id") == pid for p in items):
        raise ValueError("That program isn't on the list")
    how = "visit" if how == "visit" else "open"
    now = time.time()
    with LOCK:
        data = _load(docs_dir)
        it = data["items"].get(pid)
        it = dict(it) if isinstance(it, dict) else {}
        counted = now - float(it.get("counted_at") or 0) >= COUNT_GAP
        if counted:
            it["score"] = round(score_now(it, now) + 1, 4)
            it["at"] = it["counted_at"] = int(now)
            it[how + "s"] = int(it.get(how + "s") or 0) + 1
        it["last"] = int(now)
        data["items"][pid] = it
        if len(data["items"]) > MAX_ITEMS:   # projects that left the list long ago go first
            keep = sorted(data["items"].items(), key=lambda kv: -float(kv[1].get("last") or 0))[:MAX_ITEMS]
            data["items"] = dict(keep)
        _write(docs_dir, data)
    return {"id": pid, "counted": counted}


def top(docs_dir, n=TOP, here=None):
    """The pop-up's list: the n programs used most, then (while there's room) pinned ones, the Home page's
    top groups and the rest. Each entry is the project as the Home page has it, plus "uses" and "why"."""
    data = projects.load(docs_dir)
    items = [p for p in data["projects"] if p.get("id") and not p.get("hidden") and openable(p)]
    usage = _load(docs_dir)["items"]
    now = time.time()
    scored = sorted(((score_now(usage.get(p["id"]) or {}, now), i, p) for i, p in enumerate(items)),
                    key=lambda x: (-x[0], x[1]))
    out, seen = [], set()

    def add(p, why, score=0.0):
        if p["id"] in seen or len(out) >= n:
            return
        seen.add(p["id"])
        u = usage.get(p["id"]) or {}
        out.append({**p, "why": why, "score": round(score, 2),
                    "uses": int(u.get("opens") or 0) + int(u.get("visits") or 0), "last_used": u.get("last")})

    for score, _, p in scored:
        if score >= 0.05:
            add(p, "used", score)
    for p in items:
        if p.get("pinned"):
            add(p, "pinned")
    top_groups = [g for g in data.get("top") or [] if isinstance(g, str)]
    for g in top_groups:
        for p in items:
            if p.get("group") == g:
                add(p, "top")
    for p in items:
        add(p, "more")
    return {"items": out, "here": match(data["projects"], here) if here else None,
            "used": sum(1 for x in out if x["why"] == "used")}
