"""Command handler for ``mozyo-bridge tests profile`` (Redmine #12754).

Runs ``unittest`` discovery in-process with a timing-collecting result, then
hands the per-test durations to the pure
:mod:`mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_runtime`
summarizer and renders a text / JSON runtime summary usable from both local and
CI lanes.

Reliability is preserved (acceptance #4): discovery uses the same
``TestLoader().discover(start_dir, pattern, top_level_dir)`` mechanics as
``python -m unittest discover -s tests`` — same start dir, pattern, and module
naming — and the process exit code is driven by ``result.wasSuccessful()``.
Profiling only *adds* a summary; it never changes which tests run or their
outcome. Slow tests / budget violations are reported by default and only fail
the lane under the opt-in ``--enforce`` flag, so timing variance never makes a
normal run flaky.

Verbose output is a per-lane knob (acceptance #3): the default lane runs at
unittest verbosity 1 (quiet dots) and relies on the runtime summary for the
slow-test signal; ``-v`` is opt-in for a failure-investigation lane. See
``vibes/docs/logics/test-runtime-profiling-policy.md``.

Repo-root import parity (Redmine #13555): ``python -m unittest discover -s tests``
puts the invocation cwd (the repo root) on ``sys.path[0]``, so repo-root test
packages resolve their ``from tests.support ...`` / ``from tests.unit ...``
imports at collection time. An installed console-script entry point does *not*
put the invocation cwd on ``sys.path``, so the same discovery raised
``ModuleNotFoundError: No module named 'tests'`` for those cross-package imports
across the whole Python matrix. ``_run_suite`` therefore bootstraps the repo root
onto ``sys.path`` for the duration of discovery, matching the ``python -m`` cwd
semantics without changing ``top_level_dir`` (so discovered module names / test
IDs are unchanged — see ``tests-placement-discovery-policy.md``).
"""

from __future__ import annotations

import argparse
import contextlib
import json as _json
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path
from time import perf_counter

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_runtime import (
    DEFAULT_BUDGET_RELPATH,
    OUTCOME_ERRORED,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
    RuntimeBudget,
    RuntimeSummary,
    TestTiming,
    load_budget,
    summarize,
)
from mozyo_bridge.shared.paths import resolve_repo_root


