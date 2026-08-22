"""The closed contract a stall-watch discovery row must satisfy (Redmine #15855).

Split out of :mod:`...state.stall_escalation` to keep both sides inside the module-health
line budget (``vibes/docs/logics/module-health-gate.md``), and because this is the natural
seam rather than an arbitrary cut: everything here is **pure** — a vocabulary, a grammar,
and the predicates that enforce them. No SQLite, no filesystem, no clock. The store module
re-exports every public name, so callers and tests keep a single import surface.

Why the contract is a module rather than a docstring
----------------------------------------------------
The discovery row is rendered verbatim onto operator surfaces (``workflow supervisor
--status``, in text and JSON). ``stall_escalation`` DECLARED that this was safe — "every
stored value is a fixed classification token, an identity, a count, or a timestamp" — and
enforced none of it, so review j#110169 reproduced an absolute path and a negative count
reaching ``--status``, and review j#110183 then reproduced the same thing through the one
column the first fix had not covered.

The lesson those two rounds encode, and the reason this file exists: **a closed vocabulary
that lives only in prose is not closed.** Each rule below is enforced at BOTH the write and
the read boundary, because a store is not a trust boundary — an older build, a hand-edited
DB or a half-written row can all hold values this build forbids, and the read path is what
actually feeds the surface.

Two refusal disciplines are shared by every check here:

- **a refusal never quotes the offending value.** An error message is something an operator
  reads, so quoting the string the check exists to contain simply moves the leak.
- **a rejected row echoes nothing** — not its counts, not its reasons, and not its
  timestamp. A row whose contents are untrusted has an untrusted timestamp too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

#: The canonical drop-reason vocabulary a discovery row may name.
#:
#: Declared HERE rather than imported from the discovery layer for the same reason
#: :class:`StreakRow` is not :class:`StreakState`: this store must not reach into the
#: policy/application modules, or a rule ends up written in a state store. The cost of
#: duplication is paid by a test that asserts this set equals the producer's ``DROP_REASONS``
#: **in both directions**, so adding a reason on one side and not the other fails loudly
#: instead of silently widening what a durable row may say.
DISCOVERY_DROP_REASONS: frozenset[str] = frozenset(
    {
        "foreign_workspace",
        "outside_declared_scope",
        "live_generation_unresolved",
        "issue_anchor_unresolved",
        "no_live_locator",
    }
)

#: The one reason that is somebody ELSE's business rather than a gap in this watcher's
#: reach, so it is excluded from ``out_of_reach``. Kept as a name because the coverage
#: identity below depends on it.
DISCOVERY_FOREIGN_REASON = "foreign_workspace"

#: What a stored discovery row is replaced by when it does not satisfy the contract this
#: module declares. Every count is zero and **no stored value is echoed** — the whole point
#: is that a row which failed validation must not reach an operator surface, and a row whose
#: reasons are untrusted has an untrusted timestamp too.
#:
#: Deliberately distinct from ``None``: "the watcher has never run" and "the watcher's
#: record is unreadable" call for different operator actions, and collapsing them would hide
#: a corrupt store behind a benign-looking blank.
DISCOVERY_UNREADABLE = "unreadable"

#: The declared grammar for every timestamp this store renders.
#:
#: A timestamp column looked safe by inspection — "it's a timestamp" — and was therefore the
#: one column of the discovery row R4 validated nothing about. It is not safe by inspection:
#: nothing made it a timestamp, so it was whatever the caller passed, and review j#110183
#: reproduced ``/private/example/unsafe-observed-at`` reaching both the text and the JSON
#: status. A value is a timestamp here only if it parses AND carries a timezone; the
#: producers (:func:`_utc_now_iso` and the watcher leg's per-pass stamp) both do.
#:
#: Accepted values are NORMALIZED on write, so "parses but is written oddly" cannot survive
#: either: what comes back out is exactly what this module would have written itself.
TIMESTAMP_MAX_LENGTH = 40


def checked_timestamp(value: object, *, field: str) -> str:
    """A tz-aware ISO-8601 instant in canonical form, or a typed refusal.

    The refusal deliberately does NOT quote the offending value: the whole point of the
    check is to keep caller-supplied text out of anything an operator reads, and an error
    string is read by an operator.
    """
    if not isinstance(value, str) or not value.strip():
        raise StallDiscoveryContractError(
            f"discovery {field} must be a non-empty ISO-8601 timestamp string, not "
            f"{type(value).__name__}"
        )
    text = value.strip()
    if len(text) > TIMESTAMP_MAX_LENGTH:
        raise StallDiscoveryContractError(
            f"discovery {field} exceeds {TIMESTAMP_MAX_LENGTH} characters; "
            "an ISO-8601 instant does not"
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise StallDiscoveryContractError(
            f"discovery {field} is not an ISO-8601 timestamp"
        ) from None
    if parsed.tzinfo is None:
        # A naive instant cannot be compared with the tz-aware "now" a status surface uses,
        # and an operator reading it cannot tell which clock it belongs to.
        raise StallDiscoveryContractError(
            f"discovery {field} must carry a timezone offset"
        )
    return parsed.isoformat(timespec="seconds")


#: What an unparseable stored timestamp is replaced by on a PENDING row. A pending row is a
#: real escalation, so dropping it would lose the stall report itself; the value is replaced
#: instead, which keeps the row (and the SQL ordering that settles oldest-first) while
#: letting nothing arbitrary reach the Redmine note or the JSON status.
TIMESTAMP_UNREADABLE = "<unreadable-timestamp>"


def rendered_timestamp(value: object, *, field: str) -> str:
    """A stored timestamp for rendering: canonical, or the closed unreadable token."""
    try:
        return checked_timestamp(value, field=field)
    except StallDiscoveryContractError:
        return TIMESTAMP_UNREADABLE


#: Typed reasons a discovery row is rejected. Closed, so a status surface can branch.
DISCOVERY_BAD_REASON_TOKEN = "off_vocabulary_reason"
DISCOVERY_BAD_COUNT = "invalid_count"
DISCOVERY_INCONSISTENT = "inconsistent_counts"
DISCOVERY_MALFORMED = "malformed_row"
DISCOVERY_BAD_TIMESTAMP = "invalid_timestamp"


class StallDiscoveryContractError(ValueError):
    """A discovery summary violated the closed contract this module declares.

    Raised at the WRITE boundary. The read boundary cannot raise — a corrupt row must not
    make ``--status`` explode — so it degrades to :data:`DISCOVERY_UNREADABLE` instead.
    """


def checked_count(value: object, *, field: str) -> int:
    """A non-negative integer, or a typed refusal. ``bool`` is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StallDiscoveryContractError(
            f"discovery {field} must be a non-negative integer, not {type(value).__name__}"
        )
    if value < 0:
        raise StallDiscoveryContractError(
            f"discovery {field} must be non-negative; got {value}"
        )
    return int(value)


