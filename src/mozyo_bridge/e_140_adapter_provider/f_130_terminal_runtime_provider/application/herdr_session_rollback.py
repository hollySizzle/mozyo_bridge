"""Explicit public rollback of one exact session-start action (#13948).

The default is read-only. Execute requires terminal-bound v4/v2 authority from one
globally canonical inventory snapshot and a server-side conditional-close capability
before mutating a present normal agent. Pane-bound-v2 prepared shells use their private
receipt terminal under the same conditional-close rule; structured pane-bound-v1 receipts
can prove positive absence only. The current Herdr provider exposes no conditional-close
primitive, so every present participant is preserved. Private terminal values never enter
public verdicts, payloads, reprs, or provider error detail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for

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
    AGENT_KEY_TERMINAL_ID,
    _agent_locator,
    _norm,
    _norm_lane,
    terminal_identity_of_row,
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
    ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE,
    ROLLBACK_DETAIL,
    ROLLBACK_ABSENT,
    ROLLBACK_ALREADY_CLOSED,
    ROLLBACK_ELIGIBLE,
    ROLLBACK_INVENTORY_UNREADABLE,
    ROLLBACK_OBLIGATION_UNREADABLE,
    ROLLBACK_SETTLED,
    ROLLBACK_WORK_OBLIGATION,
    ParticipantFacts,
    classify_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    PaneBoundReceiptError,
    parse_pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_contract import (  # noqa: E501
    ParticipantVerdict as ContractParticipantVerdict,
    PreparedPaneObservation as ContractPreparedPaneObservation,
    REASON_CONDITIONAL_CLOSE_UNAVAILABLE,
    ROLLBACK_PREPARED_TERMINAL_MISMATCH,
    SessionRollbackVerdict as ContractSessionRollbackVerdict,
    StartupRollbackAgentTarget,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_identity import (  # noqa: E501
    PREPARED_PANE_ABSENT,
    PREPARED_PANE_PRESENT,
    PREPARED_PANE_UNREADABLE,
    ROLLBACK_PREPARED_NATIVE_MISMATCH,
    ROLLBACK_PREPARED_PANE_UNVERIFIABLE,
    ROLLBACK_PREPARED_RECEIPT_INVALID,
    historical_agent_generation_state as _historical_agent_generation_state,
    inventory_identity_complete as _inventory_identity_complete,
    name_matches as _name_matches,
    prepared_pane_verdict as _prepared_pane_verdict,
    terminal_bound_action_target as _terminal_bound_action_target,
    terminal_bound_action_target_absent as _terminal_bound_action_target_absent,
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

#: Non-terminal phases that may already carry participants and therefore rollback debt.
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

    def supports_conditional_close(self) -> bool:
        """Literal true only for a server-side generation-conditional close."""

    def close_agent_participant(
        self, *, workspace_id: str, lane_id: str, target: StartupRollbackAgentTarget
    ) -> tuple[bool, str]:
        """Close exactly the native/terminal-bound v2 participant generation."""

    def close_prepared_pane(
        self,
        *,
        locator: str,
        workspace_id: str,
        tab_id: str,
        expected_terminal_id: str = "",
    ) -> tuple[bool, str]:
        """Conditionally close the exact pane-bound-v2 terminal generation."""

    def current_generation_targets_absent(self, action, targets, *, store_home: Path) -> bool:
        """Prove every normal target's terminal-bound generation is globally absent."""

    def prepared_pane(
        self, *, locator: str, workspace_id: str, tab_id: str,
        expected_terminal_id: str = "",
    ) -> "PreparedPaneObservation":
        """Observe one action-recorded shell pane without interpreting its contents."""


# The public wire/result contract is shared with the newer conditional-close rail. Keep
# this module's historical import surface while using the one canonical dataclass shape.
PreparedPaneObservation = ContractPreparedPaneObservation
ParticipantVerdict = ContractParticipantVerdict
SessionRollbackVerdict = ContractSessionRollbackVerdict


