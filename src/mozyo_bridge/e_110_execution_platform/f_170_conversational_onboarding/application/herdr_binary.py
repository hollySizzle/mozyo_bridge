"""Herdr launch-binary resolution for onboarding preflight (Redmine #13498).

The herdr binary is resolved from the **trusted environment only**, never a
repo-local config. Onboarding delegates to the single integrated trusted-env
resolver every herdr surface shares (:func:`resolve_herdr_binary`, Redmine
#13496) so the preflight's notion of "resolved / which binary" can never drift
from the runtime that actually launches herdr. The resolver fails closed
(raising :class:`TerminalTransportError` with a closed reason); onboarding maps
that structured outcome onto the closed preflight :class:`HerdrBinary` fact
(``resolved`` / ``missing`` / ``ambiguous``) so a missing or ambiguous binary is
surfaced in the preflight rather than crashing it.
"""

from __future__ import annotations

import os
from typing import Mapping

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BINARY_SOURCE_ENV,
    BINARY_SOURCE_PATH,
    REASON_BINARY_AMBIGUOUS,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNSAFE_PATH,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
    resolve_herdr_binary as resolve_trusted_herdr_binary,
)

from ..domain.preflight import (
    HERDR_AMBIGUOUS,
    HERDR_MISSING,
    HERDR_RESOLVED,
    HERDR_SOURCE_ENV,
    HERDR_SOURCE_NONE,
    HERDR_SOURCE_PATH,
    HerdrBinary,
)

__all__ = ("HERDR_BINARY_ENV", "resolve_herdr_binary")

# Failure reasons that mean "the identity could not be pinned" (not simply
# absent) — surfaced as ``ambiguous`` rather than ``missing``.
_AMBIGUOUS_REASONS = frozenset({REASON_BINARY_AMBIGUOUS, REASON_BINARY_UNSAFE_PATH})


def resolve_herdr_binary(env: Mapping[str, str] | None = None) -> HerdrBinary:
    """Resolve the herdr binary for onboarding preflight via the integrated resolver.

    Returns a closed :class:`HerdrBinary`: ``resolved`` with the absolute path and
    provenance on success; ``ambiguous`` when the trusted PATH is unsafe or the
    binary is ambiguous; ``missing`` otherwise (an explicit-but-unresolvable
    value keeps ``env`` provenance; nothing configured is ``none``).
    """
    if env is None:
        env = os.environ

    try:
        resolution = resolve_trusted_herdr_binary(env)
    except TerminalTransportError as exc:
        return _from_error(exc, env)

    # The integrated resolver's source vocabulary (env/path) matches the
    # preflight's HerdrBinary source vocabulary exactly.
    source = resolution.source if resolution.source in (
        BINARY_SOURCE_ENV,
        BINARY_SOURCE_PATH,
    ) else HERDR_SOURCE_NONE
    return HerdrBinary(state=HERDR_RESOLVED, source=source, path=resolution.path)


def _from_error(exc: TerminalTransportError, env: Mapping[str, str]) -> HerdrBinary:
    explicit = bool(str(env.get(HERDR_BINARY_ENV, "")).strip())
    reason = getattr(exc, "reason", None)
    if reason in _AMBIGUOUS_REASONS:
        return HerdrBinary(
            state=HERDR_AMBIGUOUS,
            source=HERDR_SOURCE_ENV if explicit else HERDR_SOURCE_PATH,
            path=None,
        )
    if reason == REASON_BINARY_NOT_FOUND or explicit:
        # An explicit MOZYO_HERDR_BINARY value that did not resolve.
        return HerdrBinary(state=HERDR_MISSING, source=HERDR_SOURCE_ENV, path=None)
    # binary_unconfigured: nothing configured in the trusted environment.
    return HerdrBinary(state=HERDR_MISSING, source=HERDR_SOURCE_NONE, path=None)
