# claude-learn

Claude helps you get the work done **and** learn what it helped you build. It adapts how much it
teaches to what you already know, checks what you've learned, and tracks how you take ownership of
Claude's drafts.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the architecture diagram and tech stack.

This repository has two parts that share one learner engine (`learner/`):

- **Web demo (`webapp/`)**: a hosted, no-setup demo of the core loop. Pick a learner and a task;
  Claude does the task adapted to that learner, asks a question, leaves a gap; the learner
  model updates as you answer. Runs on Cloud Run.
- **Claude Code pilot**: the real-world integration, used day to day in your own repository
  (MCP server, hooks, commit attribution). Setup below.

## Web demo

Bundled content, so reviewers need nothing of their own:

- Three synthetic learners: Sam (new to the topic), Priya (building the skill), Alex (experienced).
- Two tasks: make a flaky API call resilient (code) and explain a design decision to a reviewer (writing).
- "Live Claude" calls Claude Opus 5.5 (about 20-40 s). Unchecked, it replays a recorded live
  response instantly. Over the rate limit, it falls back to recordings automatically.
- "Talk it through with Claude": explain the reasoning behind an edit, ask why, or push back on
  Claude's version. Claude replies and classifies the message: a correct explanation counts as
  self-explanation, a question is scored on the SOLO scale, and valid pushback counts as critique.
- "I'm done — use this version" closes a run in four steps: a final check by Claude (it never
  blocks), an optional explain-it-back question, a summary of what you built and what changed in
  your learner model (with when each concept comes back for review), and the file to copy or
  download, shown alongside the commit it would become in Claude Code.
- API docs at `/docs`.

Run locally:

```sh
pip install -r requirements.txt
ANTHROPIC_API_KEY=... uvicorn webapp.app:app --reload     # http://localhost:8000
```

Re-record the instant-replay responses after changing prompts: `ANTHROPIC_API_KEY=... python -m webapp.record`.

Deploy to Cloud Run (stores the key in Secret Manager):

```sh
ANTHROPIC_API_KEY=... scripts/deploy.sh <gcp-project-id> [region]
```

## Claude Code pilot setup

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
