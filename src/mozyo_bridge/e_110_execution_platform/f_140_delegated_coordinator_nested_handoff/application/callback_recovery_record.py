"""The sweep's durable record: written at most once per anchor, or not at all (#13889).

Publication is a **non-retryable outbox act**, not a retryable lease act — the distinction this
module exists to encode, and the one whose absence produced R9-F1's duplicate records.

The attempt lease serializes *who tries*; it has a TTL, so a merely-slow owner can be reclaimed
while still running, and both owners then reach this point believing they may write. No amount of
re-checking the lease closes that window: the check and the PUT cannot be made atomic against a
resource that does not honour fencing tokens. So the record identity itself is reserved, in a fence
that has **no TTL and is never reclaimed** (:mod:`...callback_publication_fence`). A lingering
``reserved`` row may be an owner mid-PUT; treating it as crash residue is exactly the mistake that
duplicated records, so this side stalls the anchor for an operator instead. Safety over
availability, deliberately (disposition j#80383, option (d)).

Order, and each step's reason:

    reserve the exact record identity   (never reclaimed -> at most one writer, ever)
      -> re-verify lease ownership      (cheap; drops a lost owner before it touches Redmine)
      -> pre-write read                 (a gate landing here folds to a zero-send resolution)
      -> PUT
      -> read back exactly one          (proves what landed; ambiguity fails closed)
      -> mark published

A PUT that raises is marked ``uncertain``, never retried: only Redmine knows whether it landed.
"""

from __future__ import annotations

from typing import Any, Callable

from mozyo_bridge.core.state.callback_publication_fence import (
    PUBLICATION_PUBLISHED,
    CallbackPublicationFence,
    PublicationKey,
)

from ..domain.redmine_journal_source import dispatch_generations

from ..domain.callback_sweep_watermark import (
    STATE_NO_PROGRESS_AFTER_HANDOFF,
    SweepWatermark,
    render_sweep_record_note,
    resolve_watermark,
    sweep_record_journals,
)


class RecordOwnershipLostError(RuntimeError):
    """The attempt lease lapsed before the record write, so nothing was published (R7-F1).

    Distinct from a write failure: nothing is broken and nothing was attempted — this sweep simply
    no longer owns the anchor, and the owner that does will publish.
    """


class RecordPublicationHeldError(RuntimeError):
    """This exact record is already reserved / uncertain / published, so this sweep writes nothing.

    Never a reason to retry: a lingering reservation may be an owner mid-PUT, and reclaiming it is
    precisely what produced duplicate records (R9-F1).
    """


class RecordPublicationUncertainError(RuntimeError):
    """A PUT was started and its fate is unknown. Never auto-retried; operator reconcile only."""


class RecordSupersededError(RuntimeError):
    """The recorder's own pre-write read contradicts the verdict, so it wrote nothing (R4-F1).

    Distinct from a write failure: nothing went wrong and nothing is broken — the lane simply is
    not stalled after all, discovered at the last durable read before the write. The caller folds
    it into the first-pass resolution, which is what acceptance 3 asks for: no false stall record
    to correct later.
    """


