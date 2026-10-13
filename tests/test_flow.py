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


def test_tagging_twice_counts_once(store):
    store.record_commit("c1", "s1", ATTR_CLAUDE)
    store.record_concept(NAME)
    store.record_evidence(NAME, "critique", True)
    store.tag_commit("c1", [NAME])
    again = store.tag_commit("c1", [NAME])
    assert again == [{"concept_id": "retry-with-backoff", "already_tagged": True}]
    assert [e["signal_type"] for e in store.explain(NAME)["evidence"]] == ["critique", "unchanged_merge"]


def test_reviewing_a_gap_twice_counts_once(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "unassisted_use", True)
    item = store.record_baseline("/repo/a.py", "full", [{"description": "d", "concept": NAME}])["withheld_items"][0]
    store.update_withheld(item, "handed_back")
    assert store.update_withheld(item, "handed_back")["already_reviewed"]
    assert store.explain(NAME)["level"] == 2


def test_tagging_assisted_work_reuses_its_session_occurrence(store):
    # One task: concept recorded during session s1, then its commit is tagged.
    store.record_concept(NAME, context_id="s1")
    store.record_commit("m1", "s1", ATTR_MIXED)
    store.tag_commit("m1", [NAME])
    assert store.explain(NAME)["track"] == "one_off"


def test_unassisted_commit_is_its_own_occurrence(store):
    store.record_concept(NAME, context_id="s1")
    store.record_commit("h1", None, ATTR_HUMAN)
    store.tag_commit("h1", [NAME])
    assert store.explain(NAME)["track"] == "skill"


def test_request_dial_clears_at_next_message(store):
    store.record_concept(NAME)
    store.set_session_dial("off")
    assert store.context([NAME])["concepts"][0]["plan"]["dial"] == "off"
    store.clear_session_dial("s1")
    assert store.context([NAME])["concepts"][0]["plan"]["dial"] == "full"


def test_server_recommended_kinds_are_recordable(store):
    store.record_concept(NAME)
    kinds = store.context([NAME])["concepts"][0]["plan"]["interventions"]
    for kind in kinds:
        store.record_intervention(NAME, kind, "prompt", "pending")


def test_touched_file_without_added_lines_readies_its_gap(store):
    item = store.record_baseline("/repo/src/retry.py", "full", [{"description": "d"}])["withheld_items"][0]
    attribution = {"files": {}, "totals": {"claude": 0, "claude_modified": 0, "human": 0}, "touched": ["src/retry.py"]}
    assert store.record_commit("t1", None, attribution)["withheld_ready_for_review"] == [item]


def test_summary_is_scoped_to_the_repository(store):
    store.record_commit("a1", None, ATTR_HUMAN, repo="/repo-a")
    store.record_commit("b1", None, ATTR_HUMAN, repo="/repo-b")
    assert store.summary(repo="/repo-a")["commits_to_tag"] == ["a1"]


def test_amend_replaces_the_earlier_commit(store):
    store.record_commit("old", "s1", ATTR_MIXED)
    store.tag_commit("old", [NAME])
    store.record_commit("new", "s1", ATTR_MIXED, replaces="old")
    rows = store.conn.execute("SELECT sha, tagged FROM commits").fetchall()
    assert [(r["sha"], r["tagged"]) for r in rows] == [("new", 1)]


def test_report_counts_only_successful_observations(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "unassisted_use", False)
    store.record_evidence(NAME, "substantive_fix", False)
    c = report.build(store.conn)["criteria"]
    assert c["A_unassisted"]["unassisted_use_evidence"] == 0
    assert c["D_critical_engagement"]["substantive_fixes"] == 0


def test_session_summary_reports_only_this_sessions_changes(store):
    store.record_concept(NAME, context_id="s1")
    store.record_concept(NAME, context_id="s0")  # seen before: a skill track
    # The finish step's explain-back, answered correctly in this session.
    store.record_intervention(NAME, "retrieval", "What happens when it's down for good?", "correct",
                              response="it gives up after five tries", session_id="s1")
    # Evidence from another session never shows up here.
    store.record_evidence(NAME, "self_explanation", True, ref="s2", session_id="s2")

    summary = store.session_summary("s1")
    assert summary["changes"] == [{"concept": NAME, "from": 0, "to": 1}]
    assert [r["rule_id"] for r in summary["rules"]] == ["L1.reach"]
    assert summary["observations"] == 1
    (upcoming,) = summary["upcoming"]
    assert upcoming["track"] == "skill" and upcoming["review_on"]
    assert store.context([NAME])["concepts"][0]["review_on"] == upcoming["review_on"]


def test_session_summary_counts_commit_reviews_done_in_the_session(store):
    # Last session's commit, tagged at the start of this one: its evidence carries the commit SHA.
    store.record_concept(NAME, context_id="s0")
    store.record_commit("abc", "s0", ATTR_HUMAN)
    store.start_session("s1", "/repo")
    store.record_concept(NAME, context_id="s1")
    store.tag_commit("abc", [NAME], session_id="s1")
    summary = store.session_summary("s1")
    assert summary["changes"] == [{"concept": NAME, "from": 0, "to": 3}]
    assert summary["observations"] == 1


def test_next_plan_ignores_this_requests_just_do_it(store):
    store.record_concept(NAME, context_id="s0")
    store.record_concept(NAME, context_id="s1")
    store.set_session_dial("off", session_id="s1")
    assert store.context([NAME], session_id="s1")["concepts"][0]["plan"]["interventions"] == []
    (upcoming,) = store.session_summary("s1")["upcoming"]
    assert upcoming["next_plan"]  # next time is a new request
