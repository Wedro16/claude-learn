"""Claude calls for the demo: do the task for a learner, review their work, grade predictions.

Every call has an offline path so the demo keeps working without an API key,
over the rate limit, or during an outage: task runs replay a recorded live
response, and reviews fall back to a simple, clearly labeled heuristic.
"""

import json
import os
import re
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
RECORDED = Path(__file__).parent / "recorded"

POLICY = """\
You are Claude, working with a professional who wants the work done AND wants to own what you
produce together. You adapt how much you teach to the learner's level on each concept, using the
plan you are given. Rules:

- Learning never gates the work: always produce a complete, high-quality draft.
- Use only the interventions the plan allows. Leave a field empty ("" or []) when its
  intervention is not in the plan.
- Teach the transferable idea, not trivia. Be brief: a sentence or two per field.
- decision_notes: the key decisions in your draft, an alternative you rejected and why, and any
  assumption worth questioning. Always include these, 2-4 short items.
- orientation (only if "orientation" is allowed): the core mental model in 2-3 sentences plus
  3-5 key terms with one-line definitions, written for someone new to the idea.
- prediction_prompt (only if "prediction" or "what_if" is allowed): ONE question the learner can
  answer in a sentence that makes them think about how the solution behaves (a prediction, or
  "what happens if..."). Not trivia.
- gaps (only if "gap" is allowed): choose 1 (at most 2) meaningful pieces the learner can write
  with a hint, tied to a plan concept: logic or an argument, never boilerplate or imports.
  In the draft, wrap each withheld span with marker lines exactly like:
      [[GAP gap-1]]
      ...the complete lines you would have written...
      [[/GAP]]
  Markers go on their own lines, around whole lines. The draft must stay complete: the text
  between markers is the real answer. For each gap give a description of what to write and a
  hint that points toward the idea without giving the answer away.
- critique_invitation (only if "critique" is allowed): invite the learner to challenge a
  specific choice in your draft, naming the trade-off.
- takeaway: one sentence the learner should remember. Always include it.
- For writing tasks the draft is the text itself (Markdown), not a description of it.
- The draft is the raw file content: never wrap it in a code fence.
"""

RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "orientation": {"type": "string"},
        "decision_notes": {"type": "array", "items": {"type": "string"}},
        "prediction_prompt": {"type": "string"},
        "draft": {"type": "string"},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap_id": {"type": "string"},
                    "concept": {"type": "string"},
                    "description": {"type": "string"},
                    "hint": {"type": "string"},
                },
                "required": ["gap_id", "concept", "description", "hint"],
                "additionalProperties": False,
            },
        },
        "critique_invitation": {"type": "string"},
        "takeaway": {"type": "string"},
    },
    "required": [
        "orientation", "decision_notes", "prediction_prompt", "draft",
        "gaps", "critique_invitation", "takeaway",
    ],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["as_expected", "different", "incorrect", "not_attempted"]},
                    "feedback": {"type": "string"},
                },
                "required": ["gap_id", "verdict", "feedback"],
                "additionalProperties": False,
            },
        },
        "other_changes": {"type": "string"},
        "caught_issue": {"type": "boolean"},
    },
    "required": ["gaps", "other_changes", "caught_issue"],
    "additionalProperties": False,
}

DISCUSS_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "kind": {"type": "string", "enum": ["explanation", "question", "pushback", "other"]},
        "understanding": {"type": "string", "enum": ["correct", "partial", "incorrect", "not_applicable"]},
        "concept": {"type": "string"},
        "solo": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
        "pushback_valid": {"type": "boolean"},
    },
    "required": ["reply", "kind", "understanding", "concept", "solo", "pushback_valid"],
    "additionalProperties": False,
}

DISCUSS_POLICY = """\
You are talking through a learner's edits with them, like a thoughtful senior colleague. They may
explain the reasoning behind a change, ask why something works, or push back on your version.

Reply in 2-4 sentences, specific to their code or text:
- Reasoning right: say so and why it matters. Partly right: name exactly what's missing.
  Wrong: correct it gently with the key idea.
- Pushback: if they're right, concede plainly and say what that changes; if not, explain the
  trade-off without being defensive.
- Question: answer it directly.
- End with one follow-up question only when it would deepen their understanding.
- Never give away the answer to an open gap (listed below). Nudge instead.

Then classify THEIR latest message:
- kind: explanation (their reasoning for something they did), question, pushback (arguing a
  choice in your draft is wrong or worse), or other.
- understanding: for an explanation, whether the reasoning is correct, partial or incorrect;
  otherwise not_applicable.
- concept: the one listed concept the message is mostly about.
- solo: for a question, its SOLO level (1 single fact, 2 several facts, 3 how things relate,
  4 applying to a new case); otherwise 0.
- pushback_valid: true only if their pushback identifies a real improvement or flaw.
"""

