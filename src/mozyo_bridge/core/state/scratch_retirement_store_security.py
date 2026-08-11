"""Closed artifact census and file-security checks for scratch retirement."""

from __future__ import annotations

import os
import json
import sqlite3
import stat
from pathlib import Path


class ScratchRetirementStoreSecurityError(RuntimeError):
    pass


def primary_artifact_paths(
    path: Path, seal_path: Path, temp_path: Path
) -> tuple[tuple[str, Path], ...]:
    return (
        ("db", path),
        ("wal", path.with_name(path.name + "-wal")),
        ("shm", path.with_name(path.name + "-shm")),
        ("journal", path.with_name(path.name + "-journal")),
        ("seal", seal_path),
        ("temp", temp_path),
    )


def _migration_artifacts(path: Path) -> tuple[tuple[str, Path], ...]:
    backup = path.with_name(path.name + ".v1.backup")
    fixed = (
        ("v1_backup", backup),
        ("v1_backup_seal", backup.with_name(backup.name + ".seal")),
        ("v1_migration", backup.with_name(backup.name + ".migration")),
        ("v1_flat_staging", backup.with_name(backup.name + ".staging")),
        ("v1_flat_seal_staging", backup.with_name(backup.name + ".seal.staging")),
        *((f"v1_backup{suffix}", backup.with_name(backup.name + suffix))
          for suffix in ("-wal", "-shm", "-journal")),
    )
    prefix = f".{backup.name}.staging-"
    try:
        dynamic = tuple(
            ("v1_private_staging", path.parent / entry.name)
            for entry in os.scandir(path.parent)
            if entry.name.startswith(prefix)
        )
    except OSError:
        dynamic = (
            (("v1_migration_inventory_unreadable", path.parent),)
            if path.parent.exists()
            else ()
        )
    return fixed + dynamic


def classify_artifacts(
    path: Path, seal_path: Path, temp_path: Path
) -> tuple[str, tuple[str, ...], str]:
    paths = primary_artifact_paths(path, seal_path, temp_path)
    present = tuple(name for name, item in paths if os.path.lexists(item))
    migration = tuple(
        name for name, item in _migration_artifacts(path) if os.path.lexists(item)
    )
    if not present:
        if migration:
            return (
                "damaged", migration,
                "the primary retirement authority is absent while backup or migration "
                "evidence remains; explicit recovery is required",
            )
        return ("absent", (), "")
    allowed = sorted(migration) == ["v1_backup", "v1_backup_seal"] and (
        _verified_backup_pair(path)
    )
    retained = _finalized_migration_provenance(path, migration)
    if migration and not allowed and not retained:
        return (
            "damaged", present + migration,
            "scratch retirement migration is incomplete; normal runtime access is "
            "disabled until explicit recovery completes",
        )
    if "temp" in present:
        return (
            "damaged", present,
            "a bootstrap staging artifact is present beside the authority; a healthy "
            "store never carries one",
        )
    if "db" in present and "seal" in present:
        return ("present", present, "")
    row_bearing = {"db", "wal", "shm", "journal"} & set(present)
    suffix = (
        "; row-bearing data exists without its identity seal"
        if row_bearing and "seal" not in present
        else "; the identity seal exists without its database"
        if "seal" in present and "db" not in present
        else ""
    )
    return (
        "damaged", present,
        f"the retirement authority's artifacts are incomplete (present: "
        f"{', '.join(present)}{suffix})",
    )


