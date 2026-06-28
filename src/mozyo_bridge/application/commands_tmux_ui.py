"""tmux-config / tmux-ui command family — OOP-first boundary (Redmine #12749 / #12638).

Split out of the ~7000-line ``application/commands.py`` as the representative
first conversion tranche for the OOP-first command decomposition
(``vibes/docs/logics/object-oriented-architecture-policy.md``; US #12638 /
Task #12785). It is **not** a free-function relocation: it introduces the
policy's named object boundaries for this family while preserving behavior and
the public CLI / import / monkeypatch surface.

Boundaries introduced here:

- **Typed command input value objects** (``TmuxConfigRequest`` /
  ``TmuxUiInstallRequest`` / ``TmuxUiUninstallRequest`` / ``TmuxUiStatusRequest``):
  the ``argparse.Namespace`` is read once at the CLI edge and never propagated
  into the use-case / domain layer (policy static-typing step 4).
- **Use case with port injection** (``ApplyTmuxConfigUseCase``): the
  config-load path coordinates the availability guard and ``tmux source-file``
  through the injected :class:`~mozyo_bridge.application.tmux_control_port.TmuxControlPort`
  instead of the naked ``require_tmux`` / ``source_tmux_conf`` calls the old
  procedural handler made. Its unit test drives a fake port, replacing the
  function-monkeypatch test seam (#12785 Done criterion).
- **Thin command handlers** (``cmd_config`` / ``cmd_tmux_ui_*``): read the CLI
  namespace into a request, invoke the use case (config) or the existing
  ``tmux_ui`` domain (install / uninstall / status — already a clean domain
  surface returning ``InstallResult`` / ``UninstallResult`` value objects), then
  render and map to an exit code. They hold no external boundary directly.

Compatibility: ``commands.py`` re-exports the ``cmd_*`` entry points from this
module so ``mozyo_bridge.application.commands.cmd_config`` (and the cli_cockpit
parser registrar that imports them) keep their identity and ``func.__name__``
bindings. The thin module-level ``cmd_*`` functions are preserved (not turned
into methods) precisely so the parser ``func`` binding and any downstream import
stay byte-compatible.
"""

from __future__ import annotations

import argparse
import json as _json
from dataclasses import dataclass
from pathlib import Path

