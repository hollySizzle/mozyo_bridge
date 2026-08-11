"""Explicit public rollback of one exact session-start action (#13948).

The default is read-only. Execute closes only terminal-bound current generations, then
requires positive absence before recording completion.

Herdr currently exposes no atomic terminal-identity compare-and-close primitive. Normal
agent rollback therefore joins terminal-bound v4/v2 authority in one action-time inventory
observation before the locator-only close, but cannot claim race-free provider atomicity.
Prepared shell panes have no terminal/generation pin at all and are consequently
observation-only: even a synthetic ``input_empty=True`` cannot authorize their close.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    HerdrLaunchGenerationStore,
)

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
    _norm_lane,
    terminal_identity_of_live_slot,
    terminal_identity_of_row,
    terminal_identity_snapshot_complete,
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

#: Prepared panes have no terminal pin and are observation-only.
PREPARED_PANE_PRESENT = "present"
PREPARED_PANE_ABSENT = "absent"
PREPARED_PANE_UNREADABLE = "unreadable"
ROLLBACK_PREPARED_PANE_UNVERIFIABLE = "prepared_pane_unverifiable"
ROLLBACK_PREPARED_RECEIPT_INVALID = "prepared_pane_receipt_invalid"
ROLLBACK_PREPARED_NATIVE_MISMATCH = "prepared_pane_native_identity_mismatch"

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

    def close_current_generation(self, action, targets, *, store_home: Path):
        """Freshly rejoin every target to ``action`` and close the exact batch."""

    def current_generation_targets_absent(self, action, targets, *, store_home: Path) -> bool:
        """Prove every normal target's terminal-bound generation is globally absent."""

    def prepared_pane(
        self, *, locator: str, workspace_id: str, tab_id: str
    ) -> "PreparedPaneObservation":
        """Observe one action-recorded shell pane without interpreting its contents."""


@dataclass(frozen=True)
class PreparedPaneObservation:
    """Positive facts about a pane that exists before ``agent start``.

    ``input_empty`` is deliberately a three-valued fact.  Only literal ``True`` may
    authorize close; ``None`` means the selected Herdr runtime exposes no authoritative
    input-state surface and therefore fails closed.
    """

    state: str
    locator: str = ""
    workspace_id: str = ""
    tab_id: str = ""
    agent_absent: bool = False
    shell_only: bool = False
    input_empty: Optional[bool] = None
    detail: str = ""


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
    #: Internal execution mode only.  The public payload remains byte-compatible.
    prepared_pane: bool = False

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


def _terminal_bound_action_target(store_home, action, participant, rows, locator) -> bool:
    """Join one live slot to this exact startup action without exposing its terminal id."""
    live_terminal_id = terminal_identity_of_live_slot(
        participant.assigned_name, locator, rows
    )
    try:
        attestation = HerdrIdentityAttestationStore(home=store_home).read(
            participant.assigned_name
        )
        generation = HerdrLaunchGenerationStore(home=store_home).read(
            participant.assigned_name
        )
    except Exception:  # noqa: BLE001 - unreadable current authority never licenses close
        return False
    attested = evaluate_attestation(
        attestation,
        live_locator=locator,
        live_terminal_id=live_terminal_id,
        expected_workspace_id=action.unit.workspace_id,
        expected_role=participant.role,
        expected_lane=action.unit.lane_id,
    )
    return bool(
        attested.ok
        and generation is not None
        and _norm(getattr(generation, "phase", "")) == GENERATION_ATTESTED
        and _norm(getattr(generation, "verdict", "")) == VERDICT_PRESENT
        and _norm(getattr(generation, "startup_action_id", ""))
        == _norm(action.action_id)
        and _norm(getattr(generation, "assigned_name", ""))
        == _norm(participant.assigned_name)
        and _norm(getattr(generation, "workspace_id", ""))
        == _norm(action.unit.workspace_id)
        and _norm(getattr(generation, "role", "")) == _norm(participant.role)
        and _norm_lane(getattr(generation, "lane_id", ""))
        == _norm_lane(action.unit.lane_id)
        and _norm(getattr(generation, "locator", "")) == locator
        and getattr(generation, "terminal_id", "") == live_terminal_id
    )


