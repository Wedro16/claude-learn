"""Demo content: task cards and seeded learner profiles.

Everything a reviewer needs is bundled here, so the demo works without any
data, documents or domain expertise of their own. Learner histories are
synthetic and labeled as such in the UI.
"""

from datetime import datetime, timedelta, timezone

from learner.store import LearnerStore

RETRY = "retry with exponential backoff"
ERRORS = "transient vs permanent errors"
RATIONALE = "writing design rationale"
JOINS = "SQL join semantics"
DI = "dependency injection"

TASKS = {
    "retry": {
        "id": "retry",
        "title": "Make a flaky API call resilient",
        "kind": "code",
        "language": "python",
        "concepts": [RETRY, ERRORS],
        "request": (
            "fetch_forecast() fails about half the time because the forecast service is flaky. "
            "Make it retry with exponential backoff and jitter, give up after a sensible number of "
            "attempts, and don't retry errors that can never succeed. Keep it dependency-free."
        ),
        "material": '''import random
import time


class FlakyForecastAPI:
    """Stand-in for a weather service that fails intermittently."""

    def __init__(self, failure_rate=0.5, seed=None):
        self.failure_rate = failure_rate
        self.rng = random.Random(seed)

    def get(self, city):
        if not city:
            raise ValueError("city is required")
        if self.rng.random() < self.failure_rate:
            raise ConnectionError("forecast service unavailable")
        return {"city": city, "high_c": 21, "low_c": 12}


def fetch_forecast(api, city):
    return api.get(city)
''',
    },
    "rationale": {
        "id": "rationale",
        "title": "Explain a design decision to a reviewer",
        "kind": "writing",
        "language": "markdown",
        "concepts": [RATIONALE],
        "request": (
            "Draft the 'Design decisions' section of the README for the forecast cache, for a "
            "teammate reviewing the pull request. Use the notes below; keep it under 250 words."
        ),
        "material": """Notes from the team:
- Added a cache in front of the forecast API; it was getting ~40 requests/min for the same 12 cities.
- TTL is 10 minutes. Upstream forecasts only change hourly; 10 min keeps us fresh after an update.
- In-process LRU (max 256 entries), not Redis: we run one instance, and Redis is another thing to operate.
- On upstream errors we serve the stale entry if we have one (marked stale), instead of failing.
- Revisit Redis if we scale past one instance.
""",
    },
}

PROFILES = {
    "novice": {
        "id": "novice",
        "name": "Sam",
        "summary": "New to both topics. Has seen retries mentioned, never written one.",
    },
    "developing": {
        "id": "developing",
        "name": "Priya",
        "summary": "Building the skill: explained backoff correctly twice, still shaky on which errors to retry.",
    },
    "proficient": {
        "id": "proficient",
        "name": "Alex",
        "summary": "Has written retry logic unassisted and caught a bug in Claude's version last month.",
    },
}


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _occur(store: LearnerStore, name: str, contexts: list[tuple[str, float]], **kwargs) -> str:
    """Record a concept with backdated occurrences (one per past session or commit)."""
    cid = store.record_concept(name, **kwargs)["concept_id"]
    for context, days in contexts:
        store.conn.execute(
            "INSERT OR IGNORE INTO occurrences (concept_id, context_id, at) VALUES (?, ?, ?)",
            (cid, context, _ago(days)),
        )
    store.conn.commit()
    return cid


def _ev(store, concept, signal, passed=None, detail=None, ref=None, days=1.0):
    store.record_evidence(concept, signal, passed, detail, ref=ref, at=_ago(days))


