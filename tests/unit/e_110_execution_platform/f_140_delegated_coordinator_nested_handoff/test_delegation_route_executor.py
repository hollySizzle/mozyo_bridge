"""Fake-provider classical tests for the delegated route live executor (#12559).

These are the hermetic acceptance net the #12557 live executor / #12558 record
package must pass *before* the #12546 real-machine smoke is ever run. They drive
the real executor (:class:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_executor.DelegationRouteExecutor`)
over fake tmux / handoff / stamp / Redmine providers and assert the contracts
fixed by:

- ``vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md``
  (``## Classical Test Obligations`` / ``## Redmine Record Package``)
- ``vibes/docs/specs/route-identity-ledger.md`` (``## Classical Test Expectations``)
- ``vibes/docs/specs/delegated-coordinator-decision-records.md``

Covered: command/record order, fail-closed routing for a rejected plan,
route-identity live re-resolution (stale cache recovered), direct cross-project
Claude send rejection, stale / unavailable / ambiguous evidence rejection,
callback-record completeness, "notification success alone is not evidence",
Redmine write-failure is non-PASS, and retry idempotence.

Hermetic by construction: no live tmux, no Redmine reads/writes, no private pane
ids baked into fixtures (the ``%N`` values here are synthetic inventory rows, not
operator topology), no host paths, no cockpit composition.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_project_config import (  # noqa: E402
    ChildCandidate,
    ChildCandidateResolution,
    STATUS_MISSING,
    STATUS_RESOLVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (  # noqa: E402
    OUTPUT_RECOMMEND_ONLY,
    PLAN_BLOCKED,
    PLAN_FAILED,
    PLAN_INSUFFICIENT,
    RealizationCandidateView,
    RouteRequest,
    plan_delegation_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_records import (  # noqa: E402
    CALLBACK_PENDING,
    CALLBACK_SENT,
    CLASS_BLOCKED,
    CLASS_CONTAMINATED,
    CLASS_ENVIRONMENTAL,
    CLASS_FAILED_ACCEPTANCE,
    CLASS_INSUFFICIENT,
    CLASS_PASS,
    PERSIST_OK,
    PERSIST_TRANSPORT_ERROR,
    RECORD_BASELINE,
    RECORD_CALLBACK_OUTCOME,
    RECORD_CHILD_DELIVERY,
    RECORD_CHILD_RESULT,
    RECORD_FINAL_CLASSIFICATION,
    RECORD_GRANDCHILD_REALIZATION,
    RECORD_PARENT_DECISION,
    RECORD_WORKER_EVIDENCE,
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
    RESOLVE_OK,
    ROUTE_LOCATOR_MISSING,
    RouteIdentity,
    RouteIdentityLedger,
    TARGET_AMBIGUOUS,
    TARGET_STALE,
    TARGET_UNAVAILABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (  # noqa: E402
    BACKEND_HERDR,
    herdr_route_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    encode_assigned_name,
)

# --- neutral placeholder identities (public/private boundary safe) ------------

WS_CHILD = "ws-child-project"
LANE_DELEG = "lane-delegated"
LANE_GC = "lane-grandchild"

RT_CHILD = "rt-child-gateway"
RT_GC = "rt-grandchild-gateway"
RT_WORKER = "rt-same-lane-worker"

SOURCE_ISSUE = "#12557"


# --- fake providers (the side-effecting boundary) -----------------------------


class FakeInventory:
    """A fake live pane inventory; records how many times it was re-scanned."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return list(self._rows)


class FakeHandoff:
    """Records every send; delivers unless the target is in ``fail``.

    ``fail`` maps a route target to a blocked reason (e.g. a marker timeout), so a
    test can make one hop not submit-complete. ``seen_keys`` dedupes by
    ``step_key`` so a retried run never double-counts a delivery.
    """

    def __init__(self, fail=None):
        self._fail = dict(fail or {})
        self.sends = []
        self.seen_keys = set()

    def send(self, request):
        self.sends.append(request)
        self.seen_keys.add(request.step_key)
        if request.route_target in self._fail:
            return HandoffSendOutcome(False, self._fail[request.route_target])
        return HandoffSendOutcome(True, "sent")


class FakeStamp:
    """Records every stamp; stamps unless ``ok`` is False."""

    def __init__(self, ok=True, reason="stamped"):
        self._ok = ok
        self._reason = reason
        self.stamps = []

    def stamp(self, request):
        self.stamps.append(request)
        return StampOutcome(self._ok, self._reason if not self._ok else "stamped")


