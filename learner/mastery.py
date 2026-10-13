"""Phase 1 mastery model: explicit, observable rules.

Every rule evaluation is written to ``rule_observations`` (fired or not), and
reaching level 3 or higher records a prediction that is later checked against
what actually happens. Those logs are the training data for the Bayesian model
that replaces these rules in phase 2.

Levels follow the ownership ladder: 0 exposed, 1 explain, 2 predict,
3 modify, 4 critique.
"""

import sqlite3
from datetime import timedelta

from learner.db import now, parse

PROMOTION_WINDOW = timedelta(days=30)
PROMOTION_MIN_OCCURRENCES = 2
DECAY_AFTER = timedelta(days=60)

CONVERSATION_SIGNALS = {
    "self_explanation", "prediction", "what_if", "conceptual_reask",
    "reference_lookup", "question", "critique", "re_explain_request",
}
REPO_SIGNALS = {"unassisted_use", "gap_completion", "substantive_fix", "unchanged_merge", "draft_ownership"}
SIGNALS = CONVERSATION_SIGNALS | REPO_SIGNALS

LEVEL_NAMES = {0: "exposed", 1: "explain", 2: "predict", 3: "modify", 4: "critique"}


def _level(conn, concept_id: str) -> int:
    return conn.execute("SELECT level FROM concept_state WHERE concept_id = ?", (concept_id,)).fetchone()["level"]


def _observe(conn, rule_id, concept_id, fired, evidence_id, from_level, to_level, at):
    conn.execute(
        "INSERT INTO rule_observations (rule_id, concept_id, passed, evidence_id, from_level, to_level, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rule_id, concept_id, int(fired), evidence_id, from_level, to_level, at),
    )


def _recent(conn, concept_id, types, limit):
    marks = ",".join("?" * len(types))
    return conn.execute(
        f"SELECT * FROM evidence WHERE concept_id = ? AND signal_type IN ({marks}) ORDER BY id DESC LIMIT ?",
        (concept_id, *types, limit),
    ).fetchall()


def _rules(conn, concept_id: str, ev: sqlite3.Row, level: int) -> list[tuple[str, bool, int]]:
    """Evaluate the rules that apply to one piece of evidence.

    Returns (rule_id, fired, new_level) for every rule evaluated, in order.
    """
    sig, passed, detail = ev["signal_type"], ev["passed"], ev["detail"]
    out = []

    def rule(rule_id, fired, target):
        nonlocal level
        if fired:
            level = target
        out.append((rule_id, fired, level))

    if sig in ("self_explanation", "prediction"):
        rule("L1.reach", bool(passed), max(level, 1))
    if sig == "conceptual_reask":
        rule("L1.drop.reask", level >= 1, level - 1)
    if sig == "what_if":
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT ref) AS n FROM evidence"
            " WHERE concept_id = ? AND signal_type = 'what_if' AND passed = 1",
            (concept_id,),
        ).fetchone()["n"]
        rule("L2.reach", bool(passed) and sessions >= 2, max(level, 2))
    if sig in ("prediction", "what_if") and not passed:
        last_two = _recent(conn, concept_id, ("prediction", "what_if"), 2)
        two_wrong = len(last_two) == 2 and all(not r["passed"] for r in last_two)
        rule("L2.drop.wrong_twice", two_wrong and level >= 2, level - 1)
    if sig == "unassisted_use":
        rule("L3.reach.unassisted", bool(passed), max(level, 3))
    if sig == "gap_completion":
        rule("L3.reach.gap", detail in ("as_expected", "different"), max(level, 3))
        rule("L3.drop.handed_back", detail == "handed_back" and level >= 3, level - 1)
    if sig in ("substantive_fix", "critique"):
        rule("L4.reach", bool(passed), 4)
    if sig == "unchanged_merge":
        last_two = _recent(conn, concept_id, tuple(REPO_SIGNALS), 2)
        repeated = len(last_two) == 2 and all(r["signal_type"] == "unchanged_merge" for r in last_two)
        rule("L4.drop.unchanged_merges", repeated and level >= 4, level - 1)
    return out


def _resolve_predictions(conn, concept_id, ev, at):
    sig, passed, detail = ev["signal_type"], ev["passed"], ev["detail"]
    if sig == "unassisted_use":
        outcome = int(bool(passed))
    elif sig == "gap_completion":
        outcome = int(detail in ("as_expected", "different"))
    else:
        return
    conn.execute(
        "UPDATE predictions SET outcome = ?, resolved_at = ?, evidence_id = ?"
        " WHERE concept_id = ? AND outcome IS NULL",
        (outcome, at, ev["id"], concept_id),
    )


