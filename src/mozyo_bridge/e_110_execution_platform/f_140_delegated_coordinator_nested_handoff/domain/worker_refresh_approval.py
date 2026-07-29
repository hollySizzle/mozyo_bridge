"""Structured owner approval for the guarded live-worker refresh (Redmine #14661 j#92487 F1).

The R2 implementation verified the approval by asking whether a token appeared ANYWHERE in the
journal notes. Review j#92487 F1 reproduced four ways that admits an unapproved close: a
negation ("this action is NOT approved: <token>"), a quoted retry command, a log line, and a
generation prefix collision (a ``:g30`` approval contains ``:g3`` as a substring and therefore
approved a different round). Prose containment is not a decision.

This module is the hardened replacement, and it deliberately COPIES the shape the repo already
hardened for the same problem — :mod:`...composer_discard_approval`
(``verify_composer_discard_approval``) — rather than writing a second approval dialect:

* the exact approval journal must exist **uniquely** on the named issue;
* that journal must carry **exactly one** canonical structured
  ``[mozyo:workflow-event:...]`` marker of this surface's approval gate — prose mentions are
  not markers, and :func:`...redmine_journal_source.marker_fields_in_note` does not parse
  markers inside code fences, so quoted commands and log lines are structurally excluded;
* every expected field must match by **exact equality**, so a different round, worker, lane or
  action can never satisfy it — and a prefix collision is impossible because the generation is
  compared as a field value, not as a substring of a longer token;
* the marker must declare a **positive decision** (``decision=approved``) and the destructive
  **effect** it authorizes, so a marker recorded to decline the action does not admit it;
* the approval source must be ``direct_owner``: a guarded worker refresh is a *destructive
  operation*, which the governed preset's ``### Owner Close Approval Delegation`` lists as a
  carve-out from standing delegation.

**Identity travels as a digest, not as raw fields.** The marker grammar is ``:``-separated, and
both the action id (``refresh-worker:<lane>:<role>:<provider>:<name>:<locator>:r<rev>``) and the
locator (``w4B:p10``) contain ``:``. Embedding them raw would split into bogus fields or
truncate the marker. The precedent solves this the same way (``slot_digest`` / ``pin_digest``),
so the exact target travels as :func:`worker_refresh_approval_digest`.

**Known limitation, deliberately not faked (j#92493).** Review j#92487 F1 also asks for issuer
authority resolved from the durable record's author. :class:`RedmineJournalEntry` carries
``issue_id`` / ``journal_id`` / ``notes`` / ``created_on`` only — it does not carry an author —
so this module CANNOT establish authorship, and it does not pretend to. It verifies what the
note itself can prove and leaves author authority as a recorded residual.
"""

from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    RedmineJournalEntry,
    marker_fields_in_note,
)

#: This surface's approval gate token (the ``composer_discard_approval.APPROVAL_GATE``
#: precedent: an approval gate is named per destructive surface, so an approval for one
#: operation can never be read as an approval for another).
WORKER_REFRESH_APPROVAL_GATE = "worker_refresh_owner_approval"
#: The approval schema version. A marker of an unknown version is refused rather than
#: interpreted under today's field meanings.
APPROVAL_VERSION = "1"
#: The only admissible decision. A marker declaring anything else (or omitting it) is not an
#: approval — this is what makes a negated / declined record fail closed.
APPROVAL_DECISION = "approved"
#: The exact destructive effect authorized. An approval for some other effect on the same lane
#: does not fund this one.
APPROVAL_EFFECT = "worker_close_relaunch_resume"
#: A guarded worker refresh is a destructive operation, which the governed preset's
#: ``### Owner Close Approval Delegation`` names as a carve-out from standing delegation — so
#: only a direct owner approval qualifies.
APPROVAL_SOURCE = "direct_owner"


class WorkerRefreshApprovalError(ValueError):
    """The named journal is not a positive structured owner approval of THIS exact action."""


