from learner import report

NAME = "retry with backoff"
ATTR_MIXED = {"files": {"src/retry.py": {}}, "totals": {"claude": 4, "claude_modified": 2, "human": 4}}
ATTR_HUMAN = {"files": {"src/other.py": {}}, "totals": {"claude": 0, "claude_modified": 0, "human": 9}}
ATTR_CLAUDE = {"files": {"src/x.py": {}}, "totals": {"claude": 7, "claude_modified": 0, "human": 0}}


def test_gap_lifecycle_from_baseline_to_level(store):
    store.record_concept(NAME)
    rec = store.record_baseline(
        "/repo/src/retry.py", "full solution", [{"description": "backoff delay", "hint": "cap it", "concept": NAME}]
    )
    item = rec["withheld_items"][0]
    assert store.get_baseline(item)["content"] == "full solution"

    commit = store.record_commit("abc123", "s1", ATTR_MIXED)
    assert commit["withheld_ready_for_review"] == [item]
    assert store.summary()["withheld_to_review"][0]["item_id"] == item

    store.update_withheld(item, "different")
    assert store.explain(NAME)["level"] == 3
    assert store.summary()["withheld_to_review"] == []


def test_tag_commit_turns_attribution_into_evidence(store):
    store.record_commit("h1", None, ATTR_HUMAN)
    store.record_commit("c1", "s1", ATTR_CLAUDE)
    store.record_commit("m1", "s1", ATTR_MIXED)
    assert store.summary()["commits_to_tag"] == ["h1", "c1", "m1"]

    store.tag_commit("h1", [NAME])
    store.tag_commit("c1", [NAME])
    store.tag_commit("m1", [NAME])
    signals = [(e["signal_type"], e["detail"]) for e in store.explain(NAME)["evidence"]]
    assert signals == [("unassisted_use", None), ("unchanged_merge", None), ("draft_ownership", "0.6")]
    assert store.summary()["commits_to_tag"] == []
    # Three distinct commits within 30 days: promoted to a skill track.
    assert store.explain(NAME)["track"] == "skill"


def test_intervention_outcomes(store):
    store.record_concept(NAME)
    store.record_intervention(NAME, "prediction", "What happens on attempt 6?", "partial")
    assert store.explain(NAME)["level"] == 0
    store.record_intervention(NAME, "prediction", "And with no cap?", "correct", response="grows forever")
    assert store.explain(NAME)["level"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0] == 2


def test_plan_follows_dial_track_and_level(store):
    store.record_concept(NAME)
    ctx = store.context([NAME, "something new"])
    assert ctx["concepts"][0]["plan"]["interventions"] == ["decision_explanation", "takeaway"]
    assert ctx["unknown"][0]["name"] == "something new"

    store.set_track(NAME, "skill")
    assert store.context([NAME])["concepts"][0]["plan"]["interventions"] == ["orientation", "worked_example", "takeaway"]

    store.record_evidence(NAME, "self_explanation", True)
    store.set_dial(NAME, mode="how_to")
    assert store.context([NAME])["concepts"][0]["plan"]["interventions"][0] == "gap"

    store.set_dial(NAME, dial="light")
    assert len(store.context([NAME])["concepts"][0]["plan"]["interventions"]) == 1

    store.set_session_dial("off")
    assert store.context([NAME])["concepts"][0]["plan"]["interventions"] == []


def test_non_learning_flags_on_skill_tracks(store):
    store.record_concept(NAME)
    store.set_track(NAME, "skill")
    store.record_evidence(NAME, "unchanged_merge")
    store.record_evidence(NAME, "unchanged_merge")
    store.record_evidence(NAME, "re_explain_request")
    store.record_evidence(NAME, "re_explain_request")
    behaviors = {f["behavior"] for f in store.summary()["non_learning_flags"]}
    assert behaviors == {"unchanged_merges", "re_explain_requests"}


def test_report_builds_and_renders(store):
    store.record_concept(NAME)
    store.set_track(NAME, "skill")
    store.record_evidence(NAME, "question", detail="2")
    store.record_evidence(NAME, "critique", True)
    r = report.build(store.conn)
    assert r["criteria"]["D_critical_engagement"]["critiques"] == 1
    assert r["criteria"]["C_harder_questions"]["solo_avg_last_30d"] == 2.0
    text = report.render(r)
    assert "retry with backoff: level 4 (critique, about)" in text