def apply_evidence(conn: sqlite3.Connection, evidence_id: int) -> dict:
    """Run the rules for one evidence row and update the concept's level."""
    ev = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    concept_id, at = ev["concept_id"], ev["at"]
    start = _level(conn, concept_id)

    # Check open predictions before this evidence can change the level.
    _resolve_predictions(conn, concept_id, ev, at)

    level = start
    fired = []
    for rule_id, did_fire, new_level in _rules(conn, concept_id, ev, level):
        _observe(conn, rule_id, concept_id, did_fire, evidence_id, level, new_level, at)
        if did_fire:
            fired.append(rule_id)
        level = new_level

    conn.execute(
        "UPDATE concept_state SET level = ?, last_evidence_at = ? WHERE concept_id = ?",
        (level, at, concept_id),
    )
    if level >= 3 and level > start:
        conn.execute(
            "INSERT INTO predictions (concept_id, level, statement, made_at) VALUES (?, ?, ?, ?)",
            (concept_id, level, "next unassisted task on this concept succeeds", at),
        )
    return {"concept_id": concept_id, "from_level": start, "to_level": level, "rules_fired": fired}


def apply_decay(conn: sqlite3.Connection, at: str | None = None) -> list[str]:
    """Drop one level for each concept with no evidence or decay in 60 days."""
    at = at or now()
    decayed = []
    for row in conn.execute("SELECT concept_id, level, last_evidence_at FROM concept_state WHERE level > 0"):
        last_decay = conn.execute(
            "SELECT MAX(at) AS at FROM rule_observations WHERE concept_id = ? AND rule_id = 'decay' AND passed = 1",
            (row["concept_id"],),
        ).fetchone()["at"]
        anchor = max(filter(None, [row["last_evidence_at"], last_decay]), default=None)
        if anchor and parse(at) - parse(anchor) >= DECAY_AFTER:
            new = row["level"] - 1
            conn.execute("UPDATE concept_state SET level = ? WHERE concept_id = ?", (new, row["concept_id"]))
            _observe(conn, "decay", row["concept_id"], True, None, row["level"], new, at)
            decayed.append(row["concept_id"])
    return decayed


def update_track(conn: sqlite3.Connection, concept_id: str, at: str | None = None) -> dict:
    """Promote a concept to a skill track once it recurs within the window."""
    at = at or now()
    since = (parse(at) - PROMOTION_WINDOW).isoformat(timespec="seconds")
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM occurrences WHERE concept_id = ? AND at >= ?",
        (concept_id, since),
    ).fetchone()["n"]
    state = conn.execute("SELECT track, track_pinned FROM concept_state WHERE concept_id = ?", (concept_id,)).fetchone()
    track = state["track"]
    if not state["track_pinned"] and track == "one_off" and count >= PROMOTION_MIN_OCCURRENCES:
        track = "skill"
        conn.execute("UPDATE concept_state SET track = 'skill' WHERE concept_id = ?", (concept_id,))
    return {"track": track, "occurrences_30d": count}


def non_learning_flags(conn: sqlite3.Connection, at: str | None = None) -> list[dict]:
    """Detect behaviors that suggest learning is not happening on skill tracks."""
    at = at or now()
    since = (parse(at) - PROMOTION_WINDOW).isoformat(timespec="seconds")
    flags = []
    skills = conn.execute("SELECT concept_id, level FROM concept_state WHERE track = 'skill'").fetchall()
    for s in skills:
        cid, level = s["concept_id"], s["level"]

        def count(sig, extra="", *params):
            return conn.execute(
                f"SELECT COUNT(*) AS n FROM evidence WHERE concept_id = ? AND signal_type = ? AND at >= ? {extra}",
                (cid, sig, since, *params),
            ).fetchone()["n"]

        # Unchanged merges are fine once a concept is mastered.
        if level < 3 and count("unchanged_merge") >= 2:
            flags.append({"concept_id": cid, "behavior": "unchanged_merges", "suggests": "delegating without engaging"})
        if count("gap_completion", "AND detail = 'handed_back'") >= 2:
            flags.append({"concept_id": cid, "behavior": "gaps_handed_back", "suggests": "avoiding the generative work"})
        if count("re_explain_request") >= 2:
            flags.append({"concept_id": cid, "behavior": "re_explain_requests", "suggests": "not retaining"})

        recent = conn.execute(
            "SELECT outcome FROM interventions WHERE concept_id = ? AND at >= ? AND outcome != 'pending'",
            (cid, since),
        ).fetchall()
        if len(recent) >= 3 and all(r["outcome"] == "skipped" for r in recent):
            flags.append({"concept_id": cid, "behavior": "interventions_skipped", "suggests": "dial set wrong, or disengaged"})

        solo = [
            int(r["detail"]) for r in conn.execute(
                "SELECT detail FROM evidence WHERE concept_id = ? AND signal_type = 'question' AND at >= ?"
                " ORDER BY at", (cid, since),
            ) if r["detail"] and r["detail"].isdigit()
        ]
        if len(solo) >= 4:
            half = len(solo) // 2
            if max(solo[half:]) <= max(solo[:half]):
                flags.append({"concept_id": cid, "behavior": "flat_question_complexity", "suggests": "plateau"})
    return flags