def seed(store: LearnerStore, profile: str) -> None:
    """Give a fresh learner store a plausible six-week history for one profile."""
    _occur(store, DI, [("past-di", 30)], description="Passing collaborators in rather than constructing them")
    _ev(store, DI, "question", detail="1", ref="past-di", days=30)

    if profile == "novice":
        _occur(store, RETRY, [("past-n1", 6), ("past-n2", 2)], description="Retrying failed calls with growing delays")
        _ev(store, RETRY, "question", detail="1", ref="past-n1", days=6)
        _ev(store, RETRY, "question", detail="1", ref="past-n2", days=2)
        _occur(store, RATIONALE, [("past-n3", 3), ("past-n4", 1)], domain="writing",
               description="Explaining a decision: what was chosen, the alternatives, the trade-offs")
        _ev(store, RATIONALE, "question", detail="1", ref="past-n3", days=3)

    elif profile == "developing":
        _occur(store, RETRY, [("past-d1", 30), ("past-d2", 18), ("past-d3", 9)],
               description="Retrying failed calls with growing delays")
        _ev(store, RETRY, "question", detail="1", ref="past-d1", days=30)
        _ev(store, RETRY, "self_explanation", True, ref="past-d1", days=30)
        _ev(store, RETRY, "question", detail="2", ref="past-d2", days=18)
        _ev(store, RETRY, "what_if", True, ref="past-d2", days=18)
        _ev(store, RETRY, "what_if", True, ref="past-d3", days=9)
        _ev(store, RETRY, "question", detail="3", ref="past-d3", days=9)
        _occur(store, ERRORS, [("past-d2", 18), ("past-d3", 9)],
               description="Which failures can succeed on retry and which never will")
        _ev(store, ERRORS, "prediction", False, ref="past-d2", days=18)
        _ev(store, ERRORS, "self_explanation", True, ref="past-d3", days=9)
        _occur(store, RATIONALE, [("past-d4", 12), ("past-d5", 4)], domain="writing",
               description="Explaining a decision: what was chosen, the alternatives, the trade-offs")
        _ev(store, RATIONALE, "self_explanation", True, ref="past-d4", days=12)
        _occur(store, JOINS, [("past-d6", 20), ("past-d7", 11)], description="How inner and outer joins treat missing rows")
        _ev(store, JOINS, "self_explanation", True, ref="past-d6", days=20)
        _ev(store, JOINS, "re_explain_request", ref="past-d7", days=11)
        _ev(store, JOINS, "re_explain_request", ref="past-d7", days=10)

    elif profile == "proficient":
        _occur(store, RETRY, [("past-p1", 40), ("past-p2", 25), ("commit-a1f3", 14)],
               description="Retrying failed calls with growing delays")
        _ev(store, RETRY, "self_explanation", True, ref="past-p1", days=40)
        _ev(store, RETRY, "what_if", True, ref="past-p1", days=40)
        _ev(store, RETRY, "what_if", True, ref="past-p2", days=25)
        _ev(store, RETRY, "unassisted_use", True, ref="commit-a1f3", days=14)
        _ev(store, RETRY, "substantive_fix", True, detail="caught unbounded delay growth", ref="past-p2", days=13)
        _ev(store, RETRY, "question", detail="4", ref="past-p2", days=13)
        _occur(store, ERRORS, [("past-p2", 25), ("commit-a1f3", 14)],
               description="Which failures can succeed on retry and which never will")
        _ev(store, ERRORS, "what_if", True, ref="past-p2", days=25)
        _ev(store, ERRORS, "what_if", True, ref="commit-a1f3", days=14)
        _ev(store, ERRORS, "unassisted_use", True, ref="commit-a1f3", days=14)
        _occur(store, RATIONALE, [("past-p3", 20), ("past-p4", 6)], domain="writing",
               description="Explaining a decision: what was chosen, the alternatives, the trade-offs")
        _ev(store, RATIONALE, "self_explanation", True, ref="past-p3", days=20)
        _ev(store, RATIONALE, "what_if", True, ref="past-p3", days=20)
        _ev(store, RATIONALE, "what_if", True, ref="past-p4", days=6)

    # Recorded occurrences decide tracks; refresh them now that history exists.
    from learner import mastery

    for (cid,) in store.conn.execute("SELECT concept_id FROM concept_state").fetchall():
        mastery.update_track(store.conn, cid)
    store.conn.commit()
