from __future__ import annotations

from typing import Any

from mozyo_bridge.shared.errors import die


def validate_notify_gate(args: object) -> None:
    if not getattr(args, "issue", None):
        die("--issue is required for notify commands")
    if not getattr(args, "journal", None) and not getattr(args, "task_id", None):
        die("--journal is required for standard pane notification; --task-id is legacy fallback only")


def landing_marker(args: object, task: dict[str, Any] | None) -> str:
    if getattr(args, "journal", None):
        return (
            f"[mozyo:notify:issue={getattr(args, 'issue')}:"
            f"journal={getattr(args, 'journal')}:"
            f"type={getattr(args, 'type', None) or 'unspecified'}]"
        )
    return f"[mozyo:notify:task={task.get('id')}:issue={task.get('issue_id')}]"


def build_prompt(args: object, agent: str, task: dict[str, Any] | None) -> str:
    marker = landing_marker(args, task)
    prompt = getattr(args, "prompt", None)
    if prompt:
        return f"{marker} {prompt}"
    if getattr(args, "journal", None):
        return (
            f"{marker} "
            f"Redmine #{getattr(args, 'issue')} journal #{getattr(args, 'journal')} is ready for {agent}. "
            "Confirm that Redmine gate as the source of truth before acting. "
            "Stop-hook handoff waiting is disabled; use this pane notification as the trigger. "
            f"type={getattr(args, 'type', None) or 'unspecified'} commit={getattr(args, 'commit', None) or ''}."
        )
    if task:
        return (
            f"{marker} "
            f"handoff task {task.get('id')} is ready for {agent}. "
            "This is a legacy queue fallback; confirm the Redmine gate first and treat Redmine as the source of truth. "
            f"issue=#{task.get('issue_id')} commit={task.get('commit')} type={task.get('type')}."
        )
    die("--journal is required for standard pane notification; --task-id is legacy fallback only")
