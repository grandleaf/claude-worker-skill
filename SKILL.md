---
name: worker
description: >
  Run a background worker (a fresh agent for each task) that does the user's queued jobs one at a time, while this main session
  (the lead) talks to the user, files and orders tasks, reviews every finished task, and shows results. Use when the
  user says "worker: <task>", "append/queue <task>", "push <task>", "push override <task>", "wts" (worker status
  table), "pause the worker", "resume", "accept N" / "N accepted", "LP <task>", "pause LP", "resume LP", "LP
  desk-only", or asks to set up a background worker, a task queue, or the worker status line.
user-invocable: true
---

# The background worker

You are the **lead**. The user talks only to you. You keep a queue of numbered tasks, brief a background **worker**
agent, review what it hands back, and show results to the user. The worker does the tasks, one at a time, in queue
order, and never talks to the user.

Files (per project; `<root>` is the project root):

| File | What |
|---|---|
| `<root>/.autopilot/worker/queue.md` | The queue: rules at the top, then one table row per task, in run order |
| `<root>/.autopilot/worker/config.json` | `lock`, `locks` (optional: several window slots, a list; shown per slot by `wts` and the status line; e.g. `unity.lock` and `unity2.lock`), `build_lock` (optional: a build that runs beside the app holds this, not `lock`), `app_process`, `worker_model`, `pinned_tail`, `brief` |
| `<root>/.autopilot/worker/state` | The HP worker's state, one line: `running N` / `paused` / `stopped` (shown as 💤 idle when no HP row is open) |
| `<root>/.autopilot/worker/lp_mode` | The LP group's mode: `running` / `desk` / `paused` |
| `<root>/.autopilot/worker/<task>.md` | The worker's notes, one file per task |
| `<root>/tools/prompts/worker.md` (or `brief`) | The worker's standing brief |
| `~/.claude/skills/worker/scripts/queue.py` | Your tool for every queue edit (see `--help`) |
| `~/.claude/skills/worker/scripts/awake.py` | This session's keep-awake (`Q awake on`, `Q awake off`; section 2) |
| `~/.claude/skills/worker/scripts/plan_usage.py` | The plan's per-model weekly limits ("Fable 40%" on the status line, a bar in ccprobe): the status line runs it; `show` prints the saved rows |
| `~/.claude/skills/worker/scripts/statusline.sh` | The status line |

Run the tool as `Q() { python3 ~/.claude/skills/worker/scripts/queue.py --root <root> "$@"; }`.

## 1. Setup (first use in a project)

If `<root>/.autopilot/worker/queue.md` does not exist:
1. Copy `templates/queue.md` and `templates/config.json` into `<root>/.autopilot/worker/`; fill in `config.json`
   (the shared-window lock path, or `"lock": ""` for a project with no shared window, such as a web tool tested in
   its own headless browser, so the status line and `wts` leave the window out; the app's process name, the worker's model; `pinned_tail` lists task numbers that
   always stay last, at least `999`).
2. Write the worker's brief from `templates/worker-brief.md`: fill in the repo, the user's standing rules, and the
   project's build, test, run and capture commands, each wrapped in the lock. Ask the user only for what you cannot
   find in the project.
3. The status line: if `~/.claude/settings.json` has no `statusLine`, ask the user, then set
   `{"statusLine": {"type": "command", "command": "~/.claude/skills/worker/scripts/statusline.sh", "padding": 0}}`.
   (If one already exists, offer to point it at this script.) Test it:
   `echo '{"workspace":{"current_dir":"<root>"}}' | ~/.claude/skills/worker/scripts/statusline.sh`.
4. Save the user's standing rules for this way of working in memory if the project uses memory (one line each).
5. Add `.autopilot/` to `.gitignore` if the user does not want it tracked.

**Start the worker** with the Agent tool: `run_in_background`, `name: "hp<N>"` (e.g. `hp154`), `description: "HP <N>:
<a few words of its title>"`, `model` = the worker's model (default `opus`), prompt: "You are the background worker.
Read `<brief>` once, then the memory index. Then do task N from `.autopilot/worker/queue.md`, report, and stop. Every
wait loop needs a timeout and a failure case; before you report, TaskStop every background job and Monitor of yours."
Give an advisor's prompt the same two sentences.
Name every agent by its table and task (the user, 2026-10-22: "should we show hp154?"): `hp154`, `mp111`, `lp74`, so
the status bar says which task each agent is on. A worker lives for one task, so the name never goes stale. Later
messages go by `SendMessage` to that name; tell an advisor the name of the worker it works with. A message reaches
it at its next step; if it had finished, the message resumes it with its context. The worker cannot spawn agents.

