"""Card keeper: one referee for a graphics card that only holds one big AI model at a time.

Strata, Jev (llama-pd), ComfyUI (FLUX), ACE-Step and the bigger Ollama models each fill most of the
main PC's 16 GB card. Anything that wants one of them asks here first (POST /api/local/card):

    {"want": "ollama:qwen3.5:9b-q8_0", "who": "Open WebUI", "evict": true}

and gets one of these back straight away (callers ask again every few seconds):

    ready    it's loaded: go ahead
    working  making room / loading it now; "msg" says what's happening
    busy     the card is busy (a chat being answered, a picture being drawn, or something else such as a
             game working the card); nothing is stopped. Wait and ask again, or use something else.
    taken    another model holds the card and evict was false (TTR uses this: never bump Strata)
    failed   it tried and couldn't; "msg" says why

The rule (the user's, 15 Nov 2026): if the card isn't busy, whatever is on it makes room for what's asked.
Nothing is stopped while it's busy. Nothing is started again by itself afterwards: the card keeps the
last thing used until something else asks.

Turned on by "card" in ~/.config/aihub/config.json (only the main PC has it); see DEFAULTS.
"""
import collections
import fnmatch
import threading
import time
import urllib.error

import aimon_core as core

DEFAULTS = {
    "users": ["docker:strata", "docker:llama-pd", "docker:comfyui", "docker:acestep", "docker:chatterbox",
              "ollama:*"],                                         # rows that need most of the card
    "ignore": ["ollama:bge-m3:latest", "ollama:all-minilm:latest"],  # small ones that fit next to anything
    "names": {"docker:strata": "Strata", "docker:llama-pd": "Jev", "docker:comfyui": "ComfyUI (FLUX pictures)",
              "docker:acestep": "ACE-Step (music)", "docker:chatterbox": "Chatterbox (voice)",
              "ollama:qwen3.5:9b-q8_0": "Qwen3.5 vision", "ollama:llama3.2-vision:11b": "Llama 3.2 Vision",
              "ollama:qwen3:14b": "qwen3:14b"},
    "busy_util": 30,        # % card use, averaged over the last 6 s, that counts as busy
    "lease_s": 15,          # a model just handed out ("ready") counts as busy this long, so nobody
                            # grabs the card between the ask and the answer starting
    "free_mb": 2000,        # card memory still in use after making room that's fine (the desktop uses ~600 MB)
    "jev_slots": "http://127.0.0.1:8097/slots",
    "comfy_queue": "http://127.0.0.1:8188/queue",
}
FAILED_SHOW_S = 30   # a failed claim is reported to the next ask for the same thing, for this long


