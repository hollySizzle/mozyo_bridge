"""The legacy lane-epoch recovery PLAN — dry-run only (Redmine #14756 j#96861 / j#96866).

A lane hibernated by a pre-#14756 build has no ``lane_epoch``, so ``sublane resume`` refuses
it and the only rail that mints one is an adoption (:mod:`...lane_epoch_adoption`) whose
result is unusable until the attestation store can actually hold an epoch. Getting such a
lane moving therefore needs a *sequence*, not a command — and j#96861 fixed that sequence's
order: **close first, adopt last**. The reverse (adopt, then close the old pair) leaves a
crash window in which the lane holds ``epoch=1`` while the store is still v1 and the old pair
is still live, and the pre-effect fence this issue added then refuses the very close that
would clear it. A recovery rail that can deadlock on its own output is not a recovery rail.

**This module plans that sequence and executes none of it, and that is a ruling rather than
an unfinished edge.** Coordinator correction j#96866 dry-ran the sequence against the real
environment and measured what the lane-local view could not see: the attested-live
intersection is **5 workspaces / 18 agents**, including the coordinator that would be running
the command. Every one of them holds a row in the v1 store, so the migration step refuses
with ``blocked_consumers_live`` no matter how carefully the target pair is closed. A rail
that closed the target pair and *then* hit that wall would have destroyed the one pair it was
asked to save and made no progress at all — a partial shutdown, arrived at honestly.

So the ordering correction and the blast-radius correction compose into one rule, which is
what :func:`plan_lane_epoch_legacy_recovery` implements:

- the consumer census runs **before** the plan admits any close, and any consumer beyond the
  target pair's own slots yields :data:`OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED` with zero
  close, zero CAS and zero migration (j#96866 ruling 1);
- the close-first sequence is emitted only when the census is already clear — i.e. when an
  external, non-consuming actor has already drained the fleet for a global offline upgrade
  (j#96866 ruling 2). It is a plan for that window, not a way to open one.

The global rollout itself — drain every workspace, stop the callback supervisors, migrate the
attestation store, migrate lifecycle v9 -> v10 backup-first, install the new runtime, relaunch
the top coordinator first — is a separate durable work unit (j#96866 ruling 4). It needs an
actor outside the fleet, and nothing here should read as a substitute for one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_epoch_adoption import legacy_adoption_refusal
from mozyo_bridge.core.state.lane_lifecycle_model import (
    DecisionPointer,
    LaneLifecycleKey,
)

#: The plan is admissible: the fleet is already drained and each step below can be taken.
PLAN_READY = "plan_ready"

#: Refused before any effect: consumers other than the target pair hold rows in this store,
#: so the migration step cannot succeed and closing the target pair would only destroy it.
#: The named next action is the separate global offline runtime upgrade, not a retry here.
OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED = "offline_global_runtime_upgrade_required"

#: Refused: the lane is not the legacy shape this rail exists for. Carries the lifecycle
#: CAS's own reason token, never a second opinion about the row.
BLOCKED_NOT_LEGACY_SHAPE = "blocked_not_legacy_shape"

#: Refused: the lane has no lifecycle row at all in this home.
BLOCKED_LANE_ABSENT = "blocked_lane_absent"

#: Refused: liveness could not be measured, so an empty intersection cannot be PROVEN. An
#: unreadable inventory is not an empty one (#13682 R1-F1), and this is the axis where
#: guessing "nobody is running" authorises a close.
BLOCKED_CONSUMERS_UNMEASURABLE = "blocked_consumers_unmeasurable"

#: Refused: the lane's own pair cannot be derived from stored authority — the generation-bound
#: release observation is absent, malformed, or not a coherent pair. Without it there is no
#: authority for WHICH consumers are this lane's own, and the answer must not be guessed.
BLOCKED_PAIR_AUTHORITY_UNAVAILABLE = "blocked_pair_authority_unavailable"

#: Refused: a stored pin that IS in the consumer census does not join byte-exact to the live
#: inventory and the startup attestation (workspace / lane / role / locator). Excluding it
#: would mean excluding a slot this lane cannot prove is its own.
BLOCKED_PAIR_IDENTITY_MISMATCH = "blocked_pair_identity_mismatch"

#: Refused: the caller's ``--target-slot`` assertion does not equal the authority-derived
#: pair. The flag asserts; it never supplies.
BLOCKED_TARGET_SLOT_ASSERTION_FAILED = "blocked_target_slot_assertion_failed"

_OK_STATES = frozenset({PLAN_READY})


@dataclass(frozen=True)
class LaneEpochRecoveryPlan:
    """The auditable result of one planning run. ``executed`` is always ``False``."""

    state: str
    lane: str = ""
    workspace_id: str = ""
    issue_id: str = ""
    #: Live agents holding a row in this store that are NOT the target pair's own slots.
    foreign_consumers: tuple = ()
    #: The lane's own slots as DERIVED from its stored release observation and joined
    #: byte-exact against live inventory + attestation. This is the set actually subtracted
    #: from the census; it never comes from the caller (j#96881 F1).
    target_slots: tuple = ()
    #: What the caller ASSERTED the pair to be, echoed verbatim. Carried separately from
    #: ``target_slots`` so a reader can see that the two were compared rather than merged —
    #: the previous shape had one field and no way to tell input from authority.
    asserted_slots: tuple = ()
    lifecycle_reason: str = ""
    steps: Sequence[str] = field(default_factory=tuple)
    detail: str = ""

    #: This rail has no execute mode. The field exists so the payload shape matches the
    #: other maintenance results, and it is a constant so no caller can read it as a
    #: promise that some flag would flip it.
    executed: bool = False

    @property
    def ok(self) -> bool:
        return self.state in _OK_STATES

    def as_payload(self) -> dict:
        return {
            "intent": "lane-epoch-recovery-plan",
            "state": self.state,
            "ok": self.ok,
            "executed": self.executed,
            "lane": self.lane,
            "workspace_id": self.workspace_id,
            "issue_id": self.issue_id,
            "foreign_consumers": list(self.foreign_consumers),
            "target_slots": list(self.target_slots),
            "asserted_slots": list(self.asserted_slots),
            "lifecycle_reason": self.lifecycle_reason,
            "steps": list(self.steps),
            "detail": self.detail,
        }


#: The canonical order fixed by j#96861, as operator-facing text. Close first: each step is
#: re-runnable from the state its predecessor leaves, and in particular a crash after the
#: adoption is harmless because the store is v3 by then, so the effect-free schema fence
#: cannot refuse the old-pair close that a retry would need.
CANONICAL_STEPS: tuple = (
    "1. Preflight the exact action / pair / revision / worktree / WIP pins and establish a "
    "replayable action record. Every later step is re-runnable from it.",
    "2. Terminally close BOTH slots of the exact old pair. In legacy migration mode this "
    "explicitly overrides the usual recover-pair contract of keeping a healthy slot: a "
    "surviving slot carries no epoch in its environment and can never satisfy the resume "
    "proof, so keeping it preserves nothing and keeps the census non-empty.",
    "3. Re-confirm the attested-live intersection is empty, freshly. The census taken before "
    "the close is evidence about the past.",
    "4. Migrate the attestation store backup-first, then read it back strictly.",
    "5. Adopt the lifecycle epoch 0 -> 1. Only now — after the store can hold what this "
    "mints. Reversing 4 and 5 is the self-deadlock j#96861 corrected.",
    "6. Relaunch a fresh pair on the v3 store with a native epoch 1.",
    "7. Resume / redispatch under the existing recovery contract, or converge on a single "
    "named hibernated stop. One or the other, fixed in advance, so a replay cannot dispatch "
    "twice.",
)


#: The exact number of slots a lane's own pair has. Not a tunable — the whole rail is
#: "close BOTH slots of the exact old pair", and a rail that accepted one slot would exclude
#: half a pair from the census while calling the result the pair (j#96890 §1).
_PAIR_SLOT_COUNT = 2


def _derive_target_pair(record, view, home: Path, workspace_id: str, lane: str):
    """``(names, refusal_state, detail)`` — the lane's OWN pair, from stored authority.

    The exclusion set that decides whether the global blocker fires, so it must not come from
    the caller. j#96881 F1 measured why: the first version subtracted a caller-supplied
    ``--target-slot`` list unconditionally, so naming every consumer turned
    ``offline_global_runtime_upgrade_required`` into ``plan_ready``. The blocker existed and
    could be erased by the party it was blocking (reproduced before fixing: four consumers,
    four names supplied, plan went green).

    **The authority is ``declared_slots``, not ``release_observation``** (j#96895, measured
    against the real #14755 row: observation length 0, declared slots length 532). Two reasons,
    and the second is the one that matters:

    - the named acceptance target has no release observation at all, so an
      observation-sourced derivation refuses the one lane this rail exists for;
    - a release observation is a snapshot of the slots the ORIGINAL release enumerated. After a
      bootstrap or a pin repair it is past evidence, not the current generation's pair. Using
      it as current authority would be reading a stale fact as a live one — the same class of
      error the epoch itself exists to close.

    Release evidence is therefore not consulted here at all. It remains useful for diagnosing
    contradictions, but it is not a fallback: a rail with two authorities eventually answers
    from whichever one happens to be populated.

    Every axis is joined **byte-exact** across three sources — the stored pin, the live
    inventory row, and the startup attestation — because a name alone proves nothing about
    which process currently answers to it. ``locator`` is joined on all three (j#96890 §2: a
    stale attestation row would otherwise let a recycled pane be excluded as "ours").

    Nothing is ever excluded on inference. Absent / corrupt / non-pair declared slots, a pin
    that is live but unattested (or the reverse), and any axis mismatch are all typed
    refusals — never a smaller exclusion set, which would silently narrow the census.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HerdrIdentityAttestationStore,
    )
    from mozyo_bridge.core.state.lane_declared_slots import decode_declared_slots

    raw = getattr(record, "declared_slots", "")
    if not str(raw or "").strip():
        return (
            frozenset(),
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            "this lane's row declares no slots, so there is no stored authority for which "
            "live processes are its own pair. An absent declaration is not an empty pair",
        )
    try:
        pins = tuple(decode_declared_slots(str(raw)))
    except Exception:  # noqa: BLE001 — a corrupt snapshot is refused, never guessed
        return (
            frozenset(),
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            "this lane's declared slots could not be decoded; a corrupt snapshot cannot say "
            "which processes belong to this lane",
        )

    if len(pins) != _PAIR_SLOT_COUNT:
        return (
            frozenset(),
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            f"this lane declares {len(pins)} slot(s); this rail acts on an exact pair of "
            f"{_PAIR_SLOT_COUNT} and will not treat a partial declaration as one. Excluding "
            f"a subset would narrow the consumer census on an incomplete answer",
        )
    if len({pin.assigned_name for pin in pins}) != _PAIR_SLOT_COUNT:
        return (
            frozenset(),
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            "this lane's declared slots repeat an assigned name, so they do not describe two "
            "distinct processes",
        )
    if len({pin.role for pin in pins}) != _PAIR_SLOT_COUNT:
        return (
            frozenset(),
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            "this lane's declared slots repeat a role; a pair is two DIFFERENT roles, and two "
            "slots claiming the same one cannot both be authoritative",
        )

    live = {agent.name: agent for agent in getattr(view, "managed_agents", ())}
    store = HerdrIdentityAttestationStore(home=home)
    derived: set = set()
    for pin in pins:
        agent = live.get(pin.assigned_name)
        try:
            attested = store.read(pin.assigned_name)
        except Exception:  # noqa: BLE001
            attested = None
        if agent is None and attested is None:
            # Neither live nor holding a row here: this slot is genuinely gone, contributes
            # nothing to the census, and excluding it would be a no-op. Not an error — but
            # not counted as ours either.
            continue
        if agent is None or attested is None:
            return (
                frozenset(),
                BLOCKED_PAIR_IDENTITY_MISMATCH,
                f"slot {pin.assigned_name!r} is "
                + ("attested here but not live" if agent is None else "live but not attested here")
                + ". The two sources disagree about this lane's own slot, so which live "
                "processes belong to this lane cannot be established",
            )
        axes = (
            (agent.workspace_id, workspace_id, "live workspace"),
            (agent.lane_id, lane, "live lane"),
            (agent.role, pin.role, "live role"),
            (agent.locator, pin.locator, "live locator"),
            (attested.workspace_id, workspace_id, "attested workspace"),
            (attested.lane_id, lane, "attested lane"),
            (attested.role, pin.role, "attested role"),
            # j#96890 §2. Without this a STALE attestation row — same name, the locator of a
            # pane that no longer exists — would still qualify the slot as ours and remove a
            # live stranger from the census.
            (attested.locator, pin.locator, "attested locator"),
        )
        # ``provider`` lives only on the declared pin: neither the herdr inventory row nor the
        # attestation record carries it (measured, not assumed). It is therefore validated as
        # part of the pair's distinctness above rather than joined here, and this note exists
        # so the omission reads as a boundary of the available evidence, not an oversight.
        for observed, expected, axis in axes:
            if str(observed) != str(expected):
                return (
                    frozenset(),
                    BLOCKED_PAIR_IDENTITY_MISMATCH,
                    f"slot {pin.assigned_name!r} has {axis} {observed!r} where this lane's "
                    f"declared pin says {expected!r}. A slot that cannot be proven to belong "
                    f"to this lane is not excluded from the consumer census",
                )
        derived.add(pin.assigned_name)
    return frozenset(derived), "", ""


