#!/usr/bin/python3 -I
"""aidoor: AI Hub's small root helper (security checklist c1).

It does only these things, and only from its own settings file (/etc/aidoor/config.json, which only
root can change):

  * opens a "door": makes one of your approved folders appear inside a running AI container, at
    /doors/<folder>, read-only or read-write, without restarting the container;
  * closes a door (the folder disappears from the container straight away);
  * restarts this PC, and sets the PCIe slot speed cap (AI Hub's Restart and PCIe buttons).

How a door works: each container that may have doors mounts <door_root>/<container> at /doors, once,
with bind propagation "rslave". A folder bind-mounted under that directory later shows up inside the
running container. Read-only doors are made read-only in a private staging folder first, so they are
never writable for even a moment. Read-write doors map the container's user to you (an idmapped
mount), so files it writes are yours, not root's. Doors are mounts, so a restart closes them all.

Every open and close goes to /var/log/aidoor.log. Install with install-aidoor.sh.

  aidoor list [--json]
  aidoor open <container> <folder> ro|rw [--until EPOCH] [--by TEXT]
  aidoor close <container> <folder> [--by TEXT] [--reason TEXT]
  aidoor expire                          close the doors whose time is up
  aidoor approve <path> [--id ID] [--label TEXT]     (you, with sudo: add a folder to the list)
  aidoor unapprove <id>
  aidoor add-container <name> [--label TEXT] [--uid N]
  aidoor reboot [--force]
  aidoor pcie <port> read-max|read-target|set <gen>
  aidoor can reboot
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

CONFIG = "/etc/aidoor/config.json"
STATE_DIR = "/run/aidoor"
STATE = STATE_DIR + "/doors.json"
LOCK = STATE_DIR + "/lock"
LOG = "/var/log/aidoor.log"
MOUNT, UMOUNT = "/usr/bin/mount", "/usr/bin/umount"
SETPCI, SYSTEMCTL = "/usr/bin/setpci", "/usr/bin/systemctl"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
PORT_RE = re.compile(r"^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
LOG_LINES = 60


class Refused(Exception):
    pass


# ---------------------------------------------------------------------------------------- files
def load_config():
    st = os.stat(CONFIG)
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise Refused(f"{CONFIG} must belong to root and not be writable by others")
    with open(CONFIG) as f:
        cfg = json.load(f)
    cfg.setdefault("door_root", "/srv/aidoors")
    cfg.setdefault("owner_uid", 1000)
    cfg.setdefault("allow_roots", ["/home/andrew", "/mnt"])
    cfg.setdefault("containers", {})
    cfg.setdefault("folders", {})
    cfg.setdefault("pcie_ports", [])
    cfg.setdefault("reboot", False)
    return cfg


def save_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, CONFIG)


def load_state():
    try:
        with open(STATE) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE)


def log(event, **kw):
    line = json.dumps({"at": int(time.time()), "event": event, **kw}, ensure_ascii=False)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def read_log(n=LOG_LINES):
    try:
        with open(LOG) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------------------------------- mounts
def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mounts():
    """{mount point: options} from this process's view of the mount table."""
    out = {}
    with open("/proc/self/mountinfo") as f:
        for line in f:
            parts = line.split()
            out[_unescape(parts[4])] = parts[5]
    return out


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kw)
    if r.returncode:
        raise Refused((r.stderr or r.stdout).strip() or f"{cmd[0]} failed ({r.returncode})")
    return r.stdout.strip()


def ensure_stage(root):
    """A private folder for building doors, so nothing is visible to containers half-made."""
    stage = os.path.join(root, ".stage")
    os.makedirs(stage, mode=0o700, exist_ok=True)
    if stage not in mounts():
        run([MOUNT, "--bind", stage, stage])
        run([MOUNT, "--make-private", stage])
    return stage


def check_path(cfg, path):
    """The real path of an approvable folder, or Refused."""
    real = os.path.realpath(path)
    if real != os.path.abspath(path):
        raise Refused(f"{path} goes through a link; approve the real folder: {real}")
    if not os.path.isdir(real):
        raise Refused(f"{real} isn't a folder")
    root = next((r for r in cfg["allow_roots"] if real.startswith(r.rstrip("/") + "/")), None)
    if not root:
        raise Refused(f"Only folders inside {', '.join(cfg['allow_roots'])} can have doors")
    rel = real[len(root.rstrip("/")) + 1:]
    if any(part.startswith(".") for part in rel.split("/")):
        raise Refused("Hidden folders (and anything inside one, like .ssh or .config) can't have doors")
    door_root = os.path.realpath(cfg["door_root"])
    if real == door_root or real.startswith(door_root + "/"):
        raise Refused("That's inside the doors folder itself")
    return real


