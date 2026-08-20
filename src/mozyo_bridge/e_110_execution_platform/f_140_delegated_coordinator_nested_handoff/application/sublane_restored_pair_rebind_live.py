"""Live authority join for the restored-pair lifecycle pin rebind rail (#15656).

The fail-closed observation + CAS write behind ``sublane rebind-restored-pair``.
Reuses the established authorities rather than inventing new ones:

- lifecycle row / owner binding: :class:`...lane_lifecycle.LaneLifecycleStore`
  (read) and :class:`...lane_recovered_pair_pin_reconcile
  .LaneRecoveredPairPinReconcileStore` (the EXISTING bounded declared-slots CAS
  — no new raw SQL);
- worktree binding: :func:`...sublane_adopt_declaration.declared_worktree_identity`
  (the one canonical token derivation every declaration writer uses);
- expected providers: :func:`...workflow_provider_resolution
  .resolve_gateway_provider` / ``resolve_worker_provider``;
- live inventory: :func:`...sublane_herdr_projection.list_herdr_agent_rows`;
- startup self-attestation: :func:`...herdr_identity_attestation
  .evaluate_attestation` generation-bound to the LIVE locator (the adopt gate).

Every gate failure is zero-write with a typed reason; the write replaces ONLY
``declared_slots`` (+ revision / updated_at) and never ``lane_generation`` —
the restored processes are the same agent-session incarnation, so existing
dispatch-marker anchors must stay valid.

#15769 extends this rail with the WRITE-SIDE launch-generation re-attest (design
decision j#108766; measured deadlock #15631 j#108621/j#108741, #15693 j#108747):
after a Herdr/tmux server loss the restored slot's server-owned ``terminal_id``
(and possibly its pane locator) is NEW while the launch-generation row still
records the launch-time values, so the read-side ``verified_generation_token``
— deliberately byte-unchanged — refuses every governed ``handoff send`` as
``target_unavailable`` forever. When the identity join holds on SERVER-OWNED
inventory facts only (unique live named slot, SLOT_LIVE, provider stamps, the
mzb1 name decoding to the generation row's exact workspace/role/lane, a unique
canonical live terminal), the rail CAS-updates the generation row's
``terminal_id`` / ``locator`` to the live values and records the old -> new
lineage durably in the structured outcome (``reattest_lineage``). A caller can
never supply the identity or terminal values that drive the CAS.

Design point (#15769 item 3, decided from the code): when the LOCATOR moved,
``completed_generation_startup_token``'s ``participant.locator ==
generation.locator`` conjunct would fail forever after the generation CAS, and
that verifier must not be widened. The smallest sound change is a
participant-side locator re-pin on the startup-transaction fence
(:meth:`...startup_transaction_fence.StartupTransactionFence
.repin_restored_participant_locator`): field-scoped (locator only — the
launch-time receipt stays byte-identical), CAS-guarded on the exact old
locator, and admitted only for ``completed_success`` / ``rollback_owed``
actions. The alternative — comparing acceptance against the generation row only
— would widen the read-side verifier for every existing consumer, exactly what
the L1 decision rejected. Write order is retry-safe: participant re-pin, then
the generation CAS, then the declared-pin reconcile; a partial failure leaves
every read-side join fail-closed and a re-run re-observes and completes the
remaining steps.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_ABSENT,
    ATTEST_STALE,
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_ROLLBACK_OWED,
    StartupTransactionFence,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    RELEASE_NOT_REQUESTED,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ProcessPinError,
    replacement_settled,
    stored_binding_kind_is,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.core.state.lane_recovered_pair_pin_reconcile import (
    LaneRecoveredPairPinReconcileStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    declared_worktree_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    probe_worktree_resolved,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind import (  # noqa: E501
    SublaneRestoredPairRebindUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    REBIND_BLOCK_AMBIGUOUS_LOCATORS,
    REBIND_BLOCK_BINDING_NOT_ISSUE,
    REBIND_BLOCK_BRANCH_DRIFTED,
    REBIND_BLOCK_DECISION_ANCHOR_UNUSABLE,
    REBIND_BLOCK_DECLARED_SLOTS_UNRESOLVED,
    REBIND_BLOCK_INVENTORY_UNREADABLE,
    REBIND_BLOCK_ISSUE_MISMATCH,
    REBIND_BLOCK_LIFECYCLE_UNREADABLE,
    REBIND_BLOCK_NOT_ACTIVE,
    REBIND_BLOCK_PROVIDER_UNRESOLVED,
    REBIND_BLOCK_RELEASE_OPEN,
    REBIND_BLOCK_REPLACEMENT_OPEN,
    REBIND_BLOCK_ROW_ABSENT,
    REBIND_BLOCK_WORKSPACE_UNRESOLVED,
    REBIND_BLOCK_WORKTREE_IDENTITY_MISMATCH,
    REBIND_BLOCK_WORKTREE_UNBOUND,
    REBIND_BLOCK_WORKTREE_UNREADABLE,
    REBIND_BLOCK_WORKTREE_UNRESOLVED,
    REBIND_SLOT_DECLARED_STILL_LIVE,
    REBIND_SLOT_DUPLICATE_LIVE,
    REBIND_SLOT_GENERATION_UNREADABLE,
    REBIND_SLOT_LIVE_ABSENT,
    REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED,
    REBIND_SLOT_LIVE_LOCATOR_UNRESOLVED,
    REBIND_SLOT_LIVE_PROVIDER_MISMATCH,
    REBIND_SLOT_MISSING_LIVE,
    REBIND_SLOT_NOT_DRIFTED,
    REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED,
    REBIND_SLOT_PROVIDER_MISMATCH,
    REBIND_SLOT_STALE,
    REBIND_SLOT_TERMINAL_UNCHANGED,
    REBIND_SLOT_UNATTESTED,
    RebindSlotPlan,
    RestoredPairRebindPlan,
    RestoredPairRebindRequest,
    slot_reason,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    terminal_identity_of_live_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_LIVE,
    classify_named_slot,
)

_PIN_ROLE_GATEWAY = "gateway"
_PIN_ROLE_WORKER = "worker"

#: ``RebindSlotPlan.generation_state`` values (#15769): the attested generation
#: row already binds the live terminal + locator / needs the re-attest CAS.
_GEN_LIVE_BOUND = "live_bound"
_GEN_REATTEST_NEEDED = "reattest_needed"


@dataclass(frozen=True)
class _SlotReattestPlan:
    """One slot's authorized launch-generation re-attest write (#15769).

    Every value is re-derived from the action-time observation (never carried
    from a caller): the CAS keys are the exact old row values and the new
    values are the server-owned live inventory facts. ``participant_repin``
    marks whether the startup-transaction participant's locator must be
    CAS-moved alongside (only when the locator itself moved and the participant
    still records the old one).
    """

    slot_role: str
    provider: str
    assigned_name: str
    startup_action_id: str
    workspace_id: str
    lane_id: str
    verdict: str
    old_locator: str
    old_terminal_id: str
    new_locator: str
    new_terminal_id: str
    participant_repin: bool
    evidence: tuple[str, ...]

    def lineage_payload(self) -> dict:
        """The journal-ready durable lineage record (#15769 acceptance)."""
        return {
            "redmine": "#15769",
            "slot_role": self.slot_role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "startup_action_id": self.startup_action_id,
            "old_terminal_id": self.old_terminal_id,
            "new_terminal_id": self.new_terminal_id,
            "old_locator": self.old_locator,
            "new_locator": self.new_locator,
            "participant_locator_repin": self.participant_repin,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class _SlotResult:
    """One declared slot's whole observation (plan + write inputs)."""

    plan: RebindSlotPlan
    pin: Optional[ProcessGenerationPin]
    reasons: list
    reattest: Optional[_SlotReattestPlan] = None
    skipped: bool = False


@dataclass(frozen=True)
class _RebindContext:
    """The single-observation join the preflight AND the write both derive from."""

    plan: RestoredPairRebindPlan
    key: Optional[LaneLifecycleKey] = None
    expected_revision: int = 0
    expected_generation: int = 0
    worktree_identity: str = ""
    decision: Optional[DecisionPointer] = None
    old_slots: tuple[ProcessGenerationPin, ...] = ()
    new_slots: tuple[ProcessGenerationPin, ...] = ()
    slot_reattests: tuple[_SlotReattestPlan, ...] = ()
    pins_changed: bool = False


@dataclass
class LiveRestoredPairRebindOps:
    """Live fail-closed observation + bounded CAS write (test seams are methods)."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    lifecycle_home: Optional[Path] = None
    attestation_home: Optional[Path] = None

    # -- overridable authority seams (fakes subclass these; the join stays real) --

    def _resolve_root(self) -> Optional[Path]:
        try:
            root = self.repo_root.expanduser().resolve(strict=True)
        except OSError:
            return None
        return root if root.is_dir() else None

    def _workspace_id(self, root: Path) -> str:
        return _norm(repo_scope_workspace_id(root))

    def _lifecycle_record(self, workspace_id: str, lane: str):
        return LaneLifecycleStore(home=self.lifecycle_home).get(
            LaneLifecycleKey(workspace_id, lane)
        )

    def _worktree_identity(self, root: Path, lane: str) -> Optional[str]:
        return declared_worktree_identity(str(root), lane)

    def _worktree_readable(self, root: Path) -> bool:
        try:
            return probe_worktree_resolved(str(root)) is True
        except Exception:  # noqa: BLE001 - unreadable worktree authority fails closed
            return False

    def _branch(self, root: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                capture_output=True,
            )
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _providers(self, root: Path) -> tuple[str, str]:
        return (
            _norm(resolve_gateway_provider(str(root))),
            _norm(resolve_worker_provider(str(root))),
        )

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _read_attestation(self, assigned_name: str):
        return HerdrIdentityAttestationStore(home=self.attestation_home).read(
            assigned_name
        )

    def _read_generation(self, assigned_name: str):
        """The current launch-generation row for this slot (raises when unreadable)."""
        return HerdrLaunchGenerationStore(home=self.attestation_home).read(
            assigned_name
        )

    def _read_startup_action(self, action_id: str):
        """The startup-transaction action a generation token names (raises on damage)."""
        return StartupTransactionFence(home=self.attestation_home).read(action_id)

    def _repin_participant(self, plan: _SlotReattestPlan) -> None:
        StartupTransactionFence(
            home=self.attestation_home
        ).repin_restored_participant_locator(
            plan.startup_action_id,
            plan.provider,
            assigned_name=plan.assigned_name,
            expected_locator=plan.old_locator,
            new_locator=plan.new_locator,
        )

    def _reattest_generation(self, plan: _SlotReattestPlan) -> None:
        HerdrLaunchGenerationStore(
            home=self.attestation_home
        ).reattest_restored_terminal(
            assigned_name=plan.assigned_name,
            startup_action_id=plan.startup_action_id,
            workspace_id=plan.workspace_id,
            role=plan.provider,
            lane_id=plan.lane_id,
            verdict=plan.verdict,
            expected_locator=plan.old_locator,
            expected_terminal_id=plan.old_terminal_id,
            live_locator=plan.new_locator,
            live_terminal_id=plan.new_terminal_id,
        )

    # -- per-slot gate --------------------------------------------------------

    def _generation_join(
        self,
        *,
        assigned: str,
        want_provider: str,
        slot_role: str,
        workspace_id: str,
        lane: str,
        live_locator: str,
        live_terminal_id,
        reasons: list,
    ):
        """The #15769 launch-generation join -> ``(generation, gen_state)``.

        ``gen_state`` is ``""`` (no usable attested row — the pre-#15769 rail
        shape), ``_GEN_LIVE_BOUND``, or ``_GEN_REATTEST_NEEDED``. Every identity
        conjunct is a SERVER-OWNED fact or the durable row itself; a foreign /
        ambiguous / undecodable join appends a typed refusal and never guesses.
        """
        try:
            generation = self._read_generation(assigned)
        except Exception:  # noqa: BLE001 - an unreadable authority is never "absent"
            reasons.append(slot_reason(REBIND_SLOT_GENERATION_UNREADABLE, slot_role))
            return None, ""
        if generation is None:
            return None, ""
        token = _norm(getattr(generation, "startup_action_id", ""))
        if (
            _norm(getattr(generation, "phase", "")) != GENERATION_ATTESTED
            or not token
            or _norm(getattr(generation, "verdict", "")) != VERDICT_PRESENT
        ):
            # A pending / superseded / non-present row is launch-rail property;
            # it is never re-attested and lends no drift evidence here.
            return None, ""
        decoded = decode_assigned_name(assigned)
        stamps_match = bool(
            decoded.ok
            and decoded.identity is not None
            and _norm(decoded.identity.workspace_id)
            == _norm(generation.workspace_id)
            and _norm(decoded.identity.role) == _norm(generation.role)
            and _norm_lane(decoded.identity.lane_id)
            == _norm_lane(generation.lane_id)
        )
        row_matches_slot = bool(
            _norm(generation.assigned_name) == assigned
            and _norm(generation.workspace_id) == _norm(workspace_id)
            and _norm(generation.role) == want_provider
            and _norm_lane(generation.lane_id) == _norm_lane(lane)
        )
        if not (stamps_match and row_matches_slot):
            # The server-owned name stamp does not decode to the generation
            # row's identity, or the row is foreign to this slot: never
            # re-attested (#15769 security invariant — foreign rejection).
            reasons.append(
                slot_reason(REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED, slot_role)
            )
            return generation, ""
        if live_terminal_id is None:
            # No unique canonical server-owned terminal for this name+locator
            # (duplicate claims / malformed snapshot): the join has no answer.
            reasons.append(
                slot_reason(REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED, slot_role)
            )
            return generation, ""
        if (
            generation.terminal_id == live_terminal_id
            and _norm(generation.locator) == live_locator
        ):
            return generation, _GEN_LIVE_BOUND
        return generation, _GEN_REATTEST_NEEDED

    def _participant_repin_needed(
        self, *, generation, want_provider: str, assigned: str, live_locator: str
    ):
        """Whether the fence participant needs the locator re-pin -> bool or ``None``.

        ``None`` means the participant-side join could not be established
        exactly (unreadable fence, non-terminal-acceptable phase, closed /
        foreign / already-diverged participant) — the caller refuses typed.
        """
        try:
            action = self._read_startup_action(
                _norm(generation.startup_action_id)
            )
        except Exception:  # noqa: BLE001 - an unreadable fence is never proof
            return None
        if action is None or _norm(getattr(action, "phase", "")) not in (
            PHASE_COMPLETED_SUCCESS,
            PHASE_ROLLBACK_OWED,
        ):
            return None
        unit = getattr(action, "unit", None)
        if not (
            unit is not None
            and _norm(getattr(unit, "workspace_id", ""))
            == _norm(generation.workspace_id)
            and _norm_lane(getattr(unit, "lane_id", ""))
            == _norm_lane(generation.lane_id)
            and want_provider in tuple(getattr(unit, "providers", ()) or ())
        ):
            return None
        participant = action.participant_for(want_provider)
        if (
            participant is None
            or getattr(participant, "closed", True)
            or _norm(getattr(participant, "assigned_name", "")) != assigned
        ):
            return None
        participant_locator = _norm(getattr(participant, "locator", ""))
        if participant_locator == live_locator:
            return False  # an earlier partial run already re-pinned it
        if participant_locator == _norm(generation.locator):
            return True
        return None

    def _slot(
        self,
        *,
        declared: ProcessGenerationPin,
        expected_provider: str,
        slot_role: str,
        rows: Sequence[Mapping[str, object]],
        workspace_id: str,
        lane: str,
        allow_missing: bool = False,
    ) -> _SlotResult:
        reasons: list[str] = []
        assigned = _norm(declared.assigned_name)
        declared_locator = _norm(declared.locator)
        want_provider = _norm(expected_provider)
        if _norm(declared.provider) != want_provider:
            # The declared pin does not bind this slot to the resolved workflow
            # provider — a swapped / foreign binding is never "the same session".
            reasons.append(slot_reason(REBIND_SLOT_PROVIDER_MISMATCH, slot_role))

        named = [
            row
            for row in rows
            if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == assigned
        ]
        row: Optional[Mapping[str, object]] = None
        live_locator = ""
        live_revision = ""
        attestation = None
        attestation_state = ATTEST_ABSENT
        generation = None
        gen_state = ""
        attestation_restore_stale = False
        if len(named) > 1:
            # A duplicate assigned name is a herdr name-uniqueness violation this
            # rail never guesses past (the adopt-gate discipline).
            reasons.append(slot_reason(REBIND_SLOT_DUPLICATE_LIVE, slot_role))
        elif not named:
            if allow_missing and not reasons:
                # #15769 single-slot mode: the pair's other slot may still be
                # re-pinned / re-attested; this slot is a typed, separate fact
                # and its declared pin stays byte-unchanged.
                plan = RebindSlotPlan(
                    slot_role=slot_role,
                    provider=_norm(declared.provider),
                    assigned_name=assigned,
                    declared_locator=declared_locator,
                    ready=False,
                    reason=slot_reason(REBIND_SLOT_MISSING_LIVE, slot_role),
                    skipped=True,
                )
                return _SlotResult(plan, declared, [], skipped=True)
            reasons.append(slot_reason(REBIND_SLOT_LIVE_ABSENT, slot_role))
        else:
            row = named[0]
            if classify_named_slot(row) != SLOT_LIVE:
                # A positively-signalled shell residue (blank detected-agent
                # field, or an unknown runtime status with no detected agent) is
                # never rebind evidence, even when the locator / terminal
                # identity and the stored attestation survived the restore
                # around the dead shell. Liveness is a required conjunct
                # INDEPENDENT of the attestation join — the same
                # `classify_named_slot` gate the live adopt applies (#15656
                # review j#107780 finding_1).
                reasons.append(slot_reason(REBIND_SLOT_STALE, slot_role))
            live_locator = _norm(_agent_locator(row))
            live_revision = _norm(row.get("runtime_revision"))
            if not live_locator:
                reasons.append(
                    slot_reason(REBIND_SLOT_LIVE_LOCATOR_UNRESOLVED, slot_role)
                )
            else:
                live_row_provider = _norm(row.get("provider"))
                live_detected_agent = _norm(row.get("agent"))
                if (live_row_provider and live_row_provider != want_provider) or (
                    live_detected_agent and live_detected_agent != want_provider
                ):
                    reasons.append(
                        slot_reason(REBIND_SLOT_LIVE_PROVIDER_MISMATCH, slot_role)
                    )
                live_terminal_id = terminal_identity_of_live_slot(
                    assigned, live_locator, rows
                )
                generation, gen_state = self._generation_join(
                    assigned=assigned,
                    want_provider=want_provider,
                    slot_role=slot_role,
                    workspace_id=workspace_id,
                    lane=lane,
                    live_locator=live_locator,
                    live_terminal_id=live_terminal_id,
                    reasons=reasons,
                )
                if live_locator == declared_locator:
                    if gen_state == _GEN_REATTEST_NEEDED:
                        # #15769: the pane survived on its own locator but the
                        # server-owned terminal identity is new — the row's
                        # re-attest IS the evidence to act on.
                        pass
                    elif gen_state == _GEN_LIVE_BOUND:
                        # Nothing is stale on any axis: typed no-op.
                        reasons.append(
                            slot_reason(REBIND_SLOT_TERMINAL_UNCHANGED, slot_role)
                        )
                    else:
                        # Nothing drifted: the declared pin already IS the live
                        # generation, so a rebind has no evidence to act on.
                        reasons.append(
                            slot_reason(REBIND_SLOT_NOT_DRIFTED, slot_role)
                        )
                elif any(
                    isinstance(other, Mapping)
                    and _norm(_agent_locator(other)) == declared_locator
                    for other in rows
                ):
                    # The old locator is still a live slot: this is NOT the
                    # restore-moved-the-pair shape; refusing keeps the rail from
                    # legitimizing a second same-name process.
                    reasons.append(
                        slot_reason(REBIND_SLOT_DECLARED_STILL_LIVE, slot_role)
                    )
                attested = False
                try:
                    attestation = self._read_attestation(assigned)
                    join = evaluate_attestation(
                        attestation,
                        live_locator=live_locator,
                        live_terminal_id=live_terminal_id,
                        expected_workspace_id=workspace_id,
                        expected_role=want_provider,
                        expected_lane=lane,
                    )
                    attestation_state = join.state
                    attested = bool(
                        join.ok
                        and _norm(getattr(attestation, "assigned_name", ""))
                        == assigned
                    )
                    # #15769: the restore signature on the attestation store —
                    # the recorded identity matched (a conflict precedes stale
                    # in `evaluate_attestation`), the agent's own boot verdict
                    # was `present`, and ONLY the locator / terminal generation
                    # pin drifted. Accepted solely when the launch-generation
                    # row independently binds (or is being re-attested to) the
                    # live slot; foreign / missing / conflicting records stay
                    # refused exactly as before.
                    attestation_restore_stale = bool(
                        join.state == ATTEST_STALE
                        and attestation is not None
                        and _norm(getattr(attestation, "verdict", ""))
                        == VERDICT_PRESENT
                        and _norm(getattr(attestation, "assigned_name", ""))
                        == assigned
                    )
                except Exception:  # noqa: BLE001 - unreadable store is never proof
                    attested = False
                    attestation_restore_stale = False
                if not (
                    attested
                    or (
                        attestation_restore_stale
                        and gen_state in (_GEN_REATTEST_NEEDED, _GEN_LIVE_BOUND)
                    )
                ):
                    reasons.append(slot_reason(REBIND_SLOT_UNATTESTED, slot_role))

        reattest: Optional[_SlotReattestPlan] = None
        if not reasons and gen_state == _GEN_REATTEST_NEEDED:
            assert generation is not None
            old_locator = _norm(generation.locator)
            locator_moved = old_locator != live_locator
            participant_repin = False
            if locator_moved:
                needed = self._participant_repin_needed(
                    generation=generation,
                    want_provider=want_provider,
                    assigned=assigned,
                    live_locator=live_locator,
                )
                if needed is None:
                    reasons.append(
                        slot_reason(
                            REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED, slot_role
                        )
                    )
                else:
                    participant_repin = needed
            if not reasons:
                reattest = _SlotReattestPlan(
                    slot_role=slot_role,
                    provider=want_provider,
                    assigned_name=assigned,
                    startup_action_id=_norm(generation.startup_action_id),
                    workspace_id=_norm(generation.workspace_id),
                    lane_id=_norm(generation.lane_id),
                    verdict=_norm(generation.verdict),
                    old_locator=old_locator,
                    old_terminal_id=generation.terminal_id,
                    new_locator=live_locator,
                    new_terminal_id=live_terminal_id,
                    participant_repin=participant_repin,
                    evidence=(
                        "unique_live_named_slot",
                        "slot_live",
                        "live_provider_stamp_match",
                        "assigned_name_decodes_generation_identity",
                        "generation_identity_matches_slot",
                        "unique_live_terminal_identity",
                        (
                            "attestation_ok_live_join"
                            if attested
                            else "attestation_restore_stale_present"
                        ),
                    ),
                )

        pin: Optional[ProcessGenerationPin] = None
        if not reasons:
            try:
                pin = ProcessGenerationPin(
                    role=declared.role,
                    provider=want_provider,
                    assigned_name=assigned,
                    locator=live_locator,
                    runtime_revision=live_revision,
                    attested_at=_norm(getattr(attestation, "observed_at", "")),
                )
            except ProcessPinError:
                reasons.append(slot_reason(REBIND_SLOT_LIVE_ABSENT, slot_role))
        slot_plan = RebindSlotPlan(
            slot_role=slot_role,
            provider=_norm(declared.provider),
            assigned_name=assigned,
            declared_locator=declared_locator,
            live_locator=live_locator,
            live_runtime_revision=live_revision,
            attestation_state=attestation_state,
            ready=not reasons,
            reason=",".join(reasons),
            generation_state=gen_state,
        )
        return _SlotResult(slot_plan, pin, reasons, reattest=reattest)

    # -- the single observation join ------------------------------------------

    def _context(self, request: RestoredPairRebindRequest) -> _RebindContext:
        issue = _norm(request.issue)
        lane = _norm_lane(request.lane)
        reasons: list[str] = []

        def blocked(**plan_fields) -> _RebindContext:
            return _RebindContext(
                RestoredPairRebindPlan(
                    issue=issue,
                    lane=lane,
                    blocked_reasons=tuple(reasons),
                    **plan_fields,
                )
            )

        root = self._resolve_root()
        if root is None:
            reasons.append(REBIND_BLOCK_WORKTREE_UNRESOLVED)
            return blocked()
        workspace_id = self._workspace_id(root)
        if not workspace_id or not lane:
            reasons.append(REBIND_BLOCK_WORKSPACE_UNRESOLVED)
            return blocked()

        try:
            record = self._lifecycle_record(workspace_id, lane)
        except Exception:  # noqa: BLE001 - an unreadable authority fails closed
            reasons.append(REBIND_BLOCK_LIFECYCLE_UNREADABLE)
            return blocked(workspace_id=workspace_id)
        if record is None:
            reasons.append(REBIND_BLOCK_ROW_ABSENT)
            return blocked(workspace_id=workspace_id)
        disposition = _norm(record.lane_disposition)
        revision = int(getattr(record, "revision", 0) or 0)
        generation = int(getattr(record, "lane_generation", 0) or 0)
        stored_worktree = _norm(record.worktree_identity)
        lane_fields = dict(
            workspace_id=workspace_id,
            worktree_identity=stored_worktree,
            lane_disposition=disposition,
            revision=revision,
            lane_generation=generation,
        )
        if disposition != DISPOSITION_ACTIVE:
            # Covers hibernated / superseded / retired: only an ACTIVE lane's
            # pair snapshot is this rail's subject.
            reasons.append(REBIND_BLOCK_NOT_ACTIVE)
        if not stored_binding_kind_is(record.binding_kind, BINDING_KIND_ISSUE) or _norm(
            record.project_scope
        ):
            reasons.append(REBIND_BLOCK_BINDING_NOT_ISSUE)
        if not issue or _norm(record.issue_id) != issue:
            reasons.append(REBIND_BLOCK_ISSUE_MISMATCH)
        if _norm(record.process_release) != RELEASE_NOT_REQUESTED:
            reasons.append(REBIND_BLOCK_RELEASE_OPEN)
        if not replacement_settled(record.replacement_state):
            reasons.append(REBIND_BLOCK_REPLACEMENT_OPEN)
        if reasons:
            return blocked(**lane_fields)

        if not stored_worktree:
            reasons.append(REBIND_BLOCK_WORKTREE_UNBOUND)
        else:
            derived = self._worktree_identity(root, lane)
            if not derived or _norm(derived) != stored_worktree:
                reasons.append(REBIND_BLOCK_WORKTREE_IDENTITY_MISMATCH)
            elif not self._worktree_readable(root):
                reasons.append(REBIND_BLOCK_WORKTREE_UNREADABLE)
            elif _norm_lane(self._branch(root)) != lane:
                reasons.append(REBIND_BLOCK_BRANCH_DRIFTED)
        if reasons:
            return blocked(**lane_fields)

        try:
            gateway_provider, worker_provider = self._providers(root)
        except Exception:  # noqa: BLE001 - an unbound role never guesses a provider
            gateway_provider = worker_provider = ""
        if not gateway_provider or not worker_provider:
            reasons.append(REBIND_BLOCK_PROVIDER_UNRESOLVED)
            return blocked(**lane_fields)

        pair = read_declared_pin_pair(record)
        if not pair.ok or pair.gateway is None or pair.worker is None:
            # Covers an empty snapshot and every suspicious declared shape:
            # there is no exact old pair for the CAS to replace.
            reasons.append(REBIND_BLOCK_DECLARED_SLOTS_UNRESOLVED)
            return blocked(**lane_fields)
        old_slots = (pair.gateway, pair.worker)

        try:
            rows = tuple(row for row in self._rows() if isinstance(row, Mapping))
        except Exception:  # noqa: BLE001 - an unreadable inventory is never evidence
            reasons.append(REBIND_BLOCK_INVENTORY_UNREADABLE)
            return blocked(**lane_fields)

        allow_missing = bool(getattr(request, "allow_single_slot", False))
        gateway_result = self._slot(
            declared=pair.gateway,
            expected_provider=gateway_provider,
            slot_role=_PIN_ROLE_GATEWAY,
            rows=rows,
            workspace_id=workspace_id,
            lane=lane,
            allow_missing=allow_missing,
        )
        worker_result = self._slot(
            declared=pair.worker,
            expected_provider=worker_provider,
            slot_role=_PIN_ROLE_WORKER,
            rows=rows,
            workspace_id=workspace_id,
            lane=lane,
            allow_missing=allow_missing,
        )
        # All-or-nothing: every slot reason blocks the WHOLE pair. A slot that
        # single-slot mode SKIPPED (typed missing-live fact, declared pin kept
        # byte-unchanged) contributes no reasons — but BOTH slots missing means
        # there is nothing to resolve at all, which is the plain refusal.
        if gateway_result.skipped and worker_result.skipped:
            reasons.append(
                slot_reason(REBIND_SLOT_LIVE_ABSENT, _PIN_ROLE_GATEWAY)
            )
            reasons.append(slot_reason(REBIND_SLOT_LIVE_ABSENT, _PIN_ROLE_WORKER))
        reasons.extend(gateway_result.reasons)
        reasons.extend(worker_result.reasons)
        slot_fields = dict(
            lane_fields, gateway=gateway_result.plan, worker=worker_result.plan
        )
        if reasons:
            return blocked(**slot_fields)
        gateway_pin = gateway_result.pin
        worker_pin = worker_result.pin
        assert gateway_pin is not None and worker_pin is not None
        if gateway_pin.locator == worker_pin.locator:
            reasons.append(REBIND_BLOCK_AMBIGUOUS_LOCATORS)
            return blocked(**slot_fields)

        try:
            decision = DecisionPointer(
                source=_norm(record.decision_source),
                issue_id=_norm(record.decision_issue_id),
                journal_id=_norm(record.decision_journal),
            )
            if not decision.authorizes_binding(issue):
                raise DecisionPointerError("stored decision does not bind this issue")
        except (DecisionPointerError, ValueError):
            reasons.append(REBIND_BLOCK_DECISION_ANCHOR_UNUSABLE)
            return blocked(**slot_fields)

        slot_reattests = tuple(
            result.reattest
            for result in (gateway_result, worker_result)
            if result.reattest is not None
        )
        new_slots = (gateway_pin, worker_pin)
        return _RebindContext(
            RestoredPairRebindPlan(
                issue=issue,
                lane=lane,
                blocked_reasons=(),
                reattest_lineage=tuple(
                    plan.lineage_payload() for plan in slot_reattests
                ),
                **slot_fields,
            ),
            key=LaneLifecycleKey(workspace_id, lane),
            expected_revision=revision,
            expected_generation=generation,
            worktree_identity=stored_worktree,
            decision=decision,
            old_slots=old_slots,
            new_slots=new_slots,
            slot_reattests=slot_reattests,
            pins_changed=new_slots != old_slots,
        )

    # -- ops protocol ----------------------------------------------------------

    def observe(self, request: RestoredPairRebindRequest) -> RestoredPairRebindPlan:
        return self._context(request).plan

    def rebind(
        self, request: RestoredPairRebindRequest
    ) -> tuple[bool, Optional[int], str]:
        context = self._context(request)
        if not context.plan.may_rebind:
            return (
                False,
                None,
                "preflight blocked: " + ", ".join(context.plan.blocked_reasons),
            )
        assert context.key is not None and context.decision is not None
        # #15769 write order (retry-safe; every step is its own byte-exact CAS
        # re-derived from THIS action-time observation): (1) the fence
        # participant locator re-pin, (2) the launch-generation re-attest,
        # (3) the declared-pin reconcile. A failure at any step leaves every
        # read-side join fail-closed, and a re-run re-observes the remaining
        # steps (a re-pinned participant is observed as no longer needing the
        # re-pin; a re-attested row reads live-bound).
        for plan in context.slot_reattests:
            if not plan.participant_repin:
                continue
            try:
                self._repin_participant(plan)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - typed zero-write
                return (
                    False,
                    None,
                    f"participant_repin_refused:{plan.slot_role}:"
                    f"{type(exc).__name__}",
                )
        for plan in context.slot_reattests:
            try:
                self._reattest_generation(plan)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - typed refusal
                return (
                    False,
                    None,
                    f"generation_reattest_refused:{plan.slot_role}:"
                    f"{type(exc).__name__}",
                )
        if not context.pins_changed:
            # A pure terminal re-attest (pane locators and pin content
            # unchanged): the lifecycle snapshot is already exact, so there is
            # nothing for the declared-pin CAS to replace.
            return True, context.expected_revision, "generation_reattested"
        store = LaneRecoveredPairPinReconcileStore(home=self.lifecycle_home)
        try:
            result = store.reconcile(
                context.key,
                expected_revision=context.expected_revision,
                expected_generation=context.expected_generation,
                issue_id=context.plan.issue,
                worktree_identity=context.worktree_identity,
                lifecycle_decision=context.decision,
                expected_old_slots=context.old_slots,
                recovered_slots=context.new_slots,
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - typed zero-write
            return False, None, type(exc).__name__
        return result.applied, result.revision, result.reason


def build_live_restored_pair_rebind_use_case(
    repo_root: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> SublaneRestoredPairRebindUseCase:
    return SublaneRestoredPairRebindUseCase(
        LiveRestoredPairRebindOps(
            repo_root=Path(repo_root), env=dict(env or os.environ)
        )
    )


__all__ = (
    "LiveRestoredPairRebindOps",
    "build_live_restored_pair_rebind_use_case",
)
