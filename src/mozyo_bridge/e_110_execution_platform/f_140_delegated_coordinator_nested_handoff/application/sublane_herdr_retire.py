"""herdr sublane retire guarded close (Redmine #13331 option A, design j#73314).

The tmux ``sublane retire`` is preflight / runbook only — the destructive half (pane kill
/ ``git worktree remove`` / branch delete) is gated behind a Design Consultation per
``vibes/docs/logics/worktree-lifecycle-boundary.md``. j#73314 (that design consultation's
answer) authorizes ONE narrow herdr actuation for retire: closing the lane workspace's own
**managed** agents — ``mzb1_<lane-ws>_codex_default`` / ``mzb1_<lane-ws>_claude_default`` —
so the last pane close lets the lane's herdr workspace disappear. It authorizes **only**
that: no ``git worktree remove`` (still runbook), and only the managed default-lane
gateway / worker slots (never a foreign / unmanaged agent).

This module is that guarded close, kept opt-in (``sublane retire --execute``) and gated on
the existing fail-closed retire preflight (``may_retire`` — issue closed / owner approved /
callbacks drained / verified / durable record / target known). It is structurally safe:
:func:`plan_herdr_retire_close` only ever lists the two managed slots as close targets, so
a foreign agent cannot be closed even if one shares the workspace; the plan records foreign
presence for the audit trail but never acts on it.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    _close_base_pane,
    _resolve_binary_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

#: The two managed provider roles a lane workspace's default-lane slots carry.
_MANAGED_ROLES = ("codex", "claude")


@dataclass(frozen=True)
class HerdrRetireClosePlan:
    """The fail-closed plan for a lane workspace's guarded retire close.

    ``close_targets`` are the ``(role, locator)`` pairs of the workspace's managed
    default-lane gateway / worker slots — the ONLY agents this retire ever closes.
    ``foreign_names`` records any *other* managed-scheme agent decoded into this workspace
    (a non-default lane or a non-gateway/worker role): informational for the audit trail
    (the workspace will not disappear while they live) and never a close target.
    """

    workspace_id: str
    close_targets: tuple[tuple[str, str], ...] = ()
    foreign_names: tuple[str, ...] = ()

    @property
    def has_targets(self) -> bool:
        return bool(self.close_targets)


@dataclass(frozen=True)
class HerdrRetireCloseResult:
    """The outcome of executing a guarded retire close (per-target, non-fatal)."""

    workspace_id: str
    closed: tuple[tuple[str, str], ...] = ()  # (role, locator) successfully closed
    failed: tuple[tuple[str, str, str], ...] = ()  # (role, locator, detail)
    foreign_names: tuple[str, ...] = ()

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "foreign_names": list(self.foreign_names),
        }


def plan_herdr_retire_close(
    rows: Sequence[Mapping[str, object]], *, workspace_id: str
) -> HerdrRetireClosePlan:
    """Decide which managed slots to close for ``workspace_id`` (pure, fail-closed).

    Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a close target iff
    it decodes to ``(workspace_id, default lane, codex|claude)`` and carries a live locator.
    A managed-scheme row in this workspace that is NOT a default-lane gateway / worker slot
    is recorded in ``foreign_names`` (never closed). Rows in other workspaces and
    undecodable (foreign) rows are ignored — an empty ``workspace_id`` matches nothing.
    """
    ws = _norm(workspace_id)
    close_targets: list[tuple[str, str]] = []
    foreign: list[str] = []
    if not ws:
        return HerdrRetireClosePlan(workspace_id=ws)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get(AGENT_KEY_NAME)
        decode = decode_assigned_name(name)
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != ws:
            continue
        if _norm_lane(identity.lane_id) == DEFAULT_LANE and identity.role in _MANAGED_ROLES:
            locator = _agent_locator(row)
            if locator:
                close_targets.append((identity.role, locator))
            continue
        # A managed-scheme agent in this workspace that is not a default-lane gateway /
        # worker slot: record it, but never close it (guarded).
        foreign.append(_norm(name))
    return HerdrRetireClosePlan(
        workspace_id=ws,
        close_targets=tuple(close_targets),
        foreign_names=tuple(foreign),
    )


def execute_herdr_retire_close(
    plan: HerdrRetireClosePlan,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> HerdrRetireCloseResult:
    """Close the planned managed slots via ``herdr pane close`` (per-target, non-fatal).

    Only ``plan.close_targets`` are closed — never a foreign row. Each close is non-fatal
    (a failed close leaves a live agent, recorded, not raised), mirroring the #13330 base
    pane reclaim's non-fatal ``pane close`` contract.
    """
    environ = env if env is not None else os.environ
    binary = _resolve_binary_or_die(environ)
    run = runner or subprocess.run
    closed: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for role, locator in plan.close_targets:
        ok, detail = _close_base_pane(binary, locator, run, timeout, environ)
        if ok:
            closed.append((role, locator))
        else:
            failed.append((role, locator, detail))
    return HerdrRetireCloseResult(
        workspace_id=plan.workspace_id,
        closed=tuple(closed),
        failed=tuple(failed),
        foreign_names=plan.foreign_names,
    )


__all__ = (
    "HerdrRetireClosePlan",
    "HerdrRetireCloseResult",
    "execute_herdr_retire_close",
    "plan_herdr_retire_close",
)
