"""Concept normalization and the sensitive-topic filter.

Claude names concepts in its own words, so the same idea arrives under many
names ("retry logic", "exponential backoff"). Every name is resolved to one
canonical concept through the alias table, with fuzzy matching as a fallback.
"""

import re
import sqlite3
from difflib import SequenceMatcher

from learner.db import now

FUZZY_THRESHOLD = 0.85

# Personal topics are never learning concepts. They are rejected before
# anything is stored. Claude is told the same in CLAUDE.md; this is the backstop.
SENSITIVE_PATTERNS = [
    r"\b(parental|maternity|paternity|sick|medical|bereavement) leave\b",
    r"\bperformance (review|improvement plan)\b",
    r"\bpip\b",
    r"\b(salary|compensation|pay) (negotiation|raise|review)\b",
    r"\b(termination|layoff|severance|disciplinary)\b",
    r"\b(harassment|discrimination|grievance)\b",
    r"\b(diagnosis|symptom|medication|therapy|mental health|pregnan\w*)\b",
    r"\b(divorce|custody|immigration status|visa sponsorship|lawsuit against)\b",
]
_SENSITIVE = re.compile("|".join(SENSITIVE_PATTERNS), re.IGNORECASE)


def is_sensitive(*texts: str) -> bool:
    return any(_SENSITIVE.search(t or "") for t in texts)


def normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    name = re.sub(r"[^a-z0-9+#.\s-]", " ", name.lower())
    name = re.sub(r"[-_\s]+", " ", name)
    return name.strip()


def slug(name: str) -> str:
    return normalize(name).replace(" ", "-")


def find(conn: sqlite3.Connection, name: str) -> str | None:
    """Return the canonical concept id for a name, or None."""
    key = normalize(name)
    row = conn.execute("SELECT concept_id FROM aliases WHERE alias = ?", (key,)).fetchone()
    if row:
        return row["concept_id"]
    best, best_score = None, 0.0
    for row in conn.execute("SELECT alias, concept_id FROM aliases"):
        score = SequenceMatcher(None, key, row["alias"]).ratio()
        if score > best_score:
            best, best_score = row["concept_id"], score
    return best if best_score >= FUZZY_THRESHOLD else None


def resolve(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    domain: str = "code",
    aliases: list[str] | None = None,
) -> tuple[str, bool]:
    """Map a name (and its aliases) to a canonical concept, creating it if new.

    Returns (concept_id, created).
    """
    names = [name, *(aliases or [])]
    concept_id = next((cid for n in names if (cid := find(conn, n))), None)
    created = concept_id is None
    if created:
        concept_id = slug(name)
        conn.execute(
            "INSERT INTO concepts (id, name, description, domain, created_at) VALUES (?, ?, ?, ?, ?)",
            (concept_id, name, description, domain, now()),
        )
        conn.execute("INSERT INTO concept_state (concept_id) VALUES (?)", (concept_id,))
    for n in names:
        conn.execute(
            "INSERT OR IGNORE INTO aliases (alias, concept_id) VALUES (?, ?)",
            (normalize(n), concept_id),
        )
    return concept_id, created
