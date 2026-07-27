"""External-client coordinator-proxy choreography tests (Redmine #14546).

Pins the whole delegation path with a **counting** send port, so every claim is about the number of
sends that actually happened rather than about the shape of a returned object: the positive path
sends exactly once, every negative path sends zero, and a repeat of the same durable decision sends
zero even after the first one completed.

The two properties that carry the security argument are asserted directly:

- **caller-supplied ``MOZYO_*`` is never authority.** The external client is, by definition, not
  attested. If the resolution consulted its env, the rail would let a caller *declare* itself into a
  workspace and delegate into it — which is the forgery the observed ``missing_identity`` /
  ``herdr_sender_identity_unresolved`` gates exist to prevent. The test sets hostile values for
  every such key and asserts the resolution is byte-identical;
- **cross-workspace is structurally impossible, not merely discouraged.** The target is selected by
  decoding the agent's own mzb1 assigned name, so a foreign-workspace row cannot be chosen even when
  it is the only live coordinator in the inventory.

Review j#89878 added three more, each pinned here because each was a way the rail could *look*
verified while verifying nothing:

- the **(action, journal) pair** is the unit of authority. A real journal on the right issue that
  carries a different decision token does not authorize this action;
- a decoded assigned name is **not** an attestation. The single live candidate must also join its
  generation-bound startup self-attestation record;
- a send that **fired** is not a send that **landed**. A non-delivering send is reported as a
  non-delivery, because the caller branches on the exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.coordinator_proxy_fence import (  # noqa: E501
    RESERVE_NEEDS_RECONCILE,
    CoordinatorProxyFence,
    ProxyRouteKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
    CALLER_ENV_KEYS_NEVER_AUTHORITY,
    SEND_DELIVERED,
    SEND_FAILED,
    ProxySendOutcome,
    decision_journals_from_entries,
    execute_proxy_delegation,
    resolve_proxy_context,
    resolve_proxy_target,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    ACTION_DISPATCH_NEXT,
    ANCHOR_ACTION_MISMATCH,
    ANCHOR_DECISION_INCOMPLETE,
    ANCHOR_GENERATION_STALE,
    ANCHOR_LANE_UNRESOLVED,
    ANCHOR_SUPERSEDED,
    ANCHOR_UNVERIFIED,
    ANCHOR_VERIFIED,
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    REASON_ANCHOR_ACTION_MISMATCH,
    REASON_ANCHOR_DECISION_INCOMPLETE,
    REASON_ANCHOR_GENERATION_STALE,
    REASON_ANCHOR_LANE_UNRESOLVED,
    REASON_ANCHOR_SUPERSEDED,
    REASON_ANCHOR_UNVERIFIED,
    REASON_AUTHORITY_MISSING,
    REASON_DELIVERY_UNCERTAIN,
    REASON_DUPLICATE,
    REASON_FENCE_UNAVAILABLE,
    REASON_TARGET_AMBIGUOUS,
    REASON_TARGET_MISSING,
    REASON_TARGET_UNATTESTED,
    REASON_WORKSPACE_UNRESOLVED,
    TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING,
    TARGET_MISSING,
    TARGET_OK,
    TARGET_UNATTESTED,
    WORKSPACE_UNRESOLVED,
    ZERO_SEND,
    DecisionRecord,
    LaneExpectation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    SCHEMA_NAME,
    SCHEMA_VERSION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E501
    ROLE_COORDINATOR,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    encode_assigned_name,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
FOREIGN_WS = "ffffffffffffffffffffffffffffffff"
SCOPE = "bare_mozyo_workspace"
ISSUE = "14546"
#: The action's own decision series (`implementation_request`), oldest first.
OLDER_JOURNAL = "89688"
CURRENT_JOURNAL = "89736"
#: A real journal on the same issue carrying a DIFFERENT decision token.
OTHER_KIND_JOURNAL = "89873"

LANE = "issue_14546_default_coordinator_authority"


def _expectation(generation=2, decision_journal="89736"):
    """The live lifecycle facts the classifier matches against (injected in these tests)."""

    def _resolve(_repo_root, lane):
        if lane != LANE:
            return None
        return LaneExpectation(
            lane=LANE, generation=generation, decision_journal=decision_journal
        )

    return _resolve

#: Canonical dispatch decisions carry lane + generation; the other-kind gate does not.
DECISIONS = (
    DecisionRecord(OLDER_JOURNAL, "implementation_request", LANE, "1"),
    DecisionRecord(CURRENT_JOURNAL, "implementation_request", LANE, "2"),
    DecisionRecord(OTHER_KIND_JOURNAL, "implementation_done"),
)


def _attested(_name, *, locator, workspace_id, provider):
    """A passing attestation join (the store is exercised separately)."""
    return True, "ok", "startup self-attestation present and generation-matched"


def _unattested(state="stale"):
    def _join(_name, *, locator, workspace_id, provider):
        return False, state, f"attestation {state}"

    return _join


def _row(workspace_id: str, provider: str, lane: str = "", locator: str = "w3:p1") -> dict:
    row = {AGENT_KEY_NAME: encode_assigned_name(workspace_id, provider, lane)}
    if locator:
        row[AGENT_KEY_LOCATOR] = locator
    return row


class CountingSendPort:
    """Counts sends so every assertion is about what actually happened."""

    def __init__(self, result: str = SEND_DELIVERED) -> None:
        self.calls: list = []
        self._result = result

    def send(self, context, action_id, *, args):
        self.calls.append((context.issue, context.journal, action_id))
        return ProxySendOutcome(result=self._result, rc=0 if self._result == SEND_DELIVERED else 1)


class ProxySendTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        self.fence = CoordinatorProxyFence(Path(self._tmp.name) / "proxy.sqlite")
        self.declare_coordinator()

    def declare_coordinator(self, role: str = ROLE_COORDINATOR, scope: str = SCOPE) -> None:
        (self.repo / ".mozyo-bridge" / "workflow-role-bindings.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_NAME,
                    "version": SCHEMA_VERSION,
                    "bindings": [{"role": role, "project_scope": scope}],
                }
            ),
            encoding="utf-8",
        )

    def remove_declaration(self) -> None:
        (self.repo / ".mozyo-bridge" / "workflow-role-bindings.json").unlink()

    def _context(
        self,
        *,
        rows=None,
        decisions=None,
        journal=CURRENT_JOURNAL,
        issue=ISSUE,
        workspace=WS,
        env=None,
        action=ACTION_DISPATCH_NEXT,
        attestation=None,
        expectation=None,
    ):
        rows = [_row(WS, "codex")] if rows is None else rows
        decisions = DECISIONS if decisions is None else tuple(decisions)
        return resolve_proxy_context(
            argparse.Namespace(repo=str(self.repo)),
            action=action,
            issue=issue,
            journal=journal,
            repo_root=self.repo,
            env=env if env is not None else {},
            rows_provider=lambda _env: rows,
            decision_journals_provider=lambda _issue: decisions,
            workspace_provider=lambda _root: workspace,
            attestation_join=attestation or _attested,
            lane_expectation_provider=expectation or _expectation(),
        )

    def _execute(self, context, *, port=None, fence=None, bootstrap=True):
        fence = fence or self.fence
        if bootstrap and not fence.is_bootstrapped():
            fence.bootstrap()
        port = port or CountingSendPort()
        result = execute_proxy_delegation(
            context,
            args=argparse.Namespace(repo=str(self.repo), action=ACTION_DISPATCH_NEXT),
            action=ACTION_DISPATCH_NEXT,
            fence=fence,
            send_port=port,
        )
        return result, port


class ResolutionTest(ProxySendTestBase):
    def test_a_declared_workspace_resolves_every_link(self):
        context = self._context()
        self.assertEqual(context.workspace_id, WS)
        self.assertEqual(context.role, ROLE_COORDINATOR)
        self.assertEqual(context.project_scope, SCOPE)
        self.assertEqual(context.provider, "codex")
        self.assertEqual(context.links.authority, AUTHORITY_RESOLVED)
        self.assertEqual(context.links.target, TARGET_OK)
        self.assertEqual(context.links.anchor, ANCHOR_VERIFIED)

    def test_an_undeclared_workspace_has_no_authority(self):
        self.remove_declaration()
        context = self._context()
        self.assertEqual(context.links.authority, AUTHORITY_MISSING)
        self.assertEqual(context.provider, "")

    def test_an_older_lane_generation_is_not_verified(self):
        context = self._context(journal=OLDER_JOURNAL)
        self.assertEqual(context.links.anchor, ANCHOR_GENERATION_STALE)

    def test_a_lane_with_no_live_lifecycle_facts_is_not_verified(self):
        context = self._context(expectation=lambda _root, _lane: None)
        self.assertEqual(context.links.anchor, ANCHOR_LANE_UNRESOLVED)

    def test_a_real_lane_advance_stales_the_decision(self):
        context = self._context(expectation=_expectation(generation=3, decision_journal="90500"))
        self.assertEqual(context.links.anchor, ANCHOR_GENERATION_STALE)

    def test_a_decision_without_lane_or_generation_is_not_verified(self):
        context = self._context(
            decisions=(DecisionRecord(CURRENT_JOURNAL, "implementation_request"),)
        )
        self.assertEqual(context.links.anchor, ANCHOR_DECISION_INCOMPLETE)

    def test_an_unreachable_redmine_is_not_verified(self):
        context = self._context(decisions=())
        self.assertEqual(context.links.anchor, ANCHOR_UNVERIFIED)

    def test_a_journal_carrying_another_decision_does_not_authorize_this_action(self):
        context = self._context(journal=OTHER_KIND_JOURNAL)
        self.assertEqual(context.links.anchor, ANCHOR_ACTION_MISMATCH)

    def test_an_unattested_live_slot_is_not_a_target(self):
        context = self._context(attestation=_unattested("stale"))
        self.assertEqual(context.links.target, TARGET_UNATTESTED)
        self.assertEqual(context.target.attestation_state, "stale")


class CallerEnvIsNeverAuthorityTest(ProxySendTestBase):
    def test_hostile_caller_env_does_not_change_the_resolution(self):
        clean = self._context(env={})
        hostile_env = {key: FOREIGN_WS for key in CALLER_ENV_KEYS_NEVER_AUTHORITY}
        hostile_env["MOZYO_AGENT_ROLE"] = "codex"
        hostile_env["MOZYO_LANE_ID"] = "default"
        hostile = self._context(env=hostile_env)

        self.assertEqual(hostile.workspace_id, clean.workspace_id)
        self.assertEqual(hostile.role, clean.role)
        self.assertEqual(hostile.provider, clean.provider)
        self.assertEqual(hostile.links, clean.links)

    def test_an_unresolvable_workspace_never_falls_back_to_caller_env(self):
        # The decisive case. When the repo checkout resolves NO anchor, an implementation that
        # "helpfully" falls back to the caller's env would resolve a workspace here — which is
        # precisely the forgery this rail must not permit. Comparing two resolutions that both
        # already had a repo-derived anchor cannot detect that fallback, so it is asserted on the
        # only input shape where a fallback would be observable.
        context = self._context(
            workspace="",
            env={key: FOREIGN_WS for key in CALLER_ENV_KEYS_NEVER_AUTHORITY},
        )
        self.assertEqual(context.workspace_id, "")
        self.assertEqual(context.links.workspace, WORKSPACE_UNRESOLVED)

        result, port = self._execute(context)
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, REASON_WORKSPACE_UNRESOLVED)
        self.assertEqual(port.calls, [])

    def test_a_caller_cannot_env_its_way_into_a_foreign_workspace(self):
        # Only a foreign-workspace agent is live. Even with the caller asserting that workspace in
        # its env, the target resolution (which decodes the agent's OWN attestation) finds none.
        context = self._context(
            rows=[_row(FOREIGN_WS, "codex")],
            env={key: FOREIGN_WS for key in CALLER_ENV_KEYS_NEVER_AUTHORITY},
        )
        self.assertEqual(context.links.target, TARGET_MISSING)
        result, port = self._execute(context)
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, REASON_TARGET_MISSING)
        self.assertEqual(port.calls, [])


class TargetResolutionTest(unittest.TestCase):
    def test_exactly_one_same_workspace_default_lane_agent_resolves(self):
        target = resolve_proxy_target([_row(WS, "codex")], workspace_id=WS, provider="codex", attestation_join=_attested)
        self.assertEqual(target.status, TARGET_OK)
        self.assertEqual(target.locator, "w3:p1")

    def test_a_foreign_workspace_row_is_never_selected(self):
        target = resolve_proxy_target(
            [_row(FOREIGN_WS, "codex")], workspace_id=WS, provider="codex",
            attestation_join=_attested,
        )
        self.assertEqual(target.status, TARGET_MISSING)

    def test_a_non_default_lane_row_is_never_selected(self):
        target = resolve_proxy_target(
            [_row(WS, "codex", lane="issue_14546")], workspace_id=WS, provider="codex",
            attestation_join=_attested,
        )
        self.assertEqual(target.status, TARGET_MISSING)

    def test_a_different_provider_row_is_never_selected(self):
        target = resolve_proxy_target(
            [_row(WS, "claude")], workspace_id=WS, provider="codex", attestation_join=_attested
        )
        self.assertEqual(target.status, TARGET_MISSING)

    def test_duplicate_default_lane_agents_are_ambiguous(self):
        target = resolve_proxy_target(
            [_row(WS, "codex", locator="w3:p1"), _row(WS, "codex", locator="w4:p1")],
            workspace_id=WS,
            provider="codex",
            attestation_join=_attested,
        )
        self.assertEqual(target.status, TARGET_AMBIGUOUS)
        self.assertEqual(target.live, 2)

    def test_a_single_agent_without_a_locator_is_unaddressable(self):
        target = resolve_proxy_target(
            [_row(WS, "codex", locator="")], workspace_id=WS, provider="codex",
            attestation_join=_attested,
        )
        self.assertEqual(target.status, TARGET_LOCATOR_MISSING)

    def test_undecodable_rows_are_ignored_not_guessed_at(self):
        target = resolve_proxy_target(
            [{"name": "some-hand-named-pane", "pane_id": "w9:p9"}, _row(WS, "codex")],
            workspace_id=WS,
            provider="codex",
            attestation_join=_attested,
        )
        self.assertEqual(target.status, TARGET_OK)
        self.assertEqual(target.locator, "w3:p1")


class SendCountTest(ProxySendTestBase):
    def test_the_positive_path_sends_exactly_once(self):
        result, port = self._execute(self._context())
        self.assertTrue(result.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(port.calls[0][:2], (ISSUE, CURRENT_JOURNAL))
        self.assertTrue(result.action_id.startswith("pxy_"))

    def test_a_repeat_of_the_same_decision_sends_zero(self):
        context = self._context()
        first, port = self._execute(context)
        self.assertTrue(first.sent)
        route = ProxyRouteKey(
            workspace_id=WS, lane_id="default", role=ROLE_COORDINATOR, action=ACTION_DISPATCH_NEXT
        )
        self.fence.complete(route, first.action_id)

        second, port2 = self._execute(self._context())
        self.assertFalse(second.sent)
        self.assertEqual(second.reason, REASON_DUPLICATE)
        self.assertEqual(port2.calls, [])

    def test_every_negative_path_sends_zero(self):
        cases = [
            ("no authority", lambda: (self.remove_declaration(), self._context())[1],
             REASON_AUTHORITY_MISSING),
            ("no target", lambda: self._context(rows=[]), REASON_TARGET_MISSING),
            ("ambiguous target",
             lambda: self._context(rows=[_row(WS, "codex", locator="a"),
                                         _row(WS, "codex", locator="b")]),
             REASON_TARGET_AMBIGUOUS),
            ("unverified anchor", lambda: self._context(decisions=()),
             REASON_ANCHOR_UNVERIFIED),
            ("decision without a lane/generation",
             lambda: self._context(
                 decisions=(DecisionRecord(CURRENT_JOURNAL, "implementation_request"),)),
             REASON_ANCHOR_DECISION_INCOMPLETE),
            ("stale lane generation", lambda: self._context(journal=OLDER_JOURNAL),
             REASON_ANCHOR_GENERATION_STALE),
            ("lane with no live facts",
             lambda: self._context(expectation=lambda _r, _l: None),
             REASON_ANCHOR_LANE_UNRESOLVED),
            ("action mismatch", lambda: self._context(journal=OTHER_KIND_JOURNAL),
             REASON_ANCHOR_ACTION_MISMATCH),
            ("unattested target", lambda: self._context(attestation=_unattested("absent")),
             REASON_TARGET_UNATTESTED),
        ]
        for label, build, reason in cases:
            with self.subTest(label):
                self.setUp()  # a clean repo + fence per case
                context = build()
                result, port = self._execute(context)
                self.assertFalse(result.sent, label)
                self.assertEqual(result.decision, ZERO_SEND, label)
                self.assertEqual(result.reason, reason, label)
                self.assertEqual(port.calls, [], label)

    def test_an_unbootstrapped_store_sends_zero_and_is_never_auto_created(self):
        context = self._context()
        result, port = self._execute(context, bootstrap=False)
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, REASON_FENCE_UNAVAILABLE)
        self.assertEqual(port.calls, [])
        self.assertFalse(self.fence.is_bootstrapped())
        self.assertFalse(self.fence.path.exists())

    def test_a_refusal_before_the_fence_consumes_no_generation(self):
        # A doomed delegation must not burn the route's generation: after fixing the target, the
        # SAME decision must still be deliverable.
        self.fence.bootstrap()
        doomed, port = self._execute(self._context(rows=[]))
        self.assertFalse(doomed.sent)
        self.assertEqual(port.calls, [])

        fixed, port2 = self._execute(self._context())
        self.assertTrue(fixed.sent)
        self.assertEqual(len(port2.calls), 1)

    def test_a_failed_send_is_not_reported_as_a_delivery(self):
        # Review j#89878 finding 3: the caller has no runtime and branches on the exit code, so a
        # send that fired but did not land must never surface as a delivered delegation.
        context = self._context()
        result, port = self._execute(context, port=CountingSendPort(result=SEND_FAILED))
        self.assertEqual(len(port.calls), 1)  # it DID fire exactly once
        self.assertFalse(result.sent)
        self.assertEqual(result.decision, ZERO_SEND)
        self.assertEqual(result.reason, REASON_DELIVERY_UNCERTAIN)
        route = ProxyRouteKey(
            workspace_id=WS, lane_id="default", role=ROLE_COORDINATOR, action=ACTION_DISPATCH_NEXT
        )
        self.assertEqual(self.fence.active(route).state, "uncertain")

        retry, port2 = self._execute(self._context())
        self.assertFalse(retry.sent)
        self.assertEqual(retry.reason, REASON_DUPLICATE)
        self.assertEqual(port2.calls, [])


class AcknowledgementLifecycleTest(ProxySendTestBase):
    """The completion half of exactly-once (review j#89918 finding 1).

    The first draft delivered, marked `delivered`, and stopped. Nothing in the product ever
    completed the generation, so the route stayed held forever and every later decision — including
    a genuinely newer one — was refused as a duplicate. A rail that works exactly once and then
    wedges is not a repeatable single-step entrypoint, and the tests hid it by driving `complete()`
    directly. These exercise the acknowledgement the way the product does.
    """

    NEWER_JOURNAL = "90100"
    NEWER_DECISIONS = DECISIONS + (
        DecisionRecord("90100", "implementation_request", LANE, "3"),
    )

    def _ack(self, action_id):
        from mozyo_bridge.core.state.coordinator_proxy_fence import CoordinatorProxyFence

        # The production surface resolves the store from the home; drive the same method the
        # `workflow proxy-ack` command calls, on this test's store.
        return self.fence.complete_by_action_id(action_id, workspace_id=WS)

    def test_without_an_ack_a_newer_decision_is_refused(self):
        first, port = self._execute(self._context())
        self.assertTrue(first.sent)

        newer, port2 = self._execute(
            self._context(
                journal=self.NEWER_JOURNAL, decisions=self.NEWER_DECISIONS,
                expectation=_expectation(generation=3, decision_journal=self.NEWER_JOURNAL),
            )
        )
        self.assertFalse(newer.sent)
        self.assertEqual(newer.reason, REASON_DUPLICATE)
        self.assertEqual(port2.calls, [])

    def test_ack_then_a_strictly_newer_decision_delivers_once(self):
        first, _ = self._execute(self._context())
        self.assertTrue(first.sent)
        self.assertTrue(self._ack(first.action_id))

        newer, port = self._execute(
            self._context(
                journal=self.NEWER_JOURNAL, decisions=self.NEWER_DECISIONS,
                expectation=_expectation(generation=3, decision_journal=self.NEWER_JOURNAL),
            )
        )
        self.assertTrue(newer.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(port.calls[0][1], self.NEWER_JOURNAL)
        self.assertNotEqual(newer.action_id, first.action_id)

    def test_ack_does_not_reopen_the_same_decision(self):
        first, _ = self._execute(self._context())
        self.assertTrue(self._ack(first.action_id))

        repeat, port = self._execute(self._context())
        self.assertFalse(repeat.sent)
        self.assertEqual(repeat.reason, REASON_DUPLICATE)
        self.assertEqual(port.calls, [])

    def test_an_unknown_or_stale_action_id_acks_nothing(self):
        first, _ = self._execute(self._context())
        self.assertFalse(self._ack("pxy_deadbeef"))
        self.assertFalse(self._ack(""))
        self.assertTrue(self._ack(first.action_id))
        self.assertFalse(self._ack(first.action_id))  # already completed

    def test_a_foreign_workspace_cannot_ack(self):
        first, _ = self._execute(self._context())
        self.assertFalse(
            self.fence.complete_by_action_id(first.action_id, workspace_id=FOREIGN_WS)
        )
        self.assertTrue(self._ack(first.action_id))

    def test_an_undelivered_generation_cannot_be_acked(self):
        # A send whose outcome was unknown stays uncertain: an ack must not paper over it.
        result, _ = self._execute(self._context(), port=CountingSendPort(result=SEND_FAILED))
        self.assertFalse(result.sent)
        self.assertFalse(self._ack(result.action_id))


class ConcurrentRetryOutcomeTest(ProxySendTestBase):
    """The outcome write is a CAS, and its result is evidence (review j#90032 finding 2).

    Ignoring it produced the one failure mode this whole rail exists to prevent: the caller was told
    the delegation was delivered while the store said `uncertain`. Because `proxy-ack` completes only
    a `delivered` generation, the route then had no way forward at all — an exactly-once rail that
    reports success and wedges is worse than one that reports failure.

    These drive the REAL fence, not a stub: the race is a store-level transition, so a fake that
    always returns True would assert the very thing under test away.
    """

    class _RacingSendPort:
        """Re-enters the reserve mid-send, exactly as a concurrent retry does."""

        def __init__(self, fence, route, result=SEND_DELIVERED):
            self.fence = fence
            self.route = route
            self.calls = []
            self.racer_verdict = ""
            self._result = result

        def send(self, context, action_id, *, args):
            self.calls.append((context.issue, context.journal, action_id))
            racer = self.fence.reserve(
                self.route, issue=context.issue, journal=context.journal
            )
            self.racer_verdict = racer.verdict
            return ProxySendOutcome(result=self._result, rc=0)

    def _route(self):
        return ProxyRouteKey(
            workspace_id=WS, lane_id="default", role=ROLE_COORDINATOR, action=ACTION_DISPATCH_NEXT
        )

    def test_a_delivered_send_whose_generation_moved_is_not_reported_as_delivered(self):
        self.fence.bootstrap()
        route = self._route()
        port = self._RacingSendPort(self.fence, route)
        result, _ = self._execute(self._context(), port=port)

        self.assertEqual(len(port.calls), 1)  # exactly one send still fired
        self.assertEqual(port.racer_verdict, RESERVE_NEEDS_RECONCILE)
        self.assertFalse(result.sent)
        self.assertEqual(result.decision, ZERO_SEND)
        self.assertEqual(result.reason, REASON_DELIVERY_UNCERTAIN)

    def test_the_reported_verdict_matches_the_stored_state(self):
        # The defect was precisely a divergence between these two.
        self.fence.bootstrap()
        route = self._route()
        port = self._RacingSendPort(self.fence, route)
        result, _ = self._execute(self._context(), port=port)

        self.assertEqual(self.fence.active(route).state, "uncertain")
        self.assertFalse(result.sent)

    def test_a_wedged_route_is_not_completable_by_ack_and_says_so(self):
        self.fence.bootstrap()
        route = self._route()
        port = self._RacingSendPort(self.fence, route)
        result, _ = self._execute(self._context(), port=port)

        # `proxy-ack` completes only a delivered generation, so a route left uncertain must never
        # have been reported as delivered in the first place.
        self.assertFalse(
            self.fence.complete_by_action_id(result.action_id, workspace_id=WS)
        )
        self.assertFalse(result.sent)

    def test_an_uncontended_delivery_still_reports_delivered(self):
        self.fence.bootstrap()
        result, port = self._execute(self._context())
        self.assertTrue(result.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(self.fence.active(self._route()).state, "delivered")

    def test_a_failed_send_whose_generation_moved_is_still_a_non_delivery(self):
        self.fence.bootstrap()
        route = self._route()
        port = self._RacingSendPort(self.fence, route, result=SEND_FAILED)
        result, _ = self._execute(self._context(), port=port)
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, REASON_DELIVERY_UNCERTAIN)


class DecisionJournalFoldTest(unittest.TestCase):
    """The anchor evidence is read from the GENERIC workflow-event token (review j#89878 F1)."""

    def _entries(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        return [
            RedmineJournalEntry(ISSUE, "89688", "[mozyo:workflow-event:gate=implementation_request]"),
            RedmineJournalEntry(ISSUE, "89754", "[mozyo:workflow-event:gate=start]"),
            RedmineJournalEntry(ISSUE, "89873", "[mozyo:workflow-event:gate=implementation_done]"),
            RedmineJournalEntry(ISSUE, "89900", "no marker here, only prose about a gate"),
            RedmineJournalEntry("99999", "89901", "[mozyo:workflow-event:gate=implementation_request]"),
        ]

    def _fold(self, entries=None, issue=ISSUE):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            marker_fields_in_note,
        )

        return decision_journals_from_entries(
            self._entries() if entries is None else entries,
            issue=issue,
            parse=marker_fields_in_note,
        )

    def test_the_dispatch_decision_is_visible(self):
        # The exact regression: `implementation_request` is NOT in the callback-gate vocabulary,
        # so reading through that reader made the one decision `dispatch_next` needs invisible.
        journals = [d.journal for d in self._fold() if d.token == "implementation_request"]
        self.assertEqual(journals, ["89688"])

    def test_each_journal_is_keyed_by_its_own_entry_id(self):
        folded = {d.token: d.journal for d in self._fold()}
        self.assertEqual(folded["start"], "89754")
        self.assertEqual(folded["implementation_done"], "89873")

    def test_prose_is_never_a_decision(self):
        self.assertNotIn("89900", [d.journal for d in self._fold()])

    def test_another_issue_s_entry_never_contributes(self):
        self.assertNotIn("89901", [d.journal for d in self._fold()])

    def test_the_canonical_dispatch_marker_s_lane_and_generation_are_preserved(self):
        # Review j#89918 F2: dropping these is what left the decision unmatchable.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )

        entries = [
            RedmineJournalEntry(
                ISSUE, "90000",
                "[mozyo:workflow-event:kind=implementation_request:lane=lane_a:lane_generation=3]",
            )
        ]
        (record,) = self._fold(entries)
        self.assertEqual(record.journal, "90000")
        self.assertEqual(record.token, "implementation_request")
        self.assertEqual(record.lane, "lane_a")
        self.assertEqual(record.lane_generation, "3")

    def test_a_gate_style_marker_carries_no_lane_or_generation(self):
        (record,) = [d for d in self._fold() if d.token == "implementation_request"]
        self.assertEqual(record.lane, "")
        self.assertEqual(record.lane_generation, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
