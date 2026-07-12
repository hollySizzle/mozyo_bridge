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

#: The default callback route for a handoff-worthy gate: the coordinator (a sublane callback
#: goes to the coordinator lane, per the workflow doctrine).
DEFAULT_CALLBACK_ROUTE = "coordinator"


def discover_candidates(
    source: RedmineJournalSource,
    issue: str,
    *,
    route: str = DEFAULT_CALLBACK_ROUTE,
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
            )
        )
    return candidates


def run_once(
    processor: CallbackOutboxProcessor,
    sender: Callable[[CallbackOutboxRow], str],
    *,
    candidates: Sequence[CallbackCandidate] = (),
    cursor: Optional[str] = None,
    stale_seconds: int = CALLBACK_CLAIM_LEASE_SECONDS,
    now: Optional[str] = None,
) -> dict:
    """Run one production callback pass (ingest -> deliver-once -> sweep); return a report.

    ``candidates`` are the freshly-observed handoff-worthy gate transitions to classify + enqueue
    (empty when this pass only drains the existing outbox). Delivery fires **one** send per
    claimed pending row through ``sender`` (the injected real / fake port), fenced by the outbox
    so a repeat / crash / concurrent processor never duplicates. The final sweep surfaces the
    pending + dead-letter backlog once. The report is redaction-safe (no pane id / credential).
    """
    report: dict = {}
    if candidates:
        report["ingest"] = processor.ingest(candidates, cursor=cursor, now=now).as_payload()
    report["deliver"] = processor.deliver(
        sender, stale_seconds=stale_seconds, now=now
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


__all__ = ("DEFAULT_CALLBACK_ROUTE", "discover_candidates", "run_once", "watch")