def _supports_conditional_close(ops: StartupRollbackOps) -> bool:
    try:
        return ops.supports_conditional_close() is True
    except Exception:  # noqa: BLE001 - failed capability discovery grants no close
        return False


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
    action,
    store_home: Path,
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
    absence_bound = not matches and _terminal_bound_action_target_absent(
        store_home, action, participant, rows
    )
    base = dict(
        recorded_closed=participant.closed and absence_bound,
        absence_generation_bound=absence_bound,
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
    if not _terminal_bound_action_target(
        store_home, action, participant, rows, live_locator
    ):
        base["live_state_unreadable"] = True
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
    except StartupTransactionError:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_AUTHORITY_UNAVAILABLE,
            detail="startup rollback authority is unreadable",
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
        if not _completed_rollback_absent(action, ops, Path(fence.path).parent):
            return SessionRollbackVerdict(
                action_id=action_id, state="incomplete", reason=REASON_INCOMPLETE,
                detail="completed rollback lacks fresh terminal-bound absence proof",
            )
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
    # The snapshot the operator's command is scoped to, captured BEFORE the lock. The
    # under-lock re-read must match this exactly (review j#81244 R8-F1): a concurrent
    # `record_participant` between this read and the lock would otherwise let the same
    # command close a participant added after the operator decided to run it.
    pre_lock = _action_fingerprint(action)
    try:
        with fence._hold():
            return _rollback_locked(
                action_id, pre_lock, ops, fence, execute=execute
            )
    except StartupTransactionBusy:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_BUSY,
            detail="startup rollback authority is busy",
        )
    except StartupTransactionError:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_AUTHORITY_UNAVAILABLE,
            detail="startup rollback authority became unavailable",
        )
    except Exception:  # noqa: BLE001 - the public rail's "never raises" is a hard
        # contract (review j#81224 R7-F4). The port-specific handlers above turn a live
        # port failure into a structured verdict; this backstop guarantees that even an
        # unforeseen exception surfaces as a fail-closed refusal, never a stack trace out
        # of a destructive command. Nothing was proven closed, so the debt is intact.
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_BLOCKED,
            detail="startup rollback execution failed",
        )


