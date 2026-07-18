"""Action-bound replacement receipts for a v1 identity-attestation store (#13933).

The shared identity-attestation store deliberately stays at its on-disk version while
older installed launchers are live (Redmine #13882).  Its v1 shape therefore cannot
carry ``replacement_action_id``.  General replacement launches remain refused there:
silently dropping that field would make a new process unverifiable.

The reviewed bound-pair convergence rail has stronger evidence than a general launch.
It owns an immutable replacement action and the startup transaction records the exact
``agent start`` participant immediately after the provider returns a locator.  For that
one rail, a replacement can be decomposed safely into:

1. a normal, v1-shaped self-attestation write (no migration and no dropped field), and
2. this separate action-binding projection, written only after the startup receipt and
   the v1 row agree on assigned name, identity, live locator, and observation generation.

Readers accept the projection only while the main store is still recognized v1 and all
of those fields still match.  A migration to v2, another process generation, another
action, a torn/unknown binding schema, or a missing receipt therefore fails closed.

The file contains identity tokens and timestamps only.  It has no argv, environment,
credential, message, or pane-content field.  Initial publication is atomic and mode
``0600``; updates use SQLite ``BEGIN IMMEDIATE`` transactions.  This store never migrates
or repairs an unknown shape.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    STORE_RECOGNIZED,
    probe_store_schema,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

HERDR_IDENTITY_REPLACEMENT_BINDING_FILENAME = (
    "herdr-identity-attestation-replacement-bindings.sqlite"
)
HERDR_IDENTITY_REPLACEMENT_BINDING_SCHEMA_VERSION = 1

BINDING_RESERVED = "reserved"
BINDING_BOUND = "bound"

_TABLE = "replacement_action_bindings"
_COLUMNS = (
    "action_id",
    "assigned_name",
    "workspace_id",
    "role",
    "lane_id",
    "old_locator",
    "startup_nonce",
    "startup_action_id",
    "phase",
    "locator",
    "attested_at",
    "created_at",
    "bound_at",
)
_EXPECTED_INFO = (
    ("action_id", "TEXT", 1, 1),
    ("assigned_name", "TEXT", 1, 2),
    ("workspace_id", "TEXT", 1, 0),
    ("role", "TEXT", 1, 0),
    ("lane_id", "TEXT", 1, 0),
    ("old_locator", "TEXT", 1, 0),
    ("startup_nonce", "TEXT", 1, 0),
    ("startup_action_id", "TEXT", 1, 0),
    ("phase", "TEXT", 1, 0),
    ("locator", "TEXT", 1, 0),
    ("attested_at", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
    ("bound_at", "TEXT", 1, 0),
)
_CREATE_SQL = f"""
CREATE TABLE {_TABLE} (
    action_id TEXT NOT NULL,
    assigned_name TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    role TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    old_locator TEXT NOT NULL,
    startup_nonce TEXT NOT NULL,
    startup_action_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    locator TEXT NOT NULL DEFAULT '',
    attested_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    bound_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (action_id, assigned_name),
    CHECK (phase IN ('reserved', 'bound')),
    CHECK (
        (phase = 'reserved' AND locator = '' AND attested_at = '' AND bound_at = '')
        OR
        (phase = 'bound' AND locator <> '' AND attested_at <> '' AND bound_at <> '')
    )
)
"""


class ReplacementActionBindingError(RuntimeError):
    """The action-binding authority is absent, malformed, or conflicts."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _token(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ReplacementActionBindingError(f"{field} is not a text token")
    if value != value.strip() or (not value and not allow_empty):
        raise ReplacementActionBindingError(
            f"{field} is empty or has surrounding whitespace"
        )
    return value


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.DatabaseError:
        pass


def herdr_identity_replacement_binding_path(
    home: Path | None = None,
) -> Path:
    return (home or mozyo_bridge_home()) / HERDR_IDENTITY_REPLACEMENT_BINDING_FILENAME


