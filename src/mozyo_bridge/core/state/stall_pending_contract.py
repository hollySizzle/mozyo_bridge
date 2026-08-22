"""The closed contract for one stored pending-escalation row (Redmine #15855).

Split from :mod:`mozyo_bridge.core.state.stall_escalation` for the reason the module-health
gate exists (``vibes/docs/logics/module-health-gate.md``): the store had grown past the line
budget, and "what a valid row is" is a genuinely separable concern from "how rows are
persisted". Nothing here touches SQLite, and nothing here decides policy — it decides only
whether a set of values is admissible.

The contract has two halves, because the fields carry different kinds of risk:

- a per-field GRAMMAR (closed vocabularies, bounded identity tokens, digits-only ids), and
- a ROUTING INTEGRITY seal, which no per-field grammar can provide: ``issue`` is the target
  of an external Redmine write, and one legitimate issue id looks exactly like another.

Review j#110192 finding_1 reproduced both halves failing at once — a direct-DB rewrite that
redirected a gate write to issue 99999, a ``lane_id`` carrying an embedded newline that
fabricated a line in a journal body, a ``consecutive`` of ``-3``, and an operator-unsafe
``last_reason`` that surfaced verbatim in the status JSON.
"""

from __future__ import annotations

import hashlib
import re

from mozyo_bridge.core.state.stall_discovery_contract import checked_timestamp

# ======================================================================================
# The pending-escalation row contract (Redmine #15855; review j#110192 finding_1)
# ======================================================================================
#
# The discovery row was closed field by field over three rounds, and each round left the
# next field open. The pending row is closed as ONE contract instead, because its fields
# are not all the same kind of risk:
#
# - ``issue`` is not a rendered value at all — it is the TARGET of an external Redmine
#   write. A corrupted row redirected a real gate write to issue 99999 in the reproduction.
# - ``lane_id`` and friends are interpolated into a journal BODY, so a newline in one
#   fabricates a line in a durable record.
# - ``stall_class`` / ``prescription`` / ``last_reason`` are rendered tokens.
#
# So the contract has two halves: a per-field grammar, and a ROUTING INTEGRITY check that
# no per-field grammar can provide (issue 99999 is a perfectly valid issue id).

#: Identity tokens: a leading non-hyphen, then word characters and hyphens. The leading
#: character matters — a value starting with ``-`` can be read as an argv flag by anything
#: that later builds a command from it (the lane-id vocabulary decision recorded in #15844).
IDENTITY_PATTERN = r"[A-Za-z0-9_][A-Za-z0-9_.:-]*"
IDENTITY_MAX_LENGTH = 128

#: A Redmine issue / journal id: digits only. Bounded so a row cannot carry a long numeric
#: blob into a write target.
NUMERIC_ID_PATTERN = r"[0-9]+"
NUMERIC_ID_MAX_LENGTH = 12

#: The canonical shape :func:`escalation_idempotency_key` produces.
IDEMPOTENCY_KEY_PATTERN = r"stallesc1_[0-9a-f]{32}"

#: ``writer_raised_<ExceptionType>`` is generated from a live exception class name, so it
#: cannot be enumerated — but it is still closed in shape.
WRITER_RAISED_PATTERN = r"writer_raised_[A-Za-z_][A-Za-z0-9_]*"

#: Every ``last_reason`` this rail can legitimately record. Anything else is refused rather
#: than rendered: the reproduction put ``/private/example/operator-unsafe-reason`` straight
#: into the status JSON.
PENDING_REASONS: frozenset[str] = frozenset(
    {
        "write_optin_unset", "base_url_unset", "credential_missing", "unauthorized",
        "no_anchor", "disabled", "unsupported_source", "transport_error",
        "readback_unverified", "already_recorded", "recorded",
        "write_refused", "write_uncertain", "issue_anchor_unresolved",
        "external_mutation_budget_spent", "nothing_pending",
        # The sentinel the store substitutes for a reason it does not recognise. It is a
        # member of this set because a value the store WRITES must be a value the store can
        # read back -- otherwise the substitution quarantines the very row it was protecting.
        "unclassified_reason",
    }
)

