#!/usr/bin/env python3
"""Checklists for the AI Hub's Checklists tab: test runs and to-do lists where each step can be ticked,
marked Works / Problem / Skipped, and answered with a reply. Claude (or anyone) can write an answer
under each step, so a checklist doubles as a conversation about what to fix.

Files live in the docs folder, in a "checklists" folder of their own:

  <id>.json        the checklist itself, written by hand or by Claude (the page never changes it):
      {"title", "about", "made": "YYYY-MM-DD", "eyebrow", "intro" (Markdown), "footer" (Markdown),
       "places": {"<key>": {"label", "short" (on each step), "tone": "amber|green|blue|red", "text",
                            "links": [["label", "url"], ...]}},
       "sections": [{"id", "title", "note",
                     "items": [{"id", "title", "where": "<places key>", "dev", "min" (minutes),
                                "extra" (true: optional, not counted), "free" (true: a free-text box only),
                                "do": ["Markdown", ...], "expect": ["Markdown", ...], "sample": "text to copy"}]}]}
  <id>.state.json  what people ticked and wrote, per step:
      {"items": {"<item id>": {"done", "result": "pass|fail|skip|", "reply", "at", "by",
                                "answer", "answer_at", "answer_by"}}}

Command line (on the PC that has the docs folder):
  python3 checklists.py                                  list the checklists
  python3 checklists.py show <id> [--all]                steps that need a look (problems, replies), or all of them
  python3 checklists.py answer <id> <item> "text" [--by NAME]   write the answer shown under a step ("" removes it)
"""
import fcntl
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

SUBDIR = "checklists"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
RESULTS = ("pass", "fail", "skip", "")
MAX_TEXT = 10000
MAX_LIST = 1024 * 1024


def _dir(docs_dir):
    if not docs_dir:
        raise ValueError("This hub has no docs folder; use the server's hub")
    return os.path.join(docs_dir, SUBDIR)


def _check_id(cid):
    cid = str(cid or "")
    if not ID_RE.fullmatch(cid) or cid.endswith(".state"):
        raise ValueError("No such checklist")
    return cid


