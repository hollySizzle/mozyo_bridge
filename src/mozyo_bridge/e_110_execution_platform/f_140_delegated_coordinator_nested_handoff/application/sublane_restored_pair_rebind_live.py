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

Design point (#15769 item 3, decided from the code; round 2 j-verdict): the
read-side verifiers must not be widened, so every stale-bound authority the
production delivery conjunct reads is re-pinned WRITE-side instead:
``completed_generation_startup_token``'s ``participant.locator ==
generation.locator`` equality via the fence participant re-pin, the
``verified_terminal_generation_token`` receipt proof via the CAS-guarded
``pane_bound_v2`` receipt re-mint (terminal taken from the server inventory,
never a caller — reasoning documented on
:mod:`...core.state.startup_transaction_restored_repin`), and the queue-enter
attestation locator/terminal equality via
:meth:`...herdr_identity_attestation.HerdrIdentityAttestationStore
.reattest_restored_terminal`. The join / repair derivation and the retry-safe
execution order live in :mod:`.sublane_restored_pair_reattest`; the participant
lineage join is a prerequisite for EVERY path where a generation row
participates in the acceptance (``live_bound`` included — a fabricated row
never bypasses it into a pin write).
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
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_reattest import (  # noqa: E501
    GEN_LIVE_BOUND,
    GEN_REATTEST_NEEDED,
    SlotReattestPlan,
    apply_slot_reattest,
    generation_join,
    participant_repair,
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
    REBIND_SLOT_LIVE_ABSENT,
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
    terminal_identity_of_live_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_LIVE,
    classify_named_slot,
)

_PIN_ROLE_GATEWAY = "gateway"
_PIN_ROLE_WORKER = "worker"


@dataclass(frozen=True)
class _SlotResult:
    """One declared slot's whole observation (plan + write inputs)."""

    plan: RebindSlotPlan
    pin: Optional[ProcessGenerationPin]
    reasons: list
    reattest: Optional[SlotReattestPlan] = None
    skipped: bool = False
    #: #15769 round 3: the slot is fully repaired already (live-bound, no stale
    #: axis). It contributes no blocking reason — a partially-completed pair's
    #: retry must finish the OTHER slot — and only an ALL-current run is the
    #: pair-level typed no-op refusal.
    already_current: bool = False


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
    slot_reattests: tuple[SlotReattestPlan, ...] = ()
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

    def _repin_participant(self, plan: SlotReattestPlan) -> None:
        repair = plan.participant
        assert repair is not None and repair.needs_write
        kwargs = {
            "assigned_name": plan.assigned_name,
            "expected_locator": repair.expected_locator,
        }
        if repair.locator_repin:
            kwargs["new_locator"] = plan.new_locator
        if repair.receipt_remint:
            kwargs["expected_receipt"] = repair.expected_receipt
            kwargs["new_receipt"] = repair.new_receipt
        StartupTransactionFence(home=self.attestation_home).repin_restored_participant(
            plan.startup_action_id, plan.provider, **kwargs
        )

    def _reattest_attestation(self, plan: SlotReattestPlan) -> None:
        HerdrIdentityAttestationStore(
            home=self.attestation_home
        ).reattest_restored_terminal(
            assigned_name=plan.assigned_name,
            workspace_id=plan.attestation_workspace_id,
            role=plan.attestation_role,
            lane_id=plan.attestation_lane_id,
            expected_locator=plan.attestation_expected_locator,
            expected_terminal_id=plan.attestation_expected_terminal_id,
            live_locator=plan.new_locator,
            live_terminal_id=plan.new_terminal_id,
        )

    def _reattest_generation(self, plan: SlotReattestPlan) -> None:
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
        already_current = False
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
        live_terminal_id = None
        attestation = None
        attestation_state = ATTEST_ABSENT
        attested = False
        attestation_restore_stale = False
        attestation_repin = False
        generation = None
        gen_state = ""
        repair = None
        repairs_needed = False
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
                generation, gen_state = generation_join(
                    self,
                    assigned=assigned,
                    want_provider=want_provider,
                    slot_role=slot_role,
                    workspace_id=workspace_id,
                    lane=lane,
                    live_locator=live_locator,
                    live_terminal_id=live_terminal_id,
                    reasons=reasons,
                )
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
                    # was `present`, and ONLY the recorded, canonical locator /
                    # terminal generation pin drifted. Accepted solely when the
                    # launch-generation row independently binds (or is being
                    # re-attested to) the live slot AND the participant lineage
                    # join holds; foreign / missing / conflicting records stay
                    # refused exactly as before.
                    recorded_locator = _norm(getattr(attestation, "locator", ""))
                    recorded_terminal = getattr(attestation, "terminal_id", "")
                    attestation_restore_stale = bool(
                        join.state == ATTEST_STALE
                        and attestation is not None
                        and _norm(getattr(attestation, "verdict", ""))
                        == VERDICT_PRESENT
                        and _norm(getattr(attestation, "assigned_name", ""))
                        == assigned
                        and recorded_locator
                        and type(recorded_terminal) is str
                        and recorded_terminal
                        and recorded_terminal.strip() == recorded_terminal
                    )
                except Exception:  # noqa: BLE001 - unreadable store is never proof
                    attested = False
                    attestation_restore_stale = False
                if gen_state:
                    # Round-2 finding 2: the participant lineage join is a
                    # PREREQUISITE for every path where a generation row
                    # participates in the acceptance — `live_bound` included. A
                    # fabricated row whose fence participants cannot be
                    # explained by this restore's lineage refuses typed with
                    # zero writes.
                    repair = participant_repair(
                        self,
                        generation=generation,
                        want_provider=want_provider,
                        assigned=assigned,
                        live_locator=live_locator,
                        live_terminal_id=live_terminal_id,
                    )
                    if repair is None:
                        reasons.append(
                            slot_reason(
                                REBIND_SLOT_PARTICIPANT_REPIN_UNRESOLVED, slot_role
                            )
                        )
                attestation_repin = bool(
                    gen_state and not attested and attestation_restore_stale
                )
                repairs_needed = bool(gen_state) and (
                    gen_state == GEN_REATTEST_NEEDED
                    or (repair is not None and repair.needs_write)
                    or attestation_repin
                )
                if live_locator == declared_locator:
                    if gen_state and repairs_needed:
                        # #15769: the pane survived on its own locator but a
                        # restore-stale axis remains (terminal / participant /
                        # receipt / attestation) — the repair IS the evidence
                        # to act on.
                        pass
                    elif gen_state == GEN_LIVE_BOUND:
                        # Nothing is stale on any axis. NOT a blocking reason
                        # (review j#108879 finding_partialpairretrydeadlocks): a
                        # retry after a partial-pair failure must pass this
                        # already-repaired slot through and finish the other
                        # one. The pair-level aggregation refuses only when
                        # EVERY slot is current.
                        already_current = True
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
                if not (attested or (attestation_restore_stale and gen_state)):
                    reasons.append(slot_reason(REBIND_SLOT_UNATTESTED, slot_role))

        reattest: Optional[SlotReattestPlan] = None
        if not reasons and gen_state and repairs_needed:
            assert generation is not None and repair is not None
            evidence = [
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
                "participant_lineage_join",
                (
                    "participant_receipt_reminted"
                    if repair.receipt_remint
                    else "participant_receipt_reproven"
                ),
            ]
            reattest = SlotReattestPlan(
                slot_role=slot_role,
                provider=want_provider,
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
                    _norm(getattr(attestation, "locator", ""))
                    if attestation_repin
                    else ""
                ),
                attestation_expected_terminal_id=(
                    getattr(attestation, "terminal_id", "")
                    if attestation_repin
                    else ""
                ),
                attestation_workspace_id=(
                    _norm(getattr(attestation, "workspace_id", ""))
                    if attestation_repin
                    else ""
                ),
                attestation_role=(
                    _norm(getattr(attestation, "role", ""))
                    if attestation_repin
                    else ""
                ),
                attestation_lane_id=(
                    _norm(getattr(attestation, "lane_id", ""))
                    if attestation_repin
                    else ""
                ),
                evidence=tuple(evidence),
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
        return _SlotResult(
            slot_plan, pin, reasons, reattest=reattest, already_current=already_current
        )

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
        if not slot_reattests and new_slots == old_slots:
            # Every resolvable slot is already current and no pin moves: the
            # true pair-level no-op (review j#108879: only an ALL-current run
            # refuses; a partially-repaired pair keeps going instead).
            for result, role in (
                (gateway_result, _PIN_ROLE_GATEWAY),
                (worker_result, _PIN_ROLE_WORKER),
            ):
                if result.already_current:
                    reasons.append(slot_reason(REBIND_SLOT_TERMINAL_UNCHANGED, role))
            if not reasons:
                reasons.append(
                    slot_reason(REBIND_SLOT_TERMINAL_UNCHANGED, _PIN_ROLE_GATEWAY)
                )
            return blocked(**slot_fields)
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
        # re-derived from THIS action-time observation): per slot, (1) the fence
        # participant re-pin (locator + receipt in one CAS), (2) the attestation
        # record re-pin, (3) the launch-generation re-attest; then (4) the
        # declared-pin reconcile. A failure at any step leaves every read-side
        # join fail-closed, and a re-run re-observes and performs only the
        # remaining steps (a re-pinned participant / attestation / row is
        # observed as no longer needing its repair).
        for plan in context.slot_reattests:
            try:
                apply_slot_reattest(self, plan)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - typed zero-write
                return (
                    False,
                    None,
                    f"slot_reattest_refused:{plan.slot_role}:{type(exc).__name__}",
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