#: Declared in this store rather than imported from the policy layer, for the same reason
#: :data:`DISCOVERY_DROP_REASONS` is: a state store that can reach the rules invites a rule
#: to be written in it. Bidirectional equality with the policy vocabularies is enforced by
#: test, so a value added on one side and not the other fails loudly.
PENDING_STALL_CLASSES: frozenset[str] = frozenset(
    {
        "screen_progressing", "busy_likely", "startup_interaction", "content_refusal",
        "unsent_composer", "provider_unresponsive_suspected",
        "unresponsive_indeterminate", "screen_unreadable", "unknown",
    }
)

PENDING_PRESCRIPTIONS: frozenset[str] = frozenset(
    {
        "no_action", "patient_wait_then_retry", "enter_only_retry",
        "context_reset_reinjection", "operator_resolves_startup_screen",
        "owner_escalation",
    }
)

PENDING_EVIDENCE_TIERS: frozenset[str] = frozenset(
    {"rendered_confirmed", "binary_read_unrendered"}
)

#: Integrity verdicts for a stored pending row.
PENDING_OK = "ok"
#: The row's own fields no longer derive its stored idempotency key, so at least one of the
#: identity/routing facts was changed after it was written. The row is KEPT (the escalation
#: happened) but is never handed to an external writer or a wake.
PENDING_ROUTING_MISMATCH = "routing_binding_mismatch"
#: A field violates its grammar. Same disposition: preserved, never externally actuated.
PENDING_FIELD_INVALID = "field_grammar_violation"


#: Rendered in place of a stored field that violates its grammar. The offending value is
#: never echoed -- a status surface is exactly where the reproduction's
#: ``/private/example/operator-unsafe-reason`` and ``rm -rf /`` came out.
PENDING_UNRENDERABLE = "unrenderable"


class StallPendingContractError(ValueError):
    """A pending row violated the closed contract. Raised at the WRITE boundary only."""


def _pattern(name: str, value: object, *, pattern: str, limit: int, allow_empty: bool) -> str:
    text = "" if value is None else str(value)
    if not text:
        if allow_empty:
            return ""
        raise StallPendingContractError(f"pending {name} must not be empty")
    if len(text) > limit:
        raise StallPendingContractError(
            f"pending {name} exceeds {limit} characters"
        )
    if re.fullmatch(pattern, text) is None:
        # `fullmatch`, never `match` + `$`: Python's `$` also matches before a trailing
        # newline, which is exactly the character this check exists to exclude (#15844).
        # The offending value is not quoted -- an error message is read by an operator.
        raise StallPendingContractError(
            f"pending {name} does not match the declared grammar"
        )
    return text


def checked_identity(value: object, *, name: str, allow_empty: bool = False) -> str:
    """An identity token: bounded, no control characters, no leading hyphen."""
    return _pattern(
        name, value, pattern=IDENTITY_PATTERN, limit=IDENTITY_MAX_LENGTH,
        allow_empty=allow_empty,
    )


def checked_numeric_id(value: object, *, name: str, allow_empty: bool = False) -> str:
    """A Redmine issue / journal id: digits only, bounded."""
    return _pattern(
        name, value, pattern=NUMERIC_ID_PATTERN, limit=NUMERIC_ID_MAX_LENGTH,
        allow_empty=allow_empty,
    )


def checked_member(value: object, *, name: str, vocabulary: frozenset) -> str:
    """A value drawn from a closed set. The value is never echoed on refusal."""
    text = "" if value is None else str(value)
    if text not in vocabulary:
        raise StallPendingContractError(
            f"pending {name} is outside the declared vocabulary of "
            f"{len(vocabulary)} value(s)"
        )
    return text


def checked_reason(value: object, *, name: str = "last_reason") -> str:
    """A recorded failure reason: a declared token, or ``writer_raised_<ExceptionType>``."""
    text = "" if value is None else str(value)
    if not text:
        return ""
    if text in PENDING_REASONS:
        return text
    if re.fullmatch(WRITER_RAISED_PATTERN, text) is not None:
        return text
    raise StallPendingContractError(
        f"pending {name} is neither a declared reason nor a writer_raised_<Type> token"
    )


