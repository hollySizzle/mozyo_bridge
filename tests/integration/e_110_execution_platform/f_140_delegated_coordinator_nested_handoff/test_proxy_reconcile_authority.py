"""Reconcile authority tests (Redmine #14546, Design Consultation Answer j#90329 contracts 2 & 4).

This suite is the record of a claim being withdrawn rather than relocated a third time.

The first authority asked "is the process running this command the coordinator?" and answered it
from the process's own ``MOZYO_*`` env — but a workspace id, a provider and the default lane are
published values, so an external caller could reproduce them exactly and be admitted. The second
moved the question to "is there an acknowledgement recorded on the anchored issue?" — but a Redmine
note is writable by anyone holding an API key, so that relocated the forgery instead of closing it.

The answer was that the proxy cannot prove the coordinator *acted*, so it stopped claiming to.
A positively recorded **delivery** is the proxy's terminal success (contract 1), acknowledgement is
withdrawn as an authority (contract 2), and the only generation still needing a human is the one
whose fate is genuinely unknown. What is pinned here:

- ``workflow proxy-ack`` reaches no store at all, whatever env or action id it is handed;
- ``workflow proxy-reconcile`` applies an operator's *finding* to exactly one generation, joined to
  that generation's stored ``(proxy_action_id, issue, journal)`` anchor — the disposition that
  releases the route is the one held to evidence and to an exact match.

The store here is the REAL fence: these are claims about state transitions, and a stub that returned
``True`` would assert the thing under test away.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.coordinator_proxy_fence import (  # noqa: E501
    PROXY_ABANDONED,
    PROXY_DELIVERED,
    PROXY_UNCERTAIN,
    CoordinatorProxyFence,
    ProxyRouteKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow_proxy,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_proxy import (  # noqa: E501
    DISPOSITION_CONFIRMED,
    DISPOSITION_NOT_SENT,
    DISPOSITION_UNKNOWN,
    cmd_workflow_proxy_ack,
    cmd_workflow_proxy_reconcile,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    ACTION_DISPATCH_NEXT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E501
    ROLE_COORDINATOR,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
ISSUE = "14546"
JOURNAL = "89736"
NEWER_JOURNAL = "90100"

#: Everything an external caller can know: the published triplet and the action id itself.
PUBLIC_ENV = {
    "MOZYO_WORKSPACE_ID": WS,
    "MOZYO_AGENT_ROLE": "codex",
    "MOZYO_LANE_ID": "default",
}


class ProxyCliTestBase(unittest.TestCase):
    """Drives the real commands against a real, isolated delegation store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        self.fence = CoordinatorProxyFence(Path(self._tmp.name) / "proxy.sqlite")
        self.fence.bootstrap()

        import mozyo_bridge.core.state.coordinator_proxy_fence as fence_mod

        self.fence_mod = fence_mod
        original = fence_mod.CoordinatorProxyFence
        fence_mod.CoordinatorProxyFence = lambda *a, **k: self.fence
        self.addCleanup(setattr, fence_mod, "CoordinatorProxyFence", original)

        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send as send_mod  # noqa: E501

        self.send_mod = send_mod
        ws_original = send_mod.live_workspace_id
        send_mod.live_workspace_id = lambda _root: WS
        self.addCleanup(setattr, send_mod, "live_workspace_id", ws_original)

        auth_original = send_mod.resolve_default_lane_authority
        send_mod.resolve_default_lane_authority = lambda _root: (
            "resolved", ROLE_COORDINATOR, "bare_mozyo_workspace", ""
        )
        self.addCleanup(setattr, send_mod, "resolve_default_lane_authority", auth_original)

    @property
    def route(self) -> ProxyRouteKey:
        return ProxyRouteKey(
            workspace_id=WS, lane_id="default", role=ROLE_COORDINATOR, action=ACTION_DISPATCH_NEXT
        )

    def _uncertain_generation(self, journal: str = JOURNAL) -> str:
        """A generation parked in the one state that needs a human, as a lost send leaves it."""
        reserved = self.fence.reserve(self.route, issue=ISSUE, journal=journal)
        self.assertTrue(reserved.won)
        self.assertTrue(
            self.fence.mark_uncertain(
                self.route, reserved.action_id, issue=ISSUE, journal=journal
            )
        )
        return reserved.action_id

    def _reconcile(self, **overrides):
        fields = dict(
            repo=str(self.repo), action=ACTION_DISPATCH_NEXT, proxy_action_id="", issue=ISSUE,
            journal=JOURNAL, disposition=DISPOSITION_UNKNOWN, evidence="", execute=False,
            as_json=True,
        )
        fields.update(overrides)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_workflow_proxy_reconcile(argparse.Namespace(**fields))
        return rc, json.loads(buf.getvalue())