def _finalized_migration_provenance(path: Path, names: tuple[str, ...]) -> bool:
    if sorted(names) != sorted((
        "v1_backup", "v1_backup_seal", "v1_migration", "v1_private_staging"
    )):
        return False
    backup = path.with_name(path.name + ".v1.backup")
    backup_seal = backup.with_name(backup.name + ".seal")
    control = backup.with_name(backup.name + ".migration")
    try:
        info = control.lstat()
        control_bytes = control.read_bytes()
        payload = json.loads(control_bytes.decode("utf-8"))
        root = control.parent / payload["staging_name"]
        root_info = root.lstat()
        marker = root / "authority.json"
        marker_info = marker.lstat()
        artifact_pins = payload["artifact_pins"]
        staging_db_info = (root / "store.sqlite3").lstat()
        staging_seal_info = (root / "store.seal").lstat()
        backup_info = backup.lstat()
        backup_seal_info = backup_seal.lstat()
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and stat.S_ISDIR(root_info.st_mode)
            and root_info.st_uid == os.geteuid()
            and stat.S_IMODE(root_info.st_mode) == 0o700
            and stat.S_ISREG(marker_info.st_mode)
            and marker_info.st_uid == os.geteuid()
            and stat.S_IMODE(marker_info.st_mode) == 0o600
            and marker_info.st_nlink == 2
            and (info.st_dev, info.st_ino) == (marker_info.st_dev, marker_info.st_ino)
            and marker.read_bytes() == control_bytes
            and set(payload) == {
                "v", "staging_name", "source_digest", "seal_nonce", "artifacts",
                "artifact_pins",
            }
            and type(payload["v"]) is int and payload["v"] == 1
            and type(payload["staging_name"]) is str
            and root.name == payload["staging_name"]
            and root.name.startswith(f".{backup.name}.staging-")
            and type(payload["source_digest"]) is str
            and len(payload["source_digest"]) == 64
            and all(char in "0123456789abcdef"
                    for char in payload["source_digest"])
            and type(payload["seal_nonce"]) is str
            and payload["seal_nonce"]
            and payload["seal_nonce"].strip() == payload["seal_nonce"]
            and payload["artifacts"] == ["authority.json", "store.sqlite3", "store.seal"]
            and type(artifact_pins) is dict
            and set(artifact_pins) == {"store.sqlite3", "store.seal"}
            and all(
                type(parts) is list and len(parts) == 2
                and all(type(part) is int and part >= 0 for part in parts)
                for parts in artifact_pins.values()
            )
            and {child.name for child in root.iterdir()}
            == {"authority.json", "store.sqlite3", "store.seal"}
            and _retained_link_matches(staging_db_info, backup_info,
                                       artifact_pins["store.sqlite3"])
            and _retained_link_matches(staging_seal_info, backup_seal_info,
                                       artifact_pins["store.seal"])
            and _verified_backup_pair(
                path,
                expected_digest=payload["source_digest"],
                expected_nonce=payload["seal_nonce"],
            )
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False


def _verified_backup_pair(
    path: Path, *, expected_digest: str = "", expected_nonce: str = ""
) -> bool:
    backup = path.with_name(path.name + ".v1.backup")
    seal = backup.with_name(backup.name + ".seal")
    try:
        from .scratch_retirement_migration import (
            _canonical_text,
            _logical_digest,
            _store_nonce,
        )

        for artifact in (backup, seal):
            info = artifact.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                return False
        digest = _logical_digest(backup)
        nonce = _canonical_text(seal)
        return (
            _store_nonce(backup) == nonce
            and (not expected_digest or digest == expected_digest)
            and (not expected_nonce or nonce == expected_nonce)
        )
    except (OSError, UnicodeError, ValueError, RuntimeError, sqlite3.DatabaseError):
        return False


def _retained_link_matches(
    staging: os.stat_result, final: os.stat_result, pin: list[object]
) -> bool:
    return (
        stat.S_ISREG(staging.st_mode)
        and stat.S_ISREG(final.st_mode)
        and staging.st_uid == os.geteuid()
        and final.st_uid == os.geteuid()
        and stat.S_IMODE(staging.st_mode) == 0o600
        and stat.S_IMODE(final.st_mode) == 0o600
        and staging.st_nlink == 2
        and final.st_nlink == 2
        and (staging.st_dev, staging.st_ino) == (final.st_dev, final.st_ino)
        and [staging.st_dev, staging.st_ino] == pin
    )


def primary_security_snapshot(
    path: Path, seal_path: Path, temp_path: Path
) -> dict[str, tuple[int, int, int]]:
    state, present, detail = classify_artifacts(path, seal_path, temp_path)
    if state != "present":
        raise ScratchRetirementStoreSecurityError(
            detail or "the retirement authority is not a complete primary store"
        )
    paths = dict(primary_artifact_paths(path, seal_path, temp_path))
    result = {}
    for name in present:
        item = paths.get(name)
        if item is None:
            continue
        try:
            info = item.lstat()
        except OSError as exc:
            raise ScratchRetirementStoreSecurityError(
                "the retirement authority artifact set drifted during verification"
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ScratchRetirementStoreSecurityError(
                "the retirement authority has an unsafe owner, mode, or file type"
            )
        result[name] = (info.st_dev, info.st_ino, info.st_ctime_ns)
    return result


__all__ = (
    "ScratchRetirementStoreSecurityError",
    "classify_artifacts",
    "primary_artifact_paths",
    "primary_security_snapshot",
)
