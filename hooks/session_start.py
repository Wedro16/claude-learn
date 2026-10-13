#!/usr/bin/env python3
"""SessionStart hook: open a learner session and hand Claude the learner summary."""

import json

from _common import read_input

from learner.store import LearnerStore, set_current_session


def main() -> None:
    data = read_input()
    session_id = data.get("session_id", "unknown")
    store = LearnerStore()
    store.start_session(session_id, data.get("cwd"))
    set_current_session(session_id, data.get("cwd"))
    summary = store.summary()
    context = (
        "Learner model summary for this session (from the learner MCP server). "
        "Follow the learning policy in CLAUDE.md.\n" + json.dumps(summary, indent=2)
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
