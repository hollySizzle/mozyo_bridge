"""Coordinator dispatch-authorization token (Redmine #13489 increment 2).

Increment 2 re-enables the sublane gateway's one-step worker dispatch, but **only** when a
coordinator has recorded a durable, structured *dispatch authorization* on the lane's Redmine
issue — the design contract's requirement 1 (``vibes/docs/logics/workflow-step-command-design.md``
``### Increment 2 dispatch 再有効化 contract``; design answer j#74922 / proposal j#74996 /
approved review j#75001). Worker liveness + a verified anchor (increment 1) are *identity /
readiness* facts and never authorize a dispatch by themselves.

This module is the **pure** authorization vocabulary + parser. A dispatch authorization is a
dedicated ``[mozyo:dispatch-authorization:...]`` marker channel — deliberately distinct from
the ``[mozyo:handoff:...]`` ``kind=implementation_request`` token (which authorizes *human/agent
implementation work*, NOT a product-runtime worker auto-dispatch; see j#75006 "Important
distinction"). Keeping it a separate channel means an ordinary ``implementation_request``
handoff can never be mis-read as a machine dispatch authority, and the absence of this marker in
production is exactly why auto-dispatch stays disabled until a coordinator emits one.

Authority rules (fail-closed):

- prose, pane notification, and a delivery ACK are **not** an authorization — only the
  structured marker read from source-of-truth Redmine (the application adapter supplies the
  live journal entries; this module never reads anything).
- an authorization is *valid* only when every required field is present and the fixed
  authority fields hold exactly: ``action=dispatch_worker``, ``conclusion=authorized``,
  ``target_role=implementation_worker``, ``authorized_by_role=coordinator``.
- the caller correlates the authorization's ``workspace_id`` / ``lane_id`` / ``issue`` /
  ``target_assigned_name`` to the action-time resolved lane + target (identity drift ->
  fail closed), and checks it has not been superseded by a later durable gate
  (:data:`SUPERSEDING_GATES`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    strict_marker_body_fields,
)

# The dedicated authorization marker channel (distinct from ``handoff`` / ``workflow-event``).
DISPATCH_AUTHORIZATION_CHANNEL = "dispatch-authorization"

# The fixed authority field values an authorization must carry exactly (design requirement 1).
ACTION_DISPATCH_WORKER = "dispatch_worker"
CONCLUSION_AUTHORIZED = "authorized"
TARGET_ROLE_WORKER = "implementation_worker"
AUTHORIZER_COORDINATOR = "coordinator"

# Why a structurally perfect authorization still cannot be counted (Redmine #14539 review
# j#92227). Both are properties of the RECORD AROUND the marker, not of the marker's own body,
# which is why they are carried separately from the required-field check.
#: The note carries another dispatch-authorization marker the canonical producer could not
#: render. Returning the readable siblings would make a note containing one clean and one forged
#: authorization read exactly like a clean note.
AMBIGUITY_UNREADABLE_SIBLING = "unreadable_sibling"
#: One journal carries more than one valid authorization for the same lane + issue. Two markers
#: at ONE journal is an ambiguity to surface, never a supersession to resolve by note order.
AMBIGUITY_DUPLICATE_AT_JOURNAL = "duplicate_authorization_at_journal"
AUTHORIZATION_AMBIGUITIES: frozenset[str] = frozenset(
    {AMBIGUITY_UNREADABLE_SIBLING, AMBIGUITY_DUPLICATE_AT_JOURNAL}
)

# The required structured fields. ``journal`` is the entry's own durable id (supplied by the
# reader, never trusted from the note body) so it is not part of this set.
_REQUIRED_FIELDS = (
    "action_id",
    "source_gate",
    "issue",
    "workspace_id",
    "lane_id",
    "target_role",
    "target_assigned_name",
    "action",
    "conclusion",
    "authorized_by_role",
)

#: A later durable gate on the issue that supersedes a standing dispatch authorization: once
#: the work has advanced to done / review / close / blocked, a fresh dispatch would be a
#: duplicate or a wrong-phase action (design requirement 1: "latest durable state が
#: implementation_done/review/close/blocked へ進行済なら monitor/no-op"). ``review`` is the
#: intake alias ``build_marker`` maps ``review_request`` / ``review_result`` onto.
SUPERSEDING_GATES = frozenset(
    {"implementation_done", "review", "review_request", "review_result", "close", "closed", "blocked"}
)

#: ``[mozyo:dispatch-authorization:<key=value:...>]`` — the same grammar the gate-marker
#: channels use, scanned here for the dedicated authorization channel only. The BODY is parsed by
#: the shared strict reader (see :func:`_parse_fields`); only the token scan is local, because this
#: channel is deliberately outside the gate-marker channel set.
_MARKER_RE = re.compile(r"\[mozyo:(?P<channel>[a-z0-9_-]+):(?P<body>[^\]]*)\]")


def _parse_fields(body: str) -> "dict[str, str] | None":
    """Parse a marker body strictly, or ``None`` when it is not renderable (pure).

    This channel's authorization reaches an ACTUAL worker dispatch, so it is read on the same
    terms as every other authority marker (Redmine #14539 review j#92106 finding 4): the shared
    strict reader over uncollapsed components. It used to be a verbatim copy of the lenient fold —
    last-write-wins on a repeated key, whitespace stripped off every key and value — so
    ``action = dispatch_worker``, ``action=deny:action=dispatch_worker`` and
    ``authorized_by_role=worker:authorized_by_role=coordinator`` all authorized a dispatch. Having
    its own parser is precisely why the shared-symbol sweep never saw it.

    This channel has a CLOSED vocabulary, so it reads through the closed-vocabulary helper
    (Redmine #14539 review j#92327 finding 1). ``strict_marker_fields`` alone answers "could a
    producer render this body", which is the right question for an open-ended gate marker and
    too weak here: an extra field, a key repeated with the same value, and a blank value are all
    bodies THIS producer cannot render, and every one of them reached ``authorize``. R24 added
    ``strict_marker_body_fields`` for exactly this and R25 edited this module without routing it
    here. A refusal makes the marker unreadable, which the note-poison below then propagates —
    so an incomplete sibling stops being something to skip past.
    """
    return strict_marker_body_fields(body, expected=frozenset(_REQUIRED_FIELDS))


@dataclass(frozen=True)
class DispatchAuthorization:
    """One coordinator dispatch authorization read from a Redmine journal note (pure value).

    ``journal`` is the durable id of the journal entry the marker was recorded in (the reader
    supplies it from the entry, never the note body). Every other field is the verbatim marker
    field. :meth:`valid` is the fail-closed gate: all required fields present and the fixed
    authority fields exactly right.
    """

    action_id: str
    source_gate: str
    issue: str
    workspace_id: str
    lane_id: str
    target_role: str
    target_assigned_name: str
    action: str
    conclusion: str
    authorized_by_role: str
    journal: str = ""
    #: Why this authorization cannot be counted even though its OWN fields may be perfect —
    #: one of :data:`AUTHORIZATION_AMBIGUITIES`, or ``""`` when nothing about its context is
    #: ambiguous. A non-empty value makes :attr:`valid` False while leaving the identity fields
    #: intact, so the lane correlator still SELECTS it and the decider surfaces a blocked
    #: dispatch rather than silently monitoring (Redmine #14539 review j#92227 findings 1 / 2).
    ambiguity: str = ""

    @property
    def valid(self) -> bool:
        """True only when every required field is present and the authority fields hold exactly."""
        if self.ambiguity:
            # Its own body may be flawless; the note or journal it lives in cannot be read as
            # naming exactly one authorization, and a dispatch is not authorized by "one of the
            # markers here is fine".
            return False
        if not all((getattr(self, name) or "").strip() for name in _REQUIRED_FIELDS):
            return False
        return (
            self.action == ACTION_DISPATCH_WORKER
            and self.conclusion == CONCLUSION_AUTHORIZED
            and self.target_role == TARGET_ROLE_WORKER
            and self.authorized_by_role == AUTHORIZER_COORDINATOR
        )

    def matches_lane(self, *, workspace_id: str, lane_id: str, issue: str) -> bool:
        """True when this authorization is for the given action-time lane + issue (identity gate)."""
        return (
            self.workspace_id == (workspace_id or "").strip()
            and self.lane_id == (lane_id or "").strip()
            and self.issue == (issue or "").strip()
        )

    def matches_target(self, target_assigned_name: str) -> bool:
        """True when this authorization names the action-time resolved target (drift gate)."""
        return self.target_assigned_name == (target_assigned_name or "").strip()


def _authorization_from_fields(
    fields: Mapping[str, str], journal: str, *, ambiguity: str = ""
) -> DispatchAuthorization:
    """Build a :class:`DispatchAuthorization` from parsed marker fields (pure)."""
    return DispatchAuthorization(
        ambiguity=ambiguity,
        action_id=(fields.get("action_id") or "").strip(),
        source_gate=(fields.get("source_gate") or "").strip(),
        issue=(fields.get("issue") or "").strip(),
        workspace_id=(fields.get("workspace_id") or "").strip(),
        lane_id=(fields.get("lane_id") or "").strip(),
        target_role=(fields.get("target_role") or "").strip(),
        target_assigned_name=(fields.get("target_assigned_name") or "").strip(),
        action=(fields.get("action") or "").strip(),
        conclusion=(fields.get("conclusion") or "").strip(),
        authorized_by_role=(fields.get("authorized_by_role") or "").strip(),
        journal=(journal or "").strip(),
    )


def parse_dispatch_authorizations(
    entries: Iterable["object"],
) -> tuple[DispatchAuthorization, ...]:
    """Every dispatch-authorization marker across ordered journal entries (pure; never prose).

    ``entries`` are duck-typed :class:`...redmine_journal_source.RedmineJournalEntry` (they
    expose ``journal_id`` and ``notes``). Scans each note for
    ``[mozyo:dispatch-authorization:...]`` tokens and yields one authorization per token in
    note order (so a later journal's authorization sorts after an earlier one). A note with no
    such token contributes nothing; invalid / partial markers are still parsed (the caller's
    :meth:`DispatchAuthorization.valid` gate rejects them) so a malformed authorization can be
    diagnosed rather than silently vanish.

    An unrenderable marker POISONS ITS WHOLE NOTE for this channel (Redmine #14539 review
    j#92227 finding 1). Emitting it as an all-blank invalid authorization was diagnosable and
    ineffective: with no identity fields it matched no lane, so the lane correlator dropped it
    and dispatched on its clean sibling — a note carrying one canonical and one forged
    authorization decided exactly like a clean note. The spine rule this US wrote for gate
    markers is not gate-specific: "same-gate の数えられない sibling が同一 note にあれば、その gate
    の読取は note 全体を fail-closed にする". Here the channel IS the gate, so the poison is
    note-and-channel scoped: every authorization from that note keeps its identity (so it is
    still SELECTED and reported) and carries :data:`AMBIGUITY_UNREADABLE_SIBLING`, making it
    invalid for every consumer that gates on ``valid`` — worker send, disposition write,
    gateway intake, and retire admission alike.
    """
    out: list[DispatchAuthorization] = []
    for entry in entries:
        notes = getattr(entry, "notes", "") or ""
        journal = str(getattr(entry, "journal_id", "") or "").strip()
        if not notes:
            continue
        parsed: list[Mapping[str, str] | None] = [
            _parse_fields(match.group("body"))
            for match in _MARKER_RE.finditer(notes)
            if match.group("channel") == DISPATCH_AUTHORIZATION_CHANNEL
        ]
        poisoned = any(fields is None for fields in parsed)
        for fields in parsed:
            # The unrenderable marker itself is still emitted (blank fields) so a malformed
            # record stays visible, and its readable siblings are emitted WITH the poison.
            out.append(
                _authorization_from_fields(
                    fields if fields is not None else {},
                    journal,
                    ambiguity=AMBIGUITY_UNREADABLE_SIBLING if poisoned else "",
                )
            )
    return tuple(out)


def build_dispatch_authorization_marker(
    *,
    action_id: str,
    source_gate: str,
    issue: str,
    workspace_id: str,
    lane_id: str,
    target_assigned_name: str,
    target_role: str = TARGET_ROLE_WORKER,
    action: str = ACTION_DISPATCH_WORKER,
    conclusion: str = CONCLUSION_AUTHORIZED,
    authorized_by_role: str = AUTHORIZER_COORDINATOR,
) -> str:
    """The canonical ``[mozyo:dispatch-authorization:...]`` marker string (pure).

    The single builder for the token so the coordinator's authorization tooling and the tests
    emit exactly the vocabulary :func:`parse_dispatch_authorizations` reads back. The fixed
    authority fields default to the only values :meth:`DispatchAuthorization.valid` accepts.
    """
    fields = [
        f"action_id={action_id}",
        f"source_gate={source_gate}",
        f"issue={issue}",
        f"workspace_id={workspace_id}",
        f"lane_id={lane_id}",
        f"target_role={target_role}",
        f"target_assigned_name={target_assigned_name}",
        f"action={action}",
        f"conclusion={conclusion}",
        f"authorized_by_role={authorized_by_role}",
    ]
    return "[mozyo:" + DISPATCH_AUTHORIZATION_CHANNEL + ":" + ":".join(fields) + "]"


__all__ = (
    "DISPATCH_AUTHORIZATION_CHANNEL",
    "ACTION_DISPATCH_WORKER",
    "CONCLUSION_AUTHORIZED",
    "TARGET_ROLE_WORKER",
    "AUTHORIZER_COORDINATOR",
    "SUPERSEDING_GATES",
    "DispatchAuthorization",
    "parse_dispatch_authorizations",
    "build_dispatch_authorization_marker",
)
