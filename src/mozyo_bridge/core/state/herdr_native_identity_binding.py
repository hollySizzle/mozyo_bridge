"""Bind mozyo logical agent identities to Herdr 0.8 native names.

Herdr 0.8 restricts an assigned agent name to 32 lowercase CLI-safe characters.
The existing ``mzb1_...`` identity is intentionally lossless and can therefore be
longer.  It remains the product/routing authority; this store is only the adapter
boundary that gives Herdr a short deterministic name and restores the logical name
on inventory reads.

The digest is not trusted as collision-free by assumption.  Both directions are
unique in SQLite and :meth:`HerdrNativeIdentityBindingStore.bind_many` checks the
whole launch set inside one ``BEGIN IMMEDIATE`` transaction.  A collision or
malformed store is a typed refusal before any pane is created.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.shared.paths import mozyo_bridge_home


NATIVE_SCHEME_PREFIX = "mza1"
NATIVE_NAME_MAX_LENGTH = 32
_DIGEST_LENGTH = NATIVE_NAME_MAX_LENGTH - len(NATIVE_SCHEME_PREFIX) - 1
_FILENAME = "herdr-native-identity.sqlite3"
_TABLE = "herdr_native_identity_bindings"
_NATIVE_NAME = re.compile(r"^mza1_[a-z2-7]{27}$")


class HerdrNativeIdentityBindingError(RuntimeError):
    """The native-name binding authority is unavailable or contradictory."""


@dataclass(frozen=True)
class HerdrNativeIdentityBinding:
    logical_name: str
    native_name: str


def herdr_native_identity_binding_path(home: Optional[Path] = None) -> Path:
    return (Path(home) if home is not None else mozyo_bridge_home()) / _FILENAME


def native_name_for(logical_name: str) -> str:
    """Return the deterministic 32-character Herdr name for ``logical_name``.

    Lowercase base32 stays inside Herdr's ``[a-z0-9_-]`` grammar.  Truncation is
    safe only in combination with the collision-checked binding store below; callers
    must never use this pure value for a launch without first calling ``bind``.
    """
    if not isinstance(logical_name, str) or not logical_name or logical_name.strip() != logical_name:
        raise HerdrNativeIdentityBindingError(
            "logical agent identity must be a non-empty, already-trimmed string"
        )
    digest = base64.b32encode(hashlib.sha256(logical_name.encode("utf-8")).digest())
    body = digest.decode("ascii").lower().rstrip("=")[:_DIGEST_LENGTH]
    native = f"{NATIVE_SCHEME_PREFIX}_{body}"
    if len(native) != NATIVE_NAME_MAX_LENGTH:
        raise HerdrNativeIdentityBindingError(
            "internal native-name derivation did not produce the fixed 32-character form"
        )
    return native


def is_native_name(value: object) -> bool:
    return isinstance(value, str) and bool(_NATIVE_NAME.fullmatch(value))


class HerdrNativeIdentityBindingStore:
    """Small home-scoped SQLite authority for logical/native name bindings."""

    def __init__(self, *, home: Optional[Path] = None, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else herdr_native_identity_binding_path(home)

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                logical_name TEXT PRIMARY KEY NOT NULL,
                native_name TEXT NOT NULL UNIQUE,
                CHECK (length(logical_name) > 0),
                CHECK (length(native_name) = {NATIVE_NAME_MAX_LENGTH})
            )
            """
        )

    def _open_write(self) -> sqlite3.Connection:
        try:
            parent_existed = self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.parent.is_dir() or self.path.parent.is_symlink():
                raise OSError("binding store parent is not a plain directory")
            if not parent_existed:
                os.chmod(self.path.parent, 0o700)
            if self.path.is_symlink():
                raise OSError("binding store path is a symbolic link")
            conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            self._schema(conn)
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                conn.close()
                raise HerdrNativeIdentityBindingError(
                    "native identity binding store permissions could not be restricted"
                ) from exc
            return conn
        except HerdrNativeIdentityBindingError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise HerdrNativeIdentityBindingError(
                "native identity binding store could not be opened for an atomic bind"
            ) from exc

    def bind(self, logical_name: str) -> HerdrNativeIdentityBinding:
        """Atomically bind one logical identity."""
        return self.bind_many((logical_name,))[0]

    def bind_many(
        self, logical_names: Sequence[str]
    ) -> tuple[HerdrNativeIdentityBinding, ...]:
        """Atomically validate and bind a complete managed-launch set.

        Derivation and duplicate checks happen before the store is opened.  The
        complete set is then checked and inserted under one write transaction, so
        a second-slot collision cannot be discovered after the first slot is live.
        """
        requested = tuple(logical_names)
        if not requested:
            return ()
        if len(set(requested)) != len(requested):
            raise HerdrNativeIdentityBindingError(
                "managed launch contains a duplicate logical agent identity"
            )
        bindings = tuple(
            HerdrNativeIdentityBinding(logical_name, native_name_for(logical_name))
            for logical_name in requested
        )
        if len({binding.native_name for binding in bindings}) != len(bindings):
            raise HerdrNativeIdentityBindingError(
                "managed launch derives a duplicate Herdr native name"
            )
        conn = self._open_write()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for binding in bindings:
                logical_row = conn.execute(
                    f"SELECT native_name FROM {_TABLE} WHERE logical_name = ?",
                    (binding.logical_name,),
                ).fetchone()
                native_row = conn.execute(
                    f"SELECT logical_name FROM {_TABLE} WHERE native_name = ?",
                    (binding.native_name,),
                ).fetchone()
                if logical_row is not None and logical_row != (binding.native_name,):
                    raise HerdrNativeIdentityBindingError(
                        "logical agent identity is already bound to a different native name"
                    )
                if native_row is not None and native_row != (binding.logical_name,):
                    raise HerdrNativeIdentityBindingError(
                        "derived Herdr native name collides with another logical identity"
                    )
            for binding in bindings:
                conn.execute(
                    f"INSERT OR IGNORE INTO {_TABLE}(logical_name, native_name) VALUES (?, ?)",
                    (binding.logical_name, binding.native_name),
                )
            conn.execute("COMMIT")
            return bindings
        except HerdrNativeIdentityBindingError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise HerdrNativeIdentityBindingError(
                "native identity binding could not be recorded atomically"
            ) from exc
        finally:
            conn.close()

    def resolve_native(self, native_name: str) -> Optional[str]:
        """Return the logical identity, ``None`` when absent; never creates a store."""
        if not is_native_name(native_name):
            raise HerdrNativeIdentityBindingError(
                "native identity does not use the canonical mza1 32-character form"
            )
        if self.path.is_symlink():
            raise HerdrNativeIdentityBindingError(
                "native identity binding store path is a symbolic link"
            )
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    f"SELECT logical_name FROM {_TABLE} WHERE native_name = ?",
                    (native_name,),
                ).fetchone()
            finally:
                conn.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise HerdrNativeIdentityBindingError(
                "native identity binding store could not be read"
            ) from exc
        if row is None:
            return None
        if (
            not isinstance(row, tuple)
            or len(row) != 1
            or not isinstance(row[0], str)
            or not row[0]
            or row[0].strip() != row[0]
            or native_name_for(row[0]) != native_name
        ):
            raise HerdrNativeIdentityBindingError(
                "native identity binding row is malformed or contradicts derivation"
            )
        return row[0]


