"""Environment doctor for mozyo-bridge.

Diagnoses CLI install, central rules state, Codex / Claude skill install state,
per-repo scaffold readiness, and (optionally) tmux pane health. Read-only: this
module never installs, repairs, or contacts external ticket systems. It only
reports what is missing and the next command an end user should run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from mozyo_bridge.application import tmux_ui as tmux_ui_module
from mozyo_bridge.application.doctor_claude_nagger import (
    ClaudeNaggerSectionUseCase,
    LiveClaudeNaggerReads,
)
from mozyo_bridge.application.doctor_claude_skill import (
    ClaudeSkillSectionUseCase,
    LiveClaudeSkillReads,
)
from mozyo_bridge.application.doctor_cli import (
    CliSectionUseCase,
    LiveCliReads,
)
from mozyo_bridge.application.doctor_codex_skill import (
    CodexSkillSectionUseCase,
    LiveCodexSkillReads,
)
from mozyo_bridge.application.doctor_launch_policy import (
    LaunchPolicySectionUseCase,
    LiveLaunchPolicyReads,
)
from mozyo_bridge.application.doctor_rules import (
    LiveRulesReads,
    RulesSectionUseCase,
)
from mozyo_bridge.application.doctor_scaffold import (
    LiveScaffoldReads,
    ScaffoldSectionUseCase,
)
from mozyo_bridge.application.doctor_health import (
    LiveDoctorSections,
    RunDoctorUseCase,
    UNHEALTHY_SECTION_STATUSES,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import AGENT_LABELS, is_agent_process, pane_lines
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import run_tmux
from mozyo_bridge.scaffold.rules import PRESETS, rules_status, scaffold_state, scaffold_status
from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.state_store import (
    COMPONENTS as _STATE_COMPONENTS,
    RECOVERY_APPEND_ONLY as _RECOVERY_APPEND_ONLY,
    RECOVERY_AUTHORITATIVE as _RECOVERY_AUTHORITATIVE,
    RECOVERY_REBUILDABLE as _RECOVERY_REBUILDABLE,
    STATE_CONTAINER_VERSION as STATE_STORE_SINGLE_DB_CONTAINER_VERSION,
    STATE_STORE_FILENAME as STATE_STORE_SINGLE_DB_FILENAME,
)


REQUIRED_SKILL_FILE = "SKILL.md"
SHARED_SKILL_REFERENCES = ("workflow.md", "safety.md", "project-map.md", "release.md")
EXPECTED_SUBCOMMANDS = ("doctor", "rules", "scaffold")

CODEX_SKILL_INSTALL_HINT = (
    "curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main"
    "/scripts/install_codex_skill.sh | sh"
)
# Use `command | VAR=value sh` so the env var reaches the downstream sh, not just
# curl. The script defaults to `global`; the explicit `MOZYO_BRIDGE_CLAUDE_SCOPE=global`
# documents intent and is required to select non-default scopes (e.g. legacy `project`).
CLAUDE_GLOBAL_SKILL_INSTALL_HINT = (
    "curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main"
    "/scripts/install_claude_skill.sh | MOZYO_BRIDGE_CLAUDE_SCOPE=global sh"
)

# Canonical "hard bad" section statuses now live in ``doctor_health`` as the
# verdict policy's input set. Re-exported here so existing importers
# (``doctor_instruction``) and ``doctor.BAD_SECTION_STATUSES`` references keep
# resolving unchanged.
BAD_SECTION_STATUSES = UNHEALTHY_SECTION_STATUSES


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


# Portable repo-local invocation an end user should switch to during active
# development / sublane dogfooding. Deliberately path-agnostic: no private
# checkout path, no operator-specific home — just the in-repo source entry that
# every checkout exposes. Concrete paths belong in the operator's runtime, not
# this distributed diagnostic.
REPO_LOCAL_INVOCATION = "PYTHONPATH=src python3 -m mozyo_bridge"

_SOURCE_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


def _read_source_version(init_path: Path) -> str | None:
    """Parse ``__version__`` from a repo-local ``mozyo_bridge/__init__.py``.

    Returns None when the file cannot be read or carries no recognizable
    ``__version__`` assignment. Reading is text-only; the source is never
    imported (importing it would shadow the running package).
    """
    try:
        text = init_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SOURCE_VERSION_RE.search(text)
    return match.group(1) if match else None


def repo_local_source_drift(
    target: Path, running_package_path: Path, running_version: str
) -> dict[str, Any] | None:
    """Detect a stale installed CLI relative to the repo-local source.

    During active development / sublane dogfooding inside a ``mozyo_bridge``
    checkout, running the *installed* ``mozyo-bridge`` can shadow newer source
    under ``src/`` — the install may lack the latest subcommands (the observed
    case: a freshly added ``agents targets`` missing from the stale install,
    Redmine #11855 / #11850 j#57328). This compares the running CLI's package
    against the checkout's ``src/mozyo_bridge`` and reports drift.

    Returns None — i.e. nothing to warn about — when either:

    - the target has no repo-local source (``src/mozyo_bridge/__init__.py``
      absent). This is the normal post-release case: the installed CLI is the
      whole story and there is nothing to be stale against, so doctor stays
      quiet outside a checkout. This is the line that keeps post-release
      *normal usage* from being confused with active *dogfooding* usage.
    - the running CLI already *is* the repo-local source (editable install or
      ``PYTHONPATH=src``), so there is no drift to flag.

    Otherwise returns a record describing the drift. The discriminator for a
    warning is *checkout presence + running package ≠ repo-local source*, not
    version equality: during active dogfooding the package version is not
    bumped until release, so the installed CLI and the checkout can share the
    same ``__version__`` (e.g. ``0.7.0``) while differing by commits — the
    originating case (a stale install missing ``agents targets``) cannot be
    detected from the version string alone (Redmine #11855 review j#57416).
    Every non-None record therefore warrants the repo-local-invocation
    guidance. ``relation`` is kept as informative context:
    ``version-differs`` (installed and source versions disagree),
    ``same-version`` (paths differ but versions match — still a warning, since
    equal versions do not guarantee equal commits during dogfooding), or
    ``unknown`` (source present but its version could not be read).
    """
    source_pkg = (target / "src" / "mozyo_bridge").resolve()
    init_path = source_pkg / "__init__.py"
    if not init_path.is_file():
        return None
    if running_package_path.resolve() == source_pkg:
        return None
    source_version = _read_source_version(init_path)
    if source_version is None:
        relation = "unknown"
    elif source_version == running_version:
        relation = "same-version"
    else:
        relation = "version-differs"
    return {
        "source_package": str(source_pkg),
        "source_version": source_version or "",
        "running_version": running_version,
        "running_package": str(running_package_path),
        "relation": relation,
        "repo_local_invocation": REPO_LOCAL_INVOCATION,
    }


def doctor_cli_section(target: Path | None = None) -> dict[str, Any]:
    """Report the running ``mozyo-bridge`` CLI install state (+ source drift).

    Read-only: it never installs or repairs. ``ok`` when the running CLI is the
    whole story; ``warning`` when a checkout's repo-local source under
    ``src/mozyo_bridge`` differs from the running install (active-development /
    dogfooding case), with the repo-local-invocation guidance as next_action.

    Thin handler: the external reads (running ``__version__`` / executable /
    package path / ``sys.executable`` / expected subcommands + the
    ``repo_local_source_drift`` detection), the authority-bearing verdict (status
    + the drift warning message), and the legacy section dict assembly now live
    behind the typed boundary in ``doctor_cli`` (#12845). ``LiveCliReads`` drives
    the running-package introspection and resolves ``repo_local_source_drift``
    at call time, ``CliSectionUseCase`` applies the pure ``evaluate_cli_section``
    policy, and the legacy section dict is preserved byte-for-byte. The
    source-drift *detection* helper (``repo_local_source_drift`` /
    ``_read_source_version``) stays here as the reusable read concern.
    """
    return CliSectionUseCase(LiveCliReads(target)).execute()


def doctor_rules_section(home: Path | None) -> dict[str, Any]:
    """Report the central rules-preset install state for ``mozyo-bridge``.

    Read-only: it never installs or repairs. ``ok`` when every installed preset
    row is ``ok``; ``missing-or-outdated`` when any preset row is not ``ok`` —
    with the matching ``mozyo-bridge rules install`` next_action guidance
    (``--home``-qualified when a custom home was diagnosed).

    Thin handler: the external read (``rules_status`` preset scan +
    ``MOZYO_BRIDGE_HOME`` home resolution), the authority-bearing verdict (status
    + next_action), and the legacy section dict assembly now live behind the
    typed boundary in ``doctor_rules`` (#12844). ``LiveRulesReads`` drives the
    preset read at call time and resolves the ``--home``-aware install command,
    ``RulesSectionUseCase`` applies the pure ``evaluate_rules_section`` policy,
    and the legacy section dict is preserved byte-for-byte.
    """
    return RulesSectionUseCase(LiveRulesReads(home)).execute()


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
    """Report the Codex skill install state for ``mozyo-bridge``.

    Read-only: it never installs or repairs. ``missing`` when no ``SKILL.md`` is
    present under the Codex skill home, ``incomplete`` when the skill is present
    but shared references are absent, ``ok`` otherwise — with the matching
    operator ``next_action`` guidance.

    Thin handler: the external read (``codex_skill_home`` / ``_check_skill_dir``)
    and the authority-bearing verdict (status + next_action) now live behind the
    typed boundary in ``doctor_codex_skill`` (#12836). ``LiveCodexSkillReads``
    drives the filesystem read at call time, ``CodexSkillSectionUseCase`` applies
    the pure ``evaluate_codex_skill_section`` policy, and the legacy section dict
    is preserved byte-for-byte.
    """
    return CodexSkillSectionUseCase(LiveCodexSkillReads()).execute()


def doctor_claude_skill_section(args: argparse.Namespace) -> dict[str, Any]:
    """Report the Claude skill install state for ``mozyo-bridge``.

    Read-only: it never installs or repairs. Inspects the three Claude skill
    surfaces (personal/global skill home, project-local ``.claude`` skill dir,
    plugin marketplace cache) and reports the section ``status`` with the
    matching precedence ``warnings`` and operator ``next_action`` guidance —
    ``plugin-managed`` when only the plugin cache supplies the skill, ``missing``
    when nothing is installed, ``incomplete`` when shared references are absent,
    ``warning`` on the legacy global+project precedence collision, ``ok``
    otherwise.

    Thin handler: the external reads (``claude_skill_global_home`` /
    ``claude_skill_project_dir`` / ``claude_plugin_skill_root`` /
    ``_check_skill_dir`` / ``_check_plugin_install``) and the authority-bearing
    verdict (status + warnings + next_action) now live behind the typed boundary
    in ``doctor_claude_skill`` (#12843). ``LiveClaudeSkillReads`` drives the
    filesystem reads at call time, ``ClaudeSkillSectionUseCase`` applies the pure
    ``evaluate_claude_skill_section`` policy, and the legacy section dict is
    preserved byte-for-byte.
    """
    return ClaudeSkillSectionUseCase(LiveClaudeSkillReads(args)).execute()


def doctor_scaffold_section(args: argparse.Namespace) -> dict[str, Any]:
    """Report the per-repo scaffold readiness for ``mozyo-bridge``.

    Read-only: it never installs or repairs. ``missing`` when the target has no
    scaffold manifest, ``invalid`` when the manifest is unusable, ``ok`` when the
    scaffold is clean, ``drifted`` otherwise — with the matching operator
    remediation ``next_action`` guidance (``--home``-qualified when a custom home
    was diagnosed).

    Thin handler: the external read (``doctor_target`` / ``doctor_home`` argument
    resolution + the ``scaffold_status`` manifest/central-state scan), the
    authority-bearing verdict (section status + the remediation next_action), and
    the legacy section dict assembly now live behind the typed boundary in
    ``doctor_scaffold`` (#12853). ``LiveScaffoldReads`` drives the scaffold read
    through the ``doctor`` module at call time, ``ScaffoldSectionUseCase`` applies
    the pure ``evaluate_scaffold_section`` policy, and the legacy section dict is
    preserved byte-for-byte.
    """
    return ScaffoldSectionUseCase(LiveScaffoldReads(args)).execute()


CLAUDE_NAGGER_DIRNAME = ".claude-nagger"
CLAUDE_NAGGER_EXAMPLES = (
    "config.yaml.example",
    "command_conventions.yaml.example",
    "mcp_conventions.yaml.example",
)
CLAUDE_NAGGER_MANIFEST_PREFIX = ".claude-nagger/"
TMUX_UI_RELATIVE_PATH = Path(".mozyo-bridge/tmux/agent-ui.conf")
TMUX_UI_MANIFEST_PATH = ".mozyo-bridge/tmux/agent-ui.conf"


def _scaffold_manifest_files(target: Path) -> set[str]:
    """Return the repo-relative POSIX paths the scaffold manifest tracks.

    Returns an empty set when no manifest exists, when the manifest is
    unreadable, or when the manifest's ``files`` entry is malformed.
    Doctor uses this as the source-of-truth for "did the operator
    intend to install this category" — disk state alone can mislead
    (e.g. leftover ``.bak.*`` backup files inside a directory that the
    operator opted out of with ``--skip-* --backup``).
    """
    try:
        state = scaffold_state(target)
    except (OSError, json.JSONDecodeError):
        return set()
    if not state or not isinstance(state, dict):
        return set()
    files = state.get("files")
    if not isinstance(files, dict):
        return set()
    return {name for name in files.keys() if isinstance(name, str)}


def doctor_claude_nagger_section(args: argparse.Namespace) -> dict[str, Any]:
    """Report on the governed preset's Claude Nagger artifacts in the target.

    The scaffold manifest is the source-of-truth for "did the project
    install Claude Nagger?". A project that scaffolded with
    ``--skip-nagger`` (or never opted in) is treated as ``skipped``
    even if backup files (``.bak.*``) or unrelated debris exist under
    ``.claude-nagger/``. Reading the manifest first avoids the false
    ``incomplete`` verdict that a directory-only check produces after
    a ``--skip-nagger --backup`` opt-out.

    The collector's three responsibilities — the external read of the
    ``.claude-nagger/`` skeleton + the scaffold manifest, the verdict authority,
    and the legacy section dict assembly — now live behind the typed boundary in
    ``doctor_claude_nagger`` (#12859). ``LiveClaudeNaggerReads`` drives the read
    through the ``doctor`` module at call time (``doctor_target``, the
    ``CLAUDE_NAGGER_*`` constants, and ``_scaffold_manifest_files``),
    ``ClaudeNaggerSectionUseCase`` applies the pure
    ``evaluate_claude_nagger_section`` policy, and the legacy section dict is
    preserved byte-for-byte.
    """
    return ClaudeNaggerSectionUseCase(LiveClaudeNaggerReads(args)).execute()


def doctor_tmux_ui_artifact_info(target: Path) -> dict[str, Any]:
    """Inspect the governed preset's tmux UI snippet on the target.

    Same source-of-truth contract as ``doctor_claude_nagger_section``:
    the scaffold manifest decides whether the project installed the
    snippet. ``skipped`` means the manifest does not track it (or
    there is no manifest); ``ok`` means the manifest tracks it and
    the file is on disk; ``incomplete`` means the manifest tracks it
    but the file was removed locally (real drift).

    The ``host_wiring`` sub-record reports whether the host tmux
    config (default ``~/.tmux.conf``) currently sources the snippet
    via the managed block written by ``mozyo-bridge tmux-ui install``.
    Host wiring is independent of the artifact landing — operators
    may have the artifact installed but choose not to wire it (the
    snippet works just as well via per-session ``source-file``).
    """
    tracked = _scaffold_manifest_files(target)
    is_tracked = TMUX_UI_MANIFEST_PATH in tracked
    snippet_path = target / TMUX_UI_RELATIVE_PATH
    present = snippet_path.exists()
    next_action: list[str] = []
    if not is_tracked:
        status = "skipped"
        next_action.append(
            "tmux UI helper is opt-out (manifest does not track agent-ui.conf); "
            f"rerun `mozyo-bridge scaffold apply <preset> --target {target}` "
            "without --skip-tmux-ui to install the snippet"
        )
    elif present:
        status = "ok"
    else:
        status = "incomplete"
        next_action.append(
            f"manifest tracks {snippet_path} but the file is missing; "
            "rerun scaffold apply --backup to restore"
        )

    host_conf = tmux_ui_module.default_host_tmux_conf()
    host_wiring = tmux_ui_module.compute_status(target, host_conf)
    wiring_actions: list[str] = []
    if is_tracked and present:
        if host_wiring["state"] == tmux_ui_module.STATE_NOT_INSTALLED:
            wiring_actions.append(
                "host tmux config does not source agent-ui.conf; run "
                f"`mozyo-bridge tmux-ui install --target {target}` to wire it"
            )
        elif host_wiring["state"] == tmux_ui_module.STATE_DRIFT:
            wiring_actions.append(
                "host tmux config has a managed block pointing elsewhere "
                f"({host_wiring.get('drift_reason')}); rerun "
                f"`mozyo-bridge tmux-ui install --target {target} --force` to refresh"
            )

    return {
        "status": status,
        "path": str(snippet_path),
        "present": present,
        "manifest_tracks_tmux_ui": is_tracked,
        "host_wiring": {
            "state": host_wiring["state"],
            "tmux_conf": host_wiring["tmux_conf"],
            "tmux_conf_exists": host_wiring["tmux_conf_exists"],
            "current_source_path": host_wiring["current_source_path"],
            "expected_snippet": host_wiring["expected_snippet"],
            "drift_reason": host_wiring["drift_reason"],
            "next_action": wiring_actions,
        },
        "next_action": next_action,
    }


def doctor_claude_launch_policy_section() -> dict[str, Any]:
    """Report the reproducible Claude launch permission policy (#11925).

    Read-only: it never launches a pane and never raises. It answers the
    one operator question that used to silently stall a lane — "will the
    *next* cockpit / sublane Claude pane mozyo creates come up in auto
    mode?" — and explains why (launch-context policy default vs the
    ``MOZYO_CLAUDE_PERMISSION_MODE`` override rail).

    ``ok`` when future cockpit / sublane Claude panes will launch ``auto``;
    ``warning`` when an env override turns auto off, or when the env var
    holds an invalid value (which would hard-error at actual launch). The
    policy is non-retroactive, so this describes future panes only —
    already-running panes keep whatever mode they started with.

    Thin handler: the external read (``describe_launch_policy``) and the
    authority-bearing verdict (status + next_action) now live behind the typed
    boundary in ``doctor_launch_policy`` (#12835). ``LiveLaunchPolicyReads``
    drives the read at call time, ``LaunchPolicySectionUseCase`` applies the
    pure ``evaluate_launch_policy_section`` policy, and the legacy section dict
    is preserved byte-for-byte.
    """
    return LaunchPolicySectionUseCase(LiveLaunchPolicyReads()).execute()


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
    list_result = run_tmux("list-panes", "-a", "-F", "#{pane_id}", check=False)
    if list_result.returncode != 0:
        # Not running under a tmux server. Doctor stays usable outside tmux.
        info["status"] = "skipped"
        info["detail"] = (
            "not connected to a tmux server (run `mozyo` to start the repo session)"
        )
        return info
    panes = pane_lines()
    info["panes_total"] = len(list_result.stdout.splitlines())
    info["agent_windows"] = {}
    info["warnings"] = []
    next_actions: list[str] = []

    # Scope agent checks to the current tmux session. Cross-session panes
    # are legitimate when the operator keeps parallel project sessions open.
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

    session_panes = (
        [
            pane
            for pane in panes
            if (pane.get("location") or "").split(":", 1)[0] == current_session
        ]
        if current_session is not None
        else []
    )

    for agent in sorted(AGENT_LABELS):
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
            bad = True
            next_actions.append(
                f"run `mozyo` from the repo, or `mozyo-bridge init {agent}` from the pane "
                f"you want to be `{agent}`"
            )
        elif len(window_indexes) > 1:
            window_entry = {
                "status": "duplicate",
                "session": current_session,
                "windows": sorted(window_indexes),
            }
            bad = True
            next_actions.append(
                f"resolve duplicate `{agent}` windows in session '{current_session}'; "
                "tmux tolerates duplicates but the resolver does not"
            )
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
                next_actions.append(
                    f"`{agent}` window in session '{current_session}' is running "
                    f"`{command or '-'}`; start the agent CLI or `mozyo-bridge init "
                    f"{agent}` on the pane that is"
                )
            if agent == "claude" and window_status == "ok":
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

    # De-dupe while preserving order so repeated suggestions (e.g. both
    # agents missing → both producing the bare-`mozyo` hint) collapse.
    seen: set[str] = set()
    info["next_action"] = [
        action for action in next_actions if not (action in seen or seen.add(action))
    ]
    info["status"] = "ok" if not bad else "warning"
    return info


def doctor_otel_section(args: argparse.Namespace) -> dict[str, Any]:
    """OTel receiver health and observation-gap report (Redmine #11677).

    Diagnosis only: the store is never treated as a source of truth and
    nothing is written to Redmine. A down receiver is NOT an error — OTLP
    is push-based, so it means "telemetry is being lost by design until
    restart"; liveness questions go to the tmux layer. Agent panes whose
    (session, agent) pair has never produced a store source are surfaced
    as observation gaps (env not injected / pre-injection launch /
    unsupported CLI), which is exactly the new blind-spot class the owner
    decision (#11639 constraint 3) requires doctor to expose.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.agent_activity import summarize_activity
    from mozyo_bridge.otel_store import OtelEventStore

    store = OtelEventStore()
    section: dict[str, Any] = {
        "status": "ok",
        "store_path": str(store.path),
        "store_exists": store.path.exists(),
        "notes": [],
    }
    section.update(store.counts())

    healthz = "http://127.0.0.1:4318/healthz"
    try:
        with urllib.request.urlopen(healthz, timeout=2) as response:
            _json.loads(response.read().decode("utf-8"))
        section["receiver_reachable"] = True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        section["receiver_reachable"] = False
        section["receiver_error"] = str(exc)
        section["notes"].append(
            "receiver not reachable: telemetry sent now is lost BY DESIGN "
            "(best-effort store, not an error). Start it with "
            "`mozyo-bridge otel serve`; use `agents list` / `session list` "
            "for liveness in the meantime."
        )

    # Observation gaps: agent panes with no telemetry source ever.
    observed_pairs = set()
    for activity in summarize_activity(store):
        hints = activity.match_hints
        if isinstance(hints.get("session"), str) and isinstance(
            hints.get("agent"), str
        ):
            observed_pairs.add((hints["session"], hints["agent"]))
    gaps: list[dict[str, str]] = []
    try:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            discover_agents,
            fold_agents_by_pane,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import try_pane_lines

        panes = try_pane_lines()
        if panes is None:
            section["notes"].append(
                "tmux unavailable: observation-gap check skipped"
            )
        else:
            for record in fold_agents_by_pane(discover_agents(panes)):
                if record.agent_kind == "unknown":
                    continue
                pairs = {
                    (view.session, record.agent_kind) for view in record.views
                }
                if not pairs & observed_pairs:
                    gaps.append(
                        {
                            "pane_id": record.pane_id,
                            "session": record.session,
                            "agent": record.agent_kind,
                        }
                    )
    except Exception as exc:  # diagnosis must never take doctor down
        section["notes"].append(f"observation-gap check failed: {exc}")
    section["unobserved_agents"] = gaps
    if gaps:
        section["notes"].append(
            f"{len(gaps)} agent pane(s) have never emitted telemetry "
            "(OTel env not injected, launched before injection, or the "
            "CLI does not emit). Restart them via `mozyo` / `mozyo-bridge "
            "init <agent>` to inject; until then their activity is "
            "`unknown` and falls back to tmux liveness."
        )
    return section


def _live_session_names() -> set[str] | None:
    """Best-effort set of live tmux session names; ``None`` when unavailable.

    Liveness is a tmux question, never a registry one — the workspace-registry
    invariant is that the registry stores identity, not runtime state. Any
    failure (tmux not installed, no server, non-zero exit) collapses to
    ``None`` so the registry section degrades to ``unknown`` rather than
    guessing a workspace is live or dead.
    """
    try:
        result = run_tmux("list-sessions", "-F", "#{session_name}", check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def doctor_workspace_registry_section(args: argparse.Namespace) -> dict[str, Any]:
    """Diagnose home registry / workspace anchor / runtime identity (#11426).

    Strictly read-only and additive to the existing scaffold/toolchain
    sections: it never creates the registry, never writes ``last_seen``, and
    never touches the anchor. It reports four layers:

    - **home registry** existence / schema / readability (safe error state);
    - **workspace registration** for the target repo;
    - **anchor** presence and anchor-vs-registry consistency;
    - **runtime** relationship between the registry's ``last_seen`` cache and
      live tmux state, explained — never conflated — so the registry is not
      mistaken for live runtime state.

    Only genuine problems flip the section red: an unreadable registry
    (``error``), an unsupported schema (``invalid``), or a registry/anchor
    workspace-id ``drift`` (``drifted``). A never-registered workspace, a
    missing registry with a recovery anchor, or a missing anchor are all
    normal, recoverable states and stay ``ok`` with an actionable hint.
    """
    from mozyo_bridge import workspace_registry as wr

    target = doctor_target(args)
    home = doctor_home(args)

    health = wr.inspect_registry_health(home)
    registry_usable = health["status"] in (
        wr.REGISTRY_HEALTH_OK,
        wr.REGISTRY_HEALTH_MISSING,
    )

    # All reads below degrade safely (load/read return None on damage), but we
    # only trust a registry row when the health probe says the registry is
    # actually usable; otherwise registration state is "unknown".
    record = wr.load_workspace_by_path(target, home=home) if registry_usable else None
    anchor = wr.read_anchor(target)
    anchor_names = wr.anchor_resolution(target)
    resolved = wr.resolve_canonical_session(target, home=home)

    next_action: list[str] = []

    # --- registration layer -------------------------------------------------
    if not registry_usable:
        registered: bool | None = None
    else:
        registered = record is not None
    registration = {
        "registered": registered,
        "workspace_id": record.workspace_id if record else None,
        "canonical_session": record.canonical_session if record else None,
        "display_path": record.display_path if record else None,
        "preset": record.preset if record else None,
        "preset_version": record.preset_version if record else None,
    }

    # Anchor name compatibility (Redmine #11920 / #11921): report which name is
    # on disk so the legacy / both-exist migration states are visible.
    if anchor_names.both_exist:
        name_state = "both"
    elif anchor_names.using_legacy:
        name_state = "legacy"
    elif anchor_names.new_exists:
        name_state = "new"
    else:
        name_state = "none"
    anchor_info = {
        "path": str(wr.anchor_path(target)),
        "legacy_path": str(wr.legacy_anchor_path(target)),
        "name_state": name_state,
        "present": anchor is not None,
        "workspace_id": anchor.get("workspace_id") if anchor else None,
        "canonical_session": anchor.get("canonical_session") if anchor else None,
    }

    # --- consistency layer --------------------------------------------------
    if not registry_usable:
        consistency_status = "unknown"
        consistency_detail = (
            "home registry is not usable; registration/consistency cannot be "
            "determined until it is repaired"
        )
    elif record is not None and anchor is not None:
        if record.workspace_id == anchor["workspace_id"]:
            consistency_status = "ok"
            consistency_detail = "registry row and anchor agree on workspace_id"
        else:
            consistency_status = "drift"
            consistency_detail = (
                "registry row and anchor disagree on workspace_id "
                f"(registry {record.workspace_id} vs anchor {anchor['workspace_id']})"
            )
    elif record is not None and anchor is None:
        consistency_status = "registry-only"
        consistency_detail = "registered, but the workspace-local anchor is missing"
    elif record is None and anchor is not None:
        consistency_status = "anchor-only"
        consistency_detail = (
            "anchor present but the home registry has no row for this workspace "
            "(registry loss or never upserted); resolution still works from the anchor"
        )
    else:
        consistency_status = "unregistered"
        consistency_detail = (
            "workspace is not registered; session name resolves via path "
            "derivation (pre-registry behavior)"
        )

    # --- runtime / last_seen layer (tmux is the liveness source) -----------
    canonical_session = resolved.name
    live_sessions = _live_session_names()
    if live_sessions is None:
        session_live: bool | None = None
        runtime_status = "unknown"
        runtime_reason = (
            "tmux unavailable; liveness unknown. registry last_seen is a "
            "registration-time cache, not live runtime state"
        )
    elif canonical_session in live_sessions:
        session_live = True
        runtime_status = "active"
        runtime_reason = (
            f"canonical session '{canonical_session}' is live in tmux now; "
            "last_seen reflects the last `workspace register`, not this liveness"
        )
    else:
        session_live = False
        runtime_status = "stale"
        runtime_reason = (
            f"canonical session '{canonical_session}' is not live in tmux; "
            "last_seen is the last registration touch, not runtime activity"
        )
    runtime = {
        "last_seen": record.last_seen if record else None,
        "canonical_session": canonical_session,
        "session_live": session_live,
        "status": runtime_status,
        "reason": runtime_reason,
    }

    # --- overall status + next actions -------------------------------------
    if health["status"] == wr.REGISTRY_HEALTH_UNREADABLE:
        section_status = "error"
        next_action.append(
            f"home registry {health['path']} is unreadable; move the corrupt "
            "file aside and re-register from each workspace's anchor "
            "(`mozyo-bridge workspace register`)"
        )
    elif health["status"] == wr.REGISTRY_HEALTH_INVALID_SCHEMA:
        section_status = "invalid"
        next_action.append(
            f"home registry {health['path']} has schema version "
            f"{health['schema_version']}, but this mozyo-bridge supports "
            f"{health['expected_schema_version']}; upgrade mozyo-bridge, or "
            "move the registry aside and re-register from anchors "
            "(`mozyo-bridge workspace register`)"
        )
    elif anchor_names.both_exist:
        section_status = "drifted"
        next_action.append(
            f"both {wr.ANCHOR_RELATIVE.as_posix()} and "
            f"{wr.ANCHOR_LEGACY_RELATIVE.as_posix()} exist; the new name is "
            f"authoritative — remove the legacy "
            f"{wr.ANCHOR_LEGACY_RELATIVE.as_posix()} after confirming the new "
            "anchor (no silent merge)"
        )
    elif consistency_status == "drift":
        section_status = "drifted"
        next_action.append(
            "registry row and anchor disagree on workspace_id; run "
            "`mozyo-bridge workspace register` to reconcile (the anchor wins)"
        )
    else:
        section_status = "ok"
        if anchor_names.using_legacy:
            next_action.append(
                f"anchor uses the legacy name "
                f"{wr.ANCHOR_LEGACY_RELATIVE.as_posix()}; run `mozyo-bridge "
                f"workspace register` to migrate it to "
                f"{wr.ANCHOR_RELATIVE.as_posix()} (the legacy name still reads)"
            )
        if consistency_status == "anchor-only":
            next_action.append(
                "home registry has no row for this workspace; run "
                "`mozyo-bridge workspace register` to restore it from the anchor"
            )
        elif consistency_status == "registry-only":
            next_action.append(
                "workspace anchor is missing; run `mozyo-bridge workspace "
                "register` to rewrite it (keeps the existing identity)"
            )
        elif consistency_status == "unregistered":
            next_action.append(
                "workspace is not registered; run `mozyo-bridge workspace "
                "register` to pin a durable identity (optional — resolution "
                "already falls back to path derivation)"
            )

    return {
        "status": section_status,
        "target": str(target),
        "home_registry": health,
        "registration": registration,
        "anchor": anchor_info,
        "consistency": {
            "status": consistency_status,
            "detail": consistency_detail,
        },
        "runtime": runtime,
        "next_action": next_action,
    }


# ---------------------------------------------------------------------------
# state store inspector / doctor (Redmine #12273)
# ---------------------------------------------------------------------------
# Read-only side-by-side detection of the legacy per-kind SQLite files and the
# future home-scoped single DB, designed against
# `vibes/docs/logics/managed-state-model.md` (state-kind / recovery-policy
# vocabulary, single-DB `state_schema_components` layout) and
# `vibes/docs/logics/runtime-observability-boundary.md` (component status is a
# read-only projection, never workflow truth or a side-effect permission).
#
# Invariants this surface MUST keep (j#61668 implementation slice):
#   - read-only only: every probe opens SQLite through a `file:...?mode=ro`
#     URI, which refuses to create the file, and is limited to
#     `PRAGMA user_version`, `PRAGMA integrity_check`, and `sqlite_master`.
#   - no parent-dir creation, no schema initialization, no migration, no
#     repair. It deliberately does NOT reuse the existing store classes
#     (OtelEventStore / ManagedEventLog / inventory / registry writers) whose
#     constructors create the home dir and initialize schema.
#   - the `status` vocabulary stays small (missing / ok / warning / invalid /
#     error). `ok` means only "readable at the expected shape for this state
#     kind" — never complete / approved / action-allowed. An absent legacy file
#     and an absent single DB are NORMAL states, not failures.

# The container layout constants (`STATE_STORE_SINGLE_DB_FILENAME`,
# `STATE_STORE_SINGLE_DB_CONTAINER_VERSION`), the recovery-policy vocabulary, and
# the per-component registry are owned by :mod:`mozyo_bridge.state_store` — the
# single source of truth shared with the #12305 migrator — and imported above.
# The read-only doctor must still fail closed on any container version it does not
# understand: a newer container is reported unsupported and left untouched
# (downgrade-safe), never `ok` (Redmine #12273 j#61689 Finding 1).
#
# Legacy per-kind files, in `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}`. `repair_action`
# is the component-scoped next-action token a damaged store should suggest; it is
# advice, not something this read-only surface performs. Derived from the shared
# state_store registry so the inspector (#12273) and migrator (#12305) cannot drift.
_LEGACY_COMPONENTS: tuple[dict[str, Any], ...] = tuple(
    {
        "component": spec.component,
        "filename": spec.legacy_filename,
        "schema_version": spec.legacy_schema_version,
        "tables": spec.legacy_tables,
        "recovery_policy": spec.recovery_policy,
        "repair_action": spec.repair_action,
    }
    for spec in _STATE_COMPONENTS
)

# Component names the future single DB is expected to absorb from the legacy
# files (managed-state-model.md `### legacy import`). A single DB that carries a
# strict subset is a partial migration, not a complete one.
_SINGLE_DB_EXPECTED_COMPONENTS = frozenset(
    spec["component"] for spec in _LEGACY_COMPONENTS
)


def _probe_sqlite_ro(path: Path) -> dict[str, Any]:
    """Open ``path`` read-only and report user_version / tables / integrity.

    Never creates or writes: the ``file:...?mode=ro`` URI errors if the file is
    absent rather than creating it, and only ``PRAGMA user_version``,
    ``sqlite_master``, and ``PRAGMA integrity_check`` are issued. A non-SQLite
    or truncated file opens lazily but fails the first query; that collapses to
    ``integrity='error'`` so a corrupt store is reported, not raised.
    """
    info: dict[str, Any] = {
        "opened": False,
        "user_version": None,
        "tables": [],
        "integrity": "unknown",
        "error": None,
    }
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        info["error"] = str(exc)
        return info
    try:
        info["opened"] = True
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            info["user_version"] = int(row[0]) if row is not None else None
        except (sqlite3.DatabaseError, TypeError, ValueError):
            info["integrity"] = "error"
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            info["tables"] = [r[0] for r in rows]
        except sqlite3.DatabaseError:
            info["integrity"] = "error"
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            info["integrity"] = "ok" if check is not None and check[0] == "ok" else "error"
        except sqlite3.DatabaseError:
            info["integrity"] = "error"
    finally:
        conn.close()
    return info


def _read_state_schema_components(path: Path) -> list[dict[str, Any]] | None:
    """Read ``state_schema_components`` rows read-only; never raises.

    Returns the parsed rows on success — possibly an empty list, which is a
    legitimate state (a metadata table with no component rows yet is an empty /
    partial migration). Returns ``None`` when the table cannot be read at all
    (malformed schema / unreadable), so the caller can distinguish a malformed
    metadata schema from a genuine partial migration. The review (#12273
    j#61689 Finding 1) flagged that both used to collapse to ``[]`` and were
    reported identically as a migratable partial state.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        rows = conn.execute(
            "SELECT component, schema_version, recovery_policy, migrated_from "
            "FROM state_schema_components ORDER BY component"
        ).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return [
        {
            "component": component,
            "schema_version": schema_version,
            "recovery_policy": recovery_policy,
            "migrated_from": migrated_from,
        }
        for component, schema_version, recovery_policy, migrated_from in rows
    ]


def _inspect_legacy_component(spec: dict[str, Any], home: Path) -> dict[str, Any]:
    path = home / spec["filename"]
    entry: dict[str, Any] = {
        "component": spec["component"],
        "path": str(path),
        "kind": "legacy",
        "recovery_policy": spec["recovery_policy"],
        "exists": False,
        "schema_version": None,
        "status": "missing",
        "readability": "absent",
        "integrity": "unknown",
        "tables": [],
        "next_action": "leave_untouched",
        "notes": [],
    }
    if not path.exists():
        entry["notes"].append(
            "legacy file absent (normal before first use or after migration)"
        )
        return entry

    entry["exists"] = True
    probe = _probe_sqlite_ro(path)
    entry["schema_version"] = probe["user_version"]
    entry["tables"] = probe["tables"]

    if not probe["opened"]:
        entry["status"] = "error"
        entry["readability"] = "unreadable"
        entry["integrity"] = "error"
        entry["next_action"] = spec["repair_action"]
        entry["notes"].append(
            f"unreadable: {probe['error'] or 'cannot open read-only'}"
        )
        return entry

    entry["readability"] = "readable"
    entry["integrity"] = probe["integrity"]

    if probe["integrity"] != "ok":
        entry["status"] = "error"
        entry["readability"] = "partial"
        entry["next_action"] = spec["repair_action"]
        entry["notes"].append("PRAGMA integrity_check did not return ok (corrupt)")
        return entry

    expected_version = spec["schema_version"]
    actual_version = probe["user_version"]
    if actual_version != expected_version:
        newer = isinstance(actual_version, int) and actual_version > expected_version
        if spec["recovery_policy"] == _RECOVERY_AUTHORITATIVE:
            # Authoritative identity is fail-closed on unknown schema: report
            # unsupported and leave the DB untouched (downgrade must not rewrite
            # newer identity state).
            entry["status"] = "invalid"
            entry["next_action"] = "leave_untouched"
            entry["notes"].append(
                f"unsupported schema_version {actual_version} (this build expects "
                f"{expected_version}); authoritative store left untouched"
            )
        else:
            # Caches/history degrade rather than fail: a newer DB is left
            # untouched (downgrade-safe), an older shape is rebuildable.
            entry["status"] = "warning"
            entry["next_action"] = (
                "leave_untouched" if newer else spec["repair_action"]
            )
            entry["notes"].append(
                f"schema_version {actual_version} != expected {expected_version}; "
                + (
                    "newer DB left untouched (downgrade-safe)"
                    if newer
                    else "older cache/history shape; rebuildable"
                )
            )
        return entry

    missing_tables = [t for t in spec["tables"] if t not in probe["tables"]]
    if missing_tables:
        entry["status"] = "invalid"
        entry["readability"] = "partial"
        entry["next_action"] = (
            "leave_untouched"
            if spec["recovery_policy"] == _RECOVERY_AUTHORITATIVE
            else spec["repair_action"]
        )
        entry["notes"].append(
            "expected tables missing: " + ", ".join(missing_tables)
        )
        return entry

    entry["status"] = "ok"
    entry["next_action"] = "leave_untouched"
    entry["notes"].append("readable at expected schema; no migration in this build")
    return entry


def _inspect_single_db(home: Path) -> dict[str, Any]:
    path = home / STATE_STORE_SINGLE_DB_FILENAME
    entry: dict[str, Any] = {
        "component": "single_db",
        "path": str(path),
        "kind": "single_db",
        "recovery_policy": "mixed",
        "exists": False,
        "schema_version": None,
        "status": "missing",
        "readability": "absent",
        "integrity": "unknown",
        "tables": [],
        "components": [],
        "next_action": "leave_untouched",
        "notes": [],
    }
    if not path.exists():
        entry["notes"].append(
            "future single DB absent (no migration has run; legacy files remain "
            "the source of state)"
        )
        return entry

    entry["exists"] = True
    probe = _probe_sqlite_ro(path)
    entry["schema_version"] = probe["user_version"]
    entry["tables"] = probe["tables"]

    if not probe["opened"]:
        entry["status"] = "error"
        entry["readability"] = "unreadable"
        entry["integrity"] = "error"
        entry["next_action"] = "restore_backup"
        entry["notes"].append(
            f"unreadable: {probe['error'] or 'cannot open read-only'}"
        )
        return entry

    entry["readability"] = "readable"
    entry["integrity"] = probe["integrity"]

    if probe["integrity"] != "ok":
        entry["status"] = "error"
        entry["readability"] = "partial"
        entry["next_action"] = "restore_backup"
        entry["notes"].append("PRAGMA integrity_check did not return ok (corrupt)")
        return entry

    # Validate the container layout version BEFORE trusting component metadata.
    # An unsupported container (typically a newer one this build does not
    # understand) must be reported unsupported and left untouched — never `ok`
    # even when component rows look complete (#12273 j#61689 Finding 1).
    container_version = probe["user_version"]
    if container_version != STATE_STORE_SINGLE_DB_CONTAINER_VERSION:
        newer = (
            isinstance(container_version, int)
            and container_version > STATE_STORE_SINGLE_DB_CONTAINER_VERSION
        )
        entry["status"] = "invalid"
        entry["next_action"] = "leave_untouched"
        entry["notes"].append(
            f"unsupported container schema version {container_version} (this build "
            f"understands {STATE_STORE_SINGLE_DB_CONTAINER_VERSION}); "
            + ("newer DB left untouched (downgrade-safe)" if newer else "left untouched")
        )
        return entry

    if "state_schema_components" not in probe["tables"]:
        # A supported container `user_version` without the component metadata
        # table is an unknown / unsupported layout. Leave it untouched.
        entry["status"] = "invalid"
        entry["next_action"] = "leave_untouched"
        entry["notes"].append(
            "container present but state_schema_components metadata table is "
            "missing; unsupported / unknown layout left untouched"
        )
        return entry

    components = _read_state_schema_components(path)
    if components is None:
        # The metadata table exists but its schema is malformed / unreadable.
        # This is NOT a partial migration: report it invalid and leave it
        # untouched rather than suggesting a dry-run migrate (#12273 j#61689
        # Finding 1) — a malformed schema is not a migratable subset.
        entry["status"] = "invalid"
        entry["readability"] = "partial"
        entry["next_action"] = "leave_untouched"
        entry["notes"].append(
            "state_schema_components present but unreadable / malformed schema; "
            "left untouched (not treated as partial migration)"
        )
        return entry

    entry["components"] = components
    present = {c["component"] for c in components}
    missing = sorted(_SINGLE_DB_EXPECTED_COMPONENTS - present)
    if missing:
        entry["status"] = "warning"
        entry["readability"] = "partial"
        entry["next_action"] = "migrate_dry_run"
        entry["notes"].append(
            "partial migration: state_schema_components carries "
            f"{sorted(present) or 'no'} component(s); not yet migrated: {missing}"
        )
    else:
        entry["status"] = "ok"
        entry["next_action"] = "inspect"
        entry["notes"].append(
            "all expected components present in state_schema_components"
        )
    return entry


# Section roll-up rank. `missing` is treated as `ok` (0): an absent legacy file
# or absent single DB is a normal state, so it must not flip doctor red.
_STATE_STORE_STATUS_RANK = {"ok": 0, "missing": 0, "warning": 1, "invalid": 2, "error": 3}
_STATE_STORE_RANK_STATUS = {0: "ok", 1: "warning", 2: "invalid", 3: "error"}


def _state_store_section_status(components: list[dict[str, Any]]) -> str:
    worst = 0
    for component in components:
        worst = max(worst, _STATE_STORE_STATUS_RANK.get(component["status"], 0))
    return _STATE_STORE_RANK_STATUS[worst]


def _state_store_next_actions(components: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for component in components:
        if component["status"] in ("warning", "invalid", "error"):
            detail = component["notes"][0] if component["notes"] else component["status"]
            actions.append(
                f"{component['component']}: {component['next_action']} ({detail})"
            )
    return actions


def collect_state_store(home: Path | None = None) -> dict[str, Any]:
    """Read-only state-store component report (Redmine #12273).

    Detects the legacy per-kind SQLite files and the future single DB
    side-by-side under the resolved home, reporting per-component status and a
    next-action token. Creates nothing and writes nothing. The result is a
    component projection, not workflow truth or a side-effect permission.
    """
    resolved_home = home or mozyo_bridge_home()
    components = [
        _inspect_legacy_component(spec, resolved_home) for spec in _LEGACY_COMPONENTS
    ]
    components.append(_inspect_single_db(resolved_home))
    return {
        "status": _state_store_section_status(components),
        "home": str(resolved_home),
        "components": components,
        "next_action": _state_store_next_actions(components),
    }


def doctor_state_store_section(args: argparse.Namespace) -> dict[str, Any]:
    return collect_state_store(doctor_home(args))


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    # Thin handler: the section orchestration (external reads) and the
    # authority-bearing health verdict now live behind the typed boundary in
    # ``doctor_health`` (#12833). ``LiveDoctorSections`` drives the section
    # collectors at call time (so existing ``patch(...doctor.doctor_*_section)``
    # integration tests are unchanged), ``RunDoctorUseCase`` applies the pure
    # ``evaluate_doctor_health`` policy, and the legacy result shape is
    # preserved byte-for-byte for the CLI / JSON / ``format_doctor_text`` paths.
    return RunDoctorUseCase(LiveDoctorSections(args)).execute()


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
