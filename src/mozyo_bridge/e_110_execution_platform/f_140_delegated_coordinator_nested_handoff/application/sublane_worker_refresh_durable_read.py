"""The durable worker-progress read for the guarded live-worker refresh (Redmine #14661).

Split out of :mod:`.sublane_worker_refresh_live` at the responsibility seam — that module
observes the LIVE runtime (inventory, render, attestation), while this one reads the DURABLE
record and answers one question: did this worker's own progress land after the anchor? Keeping
both in one module pushed it past the module-health line; the answer is a cohesive split at an
existing boundary, not an allowlist entry recording the growth.

Pure with respect to the runtime: the caller injects the durable reader and declares its
freshness, so every branch is testable without a network or a store.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WorkerRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    strict_marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_PROGRESS_GATES,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)


def worker_progress_facts(
    request: WorkerRefreshRequest, *, journal_reader, journal_reader_fresh: bool,
) -> tuple[bool, bool, bool]:
    """(landed, absent, fresh): the anchored + ordered fresh durable re-read (#13889).

    Worker progress is a structured gate marker of a :data:`WORKER_PROGRESS_GATES` kind on
    a journal STRICTLY AFTER the anchor (ordered on durable journal ids, never wall-clock),
    in the anchor issue. No worker gate marker carries a causal back-pointer to the request
    it answers, so the causal link is ordering + lane binding, resolved in the SAFE
    direction:

    - a marker carrying the #14219 lane envelope must match BOTH the pinned lane and the
      pinned lane generation — a different lane's or a superseded generation's gate is not
      this turn's progress;
    - a marker WITHOUT an envelope still counts as progress. Unknown provenance classifies
      ``turn_productive``, which REFUSES the refresh: the only mistake this direction can
      make is declining to close a worker, while the reverse would close one that had in
      fact delivered its gate.

    No reader / an unreadable read / a non-fresh (snapshot) reader leaves all facts
    ``False`` — unobservable, never "absent".
    """
    reader = journal_reader
    if reader is None or not journal_reader_fresh:
        return False, False, False
    try:
        anchor = int(_norm(request.resume_anchor_journal))
    except (TypeError, ValueError):
        return False, False, False
    try:
        entries = reader(request.effective_anchor_issue)
    except Exception:  # noqa: BLE001 - unreadable durable source => unobservable
        return False, False, False
    for entry in entries:
        try:
            jid = int(_norm(getattr(entry, "journal_id", "")))
        except (TypeError, ValueError):
            continue
        if jid <= anchor:
            continue
        notes = str(getattr(entry, "notes", "") or "")
        if notes_carry_worker_progress(request, notes):
            return True, False, True
    return False, True, True

def notes_carry_worker_progress(request: WorkerRefreshRequest, notes: str) -> bool:
    """Does this journal note carry a worker-progress gate marker for this lane? (pure)

    Read STRICTLY, and fail closed toward "progress" (Redmine #14687 review j#93273 R1-F1).
    This answer reaches an effect: ``False`` here becomes ``expected_gate_absent`` on the turn
    observation, which is the ONLY path to ``turn_failed_no_durable_gate`` — the one class that
    admits the guarded worker refresh, a destructive close.

    The lenient fold is unusable for that. Measured on the pre-fix head, with every other axis
    held at the refresh-admitting value:

        [mozyo:workflow-event:gate=implementation_done]           -> turn_productive
        [mozyo:workflow-event:gate=implementation_done:gate=zzz]  -> turn_failed_no_durable_gate
        [mozyo:workflow-event:gate=zzz:gate=implementation_done]  -> turn_productive

    A note that DOES declare ``implementation_done`` did not merely go unnoticed — last-write-wins
    erased the first declaration, so the reader positively asserted the gate was ABSENT and closed
    the worker that had delivered it. Whether that happened depended on which occurrence came
    last, which is not a safety property.

    Parsing strictly and DROPPING the unreadable marker would keep the hole, because a refused
    marker is also "no progress". So the unit is the NOTE: if any marker in it is one the
    canonical producer could not have rendered, the note's provenance is unknown, and unknown
    provenance counts as progress — the direction whose only failure mode is declining to close a
    worker (:func:`worker_progress_facts`, and the central `### Hibernate Evidence Marker
    Contract`'s "fragment を捨てて残りを一致させず marker 全体を fail-closed とする").

    A note carrying no marker at all is not unreadable; it simply carries no progress.
    """
    try:
        markers = strict_marker_fields_in_note(notes)
    except Exception:  # noqa: BLE001 - an unreadable note has unknown provenance, not "absent"
        return True
    if markers is None:
        # At least one marker is not producer-renderable. Refusing the whole note is the
        # contract's own rule, and here it lands on the safe side: refuse the refresh.
        return True
    for channel, fields in markers:
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        if _norm(fields.get("gate")) not in WORKER_PROGRESS_GATES:
            continue
        lane = fields.get("lane")
        generation = fields.get("lane_generation")
        if lane is None or generation is None:
            # Unenveloped — or only PARTIALLY enveloped, which the canonical producer
            # cannot emit at all (the lane envelope is all-or-none). Either way the lane
            # provenance is unreadable, so it counts as progress: the safe direction is
            # ``turn_productive`` (refuse the refresh). Requiring an exact match here
            # would silently skip a half-enveloped worker gate and admit a close of the
            # worker that had in fact delivered it.
            return True
        if (
            _norm_lane(lane) == _norm_lane(request.lane)
            and _norm(generation) == _norm(request.lane_generation)
        ):
            return True
    return False


__all__ = ("notes_carry_worker_progress", "worker_progress_facts")
