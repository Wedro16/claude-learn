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
    monkeypatch.setattr(webapp_app, "limiter", webapp_app.Limiter())  # each test starts under the live-call cap
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
    assert {p["id"] for p in data["profiles"]} == {"you", "novice", "developing", "proficient"}


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


def _discuss_reply(**kw):
    out = {"reply": "Right: the cap bounds the wait.", "kind": "explanation", "understanding": "correct",
           "concept": "retry with exponential backoff", "solo": 0, "pushback_valid": False}
    return {**out, **kw}


def test_discussion_turns_reasoning_into_evidence(client, monkeypatch):
    seen = {}

    def fake(task, baseline, text, open_gaps, history, message):
        seen.update(open_gaps=open_gaps, history=list(history), text=text)
        return _discuss_reply()

    monkeypatch.setattr(claude, "discuss", fake)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    body = {"profile": "developing", "message": "I capped it so attempt 10 doesn't wait minutes", "text": "my draft"}
    data = client.post(f"/api/runs/{run['run_id']}/discuss", json=body).json()
    assert data["source"] == "live" and data["reply"].startswith("Right")
    assert seen["open_gaps"] == [{"gap_id": "gap-1", "description": "Compute the delay and sleep."}]
    assert seen["text"] == "my draft"
    evidence = [e for e in client.get("/api/model/developing").json()["concepts"]
                if e["name"] == "retry with exponential backoff"][0]["evidence"]
    assert evidence == 7  # one self-explanation added to the seeded six

    # The conversation continues with its history.
    client.post(f"/api/runs/{run['run_id']}/discuss", json={**body, "message": "and the jitter?"})
    assert [t["role"] for t in seen["history"]] == ["user", "assistant"]


def test_discussion_records_questions_and_pushback(client, monkeypatch):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "proficient"}).json()
    url = f"/api/runs/{run['run_id']}/discuss"
    body = {"profile": "proficient", "message": "m", "text": "t"}
    monkeypatch.setattr(claude, "discuss", lambda *a: _discuss_reply(kind="question", understanding="not_applicable", solo=4))
    assert client.post(url, json=body).json()["events"][0]["label"] == "question, SOLO level 4"
    monkeypatch.setattr(claude, "discuss", lambda *a: _discuss_reply(kind="pushback", understanding="not_applicable", pushback_valid=True))
    assert client.post(url, json=body).json()["events"][0]["label"] == "pushback: Claude agreed"


def test_revealed_gaps_are_not_hidden_from_the_discussion(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude, "discuss", lambda task, b, t, open_gaps, h, m: seen.update(g=open_gaps) or _discuss_reply())
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    client.post(f"/api/runs/{run['run_id']}/reveal", json={"profile": "developing", "gap_id": "gap-1"})
    client.post(f"/api/runs/{run['run_id']}/discuss", json={"profile": "developing", "message": "why?", "text": "t"})
    assert seen["g"] == []


def test_discussion_offline_and_length_limit(client, monkeypatch):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    url = f"/api/runs/{run['run_id']}/discuss"
    offline = client.post(url, json={"profile": "developing", "message": "m", "text": "t", "live": False}).json()
    assert offline["source"] == "offline" and offline["events"] == []
    monkeypatch.setattr(claude, "discuss", lambda *a: _discuss_reply(kind="other", understanding="not_applicable"))
    for _ in range(webapp_app.MAX_DISCUSSION_MESSAGES):
        assert client.post(url, json={"profile": "developing", "message": "m", "text": "t"}).status_code == 200
    assert client.post(url, json={"profile": "developing", "message": "m", "text": "t"}).status_code == 400


def _finish_stubs(monkeypatch, outcome="correct"):
    monkeypatch.setattr(claude, "final_check", lambda *a: {
        "ready": True, "issues": [], "teach_back_question": "What happens when the service is down for good?"})
    monkeypatch.setattr(claude, "grade", lambda *a: {"outcome": outcome, "feedback": "Yes."})