from mozyo_bridge.application import tmux_ui as tmux_ui_module
from mozyo_bridge.application.commands_common import config_path_from_args
from mozyo_bridge.application.tmux_control_port import (
    LiveTmuxControlPort,
    TmuxControlPort,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import resolve_repo_root


# ---------------------------------------------------------------------------
# Command input value objects (CLI edge → typed request; no Namespace beyond here)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TmuxConfigRequest:
    """Resolved input for ``mozyo-bridge tmux-ui-config``."""

    config_path: str


@dataclass(frozen=True)
class TmuxConfigResult:
    """Outcome of the config-load use case."""

    loaded_path: str


@dataclass(frozen=True)
class TmuxUiInstallRequest:
    repo_root: Path
    tmux_conf: Path
    force: bool
    dry_run: bool
    backup: bool


@dataclass(frozen=True)
class TmuxUiUninstallRequest:
    tmux_conf: Path
    dry_run: bool
    backup: bool


@dataclass(frozen=True)
class TmuxUiStatusRequest:
    repo_root: Path
    tmux_conf: Path
    as_json: bool


# ---------------------------------------------------------------------------
# Use case (port-injected) for the config-load path
# ---------------------------------------------------------------------------
class ApplyTmuxConfigUseCase:
    """Source a tmux config into the running server through a tmux control port.

    The external boundary (availability guard + ``tmux source-file``) is held by
    the injected :class:`TmuxControlPort`, so the use case carries no naked
    subprocess call and is unit-testable with a fake port.
    """

    def __init__(self, tmux: TmuxControlPort) -> None:
        self._tmux = tmux

    def execute(self, request: TmuxConfigRequest) -> TmuxConfigResult:
        self._tmux.require_available()
        self._tmux.source_conf(request.config_path)
        return TmuxConfigResult(loaded_path=str(Path(request.config_path).expanduser()))


# ---------------------------------------------------------------------------
# CLI-edge request builders (read the argparse Namespace exactly once)
# ---------------------------------------------------------------------------
def _config_request(args: argparse.Namespace) -> TmuxConfigRequest:
    return TmuxConfigRequest(config_path=getattr(args, "path", None) or config_path_from_args(args))


def _host_conf(args: argparse.Namespace) -> Path:
    return tmux_ui_module.resolve_host_tmux_conf(getattr(args, "tmux_conf", None))


def _install_request(args: argparse.Namespace) -> TmuxUiInstallRequest:
    return TmuxUiInstallRequest(
        repo_root=resolve_repo_root(getattr(args, "repo", None)),
        tmux_conf=_host_conf(args),
        force=bool(getattr(args, "force", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        backup=bool(getattr(args, "backup", False)),
    )


def _uninstall_request(args: argparse.Namespace) -> TmuxUiUninstallRequest:
    return TmuxUiUninstallRequest(
        tmux_conf=_host_conf(args),
        dry_run=bool(getattr(args, "dry_run", False)),
        backup=bool(getattr(args, "backup", False)),
    )


def _status_request(args: argparse.Namespace) -> TmuxUiStatusRequest:
    return TmuxUiStatusRequest(
        repo_root=resolve_repo_root(getattr(args, "repo", None)),
        tmux_conf=_host_conf(args),
        as_json=bool(getattr(args, "as_json", False)),
    )


# ---------------------------------------------------------------------------
# Thin command handlers (argparse → request → use case / domain → render → exit)
# ---------------------------------------------------------------------------
def cmd_config(args: argparse.Namespace) -> int:
    result = ApplyTmuxConfigUseCase(LiveTmuxControlPort()).execute(_config_request(args))
    print(f"loaded tmux config: {result.loaded_path}")
    return 0


def cmd_tmux_ui_install(args: argparse.Namespace) -> int:
    request = _install_request(args)
    try:
        result = tmux_ui_module.apply_install(
            repo_root=request.repo_root,
            tmux_conf=request.tmux_conf,
            force=request.force,
            dry_run=request.dry_run,
            backup=request.backup,
        )
    except tmux_ui_module.TmuxUiError as exc:
        die(str(exc))
        return 2  # unreachable; die() raises SystemExit
    suffix = " (dry-run)" if result.dry_run else ""
    if result.action == "noop":
        print(
            f"tmux-ui install: already wired to {result.expected_snippet} "
            f"in {result.tmux_conf}; no change"
        )
    else:
        print(
            f"tmux-ui install: {result.action} managed block in "
            f"{result.tmux_conf} → {result.expected_snippet}{suffix}"
        )
        if result.previous_source_path and result.action == "replaced":
            print(f"  previous source path: {result.previous_source_path}")
        if result.backup_path:
            print(f"  backup written: {result.backup_path}")
    return 0


def cmd_tmux_ui_uninstall(args: argparse.Namespace) -> int:
    request = _uninstall_request(args)
    try:
        result = tmux_ui_module.apply_uninstall(
            tmux_conf=request.tmux_conf,
            dry_run=request.dry_run,
            backup=request.backup,
        )
    except tmux_ui_module.TmuxUiError as exc:
        die(str(exc))
        return 2  # unreachable
    suffix = " (dry-run)" if result.dry_run else ""
    if result.action == "noop":
        print(
            f"tmux-ui uninstall: no managed block found in {result.tmux_conf}; "
            "nothing to do"
        )
    else:
        print(f"tmux-ui uninstall: removed managed block from {result.tmux_conf}{suffix}")
        if result.backup_path:
            print(f"  backup written: {result.backup_path}")
    return 0


def cmd_tmux_ui_status(args: argparse.Namespace) -> int:
    request = _status_request(args)
    info = tmux_ui_module.compute_status(request.repo_root, request.tmux_conf)
    if request.as_json:
        print(_json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if info["state"] != tmux_ui_module.STATE_DRIFT else 1
    print(f"tmux-ui status: {info['state']}")
    print(f"  tmux_conf: {info['tmux_conf']} (exists={info['tmux_conf_exists']})")
    print(
        f"  expected_snippet: {info['expected_snippet']} (exists={info['snippet_exists']})"
    )
    if info.get("current_source_path"):
        print(f"  current_source_path: {info['current_source_path']}")
    if info.get("drift_reason"):
        print(f"  drift_reason: {info['drift_reason']}")
    return 0 if info["state"] != tmux_ui_module.STATE_DRIFT else 1
