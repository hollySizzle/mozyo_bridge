"""Reconcile the live `workflow step` outcome with the persisted store action (Redmine #13291).

Two independent next-action engines exist today:

- the **live** ``workflow step`` state machine
  (:mod:`...domain.workflow_step`) resolves the current lane's one-step-down
  routing/transport transition from live tmux identity — a *route* decision;
- the **persisted** ``workflow resume`` engine
  (:mod:`...domain.workflow_next_action` folding the #12671 runtime store) derives the
  overall workflow *lifecycle* pending action (await / review / integrate / close /
  resolve-blocker / …) from the durable event log — a *lifecycle* decision.

They were never reconciled: ``workflow step`` read no runtime store, so an agent could
be told to forward a fresh consultation / dispatch while the persisted workflow state
had a pending governance gate (an unconfirmed integrate / close / owner approval / a
recorded blocker) waiting. This module is the **pure, fail-toward-safe** bridge the
issue asks for: it composes the live outcome with the store's pending action and
returns a single reconciled outcome plus a fixed-vocabulary *disposition* naming how
the two were combined.

Composition contract (the fixed vocabulary + ordering the issue pins):

- **degrade, non-destructively, when the store cannot contribute.** A missing store
  (:data:`STORE_ABSENT`) or an unreadable / stale one (:data:`STORE_UNAVAILABLE`)
  leaves the live outcome byte-identical — the prior ``workflow step`` behavior — so a
  lane with no persisted runtime state is unaffected (backward compatibility).
- **do nothing when the store has no pending action.** A store whose overall action is
  a positive-occupancy no-op (``none`` / ``hold`` / ``await_implementation``) has
  nothing to reflect (:data:`RECONCILE_STORE_NO_PENDING`); the live outcome is unchanged.
- **reflect a pending, non-gating action.** A store action that is genuinely pending but
  not a gate (a low-risk callback delivery / a review to perform) is *surfaced*
  alongside the live outcome (:data:`RECONCILE_STORE_ALIGNED`) without changing the
  route — both engines agree the workflow moves forward.
- **fail toward safe when a gating store action contradicts a live forward leg.** When
  the store has a *gating* pending action — one the store's own vocabulary flags as
  ``requires_confirmation`` or ``blocked_reason`` (integrate / close / retire /
  dispatch-next / redeliver / resolve-blocker / owner-or-release gate / an unresolved
  route) — and the live step is an executable forward leg, the reconciled outcome is
  **gated** (:data:`RECONCILE_STORE_GATES_LIVE`): the forward leg is downgraded to a
  fail-closed ``blocked`` so ``workflow step`` never auto-forwards past an unhandled
  workflow gate. The safer of the two engines wins; the agent is pointed at
  ``workflow resume`` to handle the pending action first.

Scope boundaries: this module makes **no** routing decision of its own and performs no
execution (execution-leg auto-actuation is an explicit non-goal of #13291). It only
*combines* two already-computed decisions and, at most, refuses a forward leg. The
store stays the reporting surface (``workflow resume``); ``workflow step`` reads it as a
decision input and never mutates it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    WorkflowNextAction,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ACTION_AWAIT_IMPLEMENTATION,
    ACTION_HOLD,
    ACTION_NONE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_READY,
    OWNER_OPERATOR,
    WorkflowStepOutcome,
)

# ---------------------------------------------------------------------------
# Fixed vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

# ``store_status`` — how the persisted runtime store presented itself to the step.
STORE_PRESENT = "present"
STORE_ABSENT = "absent"
STORE_UNAVAILABLE = "unavailable"

# ``reconcile_disposition`` — how the live outcome and the store action were combined.
# Ordered least-to-most involved. The first three are *degrade / no-op* dispositions
# that leave the live outcome untouched (backward compatible); the last two reflect a
# pending store action in the reported output.
RECONCILE_STORE_ABSENT = "store_absent"
RECONCILE_STORE_UNAVAILABLE = "store_unavailable"
RECONCILE_STORE_NO_PENDING = "store_no_pending_action"
RECONCILE_STORE_ALIGNED = "store_aligned"
RECONCILE_STORE_GATES_LIVE = "store_gates_live"
# The store's pending action belongs to a different Redmine issue than the live-verified
# anchor of this lane, so it is **not adopted** onto this lane's step (Redmine #13489 F3c):
# a caller-supplied store must not surface a cross-issue action on a lane whose anchor was
# verified against source-of-truth Redmine. Only emitted when a ``live_anchor_issue`` is
# supplied (the herdr path); the tmux path passes ``None`` and is byte-invariant.
RECONCILE_STORE_ISSUE_MISMATCH = "store_issue_mismatch"

# The reconcile-only reason token stamped on a gated live leg (the live step's own
# ``reason`` vocabulary is fixed; this marks a forward leg held by the store gate).
REASON_STORE_PENDING_ACTION_GATES = "store_pending_action_gates"

# The dispositions that *reflect* a pending store action in the reported output. The
# degrade / no-op dispositions do not, so a lane with no pending store action keeps the
# exact prior ``workflow step`` output (Redmine #13291 backward-compat acceptance). The
# issue-mismatch disposition reflects the *rejected* action (auditable, never silently
# dropped) while leaving the live outcome unchanged.
_REFLECTING_DISPOSITIONS = frozenset(
    {RECONCILE_STORE_ALIGNED, RECONCILE_STORE_GATES_LIVE, RECONCILE_STORE_ISSUE_MISMATCH}
)

# Store overall-actions that are positive-occupancy no-ops: nothing is pending, so the
# store has nothing to reflect onto the live step.
_NON_PENDING_ACTIONS = frozenset(
    {ACTION_NONE, ACTION_HOLD, ACTION_AWAIT_IMPLEMENTATION}
)


def store_action_is_pending(action: WorkflowNextAction) -> bool:
    """True when the store's overall action is something to act on (not a no-op/hold/await)."""
    return action.action not in _NON_PENDING_ACTIONS


