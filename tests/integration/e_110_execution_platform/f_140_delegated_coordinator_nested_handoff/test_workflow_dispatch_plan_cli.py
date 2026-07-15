"""`workflow dispatch-plan` CLI integration tests (Redmine #12920).

Covers the Version-bucket lane-set dispatch plan command surface:

- the subcommand registers under the ``workflow`` family;
- a bucket snapshot enumerates open leaf candidates and skips closed / non-leaf issues;
- a candidate with a file overlap against an active lane is ``standby``; a release-gate
  candidate is ``needs_owner_decision``;
- the coordinator-owned queue (review / owner / integration waiting) is projected from
  ``--lane-signal`` lanes;
- ``--json`` emits one structured envelope and ``--journal`` emits the markdown;
- a closed/missing Version yields an unresolved plan (still exit 0);
- ``--bucket-id`` / ``--issues-json`` are required and a missing snapshot fails closed;
- the command never mutates: a dry-run / execute run reads only the supplied files.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_dispatch_plan,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.live_fixed_version_bucket import (
    LIVE_VERSION_NOT_OPEN,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    RedmineVersionReadUnavailable,
)

_ISSUES = {
    "issues": [
        {
            "id": 1,
            "tracker": {"name": "開発"},
            "status": {"name": "New", "is_closed": False},
            "fixed_version": {"id": 292, "name": "枠", "status": "open"},
            "parent": {"id": 99},
        },
        {
            "id": 2,
            "tracker": {"name": "Task"},
            "status": {"name": "New", "is_closed": False},
            "fixed_version": {"id": 292},
            "parent": {"id": 99},
        },
        {
            "id": 99,
            "tracker": {"name": "User Story"},
            "status": {"name": "New", "is_closed": False},
            "fixed_version": {"id": 292},
        },
        {
            "id": 3,
            "tracker": {"name": "Task"},
            "status": {"name": "Closed", "is_closed": True},
            "fixed_version": {"id": 292},
        },
    ]
}


def _write(tmp: Path, name: str, payload) -> str:
    path = tmp / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_dispatch_plan_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--issues-json", "x.json"]
        )
        self.assertIs(
            ns.func, cli_workflow_dispatch_plan.cmd_workflow_dispatch_plan
        )


class PlanTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.issues = _write(self.tmp, "issues.json", _ISSUES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_enumerates_and_classifies(self):
        rc, out = _run(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--issues-json", self.issues]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: true", out)
        self.assertIn("candidate: 1", out)
        self.assertIn("candidate: 2", out)
        # closed + non-leaf are skipped, not dispatched.
        self.assertIn("skipped: 99 -> not_leaf", out)
        self.assertIn("skipped: 3 -> issue_closed", out)

    def test_file_overlap_is_standby(self):
        facts = _write(self.tmp, "facts.json", {"2": {"file_overlap": ["500"]}})
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--candidate-facts",
                facts,
                "--lane-signal",
                "500:start",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("candidate: 2", out)
        self.assertIn("standby", out)

    def test_queue_projection(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--lane-signal",
                "500:review_request",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("review_waiting=['500']", out)

    def test_json_envelope(self):
        facts = _write(self.tmp, "facts.json", {"1": {"release_publish_gate": True}})
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--candidate-facts",
                facts,
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["bucket_id"], "292")
        self.assertTrue(payload["resolved"])
        by_id = {c["issue_id"]: c for c in payload["candidates"]}
        self.assertEqual(by_id["1"]["classification"], "needs_owner_decision")
        self.assertEqual(
            payload["recommended_route"],
            "coordinator_codex -> sublane_codex_gateway -> same_lane_claude",
        )

    def test_journal_markdown(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--journal",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("## Lane-set dispatch plan", out)
        self.assertIn("counts_by_classification:", out)

    def test_resolve_by_name(self):
        # Version name resolves from the issues' embedded fixed_version name ("枠"),
        # even without a --versions-json snapshot (acceptance: id/name selector).
        rc, out = _run(
            ["workflow", "dispatch-plan", "--bucket-name", "枠", "--issues-json", self.issues]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: true", out)
        self.assertIn("bucket_id: 292", out)
        self.assertIn("candidate: 1", out)

    def test_unknown_name_is_unresolved(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-name",
                "存在しない枠",
                "--issues-json",
                self.issues,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: bucket_not_found", out)

    def test_ambiguous_name_fails_closed(self):
        versions = _write(
            self.tmp,
            "versions.json",
            {"versions": [{"id": 292, "name": "重複枠"}, {"id": 293, "name": "重複枠"}]},
        )
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-name",
                "重複枠",
                "--issues-json",
                self.issues,
                "--versions-json",
                versions,
            ]
        )
        self.assertEqual(rc, 0)
        # An ambiguous name is never guessed -> unresolved, ambiguous_source skip.
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: ambiguous_source", out)
        self.assertIn("292", out)
        self.assertIn("293", out)

    def test_missing_bucket_is_unresolved(self):
        rc, out = _run(
            ["workflow", "dispatch-plan", "--bucket-id", "999", "--issues-json", self.issues]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: bucket_not_found", out)

    def test_execute_mode_is_read_only(self):
        # execute records intent only; the plan is identical and nothing is mutated.
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--mode",
                "execute",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("mode: execute", out)
        self.assertIn("candidate: 1", out)

    def test_missing_snapshot_fails_closed(self):
        with self.assertRaises(SystemExit):
            _run(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-id",
                    "292",
                    "--issues-json",
                    str(self.tmp / "nope.json"),
                ]
            )


_CF_ISSUES = {
    "issues": [
        {
            "id": 1,
            "tracker": {"name": "開発"},
            "status": {"name": "New", "is_closed": False},
            "parent": {"id": 99},
            "custom_fields": [{"id": 5, "name": "execution_bucket", "value": "bucket-a"}],
        },
        {
            "id": 2,
            "tracker": {"name": "Task"},
            "status": {"name": "New", "is_closed": False},
            "parent": {"id": 99},
            "custom_fields": [{"id": 5, "name": "execution_bucket", "value": "bucket-a"}],
        },
        {
            "id": 99,
            "tracker": {"name": "User Story"},
            "status": {"name": "New", "is_closed": False},
            "custom_fields": [{"id": 5, "name": "execution_bucket", "value": "bucket-a"}],
        },
        {
            "id": 3,
            "tracker": {"name": "Task"},
            "status": {"name": "Closed", "is_closed": True},
            "custom_fields": [{"id": 5, "name": "execution_bucket", "value": "bucket-a"}],
        },
    ]
}


class CustomFieldSourceTest(unittest.TestCase):
    """`--bucket-source custom-field` execution-bucket provider selection (Redmine #12922)."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.issues = _write(self.tmp, "cf_issues.json", _CF_ISSUES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_custom_field_by_id_enumerates_same_as_version(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-source",
                "custom-field",
                "--custom-field-id",
                "5",
                "--bucket-id",
                "bucket-a",
                "--issues-json",
                self.issues,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: true", out)
        self.assertIn("source_kind: custom_field", out)
        self.assertIn("candidate: 1", out)
        self.assertIn("candidate: 2", out)
        self.assertIn("skipped: 99 -> not_leaf", out)
        self.assertIn("skipped: 3 -> issue_closed", out)

    def test_custom_field_by_name_json_envelope(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-source",
                "custom-field",
                "--custom-field-name",
                "execution_bucket",
                "--bucket-name",
                "bucket-a",
                "--issues-json",
                self.issues,
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["bucket_id"], "bucket-a")
        self.assertEqual(payload["source_kind"], "custom_field")
        self.assertTrue(payload["resolved"])
        # Same normalized plan shape as the fixed_version source -> same candidate fields.
        by_id = {c["issue_id"]: c for c in payload["candidates"]}
        self.assertEqual(by_id["1"]["classification"], "dispatchable")
        self.assertEqual(
            payload["recommended_route"],
            "coordinator_codex -> sublane_codex_gateway -> same_lane_claude",
        )

    def test_disallowed_value_is_unresolved(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-source",
                "custom-field",
                "--custom-field-id",
                "5",
                "--allowed-bucket",
                "bucket-b",
                "--bucket-id",
                "bucket-a",
                "--issues-json",
                self.issues,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: disallowed_value", out)

    def test_allowed_value_resolves(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-source",
                "custom-field",
                "--custom-field-id",
                "5",
                "--allowed-bucket",
                "bucket-a",
                "--bucket-id",
                "bucket-a",
                "--issues-json",
                self.issues,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: true", out)

    def test_unknown_value_is_unresolved(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-source",
                "custom-field",
                "--custom-field-id",
                "5",
                "--bucket-id",
                "missing-bucket",
                "--issues-json",
                self.issues,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: bucket_not_found", out)

    def test_custom_field_source_without_field_selector_fails_closed(self):
        with self.assertRaises(SystemExit):
            _run(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-source",
                    "custom-field",
                    "--bucket-id",
                    "bucket-a",
                    "--issues-json",
                    self.issues,
                ]
            )

    def test_default_source_is_fixed_version(self):
        # No --bucket-source: the default fixed-version provider runs (custom_fields ignored).
        parser = build_parser()
        ns = parser.parse_args(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--issues-json", "x.json"]
        )
        self.assertEqual(ns.bucket_source, "fixed-version")


class ParsingTest(unittest.TestCase):
    def test_a_bucket_selector_is_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "dispatch-plan", "--issues-json", "x.json"])

    def test_bucket_id_and_name_are_mutually_exclusive(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "workflow",
                    "dispatch-plan",
                    "--issues-json",
                    "x.json",
                    "--bucket-id",
                    "292",
                    "--bucket-name",
                    "枠",
                ]
            )

    def test_issues_json_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "dispatch-plan", "--bucket-id", "292"])

    def test_malformed_lane_signal_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-id",
                    "292",
                    "--issues-json",
                    "x.json",
                    "--lane-signal",
                    "noselector",
                ]
            )