def test_finishing_summarizes_and_hands_over_the_work(client, monkeypatch):
    _finish_stubs(monkeypatch)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "novice"}).json()
    url = f"/api/runs/{run['run_id']}/finish"
    final = run["scaffold"] + "\n# my note about the cap"
    check = client.post(f"{url}/check", json={"profile": "novice", "text": final}).json()
    assert check["ready"] and check["source"] == "live"

    explained = client.post(f"{url}/explain", json={"profile": "novice", "answer": "It stops after 5 tries and re-raises"}).json()
    assert explained["outcome"] == "correct"
    assert explained["events"][0]["from"] == 0 and explained["events"][0]["to"] == 1

    done = client.post(f"{url}/complete", json={"profile": "novice", "text": final}).json()
    assert done["file"] == {"name": "forecast.py", "content": final}
    assert {"concept": "retry with exponential backoff", "from": 0, "to": 1} in done["changes"]
    assert "Assisted-by: Claude" in done["commit"]["message"]
    files = done["commit"]["note"]["files"]["forecast.py"]
    assert files["human"] == [[len(final.splitlines()), len(final.splitlines())]]
    retry = next(u for u in done["upcoming"] if u["concept"] == "retry with exponential backoff")
    assert retry["review_on"] and retry["next_plan"][0] == "prediction"  # level 1 now: questions, not a worked example


def test_skipping_the_explanation_is_logged_not_counted(client, monkeypatch):
    _finish_stubs(monkeypatch)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "novice"}).json()
    url = f"/api/runs/{run['run_id']}/finish"
    client.post(f"{url}/check", json={"profile": "novice", "text": run["scaffold"]})
    done = client.post(f"{url}/complete", json={"profile": "novice", "text": run["scaffold"]}).json()
    assert done["changes"] == []
    assert done["ownership"]["learner_share"] == 0.0


def test_a_finished_run_is_read_only(client, monkeypatch):
    _finish_stubs(monkeypatch)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    url = f"/api/runs/{run['run_id']}"
    client.post(f"{url}/finish/complete", json={"profile": "developing", "text": run["scaffold"]})
    for path, body in [("submit", {"text": "x"}), ("reveal", {"gap_id": "gap-1"}),
                       ("discuss", {"message": "m", "text": "t"}), ("finish/complete", {"text": "x"})]:
        assert client.post(f"{url}/{path}", json={"profile": "developing", **body}).status_code == 409


def test_offline_final_check_flags_unfinished_gaps(client):
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    check = client.post(f"/api/runs/{run['run_id']}/finish/check",
                        json={"profile": "developing", "text": run["scaffold"], "live": False}).json()
    assert not check["ready"] and check["issues"][0]["severity"] == "blocking"
    assert check["teach_back_question"]


def test_finishing_reviews_gaps_filled_since_the_last_submit(client, monkeypatch):
    _finish_stubs(monkeypatch)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    filled = run["scaffold"].replace(
        "            # TODO(learner) gap-1: Compute the delay and sleep. Hint: Double each time, but cap it.",
        "            delay = min(cap, base * 2 ** attempt)\n            time.sleep(random.uniform(0, delay))",
    )
    url = f"/api/runs/{run['run_id']}/finish"
    client.post(f"{url}/check", json={"profile": "developing", "text": filled, "live": False})
    done = client.post(f"{url}/complete", json={"profile": "developing", "live": False}).json()
    assert {"concept": "retry with exponential backoff", "from": 2, "to": 3} in done["changes"]
    assert done["file"]["content"] == filled


def test_finishing_uses_the_checked_version(client, monkeypatch):
    _finish_stubs(monkeypatch)
    seen = {}
    monkeypatch.setattr(claude, "grade", lambda task, q, answer, solution: seen.update(solution=solution)
                        or {"outcome": "correct", "feedback": "Yes."})
    run = client.post("/api/run", json={"task_id": "retry", "profile": "novice"}).json()
    url = f"/api/runs/{run['run_id']}/finish"
    checked = run["scaffold"] + "\n# attempts=3 is enough here"
    client.post(f"{url}/check", json={"profile": "novice", "text": checked})
    client.post(f"{url}/explain", json={"profile": "novice", "answer": "three tries then it raises"})
    assert seen["solution"] == checked  # graded against the learner's version, not Claude's
    assert client.post(f"{url}/complete", json={"profile": "novice", "text": checked + "\n# later edit"}).status_code == 409
    done = client.post(f"{url}/complete", json={"profile": "novice", "text": checked}).json()
    assert done["file"]["content"] == checked


