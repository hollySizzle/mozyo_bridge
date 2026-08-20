"""Live authority join for the pin-ABSENT restored-pair adopt rail (#15811).

The fail-closed observation + bounded declaration write behind
``sublane adopt-restored-pair``. It invents no authority: every join is one the
neighbouring rails already own, and the write is the EXISTING empty-only binding CAS.

- lane row / owner binding: :class:`...lane_lifecycle.LaneLifecycleStore` (read) and
  :meth:`...lane_declaration.LaneDeclarationStore.backfill_active_binding` (the #13809
  bounded CAS that fills an EMPTY ``declared_slots`` snapshot and refuses any non-empty
  different one, zero-write);
- slot identity: :func:`...sublane_adopt_declaration.select_named_slot_candidate` — the
  adopt path's own decode-by-name selection with its raw-multiplicity / liveness /
  surfaced-provider gates;
- launch generation / participant lineage / attestation repair:
  :func:`...sublane_restored_pair_reattest.generation_join` /
  :func:`...sublane_restored_pair_reattest.participant_repair` /
  :func:`...sublane_restored_pair_reattest.apply_slot_reattest` — the #15769 server-owned
  joins, byte-unchanged;
- host probes and store bindings: the shared
  :class:`...restored_pair_store_seams.RestoredPairStoreSeams`.

**Why this rail is not a widening.** Its subject is exactly the row whose declared-pin
snapshot is ABSENT (:data:`...lane_pin_role.PIN_PAIR_ABSENT`) — the create-path shape, where
no pin was ever written, so there is nothing to overwrite and no "old pair" a CAS could
replace. Any other snapshot shape, including a suspicious NON-EMPTY one, is refused: a
degraded snapshot is a different defect whose evidence must survive for
``sublane repair-pins`` / an owner decision. Within that subject the proof chain is
STRICTLY STRONGER than either neighbour, because there is no declared pin to check the live
slot against:

1. the slot is DECODED out of the server-owned inventory names, never matched against a
   caller-supplied name, and must be the unique live row for this workspace/lane/provider
   with agreeing surfaced provider stamps;
2. a usable ATTESTED launch-generation row is REQUIRED (:data:`...restored_pair_adopt
   .ADOPT_SLOT_GENERATION_ABSENT` otherwise) — the rebind rail may fall through to its
   declared-pin evidence when no such row exists; this rail may not, because that row is
   what ties the live process to THIS lane beyond its name;
3. the startup-transaction participant lineage join must hold for every slot;
4. the recorded self-attestation must carry this exact identity and be either live-joined
   or the #15769 restore-stale signature (identity match + ``present`` boot verdict + only
   the locator / terminal generation pin drifted). Foreign / missing / conflicting stays
   refused.

Every gate failure is zero-write with a typed reason. The write never closes, launches,
sends, chmods, or touches a worktree, and never changes ``lane_generation``: the restored
processes are the same agent-session incarnation, so existing dispatch-marker anchors stay
valid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_ABSENT,
    ATTEST_STALE,
    VERDICT_PRESENT,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    RELEASE_NOT_REQUESTED,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleKey,
    ProcessGenerationPin,
    ProcessPinError,
    replacement_settled,
    stored_binding_kind_is,
)
from mozyo_bridge.core.state.lane_pin_role import (
    PIN_PAIR_ABSENT,
    PIN_ROLE_GATEWAY,
    PIN_ROLE_WORKER,
    read_declared_pin_pair,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.restored_pair_store_seams import (  # noqa: E501
    RestoredPairStoreSeams,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    select_named_slot_candidate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt import (  # noqa: E501
    SublaneRestoredPairAdoptUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_reattest import (  # noqa: E501
    GEN_REATTEST_NEEDED,
    SlotReattestPlan,
    apply_slot_reattest,
    generation_join,
    participant_repair,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_adopt import (  # noqa: E501
    ADOPT_BLOCK_AMBIGUOUS_LOCATORS,
    ADOPT_BLOCK_BINDING_NOT_ISSUE,
    ADOPT_BLOCK_BRANCH_DRIFTED,
    ADOPT_BLOCK_DECISION_ANCHOR_UNUSABLE,
    ADOPT_BLOCK_DECLARED_PINS_PRESENT,
    ADOPT_BLOCK_INVENTORY_UNREADABLE,
    ADOPT_BLOCK_ISSUE_MISMATCH,
    ADOPT_BLOCK_LIFECYCLE_UNREADABLE,
    ADOPT_BLOCK_NOT_ACTIVE,
    ADOPT_BLOCK_PROVIDER_UNRESOLVED,
    ADOPT_BLOCK_RELEASE_OPEN,
    ADOPT_BLOCK_REPLACEMENT_OPEN,
    ADOPT_BLOCK_ROW_ABSENT,
    ADOPT_BLOCK_WORKSPACE_UNRESOLVED,
    ADOPT_BLOCK_WORKTREE_IDENTITY_MISMATCH,
    ADOPT_BLOCK_WORKTREE_UNBOUND,
    ADOPT_BLOCK_WORKTREE_UNREADABLE,
    ADOPT_BLOCK_WORKTREE_UNRESOLVED,
    ADOPT_SLOT_GENERATION_ABSENT,
    AdoptSlotPlan,
    RestoredPairAdoptPlan,
    RestoredPairAdoptRequest,
    slot_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED,
    REBIND_SLOT_UNATTESTED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
    terminal_identity_of_live_slot,
)


@dataclass(frozen=True)
class _SlotResult:
    """One slot's whole observation (plan + write inputs)."""

    plan: AdoptSlotPlan
    pin: Optional[ProcessGenerationPin]
    reasons: list
    reattest: Optional[SlotReattestPlan] = None


