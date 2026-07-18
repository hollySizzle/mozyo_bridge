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


def _outbox_store_path(args: argparse.Namespace) -> Path:
    """Resolve the callback outbox store path (``--store-path`` or the home default)."""
    raw = (getattr(args, "store_path", None) or "").strip()
    return Path(raw) if raw else workflow_runtime_store_path()


def _outbox_from_args(args: argparse.Namespace) -> CallbackOutbox:
    """Build the callback outbox over ``--store-path`` (test/debug) or the home default."""
    return CallbackOutbox(path=_outbox_store_path(args))


def _resolve_workspace_id(args: argparse.Namespace) -> str:
    """Resolve the attested workspace this callback surface owns (#13520 review R2-F5).

    The workspace registry anchor is authoritative (the current repo's workspace identity); the
    ``MOZYO_WORKSPACE_ID`` env is a fallback. ``""`` (no anchor / not resolvable) is the legacy
    un-partitioned bucket, so a bare invocation stays back-compatible. This scopes every processor's
    claim and the sender's route to one workspace, so a shared home DB never lets this surface claim
    or send another workspace's callback rows.
    """
    ws = ""
    try:
        from mozyo_bridge.core.state.workspace_registry import read_anchor
        from mozyo_bridge.application.commands_common import repo_root_from_args

        anchor = read_anchor(repo_root_from_args(args))
        ws = (anchor.get("workspace_id") if isinstance(anchor, dict) else "") or ""
    except Exception:  # noqa: BLE001 - anchor unresolvable -> fall back to env, then "" (back-compat)
        ws = ""
    return str(ws or os.environ.get("MOZYO_WORKSPACE_ID") or "").strip()


def _require_partition_workspace_id(args: argparse.Namespace) -> str:
    """Resolve the attested workspace for a MUTATING callback action, fail-closed on blank.

    #13518 review R3-F3: a mutating action (``--deliver`` / ``--run-once`` / ``--watch`` /
    ``--sweep``) claims, reconciles, and routes real callback rows over the shared home DB. An
    unresolved (blank) workspace id would claim / reconcile across ALL workspaces
    (``claim_pending(None)`` / ``recover_inflight(None)``) and let the sender route a foreign row on
    ambient cwd/env — the exact cross-workspace duplicate/misroute R2-F5 was meant to fence. So a
    mutating action REQUIRES a non-empty, authority-verified workspace id (the workspace registry
    anchor, else ``MOZYO_WORKSPACE_ID``) and claims exactly that partition.

    The blank / legacy all-workspace bucket is available ONLY behind the explicit
    ``--allow-unpartitioned-callbacks`` debug/migration surface — never as the default production
    behaviour.
    """
    ws = _resolve_workspace_id(args)
    if ws:
        return ws
    if getattr(args, "allow_unpartitioned_callbacks", False):
        return ""  # explicit debug/migration: legacy un-partitioned all-workspace claim/reclaim
    raise SystemExit(
        "workflow callbacks refuses a mutating action (--deliver / --run-once / --watch / "
        "--sweep) without a resolved workspace identity: over a shared home DB it would claim, "
        "reconcile, and route callback rows across ALL workspaces. Anchor this repo's workspace "
        "(workspace_registry) or set MOZYO_WORKSPACE_ID, then re-run. For an explicit legacy / "
        "migration sweep over the un-partitioned bucket, pass --allow-unpartitioned-callbacks."
    )


