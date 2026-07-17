"""The explicit public rollback of one session-start action (Redmine #13948, j#80989).

The compensation session-start deliberately does not perform. `session-start` observes,
reports per role, records the debt — and stops (Answer j#80991: an initial launch gets no
hidden or eager close authority). This is the separate, operator-invoked rail that may
discharge that debt, and only ever for the exact panes one exact action started.

The shape is `preflight → --execute`, the same as every other destructive public rail in
this repo, because the operator must be able to see what would be closed before anything
is. The default is read-only.

Three properties make this safe to exist:

- **Bounded by identity, not by name.** The candidates are this action's recorded
  participants. A pane whose durable name matches but whose locator does not is a
  different process and is refused; an adopted slot was never a participant at all.
- **Bounded by the fences in :mod:`...domain.startup_rollback`**, re-read at action time
  and re-checked under the same held lock that spans the close.
- **Bounded by proof.** A close's return code is not evidence of absence (#13892 j#80506
  F3): after closing, the whole unit is re-measured, and only positively-proven absence
  plus a durable completion write is reported as a rollback. Anything else is a named
  non-success that leaves the record intact for a later replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_HEALTH_CHECK,
    PHASE_LAUNCHING,
    PHASE_ROLLBACK_OWED,
    StartupTransactionBusy,
    StartupTransactionError,
    StartupTransactionFence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_rollback import (  # noqa: E501
    COMPOSER_EMPTY,
    COMPOSER_PENDING,
    COMPOSER_STARTUP_BLOCKER,
    COMPOSER_UNREADABLE,
    ROLLBACK_CLOSE_TARGETS,
    ROLLBACK_DETAIL,
    ROLLBACK_ELIGIBLE,
    ROLLBACK_INVENTORY_UNREADABLE,
    ROLLBACK_SETTLED,
    ParticipantFacts,
    classify_rollback,
)

#: Refusals that are about the ACTION, not about any one participant.
REASON_OK = "ok"
REASON_ACTION_UNKNOWN = "action_unknown"
REASON_AUTHORITY_UNAVAILABLE = "rollback_authority_unavailable"
REASON_NOTHING_OWED = "nothing_owed"
REASON_ALREADY_ROLLED_BACK = "already_rolled_back"
REASON_BUSY = "rollback_busy"
REASON_BLOCKED = "rollback_blocked"
REASON_INCOMPLETE = "rollback_incomplete"
REASON_PREFLIGHT = "preflight_only"

#: Phases from which a rollback may still act — every non-terminal phase that can have
#: participants. A run is only unrecoverable once it has said, durably, how it ended.
#:
#: `launching`: died between two starts, never reached its health check. Its first agent
#: is exactly the orphan this rail exists for.
#: `health_check`: died mid-probe, or between the probe and its verdict. The phase is
#: written before the verdict is known, so this window is real and was refused with
#: `nothing_owed` — an action holding live participants that no one could converge
#: (review j#81070 R1-F5). A crash is not a claim of success.
#:
#: `planned` is absent deliberately: it is the one phase at which no side effect exists,
#: so there is nothing to compensate and no participant to close.
ACTIONABLE_PHASES: frozenset[str] = frozenset(
    {PHASE_LAUNCHING, PHASE_HEALTH_CHECK, PHASE_ROLLBACK_OWED}
)


class StartupRollbackOps(Protocol):
    """The impure seam. Narrow on purpose: five reads and one close, nothing retirement."""

    def agent_rows(self) -> Sequence[Mapping[str, object]]:
        """The live herdr inventory. Raises on an unreadable inventory (fail-closed)."""

    def runtime_state(self, locator: str) -> str:
        """The herdr runtime receiver-state, fail-soft to ``unknown``."""

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]:
        """Content-free ``(readable, has_pending)``; ``None`` pending = unreadable."""

    def startup_blocker(self, provider: str, locator: str) -> str:
        """The matched provider startup-blocker id, or ``""``. Never returns pane text."""

    def open_obligations(self, workspace_id: str, assigned_names: Sequence[str]):
        """Every covered source's blocking obligations; ``None`` = unreadable."""

    def close(self, workspace_id: str, lane_id: str, targets):
        """Close exactly ``targets`` (``(role, locator)``); returns the close result."""


