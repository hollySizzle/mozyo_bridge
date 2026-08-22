"""The closed contract for one stored pending-escalation row (Redmine #15855).

Split from :mod:`mozyo_bridge.core.state.stall_escalation` for the reason the module-health
gate exists (``vibes/docs/logics/module-health-gate.md``): the store had grown past the line
budget, and "what a valid row is" is a genuinely separable concern from "how rows are
persisted". Nothing here touches SQLite, and nothing here decides policy — it decides only
whether a set of values is admissible.

The contract has three halves, because the fields carry different kinds of risk:

- a per-field GRAMMAR (closed vocabularies, bounded identity tokens, digits-only ids),
- a ROUTING INTEGRITY seal, which no per-field grammar can provide: ``issue`` is the target
  of an external Redmine write, and one legitimate issue id looks exactly like another, and
- an AUTHORITY classification, which says who is allowed to vouch for each column, because
  a grammar proves SHAPE and can never prove EXISTENCE (review j#110254).

Review j#110192 finding_1 reproduced the first two failing at once — a direct-DB rewrite
that redirected a gate write to issue 99999, a ``lane_id`` carrying an embedded newline that
fabricated a line in a journal body, a ``consecutive`` of ``-3``, and an operator-unsafe
``last_reason`` that surfaced verbatim in the status JSON. Review j#110254 then reproduced
the third: a canonical-shaped ``journal_id='999999'`` settled a firing, and the settled row
vanished from both the open inventory and the quarantine surface, with Redmine never asked.

Everything about one stored row lives in this ONE module on purpose. The three failures
that cost this issue rounds six, seven and eight were all the same shape — a rule declared
in one place and re-implemented, partially, somewhere else — so splitting "what a valid row
is" from "who vouches for it" would rebuild the exact seam that keeps breaking.
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
        # The writer's own deterministic refusal when the pass has spent its provider-read
        # budget and the idempotency check therefore could not run (review j#110281
        # finding_readcap). Spelled here rather than imported, for the same reason the stall
        # classes are: a state store that can reach the application layer invites a rule to
        # be written in it. `READBACK_CAPPED` must equal this literal, and a test asserts it
        # — a reason that drifts silently becomes `unclassified_reason`, which is exactly
        # the field an operator reads to learn WHY nothing is being written.
        "read_cap_reached",
        # The writer's refusal when several journals claim one firing: nobody can say which
        # is the record, so the firing stays pending and visible rather than being bound to
        # a guess (review j#110293 finding_authorityforgery). Same equality-by-test rule as
        # `read_cap_reached` above.
        "ambiguous_authority",
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

#: The shape :func:`pending_row_seal` produces. A distinct prefix from the idempotency key
#: so the two can never be mistaken for one another in a row or a log line.
ROW_SEAL_PATTERN = r"stallst1_[0-9a-f]{32}"

#: Integrity verdicts for a stored pending row.
PENDING_OK = "ok"
#: The row's own fields no longer derive its stored idempotency key, so at least one of the
#: identity/routing facts was changed after it was written. The row is KEPT (the escalation
#: happened) but is never handed to an external writer or a wake.
PENDING_ROUTING_MISMATCH = "routing_binding_mismatch"
#: A field violates its grammar. Same disposition: preserved, never externally actuated.
PENDING_FIELD_INVALID = "field_grammar_violation"
#: The row's non-identity columns no longer derive their stored seal, so something the store
#: wrote was rewritten afterwards. Same disposition — and this is the
#: verdict that makes a fully-forged SETTLED row visible: a row whose ``journal_id`` and
#: ``woke_at`` were both rewritten satisfies every lifecycle predicate and every grammar,
#: and before the seal existed it appeared in neither the open inventory nor the quarantine
#: surface (review j#110254 finding_stateauthority).
PENDING_STATE_MISMATCH = "row_binding_mismatch"


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


#: The columns the idempotency key already seals, in the order it hashes them. Rewrite any
#: one of them and the row stops deriving the key it is stored under.
IDENTITY_SEAL_FIELDS: tuple[str, ...] = (
    "workspace_id", "lane_id", "role", "generation", "stall_class", "first_observed_at",
    "issue",
)

#: EVERY OTHER column, sealed by :func:`pending_row_seal`.
#:
#: "Every other" rather than "the persistence-state ones" is the point, and it is rounds six
#: through eight compressed into one tuple. Each of those rounds sealed the columns whose
#: risk had just been demonstrated and left the rest; each time, the next round's finding was
#: a column from the rest — first ``issue``, then the five state columns, then ``journal_id``.
#: A ``prescription`` rewritten from ``patient_wait_then_retry`` to ``owner_escalation`` is
#: just as legal, just as invisible to a grammar, and reaches a durable Redmine journal just
#: the same. So the partition is closed by construction and asserted by test: every stored
#: column is in the key, in this seal, or IS one of the two derived columns.
ROW_SEAL_FIELDS: tuple[str, ...] = (
    "target", "prescription", "matched_id", "evidence_tier", "consecutive", "escalated_at",
    "journal_id", "written_at", "woke_at", "attempts", "last_attempt_at", "last_reason",
)


def pending_row_seal(*, idempotency_key: str, values) -> str:
    """Derive the tamper-evidence seal over every column the identity key does not cover.

    Bound to ``idempotency_key`` so a seal cannot be lifted from one row onto another: the
    settled state of a firing that really did reach a coordinator would otherwise be
    copyable onto a firing that never did.

    What this proves, and what it does not — stated plainly, because overstating exactly
    this kind of check is what cost rounds six and seven. It detects columns rewritten
    WITHOUT recomputing the seal. It is not a secret, and it is **not an existence proof**:
    a seal says the store derived these values, never that the journal they name exists. An
    attacker who also recomputes it is the capability deferred in j#110245 / j#110218 (store
    write access plus full recomputation). The wake admission therefore does not consult
    this seal for existence — it asks Redmine (:func:`admit_wake`).
    """
    digest = hashlib.sha256(
        "\x1f".join(
            (str(idempotency_key), *(str(values.get(name, "")) for name in ROW_SEAL_FIELDS))
        ).encode("utf-8")
    ).hexdigest()
    return f"stallst1_{digest[:32]}"


def row_seal_for(row) -> str:
    """The seal a row's own columns derive.

    Derived from the row at the WRITE boundary and never accepted from a caller: a seal a
    caller could supply would seal nothing.
    """
    return pending_row_seal(
        idempotency_key=str(getattr(row, "idempotency_key", "")),
        values={name: getattr(row, name, "") for name in ROW_SEAL_FIELDS},
    )


def checked_row_seal(value: object, *, name: str = "row_seal") -> str:
    """The seal's own grammar. Whether it DERIVES the row is a separate check, on read."""
    text = "" if value is None else str(value)
    if re.fullmatch(ROW_SEAL_PATTERN, text) is None:
        raise StallPendingContractError(f"pending {name} is not a canonical row seal")
    return text


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
    "row_seal": checked_row_seal,
}


