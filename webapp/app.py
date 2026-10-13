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
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from learner import report
from learner.attribution import _ranges
from learner.db import connect, now
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
# A visitor's sandbox (learners, runs, and any material they brought) is deleted this long after it was created.
RETENTION_SECONDS = 7 * 86400
MAX_DISCUSSION_MESSAGES = 8  # learner messages per run

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_runs (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS demo_revealed (
    run_id TEXT NOT NULL,
    gap_id TEXT NOT NULL,
    answer TEXT NOT NULL,
    PRIMARY KEY (run_id, gap_id)
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
    response.set_cookie(COOKIE, vid, max_age=RETENTION_SECONDS, httponly=True, samesite="lax")
    expire_sandboxes(vid)
    return vid


_last_sweep = 0.0
# Requests in flight per visitor. Expiry happens under _sandbox_guard and only deletes a sandbox
# no other request is using, so nothing is deleted mid-request or after another request recreated it.
_in_flight: dict[str, int] = defaultdict(int)
_sandbox_guard = threading.Lock()


@app.middleware("http")
async def hold_sandbox(request: Request, call_next):
    vid = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{32}", vid):
        return await call_next(request)  # a new visitor has no sandbox yet
    with _sandbox_guard:
        _in_flight[vid] += 1
    try:
        return await call_next(request)
    finally:
        with _sandbox_guard:
            _in_flight[vid] -= 1
            if not _in_flight[vid]:
                del _in_flight[vid]


def _expired(marker: Path, now_s: float) -> bool:
    try:
        return now_s - marker.stat().st_mtime > RETENTION_SECONDS
    except FileNotFoundError:
        return False


def expire_sandboxes(vid: str) -> None:
    """Enforce retention: a sandbox lives RETENTION_SECONDS from its creation, however often it's visited.

    The visitor's own sandbox is checked on every request; everyone else's at most hourly. A sandbox
    another request is using is left for a later check. Only quick checks and renames happen under
    _sandbox_guard (the request middleware waits on it); scanning and deleting happen outside it.
    """
    global _last_sweep
    now_s = time.time()
    trash = []
    created = DATA_DIR / vid / ".created"
    with _sandbox_guard:
        # Rechecked under the lock: a concurrent request may already have replaced the sandbox.
        if _expired(created, now_s) and _in_flight.get(vid, 0) <= 1:
            trash.append(_retire(created.parent))
        if not created.exists():
            created.parent.mkdir(parents=True, exist_ok=True)
            created.touch()
        sweep = now_s - _last_sweep > 3600
        if sweep:
            _last_sweep = now_s
    if sweep:
        # Anything left over from an interrupted delete is already out of use.
        trash += [p for p in (DATA_DIR / ".expired").glob("*") if p.is_dir()]
        candidates = [m for m in DATA_DIR.glob("*/.created") if m.parent.name != vid and _expired(m, now_s)]
        for marker in candidates:
            with _sandbox_guard:
                if not _in_flight.get(marker.parent.name) and _expired(marker, now_s):
                    trash.append(_retire(marker.parent))
    for folder in {f for f in trash if f}:
        shutil.rmtree(folder, ignore_errors=True)


def _retire(folder: Path) -> Path | None:
    """Move a sandbox out of use in one rename; the slow delete happens later, outside the lock."""
    target = DATA_DIR / ".expired" / uuid.uuid4().hex
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.rename(target)
    except FileNotFoundError:
        return None
    return target


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


def load_run(store: LearnerStore, run_id: str, open_only: bool = True) -> dict:
    row = store.conn.execute("SELECT payload FROM demo_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown run; start the task again.")
    run_state = json.loads(row["payload"])
    if open_only and run_state.get("finished"):
        raise HTTPException(409, "This run is finished. Start the task again to keep working.")
    return run_state


def save_run(store: LearnerStore, run_id: str, run: dict) -> None:
    with store.conn:
        store.conn.execute("INSERT OR REPLACE INTO demo_runs (id, payload) VALUES (?, ?)", (run_id, json.dumps(run)))


_run_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


@contextmanager
def editing_run(store: LearnerStore, run_id: str, open_only: bool = True):
    """Read, change and save a run as one step, so concurrent requests never drop each other's changes.

    Slow work (Claude calls) happens before this, on a separate read; only the change itself is locked.
    """
    with _seed_locks_guard:
        lock = _run_locks[run_id]
    with lock:
        run_state = load_run(store, run_id, open_only)
        yield run_state
        save_run(store, run_id, run_state)


def task_of(run_state: dict) -> dict:
    """The task a run was started for: a bundled one, or the visitor's own."""
    return run_state.get("task") or content.TASKS[run_state["task_id"]]


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


class OwnTask(BaseModel):
    kind: Literal["code", "writing"]
    request: str = Field(min_length=10, max_length=2000, description="What Claude should do")
    material: str = Field("", max_length=content.OWN_MATERIAL_LIMIT, description="Your code or notes")
    filename: str = Field("", max_length=200, description="Name of the file the material came from")


class RunRequest(BaseModel):
    task_id: str = Field("retry", examples=["retry"], description="A bundled task, or \"own\" with `own` set")
    profile: str = Field(examples=["developing"])
    own: OwnTask | None = Field(None, description="Your own task. Needs live Claude.")
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
        "own_material_limit": content.OWN_MATERIAL_LIMIT,
    }


@app.get("/api/model/{profile}")
def model(profile: str, request: Request, response: Response):
    """A learner's model: concepts, levels, recent level changes, criteria, flags."""
    return snapshot(learner(visitor(request, response), profile))


@app.get("/api/model/{profile}/export")
def export(profile: str, request: Request, response: Response):
    """Everything stored for a learner: a readable summary per concept, plus every table in their sandbox.

    The tables include answers, discussions and drafts (runs), baselines, evidence and rule checks.
    """
    store = learner(visitor(request, response), profile)
    ids = [r[0] for r in store.conn.execute("SELECT concept_id FROM concept_state ORDER BY concept_id")]
    tables = {}
    for (name,) in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        rows = [dict(r) for r in store.conn.execute(f'SELECT * FROM "{name}"')]
        if name == "demo_runs":
            rows = [{**r, "payload": json.loads(r["payload"])} for r in rows]
        tables[name] = rows
    body = {"profile": profile, "exported_at": now(), "concepts": [store.explain(cid) for cid in ids],
            "report": report.build(store.conn), "tables": tables}
    return JSONResponse(body, headers={
        "Content-Disposition": f'attachment; filename="claude-learn-{profile}.json"', **response.headers})


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
    """Claude does the task for this learner, choosing interventions from their model.

    Use a bundled `task_id`, or set `own` to bring your own task (needs live Claude).
    """
    store = learner(visitor(request, response), body.profile)
    run_id = uuid.uuid4().hex[:12]
    if body.own:
        task = scope_own_task(store, body.own, request, body.live)
    else:
        task = content.TASKS.get(body.task_id)
        if task is None:
            raise HTTPException(404, f"Unknown task {body.task_id!r}")
    store.start_session(run_id)
    for name in task["concepts"]:
        store.record_concept(name, context_id=run_id)
    context = store.context(task["concepts"], session_id=run_id)
    allowed = {k for c in context["concepts"] for k in c["plan"]["interventions"]}

    try:
        out, source, note = live(request, body.live, claude.run_task, task, context), "live", ""
    except claude.Unavailable as exc:
        if task.get("own"):
            raise HTTPException(503, f"Claude couldn't do this task right now ({exc}). Try again in a minute.")
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
        "task_id": task["id"], "task": task, "profile": body.profile, "source": source, "note": note,
        "plan": [{"name": c["name"], "level": c["level"], "level_name": c["level_name"], "track": c["track"],
                  "interventions": c["plan"]["interventions"]} for c in context["concepts"]],
        "orientation": out["orientation"], "decision_notes": out["decision_notes"],
        "prediction_prompt": out["prediction_prompt"], "prediction_kind": prediction_kind,
        "critique_invitation": out["critique_invitation"], "takeaway": out["takeaway"],
        "scaffold": scaffold, "gaps": gaps,
    }
    save_run(store, run_id, {**run_state, "baseline": baseline, "spans": spans})
    return {"run_id": run_id, **run_state, "model": snapshot(store)}


