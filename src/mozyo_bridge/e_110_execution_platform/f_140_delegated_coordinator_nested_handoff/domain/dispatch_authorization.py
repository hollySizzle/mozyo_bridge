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

# The dedicated authorization marker channel (distinct from ``handoff`` / ``workflow-event``).
DISPATCH_AUTHORIZATION_CHANNEL = "dispatch-authorization"

# The fixed authority field values an authorization must carry exactly (design requirement 1).
ACTION_DISPATCH_WORKER = "dispatch_worker"
CONCLUSION_AUTHORIZED = "authorized"
TARGET_ROLE_WORKER = "implementation_worker"
AUTHORIZER_COORDINATOR = "coordinator"

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
#: channels use, scanned here for the dedicated authorization channel only.
_MARKER_RE = re.compile(r"\[mozyo:(?P<channel>[a-z0-9_-]+):(?P<body>[^\]]*)\]")


def _parse_fields(body: str) -> dict[str, str]:
    """Parse a ``key=value:key=value`` marker body into a dict (pure; last write wins)."""
    fields: dict[str, str] = {}
    for token in body.split(":"):
        token = token.strip()
        if not token:
            continue
        key, eq, value = token.partition("=")
        if not eq:
            continue
        fields[key.strip()] = value.strip()
    return fields


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

    @property
    def valid(self) -> bool:
        """True only when every required field is present and the authority fields hold exactly."""
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


def _authorization_from_fields(fields: Mapping[str, str], journal: str) -> DispatchAuthorization:
    """Build a :class:`DispatchAuthorization` from parsed marker fields (pure)."""
    return DispatchAuthorization(
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
    """
    out: list[DispatchAuthorization] = []
    for entry in entries:
        notes = getattr(entry, "notes", "") or ""
        journal = str(getattr(entry, "journal_id", "") or "").strip()
        if not notes:
            continue
        for match in _MARKER_RE.finditer(notes):
            if match.group("channel") != DISPATCH_AUTHORIZATION_CHANNEL:
                continue
            fields = _parse_fields(match.group("body"))
            out.append(_authorization_from_fields(fields, journal))
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
