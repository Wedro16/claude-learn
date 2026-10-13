"""SQLite storage for the learner model.

The database lives outside the repository (default ``~/.claude-learn/learner.db``)
so learner data is never committed. Set ``CLAUDE_LEARN_HOME`` to move it.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    domain      TEXT NOT NULL DEFAULT 'code',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
    alias      TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL REFERENCES concepts(id)
);
CREATE TABLE IF NOT EXISTS relations (
    from_id TEXT NOT NULL REFERENCES concepts(id),
    to_id   TEXT NOT NULL REFERENCES concepts(id),
    type    TEXT NOT NULL CHECK (type IN ('prerequisite', 'part_of', 'used_with')),
    PRIMARY KEY (from_id, to_id, type)
);
CREATE TABLE IF NOT EXISTS concept_state (
    concept_id       TEXT PRIMARY KEY REFERENCES concepts(id),
    level            INTEGER NOT NULL DEFAULT 0,
    track            TEXT NOT NULL DEFAULT 'one_off' CHECK (track IN ('one_off', 'skill')),
    track_pinned     INTEGER NOT NULL DEFAULT 0,
    dial             TEXT NOT NULL DEFAULT 'full' CHECK (dial IN ('off', 'light', 'full')),
    mode             TEXT NOT NULL DEFAULT 'about' CHECK (mode IN ('about', 'how_to')),
    last_evidence_at TEXT
);
CREATE TABLE IF NOT EXISTS occurrences (
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    context_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (concept_id, context_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    cwd          TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    task_summary TEXT,
    dial_override TEXT CHECK (dial_override IN ('off', 'light', 'full'))
);
CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id  TEXT NOT NULL REFERENCES concepts(id),
    signal_type TEXT NOT NULL,
    passed      INTEGER,
    source      TEXT NOT NULL CHECK (source IN ('conversation', 'repo')),
    ref         TEXT,
    detail      TEXT,
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     TEXT NOT NULL,
    concept_id  TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    evidence_id INTEGER,
    from_level  INTEGER NOT NULL,
    to_level    INTEGER NOT NULL,
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id  TEXT NOT NULL,
    level       INTEGER NOT NULL,
    statement   TEXT NOT NULL,
    made_at     TEXT NOT NULL,
    outcome     INTEGER,
    resolved_at TEXT,
    evidence_id INTEGER
);
CREATE TABLE IF NOT EXISTS interventions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    concept_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    response   TEXT,
    outcome    TEXT NOT NULL CHECK (outcome IN ('correct', 'partial', 'incorrect', 'skipped', 'pending')),
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS withheld (
    item_id     TEXT PRIMARY KEY,
    baseline_id INTEGER NOT NULL REFERENCES baselines(id),
    concept_id  TEXT,
    description TEXT NOT NULL,
    hint        TEXT,
    location    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'committed', 'as_expected', 'different', 'handed_back')),
    commit_sha  TEXT,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_lines (
    session_id TEXT NOT NULL,
    path       TEXT NOT NULL,
    line_hash  TEXT NOT NULL,
    line_text  TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (session_id, path, line_hash)
);
CREATE TABLE IF NOT EXISTS commits (
    sha            TEXT PRIMARY KEY,
    session_id     TEXT,
    claude_lines   INTEGER NOT NULL,
    modified_lines INTEGER NOT NULL,
    human_lines    INTEGER NOT NULL,
    files          TEXT NOT NULL,
    tagged         INTEGER NOT NULL DEFAULT 0,
    at             TEXT NOT NULL
);
"""


def home() -> Path:
    return Path(os.environ.get("CLAUDE_LEARN_HOME", Path.home() / ".claude-learn"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    if path is None:
        home().mkdir(parents=True, exist_ok=True)
        path = home() / "learner.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
