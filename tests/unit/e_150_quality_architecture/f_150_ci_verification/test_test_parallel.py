"""Pure-domain tests for the local parallel test runner (Redmine #13733).

Exercises the shard planner (deterministic LPT partition, total coverage, serial
segregation, jobs cap, duration-vs-count weight basis), the fail-closed aggregate
verdict (green only when every shard passed AND parity holds; red on failure /
timeout / crash / missing / unexpected / shard-count mismatch), and the policy
parse (defaults, valid, and every fail-closed malformed shape). All synthetic —
no subprocess, no discovery — so the decisions are tested in isolation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_parallel import (
    DEFAULT_SHARDS_PER_JOB,
    KIND_PARALLEL,
    KIND_SERIAL,
    SHARD_CRASHED,
    SHARD_FAILED,
    SHARD_PASSED,
    SHARD_TIMEOUT,
    AggregateVerdict,
    ParallelPolicy,
    ShardResult,
    TestParallelError,
    aggregate,
    load_policy,
    matches_any,
    parse_policy_document,
    plan_shards,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_runtime import (
    OUTCOME_ERRORED,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
)


def _module_tests(spec: dict[str, int]) -> dict[str, tuple[str, ...]]:
    """Build a module -> test-id map with ``spec`` counts per module."""
    return {
        module: tuple(f"{module}.Case.test_{i}" for i in range(count))
        for module, count in spec.items()
    }


class PlanShardsTest(unittest.TestCase):
    def test_every_module_assigned_exactly_once(self) -> None:
        module_tests = _module_tests({"a": 3, "b": 1, "c": 2, "d": 4})
        plan = plan_shards(module_tests, jobs=2, policy=ParallelPolicy())
        assigned = [m for shard in plan.shards for m in shard.modules]
        self.assertEqual(sorted(assigned), ["a", "b", "c", "d"])
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(plan.total_modules, 4)
        self.assertEqual(plan.total_expected_tests, 10)

    def test_union_of_shard_ids_equals_discovered(self) -> None:
        module_tests = _module_tests({"a": 3, "b": 1, "c": 2})
        plan = plan_shards(module_tests, jobs=3, policy=ParallelPolicy())
        expected = {tid for ids in module_tests.values() for tid in ids}
        self.assertEqual(plan.expected_test_ids, frozenset(expected))

    def test_deterministic_same_inputs_same_plan(self) -> None:
        module_tests = _module_tests({"a": 5, "b": 2, "c": 7, "d": 1, "e": 3})
        p1 = plan_shards(module_tests, jobs=3, policy=ParallelPolicy())
        p2 = plan_shards(module_tests, jobs=3, policy=ParallelPolicy())
        self.assertEqual(
            [s.modules for s in p1.shards], [s.modules for s in p2.shards]
        )

    def test_jobs_caps_at_module_count(self) -> None:
        module_tests = _module_tests({"a": 1, "b": 1})
        plan = plan_shards(module_tests, jobs=8, policy=ParallelPolicy())
        # Only two modules, so at most two parallel shards despite jobs=8.
        self.assertEqual(len(plan.parallel_shards), 2)

    def test_lpt_balances_by_weight(self) -> None:
        # Heaviest-first LPT into an explicit 2 bins: 6 alone balances 3+2+1.
        module_tests = _module_tests({"big": 6, "m3": 3, "m2": 2, "m1": 1})
        plan = plan_shards(
            module_tests, jobs=2, policy=ParallelPolicy(), shard_count=2
        )
        weights = sorted(s.weight for s in plan.parallel_shards)
        self.assertEqual(weights, [6.0, 6.0])

    def test_over_partitions_beyond_jobs_by_default(self) -> None:
        # Default target is jobs * DEFAULT_SHARDS_PER_JOB, capped at module count.
        module_tests = _module_tests({f"m{i}": 1 for i in range(20)})
        plan = plan_shards(module_tests, jobs=2, policy=ParallelPolicy())
        self.assertEqual(len(plan.parallel_shards), 2 * DEFAULT_SHARDS_PER_JOB)
        # Every module still assigned exactly once (coverage preserved).
        assigned = [m for s in plan.shards for m in s.modules]
        self.assertEqual(sorted(assigned), sorted(module_tests))

    def test_shard_count_capped_at_module_count(self) -> None:
        module_tests = _module_tests({"a": 1, "b": 1})
        plan = plan_shards(
            module_tests, jobs=1, policy=ParallelPolicy(), shard_count=99
        )
        self.assertEqual(len(plan.parallel_shards), 2)

    def test_shard_count_below_one_raises(self) -> None:
        with self.assertRaises(TestParallelError):
            plan_shards(
                _module_tests({"a": 1}), jobs=1, policy=ParallelPolicy(), shard_count=0
            )

    def test_durations_weight_basis_overrides_count(self) -> None:
        module_tests = _module_tests({"a": 1, "b": 1, "c": 1})
        # By count all equal; durations make 'a' the heavy one.
        weights = {"a": 10.0, "b": 1.0, "c": 1.0}
        plan = plan_shards(
            module_tests, jobs=2, policy=ParallelPolicy(), weights=weights
        )
        self.assertEqual(plan.weight_basis, "durations")
        # 'a' (10) lands alone; b+c (2) share the other bin.
        big = max(plan.parallel_shards, key=lambda s: s.weight)
        self.assertEqual(big.modules, ("a",))

    def test_missing_duration_falls_back_to_count(self) -> None:
        module_tests = _module_tests({"a": 4, "b": 4})
        # weights present but empty for these modules -> per-module count fallback.
        plan = plan_shards(
            module_tests, jobs=2, policy=ParallelPolicy(), weights={"z": 5.0}
        )
        self.assertEqual(sorted(s.weight for s in plan.parallel_shards), [4.0, 4.0])

    def test_serial_bucket_segregated_into_own_shard(self) -> None:
        module_tests = _module_tests({"unit.safe": 2, "unit.unsafe_port": 1, "unit.safe2": 1})
        policy = ParallelPolicy(serial_modules=("unit.unsafe_*",))
        plan = plan_shards(module_tests, jobs=4, policy=policy)
        serial = plan.serial_shards
        self.assertEqual(len(serial), 1)
        self.assertEqual(serial[0].kind, KIND_SERIAL)
        self.assertEqual(serial[0].modules, ("unit.unsafe_port",))
        # The serial module is NOT in any parallel shard.
        parallel_modules = {m for s in plan.parallel_shards for m in s.modules}
        self.assertNotIn("unit.unsafe_port", parallel_modules)

    def test_no_serial_shard_when_bucket_empty(self) -> None:
        module_tests = _module_tests({"a": 1, "b": 1})
        plan = plan_shards(module_tests, jobs=2, policy=ParallelPolicy())
        self.assertEqual(plan.serial_shards, ())

    def test_all_serial_gives_no_parallel_shard(self) -> None:
        module_tests = _module_tests({"unit.a": 1, "unit.b": 1})
        policy = ParallelPolicy(serial_modules=("unit.*",))
        plan = plan_shards(module_tests, jobs=4, policy=policy)
        self.assertEqual(plan.parallel_shards, ())
        self.assertEqual(len(plan.serial_shards), 1)
        self.assertEqual(plan.serial_shards[0].modules, ("unit.a", "unit.b"))

    def test_jobs_below_one_raises(self) -> None:
        with self.assertRaises(TestParallelError):
            plan_shards(_module_tests({"a": 1}), jobs=0, policy=ParallelPolicy())


class AggregateTest(unittest.TestCase):
    def _plan(self, spec: dict[str, int], **kw):
        return plan_shards(_module_tests(spec), jobs=kw.get("jobs", 2), policy=ParallelPolicy())

    def _passed_result(self, shard) -> ShardResult:
        counts = {
            OUTCOME_PASSED: shard.expected_count,
            OUTCOME_FAILED: 0,
            OUTCOME_ERRORED: 0,
            OUTCOME_SKIPPED: 0,
        }
        return ShardResult(
            index=shard.index,
            kind=shard.kind,
            status=SHARD_PASSED,
            ran_test_ids=shard.expected_test_ids,
            counts=counts,
            returncode=0,
        )

    def test_all_passed_full_parity_is_green(self) -> None:
        plan = self._plan({"a": 2, "b": 3})
        results = [self._passed_result(s) for s in plan.shards]
        verdict = aggregate(plan, results)
        self.assertTrue(verdict.success)
        self.assertEqual(verdict.total_ran_tests, verdict.total_expected_tests)
        self.assertEqual(verdict.counts[OUTCOME_PASSED], 5)
        self.assertEqual(verdict.reasons, ())

    def test_failed_shard_is_red(self) -> None:
        plan = self._plan({"a": 2, "b": 2})
        results = [self._passed_result(s) for s in plan.shards]
        broken = results[0]
        results[0] = ShardResult(
            index=broken.index,
            kind=broken.kind,
            status=SHARD_FAILED,
            ran_test_ids=broken.ran_test_ids,
            counts={**broken.counts, OUTCOME_PASSED: 1, OUTCOME_FAILED: 1},
            returncode=1,
            detail="1 failed / 0 errored",
        )
        verdict = aggregate(plan, results)
        self.assertFalse(verdict.success)
        self.assertIn(0, verdict.failed_shards)

    def test_missing_ids_is_red_even_if_shards_report_success(self) -> None:
        # A crashed worker reports no ids: its planned tests become "missing".
        plan = self._plan({"a": 2, "b": 2})
        results = [self._passed_result(s) for s in plan.shards]
        crashed = results[1]
        results[1] = ShardResult(
            index=crashed.index,
            kind=crashed.kind,
            status=SHARD_CRASHED,
            ran_test_ids=(),
            counts={},
            returncode=1,
            detail="no result emitted",
        )
        verdict = aggregate(plan, results)
        self.assertFalse(verdict.success)
        self.assertTrue(verdict.missing_test_ids)
        self.assertTrue(any("not run by any shard" in r for r in verdict.reasons))

    def test_unexpected_ids_is_red(self) -> None:
        plan = self._plan({"a": 2})
        result = self._passed_result(plan.shards[0])
        result = ShardResult(
            index=result.index,
            kind=result.kind,
            status=SHARD_PASSED,
            ran_test_ids=result.ran_test_ids + ("a.Case.test_intruder",),
            counts=result.counts,
            returncode=0,
        )
        verdict = aggregate(plan, [result])
        self.assertFalse(verdict.success)
        self.assertEqual(verdict.unexpected_test_ids, ("a.Case.test_intruder",))

    def test_shard_count_mismatch_is_red(self) -> None:
        plan = self._plan({"a": 1, "b": 1})
        # Only one of two shards reported.
        results = [self._passed_result(plan.shards[0])]
        verdict = aggregate(plan, results)
        self.assertFalse(verdict.success)
        self.assertTrue(any("shard count mismatch" in r for r in verdict.reasons))

    def test_timeout_shard_is_red(self) -> None:
        plan = self._plan({"a": 1, "b": 1})
        results = [self._passed_result(plan.shards[0])]
        timed = plan.shards[1]
        results.append(
            ShardResult(
                index=timed.index,
                kind=timed.kind,
                status=SHARD_TIMEOUT,
                ran_test_ids=(),
                counts={},
                detail="shard exceeded the shard timeout",
            )
        )
        verdict = aggregate(plan, results)
        self.assertFalse(verdict.success)
        self.assertIn(1, verdict.failed_shards)


class ParsePolicyTest(unittest.TestCase):
    def test_none_is_empty_default(self) -> None:
        policy = parse_policy_document(None, source="x")
        self.assertEqual(policy, ParallelPolicy())

    def test_valid_document(self) -> None:
        raw = {
            "serial_modules": ["unit.a.*", "unit.b"],
            "default_jobs": 7,
            "shard_timeout_seconds": 600,
        }
        policy = parse_policy_document(raw, source="x")
        self.assertEqual(policy.serial_modules, ("unit.a.*", "unit.b"))
        self.assertEqual(policy.default_jobs, 7)
        self.assertEqual(policy.shard_timeout_seconds, 600.0)

    def test_non_mapping_fails_closed(self) -> None:
        with self.assertRaises(TestParallelError):
            parse_policy_document(["not", "a", "map"], source="x")

    def test_serial_modules_must_be_list(self) -> None:
        with self.assertRaises(TestParallelError):
            parse_policy_document({"serial_modules": "unit.*"}, source="x")

    def test_serial_module_entry_must_be_nonempty_string(self) -> None:
        with self.assertRaises(TestParallelError):
            parse_policy_document({"serial_modules": ["", "ok"]}, source="x")

    def test_default_jobs_must_be_positive_int(self) -> None:
        with self.assertRaises(TestParallelError):
            parse_policy_document({"default_jobs": 0}, source="x")
        with self.assertRaises(TestParallelError):
            parse_policy_document({"default_jobs": True}, source="x")

    def test_shard_timeout_must_be_positive_number(self) -> None:
        with self.assertRaises(TestParallelError):
            parse_policy_document({"shard_timeout_seconds": 0}, source="x")
        with self.assertRaises(TestParallelError):
            parse_policy_document({"shard_timeout_seconds": "600"}, source="x")

    def test_matches_any(self) -> None:
        self.assertTrue(matches_any("unit.a.test_x", ("unit.a.*",)))
        self.assertTrue(matches_any("unit.b", ("unit.b",)))
        self.assertFalse(matches_any("unit.b.test", ("unit.b",)))
        self.assertFalse(matches_any("unit.a", ()))


class LoadPolicyTest(unittest.TestCase):
    def test_missing_file_ok_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = load_policy(Path(tmp) / "nope.yaml")
            self.assertEqual(policy, ParallelPolicy())

    def test_missing_file_required_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TestParallelError):
                load_policy(Path(tmp) / "nope.yaml", missing_ok=False)

    def test_reads_and_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text("serial_modules:\n  - unit.x.*\n", encoding="utf-8")
            policy = load_policy(path)
            self.assertEqual(policy.serial_modules, ("unit.x.*",))

    def test_malformed_yaml_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text("serial_modules: [unclosed\n", encoding="utf-8")
            with self.assertRaises(TestParallelError):
                load_policy(path)


if __name__ == "__main__":
    unittest.main()
