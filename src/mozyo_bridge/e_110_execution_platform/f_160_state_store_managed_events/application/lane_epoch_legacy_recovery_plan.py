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
#: declared-slot snapshot is absent, malformed, or not a coherent gateway+worker pair.
#: Without it there is no
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
    #: The lane's own slots as DERIVED from its current-generation declared slots and joined
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


def _derive_target_pair(record, view, home: Path, workspace_id: str, lane: str):
    """``(names, refusal_state, detail)`` — the lane's OWN pair, from stored authority.

    The exclusion set that decides whether the global blocker fires, so it must not come from
    the caller. j#96881 F1 measured why: the first version subtracted a caller-supplied
    ``--target-slot`` list unconditionally, so naming every consumer turned
    ``offline_global_runtime_upgrade_required`` into ``plan_ready``.

    **The authority is ``declared_slots``, not ``release_observation``** (j#96895, measured
    against the real #14755 row: observation length 0, declared slots length 532). The
    observation is the ORIGINAL release's snapshot; after a bootstrap or pin repair it is past
    evidence, not the current generation's pair. Release evidence is not consulted here at
    all — a rail with two authorities answers from whichever happens to be populated.

    **``role`` and ``provider`` are different axes and must not be joined to each other**
    (j#96911 F1). The real #14755 row declares ``role=gateway/worker`` with
    ``provider=codex/claude``, while the live inventory row and the startup attestation both
    carry the PROVIDER in their own ``role`` token. Comparing ``live.role`` to ``pin.role``
    therefore compared ``codex`` against ``gateway`` and made the real dry-run refuse
    ``blocked_pair_identity_mismatch`` — the intended blocker was unreachable on the one lane
    this rail exists for. The pair's slot identity comes from
    :func:`...lane_pin_role.read_declared_pin_pair` (which already owns the canonical
    ``{gateway, worker}`` vocabulary, the legacy spellings, and every ambiguous shape), and
    the provider is what gets joined against live and attested rows.

    Every axis is byte-exact across three sources — stored pin, live inventory row, startup
    attestation — and each pin must resolve to **exactly one** of each. A name alone proves
    nothing about which process answers to it, and a duplicate makes "which one" a guess.
    Absent on both sides is a refusal too: this rail's step 2 closes BOTH slots, so a pair it
    cannot fully locate is a pair it has not identified, and excluding what it did find would
    narrow the census on a partial answer.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HerdrIdentityAttestationStore,
    )
    from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair

    pair = read_declared_pin_pair(record)
    if not pair.ok:
        return (
            frozenset(),
            BLOCKED_PAIR_AUTHORITY_UNAVAILABLE,
            f"this lane's declared slots do not name an unambiguous gateway+worker pair "
            f"({pair.reason}), so there is no stored authority for which live processes are "
            f"its own. Excluding a partial answer would narrow the consumer census",
        )

    store = HerdrIdentityAttestationStore(home=home)
    agents = tuple(getattr(view, "managed_agents", ()))
    derived: set = set()
    for pin in (pair.gateway, pair.worker):
        name = pin.assigned_name
        matches = [agent for agent in agents if agent.name == name]
        if len(matches) > 1:
            return (
                frozenset(),
                BLOCKED_PAIR_IDENTITY_MISMATCH,
                f"the live inventory holds {len(matches)} rows named {name!r}; which of them "
                f"is this lane's slot cannot be decided, and guessing would exclude a "
                f"stranger from the consumer census",
            )
        try:
            attested = store.read(name)
        except Exception:  # noqa: BLE001
            attested = None
        if not matches or attested is None:
            return (
                frozenset(),
                BLOCKED_PAIR_IDENTITY_MISMATCH,
                f"this lane's declared {pin.role} slot {name!r} is "
                + (
                    "neither live nor attested here"
                    if not matches and attested is None
                    else "attested here but not live"
                    if not matches
                    else "live but not attested here"
                )
                + ". The rail closes BOTH slots of the exact pair, so a pair it cannot fully "
                "locate is one it has not identified",
            )
        agent = matches[0]
        # NOTE the axis names. `pin.provider` is joined against the live and attested `role`
        # tokens, because those surfaces spell the provider there; `pin.role` is the
        # gateway/worker slot and has no counterpart on either. Joining them to each other is
        # the j#96911 F1 defect.
        axes = (
            (agent.workspace_id, workspace_id, "live workspace"),
            (agent.lane_id, lane, "live lane"),
            (agent.role, pin.provider, "live provider"),
            (agent.locator, pin.locator, "live locator"),
            (attested.workspace_id, workspace_id, "attested workspace"),
            (attested.lane_id, lane, "attested lane"),
            (attested.role, pin.provider, "attested provider"),
            # j#96890 §2. Without this a STALE attestation row — same name, the locator of a
            # pane that no longer exists — would still qualify the slot as ours and remove a
            # live stranger from the census.
            (attested.locator, pin.locator, "attested locator"),
        )
        for observed, expected, axis in axes:
            if str(observed) != str(expected):
                return (
                    frozenset(),
                    BLOCKED_PAIR_IDENTITY_MISMATCH,
                    f"slot {name!r} has {axis} {observed!r} where this lane's declared "
                    f"{pin.role} pin says {expected!r}. A slot that cannot be proven to "
                    f"belong to this lane is not excluded from the consumer census",
                )
        derived.add(name)
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

    # Multiplicity is PRESERVED (Redmine #14756 review j#96949 F3). The earlier
    # `sorted(set(...))` silently collapsed repeats, so asserting `gateway, worker, worker`
    # — three slots for a two-slot pair — normalised to the correct two and returned
    # `plan_ready`. An assertion that quietly edits itself into agreement is not an
    # assertion; a caller who names a slot twice has said something the authority does not,
    # and is told so.
    # The RAW input is inspected before any normalisation (Redmine #14756 review j#96971
    # R11-F8). Filtering empties first made `gateway, worker, ""` — three assertions for a
    # two-slot pair — collapse to a correct-looking two and pass; the cardinality the caller
    # got wrong disappeared before anything could compare it. Padding is refused for the same
    # reason a padded epoch token is: it is a spelling no authority produced.
    raw_asserted = tuple(asserted_slots or ())
    malformed = [
        name
        for name in raw_asserted
        if not isinstance(name, str) or not name or name != name.strip()
    ]
    if malformed:
        return LaneEpochRecoveryPlan(
            state=BLOCKED_TARGET_SLOT_ASSERTION_FAILED,
            asserted_slots=tuple(str(name) for name in raw_asserted),
            detail=(
                "an asserted slot is empty, padded, or not a string. An assertion is compared "
                "to the authority byte-for-byte, so a token that no declaration could carry "
                "is refused rather than trimmed into one that might match"
            ),
            **{"lane": lane, "workspace_id": workspace_id, "issue_id": issue_id},
        )
    asserted = tuple(sorted(raw_asserted))
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
    if asserted and (asserted != own or len(set(raw_asserted)) != len(raw_asserted)):
        return LaneEpochRecoveryPlan(
            state=BLOCKED_TARGET_SLOT_ASSERTION_FAILED,
            asserted_slots=asserted,
            target_slots=own,
            detail=(
                f"the asserted slots {list(asserted)} do not equal the pair derived from "
                f"this lane's declared slots {list(own)}. This flag asserts what the "
                f"authority already says; it cannot introduce a slot the authority does not, "
                f"and a repeated slot is a difference rather than a spelling of the same set"
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