# ---------------------------------------------------------------------------------------- commands
def cmd_list(cfg, as_json):
    state, mt, now = load_state(), mounts(), time.time()
    root = cfg["door_root"]
    doors, changed = [], False
    for c in cfg["containers"]:
        cdir = os.path.join(root, c)
        for f in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
            p = os.path.join(cdir, f)
            if p in mt:
                meta = state.get(f"{c}/{f}", {})
                folder = cfg["folders"].get(f, {})
                doors.append({"container": c, "folder": f, "label": folder.get("label", f), "path": folder.get("path", ""),
                              "mode": "ro" if mt[p].split(",")[0] == "ro" else "rw",
                              "until": meta.get("until"), "opened": meta.get("opened"), "by": meta.get("by", "")})
            else:
                try:
                    os.rmdir(p)             # an empty leftover (e.g. after a restart closed the door)
                except OSError:
                    pass
    for key in list(state):
        c, _, f = key.partition("/")
        if not any(d["container"] == c and d["folder"] == f for d in doors):
            state.pop(key)
            changed = True
    if changed:
        save_state(state)
    data = {"containers": cfg["containers"], "folders": cfg["folders"], "doors": doors, "log": read_log(),
            "door_root": root, "reboot": bool(cfg["reboot"]), "pcie_ports": cfg["pcie_ports"], "now": int(now)}
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        for d in doors:
            until = time.strftime("%d %b %H:%M", time.localtime(d["until"])) if d["until"] else "until closed"
            print(f"{d['container']:<16} {d['folder']:<24} {d['mode']}  {until}  ({d['path']})")
        if not doors:
            print("No doors open.")


def cmd_open(cfg, c, f, mode, until, by):
    if c not in cfg["containers"]:
        raise Refused(f"{c} isn't a container that may have doors")
    if f not in cfg["folders"]:
        raise Refused(f"{f} isn't an approved folder")
    if mode not in ("ro", "rw"):
        raise Refused("Choose ro (read-only) or rw (read-write)")
    if until is not None and until <= time.time():
        raise Refused("That time has already passed")
    root = cfg["door_root"]
    door = os.path.join(root, c, f)
    state, mt = load_state(), mounts()
    if door in mt:
        now_mode = "ro" if mt[door].split(",")[0] == "ro" else "rw"
        if now_mode != mode:
            raise Refused("That door is already open the other way; close it first")
        state.setdefault(f"{c}/{f}", {})["until"] = until
        save_state(state)
        log("extend", container=c, folder=f, mode=mode, until=until, by=by)
        print("Already open; its time is updated")
        return
    path = check_path(cfg, cfg["folders"][f]["path"])
    if path != cfg["folders"][f]["path"]:
        raise Refused("The approved folder has moved or become a link; approve it again")
    # Hold the folder open, so it can't be swapped for a link between the check and the mount.
    fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.readlink(f"/proc/self/fd/{fd}") != path:
            raise Refused("The folder changed while opening the door")
        stage = ensure_stage(root)
        tmp = os.path.join(stage, f"{c}--{f}--{os.getpid()}")
        os.mkdir(tmp, 0o700)
        try:
            src = f"/proc/self/fd/{fd}"
            if mode == "ro":
                run([MOUNT, "--no-canonicalize", "--bind", src, tmp], pass_fds=(fd,))
                run([MOUNT, "-o", "remount,bind,ro", tmp])
            else:
                uid, owner = int(cfg["containers"][c].get("uid", 0)), int(cfg["owner_uid"])
                opts = [] if uid == owner else ["-o", f"X-mount.idmap=b:{owner}:{uid}:1"]
                run([MOUNT, "--no-canonicalize", "--bind", *opts, src, tmp], pass_fds=(fd,))
            os.makedirs(os.path.join(root, c), mode=0o755, exist_ok=True)
            os.makedirs(door, mode=0o755, exist_ok=True)
            run([MOUNT, "--bind", tmp, door])
        finally:
            if tmp in mounts():
                run([UMOUNT, tmp])
            os.rmdir(tmp)
    finally:
        os.close(fd)
    opts = mounts().get(door, "")
    if not opts or (mode == "ro" and opts.split(",")[0] != "ro"):
        cmd_close(cfg, c, f, by, "check failed")
        raise Refused("The door didn't come out as asked, so it was closed again")
    state[f"{c}/{f}"] = {"mode": mode, "until": until, "opened": int(time.time()), "by": by}
    save_state(state)
    log("open", container=c, folder=f, path=path, mode=mode, until=until, by=by)
    print(f"Opened {cfg['folders'][f].get('label', f)} for {cfg['containers'][c].get('label', c)} ({'read-only' if mode == 'ro' else 'read-write'})")


def cmd_close(cfg, c, f, by, reason=""):
    if c not in cfg["containers"] or not NAME_RE.match(f):
        raise Refused("No such door")
    door = os.path.join(cfg["door_root"], c, f)
    if door not in mounts():
        state = load_state()
        state.pop(f"{c}/{f}", None)
        save_state(state)
        try:
            os.rmdir(door)
        except OSError:
            pass
        print("That door is already closed")
        return
    r = subprocess.run([UMOUNT, door], capture_output=True, text=True, timeout=30)
    if r.returncode:   # a program inside still has a file open: detach now, finish when it lets go
        run([UMOUNT, "--lazy", door])
    try:
        os.rmdir(door)
    except OSError:
        pass
    state = load_state()
    state.pop(f"{c}/{f}", None)
    save_state(state)
    log("close", container=c, folder=f, by=by, reason=reason)
    print("Closed")


