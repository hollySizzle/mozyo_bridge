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
    AGENT_PROVIDERS,
    MOZYO_WORKSPACE_ID_ENV,
    REASON_MISSING_SENDER_ENV,
    RECEIVER_COORDINATOR,
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


#: The existing delivery-outcome reason a herdr explicit-target mismatch projects onto
#: (Redmine #13884 review j#83307 F1/F2). NOT a new fail-closed token: the herdr resolution
#: vocabulary stays the #13302 ledger set (``vibes/docs/specs/herdr-native-identity.md``
#: §3.1). ``invalid_args`` (an inconsistent ``--target`` argument, not an unavailable window)
#: is the pre-existing ``DeliveryOutcome`` reason whose ``next_action`` ("supply the required
#: arguments") is consistent with the cause; the full ``--target-lane`` retry guidance rides
#: the die message. ``orchestrate_handoff`` reads this off ``exc.reason`` and surfaces it
#: instead of the generic ``target_unavailable`` (which would tell the operator to start a
#: window — contradicting the cause).
EXPLICIT_TARGET_MISMATCH_OUTCOME_REASON: str = "invalid_args"


class HerdrExplicitTargetMismatchError(HerdrSendEntryError):
    """An explicit ``--target`` named a different agent than the resolved route (#13884).

    A discriminable :class:`HerdrSendEntryError` subclass carrying the pre-existing
    :data:`EXPLICIT_TARGET_MISMATCH_OUTCOME_REASON` (``invalid_args``) as its ``reason`` — no
    new fail-closed token is minted (herdr-native-identity.md §3.1). The locator is never
    promoted to a routing authority (#13305); this only refuses when the named target and
    the resolved target disagree, and it stays a zero-send (``target=None``, no injection).
    """

    def __init__(self, message: str):
        super().__init__(message, reason=EXPLICIT_TARGET_MISMATCH_OUTCOME_REASON)


def _terminal_transport_config_for_root(
    repo_root: Path,
) -> Optional[TerminalTransportConfig]:
    """The repo-local ``terminal_transport`` selection for a resolved repo root.

    The Namespace-free core (Redmine #13729): the handoff facade resolves the repo
    root once at its boundary and passes the ``Path`` in, so this and the callers
    below never touch an ``argparse.Namespace``.
    """
    try:
        return load_repo_local_config(repo_root).terminal_transport
    except RepoLocalConfigError:
        return None


def _terminal_transport_config(args: argparse.Namespace) -> Optional[TerminalTransportConfig]:
    """The repo-local ``terminal_transport`` selection, or ``None`` if unreadable."""
    return _terminal_transport_config_for_root(repo_root_from_args(args))


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


def herdr_effective_backend_selected(*, repo_root: Path, target: str | None) -> bool:
    """True iff this send should use the herdr backend **for its target kind** (#13320).

    Redmine #13729: the handoff facade passes the resolved ``repo_root`` (its
    boundary Namespace->Path conversion) and the raw ``--target`` scalar, so this
    predicate is Namespace-free. Behaviour is byte-identical to the former
    ``herdr_backend_selected(args) and not explicit_tmux_pane_target(args)``.

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
    config = _terminal_transport_config_for_root(repo_root)
    backend_selected = config is not None and config.backend == BACKEND_HERDR
    return backend_selected and not is_explicit_pane_target(target)


def herdr_auto_target_repo(repo_root: Path) -> str:
    """Resolve ``--target-repo auto`` for a herdr send to the sender's own repo root.

    Redmine #13729: takes the facade-resolved ``repo_root`` (Namespace-free);
    byte-identical to the former ``str(repo_root_from_args(args))``.

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
    return str(repo_root)


