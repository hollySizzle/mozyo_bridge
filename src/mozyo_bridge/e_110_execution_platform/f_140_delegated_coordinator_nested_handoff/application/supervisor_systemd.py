"""Linux systemd **user** service + timer lifecycle for the callback supervisor (Redmine #15183).

The canonical design (``vibes/docs/logics/ticket-system-neutral-orchestrator.md`` ``### OS scheduler
adapter``) defines LaunchAgent / systemd timer / cron as adapters that start the **same** bounded
one-shot command. Only the macOS LaunchAgent realization existed, so on Linux the bounded
``--run-once`` / ``--drain-only`` bodies ran but nothing scheduled them: retiring finished sublanes,
delivering callbacks, and supplying durable state all fell back to a human running the command.
This module is the Linux realization of the *same* service lifecycle contract
(:mod:`...application.supervisor_launchd` is the macOS one), and **nothing more**.

Design boundary (mirrors the launchd adapter's, expressed in systemd terms):

- **One-shot scheduled cadence, never a resident daemon.** The service unit is ``Type=oneshot``: the
  bounded sweep runs and the process exits. A ``.timer`` unit owns the cadence — ``OnActiveSec=0s``
  (run once the moment the timer activates: the RunAtLoad equivalent, covering both
  ``enable --now`` and every later user-manager start) plus ``OnUnitActiveSec=<interval>`` (re-run
  every N seconds: the StartInterval equivalent). The service unit carries **no** ``Restart=`` and
  **no** ``RemainAfterExit=`` — a restart directive on a one-shot is a tight relaunch loop, exactly
  the reason the launchd plist has no ``KeepAlive``, so it is structurally absent, not set to a
  falsy value.
- **No secret ever reaches a unit file.** The rendered units have **no** ``Environment=`` and no
  ``EnvironmentFile=`` key at all, so no code path can serialize a credential into one. A
  systemd-started supervisor inherits no interactive shell environment; the Redmine key/URL reach it
  through the daemon-trusted home-scoped credential file (``resolve_redmine_credentials``), never a
  unit. ``ExecStart`` is the exact PATH-resolved ``mozyo-bridge`` executable + structured argv,
  systemd-quoted per argument — never a shell string, and never ``/bin/sh -c``.
- **Structured systemctl only.** Every ``systemctl --user`` invocation is structured argv
  (``daemon-reload`` / ``enable --now`` / ``disable --now`` / ``stop`` / ``restart`` / ``show``) —
  no shell. Install is idempotent (rewrite the units, reload, re-enable), restart acts only on a
  service whose owned timer is *active*, uninstall removes exactly the owned unit files.
- **Fail-closed, zero-mutation refusals.** ``install`` / ``restart`` refuse — *before* writing any
  file or invoking systemctl — on a non-Linux host, an unreachable systemd **user** manager (a
  container with no user bus is explicitly unsupported, not silently degraded), a missing
  executable, or a Redmine credential that is missing / incomplete / unsafe / malformed.
  ``uninstall`` and status stay usable with no credential at all (you must be able to tear a service
  down without configured credentials).
- **Redacted status projection.** Status reports unit existence / enabled / active / pid / scheduled
  interval / home-pin health / executable-match / credential readiness as booleans, counts, and
  fixed-vocabulary tokens only — no credential value, no request header, no repo-local path.

This module performs **no** Redmine fetch, gate progression, route resolution, or callback delivery:
installing / restarting / uninstalling the units is orthogonal to what they do when they run. It
also never touches worktrees, branches, or the #15066 managed-process retirement boundary.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_service_common import (
    CREDENTIAL_INCOMPLETE,
    CREDENTIAL_MISSING,
    CREDENTIAL_READY,
    CREDENTIAL_REFUSAL_REASON,
    CREDENTIAL_UNSAFE,
    HOME_PIN_DUPLICATE,
    HOME_PIN_MALFORMED,
    HOME_PIN_MISSING,
    HOME_PIN_NO_ARGV,
    HOME_PIN_NOT_ABSOLUTE,
    HOME_PIN_NOT_INSTALLED,
    HOME_PIN_OK,
    HOME_PIN_UNREADABLE,
    REASON_EXECUTABLE_NOT_FOUND,
    REASON_HOME_PIN_MISMATCH,
    REASON_HOME_PIN_UNHEALTHY,
    REASON_INSTALLED_COMMAND_DRIFT,
    REASON_NOT_INSTALLED,
    REASON_SERVICE_NOT_LOADED,
    SUPERVISOR_ARGV_TAIL,
    SUPERVISOR_DRAIN_ARGV_TAIL,
    SUPERVISOR_EXECUTABLE_NAME,
    SUPERVISOR_HOME_FLAG,
    Runner,
    classify_credential_readiness,
    default_runner as _default_runner,
    extract_pinned_home as _extract_pinned_home,
    first_failure_reason as _first_failure_reason,
    refused as _refused,
    resolve_mozyo_home,
    resolve_supervisor_command as _resolve_command_for_tail,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS,
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL,
    DEFAULT_SUPERVISOR_SERVICE_LABEL,
)

# ---------------------------------------------------------------------------
# Owned identity.
#
# Two DISTINCT roots must never be conflated (the launchd adapter's review j#79092 R2-F1 applies
# here unchanged):
#   - the **OS user home** (``Path.home()``) owns the unit files under the XDG user-unit directory —
#     this is where the systemd user manager looks, independent of any mozyo config;
#   - the **mozyo home** (``mozyo_bridge_home()``: ``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``)
#     owns the registry / store / credential root the supervisor reads at run time.
#
# The domain service ``label`` stays the reverse-DNS id the declarative definition carries, so the
# two adapters project the same identity; the systemd **unit names** are the filesystem-safe
# realization of it (systemd treats ``.``-suffixes as unit types, so the label is not reused
# verbatim as a unit name).
# ---------------------------------------------------------------------------

SUPERVISOR_SYSTEMD_LABEL = DEFAULT_SUPERVISOR_SERVICE_LABEL
SUPERVISOR_DRAIN_SYSTEMD_LABEL = DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL

#: The XDG-relative directory the systemd **user** manager reads owned units from.
UNIT_DIR_RELATIVE = Path("systemd/user")
#: The config root under an explicitly supplied OS user home (``$XDG_CONFIG_HOME`` default).
CONFIG_DIR_RELATIVE = Path(".config")

SERVICE_UNIT_NAME = "mozyo-bridge-callback-supervisor.service"
TIMER_UNIT_NAME = "mozyo-bridge-callback-supervisor.timer"
DRAIN_SERVICE_UNIT_NAME = "mozyo-bridge-callback-supervisor-drain.service"
DRAIN_TIMER_UNIT_NAME = "mozyo-bridge-callback-supervisor-drain.timer"

#: The systemd target a user timer installs into.
TIMERS_TARGET = "timers.target"
_SYSTEMCTL = "systemctl"
#: The ``[Timer]`` delay that reproduces launchd's ``RunAtLoad``: fire the moment the timer unit
#: becomes active (on ``enable --now`` and on every later user-manager start).
RUN_AT_LOAD_DELAY = "0s"


@dataclasses.dataclass(frozen=True)
class SupervisorUnit:
    """One owned systemd user unit pair (service + timer) and the bounded argv tail it runs."""

    label: str
    argv_tail: tuple[str, ...]
    service_unit: str
    timer_unit: str
    description: str
    default_interval_seconds: int


#: The coarse provider-reconciliation pair (``workflow supervisor --run-once``).
RECONCILE_UNIT = SupervisorUnit(
    label=SUPERVISOR_SYSTEMD_LABEL,
    argv_tail=SUPERVISOR_ARGV_TAIL,
    service_unit=SERVICE_UNIT_NAME,
    timer_unit=TIMER_UNIT_NAME,
    description="mozyo-bridge callback supervisor bounded reconciliation sweep",
    default_interval_seconds=DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
)
#: The finer local-drain pair (``workflow supervisor --drain-only``; Redmine #14150).
DRAIN_UNIT = SupervisorUnit(
    label=SUPERVISOR_DRAIN_SYSTEMD_LABEL,
    argv_tail=SUPERVISOR_DRAIN_ARGV_TAIL,
    service_unit=DRAIN_SERVICE_UNIT_NAME,
    timer_unit=DRAIN_TIMER_UNIT_NAME,
    description="mozyo-bridge callback supervisor local outbox drain",
    default_interval_seconds=DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS,
)
#: The owned pairs an install/uninstall/status sweep manages, in dependency order (reconcile first).
SUPERVISOR_UNITS = (RECONCILE_UNIT, DRAIN_UNIT)

# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens specific to this adapter (secret-safe; language-independent).
# The executable / not-installed / not-loaded / home-pin / drift / credential tokens are the shared
# platform-neutral vocabulary imported above, so both adapters refuse in the same words.
# ---------------------------------------------------------------------------

#: A verb was refused because the host is not Linux (there is no systemd user manager to drive).
REASON_UNSUPPORTED_PLATFORM = "systemd_unsupported_platform"
#: A verb was refused because no systemd **user** manager is reachable — ``systemctl`` is absent, or
#: it cannot reach a user bus (the container / no-session case). Explicitly unsupported, never a
#: silent degrade to "installed but never scheduled".
REASON_USER_MANAGER_UNAVAILABLE = "systemd_user_manager_unavailable"
#: ``systemctl --user daemon-reload`` failed (message redacted to a fixed token).
REASON_DAEMON_RELOAD_FAILED = "systemctl_daemon_reload_failed"
#: ``systemctl --user enable --now <timer>`` failed (message redacted to a fixed token).
REASON_ENABLE_FAILED = "systemctl_enable_failed"
#: ``systemctl --user restart <service>`` failed (message redacted to a fixed token).
REASON_RESTART_FAILED = "systemctl_restart_failed"


# ---------------------------------------------------------------------------
# Paths + unit rendering (pure; no host mutation, no secrets).
# ---------------------------------------------------------------------------


def unit_dir(os_home: Optional[Path] = None) -> Path:
    """The owned systemd **user** unit directory.

    With an explicit ``os_home`` (tests / an operator pinning a home) the directory is
    ``<os_home>/.config/systemd/user`` — the XDG default under that home. With no ``os_home`` the
    real user-manager search path is honoured: ``$XDG_CONFIG_HOME/systemd/user`` when that variable
    holds an absolute path, else ``~/.config/systemd/user``. Writing anywhere else would produce an
    install that ``systemctl --user`` cannot see — a silently unscheduled supervisor, which is the
    failure this adapter exists to remove.
    """
    if os_home is not None:
        return Path(os_home) / CONFIG_DIR_RELATIVE / UNIT_DIR_RELATIVE
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    config_root = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / CONFIG_DIR_RELATIVE
    return config_root / UNIT_DIR_RELATIVE


def service_unit_path(os_home: Optional[Path] = None, *, unit: SupervisorUnit = RECONCILE_UNIT) -> Path:
    """The owned ``.service`` unit path (the bounded one-shot command)."""
    return unit_dir(os_home) / unit.service_unit


def timer_unit_path(os_home: Optional[Path] = None, *, unit: SupervisorUnit = RECONCILE_UNIT) -> Path:
    """The owned ``.timer`` unit path (the cadence that starts the one-shot)."""
    return unit_dir(os_home) / unit.timer_unit


def resolve_supervisor_command(
    *,
    mozyo_home: Optional[Path] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    unit: SupervisorUnit = RECONCILE_UNIT,
) -> Optional[list[str]]:
    """The exact argv the scheduled unit runs, or ``None`` when the executable is not on PATH.

    Thin systemd-side binding of the shared resolver: the executable is PATH-resolved to an absolute
    canonical path at install time and the resolved mozyo home is pinned as ``--home <root>``, so the
    systemd-started process reads the credential / registry root the install preflight validated
    (systemd carries no ``MOZYO_BRIDGE_HOME`` from the installer's shell).
    """
    return _resolve_command_for_tail(argv_tail=unit.argv_tail, mozyo_home=mozyo_home, which=which)


def format_exec_argv(command: Sequence[str]) -> str:
    """Render argv as a systemd ``ExecStart`` value: one double-quoted, escaped token per argument.

    systemd splits ``ExecStart`` on whitespace with its own quoting rules, so an unquoted path
    containing a space would silently become two arguments. Every token is emitted double-quoted
    with ``\\`` and ``"`` escaped, which is unambiguous for any path and round-trips through
    :func:`parse_exec_argv`. This is a *value*, never a shell string: systemd execs the argv
    directly, with no ``/bin/sh`` in between.
    """
    parts = []
    for arg in command:
        escaped = str(arg).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{escaped}"')
    return " ".join(parts)


def parse_exec_argv(value: str) -> Optional[list[str]]:
    """Parse a rendered ``ExecStart`` value back into argv, or ``None`` when it is not parseable.

    The inverse of :func:`format_exec_argv`, tolerant of bare (unquoted) tokens so a hand-edited
    unit still reads back. It deliberately does **not** interpret systemd's ``-`` / ``@`` / ``:`` /
    ``!`` command prefixes or ``%`` specifiers: this adapter never writes them, so a unit carrying
    one parses to a token that will not match the expected command and is reported as drift rather
    than being silently normalized away.
    """
    argv: list[str] = []
    token: list[str] = []
    in_token = False
    quote: Optional[str] = None
    escape = False
    for ch in value:
        if escape:
            token.append(ch)
            escape = False
            continue
        if ch == "\\":
            escape = True
            in_token = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                token.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
            in_token = True
            continue
        if ch.isspace():
            if in_token:
                argv.append("".join(token))
                token = []
                in_token = False
            continue
        token.append(ch)
        in_token = True
    if quote is not None or escape:
        return None  # unterminated quote / trailing escape: not trustworthy
    if in_token:
        argv.append("".join(token))
    return argv or None


def render_service_unit(
    command: Sequence[str], *, unit: SupervisorUnit = RECONCILE_UNIT
) -> str:
    """Render the ``.service`` unit for the one-shot scheduled supervisor sweep.

    Structurally minimal and secret-free:

    - **No** ``Environment=`` / ``EnvironmentFile=`` key exists in the output, so no secret can be
      serialized in.
    - **No** ``Restart=`` and **no** ``RemainAfterExit=`` key: the command is a bounded sweep that
      exits and the ``.timer`` re-runs it. A restart directive on a one-shot would be a tight
      relaunch loop (the systemd analogue of launchd ``KeepAlive``), so it is absent by design.
    - **No** ``[Install]`` section: the *timer* is what gets enabled. A directly enabled service
      would run once at login and never again, quietly replacing the cadence.
    - ``ExecStart`` is the exact structured argv (PATH-resolved executable + fixed tail + the pinned
      ``--home <mozyo root>``). Output goes to the journal (systemd's default), so no owned log path
      is created and nothing is written outside the unit directory.
    """
    return "\n".join(
        (
            "[Unit]",
            f"Description={unit.description}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={format_exec_argv(command)}",
            f"SyslogIdentifier={Path(unit.service_unit).stem}",
            "",
        )
    )


def render_timer_unit(
    *, interval_seconds: int, unit: SupervisorUnit = RECONCILE_UNIT
) -> str:
    """Render the ``.timer`` unit that schedules the one-shot service.

    ``OnActiveSec=0s`` fires the sweep the moment the timer becomes active — on ``enable --now`` and
    again on every later user-manager start — which is launchd ``RunAtLoad``. ``OnUnitActiveSec``
    re-runs it every ``interval_seconds`` after the last run, which is launchd ``StartInterval``.
    ``AccuracySec=1s`` keeps the cadence honest instead of letting systemd coalesce it into a
    minute-wide window. No ``OnCalendar`` / ``Persistent=`` (there is no missed-run catch-up to
    replay: the next tick reconciles whatever the last one missed).
    """
    return "\n".join(
        (
            "[Unit]",
            f"Description={unit.description} timer",
            "",
            "[Timer]",
            f"Unit={unit.service_unit}",
            f"OnActiveSec={RUN_AT_LOAD_DELAY}",
            f"OnUnitActiveSec={max(1, int(interval_seconds))}s",
            "AccuracySec=1s",
            "",
            "[Install]",
            f"WantedBy={TIMERS_TARGET}",
            "",
        )
    )


# ---------------------------------------------------------------------------
# Installed-unit reading (best-effort; never raises).
# ---------------------------------------------------------------------------


def _read_unit_keys(target: Path) -> Optional[dict[str, list[str]]]:
    """Parse an installed unit file into ``{key: [values]}``; ``None`` if unreadable.

    Section-flat on purpose: this adapter only ever asks "does key X exist / what is its value",
    and the owned units never reuse a key name across sections. Comments and blank lines are
    dropped; a line without ``=`` is ignored rather than raising.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    keys: dict[str, list[str]] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        keys.setdefault(key.strip(), []).append(value.strip())
    return keys


def _installed_command(service_keys: Optional[dict[str, list[str]]]) -> Optional[list[str]]:
    """The argv an installed ``.service`` runs, or ``None`` when absent / not parseable / duplicated."""
    if not service_keys:
        return None
    values = service_keys.get("ExecStart") or []
    if len(values) != 1:
        return None  # absent, or several ExecStart lines: the effective command is not a single argv
    return parse_exec_argv(values[0])


def _installed_interval_seconds(timer_keys: Optional[dict[str, list[str]]]) -> Optional[int]:
    """The scheduled cadence an installed ``.timer`` declares, or ``None`` when unreadable.

    Only the exact ``<N>s`` form this adapter writes is read back. A hand-edited ``5min`` is
    reported as an unknown cadence rather than being re-interpreted — status projects what it can
    verify, and a cadence it cannot parse is not a cadence it should claim.
    """
    if not timer_keys:
        return None
    values = timer_keys.get("OnUnitActiveSec") or []
    if len(values) != 1:
        return None
    raw = values[0].strip()
    if not raw.endswith("s"):
        return None
    digits = raw[:-1]
    if not (digits.isascii() and digits.isdigit()):
        return None
    return int(digits)


# ---------------------------------------------------------------------------
# systemctl seam (structured argv only; no shell).
# ---------------------------------------------------------------------------


def _running_on_linux() -> bool:
    return sys.platform.startswith("linux")


def _systemctl(runner: Runner, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return runner([_SYSTEMCTL, "--user", *args])


def user_manager_available(runner: Runner = _default_runner) -> bool:
    """Whether a systemd **user** manager is reachable. Read-only; never raises.

    Probed with ``systemctl --user show --property=Version``, which succeeds only when systemctl
    exists *and* it can talk to a user bus. A container without a user session fails here, and every
    mutating verb refuses with :data:`REASON_USER_MANAGER_UNAVAILABLE` rather than writing unit files
    nothing will ever read. Deliberately not ``is-system-running``: that reports non-zero for a
    perfectly usable ``degraded`` manager.
    """
    try:
        result = _systemctl(runner, ["show", "--property=Version"])
    except (FileNotFoundError, OSError):  # systemctl absent / not executable
        return False
    return result.returncode == 0


#: The widest process id ``systemctl show`` can print. DERIVED, not chosen: Linux ``pid_t`` is a
#: signed 32-bit integer, so ten digits covers every value the kernel can assign. Reading it as an
#: ASCII decimal inside that width (never ``str.isdigit()``, which accepts non-ASCII digits
#: ``int()`` may still read but which are not pids) keeps this projection's "never raises" promise —
#: the defect Redmine #14753 recorded against the launchd adapter's pid read.
_MAX_PID_DIGITS = len(str(2**31 - 1))


def _show(
    runner: Runner, unit_name: str, properties: Sequence[str]
) -> dict[str, str]:
    """Read-only ``systemctl --user show`` → ``{property: value}``. Never raises."""
    args = ["show", unit_name, *[f"--property={p}" for p in properties]]
    try:
        result = _systemctl(runner, args)
    except (FileNotFoundError, OSError):
        return {}
    if result.returncode != 0:
        return {}
    values: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def _timer_state(runner: Runner, unit: SupervisorUnit) -> tuple[bool, bool]:
    """(timer_active, timer_enabled) for the owned timer. Read-only; never raises."""
    shown = _show(runner, unit.timer_unit, ("ActiveState", "UnitFileState"))
    return shown.get("ActiveState") == "active", shown.get("UnitFileState") == "enabled"


def _service_pid(runner: Runner, unit: SupervisorUnit) -> Optional[int]:
    """The owned one-shot's pid while a sweep is running, else ``None``. Never raises."""
    token = _show(runner, unit.service_unit, ("MainPID",)).get("MainPID", "")
    if not (token.isascii() and token.isdigit() and len(token) <= _MAX_PID_DIGITS):
        return None
    pid = int(token)
    return pid or None  # systemd reports 0 for "no process right now"


# ---------------------------------------------------------------------------
# Lifecycle verbs (structured results; fail-closed, zero-mutation refusals).
# ---------------------------------------------------------------------------


def _preflight(action: str, unit: SupervisorUnit, runner: Runner) -> Optional[dict]:
    """The host preflight both mutating verbs share, or ``None`` when the host is usable."""
    if not _running_on_linux():
        return _refused(action, REASON_UNSUPPORTED_PLATFORM, label=unit.label)
    if not user_manager_available(runner):
        return _refused(action, REASON_USER_MANAGER_UNAVAILABLE, label=unit.label)
    return None


def install(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    unit: SupervisorUnit = RECONCILE_UNIT,
) -> dict:
    """Write the owned service + timer units and enable the timer. Idempotent; fail-closed.

    Refuses — before any filesystem write or systemctl call — on a non-Linux host, an unreachable
    systemd user manager, a missing executable, or a non-ready **daemon-effective** Redmine
    credential (the mozyo-home file the systemd-started process will actually see; an installer's
    shell env / ``MOZYO_BRIDGE_HOME`` do not leak in). The mozyo home is resolved **once** and used
    for both the readiness check and the pinned ``--home`` argv, so the scheduled process reads the
    exact root the preflight validated. On success both units are rewritten idempotently, the
    manager is reloaded, and the timer is enabled + started.

    A failure *after* the units are written leaves them on disk and reports ``performed: False``
    (matching the launchd adapter): :func:`install_pair` is the atomic-or-nothing boundary and rolls
    the whole pair back, so a partial failure never leaves a half-installed pair behind.
    """
    blocked = _preflight("install", unit, runner)
    if blocked is not None:
        return blocked
    resolved_mozyo = resolve_mozyo_home(mozyo_home)
    command = resolve_supervisor_command(mozyo_home=resolved_mozyo, which=which, unit=unit)
    if command is None:
        return _refused("install", REASON_EXECUTABLE_NOT_FOUND, label=unit.label)
    readiness = classify_credential_readiness(mozyo_home=resolved_mozyo)
    if readiness != CREDENTIAL_READY:
        return _refused(
            "install", CREDENTIAL_REFUSAL_REASON[readiness],
            credential_readiness=readiness, label=unit.label,
        )

    service_target = service_unit_path(os_home, unit=unit)
    timer_target = timer_unit_path(os_home, unit=unit)
    service_target.parent.mkdir(parents=True, exist_ok=True)
    service_target.write_text(render_service_unit(command, unit=unit), encoding="utf-8")
    timer_target.write_text(
        render_timer_unit(interval_seconds=interval_seconds, unit=unit), encoding="utf-8"
    )

    # The manager must re-read the unit directory before the timer can be enabled from it.
    reload_result = _systemctl(runner, ["daemon-reload"])
    if reload_result.returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_DAEMON_RELOAD_FAILED,
            "credential_readiness": readiness, "label": unit.label,
        }
    # ``enable --now`` is idempotent: it rewrites the timers.target want and starts the timer,
    # which (OnActiveSec=0s) runs the first bounded sweep immediately.
    enable_result = _systemctl(runner, ["enable", "--now", unit.timer_unit])
    if enable_result.returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_ENABLE_FAILED,
            "credential_readiness": readiness, "label": unit.label,
        }
    return {
        "action": "install",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
        "scheduled_interval_seconds": max(1, int(interval_seconds)),
        "label": unit.label,
    }


def restart(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    unit: SupervisorUnit = RECONCILE_UNIT,
) -> dict:
    """Re-run the bounded sweep now on the *scheduled* service. Fail-closed zero-mutation.

    The **installed unit** — not the caller's current shell — is the authority on the process's
    mozyo home: restart reads the ``--home`` pin from the owned ``.service`` and checks *that* exact
    root's credential readiness, so it never reports a false-ready restart when the current shell
    resolves a different (ready) home than the scheduled unit actually runs with (j#79125 R3-F1).

    Refuses — before any systemctl mutation — on a non-Linux host, an unreachable user manager, no
    installed service unit (file absent), an owned unit that exists but carries no single parseable
    ``ExecStart``, an unhealthy ``--home`` pin (missing / malformed / duplicated / not an absolute
    canonical path), a requested ``mozyo_home`` that differs from the pin, an installed command that
    no longer matches what an install would write now (executable / argv drift — reinstall to
    change), a missing executable, a non-ready pinned-home credential, or an owned timer that is not
    active (nothing is scheduling this service; bringing it up is ``install``'s job).
    """
    blocked = _preflight("restart", unit, runner)
    if blocked is not None:
        return blocked
    service_target = service_unit_path(os_home, unit=unit)
    if not service_target.exists():
        return _refused("restart", REASON_NOT_INSTALLED, label=unit.label)
    installed_argv = _installed_command(_read_unit_keys(service_target))
    if installed_argv is None:
        # File present but unreadable / no single parseable ExecStart — unhealthy, NOT absence.
        return _refused(
            "restart", REASON_HOME_PIN_UNHEALTHY, home_pin=HOME_PIN_UNREADABLE, label=unit.label
        )
    pinned, pin_status = _extract_pinned_home(installed_argv)
    if pin_status != HOME_PIN_OK:
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=pin_status, label=unit.label)
    # A requested home that disagrees with the installed pin is a re-point attempt — refuse; a home
    # change must rewrite the unit via install, not silently re-run the old pin.
    if mozyo_home is not None and str(resolve_mozyo_home(mozyo_home)) != pinned:
        return _refused("restart", REASON_HOME_PIN_MISMATCH, home_pin=pin_status, label=unit.label)
    pinned_home = Path(pinned)
    expected = resolve_supervisor_command(mozyo_home=pinned_home, which=which, unit=unit)
    if expected is None:
        return _refused("restart", REASON_EXECUTABLE_NOT_FOUND, label=unit.label)
    if installed_argv != expected:
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT, label=unit.label)
    readiness = classify_credential_readiness(mozyo_home=pinned_home)
    if readiness != CREDENTIAL_READY:
        return _refused(
            "restart", CREDENTIAL_REFUSAL_REASON[readiness],
            credential_readiness=readiness, label=unit.label,
        )
    timer_active, _enabled = _timer_state(runner, unit)
    if not timer_active:
        return _refused(
            "restart", REASON_SERVICE_NOT_LOADED, credential_readiness=readiness, label=unit.label
        )
    # ``restart`` on a one-shot stops any in-flight sweep and runs a fresh one — the systemd
    # analogue of ``launchctl kickstart -k``. The timer's own cadence is untouched.
    result = _systemctl(runner, ["restart", unit.service_unit])
    if result.returncode != 0:
        return {
            "action": "restart", "performed": False, "reason": REASON_RESTART_FAILED,
            "credential_readiness": readiness, "label": unit.label,
        }
    return {
        "action": "restart", "performed": True, "reason": "",
        "credential_readiness": readiness, "label": unit.label,
    }


