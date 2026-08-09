"""Linux systemd **user** service + timer for the callback supervisor (Redmine #15183).

On Linux the bounded ``workflow supervisor --run-once`` body already worked, but nothing scheduled
it: retiring finished sublanes, delivering callbacks, and supplying durable state all fell back to a
human running the command. This module is the Linux realization of the service lifecycle contract
(``status`` / ``install`` / ``restart`` / ``uninstall``), and **nothing more**.

Design boundary (Redmine #15183 scope correction j#101996 — the issue body is the authority):

- **ONE owned service + ONE owned timer.** The timer starts ``workflow supervisor --run-once`` every
  60 seconds and the process exits each tick. This is deliberately *not* the macOS dual-agent shape:
  registering a separate ``--drain-only`` unit, installing two units atomically, and mirroring the
  LaunchAgent internals were all removed from the acceptance contract. The macOS adapter
  (:mod:`...application.supervisor_launchd`) is untouched by this module.
- **A 60s tick is not a 60s Redmine poll.** The supervisor body already gates provider reads behind a
  durable per-workspace cadence watermark (``reconcile_cadence`` / ``should_reconcile_source``)
  whose portable default is :data:`DEFAULT_RECONCILIATION_INTERVAL_SECONDS` (300s, with exponential
  backoff + jitter when passes come up empty). A tick inside that window is downgraded to a local
  pass with **zero** provider reads, so the frequent tick works from SQLite + Herdr and Redmine stays
  the low-frequency loss-recovery leg. That gating lives in the supervisor body, not here; this
  module only supplies the cadence and must not re-implement it.
- **No resident daemon.** ``Type=oneshot`` with **no** ``Restart=`` and **no** ``RemainAfterExit=``:
  a restart directive on a one-shot is a tight relaunch loop, so it is structurally absent, not set
  to a falsy value. ``OnActiveSec=0s`` runs one tick the moment the timer activates;
  ``OnUnitActiveSec=60s`` repeats it. No infinite wait, no in-process poll.
- **No secret in a unit.** The rendered units have **no** ``Environment=`` and no
  ``EnvironmentFile=`` key at all, so no code path can serialize a credential into one. A
  systemd-started supervisor inherits no interactive shell environment; the Redmine key/URL reach it
  through the daemon-trusted home-scoped credential file, never a unit. ``ExecStart`` is the exact
  PATH-resolved ``mozyo-bridge`` executable + structured argv, systemd-quoted per token — never a
  shell string, and never ``/bin/sh -c``.
- **An unconfigured Redmine does not block installing the timer** (acceptance: 「Redmine設定が未整備でも
  timerの導入自体は拒否しない」). This was the sharpest divergence from the macOS adapter until Redmine
  #15192 aligned that host to the same contract, so it is now shared rather than a divergence: the
  operator-visible answer to "can I install this?" no longer depends on the host (j#102151 Finding
  4). Credential readiness is *projected* as a fixed token, never used as an install gate:
  the local work a tick can safely do from SQLite + Herdr must keep running, and an unreachable
  Redmine surfaces as an explicit result rather than as a refusal to schedule anything at all.
- **Fail-closed, zero-mutation refusals** remain for the conditions that make the install itself
  meaningless: a non-Linux host, an unreachable systemd **user** manager (a container with no user
  bus is explicitly unsupported, not silently degraded), and a missing executable.
- **Structured systemctl only.** Every ``systemctl --user`` invocation is structured argv — no shell.

This module performs **no** Redmine fetch, gate progression, route resolution, or callback delivery:
installing / restarting / uninstalling the unit is orthogonal to what it does when it runs. It never
touches worktrees, branches, or the #15066 managed-process retirement boundary.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    normalize_base_url,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
# The pure unit-text layer (paths, argv resolution, quoting / specifier escaping, rendering,
# unit-file readback) lives in the sibling module so neither side exceeds the module-health line
# budget (review j#102069 F7). Everything is re-exported here, so this module remains the single
# import for the whole Linux adapter and no caller or test had to change.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_systemd_unit import (  # noqa: E501
    CONFIG_DIR_RELATIVE,
    DEFAULT_TICK_INTERVAL_SECONDS,
    HOME_PIN_DUPLICATE,
    HOME_PIN_MALFORMED,
    HOME_PIN_MISSING,
    HOME_PIN_NO_ARGV,
    HOME_PIN_NOT_ABSOLUTE,
    HOME_PIN_NOT_INSTALLED,
    HOME_PIN_OK,
    HOME_PIN_UNREADABLE,
    NEXT_ELAPSE_MONOTONIC,
    NEXT_ELAPSE_PROPERTIES,
    NEXT_ELAPSE_REALTIME,
    NEXT_ELAPSE_UNKNOWN,
    REASON_COMMAND_NOT_RENDERABLE,
    RUN_AT_LOAD_DELAY,
    SERVICE_UNIT_NAME,
    SUPERVISOR_ARGV_TAIL,
    SUPERVISOR_EXECUTABLE_NAME,
    SUPERVISOR_HOME_FLAG,
    SUPERVISOR_SYSTEMD_LABEL,
    SUPERVISOR_UNIT,
    TIMER_UNIT_NAME,
    TIMERS_TARGET,
    UNIT_DIR_RELATIVE,
    SupervisorUnit,
    extract_pinned_home,
    format_exec_argv,
    installed_command as _installed_command,
    installed_interval_seconds as _installed_interval_seconds,
    next_elapse as _next_elapse,
    parse_exec_argv,
    read_unit_keys as _read_unit_keys,
    render_service_unit,
    render_timer_unit,
    resolve_mozyo_home,
    resolve_supervisor_command,
    service_unit_path,
    timer_unit_path,
    unit_dir,
    unrenderable_argv_reason,
)

_SYSTEMCTL = "systemctl"
# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens (machine-readable; secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

#: A verb was refused because the host is not Linux (there is no systemd user manager to drive).
REASON_UNSUPPORTED_PLATFORM = "systemd_unsupported_platform"
#: A verb was refused because no systemd **user** manager is reachable — ``systemctl`` is absent, or
#: it cannot reach a user bus (the container / no-session case). Explicitly unsupported, never a
#: silent degrade to "installed but never scheduled".
REASON_USER_MANAGER_UNAVAILABLE = "systemd_user_manager_unavailable"
#: install/restart refused: the ``mozyo-bridge`` executable is not resolvable on PATH.
REASON_EXECUTABLE_NOT_FOUND = "supervisor_executable_not_found"
#: restart refused: no owned unit is installed (nothing to restart; run install first).
REASON_NOT_INSTALLED = "service_not_installed"
#: restart refused: the owned timer is not active, so nothing is scheduling this service. Bringing
#: it up is ``install``'s job, not ``restart``'s.
REASON_SERVICE_NOT_LOADED = "service_not_loaded"
#: restart/status: the installed ``--home`` pin is missing / malformed / duplicated / not an absolute
#: canonical path, so the root the scheduled process actually uses cannot be trusted.
REASON_HOME_PIN_UNHEALTHY = "home_pin_unhealthy"
#: restart refused: the requested mozyo home differs from the installed pin (a home change must go
#: through ``install``, which rewrites the unit — restart never silently re-points).
REASON_HOME_PIN_MISMATCH = "home_pin_mismatch"
#: restart refused: the installed command no longer matches what an install would write now
#: (executable moved / argv drift). Reinstall to change it; never re-run a drifted command.
REASON_INSTALLED_COMMAND_DRIFT = "installed_command_drift"
#: ``systemctl --user daemon-reload`` failed (message redacted to a fixed token).
REASON_DAEMON_RELOAD_FAILED = "systemctl_daemon_reload_failed"
#: ``systemctl --user enable --now <timer>`` failed (message redacted to a fixed token).
REASON_ENABLE_FAILED = "systemctl_enable_failed"
#: ``systemctl --user restart <service>`` failed (message redacted to a fixed token).
REASON_RESTART_FAILED = "systemctl_restart_failed"


#: Redmine credential-readiness tokens. On this adapter these are a **projection only** — never an
#: install gate (issue #15183: an unconfigured Redmine must not stop the timer from being installed).
CREDENTIAL_READY = "ready"  # api key + usable base url present
CREDENTIAL_INCOMPLETE = "incomplete"  # exactly one of key / usable url present
CREDENTIAL_MISSING = "missing"  # neither present, and nothing unsafe (plain unconfigured)
CREDENTIAL_UNSAFE = "unsafe"  # a present credential file is unsafe/malformed (permission / YAML)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# Credential readiness — PROJECTION ONLY on this adapter.
# ---------------------------------------------------------------------------


def classify_credential_readiness(*, mozyo_home: Optional[Path] = None) -> str:
    """Classify **daemon-effective** Redmine credential readiness into a fixed, secret-safe token.

    Judges what the *systemd-started* supervisor will actually have at run time, not what the
    installer's interactive shell holds: the units carry no environment block and a systemd service
    inherits no interactive shell, so readiness resolves with an **empty environ** against the
    resolved mozyo home. An installer's exported ``MOZYO_REDMINE_*`` therefore can never make this
    read ``ready``.

    On this adapter the result is **reported, never enforced**: issue #15183 requires that an
    unconfigured Redmine not block installing the timer, so that the local work a tick can safely do
    from SQLite + Herdr keeps running and an unreachable Redmine is an explicit projected state
    rather than a refusal to schedule anything. Returns only a token — never the key, the URL, or
    the resolver's warning text.
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
#: ASCII decimal inside that width (never ``str.isdigit()``, which accepts non-ASCII digits that are
#: not pids) keeps this projection's "never raises" promise — the defect class Redmine #14753
#: recorded against the launchd adapter's pid read.
_MAX_PID_DIGITS = len(str(2**31 - 1))


