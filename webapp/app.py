"""claude-learn demo: Claude does the work and teaches, adapted to the learner.

Each browser gets its own sandbox: three seeded learners (one per profile) in
SQLite files that live only as long as the server instance. The same engine as
the Claude Code pilot (learner/) runs underneath.

Run locally:  uvicorn webapp.app:app --reload
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from learner import report
from learner.db import connect
from learner.store import REVIEWED, LearnerStore
from webapp import claude, content, drafts

DATA_DIR = Path(os.environ.get("DEMO_DATA_DIR", "/tmp/claude-learn-demo"))
STATIC = Path(__file__).parent / "static"
# Content hash of the static assets: each deploy gets new asset URLs, so browsers never run stale code.
STATIC_VERSION = hashlib.sha256(
    b"".join((STATIC / name).read_bytes() for name in ("app.js", "style.css"))
).hexdigest()[:12]
PER_VISITOR_PER_HOUR = int(os.environ.get("DEMO_CALLS_PER_HOUR", "30"))
GLOBAL_PER_DAY = int(os.environ.get("DEMO_CALLS_PER_DAY", "800"))
# Proxies that append to X-Forwarded-For in front of the app (Cloud Run's front end appends 1).
TRUSTED_PROXY_HOPS = int(os.environ.get("DEMO_TRUSTED_PROXY_HOPS", "0"))
COOKIE = "claude_learn_visitor"
MAX_DISCUSSION_MESSAGES = 8  # learner messages per run

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_runs (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS demo_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

app = FastAPI(
    title="claude-learn",
    description=(
        "Claude does the work and teaches what it did, adapted to each learner. "
        "Every call is scoped to a sandboxed visitor (cookie); learner histories are synthetic demo data."
    ),
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# -- rate limiting -----------------------------------------------------------


class Limiter:
    """Caps live Claude calls per visitor and per day; over the cap, the demo goes offline-mode."""

    def __init__(self):
        self.lock = threading.Lock()
        self.visitor_calls: dict[str, deque] = defaultdict(deque)
        self.day_calls: deque = deque()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            calls = self.visitor_calls[key]
            while calls and now - calls[0] > 3600:
                calls.popleft()
            while self.day_calls and now - self.day_calls[0] > 86400:
                self.day_calls.popleft()
            if len(calls) >= PER_VISITOR_PER_HOUR or len(self.day_calls) >= GLOBAL_PER_DAY:
                return False
            calls.append(now)
            self.day_calls.append(now)
            return True


limiter = Limiter()


def client_address(request: Request) -> str:
    """The caller's address as recorded by trusted proxies.

    Callers can put anything at the start of X-Forwarded-For; each trusted proxy
    appends the address it saw, so count back from the end.
    """
    if TRUSTED_PROXY_HOPS:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if len(hops) >= TRUSTED_PROXY_HOPS:
            return hops[-TRUSTED_PROXY_HOPS]
    return request.client.host if request.client else "?"


def live(request: Request, want_live: bool, fn, *args):
    """Call Claude if allowed; raise Unavailable so callers use the offline path otherwise."""
    if not want_live:
        raise claude.Unavailable("recorded mode selected")
    if not limiter.allow(client_address(request)):
        raise claude.Unavailable("live-call limit reached for now")
    return fn(*args)


# -- visitors and learners ---------------------------------------------------


def visitor(request: Request, response: Response) -> str:
    vid = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{32}", vid):
        vid = uuid.uuid4().hex
    response.set_cookie(COOKIE, vid, max_age=7 * 86400, httponly=True, samesite="lax")
    return vid


_seed_locks: dict[Path, threading.Lock] = defaultdict(threading.Lock)
_seed_locks_guard = threading.Lock()


def learner_path(vid: str, profile: str) -> Path:
    """The sandbox file for a visitor's learner. Only known profiles map to a path."""
    if profile not in content.PROFILES:
        raise HTTPException(404, f"Unknown profile {profile!r}")
    return DATA_DIR / vid / f"{profile}.db"


