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


def is_exact_str(value: object) -> bool:
    """Whether ``value`` is a builtin ``str`` and not a subclass of one (pure).

    ``isinstance`` is not enough for a PRODUCER (Redmine #14694 review j#94038 blocker 2). Every
    field here is rendered with an f-string, so ``type(value).__format__`` decides the bytes that
    reach the durable record — and a ``str`` subclass may override it. Measured: a subclass
    validated as the head ``a*40`` rendered ``head=b*40``, and one validated as the workspace
    ``ws`` rendered ``workspace=evil:lane=forged``, injecting a second ``lane`` field ahead of the
    real one. Returning "the validated object itself" is not enough when the object gets to choose
    what it looks like at render time; only an exact builtin can promise that what was checked is
    what is written.
    """
    return type(value) is str


def is_exact_int(value: object) -> bool:
    """Whether ``value`` is a builtin ``int`` and not a subclass (and never a ``bool``) (pure).

    The same hazard as :func:`is_exact_str`, on the one numeric field a marker carries. Measured
    while sweeping that blocker's family: an ``int`` subclass overriding ``__format__`` and
    validated as generation ``3`` rendered ``lane_generation=9:head=forged``.
    """
    return type(value) is int


def validate_marker_field_value(field: str, value: object, *, what: str = "marker") -> str:
    """The value as given, or raise — the PRODUCER-side twin of the strict readers (pure).

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

    "ANY whitespace" means the value AS GIVEN (Redmine #14667 review j#93063). This function used
    to ``.strip()`` before checking, so the rule it stated was not the rule it applied: surrounding
    whitespace was silently normalized away and only INTERNAL whitespace was ever refused. A caller
    passing ``lane=' r1'`` got a clean marker back and no indication that its argument was not what
    got written — and in the proxy rail that normalization reached a send. Normalizing an invalid
    value is how a producer ends up emitting something its caller did not ask for; the boundary's
    job is to refuse it and say so. Callers that genuinely hold untrimmed input must trim it
    themselves, deliberately, before they claim the value is what they mean.
    """
    text = str(value if value is not None else "")
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

#: What a canonical journal id may START with — ``0`` would make it a different string from the id
#: the source system owns, and ``req`` is exact-matched against that id, not parsed as a number.
_ASCII_NONZERO_DIGITS = frozenset("123456789")


#: The widest value either decimal marker field can name. DERIVED, never a number written here:
#: the lane lifecycle store declares ``lane_generation INTEGER`` (``core.state.lane_lifecycle_schema``)
#: and SQLite's ``INTEGER`` is a signed 64-bit value, while ``req`` is a Redmine journal id — the
#: source system's own integer record id. A token wider than this names no row in either system.
MAX_CANONICAL_DECIMAL_VALUE = 2**63 - 1

#: The same bound as the decimal string the grammar actually compares against.
_MAX_CANONICAL_DECIMAL = str(MAX_CANONICAL_DECIMAL_VALUE)


def within_marker_decimal_width(value: str) -> bool:
    """Whether a decimal token is narrow enough for EVERY runtime to convert it (pure).

    The stable half of the grammar, shared by the producer and by the envelope PARSER, because the
    bound this replaced was not stable at all: it asked ``sys.get_int_max_str_digits()``, so what
    counted as canonical changed with the Python version AND with a mutable process-global
    (``PYTHONINTMAXSTRDIGITS`` / :func:`sys.set_int_max_str_digits`). Measured on that grammar
    (Redmine #14694 review j#94222): an uncapped producer rendered a 4301-digit ``lane_generation``
    with no refusal, and the default-capped parser then raised ``ValueError`` — one durable marker
    splitting into canonical or crash depending on how the two processes happened to be configured.

    So the bound is the PROTOCOL's, not the interpreter's. It is also always reachable: CPython
    refuses any ``int_max_str_digits`` below ``sys.int_info.str_digits_check_threshold`` (640, or 0
    for unlimited), and this bound is 19 digits — so every value this admits converts under every
    configuration a supported runtime can be in. That is what makes a canonical producer's output
    readable by any other one.
    """
    return len(value) <= len(_MAX_CANONICAL_DECIMAL)


