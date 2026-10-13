"""Commit-time provenance: trailers, attribution notes, and commit records.

Called from the git hooks in ``githooks/``. Learner data stays in the local
store; the repository only receives trailers and line ranges.
"""

import json
import os
import subprocess
from datetime import timedelta

from learner.attribution import classify
from learner.db import connect, now, parse
from learner.store import LearnerStore

NOTES_REF = "claude-attribution"
# How far back a line Claude wrote still counts toward a commit.
LINE_LOOKBACK = timedelta(days=7)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git(*args: str, input: str | None = None) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, input=input).stdout


def repo_root() -> str:
    return os.path.realpath(git("rev-parse", "--show-toplevel").strip())


def _is_amend(source: str | None, commit: str | None) -> bool:
    """Whether the commit being prepared replaces HEAD.

    ``git commit --amend`` reports ``commit HEAD`` only without -m/-F, so also check
    the git command that ran this hook.
    """
    if source == "commit" and commit == "HEAD":
        return True
    try:
        args = subprocess.run(
            ["ps", "-o", "args=", "-p", str(os.getppid())], capture_output=True, text=True
        ).stdout
    except OSError:
        return False
    return "--amend" in args.split()


def _parent(rev: str) -> str:
    """The first parent of a commit, or the empty tree for a root commit."""
    try:
        return git("rev-parse", "--verify", "-q", f"{rev}^").strip()
    except subprocess.CalledProcessError:
        return EMPTY_TREE


def _since() -> str:
    return (parse(now()) - LINE_LOOKBACK).isoformat(timespec="seconds")


def prepare_commit_msg(msg_file: str, source: str | None = None, commit: str | None = None) -> dict:
    """Add trailers when the commit being made contains Claude-originated lines."""
    if source == "merge":
        return {}
    # An amend replaces HEAD, so compare with HEAD's parent to cover the whole commit.
    base = [_parent("HEAD")] if _is_amend(source, commit) else []
    attribution = classify(connect(), git("diff", "--cached", "-U0", "--no-color", *base), repo_root(), _since())
    t = attribution["totals"]
    if t["claude"] + t["claude_modified"] > 0:
        trailers = ["--trailer", "Assisted-by: Claude"]
        if attribution["sessions"]:
            trailers += ["--trailer", f"Claude-Session: {attribution['sessions'][0]}"]
        git("interpret-trailers", "--in-place", "--if-exists", "doNothing", *trailers, msg_file)
    return attribution


def post_commit() -> dict:
    """Attribute the finished commit, attach the note, and record it for analysis.

    Works from the commit itself rather than state saved before it, so aborted
    commits and amends of any kind cannot leave it describing other work.
    """
    sha = git("rev-parse", "HEAD").strip()
    if len(git("rev-list", "--parents", "-n", "1", sha).split()) > 2:
        return {}  # merges bring in work that was attributed on its own branch
    base = _parent(sha)
    attribution = classify(connect(), git("diff", "-U0", "--no-color", base, sha), repo_root(), _since())
    touched = [p for p in git("diff", "--name-only", "-z", base, sha).split("\0") if p]
    attribution["touched"] = touched
    session = attribution["sessions"][0] if attribution["sessions"] else None

    replaces = None
    if git("reflog", "-1", "--format=%gs", "HEAD").startswith("commit (amend)"):
        replaces = git("rev-parse", "HEAD@{1}").strip()

    note = {"session": session, "files": attribution["files"]}
    git("notes", "--ref", NOTES_REF, "add", "-f", "-F", "-", sha, input=json.dumps(note, indent=2))
    return LearnerStore().record_commit(sha, session, attribution, repo=repo_root(), replaces=replaces)