class Keeper:
    def __init__(self, local, cfg):
        self.local = local
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.lock = threading.Lock()
        self.job = None       # {"want", "who", "msg", "started"} while making room / loading
        self.last = None      # {"want", "state", "msg", "at"} of the last job
        self.handed = {}      # key -> (time it was last handed out as ready, who)
        self.util = collections.deque(maxlen=3)   # card % from the last 3 polls (2 s apart)
        self.log = collections.deque(maxlen=30)
        threading.Thread(target=self._sample, daemon=True).start()

    # ---- what's on the card ---------------------------------------------------------------------

    def is_user(self, key):
        return (any(fnmatch.fnmatchcase(key, p) for p in self.cfg["users"])
                and not any(fnmatch.fnmatchcase(key, p) for p in self.cfg["ignore"]))

    def name(self, key, rows=None):
        if key in self.cfg["names"]:
            return self.cfg["names"][key]
        row = (rows or {}).get(key)
        return row["name"] if row else key.split(":", 1)[-1]

    def _sample(self):
        while True:
            try:
                gpus = self.local.snapshot().get("gpus") or []
                if gpus and gpus[0].get("util") is not None:
                    self.util.append(float(gpus[0]["util"]))
            except Exception:
                pass
            time.sleep(2)

    def _rows(self):
        return {r["key"]: r for r in self.local.snapshot()["rows"]}

    def holders(self, rows, but=None):
        return [k for k, r in rows.items() if k != but and self.is_user(k) and r["state"] in ("on", "busy")]

    def busy(self, rows, holders):
        """Why the card is busy right now, or None. Nothing gets stopped while it is."""
        strata = rows.get("docker:strata", {}).get("strata")
        if strata and (strata["busy"] or strata["queued"]):
            return "Strata is answering a chat" + (f" ({strata['queued']} more waiting)" if strata["queued"] else "")
        if rows.get("docker:llama-pd", {}).get("state") == "on":
            try:
                slots = core.http_json(self.cfg["jev_slots"], timeout=2) or []
                if any(s.get("is_processing") for s in slots if isinstance(s, dict)):
                    return "Jev is answering a chat"
            except (OSError, ValueError):
                pass
        if rows.get("docker:comfyui", {}).get("state") == "on":
            try:
                q = core.http_json(self.cfg["comfy_queue"], timeout=2) or {}
                if q.get("queue_running") or q.get("queue_pending"):
                    return "ComfyUI is drawing a picture"
            except (OSError, ValueError):
                pass
        for k, r in rows.items():
            if self.is_user(k) and r.get("busy"):
                return f"AI Hub is still {r['sub'].rstrip('…').lower()} {self.name(k, rows)}"
            if k in holders and r["state"] == "busy":   # container up but still loading its model (health: starting)
                return f"{self.name(k, rows)} is still starting up"
        for k in holders:
            at, by = self.handed.get(k, (0, ""))
            if time.time() - at < self.cfg["lease_s"]:
                return f"{self.name(k, rows)} was just handed to {by}"
        if len(self.util) == self.util.maxlen:
            avg = sum(self.util) / len(self.util)
            if avg >= self.cfg["busy_util"]:
                if holders:
                    return f"{' and '.join(self.name(k, rows) for k in holders)} is working (card {avg:.0f}% busy)"
                return f"something else is using the main PC's graphics card ({avg:.0f}% busy), maybe a game"
        return None

    # ---- asking for the card ---------------------------------------------------------------------

    def claim(self, want, who="?", evict=True):
        with self.lock:
            rows = self._rows()
            row = rows.get(want)
            if row is None and want.startswith("ollama:") and rows.get("docker:ollama", {}).get("state") != "on":
                return self._say("failed", "Ollama isn't running on the main PC", want, who)
            if row is None:
                return self._say("failed", f"There's no {want} on the main PC", want, who)
            if self.job:
                if self.job["want"] == want:
                    return {"state": "working", "msg": self.job["msg"]}
                return {"state": "busy", "msg": f"the main PC is switching to {self.name(self.job['want'], rows)} "
                                                f"for {self.job['who']}"}
            last = self.last
            if last and last["want"] == want and last["state"] == "failed" and time.time() - last["at"] < FAILED_SHOW_S:
                self.last = None   # the next ask tries again
                return {"state": "failed", "msg": last["msg"]}
            holders = self.holders(rows, but=want)
            if row["state"] in ("on", "busy") and not row.get("busy"):
                # Running ("busy" here = the container is still starting up by itself). Something else may
                # share the card (started by hand?); that's reported, not stopped.
                out = ({"state": "ready", "msg": f"{self.name(want, rows)} is ready"} if row["state"] == "on" else
                       {"state": "working", "msg": f"{self.name(want, rows)} is starting up…"})
                if row["state"] == "on":
                    self.handed[want] = (time.time(), who)
                if holders:
                    out["sharing"] = [self.name(k, rows) for k in holders]
                return out
            why = self.busy(rows, holders)
            if why:
                return self._say("busy", why, want, who, holder=[self.name(k, rows) for k in holders])
            if holders and not evict:
                names = " and ".join(self.name(k, rows) for k in holders)
                return self._say("taken", f"{names} is using the main PC's graphics card", want, who,
                                 holder=[self.name(k, rows) for k in holders])
            self.job = {"want": want, "who": who, "started": time.time(),
                        "msg": (f"Making room for {self.name(want, rows)}…" if holders else
                                f"Starting {self.name(want, rows)}…")}
            self._note(f"{who}: {self.name(want, rows)} - stopping {[self.name(k, rows) for k in holders] or 'nothing'}")
            threading.Thread(target=self._switch, args=(want, holders), daemon=True).start()
            return {"state": "working", "msg": self.job["msg"]}

    def status(self):
        rows = self._rows()
        holders = self.holders(rows)
        return {"holders": [{"key": k, "name": self.name(k, rows)} for k in holders],
                "busy": self.busy(rows, holders), "job": self.job, "last": self.last,
                "util": list(self.util), "log": list(self.log)}

    def _say(self, state, msg, want, who, **extra):
        if state != "busy":   # busy is asked every few seconds; only note the rest
            self._note(f"{who}: {want} -> {state}: {msg}")
        return {"state": state, "msg": msg, **extra}

    def _note(self, text):
        line = time.strftime("%H:%M:%S ") + text
        self.log.append(line)
        print("card:", line, flush=True)

    # ---- making room ---------------------------------------------------------------------------

    def _switch(self, want, holders):
        rows = self._rows()
        try:
            for k in holders:
                self._set_msg(f"Stopping {self.name(k, rows)} to make room for {self.name(want, rows)}…")
                self.local.pending[k] = "stopping"
                try:
                    core.stop_row(rows[k])
                finally:
                    self.local.pending.pop(k, None)
                self._wait(lambda r: r.get(k, {}).get("state", "off") == "off", 90, f"{self.name(k, rows)} didn't stop")
            self._set_msg(f"Waiting for the card's memory to free up…")
            self._wait_free_memory()
            self._set_msg(f"Starting {self.name(want, rows)}…" if want.startswith("docker:")
                          else f"Loading {self.name(want, rows)} onto the card…")
            self.local.pending[want] = "starting"
            try:
                core.start_row(self._rows().get(want) or rows[want])
            finally:
                self.local.pending.pop(want, None)
            # running is enough: callers wait for the model itself (Strata takes ~50 s to load after that)
            self._wait(lambda r: r.get(want, {}).get("state") in ("on", "busy"), 60, f"{self.name(want, rows)} didn't start")
            self._done(want, "ready", f"{self.name(want, rows)} is ready")
        except (OSError, ValueError, RuntimeError, KeyError, urllib.error.URLError) as e:
            self._done(want, "failed", f"Couldn't switch to {self.name(want, rows)}: {e}")
        except Exception as e:   # never leave the job stuck
            self._done(want, "failed", f"Couldn't switch to {self.name(want, rows)}: {type(e).__name__}: {e}")

    def _set_msg(self, msg):
        if self.job:
            self.job["msg"] = msg

    def _done(self, want, state, msg):
        with self.lock:
            self.last = {"want": want, "state": state, "msg": msg, "at": time.time()}
            if state == "ready" and self.job:
                self.handed[want] = (time.time(), self.job["who"])
            self.job = None
        self._note(f"{want} -> {state}: {msg}")
        self.local.wake.set()

    def _wait(self, ok, secs, fail):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.local.wake.set()   # poll now rather than in 2 s
            time.sleep(1.5)
            if ok(self._rows()):
                return
        raise RuntimeError(f"{fail} within {secs} s")

    def _wait_free_memory(self):
        """After stopping things, the card's memory takes a moment to come back. If it doesn't, someone
        else (a game?) holds it, and loading next to it would spill onto the CPU, so give up instead."""
        end = time.monotonic() + 30
        used = None
        while time.monotonic() < end:
            gpus = self.local.snapshot().get("gpus") or []
            used = gpus[0].get("mem_used") if gpus else None
            if used is None or used <= self.cfg["free_mb"]:
                return
            time.sleep(2)
        users = ", ".join(u["name"] for u in (gpus[0].get("users") or [])) if gpus else ""
        raise RuntimeError(f"{used / 1024:.1f} GB of the card is still in use" + (f" (by {users})" if users else ""))
