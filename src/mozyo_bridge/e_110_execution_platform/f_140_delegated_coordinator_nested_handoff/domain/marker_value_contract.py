"""The PRODUCER-side half of the marker grammar: what a field value may be (Redmine #14539).

The strict readers in :mod:`.redmine_journal_source` answer "could a canonical producer have
rendered this body". This module answers the same question from the other side, before anything
is written: a value that would not read back is refused at the boundary rather than discovered
later in a durable record.

Split out of the reader module so the two halves of one contract sit beside each other without
either growing past the module-health threshold; the reader module re-exports these names, so
every existing import keeps working.
"""

from __future__ import annotations


#: Characters a field value may not contain: the marker grammar uses them as delimiters, so a
#: value carrying one forges a field boundary and reads back as a DIFFERENT well-formed body.
MARKER_VALUE_FORBIDDEN_CHARS = frozenset(":=[]")


class MarkerValueError(ValueError):
    """A field value the marker grammar cannot round-trip (raised by the producer, never read)."""


def validate_marker_field_value(field: str, value: object, *, what: str = "marker") -> str:
    """The stripped value, or raise — the PRODUCER-side twin of the strict readers (pure).

    Promoted from :mod:`...domain.callback_recovery_key`, which hardened exactly this check for
    the recovery-admission channel (its ``_FORBIDDEN_VALUE_CHARS`` / ``_validate_value``) and now
    calls it here. Redmine #14539 review j#92374 finding 2 found the other two producers had no
    such check at all: ``build_dispatch_authorization_marker`` concatenated every value unvalidated
    and ``render_dispatch_disposition_marker`` checked only non-emptiness, so a caller passing
    ``lane_id='r1:unexpected=1'`` got a marker back that the module's OWN parser then refused —
    a canonical producer able to write a self-poisoning record into a durable authority channel.

    Refused: an empty value, one containing a delimiter, and one containing ANY whitespace. The
    whitespace rule is deliberately stricter than the reader, which only refuses whitespace
    AROUND a component: ``lane_id='r 1'`` does round-trip today, but a producer that has to
    reason about which whitespace survives is one refactor away from emitting the kind that does
    not. A producer should only emit what it is certain reads back.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise MarkerValueError(
            f"{what} field {field!r} is empty; a blank field names nothing and the strict "
            "readers refuse the whole marker"
        )
    bad = sorted(set(text) & MARKER_VALUE_FORBIDDEN_CHARS)
    if bad:
        raise MarkerValueError(
            f"{what} field {field!r}={text!r} contains {bad!r}, which the marker grammar uses as "
            "field delimiters: it would read back as a DIFFERENT well-formed body. Refusing to "
            "render a record that cannot round-trip"
        )
    if any(character.isspace() for character in text):
        raise MarkerValueError(
            f"{what} field {field!r}={text!r} contains whitespace, which a marker cannot be "
            "relied on to round-trip verbatim"
        )
    return text


#: The only characters a journal id may be built from. NOT ``str.isdigit()`` / ``str.isdecimal()``:
#: both answer True for non-ASCII digits (``"٣"`` is a decimal digit to Python), and a marker field
#: that reads as a number to Python but not to the source system is not the journal it names.
_ASCII_DIGITS = frozenset("0123456789")


def is_journal_id(value: object) -> bool:
    """Whether ``value`` is a POSITIVE decimal journal id, judged on the RAW value (pure).

    The shape half of the Review Generation Marker Contract v2's ``req=<review_request journal id>``
    — the sibling of ``review_return_route.is_full_commit_head``, which does the same job for
    ``head``. Added here (Redmine #14694 review j#93818 finding 2) rather than beside that head
    predicate purely for module-health headroom; it changes nothing about
    :func:`validate_marker_field_value`, whose four callers keep their behaviour exactly.

    Judged raw and by identity of characters, so ``" 93802 "``, ``"93 802"``, ``"abc"``,
    ``"93802=shadow"``, ``"-5"``, ``"1.5"``, ``"0"`` and a non-``str`` are all refused. The
    producer and the CLI boundary both ask THIS question, so "what a journal id looks like" has one
    answer: the CLI returns its typed refusal and the producer raises, but neither invents a value.
    """
    if not isinstance(value, str) or not value:
        return False
    if not set(value) <= _ASCII_DIGITS:
        return False
    return int(value) > 0


def require_journal_id(value: object, *, field: str = "req") -> str:
    """The RAW value as a journal id, or raise (pure). The producer twin of :func:`is_journal_id`."""
    if not is_journal_id(value):
        raise MarkerValueError(
            f"marker field {field!r} must be a positive decimal journal id, got {value!r}"
        )
    return str(value)


def require_review_head(value: object, *, field: str = "head") -> str:
    """The RAW value as a review marker's full commit head, or raise (pure).

    Whitespace and marker punctuation are refused FIRST, so ``is_full_commit_head`` (which strips
    for the read side) is judging the raw value — the same order ``render_lane_envelope`` uses, and
    the same head predicate the CLI boundary asks, so "what a head looks like" has one answer
    (Redmine #14694 review j#93818 finding 1).
    """
    from .hibernate_evidence_envelope import require_marker_token
    from .review_return_route import is_full_commit_head

    require_marker_token(value, field=field, requirement="review marker")
    if not is_full_commit_head(value):
        raise MarkerValueError(
            f"marker field {field!r} must be a full commit head, got {value!r}"
        )
    return str(value)


def require_vocabulary(value: object, *, field: str, vocabulary) -> str:
    """The RAW value, or raise unless it is EXACTLY one of ``vocabulary`` (pure).

    Vocabulary rather than shape, because for these fields a value outside the closed set is not
    merely unreadable: ``glance_snapshot_source`` rewrites an unrecognized review conclusion to
    ``pending``, so rendering one writes a record the consumer reads as a DIFFERENT value
    (Redmine #14694 review j#93818 finding 1). The producer and the consumer therefore read the
    same constant.
    """
    if value not in vocabulary:
        raise MarkerValueError(
            f"marker field {field!r} must be one of {sorted(vocabulary)}, got {value!r}"
        )
    return str(value)
