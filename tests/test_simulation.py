"""The learner model over time: simulated learners driving the real engine for 16 weeks."""

import pytest

from learner import db, mastery
from learner.simulate import simulate


@pytest.fixture(scope="module")
def runs():
    return simulate()["runs"]


def levels(run):
    return [w["level"] for w in run["weeks"]]


def test_runs_are_repeatable_and_restore_the_clock(runs):
    assert simulate()["runs"]["engaged"]["weeks"] == runs["engaged"]["weeks"]
    assert db._clock is None


def test_an_engaged_learner_climbs_and_support_fades(runs):
    run = runs["engaged"]
    assert levels(run)[0] == 0 and levels(run)[-1] == 4
    assert levels(run) == sorted(levels(run))  # never drops while practicing
    first, last = run["weeks"][0], run["weeks"][-1]
    assert "worked_example" in first["plan_next"] and last["plan_next"][0] == "critique"
    assert last["signals"]["A_unassisted"] >= 3 and last["signals"]["D_critiques"] >= 3
    assert last["flags"] == []


def test_a_delegator_stays_supported_and_is_flagged(runs):
    run = runs["delegator"]
    assert set(levels(run)) == {0}
    last = run["weeks"][-1]
    assert "worked_example" in last["plan_next"]
    assert {"unchanged_merges", "re_explain_requests"} <= set(last["flags"])
    assert last["signals"]["A_unassisted"] == 0


def test_a_returner_decays_while_away_and_recovers(runs):
    run = runs["returner"]
    away = [w["level"] for w in run["weeks"] if 6 <= w["week"] <= 15]
    assert min(away) < max(away)  # decay after 60 days without evidence
    assert levels(run)[-1] > min(away)  # back at work, the level recovers


def test_an_expert_gets_expert_treatment_within_three_weeks(runs):
    run = runs["expert"]
    assert levels(run)[2] == 4 and run["weeks"][0]["plan_next"][0] == "critique"


def test_the_model_tracks_true_knowledge_within_one_level(runs):
    for run in runs.values():
        assert run["tracking"]["weeks_within_one"] >= 15, run["persona"]


def test_asking_the_deepest_questions_is_not_a_plateau(store):
    store.record_concept("retry", context_id="s1")
    store.record_concept("retry", context_id="s0")
    for solo in ("4", "4", "4", "4"):
        store.record_evidence("retry", "question", detail=solo, ref="s1")
    assert not [f for f in mastery.non_learning_flags(store.conn) if f["behavior"] == "flat_question_complexity"]


def test_simpler_recent_questions_are_still_a_plateau(store):
    store.record_concept("retry", context_id="s1")
    store.record_concept("retry", context_id="s0")
    for solo in ("4", "4", "1", "1"):
        store.record_evidence("retry", "question", detail=solo, ref="s1")
    assert [f for f in mastery.non_learning_flags(store.conn) if f["behavior"] == "flat_question_complexity"]
