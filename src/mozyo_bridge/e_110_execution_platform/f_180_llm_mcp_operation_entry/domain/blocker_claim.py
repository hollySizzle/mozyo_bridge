"""Reading an authoritative blocker claim from a durable journal (pure, #15162).

The acceptance says ``blocked`` may only be reported with an authoritative blocker
source, a reason, and a resume condition. This module is where those three come
from — and, just as importantly, where "there is no such record" is answered
honestly instead of being softened into a blocked-ish state.

**No second grammar is invented.** The repo already has one governed shape for a
declared blocked state: the parked-state journal whose fixed fields the sublane
completion guardrail pins (``state`` / ``durable_anchor`` / ``callback_result`` /
``blocked_by`` / ``resume_condition`` / ``resume_owner``). This module reads those
fields with that shape's own field reader, :func:`governed_field`, so a duplicated
field with differing values is a conflict here exactly as it is there.

**A deliberate subset, named as such.** ``park_journal_gap`` — the hibernate
*evidence* gate — additionally requires a complete callback-outcome record
(``target`` plus a replayable retry command). That answers a different question:
*was anyone told about the park*. It is the right bar for "may this lane be
auto-hibernated"; it is the wrong bar for "is this Unit blocked", because a
declaration with an imperfect retry command still records a real blocker, and
reporting such a Unit as ``unknown`` would understate a state the durable record
does state.

So this module requires the **blocker subset**: ``state: blocked``, a non-empty
``blocked_by``, a non-empty ``resume_condition``, and a ``durable_anchor`` that
points at *this* declaration. Anything less is not a claim, and
:func:`read_blocker_claim` returns ``None`` — never a partial claim, and never a
hedge.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
    governed_field,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E501
    FRESHNESS_UNKNOWN,
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E501
    BlockedClaim,
)

#: ``state:`` value a parked-state declaration carries.
PARK_STATE_BLOCKED = "blocked"

#: ``durable_anchor:`` grammar — ``#<issue> j#<journal>``.
DURABLE_ANCHOR_RE = re.compile(r"^#(?P<issue>\d+)\s+j#(?P<journal>\d+)$")

#: The fields that constitute a blocker claim, in the governed shape's spelling.
BLOCKER_CLAIM_FIELDS = ("state", "blocked_by", "resume_condition", "durable_anchor")


def _single(notes: str, name: str) -> str:
    """The field's single value, or ``""`` for absent **and for conflicting**.

    ``governed_field`` returns a private sentinel (not a string) when the same
    field is declared twice with differing values. A record that says two things
    proves neither, so it is folded to absent here rather than picking one.
    """
    value = governed_field(notes, name)
    return value if isinstance(value, str) else ""


def read_blocker_claim(
    notes: str,
    *,
    issue_id: str,
    journal_id: str,
    observed_at: Optional[str] = None,
    freshness: str = FRESHNESS_UNKNOWN,
) -> Optional[BlockedClaim]:
    """Read one journal note as a blocker claim, or ``None``.

    ``issue_id`` / ``journal_id`` scope the read: the note's ``durable_anchor``
    must name *this* declaration. An anchor pointing at some other journal of the
    same issue is a pointer to a different record, and letting it stand in would
    let any older note on the issue supply this state's evidence.
    """
    if not notes:
        return None
    state = _single(notes, "state")
    if state.strip().lower() != PARK_STATE_BLOCKED:
        return None
    reason = _single(notes, "blocked_by").strip()
    resume = _single(notes, "resume_condition").strip()
    anchor_raw = _single(notes, "durable_anchor").strip()
    if not (reason and resume and anchor_raw):
        return None
    anchor = DURABLE_ANCHOR_RE.match(anchor_raw)
    if anchor is None:
        return None
    if issue_id and anchor.group("issue") != str(issue_id).strip():
        return None
    if journal_id and anchor.group("journal") != str(journal_id).strip():
        return None
    return BlockedClaim(
        blocker_source=SOURCE_REDMINE,
        reason=reason,
        resume_condition=resume,
        durable_anchor=anchor_raw,
        observed_at=observed_at,
        freshness=freshness,
    )


def latest_blocker_claim(
    journals: Sequence,
    *,
    issue_id: str,
    observed_at: Optional[str] = None,
    freshness: str = FRESHNESS_UNKNOWN,
) -> Optional[BlockedClaim]:
    """The most recent admissible blocker claim across ``journals``, or ``None``.

    ``journals`` is the ``(journal_id, notes)`` sequence the glance Redmine source
    already produces, so no second fetch and no second journal shape is introduced.
    Scanned newest-first by journal id, because a later declaration supersedes an
    earlier one — and a note that is *not* a claim is skipped rather than ending
    the scan, so an unrelated later journal does not hide a standing block.

    Note what this does and does not answer: it reports the latest *declaration*.
    Whether a later gate resolved it is workflow truth carried by the issue's gate
    fold, which is why the caller reports the claim **alongside** the folded
    workflow state rather than instead of it.
    """

    def _key(pair) -> int:
        try:
            return int(str(pair[0] or 0))
        except (TypeError, ValueError, IndexError):
            return 0

    pairs = [p for p in (journals or ()) if isinstance(p, (tuple, list)) and len(p) >= 2]
    for journal_id, notes in sorted(pairs, key=_key, reverse=True):
        claim = read_blocker_claim(
            str(notes or ""),
            issue_id=issue_id,
            journal_id=str(journal_id or "").strip(),
            observed_at=observed_at,
            freshness=freshness,
        )
        if claim is not None:
            return claim
    return None


__all__ = (
    "BLOCKER_CLAIM_FIELDS",
    "DURABLE_ANCHOR_RE",
    "PARK_STATE_BLOCKED",
    "latest_blocker_claim",
    "read_blocker_claim",
)