@dataclass(frozen=True)
class ParticipantVerdict:
    role: str
    assigned_name: str
    locator: str
    verdict: str
    detail: str = ""
    blocker_id: str = ""
    closed: bool = False
    close_detail: str = ""

    def as_payload(self) -> dict:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "verdict": self.verdict,
            "detail": self.detail,
            "blocker_id": self.blocker_id,
            "closed": self.closed,
            "close_detail": self.close_detail,
        }


@dataclass(frozen=True)
class SessionRollbackVerdict:
    action_id: str
    state: str
    reason: str
    detail: str = ""
    executed: bool = False
    participants: tuple[ParticipantVerdict, ...] = ()

    @property
    def ok(self) -> bool:
        return self.reason in (REASON_OK, REASON_ALREADY_ROLLED_BACK)

    def as_payload(self) -> dict:
        return {
            "action_id": self.action_id,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "executed": self.executed,
            "participants": [p.as_payload() for p in self.participants],
        }


def _composer_fact(ops: StartupRollbackOps, provider: str, locator: str) -> tuple[str, str]:
    """Three-valued composer fact + the blocker id, never a bool and never pane text."""
    blocker = ""
    try:
        blocker = _norm(ops.startup_blocker(provider, locator))
    except Exception:  # noqa: BLE001 - an unclassifiable screen is never an empty one
        blocker = ""
    readable, has_pending = ops.observe_composer(locator)
    # Read the composer FIRST and admit nothing on a negative. `not (readable and
    # has_pending)` used to pass an UNREADABLE composer through as an action-owned startup
    # screen (review j#81070 R1-F3) — "we could not see any typing" is not the same fact as
    # "there is no typing", and only the second one licenses a close.
    if not readable or has_pending is None:
        return COMPOSER_UNREADABLE, blocker
    if has_pending:
        return COMPOSER_PENDING, blocker
    if blocker:
        # Positively read, positively empty, and a recognised startup screen: this action's
        # own launch put that screen there and nobody typed into it. It is NEVER answered.
        return COMPOSER_STARTUP_BLOCKER, blocker
    return COMPOSER_EMPTY, blocker


def _facts_for(
    ops: StartupRollbackOps,
    participant,
    rows,
    *,
    inventory_readable: bool,
    obligation_names: set,
    obligation_unreadable: bool,
) -> tuple[ParticipantFacts, str]:
    if not inventory_readable:
        return (
            ParticipantFacts(
                recorded_closed=participant.closed, inventory_readable=False
            ),
            "",
        )
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and _norm(row.get(AGENT_KEY_NAME)) == _norm(participant.assigned_name)
    ]
    base = dict(
        recorded_closed=participant.closed,
        inventory_readable=True,
        name_matches=len(matches),
        recorded_locator=participant.locator,
        obligation_present=participant.assigned_name in obligation_names,
        obligation_unreadable=obligation_unreadable,
    )
    if len(matches) != 1:
        return ParticipantFacts(**base), ""
    row = matches[0]
    live_locator = _norm(_agent_locator(row))
    residue = classify_named_slot(row) == SLOT_STALE
    base.update(live_locator=live_locator, shell_residue=residue)
    if residue or not live_locator or live_locator != _norm(participant.locator):
        # Never read the runtime / composer of a pane we have not established is ours,
        # and never ask a residue pane for a turn it cannot have.
        return ParticipantFacts(**base), ""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
        _SETTLED_RUNTIME_STATES,
    )

    # A live-state port (runtime state / composer) is a herdr CLI call that can raise on an
    # I/O failure (review j#81224 R7-F4). An exception here is not "idle with an empty
    # composer" — it is an UNREADABLE live state, which fails closed to a zero-close verdict
    # rather than escaping the public rail as a raw OSError.
    try:
        base["agent_idle"] = ops.runtime_state(live_locator) in _SETTLED_RUNTIME_STATES
        composer, blocker = _composer_fact(ops, participant.role, live_locator)
    except Exception:  # noqa: BLE001 - an unreadable live state is never a settled one
        base["live_state_unreadable"] = True
        return ParticipantFacts(**base), ""
    base["composer"] = composer
    return ParticipantFacts(**base), blocker


