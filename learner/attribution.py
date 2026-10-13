"""Line-level attribution of committed changes.

Claude Code's PostToolUse hook records every line Claude writes. At commit
time each added line is classified by matching it against those lines:

- ``claude``:          identical to a line Claude wrote
- ``claude_modified``: close to a line Claude wrote, but edited
- ``human``:           no close match

Matching on line content rather than line numbers survives edits that shift
lines around.
"""

import hashlib
import re
import sqlite3
from difflib import SequenceMatcher

from learner.db import now

MODIFIED_THRESHOLD = 0.6


def line_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def significant(text: str) -> bool:
    """Blank lines and lone brackets say nothing about who wrote the code."""
    return len(text.strip()) > 2


def record_claude_lines(conn: sqlite3.Connection, session_id: str, path: str, text: str) -> int:
    lines = [l for l in text.splitlines() if significant(l)]
    for line in lines:
        conn.execute(
            "INSERT OR IGNORE INTO claude_lines (session_id, path, line_hash, line_text, at) VALUES (?, ?, ?, ?, ?)",
            (session_id, path, line_hash(line), line.strip(), now()),
        )
    return len(lines)


def parse_added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Map each file in a unified diff (``git diff -U0``) to its added (line_no, text)."""
    files: dict[str, list[tuple[int, str]]] = {}
    path, line_no = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
            if path is not None:
                files.setdefault(path, [])
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path is not None:
            files[path].append((line_no, raw[1:]))
            line_no += 1
    return files


def _ranges(numbers: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(numbers):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def classify(conn: sqlite3.Connection, session_ids: list[str], diff: str) -> dict:
    """Classify every significant added line in a diff.

    Returns the attribution note: per-file ranges for each class plus totals.
    """
    marks = ",".join("?" * len(session_ids)) or "''"
    rows = conn.execute(
        f"SELECT path, line_hash, line_text FROM claude_lines WHERE session_id IN ({marks})",
        session_ids,
    ).fetchall()
    exact = {r["line_hash"] for r in rows}
    by_path: dict[str, list[str]] = {}
    for r in rows:
        by_path.setdefault(r["path"], []).append(r["line_text"])

    files, totals = {}, {"claude": 0, "claude_modified": 0, "human": 0}
    for path, added in parse_added_lines(diff).items():
        # Prefer candidates Claude wrote in this file; fall back to all of them.
        candidates = next((v for p, v in by_path.items() if p.endswith(path) or path.endswith(p)), None)
        candidates = candidates or [t for v in by_path.values() for t in v]
        buckets: dict[str, list[int]] = {"claude": [], "claude_modified": [], "human": []}
        for no, text in added:
            if not significant(text):
                continue
            if line_hash(text) in exact:
                cls = "claude"
            elif any(SequenceMatcher(None, text.strip(), c).ratio() >= MODIFIED_THRESHOLD for c in candidates):
                cls = "claude_modified"
            else:
                cls = "human"
            buckets[cls].append(no)
            totals[cls] += 1
        if any(buckets.values()):
            files[path] = {k: _ranges(v) for k, v in buckets.items() if v}
    return {"files": files, "totals": totals}
