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
- **An unconfigured Redmine does not block installing the timer.** This is the sharpest divergence
  from the macOS adapter and it is deliberate (acceptance: 「Redmine設定が未整備でもtimerの導入自体は
  拒否しない」). Credential readiness is *projected* as a fixed token, never used as an install gate:
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

import dataclasses
import os
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
# Owned identity.
#
# Two DISTINCT roots must never be conflated:
#   - the **OS user home** owns the unit files under the XDG user-unit directory — where the systemd
#     user manager looks, independent of any mozyo config;
#   - the **mozyo home** (``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``) owns the registry / store /
#     credential root the supervisor reads at run time.
#
# The domain service ``label`` stays the reverse-DNS id the declarative definition carries; the
# systemd **unit name** is the filesystem-safe realization of it (systemd reads a trailing ``.``
# segment as the unit type, so the label is not reused verbatim as a unit name).
# ---------------------------------------------------------------------------

SUPERVISOR_SYSTEMD_LABEL = DEFAULT_SUPERVISOR_SERVICE_LABEL

#: The XDG-relative directory the systemd **user** manager reads owned units from.
UNIT_DIR_RELATIVE = Path("systemd/user")
CONFIG_DIR_RELATIVE = Path(".config")

SERVICE_UNIT_NAME = "mozyo-bridge-callback-supervisor.service"
TIMER_UNIT_NAME = "mozyo-bridge-callback-supervisor.timer"

#: The OS tick cadence: how often systemd starts one bounded ``--run-once`` sweep (issue #15183).
#: This is the *local* cadence (SQLite + Herdr). It is NOT the Redmine cadence — the supervisor body
#: gates provider reads behind its own durable watermark, whose portable default is
#: :data:`DEFAULT_RECONCILIATION_INTERVAL_SECONDS` (300s), so a tick inside that window makes zero
#: provider reads. Raising this constant would make local work less prompt; it would NOT make
#: Redmine reads more frequent, and lowering the provider cadence is not this module's decision.
DEFAULT_TICK_INTERVAL_SECONDS = 60

#: The executable name resolved from PATH at install time (never a shell string).
SUPERVISOR_EXECUTABLE_NAME = "mozyo-bridge"
#: The structured argv tail each tick runs: one bounded sweep, then exit.
SUPERVISOR_ARGV_TAIL = ("workflow", "supervisor", "--run-once")
#: The structured flag pinning the mozyo home onto the unit argv (non-secret; a config directory).
SUPERVISOR_HOME_FLAG = "--home"

#: The systemd target a user timer installs into.
TIMERS_TARGET = "timers.target"
_SYSTEMCTL = "systemctl"
#: The ``[Timer]`` delay that runs one tick the moment the timer becomes active (on ``enable --now``
#: and on every later user-manager start).
RUN_AT_LOAD_DELAY = "0s"


@dataclasses.dataclass(frozen=True)
class SupervisorUnit:
    """The owned systemd user unit pair (one service + one timer) and the argv tail it runs."""

    label: str
    argv_tail: tuple[str, ...]
    service_unit: str
    timer_unit: str
    description: str
    default_interval_seconds: int


