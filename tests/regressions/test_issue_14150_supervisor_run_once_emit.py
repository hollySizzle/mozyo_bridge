"""Redmine #14150 j#85115 — `workflow supervisor --run-once` must reach a terminal outcome.

Post-integration dogfood on exact-installed ``0.12.3a1``::

    workflow supervisor --run-once --local-wake --wake <ws>:14097 ... --json
    AttributeError: 'WorkspaceSupervisionOutcome' object has no attribute 'provider_reads'

Two independent defects produced that crash, and both are pinned here:

1. **Stale field.** The R3 split renamed the per-workspace counter to ``provider_calls``; one
   text row still read ``w.provider_reads``. The scheduled LaunchAgent ``--run-once`` reconcile
   runs this same path, so the 5-minute pass could never report an outcome.

2. **Eager text formatting.** Every command built its human text rows BEFORE ``_emit`` and
   passed the finished list, so a defect confined to the *text* formatter also killed ``--json``.
   That is why a cosmetic row could take down the machine-readable contract the service depends
   on. ``_emit`` now accepts a builder and never evaluates it in JSON mode.

Defect 1 is pinned with the REAL frozen :class:`WorkspaceSupervisionOutcome` rather than a fake:
a stand-in object carrying arbitrary attributes would have happily answered ``provider_reads``
and hidden exactly this drift.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_supervisor as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    IssueSupervisionOutcome,
    SupervisorReport,
    WorkspaceSupervisionOutcome,
)


def _run(argv) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


def _report(**kw) -> SupervisorReport:
    """A report carrying one leased workspace, built from the REAL canonical dataclasses.

    ``events_supplied`` / ``delivered`` / ``blocked`` / ``deferred`` are derived properties over
    the per-issue outcomes, so they are produced the same way the real fold produces them rather
    than injected — a stand-in that let them be set directly would not exercise the contract.
    """
    issues = (
        IssueSupervisionOutcome(issue="14097", events_supplied=2, delivered=1, deferred=1),
        IssueSupervisionOutcome(issue="13490", events_supplied=1, blocked=1),
    )
    outcome = WorkspaceSupervisionOutcome(
        workspace_id="ws1",
        lease_acquired=True,
        lease_reason="",
        supervised_issues=("14097", "13490"),
        issues=issues,
        provider_calls=7,
        **kw,
    )
    return SupervisorReport(mode="local_wake", holder="h:1", workspaces=(outcome,), duration_ms=12)


class RunOnceTextRowTest(unittest.TestCase):
    """Defect 1: the text row must read the canonical field off the real contract."""

    def test_text_rows_build_against_the_real_outcome_contract(self):
        # This is the exact call that raised AttributeError in the dogfood run.
        lines = cli._run_once_text_lines(_report(), "run-once")
        joined = "\n".join(lines)
        self.assertIn("provider_calls: 7", joined)  # roll-up
        self.assertIn("provider_calls=7", joined)  # per-workspace row
        self.assertNotIn("provider_reads", joined)

    def test_the_stale_attribute_genuinely_does_not_exist(self):
        # Pins WHY the crash happened: the canonical contract has no such attribute, so any
        # future re-introduction of the old name fails here rather than in a scheduled service.
        outcome = _report().workspaces[0]
        self.assertFalse(hasattr(outcome, "provider_reads"))
        self.assertEqual(outcome.provider_calls, 7)

    def test_skipped_workspace_row_renders(self):
        skipped = WorkspaceSupervisionOutcome(
            workspace_id="ws2", lease_acquired=False, lease_reason="held", skipped_reason="lease_held"
        )
        report = SupervisorReport(mode="local_wake", holder="h:1", workspaces=(skipped,))
        lines = cli._run_once_text_lines(report, "run-once")
        self.assertIn("  ws ws2: skipped (lease_held)", lines)

    def test_drain_label_is_used_for_a_drain_pass(self):
        self.assertIn("action: drain", cli._run_once_text_lines(_report(), "drain"))


class JsonSurvivesTextFormatterDriftTest(unittest.TestCase):
    """Defect 2: a text-only formatter fault must never take down ``--json``."""

    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))

    def _run_once(self, *, as_json: bool, formatter_raises: bool) -> tuple[int, str]:
        report = _report()

        class _Supervisor:
            def run_once(self, *, mode, wake_hints):  # noqa: ARG002 - signature match only
                return report

        argv = ["workflow", "supervisor", "--run-once", "--local-wake", "--home", self.home]
        if as_json:
            argv.append("--json")

        boom = lambda *a, **k: (_ for _ in ()).throw(AttributeError("text formatter drift"))
        ctx = (
            patch.object(cli, "_run_once_text_lines", side_effect=boom)
            if formatter_raises
            else contextlib.nullcontext()
        )
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
            ".application.workspace_callback_supervisor.build_supervisor",
            return_value=_Supervisor(),
        ), ctx:
            return _run(argv)

    def test_json_run_once_reaches_a_terminal_outcome(self):
        rc, out = self._run_once(as_json=True, formatter_raises=False)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "local_wake")
        self.assertEqual(payload["provider_calls"], 7)

    def test_json_survives_a_raising_text_formatter(self):
        # The structural guarantee: JSON mode never builds text rows, so drift confined to the
        # text formatter cannot kill the machine-readable contract the LaunchAgent consumes.
        rc, out = self._run_once(as_json=True, formatter_raises=True)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["provider_calls"], 7)

    def test_text_mode_still_surfaces_a_real_formatter_fault(self):
        # The guard must not swallow the fault where the text IS the output — otherwise a broken
        # text row would silently print nothing instead of failing loudly.
        with self.assertRaises(AttributeError):
            self._run_once(as_json=False, formatter_raises=True)

    def test_text_run_once_renders_the_canonical_counter(self):
        rc, out = self._run_once(as_json=False, formatter_raises=False)
        self.assertEqual(rc, 0)
        self.assertIn("provider_calls=7", out)


class EmitContractTest(unittest.TestCase):
    """``_emit`` accepts both a sequence and a builder (back-compatible)."""

    def _emit(self, *, as_json: bool, text_lines) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._emit({"a": 1}, as_json=as_json, text_lines=text_lines)
        return buf.getvalue()

    def test_sequence_still_supported(self):
        self.assertEqual(self._emit(as_json=False, text_lines=["x", "y"]), "x\ny\n")

    def test_builder_supported(self):
        self.assertEqual(self._emit(as_json=False, text_lines=lambda: ["x"]), "x\n")

    def test_json_never_calls_the_builder(self):
        calls = []

        def _builder():
            calls.append(1)
            return ["never"]

        out = self._emit(as_json=True, text_lines=_builder)
        self.assertEqual(json.loads(out), {"a": 1})
        self.assertEqual(calls, [])


class NonRegressionTest(unittest.TestCase):
    """Requirement 3: ``--drain-only`` / ``--status`` / service contract unaffected."""

    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))

    def test_drain_only_emits_terminal_outcome(self):
        report = SupervisorReport(mode="local_drain", holder="h:1", workspaces=())

        class _Supervisor:
            def run_once(self, *, mode, wake_hints):
                assert wake_hints == (), "a drain must not carry wake hints"
                return report

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
            ".application.workspace_callback_supervisor.build_supervisor",
            return_value=_Supervisor(),
        ):
            rc, out = _run(
                ["workflow", "supervisor", "--drain-only", "--home", self.home, "--json"]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "local_drain")
        self.assertEqual(payload["provider_calls"], 0)  # a drain reads no provider

        rc, out = _run(["workflow", "supervisor", "--drain-only", "--home", self.home])
        self.assertIn("action: drain", out)

    def test_status_json_unaffected(self):
        rc, out = _run(["workflow", "supervisor", "--status", "--home", self.home, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["action"], "status")

    def test_status_text_unaffected(self):
        rc, out = _run(["workflow", "supervisor", "--status", "--home", self.home])
        self.assertEqual(rc, 0)
        self.assertIn("action: status", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
