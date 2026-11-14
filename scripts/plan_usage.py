#!/usr/bin/env python3
"""The plan's per-model weekly limits (the "Fable" week, beside the all-models one), for the status line and ccprobe.

    plan_usage.py refresh [--week N]   fetch when the saved reading is stale (the status line runs this, detached)
    plan_usage.py fetch                fetch now and print the rows
    plan_usage.py [show]               the saved rows, and how old they are

Why (the user, 2026-11-13: "for the weekly usage bar, can we also add another one for the fable weekly? also include it
in the current status bar"). Claude Code passes the status line only `rate_limits.five_hour` and `.seven_day` (from
the API's response headers); a model's own weekly limit is not among them, nor in a mod's `$.session.usage()`. It is
in the claude.ai usage endpoint's `limits[]` (the rows /usage draws), which Claude Code answers to an SDK host's
`get_usage` control request ("For callers that need only the plan rate limits, such as a usage meter"). So this asks a
short-lived headless Claude Code for it:

    claude -p --safe-mode --no-session-persistence --input-format stream-json --output-format stream-json --verbose
    stdin:  {"type":"control_request","request_id":"…","request":{"subtype":"get_usage","skip_behaviors":true}}

No prompt, so no model call and no tokens; `--safe-mode` loads no hooks, plugins, MCP servers or CLAUDE.md;
`--no-session-persistence` saves no transcript. Claude Code signs in by itself: nothing here reads a credential.

The rows go to ~/.cache/ccprobe/plan-usage.json, as the server sent them (kind, group, percent, resets_at, scope,
severity, is_active) with the time they were fetched:

    {"fetched_at": 1794615441, "week_seen": 23, "limits": [
        {"kind": "weekly_all", "percent": 24, "resets_at": "2026-11-19T14:59:59+00:00", "scope": null, …},
        {"kind": "weekly_scoped", "percent": 40, "scope": {"model": {"display_name": "Fable"}}, …}]}

A fetch that fails writes its reason to plan-usage.err (its time counts as an attempt) and keeps the last good rows.

How often: at most once a minute while the all-models weekly figure the status line sees moves (`--week`, compared
with `week_seen`, the figure at the last fetch), else every 10 minutes while any session draws its status line. One
fetch at a time across sessions (a lock file), and each runs in its own process session, so a status line that Claude
Code cancels does not take it down.
"""
import contextlib
import fcntl
import json
import os
import selectors
import shutil
import subprocess
import sys
import time

DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ccprobe")
PATH = os.path.join(DIR, "plan-usage.json")
ERR = os.path.join(DIR, "plan-usage.err")
LOCK = os.path.join(DIR, "plan-usage.lock")
QUIET, MOVING = 600, 60          # seconds between fetches: the week figure still, and moving
TIMEOUT = 40                     # a fetch that has not answered by then is given up (and killed)
ROW_KEYS = ("kind", "group", "percent", "resets_at", "scope", "severity", "is_active")
# a Claude session's own identity, which the headless one must not take for its own
DROP_ENV = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT")


