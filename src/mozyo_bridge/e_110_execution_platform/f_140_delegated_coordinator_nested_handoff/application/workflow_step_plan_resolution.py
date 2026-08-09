"""Namespace-free, read-only `workflow step` resolution (Redmine #15151 review j#102186).

`workflow step` picks its lane-resolution path from the repo's configured terminal
transport: under ``terminal_transport.backend: herdr`` the CLI resolves the lane
herdr-natively from the attested launch identity, and only otherwise falls back to
the tmux ``TMUX_PANE`` + discovery-inventory rail.

Review finding_2 caught the local MCP server skipping that selection entirely and
calling the tmux rail unconditionally, so on a herdr-backed repo the MCP
``workflow_step_plan`` tool reported ``lane_unresolved`` where the CLI resolves a
real lane. That is a **second state machine** — exactly what
``cli-mcp-shared-application-api.md`` closed for the handoff family ("judgement in
one place, two entries").

So the selection lives here, once, behind a typed entry that reads no
``argparse.Namespace``. It is deliberately **resolution-only**: it returns the
outcome the state machine resolved and performs no dispatch, no delivery, no
lifecycle mutation and no durable write. The CLI keeps its own executing path
(store reconcile, startup-resume gate, disposition intake, forward legs); this
entry is the half a read/plan caller is allowed to reach.

The one CLI-shaped detail — ``resolve_herdr_step_outcome`` still takes a
Namespace — terminates *here*, in the CLI-adjacent layer, rather than leaking a
Namespace into the MCP feature. Only ``repo`` is read from it (via
``repo_root_from_args``); a test pins that so the shim cannot silently start
under-supplying a field the resolver later grows.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: The backend token each resolution path corresponds to. Reported on the result
#: so a caller can see *which* rail answered rather than inferring it.
BACKEND_HERDR = "herdr"
BACKEND_TMUX = "tmux"


class LaneUnavailable(RuntimeError):
    """The current lane could not be resolved. A refusal, never a default lane.

    Resolving a step for "some" lane would answer for somebody else's work, so an
    unresolvable lane is reported rather than substituted.
    """


@dataclass(frozen=True)
class StepPlanResolution:
    """One resolved step plan, plus which backend rail resolved it."""

    outcome: Any
    backend: str


def resolve_step_plan(
    repo_root: Path, *, anchor: Optional[Any] = None
) -> StepPlanResolution:
    """Resolve the next safe workflow step for ``repo_root``'s current lane.

    Selects the backend with the same shared ``herdr_backend_active`` check the
    CLI entry uses, then delegates to that backend's resolver. Raises
    :class:`LaneUnavailable` when no lane can be resolved.

    ``anchor`` is the already-determined durable anchor (a ``WorkflowAnchor``) and
    applies to the tmux rail's pure state machine; the herdr rail verifies the
    lane's own anchor against the durable record itself.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (  # noqa: E501
        herdr_backend_active,
    )

    root = Path(repo_root)
    if herdr_backend_active(root):
        return StepPlanResolution(
            outcome=_resolve_herdr(root), backend=BACKEND_HERDR
        )
    return StepPlanResolution(
        outcome=_resolve_tmux(anchor=anchor), backend=BACKEND_TMUX
    )


def _resolve_herdr(repo_root: Path):
    """Herdr-native resolution through the CLI's own resolver."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (  # noqa: E501
        resolve_herdr_step_outcome,
    )

    try:
        # The Namespace terminates here. `resolve_herdr_step_outcome` reads only
        # `repo` off it (through `repo_root_from_args`); everything else it needs
        # comes from the attested launch environment and the durable record.
        return resolve_herdr_step_outcome(argparse.Namespace(repo=str(repo_root)))
    except SystemExit as exc:
        raise LaneUnavailable(_abort_message(exc, "herdr lane resolution failed")) from exc
    except Exception as exc:  # noqa: BLE001 - a runtime read failure is a refusal
        raise LaneUnavailable(
            f"the herdr lane could not be resolved ({type(exc).__name__})"
        ) from exc


def _resolve_tmux(*, anchor: Optional[Any]):
    """tmux-rail resolution: self pane + discovery inventory + the pure resolver."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
        cli_workflow,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
        resolve_workflow_step,
    )

    try:
        cli_workflow.require_tmux()
        self_pane = cli_workflow.current_pane()
    except SystemExit as exc:
        raise LaneUnavailable(
            _abort_message(exc, "no live terminal runtime for this process")
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise LaneUnavailable(
            f"the current pane could not be resolved ({type(exc).__name__})"
        ) from exc
    if not self_pane:
        raise LaneUnavailable("the current pane could not be resolved")
    try:
        candidates = cli_workflow._discover_candidates()
    except Exception as exc:  # noqa: BLE001
        raise LaneUnavailable(
            f"lane candidates could not be discovered ({type(exc).__name__})"
        ) from exc
    return resolve_workflow_step(candidates, self_pane=self_pane, anchor=anchor)


def _abort_message(exc: SystemExit, fallback: str) -> str:
    """The typed abort message, or a fixed fallback.

    ``die`` raises ``CommandAbort`` carrying its message as an attribute, so the
    reason is read from the typed carrier rather than recovered from stderr.
    """
    message = getattr(exc, "message", None)
    return str(message) if message else fallback


__all__ = (
    "BACKEND_HERDR",
    "BACKEND_TMUX",
    "LaneUnavailable",
    "StepPlanResolution",
    "resolve_step_plan",
)
