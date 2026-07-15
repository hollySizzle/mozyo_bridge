"""Fake executor + fixtures for delegated-coordinator route-plan scenarios.

Redmine #12491 (parent Feature #12531 ``120_シナリオ・受入テスト基盤``). This is
the **support** layer for the classical (Detroit-school) scenario / regression
tests: the subject-under-test is the real pure
:func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_coordinator_route_plan.plan_delegated_coordinator_route`
planner, and the only fakes live here — at the side-effecting boundary (the
durable record sink and the pane-send / stamp transport the CLI would drive). No
real tmux, Redmine, or pane scrollback is touched; the executor records what it
*would* do so a scenario can assert order and fail-closed behavior.

Per the tests-placement policy this module is **not** a ``test_*`` module, so
``unittest discover`` never collects it; it is imported by the scenario /
regression modules. It uses abstract placeholder identities only (no private home
paths / secret-shaped literals) per the public/private boundary rule.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Self-bootstrap the repo-local ``src`` so single-file / isolated discovery works
# regardless of whether the package is installed (the #12490 robustness idiom);
# harmless when ``src`` is already importable.
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_coordinator_route_plan import (  # noqa: E402
    DelegatedCoordinatorRoutePlan,
    RoutePlanRequest,
    STEP_SEND_SAME_LANE_WORKER,
    STEP_SEND_TO_GRANDCHILD_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import (  # noqa: E402
    CONFIDENCE_STRONG,
    DelegationCandidate,
    LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_projection import (  # noqa: E402
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (  # noqa: E402
    InventoryUnit,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_dispatch import DelegationPolicy  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_read_boundary import (  # noqa: E402
    ReadAccess,
    ReadBoundaryVerdict,
    SURFACE_PARENT_DESCRIPTION,
    SURFACE_PARENT_JOURNALS,
    SURFACE_TARGET_BODY,
    SURFACE_TARGET_JOURNALS,
    classify_read_boundary,
)

# --- abstract placeholder identities (public/private boundary safe) ------------

#: The durable dispatch anchor the route is planned from.
DURABLE_ANCHOR = "#12491 j#64439"
#: The delegated coordinator's own lane unit the grandchild must descend from.
DELEGATED_COORDINATOR_UNIT = "ws-child-project/lane-delegated"
#: The realized grandchild lane unit (depth-2 implementation lane).
GRANDCHILD_UNIT = "ws-child-project/lane-grandchild"
#: The canonical child repo identity (abstract placeholder, never a home path).
CHILD_REPO_IDENTITY = "/workspace/child-project"


# --- read-boundary fixtures ---------------------------------------------------


def allowed_read() -> ReadBoundaryVerdict:
    """A bounded read: target body + journals + parent description (``allowed``)."""
    return classify_read_boundary(
        [
            ReadAccess(SURFACE_TARGET_BODY, DURABLE_ANCHOR),
            ReadAccess(SURFACE_TARGET_JOURNALS, DURABLE_ANCHOR),
            ReadAccess(SURFACE_PARENT_DESCRIPTION, "#12531"),
        ]
    )


def contaminated_read() -> ReadBoundaryVerdict:
    """A read that reached parent journals — out of bounds (``contaminated``)."""
    return classify_read_boundary(
        [
            ReadAccess(SURFACE_TARGET_BODY, DURABLE_ANCHOR),
            ReadAccess(SURFACE_PARENT_JOURNALS, "#12531"),
        ]
    )


def insufficient_read() -> ReadBoundaryVerdict:
    """A read that never read the target anchor body (``insufficient``)."""
    return classify_read_boundary([ReadAccess(SURFACE_PARENT_DESCRIPTION, "#12531")])


# --- delegation fixtures ------------------------------------------------------


def permissive_policy() -> DelegationPolicy:
    """A policy that admits a depth-2 grandchild lane (master + grandchild on)."""
    return DelegationPolicy(
        enable_delegated_coordinator=True,
        enable_grandchild_dispatch=True,
        max_delegation_depth=2,
        max_active_child_lanes=2,
    )


def gateway_candidate(
    *,
    pane_id: str = "%21",
    lane_id: str = "lane-grandchild",
    repo_root: str = CHILD_REPO_IDENTITY,
    confidence: str = CONFIDENCE_STRONG,
    ambiguous: bool = False,
) -> DelegationCandidate:
    """A strong child Codex gateway discovery candidate for the grandchild lane."""
    return DelegationCandidate(
        pane_id=pane_id,
        role="codex",
        repo_root=repo_root,
        workspace_id="ws-child-project",
        lane_id=lane_id,
        confidence=confidence,
        ambiguous=ambiguous,
    )


def realized_grandchild_rows(
    *,
    unit_id: str = GRANDCHILD_UNIT,
    parent: str = DELEGATED_COORDINATOR_UNIT,
    repo_identity: Optional[str] = CHILD_REPO_IDENTITY,
    has_codex_gateway: bool = True,
    ambiguous: bool = False,
    lane_kind: str = LANE_KIND_IMPLEMENTATION,
    delegation_depth: object = 2,
) -> list[InventoryUnit]:
    """A live-inventory unit set in which a depth-2 implementation grandchild is realized.

    Typed :class:`InventoryUnit` (not a bare tuple): a positive realization
    requires a re-resolved codex gateway (``has_codex_gateway``) and a canonical
    ``repo_identity`` so the dispatch-selected target re-verifies end to end
    (Redmine #13571 j#75473 F2). A bare tuple can never realize. ``lane_kind`` /
    ``delegation_depth`` override the live shape so a test can align the live unit
    to a non-grandchild shape (Redmine #13571 j#75494 R5-F1).
    """
    return [
        InventoryUnit(
            unit_id=unit_id,
            lane_kind=lane_kind,
            delegation_depth=delegation_depth,  # type: ignore[arg-type]
            delegation_parent=parent,
            status="derived",
            repo_identity=repo_identity,
            has_codex_gateway=has_codex_gateway,
            ambiguous=ambiguous,
        )
    ]


def base_request(**overrides: object) -> RoutePlanRequest:
    """Build a :class:`RoutePlanRequest` with realized-route defaults; override per case.

    Defaults form the happy path: a bounded ``allowed`` read, a permissive policy,
    ``launch_or_adopt`` mode, one strong gateway candidate, the canonical child
    repo identity, and a realized depth-2 grandchild lane. Pass keyword overrides
    to drive a specific scenario (e.g. ``read_boundary=contaminated_read()`` or
    ``realized_units=[]``).
    """
    defaults: dict[str, object] = dict(
        durable_anchor=DURABLE_ANCHOR,
        read_boundary=allowed_read(),
        policy=permissive_policy(),
        launch_adopt_mode=LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
        candidates=[gateway_candidate()],
        target_repo_identity=CHILD_REPO_IDENTITY,
        delegated_coordinator_unit=DELEGATED_COORDINATOR_UNIT,
        realized_units=realized_grandchild_rows(),
    )
    defaults.update(overrides)
    return RoutePlanRequest(**defaults)  # type: ignore[arg-type]


# --- fake executor (the side-effecting boundary) ------------------------------


@dataclass
class ExecutionTrace:
    """What the fake executor *would* have done, for scenario assertions.

    ``durable_records`` are the journals the executor would record (always a
    ``route_plan`` record, plus a ``blocked`` record when blocked). ``pane_sends``
    are the handoff sends (each tagged with the hop and the role profile applied);
    ``stamp_commands`` are the live ``KIND``/``DEPTH``/``PARENT`` stamps. A blocked
    plan must yield a blocked record and **zero** pane sends / stamps.
    """

    durable_records: list[dict] = field(default_factory=list)
    pane_sends: list[dict] = field(default_factory=list)
    stamp_commands: list[dict] = field(default_factory=list)

    @property
    def recorded_kinds(self) -> list[str]:
        return [r["kind"] for r in self.durable_records]

    @property
    def sent(self) -> bool:
        return bool(self.pane_sends)


class FakeDelegationExecutor:
    """Drives a route plan against fake sinks instead of tmux / Redmine.

    The executor encodes the runtime contract the #12474 smoke kept violating:

    - a **blocked** plan records a durable ``blocked`` journal and performs no
      stamp and no pane send (a same-lane handoff is never silently performed);
    - a **proceed-to-grandchild-gateway** plan stamps the live grandchild
      breadcrumb and sends to the grandchild Codex gateway under the
      ``implementation_gateway`` role profile;
    - a **proceed-to-same-lane-worker** plan sends to the same-lane worker under
      the ``implementation_worker`` role profile, with no grandchild stamp / send.

    It is a fake, not a mock: it records state and the scenario asserts on it.
    """

    def __init__(self) -> None:
        self.trace = ExecutionTrace()

    def execute(self, plan: DelegatedCoordinatorRoutePlan) -> ExecutionTrace:
        # Always record the replayable plan first (the durable record is the
        # source of truth; the sends are downstream of it).
        self.trace.durable_records.append({"kind": "route_plan", "payload": plan.to_dict()})

        if plan.is_blocked:
            self.trace.durable_records.append(
                {"kind": "blocked", "reason": plan.blocked_reason}
            )
            return self.trace

        if plan.terminal_step == STEP_SEND_TO_GRANDCHILD_GATEWAY:
            self._stamp_grandchild(plan)
            gateway = plan.role_profile_for_hop("child_to_gateway")
            self.trace.pane_sends.append(
                {
                    "hop": "child_to_gateway",
                    "role_profile": gateway.role_profile,
                    "profile_source": gateway.profile_source,
                    "target_kind": "grandchild_codex_gateway",
                }
            )
        elif plan.terminal_step == STEP_SEND_SAME_LANE_WORKER:
            worker = plan.role_profile_for_hop("gateway_to_worker")
            self.trace.pane_sends.append(
                {
                    "hop": "gateway_to_worker",
                    "role_profile": worker.role_profile,
                    "profile_source": worker.profile_source,
                    "target_kind": "same_lane_worker",
                }
            )
        return self.trace

    def _stamp_grandchild(self, plan: DelegatedCoordinatorRoutePlan) -> None:
        gate = plan.realization_gate
        unit = gate.realized_grandchild_unit if gate else None
        self.trace.stamp_commands.append(
            {
                "unit_id": unit,
                "lane_kind": LANE_KIND_IMPLEMENTATION,
                "depth": 2,
            }
        )


__all__ = (
    "DURABLE_ANCHOR",
    "DELEGATED_COORDINATOR_UNIT",
    "GRANDCHILD_UNIT",
    "CHILD_REPO_IDENTITY",
    "allowed_read",
    "contaminated_read",
    "insufficient_read",
    "permissive_policy",
    "gateway_candidate",
    "realized_grandchild_rows",
    "base_request",
    "ExecutionTrace",
    "FakeDelegationExecutor",
)