def _observe(action, ops: StartupRollbackOps, *, store_home: Path) -> tuple[list, bool]:
    """Classify every participant from one action-time observation of the live world."""
    conditional_close_supported = _supports_conditional_close(ops)
    try:
        rows = list(ops.agent_rows())
        inventory_readable = _inventory_identity_complete(rows)
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
        try:
            pane_receipt = parse_pane_bound_receipt(participant.receipt)
        except PaneBoundReceiptError:
            verdicts.append(
                ParticipantVerdict(
                    role=participant.role,
                    assigned_name=participant.assigned_name,
                    locator=participant.locator,
                    verdict=ROLLBACK_PREPARED_RECEIPT_INVALID,
                    detail=(
                        "the participant claims pane-bound authority but its receipt is "
                        "invalid; refusing to reinterpret it as a legacy launch"
                    ),
                    closed=participant.closed,
                    # A durable closed flag is replay authority even when an old or
                    # corrupted receipt can no longer be decoded. Never revisit a
                    # locator that Herdr may since have reused.
                    prepared_pane=not participant.closed,
                )
            )
            continue
        name_matches = _name_matches(participant, rows) if inventory_readable else []
        historical_generation = (
            _historical_agent_generation_state(
                store_home, action, participant, rows
            )
            if inventory_readable and not name_matches
            else "none"
        )
        if pane_receipt is None and not participant.closed and name_matches:
            verdicts.append(
                ParticipantVerdict(
                    role=participant.role,
                    assigned_name=participant.assigned_name,
                    locator=participant.locator,
                    verdict=ROLLBACK_PREPARED_TERMINAL_MISMATCH,
                    detail=(
                        "the legacy startup receipt has no terminal identity; a present "
                        "agent cannot be conditionally closed as this action's generation"
                    ),
                    closed=False,
                )
            )
            continue
        if (
            pane_receipt is not None
            and inventory_readable
            and not name_matches
            and historical_generation == "blocked"
        ):
            verdicts.append(
                ParticipantVerdict(
                    role=participant.role,
                    assigned_name=participant.assigned_name,
                    locator=participant.locator,
                    verdict=ROLLBACK_PREPARED_TERMINAL_MISMATCH,
                    detail="the recorded agent generation lacks exact global absence proof",
                    closed=participant.closed,
                )
            )
            continue
        if (
            pane_receipt is not None
            and inventory_readable
            and not name_matches
            and historical_generation == "none"
        ):
            verdicts.append(
                _prepared_pane_verdict(
                    ops,
                    participant,
                    pane_receipt,
                    inventory_readable=inventory_readable,
                    obligation_names=obligation_names,
                    obligation_unreadable=obligation_unreadable,
                    conditional_close_supported=conditional_close_supported,
                    inventory_rows=rows,
                )
            )
            continue
        if (
            pane_receipt is not None
            and not participant.closed
            and len(name_matches) == 1
            and (
                pane_receipt.native_name != native_name_for(participant.assigned_name)
                or name_matches[0].get("native_name") != pane_receipt.native_name
            )
        ):
            # A pane-bound action launched the short native identity recorded in its
            # receipt. Logical-name + locator equality alone cannot upgrade a legacy row
            # (or another native generation) into that action's close authority.
            verdicts.append(
                ParticipantVerdict(
                    role=participant.role,
                    assigned_name=participant.assigned_name,
                    locator=participant.locator,
                    verdict=ROLLBACK_PREPARED_NATIVE_MISMATCH,
                    detail=(
                        "the live logical agent row does not carry the exact Herdr native "
                        "identity recorded by this pane-bound startup action"
                    ),
                    closed=False,
                )
            )
            continue
        if (
            pane_receipt is not None
            and not participant.closed
            and len(name_matches) == 1
            and (
                not pane_receipt.terminal_id
                or name_matches[0].get(AGENT_KEY_TERMINAL_ID)
                != pane_receipt.terminal_id
            )
        ):
            verdicts.append(
                ParticipantVerdict(
                    role=participant.role,
                    assigned_name=participant.assigned_name,
                    locator=participant.locator,
                    verdict=ROLLBACK_PREPARED_TERMINAL_MISMATCH,
                    detail=(
                        "the live agent does not carry the exact terminal identity "
                        "recorded by this pane-bound startup action"
                    ),
                    closed=False,
                )
            )
            continue
        facts, blocker = _facts_for(
            ops,
            participant,
            rows,
            inventory_readable=inventory_readable,
            obligation_names=obligation_names,
            obligation_unreadable=obligation_unreadable,
            action=action,
            store_home=store_home,
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


def _action_fingerprint(action):
    """The action's WHOLE authority content, so no field can be omitted (review j#81254
    R9-F1).

    A hand-picked subset was the R8 mistake: it left out the unit identity
    (``workspace_id`` / ``lane_id`` / ``providers``) — which is the very scope
    ``ops.close`` runs against — and the participant ``receipt``, so a concurrent change to
    either passed the comparison and a rollback closed against a different unit. The
    fingerprint is now the complete durable record (``as_payload``): action id, unit,
    phase, revision, every participant field including receipt, and the timestamps. Two
    reads whose payloads are equal saw the byte-identical authority; any difference at all
    is a concurrent change the operator's command was not scoped to. The rollback's own
    writes happen AFTER this comparison, so a healthy run and a partial resume both match.
    """
    return action.as_authority_payload()


def _rollback_locked(action_id, pre_lock, ops, fence, *, execute: bool):
    # Re-read the action FRESH under the lock and act only on this snapshot (review j#81224
    # R7-F1). The pre-lock read outside is a fast-path preflight; a concurrent holder can
    # terminalize the action, change its participants, or delete it between that read and
    # the lock, and closing panes on the stale object would re-close a settled authority
    # or a newly-added one (the TOCTOU the nonblocking lock exists to prevent).
    action = fence.read(action_id)
    if action is None:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_ACTION_UNKNOWN,
            detail="the action vanished before the lock was held; nothing was closed",
        )
    if action.phase == PHASE_COMPLETED_ROLLED_BACK:
        if not _completed_rollback_absent(action, ops, Path(fence.path).parent):
            return SessionRollbackVerdict(
                action_id=action_id, state="incomplete", reason=REASON_INCOMPLETE,
                detail="completed rollback lacks fresh terminal-bound absence proof",
            )
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
    # The under-lock snapshot must match what the operator's command was scoped to (review
    # j#81244 R8-F1). A concurrent `record_participant` that added a role between the
    # pre-lock read and this one changes the revision and participant set; closing the new
    # participant would destroy a pane added after the operator decided to run — so any
    # divergence is a structured refusal, and the operator re-preflights against the new
    # shape. (A phase-only change is already handled by the checks above.)
    if _action_fingerprint(action) != pre_lock:
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_BLOCKED,
            detail=(
                "this action changed between the read and the lock (a concurrent launch "
                "recorded or a state moved); nothing was closed. Re-run to preflight the "
                "current shape."
            ),
        )
    verdicts, inventory_readable = _observe(
        action, ops, store_home=Path(fence.path).parent
    )
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
    conditional_unavailable = bool(
        any(
            verdict.verdict == ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE
            for verdict in verdicts
        )
        or (
            any(_live_target(action, verdict) for verdict in verdicts)
            and not _supports_conditional_close(ops)
        )
    )
    if conditional_unavailable:
        verdicts = [
            ParticipantVerdict(
                role=v.role,
                assigned_name=v.assigned_name,
                locator=v.locator,
                verdict=(
                    ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE
                    if _live_target(action, v)
                    else v.verdict
                ),
                detail=(
                    ROLLBACK_DETAIL[ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE]
                    if _live_target(action, v)
                    else v.detail
                ),
                blocker_id=v.blocker_id,
                closed=v.closed,
                close_detail=v.close_detail,
                prepared_pane=v.prepared_pane,
            )
            for v in verdicts
        ]
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
            reason=(
                REASON_CONDITIONAL_CLOSE_UNAVAILABLE
                if conditional_unavailable
                else REASON_BLOCKED
            ),
            detail=(
                ROLLBACK_DETAIL[ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE]
                if conditional_unavailable
                else (
                    "at least one participant may not be closed; nothing was closed. Resolve "
                    "the named cause (or retire the pair through its own rail) and re-run."
                )
            ),
            participants=tuple(verdicts),
        )
    return _execute_rollback(
        action_id, action, ops, fence, verdicts, store_home=Path(fence.path).parent
    )


