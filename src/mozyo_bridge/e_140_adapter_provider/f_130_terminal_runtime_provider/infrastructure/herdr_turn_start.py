"""Built-in herdr CLI ``wait agent-status`` wait primitive + rail resolver (Redmine #13248).

The core seam
(:mod:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail`)
defines the pure check-then-wait :class:`HerdrTurnStartRail`, the closed
outcome / wait vocabularies, and the injected wait-primitive *port*
(:class:`TurnStartWaitPort` / :class:`ArmedWait`). This module is the single
concrete, built-in provider that fills the wait port: a subprocess wrapper over
the herdr CLI ``wait agent-status`` surface (PoC E9 / E12–E14), plus a fail-closed
resolver that wires the whole rail (transport #13245 + state reader #13246 + this
wait primitive) from the same default-off backend selection and trusted-env binary
the sibling resolvers use. Core still owns the orchestration and the vocabularies;
this module only performs the provider-owned subprocess mechanics, so the
dependency points provider -> core.

Why a two-phase arm / collect (not a single blocking call)
----------------------------------------------------------
``wait agent-status`` waits for a *change into* ``working`` and (E9 c2) does not
return when the pane is already there, so the rail must **arm the wait before it
injects** — otherwise the transition the injection triggers can land in the race
window between a snapshot read and a (blocking) wait call. A single blocking call
cannot express that order, so this primitive splits it: :meth:`HerdrCliWaitPrimitive.arm`
starts the wait subprocess **non-blocking** (``Popen``) and returns an
:class:`_HerdrArmedWait`; the rail injects, then calls
:meth:`_HerdrArmedWait.collect`, which blocks on the subprocess and classifies its
exit into the core :class:`WaitResult` vocabulary.

Double timeout (herdr ``--timeout`` inside a subprocess bound)
--------------------------------------------------------------
The herdr wait's own ``--timeout <ms>`` is the inner deadline; :meth:`collect`
adds an **outer** ``communicate`` timeout of that window plus a small margin
(:data:`SUBPROCESS_TIMEOUT_MARGIN_SECONDS`), so a herdr process that hangs past its
own deadline is still reaped and reported as a ``timeout`` rather than blocking the
rail forever. The outer bound is deliberately slightly longer so a healthy herdr
timeout surfaces as the herdr-reported timeout, not the outer kill.

Exit classification (confirmed live at cutover #13254)
------------------------------------------------------
A herdr wait exit 0 carries the transition event (``changed`` — accepted even when
it returns almost immediately, the E14 subscribe-time fail-safe). A non-zero exit
is classified by a small, documented set of stderr indicators: a *pane-get* error
(the target does not exist, E9 c3) -> ``absent``; a *timed-out* message (E9 c1 /
E13) -> ``timeout``; anything else -> ``error`` (fail-closed, the rail maps it to
"delivered but unconfirmed"). The exact stderr tokens are confirmed against a live
binary at the cutover smoke (#13254); the indicator set here is defensive and the
default is fail-closed, so an unrecognised message degrades to ``error`` rather
than a confident mis-classification.

Scope (staged seam)
-------------------
No test here runs a live herdr binary: the wait primitive is exercised through an
injected ``Popen`` factory that simulates arm / exit-0 / non-zero / hang without
spawning herdr, and the rail resolver is pinned for the tmux/off and herdr
branches through the shared trusted-env binary resolution. Wiring the resolved
rail into the live handoff send path is **#13253**; the installer / pin config is
**#13249**; live verification is **#13254**.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    HERDR_STATUS_WORKING,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    TerminalTransportConfig,
    TerminalTransportError,
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    DEFAULT_INJECT_SETTLE_SECONDS,
    DEFAULT_MAX_ENTER_RESENDS,
    DEFAULT_WAIT_TIMEOUT_MS,
    ArmedWait,
    HerdrTurnStartRail,
    WaitResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    HerdrCliAgentStateReader,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
    Runner,
    HerdrCliTransport,
    _bounded_detail,
    _resolve_binary,
)

#: The outer ``communicate`` timeout margin (seconds) added on top of the herdr
#: ``--timeout`` window, so a wedged herdr process is reaped shortly after its own
#: deadline rather than blocking the rail. Slightly longer than the inner deadline
#: so a healthy timeout surfaces as the herdr timeout, not the outer kill.
SUBPROCESS_TIMEOUT_MARGIN_SECONDS = 2.0

# The Popen factory shape: a callable with ``subprocess.Popen``'s shape. Injected
# so tests can simulate arm / exit / hang without spawning a process.
PopenFactory = Callable[..., "subprocess.Popen[str]"]

# stderr substrings (lower-cased) that classify a non-zero herdr wait exit. The
# exact tokens are confirmed live at #13254; the set is defensive and the default
# (no match) fails closed to ``error``.
_ABSENT_INDICATORS = (
    "no such pane",
    "pane not found",
    "no pane",
    "unknown pane",
    "pane get",
    "get pane",
    "does not exist",
)
_TIMEOUT_INDICATORS = (
    "timed out",
    "timeout",
)


class _HerdrArmedWait:
    """A herdr ``wait agent-status`` subprocess that has been armed, not yet reaped.

    Holds the live ``Popen`` (or a pre-failed sentinel result). :meth:`collect`
    blocks on it up to the outer bound and classifies the exit; :meth:`cancel`
    reaps it without waiting (used when an inject step failed). Exactly one is
    called per armed wait.
    """

    def __init__(
        self,
        proc: Optional["subprocess.Popen[str]"],
        *,
        subprocess_timeout: float,
        prefailed: Optional[WaitResult] = None,
    ):
        self._proc = proc
        self._subprocess_timeout = subprocess_timeout
        self._prefailed = prefailed

    def collect(self) -> WaitResult:
        if self._prefailed is not None:
            return self._prefailed
        proc = self._proc
        assert proc is not None  # a non-prefailed armed wait always holds a process
        try:
            stdout, stderr = proc.communicate(timeout=self._subprocess_timeout)
        except subprocess.TimeoutExpired:
            self._reap()
            return WaitResult.timeout(
                "herdr wait exceeded the outer subprocess bound"
            )
        except OSError as exc:
            return WaitResult.error(
                f"herdr wait failed to reap ({exc.__class__.__name__})"
            )
        return _classify_exit(proc.returncode, stderr)

    def cancel(self) -> None:
        if self._prefailed is not None or self._proc is None:
            return
        self._reap()

    def _reap(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except (OSError, ValueError):
            pass
        try:
            proc.communicate(timeout=self._subprocess_timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass


def _classify_exit(returncode: object, stderr: object) -> WaitResult:
    """Map a herdr wait exit code + stderr onto a :class:`WaitResult` (never raises)."""
    if returncode == 0:
        return WaitResult.changed("herdr wait returned the working transition event")
    detail = _bounded_detail(stderr) or f"herdr wait exit {returncode}"
    lowered = detail.lower()
    if any(token in lowered for token in _ABSENT_INDICATORS):
        return WaitResult.absent(detail)
    if any(token in lowered for token in _TIMEOUT_INDICATORS):
        return WaitResult.timeout(detail)
    return WaitResult.error(detail)


class HerdrCliWaitPrimitive:
    """A :class:`TurnStartWaitPort` over the herdr CLI ``wait agent-status``.

    :meth:`arm` builds an explicit argv list (never a shell string) and starts it
    **non-blocking** through the injected ``popen`` factory, returning an
    :class:`_HerdrArmedWait` the rail collects after it injects. A malformed target
    or a spawn failure yields a *pre-failed* armed wait whose ``collect`` reports a
    fail-closed :class:`WaitResult` — arming never raises.
    """

    backend = BACKEND_HERDR

    def __init__(
        self,
        binary: str,
        *,
        status: str = HERDR_STATUS_WORKING,
        popen: Optional[PopenFactory] = None,
        timeout_margin_seconds: float = SUBPROCESS_TIMEOUT_MARGIN_SECONDS,
    ):
        if not isinstance(binary, str) or not binary:
            raise TerminalTransportError(
                "herdr wait primitive binary must be a non-empty string"
            )
        self._binary = binary
        self._status = status
        self._popen: PopenFactory = popen if popen is not None else subprocess.Popen
        self._margin = max(0.0, float(timeout_margin_seconds))

    def arm(self, target: str, *, timeout_ms: int) -> ArmedWait:
        if not valid_target(target):
            return _HerdrArmedWait(
                None,
                subprocess_timeout=0.0,
                prefailed=WaitResult.error(f"invalid target handle: {target!r}"),
            )
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            return _HerdrArmedWait(
                None,
                subprocess_timeout=0.0,
                prefailed=WaitResult.error(
                    f"wait timeout_ms must be a positive int, got {timeout_ms!r}"
                ),
            )
        argv = [
            self._binary,
            "wait",
            "agent-status",
            target,
            "--status",
            self._status,
            "--timeout",
            str(timeout_ms),
        ]
        outer_timeout = timeout_ms / 1000.0 + self._margin
        try:
            proc = self._popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return _HerdrArmedWait(
                None,
                subprocess_timeout=0.0,
                prefailed=WaitResult.error(f"herdr binary not found: {self._binary!r}"),
            )
        except OSError as exc:
            return _HerdrArmedWait(
                None,
                subprocess_timeout=0.0,
                prefailed=WaitResult.error(
                    f"herdr wait failed to spawn ({exc.__class__.__name__})"
                ),
            )
        return _HerdrArmedWait(proc, subprocess_timeout=outer_timeout)


def _resolve_herdr_binary(
    config: Optional[TerminalTransportConfig],
    env: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Resolve the trusted-env herdr binary for ``config``, or ``None`` (off).

    Rides on exactly the same default-off backend selection and trusted-environment
    binary resolution (:func:`_resolve_binary`, :data:`HERDR_BINARY_ENV`) as the
    #13245 / #13246 resolvers, so the wired rail's three providers never point at
    different binaries. Fail-closed: an unconfigured or unresolvable binary raises
    :class:`TerminalTransportError` (no silent fallback to tmux).
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
    return resolved


def resolve_turn_start_rail(
    config: Optional[TerminalTransportConfig] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
    popen: Optional[PopenFactory] = None,
    sleep: Optional[Callable[[float], None]] = None,
    wait_timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    max_enter_resends: int = DEFAULT_MAX_ENTER_RESENDS,
    inject_settle_seconds: float = DEFAULT_INJECT_SETTLE_SECONDS,
) -> Optional[HerdrTurnStartRail]:
    """Resolve the fully wired herdr turn-start rail for ``config``, or ``None`` (off).

    Builds all three providers from the one trusted-env binary
    (:func:`_resolve_herdr_binary`): the #13245 transport (``send_text`` /
    ``send_keys`` / ``read_pane``), the #13246 state reader (``read_agent_state``),
    and this module's wait primitive, then constructs the pure
    :class:`HerdrTurnStartRail` over them. Fail-closed selection (no silent fallback
    to tmux): the default / tmux backend returns ``None``; a herdr backend with no
    configured or resolvable binary raises :class:`TerminalTransportError`.

    This resolver is the staged wiring seam only — it constructs the rail but does
    **not** wire it into the live handoff send path (that is #13253).
    """
    binary = _resolve_herdr_binary(config, env)
    if binary is None:
        return None
    transport = HerdrCliTransport(binary, runner=runner)
    reader = HerdrCliAgentStateReader(binary, runner=runner)
    wait = HerdrCliWaitPrimitive(binary, popen=popen)
    return HerdrTurnStartRail(
        transport=transport,
        reader=reader,
        wait=wait,
        sleep=sleep,
        wait_timeout_ms=wait_timeout_ms,
        max_enter_resends=max_enter_resends,
        inject_settle_seconds=inject_settle_seconds,
    )


__all__ = (
    "SUBPROCESS_TIMEOUT_MARGIN_SECONDS",
    "HerdrCliWaitPrimitive",
    "resolve_turn_start_rail",
)
