"""Integration-readiness tests for the delegated route live executor (#12560).

The #12557/#12558/#12559 first lane shipped the live executor, the Redmine
record package, and their fake-provider unit nets. This suite is the *connection*
check the #12546 real-machine smoke must not be the first to exercise: it wires
the already-shipped layers together end-to-end with fakes only —

- #12549 child-candidate resolver (`resolve_child_candidate`)
- #12550 route planner (`plan_delegation_route`)
- #12553 route identity ledger (`RouteIdentityLedger` live re-resolution)
- #12557/#12558 live executor + Redmine record package

and pins them against the #12547 acceptance oracle
(`tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_delegated_coordinator_acceptance_oracle.py`),
the durable authority for the acceptance classification vocabulary.

The central guarantee: **the live executor's runtime final classification agrees
with the #12547 oracle** across the acceptance scenario space, when the route is
driven through the real resolver and planner. The planner already cross-checks
its *disposition* against the oracle (an approved test); this suite extends that
chain one hop further — resolver -> planner -> executor -> final classification —
so a real run and the oracle cannot silently disagree before #12546.

Per #12560: focused fake/local tests only; no #12546 real-machine smoke; no real
cockpit/tmux/Redmine mutation; post-#12490 placement (catalog-resolved
`tests/integration/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/`).
The `%N` values are synthetic
inventory rows, never operator topology.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
# Import the pinned #12547 acceptance oracle by name (it lives under the unit
# bounded-context dir); this is the same cross-check the planner test uses.
sys.path.insert(
    0,
    str(
        ROOT
        / "tests"
        / "unit"
        / "e_110_execution_platform"
        / "f_140_delegated_coordinator_nested_handoff"
    ),
)

import test_delegated_coordinator_acceptance_oracle as oracle  # noqa: E402

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config import (  # noqa: E402
    ChildCandidate,
    DelegationConfig,
    resolve_child_candidate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (  # noqa: E402
    OUTPUT_EXECUTE,
    OUTPUT_RECOMMEND_ONLY,
    ROUTE_CLAUDE_DIRECT,
    ROUTE_CODEX_GATEWAY,
    RealizationCandidateView,
    RouteRequest,
    plan_delegation_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_records import (  # noqa: E402
    CALLBACK_SENT,
    CLASS_PASS,
    PERSIST_OK,
    PERSIST_TRANSPORT_ERROR,
    RECORD_BASELINE,
    RECORD_CALLBACK_OUTCOME,
    RECORD_CHILD_DELIVERY,
    RECORD_FINAL_CLASSIFICATION,
    RECORD_PARENT_DECISION,
    CallbackOutcome,
    RouteRecordReceipt,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_executor import (  # noqa: E402
    DelegationRouteExecutor,
    ExecutionContext,
    HandoffSendOutcome,
    StampOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (  # noqa: E402
    RouteIdentity,
    RouteIdentityLedger,
)

WS_CHILD = "ws-child-project"
LANE_DELEG = "lane-delegated"
LANE_GC = "lane-grandchild"
RT_CHILD, RT_GC, RT_WORKER = "rt-child", "rt-gc", "rt-worker"
SOURCE_ISSUE = "#12560"


# --- minimal fakes (side-effecting boundary only) -----------------------------


class _Inv:
    def __init__(self, rows):
        self._rows = list(rows)

    def snapshot(self):
        return list(self._rows)


class _Handoff:
    def __init__(self, fail=None):
        self._fail = dict(fail or {})
        self.sends = []

    def send(self, request):
        self.sends.append(request)
        if request.route_target in self._fail:
            return HandoffSendOutcome(False, self._fail[request.route_target])
        return HandoffSendOutcome(True, "sent")


class _Stamp:
    def stamp(self, request):
        return StampOutcome(True, "stamped")


class _Sink:
    name = "fake"

    def __init__(self, fail_kinds=()):
        self._fk = set(fail_kinds)

    def persist(self, record):
        if record.kind in self._fk:
            return RouteRecordReceipt(persisted=False, reason=PERSIST_TRANSPORT_ERROR)
        return RouteRecordReceipt(persisted=True, reason=PERSIST_OK, location="j#0")


def _identity(route_id, *, lane, role, pane_name, last_seen, workspace=WS_CHILD):
    return RouteIdentity(
        workspace_id=workspace,
        lane_id=lane,
        role=role,
        pane_name=pane_name,
        route_id=route_id,
        observed_at="2026-06-26T00:00:00Z",
        last_seen_pane_id=last_seen,
    )


def _pane(pane_id, *, lane, role, route_label, workspace=WS_CHILD):
    return {
        "id": pane_id,
        "workspace_id": workspace,
        "lane_id": lane,
        "agent_role": role,
        "route_label": route_label,
    }


def _ledger():
    ledger = RouteIdentityLedger()
    ledger.record(_identity(RT_CHILD, lane=LANE_DELEG, role="codex",
                            pane_name="child-gw", last_seen="%10"))
    ledger.record(_identity(RT_GC, lane=LANE_GC, role="codex",
                            pane_name="gc-gw", last_seen="%20"))
    ledger.record(_identity(RT_WORKER, lane=LANE_GC, role="claude",
                            pane_name="gc-worker", last_seen="%21"))
    return ledger


def _inventory(child="%30", gc="%31", worker="%32"):
    return [
        _pane(child, lane=LANE_DELEG, role="codex", route_label="child-gw"),
        _pane(gc, lane=LANE_GC, role="codex", route_label="gc-gw"),
        _pane(worker, lane=LANE_GC, role="claude", route_label="gc-worker"),
    ]


def _route_ids():
    return {"child_gateway": RT_CHILD, "grandchild_gateway": RT_GC,
            "same_lane_worker": RT_WORKER}


def _context(**over):
    base = dict(
        source_issue=SOURCE_ISSUE,
        test_model="autonomous_parent",
        base_commit="0ae263d",
        route_ids=_route_ids(),
        callback_targets=(CallbackOutcome("delegation_parent", "r:#12556", True,
                                          CALLBACK_SENT),),
        child_issue="#12999",
        grandchild_unit=f"{WS_CHILD}/{LANE_GC}",
        grandchild_parent=f"{WS_CHILD}/{LANE_DELEG}",
    )
    base.update(over)
    return ExecutionContext(**base)


# --- oracle scenario -> real resolver/planner/executor chain -------------------


def _resolution_for(child_candidate):
    """Drive the REAL #12549 resolver for the scenario's candidate dimension."""
    if child_candidate == "missing":
        config = DelegationConfig()
    elif child_candidate == "ambiguous":
        config = DelegationConfig(
            child_candidates=(ChildCandidate("child-x"), ChildCandidate("child-x"))
        )
    else:
        config = DelegationConfig(child_candidates=(ChildCandidate("child-x"),))
    return resolve_child_candidate(config, child_project="child-x")


