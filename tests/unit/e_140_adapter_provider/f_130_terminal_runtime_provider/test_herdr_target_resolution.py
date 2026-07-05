"""herdr-native sender identity + target resolution tests (Redmine #13261).

Classical, fail-closed coverage of the pure core projection: sender identity from
launch env + anchor, and receiver-label -> live-agent resolution over ``agent list``
rows. No subprocess, no tmux — plain string / row inputs.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    MOZYO_AGENT_ROLE_ENV,
    MOZYO_LANE_ID_ENV,
    MOZYO_WORKSPACE_ID_ENV,
    REASON_COORDINATOR_BINDING_UNRESOLVED,
    REASON_ENV_ANCHOR_WORKSPACE_MISMATCH,
    REASON_INVALID_SENDER_ROLE,
    REASON_MISSING_ANCHOR,
    REASON_MISSING_LOCATOR,
    REASON_MISSING_SENDER_ENV,
    REASON_MULTIPLE_MATCHES,
    REASON_NO_MATCH,
    REASON_UNKNOWN_RECEIVER,
    SenderIdentity,
    resolve_herdr_target,
    resolve_sender_identity,
    resolve_target_role,
)

WS = "ws-alpha"


def _env(ws=WS, role="claude", lane="lane-1"):
    e = {}
    if ws is not None:
        e[MOZYO_WORKSPACE_ID_ENV] = ws
    if role is not None:
        e[MOZYO_AGENT_ROLE_ENV] = role
    if lane is not None:
        e[MOZYO_LANE_ID_ENV] = lane
    return e


def _row(ws, role, lane, locator):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class SenderIdentityTest(unittest.TestCase):
    def test_valid_env_and_anchor(self) -> None:
        res = resolve_sender_identity(_env(), anchor_workspace_id=WS)
        self.assertTrue(res.ok)
        self.assertEqual(
            res.identity, SenderIdentity(workspace_id=WS, role="claude", lane_id="lane-1")
        )

    def test_empty_lane_defaults(self) -> None:
        res = resolve_sender_identity(_env(lane=None), anchor_workspace_id=WS)
        self.assertTrue(res.ok)
        self.assertEqual(res.identity.lane_id, "default")

    def test_missing_workspace_env_fails_closed(self) -> None:
        res = resolve_sender_identity(_env(ws=None), anchor_workspace_id=WS)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MISSING_SENDER_ENV)

    def test_missing_role_env_fails_closed(self) -> None:
        res = resolve_sender_identity(_env(role=None), anchor_workspace_id=WS)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MISSING_SENDER_ENV)

    def test_invalid_sender_role_fails_closed(self) -> None:
        res = resolve_sender_identity(_env(role="grok"), anchor_workspace_id=WS)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_INVALID_SENDER_ROLE)

    def test_missing_anchor_fails_closed(self) -> None:
        res = resolve_sender_identity(_env(), anchor_workspace_id=None)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MISSING_ANCHOR)

    def test_env_anchor_workspace_mismatch_fails_closed(self) -> None:
        res = resolve_sender_identity(_env(ws="ws-other"), anchor_workspace_id=WS)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_ENV_ANCHOR_WORKSPACE_MISMATCH)


class TargetRoleTest(unittest.TestCase):
    def test_claude_codex_pass_through(self) -> None:
        self.assertEqual(
            resolve_target_role("claude", coordinator_provider="codex").role, "claude"
        )
        self.assertEqual(
            resolve_target_role("codex", coordinator_provider="codex").role, "codex"
        )

    def test_coordinator_uses_binding_provider(self) -> None:
        res = resolve_target_role("coordinator", coordinator_provider="codex")
        self.assertTrue(res.ok)
        self.assertEqual(res.role, "codex")

    def test_coordinator_unbound_fails_closed(self) -> None:
        res = resolve_target_role("coordinator", coordinator_provider="")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_COORDINATOR_BINDING_UNRESOLVED)

    def test_unknown_receiver_fails_closed(self) -> None:
        res = resolve_target_role("owner", coordinator_provider="codex")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_UNKNOWN_RECEIVER)


class ResolveHerdrTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sender = SenderIdentity(workspace_id=WS, role="claude", lane_id="lane-1")

    def test_resolves_single_codex_agent(self) -> None:
        rows = [
            _row(WS, "claude", "lane-1", "w1:p1"),
            _row(WS, "codex", "lane-1", "w1:p2"),
        ]
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.locator, "w1:p2")
        self.assertEqual(res.assigned_name, encode_assigned_name(WS, "codex", "lane-1"))

    def test_coordinator_resolves_to_codex_agent(self) -> None:
        rows = [_row(WS, "codex", "lane-1", "w1:p2")]
        res = resolve_herdr_target(
            "coordinator", self.sender, rows, coordinator_provider="codex"
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.locator, "w1:p2")

    def test_coordinator_binding_failure_fails_closed(self) -> None:
        rows = [_row(WS, "codex", "lane-1", "w1:p2")]
        res = resolve_herdr_target(
            "coordinator", self.sender, rows, coordinator_provider=None
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_COORDINATOR_BINDING_UNRESOLVED)

    def test_no_match_when_role_absent(self) -> None:
        # Only a claude agent present; asking for codex -> role mismatch -> no_match.
        rows = [_row(WS, "claude", "lane-1", "w1:p1")]
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_NO_MATCH)

    def test_workspace_mismatch_excluded_from_match(self) -> None:
        # A codex agent exists but in another workspace -> not a candidate -> no_match.
        rows = [_row("ws-other", "codex", "lane-1", "wX:pX")]
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_NO_MATCH)
        self.assertIn("another workspace", res.detail)

    def test_duplicate_assigned_name_fails_closed(self) -> None:
        name = encode_assigned_name(WS, "codex", "lane-1")
        rows = [
            {"name": name, "pane_id": "w1:p2"},
            {"name": name, "pane_id": "w1:p3"},
        ]
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MULTIPLE_MATCHES)

    def test_multiple_lanes_same_role_fails_closed(self) -> None:
        rows = [
            _row(WS, "codex", "lane-1", "w1:p2"),
            _row(WS, "codex", "lane-2", "w1:p3"),
        ]
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MULTIPLE_MATCHES)

    def test_malformed_name_row_is_skipped(self) -> None:
        # A foreign herdr agent (non-mzb1 name) is skipped; with no valid target the
        # resolution is a clean no_match (never a crash / mis-match).
        rows = [
            {"name": "poc_claude", "pane_id": "w9:p9"},
            {"name": "not a name", "pane_id": "w9:p8"},
        ]
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_NO_MATCH)

    def test_missing_locator_fails_closed(self) -> None:
        name = encode_assigned_name(WS, "codex", "lane-1")
        rows = [{"name": name}]  # no pane_id / pane / location
        res = resolve_herdr_target(
            "codex", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MISSING_LOCATOR)

    def test_unknown_receiver_fails_closed(self) -> None:
        rows = [_row(WS, "codex", "lane-1", "w1:p2")]
        res = resolve_herdr_target(
            "owner", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_UNKNOWN_RECEIVER)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
