"""The versioned stable idempotency key for one recovery action, and its durable wire format (#13910).

Design answer j#80984 (Option A + C), reconciled as authoritative by j#80986.

The sender (:mod:`..application.callback_sweep`) is at-most-once *per dispatch anchor*, but its last
live read cannot be made atomic with the transport call — Redmine offers no CAS — so a recovery can
land at a receiver that has already advanced (#13889 R5-F3: ``sent=True`` while the qualifying gate
already existed). This module defines the identity the **receiver** admits against, so that window is
absorbed on the receiving side rather than re-argued on the sending side.

**The key is derived, never asserted.** Its fields come from a marker the sweep wrote into the
durable recovery record, plus that record's own journal id — read fresh from Redmine at admission
time. Pane prose is never a source (j#80984 Disposition 1): the notification is a pointer, and the
durable record is the truth (``vibes/docs/logics/ack-completion-receiver-state.md`` ``## 運用への帰結``
6).

**Why the journal id is not in the marker.** Redmine's note write returns ``204 No Content``, so the
writer cannot learn where its own record landed — a self-reported id would be a claim, not a fact.
``recovery_action_journal`` is therefore the **owning entry's** id, resolved by reading the record
back, exactly as :func:`..domain.callback_sweep_watermark.sweep_record_journals` resolves the record
itself. The marker carries the eight facts the writer genuinely knows.

**Why length-prefixed canonical encoding.** A key is only an identity if distinct field tuples cannot
encode identically. Plain delimiter joining does not give that: ``lane="a"`` + ``route="b:c"`` and
``lane="a:b"`` + ``route="c"`` collapse to the same string, so two different recovery actions would
share one digest and the second would be silently no-op'd as a "duplicate". Every field is therefore
emitted as ``name=<len>:<value>``, which is injective for any value whatsoever, under a domain tag
that keeps this key space disjoint from every sibling authority's.

**Why render-time validation is fail-closed.** The wire format is the ``[mozyo:...]`` marker grammar,
whose fields are ``:``-separated ``key=value`` tokens. A value containing ``:``/``=``/``]`` would not
round-trip — it would forge a field boundary and read back as a *different, well-formed* key. That is
a silent identity forgery, so it is refused at render rather than detected later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional

from .redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)

#: The key schema version. It participates in the digest, so a future field set can never be
#: mistaken for this one: a v2 key digests differently even if every v1 field matches.
RECOVERY_KEY_SCHEMA_VERSION = 1

#: The marker ``kind`` that carries a recovery action's admission key. DELIBERATELY distinct from
#: ``callback_sweep_record`` (:data:`..domain.callback_sweep_watermark.SWEEP_RECORD_KIND`): that
#: marker identifies the record for the WRITER's own dedup, while this one identifies the ACTION a
#: receiver may admit. Only records whose recovery is actually delivered carry this marker, so a
#: zero-send resolution can never be admitted as an action.
RECOVERY_ACTION_MARKER_KIND = "callback_recovery_action"

#: The domain tag every canonical encoding is prefixed with. Domain separation: a digest from this
#: key space cannot collide with one from another authority's, whatever the field values.
_KEY_DOMAIN = "mozyo.callback_recovery_admission"

#: The ACTION tag. A second, disjoint digest space over the fields that say *which recovery this
#: is* — deliberately NOT including the journal it was published at (review j#81021 F2). See
#: :meth:`RecoveryAdmissionKey.action_digest`.
_ACTION_DOMAIN = "mozyo.callback_recovery_action"

#: ``retry_of`` when this action is not a retry. An explicit sentinel rather than a blank: every key
#: field is non-empty by construction, and "I am not a retry" is a positive statement the producer
#: must make, not an absence a reader has to interpret.
RETRY_OF_NONE = "none"

#: The canonical field order. The digest is defined over exactly these, in exactly this order.
_KEY_FIELDS = (
    "schema_version",
    "recovery_action_journal",
    "original_dispatch_anchor",
    "workspace_id",
    "lane_id",
    "lane_generation",
    "route_identity",
    "receiver_identity",
    "action_kind",
    "retry_of",
)

#: The fields that identify the ACTION rather than its publication (review j#81021 F2).
#:
#: ``recovery_action_journal`` is excluded because it is precisely what an accidental duplicate
#: publication — or a hand-copied note — changes, and ``retry_of`` because a retry is by definition
#: the same action again. Everything else is what makes two recoveries the same recovery.
_ACTION_FIELDS = (
    "schema_version",
    "original_dispatch_anchor",
    "workspace_id",
    "lane_id",
    "lane_generation",
    "route_identity",
    "receiver_identity",
    "action_kind",
)

#: The subset the marker carries: every key field the writer knows at write time. Maps key field ->
#: marker field name. ``recovery_action_journal`` is absent by construction (see the module note).
_MARKER_FIELD_NAMES = {
    "schema_version": "schema_version",
    "original_dispatch_anchor": "anchor",
    "workspace_id": "workspace",
    "lane_id": "lane",
    "lane_generation": "lane_generation",
    "route_identity": "route",
    "receiver_identity": "receiver",
    "action_kind": "action_kind",
    "retry_of": "retry_of",
}

#: Characters that cannot survive the marker grammar (``[mozyo:<channel>:k=v:k=v]``). A value
#: carrying one would forge a field boundary and read back as a different well-formed key.
_FORBIDDEN_VALUE_CHARS = frozenset(":=[]")

#: Lookup reasons. Each names a DISTINCT failure the caller must be able to tell apart — collapsing
#: them into a bare ``None`` would let "this record has no action" and "this record is ambiguous"
#: take the same branch, and only one of those is a conflict.
LOOKUP_ENTRY_ABSENT = "entry_absent"
LOOKUP_ENTRY_AMBIGUOUS = "entry_ambiguous"
LOOKUP_MARKER_ABSENT = "marker_absent"
LOOKUP_MARKER_AMBIGUOUS = "marker_ambiguous"
LOOKUP_MARKER_MALFORMED = "marker_malformed"


#: sha256 hex length. A retry linkage must look exactly like a key digest or be the sentinel.
_DIGEST_LENGTH = 64


class RecoveryKeyError(ValueError):
    """A key / marker could not be built from the given facts (fail-closed; never a partial key)."""


def _is_digest(value: str) -> bool:
    """True when ``value`` is shaped exactly like a key digest (pure)."""
    text = str(value or "")
    return len(text) == _DIGEST_LENGTH and all(c in "0123456789abcdef" for c in text)


def _validate_value(name: str, value: object) -> str:
    """Return the stripped value, or raise: an unrepresentable field is refused at the boundary."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise RecoveryKeyError(
            f"recovery admission key field {name!r} is empty: an unkeyed recovery action cannot be "
            f"admitted, deduplicated, or reconciled"
        )
    bad = sorted(set(text) & _FORBIDDEN_VALUE_CHARS)
    if bad:
        raise RecoveryKeyError(
            f"recovery admission key field {name!r}={text!r} contains {bad!r}, which the marker "
            f"grammar uses as field delimiters: it would read back as a DIFFERENT well-formed key. "
            f"Refusing to render an identity that cannot round-trip"
        )
    if any(ch.isspace() for ch in text):
        raise RecoveryKeyError(
            f"recovery admission key field {name!r}={text!r} contains whitespace, which the marker "
            f"grammar cannot round-trip verbatim"
        )
    return text


