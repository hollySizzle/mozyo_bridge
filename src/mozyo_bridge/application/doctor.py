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
import subprocess
from pathlib import Path
from typing import Any

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
from mozyo_bridge.application.doctor_tmux_ui import (
    LiveTmuxUiArtifactReads,
    TmuxUiArtifactSectionUseCase,
)
from mozyo_bridge.application.doctor_health import (
    LiveDoctorSections,
    RunDoctorUseCase,
    UNHEALTHY_SECTION_STATUSES,
)
from mozyo_bridge.application.doctor_tmux import (
    LiveTmuxPaneHealthReads,
    TmuxSectionUseCase,
)
from mozyo_bridge.application.doctor_otel import (
    LiveOtelDoctorReads,
    OtelSectionUseCase,
)
# ``pane_lines`` and ``run_tmux`` are resolved through this module at call time
# by the section adapters (the existing ``doctor.pane_lines`` / ``doctor.run_tmux``
# / ``doctor.subprocess`` section integration tests patch these names):
# ``LiveTmuxPaneHealthReads`` (#12881) uses ``run_tmux`` + ``pane_lines`` and
# ``LiveWorkspaceRegistryReads`` (#12924) uses ``run_tmux`` for the tmux liveness
# probe behind the workspace-registry section.
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import pane_lines
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import run_tmux
from mozyo_bridge.scaffold.rules import PRESETS, rules_status, scaffold_state, scaffold_status
from mozyo_bridge.application.doctor_state_store import (
    LiveStateStoreReads,
    StateStoreSectionUseCase,
)
from mozyo_bridge.application.doctor_workspace_registry import (
    LiveWorkspaceRegistryReads,
    WorkspaceRegistrySectionUseCase,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

# The state-store collector now lives behind the ``StateStoreReads`` boundary in
# :mod:`mozyo_bridge.application.doctor_state_store` (#12893). These two container
# constants stay re-exported here only because the state-store inspector
# characterization test imports them from this module — the read-only SQLite
# probing, the legacy component registry, and the recovery vocabulary moved to
# the boundary module.
from mozyo_bridge.state_store import (
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

    The collector's three responsibilities — the external read of the scaffold
    manifest + the on-disk snippet probe + the host tmux config wiring state, the
    verdict authority (``skipped`` / ``ok`` / ``incomplete`` + the artifact and
    host-wiring next_action guidance), and the legacy section dict assembly — now
    live behind the typed boundary in ``doctor_tmux_ui`` (#12866).
    ``LiveTmuxUiArtifactReads`` drives the read through the ``doctor`` /
    ``tmux_ui`` modules at call time (the ``_scaffold_manifest_files`` manifest
    reader and the ``TMUX_UI_*`` layout constants),
    ``TmuxUiArtifactSectionUseCase`` applies the pure
    ``evaluate_tmux_ui_artifact_section`` policy, and the legacy section dict is
    preserved byte-for-byte.
    """
    return TmuxUiArtifactSectionUseCase(LiveTmuxUiArtifactReads(target)).execute()


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


def doctor_tmux_section(args: argparse.Namespace) -> dict[str, Any]:
    """Diagnose tmux pane health for the doctor report (#12881 boundary).

    Thin handler over :class:`~mozyo_bridge.application.doctor_tmux.TmuxSectionUseCase`:
    the external read (tmux availability, pane snapshot, env scope, checkout
    probe) lives in :class:`LiveTmuxPaneHealthReads` and the verdict / legacy
    section dict assembly in the pure ``evaluate_tmux_section`` policy.
    """
    return TmuxSectionUseCase(LiveTmuxPaneHealthReads(args)).execute()


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

    Thin handler over
    :class:`~mozyo_bridge.application.doctor_otel.OtelSectionUseCase`: the
    external read (store counts, receiver ``/healthz`` probe, activity summary,
    tmux agent discovery) lives in :class:`LiveOtelDoctorReads` and the
    receiver-unreachable note / observation-gap detection / ``unobserved_agents``
    / legacy section dict assembly in the pure ``evaluate_otel_section`` policy.
    """
    return OtelSectionUseCase(LiveOtelDoctorReads(args)).execute()


def _live_session_names() -> set[str] | None:
    """Best-effort set of live tmux session names; ``None`` when unavailable.

    Liveness is a tmux question, never a registry one — the workspace-registry
    invariant is that the registry stores identity, not runtime state. Any
    failure (tmux not installed, no server, non-zero exit) collapses to
    ``None`` so the registry section degrades to ``unknown`` rather than
    guessing a workspace is live or dead.

    Stays in this module as the tmux read concern: ``LiveWorkspaceRegistryReads``
    (#12924) resolves it through the ``doctor`` module at call time so the
    existing ``doctor._live_session_names``-patching characterization tests (which
    keep the registry section suite hermetic / off a real tmux server) stay valid.
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

    Thin handler over
    :class:`~mozyo_bridge.application.doctor_workspace_registry.WorkspaceRegistrySectionUseCase`:
    the external reads (home registry health probe, registry row load, on-disk
    anchor read, anchor name-compat resolution, canonical-session resolution,
    and the tmux liveness probe) live in :class:`LiveWorkspaceRegistryReads` and
    the four-layer verdict / legacy section dict assembly in the pure
    ``evaluate_workspace_registry_section`` policy. Strictly read-only: it never
    creates the registry, never writes ``last_seen``, and never touches the
    anchor.
    """
    return WorkspaceRegistrySectionUseCase(LiveWorkspaceRegistryReads()).execute(
        doctor_target(args), doctor_home(args)
    )


# ---------------------------------------------------------------------------
# state store inspector / doctor (Redmine #12273 / #12893)
# ---------------------------------------------------------------------------
# The read-only state-store inspector (legacy per-kind SQLite + future single DB
# side-by-side detection) now lives behind the ``StateStoreReads`` boundary in
# :mod:`mozyo_bridge.application.doctor_state_store`. ``collect_state_store``
# stays here as a thin composition root so the existing public facade is
# preserved byte-for-byte: ``commands_state`` imports ``collect_state_store``
# from this module and the inspector characterization test imports it (and the
# ``STATE_STORE_SINGLE_DB_*`` constants re-exported above) from here.


def collect_state_store(home: Path | None = None) -> dict[str, Any]:
    """Read-only state-store component report (Redmine #12273).

    Thin composition root over the OOP-first boundary (#12893): the read-only
    SQLite probing lives behind :class:`LiveStateStoreReads` and the verdict
    policy in :func:`evaluate_state_store_section`. Detects the legacy per-kind
    SQLite files and the future single DB side-by-side under the resolved home,
    reporting per-component status and a next-action token. Creates nothing and
    writes nothing. The result is a component projection, not workflow truth or
    a side-effect permission.
    """
    resolved_home = home or mozyo_bridge_home()
    return StateStoreSectionUseCase(LiveStateStoreReads()).execute(resolved_home)


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