def _execute_rollback(action_id, action, ops, fence, verdicts, *, store_home):
    targets = [
        (v.role, v.locator)
        for v in verdicts
        if not v.prepared_pane
        and not v.closed
        and v.locator
        and _live_target(action, v)
    ]
    participants = {p.role: p for p in action.participants}
    prepared_targets = [
        v
        for v in verdicts
        if v.prepared_pane
        and not v.closed
        and v.locator
        and _live_target(action, v)
    ]
    if (targets or prepared_targets) and not _supports_conditional_close(ops):
        return SessionRollbackVerdict(
            action_id=action_id,
            state="blocked",
            reason=REASON_CONDITIONAL_CLOSE_UNAVAILABLE,
            detail=ROLLBACK_DETAIL[ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE],
            executed=False,
            participants=tuple(verdicts),
        )
    settled = list(verdicts)
    failed: dict = {}
    if targets:
        # The close port can raise AFTER a partial effect (review j#81224 R7-F4): some
        # panes may already be gone. Do NOT let that escape the public rail raw — the
        # remeasure below is what establishes the real end state, so a close exception is
        # recorded as a whole-batch failure detail and the remeasure decides per role.
        for role, locator in targets:
            participant = participants[role]
            try:
                receipt = parse_pane_bound_receipt(participant.receipt)
            except PaneBoundReceiptError:
                receipt = None
            if receipt is None or not receipt.terminal_id:
                failed[role] = "terminal-bound pane close failed"
                continue
            target = StartupRollbackAgentTarget(
                role=role,
                assigned_name=participant.assigned_name,
                locator=locator,
                native_name=receipt.native_name,
                terminal_id=receipt.terminal_id,
            )
            try:
                ok, _detail = ops.close_agent_participant(
                    workspace_id=action.unit.workspace_id,
                    lane_id=action.unit.lane_id,
                    target=target,
                )
            except Exception:  # noqa: BLE001 - provider errors are value-free below
                ok = False
            if not ok:
                failed[role] = "terminal-bound pane close failed"
    for verdict in prepared_targets:
        participant = participants[verdict.role]
        try:
            receipt = parse_pane_bound_receipt(participant.receipt)
        except PaneBoundReceiptError:
            receipt = None
        if receipt is None or not receipt.terminal_id:
            failed[verdict.role] = "terminal-bound pane close failed"
            continue
        try:
            ok, _detail = ops.close_prepared_pane(
                locator=verdict.locator,
                workspace_id=receipt.workspace_id,
                tab_id=receipt.tab_id,
                expected_terminal_id=receipt.terminal_id,
            )
        except Exception:  # noqa: BLE001 - provider errors are value-free below
            ok = False
        if not ok:
            failed[verdict.role] = "terminal-bound pane close failed"
    settled = [
        ParticipantVerdict(
            role=v.role, assigned_name=v.assigned_name, locator=v.locator,
            verdict=v.verdict, detail=v.detail, blocker_id=v.blocker_id,
            closed=v.closed, close_detail=failed.get(v.role, ""),
            prepared_pane=v.prepared_pane,
        )
        for v in verdicts
    ]
    # A close's return code is not evidence of absence (#13892 j#80506 F3), so the durable
    # `closed` flag is written from the REMEASURE, never from the close's own report
    # (review j#81070 R1-F4). Believing the report first recorded `closed=True` for a pane
    # that was still live, and the next replay then skipped it as already-settled — the
    # participant could never be closed again. Absence is the only thing that proves a
    # close, and only the remeasure can see it.
    residue, remeasure_ok = _residual_participants(action, ops, verdicts)
    normal_targets = [
        (verdict.role, verdict.locator)
        for verdict in verdicts
        if (
            not verdict.prepared_pane
            and verdict.locator
            and verdict.assigned_name not in residue
        )
    ]
    if normal_targets and remeasure_ok:
        try:
            remeasure_ok = bool(
                ops.current_generation_targets_absent(
                    action, normal_targets, store_home=store_home
                )
            )
        except Exception:  # noqa: BLE001 - unreadable absence proof completes nothing
            remeasure_ok = False
    if remeasure_ok:
        proven_gone = {
            v.role
            for v in verdicts
            if v.assigned_name not in residue
            and (v.verdict in ROLLBACK_SETTLED or _live_target(action, v))
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
            for v in settled
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
    except StartupTransactionError:
        # The panes ARE gone; we simply cannot prove it durably. Withhold the success
        # rather than fabricate it — there is no capacity leak either way (#13892 j#80526).
        return SessionRollbackVerdict(
            action_id=action_id,
            state="incomplete",
            reason=REASON_INCOMPLETE,
            detail="the rollback completed but its record could not be written",
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


def _residual_participants(action, ops, verdicts=()) -> tuple[set, bool]:
    """Fresh whole-unit re-measure: which participants are STILL live (positive proof)."""
    try:
        rows = list(ops.agent_rows())
    except Exception:  # noqa: BLE001 - an unreadable remeasure proves nothing
        return set(), False
    if not _inventory_identity_complete(rows):
        return set(), False
    live = {
        _norm(row.get(AGENT_KEY_NAME))
        for row in rows
        if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME))
    }
    residue = {p.assigned_name for p in action.participants if p.assigned_name in live}
    verdict_by_role = {v.role: v for v in verdicts}
    for participant in action.participants:
        verdict = verdict_by_role.get(participant.role)
        if participant.assigned_name in residue or not getattr(
            verdict, "prepared_pane", False
        ):
            continue
        try:
            receipt = parse_pane_bound_receipt(participant.receipt)
            if receipt is None:
                return residue, False
            observation = ops.prepared_pane(
                locator=participant.locator,
                workspace_id=receipt.workspace_id,
                tab_id=receipt.tab_id,
                expected_terminal_id=receipt.terminal_id,
            )
        except Exception:  # noqa: BLE001 - an unreadable post-close pane proves nothing
            return residue, False
        if observation.state == PREPARED_PANE_ABSENT:
            if receipt.terminal_id and observation.terminal_reclaimed is not False:
                return residue, False
            if receipt.terminal_id and any(
                terminal_identity_of_row(row) == receipt.terminal_id for row in rows
            ):
                return residue, False
            continue
        residue.add(participant.assigned_name)
        if observation.state not in {
            PREPARED_PANE_PRESENT,
            PREPARED_PANE_UNREADABLE,
        }:
            return residue, False
        if observation.state == PREPARED_PANE_UNREADABLE:
            return residue, False
    return residue, True


