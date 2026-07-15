"""macOS LaunchAgent lifecycle for the workspace callback supervisor (Redmine #13683 Phase B1).

Phase A shipped the ``workflow supervisor`` command **contract** with the three mutating verbs
(``--install`` / ``--restart`` / ``--uninstall``) fail-closed (a bare "no host mutation" refusal).
This module is the Phase B1 realization of that contract: the bounded LaunchAgent lifecycle that a
host service manager (launchd) would run — and **nothing more**.
It is deliberately *not* a general daemon manager (the OTel receiver's ``otel_launchd`` module is the
safe-pattern reference, j#78995); it manages exactly one owned label / plist / log for this supervisor.

Design boundary (design preflight j#78995 / Implementation Request j#79005):

- **One-shot scheduled cadence, never KeepAlive.** ``workflow supervisor --run-once`` is a *bounded*
  sweep that exits; the plist schedules it with ``RunAtLoad`` (run once at load) + ``StartInterval``
  (re-run every N seconds), and carries **no** ``KeepAlive`` key. Mapping a one-shot command onto
  ``KeepAlive`` would be a tight restart loop, so KeepAlive is structurally absent — not merely false.
- **No secret ever reaches the plist.** The rendered plist has **no** ``EnvironmentVariables`` key at
  all, so no code path can serialize a credential into it. A launchd-started supervisor inherits no
  shell environment; the Redmine key/URL reach it through the daemon-trusted home-scoped credential
  file (``resolve_redmine_credentials``), never the plist. ``ProgramArguments`` is the exact
  PATH-resolved ``mozyo-bridge`` executable + structured argv — never a shell string.
- **Structured launchctl only.** Every ``launchctl`` invocation is structured argv
  (``bootstrap`` / ``bootout`` / ``kickstart -k`` / ``print``) — no shell. Install is idempotent
  (bootout-then-bootstrap), restart acts only on a *loaded* service, uninstall removes exactly the
  owned label / plist and touches nothing else.
- **Fail-closed, zero-mutation refusals.** ``install`` / ``restart`` refuse — *before* writing any
  file or invoking launchctl — on a non-darwin host, a missing executable, or a Redmine credential
  that is missing / incomplete / unsafe / malformed. ``uninstall`` and status stay usable with no
  credential at all (you must be able to tear an agent down without configured credentials).
- **Redacted status projection.** Status reports plist existence / loaded / pid / scheduled interval /
  executable-match / credential-readiness as booleans, counts, and fixed-vocabulary tokens only — no
  credential value, no request header, no repo-local path, no pane text.

This module performs **no** Redmine fetch, gate progression, route resolution, or callback delivery:
installing / restarting / uninstalling the agent is orthogonal to what the agent does when it runs.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    DEFAULT_SUPERVISOR_SERVICE_LABEL,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    normalize_base_url,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)

# ---------------------------------------------------------------------------
# Owned identity (a reverse-DNS label + owned plist/log paths; not operator-private).
# ---------------------------------------------------------------------------

SUPERVISOR_LAUNCHD_LABEL = DEFAULT_SUPERVISOR_SERVICE_LABEL
PLIST_RELATIVE = Path("Library/LaunchAgents") / f"{SUPERVISOR_LAUNCHD_LABEL}.plist"
LOG_RELATIVE = Path("Library/Logs/mozyo-bridge/callback-supervisor.log")

#: The executable name resolved from PATH at install time (never a shell string).
SUPERVISOR_EXECUTABLE_NAME = "mozyo-bridge"
#: The structured argv tail the scheduled agent runs each tick (one bounded sweep, then exit).
SUPERVISOR_ARGV_TAIL = ("workflow", "supervisor", "--run-once")

# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens (machine-readable; secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

#: A mutating verb (install/restart/uninstall) was refused because the host is not macOS.
REASON_UNSUPPORTED_PLATFORM = "launchd_unsupported_platform"
#: install/restart refused: the `mozyo-bridge` executable is not resolvable on PATH.
REASON_EXECUTABLE_NOT_FOUND = "supervisor_executable_not_found"
#: restart refused: the service is not currently loaded (restart acts only on a loaded service).
REASON_SERVICE_NOT_LOADED = "service_not_loaded"
#: A launchctl bootstrap failed (message redacted to a fixed token; no host detail leaks).
REASON_BOOTSTRAP_FAILED = "launchctl_bootstrap_failed"
#: A launchctl kickstart failed (message redacted to a fixed token).
REASON_KICKSTART_FAILED = "launchctl_kickstart_failed"

#: Credential-readiness tokens (the exact readiness the live supervisor needs to reach Redmine).
CREDENTIAL_READY = "ready"  # api key + usable base url present
CREDENTIAL_INCOMPLETE = "incomplete"  # exactly one of key / usable url present
CREDENTIAL_MISSING = "missing"  # neither present, and nothing unsafe (the plain unconfigured case)
CREDENTIAL_UNSAFE = "unsafe"  # a present credential file is unsafe/malformed (permission / YAML)

#: The install/restart refusal reason for each non-ready credential state.
_CREDENTIAL_REFUSAL_REASON = {
    CREDENTIAL_INCOMPLETE: "redmine_credential_incomplete",
    CREDENTIAL_MISSING: "redmine_credential_missing",
    CREDENTIAL_UNSAFE: "redmine_credential_unsafe",
}

# A launchctl "print" for an unknown label exits non-zero; treat any non-zero as "not loaded".
_LAUNCHCTL = "launchctl"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


# ---------------------------------------------------------------------------
# Path + command + plist rendering (pure; no host mutation, no secrets).
# ---------------------------------------------------------------------------


def plist_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / PLIST_RELATIVE


def log_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / LOG_RELATIVE


def resolve_supervisor_command(
    *, which: Callable[[str], Optional[str]] = shutil.which
) -> Optional[list[str]]:
    """The exact argv the agent runs, or ``None`` when the executable is not on PATH.

    Resolved at install time via PATH so the plist survives shell-env differences. A missing
    executable is a fail-closed condition the caller turns into a zero-mutation refusal (install
    the package first) — never a shell string and never a guessed path.
    """
    executable = which(SUPERVISOR_EXECUTABLE_NAME)
    if not executable:
        return None
    return [executable, *SUPERVISOR_ARGV_TAIL]


def render_plist(
    command: Sequence[str],
    *,
    interval_seconds: int,
    home: Optional[Path] = None,
) -> bytes:
    """Render the LaunchAgent plist for the one-shot scheduled supervisor sweep.

    Structurally minimal and secret-free:

    - **No** ``EnvironmentVariables`` key exists in the output, so no secret can be serialized in.
    - **No** ``KeepAlive`` key: the command is a bounded ``--run-once`` sweep that exits;
      ``RunAtLoad`` runs it once at load and ``StartInterval`` re-runs it every ``interval_seconds``.
      KeepAlive would be a tight restart loop for a one-shot command, so it is absent by design.
    - ``ProgramArguments`` is the exact structured argv (PATH-resolved executable + fixed tail).
    """
    payload = {
        "Label": SUPERVISOR_LAUNCHD_LABEL,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "StartInterval": max(1, int(interval_seconds)),
        "StandardOutPath": str(log_path(home)),
        "StandardErrorPath": str(log_path(home)),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


# ---------------------------------------------------------------------------
# Credential readiness (the exact readiness the live supervisor needs; secret-safe token only).
# ---------------------------------------------------------------------------


def classify_credential_readiness(*, home: Optional[Path] = None) -> str:
    """Classify **daemon-effective** Redmine credential readiness into a fixed, secret-safe token.

    Judges what the *launchd-managed* supervisor will actually have at run time, not what the
    installer's interactive shell happens to hold. By this module's own plist design the agent
    carries **no** ``EnvironmentVariables`` and launchd inherits no shell environment, so the
    daemon's only credential source is the home-scoped file (``resolve_redmine_credentials``'s file
    path). Readiness therefore resolves with an **empty environ**: an installer's exported
    ``MOZYO_REDMINE_*`` can never produce a false ``ready`` for a daemon that will not see it
    (Redmine #13683 review j#79059 F1). Ready needs an api key **and** a normalizable base URL from
    the home file; a present-but-unsafe / malformed file surfaces as :data:`CREDENTIAL_UNSAFE` (the
    resolver refuses to read it and returns a redacted warning), so a fail-closed refusal is visibly
    deliberate. Returns only a token — never the key, the URL, or the warning text.
    """
    creds = resolve_redmine_credentials(home, environ={})
    if creds.warnings:
        return CREDENTIAL_UNSAFE
    has_key = bool(creds.api_key)
    has_url = bool(normalize_base_url(creds.base_url))
    if has_key and has_url:
        return CREDENTIAL_READY
    if has_key or has_url:
        return CREDENTIAL_INCOMPLETE
    return CREDENTIAL_MISSING


# ---------------------------------------------------------------------------
# launchctl seam (structured argv only; no shell).
# ---------------------------------------------------------------------------


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _running_on_darwin() -> bool:
    return sys.platform == "darwin"


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    return f"{_gui_domain()}/{SUPERVISOR_LAUNCHD_LABEL}"


def _launchctl(runner: Runner, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return runner([_LAUNCHCTL, *args])


def _is_loaded(runner: Runner) -> tuple[bool, Optional[int]]:
    """Read-only ``launchctl print`` → (loaded, pid). Never raises for a missing launchctl."""
    try:
        result = _launchctl(runner, ["print", _service_target()])
    except FileNotFoundError:  # launchctl absent (non-darwin / minimal host)
        return False, None
    if result.returncode != 0:
        return False, None
    pid: Optional[int] = None
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            token = stripped.split("=", 1)[1].strip()
            if token.isdigit():
                pid = int(token)
            break
    return True, pid


# ---------------------------------------------------------------------------
# Lifecycle verbs (structured results; fail-closed, zero-mutation refusals).
# ---------------------------------------------------------------------------


def install(
    *,
    home: Optional[Path] = None,
    interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Write the owned plist and (re)bootstrap the agent. Idempotent; fail-closed zero-mutation.

    Refuses — before any filesystem write or launchctl call — on a non-darwin host, a missing
    executable, or a non-ready **daemon-effective** Redmine credential (the home-scoped file the
    launchd agent will actually see; an installer's shell env does not count). On success the plist
    is rewritten idempotently and the agent is booted out (ignore-failure) then bootstrapped.
    """
    if not _running_on_darwin():
        return _refused("install", REASON_UNSUPPORTED_PLATFORM)
    command = resolve_supervisor_command(which=which)
    if command is None:
        return _refused("install", REASON_EXECUTABLE_NOT_FOUND)
    readiness = classify_credential_readiness(home=home)
    if readiness != CREDENTIAL_READY:
        return _refused(
            "install", _CREDENTIAL_REFUSAL_REASON[readiness], credential_readiness=readiness
        )

    target = plist_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    log_path(home).parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_plist(command, interval_seconds=interval_seconds, home=home))
    # A previously loaded agent must be booted out before bootstrap or launchd rejects the
    # duplicate label; a not-loaded bootout is fine to ignore (idempotent install).
    _launchctl(runner, ["bootout", _service_target()])
    result = _launchctl(runner, ["bootstrap", _gui_domain(), str(target)])
    if result.returncode != 0:
        return {
            "action": "install",
            "performed": False,
            "reason": REASON_BOOTSTRAP_FAILED,
            "credential_readiness": readiness,
        }
    return {
        "action": "install",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
        "scheduled_interval_seconds": max(1, int(interval_seconds)),
    }


