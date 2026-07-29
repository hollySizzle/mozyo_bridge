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

**Issuer authority (Redmine #14661 j#92494 scope, j#92601 F1).** A marker that NAMES an
approval source proves nothing — anyone who can write a note can write that field — so the
approval must also be attributable to an authority. An earlier revision compared the approval
journal's author to the ISSUE's author and called that owner authority; on the real record every
role (worker, gateway, coordinator) writes under one source-system user id, so that predicate is
satisfied by every journal on the issue and proves nothing. The governed preset says so directly:
source-system author id alone cannot identify the actor, and every issuer must be resolved to a
role through a durable authority ANCHOR.

So this module takes a :class:`...hibernate_evidence_authority.ResolvedIssuer` — the repo's
existing model for exactly this question — and requires it to be anchored. It does NOT invent an
owner-identity model of its own: an unresolved or unanchored issuer is a refusal, which keeps the
destructive path fail-closed while the representation of "direct owner" is settled as a design
ruling rather than guessed at here.
"""

from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    canonical_marker_bodies,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    RedmineJournalEntry,
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


#: The resolved writer roles that may issue a destructive owner approval for this surface.
#: EMPTY on purpose (#14661 j#92601 F1): the governed preset's approval carve-out requires a
#: ``direct_owner`` approval for a destructive operation, and this repo has no durable
#: role-resolution that yields an owner identity today — only coordinator / review-gateway /
#: lane-worker. Naming one of those here would re-create the defect this finding closed (any
#: same-account writer passing as the owner). Until the representation of "direct owner" is
#: settled by a design ruling, every ``--execute`` refuses. The preflight is unaffected.
APPROVAL_AUTHORITY_ROLES: frozenset[str] = frozenset()


class WorkerRefreshApprovalError(ValueError):
    """The named journal is not a positive structured owner approval of THIS exact action."""


def worker_refresh_approval_digest(
    *,
    action_id: str,
    action_generation: object,
    lane_revision: str,
    lane_generation: str,
    anchor_issue: str,
    resume_anchor_journal: str,
    resume_gate: str,
) -> str:
    """Canonical fingerprint of the WHOLE operation the approval authorizes. (pure)

    Review j#92533 F2 measured what an earlier digest over ``action_id + action_generation``
    alone actually authorized: with one unchanged marker, a run could be pointed at a
    different resume anchor, a different resume gate, a different lane lifecycle revision and
    a different lane generation — all four verified. The action id pins the participant
    (lane / role / provider / assigned name / locator / row revision), but the approval must
    cover what happens AFTER the close too: which durable anchor gets resumed, under which
    gate, at which lane lifecycle generation. An owner approving "close this worker" is not
    thereby approving "and resume some other anchor".

    Digested rather than embedded because several components contain the marker grammar's
    ``:`` separator (the ``pin_digest`` precedent). Components are newline-separated with
    explicit field tags and none may contain a newline, so the encoding is unambiguous; a
    domain tag pins the digest to this surface. Every component is REQUIRED — an approval that
    leaves part of the operation unnamed has not approved that part.
    """
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
    parts = {
        "action_id": str(action_id or "").strip(),
        "lane_revision": str(lane_revision or "").strip(),
        "lane_generation": str(lane_generation or "").strip(),
        "anchor_issue": str(anchor_issue or "").strip(),
        "resume_anchor_journal": str(resume_anchor_journal or "").strip(),
        "resume_gate": str(resume_gate or "").strip(),
    }
    missing = sorted(name for name, value in parts.items() if not value)
    if missing:
        raise WorkerRefreshApprovalError(
            "a worker refresh approval digest requires a non-empty "
            f"{' / '.join(missing)}"
        )
    if any("\n" in value for value in parts.values()):
        raise WorkerRefreshApprovalError(
            "a worker refresh approval digest component may not contain a newline"
        )
    encoded = "\n".join(
        [f"gate\t{WORKER_REFRESH_APPROVAL_GATE}", f"version\t{APPROVAL_VERSION}",
         f"action_generation\t{generation}"]
        + [f"{name}\t{parts[name]}" for name in sorted(parts)]
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: The COMPLETE field set a valid approval marker may carry — no more, no less. An unknown or
#: extra field is refused rather than ignored: a marker the canonical producer could not have
#: rendered is not a canonical marker, and silently tolerating unknown fields is how a future
#: meaningful field gets ignored by an old verifier (review j#92533 F2).
APPROVAL_FIELD_ORDER = (
    "gate", "version", "approval_source", "decision", "effect", "issue", "lane",
    "action_digest",
)


def parse_strict_approval_markers(notes: str) -> list[dict[str, str]]:
    """Every canonical approval marker in ``notes``, parsed STRICTLY. (pure)

    The shared :func:`...canonical_note_scan.canonical_marker_fields` is last-write-wins and
    silently drops malformed fragments, so ``decision=declined:decision=approved`` reads as an
    approval. A record that says two contradictory things has not decided anything, so this
    parser refuses it instead of picking a winner — the governed preset's exactly-one rule for
    governed fields, applied to the field this surface gates on.

    Bodies come from :func:`...canonical_note_scan.canonical_marker_bodies`, the span-preserving
    sibling of that scan. An earlier revision took only the COUNT from the shared scan and then
    re-located each marker with ``notes.find`` — which knows nothing about quote/fence exclusion,
    so a quoted marker earlier in the note was substituted for the canonical one and a canonical
    ``decision=declined`` verified as approved (review j#92601 F2). Location and exclusion are
    one authority; this module never re-derives either.
    """
    parsed: list[dict[str, str]] = []
    for _channel, body in canonical_marker_bodies(
        notes, channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
    ):
        components = body.split(":")
        if not any(
            component.strip() == f"gate={WORKER_REFRESH_APPROVAL_GATE}"
            for component in components
        ):
            continue  # some other surface's marker; not this gate's approval
        fields: dict[str, str] = {}
        order: list[str] = []
        for component in components:
            # ``partition`` makes a missing ``=`` indistinguishable from an empty value
            # (``"nonsense"`` -> key ``nonsense``, value ``""``), so the emptiness check covers
            # both; a separate branch for it was measured unkillable and is not kept.
            key, _, value = component.partition("=")
            key, value = key.strip(), value.strip()
            if not key or not value:
                raise WorkerRefreshApprovalError(
                    "the approval marker carries a malformed field "
                    "(not a non-empty key=value pair)"
                )
            if key in fields:
                raise WorkerRefreshApprovalError(
                    f"the approval marker declares {key!r} more than once; a record that "
                    "says two things has decided nothing"
                )
            fields[key] = value
            order.append(key)
        # The canonical producer emits exactly this sequence. Accepting a permutation would
        # accept a marker no producer in this repo can render (review j#92601 F2).
        if tuple(order) != APPROVAL_FIELD_ORDER:
            raise WorkerRefreshApprovalError(
                "the approval marker's field sequence is not the canonical one"
            )
        parsed.append(fields)
    return parsed


def expected_approval_fields(
    *,
    issue: str,
    lane: str,
    action_id: str,
    action_generation: object,
    lane_revision: str,
    lane_generation: str,
    anchor_issue: str,
    resume_anchor_journal: str,
    resume_gate: str,
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
            action_id=action_id, action_generation=action_generation,
            lane_revision=lane_revision, lane_generation=lane_generation,
            anchor_issue=anchor_issue, resume_anchor_journal=resume_anchor_journal,
            resume_gate=resume_gate,
        ),
    }


def render_worker_refresh_approval_marker(**operation: object) -> str:
    """The exact marker a positive owner approval must carry. (pure)

    Rendered by the read-only preflight so an operator can see precisely what to record — an
    approval contract nobody can produce is an approval contract nobody will use. Field order
    is :data:`APPROVAL_FIELD_ORDER` so the rendering is stable and the strict parser's
    exact-field-set requirement is satisfied by construction.
    """
    fields = expected_approval_fields(**operation)  # type: ignore[arg-type]
    body = ":".join(f"{key}={fields[key]}" for key in APPROVAL_FIELD_ORDER)
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


def verify_worker_refresh_approval(
    entries: Sequence[RedmineJournalEntry],
    *,
    journal: str,
    issuer: object,
    **operation: object,
) -> Mapping[str, str]:
    """Verify ONE exact structured approval from a freshly fetched issue history. (pure)

    Returns the matched marker fields, or raises :class:`WorkerRefreshApprovalError`. Every
    refusal path is fail-closed — there is no partial acceptance and no "close enough".

    ``issuer`` is a :class:`...hibernate_evidence_authority.ResolvedIssuer` the caller resolved
    from the DURABLE RECORD (#14661 j#92494) — never from a flag the actor requesting the
    destructive action supplies, which would be self-approval. It must be ANCHORED: a bare role
    token is an assertion, not a resolution, when one source-system account writes for several
    roles. An unresolved, unanchored, or non-approval-authority issuer refuses (j#92601 F1).
    """
    journal_s = str(journal or "").strip()
    issue_s = str(operation.get("issue") or "").strip()
    anchor_issue_s = str(operation.get("anchor_issue") or "").strip()
    if not journal_s:
        raise WorkerRefreshApprovalError("no approval journal was pinned")
    expected = expected_approval_fields(**operation)  # type: ignore[arg-type]
    exact = [
        entry
        for entry in entries
        if str(getattr(entry, "issue_id", "") or "").strip() == anchor_issue_s
        and str(getattr(entry, "journal_id", "") or "").strip() == journal_s
    ]
    if len(exact) != 1:
        raise WorkerRefreshApprovalError(
            "the exact Redmine approval journal does not exist uniquely on the named issue"
        )
    entry = exact[0]

    # Issuer authority, BEFORE any field is trusted: a marker that names an approval source
    # proves nothing about who wrote it (#14661 j#92601 F1).
    role = str(getattr(issuer, "role", "") or "").strip()
    anchored = bool(getattr(issuer, "is_anchored", False))
    if not role or role == ISSUER_UNKNOWN or not anchored:
        raise WorkerRefreshApprovalError(
            "the approval's issuer could not be resolved to an anchored authority; a role "
            "token without the durable record it was resolved from is an assertion, not a "
            "resolution"
        )
    if role not in APPROVAL_AUTHORITY_ROLES:
        raise WorkerRefreshApprovalError(
            "the approval journal's resolved writer does not hold owner-approval authority"
        )

    candidates = parse_strict_approval_markers(str(getattr(entry, "notes", "") or ""))
    if len(candidates) != 1:
        # Zero: the journal carries no structured approval (a prose mention, a quoted command,
        # or a log line is not a marker). Two or more: a record that declares this gate twice
        # cannot say which one is authoritative, so it authorizes nothing.
        raise WorkerRefreshApprovalError(
            "the exact journal does not contain one structured worker-refresh owner approval"
        )
    fields = candidates[0]
    # EXACT field set — an unknown or missing key is a marker the canonical producer could not
    # have rendered, so it is refused rather than partially honoured.
    if set(fields) != set(APPROVAL_FIELD_ORDER):
        raise WorkerRefreshApprovalError(
            "the approval marker's field set is not the canonical one"
        )
    wrong = [key for key, value in expected.items() if fields.get(key) != value]
    if wrong:
        raise WorkerRefreshApprovalError(
            "the structured owner approval targets another operation, round or lane "
            f"(mismatched fields: {', '.join(sorted(wrong))})"
        )
    if issue_s and str(fields.get("issue", "")).strip() != issue_s:
        raise WorkerRefreshApprovalError("the approval marker names another issue")
    return dict(fields)


__all__ = (
    "APPROVAL_AUTHORITY_ROLES",
    "APPROVAL_FIELD_ORDER",
    "WORKER_REFRESH_APPROVAL_GATE",
    "APPROVAL_VERSION",
    "APPROVAL_DECISION",
    "APPROVAL_EFFECT",
    "APPROVAL_SOURCE",
    "WorkerRefreshApprovalError",
    "parse_strict_approval_markers",
    "worker_refresh_approval_digest",
    "expected_approval_fields",
    "render_worker_refresh_approval_marker",
    "verify_worker_refresh_approval",
)
