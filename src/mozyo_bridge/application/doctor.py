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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import mozyo_bridge
from mozyo_bridge import __version__
from mozyo_bridge.application import tmux_ui as tmux_ui_module
from mozyo_bridge.domain.pane_resolver import AGENT_LABELS, is_agent_process, pane_lines
from mozyo_bridge.infrastructure.tmux_client import run_tmux
from mozyo_bridge.scaffold.rules import PRESETS, rules_status, scaffold_state, scaffold_status


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

    Otherwise returns a record describing the drift. ``relation`` is
    ``version-differs`` (strong signal: the installed and source versions
    disagree), ``unknown`` (source present but its version could not be read),
    or ``same-version`` (paths differ but versions match — surfaced for
    visibility but not a warning, since the installed CLI's public surface
    should match at an equal version).
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
    package_path = Path(mozyo_bridge.__file__).resolve().parent
    executable = shutil.which("mozyo-bridge")
    section: dict[str, Any] = {
        "status": "ok",
        "version": __version__,
        "executable": executable or "",
        "package_path": str(package_path),
        "python": sys.executable,
        "subcommands": list(EXPECTED_SUBCOMMANDS),
        "next_action": [],
    }
    if target is not None:
        drift = repo_local_source_drift(target, package_path, __version__)
        if drift is not None:
            section["source_drift"] = drift
            if drift["relation"] in ("version-differs", "unknown"):
                section["status"] = "warning"
                section["next_action"].append(
                    "running mozyo-bridge is the installed CLI "
                    f"(version {__version__}) but this checkout has repo-local "
                    f"source (src/mozyo_bridge {drift['source_version'] or 'version unknown'}); "
                    "during active development run the repo-local CLI instead: "
                    f"{drift['repo_local_invocation']} <args>"
                )
    return section


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
            f"mozyo-bridge scaffold apply <{'|'.join(PRESETS)}> --target "
            + str(target)
            + home_suffix
        )
    elif manifest == "invalid":
        section_status = "invalid"
        next_action.append(
            "regenerate manifest with `mozyo-bridge scaffold apply <preset> --target "
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
                "mozyo-bridge scaffold apply "
                + str(preset_label)
                + " --target "
                + str(target)
                + home_suffix
                + " --backup"
            )
        if any(row.get("status") != "ok" for row in detail.get("files", [])):
            next_action.append(
                "review router files; rerun `mozyo-bridge scaffold apply "
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
    """
    target = doctor_target(args)
    nagger_dir = target / CLAUDE_NAGGER_DIRNAME

    example_paths = {name: nagger_dir / name for name in CLAUDE_NAGGER_EXAMPLES}
    examples = {
        name: {
            "path": str(path),
            "present": path.exists(),
        }
        for name, path in example_paths.items()
    }
    config_path = nagger_dir / "config.yaml"
    next_action: list[str] = []

    tracked = _scaffold_manifest_files(target)
    nagger_tracked = any(p.startswith(CLAUDE_NAGGER_MANIFEST_PREFIX) for p in tracked)

    if not nagger_tracked:
        # Either no scaffold manifest at all, or the manifest exists
        # but the operator opted out of the nagger category. Both
        # collapse to `skipped`; the suggested remedy points operators
        # at a rerun without --skip-nagger when they want it back.
        status = "skipped"
        next_action.append(
            "Claude Nagger is opt-out (manifest does not track .claude-nagger/); "
            f"rerun `mozyo-bridge scaffold apply <preset> --target {target}` "
            "without --skip-nagger to install the skeleton"
        )
    elif not all(info["present"] for info in examples.values()):
        # Tracked by manifest but examples missing on disk → real drift.
        status = "incomplete"
        for name, info in examples.items():
            if not info["present"]:
                next_action.append(
                    f"missing {info['path']}; rerun scaffold apply --backup to restore"
                )
    elif config_path.exists():
        status = "ok"
    else:
        # Skeleton present, but the operator has not copied it into
        # `config.yaml` yet. The nagger does nothing until they do.
        status = "skeleton-only"
        next_action.append(
            f"copy {example_paths['config.yaml.example']} to {config_path} "
            "to activate Claude Nagger"
        )

    return {
        "status": status,
        "target": str(target),
        "nagger_dir": str(nagger_dir),
        "manifest_tracks_nagger": nagger_tracked,
        "examples": examples,
        "config_yaml": {
            "path": str(config_path),
            "present": config_path.exists(),
        },
        "next_action": next_action,
    }


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

    from mozyo_bridge.domain.agent_activity import summarize_activity
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
        from mozyo_bridge.domain.agent_discovery import (
            discover_agents,
            fold_agents_by_pane,
        )
        from mozyo_bridge.infrastructure.tmux_client import try_pane_lines

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


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    tmux_section = doctor_tmux_section(args)
    # Attach the governed-preset tmux-ui artifact state to the existing
    # tmux section so the diagnostic surface stays small. The artifact
    # status is independent of tmux server availability — operators
    # without a running tmux server still need to know whether the
    # `.mozyo-bridge/tmux/agent-ui.conf` snippet landed on the target.
    tmux_section["artifact"] = doctor_tmux_ui_artifact_info(doctor_target(args))
    sections: dict[str, dict[str, Any]] = {
        "cli": doctor_cli_section(doctor_target(args)),
        "rules": doctor_rules_section(doctor_home(args)),
        "codex_skill": doctor_codex_skill_section(),
        "claude_skill": doctor_claude_skill_section(args),
        "scaffold": doctor_scaffold_section(args),
        "claude_nagger": doctor_claude_nagger_section(args),
        "tmux": tmux_section,
        "otel": doctor_otel_section(args),
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