# ======================================================================================
# Authority classification: who is allowed to vouch for each column
# ======================================================================================
#
# The grammar table above answers "is this value admissible". It cannot answer "is this
# value TRUE", and three rounds of this issue were lost to not separating the two. So every
# column is also classified by what kind of fact it holds, and each class names the one
# mechanism entitled to vouch for it. The classification is machine-checked against the
# stored row AND against the mechanisms, so a field declared `identity_component` that does
# not actually change the idempotency key fails a test rather than a review round.

#: Sealed into the idempotency key: change one and the row stops deriving its own key.
FIELD_CLASS_IDENTITY = "identity_component"
#: Sealed into the state seal: this store's own record of how far a firing got.
FIELD_CLASS_STATE = "persistence_state"
#: Names a record in an EXTERNAL system. Nothing local can vouch for it; see
#: :data:`EXTERNAL_REFERENCE_AUTHORITY` for where the external system is actually asked.
FIELD_CLASS_EXTERNAL = "external_record_reference"
#: Neither routes nor asserts anything: grammar plus the render boundary is the whole story.
FIELD_CLASS_RENDERED = "rendered_value"

FIELD_CLASSES: frozenset[str] = frozenset(
    {FIELD_CLASS_IDENTITY, FIELD_CLASS_STATE, FIELD_CLASS_EXTERNAL, FIELD_CLASS_RENDERED}
)

#: Every stored column, and what kind of fact it holds.
#:
#: ``issue`` and ``journal_id`` are BOTH external references, and they are the two fields
#: this issue lost a round to, one after the other (j#110192 finding_1, then j#110254
#: finding_stateauthority). They differ only in WHERE their authority is consulted, which is
#: why that site is declared per field below instead of assumed.
PENDING_FIELD_CLASSES: dict[str, str] = {
    "idempotency_key": FIELD_CLASS_IDENTITY,
    "workspace_id": FIELD_CLASS_IDENTITY,
    "lane_id": FIELD_CLASS_IDENTITY,
    "role": FIELD_CLASS_IDENTITY,
    "generation": FIELD_CLASS_IDENTITY,
    "stall_class": FIELD_CLASS_IDENTITY,
    "first_observed_at": FIELD_CLASS_IDENTITY,
    "issue": FIELD_CLASS_EXTERNAL,
    "journal_id": FIELD_CLASS_EXTERNAL,
    "written_at": FIELD_CLASS_STATE,
    "woke_at": FIELD_CLASS_STATE,
    "attempts": FIELD_CLASS_STATE,
    "last_attempt_at": FIELD_CLASS_STATE,
    "last_reason": FIELD_CLASS_STATE,
    "row_seal": FIELD_CLASS_STATE,
    "target": FIELD_CLASS_RENDERED,
    "prescription": FIELD_CLASS_RENDERED,
    "matched_id": FIELD_CLASS_RENDERED,
    "evidence_tier": FIELD_CLASS_RENDERED,
    "consecutive": FIELD_CLASS_RENDERED,
    "escalated_at": FIELD_CLASS_RENDERED,
}

