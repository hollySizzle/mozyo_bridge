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
import sqlite3
from dataclasses import dataclass
from pathlib import Path


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

#: The version that carries the identity-receipt manifest as part of its REQUIRED shape.
#:
#: Design Answer j#96936 supersedes the no-schema-bump part of j#96917, on the strength of
#: the R11 j#96933 proof: an old runtime does not inspect the action id, so a per-action
#: tag can never make it fail closed. What it DOES have is an exact `user_version == 1`
#: check — a store-global fence it already enforces. So the tag stops being asked to do a
#: job it cannot do, and the store version does it instead: an old runtime meeting a v2
#: store rejects the whole store at the DB door, including rollback / status /
#: current-action.
STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION_V2 = 2
#: Versions this runtime can READ. A v1 store stays fully usable for legacy actions; only
#: writing a capability-tagged action requires v2.
STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS: tuple = (1, 2)
#: A tagged reserve was asked for on a v1 store. Typed, and zero-write: the runtime never
#: migrates a shared home on its own — that is the #14838 offline rail's authority alone.
REASON_OFFLINE_UPGRADE_REQUIRED = "offline_global_runtime_upgrade_required"

# `fullmatch`, never `$`: Python's `$` also matches BEFORE a trailing newline, so
# "startup-ir1-<64hex>\n" classified as tagged (audit j#96928 F6). An action id is a byte
# string, and its classification must be byte-exact — a trailing newline or control
# character is a DIFFERENT id and must not be recognised as either shape.
_LEGACY_ACTION_RE = re.compile(r"startup-[0-9a-f]{64}")
_TAGGED_ACTION_RE = re.compile(r"startup-(ir1)-[0-9a-f]{64}")


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
    if _LEGACY_ACTION_RE.fullmatch(value):
        return CAPABILITY_LEGACY
    tagged = _TAGGED_ACTION_RE.fullmatch(value)
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
CREATE TABLE {_IDENTITY_MANIFEST_TABLE} (
    action_id       TEXT NOT NULL PRIMARY KEY,
    workspace_id    TEXT NOT NULL,
    lane_id         TEXT NOT NULL,
    protocol        TEXT NOT NULL,
    slots           TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
)
"""

_IDENTITY_MANIFEST_COLUMNS = (
    "action_id", "workspace_id", "lane_id", "protocol", "slots", "manifest_digest",
    "nonce", "recorded_at",
)

#: The EXACT shape a usable sibling table has, as ``PRAGMA table_info`` reports it:
#: ``(cid, name, declared type, notnull, default, pk)``. Compared in full and in order.
#:
#: Audit j#96928 F5: checking only that the required column NAMES are present let a foreign
#: table with extra columns, a missing primary key, or drifted types/notnull/defaults be
#: written into. A store this feature did not create is not a store this feature may mutate:
#: the table is created ONLY when absent, and anything else is a zero-write refusal.
_IDENTITY_MANIFEST_SHAPE = (
    (0, "action_id", "TEXT", 1, None, 1),
    (1, "workspace_id", "TEXT", 1, None, 0),
    (2, "lane_id", "TEXT", 1, None, 0),
    (3, "protocol", "TEXT", 1, None, 0),
    (4, "slots", "TEXT", 1, None, 0),
    (5, "manifest_digest", "TEXT", 1, None, 0),
    (6, "nonce", "TEXT", 1, None, 0),
    (7, "recorded_at", "TEXT", 1, None, 0),
)


def _manifest_table_state(conn) -> str:
    """``"absent"`` / ``"exact"`` — or raise for anything else (zero-write)."""
    rows = conn.execute(f"PRAGMA table_info({_IDENTITY_MANIFEST_TABLE})").fetchall()
    if not rows:
        return "absent"
    actual = tuple(
        (int(r[0]), str(r[1]), str(r[2]).upper(), int(r[3]), r[4], int(r[5])) for r in rows
    )
    if actual != _IDENTITY_MANIFEST_SHAPE:
        raise StartupTransactionError(
            f"{REASON_RECEIPT_REQUIREMENT_UNAVAILABLE}: the "
            f"{_IDENTITY_MANIFEST_TABLE!r} table exists with a shape this build did not "
            "create (extra/missing columns, drifted type/notnull/default, or no primary "
            "key); refusing to read or write it — nothing was started"
        )
    return "exact"

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
        if not workspace or not lane:
            raise ValueError("a launch manifest requires an exact workspace and lane")
        if protocol != IDENTITY_MANIFEST_PROTOCOL:
            # Audit j#96931 F9: an unknown protocol was accepted and canonicalised, so a
            # manifest this build cannot interpret would still have produced a digest and a
            # tagged action. Exact only, both when writing and when reading back.
            raise ValueError(
                f"unknown launch manifest protocol {protocol!r}; this build writes and "
                f"accepts exactly {IDENTITY_MANIFEST_PROTOCOL!r}"
            )
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
            if _manifest_table_state(conn) == "absent":
                raise StartupTransactionError(
                    f"{unavailable}, but the manifest table is absent; zero-actuation"
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
    # The real binding check (audit j#96928 F1). The digest check above only proves the
    # payload is INTACT, and `manifest_digest` is itself a mutable column — rewriting the
    # payload and the digest together passes it. What cannot be forged is that the action id
    # is a hash PREIMAGE of the digest, so the reader recomputes the id from the stored row
    # and requires a byte-exact match. That needs the nonce, which is why it is stored.
    verify_manifest_binding(
        action_id,
        workspace_id=values["workspace_id"],
        lane_id=values["lane_id"],
        providers=tuple(slot.provider for slot in manifest.slots),
        nonce=values["nonce"],
        digest=values["manifest_digest"],
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
    # Audit j#96928 F3: the manifest must be the WHOLE plan. Without this, an action scoped
    # to (codex, claude) could file a manifest naming only codex, and claude would carry no
    # receipt obligation at all while the action id still claimed the capability — a hole
    # in exactly the direction that fails open.
    providers = [_norm(slot.provider) for slot in manifest.slots]
    if len(providers) != len(set(providers)):
        raise StartupTransactionError(
            "the launch manifest names a provider twice; a plan with a duplicate slot has "
            "no single obligation per provider — nothing was started"
        )
    if tuple(sorted(set(providers))) != canonical.providers:
        raise StartupTransactionError(
            "the launch manifest's provider set is not exactly the action's requested set "
            "(missing or extra slots); nothing was started"
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


def verify_manifest_binding(
    action_id: str, *, workspace_id: str, lane_id: str, providers, nonce: str, digest: str
) -> None:
    """Recompute ``action_id`` from the stored row and require a byte-exact match.

    Audit j#96928 F1. The previous reader only checked that the payload reproduced the
    stored ``manifest_digest`` — but that column is itself mutable, so rewriting the payload
    and the digest together (keeping workspace/lane) passed. What makes the binding real is
    that the action id is a hash PREIMAGE of the digest, so the reader has to be able to
    recompute the id. That needs the nonce, which is why it is now stored beside the
    manifest: without it the binding was an assertion, not a check.
    """
    rederived = startup_action_id(
        StartupUnit(workspace_id=workspace_id, lane_id=lane_id, providers=tuple(providers)),
        nonce,
        capability=CAPABILITY_IDENTITY_RECEIPT,
        manifest_digest=digest,
    )
    if rederived != action_id:
        raise StartupTransactionError(
            f"{REASON_RECEIPT_REQUIREMENT_UNAVAILABLE}: startup action {action_id!r} is not "
            "the identity its stored manifest binding reproduces; zero-actuation"
        )


@dataclass(frozen=True)
class StartupUnit:
    """Local mirror of the fence's unit, so this leaf module needs no import back."""

    workspace_id: str
    lane_id: str
    providers: tuple

    def canonical(self) -> "StartupUnit":
        return StartupUnit(
            workspace_id=_norm(self.workspace_id),
            lane_id=_norm(self.lane_id),
            providers=tuple(sorted({_norm(p) for p in self.providers if _norm(p)})),
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
    nonce: str = "",
) -> None:
    """Write the action row and (when tagged) its manifest in ONE transaction.

    The atomicity is the requirement, not an optimisation (Design Answer j#96917 item 2).
    A crash between the two writes would leave either a tagged action with no manifest —
    which must be zero-actuation, and would otherwise have to be *guessed* at — or a
    manifest for an action that never existed. Committing them together removes both gaps,
    and both happen strictly before the reserve's first side effect.
    """
    if manifest is not None:
        # Exact probe BEFORE any mutation (audit j#96928 F5): create only when absent, and
        # refuse anything whose shape this build did not create.
        if _manifest_table_state(conn) == "absent":
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
            " protocol, slots, manifest_digest, nonce, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                canonical.workspace_id,
                canonical.lane_id,
                _norm(manifest.protocol),
                manifest_payload,
                manifest_digest,
                _norm(nonce),
                now,
            ),
        )
    conn.execute("COMMIT")