@contextmanager
def _locked(folder):
    """One writer at a time across the hub's threads and the command line (flock on a lock file)."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, ".lock"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _write_json(path, obj):
    tmp = "%s.%d.%d.tmp" % (os.path.join(os.path.dirname(path), "." + os.path.basename(path)),
                            os.getpid(), threading.get_ident())
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_list(docs_dir, cid):
    """The checklist itself, plus its file time (so a page can tell when it was changed)."""
    path = os.path.join(_dir(docs_dir), _check_id(cid) + ".json")
    try:
        if os.path.getsize(path) > MAX_LIST:
            raise ValueError("That checklist file is too big")
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError("No such checklist")
    except ValueError as e:
        raise ValueError("The checklist file %s.json can't be read (%s); fix it by hand" % (cid, e))
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ValueError("The checklist file %s.json has no sections" % cid)
    return data, os.path.getmtime(path)


def read_state(docs_dir, cid):
    path = os.path.join(_dir(docs_dir), _check_id(cid) + ".state.json")
    try:
        with open(path) as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"items": {}}
    except ValueError:
        # A damaged file is never treated as empty: that would wipe every tick and reply on the next save.
        raise ValueError("The saved answers for %s can't be read; fix %s by hand first" % (cid, os.path.basename(path)))
    if not isinstance(state, dict) or not isinstance(state.get("items", {}), dict):
        raise ValueError("The saved answers for %s aren't in the right shape; fix them by hand first" % cid)
    state.setdefault("items", {})
    return state


def items(data):
    """Every step in order, with its section id."""
    return [dict(it, sec=s.get("id")) for s in data.get("sections", []) for it in s.get("items", [])
            if isinstance(it, dict) and it.get("id")]


def counts(data, state):
    st = state.get("items", {})
    core = [i for i in items(data) if not i.get("extra") and not i.get("free")]
    every = items(data)
    return {"total": len(core),
            "done": sum(1 for i in core if st.get(i["id"], {}).get("done")),
            "problems": sum(1 for i in every if st.get(i["id"], {}).get("result") == "fail"),
            "replies": sum(1 for i in every if str(st.get(i["id"], {}).get("reply") or "").strip()),
            "answers": sum(1 for i in every if st.get(i["id"], {}).get("answer")),
            "minutes_left": sum(float(i.get("min") or 0) for i in core if not st.get(i["id"], {}).get("done"))}


def list_all(docs_dir):
    folder = _dir(docs_dir)
    out = []
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        names = []
    for name in names:
        cid = name[:-5] if name.endswith(".json") else ""
        if not cid or name.startswith(".") or cid.endswith(".state") or not ID_RE.fullmatch(cid):
            continue
        try:
            data, mtime = load_list(docs_dir, cid)
            entry = {"id": cid, "title": data.get("title") or cid, "about": data.get("about", ""),
                     "made": data.get("made", ""), "modified": mtime}
            try:
                entry["counts"] = counts(data, read_state(docs_dir, cid))
            except ValueError as e:
                entry["error"] = str(e)
        except ValueError as e:
            entry = {"id": cid, "title": cid, "about": "", "made": "", "modified": 0, "error": str(e)}
        out.append(entry)
    out.sort(key=lambda e: (e.get("made") or "", e.get("modified") or 0), reverse=True)
    return {"checklists": out}


def get(docs_dir, cid):
    data, mtime = load_list(docs_dir, cid)
    state = read_state(docs_dir, cid)
    return {"id": cid, "list": data, "modified": mtime, "state": state, "counts": counts(data, state)}


def _item_ids(data):
    return {i["id"] for i in items(data)}


def save(docs_dir, req, by):
    """A tick, a result or a reply from the page: merges only the fields sent."""
    cid = _check_id(req.get("list"))
    data, _ = load_list(docs_dir, cid)
    item = str(req.get("item") or "")
    if item not in _item_ids(data):
        raise ValueError("That step isn't in the checklist any more; reload the page")
    change = {}
    if "done" in req:
        change["done"] = bool(req["done"])
    if "result" in req:
        if req["result"] not in RESULTS:
            raise ValueError("A result is Works, Problem or Skipped")
        change["result"] = req["result"]
    if "reply" in req:
        reply = str(req["reply"] or "")
        if len(reply) > MAX_TEXT:
            raise ValueError("Replies are limited to 10,000 characters")
        change["reply"] = reply
    if not change:
        raise ValueError("Nothing to save")
    folder = _dir(docs_dir)
    with _locked(folder):
        state = read_state(docs_dir, cid)
        entry = state["items"].setdefault(item, {})
        entry.update(change)
        entry["at"] = round(time.time())
        entry["by"] = by
        _write_json(os.path.join(folder, cid + ".state.json"), state)
    return entry


def answer(docs_dir, cid, item, text, by="Claude"):
    """The answer shown under a step (Claude's reply to what was written there). Empty text removes it."""
    cid = _check_id(cid)
    data, _ = load_list(docs_dir, cid)
    if item not in _item_ids(data):
        raise ValueError("No step %r in %s" % (item, cid))
    text = str(text or "").strip()
    if len(text) > MAX_TEXT:
        raise ValueError("Answers are limited to 10,000 characters")
    folder = _dir(docs_dir)
    with _locked(folder):
        state = read_state(docs_dir, cid)
        entry = state["items"].setdefault(item, {})
        if text:
            entry.update(answer=text, answer_at=round(time.time()), answer_by=by)
        else:
            for k in ("answer", "answer_at", "answer_by"):
                entry.pop(k, None)
        _write_json(os.path.join(folder, cid + ".state.json"), state)
    return entry


# ---------------------------------------------------------------- command line
def _docs_dir():
    try:
        with open(os.path.expanduser("~/.config/aihub/config.json")) as f:
            d = json.load(f).get("docs_dir")
    except (OSError, ValueError):
        d = None
    return os.path.expanduser(d) if d else None


def _when(t):
    return time.strftime("%d %b %H:%M", time.localtime(t)) if t else ""


def _main(argv):
    docs_dir = _docs_dir()
    if not docs_dir:
        print("No docs_dir in ~/.config/aihub/config.json: run this on the hub that has the docs folder.")
        return 1
    if not argv:
        for e in list_all(docs_dir)["checklists"]:
            c = e.get("counts") or {}
            print("%-34s %s · %s/%s done · %s problems · %s replies · %s answered%s" % (
                e["id"], e["title"], c.get("done", "?"), c.get("total", "?"), c.get("problems", "?"),
                c.get("replies", "?"), c.get("answers", "?"), ("  ⚠ " + e["error"]) if e.get("error") else ""))
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "show" and rest:
        g = get(docs_dir, rest[0])
        st, show_all = g["state"]["items"], "--all" in rest
        c = g["counts"]
        print("%s · %s/%s done · %s problems · %s replies · %s answered" % (
            g["list"].get("title", rest[0]), c["done"], c["total"], c["problems"], c["replies"], c["answers"]))
        for it in items(g["list"]):
            s = st.get(it["id"], {})
            reply = str(s.get("reply") or "").strip()
            needs = s.get("result") == "fail" or (reply and (s.get("answer_at") or 0) < (s.get("at") or 0))
            if not show_all and not needs:
                continue
            res = {"pass": "WORKS", "fail": "PROBLEM", "skip": "skipped"}.get(s.get("result"), "done" if s.get("done") else "-")
            print("\n[%s] %-4s %s  —  %s  %s" % ("x" if s.get("done") else " ", it["id"], it.get("title", ""), res,
                                                ("(%s, %s)" % (s.get("by") or "?", _when(s.get("at")))) if s.get("at") else ""))
            if reply:
                print("      reply:  " + reply.replace("\n", "\n              "))
            if s.get("answer"):
                print("      answer: " + str(s["answer"]).replace("\n", "\n              ") + "  (%s)" % _when(s.get("answer_at")))
        return 0
    if cmd == "answer" and len(rest) >= 3:
        by = "Claude"
        if "--by" in rest:
            i = rest.index("--by")
            by, rest = rest[i + 1], rest[:i] + rest[i + 2:]
        answer(docs_dir, rest[0], rest[1], rest[2], by)
        print("Answer saved under %s in %s" % (rest[1], rest[0]))
        return 0
    print(__doc__.split("Command line", 1)[1])
    return 2


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except ValueError as e:
        print("⚠ " + str(e))
        sys.exit(1)
