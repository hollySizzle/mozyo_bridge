"""The SUCCESSOR's own acknowledgement of an audit-failure supersession (Redmine #15166).

:mod:`.superseded_audit_failure_terminal` owns the declaration a lane writes about ITSELF and the
fence that reads it. This module owns the one record that declaration may not write for itself
without also writing into another issue's history: the named successor's acknowledgement that it
supersedes this issue's independent-audit round.

Split out of that module when the review j#101880 hardening pushed it past the oversized-module
gate, mirroring the #14755 layout one contract over (``superseded_failure_terminal`` /
``superseded_failure_correlation``). This is a move, not a change: the grammar, the field order and
the fold semantics are byte-identical, and the terminal module re-exports every name.

**What this record does and does not establish, stated rather than implied.** It is written through
the same source system as the declaration, and this workspace cannot authenticate a journal's
writer (ruling #14219 j#86718). Review j#101880 finding 1 measured the consequence directly: one
actor can place BOTH the declaration and this acknowledgement, so the two agreeing is not
independent corroboration and must never be the authority a terminal rests on. What this fold does
establish is POINTER INTEGRITY — the named successor's record names this issue and this audit
journal back, so the terminal is demonstrably about the records it claims to be about. The
admitting fact lives elsewhere (:mod:`.superseded_audit_failure_terminal` states it in one place:
the successor's approved Review Gate examined this lane's exact head).

Boundary: pure. A total function over ``(journal_id, notes)`` pairs; no IO, no Redmine, no git.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
    journal_ref,
    one_canonical_marker,
)


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None



#: The gate token the SUCCESSOR issue declares to acknowledge that it supersedes another issue's
#: independent-audit failure. A SEPARATE token from #14755's ``superseded_failure_successor``
#: rather than a shared one, for the same reason every authority gate here is named per surface: an
#: acknowledgement written about a failed Review Gate answers a different question than one written
#: about an audit record that never was a gate, and a record must never satisfy a contract it was
#: not written under. The field sets differ accordingly.
SUCCESSOR_ACK_GATE = "superseded_audit_failure_successor"
#: The schema version. An unknown version is REFUSED rather than interpreted under today's field
#: meanings.
SUCCESSOR_ACK_VERSION = "1"
#: The only admissible decision. A record written to DECLINE the pairing carries a different token
#: and therefore cannot corroborate anything.
SUCCESSOR_ACK_DECISION = "supersedes"

#: The COMPLETE, ORDERED field set a canonical acknowledgement carries — no more, no less, in this
#: sequence. There is deliberately NO lane envelope: the acknowledgement is about the SUCCESSOR's
#: work, whose lane is not the retire target's, so an envelope here could not be exact-matched
#: against anything the retire measures and would be a field that looks like a fence but is not.
SUCCESSOR_ACK_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "decision",
    "issue",
    "superseded_issue",
    "superseded_audit_journal",
    "review_journal",
)

#: No acknowledgement is in the successor's durable record at all.
ACK_NONE = "none"
#: A VALID acknowledgement: one canonical marker with every literal at its contracted value.
ACK_ACKNOWLEDGED = "acknowledged"
#: An acknowledgement is DECLARED but cannot be read as one. Fail-closed: treated exactly like
#: :data:`ACK_NONE` by every consumer, and it SUPERSEDES an older valid one.
ACK_INVALID = "invalid"

SUCCESSOR_ACK_STATES: frozenset[str] = frozenset({ACK_NONE, ACK_ACKNOWLEDGED, ACK_INVALID})


@dataclass(frozen=True)
class AuditSupersessionAcknowledgementFacts:
    """The LATEST audit-supersession acknowledgement in one SUCCESSOR issue's journals.

    ``state`` is a closed :data:`SUCCESSOR_ACK_STATES` token; ``journal`` is where it was recorded.
    The identity fields are projected from the canonical marker and are EMPTY unless the
    acknowledgement is valid — never guessed from prose, and never completed from whatever the
    caller happened to ask about.
    """

    state: str = ACK_NONE
    journal: str = ""
    issue: str = ""
    superseded_issue: str = ""
    superseded_audit_journal: str = ""
    review_journal: str = ""

    @property
    def recorded(self) -> bool:
        """True when any acknowledgement (valid or not) is in the successor's record."""
        return self.state != ACK_NONE

    @property
    def in_force(self) -> bool:
        """True ONLY for a VALID acknowledgement. :data:`ACK_INVALID` is False, like absent."""
        return self.state == ACK_ACKNOWLEDGED