def _show(runner: Runner, unit_name: str, properties: Sequence[str]) -> dict[str, str]:
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


def _refused(action: str, reason: str, **extra: object) -> dict:
    """A fail-closed, zero-mutation refusal result (fixed vocabulary; no host detail)."""
    return {"action": action, "performed": False, "reason": reason, "label": SUPERVISOR_UNIT.label,
            **extra}


def _preflight(action: str, runner: Runner) -> Optional[dict]:
    """The host preflight every verb shares, or ``None`` when the host is usable."""
    if not _running_on_linux():
        return _refused(action, REASON_UNSUPPORTED_PLATFORM)
    if not user_manager_available(runner):
        return _refused(action, REASON_USER_MANAGER_UNAVAILABLE)
    return None


# ---------------------------------------------------------------------------
# Lifecycle verbs (structured results; fail-closed, zero-mutation refusals).
# ---------------------------------------------------------------------------


def install(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Write the owned service + timer and enable the timer. Idempotent; fail-closed.

    Refuses — before any filesystem write or systemctl call — only on the conditions that make the
    install itself meaningless: a non-Linux host, an unreachable systemd user manager, or a missing
    executable.

    A non-ready Redmine credential is **not** one of them (issue #15183). Readiness is resolved
    against the pinned mozyo home and reported as ``credential_readiness``, but the timer installs
    regardless, so the local work a tick can safely do from SQLite + Herdr keeps running and an
    unconfigured Redmine surfaces as an explicit state instead of a refusal to schedule anything.
    (This is the deliberate divergence from the macOS LaunchAgent adapter, which gates on it.)

    A failure *after* the units are written leaves them on disk and reports ``performed: False``; the
    operator can fix the cause and re-run install, which rewrites both units idempotently.
    """
    blocked = _preflight("install", runner)
    if blocked is not None:
        return blocked
    resolved_mozyo = resolve_mozyo_home(mozyo_home)
    command = resolve_supervisor_command(mozyo_home=resolved_mozyo, which=which)
    if command is None:
        return _refused("install", REASON_EXECUTABLE_NOT_FOUND)
    # A token that cannot live literally on one unit-file line would produce a DIFFERENT unit, not a
    # cosmetically odd one, so refuse before writing anything (review j#102053 F4).
    unrenderable = unrenderable_argv_reason(command)
    if unrenderable:
        return _refused("install", unrenderable)
    # Projected, NOT gated: an unconfigured Redmine must not stop the timer being installed.
    readiness = classify_credential_readiness(mozyo_home=resolved_mozyo)

    service_target = service_unit_path(os_home)
    timer_target = timer_unit_path(os_home)
    service_target.parent.mkdir(parents=True, exist_ok=True)
    service_target.write_text(render_service_unit(command), encoding="utf-8")
    timer_target.write_text(render_timer_unit(interval_seconds=interval_seconds), encoding="utf-8")

    # The manager must re-read the unit directory before the timer can be enabled from it.
    if _systemctl(runner, ["daemon-reload"]).returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_DAEMON_RELOAD_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
        }
    # ``enable --now`` is idempotent: it rewrites the timers.target want and starts the timer, which
    # (OnActiveSec=0s) runs the first bounded sweep immediately.
    if _systemctl(runner, ["enable", "--now", SUPERVISOR_UNIT.timer_unit]).returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_ENABLE_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
        }
    return {
        "action": "install",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
        "scheduled_interval_seconds": max(1, int(interval_seconds)),
        "label": SUPERVISOR_UNIT.label,
    }


def restart(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Re-run the bounded sweep now on the *scheduled* service. Fail-closed zero-mutation.

    The **installed unit** — not the caller's current shell — is the authority on the root the
    scheduled process uses: restart reads the ``--home`` pin from the owned ``.service`` and reports
    *that* root's readiness, so it never claims a state belonging to a different home.

    Refuses — before any systemctl mutation — on a non-Linux host, an unreachable user manager, no
    installed service unit, an owned unit with no single parseable ``ExecStart``, an unhealthy
    ``--home`` pin, a requested ``mozyo_home`` that differs from the pin, an installed command that
    no longer matches what an install would write now (drift — reinstall to change), a missing
    executable, or an owned timer that is not active. A non-ready credential does not block a
    restart, for the same reason it does not block an install.
    """
    blocked = _preflight("restart", runner)
    if blocked is not None:
        return blocked
    service_target = service_unit_path(os_home)
    if not service_target.exists():
        return _refused("restart", REASON_NOT_INSTALLED)
    installed_argv = _installed_command(_read_unit_keys(service_target))
    if installed_argv is None:
        # Present but unreadable / no single parseable ExecStart — unhealthy, NOT absence.
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=HOME_PIN_UNREADABLE)
    pinned, pin_status = extract_pinned_home(installed_argv)
    if pin_status != HOME_PIN_OK:
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=pin_status)
    # A requested home that disagrees with the installed pin is a re-point attempt — refuse; a home
    # change must rewrite the unit via install, not silently re-run the old pin.
    if mozyo_home is not None and str(resolve_mozyo_home(mozyo_home)) != pinned:
        return _refused("restart", REASON_HOME_PIN_MISMATCH, home_pin=pin_status)
    pinned_home = Path(pinned)
    expected = resolve_supervisor_command(mozyo_home=pinned_home, which=which)
    if expected is None:
        return _refused("restart", REASON_EXECUTABLE_NOT_FOUND)
    # If an install could not render this command, the installed unit cannot legitimately match it.
    unrenderable = unrenderable_argv_reason(expected)
    if unrenderable:
        return _refused("restart", unrenderable)
    if installed_argv != expected:
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT)
    readiness = classify_credential_readiness(mozyo_home=pinned_home)
    timer_active = _show(runner, SUPERVISOR_UNIT.timer_unit, ("ActiveState",)).get(
        "ActiveState"
    ) == "active"
    if not timer_active:
        return _refused(
            "restart", REASON_SERVICE_NOT_LOADED, credential_readiness=readiness
        )
    # ``restart`` on a one-shot stops any in-flight sweep and runs a fresh one. The timer's own
    # cadence is untouched.
    if _systemctl(runner, ["restart", SUPERVISOR_UNIT.service_unit]).returncode != 0:
        return {
            "action": "restart", "performed": False, "reason": REASON_RESTART_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
        }
    return {
        "action": "restart", "performed": True, "reason": "",
        "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
    }


