# The worker's queue (kept by the lead)

One task at a time, in the order of the table (a row in "redo" first). Each line in quotes is the user's own words. **The worker edits only the
State cell of a row**; everything else is the lead's (`~/.claude/skills/worker/scripts/queue.py` does the lead's edits).

**Approvals and reviews are the lead's.** Wherever a task says the user approves, decides or sees something before
work goes on, read "the lead": hand back to the lead, who reviews and decides, and logs its taste calls in the notes.

**Accepted** is the user's word only: `✔ <date> <time> (the user: "<words>")`. The lead's review doesn't count. `wts` leaves
out accepted rows and `n/a` rows (withdrawn, superseded, folded into another task).

**The tables.** The Pri column: **HP** rows are the short tasks the background worker does one at a time; **LP** rows
are long tasks, each on its own agent. **AG** rows are the lead's own agents (started by the lead outside the queue,
one per row, named `ag<N>`; no limit; accepting one stops its agent). One numbering for all. HP rows first, then MP,
then LP, then AG, then END. The LP group's
mode is in `lp_mode` (running / desk = desk-only, kept off the shared window / paused); LP agents read it before every
step that takes the window, and never write `state` (the HP worker's).

**Placement**, within the task's own table. append / queue / "worker: …": at the tail (HP: before the LP rows; LP:
before END). push: HP worker idle, at the top; busy, just after the task in progress; LP: at the top of the LP rows.
push override (HP): the task in progress is paused at its next clean point and the new task starts at once; the
paused task resumes right after it.

**Keep awake (macOS only):** the lead runs `queue.py awake on` when an agent starts and `queue.py awake off` when none
works (task 999, END, too); each Claude session holds its own keep-awake and stops only its own.
Where the command does not exist -- Linux, or a server that never sleeps -- there is nothing to keep awake and this
line does not apply.
**Model:** every task runs on the worker's model unless the user explicitly puts a task on another model.

| # | Pri | Task | State | Accepted |
|---|---|---|---|---|
| 999 | — | **END** (a fixed number, always the last row): when the HP, MP and LP tables are empty (AG agents don't count), report "queue empty" to the lead, who runs `queue.py awake off` | standing | n/a |
