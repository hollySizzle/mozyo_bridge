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
    DECISION_ACTION_FIELD,
    canonical_decision_in_journal,
    canonical_note_text,
    render_bootstrap_decision_marker,
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
BOOTSTRAP_MARKER = render_bootstrap_decision_marker()
DISPATCH_MARKER = render_bootstrap_decision_marker(lane=LANE, lane_generation="2")
#: The action IS declared, but the lane-scoped fields the classifier joins on are absent.
DISPATCH_MARKER_NO_LANE = (
    f"[mozyo:workflow-event:gate=implementation_request:{DECISION_ACTION_FIELD}=dispatch_next]"
)


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
#: The named journal's note. The reader looks at this one journal only (j#90329 contract 5).
NAMED_NOTES = {
    CURRENT_JOURNAL: f"canonical decision\n{DISPATCH_MARKER}",
    OLDER_JOURNAL: "canonical decision\n" + render_bootstrap_decision_marker(
        lane=LANE, lane_generation="1"
    ),
    # A real canonical decision on the same issue that authorizes a DIFFERENT action.
    OTHER_KIND_JOURNAL: f"canonical decision\n{BOOTSTRAP_MARKER}",
}


def _notes(mapping=None):
    """A named-journal provider over ``{journal: notes}``."""
    table = NAMED_NOTES if mapping is None else mapping

    def _read(_issue, journal):
        if journal in table:
            return table[journal], True
        return "", False

    return _read


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
        decisions = None if decisions is None else decisions
        return resolve_proxy_context(
            argparse.Namespace(repo=str(self.repo)),
            action=action,
            issue=issue,
            journal=journal,
            repo_root=self.repo,
            env=env if env is not None else {},
            rows_provider=lambda _env: rows,
            named_journal_provider=_notes(decisions),
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
        context = self._context(decisions={CURRENT_JOURNAL: DISPATCH_MARKER_NO_LANE})
        self.assertEqual(context.links.anchor, ANCHOR_DECISION_INCOMPLETE)

    def test_a_quoted_marker_in_the_named_journal_is_not_a_decision(self):
        context = self._context(
            decisions={CURRENT_JOURNAL: f"the grammar is `{DISPATCH_MARKER}`"}
        )
        self.assertEqual(context.links.anchor, ANCHOR_UNVERIFIED)

    def test_an_unreachable_redmine_is_not_verified(self):
        context = self._context(decisions={})
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
            ("unverified anchor", lambda: self._context(decisions={}),
             REASON_ANCHOR_UNVERIFIED),
            ("decision without a lane/generation",
             lambda: self._context(decisions={CURRENT_JOURNAL: DISPATCH_MARKER_NO_LANE}),
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


class DeliveryTerminalityTest(ProxySendTestBase):
    """The completion half of exactly-once, after Design Answer j#90329 contracts 1-4.

    The first draft delivered, marked `delivered`, and stopped — nothing ever completed the
    generation, so the route wedged and every later decision was refused as a duplicate. The second
    draft unwedged it with a `proxy-ack` command, which put the *authority* in the wrong place: the
    proxy cannot prove the coordinator acted, and an ack surface that anyone can drive is not
    evidence that it did.

    The contract now says a positively recorded delivery IS the proxy's terminal success. So the
    route advances with **no acknowledgement at all**: a strictly newer canonical decision mints the
    next generation, the same decision stays duplicate forever, and only the genuinely unresolved
    case (`uncertain`) needs an operator disposition to move.
    """

    NEWER_JOURNAL = "90100"
    NEWER_DECISIONS = dict(NAMED_NOTES)
    NEWER_DECISIONS["90100"] = "canonical decision\n" + render_bootstrap_decision_marker(
        lane=LANE, lane_generation="3"
    )

    def _route(self):
        return ProxyRouteKey(
            workspace_id=WS, lane_id="default", role=ROLE_COORDINATOR, action=ACTION_DISPATCH_NEXT
        )

    def _newer(self):
        return self._context(
            journal=self.NEWER_JOURNAL, decisions=self.NEWER_DECISIONS,
            expectation=_expectation(generation=3, decision_journal=self.NEWER_JOURNAL),
        )

    def test_a_delivery_is_terminal_so_a_newer_decision_needs_no_acknowledgement(self):
        # THE contract-1 property: the route unwedges on delivery alone. Nothing is acked here.
        first, _ = self._execute(self._context())
        self.assertTrue(first.sent)
        self.assertEqual(self.fence.active(self._route()).state, "delivered")

        newer, port = self._execute(self._newer())
        self.assertTrue(newer.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(port.calls[0][1], self.NEWER_JOURNAL)
        self.assertNotEqual(newer.action_id, first.action_id)

    def test_the_same_decision_stays_duplicate_after_delivery(self):
        first, _ = self._execute(self._context())
        self.assertTrue(first.sent)

        repeat, port = self._execute(self._context())
        self.assertFalse(repeat.sent)
        self.assertEqual(repeat.reason, REASON_DUPLICATE)
        self.assertEqual(port.calls, [])

    def test_an_older_decision_never_reopens_a_delivered_route(self):
        newer, _ = self._execute(self._newer())
        self.assertTrue(newer.sent)

        older, port = self._execute(self._context())
        self.assertFalse(older.sent)
        self.assertEqual(port.calls, [])

    def test_an_uncertain_generation_blocks_even_a_newer_decision(self):
        # `uncertain` is the one state that is genuinely unresolved: the send may have landed. A
        # newer decision must NOT be admitted over it, or the coordinator could receive two.
        first, _ = self._execute(self._context(), port=CountingSendPort(result=SEND_FAILED))
        self.assertFalse(first.sent)
        self.assertEqual(self.fence.active(self._route()).state, "uncertain")

        newer, port = self._execute(self._newer())
        self.assertFalse(newer.sent)
        self.assertEqual(newer.reason, REASON_DUPLICATE)
        self.assertEqual(port.calls, [])

    def test_a_confirmed_delivery_resolves_uncertainty_forward(self):
        first, _ = self._execute(self._context(), port=CountingSendPort(result=SEND_FAILED))
        self.assertTrue(
            self.fence.confirm_delivered(
                self._route(), first.action_id, detail="operator observed the delegation land",
                issue=ISSUE, journal=CURRENT_JOURNAL,
            )
        )
        self.assertEqual(self.fence.active(self._route()).state, "delivered")

        # Forward, not backward: the newer decision proceeds, the same one stays duplicate.
        repeat, port = self._execute(self._context())
        self.assertFalse(repeat.sent)
        self.assertEqual(port.calls, [])
        newer, port2 = self._execute(self._newer())
        self.assertTrue(newer.sent)
        self.assertEqual(len(port2.calls), 1)

    def test_a_proven_non_delivery_unwedges_the_route_without_replaying_the_decision(self):
        # Contract 4's strongest disposition releases the route, but a decision is delegated once:
        # the coordinator re-issues a decision, the operator does not replay the old one.
        first, _ = self._execute(self._context(), port=CountingSendPort(result=SEND_FAILED))
        self.assertTrue(
            self.fence.mark_abandoned(
                self._route(), first.action_id, detail="operator proved the send never left",
                issue=ISSUE, journal=CURRENT_JOURNAL,
            )
        )
        replay, port = self._execute(self._context())
        self.assertFalse(replay.sent)
        self.assertEqual(replay.reason, REASON_DUPLICATE)
        self.assertEqual(port.calls, [])

        newer, port2 = self._execute(self._newer())
        self.assertTrue(newer.sent)
        self.assertEqual(len(port2.calls), 1)
        self.assertNotEqual(newer.action_id, first.action_id)

    def test_a_disposition_must_name_the_generation_it_resolves(self):
        first, _ = self._execute(self._context(), port=CountingSendPort(result=SEND_FAILED))
        route = self._route()
        # Wrong action id, wrong issue, wrong journal: each is a no-op against the stored anchor.
        self.assertFalse(
            self.fence.confirm_delivered(route, "pxy_deadbeef", detail="x",
                                         issue=ISSUE, journal=CURRENT_JOURNAL)
        )
        self.assertFalse(
            self.fence.confirm_delivered(route, first.action_id, detail="x",
                                         issue="99999", journal=CURRENT_JOURNAL)
        )
        self.assertFalse(
            self.fence.mark_abandoned(route, first.action_id, detail="x",
                                      issue=ISSUE, journal="90999")
        )
        self.assertEqual(self.fence.active(route).state, "uncertain")

    def test_a_delivered_generation_is_not_abandonable(self):
        # The retryable terminal is reachable only from the unresolved state. A delivery that
        # landed cannot be argued away into a re-send.
        first, _ = self._execute(self._context())
        self.assertFalse(
            self.fence.mark_abandoned(
                self._route(), first.action_id, detail="wishful thinking",
                issue=ISSUE, journal=CURRENT_JOURNAL,
            )
        )
        self.assertEqual(self.fence.active(self._route()).state, "delivered")


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

    def test_a_raising_send_is_contained_and_leaves_a_reconcilable_generation(self):
        # Review j#90250 F3: an exception escaping the send skipped the outcome write entirely and
        # left the generation `reserved` — a state nothing auto-resolves and which is not safely
        # re-sendable, so the only ways out were a blind re-run or the whole-store recovery.
        class _RaisingPort:
            def __init__(self):
                self.calls = []

            def send(self, context, action_id, *, args):
                self.calls.append(action_id)
                raise RuntimeError("effect boundary unknown")

        self.fence.bootstrap()
        port = _RaisingPort()
        result, _ = self._execute(self._context(), port=port)

        self.assertEqual(len(port.calls), 1)
        self.assertFalse(result.sent)
        self.assertEqual(result.decision, ZERO_SEND)
        self.assertEqual(result.reason, REASON_DELIVERY_UNCERTAIN)
        # `uncertain` is the state an operator reconcile acts on; `reserved` is the one nothing
        # resolves.
        self.assertEqual(self.fence.active(self._route()).state, "uncertain")


class CanonicalDecisionGrammarTest(unittest.TestCase):
    """A decision is read from the NAMED journal, and a quotation is never one (j#90329 c5)."""

    def _read(self, notes, action="bootstrap_lane"):
        # No parser is injected any more (Redmine #14667). The reader used to take one, and these
        # tests handed it the LENIENT fold — so the grammar under test was the caller's choice
        # rather than the rail's, and a strict reader could have been added without a single test
        # exercising it. The scan and its strictness are the shared authority's now.
        return canonical_decision_in_journal(notes, action=action)

    def test_a_canonical_marker_is_a_decision(self):
        decision, refusal = self._read(f"body\n{BOOTSTRAP_MARKER}")
        self.assertEqual(refusal, "")
        self.assertEqual(decision.token, "implementation_request")

    def test_an_inline_quotation_is_not_a_decision(self):
        _d, refusal = self._read(f"the grammar is `{BOOTSTRAP_MARKER}` — do not copy it")
        self.assertEqual(refusal, "no_canonical_decision")

    def test_a_fenced_quotation_is_not_a_decision(self):
        _d, refusal = self._read(f"```\n{BOOTSTRAP_MARKER}\n```")
        self.assertEqual(refusal, "no_canonical_decision")

    def test_two_markers_in_one_journal_are_refused(self):
        _d, refusal = self._read(f"{BOOTSTRAP_MARKER}\n{BOOTSTRAP_MARKER}")
        self.assertEqual(refusal, "duplicate_canonical_decision")

    def test_a_marker_without_the_action_field_is_refused(self):
        _d, refusal = self._read("[mozyo:workflow-event:gate=implementation_request]")
        self.assertEqual(refusal, "action_not_declared")

    def test_a_marker_declaring_another_action_is_refused(self):
        _d, refusal = self._read(f"{DISPATCH_MARKER}", action="bootstrap_lane")
        self.assertEqual(refusal, "action_not_declared")

    def test_the_lane_scoped_marker_carries_lane_and_generation(self):
        decision, refusal = self._read(DISPATCH_MARKER, action="dispatch_next")
        self.assertEqual(refusal, "")
        self.assertEqual((decision.lane, decision.lane_generation), (LANE, "2"))

    def test_code_regions_are_stripped_before_scanning(self):
        self.assertNotIn("mozyo", canonical_note_text(f"`{BOOTSTRAP_MARKER}`"))
        self.assertIn("mozyo", canonical_note_text(BOOTSTRAP_MARKER))


class QuotedDecisionShapeTest(ProxySendTestBase):
    """A quotation is not a decision — in EVERY shape Markdown renders as quoted (#14577 j#90392).

    The first quote-aware draft stripped fenced blocks and inline backticks, which reads as "handled"
    until you notice those are two of the four ways Markdown quotes. Live acceptance wrote the
    grammar into a journal as a plain `>` blockquote and the rail read it as the coordinator's own
    instruction: `decisions=[{journal: 90389}]`, `links.anchor=verified`. The delegation was
    zero-send only because a LATER link (`proxy_coordinator_authority_missing`) happened to break —
    an accident of the invocation context, not the quotation being refused.

    So these are written as a class, not as one more special case. Each negative is a way of saying
    "this text is an example"; each positive is the coordinator actually speaking. The positives
    matter as much as the negatives: a rule that refuses quotations by refusing everything would
    make the rail unusable, and the failure would look identical to a correct refusal.
    """

    #: The live probe note, verbatim (#14577 j#90389) — the shape that reached acceptance.
    LIVE_QUOTED_NOTE = (
        "## R8 negative probe — quoted bootstrap marker\n"
        "\n"
        "これは仕様例の引用であり、decisionではない。\n"
        "\n"
        "> [mozyo:workflow-event:gate=implementation_request:proxy_action=bootstrap_lane]\n"
        "\n"
        "- expected: named-journal parser がquotationとして拒否する\n"
        "- action: `bootstrap_lane`\n"
        "\n"
        "issue_14577\n"
    )

    def _read(self, notes, action="bootstrap_lane"):
        # No parser is injected any more (Redmine #14667). The reader used to take one, and these
        # tests handed it the LENIENT fold — so the grammar under test was the caller's choice
        # rather than the rail's, and a strict reader could have been added without a single test
        # exercising it. The scan and its strictness are the shared authority's now.
        return canonical_decision_in_journal(notes, action=action)

    def test_the_live_acceptance_note_is_not_a_decision(self):
        _d, refusal = self._read(self.LIVE_QUOTED_NOTE)
        self.assertEqual(refusal, "no_canonical_decision")

    def test_every_blockquote_shape_is_refused(self):
        shapes = {
            "plain": f"> {BOOTSTRAP_MARKER}",
            "no space after the marker char": f">{BOOTSTRAP_MARKER}",
            "nested": f"> > {BOOTSTRAP_MARKER}",
            "nested without spaces": f">>{BOOTSTRAP_MARKER}",
            "leading whitespace": f"   > {BOOTSTRAP_MARKER}",
            "fenced inside a quote": f"> ```\n> {BOOTSTRAP_MARKER}\n> ```",
        }
        for label, note in shapes.items():
            with self.subTest(shape=label):
                _d, refusal = self._read(note)
                self.assertEqual(refusal, "no_canonical_decision")

    def test_every_verbatim_block_shape_is_refused(self):
        shapes = {
            "backtick fence": f"```\n{BOOTSTRAP_MARKER}\n```",
            "backtick fence with an info string": f"```text\n{BOOTSTRAP_MARKER}\n```",
            "tilde fence": f"~~~\n{BOOTSTRAP_MARKER}\n~~~",
            "unclosed fence": f"```\n{BOOTSTRAP_MARKER}",
            "indented code": f"    {BOOTSTRAP_MARKER}",
            "tab-indented code": f"\t{BOOTSTRAP_MARKER}",
            "inline span": f"the grammar is `{BOOTSTRAP_MARKER}`",
        }
        for label, note in shapes.items():
            with self.subTest(shape=label):
                _d, refusal = self._read(note)
                self.assertEqual(refusal, "no_canonical_decision")

    def test_the_coordinators_own_voice_still_carries_a_decision(self):
        # The other side of the boundary. Refusing quotations must not refuse instructions.
        shapes = {
            "bare line": BOOTSTRAP_MARKER,
            "three spaces of indent": f"   {BOOTSTRAP_MARKER}",
            "on a list bullet": f"- decision: {BOOTSTRAP_MARKER}",
            "after prose": f"この lane を bootstrap する。\n\n{BOOTSTRAP_MARKER}\n",
            "with a `>` inside the prose": f"generation 1 > 0 なので進める。\n{BOOTSTRAP_MARKER}",
        }
        for label, note in shapes.items():
            with self.subTest(shape=label):
                decision, refusal = self._read(note)
                self.assertEqual(refusal, "", label)
                self.assertEqual(decision.token, "implementation_request")

    def test_a_quoted_example_beside_a_real_decision_does_not_poison_it(self):
        # The whole point of contract 5: documenting the grammar next to a real instruction must
        # neither transfer authority to the example nor make the journal ambiguous.
        note = f"例:\n\n> {BOOTSTRAP_MARKER}\n\n実際の decision:\n\n{BOOTSTRAP_MARKER}\n"
        decision, refusal = self._read(note)
        self.assertEqual(refusal, "")
        self.assertEqual(decision.token, "implementation_request")

    def test_a_marker_cannot_be_spliced_together_across_a_quoted_region(self):
        # The marker body is `[^\]]*`, which spans newlines. Blanking a quoted line without also
        # scanning line by line leaves an unclosed `[mozyo:` free to close on a `]` further down —
        # and every field it needs is already on the first line, so the splice parses cleanly as a
        # decision that no single line of the note contains.
        note = (
            f"{BOOTSTRAP_MARKER[:-1]}\n"  # the marker, minus its closing bracket
            "> quoted noise\n"
            "]\n"
        )
        _d, refusal = self._read(note)
        self.assertEqual(refusal, "no_canonical_decision")

    def test_the_delegation_refuses_a_blockquoted_decision_end_to_end(self):
        # Not just the parser: the resolution must report an UNVERIFIED anchor, because the live
        # failure was that `links.anchor` said `verified` and only a later link happened to break.
        context = self._context(decisions={CURRENT_JOURNAL: self.LIVE_QUOTED_NOTE})
        self.assertEqual(context.links.anchor, ANCHOR_UNVERIFIED)
        result, port = self._execute(context)
        self.assertFalse(result.sent)
        self.assertEqual(result.decision, ZERO_SEND)
        self.assertEqual(result.reason, REASON_ANCHOR_UNVERIFIED)
        self.assertEqual(port.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
