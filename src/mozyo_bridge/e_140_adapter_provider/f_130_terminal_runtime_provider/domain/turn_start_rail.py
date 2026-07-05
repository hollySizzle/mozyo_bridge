"""Core-facing herdr turn-start rail — the check-then-wait orchestration (Redmine #13248).

The fourth concrete cut of the built-in **terminal runtime** adapter boundary
from ``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine #12001).
The lower US's landed the pieces this rail composes:

- #13245 the transport port (``domain/terminal_transport``): ``send_text`` /
  ``send_keys`` / ``read_pane`` bare primitives;
- #13246 the state snapshot (``domain/agent_state`` + ``infrastructure/herdr_state``):
  a fail-closed ``read_agent_state`` returning a runtime receiver-state.

This module is the **orchestration** layer that turns "inject a message" into
"confirm the receiver actually *started a turn*" — the ``check-then-wait`` rail the
#13175 PoC established (``vibes/docs/logics/herdr-poc-13175-experiment-log.md``,
E9 / E12–E14). It is a pure orchestrator: every dependency (transport port, state
reader, wait primitive, clock) is injected, so all six outcome paths are
exercisable with in-memory fakes and no live herdr binary.

Why a turn-start rail at all (the ACK / completion doctrine)
-----------------------------------------------------------
``sent`` / ``ok`` from a bare send proves the sender pressed Enter; it does **not**
prove the receiver TUI submitted the prompt and began a turn
(``vibes/docs/logics/ack-completion-receiver-state.md``: delivery ACK is not task
completion, and a rendered pane is never the source of truth). Redmine #13166
hardened the tmux compat rail against exactly this false-positive — a busy /
redrawing composer that absorbs the Enter while the rail still reports ``sent`` —
with a read-only, pane-capture *turn-start observation*. This module is the herdr
analogue of that guard, built on herdr's **event** surface (``wait agent-status``)
instead of pane-capture heuristics, and it is proven equivalent-or-stronger to the
#13166 guard in the design doc (``## Implemented Terminal Runtime Turn-Start Rail
(Redmine #13248)``, the equivalence table).

The check-then-wait ordering (PoC E9 / E12, j#72258 — enforced in code)
-----------------------------------------------------------------------
``wait agent-status`` waits for a *change into* a state and does **not** return
when the pane is already in it (E9 c2): so a wait alone can neither read the
current state nor be armed after a transition without racing it. The rail
therefore follows a fixed order, and :meth:`HerdrTurnStartRail.drive_turn_start`
enforces it:

1. **Pre-injection snapshot (check).** Read the current runtime state (#13246). If
   it is anything other than ``awaiting_input`` — ``busy`` / ``blocked`` /
   ``turn_ended`` / ``unknown``, *including* an unreadable snapshot which degrades
   to ``unknown`` — the rail refuses to inject and fails closed
   (:data:`OUTCOME_PRECONDITION_NOT_IDLE`): a turn started while the pane was
   already busy could not be *attributed* to this injection, so injecting would
   make a later ``started`` unfalsifiable.
2. **Arm the wait first** (before injecting), so the ``working`` transition the
   injection triggers cannot land in the race window between the snapshot and the
   wait (E12 proved arm-then-inject returns in ~0.36s, event-driven).
3. **Inject** — ``send_text`` then ``send_keys enter``. Any transport failure
   fails closed (:data:`OUTCOME_INJECT_FAILED`) and cancels the armed wait.
4. **Collect the wait.** A ``changed`` event (exit 0) is :data:`OUTCOME_STARTED`;
   a ``timeout`` is "delivered but not started" and is re-snapshotted to tell a
   runtime :data:`OUTCOME_BLOCKED` (a permission prompt on screen, E13 / E14) from
   a plain :data:`OUTCOME_DELIVERED_NOT_STARTED`; a pane-get error (E9 c3) is
   :data:`OUTCOME_ABSENT`; an unclassifiable wait failure fails closed to
   ``delivered_not_started`` (we delivered but could not confirm a start).

Codex Enter-resend rail (PoC E14 — enforced in code)
----------------------------------------------------
E14 reproduced the long-known Codex TUI quirk over herdr: the injected text
landed in the composer but the first Enter was **not** submitted, so the turn
never started until Enter was re-sent. When the first wait times out, the rail
runs a bounded Enter-resend: it **reads the pane** (transport ``read_pane``) and
re-sends Enter *only if the injected body still sits in the composer*
(:func:`composer_retains_body`) — never re-typing the body, only the Enter — up to
:attr:`HerdrTurnStartRail.max_enter_resends` times (config; default 1, ``0``
disables it). Each resend re-arms a fresh wait first (the same check-then-wait
order). This logic is agent-kind-agnostic bounded-retry, not Codex-special-cased;
it just happens to be what E14 needed. If the pane read fails or the body is no
longer in the composer, the rail does **not** resend (fail-closed: never blindly
re-Enter when it cannot confirm the stuck-composer precondition).

Subscribe-time event caveat (PoC E14 — fail-safe)
-------------------------------------------------
E14 observed that a wait armed just after the awaited transition had already
occurred could return an event almost immediately (~11ms). The rail treats **any**
``changed`` result (exit 0) as the transition — an immediate event is accepted as
``started``, the fail-safe interpretation — so this caveat never turns a real
start into a timeout. The exact subscribe-time delivery is confirmed against a live
binary at cutover (#13254); this rail is pinned only through fakes.

Scope (staged seam — kept explicit so it does not drift)
--------------------------------------------------------
- **In scope:** the closed :data:`TURN_START_OUTCOMES` vocabulary, the structured
  :class:`TurnStartResult`, the injected-dependency wait-primitive *port*
  (:class:`TurnStartWaitPort` / :class:`ArmedWait`) and its :class:`WaitResult`
  vocabulary, the pure :func:`composer_retains_body` helper, the pure
  :class:`HerdrTurnStartRail` orchestrator, and the redaction-safe
  :func:`turn_start_rail_record_lines` telemetry renderer. All exercised by the
  fake-driven 4-case + 2-precondition + Enter-resend harness (no live binary).
- **Out of scope (later US's):** the concrete herdr ``wait agent-status``
  subprocess wait primitive lives in the sibling ``infrastructure/herdr_turn_start``
  (still a staged seam, no live binary in its tests); wiring this rail into the
  live handoff send path is **#13253**; the installer / pin config is **#13249**;
  live smoke verification of the wait surface is **#13254**.

Non-goals (unchanged, restated for this seam)
---------------------------------------------
- a herdr turn-start observation is a *layer-1/2 runtime signal*, never workflow
  truth: ``started`` / ``turn_ended`` never become task completion or a close
  gate, and ``blocked`` here is a runtime-observed block, not the durable-recorded
  ``blocked`` the attention model means (same boundary as #13246);
- no third-party / dynamic provider; herdr stays the only built-in terminal
  backend and it is default off (#13245).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BLOCKED,
    RUNTIME_RECEIVER_STATES,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)


class TurnStartRailError(TerminalTransportError):
    """A turn-start rail record / construction violates the fail-closed contract.

    Subclasses :class:`TerminalTransportError` (itself a :class:`ValueError`) so the
    whole terminal-runtime seam shares one fail-closed error base and one closed
    failure vocabulary.
    """


# --- wait-primitive result vocabulary (core-owned, closed set) ---------------
# The four ways the injected wait primitive can resolve. The primitive arms a
# ``wait agent-status <target> --status working`` and reports one of these; the
# rail maps them (plus a re-snapshot) onto the turn-start outcomes. Core-owned so
# a provider cannot invent a wait result a caller has not planned for.
WAIT_CHANGED = "changed"  # the awaited status transition was observed (event + exit 0) — E12/E14
WAIT_TIMEOUT = "timeout"  # no transition within the wait window (delivered-not-started) — E9 c1 / E13
WAIT_ABSENT = "absent"  # the target pane does not exist (a pane-get error) — E9 c3
WAIT_ERROR = "error"  # spawn / OS / unclassifiable wait failure — fail-closed

WAIT_RESULT_KINDS: frozenset[str] = frozenset(
    {WAIT_CHANGED, WAIT_TIMEOUT, WAIT_ABSENT, WAIT_ERROR}
)


@dataclass(frozen=True)
class WaitResult:
    """The structured outcome of one armed ``wait agent-status`` collection.

    ``kind`` is the sole authority and is always a member of
    :data:`WAIT_RESULT_KINDS`; ``detail`` is a short, credential-free, path-free
    diagnostic. The rail branches on ``kind`` only, so a novel provider message can
    never change control flow.
    """

    kind: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in WAIT_RESULT_KINDS:
            raise TurnStartRailError(
                f"wait result kind {self.kind!r} is not recognised; allowed: "
                f"{sorted(WAIT_RESULT_KINDS)}"
            )

    @classmethod
    def changed(cls, detail: str = "") -> "WaitResult":
        return cls(kind=WAIT_CHANGED, detail=detail)

    @classmethod
    def timeout(cls, detail: str = "") -> "WaitResult":
        return cls(kind=WAIT_TIMEOUT, detail=detail)

    @classmethod
    def absent(cls, detail: str = "") -> "WaitResult":
        return cls(kind=WAIT_ABSENT, detail=detail)

    @classmethod
    def error(cls, detail: str = "") -> "WaitResult":
        return cls(kind=WAIT_ERROR, detail=detail)


@runtime_checkable
class ArmedWait(Protocol):
    """A wait that has been *armed* (started) and not yet resolved.

    Returned by :meth:`TurnStartWaitPort.arm`. The rail arms a wait *before*
    injecting (check-then-wait), then either :meth:`collect`\\ s it (blocking until
    the awaited transition, a timeout, or an error) or :meth:`cancel`\\ s it (when
    an inject step failed and there is nothing to wait for). Exactly one of the two
    is called per armed wait.
    """

    def collect(self) -> WaitResult:
        """Block until the armed wait resolves; return its structured result."""
        ...

    def cancel(self) -> None:
        """Abandon the armed wait without waiting for it (best-effort cleanup)."""
        ...


@runtime_checkable
class TurnStartWaitPort(Protocol):
    """The injected wait primitive: arm a ``wait agent-status`` for a target.

    A built-in provider only (no dynamic loading). :meth:`arm` starts a
    non-blocking wait for the ``working`` transition on ``target`` and returns an
    :class:`ArmedWait` the rail resolves after it injects. Arming is separate from
    collecting *precisely* so the rail can arm before it injects — the E9
    change-semantics race is avoided only by that order.
    """

    def arm(self, target: str, *, timeout_ms: int) -> ArmedWait:
        """Arm a wait for ``target``'s ``working`` transition; return the handle."""
        ...