class FakeSink:
    """A fake Redmine record sink.

    Persists unless ``ok`` is False (every record fails) or the record's kind is
    in ``fail_kinds`` (only those records fail) — the latter lets a test fail a
    single record, e.g. the final classification record alone.
    """

    name = "fake-redmine"

    def __init__(self, ok=True, reason=PERSIST_TRANSPORT_ERROR, fail_kinds=()):
        self._ok = ok
        self._reason = reason
        self._fail_kinds = set(fail_kinds)
        self.records = []

    def persist(self, record):
        self.records.append(record)
        if self._ok and record.kind not in self._fail_kinds:
            return RouteRecordReceipt(persisted=True, reason=PERSIST_OK, location="j#0")
        return RouteRecordReceipt(persisted=False, reason=self._reason)


# --- fixture builders ---------------------------------------------------------


def _identity(route_id, *, workspace_id, lane_id, role, pane_name, last_seen):
    return RouteIdentity(
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role,
        pane_name=pane_name,
        route_id=route_id,
        observed_at="2026-06-26T00:00:00Z",
        last_seen_pane_id=last_seen,
    )


def _pane(pane_id, *, workspace_id, lane_id, role, route_label):
    return {
        "id": pane_id,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "agent_role": role,
        "route_label": route_label,
    }


def _ledger(*, worker_workspace=WS_CHILD):
    """A ledger with child gateway, grandchild gateway, and same-lane worker.

    ``worker_workspace`` defaults to the child workspace (same-lane); a foreign
    value exercises the cross-project Claude-send guard.
    """
    ledger = RouteIdentityLedger()
    ledger.record(
        _identity(
            RT_CHILD,
            workspace_id=WS_CHILD,
            lane_id=LANE_DELEG,
            role="codex",
            pane_name="child-gw",
            last_seen="%10",
        )
    )
    ledger.record(
        _identity(
            RT_GC,
            workspace_id=WS_CHILD,
            lane_id=LANE_GC,
            role="codex",
            pane_name="gc-gw",
            last_seen="%20",
        )
    )
    ledger.record(
        _identity(
            RT_WORKER,
            workspace_id=worker_workspace,
            lane_id=LANE_GC,
            role="claude",
            pane_name="gc-worker",
            last_seen="%21",
        )
    )
    return ledger


def _inventory(*, child_pane="%30", gc_pane="%31", worker_pane="%32",
               worker_workspace=WS_CHILD):
    """Live inventory whose pane ids differ from the cached last_seen (moved panes)."""
    return [
        _pane(child_pane, workspace_id=WS_CHILD, lane_id=LANE_DELEG,
              role="codex", route_label="child-gw"),
        _pane(gc_pane, workspace_id=WS_CHILD, lane_id=LANE_GC,
              role="codex", route_label="gc-gw"),
        _pane(worker_pane, workspace_id=worker_workspace, lane_id=LANE_GC,
              role="claude", route_label="gc-worker"),
    ]


def _route_ids():
    return {
        "child_gateway": RT_CHILD,
        "grandchild_gateway": RT_GC,
        "same_lane_worker": RT_WORKER,
    }


def _callbacks(outcome=CALLBACK_SENT):
    return (
        CallbackOutcome(
            purpose="delegation_parent",
            route="redmine:#12556 j#64962",
            required=True,
            outcome=outcome,
        ),
    )


def _context(**over):
    base = dict(
        source_issue=SOURCE_ISSUE,
        test_model="autonomous_parent",
        base_commit="ddb0a29",
        route_ids=_route_ids(),
        callback_targets=_callbacks(),
        child_issue="#12999",
        grandchild_unit="ws-child-project/lane-grandchild",
        grandchild_parent="ws-child-project/lane-delegated",
    )
    base.update(over)
    return ExecutionContext(**base)


