"""Environment doctor for mozyo-bridge.

Diagnoses CLI install, central rules state, Codex / Claude skill install state,
per-repo scaffold readiness, and (optionally) tmux pane health. Read-only: this
module never installs, repairs, or contacts external ticket systems. It only
reports what is missing and the next command an end user should run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import mozyo_bridge
from mozyo_bridge import __version__
from mozyo_bridge.domain.pane_resolver import AGENT_LABELS, is_agent_process, pane_lines
from mozyo_bridge.infrastructure.tmux_client import run_tmux
from mozyo_bridge.scaffold.rules import rules_status, scaffold_status


REQUIRED_SKILL_FILE = "SKILL.md"
SHARED_SKILL_REFERENCES = ("workflow.md", "safety.md", "project-map.md", "release.md")
EXPECTED_SUBCOMMANDS = ("doctor", "rules", "scaffold")

CODEX_SKILL_INSTALL_HINT = (
    "curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main"
    "/scripts/install_codex_skill.sh | sh"
)
# Use `command | VAR=value sh` so the env var actually reaches the script.
# `VAR=value curl ... | sh` sets the var only for curl, not for the downstream sh,
# which would silently fall back to MOZYO_BRIDGE_CLAUDE_SCOPE=project.
CLAUDE_GLOBAL_SKILL_INSTALL_HINT = (
    "curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main"
    "/scripts/install_claude_skill.sh | MOZYO_BRIDGE_CLAUDE_SCOPE=global sh"
)

BAD_SECTION_STATUSES = {
    "missing",
    "missing-or-outdated",
    "outdated",
    "incomplete",
    "invalid",
    "drifted",
    "error",
}


def codex_skill_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def claude_skill_global_home() -> Path:
    return Path(os.environ.get("MOZYO_BRIDGE_CLAUDE_HOME") or "~/.claude").expanduser()


def claude_plugin_skill_root(global_home: Path) -> Path:
    return global_home / "plugins" / "cache" / "mozyo-bridge" / "mozyo-bridge-agent"


def _check_plugin_install(plugin_root: Path) -> dict[str, Any]:
    if not plugin_root.is_dir():
        return {"present": False, "root": str(plugin_root), "versions": []}
    versions: list[dict[str, Any]] = []
    for sha_dir in sorted(plugin_root.iterdir()):
        if not sha_dir.is_dir():
            continue
        skill_md = sha_dir / "skills" / "mozyo-bridge-agent" / "SKILL.md"
        if skill_md.is_file():
            versions.append({"version": sha_dir.name, "skill_md": str(skill_md)})
    return {
        "present": bool(versions),
        "root": str(plugin_root),
        "versions": versions,
    }


def claude_skill_project_dir(args: argparse.Namespace) -> Path:
    override = os.environ.get("MOZYO_BRIDGE_CLAUDE_PROJECT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    target = getattr(args, "repo", None)
    if target:
        return Path(target).expanduser().resolve()
    return Path.cwd().resolve()


def doctor_target(args: argparse.Namespace) -> Path:
    target = getattr(args, "repo", None)
    if target:
        return Path(target).expanduser().resolve()
    return Path.cwd().resolve()


def doctor_home(args: argparse.Namespace) -> Path | None:
    home = getattr(args, "home", None)
    if home:
        return Path(home).expanduser().resolve()
    return None


def doctor_cli_section() -> dict[str, Any]:
    package_path = Path(mozyo_bridge.__file__).resolve().parent
    executable = shutil.which("mozyo-bridge")
    return {
        "status": "ok",
        "version": __version__,
        "executable": executable or "",
        "package_path": str(package_path),
        "python": sys.executable,
        "subcommands": list(EXPECTED_SUBCOMMANDS),
        "next_action": [],
    }


def doctor_rules_section(home: Path | None) -> dict[str, Any]:
    rows = rules_status(home)
    any_bad = any(row["status"] != "ok" for row in rows)
    home_path = str(
        home
        or Path(os.environ.get("MOZYO_BRIDGE_HOME") or "~/.mozyo_bridge").expanduser()
    )
    next_action: list[str] = []
    if any_bad:
        # When the diagnosed home is a custom path (passed via --home), reflect
        # it in the next action so a fresh tester or CI can execute the
        # suggestion verbatim and have it target the same home.
        if home is not None:
            next_action.append(f"mozyo-bridge rules install --home {home}")
        else:
            next_action.append("mozyo-bridge rules install")
    return {
        "status": "ok" if not any_bad else "missing-or-outdated",
        "home": home_path,
        "presets": rows,
        "next_action": next_action,
    }


def _check_skill_dir(skill_dir: Path) -> dict[str, Any]:
    skill_md = skill_dir / REQUIRED_SKILL_FILE
    if not skill_md.exists():
        return {
            "present": False,
            "path": str(skill_dir),
            "skill_md": str(skill_md),
            "references_missing": list(SHARED_SKILL_REFERENCES),
        }
    references = skill_dir / "references"
    missing_refs = [
        name for name in SHARED_SKILL_REFERENCES if not (references / name).exists()
    ]
    return {
        "present": True,
        "path": str(skill_dir),
        "skill_md": str(skill_md),
        "references_missing": missing_refs,
    }


def doctor_codex_skill_section() -> dict[str, Any]:
    home = codex_skill_home()
    skill_dir = home / "skills" / "mozyo-bridge-agent"
    info = _check_skill_dir(skill_dir)
    next_action: list[str] = []
    if not info["present"]:
        status = "missing"
        next_action.append(CODEX_SKILL_INSTALL_HINT)
    elif info["references_missing"]:
        status = "incomplete"
        next_action.append("re-run scripts/install_codex_skill.sh to sync references")
    else:
        status = "ok"
    return {
        "status": status,
        "home": str(home),
        "skill_dir": info["path"],
        "skill_md": info["skill_md"],
        "present": info["present"],
        "references_missing": info["references_missing"],
        "next_action": next_action,
    }


def doctor_claude_skill_section(args: argparse.Namespace) -> dict[str, Any]:
    global_home = claude_skill_global_home()
    global_dir = global_home / "skills" / "mozyo-bridge-agent"
    global_info = _check_skill_dir(global_dir)

    project_dir = claude_skill_project_dir(args)
    project_skill_dir = project_dir / ".claude" / "skills" / "mozyo-bridge-agent"
    project_info = _check_skill_dir(project_skill_dir)

    plugin_info = _check_plugin_install(claude_plugin_skill_root(global_home))

    warnings: list[str] = []
    if global_info["present"] and project_info["present"]:
        warnings.append(
            "personal/global Claude skill at "
            + global_info["path"]
            + " overrides project skill at "
            + project_info["path"]
            + " (Claude Code precedence: personal > project)"
        )

    next_action: list[str] = []
    status = "ok"
    if not global_info["present"] and not project_info["present"]:
        if plugin_info["present"]:
            # plugin marketplace install is the source of the skill; no legacy
            # install hint should be emitted. Treated as healthy and excluded
            # from BAD_SECTION_STATUSES so the overall doctor result stays ok.
            status = "plugin-managed"
        else:
            status = "missing"
            next_action.append(CLAUDE_GLOBAL_SKILL_INSTALL_HINT)
    elif global_info["present"] and global_info["references_missing"]:
        status = "incomplete"
        next_action.append(
            "re-run scripts/install_claude_skill.sh to sync the global skill references"
        )
    elif (
        not global_info["present"]
        and project_info["present"]
        and project_info["references_missing"]
    ):
        status = "incomplete"
        next_action.append(
            "re-run scripts/install_claude_skill.sh against this project to sync references"
        )
    elif warnings:
        status = "warning"

    return {
        "status": status,
        "global_home": str(global_home),
        "global": global_info,
        "project_dir": str(project_dir),
        "project": project_info,
        "plugin": plugin_info,
        "warnings": warnings,
        "next_action": next_action,
    }


def doctor_scaffold_section(args: argparse.Namespace) -> dict[str, Any]:
    target = doctor_target(args)
    home = doctor_home(args)
    home_suffix = f" --home {home}" if home is not None else ""
    detail = scaffold_status(target, home=home)
    manifest = detail.get("manifest")
    central_status = detail.get("central_status")
    next_action: list[str] = []

    if manifest == "missing":
        section_status = "missing"
        next_action.append(
            "mozyo-bridge scaffold rules <asana|redmine|none> --target "
            + str(target)
            + home_suffix
        )
    elif manifest == "invalid":
        section_status = "invalid"
        next_action.append(
            "regenerate manifest with `mozyo-bridge scaffold rules <preset> --target "
            + str(target)
            + home_suffix
            + " --backup`"
        )
    elif detail.get("clean"):
        section_status = "ok"
    else:
        section_status = "drifted"
        preset_label = detail.get("preset") or "<preset>"
        if central_status == "missing":
            if home is not None:
                next_action.append(f"mozyo-bridge rules install --home {home}")
            else:
                next_action.append("mozyo-bridge rules install")
        elif central_status in {"drifted-content", "drifted-version", "ok-version-only"}:
            next_action.append(
                "mozyo-bridge scaffold rules "
                + str(preset_label)
                + " --target "
                + str(target)
                + home_suffix
                + " --backup"
            )
        if any(row.get("status") != "ok" for row in detail.get("files", [])):
            next_action.append(
                "review router files; rerun `mozyo-bridge scaffold rules "
                + str(preset_label)
                + " --target "
                + str(target)
                + home_suffix
                + " --backup` to restore"
            )

    return {
        "status": section_status,
        "target": str(target),
        "detail": detail,
        "next_action": next_action,
    }


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX") or os.environ.get("TMUX_PANE"))


def _cwd_is_under_repo(cwd: str, repo_root: Path) -> bool:
    if not cwd:
        return True
    try:
        Path(cwd).expanduser().resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return True


def doctor_tmux_section(args: argparse.Namespace) -> dict[str, Any]:
    info: dict[str, Any] = {
        "status": "skipped",
        "next_action": [],
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
    }
    if subprocess.run(["sh", "-c", "command -v tmux >/dev/null 2>&1"]).returncode != 0:
        info["status"] = "missing"
        info["detail"] = "tmux not installed"
        info["next_action"] = ["install tmux to use mozyo-bridge pane notifications"]
        return info
    list_result = run_tmux(
        "list-panes", "-a", "-F", "#{pane_id} #{@agent_name}", check=False
    )
    if list_result.returncode != 0:
        # Not running under a tmux server. Doctor stays usable outside tmux.
        info["status"] = "skipped"
        info["detail"] = (
            "not connected to a tmux server (run mozyo-bridge tmux-ui-setup first)"
        )
        return info
    labeled = [
        line
        for line in list_result.stdout.splitlines()
        if len(line.split(" ", 1)) == 2 and line.split(" ", 1)[1]
    ]
    panes = pane_lines()
    info["panes_total"] = len(list_result.stdout.splitlines())
    info["labeled_panes"] = len(labeled)
    info["agent_windows"] = {}
    info["agent_panes"] = {}
    info["legacy_pane_split"] = []
    info["warnings"] = []

    # Scope agent checks to the current tmux session. Cross-session panes
    # are legitimate when the operator keeps parallel project sessions open;
    # only same-session duplicates indicate a real labeling collision.
    pane_env = os.environ.get("TMUX_PANE") or ""
    current_session: str | None = None
    if pane_env:
        for pane in panes:
            if pane["id"] == pane_env:
                location = pane.get("location") or ""
                current_session = location.split(":", 1)[0] or None
                break
    info["current_session"] = current_session or ""

    bad = False
    repo_root_raw = getattr(args, "repo", None) or "."
    repo_root = Path(repo_root_raw).expanduser().resolve()

    # Window-model (standard) per-agent diagnostics, scoped to current session.
    session_panes = (
        [
            pane
            for pane in panes
            if (pane.get("location") or "").split(":", 1)[0] == current_session
        ]
        if current_session is not None
        else []
    )

    # First pass: window-model entries (purely structural — no severity yet).
    window_ok_in_session: dict[str, bool] = {agent: False for agent in AGENT_LABELS}
    for agent in AGENT_LABELS:
        window_panes = [pane for pane in session_panes if pane.get("window_name") == agent]
        window_indexes = {
            (pane.get("location") or "").split(":", 1)[1].split(".", 1)[0]
            for pane in window_panes
            if ":" in (pane.get("location") or "")
        }
        if current_session is None:
            window_entry: dict[str, Any] = {"status": "unscoped"}
        elif not window_panes:
            window_entry = {"status": "missing", "session": current_session}
        elif len(window_indexes) > 1:
            window_entry = {
                "status": "duplicate",
                "session": current_session,
                "windows": sorted(window_indexes),
            }
            bad = True
        else:
            active = next(
                (p for p in window_panes if p.get("pane_active") == "1"),
                window_panes[0],
            )
            command = Path(active.get("command") or "").name
            window_status = "ok" if is_agent_process(command) else "not-agent-process"
            window_entry = {
                "status": window_status,
                "session": current_session,
                "window": next(iter(window_indexes), ""),
                "id": active["id"],
                "process": command,
                "cwd": active.get("cwd", ""),
            }
            if window_status != "ok":
                bad = True
            else:
                window_ok_in_session[agent] = True
            if agent == "claude":
                project_skills_dir = repo_root / ".claude" / "skills"
                if project_skills_dir.exists() and not _cwd_is_under_repo(
                    active.get("cwd", ""), repo_root
                ):
                    info["warnings"].append(
                        {
                            "kind": "claude_pane_cwd_outside_repo",
                            "cwd": active.get("cwd", "") or "-",
                            "repo": str(repo_root),
                        }
                    )
                    bad = True
        info["agent_windows"][agent] = window_entry

    # Second pass: compatibility-path (label-based) diagnostics. Pane-split
    # sessions remain operable through this path; we only flip the section to
    # ``warning`` when neither the window model nor the compat path provides a
    # reachable agent target, or when a label co-exists with an agent window in
    # a different window (genuine mixed-state collision). A pure pane-split
    # session with healthy labeled panes stays ``ok`` with informational
    # ``legacy_pane_split`` rows recorded for migration guidance.
    compat_ok_in_session: dict[str, bool] = {agent: False for agent in AGENT_LABELS}
    for agent in AGENT_LABELS:
        all_matches = [pane for pane in panes if pane["label"] == agent]
        if current_session is not None:
            in_scope = [
                pane
                for pane in all_matches
                if (pane.get("location") or "").split(":", 1)[0] == current_session
            ]
        else:
            in_scope = []
        other_count = len(all_matches) - len(in_scope)

        if not in_scope:
            if current_session is None:
                entry: dict[str, Any] = {
                    "status": "unscoped",
                    "count": len(all_matches),
                    "panes": [
                        {"id": p["id"], "location": p.get("location", "")}
                        for p in all_matches
                    ],
                }
            else:
                entry = {"status": "missing", "session": current_session}
                if other_count:
                    entry["other_sessions"] = other_count
                    entry["other_panes"] = [
                        {"id": p["id"], "location": p.get("location", "")}
                        for p in all_matches
                    ]
            info["agent_panes"][agent] = entry
        elif len(in_scope) > 1:
            entry = {
                "status": "duplicate",
                "count": len(in_scope),
                "session": current_session,
                "panes": [
                    {
                        "id": p["id"],
                        "location": p.get("location", ""),
                        "process": Path(p.get("command") or "").name,
                        "cwd": p.get("cwd", ""),
                    }
                    for p in in_scope
                ],
            }
            if other_count:
                entry["other_sessions"] = other_count
            info["agent_panes"][agent] = entry
            bad = True
        else:
            pane = in_scope[0]
            command = Path(pane["command"]).name
            pane_status = "ok" if is_agent_process(command) else "not-agent-process"
            entry = {
                "status": pane_status,
                "id": pane["id"],
                "process": command,
                "cwd": pane.get("cwd", ""),
            }
            if current_session is not None:
                entry["session"] = current_session
            if other_count:
                entry["other_sessions"] = other_count
            info["agent_panes"][agent] = entry
            window_name = pane.get("window_name") or ""
            if window_name and window_name != agent:
                # Pure compat pane-split sessions stay healthy and only
                # surface the mismatch as info. A label that lives in a
                # non-agent-named window WHILE the same agent has a real
                # window in this session is a genuine mixed-state collision
                # the resolver would have to disambiguate at runtime.
                severity = (
                    "mixed" if window_ok_in_session.get(agent) else "compat"
                )
                info["legacy_pane_split"].append(
                    {
                        "session": current_session or "",
                        "window": window_name,
                        "pane": pane["id"],
                        "label": agent,
                        "severity": severity,
                    }
                )
                if severity == "mixed":
                    bad = True
            if pane_status == "ok":
                compat_ok_in_session[agent] = True
            else:
                bad = True

    if current_session is not None:
        # An agent is "reachable" if either the window model or the compat
        # path resolves cleanly in the current session. If neither does, the
        # operator has a broken routing target and the section is bad.
        for agent in AGENT_LABELS:
            if not (window_ok_in_session[agent] or compat_ok_in_session[agent]):
                bad = True

    info["status"] = "ok" if not bad else "warning"
    return info


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    sections: dict[str, dict[str, Any]] = {
        "cli": doctor_cli_section(),
        "rules": doctor_rules_section(doctor_home(args)),
        "codex_skill": doctor_codex_skill_section(),
        "claude_skill": doctor_claude_skill_section(args),
        "scaffold": doctor_scaffold_section(args),
        "tmux": doctor_tmux_section(args),
    }
    ok = True
    for section in sections.values():
        status = section.get("status")
        if status in BAD_SECTION_STATUSES:
            ok = False
            break
        if status == "warning":
            ok = False
            break
    return {"ok": ok, "sections": sections}


def _format_skill_block(name: str, info: dict[str, Any], indent: str) -> list[str]:
    lines = [
        f"{indent}{name}: present={info['present']} path={info['path']}"
    ]
    if info["references_missing"]:
        lines.append(
            f"{indent}  references missing: {', '.join(info['references_missing'])}"
        )
    return lines


def format_doctor_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    sections = result["sections"]

    cli = sections.get("cli", {})
    cli_status = cli.get("status", "unknown")
    if "version" in cli:
        lines.append(
            f"cli: {cli_status} version={cli['version']} package={cli.get('package_path', '-')}"
        )
        if cli.get("executable"):
            lines.append(f"  executable: {cli['executable']}")
        if cli.get("subcommands"):
            lines.append(f"  subcommands: {', '.join(cli['subcommands'])}")
    else:
        lines.append(f"cli: {cli_status}")

    rules = sections.get("rules", {})
    rules_status_label = rules.get("status", "unknown")
    if "presets" in rules:
        lines.append(f"rules: {rules_status_label} home={rules.get('home', '-')}")
        for row in rules["presets"]:
            lines.append(
                f"  {row['preset']}: {row['status']} "
                f"installed={row['installed']} packaged={row['packaged']}"
            )
    else:
        lines.append(f"rules: {rules_status_label}")
    for action in rules.get("next_action", []):
        lines.append(f"  -> {action}")

    codex = sections.get("codex_skill", {})
    codex_status_label = codex.get("status", "unknown")
    if "skill_dir" in codex:
        lines.append(f"codex_skill: {codex_status_label} dir={codex['skill_dir']}")
        if not codex.get("present", True):
            lines.append(f"  SKILL.md missing at {codex.get('skill_md', '-')}")
        if codex.get("references_missing"):
            lines.append(
                f"  references missing: {', '.join(codex['references_missing'])}"
            )
    else:
        lines.append(f"codex_skill: {codex_status_label}")
    for action in codex.get("next_action", []):
        lines.append(f"  -> {action}")

    claude = sections.get("claude_skill", {})
    claude_status_label = claude.get("status", "unknown")
    lines.append(f"claude_skill: {claude_status_label}")
    if claude.get("global"):
        lines.extend(_format_skill_block("global", claude["global"], "  "))
    if claude.get("project"):
        lines.extend(_format_skill_block("project", claude["project"], "  "))
    plugin = claude.get("plugin") or {}
    if plugin:
        lines.append(f"  plugin: present={plugin.get('present', False)} root={plugin.get('root', '-')}")
        for ver in plugin.get("versions", []) or []:
            lines.append(f"    version: {ver['version']}")
    for warning in claude.get("warnings", []) or []:
        lines.append(f"  warning: {warning}")
    for action in claude.get("next_action", []):
        lines.append(f"  -> {action}")

    scaffold = sections.get("scaffold", {})
    scaffold_status_label = scaffold.get("status", "unknown")
    if "target" in scaffold:
        lines.append(f"scaffold: {scaffold_status_label} target={scaffold['target']}")
        detail = scaffold.get("detail") or {}
        manifest = detail.get("manifest")
        if manifest == "present":
            lines.append(
                f"  preset={detail.get('preset')} "
                f"central={detail.get('central_status')}"
            )
            for file_row in detail.get("files", []):
                lines.append(f"  router {file_row['path']}: {file_row['status']}")
        elif manifest is not None:
            lines.append(f"  manifest: {manifest}")
            if "error" in detail:
                lines.append(f"  error: {detail['error']}")
    else:
        lines.append(f"scaffold: {scaffold_status_label}")
    for action in scaffold.get("next_action", []):
        lines.append(f"  -> {action}")

    tmux = sections["tmux"]
    lines.append(f"tmux: {tmux['status']}")
    if "detail" in tmux and tmux["detail"]:
        lines.append(f"  {tmux['detail']}")
    if tmux.get("tmux_pane"):
        lines.append(f"  TMUX_PANE: {tmux['tmux_pane']}")
    if "panes_total" in tmux:
        lines.append(f"  panes: {tmux['panes_total']}")
        lines.append(f"  labeled_panes: {tmux['labeled_panes']}")
        if tmux.get("current_session"):
            lines.append(f"  current_session: {tmux['current_session']}")
        for agent, agent_info in tmux.get("agent_windows", {}).items():
            status = agent_info.get("status")
            session = agent_info.get("session") or "-"
            if status == "missing":
                lines.append(
                    f"  {agent}_window: missing session={session}"
                )
            elif status == "duplicate":
                wins = ",".join(agent_info.get("windows", []) or [])
                lines.append(
                    f"  {agent}_window: duplicate session={session} windows={wins or '-'}"
                )
            elif status == "unscoped":
                lines.append(
                    f"  {agent}_window: unscoped (run from inside a tmux pane to scope)"
                )
            else:
                window = agent_info.get("window") or "-"
                lines.append(
                    f"  {agent}_window: {agent_info.get('id', '-')} session={session} "
                    f"window={window} process={agent_info.get('process', '-')} status={status}"
                )
        for agent, agent_info in tmux.get("agent_panes", {}).items():
            status = agent_info["status"]
            other = agent_info.get("other_sessions") or 0
            suffix = f" other_sessions={other}" if other else ""
            if status == "missing":
                session = agent_info.get("session") or "-"
                lines.append(
                    f"  {agent}_pane (compat): missing session={session}{suffix}"
                )
            elif status == "duplicate":
                session = agent_info.get("session") or "-"
                pane_ids = ",".join(p["id"] for p in agent_info.get("panes", []))
                lines.append(
                    f"  {agent}_pane (compat): duplicate ({agent_info['count']}) "
                    f"session={session} panes={pane_ids}{suffix}"
                )
            elif status == "unscoped":
                pane_ids = ",".join(p["id"] for p in agent_info.get("panes", []))
                lines.append(
                    f"  {agent}_pane (compat): unscoped ({agent_info['count']}) "
                    f"panes={pane_ids or '-'} (run from inside a tmux pane to scope)"
                )
            else:
                lines.append(
                    f"  {agent}_pane (compat): {agent_info['id']} "
                    f"process={agent_info['process']} status={status}{suffix}"
                )
        for entry in tmux.get("legacy_pane_split", []) or []:
            severity = entry.get("severity") or "compat"
            lines.append(
                f"  legacy_pane_split ({severity}): "
                f"session={entry.get('session') or '-'} "
                f"window={entry.get('window') or '-'} "
                f"pane={entry.get('pane') or '-'} "
                f"label={entry.get('label') or '-'}"
            )
        for warning in tmux.get("warnings", []):
            if warning["kind"] == "claude_pane_cwd_outside_repo":
                lines.append(
                    "  warning: claude_pane cwd is outside repo root; "
                    "project skills may not resolve. "
                    f"cwd={warning['cwd']} repo={warning['repo']}"
                )
    for action in tmux.get("next_action", []):
        lines.append(f"  -> {action}")

    lines.append("")
    lines.append("result: " + ("ok" if result["ok"] else "needs attention"))
    return "\n".join(lines)
