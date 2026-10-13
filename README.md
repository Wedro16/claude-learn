# claude-learn

Claude helps you get the work done **and** learn what it helped you build. This repository is the
pilot: it runs in Claude Code on a Claude subscription, keeps a private learner model on your
machine, and records how you take ownership of Claude's drafts.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the architecture diagram and tech stack.

## Setup

Requires Python 3.10+ and Claude Code signed in with a Claude subscription.

```sh
scripts/install.sh
```

This creates `.venv`, installs dependencies, and points git at the hooks in `githooks/`. Then start
Claude Code in this repository. The first time, approve the project's `learner` MCP server.

Learner data lives in `~/.claude-learn/learner.db` (set `CLAUDE_LEARN_HOME` to move it). It is never
committed.

## Using it

Work as usual. Claude follows the learning policy in [CLAUDE.md](CLAUDE.md):

- It records the concepts each task involves and adapts how much it teaches to your level.
- Sometimes it leaves a gap marked `TODO(learner) <id>` with a hint. Fill it in, or ask for the
  answer; either is fine.
- Commits containing Claude-written lines get `Assisted-by: Claude` and `Claude-Session` trailers,
  plus a line-attribution note under `refs/notes/claude-attribution`.

Things you can say to Claude:

- "Just do it": learning off for this task.
- "Turn learning down for SQL" or "I want to learn how to do this, not just about it."
- "Why am I at level 2 on retries?" or "What have you recorded about me?"

## Progress

```sh
.venv/bin/python -m learner.report          # against the pilot's exit criteria
.venv/bin/python -m learner.report --json
```

## Attribution notes

Notes travel separately from commits:

```sh
git push origin refs/notes/claude-attribution
git fetch origin refs/notes/claude-attribution:refs/notes/claude-attribution
```

## Tests

```sh
.venv/bin/python -m pytest
```
