"""Action-time durable-anchor ownership gate for handoff delivery (Redmine #14246)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

from mozyo_bridge.core.state.lane_lifecycle_model import is_redmine_id


class RedmineAnchorLike(Protocol):
    source: str
    issue: str
    journal: str


AnchorT = TypeVar("AnchorT")


@dataclass(frozen=True)
class RedmineAnchorRead:
    """Credential-free facts observed for one requested Redmine anchor."""

    requested_issue: str
    requested_journal: str
    issue_exists: bool
    observed_issue: str | None
    journal_ids: tuple[str, ...]


class RedmineAnchorSource(Protocol):
    """Read-only port used by the action-time ownership gate."""

    def read_anchor(self, issue: str, journal: str) -> RedmineAnchorRead: ...


class AnchorAuthorityError(RuntimeError):
    """A typed, pre-injection refusal with the normalized anchor preserved."""

    def __init__(self, reason: str, message: str, anchor: RedmineAnchorLike):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.anchor = anchor


def _refuse(reason: str, anchor: RedmineAnchorLike) -> None:
    messages = {
        "anchor_issue_not_found": (
            f"Redmine issue #{anchor.issue} was not found; handoff refused before "
            "target resolution and transport"
        ),
        "anchor_journal_not_found": (
            f"Redmine journal #{anchor.journal} was not found under issue "
            f"#{anchor.issue}; handoff refused before target resolution and transport"
        ),
        "anchor_issue_journal_mismatch": (
            f"Redmine journal #{anchor.journal} does not belong to issue "
            f"#{anchor.issue}; handoff refused before target resolution and transport"
        ),
        "anchor_provider_unreadable": (
            "Redmine anchor ownership could not be verified at action time; check the "
            "trusted Redmine configuration and availability, then retry"
        ),
    }
    raise AnchorAuthorityError(reason, messages[reason], anchor)


def verify_redmine_anchor(
    anchor: RedmineAnchorLike, source: RedmineAnchorSource
) -> RedmineAnchorLike:
    """Verify that ``journal`` belongs to ``issue``, or fail closed before send."""

    try:
        observed = source.read_anchor(anchor.issue, anchor.journal)
    except Exception:
        _refuse("anchor_provider_unreadable", anchor)

    if not isinstance(observed, RedmineAnchorRead):
        _refuse("anchor_provider_unreadable", anchor)
    if (
        type(observed.issue_exists) is not bool
        or observed.requested_issue != anchor.issue
        or observed.requested_journal != anchor.journal
    ):
        _refuse("anchor_provider_unreadable", anchor)
    if not observed.issue_exists:
        if observed.observed_issue is not None or observed.journal_ids:
            _refuse("anchor_provider_unreadable", anchor)
        _refuse("anchor_issue_not_found", anchor)
    if observed.observed_issue is None or not is_redmine_id(observed.observed_issue):
        _refuse("anchor_provider_unreadable", anchor)
    if observed.observed_issue != anchor.issue:
        _refuse("anchor_issue_journal_mismatch", anchor)
    journal_ids = observed.journal_ids
    if (
        not isinstance(journal_ids, tuple)
        or any(not isinstance(item, str) or not is_redmine_id(item) for item in journal_ids)
        or len(journal_ids) != len(set(journal_ids))
    ):
        _refuse("anchor_provider_unreadable", anchor)
    if anchor.journal not in journal_ids:
        _refuse("anchor_journal_not_found", anchor)
    return anchor


def verify_live_handoff_anchor(anchor: AnchorT) -> AnchorT:
    """Verify a live Redmine anchor; non-Redmine anchor families pass unchanged."""

    if getattr(anchor, "source", None) != "redmine":
        return anchor
    redmine_anchor = cast(RedmineAnchorLike, anchor)
    try:
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_anchor_source import (
            LiveRedmineAnchorSource,
        )

        source = LiveRedmineAnchorSource.from_environment()
    except Exception:
        _refuse("anchor_provider_unreadable", redmine_anchor)
    return cast(AnchorT, verify_redmine_anchor(redmine_anchor, source))


__all__ = (
    "AnchorAuthorityError",
    "RedmineAnchorRead",
    "RedmineAnchorLike",
    "RedmineAnchorSource",
    "verify_live_handoff_anchor",
    "verify_redmine_anchor",
)
