"""CLI surface for `workflow callbacks` — zero-wait callback outbox (Redmine #13520 / #13518).

`mozyo-bridge workflow callbacks` is the mozyo **semantic facade** over the zero-wait callback
delivery bounded context (design answer j#75098). It exposes the three outbox operations —
ingest, deliver, sweep — through one command so an agent / operator never touches a raw Herdr /
tmux primitive (US #13518 acceptance: the tool surface is limited to mozyo semantic
operations). The correctness lives in the store / domain / orchestrator; this is the thin
argparse edge that wires them to the live Redmine journal source and the home-scoped outbox.

Actions (mutually exclusive):

- ``--sweep`` — the **fresh-turn sweep** (read-only actuation-wise): reconcile crashed / stale
  ``inflight`` rows (pre-send -> pending, post-send -> uncertain) and surface the pending +
  dead-letter backlog once, so a single fresh LLM turn reads the source journal. Sends nothing.
- ``--ingest`` — classify each ``--candidate ISSUE:JOURNAL:ROUTE[:KIND]`` against its **exact
  source journal** (from ``--redmine-json`` snapshot or ``--poll --source-issue`` live) and
  idempotently enqueue it (classified -> pending; unclassified -> dead_letter). Sends nothing.
- ``--deliver`` — recover stale rows, claim pending rows (single winner), and fire **one**
  send per row through the real sender (the handoff send port). Delivery safety is the outbox
  UNIQUE fence + one-send-per-claim (a delivered callback is never re-sent), not a refusal to
  send. Actuates.
- ``--run-once`` — one **production pass**: discover fresh handoff-worthy gate candidates from
  ``--source-issue`` (structured markers), ingest/classify, deliver once, sweep. Actuates.
- ``--watch`` — the bounded background-watcher loop: run a production pass per Herdr-event wake
  (``--max-passes`` / ``--wake-target`` stable ``wait agent-status`` event, else ``--wake-interval``),
  re-reading Redmine every wake outcome. Actuates.
- ``--emit-gate`` — the canonical **governed** gate-record writer: record a callback-required gate
  journal on Redmine (``--issue`` + ``--gate`` [+ ``--body``]) with the discoverable
  ``[mozyo:workflow-event:...]`` marker embedded, through the credential-gated, opt-in note
  transport. This is the production **producer** the watcher discovers; a not-recorded gate
  (opt-in ``write_optin_unset`` / transport failure) exits **non-zero** (fail-closed at the process
  gate — a caller can never treat an un-written gate as recorded).

Always exits 0 for a successful read / record / pass (``--emit-gate`` exits non-zero when the gate
was NOT recorded); a source / store error is a
``SystemExit`` with a redacted message (never a credential / URL / pane id).
"""

from __future__ import annotations

