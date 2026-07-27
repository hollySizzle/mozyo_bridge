"""Acknowledgement authority tests (Redmine #14546, review j#90250 finding 1).

The previous authority asked "is the process running this command the coordinator?" and answered it
from the process's own ``MOZYO_*`` env. A workspace id, a provider and the default lane are
published values, not secrets, and this platform offers no proof that a process *is* the slot whose
triplet it presents — so an external caller could reproduce them exactly and be admitted. Deriving
the canonical slot name from those same caller-supplied fields correlated the claim with itself.

The authority is now the coordinator's acknowledgement **recorded on the anchored issue**, read back
from source-of-truth Redmine. What is pinned here is that supplying the published triplet — and even
the exact action id — reaches no store at all without that durable record existing.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow_proxy,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_proxy import (  # noqa: E501
    cmd_workflow_proxy_ack,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
    render_proxy_ack_marker,
    verify_ack_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
ISSUE = "14546"
ACTION_ID = "pxy_2d60ab6542c34b8a048bb0a7373cb083"

#: Everything an external caller can know: the published triplet and the action id itself.
PUBLIC_ENV = {
    "MOZYO_WORKSPACE_ID": WS,
    "MOZYO_AGENT_ROLE": "codex",
    "MOZYO_LANE_ID": "default",
}


def _entries(*notes, issue=ISSUE):
    return [RedmineJournalEntry(issue, str(900 + i), note) for i, note in enumerate(notes)]


class AckRecordAuthorityTest(unittest.TestCase):
    def _verify(self, entries, *, issue=ISSUE, action_id=ACTION_ID):
        return verify_ack_record(
            argparse.Namespace(), issue=issue, action_id=action_id,
            entries_provider=lambda _i: entries,
        )

    def test_a_recorded_acknowledgement_authorizes(self):
        ok, reason, _detail = self._verify(_entries(render_proxy_ack_marker(ACTION_ID)))
        self.assertTrue(ok, reason)

    def test_no_record_authorizes_nothing(self):
        ok, reason, _detail = self._verify(_entries("the coordinator says it acted"))
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_not_recorded")

    def test_a_record_for_another_action_id_does_not_authorize(self):
        ok, reason, _detail = self._verify(_entries(render_proxy_ack_marker("pxy_other")))
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_not_recorded")

    def test_a_record_on_another_issue_does_not_authorize(self):
        entries = _entries(render_proxy_ack_marker(ACTION_ID), issue="99999")
        ok, reason, _detail = self._verify(entries)
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_not_recorded")

    def test_an_unreadable_record_authorizes_nothing(self):
        def _raise(_issue):
            raise RuntimeError("redmine unreachable")

        ok, reason, _detail = verify_ack_record(
            argparse.Namespace(), issue=ISSUE, action_id=ACTION_ID, entries_provider=None,
        )
        # No credentials configured in the test environment -> unreadable, never assumed.
        self.assertFalse(ok)
        self.assertIn(reason, ("proxy_ack_record_unreadable", "proxy_ack_not_recorded"))

    def test_the_issue_and_action_id_are_both_required(self):
        self.assertFalse(self._verify(_entries(), issue="")[0])
        self.assertFalse(self._verify(_entries(), action_id="")[0])


class _RecordingFence:
    def __init__(self) -> None:
        self.completions: list = []

    def is_bootstrapped(self) -> bool:
        return True

    def complete_by_action_id(self, action_id, *, workspace_id, **_kw):
        self.completions.append((action_id, workspace_id))
        return True


class ExternalCallerCannotCompleteTest(unittest.TestCase):
    """The forgery the previous authority admitted, driven through the CLI."""

    def setUp(self) -> None:
        self.fence = _RecordingFence()
        import mozyo_bridge.core.state.coordinator_proxy_fence as fence_mod

        original = fence_mod.CoordinatorProxyFence
        fence_mod.CoordinatorProxyFence = lambda: self.fence
        self.addCleanup(setattr, fence_mod, "CoordinatorProxyFence", original)

        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send as send_mod  # noqa: E501

        self.send_mod = send_mod
        ws_original = send_mod.live_workspace_id
        send_mod.live_workspace_id = lambda _root: WS
        self.addCleanup(setattr, send_mod, "live_workspace_id", ws_original)

    def _record(self, entries):
        original = self.send_mod.verify_ack_record

        def _verify(args, *, issue, action_id, entries_provider=None):
            return original(args, issue=issue, action_id=action_id,
                            entries_provider=lambda _i: entries)

        self.send_mod.verify_ack_record = _verify
        self.addCleanup(setattr, self.send_mod, "verify_ack_record", original)

    def _run(self, *, issue=ISSUE, action_id=ACTION_ID, env=None):
        args = argparse.Namespace(
            repo=str(ROOT), issue=issue, proxy_action_id=action_id, as_json=True
        )
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
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_workflow_proxy_ack(args)
        return rc, json.loads(buf.getvalue())

    def test_the_exact_published_triplet_and_action_id_touch_no_store(self):
        # THE forgery: everything the external caller can know, and nothing recorded.
        self._record(_entries("no acknowledgement here"))
        rc, out = self._run(env=PUBLIC_ENV)
        self.assertEqual(rc, 1)
        self.assertFalse(out["authorized"])
        self.assertFalse(out["completed"])
        self.assertEqual(out["reason"], "proxy_ack_not_recorded")
        self.assertEqual(self.fence.completions, [])

    def test_a_recorded_acknowledgement_completes(self):
        self._record(_entries(render_proxy_ack_marker(ACTION_ID)))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(out["authorized"])
        self.assertTrue(out["completed"])
        self.assertEqual(self.fence.completions, [(ACTION_ID, WS)])

    def test_env_is_irrelevant_to_the_verdict(self):
        # With the record present, an empty env still completes; with it absent, a perfect env
        # still does not. The invoking process is simply not the authority any more.
        self._record(_entries(render_proxy_ack_marker(ACTION_ID)))
        rc, _out = self._run()
        self.assertEqual(rc, 0)

    def test_an_unbootstrapped_store_completes_nothing(self):
        self._record(_entries(render_proxy_ack_marker(ACTION_ID)))
        self.fence.is_bootstrapped = lambda: False
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertTrue(out["authorized"])
        self.assertFalse(out["completed"])
        self.assertEqual(out["reason"], "proxy_fence_unavailable")
        self.assertEqual(self.fence.completions, [])

    def test_an_authorized_ack_still_needs_a_matching_delivered_generation(self):
        self._record(_entries(render_proxy_ack_marker(ACTION_ID)))
        self.fence.complete_by_action_id = lambda *_a, **_k: False
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertTrue(out["authorized"])
        self.assertFalse(out["completed"])
        self.assertEqual(out["reason"], "proxy_ack_no_match")

    def test_a_missing_action_id_is_refused_before_the_store(self):
        self._record(_entries(render_proxy_ack_marker(ACTION_ID)))
        rc, out = self._run(action_id="")
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_action_id_required")
        self.assertEqual(self.fence.completions, [])

    def test_a_missing_issue_is_refused_before_the_store(self):
        self._record(_entries(render_proxy_ack_marker(ACTION_ID)))
        rc, out = self._run(issue="")
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_ack_issue_required")
        self.assertEqual(self.fence.completions, [])

    def test_the_command_is_registered_with_an_issue_argument(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="workflow_command")
        cli_workflow_proxy.register_proxy_parsers(sub)
        parsed = parser.parse_args(
            ["proxy-ack", "--issue", ISSUE, "--proxy-action-id", ACTION_ID]
        )
        self.assertIs(parsed.func, cmd_workflow_proxy_ack)
        self.assertEqual(parsed.issue, ISSUE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
