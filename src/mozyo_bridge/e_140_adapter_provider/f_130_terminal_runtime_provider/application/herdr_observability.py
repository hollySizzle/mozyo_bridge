"""Shared herdr operator-observability read model (Redmine #13355).

The tmux→herdr gap audit (#13331 j#73370) found that a pure-herdr operator has no
CLI surface reading agent health: ``doctor`` (18 modules), ``mozyo-bridge status``
and the runtime observation timeline all probe tmux only. Three surfaces close
that gap — the doctor herdr section (``application/doctor_herdr``), the ``status``
herdr backend block (``application/commands_status``) and the ``observe reload``
herdr source (``f_150 commands_runtime_observation``) — and all three need the
same read: *is the herdr backend selected for this repo, is the live inventory
readable, and what identity / runtime state does each managed row carry?*

This module is that one shared read model, homed in the terminal-runtime provider
feature so the three consumer surfaces stay thin and never drift on decode /
status-mapping semantics:

- :func:`project_observed_agents` — the pure ``agent list`` row projection:
  mzb1 name decode (#13247), ``agent_status`` -> runtime receiver-state mapping
  (#13246), transient locator read. A row that does not decode is kept as an
  *unmanaged* row (with its decode reason) rather than dropped, so a foreign or
  malformed name stays visible to the operator instead of silently vanishing.
- :func:`read_herdr_inventory` — the live read: repo-local backend selection
  (a broken / absent config is NOT a herdr selection, exactly like the send
  path's ``herdr_backend_selected``), the #13331 shared workspace-segment
  resolution, and the fail-closed ``agent list`` snapshot. Every mechanical
  failure (binary unconfigured / not found, spawn error, timeout, non-zero
  exit, unrecognisable payload) is carried as a structured failure — never an
  empty *success*, so a down herdr server is always a visible fail, and never a
  raise, so a diagnostic surface cannot crash on a broken transport.

Read-only and diagnostic-only: this view is a layer-1 runtime observation
(``vibes/docs/logics/ack-completion-receiver-state.md``); it never asserts
workflow truth and never authorizes an action. The tmux backend path never
reaches this module (selection is checked first), keeping tmux output
byte-invariant.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
)

# The herdr JSON keys a status token may live under on an ``agent list`` row —
# the same candidate set the agent-state reader scans (``infrastructure
# .herdr_state._STATUS_KEYS``), duplicated as a value here so this application
# module does not import an infrastructure-private constant.
_ROW_STATUS_KEYS = ("agent_status", "status", "state")


@dataclass(frozen=True)
class HerdrObservedAgent:
    """One ``agent list`` row projected for an operator-observability surface.

    ``managed`` is True iff the row's assigned name decodes as an mzb1 identity;
    then ``workspace_id`` / ``lane_id`` / ``role`` carry the decoded slot and
    ``decode_reason`` is ``None``. An unmanaged (foreign / malformed) row keeps
    its raw name and the fail-closed decode reason, with empty slot fields.
    ``runtime_state`` is always a member of the mozyo runtime receiver-state
    vocabulary (#13246, fail-closed to ``unknown``); ``raw_status`` is the
    herdr-reported token kept as provenance (``""`` when absent). ``locator``
    is the transient pane locator (``""`` when absent) — never an identity.
    """

    name: str
    managed: bool
    workspace_id: str = ""
    lane_id: str = ""
    role: str = ""
    runtime_state: str = "unknown"
    raw_status: str = ""
    locator: str = ""
    decode_reason: Optional[str] = None

    def to_record(self) -> dict:
        """A JSON-serializable dict (the doctor section / ``--json`` shape)."""
        return {
            "name": self.name,
            "managed": self.managed,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "runtime_state": self.runtime_state,
            "raw_status": self.raw_status,
            "locator": self.locator,
            "decode_reason": self.decode_reason,
        }


@dataclass(frozen=True)
class HerdrInventoryView:
    """The shared herdr observability read result (fail-closed).

    ``backend_selected`` is False when the repo-local config does not select the
    herdr backend — the consumer surfaces then render *nothing*, keeping the
    tmux backend output byte-invariant. When selected, ``ok`` is the sole
    authority on whether the live inventory read mechanically succeeded; on
    failure ``reason`` carries the transport failure vocabulary
    (``binary_unconfigured`` / ``binary_not_found`` / ``transport_error`` /
    ``invalid_payload``) and ``detail`` a bounded diagnostic. A down herdr
    server is therefore always a structured, visible failure — never an empty
    success and never a raise. ``workspace_segment`` is this repo's #13331
    shared mzb1 workspace segment (``""`` when unresolvable, itself a
    diagnosable identity gap).
    """

    backend_selected: bool
    ok: bool = False
    reason: Optional[str] = None
    detail: str = ""
    workspace_segment: str = ""
    agents: tuple = ()

    @property
    def managed_agents(self) -> tuple:
        return tuple(agent for agent in self.agents if agent.managed)

    @property
    def unmanaged_agents(self) -> tuple:
        return tuple(agent for agent in self.agents if not agent.managed)

    def workspace_agents(self) -> tuple:
        """The managed rows whose decoded workspace is this repo's segment."""
        if not self.workspace_segment:
            return ()
        return tuple(
            agent
            for agent in self.managed_agents
            if agent.workspace_id == self.workspace_segment
        )


def _row_status_token(row: Mapping) -> str:
    for key in _ROW_STATUS_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def project_observed_agents(rows: Sequence) -> tuple:
    """Project raw ``agent list`` rows into :class:`HerdrObservedAgent` records.

    Pure over the injected rows (no subprocess / config read). A non-mapping row
    is skipped; every mapping row is kept — decoded (managed) or not (unmanaged,
    with its fail-closed decode reason) — so the operator view never silently
    drops an agent the server reports. Deterministic input ordering.
    """
    observed = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _norm(row.get(AGENT_KEY_NAME))
        raw_status = _row_status_token(row)
        runtime_state = map_agent_status(raw_status or None)
        locator = _agent_locator(row)
        decode = decode_assigned_name(name)
        if decode.ok and decode.identity is not None:
            identity = decode.identity
            observed.append(
                HerdrObservedAgent(
                    name=name,
                    managed=True,
                    workspace_id=identity.workspace_id,
                    lane_id=identity.lane_id,
                    role=identity.role,
                    runtime_state=runtime_state,
                    raw_status=raw_status,
                    locator=locator,
                )
            )
        else:
            observed.append(
                HerdrObservedAgent(
                    name=name,
                    managed=False,
                    runtime_state=runtime_state,
                    raw_status=raw_status,
                    locator=locator,
                    decode_reason=decode.reason,
                )
            )
    return tuple(observed)


def _terminal_transport_config(repo_root: Path) -> Optional[TerminalTransportConfig]:
    """The repo-local ``terminal_transport`` selection, or ``None`` if unreadable.

    A broken / unreadable / absent config resolves to ``None`` (the tmux
    default), exactly like the send path's ``herdr_backend_selected`` — so an
    observability surface can never be diverted onto the herdr path by a
    malformed config.
    """
    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
        RepoLocalConfigError,
    )

    try:
        return load_repo_local_config(repo_root).terminal_transport
    except RepoLocalConfigError:
        return None