import argparse
import json as _json
import os
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
    """Build the real one-send callback sender (#13520 review F1 — the runnable path).

    Wires :class:`...handoff_callback_sender.HandoffCallbackSender` over the real
    :class:`...callback_send_port.HandoffCallbackSendPort` (which fires ``mozyo-bridge handoff
    send`` once and maps the structured outcome). Delivery safety does **not** come from
    refusing to send — it comes from the outbox UNIQUE fence + one-send-per-claim (a delivered
    callback is never re-sent) and from ingesting only the intended (QA-only, in a controlled
    run) candidates. A test / the #13490 live harness patches this seam to inject a fake / a
    cockpit-bound real sender.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (
        HandoffCallbackSendPort,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
        HandoffCallbackSender,
    )

    return HandoffCallbackSender(HandoffCallbackSendPort())


def _herdr_wake_wait(interval_seconds: float) -> object:
    """One bounded background-watcher wake: block for the cadence, then a timeout hint.

    This is the background watcher's blocking wait — the wait/polling doctrine homes the 45–55s
    cadence here (NOT in an LLM turn). It blocks ``interval_seconds`` (a real bounded wait, not a
    busy spin) and returns falsy (a timeout hint); the runtime re-reads Redmine every pass
    regardless (the Herdr event is only a hint). The optional stable Herdr-event wait (wake early
    on an agent status change) is an injectable optimization the #13490 live harness supplies;
    fail-safe by construction (it only sleeps).
    """
    if interval_seconds > 0:
        import time

        time.sleep(interval_seconds)
    return False


def _wake_wait_fn(args: argparse.Namespace) -> Callable[[], object]:
    """Build the ``--watch`` wake primitive (#13520 review F1b).

    Production binds this to the **stable Herdr CLI event** ``wait agent-status`` when a
    ``--wake-target`` is given and the trusted herdr binary resolves: each wake blocks on a real
    herdr runtime state change (bounded by ``--wake-timeout-ms``), the sanctioned event surface of
    design j#75098 Q1 (never the raw socket, never on the LLM surface). When no wake target is
    configured (a one-shot / test pass) or the herdr binary cannot be resolved from the trusted
    environment, it falls back to a bounded interval sleep (``--wake-interval``) — still fail-safe,
    because the loop re-reads the exact Redmine journal on every wake outcome regardless. The
    #13490 live harness supplies the cockpit-bound wake target.
    """
    interval = float(getattr(args, "wake_interval", 0) or 0)
    target = (getattr(args, "wake_target", None) or "").strip()
    if target:
        try:
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
                resolve_herdr_binary,
            )
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
                DEFAULT_WAKE_STATUS,
                DEFAULT_WAKE_TIMEOUT_MS,
                build_herdr_event_wait,
            )

            binary = resolve_herdr_binary(os.environ).path
            status = (getattr(args, "wake_status", None) or DEFAULT_WAKE_STATUS).strip()
            timeout_ms = int(getattr(args, "wake_timeout_ms", 0) or DEFAULT_WAKE_TIMEOUT_MS)
            return build_herdr_event_wait(
                binary, target, status=status, timeout_ms=timeout_ms
            )
        except Exception:  # noqa: BLE001 - binary unresolved / import issue -> fail-safe bounded sleep
            pass
    return lambda: _herdr_wake_wait(interval)


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


def _watch_pass_summary(pass_result: dict) -> str:
    """Render one watch pass safely — a normal pass OR an error pass (#13520 review R2-F2).

    ``watch()`` records a pass that raised as ``{"error": <type>}`` (the background watcher survives
    a transient Redmine/store error and continues to its next wake). This must NOT ``KeyError`` on
    the missing ``deliver`` key: an error pass is surfaced as ``error=<type>`` instead of a count.
    """
    if not isinstance(pass_result, dict):
        return "error=malformed_pass"
    if "error" in pass_result:
        return f"error={pass_result['error']}"
    delivered = (pass_result.get("deliver") or {}).get("delivered") or []
    return f"delivered={len(delivered)}"


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
        sender = _callback_sender(args)  # the real handoff send port (actuates one send per row)
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
            # #13520 review R2-F6: surface the durable-receipt evidence so a write_optin_unset /
            # transport failure is observable (it never changes the outcome above).
            + (f" [persist={'ok' if d.persist_ok else d.persist_reason or 'unknown'}]"
               if d.persist_ok is not None or d.persist_reason else "")
            for d in report.delivered
        ]
        return _emit(payload, as_json=as_json, text_lines=lines)

    if getattr(args, "emit_gate", False):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (
            emit_gate_record,
        )
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
            redmine_delivery_transport_from_env,
        )

        issue = (getattr(args, "issue", None) or "").strip()
        gate = (getattr(args, "gate", None) or "").strip()
        if not issue or not gate:
            raise SystemExit("--emit-gate requires --issue and --gate")
        # Credential-gated, opt-in production writer (MOZYO_REDMINE_DELIVERY_WRITE). None ->
        # write_optin_unset (nothing written, fail-closed — never a silent success).
        transport = redmine_delivery_transport_from_env()
        receipt = emit_gate_record(
            issue, gate, body=(getattr(args, "body", None) or ""), transport=transport
        )
        payload = {"action": "emit-gate", "issue": issue, "gate": gate, **receipt.as_payload()}
        lines = [
            "action: emit-gate",
            f"issue: #{issue}",
            f"gate: {gate}",
            f"recorded: {receipt.recorded}",
            f"reason: {receipt.reason}",
        ]
        if receipt.location:
            lines.append(f"location: {receipt.location}")
        _emit(payload, as_json=as_json, text_lines=lines)
        # #13520 review R2-F1: fail-closed at the PROCESS gate too — a not-recorded gate (opt-in
        # unset / transport failure) must NOT exit 0, so a caller that reads only the return code
        # can never treat an un-written gate as recorded. The structured receipt still prints above.
        return 0 if receipt.recorded else 1

    if getattr(args, "recovery_plan", False):
        from mozyo_bridge.core.state.workflow_runtime_store import (
            CALLBACK_PENDING,
            CALLBACK_UNCERTAIN,
        )
        from mozyo_bridge.core.state.workspace_registry import read_anchor
        from mozyo_bridge.application.commands_common import repo_root_from_args
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_command import (
            build_observation,
            recovery_plan_from_observation,
        )

        repo_root = repo_root_from_args(args)
        anchor = read_anchor(repo_root)
        registry_ws = (anchor.get("workspace_id") if isinstance(anchor, dict) else "") or ""
        # The expected workspace comes from the durable anchor (--workspace-id); default to the
        # registry value when unset (then the mismatch check is trivially satisfied).
        expected_ws = (getattr(args, "workspace_id", None) or registry_ws).strip()
        obs = build_observation(
            workspace_id_expected=expected_ws,
            workspace_id_registry=registry_ws,
            redmine_anchor_readable=True,  # a full poll is heavy; the operator flags an unreadable anchor
            repo_root=str(repo_root),
            outbox_present=True,
            outbox_pending=len(outbox.read(states=[CALLBACK_PENDING])),
            outbox_uncertain=len(outbox.read(states=[CALLBACK_UNCERTAIN])),
            env=os.environ,
        )
        plan = recovery_plan_from_observation(obs)
        payload = {"action": "recovery-plan", **plan.as_payload()}
        lines = [f"action: recovery-plan", f"status: {plan.status}"]
        lines += [f"  blocker: {b}" for b in plan.blockers]
        lines += [f"  step: {s.kind} — {s.detail}" for s in plan.steps]
        lines += [f"  note: {n}" for n in plan.notes]
        return _emit(payload, as_json=as_json, text_lines=lines)

    if getattr(args, "run_once", False) or getattr(args, "watch", False):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
            discover_candidates,
            run_once,
            watch,
        )

        explicit = list(getattr(args, "candidate", None) or [])
        source_issue = (getattr(args, "source_issue", None) or "").strip()
        sender = _callback_sender(args)
        cursor = (getattr(args, "cursor", None) or "").strip() or None
        # Production discovery (F1-R1): with --source-issue + a journal source, each pass
        # RE-READS Redmine and discovers fresh handoff-worthy gate candidates from the issue's
        # structured markers (deduped by the outbox fence). Explicit --candidate specs are also
        # honored. A pass with neither discovers nothing and only drains the existing outbox.
        needs_source = bool(explicit) or bool(source_issue)
        source = _journal_source(args) if needs_source else _NULL_SOURCE

        def _pass() -> dict:
            processor = CallbackOutboxProcessor(outbox, source)
            candidates = list(explicit)
            if source_issue:
                candidates.extend(discover_candidates(source, source_issue))
            return run_once(processor, sender, candidates=candidates, cursor=cursor)

        if getattr(args, "watch", False):
            max_passes = int(getattr(args, "max_passes", 1) or 1)
            passes = watch(_wake_wait_fn(args), _pass, max_passes=max_passes)
            payload = {"action": "watch", "passes": passes}
            lines = [f"action: watch", f"passes: {len(passes)}"] + [
                f"  wake={p['wake']} {_watch_pass_summary(p['pass'])}" for p in passes
            ]
            return _emit(payload, as_json=as_json, text_lines=lines)

        report = _pass()
        payload = {"action": "run-once", **report}
        lines = [
            "action: run-once",
            f"delivered: {len(report['deliver']['delivered'])}",
            f"recovered: {len(report['deliver']['recovered'])}",
            f"pending: {len(report['sweep']['pending'])}",
            f"dead_letter: {len(report['sweep']['dead_letter'])}",
        ]
        return _emit(payload, as_json=as_json, text_lines=lines)

    raise SystemExit(
        "workflow callbacks requires an action: --sweep | --ingest | --deliver | "
        "--run-once | --watch"
    )


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
            "Zero-wait callback outbox facade (Redmine #13520 / US #13518). `--sweep` reconciles "
            "stale rows + surfaces the pending/dead-letter backlog once (sends nothing). "
            "`--ingest` classifies each --candidate against its exact source journal and enqueues "
            "it (sends nothing). `--deliver` fires one send per claimed pending row through the "
            "real handoff send port (safety = the outbox UNIQUE fence + one-send-per-claim). "
            "`--run-once` is one production pass (discover gate candidates from --source-issue, "
            "ingest, deliver, sweep); `--watch` is the bounded background-watcher loop. The "
            "journal marker is the gate authority; a notification is only a pointer."
        ),
        help="Zero-wait callback outbox: sweep / ingest / deliver / run-once / watch.",
    )
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--sweep", action="store_true", help="Fresh-turn sweep (read-only).")
    action.add_argument("--ingest", action="store_true", help="Classify + enqueue --candidate specs.")
    action.add_argument("--deliver", action="store_true", help="Fire one real send per pending row (actuates).")
    action.add_argument(
        "--run-once", dest="run_once", action="store_true",
        help="One production pass: discover (--source-issue) + ingest, deliver, sweep (actuates).",
    )
    action.add_argument(
        "--watch", action="store_true",
        help="Bounded background-watcher loop; one production pass per wake (--max-passes).",
    )
    action.add_argument(
        "--emit-gate", dest="emit_gate", action="store_true",
        help="The canonical governed gate-record writer: record a callback-required gate journal on "
             "Redmine WITH the discoverable marker (--issue + --gate; credential-gated, opt-in). "
             "Fail-closed: a not-recorded gate (opt-in unset / transport failure) exits NON-ZERO so "
             "a caller cannot treat an un-written gate as recorded.",
    )
    action.add_argument(
        "--recovery-plan", dest="recovery_plan", action="store_true",
        help="Emit the READ-ONLY host-restart recovery plan (reconciles Redmine/Git/registry/"
             "state-DB/runtime authorities; fail-closed + never-clobber; --workspace-id sets the "
             "expected anchor workspace).",
    )
    p.add_argument("--workspace-id", dest="workspace_id", help="Expected anchor workspace id for --recovery-plan.")
    p.add_argument("--issue", help="Issue id for --emit-gate.")
    p.add_argument("--gate", help="Callback-required gate kind for --emit-gate (implementation_done | review_request | review_result | owner_close_approval_waiting | blocked).")
    p.add_argument("--body", help="Optional human-readable prose body for --emit-gate (the marker is appended).")
    p.add_argument("--max-passes", dest="max_passes", type=int, default=1, help="Iterations for --watch.")
    p.add_argument(
        "--wake-interval", dest="wake_interval", type=float, default=0.0,
        help="Background-watcher wake cadence seconds for --watch (0 = one-shot; operator sets 45-55).",
    )
    p.add_argument(
        "--wake-target", dest="wake_target",
        help="Herdr agent/target to wait on via the stable `wait agent-status` event for --watch "
             "(when set + herdr resolves, the real Herdr CLI event drives wakes; else --wake-interval).",
    )
    p.add_argument(
        "--wake-status", dest="wake_status", default="working",
        help="Herdr runtime status the --wake-target waits for a change into (default: working).",
    )
    p.add_argument(
        "--wake-timeout-ms", dest="wake_timeout_ms", type=int, default=0,
        help="Bounded `wait agent-status --timeout` window in ms for --watch (0 = default 50000).",
    )
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