def is_canonical_positive_int(value: object) -> bool:
    """The INT-side twin of :func:`is_canonical_positive_decimal`, judged raw (pure).

    The same rule reached through the other type, because the defect had both halves. Self-detected
    while reproducing review j#94222: ``render_lane_envelope`` checked only ``is_exact_int`` and
    ``> 0`` and then rendered with an f-string, so a generation of ``10**5000`` either RAISED
    ``ValueError`` (capped process — a producer's programming error arriving as an untyped
    exception) or rendered a 5001-digit ``lane_generation`` no capped consumer could read
    (uncapped process). Fixing the string side alone would have left the same defect in the same
    disguise, which is how ``req`` and ``lane_generation`` diverged in the first place.
    """
    return is_exact_int(value) and 0 < value <= MAX_CANONICAL_DECIMAL_VALUE


def is_canonical_positive_decimal(value: object) -> bool:
    """Whether ``value`` is a canonical positive decimal token, judged on the RAW value (pure).

    THE shape shared by the two decimal marker fields — the review contract's ``req`` (a journal
    id) and the envelope's ``lane_generation``. One predicate because they had one defect: fixing
    it for ``req`` in R5 and leaving ``lane_generation`` on ``isdigit()`` + ``int()`` is what review
    j#94038 blocker 1 found, so the shape now has a single home rather than a per-field copy.

    See :func:`is_journal_id` for why this is lexical and never goes through ``int()``.
    """
    if not is_exact_str(value) or not value:
        return False
    if value[0] not in _ASCII_NONZERO_DIGITS:
        return False
    if not set(value) <= _ASCII_DIGITS:
        return False
    # A token naming no row in either source system names nothing. Both digits-only and no leading
    # zero are already established, so comparing the strings IS comparing the numbers — the bound is
    # applied without an `int()` the predicate would then have to survive.
    if len(value) != len(_MAX_CANONICAL_DECIMAL):
        return within_marker_decimal_width(value)
    return value <= _MAX_CANONICAL_DECIMAL


def is_journal_id(value: object) -> bool:
    """Whether ``value`` is a CANONICAL positive decimal journal id, judged on the RAW value (pure).

    The shape half of the Review Generation Marker Contract v2's ``req=<review_request journal id>``
    — the sibling of ``review_return_route.is_full_commit_head``, which does the same job for
    ``head``. Added here (Redmine #14694 review j#93818 finding 2) rather than beside that head
    predicate purely for module-health headroom; it changes nothing about
    :func:`validate_marker_field_value`, whose four callers keep their behaviour exactly.

    Judged raw and LEXICALLY, never through ``int()`` (review j#93882 finding 2). Deferring the
    positivity test to ``int()`` was wrong twice over: it accepted ``"01"``, which is not the id
    Redmine owns (its REST journals are numeric resource ids — ``<journal id="1">`` — so ``req=01``
    cannot exact-match the request it claims to answer), and it RAISED on a 4301-digit input,
    because CPython caps int-from-str conversion at 4300 digits. A predicate that raises is not a
    predicate: the CLI boundary that owes its caller a typed refusal got an exception instead.

    So: a non-``str``, the empty string, a leading zero, any non-ASCII-decimal character, and
    anything wider than :func:`within_marker_decimal_width` are all refused, on the characters
    alone. ``" 93802 "``, ``"93 802"``, ``"abc"``, ``"93802=shadow"``, ``"-5"``, ``"1.5"``, ``"0"``
    and ``"01"`` are not journal ids. The producer and the CLI both ask THIS question, so "what a
    journal id looks like" has one answer.
    """
    return is_canonical_positive_decimal(value)


