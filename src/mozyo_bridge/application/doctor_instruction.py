"""Read-only environment-recovery runbook for mozyo-bridge (Redmine #11051).

`mozyo-bridge doctor instruction` renders the *ordered recovery procedure* a
human or agent should follow to bring a workspace back to green, given the
current `mozyo-bridge doctor` diagnostics. Where `doctor` answers "what is
wrong", this command answers "in what order do I fix it, and which command is
the primary path vs the legacy fallback".

Design constraints (Redmine #11051, design consultation answer #53306):

- Read-only. This module only calls the read-only `run_doctor` /
  `run_instruction_doctor` collectors and synthesizes a runbook. It never
  installs, writes, or contacts a network/ticket system.
- Diagnostics stay in `doctor`. This command does not re-implement the section
  checks; it consumes their statuses and `next_action` hints and orders them.
- Primary vs fallback is explicit. The Claude skill primary path is the plugin
  marketplace; the curl/script path is labelled a legacy fallback. The Codex
  skill primary path is `$skill-installer`.
- Scaffold drift is review-before-restore: the runbook routes drift through
  `scaffold status` / `scaffold diff` *before* `scaffold apply --backup`.
- The CLI taxonomy rename (`instruction doctor/install` -> `runtime-config
  check/install`) is surfaced as a migration section so operators reading the
  runbook learn the new names.
"""

from __future__ import annotations

import argparse
from typing import Any

from mozyo_bridge.application.doctor import (
    BAD_SECTION_STATUSES,
    CLAUDE_GLOBAL_SKILL_INSTALL_HINT,
    CODEX_SKILL_INSTALL_HINT,
    PRESETS,
    doctor_target,
    run_doctor,
)
from mozyo_bridge.application.instruction_doctor import (
    PROFILE_REDMINE_CODEX,
    run_instruction_doctor,
)

# Step status vocabulary. `action` means a remediation command should be run;
# `ok` means the surface is already healthy; `info` is advisory (optional or
# environmental) and never flips the overall result to needs-attention.
STATUS_OK = "ok"
STATUS_ACTION = "action"
STATUS_INFO = "info"

# Claude Code skill install: plugin marketplace is the primary path; the curl
# script is a legacy fallback (kept in sync with bootstrap.md Stage 3a/3c).
CLAUDE_PLUGIN_PRIMARY = (
    "claude plugin marketplace add hollySizzle/mozyo_bridge && "
    "claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user"
)
# Codex skill install: `$skill-installer` is the primary, user-run path; curl is
# the legacy fallback.
CODEX_SKILL_PRIMARY = (
    "$skill-installer "
    "https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent"
)

# Taxonomy migration surfaced in the runbook so operators learn the new names.
# Source of truth for the rename is Redmine #11051 / #53306.
MIGRATIONS = (
    {
        "old": "mozyo-bridge instruction doctor",
        "new": "mozyo-bridge runtime-config check",
        "note": "deprecated alias; removal candidate next minor",
    },
    {
        "old": "mozyo-bridge instruction install",
        "new": "mozyo-bridge runtime-config install",
        "note": "deprecated alias; removal candidate next minor",
    },
)


def _section_needs_action(section: dict[str, Any]) -> bool:
    status = section.get("status")
    return status in BAD_SECTION_STATUSES or status == "warning"


def _command(role: str, cmd: str, *, label: str = "") -> dict[str, str]:
    """One runbook command line. `role` is primary / fallback / only."""
    entry = {"role": role, "command": cmd}
    if label:
        entry["for"] = label
    return entry


def _step(
    step_id: str,
    title: str,
    status: str,
    commands: list[dict[str, str]] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": title,
        "status": status,
        "commands": commands or [],
        "notes": notes or [],
    }


def _scaffold_preset_label(scaffold_section: dict[str, Any]) -> str:
    detail = scaffold_section.get("detail") or {}
    preset = detail.get("preset")
    if isinstance(preset, str) and preset:
        return preset
    return "<" + "|".join(PRESETS) + ">"


