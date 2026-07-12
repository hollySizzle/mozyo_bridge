"""Herdr CLI-event wake adapter for the callback background runtime (Redmine #13520 / #13518).

Design answer j#75098 Q1: the callback watcher wakes on the **stable Herdr CLI event surface**
(``wait agent-status`` — never the raw control socket, and never exposed to an LLM role), but a
Herdr event is a **wake / liveness hint, not workflow authority**. On any wake outcome — a
state change, a bounded timeout, or a wait-primitive restart / error — the background runtime
re-reads the **exact Redmine journal** (the authority) and runs the outbox
(:class:`...application.callback_outbox_processor.CallbackOutboxProcessor`). The wake never
carries content and never holds an LLM turn.

This adapter is the thin, contract-bearing seam: it wraps an injected ``wait_fn`` (the stable
Herdr wait primitive) and normalizes its result to a :class:`WakeSignal` whose
:attr:`should_reread` is **always True**. That invariant is the whole point — a caller can
never be tempted to trust the Herdr signal as authority or to skip the Redmine re-read on a
timeout. The bounded daemon loop that drives this — one production pass (discover -> ingest ->
deliver-once -> sweep) per wake — is implemented in
:func:`...application.callback_runtime.watch` (and reachable via ``workflow callbacks --watch``);
this module gives that loop its fail-safe wake primitive.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - the sanctioned herdr CLI wait boundary (injectable runner)
from dataclasses import dataclass
from typing import Callable, Optional

#: The runner seam for the production wait: given an argv, returns ``(returncode, stderr)``.
#: Injectable so tests never spawn a real ``herdr`` process.
HerdrWaitRunner = Callable[[list], "tuple[int, str]"]

#: The herdr runtime status the watcher waits for a *change into* (a scheduling hint; the
#: watcher re-reads Redmine regardless, so the exact status only affects wake latency).
DEFAULT_WAKE_STATUS = "working"
#: The default bounded ``wait agent-status --timeout`` window (ms). Homes the 45-55s watcher
#: cadence in the background runtime (NOT an LLM turn); an operator may override it.
DEFAULT_WAKE_TIMEOUT_MS = 50_000

#: A Herdr CLI event reported a runtime state change (a scheduling hint only).
WAKE_WOKE = "woke"
#: The bounded wait elapsed with no observed change (still re-read Redmine — nothing is skipped).
WAKE_TIMED_OUT = "timed_out"
#: The wait primitive failed / the background CLI event stream restarted (fail-safe; re-read).
WAKE_ERROR = "error"

WAKE_KINDS = frozenset({WAKE_WOKE, WAKE_TIMED_OUT, WAKE_ERROR})


@dataclass(frozen=True)
class WakeSignal:
    """The normalized outcome of one Herdr-event wake (a hint; Redmine stays the authority).

    ``kind`` is one of :data:`WAKE_KINDS`. ``should_reread`` is **always True**: the background
    runtime re-reads the exact Redmine journal on every wake outcome — a Herdr timeout / restart
    never means "nothing to do", and a Herdr state change is never the workflow authority.
    ``detail`` is a short, redacted, public-safe note (never a pane id / credential).
    """

    kind: str
    detail: str = ""
    should_reread: bool = True


def resolve_wake(wait_fn: Callable[[], object], *, detail: str = "") -> WakeSignal:
    """Run one bounded Herdr-event wait and normalize it to a fail-safe :class:`WakeSignal`.

    ``wait_fn`` is the injected stable Herdr wait primitive (``wait agent-status``); it returns a
    truthy value when a runtime state change was observed and a falsy value on a bounded timeout,
    or raises when the background CLI event stream fails / restarts. In **every** case this
    returns a :class:`WakeSignal` with ``should_reread=True`` — the caller re-reads the exact
    Redmine journal regardless, because the Herdr event is only a hint. A raised error is caught
    (fail-safe): a background wait failure must not crash the runtime or hold a turn.
    """
    try:
        observed = wait_fn()
    except Exception as exc:  # noqa: BLE001 - a background wait failure is fail-safe, still re-read
        return WakeSignal(
            kind=WAKE_ERROR,
            detail=detail or f"herdr wait restarted/failed: {type(exc).__name__}",
        )
    return WakeSignal(kind=WAKE_WOKE if observed else WAKE_TIMED_OUT, detail=detail)


def _default_wait_runner(argv: list) -> "tuple[int, str]":
    """Run a bounded ``herdr wait agent-status`` and capture ``(returncode, stderr)`` (production).

    The ``--timeout`` bounds herdr's own wait; an outer subprocess timeout is a safety margin so a
    hung child cannot pin the background runtime. A subprocess timeout surfaces as a raised
    ``TimeoutExpired`` — :func:`resolve_wake` catches it (fail-safe ``WAKE_ERROR``, still re-read).
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; the sanctioned herdr wait CLI
        argv, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stderr or ""


def build_herdr_event_wait(
    binary: str,
    target: str,
    *,
    status: str = DEFAULT_WAKE_STATUS,
    timeout_ms: int = DEFAULT_WAKE_TIMEOUT_MS,
    runner: Optional[HerdrWaitRunner] = None,
) -> Callable[[], object]:
    """Build the production ``wait_fn``: a bounded, blocking ``herdr wait agent-status`` (#13520 F1b).

    Returns a zero-arg callable suitable for :func:`resolve_wake`. Each call blocks on
    ``herdr wait agent-status <target> --status <status> --timeout <ms>`` (a *change into* the
    status) and returns **truthy** when herdr observed the change (rc 0) and **falsy** on a bounded
    timeout / other non-zero exit. This is the stable Herdr CLI event surface (design j#75098 Q1),
    never the raw control socket and never exposed to an LLM role; the wake is only a scheduling
    hint, so the exact ``(target, status)`` affects latency, not correctness — the runtime re-reads
    the exact Redmine journal on every wake. A spawn / reap failure propagates so ``resolve_wake``
    records it as ``WAKE_ERROR`` (fail-safe). ``runner`` is injected in tests.
    """
    run: HerdrWaitRunner = runner if runner is not None else _default_wait_runner
    argv = [
        str(binary), "wait", "agent-status", str(target),
        "--status", str(status), "--timeout", str(int(timeout_ms)),
    ]

    def _wait() -> object:
        rc, _stderr = run(list(argv))
        return rc == 0  # rc 0 = observed the change (woke); non-zero = timeout / other (re-read)

    return _wait


__all__ = (
    "WAKE_WOKE",
    "WAKE_TIMED_OUT",
    "WAKE_ERROR",
    "WAKE_KINDS",
    "WakeSignal",
    "resolve_wake",
    "HerdrWaitRunner",
    "DEFAULT_WAKE_STATUS",
    "DEFAULT_WAKE_TIMEOUT_MS",
    "build_herdr_event_wait",
)