def restart(
    *,
    home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Kickstart (kill + relaunch) the *loaded* agent. Fail-closed zero-mutation.

    Refuses — before any launchctl mutation — on a non-darwin host, a missing executable, a
    non-ready **daemon-effective** credential (home-scoped file, not shell env), or a service that
    is not currently loaded (restart never bootstraps a fresh service; that is ``install``).
    """
    if not _running_on_darwin():
        return _refused("restart", REASON_UNSUPPORTED_PLATFORM)
    if resolve_supervisor_command(which=which) is None:
        return _refused("restart", REASON_EXECUTABLE_NOT_FOUND)
    readiness = classify_credential_readiness(home=home)
    if readiness != CREDENTIAL_READY:
        return _refused(
            "restart", _CREDENTIAL_REFUSAL_REASON[readiness], credential_readiness=readiness
        )
    loaded, _pid = _is_loaded(runner)
    if not loaded:
        return _refused("restart", REASON_SERVICE_NOT_LOADED, credential_readiness=readiness)
    result = _launchctl(runner, ["kickstart", "-k", _service_target()])
    if result.returncode != 0:
        return {
            "action": "restart",
            "performed": False,
            "reason": REASON_KICKSTART_FAILED,
            "credential_readiness": readiness,
        }
    return {
        "action": "restart",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
    }


def uninstall(
    *,
    home: Optional[Path] = None,
    runner: Runner = _default_runner,
) -> dict:
    """Boot the agent out and remove exactly the owned plist. No credential required.

    Refuses only on a non-darwin host (there is no launchd to bootout). On darwin, tears down the
    agent even when credentials are absent — you must be able to remove a service without them.
    """
    if not _running_on_darwin():
        return _refused("uninstall", REASON_UNSUPPORTED_PLATFORM)
    _launchctl(runner, ["bootout", _service_target()])
    target = plist_path(home)
    existed = target.exists()
    if existed:
        target.unlink()
    return {
        "action": "uninstall",
        "performed": True,
        "reason": "",
        "removed": existed,
    }


def service_status(
    *,
    home: Optional[Path] = None,
    interval_hint: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """A read-only, redacted projection of the host service state. Mutates nothing.

    Reports plist existence, loaded/pid, the *scheduled* interval (read from the installed plist),
    whether the installed executable still matches the PATH-resolved one, and **daemon-effective**
    credential readiness (the home-scoped file the launchd agent will see, not the caller's shell
    env) — as booleans / counts / fixed tokens only. Never emits a credential value, a request
    header, a repo-local path, or pane text. Works on any platform and with no credential.
    """
    target = plist_path(home)
    plist_exists = target.exists()
    loaded, pid = _is_loaded(runner)

    installed = _read_installed_plist(target) if plist_exists else None
    scheduled_interval = installed.get("StartInterval") if installed else None
    run_at_load = bool(installed.get("RunAtLoad")) if installed else None
    keep_alive_present = ("KeepAlive" in installed) if installed else False
    no_environment_block = ("EnvironmentVariables" not in installed) if installed else True

    resolved = resolve_supervisor_command(which=which)
    installed_argv = installed.get("ProgramArguments") if installed else None
    executable_matches = bool(
        resolved is not None
        and isinstance(installed_argv, list)
        and installed_argv == resolved
    )

    return {
        "action": "service-status",
        "label": SUPERVISOR_LAUNCHD_LABEL,
        "platform_supported": _running_on_darwin(),
        "installed": plist_exists,
        "plist_exists": plist_exists,
        "loaded": loaded,
        "pid": pid,
        "scheduled_interval_seconds": (
            int(scheduled_interval)
            if isinstance(scheduled_interval, int)
            else (int(interval_hint) if not plist_exists else None)
        ),
        "run_at_load": run_at_load,
        "keep_alive_present": keep_alive_present,
        "no_environment_block": no_environment_block,
        "executable_matches": executable_matches,
        "credential_readiness": classify_credential_readiness(home=home),
    }


def _read_installed_plist(target: Path) -> Optional[dict]:
    """Best-effort parse of the installed plist; ``None`` if unreadable/malformed (never raises)."""
    try:
        raw = target.read_bytes()
        parsed = plistlib.loads(raw)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return parsed if isinstance(parsed, dict) else None


def _refused(action: str, reason: str, **extra: object) -> dict:
    """A fail-closed, zero-mutation refusal result (fixed vocabulary; no host detail)."""
    return {"action": action, "performed": False, "reason": reason, **extra}


__all__ = (
    "SUPERVISOR_LAUNCHD_LABEL",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "REASON_UNSUPPORTED_PLATFORM",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_SERVICE_NOT_LOADED",
    "REASON_BOOTSTRAP_FAILED",
    "REASON_KICKSTART_FAILED",
    "CREDENTIAL_READY",
    "CREDENTIAL_INCOMPLETE",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_UNSAFE",
    "plist_path",
    "log_path",
    "resolve_supervisor_command",
    "render_plist",
    "classify_credential_readiness",
    "install",
    "restart",
    "uninstall",
    "service_status",
)
