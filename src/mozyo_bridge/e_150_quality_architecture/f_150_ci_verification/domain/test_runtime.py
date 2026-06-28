"""Test runtime profiling and slow-test budget (Redmine #12754).

Records the *measured* runtime of the test suite so a slow CI run can be split
into its causes (test count, verbose output, integration-style mock tests,
broad implementer target selection) after the fact instead of guessing. The
companion :mod:`...application.commands_test_runtime` runs ``unittest``
discovery with a timing-collecting result and feeds the per-test durations here;
this module is pure (no I/O except :func:`load_budget`, the single YAML read) so
the slow/exempt classification is unit-testable against synthetic timings.

Two things this module owns:

- **slow-test threshold + exception record location.** A test slower than
  ``slow_test_threshold_seconds`` is *slow*. Slowness alone is not a failure:
  some integration-style tests are legitimately slow, so they are recorded as
  **exceptions** in ``test_runtime_budget.yaml`` at the repo root (the budget
  document) with a reason. :func:`summarize` marks each slow test ``exempt`` or
  a ``violation`` accordingly. See ``vibes/docs/logics/test-runtime-profiling-policy.md``.
- **reliability is not reduced.** Profiling never changes a test's outcome.
  :func:`summarize` only *reports*; whether a slow test or a budget violation
  should fail a lane is an opt-in caller decision (the CLI ``--enforce`` flag),
  kept off by default so normal CI is never made flaky by timing variance.

The budget document is intentionally fail-closed on a malformed shape (a broken
budget must not silently disable the slow-test signal), but a *missing* file
resolves to the built-in default threshold with no exceptions, so the profiler
runs on a repo that has not authored a budget yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

# PyLint has no analogue here; the default is a deliberately small unit-test
# budget. A unit test slower than one second is worth surfacing; genuinely
# slow integration-style tests are recorded as exceptions in the budget
# document rather than by raising the global threshold (which would hide every
# regression under it). Override per-repo via ``slow_test_threshold_seconds``
# in ``test_runtime_budget.yaml`` or per-run via the CLI ``--threshold`` flag.
DEFAULT_SLOW_TEST_THRESHOLD_SECONDS = 1.0

# Repo-root budget document (slow-test threshold + exception record location).
DEFAULT_BUDGET_RELPATH = "test_runtime_budget.yaml"

# Per-test outcomes (mirrors unittest result buckets). Profiling records the
# outcome alongside the duration but never alters it.
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_ERRORED = "errored"
OUTCOME_SKIPPED = "skipped"
OUTCOMES = frozenset(
    {OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_ERRORED, OUTCOME_SKIPPED}
)


class TestRuntimeError(ValueError):
    """Raised when the budget document is unreadable or malformed (fail closed)."""


@dataclass(frozen=True)
class TestTiming:
    """One test's measured runtime and (unaltered) outcome.

    ``test_id`` is the unittest dotted id (``unit.<context>.test_mod.Case.test``);
    ``duration`` is wall-clock seconds for that single test.
    """

    test_id: str
    duration: float
    outcome: str = OUTCOME_PASSED

    def as_dict(self) -> dict[str, object]:
        return {
            "test_id": self.test_id,
            "duration": round(self.duration, 6),
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class BudgetException:
    """A test allowed to exceed the threshold, with a recorded justification."""

    test_id: str
    reason: str
    owner_issue: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "test_id": self.test_id,
            "reason": self.reason,
            "owner_issue": self.owner_issue,
        }


@dataclass(frozen=True)
class RuntimeBudget:
    """Parsed ``test_runtime_budget.yaml``: threshold + exception allowlist."""

    threshold_seconds: float = DEFAULT_SLOW_TEST_THRESHOLD_SECONDS
    exceptions: tuple[BudgetException, ...] = ()

    def exception_for(self, test_id: str) -> BudgetException | None:
        for entry in self.exceptions:
            if entry.test_id == test_id:
                return entry
        return None

    def with_threshold(self, threshold_seconds: float) -> "RuntimeBudget":
        """Return a copy with an overridden threshold (CLI ``--threshold``)."""
        return replace(self, threshold_seconds=float(threshold_seconds))


@dataclass(frozen=True)
class SlowTest:
    """A test at or over the threshold, classified against the budget.

    ``exempt`` is true when the test is recorded in the budget's exceptions; the
    ``reason`` then carries the recorded justification. A non-exempt slow test is
    a ``violation`` — surfaced, and able to fail an opt-in enforcing lane, but
    never failing the normal profiling run.
    """

    test_id: str
    duration: float
    threshold_seconds: float
    exempt: bool
    reason: str | None = None

    @property
    def is_violation(self) -> bool:
        return not self.exempt

    def as_dict(self) -> dict[str, object]:
        return {
            "test_id": self.test_id,
            "duration": round(self.duration, 6),
            "threshold_seconds": self.threshold_seconds,
            "exempt": self.exempt,
            "is_violation": self.is_violation,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RuntimeSummary:
    """Aggregate runtime profile of one test run.

    ``slowest`` is the top-N timings by duration for human review; ``slow_tests``
    is every test at/over the threshold with its budget classification.
    ``stale_exceptions`` are budget entries that were not slow in this run (an
    exception that no longer earns its keep — the inverse of a missing one).
    """

    total_duration: float
    test_count: int
    threshold_seconds: float
    slowest: tuple[TestTiming, ...]
    slow_tests: tuple[SlowTest, ...]
    outcome_counts: dict[str, int]
    stale_exceptions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def violations(self) -> tuple[SlowTest, ...]:
        return tuple(s for s in self.slow_tests if s.is_violation)

    @property
    def exempt_slow(self) -> tuple[SlowTest, ...]:
        return tuple(s for s in self.slow_tests if s.exempt)

    @property
    def has_violations(self) -> bool:
        return any(s.is_violation for s in self.slow_tests)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_duration": round(self.total_duration, 6),
            "test_count": self.test_count,
            "threshold_seconds": self.threshold_seconds,
            "outcome_counts": dict(self.outcome_counts),
            "slowest": [t.as_dict() for t in self.slowest],
            "slow_tests": [s.as_dict() for s in self.slow_tests],
            "violations": [s.as_dict() for s in self.violations],
            "stale_exceptions": list(self.stale_exceptions),
        }


def summarize(
    timings: list[TestTiming] | tuple[TestTiming, ...],
    *,
    budget: RuntimeBudget,
    slowest: int = 20,
) -> RuntimeSummary:
    """Build a :class:`RuntimeSummary` from per-test ``timings`` (pure; no I/O).

    A test is *slow* when its duration is at or over ``budget.threshold_seconds``
    and is then classified ``exempt`` (recorded in the budget) or a violation.
    The summary only reports — it never decides a test's pass/fail (see the
    module docstring on not reducing reliability).
    """
    timings = tuple(timings)
    threshold = budget.threshold_seconds

    total = sum(t.duration for t in timings)
    counts: dict[str, int] = {name: 0 for name in sorted(OUTCOMES)}
    for timing in timings:
        counts[timing.outcome] = counts.get(timing.outcome, 0) + 1

    ordered = sorted(timings, key=lambda t: t.duration, reverse=True)
    slowest_n = max(0, int(slowest))
    top = tuple(ordered[:slowest_n]) if slowest_n else ()

    slow: list[SlowTest] = []
    slow_ids: set[str] = set()
    for timing in ordered:
        if timing.duration < threshold:
            continue
        slow_ids.add(timing.test_id)
        entry = budget.exception_for(timing.test_id)
        slow.append(
            SlowTest(
                test_id=timing.test_id,
                duration=timing.duration,
                threshold_seconds=threshold,
                exempt=entry is not None,
                reason=entry.reason if entry is not None else None,
            )
        )

    stale = tuple(
        entry.test_id for entry in budget.exceptions if entry.test_id not in slow_ids
    )

    return RuntimeSummary(
        total_duration=total,
        test_count=len(timings),
        threshold_seconds=threshold,
        slowest=top,
        slow_tests=tuple(slow),
        outcome_counts=counts,
        stale_exceptions=stale,
    )


def parse_budget_document(raw: object, *, source: str) -> RuntimeBudget:
    """Parse a loaded budget document into a :class:`RuntimeBudget` (fail closed).

    ``None`` / empty resolves to defaults. A non-mapping document, a wrong-typed
    threshold, or an exception entry missing ``test_id`` / ``reason`` raises
    :class:`TestRuntimeError` — a broken budget must never silently weaken the
    slow-test signal.
    """
    if raw is None:
        return RuntimeBudget()
    if not isinstance(raw, dict):
        raise TestRuntimeError(f"{source}: top level must be a mapping")

    threshold = raw.get(
        "slow_test_threshold_seconds", DEFAULT_SLOW_TEST_THRESHOLD_SECONDS
    )
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise TestRuntimeError(
            f"{source}: slow_test_threshold_seconds must be a number"
        )
    if threshold <= 0:
        raise TestRuntimeError(
            f"{source}: slow_test_threshold_seconds must be positive"
        )

    raw_exceptions = raw.get("exceptions", [])
    if not isinstance(raw_exceptions, list):
        raise TestRuntimeError(f"{source}: exceptions must be a list")

    entries: list[BudgetException] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_exceptions):
        if not isinstance(item, dict):
            raise TestRuntimeError(f"{source}: exceptions[{index}] must be a mapping")
        test_id = item.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            raise TestRuntimeError(
                f"{source}: exceptions[{index}].test_id must be a non-empty string"
            )
        test_id = test_id.strip()
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise TestRuntimeError(
                f"{source}: exceptions[{index}].reason must be a non-empty string "
                f"(record why '{test_id}' is allowed to be slow)"
            )
        if test_id in seen:
            raise TestRuntimeError(
                f"{source}: duplicate exception entry for {test_id}"
            )
        seen.add(test_id)
        owner_issue = item.get("owner_issue")
        if owner_issue is not None and not isinstance(owner_issue, (str, int)):
            raise TestRuntimeError(
                f"{source}: exceptions[{index}].owner_issue must be a string or int"
            )
        entries.append(
            BudgetException(
                test_id=test_id,
                reason=reason.strip(),
                owner_issue=str(owner_issue) if owner_issue is not None else None,
            )
        )

    return RuntimeBudget(threshold_seconds=float(threshold), exceptions=tuple(entries))


def load_budget(budget_path: Path | str, *, missing_ok: bool = True) -> RuntimeBudget:
    """Read and parse the budget document (the single filesystem read here).

    A *missing* file resolves to the default budget when ``missing_ok`` (the
    profiler runs before a budget is authored); pass ``missing_ok=False`` to
    require it. An unreadable / malformed file fails closed.
    """
    path = Path(budget_path)
    if not path.exists():
        if missing_ok:
            return RuntimeBudget()
        raise TestRuntimeError(f"test-runtime budget not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TestRuntimeError(f"cannot read {path}: {exc}") from exc
    return parse_budget_document(raw, source=str(path))


__all__ = (
    "DEFAULT_SLOW_TEST_THRESHOLD_SECONDS",
    "DEFAULT_BUDGET_RELPATH",
    "OUTCOME_PASSED",
    "OUTCOME_FAILED",
    "OUTCOME_ERRORED",
    "OUTCOME_SKIPPED",
    "OUTCOMES",
    "TestRuntimeError",
    "TestTiming",
    "BudgetException",
    "RuntimeBudget",
    "SlowTest",
    "RuntimeSummary",
    "summarize",
    "parse_budget_document",
    "load_budget",
)