def run_session_rollback(
    *,
    action_id: str,
    ops: StartupRollbackOps,
    fence: Optional[StartupTransactionFence] = None,
    home=None,
    execute: bool = False,
) -> SessionRollbackVerdict:
    """Preflight (default) or discharge one action's rollback debt. Never raises."""
    fence = fence or StartupTransactionFence(home=home)
    try:
        action = fence.read(action_id)
    except StartupTransactionError as exc:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_AUTHORITY_UNAVAILABLE,
            detail=str(exc),
        )
    if action is None:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_ACTION_UNKNOWN,
            detail=(
                "no such startup action in this store; a rollback acts only under the "
                "identity of a run that recorded what it started"
            ),
        )
    if action.phase == PHASE_COMPLETED_ROLLED_BACK:
        # Replay: answered from the record, never by closing again.
        return SessionRollbackVerdict(
            action_id=action_id,
            state="completed",
            reason=REASON_ALREADY_ROLLED_BACK,
            detail="this action was already rolled back; nothing was closed",
        )
    if action.phase not in ACTIONABLE_PHASES:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_NOTHING_OWED,
            detail=(
                f"this action is {action.phase!r}: it owes no rollback. Refusing to close "
                "panes an action did not record as owed"
            ),
        )
    try:
        with fence._hold():
            return _rollback_locked(action_id, action, ops, fence, execute=execute)
    except StartupTransactionBusy as exc:
        return SessionRollbackVerdict(
            action_id=action_id, state="blocked", reason=REASON_BUSY, detail=str(exc)
        )
    except StartupTransactionError as exc:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_AUTHORITY_UNAVAILABLE,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - the public rail's "never raises" is a hard
        # contract (review j#81224 R7-F4). The port-specific handlers above turn a live
        # port failure into a structured verdict; this backstop guarantees that even an
        # unforeseen exception surfaces as a fail-closed refusal, never a stack trace out
        # of a destructive command. Nothing was proven closed, so the debt is intact.
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_BLOCKED,
            detail=f"the rollback could not complete ({type(exc).__name__}: {exc})",
        )


def _observe(action, ops: StartupRollbackOps) -> tuple[list, bool]:
    """Classify every participant from one action-time observation of the live world."""
    try:
        rows = list(ops.agent_rows())
        inventory_readable = True
    except Exception:  # noqa: BLE001 - an unreadable inventory is never an empty one
        rows, inventory_readable = [], False
    names = [p.assigned_name for p in action.participants]
    obligation_names: set = set()
    obligation_unreadable = False
    if inventory_readable:
        try:
            found = ops.open_obligations(action.unit.workspace_id, names)
        except Exception:  # noqa: BLE001 - fail closed, never "no obligations"
            found = None
        if found is None:
            obligation_unreadable = True
        else:
            obligation_names = {
                _norm(o.target) for o in found if getattr(o, "blocks", False)
            }
    verdicts = []
    for participant in action.participants:
        facts, blocker = _facts_for(
            ops,
            participant,
            rows,
            inventory_readable=inventory_readable,
            obligation_names=obligation_names,
            obligation_unreadable=obligation_unreadable,
        )
        verdict = classify_rollback(facts)
        verdicts.append(
            ParticipantVerdict(
                role=participant.role,
                assigned_name=participant.assigned_name,
                locator=participant.locator,
                verdict=verdict,
                detail=ROLLBACK_DETAIL.get(verdict, ""),
                blocker_id=blocker if verdict == ROLLBACK_ELIGIBLE else "",
                closed=participant.closed,
            )
        )
    return verdicts, inventory_readable


