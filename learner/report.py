"""Progress report against the pilot's exit criteria.

    python -m learner.report          # readable summary
    python -m learner.report --json   # machine-readable
"""

import json
import sys
from datetime import timedelta

from learner import mastery
from learner.db import connect, now, parse


def _count(conn, sql, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


def build(conn, at: str | None = None) -> dict:
    at = at or now()
    d30 = (parse(at) - timedelta(days=30)).isoformat(timespec="seconds")
    d60 = (parse(at) - timedelta(days=60)).isoformat(timespec="seconds")

    def reasks(start, end):
        return _count(conn, "SELECT COUNT(*) FROM evidence WHERE signal_type = 'conceptual_reask' AND at > ? AND at <= ?",
                      start, end)

    def solo_avg(start, end):
        rows = conn.execute(
            "SELECT detail FROM evidence WHERE signal_type = 'question' AND at > ? AND at <= ?", (start, end)
        ).fetchall()
        vals = [int(r[0]) for r in rows if r[0] and r[0].isdigit()]
        return round(sum(vals) / len(vals), 2) if vals else None

    commits = conn.execute("SELECT claude_lines, modified_lines, human_lines FROM commits").fetchall()
    unassisted = sum(1 for c in commits if c[0] == 0 and c[1] == 0 and c[2] > 0)
    resolved = conn.execute("SELECT outcome FROM predictions WHERE outcome IS NOT NULL").fetchall()
    interventions = conn.execute("SELECT outcome FROM interventions WHERE outcome != 'pending'").fetchall()
    sessions = conn.execute("SELECT dial_override FROM sessions").fetchall()

    return {
        "as_of": at,
        "criteria": {
            "A_unassisted": {
                "unassisted_use_evidence": _count(conn, "SELECT COUNT(*) FROM evidence WHERE signal_type = 'unassisted_use'"),
                "commits": len(commits),
                "commits_without_claude_lines": unassisted,
            },
            "B_fewer_repeat_asks": {"reasks_prior_30d": reasks(d60, d30), "reasks_last_30d": reasks(d30, at)},
            "C_harder_questions": {"solo_avg_prior_30d": solo_avg(d60, d30), "solo_avg_last_30d": solo_avg(d30, at)},
            "D_critical_engagement": {
                "critiques": _count(conn, "SELECT COUNT(*) FROM evidence WHERE signal_type = 'critique' AND passed = 1"),
                "substantive_fixes": _count(conn, "SELECT COUNT(*) FROM evidence WHERE signal_type = 'substantive_fix'"),
            },
        },
        "rule_predictions": {
            "resolved": len(resolved),
            "correct": sum(r[0] for r in resolved),
        },
        "learning_kept_on": {
            "sessions": len(sessions),
            "sessions_dial_off": sum(1 for s in sessions if s[0] == "off"),
            "interventions": len(interventions),
            "interventions_skipped": sum(1 for i in interventions if i[0] == "skipped"),
        },
        "skill_tracks": [
            dict(r) for r in conn.execute(
                "SELECT c.name, s.level, s.mode FROM concept_state s JOIN concepts c ON c.id = s.concept_id"
                " WHERE s.track = 'skill' ORDER BY s.level DESC, c.name"
            )
        ],
        "non_learning_flags": mastery.non_learning_flags(conn, at),
    }


def render(r: dict) -> str:
    c = r["criteria"]
    p = r["rule_predictions"]
    k = r["learning_kept_on"]
    lines = [
        f"claude-learn progress, as of {r['as_of']}",
        "",
        "A  Does tasks without Claude",
        f"   unassisted-use evidence: {c['A_unassisted']['unassisted_use_evidence']}",
        f"   commits with no Claude lines: {c['A_unassisted']['commits_without_claude_lines']} of {c['A_unassisted']['commits']}",
        "B  Fewer repeat asks",
        f"   conceptual re-asks: {c['B_fewer_repeat_asks']['reasks_prior_30d']} (prior 30d) -> "
        f"{c['B_fewer_repeat_asks']['reasks_last_30d']} (last 30d)",
        "C  Harder questions",
        f"   average SOLO level: {c['C_harder_questions']['solo_avg_prior_30d']} -> {c['C_harder_questions']['solo_avg_last_30d']}",
        "D  Critical engagement",
        f"   critiques: {c['D_critical_engagement']['critiques']}, substantive fixes: {c['D_critical_engagement']['substantive_fixes']}",
        "",
        f"Rule predictions correct: {p['correct']} of {p['resolved']}",
        f"Learning kept on: {k['sessions'] - k['sessions_dial_off']} of {k['sessions']} sessions; "
        f"{k['interventions_skipped']} of {k['interventions']} interventions skipped",
        "",
        "Skill tracks:",
        *[f"   {s['name']}: level {s['level']} ({mastery.LEVEL_NAMES[s['level']]}, {s['mode']})" for s in r["skill_tracks"]],
        *(["   (none yet)"] if not r["skill_tracks"] else []),
    ]
    if r["non_learning_flags"]:
        lines += ["", "Signs learning may not be happening:"]
        lines += [f"   {f['concept_id']}: {f['behavior']} ({f['suggests']})" for f in r["non_learning_flags"]]
    return "\n".join(lines)


def main() -> None:
    report = build(connect())
    print(json.dumps(report, indent=2) if "--json" in sys.argv else render(report))


if __name__ == "__main__":
    main()