def uninstall(
    *,
    os_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    unit: SupervisorUnit = RECONCILE_UNIT,
) -> dict:
    """Disable + stop the owned timer/service and remove exactly the owned unit files.

    Refuses only on a non-Linux host or an unreachable user manager (there is nothing to disable).
    Otherwise it tears the pair down even when credentials are absent — you must be able to remove a
    service without them. Each systemctl step is best-effort (a not-enabled timer / not-running
    service is a normal idempotent case), then exactly the two owned files are removed and the
    manager is reloaded so the removal takes effect. No mozyo home is needed.

    The final ``reset-failed`` is not cosmetic. ``stop`` on a sweep that is mid-flight SIGTERMs it,
    so the one-shot exits on a signal and systemd records the unit as ``failed``; once the unit files
    are gone that record lingers in the manager as a ``not-found``/``failed`` entry that
    ``list-units`` still shows. Measured on a live user manager during the Redmine #15183
    installed-artifact smoke — the hermetic fake runner cannot observe manager-side state. launchd's
    ``bootout`` leaves no such trace, so "removes exactly the owned artifacts" has to mean the
    manager holds no residue either, not just that the files are unlinked.
    """
    blocked = _preflight("uninstall", unit, runner)
    if blocked is not None:
        return blocked
    _systemctl(runner, ["disable", "--now", unit.timer_unit])
    _systemctl(runner, ["stop", unit.service_unit])
    removed = False
    for target in (timer_unit_path(os_home, unit=unit), service_unit_path(os_home, unit=unit)):
        if target.exists():
            target.unlink()
            removed = True
    _systemctl(runner, ["daemon-reload"])
    _systemctl(runner, ["reset-failed", unit.service_unit, unit.timer_unit])
    return {
        "action": "uninstall", "performed": True, "reason": "",
        "removed": removed, "label": unit.label,
    }


