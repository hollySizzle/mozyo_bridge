"""CLI surface for `workflow callbacks` — zero-wait callback outbox (Redmine #13520 / #13518).

`mozyo-bridge workflow callbacks` is the mozyo **semantic facade** over the zero-wait callback
delivery bounded context (design answer j#75098). It exposes the three outbox operations —
ingest, deliver, sweep — through one command so an agent / operator never touches a raw Herdr /
tmux primitive (US #13518 acceptance: the tool surface is limited to mozyo semantic
operations). The correctness lives in the store / domain / orchestrator; this is the thin
argparse edge that wires them to the live Redmine journal source and the home-scoped outbox.

Three mutually-exclusive actions:

- ``--sweep`` — the **fresh-turn sweep** (read-only actuation-wise): reconcile crashed
  ``inflight`` rows (pre-send -> pending, post-send -> uncertain) and surface the pending +
  dead-letter backlog once, so a single fresh LLM turn reads the source journal. Sends nothing.
- ``--ingest`` — classify each ``--candidate ISSUE:JOURNAL:ROUTE[:KIND]`` against its **exact
  source journal** (from ``--redmine-json`` snapshot or ``--poll --source-issue`` live) and
  idempotently enqueue it (classified -> pending; unclassified -> dead_letter). Sends nothing.
- ``--deliver`` — recover crashed rows, claim pending rows (single winner), and fire **one**
  send per row through a configured sender. The bare CLI has **no** default sender and
  fail-closes: live callback actuation runs through the controlled #13521 harness (QA-only
  anchors), never a bare-CLI invocation that could re-send a completed request.

Always exits 0 for a successful read/record; a fail-closed actuation refusal or a source /
store error is a ``SystemExit`` with a redacted message (never a credential / URL / pane id).
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path
from typing import Callable, Optional

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxRow
from mozyo_bridge.core.state.workflow_runtime_store import workflow_runtime_store_path
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
    LiveRedmineJournalError,
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    RedmineJournalSource,
)


def _outbox_from_args(args: argparse.Namespace) -> CallbackOutbox:
    """Build the callback outbox over ``--store-path`` (test/debug) or the home default."""
    raw = (getattr(args, "store_path", None) or "").strip()
    path = Path(raw) if raw else workflow_runtime_store_path()
    return CallbackOutbox(path=path)


def _live_journal_source(args: argparse.Namespace) -> LiveRedmineJournalSource:
    """Build the live poll source from daemon-trusted credentials (patchable test seam)."""
    since = (getattr(args, "since", None) or "").strip() or None
    return LiveRedmineJournalSource.from_environment(since=since)


def _journal_source(args: argparse.Namespace) -> RedmineJournalSource:
    """Resolve the exact-journal source for classification: ``--redmine-json`` or ``--poll``.

    ``--redmine-json`` reads a fetched ``/issues/<id>.json?include=journals`` snapshot (the
    same shape ``workflow watch`` accepts); ``--poll`` reads live over daemon-trusted
    credentials. Exactly one must be given for ``--ingest`` (the classifier must read the exact
    source journal — the journal is the authority, never a guess).
    """
    raw = (getattr(args, "redmine_json", None) or "").strip()
    if raw:
        payload = _json.loads(Path(raw).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit(
                f"--redmine-json {raw!r} must contain a Redmine issue-detail object, not a "
                f"{type(payload).__name__}"
            )
        return MappingRedmineJournalSource(payload=payload)
    if getattr(args, "poll", False):
        try:
            source = _live_journal_source(args)
        except LiveRedmineJournalError as exc:
            raise SystemExit(str(exc)) from exc
        for warning in getattr(source, "warnings", ()):  # redacted; never key / URL
            print(f"warning: {warning}", file=sys.stderr)
        return source
    raise SystemExit(
        "--ingest requires a journal source: --redmine-json PATH (a fetched issue-detail "
        "snapshot) or --poll (live, credential-gated). The exact source journal is the gate "
        "authority; a callback is never classified from a notification alone."
    )


def _callback_sender(args: argparse.Namespace) -> Callable[[CallbackOutboxRow], str]:
    """Resolve the one-send callback sender for ``--deliver`` (fail-closed by default).

    The bare CLI ships **no** default sender: live callback actuation must go through the
    controlled #13521 scenario / live harness, which injects a sender bound to QA-only durable
    anchors / targets so it never re-sends a completed implementation request (j#75108 live
    safety). A bare ``--deliver`` therefore fail-closes rather than actuate a real handoff. The
    harness / a test patches this seam to supply the real (or a fake) sender.
    """
    raise SystemExit(
        "workflow callbacks --deliver has no default live sender: callback actuation runs "
        "through the #13521 controlled harness (QA-only anchors), not the bare CLI. Refusing "
        "to actuate a real handoff that could re-send a completed request."
    )


def _parse_candidate(spec: str) -> CallbackCandidate:
    """Parse an ``ISSUE:JOURNAL:ROUTE[:KIND]`` candidate spec.

    ``ISSUE`` / ``JOURNAL`` are the durable anchor of the exact source journal; ``ROUTE`` is the
    callback target (e.g. ``coordinator``); optional ``KIND`` is the notification's claimed kind
    (a pointer only — the journal marker is the authority). No prose, no free text.
    """
    raw = (spec or "").strip()
    parts = raw.split(":")
    if len(parts) < 3 or not all(p.strip() for p in parts[:3]):
        raise argparse.ArgumentTypeError(
            "--candidate expects ISSUE:JOURNAL:ROUTE[:KIND] "
            f"(e.g. 13497:74970:coordinator:review_request), got {spec!r}"
        )
    issue, journal, route = parts[0].strip(), parts[1].strip(), parts[2].strip()
    kind = parts[3].strip() if len(parts) >= 4 else ""
    return CallbackCandidate(
        issue=issue, journal=journal, callback_route=route, notification_kind=kind
    )


def _emit(payload: dict, *, as_json: bool, text_lines: list[str]) -> int:
    if as_json:
        print(_json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for line in text_lines:
            print(line)
    return 0


def cmd_workflow_callbacks(args: argparse.Namespace) -> int:
    """Run one callback-outbox action (``--sweep`` / ``--ingest`` / ``--deliver``)."""
    as_json = bool(getattr(args, "json", False))
    outbox = _outbox_from_args(args)

    if getattr(args, "sweep", False):
        processor = CallbackOutboxProcessor(outbox, _NULL_SOURCE)
        report = processor.sweep()
        payload = {"action": "sweep", **report.as_payload()}
        lines = [
            f"action: sweep",
            f"recovered: {len(report.recovered)}",
            f"pending: {len(report.pending)}",
            f"dead_letter: {len(report.dead_letter)}",
        ]
        lines += [f"  pending: #{r.issue} j#{r.journal} {r.normalized_gate}" for r in report.pending]
        lines += [
            f"  dead_letter: #{r.issue} j#{r.journal} {r.detail}" for r in report.dead_letter
        ]
        return _emit(payload, as_json=as_json, text_lines=lines)

    if getattr(args, "ingest", False):
        candidates = list(getattr(args, "candidate", None) or [])
        if not candidates:
            raise SystemExit("--ingest requires at least one --candidate ISSUE:JOURNAL:ROUTE[:KIND]")
        source = _journal_source(args)
        processor = CallbackOutboxProcessor(outbox, source)
        cursor = (getattr(args, "cursor", None) or "").strip() or None
        report = processor.ingest(candidates, cursor=cursor)
        payload = {"action": "ingest", **report.as_payload()}
        lines = [
            "action: ingest",
            f"enqueued: {report.enqueued}",
            f"duplicates: {report.duplicates}",
            f"dead_lettered: {report.dead_lettered}",
        ]
        for o in report.outcomes:
            c = o.classification
            lines.append(
                f"  #{o.candidate.issue} j#{o.candidate.journal} -> {c.disposition}"
                f" {c.normalized_gate or c.reason}"
                + (" [mismatch]" if c.mismatch else "")
                + (" [dup]" if not o.enqueue.inserted else "")
            )
        return _emit(payload, as_json=as_json, text_lines=lines)

    if getattr(args, "deliver", False):
        sender = _callback_sender(args)  # fail-closed by default (raises SystemExit)
        processor = CallbackOutboxProcessor(outbox, _NULL_SOURCE)
        report = processor.deliver(sender, limit=int(getattr(args, "limit", 32) or 32))
        payload = {"action": "deliver", **report.as_payload()}
        lines = [
            "action: deliver",
            f"recovered: {len(report.recovered)}",
            f"delivered: {len(report.delivered)}",
        ]
        lines += [
            f"  #{d.key.issue} j#{d.key.journal} {d.send_outcome} -> {d.resulting_state}"
            for d in report.delivered
        ]
        return _emit(payload, as_json=as_json, text_lines=lines)

    raise SystemExit("workflow callbacks requires an action: --sweep | --ingest | --deliver")


class _NullSource:
    """A source that yields no journal entries — for actions that do not classify (sweep/deliver)."""

    def read_entries(self, issue_id: str):
        return []


_NULL_SOURCE = _NullSource()


def register_callbacks(sub) -> None:
    """Register ``workflow callbacks`` (Redmine #13520 zero-wait callback outbox facade)."""
    p = sub.add_parser(
        "callbacks",
        description=(
            "Zero-wait callback outbox facade (Redmine #13520 / US #13518). `--sweep` runs the "
            "fresh-turn sweep (reconcile crashed rows + surface the pending/dead-letter backlog "
            "once; sends nothing). `--ingest` classifies each --candidate against its exact "
            "source journal (--redmine-json snapshot or --poll live) and idempotently enqueues "
            "it. `--deliver` fires one send per claimed pending row through a configured sender "
            "(fail-closed on the bare CLI; live actuation runs through the #13521 harness). The "
            "journal marker is the gate authority; a notification is only a pointer."
        ),
        help="Zero-wait callback outbox: sweep / ingest / deliver.",
    )
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--sweep", action="store_true", help="Fresh-turn sweep (read-only).")
    action.add_argument("--ingest", action="store_true", help="Classify + enqueue --candidate specs.")
    action.add_argument("--deliver", action="store_true", help="Fire one send per pending row.")
    p.add_argument(
        "--candidate", action="append", type=_parse_candidate, metavar="ISSUE:JOURNAL:ROUTE[:KIND]",
        help="A callback candidate (repeatable). Required for --ingest.",
    )
    p.add_argument("--redmine-json", dest="redmine_json", help="Fetched issue-detail snapshot for classification.")
    p.add_argument("--poll", action="store_true", help="Classify from a live credential-gated Redmine poll.")
    p.add_argument("--source-issue", dest="source_issue", help="Issue id for --poll.")
    p.add_argument("--since", help="Optional updated-since cursor for --poll.")
    p.add_argument("--cursor", help="Efficiency cursor to persist on --ingest.")
    p.add_argument("--limit", type=int, default=32, help="Max rows to claim per --deliver pass.")
    p.add_argument("--store-path", dest="store_path", help="Override the workflow-runtime.sqlite path (test/debug).")
    p.add_argument("--json", action="store_true", help="Emit a structured JSON result.")
    p.set_defaults(func=cmd_workflow_callbacks)


__all__ = (
    "cmd_workflow_callbacks",
    "register_callbacks",
)
