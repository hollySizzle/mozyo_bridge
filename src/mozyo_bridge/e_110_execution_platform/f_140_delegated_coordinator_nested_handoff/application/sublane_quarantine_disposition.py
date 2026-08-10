"""`sublane quarantine` generation-mismatch disposition wiring (Redmine #15193).

Split out of :mod:`sublane_quarantine` for module-health reduction — the same treatment
:mod:`sublane_hibernate_toctou` and :mod:`sublane_hibernate_preflight` received — and kept
here as its own unit because the disposition is a distinct authorization with its own
action-time contract, not a variation of the ordinary quarantine approval.

The pure decision vocabulary lives in
:mod:`...domain.generation_mismatch_disposition`; this module holds the two application
seams that bind it to the quarantine command:

- :func:`disposition_drift` — the action-time re-comparison of an approval against a FRESH
  observation, run at every edge that could close a receiver.
- :func:`register_disposition_flags` — the five CLI tokens that turn an ordinary quarantine
  request into a disposition.

See :mod:`...domain.generation_mismatch_disposition` for why the rail exists and what the
#15110 / #15140 / #15195 deadlock was.
"""

from __future__ import annotations

import argparse

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.generation_mismatch_disposition import (  # noqa: E501
    DISPOSITION_APPROVAL_INCOMPLETE,
    DISPOSITION_LIFECYCLE_ABSENT,
    DISPOSITION_LIFECYCLE_PINS_INVALID,
    DRIFT_GENERATION_AXES,
    DRIFT_LANE_GENERATION,
    DRIFT_LIFECYCLE_REVISION,
    DRIFT_PENDING_IDENTITY,
    PENDING_EFFECT_DISCARDED_ON_REPLACE,
    PENDING_EFFECT_PRESERVED,
    pending_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    PendingComposerClassification,
    agent_state_is_working,
    ordered_generation_axes,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)


def disposition_drift(
    request: "QuarantineRequest",
    inspection: "QuarantineInspection",
    classification: PendingComposerClassification,
) -> str:
    """Why a disposition approval may NOT act on the receiver that is live right now (pure).

    Redmine #15193 requirement 3: the approval is re-compared against a FRESH observation at
    action time, on the same single snapshot the rest of the fences use. Returns a
    comma-joined token list from the closed drift vocabulary, or ``""`` when the approval
    still describes exactly what is live. Every non-empty result is zero-mutation: the caller
    returns before the disposition CAS and before any process close.

    The comparison is deliberately narrower than the approval's full token set: workspace,
    lane, role, assigned name, locator and action generation are ALREADY re-verified by the
    surrounding fences (the ``action_generation`` recomputation, the release pin, and the
    lifecycle-owner check), so re-checking them here would only duplicate an existing gate.
    What is checked here is what nothing else checks — the mismatch axes and the identity of
    the pending input being discarded.
    """
    reasons: list[str] = []
    if agent_state_is_working(inspection.signal.agent_state):
        # Read from the RAW agent state, never from the classification label: the classifier
        # puts `generation_mismatch` ABOVE `agent_working`, so a mismatched receiver whose
        # worker is mid-turn still labels `generation_mismatch` and would otherwise satisfy
        # `generation_mismatch_with_pending`. Inferring idleness from the label would let
        # this rail close a pane on a running turn — the one thing every sibling fence
        # refuses absolutely.
        return "a live worker turn is in flight; active work is never disposed of"
    if not classification.generation_mismatch_with_pending:
        # The approval was minted for "mismatch + real pending input". If either half is no
        # longer true, the state the owner approved over does not exist: an empty composer
        # means there is nothing to dispose of (hibernate directly), a healed generation
        # means the ordinary quarantine rail applies, and an unreadable composer proves
        # nothing at all.
        return "state is no longer a generation mismatch holding a real pending input"
    if request.approved_pending_effect != PENDING_EFFECT_DISCARDED_ON_REPLACE:
        # A replacement destroys the composer it replaces; an approval claiming to preserve
        # it would be describing something this actuation cannot do.
        return "approved pending effect does not authorize the discard this action performs"
    approved_axes = ordered_generation_axes(tuple(request.approved_generation_axes))
    observed_axes = ordered_generation_axes(classification.generation_axes)
    if approved_axes != observed_axes:
        reasons.append(DRIFT_GENERATION_AXES)
    observed_pending = pending_identity(
        pending_observed=classification.pending_observed,
        correlated_marker_ids=inspection.signal.correlated_marker_ids,
    )
    if _norm(request.approved_pending_identity) != observed_pending:
        reasons.append(DRIFT_PENDING_IDENTITY)
    return ",".join(reasons)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def disposition_request_reason(request: "QuarantineRequest") -> str:
    """Typed refusal for a partial or invalid five-token disposition approval."""
    supplied = (
        bool(request.approved_generation_axes),
        bool(_norm(request.approved_pending_identity)),
        bool(_norm(request.approved_pending_effect)),
        request.approved_lane_generation != -1,
        request.approved_lifecycle_revision != -1,
    )
    if not any(supplied):
        return ""
    if not all(supplied):
        return DISPOSITION_APPROVAL_INCOMPLETE
    if not all(
        _positive_int(value)
        for value in (
            request.approved_lane_generation,
            request.approved_lifecycle_revision,
        )
    ):
        return DISPOSITION_LIFECYCLE_PINS_INVALID
    return ""