def scope_own_task(store: LearnerStore, own: OwnTask, request: Request, want_live: bool) -> dict:
    """Ask Claude which concepts the visitor's task involves, and resolve them against their model."""
    known = [r[0] for r in store.conn.execute("SELECT name FROM concepts ORDER BY name LIMIT 60")]
    try:
        scoped = live(request, want_live, claude.scope_task, own.kind, own.request, own.material, known)
    except claude.Unavailable as exc:
        raise HTTPException(503, f"Your own tasks need live Claude ({exc}). Turn on Live Claude, or try a bundled task.")
    names = []
    if not scoped["personal"]:
        for c in scoped["concepts"][:3]:
            rec = store.record_concept(c["name"], c["description"], domain=own.kind)
            if "concept_id" in rec:
                # Matching may map Claude's wording onto a concept the learner already has.
                name = store.conn.execute("SELECT name FROM concepts WHERE id = ?", (rec["concept_id"],)).fetchone()[0]
                if name not in names:
                    names.append(name)
    if not names:
        raise HTTPException(422, "This looks like a personal topic (HR, health or legal). claude-learn doesn't "
                                 "track those as learning; ask Claude directly instead.")
    return content.own_task(own.kind, own.request, own.material, own.filename, scoped, names)


@app.post("/api/runs/{run_id}/predict")
def predict(run_id: str, body: AnswerRequest, request: Request, response: Response):
    """Grade the learner's answer to the prediction question; it becomes evidence."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = task_of(run_state)
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
    task = task_of(run_state)
    store.record_intervention(task["concepts"][0], run_state["prediction_kind"],
                              run_state["prediction_prompt"] or "-", "skipped", session_id=run_id)
    return {"model": snapshot(store)}


@app.post("/api/runs/{run_id}/submit")
def submit(run_id: str, body: SubmitRequest, request: Request, response: Response):
    """Review the learner's version against the baseline; verdicts become evidence."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    reviewed, source, events = review_gaps(store, run_id, run_state, body.text, request, body.live)
    owned, more = record_ownership(store, run_id, run_state, body.text, reviewed)
    return {**reviewed, "source": source, "ownership": owned, "events": events + more, "model": snapshot(store)}