def _terminal_bound_action_target_absent(store_home, action, participant, rows) -> bool:
    """Prove one recorded normal participant's private generation terminal absent."""
    try:
        snapshot = tuple(rows)
        if not terminal_identity_snapshot_complete(snapshot):
            return False
        generation = HerdrLaunchGenerationStore(home=store_home).read(
            participant.assigned_name
        )
        attestation = HerdrIdentityAttestationStore(home=store_home).read(
            participant.assigned_name
        )
    except Exception:  # noqa: BLE001 - historical absence is a positive proof
        return False
    terminal_id = getattr(generation, "terminal_id", "")
    return bool(
        generation is not None
        and _norm(getattr(generation, "phase", "")) == GENERATION_ATTESTED
        and _norm(getattr(generation, "verdict", "")) == VERDICT_PRESENT
        and _norm(getattr(generation, "startup_action_id", "")) == _norm(action.action_id)
        and _norm(getattr(generation, "assigned_name", ""))
        == _norm(participant.assigned_name)
        and _norm(getattr(generation, "workspace_id", ""))
        == _norm(action.unit.workspace_id)
        and _norm(getattr(generation, "role", "")) == _norm(participant.role)
        and _norm_lane(getattr(generation, "lane_id", ""))
        == _norm_lane(action.unit.lane_id)
        and _norm(getattr(generation, "locator", "")) == _norm(participant.locator)
        and type(terminal_id) is str and terminal_id and terminal_id.strip() == terminal_id
        and evaluate_attestation(
            attestation,
            live_locator=participant.locator,
            live_terminal_id=terminal_id,
            expected_workspace_id=action.unit.workspace_id,
            expected_role=participant.role,
            expected_lane=action.unit.lane_id,
        ).ok
        and not any(
            row.get(AGENT_KEY_NAME) == participant.assigned_name
            or _agent_locator(row) == participant.locator
            or terminal_identity_of_row(row) == terminal_id
            for row in snapshot
        )
    )


def _name_matches(participant, rows) -> list[Mapping[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, Mapping)
        and _norm(row.get(AGENT_KEY_NAME)) == _norm(participant.assigned_name)
    ]


def _inventory_identity_complete(rows) -> bool:
    """Require one complete globally unique terminal-identity snapshot."""
    return terminal_identity_snapshot_complete(rows)


