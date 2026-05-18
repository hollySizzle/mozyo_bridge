from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template
from time import strftime

import yaml

from mozyo_bridge import __version__
from mozyo_bridge.shared.errors import die

ROUTER_TEMPLATE_PRESET = "_router"
RULE_RELATIVE_PATH = Path("rules") / "presets"
MANIFEST_RELATIVE_PATH = Path(".mozyo-bridge") / "scaffold.json"
PRESET_REGISTRY_FILENAME = "presets.yaml"

# Marker pair used to delimit a project-local additions block inside scaffold-
# generated routers (AGENTS.md / CLAUDE.md). Content between these markers is
# preserved by `scaffold apply` / `scaffold diff` when the on-disk router
# carries the pair, so re-syncing the scaffold base does not erase project-
# local additions the operator put inside the block. The markers are HTML
# comments so they render invisible in Markdown. Eligibility is per-file:
# only AGENTS.md and CLAUDE.md preserve; the manifest does not. Preservation
# requires BOTH the rendered template and the on-disk file to carry the pair;
# legacy on-disk files without markers fall through to the existing overwrite
# / backup / force behavior unchanged.
PROJECT_LOCAL_BEGIN_MARKER = "<!-- mozyo-bridge:project-local-additions:begin -->"
PROJECT_LOCAL_END_MARKER = "<!-- mozyo-bridge:project-local-additions:end -->"
PROJECT_LOCAL_PRESERVED_FILENAMES = frozenset({"AGENTS.md", "CLAUDE.md"})


@dataclass(frozen=True)
class RenderedFile:
    path: Path
    content: str


@dataclass(frozen=True)
class PresetDefinition:
    name: str
    workflow: str
    ticket_anchor_label: str
    extends: str | None = None


def _registry_text() -> str:
    return (
        resources.files("mozyo_bridge.scaffold.presets")
        .joinpath(PRESET_REGISTRY_FILENAME)
        .read_text(encoding="utf-8")
    )


def _load_preset_registry() -> dict[str, PresetDefinition]:
    raw = yaml.safe_load(_registry_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("presets"), dict):
        die(f"{PRESET_REGISTRY_FILENAME} must contain a mapping named `presets`")
    definitions: dict[str, PresetDefinition] = {}
    for name, value in raw["presets"].items():
        if not isinstance(name, str) or not name:
            die(f"{PRESET_REGISTRY_FILENAME} contains an invalid preset name: {name!r}")
        if not isinstance(value, dict):
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} must be a mapping")
        workflow = value.get("workflow")
        ticket_anchor_label = value.get("ticket_anchor_label")
        extends = value.get("extends")
        if not isinstance(workflow, str) or not workflow.endswith("/agent-workflow.md"):
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} has invalid workflow")
        if not isinstance(ticket_anchor_label, str) or not ticket_anchor_label:
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} has invalid ticket_anchor_label")
        if extends is not None and not isinstance(extends, str):
            die(f"{PRESET_REGISTRY_FILENAME} preset {name!r} has invalid extends")
        workflow_preset = workflow.split("/", 1)[0]
        if workflow_preset != name:
            die(
                f"{PRESET_REGISTRY_FILENAME} preset {name!r} workflow must live under "
                f"{name!r}, got {workflow!r}"
            )
        definitions[name] = PresetDefinition(
            name=name,
            workflow=workflow,
            ticket_anchor_label=ticket_anchor_label,
            extends=extends,
        )
    for definition in definitions.values():
        if definition.extends is not None and definition.extends not in definitions:
            die(
                f"{PRESET_REGISTRY_FILENAME} preset {definition.name!r} extends "
                f"unknown preset {definition.extends!r}"
            )
    return definitions


PRESET_DEFINITIONS = _load_preset_registry()
PRESETS = tuple(PRESET_DEFINITIONS)


def preset_definition(preset: str) -> PresetDefinition:
    try:
        return PRESET_DEFINITIONS[preset]
    except KeyError:
        die(f"unsupported rules preset: {preset}")


def mozyo_bridge_home() -> Path:
    return Path(os.environ.get("MOZYO_BRIDGE_HOME", "~/.mozyo_bridge")).expanduser().resolve()


def package_preset_root(preset: str):
    preset_definition(preset)
    return resources.files("mozyo_bridge.scaffold.presets").joinpath(preset)


