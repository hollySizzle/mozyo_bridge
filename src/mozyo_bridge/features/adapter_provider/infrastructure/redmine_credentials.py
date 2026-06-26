"""Daemon-trusted Redmine credential resolution (Redmine #12306).

The launchd-managed OTel receiver carries **no EnvironmentVariables block**
in its plist by construction (``application/otel_launchd.py`` safety
boundary), so a receiver started by launchd inherits none of the operator's
shell environment. Before this module the receiver read
``MOZYO_REDMINE_API_KEY`` / ``MOZYO_REDMINE_URL`` straight from
``os.environ`` only (review #56232), which meant a launchd-managed receiver
always degraded the cockpit's Redmine layer to ``unconfigured`` — the
documented follow-up in ``vibes/docs/logics/otel-event-store.md``.

This module is that follow-up: a secure, secrets-never-in-plist key/config
delivery path for the launchd case, resolved from **daemon-trusted sources
only**, in precedence order:

1. **Environment** (``MOZYO_REDMINE_API_KEY`` / ``MOZYO_REDMINE_URL``) —
   the interactive ``otel serve`` case where the operator exported the
   credentials in the launching shell. Highest precedence; unchanged
   behavior.
2. **Home-scoped credential file**
   (``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/redmine-credentials.yaml``) —
   the launchd delivery path. Per-field fallback when the matching env var
   is absent.

Credential boundary (preserves review #56232). The #56232 invariant is that
**repo-local files must never select where the API key is sent**. This file
does not weaken it: it is *home-scoped and user-owned*, daemon-trusted
exactly like the environment — never repo-local. A workspace's own
``workspace-defaults.yaml`` still only contributes a project identifier and
a host-match check (``redmine_context.read_redmine_project``); it can never
name the destination or supply the key.

Fail-closed and redaction posture (US #12306 acceptance conditions):

- **No secret ever leaves this module's value channel.** The
  :class:`RedmineCredentials` ``repr`` masks the key, and every warning /
  error string references only env-var names, the file path, and octal
  permission bits — never the credential value. Pin these with tests so a
  refactor cannot start leaking into launchd logs / journals.
- **Loose permissions fail closed.** A credential file that is group- or
  world-accessible, or not owned by the current user, is *refused* (treated
  as absent → ``unconfigured``) with a redacted warning, rather than read.
  This mirrors SSH ``StrictModes`` and ``~/.netrc`` handling.
- **Missing / malformed config degrades visibly**, never crashes: absent
  file + absent env yields ``(None, None)`` (the pre-existing
  ``unconfigured`` behavior); unreadable / malformed YAML yields the same
  with a redacted warning. An *invalid* key (present but rejected by
  Redmine) surfaces downstream as ``unavailable`` via the existing fetch
  path — also a visible degrade.

Nothing here touches the receiver's loopback-only bind; credential delivery
and network boundary are independent.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mozyo_bridge.redmine_context import API_KEY_ENV, BASE_URL_ENV
from mozyo_bridge.shared.paths import mozyo_bridge_home

CREDENTIALS_FILENAME = "redmine-credentials.yaml"

# A credential file must be readable/writable by its owner only. Any group
# or other permission bit set is a fail-closed condition.
_FORBIDDEN_PERM_BITS = 0o077

_MASK = "***"


def credentials_path(home: Path | None = None) -> Path:
    """Resolve the home-scoped Redmine credential file path.

    Follows the same ``(home or mozyo_bridge_home()) / name`` contract as
    the OTel store, so an explicit ``home`` (tests / alternate roots) and
    the ``MOZYO_BRIDGE_HOME`` env override behave identically here.
    """
    return (home or mozyo_bridge_home()) / CREDENTIALS_FILENAME


@dataclass(frozen=True)
class RedmineCredentials:
    """Resolved daemon credentials plus provenance and redacted warnings.

    ``api_key`` is the only secret field; :meth:`__repr__` masks it so the
    dataclass can never leak the value into a traceback, log line, or
    journal. ``source`` records where each field came from (``"env"`` /
    ``"file"`` / ``None``) for diagnostics. ``warnings`` are pre-redacted,
    human-actionable strings safe to print to a launchd log.
    """

    api_key: str | None = None
    base_url: str | None = None
    source: dict[str, str | None] = field(
        default_factory=lambda: {"api_key": None, "base_url": None}
    )
    warnings: tuple[str, ...] = ()

    def __repr__(self) -> str:  # never include the api_key value
        return (
            "RedmineCredentials("
            f"api_key={_MASK if self.api_key else None!r}, "
            f"base_url={self.base_url!r}, "
            f"source={self.source!r}, "
            f"warnings={self.warnings!r})"
        )


def _clean(value: object) -> str | None:
    """A non-empty, stripped string, or ``None`` for any other shape."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _permission_problem(path: Path) -> str | None:
    """Return a redacted reason the file must not be read, or ``None``.

    POSIX-only enforcement (launchd is macOS): a credential file must be a
    regular file, owned by the current user, with no group/other bits. The
    returned string names only the path and octal mode — never the value.
    """
    if not hasattr(os, "getuid"):  # pragma: no cover - non-POSIX
        return None
    try:
        info = path.stat()
    except OSError as exc:
        return f"cannot stat credential file {path} ({exc.__class__.__name__})"
    if not stat.S_ISREG(info.st_mode):
        return f"credential file {path} is not a regular file; ignoring it"
    if info.st_uid != os.getuid():
        return (
            f"credential file {path} is not owned by the current user; "
            "refusing to read it (chown it to yourself)"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & _FORBIDDEN_PERM_BITS:
        return (
            f"credential file {path} has insecure permissions {oct(mode)}; "
            "refusing to read it (run `chmod 600` on it)"
        )
    return None


def _read_file(path: Path) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Best-effort ``(api_key, base_url, warnings)`` from the credential file.

    Never raises and never echoes the credential value. A missing file is
    silent (the common ``unconfigured`` case); a present-but-unsafe or
    malformed file degrades to ``(None, None)`` with a redacted warning.
    """
    if not path.exists():
        return None, None, ()
    problem = _permission_problem(path)
    if problem is not None:
        return None, None, (problem,)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return (
            None,
            None,
            (
                f"credential file {path} is unreadable "
                f"({exc.__class__.__name__}); treating as unconfigured",
            ),
        )
    if raw is None:
        return None, None, ()
    if not isinstance(raw, dict):
        return (
            None,
            None,
            (
                f"credential file {path} must be a YAML mapping; "
                "treating as unconfigured",
            ),
        )
    section = raw.get("redmine")
    if not isinstance(section, dict):
        return (
            None,
            None,
            (
                f"credential file {path} has no `redmine:` mapping; "
                "treating as unconfigured",
            ),
        )
    return _clean(section.get("api_key")), _clean(section.get("url")), ()


def resolve_redmine_credentials(
    home: Path | None = None,
    *,
    environ: "os._Environ[str] | dict[str, str] | None" = None,
) -> RedmineCredentials:
    """Resolve daemon Redmine credentials from trusted sources.

    Per-field precedence: environment first (operator-explicit shell
    export), then the home-scoped credential file (the launchd path). The
    file is only consulted for a field the environment did not supply, and
    is read at most once. ``environ`` is injectable for hermetic tests;
    it defaults to ``os.environ``.
    """
    env = os.environ if environ is None else environ
    env_key = _clean(env.get(API_KEY_ENV))
    env_url = _clean(env.get(BASE_URL_ENV))

    file_key: str | None = None
    file_url: str | None = None
    warnings: tuple[str, ...] = ()
    # Only touch the filesystem when the environment leaves a gap, so the
    # pure-env case pays nothing and cannot be perturbed by a stray file.
    if env_key is None or env_url is None:
        file_key, file_url, warnings = _read_file(credentials_path(home))

    api_key = env_key if env_key is not None else file_key
    base_url = env_url if env_url is not None else file_url
    source = {
        "api_key": "env" if env_key else ("file" if file_key else None),
        "base_url": "env" if env_url else ("file" if file_url else None),
    }
    return RedmineCredentials(
        api_key=api_key,
        base_url=base_url,
        source=source,
        warnings=warnings,
    )
