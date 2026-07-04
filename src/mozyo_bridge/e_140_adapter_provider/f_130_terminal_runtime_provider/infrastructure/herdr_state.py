"""Built-in herdr CLI agent-state reader (Redmine #13246).

The core seam
(:mod:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state`)
defines the fail-closed herdr-status -> mozyo runtime receiver-state mapping and
the read-result records. This module is the single concrete, built-in provider
that fills it: a subprocess wrapper over the **herdr CLI** ``agent get`` /
``agent list`` JSON read surface (PoC E6 / E7, ``agent_status`` field). Core
still owns the mapping, the result vocabulary, and the target guard; this module
only performs the provider-owned CLI mechanics + JSON parsing, so the dependency
points provider -> core.

It deliberately reuses the #13245 transport adapter's plumbing rather than
duplicating it: the trusted-environment binary resolver (:func:`_resolve_binary`,
including the ``MOZYO_HERDR_BINARY`` / PATH-key fail-closed rules), the injected
:data:`Runner` shape, the command timeout, and the bounded-detail helper all
come from :mod:`...infrastructure.herdr_transport`. The binary a checkout can
cause mozyo to spawn is therefore pinned by the trusted environment exactly as
for the transport seam — a repo-local config can only *select* the herdr backend,
never say which binary runs.

Snapshot, not wait (staged seam)
--------------------------------
:meth:`HerdrCliAgentStateReader.read_agent_state` is a **snapshot** read: it
reports the pane's current runtime state at the moment it is called. It is the
``current-state snapshot`` half of the ``check-then-wait`` rail the PoC
established (E9 / E12–E14): ``wait agent-status`` waits for a *change* into a
state and (E9 c2) does not return when already in it, so a caller must read a
snapshot before arming a wait. The wait rail itself — arming a wait, the Codex
Enter-resend, and the subscribe-time event behaviour observed in E14 — is
**#13248**, not this US. No test here runs a live herdr binary: the reader is
exercised through an injected subprocess ``runner`` that simulates argv,
success / not-found / non-zero-exit / timeout without spawning herdr.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    AgentStateListResult,
    AgentStateResult,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    HERDR_BINARY_ENV,
    Runner,
    _bounded_detail,
    _resolve_binary,
)

# The herdr JSON keys a status token may live under (defensive: the exact
# schema is confirmed against a live binary in a later US, so a small candidate
# set is scanned in order). Core maps whatever token is found; an absent /
# unrecognised one degrades to ``unknown``.
_STATUS_KEYS = ("agent_status", "status", "state")

# The herdr JSON keys an agent handle may live under, for ``agent list`` rows.
_HANDLE_KEYS = ("name", "agent", "target", "id", "handle")

# The herdr JSON keys the ``agent list`` payload's row array may live under when
# the payload is an object rather than a bare array.
_LIST_KEYS = ("agents", "panes", "items")


class HerdrCliAgentStateReader:
    """A herdr agent-state reader over the herdr CLI (``agent get`` / ``agent list``).

    Each read builds an explicit argv list (never a shell string) and runs it
    through the injected ``runner``. Every failure — a malformed target, a
    missing binary, a non-zero exit, a timeout, or an OS error — is turned into a
    structured failure result (state degraded to ``unknown``); a soft failure
    (parseable command output but a missing / unrecognised status) is a
    *successful* read of an ``unknown`` state. A read never raises out of the
    reader.
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
                "herdr agent-state reader binary must be a non-empty string"
            )
        self._binary = binary
        self._runner: Runner = runner if runner is not None else subprocess.run
        self._timeout = timeout

    # -- reads ----------------------------------------------------------------

    def read_agent_state(self, target: str) -> AgentStateResult:
        """Snapshot the runtime state of ``target`` via ``agent get`` (PoC E7).

        Returns an :class:`AgentStateResult`. A mechanical failure (bad target,
        missing binary, non-zero exit, timeout, OS error) fails closed with a
        transport reason and ``state=unknown``; a successful command whose JSON
        carries no recognised status is a successful read of ``unknown``.
        """
        if not valid_target(target):
            return AgentStateResult.failure(
                REASON_INVALID_TARGET, f"invalid target handle: {target!r}"
            )
        completed = self._invoke(["agent", "get", target, "--json"])
        if isinstance(completed, AgentStateResult):
            return completed  # a fail-closed spawn / timeout outcome
        if completed.returncode != 0:
            return AgentStateResult.failure(
                REASON_TRANSPORT_ERROR,
                _bounded_detail(completed.stderr) or f"herdr exit {completed.returncode}",
            )
        raw_status = _extract_status(completed.stdout)
        return AgentStateResult.observed(
            map_agent_status(raw_status), raw_status=raw_status
        )

    def list_agent_states(self) -> AgentStateListResult:
        """Snapshot all managed agents' runtime states via ``agent list``.

        Returns an :class:`AgentStateListResult`. A mechanical failure fails
        closed with a transport reason and no rows; a successful command yields
        one ``(handle, runtime_state)`` pair per parseable row (a row with a
        missing / unrecognised status maps to ``unknown`` rather than being
        dropped). A row missing a usable handle is skipped.
        """
        completed = self._invoke(["agent", "list", "--json"])
        if isinstance(completed, AgentStateResult):
            # Re-shape the shared spawn / timeout failure into the list result.
            return AgentStateListResult.failure(
                completed.reason or REASON_TRANSPORT_ERROR, completed.detail
            )
        if completed.returncode != 0:
            return AgentStateListResult.failure(
                REASON_TRANSPORT_ERROR,
                _bounded_detail(completed.stderr) or f"herdr exit {completed.returncode}",
            )
        return AgentStateListResult.observed(_extract_list(completed.stdout))

    # -- internals ------------------------------------------------------------

    def _invoke(self, tail: list):
        """Run ``binary tail...``; return the CompletedProcess or a failure result.

        A missing binary maps to ``binary_not_found`` and any other spawn / OS /
        timeout failure to ``transport_error``, returned as an
        :class:`AgentStateResult` (both readers re-shape it as needed), so no
        exception escapes a read.
        """
        argv = [self._binary, *tail]
        try:
            return self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError:
            return AgentStateResult.failure(
                REASON_BINARY_NOT_FOUND,
                f"herdr binary not found: {self._binary!r}",
            )
        except subprocess.TimeoutExpired:
            return AgentStateResult.failure(
                REASON_TRANSPORT_ERROR, "herdr command timed out"
            )
        except OSError as exc:
            return AgentStateResult.failure(
                REASON_TRANSPORT_ERROR, f"herdr command failed ({exc.__class__.__name__})"
            )