def selected_attestation_store_is_v1(home: Path | None = None) -> bool:
    """Whether the selected main store is the exact recognized v1 shape (read-only)."""
    observation = probe_store_schema(herdr_identity_attestation_path(home))
    return bool(
        observation.state == STORE_RECOGNIZED and observation.version == 1
    )


@dataclass(frozen=True)
class ReplacementActionBinding:
    action_id: str
    assigned_name: str
    workspace_id: str
    role: str
    lane_id: str
    old_locator: str
    startup_nonce: str
    startup_action_id: str
    phase: str
    locator: str = ""
    attested_at: str = ""
    created_at: str = ""
    bound_at: str = ""

    def as_payload(self) -> dict:
        return {
            "action_id": self.action_id,
            "assigned_name": self.assigned_name,
            "workspace_id": self.workspace_id,
            "role": self.role,
            "lane_id": self.lane_id,
            "old_locator": self.old_locator,
            "startup_nonce": self.startup_nonce,
            "startup_action_id": self.startup_action_id,
            "phase": self.phase,
            "locator": self.locator,
            "attested_at": self.attested_at,
            "created_at": self.created_at,
            "bound_at": self.bound_at,
        }


def _decode(row: tuple) -> ReplacementActionBinding:
    if len(row) != len(_COLUMNS):
        raise ReplacementActionBindingError("binding row has an unexpected width")
    values = tuple(
        _token(
            value,
            name,
            allow_empty=name in {"locator", "attested_at", "bound_at"},
        )
        for name, value in zip(_COLUMNS, row)
    )
    binding = ReplacementActionBinding(**dict(zip(_COLUMNS, values)))
    if binding.phase not in (BINDING_RESERVED, BINDING_BOUND):
        raise ReplacementActionBindingError("binding row has an unknown phase")
    if binding.phase == BINDING_RESERVED and any(
        (binding.locator, binding.attested_at, binding.bound_at)
    ):
        raise ReplacementActionBindingError("reserved binding carries bound fields")
    if binding.phase == BINDING_BOUND and not all(
        (binding.locator, binding.attested_at, binding.bound_at)
    ):
        raise ReplacementActionBindingError("bound binding is incomplete")
    return binding


