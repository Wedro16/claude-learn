#!/usr/bin/env python3
"""SessionStart hook: open a learner session and hand Claude the learner summary."""

import json
import os
import subprocess

from _common import read_input

from learner.store import LearnerStore, set_current_session


def repo_root(cwd: str | None) -> str | None:
    """The git repository this session works in; commits and gaps are scoped to it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd or None, capture_output=True, text=True, check=True
        ).stdout.strip()
        return os.path.realpath(out)
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "unknown")
    repo = repo_root(data.get("cwd"))
    store = LearnerStore()
    store.start_session(session_id, data.get("cwd"), repo)
    set_current_session(session_id, data.get("cwd"))
    summary = store.summary(repo=repo)
    context = (
        "Learner model summary for this session (from the learner MCP server). "
        "Follow the learning policy in CLAUDE.md.\n" + json.dumps(summary, indent=2)
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