def build_runbook(
    doctor_result: dict[str, Any],
    instruction_result: dict[str, Any],
    target: str,
    profile: str,
) -> list[dict[str, Any]]:
    """Synthesize the ordered recovery steps from read-only diagnostics.

    The order follows the design-consultation answer (#53306): CLI/rules
    readiness, central rules, agent skills (primary vs fallback), scaffold
    drift (review-before-restore), runtime config, optional utilities, then a
    final verification pass.
    """
    sections = doctor_result.get("sections", {})
    cli = sections.get("cli", {})
    rules = sections.get("rules", {})
    codex_skill = sections.get("codex_skill", {})
    claude_skill = sections.get("claude_skill", {})
    scaffold = sections.get("scaffold", {})
    nagger = sections.get("claude_nagger", {})
    tmux = sections.get("tmux", {})

    steps: list[dict[str, Any]] = []

    # 1. CLI / rules readiness anchor — the runbook starts from the doctor run.
    cli_notes = [
        f"mozyo-bridge {cli.get('version', '?')} "
        f"({cli.get('executable') or 'not on PATH'})",
        "Diagnose first: mozyo-bridge doctor --target " + target + " --json",
    ]
    cli_drift = cli.get("source_drift")
    if cli_drift and cli.get("status") != STATUS_OK:
        # Stale installed CLI vs repo-local source (Redmine #11855): inside a
        # checkout, point the operator at the repo-local invocation so a
        # sublane does not silently run an older install missing newer
        # subcommands.
        cli_notes.append(
            "Stale installed CLI vs repo-local source: during active "
            f"development run `{cli_drift['repo_local_invocation']} <args>` "
            "instead of the installed mozyo-bridge."
        )
    steps.append(
        _step(
            "cli",
            "CLI readiness",
            STATUS_OK if cli.get("status") == "ok" else STATUS_ACTION,
            notes=cli_notes,
        )
    )

    # 2. Central preset rules.
    rules_commands = [
        _command("only", action) for action in rules.get("next_action", [])
    ] or [_command("only", "mozyo-bridge rules install")]
    steps.append(
        _step(
            "rules",
            "Central preset rules",
            STATUS_ACTION if _section_needs_action(rules) else STATUS_OK,
            commands=rules_commands if _section_needs_action(rules) else [],
            notes=["central preset store agents read at runtime"],
        )
    )

    # 3. Agent skills — primary vs legacy fallback is explicit.
    claude_ok = claude_skill.get("status") in ("ok", "plugin-managed")
    codex_ok = codex_skill.get("status") == "ok"
    skill_status = STATUS_OK if (claude_ok and codex_ok) else STATUS_ACTION
    skill_commands: list[dict[str, str]] = []
    if not claude_ok:
        skill_commands.append(
            _command("primary", CLAUDE_PLUGIN_PRIMARY, label="claude (plugin marketplace)")
        )
        skill_commands.append(
            _command("fallback", CLAUDE_GLOBAL_SKILL_INSTALL_HINT, label="claude (legacy curl)")
        )
    if not codex_ok:
        skill_commands.append(
            _command("primary", CODEX_SKILL_PRIMARY, label="codex ($skill-installer, user-run)")
        )
        skill_commands.append(
            _command("fallback", CODEX_SKILL_INSTALL_HINT, label="codex (legacy curl)")
        )
    skill_notes = [
        "Claude primary = plugin marketplace; curl is a legacy fallback.",
        "Codex primary = $skill-installer (user-run); curl is a legacy fallback.",
        "Restart Claude Code / Codex after any skill install.",
    ]
    steps.append(
        _step("agent_skills", "Agent skills", skill_status, skill_commands, skill_notes)
    )

    # 4. Scaffold drift — review before restore.
    scaffold_action = _section_needs_action(scaffold)
    preset_label = _scaffold_preset_label(scaffold)
    scaffold_commands: list[dict[str, str]] = []
    if scaffold_action:
        scaffold_commands.append(
            _command(
                "primary",
                f"mozyo-bridge scaffold status --target {target}",
                label="review drift first",
            )
        )
        scaffold_commands.append(
            _command(
                "primary",
                f"mozyo-bridge scaffold diff {preset_label} --target {target}",
                label="inspect the diff",
            )
        )
        scaffold_commands.append(
            _command(
                "fallback",
                f"mozyo-bridge scaffold apply {preset_label} --target {target} --backup",
                label="restore only after reviewing the diff",
            )
        )
    steps.append(
        _step(
            "scaffold",
            "Scaffold drift",
            STATUS_ACTION if scaffold_action else STATUS_OK,
            scaffold_commands,
            ["Review the diff before restoring; do not blind-apply over local edits."],
        )
    )

    # 5. Runtime config (repo-root Codex/MCP config). Read-only check first;
    #    write only from verified workspace-defaults, identity changes need an
    #    operator.
    runtime_ok = bool(instruction_result.get("ok"))
    runtime_commands: list[dict[str, str]] = []
    if not runtime_ok:
        runtime_commands.append(
            _command(
                "primary",
                f"mozyo-bridge runtime-config check --target {target} --profile {profile}",
                label="re-check after fixing",
            )
        )
        runtime_commands.append(
            _command(
                "fallback",
                f"mozyo-bridge runtime-config install --target {target} --profile {profile} --write",
                label="only with verified workspace-defaults",
            )
        )
    steps.append(
        _step(
            "runtime_config",
            "Runtime config (repo-root)",
            STATUS_ACTION if not runtime_ok else STATUS_OK,
            runtime_commands,
            [
                "install projects verified workspace-defaults; it never invents values.",
                "Choosing/changing the default project identity needs operator confirmation.",
            ],
        )
    )

    # 6. Optional utilities — only after core readiness.
    optional_notes: list[str] = []
    nagger_status = nagger.get("status")
    if nagger_status in ("skeleton-only", "incomplete"):
        optional_notes.append(
            "Claude Nagger skeleton present but not activated "
            "(copy .claude-nagger/config.yaml.example to config.yaml)."
        )
    artifact = (tmux.get("artifact") or {}) if isinstance(tmux, dict) else {}
    host_wiring = artifact.get("host_wiring") or {}
    for action in host_wiring.get("next_action", []) or []:
        optional_notes.append(action)
    steps.append(
        _step(
            "optional_utilities",
            "Optional utilities (Claude Nagger / tmux UI)",
            STATUS_INFO,
            notes=optional_notes or ["No optional utility action pending."],
        )
    )

    # 7. Final verification.
    steps.append(
        _step(
            "final_verification",
            "Final verification",
            STATUS_INFO,
            commands=[
                _command("only", f"mozyo-bridge doctor --target {target}"),
                _command(
                    "only",
                    f"mozyo-bridge runtime-config check --target {target} --profile {profile}",
                ),
            ],
        )
    )

    return steps


