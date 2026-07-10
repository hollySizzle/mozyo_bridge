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
duplicating it: the shared trusted-environment binary resolver
(:func:`resolve_herdr_binary`, the ``MOZYO_HERDR_BINARY`` → trusted-PATH ``herdr``
fail-closed order), the injected
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
    REASON_INVALID_PAYLOAD,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    Runner,
    _bounded_detail,
    resolve_herdr_binary,
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

# An ``agent list`` payload may wrap the rows one level down under an envelope
# key (e.g. ``{"result": {"agents": [...]}}``); these are the recognised envelope
# keys scanned before giving up. A payload with none of :data:`_LIST_KEYS` at the
# top level and no recognised envelope is treated as *unrecognisable* (fail
# closed), distinct from a recognised-but-empty list.
_LIST_ENVELOPE_KEYS = ("result", "data")

# An ``agent get`` payload nests the agent object under the same envelope plus an
# object key (live 0.7.1: ``{"id": ..., "result": {"agent": {"agent_status": ...}}}``);
# these are the recognised object keys descended into below an envelope before
# giving up on a status token.
_GET_OBJECT_KEYS = ("agent", "pane")


def agent_row_runtime_state(row: object) -> str:
    """Map one raw ``agent list`` row's status to a runtime receiver-state (pure).

    The public per-row twin of the :func:`_rows_to_state_pairs` mapping, shared
    with read models that fold the raw inventory themselves (the cockpit herdr
    supplier, Redmine #13356): the row's status token is looked up under the same
    :data:`_STATUS_KEYS` candidates and mapped through the core fail-closed
    :func:`~mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state.map_agent_status`
    — a non-mapping row / absent / unrecognised status degrades to ``unknown``.
    Never raises.
    """
    if not isinstance(row, Mapping):
        return map_agent_status(None)
    return map_agent_status(_first_str(row, _STATUS_KEYS))


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
        completed = self._invoke(["agent", "get", target])
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

        Returns an :class:`AgentStateListResult`. Failure modes, all fail-closed:

        - a mechanical failure (missing binary, spawn / OS error, timeout,
          non-zero exit) fails closed with a transport reason and no rows;
        - a command that ran but returned a payload that is **not a recognisable
          list schema** (non-JSON, a scalar JSON value, or an object with no
          recognised row container) fails closed with :data:`REASON_INVALID_PAYLOAD`
          rather than reporting an empty *success* — an unreadable list is not
          "no agents".

        On a recognised payload the read succeeds and yields one
        ``(handle, runtime_state)`` pair per usable row (a row with a missing /
        unrecognised status maps to ``unknown`` rather than being dropped). A row
        that is not an object, or whose handle is missing / not a well-formed
        target (see :func:`valid_target`), is **skipped** — one malformed row does
        not fail the whole read — and the number skipped is recorded in the
        result ``detail`` so the loss stays observable.
        """
        completed = self._invoke(["agent", "list"])
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
        rows = _extract_list_rows(completed.stdout)
        if rows is None:
            return AgentStateListResult.failure(
                REASON_INVALID_PAYLOAD,
                "herdr agent list payload was not a recognised JSON array or "
                "agents object",
            )
        pairs, skipped = _rows_to_state_pairs(rows)
        detail = f"skipped {skipped} row(s) with an invalid handle" if skipped else ""
        return AgentStateListResult.observed(pairs, detail=detail)

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

    Live 0.7.1 nests the agent object under an envelope
    (``{"id": ..., "result": {"agent": {"agent_status": ...}}}``, #13307 live
    smoke); flat shapes keep working as aliases. The scan is defensive: the
    shallowest scope wins — top level first, then each recognised envelope
    object, then a recognised agent object inside it. Anything else (non-JSON, a
    non-object, no candidate key at any scope) yields ``None``, which the core
    maps to ``unknown``. Never raises.
    """
    return _status_from_container(_load_json(stdout))


def _status_from_container(payload: object) -> Optional[str]:
    """Recursively resolve the status token from a decoded payload, or ``None``."""
    if not isinstance(payload, Mapping):
        return None
    token = _first_str(payload, _STATUS_KEYS)
    if token is not None:
        return token
    for key in (*_LIST_ENVELOPE_KEYS, *_GET_OBJECT_KEYS):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            token = _status_from_container(nested)
            if token is not None:
                return token
    return None


def _extract_list_rows(stdout: object) -> Optional[list]:
    """Return the recognised row list from an ``agent list`` payload, or ``None``.

    A payload is *recognisable* iff it is a bare JSON array, an object carrying a
    list under one of :data:`_LIST_KEYS`, or an object wrapping such a container
    one level down under an envelope key (:data:`_LIST_ENVELOPE_KEYS`). Any of
    those yields the row list, which may legitimately be **empty** (a recognised
    "no agents"). Anything else — a non-JSON payload, a scalar JSON value, or an
    object with no recognised container — is **unrecognisable** and yields
    ``None``, so the caller fails closed rather than reporting an empty success.
    Never raises.
    """
    return _rows_from_container(_load_json(stdout))


def _rows_from_container(payload: object) -> Optional[list]:
    """Recursively resolve the row list from a decoded payload, or ``None``."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in _LIST_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return candidate
        for key in _LIST_ENVELOPE_KEYS:
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                rows = _rows_from_container(nested)
                if rows is not None:
                    return rows
    return None


def _rows_to_state_pairs(rows: list) -> tuple[tuple[tuple[str, str], ...], int]:
    """Map recognised rows to ``(handle, runtime_state)`` pairs; count skips.

    Each row must be an object with a handle (first present string of
    :data:`_HANDLE_KEYS`) that, after trimming, is a well-formed target (reusing
    the core :func:`valid_target` guard so a list row's handle is validated
    exactly like an ``agent get`` target). Its status is mapped through the core
    (a missing / unrecognised status becomes ``unknown``). A row that is not an
    object, or whose handle is missing / blank / malformed, is **skipped** (never
    a whole-payload failure); the returned int is how many rows were skipped.
    Never raises.
    """
    pairs: list[tuple[str, str]] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, Mapping):
            skipped += 1
            continue
        raw_handle = _first_str(row, _HANDLE_KEYS)
        handle = raw_handle.strip() if isinstance(raw_handle, str) else ""
        if not valid_target(handle):
            skipped += 1
            continue
        state = map_agent_status(_first_str(row, _STATUS_KEYS))
        pairs.append((handle, state))
    return tuple(pairs), skipped


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
    binary resolution as the transport resolver (#13245), sharing the single
    :func:`resolve_herdr_binary` so the resolution order never drifts. Fail-closed
    selection semantics (no silent fallback to tmux):

    - the default / tmux backend returns ``None`` — herdr is off;
    - the herdr backend with no :data:`HERDR_BINARY_ENV` and no trusted-PATH
      ``herdr`` raises :class:`TerminalTransportError` (``binary_unconfigured``);
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
    resolution = resolve_herdr_binary(source_env)
    return HerdrCliAgentStateReader(resolution.path, runner=runner)


__all__ = (
    "HerdrCliAgentStateReader",
    "agent_row_runtime_state",
    "resolve_agent_state_reader",
)
