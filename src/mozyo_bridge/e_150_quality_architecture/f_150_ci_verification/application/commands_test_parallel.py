"""Command handlers for ``mozyo-bridge tests parallel`` (Redmine #13733).

Runs the same test set as ``python -m unittest discover -s tests`` (the
authoritative serial discovery, shared with ``tests profile``) but spread across
**isolated process shards** for local throughput, without weakening the verdict.

Two handlers live here:

- :func:`cmd_tests_parallel` — the parent. It runs the authoritative discovery
  once (reusing ``tests profile``'s ``_repo_root_importable`` so cross-package
  ``from tests.support ...`` imports resolve identically), groups the discovered
  tests by module, plans deterministic shards, spawns one subprocess per shard
  with an isolated ``HOME`` / ``TMPDIR`` / ``MOZYO_BRIDGE_HOME`` and the live
  cockpit-session env pins stripped, collects each shard's structured result, and
  folds them into a fail-closed aggregate verdict.

- :func:`cmd_tests_shard_worker` — the child (a hidden subcommand). Given a spec
  file listing its assigned modules and the discovery parameters, it re-runs the
  *identical* ``discover`` call, filters the suite to its assigned modules, runs
  them, and writes a JSON result file (ran test ids + outcome counts + success).

Parity is structural, not hopeful: parent and worker use the same discovery
mechanics, so the union of what the shards run equals the discovered set. The
aggregate (in the pure domain module) is green only when every shard passed *and*
that union exactly matches the discovered set — a crashed worker, a timeout, a
collection-time import error, or a dropped module all keep the aggregate red
(acceptance: "shard failure を aggregate green にしない").

Isolation protects the operator's live Herdr lane (acceptance #3): each shard
gets its own ``HOME`` / ``TMPDIR`` / ``MOZYO_BRIDGE_HOME`` and cannot see ``TMUX``
/ ``TMUX_PANE`` / ``MOZYO_WORKSPACE_ID`` / ``MOZYO_LANE_ID`` / ``MOZYO_AGENT_ROLE``.
The fresh ``HOME`` is made *functional* so it does not break parity — the shard
gets ``PYTHONUSERBASE`` (so a nested ``python`` still finds user-site deps) and a
deterministic git identity (so ``git commit`` works without ``~/.gitconfig``). See
``_shard_env`` for the full rationale.
"""

from __future__ import annotations

