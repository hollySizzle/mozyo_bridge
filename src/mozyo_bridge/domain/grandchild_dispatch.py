"""Delegated coordinator -> grandchild dispatch decision resolver (Redmine #12458).

US #12454 (`親子孫 delegated coordinator cockpit window UX`) splits the parent ->
delegated coordinator -> grandchild route into a durable policy shape (#12456)
and the runtime primitives that act on it. #12457 implemented the **depth 1**
parent -> delegated coordinator launch/adopt decision
(:mod:`mozyo_bridge.domain.delegation_launch_adopt`). This module is the **depth
2** runtime decision primitive: the delegated coordinator deciding whether to
launch or adopt a *grandchild* implementation lane for context preservation, and
recording that decision durably.

It layers two concerns on top of the reused #12457 launch/adopt selector:

1. **A delegation policy gate** (``delegation-policy-project-config.md``): the
   ``enable_delegated_coordinator`` master gate, the ``enable_grandchild_dispatch``
   depth-2 permission, the ``max_delegation_depth`` hop ceiling (hard ceiling
   :data:`HARD_CEILING_DEPTH` = 2), and the ``max_active_child_lanes`` capacity.
   Grandchild (depth 2) dispatch opens only on the AND of the master gate, the
   grandchild flag, and an effective depth ceiling that admits the new lane —
   the stricter side always wins, and every malformed value clamps to the safe
   (delegation-suppressing) side.
2. **The grandchild dispatch / no-dispatch decision** (the spine's
   ``### 孫 dispatch / context 保護`` and ``delegated-coordinator-decision-records.md``
   §2 / §3): a *dispatch* outcome reuses #12457's fail-closed launch/adopt
   selector to pick the grandchild lane's **Codex gateway** (never the grandchild
   Claude directly), while an explicit *no-dispatch* outcome records the
   ``grandchild_dispatch: avoided`` reason when the delegated coordinator keeps
   the work in its own lane.

Invariants kept enforced in code (the #12458 acceptance):

- **Policy boundary is effective.** ``enable_grandchild_dispatch`` /
  ``max_delegation_depth: 2`` (and the master gate / capacity) gate depth-2
  dispatch; a disabled or too-shallow policy fails closed with an explicit
  reason rather than silently dispatching.
- **Pure decision, no side effects.** No tmux, no filesystem, no send. The
  delegated coordinator records the durable decision *before* any pane
  notification or runtime mutation; a launch outcome is a structured *decision
  to launch a visible cockpit lane* — core is not a worktree manager, so it
  never spawns a lane here.
- **The grandchild lane is a visible, durable-anchored lane, never a hidden
  subagent.** :attr:`GrandchildDispatchDecision.visible_lane_required` is always
  true: "context 圧迫回避" is achieved by dispatching to a declared lane, not by
  an invisible hierarchy hop (``delegation-policy-project-config.md`` fixed
  invariant).
- **The route lands at the grandchild Codex gateway.** A ``claude`` candidate is
  never selectable (inherited from :func:`resolve_launch_adopt`'s ``required_role
  == codex`` invariant) — no direct cross-lane / cross-project Claude send.
- **Routing authority is identity, never display proximity.** Selection reads
  role, canonical repo identity (the mandatory ``--target-repo`` gate), lane id,
  and resolver provenance only — never window / session / title / display
  proximity.
- **Multi-coordinator callback coverage.** The callbacks to the GK parent route
  and the mozyo_bridge coordinator route are both required and replayable: a lone
  ``delegation_parent`` callback does not satisfy coverage unless the owning /
  audit coordinator is *explicitly* declared the same route
  (``delegated-coordinator-decision-records.md`` §4.1).

The module is pure (dataclasses + resolvers) and imports only the sibling
:mod:`delegation_launch_adopt` domain primitive, so it has no import-time
dependency on tmux and stays trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from mozyo_bridge.domain.delegation_launch_adopt import (
    CallbackTarget,
    DelegationCandidate,
    DelegationLaunchAdoptError,
    LaunchAdoptDecision,
    PURPOSE_AUDIT_COORDINATOR,
    PURPOSE_OWNING_US_COORDINATOR,
    ROLE_CODEX,
    resolve_launch_adopt,
    validate_callback_targets,
)

# --- delegation depth vocabulary (delegation-policy-project-config.md) ---------
# depth 0 = parent coordinator (delegation tree root)
# depth 1 = delegated coordinator
# depth 2 = grandchild implementation lane

#: The audit-safe shallow-delegation hard ceiling from Feature #12386: the
#: grandchild implementation lane (depth 2) is the deepest hop a project policy
#: may admit. ``max_delegation_depth`` is clamped to this and a value above it is
#: invalid config (no 4-level default is defined).
HARD_CEILING_DEPTH = 2

#: The default depth of the dispatching actor: the delegated coordinator sits at
#: depth 1, so the grandchild lane it dispatches lands at depth 2.
DEFAULT_DELEGATED_COORDINATOR_DEPTH = 1

#: ``decision_record_policy`` no-dispatch / context-neutral recording granularity.
RECORD_POLICY_MINIMAL = "minimal"
RECORD_POLICY_VERBOSE = "verbose"
RECORD_POLICIES: frozenset[str] = frozenset({RECORD_POLICY_MINIMAL, RECORD_POLICY_VERBOSE})

# --- grandchild dispatch outcomes ---------------------------------------------

#: Adopt an existing visible grandchild Codex gateway (the launch/adopt selector
#: resolved exactly one strong, non-ambiguous candidate).
OUTCOME_DISPATCH_ADOPT = "dispatch_adopt"
#: Launch a new grandchild implementation lane (the caller performs the worktree
#: / cockpit creation; this module only decides ``dispatch_launch``).
OUTCOME_DISPATCH_LAUNCH = "dispatch_launch"
#: The delegated coordinator keeps the work in its own lane (the spine's
#: ``grandchild_dispatch: avoided`` path); ``no_dispatch_reason`` names why.
OUTCOME_NO_DISPATCH = "no_dispatch"
#: Form no grandchild route; ``reason`` names why (a ``REASON_*`` token, either a
#: policy-gate reason here or a launch/adopt selection reason inherited from
#: #12457).
OUTCOME_FAIL_CLOSED = "fail_closed"

# --- policy-gate fail-closed reasons ------------------------------------------

#: ``enable_delegated_coordinator: false`` — the master gate is off, so no nested
#: delegation (and therefore no grandchild) forms.
REASON_MASTER_GATE_DISABLED = "master_gate_disabled"
#: ``enable_grandchild_dispatch: false`` — depth-2 dispatch is not permitted by
#: policy even though the master gate is on.
REASON_GRANDCHILD_DISABLED = "grandchild_dispatch_disabled"
#: The new lane's depth would exceed the effective / hard depth ceiling
#: (``max_delegation_depth < 2`` or a clamped-to-zero invalid depth).
REASON_DEPTH_CEILING_EXCEEDED = "depth_ceiling_exceeded"
#: The delegated coordinator already holds ``max_active_child_lanes`` active
#: grandchild lanes; opening another would exceed the capacity.
REASON_ACTIVE_LANE_CAPACITY_EXHAUSTED = "active_lane_capacity_exhausted"

# --- grandchild dispatch purpose (spine ### 孫 dispatch / context 保護) ---------

#: The sole policy-recognized purpose for opening a grandchild lane: protecting
#: the parent / delegated coordinator LLM context window — not work size.
PURPOSE_PRESERVE_CONTEXT = "preserve_coordinator_context"

# --- no-dispatch reason vocabulary (decision-records §3) ----------------------

#: Context pressure is low; the delegated coordinator keeps the work without
#: losing the pointers / judgement it must retain.
NO_DISPATCH_REASON_CONTEXT_COST_LOW = "context_cost_low"
#: No trial-and-error round trips, so no intermediate state accrues in context.
NO_DISPATCH_REASON_SINGLE_PASS = "single_pass_no_iteration"
#: An urgent minimal correction (the spine Admission Rule exception).
NO_DISPATCH_REASON_URGENT_MINIMAL = "urgent_minimal_correction"

#: Recognized no-dispatch reason tokens; a free-form ``<具体記述>`` borderline
#: reason is also accepted (decision-records §3 leaves the tail open).
KNOWN_NO_DISPATCH_REASONS: frozenset[str] = frozenset(
    {
        NO_DISPATCH_REASON_CONTEXT_COST_LOW,
        NO_DISPATCH_REASON_SINGLE_PASS,
        NO_DISPATCH_REASON_URGENT_MINIMAL,
    }
)

# --- multi-coordinator callback coverage (decision-records §4.1) --------------

#: Explicit declaration that the owning-US / audit coordinator callback route is
#: the same as the delegation parent route. Required to satisfy multi-coordinator
#: coverage without a distinct owning/audit target — "推測で省略しない".
OWNING_COVERAGE_SAME_AS_PARENT = "same_as_delegation_parent"


# --- delegation policy --------------------------------------------------------


@dataclass(frozen=True)
class DelegationPolicy:
    """The delegation policy knobs that gate grandchild (depth-2) dispatch.

    Mirrors the ``delegation:`` config knob schema in
    ``delegation-policy-project-config.md``. This is a *desired policy
    declaration* (the loader from ``.mozyo-bridge/config.yaml`` is the #12390
    follow-up — this primitive takes the resolved values from the durable record,
    the same way #12457 takes ``--launch-adopt-mode`` from the durable record).
    The defaults are the spec's safety-biased defaults: delegation off, grandchild
    off, depth 1, one active child lane.
    """

    enable_delegated_coordinator: bool = False
    enable_grandchild_dispatch: bool = False
    max_delegation_depth: int = 1
    max_active_child_lanes: int = 1
    decision_record_policy: str = RECORD_POLICY_MINIMAL


@dataclass(frozen=True)
class EffectiveDelegationPolicy:
    """A delegation policy normalized to the fail-closed / clamp matrix.

    Carries the effective (clamped) values and the ``diagnostics`` tokens for any
    malformed input, so an invalid policy surfaces a diagnostic *and* falls to the
    safe side rather than silently widening the ceiling.
    """

    enable_delegated_coordinator: bool
    enable_grandchild_dispatch: bool
    effective_max_depth: int
    effective_max_active_child_lanes: int
    decision_record_policy: str
    diagnostics: tuple[str, ...] = ()

    @property
    def grandchild_permitted(self) -> bool:
        """The AND condition that opens depth 2 (master + grandchild + depth>=2)."""
        return (
            self.enable_delegated_coordinator
            and self.enable_grandchild_dispatch
            and self.effective_max_depth >= HARD_CEILING_DEPTH
        )


def effective_policy(policy: DelegationPolicy) -> EffectiveDelegationPolicy:
    """Normalize a :class:`DelegationPolicy` to the spec fail-closed / clamp matrix.

    - ``max_delegation_depth`` outside ``0..2`` / negative / non-int → effective
      ``0`` with a diagnostic (delegation suppressed).
    - ``max_active_child_lanes < 1`` / non-int → effective ``1`` with a diagnostic.
    - ``enable_delegated_coordinator: false`` (master gate) → effective depth
      clamps to ``0`` regardless of ``max_delegation_depth`` (the master gate
      wins).
    - ``decision_record_policy`` not in {minimal, verbose} → ``minimal`` with a
      diagnostic.

    Pure and deterministic.
    """
    diagnostics: list[str] = []

    raw_depth = policy.max_delegation_depth
    if not isinstance(raw_depth, bool) and isinstance(raw_depth, int) and 0 <= raw_depth <= HARD_CEILING_DEPTH:
        depth = raw_depth
    else:
        depth = 0
        diagnostics.append(f"invalid_max_delegation_depth:{raw_depth!r}_clamped_to_0")

    raw_lanes = policy.max_active_child_lanes
    if not isinstance(raw_lanes, bool) and isinstance(raw_lanes, int) and raw_lanes >= 1:
        lanes = raw_lanes
    else:
        lanes = 1
        diagnostics.append(f"invalid_max_active_child_lanes:{raw_lanes!r}_clamped_to_1")

    record_policy = policy.decision_record_policy
    if record_policy not in RECORD_POLICIES:
        diagnostics.append(f"invalid_decision_record_policy:{record_policy!r}_clamped_to_minimal")
        record_policy = RECORD_POLICY_MINIMAL

    if not policy.enable_delegated_coordinator and depth != 0:
        # Master gate wins: nested delegation cannot happen, effective depth is 0.
        diagnostics.append("master_gate_disabled_clamps_effective_depth_to_0")
        depth = 0

    return EffectiveDelegationPolicy(
        enable_delegated_coordinator=policy.enable_delegated_coordinator,
        enable_grandchild_dispatch=policy.enable_grandchild_dispatch,
        effective_max_depth=depth,
        effective_max_active_child_lanes=lanes,
        decision_record_policy=record_policy,
        diagnostics=tuple(diagnostics),
    )


@dataclass(frozen=True)
class GrandchildPolicyGate:
    """The resolved depth-2 dispatch permission for the current delegation state.

    ``permitted`` is true only when the effective policy admits a grandchild lane
    at :attr:`new_lane_depth`. When false, ``reason`` carries the policy
    ``REASON_*`` token. ``effective`` is the normalized policy (its
    ``diagnostics`` are surfaced for the audit record).
    """

    permitted: bool
    new_lane_depth: int
    effective: EffectiveDelegationPolicy
    reason: Optional[str] = None


def resolve_grandchild_policy_gate(
    policy: DelegationPolicy,
    *,
    current_depth: int = DEFAULT_DELEGATED_COORDINATOR_DEPTH,
    active_grandchild_lanes: int = 0,
) -> GrandchildPolicyGate:
    """Resolve whether the policy permits a grandchild lane at ``current_depth + 1``.

    The fail-closed order (master gate wins, then grandchild flag, then depth
    ceiling, then capacity), per ``delegation-policy-project-config.md`` ``### knob
    間の安全な相互作用``:

    1. ``enable_delegated_coordinator: false`` →
       :data:`REASON_MASTER_GATE_DISABLED`.
    2. ``enable_grandchild_dispatch: false`` → :data:`REASON_GRANDCHILD_DISABLED`.
    3. the new lane depth (``current_depth + 1``) exceeds the effective max depth
       or the hard ceiling → :data:`REASON_DEPTH_CEILING_EXCEEDED` (covers
       ``max_delegation_depth < 2`` and an invalid clamped-to-0 depth).
    4. ``active_grandchild_lanes >= max_active_child_lanes`` →
       :data:`REASON_ACTIVE_LANE_CAPACITY_EXHAUSTED`.

    Pure and deterministic over its inputs.
    """
    eff = effective_policy(policy)
    new_lane_depth = current_depth + 1

    def _gate(permitted: bool, reason: Optional[str]) -> GrandchildPolicyGate:
        return GrandchildPolicyGate(
            permitted=permitted,
            new_lane_depth=new_lane_depth,
            effective=eff,
            reason=reason,
        )

    if not eff.enable_delegated_coordinator:
        return _gate(False, REASON_MASTER_GATE_DISABLED)
    if not eff.enable_grandchild_dispatch:
        return _gate(False, REASON_GRANDCHILD_DISABLED)
    if new_lane_depth > eff.effective_max_depth or new_lane_depth > HARD_CEILING_DEPTH:
        return _gate(False, REASON_DEPTH_CEILING_EXCEEDED)
    if active_grandchild_lanes >= eff.effective_max_active_child_lanes:
        return _gate(False, REASON_ACTIVE_LANE_CAPACITY_EXHAUSTED)
    return _gate(True, None)


# --- multi-coordinator callback coverage --------------------------------------


def validate_grandchild_callback_targets(
    targets: Optional[Iterable[CallbackTarget]],
    *,
    owning_coverage: Optional[str] = None,
) -> tuple[CallbackTarget, ...]:
    """Validate the grandchild dispatch callback targets, failing closed on coverage gaps.

    Layers the decision-records §4.1 *multi-coordinator coverage* requirement on
    top of #12457's :func:`validate_callback_targets` (which already mandates a
    required ``delegation_parent``). For the GK parent -> mozyo_bridge ->
    grandchild route, callbacks to **both** the GK parent route (``delegation_parent``)
    and the mozyo_bridge coordinator route (``owning_us_coordinator`` /
    ``audit_coordinator``) must be replayable. A lone ``delegation_parent``
    callback does not satisfy coverage unless the owning / audit coordinator is
    **explicitly** declared the same route via
    ``owning_coverage=``:data:`OWNING_COVERAGE_SAME_AS_PARENT` — coverage is never
    omitted by assumption.

    Raises :class:`DelegationLaunchAdoptError` on any violation; returns the
    normalized tuple otherwise.
    """
    items = validate_callback_targets(targets)
    has_owning = any(
        t.purpose in (PURPOSE_OWNING_US_COORDINATOR, PURPOSE_AUDIT_COORDINATOR)
        for t in items
    )
    if has_owning:
        return items
    if owning_coverage == OWNING_COVERAGE_SAME_AS_PARENT:
        return items
    if owning_coverage:
        raise DelegationLaunchAdoptError(
            f"unknown owning_coverage {owning_coverage!r}; expected "
            f"{OWNING_COVERAGE_SAME_AS_PARENT!r} or a distinct owning_us_coordinator "
            f"/ audit_coordinator callback target."
        )
    raise DelegationLaunchAdoptError(
        "grandchild dispatch callback coverage requires an explicit "
        "owning_us_coordinator / audit_coordinator target (the mozyo_bridge "
        "coordinator route) OR an explicit same_as_delegation_parent declaration; "
        "a lone delegation_parent callback does not satisfy multi-coordinator "
        "coverage (delegated-coordinator-decision-records.md §4.1)."
    )


# --- grandchild dispatch decision ---------------------------------------------


@dataclass(frozen=True)
class GrandchildDispatchDecision:
    """The resolved grandchild (depth-2) dispatch decision (Redmine #12458).

    ``outcome`` is one of :data:`OUTCOME_DISPATCH_ADOPT` /
    :data:`OUTCOME_DISPATCH_LAUNCH` / :data:`OUTCOME_NO_DISPATCH` /
    :data:`OUTCOME_FAIL_CLOSED`. For ``dispatch_adopt`` :attr:`selected` is the
    single resolved grandchild Codex gateway candidate; ``dispatch_launch`` /
    ``no_dispatch`` / ``fail_closed`` carry ``None``. ``reason`` carries a
    ``REASON_*`` token only when fail-closed (a policy-gate reason, or a
    launch/adopt selection reason inherited from #12457 via :attr:`launch_adopt`).
    :attr:`policy_gate` always records the resolved depth-2 permission (and the
    effective-policy diagnostics) so the audit record can show why a route did or
    did not form. :attr:`visible_lane_required` is always true — the grandchild
    lane is a declared durable-anchored cockpit lane, never a hidden subagent.
    """

    outcome: str
    policy_gate: GrandchildPolicyGate
    purpose: str = PURPOSE_PRESERVE_CONTEXT
    reason: Optional[str] = None
    launch_adopt: Optional[LaunchAdoptDecision] = None
    selected: Optional[DelegationCandidate] = None
    no_dispatch_reason: Optional[str] = None
    child_project: Optional[str] = None
    target_repo_identity: Optional[str] = None
    visible_lane_required: bool = True

    @property
    def is_dispatch(self) -> bool:
        return self.outcome in (OUTCOME_DISPATCH_ADOPT, OUTCOME_DISPATCH_LAUNCH)

    @property
    def is_adopt(self) -> bool:
        return self.outcome == OUTCOME_DISPATCH_ADOPT

    @property
    def is_launch(self) -> bool:
        return self.outcome == OUTCOME_DISPATCH_LAUNCH

    @property
    def is_no_dispatch(self) -> bool:
        return self.outcome == OUTCOME_NO_DISPATCH

    @property
    def is_fail_closed(self) -> bool:
        return self.outcome == OUTCOME_FAIL_CLOSED

    @property
    def delegation_depth(self) -> int:
        """The depth the dispatched grandchild lane occupies (audit-safe shallow)."""
        return self.policy_gate.new_lane_depth

    def to_dict(self) -> dict[str, object]:
        eff = self.policy_gate.effective
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "purpose": self.purpose,
            "no_dispatch_reason": self.no_dispatch_reason,
            "delegation_depth": self.delegation_depth,
            "visible_lane_required": self.visible_lane_required,
            "policy_permitted": self.policy_gate.permitted,
            "policy_gate_reason": self.policy_gate.reason,
            "child_project": self.child_project,
            "target_repo_identity": self.target_repo_identity,
            "effective_policy": {
                "enable_delegated_coordinator": eff.enable_delegated_coordinator,
                "enable_grandchild_dispatch": eff.enable_grandchild_dispatch,
                "effective_max_depth": eff.effective_max_depth,
                "effective_max_active_child_lanes": eff.effective_max_active_child_lanes,
                "decision_record_policy": eff.decision_record_policy,
                "diagnostics": list(eff.diagnostics),
            },
            "selected": self.selected.summary_dict() if self.selected else None,
            "launch_adopt": self.launch_adopt.to_dict() if self.launch_adopt else None,
        }


def resolve_grandchild_dispatch(
    *,
    policy: DelegationPolicy,
    mode: str,
    candidates: Sequence[DelegationCandidate],
    target_repo_identity: Optional[str],
    current_depth: int = DEFAULT_DELEGATED_COORDINATOR_DEPTH,
    active_grandchild_lanes: int = 0,
    excluded_lane_ids: Iterable[str] = (),
    child_project: Optional[str] = None,
    purpose: str = PURPOSE_PRESERVE_CONTEXT,
) -> GrandchildDispatchDecision:
    """Resolve a fail-closed grandchild (depth-2) dispatch decision.

    The decision is the composition of two gates:

    1. **Policy gate** (:func:`resolve_grandchild_policy_gate`): if the policy
       does not permit a depth-2 lane (master gate off / grandchild off / depth
       ceiling / capacity), fail closed with the policy reason *before* any
       candidate is even considered — the durable decision is recorded before any
       pane notification or runtime mutation.
    2. **Launch/adopt selection** (reused :func:`resolve_launch_adopt`): with the
       policy permitting, run the #12457 fail-closed selector over the discovery
       candidates with ``required_role`` fixed to the Codex gateway — the route
       never lands directly at the grandchild Claude. Its ``adopt`` / ``launch`` /
       ``fail_closed`` outcomes map to :data:`OUTCOME_DISPATCH_ADOPT` /
       :data:`OUTCOME_DISPATCH_LAUNCH` / :data:`OUTCOME_FAIL_CLOSED` (carrying the
       inherited selection reason).

    An unknown ``mode`` raises :class:`DelegationLaunchAdoptError` (a caller
    error), the same as #12457. Pure and deterministic over its inputs.
    """
    gate = resolve_grandchild_policy_gate(
        policy,
        current_depth=current_depth,
        active_grandchild_lanes=active_grandchild_lanes,
    )
    if not gate.permitted:
        return GrandchildDispatchDecision(
            outcome=OUTCOME_FAIL_CLOSED,
            policy_gate=gate,
            purpose=purpose,
            reason=gate.reason,
            child_project=child_project,
            target_repo_identity=(target_repo_identity or None),
        )

    launch_adopt = resolve_launch_adopt(
        mode=mode,
        candidates=candidates,
        target_repo_identity=target_repo_identity,
        required_role=ROLE_CODEX,
        excluded_lane_ids=excluded_lane_ids,
        child_project=child_project,
    )

    if launch_adopt.is_adopt:
        outcome, selected, reason = OUTCOME_DISPATCH_ADOPT, launch_adopt.selected, None
    elif launch_adopt.is_launch:
        outcome, selected, reason = OUTCOME_DISPATCH_LAUNCH, None, None
    else:
        outcome, selected, reason = OUTCOME_FAIL_CLOSED, None, launch_adopt.reason

    return GrandchildDispatchDecision(
        outcome=outcome,
        policy_gate=gate,
        purpose=purpose,
        reason=reason,
        launch_adopt=launch_adopt,
        selected=selected,
        child_project=child_project,
        target_repo_identity=launch_adopt.target_repo_identity,
    )


def resolve_no_dispatch(
    *,
    policy: DelegationPolicy,
    no_dispatch_reason: str,
    current_depth: int = DEFAULT_DELEGATED_COORDINATOR_DEPTH,
    purpose: str = PURPOSE_PRESERVE_CONTEXT,
    child_project: Optional[str] = None,
) -> GrandchildDispatchDecision:
    """Record an explicit ``grandchild_dispatch: avoided`` no-dispatch decision.

    The delegated coordinator may always choose to keep context-consuming work in
    its own lane (the spine ``#### 孫 dispatch を避けてよい作業`` / decision-records
    §3 path) — this is independent of whether the policy *would* permit a depth-2
    lane, so the policy gate is recorded for the audit summary but does not block
    the no-dispatch. ``no_dispatch_reason`` must be non-empty; the known tokens
    are :data:`KNOWN_NO_DISPATCH_REASONS` and a free-form ``<具体記述>`` borderline
    reason is also accepted (§3 leaves the tail open).

    Raises :class:`DelegationLaunchAdoptError` on a blank reason.
    """
    if not (no_dispatch_reason or "").strip():
        raise DelegationLaunchAdoptError(
            "a no-dispatch (grandchild_dispatch: avoided) decision must carry a "
            "non-empty reason (decision-records §3)."
        )
    gate = resolve_grandchild_policy_gate(policy, current_depth=current_depth)
    return GrandchildDispatchDecision(
        outcome=OUTCOME_NO_DISPATCH,
        policy_gate=gate,
        purpose=purpose,
        no_dispatch_reason=no_dispatch_reason.strip(),
        child_project=child_project,
    )


__all__ = (
    "HARD_CEILING_DEPTH",
    "DEFAULT_DELEGATED_COORDINATOR_DEPTH",
    "RECORD_POLICY_MINIMAL",
    "RECORD_POLICY_VERBOSE",
    "RECORD_POLICIES",
    "OUTCOME_DISPATCH_ADOPT",
    "OUTCOME_DISPATCH_LAUNCH",
    "OUTCOME_NO_DISPATCH",
    "OUTCOME_FAIL_CLOSED",
    "REASON_MASTER_GATE_DISABLED",
    "REASON_GRANDCHILD_DISABLED",
    "REASON_DEPTH_CEILING_EXCEEDED",
    "REASON_ACTIVE_LANE_CAPACITY_EXHAUSTED",
    "PURPOSE_PRESERVE_CONTEXT",
    "NO_DISPATCH_REASON_CONTEXT_COST_LOW",
    "NO_DISPATCH_REASON_SINGLE_PASS",
    "NO_DISPATCH_REASON_URGENT_MINIMAL",
    "KNOWN_NO_DISPATCH_REASONS",
    "OWNING_COVERAGE_SAME_AS_PARENT",
    "DelegationPolicy",
    "EffectiveDelegationPolicy",
    "effective_policy",
    "GrandchildPolicyGate",
    "resolve_grandchild_policy_gate",
    "validate_grandchild_callback_targets",
    "GrandchildDispatchDecision",
    "resolve_grandchild_dispatch",
    "resolve_no_dispatch",
)
