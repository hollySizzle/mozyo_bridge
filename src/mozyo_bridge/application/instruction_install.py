"""Generate repo-root Codex runtime config from workspace-defaults (#10930).

`mozyo-bridge runtime-config check --profile redmine-codex` (renamed from
`instruction doctor` in Redmine #11051) checks that a Redmine/Codex workspace
declares its verified default project in `<repo>/.codex/config.toml`, but it is
read-only: it never closes the gap between "the source of truth exists" and "the
runtime config reflects it". This module is the write side. It reads the
verified Redmine default project from
`<repo>/.mozyo-bridge/workspace-defaults.yaml` (still the single source of
truth) and renders / merges the `[redmine]` and
`[mcp_servers.redmine_epic_grid]` tables into `<repo>/.codex/config.toml` so
that `runtime-config check` turns green.

Design constraints (Redmine #10930):

- The single source of truth stays `workspace-defaults.yaml`. This command
  only projects it into the Codex runtime config; it never invents values.
- Only the repo-root `<repo>/.codex/config.toml` is written. Home config is
  never read or written.
- Default is a dry-run; an actual write requires `--write`.
- Only a **verified** default project is installed. An incomplete verification
  record fails, mirroring the renderer's "treat as a suggestion only" posture —
  generating runtime config from an unverified default is the anti-goal.
- No credentials are generated. The workspace-defaults loader already rejects
  credential-shape values, so the projected fields are non-secret.
- An existing config is preserved: when the managed tables are absent they are
  appended (other content is left byte-for-byte intact). When they already
  exist and disagree, the command fails and asks the operator to resolve the
  conflict, unless `--force` is given to regenerate the managed tables.
- `.mcp.json` stays deferred — this command does not generate it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mozyo_bridge.application.instruction_doctor import (
    PROFILE_REDMINE_CODEX,
    _toml,
    run_instruction_doctor,
)
from mozyo_bridge.workspace_defaults import (
    WorkspaceDefaults,
    load_repo_defaults,
)

# The Redmine MCP RPC endpoint path. Combined with the host derived from the
# workspace-defaults project URL so no project-specific host is hard-coded in
# distributed source — the host comes from the workspace-local YAML.
_MCP_RPC_PATH = "/mcp/rpc"

CODEX_CONFIG_RELATIVE = Path(".codex/config.toml")

# Managed dotted keys. Everything else in an existing config is preserved.
_REDMINE_TABLE = "redmine"
_MCP_TABLE_HEADER = "mcp_servers.redmine_epic_grid"
# Top-level table headers this command owns (used for force-replace stripping).
_MANAGED_TABLE_HEADERS = ("redmine", "mcp_servers.redmine_epic_grid")


def _mcp_url_from_project_url(project_url: str) -> str:
    """Derive the Redmine MCP RPC URL from the project URL's host."""
    parts = urlsplit(project_url)
    return f"{parts.scheme}://{parts.netloc}{_MCP_RPC_PATH}"


