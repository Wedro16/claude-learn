"""MCP server exposing the learner model to Claude Code.

Run with ``python -m learner.server`` (stdio). Wired up in the repo's .mcp.json.
"""

import functools
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from learner.store import LearnerStore, current_session_id

INSTRUCTIONS = """\
Tools for the learner model of the person you are working with. Call
get_learner_context before substantial work, record concepts and evidence as
you go, and record a baseline whenever you withhold work for the learner.
Never record personal topics (HR, health, personal legal matters)."""

server = MCPServer(name="learner", instructions=INSTRUCTIONS)


def store() -> LearnerStore:
    # Tools run on worker threads and SQLite connections are bound to their
    # thread, so each call opens its own (cheap) connection.
    return LearnerStore()


def tool(fn):
    """Register a tool; expected errors (unknown concept, bad value) reach Claude with their message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    return server.tool()(wrapper)


Signal = Literal[
    "self_explanation", "prediction", "what_if", "conceptual_reask", "reference_lookup",
    "question", "critique", "re_explain_request", "unassisted_use", "gap_completion",
    "substantive_fix", "unchanged_merge", "draft_ownership",
]


@tool
def get_learner_context(concepts: list[str]) -> dict:
    """Level, track, dial and recommended interventions for the concepts this task involves.

    Call this before substantial work. Unknown concepts come back with a plan for a new topic.
    """
    return store().context(concepts)


@tool
def get_session_summary() -> dict:
    """What this session changed: level changes and the rules behind them, evidence recorded, and for
    each concept the session touched, its level, its next review date and what you'll lead with next time.

    Use it for the summary when the person finishes a task.
    """
    return store().session_summary()


@tool
def get_learner_summary() -> dict:
    """Skill tracks, due reviews, commits to tag, withheld items to review, and non-learning flags."""
    s = store()
    return s.summary(repo=s.session_repo(current_session_id()))


@tool
def record_concept(
    name: str,
    description: str = "",
    domain: Literal["code", "writing", "analysis"] = "code",
    aliases: list[str] | None = None,
    relations: list[dict] | None = None,
) -> dict:
    """Record a transferable concept the current task involves (not a task, not an incidental detail).

    Records an occurrence for this session, which drives skill-track promotion.
    relations: [{"concept": "<name>", "type": "prerequisite" | "part_of" | "used_with"}].
    """
    return store().record_concept(name, description, domain, aliases, relations, context_id=current_session_id())


@tool
def record_evidence(
    concept: str,
    signal_type: Signal,
    passed: bool | None = None,
    detail: str | None = None,
) -> dict:
    """Record one observation about the learner's understanding of a concept.

    question: detail is the SOLO level 1-4 (1 single fact, 2 several facts, 3 relations, 4 new cases).
    conceptual_reask: a "why/how does" question on a concept they already showed they understood.
    reference_lookup: syntax/API/style lookups; logged, never counted against them.
    critique / substantive_fix: passed=true when their critique or fix improved the result.
    """
    return store().record_evidence(concept, signal_type, passed, detail, ref=current_session_id())


@tool
def record_intervention(
    concept: str,
    kind: Literal[
        "orientation", "worked_example", "gap", "takeaway", "decision_explanation", "prediction",
        "retrieval", "self_explanation", "what_if", "critique",
    ],
    prompt: str,
    outcome: Literal["correct", "partial", "incorrect", "skipped", "pending"],
    response: str | None = None,
) -> dict:
    """Log an intervention you made and how the learner responded. Correct/incorrect answers become evidence."""
    return store().record_intervention(concept, kind, prompt, outcome, response)


@tool
def record_baseline(path: str, content: str, withheld: list[dict]) -> dict:
    """Store the complete draft you would have written, and the items you left for the learner.

    withheld: [{"description": ..., "hint": ..., "concept": ..., "location": <file path>}].
    Returns item ids; put each id in the TODO(learner) marker for that gap.
    """
    return store().record_baseline(path, content, withheld)


@tool
def get_withheld_item(item_id: str) -> dict:
    """A withheld item with its baseline draft, for checking the learner's completion or revealing it."""
    return store().get_baseline(item_id)


@tool
def review_withheld_item(
    item_id: str,
    status: Literal["as_expected", "different", "handed_back"],
) -> dict:
    """Record how the learner finished a withheld item, compared with the baseline.

    as_expected: matches the baseline's approach. different: works but differs (possibly better).
    handed_back: they asked you to do it, or left it undone.
    """
    return store().update_withheld(item_id, status)


@tool
def tag_commit(sha: str, concepts: list[str]) -> list[dict]:
    """Attach the concepts a commit exercises; its line attribution then becomes evidence."""
    return store().tag_commit(sha, concepts)


@tool
def set_learning_dial(
    dial: Literal["off", "light", "full"] | None = None,
    mode: Literal["about", "how_to"] | None = None,
    concept: str | None = None,
) -> dict:
    """Set the dial for a topic (with concept), or for the current request only (without concept).

    A request-only setting is cleared when the person sends their next message.

    mode: "about" to understand a concept, "how_to" to be able to do it. Topic-level only.
    """
    if concept:
        return store().set_dial(concept, dial, mode)
    return store().set_session_dial(dial or "full")


@tool
def set_track(concept: str, track: Literal["one_off", "skill"]) -> dict:
    """Promote or demote a topic at the learner's request. Pins it so automatic promotion leaves it alone."""
    return store().set_track(concept, track)


@tool
def explain_concept(concept: str) -> dict:
    """Everything recorded about a concept and every level change, so the learner can see why."""
    return store().explain(concept)


if __name__ == "__main__":
    server.run()
