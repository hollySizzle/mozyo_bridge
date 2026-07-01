"""OOP-first boundary for the ``doctor`` CLI command tail (Redmine #12927).

This carves the residual ``cmd_doctor`` command body and the ``format_doctor_text``
rendering tail out of the orchestration module into one bounded presentation /
command boundary:

- :func:`format_doctor_text` / :func:`_format_skill_block`: the pure
  ``run_doctor`` result-dict -> text renderer. Moved here byte-for-byte from
  :mod:`mozyo_bridge.application.doctor`, which now re-exports
  ``format_doctor_text`` so existing ``doctor.format_doctor_text`` importers
  (``commands.py`` line-1 import, the workspace-registry / state-store inspector
  integration tests) and the ``commands.format_doctor_text`` patch target keep
  resolving unchanged.
- :class:`DoctorCommandOutcome`: the rendered stdout payload + process exit code
  the ``doctor`` command produces.
- :class:`DoctorCommandUseCase`: composes an injected doctor runner with the
  json/text rendering decision and the ``result["ok"]`` -> exit-code mapping,
  leaving ``cmd_doctor`` a thin composition root that only prints the outcome's
  stdout and returns its exit code.

The use case takes the runner and the text renderer as injected callables so the
thin ``cmd_doctor`` adapter can hand it the ``commands``-module globals resolved
*at call time*. That keeps the existing ``commands.run_doctor`` /
``commands.format_doctor_text`` monkeypatch integration tests (and the status
command's doctor-tail continuation, which routes through ``commands.cmd_doctor``)
driving the live or patched doctor unchanged. This module never reads the
filesystem, never imports :mod:`mozyo_bridge.application.doctor`, and never owns
stdout itself; rendering stays side-effect free.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Callable

# The public boundary surface of this command module. ``format_doctor_text`` is
# the pure renderer re-exported by :mod:`mozyo_bridge.application.doctor` for the
# ``doctor.format_doctor_text`` / ``commands.format_doctor_text`` compatibility
# facade; ``DoctorCommandUseCase`` / ``DoctorCommandOutcome`` are the command
# wrapper the thin ``cmd_doctor`` adapter composes. ``_format_skill_block`` is an
# internal helper of ``format_doctor_text`` and is intentionally excluded.
__all__ = [
    "DoctorCommandOutcome",
    "DoctorCommandUseCase",
    "format_doctor_text",
]


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
        drift = cli.get("source_drift")
        if drift:
            lines.append(
                f"  source_drift: {drift['relation']} "
                f"repo_local_source={drift['source_package']} "
                f"source_version={drift['source_version'] or '-'}"
            )
        for action in cli.get("next_action", []):
            lines.append(f"  -> {action}")
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

    registry = sections.get("workspace_registry") or {}
    if registry:
        registry_status_label = registry.get("status", "unknown")
        lines.append(
            f"workspace_registry: {registry_status_label} "
            f"target={registry.get('target', '-')}"
        )
        home_registry = registry.get("home_registry") or {}
        if home_registry:
            schema = home_registry.get("schema_version")
            lines.append(
                f"  home_registry: {home_registry.get('status', 'unknown')} "
                f"path={home_registry.get('path', '-')} "
                f"schema={schema if schema is not None else '-'}"
            )
            if home_registry.get("error"):
                lines.append(f"    error: {home_registry['error']}")
        reg = registry.get("registration") or {}
        lines.append(
            f"  registration: registered={reg.get('registered')} "
            f"session={reg.get('canonical_session') or '-'}"
        )
        anchor = registry.get("anchor") or {}
        lines.append(
            f"  anchor: present={anchor.get('present')} "
            f"path={anchor.get('path', '-')}"
        )
        consistency = registry.get("consistency") or {}
        if consistency:
            lines.append(
                f"  consistency: {consistency.get('status', 'unknown')} "
                f"({consistency.get('detail', '-')})"
            )
        runtime = registry.get("runtime") or {}
        if runtime:
            lines.append(
                f"  runtime: {runtime.get('status', 'unknown')} "
                f"last_seen={runtime.get('last_seen') or '-'} "
                f"({runtime.get('reason', '-')})"
            )
        for action in registry.get("next_action", []):
            lines.append(f"  -> {action}")

    state_store = sections.get("state_store") or {}
    if state_store:
        lines.append(
            f"state_store: {state_store.get('status', 'unknown')} "
            f"home={state_store.get('home', '-')}"
        )
        for component in state_store.get("components", []):
            schema = component.get("schema_version")
            lines.append(
                f"  {component.get('component', '-')}: {component.get('status', 'unknown')} "
                f"kind={component.get('kind', '-')} exists={component.get('exists')} "
                f"schema={schema if schema is not None else '-'} "
                f"integrity={component.get('integrity', '-')} "
                f"recovery={component.get('recovery_policy', '-')} "
                f"-> {component.get('next_action', '-')}"
            )
            notes = component.get("notes") or []
            if notes:
                lines.append(f"    note: {notes[0]}")
        for action in state_store.get("next_action", []):
            lines.append(f"  -> {action}")

    nagger = sections.get("claude_nagger") or {}
    if nagger:
        nagger_status_label = nagger.get("status", "unknown")
        lines.append(
            f"claude_nagger: {nagger_status_label} target={nagger.get('target', '-')}"
        )
        if "config_yaml" in nagger:
            cfg = nagger["config_yaml"]
            lines.append(
                f"  config.yaml: present={cfg['present']} path={cfg['path']}"
            )
        for name, info in (nagger.get("examples") or {}).items():
            lines.append(
                f"  {name}: present={info['present']} path={info['path']}"
            )
        for action in nagger.get("next_action", []):
            lines.append(f"  -> {action}")

    launch_policy = sections.get("claude_launch_policy") or {}
    if launch_policy:
        lines.append(
            f"claude_launch_policy: {launch_policy.get('status', 'unknown')} "
            f"effective_mode={launch_policy.get('effective_mode') or '-'} "
            f"source={launch_policy.get('source', '-')}"
        )
        lines.append(f"  scope: {launch_policy.get('scope', '-')}")
        if launch_policy.get("env_present"):
            lines.append(
                f"  {launch_policy.get('env_var', 'env')}="
                f"{launch_policy.get('env_value', '') or '-'}"
            )
        for action in launch_policy.get("next_action", []):
            lines.append(f"  -> {action}")

    tmux = sections["tmux"]
    lines.append(f"tmux: {tmux['status']}")
    if "detail" in tmux and tmux["detail"]:
        lines.append(f"  {tmux['detail']}")
    artifact = tmux.get("artifact") or {}
    if artifact:
        lines.append(
            f"  agent-ui.conf: present={artifact['present']} "
            f"status={artifact['status']} path={artifact['path']}"
        )
        for action in artifact.get("next_action", []):
            lines.append(f"  -> {action}")
        host_wiring = artifact.get("host_wiring") or {}
        if host_wiring:
            lines.append(
                f"  host_wiring: {host_wiring.get('state', 'unknown')} "
                f"tmux_conf={host_wiring.get('tmux_conf', '-')}"
            )
            current = host_wiring.get("current_source_path")
            if current:
                lines.append(f"    current_source_path: {current}")
            drift = host_wiring.get("drift_reason")
            if drift:
                lines.append(f"    drift_reason: {drift}")
            for action in host_wiring.get("next_action", []) or []:
                lines.append(f"  -> {action}")
    if tmux.get("tmux_pane"):
        lines.append(f"  TMUX_PANE: {tmux['tmux_pane']}")
    if "panes_total" in tmux:
        lines.append(f"  panes: {tmux['panes_total']}")
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
        for warning in tmux.get("warnings", []):
            if warning["kind"] == "claude_pane_cwd_outside_repo":
                lines.append(
                    "  warning: claude_pane cwd is outside repo root; "
                    "project skills may not resolve. "
                    f"cwd={warning['cwd']} repo={warning['repo']}"
                )
    for action in tmux.get("next_action", []):
        lines.append(f"  -> {action}")

    otel = sections.get("otel", {})
    if otel:
        reachable = otel.get("receiver_reachable")
        lines.append(
            f"otel: {otel.get('status', 'unknown')} "
            f"receiver={'reachable' if reachable else 'down (lost by design)'} "
            f"events={otel.get('total', 0)} store={otel.get('store_path', '-')}"
        )
        for gap in otel.get("unobserved_agents", []):
            lines.append(
                f"  unobserved: {gap['agent']} pane={gap['pane_id']} "
                f"session={gap['session']} (no telemetry ever; env not "
                "injected or pre-injection launch)"
            )
        for note in otel.get("notes", []):
            lines.append(f"  note: {note}")

    lines.append("")
    lines.append("result: " + ("ok" if result["ok"] else "needs attention"))
    return "\n".join(lines)


@dataclass(frozen=True)
class DoctorCommandOutcome:
    """The rendered stdout payload and process exit code of the ``doctor`` command.

    ``stdout`` is the single block the thin ``cmd_doctor`` adapter hands to one
    ``print(...)`` call (matching the legacy ``print(format_doctor_text(...))`` /
    ``print(json.dumps(...))`` behaviour byte-for-byte, trailing newline
    included). ``exit_code`` is ``0`` when the doctor result is healthy, ``1``
    otherwise.
    """

    stdout: str
    exit_code: int


class DoctorCommandUseCase:
    """Compose the doctor run, the json/text rendering decision, and the exit code.

    The doctor runner and the text renderer are injected callables so the thin
    ``cmd_doctor`` adapter can supply the ``commands``-module globals resolved at
    call time (preserving the ``commands.run_doctor`` / ``commands.format_doctor_text``
    monkeypatch surface). The use case owns no stdout: it returns a
    :class:`DoctorCommandOutcome` the adapter prints.
    """

    def __init__(
        self,
        run_doctor: Callable[[argparse.Namespace], dict[str, Any]],
        render_text: Callable[[dict[str, Any]], str] = format_doctor_text,
    ) -> None:
        self._run_doctor = run_doctor
        self._render_text = render_text

    def execute(self, args: argparse.Namespace) -> DoctorCommandOutcome:
        result = self._run_doctor(args)
        if getattr(args, "json", False):
            stdout = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        else:
            stdout = self._render_text(result)
        return DoctorCommandOutcome(
            stdout=stdout,
            exit_code=0 if result["ok"] else 1,
        )