def _grandchild_plan(grandchild_required=True, output_mode=None, status=STATUS_RESOLVED,
                     grandchild_can_launch=True, same_lane_worker_available=False):
    res = ChildCandidateResolution(
        status=status,
        diagnostic="ok",
        requested_child_project="child-x",
        requested_capability=None,
        candidate=ChildCandidate(child_project="child-x") if status == STATUS_RESOLVED else None,
    )
    kwargs = dict(
        durable_anchor="redmine:#12557 j#0",
        child_project="child-x",
        grandchild_required=grandchild_required,
        parent_project="parent-p",
        parent_issue="#12556",
        redmine_project="giken-x",
        lane=LANE_GC,
        upstream_coordinator="coord",
        gateway_callback_target="gw-cb",
        parent_callback_target="p-cb",
    )
    if output_mode is not None:
        kwargs["output_mode"] = output_mode
    req = RouteRequest(**kwargs)
    match = [RealizationCandidateView(True, True, True)]
    return plan_delegation_route(
        res,
        req,
        child_candidates=match,
        grandchild_candidates=([] if same_lane_worker_available else match),
        grandchild_can_launch=grandchild_can_launch,
        same_lane_worker_available=same_lane_worker_available,
    )


def _executor(inventory=None, handoff=None, stamp=None, sink=None):
    return DelegationRouteExecutor(
        inventory=inventory or FakeInventory(_inventory()),
        handoff=handoff or FakeHandoff(),
        stamp=stamp or FakeStamp(),
        record_sink=sink or FakeSink(),
    )


# --- tests --------------------------------------------------------------------


class GrandchildRouteHappyPathTest(unittest.TestCase):
    """A clean executable grandchild route -> ordered records + PASS."""

    def setUp(self):
        self.handoff = FakeHandoff()
        self.stamp = FakeStamp()
        self.sink = FakeSink()
        self.inv = FakeInventory(_inventory())
        self.executor = _executor(self.inv, self.handoff, self.stamp, self.sink)
        self.result = self.executor.execute(_grandchild_plan(), _ledger(), _context())

    def test_classifies_pass(self):
        self.assertEqual(self.result.classification, CLASS_PASS)
        self.assertTrue(self.result.is_pass)

    def test_record_package_in_canonical_order(self):
        self.assertEqual(
            self.result.record_kinds,
            (
                RECORD_BASELINE,
                RECORD_PARENT_DECISION,
                RECORD_CHILD_DELIVERY,
                RECORD_CHILD_RESULT,
                RECORD_GRANDCHILD_REALIZATION,
                RECORD_WORKER_EVIDENCE,
                RECORD_CALLBACK_OUTCOME,
                RECORD_FINAL_CLASSIFICATION,
            ),
        )

    def test_sends_in_route_order(self):
        self.assertEqual(
            [s.route_target for s in self.result.sends],
            ["child_gateway", "grandchild_gateway", "same_lane_worker"],
        )

    def test_worker_send_is_the_only_claude_target(self):
        claude = [s for s in self.result.sends if s.role == "claude"]
        self.assertEqual(len(claude), 1)
        self.assertEqual(claude[0].route_target, "same_lane_worker")

    def test_grandchild_stamped_once_at_depth_two(self):
        self.assertEqual(len(self.result.stamps), 1)
        self.assertEqual(self.result.stamps[0].depth, 2)

    def test_inventory_re_scanned_per_hop(self):
        # One snapshot per re-resolution: child + grandchild(stamp) + grandchild
        # (gateway send) + worker = 4.
        self.assertEqual(self.inv.calls, 4)

    def test_public_markdown_has_no_private_pane_id(self):
        md = self.result.package.public_markdown()
        for pane_id in ("%30", "%31", "%32", "%10", "%20", "%21"):
            self.assertNotIn(pane_id, md)


class RouteIdentityReResolutionTest(unittest.TestCase):
    """A stale cached pane id is transparently recovered via stable identity."""

    def test_moved_pane_recovered_and_send_uses_live_id(self):
        result = _executor().execute(_grandchild_plan(), _ledger(), _context())
        child = result.resolutions[0]
        self.assertTrue(child.is_resolved)
        self.assertTrue(child.pane_id_refreshed)  # %10 cached -> %30 live
        self.assertEqual(child.resolved_pane_id, "%30")
        self.assertEqual(result.sends[0].pane_id, "%30")
        self.assertEqual(result.classification, CLASS_PASS)


