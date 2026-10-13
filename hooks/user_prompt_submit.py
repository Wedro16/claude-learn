#!/usr/bin/env python3
"""UserPromptSubmit hook: a new message starts a new request, so clear any request-only dial."""

from _common import read_input

from learner.store import LearnerStore


def main() -> None:
    data = read_input()
    if data.get("session_id"):
        LearnerStore().clear_session_dial(data["session_id"])


if __name__ == "__main__":
    main()