def uninstall(*, os_home: Optional[Path] = None, runner: Runner = _default_runner) -> dict:
    """Disable + stop the owned timer/service and remove exactly the owned unit files.

    Refuses only on a non-Linux host or an unreachable user manager (there is nothing to disable).
    Otherwise it tears the unit down even when credentials are absent — you must be able to remove a
    service without them. Each systemctl step is best-effort (a not-enabled timer / not-running
    service is a normal idempotent case), then exactly the two owned files are removed and the
    manager is reloaded so the removal takes effect. No mozyo home is needed.

    The final ``reset-failed`` is not cosmetic. ``stop`` on a sweep that is mid-flight SIGTERMs it,
    so the one-shot exits on a signal and systemd records the unit as ``failed``; once the unit files
    are gone that record lingers in the manager as a ``not-found``/``failed`` entry that
    ``list-units`` still shows. Measured on a live user manager during the Redmine #15183
    installed-artifact smoke — a fake runner cannot observe manager-side state. launchd's ``bootout``
    leaves no such trace, so "removes exactly the owned artifacts" has to mean the manager holds no
    residue either, not just that the files are unlinked.
    """
    blocked = _preflight("uninstall", runner)
    if blocked is not None:
        return blocked
    _systemctl(runner, ["disable", "--now", SUPERVISOR_UNIT.timer_unit])
    _systemctl(runner, ["stop", SUPERVISOR_UNIT.service_unit])
    removed = False
    for target in (timer_unit_path(os_home), service_unit_path(os_home)):
        if target.exists():
            target.unlink()
            removed = True
    _systemctl(runner, ["daemon-reload"])
    _systemctl(
        runner, ["reset-failed", SUPERVISOR_UNIT.service_unit, SUPERVISOR_UNIT.timer_unit]
    )
    return {
        "action": "uninstall", "performed": True, "reason": "",
        "removed": removed, "label": SUPERVISOR_UNIT.label,
    }


