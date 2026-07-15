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
from mozyo_bridge.shared.paths import mozyo_bridge_home

# ---------------------------------------------------------------------------
# Owned identity (a reverse-DNS label + owned plist/log paths; not operator-private).
#
# Two DISTINCT roots must never be conflated (review j#79092 R2-F1):
#   - the **OS user home** (``Path.home()``) owns the plist + log under ``~/Library`` — this is
#     where launchd looks for LaunchAgents, independent of any mozyo config;
#   - the **mozyo home** (``mozyo_bridge_home()``: ``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``)
#     owns the registry / store / credential root the supervisor reads at run time.
# ---------------------------------------------------------------------------

SUPERVISOR_LAUNCHD_LABEL = DEFAULT_SUPERVISOR_SERVICE_LABEL
PLIST_RELATIVE = Path("Library/LaunchAgents") / f"{SUPERVISOR_LAUNCHD_LABEL}.plist"
LOG_RELATIVE = Path("Library/Logs/mozyo-bridge/callback-supervisor.log")

#: The executable name resolved from PATH at install time (never a shell string).
SUPERVISOR_EXECUTABLE_NAME = "mozyo-bridge"
#: The structured argv tail the scheduled agent runs each tick (one bounded sweep, then exit). The
#: resolved mozyo home is pinned onto this as ``--home <root>`` at install time (see
#: :func:`resolve_supervisor_command`) so the launchd daemon reads the *same* credential / registry
#: root the install preflight validated — launchd carries no ``MOZYO_BRIDGE_HOME`` (j#79092 R2-F1).
SUPERVISOR_ARGV_TAIL = ("workflow", "supervisor", "--run-once")
#: The structured flag that pins the mozyo home root onto the daemon argv (non-secret; a config
#: directory, resolved by the supervisor CLI's ``--home``).
SUPERVISOR_HOME_FLAG = "--home"

# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens (machine-readable; secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

#: A mutating verb (install/restart/uninstall) was refused because the host is not macOS.
REASON_UNSUPPORTED_PLATFORM = "launchd_unsupported_platform"
#: install/restart refused: the `mozyo-bridge` executable is not resolvable on PATH.
REASON_EXECUTABLE_NOT_FOUND = "supervisor_executable_not_found"
#: restart refused: the service is not currently loaded (restart acts only on a loaded service).
REASON_SERVICE_NOT_LOADED = "service_not_loaded"
#: restart refused: no owned plist is installed (nothing to restart; run install first).
REASON_NOT_INSTALLED = "service_not_installed"
#: restart/status: the installed plist's ``--home`` pin is missing / malformed / duplicated / not an
#: absolute canonical path, so the daemon-effective root cannot be trusted (fail-closed for restart;
#: unhealthy for status). Also used when the owned plist file exists but is unreadable / non-mapping.
REASON_HOME_PIN_UNHEALTHY = "home_pin_unhealthy"
#: restart refused: the requested mozyo home differs from the installed plist pin (a home change
#: must go through ``install``, which rewrites the plist — restart never silently re-points).
REASON_HOME_PIN_MISMATCH = "home_pin_mismatch"
#: restart refused: the installed ``ProgramArguments`` no longer match the command an install would
#: write now (executable moved / argv drift). An executable / home change must reinstall (rewrite the
#: plist); restart never kickstarts a drifted command (j#79136 R4-F2).
REASON_INSTALLED_COMMAND_DRIFT = "installed_command_drift"
#: A launchctl bootstrap failed (message redacted to a fixed token; no host detail leaks).
REASON_BOOTSTRAP_FAILED = "launchctl_bootstrap_failed"
#: A launchctl kickstart failed (message redacted to a fixed token).
REASON_KICKSTART_FAILED = "launchctl_kickstart_failed"

