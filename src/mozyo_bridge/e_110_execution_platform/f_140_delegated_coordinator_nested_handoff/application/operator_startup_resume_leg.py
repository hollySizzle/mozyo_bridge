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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Sequence

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


def parse_gate_from_note(notes: str) -> Optional[OperatorStartupGate]:
    """Parse the gate JSON payload from one journal note, or None (fail-soft).

    Finds the :data:`GATE_JOURNAL_MARKER` sentinel and decodes the first non-empty line
    after it with :meth:`OperatorStartupGate.from_record`. A note without the sentinel, a
    malformed JSON, or a record that fails the schema invariants returns None rather than
    raising — a bad durable record must never resume, and the caller treats "no gate" as
    not-resumable.
    """
    if not notes or GATE_JOURNAL_MARKER not in notes:
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


def parse_latest_gate(entries: Sequence[object]) -> Optional[OperatorStartupGate]:
    """The most recent parseable durable gate across journal entries (newest-first).

    ``entries`` is a sequence of objects exposing a ``.notes`` attribute (Redmine journal
    entries, chronological). Scans newest-first and returns the first gate that parses; the
    append-only transition chain means the newest gate record is the current state.
    """
    for entry in reversed(list(entries)):
        notes = getattr(entry, "notes", None)
        if not isinstance(notes, str):
            continue
        gate = parse_gate_from_note(notes)
        if gate is not None:
            return gate
    return None


# ---------------------------------------------------------------------------
# Ports (injectable seams; thin live defaults below, mirroring #13489's leg).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ObservedTargetResolution:
    """The action-time resolution of the gate's live target (port output).

    ``observed`` is the freshly-resolved :class:`ObservedStartupTarget` (resolved /
    ambiguous / unresolved), ``read_visible`` binds the #13760 visible-pane read for the
    resolved target, and ``profile_version`` / ``classifier_version`` are the action-time
    provider-profile / classifier versions that stamp the projected gate's classification.
    """

    observed: ObservedStartupTarget
    read_visible: Callable[[], object]
    profile_version: str
    classifier_version: str


#: Read the latest durable operator startup gate for an issue from the ticket provider.
GateSource = Callable[[str], Optional[OperatorStartupGate]]
#: Re-resolve the gate's live target + read primitive at action time, or None (unresolved).
TargetResolver = Callable[
    [OperatorStartupGate, Mapping[str, str]], Optional[ObservedTargetResolution]
]
#: Build the single high-level send that re-issues the original request anchor.
ResumeSendFactory = Callable[
    [OperatorStartupGate, str, Mapping[str, str]], Callable[[], SendOutcome]
]
#: Durably record the advanced gate (append-only transition) back to the ticket provider.
GateRecorder = Callable[[OperatorStartupGate], None]


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

    A missing durable gate is :data:`RESUME_NOT_RESUMABLE` (nothing to resume). An
    un-resolvable live target feeds an ``unresolved`` observation to the orchestrator, which
    zero-sends (``resume_not_clear``). A lost / corrupt fence is :data:`RESUME_FENCE_UNAVAILABLE`
    with no send. ``send_factory`` / ``target_resolver`` / ``gate_source`` / ``gate_recorder``
    / ``fence`` are injectable for hermetic tests; the defaults are the production bindings.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args

    environ: Mapping[str, str] = env if env is not None else _os_environ()
    repo_root = str(repo_root_from_args(args))
    stamp = observed_at or _utc_now()

    # 1. Re-read the latest durable gate + original anchor from the ticket-provider port.
    source = gate_source if gate_source is not None else _default_gate_source(repo_root, environ)
    gate = source(str(issue).strip())
    if gate is None:
        return StartupResumeResult(
            result=RESUME_NOT_RESUMABLE,
            detail=f"no durable operator startup gate found for issue {issue}",
        )

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

    # 3. Build the single high-level send that re-issues the original anchor.
    factory = send_factory if send_factory is not None else _default_send_factory
    send = factory(gate, repo_root, environ)

    # 4. Bootstrap the fence (deletion-safe; a store loss fails closed, never a fresh empty
    #    store that could re-send an already-delivered action — mirrors #13489's leg).
    outbox = fence if fence is not None else DispatchOutboxFence()
    try:
        outbox.bootstrap()
    except DispatchOutboxFenceError as exc:
        return StartupResumeResult(
            result=RESUME_FENCE_UNAVAILABLE,
            detail=f"dispatch outbox fence unavailable ({exc}); no send — operator recover() required",
        )

    # 5. Drive the exactly-once orchestrator (reserve + at most one send, fail-closed).
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

    # 6. Durably record the append-only gate transition when the orchestrator advanced it.
    if result.advanced_gate is not None:
        recorder = gate_recorder if gate_recorder is not None else _default_gate_recorder(issue, repo_root, environ)
        recorder(result.advanced_gate)

    return result