def service_status(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_hint: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    unit: SupervisorUnit = RECONCILE_UNIT,
) -> dict:
    """A read-only, redacted projection of the host service state. Mutates nothing.

    Reports unit existence (under the owned unit directory), timer enabled/active, the running
    sweep's pid if any, the *scheduled* interval, the ``--home`` pin health, whether the installed
    command still matches the one an install would write now, and **daemon-effective** credential
    readiness — as booleans / counts / fixed tokens only. When a service unit is installed and
    readable, ``credential_readiness`` is that of the **pinned** mozyo home (the root the scheduled
    process actually runs with), not the caller's current shell, so the projection reflects the
    *installed* service, not a would-be re-point (j#79125 R3-F1). An unhealthy pin — or an owned unit
    that exists but carries no single parseable ``ExecStart`` (``home_pin`` = ``unreadable_plist``;
    distinct from absence, which is ``not_installed``) — surfaces as ``home_pin`` != ``ok`` with an
    empty readiness (unknowable). Only when nothing is installed is ``credential_readiness`` the
    would-be root's. Never emits a credential value, a request header, or a repo-local path.
    """
    service_target = service_unit_path(os_home, unit=unit)
    timer_target = timer_unit_path(os_home, unit=unit)
    service_exists = service_target.exists()
    timer_exists = timer_target.exists()
    timer_active, timer_enabled = _timer_state(runner, unit)

    service_keys = _read_unit_keys(service_target) if service_exists else None
    timer_keys = _read_unit_keys(timer_target) if timer_exists else None
    installed_argv = _installed_command(service_keys)

    # ``KeepAlive``-equivalent: any Restart= / RemainAfterExit= directive would turn the bounded
    # one-shot into a relaunch loop. Both must be structurally absent, not merely falsy.
    keep_alive_present = bool(
        service_keys and ("Restart" in service_keys or "RemainAfterExit" in service_keys)
    )
    no_environment_block = not (
        service_keys and ("Environment" in service_keys or "EnvironmentFile" in service_keys)
    )
    # ``RunAtLoad`` equivalent: the timer fires the moment it becomes active.
    run_at_load = (
        (timer_keys.get("OnActiveSec") == [RUN_AT_LOAD_DELAY]) if timer_keys else None
    )

    if installed_argv is not None:
        pinned, pin_status = _extract_pinned_home(installed_argv)
        # The installed service's authority is its own pin; readiness is unknowable if the pin is bad.
        credential_readiness = (
            classify_credential_readiness(mozyo_home=Path(pinned))
            if pin_status == HOME_PIN_OK
            else ""
        )
        expected = (
            resolve_supervisor_command(mozyo_home=Path(pinned), which=which, unit=unit)
            if pin_status == HOME_PIN_OK
            else None
        )
    elif service_exists:
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
        expected is not None and isinstance(installed_argv, list) and installed_argv == expected
    )
    scheduled_interval = _installed_interval_seconds(timer_keys)

    return {
        "action": "service-status",
        "label": unit.label,
        "platform_supported": _running_on_linux(),
        "user_manager_available": user_manager_available(runner),
        # A pair is "installed" only when BOTH owned units are present: a lone service file has no
        # cadence and a lone timer has nothing to start.
        "installed": service_exists and timer_exists,
        "service_unit": unit.service_unit,
        "timer_unit": unit.timer_unit,
        "service_unit_exists": service_exists,
        "timer_unit_exists": timer_exists,
        "timer_enabled": timer_enabled,
        # ``loaded`` is the cross-adapter word for "the host manager is scheduling this service".
        "loaded": timer_active,
        "pid": _service_pid(runner, unit),
        "scheduled_interval_seconds": (
            scheduled_interval
            if scheduled_interval is not None
            else (int(interval_hint) if not timer_exists else None)
        ),
        "run_at_load": run_at_load,
        "keep_alive_present": keep_alive_present,
        "no_environment_block": no_environment_block,
        "home_pin": pin_status,
        "executable_matches": executable_matches,
        "credential_readiness": credential_readiness,
    }


