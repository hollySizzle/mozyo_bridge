"""Receiver-side admission of one recovery action: verify fresh, then claim once (#13910).

Design answer j#80984 (Option A + C), authoritative per j#80986.

This is the seam a receiver crosses **before its first state-changing effect** for a recovery it was
pointed at. "Actuation" is that first effect — not the agent's whole recovery round, and not only a
re-dispatch (j#80984 Disposition 2).

    fresh durable read  (ONE read: the key and the verdict describe the same instant)
      -> resolve the action's key from the record's marker   (never from pane prose)
      -> drift check: is this delivery addressed to THIS receiver, for THIS round?
      -> supersede check: is the stall the record asserts still provable NOW?
      -> claim the key                                        (CAS; exactly one winner)

Only ``admitted`` authorizes an effect. Every other outcome is a durable zero-actuation.

**Why one read, not two.** The key comes from the record's marker and the supersede verdict from the
same issue's journals. Reading twice would let the key describe one instant and the verdict another,
so a gate landing between them would be invisible — the very TOCTOU shape #13889 R2-F1 was about.
One read closes that by construction.

**Why verification precedes the claim.** A superseded recovery must not consume its key: nothing
was actuated, so nothing should be recorded as admitted. Claiming first would burn the key on a
recovery that was never performed, and — since claims are never reclaimed (j#80984 Disposition 4) —
a later legitimate delivery of that same action could then never be admitted.

**Why the receiver asserts its own identity.** ``route_identity`` / ``receiver_identity`` are passed
in and compared against the marker rather than read out of it. A key read out of the record would
match itself trivially; the question that matters is whether the agent *holding* this delivery is
the one it was addressed to. A mismatch is a conflict — a misrouted or replayed-elsewhere delivery —
and is fail-closed.

**What this does NOT claim** (j#80984 Disposition 3, the Option C boundary). The rail is advisory:
no sidecar exists, so a receiver that never calls it cannot be stopped by code
(``vibes/docs/logics/ack-completion-receiver-state.md`` ``## Sidecar の位置づけ``). Acceptance 2 holds
to the extent the standard receiver contract requires the call. Mechanical bypass prevention is a
separate residual and is **not** simulated here. A successful claim is an admission, never a task
completion — that truth lives in the Redmine gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mozyo_bridge.core.state.callback_recovery_receipt import (
    CallbackRecoveryReceipt,
    CallbackRecoveryReceiptError,
)

from ..domain.callback_recovery_key import (
    LOOKUP_ENTRY_AMBIGUOUS,
    LOOKUP_MARKER_AMBIGUOUS,
    RecoveryAdmissionKey,
    resolve_recovery_action_key,
)
from ..domain.callback_sweep_watermark import resolve_watermark
from ..domain.redmine_journal_source import (
    dispatch_generations,
    resolve_dispatch_entry_journal,
)
from .callback_sweep import source_is_fresh

#: The claim was won: this receiver may perform this recovery round's first effect, once.
ADMIT_ADMITTED = "admitted"
#: This exact action was already admitted here. A durable no-op (never re-admitted).
ADMIT_DUPLICATE = "duplicate"
#: The record's stall is no longer provable, or its round was superseded: nothing to recover.
ADMIT_SUPERSEDED = "superseded"
#: The delivery contradicts the durable record (route / receiver / anchor drift, ambiguity, or a
#: digest hit whose stored identity differs). Fail-closed: never actuate on a contradiction.
ADMIT_CONFLICT = "conflict"
#: The durable record or the authority could not be read well enough to decide. Fail-closed.
ADMIT_UNREADABLE = "unreadable"

#: Lookup reasons that mean the record CONTRADICTS itself rather than merely being unreadable.
_CONFLICT_LOOKUPS = frozenset({LOOKUP_ENTRY_AMBIGUOUS, LOOKUP_MARKER_AMBIGUOUS})


@dataclass(frozen=True)
class AdmissionOutcome:
    """What the receiver is authorized to do, and why.

    ``may_actuate`` is set explicitly at every construction site rather than derived from
    ``outcome`` by a reader: an authorization that a caller can re-infer is an authorization that a
    caller can re-infer *wrongly*. Exactly one outcome ever carries it True.
    """

    outcome: str
    may_actuate: bool
    detail: str
    key_digest: str = ""
    lookup_reason: str = ""
    recovery_action_journal: str = ""

    def as_payload(self) -> dict[str, Any]:
        """The structured, replayable outcome (the CLI's JSON body)."""
        return {
            "outcome": self.outcome,
            "may_actuate": self.may_actuate,
            "detail": self.detail,
            "key_digest": self.key_digest,
            "lookup_reason": self.lookup_reason,
            "recovery_action_journal": self.recovery_action_journal,
        }


def _drift(key: RecoveryAdmissionKey, *, workspace_id: str, route_identity: str,
           receiver_identity: str) -> tuple[str, ...]:
    """The identity fields where the caller disagrees with the durable record (pure)."""
    presented = {
        "workspace_id": str(workspace_id or "").strip(),
        "route_identity": str(route_identity or "").strip(),
        "receiver_identity": str(receiver_identity or "").strip(),
    }
    return tuple(
        sorted(name for name, value in presented.items() if value != str(getattr(key, name)))
    )


def admit_recovery(
    *,
    source: object,
    issue: str,
    recovery_action_journal: str,
    workspace_id: str,
    route_identity: str,
    receiver_identity: str,
    receipt: Optional[CallbackRecoveryReceipt] = None,
    now: Optional[str] = None,
) -> AdmissionOutcome:
    """Decide whether this receiver may actuate the recovery recorded at ``recovery_action_journal``.

    ``source`` must promise a genuinely fresh durable read per call (``fresh_read = True``): a
    frozen snapshot cannot observe a gate that landed after it was taken, so admitting on one would
    reinstate the very window this rail exists to close (#13889 R2-F1). Classification-only sources
    are refused rather than trusted.

    Returns the outcome; raises nothing for an ordinary failure. Only :data:`ADMIT_ADMITTED` carries
    ``may_actuate=True``.
    """
    issue_s = str(issue or "").strip()
    anchor = str(recovery_action_journal or "").strip()
    if not (issue_s and anchor):
        return AdmissionOutcome(
            outcome=ADMIT_UNREADABLE,
            may_actuate=False,
            detail="admission requires both an issue and the recovery action's journal id",
            recovery_action_journal=anchor,
        )
    if not source_is_fresh(source):
        return AdmissionOutcome(
            outcome=ADMIT_UNREADABLE,
            may_actuate=False,
            detail=(
                f"{type(source).__name__} does not declare fresh_read: a snapshot cannot show a "
                f"gate that landed after it was taken, so it cannot prove the recovery is still "
                f"warranted. Use a live durable source to admit"
            ),
            recovery_action_journal=anchor,
        )

    # (1) ONE fresh durable read. The key and the verdict below both come from it, so they describe
    #     the same instant and no gate can land invisibly between them.
    try:
        entries = list(source.read_entries(issue_s))
    except Exception as exc:  # noqa: BLE001 - an unreadable record must not authorize an effect
        return AdmissionOutcome(
            outcome=ADMIT_UNREADABLE,
            may_actuate=False,
            detail=(
                f"the durable record could not be read ({type(exc).__name__}: {exc}); the receiver "
                f"abstains rather than actuate a recovery it cannot verify"
            ),
            recovery_action_journal=anchor,
        )

    # (2) The action's identity, derived from the record's structured marker (never pane prose).
    lookup = resolve_recovery_action_key(entries, recovery_action_journal=anchor)
    if not lookup.resolved:
        return AdmissionOutcome(
            outcome=(
                ADMIT_CONFLICT if lookup.reason in _CONFLICT_LOOKUPS else ADMIT_UNREADABLE
            ),
            may_actuate=False,
            detail=lookup.detail,
            lookup_reason=lookup.reason,
            recovery_action_journal=anchor,
        )
    key = lookup.key

    # (3) Is this delivery addressed to THIS receiver? A key that only matches itself proves nothing.
    drifted = _drift(
        key,
        workspace_id=workspace_id,
        route_identity=route_identity,
        receiver_identity=receiver_identity,
    )
    if drifted:
        return AdmissionOutcome(
            outcome=ADMIT_CONFLICT,
            may_actuate=False,
            detail=(
                f"this delivery does not match the durable recovery action in {list(drifted)!r}: "
                f"the record addresses a different workspace / route / receiver, so admitting it "
                f"here would actuate someone else's recovery"
            ),
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )

    # (4) Is the stall the record asserts still provable NOW? Same predicate the sender applies at
    #     its final live read, so the receiver's verdict is symmetric with the sender's.
    dispatch = resolve_dispatch_entry_journal(
        entries, lane=key.lane_id, lane_generation=key.lane_generation
    )
    generations = dispatch_generations(entries, lane=key.lane_id)
    watermark = resolve_watermark(
        entries,
        dispatch_journal=dispatch,
        lane=key.lane_id,
        lane_generation=key.lane_generation,
        latest_generation=generations[-1] if generations else 0,
    )
    if not watermark.anchored or watermark.dispatch_journal != key.original_dispatch_anchor:
        return AdmissionOutcome(
            outcome=ADMIT_CONFLICT,
            may_actuate=False,
            detail=(
                f"the record's dispatch anchor {key.original_dispatch_anchor!r} does not resolve "
                f"against the live record (now {watermark.dispatch_journal!r}): the recovery "
                f"describes a round this issue no longer shows"
            ),
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )
    if watermark.superseded:
        return AdmissionOutcome(
            outcome=ADMIT_SUPERSEDED,
            may_actuate=False,
            detail=(
                f"a newer dispatch round opened (generation {key.lane_generation} -> "
                f"{watermark.latest_generation}); this recovery describes a superseded round and "
                f"is not actuated"
            ),
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )
    if not watermark.stall_provable:
        landed = [j for j, _ in watermark.progress]
        return AdmissionOutcome(
            outcome=ADMIT_SUPERSEDED,
            may_actuate=False,
            detail=(
                f"the stall this recovery asserts is no longer provable "
                f"(progress={landed or '-'} opaque={list(watermark.opaque) or '-'} after anchor "
                f"j#{key.original_dispatch_anchor}): the lane advanced between the send and this "
                f"admission, so the recovery is refused — this is the #13889 read->send window, "
                f"absorbed here"
            ),
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )

    # (5) Claim. Two concurrent receivers serialize here; exactly one performs the effect.
    store = receipt if receipt is not None else CallbackRecoveryReceipt()
    try:
        claim = store.claim(key, now=now)
    except CallbackRecoveryReceiptError as exc:
        return AdmissionOutcome(
            outcome=ADMIT_UNREADABLE,
            may_actuate=False,
            detail=(
                f"the admission authority is unavailable ({exc}); zero-actuation rather than "
                f"perform an effect that may already have been performed"
            ),
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )
    if claim.conflict:
        return AdmissionOutcome(
            outcome=ADMIT_CONFLICT,
            may_actuate=False,
            detail=claim.detail,
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )
    if not claim.won:
        return AdmissionOutcome(
            outcome=ADMIT_DUPLICATE,
            may_actuate=False,
            detail=claim.detail,
            key_digest=key.digest(),
            recovery_action_journal=anchor,
        )
    return AdmissionOutcome(
        outcome=ADMIT_ADMITTED,
        may_actuate=True,
        detail=(
            f"admitted: this receiver performs the recovery round for anchor "
            f"j#{key.original_dispatch_anchor} once. Admission is not completion — record the "
            f"round's outcome as a durable Redmine gate"
        ),
        key_digest=key.digest(),
        recovery_action_journal=anchor,
    )


__all__ = (
    "ADMIT_ADMITTED",
    "ADMIT_DUPLICATE",
    "ADMIT_SUPERSEDED",
    "ADMIT_CONFLICT",
    "ADMIT_UNREADABLE",
    "AdmissionOutcome",
    "admit_recovery",
)
