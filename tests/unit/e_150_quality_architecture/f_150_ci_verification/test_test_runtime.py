"""Test runtime profiling / slow-test budget tests (Redmine #12754).

Covers the pure budget parse (defaults / valid / fail-closed shapes), the
timing->summary classification (slow vs exempt vs violation, slowest ordering,
threshold boundary, stale exceptions), the filesystem ``load_budget`` reader,
and the CLI glue: ``TimingTestResult`` records per-test duration + outcome
without altering pass/fail, and ``cmd_tests_profile`` keeps the suite verdict
authoritative (success -> 0, failure -> 1) while ``--enforce`` only adds a
non-zero exit for a non-exempt slow test. The summarizer is fed synthetic
timings so the classification is exercised without running a real slow suite.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_runtime import (  # noqa: E402
    DEFAULT_SLOW_TEST_THRESHOLD_SECONDS,
    OUTCOME_ERRORED,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
    BudgetException,
    RuntimeBudget,
    TestRuntimeError,
    TestTiming,
    load_budget,
    parse_budget_document,
    summarize,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E402
    commands_test_runtime,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_runtime import (  # noqa: E402
    TimingTestResult,
    cmd_tests_profile,
)


def _t(test_id: str, duration: float, outcome: str = OUTCOME_PASSED) -> TestTiming:
    return TestTiming(test_id=test_id, duration=duration, outcome=outcome)


class ParseBudgetDocumentTest(unittest.TestCase):
    def test_none_resolves_to_defaults(self) -> None:
        budget = parse_budget_document(None, source="x")
        self.assertEqual(
            budget.threshold_seconds, DEFAULT_SLOW_TEST_THRESHOLD_SECONDS
        )
        self.assertEqual(budget.exceptions, ())

    def test_valid_threshold_and_exceptions(self) -> None:
        budget = parse_budget_document(
            {
                "slow_test_threshold_seconds": 2.5,
                "exceptions": [
                    {
                        "test_id": "pkg.test_mod.Case.test_slow",
                        "reason": "integration-style",
                        "owner_issue": 12754,
                    }
                ],
            },
            source="x",
        )
        self.assertEqual(budget.threshold_seconds, 2.5)
        self.assertEqual(len(budget.exceptions), 1)
        entry = budget.exceptions[0]
        self.assertEqual(entry.test_id, "pkg.test_mod.Case.test_slow")
        self.assertEqual(entry.reason, "integration-style")
        self.assertEqual(entry.owner_issue, "12754")  # normalized to str

    def test_non_mapping_top_level_fails_closed(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document([1, 2], source="x")

    def test_bool_threshold_rejected(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document(
                {"slow_test_threshold_seconds": True}, source="x"
            )

    def test_non_positive_threshold_rejected(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document(
                {"slow_test_threshold_seconds": 0}, source="x"
            )

    def test_exceptions_not_list_fails_closed(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document({"exceptions": {"a": 1}}, source="x")

    def test_exception_entry_missing_reason_fails_closed(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document(
                {"exceptions": [{"test_id": "pkg.test"}]}, source="x"
            )

    def test_exception_entry_missing_test_id_fails_closed(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document(
                {"exceptions": [{"reason": "slow"}]}, source="x"
            )

    def test_duplicate_exception_fails_closed(self) -> None:
        with self.assertRaises(TestRuntimeError):
            parse_budget_document(
                {
                    "exceptions": [
                        {"test_id": "a", "reason": "r1"},
                        {"test_id": "a", "reason": "r2"},
                    ]
                },
                source="x",
            )


class RuntimeBudgetTest(unittest.TestCase):
    def test_with_threshold_overrides(self) -> None:
        budget = RuntimeBudget(threshold_seconds=1.0)
        self.assertEqual(budget.with_threshold(0.25).threshold_seconds, 0.25)
        # original unchanged (frozen / replace)
        self.assertEqual(budget.threshold_seconds, 1.0)

    def test_exception_for_lookup(self) -> None:
        budget = RuntimeBudget(
            exceptions=(BudgetException(test_id="a", reason="slow"),)
        )
        self.assertIsNotNone(budget.exception_for("a"))
        self.assertIsNone(budget.exception_for("b"))


class SummarizeTest(unittest.TestCase):
    def test_totals_counts_and_slowest_ordering(self) -> None:
        timings = [
            _t("a", 0.10, OUTCOME_PASSED),
            _t("b", 0.50, OUTCOME_FAILED),
            _t("c", 0.20, OUTCOME_SKIPPED),
            _t("d", 0.05, OUTCOME_ERRORED),
        ]
        summary = summarize(timings, budget=RuntimeBudget(threshold_seconds=1.0))
        self.assertEqual(summary.test_count, 4)
        self.assertAlmostEqual(summary.total_duration, 0.85)
        self.assertEqual(summary.outcome_counts[OUTCOME_PASSED], 1)
        self.assertEqual(summary.outcome_counts[OUTCOME_FAILED], 1)
        self.assertEqual(summary.outcome_counts[OUTCOME_SKIPPED], 1)
        self.assertEqual(summary.outcome_counts[OUTCOME_ERRORED], 1)
        # slowest is duration-descending
        self.assertEqual([t.test_id for t in summary.slowest], ["b", "c", "a", "d"])

    def test_slowest_limit_respected(self) -> None:
        timings = [_t(f"t{i}", float(i)) for i in range(10)]
        summary = summarize(
            timings, budget=RuntimeBudget(threshold_seconds=100.0), slowest=3
        )
        self.assertEqual(len(summary.slowest), 3)
        self.assertEqual([t.test_id for t in summary.slowest], ["t9", "t8", "t7"])

    def test_threshold_boundary_is_inclusive(self) -> None:
        # duration == threshold counts as slow.
        summary = summarize(
            [_t("a", 1.0)], budget=RuntimeBudget(threshold_seconds=1.0)
        )
        self.assertEqual(len(summary.slow_tests), 1)

    def test_violation_vs_exempt_classification(self) -> None:
        budget = RuntimeBudget(
            threshold_seconds=0.5,
            exceptions=(BudgetException(test_id="slow_ok", reason="integration"),),
        )
        timings = [
            _t("fast", 0.10),
            _t("slow_bad", 0.90),
            _t("slow_ok", 0.90),
        ]
        summary = summarize(timings, budget=budget)
        self.assertEqual({s.test_id for s in summary.slow_tests}, {"slow_bad", "slow_ok"})
        self.assertEqual([s.test_id for s in summary.violations], ["slow_bad"])
        self.assertEqual([s.test_id for s in summary.exempt_slow], ["slow_ok"])
        self.assertTrue(summary.has_violations)
        exempt = next(s for s in summary.exempt_slow)
        self.assertEqual(exempt.reason, "integration")

    def test_stale_exception_reported(self) -> None:
        # an exception whose test was not slow this run is stale.
        budget = RuntimeBudget(
            threshold_seconds=0.5,
            exceptions=(BudgetException(test_id="not_slow", reason="r"),),
        )
        summary = summarize([_t("not_slow", 0.10)], budget=budget)
        self.assertEqual(summary.stale_exceptions, ("not_slow",))
        self.assertFalse(summary.has_violations)

    def test_empty_timings(self) -> None:
        summary = summarize([], budget=RuntimeBudget())
        self.assertEqual(summary.test_count, 0)
        self.assertEqual(summary.total_duration, 0)
        self.assertEqual(summary.slow_tests, ())

    def test_as_dict_round_trips(self) -> None:
        budget = RuntimeBudget(threshold_seconds=0.5)
        summary = summarize([_t("a", 0.90)], budget=budget)
        payload = summary.as_dict()
        self.assertEqual(payload["test_count"], 1)
        self.assertEqual(len(payload["violations"]), 1)
        # JSON-serializable
        json.dumps(payload)


class LoadBudgetTest(unittest.TestCase):
    def test_missing_ok_returns_defaults(self) -> None:
        budget = load_budget(Path("/nonexistent/test_runtime_budget.yaml"))
        self.assertEqual(
            budget.threshold_seconds, DEFAULT_SLOW_TEST_THRESHOLD_SECONDS
        )

    def test_missing_not_ok_raises(self) -> None:
        with self.assertRaises(TestRuntimeError):
            load_budget(
                Path("/nonexistent/test_runtime_budget.yaml"), missing_ok=False
            )

    def test_reads_and_parses_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_runtime_budget.yaml"
            path.write_text(
                "slow_test_threshold_seconds: 3.0\n"
                "exceptions:\n"
                "  - test_id: pkg.test_mod.Case.test_x\n"
                "    reason: integration-style\n",
                encoding="utf-8",
            )
            budget = load_budget(path)
            self.assertEqual(budget.threshold_seconds, 3.0)
            self.assertEqual(budget.exceptions[0].test_id, "pkg.test_mod.Case.test_x")

    def test_repo_budget_document_is_valid(self) -> None:
        # The committed repo-root budget must parse (fail-closed contract).
        budget = load_budget(ROOT / "test_runtime_budget.yaml", missing_ok=False)
        self.assertEqual(
            budget.threshold_seconds, DEFAULT_SLOW_TEST_THRESHOLD_SECONDS
        )
        self.assertEqual(budget.exceptions, ())


class TimingTestResultTest(unittest.TestCase):
    def test_records_timing_and_outcome_without_changing_verdict(self) -> None:
        # Defined locally so unittest discovery never collects these synthetic
        # cases (a module-level TestCase with an intentional failure would
        # pollute the real suite).
        class _SampleTests(unittest.TestCase):
            def test_pass(self) -> None:
                self.assertTrue(True)

            def test_fail(self) -> None:
                self.assertTrue(False)

            @unittest.skip("intentional skip")
            def test_skipped(self) -> None:
                pass

        suite = unittest.TestSuite(
            [
                _SampleTests("test_pass"),
                _SampleTests("test_fail"),
                _SampleTests("test_skipped"),
            ]
        )
        runner = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0, resultclass=TimingTestResult
        )
        result = runner.run(suite)
        # verdict unchanged: one failure -> not successful
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.timings), 3)
        by_id = {t.test_id.rsplit(".", 1)[-1]: t for t in result.timings}
        self.assertEqual(by_id["test_pass"].outcome, OUTCOME_PASSED)
        self.assertEqual(by_id["test_fail"].outcome, OUTCOME_FAILED)
        self.assertEqual(by_id["test_skipped"].outcome, OUTCOME_SKIPPED)
        for timing in result.timings:
            self.assertGreaterEqual(timing.duration, 0.0)


class _FakeResult:
    def __init__(self, successful: bool) -> None:
        self._successful = successful

    def wasSuccessful(self) -> bool:
        return self._successful


class CmdTestsProfileTest(unittest.TestCase):
    def _args(self, **overrides: object) -> argparse.Namespace:
        base = {
            "repo": None,
            "budget": None,
            "threshold": 0.5,
            "slowest": 5,
            "enforce": False,
            "format": "text",
            "start_dir": "tests",
            "pattern": "test*.py",
            "top_level_dir": None,
            "failfast": False,
            "verbosity": 0,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, *, timings, successful, **overrides):
        result = _FakeResult(successful)
        with mock.patch.object(
            commands_test_runtime,
            "_run_suite",
            return_value=(result, list(timings)),
        ), mock.patch.object(
            commands_test_runtime,
            "load_budget",
            return_value=RuntimeBudget(threshold_seconds=0.5),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cmd_tests_profile(self._args(**overrides))
            return code, buf.getvalue()

    def test_success_returns_zero_and_prints_summary(self) -> None:
        code, out = self._run(
            timings=[_t("a", 0.10)], successful=True
        )
        self.assertEqual(code, 0)
        self.assertIn("test runtime summary", out)

    def test_failure_returns_one_even_without_enforce(self) -> None:
        code, _ = self._run(timings=[_t("a", 0.10)], successful=False)
        self.assertEqual(code, 1)

    def test_violation_does_not_fail_by_default(self) -> None:
        # slow non-exempt test, but enforce off -> still 0 when suite passes.
        code, out = self._run(timings=[_t("a", 0.90)], successful=True)
        self.assertEqual(code, 0)
        self.assertIn("VIOLATION", out)

    def test_enforce_fails_on_violation(self) -> None:
        code, _ = self._run(
            timings=[_t("a", 0.90)], successful=True, enforce=True
        )
        self.assertEqual(code, 1)

    def test_enforce_passes_when_no_violation(self) -> None:
        code, _ = self._run(
            timings=[_t("a", 0.10)], successful=True, enforce=True
        )
        self.assertEqual(code, 0)

    def test_json_format_emits_summary_and_success(self) -> None:
        code, out = self._run(
            timings=[_t("a", 0.90)], successful=True, format="json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["test_count"], 1)
        self.assertEqual(len(payload["violations"]), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
