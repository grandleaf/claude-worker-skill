# The worker's queue (kept by the lead)

One task at a time, in the order of the table (a row in "redo" first). Each line in quotes is the user's own words. **The worker edits only the
State cell of a row**; everything else is the lead's (`~/.claude/skills/worker/scripts/queue.py` does the lead's edits).

**Approvals and reviews are the lead's.** Wherever a task says the user approves, decides or sees something before
work goes on, read "the lead": hand back to the lead, who reviews and decides, and logs its taste calls in the notes.

**Accepted** is the user's word only: `✔ <date> (the user: "<words>")`. The lead's review doesn't count. `wts` leaves
out accepted rows and `n/a` rows (withdrawn, superseded, folded into another task).

**Two tables.** The Pri column: **HP** rows are the short tasks the background worker does one at a time; **LP** rows
are long tasks, each on its own agent. One numbering for both. HP rows first, then LP rows, then END. The LP group's
mode is in `lp_mode` (running / desk = desk-only, kept off the shared window / paused); LP agents read it before every
step that takes the window, and never write `state` (the HP worker's).

**Placement**, within the task's own table. append / queue / "worker: …": at the tail (HP: before the LP rows; LP:
before END). push: HP worker idle, at the top; busy, just after the task in progress; LP: at the top of the LP rows.
push override (HP): the task in progress is paused at its next clean point and the new task starts at once; the
paused task resumes right after it.

**Caffeinate:** queuing any task starts `caffeinate -dims` if it is off; task 999 (END) stops it.
**Model:** every task runs on the worker's model unless the user explicitly puts a task on another model.

| # | Pri | Task | State | Accepted |
|---|---|---|---|---|
| 999 | — | **END** (a fixed number, always the last row): when all three tables (HP, MP and LP) are empty, report "queue empty" to the lead, who stops caffeinate | standing | n/a |
