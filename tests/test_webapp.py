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
    monkeypatch.setattr(claude, "recorded_run", lambda task_id, profile, allowed: (RUN, True))
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


def test_reset_rejects_paths_outside_the_sandbox(client, tmp_path):
    victim = tmp_path / "victim.db"
    victim.write_text("keep me")
    for bad in (str(tmp_path / "victim"), "../../victim", "novice/../../x"):
        assert client.post("/api/reset", json={"profile": bad}).status_code == 404
    assert victim.read_text() == "keep me"


def test_rate_limit_key_ignores_caller_supplied_forwarding(monkeypatch):
    from starlette.requests import Request

    def request(xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 1)})

    monkeypatch.setattr(webapp_app, "TRUSTED_PROXY_HOPS", 1)
    assert webapp_app.client_address(request("6.6.6.6, 203.0.113.7")) == "203.0.113.7"
    assert webapp_app.client_address(request("203.0.113.7")) == "203.0.113.7"
    monkeypatch.setattr(webapp_app, "TRUSTED_PROXY_HOPS", 0)
    assert webapp_app.client_address(request("6.6.6.6")) == "10.0.0.9"


def test_offline_review_never_credits_an_unverified_difference(client, monkeypatch):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    emptied = "\n".join(l for l in run["scaffold"].splitlines() if "TODO(learner)" not in l)
    data = client.post(f"/api/runs/{run['run_id']}/submit",
                       json={"profile": "developing", "text": emptied, "live": False}).json()
    assert data["gaps"][0]["verdict"] == "unverified" and data["events"] == []
    model = client.get("/api/model/developing").json()
    assert {c["name"]: c["level"] for c in model["concepts"]}["retry with exponential backoff"] == 2
    # The gap stays open, so a later live review can still credit it.
    review = lambda *a: {"gaps": [{"gap_id": "gap-1", "verdict": "as_expected", "feedback": "ok"}],
                         "other_changes": "", "caught_issue": False}
    monkeypatch.setattr(claude, "review", review)
    data = client.post(f"/api/runs/{run['run_id']}/submit", json={"profile": "developing", "text": emptied}).json()
    assert data["events"][0]["to"] == 3


def test_revealed_answers_count_as_claude_written(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    answer = client.post(f"/api/runs/{run['run_id']}/reveal",
                         json={"profile": "developing", "gap_id": "gap-1"}).json()["answer"]
    pasted = "\n".join(answer if "TODO(learner)" in l else l for l in run["scaffold"].splitlines())
    data = client.post(f"/api/runs/{run['run_id']}/submit",
                       json={"profile": "developing", "text": pasted, "live": False}).json()
    assert data["ownership"]["totals"]["human"] == 0


def test_concurrent_first_requests_all_see_a_seeded_learner(client):
    from concurrent.futures import ThreadPoolExecutor

    vid = "a" * 32
    def concepts(_):
        return len(webapp_app.snapshot(webapp_app.learner(vid, "developing"))["concepts"])
    with ThreadPoolExecutor(8) as pool:
        counts = list(pool.map(concepts, range(8)))
    assert len(set(counts)) == 1 and counts[0] >= 5


def test_replay_matches_the_current_plan_or_says_it_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "RECORDED", tmp_path)
    (tmp_path / "retry-developing.json").write_text('{"_plan": ["gap", "prediction", "retrieval"], "who": "dev"}')
    (tmp_path / "retry-proficient.json").write_text('{"_plan": ["critique", "what_if"], "who": "pro"}')
    out, matched = claude.recorded_run("retry", "developing", {"critique", "what_if"})
    assert matched and out["who"] == "pro"
    out, matched = claude.recorded_run("retry", "developing", {"critique", "gap"})
    assert not matched and out["who"] == "dev"


def test_recorded_run_notes_a_plan_mismatch(client, monkeypatch):
    monkeypatch.setattr(claude, "recorded_run", lambda task_id, profile, allowed: (RUN, False))
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing", "live": False}).json()
    assert "model has changed" in run["note"]


def test_unregistered_and_unclosed_spans_stay_in_the_scaffold():
    draft = "a = 1\n[[GAP gap-1]]\nb = 2\n[[/GAP]]\n[[GAP gap-9]]\nc = 3\n[[/GAP]]\n[[GAP gap-2]]\nd = 4\ne = 5"
    gaps = [{"gap_id": "gap-1", "description": "d", "hint": "h"}, {"gap_id": "gap-2", "description": "d", "hint": "h"}]
    out = drafts.scaffold(draft, gaps, "code")
    assert out.splitlines() == ["a = 1", "# TODO(learner) gap-1: d Hint: h", "c = 3", "d = 4", "e = 5"]
    baseline, _ = drafts.split(draft)
    assert baseline.splitlines() == ["a = 1", "b = 2", "c = 3", "d = 4", "e = 5"]


def test_index_versions_its_assets(client):
    res = client.get("/")
    assert res.headers["cache-control"] == "no-cache"
    assert f"/static/app.js?v={webapp_app.STATIC_VERSION}" in res.text
