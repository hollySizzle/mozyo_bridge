"""Read-only projection helpers for ``sublane prepare-bound-pair`` (Redmine #13933).

Extracted from the live adapter so that composition module stays inside the module-health
gate (Redmine #13933 R13, review j#82079): none of these carry a destructive effect. They
are the request adapter, the action-closed-provider fold, and the a14 rollback-owed-partial
detection that the read-only preflight uses to hand the operator the exact public rollback
``--action-id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    BINDING_RESERVED,
    HerdrIdentityReplacementBindingStore,
)
from mozyo_bridge.core.state.lane_lifecycle import norm
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_ROLLBACK_OWED,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
    list_herdr_agent_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (
    LiveSublaneQuarantineOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (
    PrepareBoundPairRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    ConvergeBoundPairRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    BoundSlot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm_lane as norm_lane,
    decode_assigned_name,
    encode_assigned_name,
)


def convergence_request(request: PrepareBoundPairRequest) -> ConvergeBoundPairRequest:
    return ConvergeBoundPairRequest(
        issue=request.issue,
        journal=request.journal,
        lane=request.lane,
        worktree=request.worktree,
        branch=request.branch,
    )


def action_closed_providers(slots: Sequence[BoundSlot]) -> tuple[str, ...]:
    """Provider roles whose live absence is proven by this action's own immutable close.

    ``close_proven`` is set only where the slot has no live row AND the transaction pinned by
    the approved action id carries that participant past ``close_owed``.
    """
    return tuple(slot.provider for slot in slots if slot.close_proven)


def _exact_normal_v1_row(home, intent, live_locator: str) -> bool:
    """True iff the live locator carries an exact, unbound normal-v1 attestation row."""
    try:
        record = HerdrIdentityAttestationStore(home=home).read(intent.assigned_name)
    except Exception:  # noqa: BLE001 - unreadable is not a match
        return False
    join = evaluate_attestation(
        record,
        live_locator=live_locator,
        expected_workspace_id=intent.workspace_id,
        expected_role=intent.role,
        expected_lane=intent.lane_id,
    )
    return (
        bool(join.ok)
        and record is not None
        and not norm(getattr(record, "replacement_action_id", ""))
    )


def resolve_rollback_owed_startup_action(
    *, repo_root, env, workspace: str, lane: str, action_id: str
) -> str:
    """The inner startup action id of an exact rollback-owed fresh launch this action owns.

    The installed a14 partial (Redmine #13933 R13 F1, review j#82079): a prepare action whose
    embedded session-start already put a live fresh slot in place yet left ITS startup
    transaction ``rollback_owed``. The read-only preflight must both name that state and hand
    the operator the exact ``--action-id`` the public startup rollback rail needs; without it
    the rollback -> replay recovery chain has no entry point. Returns "" unless every conjunct
    matches (the same ones the v1 bind requires: an un-closed participant receipt at the exact
    live locator, a clean normal-v1 attestation row, the exact startup unit). The side binding
    keyed by ``(action_id, assigned_name)`` is the proof this action owns the slot. Purely
    read-only and fail-soft: any unreadable store yields "".
    """
    action_id = norm(action_id)
    workspace = norm(workspace)
    if not action_id or not workspace:
        return ""
    try:
        providers = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
        managed_pair = tuple(providers)
        rows = tuple(list_herdr_agent_rows(env))
        home = mozyo_bridge_home()
        binding_store = HerdrIdentityReplacementBindingStore(home=home)
        fence = StartupTransactionFence(home=home)
        for provider in providers:
            assigned_name = encode_assigned_name(workspace, provider, lane)
            intent = binding_store.read(action_id, assigned_name)
            if intent is None:
                continue
            # Mirror the authoritative bind path's exact identity + unit join
            # (herdr_session_start_v1_replacement_binding._binding_identity_matches, review
            # j#82084 F1): the stored binding is trusted only when its immutable identity is
            # this exact unit AND its startup id re-derives from that unit + the reserved
            # nonce. A binding whose stored workspace/role/lane, or whose startup unit, belongs
            # to another generation must never lend its startup id to a rollback aimed here.
            if (
                intent.phase != BINDING_RESERVED
                or norm(intent.workspace_id) != workspace
                or norm(intent.role) != norm(provider)
                or norm(intent.lane_id) != norm(lane)
                or norm(intent.assigned_name) != norm(assigned_name)
            ):
                continue
            try:
                expected_startup = startup_action_id(
                    StartupUnit(workspace, lane, managed_pair), intent.startup_nonce
                )
            except ValueError:
                continue
            if norm(intent.startup_action_id) != expected_startup:
                continue
            named = [
                row
                for row in rows
                if norm(row.get(AGENT_KEY_NAME)) == norm(assigned_name)
            ]
            if len(named) != 1:
                continue
            live_locator = _agent_locator(named[0])
            if not live_locator or live_locator == norm(intent.old_locator):
                continue
            startup = fence.read(expected_startup)
            if (
                startup is None
                or startup.phase != PHASE_ROLLBACK_OWED
                or startup.action_id != expected_startup
                or norm(startup.unit.workspace_id) != workspace
                or norm(startup.unit.lane_id) != norm(lane)
                or tuple(startup.unit.providers) != tuple(sorted(set(managed_pair)))
            ):
                continue
            owed = startup.participant_for(intent.role)
            if (
                owed is None
                or owed.closed
                or owed.assigned_name != intent.assigned_name
                or owed.locator != live_locator
                or not owed.receipt
            ):
                continue
            if not _exact_normal_v1_row(home, intent, live_locator):
                continue
            return intent.startup_action_id
    except Exception:  # noqa: BLE001 - a read-only projection is fail-soft
        return ""
    return ""


@dataclass
class SnapshotQuarantineOps(LiveSublaneQuarantineOps):
    """Run the pending-composer classifier against one already-read inventory."""

    snapshot_rows: Sequence[Mapping[str, object]] = ()
    #: Provider roles whose live row is absent *because this immutable action closed it*
    #: (Redmine #13933 j#80934).  Only a caller holding that transaction proof may set this.
    action_closed_roles: tuple[str, ...] = ()

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return self.snapshot_rows

    def _pair_ok(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        workspace_id: str,
        lane: str,
    ) -> bool:
        """Admit the sibling absence this action itself caused, and nothing else.

        The inherited pair fence requires BOTH provider rows live and co-placed.  A
        ``prepare-bound-pair`` run that closed one role and then failed to relaunch destroys
        that premise with its own effect, so the still-owed role reads as
        ``generation_mismatch`` and the transaction can never be replayed (j#80934).  The
        absence is re-admitted only for a role whose close THIS transaction proves, and only
        while that role stays absent — a reappeared or foreign sibling falls back to the
        inherited fence, which is the authority for every live row.
        """
        if super()._pair_ok(rows, workspace_id=workspace_id, lane=lane):
            return True
        if not self.action_closed_roles:
            return False
        try:
            providers = set(self._providers())
        except WorkflowProviderUnresolved:
            return False
        closed = {norm(role) for role in self.action_closed_roles}
        # A proper subset: a wholly action-closed pair has no live composer left to classify.
        if not closed < providers:
            return False
        live: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decoded.ok or decoded.identity is None:
                continue
            identity = decoded.identity
            if (
                identity.workspace_id == workspace_id
                and norm_lane(identity.lane_id) == norm_lane(lane)
                and identity.role in providers
            ):
                live.append(identity.role)
        # Every role this action closed must still be gone (a reappeared sibling is a live row
        # the inherited fence owns), and every other role of the pair must still be live and
        # unique.  Identity / revision / cwd of the classified row remain the caller's fences.
        return (
            len(live) == len(set(live))
            and not (closed & set(live))
            and (providers - closed) <= set(live)
        )


__all__ = (
    "SnapshotQuarantineOps",
    "action_closed_providers",
    "convergence_request",
    "resolve_rollback_owed_startup_action",
)