#: ``home_pin`` extraction status vocabulary (see :func:`_extract_pinned_home`).
HOME_PIN_OK = "ok"
HOME_PIN_MISSING = "missing"
HOME_PIN_DUPLICATE = "duplicate"
HOME_PIN_MALFORMED = "malformed"
#: The pin value is present but not an absolute, lexically-canonical path (relative / ``~`` / has
#: ``..`` etc.) — a launchd daemon resolves it from a different cwd than the installer (j#79136 R4-F1).
HOME_PIN_NOT_ABSOLUTE = "not_absolute"
HOME_PIN_NO_ARGV = "no_argv"
#: The owned plist file exists but could not be parsed / is not a mapping (distinct from absence,
#: which is ``not_installed``) — j#79136 R4-F3.
HOME_PIN_UNREADABLE = "unreadable_plist"
HOME_PIN_NOT_INSTALLED = "not_installed"

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


def plist_path(os_home: Optional[Path] = None) -> Path:
    """The owned plist path under the **OS user home** (``~/Library/LaunchAgents``)."""
    return (os_home or Path.home()) / PLIST_RELATIVE


def log_path(os_home: Optional[Path] = None) -> Path:
    """The owned log path under the **OS user home** (``~/Library/Logs``)."""
    return (os_home or Path.home()) / LOG_RELATIVE


def resolve_mozyo_home(mozyo_home: Optional[Path] = None) -> Path:
    """Resolve the exact **mozyo home** root (credential / registry / store) as an absolute path.

    ``mozyo_home`` (the supervisor CLI's ``--home``) wins; otherwise the package's home contract
    (:func:`mozyo_bridge_home`: ``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``). An explicit value is
    ``expanduser().resolve()``-normalized to an **absolute canonical root** — a relative / ``~``
    input must never be pinned onto the daemon argv, since a LaunchAgent's working directory is not
    the installer shell's, so a relative pin would re-diverge the credential / registry root
    (j#79125 R3-F2). ``mozyo_bridge_home()`` already returns an absolute resolved path.
    """
    if mozyo_home is not None:
        return Path(mozyo_home).expanduser().resolve()
    return mozyo_bridge_home()