def run_doctor_instruction(args: argparse.Namespace) -> dict[str, Any]:
    """Collect read-only diagnostics and render the recovery runbook result."""
    profile = getattr(args, "profile", PROFILE_REDMINE_CODEX) or PROFILE_REDMINE_CODEX
    target = str(doctor_target(args))

    doctor_result = run_doctor(args)
    instruction_args = argparse.Namespace(
        target=getattr(args, "repo", None), profile=profile
    )
    instruction_result = run_instruction_doctor(instruction_args)

    steps = build_runbook(doctor_result, instruction_result, target, profile)
    pending = [s for s in steps if s["status"] == STATUS_ACTION]
    return {
        "ok": not pending,
        "target": target,
        "profile": profile,
        "doctor_ok": bool(doctor_result.get("ok")),
        "runtime_config_ok": bool(instruction_result.get("ok")),
        "steps": steps,
        "pending_step_ids": [s["id"] for s in pending],
        "migrations": [dict(m) for m in MIGRATIONS],
    }


def format_doctor_instruction_text(result: dict[str, Any]) -> str:
    lines: list[str] = [
        "doctor instruction (read-only recovery runbook): "
        + ("all clear" if result["ok"] else "remediation needed"),
        f"target={result['target']} profile={result['profile']}",
        "",
    ]
    symbols = {STATUS_OK: "ok", STATUS_ACTION: "DO", STATUS_INFO: "i"}
    for index, step in enumerate(result["steps"], start=1):
        sym = symbols.get(step["status"], step["status"])
        lines.append(f"{index}. [{sym}] {step['title']}")
        for command in step["commands"]:
            role = command["role"]
            label = command.get("for")
            suffix = f"  # {label}" if label else ""
            prefix = "" if role == "only" else f"{role}: "
            lines.append(f"     $ {prefix}{command['command']}{suffix}")
        for note in step["notes"]:
            lines.append(f"     - {note}")

    lines.append("")
    lines.append("migration (CLI taxonomy rename):")
    for migration in result["migrations"]:
        lines.append(
            f"  {migration['old']} -> {migration['new']}  ({migration['note']})"
        )

    lines.append("")
    lines.append("result: " + ("ok" if result["ok"] else "needs attention"))
    return "\n".join(lines)
