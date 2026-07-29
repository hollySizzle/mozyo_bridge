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
