"""Bind one session-start run to its durable startup action (Redmine #13948, j#80989).

The launcher's side of :mod:`...core.state.startup_transaction_fence`. It exists so the
composition root spends three calls, not thirty lines, on the thing that makes a partial
pair recoverable: an identity reserved *before* the first side effect, a participant
recorded *immediately after* each launch, and a phase that says what the run still owes.

Ordering is the contract, not an implementation detail:

- **reserve → launch**, never the reverse. A workspace/tab/agent created before its record
  exists is a side effect nobody can prove they caused, which is precisely the #13441
  partial lane and the #13882 partial pair.
- **record each launch as it happens.** Recording them all at the end would lose exactly
  the case that matters: a run that dies between two starts. That run's first agent is
  live, and only its participant row can tell a later rollback whose it is.
- **the phase is the debt.** ``rollback_owed`` is written by the run that incurred it and
  cleared only by the explicit public rail (Answer j#80991) — session-start never closes.

Every entry point is fail-soft on the *store* and fail-closed on the *decision*: a run
whose fence is unusable must not silently launch untracked (there would be no way back),
so a reserve failure raises before any side effect. Once panes exist, a bookkeeping
failure must not destroy them either — it is surfaced, and the panes stay.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_ROLLBACK_OWED,
    PHASE_SUCCESS_OWED,
    Participant,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)


def new_action_nonce() -> str:
    """Mint the per-invocation nonce that separates a re-run from its predecessor.

    Minted here rather than inside the fence so the identity function stays pure and
    testable, and so a test can pin an exact action id by passing its own nonce.
    """
    return uuid.uuid4().hex


class StartupTransaction:
    """One run's handle on its durable action. Construction reserves nothing."""

    def __init__(
        self,
        *,
        fence: StartupTransactionFence,
        unit: StartupUnit,
        nonce: str,
    ) -> None:
        self._fence = fence
        self._unit = unit
        self._nonce = nonce
        self._action = None

    @property
    def action_id(self) -> str:
        return self._action.action_id if self._action is not None else ""

    def reserve(self) -> str:
        """Durably record the identity BEFORE the run's first side effect."""
        self._action = self._fence.reserve(self._unit, self._nonce)
        return self._action.action_id

    def record_launch(self, slot, *, receipt: str = "") -> None:
        """Record one completed ``agent start`` as a participant of this action."""
        if self._action is None:
            raise StartupTransactionError(
                "a launch was recorded before its startup action was reserved; the "
                "reserve must precede every side effect"
            )
        self._action = self._fence.record_participant(
            self._action.action_id,
            Participant(
                role=slot.provider,
                assigned_name=slot.assigned_name,
                locator=slot.locator,
                receipt=receipt,
            ),
        )

    def settle(self, *, owed: bool, launched: bool) -> None:
        """Close the run's books: success, or a debt only the rollback rail may clear.

        ``owed`` is the run's OWN compensation debt — whether a slot THIS run freshly
        launched failed to come up healthy (:attr:`SessionStartResult.owes_rollback`), NOT
        the pair aggregate ``ok`` (Redmine #13933 R13, j#82038). The distinction is the fix:
        a healthy fresh launch that merely adopted a non-green sibling owes nothing, so the
        transaction completes instead of leaving a phantom rollback owed against a pane the
        run never created — which is what stalled the v1 replacement bind at ``launch_owed``.

        ``launched`` is what makes the difference between a debt and a fact: a run that
        started nothing (all-adopt) has no side effect to compensate even when it reports
        unhealthy, so it completes rather than leaving a rollback owed against panes it
        never created.
        """
        if self._action is None:
            return
        action_id = self._action.action_id
        self._fence.set_phase(action_id, PHASE_HEALTH_CHECK)
        if not owed:
            self._fence.set_phase(action_id, PHASE_SUCCESS_OWED)
            self._action = self._fence.set_phase(action_id, PHASE_COMPLETED_SUCCESS)
            return
        if not launched:
            # Nothing of ours is out there; there is nothing to roll back. Saying
            # `rollback_owed` here would invite the rail to look for participants that
            # do not exist and report a blocked compensation for a debt that is not one.
            self._action = self._fence.set_phase(action_id, PHASE_COMPLETED_SUCCESS)
            return
        self._action = self._fence.set_phase(action_id, PHASE_ROLLBACK_OWED)


def open_startup_transaction(
    *,
    workspace_id: str,
    lane_id: str,
    providers: Sequence[str],
    dry_run: bool,
    home: Optional[Path] = None,
    fence: Optional[StartupTransactionFence] = None,
    nonce: str = "",
) -> Optional[StartupTransaction]:
    """Reserve this run's action, or ``None`` for a dry run (which starts nothing).

    Raises :class:`StartupTransactionError` when the authority is unusable. That is
    deliberate and is the whole point of reserving first: a run that cannot record what it
    is about to start must not start it — an untracked partial pair is the defect.
    """
    if dry_run:
        return None
    transaction = StartupTransaction(
        fence=fence or StartupTransactionFence(home=home),
        unit=StartupUnit(
            workspace_id=workspace_id, lane_id=lane_id, providers=tuple(providers)
        ),
        nonce=nonce or new_action_nonce(),
    )
    transaction.reserve()
    return transaction


def launch_receipt(*, target_workspace: str, target_tab: str) -> str:
    """The launcher's own placement evidence, as a fixed, value-free token.

    Kept with the participant so a rollback can show the pane it is about to close is the
    one THIS action placed — not merely one whose durable name matches.
    """
    receipt = f"workspace={target_workspace or '-'}"
    if target_tab:
        receipt += f" tab={target_tab}"
    return receipt


__all__ = (
    "StartupTransaction",
    "launch_receipt",
    "new_action_nonce",
    "open_startup_transaction",
)