# --- turn-start outcome vocabulary (core-owned, closed set) ------------------
# The closed set of results the rail reports. Four "post-injection" outcomes
# (started / delivered-not-started / blocked / absent) plus two "pre-injection"
# fail-closed outcomes (precondition-not-idle / inject-failed). Every path returns
# a structured :class:`TurnStartResult`; the rail never raises.
OUTCOME_STARTED = "started"  # wait observed the working transition (E12/E14)
OUTCOME_DELIVERED_NOT_STARTED = "delivered_not_started"  # injected, but no turn started in the window (E9 c1/E13)
OUTCOME_BLOCKED = "blocked"  # injected, timed out, and a re-snapshot found a runtime block (E13/E14)
OUTCOME_ABSENT = "absent"  # the target pane does not exist (E9 c3)
OUTCOME_PRECONDITION_NOT_IDLE = "precondition_not_idle"  # pre-injection snapshot was not awaiting_input (fail-closed)
OUTCOME_INJECT_FAILED = "inject_failed"  # a send_text / send_keys transport step failed (fail-closed)

TURN_START_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_STARTED,
        OUTCOME_DELIVERED_NOT_STARTED,
        OUTCOME_BLOCKED,
        OUTCOME_ABSENT,
        OUTCOME_PRECONDITION_NOT_IDLE,
        OUTCOME_INJECT_FAILED,
    }
)