def require_journal_id(value: object, *, field: str = "req") -> str:
    """The RAW value as a journal id, or raise (pure). The producer twin of :func:`is_journal_id`."""
    if not is_journal_id(value):
        raise MarkerValueError(
            f"marker field {field!r} must be a canonical positive decimal journal id, got {value!r}"
        )
    return value  # the value ITSELF, never a rendering of it (review j#93882 finding 1)


def require_review_head(value: object, *, field: str = "head") -> str:
    """The RAW value as a review marker's full commit head, or raise (pure).

    Whitespace and marker punctuation are refused FIRST, so ``is_full_commit_head`` (which strips
    for the read side) is judging the raw value — the same order ``render_lane_envelope`` uses, and
    the same head predicate the CLI boundary asks, so "what a head looks like" has one answer
    (Redmine #14694 review j#93818 finding 1).
    """
    from .hibernate_evidence_envelope import require_marker_token
    from .review_return_route import is_full_commit_head

    token = require_marker_token(value, field=field, requirement="review marker")
    if not is_full_commit_head(token):
        raise MarkerValueError(f"marker field {field!r} must be a full commit head, got {value!r}")
    return token  # the value ITSELF, never a rendering of it (review j#93882 finding 1)


def review_marker_fields(
    *, conclusion=None, callback=None, target_head=None, review_request_journal=None
) -> "list[str]":
    """The validated ``conclusion`` / ``callback`` / ``head`` / ``req`` components (pure).

    Grouped here, beside the validators, rather than inline in the renderer: the two halves of the
    review-evidence contract stay together, and the reader module — already over the module-health
    threshold before this lane touched it — takes only the call (Redmine #14694 review j#93882
    finding 3). Returns the vocabulary pair and the anchor pair separately so the caller keeps the
    marker's field ORDER, which the byte pins depend on.
    """
    from .sublane_admission import CALLBACK_STATES, REVIEW_CONCLUSIONS

    parts = []
    for key, value, vocabulary in (
        ("conclusion", conclusion, REVIEW_CONCLUSIONS),
        ("callback", callback, CALLBACK_STATES),
    ):
        if value is not None:
            parts.append(f"{key}={require_vocabulary(value, field=key, vocabulary=vocabulary)}")
    return parts


def review_anchor_fields(*, target_head=None, review_request_journal=None) -> "list[str]":
    """The validated ``head`` / ``req`` components, in marker order (pure). See #13974's contract."""
    parts = []
    if target_head is not None:
        parts.append(f"head={require_review_head(target_head)}")
    if review_request_journal is not None:
        parts.append(f"req={require_journal_id(review_request_journal)}")
    return parts


def require_vocabulary(value: object, *, field: str, vocabulary) -> str:
    """The RAW value, or raise unless it is a ``str`` that is EXACTLY one of ``vocabulary`` (pure).

    Vocabulary rather than shape, because for these fields a value outside the closed set is not
    merely unreadable: ``glance_snapshot_source`` rewrites an unrecognized review conclusion to
    ``pending``, so rendering one writes a record the consumer reads as a DIFFERENT value
    (Redmine #14694 review j#93818 finding 1). The producer and the consumer read the same constant.

    The ``str`` check comes FIRST and the accepted value itself is returned (review j#93882 finding
    1). Testing membership and then rendering ``str(value)`` let those be two different values: an
    object whose ``__hash__`` / ``__eq__`` impersonate ``"approved"`` while ``__str__`` says
    ``"bogus"`` passed the vocabulary and wrote ``conclusion=bogus`` — which the strict reader then
    extracted as ``bogus``, and the consumer downgraded to ``pending``. Membership can only speak
    for the object it was asked about, so what is written must BE that object, not a rendering of
    it. Rejecting a plain ``int`` was never a type contract; it was membership happening to fail.
    """
    if not is_exact_str(value) or value not in vocabulary:
        raise MarkerValueError(
            f"marker field {field!r} must be one of {sorted(vocabulary)}, got {value!r}"
        )
    return value