@dataclass(frozen=True)
class RecoveryAdmissionKey:
    """The exact identity of ONE recovery action, as j#80984 Disposition 2 fixes it.

    Every field narrows *which* action this is, and a drift in any of them means the delivery in
    hand is not the action the durable record describes:

    - ``recovery_action_journal`` — the durable record's own journal id (the ordered anchor the
      receiver was pointed at). The authority, resolved by read-back, never self-reported;
    - ``original_dispatch_anchor`` — the dispatch round the stall was derived against;
    - ``workspace_id`` / ``lane_id`` / ``lane_generation`` — the partition and the exact round;
    - ``route_identity`` — the assigned name the delivery was addressed to;
    - ``receiver_identity`` — the semantic receiver role that is allowed to admit it;
    - ``action_kind`` — which recovery action this is, so two kinds at one anchor stay distinct;
    - ``retry_of`` — :data:`RETRY_OF_NONE`, or the digest of the key this one explicitly retries.

    **Why ``retry_of`` exists** (review j#81021 F2). ``recovery_action_journal`` is in the key, so
    the same recovery re-published at a different journal id digests differently — and both copies
    admitted. A hand-copied note or an accidental duplicate publication therefore walked straight
    around a claim that is never reclaimed. Journal id alone cannot separate "the coordinator
    deliberately issued a retry" from "this action showed up twice", so the producer has to *say*
    which it is, and :meth:`action_digest` is what lets the authority hold it to that.
    """

    recovery_action_journal: str
    original_dispatch_anchor: str
    workspace_id: str
    lane_id: str
    lane_generation: str
    route_identity: str
    receiver_identity: str
    action_kind: str
    retry_of: str = RETRY_OF_NONE
    schema_version: int = RECOVERY_KEY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in _KEY_FIELDS:
            if name == "schema_version":
                continue
            object.__setattr__(self, name, _validate_value(name, getattr(self, name)))
        # A retry linkage is either the explicit "not a retry" sentinel or a real key digest.
        # Anything else is refused here rather than compared later: a malformed linkage that
        # reached the authority would simply never match, and would surface as a mystery conflict
        # instead of the producer bug it is.
        if self.retry_of != RETRY_OF_NONE and not _is_digest(self.retry_of):
            raise RecoveryKeyError(
                f"recovery admission key retry_of={self.retry_of!r} is neither {RETRY_OF_NONE!r} "
                f"nor a {_DIGEST_LENGTH}-hex-character key digest: a retry must name the exact key "
                f"it retries, or positively declare that it is not a retry"
            )
        try:
            version = int(self.schema_version)
        except (TypeError, ValueError) as exc:
            raise RecoveryKeyError(
                f"recovery admission key schema_version={self.schema_version!r} is not an integer"
            ) from exc
        if version != RECOVERY_KEY_SCHEMA_VERSION:
            raise RecoveryKeyError(
                f"recovery admission key schema_version={version} is not the supported version "
                f"{RECOVERY_KEY_SCHEMA_VERSION}: an unknown key schema is refused rather than "
                f"admitted under this version's meaning"
            )
        object.__setattr__(self, "schema_version", version)

    def as_fields(self) -> tuple[tuple[str, str], ...]:
        """The canonical ``(name, value)`` pairs in canonical order (pure)."""
        return tuple((name, str(getattr(self, name))) for name in _KEY_FIELDS)

    def canonical_encoding(self) -> str:
        """The injective canonical encoding of this key (pure).

        Length-prefixed per field under the domain tag, so no field value — whatever it contains —
        can shift a boundary and make two distinct keys encode identically.
        """
        parts = [f"{_KEY_DOMAIN}.v{self.schema_version}"]
        parts.extend(f"{name}={len(value)}:{value}" for name, value in self.as_fields())
        return "\x1f".join(parts)

    def digest(self) -> str:
        """The stable content digest of this key (pure, sha256 of :meth:`canonical_encoding`)."""
        return hashlib.sha256(self.canonical_encoding().encode("utf-8")).hexdigest()

    def action_canonical_encoding(self) -> str:
        """The injective canonical encoding of the ACTION this key names (pure).

        Same length-prefixed scheme, different domain tag and a narrower field set — so an action
        digest can never be mistaken for a key digest, and vice versa.
        """
        parts = [f"{_ACTION_DOMAIN}.v{self.schema_version}"]
        parts.extend(
            f"{name}={len(str(getattr(self, name)))}:{getattr(self, name)}"
            for name in _ACTION_FIELDS
        )
        return "\x1f".join(parts)

    def action_digest(self) -> str:
        """The digest of *which recovery this is*, ignoring where it was published (pure).

        Review j#81021 F2. Two keys share an action digest exactly when they are the same recovery
        — same round, same route, same receiver, same kind — however many journals it was published
        at. That is what lets the authority ask the question journal id cannot answer: "has this
        recovery already been admitted here, under any name?"
        """
        return hashlib.sha256(self.action_canonical_encoding().encode("utf-8")).hexdigest()

    @property
    def is_retry(self) -> bool:
        """True when this key positively declares itself a retry of a specific prior key."""
        return self.retry_of != RETRY_OF_NONE


