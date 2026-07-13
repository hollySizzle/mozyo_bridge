"""Deterministic shard planning + fail-closed aggregate verdict for the local
parallel test runner (Redmine #13733).

This module is **pure**: no subprocess, no unittest discovery, no environment,
no filesystem except the single YAML read in :func:`load_policy`. The handler
(:mod:`...application.commands_test_parallel`) owns discovery, process spawning,
and env isolation; it feeds this module the discovered ``module -> test-id`` map
and the raw per-shard results, and this module decides two things:

- **how modules partition into shards** — a deterministic longest-processing-time
  (LPT) bin-packing over per-module weights (measured durations when a manifest
  is supplied, else the module's discovered test count), with any module matched
  by the serial-bucket patterns pulled out into a single non-concurrent serial
  shard. Determinism matters: the same discovered suite + same jobs + same
  weights always yields the same plan, so a shard failure is reproducible via the
  emitted replay command.

- **whether the aggregate is green** — and it is green *only* when every shard
  succeeded **and** the union of the test ids the shards actually ran exactly
  equals the discovered set (parity). A shard that silently dropped a module, a
  worker that crashed before emitting its result, a timeout, or a collection-time
  import error can therefore never be laundered into an aggregate green
  (acceptance: "shard failure を aggregate green にしない").

The companion policy document is
``vibes/docs/logics/local-parallel-test-runner-policy.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

# Host capacity is the default parallelism ceiling (acceptance: 既定並列度は host
# capacity を上限にし ``--jobs`` で制御). The handler resolves the concrete host
# CPU count; this constant is only the floor when nothing else is known.
MIN_JOBS = 1

# Repo-root policy document (serial-bucket patterns + default jobs / timeout).
DEFAULT_POLICY_RELPATH = "test_parallel_policy.yaml"

# Per-shard status vocabulary. ``passed`` is the only green status; every other
# status is a fail-closed terminal state that forces the aggregate red.
SHARD_PASSED = "passed"
SHARD_FAILED = "failed"          # ran, but a test failed / errored (verdict red)
SHARD_TIMEOUT = "timeout"        # killed after exceeding the shard timeout
SHARD_CRASHED = "crashed"        # worker died / emitted no parseable result
SHARD_STATUSES = frozenset(
    {SHARD_PASSED, SHARD_FAILED, SHARD_TIMEOUT, SHARD_CRASHED}
)

# Shard kinds. Serial-bucket modules run in a dedicated shard that the handler
# executes on its own (never concurrently with the parallel shards).
KIND_PARALLEL = "parallel"
KIND_SERIAL = "serial"


class TestParallelError(ValueError):
    """Raised on a malformed policy document or an impossible plan (fail closed)."""


def matches_any(module: str, patterns: tuple[str, ...]) -> bool:
    """True when ``module`` (a discovered dotted module name) matches a pattern.

    Patterns are case-sensitive :mod:`fnmatch` globs over the dotted module name
    as unittest discovery reports it (e.g. ``unit.e_120_operations_cockpit.*``
    or an exact ``unit.e_150_quality_architecture.test_doctor_tmux``). A bare
    prefix without a wildcard matches only that exact module; author ``prefix.*``
    to capture a subtree.
    """
    return any(fnmatchcase(module, pattern) for pattern in patterns)


@dataclass(frozen=True)
class ParallelPolicy:
    """Parsed ``test_parallel_policy.yaml``.

    ``serial_modules`` are fnmatch globs; any discovered module matching one is
    routed to the serial bucket. ``default_jobs`` / ``shard_timeout_seconds`` are
    optional defaults the CLI flags override. A *missing* policy document
    resolves to this empty default (the runner works before a policy is authored)
    but a *malformed* one fails closed — a broken policy must not silently drop
    the serial bucket or the timeout guard.
    """

    serial_modules: tuple[str, ...] = ()
    default_jobs: int | None = None
    shard_timeout_seconds: float | None = None

    def is_serial(self, module: str) -> bool:
        return matches_any(module, self.serial_modules)


@dataclass(frozen=True)
class Shard:
    """One unit of parallel work: a set of modules run in one isolated process.

    ``expected_test_ids`` is the exact set of unittest ids the shard is planned to
    run (the union of its modules' discovered ids), sorted for determinism. The
    aggregate compares this against what the shard *actually* ran.
    """

    index: int
    kind: str
    modules: tuple[str, ...]
    expected_test_ids: tuple[str, ...]
    weight: float

    @property
    def expected_count(self) -> int:
        return len(self.expected_test_ids)


@dataclass(frozen=True)
class ShardPlan:
    """A deterministic partition of the discovered suite into shards."""

    shards: tuple[Shard, ...]
    jobs: int
    weight_basis: str  # "durations" | "test_count"
    total_modules: int
    total_expected_tests: int

    @property
    def parallel_shards(self) -> tuple[Shard, ...]:
        return tuple(s for s in self.shards if s.kind == KIND_PARALLEL)

    @property
    def serial_shards(self) -> tuple[Shard, ...]:
        return tuple(s for s in self.shards if s.kind == KIND_SERIAL)

    @property
    def expected_test_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        for shard in self.shards:
            ids.update(shard.expected_test_ids)
        return frozenset(ids)


@dataclass(frozen=True)
class ShardResult:
    """The observed outcome of running one shard (built by the handler).

    ``ran_test_ids`` are the ids the shard reported running; ``counts`` mirrors
    unittest outcome buckets. ``status`` is the fail-closed classification and
    ``detail`` carries a short human reason for a non-passed status.
    """

    index: int
    kind: str
    status: str
    ran_test_ids: tuple[str, ...]
    counts: dict[str, int]
    returncode: int | None = None
    detail: str | None = None
    replay_command: str = ""
    duration_seconds: float | None = None
    failed_test_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == SHARD_PASSED


@dataclass(frozen=True)
class AggregateVerdict:
    """The fail-closed aggregate of every shard against the discovered suite.

    ``success`` is true only when every shard passed and parity holds
    (``missing_test_ids`` and ``unexpected_test_ids`` are both empty). ``reasons``
    lists every distinct cause of a red verdict for the human summary.
    """

    success: bool
    total_expected_tests: int
    total_ran_tests: int
    counts: dict[str, int]
    failed_shards: tuple[int, ...]
    missing_test_ids: tuple[str, ...]
    unexpected_test_ids: tuple[str, ...]
    reasons: tuple[str, ...] = field(default_factory=tuple)
    failed_test_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "total_expected_tests": self.total_expected_tests,
            "total_ran_tests": self.total_ran_tests,
            "counts": dict(self.counts),
            "failed_shards": list(self.failed_shards),
            "failed_test_ids": list(self.failed_test_ids),
            "missing_test_ids": list(self.missing_test_ids),
            "unexpected_test_ids": list(self.unexpected_test_ids),
            "reasons": list(self.reasons),
        }


def _weight_for(module: str, test_ids: tuple[str, ...], weights: dict[str, float] | None) -> float:
    """Per-module planning weight.

    Prefer a measured duration from the manifest; fall back to the discovered
    test count for any module the manifest does not cover. The fallback keeps the
    plan deterministic and total even with a partial or absent manifest — the
    module is never dropped, only weighted more coarsely.
    """
    if weights is not None:
        measured = weights.get(module)
        if measured is not None and measured > 0:
            return float(measured)
    # Test count is the coarse deterministic fallback. An empty module still
    # carries a tiny non-zero weight so LPT keeps its placement stable.
    return float(len(test_ids)) or 0.001


def plan_shards(
    module_tests: dict[str, tuple[str, ...]],
    *,
    jobs: int,
    policy: ParallelPolicy,
    weights: dict[str, float] | None = None,
) -> ShardPlan:
    """Partition discovered modules into a deterministic set of shards.

    ``module_tests`` maps each discovered dotted module name to its tuple of
    unittest ids (from the authoritative :func:`unittest.TestLoader.discover`
    pass). Modules matched by ``policy.serial_modules`` go to a single serial
    shard; the rest are LPT bin-packed into ``min(jobs, n_parallel_modules)``
    parallel shards. Raises :class:`TestParallelError` if ``jobs`` < 1.
    """
    if jobs < MIN_JOBS:
        raise TestParallelError(f"jobs must be >= {MIN_JOBS}, got {jobs}")

    weight_basis = "durations" if weights else "test_count"

    serial_modules: list[str] = []
    parallel_modules: list[str] = []
    for module in sorted(module_tests):
        if policy.is_serial(module):
            serial_modules.append(module)
        else:
            parallel_modules.append(module)

    def shard_ids(modules: list[str]) -> tuple[str, ...]:
        ids: list[str] = []
        for module in modules:
            ids.extend(module_tests[module])
        return tuple(sorted(ids))

    def shard_weight(modules: list[str]) -> float:
        return sum(_weight_for(m, module_tests[m], weights) for m in modules)

    # LPT: assign heaviest modules first, each to the currently-lightest bin.
    # Ties break on module name (the sort below) and then lowest bin index, so
    # the partition is fully determined by the inputs.
    bin_count = max(1, min(jobs, len(parallel_modules))) if parallel_modules else 0
    bins: list[list[str]] = [[] for _ in range(bin_count)]
    bin_load = [0.0] * bin_count
    ordered = sorted(
        parallel_modules,
        key=lambda m: (-_weight_for(m, module_tests[m], weights), m),
    )
    for module in ordered:
        target = min(range(bin_count), key=lambda i: (bin_load[i], i))
        bins[target].append(module)
        bin_load[target] += _weight_for(module, module_tests[module], weights)

    shards: list[Shard] = []
    for index, modules in enumerate(bins):
        modules_sorted = sorted(modules)
        shards.append(
            Shard(
                index=index,
                kind=KIND_PARALLEL,
                modules=tuple(modules_sorted),
                expected_test_ids=shard_ids(modules_sorted),
                weight=shard_weight(modules_sorted),
            )
        )

    if serial_modules:
        shards.append(
            Shard(
                index=len(shards),
                kind=KIND_SERIAL,
                modules=tuple(serial_modules),
                expected_test_ids=shard_ids(serial_modules),
                weight=shard_weight(serial_modules),
            )
        )

    total_expected = sum(len(ids) for ids in module_tests.values())
    return ShardPlan(
        shards=tuple(shards),
        jobs=jobs,
        weight_basis=weight_basis,
        total_modules=len(module_tests),
        total_expected_tests=total_expected,
    )


def aggregate(plan: ShardPlan, results: list[ShardResult] | tuple[ShardResult, ...]) -> AggregateVerdict:
    """Fold shard results into a fail-closed aggregate verdict (pure).

    Green requires all three: every shard ``ok``; no missing ids (a planned test
    that no shard ran — a dropped shard or a worker that died mid-run); and no
    unexpected ids (a shard ran a test outside its plan). Any deviation yields a
    red verdict with an explicit reason. Outcome counts are summed only over
    shards that actually reported, but a missing report is itself a red reason —
    so under-counting can never read as green.
    """
    results = tuple(results)
    counts: dict[str, int] = {}
    ran_ids: set[str] = set()
    failed: list[int] = []
    failed_ids: list[str] = []
    reasons: list[str] = []

    for result in results:
        for name, value in result.counts.items():
            counts[name] = counts.get(name, 0) + int(value)
        ran_ids.update(result.ran_test_ids)
        failed_ids.extend(result.failed_test_ids)
        if not result.ok:
            failed.append(result.index)
            reason = result.detail or result.status
            reasons.append(f"shard {result.index} ({result.kind}): {reason}")

    expected = plan.expected_test_ids
    missing = tuple(sorted(expected - ran_ids))
    unexpected = tuple(sorted(ran_ids - expected))

    if missing:
        reasons.append(
            f"parity: {len(missing)} discovered test(s) were not run by any shard"
        )
    if unexpected:
        reasons.append(
            f"parity: {len(unexpected)} test(s) ran outside the discovered set"
        )
    if len(results) != len(plan.shards):
        reasons.append(
            f"shard count mismatch: planned {len(plan.shards)}, observed {len(results)}"
        )

    success = not failed and not missing and not unexpected and len(results) == len(plan.shards)

    return AggregateVerdict(
        success=success,
        total_expected_tests=plan.total_expected_tests,
        total_ran_tests=len(ran_ids),
        counts=counts,
        failed_shards=tuple(failed),
        missing_test_ids=missing,
        unexpected_test_ids=unexpected,
        reasons=tuple(reasons),
        failed_test_ids=tuple(sorted(failed_ids)),
    )


def parse_policy_document(raw: object, *, source: str) -> ParallelPolicy:
    """Parse a loaded policy document into a :class:`ParallelPolicy` (fail closed).

    ``None`` / empty resolves to the empty default. A non-mapping document, a
    non-list ``serial_modules``, a non-string pattern, a non-positive
    ``default_jobs``, or a non-positive ``shard_timeout_seconds`` raises
    :class:`TestParallelError`.
    """
    if raw is None:
        return ParallelPolicy()
    if not isinstance(raw, dict):
        raise TestParallelError(f"{source}: top level must be a mapping")

    raw_serial = raw.get("serial_modules", [])
    if not isinstance(raw_serial, list):
        raise TestParallelError(f"{source}: serial_modules must be a list")
    patterns: list[str] = []
    for index, item in enumerate(raw_serial):
        if not isinstance(item, str) or not item.strip():
            raise TestParallelError(
                f"{source}: serial_modules[{index}] must be a non-empty string"
            )
        patterns.append(item.strip())

    default_jobs = raw.get("default_jobs")
    if default_jobs is not None:
        if isinstance(default_jobs, bool) or not isinstance(default_jobs, int):
            raise TestParallelError(f"{source}: default_jobs must be an integer")
        if default_jobs < MIN_JOBS:
            raise TestParallelError(f"{source}: default_jobs must be >= {MIN_JOBS}")

    timeout = raw.get("shard_timeout_seconds")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TestParallelError(
                f"{source}: shard_timeout_seconds must be a number"
            )
        if timeout <= 0:
            raise TestParallelError(
                f"{source}: shard_timeout_seconds must be positive"
            )

    return ParallelPolicy(
        serial_modules=tuple(patterns),
        default_jobs=default_jobs,
        shard_timeout_seconds=float(timeout) if timeout is not None else None,
    )


def load_policy(policy_path: Path | str, *, missing_ok: bool = True) -> ParallelPolicy:
    """Read and parse the policy document (the single filesystem read here).

    A *missing* file resolves to the empty default when ``missing_ok``; an
    unreadable / malformed file fails closed.
    """
    path = Path(policy_path)
    if not path.exists():
        if missing_ok:
            return ParallelPolicy()
        raise TestParallelError(f"test-parallel policy not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TestParallelError(f"cannot read {path}: {exc}") from exc
    return parse_policy_document(raw, source=str(path))


__all__ = (
    "MIN_JOBS",
    "DEFAULT_POLICY_RELPATH",
    "SHARD_PASSED",
    "SHARD_FAILED",
    "SHARD_TIMEOUT",
    "SHARD_CRASHED",
    "SHARD_STATUSES",
    "KIND_PARALLEL",
    "KIND_SERIAL",
    "TestParallelError",
    "matches_any",
    "ParallelPolicy",
    "Shard",
    "ShardPlan",
    "ShardResult",
    "AggregateVerdict",
    "plan_shards",
    "aggregate",
    "parse_policy_document",
    "load_policy",
)
