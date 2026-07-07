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
from pathlib import Path
from typing import Optional

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AUTO_TARGET_REPO,
    is_explicit_pane_target,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
    resolve_coordinator_provider,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_route_authority import (
    resolve_herdr_cross_workspace_target,
    resolve_herdr_route_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
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


def explicit_tmux_pane_target(args: argparse.Namespace) -> bool:
    """True iff this send names an explicit tmux pane-id (``%N``) target.

    Redmine #13320 (a-narrow, j#73114): the target-kind half of the effective-backend
    predicate. Lane choreography — ``sublane create --execute`` dispatch,
    gateway→worker relay, worker/gateway callback — always addresses its peer by an
    explicit ``--target %NN`` pane, so those legs must ride the tmux runtime rail even
    when ``terminal_transport.backend: herdr`` is the repo-local default. Role /
    receiver-name based implicit resolution and herdr locator / assigned-name targets
    carry no ``%N`` handle and are unaffected. Pure/string-only over ``args.target``.
    """
    return is_explicit_pane_target(getattr(args, "target", None))


def herdr_effective_backend_selected(args: argparse.Namespace) -> bool:
    """True iff this send should use the herdr backend **for its target kind** (#13320).

    The single effective-backend predicate shared by both send-path branch points —
    the ``@bind_runtime_transport`` decorator (which installs the herdr binding +
    turn-start rail) and ``orchestrate_handoff`` (which gates ``require_tmux()`` and
    target resolution). It is the config-level herdr selection
    (:func:`herdr_backend_selected`) NARROWED by target kind (auditor answer j#73114,
    a-narrow): an explicit tmux ``%pane`` target is NOT a herdr send even under
    ``backend: herdr`` — it routes through the tmux rail.

    Both branch points MUST read this one predicate (or its
    :func:`explicit_tmux_pane_target` half). A half-applied split — e.g. skipping
    ``require_tmux()`` for a herdr config while still handing an explicit ``%pane`` to
    the herdr shim, or installing the herdr rail while ``orchestrate_handoff`` runs the
    tmux path — would hand a tmux target to the herdr locator resolver or skip tmux
    preflight. Routing a ``%pane`` to the tmux rail under ``backend: herdr`` is an
    explicit target-kind route selection, not a silent fallback: an unresolvable
    ``%pane`` / absent tmux / receiver-binding mismatch still fails closed on the tmux
    path exactly as it does under ``backend: tmux``.
    """
    return herdr_backend_selected(args) and not explicit_tmux_pane_target(args)


def herdr_auto_target_repo(args: argparse.Namespace) -> str:
    """Resolve ``--target-repo auto`` for a herdr send to the sender's own repo root.

    Redmine #13331 (j#73312 scope addition #2): a herdr send has no tmux ``%pane`` /
    ``pane_current_path`` to infer a target repo from, so the ``%pane``-only ``auto`` gate is
    inapplicable — it forced a hand-passed ``--target-repo <root>`` even for a same-workspace
    herdr send. The herdr target is already workspace-scoped by the resolver (a same-workspace
    send resolves the receiver in the sender's own workspace; a cross-workspace lane dispatch
    passes an EXPLICIT ``--target-repo``, never ``auto``), so ``auto`` resolves to the sender's
    own repo root — the same-workspace target's repo — and flows through the unchanged
    ``target_repo_mismatch`` gate like a hand-passed root. Kept out of the oversized
    ``commands.py`` (module-health gate); the command module calls this from its herdr branch.
    """
    return str(repo_root_from_args(args))


def _explicit_target_workspace_id(args: argparse.Namespace) -> str:
    """The herdr workspace *segment* of an explicit ``--target-repo <path>``, or ``""``.

    Redmine #13331: option A makes a lane its own herdr workspace, so a
    coordinator→lane-gateway dispatch names the lane worktree with an explicit
    ``--target-repo <worktree-root>``. The segment is resolved through the SAME shared
    resolver :func:`~...herdr_session_start.herdr_workspace_segment` used to mint the lane
    (design j#73357): a linked git worktree → its path-derived lane token (the exact
    ``workspace`` segment its ``mzb1_<segment>_<role>_default`` agents carry), a standalone
    checkout → its registry workspace_id. Returns ``""`` for the ``auto`` sentinel, an
    unset value, or an unresolvable path (so the caller falls back to same-workspace
    resolution rather than guessing a workspace).
    """
    raw = getattr(args, "target_repo", None)
    if not raw or raw == AUTO_TARGET_REPO:
        return ""
    try:
        return herdr_workspace_segment(Path(raw))
    except (OSError, ValueError):
        return ""


def resolve_herdr_send_target(args: argparse.Namespace, *, receiver: str) -> dict:
    """Resolve the herdr-native send target and synthesize its pane record (fail-closed).

    Resolves the sender identity (``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` /
    ``MOZYO_LANE_ID`` cross-checked against the repo anchor), lists the live herdr
    inventory, and resolves ``receiver`` to a single live agent through the #13305
    backend-neutral route authority — the lane-in-match tuple
    ``(workspace_id, lane_id, role, pane_name)`` with a deterministically derived lane
    (:func:`~...application.herdr_route_authority.resolve_herdr_route_target`), not the
    legacy lane-less ``(workspace_id, role)`` projection. Returns a
    ``project_preflight_target``-compatible pane dict whose ``id`` is the live herdr
    locator.

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
    # Redmine #13331 (design j#73357): the sender's own workspace segment. For a lane agent
    # (gateway / worker) the repo root is a linked git worktree with NO registry anchor, so
    # cross-check the launch-injected `MOZYO_WORKSPACE_ID` against the shared segment
    # resolver (a path-derived lane token) instead of the absent anchor — otherwise the lane
    # agent could never resolve its own identity to send (gateway→worker, callbacks). A
    # standalone / main checkout resolves to its registry workspace_id, byte-for-byte as
    # before (the env↔anchor mismatch guard is preserved).
    anchor_ws = herdr_workspace_segment(repo_root) or None

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

    # Redmine #13331: cross-workspace explicit dispatch. When `--target-repo <path>`
    # names a worktree whose mozyo workspace id differs from the sender's, the receiver
    # lives in THAT workspace (a lane's own herdr workspace under option A), which the
    # sender-scoped route authority below cannot reach. Resolve the receiver's canonical
    # default-lane slot in the named target workspace instead (still the one
    # backend-neutral route authority; the live locator stays transient cache). A
    # `--target-repo` that resolves to the sender's OWN workspace (or `auto` / unset)
    # falls through to the same-workspace path unchanged, so same-workspace herdr sends
    # are byte-for-byte as before.
    target_workspace_id = _explicit_target_workspace_id(args)
    cross_workspace = bool(target_workspace_id) and target_workspace_id != sender.workspace_id
    if cross_workspace:
        explicit_lane = getattr(args, "target_lane", None)
        resolution = resolve_herdr_cross_workspace_target(
            receiver,
            target_workspace_id,
            rows,
            coordinator_provider=coordinator_provider,
            target_lane=_norm(explicit_lane) if explicit_lane else "",
        )
    else:
        # Redmine #13305: resolve through the single backend-neutral route authority
        # (lane-in-match `(workspace_id, lane_id, role, pane_name)`), not the lane-less
        # `(workspace_id, role)` projection. A lane-unspecified send derives one lane
        # deterministically (explicit > sender same-lane > coordinator default > legacy
        # default) and re-resolves that slot; a slot not live fails closed with the
        # #13302 ledger vocabulary rather than scanning all lanes. `--target-lane`
        # (absent today) is threaded so a future explicit-lane caller is honoured.
        explicit_lane = getattr(args, "target_lane", None)
        resolution = resolve_herdr_route_target(
            receiver,
            sender,
            rows,
            coordinator_provider=coordinator_provider,
            explicit_lane=explicit_lane,
        )
    if resolution.is_fail:
        raise HerdrSendEntryError(
            f"herdr target resolution failed for receiver {receiver!r} in workspace "
            f"{sender.workspace_id!r} lane {resolution.lane!r} "
            f"(reason={resolution.reason}): {resolution.detail}",
            reason=resolution.reason,
        )
    identity = resolution.identity
    assert identity is not None  # success guarantees an identity
    # The synthesized target record's `cwd` is the TARGET agent's repo root (the tmux path
    # reads the target pane's own cwd). For a same-workspace send that is the sender's repo;
    # for a #13331 cross-workspace dispatch it is the named lane worktree (`--target-repo`),
    # so the downstream `target_repo_mismatch` gate compares like-for-like (observed target
    # cwd vs the explicit `--target-repo`) instead of blocking on the sender's own root.
    if cross_workspace:
        target_cwd = str(Path(getattr(args, "target_repo")).expanduser())
    else:
        target_cwd = str(repo_root)
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
        "cwd": target_cwd,
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
    "explicit_tmux_pane_target",
    "herdr_backend_selected",
    "herdr_effective_backend_selected",
    "resolve_herdr_send_target",
)
