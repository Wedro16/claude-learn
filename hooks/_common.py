"""Shared setup for hook scripts: make the learner package importable, read hook input."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def read_input() -> dict:
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}