def _action_matches_issue(action: WorkflowNextAction, issue: str) -> bool:
    """True when the store action is about ``issue`` (its ``target_issue`` or anchor issue).

    Used to issue-correlate a pending store action with the lane's live-verified anchor
    (Redmine #13489 F3c). An action with no ``target_issue`` and no anchor issue cannot be
    correlated, so it does **not** match — a caller-supplied store never surfaces an
    uncorrelated action onto a source-of-truth-verified lane.
    """
    want = (issue or "").strip()
    if not want:
        return False
    if (action.target_issue or "").strip() == want:
        return True
    # ``anchor`` is a durable Redmine pointer (``issue:journal`` / ``redmine:issue=…:journal=…``);
    # the issue is the first numeric-ish segment after any ``redmine:``/``issue=`` prefix.
    anchor = (action.anchor or "").strip()
    if not anchor:
        return False
    tail = anchor.split(":", 1)[1] if anchor.startswith("redmine:") else anchor
    first = tail.split(":", 1)[0].strip()
    if first.startswith("issue="):
        first = first[len("issue="):].strip()
    return first == want


def store_action_is_gating(action: WorkflowNextAction) -> bool:
    """True when a pending store action must gate a live forward leg (fail-toward-safe).

    Reuses the store engine's *own* fixed vocabulary rather than a parallel risk model:
    an action the persisted engine already flags ``requires_confirmation`` (integrate /
    close / retire / dispatch-next / redeliver / resolve-blocker / owner-or-release gate)
    or ``blocked_reason`` (unknown action / unresolved route) is exactly the class of
    workflow gate ``workflow step`` must not silently forward past.
    """
    return action.requires_confirmation or action.is_blocked


def _gate_live(
    live: WorkflowStepOutcome, store_action: WorkflowNextAction
) -> WorkflowStepOutcome:
    """Downgrade a live forward leg to a fail-closed block held by the store gate.

    Keeps every resolved live routing field (state / primitive / target / lane) as
    context but flips ``execution`` to ``blocked`` (so the CLI never dispatches the
    primitive) with the reconcile reason and the operator as the next owner: the
    persisted workflow gate must be handled (via ``workflow resume``) before a new
    forward step.
    """
    return dataclasses.replace(
        live,
        execution=EXECUTION_BLOCKED,
        reason=REASON_STORE_PENDING_ACTION_GATES,
        next_owner=OWNER_OPERATOR,
        next_action=(
            "the persisted runtime store has a pending "
            f"{store_action.action!r} action (owner_role={store_action.owner_role}"
            f"{f', blocked_reason={store_action.blocked_reason}' if store_action.blocked_reason else ''}) "
            "that must be handled before a new forward step; run `mozyo-bridge "
            "workflow resume` to act on it, then step again"
        ),
        detail=(
            (live.detail + " | " if live.detail else "")
            + f"held by store pending action {store_action.action!r}"
        ),
    )


