"""Owned launchd identity + the pure plist text layer for the callback supervisor (Redmine #15192).

Split out of :mod:`...application.supervisor_launchd` so neither side exceeds the module-health
line budget, mirroring the split the Linux adapter already carries
(:mod:`...application.supervisor_systemd_unit`, review j#102069 F7). The division is the same one:
everything here is **pure** — owned identity, path resolution, argv resolution, plist rendering and
read-back, and the fixed vocabularies those produce. Nothing in this module runs ``launchctl``,
touches a credential, or mutates the host; the lifecycle verbs that do live in the sibling module.

Every name is re-exported from ``supervisor_launchd``, so that module remains the single import for
the whole macOS adapter and no caller or test had to change.
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
import shutil
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    DEFAULT_OS_TICK_INTERVAL_SECONDS,
    DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL,
    DEFAULT_SUPERVISOR_SERVICE_LABEL,
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
# Owned agent (Redmine #15192): exactly ONE. The retired ``--drain-only`` agent's identity is kept
# below purely so an upgrade can RECOGNIZE and remove what a pre-#15192 install left behind — it is a
# migration target, never something a verb installs.
# ---------------------------------------------------------------------------

#: The retired local-drain agent's owned identity (#14150, retired by #15192). Kept so
#: :func:`classify_legacy_drain` can tell "our old registration" from "a stranger's plist that
#: happens to sit at this path" — the removal fence needs both the path and the label.
SUPERVISOR_DRAIN_LAUNCHD_LABEL = DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL
DRAIN_PLIST_RELATIVE = Path("Library/LaunchAgents") / f"{SUPERVISOR_DRAIN_LAUNCHD_LABEL}.plist"
DRAIN_LOG_RELATIVE = Path("Library/Logs/mozyo-bridge/callback-supervisor-drain.log")
SUPERVISOR_DRAIN_ARGV_TAIL = ("workflow", "supervisor", "--drain-only")


@dataclasses.dataclass(frozen=True)
class SupervisorAgent:
    """One owned launchd agent's identity (label + plist/log paths + the bounded argv tail it runs)."""

    label: str
    argv_tail: tuple[str, ...]
    plist_relative: Path
    log_relative: Path
    default_interval_seconds: int


#: The single owned agent: one bounded ``workflow supervisor --run-once`` per tick, at the shared
#: portable OS cadence both host adapters register at (#15192).
SUPERVISOR_AGENT = SupervisorAgent(
    label=SUPERVISOR_LAUNCHD_LABEL,
    argv_tail=SUPERVISOR_ARGV_TAIL,
    plist_relative=PLIST_RELATIVE,
    log_relative=LOG_RELATIVE,
    default_interval_seconds=DEFAULT_OS_TICK_INTERVAL_SECONDS,
)
#: The owned agents an install/uninstall/status sweep manages. Exactly one since #15192; the tuple
#: shape is kept because the CLI renders an ``agents`` roster on every backend.
SUPERVISOR_AGENTS = (SUPERVISOR_AGENT,)

#: The retired drain agent, as a migration target only. Deliberately NOT in
#: :data:`SUPERVISOR_AGENTS`: no verb installs, restarts, or reports it as owned — ``install`` and
#: ``uninstall`` only *remove* it.
LEGACY_DRAIN_AGENT = SupervisorAgent(
    label=SUPERVISOR_DRAIN_LAUNCHD_LABEL,
    argv_tail=SUPERVISOR_DRAIN_ARGV_TAIL,
    plist_relative=DRAIN_PLIST_RELATIVE,
    log_relative=DRAIN_LOG_RELATIVE,
    default_interval_seconds=DEFAULT_OS_TICK_INTERVAL_SECONDS,
)

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


# ---------------------------------------------------------------------------
# Path + command + plist rendering (pure; no host mutation, no secrets).
# ---------------------------------------------------------------------------


def plist_path(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> Path:
    """The owned plist path under the **OS user home** (``~/Library/LaunchAgents``)."""
    return (os_home or Path.home()) / agent.plist_relative


def log_path(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> Path:
    """The owned log path under the **OS user home** (``~/Library/Logs``)."""
    return (os_home or Path.home()) / agent.log_relative


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
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> Optional[list[str]]:
    """The exact argv the agent runs, or ``None`` when the executable is not on PATH.

    The executable is PATH-resolved at install time (so the plist survives shell-env differences)
    and normalized to an **absolute canonical path** (``os.path.abspath``): a relative PATH entry
    makes ``shutil.which`` return a relative path, which a LaunchAgent would resolve from its own
    working directory rather than the installer's — the same cwd divergence closed for the ``--home``
    pin (j#79149 R5-F1). The **resolved mozyo home** is likewise pinned as ``--home <root>`` so the
    daemon reads the credential / registry root the preflight validated (j#79092 R2-F1). A missing
    executable is a fail-closed condition the caller turns into a zero-mutation refusal (install the
    package first) — never a shell string and never a guessed path.
    """
    executable = which(SUPERVISOR_EXECUTABLE_NAME)
    if not executable:
        return None
    return [
        os.path.abspath(executable),
        *agent.argv_tail,
        SUPERVISOR_HOME_FLAG,
        str(resolve_mozyo_home(mozyo_home)),
    ]


def render_plist(
    command: Sequence[str],
    *,
    interval_seconds: int,
    os_home: Optional[Path] = None,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
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
        "Label": agent.label,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "StartInterval": max(1, int(interval_seconds)),
        "StandardOutPath": str(log_path(os_home, agent=agent)),
        "StandardErrorPath": str(log_path(os_home, agent=agent)),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)




def read_installed_plist(target: Path) -> Optional[dict]:
    """Best-effort parse of the installed plist; ``None`` if unreadable/malformed (never raises)."""
    try:
        raw = target.read_bytes()
        parsed = plistlib.loads(raw)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_pinned_home(installed_argv: object) -> tuple[Optional[str], str]:
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


__all__ = (
    "SUPERVISOR_LAUNCHD_LABEL",
    "PLIST_RELATIVE",
    "LOG_RELATIVE",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "SUPERVISOR_DRAIN_LAUNCHD_LABEL",
    "DRAIN_PLIST_RELATIVE",
    "DRAIN_LOG_RELATIVE",
    "SUPERVISOR_DRAIN_ARGV_TAIL",
    "SupervisorAgent",
    "SUPERVISOR_AGENT",
    "SUPERVISOR_AGENTS",
    "LEGACY_DRAIN_AGENT",
    "HOME_PIN_OK",
    "HOME_PIN_MISSING",
    "HOME_PIN_DUPLICATE",
    "HOME_PIN_MALFORMED",
    "HOME_PIN_NOT_ABSOLUTE",
    "HOME_PIN_NO_ARGV",
    "HOME_PIN_UNREADABLE",
    "HOME_PIN_NOT_INSTALLED",
    "plist_path",
    "log_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "render_plist",
    "extract_pinned_home",
    "read_installed_plist",
)