def learner(vid: str, profile: str) -> LearnerStore:
    path = learner_path(vid, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _seed_locks_guard:
        lock = _seed_locks[path]
    # Seeding happens once, completely, before anyone reads the model.
    with lock:
        store = LearnerStore(connect(path))
        store.conn.executescript(RUNS_SCHEMA)
        if not store.conn.execute("SELECT 1 FROM demo_meta WHERE key = 'seeded'").fetchone():
            content.seed(store, profile)
            with store.conn:
                store.conn.execute("INSERT OR REPLACE INTO demo_meta (key, value) VALUES ('seeded', '1')")
    return store


def load_run(store: LearnerStore, run_id: str) -> dict:
    row = store.conn.execute("SELECT payload FROM demo_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown run; start the task again.")
    return json.loads(row["payload"])


def save_run(store: LearnerStore, run_id: str, run: dict) -> None:
    with store.conn:
        store.conn.execute("INSERT OR REPLACE INTO demo_runs (id, payload) VALUES (?, ?)", (run_id, json.dumps(run)))


def snapshot(store: LearnerStore) -> dict:
    """The learner model as the UI shows it."""
    concepts = []
    for (cid,) in store.conn.execute("SELECT concept_id FROM concept_state ORDER BY level DESC, concept_id"):
        e = store.explain(cid)
        concepts.append({
            "name": e["name"], "level": e["level"], "level_name": e["level_name"], "track": e["track"],
            "evidence": len(e["evidence"]),
            "changes": [c for c in e["rule_changes"] if c["from_level"] != c["to_level"]][-4:],
        })
    r = report.build(store.conn)
    return {"concepts": concepts, "criteria": r["criteria"], "flags": r["non_learning_flags"]}


def event(result: dict, label: str) -> dict | None:
    if not result or "from_level" not in result:
        return None
    return {
        "concept": result["concept_id"], "label": label, "rules": result["rules_fired"],
        "from": result["from_level"], "to": result["to_level"],
    }


# -- API ---------------------------------------------------------------------


class RunRequest(BaseModel):
    task_id: str = Field(examples=["retry"])
    profile: str = Field(examples=["developing"])
    live: bool = True


class AnswerRequest(BaseModel):
    profile: str
    answer: str = Field(max_length=4000)
    live: bool = True


class SubmitRequest(BaseModel):
    profile: str
    text: str = Field(max_length=40000)
    live: bool = True


class DiscussRequest(BaseModel):
    profile: str
    message: str = Field(min_length=1, max_length=2000)
    text: str = Field(max_length=40000, description="The learner's current version of the draft")
    live: bool = True


class RevealRequest(BaseModel):
    profile: str
    gap_id: str


class ProfileRequest(BaseModel):
    profile: str


@app.get("/", include_in_schema=False)
def index():
    html = (STATIC / "index.html").read_text()
    for asset in ("app.js", "style.css"):
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={STATIC_VERSION}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/catalog")
def catalog():
    """Task cards and learner profiles bundled with the demo."""
    return {
        "tasks": list(content.TASKS.values()),
        "profiles": list(content.PROFILES.values()),
        "live_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


@app.get("/api/model/{profile}")
def model(profile: str, request: Request, response: Response):
    """A learner's model: concepts, levels, recent level changes, criteria, flags."""
    return snapshot(learner(visitor(request, response), profile))


@app.post("/api/reset")
def reset(body: ProfileRequest, request: Request, response: Response):
    """Restore a learner to their seeded history."""
    vid = visitor(request, response)
    path = learner_path(vid, body.profile)
    with _seed_locks_guard:
        lock = _seed_locks[path]
    with lock:
        path.unlink(missing_ok=True)
    return snapshot(learner(vid, body.profile))


@app.post("/api/run")
def run(body: RunRequest, request: Request, response: Response):
    """Claude does the task for this learner, choosing interventions from their model."""
    task = content.TASKS.get(body.task_id)
    if task is None:
        raise HTTPException(404, f"Unknown task {body.task_id!r}")
    store = learner(visitor(request, response), body.profile)
    run_id = uuid.uuid4().hex[:12]
    store.start_session(run_id)
    for name in task["concepts"]:
        store.record_concept(name, context_id=run_id)
    context = store.context(task["concepts"], session_id=run_id)
    allowed = {k for c in context["concepts"] for k in c["plan"]["interventions"]}

    try:
        out, source, note = live(request, body.live, claude.run_task, task, context), "live", ""
    except claude.Unavailable as exc:
        out, matched = claude.recorded_run(task["id"], body.profile, allowed)
        source = "recorded"
        note = str(exc) if matched else (
            f"{content.PROFILES[body.profile]['name']}'s model has changed since this replay was recorded, "
            "so it may not match the plan above. Turn on Live Claude for a response adapted to the current model."
        )

    # The plan, not the model, decides which interventions appear.
    if "orientation" not in allowed:
        out["orientation"] = ""
    if not allowed & {"prediction", "what_if"}:
        out["prediction_prompt"] = ""
    if "critique" not in allowed:
        out["critique_invitation"] = ""
    baseline, spans = drafts.split(out["draft"])
    gaps = [g for g in out["gaps"] if g["gap_id"] in spans] if "gap" in allowed else []
    for g in gaps:
        if g["concept"] not in task["concepts"]:
            g["concept"] = task["concepts"][0]
    draft = out["draft"] if gaps else baseline
    scaffold = drafts.scaffold(draft, gaps, task["kind"])

    if gaps:
        rec = store.record_baseline(
            task["id"], baseline,
            [{"item_id": f"{run_id}-{g['gap_id']}", "description": g["description"], "hint": g["hint"],
              "concept": g["concept"], "location": task["id"]} for g in gaps],
            session_id=run_id,
        )
        for g, item_id in zip(gaps, rec["withheld_items"]):
            g["item_id"] = item_id
    # Log what was shown; questions are logged when answered or skipped.
    primary = task["concepts"][0]
    for kind in context["concepts"][0]["plan"]["interventions"]:
        if kind in ("prediction", "what_if", "retrieval") or (kind == "gap" and not gaps):
            continue
        store.record_intervention(primary, kind, kind, "pending", session_id=run_id)

    prediction_kind = "prediction" if "prediction" in allowed else "what_if"
    run_state = {
        "task_id": task["id"], "profile": body.profile, "source": source, "note": note,
        "plan": [{"name": c["name"], "level": c["level"], "level_name": c["level_name"], "track": c["track"],
                  "interventions": c["plan"]["interventions"]} for c in context["concepts"]],
        "orientation": out["orientation"], "decision_notes": out["decision_notes"],
        "prediction_prompt": out["prediction_prompt"], "prediction_kind": prediction_kind,
        "critique_invitation": out["critique_invitation"], "takeaway": out["takeaway"],
        "scaffold": scaffold, "gaps": gaps,
    }
    save_run(store, run_id, {**run_state, "baseline": baseline, "spans": spans})
    return {"run_id": run_id, **run_state, "model": snapshot(store)}


@app.post("/api/runs/{run_id}/predict")
def predict(run_id: str, body: AnswerRequest, request: Request, response: Response):
    """Grade the learner's answer to the prediction question; it becomes evidence."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = content.TASKS[run_state["task_id"]]
    question = run_state["prediction_prompt"]
    if not question:
        raise HTTPException(400, "This run has no prediction question.")
    try:
        graded, source = live(request, body.live, claude.grade, task, question, body.answer, run_state["baseline"]), "live"
    except claude.Unavailable:
        graded, source = claude.heuristic_grade(), "offline"
    result = store.record_intervention(
        task["concepts"][0], run_state["prediction_kind"], question, graded["outcome"],
        response=body.answer, session_id=run_id,
    )
    events = [e for e in [event(result, f"{run_state['prediction_kind']} answered {graded['outcome']}")] if e]
    return {**graded, "source": source, "events": events, "model": snapshot(store)}


@app.post("/api/runs/{run_id}/skip")
def skip(run_id: str, body: ProfileRequest, request: Request, response: Response):
    """Skip the prediction question. Allowed and logged; never counted against the learner."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = content.TASKS[run_state["task_id"]]
    store.record_intervention(task["concepts"][0], run_state["prediction_kind"],
                              run_state["prediction_prompt"] or "-", "skipped", session_id=run_id)
    return {"model": snapshot(store)}


@app.post("/api/runs/{run_id}/submit")
def submit(run_id: str, body: SubmitRequest, request: Request, response: Response):
    """Review the learner's version against the baseline; verdicts become evidence."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = content.TASKS[run_state["task_id"]]
    gaps = run_state["gaps"]
    try:
        reviewed = live(request, body.live, claude.review, task, run_state["baseline"],
                        run_state["scaffold"], body.text, gaps)
        source = "live"
    except claude.Unavailable:
        reviewed, source = claude.heuristic_review(run_state["spans"], body.text, gaps), "offline"

    by_id = {g["gap_id"]: g for g in gaps}
    events = []
    for verdict in reviewed["gaps"]:
        gap = by_id.get(verdict["gap_id"])
        if gap is None:
            continue
        v = verdict["verdict"]
        if v in ("as_expected", "different"):
            result = store.update_withheld(gap["item_id"], v)
        elif v == "incorrect":
            result = store.record_evidence(gap["concept"], "gap_completion", False, detail="incorrect", ref=run_id)
        else:
            continue
        if e := event(result, f"gap {gap['gap_id']}: {v.replace('_', ' ')}"):
            events.append(e)

    # Answers Claude revealed are Claude's lines too, wherever the learner pastes them.
    reference = "\n".join([run_state["scaffold"], *run_state.get("revealed", {}).values()])
    owned = drafts.ownership(reference, body.text)
    primary = task["concepts"][0]
    store.record_evidence(primary, "draft_ownership", None, detail=str(owned["learner_share"]), ref=run_id)
    if reviewed.get("caught_issue"):
        if e := event(store.record_evidence(primary, "substantive_fix", True, ref=run_id), "caught an issue in Claude's draft"):
            events.append(e)
    return {**reviewed, "source": source, "ownership": owned, "events": events, "model": snapshot(store)}


@app.post("/api/runs/{run_id}/discuss")
def discuss(run_id: str, body: DiscussRequest, request: Request, response: Response):
    """Talk an edit through with Claude: explain your reasoning, ask why, or push back.

    Claude replies, and classifies the message: a correct explanation is
    self-explanation evidence, a question is scored on the SOLO scale, and valid
    pushback is critique evidence.
    """
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = content.TASKS[run_state["task_id"]]
    history = run_state.setdefault("discussion", [])
    if sum(t["role"] == "user" for t in history) >= MAX_DISCUSSION_MESSAGES:
        raise HTTPException(400, "This conversation is long enough for one run. Start the task again to keep going.")
    open_gaps = [
        {"gap_id": g["gap_id"], "description": g["description"]}
        for g in run_state["gaps"] if store.get_baseline(g["item_id"])["status"] not in REVIEWED
    ]
    try:
        out = live(request, body.live, claude.discuss, task, run_state["baseline"], body.text,
                   open_gaps, history, body.message)
    except claude.Unavailable:
        return {"reply": "Claude can't respond offline. Turn on Live Claude to talk this through.",
                "source": "offline", "events": [], "model": snapshot(store)}

    concept = out["concept"] if out["concept"] in task["concepts"] else task["concepts"][0]
    previous = next((t["content"] for t in reversed(history) if t["role"] == "assistant"),
                    "Explain the reasoning behind your edits.")
    result, label = None, None
    if out["kind"] == "explanation" and out["understanding"] != "not_applicable":
        result = store.record_intervention(concept, "self_explanation", previous, out["understanding"],
                                           response=body.message, session_id=run_id)
        label = f"explanation: {out['understanding']}"
    elif out["kind"] == "question" and out["solo"]:
        result = store.record_evidence(concept, "question", detail=str(out["solo"]), ref=run_id)
        label = f"question, SOLO level {out['solo']}"
    elif out["kind"] == "pushback":
        result = store.record_evidence(concept, "critique", out["pushback_valid"], ref=run_id)
        label = "pushback: Claude agreed" if out["pushback_valid"] else "pushback: Claude explained the trade-off"

    history += [{"role": "user", "content": body.message}, {"role": "assistant", "content": out["reply"]}]
    save_run(store, run_id, run_state)
    events = [e for e in [event(result, label)] if e] if label else []
    return {**out, "concept": concept, "source": "live", "events": events, "model": snapshot(store)}


@app.post("/api/runs/{run_id}/reveal")
def reveal(run_id: str, body: RevealRequest, request: Request, response: Response):
    """Show Claude's version of a gap. Fine to do; recorded as handed back."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    gap = next((g for g in run_state["gaps"] if g["gap_id"] == body.gap_id), None)
    if gap is None:
        raise HTTPException(404, "Unknown gap")
    result = store.update_withheld(gap["item_id"], "handed_back")
    run_state.setdefault("revealed", {})[gap["gap_id"]] = run_state["spans"][gap["gap_id"]]
    save_run(store, run_id, run_state)
    events = [e for e in [event(result, f"gap {gap['gap_id']}: handed back")] if e]
    return {"gap_id": gap["gap_id"], "answer": run_state["spans"][gap["gap_id"]], "events": events,
            "model": snapshot(store)}


@app.get("/health", include_in_schema=False)
def health(request: Request):
    # The entry count (never the addresses) confirms how many proxies append to X-Forwarded-For.
    forwarded = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return {"ok": True, "forwarded_entries": len(forwarded), "trusted_proxy_hops": TRUSTED_PROXY_HOPS}