class LiveRedmineModeTest(unittest.TestCase):
    """`--live-redmine` (Redmine #13687 Increment 1): explicit opt-in, fail-closed.

    The live *read* itself is unit-tested at the f_120 composition boundary against an
    injected opener; here we pin the CLI contract on top of it: the flag is exclusive with
    the snapshot input, a blocked read exits 2 with its reason on stderr and **no plan on
    stdout** (so "could not look" is never rendered as "no work"), and the combinations the
    live path cannot honour are refused explicitly rather than resolving to an empty bucket.
    """

    _READ = (
        "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
        ".application.cli_workflow_dispatch_plan.read_live_fixed_version_bucket"
    )

    def _live_read(self):
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.live_fixed_version_bucket import (
            LiveBucketRead,
        )
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.fixed_version_lane_bucket_provider import (
            RedmineFixedVersionLaneBucketProvider,
        )

        return LiveBucketRead(
            provider=RedmineFixedVersionLaneBucketProvider(
                issues_payload=_ISSUES,
                versions_payload={
                    "versions": [{"id": 292, "name": "枠", "status": "open"}]
                },
            ),
            project_identifier="giken-3800-mozyo-bridge",
            project_id=38,
            version_id="292",
            version_name="枠",
            issue_count=4,
        )

    def _run_live(self, argv, *, side_effect=None, return_value=None):
        err = io.StringIO()
        with mock.patch(
            self._READ, side_effect=side_effect, return_value=return_value
        ) as read, contextlib.redirect_stderr(err):
            code, out = _run(argv)
        return code, out, err.getvalue(), read

    def test_live_read_plans_through_the_pure_planner(self):
        code, out, _, read = self._run_live(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--live-redmine", "--json"],
            return_value=self._live_read(),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["resolved"])
        self.assertEqual(payload["bucket_id"], "292")
        # The selector reaches the read; the repo defaults supply the project scope.
        self.assertEqual(read.call_args.kwargs["bucket_id"], "292")
        self.assertIsNone(read.call_args.kwargs["bucket_name"])

    def test_explicit_repo_is_passed_to_the_live_read(self):
        _, _, _, read = self._run_live(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-name",
                "枠",
                "--live-redmine",
                "--repo",
                "/some/repo",
            ],
            return_value=self._live_read(),
        )
        self.assertEqual(read.call_args.kwargs["repo_root"], Path("/some/repo"))
        self.assertEqual(read.call_args.kwargs["bucket_name"], "枠")

    def test_blocked_live_read_exits_2_with_its_reason_and_no_plan(self):
        blocked = RedmineVersionReadUnavailable(
            "version #292 status is 'closed', not 'open'",
            reason=LIVE_VERSION_NOT_OPEN,
        )
        code, out, err, _ = self._run_live(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--live-redmine", "--json"],
            side_effect=blocked,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")  # never a "0 candidates" plan on a read we could not do
        self.assertIn(LIVE_VERSION_NOT_OPEN, err)

    def test_custom_field_bucket_source_is_refused_live(self):
        # A live issues read sends no include=, so custom-field values are not guaranteed
        # present; resolving them live would look like an empty bucket ("no work").
        code, out, err, read = self._run_live(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "bucket-a",
                "--live-redmine",
                "--bucket-source",
                "custom-field",
                "--custom-field-id",
                "5",
            ],
            return_value=self._live_read(),
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(cli_workflow_dispatch_plan.LIVE_UNSUPPORTED_BUCKET_SOURCE, err)
        read.assert_not_called()  # refused before any network reach

    def test_versions_json_snapshot_cannot_be_combined_with_live(self):
        # A stale Version status is more dangerous than none: it can render a closed
        # Version as open, which is exactly the gate the live read exists to enforce.
        with tempfile.TemporaryDirectory() as tmp:
            versions = _write(Path(tmp), "versions.json", {"versions": []})
            code, out, err, read = self._run_live(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-id",
                    "292",
                    "--live-redmine",
                    "--versions-json",
                    versions,
                ],
                return_value=self._live_read(),
            )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(cli_workflow_dispatch_plan.LIVE_SNAPSHOT_INPUT_CONFLICT, err)
        read.assert_not_called()

    def test_live_and_issues_json_are_mutually_exclusive(self):
        parser = build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-id",
                    "292",
                    "--issues-json",
                    "x.json",
                    "--live-redmine",
                ]
            )

    def test_a_source_is_still_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["workflow", "dispatch-plan", "--bucket-id", "292"])

    def test_snapshot_mode_never_reaches_the_live_read(self):
        # The opt-in contract: without --live-redmine there is no network reach and no
        # credential use, whatever else is passed.
        with tempfile.TemporaryDirectory() as tmp:
            issues = _write(Path(tmp), "issues.json", _ISSUES)
            with mock.patch(self._READ) as read:
                code, out = _run(
                    ["workflow", "dispatch-plan", "--bucket-id", "292", "--issues-json", issues]
                )
        self.assertEqual(code, 0)
        self.assertIn("resolved: true", out)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
