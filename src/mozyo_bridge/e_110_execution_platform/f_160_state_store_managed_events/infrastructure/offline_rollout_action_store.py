"""Private, sealed action store for the external offline-rollout runner (#14838)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    OfflineRolloutActionError,
    canonical_bytes,
    canonical_digest,
    validate_action,
    validate_action_for_readback,
)


STORE_DIRECTORY = "offline-rollout-actions-v1"
_ACTION_ID = re.compile(r"offline_[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class OfflineRolloutActionStoreError(RuntimeError):
    """A private action record cannot be trusted or updated."""


def _require_private_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise OfflineRolloutActionStoreError("action_store_unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OfflineRolloutActionStoreError("action_store_not_private_directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise OfflineRolloutActionStoreError("action_store_permissions_unsafe")


class OfflineRolloutActionStore:
    """One sealed JSON record per action, outside the three migrated stores."""

    def __init__(self, home: Path):
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / STORE_DIRECTORY

    @staticmethod
    def _validate_id(action_id: object) -> str:
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            raise OfflineRolloutActionStoreError("action_id_invalid")
        return action_id

    def action_directory(self, action_id: str, *, create: bool = False) -> Path:
        token = self._validate_id(action_id)
        _require_private_directory(self.root, create=create)
        target = self.root / token
        _require_private_directory(target, create=create)
        return target

    @contextmanager
    def locked(self, action_id: str, *, create: bool = False) -> Iterator[Path]:
        directory = self.action_directory(action_id, create=create)
        lock_path = directory / "action.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise OfflineRolloutActionStoreError("action_lock_unavailable") from exc
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OfflineRolloutActionStoreError("action_busy") from exc
            yield directory
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _record_path(directory: Path) -> Path:
        return directory / "action.json"

    def create(self, action: Mapping[str, object]) -> None:
        try:
            validate_action(action)
        except OfflineRolloutActionError as exc:
            raise OfflineRolloutActionStoreError(str(exc)) from exc
        action_id = str(action["action_id"])
        with self.locked(action_id, create=True) as directory:
            path = self._record_path(directory)
            if path.exists():
                raise OfflineRolloutActionStoreError("action_already_exists")
            self._write_path(path, action)

    def load(self, action_id: str) -> dict:
        directory = self.action_directory(action_id, create=False)
        return self._read_path(self._record_path(directory), validate_action)

    def load_for_status(self, action_id: str) -> dict:
        """Read a sealed historical record without granting it execution authority."""

        directory = self.action_directory(action_id, create=False)
        return self._read_path(
            self._record_path(directory), validate_action_for_readback
        )

    def save_locked(self, directory: Path, action: Mapping[str, object]) -> None:
        """Save while the caller holds :meth:`locked` for this exact directory."""
        try:
            validate_action(action)
        except OfflineRolloutActionError as exc:
            raise OfflineRolloutActionStoreError(str(exc)) from exc
        if directory.name != action["action_id"] or directory.parent != self.root:
            raise OfflineRolloutActionStoreError("action_directory_mismatch")
        self._write_path(self._record_path(directory), action)

    def load_locked(self, directory: Path) -> dict:
        return self._read_path(self._record_path(directory), validate_action)

    @staticmethod
    def _write_path(path: Path, action: Mapping[str, object]) -> None:
        payload = json.loads(canonical_bytes(action).decode("ascii"))
        envelope = {"payload": payload, "payload_sha256": canonical_digest(payload)}
        raw = canonical_bytes(envelope) + b"\n"
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(temp, flags, 0o600)
            try:
                os.write(fd, raw)
                os.fsync(fd)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise OfflineRolloutActionStoreError("action_write_failed") from exc

    @staticmethod
    def _read_sealed_payload(path: Path):
        """Apply identical filesystem/JSON/seal checks to every decode policy."""

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                    raise OfflineRolloutActionStoreError("action_record_permissions_unsafe")
                raw = b""
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > 16 * 1024 * 1024:
                        raise OfflineRolloutActionStoreError("action_record_too_large")
            finally:
                os.close(fd)
        except OSError as exc:
            raise OfflineRolloutActionStoreError("action_record_unavailable") from exc
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OfflineRolloutActionStoreError("action_record_unreadable") from exc
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "payload",
            "payload_sha256",
        }:
            raise OfflineRolloutActionStoreError("action_record_invalid")
        if raw != canonical_bytes(envelope) + b"\n":
            raise OfflineRolloutActionStoreError("action_record_noncanonical")
        payload = envelope.get("payload")
        digest = envelope.get("payload_sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise OfflineRolloutActionStoreError("action_record_seal_invalid")
        if canonical_digest(payload) != digest:
            raise OfflineRolloutActionStoreError("action_record_seal_mismatch")
        return payload

    @classmethod
    def _read_path(cls, path: Path, validator) -> dict:
        payload = cls._read_sealed_payload(path)
        try:
            validator(payload)
        except OfflineRolloutActionError as exc:
            raise OfflineRolloutActionStoreError(str(exc)) from exc
        return json.loads(canonical_bytes(payload).decode("ascii"))


__all__ = (
    "OfflineRolloutActionStore",
    "OfflineRolloutActionStoreError",
    "STORE_DIRECTORY",
)