def validate_discovery(
    *, candidates: int, watched: int, out_of_reach: int, dropped: Optional[dict]
) -> "tuple[int, int, int, dict[str, int]]":
    """Check a coverage summary against the closed contract; raise on any violation.

    Three separate obligations, each of which the store previously only *claimed*:

    - every reason is a declared token, so no caller-supplied string (a path, a message, a
      lane id) can reach a durable row and from there an operator surface;
    - every count is a non-negative integer, so a status line cannot render ``-3``;
    - the counts agree with each other. The producer partitions every candidate into
      exactly one bucket, so ``candidates == watched + sum(dropped)`` holds by construction,
      and ``out_of_reach`` is that sum minus the foreign-workspace rows. A row that fails
      these is not a row this rail wrote, whatever it says.
    """
    counts = {
        "candidates": checked_count(candidates, field="candidates"),
        "watched": checked_count(watched, field="watched"),
        "out_of_reach": checked_count(out_of_reach, field="out_of_reach"),
    }
    if dropped is None:
        dropped = {}
    if not isinstance(dropped, dict):
        raise StallDiscoveryContractError(
            f"discovery dropped must be a mapping, not {type(dropped).__name__}"
        )
    checked: dict[str, int] = {}
    for reason, count in dropped.items():
        if not isinstance(reason, str) or reason not in DISCOVERY_DROP_REASONS:
            # The reason is NOT echoed back: quoting an off-vocabulary token in the error
            # would put the very string this check exists to contain into a log line.
            raise StallDiscoveryContractError(
                "discovery dropped names a reason outside the declared vocabulary; "
                f"allowed: {sorted(DISCOVERY_DROP_REASONS)}"
            )
        checked[reason] = checked_count(count, field=f"dropped[{reason}]")

    total_dropped = sum(checked.values())
    if counts["candidates"] != counts["watched"] + total_dropped:
        raise StallDiscoveryContractError(
            "discovery counts disagree: candidates must equal watched + sum(dropped); "
            f"got {counts['candidates']} != {counts['watched']} + {total_dropped}"
        )
    expected_reach = total_dropped - checked.get(DISCOVERY_FOREIGN_REASON, 0)
    if counts["out_of_reach"] != expected_reach:
        raise StallDiscoveryContractError(
            "discovery counts disagree: out_of_reach must equal sum(dropped) minus "
            f"{DISCOVERY_FOREIGN_REASON}; got {counts['out_of_reach']} != {expected_reach}"
        )
    return counts["candidates"], counts["watched"], counts["out_of_reach"], checked


def discovery_reject_token(exc: StallDiscoveryContractError) -> str:
    """Map a contract violation onto a CLOSED token — never the exception's own text.

    Same discipline as the config resolver's redaction: an operator surface gets a token it
    can branch on, and the message (which may quote a count, a field name, or a caller's
    data) stays out of it.
    """
    text = str(exc)
    if "observed_at" in text:
        return DISCOVERY_BAD_TIMESTAMP
    if "vocabulary" in text:
        return DISCOVERY_BAD_REASON_TOKEN
    if "disagree" in text:
        return DISCOVERY_INCONSISTENT
    if "non-negative integer" in text or "non-negative" in text or "mapping" in text:
        return DISCOVERY_BAD_COUNT
    return DISCOVERY_MALFORMED


def unreadable_discovery(reason: str) -> dict:
    """The typed stand-in for a stored row that failed validation (echoes nothing)."""
    return {
        "observed_at": "",
        "candidates": 0,
        "watched": 0,
        "out_of_reach": 0,
        "dropped": {},
        DISCOVERY_UNREADABLE: reason,
    }


__all__ = (
    "DISCOVERY_BAD_COUNT",
    "DISCOVERY_BAD_REASON_TOKEN",
    "DISCOVERY_BAD_TIMESTAMP",
    "DISCOVERY_DROP_REASONS",
    "DISCOVERY_FOREIGN_REASON",
    "DISCOVERY_INCONSISTENT",
    "DISCOVERY_MALFORMED",
    "DISCOVERY_UNREADABLE",
    "TIMESTAMP_MAX_LENGTH",
    "TIMESTAMP_UNREADABLE",
    "StallDiscoveryContractError",
    "checked_count",
    "checked_timestamp",
    "discovery_reject_token",
    "rendered_timestamp",
    "unreadable_discovery",
    "validate_discovery",
)
