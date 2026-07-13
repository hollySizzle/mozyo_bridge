"""`enforce_gateway_route` disposition-gate wiring (Redmine #13681 W3).

Proves the send gate actually fires end-to-end against a REAL lifecycle store: a
governed delivery to a superseded lane emits the blocked outcome and dies before any
text is typed, while an active lane and an owner-unbound lane (no row) stay
byte-invariant. This is the adversarial proof that the disposition seam is wired to
the same `(workspace, lane)` key the create / supersede writes use — not merely that
the pure policy can block.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_SUPERSEDED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    gateway_route_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_route_gate import (  # noqa: E501
    _resolve_target_disposition,
    enforce_gateway_route,
)

WS = "wProj"
LANE = "issue_13583_x"
ISSUE = "13583"


@dataclass
class _Target:
    workspace_id: str
    lane_id: str
    role: str = "codex"


class _FakeBinding:
    def provider_for(self, role):
        return "claude"


class _Die(Exception):
    pass


class DispositionGateWiringTest(unittest.TestCase):
    def _seed(self, home: Path, disposition: str) -> None:
        store = LaneLifecycleStore(home=home)
        key = LaneLifecycleKey(WS, LANE)
        decision = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="76630")
        store.declare_active(key, decision=decision, issue_id=ISSUE)
        if disposition == DISPOSITION_SUPERSEDED:
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_SUPERSEDED,
                decision=decision,
            )

    def test_resolve_disposition_matches_store_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._seed(home, DISPOSITION_SUPERSEDED)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                self.assertEqual(
                    _resolve_target_disposition(_Target(WS, LANE)),
                    DISPOSITION_SUPERSEDED,
                )
                # Owner-unbound / absent lane -> None (byte-invariant).
                self.assertIsNone(_resolve_target_disposition(_Target(WS, "other")))
                # Missing unit fields -> None, never a raised key error.
                self.assertIsNone(_resolve_target_disposition(_Target("", LANE)))

    def _enforce(self, home: Path, target: _Target):
        emitted = []
        args = argparse.Namespace(allow_direct_worker=False)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), patch.object(
            gateway_route_gate, "load_workflow_binding", return_value=(_FakeBinding(), [])
        ), patch.object(gateway_route_gate, "die", side_effect=_Die):
            enforce_gateway_route(
                args,
                kind="implementation_request",
                receiver="codex",
                preflight_target=target,
                source="redmine",
                mode="queue-enter",
                anchor=None,
                target="wProj:p9",
                record_format="text",
                record_command=None,
                emit=lambda outcome, **kw: emitted.append(outcome),
                sender_lane_unit=(None, None),
            )
        return emitted

    def test_superseded_lane_send_blocks_and_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._seed(home, DISPOSITION_SUPERSEDED)
            with self.assertRaises(_Die):
                self._enforce(home, _Target(WS, LANE))

    def test_active_lane_send_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._seed(home, DISPOSITION_ACTIVE)
            emitted = self._enforce(home, _Target(WS, LANE))
            self.assertEqual(emitted, [])  # no block emitted, no die

    def test_owner_unbound_lane_send_is_byte_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)  # no lifecycle row seeded
            emitted = self._enforce(home, _Target(WS, "never_declared"))
            self.assertEqual(emitted, [])


if __name__ == "__main__":
    unittest.main()
