"""The #15769 restored-slot re-attest join + write plans for the rebind rail.

Companion to :mod:`.sublane_restored_pair_rebind_live` (module-health split): the
rail's per-slot gate delegates the launch-generation identity join, the
participant / receipt / attestation repair derivation, and the repair execution
here. Contract anchors: design decision #15769 j#108766; measured deadlock #15631
j#108621/j#108741, #15693 j#108747; round-2 findings (production queue-enter
conjunct + fabricated-live-bound zero-write).

Three joins, every conjunct a SERVER-OWNED fact or a durable row read at action
time (a caller can never supply the identity, terminal, or receipt values that
drive any CAS):

* **generation join** — the attested launch-generation row must decode-match the
  server-owned mzb1 name stamp AND the slot's expected identity, and the live
  terminal must be uniquely resolvable; the row is then ``live_bound`` (already
  binds the live values) or ``reattest_needed``.
* **participant join** — REQUIRED whenever a generation row participates in the
  acceptance (``live_bound`` included — round-2 finding 2: a fabricated
  ``live_bound`` row must not bypass it): the startup action must exist in an
  accepted phase, its participant must be open, exactly this slot, and at a
  locator explainable by the restore lineage (the live locator, or the
  generation row's old locator awaiting the re-pin). The participant's
  ``pane_bound_v2`` receipt must be re-provable: its native name must be this
  slot's, and its terminal must be the live terminal (already re-proven) or the
  generation row's old terminal (awaiting the re-mint). A v1 / unparseable /
  foreign receipt is refused — v1 provenance is never promoted to v2 and no
  receipt is ever fabricated (the re-mint reasoning is documented on
  :mod:`...core.state.startup_transaction_restored_repin`).
* **attestation repair** — when the recorded self-attestation is the restore
  signature (identity match, boot verdict ``present``, only the locator /
  terminal generation pin drifted), the record's pin is CAS-moved to the live
  values (:meth:`...herdr_identity_attestation.HerdrIdentityAttestationStore
  .reattest_restored_terminal`) so the production queue-enter delivery conjunct
  (attestation locator/terminal equality + the terminal-bound receipt proof of
  ``verified_terminal_generation_token``) passes naturally post-rail — round-2
  finding 1.

Execution order (retry-safe; each step is its own byte-exact CAS re-derived from
the action-time observation): participant re-pin (locator + receipt in one fence
CAS) -> attestation re-pin -> generation CAS -> declared-pin reconcile (in the
rail). A partial failure leaves every read-side join fail-closed and a re-run
re-observes and completes only the remaining steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_ROLLBACK_OWED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    PaneBoundReceiptError,
    pane_bound_receipt,
    parse_pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
    decode_assigned_name,
)

#: ``RebindSlotPlan.generation_state`` values: the attested generation row already
#: binds the live terminal + locator / needs the re-attest CAS.
GEN_LIVE_BOUND = "live_bound"
GEN_REATTEST_NEEDED = "reattest_needed"


@dataclass(frozen=True)
class ParticipantRepair:
    """The fence participant's observed state and the repair it authorizes.

    ``expected_locator`` / ``expected_receipt`` are the byte-exact CAS keys read
    from the participant itself at observation time. Receipt BYTES never leave
    this value into any payload — lineage reports booleans and terminal ids only.
    """

    expected_locator: str
    locator_repin: bool
    expected_receipt: str = ""
    new_receipt: str = ""
    receipt_remint: bool = False

    @property
    def needs_write(self) -> bool:
        return self.locator_repin or self.receipt_remint


@dataclass(frozen=True)
class SlotReattestPlan:
    """One slot's authorized restore repairs (#15769).

    Every value is re-derived from the action-time observation (never carried
    from a caller): the CAS keys are the exact old row values and the new values
    are the server-owned live inventory facts.
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
    generation_cas: bool
    participant: Optional[ParticipantRepair]
    attestation_repin: bool
    attestation_expected_locator: str
    attestation_expected_terminal_id: str
    attestation_workspace_id: str
    attestation_role: str
    attestation_lane_id: str
    evidence: tuple[str, ...]

    @property
    def needs_write(self) -> bool:
        return bool(
            self.generation_cas
            or (self.participant is not None and self.participant.needs_write)
            or self.attestation_repin
        )

    def lineage_payload(self) -> dict:
        """The journal-ready durable lineage record (no receipt bytes)."""
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
            "generation_reattested": self.generation_cas,
            "participant_locator_repin": bool(
                self.participant is not None and self.participant.locator_repin
            ),
            "participant_receipt_reminted": bool(
                self.participant is not None and self.participant.receipt_remint
            ),
            "attestation_repin": self.attestation_repin,
            "evidence": list(self.evidence),
        }