def review_gaps(store: LearnerStore, run_id: str, run_state: dict, text: str, request: Request,
                want_live: bool) -> tuple[dict, str, list[dict]]:
    """Review the learner's version gap by gap; verdicts become evidence."""
    task = task_of(run_state)
    gaps = run_state["gaps"]
    try:
        reviewed = live(request, want_live, claude.review, task, run_state["baseline"], run_state["scaffold"], text, gaps)
        source = "live"
    except claude.Unavailable:
        reviewed, source = claude.heuristic_review(run_state["spans"], text, gaps), "offline"

    by_id = {g["gap_id"]: g for g in gaps}
    events = []
    for verdict in reviewed["gaps"]:
        gap = by_id.get(verdict["gap_id"])
        if gap is None:
            continue
        v = verdict["verdict"]
        if v in ("as_expected", "different"):
            result = store.update_withheld(gap["item_id"], v, ref=run_id)
        elif v == "incorrect":
            result = store.record_evidence(gap["concept"], "gap_completion", False, detail="incorrect", ref=run_id)
        else:
            continue
        if e := event(result, f"gap {gap['gap_id']}: {v.replace('_', ' ')}"):
            events.append(e)
    return reviewed, source, events


def reference_text(run_id: str, store: LearnerStore, run_state: dict) -> str:
    """Everything Claude wrote for this run: the scaffold, plus any answers it revealed."""
    revealed = [r[0] for r in store.conn.execute("SELECT answer FROM demo_revealed WHERE run_id = ?", (run_id,))]
    return "\n".join([run_state["scaffold"], *revealed])


