"""Unit pins for the managed-lane herdr plugin policy (Redmine #14619).

Covers the pure classification model (capability class, supply-chain provenance,
the pinned-allow / repository-deny asymmetry, fail-closed unknown) and the ops /
CLI edge (read-only inventory query, malformed records, path non-disclosure,
exit semantics).

No test touches a real plugin, the operator config root, the network, or a live
herdr binary: the inventory is always an injected document, and the one
subprocess path is exercised through a patched ``subprocess.run`` that also
serves as the guard proving this surface issues no mutating herdr subcommand.
Every path literal below is an abstract placeholder, never an operator path.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_policy import (  # noqa: E402
    BUILD_NONE,
    BUILD_REMOTE_ARTIFACT,
    BUILD_SOURCE_ONLY,
    BUILD_UNREVIEWED,
    CLASS_AGENT_INPUT_WRITER,
    CLASS_TEST_ORACLE,
    CLASS_UNKNOWN,
    CLASS_UX_ONLY,
    DENY_REASONS,
    ENABLE_SCOPE,
    FORBIDDEN_PLUGIN_AUTHORITIES,
    REASON_AGENT_INPUT_WRITER,
    REASON_IDENTITY_MISMATCH,
    REASON_MANIFEST_DRIFT,
    REASON_NO_LANE_AUTHORITY,
    REASON_UNPINNED_REMOTE_BUILD,
    REASON_UNPINNED_SOURCE,
    REASON_UNREVIEWED_BUILD,
    REASON_UNREVIEWED_PIN,
    REVIEWED_PLUGINS,
    SOURCE_KIND_GITHUB,
    HerdrPluginPolicyError,
    PluginObservation,
    PluginSourceRef,
    PolicyDecision,
    ReviewedPlugin,
    build_review_registry,
    classify_plugin,
    observe_plugin,
    plan_install,
    resolve_review,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402
    herdr_plugin_policy_ops as ops,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_distribution import (  # noqa: E402
    cmd_herdr_plugin_policy,
    register_herdr_plugin_policy_parser,
)

# The reviewed identities, restated here rather than imported as a pair with the
# registry: a test that reads its expectation out of the thing under test cannot
# catch the registry changing.
FILE_VIEWER_COMMIT = "96fcc0a2bdd2727ec88c38f8c8806f97b7ca0ea0"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"

#: Abstract placeholder paths standing in for the three absolute operator-home
#: paths herdr's real payload carries. Never an operator path.
PLACEHOLDER_ROOT = "/opt/example-config/herdr/plugins/github/example-plugin-0000"
PLACEHOLDER_MANIFEST = f"{PLACEHOLDER_ROOT}/herdr-plugin.toml"

#: A POSIX absolute-path token: a slash immediately followed by a path segment.
#: ``HOME / XDG_CONFIG_HOME`` in prose does not match (the slash stands alone).
_ABS_PATH_TOKEN = re.compile(r"/[A-Za-z0-9._-]+/")


def plugin_record(
    *,
    plugin_id: str = "herdr-file-viewer",
    version: str = "1.14.0",
    enabled: bool = True,
    owner: str = "smarzban",
    repo: str = "herdr-file-viewer",
    commit: str = FILE_VIEWER_COMMIT,
    kind: str = SOURCE_KIND_GITHUB,
    build: bool = True,
    source: object = ...,
) -> dict:
    """A herdr ``plugin list --json`` plugin record, shaped like the real payload."""
    record: dict = {
        "plugin_id": plugin_id,
        "name": plugin_id,
        "version": version,
        "enabled": enabled,
        "manifest_path": PLACEHOLDER_MANIFEST,
        "plugin_root": PLACEHOLDER_ROOT,
        "panes": [{"id": "viewer", "command": ["./target/release/viewer"]}],
        "actions": [{"id": "open", "command": ["bash", "scripts/open.sh"]}],
    }
    if build:
        record["build"] = [{"command": ["/bin/sh", "scripts/fetch-or-build.sh"]}]
    record["source"] = (
        {
            "kind": kind,
            "owner": owner,
            "repo": repo,
            "resolved_commit": commit,
            "managed_path": PLACEHOLDER_ROOT,
        }
        if source is ...
        else source
    )
    return record


def inventory_document(*records: object) -> str:
    """The full ``herdr plugin list --json`` envelope around ``records``."""
    return json.dumps(
        {
            "id": "cli:plugin",
            "result": {"plugins": list(records), "type": "plugin_list"},
        }
    )


class SourceRefTests(unittest.TestCase):
    """A source reference is an identity, so its fields are validated as one."""

    def test_full_lowercase_hex_commit_is_a_pin(self):
        ref = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "smarzban", "herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        self.assertTrue(ref.is_pinned)
        self.assertEqual(
            ref.describe(),
            f"github:smarzban/herdr-file-viewer@{FILE_VIEWER_COMMIT}",
        )

    def test_abbreviated_commit_is_refused(self):
        # An abbreviated commit cannot be compared for equality with a full one, so
        # accepting it would make the pin unfalsifiable rather than merely shorter.
        with self.assertRaises(HerdrPluginPolicyError):
            PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", FILE_VIEWER_COMMIT[:8])

    def test_uppercase_commit_is_refused(self):
        with self.assertRaises(HerdrPluginPolicyError):
            PluginSourceRef.pinned(
                SOURCE_KIND_GITHUB, "o", "r", FILE_VIEWER_COMMIT.upper()
            )

    def test_non_string_commit_is_refused_before_any_shape_check(self):
        for value in (True, 96, ["a"], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(HerdrPluginPolicyError):
                    PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", value)

    def test_owner_or_repo_carrying_a_separator_is_refused(self):
        # A segment that can hold a '/' can hold a second path component, which is
        # how an identity turns into a path.
        for owner, repo in (("a/b", "r"), ("o", "r/s"), ("o ", "r"), ("", "r")):
            with self.subTest(owner=owner, repo=repo):
                with self.assertRaises(HerdrPluginPolicyError):
                    PluginSourceRef.repository(SOURCE_KIND_GITHUB, owner, repo)

    def test_non_github_kind_is_not_a_pinnable_identity(self):
        with self.assertRaises(HerdrPluginPolicyError):
            PluginSourceRef.repository("link", "o", "r")

    def test_repo_key_drops_the_commit(self):
        ref = PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", FILE_VIEWER_COMMIT)
        self.assertEqual(ref.repo_key, PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r"))


class RegistryInvariantTests(unittest.TestCase):
    """The asymmetry that keeps a repository-scoped entry from widening an allow."""

    def _entry(self, ref, plugin_class, provenance=BUILD_NONE) -> ReviewedPlugin:
        return ReviewedPlugin(
            ref=ref,
            plugin_id="p",
            plugin_class=plugin_class,
            build_provenance=provenance,
            review_anchor="#0 j#0",
            rationale="fixture",
        )

    def test_repository_scoped_allow_is_refused(self):
        # The guard-breaking probe: this is exactly the construction that would let
        # an allow extend to every future upstream commit.
        with self.assertRaises(HerdrPluginPolicyError):
            self._entry(
                PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r"), CLASS_UX_ONLY
            )

    def test_repository_scoped_deny_is_allowed(self):
        for deny_class in (CLASS_TEST_ORACLE, CLASS_AGENT_INPUT_WRITER):
            with self.subTest(deny_class=deny_class):
                entry = self._entry(
                    PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r"), deny_class
                )
                self.assertEqual(entry.plugin_class, deny_class)

    def test_unknown_class_is_not_recordable(self):
        with self.assertRaises(HerdrPluginPolicyError):
            self._entry(
                PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT),
                CLASS_UNKNOWN,
            )

    def test_entry_without_a_review_anchor_is_refused(self):
        with self.assertRaises(HerdrPluginPolicyError):
            ReviewedPlugin(
                ref=PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT),
                plugin_id="p",
                plugin_class=CLASS_UX_ONLY,
                build_provenance=BUILD_NONE,
                review_anchor="   ",
                rationale="fixture",
            )

    def test_duplicate_reference_is_refused(self):
        ref = PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT)
        with self.assertRaises(HerdrPluginPolicyError):
            build_review_registry(
                (self._entry(ref, CLASS_UX_ONLY), self._entry(ref, CLASS_UX_ONLY))
            )

    def test_repository_cannot_be_both_deny_classified_and_pinned_allowed(self):
        with self.assertRaises(HerdrPluginPolicyError):
            build_review_registry(
                (
                    self._entry(
                        PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r"),
                        CLASS_AGENT_INPUT_WRITER,
                    ),
                    self._entry(
                        PluginSourceRef.pinned(
                            SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT
                        ),
                        CLASS_UX_ONLY,
                    ),
                )
            )

    def test_shipped_registry_holds_the_three_reviewed_projects(self):
        described = {ref.describe() for ref in REVIEWED_PLUGINS}
        self.assertEqual(
            described,
            {
                f"github:smarzban/herdr-file-viewer@{FILE_VIEWER_COMMIT}",
                "github:yuk1ty/herdr-spreader",
                "github:persiyanov/herdr-reviewr",
            },
        )


class ClassificationTests(unittest.TestCase):
    """The close conditions, as decisions rather than prose."""

    def test_file_viewer_at_the_reviewed_pin_is_ux_only_and_enable_admitted(self):
        verdict = classify_plugin(observe_plugin(plugin_record()))
        self.assertEqual(verdict.plugin_class, CLASS_UX_ONLY)
        self.assertTrue(verdict.enable.admitted)
        self.assertFalse(verdict.breach)

    def test_file_viewer_install_is_denied_for_its_unpinned_remote_build(self):
        # The two axes disagree on purpose: a benign capability whose install
        # fetches a remote artifact keyed by version, not by the pinned commit.
        verdict = classify_plugin(observe_plugin(plugin_record()))
        self.assertEqual(verdict.build_provenance, BUILD_REMOTE_ARTIFACT)
        self.assertFalse(verdict.install.admitted)
        self.assertEqual(verdict.install.reason, REASON_UNPINNED_REMOTE_BUILD)

    def test_file_viewer_at_any_other_commit_is_unknown_and_denied(self):
        verdict = classify_plugin(observe_plugin(plugin_record(commit=OTHER_COMMIT)))
        self.assertEqual(verdict.plugin_class, CLASS_UNKNOWN)
        self.assertEqual(verdict.enable.reason, REASON_UNREVIEWED_PIN)
        self.assertEqual(verdict.install.reason, REASON_UNREVIEWED_PIN)

    def test_spreader_is_a_test_oracle_with_no_lane_authority(self):
        # Its build provenance is unreviewed, which asserts nothing about whether a
        # build exists — so neither manifest shape may be reported as drift, and
        # both must land on the reason that is actually true.
        for build in (False, True):
            with self.subTest(build=build):
                verdict = classify_plugin(
                    observe_plugin(
                        plugin_record(
                            plugin_id="herdr-spreader",
                            owner="yuk1ty",
                            repo="herdr-spreader",
                            commit=OTHER_COMMIT,
                            build=build,
                        )
                    )
                )
                self.assertEqual(verdict.plugin_class, CLASS_TEST_ORACLE)
                self.assertEqual(verdict.build_provenance, BUILD_UNREVIEWED)
                self.assertEqual(verdict.enable.reason, REASON_NO_LANE_AUTHORITY)

    def test_reviewr_is_denied_at_every_commit(self):
        # A repository-scoped deny is what makes this true for a commit nobody has
        # reviewed; a commit-pinned deny would fall through to unknown instead.
        for commit in (OTHER_COMMIT, FILE_VIEWER_COMMIT):
            with self.subTest(commit=commit):
                verdict = classify_plugin(
                    observe_plugin(
                        plugin_record(
                            plugin_id="herdr-reviewr",
                            owner="persiyanov",
                            repo="herdr-reviewr",
                            commit=commit,
                        )
                    )
                )
                self.assertEqual(verdict.plugin_class, CLASS_AGENT_INPUT_WRITER)
                self.assertEqual(verdict.enable.reason, REASON_AGENT_INPUT_WRITER)
                self.assertFalse(verdict.install.admitted)

    def test_an_unreviewed_plugin_is_unknown_and_denied_on_both_axes(self):
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(
                    plugin_id="some-other-plugin",
                    owner="someone",
                    repo="some-other-plugin",
                    commit=OTHER_COMMIT,
                )
            )
        )
        self.assertEqual(verdict.plugin_class, CLASS_UNKNOWN)
        self.assertFalse(verdict.enable.admitted)
        self.assertFalse(verdict.install.admitted)

    def test_a_locally_linked_plugin_has_no_identity_and_is_denied(self):
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(source={"kind": "link", "managed_path": PLACEHOLDER_ROOT})
            )
        )
        self.assertEqual(verdict.enable.reason, REASON_UNPINNED_SOURCE)
        self.assertIsNone(verdict.observation.ref)

    def test_a_masquerading_manifest_is_an_identity_mismatch(self):
        # Same reviewed source pin, but the local manifest now calls itself
        # something else — the bytes on disk are not what was reviewed.
        verdict = classify_plugin(
            observe_plugin(plugin_record(plugin_id="herdr-something-else"))
        )
        self.assertEqual(verdict.enable.reason, REASON_IDENTITY_MISMATCH)
        self.assertEqual(verdict.install.reason, REASON_IDENTITY_MISMATCH)

    def test_a_build_step_appearing_after_review_is_manifest_drift(self):
        # The commit pin fixes what upstream published, not the bytes left in the
        # operator's plugin directory afterwards. Needs a review that recorded a
        # build-less manifest, which nothing shipped has.
        ref = PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT)
        entry = ReviewedPlugin(
            ref=ref,
            plugin_id="p",
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_NONE,
            review_anchor="#0 j#0",
            rationale="fixture",
        )
        clean = plugin_record(
            plugin_id="p", owner="o", repo="r", commit=OTHER_COMMIT, build=False
        )
        drifted = dict(clean, build=[{"command": ["/bin/sh", "x.sh"]}])
        with mock.patch.dict(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".domain.herdr_plugin_policy.REVIEWED_PLUGINS",
            {ref: entry},
            clear=True,
        ):
            self.assertTrue(classify_plugin(observe_plugin(clean)).enable.admitted)
            self.assertEqual(
                classify_plugin(observe_plugin(drifted)).enable.reason,
                REASON_MANIFEST_DRIFT,
            )

    def test_a_build_step_disappearing_after_review_is_also_drift(self):
        self.assertEqual(
            classify_plugin(observe_plugin(plugin_record(build=False))).enable.reason,
            REASON_MANIFEST_DRIFT,
        )

    def test_every_deny_reason_comes_from_the_closed_vocabulary(self):
        records = [
            plugin_record(),
            plugin_record(commit=OTHER_COMMIT),
            plugin_record(plugin_id="x"),
            plugin_record(source={"kind": "link"}),
            plugin_record(build=False),
        ]
        for record in records:
            verdict = classify_plugin(observe_plugin(record))
            for decision in (verdict.enable, verdict.install):
                with self.subTest(record=record["plugin_id"], reason=decision.reason):
                    if decision.admitted:
                        self.assertIsNone(decision.reason)
                    else:
                        self.assertIn(decision.reason, DENY_REASONS)


class ObservationTests(unittest.TestCase):
    """Normalization is where a malformed record and a private path are stopped."""

    def test_no_field_can_hold_a_filesystem_path(self):
        # Structural, not a formatting rule: the record has nowhere to put the three
        # absolute paths herdr's payload carries, so no formatter can leak one by
        # forgetting to redact.
        names = {field.name for field in dataclasses.fields(PluginObservation)}
        self.assertEqual(
            names & {"manifest_path", "plugin_root", "managed_path", "path"}, set()
        )

    def test_enabled_must_be_a_real_boolean(self):
        # bool is a subclass of int, so an isinstance(..., int) check would read 1 as
        # True — and this flag is what decides whether a denial is a live breach.
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                record = plugin_record()
                record["enabled"] = value
                with self.assertRaises(HerdrPluginPolicyError):
                    observe_plugin(record)

    def test_missing_or_malformed_plugin_id_is_refused(self):
        for value in (None, "", 7, "has/slash"):
            with self.subTest(value=value):
                record = plugin_record()
                record["plugin_id"] = value
                with self.assertRaises(HerdrPluginPolicyError):
                    observe_plugin(record)

    def test_a_non_list_manifest_surface_is_malformed_not_absent(self):
        # Refusing to read a field is not the same as reading it as empty.
        for key in ("build", "panes", "actions"):
            with self.subTest(key=key):
                record = plugin_record()
                record[key] = "not-a-list"
                with self.assertRaises(HerdrPluginPolicyError):
                    observe_plugin(record)

    def test_an_absent_manifest_surface_reads_as_not_declared(self):
        record = plugin_record(build=False)
        record.pop("panes")
        observation = observe_plugin(record)
        self.assertFalse(observation.declares_build)
        self.assertFalse(observation.declares_panes)
        self.assertTrue(observation.declares_actions)

    def test_a_non_mapping_record_is_refused(self):
        for record in ("plugin", 7, ["plugin"], None):
            with self.subTest(record=record):
                with self.assertRaises(HerdrPluginPolicyError):
                    observe_plugin(record)


class PolicyDecisionTests(unittest.TestCase):
    def test_an_admitted_decision_may_not_carry_a_reason(self):
        with self.assertRaises(HerdrPluginPolicyError):
            PolicyDecision(admitted=True, reason=REASON_UNPINNED_SOURCE)

    def test_a_denied_decision_must_carry_a_known_reason(self):
        for reason in (None, "made_up_reason"):
            with self.subTest(reason=reason):
                with self.assertRaises(HerdrPluginPolicyError):
                    PolicyDecision(admitted=False, reason=reason)


class InstallPlanTests(unittest.TestCase):
    """The supply-chain axis, asked about a plugin that does not exist locally yet."""

    def test_a_candidate_with_no_reference_is_denied(self):
        self.assertEqual(plan_install(None).reason, REASON_UNPINNED_SOURCE)

    def test_a_deny_classified_project_reports_the_project_reason_without_a_commit(self):
        # Reporting "you did not name a commit" here would invite the operator to
        # supply one and be denied again.
        decision = plan_install(
            PluginSourceRef.repository(SOURCE_KIND_GITHUB, "yuk1ty", "herdr-spreader")
        )
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason, REASON_UNREVIEWED_BUILD)

    def test_an_unreviewed_repository_without_a_commit_is_unpinned(self):
        decision = plan_install(
            PluginSourceRef.repository(SOURCE_KIND_GITHUB, "someone", "some-plugin")
        )
        self.assertEqual(decision.reason, REASON_UNPINNED_SOURCE)

    def test_an_unreviewed_pinned_candidate_is_denied_as_unreviewed(self):
        decision = plan_install(
            PluginSourceRef.pinned(
                SOURCE_KIND_GITHUB, "someone", "some-plugin", OTHER_COMMIT
            )
        )
        self.assertEqual(decision.reason, REASON_UNREVIEWED_PIN)

    def test_a_reviewed_build_less_project_would_be_admissible(self):
        # Nothing shipped has this provenance today; the pin proves the admit path
        # exists and is reachable, so the deny cases above are not vacuous.
        ref = PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT)
        entry = ReviewedPlugin(
            ref=ref,
            plugin_id="p",
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_SOURCE_ONLY,
            review_anchor="#0 j#0",
            rationale="fixture",
        )
        with mock.patch.dict(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".domain.herdr_plugin_policy.REVIEWED_PLUGINS",
            {ref: entry},
            clear=True,
        ):
            self.assertTrue(plan_install(ref).admitted)

    def test_resolve_review_finds_the_repository_deny_for_an_unreviewed_commit(self):
        ref = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "persiyanov", "herdr-reviewr", OTHER_COMMIT
        )
        review = resolve_review(ref)
        self.assertIsNotNone(review)
        self.assertEqual(review.plugin_class, CLASS_AGENT_INPUT_WRITER)

    def test_a_repository_deny_outranks_a_pinned_allow_for_the_same_repository(self):
        # ``build_review_registry`` rejects this pairing, so the resolution ORDER is
        # unobservable through any registry the constructor would accept — which is
        # exactly why it needs pinning here. Without it the docstring's "the deny is
        # consulted first" would be a claim no test could falsify, and a later edit
        # could reverse it while every other test stayed green.
        repo_ref = PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r")
        pinned_ref = PluginSourceRef.pinned(SOURCE_KIND_GITHUB, "o", "r", OTHER_COMMIT)
        with self.assertRaises(HerdrPluginPolicyError):
            build_review_registry(
                (
                    ReviewedPlugin(
                        ref=repo_ref,
                        plugin_id="p",
                        plugin_class=CLASS_AGENT_INPUT_WRITER,
                        build_provenance=BUILD_NONE,
                        review_anchor="#0 j#0",
                        rationale="fixture",
                    ),
                    ReviewedPlugin(
                        ref=pinned_ref,
                        plugin_id="p",
                        plugin_class=CLASS_UX_ONLY,
                        build_provenance=BUILD_NONE,
                        review_anchor="#0 j#0",
                        rationale="fixture",
                    ),
                )
            )
        conflicting = {
            repo_ref: ReviewedPlugin(
                ref=repo_ref,
                plugin_id="p",
                plugin_class=CLASS_AGENT_INPUT_WRITER,
                build_provenance=BUILD_NONE,
                review_anchor="#0 j#0",
                rationale="fixture",
            ),
            pinned_ref: ReviewedPlugin(
                ref=pinned_ref,
                plugin_id="p",
                plugin_class=CLASS_UX_ONLY,
                build_provenance=BUILD_NONE,
                review_anchor="#0 j#0",
                rationale="fixture",
            ),
        }
        with mock.patch.dict(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".domain.herdr_plugin_policy.REVIEWED_PLUGINS",
            conflicting,
            clear=True,
        ):
            self.assertEqual(
                resolve_review(pinned_ref).plugin_class, CLASS_AGENT_INPUT_WRITER
            )


class AuthorityBoundaryTests(unittest.TestCase):
    """No plugin class is ever a route to a core-owned authority."""

    def test_forbidden_authorities_cover_the_lane_concerns_this_issue_names(self):
        for authority in (
            "workflow_authority",
            "owner_approval",
            "close_approval",
            "review_authority",
            "routing_authority",
            "send_safety",
            "delivery_authority",
            "durable_anchor_authority",
            "lane_identity",
            "retire_authority",
        ):
            with self.subTest(authority=authority):
                self.assertIn(authority, FORBIDDEN_PLUGIN_AUTHORITIES)

    def test_no_reviewed_entry_grants_an_authority(self):
        for entry in REVIEWED_PLUGINS.values():
            with self.subTest(plugin_id=entry.plugin_id):
                self.assertNotIn(entry.plugin_class, FORBIDDEN_PLUGIN_AUTHORITIES)

    def test_only_the_ux_only_class_can_be_admitted_for_enable(self):
        admitted_classes = set()
        for record in (
            plugin_record(),
            plugin_record(
                plugin_id="herdr-spreader",
                owner="yuk1ty",
                repo="herdr-spreader",
                commit=OTHER_COMMIT,
                build=False,
            ),
            plugin_record(
                plugin_id="herdr-reviewr",
                owner="persiyanov",
                repo="herdr-reviewr",
                commit=OTHER_COMMIT,
            ),
            plugin_record(plugin_id="x", owner="s", repo="x", commit=OTHER_COMMIT),
        ):
            verdict = classify_plugin(observe_plugin(record))
            if verdict.enable.admitted:
                admitted_classes.add(verdict.plugin_class)
        self.assertEqual(admitted_classes, {CLASS_UX_ONLY})


class InventoryReadTests(unittest.TestCase):
    """The one herdr invocation, and everything that can go wrong reading it."""

    def test_the_only_argv_is_the_read_only_inventory_query(self):
        self.assertEqual(ops.INVENTORY_ARGV, ("plugin", "list", "--json"))

    def test_query_runs_exactly_that_argv_against_the_trusted_binary(self):
        resolution = mock.Mock(path="/opt/example-bin/herdr")
        completed = mock.Mock(returncode=0, stdout=inventory_document(), stderr="")
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".infrastructure.herdr_transport.resolve_herdr_binary",
            return_value=resolution,
        ), mock.patch.object(
            ops.subprocess, "run", return_value=completed
        ) as run:
            ops.query_inventory({})
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0], ["/opt/example-bin/herdr", "plugin", "list", "--json"]
        )

    def test_a_non_zero_herdr_exit_fails_closed(self):
        resolution = mock.Mock(path="/opt/example-bin/herdr")
        completed = mock.Mock(returncode=3, stdout="", stderr="boom")
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".infrastructure.herdr_transport.resolve_herdr_binary",
            return_value=resolution,
        ), mock.patch.object(ops.subprocess, "run", return_value=completed):
            with self.assertRaises(ops.InventoryReadError) as caught:
                ops.query_inventory({})
        self.assertEqual(caught.exception.reason, ops.READ_HERDR_ERROR)

    def test_parse_rejects_everything_that_is_not_the_plugin_list_envelope(self):
        for document in (
            "not json",
            "[]",
            "{}",
            json.dumps({"result": "plugins"}),
            json.dumps({"result": {}}),
            json.dumps({"result": {"plugins": "one"}}),
        ):
            with self.subTest(document=document[:24]):
                with self.assertRaises(ops.InventoryReadError):
                    ops.parse_inventory(document)

    def test_an_empty_plugin_list_is_a_clean_inventory(self):
        status = ops.classify_inventory(ops.parse_inventory(inventory_document()))
        self.assertEqual(status.verdicts, ())
        self.assertTrue(status.ok)

    def test_an_unreadable_record_is_collected_and_never_skipped(self):
        broken = plugin_record(plugin_id="broken")
        broken["enabled"] = "yes"
        status = ops.classify_inventory(
            ops.parse_inventory(inventory_document(plugin_record(), broken))
        )
        self.assertEqual(len(status.verdicts), 1)
        self.assertEqual(len(status.malformed), 1)
        self.assertEqual(status.malformed[0].index, 1)
        self.assertFalse(status.ok)


class StatusReportTests(unittest.TestCase):
    """Exit semantics, breach detection, scope truthfulness, path non-disclosure."""

    def _status(self, *records):
        return ops.classify_inventory(ops.parse_inventory(inventory_document(*records)))

    def test_a_denied_plugin_that_is_not_enabled_is_the_policy_working(self):
        status = self._status(
            plugin_record(
                plugin_id="herdr-reviewr",
                owner="persiyanov",
                repo="herdr-reviewr",
                commit=OTHER_COMMIT,
                enabled=False,
            )
        )
        self.assertFalse(status.verdicts[0].enable.admitted)
        self.assertEqual(status.breaches, ())
        self.assertTrue(status.ok)

    def test_a_denied_plugin_that_is_enabled_is_a_breach(self):
        status = self._status(
            plugin_record(
                plugin_id="herdr-reviewr",
                owner="persiyanov",
                repo="herdr-reviewr",
                commit=OTHER_COMMIT,
                enabled=True,
            )
        )
        self.assertEqual(len(status.breaches), 1)
        self.assertFalse(status.ok)
        self.assertIn("BREACH", ops.format_status_text(status))

    def test_the_report_states_the_enable_scope_is_user_global(self):
        status = self._status(plugin_record())
        payload = status.as_payload()
        self.assertEqual(payload["enable_scope"]["scope"], ENABLE_SCOPE)
        self.assertEqual(ENABLE_SCOPE, "user_global")
        text = ops.format_status_text(status)
        self.assertIn("user_global", text)
        self.assertIn("There is no workspace-local enable", text)

    def test_the_report_never_calls_an_enable_workspace_local(self):
        text = ops.format_status_text(self._status(plugin_record()))
        for wrong in ("workspace-local enable", "session-local", "lane-local"):
            with self.subTest(wrong=wrong):
                self.assertNotIn(f"is {wrong}", text)

    def test_no_operator_path_reaches_the_report(self):
        # The input carries the three absolute paths herdr's real payload does.
        status = self._status(plugin_record(), plugin_record(plugin_id="x"))
        rendered = json.dumps(status.as_payload()) + ops.format_status_text(status)
        self.assertIn(PLACEHOLDER_ROOT, inventory_document(plugin_record()))
        self.assertNotIn(PLACEHOLDER_ROOT, rendered)
        self.assertNotIn(PLACEHOLDER_MANIFEST, rendered)
        self.assertIsNone(_ABS_PATH_TOKEN.search(rendered))

    def test_a_malformed_record_does_not_carry_its_path_into_the_report(self):
        # The parse message quotes the offending value, and that value is
        # third-party data that can hold a path.
        broken = plugin_record()
        broken["plugin_id"] = f"{PLACEHOLDER_ROOT}/evil"
        status = self._status(broken)
        rendered = json.dumps(status.as_payload()) + ops.format_status_text(status)
        self.assertNotIn(PLACEHOLDER_ROOT, rendered)
        self.assertIsNone(_ABS_PATH_TOKEN.search(rendered))

    def test_plan_enable_on_an_absent_plugin_is_not_admissible(self):
        plan = ops.plan_enable(self._status(plugin_record()), "not-installed")
        self.assertFalse(plan.found)
        self.assertFalse(plan.ok)

    def test_plan_enable_admits_the_reviewed_ux_only_plugin(self):
        plan = ops.plan_enable(self._status(plugin_record()), "herdr-file-viewer")
        self.assertTrue(plan.ok)


class CliTests(unittest.TestCase):
    """The command surface: exit codes, and that it mutates nothing."""

    def setUp(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        register_herdr_plugin_policy_parser(sub)
        self.parser = parser
        self.inventory = Path(self.enterContext(_temp_dir())) / "inventory.json"

    def _parse(self, *argv):
        return self.parser.parse_args(["plugin-policy", *argv])

    def _write(self, *records):
        self.inventory.write_text(inventory_document(*records), encoding="utf-8")
        return str(self.inventory)

    def test_status_on_a_clean_inventory_exits_zero(self):
        args = self._parse("--from-json", self._write(plugin_record()))
        with mock.patch.object(ops.subprocess, "run") as run:
            self.assertEqual(cmd_herdr_plugin_policy(args), 0)
        run.assert_not_called()

    def test_status_on_a_breach_exits_non_zero(self):
        path = self._write(
            plugin_record(
                plugin_id="herdr-reviewr",
                owner="persiyanov",
                repo="herdr-reviewr",
                commit=OTHER_COMMIT,
            )
        )
        self.assertEqual(cmd_herdr_plugin_policy(self._parse("--from-json", path)), 1)

    def test_an_unreadable_inventory_document_exits_non_zero(self):
        args = self._parse("--from-json", str(self.inventory.parent / "absent.json"))
        self.assertEqual(cmd_herdr_plugin_policy(args), 1)

    def test_plan_enable_exit_code_follows_the_decision(self):
        path = self._write(
            plugin_record(),
            plugin_record(
                plugin_id="herdr-reviewr",
                owner="persiyanov",
                repo="herdr-reviewr",
                commit=OTHER_COMMIT,
                enabled=False,
            ),
        )
        for plugin_id, expected in (("herdr-file-viewer", 0), ("herdr-reviewr", 1)):
            with self.subTest(plugin_id=plugin_id):
                args = self._parse("--from-json", path, "--plan-enable", plugin_id)
                self.assertEqual(cmd_herdr_plugin_policy(args), expected)

    def test_plan_install_needs_no_inventory_and_runs_no_subprocess(self):
        args = self._parse(
            "--plan-install", "persiyanov/herdr-reviewr", "--ref", OTHER_COMMIT
        )
        with mock.patch.object(ops.subprocess, "run") as run:
            self.assertEqual(cmd_herdr_plugin_policy(args), 1)
        run.assert_not_called()

    def test_plan_install_without_a_ref_is_denied(self):
        args = self._parse("--plan-install", "smarzban/herdr-file-viewer")
        self.assertEqual(cmd_herdr_plugin_policy(args), 1)

    def test_no_mode_ever_issues_a_mutating_herdr_subcommand(self):
        # The adversarial guard: whatever the mode, the only subprocess this surface
        # may reach for is the read-only inventory query.
        path = self._write(plugin_record())
        modes = (
            ["--from-json", path],
            ["--from-json", path, "--plan-enable", "herdr-file-viewer"],
            ["--plan-install", "smarzban/herdr-file-viewer", "--ref", FILE_VIEWER_COMMIT],
        )
        completed = mock.Mock(returncode=0, stdout=inventory_document(), stderr="")
        for argv in modes:
            with self.subTest(argv=argv):
                with mock.patch.object(
                    ops.subprocess, "run", return_value=completed
                ) as run:
                    cmd_herdr_plugin_policy(self._parse(*argv))
                for call in run.call_args_list:
                    self.assertEqual(
                        tuple(call.args[0][1:]), ops.INVENTORY_ARGV
                    )

    def test_json_output_is_machine_readable(self):
        args = self._parse("--from-json", self._write(plugin_record()), "--json")
        with mock.patch("builtins.print") as printed:
            cmd_herdr_plugin_policy(args)
        payload = json.loads(printed.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["plugins"][0]["class"], CLASS_UX_ONLY)
        self.assertEqual(payload["plugins"][0]["enable_scope"], ENABLE_SCOPE)


def _temp_dir():
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
