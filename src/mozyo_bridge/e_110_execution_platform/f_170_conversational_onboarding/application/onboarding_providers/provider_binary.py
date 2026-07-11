"""Trusted resolution of the conversation-provider CLI executable (#13497 j#74942).

Executable identity is an **outer boundary**: the production binding must not
leave "which `claude` runs" to the action-time ambient ``PATH`` (nor to the model
/ tool sandbox). This resolves the provider CLI to a verified absolute executable
*before* any conversation turn, mirroring the trusted herdr-binary resolver
(#13496):

- an explicit, trusted override via the named :data:`CLAUDE_BINARY_ENV` boundary
  must itself be **absolute** (a relative override is refused, never resolved
  against the cwd) and is verified as a regular executable;
- otherwise the binary is searched on the **trusted** ``PATH`` only — a ``PATH``
  with any empty or relative component is refused (a shell would resolve those
  against the cwd), and a bare name never falls back to an ambient ``PATH``;
- the executable bit is verified against the symlink-resolved ``realpath``, and
  the **realpath** (not a symlink-preserving abspath) is what is returned as
  ``argv[0]`` — so provider identity is deterministic across cwd and symlinks;
- the ``PATH`` search collects the **distinct** executable realpaths: zero →
  missing, one → resolved, two or more → ambiguous fail-closed (first-match is
  not an ambiguity check).

Every failure raises :class:`ConversationProviderError`
(``conversation_provider_unavailable``) so the caller mutates nothing and never
spawns a subprocess against an unverified path (Redmine #13497 j#74942 / j#74946).
"""

from __future__ import annotations

import os
from typing import Mapping

from ...domain.conversation_port import (
    PROVIDER_UNAVAILABLE,
    ConversationProviderError,
)

__all__ = (
    "CLAUDE_BINARY_ENV",
    "DEFAULT_CLAUDE_BINARY_NAME",
    "resolve_claude_binary",
)

#: The single trusted-environment override boundary for the provider executable.
CLAUDE_BINARY_ENV = "MOZYO_ONBOARDING_CLAUDE_BINARY"
DEFAULT_CLAUDE_BINARY_NAME = "claude"


def _verify_executable_realpath(candidate: str) -> str | None:
    """Return the symlink-resolved ``realpath`` iff it is a regular executable.

    The executable bit is checked against the ``realpath`` (a dangling /
    non-executable symlink fails), and that same ``realpath`` is returned as the
    deterministic ``argv[0]`` — never a cwd- or symlink-dependent path
    (Redmine #13497 j#74946).
    """
    real = os.path.realpath(candidate)
    if os.path.isfile(real) and os.access(real, os.X_OK):
        return real
    return None


def _trusted_path_dirs(env: Mapping[str, str]) -> list[str]:
    """The trusted ``PATH`` components, fail-closed on any unsafe one.

    No ``PATH`` (or an empty one) yields no search directory — a bare name simply
    does not resolve, never falling back to the ambient process ``PATH``. If any
    component is empty or relative, the whole ``PATH`` is refused rather than
    silently dropping it (that would rewrite the caller's ``PATH`` semantics).
    """
    raw = env.get("PATH", "")
    if not isinstance(raw, str) or raw == "":
        return []
    components = raw.split(os.pathsep)
    unsafe = [comp for comp in components if comp == "" or not os.path.isabs(comp)]
    if unsafe:
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE,
            "trusted PATH contains unsafe (empty or relative) components; "
            "refusing to resolve the conversation provider from an unsafe PATH",
        )
    dirs: list[str] = []
    for comp in components:
        if comp not in dirs:
            dirs.append(comp)
    return dirs


def resolve_claude_binary(
    env: Mapping[str, str] | None = None,
    *,
    name: str = DEFAULT_CLAUDE_BINARY_NAME,
) -> str:
    """Resolve the provider CLI to a verified absolute path, or fail closed.

    Order: the explicit :data:`CLAUDE_BINARY_ENV` override (verified), then the
    trusted ``PATH``. Raises :class:`ConversationProviderError`
    (``conversation_provider_unavailable``) when the override is not executable,
    the trusted ``PATH`` is unsafe, or ``name`` is not found as a regular
    executable — the caller must then mutate nothing and spawn no subprocess.
    """
    env = os.environ if env is None else env

    override = env.get(CLAUDE_BINARY_ENV)
    if isinstance(override, str) and override.strip():
        value = override.strip()
        if not os.path.isabs(value):
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE,
                f"{CLAUDE_BINARY_ENV} must be an absolute path; refusing to "
                f"resolve a relative override against the current directory",
            )
        verified = _verify_executable_realpath(value)
        if verified is None:
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE,
                f"{CLAUDE_BINARY_ENV} does not point at a regular executable file",
            )
        return verified

    # Collect the DISTINCT executable realpaths across the trusted PATH: zero →
    # missing, one → resolved, two or more → ambiguous (fail-closed). First-match
    # is not an ambiguity check (Redmine #13497 j#74946).
    realpaths: list[str] = []
    for directory in _trusted_path_dirs(env):
        verified = _verify_executable_realpath(os.path.join(directory, name))
        if verified is not None and verified not in realpaths:
            realpaths.append(verified)

    if not realpaths:
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE,
            f"the {name!r} conversation-provider CLI was not found as a regular "
            f"executable on the trusted PATH",
        )
    if len(realpaths) > 1:
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE,
            f"the {name!r} conversation-provider CLI resolves to "
            f"{len(realpaths)} distinct executables on the trusted PATH; "
            f"refusing to select ambiguously",
        )
    return realpaths[0]