FINAL_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "ready": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "minor"]},
                    "description": {"type": "string"},
                },
                "required": ["severity", "description"],
                "additionalProperties": False,
            },
        },
        "teach_back_question": {"type": "string"},
    },
    "required": ["ready", "issues", "teach_back_question"],
    "additionalProperties": False,
}

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["correct", "partial", "incorrect"]},
        "feedback": {"type": "string"},
    },
    "required": ["outcome", "feedback"],
    "additionalProperties": False,
}


class Unavailable(Exception):
    """Live Claude can't be used for this call; use the offline path."""


_client = None


def client() -> anthropic.Anthropic:
    global _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise Unavailable("no API key configured")
    if _client is None:
        _client = anthropic.Anthropic(max_retries=2, timeout=120.0)
    return _client


def _call(system: str, user: str | list[dict], schema: dict, effort: str, max_tokens: int) -> dict:
    """One structured-output request; user is a message or a full message history."""
    messages = [{"role": "user", "content": user}] if isinstance(user, str) else user
    try:
        response = client().beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            # On a safety decline, the API reruns the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
        raise Unavailable(f"Claude API unavailable: {exc.__class__.__name__}") from exc
    except anthropic.APIStatusError as exc:
        raise Unavailable(f"Claude API error {exc.status_code}") from exc
    if response.stop_reason in ("refusal", "max_tokens"):
        raise Unavailable(f"stopped: {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def describe_plan(task: dict, context: dict) -> str:
    lines = []
    for c in context["concepts"]:
        lines.append(
            f"- {c['name']}: level {c['level']} ({c['level_name']}), track {c['track']}, "
            f"allowed interventions: {', '.join(c['plan']['interventions']) or 'none'}"
        )
    return "\n".join(lines)


def run_task(task: dict, context: dict) -> dict:
    """Ask Claude to do the task, adapted to the learner's plan."""
    allowed = sorted({k for c in context["concepts"] for k in c["plan"]["interventions"]})
    user = (
        f"Task ({task['kind']}): {task['request']}\n\n"
        f"Material:\n```{task['language']}\n{task['material']}\n```\n\n"
        f"Learner plan by concept (levels: 0 exposed, 1 explain, 2 predict, 3 modify, 4 critique):\n"
        f"{describe_plan(task, context)}\n\n"
        f"Allowed interventions for this task: {', '.join(allowed) or 'none'}.\n"
        f"Gap concepts must be one of: {', '.join(task['concepts'])}."
    )
    return _call(POLICY, user, RUN_SCHEMA, effort="medium", max_tokens=16000)


def review(task: dict, baseline: str, scaffold: str, learner_text: str, gaps: list[dict]) -> dict:
    """Compare the learner's completed version with the baseline, gap by gap."""
    system = (
        "You review a learner's completion of gaps that were left for them in a draft. Compare each "
        "gap with the baseline's version. Verdicts: as_expected (same approach, works), different "
        "(different approach that still works; say whether it's better or worse and why), incorrect "
        "(would not work or misses the point; give a nudge, not the answer), not_attempted (the "
        "TODO(learner) marker is still there). Feedback: 1-2 sentences, specific, encouraging, "
        "never condescending. In other_changes, briefly note any meaningful edits outside the gaps "
        "(or \"\"). caught_issue: true only if their edits fix a real problem in the baseline."
    )
    user = (
        f"Task: {task['request']}\n\nGaps:\n{json.dumps(gaps, indent=2)}\n\n"
        f"Baseline (complete draft):\n```\n{baseline}\n```\n\n"
        f"Scaffold the learner received:\n```\n{scaffold}\n```\n\n"
        f"Learner's version:\n```\n{learner_text}\n```"
    )
    return _call(system, user, REVIEW_SCHEMA, effort="medium", max_tokens=8000)


def final_check(task: dict, baseline: str, learner_text: str) -> dict:
    """Read the learner's final version against the task before they use it."""
    system = (
        "The learner is about to use their final version. Check it against the task the way a "
        "good reviewer would. Report only real problems: blocking (it would fail, or doesn't do what "
        "the task asks) or minor (worth fixing; at most two). No style nitpicks, and say nothing "
        "about choices that differ from the reference but work. ready is true when nothing is "
        "blocking. Each description: one or two specific sentences pointing at the line or passage. "
        "Then write one teach-back question: ask the learner to explain, in a sentence or two and "
        "without looking, the most important idea in their final version."
    )
    user = (
        f"Task: {task['request']}\n\nReference version (for comparison only):\n```\n{baseline}\n```\n\n"
        f"Learner's final version:\n```\n{learner_text}\n```"
    )
    return _call(system, user, FINAL_CHECK_SCHEMA, effort="medium", max_tokens=6000)


def grade(task: dict, question: str, answer: str, baseline: str) -> dict:
    """Grade a learner's answer to a prediction or what-if question."""
    system = (
        "Grade a learner's short answer to a question about a solution. correct: the key idea is "
        "right even if informal. partial: on the right track but missing the key point. incorrect: "
        "wrong or empty. Feedback: 1-2 sentences that confirm or correct the idea; never condescending."
    )
    user = f"Solution:\n```\n{baseline}\n```\n\nQuestion: {question}\n\nLearner's answer: {answer}"
    return _call(system, user, GRADE_SCHEMA, effort="low", max_tokens=4000)


def discuss(task: dict, baseline: str, learner_text: str, open_gaps: list[dict], history: list[dict], message: str) -> dict:
    """Respond to the learner explaining, questioning or pushing back on their edits."""
    system = (
        f"{DISCUSS_POLICY}\nTask: {task['request']}\nConcepts: {', '.join(task['concepts'])}\n\n"
        f"Your complete version:\n```\n{baseline}\n```\n\n"
        f"Open gaps (don't reveal their answers): {json.dumps(open_gaps) if open_gaps else 'none'}"
    )
    # The learner's current version rides with every message, so the reply sees their latest edits.
    turns = [{"role": t["role"], "content": t["content"]} for t in history]
    turns.append({"role": "user", "content": f"My current version:\n```\n{learner_text}\n```\n\n{message}"})
    return _call(system, turns, DISCUSS_SCHEMA, effort="low", max_tokens=4000)


# -- offline paths -----------------------------------------------------------


def recorded_run(task_id: str, profile: str, allowed: set[str]) -> tuple[dict, bool]:
    """A recorded live response for this task, preferring one made for the same plan.

    Returns (response, matched). Each recording stores the interventions its plan
    allowed; a learner whose model has moved on may match another profile's
    recording. Without a match, the profile's own recording is returned unmatched.
    """
    candidates = sorted(RECORDED.glob(f"{task_id}-*.json"), key=lambda p: p.stem != f"{task_id}-{profile}")
    if not candidates:
        raise Unavailable("no recorded response for this task")
    for path in candidates:
        out = json.loads(path.read_text())
        if set(out.get("_plan", [])) == allowed:
            return out, True
    own = RECORDED / f"{task_id}-{profile}.json"
    return json.loads((own if own.exists() else candidates[0]).read_text()), False


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def heuristic_review(baseline_spans: dict[str, str], learner_text: str, gaps: list[dict]) -> dict:
    """Offline review: unfilled markers are not attempted; exact matches are as expected."""
    from webapp.drafts import unfilled

    open_ids = unfilled(learner_text)
    out = []
    for g in gaps:
        expected = _norm(baseline_spans.get(g["gap_id"], ""))
        if g["gap_id"] in open_ids:
            verdict, feedback = "not_attempted", "The TODO(learner) marker is still there."
        elif expected and expected in _norm(learner_text):
            verdict, feedback = "as_expected", "Matches the baseline."
        else:
            # Without Claude there's no way to tell a working alternative from a wrong one,
            # so it earns no credit and the gap stays open for a live review.
            verdict = "unverified"
            feedback = "Differs from the baseline, so it can't be checked offline. Turn on Live Claude to have it reviewed."
        out.append({"gap_id": g["gap_id"], "verdict": verdict, "feedback": feedback})
    return {"gaps": out, "other_changes": "", "caught_issue": False}


def heuristic_final_check(task: dict, learner_text: str) -> dict:
    """Offline: the only thing we can check for sure is unfinished TODO markers."""
    from webapp.drafts import unfilled

    issues = [{"severity": "blocking", "description": f"{gap_id} still has its TODO(learner) marker."}
              for gap_id in sorted(unfilled(learner_text))]
    issues.append({"severity": "minor", "description": "Offline check only: turn on Live Claude for a real review."})
    return {"ready": not any(i["severity"] == "blocking" for i in issues), "issues": issues,
            "teach_back_question": task["teach_back"]}


def heuristic_grade() -> dict:
    return {"outcome": "partial", "feedback": "Recorded. (Offline mode: live Claude would grade this answer.)"}
