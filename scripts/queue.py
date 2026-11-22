#!/usr/bin/env python3
"""The worker queue: the lead's tool for the background-worker skill.

The queue is a markdown table in <project>/.autopilot/worker/queue.md, one row per task, in run order:

    | # | Pri | Task | State | Accepted |
    |---|---|---|---|---|
    | 41 | HP | <the task, the user's words in quotes, addenda> | done 14:20 (`notes.md`) | ✔ 2026-10-17 (the user: "...") |

Four tables in one file. **HP** (high priority): the short tasks the background worker does one at a time and the
user checks one by one. **MP** (medium priority): mid-sized tasks, each on its own agent, ranked between the two: they
start before LP tasks and come before them for the shared window. **LP** (low priority): long tasks (a stage rebuild),
each on its own agent. **AG** (the lead's own agents, 2026-11-14): agents the lead starts itself, outside the queue,
one per row, named ag<N> (the user: "I want to make them a home in the WTS ... they should work like a normal WTS
task"); no limit and no group mode, and they do not keep the queue open (END) or the Mac awake. MP and LP each have a
group mode (running / desk-only / paused) the user sets for the whole table. One numbering for all: a task keeps its number when it moves between tables. The file keeps the HP rows first,
then MP, then LP, then AG, then the pinned tail (999, END, last). Rows without a Pri cell (an older queue) count as HP.

Settings live in <project>/.autopilot/worker/config.json (see templates/config.json): the shared-window lock, the app's
process name, the worker's model, and the pinned tail (e.g. [999]).
State files beside the queue: `state` (the HP workers: running N / paused / stopped, the union of every live
session's own state), and `mp_mode` and `lp_mode` (running / desk / paused: each group's mode).

Per-session settings (the user, 2026-11-07: "when we set the max parallel task for HP, mp, and LP, i want it local to
a claude session, not a global settings"): `sessions/<session id>.json` beside the queue holds one Claude session's
limits (HP, MP, LP) and its own HP state. The session is CLAUDE_CODE_SESSION_ID, which Claude Code sets for every
command a session runs (`! wts` too); `--session ID` overrides it. config.json's `hp_parallel`, `mp_parallel` and
`lp_parallel` are only the defaults a new session starts with (HP 1, MP and LP no limit when absent); `limit` never
writes them. A session counts as live while its Claude process (CLAUDE_PID) runs. Two Claude processes can run one
conversation, so one session id (`claude --resume <id>` in one folder, then `claude --continue` in the folder where
the conversation began: its newest there): the first keeps `sessions/<id>.json`, a later one writes `<id>@<pid>`
(`<id>_<pid>.json` on disk, the name made safe).

A session's queue is its own (the user, 2026-11-22: "the wts should be local to each session"): the one at or above
the folder its Claude process runs in (~/.claude/sessions/<CLAUDE_PID>.json says which), as the `wts` command finds
it. A conversation continued in another folder still remembers its old project's --root; every command but
statusline (its root comes from the session's own folder) and awake (no queue) refuses a --root that is not this
session's own. --other-project reads another project's tables (wts, order, window only), as an onlooker: no
session of its own there.

Commands (run from the project root, or pass --root; with neither, the nearest queue above the current folder):
  queue.py add [--pri HP|MP|LP|AG] --mode append|push|override --text "..."   file a new task (HP by default; AG
                                                          records this session as the agent's owner)
  queue.py addendum N --text "..."                        append a dated addendum to task N's Task cell
  queue.py state N --text "..."                           set task N's State cell (the lead's own edits)
  queue.py accept N [N ...] --words "..."                 mark tasks accepted by the user
  queue.py close N [N ...] --why "..."                    Accepted n/a (<why>): withdrawn, cancelled, folded into M
  queue.py reopen N [N ...]                               undo an accept or a close: the row is back in its table
  queue.py pri N HP|MP|LP|AG                                move task N to another table (its number stays)
  queue.py worker running [N ...] | paused | stopped      this session's HP state (its own tasks, up to its HP limit);
                                                          the shared `state` file becomes the union of live sessions;
                                                          the running rows move to the top of their table, in that order
  queue.py top N [N ...]                                  move open rows to the top of their table, in the order given
  queue.py limit                                          this session's limits and where each comes from
  queue.py limit hp N [mp N] [lp N]                       set this session's limits (N >= 0, or "none" for no limit)
  queue.py limit reset [hp|mp|lp ...]                     back to the defaults in config.json
  queue.py mp running | desk | paused                     write the MP group's mode
  queue.py lp running | desk | paused                     write the LP group's mode
  queue.py wts [--all]                                    the window line, then the HP, MP, LP and AG tables
  queue.py wts hx [--all]                                 the closed tasks (HX, history; `wts history` too) (accepted or n/a), newest first: the last
                                                          20, or every one
  queue.py order                                          the pending run order, one line each
  queue.py window                                         who holds the shared window
  queue.py statusline                                     the HP part, then the MP, LP and AG parts (tab-separated)
  queue.py awake on | off | status                        keep the Mac awake while this session's agents work: each
                                                          Claude session holds its own and stops only its own (awake.py)

Placement (the user's words), within the task's own table:
  append / queue / "worker: ..."  at the tail of its table (HP: before the MP rows; MP: before the LP rows; LP:
                  before END).
  push            HP worker idle: at the top of the pending HP tasks; busy: just after the HP task in progress.
                  MP, LP: at the top of that table's pending tasks.
  push override   (HP) the task in progress is paused (its State says so) and the new task goes on top, to start now.
Only the State cell is a worker's; everything else in the queue is the lead's.
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import time
import subprocess
import sys

ROW = re.compile(r"^\| (\d+) \|")
PRIS = ("HP", "MP", "LP", "AG")
LIMITED = ("HP", "MP", "LP")            # AG has no limit: each of the lead's own agents is one the user asked for
LP_MODES = {"running": "⏩ running", "desk": "📝 desk-only", "paused": "✋ PAUSED"}   # the MP and LP groups' modes
LIMIT_KEYS = {"HP": "hp_parallel", "MP": "mp_parallel", "LP": "lp_parallel"}       # config.json: a new session's defaults


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def find_root(p):
    """The nearest folder at or above p whose .autopilot/worker holds a queue (as the `wts` command finds it), or None."""
    p = os.path.abspath(p)
    while True:
        if os.path.isfile(os.path.join(p, ".autopilot", "worker", "queue.md")):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up


def _registry(pid):
    """Claude Code's own entry for one of its processes (~/.claude/sessions/<pid>.json; never the .key files), or {}."""
    try:
        with open(os.path.join(os.path.expanduser("~/.claude/sessions"), f"{int(pid)}.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def session_home():
    """The folder this Claude session runs in (its process's registry entry), or None outside Claude Code."""
    pid = os.environ.get("CLAUDE_PID", "")
    cwd = _registry(pid).get("cwd") if pid.isdigit() else None
    return cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None


def foreign_root(root):
    """Why `root` is not this session's queue, or "" when it is (or no Claude session can say)."""
    home = session_home()
    if not home:
        return ""
    own = find_root(home)
    if own and os.path.realpath(own) == os.path.realpath(root):
        return ""
    return (f"{root} is not this session's project: this session runs in {home}, "
            + (f"whose queue is {own}." if own else "which has no worker queue.")
            + " A conversation resumed or continued in another folder still remembers its old project, but a session's"
              " queue is the one at or above its own folder. Tell the user so; pass --other-project only when the user"
              " asks for that project's table by name (wts, order and window only).")


def own_session(q, conv):
    """This process's name among the session files: the conversation's id, or <id>@<pid> while another live Claude
    process on the same conversation holds the id's file, so each keeps its own limits and HP state."""
    pid = os.environ.get("CLAUDE_PID", "")
    if not conv or not pid.isdigit():
        return conv
    alt = f"{conv}@{pid}"
    if os.path.exists(q._session_file(alt)):
        return alt
    other = q.read_session(conv).get("pid")
    if isinstance(other, int) and other != int(pid) and pid_alive(other) and _registry(other).get("sessionId") == conv:
        return alt
    return conv


def num_key(x):
    return (0, int(x), "") if str(x).isdigit() else (1, 0, str(x))


def now_hm():
    return dt.datetime.now().strftime("%H:%M")


def today():
    return dt.date.today().isoformat()


class Queue:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, ".autopilot", "worker")
        self.path = os.path.join(self.dir, "queue.md")
        cfg_path = os.path.join(self.dir, "config.json")
        self.cfg = {"lock": ".autopilot/run.lock", "app_process": "", "worker_model": "the worker", "pinned_tail": [999]}
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                self.cfg.update(json.load(f))
        with open(self.path) as f:
            self.lines = f.read().split("\n")
        self.titles_path = os.path.join(self.dir, "titles.json")
        try:
            with open(self.titles_path) as f:
                self.titles = json.load(f)
        except (OSError, ValueError):
            self.titles = {}
        self.reviewed = set(int(n) for n in self.titles.get("_reviewed", []))

    def mark_reviewed(self, ns):
        self.reviewed.update(ns)
        self.titles["_reviewed"] = sorted(self.reviewed)
        with open(self.titles_path, "w") as f:
            json.dump(self.titles, f, indent=1, ensure_ascii=False)

    def set_owner(self, n, sid):
        """The Claude session that started an AG row's agent (an agent lives in one lead session)."""
        self.titles.setdefault("_session", {})[str(n)] = sid
        with open(self.titles_path, "w") as f:
            json.dump(self.titles, f, indent=1, ensure_ascii=False)

    def owner(self, n):
        return (self.titles.get("_session") or {}).get(str(n), "")

    def set_title(self, n, text):
        self.titles[str(n)] = text.strip()
        with open(self.titles_path, "w") as f:
            json.dump(self.titles, f, indent=1, ensure_ascii=False)

    # --- rows -------------------------------------------------------------------------------------------------
    def rows(self):
        out = []
        for i, line in enumerate(self.lines):
            m = ROW.match(line)
            if not m:
                continue
            cells = line.split("|")
            # | n | pri | task ... | state | accepted |  ->  ['', n, pri, task..., state, accepted, '']
            pri = cells[2].strip() if len(cells) > 6 and cells[2].strip() in PRIS + ("—", "-") else ""
            first = 3 if pri else 2
            n = int(m.group(1))
            out.append({
                "i": i, "n": n, "pri": pri if pri in PRIS else ("—" if n in self.pinned() else "HP"),
                "task": "|".join(cells[first:-3]).strip(),
                "state": cells[-3].strip(), "acc": cells[-2].strip(),
            })
        return out

    def find(self, n):
        for r in self.rows():
            if r["n"] == n:
                return r
        sys.exit(f"no task {n} in {self.path}")

    def set_row(self, r, task=None, state=None, acc=None, pri=None):
        task = r["task"] if task is None else task
        state = r["state"] if state is None else state
        acc = r["acc"] if acc is None else acc
        pri = r["pri"] if pri is None else pri
        self.lines[r["i"]] = f"| {r['n']} | {pri} | {task} | {state} | {acc} |"

    def save(self):
        with open(self.path, "w") as f:
            f.write("\n".join(self.lines))

    # The State cell's first words name the row's kind. Agents write the rest in their own words; only the head counts.
    # A leading "redo" (a task sent back) is a marker, not a kind of its own: "redo", "redo: in progress 18:00",
    # "redo done 18:10" read as queued, active and done, and is_redo() lifts the open ones to the top.
    HEADS = (("done", "done"), ("in progress", "active"), ("in hand", "active"), ("paused", "paused"),
             ("parked", "parked"), ("waiting", "parked"), ("on hold", "parked"), ("hold", "parked"),
             ("needs the user", "asks"), ("queued", "queued"))

    @staticmethod
    def head(r):
        s = r["state"].lower().lstrip("⏩⏳✋✅⛔❓ \u2014-")  # a sign an agent typed into its State is not its first word
        redo = s.startswith("redo")
        if redo:
            s = s[4:].lstrip(" :;,-\u2014")
        return redo, s

    @staticmethod
    def kind(r):
        if "✔" in r["acc"] or r["acc"].lower().startswith("n/a"):
            return "closed"
        redo, s = Queue.head(r)
        k = next((k for h, k in Queue.HEADS if s.startswith(h)), "other")
        return "queued" if redo and k == "other" else k  # "redo" or "redo: <why>" waits its turn, at the top

    def pinned(self):
        return set(int(x) for x in self.cfg.get("pinned_tail", [999]))

    def next_number(self):
        # one numbering for both tables: every number in use counts (only END's high fixed number is left out)
        nums = [r["n"] for r in self.rows() if r["n"] < 900]
        return (max(nums) if nums else 0) + 1

    # --- placement --------------------------------------------------------------------------------------------
    def end_index(self):
        """Where the pinned tail starts (END), or after the last row."""
        rows = self.rows()
        for r in rows:
            if r["n"] in self.pinned():
                return r["i"]
        return rows[-1]["i"] + 1 if rows else len(self.lines)

    def tail_index(self, pri):
        """The tail of a table: before the first row of a lower table (HP before MP and LP, MP before LP), or END."""
        lower = PRIS[PRIS.index(pri) + 1:] if pri in PRIS else ()
        for r in self.rows():
            if r["pri"] in lower or r["n"] in self.pinned():
                return r["i"]
        return self.end_index()

    def top_index(self, pri):
        """The first pending row of a table (or its tail)."""
        for r in self.rows():
            if r["n"] in self.pinned():
                break
            if r["pri"] == pri and self.kind(r) in ("queued", "paused", "parked", "active"):
                return r["i"]
        return self.tail_index(pri)

    def active(self, pri="HP"):
        for r in self.rows():
            if r["pri"] == pri and self.kind(r) == "active":
                return r
        return None

    def add(self, mode, text, pri="HP"):
        n = self.next_number()
        act = self.active(pri)
        stamp = f"({today()} {now_hm()})"
        if mode == "append":
            where, label, state = self.tail_index(pri), f"Queued by the user {stamp}:", "queued"
        elif mode == "push":
            label = f"PUSHED by the user {stamp}:"
            where = act["i"] + 1 if (act and pri == "HP") else self.top_index(pri)
            state = "queued (pushed: next)"
        elif mode == "override":
            if pri != "HP":
                sys.exit("push override is for HP tasks; for MP or LP use push, or pause the group")
            label = f"PUSH OVERRIDE by the user {stamp}:"
            state = "queued (push override: start now)"
            if act:
                self.set_row(act, state=f"paused {now_hm()} by push override for {n} (was: {act['state']}); resume right after {n}")
                where = act["i"]
            else:
                where = self.top_index(pri)
        else:
            sys.exit("mode must be append, push or override")
        self.lines.insert(where, f"| {n} | {pri} | {label} {text.strip()} | {state} | — |")
        self.save()
        return n, act

    def move(self, n, pri):
        r = self.find(n)
        if r["n"] in self.pinned():
            sys.exit(f"{n} is pinned (END); it stays last")
        line = self.lines.pop(r["i"])
        r = dict(r, i=None)
        where = self.tail_index(pri) if self.kind(r) in ("queued", "other") else self.top_index(pri)
        self.lines.insert(where, line)
        r["i"] = where
        self.set_row(r, pri=pri)
        self.save()

    def lift(self, ns):
        """Put the named rows at the top of their table, in the order given (the user, 2026-11-07: "when we unpause 326,
        it should move to the top after 309"): the rows that run go first, so the table reads in run order. A pinned or
        closed row is left where it is. Returns the numbers moved."""
        moved = []
        for n in ns:
            r = self.find(n)
            if r["n"] in self.pinned() or self.kind(r) == "closed":
                continue
            line = self.lines.pop(r["i"])
            after = [x for x in self.rows() if x["pri"] == r["pri"] and x["n"] in moved]
            if after:
                where = after[-1]["i"] + 1          # right after the rows lifted before it
            else:
                tops = [x for x in self.rows() if x["pri"] == r["pri"] and x["n"] not in self.pinned()]
                where = tops[0]["i"] if tops else self.tail_index(r["pri"])
            self.lines.insert(where, line)
            moved.append(n)
        if moved:
            self.save()
        return moved

    def migrate(self):
        """An older queue (no Pri column): add the column, every row HP (END: —)."""
        changed = False
        for i, line in enumerate(self.lines):
            if line.startswith("| # | Task | State | Accepted |"):
                self.lines[i] = "| # | Pri | Task | State | Accepted |"
                changed = True
            elif line.startswith("|---|---|---|---|") and not line.startswith("|---|---|---|---|---|"):
                self.lines[i] = "|---|---|---|---|---|"
                changed = True
        for r in self.rows():
            cells = self.lines[r["i"]].split("|")
            if not (len(cells) > 6 and cells[2].strip() in PRIS + ("—", "-")):
                self.set_row(r)
                changed = True
        if changed:
            self.save()
        return changed

    # --- state files ------------------------------------------------------------------------------------------
    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text.strip() + "\n")

    def _read(self, name):
        try:
            with open(os.path.join(self.dir, name)) as f:
                return f.readline().strip()
        except OSError:
            return ""

    def write_state(self, text):
        self._write("state", text)

    def read_state(self):
        return self._read("state")

    def group_mode(self, pri):
        """The MP or LP group's mode (running / desk / paused); running when none is set."""
        m = self._read(f"{pri.lower()}_mode").split(" ")[0]
        return m if m in LP_MODES else "running"

    def lp_mode(self):
        return self.group_mode("LP")

    # --- per-session limits and HP state (the user, 2026-11-07) ------------------------------------------------
    def _session_file(self, sid):
        return os.path.join(self.dir, "sessions", re.sub(r"[^A-Za-z0-9_-]", "_", sid) + ".json")

    def read_session(self, sid):
        try:
            with open(self._session_file(sid)) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_session(self, sid, d):
        pid = os.environ.get("CLAUDE_PID")
        d["pid"] = int(pid) if pid and pid.isdigit() else None
        d["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        path = self._session_file(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, path)

    def sessions(self, me=""):
        """(id, data, live) for every session file; drops dead sessions' files after 14 days."""
        folder = os.path.join(self.dir, "sessions")
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
        except OSError:
            return []
        mine = os.path.basename(self._session_file(me)) if me else ""
        out = []
        for n in names:
            path = os.path.join(folder, n)
            try:
                with open(path) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            live = n == mine or (pid_alive(d["pid"]) if d.get("pid") else
                                 time.time() - os.path.getmtime(path) < 12 * 3600)
            if not live and time.time() - os.path.getmtime(path) > 14 * 86400:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            out.append((n[:-5], d, live))
        return out

    def default_limit(self, pri):
        if pri not in LIMIT_KEYS:
            return None
        v = self.cfg.get(LIMIT_KEYS[pri])
        if v is None:
            return 1 if pri == "HP" else None
        return int(v)

    def limit(self, pri, sid):
        """This session's limit for a table, and where it comes from ("this session" or "default")."""
        if pri not in LIMITED:
            return None, "no limit"
        lim = self.read_session(sid).get("limits", {}) if sid else {}
        if pri in lim:
            return lim[pri], "this session"
        return self.default_limit(pri), "default"

    def union_state(self, me):
        """The union of every live session's own HP state ("" when no session has written one)."""
        running, any_running, paused, seen = set(), False, False, False
        for sid, d, live in self.sessions(me):
            st = d.get("hp", "")
            if not live or not st:
                continue
            seen = True
            if st.startswith("running"):
                any_running = True
                running.update(st.split()[1:])
            elif st.startswith("paused"):
                paused = True
        if not seen:
            return ""
        return ("running" + (" " + " ".join(sorted(running, key=num_key)) if running else "")) if any_running else \
            ("paused" if paused else "stopped")

    def sync_state(self, me):
        """The shared `state` file = the union of every live session's own HP state."""
        text = self.union_state(me) or "stopped"
        self.write_state(text)
        return text

    def current_state(self):
        """What the HP workers are doing now: the live sessions' union, else the shared file (older writers)."""
        return self.union_state(getattr(self, "sid", "")) or self.read_state()

    def others_running(self, me):
        """HP tasks other live sessions run in this project."""
        mine = os.path.basename(self._session_file(me))[:-5] if me else ""
        out = set()
        for sid, d, live in self.sessions(me):
            if live and sid != mine and d.get("hp", "").startswith("running"):
                out.update(d["hp"].split()[1:])
        return sorted(out, key=num_key)

    # --- the shared window ------------------------------------------------------------------------------------
    def lock_paths(self):
        # "locks" (a list) since 2026-10-29: several window slots (e.g. unity.lock and unity2.lock); else "lock"
        # "lock": "" says the project has no shared window (a web tool tested in its own headless browser): no slots
        rels = self.cfg.get("locks") or [self.cfg.get("lock", ".autopilot/run.lock")]
        return [os.path.join(self.root, r) for r in rels if r]

    def lock_text(self):
        held = [t for t in (self.one_lock_text(l) for l in self.lock_paths()) if t]
        return " / ".join(held) if held else None

    def one_lock_text(self, lock):
        if os.path.isdir(lock):
            try:
                with open(os.path.join(lock, "who")) as f:
                    return f.read().strip() or "someone (an empty who file)"
            except OSError:  # whoever took the lock did not say who: never guess
                t = time.strftime("%H:%M", time.localtime(os.path.getmtime(lock)))
                return f"someone who left no name (since {t})"
        if os.path.isfile(lock):
            with open(lock) as f:
                return f.read().strip() or "held"
        return None

    def build_lock_text(self):
        rel = self.cfg.get("build_lock") or ""
        lock = os.path.join(self.root, rel) if rel else ""
        if not lock or not os.path.isdir(lock):
            return None
        try:
            with open(os.path.join(lock, "who")) as f:
                return f.read().strip() or "someone"
        except OSError:
            return "someone"

    def window(self):
        paths = self.lock_paths()
        if not paths:
            return ""   # no shared window: the wts heading and the status line leave it out
        if len(paths) == 1:
            who = self.lock_text()
            return f"🔴 Window: held by {who}" if who else "🟢 Window: free"
        # win0, win1 ... (0-based, the user 2026-10-29); 🔒 = reserved for the user's shows (<lock>.reserved)
        out = []
        for i, l in enumerate(paths):
            w, r = self.one_lock_text(l), os.path.isfile(l + ".reserved")
            name = f"win{i}" + ("🔒" if r else "")
            out.append(f"🔴 {name}: {w}" if w else (f"🔒 win{i} reserved" if r else f"🟢 win{i} free"))
        return " · ".join(out)


SIGN = {"active": "⏩", "queued": "⏳", "paused": "✋", "parked": "✋", "asks": "❓", "done": "✅", "other": "⏳"}


def runs_on(q, r):
    m = re.findall(r"on the ([A-Z][\w .]*?) model", r["task"])  # the last mention wins (an addendum may move it)
    return m[-1] if m else ("own agent" if r["pri"] == "AG" else q.cfg.get("worker_model", "the worker"))


def _load_awake():
    """awake.py beside this file (one hold per Claude session), loaded now so a host that imports queue.py with
    bytecode writing off (ccprobe) loads it the same way."""
    try:
        spec = importlib.util.spec_from_file_location("worker_awake", os.path.join(os.path.dirname(os.path.abspath(__file__)), "awake.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


awake = _load_awake()


def caffeinated(session=None):
    """True or False while a keep-awake can exist, None where it cannot.

    `caffeinate` is macOS-only.  On a machine without it -- Linux, or any
    server that does not sleep -- there is nothing to keep awake, and printing
    "caffeinate off" would be a status report about a feature that is not
    there.  None means "not applicable" and the callers leave the segment out,
    which is what `statusline.sh` already does with `command -v`.

    With `session` (a Claude pid): whether that session holds its own (since
    2026-11-08 each session holds one, see awake.py); without: whether any does.
    """
    if not shutil.which("caffeinate"):
        return None
    if awake:
        hs = awake.holds()
        return any(h["session"] == session for h in hs) if session else bool(hs)
    try:
        return subprocess.run(["pgrep", "-f", "^caffeinate -dims"], capture_output=True).returncode == 0
    except OSError:
        return False


def awake_holds():
    """{Claude session pid: its holds} (0: a hold no session owns); None where there is no caffeinate or no awake.py."""
    if not awake or not shutil.which("caffeinate"):
        return None
    out = {}
    for h in awake.holds():
        out[h["session"]] = out.get(h["session"], 0) + 1
    return out


def awake_text():
    """The wts line's part: this session's own hold, and whether others keep the Mac awake."""
    if not shutil.which("caffeinate"):
        return ""
    if not awake:
        return f" · caffeinate {'on' if caffeinated() else 'off'}"
    mine, others = awake.counts()
    return (" · caffeinate " + ("on" if mine else "off")
            + (f" (on in {others} other session{'s' if others != 1 else ''})" if others and not mine else ""))


def hp_has_work(q):
    # an open HP row (queued, in hand, paused, parked, asking the user, or in words of its own) is work left
    return any(r["pri"] == "HP" and r["n"] not in q.pinned() and q.kind(r) in ("asks", "active", "paused", "parked", "queued", "other")
               for r in q.rows())


def hp_line(q):
    st = q.current_state() or "unknown"
    if not st.startswith(("running", "paused")) and not hp_has_work(q):
        return "💤 idle"  # stopped with nothing to do reads as idle, not as a halt
    return {"running": "⏩ running", "paused": "✋ PAUSED", "stopped": "■ stopped"}.get(st.split(" ")[0], st) + \
        (f" {st.split(' ', 1)[1]}" if st.startswith("running ") else "")


def ag_line(q):
    """AG's legend, as the other tables': running while one of its agents works, else idle (it has no mode or limit)."""
    return "⏩ running" if any(r["pri"] == "AG" and q.kind(r) == "active" for r in q.rows()) else "💤 idle"


def short_status(q, r):
    """The State cell cut to its head (before the first ": "), with the lead's review and what waits on it."""
    k = q.kind(r)
    st = re.sub(r"\*\*|`", "", r["state"]).strip().lstrip("⏩⏳✋✅⛔❓ ")
    st = re.sub(r"\((pushed|push override)[^)]*\)", lambda m: "(" + m.group(1) + ")", st)   # "(pushed: next)" -> "(pushed)": the colon inside is not the head's end
    head = re.split(r":\s", st, maxsplit=1)[0].strip()
    head = re.sub(r"^HOLD", "on hold", head)
    head = re.sub(r"\s*\((?:LP|RESUMED|resumed|the user|PAUSE)[^)]*\)", "", head)   # the table already says these
    head = re.sub(r"\s+", " ", head).strip()
    if len(head) > 44:
        head = head[:41] + "..."
    extra = []
    sign = SIGN.get(k, '')
    if k == "done":
        reviewed = r["n"] in q.reviewed or "Lead review" in r["task"]
        if "fix owed" in st:
            extra.append("small fix owed")
        elif not reviewed:
            # the agent's work is done, the lead's review is not (the user, 2026-11-18: "only after the Lead's review, we
            # can mark an agent or an worker as done"): 🔍 "to review <time>", and ✅ only once the lead marks it reviewed
            sign = "🔍"
            head = re.sub(r"^done\b", "to review", head)
            if not head.startswith("to review"):
                extra.append("to review")
    if "waiting for the window" in st and k != "done" and "waiting for the window" not in head:
        extra.append("waits for the window")
    return f"{sign} {head}" + (f" · {', '.join(extra)}" if extra else "")


def title_of(q, r):
    t = q.titles.get(str(r["n"]))
    if t:
        return t
    task = re.sub(r"\*\*|`", "", r["task"])
    task = re.sub(r"^(Queued|PUSHED|PUSH OVERRIDE)[^(:]*(?:\([^)]*\))?:\s*", "", task)   # "(2026-11-14 20:37):": the time's colon is not the label's end
    return task if len(task) <= 60 else task[:57] + "..."


def is_redo(r):
    # a task sent back for a redo (State "redo: ...") goes to the top of its table and of the run order (the user, 2026-10-19)
    return Queue.head(r)[0] and Queue.kind(r) not in ("done", "closed")


def table_rows(q, rows, show_all):
    open_ = [r for r in rows if q.kind(r) in ("asks", "active", "paused", "parked", "queued", "other")]  # "other": a state in words of its own; never hide it
    open_.sort(key=lambda r: not is_redo(r))  # stable: redo rows first, the rest keep their order
    done = [r for r in rows if q.kind(r) == "done"]
    closed = [r for r in rows if q.kind(r) == "closed"] if show_all else []
    return [(str(r["n"]), title_of(q, r), runs_on(q, r), short_status(q, r), r["acc"] if r["acc"] not in ("", "-") else "—")
            for r in open_ + done + closed]


WIDE = set("⏩⏳✋✅⛔❓🟢🔴☕💤📝")


def width(s):
    return sum(2 if ch in WIDE else (0 if ch == "\ufe0f" else 1) for ch in s)


def table(q, rows, show_all, term=False):
    head = ("#", "Task", "Runs on", "Status", "Accepted")
    grid(head, table_rows(q, rows, show_all) or [("—", "(nothing open)", "", "", "")], term)


RE_ACC_OK = re.compile(r"✔\s*(\d{4}-\d\d-\d\d)?(?:\s+(\d\d:\d\d))?\s*(?:\((?:the user:\s*)?(.*)\))?\s*$", re.S)
RE_ACC_NA = re.compile(r"n/a\s*(?:\((.*)\))?\s*$", re.S | re.I)
RE_STAMP = re.compile(r"(?:^|,\s*)(\d{4}-\d\d-\d\d)(?:\s+(\d\d:\d\d))?\s*$")


def history_rows(q):
    """The closed tasks (accepted, or n/a: withdrawn, cancelled, folded into another), newest first (the user,
    2026-11-14: "any accepted, or aborted task goes to there"). Each: {n, pri, title, ok, date, time, why}: ok is True
    for an accept, why the user's words or the n/a reason. Ordered by the date and time the Accepted cell carries
    (a time since 2026-11-14), then by number; a row with no date (an older n/a) comes after every dated one."""
    out = []
    for r in q.rows():
        if r["n"] in q.pinned() or q.kind(r) != "closed":
            continue
        acc = r["acc"].strip()
        date = time_ = None
        if "✔" in acc:
            m = RE_ACC_OK.search(acc)
            date, time_, why = (m.group(1), m.group(2), (m.group(3) or "").strip()) if m else (None, None, "")
            why, ok = why.strip('"“” '), True
        else:
            m = RE_ACC_NA.match(acc)
            why, ok = ((m.group(1) or "") if m else acc).strip(), False
            st = RE_STAMP.search(why)
            if st:
                date, time_, why = st.group(1), st.group(2), why[:st.start()].strip(" ,")
        out.append({"n": r["n"], "pri": r["pri"], "title": title_of(q, r), "ok": ok, "date": date, "time": time_, "why": why})
    out.sort(key=lambda h: (h["date"] is not None, h["date"] or "", h["time"] or "", h["n"]), reverse=True)
    return out


def print_history(q, show_all, term=False):
    hist = history_rows(q)
    shown = hist if show_all else hist[:20]
    b = "" if term else "**"
    more = f" · the newest {len(shown)} (`wts hx --all`: every one)" if len(shown) < len(hist) else ""
    print(f"{b}HX{b} · history · {len(hist)} closed{more}")
    print()
    def outcome(h):
        why = h["why"] if len(h["why"]) <= 40 else h["why"][:37] + "..."
        return (f'✔ "{why}"' if why else "✔") if h["ok"] else (f"n/a: {why}" if why else "n/a")
    body = [(str(h["n"]), h["pri"], h["title"], " ".join(x for x in (h["date"], h["time"]) if x) or "—", outcome(h)) for h in shown]
    grid(("#", "Pri", "Task", "Closed", "Outcome"), body or [("—", "", "(nothing closed yet)", "", "")], term)


def grid(head, body, term=False):
    if not term:
        print("| " + " | ".join(head) + " |")
        print("|---|---|---|---|---|")
        for row in body:
            print("| " + " | ".join(row) + " |")
        return
    # a boxed table, as Claude Code draws a markdown table
    cols = list(zip(head, *body))
    w = [max(width(c) for c in col) for col in cols]
    def rule(l, m, r):
        return l + m.join("─" * (x + 2) for x in w) + r
    def line(cells):
        return "│" + "│".join(" " + c + " " * (w[i] - width(c)) + " " for i, c in enumerate(cells)) + "│"
    print(rule("┌", "┬", "┐"))
    print(line(head))
    print(rule("├", "┼", "┤"))
    for i, row in enumerate(body):
        print(line(row))
        if i < len(body) - 1:
            print(rule("├", "┼", "┤"))
    print(rule("└", "┴", "┘"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="the project (default: the nearest queue at or above the current folder)")
    ap.add_argument("--session", default=None, help="the Claude session (default: CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--other-project", action="store_true",
                    help="read another project's tables (wts, order, window) when the user asks for it by name")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--mode", required=True, choices=["append", "push", "override"]); a.add_argument("--text", required=True)
    a.add_argument("--pri", default="HP", choices=PRIS)
    a.add_argument("--title", help="a short name for wts (a few words)")
    rv = sub.add_parser("reviewed"); rv.add_argument("n", type=int, nargs="+")
    ti = sub.add_parser("title"); ti.add_argument("n", type=int); ti.add_argument("--text", required=True)
    b = sub.add_parser("addendum"); b.add_argument("n", type=int); b.add_argument("--text", required=True)
    s = sub.add_parser("state"); s.add_argument("n", type=int); s.add_argument("--text", required=True)
    c = sub.add_parser("accept"); c.add_argument("n", type=int, nargs="+"); c.add_argument("--words", required=True)
    cl = sub.add_parser("close"); cl.add_argument("n", type=int, nargs="+"); cl.add_argument("--why", required=True)
    ro = sub.add_parser("reopen"); ro.add_argument("n", type=int, nargs="+")
    p = sub.add_parser("pri"); p.add_argument("n", type=int); p.add_argument("pri", choices=PRIS)
    tp = sub.add_parser("top"); tp.add_argument("n", type=int, nargs="+")
    w = sub.add_parser("worker"); w.add_argument("what", choices=["running", "paused", "stopped"]); w.add_argument("n", nargs="*")
    lm = sub.add_parser("limit"); lm.add_argument("pairs", nargs="*", help="hp N [mp N] [lp N], or reset [hp|mp|lp ...]")
    l = sub.add_parser("lp"); l.add_argument("mode", choices=list(LP_MODES))
    mp = sub.add_parser("mp"); mp.add_argument("mode", choices=list(LP_MODES))
    t = sub.add_parser("wts"); t.add_argument("--all", action="store_true")
    t.add_argument("only", nargs="*", type=str.upper, choices=list(PRIS) + ["HX", "HISTORY"],
                   help="show only these tables (wts hp, wts mp lp); hx (or history): the closed tasks, newest first")
    t.add_argument("--term", action="store_true", help="aligned plain text for a terminal (the `wts` shell command)")
    sub.add_parser("order")
    sub.add_parser("window")
    sub.add_parser("statusline")
    sub.add_parser("migrate")
    aw = sub.add_parser("awake"); aw.add_argument("what", nargs="?", default="status", choices=["on", "off", "status"])
    args = ap.parse_args()
    if args.cmd == "awake":   # needs no queue: a session holds its keep-awake whatever the folder
        if not awake:
            sys.exit("awake.py is missing beside queue.py")
        print({"on": awake.on, "off": awake.off, "status": awake.status}[args.what]())
        return
    if getattr(args, "text", None):  # a table row is one line: flatten line breaks and pipes
        args.text = " ".join(args.text.replace("|", "/").split())
    args.root = args.root or find_root(os.getcwd()) or os.getcwd()
    if args.other_project:
        if args.cmd not in ("wts", "order", "window"):
            sys.exit("--other-project only reads another project's tables: wts, order, window")
    elif args.cmd != "statusline":   # the status line finds its root from the session's own folder
        why = foreign_root(args.root)
        if why:
            sys.exit(why)
    q = Queue(args.root)
    conv = "" if args.other_project else args.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    sid = conv if args.session else own_session(q, conv)
    q.sid = sid

    if args.cmd == "add":
        n, act = q.add(args.mode, args.text, args.pri)
        if args.title:
            q.set_title(n, args.title)
        if args.pri == "AG" and conv:
            q.set_owner(n, conv)   # the conversation's id: an agent lives in its conversation
        print(f"filed task {n} ({args.pri}, {args.mode})" + (f"; task {act['n']} paused for it" if args.mode == "override" and act else ""))
        print_order(q)
    elif args.cmd == "reviewed":
        q.mark_reviewed(args.n); print("reviewed by the lead: " + ", ".join(map(str, args.n)))
    elif args.cmd == "title":
        q.find(args.n); q.set_title(args.n, args.text); print(f"title of {args.n}: {args.text}")
    elif args.cmd == "addendum":
        r = q.find(args.n)
        q.set_row(r, task=r["task"].rstrip() + f" **Addendum (the user, {today()} {now_hm()}):** {args.text.strip()}")
        q.save(); print(f"addendum added to {args.n}")
    elif args.cmd == "state":
        q.set_row(q.find(args.n), state=args.text.strip()); q.save(); print(f"state of {args.n} set")
        if Queue.kind({"state": args.text, "acc": ""}) == "other":  # the table cannot sort a State it does not know: say so at once
            print("warning: the State does not start with a known word, so wts shows it as ⏳ and cannot place it; start it with "
                  + ", ".join(f'"{h}"' for h, _ in Queue.HEADS) + ' (optionally after "redo")')
    elif args.cmd == "accept":
        for n in args.n:
            q.set_row(q.find(n), acc=f'✔ {today()} {now_hm()} (the user: "{args.words}")')   # the time orders History (2026-11-14)
        q.save(); print("accepted: " + ", ".join(map(str, args.n)))
        # config "on_accept": a command run with the accepted numbers (e.g. 2026-10-30: clean the task's scratch
        # capture frames once the user accepts it; the user: "yes, do both")
        hook = q.cfg.get("on_accept")
        if hook:
            import shlex, subprocess
            r = subprocess.run(shlex.split(hook) + [str(n) for n in args.n], cwd=q.root, capture_output=True, text=True)
            print((r.stdout or r.stderr).strip())
    elif args.cmd == "close":
        why = " ".join(args.why.replace("|", "/").split())
        for n in args.n:
            q.set_row(q.find(n), acc=f"n/a ({why}, {today()} {now_hm()})")
        q.save(); print(f"closed (n/a, {why}): " + ", ".join(map(str, args.n)))
    elif args.cmd == "reopen":
        # the user, 2026-11-14: a History of closed tasks makes a mistaken accept easy to spot; this undoes it
        done, left = [], []
        for n in args.n:
            r = q.find(n)
            if q.kind(r) != "closed":
                left.append(n)
                continue
            q.set_row(r, acc="—")
            done.append(f"{n} ({r['pri']}: {short_status(q, dict(r, acc='—')).strip()})")
        if done:
            q.save()
        print("reopened, back in their tables: " + (", ".join(done) or "none")
              + (f"; not closed, left as they are: {', '.join(map(str, left))}" if left else ""))
    elif args.cmd == "pri":
        q.move(args.n, args.pri); print(f"{args.n} is now {args.pri}"); print_order(q)
    elif args.cmd == "top":
        moved = q.lift(args.n)
        print("moved to the top of their table: " + (", ".join(map(str, moved)) or "none"))
    elif args.cmd == "worker":
        text = args.what + (" " + " ".join(args.n) if args.n else "")
        if args.what == "running" and args.n:
            # the running rows lead their table, in the order given (2026-11-07)
            q.lift([int(x) for x in args.n if str(x).isdigit()])
        if not sid:   # no Claude session (a plain terminal): the shared file only, as before 2026-11-07
            q.write_state(text); print("HP worker state: " + q.read_state())
        else:
            d = q.read_session(sid); d["hp"] = text; q.write_session(sid, d)
            shared = q.sync_state(sid)
            print("HP worker state (this session): " + text + ("" if shared == text else f"; all sessions: {shared}"))
            lim, src = q.limit("HP", sid)
            if args.what == "running" and lim is not None and len(args.n) > lim:
                print(f"warning: {len(args.n)} running tasks, over this session's HP limit of {lim} ({src})")
    elif args.cmd == "limit":
        words = [w.lower() for w in args.pairs]
        if words and not sid:
            sys.exit("no Claude session: CLAUDE_CODE_SESSION_ID is not set; pass --session ID")
        d = q.read_session(sid) if sid else {}
        lim = d.setdefault("limits", {})
        if words[:1] == ["reset"]:
            for pri in ([w.upper() for w in words[1:]] or list(LIMITED)):
                if pri not in LIMITED:
                    sys.exit(f"unknown table {pri.lower()}: use hp, mp or lp")
                lim.pop(pri, None)
            q.write_session(sid, d)
        elif words:
            if len(words) % 2:
                sys.exit("give pairs: hp N [mp N] [lp N]")
            for pri, val in zip(words[::2], words[1::2]):
                pri = pri.upper()
                if pri not in LIMITED:
                    sys.exit(f"unknown table {pri.lower()}: use hp, mp or lp")
                if val in ("none", "-", "unlimited"):
                    lim[pri] = None
                elif val.isdigit():
                    lim[pri] = int(val)
                else:
                    sys.exit(f"{pri} limit must be a whole number >= 0, or none")
            q.write_session(sid, d)
        def show(pri):
            n, src = q.limit(pri, sid)
            return f"{pri} {'no limit' if n is None else n} ({src})"
        print(f"limits for {'session ' + sid[:8] if sid else 'no session (defaults)'}: " + " · ".join(show(p) for p in LIMITED))
    elif args.cmd in ("lp", "mp"):
        q._write(f"{args.cmd}_mode", f"{args.mode} {today()} {now_hm()}"); print(f"{args.cmd.upper()} mode: " + LP_MODES[args.mode])
    elif args.cmd == "window":
        print(q.window() or "No shared window (config.json: \"lock\": \"\")")
    elif args.cmd == "order":
        print_order(q)
    elif args.cmd == "migrate":
        print("migrated" if q.migrate() else "already has the Pri column")
    elif args.cmd == "statusline":
        # Running parts in bright white (the user, 2026-11-07: "when there is something running e.g. "HP: 49" now, we
        # should make them highlight (the brighter white color, just like the current line)"). 97 is bright white; 0
        # resets to the normal color, the one the model name has (2026-11-07: restoring dim made the rest "too dark").
        def hi(t):
            return f"\033[97m{t}\033[0m"
        rows = [r for r in q.rows() if r["n"] not in q.pinned()]
        hp_paused = [str(r["n"]) for r in rows if r["pri"] == "HP" and q.kind(r) == "paused"]
        st = q.current_state()
        doing = ""
        who = q.lock_text() or ""
        if not re.search(r"\b(build|capture)", who):
            who = q.build_lock_text() or who   # a build beside the app holds only the build lock
        verb = next((v for v in ("build", "capture") if re.search(rf"\b{v}", who)), "")
        if st.startswith("running") and who.lower().startswith(("worker", "hp")) and verb:
            doing = " (building)" if verb == "build" else " (capturing)"
        if st.startswith("running"):
            # 2026-10-29, the user: "use HP 202/184/191. No need to show the icon, since everything shown there is already in progress"
            hp = f"HP: {'/'.join(st.split()[1:]) if ' ' in st else 'running'}{doing}"   # the user: "show HP: 202/184/..."
        elif st.startswith("paused"):
            hp = "HP ✋ PAUSED" + (f" at {'/'.join(hp_paused)}" if hp_paused else "")   # 2026-10-28, the user: put a "/" between the numbers (no space)
        elif st.startswith("stopped") and hp_has_work(q):
            hp = "HP ■ stopped"
        else:
            hp = "HP 💤 idle"
        def group(pri):
            open_ = [r for r in rows if r["pri"] == pri and q.kind(r) in ("active", "paused", "parked", "queued", "other")]
            if not open_:
                return ""
            mode = q.group_mode(pri) if pri in ("MP", "LP") else "running"
            sign = {"running": "", "desk": "📝 desk ", "paused": "✋ PAUSED "}[mode]   # running: no sign (2026-10-29, as HP)
            names = "/".join(str(r["n"]) + ("✋" if q.kind(r) == "paused" and mode != "paused" else "") for r in open_)
            txt = f"{pri}: {sign}{names}"
            return hi(txt) if mode == "running" and any(q.kind(r) == "active" for r in open_) else txt
        # MP and AG show only while they have open tasks; LP always (LP none)
        mp, lp, ag = group("MP"), group("LP") or "LP none", group("AG")
        print(f"{hi(hp) if st.startswith('running') else hp}\t{mp + '  ' if mp else ''}{lp}{'  ' + ag if ag else ''}")
    elif args.cmd == "wts":
        rows = [r for r in q.rows()]
        pinned = q.pinned()
        b = "" if args.term else "**"
        only = ["HX" if x == "HISTORY" else x for x in args.only] or list(PRIS)   # HX: history's shorthand (2026-11-14)
        if only == ["HX"]:
            print_history(q, args.all, args.term)
            return
        print((q.window() + awake_text()).lstrip(" ·") or "No shared window")
        print()
        for k, pri in enumerate(p for p in PRIS if p in only):
            if k:
                print()
            n, src = q.limit(pri, sid)
            cap = f" · max {'none' if n is None else n} ({src if sid else 'default; no session'})" if pri in LIMITED else ""   # per session (2026-11-07)
            others = q.others_running(sid) if pri == "HP" and sid else []
            print(f"{b}{pri}{b} · " + (f"the worker: {hp_line(q)}" if pri == "HP" else ag_line(q) if pri == "AG" else LP_MODES[q.group_mode(pri)]) + cap
                  + (f" · other sessions run {', '.join(others)}" if others else ""))
            print()
            table(q, [r for r in rows if r["pri"] == pri and r["n"] not in pinned], args.all, args.term)
        if "HX" in only:
            print()
            print_history(q, args.all, args.term)
        elif not args.only:
            n_closed = len(history_rows(q))
            if n_closed:
                print()
                print(f"HX (history): {n_closed} closed (`wts hx`)")
        for r in rows:
            if r["n"] in pinned and not args.only:
                print()
                tail = (" Then the lead runs `queue.py awake off`."
                        if caffeinated() is not None else "")
                print(f"{r['n']} END: when the HP, MP and LP tables are empty, \"queue empty\" (AG agents don't count).{tail}")


def print_order(q):
    pinned = q.pinned()
    print("run order:")
    for pri in PRIS:
        for r in sorted(q.rows(), key=lambda r: not is_redo(r)):  # redo rows run first
            k = "redo" if is_redo(r) else q.kind(r)
            if r["pri"] == pri and r["n"] not in pinned and k in ("redo", "active", "paused", "parked", "queued"):
                print(f"  {pri} {r['n']:>4}  {SIGN.get(k, '·')} {k:<7} {re.sub(r'[*`]', '', r['task'])[:70]}")
    for r in q.rows():
        if r["n"] in pinned:
            print(f"     {r['n']:>4}  · {re.sub(r'[*`]', '', r['task'])[:70]}")


if __name__ == "__main__":
    main()
