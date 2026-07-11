"""Host-restart recovery reconciliation scenario (Redmine #13521 / #13518 j#75329 / #13520 j#75276).

One cohesive narrative over the session-start adopt planner acting as the recovery reconciler,
asserting the reboot-recovery bullets end-to-end rather than in isolation:

- **positive** — a lane slot still backed by a live agent adopts on its durable assigned name
  (the route resumes without a relaunch);
- **negative** — a lane slot that is only shell / name residue after the reboot (durable name
  survives, foreground `-zsh`, no detected agent) is classified `stale`, never blind-adopted
  and never launched over the still-taken name;
- **never-clobber consistency** — the reconciliation pass is read-only: no `agent start`, no
  `pane close`, no `workspace create`. The destructive stale-pane close + same-slot relaunch
  stays an owner-approved recovery gate (#13518 j#75331), not a side effect of the plan.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    SLOT_ADOPTED,
    SLOT_STALE,
    prepare_session,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"
LANE = "issue_13518_zero_wait_callback"


class _RebootHerdr:
    """A fake herdr whose `agent list` returns a post-reboot inventory; records every call.

    Any non-list command (agent start / pane close / workspace create) is recorded so the test
    can assert the reconciliation pass performed no destructive side effect.
    """

    def __init__(self, rows):
        self._rows = rows
        self.calls: list = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        self.calls.append(rest)
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self._rows}), stderr=""
            )
        # A real launch/close/create should never happen in a read-only reconciliation pass.
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


class HostRestartRecoveryScenarioTest(unittest.TestCase):
    def _prepare(self, tmp, rows):
        repo = Path(tmp) / "repo"
        repo.mkdir()
        home = Path(tmp) / "home"
        home.mkdir()
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            register_workspace(repo, home=home)
            ws = read_anchor(repo)["workspace_id"]
            herdr = _RebootHerdr(rows(ws))
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            result = prepare_session(
                repo_root=repo,
                providers=["codex", "claude"],
                lane_id=LANE,
                env={HERDR_ENV: str(binpath)},
                runner=herdr.run,
            )
        return result, herdr

    def test_reboot_inventory_reconciles_live_adopt_and_shell_residue_stale(self):
        # After the reboot the codex gateway is a shell residue (name survived, no detected
        # agent, agent_status unknown) while the claude worker is still a live agent.
        with tempfile.TemporaryDirectory() as tmp:
            result, herdr = self._prepare(
                tmp,
                rows=lambda ws: [
                    {
                        "name": encode_assigned_name(ws, "codex", LANE),
                        "pane_id": "w19:p3",
                        "agent_status": "unknown",
                    },
                    {
                        "name": encode_assigned_name(ws, "claude", LANE),
                        "pane_id": "w19:p4",
                        "agent": "claude",
                        "agent_status": "idle",
                    },
                ],
            )
        by_provider = {s.provider: s for s in result.slots}
        # negative: the shell-residue gateway is stale, surfaced with its residue pane locator.
        self.assertEqual(by_provider["codex"].outcome, SLOT_STALE)
        self.assertEqual(by_provider["codex"].locator, "w19:p3")
        # positive: the live worker adopts and resumes on its durable assigned name.
        self.assertEqual(by_provider["claude"].outcome, SLOT_ADOPTED)
        self.assertEqual(by_provider["claude"].locator, "w19:p4")
        # never-clobber consistency: the pass is read-only — no destructive herdr side effect.
        self.assertEqual([c for c in herdr.calls if c[:2] == ["agent", "start"]], [])
        self.assertEqual([c for c in herdr.calls if c[:2] == ["pane", "close"]], [])
        self.assertEqual([c for c in herdr.calls if c[:2] == ["workspace", "create"]], [])


if __name__ == "__main__":
    unittest.main()