def load():
    try:
        with open(PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def due(week=None, snap=None, now=None):
    """Is a fetch due? The last attempt (a good fetch or a failed one) against QUIET, or MOVING when `week` differs
    from the figure the last good fetch was asked with."""
    snap = load() if snap is None else snap
    now = time.time() if now is None else now
    last = max(snap.get("fetched_at") or 0, _mtime(ERR))
    moved = week is not None and snap.get("week_seen") is not None and week != snap.get("week_seen")
    return now - last >= (MOVING if moved else QUIET)


def when(v):
    """A row's resets_at (an ISO time, as the endpoint sends it; or Unix seconds) as Unix seconds, else None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000 if v > 1e11 else v
    if isinstance(v, str):
        import datetime as dt
        with contextlib.suppress(ValueError):
            t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()
    return None


def _claude():
    return shutil.which("claude") or next((p for p in (os.path.expanduser("~/.local/bin/claude"),) if os.access(p, os.X_OK)), None)


def available():
    return bool(_claude())


def ask(timeout=TIMEOUT):
    """The usage endpoint's rows as Claude Code answers `get_usage`, or raise RuntimeError with the reason."""
    exe = _claude()
    if not exe:
        raise RuntimeError("no claude command on PATH")
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    rid = "plan-usage-%d" % os.getpid()
    req = {"type": "control_request", "request_id": rid, "request": {"subtype": "get_usage", "skip_behaviors": True}}
    os.makedirs(DIR, exist_ok=True)
    p = subprocess.Popen([exe, "-p", "--safe-mode", "--no-session-persistence", "--input-format", "stream-json",
                          "--output-format", "stream-json", "--verbose"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=DIR, env=env)
    try:
        p.stdin.write((json.dumps(req) + "\n").encode())
        p.stdin.flush()
        sel, buf, end = selectors.DefaultSelector(), b"", time.time() + timeout
        sel.register(p.stdout, selectors.EVENT_READ)
        while time.time() < end:
            if not sel.select(max(0.0, end - time.time())):
                break
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("claude exited without answering (code %s)" % p.poll())
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                r = d.get("response") if isinstance(d, dict) and d.get("type") == "control_response" else None
                if not isinstance(r, dict) or r.get("request_id") != rid:
                    continue
                if r.get("subtype") != "success":
                    raise RuntimeError("get_usage refused: %s" % (r.get("error") or r.get("subtype")))
                body = r.get("response") or {}
                rl = body.get("rate_limits")
                if not body.get("rate_limits_available") or not isinstance(rl, dict):
                    raise RuntimeError("no plan limits for this sign-in (an API key, or the usage fetch failed)")
                rows = rl.get("limits")
                if not isinstance(rows, list):
                    raise RuntimeError("the usage reply carried no limits[] rows")
                return [{k: x.get(k) for k in ROW_KEYS} for x in rows if isinstance(x, dict)]
        raise RuntimeError("no answer in %ds" % timeout)
    finally:
        with contextlib.suppress(OSError):
            p.stdin.close()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _write(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def fetch(week=None):
    """Fetch, save, and return the snapshot; on failure keep the last good one and record why in plan-usage.err."""
    try:
        rows = ask()
    except (RuntimeError, OSError) as e:
        _write(ERR, "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
        raise
    snap = {"fetched_at": int(time.time()), "week_seen": week, "limits": rows}
    _write(PATH, json.dumps(snap))
    with contextlib.suppress(OSError):
        os.unlink(ERR)
    return snap


def refresh(week=None):
    """Fetch when due, one fetch at a time: a second caller while one runs returns at once."""
    os.makedirs(DIR, exist_ok=True)
    with open(LOCK, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        if due(week):
            with contextlib.suppress(RuntimeError, OSError):
                return fetch(week)
    return None


def label(row):
    sc = row.get("scope") or {}
    name = ((sc.get("model") or {}).get("display_name") or (sc.get("surface") or {}).get("display_name")) if isinstance(sc, dict) else None
    return name or {"weekly_all": "week", "session": "5h"}.get(row.get("kind"), row.get("kind") or "?")


def show():
    snap = load()
    if not snap.get("limits"):
        err = open(ERR).read().strip() if os.path.exists(ERR) else ""
        return "plan usage: nothing saved yet" + (" (last try: %s)" % err if err else "")
    age = int(time.time() - (snap.get("fetched_at") or 0))
    lines = ["plan usage, fetched %ds ago:" % age]
    for r in snap["limits"]:
        lines.append("  %-8s %5s%%  resets %s%s" % (label(r), r.get("percent"), r.get("resets_at"), "  (the server's pick)" if r.get("is_active") else ""))
    if os.path.exists(ERR):
        lines.append("last failed try: " + open(ERR).read().strip())
    return "\n".join(lines)


def main(argv):
    cmd = argv[0] if argv else "show"
    week = None
    if "--week" in argv:
        i = argv.index("--week")
        with contextlib.suppress(IndexError, ValueError):
            week = int(argv[i + 1])
    if cmd == "refresh":
        refresh(week)
    elif cmd == "fetch":
        try:
            fetch(week)
        except (RuntimeError, OSError) as e:
            sys.exit("plan usage: %s" % e)
        print(show())
    elif cmd == "show":
        print(show())
    else:
        sys.exit(__doc__.split("\n\n")[1])


if __name__ == "__main__":
    main(sys.argv[1:])