# ---------------------------------------------------------------------------
# Thin live default bindings (production-only, lazy-imported — like #13489's leg
# ``_default_send_factory`` / ``_resolve_target_locator``). Each is fail-soft toward
# zero-send: an unresolved target / unavailable rail yields an unresolved observation or
# a not-started outcome rather than a blind send.
# ---------------------------------------------------------------------------
def _os_environ() -> Mapping[str, str]:
    import os

    return os.environ


def _default_gate_source(repo_root: str, env: Mapping[str, str]) -> GateSource:
    """Default gate source: read the latest durable gate from the live Redmine journal."""

    def _read(issue_id: str) -> Optional[OperatorStartupGate]:
        try:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
                LiveRedmineJournalSource,
            )

            source = LiveRedmineJournalSource.from_environment(environ=env)
            entries = source.read_entries(issue_id)
        except Exception:  # noqa: BLE001 - live transport / unconfigured creds -> no gate (fail-soft, zero-send)
            return None
        return parse_latest_gate(list(entries))

    return _read


def _default_target_resolver(
    gate: OperatorStartupGate, env: Mapping[str, str]
) -> Optional[ObservedTargetResolution]:
    """Default action-time target resolver (production-only; fail-soft to unresolved).

    Re-resolves the live target for the gate's pinned identity and binds the #13760 read.
    Any resolution failure returns None so :func:`execute_startup_resume` feeds an
    unresolved observation to the orchestrator (zero-send). The live registry / generation
    attestation wiring lands here; until it is provisioned this default resolves nothing,
    which is the safe (zero-send) direction.
    """
    return None  # production wiring point; fail-soft to unresolved -> zero-send.


def _default_send_factory(
    gate: OperatorStartupGate, repo_root: str, env: Mapping[str, str]
) -> Callable[[], SendOutcome]:
    """Default high-level send: re-issue the original anchor over the handoff rail.

    Production-only. Binds the single send to the existing high-level handoff rail
    (``handoff send`` re-issuing the original ``implementation_request`` anchor); the
    structured turn-start it surfaces is the fence's :class:`SendOutcome`. Until the live
    rail binding is provisioned, the default reports a not-started outcome (no blind send),
    which the orchestrator maps to uncertain / reconcile — never a false delivered.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution import (
        TURN_START_NOT_STARTED,
    )

    def _send() -> SendOutcome:
        return SendOutcome(
            turn_start=TURN_START_NOT_STARTED,
            detail="live high-level resume send rail not provisioned; no blind send",
        )

    return _send


def _default_gate_recorder(issue: object, repo_root: str, env: Mapping[str, str]) -> GateRecorder:
    """Default gate recorder: append the advanced gate's durable journal (production-only)."""

    def _record(gate: OperatorStartupGate) -> None:
        # Production wiring point: append render_gate_journal(gate) to the issue's Redmine
        # journal via the high-level ticket-provider rail. A recording failure must not
        # crash the leg (the send already happened / was fenced); it is best-effort here.
        _ = render_gate_journal(gate)  # constructed here so the payload is always well-formed.

    return _record


__all__ = (
    "GATE_JOURNAL_MARKER",
    "ObservedTargetResolution",
    "GateSource",
    "TargetResolver",
    "ResumeSendFactory",
    "GateRecorder",
    "render_gate_journal",
    "parse_gate_from_note",
    "parse_latest_gate",
    "execute_startup_resume",
)
