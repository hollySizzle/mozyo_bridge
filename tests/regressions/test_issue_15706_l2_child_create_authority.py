"""Redmine #15706 — an attested L2 gateway may create its own child L3 lane.

Measured (#15703 j#107980): the L2 delegated coordinator (lane
``issue_15693_l2_trial``, provider codex) ran ``sublane create --execute`` for its
child L3 lane and the sender preflight refused with ``missing_identity`` /
``sender_attestation`` — ``evaluate_dispatch_sender`` requires the sender to be the
default-lane coordinator, so no delegated_coordinator lane could ever create the
child its geometry exists for. #15700 opened the "L1 creates L2" entrance; the "L2
creates L3" sender authority did not exist.

The fix (design j#108058): the ops-level sender preflight runs the extended
sender-authority contract — a NON-default-lane sender creating a CHILD IMPLEMENTATION
lane is admitted iff the durable records verify it as the launch-time attested
gateway slot of an active ``delegated_coordinator`` lane. This file drives the exact
adapter seam that refused (``HerdrSublaneActuatorOps.preflight_dispatch_sender``)
against the REAL lifecycle + attestation stores and pins:

- the fixed symptom: the attested L2 gateway's child-implementation create passes the
  sender gate, and the verdict's verified parent lane is stashed for the child's
  lifecycle declaration;
- the pre-#15706 refusal is unchanged when the request is NOT a child implementation
  lane (the same sender without ``--lane-kind implementation`` keeps the legacy
  "is not the coordinator default lane" refusal — acceptance condition 2).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    IdentityAttestationRecord,
    record_identity_attestation,
)
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E402,E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    AGENT_KEY_TERMINAL_ID,
    DEFAULT_LANE,
    encode_assigned_name,
)

L2_LANE = "issue_15693_l2_trial"
CHILD_ISSUE = "15703"
CHILD_LANE = "issue_15703_l3_child"
PROXY_SEND = (
    "mozyo_bridge.e_110_execution_platform."
    "f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send"
)


class L2ChildCreateSenderAuthorityTest(unittest.TestCase):
    def _fixture(self, tmp: str) -> tuple:
        home = Path(tmp) / "home"
        home.mkdir()
        repo = Path(tmp) / "repo"
        repo.mkdir()
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            ws = register_workspace(repo, home=home).record.workspace_id
        LaneLifecycleStore(home=home).declare_active(
            LaneLifecycleKey(ws, L2_LANE),
            decision=DecisionPointer(
                source="redmine", issue_id="15693", journal_id="107868"
            ),
            issue_id="15693",
            worktree_identity="wt_l2trial01",
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        record_identity_attestation(
            IdentityAttestationRecord(
                assigned_name=encode_assigned_name(ws, "codex", L2_LANE),
                workspace_id=ws,
                role="codex",
                lane_id=L2_LANE,
                locator="w9:p1",
                verdict=VERDICT_PRESENT,
                terminal_id="t-l2gw",
            ),
            home=home,
        )
        rows = [
            {
                AGENT_KEY_NAME: encode_assigned_name(ws, "codex", L2_LANE),
                AGENT_KEY_LOCATOR: "w9:p1",
                AGENT_KEY_TERMINAL_ID: "t-l2gw",
            },
            {
                AGENT_KEY_NAME: encode_assigned_name(ws, "claude", L2_LANE),
                AGENT_KEY_LOCATOR: "w9:p2",
                AGENT_KEY_TERMINAL_ID: "t-l2wk",
            },
        ]
        env = {
            "MOZYO_WORKSPACE_ID": ws,
            "MOZYO_AGENT_ROLE": "codex",
            "MOZYO_LANE_ID": L2_LANE,
        }
        return home, repo, ws, rows, env

    def _preflight(self, home, repo, rows, env, *, lane_kind: str):
        ops = HerdrSublaneActuatorOps(
            repo_root=repo,
            lane_label=CHILD_LANE,
            issue=CHILD_ISSUE,
            journal="107980",
            lane_kind=lane_kind,
            env=env,
        )
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            with patch(f"{PROXY_SEND}.live_agent_rows", return_value=rows):
                ok, detail = ops.preflight_dispatch_sender()
        return ops, ok, detail

    def test_attested_l2_gateway_child_implementation_create_passes_the_gate(self):
        # The #15703 j#107980 shape, after the fix: the same sender identity that was
        # refused now passes the ops sender gate when the request is a child
        # implementation lane — and the VERIFIED parent lane is stashed for the
        # child's lifecycle declaration (never taken from raw env elsewhere).
        with tempfile.TemporaryDirectory() as tmp:
            home, repo, _ws, rows, env = self._fixture(tmp)
            ops, ok, detail = self._preflight(
                home, repo, rows, env, lane_kind=LANE_KIND_IMPLEMENTATION
            )
        self.assertTrue(ok, detail)
        self.assertIn("delegated_coordinator", detail)
        self.assertEqual(ops.verified_parent_lane_id, L2_LANE)

    def test_the_same_sender_without_a_child_implementation_request_still_refuses(self):
        # Acceptance condition 2: the pre-#15706 refusal is unchanged for a
        # non-child-implementation request from the same (attested) L2 sender.
        with tempfile.TemporaryDirectory() as tmp:
            home, repo, _ws, rows, env = self._fixture(tmp)
            ops, ok, detail = self._preflight(home, repo, rows, env, lane_kind="")
        self.assertFalse(ok)
        self.assertEqual(
            detail,
            f"sender lane {L2_LANE!r} is not the coordinator "
            f"default lane {DEFAULT_LANE!r}",
        )
        self.assertEqual(ops.verified_parent_lane_id, "")


if __name__ == "__main__":
    unittest.main()