def _completed_rollback_absent(action, ops, store_home: Path) -> bool:
    """Revalidate every old-writer completion; durable closed bits are audit only."""
    try:
        rows = tuple(ops.agent_rows())
        if not _inventory_identity_complete(rows):
            return False
        for participant in action.participants:
            receipt = parse_pane_bound_receipt(participant.receipt)
            historical_generation = _historical_agent_generation_state(
                store_home, action, participant, rows
            )
            if historical_generation == "absent":
                continue
            if historical_generation == "blocked" or receipt is None:
                return False
            if receipt.terminal_id and any(
                terminal_identity_of_row(row) == receipt.terminal_id for row in rows
            ):
                return False
            observation = ops.prepared_pane(
                locator=participant.locator,
                workspace_id=receipt.workspace_id,
                tab_id=receipt.tab_id,
                expected_terminal_id=receipt.terminal_id,
            )
            if (
                observation.state != PREPARED_PANE_ABSENT
                or (
                    receipt.terminal_id
                    and observation.terminal_reclaimed is not False
                )
            ):
                return False
        return True
    except Exception:  # noqa: BLE001 - replay completion requires positive fresh proof
        return False


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
    "PREPARED_PANE_ABSENT",
    "PREPARED_PANE_PRESENT",
    "PREPARED_PANE_UNREADABLE",
    "ROLLBACK_PREPARED_PANE_UNVERIFIABLE",
    "ROLLBACK_PREPARED_NATIVE_MISMATCH",
    "ROLLBACK_PREPARED_RECEIPT_INVALID",
    "ParticipantVerdict",
    "PreparedPaneObservation",
    "SessionRollbackVerdict",
    "StartupRollbackOps",
    "run_session_rollback",
)