def _extract_status(stdout: object) -> Optional[str]:
    """Extract a herdr status token from an ``agent get`` payload, or ``None``.

    Defensive by design (the live JSON schema is confirmed in a later US): a JSON
    object contributes the first present of a small candidate key set whose value
    is a string; anything else (non-JSON, a non-object, no candidate key) yields
    ``None``, which the core maps to ``unknown``. Never raises.
    """
    payload = _load_json(stdout)
    if not isinstance(payload, Mapping):
        return None
    return _first_str(payload, _STATUS_KEYS)


def _extract_list(stdout: object) -> tuple[tuple[str, str], ...]:
    """Extract ``(handle, runtime_state)`` pairs from an ``agent list`` payload.

    Accepts either a bare JSON array of row objects or an object carrying the
    rows under a candidate key. Each row must be an object with a usable handle
    (first present string of :data:`_HANDLE_KEYS`); its status is mapped through
    the core (a missing / unrecognised status becomes ``unknown``). Rows without
    a usable handle are skipped. A non-JSON / unrecognised payload yields no
    rows. Never raises.
    """
    payload = _load_json(stdout)
    rows: object
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        rows = None
        for key in _LIST_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        if rows is None:
            return ()
    else:
        return ()
    pairs: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        handle = _first_str(row, _HANDLE_KEYS)
        if handle is None or not handle:
            continue
        state = map_agent_status(_first_str(row, _STATUS_KEYS))
        pairs.append((handle, state))
    return tuple(pairs)


def _load_json(stdout: object) -> object:
    if not isinstance(stdout, str):
        return None
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def _first_str(mapping: Mapping, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return None


def resolve_agent_state_reader(
    config: Optional[TerminalTransportConfig] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
) -> Optional[HerdrCliAgentStateReader]:
    """Resolve the built-in herdr agent-state reader for ``config``, or ``None``.

    Rides on the same default-off backend selection and trusted-environment
    binary resolution as the transport resolver (#13245), reusing
    :func:`_resolve_binary` so the two never drift. Fail-closed selection
    semantics (no silent fallback to tmux):

    - the default / tmux backend returns ``None`` — herdr is off;
    - the herdr backend with no :data:`HERDR_BINARY_ENV` in the trusted
      environment raises :class:`TerminalTransportError` (``binary_unconfigured``);
    - the herdr backend with a configured but unresolvable binary raises
      :class:`TerminalTransportError` (``binary_not_found``);
    - the herdr backend with a resolvable binary returns a
      :class:`HerdrCliAgentStateReader` bound to the resolved path.
    """
    if config is None:
        config = TerminalTransportConfig.default()
    if not config.herdr_enabled:
        return None
    source_env = env if env is not None else os.environ
    raw = source_env.get(HERDR_BINARY_ENV)
    binary = raw.strip() if isinstance(raw, str) else ""
    if not binary:
        raise TerminalTransportError(
            f"terminal transport backend 'herdr' is selected but no herdr binary "
            f"is configured in the trusted environment ({HERDR_BINARY_ENV}); refusing "
            f"to fall back to tmux",
            reason=REASON_BINARY_UNCONFIGURED,
        )
    resolved = _resolve_binary(binary, source_env)
    if resolved is None:
        raise TerminalTransportError(
            f"herdr binary {binary!r} (from {HERDR_BINARY_ENV}) was not found as an "
            f"executable file or on the trusted environment PATH; refusing to fall "
            f"back to tmux",
            reason=REASON_BINARY_NOT_FOUND,
        )
    return HerdrCliAgentStateReader(resolved, runner=runner)


__all__ = (
    "HerdrCliAgentStateReader",
    "resolve_agent_state_reader",
)
