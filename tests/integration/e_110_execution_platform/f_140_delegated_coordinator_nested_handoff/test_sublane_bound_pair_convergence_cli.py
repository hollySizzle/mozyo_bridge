"""CLI wiring for the #13933 ``sublane converge-bound-pair`` public rail."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    BoundPairObservation,
    PinRepairResult,
    ReplacementDrive,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import BoundSlot
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import SLOT_RECOVER
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live as live


BASE = [
    "sublane", "converge-bound-pair",
    "--issue", "13933",
    "--journal", "80899",
    "--lane", "issue_13933_bound_stale_pair_convergence",
    "--worktree", "/tmp/wt-13933",
    "--branch", "issue_13933_bound_stale_pair_convergence",
]


class _Ops:
    def __init__(self, *, inventory=True):
        slots = (
            BoundSlot("gateway", "codex", "gw", "w1:p1", SLOT_RECOVER),
            BoundSlot("worker", "claude", "wk", "w1:p2", SLOT_RECOVER),
        )
        self.observation = BoundPairObservation(
            workspace_id="ws",
            worktree_path="/tmp/wt-13933",
            worktree_identity="wt_test",
            branch="issue_13933_bound_stale_pair_convergence",
            revision=4,
            generation=1,
            lifecycle_exact=True,
            pins_empty=True,
            inventory_readable=inventory,
            worktree_readable=True,
            worktree_clean=True,
            branch_matches=True,
            slots=slots if inventory else (),
        )

    def observe(self, request, *, action_id=""):
        return self.observation

    def approval_fields(self, issue, journal):
        return ()

    def drive_replacement(self, request, expectation, slots):
        return ReplacementDrive(False, "not_called")

    def final_pins(self, request, *, action_id):
        return self.observation, ()

    def repair_pins(self, request, expectation, observation, pins):
        return PinRepairResult(False, "not_called")

    def finish_replacement(self, expectation):
        return False


class BoundPairConvergenceCliTests(unittest.TestCase):
    def setUp(self):
        self.original = live.LiveBoundPairConvergenceOps

    def tearDown(self):
        live.LiveBoundPairConvergenceOps = self.original

    def _run(self, argv, ops):
        live.LiveBoundPairConvergenceOps = lambda **kwargs: ops
        parser = build_parser(None)
        ns = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = ns.func(ns)
        return rc, json.loads(stdout.getvalue())

    def test_registers_under_sublane_and_requires_exact_branch_and_worktree(self):
        parser = build_parser(None)
        ns = parser.parse_args(BASE)
        self.assertEqual(ns.func.__name__, "cmd_sublane_converge_bound_pair")
        for flag in ("--issue", "--journal", "--lane", "--worktree", "--branch"):
            argv = list(BASE)
            index = argv.index(flag)
            del argv[index : index + 2]
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)

    def test_default_is_read_only_and_emits_exact_approval_marker(self):
        rc, payload = self._run(BASE + ["--json"], _Ops())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "actionable")
        self.assertFalse(payload["executed"])
        self.assertIn("gate=bound_pair_convergence_approval", payload["approval_marker"])

    def test_execute_without_live_structured_approval_is_zero_effect_exit_1(self):
        rc, payload = self._run(BASE + ["--execute", "--json"], _Ops())
        self.assertEqual(rc, 1)
        self.assertEqual(payload["reason"], "approval_missing")
        self.assertTrue(payload["executed"])

    def test_unreadable_inventory_fails_closed_before_approval_read(self):
        rc, payload = self._run(BASE + ["--execute", "--json"], _Ops(inventory=False))
        self.assertEqual(rc, 1)
        self.assertEqual(payload["reason"], "inventory_unreadable")


if __name__ == "__main__":
    unittest.main()
