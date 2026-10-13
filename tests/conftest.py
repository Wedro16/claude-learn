import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learner.db import connect
from learner.store import LearnerStore, set_current_session


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LEARN_HOME", str(tmp_path / "learn"))
    return tmp_path / "learn"


@pytest.fixture
def store(home):
    s = LearnerStore(connect())
    s.start_session("s1", "/repo")
    set_current_session("s1", "/repo")
    return s
