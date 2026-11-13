#!/usr/bin/env python3
"""AI Hub login.

People log in to the page with one hub password (the same on every PC). Programs (the other PC's hub,
TTR, Ask the App, scripts) send the machine key instead, in the X-Hub-Key header.

Files (all readable by you only):
  ~/.config/aihub/auth.json            the password, as a salted PBKDF2 hash (never the password itself)
  ~/.config/aihub/hub.key              the machine key; the same file on every PC
  ~/.local/state/aihub/sessions.json   browsers that are logged in (only a hash of each cookie is kept)
  ~/.local/state/aihub/auth.log        every login, wrong password and logout

Command line (run it on the main PC; it updates the server too, over ssh):
  python3 hub_auth.py set-password     asks for a new hub password, twice, and logs every browser out
  python3 hub_auth.py logout-all       logs every browser out (on this PC only)
  python3 hub_auth.py status
"""
import getpass
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie

CONF_DIR = os.path.expanduser("~/.config/aihub")
AUTH_FILE = os.path.join(CONF_DIR, "auth.json")
KEY_FILE = os.path.join(CONF_DIR, "hub.key")
STATE_DIR = os.path.expanduser("~/.local/state/aihub")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
LOG_FILE = os.path.join(STATE_DIR, "auth.log")

COOKIE = "aihub_session"
SESSION_DAYS = 30
ITERATIONS = 600_000
MIN_LENGTH = 8
FAIL_WINDOW = 15 * 60       # wrong passwords count for this long
FAIL_PER_ADDRESS = 8        # then that address waits
FAIL_TOTAL = 25             # then everyone waits (logins through Caddy all come from one address)
KEY_HEADER = "X-Hub-Key"


def _write_private(path, text):
    """Write a file only you can read, in one step (a crash never leaves half a file)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _hash(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()


def password_record(password):
    salt = secrets.token_hex(16)
    return {"salt": salt, "hash": _hash(password, salt, ITERATIONS), "iterations": ITERATIONS,
            "set_at": int(time.time())}


def read_key():
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def key_header():
    """{"X-Hub-Key": ...} for programs calling another hub, or {} if this PC has no key."""
    key = read_key()
    return {KEY_HEADER: key} if key else {}


def _token_id(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.fails = []            # [(time, address)]
        self.sessions = {}         # {sha256 of the cookie: {"until", "made", "from", "agent"}}
        self._sessions_mtime = None

    # ------------------------------------------------------------------ files
    def _load_sessions(self):
        """Re-read the sessions file when it changed (logout-all from the command line)."""
        try:
            mtime = os.stat(SESSIONS_FILE).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._sessions_mtime:
            data = _read_json(SESSIONS_FILE, {})
            self.sessions = data if isinstance(data, dict) else {}
            self._sessions_mtime = mtime

    def _save_sessions(self):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v.get("until", 0) > now}
        _write_private(SESSIONS_FILE, json.dumps(self.sessions, indent=1))
        self._sessions_mtime = os.stat(SESSIONS_FILE).st_mtime_ns

    def log(self, event, address="", agent=""):
        line = json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "from": address,
                           "agent": (agent or "")[:120]})
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ checks
    @staticmethod
    def has_password():
        rec = _read_json(AUTH_FILE, {})
        return bool(isinstance(rec, dict) and rec.get("hash") and rec.get("salt"))

    @staticmethod
    def check_key(given):
        key = read_key()
        return bool(key and given) and hmac.compare_digest(given.strip().encode(), key.encode())

    def check_cookie(self, cookie_header):
        token = self.token_from(cookie_header)
        if not token:
            return False
        with self.lock:
            self._load_sessions()
            s = self.sessions.get(_token_id(token))
            return bool(s and s.get("until", 0) > time.time())

    @staticmethod
    def token_from(cookie_header):
        if not cookie_header:
            return ""
        try:
            c = SimpleCookie()
            c.load(cookie_header)
        except Exception:   # a malformed cookie from some other site on the same address
            return ""
        return c[COOKIE].value if COOKIE in c else ""

    # ------------------------------------------------------------------ log in / out
    def login(self, password, address, agent):
        """The new cookie value, or ValueError with the message to show."""
        now = time.time()
        with self.lock:
            self.fails = [(t, a) for t, a in self.fails if now - t < FAIL_WINDOW]
            mine = sum(1 for _, a in self.fails if a == address)
            if mine >= FAIL_PER_ADDRESS or len(self.fails) >= FAIL_TOTAL:
                wait = int(FAIL_WINDOW - (now - min(t for t, _ in self.fails))) // 60 + 1
                self.log("locked", address, agent)
                raise ValueError(f"Too many wrong passwords. Try again in {wait} minute{'s' if wait != 1 else ''}.")
        rec = _read_json(AUTH_FILE, {})
        if not (isinstance(rec, dict) and rec.get("hash")):
            raise ValueError("No hub password is set yet. See the steps on this page.")
        given = str(password or "")
        ok = hmac.compare_digest(_hash(given, rec["salt"], int(rec.get("iterations") or ITERATIONS)), rec["hash"])
        if not ok:
            with self.lock:
                self.fails.append((time.time(), address))
            self.log("wrong password", address, agent)
            time.sleep(1)
            raise ValueError("Wrong password")
        token = secrets.token_urlsafe(32)
        with self.lock:
            self._load_sessions()
            self.sessions[_token_id(token)] = {"until": int(now + SESSION_DAYS * 86400), "made": int(now),
                                               "from": address, "agent": (agent or "")[:120]}
            self._save_sessions()
        self.log("login", address, agent)
        return token

    def logout(self, cookie_header, address="", everywhere=False):
        with self.lock:
            self._load_sessions()
            if everywhere:
                self.sessions = {}
            else:
                self.sessions.pop(_token_id(self.token_from(cookie_header) or "-"), None)
            self._save_sessions()
        self.log("logout everywhere" if everywhere else "logout", address)

    @staticmethod
    def cookie(token, secure):
        """The Set-Cookie value: kept 30 days, never readable by the page's scripts."""
        if not token:
            return f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
        return (f"{COOKIE}={token}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Lax"
                + ("; Secure" if secure else ""))


