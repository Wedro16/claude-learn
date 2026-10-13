"""Record one live Claude response per task and learner, for instant replay and offline mode.

    ANTHROPIC_API_KEY=... python -m webapp.record            # all tasks and profiles
    ANTHROPIC_API_KEY=... python -m webapp.record retry      # one task
"""

import json
import sys
import tempfile
from pathlib import Path

from learner.db import connect
from learner.store import LearnerStore
from webapp import claude, content, drafts


def record(task_id: str, profile: str) -> Path:
    task = content.TASKS[task_id]
    with tempfile.TemporaryDirectory() as tmp:
        store = LearnerStore(connect(Path(tmp) / "learner.db"))
        content.seed(store, profile)
        store.start_session("record")
        for name in task["concepts"]:
            store.record_concept(name, context_id="record")
        context = store.context(task["concepts"], session_id="record")
        out = claude.run_task(task, context)
    _, spans = drafts.split(out["draft"])
    missing = [g["gap_id"] for g in out["gaps"] if g["gap_id"] not in spans]
    if missing:
        print(f"  warning: gaps without markers: {missing}")
    path = claude.RECORDED / f"{task_id}-{profile}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    return path


def main() -> None:
    tasks = sys.argv[1:] or list(content.TASKS)
    for task_id in tasks:
        for profile in content.PROFILES:
            print(f"recording {task_id} / {profile} ...", flush=True)
            print(f"  wrote {record(task_id, profile)}")


if __name__ == "__main__":
    main()
