"""Core-facing ticket adapter record seam (Redmine #12034).

This is the first concrete cut of the built-in ticket adapter boundary from
``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine #12001). It
defines the *small internal record* that every ticket provider must normalize
into, plus the core-owned vocabulary and decisions that providers are
forbidden from inventing.

Boundary, restated from the design doc so it stays enforced in code:

- **Core owns** the durable-anchor vocabulary, the workflow-gate names, the
  review / close / owner-approval boundary, and the secret / private-data
  rules. None of that is delegated to a provider.
- **Providers own** API calls, issue / journal / comment fetch, status-update
  mechanics, project / version lookup, and provider-specific URL formatting.
  A provider may *expose* a ``close_issue`` mechanic, but **core** decides
  whether close approval has actually been satisfied — see
  :func:`owner_approval`.

These records are intentionally pure dataclasses with no network, no I/O, and
no approval logic. The built-in Redmine provider that fills them lives in
``mozyo_bridge.infrastructure.redmine_ticket_provider``; this module never
imports a provider, so the dependency only ever points provider -> core.

Non-goals (kept explicit so the seam does not drift into a plugin API):

- no third-party / arbitrary-code provider loading;
- no public ABI or long-term compatibility promise for these record shapes;
- no provider-defined workflow truth, gate names, or approval semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, runtime_checkable

from mozyo_bridge.domain.handoff import KIND_LABELS

# The workflow-gate vocabulary is core-owned. It is the durable-record subset
# of the handoff ``KIND_LABELS`` (the kinds that move a unit through review /
# completion), sourced from ``handoff`` so the two never diverge. A provider
# observes journals; it never decides which of these a journal satisfies, and
# it cannot add new gate names.
WORKFLOW_GATE_KINDS: frozenset[str] = frozenset(
    {"implementation_done", "review_request", "review_result"}
) & KIND_LABELS

# Owner close approval is deliberately *not* in the gate vocabulary above:
# reaching a gate is a provider-observable journal fact, but "close approval is
# satisfied" is a core decision (design doc: provider may expose close_issue,
# core decides whether close approval has been satisfied).


class TicketRecordError(ValueError):
    """A provider produced a record that violates the core contract."""


@dataclass(frozen=True)
class IssueRef:
    """A normalized pointer to a ticket issue.

    ``provider`` names the built-in adapter that produced this record (e.g.
    ``"redmine"``). ``id`` is the provider-native issue id as a string. The
    remaining fields are best-effort context; subjects are intentionally
    absent — they can carry personal or confidential summaries and never
    belong on a core-facing record.
    """

    provider: str
    id: str
    status: Optional[str] = None
    updated_on: Optional[str] = None
    url: Optional[str] = None


@dataclass(frozen=True)
class JournalRef:
    """A normalized pointer to a single journal/update entry on an issue."""

    provider: str
    issue_id: str
    id: str
    created_on: Optional[str] = None


@dataclass(frozen=True)
class CommentRef:
    """A normalized pointer to a human-authored comment / note.

    In trackers like Redmine a comment is carried by a journal entry; the
    ``journal_id`` links back to it when known. ``notes`` is the comment body
    as provided by the tracker — callers remain responsible for the
    secret / private-data rules before persisting it anywhere durable.
    """

    provider: str
    issue_id: str
    notes: str
    journal_id: Optional[str] = None


@dataclass(frozen=True)
class WorkflowGate:
    """A core-recognized workflow gate observed on an issue.

    ``name`` is always one of :data:`WORKFLOW_GATE_KINDS`; construction goes
    through :func:`classify_workflow_gate` so a provider can never smuggle in
    an unrecognized gate name. ``journal_ref`` points at the journal the gate
    was observed in, when known.
    """

    name: str
    issue_id: str
    journal_ref: Optional[JournalRef] = None


@dataclass(frozen=True)
class OwnerApproval:
    """A core decision about whether owner close approval is satisfied.

    This record is produced by :func:`owner_approval`, never directly by a
    provider. A provider may report close *mechanics* (e.g. an issue moved to a
    closed status), but whether that constitutes a satisfied owner approval is
    a core judgement recorded here.
    """

    issue_id: str
    approved: bool
    approver: Optional[str] = None
    journal_ref: Optional[JournalRef] = None


@runtime_checkable
class TicketProvider(Protocol):
    """The built-in ticket provider boundary.

    Implementations are *built-in* providers only — there is no dynamic
    loading and no public extension contract (see the module docstring and the
    adapter-boundary design doc). A provider converts its own API shapes into
    the normalized records above and owns URL formatting for its tracker. It
    must not implement workflow-gate classification or owner-approval
    decisions; those are core functions in this module.
    """

    name: str

    def normalize_issue(self, raw: Mapping[str, object]) -> IssueRef:
        """Normalize one provider-native issue payload into an :class:`IssueRef`."""
        ...


def classify_workflow_gate(
    kind: str,
    issue_id: str,
    *,
    journal_ref: Optional[JournalRef] = None,
) -> Optional[WorkflowGate]:
    """Map an observed handoff/journal ``kind`` to a core :class:`WorkflowGate`.

    Returns ``None`` for any kind that is not a recognized workflow gate
    (including ``implementation_request`` / ``design_consultation`` / ``reply``
    / ``custom``, which are not completion gates). This is the *only* sanctioned
    way to build a :class:`WorkflowGate`, keeping the gate vocabulary
    core-owned. Pure; no I/O.
    """
    if kind not in WORKFLOW_GATE_KINDS:
        return None
    return WorkflowGate(name=kind, issue_id=issue_id, journal_ref=journal_ref)


def owner_approval(
    issue_id: str,
    *,
    approved: bool,
    approver: Optional[str] = None,
    journal_ref: Optional[JournalRef] = None,
) -> OwnerApproval:
    """Construct the core's owner-approval decision for an issue.

    This is the core-side boundary the design doc requires: a provider may
    surface close mechanics, but only core decides — via this function —
    whether close approval is satisfied. Pure; no I/O.
    """
    return OwnerApproval(
        issue_id=issue_id,
        approved=approved,
        approver=approver,
        journal_ref=journal_ref,
    )


__all__ = (
    "CommentRef",
    "IssueRef",
    "JournalRef",
    "OwnerApproval",
    "TicketProvider",
    "TicketRecordError",
    "WORKFLOW_GATE_KINDS",
    "WorkflowGate",
    "classify_workflow_gate",
    "owner_approval",
)
