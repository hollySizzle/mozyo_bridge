"""Redmine custom-field execution-bucket lane bucket provider tests (Redmine #12922).

Drives the concrete :class:`RedmineCustomFieldLaneBucketProvider` over in-memory Redmine
snapshots (the production provider is its own test double — no mock library, matching the
f_120 convention). Covers: field selection by id and by name; the normalized
:class:`LaneBucket` result shape (custom_field source kind, value as id/name, no Version
status/dates) and its compatibility with the fixed_version provider's shape; open-leaf
enumeration; the runtime :class:`LaneBucketProvider` protocol conformance; the umbrella vs.
per-child execution-bucket judgment; the allow-list; and every fail-closed skip reason
(unset value, ambiguous multi-value, disallowed value, unknown bucket, closed issue, issue
absent, empty selector).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.lane_bucket_provider import (  # noqa: E402
    SKIP_AMBIGUOUS_SOURCE,
    SKIP_BUCKET_NOT_FOUND,
    SKIP_DISALLOWED_VALUE,
    SKIP_ISSUE_CLOSED,
    SKIP_NO_EXECUTION_BUCKET,
    SOURCE_KIND_CUSTOM_FIELD,
    LaneBucketError,
    LaneBucketProvider,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.custom_field_lane_bucket_provider import (  # noqa: E402
    CustomFieldBucketConfig,
    RedmineCustomFieldLaneBucketProvider,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.fixed_version_lane_bucket_provider import (  # noqa: E402
    RedmineFixedVersionLaneBucketProvider,
)

_FIELD_ID = "5"
_FIELD_NAME = "execution_bucket"


def _issue(
    issue_id: int,
    *,
    tracker: str = "Task",
    status: str = "新規",
    closed: bool = False,
    parent: int | None = None,
    bucket: object = "bucket-a",
) -> dict:
    """A Redmine issue object carrying the execution-bucket custom field.

    ``bucket`` is the custom-field ``value``: a string (single value), a list (multi-value),
    or ``None`` to omit the field entirely (unset).
    """
    payload: dict = {
        "id": issue_id,
        "tracker": {"name": tracker},
        "status": {"name": status, "is_closed": closed},
    }
    if parent is not None:
        payload["parent"] = {"id": parent}
    if bucket is not None:
        payload["custom_fields"] = [
            {"id": int(_FIELD_ID), "name": _FIELD_NAME, "value": bucket}
        ]
    return payload


def _provider_by_id(issues: object) -> RedmineCustomFieldLaneBucketProvider:
    return RedmineCustomFieldLaneBucketProvider(
        issues_payload=issues, config=CustomFieldBucketConfig(field_id=_FIELD_ID)
    )


def _provider_by_name(issues: object) -> RedmineCustomFieldLaneBucketProvider:
    return RedmineCustomFieldLaneBucketProvider(
        issues_payload=issues, config=CustomFieldBucketConfig(field_name=_FIELD_NAME)
    )


class ConfigTest(unittest.TestCase):
    def test_requires_field_id_or_name(self) -> None:
        with self.assertRaises(LaneBucketError):
            CustomFieldBucketConfig()

    def test_normalizes_blank_to_none(self) -> None:
        with self.assertRaises(LaneBucketError):
            CustomFieldBucketConfig(field_id="  ", field_name="")

    def test_allowed_values_coerced_to_frozenset(self) -> None:
        config = CustomFieldBucketConfig(field_id="5", allowed_values={"a", "b"})
        self.assertIsInstance(config.allowed_values, frozenset)
        self.assertTrue(config.value_allowed("a"))
        self.assertFalse(config.value_allowed("c"))

    def test_no_allow_list_permits_any_value(self) -> None:
        self.assertTrue(CustomFieldBucketConfig(field_id="5").value_allowed("anything"))


class ProviderConformanceTest(unittest.TestCase):
    def test_is_lane_bucket_provider(self) -> None:
        provider = _provider_by_id([])
        self.assertIsInstance(provider, LaneBucketProvider)
        self.assertEqual(provider.source_kind, SOURCE_KIND_CUSTOM_FIELD)


class ResolveBucketTest(unittest.TestCase):
    def _issues(self) -> dict:
        return {
            "issues": [
                _issue(12922, tracker="開発", status="着手中", parent=12670),
                _issue(12930, parent=12922),
                _issue(12931, parent=12922, closed=True, status="終了"),
            ]
        }

    def test_field_by_id(self) -> None:
        resolution = _provider_by_id(self._issues()).resolve_bucket("bucket-a")
        self.assertTrue(resolution.resolved)
        bucket = resolution.bucket
        assert bucket is not None
        self.assertEqual(bucket.bucket_id, "bucket-a")
        self.assertEqual(bucket.name, "bucket-a")
        self.assertEqual(bucket.source_kind, SOURCE_KIND_CUSTOM_FIELD)
        # A custom-field bucket has no Redmine Version status / dates.
        self.assertIsNone(bucket.status)
        self.assertIsNone(bucket.start_date)
        self.assertIsNone(bucket.due_date)
        self.assertEqual(bucket.total_issues, 3)
        self.assertEqual(bucket.total_open, 2)

    def test_field_by_name_resolves_same_bucket(self) -> None:
        by_id = _provider_by_id(self._issues()).resolve_bucket("bucket-a").bucket
        by_name = _provider_by_name(self._issues()).resolve_bucket("bucket-a").bucket
        assert by_id is not None and by_name is not None
        self.assertEqual(by_id.as_dict(), by_name.as_dict())

    def test_open_leaf_enumeration(self) -> None:
        bucket = _provider_by_id(self._issues()).resolve_bucket("bucket-a").bucket
        assert bucket is not None
        # 12922 is an open parent of 12930 -> not a leaf; 12930 is the work leaf;
        # 12931 is closed -> not a leaf.
        self.assertEqual({i.issue_id for i in bucket.open_leaf_issues}, {"12930"})

    def test_single_parent_us_recorded(self) -> None:
        issues = {"issues": [_issue(1, parent=100), _issue(2, parent=100)]}
        bucket = _provider_by_id(issues).resolve_bucket("bucket-a").bucket
        assert bucket is not None
        self.assertFalse(bucket.is_umbrella)
        self.assertEqual(bucket.parent_us, "100")

    def test_bucket_level_umbrella_when_two_parents(self) -> None:
        issues = {"issues": [_issue(1, parent=100), _issue(2, parent=200)]}
        bucket = _provider_by_id(issues).resolve_bucket("bucket-a").bucket
        assert bucket is not None
        self.assertTrue(bucket.is_umbrella)
        self.assertIsNone(bucket.parent_us)

    def test_bare_list_issues_payload(self) -> None:
        bucket = _provider_by_id([_issue(1)]).resolve_bucket("bucket-a").bucket
        assert bucket is not None
        self.assertEqual(bucket.total_open, 1)

    def test_int_custom_field_value_coerced_to_str(self) -> None:
        # A numeric custom-field value is normalized to a string id like the rest.
        issues = {"issues": [_issue(1, bucket=42)]}
        resolution = _provider_by_id(issues).resolve_bucket("42")
        self.assertTrue(resolution.resolved)
        self.assertEqual(resolution.bucket.bucket_id, "42")

    def test_multi_value_issue_excluded_from_bucket(self) -> None:
        # An ambiguous multi-value issue does not silently join a single bucket.
        issues = {
            "issues": [
                _issue(1, bucket="bucket-a"),
                _issue(2, bucket=["bucket-a", "bucket-b"]),
            ]
        }
        bucket = _provider_by_id(issues).resolve_bucket("bucket-a").bucket
        assert bucket is not None
        self.assertEqual({i.issue_id for i in bucket.issues}, {"1"})


class NormalizedShapeCompatibilityTest(unittest.TestCase):
    """The custom-field bucket payload is the same shape dispatch reads for fixed_version."""

    def test_same_payload_keys_as_fixed_version_bucket(self) -> None:
        cf_bucket = _provider_by_id({"issues": [_issue(1, parent=100)]}).resolve_bucket(
            "bucket-a"
        ).bucket
        fv_issues = {
            "issues": [
                {
                    "id": 1,
                    "tracker": {"name": "Task"},
                    "status": {"name": "新規", "is_closed": False},
                    "parent": {"id": 100},
                    "fixed_version": {"id": 292, "name": "枠", "status": "open"},
                }
            ]
        }
        fv_bucket = RedmineFixedVersionLaneBucketProvider(
            issues_payload=fv_issues
        ).resolve_bucket("292").bucket
        assert cf_bucket is not None and fv_bucket is not None
        # Identical record shape -> build_dispatch_plan reads either provider's bucket.
        self.assertEqual(set(cf_bucket.as_dict()), set(fv_bucket.as_dict()))


class FailClosedSkipTest(unittest.TestCase):
    def test_unset_value_issue_bucket(self) -> None:
        provider = _provider_by_id([_issue(1, bucket=None)])
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_NO_EXECUTION_BUCKET
        )

    def test_empty_string_value_is_unset(self) -> None:
        provider = _provider_by_id([_issue(1, bucket="   ")])
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_NO_EXECUTION_BUCKET
        )

    def test_multi_value_issue_bucket_is_ambiguous(self) -> None:
        provider = _provider_by_id([_issue(1, bucket=["a", "b"])])
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_AMBIGUOUS_SOURCE
        )

    def test_duplicate_value_in_list_is_single_not_ambiguous(self) -> None:
        # The same value twice is one distinct value -> resolves, not ambiguous.
        provider = _provider_by_id([_issue(1, bucket=["a", "a"])])
        resolution = provider.resolve_issue_bucket("1")
        self.assertTrue(resolution.resolved)
        self.assertEqual(resolution.bucket.bucket_id, "a")

    def test_disallowed_value_fails_closed(self) -> None:
        provider = RedmineCustomFieldLaneBucketProvider(
            issues_payload=[_issue(1, bucket="bucket-a")],
            config=CustomFieldBucketConfig(
                field_id=_FIELD_ID, allowed_values=frozenset({"bucket-b"})
            ),
        )
        self.assertEqual(
            provider.resolve_bucket("bucket-a").skip.reason, SKIP_DISALLOWED_VALUE
        )

    def test_allowed_value_resolves(self) -> None:
        provider = RedmineCustomFieldLaneBucketProvider(
            issues_payload=[_issue(1, bucket="bucket-a")],
            config=CustomFieldBucketConfig(
                field_id=_FIELD_ID, allowed_values=frozenset({"bucket-a"})
            ),
        )
        self.assertTrue(provider.resolve_bucket("bucket-a").resolved)

    def test_disallowed_value_via_issue_bucket(self) -> None:
        provider = RedmineCustomFieldLaneBucketProvider(
            issues_payload=[_issue(1, bucket="bucket-a")],
            config=CustomFieldBucketConfig(
                field_id=_FIELD_ID, allowed_values=frozenset({"bucket-b"})
            ),
        )
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_DISALLOWED_VALUE
        )

    def test_unknown_bucket(self) -> None:
        self.assertEqual(
            _provider_by_id([_issue(1)]).resolve_bucket("missing").skip.reason,
            SKIP_BUCKET_NOT_FOUND,
        )

    def test_empty_bucket_value(self) -> None:
        self.assertEqual(
            _provider_by_id([_issue(1)]).resolve_bucket("  ").skip.reason,
            SKIP_AMBIGUOUS_SOURCE,
        )

    def test_closed_issue(self) -> None:
        provider = _provider_by_id([_issue(1, closed=True, status="終了")])
        self.assertEqual(
            provider.resolve_issue_bucket("1").skip.reason, SKIP_ISSUE_CLOSED
        )

    def test_issue_absent_from_snapshot(self) -> None:
        provider = _provider_by_id([_issue(1)])
        self.assertEqual(
            provider.resolve_issue_bucket("777").skip.reason, SKIP_AMBIGUOUS_SOURCE
        )

    def test_open_issue_resolves_to_its_bucket(self) -> None:
        resolution = _provider_by_id([_issue(1, bucket="bucket-a")]).resolve_issue_bucket("1")
        self.assertTrue(resolution.resolved)
        self.assertEqual(resolution.bucket.bucket_id, "bucket-a")


class ResolveExecutionBucketTest(unittest.TestCase):
    def test_umbrella_parent_children_span_buckets(self) -> None:
        issues = {
            "issues": [
                _issue(100, bucket="parent-bucket"),
                _issue(1, parent=100, bucket="bucket-a"),
                _issue(2, parent=100, bucket="bucket-b"),
            ]
        }
        decision = _provider_by_id(issues).resolve_execution_bucket("100")
        self.assertTrue(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ("bucket-a", "bucket-b"))
        self.assertEqual(decision.parent_bucket, "parent-bucket")
        self.assertEqual(decision.execution_bucket_for("1"), "bucket-a")
        self.assertEqual(decision.execution_bucket_for("2"), "bucket-b")

    def test_single_bucket_parent_not_umbrella(self) -> None:
        issues = {
            "issues": [
                _issue(100, bucket="bucket-a"),
                _issue(1, parent=100, bucket="bucket-a"),
                _issue(2, parent=100, bucket="bucket-a"),
            ]
        }
        decision = _provider_by_id(issues).resolve_execution_bucket("100")
        self.assertFalse(decision.is_umbrella)
        self.assertEqual(decision.child_buckets, ("bucket-a",))

    def test_unset_child_contributes_none(self) -> None:
        issues = {
            "issues": [
                _issue(1, parent=100, bucket="bucket-a"),
                _issue(2, parent=100, bucket=None),
            ]
        }
        decision = _provider_by_id(issues).resolve_execution_bucket("100")
        self.assertFalse(decision.is_umbrella)
        self.assertIsNone(decision.execution_bucket_for("2"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