class WithdrawnAcknowledgementTest(ProxyCliTestBase):
    """Contract 2: the ack surface stayed, its authority did not."""

    def _ack(self, *, env=None, action_id="pxy_deadbeef", issue=ISSUE):
        import os

        saved = {k: os.environ.get(k) for k in PUBLIC_ENV}
        if env:
            os.environ.update(env)

            def _restore():
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            self.addCleanup(_restore)
        args = argparse.Namespace(
            repo=str(self.repo), issue=issue, proxy_action_id=action_id, as_json=True
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_workflow_proxy_ack(args)
        return rc, json.loads(buf.getvalue())

    def test_the_published_triplet_and_a_live_action_id_change_nothing(self):
        # THE forgery the previous two authorities each admitted in turn: everything the external
        # caller can know, aimed at a generation that really is delivered on this store.
        reserved = self.fence.reserve(self.route, issue=ISSUE, journal=JOURNAL)
        self.fence.mark_delivered(self.route, reserved.action_id, issue=ISSUE, journal=JOURNAL)

        rc, out = self._ack(env=PUBLIC_ENV, action_id=reserved.action_id)
        self.assertEqual(rc, 1)
        self.assertFalse(out["completed"])
        self.assertTrue(out["deprecated"])
        self.assertEqual(out["reason"], "proxy_ack_withdrawn")
        # The generation is untouched: the ack neither completed it nor consumed it.
        self.assertEqual(self.fence.active(self.route).state, PROXY_DELIVERED)

    def test_the_command_touches_no_store_at_all(self):
        # Not "fails to complete" — never reaches a store. Any attempt to open one fails the test.
        def _explode(*_a, **_k):
            raise AssertionError("proxy-ack must not open the delegation store")

        self.fence_mod.CoordinatorProxyFence = _explode
        rc, out = self._ack()
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_ack_withdrawn")

    def test_it_stays_registered_so_an_existing_runbook_fails_visibly(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="workflow_command")
        cli_workflow_proxy.register_proxy_parsers(sub)
        parsed = parser.parse_args(["proxy-ack", "--issue", ISSUE, "--proxy-action-id", "pxy_x"])
        self.assertIs(parsed.func, cmd_workflow_proxy_ack)


class ReconcileAnchorTest(ProxyCliTestBase):
    """Contract 4: a disposition applies to ONE generation, named exactly."""

    def test_the_full_anchor_is_required(self):
        action_id = self._uncertain_generation()
        for missing in ("proxy_action_id", "issue", "journal"):
            with self.subTest(missing=missing):
                fields = {
                    "proxy_action_id": action_id, "disposition": DISPOSITION_CONFIRMED,
                    "evidence": "operator note", "execute": True,
                }
                fields[missing] = ""
                rc, out = self._reconcile(**fields)
                self.assertEqual(rc, 1)
                self.assertEqual(out["reason"], "proxy_reconcile_anchor_required")
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)

    def test_an_unknown_disposition_is_refused(self):
        action_id = self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id=action_id, disposition="looks-fine", evidence="e", execute=True
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_reconcile_disposition_unknown")
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)

    def test_a_terminal_disposition_needs_evidence(self):
        action_id = self._uncertain_generation()
        for disposition in (DISPOSITION_CONFIRMED, DISPOSITION_NOT_SENT):
            with self.subTest(disposition=disposition):
                rc, out = self._reconcile(
                    proxy_action_id=action_id, disposition=disposition, evidence="", execute=True
                )
                self.assertEqual(rc, 1)
                self.assertEqual(out["reason"], "proxy_reconcile_evidence_required")
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)

    def test_another_generations_action_id_applies_nothing(self):
        self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id="pxy_deadbeef", disposition=DISPOSITION_NOT_SENT,
            evidence="operator note", execute=True,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_reconcile_no_match")
        self.assertFalse(out["applied"])
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)

    def test_a_different_decision_anchor_applies_nothing(self):
        # The action id alone is not the anchor: the decision the generation carries is part of it,
        # so a disposition written against a different journal cannot land on this one.
        action_id = self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id=action_id, journal=NEWER_JOURNAL,
            disposition=DISPOSITION_CONFIRMED, evidence="operator note", execute=True,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_reconcile_anchor_mismatch")
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)

    def test_it_is_a_dry_run_until_execute(self):
        action_id = self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id=action_id, disposition=DISPOSITION_NOT_SENT, evidence="operator note"
        )
        self.assertEqual(rc, 0)
        self.assertFalse(out["applied"])
        self.assertFalse(out["executed"])
        self.assertEqual(out["generation_state"], PROXY_UNCERTAIN)
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)


