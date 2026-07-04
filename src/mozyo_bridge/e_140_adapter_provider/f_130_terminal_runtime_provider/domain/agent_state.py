"""Core-facing herdr agent-status -> mozyo runtime receiver-state mapping (Redmine #13246).

The second concrete cut of the built-in **terminal runtime** adapter boundary
from ``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine #12001).
#13245 landed the transport port (``domain/terminal_transport``); this module
lands the *state* half — a pure, fail-closed mapping from the state herdr
reports about a pane's agent (``working`` / ``blocked`` / ``idle`` / ``done`` /
``unknown``, proven in the #13175 PoC
``vibes/docs/logics/herdr-poc-13175-experiment-log.md``, E7 / E13 / E14) onto a
small mozyo-owned **runtime receiver-state** vocabulary.

Boundary, restated from the design docs so it stays enforced in code:

- **Core owns** the observed-status vocabulary, the runtime receiver-state
  vocabulary, the pure mapping, the fail-closed result shape, and the
  fail-closed behaviour. Those are the values and records in *this* module.
- **Providers own** the concrete ``agent get`` / ``agent list`` CLI mechanics
  and JSON parsing. The built-in herdr provider that fills this seam lives in
  the sibling ``infrastructure.herdr_state``; this module imports no provider,
  so the dependency only ever points provider -> core.

Why a runtime receiver-state vocabulary and not an attention / workflow state
----------------------------------------------------------------------------
herdr ``agent_status`` is a **layer-1 runtime receiver signal** in the ACK /
completion doctrine (``vibes/docs/logics/ack-completion-receiver-state.md``): it
observes the pane's rendered runtime, not durable workflow truth. The mapping
target here is therefore a runtime observation vocabulary, deliberately kept
*incapable* of asserting workflow truth:

- it is **not** the derived cockpit ``attention_state``
  (``vibes/docs/logics/cockpit-attention-state.md`` — ``healthy`` /
  ``owner_waiting`` / ``review_waiting`` / ``blocked`` / ``stalled`` / ``done`` /
  ``retired_candidate`` / ``unknown``), which is derived from Redmine journals /
  gates / managed events *plus* runtime observation. This runtime state is only
  one *input* a caller may later feed into that derivation; it never overwrites
  it. In particular a herdr ``blocked`` here is a *runtime-observed* block (a
  permission prompt is on screen, PoC E13 ``generic_permission_prompt`` /
  E14 ``osc_title_blocked``), not the durable-recorded ``blocked`` the attention
  model means (``blocked_recorded``);
- it is **not** ``task completion`` / a close gate. Critically, herdr ``done``
  means the assistant *turn* finished (PoC E14 ``wait done``), which the doctrine
  classes as a layer-2 ``assistant_turn_finished`` signal — it is **not** the
  attention model's ``done`` (``close_gate_satisfied``) and must never be
  promoted to it. The mapping keeps ``done`` as a distinct ``turn_ended`` runtime
  observation so a caller cannot silently read it as workflow ``done``.

Fail-closed rule (the whole point of the seam):

- an unknown / unrecognised / non-string herdr status, and any parse failure,
  all map to :data:`RUNTIME_UNKNOWN`. :func:`map_agent_status` never raises; a
  broken or novel observation degrades to ``unknown`` (which callers treat as
  "consult tmux liveness", never as death or completion), exactly like the OTel
  activity layer's ``unknown``
  (``e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.agent_activity``).

Scope (staged seam — kept explicit so it does not drift):

- **In scope:** the observed-status vocabulary, the runtime receiver-state
  vocabulary, the pure fail-closed mapping, and the fail-closed read result
  record (filled by the sibling herdr ``agent get`` / ``agent list`` reader).
- **Out of scope (later US's):** ``wait agent-status`` turn-start / change
  semantics (#13248 — this module is a *snapshot* read model; the check-then-wait
  rail from PoC E9 / E12–E14 is built there, see the design doc), durable
  identity naming (#13247), any test that runs a live herdr binary, wiring this
  runtime state into the cockpit attention derivation, and any installer /
  distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TRANSPORT_FAILURE_REASONS,
    TerminalTransportError,
)


class AgentStateError(TerminalTransportError):
    """An agent-state result violates the fail-closed contract.

    Subclasses :class:`TerminalTransportError` (itself a :class:`ValueError`) so
    the whole terminal-runtime seam shares one fail-closed error base and one
    closed failure-reason vocabulary.
    """


# --- herdr observed-status vocabulary (core-owned) ---------------------------
# The values herdr's ``agent_status`` field reports for a managed pane, as
# catalogued in the #13175 PoC (E7 working/idle/done, E13/E14 blocked, E6
# unknown). Core-owned so a provider cannot invent a status; an unrecognised
# value maps to ``unknown`` rather than being trusted.
HERDR_STATUS_WORKING = "working"
HERDR_STATUS_BLOCKED = "blocked"
HERDR_STATUS_IDLE = "idle"
HERDR_STATUS_DONE = "done"
HERDR_STATUS_UNKNOWN = "unknown"

HERDR_AGENT_STATUSES: frozenset[str] = frozenset(
    {
        HERDR_STATUS_WORKING,
        HERDR_STATUS_BLOCKED,
        HERDR_STATUS_IDLE,
        HERDR_STATUS_DONE,
        HERDR_STATUS_UNKNOWN,
    }
)

# --- mozyo runtime receiver-state vocabulary (core-owned, the mapping target) -
# A layer-1 runtime observation vocabulary (ACK / completion doctrine). Kept
# deliberately incapable of asserting workflow truth: no ``owner_waiting`` /
# ``review_waiting`` / close ``done`` here — those are the *derived* attention
# model's, not a terminal transport's.
RUNTIME_BUSY = "busy"  # herdr working: actively producing a turn
RUNTIME_BLOCKED = "blocked"  # herdr blocked: a permission prompt is on screen (runtime-observed)
RUNTIME_AWAITING_INPUT = "awaiting_input"  # herdr idle: quiet, waiting for input
RUNTIME_TURN_ENDED = "turn_ended"  # herdr done: assistant turn finished (NOT close/task done)
RUNTIME_UNKNOWN = "unknown"  # unreadable / unrecognised / parse failure — fail-closed

RUNTIME_RECEIVER_STATES: frozenset[str] = frozenset(
    {
        RUNTIME_BUSY,
        RUNTIME_BLOCKED,
        RUNTIME_AWAITING_INPUT,
        RUNTIME_TURN_ENDED,
        RUNTIME_UNKNOWN,
    }
)

# The pure mapping, herdr observed status -> mozyo runtime receiver-state. Every
# recognised herdr status has exactly one target; everything else fails closed
# to ``unknown`` via :func:`map_agent_status` (this table is only the recognised
# rows). ``done`` -> ``turn_ended`` (not ``done``) is the load-bearing
# fail-closed choice: it keeps the assistant-turn signal from being read as
# workflow / close ``done``.
_STATUS_TO_RUNTIME: dict[str, str] = {
    HERDR_STATUS_WORKING: RUNTIME_BUSY,
    HERDR_STATUS_BLOCKED: RUNTIME_BLOCKED,
    HERDR_STATUS_IDLE: RUNTIME_AWAITING_INPUT,
    HERDR_STATUS_DONE: RUNTIME_TURN_ENDED,
    HERDR_STATUS_UNKNOWN: RUNTIME_UNKNOWN,
}


def normalize_status(status: object) -> Optional[str]:
    """Normalise a raw herdr status token to a recognised value, or ``None``.

    A non-string, an empty / whitespace token, or a value outside
    :data:`HERDR_AGENT_STATUSES` (after a lowercase + strip) yields ``None`` —
    the caller then fails closed to ``unknown``. Never raises.
    """
    if not isinstance(status, str):
        return None
    token = status.strip().lower()
    if token in HERDR_AGENT_STATUSES:
        return token
    return None


def map_agent_status(status: object) -> str:
    """Map a herdr ``agent_status`` to a mozyo runtime receiver-state (pure).

    Total and fail-closed: any recognised herdr status maps to its runtime
    state; a non-string, an unrecognised token, or a parse-derived ``None`` all
    map to :data:`RUNTIME_UNKNOWN`. Never raises — a novel or malformed
    observation degrades to ``unknown`` rather than a confident wrong state.
    """
    normalized = normalize_status(status)
    if normalized is None:
        return RUNTIME_UNKNOWN
    # Every recognised status is in the table; ``unknown`` maps to
    # ``RUNTIME_UNKNOWN`` there too.
    return _STATUS_TO_RUNTIME[normalized]


@dataclass(frozen=True)
class AgentStateResult:
    """The structured outcome of a single agent-state read (``agent get``).

    ``ok`` is the sole authority on whether the read mechanically succeeded.
    ``state`` is *always* a member of :data:`RUNTIME_RECEIVER_STATES` so a caller
    can branch on it unconditionally; on any failure it is forced to
    :data:`RUNTIME_UNKNOWN` — a failed read may never assert a confident state.
    On failure ``reason`` is one of :data:`TRANSPORT_FAILURE_REASONS` (the same
    closed vocabulary the transport results use); on success ``reason is None``.
    ``raw_status`` is the herdr-reported token when one was parsed, kept as
    provenance only (it may be an unrecognised token that mapped to ``unknown``);
    it is ``None`` when nothing was parsed.

    Note a successful read can still carry ``state == RUNTIME_UNKNOWN``: the
    command ran and returned JSON but the status was missing / unrecognised
    (``ok=True``, an *observed* unknown), which is distinct from a mechanical
    read failure (``ok=False``, could-not-observe). Both fail closed to
    ``unknown`` for a state-only caller.
    """

    ok: bool
    state: str
    reason: Optional[str] = None
    detail: str = ""
    raw_status: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise AgentStateError(f"result 'ok' must be a bool, got {self.ok!r}")
        if self.state not in RUNTIME_RECEIVER_STATES:
            raise AgentStateError(
                f"agent state {self.state!r} is not a recognised runtime receiver "
                f"state; allowed: {sorted(RUNTIME_RECEIVER_STATES)}"
            )
        if self.ok:
            if self.reason is not None:
                raise AgentStateError(
                    f"a successful agent-state result may not carry a failure "
                    f"reason, got {self.reason!r}"
                )
            return
        # Failure: must carry a valid reason and may never assert a confident
        # state (fail closed to unknown).
        if self.reason not in TRANSPORT_FAILURE_REASONS:
            raise AgentStateError(
                f"a failed agent-state result must carry a reason from "
                f"{sorted(TRANSPORT_FAILURE_REASONS)}, got {self.reason!r}"
            )
        if self.state != RUNTIME_UNKNOWN:
            raise AgentStateError(
                f"a failed agent-state result must degrade to {RUNTIME_UNKNOWN!r}, "
                f"got {self.state!r}"
            )

    @classmethod
    def observed(
        cls, state: str, *, raw_status: Optional[str] = None
    ) -> "AgentStateResult":
        """A successful read that observed ``state`` (``raw_status`` provenance)."""
        return cls(ok=True, state=state, reason=None, raw_status=raw_status)

    @classmethod
    def failure(cls, reason: str, detail: str = "") -> "AgentStateResult":
        """A mechanically failed read; state degrades to ``unknown``."""
        return cls(ok=False, state=RUNTIME_UNKNOWN, reason=reason, detail=detail)


@dataclass(frozen=True)
class AgentStateListResult:
    """The structured outcome of an ``agent list`` read (many agents).

    ``ok`` is the sole authority on whether the list read mechanically
    succeeded. ``states`` is a tuple of ``(agent_handle, runtime_state)`` pairs,
    each ``runtime_state`` a member of :data:`RUNTIME_RECEIVER_STATES` (an entry
    with a missing / unrecognised status maps to ``unknown`` rather than being
    dropped). On failure ``ok=False``, ``states`` is empty, and ``reason`` is a
    :data:`TRANSPORT_FAILURE_REASONS` value.
    """

    ok: bool
    states: tuple[tuple[str, str], ...] = ()
    reason: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise AgentStateError(f"result 'ok' must be a bool, got {self.ok!r}")
        for entry in self.states:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or entry[1] not in RUNTIME_RECEIVER_STATES
            ):
                raise AgentStateError(
                    f"agent list entry must be a (handle, runtime_state) pair with a "
                    f"recognised state, got {entry!r}"
                )
        if self.ok:
            if self.reason is not None:
                raise AgentStateError(
                    f"a successful agent-list result may not carry a failure reason, "
                    f"got {self.reason!r}"
                )
            return
        if self.reason not in TRANSPORT_FAILURE_REASONS:
            raise AgentStateError(
                f"a failed agent-list result must carry a reason from "
                f"{sorted(TRANSPORT_FAILURE_REASONS)}, got {self.reason!r}"
            )
        if self.states:
            raise AgentStateError(
                "a failed agent-list result must carry no states"
            )

    @classmethod
    def observed(
        cls, states: tuple[tuple[str, str], ...]
    ) -> "AgentStateListResult":
        return cls(ok=True, states=tuple(states), reason=None)

    @classmethod
    def failure(cls, reason: str, detail: str = "") -> "AgentStateListResult":
        return cls(ok=False, states=(), reason=reason, detail=detail)


__all__ = (
    "HERDR_AGENT_STATUSES",
    "HERDR_STATUS_BLOCKED",
    "HERDR_STATUS_DONE",
    "HERDR_STATUS_IDLE",
    "HERDR_STATUS_UNKNOWN",
    "HERDR_STATUS_WORKING",
    "RUNTIME_AWAITING_INPUT",
    "RUNTIME_BLOCKED",
    "RUNTIME_BUSY",
    "RUNTIME_RECEIVER_STATES",
    "RUNTIME_TURN_ENDED",
    "RUNTIME_UNKNOWN",
    "AgentStateError",
    "AgentStateListResult",
    "AgentStateResult",
    "map_agent_status",
    "normalize_status",
)
