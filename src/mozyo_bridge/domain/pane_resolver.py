from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from mozyo_bridge.infrastructure.tmux_client import pane_lines, run_tmux, validate_target
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import READ_MARK_PREFIX


AGENT_PROCESSES = {"claude", "codex", "node"}
AGENT_COMMANDS = {
    "claude": "claude",
    "codex": "codex",
}
VERSIONED_NATIVE_BINARY_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+].*)?")
READ_MARK_TTL_SECONDS = 300


def read_mark_path(pane_id: str) -> Path:
    return Path(f"{READ_MARK_PREFIX}{pane_id.replace('%', '_')}")


def mark_read(pane_id: str) -> None:
    payload = {
        "pane_id": pane_id,
        "sender_pane": os.environ.get("TMUX_PANE", ""),
        "created_at": time.time(),
    }
    read_mark_path(pane_id).write_text(json.dumps(payload), encoding="utf-8")


def require_read(pane_id: str) -> None:
    path = read_mark_path(pane_id)
    if not path.exists():
        die(f"must read target before interacting: mozyo-bridge read {pane_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        clear_read(pane_id)
        die(f"stale read marker for {pane_id}; read target again before interacting")
    if payload.get("pane_id") != pane_id:
        clear_read(pane_id)
        die(f"read marker target mismatch for {pane_id}; read target again before interacting")
    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)) or time.time() - created_at > READ_MARK_TTL_SECONDS:
        clear_read(pane_id)
        die(f"read marker expired for {pane_id}; read target again before interacting")


def clear_read(pane_id: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        read_mark_path(pane_id).unlink()


def is_tmux_target(target: str) -> bool:
    return target.startswith("%") or ":" in target or "." in target


def current_pane() -> str:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        die("TMUX_PANE is not set; run from inside tmux for this command")
    return pane


def current_session_name() -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = run_tmux("display-message", "-t", pane, "-p", "#{session_name}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def find_labeled_panes(label: str, session: str | None = None, fallback: bool = True) -> list[dict[str, str]]:
    matches = [pane for pane in pane_lines() if pane["label"] == label]
    if session:
        session_matches = [pane for pane in matches if pane["location"].split(":", 1)[0] == session]
        if session_matches or not fallback:
            return session_matches
    return matches


def find_labeled_pane(label: str, session: str | None = None, fallback: bool = True) -> dict[str, str] | None:
    matches = find_labeled_panes(label, session=session, fallback=fallback)
    if len(matches) > 1:
        ids = ", ".join(pane["id"] for pane in matches)
        die(f"multiple panes found with label '{label}': {ids}")
    return matches[0] if matches else None


def resolve_target(target: str) -> str:
    if is_tmux_target(target):
        validate_target(target)
        return target
    pane = find_labeled_pane(target, session=current_session_name())
    if pane:
        return pane["id"]
    die(f"no pane label found: {target}")
    raise AssertionError("unreachable")


def pane_info(target: str) -> dict[str, str]:
    pane_id = resolve_target(target)
    for pane in pane_lines():
        if pane["id"] == pane_id:
            return pane
    die(f"pane disappeared after resolve: {target}")
    raise AssertionError("unreachable")


def is_agent_process(command: str) -> bool:
    name = Path(command or "").name
    return name in AGENT_PROCESSES or VERSIONED_NATIVE_BINARY_RE.fullmatch(name) is not None


def ensure_agent_target(pane: dict[str, str], expected_label: str, force: bool = False) -> None:
    if force:
        return
    label = pane.get("label") or ""
    command = Path(pane.get("command") or "").name
    if label == expected_label and is_agent_process(command):
        return
    die(
        "target pane does not look like an agent pane; "
        f"label={label or '-'} process={command or '-'} expected_label={expected_label}. "
        "Use --force only for an explicit operator-approved send."
    )
