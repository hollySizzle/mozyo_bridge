"""Shared herdr-backend preflight for the standard operator/agent entrypoints (Redmine #13446).

The problem this closes (issue #13446, recurrence #13435 j#74176 -> j#74177): with
``terminal_transport.backend: herdr`` selected and the workspace's Codex/Claude agents
already live, the standard entrypoints an agent reaches for first — ``handoff send
--select`` / ``agents targets`` (tmux semantic selection) and ``workflow step`` (tmux
``%pane`` self-lane resolution) — silently fall back onto the tmux-era rails and die with
a tmux-shaped diagnostic (``no_candidate:repo`` / ``self_lane_unresolved``). That is not a
per-agent attention failure; it is a harness gap where the old tmux selection surface is
still reachable as a *standard* entrypoint under the herdr backend.

Scope note (Redmine #13489): ``workflow step`` no longer uses this module's fail-closed
dead-end. It now resolves **herdr-natively** from the launch-time sender identity (see
:mod:`...f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step`) and
only reuses :func:`herdr_backend_active` for backend selection. The ``handoff send
--select`` / ``agents targets`` selection surfaces still fail closed here with the standard
dispatch hint, since a herdr session has no tmux semantic-selection equivalent to offer.

This module is the single, config-guarded preflight helper those surfaces share. It does
**not** re-implement herdr-native routing (that stays in :mod:`herdr_send_entry` /
:mod:`herdr_route_authority`); it answers two narrow questions and owns one guidance
vocabulary:

- *Is the herdr backend active for this repo?* (:func:`herdr_backend_active`) — the cheap
  selection-only check, delegating to the same
  :func:`~...application.herdr_observability.herdr_backend_selected_for` predicate the
  observability surfaces use, so a broken / absent config resolves to the tmux default and
  never diverts a surface onto the herdr guidance.
- *Which herdr-native lane-identity env vars did the caller's shell actually carry?*
  (:func:`herdr_lane_env_snapshot`) — so a fail-closed diagnostic can report that it looked
  at ``HERDR_PANE_ID`` / ``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``
  first (Acceptance 2) instead of ignoring them for a bare tmux ``%pane`` read.

The guidance strings name the standard herdr lane-dispatch surface (``sublane create /
start --execute`` via the coordinator) and mark ``handoff send`` / explicit ``%pane`` /
``agents targets`` as tmux-era primitive / debug / compat surfaces (Acceptance 4). Every
consumer stays byte-invariant under ``backend: tmux``: the helpers only produce output when
:func:`herdr_backend_active` is true.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (
    herdr_backend_selected_for,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    MOZYO_AGENT_ROLE_ENV,
    MOZYO_LANE_ID_ENV,
    MOZYO_WORKSPACE_ID_ENV,
)

# The herdr per-pane locator env a herdr UI injects into an attested lane agent's shell.
# Named alongside the launch-time sender-identity env so a preflight snapshot reports the
# full herdr-native identity surface. Not yet consumed as a routing authority anywhere in
# src (the send path resolves the target from the live inventory, not this env); it is read
# here only so a fail-closed diagnostic can show it was looked at (Redmine #13446).
HERDR_PANE_ID_ENV = "HERDR_PANE_ID"

# The herdr-native lane-identity env keys a preflight looks at *first*, in a stable order,
# before any tmux `%pane` read. `HERDR_PANE_ID` is the per-pane locator; the `MOZYO_*` trio
# is the launch-time attested sender identity (workspace / role / lane) resolved by
# `resolve_sender_identity`.
HERDR_LANE_ENV_KEYS = (
    HERDR_PANE_ID_ENV,
    MOZYO_WORKSPACE_ID_ENV,
    MOZYO_AGENT_ROLE_ENV,
    MOZYO_LANE_ID_ENV,
)

# The literal marker every herdr-backend guidance string carries so an operator (and a
# regression assertion, Redmine #13446) can key on "this diagnostic knows the herdr backend
# is active" regardless of the surrounding surface-specific wording.
HERDR_BACKEND_ACTIVE_MARKER = "herdr backend active"

# The canonical standard-dispatch hint: the one sentence every herdr-backend entrypoint
# guidance appends so the standard lane-dispatch surface and the demoted tmux-era primitives
# read the same everywhere (Acceptance 4). Kept as one constant so the surfaces never drift.
HERDR_STANDARD_DISPATCH_HINT = (
    "Standard herdr lane dispatch is `sublane create --execute` / "
    "`sublane start --execute` (through the coordinator); `handoff send` / an explicit "
    "`%pane` / `agents targets` are tmux-era primitive/debug/compat surfaces, not the "
    "standard entrypoint here. See vibes/docs/tasks/herdr-lane-operations.md."
)


def herdr_backend_active(repo_root: Path) -> bool:
    """True iff ``repo_root``'s repo-local config selects the herdr backend (fail-open to tmux).

    The cheap selection-only check (no binary resolution, no ``agent list``): delegates to
    the shared :func:`herdr_backend_selected_for` so a broken / absent / malformed config
    resolves to the tmux default (``False``) and never diverts a standard entrypoint onto
    the herdr guidance path.
    """
    try:
        return herdr_backend_selected_for(Path(repo_root))
    except (OSError, ValueError):
        return False


def herdr_lane_env_snapshot(
    env: Optional[Mapping[str, str]] = None,
) -> "dict[str, bool]":
    """Which herdr-native lane-identity env keys the caller's shell carries (non-empty).

    Pure over the injected ``env`` (defaults to :data:`os.environ`). Returns an ordered
    ``{key: present}`` map over :data:`HERDR_LANE_ENV_KEYS` — ``present`` is ``True`` only
    when the value is a non-empty / non-whitespace string. A preflight diagnostic reports
    this so it demonstrably looked at ``HERDR_PANE_ID`` / ``MOZYO_*`` before any tmux
    ``%pane`` read (Redmine #13446 Acceptance 2), rather than silently ignoring them.
    """
    source = os.environ if env is None else env
    snapshot: dict[str, bool] = {}
    for key in HERDR_LANE_ENV_KEYS:
        value = source.get(key)
        snapshot[key] = bool(isinstance(value, str) and value.strip())
    return snapshot


def herdr_lane_env_detail(env: Optional[Mapping[str, str]] = None) -> str:
    """A one-line ``key=present|absent`` rendering of :func:`herdr_lane_env_snapshot`.

    The stable diagnostic breadcrumb a fail-closed outcome puts in its ``detail`` so the
    operator sees exactly which herdr-native identity env the preflight observed. No env
    *values* are ever printed (they may carry workspace/lane identifiers) — only presence.
    """
    snapshot = herdr_lane_env_snapshot(env)
    cells = " ".join(
        f"{key}={'present' if present else 'absent'}" for key, present in snapshot.items()
    )
    return f"herdr lane env: {cells}"


def herdr_backend_guidance(env: Optional[Mapping[str, str]] = None) -> str:
    """The standard herdr-backend guidance line: marker + standard-dispatch hint.

    The single guidance string a herdr-backend entrypoint appends to its diagnostic. Always
    leads with :data:`HERDR_BACKEND_ACTIVE_MARKER` and the :data:`HERDR_STANDARD_DISPATCH_HINT`
    so every surface reads the same; the caller supplies its own surface-specific preamble
    (e.g. the selection reason) around it. ``env`` is accepted for symmetry with the other
    helpers but the base guidance is env-independent (callers that want the env snapshot
    append :func:`herdr_lane_env_detail` themselves).
    """
    return f"{HERDR_BACKEND_ACTIVE_MARKER} — {HERDR_STANDARD_DISPATCH_HINT}"


__all__ = (
    "HERDR_BACKEND_ACTIVE_MARKER",
    "HERDR_LANE_ENV_KEYS",
    "HERDR_PANE_ID_ENV",
    "HERDR_STANDARD_DISPATCH_HINT",
    "herdr_backend_active",
    "herdr_backend_guidance",
    "herdr_lane_env_detail",
    "herdr_lane_env_snapshot",
)