def router_template_text(filename: str) -> str:
    return (
        resources.files("mozyo_bridge.scaffold.presets")
        .joinpath(ROUTER_TEMPLATE_PRESET)
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def installed_preset_dir(preset: str, home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / RULE_RELATIVE_PATH / preset


def installed_agent_workflow(preset: str, home: Path | None = None) -> Path:
    return installed_preset_dir(preset, home) / "agent-workflow.md"


def preset_install_requirements(preset: str) -> tuple[str, ...]:
    seen: list[str] = []
    current: str | None = preset
    while current is not None:
        if current in seen:
            die(f"preset inheritance cycle detected: {' -> '.join(seen + [current])}")
        definition = preset_definition(current)
        seen.append(current)
        current = definition.extends
    return tuple(seen)


def portable_rule_path(preset: str) -> str:
    # Symbolic path embedded into generated routers and the scaffold manifest so
    # they stay portable across hosts (no user-specific home leakage) while still
    # honoring MOZYO_BRIDGE_HOME at consumption time. The consuming agent expands
    # ${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge} when it reads the router.
    preset_definition(preset)
    return f"${{MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}}/rules/presets/{preset}/agent-workflow.md"


def package_text(preset: str, filename: str) -> str:
    return package_preset_root(preset).joinpath(filename).read_text(encoding="utf-8")


def package_version(preset: str) -> str:
    return package_text(preset, "VERSION").strip()


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_rules(home: Path | None = None) -> list[Path]:
    root = home or mozyo_bridge_home()
    written: list[Path] = []
    for preset in PRESETS:
        target_dir = installed_preset_dir(preset, root)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("VERSION", "agent-workflow.md"):
            content = package_text(preset, filename)
            target = target_dir / filename
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8")
                written.append(target)
    return written


def rules_status(home: Path | None = None) -> list[dict[str, str]]:
    root = home or mozyo_bridge_home()
    rows: list[dict[str, str]] = []
    for preset in PRESETS:
        expected_version = package_version(preset)
        preset_dir = installed_preset_dir(preset, root)
        version_path = preset_dir / "VERSION"
        workflow_path = preset_dir / "agent-workflow.md"
        if not version_path.exists() or not workflow_path.exists():
            status = "missing"
            installed_version = "-"
        else:
            installed_version = version_path.read_text(encoding="utf-8").strip()
            status = "ok" if installed_version == expected_version else "outdated"
        rows.append(
            {
                "preset": preset,
                "status": status,
                "installed": installed_version,
                "packaged": expected_version,
                "path": str(workflow_path),
            }
        )
    return rows


def require_installed_preset(preset: str, home: Path | None = None) -> Path:
    missing: list[str] = []
    for required_preset in preset_install_requirements(preset):
        workflow = installed_agent_workflow(required_preset, home)
        version = installed_preset_dir(required_preset, home) / "VERSION"
        if not workflow.exists() or not version.exists():
            missing.append(required_preset)
    if missing:
        die(
            "rules preset is not installed: "
            + ", ".join(missing)
            + ". Run `mozyo-bridge rules install` first."
        )
    return installed_agent_workflow(preset, home)


def router_context(preset: str, target: Path, workflow_path: Path) -> dict[str, str]:
    definition = preset_definition(preset)
    return {
        "preset": preset,
        "preset_version": package_version(preset),
        "project_root": str(target),
        "rule_path": portable_rule_path(preset),
        "mozyo_bridge_version": __version__,
        "ticket_anchor_label": definition.ticket_anchor_label,
    }


def render_router_pair(preset: str, target: Path, workflow_path: Path) -> list[RenderedFile]:
    context = router_context(preset, target, workflow_path)
    files = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        template = Template(router_template_text(filename))
        files.append(RenderedFile(Path(filename), template.safe_substitute(context)))
    return files


def installed_preset_hash(preset: str, home: Path | None = None) -> str | None:
    workflow = installed_agent_workflow(preset, home)
    if not workflow.exists():
        return None
    return sha256_file(workflow)


def installed_preset_version(preset: str, home: Path | None = None) -> str | None:
    version_path = installed_preset_dir(preset, home) / "VERSION"
    if not version_path.exists():
        return None
    return version_path.read_text(encoding="utf-8").strip()


def manifest_content(preset: str, workflow_path: Path, rendered: list[RenderedFile]) -> str:
    # `rule_path` is stored in symbolic form so the manifest stays safe to commit
    # in target repositories. Drift detection uses `preset_hash` and per-file
    # sha256 entries, not this field, so sanitizing it does not weaken status.
    payload = {
        "schema_version": 2,
        "mode": "central",
        "preset": preset,
        "preset_version": package_version(preset),
        "preset_hash": sha256_file(workflow_path),
        "generated_by": f"mozyo-bridge {__version__}",
        "rule_path": portable_rule_path(preset),
        "files": {
            str(item.path): {
                "sha256": sha256_text(item.content),
            }
            for item in rendered
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak.{strftime('%Y%m%d%H%M%S')}")


def extract_project_local_block(text: str) -> str | None:
    """Return the literal content between project-local markers, or None.

    Returns None when either marker is absent so callers can fall through to
    the existing overwrite-or-protect behavior unchanged. The returned string
    is the raw inner content including leading/trailing whitespace so
    substitution is byte-for-byte stable.
    """
    begin = text.find(PROJECT_LOCAL_BEGIN_MARKER)
    if begin < 0:
        return None
    block_start = begin + len(PROJECT_LOCAL_BEGIN_MARKER)
    end = text.find(PROJECT_LOCAL_END_MARKER, block_start)
    if end < 0:
        return None
    return text[block_start:end]


def substitute_project_local_block(rendered: str, replacement: str) -> str:
    """Replace the project-local block in `rendered` with `replacement`.

    Returns `rendered` unchanged if either marker is missing from the
    rendered template (defensive fallback — preservation requires both sides
    to carry the marker pair).
    """
    begin = rendered.find(PROJECT_LOCAL_BEGIN_MARKER)
    if begin < 0:
        return rendered
    block_start = begin + len(PROJECT_LOCAL_BEGIN_MARKER)
    end = rendered.find(PROJECT_LOCAL_END_MARKER, block_start)
    if end < 0:
        return rendered
    return rendered[:block_start] + replacement + rendered[end:]


def apply_project_local_preservation(
    rendered_items: list[RenderedFile],
    target: Path,
) -> list[RenderedFile]:
    """Substitute on-disk project-local blocks into the rendered routers.

    For each rendered file whose basename is in PROJECT_LOCAL_PRESERVED_FILENAMES,
    if the on-disk file exists and contains the marker pair, the content between
    the markers in the on-disk file is substituted into the rendered template
    (which also carries the marker pair). The manifest must be built AFTER this
    step so its per-file sha256 entries reflect the post-substitution content
    that will land on disk.

    Files not eligible for preservation, files missing on disk, and on-disk
    files without the marker pair all pass through unchanged.
    """
    preserved: list[RenderedFile] = []
    for item in rendered_items:
        if item.path.name not in PROJECT_LOCAL_PRESERVED_FILENAMES:
            preserved.append(item)
            continue
        on_disk_path = target / item.path
        if not on_disk_path.exists():
            preserved.append(item)
            continue
        try:
            on_disk_text = on_disk_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            preserved.append(item)
            continue
        block = extract_project_local_block(on_disk_text)
        if block is None:
            preserved.append(item)
            continue
        preserved.append(RenderedFile(item.path, substitute_project_local_block(item.content, block)))
    return preserved


def render_scaffold_files(
    preset: str,
    target: Path,
    home: Path | None = None,
) -> list[RenderedFile]:
    target = target.expanduser().resolve()
    workflow_path = require_installed_preset(preset, home)
    rendered = render_router_pair(preset, target, workflow_path)
    rendered = apply_project_local_preservation(rendered, target)
    manifest = RenderedFile(
        MANIFEST_RELATIVE_PATH,
        manifest_content(preset, workflow_path, rendered),
    )
    return rendered + [manifest]


def write_scaffold(
    preset: str,
    target: Path,
    dry_run: bool = False,
    backup: bool = False,
    force: bool = False,
    home: Path | None = None,
) -> list[Path]:
    if backup and force:
        die("--backup and --force are mutually exclusive")
    target = target.expanduser().resolve()
    workflow_path = require_installed_preset(preset, home)
    rendered = render_router_pair(preset, target, workflow_path)
    rendered = apply_project_local_preservation(rendered, target)
    manifest = RenderedFile(MANIFEST_RELATIVE_PATH, manifest_content(preset, workflow_path, rendered))
    all_files = rendered + [manifest]
    existing = [target / item.path for item in all_files if (target / item.path).exists()]
    protected = [path for path in existing if path.name in PROJECT_LOCAL_PRESERVED_FILENAMES]
    if protected and not backup and not force:
        die("refusing to overwrite existing scaffold files: " + ", ".join(str(path) for path in protected))
    if dry_run:
        return [target / item.path for item in all_files]
    if backup:
        for path in existing:
            backup_target = backup_path(path)
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_target)
    for item in all_files:
        path = target / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content, encoding="utf-8")
    return [target / item.path for item in all_files]


def scaffold_state(target: Path) -> dict[str, object] | None:
    path = target / MANIFEST_RELATIVE_PATH
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def scaffold_status(target: Path, home: Path | None = None) -> dict[str, object]:
    target = target.expanduser().resolve()
    manifest_path = target / MANIFEST_RELATIVE_PATH
    result: dict[str, object] = {
        "target": str(target),
        "manifest_path": str(manifest_path),
        "manifest": "missing",
        "clean": False,
    }
    if not manifest_path.exists():
        return result

    try:
        state = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result["manifest"] = "invalid"
        result["error"] = f"manifest is not valid JSON: {exc}"
        return result
    if not isinstance(state, dict):
        result["manifest"] = "invalid"
        result["error"] = "manifest root must be a JSON object"
        return result
    preset = state.get("preset")
    if not isinstance(preset, str) or preset not in PRESET_DEFINITIONS:
        result["manifest"] = "invalid"
        result["error"] = f"manifest preset is missing or unsupported: {preset!r}"
        return result

    schema_version = state.get("schema_version")
    manifest_preset_version = state.get("preset_version")
    manifest_preset_hash = state.get("preset_hash")
    manifest_files = state.get("files") if isinstance(state.get("files"), dict) else {}

    # Schema v2 contract: `files` must include AGENTS.md and CLAUDE.md with sha256
    # strings so router drift can actually be verified. Without these, the status
    # command cannot answer its own question, so refuse to call the manifest
    # usable. Schema v1 manifests predate this contract and stay tolerated
    # (they fall through to the `manifest-missing-hash` per-file status below).
    if schema_version == 2:
        required_router_files = ("AGENTS.md", "CLAUDE.md")
        missing_router_entries = []
        for filename in required_router_files:
            entry = manifest_files.get(filename)
            if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
                missing_router_entries.append(filename)
        if missing_router_entries:
            result["manifest"] = "invalid"
            result["error"] = (
                "schema v2 manifest is missing router hash entries for: "
                + ", ".join(missing_router_entries)
                + ". Regenerate with `mozyo-bridge scaffold apply <preset> --backup`."
            )
            return result

    central_workflow = installed_agent_workflow(preset, home)
    central_version = installed_preset_version(preset, home)
    central_hash = installed_preset_hash(preset, home)
    missing_requirements = [
        required_preset
        for required_preset in preset_install_requirements(preset)
        if installed_preset_hash(required_preset, home) is None
        or installed_preset_version(required_preset, home) is None
    ]
    if missing_requirements:
        central_status = "missing"
    elif manifest_preset_hash is None:
        # Pre-v2 manifest cannot detect content drift. Fall back to version compare.
        if manifest_preset_version is not None and manifest_preset_version == central_version:
            central_status = "ok-version-only"
        else:
            central_status = "drifted-version"
    elif manifest_preset_hash != central_hash:
        central_status = "drifted-content"
    elif manifest_preset_version is not None and manifest_preset_version != central_version:
        # Same content but the recorded version label moved (rare).
        central_status = "drifted-version"
    else:
        central_status = "ok"

    file_rows: list[dict[str, str]] = []
    for filename in sorted(manifest_files.keys()):
        recorded = manifest_files.get(filename)
        expected_hash = (
            recorded.get("sha256") if isinstance(recorded, dict) else None
        )
        on_disk = target / filename
        if not on_disk.exists():
            file_status = "missing"
            on_disk_hash = None
        elif not isinstance(expected_hash, str):
            file_status = "manifest-missing-hash"
            on_disk_hash = sha256_file(on_disk)
        else:
            on_disk_hash = sha256_file(on_disk)
            file_status = "ok" if on_disk_hash == expected_hash else "drifted"
        file_rows.append(
            {
                "path": filename,
                "status": file_status,
                "expected_sha256": expected_hash or "",
                "actual_sha256": on_disk_hash or "",
            }
        )

    # Only an exact `ok` is fully clean. `ok-version-only` means the manifest is
    # schema v1 and we cannot detect content drift; flag so the user regenerates
    # the manifest under schema v2.
    central_drift = central_status != "ok"
    router_drift = any(row["status"] != "ok" for row in file_rows)
    clean = not central_drift and not router_drift

    result.update(
        {
            "manifest": "present",
            "schema_version": schema_version,
            "preset": preset,
            "rule_path": str(central_workflow),
            "manifest_preset_version": manifest_preset_version,
            "manifest_preset_hash": manifest_preset_hash,
            "installed_preset_version": central_version,
            "installed_preset_hash": central_hash,
            "central_status": central_status,
            "files": file_rows,
            "clean": clean,
        }
    )
    return result
