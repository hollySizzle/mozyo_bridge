"""herdr-native send-target entry resolution (Redmine #13261, increment 2).

The orchestrate-entry seam that lets ``orchestrate_handoff`` resolve its send target
**without tmux** when ``terminal_transport.backend: herdr``. In increment 1 the herdr
shim only translated a rail-supplied tmux ``%N`` into a herdr locator; the rail still
resolved that ``%N`` through the tmux pane resolver (``pane_info``), which dies in a
pure herdr session (no tmux server). This module closes that gap: under the herdr
backend the rail resolves the target from the **launch-time sender identity** (env +
anchor) + the **live herdr inventory** (WU1 :func:`resolve_herdr_target`) and hands
``orchestrate_handoff`` a synthesized, ``project_preflight_target``-compatible pane
record whose ``id`` is the live herdr locator — so every downstream guard / projection
that reads pane-dict fields keeps working, and the shim passes the locator straight
through (it is already ``valid_target``).

Kept out of the oversized ``application/commands.py`` (module-health gate): the command
module keeps only a small, strictly config-guarded branch that calls
:func:`herdr_backend_selected` and :func:`resolve_herdr_send_target`. The ``backend:
tmux`` path never touches this module, so it stays byte-identical.

Fail-closed: an un-attested sender identity, an unavailable herdr binary / inventory,
or a receiver that does not resolve to a single live agent raises
:class:`HerdrSendEntryError`; the caller emits a structured ``blocked`` /
``target_unavailable`` outcome and ``die``s — never a silent tmux fallback, never a send
to a guessed target.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.core.state.workspace_registry import read_anchor
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
    resolve_coordinator_provider,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    resolve_herdr_target,
    resolve_sender_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    TerminalTransportConfig,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    resolve_agent_lister,
)


class HerdrSendEntryError(ValueError):
    """A pure-herdr send target cannot be resolved (fail-closed)."""

    def __init__(self, message: str, *, reason: Optional[str] = None):
        super().__init__(message)
        self.reason = reason


def _terminal_transport_config(args: argparse.Namespace) -> Optional[TerminalTransportConfig]:
    """The repo-local ``terminal_transport`` selection, or ``None`` if unreadable."""
    try:
        return load_repo_local_config(repo_root_from_args(args)).terminal_transport
    except RepoLocalConfigError:
        return None


def herdr_backend_selected(args: argparse.Namespace) -> bool:
    """True iff the repo-local config selects the herdr terminal backend.

    A broken / unreadable config is *not* a herdr selection (it resolves to the tmux
    default), exactly like :func:`resolve_handoff_transport_binding` — so an absent /
    malformed config never diverts the send onto the herdr path.
    """
    config = _terminal_transport_config(args)
    return config is not None and config.backend == BACKEND_HERDR


def resolve_herdr_send_target(args: argparse.Namespace, *, receiver: str) -> dict:
    """Resolve the herdr-native send target and synthesize its pane record (fail-closed).

    Resolves the sender identity (``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` /
    ``MOZYO_LANE_ID`` cross-checked against the repo anchor), lists the live herdr
    inventory, and resolves ``receiver`` to a single live agent scoped to the sender's
    workspace + provider role (WU1). Returns a ``project_preflight_target``-compatible
    pane dict whose ``id`` is the live herdr locator.

    The synthesized record projects as a ``normal_window`` agent (role carried on the
    ``window_name``, not a ``@mozyo_agent_role`` pane option): a herdr agent is not a
    cockpit-managed pane, so the main-lane cockpit guard stays inactive while
    ``binds_receiver`` still resolves the strong role. Raises
    :class:`HerdrSendEntryError` on any fail-closed condition.
    """
    config = _terminal_transport_config(args)
    if config is None or config.backend != BACKEND_HERDR:
        raise HerdrSendEntryError(
            "herdr send target requested but the herdr backend is not selected",
            reason="backend_not_selected",
        )
    repo_root = repo_root_from_args(args)
    anchor = read_anchor(repo_root)
    anchor_ws = anchor.get("workspace_id") if isinstance(anchor, dict) else None

    sender_res = resolve_sender_identity(os.environ, anchor_workspace_id=anchor_ws)
    if not sender_res.ok or sender_res.identity is None:
        raise HerdrSendEntryError(
            "herdr backend selected but the sender identity is not attested "
            f"(reason={sender_res.reason}): {sender_res.detail}",
            reason=sender_res.reason,
        )
    sender = sender_res.identity
    coordinator_provider = resolve_coordinator_provider(str(repo_root))

    try:
        lister = resolve_agent_lister(config)
        if lister is None:  # defensive: herdr_enabled implies non-None
            raise HerdrSendEntryError(
                "herdr backend selected but no agent lister could be resolved"
            )
        rows = lister.list_agent_rows()
    except TerminalTransportError as exc:
        raise HerdrSendEntryError(
            f"herdr inventory unavailable: {exc}", reason=getattr(exc, "reason", None)
        )

    resolution = resolve_herdr_target(
        receiver, sender, rows, coordinator_provider=coordinator_provider
    )
    if resolution.is_fail:
        raise HerdrSendEntryError(
            f"herdr target resolution failed for receiver {receiver!r} in workspace "
            f"{sender.workspace_id!r} (reason={resolution.reason}): {resolution.detail}",
            reason=resolution.reason,
        )
    identity = resolution.identity
    assert identity is not None  # success guarantees an identity
    return {
        "id": resolution.locator,
        # No tmux location: the pure-herdr target is addressed by its live locator and
        # its identity is already workspace/role-scoped by the inventory decode. The
        # tmux-session gates that read `location` are skipped under the herdr backend.
        "location": "",
        "window_name": identity.role,
        "command": identity.role,
        "pane_active": "1",
        # normal_window projection (role on window_name, no @mozyo_agent_role option):
        # a herdr agent is not a cockpit pane, so the main-lane cockpit guard stays
        # inactive while binds_receiver still resolves the strong role.
        "agent_role": "",
        "workspace_id": identity.workspace_id,
        "lane_id": identity.lane_id,
        "cwd": str(repo_root),
        # Diagnostic breadcrumb (not consumed by the pane projection): the durable
        # herdr name this locator was resolved from.
        "herdr_assigned_name": resolution.assigned_name,
        # The env-derived SENDER Unit (Redmine #13261 increment 4). Carried on the
        # target record so the gateway-route gate can enforce on the sender's lane
        # without a tmux `current_pane_lane_unit()` call — the sender identity was
        # already resolved here (single resolution, no duplication). Not part of the
        # pane projection (project_preflight_target ignores unknown keys).
        "herdr_sender_workspace_id": sender.workspace_id,
        "herdr_sender_lane_id": sender.lane_id,
    }


__all__ = (
    "HerdrSendEntryError",
    "herdr_backend_selected",
    "resolve_herdr_send_target",
)
