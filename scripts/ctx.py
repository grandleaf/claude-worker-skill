#!/usr/bin/env python3
"""Context size of each background agent: what every one of its steps sends again.

    ctx.py [--root <project root>] [--session <session id>] [--all]

Reads the transcripts under ~/.claude/projects/<root with / turned to ->/<session>/subagents/ (the newest session that
has agents, unless --session names one) and prints, newest first, each agent's id, description, its context at its
last step (input + cache read + cache write), and when that step was. Without --all, agents idle for more than a day
are left out.

The rule it serves: a fresh worker per task, a fresh agent per unit for MP and LP. A redo may go
back to the same agent only while its context is small (under about 300k); past that, a fresh agent from the notes.
"""
import argparse, glob, json, os, sys
from datetime import datetime, timedelta, timezone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--session")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    base = os.path.expanduser("~/.claude/projects/" + os.path.abspath(a.root).replace("/", "-"))
    if a.session:
        sub = os.path.join(base, a.session, "subagents")
    else:
        dirs = [d for d in glob.glob(os.path.join(base, "*", "subagents")) if os.path.isdir(d)]
        if not dirs:
            sys.exit("no agent transcripts under " + base)
        sub = max(dirs, key=os.path.getmtime)
    rows = []
    for f in glob.glob(os.path.join(sub, "agent-*.jsonl")):
        aid = os.path.basename(f)[6:-6]
        ctx, ts = 0, ""
        for line in open(f, errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            u = d.get("message", {}).get("usage") if d.get("type") == "assistant" else None
            if u:
                ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                ts = d.get("timestamp", "")
        desc = ""
        meta = f[:-6] + ".meta.json"
        if os.path.exists(meta):
            try:
                desc = json.load(open(meta)).get("description", "")
            except ValueError:
                pass
        rows.append((ts, aid, desc, ctx))
    cut = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for ts, aid, desc, ctx in sorted(rows, reverse=True):
        if not a.all and ts < cut:
            continue
        flag = "  <- big: fresh agent next time" if ctx > 300_000 else ""
        print(f"{aid[:9]}  {desc[:44]:44}  {ctx:>9,}  {ts[:16]}{flag}")


if __name__ == "__main__":
    main()
