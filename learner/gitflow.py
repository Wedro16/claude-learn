"""Commit-time provenance: trailers, attribution notes, and commit records.

Called from the git hooks in ``githooks/``. Learner data stays in the local
store; the repository only receives trailers and line ranges.
"""

import json
import subprocess
from datetime import timedelta
from pathlib import Path

from learner.attribution import classify
from learner.db import connect, now, parse
from learner.store import LearnerStore, current_session_id

NOTES_REF = "claude-attribution"
SESSION_LOOKBACK = timedelta(days=7)


def git(*args: str, input: str | None = None) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, input=input).stdout


def _pending_path() -> Path:
    return Path(git("rev-parse", "--git-dir").strip()) / "claude-attribution-pending.json"


def recent_sessions(conn) -> list[str]:
    since = (parse(now()) - SESSION_LOOKBACK).isoformat(timespec="seconds")
    return [r["id"] for r in conn.execute("SELECT id FROM sessions WHERE started_at >= ?", (since,))]


def prepare_commit_msg(msg_file: str, source: str | None = None) -> dict:
    """Classify the staged diff; add trailers when Claude-originated lines are present."""
    if source == "merge":
        return {}
    conn = connect()
    attribution = classify(conn, recent_sessions(conn), git("diff", "--cached", "-U0", "--no-color"))
    session = current_session_id()
    attribution["session"] = session
    _pending_path().write_text(json.dumps(attribution))
    t = attribution["totals"]
    if t["claude"] + t["claude_modified"] > 0:
        trailers = ["--trailer", "Assisted-by: Claude"]
        if session:
            trailers += ["--trailer", f"Claude-Session: {session}"]
        git("interpret-trailers", "--in-place", "--if-exists", "doNothing", *trailers, msg_file)
    return attribution


def post_commit() -> dict:
    """Attach the attribution note to HEAD and record the commit for analysis."""
    pending = _pending_path()
    if not pending.exists():
        return {}
    attribution = json.loads(pending.read_text())
    pending.unlink()
    sha = git("rev-parse", "HEAD").strip()
    note = {"session": attribution.get("session"), "files": attribution["files"]}
    git("notes", "--ref", NOTES_REF, "add", "-f", "-F", "-", sha, input=json.dumps(note, indent=2))
    return LearnerStore().record_commit(sha, attribution.get("session"), attribution)