@dataclass(frozen=True)
class ReconciledStep:
    """The live ``workflow step`` outcome composed with the persisted store action.

    :attr:`outcome` is the *effective* outcome the CLI reports and acts on — identical
    to :attr:`live_outcome` in every degrade / no-op / aligned disposition, and a gated
    (``blocked``) variant only under :data:`RECONCILE_STORE_GATES_LIVE`.
    :attr:`disposition` names how the two were combined (fixed vocabulary);
    :attr:`store_action` is the store's overall pending action snapshot for reporting
    (``None`` when the store could not contribute).
    """

    outcome: WorkflowStepOutcome
    live_outcome: WorkflowStepOutcome
    disposition: str
    store_action: Optional[WorkflowNextAction] = None

    @property
    def reflects_store(self) -> bool:
        """True when a pending store action is reflected in the reported output.

        Only the aligned / gated dispositions reflect the store; the degrade and
        no-pending dispositions leave the output byte-identical to the prior
        ``workflow step`` behavior, so the reconcile fields are omitted for them.
        """
        return self.disposition in _REFLECTING_DISPOSITIONS

    def store_action_payload(self) -> dict[str, object]:
        """Public-safe projection of the reflected store action (no pane id; empty if none)."""
        na = self.store_action
        if na is None:
            return {}
        return {
            "action": na.action,
            "owner_role": na.owner_role,
            "target_issue": na.target_issue,
            "anchor": na.anchor,
            "risk_level": na.risk_level,
            "requires_confirmation": na.requires_confirmation,
            "blocked_reason": na.blocked_reason,
            "reason": na.reason,
        }

    def reconcile_payload_fields(self) -> dict[str, object]:
        """The reconcile fields to merge into the reported envelope (empty unless reflecting)."""
        if not self.reflects_store:
            return {}
        return {
            "reconcile_disposition": self.disposition,
            "store_pending_action": self.store_action_payload(),
        }

    def reconcile_text_lines(self) -> list[str]:
        """Human-text reconcile lines to append to the step summary (empty unless reflecting)."""
        if not self.reflects_store:
            return []
        na = self.store_action
        lines = [f"reconcile_disposition: {self.disposition}"]
        if na is not None:
            lines.append(
                "store_pending_action: "
                f"action={na.action} owner_role={na.owner_role} "
                f"target_issue={na.target_issue or '<none>'} "
                f"risk_level={na.risk_level} "
                f"requires_confirmation={str(na.requires_confirmation).lower()} "
                f"blocked_reason={na.blocked_reason or '<none>'}"
            )
        return lines


def reconcile_step_with_store(
    live: WorkflowStepOutcome,
    store_action: Optional[WorkflowNextAction],
    *,
    store_status: str,
    live_anchor_issue: Optional[str] = None,
) -> ReconciledStep:
    """Compose the live step outcome with the store's pending action (pure, #13291 / #13489).

    ``store_status`` is one of :data:`STORE_PRESENT` / :data:`STORE_ABSENT` /
    :data:`STORE_UNAVAILABLE` (the CLI reads the store fail-open and classifies it).
    ``store_action`` is the store's overall pending action when present, else ``None``.
    ``live_anchor_issue`` is the Redmine issue the live outcome's anchor was **verified**
    against (the herdr path passes it; the tmux path passes ``None`` and is byte-invariant).

    Ordering (fixed vocabulary, fail-toward-safe):

    1. absent / unavailable store -> degrade to the live outcome unchanged;
    2. present but non-pending action -> live unchanged (nothing to reflect);
    3. **issue-correlation (Redmine #13489 F3c):** when a ``live_anchor_issue`` is supplied and
       the pending action belongs to a *different* Redmine issue, the caller-supplied store must
       not surface a cross-issue action onto this source-of-truth-verified lane — it is **not
       adopted** (:data:`RECONCILE_STORE_ISSUE_MISMATCH`), the rejected action reflected for
       audit while the live outcome is unchanged;
    4. pending, gating action + a live forward (``ready``) leg -> gate the live leg
       (fail-closed ``blocked``): the store gate wins over the forward step;
    5. otherwise (pending non-gating, or the live leg is already not a forward step) ->
       surface the store action alongside the unchanged live outcome (aligned).
    """
    if store_status == STORE_ABSENT:
        return ReconciledStep(live, live, RECONCILE_STORE_ABSENT, None)
    if store_status == STORE_UNAVAILABLE or store_action is None:
        return ReconciledStep(live, live, RECONCILE_STORE_UNAVAILABLE, None)

    if not store_action_is_pending(store_action):
        return ReconciledStep(live, live, RECONCILE_STORE_NO_PENDING, store_action)

    anchor_issue = (live_anchor_issue or "").strip()
    if anchor_issue and not _action_matches_issue(store_action, anchor_issue):
        # A caller-supplied store action for a different issue is not this lane's action;
        # reject it rather than reflect / gate it onto the verified-anchor lane.
        return ReconciledStep(live, live, RECONCILE_STORE_ISSUE_MISMATCH, store_action)

    if store_action_is_gating(store_action) and live.execution == EXECUTION_READY:
        gated = _gate_live(live, store_action)
        return ReconciledStep(gated, live, RECONCILE_STORE_GATES_LIVE, store_action)

    # Pending but non-gating, or the live leg is not a forward step (already blocked, or
    # a grandchild no-op): surface the store action, leave the live outcome unchanged.
    return ReconciledStep(live, live, RECONCILE_STORE_ALIGNED, store_action)


__all__ = (
    "STORE_PRESENT",
    "STORE_ABSENT",
    "STORE_UNAVAILABLE",
    "RECONCILE_STORE_ABSENT",
    "RECONCILE_STORE_UNAVAILABLE",
    "RECONCILE_STORE_NO_PENDING",
    "RECONCILE_STORE_ALIGNED",
    "RECONCILE_STORE_GATES_LIVE",
    "RECONCILE_STORE_ISSUE_MISMATCH",
    "REASON_STORE_PENDING_ACTION_GATES",
    "store_action_is_pending",
    "store_action_is_gating",
    "ReconciledStep",
    "reconcile_step_with_store",
)
