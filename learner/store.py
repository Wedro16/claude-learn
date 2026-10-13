"""High-level learner model operations shared by the MCP server, hooks and report."""

import json
import sqlite3
from datetime import timedelta

from learner import concepts, mastery
from learner.db import connect, home, now, parse

# Spaced review intervals for skill-track concepts, by level.
REVIEW_AFTER = {1: timedelta(days=2), 2: timedelta(days=7), 3: timedelta(days=21), 4: timedelta(days=45)}

# Intervention kinds that are themselves evidence of understanding.
INTERVENTION_SIGNAL = {
    "prediction": "prediction",
    "retrieval": "self_explanation",
    "self_explanation": "self_explanation",
    "what_if": "what_if",
    "critique": "critique",
}
INTERVENTION_KINDS = {
    "orientation", "worked_example", "gap", "takeaway", "decision_explanation", *INTERVENTION_SIGNAL,
}
REVIEWED = ("as_expected", "different", "handed_back")


class NotFound(ValueError):
    pass


def current_session_id() -> str | None:
    path = home() / "current_session.json"
    try:
        return json.loads(path.read_text())["id"]
    except (OSError, ValueError, KeyError):
        return None


def set_current_session(session_id: str, cwd: str | None) -> None:
    home().mkdir(mode=0o700, parents=True, exist_ok=True)
    (home() / "current_session.json").write_text(json.dumps({"id": session_id, "cwd": cwd, "at": now()}))


