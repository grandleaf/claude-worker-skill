#!/usr/bin/env python3
"""Keep the Mac awake while a Claude session's agents work: one hold per session, and only its own.

    awake.py on        this session holds a keep-awake (nothing new when it already does)
    awake.py off       this session lets its own go; other sessions' holds stay
    awake.py [status]  who holds one: this session, the others (their project, since when)
    awake.py counts    "MINE OTHERS" for the status line; nothing where there is no caffeinate

A hold is `caffeinate -dims -w <the session's Claude pid>`, started detached (its own process session, so it outlives
the shell that ran this) and ended by `off`, or by macOS itself the moment that Claude process exits.

Why (the user, 2026-11-08: "when multiple claude session turn on or turn off caffinate, that will cause race
condition"). Until then each lead ran "start caffeinate if none is running" and stopped "it" when its own work ended.
The check was global but the stop was not: a lead that found another project's caffeinate started none, and when that
other lead stopped its own the Mac could sleep under the first one's agents; a lead that wanted "it" off killed
whatever pgrep found (twice `pkill -x caffeinate`, which also killed Claude Code's own short `caffeinate -i -t 300`);
and a session that died left its caffeinate running for good.

A shared counter (lock a file, +1 / -1, start at 0 -> 1, stop at 1 -> 0) fixes the first two but drifts: a session
that crashes, or forgets its -1, leaves the count above zero and the Mac never sleeps again; a double -1 stops it under
someone else. macOS already keeps that count without drift: every power assertion is one, the Mac stays awake while any
is held, and a process's assertions go when it exits. So each session holds its own process, `on` and `off` are
idempotent, no session can stop another's, and `-w` ties each hold to its session's life. A file lock still serialises
`on` and `off`, so two calls at once in one session cannot start two holds.

The session is the nearest `claude` process above this one (CLAUDE_PID when there is none to walk to). Where there is
no `caffeinate` (Linux, a server that does not sleep) every command is a no-op.
"""
import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time

LOCK = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "claude-worker", "awake.lock")


