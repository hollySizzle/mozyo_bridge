"""Review-gate marker-field builders for ``workflow callbacks --emit-gate``.

Extracted from ``cli_workflow_callbacks`` to keep that module under the module-health boundary
(Redmine #14219 T2b added the evidence envelope). Builds and fail-closed-validates the Review
Generation Marker Contract v2 fields (#13974) plus the optional hibernate-evidence lane envelope
(#14219 T2b) that ``render_workflow_event_marker`` emits.
"""

from __future__ import annotations

import argparse
from typing import Optional

from ..domain.hibernate_evidence_envelope import contains_marker_separator
from ..domain.marker_value_contract import (
    is_canonical_positive_decimal,
    is_exact_str,
    is_journal_id,
)

_REVIEW_REQUEST_GATE = "review_request"
_REVIEW_RESULT_GATE = "review_result"


def review_gate_marker_fields(args: argparse.Namespace, gate: str) -> "tuple[dict, Optional[str]]":
    """Build + fail-closed-validate the v2 marker fields for a review gate (#13974 j#81487 F2).

    Returns ``(marker_fields, refusal)``. For a ``review_request`` gate ``--target-head`` is required
    and must be a full commit head; for a ``review_result`` gate ``--target-head`` (full head) AND
    ``--review-request-journal`` are required. A missing / malformed input yields a fixed refusal token
    (nothing is written — the producer never emits a marker the fence would reject). A non-review gate
    carries no v2 fields (``({}, None)``).

    Every value is judged RAW, for the reason review j#93646 finding 1 gave about this function's
    envelope half (Redmine #14694): a CLI producer that trims before validating is the surface
    that turns an operator's ``--target-head " <sha> "`` into the canonical authority field, and
    fixing three of this function's five values would have left the finding half-closed in the
    very function it names. ``is_full_commit_head`` strips for the READ side, so the padding is
    caught here, before it is consulted. The refusal vocabulary is unchanged for the head; the
    journal id gains the ``malformed`` token it never had (it only had ``missing``).
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
    head = getattr(args, "target_head", None) or ""
    if not head:
        return {}, "review_marker_missing_target_head"
    # `isinstance` FIRST, for the reason review j#93882 gave about both halves of this contract:
    # `contains_marker_separator` and `is_full_commit_head` both judge `str(value)`, so a non-`str`
    # whose `__str__` looks like a head passed here and was handed on AS THE OBJECT — the domain
    # producer then raised, turning this boundary's typed refusal into a traceback. Self-detected
    # while sweeping F1's family; the CLI must answer with a token whatever it is given.
    if not is_exact_str(head) or contains_marker_separator(head) or not is_full_commit_head(head):
        return {}, "review_marker_malformed_target_head"
    fields: dict = {"target_head": head}
    if gate == _REVIEW_RESULT_GATE:
        req = getattr(args, "review_request_journal", None) or ""
        if not req:
            return {}, "review_marker_missing_review_request_journal"
        # The contract's `req` is a review_request JOURNAL ID, so the shape is checked, not just
        # the characters (review j#93818 finding 2): `abc`, `93802=shadow`, `-5`, `0` and `1.5`
        # all passed the separator check and reached the writer path. The domain producer raises
        # on the same question; a CLI owes its caller the typed refusal instead.
        if not is_journal_id(req):
            return {}, "review_marker_malformed_review_request_journal"
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

    An identity carrying a marker separator is refused HERE with a typed token as well as by the
    renderer (checkpoint j#86443 R2-F3): the renderer raises, which is right for a producer bug, but
    a CLI argument is operator input and deserves the same fixed refusal vocabulary as the other
    malformed inputs. That is why the membership question is asked through the surface's shared
    :func:`contains_marker_separator` rather than by iterating the punctuation tuple (Redmine
    #14694): a ``\\n``-bearing ``--evidence-lane`` is refused by the renderer, so listing fewer
    characters here would turn an operator typo into a traceback instead of a typed refusal.

    The values are judged RAW (review j#93646 finding 1). This used to ``strip()`` them first,
    which made THIS the surface that turned ``--evidence-workspace " ws "`` into the canonical
    authority field ``ws`` — the exact defect #14694 exists to close, surviving in the one producer
    an operator actually calls. Being a CLI does not license normalizing: what it owes its caller
    is the fixed refusal vocabulary, and it still gives one, because padding IS a marker separator
    (``evidence_envelope_malformed_identity``) and a padded generation is not ``isdigit()``
    (``evidence_envelope_malformed_generation``). Nothing here becomes a traceback.
    """
    ws = getattr(args, "evidence_workspace", None) or ""
    lane = getattr(args, "evidence_lane", None) or ""
    gen_raw = getattr(args, "evidence_lane_generation", None) or ""
    if not ws and not lane and not gen_raw:
        return {}, None
    if not ws or not lane or not gen_raw:
        return {}, "evidence_envelope_incomplete"
    # `isinstance` before the character test, same reason as the head above: the identities are
    # handed to the strict renderer AS SUPPLIED, so a non-`str` that merely stringifies cleanly
    # reached it and raised, instead of being refused here with a token.
    if not all(is_exact_str(value) for value in (ws, lane)):
        return {}, "evidence_envelope_malformed_identity"
    if any(contains_marker_separator(value) for value in (ws, lane)):
        return {}, "evidence_envelope_malformed_identity"
    # The generation asks the SAME canonical-decimal question as the review contract's `req`
    # (review j#94038 blocker 1). `isdigit()` + `int()` was not that question: it accepted `"01"`
    # and `"٣"` and rewrote them into a generation nobody named, and it RAISED on a 4301-digit
    # input — through the very line whose docstring promised nothing here becomes a traceback.
    if not is_canonical_positive_decimal(gen_raw):
        return {}, "evidence_envelope_malformed_generation"
    return {
        "evidence_workspace": ws,
        "evidence_lane": lane,
        "evidence_lane_generation": int(gen_raw),
    }, None