def generation_join(
    ops,
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
    """The launch-generation identity join -> ``(generation, gen_state)``.

    ``gen_state`` is ``""`` (no usable attested row — the pre-#15769 rail shape),
    :data:`GEN_LIVE_BOUND`, or :data:`GEN_REATTEST_NEEDED`. Every identity
    conjunct is a SERVER-OWNED fact or the durable row itself; a foreign /
    ambiguous / undecodable join appends a typed refusal and never guesses.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import GENERATION_ATTESTED
    from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
        REBIND_SLOT_GENERATION_UNREADABLE,
        REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED,
        slot_reason,
    )

    try:
        generation = ops._read_generation(assigned)
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
        # A pending / superseded / non-present row is launch-rail property; it
        # is never re-attested and lends no drift evidence here.
        return None, ""
    decoded = decode_assigned_name(assigned)
    stamps_match = bool(
        decoded.ok
        and decoded.identity is not None
        and _norm(decoded.identity.workspace_id) == _norm(generation.workspace_id)
        and _norm(decoded.identity.role) == _norm(generation.role)
        and _norm_lane(decoded.identity.lane_id) == _norm_lane(generation.lane_id)
    )
    row_matches_slot = bool(
        _norm(generation.assigned_name) == assigned
        and _norm(generation.workspace_id) == _norm(workspace_id)
        and _norm(generation.role) == want_provider
        and _norm_lane(generation.lane_id) == _norm_lane(lane)
    )
    if not (stamps_match and row_matches_slot):
        # The server-owned name stamp does not decode to the generation row's
        # identity, or the row is foreign to this slot: never re-attested
        # (#15769 security invariant — foreign rejection).
        reasons.append(slot_reason(REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED, slot_role))
        return generation, ""
    if live_terminal_id is None:
        # No unique canonical server-owned terminal for this name+locator
        # (duplicate claims / malformed snapshot): the join has no answer.
        reasons.append(slot_reason(REBIND_SLOT_LIVE_IDENTITY_JOIN_FAILED, slot_role))
        return generation, ""
    if (
        generation.terminal_id == live_terminal_id
        and _norm(generation.locator) == live_locator
    ):
        return generation, GEN_LIVE_BOUND
    return generation, GEN_REATTEST_NEEDED


def participant_repair(
    ops, *, generation, want_provider: str, assigned: str, live_locator: str,
    live_terminal_id: str,
) -> Optional[ParticipantRepair]:
    """The REQUIRED participant/receipt join -> repair state, or ``None`` (refuse).

    Runs for EVERY slot whose generation row participates in the acceptance
    (``live_bound`` included — round-2 finding 2). ``None`` means the
    participant-side lineage could not be established exactly (unreadable fence,
    non-accepted phase, closed / foreign / unexplainable participant, or a
    receipt that cannot be re-proven from server-owned facts).
    """
    try:
        action = ops._read_startup_action(_norm(generation.startup_action_id))
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
        locator_repin = False  # an earlier partial run already re-pinned it
    elif participant_locator == _norm(generation.locator):
        locator_repin = participant_locator != live_locator
    else:
        return None
    # -- receipt leg (round-2 finding 1): re-provable from server-owned facts. --
    raw_receipt = getattr(participant, "receipt", "")
    try:
        parsed = parse_pane_bound_receipt(raw_receipt)
    except PaneBoundReceiptError:
        return None
    if parsed is None or not parsed.terminal_id:
        # A legacy / v1 receipt was never terminal-bound; fabricating v2
        # provenance for it is the promotion the spec forbids.
        return None
    if parsed.native_name != native_name_for(assigned):
        return None
    if parsed.terminal_id == live_terminal_id:
        return ParticipantRepair(
            expected_locator=participant_locator, locator_repin=locator_repin
        )
    if parsed.terminal_id != generation.terminal_id:
        # Bound to a terminal that is neither the live one nor the generation
        # row's old one: not this restore's lineage — refuse, never re-mint.
        return None
    return ParticipantRepair(
        expected_locator=participant_locator,
        locator_repin=locator_repin,
        expected_receipt=raw_receipt,
        new_receipt=pane_bound_receipt(
            target_workspace=parsed.workspace_id,
            target_tab=parsed.tab_id,
            native_name=parsed.native_name,
            terminal_id=live_terminal_id,
        ),
        receipt_remint=True,
    )


def apply_slot_reattest(ops, plan: SlotReattestPlan) -> None:
    """Execute one slot's repairs in the retry-safe order (typed errors raise)."""
    if plan.participant is not None and plan.participant.needs_write:
        ops._repin_participant(plan)
    if plan.attestation_repin:
        ops._reattest_attestation(plan)
    if plan.generation_cas:
        ops._reattest_generation(plan)


__all__ = (
    "GEN_LIVE_BOUND",
    "GEN_REATTEST_NEEDED",
    "ParticipantRepair",
    "SlotReattestPlan",
    "apply_slot_reattest",
    "generation_join",
    "participant_repair",
)
