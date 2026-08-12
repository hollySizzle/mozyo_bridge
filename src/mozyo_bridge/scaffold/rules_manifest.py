"""Repo-local rules installation and scaffold-manifest synchronization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from mozyo_bridge.shared.errors import die


class RulesStoreLike(Protocol):
    """Structural subset needed by the installer."""

    is_repo_local: bool
    repo: Path | None


def _manifest_update(
    store: RulesStoreLike,
    *,
    manifest_relative_path: Path,
    central_mode: str,
    repo_local_mode: str,
    package_text: Callable[[str, str], str],
) -> tuple[Path, str] | None:
    """Plan the manifest identity update before writing any preset files."""
    if not store.is_repo_local or store.repo is None:
        return None
    path = store.repo / manifest_relative_path
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot update repo-local rules manifest {path}: {exc}")
    if not isinstance(payload, dict):
        die(f"invalid repo-local rules manifest {path}: expected object")
    manifest_mode = payload.get("mode")
    if manifest_mode == central_mode:
        # This is the documented first step of a central -> repo-local switch:
        # install the destination store without rewriting the still-central
        # manifest.  The following `scaffold apply --repo-local` owns the mode,
        # rule_path, router hashes, and preservation-aware router replacement.
        return None
    if manifest_mode != repo_local_mode:
        die(
            f"invalid repo-local rules manifest {path}: "
            f"mode must be {central_mode!r} or {repo_local_mode!r}"
        )
    preset = payload.get("preset")
    if not isinstance(preset, str) or not preset:
        die(f"invalid repo-local rules manifest {path}: preset is missing")
    workflow = package_text(preset, "agent-workflow.md")
    updated = dict(payload)
    updated["preset_version"] = package_text(preset, "VERSION").strip()
    updated["preset_hash"] = hashlib.sha256(workflow.encode("utf-8")).hexdigest()
    content = json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return None if path.read_text(encoding="utf-8") == content else (path, content)


def install_rule_store(
    store: RulesStoreLike,
    *,
    presets: Iterable[str],
    preset_dir: Callable[[str], Path],
    package_text: Callable[[str, str], str],
    manifest_relative_path: Path,
    central_mode: str,
    repo_local_mode: str,
) -> list[Path]:
    """Install presets and couple their repo-local manifest identity."""
    manifest_update = _manifest_update(
        store,
        manifest_relative_path=manifest_relative_path,
        central_mode=central_mode,
        repo_local_mode=repo_local_mode,
        package_text=package_text,
    )
    written: list[Path] = []
    for preset in presets:
        target_dir = preset_dir(preset)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("VERSION", "agent-workflow.md"):
            content = package_text(preset, filename)
            target = target_dir / filename
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8")
                written.append(target)
    if manifest_update is not None:
        path, content = manifest_update
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