import argparse
import contextlib
import json as _json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from concurrent.futures import (
    FIRST_COMPLETED as _FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait as _wait,
)
from pathlib import Path

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_runtime import (
    _repo_root_importable,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_parallel import (
    DEFAULT_POLICY_RELPATH,
    SHARD_CRASHED,
    SHARD_FAILED,
    SHARD_PASSED,
    SHARD_TIMEOUT,
    AggregateVerdict,
    ParallelPolicy,
    Shard,
    ShardPlan,
    ShardResult,
    TestParallelError,
    aggregate,
    load_policy,
    plan_shards,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_runtime import (
    OUTCOME_ERRORED,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
)
from mozyo_bridge.shared.paths import resolve_repo_root

# Live cockpit-session env pins removed from every shard subprocess so a test can
# never attach to or act on the operator's running Herdr lane (acceptance #3).
STRIPPED_ENV_KEYS = (
    "TMUX",
    "TMUX_PANE",
    "MOZYO_WORKSPACE_ID",
    "MOZYO_LANE_ID",
    "MOZYO_AGENT_ROLE",
)

_OUTCOME_ORDER = (OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_ERRORED, OUTCOME_SKIPPED)


# --------------------------------------------------------------------------- #
# Discovery (parent)                                                          #
# --------------------------------------------------------------------------- #

def _repo_root(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def _iter_tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _discovery_params(args: argparse.Namespace) -> tuple[str, str, str | None]:
    start_dir = getattr(args, "start_dir", "tests")
    pattern = getattr(args, "pattern", "test*.py")
    top_level = getattr(args, "top_level_dir", None)
    return start_dir, pattern, top_level


def _discover_module_tests(
    repo_root: Path, start_dir: str, pattern: str, top_level_dir: str | None
) -> dict[str, tuple[str, ...]]:
    """Discover the suite and group test ids by dotted module (fail closed).

    Uses the identical ``TestLoader().discover`` call as ``tests profile`` /
    ``python -m unittest discover`` under ``_repo_root_importable``. A
    collection-time import error surfaces as a ``unittest.loader`` sentinel; any
    such sentinel makes the base suite un-shardable, so we fail closed here
    rather than plan around a suite that does not even import.
    """
    abs_start = repo_root / start_dir
    if not abs_start.is_dir():
        raise TestParallelError(f"test start dir not found: {abs_start}")
    top = str(Path(top_level_dir)) if top_level_dir else None

    loader = unittest.TestLoader()
    with _repo_root_importable(repo_root):
        suite = loader.discover(
            start_dir=str(abs_start), pattern=pattern, top_level_dir=top
        )
        grouped: dict[str, list[str]] = {}
        collection_errors: list[str] = []
        for test in _iter_tests(suite):
            module = type(test).__module__
            if module.startswith("unittest"):
                # _FailedTest / ModuleImportFailure sentinel: the module failed
                # to import at collection time.
                collection_errors.append(test.id())
                continue
            grouped.setdefault(module, []).append(test.id())

    if collection_errors:
        joined = ", ".join(sorted(collection_errors))
        raise TestParallelError(
            f"collection import error (suite does not import cleanly): {joined}"
        )
    return {module: tuple(ids) for module, ids in grouped.items()}


def _load_durations(path: str | None) -> dict[str, float] | None:
    """Load an optional per-module duration manifest for weighted sharding.

    Accepts either ``{module: seconds}`` or the ``tests profile --format json``
    shape (``{"slowest": [...], ...}`` / a ``timings`` list of
    ``{test_id, duration}``) and folds per-test durations up to their module. A
    missing / unreadable manifest returns ``None`` so planning falls back to
    test-count weighting.
    """
    if not path:
        return None
    file = Path(path)
    if not file.exists():
        raise TestParallelError(f"durations manifest not found: {file}")
    try:
        raw = _json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TestParallelError(f"cannot read durations manifest {file}: {exc}") from exc

    if isinstance(raw, dict) and all(
        isinstance(v, (int, float)) for v in raw.values()
    ):
        return {str(k): float(v) for k, v in raw.items()}

    # Fold a list of per-test timings up to the owning module.
    timings: list[dict] = []
    if isinstance(raw, dict):
        for key in ("timings", "slowest"):
            value = raw.get(key)
            if isinstance(value, list):
                timings.extend(t for t in value if isinstance(t, dict))
    elif isinstance(raw, list):
        timings = [t for t in raw if isinstance(t, dict)]

    module_weights: dict[str, float] = {}
    for timing in timings:
        test_id = timing.get("test_id")
        duration = timing.get("duration")
        if not isinstance(test_id, str) or not isinstance(duration, (int, float)):
            continue
        module = test_id.rsplit(".", 2)[0] if test_id.count(".") >= 2 else test_id
        module_weights[module] = module_weights.get(module, 0.0) + float(duration)
    return module_weights or None


# --------------------------------------------------------------------------- #
# Shard execution (parent)                                                    #
# --------------------------------------------------------------------------- #

# Deterministic git identity for shards (the fresh HOME has no ~/.gitconfig, so a
# test that runs `git commit` would otherwise fail "please tell me who you are").
_SHARD_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "mozyo-tests-shard",
    "GIT_AUTHOR_EMAIL": "shard@mozyo-tests.localhost",
    "GIT_COMMITTER_NAME": "mozyo-tests-shard",
    "GIT_COMMITTER_EMAIL": "shard@mozyo-tests.localhost",
}


def _shard_env(repo_root: Path, shard_home: Path) -> dict[str, str]:
    """Isolated environment for one shard subprocess (acceptance #3: 固有 HOME).

    Each shard gets its own ``HOME`` / ``TMPDIR`` / ``MOZYO_BRIDGE_HOME`` so a test
    can never read or write the operator's home-scoped config/state or the live
    Herdr lane, and the live cockpit-session pins are stripped. The challenge is
    doing this *without* breaking parity: a bare fresh HOME hides the interpreter's
    user site-packages (where deps like PyYAML may be pip-user-installed) and has
    no git identity, which broke otherwise-hermetic tests that spawn a nested
    ``python -m mozyo_bridge`` or ``git commit``. So the fresh HOME is made
    functional:

    - ``PYTHONUSERBASE`` is pinned to the parent's real user base, so a nested
      ``python`` resolves user-site packages regardless of the shard's HOME.
    - ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` give a deterministic identity so
      ``git commit`` works without the operator's ``~/.gitconfig``.

    ``MOZYO_REPO`` is inherited (not pinned): repo resolution then follows the same
    cwd/env rules as the serial run, and pinning it broke tests that exercise
    divergent-cwd resolution.

    ``PYTHONPATH`` is likewise inherited *verbatim* — never augmented. The shard's
    own runtime is resolved in-process instead (see :func:`_shard_command`), because
    ``PYTHONPATH`` is inherited by everything a test body spawns, and a source
    ``src/`` entry leaking into a nested ``pip install <wheel>`` makes pip see the
    same-version metadata already importable and skip the install (exit 0, no console
    script) — a verdict divergence from serial that no per-shard isolation can undo
    (Redmine #13733 / #13735 j#78390 F1).
    """
    env = dict(os.environ)
    home = shard_home / "home"
    tmp = shard_home / "tmp"
    mozyo = shard_home / "mozyo"
    for directory in (home, tmp, mozyo):
        directory.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["MOZYO_BRIDGE_HOME"] = str(mozyo)
    # Keep the fresh HOME functional (see docstring): user-site deps + git identity.
    user_base = _user_base()
    if user_base:
        env["PYTHONUSERBASE"] = user_base
    env.update(_SHARD_GIT_IDENTITY)
    for key in STRIPPED_ENV_KEYS:
        env.pop(key, None)
    return env


def _user_base() -> str | None:
    """The parent's real Python user base, resolved before the HOME override.

    Passed to the shard as ``PYTHONUSERBASE`` so a nested ``python`` under the
    shard's fresh HOME still finds pip-user-installed deps.
    """
    import site

    try:
        return site.getuserbase()
    except Exception:  # pragma: no cover - not all environments expose this
        return None


def _runtime_root() -> str:
    """Absolute dir that makes the *parent's own* mozyo_bridge importable.

    The parent may be running from a source checkout that is not installed in the
    interpreter (``PYTHONPATH=src python -m mozyo_bridge``), so the child cannot be
    trusted to import the same runtime by default — and it runs with
    ``cwd=<target repo>``, which for the fixture tests is a foreign tree. Resolving
    the package's own location gives the child an absolute entry that pins it to the
    parent's runtime regardless of cwd or install mode.
    """
    import mozyo_bridge  # local import: avoid a package-load cycle at import time

    return str(Path(mozyo_bridge.__file__).resolve().parent.parent)


# The child is launched with `python -c <bootstrap>` rather than `python -m
# mozyo_bridge` + PYTHONPATH: `sys.path` is process-local, so the runtime entry
# reaches the shard worker WITHOUT being inherited by anything the test bodies
# spawn. Injecting it via PYTHONPATH instead leaked `src/` into nested subprocesses
# and broke serial/parallel verdict parity (Redmine #13735 j#78390 F1) — the shard
# env must be the serial env, plus isolation, and nothing else.
_SHARD_BOOTSTRAP = (
    "import sys\n"
    "sys.path.insert(0, {runtime!r})\n"
    "from mozyo_bridge.application.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def _shard_command(
    repo_root: Path, spec_path: Path, result_path: Path, runtime_root: str
) -> list[str]:
    """The shard worker's argv (runtime pinned in-process, not via the env)."""
    return [
        sys.executable,
        "-c",
        _SHARD_BOOTSTRAP.format(runtime=runtime_root),
        "tests",
        "_shard-worker",
        # Pass the parent-resolved repo explicitly. resolve_repo_root gives an
        # explicit --repo precedence over the (inherited, test-body-facing)
        # MOZYO_REPO env, so a conflicting ambient MOZYO_REPO can never point the
        # worker at the wrong tree (Redmine #13733 R2-F1).
        "--repo",
        str(repo_root),
        "--spec",
        str(spec_path),
        "--result",
        str(result_path),
    ]


def _replay_command(shard: Shard) -> str:
    """A human-runnable serial reproduction of one shard (from the repo root).

    Modules discover as ``unit.<ctx>.test_mod`` (relative to ``tests/``); the same
    module is importable as ``tests.unit.<ctx>.test_mod`` from the repo root, so
    this command reproduces exactly the shard's tests serially for debugging. The
    runner additionally isolates HOME/TMPDIR/MOZYO_BRIDGE_HOME per shard.
    """
    if not shard.modules:
        return ""
    modules = " ".join(f"tests.{module}" for module in shard.modules)
    return f"python -m unittest -v {modules}"


# How much of a shard's stdout/stderr to retain (acceptance: 各 shard へ ...
# stdout/stderr/exit を収集する). Bounded so a chatty shard can't blow up memory
# or the JSON output, but large enough to carry a failure's evidence.
_OUTPUT_TAIL_LIMIT = 8000


def _tail(stream: str | bytes | None) -> str:
    """Bounded trailing slice of a captured stream (str or bytes), for evidence."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", "replace")
    return stream[-_OUTPUT_TAIL_LIMIT:]


def _classify_shard(
    shard: Shard,
    *,
    returncode: int | None,
    result_payload: dict | None,
    timed_out: bool,
    stdout_tail: str,
    stderr_tail: str,
    duration: float,
) -> ShardResult:
    """Turn a subprocess outcome into a fail-closed :class:`ShardResult`.

    The shard's captured stdout/stderr tails and exit code are retained on the
    result (not just on a crash) so a failure's first-hand evidence is available
    from the aggregate output without re-running.
    """
    replay = _replay_command(shard)

    def build(status: str, *, ran=(), counts=None, detail=None, failed=()) -> ShardResult:
        return ShardResult(
            index=shard.index,
            kind=shard.kind,
            status=status,
            ran_test_ids=tuple(ran),
            counts=counts or {},
            returncode=returncode,
            detail=detail,
            replay_command=replay,
            duration_seconds=duration,
            failed_test_ids=tuple(failed),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )

    if timed_out:
        return build(SHARD_TIMEOUT, detail="shard exceeded the shard timeout")
    if result_payload is None:
        return build(
            SHARD_CRASHED,
            detail=f"no result emitted (returncode={returncode})",
        )
    ran_ids = tuple(result_payload.get("ran_test_ids", ()))
    failed_ids = tuple(result_payload.get("failed_test_ids", ()))
    counts = {
        name: int(result_payload.get("counts", {}).get(name, 0))
        for name in _OUTCOME_ORDER
    }
    success = bool(result_payload.get("success", False))
    if success and returncode == 0:
        return build(SHARD_PASSED, ran=ran_ids, counts=counts, failed=failed_ids)
    if not success:
        detail = (
            f"{counts.get(OUTCOME_FAILED, 0)} failed / "
            f"{counts.get(OUTCOME_ERRORED, 0)} errored"
        )
        return build(SHARD_FAILED, ran=ran_ids, counts=counts, detail=detail, failed=failed_ids)
    # Reported success but a non-zero exit — treat as a crash, not green.
    return build(
        SHARD_CRASHED,
        ran=ran_ids,
        counts=counts,
        detail=f"success reported but returncode={returncode}",
        failed=failed_ids,
    )


def _run_shard(
    shard: Shard,
    *,
    repo_root: Path,
    work_dir: Path,
    start_dir: str,
    pattern: str,
    top_level_dir: str | None,
    failfast: bool,
    timeout: float | None,
) -> ShardResult:
    """Run one shard as an isolated subprocess and classify the outcome."""
    shard_home = work_dir / f"shard-{shard.index}"
    shard_home.mkdir(parents=True, exist_ok=True)
    spec_path = shard_home / "spec.json"
    result_path = shard_home / "result.json"
    spec_path.write_text(
        _json.dumps(
            {
                "modules": list(shard.modules),
                "start_dir": start_dir,
                "pattern": pattern,
                "top_level_dir": top_level_dir,
                "failfast": failfast,
            }
        ),
        encoding="utf-8",
    )
    cmd = _shard_command(repo_root, spec_path, result_path, _runtime_root())
    env = _shard_env(repo_root, shard_home)
    started = time.perf_counter()
    timed_out = False
    returncode: int | None = None
    stdout_tail = ""
    stderr_tail = ""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        returncode = proc.returncode
        stdout_tail = _tail(proc.stdout)
        stderr_tail = _tail(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_tail = _tail(exc.stdout)
        stderr_tail = _tail(exc.stderr)
    duration = time.perf_counter() - started

    result_payload: dict | None = None
    if not timed_out and result_path.exists():
        try:
            result_payload = _json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result_payload = None

    return _classify_shard(
        shard,
        returncode=returncode,
        stdout_tail=stdout_tail,
        result_payload=result_payload,
        timed_out=timed_out,
        stderr_tail=stderr_tail,
        duration=duration,
    )


def _skipped_shard(shard: Shard, reason: str) -> ShardResult:
    return ShardResult(
        index=shard.index,
        kind=shard.kind,
        status=SHARD_FAILED,
        ran_test_ids=(),
        counts={},
        returncode=None,
        detail=reason,
        replay_command=_replay_command(shard),
        duration_seconds=None,
    )


def _execute_plan(
    plan: ShardPlan,
    *,
    repo_root: Path,
    work_dir: Path,
    start_dir: str,
    pattern: str,
    top_level_dir: str | None,
    failfast: bool,
    timeout: float | None,
) -> list[ShardResult]:
    """Run the parallel shards through a bounded pool, then the serial shard alone.

    Shards are over-partitioned relative to ``jobs`` (the worker count), so a pool
    of ``jobs`` workers drains a queue of finer shards. With ``--failfast``, once a
    completed shard has failed no further queued shard is launched; the un-started
    shards are recorded as failed (``not run``) so the aggregate stays red rather
    than silently green. (In-flight shards are allowed to finish — a subprocess is
    not forcibly killed mid-run.)
    """

    def run(shard: Shard) -> ShardResult:
        return _run_shard(
            shard,
            repo_root=repo_root,
            work_dir=work_dir,
            start_dir=start_dir,
            pattern=pattern,
            top_level_dir=top_level_dir,
            failfast=failfast,
            timeout=timeout,
        )

    results: dict[int, ShardResult] = {}
    parallel = list(plan.parallel_shards)
    serial = list(plan.serial_shards)
    aborted = False

    if parallel:
        max_workers = max(1, min(plan.jobs, len(parallel)))
        pending = iter(parallel)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            inflight: dict = {}

            def submit_next() -> bool:
                shard = next(pending, None)
                if shard is None:
                    return False
                inflight[pool.submit(run, shard)] = shard
                return True

            for _ in range(max_workers):
                if not submit_next():
                    break
            while inflight:
                done, _pending = _wait(inflight, return_when=_FIRST_COMPLETED)
                for future in done:
                    shard = inflight.pop(future)
                    result = future.result()
                    results[shard.index] = result
                    if failfast and not result.ok:
                        aborted = True
                # Refill only while not aborted; once aborted we stop launching
                # further queued shards (they are marked skipped below).
                while not aborted and len(inflight) < max_workers and submit_next():
                    pass

    for shard in serial:
        if aborted:
            results[shard.index] = _skipped_shard(shard, "not run (--failfast)")
            continue
        result = run(shard)
        results[shard.index] = result
        if failfast and not result.ok:
            aborted = True

    for shard in plan.shards:
        results.setdefault(shard.index, _skipped_shard(shard, "not run (--failfast)"))

    return [results[shard.index] for shard in plan.shards]


# --------------------------------------------------------------------------- #
# Parent command                                                              #
# --------------------------------------------------------------------------- #

def _resolve_jobs(args: argparse.Namespace, policy: ParallelPolicy) -> int:
    explicit = getattr(args, "jobs", None)
    if explicit is not None:
        return max(1, int(explicit))
    if policy.default_jobs is not None:
        return max(1, policy.default_jobs)
    return max(1, os.cpu_count() or 1)


def _resolve_timeout(args: argparse.Namespace, policy: ParallelPolicy) -> float | None:
    explicit = getattr(args, "shard_timeout", None)
    if explicit is not None:
        return float(explicit)
    return policy.shard_timeout_seconds


def _policy_path(args: argparse.Namespace, repo_root: Path) -> Path:
    explicit = getattr(args, "serial_policy", None)
    if explicit:
        return Path(explicit)
    return repo_root / DEFAULT_POLICY_RELPATH


def cmd_tests_parallel(args: argparse.Namespace) -> int:
    """Plan, run, and aggregate the isolated-shard parallel test run."""
    repo_root = _repo_root(args)
    start_dir, pattern, top_level_dir = _discovery_params(args)
    try:
        policy = load_policy(_policy_path(args, repo_root))
        weights = _load_durations(getattr(args, "durations", None))
        module_tests = _discover_module_tests(
            repo_root, start_dir, pattern, top_level_dir
        )
    except TestParallelError as exc:
        print(f"parallel test run failed: {exc}", file=sys.stderr)
        return 1

    if not module_tests:
        print("no tests discovered", file=sys.stderr)
        return 1

    jobs = _resolve_jobs(args, policy)
    timeout = _resolve_timeout(args, policy)
    shards = getattr(args, "shards", None)
    shard_count = max(1, int(shards)) if shards is not None else None
    plan = plan_shards(
        module_tests, jobs=jobs, policy=policy, weights=weights, shard_count=shard_count
    )

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="mozyo-tests-parallel-") as tmp:
        results = _execute_plan(
            plan,
            repo_root=repo_root,
            work_dir=Path(tmp),
            start_dir=start_dir,
            pattern=pattern,
            top_level_dir=top_level_dir,
            failfast=bool(getattr(args, "failfast", False)),
            timeout=timeout,
        )
    wall = time.perf_counter() - started

    verdict = aggregate(plan, results)

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        _render_json(plan, results, verdict, wall)
    else:
        _render_text(plan, results, verdict, wall)

    return 0 if verdict.success else 1


# --------------------------------------------------------------------------- #
# Shard worker (child, hidden subcommand)                                     #
# --------------------------------------------------------------------------- #

class _ShardCollectingResult(unittest.TextTestResult):
    """Records ran test ids + outcome buckets; test output goes to stderr."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.ran_ids: list[str] = []
        self.bucket = {
            OUTCOME_PASSED: 0,
            OUTCOME_FAILED: 0,
            OUTCOME_ERRORED: 0,
            OUTCOME_SKIPPED: 0,
        }
        self._outcomes: dict[int, str] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self.ran_ids.append(test.id())

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

    def addExpectedFailure(self, test: unittest.TestCase, err: object) -> None:
        super().addExpectedFailure(test, err)
        self._outcomes[id(test)] = OUTCOME_PASSED

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._outcomes[id(test)] = OUTCOME_FAILED

    def stopTest(self, test: unittest.TestCase) -> None:
        outcome = self._outcomes.pop(id(test), OUTCOME_PASSED)
        self.bucket[outcome] = self.bucket.get(outcome, 0) + 1
        super().stopTest(test)


@contextlib.contextmanager
def _shard_names_importable(repo_root: Path, top_level_dir: Path) -> Iterator[None]:
    """Reproduce ``discover``'s ``sys.path`` so module names resolve identically.

    ``discover(start_dir=tests, top_level_dir=None)`` inserts the top-level dir
    (``tests/``) at ``sys.path[0]`` and names modules relative to it (``unit.<ctx>
    .test_mod``); ``_repo_root_importable`` separately puts the repo root on the
    path so the test modules' own ``from tests.support ...`` imports resolve.
    Loading the shard's assigned modules **by name** (rather than re-discovering
    the whole tree) needs both present, and yields exactly the ids ``discover``
    would produce for those modules — the parent's parity check is the backstop
    if they ever diverge.
    """
    added: list[str] = []
    for entry in (str(top_level_dir), str(repo_root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
            added.append(entry)
    try:
        yield
    finally:
        for entry in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(entry)


def cmd_tests_shard_worker(args: argparse.Namespace) -> int:
    """Run one shard's assigned modules and write its JSON result file.

    This is the child process spawned by :func:`cmd_tests_parallel`. It loads
    only its assigned modules by name (so it never re-imports the whole tree),
    with ``sys.path`` matching ``discover`` so the ids it runs are exactly the
    subset of the parent's discovered ids for those modules.
    """
    spec = _json.loads(Path(args.spec).read_text(encoding="utf-8"))
    modules = list(spec.get("modules", ()))
    start_dir = spec.get("start_dir", "tests")
    top_level_dir = spec.get("top_level_dir")
    failfast = bool(spec.get("failfast", False))

    repo_root = _repo_root(args)
    top_level = Path(top_level_dir) if top_level_dir else (repo_root / start_dir)

    loader = unittest.TestLoader()
    with _shard_names_importable(repo_root, top_level):
        suite = unittest.TestSuite()
        for module in modules:
            # A module that fails to import yields a _FailedTest that errors when
            # run — fail-closed, never silently dropped.
            suite.addTests(loader.loadTestsFromName(module))
        runner = unittest.TextTestRunner(
            stream=sys.stderr,
            verbosity=1,
            failfast=failfast,
            resultclass=_ShardCollectingResult,
        )
        result = runner.run(suite)

    assert isinstance(result, _ShardCollectingResult)
    failed_ids = [test.id() for test, _ in result.failures]
    failed_ids += [test.id() for test, _ in result.errors]
    payload = {
        "ran_test_ids": result.ran_ids,
        "counts": result.bucket,
        "failed_test_ids": sorted(failed_ids),
        "success": result.wasSuccessful(),
    }
    Path(args.result).write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return 0 if result.wasSuccessful() else 1


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def _render_text(
    plan: ShardPlan,
    results: list[ShardResult],
    verdict: AggregateVerdict,
    wall: float,
) -> None:
    parallel_n = len(plan.parallel_shards)
    serial_n = len(plan.serial_shards)
    print("=== parallel test run ===")
    print(
        f"plan: {len(plan.shards)} shards ({parallel_n} parallel + {serial_n} serial), "
        f"jobs={plan.jobs}, weight={plan.weight_basis}"
    )
    print(
        f"modules: {plan.total_modules}  discovered tests: {plan.total_expected_tests}"
    )
    for result in results:
        shard = plan.shards[result.index]
        secs = f"{result.duration_seconds:6.2f}s" if result.duration_seconds else "   -  "
        exit_str = "-" if result.returncode is None else str(result.returncode)
        tag = "PASS" if result.ok else result.status.upper()
        line = (
            f"  shard {result.index} [{result.kind}] "
            f"modules={len(shard.modules)} tests={shard.expected_count} "
            f"{secs} exit={exit_str}  {tag}"
        )
        if result.detail and not result.ok:
            line += f"  -- {result.detail}"
        print(line)

    counts = verdict.counts
    print(
        "outcomes: "
        + "  ".join(f"{name}={counts.get(name, 0)}" for name in _OUTCOME_ORDER)
    )
    print(
        f"tests: expected={verdict.total_expected_tests} "
        f"ran={verdict.total_ran_tests}"
    )
    print(f"wall clock: {wall:.2f}s")
    print(f"result: {'PASS' if verdict.success else 'FAIL'}")

    if not verdict.success:
        for reason in verdict.reasons:
            print(f"  reason: {reason}")
        if verdict.failed_test_ids:
            print(f"failed tests ({len(verdict.failed_test_ids)}):")
            for test_id in verdict.failed_test_ids:
                print(f"  - {test_id}")
        for result in results:
            if result.ok:
                continue
            tail = (result.stderr_tail or result.stdout_tail or "").strip()
            if tail:
                clipped = tail[-1200:]
                print(f"shard {result.index} output tail (exit={result.returncode}):")
                for out_line in clipped.splitlines()[-20:]:
                    print(f"  | {out_line}")
        print("replay the failed shards serially from the repo root:")
        for result in results:
            if not result.ok and result.replay_command:
                print(f"  # shard {result.index}\n  {result.replay_command}")


def _render_json(
    plan: ShardPlan,
    results: list[ShardResult],
    verdict: AggregateVerdict,
    wall: float,
) -> None:
    payload = {
        "success": verdict.success,
        "jobs": plan.jobs,
        "shard_count": len(plan.shards),
        "weight_basis": plan.weight_basis,
        "total_modules": plan.total_modules,
        "wall_clock_seconds": round(wall, 6),
        "shards": [
            {
                "index": result.index,
                "kind": result.kind,
                "status": result.status,
                "modules": list(plan.shards[result.index].modules),
                "expected_tests": plan.shards[result.index].expected_count,
                "ran_tests": len(result.ran_test_ids),
                "counts": dict(result.counts),
                "duration_seconds": (
                    round(result.duration_seconds, 6)
                    if result.duration_seconds is not None
                    else None
                ),
                "returncode": result.returncode,
                "detail": result.detail,
                "stdout_tail": result.stdout_tail,
                "stderr_tail": result.stderr_tail,
                "replay_command": result.replay_command,
            }
            for result in results
        ],
        "aggregate": verdict.as_dict(),
    }
    print(_json.dumps(payload, ensure_ascii=False, indent=2))


__all__ = (
    "STRIPPED_ENV_KEYS",
    "cmd_tests_parallel",
    "cmd_tests_shard_worker",
)
