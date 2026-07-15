"""Action-time live resume leg (Redmine #13813).

The live wiring for the startup-clear exactly-once resume (review j#79268 Finding 1).
:mod:`.operator_startup_resume` is the pure orchestration boundary (all inputs injected);
this module is its **action-time application leg** — the analogue of #13489's
:mod:`.herdr_dispatch_cli` (:func:`execute_herdr_dispatch`). It binds the four ports the
durable Implementation Request (j#79214 items 1/3/6) requires and drives the orchestrator:

1. **ticket-provider port re-read** — read the *latest durable operator startup gate* and
   the original request anchor from source-of-truth Redmine (never a saved projection /
   pane title / cache): :data:`GateSource`.
2. **action-time identity / generation resolution** — re-resolve the exact live target
   (workspace / repo / lane / role / provider / assigned name / agent generation) and bind
   the #13760 visible-pane read at action time: :data:`TargetResolver`.
3. **existing high-level send** — build the single send that re-issues the original anchor
   over the existing high-level handoff rail: :data:`ResumeSendFactory`.
4. **append-only gate transition record** — durably record the advanced gate
   (``verified_clear`` / ``consumed``) back to the ticket provider: :data:`GateRecorder`.

Every port is injectable (typed callable) with a thin, lazy-imported live default, exactly
as #13489's leg injects its ``send_factory`` / ``fence``: the orchestration + serialization
are proven hermetically with injected fakes, and the live defaults are the production
bindings. The exactly-once safety stays in :func:`resume_startup_gate` — this leg only
re-resolves the action-time facts, drives it once, and records the durable transition.

Durable gate serialization (ticket-provider port): a gate is persisted to a Redmine
journal as its :meth:`OperatorStartupGate.to_record` JSON on the line after the
:data:`GATE_JOURNAL_MARKER` sentinel. ``to_record`` is pasteable-safe by construction (no
path / pane body / credential), so the JSON is safe to journal. A flat ``key=value:...``
marker cannot hold the gate (``repo_identity_digest`` itself contains a ``:``), so the
single-line JSON payload is the wire form; :func:`parse_latest_gate` reads it back with
:meth:`OperatorStartupGate.from_record`.
"""

from __future__ import annotations