def _activate_supervisor_process() -> None:
    """Activate a bounded supervisor pass after a gate commit (#13683 review R3-F1, design j#77216 b6).

    The design answer makes the source activation mechanism a Phase A requirement (only the
    installed-artifact service residency / live dogfood defers to Phase B): a canonical gate commit
    must START / wake / activate a supervisor pass, not merely enqueue a wake into a passive queue.
    This spawns a **detached, bounded** ``workflow supervisor --run-once --local-wake`` so the wake
    just enqueued is consumed by the lease owner without waiting for the reconciliation interval.
    Best-effort: a spawn failure is swallowed (the wake is still recovered by bounded reconciliation).
    Patched in tests to activate an in-process supervisor (the E2E drives gate -> activation ->
    consume -> delivery without calling ``run_once`` itself).
    """
    import subprocess
    import sys

    try:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell; detached bounded supervisor run
            [sys.executable, "-m", "mozyo_bridge", "workflow", "supervisor", "--run-once", "--local-wake"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001 - a spawn failure never fails an already-recorded gate
        pass


def _best_effort_emit_supervisor_wake(args: argparse.Namespace, issue: str) -> None:
    """Enqueue a supervisor local wake for (workspace, issue) after a recorded gate, then activate.

    #13683 R1-F2 + R3-F1: the workspace is the attested id this surface owns
    (:func:`_resolve_workspace_id`); a blank id (un-partitioned legacy bucket) or a blank issue is a
    no-op. The wake is enqueued to the durable coalesced queue AND a bounded supervisor pass is
    **activated** (:func:`_activate_supervisor_process`) so the gate commit actually starts the
    consume -> delivery, not just leaves a passive queue entry. Wholly best-effort: any failure is
    swallowed — the gate is already recorded on Redmine (the durable authority), and the supervisor's
    bounded reconciliation recovers a lost wake.
    """
    ws = _resolve_workspace_id(args)
    iss = str(issue or "").strip()
    if not ws or not iss:
        return
    try:
        from mozyo_bridge.core.state.supervisor_wake import SupervisorWakeStore

        SupervisorWakeStore().enqueue(ws, iss)
    except Exception:  # noqa: BLE001 - a wake emit never fails an already-recorded gate
        pass
    _activate_supervisor_process()


def _watch_sender_attested(args: argparse.Namespace) -> bool:
    """Whether the launch-time coordinator sender identity is attested for a managed watcher.

    #13518 review R3-F1: the managed watcher RECORDS this in its resolved config so an un-attested
    watcher is visible — it may still observe / plan, but its downstream sends fail-closed on the
    workspace pin (they never route on ambient env). Attested = a workspace id resolves from an
    authority (the registry anchor, else ``MOZYO_WORKSPACE_ID``) AND the coordinator role env
    (``MOZYO_AGENT_ROLE``) is present. This is a recorded observation, not the send-time authority
    (the send port still enforces the exact workspace pin — R3-F3).
    """
    return bool(_resolve_workspace_id(args)) and bool((os.environ.get("MOZYO_AGENT_ROLE") or "").strip())


#: Explicit review_result decision kinds that are NOT an approval, so they carry no
#: generation-admission obligation and stay unfenced (back-compat). Anything else — including an
#: unspecified decision — is treated as an approval and is fenced FAIL-CLOSED (#13518 review R4-F2).
_NON_APPROVAL_REVIEW_DECISIONS = frozenset({"changes_requested", "finding", "progress"})

#: A review_result APPROVAL was written without the durable generation observation + consumer id
#: the admission fence REQUIRES (#13518 review R4-F2). Fail-closed: an approval can never be recorded
#: outside the generation lease + pre-approval reread fence, even when the caller omits the flags.
REASON_APPROVAL_FENCE_INPUTS_MISSING = "approval_requires_generation_observation_and_consumer"


def _review_approval_refusal(
    args: argparse.Namespace, issue: str, gate: str, marker_fields: "Optional[dict]" = None
):
    """Return a fail-closed refusal reason for a review_result APPROVAL write, or ``None`` to allow.

    #13518 review R3-F2 / R4-F2: a ``review_result`` APPROVAL is mechanically distinguished from a
    non-approval decision (changes_requested / finding / progress) by ``--review-decision``. An
    approval — whether ``--review-decision approval`` OR an UNSPECIFIED review_result decision
    (fail-closed default) — MUST pass the admission fence: a durable single-consumer generation lease
    + the pre-approval reread fence (:func:`...review_admission.admit_review_approval`). The fence is
    NOT optional: an approval with no durable review observation (``--review-generation-json``) or no
    ``--consumer-id`` is refused (:data:`REASON_APPROVAL_FENCE_INPUTS_MISSING`) rather than silently
    admitted, so a stale / duplicate approval writer can never bypass the fence by omitting the flags.

    Back-compat (``None``, unfenced) is limited to a gate that is not ``review_result`` OR an
    EXPLICIT non-approval review_result decision — never an approval.
    """
    if gate != "review_result":
        return None
    decision = (getattr(args, "review_decision", None) or "").strip().lower()
    if decision in _NON_APPROVAL_REVIEW_DECISIONS:
        return None  # an explicit non-approval decision carries no generation-admission obligation
    # An approval (explicit `approval`, or an unspecified review_result decision) MUST be fenced.
    path = (getattr(args, "review_generation_json", None) or "").strip()
    consumer = (getattr(args, "consumer_id", None) or "").strip()
    if not path or not consumer:
        return REASON_APPROVAL_FENCE_INPUTS_MISSING
    try:
        import json

        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.review_admission import (  # noqa: E501
            GenerationLeaseStore,
            admit_review_approval,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
            ReviewDecision,
            ReviewGeneration,
        )

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        gen = ReviewGeneration(
            issue=str(raw.get("issue", issue)),
            review_request_journal=str(raw.get("review_request_journal", "")),
            target_head=str(raw.get("target_head", "")),
        )
        # j#81506 F2: the admission observation's generation identity MUST exact-match the marker write
        # target — an approval lease for ONE generation must never write another's approved marker.
        if marker_fields:
            if str(gen.issue).strip() != str(issue).strip():
                return "approval_observation_issue_mismatch"
            if gen.review_request_journal.strip() != str(
                marker_fields.get("review_request_journal", "") or ""
            ).strip():
                return "approval_observation_request_mismatch"
            if gen.target_head.strip() != str(marker_fields.get("target_head", "") or "").strip():
                return "approval_observation_head_mismatch"
        decisions = [
            ReviewDecision(
                generation=gen,
                kind=str(d.get("kind", "")),
                seq=int(d.get("seq", 0)),
                blocking=bool(d.get("blocking", False)),
                disposition=str(d.get("disposition", "unresolved")),
                journal_id=str(d.get("journal_id", "")),
            )
            for d in (raw.get("decisions") or [])
        ]
        source_request_seq = int(raw.get("source_request_seq", 0))
        lease = GenerationLeaseStore(store=WorkflowRuntimeStore(path=_outbox_store_path(args)))
        result = admit_review_approval(
            lease=lease, generation=gen, consumer_id=consumer,
            source_request_seq=source_request_seq, decisions=decisions,
        )
        return None if result.admissible else result.reason
    except Exception:  # noqa: BLE001 - an unreadable / malformed durable observation fails closed
        return "review_generation_observation_unreadable"


#: The review gates that MUST carry Review Generation Marker Contract v2 marker fields (#13974 j#81487
#: F2): the canonical producer refuses to write one head-less / req-less rather than emitting a marker
#: the callback generation fence would fail closed.
_REVIEW_REQUEST_GATE = "review_request"
_REVIEW_RESULT_GATE = "review_result"


def _review_gate_marker_fields(args: argparse.Namespace, gate: str) -> "tuple[dict, Optional[str]]":
    """Build + fail-closed-validate the v2 marker fields for a review gate (#13974 j#81487 F2).

    Returns ``(marker_fields, refusal)``. For a ``review_request`` gate ``--target-head`` is required
    and must be a full commit head; for a ``review_result`` gate ``--target-head`` (full head) AND
    ``--review-request-journal`` are required. A missing / malformed input yields a fixed refusal token
    (nothing is written — the producer never emits a marker the fence would reject). A non-review gate
    carries no v2 fields (``({}, None)``).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (  # noqa: E501
        is_full_commit_head,
    )

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
        REVIEW_APPROVED,
        REVIEW_CHANGES_REQUESTED,
    )

    if gate not in (_REVIEW_REQUEST_GATE, _REVIEW_RESULT_GATE):
        return {}, None
    head = (getattr(args, "target_head", None) or "").strip()
    if not head:
        return {}, "review_marker_missing_target_head"
    if not is_full_commit_head(head):
        return {}, "review_marker_malformed_target_head"
    fields: dict = {"target_head": head}
    if gate == _REVIEW_RESULT_GATE:
        req = (getattr(args, "review_request_journal", None) or "").strip()
        if not req:
            return {}, "review_marker_missing_review_request_journal"
        fields["review_request_journal"] = req
        # v2 (`### Gate Schema`): a review_result marker carries its conclusion. The `--review-decision`
        # maps to the marker vocabulary — an approval / unspecified decision is ``approved``, any
        # explicit non-approval outcome (changes_requested / finding / progress) is ``changes_requested``.
        decision = (getattr(args, "review_decision", None) or "").strip().lower()
        fields["conclusion"] = (
            REVIEW_APPROVED if decision in ("", "approval") else REVIEW_CHANGES_REQUESTED
        )
    return fields, None


def _live_journal_source(args: argparse.Namespace) -> LiveRedmineJournalSource:
    """Build the live poll source from daemon-trusted credentials (patchable test seam)."""
    since = (getattr(args, "since", None) or "").strip() or None
    return LiveRedmineJournalSource.from_environment(since=since)


def _optional_journal_source(args: argparse.Namespace) -> Optional[RedmineJournalSource]:
    """A Redmine source iff ``--poll`` / ``--redmine-json`` was given, else ``None`` (no raise; #13974 R2)."""
    if (getattr(args, "redmine_json", None) or "").strip() or getattr(args, "poll", False):
        return _journal_source(args)
    return None


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

    return HandoffCallbackSender(
        HandoffCallbackSendPort(attested_workspace_id=_resolve_workspace_id(args))
    )


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
        processor = CallbackOutboxProcessor(
            outbox, _NULL_SOURCE, workspace_id=_require_partition_workspace_id(args)
        )
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
        processor = CallbackOutboxProcessor(outbox, source, workspace_id=_resolve_workspace_id(args))
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
        ws = _require_partition_workspace_id(args)  # R3-F3: fail-closed before any claim / send
        sender = _callback_sender(args)  # the real handoff send port (actuates one send per row)
        source = _optional_journal_source(args)
        if source is not None:
            # Redmine #13974 R2: a readable provider lets `--deliver` CONVERGE a previous-generation /
            # hibernated-owner review_return backlog row to a terminal zero-send (the supervisor's fence),
            # not merely attempt-and-retry it. Source-less `--deliver` stays the raw drain below.
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import deliver_workspace_backlog  # noqa: E501

            outcome = deliver_workspace_backlog(outbox, ws, source=source, sender=sender)
            payload = {"action": "deliver", "fenced_source": True, **outcome.as_payload()}
            lines = [
                "action: deliver",
                f"fenced: {outcome.fenced}",
                f"delivered: {outcome.delivered}",
                f"transient_skipped: {outcome.transient_skipped}",
            ]
            return _emit(payload, as_json=as_json, text_lines=lines)
        processor = CallbackOutboxProcessor(outbox, _NULL_SOURCE, workspace_id=ws)
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
        # #13518 review R3-F2: a review_result APPROVAL write is fenced BEFORE it is recorded when a
        # durable review observation (--review-generation-json) + a --consumer-id are supplied. The
        # durable single-consumer generation lease + the pre-approval reread fence refuse a duplicate
        # consumer or a stale approval (a snapshot predating a newer unresolved blocking finding —
        # the #13586 case). Refusal fails closed: nothing is written, exit non-zero.
        # #13974 j#81487 F2: a review_request / review_result gate MUST carry the v2 marker fields
        # (exact full target_head; review_result also its answered review_request journal + conclusion).
        # The canonical producer refuses to write a head-less / req-less / malformed review marker
        # rather than emit one the callback generation fence would fail closed. j#81506 F2: an approval
        # write additionally exact-matches its generation-admission observation to these marker fields —
        # a lease for another generation must never write this one's approved marker.
        marker_fields, marker_refusal = _review_gate_marker_fields(args, gate)
        refusal = marker_refusal or _review_approval_refusal(args, issue, gate, marker_fields)
        if refusal is not None:
            payload = {"action": "emit-gate", "issue": issue, "gate": gate,
                       "recorded": False, "reason": refusal}
            _emit(payload, as_json=as_json, text_lines=[
                "action: emit-gate", f"issue: #{issue}", f"gate: {gate}",
                "recorded: False", f"reason: {refusal}",
            ])
            return 1
        # Credential-gated, opt-in production writer (MOZYO_REDMINE_DELIVERY_WRITE). None ->
        # write_optin_unset (nothing written, fail-closed — never a silent success).
        transport = redmine_delivery_transport_from_env()
        receipt = emit_gate_record(
            issue, gate, body=(getattr(args, "body", None) or ""), transport=transport,
            marker_fields=marker_fields,
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
        # #13683 review R1-F2: the canonical gate writer is the PRIMARY supervisor trigger — after a
        # gate is RECORDED, emit a best-effort local wake for (workspace, issue) so the workspace
        # callback supervisor re-reads that issue without waiting for the reconciliation interval.
        # Best-effort: a wake-store failure never fails the (already-recorded) gate; a lost wake is
        # recovered by the supervisor's bounded reconciliation.
        if receipt.recorded:
            _best_effort_emit_supervisor_wake(args, issue)
        # #13520 review R2-F1: fail-closed at the PROCESS gate too — a not-recorded gate (opt-in
        # unset / transport failure) must NOT exit 0, so a caller that reads only the return code
        # can never treat an un-written gate as recorded. The structured receipt still prints above.
        return 0 if receipt.recorded else 1

    if getattr(args, "emit_progress", False):
        # #13889 review F2: the producer half of the sweep watermark. A worker-side progress gate
        # (review_finding_verdict / progress_log / start / design_consultation) recorded through
        # this path is marker-bearing, so the sweep can classify it structurally instead of
        # abstaining from every stall verdict on the issue. Same opt-in / fail-closed contract as
        # --emit-gate; the marker is round-scoped so it cannot be read as another round's progress.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (
            emit_progress_record,
        )
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
            redmine_delivery_transport_from_env,
        )

        issue = (getattr(args, "issue", None) or "").strip()
        kind = (getattr(args, "progress_kind", None) or "").strip()
        lane = (getattr(args, "lane", None) or "").strip()
        generation = (getattr(args, "lane_generation", None) or "").strip()
        if not (issue and kind and lane and generation):
            raise SystemExit(
                "--emit-progress requires --issue, --progress-kind, --lane and --lane-generation "
                "(an unscoped progress marker cannot be attributed to a dispatch round)"
            )
        transport = redmine_delivery_transport_from_env()
        try:
            receipt = emit_progress_record(
                issue, kind, lane=lane, lane_generation=generation,
                body=(getattr(args, "body", None) or ""), transport=transport,
            )
        except ValueError as exc:  # an out-of-vocabulary kind is a caller error, surfaced
            raise SystemExit(str(exc)) from exc
        payload = {"action": "emit-progress", "issue": issue, "kind": kind, "lane": lane,
                   "lane_generation": generation, **receipt.as_payload()}
        lines = [
            "action: emit-progress",
            f"issue: #{issue}",
            f"kind: {kind}",
            f"lane: {lane} generation: {generation}",
            f"recorded: {receipt.recorded}",
            f"reason: {receipt.reason}",
        ]
        if receipt.location:
            lines.append(f"location: {receipt.location}")
        _emit(payload, as_json=as_json, text_lines=lines)
        # A progress gate owes no coordinator callback (that is the whole point of the separate
        # vocabulary), so unlike --emit-gate this deliberately emits NO supervisor wake.
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
        # #13520 review R2-F3: MEASURE authorities at action-time; never replace an unknown with a
        # safe default. The expected workspace comes from the durable anchor (--workspace-id) — when
        # unset it is UNVERIFIED (left blank so the reconciler fail-closes on the mismatch, never a
        # silent self-match to the registry). Redmine anchor readability is likewise unverified
        # unless the operator asserts they read the exact gate journal (--anchor-readable), so it
        # defaults to fail-closed rather than a hard-coded True. Outbox presence is measured from
        # the store; the live Herdr slot inventory is the #13490 live surface (best-effort empty).
        expected_ws = (getattr(args, "workspace_id", None) or "").strip()
        anchor_readable = bool(getattr(args, "anchor_readable", False))
        store_p = _outbox_store_path(args)
        outbox_present = store_p.exists()
        obs = build_observation(
            workspace_id_expected=expected_ws,
            workspace_id_registry=registry_ws,
            redmine_anchor_readable=anchor_readable,
            repo_root=str(repo_root),
            outbox_present=outbox_present,
            outbox_pending=len(outbox.read(states=[CALLBACK_PENDING])) if outbox_present else 0,
            outbox_uncertain=len(outbox.read(states=[CALLBACK_UNCERTAIN])) if outbox_present else 0,
            # #13518 review R3-F4b: derive the outbox ownership from the ACTUAL row workspace ids,
            # never substitute the registry id. A foreign / mixed / unknown-ownership DB is then
            # observed truthfully and fail-closes (BLOCK_DB_CONTRADICTION) instead of being reported
            # as registry-owned by construction.
            outbox_workspace_ids=outbox.workspace_ids() if outbox_present else (),
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

        # R3-F3: a production pass claims + routes real rows — fail-closed before any pass on a
        # blank/unverified workspace (claim exactly this partition; sender pins to it).
        ws = _require_partition_workspace_id(args)
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
            processor = CallbackOutboxProcessor(outbox, source, workspace_id=ws)
            candidates = list(explicit)
            if source_issue:
                candidates.extend(
                    discover_candidates(source, source_issue, workspace_id=ws)
                )
            return run_once(processor, sender, candidates=candidates, cursor=cursor)

        if getattr(args, "watch", False):
            max_passes = int(getattr(args, "max_passes", 1) or 1)
            wake_target = (getattr(args, "wake_target", None) or "").strip()
            managed = bool(source_issue and wake_target)
            if managed:
                # #13518 review R3-F1: the PRODUCTION managed-watcher composition. A --watch with a
                # source issue to re-read AND a stable Herdr wake target is a managed watcher — it
                # composes through the fail-closed `resolve_watcher_config` (source issue / attested
                # workspace it owns / stable wake target) and `run_managed_watch`, which is the
                # restart owner within its bounded budget (every wake outcome re-reads Redmine; a
                # raising pass is recorded, not fatal). This is the sanctioned entrypoint that
                # consumes the composition root — no longer only its unit test.
                from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_watcher_runtime import (  # noqa: E501
                    resolve_watcher_config,
                    run_managed_watch,
                )
                from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (  # noqa: E501
                    DEFAULT_WAKE_STATUS,
                    DEFAULT_WAKE_TIMEOUT_MS,
                )

                config = resolve_watcher_config(
                    source_issue=source_issue,
                    workspace_id=ws,
                    wake_target=wake_target,
                    sender_attested=_watch_sender_attested(args),
                    max_passes=max_passes,
                    wake_status=(getattr(args, "wake_status", None) or DEFAULT_WAKE_STATUS),
                    wake_timeout_ms=int(getattr(args, "wake_timeout_ms", 0) or DEFAULT_WAKE_TIMEOUT_MS),
                )
                passes = run_managed_watch(config, run_pass=_pass, wait_fn=_wake_wait_fn(args))
                payload = {"action": "watch", "managed": True, "config": config.as_payload(), "passes": passes}
                lines = (
                    [f"action: watch (managed)", f"passes: {len(passes)}",
                     f"  sender_attested: {config.sender_attested}"]
                    + [f"  wake={p['wake']} {_watch_pass_summary(p['pass'])}" for p in passes]
                )
            else:
                # Ad-hoc bounded watch (no managed composition): interval / best-effort wake, drains
                # the existing outbox. Kept for back-compat; not the production restart owner.
                passes = watch(_wake_wait_fn(args), _pass, max_passes=max_passes)
                payload = {"action": "watch", "managed": False, "passes": passes}
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
    """Register the callback family: ``workflow callbacks`` + ``workflow callback-admit``.

    ``callback-admit`` (#13910) is registered here, next to its sibling, rather than from
    :mod:`.cli_workflow`: that aggregator sits exactly at the module-health threshold, so the four
    lines an import + call would cost there are a real design constraint, not an inconvenience to
    allowlist around. This is the callback family's own registrar and a receiver admission rail is
    a callback subcommand, so the registration is cohesive here.
    """
    from .cli_workflow_recovery_admission import (
        register_callback_admit,
        register_callback_receipt,
    )

    register_callback_admit(sub)
    register_callback_receipt(sub)
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
        "--emit-progress", dest="emit_progress", action="store_true",
        help="The canonical governed PROGRESS-record writer (Redmine #13889): record a worker-side "
             "progress gate that proves the lane is alive but owes NO coordinator callback "
             "(--issue + --progress-kind + --lane + --lane-generation; credential-gated, opt-in). "
             "Recorded this way the gate is marker-bearing, so the callback sweep can classify it "
             "structurally instead of abstaining; recorded as prose it is invisible to the sweep. "
             "Fail-closed: a not-recorded gate exits NON-ZERO.",
    )
    action.add_argument(
        "--recovery-plan", dest="recovery_plan", action="store_true",
        help="Emit the READ-ONLY host-restart recovery plan (reconciles Redmine/Git/registry/"
             "state-DB/runtime authorities; fail-closed + never-clobber). Measures at action-time: "
             "unverified authorities fail-closed, never assumed safe. --workspace-id (expected "
             "anchor workspace) and --anchor-readable (you verified the exact gate journal reads) "
             "assert what you measured; unset = unverified = fail-closed.",
    )
    p.add_argument("--workspace-id", dest="workspace_id", help="Expected anchor workspace id for --recovery-plan (unset = unverified).")
    p.add_argument(
        "--anchor-readable", dest="anchor_readable", action="store_true",
        help="Assert the exact Redmine gate journal was verified readable (--recovery-plan; unset = unverified = fail-closed).",
    )
    p.add_argument("--issue", help="Issue id for --emit-gate / --emit-progress.")
    p.add_argument("--gate", help="Callback-required gate kind for --emit-gate (implementation_done | review_request | review_result | owner_close_approval_waiting | blocked).")
    p.add_argument(
        "--progress-kind", dest="progress_kind",
        help="Worker-side progress gate kind for --emit-progress (start | progress_log | "
             "review_finding_verdict | design_consultation). Deliberately DISJOINT from --gate: "
             "these prove the lane advanced but wake no coordinator.",
    )
    p.add_argument("--lane", help="Lane id that scopes the --emit-progress marker to its dispatch round.")
    p.add_argument(
        "--lane-generation", dest="lane_generation",
        help="Lane generation that scopes the --emit-progress marker to its dispatch round "
             "(required: an unscoped progress marker cannot be attributed to a round).",
    )
    p.add_argument(
        "--review-generation-json", dest="review_generation_json",
        help="#13518 R3-F2: durable review observation {issue, review_request_journal, target_head, "
             "source_request_seq, decisions:[...]} used to FENCE a --emit-gate --gate review_result "
             "approval (durable generation lease + pre-approval reread fence; refusal fails closed).",
    )
    p.add_argument(
        "--consumer-id", dest="consumer_id",
        help="#13518 R3-F2: the approving consumer id for the review_result generation lease "
             "(a duplicate consumer of the same generation is refused).",
    )
    p.add_argument(
        "--review-decision", dest="review_decision",
        choices=["approval", "changes_requested", "finding", "progress"],
        help="#13518 R4-F2: the review_result decision kind. An `approval` (or an UNSPECIFIED "
             "review_result decision — fail-closed default) MUST pass the generation-admission fence "
             "(--review-generation-json + --consumer-id required, else refused). An explicit "
             "non-approval decision (changes_requested / finding / progress) is unfenced.",
    )
    p.add_argument("--body", help="Optional human-readable prose body for --emit-gate (the marker is appended).")
    p.add_argument(
        "--target-head", dest="target_head",
        help="Review Generation Marker Contract v2 (#13974): the exact full commit head this review "
             "gate reviews/requests. Required + full-hex for --emit-gate review_request / review_result.",
    )
    p.add_argument(
        "--review-request-journal", dest="review_request_journal",
        help="Review Generation Marker Contract v2 (#13974): the review_request journal a review_result "
             "answers. Required for --emit-gate review_result.",
    )
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
    p.add_argument(
        "--allow-unpartitioned-callbacks", dest="allow_unpartitioned_callbacks", action="store_true",
        help="DEBUG/MIGRATION ONLY: allow a mutating action (--deliver / --run-once / --watch / "
             "--sweep) to run against the legacy un-partitioned (all-workspace) bucket when no "
             "workspace identity resolves. Default production fails closed (#13518 review R3-F3) — "
             "never route/reclaim across workspaces on a shared home DB without an attested id.",
    )
    p.add_argument("--json", action="store_true", help="Emit a structured JSON result.")
    p.set_defaults(func=cmd_workflow_callbacks)


__all__ = (
    "cmd_workflow_callbacks",
    "register_callbacks",
)
