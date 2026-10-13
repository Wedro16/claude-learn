"""Simulated learners over 16 weeks, driving the real learner engine.

Each simulated person has a hidden "true knowledge" of one concept, between 0 and 1, that the
learner model never sees. Every task runs the way the learning policy (CLAUDE.md) describes:
the session starts, due reviews and pending commits are handled, Claude reads the plan, and the
person responds to each intervention according to their behavior and what they actually know.
Their answers become the same evidence a real session records, through the same store calls.

Because the true knowledge is known, the run also measures how closely the model's level tracks
it. Everything is seeded, so a run is repeatable.

    python -m learner.simulate                 # summary
    python -m learner.simulate --json out.json # full trace: inputs, engine calls, outputs
"""

import argparse
import json
import math
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learner import db, mastery, report
from learner.db import connect
from learner.store import LearnerStore

CONCEPT = "retry with exponential backoff"
START = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)  # a Monday
WEEKS = 16
TASK_DAYS = (0, 3)  # two tasks a week: Monday and Thursday
SEED = 7

PERSONAS = {
    "engaged": {
        "name": "Engaged learner",
        "summary": "New to the concept. Answers questions, fills gaps, explains back, asks questions.",
        "knowledge": 0.05, "behavior": "engaged", "away": None, "unassisted_from": 6,
    },
    "delegator": {
        "name": "Delegator",
        "summary": "New to the concept. Usually says \"just do it\", reveals gaps, commits Claude's code unchanged.",
        "knowledge": 0.05, "behavior": "delegator", "away": None, "unassisted_from": None,
    },
    "returner": {
        "name": "Returner",
        "summary": "Engaged for five weeks, then away for ten (weeks 6-15), then back.",
        "knowledge": 0.05, "behavior": "engaged", "away": (6, 15), "unassisted_from": 4,
    },
    "expert": {
        "name": "Expert",
        "summary": "Already knows the concept well. Engages, and looks for flaws in Claude's output.",
        "knowledge": 0.9, "behavior": "engaged", "away": None, "unassisted_from": 1,
    },
}

# How much each activity moves true knowledge toward 1, for someone doing it themselves.
# Generative work (predicting, writing the gap, explaining) teaches more than reading.
LEARNING = {
    "orientation": 0.015, "worked_example": 0.03, "decision_explanation": 0.015, "takeaway": 0.005,
    "prediction": 0.035, "what_if": 0.03, "retrieval": 0.035, "gap": 0.06, "critique": 0.02,
    "question": 0.01, "unassisted": 0.04,
}
PASSIVE = 0.25  # a delegator only skims: a quarter of the effect
FORGET_PER_DAY = 0.004  # without practice, knowledge fades by about 0.4% a day


def true_level(knowledge: float) -> int:
    """The ownership level a person with this knowledge would actually reach."""
    return sum(knowledge >= t for t in (0.2, 0.4, 0.6, 0.8))