import json
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    DispatchOutboxFenceError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
    SendOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (
    RESOLUTION_UNRESOLVED,
    ObservedStartupTarget,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume import (
    RESUME_FENCE_UNAVAILABLE,
    RESUME_NOT_RESUMABLE,
    RESUME_RECORDER_UNAVAILABLE,
    StartupResumeResult,
    resume_startup_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    STATE_REQUIRED,
    OperatorStartupGate,
    OperatorStartupGateError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (
    operator_startup_gate_record_lines,
    operator_startup_resume_record_lines,
)

#: Sentinel line preceding the single-line gate JSON payload in a durable journal note.
GATE_JOURNAL_MARKER = "[mozyo:operator-startup-gate:v=2]"


# ---------------------------------------------------------------------------
# Durable gate serialization (ticket-provider wire form).
# ---------------------------------------------------------------------------
def render_gate_journal(gate: OperatorStartupGate) -> str:
    """Render a pasteable durable journal note for a gate (human lines + JSON payload).

    The human lines are the state-appropriate pasteable renderer; the machine payload is
    ``gate.to_record()`` as compact single-line JSON after :data:`GATE_JOURNAL_MARKER`.
    Both are path/secret-safe by construction. ``owner_approved`` / ``operator_reported_done``
    have no dedicated human renderer (they are recorded by owner / operator action upstream),
    so only the machine payload is emitted for them.
    """
    if gate.state == STATE_REQUIRED:
        lines = list(operator_startup_gate_record_lines(gate))
    else:
        try:
            lines = list(operator_startup_resume_record_lines(gate))
        except OperatorStartupGateError:
            # owner_approved / operator_reported_done: machine payload only.
            lines = [
                f"- operator_startup_gate (gate {gate.gate_id}, "
                f"action_generation={gate.action_generation}, state={gate.state})."
            ]
    payload = json.dumps(gate.to_record(), sort_keys=True, separators=(",", ":"))
    return "\n".join([*lines, "", GATE_JOURNAL_MARKER, payload])


# Gate-read status (:class:`LatestGateRead`). A durable gate read is one of three: a
# valid gate, no gate record at all, or a CORRUPT newest gate record (fail-closed).
GATE_READ_GATE = "gate"  # a valid latest gate was read
GATE_READ_NONE = "no_gate"  # no gate-marker-bearing journal entry exists at all
GATE_READ_CORRUPT = "corrupt"  # the NEWEST gate-marker entry is malformed / schema-invalid


@dataclass(frozen=True)
class LatestGateRead:
    """The typed result of reading the latest durable gate (Finding 3, review j#79309).

    ``status`` is :data:`GATE_READ_GATE` (``gate`` present), :data:`GATE_READ_NONE`
    (nothing to resume), or :data:`GATE_READ_CORRUPT` (the newest gate record could not be
    parsed — fail closed, never fall back to an older gate). Distinguishing corrupt from
    absent is the whole point: a marker-absent journal entry is unrelated and skipped, but a
    marker-PRESENT-but-invalid newest record must stop the resume, not silently resume from a
    stale older gate.
    """

    status: str
    gate: Optional[OperatorStartupGate] = None


def note_has_gate_marker(notes: object) -> bool:
    """True when a note carries the gate sentinel (i.e. it is a gate record entry)."""
    return isinstance(notes, str) and GATE_JOURNAL_MARKER in notes


def parse_gate_from_note(notes: str) -> Optional[OperatorStartupGate]:
    """Parse the gate JSON payload from one gate-marker note, or None if malformed.

    Precondition: the note carries :data:`GATE_JOURNAL_MARKER` (check
    :func:`note_has_gate_marker` first). Decodes the first non-empty line after the sentinel
    with :meth:`OperatorStartupGate.from_record`. Malformed JSON or a record that fails the
    schema invariants returns None — the caller treats a malformed newest record as CORRUPT
    (fail-closed), never as "no gate".
    """
    if not note_has_gate_marker(notes):
        return None
    after = notes.split(GATE_JOURNAL_MARKER, 1)[1]
    for line in after.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(record, dict):
            return None
        try:
            return OperatorStartupGate.from_record(record)
        except OperatorStartupGateError:
            return None
    return None


def parse_latest_gate(entries: Sequence[object]) -> LatestGateRead:
    """Read the latest durable gate (newest-first), fail-closed on a corrupt newest record.

    ``entries`` is a sequence of objects exposing a ``.notes`` attribute (Redmine journal
    entries, chronological). Scans newest-first for the first *gate-marker-bearing* entry —
    that is the current gate record (the transition chain is append-only). If it parses, the
    result is :data:`GATE_READ_GATE`; if it is malformed / schema-invalid, the result is
    :data:`GATE_READ_CORRUPT` — the read **stops there** and never falls back to an older,
    stale gate (Finding 3, review j#79309: a corrupt supersede/consume record must not let a
    stale ``operator_reported_done`` gate resume). Journal entries WITHOUT the marker are
    unrelated and skipped; if none carry a marker, the result is :data:`GATE_READ_NONE`.
    """
    for entry in reversed(list(entries)):
        notes = getattr(entry, "notes", None)
        if not note_has_gate_marker(notes):
            continue  # unrelated journal entry — skip, keep scanning older entries.
        # This is the NEWEST gate record. It decides the read; no fallback past it.
        gate = parse_gate_from_note(notes)
        if gate is not None:
            return LatestGateRead(status=GATE_READ_GATE, gate=gate)
        return LatestGateRead(status=GATE_READ_CORRUPT)
    return LatestGateRead(status=GATE_READ_NONE)


# ---------------------------------------------------------------------------
# Ports (injectable seams; thin live defaults below, mirroring #13489's leg).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ObservedTargetResolution:
    """The action-time resolution of the gate's live target (port output).

    ``observed`` is the freshly-resolved :class:`ObservedStartupTarget` (resolved /
    ambiguous / unresolved), ``read_visible`` binds the #13760 visible-pane read for the
    resolved target, ``profile_version`` / ``classifier_version`` are the action-time
    provider-profile / classifier versions that stamp the projected gate's classification,
    and ``locator`` is the exact resolved live target handle the send binds to.
    """

    observed: ObservedStartupTarget
    read_visible: Callable[[], object]
    profile_version: str
    classifier_version: str
    locator: str = ""


#: Read the latest durable operator startup gate for an issue from the ticket provider.
#: Returns a typed :class:`LatestGateRead` (gate / no_gate / corrupt) so a corrupt newest
#: record fails closed rather than silently resuming from a stale older gate (Finding 3).
GateSource = Callable[[str], LatestGateRead]
#: Re-resolve the gate's live target + read primitive at action time, or None (unresolved).
TargetResolver = Callable[
    [OperatorStartupGate, Mapping[str, str]], Optional[ObservedTargetResolution]
]
#: Build the single high-level send that re-issues the original request anchor, given the
#: exact resolved live target ``locator``.
ResumeSendFactory = Callable[
    [OperatorStartupGate, str, str, Mapping[str, str]], Callable[[], SendOutcome]
]


class GateRecorder(Protocol):
    """Durably append the advanced gate transition to the ticket provider (typed port).

    ``preflight`` (checked BEFORE the reserve) is True only when the write path is available
    (write opt-in + trusted base URL + credential); ``record`` appends the advanced gate and
    returns True on a confirmed write, False on any transport failure (a record-failed the
    leg maps to operator reconcile — the fence stays the exactly-once authority).
    """

    def preflight(self) -> bool: ...

    def record(self, gate: OperatorStartupGate) -> bool: ...


def _utc_now() -> str:
    """ISO-8601 UTC timestamp at seconds precision (application-layer clock read)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def execute_startup_resume(
    args: object,
    issue: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    observed_at: Optional[str] = None,
    now: Optional[str] = None,
    gate_source: Optional[GateSource] = None,
    target_resolver: Optional[TargetResolver] = None,
    send_factory: Optional[ResumeSendFactory] = None,
    gate_recorder: Optional[GateRecorder] = None,
    fence: Optional[DispatchOutboxFence] = None,
) -> StartupResumeResult:
    """Drive the action-time resume for an issue (the live leg over injectable ports).

    Order (each fail-closed): read the latest durable gate from the ticket-provider port;
    re-resolve the exact live target / generation and bind the #13760 read at action time;
    build the single high-level send; bootstrap the fence (deletion-safe); drive
    :func:`resume_startup_gate` (which reserves + sends at most once, fail-closed); and,
    when it advances the gate, record the append-only transition durably.

    A missing durable gate (:data:`GATE_READ_NONE`) is :data:`RESUME_NOT_RESUMABLE`. A
    CORRUPT newest gate record (:data:`GATE_READ_CORRUPT`) is fail-closed zero-send — it
    NEVER falls back to an older stale gate (Finding 3, review j#79309). An un-resolvable
    live target feeds an ``unresolved`` observation to the orchestrator, which zero-sends
    (``resume_not_clear``). A lost / corrupt fence is :data:`RESUME_FENCE_UNAVAILABLE` with no
    send. ``send_factory`` / ``target_resolver`` / ``gate_source`` / ``gate_recorder`` /
    ``fence`` are injectable for hermetic tests; the defaults are the production bindings.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args

    environ: Mapping[str, str] = env if env is not None else _os_environ()
    repo_root = str(repo_root_from_args(args))
    stamp = observed_at or _utc_now()

    # 1. Re-read the latest durable gate + original anchor from the ticket-provider port.
    source = gate_source if gate_source is not None else _default_gate_source(repo_root, environ)
    read = source(str(issue).strip())
    if read.status == GATE_READ_CORRUPT:
        # The NEWEST gate record is malformed / schema-invalid: fail closed. Never resume
        # from a stale older gate (Finding 3) — a corrupt supersede/consume record must not
        # let an older `operator_reported_done` gate re-issue the request.
        return StartupResumeResult(
            result=RESUME_NOT_RESUMABLE,
            detail=(
                f"latest durable operator startup gate for issue {issue} is corrupt "
                "(malformed / schema-invalid); fail-closed, no fallback to an older gate"
            ),
        )
    if read.status != GATE_READ_GATE or read.gate is None:
        return StartupResumeResult(
            result=RESUME_NOT_RESUMABLE,
            detail=f"no durable operator startup gate found for issue {issue}",
        )
    gate = read.gate

    # 2. Re-resolve the exact live target + read primitive at action time.
    resolver = target_resolver if target_resolver is not None else _default_target_resolver
    resolution = resolver(gate, environ)
    if resolution is None:
        # Live target could not be resolved: hand the orchestrator an unresolved observation
        # so it zero-sends (identity_unresolved -> resume_not_clear), never a blind send.
        resolution = ObservedTargetResolution(
            observed=ObservedStartupTarget(resolution=RESOLUTION_UNRESOLVED),
            read_visible=lambda: "",
            profile_version="",
            classifier_version="",
        )

    # 3. Durable gate-transition WRITE preflight — BEFORE the reserve (j#79332 §5). If the
    #    write path is unavailable (write opt-in unset / no trusted base URL / no credential),
    #    a delivered send could not be durably recorded, so reserve/send 0.
    recorder = gate_recorder if gate_recorder is not None else _default_gate_recorder(issue, environ)
    if not recorder.preflight():
        return StartupResumeResult(
            result=RESUME_RECORDER_UNAVAILABLE,
            detail=(
                f"durable gate-transition writer unavailable for issue {issue} "
                "(write opt-in / trusted base URL / credential unset); reserve/send 0"
            ),
        )

    # 4. Build the single high-level send that re-issues the original anchor to the exact
    #    resolved live locator.
    factory = send_factory if send_factory is not None else _default_send_factory
    send = factory(gate, resolution.locator, repo_root, environ)

    # 5. Bootstrap the fence (deletion-safe; a store loss fails closed, never a fresh empty
    #    store that could re-send an already-delivered action — mirrors #13489's leg).
    outbox = fence if fence is not None else DispatchOutboxFence()
    try:
        outbox.bootstrap()
    except DispatchOutboxFenceError as exc:
        return StartupResumeResult(
            result=RESUME_FENCE_UNAVAILABLE,
            detail=f"dispatch outbox fence unavailable ({exc}); no send — operator recover() required",
        )

    # 6. Drive the exactly-once orchestrator (reserve + at most one send, fail-closed).
    result = resume_startup_gate(
        existing_gate=gate,
        observed=resolution.observed,
        read_visible=resolution.read_visible,
        fence=outbox,
        send=send,
        profile_version=resolution.profile_version,
        classifier_version=resolution.classifier_version,
        observed_at=stamp,
        now=now,
    )

    # 7. Durably append the gate transition when the orchestrator advanced it. A post-send
    #    append failure cannot roll back the send — the fence is authoritative (a delivered
    #    row already refuses a re-reserve), so this is surfaced as a typed record_failed /
    #    operator-reconcile flag, never a re-send (j#79332 §5).
    if result.advanced_gate is not None:
        if not recorder.record(result.advanced_gate):
            return dataclasses.replace(
                result,
                needs_reconcile=True,
                record_failed=True,
                detail=(
                    result.detail
                    + "; durable gate-transition append FAILED (operator reconcile; the "
                    "send is fenced exactly-once, the durable gate journal is behind)"
                ),
            )

    return result


# ---------------------------------------------------------------------------
# Live default bindings (lazy-imported; each composed of injectable sub-seams so the
# production composition root is proven with fakes). Every binding is fail-soft toward
# zero-send: an unresolved target / unavailable writer / unconfirmed send yields an
# unresolved observation / preflight-fail / uncertain outcome rather than a blind delivery.
# ---------------------------------------------------------------------------
def _os_environ() -> Mapping[str, str]:
    import os

    return os.environ


def _default_gate_source(repo_root: str, env: Mapping[str, str]) -> GateSource:
    """Default gate source: read the latest durable gate from the live Redmine journal."""

    def _read(issue_id: str) -> LatestGateRead:
        try:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
                LiveRedmineJournalSource,
            )

            source = LiveRedmineJournalSource.from_environment(environ=env)
            entries = source.read_entries(issue_id)
        except Exception:  # noqa: BLE001 - live transport / unconfigured creds -> no gate (fail-soft, zero-send)
            return LatestGateRead(status=GATE_READ_NONE)
        return parse_latest_gate(list(entries))

    return _read


def _default_target_resolver(
    gate: OperatorStartupGate, env: Mapping[str, str]
) -> Optional[ObservedTargetResolution]:
    """Default action-time target resolver: lifecycle + pins + inventory + attestation.

    Delegates to :class:`ResumeTargetResolver` (its live reads are injectable sub-seams).
    Returns None on any drift / mismatch so the leg zero-sends — never a blind send.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_target import (
        ResumeTargetResolver,
    )

    return ResumeTargetResolver(env=env).resolve(gate, env)


def _default_send_factory(
    gate: OperatorStartupGate, locator: str, repo_root: str, env: Mapping[str, str]
) -> Callable[[], SendOutcome]:
    """Default high-level send: one ``handoff send --record-format json`` to the live locator.

    Delegates to :class:`ResumeHandoffSendPort` (its runner is an injectable sub-seam). Only
    a positively confirmed landing maps to ``started``; every other outcome is uncertain.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_send import (
        ResumeHandoffSendPort,
    )

    return ResumeHandoffSendPort(locator=locator).build(gate, repo_root, env)


def _default_gate_recorder(issue: object, env: Mapping[str, str]) -> GateRecorder:
    """Default gate recorder: a credentialed ticket-provider append with pre-reserve preflight.

    Delegates to :class:`ResumeGateRecorder` (its transport / credential resolvers are
    injectable sub-seams). ``preflight`` gates on the write opt-in + base URL + credential;
    ``record`` appends the advanced gate journal and reports a confirmed write.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_record import (
        ResumeGateRecorder,
    )

    return ResumeGateRecorder(issue=str(issue).strip(), env=env)


__all__ = (
    "GATE_JOURNAL_MARKER",
    "GATE_READ_GATE",
    "GATE_READ_NONE",
    "GATE_READ_CORRUPT",
    "LatestGateRead",
    "ObservedTargetResolution",
    "GateSource",
    "TargetResolver",
    "ResumeSendFactory",
    "GateRecorder",
    "render_gate_journal",
    "note_has_gate_marker",
    "parse_gate_from_note",
    "parse_latest_gate",
    "execute_startup_resume",
)
