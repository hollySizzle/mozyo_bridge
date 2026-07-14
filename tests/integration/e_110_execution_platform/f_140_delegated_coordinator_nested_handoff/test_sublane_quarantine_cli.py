"""`sublane quarantine` CLI wiring tests (Redmine #13763 j#78052 step 4).

The CLI is the owner-facing surface of the quarantine contract, so its defaults are part
of the safety boundary:

- the subcommand registers under the ``sublane`` family (alongside hibernate / resume);
- every exact-target field (lane / role / assigned name / locator / action generation /
  approval journal + timestamp + revision) is **required** — a partially identified
  receiver can never be named on the command line;
- the default is read-only preflight: ``--execute`` is opt-in, and without it the run
  closes nothing and writes no lifecycle row;
- a blocked outcome exits non-zero, and the JSON payload carries the fixed classification
  and generation only — never the composer body.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.lane_lifecycle import (
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.core.state.lane_lifecycle_model import REPLACEMENT_NOT_REQUESTED
from mozyo_bridge.core.state.lane_replacement import LaneReplacementStore
from mozyo_bridge.core.state.lane_replacement_model import quarantine_action_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_quarantine as quarantine_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    QuarantineInspection,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    PendingComposerSignal,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
ISSUE = "13763"
LANE = "issue_13763_pending_composer_quarantine"
ROLE = "claude"
NAME = encode_assigned_name(WS, ROLE, LANE)
OLD_LOCATOR = f"{WS}:p44"
APPROVAL_JOURNAL = "78200"
APPROVED_AT = "2026-07-14T09:05:00+00:00"
ATTESTED_AT = "2026-07-14T09:00:00+00:00"
AGENT_REVISION = 7
ACTION = quarantine_action_id(lane_id=LANE, role=ROLE, locator=OLD_LOCATOR)

SECRET_BODY = "coordinatorへ… private unsent draft"

ARGV = [
    "sublane",
    "quarantine",
    "--issue",
    ISSUE,
    "--lane",
    LANE,
    "--journal",
    APPROVAL_JOURNAL,
    "--role",
    ROLE,
    "--assigned-name",
    NAME,
    "--locator",
    OLD_LOCATOR,
    "--action-generation",
    ACTION,
    "--approval-observed-at",
    APPROVED_AT,
    "--approved-revision",
    str(AGENT_REVISION),
]


class _FakeOps:
    """Stands in for the live herdr adapter: a candidate composer, zero actuation."""

    calls: list[str] = []

    def __init__(self, **_kw) -> None:
        pass

    def inspect(self, request) -> QuarantineInspection:
        return QuarantineInspection(
            workspace_id=WS,
            signal=PendingComposerSignal(
                inventory_readable=True,
                has_pending=True,
                agent_state="idle",
                identity_attested=True,
                generation_matches=True,
            ),
            row_revision=AGENT_REVISION,
            attested_at=ATTESTED_AT,
        )

    def close_receiver(self, request, pin):  # pragma: no cover - must never run here
        _FakeOps.calls.append("close")
        raise AssertionError("preflight must not close a receiver")

    def heal_receiver(self, request):  # pragma: no cover - must never run here
        _FakeOps.calls.append("heal")
        raise AssertionError("preflight must not launch a receiver")

    def verify_fresh_receiver(self, request, *, fresh_after):  # pragma: no cover
        raise AssertionError("preflight must not verify a fresh receiver")


class RegistrationTest(unittest.TestCase):
    def test_quarantine_is_registered_under_sublane(self) -> None:
        ns = build_parser().parse_args(ARGV)
        self.assertIs(ns.func, quarantine_module.cmd_sublane_quarantine)
        self.assertEqual(ns.lane, LANE)
        self.assertEqual(ns.locator, OLD_LOCATOR)
        self.assertEqual(ns.action_generation, ACTION)
        self.assertEqual(ns.approved_revision, AGENT_REVISION)

    def test_execute_is_opt_in(self) -> None:
        self.assertFalse(build_parser().parse_args(ARGV).execute)
        self.assertTrue(build_parser().parse_args([*ARGV, "--execute"]).execute)

    def test_every_exact_target_field_is_required(self) -> None:
        # Dropping any one of them would leave the receiver only partially identified,
        # which is precisely what the contract forbids naming.
        for flag in (
            "--issue",
            "--lane",
            "--journal",
            "--role",
            "--assigned-name",
            "--locator",
            "--action-generation",
            "--approval-observed-at",
            "--approved-revision",
        ):
            with self.subTest(flag=flag):
                index = ARGV.index(flag)
                argv = ARGV[:index] + ARGV[index + 2 :]
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        build_parser().parse_args(argv)


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        _FakeOps.calls = []

        self.lifecycle = LaneLifecycleStore(home=self.home)
        self.key = LaneLifecycleKey(WS, LANE)
        self.lifecycle.declare_active(
            self.key,
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id="78011"
            ),
            issue_id=ISSUE,
        )

        home = self.home
        for target, replacement in (
            ("LiveSublaneQuarantineOps", _FakeOps),
            ("LaneReplacementStore", lambda **_kw: LaneReplacementStore(home=home)),
        ):
            patcher = mock.patch.object(quarantine_module, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, argv):
        ns = build_parser().parse_args(argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ns.func(ns)
        return rc, out.getvalue()

    def test_default_run_is_read_only_preflight(self) -> None:
        rc, out = self._run(ARGV)

        self.assertEqual(rc, 0)
        self.assertIn("uncorrelated", out)
        # No process was closed or launched, and the lifecycle row never moved.
        self.assertEqual(_FakeOps.calls, [])
        row = self.lifecycle.get(self.key)
        self.assertEqual(row.replacement_state, REPLACEMENT_NOT_REQUESTED)
        self.assertEqual(row.revision, 1)

    def test_json_payload_reports_classification_not_composer_body(self) -> None:
        rc, out = self._run([*ARGV, "--json"])
        payload = json.loads(out)

        self.assertEqual(rc, 0)
        self.assertEqual(payload["classification"], "uncorrelated")
        self.assertTrue(payload["quarantine_candidate"])
        self.assertFalse(payload["executed"])
        self.assertEqual(payload["action_generation"], ACTION)
        self.assertEqual(payload["replacement_state"], REPLACEMENT_NOT_REQUESTED)
        # Contract 8: the record carries the fixed classification + generation only.
        self.assertNotIn(SECRET_BODY, out)
        self.assertNotIn("body", payload)

    def test_blocked_execution_exits_non_zero(self) -> None:
        # --execute with an approval whose action generation names a different receiver:
        # the command must fail loudly rather than actuate something close enough.
        argv = list(ARGV)
        argv[argv.index("--action-generation") + 1] = quarantine_action_id(
            lane_id=LANE, role=ROLE, locator=f"{WS}:pOTHER"
        )
        rc, out = self._run([*argv, "--execute"])

        self.assertEqual(rc, 1)
        self.assertIn("action generation", out)
        self.assertEqual(_FakeOps.calls, [])
        self.assertEqual(
            self.lifecycle.get(self.key).replacement_state, REPLACEMENT_NOT_REQUESTED
        )


if __name__ == "__main__":
    unittest.main()