def render_recovery_action_marker(
    *,
    original_dispatch_anchor: str,
    workspace_id: str,
    lane_id: str,
    lane_generation: object,
    route_identity: str,
    receiver_identity: str,
    action_kind: str,
    retry_of: str = RETRY_OF_NONE,
) -> str:
    """The durable marker that carries a recovery action's admission key (pure; fail-closed).

    Written into the recovery record the notification points at, and read back by
    :func:`resolve_recovery_action_key`. Every field is validated here rather than at read time:
    an unrepresentable value is a defect in the caller, and rendering it would mint a marker that
    silently reads back as a different key.

    This is the **canonical producer** of a retry linkage (review j#81021 F2). A coordinator that
    deliberately re-issues a recovery — because the prior round's receiver claimed and then died,
    and claims are never reclaimed — records a new durable journal bearing this marker with
    ``retry_of`` set to the prior key's digest. That declaration is the only thing that separates an
    authorized retry from a duplicate publication, so it is written here, in the marker, rather than
    asserted in prose.

    ``retry_of`` defaults to :data:`RETRY_OF_NONE` because "not a retry" is the overwhelmingly
    common case AND the fail-closed one: an action that omits the linkage is refused when a prior
    admission exists, never admitted. (Contrast ``route_identity`` / ``receiver_identity``, which
    have no safe default and are therefore required.)
    """
    fields = {
        "original_dispatch_anchor": original_dispatch_anchor,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "lane_generation": lane_generation,
        "route_identity": route_identity,
        "receiver_identity": receiver_identity,
        "action_kind": action_kind,
        "retry_of": retry_of,
    }
    rendered = {name: _validate_value(name, value) for name, value in fields.items()}
    if rendered["retry_of"] != RETRY_OF_NONE and not _is_digest(rendered["retry_of"]):
        raise RecoveryKeyError(
            f"recovery action marker retry_of={rendered['retry_of']!r} is neither "
            f"{RETRY_OF_NONE!r} nor a key digest: refusing to mint a linkage no authority can "
            f"resolve"
        )
    tokens = [
        f"kind={RECOVERY_ACTION_MARKER_KIND}",
        f"{_MARKER_FIELD_NAMES['schema_version']}={RECOVERY_KEY_SCHEMA_VERSION}",
    ]
    tokens.extend(
        f"{_MARKER_FIELD_NAMES[name]}={rendered[name]}"
        for name in _KEY_FIELDS
        if name not in ("schema_version", "recovery_action_journal")
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:" + ":".join(tokens) + "]"