def _prepared_pane_verdict(
    ops: StartupRollbackOps,
    participant,
    receipt,
    *,
    inventory_readable: bool,
    obligation_names: set,
    obligation_unreadable: bool,
) -> ParticipantVerdict:
    """Classify a receipt-bound pane whose logical agent row is absent.

    The old agent-only rule treated an absent agent row as a settled participant.  That
    is false during Herdr 0.8's split-before-start interval: the shell pane is already a
    side effect.  It is closeable only from every positive fact below; an unavailable
    input-state fact therefore preserves the pane.
    """
    if not inventory_readable:
        verdict = ROLLBACK_INVENTORY_UNREADABLE
        detail = ROLLBACK_DETAIL[verdict]
    elif obligation_unreadable:
        verdict = ROLLBACK_OBLIGATION_UNREADABLE
        detail = ROLLBACK_DETAIL[verdict]
    elif participant.assigned_name in obligation_names:
        verdict = ROLLBACK_WORK_OBLIGATION
        detail = ROLLBACK_DETAIL[verdict]
    else:
        try:
            observation = ops.prepared_pane(
                locator=participant.locator,
                workspace_id=receipt.workspace_id,
                tab_id=receipt.tab_id,
            )
        except Exception:  # noqa: BLE001 - absence of a positive pane read blocks close
            observation = PreparedPaneObservation(
                state=PREPARED_PANE_UNREADABLE,
                detail="prepared pane inventory could not be read",
            )
        if observation.state == PREPARED_PANE_ABSENT:
            verdict = ROLLBACK_ABSENT
            detail = (
                "the pane_bound_v1 locator is positively absent from the complete Herdr "
                "pane inventory; there is nothing to close"
            )
        elif observation.state == PREPARED_PANE_PRESENT:
            verdict = ROLLBACK_PREPARED_PANE_UNVERIFIABLE
            detail = (
                "the action-recorded shell pane has no terminal-bound generation pin; "
                "a locator/workspace/tab match cannot authorize closing a reused pane"
            )
        else:
            verdict = ROLLBACK_PREPARED_PANE_UNVERIFIABLE
            detail = observation.detail or (
                "the action-recorded prepared pane could not be proven to have the same "
                "container, no agent, only its shell, and no input; refusing to close it"
            )
    return ParticipantVerdict(
        role=participant.role,
        assigned_name=participant.assigned_name,
        locator=participant.locator,
        verdict=verdict,
        detail=detail,
        closed=participant.closed,
        prepared_pane=True,
    )


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


def _observe(action, ops: StartupRollbackOps, *, store_home: Path) -> tuple[list, bool]:
    """Classify every participant from one action-time observation of the live world."""
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
        except PaneBoundReceiptError as exc:
            verdicts.append(
                ParticipantVerdict(
                    role=participant.role,
                    assigned_name=participant.assigned_name,
                    locator=participant.locator,
                    verdict=ROLLBACK_PREPARED_RECEIPT_INVALID,
                    detail=(
                        "the participant claims pane-bound authority but its receipt is "
                        f"invalid ({exc}); refusing to reinterpret it as a legacy launch"
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
        if pane_receipt is not None and inventory_readable and not name_matches:
            verdicts.append(
                _prepared_pane_verdict(
                    ops,
                    participant,
                    pane_receipt,
                    inventory_readable=inventory_readable,
                    obligation_names=obligation_names,
                    obligation_unreadable=obligation_unreadable,
                )
            )
            continue
        if (
            pane_receipt is not None
            and not participant.closed
            and len(name_matches) == 1
            and name_matches[0].get("native_name") != pane_receipt.native_name
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
    return action.as_payload()


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
    settled = list(verdicts)
    failed: dict = {}
    if targets:
        # The close port can raise AFTER a partial effect (review j#81224 R7-F4): some
        # panes may already be gone. Do NOT let that escape the public rail raw — the
        # remeasure below is what establishes the real end state, so a close exception is
        # recorded as a whole-batch failure detail and the remeasure decides per role.
        try:
            result = ops.close_current_generation(action, targets, store_home=store_home)
            failed = {
                role: "terminal-bound pane close failed"
                for role, _locator, _detail in getattr(result, "failed", ())
            }
        except Exception:  # noqa: BLE001 - a close that raised is a close that may
            # have partially acted; the remeasure, not this exception, decides the outcome.
            failed = {role: "terminal-bound pane close failed" for role, _ in targets}
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
        (participant.role, participant.locator)
        for participant in action.participants
        if parse_pane_bound_receipt(participant.receipt) is None
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
            )
        except Exception:  # noqa: BLE001 - an unreadable post-close pane proves nothing
            return residue, False
        if observation.state == PREPARED_PANE_ABSENT:
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
            if receipt is None:
                if not _terminal_bound_action_target_absent(
                    store_home, action, participant, rows
                ):
                    return False
                continue
            observation = ops.prepared_pane(
                locator=participant.locator,
                workspace_id=receipt.workspace_id,
                tab_id=receipt.tab_id,
            )
            if observation.state != PREPARED_PANE_ABSENT:
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