def _toml_basic_string(value: str) -> str:
    """Render a TOML basic string with the minimal required escaping."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _desired_values(defaults: WorkspaceDefaults) -> dict[str, str]:
    """Flat dotted-key -> value map of everything this command manages."""
    project = defaults.default_project
    mcp_url = _mcp_url_from_project_url(project.url)
    return {
        "redmine.default_project": project.identifier,
        "redmine.default_project_name": project.name,
        "redmine.default_project_url": project.url,
        "mcp_servers.redmine_epic_grid.url": mcp_url,
        "mcp_servers.redmine_epic_grid.http_headers.X-Default-Project": (
            project.identifier
        ),
    }


def render_managed_blocks(defaults: WorkspaceDefaults) -> str:
    """Render the `[redmine]` and `[mcp_servers.redmine_epic_grid]` TOML."""
    project = defaults.default_project
    mcp_url = _mcp_url_from_project_url(project.url)
    return (
        "[redmine]\n"
        f"default_project = {_toml_basic_string(project.identifier)}\n"
        f"default_project_name = {_toml_basic_string(project.name)}\n"
        f"default_project_url = {_toml_basic_string(project.url)}\n"
        "\n"
        "[mcp_servers.redmine_epic_grid]\n"
        f"url = {_toml_basic_string(mcp_url)}\n"
        "http_headers = { X-Default-Project = "
        f"{_toml_basic_string(project.identifier)} }}\n"
    )


def _current_value(parsed: dict, dotted_key: str) -> str | None:
    """Read a dotted key from a parsed TOML dict; None if absent / non-string."""
    node: Any = parsed
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def _has_managed_table(parsed: dict) -> bool:
    """True if either managed table already exists in the parsed config."""
    if isinstance(parsed.get("redmine"), dict):
        return True
    mcp = parsed.get("mcp_servers")
    return isinstance(mcp, dict) and isinstance(mcp.get("redmine_epic_grid"), dict)


def _strip_managed_tables(text: str) -> str:
    """Remove the managed top-level table sections from raw TOML text.

    A managed section spans from its `[header]` (or `[header.sub]`) line to the
    line before the next top-level `[`-headed line, or EOF. All other content
    is preserved verbatim. Used only on the `--force` regenerate path.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("["):
            header = stripped.lstrip("[").split("]", 1)[0].strip()
            top = header.split(".")[0]
            sub = header
            is_managed = (
                top == _REDMINE_TABLE
                or sub == _MCP_TABLE_HEADER
                or header.startswith(_MCP_TABLE_HEADER + ".")
                or header == _REDMINE_TABLE
                or header.startswith(_REDMINE_TABLE + ".")
            )
            skipping = is_managed
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    return "".join(out)


def _append_blocks(existing: str, blocks: str) -> str:
    """Append managed blocks to existing text with a single blank-line gap."""
    if not existing.strip():
        return blocks
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + blocks