@dataclass(frozen=True)
class TurnStartResult:
    """The structured outcome of a turn-start drive (never raises).

    ``outcome`` is the sole authority and is always a member of
    :data:`TURN_START_OUTCOMES`. The remaining fields are redaction-safe telemetry
    (tokens + numbers only, bounded/path-free ``detail``) so an auditor can replay
    the rail:

    - ``snapshot_state`` — the pre-injection observed runtime state (a member of
      :data:`RUNTIME_RECEIVER_STATES`; ``unknown`` when the snapshot was
      unreadable);
    - ``wait_kind`` — the final wait result kind (a member of
      :data:`WAIT_RESULT_KINDS`), or ``None`` when no wait was ever armed (a
      pre-injection fail-closed outcome);
    - ``enter_resends`` — how many *extra* Enter keypresses the resend rail issued
      (0 when the first wait resolved or the resend rail was disabled / skipped);
    - ``reclassified_blocked`` — ``True`` iff a wait timeout was re-snapshotted and
      found a runtime block (the outcome is then ``blocked``).
    """

    outcome: str
    detail: str = ""
    snapshot_state: str = RUNTIME_UNKNOWN
    wait_kind: Optional[str] = None
    enter_resends: int = 0
    reclassified_blocked: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in TURN_START_OUTCOMES:
            raise TurnStartRailError(
                f"turn-start outcome {self.outcome!r} is not recognised; allowed: "
                f"{sorted(TURN_START_OUTCOMES)}"
            )
        if self.snapshot_state not in RUNTIME_RECEIVER_STATES:
            raise TurnStartRailError(
                f"snapshot_state {self.snapshot_state!r} is not a recognised runtime "
                f"receiver state; allowed: {sorted(RUNTIME_RECEIVER_STATES)}"
            )
        if self.wait_kind is not None and self.wait_kind not in WAIT_RESULT_KINDS:
            raise TurnStartRailError(
                f"wait_kind {self.wait_kind!r} is not a recognised wait result kind; "
                f"allowed: {sorted(WAIT_RESULT_KINDS)}"
            )
        if not isinstance(self.enter_resends, int) or isinstance(self.enter_resends, bool):
            raise TurnStartRailError(
                f"enter_resends must be an int, got {self.enter_resends!r}"
            )
        if self.enter_resends < 0:
            raise TurnStartRailError(
                f"enter_resends must be non-negative, got {self.enter_resends}"
            )

    @property
    def started(self) -> bool:
        """True only for a confirmed turn start."""
        return self.outcome == OUTCOME_STARTED

    @property
    def delivered(self) -> bool:
        """True when the message was injected (a wait was armed and collected).

        False only for the two pre-injection fail-closed outcomes
        (``precondition_not_idle`` / ``inject_failed``).
        """
        return self.outcome not in (
            OUTCOME_PRECONDITION_NOT_IDLE,
            OUTCOME_INJECT_FAILED,
        )

    def to_telemetry_dict(self) -> dict:
        """The machine-readable turn-start telemetry (Redmine #13255, j#72602 dec. 4).

        Tokens + numbers only (no free text, no ``detail``, no absolute paths), so
        it is safe to carry verbatim on the structured delivery outcome / JSON and
        the pasteable durable record. This is the *structured* companion to the
        human-readable :func:`turn_start_rail_record_lines`: the projection folds
        two rail outcomes (``delivered_not_started`` / ``blocked``) onto reused
        ``(status, reason)`` wire tokens, so an auditor (and the future #12656
        ledger) reads THIS field — not the reason alone — to replay the rail. The
        keys are exactly the five fields j#72602 decision 4 named:
        ``outcome`` / ``snapshot_state`` / ``wait_kind`` / ``enter_resends`` /
        ``reclassified_blocked``.
        """
        return {
            "outcome": self.outcome,
            "snapshot_state": self.snapshot_state,
            "wait_kind": self.wait_kind,
            "enter_resends": self.enter_resends,
            "reclassified_blocked": self.reclassified_blocked,
        }