def record_ownership(store: LearnerStore, run_id: str, run_state: dict, text: str,
                     reviewed: dict | None = None) -> tuple[dict, list[dict]]:
    """Who wrote what in this version. Recorded once per distinct version, so resubmitting adds nothing."""
    task = task_of(run_state)
    # Answers Claude revealed are Claude's lines too, wherever the learner pastes them.
    owned = drafts.ownership(reference_text(run_id, store, run_state), text)
    primary = task["concepts"][0]
    events = []
    digest = hashlib.sha256(text.encode()).hexdigest()
    with editing_run(store, run_id, open_only=False) as current:
        fresh = current.get("ownership_recorded_for") != digest
        current["ownership_recorded_for"] = digest
    if fresh:
        store.record_evidence(primary, "draft_ownership", None, detail=str(owned["learner_share"]), ref=run_id)
    if reviewed and reviewed.get("caught_issue"):
        if e := event(store.record_evidence(primary, "substantive_fix", True, ref=run_id), "caught an issue in Claude's draft"):
            events.append(e)
    return owned, events


@app.post("/api/runs/{run_id}/discuss")
def discuss(run_id: str, body: DiscussRequest, request: Request, response: Response):
    """Talk an edit through with Claude: explain your reasoning, ask why, or push back.

    Claude replies, and classifies the message: a correct explanation is
    self-explanation evidence, a question is scored on the SOLO scale, and valid
    pushback is critique evidence.
    """
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = task_of(run_state)
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

    with editing_run(store, run_id) as current:
        current.setdefault("discussion", []).extend(
            [{"role": "user", "content": body.message}, {"role": "assistant", "content": out["reply"]}])
    events = [e for e in [event(result, label)] if e] if label else []
    return {**out, "concept": concept, "source": "live", "events": events, "model": snapshot(store)}


class FinishCheckRequest(BaseModel):
    profile: str
    text: str = Field(max_length=40000, description="The learner's final version")
    live: bool = True


class CompleteRequest(BaseModel):
    profile: str
    text: str | None = Field(None, max_length=40000, description=(
        "The version to finish. Optional after a final check, which fixes the version; if given, it must match."))
    live: bool = True


@app.post("/api/runs/{run_id}/finish/check")
def finish_check(run_id: str, body: FinishCheckRequest, request: Request, response: Response):
    """Finish step 1: a final, non-blocking review of the version the learner will use."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = task_of(run_state)
    try:
        out, source = live(request, body.live, claude.final_check, task, run_state["baseline"], body.text), "live"
    except claude.Unavailable:
        out, source = claude.heuristic_final_check(task, body.text), "offline"
    # The checked version is the one that gets explained and finished.
    with editing_run(store, run_id) as current:
        current["final"] = {"check": out, "text": body.text}
    return {**out, "source": source}


@app.post("/api/runs/{run_id}/finish/explain")
def finish_explain(run_id: str, body: AnswerRequest, request: Request, response: Response):
    """Finish step 2: explain it back. Graded retrieval practice; optional."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = task_of(run_state)
    final = run_state.get("final", {})
    question = final.get("check", {}).get("teach_back_question") or task["teach_back"]
    # Graded against the learner's own final version: a valid change of approach is not a wrong answer.
    solution = final.get("text") or run_state["baseline"]
    try:
        graded, source = live(request, body.live, claude.grade, task, question, body.answer, solution), "live"
    except claude.Unavailable:
        graded, source = claude.heuristic_grade(), "offline"
    result = store.record_intervention(task["concepts"][0], "retrieval", question, graded["outcome"],
                                       response=body.answer, session_id=run_id)
    with editing_run(store, run_id) as current:
        current.setdefault("final", {}).update(explained=True, teach_back=graded["outcome"])
    events = [e for e in [event(result, f"explained it back: {graded['outcome']}")] if e]
    return {**graded, "question": question, "source": source, "events": events, "model": snapshot(store)}