def test_concurrent_reveals_keep_both_answers(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    two = {**RUN, "draft": DRAFT.replace("            [[/GAP]]\n", "            [[/GAP]]\n    [[GAP gap-2]]\n    return None\n    [[/GAP]]\n"),
           "gaps": [dict(RUN["gaps"][0]), {**RUN["gaps"][0], "gap_id": "gap-2"}]}
    monkeypatch.setattr(claude, "run_task", lambda *a: two)
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    assert len(run["gaps"]) == 2
    url = f"/api/runs/{run['run_id']}/reveal"
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda g: client.post(url, json={"profile": "developing", "gap_id": g}), ["gap-1", "gap-2"]))
    pasted = run["scaffold"].replace(
        "            # TODO(learner) gap-1: Compute the delay and sleep. Hint: Double each time, but cap it.",
        "            delay = min(cap, base * 2 ** attempt)\n            time.sleep(random.uniform(0, delay))",
    ).replace("    # TODO(learner) gap-2: Compute the delay and sleep. Hint: Double each time, but cap it.", "    return None")
    data = client.post(f"/api/runs/{run['run_id']}/submit", json={"profile": "developing", "text": pasted, "live": False}).json()
    assert data["ownership"]["totals"]["human"] == 0


def test_summary_counts_only_this_runs_evidence(client, monkeypatch):
    _finish_stubs(monkeypatch)
    a = client.post("/api/run", json={"task_id": "retry", "profile": "novice"}).json()
    b = client.post("/api/run", json={"task_id": "rationale", "profile": "novice"}).json()
    # Work in run B (another tab) moves a level...
    client.post(f"/api/runs/{b['run_id']}/finish/check", json={"profile": "novice", "text": b["scaffold"]})
    client.post(f"/api/runs/{b['run_id']}/finish/explain", json={"profile": "novice", "answer": "a"})
    # ...and run A's summary doesn't claim it.
    done = client.post(f"/api/runs/{a['run_id']}/finish/complete", json={"profile": "novice", "text": a["scaffold"]}).json()
    assert done["changes"] == [] and done["rules"] == []


SCOPE = {"personal": False, "title": "Paginate the orders query", "language": "sql", "filename": "orders.sql",
         "commit_message": "Paginate the orders query", "teach_back": "Why keyset over OFFSET?",
         "concepts": [{"name": "keyset pagination", "description": "Paging by the last seen key"}]}