class TimingTestResult(unittest.TextTestResult):
    """A ``TextTestResult`` that records each test's wall-clock duration.

    Timing is purely observational: every ``add*`` simply records the outcome
    and defers to ``super()`` so pass/fail reporting is unchanged. Durations are
    keyed by test object identity (stable across a single start/stop pair), and a
    test that never reached ``startTest`` (e.g. a module import error surfaced
    against a placeholder) is skipped rather than guessed.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.timings: list[TestTiming] = []
        self._starts: dict[int, float] = {}
        self._outcomes: dict[int, str] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._starts[id(test)] = perf_counter()

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._outcomes[id(test)] = OUTCOME_PASSED

    def addError(self, test: unittest.TestCase, err: object) -> None:
        super().addError(test, err)
        self._outcomes[id(test)] = OUTCOME_ERRORED

    def addFailure(self, test: unittest.TestCase, err: object) -> None:
        super().addFailure(test, err)
        self._outcomes[id(test)] = OUTCOME_FAILED

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._outcomes[id(test)] = OUTCOME_SKIPPED

    def addSubTest(
        self, test: unittest.TestCase, subtest: unittest.TestCase, outcome: object
    ) -> None:
        # unittest reports each `with self.subTest(...)` block here, not via
        # add{Success,Failure,Error}. A passing subtest has outcome=None and the
        # parent's addSuccess still fires; but when a subtest FAILS, addSuccess
        # is never called for the parent, so without this override stopTest would
        # fall back to OUTCOME_PASSED and the summary would mis-record a failing
        # test as passed (Redmine #12754 review j#67789). Record the parent's
        # outcome as failed/errored so outcome_counts matches the suite verdict.
        super().addSubTest(test, subtest, outcome)
        if outcome is None:
            return
        if issubclass(outcome[0], test.failureException):
            new_outcome = OUTCOME_FAILED
        else:
            new_outcome = OUTCOME_ERRORED
        # An error is more severe than a failure: never downgrade an already
        # errored parent to failed when a later subtest only fails.
        if self._outcomes.get(id(test)) == OUTCOME_ERRORED:
            return
        self._outcomes[id(test)] = new_outcome

    def addExpectedFailure(self, test: unittest.TestCase, err: object) -> None:
        super().addExpectedFailure(test, err)
        # An expected failure is a passing outcome for the suite.
        self._outcomes[id(test)] = OUTCOME_PASSED

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._outcomes[id(test)] = OUTCOME_FAILED

    def stopTest(self, test: unittest.TestCase) -> None:
        start = self._starts.pop(id(test), None)
        outcome = self._outcomes.pop(id(test), OUTCOME_PASSED)
        if start is not None:
            self.timings.append(
                TestTiming(
                    test_id=test.id(),
                    duration=perf_counter() - start,
                    outcome=outcome,
                )
            )
        super().stopTest(test)


def _repo_root(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def _budget_path(args: argparse.Namespace, repo_root: Path) -> Path:
    explicit = getattr(args, "budget", None)
    if explicit:
        return Path(explicit)
    return repo_root / DEFAULT_BUDGET_RELPATH


def _resolve_budget(args: argparse.Namespace, repo_root: Path) -> RuntimeBudget:
    budget = load_budget(_budget_path(args, repo_root))
    override = getattr(args, "threshold", None)
    if override is not None:
        budget = budget.with_threshold(override)
    return budget


def _verbosity(args: argparse.Namespace) -> int:
    # Default lane = 1 (quiet dots + summary). -v / -q are the per-lane knob.
    value = getattr(args, "verbosity", None)
    return 1 if value is None else int(value)


@contextlib.contextmanager
def _repo_root_importable(repo_root: Path) -> Iterator[None]:
    """Make the repo root importable for the duration of discovery.

    ``python -m unittest discover -s tests`` runs with the invocation cwd (the
    repo root) on ``sys.path[0]``, which is what lets repo-root test packages
    resolve their ``from tests.support ...`` / ``from tests.unit ...`` imports.
    An installed console-script entry point does not put the cwd on ``sys.path``,
    so the same discovery fails to import those cross-package helpers (Redmine
    #13555). Insert the repo root at the front — matching the ``python -m`` cwd
    semantics — only when it is absent, and restore ``sys.path`` afterwards so
    in-process callers (tests, embedded runs) stay isolated. ``top_level_dir`` is
    deliberately left untouched, so this only *enables* the imports; it never
    changes which modules are discovered or their dotted names / test IDs.
    """
    root = str(repo_root)
    if root in sys.path:
        # Local ``python -m`` / editable-install lane already has the repo root
        # (cwd) on the path; nothing to add and nothing to clean up.
        yield
        return
    sys.path.insert(0, root)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(root)


def _run_suite(
    repo_root: Path, args: argparse.Namespace
) -> tuple[TimingTestResult, list[TestTiming]]:
    start_dir = repo_root / getattr(args, "start_dir", "tests")
    if not start_dir.is_dir():
        raise SystemExit(f"test start dir not found: {start_dir}")
    top_level = getattr(args, "top_level_dir", None)
    top_level_dir = str(Path(top_level)) if top_level else None

    loader = unittest.TestLoader()
    with _repo_root_importable(repo_root):
        suite = loader.discover(
            start_dir=str(start_dir),
            pattern=getattr(args, "pattern", "test*.py"),
            top_level_dir=top_level_dir,
        )
        runner = unittest.TextTestRunner(
            stream=sys.stderr,
            verbosity=_verbosity(args),
            failfast=bool(getattr(args, "failfast", False)),
            resultclass=TimingTestResult,
        )
        result = runner.run(suite)
    return result, list(result.timings)


def cmd_tests_profile(args: argparse.Namespace) -> int:
    """Run the suite with timing, print a runtime summary, preserve the verdict."""
    repo_root = _repo_root(args)
    budget = _resolve_budget(args, repo_root)
    result, timings = _run_suite(repo_root, args)

    summary = summarize(
        timings, budget=budget, slowest=int(getattr(args, "slowest", 20))
    )

    fmt = getattr(args, "format", "text")
    success = result.wasSuccessful()
    if fmt == "json":
        payload = summary.as_dict()
        payload["success"] = success
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _render_text(summary)

    if not success:
        # The suite verdict is authoritative; profiling never masks a failure.
        return 1
    if bool(getattr(args, "enforce", False)) and summary.has_violations:
        # Opt-in enforcing lane only.
        return 1
    return 0


_OUTCOME_ORDER = (OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_ERRORED, OUTCOME_SKIPPED)


def _render_text(summary: RuntimeSummary) -> None:
    print("=== test runtime summary ===")
    print(
        f"tests: {summary.test_count}  "
        f"total: {summary.total_duration:.3f}s (sum of per-test wall clock)"
    )
    counts = summary.outcome_counts
    print(
        "outcomes: "
        + "  ".join(f"{name}={counts.get(name, 0)}" for name in _OUTCOME_ORDER)
    )
    print(f"slow-test threshold: {summary.threshold_seconds:g}s")

    if summary.slowest:
        print(f"slowest {len(summary.slowest)}:")
        for timing in summary.slowest:
            print(f"  {timing.duration:8.3f}s  {timing.test_id}")

    print(
        f"slow tests (>= {summary.threshold_seconds:g}s): {len(summary.slow_tests)}  "
        f"(violations: {len(summary.violations)}, exempt: {len(summary.exempt_slow)})"
    )
    for slow in summary.slow_tests:
        tag = "exempt    " if slow.exempt else "VIOLATION "
        suffix = f"  -- {slow.reason}" if slow.reason else ""
        print(f"  [{tag}] {slow.duration:8.3f}s  {slow.test_id}{suffix}")

    if summary.stale_exceptions:
        print("stale budget exceptions (not slow this run; consider removing):")
        for test_id in summary.stale_exceptions:
            print(f"  - {test_id}")


__all__ = ("TimingTestResult", "cmd_tests_profile", "_repo_root_importable")
