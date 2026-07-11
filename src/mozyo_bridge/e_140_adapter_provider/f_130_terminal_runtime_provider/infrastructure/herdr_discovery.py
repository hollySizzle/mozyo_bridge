"""Built-in herdr CLI agent discovery lister (Redmine #13261).

The provider half of the herdr-native target-resolution boundary
(:mod:`...domain.herdr_target_resolution`): the concrete implementation of
:class:`~...domain.herdr_target_resolution.HerdrAgentDiscoveryPort`. It runs herdr
``agent list`` and returns its raw rows (each carrying the durable ``name`` and the
transient ``pane_id`` locator) so core can decode + match them.

It reuses the existing herdr plumbing rather than duplicating it: the shared
trusted-environment binary resolver (``MOZYO_HERDR_BINARY`` / trusted ``PATH`` ``herdr``,
:func:`resolve_herdr_binary`), the injected :data:`Runner` shape, the command timeout, and the #13246 defensive
row extractor (:func:`_extract_list_rows`). Core owns what the rows mean; this
module only performs the provider-owned CLI mechanics, so the dependency points
provider -> core. Any mechanical failure (missing binary, spawn / OS error,
timeout, non-zero exit) or an unrecognisable payload fails closed with a
:class:`~...domain.terminal_transport.TerminalTransportError` — never a silent empty
list, so a target resolution can never re-bind against "no agents" that were really
an unreadable snapshot.

No test here runs a live herdr binary: the lister is exercised through an injected
subprocess ``runner`` that verifies argv and simulates outcomes.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_BINARY_NOT_FOUND,
    REASON_INVALID_PAYLOAD,
    REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    Runner,
    _bounded_detail,
    resolve_herdr_binary,
)


class HerdrCliAgentLister:
    """A :class:`HerdrAgentDiscoveryPort` over herdr ``agent list`` (JSON default output).

    Builds an explicit argv (never a shell string) and runs it through the injected
    ``runner``. Fail-closed: any mechanical failure or an unrecognisable payload
    raises :class:`TerminalTransportError`, so a caller never mistakes an unreadable
    snapshot for an empty inventory.
    """

    def __init__(
        self,
        binary: str,
        *,
        runner: Optional[Runner] = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ):
        if not isinstance(binary, str) or not binary:
            raise TerminalTransportError(
                "herdr agent lister binary must be a non-empty string"
            )
        self._binary = binary
        self._runner: Runner = runner if runner is not None else subprocess.run
        self._timeout = timeout

    def list_agent_rows(self) -> Sequence[Mapping[str, object]]:
        """Run herdr ``agent list`` and return its raw rows (fail closed)."""
        argv = [self._binary, "agent", "list"]
        try:
            completed = self._runner(
                argv, capture_output=True, text=True, timeout=self._timeout
            )
        except FileNotFoundError:
            raise TerminalTransportError(
                f"herdr binary not found: {self._binary!r}",
                reason=REASON_BINARY_NOT_FOUND,
            )
        except subprocess.TimeoutExpired:
            raise TerminalTransportError(
                "herdr agent list timed out", reason=REASON_TRANSPORT_ERROR
            )
        except OSError as exc:
            raise TerminalTransportError(
                f"herdr agent list failed ({exc.__class__.__name__})",
                reason=REASON_TRANSPORT_ERROR,
            )
        if completed.returncode != 0:
            raise TerminalTransportError(
                _bounded_detail(completed.stderr)
                or f"herdr agent list exited {completed.returncode}",
                reason=REASON_TRANSPORT_ERROR,
            )
        rows = _extract_list_rows(completed.stdout)
        if rows is None:
            raise TerminalTransportError(
                "herdr agent list payload was not a recognised JSON array or agents "
                "object",
                reason=REASON_INVALID_PAYLOAD,
            )
        return rows


def resolve_agent_lister(
    config: Optional[TerminalTransportConfig] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
) -> Optional[HerdrCliAgentLister]:
    """Resolve the built-in herdr agent lister for ``config``, or ``None`` (off).

    Same default-off backend selection + trusted-environment binary resolution as
    the transport / state resolvers (#13245 / #13246), sharing the single
    :func:`resolve_herdr_binary` so the resolution order never drifts. Fail-closed
    (no silent fallback to tmux):

    - the default / tmux backend returns ``None``;
    - herdr selected with no :data:`HERDR_BINARY_ENV` and no trusted-PATH ``herdr``
      raises :class:`TerminalTransportError` (``binary_unconfigured``);
    - herdr selected with an unresolvable binary raises (``binary_not_found``);
    - herdr selected with a resolvable binary returns a :class:`HerdrCliAgentLister`.
    """
    if config is None:
        config = TerminalTransportConfig.default()
    if not config.herdr_enabled:
        return None
    source_env = env if env is not None else os.environ
    resolution = resolve_herdr_binary(source_env)
    return HerdrCliAgentLister(resolution.path, runner=runner)


__all__ = (
    "HerdrCliAgentLister",
    "resolve_agent_lister",
)