#: The single owned unit (issue #15183: one service + one timer, no second cadence).
SUPERVISOR_UNIT = SupervisorUnit(
    label=SUPERVISOR_SYSTEMD_LABEL,
    argv_tail=SUPERVISOR_ARGV_TAIL,
    service_unit=SERVICE_UNIT_NAME,
    timer_unit=TIMER_UNIT_NAME,
    description="mozyo-bridge callback supervisor bounded sweep",
    default_interval_seconds=DEFAULT_TICK_INTERVAL_SECONDS,
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

#: ``home_pin`` status vocabulary (see :func:`extract_pinned_home`).
HOME_PIN_OK = "ok"
HOME_PIN_MISSING = "missing"
HOME_PIN_DUPLICATE = "duplicate"
HOME_PIN_MALFORMED = "malformed"
#: Present but not an absolute, lexically-canonical path — a scheduled service resolves such a pin
#: from a different working directory than the installer's.
HOME_PIN_NOT_ABSOLUTE = "not_absolute"
HOME_PIN_NO_ARGV = "no_argv"
#: The owned unit exists but carries no single parseable command (distinct from absence).
HOME_PIN_UNREADABLE = "unreadable_unit"
HOME_PIN_NOT_INSTALLED = "not_installed"

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
# Paths + command resolution (pure; no host mutation, no secrets).
# ---------------------------------------------------------------------------


def unit_dir(os_home: Optional[Path] = None) -> Path:
    """The owned systemd **user** unit directory.

    With an explicit ``os_home`` (tests / an operator pinning a home) the directory is
    ``<os_home>/.config/systemd/user`` — the XDG default under that home. With no ``os_home`` the
    real user-manager search path is honoured: ``$XDG_CONFIG_HOME/systemd/user`` when that variable
    holds an absolute path, else ``~/.config/systemd/user``. Writing anywhere else would produce an
    install that ``systemctl --user`` cannot see — a silently unscheduled supervisor, which is the
    exact failure this adapter exists to remove.
    """
    if os_home is not None:
        return Path(os_home) / CONFIG_DIR_RELATIVE / UNIT_DIR_RELATIVE
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    config_root = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / CONFIG_DIR_RELATIVE
    return config_root / UNIT_DIR_RELATIVE


def service_unit_path(os_home: Optional[Path] = None) -> Path:
    """The owned ``.service`` unit path (the bounded one-shot command)."""
    return unit_dir(os_home) / SUPERVISOR_UNIT.service_unit


def timer_unit_path(os_home: Optional[Path] = None) -> Path:
    """The owned ``.timer`` unit path (the cadence that starts the one-shot)."""
    return unit_dir(os_home) / SUPERVISOR_UNIT.timer_unit


def resolve_mozyo_home(mozyo_home: Optional[Path] = None) -> Path:
    """Resolve the **mozyo home** root (credential / registry / store) as an absolute path.

    ``mozyo_home`` (the supervisor CLI's ``--home``) wins; otherwise the package home contract
    (``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``). An explicit value is normalized to an absolute
    canonical root: a relative / ``~`` value must never be pinned onto the unit argv, because a
    systemd-started process resolves it from its own working directory, not the installer's.
    """
    if mozyo_home is not None:
        return Path(mozyo_home).expanduser().resolve()
    return mozyo_bridge_home()


def resolve_supervisor_command(
    *,
    mozyo_home: Optional[Path] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[list[str]]:
    """The exact argv the scheduled unit runs, or ``None`` when the executable is not on PATH.

    The executable is PATH-resolved at install time (so the unit survives shell-env differences) and
    normalized to an absolute canonical path: a relative PATH entry makes ``shutil.which`` return a
    relative path, which systemd would resolve from its own working directory. The resolved mozyo
    home is pinned as ``--home <root>`` so the scheduled process reads the root the install resolved
    (systemd carries no ``MOZYO_BRIDGE_HOME`` from the installer's shell). A missing executable is a
    fail-closed condition the caller turns into a zero-mutation refusal — never a guessed path.
    """
    executable = which(SUPERVISOR_EXECUTABLE_NAME)
    if not executable:
        return None
    return [
        os.path.abspath(executable),
        *SUPERVISOR_UNIT.argv_tail,
        SUPERVISOR_HOME_FLAG,
        str(resolve_mozyo_home(mozyo_home)),
    ]


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

    The inverse of :func:`format_exec_argv`, tolerant of bare (unquoted) tokens so a hand-edited unit
    still reads back. It deliberately does **not** interpret systemd's ``-`` / ``@`` / ``:`` / ``!``
    command prefixes or ``%`` specifiers: this adapter never writes them, so a unit carrying one
    parses to a token that will not match the expected command and is reported as drift rather than
    being silently normalized away.
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


def render_service_unit(command: Sequence[str]) -> str:
    """Render the ``.service`` unit for the one-shot scheduled supervisor sweep.

    Structurally minimal and secret-free:

    - **No** ``Environment=`` / ``EnvironmentFile=`` key exists in the output, so no secret can be
      serialized in.
    - **No** ``Restart=`` and **no** ``RemainAfterExit=`` key: the command is a bounded sweep that
      exits and the ``.timer`` re-runs it. A restart directive on a one-shot would be a tight
      relaunch loop, so it is absent by design.
    - **No** ``[Install]`` section: the *timer* is what gets enabled. A directly enabled service
      would run once at login and never again, quietly replacing the cadence.
    - ``ExecStart`` is the exact structured argv. Output goes to the journal (systemd's default), so
      no owned log path is created and nothing is written outside the unit directory.
    """
    return "\n".join(
        (
            "[Unit]",
            f"Description={SUPERVISOR_UNIT.description}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={format_exec_argv(command)}",
            f"SyslogIdentifier={Path(SUPERVISOR_UNIT.service_unit).stem}",
            "",
        )
    )


def render_timer_unit(*, interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS) -> str:
    """Render the ``.timer`` unit that schedules the one-shot service.

    ``OnActiveSec=0s`` fires one tick the moment the timer becomes active — on ``enable --now`` and
    again on every later user-manager start. ``OnUnitActiveSec`` repeats it every
    ``interval_seconds`` after the last run (default 60s). ``AccuracySec=1s`` keeps the cadence
    honest instead of letting systemd coalesce it into a minute-wide window. No ``OnCalendar`` /
    ``Persistent=``: there is no missed run to replay, because the next tick reconciles whatever the
    last one missed.
    """
    return "\n".join(
        (
            "[Unit]",
            f"Description={SUPERVISOR_UNIT.description} timer",
            "",
            "[Timer]",
            f"Unit={SUPERVISOR_UNIT.service_unit}",
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
# Installed-unit reading (best-effort; never raises).
# ---------------------------------------------------------------------------


def _read_unit_keys(target: Path) -> Optional[dict[str, list[str]]]:
    """Parse an installed unit file into ``{key: [values]}``; ``None`` if unreadable.

    Section-flat on purpose: this adapter only asks "does key X exist / what is its value", and the
    owned units never reuse a key name across sections. Comments, section headers, and blank lines
    are dropped; a line without ``=`` is ignored rather than raising.
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
    """The argv an installed ``.service`` runs, or ``None`` when absent / unparseable / duplicated."""
    if not service_keys:
        return None
    values = service_keys.get("ExecStart") or []
    if len(values) != 1:
        return None  # absent, or several ExecStart lines: not a single effective argv
    return parse_exec_argv(values[0])


def _installed_interval_seconds(timer_keys: Optional[dict[str, list[str]]]) -> Optional[int]:
    """The cadence an installed ``.timer`` declares, or ``None`` when unreadable.

    Only the exact ``<N>s`` form this adapter writes is read back. A hand-edited ``5min`` is reported
    as an unknown cadence rather than re-interpreted: status projects what it can verify, and a
    cadence it cannot parse is not a cadence it should claim.
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


def extract_pinned_home(installed_argv: object) -> tuple[Optional[str], str]:
    """Extract the ``--home`` pin from an installed command's argv (strict).

    Returns ``(pinned_home, status)``. The installed unit — not the caller's current shell — is the
    authority on the root the scheduled process uses, so restart / status read the pin from here. A
    missing / duplicated / value-less pin is not trusted, and a pin that is not an absolute,
    lexically-canonical path is rejected too: systemd resolves such a pin from a different working
    directory than the installer. Every non-``ok`` case is surfaced, never guessed.
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
    if not os.path.isabs(value) or value != os.path.normpath(value):
        return None, HOME_PIN_NOT_ABSOLUTE
    return value, HOME_PIN_OK


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
_NEXT_ELAPSE_PROPERTIES = ("NextElapseUSecRealtime", "NextElapseUSecMonotonic")
_TIMER_PROPERTIES = (
    "ActiveState", "UnitFileState", "LastTriggerUSec", *_NEXT_ELAPSE_PROPERTIES,
)
_SERVICE_PROPERTIES = ("MainPID", "Result", "ExecMainStatus", "ExecMainExitTimestamp")

#: How to read a ``next_elapse`` value. A monotonic value is measured since boot, NOT a wall clock,
#: so a reader that assumed wall-clock time would misreport when the next tick runs.
NEXT_ELAPSE_REALTIME = "realtime"
NEXT_ELAPSE_MONOTONIC = "monotonic"
NEXT_ELAPSE_UNKNOWN = ""


def _next_elapse(timer_shown: dict[str, str]) -> tuple[str, str]:
    """``(value, basis)`` for the next scheduled run; ``("", "")`` when systemd reports neither."""
    for prop, basis in (
        (_NEXT_ELAPSE_PROPERTIES[0], NEXT_ELAPSE_REALTIME),
        (_NEXT_ELAPSE_PROPERTIES[1], NEXT_ELAPSE_MONOTONIC),
    ):
        value = (timer_shown.get(prop) or "").strip()
        if value:
            return value, basis
    return "", NEXT_ELAPSE_UNKNOWN


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