@dataclass(frozen=True)
class _AdoptContext:
    """The single observation join the preflight AND the write both derive from."""

    plan: RestoredPairAdoptPlan
    key: Optional[LaneLifecycleKey] = None
    expected_revision: int = 0
    worktree_identity: str = ""
    new_slots: tuple[ProcessGenerationPin, ...] = ()
    slot_reattests: tuple[SlotReattestPlan, ...] = ()


@dataclass
class LiveRestoredPairAdoptOps(RestoredPairStoreSeams):
    """Live fail-closed observation + bounded declaration write."""

    # -- per-slot gate --------------------------------------------------------

    def _slot(
        self,
        *,
        slot_role: str,
        provider: str,
        rows,
        workspace_id: str,
        lane: str,
    ) -> _SlotResult:
        """Resolve ONE slot from live restore evidence alone (no declared pin exists)."""
        reasons: list[str] = []
        candidate, candidate_reason = select_named_slot_candidate(
            rows=rows, workspace_id=workspace_id, lane_id=lane, provider=provider
        )
        if candidate is None:
            reason = slot_reason(candidate_reason, slot_role)
            return _SlotResult(
                AdoptSlotPlan(slot_role=slot_role, provider=provider, reason=reason),
                None,
                [reason],
            )
        assigned = candidate.assigned_name
        live_locator = candidate.locator
        live_revision = _norm(candidate.row.get("runtime_revision"))
        live_terminal_id = terminal_identity_of_live_slot(assigned, live_locator, rows)

        generation, gen_state = generation_join(
            self,
            assigned=assigned,
            want_provider=provider,
            slot_role=slot_role,
            workspace_id=workspace_id,
            lane=lane,
            live_locator=live_locator,
            live_terminal_id=live_terminal_id,
            reasons=reasons,
        )
        if not gen_state and not reasons:
            # `generation_join` reports "no usable attested row" as an empty state with no
            # reason (the rebind rail's pre-#15769 fall-through). This rail has no declared
            # pin to fall back on, so the missing server-owned row is a hard refusal.
            reasons.append(slot_reason(ADOPT_SLOT_GENERATION_ABSENT, slot_role))

        attestation = None
        attestation_state = ATTEST_ABSENT
        attested = False
        attestation_restore_stale = False
        try:
            attestation = self._read_attestation(assigned)
            join = evaluate_attestation(
                attestation,
                live_locator=live_locator,
                live_terminal_id=live_terminal_id,
                expected_workspace_id=workspace_id,
                expected_role=provider,
                expected_lane=lane,
            )
            attestation_state = join.state
            attested = bool(
                join.ok and _norm(getattr(attestation, "assigned_name", "")) == assigned
            )
            # The #15769 restore signature, evaluated identically here: the recorded
            # identity matched (a conflict precedes stale in `evaluate_attestation`), the
            # agent's own boot verdict was `present`, and ONLY the recorded locator /
            # terminal generation pin drifted.
            recorded_locator = _norm(getattr(attestation, "locator", ""))
            recorded_terminal = getattr(attestation, "terminal_id", "")
            attestation_restore_stale = bool(
                join.state == ATTEST_STALE
                and attestation is not None
                and _norm(getattr(attestation, "verdict", "")) == VERDICT_PRESENT
                and _norm(getattr(attestation, "assigned_name", "")) == assigned
                and recorded_locator
                and type(recorded_terminal) is str
                and recorded_terminal
                and recorded_terminal.strip() == recorded_terminal
            )
        except Exception:  # noqa: BLE001 - an unreadable store is never proof
            attested = False
            attestation_restore_stale = False

        repair = None
        if gen_state:
            # Required for every slot whose generation row participates in the acceptance
            # (#15769 round-2 finding 2): a fabricated row never bypasses the fence lineage.
            repair = participant_repair(
                self,
                generation=generation,
                want_provider=provider,
                assigned=assigned,
                live_locator=live_locator,
                live_terminal_id=live_terminal_id,
            )
            if repair is None:
                reasons.append(
                    slot_reason(REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED, slot_role)
                )
        attestation_repin = bool(gen_state and not attested and attestation_restore_stale)
        if not (attested or (attestation_restore_stale and gen_state)):
            reasons.append(slot_reason(REBIND_SLOT_UNATTESTED, slot_role))

        reattest: Optional[SlotReattestPlan] = None
        pin: Optional[ProcessGenerationPin] = None
        if not reasons:
            assert generation is not None and repair is not None
            reattest = _reattest_plan(
                slot_role=slot_role,
                provider=provider,
                assigned=assigned,
                generation=generation,
                live_locator=live_locator,
                live_terminal_id=live_terminal_id,
                gen_state=gen_state,
                repair=repair,
                attested=attested,
                attestation=attestation,
                attestation_repin=attestation_repin,
            )
            try:
                # `runtime_revision` is deliberately EMPTY on a FIRST declaration, the same
                # convention the adopt path (`_resolve_attested_slot`) and the hibernated
                # pin repair use: herdr exposes no runtime-version surface, the generation
                # discriminant is the locator, and a fabricated version is never written.
                # The observed value still rides the display plan below as evidence.
                pin = ProcessGenerationPin(
                    role=slot_role,
                    provider=provider,
                    assigned_name=assigned,
                    locator=live_locator,
                    attested_at=_norm(getattr(attestation, "observed_at", "")),
                )
            except ProcessPinError:
                reasons.append(slot_reason(ADOPT_SLOT_GENERATION_ABSENT, slot_role))
                reattest = None
        slot_plan = AdoptSlotPlan(
            slot_role=slot_role,
            provider=provider,
            assigned_name=assigned,
            live_locator=live_locator,
            live_runtime_revision=live_revision,
            attestation_state=attestation_state,
            ready=not reasons,
            reason=",".join(reasons),
            generation_state=gen_state,
        )
        return _SlotResult(slot_plan, pin, reasons, reattest=reattest)

    # -- the single observation join ------------------------------------------

    def _context(self, request: RestoredPairAdoptRequest) -> _AdoptContext:
        issue = _norm(request.issue)
        lane = _norm_lane(request.lane)
        reasons: list[str] = []

        def blocked(**plan_fields) -> _AdoptContext:
            return _AdoptContext(
                RestoredPairAdoptPlan(
                    issue=issue,
                    lane=lane,
                    blocked_reasons=tuple(reasons),
                    **plan_fields,
                )
            )

        root = self._resolve_root()
        if root is None:
            reasons.append(ADOPT_BLOCK_WORKTREE_UNRESOLVED)
            return blocked()
        workspace_id = self._workspace_id(root)
        if not workspace_id or not lane:
            reasons.append(ADOPT_BLOCK_WORKSPACE_UNRESOLVED)
            return blocked()

        try:
            record = self._lifecycle_record(workspace_id, lane)
        except Exception:  # noqa: BLE001 - an unreadable authority fails closed
            reasons.append(ADOPT_BLOCK_LIFECYCLE_UNREADABLE)
            return blocked(workspace_id=workspace_id)
        if record is None:
            reasons.append(ADOPT_BLOCK_ROW_ABSENT)
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
            reasons.append(ADOPT_BLOCK_NOT_ACTIVE)
        if not stored_binding_kind_is(record.binding_kind, BINDING_KIND_ISSUE) or _norm(
            record.project_scope
        ):
            reasons.append(ADOPT_BLOCK_BINDING_NOT_ISSUE)
        if not issue or _norm(record.issue_id) != issue:
            reasons.append(ADOPT_BLOCK_ISSUE_MISMATCH)
        if _norm(record.process_release) != RELEASE_NOT_REQUESTED:
            reasons.append(ADOPT_BLOCK_RELEASE_OPEN)
        if not replacement_settled(record.replacement_state):
            reasons.append(ADOPT_BLOCK_REPLACEMENT_OPEN)
        if reasons:
            return blocked(**lane_fields)

        if not stored_worktree:
            reasons.append(ADOPT_BLOCK_WORKTREE_UNBOUND)
        else:
            derived = self._worktree_identity(root, lane)
            if not derived or _norm(derived) != stored_worktree:
                reasons.append(ADOPT_BLOCK_WORKTREE_IDENTITY_MISMATCH)
            elif not self._worktree_readable(root):
                reasons.append(ADOPT_BLOCK_WORKTREE_UNREADABLE)
            elif _norm_lane(self._branch(root)) != lane:
                reasons.append(ADOPT_BLOCK_BRANCH_DRIFTED)
        if reasons:
            return blocked(**lane_fields)

        try:
            gateway_provider, worker_provider = self._providers(root)
        except Exception:  # noqa: BLE001 - an unbound role never guesses a provider
            gateway_provider = worker_provider = ""
        if not gateway_provider or not worker_provider:
            reasons.append(ADOPT_BLOCK_PROVIDER_UNRESOLVED)
            return blocked(**lane_fields)

        # THE subject gate, at the position the sibling rail reads its declared pair (after
        # the lane / worktree / provider gates, before the inventory) so the two rails order
        # their refusals identically. Only a row whose snapshot is exactly ABSENT is this
        # rail's target: a resolvable pair has nothing to declare, and a NON-EMPTY
        # suspicious snapshot (unreadable / foreign / mixed / duplicate / half a pair) is a
        # different defect whose evidence must not be overwritten from live observation.
        pair = read_declared_pin_pair(record)
        if pair.reason != PIN_PAIR_ABSENT:
            reasons.append(
                f"{ADOPT_BLOCK_DECLARED_PINS_PRESENT}:"
                f"{pair.reason or 'declared_pin_pair_ok'}"
            )
            return blocked(**lane_fields)

        try:
            rows = tuple(row for row in self._rows() if isinstance(row, Mapping))
        except Exception:  # noqa: BLE001 - an unreadable inventory is never evidence
            reasons.append(ADOPT_BLOCK_INVENTORY_UNREADABLE)
            return blocked(**lane_fields)

        gateway_result = self._slot(
            slot_role=PIN_ROLE_GATEWAY,
            provider=gateway_provider,
            rows=rows,
            workspace_id=workspace_id,
            lane=lane,
        )
        worker_result = self._slot(
            slot_role=PIN_ROLE_WORKER,
            provider=worker_provider,
            rows=rows,
            workspace_id=workspace_id,
            lane=lane,
        )
        # All-or-nothing. There is no single-slot mode: with no declared pin the row holds
        # no record of what the other half of the pair was, so half an observation could
        # never be declared as a pair.
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
            reasons.append(ADOPT_BLOCK_AMBIGUOUS_LOCATORS)
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
            reasons.append(ADOPT_BLOCK_DECISION_ANCHOR_UNUSABLE)
            return blocked(**slot_fields)

        slot_reattests = tuple(
            result.reattest
            for result in (gateway_result, worker_result)
            if result.reattest is not None and result.reattest.needs_write
        )
        return _AdoptContext(
            RestoredPairAdoptPlan(
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
            worktree_identity=stored_worktree,
            new_slots=(gateway_pin, worker_pin),
            slot_reattests=slot_reattests,
        )

    # -- ops protocol ----------------------------------------------------------

    def observe(self, request: RestoredPairAdoptRequest) -> RestoredPairAdoptPlan:
        return self._context(request).plan

    def adopt(
        self, request: RestoredPairAdoptRequest
    ) -> tuple[bool, Optional[int], str]:
        context = self._context(request)
        if not context.plan.may_adopt:
            return (
                False,
                None,
                "preflight blocked: " + ", ".join(context.plan.blocked_reasons),
            )
        assert context.key is not None
        # The #15769 write order, retry-safe: per slot, (1) the fence participant re-pin
        # (locator + receipt in one CAS), (2) the attestation record re-pin, (3) the
        # launch-generation re-attest; then (4) the declared-pin declaration. A failure at
        # any step leaves every read-side join fail-closed, and a re-run re-observes and
        # performs only the remaining steps.
        for plan in context.slot_reattests:
            try:
                apply_slot_reattest(self, plan)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - typed zero-write
                return (
                    False,
                    None,
                    f"slot_reattest_refused:{plan.slot_role}:{type(exc).__name__}",
                )
        try:
            result = LaneDeclarationStore(
                home=self.lifecycle_home
            ).backfill_active_binding(
                context.key,
                expected_revision=context.expected_revision,
                issue_id=context.plan.issue,
                worktree_identity=context.worktree_identity,
                declared_slots=context.new_slots,
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - typed zero-write
            return False, None, type(exc).__name__
        return result.applied, result.revision, result.reason


def _reattest_plan(
    *,
    slot_role: str,
    provider: str,
    assigned: str,
    generation,
    live_locator: str,
    live_terminal_id,
    gen_state: str,
    repair,
    attested: bool,
    attestation,
    attestation_repin: bool,
) -> SlotReattestPlan:
    """The slot's authorized restore repairs + the evidence conjuncts that held."""
    evidence = [
        "unique_live_named_slot",
        "slot_live",
        "live_provider_stamp_match",
        "assigned_name_decodes_generation_identity",
        "generation_identity_matches_slot",
        "unique_live_terminal_identity",
        "attested_generation_row_required",
        "declared_pins_absent_subject",
        (
            "attestation_ok_live_join"
            if attested
            else "attestation_restore_stale_present"
        ),
        "participant_lineage_join",
        (
            "participant_receipt_reminted"
            if repair.receipt_remint
            else "participant_receipt_reproven"
        ),
    ]
    return SlotReattestPlan(
        slot_role=slot_role,
        provider=provider,
        assigned_name=assigned,
        startup_action_id=_norm(generation.startup_action_id),
        workspace_id=_norm(generation.workspace_id),
        lane_id=_norm(generation.lane_id),
        verdict=_norm(generation.verdict),
        old_locator=_norm(generation.locator),
        old_terminal_id=generation.terminal_id,
        new_locator=live_locator,
        new_terminal_id=live_terminal_id,
        generation_cas=(gen_state == GEN_REATTEST_NEEDED),
        participant=repair,
        attestation_repin=attestation_repin,
        attestation_expected_locator=(
            _norm(getattr(attestation, "locator", "")) if attestation_repin else ""
        ),
        attestation_expected_terminal_id=(
            getattr(attestation, "terminal_id", "") if attestation_repin else ""
        ),
        attestation_workspace_id=(
            _norm(getattr(attestation, "workspace_id", "")) if attestation_repin else ""
        ),
        attestation_role=(
            _norm(getattr(attestation, "role", "")) if attestation_repin else ""
        ),
        attestation_lane_id=(
            _norm(getattr(attestation, "lane_id", "")) if attestation_repin else ""
        ),
        evidence=tuple(evidence),
    )


def build_live_restored_pair_adopt_use_case(
    repo_root: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> SublaneRestoredPairAdoptUseCase:
    return SublaneRestoredPairAdoptUseCase(
        LiveRestoredPairAdoptOps(
            repo_root=Path(repo_root), env=dict(env or os.environ)
        )
    )


__all__ = (
    "LiveRestoredPairAdoptOps",
    "build_live_restored_pair_adopt_use_case",
)