def resolve_supervisor_command(
    *,
    mozyo_home: Optional[Path] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[list[str]]:
    """The exact argv the agent runs, or ``None`` when the executable is not on PATH.

    The executable is PATH-resolved at install time (so the plist survives shell-env differences)
    and the **resolved mozyo home** is pinned as ``--home <root>`` so the launchd daemon reads the
    same credential / registry root the install preflight validated (j#79092 R2-F1). A missing
    executable is a fail-closed condition the caller turns into a zero-mutation refusal (install the
    package first) — never a shell string and never a guessed path.
    """
    executable = which(SUPERVISOR_EXECUTABLE_NAME)
    if not executable:
        return None
    return [
        executable,
        *SUPERVISOR_ARGV_TAIL,
        SUPERVISOR_HOME_FLAG,
        str(resolve_mozyo_home(mozyo_home)),
    ]


def render_plist(
    command: Sequence[str],
    *,
    interval_seconds: int,
    os_home: Optional[Path] = None,
) -> bytes:
    """Render the LaunchAgent plist for the one-shot scheduled supervisor sweep.

    Structurally minimal and secret-free:

    - **No** ``EnvironmentVariables`` key exists in the output, so no secret can be serialized in.
    - **No** ``KeepAlive`` key: the command is a bounded ``--run-once`` sweep that exits;
      ``RunAtLoad`` runs it once at load and ``StartInterval`` re-runs it every ``interval_seconds``.
      KeepAlive would be a tight restart loop for a one-shot command, so it is absent by design.
    - ``ProgramArguments`` is the exact structured argv (PATH-resolved executable + fixed tail +
      the pinned ``--home <mozyo root>``). The log lives under the OS user home (``os_home``).
    """
    payload = {
        "Label": SUPERVISOR_LAUNCHD_LABEL,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "StartInterval": max(1, int(interval_seconds)),
        "StandardOutPath": str(log_path(os_home)),
        "StandardErrorPath": str(log_path(os_home)),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


# ---------------------------------------------------------------------------
# Credential readiness (the exact readiness the live supervisor needs; secret-safe token only).
# ---------------------------------------------------------------------------


def classify_credential_readiness(*, mozyo_home: Optional[Path] = None) -> str:
    """Classify **daemon-effective** Redmine credential readiness into a fixed, secret-safe token.

    Judges what the *launchd-managed* supervisor will actually have at run time, not what the
    installer's interactive shell happens to hold. Two independent leaks are closed:

    - **shell key/URL** — the plist carries no ``EnvironmentVariables`` and launchd inherits no
      shell environment, so readiness resolves with an **empty environ**: an installer's exported
      ``MOZYO_REDMINE_*`` can never produce a false ``ready`` (Redmine #13683 review j#79059 F1).
    - **shell home root** — the credential file's root is the resolved **mozyo home**
      (:func:`resolve_mozyo_home`), the exact root pinned onto the daemon argv, not whatever
      ``mozyo_bridge_home()`` a later launchd process (with no ``MOZYO_BRIDGE_HOME``) would
      re-derive (j#79092 R2-F1).

    Ready needs an api key **and** a normalizable base URL from that home file; a present-but-unsafe
    / malformed file surfaces as :data:`CREDENTIAL_UNSAFE` (the resolver refuses to read it and
    returns a redacted warning), so a fail-closed refusal is visibly deliberate. Returns only a
    token — never the key, the URL, or the warning text.
    """
    creds = resolve_redmine_credentials(resolve_mozyo_home(mozyo_home), environ={})
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
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Write the owned plist and (re)bootstrap the agent. Idempotent; fail-closed zero-mutation.

    Refuses — before any filesystem write or launchctl call — on a non-darwin host, a missing
    executable, or a non-ready **daemon-effective** Redmine credential (the mozyo-home file the
    launchd agent will actually see; an installer's shell env / ``MOZYO_BRIDGE_HOME`` do not leak
    in). The mozyo home is resolved **once** and used for both the readiness check and the pinned
    ``--home`` argv, so the daemon reads the exact root the preflight validated. The plist / log
    live under the OS user home (``os_home``). On success the plist is rewritten idempotently and
    the agent is booted out (ignore-failure) then bootstrapped.
    """
    if not _running_on_darwin():
        return _refused("install", REASON_UNSUPPORTED_PLATFORM)
    resolved_mozyo = resolve_mozyo_home(mozyo_home)
    command = resolve_supervisor_command(mozyo_home=resolved_mozyo, which=which)
    if command is None:
        return _refused("install", REASON_EXECUTABLE_NOT_FOUND)
    readiness = classify_credential_readiness(mozyo_home=resolved_mozyo)
    if readiness != CREDENTIAL_READY:
        return _refused(
            "install", _CREDENTIAL_REFUSAL_REASON[readiness], credential_readiness=readiness
        )

    target = plist_path(os_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    log_path(os_home).parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_plist(command, interval_seconds=interval_seconds, os_home=os_home))
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
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Kickstart (kill + relaunch) the *loaded* agent. Fail-closed zero-mutation.

    The **installed plist** — not the caller's current shell — is the authority on the daemon's
    mozyo home: restart reads the ``--home`` pin from the owned plist and checks *that* exact root's
    credential readiness, so it never reports a false-ready restart when the current shell resolves a
    different (ready) home than the one the loaded service actually runs with (j#79125 R3-F1).

    Refuses — before any launchctl mutation — on a non-darwin host, no installed plist (file
    absent), an owned plist that exists but is unreadable / non-mapping, an unhealthy ``--home`` pin
    (missing / malformed / duplicated / not an absolute canonical path), a requested ``mozyo_home``
    that differs from the pin, installed ``ProgramArguments`` that no longer match the command an
    install would write now (executable / argv drift — reinstall to change), a missing executable, a
    non-ready pinned-home credential, or a service that is not loaded.
    """
    if not _running_on_darwin():
        return _refused("restart", REASON_UNSUPPORTED_PLATFORM)
    target = plist_path(os_home)
    if not target.exists():
        return _refused("restart", REASON_NOT_INSTALLED)
    installed = _read_installed_plist(target)
    if installed is None:
        # File present but unreadable / non-mapping — unhealthy, NOT absence (j#79136 R4-F3).
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=HOME_PIN_UNREADABLE)
    installed_argv = installed.get("ProgramArguments")
    pinned, pin_status = _extract_pinned_home(installed_argv)
    if pin_status != HOME_PIN_OK:
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=pin_status)
    # A requested home that disagrees with the installed pin is a re-point attempt — refuse; a home
    # change must rewrite the plist via install, not silently kickstart the old pin.
    if mozyo_home is not None and str(resolve_mozyo_home(mozyo_home)) != pinned:
        return _refused("restart", REASON_HOME_PIN_MISMATCH, home_pin=pin_status)
    pinned_home = Path(pinned)
    expected = resolve_supervisor_command(mozyo_home=pinned_home, which=which)
    if expected is None:
        return _refused("restart", REASON_EXECUTABLE_NOT_FOUND)
    # The installed command must still be exactly what an install would write (same authority as
    # `service_status`'s executable_matches): a moved executable or any argv drift means the loaded
    # service runs a stale command — reinstall to change it, never kickstart the drift (j#79136 R4-F2).
    if installed_argv != expected:
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT)
    readiness = classify_credential_readiness(mozyo_home=pinned_home)
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
    os_home: Optional[Path] = None,
    runner: Runner = _default_runner,
) -> dict:
    """Boot the agent out and remove exactly the owned plist. No credential required.

    Refuses only on a non-darwin host (there is no launchd to bootout). On darwin, tears down the
    agent even when credentials are absent — you must be able to remove a service without them. The
    plist lives under the OS user home (``os_home``); no mozyo home is needed to remove it.
    """
    if not _running_on_darwin():
        return _refused("uninstall", REASON_UNSUPPORTED_PLATFORM)
    _launchctl(runner, ["bootout", _service_target()])
    target = plist_path(os_home)
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
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_hint: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """A read-only, redacted projection of the host service state. Mutates nothing.

    Reports plist existence (under the OS user home ``os_home``), loaded/pid, the *scheduled*
    interval, the ``--home`` pin health, whether the installed argv still matches the one an install
    would write now, and **daemon-effective** credential readiness — as booleans / counts / fixed
    tokens only. When a plist is installed and readable, ``credential_readiness`` is that of the
    **pinned** mozyo home (the root the loaded daemon actually runs with), not the caller's current
    shell, so the projection reflects the *installed daemon*, not a would-be re-point (j#79125 R3-F1).
    An unhealthy pin — or an owned plist that exists but is unreadable / non-mapping (``home_pin`` =
    ``unreadable_plist``; distinct from absence, which is ``not_installed`` — j#79136 R4-F3) —
    surfaces as ``home_pin`` != ``ok`` with an empty readiness (unknowable). Only when nothing is
    installed is ``credential_readiness`` the would-be root's (``mozyo_home`` / default). Never emits
    a credential value, a request header, a repo-local path, or pane text.
    """
    target = plist_path(os_home)
    plist_exists = target.exists()
    loaded, pid = _is_loaded(runner)

    installed = _read_installed_plist(target) if plist_exists else None
    # Three distinct states: absent (not_installed), present-but-unreadable (unreadable_plist), and
    # present + parsed (judged by its --home pin) — j#79136 R4-F3.
    scheduled_interval = installed.get("StartInterval") if installed else None
    run_at_load = bool(installed.get("RunAtLoad")) if installed else None
    keep_alive_present = ("KeepAlive" in installed) if installed else False
    no_environment_block = ("EnvironmentVariables" not in installed) if installed else True

    installed_argv = installed.get("ProgramArguments") if installed else None
    if installed is not None:
        pinned, pin_status = _extract_pinned_home(installed_argv)
        # The installed daemon's authority is its own pin; readiness is unknowable if the pin is bad.
        credential_readiness = (
            classify_credential_readiness(mozyo_home=Path(pinned))
            if pin_status == HOME_PIN_OK
            else ""
        )
        # "still what an install would write" is judged against the pinned home, not the caller's.
        expected = (
            resolve_supervisor_command(mozyo_home=Path(pinned), which=which)
            if pin_status == HOME_PIN_OK
            else None
        )
    elif plist_exists:
        # File present but unparseable / non-mapping — unhealthy, NOT absence (j#79136 R4-F3).
        pin_status = HOME_PIN_UNREADABLE
        credential_readiness = ""
        expected = None
    else:
        pin_status = HOME_PIN_NOT_INSTALLED
        credential_readiness = classify_credential_readiness(
            mozyo_home=resolve_mozyo_home(mozyo_home)
        )
        expected = None

    executable_matches = bool(
        expected is not None
        and isinstance(installed_argv, list)
        and installed_argv == expected
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
        "home_pin": pin_status,
        "executable_matches": executable_matches,
        "credential_readiness": credential_readiness,
    }


