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

from mozyo_bridge import __version__
from mozyo_bridge.shared.errors import die

PRESETS = ("asana", "redmine", "none")
RULE_RELATIVE_PATH = Path("rules") / "presets"
MANIFEST_RELATIVE_PATH = Path(".mozyo-bridge") / "scaffold.json"


@dataclass(frozen=True)
class RenderedFile:
    path: Path
    content: str


def mozyo_bridge_home() -> Path:
    return Path(os.environ.get("MOZYO_BRIDGE_HOME", "~/.mozyo_bridge")).expanduser()


def package_preset_root(preset: str):
    if preset not in PRESETS:
        die(f"unsupported rules preset: {preset}")
    return resources.files("mozyo_bridge.scaffold.presets").joinpath(preset)


def installed_preset_dir(preset: str, home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / RULE_RELATIVE_PATH / preset


def installed_agent_workflow(preset: str, home: Path | None = None) -> Path:
    return installed_preset_dir(preset, home) / "agent-workflow.md"


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
    workflow = installed_agent_workflow(preset, home)
    version = installed_preset_dir(preset, home) / "VERSION"
    if not workflow.exists() or not version.exists():
        die(f"rules preset is not installed: {preset}. Run `mozyo-bridge rules install` first.")
    return workflow


def router_context(preset: str, target: Path, workflow_path: Path) -> dict[str, str]:
    return {
        "preset": preset,
        "preset_version": package_version(preset),
        "project_root": str(target),
        "rule_path": str(workflow_path),
        "mozyo_bridge_version": __version__,
    }


def render_router_pair(preset: str, target: Path, workflow_path: Path) -> list[RenderedFile]:
    context = router_context(preset, target, workflow_path)
    files = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        template = Template(package_text(preset, filename))
        files.append(RenderedFile(Path(filename), template.safe_substitute(context)))
    return files


def manifest_content(preset: str, workflow_path: Path, rendered: list[RenderedFile]) -> str:
    payload = {
        "schema_version": 1,
        "mode": "central",
        "preset": preset,
        "preset_version": package_version(preset),
        "generated_by": f"mozyo-bridge {__version__}",
        "rule_path": str(workflow_path),
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
    manifest = RenderedFile(MANIFEST_RELATIVE_PATH, manifest_content(preset, workflow_path, rendered))
    all_files = rendered + [manifest]
    existing = [target / item.path for item in all_files if (target / item.path).exists()]
    protected = [path for path in existing if path.name in {"AGENTS.md", "CLAUDE.md"}]
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