# ---------------------------------------------------------------------------
# Dual-unit orchestration: install / restart / uninstall / status BOTH owned pairs (reconcile +
# drain) with the same fail-closed / rollback policy the launchd adapter applies, so an operator
# manages the split as one owned set. Each per-unit result is surfaced under ``units`` /``agents``
# (secret-safe tokens only); the ``agents`` key keeps the CLI projection adapter-neutral.
# ---------------------------------------------------------------------------


def install_pair(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    reconcile_interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    drain_interval_seconds: int = DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Install BOTH owned unit pairs atomically-or-nothing (Redmine #15183 / #14150).

    Installs the reconcile pair first (the coarse provider-reconciliation fallback); if it refuses
    (non-Linux / no user manager / missing executable / non-ready credential) nothing else is
    touched. If it installs but the drain pair then fails, the reconcile install is ROLLED BACK
    (uninstalled) so a partial failure never leaves a half-installed set — the operator sees a clean
    fail-closed result and can fix the cause and retry. Both succeeding is the only ``performed``
    outcome. Idempotent (each install rewrites its units and re-enables its timer).
    """
    reconcile = install(
        os_home=os_home, mozyo_home=mozyo_home, interval_seconds=reconcile_interval_seconds,
        runner=runner, which=which, unit=RECONCILE_UNIT,
    )
    if not reconcile.get("performed"):
        return {
            "action": "install", "performed": False,
            "reason": reconcile.get("reason", ""), "agents": [reconcile],
        }
    drain = install(
        os_home=os_home, mozyo_home=mozyo_home, interval_seconds=drain_interval_seconds,
        runner=runner, which=which, unit=DRAIN_UNIT,
    )
    if not drain.get("performed"):
        # Partial failure: roll BOTH pairs back so no half-installed set is left behind — the
        # reconcile pair that succeeded AND the drain pair's units (``install`` writes the files
        # before it reloads/enables, so a failed enable leaves them to clean up).
        rollback = uninstall_pair(os_home=os_home, runner=runner)
        return {
            "action": "install", "performed": False, "reason": drain.get("reason", ""),
            "rolled_back": True, "agents": [reconcile, drain], "rollback": rollback,
        }
    return {"action": "install", "performed": True, "reason": "", "agents": [reconcile, drain]}


def uninstall_pair(*, os_home: Optional[Path] = None, runner: Runner = _default_runner) -> dict:
    """Uninstall BOTH owned unit pairs. Each is idempotent; refuses only on an unusable host."""
    results = [uninstall(os_home=os_home, runner=runner, unit=u) for u in SUPERVISOR_UNITS]
    return {
        "action": "uninstall",
        "performed": all(r.get("performed") for r in results),
        "reason": _first_failure_reason(results),
        "agents": results,
    }


def restart_pair(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Restart BOTH owned services. Each fails closed on not-scheduled / drift / non-ready."""
    results = [
        restart(os_home=os_home, mozyo_home=mozyo_home, runner=runner, which=which, unit=u)
        for u in SUPERVISOR_UNITS
    ]
    return {
        "action": "restart",
        "performed": all(r.get("performed") for r in results),
        "reason": _first_failure_reason(results),
        "agents": results,
    }


def service_status_pair(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    reconcile_interval_hint: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    drain_interval_hint: int = DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Read-only redacted host status of BOTH owned unit pairs. Mutates nothing."""
    return {
        "action": "service-status",
        "agents": [
            service_status(
                os_home=os_home, mozyo_home=mozyo_home, interval_hint=reconcile_interval_hint,
                runner=runner, which=which, unit=RECONCILE_UNIT,
            ),
            service_status(
                os_home=os_home, mozyo_home=mozyo_home, interval_hint=drain_interval_hint,
                runner=runner, which=which, unit=DRAIN_UNIT,
            ),
        ],
    }


__all__ = (
    "SUPERVISOR_SYSTEMD_LABEL",
    "SUPERVISOR_DRAIN_SYSTEMD_LABEL",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_DRAIN_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "SERVICE_UNIT_NAME",
    "TIMER_UNIT_NAME",
    "DRAIN_SERVICE_UNIT_NAME",
    "DRAIN_TIMER_UNIT_NAME",
    "TIMERS_TARGET",
    "RUN_AT_LOAD_DELAY",
    "REASON_UNSUPPORTED_PLATFORM",
    "REASON_USER_MANAGER_UNAVAILABLE",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_SERVICE_NOT_LOADED",
    "REASON_NOT_INSTALLED",
    "REASON_HOME_PIN_UNHEALTHY",
    "REASON_HOME_PIN_MISMATCH",
    "REASON_INSTALLED_COMMAND_DRIFT",
    "REASON_DAEMON_RELOAD_FAILED",
    "REASON_ENABLE_FAILED",
    "REASON_RESTART_FAILED",
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
    "SupervisorUnit",
    "RECONCILE_UNIT",
    "DRAIN_UNIT",
    "SUPERVISOR_UNITS",
    "unit_dir",
    "service_unit_path",
    "timer_unit_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "format_exec_argv",
    "parse_exec_argv",
    "render_service_unit",
    "render_timer_unit",
    "classify_credential_readiness",
    "user_manager_available",
    "install",
    "restart",
    "uninstall",
    "service_status",
    "install_pair",
    "restart_pair",
    "uninstall_pair",
    "service_status_pair",
)