def test_own_task_is_scoped_then_done(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(claude, "scope_task", lambda kind, request, material, known: seen.update(known=known) or SCOPE)
    monkeypatch.setattr(claude, "run_task", lambda task, context: seen.update(task=task) or
                        {**RUN, "draft": "SELECT * FROM orders\nWHERE id > :last\nORDER BY id LIMIT 50", "gaps": []})
    own = {"kind": "code", "request": "Paginate this query without OFFSET", "material": "SELECT * FROM orders",
           "filename": "../../etc/orders.sql"}
    run = client.post("/api/run", json={"task_id": "own", "profile": "you", "own": own}).json()
    assert run["task"]["concepts"] == ["keyset pagination"] and run["task"]["filename"] == "orders.sql"
    assert seen["task"]["material"] == "SELECT * FROM orders" and seen["known"] == []
    assert run["plan"][0]["level"] == 0 and run["plan"][0]["track"] != "skill"  # seen once: a one-off so far
    done = client.post(f"/api/runs/{run['run_id']}/finish/complete", json={"profile": "you", "text": run["scaffold"]}).json()
    assert done["file"]["name"] == "orders.sql" and done["commit"]["message"].startswith("Paginate the orders query")
    model = client.get("/api/model/you").json()
    assert [c["name"] for c in model["concepts"]] == ["keyset pagination"]


def test_own_task_needs_live_claude_and_skips_personal_topics(client, monkeypatch):
    own = {"kind": "writing", "request": "Draft a note to my manager about my sick leave", "material": ""}
    assert client.post("/api/run", json={"profile": "you", "own": own, "live": False}).status_code == 503
    monkeypatch.setattr(claude, "scope_task", lambda *a: {**SCOPE, "personal": True, "concepts": []})
    assert client.post("/api/run", json={"profile": "you", "own": own}).status_code == 422
    assert client.get("/api/model/you").json()["concepts"] == []


def test_you_start_empty_and_can_export_everything(client):
    assert client.get("/api/model/you").json()["concepts"] == []
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    client.post(f"/api/runs/{run['run_id']}/predict", json={"profile": "developing", "answer": "bounded"})
    res = client.get("/api/model/developing/export")
    assert "attachment" in res.headers["content-disposition"]
    data = res.json()
    assert "retry with exponential backoff" in {c["name"] for c in data["concepts"]}
    tables = data["tables"]
    assert any(i["response"] == "bounded" for i in tables["interventions"])
    assert tables["demo_runs"][0]["payload"]["scaffold"] == run["scaffold"] and tables["baselines"]


def test_sandboxes_expire_after_retention_however_often_visited(client):
    import os

    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    url = f"/api/runs/{run['run_id']}/reveal"
    assert client.post(url, json={"profile": "developing", "gap_id": "gap-1"}).status_code == 200
    (marker,) = webapp_app.DATA_DIR.glob("*/.created")
    old = marker.stat().st_mtime - webapp_app.RETENTION_SECONDS - 60
    os.utime(marker, (old, old))
    # Coming back doesn't extend it: the expired sandbox, and the run in it, are gone.
    assert client.post(url, json={"profile": "developing", "gap_id": "gap-1"}).status_code == 404
    assert marker.stat().st_mtime > old + 60


def test_finishing_credits_an_issue_caught_since_the_last_submit(client, monkeypatch):
    _finish_stubs(monkeypatch)
    monkeypatch.setattr(claude, "review", lambda *a: {
        "gaps": [{"gap_id": "gap-1", "verdict": "as_expected", "feedback": "ok"}],
        "other_changes": "Fixed the off-by-one", "caught_issue": True})
    run = client.post("/api/run", json={"task_id": "retry", "profile": "developing"}).json()
    filled = run["scaffold"].replace("# TODO(learner) gap-1", "delay = 1  #")
    url = f"/api/runs/{run['run_id']}/finish"
    client.post(f"{url}/check", json={"profile": "developing", "text": filled})
    done = client.post(f"{url}/complete", json={"profile": "developing"}).json()
    assert any(r["rule_id"] for r in done["rules"])
    export = client.get("/api/model/developing/export").json()["tables"]["evidence"]
    assert any(e["signal_type"] == "substantive_fix" and e["ref"] == run["run_id"] for e in export)


def test_expiry_never_deletes_a_sandbox_in_use_or_already_replaced(client, monkeypatch):
    import os

    vid = "b" * 32
    webapp_app.expire_sandboxes(vid)
    marker = webapp_app.DATA_DIR / vid / ".created"
    (marker.parent / "work.db").write_text("new work")
    old = marker.stat().st_mtime - webapp_app.RETENTION_SECONDS - 60
    os.utime(marker, (old, old))

    # Another request is using the sandbox: it survives, and is checked again later.
    monkeypatch.setitem(webapp_app._in_flight, vid, 2)
    webapp_app.expire_sandboxes(vid)
    assert (marker.parent / "work.db").exists()

    # The first request expires it and starts fresh; a second that saw the old timestamp
    # rechecks under the lock and leaves the new sandbox alone.
    monkeypatch.setitem(webapp_app._in_flight, vid, 1)
    webapp_app.expire_sandboxes(vid)
    (marker.parent / "work.db").write_text("newer work")
    webapp_app.expire_sandboxes(vid)
    assert (marker.parent / "work.db").read_text() == "newer work"

    # The sweep skips sandboxes in use.
    other = "c" * 32
    webapp_app.expire_sandboxes(other)
    os.utime(webapp_app.DATA_DIR / other / ".created", (old, old))
    monkeypatch.setitem(webapp_app._in_flight, other, 1)
    monkeypatch.setattr(webapp_app, "_last_sweep", 0.0)
    webapp_app.expire_sandboxes(vid)
    assert (webapp_app.DATA_DIR / other).exists()