#: The ``systemctl show`` properties the status projection reads. All are non-secret scalars.
#:
#: BOTH next-elapse properties are read, and that is not redundancy. systemd populates
#: ``NextElapseUSecRealtime`` only for calendar (``OnCalendar``) timers; a monotonic timer — which
#: this adapter's ``OnActiveSec`` / ``OnUnitActiveSec`` pair is — publishes
#: ``NextElapseUSecMonotonic`` and leaves the realtime one **empty**. Reading only the realtime
#: property reported a blank "next run" against a live, correctly-scheduled timer (measured during
#: the Redmine #15183 installed-artifact smoke). Reading both keeps the projection correct if the
#: cadence ever becomes calendar-based, and :data:`_NEXT_ELAPSE_PROPERTIES` fixes the preference
#: order so the answer is never silently empty.

#: The ``systemctl show`` properties the status projection reads. All are non-secret scalars. The
#: next-elapse pair is owned by the unit-text layer (see ``NEXT_ELAPSE_PROPERTIES``).
_TIMER_PROPERTIES = ("ActiveState", "UnitFileState", "LastTriggerUSec", *NEXT_ELAPSE_PROPERTIES)
_SERVICE_PROPERTIES = ("MainPID", "Result", "ExecMainStatus", "ExecMainExitTimestamp")