# ---------------------------------------------------------------------- command line
PEER = os.environ.get("AIHUB_AUTH_PEER", "server")   # the ssh name of the other PC


def _set_password_here(rec):
    _write_private(AUTH_FILE, json.dumps(rec, indent=1))
    _write_private(SESSIONS_FILE, "{}")     # a new password logs every browser out


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "set-password":
        if "--stdin" in argv:          # tests: the password on standard input, this PC only
            pw = sys.stdin.readline().rstrip("\n")
            again = pw
        else:
            pw = getpass.getpass("New AI Hub password: ")
            again = getpass.getpass("Type it again: ")
        if pw != again:
            sys.exit("The two didn't match. Nothing was changed.")
        if len(pw) < MIN_LENGTH:
            sys.exit(f"Use at least {MIN_LENGTH} characters. Nothing was changed.")
        rec = password_record(pw)
        _set_password_here(rec)
        print("Set on this PC. Every browser here has to log in again.")
        if "--stdin" not in argv and "--this-pc-only" not in argv:
            r = subprocess.run(["ssh", PEER, "python3", "~/Documents/aimon/hub_auth.py", "import-password"],
                               input=json.dumps(rec), text=True, capture_output=True, timeout=60)
            if r.returncode:
                print(f"Couldn't set it on '{PEER}': {(r.stderr or r.stdout).strip()}")
                print("Run this again on that PC, or later with the server switched on.")
            else:
                print(r.stdout.strip())
    elif cmd == "import-password":     # used by set-password on the other PC: the hash, never the password
        rec = json.loads(sys.stdin.read())
        if not (isinstance(rec, dict) and rec.get("hash") and rec.get("salt")):
            sys.exit("That isn't a password record")
        _set_password_here(rec)
        print(f"Set on {os.uname().nodename} too. Every browser there has to log in again.")
    elif cmd == "logout-all":
        _write_private(SESSIONS_FILE, "{}")
        print("Every browser on this PC has to log in again.")
    elif cmd == "make-key":
        if read_key():
            print(f"{KEY_FILE} already exists")
        else:
            _write_private(KEY_FILE, secrets.token_urlsafe(32) + "\n")
            print(f"Made {KEY_FILE}. Copy it to the other PC (same path).")
    elif cmd == "status":
        sessions = _read_json(SESSIONS_FILE, {})
        live = [s for s in sessions.values() if s.get("until", 0) > time.time()] if isinstance(sessions, dict) else []
        print(f"Password set: {'yes' if Auth.has_password() else 'NO'}")
        print(f"Machine key:  {'yes' if read_key() else 'NO'} ({KEY_FILE})")
        print(f"Logged-in browsers: {len(live)}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
