# worker: a background task queue for Claude Code

A [Claude Code](https://claude.com/claude-code) skill. Your main session becomes the **lead**: you talk only to it,
it files your requests as numbered tasks in a queue, starts a fresh background agent (a **worker**) for each one,
reviews what comes back, and shows you the result. You keep working with the lead while the workers run.

- **Three tables:** HP (short tasks, one or a few workers at a time, in order), MP and LP (mid-sized and long tasks,
  each on its own agent). One numbering for all; a task keeps its number when it moves.
- **Your words drive it:** `worker: <task>`, `push <task>`, `push override <task>`, `pause the worker`, `resume`,
  `accept N`, `LP <task>`, `pause LP`, `LP desk-only`, `show me N`, and `wts` for the status table.
- **One shared window:** builds, captures and app runs take a lock, so the agents and your own showings never collide.
  The lock is held only while the app is open, so a showing gets in between a worker's runs.
- **A fresh agent per task**, so contexts stay small; the task's notes file is the hand-off for a redo.
- **A status line** with the window, the workers and the groups' modes, and `! wts` for the table at no model tokens.

The queue lives in your project at `.autopilot/worker/queue.md`, a plain Markdown table you can read and edit.

## Install

```sh
git clone https://github.com/grandleaf/claude-worker-skill ~/.claude/skills/worker
ln -s ~/.claude/skills/worker/scripts/wts ~/.local/bin/wts   # optional: `! wts` in Claude Code (any folder on your PATH)
```

Then, in a project, ask Claude Code to "set up a background worker". The lead follows `SKILL.md` section 1: it copies
`templates/queue.md` and `templates/config.json` into `.autopilot/worker/`, writes the worker's brief from
`templates/worker-brief.md` with your project's build, test and run commands, and offers to set the status line:

```json
{"statusLine": {"type": "command", "command": "~/.claude/skills/worker/scripts/statusline.sh", "padding": 0}}
```

## Files

| File | What |
|---|---|
| `SKILL.md` | The lead's instructions: the tables, your words, the status table, messages to the worker, the shared window, reviews |
| `scripts/queue.py` | The lead's tool for every queue edit (`python3 scripts/queue.py --help`) |
| `scripts/statusline.sh` | The status line |
| `scripts/wts` | Prints the status table (`queue.py wts --term`) for the project above the current folder |
| `scripts/ctx.py` | Each background agent's context size, to know when a fresh one is cheaper |
| `templates/` | A new project's `queue.md`, `config.json` and the worker's brief |

## Needs

Claude Code with background agents, Python 3, and a POSIX shell. Written on macOS: `caffeinate` (keeping the Mac awake
while tasks run) is macOS-only and simply left out elsewhere.

## Licence

MIT (`LICENSE`).