class FailClosedPlanTest(unittest.TestCase):
    """A non-pass-eligible plan never mutates live state."""

    def _run(self, plan):
        handoff, stamp = FakeHandoff(), FakeStamp()
        result = _executor(handoff=handoff, stamp=stamp).execute(
            plan, _ledger(), _context()
        )
        return result, handoff, stamp

    def test_failed_acceptance_plan_no_side_effects(self):
        plan = _grandchild_plan(status=STATUS_MISSING)
        self.assertEqual(plan.disposition, PLAN_FAILED)
        result, handoff, stamp = self._run(plan)
        self.assertEqual(result.classification, CLASS_FAILED_ACCEPTANCE)
        self.assertEqual(handoff.sends, [])
        self.assertEqual(stamp.stamps, [])
        self.assertEqual(
            result.record_kinds, (RECORD_BASELINE, RECORD_FINAL_CLASSIFICATION)
        )

    def test_blocked_plan_maps_to_blocked(self):
        plan = _grandchild_plan(grandchild_can_launch=False,
                                same_lane_worker_available=True)
        self.assertEqual(plan.disposition, PLAN_BLOCKED)
        result, handoff, _ = self._run(plan)
        self.assertEqual(result.classification, CLASS_BLOCKED)
        self.assertEqual(handoff.sends, [])

    def test_insufficient_plan_maps_to_insufficient(self):
        plan = _grandchild_plan(output_mode=OUTPUT_RECOMMEND_ONLY)
        self.assertEqual(plan.disposition, PLAN_INSUFFICIENT)
        result, handoff, _ = self._run(plan)
        self.assertEqual(result.classification, CLASS_INSUFFICIENT)
        self.assertEqual(handoff.sends, [])


class DirectClaudeSendRejectionTest(unittest.TestCase):
    """A same-lane worker in a foreign workspace is a cross-project Claude send."""

    def test_cross_project_worker_fails_closed_no_send(self):
        ledger = _ledger(worker_workspace="ws-foreign")
        inv = FakeInventory(_inventory(worker_workspace="ws-foreign"))
        handoff = FakeHandoff()
        result = _executor(inv, handoff).execute(_grandchild_plan(), ledger, _context())
        self.assertEqual(result.classification, CLASS_FAILED_ACCEPTANCE)
        # The child + grandchild-gateway sends happen; the Claude worker send does not.
        self.assertNotIn("same_lane_worker", [s.route_target for s in handoff.sends])


class StaleEvidenceRejectionTest(unittest.TestCase):
    """Stale / unavailable / ambiguous live evidence fails closed (no send)."""

    def test_target_unavailable_blocks_route(self):
        inv = FakeInventory([])  # nothing live
        handoff = FakeHandoff()
        result = _executor(inv, handoff).execute(_grandchild_plan(), _ledger(), _context())
        self.assertEqual(result.classification, CLASS_BLOCKED)
        self.assertEqual(result.resolutions[0].status, TARGET_UNAVAILABLE)
        self.assertEqual(handoff.sends, [])

    def test_stale_cached_pane_id_blocks_route(self):
        # The child lane/role slot is occupied by a different labeled identity
        # whose pane id equals the cached last_seen (%10): trusting it would
        # mis-route.
        rows = _inventory()
        rows[0] = _pane("%10", workspace_id=WS_CHILD, lane_id=LANE_DELEG,
                        role="codex", route_label="someone-else")
        result = _executor(FakeInventory(rows)).execute(
            _grandchild_plan(), _ledger(), _context()
        )
        self.assertEqual(result.resolutions[0].status, TARGET_STALE)
        self.assertEqual(result.classification, CLASS_BLOCKED)

    def test_ambiguous_live_match_blocks_route(self):
        rows = _inventory()
        rows.append(_pane("%99", workspace_id=WS_CHILD, lane_id=LANE_DELEG,
                          role="codex", route_label="child-gw"))
        result = _executor(FakeInventory(rows)).execute(
            _grandchild_plan(), _ledger(), _context()
        )
        self.assertEqual(result.resolutions[0].status, TARGET_AMBIGUOUS)
        self.assertEqual(result.classification, CLASS_BLOCKED)


