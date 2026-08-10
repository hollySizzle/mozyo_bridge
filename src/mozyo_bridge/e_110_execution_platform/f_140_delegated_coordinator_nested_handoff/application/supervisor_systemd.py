"""Linux systemd user service+timer lifecycle for the callback supervisor (#15183/#15192).
One timer starts the bounded ``workflow supervisor --run-once`` oneshot at the shared OS cadence.
No drain-only unit, daemon, shell, credential, environment file, ``Restart=``, or ``RemainAfterExit=``
exists. Credential readiness is projected; provider cadence belongs to the supervisor body.
Mutating verbs use pinned files, a cooperative lock, and typed manager attestation; they do no
Redmine work, route progression, callback delivery, worktree change, or process retirement.
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
# The pure unit-text layer (paths, argv resolution, quoting / specifier escaping, rendering and
# parsing pinned bytes) lives in the sibling module. Host file access lives in the separate pinned
# filesystem seam. Everything public is re-exported here, so callers still use one Linux adapter.
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
    parse_unit_keys as _parse_unit_keys,
    render_service_unit,
    render_timer_unit,
    resolve_mozyo_home,
    resolve_supervisor_command,
    service_unit_path,
    timer_unit_path,
    unit_dir,
    unrenderable_argv_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_systemd_fs import (  # noqa: E501
    UNIT_ABSENT,
    UNIT_OWNED,
    UNIT_UNREADABLE,
    OwnedUnitPathError,
    UnsafeUnitArtifactError,
    acquire_lifecycle_lock,
    read_units as _read_units,
    unlink_units as _unlink_units,
    write_units as _write_units,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_scheduler_lifecycle_lock import (  # noqa: E501
    SchedulerLifecycleLockBusy,
    SchedulerLifecycleLockError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_systemd_manager import (  # noqa: E501
    MANAGER_DEFINITION_DRIFT,
    SystemdManagerInspector,
    UNINSTALL_DISABLE_FAILED,
    UNINSTALL_RELOAD_FAILED,
    UNINSTALL_RESET_FAILED,
    UNINSTALL_STOP_FAILED,
    disk_definition_matches,
    run_systemctl as _systemctl,
    run_uninstall_manager_sequence,
    show_properties as _show,
    user_manager_available as _user_manager_available,
)

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
#: restart refused: the owned service's run state could not be READ (an unreachable manager, or a
#: reply this parser cannot resolve — e.g. one that answers `ActiveState` twice with different
#: values). Distinct from ``service_not_loaded`` because the facts differ: one says it is not
#: running, this one says we cannot tell (review j#102383 finding r8f2). The token is **shared with
#: the macOS adapter** and names no OS-specific manager noun, because the backend declares one
#: operator-visible meaning per verb (review j#102398 finding r9f2).
REASON_SERVICE_STATE_UNREADABLE = "service_state_unreadable"
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
#: ``systemctl --user disable --now <timer>`` failed.  The timer may have been changed before the
#: command reported failure, so uninstall reports an uncertain effect and keeps the unit files.
REASON_DISABLE_FAILED = "systemctl_disable_failed"
#: ``systemctl --user restart <service>`` failed (message redacted to a fixed token).
REASON_RESTART_FAILED = "systemctl_restart_failed"
#: A unit path or artifact could not be identified as a regular, singly-linked owned entry. This is
#: a zero-mutation refusal when observed during preflight and remains a typed failure if action-time
#: revalidation detects drift after a manager call (review j#102843 finding r15f1).
REASON_UNIT_UNREADABLE = "systemd_unit_unreadable"
#: A pinned, identified write/removal failed. Host paths and exception strings are not projected.
REASON_UNIT_WRITE_FAILED = "systemd_unit_write_failed"
REASON_UNIT_REMOVAL_FAILED = "systemd_unit_removal_failed"
#: Another cooperating scheduler lifecycle operation owns the shared fence.
REASON_LIFECYCLE_BUSY = "scheduler_lifecycle_busy"
REASON_LIFECYCLE_LOCK_UNREADABLE = "scheduler_lifecycle_lock_unreadable"
#: Typed manager state could not be read without trusting raw text.
REASON_MANAGER_DEFINITION_UNREADABLE = "manager_effective_definition_unreadable"
#: The loaded definition differs from the exact owned command/home/path/timer contract.
REASON_MANAGER_DEFINITION_DRIFT = "manager_effective_definition_drift"
REASON_START_FAILED = "systemctl_start_failed"
REASON_STOP_FAILED = "systemctl_stop_failed"
REASON_RESET_FAILED = "systemctl_reset_failed"

EFFECT_NONE = "none"
EFFECT_PARTIAL = "partial"
EFFECT_UNCERTAIN = "uncertain"
EFFECT_COMPLETE = "complete"


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


def user_manager_available(runner: Runner = _default_runner) -> bool:
    """Whether the systemd user manager answers a read-only version probe."""
    return _user_manager_available(runner)


#: The widest process id ``systemctl show`` can print. DERIVED, not chosen: Linux ``pid_t`` is a
#: signed 32-bit integer, so ten digits covers every value the kernel can assign. Reading it as an
#: ASCII decimal inside that width (never ``str.isdigit()``, which accepts non-ASCII digits that are
#: not pids) keeps this projection's "never raises" promise — the defect class Redmine #14753
#: recorded against the launchd adapter's pid read.
_MAX_PID_DIGITS = len(str(2**31 - 1))


#: Whether the host manager's answer could be READ, in the vocabulary the macOS adapter publishes
#: (review j#102200 finding r3f2). The same three tokens on both hosts, so `probe_state` means one
#: thing in the common status contract. The drift guard is a test, not an import, so neither OS
#: adapter has to import the other.
PROBE_LOADED = "loaded"
PROBE_CONFIRMED_ABSENT = "confirmed_absent"
PROBE_UNREADABLE = "unreadable"


#: ``ActiveState`` values that mean the manager IS running this unit. ``reloading`` belongs here:
#: systemd defines it as active while reloading its configuration, not as stopped.
_ACTIVE_STATES = frozenset({"active", "reloading"})
#: ``ActiveState`` values that positively mean the unit is NOT running. Only these two are a
#: confirmed absence; everything else is either mid-transition or a value this code does not know.
_ABSENT_STATES = frozenset({"inactive", "failed"})
#: Mid-transition values. Documented by systemd, but neither running nor confirmed stopped — the
#: answer is "ask again", which is exactly what unreadable means to the callers of this projection.
_TRANSITIONAL_STATES = frozenset({"activating", "deactivating"})


def _probe_state(shown: dict[str, str], *, manager_available: bool) -> str:
    """Classify the host manager read into the shared three-token vocabulary.

    ``systemctl show`` answers for a unit it does not know (with empty / inactive values), so an
    EMPTY mapping here does not mean "no such unit" — it means the read itself failed, which is the
    unreadable case. An unreachable user manager is unreadable for the same reason (j#102200 r3f2),
    and so is an absent or empty ``ActiveState``: reading *some* property is not reading the
    *schedule state* (j#102235 r4f2).

    The classification is a **closed vocabulary**, not "active versus everything else" (review
    j#102309 finding r5f2). That open negation reported ``reloading`` — which systemd defines as
    active — plus both transition states and any value this code has never heard of, as
    ``confirmed_absent``: an assertion that the unit is definitely not running, made about states
    where it may well be. Absence is now claimed only for the two values that actually mean it, and
    anything unrecognized falls to :data:`PROBE_UNREADABLE`.

    Note the symmetry with the macOS side: there the rule is "do not fold unknown into *absent*",
    and here it is the same rule pointing the other way — do not fold unknown into any *confirmed*
    state. A value systemd adds in a future release must read as "I do not know", never as a fact.

    That is why the value is compared **exactly**: no case folding, no trimming. Each normalization
    reopened the vocabulary this function closes. Folding case let ``INACTIVE`` read as a confirmed
    absence and ``ACTIVE`` as a confirmed run (review j#102327 finding r6f2); trimming let
    ``ActiveState= inactive `` do the same (review j#102378 finding r7f2). Neither spelling is a
    token systemd's D-Bus interface enumerates — upstream lists ``ActiveState`` as bare lowercase
    literals — so both are values this code has not been told the meaning of, which is exactly the
    unknown case. The justification offered for the trim (that the whitespace was framing this
    parser had introduced) did not hold: ``splitlines`` removes the terminator, and everything after
    the first ``=`` is the manager's answer.
    """
    if not manager_available:
        return PROBE_UNREADABLE
    active_state = shown.get("ActiveState") or ""
    if not active_state:
        return PROBE_UNREADABLE
    if active_state in _ACTIVE_STATES:
        return PROBE_LOADED
    if active_state in _ABSENT_STATES:
        return PROBE_CONFIRMED_ABSENT
    # Transitional and unknown alike: not a state this projection may assert as fact.
    return PROBE_UNREADABLE


def _refused(action: str, reason: str, **extra: object) -> dict:
    """A fixed-vocabulary refusal; callers override ``effect_state`` after prior effects."""
    return {
        "action": action, "performed": False, "reason": reason,
        "label": SUPERVISOR_UNIT.label, "effect_state": EFFECT_NONE, **extra,
    }


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
    """Install under the shared lifecycle lock with typed manager attestation."""
    blocked = _preflight("install", runner)
    if blocked is not None:
        return blocked
    command = resolve_supervisor_command(mozyo_home=mozyo_home, which=which)
    if command is None:
        return _refused("install", REASON_EXECUTABLE_NOT_FOUND)
    unrenderable = unrenderable_argv_reason(command)
    if unrenderable:
        return _refused("install", unrenderable)
    inspector = SystemdManagerInspector(runner)
    if not inspector.capability_available():
        return _refused("install", REASON_MANAGER_DEFINITION_UNREADABLE)
    try:
        lifecycle = acquire_lifecycle_lock(os_home)
    except SchedulerLifecycleLockBusy:
        return _refused("install", REASON_LIFECYCLE_BUSY)
    except (SchedulerLifecycleLockError, OwnedUnitPathError, OSError):
        return _refused("install", REASON_LIFECYCLE_LOCK_UNREADABLE)
    with lifecycle:
        return _install_locked(
            os_home=os_home, mozyo_home=mozyo_home, interval_seconds=interval_seconds,
            runner=runner, which=which, inspector=inspector,
        )


def _install_locked(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    inspector: Optional[SystemdManagerInspector] = None,
) -> dict:
    """Write, enable-without-reload, reload, attest, then start under the held lock."""
    blocked = _preflight("install", runner)
    if blocked is not None:
        return blocked
    inspector = inspector or SystemdManagerInspector(runner)
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

    # Inspect both names before the first mutation. A symlink/hardlink/device at either owned name,
    # or a symlinked ancestor, proves no artifact identity and refuses without writing or calling a
    # mutating systemctl verb. The writer repeats the same check on its pinned directory fd at the
    # action point (review j#102843 finding r15f1).
    units = _read_units(os_home)
    if UNIT_UNREADABLE in (units.service_state, units.timer_state):
        return _refused("install", REASON_UNIT_UNREADABLE)
    timer_shown = _show(runner, SUPERVISOR_UNIT.timer_unit, ("ActiveState",))
    timer_state = _probe_state(timer_shown, manager_available=True)
    if timer_state == PROBE_UNREADABLE:
        return _refused(
            "install", REASON_SERVICE_STATE_UNREADABLE,
            credential_readiness=readiness, probe_state=timer_state,
        )
    if timer_state == PROBE_LOADED:
        if _read_units(os_home) != units:
            return _refused(
                "install", REASON_INSTALLED_COMMAND_DRIFT,
                credential_readiness=readiness, probe_state=timer_state,
            )
        stopped = _systemctl(runner, ["stop", SUPERVISOR_UNIT.timer_unit])
        if stopped.returncode != 0:
            return {
                "action": "install", "performed": False, "reason": REASON_STOP_FAILED,
                "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
                "probe_state": timer_state, "effect_state": EFFECT_UNCERTAIN,
            }
    if _read_units(os_home) != units:
        return _refused(
            "install", REASON_INSTALLED_COMMAND_DRIFT,
            credential_readiness=readiness, probe_state=timer_state,
            effect_state=EFFECT_PARTIAL if timer_state == PROBE_LOADED else EFFECT_NONE,
        )
    expected_service_payload = render_service_unit(command).encode("utf-8")
    expected_timer_payload = render_timer_unit(
        interval_seconds=interval_seconds
    ).encode("utf-8")
    try:
        _write_units(
            expected_service_payload,
            expected_timer_payload,
            os_home,
        )
    except (UnsafeUnitArtifactError, OwnedUnitPathError):
        return _refused("install", REASON_UNIT_UNREADABLE, effect_state=EFFECT_PARTIAL)
    except OSError:
        return _refused("install", REASON_UNIT_WRITE_FAILED, effect_state=EFFECT_PARTIAL)

    # `enable` normally reloads implicitly.  Suppress that unobservable second consumption and make
    # the one explicit reload below the definition load this invocation attests.
    if _systemctl(
        runner, ["enable", "--no-reload", SUPERVISOR_UNIT.timer_unit]
    ).returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_ENABLE_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
            "effect_state": EFFECT_PARTIAL,
        }
    if _systemctl(runner, ["daemon-reload"]).returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_DAEMON_RELOAD_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
            "effect_state": EFFECT_PARTIAL,
        }
    effect_units = _read_units(os_home)
    if UNIT_UNREADABLE in (effect_units.service_state, effect_units.timer_state):
        return _refused(
            "install", REASON_UNIT_UNREADABLE, credential_readiness=readiness,
            effect_state=EFFECT_PARTIAL,
        )
    if (
        effect_units.service_state != UNIT_OWNED
        or effect_units.timer_state != UNIT_OWNED
        or effect_units.service_payload != expected_service_payload
        or effect_units.timer_payload != expected_timer_payload
    ):
        return _refused(
            "install", REASON_INSTALLED_COMMAND_DRIFT, credential_readiness=readiness,
            effect_state=EFFECT_PARTIAL,
        )
    attestation = inspector.attest(
        service_unit=SUPERVISOR_UNIT.service_unit,
        timer_unit=SUPERVISOR_UNIT.timer_unit,
        service_path=service_unit_path(os_home),
        timer_path=timer_unit_path(os_home),
        expected_argv=command,
        interval_seconds=interval_seconds,
    )
    if not attestation.matched:
        return _refused(
            "install",
            REASON_MANAGER_DEFINITION_DRIFT
            if attestation.state == MANAGER_DEFINITION_DRIFT
            else REASON_MANAGER_DEFINITION_UNREADABLE,
            credential_readiness=readiness, effect_state=EFFECT_PARTIAL,
        )
    if _systemctl(runner, ["start", SUPERVISOR_UNIT.timer_unit]).returncode != 0:
        return {
            "action": "install", "performed": False, "reason": REASON_START_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
            "effect_state": EFFECT_UNCERTAIN,
        }
    return {
        "action": "install",
        "performed": True,
        "reason": "",
        "credential_readiness": readiness,
        "scheduled_interval_seconds": max(1, int(interval_seconds)),
        "label": SUPERVISOR_UNIT.label,
        "effect_state": EFFECT_COMPLETE,
    }


def restart(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> dict:
    """Restart only after disk and manager-effective definitions agree under one lock."""
    blocked = _preflight("restart", runner)
    if blocked is not None:
        return blocked
    units = _read_units(os_home)
    if UNIT_UNREADABLE in (units.service_state, units.timer_state):
        return _refused("restart", REASON_UNIT_UNREADABLE)
    if units.service_state == UNIT_ABSENT:
        return _refuse_missing_restart(runner)
    inspector = SystemdManagerInspector(runner)
    if not inspector.capability_available():
        return _refused("restart", REASON_MANAGER_DEFINITION_UNREADABLE)
    try:
        lifecycle = acquire_lifecycle_lock(os_home)
    except SchedulerLifecycleLockBusy:
        return _refused("restart", REASON_LIFECYCLE_BUSY)
    except (SchedulerLifecycleLockError, OwnedUnitPathError, OSError):
        return _refused("restart", REASON_LIFECYCLE_LOCK_UNREADABLE)
    with lifecycle:
        return _restart_locked(
            os_home=os_home, mozyo_home=mozyo_home, runner=runner, which=which,
            inspector=inspector,
        )


def _restart_locked(
    *,
    os_home: Optional[Path] = None,
    mozyo_home: Optional[Path] = None,
    runner: Runner = _default_runner,
    which: Callable[[str], Optional[str]] = shutil.which,
    inspector: Optional[SystemdManagerInspector] = None,
) -> dict:
    """Re-run only when pinned disk and typed manager-effective definitions both match."""
    blocked = _preflight("restart", runner)
    if blocked is not None:
        return blocked
    inspector = inspector or SystemdManagerInspector(runner)
    units = _read_units(os_home)
    if UNIT_UNREADABLE in (units.service_state, units.timer_state):
        return _refused("restart", REASON_UNIT_UNREADABLE)
    if units.service_state == UNIT_ABSENT:
        return _refuse_missing_restart(runner)
    installed_argv = _installed_command(_parse_unit_keys(units.service_payload))
    if installed_argv is None:
        # Present but unreadable / no single parseable ExecStart — unhealthy, NOT absence.
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=HOME_PIN_UNREADABLE)
    pinned, pin_status = extract_pinned_home(installed_argv)
    if pin_status != HOME_PIN_OK:
        return _refused("restart", REASON_HOME_PIN_UNHEALTHY, home_pin=pin_status)
    if mozyo_home is not None and str(resolve_mozyo_home(mozyo_home)) != pinned:
        return _refused("restart", REASON_HOME_PIN_MISMATCH, home_pin=pin_status)
    pinned_home = Path(pinned)
    expected = resolve_supervisor_command(mozyo_home=pinned_home, which=which)
    if expected is None:
        return _refused("restart", REASON_EXECUTABLE_NOT_FOUND)
    unrenderable = unrenderable_argv_reason(expected)
    if unrenderable:
        return _refused("restart", unrenderable)
    if installed_argv != expected:
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT)
    installed_interval = _installed_interval_seconds(
        _parse_unit_keys(units.timer_payload)
    )
    if installed_interval is None:
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT)
    # Parsed argv/cadence are necessary but not sufficient authority.  A stable extra directive
    # (for example Environment=, an Exec* hook, or a second timer trigger) must not be restarted
    # merely because the two fields above still match.  Exact renderer bytes are the closed disk
    # schema this adapter owns; manager-effective fields are checked separately below.
    if (
        units.service_state != UNIT_OWNED
        or units.timer_state != UNIT_OWNED
        or not disk_definition_matches(
            units.service_payload,
            units.timer_payload,
            expected_argv=expected,
            interval_seconds=installed_interval,
        )
    ):
        return _refused("restart", REASON_INSTALLED_COMMAND_DRIFT)
    readiness = classify_credential_readiness(mozyo_home=pinned_home)
    timer_shown = _show(runner, SUPERVISOR_UNIT.timer_unit, ("ActiveState",))
    timer_state = _probe_state(timer_shown, manager_available=True)
    if timer_state != PROBE_LOADED:
        return _refused(
            "restart",
            REASON_SERVICE_NOT_LOADED
            if timer_state == PROBE_CONFIRMED_ABSENT
            else REASON_SERVICE_STATE_UNREADABLE,
            credential_readiness=readiness,
            probe_state=timer_state,
        )
    action_units = _read_units(os_home)
    if UNIT_UNREADABLE in (action_units.service_state, action_units.timer_state):
        return _refused(
            "restart",
            REASON_UNIT_UNREADABLE,
            credential_readiness=readiness,
            probe_state=timer_state,
        )
    action_argv = _installed_command(_parse_unit_keys(action_units.service_payload))
    action_home, action_pin_status = extract_pinned_home(action_argv)
    if (
        action_units.service_state != units.service_state
        or action_units.timer_state != units.timer_state
        or action_units.service_payload != units.service_payload
        or action_units.timer_payload != units.timer_payload
        or action_argv != expected
        or action_pin_status != HOME_PIN_OK
        or action_home != pinned
    ):
        return _refused(
            "restart",
            REASON_INSTALLED_COMMAND_DRIFT,
            credential_readiness=readiness,
            probe_state=timer_state,
        )
    action_interval = _installed_interval_seconds(
        _parse_unit_keys(action_units.timer_payload)
    )
    if action_interval is None:
        return _refused(
            "restart", REASON_INSTALLED_COMMAND_DRIFT,
            credential_readiness=readiness, probe_state=timer_state,
        )
    attestation = inspector.attest(
        service_unit=SUPERVISOR_UNIT.service_unit,
        timer_unit=SUPERVISOR_UNIT.timer_unit,
        service_path=service_unit_path(os_home),
        timer_path=timer_unit_path(os_home),
        expected_argv=expected,
        interval_seconds=action_interval,
    )
    if not attestation.matched:
        return _refused(
            "restart",
            REASON_MANAGER_DEFINITION_DRIFT
            if attestation.state == MANAGER_DEFINITION_DRIFT
            else REASON_MANAGER_DEFINITION_UNREADABLE,
            credential_readiness=readiness, probe_state=timer_state,
        )
    final_units = _read_units(os_home)
    if final_units != action_units:
        return _refused(
            "restart", REASON_INSTALLED_COMMAND_DRIFT,
            credential_readiness=readiness, probe_state=timer_state,
        )
    if _systemctl(runner, ["restart", SUPERVISOR_UNIT.service_unit]).returncode != 0:
        return {
            "action": "restart", "performed": False, "reason": REASON_RESTART_FAILED,
            "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
            "effect_state": EFFECT_UNCERTAIN,
        }
    return {
        "action": "restart", "performed": True, "reason": "",
        "credential_readiness": readiness, "label": SUPERVISOR_UNIT.label,
        "effect_state": EFFECT_COMPLETE,
    }


def _refuse_missing_restart(runner: Runner) -> dict:
    """Preserve the shared manager-state reason when no disk unit can authorize an effect."""
    shown = _show(runner, SUPERVISOR_UNIT.timer_unit, ("ActiveState",))
    state = _probe_state(shown, manager_available=True)
    if state == PROBE_LOADED:
        return _refused("restart", REASON_NOT_INSTALLED, probe_state=state)
    reason = (
        REASON_SERVICE_NOT_LOADED
        if state == PROBE_CONFIRMED_ABSENT
        else REASON_SERVICE_STATE_UNREADABLE
    )
    return _refused("restart", reason, probe_state=state)


def uninstall(*, os_home: Optional[Path] = None, runner: Runner = _default_runner) -> dict:
    """Uninstall under the same cooperating-writer lifecycle fence."""
    blocked = _preflight("uninstall", runner)
    if blocked is not None:
        return blocked
    try:
        lifecycle = acquire_lifecycle_lock(os_home)
    except SchedulerLifecycleLockBusy:
        return _refused("uninstall", REASON_LIFECYCLE_BUSY, removed=False)
    except (SchedulerLifecycleLockError, OwnedUnitPathError, OSError):
        return _refused("uninstall", REASON_LIFECYCLE_LOCK_UNREADABLE, removed=False)
    with lifecycle:
        return _uninstall_locked(os_home=os_home, runner=runner)


def _uninstall_locked(*, os_home: Optional[Path] = None, runner: Runner = _default_runner) -> dict:
    """Disable, remove the two owned unit files, and clear manager residue."""
    blocked = _preflight("uninstall", runner)
    if blocked is not None:
        return blocked
    units = _read_units(os_home)
    if UNIT_UNREADABLE in (units.service_state, units.timer_state):
        return _refused("uninstall", REASON_UNIT_UNREADABLE, removed=False)
    try:
        outcome = run_uninstall_manager_sequence(
            runner,
            timer_unit=SUPERVISOR_UNIT.timer_unit,
            service_unit=SUPERVISOR_UNIT.service_unit,
            remove_units=lambda: _unlink_units(os_home),
        )
    except UnsafeUnitArtifactError:
        return _refused(
            "uninstall", REASON_UNIT_UNREADABLE, removed=None, effect_state=EFFECT_PARTIAL
        )
    except (OwnedUnitPathError, OSError):
        return _refused(
            "uninstall", REASON_UNIT_REMOVAL_FAILED, removed=None,
            effect_state=EFFECT_PARTIAL,
        )
    if not outcome.completed:
        reason = {
            UNINSTALL_DISABLE_FAILED: REASON_DISABLE_FAILED,
            UNINSTALL_STOP_FAILED: REASON_STOP_FAILED,
            UNINSTALL_RELOAD_FAILED: REASON_DAEMON_RELOAD_FAILED,
            UNINSTALL_RESET_FAILED: REASON_RESET_FAILED,
        }[outcome.phase]
        return _refused(
            "uninstall", reason, removed=outcome.removed,
            effect_state=(
                EFFECT_UNCERTAIN if outcome.removed is False else EFFECT_PARTIAL
            ),
        )
    return {
        "action": "uninstall", "performed": True, "reason": "",
        "removed": outcome.removed, "label": SUPERVISOR_UNIT.label,
        "effect_state": EFFECT_COMPLETE,
    }


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
    units = _read_units(os_home)
    service_exists = units.service_state != UNIT_ABSENT
    timer_exists = units.timer_state != UNIT_ABSENT

    manager_available = user_manager_available(runner)
    timer_shown = _show(runner, SUPERVISOR_UNIT.timer_unit, _TIMER_PROPERTIES)
    probe_state = _probe_state(timer_shown, manager_available=manager_available)
    service_shown = _show(runner, SUPERVISOR_UNIT.service_unit, _SERVICE_PROPERTIES)

    service_keys = (
        _parse_unit_keys(units.service_payload)
        if units.service_state == UNIT_OWNED
        else None
    )
    timer_keys = (
        _parse_unit_keys(units.timer_payload)
        if units.timer_state == UNIT_OWNED
        else None
    )
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
        "user_manager_available": manager_available,
        # Whether the host manager's answer could be READ, in the same fixed vocabulary the macOS
        # adapter publishes (review j#102200 finding r3f2): a failed read must not be reported as a
        # confirmed state. The timer is the unit that answers "is anything scheduling this".
        "probe_state": probe_state,
        # Installed only when BOTH owned units are present: a lone service has no cadence and a lone
        # timer has nothing to start.
        "installed": units.service_state == UNIT_OWNED and units.timer_state == UNIT_OWNED,
        "service_unit": SUPERVISOR_UNIT.service_unit,
        "timer_unit": SUPERVISOR_UNIT.timer_unit,
        "service_unit_exists": service_exists,
        "timer_unit_exists": timer_exists,
        "service_unit_state": units.service_state,
        "timer_unit_state": units.timer_state,
        # Exact, like `probe_state`: `UnitFileState` is another enumerated systemd token, so an
        # unrecognized spelling means "not the state we can name", never "enabled".
        "timer_enabled": timer_shown.get("UnitFileState") == "enabled",
        # ``loaded`` is the cross-adapter word for "the host manager is scheduling this service",
        # derived from the SAME classification as ``probe_state`` so the two cannot disagree about
        # one state machine (review j#102309 finding r5f2): reading `active` here while the state
        # token said otherwise is exactly the drift that finding recorded.
        "loaded": probe_state == PROBE_LOADED,
        # When the next tick runs (acceptance: 次回起動), with the basis needed to read it: a
        # monotonic value is measured since boot, not a wall clock.
        "next_elapse": next_elapse_value,
        "next_elapse_basis": next_elapse_basis,
        # Wall-clock time of the last trigger — the operator-friendly companion to a monotonic
        # next-elapse, and the value that makes the observed cadence checkable. These three are
        # DISPLAY values, so tidying them is a formatting choice made here, at the point of display
        # — deliberately not in `_show`, where the same trim would widen the state vocabulary the
        # migration fence depends on (review j#102378 finding r7f2).
        "last_trigger": timer_shown.get("LastTriggerUSec", "").strip(),
        # How the last tick ended (acceptance: 直近の終了結果). ``Result`` is systemd's fixed
        # vocabulary (``success`` / ``exit-code`` / ``signal`` / ``timeout`` / ...).
        "last_result": service_shown.get("Result", "").strip(),
        "last_exit_status": _int_or_none(service_shown.get("ExecMainStatus", "")),
        "last_exit_at": service_shown.get("ExecMainExitTimestamp", "").strip(),
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
        # Publish the trusted EXPECTED argv only when the installed unit matches it exactly. The
        # owned filename does not make arbitrary ExecStart tokens safe to echo; drift can contain a
        # secret-shaped flag and must remain absent from repr / JSON (review j#102843 r15f3).
        "installed_command": (
            list(expected)
            if expected is not None
            and isinstance(installed_argv, list)
            and installed_argv == expected
            else []
        ),
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
    "PROBE_LOADED",
    "PROBE_CONFIRMED_ABSENT",
    "PROBE_UNREADABLE",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_COMMAND_NOT_RENDERABLE",
    "REASON_SERVICE_NOT_LOADED",
    "REASON_SERVICE_STATE_UNREADABLE",
    "REASON_NOT_INSTALLED",
    "REASON_HOME_PIN_UNHEALTHY",
    "REASON_HOME_PIN_MISMATCH",
    "REASON_INSTALLED_COMMAND_DRIFT",
    "REASON_DAEMON_RELOAD_FAILED",
    "REASON_DISABLE_FAILED",
    "REASON_ENABLE_FAILED",
    "REASON_START_FAILED",
    "REASON_STOP_FAILED",
    "REASON_LIFECYCLE_BUSY", "REASON_LIFECYCLE_LOCK_UNREADABLE",
    "REASON_MANAGER_DEFINITION_UNREADABLE", "REASON_MANAGER_DEFINITION_DRIFT",
    "EFFECT_NONE", "EFFECT_PARTIAL", "EFFECT_UNCERTAIN", "EFFECT_COMPLETE",
    "REASON_RESTART_FAILED",
    "REASON_RESET_FAILED",
    "REASON_UNIT_UNREADABLE",
    "REASON_UNIT_WRITE_FAILED",
    "REASON_UNIT_REMOVAL_FAILED",
    "UNIT_ABSENT",
    "UNIT_OWNED",
    "UNIT_UNREADABLE",
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