def _rollback_locked(action_id, action, ops, fence, *, execute: bool):
    # Re-read the action FRESH under the lock and act only on this snapshot (review j#81224
    # R7-F1). The pre-lock read outside is a fast-path preflight; a concurrent holder can
    # terminalize the action, change its participants, or delete it between that read and
    # the lock, and closing panes on the stale object would re-close a settled authority
    # (the TOCTOU the nonblocking lock exists to prevent). Everything below decides from
    # `action`, the under-lock snapshot — never the caller's stale one.
    action = fence.read(action_id)
    if action is None:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_ACTION_UNKNOWN,
            detail="the action vanished before the lock was held; nothing was closed",
        )
    if action.phase == PHASE_COMPLETED_ROLLED_BACK:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="completed",
            reason=REASON_ALREADY_ROLLED_BACK,
            detail="a concurrent rollback completed this action; nothing was closed",
        )
    if action.phase not in ACTIONABLE_PHASES:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_NOTHING_OWED,
            detail=(
                f"under the lock this action is {action.phase!r}: it owes no rollback. "
                "Refusing to close panes an action did not record as owed"
            ),
        )
    verdicts, inventory_readable = _observe(action, ops)
    if not inventory_readable:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_BLOCKED,
            detail=ROLLBACK_DETAIL[ROLLBACK_INVENTORY_UNREADABLE],
            participants=tuple(verdicts),
        )
    # SETTLED (`already_closed` / `absent`) is not blocked: a previous attempt of this same
    # action proved that participant gone, or it never came up. Treating either as a
    # blocker is how an interrupted rollback becomes permanently stuck — the #13847 R1-F1 /
    # #13892 partial-close discipline, re-derived here because this rail resumes too.
    blocked = [v for v in verdicts if v.verdict not in ROLLBACK_SETTLED]
    if not execute:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="actionable" if not blocked else "blocked",
            reason=REASON_PREFLIGHT,
            detail=(
                "read-only preflight; nothing was closed. Re-run with --execute to "
                "discharge this action's rollback debt."
            ),
            participants=tuple(verdicts),
        )
    if blocked:
        # All-or-nothing on intent, not on effect: a pair whose sibling must be preserved
        # is reported, and no half-close is performed behind the operator's back.
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_BLOCKED,
            detail=(
                "at least one participant may not be closed; nothing was closed. Resolve "
                "the named cause (or retire the pair through its own rail) and re-run."
            ),
            participants=tuple(verdicts),
        )
    return _execute_rollback(action_id, action, ops, fence, verdicts)


