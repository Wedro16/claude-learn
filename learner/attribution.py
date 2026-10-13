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
import os
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
    # Resolve symlinks so paths compare with git's repository root.
    path = os.path.realpath(path)
    lines = [l for l in text.splitlines() if significant(l)]
    for line in lines:
        # A rewrite refreshes the time, so recent work is never aged out.
        conn.execute(
            "INSERT INTO claude_lines (session_id, path, line_hash, line_text, at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (session_id, path, line_hash) DO UPDATE SET at = excluded.at",
            (session_id, path, line_hash(line), line.strip(), now()),
        )
    return len(lines)


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_path(path: str) -> str:
    """Decode a path Git quoted C-style (non-ASCII, quotes or control characters)."""
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode()
            i += 1
        elif body[i + 1 : i + 4].isdigit() and len(body[i + 1 : i + 4]) == 3:
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            out.append(_ESCAPES.get(body[i + 1], ord(body[i + 1])))
            i += 2
    return out.decode("utf-8", errors="replace")


def parse_added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Map each file in a unified diff (``git diff -U0``) to its added (line_no, text)."""
    files: dict[str, list[tuple[int, str]]] = {}
    path, line_no = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = unquote_path(raw[4:])
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


def classify(conn: sqlite3.Connection, diff: str, repo_root: str, since: str) -> dict:
    """Classify every significant added line in a diff.

    Only lines Claude wrote in this repository (absolute paths under ``repo_root``)
    since ``since`` are candidates, whichever session wrote them.

    Returns the attribution note: per-file ranges for each class, totals, and the
    sessions whose lines matched, most recent first.
    """
    prefix = repo_root.rstrip("/") + "/"
    rows = conn.execute(
        "SELECT session_id, path, line_hash, line_text, at FROM claude_lines"
        " WHERE substr(path, 1, ?) = ? AND at >= ?",
        (len(prefix), prefix, since),
    ).fetchall()
    exact = {}
    for r in rows:
        exact[r["line_hash"]] = max(exact.get(r["line_hash"], ("", "")), (r["at"], r["session_id"]))
    by_path: dict[str, list[tuple[str, str, str]]] = {}
    for r in rows:
        by_path.setdefault(r["path"][len(prefix):], []).append((r["line_text"], r["at"], r["session_id"]))
    matched: dict[str, str] = {}

    files, totals = {}, {"claude": 0, "claude_modified": 0, "human": 0}
    for path, added in parse_added_lines(diff).items():
        # Prefer candidates Claude wrote in this file; fall back to the whole repository.
        candidates = by_path.get(path) or [t for v in by_path.values() for t in v]
        buckets: dict[str, list[int]] = {"claude": [], "claude_modified": [], "human": []}
        for no, text in added:
            if not significant(text):
                continue
            hit = exact.get(line_hash(text))
            if hit is None:
                hit = next(
                    ((at, sid) for c, at, sid in candidates
                     if SequenceMatcher(None, text.strip(), c).ratio() >= MODIFIED_THRESHOLD),
                    None,
                )
                cls = "claude_modified" if hit else "human"
            else:
                cls = "claude"
            if hit:
                matched[hit[1]] = max(matched.get(hit[1], ""), hit[0])
            buckets[cls].append(no)
            totals[cls] += 1
        if any(buckets.values()):
            files[path] = {k: _ranges(v) for k, v in buckets.items() if v}
    sessions = sorted(matched, key=matched.get, reverse=True)
    return {"files": files, "totals": totals, "sessions": sessions}
