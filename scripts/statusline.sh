#!/bin/bash
# Claude Code status line for the background-worker skill: the model, the shared app window, the worker, caffeinate,
# and context use. Project settings come from <cwd>/.autopilot/worker/config.json ("lock", "app_process"); without one
# it shows only what it can find.
#   🟢 window free | 🔴 window: <who> <what> <time>  (+ "app open" while the app runs)
#   🔨 build: <who> since <time>  (only with "build_lock" in config.json: a build beside the app holds that lock)
#   HP ⏩ N (building/capturing) | HP ✋ PAUSED at N | HP ■ stopped | HP idle   (the background worker, short tasks)
#   LP ⏩ 52 74 | LP 📝 desk 52 | LP ✋ PAUSED 52 74 | LP none                          (long tasks; 📝 desk = kept off the window)
#   MP ⏩ 110 (shown before LP, only while MP has open tasks; the same signs as LP)
# The lock: a directory taken by the lead (an optional `who` file inside names it), or a file written by the worker
# ("worker build|capture <date>"). The HP worker's state: <cwd>/.autopilot/worker/state ("running N" / "paused" / "stopped");
# the LP group's mode: <cwd>/.autopilot/worker/lp_mode ("running" / "desk" / "paused").
SL_INPUT=$(cat); export SL_INPUT
eval "$(python3 - <<'EOF' 2>/dev/null
import json, os, shlex
try:
    d = json.loads(os.environ.get("SL_INPUT") or "{}")
except Exception:
    d = {}
m = (d.get("model") or {}).get("display_name", "")
ws = d.get("workspace") or {}
c = ws.get("current_dir") or d.get("cwd", "") or os.getcwd()
def _root(p):
    p = os.path.abspath(p)
    while True:
        if os.path.isdir(os.path.join(p, ".autopilot", "worker")):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up
c = next((r for r in (_root(x) for x in (ws.get("project_dir"), c) if x) if r), c)
cw = d.get("context_window") or {}
pct = cw.get("used_percentage")
if pct is None:
    cur = d.get("context_window_tokens") or cw.get("current_usage")
    size = cw.get("context_window_size")
    try:
        if cur and size:
            if isinstance(cur, dict):
                cur = sum(v for v in cur.values() if isinstance(v, (int, float)))
            pct = round(100 * cur / size)
    except Exception:
        pct = None
cfg = {}
try:
    with open(os.path.join(c, ".autopilot", "worker", "config.json")) as f:
        cfg = json.load(f)
except Exception:
    pass
print("MODEL=%s CWD=%s PCT=%s LOCK_REL=%s APP_PROC=%s BUILD_LOCK_REL=%s" % (
    shlex.quote(m), shlex.quote(c), shlex.quote("" if pct is None else str(int(pct))),
    shlex.quote(cfg.get("lock", ".autopilot/run.lock")), shlex.quote(cfg.get("app_process", "")),
    shlex.quote(cfg.get("build_lock", ""))))
EOF
)"
CWD=${CWD:-$PWD}
# CWD is the project root: the python above takes workspace.project_dir, or the nearest folder upward with
# .autopilot/worker (the session's folder can move into subfolders, which would hide the window part).
# No comments inside that heredoc: bash 3.2 misparses them inside $( ).
# held <dir>: how long a lock has been held, from the folder's own time (made when it was taken): "45s", "7m", "1h05m"
held() {
  local t now s
  t=$(stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null) || return
  now=$(date +%s); s=$(( now - t )); [ "$s" -lt 0 ] && s=0
  if [ "$s" -lt 60 ]; then echo "${s}s"
  elif [ "$s" -lt 3600 ]; then echo "$(( s / 60 ))m"
  else printf '%dh%02dm\n' $(( s / 3600 )) $(( s % 3600 / 60 )); fi
}
parts=("${MODEL:-Claude}")
if [ -d "$CWD/.autopilot/worker" ]; then
  lock="$CWD/${LOCK_REL:-.autopilot/run.lock}"
  if [ -d "$lock" ]; then
    who=$(cat "$lock/who" 2>/dev/null || echo "lead")
    set -- $who; tm=$(echo "$who" | grep -oE '[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?' | head -1)
    hd=$(held "$lock")
    win="🔴 window: ${1:-lead}${2:+ $2}${tm:+ since $tm}${hd:+ ($hd)}"
  elif [ -f "$lock" ]; then
    txt=$(cat "$lock"); set -- $txt; tm=$(echo "$txt" | grep -oE '[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?' | head -1)
    hd=$(held "$lock")
    win="🔴 window: ${1:-held}${2:+ $2}${tm:+ since $tm}${hd:+ ($hd)}"
  else
    win="🟢 window free"
  fi
  [ -n "$APP_PROC" ] && pgrep -x "$APP_PROC" >/dev/null 2>&1 && win="$win · app open"
  parts+=("$win")
  # A build beside the app (config "build_lock") holds its own lock, not the window: "🔨 build: hp182 since 21:40"
  if [ -n "$BUILD_LOCK_REL" ] && [ -d "$CWD/$BUILD_LOCK_REL" ]; then
    bw=$(cat "$CWD/$BUILD_LOCK_REL/who" 2>/dev/null || echo "someone")
    set -- $bw; tm=$(echo "$bw" | grep -oE '[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?' | head -1)
    hd=$(held "$CWD/$BUILD_LOCK_REL")
    parts+=("🔨 build: ${1:-someone}${tm:+ since $tm}${hd:+ ($hd)}")
  fi
  # The HP worker and the LP group, from the queue tool (one parser for the queue): "HP ⏩ 76 (capturing)", "LP 📝 desk 52 74✋"
  hplp=$(python3 "$(dirname "$0")/queue.py" --root "$CWD" statusline 2>/dev/null)
  if [ -n "$hplp" ]; then
    parts+=("${hplp%%$'\t'*}")
    parts+=("${hplp#*$'\t'}")
  fi
fi
if pgrep -f "^caffeinate -dims" >/dev/null 2>&1; then parts+=("☕ on"); elif command -v caffeinate >/dev/null 2>&1; then parts+=("☕ off"); fi
[ -n "$PCT" ] && parts+=("ctx ${PCT}%")
out=""; for p in "${parts[@]}"; do out="${out:+$out · }$p"; done
printf '%s' "$out"
