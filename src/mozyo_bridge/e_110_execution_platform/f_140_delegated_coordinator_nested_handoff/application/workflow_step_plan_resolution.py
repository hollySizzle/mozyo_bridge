"""Namespace-free, read-only `workflow step` resolution (Redmine #15151 review j#102186).

`workflow step` picks its lane-resolution path from the repo's configured terminal
transport: under ``terminal_transport.backend: herdr`` the CLI resolves the lane
herdr-natively from the attested launch identity, and only otherwise falls back to
the tmux ``TMUX_PANE`` + discovery-inventory rail.

Review j#102186 finding_2 caught the local MCP server skipping that selection
entirely and calling the tmux rail unconditionally, so on a herdr-backed repo the
MCP ``workflow_step_plan`` tool reported ``lane_unresolved`` where the CLI
resolves a real lane. That is a **second state machine** — exactly what
``cli-mcp-shared-application-api.md`` closed for the handoff family ("judgement in
one place, two entries").

The first fix added this module but wired only MCP to it, leaving the CLI's own
`_herdr_step_preflight` + tmux branch in place — which made three places, not one,
while this docstring claimed the selection lived here "once". Review j#102241
r2f3 caught that. **Both** entries now call :func:`resolve_step_plan`: the CLI
uses it for its resolution half and keeps its executing half (store reconcile,
startup-resume gate, disposition intake, forward legs) after it, and MCP uses the
same call and stops at the plan. A structural test pins that both callers reach
this entry, so the claim is checkable rather than asserted.

It is **resolution-only**: it returns the outcome the state machine resolved and
performs no dispatch, no delivery, no lifecycle mutation and no durable write.

Two details the CLI's exit contract depends on:

- ``LaneUnavailable`` carries the original ``SystemExit`` in :attr:`abort` when
  one caused it. ``die`` has already written its message to stderr and raised
  ``CommandAbort``, so the CLI re-raises that exact object and its exit code and
  stderr output are unchanged; only the MCP caller converts it to a structured
  refusal.
- the herdr preflight still takes a Namespace. That terminates *here*, in the
  CLI-adjacent layer, rather than leaking into the MCP feature. Only ``repo`` is
  read from it (via ``repo_root_from_args``); a test pins that, so the shim
  cannot silently under-supply a field the resolver later grows.
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

    ``abort`` carries the ``SystemExit`` that caused it, when one did. The CLI
    re-raises that exact object so its exit code and already-written stderr stay
    byte-identical; the MCP caller ignores it and reports a structured refusal.
    """

    def __init__(self, message: str, *, abort: Optional[SystemExit] = None) -> None:
        super().__init__(message)
        self.abort = abort


@dataclass(frozen=True)
class StepPlanResolution:
    """One resolved step plan, plus which backend rail resolved it."""

    outcome: Any
    backend: str

    @property
    def is_herdr(self) -> bool:
        return self.backend == BACKEND_HERDR


def resolve_step_plan(
    repo_root: Path,
    *,
    anchor: Optional[Any] = None,
    pending_callback: Optional[Any] = None,
    session: Optional[str] = None,
) -> StepPlanResolution:
    """Resolve the next safe workflow step for ``repo_root``'s current lane.

    The single resolution entry both the CLI and MCP go through. The backend
    selection itself lives one level down, in the herdr preflight seam
    (:func:`_resolve_herdr`), so there is exactly one place at runtime that
    decides which rail answers. Raises :class:`LaneUnavailable` when no lane can
    be resolved.

    ``anchor`` / ``pending_callback`` / ``session`` are the already-determined
    inputs to the tmux rail's pure state machine. The herdr rail takes none of
    them — it verifies the lane's own anchor against the durable record and
    classifies from the attested launch identity — so they are accepted and
    ignored there rather than being forced into a shape it does not have.
    """
    root = Path(repo_root)
    herdr_outcome = _resolve_herdr(root)
    if herdr_outcome is not None:
        return StepPlanResolution(outcome=herdr_outcome, backend=BACKEND_HERDR)
    return StepPlanResolution(
        outcome=_resolve_tmux(
            anchor=anchor, pending_callback=pending_callback, session=session
        ),
        backend=BACKEND_TMUX,
    )


def _resolve_herdr(repo_root: Path):
    """The backend selection + herdr-native resolution, or ``None`` under tmux.

    Delegates to ``cli_workflow._herdr_step_preflight``, which is the **one**
    place the ``herdr_backend_active`` check and the herdr resolver are wired
    together. Routing through it rather than re-deriving the pair here is what
    makes the selection single: the CLI reaches it through this entry, and so
    does MCP, so there is no second copy to drift (review j#102241 r2f3).

    That function lives in the CLI-adjacent module together with the tmux seams
    (``require_tmux`` / ``current_pane`` / ``_discover_candidates``) this module
    already calls, and it has its own scenario coverage there. Reaching for it
    keeps one selection point without relocating a tested seam.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
        cli_workflow,
    )

    try:
        # The Namespace terminates here. `_herdr_step_preflight` reads only `repo`
        # off it (through `repo_root_from_args`), and the resolver it calls reads
        # nothing else; everything further comes from the attested launch
        # environment and the durable record. A test pins that.
        return cli_workflow._herdr_step_preflight(
            argparse.Namespace(repo=str(repo_root))
        )
    except SystemExit as exc:
        raise LaneUnavailable(
            _abort_message(exc, "herdr lane resolution failed"), abort=exc
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a runtime read failure is a refusal
        raise LaneUnavailable(
            f"the herdr lane could not be resolved ({type(exc).__name__})"
        ) from exc


def _resolve_tmux(
    *,
    anchor: Optional[Any],
    pending_callback: Optional[Any] = None,
    session: Optional[str] = None,
):
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
            _abort_message(exc, "no live terminal runtime for this process"), abort=exc
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
    return resolve_workflow_step(
        candidates,
        self_pane=self_pane,
        anchor=anchor,
        pending_callback=pending_callback,
        session=session,
    )


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