@app.post("/api/runs/{run_id}/finish/complete")
def finish_complete(run_id: str, body: CompleteRequest, request: Request, response: Response):
    """Finish steps 3 and 4: close the run; summarize it; hand over the file and its commit."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    task = task_of(run_state)
    final = run_state.get("final", {})
    text = final.get("text")
    if text is None:
        if body.text is None:
            raise HTTPException(400, "Send the text to finish, or run the final check first.")
        text = body.text
    elif body.text is not None and body.text != text:
        raise HTTPException(409, "The draft changed after the final check. Run the check again on this version.")

    # Gaps filled since the last submit still earn credit: review them before the run closes.
    open_gaps = [g for g in run_state["gaps"] if store.get_baseline(g["item_id"])["status"] not in REVIEWED]
    if any(g["gap_id"] not in drafts.unfilled(text) for g in open_gaps):
        reviewed, _, _ = review_gaps(store, run_id, run_state, text, request, body.live)
    else:
        reviewed = None

    with editing_run(store, run_id) as current:
        final = current.setdefault("final", {})
        current["finished"] = True
        final["text"] = text
    if not final.get("explained"):
        question = final.get("check", {}).get("teach_back_question") or task["teach_back"]
        store.record_intervention(task["concepts"][0], "retrieval", question, "skipped", session_id=run_id)

    # What you built: who wrote what in the final version.
    owned, _ = record_ownership(store, run_id, run_state, text, reviewed)
    by_class: dict[str, list[int]] = {}
    for number, line in enumerate(owned["lines"], start=1):
        if line["cls"] != "blank":
            by_class.setdefault(line["cls"], []).append(number)
    note = {"session": run_id, "files": {task["filename"]: {k: _ranges(v) for k, v in by_class.items()}}}
    trailers = "\n\nAssisted-by: Claude\nClaude-Session: " + run_id if by_class.get("claude") or by_class.get("claude_modified") else ""

    # What changed in your model because of this run (only its own evidence), and what comes next.
    session = store.session_summary(run_id)
    return {
        "check": final.get("check"),
        "ownership": owned,
        "changes": session["changes"],
        "rules": session["rules"],
        "observations": session["observations"],
        "teach_back": final.get("teach_back"),
        "upcoming": session["upcoming"],
        "file": {"name": task["filename"], "content": text},
        "commit": {"message": task["commit_message"] + trailers, "note": note},
        "model": snapshot(store),
    }


@app.post("/api/runs/{run_id}/reveal")
def reveal(run_id: str, body: RevealRequest, request: Request, response: Response):
    """Show Claude's version of a gap. Fine to do; recorded as handed back."""
    store = learner(visitor(request, response), body.profile)
    run_state = load_run(store, run_id)
    gap = next((g for g in run_state["gaps"] if g["gap_id"] == body.gap_id), None)
    if gap is None:
        raise HTTPException(404, "Unknown gap")
    result = store.update_withheld(gap["item_id"], "handed_back", ref=run_id)
    # One row per revealed answer, so two reveals at once can't overwrite each other.
    with store.conn:
        store.conn.execute("INSERT OR REPLACE INTO demo_revealed (run_id, gap_id, answer) VALUES (?, ?, ?)",
                           (run_id, gap["gap_id"], run_state["spans"][gap["gap_id"]]))
    events = [e for e in [event(result, f"gap {gap['gap_id']}: handed back")] if e]
    return {"gap_id": gap["gap_id"], "answer": run_state["spans"][gap["gap_id"]], "events": events,
            "model": snapshot(store)}


@app.get("/health", include_in_schema=False)
def health(request: Request):
    # The entry count (never the addresses) confirms how many proxies append to X-Forwarded-For.
    forwarded = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return {"ok": True, "forwarded_entries": len(forwarded), "trusted_proxy_hops": TRUSTED_PROXY_HOPS}

