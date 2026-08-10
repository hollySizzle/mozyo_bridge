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

1. **Pre-injection snapshot (check).** Read the current runtime state (#13246). The
   rail injects only from an :data:`INJECTABLE_PRECONDITION_STATES` member —
   ``awaiting_input`` (herdr ``idle``) or ``turn_ended`` (herdr ``done``), both
   static "waiting for the next input" states (#13319, design j#73077). Any other
   state — ``busy`` / ``blocked`` / ``unknown``, *including* an unreadable snapshot
   which degrades to ``unknown`` — makes the rail refuse to inject and fail closed
   (:data:`OUTCOME_PRECONDITION_NOT_IDLE`): a turn started while the pane was
   already busy could not be *attributed* to this injection, so injecting would
   make a later ``started`` unfalsifiable. ``turn_ended`` is safe because the prior
   turn is already over, so the next ``working`` is still attributable to this send;
   it stays ``turn_ended`` (the #13246 mapping is unchanged) and is never promoted
   to workflow/close ``done``.
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
(:func:`composer_retains_body`, a whitespace-insensitive match so the composer's
mid-token line wraps do not hide the body — Redmine #13322) — never re-typing the
body, only the Enter — up to
:attr:`HerdrTurnStartRail.max_enter_resends` times (config; default 1, ``0``
disables it). Each resend re-arms a fresh wait first (the same check-then-wait
order). This logic is agent-kind-agnostic bounded-retry, not Codex-special-cased;
it just happens to be what E14 needed. If the pane read fails or the body is no
longer in the composer, the rail does **not** resend (fail-closed: never blindly
re-Enter when it cannot confirm the stuck-composer precondition).

Wait-ERROR Enter-resend (Redmine #15202 — enforced in code)
-----------------------------------------------------------
E14's rail was armed by :data:`WAIT_TIMEOUT` alone, so a first wait that resolved
:data:`WAIT_ERROR` — a spawn / OS / unclassifiable wait failure — fell straight
through to ``delivered_not_started`` with ``enter_resends=0``. #15199 hit that
shape nine times in one lane: the body was typed, the first Enter was sent, the
*observation* failed, and the composer kept the request forever. A failed wait is
evidence about the **observer**, not the receiver, so refusing to press Enter
again is not a safety property — it is a lost turn.

``error`` is therefore a resend candidate too. Timeout-only sequences keep the
configured :attr:`HerdrTurnStartRail.max_enter_resends` budget. After ``error``,
the effective total budget is hard-capped at one, including a later timeout. The
error resend re-waits on :attr:`HerdrTurnStartRail.error_resend_wait_timeout_ms`
(default and hard maximum 15s): the failed first wait measured no start latency.

The error path's resend gate is deliberately **stricter** than the timeout path's,
and the asymmetry is the point. A timeout is a positive observation (the wait ran
and saw no transition); an error is the absence of one, so before it presses Enter
into a pane it cannot characterise the rail must positively establish *who* holds
the target and *what* is on it. The six gate conditions, and why each one is
fail-closed, are documented on ``domain/turn_start_resend_gate`` — the leaf that
owns the gate's vocabulary — and enforced in :meth:`HerdrTurnStartRail._error_resend_gate`.

Every refusal is recorded as a closed :data:`RESEND_SKIP_REASONS` token, and the
FIRST wait kind is preserved in :attr:`TurnStartResult.first_wait_kind` so a
recovered turn never erases that the first observation failed. The
:data:`WAIT_TIMEOUT` gate is untouched (#15202 requirement 5): same two checks,
same 8s re-wait, same reader call sequence — a rail constructed without a
``screen_guard`` / ``identity_probe`` behaves on timeouts byte-for-byte as before.

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
  vocabulary, the pure :class:`HerdrTurnStartRail` orchestrator, and the
  redaction-safe :func:`turn_start_rail_record_lines` telemetry renderer. The
  resend gate's closed skip vocabulary and pure predicates live in the leaf
  ``domain/turn_start_resend_gate``, re-exported here so importers are unchanged.
  All exercised by the fake-driven 4-case + 2-precondition + Enter-resend harness.
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
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)
# The resend gate's closed vocabulary and pure predicates live in their own leaf module
# (module-health gate). Re-exported below via ``__all__`` so every existing importer of
# ``composer_retains_body`` from this module is unchanged.
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (  # noqa: E501
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_BUDGET_EXHAUSTED,
    RESEND_SKIP_DISABLED,
    RESEND_SKIP_ENTER_SEND_FAILED,
    RESEND_SKIP_IDENTITY_DRIFT,
    RESEND_SKIP_IDENTITY_PROBE_UNBOUND,
    RESEND_SKIP_IDENTITY_UNCONFIRMED,
    RESEND_SKIP_NONE,
    RESEND_SKIP_PANE_UNREADABLE,
    RESEND_SKIP_REASONS,
    RESEND_SKIP_RECEIVER_BLOCKED,
    RESEND_SKIP_SCREEN_GUARD_UNBOUND,
    RESEND_SKIP_STARTUP_SCREEN,
    RESEND_SKIP_STATE_NOT_INJECTABLE,
    RESEND_SKIP_STATE_UNREADABLE,
    RESEND_SKIP_WAIT_UNARMED,
    ResendIdentityProbe,
    ResendScreenGuard,
    composer_retains_body,
    current_composer_retains_body,
    probe_identity,
    screen_guard_detects,
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

#: The wait kinds that make an Enter-only resend a *candidate* (Redmine #15202).
#: ``timeout`` is the E14 stuck-composer shape; ``error`` is the #15199 shape where
#: the observation itself failed. Both mean "the body is delivered and no start was
#: confirmed"; neither is by itself permission to press Enter — each has its own gate
#: below. ``changed`` (started) and ``absent`` (no pane) are terminal and never resend.
RESENDABLE_WAIT_KINDS: frozenset[str] = frozenset({WAIT_TIMEOUT, WAIT_ERROR})


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


# --- injectable pre-injection precondition set (core-owned, closed set) ------
# The runtime receiver-states from which the rail is willing to inject (Redmine
# #13319, design consultation j#73077). Both are "静止" states that hold until the
# next input, so a wait armed before injection can attribute the subsequent
# ``working`` transition to *this* send:
#
# - ``awaiting_input`` (herdr ``idle``): the composer is quiet, no turn running;
# - ``turn_ended`` (herdr ``done``): the assistant turn finished and herdr holds
#   ``done`` until the next input (#13319 measured it persisting 60s+, so
#   ``wait ... --status idle`` times out). It is NOT workflow/close ``done`` (the
#   ``agent_state`` mapping is unchanged); it is only an injectable static runtime
#   state here — the previous turn is already over, so a next ``working`` is still
#   attributable to this send.
#
# ``busy`` / ``blocked`` / ``unknown`` are deliberately excluded and keep failing
# closed (:data:`OUTCOME_PRECONDITION_NOT_IDLE`): ``busy`` would break attribution
# to an already-running turn, ``blocked`` is a runtime block (a permission prompt),
# and ``unknown`` covers a read failure or a novel status. This set is the *only*
# thing #13319 widened — the mapping, outcome vocabulary, and fail-closed rules are
# unchanged.
INJECTABLE_PRECONDITION_STATES: frozenset[str] = frozenset(
    {RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED}
)


# --- turn-start outcome vocabulary (core-owned, closed set) ------------------
# The closed set of results the rail reports. Four "post-injection" outcomes
# (started / delivered-not-started / blocked / absent) plus two "pre-injection"
# fail-closed outcomes (precondition-not-idle / inject-failed). Every path returns
# a structured :class:`TurnStartResult`; the rail never raises.
OUTCOME_STARTED = "started"  # wait observed the working transition (E12/E14)
OUTCOME_DELIVERED_NOT_STARTED = "delivered_not_started"  # injected, but no turn started in the window (E9 c1/E13)
OUTCOME_BLOCKED = "blocked"  # injected, timed out, and a re-snapshot found a runtime block (E13/E14)
OUTCOME_ABSENT = "absent"  # the target pane does not exist (E9 c3)
OUTCOME_PRECONDITION_NOT_IDLE = "precondition_not_idle"  # pre-injection snapshot was not injectable (busy/blocked/unknown) — fail-closed
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
    - ``first_wait_kind`` — the kind the FIRST armed wait resolved to, preserved
      even when a resend later changed the verdict (Redmine #15202): a start that
      needed a resend is not the same fact as one that did not. Equal to
      ``wait_kind`` when no resend ran, ``None`` alongside a ``None`` ``wait_kind``;
    - ``enter_resends`` — how many *extra* Enter keypresses the resend rail issued
      (0 when the first wait resolved or the resend rail was disabled / skipped);
    - ``resend_skipped_reason`` — a member of :data:`RESEND_SKIP_REASONS`: why a
      candidate resend did not happen, or ``""`` when one ran or none was warranted;
    - ``reclassified_blocked`` — ``True`` iff a wait timeout was re-snapshotted and
      found a runtime block (the outcome is then ``blocked``).
    """

    outcome: str
    detail: str = ""
    snapshot_state: str = RUNTIME_UNKNOWN
    wait_kind: Optional[str] = None
    enter_resends: int = 0
    reclassified_blocked: bool = False
    first_wait_kind: Optional[str] = None
    resend_skipped_reason: str = RESEND_SKIP_NONE

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
        if (
            self.first_wait_kind is not None
            and self.first_wait_kind not in WAIT_RESULT_KINDS
        ):
            raise TurnStartRailError(
                f"first_wait_kind {self.first_wait_kind!r} is not a recognised wait "
                f"result kind; allowed: {sorted(WAIT_RESULT_KINDS)}"
            )
        if self.resend_skipped_reason not in RESEND_SKIP_REASONS:
            raise TurnStartRailError(
                f"resend_skipped_reason {self.resend_skipped_reason!r} is not "
                f"recognised; allowed: {sorted(RESEND_SKIP_REASONS)}"
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
        first five keys are exactly the fields j#72602 decision 4 named:
        ``outcome`` / ``snapshot_state`` / ``wait_kind`` / ``enter_resends`` /
        ``reclassified_blocked``.

        Redmine #15202 adds two **additive** keys the five could not answer for:
        ``first_wait_kind`` (``wait_kind`` became the kind AFTER a resend, so a
        recovered turn reported ``changed`` and the original failure vanished) and
        ``resend_skipped_reason`` (``enter_resends=0`` alone cannot say whether a
        resend was unwanted or refused). Existing keys keep their meaning and
        position, so an older reader is unaffected.
        """
        return {
            "outcome": self.outcome,
            "snapshot_state": self.snapshot_state,
            "wait_kind": self.wait_kind,
            "enter_resends": self.enter_resends,
            "reclassified_blocked": self.reclassified_blocked,
            "first_wait_kind": self.first_wait_kind,
            "resend_skipped_reason": self.resend_skipped_reason,
        }


#: The default raw key token submitted after the text (herdr ``pane send-keys``).
DEFAULT_ENTER_KEYS = "enter"

#: The default ``wait agent-status --timeout`` window, in milliseconds. Aligned
#: with the #13166 codex-standard-rail landing window (8.0s) so the herdr rail
#: waits about as long as the tmux guard it is equivalent to.
DEFAULT_WAIT_TIMEOUT_MS = 8000

#: The default bound on Enter re-sends after a first wait that did not confirm a
#: start (PoC E14 ``timeout``; Redmine #15202 ``error``). ``1`` allows a single
#: resend (what E14 needed); ``0`` disables the resend rail. This is ONE budget
#: shared by both arming kinds, so the body is typed once and at most this many
#: extra Enter keypresses are ever pressed per drive — a mixed timeout-then-error
#: sequence cannot spend it twice.
DEFAULT_MAX_ENTER_RESENDS = 1

#: The default ``wait agent-status --timeout`` window for the re-wait after a
#: WAIT_ERROR-armed Enter resend, in milliseconds (Redmine #15202). Longer than
#: :data:`DEFAULT_WAIT_TIMEOUT_MS` on purpose: the first wait *failed* rather than
#: timing out, so it produced no evidence about how long a start takes on this
#: receiver, and re-waiting only the 8s landing window would turn a slow-but-real
#: start into a second unconfirmed record. The timeout-armed resend keeps the 8s
#: window, where the first wait did measure the receiver.
DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS = 15000

#: The HARD upper bound on the error-resend re-wait window, in milliseconds. The
#: requirement is "再待機は最大15秒" (#15202 j#102578 item 2) — a *maximum*, not a
#: default. Making the field merely positive-checked let a caller configure 21s and
#: still call it compliant (audit j#102755 finding 2), so the bound is enforced at
#: construction and a larger value is refused rather than silently clamped: a caller
#: asking for 30s has a different intent than this contract permits, and clamping
#: would honour the letter while hiding the disagreement.
MAX_ERROR_RESEND_WAIT_TIMEOUT_MS = 15000

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
        error_resend_wait_timeout_ms: int = DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS,
        identity_probe: Optional[ResendIdentityProbe] = None,
    ):
        if not isinstance(wait_timeout_ms, int) or isinstance(wait_timeout_ms, bool):
            raise TurnStartRailError(
                f"wait_timeout_ms must be an int, got {wait_timeout_ms!r}"
            )
        if wait_timeout_ms <= 0:
            raise TurnStartRailError(
                f"wait_timeout_ms must be positive, got {wait_timeout_ms}"
            )
        if not isinstance(error_resend_wait_timeout_ms, int) or isinstance(
            error_resend_wait_timeout_ms, bool
        ):
            raise TurnStartRailError(
                "error_resend_wait_timeout_ms must be an int, got "
                f"{error_resend_wait_timeout_ms!r}"
            )
        if error_resend_wait_timeout_ms <= 0:
            raise TurnStartRailError(
                "error_resend_wait_timeout_ms must be positive, got "
                f"{error_resend_wait_timeout_ms}"
            )
        if error_resend_wait_timeout_ms > MAX_ERROR_RESEND_WAIT_TIMEOUT_MS:
            raise TurnStartRailError(
                "error_resend_wait_timeout_ms must not exceed "
                f"{MAX_ERROR_RESEND_WAIT_TIMEOUT_MS} ms (the contract's '再待機は最大"
                f"15秒' maximum), got {error_resend_wait_timeout_ms}"
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
        self._error_resend_wait_timeout_ms = error_resend_wait_timeout_ms
        self._identity_probe = identity_probe

    @property
    def max_enter_resends(self) -> int:
        return self._max_enter_resends

    @property
    def error_resend_wait_timeout_ms(self) -> int:
        """The re-wait window (ms) after a WAIT_ERROR-armed Enter resend (#15202)."""
        return self._error_resend_wait_timeout_ms

    @property
    def reader(self):
        """The injected #13246 state reader (``read_agent_state``).

        Exposed read-only so a caller that already holds the resolved herdr rail
        (stashed on ``commands.active_herdr_turn_start_rail`` for a herdr send) can
        take runtime-state snapshots without resolving a second reader from config.
        The queue-enter path borrows it for its pre-Enter causal baseline, strict
        at-most-once resend gate, and post-choreography snapshot (#13292 / #15242).
        That path still does NOT call ``drive_turn_start`` or transfer injection
        ownership, because queue-enter deliberately permits a busy receiver.
        """
        return self._reader

    def read_visible_pane(self, target: str) -> str:
        """The rendered visible content of ``target`` (a read-only borrow of the transport).

        Redmine #14082 R2: the background_service delivery seam runs the #13760 pre-send startup
        admission (:func:`...herdr_startup_admission.evaluate_startup_admission`) immediately before
        :meth:`drive_turn_start`, exactly as the ``handoff send`` boundary does. That gate needs the
        receiver's VISIBLE pane text (classified against the provider's declared startup screens),
        which is a different read than the ``read_agent_state`` snapshot the rail's precondition gate
        uses. This exposes the transport's ``read_pane`` read-only — symmetric with :attr:`reader` — so
        a caller holding the resolved rail reads the visible pane through the same bound primitive
        without resolving a second transport from config. Raises on a failed read so the admission
        fails **closed** (an unreadable pane is a zero-send refusal, never treated as startup-clear).
        """
        read = self._transport.read_pane(target)
        if not read.ok or read.content is None:
            raise TerminalTransportError(
                f"visible-pane read failed for {target!r}: {read.reason or 'unreadable'}"
            )
        return read.content

    def arm_turn_start_wait(self, target: str, *, timeout_ms: int) -> ArmedWait:
        """Arm this rail's bound working-transition observer without injecting.

        The Herdr queue-enter path owns body injection because it permits a busy
        receiver, unlike :meth:`drive_turn_start`.  It still must use the exact
        same bound wait primitive (binary, server and environment) as the standard
        rail.  This narrow two-stage seam lets that path arm before its Enter
        without resolving a second provider or reaching into ``_wait``.
        """
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise TurnStartRailError(
                f"timeout_ms must be an int, got {timeout_ms!r}"
            )
        if timeout_ms <= 0:
            raise TurnStartRailError(
                f"timeout_ms must be positive, got {timeout_ms}"
            )
        return self._wait.arm(target, timeout_ms=timeout_ms)

    def drive_turn_start(
        self,
        target: str,
        text: str,
        *,
        enter_keys: str = DEFAULT_ENTER_KEYS,
        screen_guard: Optional[ResendScreenGuard] = None,
    ) -> TurnStartResult:
        """Inject ``text`` into ``target`` and confirm a turn started (check-then-wait).

        Follows the fixed order from the module docstring: snapshot → arm wait →
        inject → collect (→ bounded Enter-resend → re-snapshot). Returns a
        structured :class:`TurnStartResult`; never raises.

        ``screen_guard`` is the optional pure pane classifier (:data:`ResendScreenGuard`)
        the WAIT_ERROR resend gate requires (Redmine #15202). Leaving it unbound — like
        leaving the constructor's ``identity_probe`` unbound — does not change any
        behaviour this rail had before #15202. It only withholds the new error-armed
        resend, which refuses rather than press Enter into a pane whose occupant or
        startup screens it cannot rule out.
        """
        # --- 1. Pre-injection snapshot (check). A non-injectable (or unreadable)
        # state fails closed: a turn on a busy/blocked/unknown pane cannot be
        # attributed to us. Injectable = awaiting_input OR turn_ended (#13319): both
        # are static states that hold until the next input, so the wait armed below
        # attributes the next ``working`` transition to this send.
        snapshot = self._reader.read_agent_state(target)
        snapshot_state = snapshot.state
        if snapshot_state not in INJECTABLE_PRECONDITION_STATES:
            return TurnStartResult(
                outcome=OUTCOME_PRECONDITION_NOT_IDLE,
                detail=(
                    f"pre-injection snapshot was {snapshot_state!r} "
                    f"(read_ok={snapshot.ok}, reason={snapshot.reason}); refusing to "
                    "inject — a turn could not be attributed to this send"
                ),
                snapshot_state=snapshot_state,
            )

        # --- 1b. Capture WHO holds the target, before a single byte is typed (#15202,
        # audit j#102755 finding 3). This is the baseline the error-resend gate compares
        # against: the outer identity gates (target resolution, `--target-repo`, startup
        # admission) all run BEFORE this call and never re-run mid-drive, so nothing
        # otherwise guarantees the locator still addresses the same agent 8–15s later.
        # A pane can be killed and its id reused, or a lane relaunched, inside the wait
        # window. `None` here (no probe, or an unresolvable one) is not a send failure —
        # the send proceeds exactly as before — it only makes the extra Enter unavailable.
        baseline_identity = (
            None
            if self._identity_probe is None
            else probe_identity(self._identity_probe, target)
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

        # --- 4. Collect, then run the bounded resend rail (E14 / #15202).
        # Timeout-only sequences retain the configured budget. Once an error is
        # observed, the effective total budget is capped at one across both kinds.
        wait_result = armed.collect()
        first_wait_kind = wait_result.kind
        resends = 0
        skipped_reason = RESEND_SKIP_NONE
        error_seen = False
        while wait_result.kind in RESENDABLE_WAIT_KINDS:
            if wait_result.kind == WAIT_ERROR:
                error_seen = True
            effective_resend_budget = (
                min(self._max_enter_resends, 1)
                if error_seen
                else self._max_enter_resends
            )
            if resends >= effective_resend_budget:
                skipped_reason = (
                    RESEND_SKIP_DISABLED
                    if effective_resend_budget == 0
                    else RESEND_SKIP_BUDGET_EXHAUSTED
                )
                break
            if wait_result.kind == WAIT_TIMEOUT:
                # E14, unchanged (#15202 requirement 5): only re-Enter when the
                # injected body is still in the composer. A read failure or a cleared
                # composer stops the rail — same two checks, same reader call sequence.
                gate = self._timeout_resend_gate(target, text)
                rearm_timeout_ms = self._wait_timeout_ms
            else:
                gate = self._error_resend_gate(
                    target, text, screen_guard, baseline_identity
                )
                rearm_timeout_ms = self._error_resend_wait_timeout_ms
            if gate != RESEND_SKIP_NONE:
                skipped_reason = gate
                break
            rearmed = self._wait.arm(target, timeout_ms=rearm_timeout_ms)
            resend_result = self._transport.send_keys(target, enter_keys)
            if not resend_result.ok:
                rearmed.cancel()
                skipped_reason = RESEND_SKIP_ENTER_SEND_FAILED
                break
            resends += 1
            wait_result = rearmed.collect()

        return self._classify(
            wait_result,
            target=target,
            snapshot_state=snapshot_state,
            resends=resends,
            first_wait_kind=first_wait_kind,
            skipped_reason=skipped_reason,
        )

    def _timeout_resend_gate(self, target: str, text: str) -> str:
        """The E14 stuck-composer gate: a skip reason, or :data:`RESEND_SKIP_NONE`.

        Byte-for-byte the pre-#15202 condition (``read_pane`` must succeed and the
        composer must still hold the body); it only *names* which half refused, which
        is telemetry rather than control flow.
        """
        read = self._transport.read_pane(target)
        if not read.ok:
            return RESEND_SKIP_PANE_UNREADABLE
        if not composer_retains_body(read.content, text):
            return RESEND_SKIP_BODY_ABSENT
        return RESEND_SKIP_NONE

    def _error_resend_gate(
        self,
        target: str,
        text: str,
        screen_guard: Optional[ResendScreenGuard],
        baseline_identity: Optional[str],
    ) -> str:
        """The #15202 wait-error gate: a skip reason, or :data:`RESEND_SKIP_NONE`.

        Stricter than :meth:`_timeout_resend_gate` because a failed wait is the absence
        of an observation rather than a negative one — see the module docstring. The
        order is deliberate: the free checks first (an unbound guard / probe costs no
        read), then identity — *who* holds the pane is prior to *what* is rendered on
        it, and re-reading a pane that a different agent now owns is already reading the
        wrong thing — then the pane read, then the screen classification BEFORE the body
        check (so a modal rendering over a still-visible body is reported as the screen
        it is rather than as a retained composer), and the runtime state last, since it
        costs another read.
        """
        if screen_guard is None:
            return RESEND_SKIP_SCREEN_GUARD_UNBOUND
        if self._identity_probe is None:
            return RESEND_SKIP_IDENTITY_PROBE_UNBOUND
        if baseline_identity is None:
            # The pre-injection probe could not name the holder, so there is nothing to
            # compare against and drift is undetectable. Refuse rather than assume.
            return RESEND_SKIP_IDENTITY_UNCONFIRMED
        current_identity = probe_identity(self._identity_probe, target)
        if current_identity is None:
            return RESEND_SKIP_IDENTITY_UNCONFIRMED
        if current_identity != baseline_identity:
            # A DIFFERENT agent holds this locator now. The extra Enter would land on a
            # receiver that never got the body — the exact wrong-target send the outer
            # identity gates exist to prevent, arriving through the resend instead.
            return RESEND_SKIP_IDENTITY_DRIFT
        read = self._transport.read_pane(target)
        if not read.ok:
            return RESEND_SKIP_PANE_UNREADABLE
        content = read.content
        if not isinstance(content, str) or not content.strip():
            # A blank read is not evidence of a clear composer (#13760's live lane saw
            # an empty pane *after* a dialog ate the body). Never "clear".
            return RESEND_SKIP_PANE_UNREADABLE
        if screen_guard_detects(screen_guard, content):
            return RESEND_SKIP_STARTUP_SCREEN
        if not composer_retains_body(content, text):
            return RESEND_SKIP_BODY_ABSENT
        # The runtime re-snapshot must POSITIVELY confirm an injectable receiver, not
        # merely fail to say "blocked". `AgentStateResult` forces `state=unknown` on a
        # mechanical read failure, so an `== RUNTIME_BLOCKED` test alone admits a resend
        # on a read that never happened — fail-OPEN, and exactly the "read失敗では再送し
        # ない" requirement it was meant to satisfy (audit j#102755 finding 1). A
        # successful read can also carry an *observed* unknown, and `busy` means a turn
        # is already running, so both are refused too. Same injectable set the
        # pre-injection precondition gate uses, so "may we inject here" has one answer.
        resnap = self._reader.read_agent_state(target)
        if not resnap.ok:
            return RESEND_SKIP_STATE_UNREADABLE
        if resnap.state == RUNTIME_BLOCKED:
            # A runtime permission prompt is up: Enter would answer it, not submit.
            return RESEND_SKIP_RECEIVER_BLOCKED
        if resnap.state not in INJECTABLE_PRECONDITION_STATES:
            return RESEND_SKIP_STATE_NOT_INJECTABLE
        return RESEND_SKIP_NONE

    def _classify(
        self,
        wait_result: WaitResult,
        *,
        target: str,
        snapshot_state: str,
        resends: int,
        first_wait_kind: Optional[str] = None,
        skipped_reason: str = RESEND_SKIP_NONE,
    ) -> TurnStartResult:
        """Map the final wait result (+ a re-snapshot on timeout) onto an outcome."""
        if wait_result.kind == WAIT_CHANGED:
            return TurnStartResult(
                outcome=OUTCOME_STARTED,
                detail="wait observed the working transition (turn started)",
                snapshot_state=snapshot_state,
                wait_kind=wait_result.kind,
                enter_resends=resends,
                first_wait_kind=first_wait_kind,
                resend_skipped_reason=skipped_reason,
            )
        if wait_result.kind == WAIT_ABSENT:
            return TurnStartResult(
                outcome=OUTCOME_ABSENT,
                detail="the target pane does not exist (pane-get error on wait)",
                snapshot_state=snapshot_state,
                wait_kind=wait_result.kind,
                enter_resends=resends,
                first_wait_kind=first_wait_kind,
                resend_skipped_reason=skipped_reason,
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
                first_wait_kind=first_wait_kind,
                resend_skipped_reason=skipped_reason,
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
                first_wait_kind=first_wait_kind,
                resend_skipped_reason=skipped_reason,
            )
        return TurnStartResult(
            outcome=OUTCOME_DELIVERED_NOT_STARTED,
            detail="wait timed out; delivered but no turn started in the window",
            snapshot_state=snapshot_state,
            wait_kind=wait_result.kind,
            enter_resends=resends,
            first_wait_kind=first_wait_kind,
            resend_skipped_reason=skipped_reason,
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
    wait_token = result.wait_kind if result.wait_kind is not None else "not-armed"
    # Redmine #15202: name the FIRST wait whenever a resend changed the verdict, so a
    # recovered turn still records that the first observation failed, and name a
    # refused resend's reason so `0 Enter re-send(s)` is never ambiguous between
    # "none was needed" and "one was wanted and withheld".
    first_token = (
        f", first wait {result.first_wait_kind}"
        if result.first_wait_kind is not None
        and result.first_wait_kind != result.wait_kind
        else ""
    )
    skip_token = (
        f", resend withheld: {result.resend_skipped_reason}"
        if result.resend_skipped_reason
        else ""
    )
    return [
        (
            "- Turn start (herdr rail): outcome "
            f"{result.outcome} (snapshot {result.snapshot_state}, "
            f"wait {wait_token}"
            f"{first_token}, "
            f"{result.enter_resends} Enter re-send(s)"
            f"{', re-snapshot found block' if result.reclassified_blocked else ''}"
            f"{skip_token}). "
            "Check-then-wait: snapshot before injection, wait armed before Enter; "
            "the body was typed once and only Enter was ever re-sent."
        )
    ]


__all__ = (
    "DEFAULT_ENTER_KEYS",
    "DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS",
    "DEFAULT_INJECT_SETTLE_SECONDS",
    "DEFAULT_MAX_ENTER_RESENDS",
    "DEFAULT_WAIT_TIMEOUT_MS",
    "INJECTABLE_PRECONDITION_STATES",
    "MAX_ERROR_RESEND_WAIT_TIMEOUT_MS",
    "OUTCOME_ABSENT",
    "OUTCOME_BLOCKED",
    "OUTCOME_DELIVERED_NOT_STARTED",
    "OUTCOME_INJECT_FAILED",
    "OUTCOME_PRECONDITION_NOT_IDLE",
    "OUTCOME_STARTED",
    "RESENDABLE_WAIT_KINDS",
    "RESEND_SKIP_BODY_ABSENT",
    "RESEND_SKIP_BUDGET_EXHAUSTED",
    "RESEND_SKIP_DISABLED",
    "RESEND_SKIP_ENTER_SEND_FAILED",
    "RESEND_SKIP_IDENTITY_DRIFT",
    "RESEND_SKIP_IDENTITY_PROBE_UNBOUND",
    "RESEND_SKIP_IDENTITY_UNCONFIRMED",
    "RESEND_SKIP_NONE",
    "RESEND_SKIP_PANE_UNREADABLE",
    "RESEND_SKIP_REASONS",
    "RESEND_SKIP_RECEIVER_BLOCKED",
    "RESEND_SKIP_SCREEN_GUARD_UNBOUND",
    "RESEND_SKIP_STARTUP_SCREEN",
    "RESEND_SKIP_STATE_NOT_INJECTABLE",
    "RESEND_SKIP_STATE_UNREADABLE",
    "RESEND_SKIP_WAIT_UNARMED",
    "TURN_START_OUTCOMES",
    "WAIT_ABSENT",
    "WAIT_CHANGED",
    "WAIT_ERROR",
    "WAIT_RESULT_KINDS",
    "WAIT_TIMEOUT",
    "ArmedWait",
    "HerdrTurnStartRail",
    "ResendIdentityProbe",
    "ResendScreenGuard",
    "TurnStartRailError",
    "TurnStartResult",
    "TurnStartWaitPort",
    "WaitResult",
    "composer_retains_body",
    "current_composer_retains_body",
    "turn_start_rail_record_lines",
)
