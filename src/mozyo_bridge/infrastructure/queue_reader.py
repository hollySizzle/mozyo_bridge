from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mozyo_bridge.shared.errors import die


def load_queue(queue_path: Path) -> dict[str, Any]:
    if not queue_path.exists():
        return {"tasks": []}
    data = yaml.safe_load(queue_path.read_text(encoding="utf-8")) or {"tasks": []}
    if not isinstance(data, dict):
        die(f"queue root must be a mapping: {queue_path}")
    tasks = data.setdefault("tasks", [])
    if not isinstance(tasks, list):
        die(f"queue tasks must be a list: {queue_path}")
    return data


def find_handoff_task(args: object, agent: str) -> dict[str, Any] | None:
    task_id = getattr(args, "task_id", None)
    if not task_id:
        return None
    data = load_queue(Path(getattr(args, "queue")).expanduser().resolve())
    candidates = []
    for task in data.get("tasks", []):
        if task.get("to") != agent:
            continue
        if task_id and task.get("id") != task_id:
            continue
        issue = getattr(args, "issue", None)
        if issue and str(task.get("issue_id") or "") != str(issue):
            continue
        task_type = getattr(args, "type", None)
        if task_type and task.get("type") != task_type:
            continue
        if task.get("status") not in {"pending", "claimed"}:
            continue
        candidates.append(task)
    if not candidates:
        die("no pending/claimed handoff task matched; do not notify pane without a queue task")
    return sorted(candidates, key=lambda task: str(task.get("created_at") or ""))[-1]
