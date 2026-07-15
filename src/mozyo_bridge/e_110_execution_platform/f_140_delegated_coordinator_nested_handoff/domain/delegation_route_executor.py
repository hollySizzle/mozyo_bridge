"""Delegated route live executor core (Redmine #12557).

The #12550 planner
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner`) emits a *pure*
:class:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner.RoutePlan` — an ordered,
typed command/record sequence — and explicitly deferred the side-effecting
*executor* that turns it into live cockpit / tmux / Redmine mutations (planner
docstring, ``executor (a deliberately deferred follow-up)``). The #12553 route
identity ledger (:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger`) likewise
shipped the stable-identity / live-re-resolution contract and deferred wiring it
into the live ``handoff send`` path. This module is that shared follow-up: the
live executor core that runs a :class:`RoutePlan` against the route identity
ledger and emits the #12558 replayable Redmine record package.

Design (the #12546 real-machine smoke must never be the first place these
contracts are exercised — ``delegated-coordinator-smoke-test-frame.md``
``## 推奨実装方針``):

- **The plan is authority; the executor never re-decides routing.** A plan that
  is not :attr:`RoutePlan.is_pass_eligible` performs **zero** sends / stamps and
  is recorded with the matching fail-closed classification
  (``failed_acceptance`` / ``blocked`` / ``insufficient``). The executor cannot
  upgrade a rejected plan into a live route.
- **Re-resolve immediately before every send.** Each handoff / stamp hop
  re-scans a freshly fetched live inventory through the backend-neutral resolver
  (:func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver.resolve_for_route_target_neutral`,
  Redmine #13302), selected by :attr:`ExecutionContext.backend`: ``tmux`` (the
  default) is byte-for-byte the ledger's
  :func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger.resolve_for_route_target`,
  while ``herdr`` re-resolves the same injected snapshot as a live ``agent list``.
  Either way a moved pane is transparently recovered and a stale / ambiguous /
  missing target fails closed *before* anything is sent. A cached
  ``last_seen_pane_id`` is never the send authority (#12553 Required behavior #2).
- **Fail closed, and never count notification as evidence.** A fail-closed
  re-resolution blocks the route; a send that does not submit-complete is
  environmental; a forbidden cross-project Claude resolution is a routing
  invariant violation; a Redmine record write failure is non-PASS. None of these
  can reach ``PASS`` (#12558 classification contract).

Purity / boundary (mirrors #12550 / #12553): this module performs no real I/O
itself. tmux, the live inventory, the handoff transport, and the Redmine write
are all **injected** protocols (:class:`LiveInventoryProvider`,
:class:`HandoffTransport`, :class:`StampTransport`, and the #12558
:class:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_records.RouteRecordSink`); the
live, credential-gated adapters are the deferred actuator follow-up and the
classical tests drive fakes. Private topology (a resolved ``%N`` pane id) never
enters a public record — it stays on the runtime request objects only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (
    DelegationRoutePlanError,
    PLAN_BLOCKED,
    PLAN_FAILED,
    PLAN_INSUFFICIENT,
    REALIZE_ADOPT,
    REALIZE_LAUNCH,
    STEP_CALLBACK_RECORD,
    STEP_CHILD_HANDOFF,
    STEP_GRANDCHILD_STAMP,
    STEP_PARENT_DECISION,
    STEP_WORKER_HANDOFF,
    TARGET_CHILD_GATEWAY,
    TARGET_GRANDCHILD_GATEWAY,
    TARGET_SAME_LANE_WORKER,
    RoutePlan,
    PlannedStep,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_records import (
    CLASS_BLOCKED,
    CLASS_ENVIRONMENTAL,
    CLASS_FAILED_ACCEPTANCE,
    CLASS_INSUFFICIENT,
    CLASS_PASS,
    CallbackOutcome,
    ClassificationInputs,
    NullRouteRecordSink,
    RouteExecutionRecord,
    RouteRecordPackage,
    RouteRecordReceipt,
    RouteRecordSink,
    all_required_callbacks_recorded,
    baseline_record,
    callback_outcome_record,
    child_delivery_record,
    child_result_record,
    classify_final,
    final_classification_record,
    grandchild_realization_record,
    parent_decision_record,
    validate_classification,
    worker_evidence_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    RESOLVE_OK,
    ROLE_CLAUDE,
    ROLE_CODEX,
    RouteIdentityLedger,
    RouteResolution,
    TARGET_UNAVAILABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (
    BACKEND_TMUX,
    resolve_for_route_target_neutral,
)

#: A non-ledger resolution status used only inside an executor record when a hop
#: is refused before live re-resolution (a forbidden cross-project Claude send).
#: Distinct from the ledger fail-closed tokens so a replay can tell a routing
#: invariant violation from a target that merely could not be found live.
RESOLUTION_INVARIANT_REFUSED: str = "cross_project_claude_direct_send"

#: Map of the #12550 fail-closed dispositions to the #12558 classification an
#: un-executable plan is recorded with. A pass-eligible plan is not in this map;
#: its classification is computed from the live execution instead.
_DISPOSITION_CLASSIFICATION: dict[str, str] = {
    PLAN_FAILED: CLASS_FAILED_ACCEPTANCE,
    PLAN_BLOCKED: CLASS_BLOCKED,
    PLAN_INSUFFICIENT: CLASS_INSUFFICIENT,
}


class DelegationRouteExecutorError(ValueError):
    """A live-executor input is malformed (a programming error, fail-closed).

    A *runtime* outcome (a fail-closed re-resolution, a blocked send, a write
    failure) is carried in the record package and the final classification, never
    raised. Only a malformed input — the wrong plan / ledger type, an unknown
    route target token — raises here.
    """


# ---------------------------------------------------------------------------
# Injected side-effect transports. Each is a small protocol so the classical
# tests drive fakes; the live, credential-/tmux-gated adapters are deferred.
# ---------------------------------------------------------------------------


@runtime_checkable
class LiveInventoryProvider(Protocol):
    """Fetches the current live inventory for the execution's backend.

    Called once per hop, immediately before re-resolution, so every send is
    matched against the *current* topology rather than a snapshot taken at plan
    time. The row shape is the one the selected :attr:`ExecutionContext.backend`
    expects — ``agents targets`` / ``try_pane_lines`` panes for ``tmux``, or a live
    ``agent list`` snapshot for ``herdr`` (Redmine #13302); the backend-neutral
    resolver normalizes either into the row sequence
    :func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger.resolve_route` consumes.
    """

    def snapshot(self) -> Sequence[Mapping[str, object]]:
        ...


@dataclass(frozen=True)
class HandoffSendRequest:
    """A single re-resolved handoff send the executor asks the transport to deliver.

    :attr:`pane_id` is the live, re-resolved target (runtime topology — it never
    enters a public record). :attr:`step_key` is a deterministic idempotency key
    so a retried execution does not double-send. :attr:`body_pointer` is a
    public-safe durable anchor pointer, never the full resolved contract.
    """

    route_target: str
    role: str
    role_profile: str
    pane_id: str
    step_key: str
    body_pointer: str


@dataclass(frozen=True)
class HandoffSendOutcome:
    """The transport's result for one send: did it submit-complete, and why not."""

    delivered: bool
    reason: str = "sent"


@runtime_checkable
class HandoffTransport(Protocol):
    """The injected boundary that delivers a re-resolved handoff to a live pane."""

    def send(self, request: HandoffSendRequest) -> HandoffSendOutcome:
        ...


@dataclass(frozen=True)
class StampRequest:
    """A grandchild ``KIND`` / ``DEPTH`` / ``PARENT`` projection stamp request."""

    route_target: str
    pane_id: str
    unit_id: str
    depth: int
    parent: str
    step_key: str


@dataclass(frozen=True)
class StampOutcome:
    """The transport's result for one stamp."""

    stamped: bool
    reason: str = "stamped"


@runtime_checkable
class StampTransport(Protocol):
    """The injected boundary that stamps the live grandchild lane projection."""

    def stamp(self, request: StampRequest) -> StampOutcome:
        ...


# ---------------------------------------------------------------------------
# Execution context + result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    """The public-safe, durable facts a route execution needs beyond the plan.

    :attr:`route_ids` maps each logical planner target
    (``child_gateway`` / ``grandchild_gateway`` / ``same_lane_worker``) to the
    ledger ``route_id`` whose stable identity is re-resolved for that hop.
    :attr:`callback_targets` are the required/optional parent callback routes the
    callback record must account for. :attr:`contaminated` / :attr:`insufficient_read`
    carry an upstream read-boundary verdict into the classification so a
    contaminated read can never be reported as PASS.

    :attr:`backend` selects the liveness backend the hop re-resolution matches
    against (Redmine #13302): ``tmux`` (the default) re-resolves the injected
    inventory as ``try_pane_lines`` rows — byte-for-byte the pre-#13302 behaviour
    and record projection — while ``herdr`` re-resolves the same injected snapshot
    as live ``agent list`` rows through the backend-neutral resolver. The backend
    is a per-execution selector, not a per-identity field: the ledger identities
    are backend-agnostic and the same fail-closed vocabulary is projected either
    way.
    """

    source_issue: str
    test_model: str
    base_commit: str
    route_ids: Mapping[str, str]
    callback_targets: tuple[CallbackOutcome, ...] = ()
    child_issue: str = "not_created"
    decision_basis: str = "parent project config + durable issue context"
    grandchild_unit: str = ""
    grandchild_parent: str = ""
    fresh_panes: bool = True
    contaminated: bool = False
    insufficient_read: bool = False
    cross_project: bool = True
    backend: str = BACKEND_TMUX
    #: The per-target expected-role map for route re-resolution (Redmine #13569
    #: Increment 2B). ``None`` uses the built-in binding (gateway codex / worker claude),
    #: byte-identical; an application caller supplies the binding-resolved map (via
    #: ``route_identity_ledger.expected_roles_for``) so a rebound provider re-resolves
    #: against ITS pane rather than being refused by the role-mismatch guard.
    expected_roles: "Optional[Mapping[str, str]]" = None


@dataclass(frozen=True)
class ExecutionResult:
    """The replayable outcome of running one route plan.

    :attr:`package` is the ordered #12558 record package; :attr:`classification`
    is the single final verdict. :attr:`sends` / :attr:`stamps` /
    :attr:`resolutions` are the side-effects that were *attempted*, in order, for
    scenario assertions; :attr:`receipts` are the per-record persistence results.
    """

    classification: str
    reason: str
    package: RouteRecordPackage
    sends: tuple[HandoffSendRequest, ...] = ()
    stamps: tuple[StampRequest, ...] = ()
    resolutions: tuple[RouteResolution, ...] = ()
    receipts: tuple[RouteRecordReceipt, ...] = ()

    @property
    def is_pass(self) -> bool:
        return self.classification == CLASS_PASS

    @property
    def record_kinds(self) -> tuple[str, ...]:
        return self.package.kinds()

    @property
    def write_failed(self) -> bool:
        return any(not receipt.persisted for receipt in self.receipts)


class DelegationRouteExecutor:
    """Runs a #12550 :class:`RoutePlan` against the #12553 ledger, fail-closed.

    The executor is *stateless across calls*: every run is a pure function of
    ``(plan, ledger, context, providers)`` plus the live inventory the provider
    returns, so re-executing the same plan is idempotent at the executor level
    (a deduping transport keyed on :attr:`HandoffSendRequest.step_key` makes the
    side-effects idempotent too).
    """

    def __init__(
        self,
        *,
        inventory: LiveInventoryProvider,
        handoff: HandoffTransport,
        stamp: StampTransport,
        record_sink: Optional[RouteRecordSink] = None,
    ) -> None:
        self._inventory = inventory
        self._handoff = handoff
        self._stamp = stamp
        self._sink: RouteRecordSink = record_sink or NullRouteRecordSink()

    # -- public API --------------------------------------------------------

    def execute(
        self, plan: RoutePlan, ledger: RouteIdentityLedger, context: ExecutionContext
    ) -> ExecutionResult:
        """Execute ``plan`` and return the replayable record package + verdict."""
        if not isinstance(plan, RoutePlan):
            raise DelegationRouteExecutorError(
                f"execute requires a RoutePlan, got {type(plan).__name__}"
            )
        if not isinstance(ledger, RouteIdentityLedger):
            raise DelegationRouteExecutorError(
                f"execute requires a RouteIdentityLedger, got {type(ledger).__name__}"
            )

        run = _Run(self, plan, ledger, context)
        return run.go()


@dataclass
class _Run:
    """Mutable per-execution accumulator (one instance per :meth:`execute`)."""

    executor: DelegationRouteExecutor
    plan: RoutePlan
    ledger: RouteIdentityLedger
    context: ExecutionContext
    package: RouteRecordPackage = field(init=False)
    sends: list[HandoffSendRequest] = field(default_factory=list)
    stamps: list[StampRequest] = field(default_factory=list)
    resolutions: list[RouteResolution] = field(default_factory=list)
    receipts: list[RouteRecordReceipt] = field(default_factory=list)
    blocked: bool = False
    invariant_violation: bool = False
    environmental: bool = False
    route_fully_realized: bool = True
    anchor_workspace: str = ""
    stopped: bool = False

    def __post_init__(self) -> None:
        self.package = RouteRecordPackage(source_issue=self.context.source_issue)

    # -- orchestration -----------------------------------------------------

    def go(self) -> ExecutionResult:
        self._emit(
            baseline_record(
                source_issue=self.context.source_issue,
                test_model=self.context.test_model,
                fresh_panes=self.context.fresh_panes,
                base_commit=self.context.base_commit,
            )
        )

        # A non-pass-eligible plan never mutates live state: record its mapped
        # fail-closed classification and stop. The executor cannot upgrade a
        # rejected route into a live send.
        if not self.plan.is_pass_eligible:
            classification = _DISPOSITION_CLASSIFICATION.get(
                self.plan.disposition, CLASS_INSUFFICIENT
            )
            return self._finish(classification, f"plan_{self.plan.diagnostic}")

        for step in self.plan.steps:
            if self.stopped:
                break
            self._run_step(step)

        return self._finish(*self._classify())

    def _run_step(self, step: PlannedStep) -> None:
        if step.kind == STEP_PARENT_DECISION:
            self._parent_decision()
        elif step.kind == STEP_CHILD_HANDOFF:
            self._child_handoff(step)
        elif step.kind == STEP_GRANDCHILD_STAMP:
            self._grandchild_stamp(step)
        elif step.kind == STEP_WORKER_HANDOFF:
            self._worker_handoff(step)
        elif step.kind == STEP_CALLBACK_RECORD:
            self._callback_record()
        # An unknown step kind is impossible from the typed planner; ignore
        # defensively rather than raise mid-route.

    # -- individual steps --------------------------------------------------

    def _parent_decision(self) -> None:
        child_delegation = (
            "used" if self.plan.requested_child_project else "not_applicable"
        )
        self._emit(
            parent_decision_record(
                source_issue=self.context.source_issue,
                child_project=self.plan.requested_child_project,
                child_delegation=child_delegation,
                role_profile_chain=self.plan.role_profile_chain,
                basis=self.context.decision_basis,
            )
        )

    def _child_handoff(self, step: PlannedStep) -> None:
        resolution = self._resolve(TARGET_CHILD_GATEWAY)
        self.resolutions.append(resolution)
        send_outcome = "not_attempted"
        if resolution.is_resolved:
            self.anchor_workspace = (
                resolution.identity.workspace_id if resolution.identity else ""
            )
            send_outcome = self._send(
                TARGET_CHILD_GATEWAY, ROLE_CODEX, _profile(step), resolution
            )
        else:
            self._mark_fail_closed(resolution)
        self._emit(
            child_delivery_record(
                source_issue=self.context.source_issue,
                resolution=resolution,
                role_profile=_profile(step),
                send_outcome=send_outcome,
            )
        )
        # Child result is derived from the realized route shape.
        if not self.stopped:
            self._child_result()

    def _child_result(self) -> None:
        if self.plan.grandchild_realization in (REALIZE_ADOPT, REALIZE_LAUNCH):
            dispatch = f"dispatch_{self.plan.grandchild_realization}"
            reason = "not_applicable"
        else:
            dispatch = "avoided"
            reason = "grandchild_not_required"
        self._emit(
            child_result_record(
                source_issue=self.context.source_issue,
                child_issue=self.context.child_issue,
                grandchild_dispatch=dispatch,
                no_dispatch_reason=reason,
            )
        )

    def _grandchild_stamp(self, step: PlannedStep) -> None:
        resolution = self._resolve(TARGET_GRANDCHILD_GATEWAY)
        self.resolutions.append(resolution)
        stamp_outcome = "not_attempted"
        if resolution.is_resolved:
            request = StampRequest(
                route_target=TARGET_GRANDCHILD_GATEWAY,
                pane_id=resolution.resolved_pane_id,
                unit_id=self.context.grandchild_unit,
                depth=2,
                parent=self.context.grandchild_parent,
                step_key=self._step_key("grandchild_stamp"),
            )
            self.stamps.append(request)
            outcome = self.executor._stamp.stamp(request)
            if outcome.stamped:
                stamp_outcome = "stamped"
            else:
                stamp_outcome = f"blocked:{outcome.reason}"
                self.environmental = True
                self.route_fully_realized = False
                self.stopped = True
        else:
            self._mark_fail_closed(resolution)
        self._emit(
            grandchild_realization_record(
                source_issue=self.context.source_issue,
                resolution=resolution,
                realization=self.plan.grandchild_realization,
                stamp_outcome=stamp_outcome,
                depth=2,
                parent=self.context.grandchild_parent,
            )
        )

    def _worker_handoff(self, step: PlannedStep) -> None:
        # Two worker-handoff steps share this kind; dispatch on the route target.
        if step.route_target == TARGET_GRANDCHILD_GATEWAY:
            resolution = self._resolve(TARGET_GRANDCHILD_GATEWAY)
            self.resolutions.append(resolution)
            if resolution.is_resolved:
                self.anchor_workspace = (
                    resolution.identity.workspace_id if resolution.identity else ""
                )
                self._send(
                    TARGET_GRANDCHILD_GATEWAY, ROLE_CODEX, _profile(step), resolution
                )
            else:
                self._mark_fail_closed(resolution)
            return

        # same_lane_worker: the only Claude target, and only because it never
        # crosses a project boundary. A worker identity in a different workspace
        # than the gateway it descends from is a forbidden cross-project Claude
        # send — fail closed before any live re-resolution.
        resolution, refused = self._resolve_worker()
        self.resolutions.append(resolution)
        if refused:
            send_outcome = f"blocked:{RESOLUTION_INVARIANT_REFUSED}"
        elif resolution.is_resolved:
            send_outcome = self._send(
                TARGET_SAME_LANE_WORKER, ROLE_CLAUDE, _profile(step), resolution
            )
        else:
            send_outcome = "not_attempted"
            self._mark_fail_closed(resolution)
        self._emit(
            worker_evidence_record(
                source_issue=self.context.source_issue,
                resolution=resolution,
                role_profile=_profile(step),
                send_outcome=send_outcome,
                fresh_projection="worker-observed DEPTH=2",
            )
        )

    def _callback_record(self) -> None:
        self._emit(
            callback_outcome_record(
                source_issue=self.context.source_issue,
                targets=self.context.callback_targets,
            )
        )

    # -- re-resolution helpers --------------------------------------------

    def _resolve(self, target_token: str) -> RouteResolution:
        """Re-resolve a logical target against a freshly fetched live inventory."""
        route_id = self.context.route_ids.get(target_token, "")
        identity = self.ledger.get(route_id) if route_id else None
        if identity is None:
            return RouteResolution(
                status=TARGET_UNAVAILABLE,
                route_id=route_id,
                detail="no ledger identity recorded for route target",
            )
        inventory = self.executor._inventory.snapshot()
        try:
            return resolve_for_route_target_neutral(
                target_token,
                identity,
                inventory,
                backend=self.context.backend,
                cross_project=self.context.cross_project,
                expected_roles=self.context.expected_roles,
            )
        except DelegationRoutePlanError as exc:
            # A role mismatch on a gateway/coordinator target is a malformed
            # re-resolution request: fail closed as a routing invariant rather
            # than crashing the run.
            self.invariant_violation = True
            self.route_fully_realized = False
            self.stopped = True
            return RouteResolution(
                status=RESOLUTION_INVARIANT_REFUSED,
                route_id=route_id,
                detail=str(exc),
            )

    def _resolve_worker(self) -> tuple[RouteResolution, bool]:
        """Re-resolve the same-lane worker, refusing a cross-project Claude send.

        ``cross_project`` is derived, not assumed: the same-lane worker must live
        in the same workspace as the gateway it descends from. A worker identity
        in a *different* workspace is a direct cross-project Claude send and is
        refused before any live match (defense-in-depth with the planner and the
        ledger's own guards).
        """
        route_id = self.context.route_ids.get(TARGET_SAME_LANE_WORKER, "")
        identity = self.ledger.get(route_id) if route_id else None
        if identity is None:
            return (
                RouteResolution(
                    status=TARGET_UNAVAILABLE,
                    route_id=route_id,
                    detail="no ledger identity recorded for same-lane worker",
                ),
                False,
            )
        worker_cross_project = bool(
            self.anchor_workspace and identity.workspace_id != self.anchor_workspace
        )
        inventory = self.executor._inventory.snapshot()
        try:
            resolution = resolve_for_route_target_neutral(
                TARGET_SAME_LANE_WORKER,
                identity,
                inventory,
                backend=self.context.backend,
                cross_project=worker_cross_project,
                expected_roles=self.context.expected_roles,
            )
        except DelegationRoutePlanError as exc:
            self.invariant_violation = True
            self.route_fully_realized = False
            self.stopped = True
            return (
                RouteResolution(
                    status=RESOLUTION_INVARIANT_REFUSED,
                    route_id=route_id,
                    detail=str(exc),
                ),
                True,
            )
        return resolution, False

    def _send(
        self,
        target_token: str,
        role: str,
        role_profile: str,
        resolution: RouteResolution,
    ) -> str:
        """Deliver a re-resolved handoff; a non-submit-complete send is environmental."""
        request = HandoffSendRequest(
            route_target=target_token,
            role=role,
            role_profile=role_profile,
            pane_id=resolution.resolved_pane_id,
            step_key=self._step_key(f"send_{target_token}"),
            body_pointer=f"redmine:{self.context.source_issue}",
        )
        self.sends.append(request)
        outcome = self.executor._handoff.send(request)
        if outcome.delivered:
            return "sent"
        # A send that did not submit-complete (marker timeout, focus) is an
        # environmental non-attempt; the route is not fully realized.
        self.environmental = True
        self.route_fully_realized = False
        self.stopped = True
        return f"blocked:{outcome.reason}"

    def _mark_fail_closed(self, resolution: RouteResolution) -> None:
        """A fail-closed re-resolution blocks the route (no send performed)."""
        self.blocked = True
        self.route_fully_realized = False
        self.stopped = True

    # -- finalization ------------------------------------------------------

    def _classify(self) -> tuple[str, str]:
        callbacks_recorded = all_required_callbacks_recorded(
            self.context.callback_targets
        )
        inputs = ClassificationInputs(
            contaminated=self.context.contaminated,
            invariant_violation=self.invariant_violation,
            blocked=self.blocked,
            redmine_write_failed=any(not r.persisted for r in self.receipts),
            environmental=self.environmental,
            route_fully_realized=self.route_fully_realized,
            callbacks_recorded=callbacks_recorded,
            insufficient_read=self.context.insufficient_read,
        )
        return classify_final(inputs)

    def _finish(self, classification: str, reason: str) -> ExecutionResult:
        classification = validate_classification(classification)
        # Persist the verdict, then fold the final record's *own* write outcome
        # back into the classification. ``_classify`` only saw the receipts of
        # the records emitted before this one, so a failure to persist the
        # final-classification record itself would otherwise leave a ``PASS``
        # result whose verdict was never durably written (``is_pass`` True while
        # ``write_failed`` True). A run whose verdict cannot be replayed is not a
        # PASS: downgrade fail-closed to ``environmental`` (Redmine write failure
        # is non-PASS — #12558 contract).
        record = final_classification_record(
            source_issue=self.context.source_issue,
            classification=classification,
            reason=reason,
        )
        receipt = self.executor._sink.persist(record)
        if classification == CLASS_PASS and (
            not receipt.persisted or any(not r.persisted for r in self.receipts)
        ):
            classification = CLASS_ENVIRONMENTAL
            reason = "redmine_record_write_failed"
            # Re-stamp the in-memory verdict record so the package and the
            # returned classification never disagree. The failed write means no
            # ``PASS`` verdict was persisted durably, so there is nothing to
            # contradict.
            record = final_classification_record(
                source_issue=self.context.source_issue,
                classification=classification,
                reason=reason,
            )
        self.package.append(record)
        self.receipts.append(receipt)
        return ExecutionResult(
            classification=classification,
            reason=reason,
            package=self.package,
            sends=tuple(self.sends),
            stamps=tuple(self.stamps),
            resolutions=tuple(self.resolutions),
            receipts=tuple(self.receipts),
        )

    def _emit(self, record: RouteExecutionRecord) -> None:
        """Append a record to the package and persist it through the injected sink."""
        self.package.append(record)
        self.receipts.append(self.executor._sink.persist(record))

    def _step_key(self, suffix: str) -> str:
        """A deterministic idempotency key so a retried run does not double-send."""
        return f"{self.context.source_issue}:{suffix}"


def _profile(step: PlannedStep) -> str:
    """The role-profile token carried by a handoff step (or empty when absent)."""
    return step.role_profile.role_profile if step.role_profile is not None else ""


__all__ = (
    "RESOLUTION_INVARIANT_REFUSED",
    "DelegationRouteExecutorError",
    "LiveInventoryProvider",
    "HandoffSendRequest",
    "HandoffSendOutcome",
    "HandoffTransport",
    "StampRequest",
    "StampOutcome",
    "StampTransport",
    "ExecutionContext",
    "ExecutionResult",
    "DelegationRouteExecutor",
)