def cmd_expire(cfg):
    now, n = time.time(), 0
    for key, meta in list(load_state().items()):
        if meta.get("until") and meta["until"] <= now:
            c, _, f = key.partition("/")
            cmd_close(cfg, c, f, "aidoor", "time up")
            n += 1
    print(f"{n} door(s) closed")


def cmd_approve(cfg, path, fid, label):
    real = check_path(cfg, path)
    fid = fid or re.sub(r"[^a-z0-9_.-]+", "-", os.path.basename(real).lower()).strip("-.") or "folder"
    if not NAME_RE.match(fid):
        raise Refused("The id may use a-z, 0-9, dot, dash and underscore")
    if fid in cfg["folders"] and cfg["folders"][fid]["path"] != real:
        raise Refused(f"The id {fid} is already used for {cfg['folders'][fid]['path']}; give another with --id")
    cfg["folders"][fid] = {"label": label or os.path.basename(real), "path": real}
    save_config(cfg)
    log("approve", folder=fid, path=real, by=os.environ.get("SUDO_USER", "root"))
    print(f"Approved {real} as '{fid}'")


def cmd_unapprove(cfg, fid):
    if fid not in cfg["folders"]:
        raise Refused(f"{fid} isn't approved")
    if any(k.endswith("/" + fid) for k in load_state()):
        raise Refused("Close its doors first")
    del cfg["folders"][fid]
    save_config(cfg)
    log("unapprove", folder=fid, by=os.environ.get("SUDO_USER", "root"))
    print(f"Removed {fid}")


def cmd_add_container(cfg, name, label, uid):
    if not NAME_RE.match(name):
        raise Refused("Container names may use a-z, 0-9, dot, dash and underscore")
    cfg["containers"][name] = {"label": label or name, "uid": uid}
    save_config(cfg)
    os.makedirs(os.path.join(cfg["door_root"], name), mode=0o755, exist_ok=True)
    log("add-container", container=name, by=os.environ.get("SUDO_USER", "root"))
    print(f"Added {name}. Give the container this mount once (compose 'volumes:'):\n"
          f"  - {cfg['door_root']}/{name}:/doors:ro,rslave")


def cmd_pcie(cfg, port, op, gen):
    port = (port or "").lower()
    if not PORT_RE.match(port) or port not in cfg["pcie_ports"]:
        raise Refused(f"No speed setting for slot {port} on this PC")
    if op == "read-max":
        print(run([SETPCI, "-s", port, "CAP_EXP+0c.l"]))
    elif op == "read-target":
        print(run([SETPCI, "-s", port, "CAP_EXP+30.w"]))
    elif op == "set" and gen in ("1", "2", "3", "4", "5"):
        run([SETPCI, "-s", port, f"CAP_EXP+30.w={int(gen):x}:f"])
        log("pcie", port=port, gen=int(gen), by=os.environ.get("SUDO_USER", "root"))
        print("ok")
    else:
        raise Refused("Use read-max, read-target or set <1-5>")


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise Refused(f"{name} needs a value")
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return default


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if os.geteuid() != 0:
        raise Refused("Run it with sudo")
    cmd = args.pop(0)
    by = " ".join(str(opt(args, "--by") or os.environ.get("SUDO_USER") or "root").split())[:80]
    cfg = load_config()
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if cmd == "list":
            cmd_list(cfg, "--json" in args)
        elif cmd == "open" and len(args) >= 3:
            until = opt(args, "--until")
            cmd_open(cfg, args[0], args[1], args[2], int(until) if until else None, by)
        elif cmd == "close" and len(args) >= 2:
            reason = opt(args, "--reason", "")
            cmd_close(cfg, args[0], args[1], by, reason)
        elif cmd == "expire":
            cmd_expire(cfg)
        elif cmd == "approve" and args:
            fid, label = opt(args, "--id"), opt(args, "--label")
            cmd_approve(cfg, args[0], fid, label)
        elif cmd == "unapprove" and args:
            cmd_unapprove(cfg, args[0])
        elif cmd == "add-container" and args:
            label, uid = opt(args, "--label"), int(opt(args, "--uid", "0"))
            cmd_add_container(cfg, args[0], label, uid)
        elif cmd == "reboot":
            if not cfg["reboot"]:
                raise Refused("Restarting isn't allowed on this PC (reboot: false in the config)")
            log("reboot", force="--force" in args, by=by)
            subprocess.Popen([SYSTEMCTL, "reboot"] + (["--force"] if "--force" in args else []))
            print("Restarting")
        elif cmd == "pcie" and len(args) >= 2:
            cmd_pcie(cfg, args[0], args[1], args[2] if len(args) > 2 else None)
        elif cmd == "can" and args == ["reboot"]:
            return 0 if cfg["reboot"] else 1
        else:
            raise Refused(f"Unknown command: {cmd} (see aidoor --help)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Refused as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"aidoor: {e}", file=sys.stderr)
        sys.exit(2)