def logicalize_agent_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    store: Optional[HerdrNativeIdentityBindingStore] = None,
) -> tuple[Mapping[str, object], ...]:
    """Restore logical names at the Herdr inventory boundary.

    Legacy ``mzb1`` rows pass through byte-for-byte.  An ``mza1`` row without a
    readable binding is not an unmanaged agent: it is a managed identity whose
    authority is missing, so the whole snapshot is refused.
    """
    authority = store or HerdrNativeIdentityBindingStore()
    restored: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            restored.append(row)
            continue
        name = row.get("name")
        if (
            isinstance(name, str)
            and name.startswith(f"{NATIVE_SCHEME_PREFIX}_")
            and not is_native_name(name)
        ):
            raise HerdrNativeIdentityBindingError(
                "Herdr inventory contains a malformed managed native identity"
            )
        if not is_native_name(name):
            restored.append(row)
            continue
        logical_name = authority.resolve_native(name)
        if logical_name is None:
            raise HerdrNativeIdentityBindingError(
                "Herdr inventory contains an mza1 name with no durable logical binding"
            )
        logical_row = dict(row)
        logical_row["native_name"] = name
        logical_row["name"] = logical_name
        restored.append(logical_row)
    return tuple(restored)


__all__ = (
    "HerdrNativeIdentityBinding",
    "HerdrNativeIdentityBindingError",
    "HerdrNativeIdentityBindingStore",
    "NATIVE_NAME_MAX_LENGTH",
    "NATIVE_SCHEME_PREFIX",
    "herdr_native_identity_binding_path",
    "is_native_name",
    "logicalize_agent_rows",
    "native_name_for",
)
