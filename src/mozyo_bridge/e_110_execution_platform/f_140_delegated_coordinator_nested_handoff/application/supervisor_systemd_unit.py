"""Pure unit-text layer for the Linux systemd supervisor adapter (Redmine #15183).

Everything here answers "what exactly goes in the unit files, and what does an installed unit say"
— path resolution, argv resolution, systemd quoting / specifier escaping, unit rendering, and
unit-file readback. All of it is **pure or read-only**: no ``systemctl``, no host mutation, no
credential resolution. The lifecycle verbs that drive the host manager live in the sibling
:mod:`...application.supervisor_systemd`, which re-exports every public name below so the adapter's
surface is a single import for callers.

Split out of that module to keep both sides under the module-health line budget
(``vibes/docs/logics/module-health-gate.md``; review j#102069 Finding 7). The seam is the natural
responsibility boundary, not an arbitrary cut: this file is the part that can be reasoned about and
tested without a systemd user manager anywhere in sight.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    DEFAULT_SUPERVISOR_SERVICE_LABEL,
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
# Vocabulary owned by the text layer (secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

#: install refused: a resolved argv token cannot be represented literally on a single unit-file line
#: (a newline / carriage return / other C0 control character). No escaping makes it safe, so writing
#: the unit would produce a *different* unit rather than an odd-looking one (review j#102053 F4).
REASON_COMMAND_NOT_RENDERABLE = "supervisor_command_not_renderable"

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

#: How to read a ``next_elapse`` value. A monotonic value is measured since boot, NOT a wall clock,
#: so a reader that assumed wall-clock time would misreport when the next tick runs.
NEXT_ELAPSE_REALTIME = "realtime"
NEXT_ELAPSE_MONOTONIC = "monotonic"
NEXT_ELAPSE_UNKNOWN = ""

#: The ``systemctl show`` timer properties whose next-elapse value this layer interprets. BOTH are
#: read deliberately: systemd populates ``NextElapseUSecRealtime`` only for calendar timers, and this
#: adapter's ``OnActiveSec`` / ``OnUnitActiveSec`` pair is monotonic, so the realtime one is empty on
#: a live timer (measured, Redmine #15183 smoke).
NEXT_ELAPSE_PROPERTIES = ("NextElapseUSecRealtime", "NextElapseUSecMonotonic")

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


def unrenderable_argv_reason(command: Sequence[str]) -> str:
    """``""`` when every token can be pinned literally, else the token-level reason it cannot.

    A unit file is line-based, so a value carrying a newline / carriage return does not produce a
    "weird path" — it produces a **different unit**: the tail lands on its own line and is parsed as
    another directive (or silently dropped). Other C0 control characters are equally untrustworthy
    to round-trip. There is no escape that makes them safe inside an ``ExecStart`` value, so this is
    a fail-closed condition the caller turns into a zero-mutation refusal rather than writing a
    corrupt unit and reporting success. Measured boundary re-check requested by review j#102053
    Finding 4: the earlier "unambiguous for any path" claim only held for spaces and quotes.
    """
    for arg in command:
        text = str(arg)
        if any(ch == "\n" or ch == "\r" or (ord(ch) < 0x20) or ord(ch) == 0x7F for ch in text):
            return REASON_COMMAND_NOT_RENDERABLE
    return ""


def format_exec_argv(command: Sequence[str]) -> str:
    """Render argv as a systemd ``ExecStart`` value: one double-quoted, escaped token per argument.

    Three separate escaping duties, each load-bearing:

    - **whitespace** — systemd splits ``ExecStart`` on whitespace, so an unquoted path containing a
      space would silently become two arguments. Every token is double-quoted.
    - **quotes / backslashes** — escaped as ``\\"`` / ``\\\\`` so the quoting itself round-trips.
    - **percent** — a literal ``%`` is written ``%%``. This is NOT cosmetic: ``ExecStart`` resolves
      systemd *specifiers*, so an unescaped ``%h`` in an executable or ``--home`` path is expanded
      by systemd at load time. Measured on a live user manager (review j#102053 Finding 4): a unit
      whose ``ExecStart`` read ``"/opt/%h/mozyo-bridge" "--home" "/tmp/%h"`` was reported by
      ``systemctl show`` as ``argv[]=/opt//home/holly/mozyo-bridge --home /tmp//home/holly`` — a
      different executable and a different mozyo home than the unit's literal text. Quoting does
      not suppress specifier expansion; only ``%%`` does. Without this, the pin is not a pin, and
      ``executable_matches`` compares the file's literal text and reports ``True`` while systemd
      execs something else.

    This is a *value*, never a shell string: systemd execs the argv directly, with no ``/bin/sh``.
    Callers must reject :func:`unrenderable_argv_reason` tokens first — this function assumes the
    command is renderable.
    """
    parts = []
    for arg in command:
        escaped = (
            str(arg).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        )
        parts.append(f'"{escaped}"')
    return " ".join(parts)


def parse_exec_argv(value: str) -> Optional[list[str]]:
    """Parse a rendered ``ExecStart`` value back into argv, or ``None`` when it is not parseable.

    The inverse of :func:`format_exec_argv`, tolerant of bare (unquoted) tokens so a hand-edited
    unit still reads back. Two deliberate refusals to guess:

    - systemd's ``-`` / ``@`` / ``:`` / ``!`` **command prefixes** are not interpreted. This adapter
      never writes them, so a unit carrying one parses to a token that will not match the expected
      command and is reported as drift rather than being normalized away.
    - an **unresolvable specifier** makes the whole readback untrustworthy. ``%%`` is un-escaped back
      to a literal ``%`` (the exact inverse of the renderer), but a *lone* ``%x`` is something
      systemd will expand into a value only systemd knows, so the argv in the file is not the argv
      that runs. Returning ``None`` makes status report ``unreadable_unit`` / ``executable_matches``
      false and makes restart fail closed — never a confident comparison against text whose runtime
      meaning we cannot reproduce (review j#102053 Finding 4).
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
    if not argv:
        return None
    resolved: list[str] = []
    for tok in argv:
        literal = _resolve_percent(tok)
        if literal is None:
            return None  # a specifier we cannot resolve -> the whole readback is untrustworthy
        resolved.append(literal)
    return resolved


def _resolve_percent(token: str) -> Optional[str]:
    """``%%`` -> literal ``%``; ``None`` when a lone specifier (``%h`` etc.) remains."""
    out: list[str] = []
    index = 0
    while index < len(token):
        ch = token[index]
        if ch != "%":
            out.append(ch)
            index += 1
            continue
        if index + 1 < len(token) and token[index + 1] == "%":
            out.append("%")
            index += 2
            continue
        return None  # `%` followed by anything else (or nothing) is a specifier / malformed
    return "".join(out)


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
# Installed-unit reading (best-effort; never raises).
# ---------------------------------------------------------------------------


def read_unit_keys(target: Path) -> Optional[dict[str, list[str]]]:
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


def installed_command(service_keys: Optional[dict[str, list[str]]]) -> Optional[list[str]]:
    """The argv an installed ``.service`` runs, or ``None`` when absent / unparseable / duplicated."""
    if not service_keys:
        return None
    values = service_keys.get("ExecStart") or []
    if len(values) != 1:
        return None  # absent, or several ExecStart lines: not a single effective argv
    return parse_exec_argv(values[0])


def installed_interval_seconds(timer_keys: Optional[dict[str, list[str]]]) -> Optional[int]:
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




def next_elapse(timer_shown: dict) -> tuple[str, str]:
    """``(value, basis)`` for the next scheduled run; ``("", "")`` when systemd reports neither."""
    for prop, basis in (
        (NEXT_ELAPSE_PROPERTIES[0], NEXT_ELAPSE_REALTIME),
        (NEXT_ELAPSE_PROPERTIES[1], NEXT_ELAPSE_MONOTONIC),
    ):
        value = (timer_shown.get(prop) or "").strip()
        if value:
            return value, basis
    return "", NEXT_ELAPSE_UNKNOWN


__all__ = (
    "UNIT_DIR_RELATIVE",
    "CONFIG_DIR_RELATIVE",
    "SERVICE_UNIT_NAME",
    "TIMER_UNIT_NAME",
    "TIMERS_TARGET",
    "RUN_AT_LOAD_DELAY",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "SUPERVISOR_SYSTEMD_LABEL",
    "SupervisorUnit",
    "SUPERVISOR_UNIT",
    "REASON_COMMAND_NOT_RENDERABLE",
    "HOME_PIN_OK",
    "HOME_PIN_MISSING",
    "HOME_PIN_DUPLICATE",
    "HOME_PIN_MALFORMED",
    "HOME_PIN_NOT_ABSOLUTE",
    "HOME_PIN_NO_ARGV",
    "HOME_PIN_UNREADABLE",
    "HOME_PIN_NOT_INSTALLED",
    "NEXT_ELAPSE_REALTIME",
    "NEXT_ELAPSE_MONOTONIC",
    "NEXT_ELAPSE_UNKNOWN",
    "NEXT_ELAPSE_PROPERTIES",
    "unit_dir",
    "service_unit_path",
    "timer_unit_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "unrenderable_argv_reason",
    "format_exec_argv",
    "parse_exec_argv",
    "render_service_unit",
    "render_timer_unit",
    "read_unit_keys",
    "installed_command",
    "installed_interval_seconds",
    "extract_pinned_home",
    "next_elapse",
)