def _collapse_ws(text: str) -> str:
    """Collapse every whitespace run to a single space (wrapping-insensitive-ish)."""
    return " ".join(text.split())


def composer_retains_body(content: object, text: object) -> bool:
    """True when the injected ``text`` still appears in the pane ``content`` (pure).

    The Enter-resend gate (PoC E14): the rail re-sends Enter only when the injected
    body is still sitting in the composer — the stuck-Enter signature. Both sides
    are whitespace-collapsed (so a soft line-wrap in the rendered composer does not
    hide the body) and a non-empty body is required. Anything non-string, or an
    empty body, is ``False`` — a read that could not confirm retention must not
    authorise a resend. Never raises.
    """
    if not isinstance(content, str) or not isinstance(text, str):
        return False
    body = _collapse_ws(text)
    if not body:
        return False
    return body in _collapse_ws(content)


#: The default raw key token submitted after the text (herdr ``pane send-keys``).
DEFAULT_ENTER_KEYS = "enter"

#: The default ``wait agent-status --timeout`` window, in milliseconds. Aligned
#: with the #13166 codex-standard-rail landing window (8.0s) so the herdr rail
#: waits about as long as the tmux guard it is equivalent to.
DEFAULT_WAIT_TIMEOUT_MS = 8000

#: The default bound on Enter re-sends after the first wait times out (PoC E14).
#: ``1`` allows a single resend (what E14 needed); ``0`` disables the resend rail.
DEFAULT_MAX_ENTER_RESENDS = 1