@dataclass(frozen=True)
class RecoveryActionLookup:
    """The outcome of resolving a recovery action key from durable entries.

    ``key`` is set only on an unambiguous success; otherwise ``reason`` names WHICH failure this
    was. The two are never both meaningful: a caller that sees a ``reason`` must not actuate.
    """

    key: Optional[RecoveryAdmissionKey] = None
    reason: str = ""
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.key is not None


def _marker_key_from_fields(fields: dict, *, recovery_action_journal: str) -> RecoveryAdmissionKey:
    """Build the key from one marker's fields + the owning entry's id, or raise (pure)."""
    missing = [
        marker_name
        for key_name, marker_name in _MARKER_FIELD_NAMES.items()
        if key_name != "schema_version" and not str(fields.get(marker_name, "") or "").strip()
    ]
    if missing:
        raise RecoveryKeyError(
            f"recovery action marker is missing required field(s) {missing!r}: an incomplete key "
            f"cannot be admitted"
        )
    raw_version = str(fields.get(_MARKER_FIELD_NAMES["schema_version"], "") or "").strip()
    if not raw_version:
        raise RecoveryKeyError(
            "recovery action marker carries no schema_version: an unversioned key cannot be "
            "interpreted under this version's meaning"
        )
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise RecoveryKeyError(
            f"recovery action marker schema_version={raw_version!r} is not an integer"
        ) from exc
    return RecoveryAdmissionKey(
        schema_version=version,
        recovery_action_journal=recovery_action_journal,
        original_dispatch_anchor=fields.get(_MARKER_FIELD_NAMES["original_dispatch_anchor"], ""),
        workspace_id=fields.get(_MARKER_FIELD_NAMES["workspace_id"], ""),
        lane_id=fields.get(_MARKER_FIELD_NAMES["lane_id"], ""),
        lane_generation=fields.get(_MARKER_FIELD_NAMES["lane_generation"], ""),
        route_identity=fields.get(_MARKER_FIELD_NAMES["route_identity"], ""),
        receiver_identity=fields.get(_MARKER_FIELD_NAMES["receiver_identity"], ""),
        action_kind=fields.get(_MARKER_FIELD_NAMES["action_kind"], ""),
        retry_of=fields.get(_MARKER_FIELD_NAMES["retry_of"], ""),
    )