def _legacy_lane_token(repo_root: Path) -> str:
    """The pre-#13377 per-lane workspace token for a linked-worktree repo, or ``""``.

    Deterministically re-derived from the worktree's canonical path — the exact value a
    #13331-model launch injected as ``MOZYO_WORKSPACE_ID`` — so the legacy attestation
    compat in :func:`resolve_herdr_send_target` matches only the token this checkout
    could legitimately carry. ``""`` for a main / standalone checkout (no legacy form).
    """
    from mozyo_bridge.core.state.workspace_registry import _is_linked_worktree
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
    )

    try:
        resolved = Path(repo_root).expanduser().resolve()
        if not _is_linked_worktree(resolved):
            return ""
        return derive_lane_workspace_token(str(resolved))
    except (OSError, ValueError):
        return ""


def _explicit_target_workspace_id(target_repo: str | None) -> str:
    """The herdr workspace *segment* of an explicit ``--target-repo <path>``, or ``""``.

    Redmine #13377 (shared project workspace): a lane worktree's segment resolves to the
    MAIN checkout's workspace identity through the SAME shared resolver
    :func:`~...herdr_session_start.herdr_workspace_segment` used to mint the lane slots
    — so a coordinator→lane-gateway ``--target-repo <lane-worktree>`` resolves to the
    sender's OWN workspace and the dispatch flows through the same-workspace,
    explicit-lane path (``--target-repo`` is a repo/cwd gate, not a workspace selector;
    j#73613). A genuinely different repo (cross-project send) still resolves to a
    distinct workspace and takes the cross-workspace branch. Returns ``""`` for the
    ``auto`` sentinel, an unset value, or an unresolvable path (so the caller falls back
    to same-workspace resolution rather than guessing a workspace).
    """
    raw = target_repo
    if not raw or raw == AUTO_TARGET_REPO:
        return ""
    try:
        return herdr_workspace_segment(Path(raw))
    except (OSError, ValueError):
        return ""