def available():
    return bool(shutil.which("caffeinate"))


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def table():
    """{pid: (ppid, comm)} for every process (one ps call; comm is the last column, so a path with spaces is whole)."""
    out = {}
    for line in _run(["ps", "-axo", "pid=,ppid=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return out


def _is_claude(ent):
    return bool(ent) and os.path.basename(ent[1]) == "claude"


def claude_above(pid, procs):
    """The nearest claude process above pid (pid itself excluded), or None."""
    seen, p = set(), procs.get(pid, (0, ""))[0]
    while p > 1 and p not in seen:
        seen.add(p)
        ent = procs.get(p)
        if not ent:
            return None
        if _is_claude(ent):
            return p
        p = ent[0]
    return None


def session_pid(procs=None):
    """This Claude session's process: the nearest claude above us, else CLAUDE_PID if that process runs; else None."""
    procs = procs if procs is not None else table()
    p = claude_above(os.getpid(), procs)
    if p:
        return p
    v = os.environ.get("CLAUDE_PID", "")
    return int(v) if v.isdigit() and int(v) in procs else None


def holds(procs=None):
    """Every keep-awake hold: [{pid, session, watch}]. `session` is the Claude pid it belongs to: the one it watches
    (`-w`), or for an older plain `caffeinate -dims` (a session's background task) the claude above it, or 0 when none
    is (left running by a shell that is gone). Claude Code's own short `caffeinate -i -t 300` is not a hold."""
    procs = procs if procs is not None else table()
    cafs = [p for p, (_, comm) in procs.items() if os.path.basename(comm) == "caffeinate"]
    if not cafs:
        return []
    out = []
    for line in _run(["ps", "-o", "pid=,args=", "-p", ",".join(map(str, cafs))]).splitlines():
        pid, _, args = line.strip().partition(" ")
        t = args.split()
        if not pid.isdigit() or len(t) < 2 or os.path.basename(t[0]) != "caffeinate" or t[1] != "-dims":
            continue
        if len(t) == 4 and t[2] == "-w" and t[3].isdigit():
            out.append({"pid": int(pid), "session": int(t[3]), "watch": True})
        elif len(t) == 2:
            out.append({"pid": int(pid), "session": claude_above(int(pid), procs) or 0, "watch": False})
    return out


def counts(me=None, procs=None):
    """(this session's holds, how many other sessions hold one); a hold with no session counts as one other."""
    procs = procs if procs is not None else table()
    me = me if me is not None else session_pid(procs)
    hs = holds(procs)
    mine = sum(1 for h in hs if me and h["session"] == me)
    others = {h["session"] or -h["pid"] for h in hs if not (me and h["session"] == me)}
    return mine, len(others)


@contextlib.contextmanager
def _locked():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _others_text(me, procs):
    n = counts(me, procs)[1]
    return f"{n} other session{'s' if n != 1 else ''} still keep{'s' if n == 1 else ''} the Mac awake" if n \
        else "no other session keeps the Mac awake"


def _gone(pids, wait=2.0):
    end = time.time() + wait
    while time.time() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return not any(_alive(p) for p in pids)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _me_or_exit(procs):
    me = session_pid(procs)
    if not me:
        sys.exit("awake: no Claude session above this command (run it from a Claude session's Bash, or set CLAUDE_PID)")
    return me


def on():
    if not available():
        return "awake: no caffeinate here (macOS only): nothing to do"
    with _locked():
        procs = table()
        me = _me_or_exit(procs)
        mine = [h for h in holds(procs) if h["session"] == me]
        kept = next((h for h in mine if h["watch"]), None)
        if kept:
            return f"awake: on already for this session (caffeinate {kept['pid']}, until `awake off` or the session ends)"
        p = subprocess.Popen(["caffeinate", "-dims", "-w", str(me)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        time.sleep(0.2)
        if p.poll() is not None:
            sys.exit(f"awake: caffeinate exited at once (code {p.returncode})")
        old = [h["pid"] for h in mine if not h["watch"]]   # this session's own older plain `caffeinate -dims`
        for pid in old:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        note = f"; replaced its older caffeinate {', '.join(map(str, old))}" if old else ""
        return f"awake: on for this session (caffeinate {p.pid}, until `awake off` or the session ends){note}"


def off():
    if not available():
        return "awake: no caffeinate here (macOS only): nothing to do"
    with _locked():
        procs = table()
        me = _me_or_exit(procs)
        mine = [h["pid"] for h in holds(procs) if h["session"] == me]
        for pid in mine:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        if mine and not _gone(mine):
            return f"awake: asked caffeinate {', '.join(map(str, mine))} to stop, still running"
        procs = table()
        head = f"awake: off for this session (stopped caffeinate {', '.join(map(str, mine))})" if mine \
            else "awake: off for this session (it held none)"
        return f"{head}; {_others_text(me, procs)}"


def _cwd(pid):
    for line in _run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"]).splitlines():
        if line.startswith("n"):
            return os.path.basename(line[1:].rstrip("/")) or line[1:]
    return "?"


def status():
    if not available():
        return "awake: no caffeinate here (macOS only)"
    procs = table()
    me = session_pid(procs)
    hs = holds(procs)
    etime = {}
    if hs:
        for line in _run(["ps", "-o", "pid=,etime=", "-p", ",".join(str(h["pid"]) for h in hs)]).splitlines():
            a = line.split()
            if len(a) == 2 and a[0].isdigit():
                etime[int(a[0])] = a[1]
    mine = [h for h in hs if me and h["session"] == me]
    lines = [f"this session ({me or 'not found'}): " + (", ".join(
        f"on, caffeinate {h['pid']} for {etime.get(h['pid'], '?')}{'' if h['watch'] else ' (older plain one)'}" for h in mine) or "off")]
    for h in hs:
        if h in mine:
            continue
        who = f"session {h['session']} ({_cwd(h['session'])})" if h["session"] else "no session (left running)"
        lines.append(f"{who}: on, caffeinate {h['pid']} for {etime.get(h['pid'], '?')}{'' if h['watch'] else ' (older plain one)'}")
    if len(lines) == 1:
        lines.append("no other session holds one")
    return "\n".join(lines)


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "counts":
        if available():
            print("%d %d" % counts())
        return
    fn = {"on": on, "off": off, "status": status}.get(cmd)
    if not fn:
        sys.exit(__doc__.split("\n\n")[1])
    print(fn())


if __name__ == "__main__":
    main(sys.argv[1:])