def _workspace_segment(repo_root: Path) -> str:
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        herdr_workspace_segment,
    )

    try:
        return herdr_workspace_segment(repo_root)
    except (OSError, ValueError):
        return ""


def herdr_backend_selected_for(repo_root: Path) -> bool:
    """True iff ``repo_root``'s repo-local config selects the herdr backend.

    The cheap selection-only check (no binary resolution, no ``agent list``)
    for callers that must decide *whether* to observe before paying for the
    read — e.g. ``observe reload --source all`` including the herdr snapshot
    only under the herdr backend (tmux byte-invariance).
    """
    config = _terminal_transport_config(repo_root)
    return config is not None and config.backend == BACKEND_HERDR


def read_herdr_inventory(
    repo_root: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    lister=None,
) -> HerdrInventoryView:
    """The live herdr observability read for ``repo_root`` (fail-closed, no raise).

    Backend selection is checked first: a repo whose config does not select the
    herdr backend returns ``backend_selected=False`` and performs **no** herdr
    read at all (tmux byte-invariance). Under the herdr backend, the binary
    resolution and the ``agent list`` snapshot fail closed into a structured
    failure view (``ok=False`` + transport reason/detail) — a down or
    misconfigured herdr server is a visible diagnostic fact, never a crash of
    the diagnostic surface itself. ``lister`` / ``env`` are injectable so tests
    never spawn a live herdr binary.
    """
    config = _terminal_transport_config(repo_root)
    if config is None or config.backend != BACKEND_HERDR:
        return HerdrInventoryView(backend_selected=False)
    segment = _workspace_segment(repo_root)
    source_env = env if env is not None else os.environ
    try:
        if lister is None:
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (  # noqa: E501
                resolve_agent_lister,
            )

            lister = resolve_agent_lister(config, env=source_env)
        if lister is None:  # defensive: herdr_enabled implies non-None
            return HerdrInventoryView(
                backend_selected=True,
                ok=False,
                reason=REASON_TRANSPORT_ERROR,
                detail="herdr backend selected but no agent lister could be resolved",
                workspace_segment=segment,
            )
        rows = lister.list_agent_rows()
    except TerminalTransportError as exc:
        return HerdrInventoryView(
            backend_selected=True,
            ok=False,
            reason=getattr(exc, "reason", None) or REASON_TRANSPORT_ERROR,
            detail=str(exc),
            workspace_segment=segment,
        )
    return HerdrInventoryView(
        backend_selected=True,
        ok=True,
        workspace_segment=segment,
        agents=project_observed_agents(rows),
    )


__all__ = (
    "HerdrInventoryView",
    "HerdrObservedAgent",
    "herdr_backend_selected_for",
    "project_observed_agents",
    "read_herdr_inventory",
)