def worker_refresh_approval_digest(*, action_id: str, action_generation: object) -> str:
    """Canonical fingerprint of the exact action + generation the approval authorizes. (pure)

    Digested rather than embedded because the action id contains the marker grammar's ``:``
    separator (the ``pin_digest`` precedent). The components are newline-separated and neither
    can contain a newline, so the encoding is unambiguous; a domain tag pins the digest to this
    surface so a fingerprint can never be replayed from another one.
    """
    action = str(action_id or "").strip()
    try:
        generation = int(action_generation)
    except (TypeError, ValueError):
        raise WorkerRefreshApprovalError(
            "a worker refresh approval digest requires an integer action generation"
        ) from None
    if isinstance(action_generation, bool) or generation < 1:
        raise WorkerRefreshApprovalError(
            "a worker refresh approval digest requires a positive action generation"
        )
    if not action:
        raise WorkerRefreshApprovalError(
            "a worker refresh approval digest requires a non-empty action id"
        )
    encoded = "\n".join(
        (f"gate\t{WORKER_REFRESH_APPROVAL_GATE}", f"version\t{APPROVAL_VERSION}",
         f"action_id\t{action}", f"action_generation\t{generation}")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def expected_approval_fields(
    *, issue: str, lane: str, action_id: str, action_generation: object
) -> dict[str, str]:
    """The complete field map a valid approval marker must match by EXACT equality. (pure)"""
    issue_s = str(issue or "").strip()
    lane_s = str(lane or "").strip()
    if not issue_s or not lane_s:
        raise WorkerRefreshApprovalError(
            "a worker refresh approval requires a non-empty issue and lane"
        )
    return {
        "gate": WORKER_REFRESH_APPROVAL_GATE,
        "version": APPROVAL_VERSION,
        "approval_source": APPROVAL_SOURCE,
        "decision": APPROVAL_DECISION,
        "effect": APPROVAL_EFFECT,
        "issue": issue_s,
        "lane": lane_s,
        "action_digest": worker_refresh_approval_digest(
            action_id=action_id, action_generation=action_generation
        ),
    }


def render_worker_refresh_approval_marker(
    *, issue: str, lane: str, action_id: str, action_generation: object
) -> str:
    """The exact marker a positive owner approval must carry. (pure)

    Rendered by the read-only preflight so an operator can see precisely what to record — an
    approval contract nobody can produce is an approval contract nobody will use. Field order
    is fixed so the rendering is stable across runs.
    """
    fields = expected_approval_fields(
        issue=issue, lane=lane, action_id=action_id, action_generation=action_generation
    )
    ordered = (
        "gate", "version", "approval_source", "decision", "effect", "issue", "lane",
        "action_digest",
    )
    body = ":".join(f"{key}={fields[key]}" for key in ordered)
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


def verify_worker_refresh_approval(
    entries: Sequence[RedmineJournalEntry],
    *,
    issue: str,
    journal: str,
    lane: str,
    action_id: str,
    action_generation: object,
) -> Mapping[str, str]:
    """Verify ONE exact structured approval from a freshly fetched issue history. (pure)

    Returns the matched marker fields, or raises :class:`WorkerRefreshApprovalError`. Every
    refusal path is fail-closed — there is no partial acceptance and no "close enough".
    """
    journal_s = str(journal or "").strip()
    issue_s = str(issue or "").strip()
    if not journal_s:
        raise WorkerRefreshApprovalError("no approval journal was pinned")
    expected = expected_approval_fields(
        issue=issue_s, lane=lane, action_id=action_id, action_generation=action_generation
    )
    exact = [
        entry
        for entry in entries
        if str(getattr(entry, "issue_id", "") or "").strip() == issue_s
        and str(getattr(entry, "journal_id", "") or "").strip() == journal_s
    ]
    if len(exact) != 1:
        raise WorkerRefreshApprovalError(
            "the exact Redmine approval journal does not exist uniquely on the named issue"
        )
    candidates = [
        fields
        for channel, fields in marker_fields_in_note(
            str(getattr(exact[0], "notes", "") or "")
        )
        if channel == MARKER_CHANNEL_WORKFLOW_EVENT
        and str(fields.get("gate", "")).strip() == WORKER_REFRESH_APPROVAL_GATE
    ]
    if len(candidates) != 1:
        # Zero: the journal carries no structured approval (a prose mention, a quoted command,
        # or a log line is not a marker). Two or more: a record that declares this gate twice
        # cannot say which one is authoritative, so it authorizes nothing (the governed
        # preset's exactly-one rule for governed fields).
        raise WorkerRefreshApprovalError(
            "the exact journal does not contain one structured worker-refresh owner approval"
        )
    fields = candidates[0]
    wrong = [
        key for key, value in expected.items()
        if str(fields.get(key, "")).strip() != value
    ]
    if wrong:
        raise WorkerRefreshApprovalError(
            "the structured owner approval targets another operation, round or lane "
            f"(mismatched fields: {', '.join(sorted(wrong))})"
        )
    return dict(fields)


__all__ = (
    "WORKER_REFRESH_APPROVAL_GATE",
    "APPROVAL_VERSION",
    "APPROVAL_DECISION",
    "APPROVAL_EFFECT",
    "APPROVAL_SOURCE",
    "WorkerRefreshApprovalError",
    "worker_refresh_approval_digest",
    "expected_approval_fields",
    "render_worker_refresh_approval_marker",
    "verify_worker_refresh_approval",
)
