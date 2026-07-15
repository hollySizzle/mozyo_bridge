"""Bare-``mozyo`` pre-attach replacement reconciliation seam (Redmine #13806 tranche C).

After onboarding / root resolution and *before* the UI attach, the composition root asks
this bounded seam what to do about the resolved session (j#78384 §5):

- a fully **ready** session (every slot adopted / launched) — **pass through**: the seam
  is a no-op and the existing all-adopt / all-launch / tmux / explicit ``herdr
  session-start`` behavior is preserved byte-for-byte;
- an **unresolved** session (a stale-attestation coordinator slot) with exactly one
  positive approved transaction — **reconcile once**: call the process-external self-close
  executor and the fresh-coordinator drain a single time;
- an unresolved session with no actionable approval (absent / stale / ambiguous /
  unreadable) — a single **typed blocked** outcome, zero process / input / route / outbox
  writes.

The seam owns no Redmine-body parsing, no kill implementation, and no raw transport — it is
a composition root that delegates the actuation to the injected use cases (the executor and
the drain) and returns a typed outcome. It is bounded: at most one reconciliation pass; a
still-incomplete transaction is replayed from its durable owed state by the next bare
``mozyo`` run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.core.state.replacement_transaction_model import (
    ReplacementTransactionKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DrainResult,
    FreshCoordinatorDrainUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.self_close_executor import (  # noqa: E501
    SELF_CLOSE_REPLACED,
    SelfCloseExecutorUseCase,
    SelfCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E501
    RECONCILE_BLOCKED,
    RECONCILE_ONCE,
    RECONCILE_PASS_THROUGH,
    decide_pre_attach,
)


@dataclass(frozen=True)
class ReconcileResolution:
    """What the composition root resolved for an unresolved session.

    ``token`` is a :mod:`...domain.session_replacement_reconcile` ``TXN_*`` resolution. For
    :data:`...TXN_RESOLVED_EXACT` the remaining fields pin the exact approved transaction and
    the two holders — the process-external ``executor_holder`` (drives the self-close) and the
    fresh coordinator's action-bound ``fresh_holder`` (claims + drains). For any other token
    they are unused (the seam blocks without touching the store).
    """

    token: str
    key: Optional[ReplacementTransactionKey] = None
    action_generation: int = 0
    executor_holder: str = ""
    fresh_holder: str = ""


@dataclass(frozen=True)
class PreAttachOutcome:
    """The typed pre-attach seam outcome the composition root renders / gates on."""

    kind: str
    blocked_reason: str = ""
    self_close: Optional[SelfCloseResult] = None
    drain: Optional[DrainResult] = None

    @property
    def pass_through(self) -> bool:
        return self.kind == RECONCILE_PASS_THROUGH

    @property
    def blocked(self) -> bool:
        return self.kind == RECONCILE_BLOCKED

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "blocked_reason": self.blocked_reason,
            "self_close": self.self_close.as_payload() if self.self_close else None,
            "drain": self.drain.as_payload() if self.drain else None,
        }


class PreAttachReconcileUseCase:
    """The bounded pre-attach reconciliation composition root (tranche C)."""

    def __init__(
        self,
        executor: SelfCloseExecutorUseCase,
        drain: FreshCoordinatorDrainUseCase,
    ) -> None:
        self._executor = executor
        self._drain = drain

    def reconcile(
        self, resolution: ReconcileResolution, *, session_ready: bool
    ) -> PreAttachOutcome:
        """Decide + (at most once) actuate the pre-attach replacement reconciliation.

        A ready session passes through with zero effect. An unresolved session reconciles
        once ONLY for an exact positive transaction; every other resolution is a typed
        blocked outcome with no store / process write.
        """
        decision = decide_pre_attach(
            session_ready=session_ready, resolution=resolution.token
        )
        if decision.kind == RECONCILE_PASS_THROUGH:
            return PreAttachOutcome(kind=RECONCILE_PASS_THROUGH)
        if decision.kind == RECONCILE_BLOCKED:
            # Zero process / input / route / outbox / store write on the blocked path.
            return PreAttachOutcome(
                kind=RECONCILE_BLOCKED, blocked_reason=decision.blocked_reason
            )
        # RECONCILE_ONCE: exactly one positive transaction. Run the executor once; only if the
        # self coordinator is replaced does the fresh coordinator claim + drain run.
        self_result = self._executor.run(
            resolution.key,
            holder=resolution.executor_holder,
            expected_action_generation=resolution.action_generation,
        )
        if self_result.status != SELF_CLOSE_REPLACED:
            # The self-close was blocked or stopped fail-closed; do not proceed to the drain.
            # A later bare `mozyo` run replays from the durable owed state.
            return PreAttachOutcome(kind=RECONCILE_ONCE, self_close=self_result)
        drain_result = self._drain.run(
            resolution.key,
            holder=resolution.fresh_holder,
            expected_action_generation=resolution.action_generation,
        )
        return PreAttachOutcome(
            kind=RECONCILE_ONCE, self_close=self_result, drain=drain_result
        )


__all__ = (
    "ReconcileResolution",
    "PreAttachOutcome",
    "PreAttachReconcileUseCase",
)