def resolve_recovery_action_key(
    entries: Iterable[object], *, recovery_action_journal: str
) -> RecoveryActionLookup:
    """Resolve the admission key of the recovery action recorded at ``recovery_action_journal``.

    The read side of :func:`render_recovery_action_marker` (pure). The anchor authority is the
    durable entry's **own** id, mirroring :func:`..domain.callback_sweep_watermark.sweep_record_journals`.

    Fails closed on every ambiguity, because each one means the receiver cannot prove WHICH action
    it holds: no such entry, two entries claiming the id, no action marker (a zero-send resolution
    or an unrelated note — there is nothing to actuate), two action markers in one note, or a
    marker that does not parse into a complete key.
    """
    anchor = str(recovery_action_journal or "").strip()
    if not anchor:
        return RecoveryActionLookup(
            reason=LOOKUP_ENTRY_ABSENT,
            detail="no recovery action journal was supplied; there is no action to admit",
        )
    matched = [
        entry
        for entry in (entries or ())
        if str(getattr(entry, "journal_id", "") or "").strip() == anchor
    ]
    if not matched:
        return RecoveryActionLookup(
            reason=LOOKUP_ENTRY_ABSENT,
            detail=(
                f"journal j#{anchor} is not present in the durable record read for this issue; the "
                f"pointed-at recovery action cannot be verified"
            ),
        )
    if len(matched) > 1:
        return RecoveryActionLookup(
            reason=LOOKUP_ENTRY_AMBIGUOUS,
            detail=f"{len(matched)} durable entries claim journal id j#{anchor}; refusing to pick one",
        )
    markers = [
        fields
        for channel, fields in marker_fields_in_note(str(getattr(matched[0], "notes", "") or ""))
        if channel == MARKER_CHANNEL_WORKFLOW_EVENT
        and str(fields.get("kind", "")).strip() == RECOVERY_ACTION_MARKER_KIND
    ]
    if not markers:
        return RecoveryActionLookup(
            reason=LOOKUP_MARKER_ABSENT,
            detail=(
                f"journal j#{anchor} carries no {RECOVERY_ACTION_MARKER_KIND} marker: it records no "
                f"recovery action (a zero-send resolution or an unrelated note), so there is "
                f"nothing to admit"
            ),
        )
    if len(markers) > 1:
        return RecoveryActionLookup(
            reason=LOOKUP_MARKER_AMBIGUOUS,
            detail=(
                f"journal j#{anchor} carries {len(markers)} {RECOVERY_ACTION_MARKER_KIND} markers; "
                f"the action's identity is ambiguous and is not guessed"
            ),
        )
    try:
        key = _marker_key_from_fields(markers[0], recovery_action_journal=anchor)
    except RecoveryKeyError as exc:
        return RecoveryActionLookup(reason=LOOKUP_MARKER_MALFORMED, detail=str(exc))
    return RecoveryActionLookup(key=key)


__all__ = (
    "RECOVERY_KEY_SCHEMA_VERSION",
    "RETRY_OF_NONE",
    "RECOVERY_ACTION_MARKER_KIND",
    "LOOKUP_ENTRY_ABSENT",
    "LOOKUP_ENTRY_AMBIGUOUS",
    "LOOKUP_MARKER_ABSENT",
    "LOOKUP_MARKER_AMBIGUOUS",
    "LOOKUP_MARKER_MALFORMED",
    "RecoveryKeyError",
    "RecoveryAdmissionKey",
    "RecoveryActionLookup",
    "render_recovery_action_marker",
    "resolve_recovery_action_key",
)