class LearnerStore:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or connect()

    # -- sessions ----------------------------------------------------------

    def start_session(self, session_id: str, cwd: str | None = None, repo: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (id, cwd, repo, started_at) VALUES (?, ?, ?, ?)",
                (session_id, cwd, repo, now()),
            )
            mastery.apply_decay(self.conn)

    def session_repo(self, session_id: str | None) -> str | None:
        row = self.conn.execute("SELECT repo FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row["repo"] if row else None

    def end_session(self, session_id: str, task_summary: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET ended_at = ?, task_summary = COALESCE(?, task_summary) WHERE id = ?",
                (now(), task_summary, session_id),
            )

    # -- concepts ----------------------------------------------------------

    def _concept_id(self, name: str) -> str:
        cid = concepts.find(self.conn, name)
        if cid is None:
            raise NotFound(f"Unknown concept {name!r}; record it with record_concept first.")
        return cid

    def record_concept(
        self,
        name: str,
        description: str = "",
        domain: str = "code",
        aliases: list[str] | None = None,
        relations: list[dict] | None = None,
        context_id: str | None = None,
    ) -> dict:
        related = [rel["concept"] for rel in relations or []]
        if concepts.is_sensitive(name, description, *(aliases or []), *related):
            return {"rejected": "sensitive", "message": "Personal topics are not tracked as learning concepts."}
        with self.conn:
            cid, created = concepts.resolve(self.conn, name, description, domain, aliases)
            for rel in relations or []:
                other, _ = concepts.resolve(self.conn, rel["concept"], domain=domain)
                self.conn.execute(
                    "INSERT OR IGNORE INTO relations (from_id, to_id, type) VALUES (?, ?, ?)",
                    (cid, other, rel["type"]),
                )
            if context_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO occurrences (concept_id, context_id, at) VALUES (?, ?, ?)",
                    (cid, context_id, now()),
                )
            track = mastery.update_track(self.conn, cid)
        return {"concept_id": cid, "created": created, **track}

    # -- evidence ----------------------------------------------------------

    def record_evidence(
        self,
        concept: str,
        signal_type: str,
        passed: bool | None = None,
        detail: str | None = None,
        ref: str | None = None,
        at: str | None = None,
    ) -> dict:
        if signal_type not in mastery.SIGNALS:
            raise ValueError(f"Unknown signal_type {signal_type!r}. Known: {sorted(mastery.SIGNALS)}")
        cid = self._concept_id(concept)
        source = "repo" if signal_type in mastery.REPO_SIGNALS else "conversation"
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO evidence (concept_id, signal_type, passed, source, ref, detail, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, signal_type, None if passed is None else int(passed), source, ref, detail, at or now()),
            )
            return mastery.apply_evidence(self.conn, cur.lastrowid)

    def record_intervention(
        self,
        concept: str,
        kind: str,
        prompt: str,
        outcome: str,
        response: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        if kind not in INTERVENTION_KINDS:
            raise ValueError(f"Unknown intervention kind {kind!r}. Known: {sorted(INTERVENTION_KINDS)}")
        cid = self._concept_id(concept)
        session_id = session_id or current_session_id()
        with self.conn:
            self.conn.execute(
                "INSERT INTO interventions (session_id, concept_id, kind, prompt, response, outcome, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, cid, kind, prompt, response, outcome, now()),
            )
        # Only clear outcomes count as evidence; partial answers and skips are logged only.
        signal = INTERVENTION_SIGNAL.get(kind)
        if signal and outcome in ("correct", "incorrect"):
            return self.record_evidence(cid, signal, outcome == "correct", ref=session_id)
        return {"concept_id": cid, "logged": True}

    # -- baselines and withheld items -------------------------------------

    def record_baseline(self, path: str, content: str, withheld: list[dict], session_id: str | None = None) -> dict:
        session_id = session_id or current_session_id()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO baselines (session_id, repo, path, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, self.session_repo(session_id), path, content, now()),
            )
            baseline_id = cur.lastrowid
            items = []
            for item in withheld:
                cid = concepts.find(self.conn, item["concept"]) if item.get("concept") else None
                item_id = item.get("item_id") or f"w{baseline_id}-{len(items) + 1}"
                self.conn.execute(
                    "INSERT INTO withheld (item_id, baseline_id, concept_id, description, hint, location, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, baseline_id, cid, item["description"], item.get("hint"),
                     item.get("location") or path, now()),
                )
                items.append(item_id)
        return {"baseline_id": baseline_id, "withheld_items": items}

    def get_baseline(self, item_id: str) -> dict:
        row = self.conn.execute(
            "SELECT w.*, b.path, b.content FROM withheld w JOIN baselines b ON b.id = w.baseline_id WHERE w.item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"No withheld item {item_id!r}")
        return dict(row)

    def update_withheld(self, item_id: str, status: str, commit_sha: str | None = None, ref: str | None = None) -> dict:
        item = self.get_baseline(item_id)
        if item["status"] in REVIEWED:
            # A gap is reviewed once; repeating it would count the same work twice.
            return {"item_id": item_id, "status": item["status"], "already_reviewed": True}
        with self.conn:
            self.conn.execute(
                "UPDATE withheld SET status = ?, commit_sha = COALESCE(?, commit_sha), updated_at = ? WHERE item_id = ?",
                (status, commit_sha, now(), item_id),
            )
        if status in REVIEWED and item["concept_id"]:
            return self.record_evidence(
                item["concept_id"], "gap_completion", status != "handed_back", detail=status,
                ref=commit_sha or item["commit_sha"] or ref,
            )
        return {"item_id": item_id, "status": status}

    # -- commits -----------------------------------------------------------

    def record_commit(
        self,
        sha: str,
        session_id: str | None,
        attribution: dict,
        repo: str | None = None,
        replaces: str | None = None,
    ) -> dict:
        t = attribution["totals"]
        # Every touched file, including ones with no significant added lines (a removed TODO marker).
        files = attribution.get("touched") or list(attribution["files"])
        with self.conn:
            tagged = 0
            if replaces:
                # An amend replaces the earlier commit; carry its tags over instead of counting both.
                old = self.conn.execute("SELECT tagged FROM commits WHERE sha = ?", (replaces,)).fetchone()
                tagged = old["tagged"] if old else 0
                self.conn.execute("DELETE FROM commits WHERE sha = ?", (replaces,))
                self.conn.execute("UPDATE commit_tags SET sha = ? WHERE sha = ?", (sha, replaces))
                self.conn.execute("UPDATE withheld SET commit_sha = ? WHERE commit_sha = ?", (sha, replaces))
            self.conn.execute(
                "INSERT OR REPLACE INTO commits"
                " (sha, session_id, repo, claude_lines, modified_lines, human_lines, files, tagged, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, session_id, repo, t["claude"], t["claude_modified"], t["human"], json.dumps(files), tagged, now()),
            )
            # Withheld items in touched files are now ready for Claude to review.
            pending = self.conn.execute(
                "SELECT w.item_id, w.location FROM withheld w JOIN baselines b ON b.id = w.baseline_id"
                " WHERE w.status = 'pending' AND (? IS NULL OR b.repo IS NULL OR b.repo = ?)",
                (repo, repo),
            ).fetchall()
            ready = [
                p["item_id"] for p in pending
                if any(f.endswith(p["location"]) or p["location"].endswith(f) for f in files)
            ]
            for item_id in ready:
                self.conn.execute(
                    "UPDATE withheld SET status = 'committed', commit_sha = ?, updated_at = ? WHERE item_id = ?",
                    (sha, now(), item_id),
                )
        return {"sha": sha, "totals": t, "withheld_ready_for_review": ready}

    def tag_commit(self, sha: str, concept_names: list[str]) -> list[dict]:
        """Attach concepts to a commit and turn its attribution into evidence."""
        commit = self.conn.execute("SELECT * FROM commits WHERE sha = ?", (sha,)).fetchone()
        if commit is None:
            raise NotFound(f"No recorded commit {sha!r}")
        claude, modified, human = commit["claude_lines"], commit["modified_lines"], commit["human_lines"]
        total = claude + modified + human
        # Claude-assisted work already counted an occurrence for its session; reuse it.
        context = commit["session_id"] or sha
        results = []
        for name in concept_names:
            rec = self.record_concept(name, context_id=context)
            if "rejected" in rec:
                results.append({"concept": name, **rec})
                continue
            cid = rec["concept_id"]
            with self.conn:
                fresh = self.conn.execute(
                    "INSERT OR IGNORE INTO commit_tags (sha, concept_id) VALUES (?, ?)", (sha, cid)
                ).rowcount
            if not fresh:
                # Each commit/concept pair counts once.
                results.append({"concept_id": cid, "already_tagged": True})
                continue
            if total and claude == 0 and modified == 0:
                res = self.record_evidence(cid, "unassisted_use", True, ref=sha)
            elif total and modified == 0 and human == 0:
                res = self.record_evidence(cid, "unchanged_merge", None, ref=sha)
            else:
                share = round((modified + human) / total, 2) if total else 0.0
                res = self.record_evidence(cid, "draft_ownership", None, detail=str(share), ref=sha)
            results.append(res)
        with self.conn:
            self.conn.execute("UPDATE commits SET tagged = 1 WHERE sha = ?", (sha,))
        return results

    # -- settings ----------------------------------------------------------

    def set_dial(self, concept: str, dial: str | None = None, mode: str | None = None) -> dict:
        cid = self._concept_id(concept)
        with self.conn:
            self.conn.execute(
                "UPDATE concept_state SET dial = COALESCE(?, dial), mode = COALESCE(?, mode) WHERE concept_id = ?",
                (dial, mode, cid),
            )
        return self._state(cid)

    def set_session_dial(self, dial: str, session_id: str | None = None) -> dict:
        """Override the dial for the current request. Cleared when the person sends their next message."""
        session_id = session_id or current_session_id()
        with self.conn:
            self.conn.execute("UPDATE sessions SET dial_override = ? WHERE id = ?", (dial, session_id))
        return {"session_id": session_id, "dial_override": dial, "until": "the person's next message"}

    def clear_session_dial(self, session_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sessions SET dial_override = NULL WHERE id = ?", (session_id,))

    def set_track(self, concept: str, track: str) -> dict:
        cid = self._concept_id(concept)
        with self.conn:
            self.conn.execute(
                "UPDATE concept_state SET track = ?, track_pinned = 1 WHERE concept_id = ?", (track, cid)
            )
        return self._state(cid)

    # -- reads -------------------------------------------------------------

    def _state(self, cid: str) -> dict:
        row = self.conn.execute(
            "SELECT c.id, c.name, c.domain, s.* FROM concepts c JOIN concept_state s ON s.concept_id = c.id"
            " WHERE c.id = ?", (cid,),
        ).fetchone()
        d = dict(row)
        d["level_name"] = mastery.LEVEL_NAMES[d["level"]]
        return d

    def _review_on(self, state: dict) -> str | None:
        """The date a skill-track concept comes back for review, if it has one yet."""
        if state["track"] != "skill" or state["level"] == 0 or not state["last_evidence_at"]:
            return None
        return (parse(state["last_evidence_at"]) + REVIEW_AFTER[state["level"]]).date().isoformat()

    def _review_due(self, state: dict, at: str) -> bool:
        if state["track"] != "skill" or state["level"] == 0 or not state["last_evidence_at"]:
            return False
        return parse(at) - parse(state["last_evidence_at"]) >= REVIEW_AFTER[state["level"]]

    def plan(self, state: dict, session_dial: str | None = None) -> dict:
        """Which interventions fit this concept right now."""
        dial = session_dial or state["dial"]
        if dial == "off":
            return {"dial": "off", "interventions": [], "note": "Learning is off: deliver the full output."}
        if state["track"] == "one_off":
            kinds = ["decision_explanation", "takeaway"]
        elif state["level"] == 0:
            kinds = ["orientation", "worked_example", "takeaway"]
        elif state["level"] <= 2:
            kinds = ["gap", "prediction", "what_if"] if state["mode"] == "how_to" else ["prediction", "gap", "retrieval"]
        else:
            kinds = ["critique", "gap"] if state["mode"] == "how_to" else ["critique", "what_if"]
        limit = 1 if dial == "light" else 3
        return {"dial": dial, "interventions": kinds[:limit]}

    def context(self, concept_names: list[str], session_id: str | None = None) -> dict:
        session_id = session_id or current_session_id()
        row = self.conn.execute("SELECT dial_override FROM sessions WHERE id = ?", (session_id,)).fetchone()
        session_dial = row["dial_override"] if row else None
        at = now()
        out = {"session_id": session_id, "session_dial": session_dial, "concepts": [], "unknown": []}
        for name in concept_names:
            cid = concepts.find(self.conn, name)
            if cid is None:
                out["unknown"].append({"name": name, "plan": self.plan(
                    {"track": "one_off", "level": 0, "dial": "full", "mode": "about"}, session_dial)})
                continue
            state = self._state(cid)
            state["review_due"] = self._review_due(state, at)
            state["review_on"] = self._review_on(state)
            state["plan"] = self.plan(state, session_dial)
            out["concepts"].append(state)
        return out

    def session_summary(self, session_id: str | None = None) -> dict:
        """What a session did to the learner model, for the summary when a task is finished.

        Counts only evidence recorded for this session, so other sessions never show up here.
        """
        session_id = session_id or current_session_id()
        rules = [dict(r) for r in self.conn.execute(
            "SELECT c.name AS concept, o.rule_id, o.from_level, o.to_level FROM rule_observations o"
            " JOIN evidence e ON e.id = o.evidence_id JOIN concepts c ON c.id = o.concept_id"
            " WHERE e.ref = ? AND o.passed = 1 AND o.from_level != o.to_level ORDER BY o.id", (session_id,))]
        first_last: dict[str, list[int]] = {}
        for r in rules:
            first_last.setdefault(r["concept"], [r["from_level"], r["to_level"]])[1] = r["to_level"]
        names = [r[0] for r in self.conn.execute(
            "SELECT c.name FROM occurrences o JOIN concepts c ON c.id = o.concept_id"
            " WHERE o.context_id = ? ORDER BY c.name", (session_id,))]
        upcoming = []
        for c in self.context(names, session_id=None)["concepts"]:
            upcoming.append({"concept": c["name"], "level": c["level"], "level_name": c["level_name"],
                             "track": c["track"], "review_on": c["review_on"], "next_plan": c["plan"]["interventions"]})
        return {
            "session_id": session_id,
            "changes": [{"concept": c, "from": a, "to": b} for c, (a, b) in first_last.items() if a != b],
            "rules": rules,
            "observations": self.conn.execute("SELECT COUNT(*) FROM evidence WHERE ref = ?", (session_id,)).fetchone()[0],
            "upcoming": upcoming,
        }

    def summary(self, repo: str | None = None) -> dict:
        """What Claude needs at the start of a session.

        Concept history is shared across repositories; commits and gaps are limited to ``repo``.
        """
        at = now()
        states = [
            self._state(r["concept_id"])
            for r in self.conn.execute("SELECT concept_id FROM concept_state ORDER BY level DESC")
        ]
        return {
            "skill_tracks": [
                {"concept": s["name"], "level": s["level"], "level_name": s["level_name"], "mode": s["mode"]}
                for s in states if s["track"] == "skill"
            ],
            "reviews_due": [s["name"] for s in states if self._review_due(s, at)],
            "commits_to_tag": [
                r["sha"] for r in self.conn.execute(
                    "SELECT sha FROM commits WHERE tagged = 0 AND (? IS NULL OR repo = ?) ORDER BY at", (repo, repo)
                )
            ],
            "withheld_to_review": [
                dict(r) for r in self.conn.execute(
                    "SELECT w.item_id, w.description, w.location, w.commit_sha FROM withheld w"
                    " JOIN baselines b ON b.id = w.baseline_id"
                    " WHERE w.status = 'committed' AND (? IS NULL OR b.repo = ?)", (repo, repo),
                )
            ],
            "non_learning_flags": mastery.non_learning_flags(self.conn, at),
        }

    def explain(self, concept: str) -> dict:
        """Everything recorded about one concept, so the learner can see why."""
        cid = self._concept_id(concept)
        return {
            **self._state(cid),
            "aliases": [r["alias"] for r in self.conn.execute("SELECT alias FROM aliases WHERE concept_id = ?", (cid,))],
            "evidence": [dict(r) for r in self.conn.execute(
                "SELECT signal_type, passed, detail, source, ref, at FROM evidence WHERE concept_id = ? ORDER BY id",
                (cid,))],
            "rule_changes": [dict(r) for r in self.conn.execute(
                "SELECT rule_id, from_level, to_level, at FROM rule_observations"
                " WHERE concept_id = ? AND passed = 1 ORDER BY id", (cid,))],
        }
