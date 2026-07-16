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
    RESUME_EXECUTION_ROOT_UNSAFE,
    RESUME_FENCE_UNAVAILABLE,
    RESUME_LEGACY_REAPPROVAL_REQUIRED,
    RESUME_NOT_RESUMABLE,
    RESUME_RECORDER_UNAVAILABLE,
    StartupResumeResult,
    resume_startup_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    OPERATOR_STARTUP_GATE_LEGACY_SCHEMA_VERSIONS,
    OPERATOR_STARTUP_GATE_SCHEMA_VERSION,
    STATE_REQUIRED,
    OperatorStartupGate,
    OperatorStartupGateError,
    schema_version_of,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (
    operator_startup_gate_record_lines,
    operator_startup_resume_record_lines,
)

#: Version-agnostic prefix of the sentinel line preceding the single-line gate JSON payload.
#: The trailing ``<version>]`` varies by schema version; detection matches the prefix so a
#: legacy ``v=2`` note is still recognized as a gate record (and classified as LEGACY via its
#: ``schema_version`` field), never skipped as unrelated prose (Design Answer j#79405 §B).
GATE_JOURNAL_MARKER_PREFIX = "[mozyo:operator-startup-gate:v="
#: Sentinel line for a CURRENT (v3) gate record — the wire form new records are written with.
GATE_JOURNAL_MARKER = f"{GATE_JOURNAL_MARKER_PREFIX}{OPERATOR_STARTUP_GATE_SCHEMA_VERSION}]"