class Sim:
    def __init__(self, persona: str, path: Path, seed: int = SEED):
        self.key, self.p = persona, PERSONAS[persona]
        self.store = LearnerStore(connect(path))
        self.rng = random.Random(f"{seed}-{persona}")
        self.k = self.p["knowledge"]
        self.t = START
        self.last_practice = START
        self.events, self.weeks = [], []
        self.commits = 0
        db.set_clock(lambda: self.t.isoformat(timespec="seconds"))

    # -- the hidden learner ------------------------------------------------

    def learn(self, activity: str, engaged: bool = True) -> None:
        gain = LEARNING[activity] * (1 if engaged else PASSIVE)
        self.k += gain * (1 - self.k)
        self.last_practice = self.t

    def forget(self) -> None:
        days = (self.t - self.last_practice).total_seconds() / 86400
        self.k *= math.exp(-FORGET_PER_DAY * max(days, 0))
        self.last_practice = self.t

    def succeeds(self, bonus: float = 0.0) -> bool:
        return self.rng.random() < min(1.0, self.k + bonus)

    # -- recording ---------------------------------------------------------

    def level(self) -> int:
        row = self.store.conn.execute(
            "SELECT s.level FROM concept_state s JOIN concepts c ON c.id = s.concept_id WHERE c.name = ?", (CONCEPT,)
        ).fetchone()
        return row["level"] if row else 0

    def log(self, week: int, stage: str, did: str, call: str, result: dict | None = None) -> None:
        result = result or {}
        self.events.append({
            "week": week, "date": self.t.date().isoformat(), "stage": stage, "input": did, "engine_call": call,
            "rules_fired": result.get("rules_fired", []),
            "level": [result["from_level"], result["to_level"]] if "from_level" in result else [self.level()] * 2,
            "true_knowledge": round(self.k, 2),
        })

    def answer(self, week, sid, kind, prompt, stage="work"):
        """Answer a question Claude asked. Delegators skip; others are right as often as they know it."""
        if self.p["behavior"] == "delegator":
            self.store.record_intervention(CONCEPT, kind, prompt, "skipped", session_id=sid)
            self.log(week, stage, f"Skipped the {kind.replace('_', ' ')} question",
                     f"record_intervention({kind}, skipped)")
            return
        right = self.succeeds()
        outcome = "correct" if right else ("partial" if self.k > 0.3 else "incorrect")
        self.learn(kind)
        res = self.store.record_intervention(CONCEPT, kind, prompt, outcome, session_id=sid)
        self.log(week, stage, f"Answered the {kind.replace('_', ' ')} question: {outcome}",
                 f"record_intervention({kind}, {outcome})", res)

    # -- one task, as the learning policy runs it --------------------------

    def task(self, week: int, n: int) -> None:
        sid = f"{self.key}-w{week:02d}-{n}"
        delegator = self.p["behavior"] == "delegator"
        self.forget()
        self.store.start_session(sid)  # applies decay for long gaps
        decayed = self.store.conn.execute(
            "SELECT from_level, to_level FROM rule_observations WHERE rule_id = 'decay' AND at = ?",
            (self.t.isoformat(timespec="seconds"),),
        ).fetchone()
        if decayed:
            self.log(week, "session start", "Came back after a long gap", "start_session -> apply_decay",
                     {"rules_fired": ["decay"], "from_level": decayed[0], "to_level": decayed[1]})

        summary = self.store.summary()
        for sha in summary["commits_to_tag"]:
            for res in self.store.tag_commit(sha, [CONCEPT], session_id=sid):
                kind = self.store.conn.execute(
                    "SELECT signal_type FROM evidence WHERE ref = ? ORDER BY id DESC LIMIT 1", (sha,)).fetchone()[0]
                self.log(week, "session start", f"Last commit tagged: {kind.replace('_', ' ')}",
                         f"tag_commit({sha})", res)
        if CONCEPT in summary["reviews_due"]:
            self.answer(week, sid, "retrieval", "Spaced review: explain the idea in a sentence", "review")

        if delegator and self.rng.random() < 0.75:
            self.store.set_session_dial("off", session_id=sid)
            self.log(week, "ask", "Said \"just do it\"", "set_session_dial(off)")
        self.store.record_concept(CONCEPT, context_id=sid)
        plan = self.store.context([CONCEPT], session_id=sid)["concepts"][0]["plan"]["interventions"]
        dial_off = self.store.conn.execute("SELECT dial_override FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
        explain_back = dial_off != "off"
        if explain_back:
            plan = plan[:2]  # the explain-back at finish counts toward the three per task (CLAUDE.md)
        self.log(week, "ask", "Asked for a task on the concept",
                 f"get_learner_context -> plan: {', '.join(plan) or 'none (dial off)'}"
                 + (" + explain-back at finish" if explain_back else ""))

        for kind in plan:
            if kind in ("orientation", "worked_example", "decision_explanation", "takeaway"):
                self.learn(kind, engaged=not delegator)
                self.store.record_intervention(CONCEPT, kind, kind, "pending", session_id=sid)
            elif kind in ("prediction", "what_if", "retrieval"):
                self.answer(week, sid, kind, f"{kind} question")
            elif kind == "gap":
                item = self.store.record_baseline(
                    f"{sid}.py", "full solution", [{"description": "backoff delay", "hint": "cap it", "concept": CONCEPT}],
                    session_id=sid)["withheld_items"][0]
                if delegator:
                    res = self.store.update_withheld(item, "handed_back", ref=sid)
                    self.log(week, "make it yours", "Revealed Claude's answer to the gap",
                             "review_withheld_item(handed_back)", res)
                elif self.succeeds(bonus=0.15):  # the hint helps
                    self.learn("gap")
                    res = self.store.update_withheld(item, "as_expected", ref=sid)
                    self.log(week, "make it yours", "Filled the gap; it matches Claude's version",
                             "review_withheld_item(as_expected)", res)
                else:
                    self.learn("gap")
                    res = self.store.record_evidence(CONCEPT, "gap_completion", False, detail="incorrect", ref=sid)
                    self.log(week, "make it yours", "Filled the gap, but it doesn't work",
                             "record_evidence(gap_completion, incorrect)", res)
            elif kind == "critique":
                found = not delegator and self.succeeds(bonus=-0.45)  # real flaws are rarer than right answers
                if found:
                    self.learn("critique")
                    res = self.store.record_evidence(CONCEPT, "critique", True, ref=sid)
                    self.log(week, "make it yours", "Pushed back on a real flaw in Claude's draft",
                             "record_evidence(critique, passed)", res)

        if delegator:
            if self.rng.random() < 0.5:
                res = self.store.record_evidence(CONCEPT, "re_explain_request", None, ref=sid)
                self.log(week, "make it yours", "Asked Claude to explain its earlier answer again",
                         "record_evidence(re_explain_request)", res)
        else:
            solo = 1 + min(3, int(self.k * 4))  # deeper questions as understanding grows
            self.learn("question")
            res = self.store.record_evidence(CONCEPT, "question", detail=str(solo), ref=sid)
            self.log(week, "make it yours", f"Asked a question (SOLO level {solo})",
                     f"record_evidence(question, SOLO {solo})", res)

        if explain_back:
            self.answer(week, sid, "retrieval", "Explain it back", "finish")

        self.commits += 1
        own = 0 if delegator else round(10 * self.k)
        totals = {"claude": 10 - own, "claude_modified": own // 2, "human": own - own // 2}
        self.store.record_commit(f"{sid}-c", sid, {"files": {f"{sid}.py": {}}, "totals": totals})
        self.log(week, "finish", "Committed" + (" Claude's code unchanged" if delegator else " their version"),
                 f"record_commit(claude={totals['claude']}, edited={totals['claude_modified']}, theirs={totals['human']})")

    def unassisted(self, week: int) -> None:
        """A task on the concept done without Claude; it's tagged at the next session."""
        if self.succeeds():
            self.learn("unassisted")
            self.store.record_commit(f"{self.key}-w{week:02d}-solo", None,
                                     {"files": {"solo.py": {}}, "totals": {"claude": 0, "claude_modified": 0, "human": 12}})
            self.log(week, "on their own", "Did a task on the concept without Claude", "record_commit(theirs=12)")

    def snapshot(self, week: int) -> None:
        ctx = self.store.context([CONCEPT], session_id=None)["concepts"]
        c = ctx[0] if ctx else {"level": 0, "track": "one_off", "review_on": None, "plan": {"interventions": []}}
        r = report.build(self.store.conn)["criteria"]
        self.weeks.append({
            "week": week, "level": c["level"], "true_knowledge": round(self.k, 3), "true_level": true_level(self.k),
            "track": c["track"], "next_review": c["review_on"], "plan_next": c["plan"]["interventions"],
            "flags": sorted({f["behavior"] for f in self.store.summary()["non_learning_flags"]}),
            "signals": {
                "A_unassisted": r["A_unassisted"]["unassisted_use_evidence"],
                "B_reexplain_requests": self.store.conn.execute(
                    "SELECT COUNT(*) FROM evidence WHERE signal_type = 're_explain_request'").fetchone()[0],
                "C_solo_last_30d": r["C_harder_questions"]["solo_avg_last_30d"],
                "D_critiques": r["D_critical_engagement"]["critiques"] + r["D_critical_engagement"]["substantive_fixes"],
            },
        })

    def run(self) -> dict:
        away = self.p["away"]
        for week in range(1, WEEKS + 1):
            for n, day in enumerate(TASK_DAYS, start=1):
                self.t = START + timedelta(weeks=week - 1, days=day)
                if away and away[0] <= week <= away[1]:
                    continue
                self.task(week, n)
            start = self.p["unassisted_from"]
            if start and week >= start and week % 2 == 0 and not (away and away[0] <= week <= away[1]):
                self.t = START + timedelta(weeks=week - 1, days=5)
                self.forget()
                self.unassisted(week)
            self.t = START + timedelta(weeks=week - 1, days=6, hours=12)
            self.forget()
            # What the model would say if a session started now: decay applies after 60 idle days.
            mastery.apply_decay(self.store.conn)
            self.store.conn.commit()
            self.snapshot(week)
        db.set_clock(None)
        errors = [abs(w["level"] - w["true_level"]) for w in self.weeks]
        return {
            "persona": self.key, **{k: self.p[k] for k in ("name", "summary")},
            "inputs": {"starting_knowledge": self.p["knowledge"], "behavior": self.p["behavior"],
                       "away_weeks": self.p["away"], "works_without_claude_from_week": self.p["unassisted_from"]},
            "weeks": self.weeks, "events": self.events,
            "tracking": {"mean_level_error": round(sum(errors) / len(errors), 2),
                         "weeks_exact": sum(e == 0 for e in errors), "weeks_within_one": sum(e <= 1 for e in errors)},
        }


def simulate(seed: int = SEED) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        runs = {key: Sim(key, Path(tmp) / f"{key}.db", seed).run() for key in PERSONAS}
    return {"concept": CONCEPT, "start": START.date().isoformat(), "weeks": WEEKS, "seed": seed,
            "tasks_per_week": len(TASK_DAYS), "runs": runs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", help="write the full trace here")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    out = simulate(args.seed)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2) + "\n")
    for run in out["runs"].values():
        w = run["weeks"]
        print(f"\n{run['name']}: {run['summary']}")
        print("  model level by week: " + " ".join(str(x["level"]) for x in w))
        print("  true level by week:  " + " ".join(str(x["true_level"]) for x in w))
        last = w[-1]
        print(f"  end: level {last['level']}, true knowledge {last['true_knowledge']}, plan {last['plan_next']},"
              f" flags {last['flags'] or 'none'}, signals {last['signals']}")
        print(f"  tracking: {run['tracking']}")


if __name__ == "__main__":
    main()