class ReconcileDispositionTest(ProxyCliTestBase):
    """Each disposition says what was ESTABLISHED, and moves the route accordingly."""

    def test_confirmed_delivered_reaches_the_terminal_success(self):
        action_id = self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id=action_id, disposition=DISPOSITION_CONFIRMED,
            evidence="the coordinator's transcript shows the delegated turn", execute=True,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["applied"])
        self.assertEqual(out["generation_state"], PROXY_DELIVERED)
        # Terminal success: the route takes a strictly newer decision, and no acknowledgement.
        self.assertTrue(self.fence.reserve(self.route, issue=ISSUE, journal=NEWER_JOURNAL).won)

    def test_proven_not_sent_releases_the_route_without_replaying_the_decision(self):
        action_id = self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id=action_id, disposition=DISPOSITION_NOT_SENT,
            evidence="no delegated turn in the coordinator's transcript", execute=True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["generation_state"], PROXY_ABANDONED)
        # A decision is delegated once, whatever its generation's fate: the coordinator issues the
        # next decision, the operator does not replay the old one.
        self.assertFalse(self.fence.reserve(self.route, issue=ISSUE, journal=JOURNAL).won)
        self.assertTrue(self.fence.reserve(self.route, issue=ISSUE, journal=NEWER_JOURNAL).won)

    def test_unknown_claims_nothing_terminal(self):
        action_id = self._uncertain_generation()
        rc, out = self._reconcile(
            proxy_action_id=action_id, disposition=DISPOSITION_UNKNOWN, execute=True
        )
        # Already uncertain: there is nothing to move, and nothing is claimed.
        self.assertEqual(rc, 1)
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "proxy_reconcile_not_applicable")
        self.assertEqual(self.fence.active(self.route).state, PROXY_UNCERTAIN)
        # And it never admits a newer decision on its own.
        self.assertFalse(self.fence.reserve(self.route, issue=ISSUE, journal=NEWER_JOURNAL).won)

    def test_a_delivered_generation_is_not_argued_back_open(self):
        reserved = self.fence.reserve(self.route, issue=ISSUE, journal=JOURNAL)
        self.fence.mark_delivered(self.route, reserved.action_id, issue=ISSUE, journal=JOURNAL)
        rc, out = self._reconcile(
            proxy_action_id=reserved.action_id, disposition=DISPOSITION_NOT_SENT,
            evidence="wishful thinking", execute=True,
        )
        self.assertEqual(rc, 1)
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "proxy_reconcile_not_applicable")
        self.assertEqual(self.fence.active(self.route).state, PROXY_DELIVERED)

    def test_the_command_is_registered_with_its_anchor_and_disposition(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="workflow_command")
        cli_workflow_proxy.register_proxy_parsers(sub)
        parsed = parser.parse_args(
            [
                "proxy-reconcile", "--action", ACTION_DISPATCH_NEXT,
                "--proxy-action-id", "pxy_x", "--issue", ISSUE, "--journal", JOURNAL,
                "--disposition", DISPOSITION_NOT_SENT, "--evidence", "e", "--execute",
            ]
        )
        self.assertIs(parsed.func, cmd_workflow_proxy_reconcile)
        self.assertEqual(parsed.disposition, DISPOSITION_NOT_SENT)
        self.assertTrue(parsed.execute)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