def _journal_acknowledgement(notes: str) -> Optional[AuditSupersessionAcknowledgementFacts]:
    """The acknowledgement ONE journal declares, or ``None`` if it declares none (pure)."""
    declared, fields = one_canonical_marker(
        notes, gate=SUCCESSOR_ACK_GATE, field_order=SUCCESSOR_ACK_FIELD_ORDER
    )
    if not declared:
        return None
    if fields is None:
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    constants = {
        "gate": SUCCESSOR_ACK_GATE,
        "version": SUCCESSOR_ACK_VERSION,
        "decision": SUCCESSOR_ACK_DECISION,
    }
    if any(fields.get(key) != value for key, value in constants.items()):
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    issue = str(fields.get("issue", "") or "").strip()
    superseded_issue = str(fields.get("superseded_issue", "") or "").strip()
    superseded_audit = journal_ref(fields.get("superseded_audit_journal", ""))
    review = journal_ref(fields.get("review_journal", ""))
    if not issue or not superseded_issue or not superseded_audit or not review:
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    if issue == superseded_issue:
        # An issue cannot acknowledge that it supersedes itself; a self-referential supersession
        # orders nothing (the #14695 j#94260 shape, one level down).
        return AuditSupersessionAcknowledgementFacts(state=ACK_INVALID)
    return AuditSupersessionAcknowledgementFacts(
        state=ACK_ACKNOWLEDGED,
        issue=issue,
        superseded_issue=superseded_issue,
        superseded_audit_journal=superseded_audit,
        review_journal=review,
    )


def fold_audit_supersession_acknowledgement(
    journals: Sequence[Tuple[object, str]],
) -> AuditSupersessionAcknowledgementFacts:
    """The LATEST audit-supersession acknowledgement across the successor's journals (pure).

    Latest-wins by journal id, supersede-by-EXISTING. A successor that later withdrew the pairing
    by recording a malformed or superseding acknowledgement therefore shadows the older valid one,
    rather than being skipped so the stale one keeps corroborating.
    """
    latest: Optional[Tuple[int, AuditSupersessionAcknowledgementFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_acknowledgement(notes or "")
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                AuditSupersessionAcknowledgementFacts(
                    state=facts.state,
                    journal=str(jint),
                    issue=facts.issue,
                    superseded_issue=facts.superseded_issue,
                    superseded_audit_journal=facts.superseded_audit_journal,
                    review_journal=facts.review_journal,
                ),
            )
    return latest[1] if latest is not None else AuditSupersessionAcknowledgementFacts()


def render_audit_supersession_acknowledgement_marker(
    *,
    issue: str,
    superseded_issue: str,
    superseded_audit_journal: object,
    review_journal: object,
) -> str:
    """The exact marker a valid successor acknowledgement must carry (pure).

    Field order is :data:`SUCCESSOR_ACK_FIELD_ORDER`, so what this emits is what the strict reader
    accepts, by construction. Every producer error raises ``ValueError`` rather than being written.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
        reject_marker_separator,
    )

    issue_s = str(issue or "").strip()
    superseded_s = str(superseded_issue or "").strip()
    if not issue_s or not superseded_s:
        raise ValueError(
            "an audit-supersession acknowledgement requires a non-empty issue and superseded_issue"
        )
    if issue_s == superseded_s:
        raise ValueError("an issue cannot acknowledge that it supersedes itself")
    reject_marker_separator(issue_s, field="issue")
    reject_marker_separator(superseded_s, field="superseded_issue")
    superseded_audit = journal_ref(superseded_audit_journal)
    review = journal_ref(review_journal)
    if not superseded_audit:
        raise ValueError(
            "an audit-supersession acknowledgement requires the superseded audit journal id, got "
            f"{superseded_audit_journal!r}"
        )
    if not review:
        raise ValueError(
            "an audit-supersession acknowledgement requires its own approved review journal id, "
            f"got {review_journal!r}"
        )
    body = ":".join(
        [
            f"gate={SUCCESSOR_ACK_GATE}",
            f"version={SUCCESSOR_ACK_VERSION}",
            f"decision={SUCCESSOR_ACK_DECISION}",
            f"issue={issue_s}",
            f"superseded_issue={superseded_s}",
            f"superseded_audit_journal={superseded_audit}",
            f"review_journal={review}",
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"

__all__ = (
    "ACK_ACKNOWLEDGED",
    "ACK_INVALID",
    "ACK_NONE",
    "AuditSupersessionAcknowledgementFacts",
    "SUCCESSOR_ACK_DECISION",
    "SUCCESSOR_ACK_FIELD_ORDER",
    "SUCCESSOR_ACK_GATE",
    "SUCCESSOR_ACK_STATES",
    "SUCCESSOR_ACK_VERSION",
    "fold_audit_supersession_acknowledgement",
    "render_audit_supersession_acknowledgement_marker",
)