def identical_pending_replay(
    conn, *, action_id: str, canonical, manifest_digest: str, manifest_payload: str, nonce: str
) -> bool:
    """True iff this reserve is a byte-exact replay of an untouched tagged reservation.

    Every field is compared, including the ones a caller cannot see (the stored payload and
    the stored nonce), and the action must still be exactly as reserved — reserved phase,
    revision 1, no participants. A reservation that has already started something is NOT
    replayable: returning it would hand a second caller an action whose effects are
    already in flight.

    Never mutates. A divergent replay simply returns False and the caller refuses.
    """
    row = conn.execute(
        "SELECT workspace_id, lane_id, providers, phase, revision, participants"
        " FROM startup_actions WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    if row is None:
        return False
    workspace, lane, providers, phase, revision, participants = row
    if (
        workspace != canonical.workspace_id
        or lane != canonical.lane_id
        or providers != ",".join(canonical.providers)
        or _norm(phase) != "planned"
        or int(revision) != 1
    ):
        return False
    try:
        if json.loads(participants):
            return False
    except (ValueError, TypeError):
        return False
    if _manifest_table_state(conn) == "absent":
        return False
    stored = conn.execute(
        f"SELECT slots, manifest_digest, nonce FROM {_IDENTITY_MANIFEST_TABLE}"
        " WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    if stored is None:
        return False
    return (
        stored[0] == manifest_payload
        and stored[1] == manifest_digest
        and stored[2] == _norm(nonce)
    )


def reserve_or_replay(
    conn,
    *,
    action_id: str,
    canonical,
    phase: str,
    now: str,
    manifest,
    manifest_digest: str,
    manifest_payload: str,
    nonce: str,
) -> str:
    """Write the reservation, or recognise an exact replay. Returns the replayed id or "".

    One place decides, so the "already exists" branch and the write can never disagree
    about what an identical replay is. Audit j#96928 F4: an EXACT identical replay of a
    TAGGED reserve is idempotent — same unit, same nonce, same manifest, action untouched
    in its reserved state — because that is one action being retried, and refusing it would
    strand a crash-replay that had already written. Anything divergent is still a nonce
    reuse and refuses; nothing is ever updated or replaced. A LEGACY reserve keeps its
    existing contract exactly: a repeat is a reuse, full stop.
    """
    existing = conn.execute(
        "SELECT phase FROM startup_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    if existing is not None:
        if manifest is not None and identical_pending_replay(
            conn,
            action_id=action_id,
            canonical=canonical,
            manifest_digest=manifest_digest,
            manifest_payload=manifest_payload,
            nonce=nonce,
        ):
            return action_id
        raise StartupTransactionError(
            f"startup action {action_id!r} already exists (phase {existing[0]!r}); a nonce "
            "must never be reused — refusing to reserve over a recorded action"
        )
    write_reserved_action(
        conn,
        action_id=action_id,
        canonical=canonical,
        phase=phase,
        now=now,
        manifest=manifest,
        manifest_digest=manifest_digest,
        manifest_payload=manifest_payload,
        nonce=nonce,
    )
    return ""


def verify_supported_version(conn) -> int:
    """Accept a store version this runtime can read, or fail closed. Returns the version."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS:
        raise StartupTransactionError(
            f"startup transaction store schema {version!r} is not one this runtime "
            f"supports {list(STARTUP_TRANSACTION_FENCE_SUPPORTED_VERSIONS)}; fail closed "
            "rather than read an unknown shape"
        )
    return version


def verify_v2_manifest_shape(conn) -> None:
    """A v2 store MUST carry the manifest table, in exactly this build's shape.

    The difference from the v1 tolerance is the point (Design Answer j#96936 item 1): under
    v1 the sibling table is additive and its absence is simply "no manifests here". Under
    v2 it is part of the declared schema, so its absence is a PARTIAL schema and fails
    closed — otherwise a v2 store could claim the capability contract while carrying none
    of the rows that contract is about.
    """
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != 2:
        return
    if _manifest_table_state(conn) == "absent":
        raise StartupTransactionError(
            "the startup transaction authority declares schema v2 but is missing the "
            f"{_IDENTITY_MANIFEST_TABLE!r} table (partial schema); fail closed"
        )


def require_v2_for_tagged_reserve(conn) -> None:
    """A capability-tagged action may be reserved ONLY on a v2 store (j#96936 item 2).

    This is what makes the whole fail-closed argument hold. A tag written into a v1 store
    would be invisible to an older peer runtime, which reads v1 happily and never inspects
    an action id — so it could spend a receipt-capable action with no idea one existed. By
    refusing to write the tag until the store itself is v2, the only stores that ever
    contain tagged actions are stores an old runtime already rejects wholesale at its
    exact ``user_version == 1`` check.

    Zero-write and typed: the runtime NEVER migrates a shared home on its own. Upgrading is
    an all-consumers-stopped, backed-up, plan-checked operation owned by the #14838 offline
    rail (item 3), and a normal startup that quietly bumped a version would be exactly the
    implicit migration that rail exists to prevent.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != 2:
        raise StartupTransactionError(
            f"{REASON_OFFLINE_UPGRADE_REQUIRED}: this launch would record an identity "
            f"receipt obligation, which requires startup-transaction store schema v2, but "
            f"the store is v{version}. Nothing was started and nothing was written — the "
            "upgrade is an offline, all-consumers-stopped rollout, never something a "
            "launch performs on its own."
        )




# --- Offline v1 -> v2 migration primitive (Design Answer j#96936 items 3, 5, 7) ----------
#
# #14741 owns this PRIMITIVE and its regressions; #14838 owns the orchestration around it
# (stop every consumer, back up, migrate the sibling stores too, restart on an attested new
# binary, verify health, roll back). The real migration of a shared home runs only under
# that rail with exact owner approval — nothing here is invoked by a normal startup.

MIGRATION_OK = "migrated"
#: Already v2 and exactly the shape v2 requires. Idempotent replay of a completed rollout.
MIGRATION_ALREADY_V2 = "already_v2"
#: A consumer still holds the store. Migrating under a live peer is the one thing an
#: offline rollout must never do, so contention is a refusal and never a wait.
MIGRATION_LIVE_CONSUMER = "live_consumer"
#: The store already contains capability-tagged actions while claiming v1. Nothing this
#: build wrote could be in that state, so the store's history is not what it claims.
MIGRATION_TAGGED_ROWS_PRESENT = "tagged_rows_present"
#: The named sibling table exists in a shape this build did not create.
MIGRATION_FOREIGN_SIBLING = "foreign_sibling_schema"
#: The backup could not be produced. No backup, no migration.
MIGRATION_BACKUP_FAILED = "backup_failed"
#: The caller's migration plan is not the plan this store presents.
MIGRATION_PLAN_DRIFT = "plan_drift"


class StartupStoreMigrationRefused(StartupTransactionError):
    """An offline v1->v2 migration was refused. Carries a fixed reason; zero mutation."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def startup_store_migration_plan_digest(conn) -> str:
    """A digest of what the migration is ABOUT to act on, for the caller to pre-approve.

    Covers the facts a rollout plan is written against: the schema version, the action ids
    present, and whether the sibling table already exists. If any of them changed between
    the plan being approved and the migration running, the digest differs and the migration
    refuses — which is what "plan drift" means operationally.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    actions = [
        str(row[0])
        for row in conn.execute(
            "SELECT action_id FROM startup_actions ORDER BY action_id"
        ).fetchall()
    ]
    sibling = _manifest_table_state(conn)
    payload = json.dumps([version, actions, sibling], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refuse_if_tagged_rows(conn) -> None:
    for row in conn.execute("SELECT action_id FROM startup_actions").fetchall():
        try:
            capability = action_capability(row[0])
        except StartupTransactionError:
            raise StartupStoreMigrationRefused(
                MIGRATION_TAGGED_ROWS_PRESENT,
                "the store holds an action id this build cannot classify",
            )
        if capability != CAPABILITY_LEGACY:
            raise StartupStoreMigrationRefused(
                MIGRATION_TAGGED_ROWS_PRESENT,
                "the store already holds capability-tagged actions while declaring v1",
            )


def migrate_startup_store_v1_to_v2(fence, *, backup_path, expected_plan_digest: str = "") -> str:
    """Take a v1 startup store to v2, offline and fail-closed. Returns a fixed token.

    Every refusal happens BEFORE any mutation, and the migration itself is one transaction:
    create the sibling table if absent, then set ``user_version = 2``. A store left
    half-migrated would be the worst outcome available here — an old runtime would still
    accept it while a new one thinks the capability contract holds — so there is no
    intermediate state to be interrupted in.
    """
    import shutil

    backup = Path(backup_path)
    try:
        holder = fence._hold()
    except Exception as exc:  # noqa: BLE001 - contention is a refusal, never a wait
        raise StartupStoreMigrationRefused(MIGRATION_LIVE_CONSUMER, str(exc)) from exc
    with holder:
        with fence._connection("rw") as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 2:
                # Idempotent replay of a completed rollout; the connection's own `_verify`
                # already proved the v2 shape, so there is nothing left to do.
                return MIGRATION_ALREADY_V2
            if version != 1:
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT, f"store is v{version}, not v1"
                )
            # `_manifest_table_state` raises on a foreign/partial shape — surface it as the
            # migration's own typed refusal rather than a generic authority error.
            try:
                sibling = _manifest_table_state(conn)
            except StartupTransactionError as exc:
                raise StartupStoreMigrationRefused(
                    MIGRATION_FOREIGN_SIBLING, str(exc)
                ) from exc
            _refuse_if_tagged_rows(conn)
            actual_plan = startup_store_migration_plan_digest(conn)
            if expected_plan_digest and actual_plan != expected_plan_digest:
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT,
                    "the store is not in the state the approved migration plan described",
                )
            try:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fence.path, backup)
                if not backup.exists() or backup.stat().st_size <= 0:
                    raise OSError("backup is absent or empty")
            except OSError as exc:
                raise StartupStoreMigrationRefused(
                    MIGRATION_BACKUP_FAILED, str(exc)
                ) from exc
            try:
                conn.execute("BEGIN IMMEDIATE")
                if sibling == "absent":
                    conn.execute(_IDENTITY_MANIFEST_SQL)
                conn.execute("PRAGMA user_version = 2")
                conn.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT, f"the migration write failed ({exc})"
                ) from exc
    return MIGRATION_OK
