"""Exact-generation executable identity receipts + update-relaunch evidence (#14741).

The durable half of Design Answer j#96917, reworked to the corrections in j#96884, j#96899
and j#96966 C12-C16. It answers one question for a self-heal that fires *after* the pane it
would have read is gone: **which executable was the condemned generation running, and did
that generation actually show a provider update screen?**

Load-bearing, not a cache (audit j#96966 C12)
---------------------------------------------
The first cut called itself ``rebuildable_cache`` and swallowed every store error, so an
absent, corrupt or unwritable store read as "no evidence" and the heal proceeded as a
generic relaunch. That is exactly backwards: on a **receipt-capable** generation, being
unable to consult this store is the very state in which the #14741 loop was invisible.
Every failure here is a typed error, and a receipt-capable caller must treat it as
zero-launch / zero-self-heal.

Legacy and generic paths keep their byte-invariance a different way — by never reaching
this store at all. Capability is decided by the startup action's own shape (see
:mod:`.startup_action_capability`), OUTSIDE this file, so deleting this store cannot make a
receipt-capable action look like a pre-feature one.

Two phases, and why ``unbound_pending`` is not authority (j#96899)
------------------------------------------------------------------
A fresh sublane's lifecycle row is declared *after* the launch completes, and a default lane
has none at all — so requiring the real lane generation at reserve time would refuse every
fresh launch. The reservation is therefore a NON-authority record with a typed
``unbound_pending`` lifecycle axis: it pins the exact startup action, workspace, lane,
provider, assigned name and the identity the preflight already resolved, and claims nothing
about the lane generation. A blank is never stored as a generation.

``attested`` is the authority, and it is reached only once the caller can present the whole
composite proof: the exact startup participant, the finalized launch generation, a
generation-matched attestation, the live locator, and the lane's actual lifecycle
generation/revision.

Staleness is a join, never a clock (audit j#96966 C16)
------------------------------------------------------
Evidence is live iff its receipt's ``(lane_generation, lifecycle_revision)`` byte-equals the
pair the caller read from the lane's own lifecycle authority. The first cut compared
``attested_at`` timestamps, which lets a clock rollback, a same-microsecond tie, or a NULL
keep stale evidence alive — and stale evidence arming a relaunch is the loop this ticket is
about.

No second launch debt (j#96917 item 3)
--------------------------------------
Evidence goes ``bound -> consumed``, and consumption happens only AFTER a verified relaunch.
The obligation to *perform* a relaunch stays in the existing replacement-transaction
participant DAG; duplicating it here would create two authorities that can disagree about
whether a launch is owed.

Privacy: every column is a fixed token, an identity segment, an opaque digest, or a
timestamp kept for diagnosis only and never used to decide anything.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

LAUNCH_IDENTITY_RECEIPT_FILENAME = "launch-identity-receipt.sqlite"
LAUNCH_IDENTITY_RECEIPT_SCHEMA_VERSION = 1
#: NOT ``rebuildable_cache``. A receipt-capable generation that cannot consult this store is
#: zero-actuation; the store is part of the safety argument, not a diagnostic.
LAUNCH_IDENTITY_RECEIPT_RECOVERY_POLICY = "load_bearing_fail_closed"

#: Reserved before the lane has a generation to bind to. NOT authority (j#96899).
RECEIPT_UNBOUND_PENDING = "unbound_pending"
#: The launch came up and the whole composite proof agreed. The authority phase.
RECEIPT_ATTESTED = "attested"

EVIDENCE_BOUND = "bound"
EVIDENCE_CONSUMED = "consumed"

# --- Typed outcomes. Fixed tokens, safe on a durable record. -----------------------------
RESERVE_OK = "reserved"
#: An identical reservation already exists — one action retried, not a second action.
RESERVE_IDENTICAL_REPLAY = "identical_replay"

FINALIZE_OK = "attested"
#: No matching ``unbound_pending`` row (absent, already attested, or a different identity).
FINALIZE_NO_PENDING_MATCH = "no_pending_match"

BIND_OK = "bound"
BIND_NO_ATTESTED_RECEIPT = "no_attested_receipt"
BIND_IDENTITY_MISMATCH = "identity_mismatch"
BIND_ALREADY_BOUND = "already_bound"
BIND_ALREADY_CONSUMED = "already_consumed"

CONSUME_OK = "consumed"
CONSUME_ABSENT = "absent"
CONSUME_REPLAY = "replay"
CONSUME_FOREIGN = "foreign"

_RECEIPTS = "launch_identity_receipts"
_EVIDENCE = "update_relaunch_evidence"

_RECEIPTS_SQL = f"""
CREATE TABLE {_RECEIPTS} (
    workspace_id       TEXT NOT NULL,
    lane_id            TEXT NOT NULL,
    provider           TEXT NOT NULL,
    assigned_name      TEXT NOT NULL,
    startup_action_id  TEXT NOT NULL,
    identity_digest    TEXT NOT NULL,
    phase              TEXT NOT NULL,
    lane_generation    TEXT NOT NULL,
    lifecycle_revision TEXT NOT NULL,
    locator            TEXT NOT NULL,
    reserved_at        TEXT NOT NULL,
    attested_at        TEXT NOT NULL,
    PRIMARY KEY (workspace_id, lane_id, provider, assigned_name, startup_action_id)
)
"""

_EVIDENCE_SQL = f"""
CREATE TABLE {_EVIDENCE} (
    workspace_id       TEXT NOT NULL,
    lane_id            TEXT NOT NULL,
    provider           TEXT NOT NULL,
    assigned_name      TEXT NOT NULL,
    startup_action_id  TEXT NOT NULL,
    blocker_id         TEXT NOT NULL,
    identity_digest    TEXT NOT NULL,
    phase              TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    consumed_at        TEXT NOT NULL,
    consumed_by        TEXT NOT NULL,
    PRIMARY KEY (workspace_id, lane_id, provider, assigned_name, startup_action_id)
)
"""

_RECEIPT_COLUMNS = (
    "workspace_id", "lane_id", "provider", "assigned_name", "startup_action_id",
    "identity_digest", "phase", "lane_generation", "lifecycle_revision", "locator",
    "reserved_at", "attested_at",
)
_EVIDENCE_COLUMNS = (
    "workspace_id", "lane_id", "provider", "assigned_name", "startup_action_id",
    "blocker_id", "identity_digest", "phase", "observed_at", "consumed_at", "consumed_by",
)

_KEY_COLUMNS = ("workspace_id", "lane_id", "provider", "assigned_name", "startup_action_id")
_KEY_WHERE = " AND ".join(f"{c} = ?" for c in _KEY_COLUMNS)


class LaunchIdentityReceiptError(RuntimeError):
    """The receipt authority is absent, malformed, or a write violated its contract.

    Always fail-closed for a receipt-capable generation: the caller must treat this as
    zero-launch / zero-self-heal, never as "no evidence".
    """


def launch_identity_receipt_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / LAUNCH_IDENTITY_RECEIPT_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _token(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise LaunchIdentityReceiptError(f"{field} is not a text token")
    if value != value.strip():
        raise LaunchIdentityReceiptError(f"{field} has surrounding whitespace")
    if not value:
        raise LaunchIdentityReceiptError(f"{field} is empty")
    return value


def _canonical(sql: str) -> str:
    return " ".join(sql.split()).strip()


@dataclass(frozen=True)
class GenerationKey:
    """The exact generation a receipt and its evidence are bound to.

    All five parts are required. A partial key is not a weaker key — it is another lane's
    row waiting to be mistaken for this one.
    """

    workspace_id: str
    lane_id: str
    provider: str
    assigned_name: str
    startup_action_id: str

    def as_row(self) -> tuple:
        return (
            _token(self.workspace_id, "workspace_id"),
            _token(self.lane_id, "lane_id"),
            _token(self.provider, "provider"),
            _token(self.assigned_name, "assigned_name"),
            _token(self.startup_action_id, "startup_action_id"),
        )


@dataclass(frozen=True)
class IdentityReceipt:
    key: GenerationKey
    identity_digest: str
    phase: str
    lane_generation: str = ""
    lifecycle_revision: str = ""
    locator: str = ""
    reserved_at: str = ""
    attested_at: str = ""

    @property
    def attested(self) -> bool:
        return self.phase == RECEIPT_ATTESTED


@dataclass(frozen=True)
class UpdateRelaunchEvidence:
    key: GenerationKey
    blocker_id: str
    identity_digest: str
    phase: str
    observed_at: str = ""
    consumed_at: str = ""
    consumed_by: str = ""

    @property
    def bound(self) -> bool:
        return self.phase == EVIDENCE_BOUND


def _decode_receipt(row: tuple) -> IdentityReceipt:
    v = dict(zip(_RECEIPT_COLUMNS, row))
    return IdentityReceipt(
        key=GenerationKey(*(v[c] for c in _KEY_COLUMNS)),
        identity_digest=v["identity_digest"],
        phase=v["phase"],
        lane_generation=v["lane_generation"],
        lifecycle_revision=v["lifecycle_revision"],
        locator=v["locator"],
        reserved_at=v["reserved_at"],
        attested_at=v["attested_at"],
    )


def _decode_evidence(row: tuple) -> UpdateRelaunchEvidence:
    v = dict(zip(_EVIDENCE_COLUMNS, row))
    return UpdateRelaunchEvidence(
        key=GenerationKey(*(v[c] for c in _KEY_COLUMNS)),
        blocker_id=v["blocker_id"],
        identity_digest=v["identity_digest"],
        phase=v["phase"],
        observed_at=v["observed_at"],
        consumed_at=v["consumed_at"],
        consumed_by=v["consumed_by"],
    )


class LaunchIdentityReceiptStore:
    """Fail-closed store for identity receipts and update-relaunch evidence."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None):
        self.path = Path(path) if path is not None else launch_identity_receipt_path(home)

    # --- connection / schema ----------------------------------------------------------

    def _create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        try:
            conn.executescript(_RECEIPTS_SQL + ";" + _EVIDENCE_SQL)
            conn.execute(f"PRAGMA user_version = {LAUNCH_IDENTITY_RECEIPT_SCHEMA_VERSION}")
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            # A chmod failure is NOT success: the file carries identity segments.
            raise LaunchIdentityReceiptError(
                f"the receipt authority could not be made private ({exc})"
            ) from exc

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if not self.path.exists():
            if not create:
                raise LaunchIdentityReceiptError(
                    f"the launch identity receipt authority {self.path.name} is absent; a "
                    "receipt-capable generation is zero-actuation without it — an absent "
                    "authority is never read as 'no evidence'"
                )
            self._create()
        try:
            conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        except sqlite3.DatabaseError as exc:
            raise LaunchIdentityReceiptError(
                f"the receipt authority {self.path.name} could not be opened ({exc})"
            ) from exc
        try:
            self._verify(conn)
        except Exception:
            conn.close()
            raise
        return conn

    def _verify(self, conn: sqlite3.Connection) -> None:
        """Exact schema signature, or fail closed (audit j#96966 C16).

        Table NAMES are not a schema. The stored DDL text is compared for both tables, and
        any attached index or trigger is refused — those are separate objects a name check
        cannot see, and a store this build did not create is not one it may write into.
        """
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            objects = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE sql IS NOT NULL"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise LaunchIdentityReceiptError(
                f"the receipt authority {self.path.name} is unreadable ({exc})"
            ) from exc
        if version != LAUNCH_IDENTITY_RECEIPT_SCHEMA_VERSION:
            raise LaunchIdentityReceiptError(
                f"the receipt authority is schema v{version}, expected "
                f"v{LAUNCH_IDENTITY_RECEIPT_SCHEMA_VERSION}; refusing to read it"
            )
        expected = {
            _RECEIPTS: _canonical(_RECEIPTS_SQL),
            _EVIDENCE: _canonical(_EVIDENCE_SQL),
        }
        seen: dict = {}
        for kind, name, tbl_name, sql in objects:
            if kind in ("index", "trigger") and str(tbl_name) in expected:
                raise LaunchIdentityReceiptError(
                    f"the receipt authority carries an attached {kind} {name!r} this build "
                    "did not create; refusing to read or write it"
                )
            if kind == "table":
                seen[str(name)] = _canonical(str(sql))
        for table, ddl in expected.items():
            if seen.get(table) != ddl:
                raise LaunchIdentityReceiptError(
                    f"the receipt authority's {table!r} schema is not the one this build "
                    "creates (missing, or drifted shape/constraints); refusing to use it"
                )

    # --- receipts ---------------------------------------------------------------------

    def reserve(self, key: GenerationKey, *, identity_digest: str) -> str:
        """Record the identity this launch is about to start as ``unbound_pending``.

        NON-authority by construction (j#96899): the lane has no generation yet, and a blank
        is never stored as one. Insert-or-identical CAS — ``INSERT OR REPLACE`` is refused
        (audit j#96966 C16), so a divergent reservation on the same key is a typed zero-write
        rather than a silent overwrite of what the first one promised.
        """
        row = key.as_row()
        digest = _token(identity_digest, "identity_digest")
        now = _utc_now()
        with closing(self._connect(create=True)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    f"SELECT identity_digest, phase FROM {_RECEIPTS} WHERE {_KEY_WHERE}",
                    row,
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    if existing[0] == digest and existing[1] == RECEIPT_UNBOUND_PENDING:
                        return RESERVE_IDENTICAL_REPLAY
                    raise LaunchIdentityReceiptError(
                        f"a receipt already exists for this generation in phase "
                        f"{existing[1]!r} with a different identity; refusing to overwrite "
                        "what an earlier reservation promised"
                    )
                conn.execute(
                    f"INSERT INTO {_RECEIPTS} ({', '.join(_RECEIPT_COLUMNS)})"
                    " VALUES (?,?,?,?,?,?,?,'','','',?,'')",
                    (*row, digest, RECEIPT_UNBOUND_PENDING, now),
                )
                conn.execute("COMMIT")
            except LaunchIdentityReceiptError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return RESERVE_OK

    def finalize(
        self,
        key: GenerationKey,
        *,
        identity_digest: str,
        locator: str,
        lane_generation: str,
        lifecycle_revision: str,
        composite_proof: bool,
    ) -> str:
        """Promote ``unbound_pending`` -> ``attested`` with the lane's ACTUAL generation.

        ``composite_proof`` is the caller's assertion that the whole join held for this slot
        (audit j#96966 C13): the exact startup participant, the finalized launch generation,
        a generation-matched attestation, and the live locator. This store cannot check
        those — they live in other authorities — so it refuses to attest without the claim
        and requires every value it CAN check to be present and exact.

        A blank generation, revision or locator is refused: an authority row with a blank
        axis is not a weaker authority, it is a row that cannot be joined against anything.
        """
        row = key.as_row()
        digest = _token(identity_digest, "identity_digest")
        generation = _token(lane_generation, "lane_generation")
        revision = _token(lifecycle_revision, "lifecycle_revision")
        live_locator = _token(locator, "locator")
        if composite_proof is not True:
            raise LaunchIdentityReceiptError(
                "refusing to attest a receipt without the composite launch proof "
                "(startup participant + launch generation + attestation + live locator)"
            )
        now = _utc_now()
        with closing(self._connect(create=False)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    f"UPDATE {_RECEIPTS} SET phase = ?, lane_generation = ?,"
                    " lifecycle_revision = ?, locator = ?, attested_at = ?"
                    f" WHERE {_KEY_WHERE} AND phase = ? AND identity_digest = ?",
                    (
                        RECEIPT_ATTESTED, generation, revision, live_locator, now,
                        *row, RECEIPT_UNBOUND_PENDING, digest,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            # The rowcount IS the CAS result and is checked: a zero-row UPDATE reporting
            # success is how an unproven generation would become an authority.
            return FINALIZE_OK if cursor.rowcount == 1 else FINALIZE_NO_PENDING_MATCH

    def read_receipt(self, key: GenerationKey) -> Optional[IdentityReceipt]:
        with closing(self._connect(create=False)) as conn:
            found = conn.execute(
                f"SELECT {', '.join(_RECEIPT_COLUMNS)} FROM {_RECEIPTS} WHERE {_KEY_WHERE}",
                key.as_row(),
            ).fetchone()
        return _decode_receipt(found) if found else None

    # --- evidence ---------------------------------------------------------------------

    def bind_evidence(
        self, key: GenerationKey, *, blocker_id: str, identity_digest: str
    ) -> str:
        """CAS-bind update evidence onto this generation's ATTESTED receipt.

        Keyed on the EXACT generation (audit j#96966 C14). The first cut looked the receipt
        up by provider + locator, so a reused locator could attach a live screen to a stale
        attested row from an earlier generation. A locator is a pane name, not an identity.
        """
        row = key.as_row()
        digest = _token(identity_digest, "identity_digest")
        blocker = _token(blocker_id, "blocker_id")
        now = _utc_now()
        with closing(self._connect(create=False)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                receipt = conn.execute(
                    f"SELECT phase, identity_digest FROM {_RECEIPTS} WHERE {_KEY_WHERE}",
                    row,
                ).fetchone()
                if receipt is None or receipt[0] != RECEIPT_ATTESTED:
                    conn.execute("ROLLBACK")
                    return BIND_NO_ATTESTED_RECEIPT
                if receipt[1] != digest:
                    conn.execute("ROLLBACK")
                    return BIND_IDENTITY_MISMATCH
                existing = conn.execute(
                    f"SELECT phase, blocker_id, identity_digest FROM {_EVIDENCE}"
                    f" WHERE {_KEY_WHERE}",
                    row,
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    if existing[0] == EVIDENCE_CONSUMED:
                        return BIND_ALREADY_CONSUMED
                    if existing[0] != EVIDENCE_BOUND:
                        raise LaunchIdentityReceiptError(
                            f"evidence for this generation is in unknown phase "
                            f"{existing[0]!r}; refusing to interpret it"
                        )
                    if existing[1] != blocker or existing[2] != digest:
                        raise LaunchIdentityReceiptError(
                            "evidence for this generation is already bound to a different "
                            "screen or identity; refusing to overwrite it"
                        )
                    return BIND_ALREADY_BOUND
                conn.execute(
                    f"INSERT INTO {_EVIDENCE} ({', '.join(_EVIDENCE_COLUMNS)})"
                    " VALUES (?,?,?,?,?,?,?,?,?,'','')",
                    (*row, blocker, digest, EVIDENCE_BOUND, now),
                )
                conn.execute("COMMIT")
            except LaunchIdentityReceiptError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return BIND_OK

    def read_bound_evidence(
        self,
        *,
        workspace_id: str,
        lane_id: str,
        provider: str,
        lane_generation: str,
        lifecycle_revision: str,
    ) -> Optional[UpdateRelaunchEvidence]:
        """The lane's live evidence for ``provider``, or ``None``.

        Live means: bound (not consumed), on an ATTESTED receipt, whose
        ``(lane_generation, lifecycle_revision)`` byte-equals the pair the caller read from
        the lane's own lifecycle authority.

        That join replaces the first cut's ``attested_at`` comparison (audit j#96966 C16). A
        wall clock decides staleness by whichever timestamp happens to be larger, so a clock
        rollback, a same-microsecond tie, or a NULL kept stale evidence alive — and stale
        evidence arming a relaunch is the loop this whole ticket is about.
        """
        with closing(self._connect(create=False)) as conn:
            found = conn.execute(
                f"SELECT {', '.join('e.' + c for c in _EVIDENCE_COLUMNS)}"
                f" FROM {_EVIDENCE} AS e JOIN {_RECEIPTS} AS r"
                "  ON r.workspace_id = e.workspace_id AND r.lane_id = e.lane_id"
                "  AND r.provider = e.provider AND r.assigned_name = e.assigned_name"
                "  AND r.startup_action_id = e.startup_action_id"
                " WHERE e.workspace_id = ? AND e.lane_id = ? AND e.provider = ?"
                "  AND e.phase = ? AND r.phase = ?"
                "  AND r.lane_generation = ? AND r.lifecycle_revision = ?",
                (
                    _token(workspace_id, "workspace_id"),
                    _token(lane_id, "lane_id"),
                    _token(provider, "provider"),
                    EVIDENCE_BOUND,
                    RECEIPT_ATTESTED,
                    _token(lane_generation, "lane_generation"),
                    _token(lifecycle_revision, "lifecycle_revision"),
                ),
            ).fetchall()
        if not found:
            return None
        if len(found) > 1:
            raise LaunchIdentityReceiptError(
                "more than one live evidence row matches this lane generation; refusing to "
                "pick one"
            )
        return _decode_evidence(found[0])

    def consume_evidence(self, key: GenerationKey, *, consumed_by: str) -> str:
        """CAS ``bound`` -> ``consumed``, attributed to an exact durable action id.

        Called only AFTER a verified relaunch (audit j#96966 C15). The first cut consumed
        BEFORE the replacement action started and ignored the outcome, so a crash between
        the two lost the evidence while the launch never happened — and the existing
        ``launch_owed`` debt had nothing left to re-arm from.

        ``consumed_by`` must be a durable action id. The first cut put a worktree path in
        it, which is a location, not an actor.
        """
        row = key.as_row()
        actor = _token(consumed_by, "consumed_by")
        if os.sep in actor:
            raise LaunchIdentityReceiptError(
                "consumed_by must be a durable action id, not a path"
            )
        now = _utc_now()
        with closing(self._connect(create=False)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    f"SELECT phase, consumed_by FROM {_EVIDENCE} WHERE {_KEY_WHERE}", row
                ).fetchone()
                if existing is None:
                    conn.execute("ROLLBACK")
                    return CONSUME_ABSENT
                if existing[0] == EVIDENCE_CONSUMED:
                    conn.execute("ROLLBACK")
                    return CONSUME_REPLAY if existing[1] == actor else CONSUME_FOREIGN
                if existing[0] != EVIDENCE_BOUND:
                    conn.execute("ROLLBACK")
                    raise LaunchIdentityReceiptError(
                        f"evidence is in unknown phase {existing[0]!r}; refusing to consume"
                    )
                cursor = conn.execute(
                    f"UPDATE {_EVIDENCE} SET phase = ?, consumed_at = ?, consumed_by = ?"
                    f" WHERE {_KEY_WHERE} AND phase = ?",
                    (EVIDENCE_CONSUMED, now, actor, *row, EVIDENCE_BOUND),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    raise LaunchIdentityReceiptError(
                        "the evidence consume matched no row; fail closed rather than "
                        "report a consume that did not happen"
                    )
                conn.execute("COMMIT")
            except LaunchIdentityReceiptError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return CONSUME_OK


__all__ = (
    "BIND_ALREADY_BOUND",
    "BIND_ALREADY_CONSUMED",
    "BIND_IDENTITY_MISMATCH",
    "BIND_NO_ATTESTED_RECEIPT",
    "BIND_OK",
    "CONSUME_ABSENT",
    "CONSUME_FOREIGN",
    "CONSUME_OK",
    "CONSUME_REPLAY",
    "EVIDENCE_BOUND",
    "EVIDENCE_CONSUMED",
    "FINALIZE_NO_PENDING_MATCH",
    "FINALIZE_OK",
    "GenerationKey",
    "IdentityReceipt",
    "LAUNCH_IDENTITY_RECEIPT_FILENAME",
    "LAUNCH_IDENTITY_RECEIPT_RECOVERY_POLICY",
    "LAUNCH_IDENTITY_RECEIPT_SCHEMA_VERSION",
    "LaunchIdentityReceiptError",
    "LaunchIdentityReceiptStore",
    "RECEIPT_ATTESTED",
    "RECEIPT_UNBOUND_PENDING",
    "RESERVE_IDENTICAL_REPLAY",
    "RESERVE_OK",
    "UpdateRelaunchEvidence",
    "launch_identity_receipt_path",
)