def run_instruction_install(args: argparse.Namespace) -> dict[str, Any]:
    """Plan or apply the Codex runtime config projection. Returns a result dict."""
    from mozyo_bridge.shared.paths import resolve_repo_root

    profile = getattr(args, "profile", PROFILE_REDMINE_CODEX) or PROFILE_REDMINE_CODEX
    target = resolve_repo_root(getattr(args, "target", None))
    write = bool(getattr(args, "write", False))
    force = bool(getattr(args, "force", False))

    config_path = target / CODEX_CONFIG_RELATIVE
    rel = "<repo>/.codex/config.toml"
    result: dict[str, Any] = {
        "ok": False,
        "profile": profile,
        "target": str(target),
        "config_path": str(config_path),
        "write": write,
        "force": force,
        "action": "none",
        "wrote": False,
        "messages": [],
    }

    # Load the source of truth. `load_repo_defaults` dies (exit 1) on a
    # missing/invalid YAML, a credential-shape value, or the both-name conflict
    # (Redmine #11920 / #11921), which is the correct fatal behavior for this
    # command too; the new project-defaults.yaml wins over the legacy name.
    defaults = load_repo_defaults(target)

    if not defaults.verification.is_complete:
        result["action"] = "unverified"
        result["messages"].append(
            "project-defaults verification is incomplete; refusing to "
            "install an unverified default project into runtime config. "
            "Set verification.verified/verification_date/verified_by in "
            f".mozyo-bridge/{defaults.source_path.name} and re-run."
        )
        return result

    desired = _desired_values(defaults)
    blocks = render_managed_blocks(defaults)
    result["rendered_blocks"] = blocks

    if not config_path.exists():
        result["action"] = "create"
        result["messages"].append(f"{rel} is missing; would create it from workspace-defaults.")
        new_text = blocks
        return _finish(result, config_path, new_text, write, rel)

    existing = config_path.read_text(encoding="utf-8")
    try:
        parsed = _toml.loads(existing)
    except (_toml.TOMLDecodeError, UnicodeDecodeError) as exc:
        result["action"] = "parse-error"
        result["messages"].append(
            f"{rel} is not valid TOML ({exc}); fix it by hand before running install."
        )
        return result

    current = {key: _current_value(parsed, key) for key in desired}
    conflicts = {
        key: {"current": current[key], "desired": desired[key]}
        for key in desired
        if current[key] is not None and current[key] != desired[key]
    }
    missing = [key for key in desired if current[key] is None]

    if not missing and not conflicts:
        result["ok"] = True
        result["action"] = "up-to-date"
        result["messages"].append(f"{rel} already matches workspace-defaults; nothing to do.")
        return result

    if not _has_managed_table(parsed):
        # No managed table exists yet: append ours, preserving all other content.
        result["action"] = "append"
        result["messages"].append(
            f"{rel} exists without the Redmine default block; would append "
            "[redmine] and [mcp_servers.redmine_epic_grid] (other keys preserved)."
        )
        return _finish(result, config_path, _append_blocks(existing, blocks), write, rel)

    # A managed table already exists but is incomplete or conflicting.
    result["conflicts"] = conflicts
    result["missing"] = missing
    if not force:
        result["action"] = "conflict"
        detail = []
        for key, vals in conflicts.items():
            detail.append(f"{key}: existing {vals['current']!r} != workspace-defaults {vals['desired']!r}")
        for key in missing:
            detail.append(f"{key}: missing")
        result["messages"].append(
            f"{rel} already has a Redmine/MCP block that disagrees with "
            "workspace-defaults. Refusing to edit it in place. Resolve manually, "
            "or re-run with --force to regenerate the managed tables "
            "(other tables are preserved). Details: " + "; ".join(detail)
        )
        return result

    # --force: regenerate the managed tables, preserving every other table.
    result["action"] = "force-replace"
    result["messages"].append(
        f"--force: would replace the [redmine] and [mcp_servers.redmine_epic_grid] "
        f"tables in {rel} from workspace-defaults (other tables preserved)."
    )
    stripped = _strip_managed_tables(existing)
    return _finish(result, config_path, _append_blocks(stripped, blocks), write, rel)


def _finish(
    result: dict[str, Any],
    config_path: Path,
    new_text: str,
    write: bool,
    rel: str,
) -> dict[str, Any]:
    """Apply the planned write (or leave a dry-run plan) and verify doctor-green."""
    result["new_text"] = new_text
    if not write:
        result["ok"] = True
        result["messages"].append("dry-run: pass --write to apply.")
        return result
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_text, encoding="utf-8")
    result["wrote"] = True
    # Verify the write actually makes `instruction doctor` green so install and
    # check stay in lockstep (Redmine #10930 completion condition).
    doctor = run_instruction_doctor(
        argparse.Namespace(target=str(config_path.parent.parent), profile=result["profile"])
    )
    result["doctor_ok"] = bool(doctor["ok"])
    result["ok"] = bool(doctor["ok"])
    if result["doctor_ok"]:
        result["messages"].append(f"wrote {rel}; runtime-config check is green.")
    else:
        result["messages"].append(
            f"wrote {rel} but runtime-config check still reports a failure; "
            "inspect with `mozyo-bridge runtime-config check`."
        )
    return result


def format_instruction_install_text(result: dict[str, Any]) -> str:
    head = "ok" if result["ok"] else "FAIL"
    # Canonical command name on stdout (Redmine #11051); the deprecated
    # `instruction install` alias warns on stderr via the CLI.
    lines = [
        f"runtime-config install: {head} profile={result['profile']} "
        f"action={result['action']} target={result['target']}",
    ]
    for message in result["messages"]:
        lines.append(f"  {message}")
    if not result.get("write") and result.get("rendered_blocks") and result["action"] != "up-to-date":
        lines.append("  --- would write ---")
        for block_line in result["rendered_blocks"].splitlines():
            lines.append(f"  {block_line}")
    return "\n".join(lines)
