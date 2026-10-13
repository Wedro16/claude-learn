"""End-to-end: the real git hooks in a scratch repository."""

import json
import subprocess
from pathlib import Path

from learner.attribution import record_claude_lines
from learner.db import connect
from learner.store import LearnerStore

HOOKS = Path(__file__).resolve().parent.parent / "githooks"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "core.hooksPath", str(HOOKS))
    return repo


def test_claude_lines_get_trailers_and_note(store, tmp_path):
    repo = make_repo(tmp_path)
    record_claude_lines(store.conn, "s1", str(repo / "retry.py"), "def retry(fn):\n    return fn()")
    store.conn.commit()
    (repo / "retry.py").write_text("def retry(fn):\n    return fn()\n# tuned by hand\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Add retry")

    message = git(repo, "log", "-1", "--format=%B")
    assert "Assisted-by: Claude" in message and "Claude-Session: s1" in message
    note = json.loads(git(repo, "notes", "--ref", "claude-attribution", "show", "HEAD"))
    assert note["files"]["retry.py"] == {"claude": [[1, 2]], "human": [[3, 3]]}

    sha = git(repo, "rev-parse", "HEAD").strip()
    row = LearnerStore(connect()).conn.execute("SELECT * FROM commits WHERE sha = ?", (sha,)).fetchone()
    assert (row["claude_lines"], row["human_lines"], row["session_id"]) == (2, 1, "s1")


def test_human_only_commit_has_no_trailers(store, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "notes.md").write_text("Written entirely by a person.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Notes")
    assert "Assisted-by" not in git(repo, "log", "-1", "--format=%B")
    sha = git(repo, "rev-parse", "HEAD").strip()
    row = LearnerStore(connect()).conn.execute("SELECT human_lines FROM commits WHERE sha = ?", (sha,)).fetchone()
    assert row["human_lines"] == 1
