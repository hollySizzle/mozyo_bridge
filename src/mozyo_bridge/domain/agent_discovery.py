"""Cross-workspace agent discovery surface (Redmine #10332).

Read-only discovery of every tmux pane structured by ``session`` /
``window_index`` / ``window_name`` / ``pane_id`` / ``process`` / ``cwd`` /
``repo_root`` / ``agent_kind``. Used by ``mozyo-bridge agents list`` and as
the building block for cross-workspace handoff targeting.

The legacy ``find_agent_window`` resolver in ``pane_resolver`` only addresses
**same-session** routing; it intentionally fails closed on cross-session
fallback. This module adds the structured enumeration the sender needs in
order to *name* a cross-workspace target explicitly, which is the only path
the cross-workspace handoff gate accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from mozyo_bridge.infrastructure.tmux_client import pane_lines
from mozyo_bridge.shared.paths import PROJECT_MARKERS


AGENT_KIND_CLAUDE = "claude"
AGENT_KIND_CODEX = "codex"
AGENT_KIND_UNKNOWN = "unknown"
AGENT_KINDS = frozenset({AGENT_KIND_CLAUDE, AGENT_KIND_CODEX, AGENT_KIND_UNKNOWN})


@dataclass(frozen=True)
class AgentRecord:
    pane_id: str
    session: str
    window_index: str
    window_name: str
    pane_index: str
    pane_active: bool
    process: str
    cwd: str
    repo_root: str | None
    agent_kind: str
    ambiguous: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "session": self.session,
            "window_index": self.window_index,
            "window_name": self.window_name,
            "pane_index": self.pane_index,
            "pane_active": self.pane_active,
            "process": self.process,
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "agent_kind": self.agent_kind,
            "ambiguous": self.ambiguous,
        }


def infer_repo_root(cwd: str) -> str | None:
    """Walk up from ``cwd`` until a PROJECT_MARKERS-bearing directory is found.

    Returns the absolute path as a string, or ``None`` when no marker is
    reachable (filesystem root, unreadable path, etc.). The resolver is
    permissive on errors because discovery is read-only — a missing
    repo_root in the output is informational, not fatal.
    """
    if not cwd:
        return None
    try:
        current = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if any((path / marker).exists() for marker in PROJECT_MARKERS):
            return str(path)
    return None


def classify_agent_kind(window_name: str) -> str:
    if window_name == AGENT_KIND_CLAUDE:
        return AGENT_KIND_CLAUDE
    if window_name == AGENT_KIND_CODEX:
        return AGENT_KIND_CODEX
    return AGENT_KIND_UNKNOWN


def _parse_location(location: str) -> tuple[str, str, str]:
    session, _, rest = location.partition(":")
    window_index, _, pane_index = rest.partition(".")
    return session, window_index, pane_index


def discover_agents(panes: Iterable[dict[str, str]] | None = None) -> list[AgentRecord]:
    """Enumerate every tmux pane and classify by window-name agent rail.

    ``ambiguous`` flags panes whose ``(session, window_name)`` pair spans
    more than one distinct window index in the same session — the same
    fail-closed surface ``find_agent_window`` already raises on within a
    single session. Discovery does not raise on the ambiguity; it surfaces
    the flag so callers can decide whether to disambiguate before acting.
    """
    raw = list(panes) if panes is not None else pane_lines()
    window_indexes: dict[tuple[str, str], set[str]] = {}
    parsed: list[tuple[dict[str, str], str, str, str]] = []
    for pane in raw:
        location = pane.get("location") or ""
        session, window_index, pane_index = _parse_location(location)
        window_name = pane.get("window_name") or ""
        if window_name:
            window_indexes.setdefault((session, window_name), set()).add(window_index)
        parsed.append((pane, session, window_index, pane_index))
    records: list[AgentRecord] = []
    for pane, session, window_index, pane_index in parsed:
        window_name = pane.get("window_name") or ""
        ambig_windows = window_indexes.get((session, window_name), set())
        ambiguous = bool(window_name) and len(ambig_windows) > 1
        cwd = pane.get("cwd") or ""
        records.append(
            AgentRecord(
                pane_id=pane.get("id") or "",
                session=session,
                window_index=window_index,
                window_name=window_name,
                pane_index=pane_index,
                pane_active=(pane.get("pane_active") == "1"),
                process=pane.get("command") or "",
                cwd=cwd,
                repo_root=infer_repo_root(cwd),
                agent_kind=classify_agent_kind(window_name),
                ambiguous=ambiguous,
            )
        )
    return records


def filter_agents(
    records: Iterable[AgentRecord],
    *,
    session: str | None = None,
    agent_kind: str | None = None,
) -> list[AgentRecord]:
    out: list[AgentRecord] = []
    for record in records:
        if session is not None and record.session != session:
            continue
        if agent_kind is not None and record.agent_kind != agent_kind:
            continue
        out.append(record)
    return out