def plan_lane_epoch_legacy_recovery(
    *,
    home: Path,
    view,
    workspace_id: str,
    lane: str,
    issue_id: str,
    expected_revision: int,
    decision: DecisionPointer,
    asserted_slots: Sequence[str] = (),
) -> LaneEpochRecoveryPlan:
    """Plan (never perform) the legacy recovery for one exact lane.

    Fail-closed in the order the effects would have happened, so a refusal always arrives
    before the effect it is refusing. The consumer census is evaluated ahead of the lifecycle
    shape for the same reason: a lane that IS the legacy shape but sits in a home with a live
    fleet must hear about the fleet, because that is the fact that decides whether any of this
    is possible — not a detail to mention after admitting the plan.

    ``asserted_slots`` is an ASSERTION about the lane's own pair, never the source of it
    (j#96881 F1). It is compared against the authority-derived set and any difference is a
    refusal; it can only ever make the plan stricter. The parameter is deliberately not named
    ``target_slots`` any more: the old name described what the value was used for, and what it
    was used for was the defect.
    """
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_attestation_store_maintenance import (  # noqa: E501
        CONSUMERS_NONE,
        CONSUMERS_PRESENT,
        measure_store_consumers,
    )
    from mozyo_bridge.core.state.lane_lifecycle_readonly import LaneLifecycleReader

    asserted = tuple(
        sorted({str(name).strip() for name in asserted_slots if str(name).strip()})
    )
    base = {"lane": lane, "workspace_id": workspace_id, "issue_id": issue_id}

    # 1. The census, FIRST. Reusing the maintenance module's own measurement rather than
    #    re-deriving the intersection here: j#96856 already established that the gate is
    #    `live & attested`, and a planner with a second definition of "consumer" would
    #    eventually disagree with the gate it is predicting.
    state, names = measure_store_consumers(view, home)
    if state not in (CONSUMERS_NONE, CONSUMERS_PRESENT):
        return LaneEpochRecoveryPlan(
            state=BLOCKED_CONSUMERS_UNMEASURABLE,
            asserted_slots=asserted,
            detail=(
                "the live herdr inventory or this store's rows could not be enumerated, so "
                "an empty consumer intersection cannot be proven. An unmeasured fleet is "
                "not an idle one, and admitting the plan here would authorise closing a "
                "pair on the strength of a read that failed"
            ),
            **base,
        )

    # 2. The lane's OWN pair, derived from stored authority. This must happen before the
    #    subtraction, and it must not consult the caller: the set being subtracted is exactly
    #    the set that decides whether the global blocker fires.
    record = LaneLifecycleReader(home=home).get(LaneLifecycleKey(workspace_id, lane))
    if record is None:
        return LaneEpochRecoveryPlan(
            state=BLOCKED_LANE_ABSENT,
            asserted_slots=asserted,
            detail="no lifecycle row exists for this workspace / lane in this home",
            **base,
        )
    derived, pair_refusal, pair_detail = _derive_target_pair(
        record, view, home, workspace_id, lane
    )
    if pair_refusal:
        return LaneEpochRecoveryPlan(
            state=pair_refusal, asserted_slots=asserted, detail=pair_detail, **base
        )
    own = tuple(sorted(derived))
    if asserted and asserted != own:
        return LaneEpochRecoveryPlan(
            state=BLOCKED_TARGET_SLOT_ASSERTION_FAILED,
            asserted_slots=asserted,
            target_slots=own,
            detail=(
                f"the asserted slots {list(asserted)} do not equal the pair derived from this "
                f"lane's stored release observation {list(own)}. This flag asserts what the "
                f"authority already says; it cannot introduce a slot the authority does not"
            ),
            **base,
        )

    foreign = tuple(name for name in names if name not in derived)
    if foreign:
        return LaneEpochRecoveryPlan(
            state=OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED,
            foreign_consumers=foreign,
            target_slots=own,
            asserted_slots=asserted,
            detail=(
                f"{len(foreign)} live managed agent(s) outside this lane's own pair hold a "
                f"startup self-attestation in this store ({', '.join(foreign)}). The store "
                f"migration this recovery depends on refuses while any of them is live, so "
                f"closing the target pair first would destroy it and still not reach the "
                f"migration. This is not a lane-local problem and has no lane-local fix: it "
                f"needs the global offline runtime upgrade — drain every workspace, stop the "
                f"callback supervisors, migrate the attestation store and the lifecycle "
                f"store backup-first, install the new runtime, relaunch the top coordinator "
                f"first — driven by an actor that is not itself one of these consumers"
            ),
            **base,
        )

    # 3. Only now the lane's own shape, via the adoption CAS's own predicate.
    refusal = legacy_adoption_refusal(
        record,
        expected_revision=expected_revision,
        issue_id=issue_id,
        decision=decision,
    )
    if refusal is not None:
        return LaneEpochRecoveryPlan(
            state=BLOCKED_NOT_LEGACY_SHAPE,
            lifecycle_reason=refusal.reason,
            target_slots=own,
            asserted_slots=asserted,
            detail=(
                f"the lifecycle CAS would refuse the adoption step with "
                f"{refusal.reason!r}, so the sequence cannot complete. This is the store's "
                f"own verdict, not a separate opinion about the row"
            ),
            **base,
        )
    return LaneEpochRecoveryPlan(
        state=PLAN_READY,
        steps=CANONICAL_STEPS,
        target_slots=own,
        asserted_slots=asserted,
        detail=(
            "the attested-live intersection is empty and the lane is the legacy shape, so "
            "each step below can be taken in order. This plan is only valid inside an "
            "already-armed global offline upgrade window: nothing here opens one, and the "
            "census above is evidence about the moment it was taken"
        ),
        **base,
    )


def format_recovery_plan(plan: LaneEpochRecoveryPlan) -> str:
    """Human-readable rendering (the JSON payload is the machine surface)."""
    lines = [f"herdr lane-epoch-recovery-plan: {plan.state}", f"  lane: {plan.lane}"]
    if plan.foreign_consumers:
        lines.append(f"  foreign consumers: {', '.join(plan.foreign_consumers)}")
    if plan.lifecycle_reason:
        lines.append(f"  lifecycle: {plan.lifecycle_reason}")
    if plan.detail:
        lines.append(f"  {plan.detail}")
    for step in plan.steps:
        lines.append(f"  {step}")
    lines.append("  (plan only — this command performs none of the steps above)")
    return "\n".join(lines)


__all__ = (
    "BLOCKED_CONSUMERS_UNMEASURABLE",
    "BLOCKED_LANE_ABSENT",
    "BLOCKED_NOT_LEGACY_SHAPE",
    "CANONICAL_STEPS",
    "OFFLINE_GLOBAL_RUNTIME_UPGRADE_REQUIRED",
    "PLAN_READY",
    "LaneEpochRecoveryPlan",
    "format_recovery_plan",
    "plan_lane_epoch_legacy_recovery",
)
