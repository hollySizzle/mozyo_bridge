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

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_restored_pair_adopt_cli as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_adopt_live import (  # noqa: E402,E501
    LiveRestoredPairAdoptOps,
)

from tests.regressions.test_issue_15811_cold_pair_recovery import (  # noqa: E402
    GW_NAME,
    GW_OLD,
    GW_PROVIDER,
    GW_TERM_NEW,
    ISSUE,
    JOURNAL,
    LANE,
    TOKEN,
    WK_NAME,
    WK_OLD,
    WK_PROVIDER,
    WK_TERM_NEW,
    WS,
    _row,
    seed_restored_lane_fixture,
)

#: A host-local path that must never appear in operator output.
HOST_WORKTREE = "/home/operator/private/worktrees/issue_15811"


def _live_rows() -> list[dict]:
    """The restored fleet: same server-owned stamps, NEW terminal ids."""
    return [
        _row(GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER),
        _row(WK_NAME, WK_OLD, WK_TERM_NEW, WK_PROVIDER),
    ]


def _ops_factory(home: Path, rows):
    """A ``LiveRestoredPairAdoptOps`` bound to ``home`` with the host probes faked."""

    class _Ops(LiveRestoredPairAdoptOps):
        def _resolve_root(self):
            return Path(HOST_WORKTREE)

        def _workspace_id(self, root):
            return WS

        def _worktree_identity(self, root, lane):
            return TOKEN

        def _worktree_readable(self, root):
            return True

        def _branch(self, root):
            return LANE

        def _providers(self, root):
            return (GW_PROVIDER, WK_PROVIDER)

        def _rows(self):
            return list(rows)

    def factory(*, repo_root):
        return _Ops(
            repo_root=Path(repo_root),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )

    return factory


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
        factory = _ops_factory(self.home, _live_rows() if rows is None else rows)
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
        self.assertEqual(payload["plan"]["gateway"]["generation_state"],
                         "reattest_needed")

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
        rows = _live_rows() + [
            _row(GW_NAME, "w9:%99", "term-gw-dup", GW_PROVIDER)
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