def _read_installed_plist(target: Path) -> Optional[dict]:
    """Best-effort parse of the installed plist; ``None`` if unreadable/malformed (never raises)."""
    try:
        raw = target.read_bytes()
        parsed = plistlib.loads(raw)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_pinned_home(installed_argv: object) -> tuple[Optional[str], str]:
    """Extract the ``--home`` pin from an installed plist's ``ProgramArguments`` (strict).

    Returns ``(pinned_home, status)``. The installed plist — not the caller's current shell — is the
    authority on the daemon's mozyo home, so restart / status read the pin from here (j#79125 R3-F1).
    A missing / duplicated / value-less pin is *not* trusted (the daemon-effective root is unknowable),
    and a pin that is not an **absolute, lexically-canonical** path (relative / ``~`` / containing
    ``..``) is rejected too: a LaunchAgent resolves such a pin from a different working directory than
    the installer, re-opening the R3-F2 divergence in the installed service (j#79136 R4-F1). Every
    non-``ok`` case is surfaced (fail-closed for restart, unhealthy for status), never guessed.
    """
    if not isinstance(installed_argv, list):
        return None, HOME_PIN_NO_ARGV
    indices = [i for i, arg in enumerate(installed_argv) if arg == SUPERVISOR_HOME_FLAG]
    if not indices:
        return None, HOME_PIN_MISSING
    if len(indices) > 1:
        return None, HOME_PIN_DUPLICATE
    value_index = indices[0] + 1
    if value_index >= len(installed_argv):
        return None, HOME_PIN_MALFORMED
    value = installed_argv[value_index]
    if not isinstance(value, str) or not value.strip() or value.startswith("--"):
        return None, HOME_PIN_MALFORMED
    # An install always pins ``str(resolve_mozyo_home(...))`` — absolute + canonical. Anything else
    # (relative, ``~``, ``/a/../b``) would be resolved from launchd's cwd, not the installer's.
    if not os.path.isabs(value) or value != os.path.normpath(value):
        return None, HOME_PIN_NOT_ABSOLUTE
    return value, HOME_PIN_OK


