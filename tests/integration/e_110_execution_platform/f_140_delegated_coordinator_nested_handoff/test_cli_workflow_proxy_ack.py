"""``workflow proxy-ack`` admission tests (Redmine #14546, review j#89969 finding 1).

The first acknowledgement surface admitted anyone who held the delegation's opaque action id — and
the delegation envelope hands that id straight back to the **external client** that made it. So the
caller could complete its own delegation the instant it was delivered, never waiting for the
coordinator, and immediately open the route for the next one. That is not an acknowledgement; it is
a delivery receipt promoted to completion truth, which
``vibes/docs/logics/ack-completion-receiver-state.md`` explicitly separates.

These tests drive the **CLI command**, not the store method underneath it — the previous round's
tests called ``complete_by_action_id()`` directly, which is exactly why an unguarded command surface
went unnoticed. What is pinned here is the admission: possession of an action id is worth nothing,
an unattested shell is refused, and every write happens after the authority check rather than
before it.
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


class _RecordingFence:
    """A fence that refuses to be used unless the command already admitted the caller."""

    def __init__(self) -> None:
        self.completions: list = []
        self.bootstrapped = True

    def is_bootstrapped(self) -> bool:
        return self.bootstrapped

    def complete_by_action_id(self, action_id, *, workspace_id, **_kw):
        self.completions.append((action_id, workspace_id))
        return True


class ProxyAckAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fence = _RecordingFence()
        self._patch("CoordinatorProxyFence", lambda: self.fence)

    def _patch(self, name, value):
        """Patch a name into the lazily-imported module the command resolves at call time."""
        import mozyo_bridge.core.state.coordinator_proxy_fence as fence_mod

        original = getattr(fence_mod, name)
        setattr(fence_mod, name, value)
        self.addCleanup(setattr, fence_mod, name, original)

    def _authority(self, result):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send as send_mod  # noqa: E501

        original = send_mod.resolve_ack_authority
        send_mod.resolve_ack_authority = lambda _root, *, env: result
        self.addCleanup(setattr, send_mod, "resolve_ack_authority", original)

    def _workspace(self, value):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send as send_mod  # noqa: E501

        original = send_mod.live_workspace_id
        send_mod.live_workspace_id = lambda _root: value
        self.addCleanup(setattr, send_mod, "live_workspace_id", original)

    def _run(self, action_id="pxy_abc"):
        args = argparse.Namespace(
            repo=str(ROOT), proxy_action_id=action_id, as_json=True
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_workflow_proxy_ack(args)
        return rc, json.loads(buf.getvalue())

    def test_an_unattested_caller_cannot_complete_and_never_touches_the_store(self):
        self._workspace("ws1")
        self._authority(
            (False, "proxy_ack_unattested", "this shell carries no attested lane identity")
        )
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertFalse(out["authorized"])
        self.assertFalse(out["completed"])
        self.assertEqual(out["reason"], "proxy_ack_unattested")
        # The decisive assertion: holding the id got the caller nowhere near the fence.
        self.assertEqual(self.fence.completions, [])

    def test_possession_of_a_real_action_id_is_not_a_credential(self):
        # The external client receives this id in its own delegation envelope.
        self._workspace("ws1")
        self._authority((False, "proxy_ack_unattested", "not the coordinator"))
        rc, out = self._run(action_id="pxy_0123456789abcdef0123456789abcdef")
        self.assertEqual(rc, 1)
        self.assertFalse(out["completed"])
        self.assertEqual(self.fence.completions, [])

    def test_a_foreign_workspace_sender_cannot_complete(self):
        self._workspace("ws1")
        self._authority(
            (False, "proxy_ack_foreign_workspace", "different workspace")
        )
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_ack_foreign_workspace")
        self.assertEqual(self.fence.completions, [])

    def test_a_non_default_lane_agent_cannot_complete(self):
        self._workspace("ws1")
        self._authority((False, "proxy_ack_not_default_lane", "sublane agent"))
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_ack_not_default_lane")
        self.assertEqual(self.fence.completions, [])

    def test_the_attested_default_coordinator_completes(self):
        self._workspace("ws1")
        self._authority((True, "", "attested default coordinator"))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(out["authorized"])
        self.assertTrue(out["completed"])
        self.assertEqual(self.fence.completions, [("pxy_abc", "ws1")])

    def test_an_admitted_caller_still_needs_a_matching_delivered_generation(self):
        self._workspace("ws1")
        self._authority((True, "", "attested default coordinator"))
        self.fence.complete_by_action_id = lambda *_a, **_k: False
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertTrue(out["authorized"])
        self.assertFalse(out["completed"])
        self.assertEqual(out["reason"], "proxy_ack_no_match")

    def test_an_admitted_caller_without_an_action_id_is_refused(self):
        self._workspace("ws1")
        self._authority((True, "", "attested default coordinator"))
        rc, out = self._run(action_id="")
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "proxy_action_id_required")
        self.assertEqual(self.fence.completions, [])

    def test_the_command_is_registered_on_the_workflow_group(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="workflow_command")
        cli_workflow_proxy.register_proxy_parsers(sub)
        parsed = parser.parse_args(["proxy-ack", "--proxy-action-id", "pxy_x"])
        self.assertIs(parsed.func, cmd_workflow_proxy_ack)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