def escalation_idempotency_key(
    *,
    workspace_id: str,
    lane_id: str,
    role: str,
    generation: str,
    stall_class: str,
    first_observed_at: str,
    issue: str = "",
) -> str:
    """The stable key identifying ONE firing of ONE streak.

    Derived from the run's own identity rather than from the firing pass's clock, so a
    crash-and-retry of the same firing collides and a genuinely different run does not.
    ``first_observed_at`` is what separates two runs of the same class on the same slot and
    generation: the policy layer restarts ``first_observed_at`` on every restart, so two
    runs separated by a reset produce two keys while one run retried across a crash keeps
    producing the same one.

    Deliberately NOT derived from ``escalated_at``: that moves on every pass, which would
    make each retry look like a new escalation and write a duplicate Redmine journal — the
    exact failure the readback fence exists to prevent.

    ``issue`` participates because the key doubles as a **routing integrity** seal. The
    issue is not a rendered value — it is the target of an external Redmine write — and no
    per-field grammar can tell a legitimate issue id from a different legitimate issue id.
    Binding it into the key means a row whose issue was altered after it was written no
    longer derives its own key, which is detectable on read (review j#110192 finding_1).
    """
    digest = hashlib.sha256(
        "\x1f".join(
            (
                workspace_id,
                lane_id,
                role,
                generation,
                stall_class,
                first_observed_at,
                issue,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"stallesc1_{digest[:32]}"


#: Recorded in place of a reason the contract does not know. Distinct from every declared
#: reason, so "the writer reported something unrecognised" is not silently indistinguishable
#: from "the writer reported nothing".
UNCLASSIFIED_REASON = "unclassified_reason"

#: Counts are bounded on both sides. A negative count is not merely odd: ``consecutive``
#: below 1 makes a row unreachable by every ``>= threshold`` comparison while it keeps
#: occupying the slot, and ``attempts`` below 0 reads as "tried less than never", which
#: erases a refusal history rather than reporting one (review j#110218).
COUNT_MAX = 100_000


def checked_count(value: object, *, name: str, minimum: int) -> int:
    """A stored count as an int, or a refusal. Never a silent coercion.

    Conversion happens HERE rather than at the row boundary so that a non-numeric stored
    value becomes a typed verdict instead of a ``ValueError`` escaping a read surface —
    including :meth:`quarantined_pending`, the surface whose whole job is to make corrupted
    rows visible (review j#110218).
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise StallPendingContractError(f"pending {name} is not a count")
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise StallPendingContractError(f"pending {name} is not an integer") from None
    if count < minimum or count > COUNT_MAX:
        raise StallPendingContractError(
            f"pending {name} must be between {minimum} and {COUNT_MAX}"
        )
    return count


def _key_checker(value: object) -> str:
    if re.fullmatch(IDEMPOTENCY_KEY_PATTERN, str(value or "")) is None:
        raise StallPendingContractError("pending idempotency_key is not canonical")
    return str(value)


def _identity(name: str, *, allow_empty: bool = False):
    return lambda v: checked_identity(v, name=name, allow_empty=allow_empty)


def _numeric(name: str):
    return lambda v: checked_numeric_id(v, name=name, allow_empty=True)


def _member(name: str, vocabulary, *, allow_empty: bool = False):
    def check(v):
        if allow_empty and not str(v or ""):
            return ""
        return checked_member(v, name=name, vocabulary=vocabulary)

    return check


def _instant(field: str, *, allow_empty: bool = False):
    def check(v):
        text = "" if v is None else str(v)
        if allow_empty and not text:
            return ""
        try:
            return checked_timestamp(text, field=field)
        except Exception:  # noqa: BLE001 - re-typed; the discovery error is not ours
            raise StallPendingContractError(f"pending {field} is not a valid instant") from None

    return check


def _count(name: str, *, minimum: int):
    return lambda v: checked_count(v, name=name, minimum=minimum)


#: EVERY persisted column of a pending row, and the grammar it must satisfy.
#:
#: A table rather than a hand-written sequence of checks, because the previous shape made
#: the same mistake five rounds running: each review named a field, the field got a check,
#: and the fields nobody had named yet stayed open. Round six found the whole *category* of
#: persistence-state columns (``journal_id`` / ``written_at`` / ``woke_at`` / ``attempts`` /
#: ``last_attempt_at``) unvalidated, immediately after the previous round declared the row
#: "closed" — the columns were skipped because they are the ones this store writes itself,
#: which forgets that a store is not a trust boundary.
#:
#: The completeness of this table against :class:`PendingEscalation` is asserted by test, so
#: a new column that is added without a grammar fails loudly instead of quietly joining the
#: unvalidated set.
PENDING_FIELD_CHECKERS = {
    # --- identity and routing: sealed into the idempotency key --------------------
    "idempotency_key": _key_checker,
    "workspace_id": _identity("workspace_id"),
    "lane_id": _identity("lane_id"),
    "role": _identity("role"),
    "generation": _identity("generation", allow_empty=True),
    "target": _identity("target", allow_empty=True),
    "issue": _numeric("issue"),
    # --- what the stall was ------------------------------------------------------
    "stall_class": _member("stall_class", PENDING_STALL_CLASSES),
    "prescription": _member("prescription", PENDING_PRESCRIPTIONS),
    "matched_id": _identity("matched_id", allow_empty=True),
    "evidence_tier": _member("evidence_tier", PENDING_EVIDENCE_TIERS, allow_empty=True),
    "consecutive": _count("consecutive", minimum=1),
    "first_observed_at": _instant("observed_at.first"),
    "escalated_at": _instant("observed_at.escalated"),
    # --- persistence state: how far this firing got ------------------------------
    # `journal_id` is the strongest case in this group. It is not decoration: a row that
    # carries one is treated as WRITTEN, and a wake settles the escalation against it. A
    # non-canonical value therefore settles a firing against a journal nobody read back.
    "journal_id": _numeric("journal_id"),
    "written_at": _instant("observed_at.written", allow_empty=True),
    "woke_at": _instant("observed_at.woke", allow_empty=True),
    "attempts": _count("attempts", minimum=0),
    "last_attempt_at": _instant("observed_at.attempt", allow_empty=True),
    "last_reason": checked_reason,
}


def validate_pending_fields(pending, *, first_observed_at: str = "") -> dict:
    """Hold an outbound pending row to the whole-row contract, or refuse to store it.

    Raises :class:`StallPendingContractError`; the caller's ``_mutate`` turns that into a
    refused write rather than a corrupted row. Returns the validated values so the INSERT
    binds *these* rather than the originals — a validator whose result the caller can
    forget to use is a validator that will eventually be bypassed.

    ``first_observed_at`` may be passed pre-normalized by a caller that already needed the
    normalized instant; it is validated either way.
    """
    values = {}
    for name, checker in PENDING_FIELD_CHECKERS.items():
        raw = first_observed_at if (name == "first_observed_at" and first_observed_at) else getattr(pending, name)
        values[name] = checker(raw)
    return values


def rendered_field(value: object, checker) -> str:
    """A stored field as it may be shown, or :data:`PENDING_UNRENDERABLE`.

    Field-by-field rather than all-or-nothing on purpose: an operator looking at a
    quarantined row still needs to see WHICH field is wrong, and blanking the whole row
    would hide that as effectively as echoing it would leak.
    """
    try:
        return checker(value)
    except StallPendingContractError:
        return PENDING_UNRENDERABLE


def pending_row_integrity(pending: "PendingEscalation") -> str:
    """Classify a row read back out of the store: OK, bad grammar, or bad routing.

    The two failures are separate on purpose. A grammar violation is a value that could
    never have been written; a ROUTING mismatch is a set of values that are each perfectly
    legal but no longer derive the key they are stored under — the shape of the
    reproduction that redirected a gate write to issue 99999, where every individual field
    passed inspection.
    """
    try:
        validate_pending_fields(pending)
    except StallPendingContractError:
        return PENDING_FIELD_INVALID
    expected = escalation_idempotency_key(
        workspace_id=pending.workspace_id,
        lane_id=pending.lane_id,
        role=pending.role,
        generation=pending.generation,
        stall_class=pending.stall_class,
        first_observed_at=pending.first_observed_at,
        issue=pending.issue,
    )
    # Constant-time comparison is not the point here (this is a local corruption check, not
    # an authentication check) -- an ordinary compare is what a reader should expect.
    if expected != pending.idempotency_key:
        return PENDING_ROUTING_MISMATCH
    return PENDING_OK


def canonical_journal_id(value: object) -> bool:
    """Whether a stored ``journal_id`` names a real Redmine journal: digits, and only digits.

    Mirrored in SQL by :meth:`StallEscalationStore.mark_woken` so the fence holds whether
    the caller asks in Python or the UPDATE runs on its own (review j#110218).
    """
    text = str(value or "")
    return bool(text) and text.isdigit()


def pending_telemetry(row) -> dict:
    """Project one stored row for an operator surface, field by field.

    Every field passes through its own grammar on the way OUT, not just on the way in. The
    store is not a trust boundary: a row can be altered after it was written, and a
    projection that trusts what it read is how ``rm -rf /`` and an absolute private path
    reached the status JSON in the review j#110192 finding_1 reproduction.

    Nothing is dropped for being invalid — an invalid field renders as
    :data:`PENDING_UNRENDERABLE`, so "this row has a bad prescription" stays legible while
    the bad prescription itself does not. Blanking the whole row would hide WHICH field is
    wrong as effectively as echoing it would leak.

    The checkers are :data:`PENDING_FIELD_CHECKERS`, the same table the write boundary
    uses. One table, both directions: a field cannot acquire a grammar on entry and quietly
    keep rendering raw on exit.
    """
    check = PENDING_FIELD_CHECKERS
    payload: dict = {
        "idempotency_key": rendered_field(row.idempotency_key, check["idempotency_key"]),
        "slot": "/".join(
            (
                rendered_field(row.workspace_id, check["workspace_id"]),
                rendered_field(row.lane_id, check["lane_id"]),
                rendered_field(row.role, check["role"]),
            )
        ),
        "stall_class": rendered_field(row.stall_class, check["stall_class"]),
        "prescription": rendered_field(row.prescription, check["prescription"]),
        "consecutive": rendered_field(row.consecutive, check["consecutive"]),
        "first_observed_at": row.first_observed_at,
        "escalated_at": row.escalated_at,
        "recorded": row.recorded,
        "settled": row.settled,
        "attempts": rendered_field(row.attempts, check["attempts"]),
        # Always present, including on a healthy row: an operator reading a status payload
        # should not have to know that a MISSING key would have meant trouble.
        "integrity": row.integrity,
    }
    for name in (
        "generation", "target", "issue", "matched_id", "evidence_tier",
        "journal_id", "last_reason",
    ):
        value = getattr(row, name)
        if value:
            payload[name] = rendered_field(value, check[name])
    return payload


__all__ = (
    "COUNT_MAX",
    "PENDING_FIELD_CHECKERS",
    "IDEMPOTENCY_KEY_PATTERN",
    "IDENTITY_MAX_LENGTH",
    "IDENTITY_PATTERN",
    "NUMERIC_ID_MAX_LENGTH",
    "NUMERIC_ID_PATTERN",
    "PENDING_EVIDENCE_TIERS",
    "PENDING_FIELD_INVALID",
    "PENDING_OK",
    "PENDING_PRESCRIPTIONS",
    "PENDING_REASONS",
    "PENDING_ROUTING_MISMATCH",
    "PENDING_STALL_CLASSES",
    "PENDING_UNRENDERABLE",
    "UNCLASSIFIED_REASON",
    "WRITER_RAISED_PATTERN",
    "StallPendingContractError",
    "checked_identity",
    "checked_member",
    "checked_numeric_id",
    "checked_reason",
    "escalation_idempotency_key",
    "canonical_journal_id",
    "checked_count",
    "pending_row_integrity",
    "pending_telemetry",
    "rendered_field",
    "validate_pending_fields",
)
