"""Platform-neutral core shared by every supervisor OS scheduler adapter (Redmine #15183).

The canonical design (``vibes/docs/logics/ticket-system-neutral-orchestrator.md`` ``### OS scheduler
adapter``) defines LaunchAgent / systemd timer / cron as adapters that start the **same** bounded
one-shot command. Everything in that contract which is not launchd- or systemd-specific lives here,
so a fix reaches both adapters instead of drifting between two copies:

- the executable / argv / ``--home`` pin the scheduled command runs (:func:`resolve_supervisor_command`);
- the **daemon-effective** mozyo home resolution (:func:`resolve_mozyo_home`) and the credential
  readiness it implies (:func:`classify_credential_readiness`) — judged with an **empty environ**,
  because no scheduler adapter carries the installer's shell environment into the scheduled process;
- the ``--home`` pin extraction / health vocabulary (:func:`extract_pinned_home`) that makes the
  *installed* unit — never the caller's shell — the authority on the daemon's root;
- the fixed-vocabulary refusal tokens and the zero-mutation refusal shape (:func:`refused`).

This module performs **no** host mutation and holds **no** platform branch: an adapter decides what
"installed" means on its host, and calls in here for the parts that are the same everywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    normalize_base_url,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    resolve_redmine_credentials,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

# ---------------------------------------------------------------------------
# The bounded one-shot command every adapter schedules (never a shell string).
# ---------------------------------------------------------------------------

#: The executable name resolved from PATH at install time (never a shell string).
SUPERVISOR_EXECUTABLE_NAME = "mozyo-bridge"
#: The structured argv tail the coarse provider-reconciliation tick runs (one bounded sweep, exit).
SUPERVISOR_ARGV_TAIL = ("workflow", "supervisor", "--run-once")
#: The structured argv tail the finer local-drain tick runs (Redmine #14150; zero provider reads).
SUPERVISOR_DRAIN_ARGV_TAIL = ("workflow", "supervisor", "--drain-only")
#: The structured flag that pins the mozyo home root onto the daemon argv (non-secret; a config
#: directory, resolved by the supervisor CLI's ``--home``).
SUPERVISOR_HOME_FLAG = "--home"

# ---------------------------------------------------------------------------
# Fixed-vocabulary reason tokens (machine-readable; secret-safe; UI-language-independent).
# ---------------------------------------------------------------------------

#: install/restart refused: the `mozyo-bridge` executable is not resolvable on PATH.
REASON_EXECUTABLE_NOT_FOUND = "supervisor_executable_not_found"
#: restart refused: no owned unit/plist is installed (nothing to restart; run install first).
REASON_NOT_INSTALLED = "service_not_installed"
#: restart refused: the installed service is not currently loaded / scheduled by the host manager
#: (launchd: not bootstrapped; systemd: the owned timer is not active). Restart acts only on a
#: service the host manager is already running — bringing one up is ``install``'s job.
REASON_SERVICE_NOT_LOADED = "service_not_loaded"
#: restart/status: the installed ``--home`` pin is missing / malformed / duplicated / not an absolute
#: canonical path, so the daemon-effective root cannot be trusted (fail-closed for restart;
#: unhealthy for status). Also used when the owned unit file exists but is unreadable / malformed.
REASON_HOME_PIN_UNHEALTHY = "home_pin_unhealthy"
#: restart refused: the requested mozyo home differs from the installed pin (a home change must go
#: through ``install``, which rewrites the unit — restart never silently re-points).
REASON_HOME_PIN_MISMATCH = "home_pin_mismatch"
#: restart refused: the installed command no longer matches what an install would write now
#: (executable moved / argv drift). Reinstall to change it; never restart a drifted command.
REASON_INSTALLED_COMMAND_DRIFT = "installed_command_drift"

#: ``home_pin`` extraction status vocabulary (see :func:`extract_pinned_home`).
HOME_PIN_OK = "ok"
HOME_PIN_MISSING = "missing"
HOME_PIN_DUPLICATE = "duplicate"
HOME_PIN_MALFORMED = "malformed"
#: The pin value is present but not an absolute, lexically-canonical path (relative / ``~`` / has
#: ``..`` etc.) — a scheduled daemon resolves it from a different cwd than the installer.
HOME_PIN_NOT_ABSOLUTE = "not_absolute"
HOME_PIN_NO_ARGV = "no_argv"
#: The owned unit file exists but could not be parsed / carries no command (distinct from absence,
#: which is ``not_installed``).
HOME_PIN_UNREADABLE = "unreadable_plist"
HOME_PIN_NOT_INSTALLED = "not_installed"

#: Credential-readiness tokens (the exact readiness the live supervisor needs to reach Redmine).
CREDENTIAL_READY = "ready"  # api key + usable base url present
CREDENTIAL_INCOMPLETE = "incomplete"  # exactly one of key / usable url present
CREDENTIAL_MISSING = "missing"  # neither present, and nothing unsafe (the plain unconfigured case)
CREDENTIAL_UNSAFE = "unsafe"  # a present credential file is unsafe/malformed (permission / YAML)

#: The install/restart refusal reason for each non-ready credential state.
CREDENTIAL_REFUSAL_REASON = {
    CREDENTIAL_INCOMPLETE: "redmine_credential_incomplete",
    CREDENTIAL_MISSING: "redmine_credential_missing",
    CREDENTIAL_UNSAFE: "redmine_credential_unsafe",
}

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run a structured argv with captured output; never a shell string."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# Path + command resolution (pure; no host mutation, no secrets).
# ---------------------------------------------------------------------------


def resolve_mozyo_home(mozyo_home: Optional[Path] = None) -> Path:
    """Resolve the exact **mozyo home** root (credential / registry / store) as an absolute path.

    ``mozyo_home`` (the supervisor CLI's ``--home``) wins; otherwise the package's home contract
    (:func:`mozyo_bridge_home`: ``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``). An explicit value is
    ``expanduser().resolve()``-normalized to an **absolute canonical root** — a relative / ``~``
    input must never be pinned onto the daemon argv, since a scheduled service's working directory
    is not the installer shell's, so a relative pin would re-diverge the credential / registry root
    (j#79125 R3-F2). ``mozyo_bridge_home()`` already returns an absolute resolved path.
    """
    if mozyo_home is not None:
        return Path(mozyo_home).expanduser().resolve()
    return mozyo_bridge_home()


def resolve_supervisor_command(
    *,
    argv_tail: Sequence[str],
    mozyo_home: Optional[Path] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[list[str]]:
    """The exact argv a scheduled tick runs, or ``None`` when the executable is not on PATH.

    The executable is PATH-resolved at install time (so the installed unit survives shell-env
    differences) and normalized to an **absolute canonical path** (``os.path.abspath``): a relative
    PATH entry makes ``shutil.which`` return a relative path, which a scheduled service would
    resolve from its own working directory rather than the installer's (j#79149 R5-F1). The
    **resolved mozyo home** is likewise pinned as ``--home <root>`` so the daemon reads the
    credential / registry root the preflight validated (j#79092 R2-F1). A missing executable is a
    fail-closed condition the caller turns into a zero-mutation refusal (install the package first)
    — never a shell string and never a guessed path.
    """
    executable = which(SUPERVISOR_EXECUTABLE_NAME)
    if not executable:
        return None
    return [
        os.path.abspath(executable),
        *[str(part) for part in argv_tail],
        SUPERVISOR_HOME_FLAG,
        str(resolve_mozyo_home(mozyo_home)),
    ]


# ---------------------------------------------------------------------------
# Credential readiness (the exact readiness the live supervisor needs; secret-safe token only).
# ---------------------------------------------------------------------------


def classify_credential_readiness(*, mozyo_home: Optional[Path] = None) -> str:
    """Classify **daemon-effective** Redmine credential readiness into a fixed, secret-safe token.

    Judges what the *scheduler-managed* supervisor will actually have at run time, not what the
    installer's interactive shell happens to hold. Two independent leaks are closed:

    - **shell key/URL** — no adapter writes an environment block, and a scheduled process inherits
      no interactive shell environment, so readiness resolves with an **empty environ**: an
      installer's exported ``MOZYO_REDMINE_*`` can never produce a false ``ready`` (j#79059 F1).
    - **shell home root** — the credential file's root is the resolved **mozyo home**
      (:func:`resolve_mozyo_home`), the exact root pinned onto the daemon argv, not whatever
      ``mozyo_bridge_home()`` a later scheduled process (with no ``MOZYO_BRIDGE_HOME``) would
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
# Installed-command inspection + refusal shape.
# ---------------------------------------------------------------------------


def extract_pinned_home(installed_argv: object) -> tuple[Optional[str], str]:
    """Extract the ``--home`` pin from an installed command's argv (strict).

    Returns ``(pinned_home, status)``. The installed unit — not the caller's current shell — is the
    authority on the daemon's mozyo home, so restart / status read the pin from here (j#79125 R3-F1).
    A missing / duplicated / value-less pin is *not* trusted (the daemon-effective root is
    unknowable), and a pin that is not an **absolute, lexically-canonical** path (relative / ``~`` /
    containing ``..``) is rejected too: a scheduled service resolves such a pin from a different
    working directory than the installer, re-opening the R3-F2 divergence in the installed service
    (j#79136 R4-F1). Every non-``ok`` case is surfaced (fail-closed for restart, unhealthy for
    status), never guessed.
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
    # (relative, ``~``, ``/a/../b``) would be resolved from the scheduler's cwd, not the installer's.
    if not os.path.isabs(value) or value != os.path.normpath(value):
        return None, HOME_PIN_NOT_ABSOLUTE
    return value, HOME_PIN_OK


def refused(action: str, reason: str, **extra: object) -> dict:
    """A fail-closed, zero-mutation refusal result (fixed vocabulary; no host detail)."""
    return {"action": action, "performed": False, "reason": reason, **extra}


def first_failure_reason(results: Sequence[dict]) -> str:
    """The reason token of the first non-performed unit (secret-safe), or '' when all performed."""
    for r in results:
        if not r.get("performed"):
            return str(r.get("reason", ""))
    return ""


__all__ = (
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_DRAIN_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "REASON_EXECUTABLE_NOT_FOUND",
    "REASON_NOT_INSTALLED",
    "REASON_SERVICE_NOT_LOADED",
    "REASON_HOME_PIN_UNHEALTHY",
    "REASON_HOME_PIN_MISMATCH",
    "REASON_INSTALLED_COMMAND_DRIFT",
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
    "CREDENTIAL_REFUSAL_REASON",
    "Runner",
    "default_runner",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "classify_credential_readiness",
    "extract_pinned_home",
    "refused",
    "first_failure_reason",
)
