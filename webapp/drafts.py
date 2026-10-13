"""One draft, two views.

Claude writes a single complete draft and marks the spans it wants the learner
to write. The baseline is the draft with the markers removed; the scaffold
replaces each marked span with a TODO(learner) line carrying a hint. That keeps
the baseline and the scaffold consistent by construction.

Marker syntax (each on its own line):

    [[GAP gap-1]]
    ...lines the learner will write...
    [[/GAP]]
"""

import re
from difflib import SequenceMatcher

from learner.attribution import MODIFIED_THRESHOLD, line_hash, significant

_OPEN = re.compile(r"^\s*\[\[GAP\s+([\w-]+)\]\]\s*$")
_CLOSE = re.compile(r"^\s*\[\[/GAP\]\]\s*$")
_TODO = re.compile(r"TODO\(learner\)\s+([\w-]+)")


def unfence(draft: str) -> str:
    """Drop a Markdown code fence wrapped around the whole draft."""
    lines = draft.strip("\n").splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return draft


def split(draft: str) -> tuple[str, dict[str, str]]:
    """Return (baseline, {gap_id: withheld text}). Unbalanced markers are dropped, keeping the text."""
    baseline, spans, current, buffer = [], {}, None, []
    for line in unfence(draft).splitlines():
        if (m := _OPEN.match(line)) and current is None:
            current, buffer = m.group(1), []
        elif _CLOSE.match(line) and current is not None:
            spans[current] = "\n".join(buffer)
            baseline.extend(buffer)
            current = None
        elif current is not None:
            buffer.append(line)
        elif not _OPEN.match(line) and not _CLOSE.match(line):
            baseline.append(line)
    if current is not None:  # never closed: keep the text, drop the gap
        baseline.extend(buffer)
    return "\n".join(baseline), spans


def _todo(gap: dict, indent: str, kind: str) -> str:
    text = f"TODO(learner) {gap['gap_id']}: {gap['description']} Hint: {gap['hint']}"
    return f"{indent}# {text}" if kind == "code" else f"{indent}[{text}]"


def scaffold(draft: str, gaps: list[dict], kind: str) -> str:
    """The draft with each registered gap's span replaced by its TODO line.

    Only gaps the server registered (and can reveal and review) are withheld.
    Spans with no matching gap, and a span whose marker never closes, stay as
    complete text, matching what split() keeps in the baseline.
    """
    by_id = {g["gap_id"]: g for g in gaps}
    out, inside, buffer = [], None, []
    for line in unfence(draft).splitlines():
        if (m := _OPEN.match(line)) and inside is None:
            inside, buffer = m.group(1), []
        elif _CLOSE.match(line) and inside is not None:
            if inside in by_id:
                indent = next((l[: len(l) - len(l.lstrip())] for l in buffer if l.strip()), "")
                out.append(_todo(by_id[inside], indent, kind))
            else:
                out.extend(buffer)
            inside = None
        elif inside is not None:
            buffer.append(line)
        elif not _OPEN.match(line) and not _CLOSE.match(line):
            out.append(line)
    if inside is not None:  # never closed: keep the text
        out.extend(buffer)
    return "\n".join(out)


def unfilled(text: str) -> set[str]:
    """Gap ids whose TODO(learner) marker is still in the learner's text."""
    return set(_TODO.findall(text))


def ownership(claude_text: str, learner_text: str) -> dict:
    """Classify each line of the learner's version against what Claude wrote.

    Uses the same rules as commit attribution: identical lines are Claude's,
    close matches are Claude's lines edited by the learner, the rest are the learner's.
    """
    claude_lines = [l.strip() for l in claude_text.splitlines() if significant(l) and not _TODO.search(l)]
    exact = {line_hash(l) for l in claude_lines}
    lines, totals = [], {"claude": 0, "claude_modified": 0, "human": 0}
    for line in learner_text.splitlines():
        if not significant(line):
            lines.append({"text": line, "cls": "blank"})
            continue
        if line_hash(line) in exact:
            cls = "claude"
        elif any(SequenceMatcher(None, line.strip(), c).ratio() >= MODIFIED_THRESHOLD for c in claude_lines):
            cls = "claude_modified"
        else:
            cls = "human"
        totals[cls] += 1
        lines.append({"text": line, "cls": cls})
    counted = sum(totals.values())
    share = round((totals["claude_modified"] + totals["human"]) / counted, 2) if counted else 0.0
    return {"lines": lines, "totals": totals, "learner_share": share}
