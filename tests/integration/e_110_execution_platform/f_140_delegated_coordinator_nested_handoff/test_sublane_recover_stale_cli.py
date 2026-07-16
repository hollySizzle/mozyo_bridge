"""`sublane recover-stale` CLI wiring tests (Redmine #13806 tranche D j#79485 / R1-F1 j#79528).

The owner-facing surface of the stale-worker recovery contract, wired to the LIVE inventory
observation (review j#79528 F1 — a fail-closed staged seam left the product gap open). The
command actually classifies the pinned target in read-only preflight; ``--execute`` refuses a
non-actionable target zero-close. These tests drive the command hermetically with a fake herdr
``agent list`` (no real managed worker is actuated) and pin:

- registration under the ``sublane`` family and the required exact-target fields;
- a preflight that classifies a real target (``identity_unknown`` when the inventory is empty,
  ``actionable`` for a genuine stale worker) and exits 0 (an informational report);
- an ``--execute`` on a non-actionable target that refuses zero-close and exits 1.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser  # noqa: E402
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live as live  # noqa: E402,E501
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection as proj  # noqa: E402,E501
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

WS = "wsCLI"
LANE = "issue_13806_recover"
ROLE = "claude"
LOCATOR = "w9:p5"
NAME = encode_assigned_name(WS, ROLE, LANE)

BASE = [
    "sublane", "recover-stale",
    "--issue", "13806", "--lane", LANE, "--role", ROLE,
    "--provider", ROLE, "--assigned-name", NAME, "--locator", LOCATOR,
]


def _stale_row():
    return {
        "name": NAME, "pane_id": LOCATOR, "agent": "", "status": "unknown",
        "revision": 3, "foreground_cwd": str(ROOT),
    }


class RecoverStaleCliTests(unittest.TestCase):
    def setUp(self):
        self._orig_rows = live.list_herdr_agent_rows
        self._orig_ws_live = live.repo_scope_workspace_id
        self._orig_ws_proj = proj.repo_scope_workspace_id
        live.repo_scope_workspace_id = lambda root: WS
        proj.repo_scope_workspace_id = lambda root: WS

    def tearDown(self):
        live.list_herdr_agent_rows = self._orig_rows
        live.repo_scope_workspace_id = self._orig_ws_live
        proj.repo_scope_workspace_id = self._orig_ws_proj

    def _run(self, argv, rows):
        live.list_herdr_agent_rows = lambda env: rows
        parser = build_parser(None)
        ns = parser.parse_args(argv + ["--repo", str(ROOT)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ns.func(ns)
        return rc, out.getvalue()

    def test_registers_under_sublane_family(self):
        parser = build_parser(None)
        ns = parser.parse_args(BASE)
        self.assertEqual(ns.func.__name__, "cmd_sublane_recover_stale")

    def test_exact_target_fields_are_required(self):
        parser = build_parser(None)
        for drop in ("--lane", "--role", "--provider", "--assigned-name", "--locator"):
            argv = list(BASE)
            idx = argv.index(drop)
            del argv[idx : idx + 2]
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)

    def test_preflight_empty_inventory_is_identity_unknown_exit_0(self):
        rc, text = self._run(BASE + ["--json"], rows=[])
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], "identity_unknown")
        self.assertEqual(payload["status"], "preflight")
        self.assertFalse(payload["executed"])
        self.assertEqual(rc, 0)  # a preflight report is informational

    def test_preflight_classifies_real_stale_target_actionable(self):
        rc, text = self._run(BASE + ["--json"], rows=[_stale_row()])
        payload = json.loads(text)
        self.assertEqual(payload["verdict"], "actionable")
        self.assertEqual(payload["status"], "preflight")
        self.assertEqual(rc, 0)

    def test_execute_on_non_actionable_target_refuses_zero_close_exit_1(self):
        rc, text = self._run(BASE + ["--execute", "--json"], rows=[])
        payload = json.loads(text)
        self.assertTrue(payload["executed"])
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["verdict"], "identity_unknown")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