class EnvironmentalAndEvidenceTest(unittest.TestCase):
    """Notification-only success and write failures are never PASS."""

    def test_marker_timeout_send_is_environmental(self):
        handoff = FakeHandoff(fail={"child_gateway": "marker_timeout"})
        result = _executor(handoff=handoff).execute(
            _grandchild_plan(), _ledger(), _context()
        )
        self.assertEqual(result.classification, CLASS_ENVIRONMENTAL)

    def test_redmine_write_failure_is_non_pass(self):
        result = _executor(sink=FakeSink(ok=False)).execute(
            _grandchild_plan(), _ledger(), _context()
        )
        self.assertTrue(result.write_failed)
        self.assertEqual(result.classification, CLASS_ENVIRONMENTAL)
        self.assertNotEqual(result.classification, CLASS_PASS)

    def test_final_record_write_failure_is_not_pass(self):
        # Regression for #12556 j#64989: only the final_classification record's
        # write fails. Every prior record persisted (so the candidate verdict was
        # PASS), but a verdict that cannot be durably written is not a PASS.
        sink = FakeSink(fail_kinds={RECORD_FINAL_CLASSIFICATION})
        result = _executor(sink=sink).execute(
            _grandchild_plan(), _ledger(), _context()
        )
        self.assertTrue(result.write_failed)
        self.assertNotEqual(result.classification, CLASS_PASS)
        self.assertEqual(result.classification, CLASS_ENVIRONMENTAL)
        # The invariant under audit: is_pass must never coexist with write_failed.
        self.assertFalse(result.is_pass and result.write_failed)
        # Package's final record matches the downgraded verdict (no PASS claim).
        final = result.package.records()[-1]
        self.assertEqual(final.kind, RECORD_FINAL_CLASSIFICATION)
        self.assertIn(("classification", CLASS_ENVIRONMENTAL), final.fields)

    def test_pending_required_callback_is_insufficient(self):
        # Every hop delivered, but the required callback was never recorded:
        # notification success alone is not evidence.
        result = _executor().execute(
            _grandchild_plan(),
            _ledger(),
            _context(callback_targets=_callbacks(outcome=CALLBACK_PENDING)),
        )
        self.assertEqual(result.classification, CLASS_INSUFFICIENT)

    def test_contaminated_read_overrides_to_contaminated(self):
        result = _executor().execute(
            _grandchild_plan(), _ledger(), _context(contaminated=True)
        )
        self.assertEqual(result.classification, CLASS_CONTAMINATED)


class NoGrandchildRouteTest(unittest.TestCase):
    """A route without a grandchild lane realizes child delivery + callback only."""

    def test_child_only_route_passes_without_grandchild_records(self):
        plan = _grandchild_plan(grandchild_required=False)
        result = _executor().execute(plan, _ledger(), _context())
        self.assertEqual(result.classification, CLASS_PASS)
        self.assertNotIn(RECORD_GRANDCHILD_REALIZATION, result.record_kinds)
        self.assertNotIn(RECORD_WORKER_EVIDENCE, result.record_kinds)
        self.assertEqual([s.route_target for s in result.sends], ["child_gateway"])


class RetryIdempotenceTest(unittest.TestCase):
    """Re-executing the same plan is deterministic and does not double-send."""

    def test_two_runs_identical_and_deduped(self):
        handoff = FakeHandoff()
        executor = _executor(handoff=handoff)
        plan, ledger, ctx = _grandchild_plan(), _ledger(), _context()

        r1 = executor.execute(plan, ledger, ctx)
        keys_after_first = set(handoff.seen_keys)
        r2 = executor.execute(plan, ledger, ctx)

        self.assertEqual(r1.record_kinds, r2.record_kinds)
        self.assertEqual(r1.classification, r2.classification, CLASS_PASS)
        # Deterministic step keys: the retry re-uses the same keys, so a deduping
        # transport delivers each hop exactly once across retries.
        self.assertEqual(keys_after_first, handoff.seen_keys)
        self.assertEqual(len(handoff.seen_keys), 3)
        self.assertEqual(
            [s.step_key for s in r1.sends], [s.step_key for s in r2.sends]
        )


# --- herdr backend live re-resolution (#13302) --------------------------------


def _herdr_ledger():
    """A ledger of herdr route identities (canonical assigned-name stable labels).

    Mirrors ``_ledger()`` but each identity's ``pane_name`` is the deterministic
    canonical assigned name for its slot (via ``herdr_route_identity``), so it
    matches the decoded ``agent list`` inventory row for that slot.
    """
    ledger = RouteIdentityLedger()
    ledger.record(
        herdr_route_identity(
            workspace_id=WS_CHILD, role="codex", lane_id=LANE_DELEG,
            route_id=RT_CHILD, last_seen_locator="w0:c0",
        )
    )
    ledger.record(
        herdr_route_identity(
            workspace_id=WS_CHILD, role="codex", lane_id=LANE_GC,
            route_id=RT_GC, last_seen_locator="w0:g0",
        )
    )
    ledger.record(
        herdr_route_identity(
            workspace_id=WS_CHILD, role="claude", lane_id=LANE_GC,
            route_id=RT_WORKER, last_seen_locator="w0:k0",
        )
    )
    return ledger