class HerdrIdentityReplacementBindingStore:
    """Fail-closed action binding store for v1 main-attestation generations."""

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or herdr_identity_replacement_binding_path(home)

    @staticmethod
    def new_startup_nonce() -> str:
        return secrets.token_hex(24)

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        info = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5])
            for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()
        )
        if (
            version != HERDR_IDENTITY_REPLACEMENT_BINDING_SCHEMA_VERSION
            or tables != (_TABLE,)
            or info != _EXPECTED_INFO
        ):
            raise ReplacementActionBindingError(
                "replacement action binding store has an unknown or partial schema; "
                "refusing to migrate or repair it implicitly"
            )

    def _validate_file_security(self) -> None:
        """Require an operator-owned regular 0600 file; never repair it implicitly."""
        try:
            metadata = self.path.lstat()
        except OSError as exc:
            raise ReplacementActionBindingError(
                "replacement action binding store metadata is unreadable"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ReplacementActionBindingError(
                "replacement action binding store is not a regular file"
            )
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ReplacementActionBindingError(
                "replacement action binding store is not owned by the current operator"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ReplacementActionBindingError(
                "replacement action binding store permissions are not exactly 0600"
            )

    def _publish_fresh(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(
            prefix=".replacement-bindings-", suffix=".sqlite", dir=self.path.parent
        )
        candidate = Path(raw)
        os.close(fd)
        try:
            os.chmod(candidate, 0o600)
            conn = sqlite3.connect(candidate)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute(
                    f"PRAGMA user_version={HERDR_IDENTITY_REPLACEMENT_BINDING_SCHEMA_VERSION}"
                )
                conn.execute(_CREATE_SQL)
                conn.commit()
                self._validate_schema(conn)
            finally:
                conn.close()
            try:
                os.link(candidate, self.path)
            except FileExistsError:
                pass  # an atomically-published peer won; validate it below
        finally:
            candidate.unlink(missing_ok=True)

    def _connect_existing(self, *, readonly: bool) -> sqlite3.Connection:
        self._validate_file_security()
        mode = "ro" if readonly else "rw"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode={mode}", uri=True)
            conn.execute("PRAGMA busy_timeout=2000")
            self._validate_schema(conn)
            return conn
        except ReplacementActionBindingError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            if conn is not None:
                conn.close()
            raise ReplacementActionBindingError(
                "replacement action binding store is unreadable"
            ) from exc
        except BaseException:
            if conn is not None:
                conn.close()
            raise

    def _ensure_store(self) -> None:
        try:
            if not self.path.exists():
                self._publish_fresh()
            conn = self._connect_existing(readonly=True)
            conn.close()
        except ReplacementActionBindingError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ReplacementActionBindingError(
                "replacement action binding store could not be published atomically"
            ) from exc

    @staticmethod
    def _row(conn: sqlite3.Connection, action_id: str, assigned_name: str):
        return conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} "
            "WHERE action_id=? AND assigned_name=?",
            (action_id, assigned_name),
        ).fetchone()

    def read(
        self, action_id: str, assigned_name: str
    ) -> Optional[ReplacementActionBinding]:
        action = _token(action_id, "action_id")
        name = _token(assigned_name, "assigned_name")
        if not self.path.exists():
            return None
        try:
            conn = self._connect_existing(readonly=True)
            try:
                conn.execute("BEGIN")
                row = self._row(conn, action, name)
            finally:
                conn.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ReplacementActionBindingError(
                "replacement action binding store is unreadable"
            ) from exc
        return _decode(row) if row is not None else None

    def reserve(
        self,
        *,
        action_id: str,
        assigned_name: str,
        workspace_id: str,
        role: str,
        lane_id: str,
        old_locator: str,
        startup_nonce: str,
        startup_action_id: str,
    ) -> ReplacementActionBinding:
        fields = {
            "action_id": _token(action_id, "action_id"),
            "assigned_name": _token(assigned_name, "assigned_name"),
            "workspace_id": _token(workspace_id, "workspace_id"),
            "role": _token(role, "role"),
            "lane_id": _token(lane_id, "lane_id"),
            "old_locator": _token(old_locator, "old_locator"),
            "startup_nonce": _token(startup_nonce, "startup_nonce"),
            "startup_action_id": _token(startup_action_id, "startup_action_id"),
        }
        self._ensure_store()
        conn = self._connect_existing(readonly=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, fields["action_id"], fields["assigned_name"])
            if row is not None:
                existing = _decode(row)
                immutable = (
                    existing.workspace_id,
                    existing.role,
                    existing.lane_id,
                    existing.old_locator,
                    existing.startup_nonce,
                    existing.startup_action_id,
                )
                requested = (
                    fields["workspace_id"],
                    fields["role"],
                    fields["lane_id"],
                    fields["old_locator"],
                    fields["startup_nonce"],
                    fields["startup_action_id"],
                )
                if immutable != requested:
                    raise ReplacementActionBindingError(
                        "the same replacement action is already reserved for a different "
                        "identity or generation"
                    )
                conn.commit()
                return existing
            now = _utc_now()
            conn.execute(
                f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                (
                    fields["action_id"], fields["assigned_name"], fields["workspace_id"],
                    fields["role"], fields["lane_id"], fields["old_locator"],
                    fields["startup_nonce"], fields["startup_action_id"],
                    BINDING_RESERVED, "", "", now, "",
                ),
            )
            conn.commit()
            return ReplacementActionBinding(
                **fields, phase=BINDING_RESERVED, created_at=now
            )
        except ReplacementActionBindingError:
            _rollback_quietly(conn)
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            _rollback_quietly(conn)
            raise ReplacementActionBindingError(
                "replacement binding reservation write failed"
            ) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()

    def replace_rolled_back_reservation(
        self,
        current: ReplacementActionBinding,
        *,
        startup_nonce: str,
        startup_action_id: str,
    ) -> ReplacementActionBinding:
        """CAS one still-reserved, externally-proven-rolled-back launch attempt.

        The caller owns the startup-transaction authority check.  This store only
        performs the byte-exact compare-and-set, so a concurrent bind/replacement or a
        foreign reservation can never be overwritten.
        """
        if current.phase != BINDING_RESERVED:
            raise ReplacementActionBindingError(
                "only a reserved replacement binding can change launch attempt"
            )
        nonce = _token(startup_nonce, "startup_nonce")
        startup_id = _token(startup_action_id, "startup_action_id")
        conn = self._connect_existing(readonly=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, current.action_id, current.assigned_name)
            if row is None or _decode(row) != current:
                raise ReplacementActionBindingError(
                    "binding reservation changed before its rolled-back attempt could be replaced"
                )
            conn.execute(
                f"UPDATE {_TABLE} SET startup_nonce=?, startup_action_id=? "
                "WHERE action_id=? AND assigned_name=? AND phase=? "
                "AND startup_nonce=? AND startup_action_id=?",
                (
                    nonce,
                    startup_id,
                    current.action_id,
                    current.assigned_name,
                    BINDING_RESERVED,
                    current.startup_nonce,
                    current.startup_action_id,
                ),
            )
            if conn.total_changes != 1:
                raise ReplacementActionBindingError(
                    "binding launch-attempt compare-and-set was refused"
                )
            conn.commit()
            return ReplacementActionBinding(
                action_id=current.action_id,
                assigned_name=current.assigned_name,
                workspace_id=current.workspace_id,
                role=current.role,
                lane_id=current.lane_id,
                old_locator=current.old_locator,
                startup_nonce=nonce,
                startup_action_id=startup_id,
                phase=BINDING_RESERVED,
                created_at=current.created_at,
            )
        except ReplacementActionBindingError:
            _rollback_quietly(conn)
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            _rollback_quietly(conn)
            raise ReplacementActionBindingError(
                "replacement binding attempt update failed"
            ) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()

    def bind(
        self,
        intent: ReplacementActionBinding,
        *,
        attestation: IdentityAttestationRecord,
        receipt_startup_action_id: str,
        receipt_role: str,
        receipt_assigned_name: str,
        receipt_locator: str,
        receipt_present: bool,
    ) -> ReplacementActionBinding:
        """Bind only an exact durable launch receipt to its exact v1 row generation."""
        if not receipt_present:
            raise ReplacementActionBindingError("startup launch receipt is absent")
        receipt = (
            _token(receipt_startup_action_id, "receipt_startup_action_id"),
            _token(receipt_role, "receipt_role"),
            _token(receipt_assigned_name, "receipt_assigned_name"),
            _token(receipt_locator, "receipt_locator"),
        )
        observed = (
            _token(attestation.assigned_name, "attestation.assigned_name"),
            _token(attestation.workspace_id, "attestation.workspace_id"),
            _token(attestation.role, "attestation.role"),
            _token(attestation.lane_id, "attestation.lane_id"),
            _token(attestation.locator, "attestation.locator"),
            _token(attestation.observed_at, "attestation.observed_at"),
        )
        expected = (
            intent.assigned_name,
            intent.workspace_id,
            intent.role,
            intent.lane_id,
            receipt[3],
            observed[5],
        )
        if observed != expected:
            raise ReplacementActionBindingError(
                "startup receipt, reserved identity, and v1 attestation generation do not match"
            )
        if (
            receipt[0] != intent.startup_action_id
            or receipt[1] != intent.role
            or receipt[2] != intent.assigned_name
            or attestation.replacement_action_id
        ):
            raise ReplacementActionBindingError(
                "startup receipt or main-store action field conflicts with the reservation"
            )
        conn = self._connect_existing(readonly=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, intent.action_id, intent.assigned_name)
            if row is None:
                raise ReplacementActionBindingError("binding reservation disappeared")
            current = _decode(row)
            if current != intent:
                if (
                    current.phase == BINDING_BOUND
                    and current.action_id == intent.action_id
                    and current.assigned_name == intent.assigned_name
                    and current.workspace_id == intent.workspace_id
                    and current.role == intent.role
                    and current.lane_id == intent.lane_id
                    and current.old_locator == intent.old_locator
                    and current.startup_nonce == intent.startup_nonce
                    and current.startup_action_id == intent.startup_action_id
                    and current.locator == receipt[3]
                    and current.attested_at == observed[5]
                ):
                    conn.commit()
                    return current
                raise ReplacementActionBindingError(
                    "binding reservation changed before the action could be bound"
                )
            now = _utc_now()
            conn.execute(
                f"UPDATE {_TABLE} SET phase=?, locator=?, attested_at=?, bound_at=? "
                "WHERE action_id=? AND assigned_name=? AND phase=?",
                (
                    BINDING_BOUND, receipt[3], observed[5], now,
                    intent.action_id, intent.assigned_name, BINDING_RESERVED,
                ),
            )
            if conn.total_changes != 1:
                raise ReplacementActionBindingError("binding compare-and-set was refused")
            conn.commit()
            return ReplacementActionBinding(
                action_id=intent.action_id,
                assigned_name=intent.assigned_name,
                workspace_id=intent.workspace_id,
                role=intent.role,
                lane_id=intent.lane_id,
                old_locator=intent.old_locator,
                startup_nonce=intent.startup_nonce,
                startup_action_id=intent.startup_action_id,
                phase=BINDING_BOUND,
                locator=receipt[3],
                attested_at=observed[5],
                created_at=intent.created_at,
                bound_at=now,
            )
        except ReplacementActionBindingError:
            _rollback_quietly(conn)
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            _rollback_quietly(conn)
            raise ReplacementActionBindingError(
                "replacement binding publication failed"
            ) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()


def replacement_action_is_bound(
    record: Optional[IdentityAttestationRecord],
    *,
    action_id: str,
    live_locator: str,
    expected_workspace_id: str,
    expected_role: str,
    expected_lane: str,
    expected_assigned_name: str = "",
    expected_old_locator: str = "",
    home: Path | None = None,
) -> bool:
    """Exact direct-v2 or side-bound-v1 action match; never fabricates a binding."""
    if record is None:
        return False
    action = (action_id or "").strip()
    if not action or action != action_id:
        return False
    if (
        record.workspace_id != expected_workspace_id
        or record.role != expected_role
        or record.lane_id != expected_lane
        or record.locator != live_locator
        or (expected_assigned_name and record.assigned_name != expected_assigned_name)
    ):
        return False
    direct = record.replacement_action_id
    if direct:
        return direct == action
    if not selected_attestation_store_is_v1(home):
        return False
    try:
        binding = HerdrIdentityReplacementBindingStore(home=home).read(
            action, record.assigned_name
        )
    except (ReplacementActionBindingError, sqlite3.DatabaseError, OSError):
        return False
    if binding is None or binding.phase != BINDING_BOUND:
        return False
    return (
        binding.action_id == action
        and binding.assigned_name == record.assigned_name
        and (not expected_assigned_name or binding.assigned_name == expected_assigned_name)
        and binding.workspace_id == record.workspace_id == expected_workspace_id
        and binding.role == record.role == expected_role
        and binding.lane_id == record.lane_id == expected_lane
        and (not expected_old_locator or binding.old_locator == expected_old_locator)
        and binding.locator == record.locator == live_locator
        and binding.attested_at == (record.observed_at or "")
    )


__all__ = (
    "BINDING_BOUND",
    "BINDING_RESERVED",
    "HERDR_IDENTITY_REPLACEMENT_BINDING_FILENAME",
    "HERDR_IDENTITY_REPLACEMENT_BINDING_SCHEMA_VERSION",
    "HerdrIdentityReplacementBindingStore",
    "ReplacementActionBinding",
    "ReplacementActionBindingError",
    "herdr_identity_replacement_binding_path",
    "replacement_action_is_bound",
    "selected_attestation_store_is_v1",
)