def build_recovery_recorder(
    *,
    source: object,
    issue: str,
    lane: str,
    lane_generation: object,
    post_note: Callable[[str, str], object],
    grant_is_live: Callable[[], bool],
    publication_fence: "CallbackPublicationFence",
    workspace_id: str,
) -> Callable[[dict, SweepWatermark], str]:
    """Build the production ``record_fn``: write the sweep record, then RESOLVE its journal id.

    Review R2-F3. Redmine's note write returns ``204 No Content`` with no journal id, so the writer
    cannot learn where its own record landed. This uses the same write -> re-read -> resolve-by-
    marker pattern :mod:`...reconcile_dispatch_writer` established for the IR anchor: the record
    carries an identifying marker, and its OWNING entry's journal id (the durable authority, never a
    self-reported field) is read back afterwards.

    Idempotent by pre-read: an already-recorded resolution is recovered rather than duplicated, so
    repeated sweeps at the same verdict do not spam the issue. Keyed by ``outcome`` as well as the
    dispatch anchor, so a legitimately changed verdict (``stall_unprovable`` -> a landed gate) is
    recorded once each. Returns ``""`` when the record cannot be resolved — :func:`sweep_once` then
    cancels the send rather than perform an unrecorded one.

    ``grant_is_live()`` conditions THE WRITE ITSELF on still holding the attempt lease (review
    R7-F1). Checking ownership before *calling* the recorder is not enough and was the previous
    defect: this function then performs a full Redmine pre-read before ``post_note``, so the gap
    between that check and the actual publication is not microscopic — it is a whole network
    round-trip, which is ample time for a slow owner's TTL to lapse and the anchor to be reclaimed.
    The check therefore sits where it can be honest: immediately before the write, after every read
    this function performs. A lapsed grant raises :class:`RecordOwnershipLostError` and writes
    nothing.
    """

    def _record(result: dict, watermark: SweepWatermark) -> str:
        outcome = str(result.get("state", "") or "").strip()
        anchor = str(watermark.dispatch_journal or "").strip()
        if not (outcome and anchor):
            return ""
        keys = dict(
            lane=lane, lane_generation=lane_generation, dispatch_anchor=anchor, outcome=outcome
        )
        pre = list(source.read_entries(str(issue)))
        existing = sweep_record_journals(pre, **keys)
        if len(existing) == 1:
            return existing[0]  # already recorded this resolution: recover, write nothing
        if len(existing) >= 2:
            return ""  # ambiguous: fail closed rather than pick one
        # Review R4-F1: this pre-read is a durable OBSERVATION, not just an idempotency lookup.
        # Writing a stall record while it already shows a landed gate produces exactly the
        # false-verdict-then-correction pair acceptance 3 forbids, so the verdict is re-checked
        # against the very entries about to be written against, and a superseded stall aborts
        # BEFORE the write. The caller folds this into the first-pass resolution.
        if outcome == STATE_NO_PROGRESS_AFTER_HANDOFF:
            fresh = resolve_watermark(
                pre,
                dispatch_journal=anchor,
                lane=lane,
                lane_generation=lane_generation,
                latest_generation=(dispatch_generations(pre, lane=lane) or (0,))[-1],
            )
            if not fresh.stall_provable or fresh.superseded:
                raise RecordSupersededError(
                    f"the stall verdict no longer holds at write time (progress="
                    f"{[j for j, _ in fresh.progress]} opaque={list(fresh.opaque)} "
                    f"superseded={fresh.superseded}); no stall record was written"
                )
        # The attempt lease still gates whether this sweep should be doing anything at all, but it
        # is NOT what makes the write at-most-once (R9-F1): a lapse here only means someone else is
        # now doing the work.
        if not grant_is_live():
            raise RecordOwnershipLostError(
                "the attempt lease lapsed before the record write; publishing nothing"
            )
        # THE publication authority (j#80383 option (d)): reserve this exact record identity before
        # the PUT. Unlike the lease this is never reclaimed on a timer, so a suspended or crashed
        # owner keeps its claim and nobody else writes the same record. That converts arbitrary
        # suspension from a duplicate into an availability loss an operator reconciles.
        pub_key = PublicationKey(
            workspace_id=str(workspace_id), lane_id=str(lane), issue=str(issue),
            lane_generation=str(lane_generation), dispatch_anchor=anchor, outcome=outcome,
        )
        reservation = publication_fence.reserve(pub_key)
        if not reservation.may_publish:
            if reservation.prior_state == PUBLICATION_PUBLISHED and reservation.journal_id:
                return reservation.journal_id      # idempotent recovery of our own prior write
            raise RecordPublicationHeldError(
                f"this record is already {reservation.prior_state}; publishing nothing "
                f"({reservation.detail})"
            )
        try:
            post_note(str(issue), render_sweep_record_note(_record_body(result), **keys))
        except Exception as exc:  # noqa: BLE001 - a PUT of unknown fate is NEVER auto-retried
            publication_fence.mark_uncertain(
                pub_key, reservation.token, detail=f"PUT raised {type(exc).__name__}"
            )
            raise RecordPublicationUncertainError(
                f"the record PUT raised {type(exc).__name__}; its fate is unknown and it will not "
                f"be retried automatically — reconcile against Redmine"
            ) from exc
        try:
            written = sweep_record_journals(source.read_entries(str(issue)), **keys)
        except Exception as exc:  # noqa: BLE001 - the PUT may well have landed; do not retry
            publication_fence.mark_uncertain(
                pub_key, reservation.token, detail=f"read-back raised {type(exc).__name__}"
            )
            raise RecordPublicationUncertainError(
                f"the record was PUT but the read-back raised {type(exc).__name__}; its fate is "
                f"unknown and it will not be retried automatically"
            ) from exc
        if len(written) != 1:
            publication_fence.mark_uncertain(
                pub_key, reservation.token, detail=f"read-back resolved {len(written)} records"
            )
            raise RecordPublicationUncertainError(
                f"the record PUT resolved {len(written)} records, not exactly one; its fate is "
                f"ambiguous and it will not be retried automatically"
            )
        publication_fence.mark_published(pub_key, reservation.token, written[0])
        return written[0]

    return _record


def _record_body(result: dict) -> str:
    """The human-readable sweep record: the classification, what was missing, the retry target."""
    lines = [
        "## Gate: progress_log — callback sweep record",
        "",
        f"- **state**: `{result.get('state', '')}`",
        f"- **is_stall**: {result.get('is_stall', False)}",
        f"- **dispatch_anchor**: j#{result.get('dispatch_journal', '') or '-'}",
        f"- **callback**: `{result.get('callback', '')}`",
        f"- **send_reason**: `{result.get('send_reason', '') or 'pending'}`",
        "",
        f"{result.get('summary', '')}",
    ]
    progress = result.get("progress_journals") or []
    if progress:
        lines += ["", "### 観測した durable progress"] + [
            f"- j#{p['journal']} `{p['kind']}`" for p in progress
        ]
    opaque = result.get("opaque_journals") or []
    if opaque:
        lines += [
            "",
            "### marker を持たない post-anchor journal (分類不能)",
            "- " + ", ".join(f"j#{j}" for j in opaque),
        ]
    steps = result.get("recovery") or []
    if steps:
        lines += ["", "### recovery"] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines += [
        "",
        "本 record は coordinator の sweep 記録であり、worker progress ではない "
        "(marker kind は `callback_sweep_record`)。",
    ]
    return "\n".join(lines)
