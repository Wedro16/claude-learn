#!/usr/bin/env python3
"""SessionEnd hook: close the learner session."""

from _common import read_input

from learner.store import LearnerStore


def main() -> None:
    data = read_input()
    if data.get("session_id"):
        LearnerStore().end_session(data["session_id"])


if __name__ == "__main__":
    main()
