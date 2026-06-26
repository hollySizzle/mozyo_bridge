"""Built-in Redmine ticket provider (Redmine #12034).

The first — and, per the adapter-boundary design (Redmine #12001), for v0.8 the
*only* — concrete :class:`~mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter.TicketProvider`.
It converts Redmine API JSON shapes (the ``/issues.json`` payload, the
``journals`` array, and the ``RedmineAnchor`` used by handoffs) into the
core-facing normalized records, and owns Redmine-specific URL formatting.

What this provider deliberately does **not** do, because core owns it:

- it does not classify workflow gates (use
  :func:`mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter.classify_workflow_gate`);
- it does not decide owner close approval (use
  :func:`mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter.owner_approval`);
- it does not perform any network call here — normalization is pure over data
  the caller already fetched, so it can never become a second place that sends
  the API key anywhere. The trusted-base / credential boundary stays in
  ``mozyo_bridge.redmine_context``.

There is no dynamic provider loading and no public plugin contract; this is a
built-in classification, not an extension point.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import RedmineAnchor
from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter import (
    CommentRef,
    IssueRef,
    JournalRef,
)

PROVIDER_NAME = "redmine"


def _str_or_none(value: object) -> Optional[str]:
    """Coerce a JSON scalar to ``str`` for an id/text field, or ``None``.

    Redmine ids arrive as ints; record ids are strings so providers stay
    comparable. Empty / missing values normalize to ``None`` rather than the
    string ``"None"``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class RedmineTicketProvider:
    """Normalize Redmine API shapes into core ticket records."""

    name = PROVIDER_NAME

    def normalize_issue(self, raw: Mapping[str, object]) -> IssueRef:
        """Convert one Redmine issue object into an :class:`IssueRef`.

        Lenient by design: this mirrors the best-effort posture of the cockpit
        read model, so a partial issue object still yields a record with
        ``None`` fields rather than raising. The Redmine ``status`` is a nested
        object whose ``name`` we surface; ``subject`` is intentionally never
        read (surface minimization — it can carry confidential summaries).
        """
        status = raw.get("status")
        status_name = (
            _str_or_none(status.get("name"))
            if isinstance(status, Mapping)
            else None
        )
        return IssueRef(
            provider=self.name,
            id=_str_or_none(raw.get("id")) or "",
            status=status_name,
            updated_on=_str_or_none(raw.get("updated_on")),
        )

    def normalize_journal(
        self, issue_id: str, raw: Mapping[str, object]
    ) -> JournalRef:
        """Convert one Redmine journal object into a :class:`JournalRef`."""
        return JournalRef(
            provider=self.name,
            issue_id=str(issue_id),
            id=_str_or_none(raw.get("id")) or "",
            created_on=_str_or_none(raw.get("created_on")),
        )

    def normalize_journals(
        self, issue_id: str, raw_journals: Sequence[Mapping[str, object]]
    ) -> list[JournalRef]:
        """Convert a Redmine ``journals`` array into :class:`JournalRef` records."""
        return [
            self.normalize_journal(issue_id, j)
            for j in raw_journals
            if isinstance(j, Mapping)
        ]

    def normalize_comments(
        self, issue_id: str, raw_journals: Sequence[Mapping[str, object]]
    ) -> list[CommentRef]:
        """Extract human comments (journal ``notes``) as :class:`CommentRef` records.

        Redmine carries comments inside journal entries; only journals with a
        non-empty ``notes`` body become comments (pure field-change journals do
        not). The note body is returned verbatim — callers own the
        secret / private-data rules before persisting it anywhere durable.
        """
        comments: list[CommentRef] = []
        for j in raw_journals:
            if not isinstance(j, Mapping):
                continue
            notes = _str_or_none(j.get("notes"))
            if not notes:
                continue
            comments.append(
                CommentRef(
                    provider=self.name,
                    issue_id=str(issue_id),
                    notes=notes,
                    journal_id=_str_or_none(j.get("id")),
                )
            )
        return comments

    def refs_from_anchor(
        self, anchor: RedmineAnchor
    ) -> tuple[IssueRef, JournalRef]:
        """Bridge an existing handoff :class:`RedmineAnchor` to the record seam.

        A handoff anchor already is a durable ``(issue, journal)`` pointer; this
        exposes it as the same normalized records the API path produces, so
        downstream workflow code can speak one record vocabulary regardless of
        whether the pointer came from a live fetch or a handoff marker.
        """
        issue_ref = IssueRef(provider=self.name, id=str(anchor.issue))
        journal_ref = JournalRef(
            provider=self.name,
            issue_id=str(anchor.issue),
            id=str(anchor.journal),
        )
        return issue_ref, journal_ref

    @staticmethod
    def issue_url(base_url: str, issue_id: str | int) -> str:
        """Format the canonical Redmine issue URL (provider-owned formatting).

        ``base_url`` must already be a trusted scheme+host (see
        ``redmine_context.normalize_base_url``); this only appends the
        Redmine-specific ``/issues/<id>`` path.
        """
        return f"{base_url.rstrip('/')}/issues/{issue_id}"


# Stateless singleton; the provider holds no per-call state.
REDMINE_TICKET_PROVIDER = RedmineTicketProvider()


__all__ = (
    "PROVIDER_NAME",
    "REDMINE_TICKET_PROVIDER",
    "RedmineTicketProvider",
)
