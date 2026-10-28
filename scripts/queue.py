#!/usr/bin/env python3
"""The worker queue: the lead's tool for the background-worker skill.

The queue is a markdown table in <project>/.autopilot/worker/queue.md, one row per task, in run order:

    | # | Pri | Task | State | Accepted |
    |---|---|---|---|---|
    | 41 | HP | <the task, the user's words in quotes, addenda> | done 14:20 (`notes.md`) | ✔ 2026-10-17 (the user: "...") |

Three tables in one file. **HP** (high priority): the short tasks the background worker does one at a time and the
user checks one by one. **MP** (medium priority): mid-sized tasks, each on its own agent, ranked between the two: they
start before LP tasks and come before them for the shared window. **LP** (low priority): long tasks (a large rebuild),
each on its own agent. MP and LP each have a group mode (running / desk-only / paused) the user sets for the whole
table. One numbering for all: a task keeps its number when it moves between tables. The file keeps the HP rows first,
then MP, then LP, then the pinned tail (999, END, last). Rows without a Pri cell (an older queue) count as HP.

Settings live in <project>/.autopilot/worker/config.json (see templates/config.json): the shared-window lock, the app's
process name, the worker's model, and the pinned tail (e.g. [999]).
State files beside the queue: `state` (the HP worker: running N / paused / stopped), and `mp_mode` and `lp_mode`
(running / desk / paused: each group's mode).

Commands (run from the project root, or pass --root):
  queue.py add [--pri HP|MP|LP] --mode append|push|override --text "..."   file a new task (HP by default)
  queue.py addendum N --text "..."                        append a dated addendum to task N's Task cell
  queue.py state N --text "..."                           set task N's State cell (the lead's own edits)
  queue.py accept N [N ...] --words "..."                 mark tasks accepted by the user
  queue.py pri N HP|MP|LP                                 move task N to another table (its number stays)
  queue.py worker running [N ...] | paused | stopped      write the HP workers' state file (up to hp_parallel tasks)
  queue.py mp running | desk | paused                     write the MP group's mode
  queue.py lp running | desk | paused                     write the LP group's mode
  queue.py wts [--all]                                    the window line, then the HP, MP and LP tables
  queue.py order                                          the pending run order, one line each
  queue.py window                                         who holds the shared window
  queue.py statusline                                     the HP part, then the MP and LP parts (tab-separated)

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
import json
import os
import re
import time
import subprocess
import sys

ROW = re.compile(r"^\| (\d+) \|")
PRIS = ("HP", "MP", "LP")
LP_MODES = {"running": "⏩ running", "desk": "📝 desk-only", "paused": "✋ PAUSED"}   # the MP and LP groups' modes


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

    # --- the shared window ------------------------------------------------------------------------------------
    def lock_text(self):
        lock = os.path.join(self.root, self.cfg.get("lock", ".autopilot/run.lock"))
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
        who = self.lock_text()
        return f"🔴 Window: held by {who}" if who else "🟢 Window: free"


SIGN = {"active": "⏩", "queued": "⏳", "paused": "✋", "parked": "✋", "asks": "❓", "done": "✅", "other": "⏳"}


def runs_on(q, r):
    m = re.findall(r"on the ([A-Z][\w .]*?) model", r["task"])  # the last mention wins (an addendum may move it)
    return m[-1] if m else q.cfg.get("worker_model", "the worker")


def caffeinated():
    try:
        return subprocess.run(["pgrep", "-f", "^caffeinate -dims"], capture_output=True).returncode == 0
    except OSError:
        return False


def hp_has_work(q):
    # an open HP row (queued, in hand, paused, parked, asking the user, or in words of its own) is work left
    return any(r["pri"] == "HP" and r["n"] not in q.pinned() and q.kind(r) in ("asks", "active", "paused", "parked", "queued", "other")
               for r in q.rows())


def hp_line(q):
    st = q.read_state() or "unknown"
    if not st.startswith(("running", "paused")) and not hp_has_work(q):
        return "💤 idle"  # stopped with nothing to do reads as idle, not as a halt
    return {"running": "⏩ running", "paused": "✋ PAUSED", "stopped": "■ stopped"}.get(st.split(" ")[0], st) + \
        (f" {st.split(' ', 1)[1]}" if st.startswith("running ") else "")


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
    if k == "done":
        reviewed = r["n"] in q.reviewed or "Lead review" in r["task"]
        if "fix owed" in st:
            extra.append("small fix owed")
        elif not reviewed:          # a reviewed done row says nothing more: that is the normal case
            extra.append("to review")
    if "waiting for the window" in st and k != "done" and "waiting for the window" not in head:
        extra.append("waits for the window")
    return f"{SIGN.get(k, '')} {head}" + (f" · {', '.join(extra)}" if extra else "")


def title_of(q, r):
    t = q.titles.get(str(r["n"]))
    if t:
        return t
    task = re.sub(r"\*\*|`", "", r["task"])
    task = re.sub(r"^(Queued|PUSHED|PUSH OVERRIDE)[^:]*:\s*", "", task)
    return task if len(task) <= 60 else task[:57] + "..."


def is_redo(r):
    # a task sent back for a redo (State "redo: ...") goes to the top of its table and of the run order
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
    body = table_rows(q, rows, show_all) or [("—", "(nothing open)", "", "", "")]
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
    ap.add_argument("--root", default=os.getcwd())
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--mode", required=True, choices=["append", "push", "override"]); a.add_argument("--text", required=True)
    a.add_argument("--pri", default="HP", choices=PRIS)
    a.add_argument("--title", help="a short name for wts (a few words)")
    rv = sub.add_parser("reviewed"); rv.add_argument("n", type=int, nargs="+")
    ti = sub.add_parser("title"); ti.add_argument("n", type=int); ti.add_argument("--text", required=True)
    b = sub.add_parser("addendum"); b.add_argument("n", type=int); b.add_argument("--text", required=True)
    s = sub.add_parser("state"); s.add_argument("n", type=int); s.add_argument("--text", required=True)
    c = sub.add_parser("accept"); c.add_argument("n", type=int, nargs="+"); c.add_argument("--words", required=True)
    p = sub.add_parser("pri"); p.add_argument("n", type=int); p.add_argument("pri", choices=PRIS)
    w = sub.add_parser("worker"); w.add_argument("what", choices=["running", "paused", "stopped"]); w.add_argument("n", nargs="*")
    l = sub.add_parser("lp"); l.add_argument("mode", choices=list(LP_MODES))
    mp = sub.add_parser("mp"); mp.add_argument("mode", choices=list(LP_MODES))
    t = sub.add_parser("wts"); t.add_argument("--all", action="store_true")
    t.add_argument("only", nargs="*", type=str.upper, choices=list(PRIS), help="show only these tables (wts hp, wts mp lp)")
    t.add_argument("--term", action="store_true", help="aligned plain text for a terminal (the `wts` shell command)")
    sub.add_parser("order")
    sub.add_parser("window")
    sub.add_parser("statusline")
    sub.add_parser("migrate")
    args = ap.parse_args()
    if getattr(args, "text", None):  # a table row is one line: flatten line breaks and pipes
        args.text = " ".join(args.text.replace("|", "/").split())
    q = Queue(args.root)

    if args.cmd == "add":
        n, act = q.add(args.mode, args.text, args.pri)
        if args.title:
            q.set_title(n, args.title)
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
            q.set_row(q.find(n), acc=f'✔ {today()} (the user: "{args.words}")')
        q.save(); print("accepted: " + ", ".join(map(str, args.n)))
    elif args.cmd == "pri":
        q.move(args.n, args.pri); print(f"{args.n} is now {args.pri}"); print_order(q)
    elif args.cmd == "worker":
        q.write_state(args.what + (" " + " ".join(args.n) if args.n else "")); print("HP worker state: " + q.read_state())
    elif args.cmd in ("lp", "mp"):
        q._write(f"{args.cmd}_mode", f"{args.mode} {today()} {now_hm()}"); print(f"{args.cmd.upper()} mode: " + LP_MODES[args.mode])
    elif args.cmd == "window":
        print(q.window())
    elif args.cmd == "order":
        print_order(q)
    elif args.cmd == "migrate":
        print("migrated" if q.migrate() else "already has the Pri column")
    elif args.cmd == "statusline":
        rows = [r for r in q.rows() if r["n"] not in q.pinned()]
        hp_paused = [str(r["n"]) for r in rows if r["pri"] == "HP" and q.kind(r) == "paused"]
        st = q.read_state()
        doing = ""
        who = q.lock_text() or ""
        if not re.search(r"\b(build|capture)", who):
            who = q.build_lock_text() or who   # a build beside the app holds only the build lock
        verb = next((v for v in ("build", "capture") if re.search(rf"\b{v}", who)), "")
        if st.startswith("running") and who.lower().startswith(("worker", "hp")) and verb:
            doing = " (building)" if verb == "build" else " (capturing)"
        if st.startswith("running"):
            hp = f"HP ⏩ {st.split(' ', 1)[1] if ' ' in st else 'running'}{doing}"
        elif st.startswith("paused"):
            hp = "HP ✋ PAUSED" + (f" at {' '.join(hp_paused)}" if hp_paused else "")
        elif st.startswith("stopped") and hp_has_work(q):
            hp = "HP ■ stopped"
        else:
            hp = "HP 💤 idle"
        def group(pri):
            open_ = [r for r in rows if r["pri"] == pri and q.kind(r) in ("active", "paused", "parked", "queued", "other")]
            if not open_:
                return ""
            mode = q.group_mode(pri)
            sign = {"running": "⏩", "desk": "📝 desk", "paused": "✋ PAUSED"}[mode]
            names = " ".join(str(r["n"]) + ("✋" if q.kind(r) == "paused" and mode != "paused" else "") for r in open_)
            return f"{pri} {sign} {names}"
        # MP shows only while it has open tasks; LP always (LP none)
        mp, lp = group("MP"), group("LP") or "LP none"
        print(f"{hp}\t{mp + '  ' if mp else ''}{lp}")
    elif args.cmd == "wts":
        rows = [r for r in q.rows()]
        pinned = q.pinned()
        print(q.window() + f" · caffeinate {'on' if caffeinated() else 'off'}")
        print()
        b = "" if args.term else "**"
        only = args.only or list(PRIS)
        for k, pri in enumerate(p for p in PRIS if p in only):
            if k:
                print()
            print(f"{b}{pri}{b} · " + (f"the worker: {hp_line(q)}" if pri == "HP" else LP_MODES[q.group_mode(pri)]))
            print()
            table(q, [r for r in rows if r["pri"] == pri and r["n"] not in pinned], args.all, args.term)
        for r in rows:
            if r["n"] in pinned and not args.only:
                print()
                print(f"{r['n']} END: when all three tables are empty, \"queue empty\", then the lead stops caffeinate.")


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