def service_status(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_hint: int = DEFAULT_TICK_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """A read-only, redacted projection of the host service state. Mutates nothing.

    Answers exactly what the acceptance contract asks an operator to be able to see without secrets:
    whether the unit is installed and enabled, **when it next runs** (``next_elapse``), **how the
    last run ended** (``last_result`` / ``last_exit_status`` / ``last_exit_at``), and **what it
    runs** (the scheduled interval, the ``--home`` pin health, and whether the installed command is
    still the one an install would write now). Everything is a boolean, count, fixed token, or
    timestamp — no credential value, no request header, no repo-local path.

    When the unit is installed and readable, ``credential_readiness`` is that of the **pinned** mozyo
    home (the root the scheduled process actually runs with), not the caller's current shell. An
    unhealthy pin — or an owned unit that exists but carries no single parseable ``ExecStart``
    (``home_pin`` = ``unreadable_unit``; distinct from absence, which is ``not_installed``) —
    surfaces as ``home_pin`` != ``ok`` with an empty readiness (unknowable). Only when nothing is
    installed is ``credential_readiness`` the would-be root's.
    """
    service_target = service_unit_path(os_home)
    timer_target = timer_unit_path(os_home)
    service_exists = service_target.exists()
    timer_exists = timer_target.exists()

    timer_shown = _show(runner, SUPERVISOR_UNIT.timer_unit, _TIMER_PROPERTIES)
    service_shown = _show(runner, SUPERVISOR_UNIT.service_unit, _SERVICE_PROPERTIES)

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
    run_at_load = (timer_keys.get("OnActiveSec") == [RUN_AT_LOAD_DELAY]) if timer_keys else None

    if installed_argv is not None:
        pinned, pin_status = extract_pinned_home(installed_argv)
        credential_readiness = (
            classify_credential_readiness(mozyo_home=Path(pinned))
            if pin_status == HOME_PIN_OK
            else ""
        )
        expected = (
            resolve_supervisor_command(mozyo_home=Path(pinned), which=which)
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

    scheduled_interval = _installed_interval_seconds(timer_keys)
    next_elapse_value, next_elapse_basis = _next_elapse(timer_shown)

    return {
        "action": "service-status",
        "label": SUPERVISOR_UNIT.label,
        "platform_supported": _running_on_linux(),
        "user_manager_available": user_manager_available(runner),
        # Installed only when BOTH owned units are present: a lone service has no cadence and a lone
        # timer has nothing to start.
        "installed": service_exists and timer_exists,
        "service_unit": SUPERVISOR_UNIT.service_unit,
        "timer_unit": SUPERVISOR_UNIT.timer_unit,
        "service_unit_exists": service_exists,
        "timer_unit_exists": timer_exists,
        "timer_enabled": timer_shown.get("UnitFileState") == "enabled",
        # ``loaded`` is the cross-adapter word for "the host manager is scheduling this service".
        "loaded": timer_shown.get("ActiveState") == "active",
        # When the next tick runs (acceptance: 次回起動), with the basis needed to read it: a
        # monotonic value is measured since boot, not a wall clock.
        "next_elapse": next_elapse_value,
        "next_elapse_basis": next_elapse_basis,
        # Wall-clock time of the last trigger — the operator-friendly companion to a monotonic
        # next-elapse, and the value that makes the observed cadence checkable.
        "last_trigger": timer_shown.get("LastTriggerUSec", ""),
        # How the last tick ended (acceptance: 直近の終了結果). ``Result`` is systemd's fixed
        # vocabulary (``success`` / ``exit-code`` / ``signal`` / ``timeout`` / ...).
        "last_result": service_shown.get("Result", ""),
        "last_exit_status": _int_or_none(service_shown.get("ExecMainStatus", "")),
        "last_exit_at": service_shown.get("ExecMainExitTimestamp", ""),
        "pid": _pid_or_none(service_shown.get("MainPID", "")),
        "scheduled_interval_seconds": (
            scheduled_interval
            if scheduled_interval is not None
            else (int(interval_hint) if not timer_exists else None)
        ),
        # The provider cadence the supervisor body enforces internally, surfaced so an operator can
        # see that a 60s tick is not a 60s Redmine poll. This adapter does not set or enforce it.
        "provider_reconcile_interval_seconds": DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
        "run_at_load": run_at_load,
        "keep_alive_present": keep_alive_present,
        "no_environment_block": no_environment_block,
        "home_pin": pin_status,
        "executable_matches": bool(
            expected is not None and isinstance(installed_argv, list) and installed_argv == expected
        ),
        # What it runs, as the exact argv (non-secret: an executable path + fixed flags + a config
        # directory). This is the "実行内容" the acceptance contract asks status to show.
        "installed_command": list(installed_argv) if installed_argv else [],
        "credential_readiness": credential_readiness,
    }


def _int_or_none(token: str) -> Optional[int]:
    """A small signed decimal, or ``None``. Never raises (see :data:`_MAX_PID_DIGITS` rationale)."""
    raw = (token or "").strip()
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if not (digits.isascii() and digits.isdigit() and len(digits) <= _MAX_PID_DIGITS):
        return None
    return -int(digits) if negative else int(digits)


def _pid_or_none(token: str) -> Optional[int]:
    """The owned one-shot's pid while a sweep is running, else ``None``. Never raises."""
    pid = _int_or_none(token)
    if pid is None or pid <= 0:  # systemd reports 0 for "no process right now"
        return None
    return pid


__all__ = (
    "SUPERVISOR_SYSTEMD_LABEL",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "SERVICE_UNIT_NAME",
    "TIMER_UNIT_NAME",
    "TIMERS_TARGET",
    "RUN_AT_LOAD_DELAY",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "REASON_UNSUPPORTED_PLATFORM",
    "REASON_USER_MANAGER_UNAVAILABLE",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_COMMAND_NOT_RENDERABLE",
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
    "SUPERVISOR_UNIT",
    "unit_dir",
    "service_unit_path",
    "timer_unit_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "format_exec_argv",
    "parse_exec_argv",
    "unrenderable_argv_reason",
    "NEXT_ELAPSE_REALTIME",
    "NEXT_ELAPSE_MONOTONIC",
    "NEXT_ELAPSE_UNKNOWN",
    "render_service_unit",
    "render_timer_unit",
    "classify_credential_readiness",
    "extract_pinned_home",
    "user_manager_available",
    "install",
    "restart",
    "uninstall",
    "service_status",
)
