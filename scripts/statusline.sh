#!/bin/bash
# Claude Code status line for the background-worker skill: the model, the shared app window, the worker, caffeinate,
# and context use. Project settings come from <cwd>/.autopilot/worker/config.json ("lock", "app_process"); without one
# it shows only what it can find.
#   🟢 window free | 🔴 window: <who> <what> <time>  (+ "app open" while the app runs)
#   🔨 build: <who> since <time>  (only with "build_lock" in config.json: a build beside the app holds that lock)
#   HP ⏩ N (building/capturing) | HP ✋ PAUSED at N | HP ■ stopped | HP idle   (the background worker, short tasks)
#   LP ⏩ 52 74 | LP 📝 desk 52 | LP ✋ PAUSED 52 74 | LP none                          (long tasks; 📝 desk = kept off the window)
#   MP ⏩ 110 (shown before LP, only while MP has open tasks; the same signs as LP)
#   ctx 41% · week 24% · fable 40%   (context use; the plan's weekly limit; each model's own week, from plan_usage.py)
#   A part with something running (HP: 49; MP or LP running with a task in progress) is drawn in bright white
#   (queue.py statusline adds the codes; the user, 2026-11-07)
# The lock: a directory taken by the lead (an optional `who` file inside names it), or a file written by the worker
# ("worker build|capture <date>"). The HP worker's state: <cwd>/.autopilot/worker/state ("running N" / "paused" / "stopped");
# the LP group's mode: <cwd>/.autopilot/worker/lp_mode ("running" / "desk" / "paused").
SL_INPUT=$(cat); export SL_INPUT
SL_DIR=$(cd "$(dirname "$0")" && pwd); export SL_DIR
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
# The weekly usage (the user, 2026-11-05: "show the claude weekly usage in the status bar, after the ctx"):
# rate_limits.seven_day.used_percentage and its reset time, as Claude Code passes them.
wk = wkr = ""
try:
    sd = (d.get("rate_limits") or {}).get("seven_day") or {}
    if sd.get("used_percentage") is not None:
        wk = str(int(round(sd["used_percentage"])))
        if sd.get("resets_at"):
            import time
            wkr = time.strftime("%a %H:%M", time.localtime(int(sd["resets_at"])))
except Exception:
    pass
# Keep the plan limits for the ccprobe page (the user, 2026-11-08: "show how many weekly quota still left ... in the
# ccprobe web interface"): Claude Code gives rate_limits only to the status line, so the latest goes to
# ~/.cache/ccprobe/rate-limits.json, written when it changes or once a minute (the file time says it is fresh).
# Every session writes it, and an idle one still passes on the figure from its last reply (two days old, say), so
# each window keeps the newest reading: the later reset, and for the same reset the higher figure (use only grows
# inside a window). A window whose reset has passed is dropped.
try:
    rl = d.get("rate_limits")
    if isinstance(rl, dict) and rl:
        import time
        rdir = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ccprobe")
        rpath = os.path.join(rdir, "rate-limits.json")
        def _n(v):
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
        def _age(w):
            return (_n(w.get("resets_at")), _n(w.get("used_percentage"))) if isinstance(w, dict) else (-1, -1)
        try:
            with open(rpath) as f:
                old = json.load(f).get("rate_limits")
            old = old if isinstance(old, dict) else {}
            fresh = time.time() - os.path.getmtime(rpath) < 60
        except Exception:
            old, fresh = {}, False
        now = time.time()
        new = {k: w for k, w in old.items() if not (0 < _n((w or {}).get("resets_at")) < now)}
        for k, w in rl.items():
            if isinstance(w, dict) and _age(w) >= _age(new.get(k)) and not (0 < _n(w.get("resets_at")) < now):
                new[k] = w
        if new != old or not fresh:
            os.makedirs(rdir, exist_ok=True)
            tmp = "%s.%d.tmp" % (rpath, os.getpid())
            with open(tmp, "w") as f:
                json.dump({"rate_limits": new}, f)
            os.replace(tmp, rpath)
            for x in os.listdir(rdir):
                if x.endswith(".tmp") and now - os.path.getmtime(os.path.join(rdir, x)) > 600:
                    os.unlink(os.path.join(rdir, x))
        nw = (new.get("seven_day") or {}).get("used_percentage")
        if isinstance(nw, (int, float)):
            wk = str(int(round(nw)))
except Exception:
    pass
