"""Production callback runtime: one pass + bounded wake loop (Redmine #13520 review F1).

The runnable path the review (j#75147 F1) required: a background runtime that ties the stable
Herdr CLI-event wake -> exact Redmine re-read -> ingest/claim -> **one** semantic handoff send
-> durable outcome, without an LLM turn poll and without exposing raw Herdr to an LLM role.

- :func:`run_once` is a single production pass: (optionally) ingest freshly-observed
  handoff-worthy gate candidates against their exact source journal, deliver the pending
  outbox (one send per row through the injected sender), then sweep the backlog. It returns a
  redaction-safe report. This is what a ``workflow callbacks --run-once`` invocation runs and
  what one wake iteration does.
- :func:`watch` is the bounded daemon loop: it drives :func:`...callback_wake.resolve_wake` for
  a Herdr-event wake hint and runs a pass on **every** outcome (a Herdr timeout / restart still
  re-reads Redmine — the event is only a hint), for a bounded number of iterations so it never
  becomes an unbounded LLM-turn poll. The wait primitive and the pass are injected.

All I/O is injected (the processor's source / store / sender, the wake ``wait_fn``); the runtime
holds no LLM turn and resolves nothing raw itself.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.callback_outbox import CALLBACK_CLAIM_LEASE_SECONDS, CallbackOutboxRow
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
    resolve_wake,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
    markers_from_source,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    OwningLaneBinding,
    ReviewReturnPlan,
    encode_review_return_payload,
    plan_review_returns,
)

#: The default callback route for a handoff-worthy gate: the coordinator (a sublane callback
#: goes to the coordinator lane, per the workflow doctrine).
DEFAULT_CALLBACK_ROUTE = "coordinator"


def discover_candidates(
    source: RedmineJournalSource,
    issue: str,
    *,
    route: str = DEFAULT_CALLBACK_ROUTE,
    workspace_id: str = "",
    target_lane: str = "",
    target_receiver: str = "",
    target_generation: str = "",
) -> list[CallbackCandidate]:
    """Discover callback candidates from a source issue's structured gate markers (#13520 F1-R1).

    Reads the issue's journals from ``source`` and extracts every handoff-worthy structured gate
    marker (:func:`...redmine_journal_source.markers_from_source` — the machine ``[mozyo:...]``
    token, never prose), turning each into a :class:`CallbackCandidate` targeting ``route`` (the
    coordinator by default). This is the production discovery the review required: a real
    handoff-worthy durable gate update on the issue becomes a callback candidate, deduped
    downstream by the outbox UNIQUE fence (re-discovering the same gate enqueues no new row). A
    read failure propagates to the caller (the processor's ingest handles it fail-closed per
    candidate); an issue with no gate marker yields ``[]`` (nothing to deliver — never a guess).
    """
    candidates: list[CallbackCandidate] = []
    for marker in markers_from_source(source, issue):
        candidates.append(
            CallbackCandidate(
                issue=str(marker.issue).strip(),
                journal=str(marker.journal).strip(),
                callback_route=route,
                notification_kind=marker.gate,
                workspace_id=str(workspace_id or "").strip(),
                target_lane=str(target_lane or "").strip(),
                target_receiver=str(target_receiver or "").strip(),
                target_generation=str(target_generation or "").strip(),
            )
        )
    return candidates


def discover_review_returns(
    source: RedmineJournalSource,
    issue: str,
    owner: OwningLaneBinding,
    *,
    workspace_id: str = "",
    dispatch_anchor_journal: Optional[str] = None,
) -> tuple[list[CallbackCandidate], list[ReviewReturnPlan]]:
    """Discover correlated review_result return candidates for an issue (Redmine #13684).

    Reads the issue's structured gate markers and asks the pure
    :func:`...domain.review_return_route.plan_review_returns` policy which review_result (if any) is
    returnable to its durable owning-lane Codex gateway, given ``owner`` (the #13681/#13689 owning-lane
    binding + generation + gateway receiver the caller already resolved). Only a :data:`RETURN_OK` plan
    becomes a :class:`CallbackCandidate` — carrying the ``review_return:<lane>`` route (a distinct
    idempotency key from the coordinator callback) and the durable expected target tuple
    (lane / gateway receiver / owning-lane generation) the background_service delivery authority binds
    the re-resolved live target + independently-read live generation to.

    ``dispatch_anchor_journal`` (Redmine #13974) is the current owning lane+generation's dispatch
    anchor. When supplied (the fenced production supervisor), a review_result whose review round
    predates the anchor is a previous-generation round and is refused (not enqueued) — the missing
    admission-edge generation fence that let an old review_result be retargeted onto a new lane
    generation. ``None`` (the default) leaves discovery unfenced (behavior unchanged); a
    supplied-but-unresolvable anchor (``""``) fails closed (no return candidate this pass).

    Returns ``(candidates, plans)``: the candidates to enqueue, plus every plan (incl. refusals) so the
    caller can record why nothing was returned (observability). A read failure propagates to the caller
    (handled fail-closed per candidate downstream); an issue with no returnable review_result yields no
    candidate — never a guess.
    """
    markers = markers_from_source(source, issue)
    plans = list(
        plan_review_returns(markers, issue, owner, dispatch_anchor_journal=dispatch_anchor_journal)
    )
    candidates: list[CallbackCandidate] = []
    for plan in plans:
        if not plan.emit:
            continue
        candidates.append(
            CallbackCandidate(
                issue=str(issue).strip(),
                journal=str(plan.review_journal).strip(),
                callback_route=plan.callback_route,
                notification_kind="review_result",
                # The correlated review_request (action identity) + the reviewed target_head (#13974)
                # the row is bound to, persisted on the outbox row so the send authority re-verifies the
                # round AND the head against the current review generation at action time (R1-F2 / j#81454).
                payload=encode_review_return_payload(
                    plan.review_request_journal, plan.target_head
                ),
                workspace_id=str(workspace_id or "").strip(),
                target_lane=plan.target_lane,
                target_receiver=plan.target_receiver,
                target_generation=plan.target_generation,
            )
        )
    return candidates, plans


def run_once(
    processor: CallbackOutboxProcessor,
    sender: Callable[[CallbackOutboxRow], str],
    *,
    candidates: Sequence[CallbackCandidate] = (),
    cursor: Optional[str] = None,
    stale_seconds: int = CALLBACK_CLAIM_LEASE_SECONDS,
    now: Optional[str] = None,
    send_fence_fn: "Optional[Callable[[CallbackOutboxRow], tuple[bool, str]]]" = None,
    issue: Optional[str] = None,
) -> dict:
    """Run one production callback pass (ingest -> deliver-once -> sweep); return a report.

    ``candidates`` are the freshly-observed handoff-worthy gate transitions to classify + enqueue
    (empty when this pass only drains the existing outbox). Delivery fires **one** send per
    claimed pending row through ``sender`` (the injected real / fake port), fenced by the outbox
    so a repeat / crash / concurrent processor never duplicates. ``send_fence_fn`` (Redmine #13968
    R2-F1) is an optional per-row send-edge fence forwarded to :meth:`...deliver` — a fenced row is
    zero-send + terminally uncertain, which stops a pre-existing / recovered historical backlog row
    that the caller's ingest-side candidate fence never saw. The final sweep surfaces the pending +
    dead-letter backlog once. The report is redaction-safe (no pane id / credential).
    """
    report: dict = {}
    if candidates:
        report["ingest"] = processor.ingest(candidates, cursor=cursor, now=now).as_payload()
    report["deliver"] = processor.deliver(
        sender, stale_seconds=stale_seconds, now=now, send_fence_fn=send_fence_fn, issue=issue
    ).as_payload()
    report["sweep"] = processor.sweep(stale_seconds=stale_seconds, now=now).as_payload()
    return report


def watch(
    wait_fn: Callable[[], object],
    run_pass: Callable[[], dict],
    *,
    max_passes: int,
    wake_detail: str = "",
) -> list:
    """Bounded Herdr-event wake loop: drive one pass per wake, up to ``max_passes`` iterations.

    Each iteration resolves a wake (:func:`...callback_wake.resolve_wake` over ``wait_fn`` — the
    stable Herdr wait primitive), then runs ``run_pass`` **regardless** of the wake outcome
    (woke / timed_out / restart-error), because the Herdr event is a hint and Redmine is the
    authority (``should_reread`` is always True). ``max_passes`` bounds the loop so a background
    watcher never degrades into an unbounded poll; the real daemon calls this repeatedly under
    operator supervision. Returns one ``{wake, pass}`` record per iteration.

    Background lifecycle resilience (#13520 review F1b): a single pass that raises (a transient
    Redmine read / store error) is caught and recorded as ``{"error": <type>}`` rather than
    crashing the watcher or holding a turn — the loop survives to its next bounded wake, exactly
    as it survives a wait restart. Correctness is unaffected: the outbox fence makes every pass
    idempotent, so a skipped/failed pass loses nothing (the next pass re-reads Redmine).
    """
    results: list = []
    for _ in range(max(0, int(max_passes))):
        signal = resolve_wake(wait_fn, detail=wake_detail)
        # should_reread is always True; the wake outcome is telemetry, the pass is unconditional.
        try:
            outcome: dict = run_pass()
        except Exception as exc:  # noqa: BLE001 - a failed pass must not kill the background watcher
            outcome = {"error": type(exc).__name__}
        results.append({"wake": signal.kind, "pass": outcome})
    return results


__all__ = (
    "DEFAULT_CALLBACK_ROUTE",
    "discover_candidates",
    "discover_review_returns",
    "run_once",
    "watch",
)
