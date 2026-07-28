"""`herdr smoke-shared-space` CLI contract tests (#14187, review j#85841 F3).

The diagnostic promise of this command is that a run leaves *durable evidence* behind
— failure phase, residue counts, endpoint-gate negative proof (issue Acceptance 4).
The first implementation kept that promise only when the run succeeded: a
non-converged ``--execute --json`` called ``die()`` before rendering, so it exited 2
with an empty stdout while stderr told the operator to go and inspect JSON that was
never printed.

Both branches are therefore asserted here, and each success assertion is paired with
its failure counterpart so neither can pass vacuously.  No Herdr, no subprocess, no
network: the actuation function is substituted with an injected report.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    shared_space_smoke_cli as cli,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_shared_space_smoke import (  # noqa: E402,E501
    bounded_process_timeout,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E402,E501
    PHASE_WORKER_ERROR,
    SharedSpaceSmokeError,
)


def _report(*, success: bool) -> dict:
    """A redaction-safe report shaped like the real one, converged or not."""
    return {
        "actuated": True,
        "cross_process": True,
        "success": success,
        "requested_projects": 2,
        "completed_projects": 2 if success else 1,
        "all_projects_completed": success,
        "coordinators_create_count": 1 if success else 0,
        "duplicate_agents": 0,
        "lock_engaged": True,
        "lock_released_clean": True,
        "residue_workspaces": 0 if success else -1,
        "residue_agents": 0 if success else -1,
        "residue_verified": success,
        "cleanup_attempted": True,
        "converged": success,
        "residue_clear": success,
        "projects": [
            {"project_key": "p0", "outcome": "created", "failure_phase": "none",
             "launched_roles": [], "adopted_roles": []},
            {
                "project_key": "p1",
                "outcome": "created" if success else "failed",
                "failure_phase": "none" if success else PHASE_WORKER_ERROR,
                "launched_roles": [],
                "adopted_roles": [],
            },
        ],
        "server_started": True,
        "server_ready": True,
        "endpoint_bound": True,
        "operator_server_connected": False,
        "operator_endpoint_requests": 0,
        "endpoint_escape_refusals": 0,
        "endpoint_gate_dispatched_calls": 6,
        "endpoint_gate_bound_calls": 6,
        "endpoint_gate_processes": 3 if success else 2,
        "endpoint_gate_receipts_expected": 2,
        "endpoint_gate_receipts_missing": 0 if success else 1,
        "endpoint_gate_receipts_complete": success,
        "endpoint_gate_receipts_consistent": True,
        "endpoint_gate_proven_zero_external": success,
        "endpoint_refusal_reasons": [],
        "graceful_stop_refused": False,
        "server_stopped": True,
        "endpoint_residue": 0,
        # -1 on the failing branch: the fork round never completed, so worker residue
        # was never established (distinct from "there were none").
        "worker_processes_orphaned": 0 if success else -1,
    }


class _CliRun:
    """One CLI invocation with captured streams and exit code."""

    def __init__(self, exit_code, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def json(self) -> dict:
        return json.loads(self.stdout)


def _run(report: dict, *, json_mode: bool, execute: bool = True) -> _CliRun:
    args = Namespace(
        isolated_home="/tmp/does-not-need-to-exist",
        projects=2,
        execute=execute,
        process_timeout=1.0,
        json=json_mode,
    )
    out, err = io.StringIO(), io.StringIO()
    exit_code = 0
    with mock.patch.object(
        cli, "run_disposable_shared_space_smoke", lambda *a, **k: dict(report)
    ):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.cmd_herdr_smoke_shared_space(args)
            except SystemExit as exc:  # ``die()`` is the failure signal
                exit_code = exc.code
    return _CliRun(exit_code, out.getvalue(), err.getvalue())


class ExecuteJsonEvidenceTests(unittest.TestCase):
    def test_converged_run_emits_json_and_exits_zero(self) -> None:
        run = _run(_report(success=True), json_mode=True)
        self.assertEqual(run.exit_code, 0)
        self.assertTrue(run.json["success"])
        self.assertTrue(run.json["endpoint_gate_proven_zero_external"])

    def test_non_converged_run_emits_the_SAME_evidence_and_exits_nonzero(self) -> None:
        """The finding itself: failure is signalled by the code, not by silence."""
        run = _run(_report(success=False), json_mode=True)

        self.assertEqual(run.exit_code, 2)
        self.assertNotEqual(run.stdout, "", "the failing branch printed no evidence")
        payload = run.json
        self.assertFalse(payload["success"])
        # The three facts an operator needs to act on a failure, all present.
        self.assertEqual(payload["projects"][1]["failure_phase"], PHASE_WORKER_ERROR)
        self.assertEqual(payload["residue_workspaces"], -1)
        self.assertFalse(payload["endpoint_gate_receipts_complete"])
        self.assertIn("herdr smoke-shared-space failed", run.stderr)

    def test_evidence_keys_do_not_depend_on_the_outcome(self) -> None:
        """A failing run must not be a degraded report — same schema, different values."""
        self.assertEqual(
            sorted(_run(_report(success=True), json_mode=True).json),
            sorted(_run(_report(success=False), json_mode=True).json),
        )


class ExecuteTextEvidenceTests(unittest.TestCase):
    def test_text_mode_names_the_failure_phase_in_closed_tokens(self) -> None:
        run = _run(_report(success=False), json_mode=False)

        self.assertEqual(run.exit_code, 2)
        self.assertIn(f"failure_phases={PHASE_WORKER_ERROR}", run.stdout)
        self.assertIn("completed_projects=1/2", run.stdout)
        self.assertIn("endpoint_gate_receipts_complete=False", run.stdout)
        self.assertIn("endpoint_gate_proven_zero_external=False", run.stdout)
        # "we never established worker residue" must be visible, not implied.
        self.assertIn("orphaned_workers=-1", run.stdout)

    def test_text_mode_reports_no_phase_when_nothing_failed(self) -> None:
        run = _run(_report(success=True), json_mode=False)

        self.assertEqual(run.exit_code, 0)
        self.assertIn("failure_phases=none", run.stdout)
        self.assertIn("completed_projects=2/2", run.stdout)

    def test_no_home_path_reaches_the_text_summary(self) -> None:
        run = _run(_report(success=False), json_mode=False)
        self.assertNotIn("/tmp/does-not-need-to-exist", run.stdout)


class ProcessTimeoutDomainTests(unittest.TestCase):
    """The entry point must refuse a bound the driver cannot honour (j#91604 F2).

    A bare ``type=float`` accepted ``inf`` / ``nan`` / ``0`` / negatives, which only
    failed inside ``Process.join`` — after the whole worker fleet had started, with an
    ``OverflowError`` that no ``except`` in this module catches, so the run lost its
    workers *and* the redacted evidence at the same time.
    """

    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        cli.register_herdr_smoke_shared_space_parser(parser.add_subparsers())
        return parser

    def _parse(self, value: str):
        # ``=`` form: a bare "-inf" would be read as a flag and mask the real behaviour.
        return self._parser().parse_args(
            ["smoke-shared-space", "--isolated-home", "/tmp/iso",
             f"--process-timeout={value}"]
        )

    def test_unusable_timeouts_are_rejected_at_the_entry_point(self) -> None:
        for value in ("inf", "-inf", "nan", "0", "-1", "1e9", "later"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as caught:
                    self._parse(value)
                self.assertEqual(caught.exception.code, 2)

    def test_usable_timeouts_are_accepted(self) -> None:
        """Baseline: the guard must not reject the values the smoke actually uses."""
        for value, expected in (("45", 45.0), ("0.5", 0.5), ("3600", 3600.0)):
            with self.subTest(value=value):
                self.assertEqual(self._parse(value).process_timeout, expected)

    def test_a_huge_int_is_a_typed_domain_error_not_a_raw_overflow(self) -> None:
        """``float(10**10000)`` raises OverflowError, which is not a ValueError.

        Leaving it out of the except clause let a direct ``Namespace`` caller reach a
        raw traceback with zero evidence (review j#91638 F2).
        """
        for value in (10 ** 10000, -(10 ** 10000), "1" + "0" * 5000):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(SharedSpaceSmokeError):
                    bounded_process_timeout(value)

    def test_the_cli_delegates_the_domain_to_the_driver(self) -> None:
        """No second, weaker conversion may sit in front of the driver authority."""
        args = Namespace(
            isolated_home="/tmp/iso", projects=2, execute=True,
            process_timeout=10 ** 10000, json=True,
        )
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.cmd_herdr_smoke_shared_space(args)
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2, "a huge int must be a typed, rendered refusal")
        self.assertIn("herdr smoke-shared-space failed", err.getvalue())

    def test_the_default_is_inside_the_accepted_domain(self) -> None:
        args = self._parser().parse_args(
            ["smoke-shared-space", "--isolated-home", "/tmp/iso"]
        )
        self.assertEqual(
            bounded_process_timeout(args.process_timeout), args.process_timeout
        )


class PreflightBranchTests(unittest.TestCase):
    def test_read_only_preflight_is_unchanged_and_never_dies(self) -> None:
        """Without ``--execute`` there is no success key to fail on."""
        preflight = {
            "isolated_home_ok": True,
            "clean_slate_ok": True,
            "mode": "shared_space",
            "projects": 2,
            "coordinators_create_expected": 1,
            "actuated": False,
        }
        args = Namespace(
            isolated_home="/tmp/iso", projects=2, execute=False,
            process_timeout=1.0, json=True,
        )
        out = io.StringIO()
        with mock.patch.object(
            cli, "smoke_shared_space_preflight", lambda *a, **k: dict(preflight)
        ):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.cmd_herdr_smoke_shared_space(args), 0)
        self.assertFalse(json.loads(out.getvalue())["actuated"])


if __name__ == "__main__":
    unittest.main()
