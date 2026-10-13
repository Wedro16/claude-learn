"""The demo web app, with Claude replaced by a fake so tests never call the API."""

import pytest
from fastapi.testclient import TestClient

from webapp import app as webapp_app
from webapp import claude, drafts

DRAFT = """\
import random
import time


def fetch_forecast(api, city, attempts=5, base=0.5, cap=8.0):
    for attempt in range(attempts):
        try:
            return api.get(city)
        except ValueError:
            raise
        except ConnectionError:
            if attempt == attempts - 1:
                raise
            [[GAP gap-1]]
            delay = min(cap, base * 2 ** attempt)
            time.sleep(random.uniform(0, delay))
            [[/GAP]]
"""

RUN = {
    "orientation": "Retries give transient failures another chance.\n\n- backoff: wait longer each time",
    "decision_notes": ["Full jitter spreads retries out.", "ValueError is never retried."],
    "prediction_prompt": "What happens to the total wait if the service is down for good?",
    "draft": DRAFT,
    "gaps": [{"gap_id": "gap-1", "concept": "retry with exponential backoff",
              "description": "Compute the delay and sleep.", "hint": "Double each time, but cap it."}],
    "critique_invitation": "Is full jitter right when only one client retries?",
    "takeaway": "Retry only what can succeed, and wait longer each time.",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_app, "DATA_DIR", tmp_path / "demo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(claude, "run_task", lambda task, context: {**RUN, "gaps": [dict(g) for g in RUN["gaps"]]})
    monkeypatch.setattr(claude, "grade", lambda *a: {"outcome": "correct", "feedback": "Yes: it stays bounded."})
    return TestClient(webapp_app.app)


def test_split_and_scaffold_are_consistent():
    baseline, spans = drafts.split(DRAFT)
    assert "[[GAP" not in baseline and "delay = min(cap" in baseline
    assert spans["gap-1"].strip().startswith("delay = min")
    scaffold = drafts.scaffold(DRAFT, RUN["gaps"], "code")
    assert "            # TODO(learner) gap-1: Compute the delay and sleep. Hint: Double each time, but cap it." in scaffold
    assert "delay = min" not in scaffold
    assert drafts.unfilled(scaffold) == {"gap-1"}


def test_unbalanced_markers_keep_the_text():
    baseline, spans = drafts.split("a = 1\n[[GAP gap-1]]\nb = 2\n")
    assert baseline == "a = 1\nb = 2" and spans == {}


def test_ownership_classifies_lines():
    owned = drafts.ownership("total = price * quantity\nreturn total", "total = price * quantity * 1.2\nreturn total\nlog(total)")
    assert [l["cls"] for l in owned["lines"]] == ["claude_modified", "claude", "human"]
    assert owned["learner_share"] == 0.67


def test_catalog_lists_bundled_content(client):
    data = client.get("/api/catalog").json()
    assert {t["id"] for t in data["tasks"]} == {"retry", "rationale"}
    assert {p["id"] for p in data["profiles"]} == {"novice", "developing", "proficient"}


def test_seeded_profiles_have_distinct_levels(client):
    levels = {}
    for p in ("novice", "developing", "proficient"):
        model = client.get(f"/api/model/{p}").json()
        levels[p] = {c["name"]: c["level"] for c in model["concepts"]}["retry with exponential backoff"]
    assert levels == {"novice": 0, "developing": 2, "proficient": 4}


def test_developing_learner_gets_gap_and_question(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    assert run["source"] == "live"
    assert run["prediction_prompt"] and run["gaps"][0]["gap_id"] == "gap-1"
    assert "TODO(learner) gap-1" in run["scaffold"] and "delay = min" not in run["scaffold"]
    assert run["orientation"] == "" and run["critique_invitation"] == ""
    assert "baseline" not in run and "spans" not in run  # the answer never reaches the browser


def test_plan_not_model_decides_interventions(client):
    novice = client.post("/api/run", json={"task_id": "retry", "profile": "novice"}).json()
    assert novice["gaps"] == [] and novice["prediction_prompt"] == "" and novice["orientation"]
    assert "delay = min" in novice["scaffold"]  # full worked example
    pro = client.post("/api/run", json={"task_id": "retry", "profile": "proficient"}).json()
    assert pro["critique_invitation"] and pro["gaps"] == []


def test_filling_the_gap_raises_the_level(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    filled = run["scaffold"].replace(
        "            # TODO(learner) gap-1: Compute the delay and sleep. Hint: Double each time, but cap it.",
        "            delay = min(cap, base * 2 ** attempt)\n            time.sleep(random.uniform(0, delay))",
    )
    # Offline review: exact match with the baseline is "as expected".
    data = client.post(f"/api/runs/{run['run_id']}/submit",
                       json={"profile": "developing", "text": filled, "live": False}).json()
    assert data["source"] == "offline" and data["gaps"][0]["verdict"] == "as_expected"
    assert data["events"][0]["from"] == 2 and data["events"][0]["to"] == 3
    assert data["ownership"]["totals"]["human"] >= 1


def test_prediction_answer_becomes_evidence(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    data = client.post(f"/api/runs/{run['run_id']}/predict",
                       json={"profile": "developing", "answer": "Bounded by the cap times attempts"}).json()
    assert data["outcome"] == "correct" and data["source"] == "live"


def test_reveal_hands_back_and_returns_the_answer(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    data = client.post(f"/api/runs/{run['run_id']}/reveal", json={"profile": "developing", "gap_id": "gap-1"}).json()
    assert "delay = min(cap" in data["answer"]


def test_recorded_mode_needs_no_live_call(client, monkeypatch):
    monkeypatch.setattr(claude, "recorded_run", lambda task_id, profile: RUN)
    monkeypatch.setattr(claude, "run_task", lambda *a: pytest.fail("live call in recorded mode"))
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing", "live": False}).json()
    assert run["source"] == "recorded"


def test_visitors_are_isolated(client):
    other = TestClient(webapp_app.app)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    client.post(f"/api/runs/{run['run_id']}/reveal", json={"profile": "developing", "gap_id": "gap-1"})
    a = client.get("/api/model/developing").json()
    b = other.get("/api/model/developing").json()
    retry = lambda m: next(c for c in m["concepts"] if c["name"] == "retry with exponential backoff")
    assert retry(a)["evidence"] > retry(b)["evidence"]


def test_reset_restores_seeded_history(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    client.post(f"/api/runs/{run['run_id']}/reveal", json={"profile": "developing", "gap_id": "gap-1"})
    model = client.post("/api/reset", json={"profile": "developing"}).json()
    assert {c["name"]: c["level"] for c in model["concepts"]}["retry with exponential backoff"] == 2


def test_a_fenced_draft_is_unwrapped():
    fenced = "```python\n" + DRAFT + "```"
    assert drafts.split(fenced) == drafts.split(DRAFT)
    assert not drafts.scaffold(fenced, RUN["gaps"], "code").startswith("```")