#: Where the external system is actually asked. An ``external_record_reference`` field with
#: no entry here is a field whose existence NOBODY checks — precisely the state
#: ``journal_id`` was in for two rounds while carrying a grammar that looked like rigour.
#:
#: - ``issue`` is checked by the write itself: a journal cannot be posted to an issue that
#:   does not exist, so the external system answers at the moment of use — and a redirected
#:   issue additionally breaks the idempotency key, which it is sealed into.
#: - ``journal_id`` is checked at the WAKE admission by exact readback, because nothing else
#:   in this rail ever consults it: the stored id is what a woken coordinator is told to go
#:   read, so the wake is the entire external effect a fabricated id buys.
EXTERNAL_REFERENCE_AUTHORITY: dict[str, str] = {
    "issue": "write_admission",
    "journal_id": "wake_admission",
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
    if row_seal_for(pending) != pending.row_seal:
        return PENDING_STATE_MISMATCH
    return PENDING_OK


def canonical_journal_id(value: object) -> bool:
    """Whether a stored ``journal_id`` has the shape of a Redmine journal id.

    DERIVED from :data:`PENDING_FIELD_CHECKERS`, not a second implementation of it. The
    previous version was a hand-written ``str.isdigit()``, which drifted from the table in
    two directions at once: it admitted the 13-character ``1234567890123`` (the table's
    12-character bound was missing) and it admitted non-ASCII digits (``str.isdigit()`` is
    true for Arabic-Indic digits, which ``[0-9]`` is not). Review j#110254
    finding_checkerdrift found the first; the second came free with the same mistake.

    Note what this function does NOT claim, since claiming it is what cost round seven: a
    canonical shape is not an existing journal. ``999999`` passes here and names nothing.
    Existence is :func:`admit_wake`'s question, and only Redmine can answer it.
    """
    try:
        PENDING_FIELD_CHECKERS["journal_id"](value)
    except StallPendingContractError:
        return False
    return bool(str(value or ""))


def canonical_idempotency_key(value: object) -> bool:
    """Whether a value has the shape :func:`escalation_idempotency_key` produces.

    DERIVED from :data:`PENDING_FIELD_CHECKERS`, for the same reason
    :func:`canonical_journal_id` is. The external-authority readback uses it to refuse a
    request outright rather than compare it: a caller must not be able to go fishing with a
    prefix, and "is this even a key" is not a question that deserves a second implementation.
    """
    try:
        PENDING_FIELD_CHECKERS["idempotency_key"](value)
    except StallPendingContractError:
        return False
    return True


#: The one place the digit alphabet is written for the SQL side.
_SQL_DIGIT_CLASS = "[0-9]"


def canonical_numeric_id_sql(column: str) -> str:
    """A SQL predicate accepting exactly what :func:`canonical_journal_id` accepts.

    ``GLOB '[0-9]*'`` alone admits ``110200x``; adding the negated class still admitted the
    13-digit id the bound exists to refuse. Both halves and the bound now come from the same
    constants the Python checker uses, and the equivalence is asserted over a corpus by test
    rather than argued here — an argued equivalence is exactly what drifted.
    """
    name = checked_identity(column, name="sql column")
    return (
        f"{name} GLOB '{_SQL_DIGIT_CLASS}*' "
        f"AND NOT {name} GLOB '*[^0-9]*' "
        f"AND LENGTH({name}) BETWEEN 1 AND {NUMERIC_ID_MAX_LENGTH}"
    )


#: Admitted: the external authority answered with exactly the stored journal id.
WAKE_ADMITTED = ""
#: The row failed the stored-row contract, so nothing about it may drive an effect.
WAKE_ROW_QUARANTINED = "row_quarantined"
#: The stored id is not even shaped like a journal id.
WAKE_JOURNAL_NOT_CANONICAL = "journal_not_canonical"
#: The authority was not consulted, or could not answer. Fail-closed: no wake.
WAKE_JOURNAL_UNVERIFIED = "journal_unverified"
#: The authority answered with a DIFFERENT journal id than the row claims.
WAKE_JOURNAL_MISMATCH = "journal_mismatch"

WAKE_REFUSALS: frozenset[str] = frozenset(
    {
        WAKE_ROW_QUARANTINED,
        WAKE_JOURNAL_NOT_CANONICAL,
        WAKE_JOURNAL_UNVERIFIED,
        WAKE_JOURNAL_MISMATCH,
    }
)


def admit_wake(pending, observed_journal_id: object) -> str:
    """Whether a coordinator may be woken for this row; ``""`` admits, else a refusal token.

    ``observed_journal_id`` is what the EXTERNAL system says carries this firing — the id
    found by matching the firing's own idempotency key in the issue's journals. It is not
    the row's opinion of itself, which is the whole point: a row claiming
    ``journal_id='999999'`` is what this admission exists to refuse.

    Fail-closed on an unanswered authority. A wake that cannot be justified is skipped and
    retried next pass; the other direction tells a coordinator to go read a journal that
    does not exist, which is the failure this rail's readback fence was built to prevent.

    ONE admission for both callers — the row recorded on an earlier pass and the row
    recorded moments ago in the same pass. Two admissions would be two implementations of
    one rule, and this issue has now lost three rounds to exactly that.
    """
    if not getattr(pending, "externally_writable", False):
        return WAKE_ROW_QUARANTINED
    stored = str(getattr(pending, "journal_id", "") or "")
    if not canonical_journal_id(stored):
        return WAKE_JOURNAL_NOT_CANONICAL
    observed = "" if observed_journal_id is None else str(observed_journal_id)
    if not observed:
        return WAKE_JOURNAL_UNVERIFIED
    if observed != stored:
        return WAKE_JOURNAL_MISMATCH
    return WAKE_ADMITTED


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

    EVERY column goes through the table. The two instants did not, for one round — they were
    assigned straight from the row in the same commit that introduced the table, so a row
    that reached this function without passing through the store's own reader rendered
    ``/etc/shadow`` as an ``escalated_at`` (review j#110254 finding_checkerdrift, face c).
    :data:`PROJECTION_DERIVED_FIELDS` names the only columns that are deliberately absent,
    and a test asserts every other column is present and renderable-checked.
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
        "first_observed_at": rendered_field(row.first_observed_at, check["first_observed_at"]),
        "escalated_at": rendered_field(row.escalated_at, check["escalated_at"]),
        "recorded": row.recorded,
        "settled": row.settled,
        "attempts": rendered_field(row.attempts, check["attempts"]),
        # Always present, including on a healthy row: an operator reading a status payload
        # should not have to know that a MISSING key would have meant trouble.
        "integrity": row.integrity,
    }
    for name in (
        "generation", "target", "issue", "matched_id", "evidence_tier",
        "journal_id", "written_at", "woke_at", "last_attempt_at", "last_reason",
    ):
        value = getattr(row, name)
        if value:
            payload[name] = rendered_field(value, check[name])
    return payload


#: Columns deliberately absent from :func:`pending_telemetry`, and why.
#:
#: ``row_seal`` is a derivation of the other columns and carries no fact of its own;
#: what an operator needs from it is the verdict, which is ``integrity``. Listing the
#: exclusions rather than leaving them implicit is what lets the completeness test be a
#: statement about ALL columns.
PROJECTION_DERIVED_FIELDS: frozenset[str] = frozenset({"row_seal"})


__all__ = (
    "COUNT_MAX",
    "EXTERNAL_REFERENCE_AUTHORITY",
    "FIELD_CLASSES",
    "FIELD_CLASS_EXTERNAL",
    "FIELD_CLASS_IDENTITY",
    "FIELD_CLASS_RENDERED",
    "FIELD_CLASS_STATE",
    "PENDING_FIELD_CHECKERS",
    "PENDING_FIELD_CLASSES",
    "PENDING_STATE_MISMATCH",
    "PROJECTION_DERIVED_FIELDS",
    "IDENTITY_SEAL_FIELDS",
    "ROW_SEAL_FIELDS",
    "ROW_SEAL_PATTERN",
    "WAKE_ADMITTED",
    "WAKE_JOURNAL_MISMATCH",
    "WAKE_JOURNAL_NOT_CANONICAL",
    "WAKE_JOURNAL_UNVERIFIED",
    "WAKE_REFUSALS",
    "WAKE_ROW_QUARANTINED",
    "admit_wake",
    "canonical_idempotency_key",
    "canonical_numeric_id_sql",
    "checked_row_seal",
    "pending_row_seal",
    "row_seal_for",
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
