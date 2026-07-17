"""CLI wiring for #13933 ``sublane prepare-bound-pair``."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (
    PreparationDrive,
    PreparationObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    BoundSlot,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_PRESERVE_PENDING,
    SLOT_RECOVER,
)
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard_live as live


BASE = [
    "sublane", "prepare-bound-pair",
    "--issue", "13933",
    "--journal", "80908",
    "--lane", "issue_13933_bound_stale_pair_convergence",
    "--worktree", "/tmp/wt-13933",
    "--branch", "issue_13933_bound_stale_pair_convergence",
]


class _Ops:
    def __init__(self):
        self.observation = PreparationObservation(
            workspace_id="ws",
            worktree_path="/tmp/wt-13933",
            worktree_identity="wt_test",
            branch="issue_13933_bound_stale_pair_convergence",
            revision=4,
            generation=1,
            lifecycle_exact=True,
            pins_empty=True,
            inventory_readable=True,
            worktree_readable=True,
            worktree_clean=True,
            branch_matches=True,
            slots=(
                BoundSlot("gateway", "codex", "gw", "w1:p1", SLOT_PRESERVE_PENDING),
                BoundSlot("worker", "claude", "wk", "w1:p2", SLOT_RECOVER),
            ),
            discard_roles=("gateway",),
        )

    def observe(self, request, *, action_id=""):
        return self.observation

    def approval_fields(self, issue, journal):
        return ()

    def drive(self, request, expectation, initial):
        return PreparationDrive(False, "not_called")


class PrepareBoundPairCliTests(unittest.TestCase):
    def setUp(self):
        self.original = live.LiveBoundPairPreparationOps

    def tearDown(self):
        live.LiveBoundPairPreparationOps = self.original

    def _run(self, argv):
        live.LiveBoundPairPreparationOps = lambda **kwargs: _Ops()
        parser = build_parser(None)
        ns = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = ns.func(ns)
        return rc, json.loads(stdout.getvalue())

    def test_registers_with_all_exact_identity_arguments(self):
        parser = build_parser(None)
        ns = parser.parse_args(BASE)
        self.assertEqual(ns.func.__name__, "cmd_sublane_prepare_bound_pair")
        for flag in ("--issue", "--journal", "--lane", "--worktree", "--branch"):
            argv = list(BASE)
            index = argv.index(flag)
            del argv[index : index + 2]
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)

    def test_default_is_read_only_and_emits_discard_marker(self):
        rc, payload = self._run(BASE + ["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "actionable")
        self.assertFalse(payload["executed"])
        self.assertEqual(payload["discard_roles"], ["gateway"])
        self.assertIn("gate=bound_pair_composer_discard_approval", payload["approval_marker"])

    def test_execute_without_live_structured_approval_is_zero_effect(self):
        rc, payload = self._run(BASE + ["--execute", "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(payload["reason"], "approval_missing")
        self.assertTrue(payload["executed"])
        self.assertFalse(payload["pins_repaired"])
        self.assertFalse(payload["resumed"])
        self.assertFalse(payload["sent"])


if __name__ == "__main__":
    unittest.main()