# The weekly limit of one model, the Fable week (the user, 2026-11-13: "for the weekly usage bar, can we also add
# another one for the fable weekly? also include it in the current status bar"): not in rate_limits, so plan_usage.py
# asks a headless Claude Code for the usage rows and saves them to ~/.cache/ccprobe/plan-usage.json; this shows them,
# as "fable 40%", lower case like "week", with the age once over half an hour, and starts that fetch in the background when it is due.
fab = ""
try:
    import sys, time, subprocess
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.environ.get("SL_DIR") or ".")
    import plan_usage
    snap = plan_usage.load()
    now = time.time()
    age = now - (snap.get("fetched_at") or 0)
    got = []
    for r in snap.get("limits") or []:
        sc = r.get("scope") or {}
        nm = (sc.get("model") or {}).get("display_name") or (sc.get("surface") or {}).get("display_name")
        rs = r.get("resets_at")
        try:
            gone = bool(rs) and plan_usage.when(rs) <= now
        except Exception:
            gone = False
        if r.get("kind") == "weekly_scoped" and nm and isinstance(r.get("percent"), (int, float)) and not gone:
            got.append("%s %d%%" % (nm.lower(), round(r["percent"])))
    if got:
        fab = " · ".join(got)
        if age > 1800:
            fab += " %s" % (("%dm ago" % (age // 60)) if age < 3600 else ("%dh ago" % (age // 3600)) if age < 86400 else ("%dd ago" % (age // 86400)))
    week = int(wk) if wk else None
    if plan_usage.available() and plan_usage.due(week, snap, now):
        subprocess.Popen([sys.executable, plan_usage.__file__, "refresh"] + (["--week", str(week)] if week is not None else []),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
except Exception:
    pass
cfg = {}
try:
    with open(os.path.join(c, ".autopilot", "worker", "config.json")) as f:
        cfg = json.load(f)
except Exception:
    pass
print("MODEL=%s CWD=%s PCT=%s WK=%s WKR=%s FAB=%s LOCK_REL=%s APP_PROC=%s BUILD_LOCK_REL=%s" % (
    shlex.quote(m), shlex.quote(c), shlex.quote("" if pct is None else str(int(pct))), shlex.quote(wk), shlex.quote(wkr), shlex.quote(fab),
    shlex.quote(" ".join(cfg.get("locks") or [cfg.get("lock", ".autopilot/run.lock")])), shlex.quote(cfg.get("app_process", "")),
    shlex.quote(cfg.get("build_lock", ""))))
EOF
)"
CWD=${CWD:-$PWD}
# CWD is the project root: the python above takes workspace.project_dir, or the nearest folder upward with
# .autopilot/worker (2026-10-25: the session's folder moves into subfolders, which used to hide the window part).
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
  # LOCK_REL: one lock, or several window slots separated by spaces (config "locks"). The user, 2026-10-29: "dont do
  # combine status for the windows in the status bar. allocate two permanent slots for them": one part per slot, always
  # shown in the same place: "🔴 win0: hp202 capture since 22:50 (12s)" · "🟢 win1 free".
  nl=0; set -- ${LOCK_REL:-.autopilot/run.lock}; nslots=$#
  for rel in ${LOCK_REL:-.autopilot/run.lock}; do
    nl=$(( nl + 1 )); lock="$CWD/$rel"; one=""
    if [ -d "$lock" ]; then
      who=$(cat "$lock/who" 2>/dev/null || echo "lead")
      set -- $who; tm=$(echo "$who" | grep -oE '[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?' | head -1)
      hd=$(held "$lock"); one="${1:-lead}${2:+ $2}${tm:+ since $tm}${hd:+ ($hd)}"
    elif [ -f "$lock" ]; then
      txt=$(cat "$lock"); set -- $txt; tm=$(echo "$txt" | grep -oE '[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?' | head -1)
      hd=$(held "$lock"); one="${1:-held}${2:+ $2}${tm:+ since $tm}${hd:+ ($hd)}"
    fi
    # 0-based (the user, 2026-10-29: "make the win index start from 0 ... win0, and win1"); a window the user reserved
    # for shows (a file <lock>.reserved) shows 🔒: "🔒 win1 reserved", "🔴 win1🔒: lead show since 23:30 (2m)".
    if [ "$nslots" -le 1 ]; then name="window"; else name="win$(( nl - 1 ))"; fi
    rs=""; [ -f "$lock.reserved" ] && rs="🔒"
    if [ -n "$one" ]; then parts+=("🔴 $name$rs: $one")
    elif [ -n "$rs" ]; then parts+=("🔒 $name reserved")
    else parts+=("🟢 $name free"); fi
  done
  # A pending "reserve whichever frees first" (<locks' folder>/winres.any, e.g. a tools/winres.sh reserve any)
  set -- ${LOCK_REL:-.autopilot/run.lock}; [ -f "$CWD/$(dirname "$1")/winres.any" ] && parts+=("🔒 next free win → reserved")
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
# Keep-awake: since 2026-11-08 each Claude session holds its own (`queue.py awake on|off`, awake.py: a
# `caffeinate -dims -w <its Claude pid>`), so this part says whether THIS session holds one: "☕ on", "☕ off", or
# "☕ off · on in 2 other sessions" when only others keep the Mac awake (theirs can stop at any time).
# A safety net (the user, 2026-11-05): with this session holding one and no queue row "in progress"/"in hand" in any
# table, say so: "☕ on · nothing working" (the lead missed turning it off).
if command -v caffeinate >/dev/null 2>&1; then
  set -- $(python3 "$(dirname "$0")/awake.py" counts 2>/dev/null); mine=${1:-0}; others=${2:-0}
  if [ "$mine" -gt 0 ]; then
    busy=1
    if [ -f "$CWD/.autopilot/worker/queue.md" ]; then
      busy=$(awk -F'|' '$2 ~ /^ *[0-9]+ *$/ && $5 ~ /^ *(in progress|in hand)/ {n++} END{print n+0}' "$CWD/.autopilot/worker/queue.md" 2>/dev/null)
    fi
    if [ "${busy:-1}" = "0" ]; then parts+=("☕ on · nothing working"); else parts+=("☕ on"); fi
  elif [ "$others" -gt 0 ]; then
    if [ "$others" = 1 ]; then parts+=("☕ off · on in 1 other session"); else parts+=("☕ off · on in $others other sessions"); fi
  else parts+=("☕ off"); fi
fi
[ -n "$PCT" ] && parts+=("ctx ${PCT}%")
[ -n "$WK" ] && parts+=("week ${WK}%")
[ -n "$FAB" ] && parts+=("$FAB")
out=""; for p in "${parts[@]}"; do out="${out:+$out · }$p"; done
printf '%s' "$out"