def _herdr_row(*, workspace_id, lane_id, role, locator):
    """A live herdr ``agent list`` row; ``locator=None`` emits a locator-less row."""
    row = {"name": encode_assigned_name(workspace_id, role, lane_id)}
    if locator is not None:
        row["pane_id"] = locator
    return row


def _herdr_inventory(*, worker_locator="w1:k9"):
    """Live herdr inventory covering the child / grandchild / worker slots."""
    return [
        _herdr_row(workspace_id=WS_CHILD, lane_id=LANE_DELEG, role="codex",
                   locator="w1:c9"),
        _herdr_row(workspace_id=WS_CHILD, lane_id=LANE_GC, role="codex",
                   locator="w1:g9"),
        _herdr_row(workspace_id=WS_CHILD, lane_id=LANE_GC, role="claude",
                   locator=worker_locator),
    ]


class HerdrBackendExecutorTest(unittest.TestCase):
    """Executor re-resolution against a live herdr ``agent list`` inventory."""

    def test_herdr_route_resolves_and_passes(self):
        handoff = FakeHandoff()
        inv = FakeInventory(_herdr_inventory())
        executor = _executor(inv, handoff, FakeStamp(), FakeSink())
        result = executor.execute(
            _grandchild_plan(), _herdr_ledger(), _context(backend=BACKEND_HERDR)
        )
        # Every hop resolved cleanly against the herdr inventory -> PASS.
        self.assertEqual(result.classification, CLASS_PASS)
        self.assertTrue(all(r.status == RESOLVE_OK for r in result.resolutions))
        # Sends address the live herdr locators, not the cached last_seen values.
        self.assertEqual(
            [s.route_target for s in result.sends],
            ["child_gateway", "grandchild_gateway", "same_lane_worker"],
        )
        self.assertEqual(
            {s.pane_id for s in result.sends}, {"w1:c9", "w1:g9", "w1:k9"}
        )

    def test_herdr_locator_missing_worker_fails_closed(self):
        # The worker agent is live in its slot but its agent-list row carries no
        # usable locator -> route_locator_missing (herdr-only), fail closed, no
        # blank-target send. The fail-closed vocabulary is projected unchanged.
        handoff = FakeHandoff()
        inv = FakeInventory(_herdr_inventory(worker_locator=None))
        executor = _executor(inv, handoff, FakeStamp(), FakeSink())
        result = executor.execute(
            _grandchild_plan(), _herdr_ledger(), _context(backend=BACKEND_HERDR)
        )
        self.assertEqual(result.classification, CLASS_BLOCKED)
        worker_res = result.resolutions[-1]
        self.assertEqual(worker_res.status, ROUTE_LOCATOR_MISSING)
        self.assertTrue(worker_res.is_fail_closed)
        self.assertNotIn("same_lane_worker", [s.route_target for s in result.sends])

    def test_herdr_ambiguous_slot_fails_closed(self):
        # Two live agents decode to the child-gateway slot -> target_ambiguous;
        # no send, blocked, and no downstream hops.
        inv_rows = _herdr_inventory()
        inv_rows.append(
            _herdr_row(workspace_id=WS_CHILD, lane_id=LANE_DELEG, role="codex",
                       locator="w2:c2")
        )
        handoff = FakeHandoff()
        executor = _executor(FakeInventory(inv_rows), handoff, FakeStamp(), FakeSink())
        result = executor.execute(
            _grandchild_plan(), _herdr_ledger(), _context(backend=BACKEND_HERDR)
        )
        self.assertEqual(result.classification, CLASS_BLOCKED)
        self.assertEqual(result.resolutions[0].status, TARGET_AMBIGUOUS)
        self.assertEqual(result.sends, ())

    def test_herdr_unavailable_slot_fails_closed(self):
        # No live claude agent for the worker slot -> target_unavailable.
        rows = [
            _herdr_row(workspace_id=WS_CHILD, lane_id=LANE_DELEG, role="codex",
                       locator="w1:c9"),
            _herdr_row(workspace_id=WS_CHILD, lane_id=LANE_GC, role="codex",
                       locator="w1:g9"),
        ]
        handoff = FakeHandoff()
        executor = _executor(FakeInventory(rows), handoff, FakeStamp(), FakeSink())
        result = executor.execute(
            _grandchild_plan(), _herdr_ledger(), _context(backend=BACKEND_HERDR)
        )
        self.assertEqual(result.classification, CLASS_BLOCKED)
        self.assertEqual(result.resolutions[-1].status, TARGET_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