def _refused(action: str, reason: str, **extra: object) -> dict:
    """A fail-closed, zero-mutation refusal result (fixed vocabulary; no host detail)."""
    return {"action": action, "performed": False, "reason": reason, **extra}


__all__ = (
    "SUPERVISOR_LAUNCHD_LABEL",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "REASON_UNSUPPORTED_PLATFORM",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_SERVICE_NOT_LOADED",
    "REASON_NOT_INSTALLED",
    "REASON_HOME_PIN_UNHEALTHY",
    "REASON_HOME_PIN_MISMATCH",
    "REASON_INSTALLED_COMMAND_DRIFT",
    "REASON_BOOTSTRAP_FAILED",
    "REASON_KICKSTART_FAILED",
    "HOME_PIN_OK",
    "HOME_PIN_MISSING",
    "HOME_PIN_DUPLICATE",
    "HOME_PIN_MALFORMED",
    "HOME_PIN_NOT_ABSOLUTE",
    "HOME_PIN_NO_ARGV",
    "HOME_PIN_UNREADABLE",
    "HOME_PIN_NOT_INSTALLED",
    "CREDENTIAL_READY",
    "CREDENTIAL_INCOMPLETE",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_UNSAFE",
    "plist_path",
    "log_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "render_plist",
    "classify_credential_readiness",
    "install",
    "restart",
    "uninstall",
    "service_status",
)