def disposition_lifecycle_reason(
    request: "QuarantineRequest", record: object, *, expected_revision: int
) -> str:
    """Recompare the approval's incarnation pins with one fresh lifecycle row."""
    request_reason = disposition_request_reason(request)
    if request_reason or not request.is_disposition:
        return request_reason
    if record is None:
        return DISPOSITION_LIFECYCLE_ABSENT
    generation = getattr(record, "lane_generation", None)
    revision = getattr(record, "revision", None)
    if not _positive_int(generation) or not _positive_int(revision):
        return DISPOSITION_LIFECYCLE_PINS_INVALID
    if generation != request.approved_lane_generation:
        return DRIFT_LANE_GENERATION
    if revision != expected_revision:
        return DRIFT_LIFECYCLE_REVISION
    return ""


def register_disposition_flags(parser: argparse.ArgumentParser) -> None:
    """Add the five #15193 disposition tokens to the `sublane quarantine` parser."""
    # Generation-mismatch disposition tokens (Redmine #15193). Optional and only meaningful
    # TOGETHER: supplying all five turns the request into a disposition, which is the only
    # way a `generation_mismatch` receiver holding a real pending input can be acted on. A
    # partial set stays an ordinary quarantine and is refused as not-eligible, so no caller
    # can unlock the mismatch path without also stating the pending input's fate.
    parser.add_argument(
        "--approved-generation-axes",
        dest="approved_generation_axes",
        default="",
        help=(
            "Redmine #15193: comma-separated generation mismatch axes the approval is "
            "granted over (identity,revision,workspace_cwd,pair,row_ambiguous). Must equal "
            "the axes observed at action time or the execute fails closed. Obtain from "
            "`sublane quarantine-inspect`; do not hand-assemble."
        ),
    )
    parser.add_argument(
        "--approved-pending-identity",
        dest="approved_pending_identity",
        default="",
        help=(
            "Redmine #15193: identity of the pending composer input the owner approved "
            "discarding. Re-verified at action time so a DIFFERENT input that arrived "
            "since the approval is never silently destroyed."
        ),
    )
    parser.add_argument(
        "--approved-pending-effect",
        dest="approved_pending_effect",
        default="",
        choices=("", PENDING_EFFECT_DISCARDED_ON_REPLACE, PENDING_EFFECT_PRESERVED),
        help=(
            "Redmine #15193: what the approval says becomes of the pending input. Only "
            f"`{PENDING_EFFECT_DISCARDED_ON_REPLACE}` authorizes this actuation, because a "
            "replacement cannot preserve the composer it replaces."
        ),
    )
    parser.add_argument(
        "--approved-lane-generation",
        dest="approved_lane_generation",
        default=-1,
        type=int,
        help="Redmine #15193: positive lifecycle incarnation observed by quarantine-inspect.",
    )
    parser.add_argument(
        "--approved-lifecycle-revision",
        dest="approved_lifecycle_revision",
        default=-1,
        type=int,
        help="Redmine #15193: positive shared lifecycle revision observed by quarantine-inspect.",
    )


__all__ = (
    "disposition_drift",
    "disposition_lifecycle_reason",
    "disposition_request_reason",
    "register_disposition_flags",
)