def resolve_herdr_send_target(
    *,
    repo_root: Path,
    target: str | None,
    target_repo: str | None,
    target_lane: str | None,
    receiver: str,
) -> dict:
    """Resolve the herdr-native send target and synthesize its pane record (fail-closed).

    Redmine #13729: the handoff facade passes the resolved ``repo_root`` and the
    raw ``--target`` / ``--target-repo`` / ``--target-lane`` scalars, so this
    resolver is Namespace-free; behaviour is byte-identical to the former
    ``args``-reading form.

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
    config = _terminal_transport_config_for_root(repo_root)
    if config is None or config.backend != BACKEND_HERDR:
        raise HerdrSendEntryError(
            "herdr send target requested but the herdr backend is not selected",
            reason="backend_not_selected",
        )
    # Redmine #13377 (design j#73613): the sender's own workspace segment. A lane agent
    # (gateway / worker) runs in a linked git worktree, whose segment now resolves to the
    # MAIN checkout's workspace identity (shared project workspace) — matching the
    # `MOZYO_WORKSPACE_ID=<project-ws>` its launch injected. A standalone / main checkout
    # resolves to its registry workspace_id, byte-for-byte as before (the env↔anchor
    # mismatch guard is preserved).
    anchor_ws = herdr_workspace_segment(repo_root) or None
    # Legacy lane attestation compatibility (pre-#13377 lanes still live during the
    # transition): a #13331-model lane agent was launched with
    # `MOZYO_WORKSPACE_ID=wt_<hash>` (its own per-lane workspace). Accept exactly the
    # worktree's deterministically re-derived token — never an arbitrary env value — so
    # a live legacy lane keeps sending (gateway→worker, callbacks) until it retires.
    env_ws = _norm(os.environ.get(MOZYO_WORKSPACE_ID_ENV))
    if env_ws and env_ws != (anchor_ws or ""):
        legacy_token = _legacy_lane_token(repo_root)
        if legacy_token and env_ws == legacy_token:
            anchor_ws = legacy_token

    sender_res = resolve_sender_identity(os.environ, anchor_workspace_id=anchor_ws)
    if not sender_res.ok or sender_res.identity is None:
        # Redmine #13397 finding 2 (design consultation answer j#73755, Option B):
        # a herdr send needs an attested launch-time lane-sender identity
        # (`MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID`), which an
        # env-less operator terminal does not carry. That terminal is NOT a sanctioned
        # lane-dispatch origin (admitting it would bypass the workspace/lane scope +
        # coordinator-binding attestation this rail exists to enforce). The historical
        # message read as a tmux-era `target_unavailable`; name the herdr-native cause
        # and point to the one sanctioned route so an operator is not left guessing.
        route_hint = (
            " Dispatch lanes through the coordinator agent (coordinator -> "
            "target-lane Codex gateway -> same-lane Claude worker), or run this send "
            "from an attested lane agent pane. An env-less operator shell is not a "
            "lane-dispatch origin; see vibes/docs/specs/herdr-native-identity.md."
        )
        if sender_res.reason == REASON_MISSING_SENDER_ENV:
            raise HerdrSendEntryError(
                "herdr backend selected but this shell carries no attested lane-sender "
                f"identity ({MOZYO_WORKSPACE_ID_ENV} / MOZYO_AGENT_ROLE unset): "
                f"{sender_res.detail}." + route_hint,
                reason=sender_res.reason,
            )
        raise HerdrSendEntryError(
            "herdr backend selected but the sender identity is not attested "
            f"(reason={sender_res.reason}): {sender_res.detail}." + route_hint,
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

    # Cross-workspace explicit dispatch (#13331, re-scoped by #13377): under the shared
    # project workspace model a lane worktree resolves to the sender's OWN workspace, so
    # a coordinator→lane-gateway dispatch flows through the same-workspace path below
    # with an explicit `--target-lane` (j#73613). This branch now fires only when
    # `--target-repo <path>` names a genuinely different repo (a cross-project send):
    # the receiver's slot is resolved in THAT workspace (still the one backend-neutral
    # route authority; the live locator stays transient cache). A `--target-repo` that
    # resolves to the sender's own workspace (or `auto` / unset) falls through to the
    # same-workspace path unchanged.
    target_workspace_id = _explicit_target_workspace_id(target_repo)
    cross_workspace = bool(target_workspace_id) and target_workspace_id != sender.workspace_id
    explicit_lane = target_lane
    # Redmine #13476 (Design Consultation Answer j#74599, Option A): the sublane->parent
    # coordinator callback keeps the backend-neutral documented form
    # `--to codex --target coordinator`. `--target coordinator` is a semantic pseudo-target
    # (the same `coordinator` route identity the tmux pane resolver consumes as
    # `COORDINATOR_LABEL`), NOT a live herdr locator, so the herdr rail translates it here
    # into the coordinator route authority instead of routing by the `--to` receiver.
    # Resolving with the `coordinator` receiver makes `resolve_target_role` bind the
    # configured coordinator provider and `derive_target_lane` derive the workspace DEFAULT
    # lane (tier 3 — the parent coordinator), NOT the sublane sender's own lane (the tier-2
    # same-lane a bare `--to codex` derives, Review j#74511 Finding 1). An explicit
    # `--target-lane` still wins (tier-1 explicit lane in `derive_target_lane`), so an
    # intentional lane override is never ignored. The outward `receiver` (marker `to=codex`
    # / the `binds_receiver` process gate) is unchanged — the coordinator IS a codex, so the
    # role the route resolves (coordinator provider) matches the `--to codex` binding, and
    # `--to` public choices stay `claude|codex` (this is an internal, semantic translation).
    norm_target = _norm(target)
    route_receiver = (
        RECEIVER_COORDINATOR
        if norm_target == RECEIVER_COORDINATOR
        else receiver
    )
    if cross_workspace:
        resolution = resolve_herdr_cross_workspace_target(
            route_receiver,
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
        # (Redmine #13377) is the explicit-lane field a coordinator→lane-gateway
        # dispatch passes under the shared project workspace model.
        resolution = resolve_herdr_route_target(
            route_receiver,
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
    # Redmine #13884 (repro anchors #13882 j#79958 / #13883 j#79959): an explicit concrete
    # `--target` (a live herdr locator / assigned name — NOT the `coordinator` pseudo-target,
    # NOT a bare provider token) asserts WHICH agent the send must reach. The #13305 route
    # authority resolves the target from `--to` + `--target-lane` + `--target-repo` and
    # treats the locator as transient cache, never the routing key — correct by design (a
    # locator is not stable identity), and the exact reason a lane-pinned worker dispatch
    # (#13485/#13488) passes `--target <worker-locator>` alongside `--target-lane <lane>`
    # yet still resolves the stable slot. The defect was the SILENT drop: a `--target` that
    # named a DIFFERENT agent than the derived route (a coordinator's cross-lane
    # `--target <lane-gateway-locator>` with no `--target-lane`, which derived the sender's
    # OWN default lane and resolved the coordinator's own pane) was dropped, the send landed
    # on that wrong agent, and it reported a false-positive `sent` (a sender echo). Cross-check
    # the explicit target against the resolved identity: a `--target` that agrees with the
    # derived route (same live locator or same durable assigned name) passes through unchanged
    # (resolve-to-exact); a MISMATCH fails closed with a typed zero-send reason instead of a
    # coordinator / sender-lane fallback (the `orchestrate_handoff` herdr branch projects this
    # raise onto a `blocked` / `target_unavailable` outcome, `target=None`, no injection). The
    # locator stays evidence, never the authority — the pin (`--target-lane`), not the
    # locator, is what resolves the intended target; this guard only refuses to send when the
    # named target and the resolved target disagree.
    if norm_target and norm_target != RECEIVER_COORDINATOR and norm_target not in AGENT_PROVIDERS:
        if norm_target not in (_norm(resolution.locator), _norm(resolution.assigned_name)):
            raise HerdrExplicitTargetMismatchError(
                f"herdr send named an explicit --target {target!r} but the route authority "
                f"resolved a different agent (live locator {resolution.locator!r}, name "
                f"{resolution.assigned_name!r}) from --to={receiver!r} + --target-lane="
                f"{(_norm(explicit_lane) or None)!r} + --target-repo (lane basis "
                f"{resolution.lane_basis!r}). The named target and the derived route "
                "disagree; refusing to send to the derived target, which would echo the "
                "send onto the sender's own lane and report a false-positive `sent` "
                "(Redmine #13884). Pin the intended lane with --target-lane <lane> (or use "
                "--target coordinator) so the route authority resolves the target you named."
            )
    # The synthesized target record's `cwd` is the TARGET agent's repo root (the tmux path
    # reads the target pane's own cwd). Three shapes (#13331 / #13377 j#73640 finding 1):
    #
    # - a #13331 cross-workspace dispatch names the target repo explicitly, so `cwd` is
    #   the expanded `--target-repo` and the downstream `target_repo_mismatch` gate
    #   compares like-for-like (observed target cwd vs the explicit `--target-repo`);
    # - a #13377 shared-model explicit-lane dispatch (`--target-lane` + an explicit
    #   non-auto `--target-repo <lane worktree>`) resolves in the sender's OWN workspace,
    #   but the resolved lane slot's launch cwd IS the lane worktree (`prepare_session
    #   --cwd`), not the sender's repo — synthesizing the sender root here made the repo
    #   gate structurally fail (`expected` = lane worktree vs `observed` = main repo).
    #   The explicit pair rides the same like-for-like precedent as cross-workspace;
    # - every other same-workspace send (no explicit lane) keeps `cwd` = the sender's
    #   repo root, so the repo gate's conservatism for implicit sends is unchanged.
    raw_target_repo = target_repo
    explicit_target_repo = bool(raw_target_repo) and raw_target_repo != AUTO_TARGET_REPO
    if cross_workspace or (explicit_target_repo and _norm(explicit_lane)):
        target_cwd = str(Path(raw_target_repo).expanduser())
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
    "HerdrExplicitTargetMismatchError",
    "EXPLICIT_TARGET_MISMATCH_OUTCOME_REASON",
    "explicit_tmux_pane_target",
    "herdr_backend_selected",
    "herdr_effective_backend_selected",
    "resolve_herdr_send_target",
)
