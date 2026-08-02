"""Identity-receipt capability marker + canonical launch manifest (Redmine #14741).

Carved out of :mod:`.startup_transaction_fence` so that module stays under the
module-health ceiling: this is a cohesive, dependency-free unit (a few constants, two pure
value types, and four pure functions) and the fence re-exports every name, so no importer
changes.

**Why the capability lives on the action id.** Design Answer j#96917 / j#96892: if
"does this action owe identity receipts?" were recorded inside the identity-receipt
sidecar, then deleting or corrupting that sidecar would delete the capability with it — a
receipt-capable action would read as a pre-feature legacy one and the self-heal would fail
OPEN into exactly the generic relaunch #14741 exists to stop. Encoding it in the action id
puts the marker in ``startup_actions.action_id``, outside the sidecar, where losing the
sidecar cannot reach it.

This module is a LEAF: it imports nothing from the fence (it takes a duck-typed unit with a
``canonical()``), so the fence can import it without a cycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


class StartupTransactionError(RuntimeError):
    """The startup transaction authority is unusable / was asked for something invalid."""


class StartupTransactionBusy(StartupTransactionError):
    """Another startup transaction holds this authority. Never wait, never steal."""


def _norm(value: object) -> str:
    return str(value or "").strip()


#: A pre-#14741 action. Carries no capability claim; its launches predate identity receipts.
CAPABILITY_LEGACY = ""
#: An action that MUST satisfy identity receipts (Redmine #14741, Design Answer j#96917).
CAPABILITY_IDENTITY_RECEIPT = "ir1"

CAPABILITIES: frozenset = frozenset({CAPABILITY_LEGACY, CAPABILITY_IDENTITY_RECEIPT})

_LEGACY_ACTION_RE = re.compile(r"^startup-[0-9a-f]{64}$")
_TAGGED_ACTION_RE = re.compile(r"^startup-(ir1)-[0-9a-f]{64}$")


def startup_action_id(
    unit: StartupUnit,
    nonce: str,
    *,
    capability: str = CAPABILITY_LEGACY,
    manifest_digest: str = "",
) -> str:
    """The immutable identity of one session-start invocation.

    The unit alone is NOT an identity: the same operator re-running the same command in the
    same lane is a *different* action, and letting the second inherit the first's record is
    how an old completion gets applied to a live pair. The ``nonce`` is what separates
    them; it is supplied by the caller (and injected by tests) rather than minted here, so
    this stays pure and the invocation stays the single place a new identity is born.

    Capability tagging (Redmine #14741, Design Answer j#96917)
    ---------------------------------------------------------
    The action id doubles as the **capability marker**, and that is the whole point of
    putting it here rather than in a table: the marker then lives in
    ``startup_actions.action_id``, OUTSIDE the identity-receipt sidecar, so deleting or
    corrupting that sidecar cannot make a receipt-capable action read as a pre-feature
    legacy one and fail OPEN into a generic heal (j#96892).

    ``CAPABILITY_LEGACY`` (the default) reproduces the pre-#14741 id **byte for byte**, so
    every existing caller and every stored id is unchanged. ``CAPABILITY_IDENTITY_RECEIPT``
    yields ``startup-ir1-<64hex>`` and REQUIRES ``manifest_digest``: a tagged action's
    identity is content-bound to the canonical launch manifest it promises to satisfy, so a
    manifest that is missing, truncated, or tampered with can no longer correspond to the
    id that named it.
    """
    canonical = unit.canonical()
    values = (
        canonical.workspace_id,
        canonical.lane_id,
        ",".join(canonical.providers),
        _norm(nonce),
    )
    if not all(values):
        raise ValueError(
            "a startup action identity requires an exact workspace, lane, requested "
            "provider set, and nonce"
        )
    capability = _norm(capability)
    if capability not in CAPABILITIES:
        raise ValueError(
            f"unknown startup action capability {capability!r}; allowed: "
            f"{sorted(CAPABILITIES)}"
        )
    if capability == CAPABILITY_LEGACY:
        if _norm(manifest_digest):
            raise ValueError(
                "a legacy startup action carries no manifest; passing a manifest digest "
                "would silently produce an id nothing can re-derive"
            )
        encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
        return "startup-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    digest = _norm(manifest_digest)
    if not digest:
        raise ValueError(
            "a capability-tagged startup action must be content-bound to its canonical "
            "launch manifest digest; refusing to mint a tag nothing can verify"
        )
    encoded = json.dumps(
        (*values, capability, digest), ensure_ascii=True, separators=(",", ":")
    )
    return (
        f"startup-{capability}-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    )


def action_capability(action_id: object) -> str:
    """The capability an action id claims (pure). Raises on an unrecognised shape.

    Exactly two shapes are recognised, and anything else is a typed refusal rather than a
    guess: an id whose shape we cannot classify must never be *assumed* legacy, because
    "assume legacy" is precisely the fail-open j#96892 forbids.
    """
    value = action_id if isinstance(action_id, str) else ""
    if _LEGACY_ACTION_RE.match(value):
        return CAPABILITY_LEGACY
    tagged = _TAGGED_ACTION_RE.match(value)
    if tagged:
        return tagged.group(1)
    raise StartupTransactionError(
        f"startup action id {value!r} matches neither the legacy nor a known "
        "capability-tagged shape; refusing to classify it (an unclassifiable action is "
        "never treated as a pre-feature legacy one)"
    )


def requires_identity_receipt(action_id: object) -> bool:
    """True iff this action promised identity receipts. Raises on an unknown shape."""
    return action_capability(action_id) == CAPABILITY_IDENTITY_RECEIPT


def startup_action_id_matching(
    unit: StartupUnit, nonce: str, observed_action_id: object
) -> str:
    """Re-derive the id for an ALREADY-OBSERVED action, honouring its capability.

    The one helper every re-derivation surface goes through (j#96917). Each of them asks the
    same question — "is this stored action the one these inputs would have produced?" — and
    each of them previously assumed the legacy shape.

    A legacy observed id re-derives byte-identically, so those paths are unchanged. A
    **tagged** observed id returns ``""``: its identity is content-bound to a manifest the
    caller does not hold, so it cannot be corresponded from ``(unit, nonce)`` alone, and the
    honest answer is "not re-derivable here" rather than a legacy id that will silently fail
    to match. Callers treat ``""`` as no-match, which is fail-closed. An unclassifiable id
    raises, which is also fail-closed — and deliberately louder, because it means the store
    holds something neither runtime wrote.
    """
    if action_capability(observed_action_id) != CAPABILITY_LEGACY:
        return ""
    return startup_action_id(unit, nonce)


#: The additive sibling table (Design Answer j#96917 item 2). It lives in the SAME store as
#: ``startup_actions`` and is deliberately NOT in :data:`_EXPECTED_COLUMNS`: ``_verify_shape``
#: validates only the required tables, so an extra one is tolerated and a store written by a
#: runtime that predates this feature still verifies byte-unchanged. No ``user_version`` bump,
#: no participant-key-set change, no migration of a shared home.
#:
#: ``startup_execution_events`` was NOT reused for this: it is an explicitly best-effort
#: diagnostic whose absence its own module forbids reading as authority, and this manifest is
#: authority — a tagged action whose manifest is missing is zero-actuation, not "no events".
_IDENTITY_MANIFEST_TABLE = "startup_identity_manifests"

_IDENTITY_MANIFEST_SQL = f"""
CREATE TABLE IF NOT EXISTS {_IDENTITY_MANIFEST_TABLE} (
    action_id       TEXT PRIMARY KEY NOT NULL,
    workspace_id    TEXT NOT NULL,
    lane_id         TEXT NOT NULL,
    protocol        TEXT NOT NULL,
    slots           TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
)
"""

_IDENTITY_MANIFEST_COLUMNS = (
    "action_id", "workspace_id", "lane_id", "protocol", "slots", "manifest_digest",
    "recorded_at",
)

#: The manifest protocol this build writes and accepts.
IDENTITY_MANIFEST_PROTOCOL = "ir1"

#: A tagged action whose manifest cannot be positively corresponded. ALWAYS zero-actuation:
#: never "no requirement", never legacy.
REASON_RECEIPT_REQUIREMENT_UNAVAILABLE = "identity_receipt_requirement_unavailable"


@dataclass(frozen=True)
class IdentityManifestSlot:
    """One planned launch slot's receipt obligation, as the preflight pinned it."""

    provider: str
    assigned_name: str
    identity_receipt_required: bool
    #: The identity the PREFLIGHT pinned for this slot (``ResolvedProviderLaunch``), never a
    #: separate disk re-resolution (j#96886). Empty exactly when not required.
    identity_digest: str = ""

    def canonical(self) -> tuple:
        provider = _norm(self.provider)
        assigned = _norm(self.assigned_name)
        required = bool(self.identity_receipt_required)
        digest = _norm(self.identity_digest)
        if not provider or not assigned:
            raise ValueError("a manifest slot requires an exact provider and assigned name")
        if required and not digest:
            raise ValueError(
                f"slot {provider!r} is marked receipt-required but carries no pinned "
                "identity; a requirement nothing can satisfy is never recorded"
            )
        if not required and digest:
            raise ValueError(
                f"slot {provider!r} is not receipt-required but carries an identity; the "
                "manifest must state exactly one of the two"
            )
        return (provider, assigned, required, digest)


@dataclass(frozen=True)
class IdentityManifest:
    """The canonical, complete launch plan one tagged action promises to satisfy.

    Complete is load-bearing: an unbound provider is recorded with
    ``identity_receipt_required=False`` rather than omitted, so "this slot has no obligation"
    is a written fact and not an absence that a later reader has to interpret. That is also
    what keeps ``package query 0`` honest for unbound providers — the manifest says they were
    considered and excluded.
    """

    workspace_id: str
    lane_id: str
    slots: tuple
    protocol: str = IDENTITY_MANIFEST_PROTOCOL

    def canonical_payload(self) -> str:
        workspace = _norm(self.workspace_id)
        lane = _norm(self.lane_id)
        protocol = _norm(self.protocol)
        if not workspace or not lane or not protocol:
            raise ValueError("a launch manifest requires an exact workspace, lane, protocol")
        if not self.slots:
            raise ValueError("a launch manifest records the WHOLE plan; it is never empty")
        slots = [slot.canonical() for slot in self.slots]
        if len({(s[0], s[1]) for s in slots}) != len(slots):
            raise ValueError("a launch manifest must not repeat a (provider, assigned) slot")
        return json.dumps(
            [protocol, workspace, lane, slots],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()

    def required_slots(self) -> tuple:
        return tuple(s for s in self.slots if s.identity_receipt_required)




#: The damaged-store token. Imported lazily inside the read so this module stays a LEAF
#: (the fence imports this one; importing it back at module scope would be a cycle).
def read_identity_manifest(fence, action_id: str):
    """The canonical launch manifest a TAGGED action is content-bound to (fail-closed).

    Returns ``None`` for a legacy (untagged) action: it made no receipt promise, so there
    is nothing to read and nothing owed. That is the ONLY absence this method reports as
    benign, and it is decided from the action id's own shape — never from whether a row
    happens to be there (j#96892).

    For a tagged action, every other outcome raises
    :class:`StartupTransactionError` carrying
    :data:`REASON_RECEIPT_REQUIREMENT_UNAVAILABLE`, which callers must treat as
    zero-actuation: an absent store, an absent sibling table, a missing row, an
    undecodable payload, or a payload whose digest does not reproduce the action id it
    is filed under. The last one is what makes tampering detectable rather than merely
    unlikely — the id IS the digest's witness.
    """
    capability = action_capability(action_id)
    if capability == CAPABILITY_LEGACY:
        return None
    unavailable = (
        f"{REASON_RECEIPT_REQUIREMENT_UNAVAILABLE}: startup action {action_id!r} "
        "promised identity receipts"
    )
    from mozyo_bridge.core.state.startup_transaction_fence import STORE_DAMAGED

    shape = fence.store_shape()
    if shape.absent or shape.state == STORE_DAMAGED:
        raise StartupTransactionError(
            f"{unavailable}, but its startup authority is absent or damaged; "
            "zero-actuation (never read as a pre-feature legacy action)"
        )
    with fence._connection("ro") as conn:
        try:
            columns = {
                str(row[1])
                for row in conn.execute(
                    f"PRAGMA table_info({_IDENTITY_MANIFEST_TABLE})"
                ).fetchall()
            }
            if set(_IDENTITY_MANIFEST_COLUMNS) - columns:
                raise StartupTransactionError(
                    f"{unavailable}, but the manifest table is absent or partial; "
                    "zero-actuation"
                )
            row = conn.execute(
                f"SELECT {', '.join(_IDENTITY_MANIFEST_COLUMNS)} FROM "
                f"{_IDENTITY_MANIFEST_TABLE} WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        except StartupTransactionError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise StartupTransactionError(
                f"{unavailable}, but its manifest could not be read ({exc}); "
                "zero-actuation"
            ) from exc
    if row is None:
        raise StartupTransactionError(
            f"{unavailable}, but no manifest is filed for it; zero-actuation"
        )
    values = dict(zip(_IDENTITY_MANIFEST_COLUMNS, row))
    try:
        decoded = json.loads(values["slots"])
        protocol, workspace, lane, slots = decoded
        manifest = IdentityManifest(
            workspace_id=workspace,
            lane_id=lane,
            protocol=protocol,
            slots=tuple(
                IdentityManifestSlot(
                    provider=slot[0],
                    assigned_name=slot[1],
                    identity_receipt_required=bool(slot[2]),
                    identity_digest=slot[3],
                )
                for slot in slots
            ),
        )
        digest = manifest.digest()
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        raise StartupTransactionError(
            f"{unavailable}, but its manifest payload is malformed ({exc}); "
            "zero-actuation"
        ) from exc
    if digest != values["manifest_digest"]:
        raise StartupTransactionError(
            f"{unavailable}, but its stored manifest does not reproduce the digest it "
            "was filed under; zero-actuation"
        )
    if (
        _norm(manifest.workspace_id) != _norm(values["workspace_id"])
        or _norm(manifest.lane_id) != _norm(values["lane_id"])
    ):
        # A payload whose own workspace/lane disagree with the row it is filed under is
        # a manifest moved between actions. The digest check above cannot see that (it
        # only proves the payload is intact), so it is checked separately.
        raise StartupTransactionError(
            f"{unavailable}, but its manifest describes a different workspace/lane "
            "than the action it is filed under; zero-actuation"
        )
    return manifest



def resolve_reserve_identity(canonical, nonce: str, manifest):
    """``(action_id, manifest_digest, manifest_payload)`` for one reserve (pure).

    Splits the reserve's identity decision from its IO so the fence module stays under the
    module-health ceiling. ``manifest is None`` is the pre-#14741 path and reproduces the
    legacy id byte for byte; a manifest mints the capability-tagged id content-bound to it.

    Refuses a manifest whose own workspace/lane disagree with the action's: a plan filed
    against the wrong action is exactly the confusion the content-binding exists to make
    impossible, and it is cheaper to refuse here than to detect later.
    """
    if manifest is None:
        return (startup_action_id(canonical, nonce), "", "")
    try:
        payload = manifest.canonical_payload()
    except ValueError as exc:
        raise StartupTransactionError(
            f"the launch manifest for this reserve is not canonical ({exc}); "
            "nothing was started"
        ) from exc
    if (
        _norm(manifest.workspace_id) != canonical.workspace_id
        or _norm(manifest.lane_id) != canonical.lane_id
    ):
        raise StartupTransactionError(
            "the launch manifest describes a different workspace/lane than the action it "
            "would be bound to; nothing was started"
        )
    digest = manifest.digest()
    return (
        startup_action_id(
            canonical,
            nonce,
            capability=CAPABILITY_IDENTITY_RECEIPT,
            manifest_digest=digest,
        ),
        digest,
        payload,
    )


def write_reserved_action(
    conn,
    *,
    action_id: str,
    canonical,
    phase: str,
    now: str,
    manifest,
    manifest_digest: str,
    manifest_payload: str,
) -> None:
    """Write the action row and (when tagged) its manifest in ONE transaction.

    The atomicity is the requirement, not an optimisation (Design Answer j#96917 item 2).
    A crash between the two writes would leave either a tagged action with no manifest —
    which must be zero-actuation, and would otherwise have to be *guessed* at — or a
    manifest for an action that never existed. Committing them together removes both gaps,
    and both happen strictly before the reserve's first side effect.
    """
    if manifest is not None:
        # Created here, inside the caller's lock, only after the store itself verified. It
        # is additive, so a store written before this feature simply does not have it yet.
        conn.execute(_IDENTITY_MANIFEST_SQL)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO startup_actions (action_id, workspace_id, lane_id, providers,"
        " phase, revision, participants, reserved_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            action_id,
            canonical.workspace_id,
            canonical.lane_id,
            ",".join(canonical.providers),
            phase,
            1,
            json.dumps([]),
            now,
            now,
        ),
    )
    if manifest is not None:
        conn.execute(
            f"INSERT INTO {_IDENTITY_MANIFEST_TABLE} (action_id, workspace_id, lane_id,"
            " protocol, slots, manifest_digest, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                canonical.workspace_id,
                canonical.lane_id,
                _norm(manifest.protocol),
                manifest_payload,
                manifest_digest,
                now,
            ),
        )
    conn.execute("COMMIT")