#: The default settle delay (seconds) between ``send_text`` and ``send_keys enter``.
#: Zero by default (the seam is staged; the live cutover tunes it), but the clock is
#: injected so a caller can add a settle without touching the rail.
DEFAULT_INJECT_SETTLE_SECONDS = 0.0


class HerdrTurnStartRail:
    """The pure check-then-wait turn-start orchestrator (Redmine #13248).

    Composes the injected transport port (#13245), state reader (#13246), and wait
    primitive into the ``drive_turn_start`` procedure documented at module level.
    It performs **no** direct I/O: every dependency is injected, so all six
    :data:`TURN_START_OUTCOMES` are reachable with in-memory fakes.

    Dependencies:

    - ``transport`` — a :class:`~...domain.terminal_transport.TerminalTransportPort`
      (``send_text`` / ``send_keys`` for injection, ``read_pane`` for the
      Enter-resend composer check);
    - ``reader`` — a #13246 state reader exposing
      ``read_agent_state(target) -> AgentStateResult`` (the pre-injection snapshot
      and the timeout re-snapshot);
    - ``wait`` — a :class:`TurnStartWaitPort` (arm the ``working`` transition wait);
    - ``sleep`` — an injected clock (``Callable[[float], None]``); defaults to a
      no-op so the pure default settle is zero-cost and fully testable.
    """

    def __init__(
        self,
        *,
        transport,
        reader,
        wait: TurnStartWaitPort,
        sleep: Optional[Callable[[float], None]] = None,
        wait_timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
        max_enter_resends: int = DEFAULT_MAX_ENTER_RESENDS,
        inject_settle_seconds: float = DEFAULT_INJECT_SETTLE_SECONDS,
    ):
        if not isinstance(wait_timeout_ms, int) or isinstance(wait_timeout_ms, bool):
            raise TurnStartRailError(
                f"wait_timeout_ms must be an int, got {wait_timeout_ms!r}"
            )
        if wait_timeout_ms <= 0:
            raise TurnStartRailError(
                f"wait_timeout_ms must be positive, got {wait_timeout_ms}"
            )
        if not isinstance(max_enter_resends, int) or isinstance(max_enter_resends, bool):
            raise TurnStartRailError(
                f"max_enter_resends must be an int, got {max_enter_resends!r}"
            )
        if max_enter_resends < 0:
            raise TurnStartRailError(
                f"max_enter_resends must be non-negative (0 disables resends), got "
                f"{max_enter_resends}"
            )
        self._transport = transport
        self._reader = reader
        self._wait = wait
        self._sleep: Callable[[float], None] = sleep if sleep is not None else _no_sleep
        self._wait_timeout_ms = wait_timeout_ms
        self._max_enter_resends = max_enter_resends
        self._inject_settle_seconds = max(0.0, float(inject_settle_seconds))

    @property
    def max_enter_resends(self) -> int:
        return self._max_enter_resends

    @property
    def reader(self):
        """The injected #13246 state reader (``read_agent_state``).

        Exposed read-only so a caller that already holds the resolved herdr rail
        (stashed on ``commands.active_herdr_turn_start_rail`` for a herdr send) can
        take a read-only runtime-state snapshot without resolving a second reader
        from config. Used by the queue-enter post-choreography turn-start
        observation (Redmine #13292): that path does NOT drive the rail (no
        ``drive_turn_start``, no injection ownership, no ``precondition_not_idle``
        fail-close) — it only borrows the reader for an additive, telemetry-only
        ``agent get`` snapshot.
        """
        return self._reader

    def drive_turn_start(
        self, target: str, text: str, *, enter_keys: str = DEFAULT_ENTER_KEYS
    ) -> TurnStartResult:
        """Inject ``text`` into ``target`` and confirm a turn started (check-then-wait).

        Follows the fixed order from the module docstring: snapshot → arm wait →
        inject → collect (→ bounded Enter-resend → re-snapshot). Returns a
        structured :class:`TurnStartResult`; never raises.
        """
        # --- 1. Pre-injection snapshot (check). Non-idle (or unreadable) fails
        # closed: a turn on an already-busy pane cannot be attributed to us.
        snapshot = self._reader.read_agent_state(target)
        snapshot_state = snapshot.state
        if snapshot_state != RUNTIME_AWAITING_INPUT:
            return TurnStartResult(
                outcome=OUTCOME_PRECONDITION_NOT_IDLE,
                detail=(
                    f"pre-injection snapshot was {snapshot_state!r} "
                    f"(read_ok={snapshot.ok}, reason={snapshot.reason}); refusing to "
                    "inject — a turn could not be attributed to this send"
                ),
                snapshot_state=snapshot_state,
            )

        # --- 2. Arm the wait BEFORE injecting (avoid the E9 change-semantics race).
        armed = self._wait.arm(target, timeout_ms=self._wait_timeout_ms)

        # --- 3. Inject: send_text, then (after a settle) send_keys enter.
        text_result = self._transport.send_text(target, text)
        if not text_result.ok:
            armed.cancel()
            return TurnStartResult(
                outcome=OUTCOME_INJECT_FAILED,
                detail=f"send_text failed (reason={text_result.reason})",
                snapshot_state=snapshot_state,
            )
        if self._inject_settle_seconds:
            self._sleep(self._inject_settle_seconds)
        enter_result = self._transport.send_keys(target, enter_keys)
        if not enter_result.ok:
            armed.cancel()
            return TurnStartResult(
                outcome=OUTCOME_INJECT_FAILED,
                detail=f"send_keys failed (reason={enter_result.reason})",
                snapshot_state=snapshot_state,
            )

        # --- 4. Collect the wait, then run the bounded Enter-resend rail on timeout.
        wait_result = armed.collect()
        resends = 0
        while (
            wait_result.kind == WAIT_TIMEOUT and resends < self._max_enter_resends
        ):
            # E14 Codex Enter-resend: only re-Enter when the injected body is still
            # in the composer (a read failure or a cleared composer stops the rail).
            read = self._transport.read_pane(target)
            if not read.ok or not composer_retains_body(read.content, text):
                break
            rearmed = self._wait.arm(target, timeout_ms=self._wait_timeout_ms)
            resend_result = self._transport.send_keys(target, enter_keys)
            if not resend_result.ok:
                rearmed.cancel()
                break
            resends += 1
            wait_result = rearmed.collect()

        return self._classify(
            wait_result,
            target=target,
            snapshot_state=snapshot_state,
            resends=resends,
        )

    def _classify(
        self, wait_result: WaitResult, *, target: str, snapshot_state: str, resends: int
    ) -> TurnStartResult:
        """Map the final wait result (+ a re-snapshot on timeout) onto an outcome."""
        if wait_result.kind == WAIT_CHANGED:
            return TurnStartResult(
                outcome=OUTCOME_STARTED,
                detail="wait observed the working transition (turn started)",
                snapshot_state=snapshot_state,
                wait_kind=wait_result.kind,
                enter_resends=resends,
            )
        if wait_result.kind == WAIT_ABSENT:
            return TurnStartResult(
                outcome=OUTCOME_ABSENT,
                detail="the target pane does not exist (pane-get error on wait)",
                snapshot_state=snapshot_state,
                wait_kind=wait_result.kind,
                enter_resends=resends,
            )
        if wait_result.kind == WAIT_ERROR:
            # We delivered but could not observe the wait — fail closed to
            # "delivered but not confirmed started" (never a confident started).
            return TurnStartResult(
                outcome=OUTCOME_DELIVERED_NOT_STARTED,
                detail=f"wait failed unclassifiably ({wait_result.detail}); "
                "delivered but turn start unconfirmed",
                snapshot_state=snapshot_state,
                wait_kind=wait_result.kind,
                enter_resends=resends,
            )
        # WAIT_TIMEOUT: re-snapshot to tell a runtime block from a plain
        # delivered-not-started (E13/E14: blocked mid-turn times out ``working``).
        resnap = self._reader.read_agent_state(target)
        if resnap.state == RUNTIME_BLOCKED:
            return TurnStartResult(
                outcome=OUTCOME_BLOCKED,
                detail="wait timed out and a re-snapshot found a runtime block "
                "(a permission prompt is on screen)",
                snapshot_state=snapshot_state,
                wait_kind=wait_result.kind,
                enter_resends=resends,
                reclassified_blocked=True,
            )
        return TurnStartResult(
            outcome=OUTCOME_DELIVERED_NOT_STARTED,
            detail="wait timed out; delivered but no turn started in the window",
            snapshot_state=snapshot_state,
            wait_kind=wait_result.kind,
            enter_resends=resends,
        )