def _execute_rollback(action_id, action, ops, fence, verdicts):
    targets = [
        (v.role, v.locator)
        for v in verdicts
        if not v.closed and v.locator and _live_target(action, v)
    ]
    settled = list(verdicts)
    failed: dict = {}
    if targets:
        # The close port can raise AFTER a partial effect (review j#81224 R7-F4): some
        # panes may already be gone. Do NOT let that escape the public rail raw — the
        # remeasure below is what establishes the real end state, so a close exception is
        # recorded as a whole-batch failure detail and the remeasure decides per role.
        try:
            result = ops.close(action.unit.workspace_id, action.unit.lane_id, targets)
            failed = {role: detail for role, _, detail in getattr(result, "failed", ())}
        except Exception as exc:  # noqa: BLE001 - a close that raised is a close that may
            # have partially acted; the remeasure, not this exception, decides the outcome.
            failed = {role: f"close raised: {exc}" for role, _ in targets}
    # A close's return code is not evidence of absence (#13892 j#80506 F3), so the durable
    # `closed` flag is written from the REMEASURE, never from the close's own report
    # (review j#81070 R1-F4). Believing the report first recorded `closed=True` for a pane
    # that was still live, and the next replay then skipped it as already-settled — the
    # participant could never be closed again. Absence is the only thing that proves a
    # close, and only the remeasure can see it.
    residue, remeasure_ok = _residual_participants(action, ops)
    if remeasure_ok:
        proven_gone = {
            v.role
            for v in verdicts
            if v.assigned_name not in residue and v.verdict in ROLLBACK_SETTLED
        }
        settled = [
            ParticipantVerdict(
                role=v.role,
                assigned_name=v.assigned_name,
                locator=v.locator,
                verdict=v.verdict,
                detail=v.detail,
                blocker_id=v.blocker_id,
                closed=v.closed or v.role in proven_gone,
                close_detail=failed.get(v.role, ""),
            )
            for v in verdicts
        ]
        for role in proven_gone:
            fence.mark_closed(action_id, role)
    if not remeasure_ok:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="incomplete",
            reason=REASON_INCOMPLETE,
            detail=(
                "the post-close inventory could not be read, so this rollback cannot be "
                "proven; the action stays owed and is safe to re-run"
            ),
            executed=True,
            participants=tuple(settled),
        )
    if residue:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="incomplete",
            reason=REASON_INCOMPLETE,
            detail=(
                f"still live after the close: {', '.join(sorted(residue))}; the action "
                "stays owed and is safe to re-run"
            ),
            executed=True,
            participants=tuple(settled),
        )
    try:
        fence.set_phase(action_id, PHASE_COMPLETED_ROLLED_BACK)
    except StartupTransactionError as exc:
        # The panes ARE gone; we simply cannot prove it durably. Withhold the success
        # rather than fabricate it — there is no capacity leak either way (#13892 j#80526).
        return SessionRollbackVerdict(
            action_id=action_id,
            state="incomplete",
            reason=REASON_INCOMPLETE,
            detail=f"the rollback completed but its record could not be written ({exc})",
            executed=True,
            participants=tuple(settled),
        )
    return SessionRollbackVerdict(
        action_id=action_id,
        state="completed",
        reason=REASON_OK,
        detail="every participant of this action is proven absent",
        executed=True,
        participants=tuple(settled),
    )


def _live_target(action, verdict) -> bool:
    """Only close a participant an action-time observation actually found LIVE and ours.

    Keyed on the closed set of close-target verdicts, never on "settled" (review j#81070
    R1-F2). `absent` is settled and must not be a target: the recorded locator is an
    address this action once launched at, and handing it to close after the name is gone
    closed whoever had since taken that pane id.
    """
    return verdict.verdict in ROLLBACK_CLOSE_TARGETS and not verdict.closed


def _residual_participants(action, ops) -> tuple[set, bool]:
    """Fresh whole-unit re-measure: which participants are STILL live (positive proof)."""
    try:
        rows = list(ops.agent_rows())
    except Exception:  # noqa: BLE001 - an unreadable remeasure proves nothing
        return set(), False
    live = {
        _norm(row.get(AGENT_KEY_NAME))
        for row in rows
        if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME))
    }
    return {p.assigned_name for p in action.participants if p.assigned_name in live}, True


__all__ = (
    "ACTIONABLE_PHASES",
    "REASON_ACTION_UNKNOWN",
    "REASON_ALREADY_ROLLED_BACK",
    "REASON_AUTHORITY_UNAVAILABLE",
    "REASON_BLOCKED",
    "REASON_BUSY",
    "REASON_INCOMPLETE",
    "REASON_NOTHING_OWED",
    "REASON_OK",
    "REASON_PREFLIGHT",
    "ParticipantVerdict",
    "SessionRollbackVerdict",
    "StartupRollbackOps",
    "run_session_rollback",
)