def _plan_for(scenario):
    """Map an oracle scenario to a real #12550 plan via the real #12549 resolver."""
    resolution = _resolution_for(scenario.child_candidate)
    request = RouteRequest(
        durable_anchor="redmine:#12560 j#0",
        child_project="child-x",
        grandchild_required=(scenario.grandchild_requirement == "required"),
        route_target_role=(
            ROUTE_CLAUDE_DIRECT
            if scenario.route_target_role == "claude_direct"
            else ROUTE_CODEX_GATEWAY
        ),
        cross_project=scenario.cross_project,
        output_mode=(
            OUTPUT_RECOMMEND_ONLY
            if scenario.resolver_output == "read_only_recommendation"
            else OUTPUT_EXECUTE
        ),
        parent_project="parent-p",
        parent_issue="#12556",
        redmine_project="giken-x",
        lane=LANE_GC,
        upstream_coordinator="coord",
        gateway_callback_target="gw-cb",
        parent_callback_target="p-cb",
        gateway_provider="codex",
        worker_provider="claude",
    )
    match = [RealizationCandidateView(True, True, True)]
    if scenario.grandchild_realization == "same_lane_fallback":
        gc_candidates, gc_can_launch, same_lane = [], False, True
    else:  # visible_lane / not_applicable
        gc_candidates, gc_can_launch, same_lane = match, True, False
    return plan_delegation_route(
        resolution,
        request,
        child_candidates=match,
        grandchild_candidates=gc_candidates,
        grandchild_can_launch=gc_can_launch,
        same_lane_worker_available=same_lane,
    )


def _execute_scenario(scenario):
    """Run the full resolver->planner->executor chain for an oracle scenario."""
    plan = _plan_for(scenario)
    handoff, sink = _Handoff(), _Sink()
    if scenario.environmental_fault == "redmine_write_error":
        sink = _Sink(fail_kinds={RECORD_CHILD_DELIVERY})
    elif scenario.environmental_fault is not None:
        handoff = _Handoff(fail={"child_gateway": scenario.environmental_fault})
    contaminated = bool(scenario.read_surface & oracle.FORBIDDEN_READ_SURFACES)
    executor = DelegationRouteExecutor(
        inventory=_Inv(_inventory()), handoff=handoff, stamp=_Stamp(),
        record_sink=sink,
    )
    return executor.execute(plan, _ledger(), _context(contaminated=contaminated))


