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


def commits_table():
    return {r["sha"]: dict(r) for r in LearnerStore(connect()).conn.execute("SELECT * FROM commits")}


def note(repo, rev="HEAD"):
    return json.loads(git(repo, "notes", "--ref", "claude-attribution", "show", rev))


def claude_commit(store, repo):
    record_claude_lines(store.conn, "s1", str(repo / "retry.py"), "def retry(fn):\n    return fn()")
    store.conn.commit()
    (repo / "retry.py").write_text("def retry(fn):\n    return fn()\n# tuned by hand\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Add retry")


def test_message_only_amend_keeps_full_attribution(store, tmp_path):
    repo = make_repo(tmp_path)
    claude_commit(store, repo)
    old = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "commit", "-q", "--amend", "-m", "Add retry helper")
    new = git(repo, "rev-parse", "HEAD").strip()
    assert "Assisted-by: Claude" in git(repo, "log", "-1", "--format=%B")
    assert note(repo)["files"]["retry.py"] == {"claude": [[1, 2]], "human": [[3, 3]]}
    rows = commits_table()
    assert old not in rows
    assert (rows[new]["claude_lines"], rows[new]["human_lines"]) == (2, 1)


def test_amend_with_more_changes_covers_the_whole_commit(store, tmp_path):
    repo = make_repo(tmp_path)
    claude_commit(store, repo)
    (repo / "retry.py").write_text("def retry(fn):\n    return fn()\n# tuned by hand\n# and again\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--amend", "--no-edit")
    assert note(repo)["files"]["retry.py"] == {"claude": [[1, 2]], "human": [[3, 4]]}
    assert len(commits_table()) == 1


def test_merge_commits_are_not_recorded(store, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("base line\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("side change\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "side")
    git(repo, "checkout", "-q", "-")
    (repo / "c.txt").write_text("main change\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge = git(repo, "rev-parse", "HEAD").strip()
    assert merge not in commits_table()
    assert len(commits_table()) == 3


def test_aborted_commit_leaves_nothing_behind(store, tmp_path):
    repo = make_repo(tmp_path)
    claude_commit(store, repo)
    (repo / "retry.py").write_text("def retry(fn):\n    return fn()\n# tuned by hand\n# more\n")
    git(repo, "add", ".")
    # An empty message aborts the commit after prepare-commit-msg has run.
    aborted = subprocess.run(["git", "commit", "-q", "-m", ""], cwd=repo, capture_output=True, text=True)
    assert aborted.returncode != 0
    (repo / "x.txt").write_text("unrelated human work\n")
    git(repo, "add", "x.txt")
    git(repo, "reset", "-q", "retry.py")
    git(repo, "commit", "-q", "-m", "Unrelated")
    assert note(repo)["files"] == {"x.txt": {"human": [[1, 1]]}}


def test_claude_work_in_another_repository_does_not_count(store, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    record_claude_lines(store.conn, "s1", str(other / "util.py"), "import json")
    store.conn.commit()
    repo = make_repo(tmp_path)
    (repo / "util.py").write_text("import json\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "util")
    assert "Assisted-by" not in git(repo, "log", "-1", "--format=%B")


def test_non_ascii_paths_are_attributed_by_name(store, tmp_path):
    repo = make_repo(tmp_path)
    record_claude_lines(store.conn, "s1", str(repo / "café.py"), "value = compute_total()")
    store.conn.commit()
    (repo / "café.py").write_text("value = compute_total()\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "café")
    assert note(repo)["files"] == {"café.py": {"claude": [[1, 1]]}}