# ---------------------------------------------------------------------------
# Durable gate serialization (ticket-provider wire form).
# ---------------------------------------------------------------------------
def render_gate_journal(gate: OperatorStartupGate, *, supersedes_note: str = "") -> str:
    """Render a pasteable durable journal note for a gate (human lines + JSON payload).

    The human lines are the state-appropriate pasteable renderer; the machine payload is
    ``gate.to_record()`` as compact single-line JSON after :data:`GATE_JOURNAL_MARKER`.
    Both are path/secret-safe by construction. ``owner_approved`` / ``operator_reported_done``
    have no dedicated human renderer (they are recorded by owner / operator action upstream),
    so only the machine payload is emitted for them. ``supersedes_note`` (when a fresh v3 gate is
    re-issued over a legacy one) is prepended as an auditable pointer to the superseded gate.
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
    if supersedes_note.strip():
        lines = [supersedes_note.strip(), *lines]
    payload = json.dumps(gate.to_record(), sort_keys=True, separators=(",", ":"))
    return "\n".join([*lines, "", GATE_JOURNAL_MARKER, payload])


# Gate-read status (:class:`LatestGateRead`). A durable gate read is one of five: a valid
# current (v3) gate, definitively no gate record at all, a readable LEGACY (v1/v2) record
# (reapproval required — never resumed), a CORRUPT newest gate record (fail-closed), or an
# UNREADABLE ticket-provider read (indeterminate — fail-closed, NOT conflated with no_gate).
GATE_READ_GATE = "gate"  # a valid current (v3) gate was read
GATE_READ_NONE = "no_gate"  # the journal read SUCCEEDED and carries no gate marker (proceed)
GATE_READ_LEGACY = "legacy"  # the NEWEST gate record is a readable legacy (v1/v2) schema
GATE_READ_CORRUPT = "corrupt"  # the NEWEST gate-marker entry is malformed / schema-invalid
GATE_READ_UNREADABLE = "unreadable"  # the ticket-provider read itself failed (indeterminate)


@dataclass(frozen=True)
class LatestGateRead:
    """The typed result of reading the latest durable gate (Finding 3, review j#79309).

    ``status`` is :data:`GATE_READ_GATE` (a current-v3 ``gate`` present), :data:`GATE_READ_NONE`
    (nothing to resume), :data:`GATE_READ_LEGACY` (the newest gate record is a readable legacy
    v1/v2 schema — it predates the runtime_role / lane_revision contract and routes to reapproval,
    Design Answer j#79405 §B), or :data:`GATE_READ_CORRUPT` (the newest gate record could not be
    parsed — fail closed, never fall back to an older gate). Distinguishing legacy / corrupt from
    absent is the whole point: a marker-absent journal entry is unrelated and skipped, but a
    marker-PRESENT newest record (legacy or invalid) must stop the resume, not silently resume
    from a stale older gate.
    """

    status: str
    gate: Optional[OperatorStartupGate] = None
    #: The raw legacy record dict when ``status`` is :data:`GATE_READ_LEGACY` — the reapproval leg
    #: structurally extracts the work anchor / lane / classification to re-observe a fresh v3 gate.
    legacy_record: Optional[dict] = None
    #: The durable ``journal_id`` of the selected legacy entry (the journal record's own id, not a
    #: pointer the note mentions) — the fresh v3 gate's ``supersedes`` marker names THIS journal so
    #: the reissue uniquely points at the superseded record (review j#79524 F1). None when the source
    #: entry has no journal id (the leg then fails closed to a manual reapproval, never auto-reissue).
    journal_id: Optional[str] = None


def note_has_gate_marker(notes: object) -> bool:
    """True when a note carries a gate sentinel of ANY schema version (it is a gate record).

    Matches the version-agnostic :data:`GATE_JOURNAL_MARKER_PREFIX` so a legacy ``v=2`` note is
    still recognized as a gate record (and classified via its ``schema_version`` field), never
    skipped as unrelated prose.
    """
    return isinstance(notes, str) and GATE_JOURNAL_MARKER_PREFIX in notes


def _gate_record_from_note(notes: str) -> Optional[dict]:
    """The raw gate record ``dict`` from a gate-marker note, or None if unreadable.

    Locates the marker line (any schema version) and decodes the first non-empty line after
    it as JSON. Malformed JSON / a non-dict payload / a missing marker returns None — the
    caller version-dispatches on the record's ``schema_version``.
    """
    if not note_has_gate_marker(notes):
        return None
    lines = notes.splitlines()
    marker_index = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(GATE_JOURNAL_MARKER_PREFIX) and stripped.endswith("]"):
            marker_index = index
            break
    if marker_index < 0:
        return None
    for line in lines[marker_index + 1:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            return None
        return record if isinstance(record, dict) else None
    return None


def _legacy_record_is_readable(record: object) -> bool:
    """True when a legacy (v1/v2) record is a structurally well-formed gate (readable, not corrupt).

    A readable legacy record carries the core gate skeleton — ``gate_id``, a ``state``, and the
    ``original_request`` / ``target`` / ``classification`` child objects with their core identity
    keys — so it can be identified as a real prior gate and routed to reapproval (review j#79481 F1).
    A bare ``{"schema_version": 2}`` fragment has none of that and is corrupt, not legacy. The check
    is structural only (it does NOT require the v3 ``runtime_role`` / ``lane_revision`` a legacy
    record legitimately lacks).
    """
    if not isinstance(record, Mapping):
        return False
    if not str(record.get("gate_id") or "").strip():
        return False
    if not str(record.get("state") or "").strip():
        return False
    target = record.get("target")
    original = record.get("original_request")
    classification = record.get("classification")
    if not (
        isinstance(target, Mapping)
        and isinstance(original, Mapping)
        and isinstance(classification, Mapping)
    ):
        return False
    target_keys = (
        "workspace_id",
        "repo_identity_digest",
        "execution_root",
        "lane_id",
        "target_role",
        "target_assigned_name",
        "provider_id",
        "agent_generation",
    )
    original_keys = ("source", "issue", "journal", "delivery_id")
    return all(k in target for k in target_keys) and all(k in original for k in original_keys)


def parse_gate_from_note(notes: str) -> Optional[OperatorStartupGate]:
    """Parse a CURRENT (v3) gate from one gate-marker note, or None if legacy / malformed.

    Precondition: the note carries a gate marker (check :func:`note_has_gate_marker` first).
    Returns a parsed gate ONLY for a current-v3 record; a readable legacy (v1/v2) record or a
    malformed / schema-invalid record returns None. The caller (:func:`parse_latest_gate`)
    distinguishes legacy from corrupt via :func:`schema_version_of`.
    """
    record = _gate_record_from_note(notes)
    if record is None:
        return None
    if schema_version_of(record) != OPERATOR_STARTUP_GATE_SCHEMA_VERSION:
        return None
    try:
        return OperatorStartupGate.from_record(record)
    except OperatorStartupGateError:
        return None


def parse_latest_gate(entries: Sequence[object]) -> LatestGateRead:
    """Read the latest durable gate (newest-first), version-dispatching the newest record.

    ``entries`` is a sequence of objects exposing a ``.notes`` attribute (Redmine journal
    entries, chronological). Scans newest-first for the first *gate-marker-bearing* entry —
    that is the current gate record (the transition chain is append-only). The NEWEST gate
    record decides the read; there is **no fallback** past it to an older, stale gate
    (Finding 3, review j#79309). It is classified by its ``schema_version`` (Design Answer
    j#79405 §B):

    * a current v3 record that parses -> :data:`GATE_READ_GATE`;
    * a STRUCTURALLY READABLE legacy v1/v2 record -> :data:`GATE_READ_LEGACY` (reapproval required
      — a legacy gate is never resumed, but it is not corrupt either);
    * any other version, unreadable JSON, a v3 record that fails the schema invariants, OR a legacy
      version whose payload is a malformed fragment (e.g. a bare ``{"schema_version": 2}``) ->
      :data:`GATE_READ_CORRUPT` (review j#79481 F1: readable-legacy is distinct from corrupt).

    Journal entries WITHOUT any gate marker are unrelated and skipped; if none carry a marker,
    the result is :data:`GATE_READ_NONE`.
    """
    for entry in reversed(list(entries)):
        notes = getattr(entry, "notes", None)
        if not note_has_gate_marker(notes):
            continue  # unrelated journal entry — skip, keep scanning older entries.
        # This is the NEWEST gate record. It decides the read; no fallback past it.
        record = _gate_record_from_note(notes)
        if record is None:
            return LatestGateRead(status=GATE_READ_CORRUPT)
        version = schema_version_of(record)
        if version in OPERATOR_STARTUP_GATE_LEGACY_SCHEMA_VERSIONS:
            # A readable legacy record -> reapproval (carry it + the entry's durable journal_id for
            # re-observation and the supersedes pointer); a malformed legacy fragment -> corrupt.
            if _legacy_record_is_readable(record):
                journal_id = str(getattr(entry, "journal_id", "") or "").strip() or None
                return LatestGateRead(
                    status=GATE_READ_LEGACY, legacy_record=record, journal_id=journal_id
                )
            return LatestGateRead(status=GATE_READ_CORRUPT)
        if version != OPERATOR_STARTUP_GATE_SCHEMA_VERSION:
            return LatestGateRead(status=GATE_READ_CORRUPT)
        try:
            gate = OperatorStartupGate.from_record(record)
        except OperatorStartupGateError:
            return LatestGateRead(status=GATE_READ_CORRUPT)
        return LatestGateRead(status=GATE_READ_GATE, gate=gate)
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
#: Re-observe a fresh v3 ``required`` gate for a readable legacy (v1/v2) latest gate, from the
#: authoritative lifecycle observation (``legacy_record, issue, repo_root``), or None on any drift /
#: producer error (fail-closed). The default binds the production :mod:`operator_startup_gate_producer`.
LegacyReissuer = Callable[[dict, str, str], Optional[OperatorStartupGate]]


class GateRecorder(Protocol):
    """Durably append the advanced gate transition to the ticket provider (typed port).

    ``preflight`` (checked BEFORE the reserve) is True only when the write path is available
    (write opt-in + trusted base URL + credential); ``record`` appends the advanced gate and
    returns True on a confirmed write, False on any transport failure (a record-failed the
    leg maps to operator reconcile — the fence stays the exactly-once authority).
    """

    def preflight(self) -> bool: ...

    def record(self, gate: OperatorStartupGate) -> bool: ...

    def record_reissue(self, gate: OperatorStartupGate, supersedes_note: str) -> bool:
        """Append a fresh v3 gate journal carrying a ``supersedes`` pointer (legacy reapproval)."""
        ...


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
    legacy_reissuer: Optional[LegacyReissuer] = None,
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

    # 1. Re-read the latest durable gate + original anchor from the ticket-provider port. A source
    #    that raises is treated as UNREADABLE (indeterminate -> fail closed), never propagated as an
    #    exception that could abort the whole workflow step (review j#79524 F3).
    source = gate_source if gate_source is not None else _default_gate_source(repo_root, environ)
    try:
        read = source(str(issue).strip())
    except Exception:  # noqa: BLE001 - a raising source is indeterminate -> UNREADABLE (zero-send)
        read = LatestGateRead(status=GATE_READ_UNREADABLE)
    if read.status in (GATE_READ_CORRUPT, GATE_READ_UNREADABLE):
        # The newest gate record is malformed/schema-invalid, OR the ticket-provider read itself
        # failed (indeterminate): fail closed with zero actuation (review j#79309 F3 / j#79504 F1).
        # Never resume from a stale older gate, and never conflate an unreadable read with "no gate".
        why = "corrupt (malformed / schema-invalid)" if read.status == GATE_READ_CORRUPT else (
            "unreadable (ticket-provider read failed; indeterminate)"
        )
        return StartupResumeResult(
            result=RESUME_NOT_RESUMABLE,
            detail=(
                f"latest durable operator startup gate for issue {issue} is {why}; "
                "fail-closed, no fallback and no send"
            ),
        )
    if read.status == GATE_READ_LEGACY:
        # The NEWEST gate record is a readable legacy (v1/v2) schema: it predates the v3
        # runtime_role / lane_revision contract, so resuming it would fabricate an exact-revision
        # approval (Design Answer j#79405 §B). Reserve/send 0. Re-observe a FRESH v3 `required`
        # gate from the authoritative lifecycle observation (production producer) and durably
        # record it with a `supersedes` pointer to the legacy gate — the newest-wins append chain
        # makes it supersede the legacy record, and it awaits a fresh owner approval (review j#79504
        # F2). A producer drift / unavailable writer degrades to guiding a MANUAL reapproval.
        legacy_record = read.legacy_record or {}
        reissuer = legacy_reissuer if legacy_reissuer is not None else _default_legacy_reissuer
        try:
            fresh = reissuer(legacy_record, str(issue).strip(), repo_root)
        except Exception:  # noqa: BLE001 - reissue failure -> manual reapproval (never actuation)
            fresh = None
        reissued = False
        # The supersedes pointer must name the EXACT superseded journal id (review j#79524 F1). If
        # the source entry carries no journal id, DO NOT auto-reissue (the fresh record could not
        # unambiguously supersede the legacy one) — fall back to a manual reapproval.
        if fresh is not None and read.journal_id:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_producer import (
                reissue_supersedes_note,
            )

            recorder = gate_recorder if gate_recorder is not None else _default_gate_recorder(
                issue, environ
            )
            if recorder.preflight():
                note = reissue_supersedes_note(superseded_journal=read.journal_id)
                reissued = recorder.record_reissue(fresh, note)
        detail = (
            f"latest durable operator startup gate for issue {issue} is a legacy (v1/v2) record; "
            + (
                "re-observed and durably recorded a fresh v3 required gate (supersedes the legacy "
                "gate, awaiting fresh owner approval); reserve/send 0, no legacy backfill"
                if reissued
                else "reserve/send 0, manual operator re-approval of a fresh v3 gate required "
                "(no fresh observation recorded)"
            )
        )
        # `advanced_gate` is deliberately unset: a fresh v3 `required` gate is a NEW gate (recorded
        # via record_reissue above), not a same-gate transition of the legacy record.
        return StartupResumeResult(result=RESUME_LEGACY_REAPPROVAL_REQUIRED, detail=detail)
    if read.status != GATE_READ_GATE or read.gate is None:
        return StartupResumeResult(
            result=RESUME_NOT_RESUMABLE,
            detail=f"no durable operator startup gate found for issue {issue}",
        )
    gate = read.gate

    # 2. Re-resolve the exact live target + read primitive at action time. The default resolver
    #    binds to the EXPLICIT repo root (registry + provider-binding authority, j#79405 §A/§C).
    if target_resolver is not None:
        resolution = target_resolver(gate, environ)
    else:
        resolution = _default_target_resolver(gate, environ, repo_root)
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

    # 3b. Execution-root safety — BEFORE the reserve (Design Answer j#79405 §C). The gate's
    #     repo-relative execution_root must resolve to a `--workdir` at or under the freshly
    #     resolved repo root; an escape / unresolvable root fails closed with reserve/send 0 so a
    #     re-issue can never land outside the pinned execution root. (The send port derives the
    #     identical workdir from the same pure helper; this pre-checks it before touching the fence.)
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_send import (
        resolve_execution_workdir,
    )

    if resolve_execution_workdir(repo_root, gate.target.execution_root) is None:
        return StartupResumeResult(
            result=RESUME_EXECUTION_ROOT_UNSAFE,
            detail=(
                f"gate execution_root {gate.target.execution_root!r} does not safely resolve "
                f"under the action-time repo root for issue {issue}; reserve/send 0"
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
        except Exception:  # noqa: BLE001 - ticket provider NOT CONFIGURED (no creds / base URL)
            # No ticket-provider read path exists here, so there is no startup-gate system to
            # consult: this is a definitive "no gate" (proceed), NOT an indeterminate read. Keeping
            # these distinct is what stops an unconfigured environment from hijacking every step.
            return LatestGateRead(status=GATE_READ_NONE)
        try:
            entries = source.read_entries(issue_id)
        except Exception:  # noqa: BLE001 - CONFIGURED but the read FAILED -> indeterminate
            # The provider is configured but the read failed (transient / transport): fail closed,
            # never conflated with "no gate" (review j#79504 F1). The top-level workflow zero-actuates.
            return LatestGateRead(status=GATE_READ_UNREADABLE)
        return parse_latest_gate(list(entries))

    return _read


def _default_target_resolver(
    gate: OperatorStartupGate, env: Mapping[str, str], repo_root: str
) -> Optional[ObservedTargetResolution]:
    """Default action-time target resolver: lifecycle + binding + pins + inventory + attestation.

    Delegates to :class:`ResumeTargetResolver`, bound to the EXPLICIT ``repo_root`` so its
    provider-binding and registry-workspace re-resolution read the target repo's authority (not
    the anchor-less sender identity — j#79405 §A/§C). Its live reads are injectable sub-seams;
    it returns None on any drift / mismatch so the leg zero-sends — never a blind send.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_target import (
        ResumeTargetResolver,
    )

    return ResumeTargetResolver(env=env, repo_root=repo_root).resolve(gate, env)


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


def _default_legacy_reissuer(
    legacy_record: dict, issue: str, repo_root: str
) -> Optional[OperatorStartupGate]:
    """Re-observe a fresh v3 ``required`` gate for a readable legacy latest, or None (fail-closed).

    Structurally extracts the work anchor / lane / workflow role / classification from the legacy
    record (never its runtime identity — that is re-observed), reads the authoritative live lifecycle
    record + the repo's provider binding, and builds a fresh v3 gate through the production
    :func:`build_v3_required_gate_from_observation`. The fresh gate carries its runtime_role / provider
    / generation / revision / workspace from the CURRENT observation (Design Answer j#79405 §B — no
    legacy backfill); its ``action_generation`` is the legacy generation + 1.

    The current lifecycle row must be a *valid fresh authority* — **active**, bound to the exact
    workspace / lane, and owning the legacy gate's original-request issue (review j#79524 F2,
    mirroring the resume resolver's ``record.issue_id == gate.original_request.issue`` gate) —
    otherwise a retired or foreign-issue lane could mint an invalid approval target. Any missing
    field / inactive / foreign-issue / unreadable lifecycle / producer drift returns None so the
    leg guides a manual reapproval instead. ``issue`` (the read anchor) must be present.
    """
    target = legacy_record.get("target") or {}
    workspace_id = str(target.get("workspace_id") or "").strip()
    lane_id = str(target.get("lane_id") or "").strip()
    workflow_role = str(target.get("target_role") or "").strip()
    execution_root = str(target.get("execution_root") or "").strip()
    gate_id = str(legacy_record.get("gate_id") or "").strip()
    original_issue = str((legacy_record.get("original_request") or {}).get("issue") or "").strip()
    read_anchor = str(issue or "").strip()
    try:
        action_generation = int(legacy_record.get("action_generation"))
    except (TypeError, ValueError):
        return None
    if not (
        workspace_id
        and lane_id
        and workflow_role
        and execution_root
        and gate_id
        and original_issue
        and read_anchor
    ):
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_producer import (
            build_v3_required_gate_from_observation,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_target import (
            _default_lifecycle_get,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
            load_workflow_binding,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
            GateClassification,
            OriginalRequest,
        )

        lifecycle = _default_lifecycle_get(workspace_id, lane_id)
        if lifecycle is None:
            return None
        # The row must be a valid fresh authority: active, exact workspace/lane, and owning the
        # legacy gate's original-request issue (mirrors the resume resolver's binding gate).
        if getattr(lifecycle, "lane_disposition", None) != DISPOSITION_ACTIVE:
            return None
        if str(getattr(lifecycle, "repo_workspace_id", "")).strip() != workspace_id:
            return None
        if str(getattr(lifecycle, "lane_id", "")).strip() != lane_id:
            return None
        if str(getattr(lifecycle, "issue_id", "")).strip() != original_issue:
            return None
        binding, _warnings = load_workflow_binding(repo_root)
        return build_v3_required_gate_from_observation(
            record=lifecycle,
            binding=binding,
            workflow_role=workflow_role,
            execution_root=execution_root,
            gate_id=gate_id,
            action_generation=action_generation + 1,
            original_request=OriginalRequest.from_record(legacy_record.get("original_request") or {}),
            classification=GateClassification.from_record(legacy_record.get("classification") or {}),
        )
    except Exception:  # noqa: BLE001 - producer drift / unreadable observation -> manual reapproval
        return None


__all__ = (
    "GATE_JOURNAL_MARKER",
    "GATE_JOURNAL_MARKER_PREFIX",
    "GATE_READ_GATE",
    "GATE_READ_NONE",
    "GATE_READ_LEGACY",
    "GATE_READ_CORRUPT",
    "GATE_READ_UNREADABLE",
    "LatestGateRead",
    "ObservedTargetResolution",
    "GateSource",
    "TargetResolver",
    "ResumeSendFactory",
    "LegacyReissuer",
    "GateRecorder",
    "render_gate_journal",
    "note_has_gate_marker",
    "parse_gate_from_note",
    "parse_latest_gate",
    "execute_startup_resume",
)
