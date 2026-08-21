"""Operator-view acceptance for the pin-absent restored-pair adopt rail (Redmine #15811).

The recovery at the surface a coordinator actually drives
(``mozyo-bridge sublane adopt-restored-pair``): a lane whose gateway+worker pair a herdr
server generation change restored, and whose lifecycle row never declared pins, goes
through the read-only preflight and then ``--execute``. This suite pins what the operator
sees and what the exit code says — the contract the cockpit UI calls through, so ADR-0013's
"the user never has to reach for a pane" has a rail to invoke instead of an owner-run
manual pane close.

Context: ``e_110_execution_platform`` / ``f_140_delegated_coordinator_nested_handoff``,
crossing the live authority join, the use case and the CLI handler. Only the HOST probes
(repo root, workspace id, worktree token, branch, providers, live inventory) are faked; the
lifecycle / startup-fence / launch-generation / attestation stores are real against a temp
home, so nothing here launches a process, sends a notification or touches a worktree.

Scenarios pinned:

- the command surface itself: the rail is reachable as ``sublane adopt-restored-pair`` off
  the composed group, and ``--execute`` / ``--json`` are off by default (a write must be
  asked for). These are public-contract assertions, which is why they live here and not in
  the issue's regression file (`tests-placement-discovery-policy.md` R3-b; review j#109452
  ``finding_sharedtestsupport``);
- the read-only preflight on a recoverable lane exits 0 and reports ``may_adopt: True``;
- ``--execute`` exits 0, reports the declaration, and the second run exits 1 with the typed
  ``declared_pins_present`` refusal rather than writing again;
- a lane the rail cannot prove exits 1 with the typed slot reason (a refusal IS a command
  failure here — the operator must not read it as "recovered");
- ``--json`` emits the machine-readable payload the UI consumes;
- neither the text nor the JSON output carries a host-local worktree path.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli_common import add_repo_option  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_restored_pair_adopt_cli as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_sublane_group import (  # noqa: E402,E501
    register_sublane_group,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt_live import (  # noqa: E402,E501
    LiveRestoredPairAdoptOps,
)

from tests.support.restored_pair_fixtures import (  # noqa: E402
    GW_NAME,
    GW_PROVIDER,
    ISSUE,
    JOURNAL,
    LANE,
    REPO_ROOT,
    TOKEN,
    WK_NAME,
    WK_PROVIDER,
    WS,
    FakeHostProbes,
    inventory_row,
    preserved_pane_rows,
    seed_restored_lane_fixture,
)

#: The faked host repo root — a host-local path that must never reach operator output.
HOST_WORKTREE = str(REPO_ROOT)


def _ops_factory(home: Path, rows):
    """A ``LiveRestoredPairAdoptOps`` bound to ``home`` with the host probes faked."""

    class _Ops(FakeHostProbes, LiveRestoredPairAdoptOps):
        pass

    def factory(*, repo_root):
        ops = _Ops(
            repo_root=Path(repo_root),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )
        ops.repo_root = REPO_ROOT
        ops.test_workspace = WS
        ops.test_token = TOKEN
        ops.test_branch = LANE
        ops.test_providers = (GW_PROVIDER, WK_PROVIDER)
        ops.test_rows = list(rows)
        return ops

    return factory


class CommandSurface(unittest.TestCase):
    """The operator's entry point exists and defaults to doing nothing."""

    def test_the_rail_is_reachable_off_the_composed_sublane_group(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register_sublane_group(
            sub,
            add_repo_option=add_repo_option,
            add_lifecycle_json=lambda p: p.add_argument(
                "--lifecycle-json", action="store_true"
            ),
        )
        args = parser.parse_args(
            ["sublane", "adopt-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertEqual(args.sublane_command, "adopt-restored-pair")
        self.assertFalse(args.execute)

    def test_execute_and_json_are_off_unless_asked_for(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        cli.register_sublane_adopt_restored_pair_parser(sub)
        args = parser.parse_args(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertFalse(args.execute)
        self.assertFalse(args.json)
        args = parser.parse_args(
            [
                "adopt-restored-pair", "--issue", ISSUE, "--lane", LANE,
                "--journal", JOURNAL, "--execute", "--json",
            ]
        )
        self.assertTrue(args.execute)
        self.assertTrue(args.json)
        self.assertEqual(args.journal, JOURNAL)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mzb-15811-acceptance-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def drive(self, argv: list[str], *, rows=None) -> tuple[int, str]:
        """Run the command exactly as the operator's argv reaches it."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        cli.register_sublane_adopt_restored_pair_parser(sub)
        args = parser.parse_args(argv)
        buffer = io.StringIO()
        factory = _ops_factory(
            self.home, preserved_pane_rows() if rows is None else rows
        )
        with mock.patch.object(cli, "LiveRestoredPairAdoptOps", factory):
            with contextlib.redirect_stdout(buffer):
                code = cli.cmd_sublane_adopt_restored_pair(args)
        return code, buffer.getvalue()


class RecoverableLane(_Base):
    def setUp(self):
        super().setUp()
        seed_restored_lane_fixture(self.home)

    def test_preflight_exits_zero_and_reports_the_rail_is_ready(self):
        code, out = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("may_adopt: True", out)
        self.assertIn("status: preflight", out)
        self.assertIn("applied: False", out)

    def test_execute_exits_zero_and_reports_the_declaration(self):
        code, out = self.drive(
            [
                "adopt-restored-pair", "--issue", ISSUE, "--lane", LANE,
                "--journal", JOURNAL, "--execute",
            ]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("status: completed", out)
        self.assertIn("applied: True", out)
        self.assertIn("reattest_lineage[gateway]", out)

    def test_the_second_execute_exits_one_with_the_typed_refusal(self):
        first, _ = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE, "--execute"]
        )
        self.assertEqual(first, 0)
        code, out = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE, "--execute"]
        )
        self.assertEqual(code, 1, out)
        self.assertIn("declared_pins_present:declared_pin_pair_ok", out)
        self.assertIn("applied: False", out)

    def test_json_carries_the_machine_readable_verdict(self):
        code, out = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE, "--json"]
        )
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertTrue(payload["plan"]["may_adopt"])
        self.assertEqual(payload["plan"]["blocked_reasons"], [])
        self.assertEqual(payload["plan"]["gateway"]["assigned_name"], GW_NAME)
        self.assertEqual(payload["plan"]["worker"]["assigned_name"], WK_NAME)
        self.assertEqual(
            payload["plan"]["gateway"]["generation_state"], "reattest_needed"
        )

    def test_no_host_local_worktree_path_reaches_the_operator_output(self):
        _, text = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE, "--execute"]
        )
        self.assertNotIn(HOST_WORKTREE, text)
        _, payload = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE, "--json"]
        )
        self.assertNotIn(HOST_WORKTREE, payload)


class UnprovableLaneIsACommandFailure(_Base):
    def test_a_slot_the_rail_cannot_prove_exits_one_with_the_typed_reason(self):
        seed_restored_lane_fixture(self.home)
        rows = preserved_pane_rows() + [
            inventory_row(GW_NAME, "w9:%99", "term-gw-dup", GW_PROVIDER)
        ]
        code, out = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE, "--execute"],
            rows=rows,
        )
        self.assertEqual(code, 1, out)
        self.assertIn("duplicate_live_candidates:gateway", out)
        self.assertIn("may_adopt: False", out)

    def test_an_absent_lane_row_exits_one_rather_than_reporting_success(self):
        code, out = self.drive(
            ["adopt-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertEqual(code, 1, out)
        self.assertIn("lifecycle_row_absent", out)


if __name__ == "__main__":
    unittest.main()
