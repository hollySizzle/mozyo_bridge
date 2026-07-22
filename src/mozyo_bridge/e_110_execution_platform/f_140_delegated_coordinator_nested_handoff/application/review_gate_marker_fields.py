"""Review-gate marker-field builders for ``workflow callbacks --emit-gate``.

Extracted from ``cli_workflow_callbacks`` to keep that module under the module-health boundary
(Redmine #14219 T2b added the evidence envelope). Builds and fail-closed-validates the Review
Generation Marker Contract v2 fields (#13974) plus the optional hibernate-evidence lane envelope
(#14219 T2b) that ``render_workflow_event_marker`` emits.
"""

from __future__ import annotations

import argparse
from typing import Optional

_REVIEW_REQUEST_GATE = "review_request"
_REVIEW_RESULT_GATE = "review_result"


def review_gate_marker_fields(args: argparse.Namespace, gate: str) -> "tuple[dict, Optional[str]]":
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
    # Redmine #14219 T2b: the optional hibernate-evidence lane envelope. All-or-nothing — a
    # partially-supplied envelope is a fixed refusal (never a half-bound marker); a fully-absent one
    # leaves the marker as a legacy review gate (valid for glance/callback, not auto-hibernate
    # evidence). ``lane_generation`` must be a positive integer.
    envelope, refusal = lane_envelope_marker_fields(args)
    if refusal is not None:
        return {}, refusal
    fields.update(envelope)
    return fields, None


def lane_envelope_marker_fields(args: argparse.Namespace) -> "tuple[dict, Optional[str]]":
    """Build the optional ``workspace``/``lane``/``lane_generation`` evidence envelope, fail-closed.

    Returns ``({}, None)`` when none of the three is supplied (a legacy marker). When ANY is supplied,
    all three are required and the generation must be a positive integer, else a fixed refusal token.
    """
    ws = (getattr(args, "evidence_workspace", None) or "").strip()
    lane = (getattr(args, "evidence_lane", None) or "").strip()
    gen_raw = (getattr(args, "evidence_lane_generation", None) or "").strip()
    if not ws and not lane and not gen_raw:
        return {}, None
    if not ws or not lane or not gen_raw:
        return {}, "evidence_envelope_incomplete"
    if not gen_raw.isdigit() or int(gen_raw) <= 0:
        return {}, "evidence_envelope_malformed_generation"
    return {
        "evidence_workspace": ws,
        "evidence_lane": lane,
        "evidence_lane_generation": int(gen_raw),
    }, None