#: Single-dimension oracle scenarios (the oracle's own test style); each maps
#: cleanly through resolver -> planner -> executor. The stale-evidence and
#: projection-chain oracle dimensions are intentionally excluded: the executor
#: *refuses* stale evidence via the ledger (a separate test below) rather than
#: "using stale evidence as proof", so they are not a classification-equality
#: relationship.
def _scenarios():
    forbidden = sorted(oracle.FORBIDDEN_READ_SURFACES)[0]
    return {
        "clean_pass": oracle.clean_autonomous_pass_scenario(),
        "missing_candidate": oracle.clean_autonomous_pass_scenario(
            child_candidate="missing"),
        "ambiguous_candidate": oracle.clean_autonomous_pass_scenario(
            child_candidate="ambiguous"),
        "claude_direct": oracle.clean_autonomous_pass_scenario(
            route_target_role="claude_direct"),
        "grandchild_same_lane_fallback": oracle.clean_autonomous_pass_scenario(
            grandchild_realization="same_lane_fallback"),
        "read_only_recommendation": oracle.clean_autonomous_pass_scenario(
            resolver_output="read_only_recommendation"),
        "contaminated_read": oracle.clean_autonomous_pass_scenario(
            read_surface=frozenset({forbidden})),
        "environmental_marker_timeout": oracle.clean_autonomous_pass_scenario(
            environmental_fault="marker_timeout"),
        "environmental_redmine_write": oracle.clean_autonomous_pass_scenario(
            environmental_fault="redmine_write_error"),
    }


class ExecutorOracleConsistencyTest(unittest.TestCase):
    """The live executor's final classification agrees with the #12547 oracle."""

    def test_executor_matches_oracle_across_scenarios(self):
        for name, scenario in _scenarios().items():
            with self.subTest(scenario=name):
                expected = oracle.classify_acceptance_run(scenario).classification
                actual = _execute_scenario(scenario).classification
                self.assertEqual(
                    actual, expected,
                    f"{name}: executor={actual!r} oracle={expected!r}",
                )

    def test_clean_scenario_is_pass_both_sides(self):
        scenario = oracle.clean_autonomous_pass_scenario()
        self.assertEqual(
            oracle.classify_acceptance_run(scenario).classification, CLASS_PASS)
        self.assertEqual(_execute_scenario(scenario).classification, CLASS_PASS)


class RealResolverChainTest(unittest.TestCase):
    """The real #12549 resolver feeds the planner and executor end-to-end."""

    def test_resolved_candidate_realizes_full_replayable_package(self):
        result = _execute_scenario(oracle.clean_autonomous_pass_scenario())
        self.assertEqual(result.classification, CLASS_PASS)
        # Full replayable record package, in canonical order, public-safe.
        self.assertEqual(result.record_kinds[0], RECORD_BASELINE)
        self.assertEqual(result.record_kinds[-1], RECORD_FINAL_CLASSIFICATION)
        for kind in (RECORD_PARENT_DECISION, RECORD_CHILD_DELIVERY,
                     RECORD_CALLBACK_OUTCOME):
            self.assertIn(kind, result.record_kinds)
        md = result.package.public_markdown()
        for pane_id in ("%30", "%31", "%32", "%10", "%20", "%21"):
            self.assertNotIn(pane_id, md)

    def test_missing_candidate_fails_closed_with_no_sends(self):
        scenario = oracle.clean_autonomous_pass_scenario(child_candidate="missing")
        plan = _plan_for(scenario)
        handoff = _Handoff()
        DelegationRouteExecutor(
            inventory=_Inv(_inventory()), handoff=handoff, stamp=_Stamp(),
            record_sink=_Sink(),
        ).execute(plan, _ledger(), _context())
        self.assertEqual(handoff.sends, [])

    def test_role_profile_chain_matches_oracle_anchor(self):
        plan = _plan_for(oracle.clean_autonomous_pass_scenario())
        self.assertEqual(plan.role_profile_chain, oracle.EXPECTED_ROLE_PROFILE_CHAIN)


class LedgerHardeningConnectedTest(unittest.TestCase):
    """#12553 live re-resolution is wired through the integrated executor chain."""

    def test_moved_pane_recovered_end_to_end(self):
        # Cached last_seen (%10) is stale; the live pane (%30) is recovered via
        # the stable identity, end-to-end from a real resolver-driven plan.
        result = _execute_scenario(oracle.clean_autonomous_pass_scenario())
        child = result.resolutions[0]
        self.assertTrue(child.is_resolved)
        self.assertTrue(child.pane_id_refreshed)
        self.assertEqual(child.resolved_pane_id, "%30")

    def test_unavailable_target_blocks_the_integrated_route(self):
        # The executor refuses stale/absent evidence (the #12553 contract) rather
        # than "using stale evidence as proof": an empty live inventory fails
        # closed to blocked, with no send.
        plan = _plan_for(oracle.clean_autonomous_pass_scenario())
        handoff = _Handoff()
        result = DelegationRouteExecutor(
            inventory=_Inv([]), handoff=handoff, stamp=_Stamp(), record_sink=_Sink(),
        ).execute(plan, _ledger(), _context())
        self.assertEqual(result.classification, "blocked")
        self.assertEqual(handoff.sends, [])


if __name__ == "__main__":
    unittest.main()
