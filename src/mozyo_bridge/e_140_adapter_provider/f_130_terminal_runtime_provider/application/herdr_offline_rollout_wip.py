"""Private, content-bound WIP snapshots for the global offline rollout (#14838)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    read_live_worktree_fingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    canonical_bytes,
)


def _ok(**receipt) -> PhaseExecutionResult:
    return PhaseExecutionResult(True, receipt=receipt)


def _fail(reason: str, detail: str = "") -> PhaseExecutionResult:
    return PhaseExecutionResult(False, reason=reason, detail=detail[:1000])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_wip_snapshots(
    *, records, paths: Mapping[str, str], action_directory: Path
) -> PhaseExecutionResult:
    """Verify every fingerprint and privately preserve dirty tracked/index/untracked bytes."""
    root = action_directory / "wip"
    root.mkdir(mode=0o700, exist_ok=True)
    for row in records:
        snapshot_id = row["snapshot_id"]
        target = root / hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()
        manifest_path = target / "manifest.json"
        repo = Path(paths[snapshot_id])
        current = read_live_worktree_fingerprint(repo, 30.0)
        expected = row["wip"]
        if not current.readable or current.digest != expected["digest"]:
            return _fail("wip_drift", snapshot_id)
        if not expected["dirty"] and not expected["untracked"]:
            continue
        if manifest_path.is_file():
            recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = recorded.get("files")
            expected_files = {
                "worktree.patch",
                "index.patch",
                "untracked.list",
                "git-index",
                "untracked.tar",
            }
            if (
                recorded.get("snapshot_id") != snapshot_id
                or recorded.get("wip_digest") != current.digest
                or not isinstance(files, Mapping)
                or set(files) != expected_files
                or any(
                    not (target / name).is_file()
                    or _sha256(target / name) != digest
                    for name, digest in files.items()
                )
            ):
                return _fail("wip_snapshot_readback_failed", snapshot_id)
            continue
        target.mkdir(mode=0o700, exist_ok=False)
        commands = {
            "worktree.patch": ["git", "diff", "HEAD", "--binary", "--no-ext-diff"],
            "index.patch": ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
            "untracked.list": ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        }
        outputs = {}
        for filename, argv in commands.items():
            result = subprocess.run(
                argv, cwd=repo, capture_output=True, check=False, timeout=60.0
            )
            if result.returncode != 0:
                return _fail("wip_snapshot_failed", snapshot_id)
            path = target / filename
            path.write_bytes(result.stdout)
            path.chmod(0o600)
            outputs[filename] = _sha256(path)
        index_result = subprocess.run(
            ["git", "rev-parse", "--git-path", "index"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
            timeout=30.0,
        )
        if index_result.returncode != 0:
            return _fail("wip_index_unreadable", snapshot_id)
        index_path = Path(index_result.stdout.strip())
        if not index_path.is_absolute():
            index_path = repo / index_path
        index_copy = target / "git-index"
        index_copy.write_bytes(index_path.read_bytes())
        index_copy.chmod(0o600)
        outputs["git-index"] = _sha256(index_copy)
        untracked = (target / "untracked.list").read_bytes().split(b"\0")
        archive = target / "untracked.tar"
        with tarfile.open(archive, "w", dereference=False) as tar:
            for raw in untracked:
                if raw:
                    relative = os.fsdecode(raw)
                    tar.add(repo / relative, arcname=relative, recursive=False)
        archive.chmod(0o600)
        outputs["untracked.tar"] = _sha256(archive)
        manifest = {
            "snapshot_id": snapshot_id,
            "wip_digest": current.digest,
            "files": outputs,
        }
        manifest_path.write_bytes(canonical_bytes(manifest) + b"\n")
        manifest_path.chmod(0o600)
    return _ok(wip_snapshots_verified=True)


__all__ = ("ensure_wip_snapshots",)
