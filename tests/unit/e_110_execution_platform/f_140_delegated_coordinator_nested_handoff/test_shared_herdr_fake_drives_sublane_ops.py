"""Shared fake herdr drives a real production composition seam (Redmine #13407).

US A of #13398 requires the new shared fake to work against at least one real
module ("最低 1 module で動作実証"), driving the **real composition seam** rather
than a stubbed one (auditor #13398 j#73769 裁定 1). This module is that proof:
it stands up the real :class:`HerdrSublaneActuatorOps` — the same append →
read-back round trip the canonical inline ``_StatefulHerdr`` exercises
(``test_sublane_actuator_herdr_ops.py``) — but with ``support.herdr_fake.FakeHerdr``
injected at the outermost ``Runner`` boundary. It shows the shared fake is a
drop-in for the per-file inline fake: the real ops drive ``workspace create`` →
``agent start`` (×2) → ``pane close`` (base) and read the lane back out of
``agent list``, all against the shared state machine.

This is a *new* demonstration module; it does not touch the existing inline-fake
tests (their byte-invariant migration is US E, not this US).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
_TESTS_ROOT = Path(__file__).resolve().parents[3]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from mozyo_bridge.core.state.workspace_registry import read_anchor  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E402,E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E402,E501
    SUBLANE_STATE_ACTIVE,
)

from support.herdr_fake import FakeHerdr  # noqa: E402
from tests.support.agent_provider_binaries import provider_bin_path, with_provider_path

HERDR_ENV = "MOZYO_HERDR_BINARY"


def _fake_binary(tmp: str) -> Path:
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binpath


class SharedFakeDrivesSublaneOpsTest(unittest.TestCase):
    def _ops(self, tmp, fake):
        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        coord = Path(tmp) / "coord"
        coord.mkdir(exist_ok=True)
        binpath = _fake_binary(tmp)
        env = with_provider_path({HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)})
        ops = HerdrSublaneActuatorOps(
            repo_root=coord,
            lane_label="issue_13407_x",
            issue="13407",
            env=env,
            runner=fake.run,
        )
        return ops, home

    def test_append_then_read_lane_round_trips_through_shared_fake(self) -> None:
        fake = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, fake)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # A fresh worktree has no herdr workspace yet -> read is absent.
                self.assertIsNone(ops.read_lane(str(worktree)))
                ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                lane_ws = read_anchor(worktree)["workspace_id"]

        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, lane_ws)
        self.assertEqual(view.lane_id, "issue_13407_x")
        self.assertEqual(view.repo_root, str(worktree))
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)
        # Both managed slots resolved to distinct live herdr locators in the same
        # (single) herdr workspace the shared fake minted for the lane.
        self.assertTrue(view.gateway_pane and view.worker_pane)
        self.assertNotEqual(view.gateway_pane, view.worker_pane)
        gateway_ws = view.gateway_pane.split(":", 1)[0]
        worker_ws = view.worker_pane.split(":", 1)[0]
        self.assertEqual(gateway_ws, worker_ws)
        self.assertIn(gateway_ws, fake.workspace_ids)

    def test_real_ops_launch_claude_worker_auto_via_shared_fake(self) -> None:
        # The real launch sequence flows through the shared fake: the recorded
        # `agent start` argvs show the Claude worker gets `--permission-mode auto`
        # (Redmine #13360) and Codex does not — proving the shared fake carries the
        # real launch argv end to end, not a stubbed shape.
        fake = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, fake)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
        by_provider = {}
        for argv in fake.start_argvs:
            # argv[0] is the resolved absolute executable (#13441), not the label.
            argv0 = argv[argv.index("--") + 1]
            provider = next(
                (p for p in ("claude", "codex") if argv0 == provider_bin_path(p)),
                argv0,
            )
            by_provider[provider] = argv
        self.assertIn("--permission-mode", by_provider["claude"])
        self.assertEqual(
            by_provider["claude"][by_provider["claude"].index("--permission-mode") + 1],
            "auto",
        )
        self.assertNotIn("--permission-mode", by_provider["codex"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