def _no_sleep(_seconds: float) -> None:
    """The default injected clock: a no-op (the default settle is zero)."""
    return None


def turn_start_rail_record_lines(result: TurnStartResult) -> list[str]:
    """Render the additive turn-start durable-record telemetry (pure, redaction-safe).

    Follows the #13166 ``turn_start_record_lines`` precedent: tokens + numbers and
    a verdict only, no free text and no absolute paths, so it is safe in a pasteable
    delivery record / persisted note. It documents what the rail observed and never
    overrides ``next_action``; the structured outcome owns the wire.
    """
    return [
        (
            "- Turn start (herdr rail): outcome "
            f"{result.outcome} (snapshot {result.snapshot_state}, "
            f"wait {result.wait_kind if result.wait_kind is not None else 'not-armed'}, "
            f"{result.enter_resends} Enter re-send(s)"
            f"{', re-snapshot found block' if result.reclassified_blocked else ''}). "
            "Check-then-wait: snapshot before injection, wait armed before Enter; "
            "the body was typed once and only Enter was ever re-sent."
        )
    ]


__all__ = (
    "DEFAULT_ENTER_KEYS",
    "DEFAULT_INJECT_SETTLE_SECONDS",
    "DEFAULT_MAX_ENTER_RESENDS",
    "DEFAULT_WAIT_TIMEOUT_MS",
    "OUTCOME_ABSENT",
    "OUTCOME_BLOCKED",
    "OUTCOME_DELIVERED_NOT_STARTED",
    "OUTCOME_INJECT_FAILED",
    "OUTCOME_PRECONDITION_NOT_IDLE",
    "OUTCOME_STARTED",
    "TURN_START_OUTCOMES",
    "WAIT_ABSENT",
    "WAIT_CHANGED",
    "WAIT_ERROR",
    "WAIT_RESULT_KINDS",
    "WAIT_TIMEOUT",
    "ArmedWait",
    "HerdrTurnStartRail",
    "TurnStartRailError",
    "TurnStartResult",
    "TurnStartWaitPort",
    "WaitResult",
    "composer_retains_body",
    "turn_start_rail_record_lines",
)
