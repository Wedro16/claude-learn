# Pilot architecture

The pilot runs entirely on one machine, inside Claude Code on a Claude subscription. There are no
cloud services. Learner data stays in a local SQLite file outside the repository; the repository
only receives commit trailers and line-attribution notes.

```mermaid
flowchart LR
    person([Professional])

    subgraph cc["Claude Code (Claude subscription)"]
        claude["Claude<br/><i>follows CLAUDE.md learning policy</i>"]
        hooks["Claude Code hooks<br/><i>Python 3.13</i><br/>SessionStart · UserPromptSubmit · PostToolUse · SessionEnd"]
    end

    subgraph local["Local machine"]
        mcp["learner MCP server<br/><i>Python 3.13 · mcp SDK 2.x · stdio</i>"]
        db[("Learner store<br/><i>SQLite · ~/.claude-learn/learner.db</i>")]
        gh["git hooks<br/><i>Python 3.13</i><br/>prepare-commit-msg · post-commit"]
        report["learner.report CLI"]
    end

    repo[("GitHub repository<br/><i>commits + trailers<br/>refs/notes/claude-attribution</i>")]

    person <-->|tasks, answers| claude
    claude <-->|12 MCP tools| mcp
    hooks -->|session summary in| claude
    hooks -->|lines Claude wrote| db
    mcp <--> db
    person -->|commits| gh
    gh -->|trailers + notes| repo
    gh -->|commit attribution| db
    report --> db
```

## Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| Claude access | Claude Code on a Claude subscription | No API key or backend needed for a single-user pilot |
| Learning policy | `CLAUDE.md` | Claude Code loads it into every session in this repo |
| Learner model API | MCP server, Python 3.13, `mcp` SDK 2.x, stdio transport | Claude calls it as tools; no network service to run |
| Storage | SQLite (stdlib `sqlite3`) | Zero setup; one file per learner, outside the repo |
| Session context | Claude Code hooks (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `SessionEnd`) | Load learner context and record Claude-written lines without Claude having to remember |
| Provenance | git `prepare-commit-msg` and `post-commit` hooks, git notes | Trailers and line ranges travel with the repository |
| Repository host | GitHub (existing) | Easy to reproduce |
| Reporting | `python -m learner.report` | Exit-criteria metrics without a frontend |
| Tests | pytest | Unit tests plus end-to-end tests of the git hooks |

## Data flow for one task

1. **SessionStart hook** opens a session and gives Claude the learner summary: skill tracks, due
   reviews, commits to tag, withheld items to review, non-learning flags.
2. **Claude** records the task's concepts, reads the plan from `get_learner_context`, and does the
   work with the interventions the plan allows.
3. When Claude withholds part of the work, it stores the full draft with `record_baseline` and
   leaves `TODO(learner) <item_id>` markers.
4. **PostToolUse hook** records every line Claude writes.
5. On commit, **prepare-commit-msg** adds `Assisted-by` / `Claude-Session` trailers when the commit
   contains lines Claude wrote in this repository in the last 7 days. **post-commit** classifies each
   added line of the finished commit as `claude`, `claude_modified` or `human` (amends included),
   writes the attribution note, and records the commit. Merge commits are skipped.
6. In the next session, Claude tags the commit's concepts and reviews completed gaps against the
   baseline. Those become evidence, and the mastery rules update levels.

## What the pilot defers

The shared concept graph and Insights API (aggregates need 3 or more people), the key service
(disk encryption covers a local store), the Google Docs bridge, and the Bayesian mastery model.
See the design doc's Technical architecture tab for the production design on GCP.
