"""The ONE signature classifier for the hibernated worktree-binding repair (#14475).

Review j#88526 F2 is the third round of the same defect: the command's read-only preflight and
the store's CAS decided the *same* question with *separately written* predicates, so they
disagreed — first because the preflight had no axis at all, then because it projected only
some axes, then because it compared **normalized** values where the CAS compares **raw** ones.
A row persisted as ``issue_id=' 14475 '`` therefore reported "``--execute`` would record" and
was then refused ``repair_cas_refused``: a dry-run green an owner could approve from.

Writing the missing axes down a fourth time would be the same bet. This module removes the bet:
both callers classify through :func:`classify_repair_signature`, so the preflight cannot report
a verdict the CAS would not reach.

The comparisons are **raw** because the CAS's are: a persisted value carrying padding is
malformed state, and refusing it is the safe answer both surfaces must give. Normalizing here
would silently widen the store's contract instead of describing it.

Pure: no store, no connection, no I/O. It reads a record-shaped object (anything exposing the
lifecycle row's attributes) and returns a closed token.
"""

from __future__ import annotations

from typing import Any

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    DISPOSITION_HIBERNATED,
    RELEASE_RELEASED,
    decode_declared_slots,
    encode_declared_slots,
    norm,
    replacement_settled,
    validate_declared_slots,
    stored_binding_kind_is,
)

# -- the closed signature vocabulary -------------------------------------------

#: Every axis holds; the repair may proceed (subject to the caller's own evidence checks).
SIGNATURE_OK = "ok"

#: The row is not ``hibernated`` (an ``active`` lane binds through the #13809 backfill; a
#: ``superseded`` / ``retired`` row is terminal).
SIGNATURE_NOT_HIBERNATED = "lane_not_hibernated"
#: ``binding_kind`` is not ``issue`` — an axis the CAS checks independently of the scope.
SIGNATURE_BINDING_KIND = "lane_binding_kind_is_not_issue"
#: The row's ``issue_id`` is not RAW-equal to the caller's issue (a padded / different value).
SIGNATURE_WRONG_ISSUE = "lane_owns_a_different_issue"
#: The row carries a project scope (RAW non-empty, so whitespace counts).
SIGNATURE_PROJECT_SCOPE = "lane_owns_a_project_scope"
#: ``process_release`` is not RAW-equal to ``released``.
SIGNATURE_RELEASE_NOT_SETTLED = "lane_process_release_not_settled"
#: A receiver replacement is in flight.
SIGNATURE_REPLACEMENT_IN_FLIGHT = "lane_replacement_in_flight"
#: The row carries no declared pins (the #13879 / #13842 shape those surfaces own).
SIGNATURE_MISSING_PINS = "hibernated_record_missing_pins"
#: The pins do not survive the model's own validator (duplicate / malformed slots).
SIGNATURE_INVALID_PINS = "declared_pins_fail_validation"
#: The pins are valid but the stored bytes are not their canonical encoding — so the CAS's
#: ``declared_slots == encode(validate(...))`` comparison can never match them.
SIGNATURE_PINS_NOT_CANONICAL = "declared_pins_are_not_canonically_encoded"

SIGNATURE_REASONS = frozenset(
    {
        SIGNATURE_OK,
        SIGNATURE_NOT_HIBERNATED,
        SIGNATURE_BINDING_KIND,
        SIGNATURE_WRONG_ISSUE,
        SIGNATURE_PROJECT_SCOPE,
        SIGNATURE_RELEASE_NOT_SETTLED,
        SIGNATURE_REPLACEMENT_IN_FLIGHT,
        SIGNATURE_MISSING_PINS,
        SIGNATURE_INVALID_PINS,
        SIGNATURE_PINS_NOT_CANONICAL,
    }
)

#: Every token that refuses the repair (everything but :data:`SIGNATURE_OK`).
SIGNATURE_BLOCKERS = frozenset(SIGNATURE_REASONS - {SIGNATURE_OK})


def canonical_declared_slots(raw: str) -> str:
    """The canonical encoding of a stored slot snapshot, or ``""``. (pure, fail-closed)

    ``""`` whenever the snapshot is absent, undecodable, or does not validate — the caller
    then reports the matching blocker rather than treating an unusable snapshot as canonical.
    """
    if not raw:
        return ""
    try:
        return encode_declared_slots(validate_declared_slots(decode_declared_slots(raw)))
    except Exception:  # noqa: BLE001 - an unusable snapshot has no canonical form
        return ""


def classify_repair_signature(record: Any, *, issue_id: str) -> str:
    """Does this row match the repair's exact signature? (pure, fail-closed, ordered)

    Returns :data:`SIGNATURE_OK` or the first failing axis, in the same order the CAS checks
    them so the two surfaces name the same reason for the same row. ``record`` is any object
    exposing the lifecycle row's attributes; a missing attribute reads as empty and therefore
    blocks.

    Deliberately RAW-comparing (review j#88526 F2): the CAS compares the persisted bytes, so a
    padded ``issue_id`` / ``process_release`` / scope, or a non-canonically encoded pin
    snapshot, is refused by BOTH surfaces instead of passing a normalizing preflight and
    failing the exact CAS.

    It does NOT decide the revision / generation CAS (the caller's own pinned evidence), the
    already-bound axes, or the worktree-evidence axes: those are not signature facts about the
    row alone.
    """
    if getattr(record, "lane_disposition", "") != DISPOSITION_HIBERNATED:
        return SIGNATURE_NOT_HIBERNATED
    if not stored_binding_kind_is(getattr(record, "binding_kind", ""), BINDING_KIND_ISSUE):
        return SIGNATURE_BINDING_KIND
    if getattr(record, "issue_id", "") != norm(issue_id):
        return SIGNATURE_WRONG_ISSUE
    if getattr(record, "project_scope", ""):
        return SIGNATURE_PROJECT_SCOPE
    raw_slots = getattr(record, "declared_slots", "")
    if not raw_slots:
        return SIGNATURE_MISSING_PINS
    canonical = canonical_declared_slots(raw_slots)
    if not canonical:
        return SIGNATURE_INVALID_PINS
    if raw_slots != canonical:
        return SIGNATURE_PINS_NOT_CANONICAL
    if getattr(record, "process_release", "") != RELEASE_RELEASED:
        return SIGNATURE_RELEASE_NOT_SETTLED
    if not replacement_settled(getattr(record, "replacement_state", "")):
        return SIGNATURE_REPLACEMENT_IN_FLIGHT
    return SIGNATURE_OK


def signature_matches(record: Any, *, issue_id: str) -> bool:
    """The boolean projection of :func:`classify_repair_signature`. (pure, fail-closed)"""
    return classify_repair_signature(record, issue_id=issue_id) == SIGNATURE_OK


__all__ = (
    "SIGNATURE_OK",
    "SIGNATURE_NOT_HIBERNATED",
    "SIGNATURE_BINDING_KIND",
    "SIGNATURE_WRONG_ISSUE",
    "SIGNATURE_PROJECT_SCOPE",
    "SIGNATURE_RELEASE_NOT_SETTLED",
    "SIGNATURE_REPLACEMENT_IN_FLIGHT",
    "SIGNATURE_MISSING_PINS",
    "SIGNATURE_INVALID_PINS",
    "SIGNATURE_PINS_NOT_CANONICAL",
    "SIGNATURE_REASONS",
    "SIGNATURE_BLOCKERS",
    "canonical_declared_slots",
    "classify_repair_signature",
    "signature_matches",
)