**Up to this session's HP limit at once (the user, 2026-10-25: "set max paralle task in HP to be 2"; 2026-10-27: "set HP's maximum paralle task to be 5").**
**Limits are per Claude session** (the user, 2026-11-07: "when we set the max parallel task for HP, mp, and LP, i want
it local to a claude session, not a global settings"). `Q limit` shows this session's HP, MP and LP limits and where
each comes from; `Q limit hp 2 mp 0` sets them for this session only, `Q limit reset [hp|mp|lp]` goes back to the
defaults. config.json's `hp_parallel`, `mp_parallel` and `lp_parallel` are only the defaults a new session starts
with (HP 1, MP and LP no limit when absent): never edit them for a user's "set the max parallel to N". The session is
`CLAUDE_CODE_SESSION_ID`, set by Claude Code for every command; `wts` shows each limit in its table's heading.
Whenever fewer HP workers run than this session's HP limit, start the next queued HP task in its own `hp<N>` worker.
**Strict top N (the user, 2026-10-29: "Just do top N, so I can easily tracking them"):** the open HP tasks run in
table order, the top N rows (N = this session's HP limit); never skip a row because it is blocked (a build, the licence) or shares a
file with a running task. A blocked task keeps its slot, does what it can and waits ("waiting for a build: …"); two
tasks on one file are each told the other's entries. A slot frees only when a task is done. Record this session's running tasks: `queue.py worker running 128 171`
(it writes this session's own state; the shared `state` file and the status line show the union of all live
sessions). It also moves the running rows to the top of their table, in the order given, so the table always reads in
run order: a task started out of order (the user's word: "unpause 326") goes right after the ones already running
(the user, 2026-11-07: "when we unpause 326, it should move to the top after 309"). `Q top N ...` does the same by hand. **Another session leading the same project:** rows it runs are its own (`wts` says "other sessions run
…"); skip them and take the top N of the rest. The workers share the tree, the game window lock
and the build: tell each one the other is running, which files are its, and that a build failing in the other's files
means wait and retry, never edit them.

**A fresh agent per task (the user, 2026-10-22).** Every step an agent takes sends its whole context again, and that
counts against the user's quota. A worker kept across tasks 146 to 153 grew to about 846k tokens.
- **HP:** start a fresh worker for each task, named for it (`hp<N>`). The old one is retired: send it nothing more.
- **A redo right after the show** may go back to the same worker while its context is under about 300k. Past that, a
  fresh worker does it from the task's notes.
- **MP and LP:** a fresh agent for each unit or milestone, carrying on from the task's journal or status file, once
  the agent is past about 300k; a smaller one carries on into the next unit (a fresh agent's catch-up would cost more).
- **Handing over a long task (MP or LP: a stage rebuild):** only at a unit boundary or a clean point, never in the
  middle of a change. The journal and status file must hold the why of each decision and the dead ends. Before it
  edits anything, the fresh agent sends you its plan for the unit in a few lines; check it against the journal (a
  misread handoff is caught here), then let it go on. Review every unit as before; that catches drift.
- **A pause is not a reason for a fresh agent.** On unpause, resume the same agent if it is under about 300k; start a
  fresh one only when it is past that or at a unit boundary. The fresh agent's catch-up (re-reading the brief, notes
  and code, about 50-150k) pays off only over the steps that follow, so it is cheaper for a long stretch of work and
  dearer for a step or two.
- **A long task** (several advisor rounds) can be handed off midway: ask the agent to bring its notes up to date and
  stop at a clean point, then start a fresh one.
- **Check sizes** with `python3 ~/.claude/skills/worker/scripts/ctx.py --root <root>`.
- **The handoff is the notes file.** The brief tells the worker to write it so that an agent knowing nothing of the
  task can do a redo from it alone.

**A visual task gets a visual advisor (the user, 2026-10-22: "If an work item related to the visual, please also ask
for a visual advisor for suggestion").** Any task about how something looks or moves (a model, an effect, a shader, a
motion, scenery) starts with two agents: the worker `hp<N>` and an advisor `adv<N>`. The advisor researches real
references (and the Sky Force walkthrough where it fits), inspects and scores, but never edits game files or builds.
It sends the worker its advice, and it sends the lead one line after each round. It scores two task-specific criteria
0-10, for up to 4 rounds, and stops at 8+ on both. When a reference image or clip matters, it makes side-by-side sheets
of the reference against ours at the same scale. Before the rounds start, the worker saves the files it will touch to
`.autopilot/worker/<topic>-pre-advisor/`. If the rounds run out below 8, the worker does one final fix pass on the
advisor's last points, with no further round, and the lead checks the frames. A pure logic or data fix with no look to
judge needs no advisor. Anything that moves is judged from a clip from round 1, never from stills alone (the user,
2026-10-25, task 169: 166 passed on stills and failed at the show); say so in both briefs. The template for the advisor's prompt is the adv128/adv129 prompt: the job and the user's
words, how to see it, the rules, round 0, the rounds, the lead line, the final report.

## 1b. Three tables: HP, MP and LP (and AG, section 1c)

The queue has a **Pri** column. **HP** (high priority): short tasks the one background worker does in order, which the
user checks one by one. **MP** (medium priority, added 2026-10-18): mid-sized tasks, too big for the worker's queue but
not a stage rebuild, each on its own agent that you start with the Agent tool (its row says so). **LP** (low
priority): long tasks (a stage rebuild, a big refactor), each on its own agent too. **One numbering** for all; a task
keeps its number when it moves (`Q pri N HP|MP|LP`). The file keeps HP rows first, then MP, then LP, then END.

**The order between them.** HP comes first, then MP, then LP: you start MP agents before LP ones, and when the shared
window is wanted by more than one, an MP agent goes before an LP agent (tell LP agents to step aside, or put LP in
desk-only, while an MP agent needs the window). The worker and HP showings still come before both.

**The group modes.** MP and LP each have one (`Q mp running|desk|paused`, `Q lp running|desk|paused`), for the whole
table, so the user can keep either group out of their way. The modes, the same for both (described for LP):
- `running`: LP agents use the shared window, with a 30 s gap after each release so HP work and showings get in.
- `desk` (desk-only): LP agents keep working at the desk (designs, code, data, tests that need no window) but take no
  build, capture, data copy or app run; a step that needs the window waits in the row's State ("waiting for the
  window: …"), at most one unit ahead.
- `paused`: LP agents stop at the next clean point.
After changing a mode, message every agent of that group with the new mode (a stopped agent is resumed by the message: only
send `running` or work to one that should go on). LP agents never write the HP worker's `state` file.

**The data hole:** an HP build or data copy carries LP's half-done changes too (one tree). Keep every save compiling,
and copy data into the built app only right after a build when code has changed since (`sync_data`-style scripts
should refuse otherwise): one unknown key can stop the app from loading.

## 1c. AG: the lead's own agents

The user, 2026-11-14: "I want to make them a home in the WTS, since most time i use the wTS table ... they should work
like a normal wTS task ... I also want to do the things like \"accept\", so them will be removed from the WTS table."
**Every agent you start yourself outside HP, MP and LP gets an AG row first** (a task's own helpers, such as an
advisor, do not: they belong to their task). The fourth table, after LP, in the one numbering:
1. File it: `Q add --pri AG --mode append --title "a few words" --text "<the user's words in quotes>, what it is for.
   Runs as a fresh agent on the <Model> model."`; it records this session as the agent's owner.
2. Start it named **`ag<N>`** with the description **"AG N: <what>"** (ccprobe and the status line tie it to its row
   by that name), and set its State: `Q state N --text "in progress <time>: …"`.
3. Keep its State current when it reports (`done …`, `waiting …`); an agent that only waits by design (a placeholder)
   stays `in progress`.

The differences from MP and LP: **no limit** and **no group mode** (each agent is one the user asked for); **off the
shared window** unless its row says otherwise (then after LP); **they never count for the keep-awake** (`Q awake`) **nor
for "queue empty"** (END). **`accept N` on an AG row stops its agent first** if it is still alive (the user: "accept
stops the agent"): a clean stop it has (a stop file, a message), else `TaskStop`; then `Q accept`. Say in one line that
you stopped it.

## 2. The user's words

| The user says | Where it goes | What you do |
|---|---|---|
| `worker: <task>`, `append <task>`, `queue <task>` | **Tail**: just before the pinned rows (END last). | `Q add --mode append --text "<row text>"`, then message the worker. |
| `push <task>` | **Worker idle:** the top. **Worker busy:** just after the task in progress (it runs next). | `Q add --mode push …`, message the worker: "finish the task in hand, then N". |
| `push override <task>` | **The top, now.** The task in progress is paused (its State says so) and resumes right after. | `Q add --mode override …`, send PUSH OVERRIDE (section 4). |
| `worker: for N, <note>` | An addendum on task N's row. | `Q addendum N --text "…"`; if N is in flight, message the worker now. |
| `wts` | — | The status table (section 3). |
| `accept N` / `N accepted` / "accept all the X ones" | — | `Q accept N … --words "<their words>"`; they leave `wts`. For "all the X ones", list which rows you took and which you left. |
| `LP <task>`, "a long task" | **LP table**, its tail. | `Q add --pri LP --mode append …`; start its own agent (or queue it). |
| `AG <task>`, "spawn an agent to …" (not the worker) | **AG table**, its tail. | Section 1c: `Q add --pri AG …`, then its agent `ag<N>`. |
| `reopen N` | — | `Q reopen N`: undoes an accept or a close (a mistaken one); the row is back in its table with its State. |
| `wts hx` / `wts history` | — | `Q wts hx` (HX, the history: the last 20 closed, newest first; `--all` for every one): paste it as it is. |
| `accept N` on an AG row | — | Stop its agent if it is alive, then `Q accept N`; it leaves `wts` (section 1c). |
| `stop N` (AG) | — | Stop its agent; `Q state N --text "paused <time>: stopped by the user"`; the row stays until accepted. |
| `cancel N` (AG) | — | Stop its agent; `Q close N --why "cancelled by the user"`: it leaves `wts` without a ✔. |
| `HP N` / `LP N` | Moves task N to that table. | `Q pri N HP|LP`; tell the agents concerned. |
| `pause LP` / `LP desk-only` / `resume LP` | — | `Q lp paused|desk|running`; message every LP agent. |
| `pause N` | — | That task only: PAUSE to the agent running it; the HP worker goes on to the next HP task. |
| "max parallel N" for HP, MP or LP ("set HP's max parallel to 2") | — | `Q limit hp 2` (this session only); start or let finish agents to match; never edit config.json for it. |
| `pause the worker` | — | Send PAUSE; `Q worker paused`. |
| `resume`, `worker: continue` | — | Send RESUME with what changed while paused and the order; `Q worker running`. |
| `reserve winN` / `release winN` (projects with `locks`) | — | Keep a window for the user's shows / give it back: the project's tool (e.g. `tools/winres.sh reserve|release win0|win1|all`); say what is in that window now. |
| `show me N` / `show me <thing>` | — | Open the app at what task N changed, if the window is free (section 5). |

Every row is written by you, in the user's own words first (in quotes), then what they mean in plain terms, then how
it will be checked and what picture sheet to make. One row, one outcome. A follow-up on an open task is an addendum,
not a new task. When a new note supersedes an open task, fold the old one in (`Q state N --text "folded into M"` and
`Q close N --why "folded into M"`).

**Keep the Mac awake exactly while this session's agents work** (macOS; the user, 2026-11-05: "when there's nothing
in the WTS to do, e.g. all paused, or the max parallel set to 0 and all task can be run are finished, we need turn off
the caffinate"). **`Q awake on`** whenever an agent starts or resumes: queuing a task that will start, unpause, raising
a table's limit (`Q limit`). **`Q awake off`** as soon as none of this session's agents is working: all three tables
paused and every agent confirmed paused; the limits at 0 and the last running agent finished; or "queue empty". After
every agent's final report, a pause, or a limit change, check: any worker, MP/LP agent or advisor still working (an
agent "waiting on its own background work" counts as working: section 6; AG agents never count: section 1c)? If
none, `Q awake off`. Queuing a task that cannot start (tables paused or at 0) does not turn it on. `Q awake` shows who
holds one. **Each Claude session holds its own and stops only its own** (the user, 2026-11-08: "when multiple claude
session turn on or turn off caffinate, that will cause race condition"): `awake on` starts a `caffeinate -dims -w <this
session's Claude pid>`, detached, so it outlives the Bash call and ends by itself when the session exits; a second `on`
does nothing; `off` stops only this session's. macOS counts the holds itself (the Mac stays awake while any session
holds one), so there is no shared counter to drift. **So never** start `caffeinate` yourself (no `run_in_background`,
no `&`), never decide from `pgrep` that "one is already running" (that is another session's, and it stops when that
session is done: 2026-11-06, a lead relied on another project's), and never `pkill`/`killall caffeinate` or kill a
caffeinate by pid (2026-11-07 and 11-08: `pkill -x caffeinate` killed another project's, and Claude Code's own). A plain
`caffeinate -dims` this session started the old way is replaced by the next `awake on` and stopped by `awake off`.
**Where there is no `caffeinate`** (Linux, or a server that does not sleep), `awake` does nothing and `queue.py`
leaves the segment out. **The model rule:** every task runs on the worker's model unless the user explicitly
puts a task on another model; such a task gets its own fresh agent on that model, started by you, and its row says
"Runs as a fresh agent on the <Model> model".

## 3. The status table (`wts`)

**Fast path (the user, 2026-10-17: "can we make the \"wts\" table display faster? now it takes a while and burn quite
some tokens"):** run `Q wts` and paste its output as it is: the window line, the HP table, the LP table, END. Do not
rewrite it. It reads short titles from `.autopilot/worker/titles.json` (give one when filing: `Q add … --title "a few
words"`, or `Q title N --text …`), cuts each State to its head, and marks done rows "to review" until you have
reviewed them (`Q reviewed N …`, or a "Lead review" addendum; a reviewed done row shows no mark). Add a line under it only if something needs saying.
**Zero tokens:** the user can type `! wts` (`~/.local/bin/wts`, which runs `queue.py wts --term`: the same content
as `Q wts`, its tables boxed with ┌┬┐ lines). Never fold rows into prose; never group tasks.

```
🔴 Window: held by worker capture Sun Oct 18 10:08:26 PDT 2026 · caffeinate on

**HP** · the worker: ⏩ running 86

| # | Task | Runs on | Status | Accepted |
|---|---|---|---|---|
| 86 | Islet turtle swims in and fades out smoothly | Worker (Opus 5.5) | ⏩ in progress 09:36 | — |
| 87 | Seal moves earlier, seen entering the water | Worker (Opus 5.5) | ⏳ queued | — |
| 83 | Dolphins move to d1875, left side | Worker (Opus 5.5) | ✅ done 00:18 | — |

**LP** · 📝 desk-only

| # | Task | Runs on | Status | Accepted |
|---|---|---|---|---|
| 74 | Stage 5 (Red Canyon) rebuild | Opus 5.5 | ✋ waiting for the window | — |
| 77 | Stage 6 (Oil Fields) rebuild | Opus 5.5 | ⏩ in progress · waits for the window | — |
| 52 | Stage 4 (Dune Road) rebuild | Fable 5.1 | ✅ done 07:37 | — |

999 END: when all three tables are empty, "queue empty", then the lead runs `Q awake off` (macOS only).
```

**How a row reads.** The sign comes from the first words of the State cell:

| State begins with | Sign | Listed |
|---|---|---|
| `needs the user` | ❓ | first among open rows |
| `in progress` / `in hand` | ⏩ | open |
| `paused` | ✋ | open |
| `parked` / `waiting` / `hold` / `on hold` | ✋ | open |
| `queued` | ⏳ | open |
| anything else (an agent's own words) | ⏳ | open: **never hidden** (until 2026-10-18 such a row vanished) |
| `done` | ✅ | after the open rows |
| Accepted ✔ or n/a | — | left out (`wts --all` shows them) |

- The cell shows the State's head (before the first ": "), plus "· to review" (a done row not yet reviewed; a reviewed one shows nothing), "· small fix owed"
  (the State says "fix owed") and "· waits for the window" (it says so). "HOLD" reads as "on hold".
- **Runs on** is the last "on the <Model> model" in the row's text, so an addendum that moves a task to another model
  ("From now on it runs on the Opus 5.5 model") changes the column; with none, the worker's model.
- **The HP heading** shows the state file: ⏩ running N, ✋ PAUSED, ■ stopped while HP rows are still open, and
  **💤 idle** when no HP row is open (the user, 2026-10-18: "while HP has nothing to do, status line … should be
  IDLE, not STOPPED"). The status line uses the same words.
- Tell every agent to begin its State with one of those words (`in progress 09:20: U1 built, waiting for the lead`),
  and fix a row whose State has gone stale (`Q state N --text "in progress …"`).

Signs, first in the Status cell: ✅ done · ⏩ in progress · ⏳ queued · ✋ PAUSED or parked · ⛔ blocked · ❓ needs the
user. Only natively wide emoji line up in a terminal table (no U+FE0F symbols such as ⏸️ ▶️). Mark a row reviewed
(`Q reviewed N`) only after you have reviewed it (section 6).

## 4. Messages to the worker

- **New task:** "Queued task N (row N in queue.md), <placement>: <one paragraph with the user's words>. Order: <next
  few numbers>."
- **PUSH OVERRIDE:** "PUSH OVERRIDE, from the user: task N (row N). Finish only the atomic step in flight (a running
  build or capture: never kill it), leave the tree building, note where you are in task M's State ('paused <time> for
  N: done …, next …'), write `running N` to the state file, and start N now. Then resume M from your note."
- **PAUSE:** "PAUSE, from the user. Stop at the next clean point: let a running build or capture finish, then release
  the lock. Start nothing new. Write where you are in the row's State ('paused <time>: …'), reply 'paused' and wait."
- **RESUME:** "RESUME, from the user. While you were paused: <accepted, new tasks, addenda>. Order: <list>."
- **Step aside** (the user wants the app now): "After the step in flight, release the lock and stay off it for 5
  minutes."
- **Review result:** "N passes my review", or exactly what to fix and how it will be checked.

The worker keeps `.autopilot/worker/state` and the State cells current; you write `paused`/`running` on the user's
pause and resume. If the status line shows the wrong state, fix the file.

## 5. The shared window (showings)

The lead and the worker share one built app, so one build or run at a time, under the lock in `config.json`.
**The window lock is held only while the app is open or a new build is swapped in:** never across reading frames, editing or thinking; one run per take, released the moment the app closes, so a
showing gets in between a worker's runs. Workers use the project's one-run helper (e.g. a `tools/run1.sh`, which
`.autopilot/worker/bin/shots.sh` calls per spec) and its build tool (`tools/build.sh`), which builds beside the app
under a separate build lock (`build_lock` in `config.json`) and takes the window lock only for the swap. A worker's
own capture wrapper calls the helper per run; it never takes the window lock across a loop of runs.
- Your showing, run in the background so the conversation goes on:
  `if mkdir <lock> 2>/dev/null; then echo OPENED; <open the app at the right place>; rmdir <lock>; else echo HELD; cat <lock>; fi`
  A few seconds later check it said OPENED and the app is running. If HELD, tell the user who holds it and since
  when (`Q window`); do not queue the showing; offer to ask the worker to step aside.
- Your own build: through the project's build tool (e.g. `WHO=lead tools/build.sh <log>`), which names itself
  in the build lock and in the window lock for the swap. A project with no build tool: `mkdir <lock>` and write
  `lead build <date>` into `<lock>/who`, so the status line names it.
- **Never `mkdir -p`** on the lock: it succeeds when the lock is held, and you will think you have it.
- When the app closes, say so in one line. Don't promise controls you have not checked in the code or the app's help.

## 6. Reviewing (the user's default: you approve)

The user's standing rule: for every worker task, you review and approve at your best without them ("batch"). A task
that says "the user approves X" means you do. Log taste calls in the task's notes so the user can overrule them.
- Read the worker's report and notes; **look at its picture sheets yourself**. If a sheet is too small to judge, or the
  app's own text covers the subject, ask for close crops (4x) before passing.
- Say plainly what a still cannot show (timing, smoothness); the user judges motion by eye.
- Watch for side effects beyond the task (a shared model, shader or setting used elsewhere) and tell the user.
- Pass, or send back with exact fixes. Then tell the user in a few lines, with how to see it.
- **Close the agent out (the user, 2026-11-13: "have you closed hp88 correctly?" It had not been: a wait loop of its
  own was still running, and the lead had said "closed" after checking only the agent list, OS processes and the
  lock).** An agent is closed only when its notification says **finished**. A notification that says it "is waiting on
  its own background work" **after** its report means a job or Monitor of its own is still running: message the agent
  to list and TaskStop its jobs (or TaskStop the job id yourself when the user gives it), wait for the "finished"
  notification, and only then tell the user it is closed or run `Q awake off`. `ListAgents`, `ps` and the lock do not
  show such jobs. A stuck wait also means the agent may have reported a check it never saw finish: rerun that check
  yourself before the task counts as reviewed.

## 7. Method for visual work (the default to put in rows)

Research first (web pictures and video frames of the real thing, saved as references only, never as textures). After
**each** iteration, a comparison sheet: our capture cropped beside the reference at the same scale and angle, plus a
half-transparent overlay, for shape, colour and texture; fix the gaps and repeat. One frame at normal viewing
distance: it must read at a glance.
