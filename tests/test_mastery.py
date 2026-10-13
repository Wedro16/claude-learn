from learner import mastery

NAME = "retry with backoff"


def level(store):
    return store.explain(NAME)["level"]


def test_correct_prediction_reaches_explain(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "prediction", False)
    assert level(store) == 0
    store.record_evidence(NAME, "prediction", True)
    assert level(store) == 1


def test_predict_needs_correct_what_ifs_in_two_sessions(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "what_if", True, ref="s1")
    store.record_evidence(NAME, "what_if", True, ref="s1")
    assert level(store) == 0
    store.record_evidence(NAME, "what_if", True, ref="s2")
    assert level(store) == 2


def test_two_wrong_predictions_in_a_row_drop_predict(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "what_if", True, ref="s1")
    store.record_evidence(NAME, "what_if", True, ref="s2")
    store.record_evidence(NAME, "prediction", False)
    assert level(store) == 2
    store.record_evidence(NAME, "what_if", False)
    assert level(store) == 1


def test_reask_drops_a_level(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "self_explanation", True)
    store.record_evidence(NAME, "conceptual_reask")
    assert level(store) == 0


def test_reference_lookup_never_counts_against(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "self_explanation", True)
    store.record_evidence(NAME, "reference_lookup")
    assert level(store) == 1


def test_unassisted_use_reaches_modify_and_records_prediction(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "unassisted_use", True)
    assert level(store) == 3
    pred = store.conn.execute("SELECT * FROM predictions").fetchone()
    assert pred["level"] == 3 and pred["outcome"] is None
    store.record_evidence(NAME, "unassisted_use", True)
    pred = store.conn.execute("SELECT * FROM predictions").fetchone()
    assert pred["outcome"] == 1


def test_handed_back_gap_resolves_prediction_as_wrong_and_drops(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "unassisted_use", True)
    store.record_evidence(NAME, "gap_completion", False, detail="handed_back")
    assert level(store) == 2
    assert store.conn.execute("SELECT outcome FROM predictions").fetchone()["outcome"] == 0


def test_critique_reaches_level_4_and_repeated_unchanged_merges_drop_it(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "critique", True)
    assert level(store) == 4
    store.record_evidence(NAME, "unchanged_merge")
    assert level(store) == 4
    store.record_evidence(NAME, "unchanged_merge")
    assert level(store) == 3


def test_every_rule_evaluation_is_logged(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "prediction", False)
    obs = store.conn.execute("SELECT rule_id, passed FROM rule_observations").fetchall()
    assert ("L1.reach", 0) in [(o["rule_id"], o["passed"]) for o in obs]


def test_decay_after_60_days_without_evidence(store):
    store.record_concept(NAME)
    store.record_evidence(NAME, "self_explanation", True, at="2026-01-01T00:00:00+00:00")
    store.record_evidence(NAME, "what_if", True, ref="a", at="2026-01-01T00:00:00+00:00")
    store.record_evidence(NAME, "what_if", True, ref="b", at="2026-01-01T00:00:00+00:00")
    assert level(store) == 2
    assert mastery.apply_decay(store.conn, "2026-02-15T00:00:00+00:00") == []
    assert mastery.apply_decay(store.conn, "2026-03-05T00:00:00+00:00") == ["retry-with-backoff"]
    assert level(store) == 1
    # The decay itself restarts the clock.
    assert mastery.apply_decay(store.conn, "2026-03-06T00:00:00+00:00") == []


def _occurrence(store, cid, ctx, at):
    store.conn.execute("INSERT INTO occurrences (concept_id, context_id, at) VALUES (?, ?, ?)", (cid, ctx, at))


def test_promotion_needs_two_occurrences_within_30_days(store):
    cid = store.record_concept(NAME, context_id="s1")["concept_id"]
    assert store.explain(NAME)["track"] == "one_off"
    _occurrence(store, cid, "old", "2020-01-01T00:00:00+00:00")
    assert mastery.update_track(store.conn, cid)["track"] == "one_off"
    _occurrence(store, cid, "recent", mastery.now())
    assert mastery.update_track(store.conn, cid) == {"track": "skill", "occurrences_30d": 2}


def test_demotion_by_learner_is_pinned(store):
    cid = store.record_concept(NAME)["concept_id"]
    store.set_track(NAME, "one_off")
    _occurrence(store, cid, "x", mastery.now())
    _occurrence(store, cid, "y", mastery.now())
    assert mastery.update_track(store.conn, cid)["track"] == "one_off"
