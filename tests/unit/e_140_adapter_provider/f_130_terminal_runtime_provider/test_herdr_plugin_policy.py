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
import copy
import dataclasses
import json
import re
import sys
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_policy import (  # noqa: E402
    BUILD_NONE,
    BUILD_REMOTE_ARTIFACT,
    BUILD_SOURCE_ONLY,
    BUILD_UNREVIEWED,
    CLASS_AGENT_INPUT_WRITER,
    CLASS_PRESENTATION_CONTROL,
    CLASS_TEST_ORACLE,
    CLASS_UNKNOWN,
    CLASS_UX_ONLY,
    DENY_REASONS,
    ENABLE_ADMITTED_CLASSES,
    ENABLE_SCOPE,
    FORBIDDEN_PLUGIN_AUTHORITIES,
    REASON_AGENT_INPUT_WRITER,
    REASON_IDENTITY_MISMATCH,
    REASON_MANIFEST_DRIFT,
    REASON_MANIFEST_UNAVAILABLE,
    REASON_MALFORMED_RECORD,
    REASON_NO_LANE_AUTHORITY,
    REASON_UNPINNED_REMOTE_BUILD,
    REASON_UNPINNED_SOURCE,
    REASON_UNREVIEWED_BUILD,
    REASON_AMBIGUOUS_TARGET,
    REASON_INVALID_TARGET_ID,
    REASON_INVENTORY_INCOMPLETE,
    REASON_TARGET_NOT_INSTALLED,
    REASON_UNREVIEWED_PIN,
    REVIEWED_PLUGINS,
    SOURCE_KIND_ABSENT,
    SOURCE_KIND_GITHUB,
    SOURCE_KIND_LINK,
    SOURCE_KIND_UNRECOGNIZED,
    HerdrPluginPolicyError,
    PluginObservation,
    PluginSourceRef,
    PluginVerdict,
    PolicyDecision,
    ReviewedPlugin,
    build_review_registry,
    classify_plugin,
    observe_plugin,
    plan_install,
    resolve_review,
    source_ref_from_parts,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_identity import (  # noqa: E402
    manifest_capability_digest,
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
UNIT_BOARD_COMMIT = "19e4ac6ff63197aa5b255a37ecb3472da8b4886e"
UNIT_BOARD_SUBDIR = ("herdr-plugins", "mozyo-unit-board")
UNIT_BOARD_SPEC = "hollySizzle/mozyo_bridge/herdr-plugins/mozyo-unit-board"
UNIT_BOARD_CANONICAL_SPEC = "hollysizzle/mozyo_bridge/herdr-plugins/mozyo-unit-board"
UNIT_BOARD_MANIFEST_DIGEST = (
    "1bd86a85d625afae4863964c653145ad77eb57d01500e9166b1e2c73051d6d56"
)
FILE_VIEWER_MANIFEST_DIGEST = (
    "6e6bc1bb27f621b1d223f4b23cb9bd70dc036181e0d357b4f0283162d31b1c1f"
)
TEST_MANIFEST_DIGEST = "0" * 64

#: Abstract placeholder paths standing in for the three absolute operator-home
#: paths herdr's real payload carries. Never an operator path.
PLACEHOLDER_ROOT = "/opt/example-config/herdr/plugins/github/example-plugin-0000"
PLACEHOLDER_MANIFEST = f"{PLACEHOLDER_ROOT}/herdr-plugin.toml"

#: A POSIX absolute-path token: a slash immediately followed by a path segment.
#: ``HOME / XDG_CONFIG_HOME`` in prose does not match (the slash stands alone).
_ABS_PATH_TOKEN = re.compile(r"/[A-Za-z0-9._-]+/")

#: The value the whole-surface leak oracle injects. Path-shaped so it trips the
#: absolute-path scan too, and distinctive enough to find in any rendering.
LEAK_MARKER = "/opt/private-probe/leaked-secret/value.txt"


def _leaked(rendered: str, injected: str) -> bool:
    """Whether ``rendered`` leaked ``injected``, by three independent tests.

    Conjoined rather than substituted (the suite's own rule for fixing an oracle):
    the exact injected value, this file's independently written path regex, and
    production's own detector. The third was added after review j#92194 showed the
    test regex and production's shared a blind spot; keeping the first two means a
    bug in production's detector cannot make this oracle blind with it.
    """
    return (
        injected in rendered
        or _ABS_PATH_TOKEN.search(rendered) is not None
        or ops.contains_absolute_path(rendered)
    )


def _patched_registry(entries):
    """Swap the reviewed registry for a test.

    `mock.patch.dict` edits the mapping in place, which a read-only authority
    refuses (review j#92330). Replacing the module attribute is both what works
    and what a test should do: it substitutes a registry rather than reaching into
    the real one.
    """
    import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_policy as policy

    return mock.patch.object(policy, "REVIEWED_PLUGINS", MappingProxyType(dict(entries)))


def _string_leaf_paths(node: object, prefix: "tuple" = ()) -> "list[tuple]":
    """Every path to a string leaf inside a nested record (for the leak oracle)."""
    if isinstance(node, str):
        return [prefix]
    if isinstance(node, dict):
        found: "list[tuple]" = []
        for key, value in node.items():
            found.extend(_string_leaf_paths(value, prefix + (key,)))
        return found
    if isinstance(node, list):
        found = []
        for index, value in enumerate(node):
            found.extend(_string_leaf_paths(value, prefix + (index,)))
        return found
    return []


def _with_leaf(node: object, path: "tuple", value: str) -> object:
    """A deep copy of ``node`` with the leaf at ``path`` replaced by ``value``."""
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(node, dict):
        return {
            key: _with_leaf(item, rest, value) if key == head else copy.deepcopy(item)
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [
            _with_leaf(item, rest, value) if index == head else copy.deepcopy(item)
            for index, item in enumerate(node)
        ]
    return copy.deepcopy(node)


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
    subdir: object = None,
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
    }
    if plugin_id == "mozyo.unit-board":
        record.update(
            {
                "version": "0.2.0",
                "min_herdr_version": "0.8.0",
                "platforms": ["linux", "macos"],
                "startup": [
                    {
                        "command": [
                            "mozyo-bridge",
                            "herdr",
                            "unit-board",
                            "sync",
                            "--quiet",
                        ]
                    }
                ],
                "actions": [
                    {
                        "command": [
                            "mozyo-bridge",
                            "herdr",
                            "unit-board",
                            "sync",
                        ],
                        "contexts": ["workspace"],
                        "id": "sync",
                        "title": "Refresh mozyo Unit labels",
                    }
                ],
                "events": [
                    {
                        "command": [
                            "mozyo-bridge",
                            "herdr",
                            "unit-board",
                            "sync",
                            "--quiet",
                        ],
                        "on": event,
                    }
                    for event in ("pane.agent_detected", "pane.created", "pane.exited")
                ],
                "panes": [
                    {
                        "command": [
                            "mozyo-bridge",
                            "herdr",
                            "unit-board",
                            "interact",
                        ],
                        "height": "75%",
                        "id": "board",
                        "placement": "popup",
                        "title": "mozyo Unit board",
                        "width": "92%",
                    }
                ],
            }
        )
    elif plugin_id == "herdr-file-viewer":
        windows_prefix = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
        ]
        windows_body = (
            "$u=New-Object System.Text.UTF8Encoding($false); "
            "[Console]::OutputEncoding=$u; $OutputEncoding=$u; "
            "$b=$env:HERDR_BIN_PATH; if(-not $b){$b='herdr'}; "
            "$p=((& $b plugin list --json|ConvertFrom-Json).result.plugins|"
            "?{$_.plugin_id -eq 'herdr-file-viewer'}).plugin_root; "
            "if($p -and $p.StartsWith('\\\\?\\')){$p=$p.Substring(4)}; "
        )
        split_description = (
            "Open the git-aware file viewer in a split pane beside the current work."
        )
        tab_description = (
            "Open the git-aware file viewer in its own tab (switch to it if already open)."
        )
        record.update(
            {
                "min_herdr_version": "0.7.0",
                "platforms": ["linux", "macos", "windows"],
                "actions": [
                    {
                        "command": ["bash", "scripts/open-file-viewer.sh"],
                        "description": split_description,
                        "id": "open-file-viewer",
                        "platforms": ["linux", "macos"],
                        "title": "Open file viewer",
                    },
                    {
                        "command": ["bash", "scripts/open-file-viewer-tab.sh"],
                        "description": tab_description,
                        "id": "open-file-viewer-tab",
                        "platforms": ["linux", "macos"],
                        "title": "Open file viewer (tab)",
                    },
                    {
                        "command": windows_prefix
                        + [
                            windows_body
                            + "& (Join-Path (Join-Path $p 'scripts') "
                            "'open-file-viewer-tab.ps1')"
                        ],
                        "description": tab_description,
                        "id": "open-file-viewer-tab-windows",
                        "platforms": ["windows"],
                        "title": "Open file viewer (tab)",
                    },
                    {
                        "command": windows_prefix
                        + [
                            windows_body
                            + "& (Join-Path (Join-Path $p 'scripts') "
                            "'open-file-viewer.ps1')"
                        ],
                        "description": split_description,
                        "id": "open-file-viewer-windows",
                        "platforms": ["windows"],
                        "title": "Open file viewer",
                    },
                ],
                "panes": [
                    {
                        "command": ["./target/release/herdr-file-viewer"],
                        "id": "file-viewer",
                        "placement": "split",
                        "title": "Files",
                    }
                ],
            }
        )
    else:
        record.update(
            {
                "panes": [
                    {"id": "viewer", "command": ["./target/release/viewer"]}
                ],
                "actions": [
                    {"id": "open", "command": ["bash", "scripts/open.sh"]}
                ],
            }
        )
    if build:
        record["build"] = (
            [
                {
                    "command": ["/bin/sh", "scripts/fetch-or-build.sh"],
                    "platforms": ["linux", "macos"],
                },
                {
                    "command": [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        "scripts/fetch-or-build.ps1",
                    ],
                    "platforms": ["windows"],
                },
            ]
            if plugin_id == "herdr-file-viewer"
            else [{"command": ["/bin/sh", "scripts/fetch-or-build.sh"]}]
        )
    source_record = {
        "kind": kind,
        "owner": owner,
        "repo": repo,
        "resolved_commit": commit,
        "managed_path": PLACEHOLDER_ROOT,
    }
    if subdir is not None:
        source_record["subdir"] = subdir
    record["source"] = source_record if source is ... else source
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
        for owner, repo in (
            ("a/b", "r"),
            ("o", "r/s"),
            ("o ", "r"),
            ("", "r"),
            (".", "r"),
            ("o", ".."),
        ):
            with self.subTest(owner=owner, repo=repo):
                with self.assertRaises(HerdrPluginPolicyError):
                    PluginSourceRef.repository(SOURCE_KIND_GITHUB, owner, repo)

    def test_non_github_kind_is_not_a_pinnable_identity(self):
        with self.assertRaises(HerdrPluginPolicyError):
            PluginSourceRef.repository("link", "o", "r")

    def test_github_owner_and_repo_are_one_case_insensitive_identity(self):
        mixed = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            "PERSIYANOV",
            "HERDR-REVIEWR",
            OTHER_COMMIT,
            subdir=("Plugins", "Reviewr"),
        )
        lower = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            "persiyanov",
            "herdr-reviewr",
            OTHER_COMMIT,
            subdir=("Plugins", "Reviewr"),
        )
        self.assertEqual(mixed, lower)
        self.assertEqual(mixed.repo_key, lower.repo_key)
        self.assertEqual(mixed.owner, "persiyanov")
        self.assertEqual(mixed.repo, "herdr-reviewr")
        self.assertEqual(mixed.subdir, ("Plugins", "Reviewr"))

    def test_repo_key_drops_the_commit_and_subdir(self):
        ref = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            "o",
            "r",
            FILE_VIEWER_COMMIT,
            subdir=("plugins", "viewer"),
        )
        self.assertEqual(
            ref.repo_key,
            PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r"),
        )

    def test_subdir_is_part_of_the_exact_identity_and_description(self):
        ref = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            "o",
            "r",
            FILE_VIEWER_COMMIT,
            subdir=("plugins", "viewer"),
        )
        other = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            "o",
            "r",
            FILE_VIEWER_COMMIT,
            subdir=("plugins", "other"),
        )
        self.assertNotEqual(ref, other)
        self.assertEqual(ref.install_spec, "o/r/plugins/viewer")
        self.assertEqual(
            ref.describe(),
            f"github:o/r/plugins/viewer@{FILE_VIEWER_COMMIT}",
        )

    def test_invalid_subdir_falls_back_only_to_the_repository_identity(self):
        invalid = (
            "",
            "/plugins/viewer",
            "plugins/",
            "plugins//viewer",
            ".",
            "..",
            "plugins/../viewer",
            "plugins\\viewer",
            "plugins/with space",
            "plugins/with:colon",
            "plugins/with\x00nul",
            "plugins/with\ncontrol",
            "x" * 65,
            "/".join("x" for _ in range(17)),
            7,
            ["plugins", "viewer"],
        )
        for subdir in invalid:
            with self.subTest(subdir=repr(subdir)[:40]):
                ref = source_ref_from_parts(
                    SOURCE_KIND_GITHUB,
                    "o",
                    "r",
                    FILE_VIEWER_COMMIT,
                    subdir,
                )
                self.assertEqual(
                    ref,
                    PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r"),
                )
                self.assertFalse(ref.is_pinned)

    def test_invalid_repository_identity_has_no_reference(self):
        for owner, repo in ((".", "r"), ("o", ".."), ("bad/owner", "r")):
            with self.subTest(owner=owner, repo=repo):
                self.assertIsNone(
                    source_ref_from_parts(
                        SOURCE_KIND_GITHUB,
                        owner,
                        repo,
                        FILE_VIEWER_COMMIT,
                        "plugins/viewer",
                    )
                )

    def test_bad_commit_keeps_a_valid_subdir_on_an_unpinned_reference(self):
        ref = source_ref_from_parts(
            SOURCE_KIND_GITHUB,
            "persiyanov",
            "herdr-reviewr",
            "not-a-commit",
            "plugins/reviewr",
        )
        self.assertIsNotNone(ref)
        self.assertFalse(ref.is_pinned)
        self.assertEqual(ref.subdir, ("plugins", "reviewr"))
        self.assertEqual(
            ref.repo_key,
            PluginSourceRef.repository(
                SOURCE_KIND_GITHUB, "persiyanov", "herdr-reviewr"
            ),
        )


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
            manifest_digest=(
                TEST_MANIFEST_DIGEST if plugin_class not in (CLASS_TEST_ORACLE, CLASS_AGENT_INPUT_WRITER) else None
            ),
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

    def test_repository_scoped_review_entry_cannot_name_one_subdir(self):
        ref = PluginSourceRef(
            kind=SOURCE_KIND_GITHUB,
            owner="o",
            repo="r",
            subdir=("plugins", "one"),
        )
        with self.assertRaises(HerdrPluginPolicyError):
            self._entry(ref, CLASS_AGENT_INPUT_WRITER)

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
                manifest_digest=TEST_MANIFEST_DIGEST,
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

    def test_case_variant_repository_deny_and_allow_still_conflict(self):
        with self.assertRaises(HerdrPluginPolicyError):
            build_review_registry(
                (
                    self._entry(
                        PluginSourceRef.repository(
                            SOURCE_KIND_GITHUB, "PERSIYANOV", "HERDR-REVIEWR"
                        ),
                        CLASS_AGENT_INPUT_WRITER,
                    ),
                    self._entry(
                        PluginSourceRef.pinned(
                            SOURCE_KIND_GITHUB,
                            "persiyanov",
                            "herdr-reviewr",
                            OTHER_COMMIT,
                        ),
                        CLASS_UX_ONLY,
                    ),
                )
            )

    def test_shipped_registry_holds_the_reviewed_projects_and_exact_unit_board(self):
        described = {ref.describe() for ref in REVIEWED_PLUGINS}
        self.assertEqual(
            described,
            {
                f"github:{UNIT_BOARD_CANONICAL_SPEC}@{UNIT_BOARD_COMMIT}",
                f"github:smarzban/herdr-file-viewer@{FILE_VIEWER_COMMIT}",
                "github:yuk1ty/herdr-spreader",
                "github:persiyanov/herdr-reviewr",
            },
        )


class ClassificationTests(unittest.TestCase):
    """The close conditions, as decisions rather than prose."""

    def test_file_viewer_at_the_reviewed_pin_is_ux_only_and_enable_admitted(self):
        observation = observe_plugin(plugin_record())
        self.assertEqual(observation.manifest_digest, FILE_VIEWER_MANIFEST_DIGEST)
        verdict = classify_plugin(observation)
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

    def test_unit_board_exact_subdir_and_commit_is_admitted_on_both_axes(self):
        observation = observe_plugin(
            plugin_record(
                plugin_id="mozyo.unit-board",
                owner="hollySizzle",
                repo="mozyo_bridge",
                commit=UNIT_BOARD_COMMIT,
                subdir="/".join(UNIT_BOARD_SUBDIR),
                build=False,
            )
        )
        self.assertEqual(observation.manifest_digest, UNIT_BOARD_MANIFEST_DIGEST)
        verdict = classify_plugin(observation)
        self.assertEqual(verdict.plugin_class, CLASS_PRESENTATION_CONTROL)
        self.assertEqual(verdict.build_provenance, BUILD_NONE)
        self.assertTrue(verdict.enable.admitted)
        self.assertTrue(verdict.install.admitted)

    def test_unit_board_allow_does_not_match_root_sibling_child_or_other_commit(self):
        variants = (
            (None, UNIT_BOARD_COMMIT),
            ("herdr-plugins/another-plugin", UNIT_BOARD_COMMIT),
            ("herdr-plugins/mozyo-unit-board/child", UNIT_BOARD_COMMIT),
            ("/".join(UNIT_BOARD_SUBDIR), OTHER_COMMIT),
        )
        for subdir, commit in variants:
            with self.subTest(subdir=subdir, commit=commit):
                verdict = classify_plugin(
                    observe_plugin(
                        plugin_record(
                            plugin_id="mozyo.unit-board",
                            owner="hollySizzle",
                            repo="mozyo_bridge",
                            commit=commit,
                            subdir=subdir,
                            build=False,
                        )
                    )
                )
                self.assertEqual(verdict.plugin_class, CLASS_UNKNOWN)
                self.assertFalse(verdict.enable.admitted)
                self.assertFalse(verdict.install.admitted)

    def test_unit_board_manifest_id_and_build_surface_are_rechecked(self):
        common = dict(
            owner="hollySizzle",
            repo="mozyo_bridge",
            commit=UNIT_BOARD_COMMIT,
            subdir="/".join(UNIT_BOARD_SUBDIR),
        )
        wrong_id = classify_plugin(
            observe_plugin(plugin_record(plugin_id="another-plugin", build=False, **common))
        )
        self.assertEqual(wrong_id.enable.reason, REASON_IDENTITY_MISMATCH)
        unexpected_build = classify_plugin(
            observe_plugin(plugin_record(plugin_id="mozyo.unit-board", build=True, **common))
        )
        self.assertEqual(unexpected_build.enable.reason, REASON_MANIFEST_DRIFT)
        self.assertEqual(unexpected_build.install.reason, REASON_MANIFEST_DRIFT)

    def test_unit_board_source_pin_does_not_admit_a_changed_manifest_command(self):
        record = plugin_record(
            plugin_id="mozyo.unit-board",
            owner="hollySizzle",
            repo="mozyo_bridge",
            commit=UNIT_BOARD_COMMIT,
            subdir="/".join(UNIT_BOARD_SUBDIR),
            build=False,
        )
        record["events"][0]["command"] = ["another-program", "write-input"]
        verdict = classify_plugin(observe_plugin(record))
        self.assertEqual(verdict.enable.reason, REASON_MANIFEST_DRIFT)
        self.assertEqual(verdict.install.reason, REASON_MANIFEST_DRIFT)

    def test_manifest_warning_refuses_enable_without_echoing_the_warning(self):
        record = plugin_record(
            plugin_id="mozyo.unit-board",
            owner="hollySizzle",
            repo="mozyo_bridge",
            commit=UNIT_BOARD_COMMIT,
            subdir="/".join(UNIT_BOARD_SUBDIR),
            build=False,
        )
        record["warnings"] = [LEAK_MARKER]
        verdict = classify_plugin(observe_plugin(record))
        self.assertEqual(verdict.enable.reason, REASON_MANIFEST_UNAVAILABLE)
        rendered = json.dumps(ops.PolicyStatus((verdict,), ()).as_payload())
        self.assertNotIn(LEAK_MARKER, rendered)

    def test_unknown_or_noncanonical_manifest_is_unreadable_and_blocks_enable_plan(self):
        variants = {}
        unknown = plugin_record(
            plugin_id="mozyo.unit-board",
            owner="hollySizzle",
            repo="mozyo_bridge",
            commit=UNIT_BOARD_COMMIT,
            subdir="/".join(UNIT_BOARD_SUBDIR),
            build=False,
        )
        unknown["future_capability"] = [{"command": [LEAK_MARKER]}]
        variants["unknown-top-level"] = unknown

        noncanonical = copy.deepcopy(unknown)
        noncanonical.pop("future_capability")
        noncanonical["events"] = {"command": [LEAK_MARKER]}
        variants["non-list-capability"] = noncanonical

        for label, record in variants.items():
            with self.subTest(label=label):
                status = ops.classify_inventory([record])
                self.assertEqual(status.verdicts, ())
                self.assertEqual(len(status.malformed), 1)
                self.assertEqual(
                    status.as_payload()["malformed"][0]["reason"],
                    REASON_MALFORMED_RECORD,
                )
                status_text = ops.format_status_text(status)
                self.assertIn(f"[{REASON_MALFORMED_RECORD}]", status_text)

                plan = ops.plan_enable(status, "mozyo.unit-board")
                self.assertEqual(plan.decision.reason, REASON_INVENTORY_INCOMPLETE)
                self.assertEqual(
                    plan.as_payload()["decision"]["reason"],
                    REASON_INVENTORY_INCOMPLETE,
                )
                self.assertIn(
                    f"[{REASON_INVENTORY_INCOMPLETE}]",
                    ops.format_enable_plan_text(plan),
                )
                rendered = (
                    json.dumps(status.as_payload())
                    + status_text
                    + json.dumps(plan.as_payload())
                    + ops.format_enable_plan_text(plan)
                )
                self.assertNotIn(LEAK_MARKER, rendered)

    def test_repository_deny_is_case_insensitive(self):
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(
                    plugin_id="herdr-reviewr",
                    owner="PERSIYANOV",
                    repo="HERDR-REVIEWR",
                    commit=OTHER_COMMIT,
                )
            )
        )
        self.assertEqual(verdict.plugin_class, CLASS_AGENT_INPUT_WRITER)
        self.assertEqual(verdict.enable.reason, REASON_AGENT_INPUT_WRITER)

    def test_requested_ref_does_not_override_the_resolved_commit_identity(self):
        record = plugin_record(
            plugin_id="mozyo.unit-board",
            owner="hollySizzle",
            repo="mozyo_bridge",
            commit=UNIT_BOARD_COMMIT,
            subdir="/".join(UNIT_BOARD_SUBDIR),
            build=False,
        )
        record["source"]["requested_ref"] = "main"
        verdict = classify_plugin(observe_plugin(record))
        self.assertTrue(verdict.enable.admitted)

    def test_invalid_subdir_never_falls_through_to_the_unit_board_allow(self):
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(
                    plugin_id="mozyo.unit-board",
                    owner="hollySizzle",
                    repo="mozyo_bridge",
                    commit=UNIT_BOARD_COMMIT,
                    subdir="herdr-plugins/../mozyo-unit-board",
                    build=False,
                )
            )
        )
        self.assertEqual(verdict.plugin_class, CLASS_UNKNOWN)
        self.assertEqual(verdict.enable.reason, REASON_UNPINNED_SOURCE)
        self.assertIn("exact plugin path and commit identity", verdict.enable.detail)
        self.assertNotIn("names no exact commit", verdict.enable.detail)

    def test_repository_deny_applies_to_valid_and_malformed_subdirs(self):
        for subdir in ("plugins/reviewr", "plugins/../reviewr", "bad space"):
            with self.subTest(subdir=subdir):
                verdict = classify_plugin(
                    observe_plugin(
                        plugin_record(
                            plugin_id="herdr-reviewr",
                            owner="persiyanov",
                            repo="herdr-reviewr",
                            commit=OTHER_COMMIT,
                            subdir=subdir,
                        )
                    )
                )
                self.assertEqual(verdict.plugin_class, CLASS_AGENT_INPUT_WRITER)
                self.assertEqual(verdict.enable.reason, REASON_AGENT_INPUT_WRITER)

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

    def test_an_abbreviated_pin_keeps_the_repository_deny_classification(self):
        # Review j#92053 finding 3: a malformed commit used to discard owner/repo
        # too, so reviewr classified as `unknown` / `unpinned_source`. The deny
        # survived but its class and reason were both untrue — defeating the very
        # property the repository-scoped deny exists to provide.
        for commit in ("6c304925", "6C304925BDD2727EC88C38F8C8806F97B7CA0EA0", "", 7):
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

    def test_an_abbreviated_pin_keeps_the_spreader_classification(self):
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(
                    plugin_id="herdr-spreader",
                    owner="yuk1ty",
                    repo="herdr-spreader",
                    commit="5f76bc9e",
                    build=False,
                )
            )
        )
        self.assertEqual(verdict.plugin_class, CLASS_TEST_ORACLE)
        self.assertEqual(verdict.enable.reason, REASON_NO_LANE_AUTHORITY)

    def test_an_abbreviated_pin_never_upgrades_an_allow(self):
        # The other direction of the same fix: keeping the repository identity must
        # not let a pinned-allow project in at an unpinned identity.
        verdict = classify_plugin(observe_plugin(plugin_record(commit="96fcc0a2")))
        self.assertEqual(verdict.plugin_class, CLASS_UNKNOWN)
        self.assertFalse(verdict.enable.admitted)
        self.assertEqual(verdict.enable.reason, REASON_UNPINNED_SOURCE)

    def test_a_malformed_owner_or_repo_still_yields_no_identity(self):
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(source={"kind": "github", "owner": "a/b", "repo": "r"})
            )
        )
        self.assertIsNone(verdict.observation.ref)
        self.assertEqual(verdict.enable.reason, REASON_UNPINNED_SOURCE)

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
        clean = plugin_record(
            plugin_id="p", owner="o", repo="r", commit=OTHER_COMMIT, build=False
        )
        entry = ReviewedPlugin(
            ref=ref,
            plugin_id="p",
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_NONE,
            review_anchor="#0 j#0",
            rationale="fixture",
            manifest_digest=manifest_capability_digest(clean),
        )
        drifted = dict(clean, build=[{"command": ["/bin/sh", "x.sh"]}])
        with _patched_registry({ref: entry}):
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

    def test_every_field_is_validated_at_construction(self):
        # Review j#92092 finding 1: `__post_init__` hand-listed three checks while
        # the docstring claimed all eight fields were closed. Direct construction
        # with a path in `declares_build` succeeded and reached the report.
        valid = dict(
            plugin_id="p",
            enabled=True,
            source_kind=SOURCE_KIND_GITHUB,
            ref=None,
            declares_build=False,
            declares_panes=False,
            declares_actions=False,
            manifest_digest=TEST_MANIFEST_DIGEST,
            manifest_warnings=False,
        )
        PluginObservation(**valid)  # the fixture itself must be constructible
        hostile = {
            "plugin_id": (7, "", "a/b", "x" * 5000, LEAK_MARKER),
            "enabled": (1, 0, "yes", None),
            "source_kind": (["x"], 7, "not-a-kind", None),
            "ref": ("github:o/r", 7, object()),
            "declares_build": (LEAK_MARKER, 1, None),
            "declares_panes": (LEAK_MARKER, 1, None),
            "declares_actions": (LEAK_MARKER, 1, None),
            "manifest_digest": (LEAK_MARKER, "0" * 63, "G" * 64, None),
            "manifest_warnings": (LEAK_MARKER, 1, None),
        }
        # Every field of the dataclass must appear here, or the table has the same
        # gap the implementation had.
        self.assertEqual(
            {field.name for field in dataclasses.fields(PluginObservation)},
            set(hostile),
        )
        for field_name, values in hostile.items():
            for value in values:
                with self.subTest(field=field_name, value=repr(value)[:24]):
                    with self.assertRaises(HerdrPluginPolicyError):
                        PluginObservation(**{**valid, field_name: value})

    def test_a_field_without_a_validator_cannot_be_constructed(self):
        # The guard that makes the check table self-policing: adding a field and
        # forgetting its validator must fail loudly rather than pass silently,
        # because forgetting is exactly what happened.
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_identity as identity

        trimmed = dict(identity._OBSERVATION_FIELD_CHECKS)
        trimmed.pop("declares_build")
        with mock.patch.object(
            identity, "_OBSERVATION_FIELD_CHECKS", trimmed
        ):
            with self.assertRaises(HerdrPluginPolicyError):
                PluginObservation(
                    plugin_id="p",
                    enabled=True,
                    source_kind=SOURCE_KIND_GITHUB,
                    ref=None,
                    declares_build=False,
                    declares_panes=False,
                    declares_actions=False,
                    manifest_digest=TEST_MANIFEST_DIGEST,
                    manifest_warnings=False,
                )

    def test_an_identifier_is_bounded(self):
        # Self-reported alongside j#92092 F3: the alphabet alone bounds shape but
        # not length, and a 5,000-character id was accepted and rendered.
        with self.assertRaises(HerdrPluginPolicyError):
            observe_plugin(plugin_record(plugin_id="x" * 5000))
        observe_plugin(plugin_record(plugin_id="x" * 64))

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

    def test_exact_unit_board_candidate_is_admitted(self):
        plan = ops.plan_candidate_install(UNIT_BOARD_SPEC, UNIT_BOARD_COMMIT)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.spec, UNIT_BOARD_CANONICAL_SPEC)
        self.assertEqual(plan.ref.subdir, UNIT_BOARD_SUBDIR)
        self.assertEqual(plan.decision, plan_install(plan.ref))

    def test_unit_board_candidate_requires_exact_subdir_and_commit(self):
        variants = (
            ("hollySizzle/mozyo_bridge", UNIT_BOARD_COMMIT),
            ("hollySizzle/mozyo_bridge/herdr-plugins/other", UNIT_BOARD_COMMIT),
            (UNIT_BOARD_SPEC + "/child", UNIT_BOARD_COMMIT),
            (UNIT_BOARD_SPEC, OTHER_COMMIT),
            (UNIT_BOARD_SPEC, "not-a-commit"),
        )
        for spec, commit in variants:
            with self.subTest(spec=spec, commit=commit):
                self.assertFalse(ops.plan_candidate_install(spec, commit).ok)

    def test_invalid_subdir_is_not_echoed_and_never_reaches_an_allow(self):
        hostile_subdirs = (
            "../escape",
            "bad space",
            "bad\nZZFORGEDLINEZZ",
            "~private",
            "C:\\private",
        )
        repository = PluginSourceRef.repository(SOURCE_KIND_GITHUB, "o", "r")
        for subdir in hostile_subdirs:
            with self.subTest(subdir=repr(subdir)):
                plan = ops.plan_candidate_install(f"o/r/{subdir}", OTHER_COMMIT)
                rendered = json.dumps(plan.as_payload()) + ops.format_install_plan_text(
                    plan
                )
                self.assertEqual(plan.ref, repository)
                self.assertEqual(plan.spec, "o/r")
                self.assertFalse(plan.ok)
                self.assertNotIn(subdir, rendered)
                self.assertNotIn("ZZFORGEDLINEZZ", rendered)

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
            manifest_digest=TEST_MANIFEST_DIGEST,
        )
        with _patched_registry({ref: entry}):
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
                        manifest_digest=TEST_MANIFEST_DIGEST,
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
                manifest_digest=TEST_MANIFEST_DIGEST,
            ),
        }
        with _patched_registry(conflicting):
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

    def test_only_explicit_presentation_classes_can_be_admitted_for_enable(self):
        admitted_classes = set()
        for record in (
            plugin_record(),
            plugin_record(
                plugin_id="mozyo.unit-board",
                owner="hollySizzle",
                repo="mozyo_bridge",
                commit=UNIT_BOARD_COMMIT,
                subdir="/".join(UNIT_BOARD_SUBDIR),
                build=False,
            ),
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
        self.assertEqual(admitted_classes, set(ENABLE_ADMITTED_CLASSES))


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

    def test_no_string_field_anywhere_in_the_payload_can_reach_the_report(self):
        # Review j#92053 finding 1: the previous version of this class enumerated
        # the fields it thought could carry a path, and the enumeration was wrong —
        # `version` and `source_kind` were free text and went straight through, so
        # a clean `ok=true` report carried a private path.
        #
        # The enumeration is therefore replaced by an oracle over the whole input
        # surface: inject the marker into EVERY string leaf of a real-shaped
        # payload, one leaf at a time, and require it never to surface. A field
        # added later is covered without anyone remembering to list it.
        leaks = []
        for path in _string_leaf_paths(plugin_record()):
            record = _with_leaf(plugin_record(), path, LEAK_MARKER)
            status = self._status(record)
            rendered = json.dumps(status.as_payload()) + ops.format_status_text(status)
            if _leaked(rendered, LEAK_MARKER):
                leaks.append(".".join(str(part) for part in path))
        self.assertEqual(leaks, [], f"payload leaf(s) reached the report: {leaks}")

    def test_no_plan_operand_can_reach_a_report(self):
        # Review j#92092 finding 2: the inventory oracle above covers ONE of this
        # surface's untrusted inputs. A plan operand never passes through the
        # inventory, so the oracle structurally could not see it — and the raw
        # operand went into both the text and the JSON. This is the same oracle
        # applied to the second input path.
        empty = ops.PolicyStatus(verdicts=(), malformed=())
        populated = self._status(plugin_record())
        # An operand that IS a valid bounded identifier is deliberately echoed — it
        # names what the operator asked about, it is theirs rather than a third
        # party's, and its alphabet admits no path separator or control character.
        # So the hostile set here is "operands that are not identifiers", which is
        # narrower than the inventory oracle's "every string leaf": the two inputs
        # have different owners and therefore different rules.
        hostile = (
            LEAK_MARKER,
            "a\nBREACH: forged\nb",
            "id with spaces",
            "x" * 5000,
            "../../etc/passwd",
            "tab\tseparated",
        )
        leaks = []
        for operand in hostile:
            for status in (empty, populated):
                plan = ops.plan_enable(status, operand)
                rendered = json.dumps(plan.as_payload()) + ops.format_enable_plan_text(
                    plan
                )
                if _leaked(rendered, operand):
                    leaks.append(("enable", operand[:24]))
            install = ops.plan_candidate_install(operand, None)
            rendered = json.dumps(install.as_payload()) + ops.format_install_plan_text(
                install
            )
            if _leaked(rendered, operand):
                leaks.append(("install", operand[:24]))
            spec_install = ops.plan_candidate_install(f"{operand}/{operand}", None)
            rendered = json.dumps(
                spec_install.as_payload()
            ) + ops.format_install_plan_text(spec_install)
            if _leaked(rendered, operand):
                leaks.append(("install-spec", operand[:24]))
        self.assertEqual(leaks, [], f"operand(s) reached a report: {leaks}")

    def test_a_newline_operand_cannot_forge_a_report_line(self):
        # Worse than disclosure: this text is written to be pasted into a durable
        # record. The probe deliberately does NOT look for `BREACH:` — that line is
        # one this report legitimately writes (see the test below), so its absence
        # would prove nothing. It looks for a marker only the operand can supply.
        forged = "ZZFORGEDLINEZZ"
        plan = ops.plan_enable(
            ops.PolicyStatus(verdicts=(), malformed=()),
            f"a\n{forged}\nb",
        )
        text = ops.format_enable_plan_text(plan)
        self.assertFalse(
            any(line.strip().startswith(forged) for line in text.splitlines())
        )
        self.assertNotIn(forged, text)
        self.assertEqual(plan.decision.reason, REASON_INVALID_TARGET_ID)
        self.assertFalse(plan.ok)

    def test_an_enable_plan_legitimately_prints_a_breach_line(self):
        # The reason the probe above cannot use `BREACH:`: an enable plan whose
        # plugin is enabled and inadmissible reports exactly that. An oracle that
        # treats this as forgery is measuring the wrong question — the mistake this
        # suite made three times (reviews j#92141, j#92194, j#92241).
        status = ops.classify_inventory(
            ops.parse_inventory(
                inventory_document(
                    plugin_record(
                        plugin_id="herdr-reviewr",
                        owner="persiyanov",
                        repo="herdr-reviewr",
                        commit=OTHER_COMMIT,
                    )
                )
            )
        )
        text = ops.format_enable_plan_text(ops.plan_enable(status, "herdr-reviewr"))
        self.assertTrue(
            any(line.strip().startswith("BREACH:") for line in text.splitlines())
        )

    def test_a_valid_operand_is_still_echoed(self):
        # The closed token must not swallow the ordinary case, or the oracle above
        # would pass on a report that says nothing.
        plan = ops.plan_enable(self._status(plugin_record()), "herdr-file-viewer")
        self.assertEqual(plan.as_payload()["plugin_id"], "herdr-file-viewer")
        self.assertIn("herdr-file-viewer", ops.format_enable_plan_text(plan))
        install = ops.plan_candidate_install(
            "smarzban/herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        self.assertEqual(install.as_payload()["spec"], "smarzban/herdr-file-viewer")

    def test_the_oracle_would_catch_a_leak(self):
        # The oracle above proves nothing unless it can fail. Feed the same marker
        # through a formatter that does echo it, and require detection — otherwise
        # a future change that made `_status` return nothing would read as "green".
        rendered = f"version: {LEAK_MARKER}"
        self.assertTrue(
            LEAK_MARKER in rendered or _ABS_PATH_TOKEN.search(rendered) is not None
        )
        self.assertTrue(_string_leaf_paths(plugin_record()))

    def test_no_version_survives_into_the_record_at_all(self):
        # Review j#92092 finding 3: constraining `version` to a version-shaped
        # alphabet was not a closed representation — an alphanumeric marker passed
        # straight through. Closed means core owns the value, not that we narrowed
        # its shape, so the field is gone. Identity is the commit pin.
        self.assertNotIn(
            "version", {field.name for field in dataclasses.fields(PluginObservation)}
        )
        for value in (LEAK_MARKER, "LEAKEDSECRETVALUE0123456789", "1.14.0"):
            with self.subTest(value=value):
                status = self._status(plugin_record(version=value))
                rendered = json.dumps(status.as_payload()) + ops.format_status_text(
                    status
                )
                self.assertNotIn(value, rendered)

    def test_an_unrecognized_source_kind_is_projected_not_echoed(self):
        status = self._status(
            plugin_record(source={"kind": LEAK_MARKER, "owner": "o", "repo": "r"})
        )
        verdict = status.verdicts[0]
        self.assertEqual(verdict.observation.source_kind, SOURCE_KIND_UNRECOGNIZED)
        rendered = json.dumps(status.as_payload()) + ops.format_status_text(status)
        self.assertNotIn(LEAK_MARKER, rendered)

    def test_the_known_source_kinds_are_still_distinguished(self):
        cases = (
            (..., SOURCE_KIND_GITHUB),
            ({"kind": "link"}, SOURCE_KIND_LINK),
            (None, SOURCE_KIND_ABSENT),
        )
        for source, expected in cases:
            with self.subTest(expected=expected):
                status = self._status(plugin_record(source=source))
                self.assertEqual(status.verdicts[0].observation.source_kind, expected)

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
        self.assertEqual(plan.decision.reason, REASON_TARGET_NOT_INSTALLED)

    def test_plan_enable_admits_the_reviewed_ux_only_plugin(self):
        plan = ops.plan_enable(self._status(plugin_record()), "herdr-file-viewer")
        self.assertTrue(plan.ok)

    def test_plan_enable_refuses_an_inventory_that_did_not_fully_read(self):
        # Review j#92053 finding 2: status.ok already said "not fully classified",
        # but the enable answer — the one that gates an operator action — was drawn
        # from the readable remainder and came back admitted.
        broken = plugin_record(plugin_id="herdr-file-viewer")
        broken["enabled"] = "yes"
        status = self._status(plugin_record(), broken)
        self.assertFalse(status.fully_read)
        plan = ops.plan_enable(status, "herdr-file-viewer")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.decision.reason, REASON_INVENTORY_INCOMPLETE)

    def test_an_unreadable_record_blocks_even_an_unrelated_plugin_id(self):
        # The unreadable record may BE the plugin asked about, or a second claimant
        # to its id — neither is knowable, so the id asked about cannot narrow it.
        broken = plugin_record(plugin_id="something-else")
        broken["enabled"] = 1
        status = self._status(plugin_record(), broken)
        plan = ops.plan_enable(status, "herdr-file-viewer")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.decision.reason, REASON_INVENTORY_INCOMPLETE)

    def test_a_duplicate_plugin_id_is_ambiguous_not_first_match(self):
        # Same id, two different sources — one of them deny-classified. Answering
        # from the first match answers about a different plugin than the one an
        # `herdr plugin enable <id>` would affect.
        impostor = plugin_record(
            plugin_id="herdr-file-viewer",
            owner="persiyanov",
            repo="herdr-reviewr",
            commit=OTHER_COMMIT,
        )
        status = self._status(plugin_record(), impostor)
        self.assertTrue(status.fully_read)
        plan = ops.plan_enable(status, "herdr-file-viewer")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.decision.reason, REASON_AMBIGUOUS_TARGET)
        self.assertIsNone(plan.verdict)

    def test_the_enable_plan_payload_always_carries_its_decision(self):
        for plugin_id in ("herdr-file-viewer", "not-installed"):
            with self.subTest(plugin_id=plugin_id):
                plan = ops.plan_enable(self._status(plugin_record()), plugin_id)
                payload = plan.as_payload()
                self.assertEqual(payload["ok"], plan.decision.admitted)
                self.assertEqual(
                    payload["decision"]["reason"], plan.decision.reason
                )


#: A minimal VALID construction for every renderable DTO in this subsystem. The
#: test below asserts this table covers every frozen dataclass the two modules
#: export that carries a string field — so a DTO added later is swept without
#: anyone remembering to add it. Four review rounds were each lost to a
#: hand-enumeration of surfaces, so the enumeration is checked rather than trusted.
def _dto_samples():
    valid_ref = PluginSourceRef.pinned(
        SOURCE_KIND_GITHUB, "smarzban", "herdr-file-viewer", FILE_VIEWER_COMMIT
    )
    observation = observe_plugin(plugin_record())
    decision = PolicyDecision.deny(REASON_UNPINNED_SOURCE, "why")
    verdict = classify_plugin(observation)
    return {
        PluginSourceRef: dict(
            kind=SOURCE_KIND_GITHUB, owner="o", repo="r", commit=FILE_VIEWER_COMMIT
        ),
        ReviewedPlugin: dict(
            ref=valid_ref,
            plugin_id="p",
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_NONE,
            review_anchor="#0 j#0",
            rationale="fixture",
            manifest_digest=TEST_MANIFEST_DIGEST,
        ),
        PluginObservation: dict(
            plugin_id="p",
            enabled=True,
            source_kind=SOURCE_KIND_GITHUB,
            ref=None,
            declares_build=False,
            declares_panes=False,
            declares_actions=False,
            manifest_digest=TEST_MANIFEST_DIGEST,
            manifest_warnings=False,
        ),
        PolicyDecision: dict(admitted=False, reason=REASON_UNPINNED_SOURCE, detail="d"),
        PluginVerdict: dict(
            observation=observation,
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_NONE,
            review_anchor="#0 j#0",
            enable=decision,
            install=decision,
        ),
        ops.MalformedEntry: dict(index=0, detail="d"),
        ops.PolicyStatus: dict(verdicts=(verdict,), malformed=()),
        ops.EnablePlan: dict(
            plugin_id="herdr-file-viewer", found=True, verdict=verdict, decision=decision
        ),
        ops.InstallPlan: dict(spec="o/r", ref=valid_ref, decision=decision),
    }


class RenderableDtoBoundaryTests(unittest.TestCase):
    """Every renderable DTO closes its own text — not just the factory that builds it.

    Review j#92141 finding 1: the factories normalized and the value objects did
    not, so `EnablePlan(...)`, `dataclasses.replace(...)`, `MalformedEntry(...)`,
    `PolicyDecision.admit(...)` and `PluginVerdict(...)` each carried a path and a
    forged `BREACH:` line into the report. That is the same gap as the three
    rounds before it, one layer out.
    """

    HOSTILE = (
        LEAK_MARKER,
        "a\nBREACH: forged\nb",
        "bell\x07here",
        "x" * 5000,
    )

    def test_the_sample_table_covers_every_renderable_dto(self):
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_identity as identity
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_policy as policy

        found = set()
        for module in (identity, policy, ops):
            for name in getattr(module, "__all__", ()):
                obj = getattr(module, name, None)
                if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                    if any(
                        field.type in ("str", "Optional[str]")
                        or field.name in {"detail", "spec", "plugin_id"}
                        for field in dataclasses.fields(obj)
                    ):
                        found.add(obj)
        self.assertEqual(
            found - set(_dto_samples()),
            set(),
            "a renderable DTO is not covered by the sweep table",
        )

    def test_no_renderable_dto_accepts_hostile_text(self):
        # Either the constructor refuses, or the stored value is already safe
        # (the sanitizing boundary). Both end with a record that cannot render a
        # path or forge a line; they differ only in who is at fault.
        failures = []
        for cls, kwargs in _dto_samples().items():
            for field in dataclasses.fields(cls):
                if not isinstance(kwargs.get(field.name), str):
                    continue
                for hostile in self.HOSTILE:
                    try:
                        built = cls(**{**kwargs, field.name: hostile})
                    except HerdrPluginPolicyError:
                        continue
                    stored = getattr(built, field.name)
                    if _ABS_PATH_TOKEN.search(stored) or any(
                        ch in stored for ch in "\n\r\x07\x00"
                    ):
                        failures.append(f"{cls.__name__}.{field.name}")
        self.assertEqual(failures, [], f"DTO field(s) accepted hostile text: {failures}")

    def test_replace_cannot_reopen_a_closed_plan(self):
        plan = ops.plan_enable(
            ops.PolicyStatus(verdicts=(), malformed=()), "herdr-file-viewer"
        )
        for hostile in self.HOSTILE:
            with self.subTest(hostile=hostile[:20]):
                with self.assertRaises(HerdrPluginPolicyError):
                    dataclasses.replace(plan, plugin_id=hostile)


#: Absolute-path forms derived from the POSIX / Windows *specification*, not from
#: production's regex. Review j#92194 F1: the previous oracle was written in the
#: same shape as the implementation (`/segment/`), so both were blind to `/etc`,
#: `/`, `/秘密` and `/tmp-☃/secret` at once — two layers that fail together are one
#: layer. An oracle only tests a detector if it was derived independently.
SPEC_ABSOLUTE_PATHS = (
    "/etc",                    # single component
    "/",                       # the root itself
    "/秘密",                    # non-ASCII component
    "/tmp-☃/secret",           # mixed alphabet
    "/etc/passwd",             # multi component
    "/a//b",                   # doubled separator
    "/trailing/",              # trailing separator
    "C:/Users/x",              # drive root, forward slash
    "C:\\Users\\x",            # drive root, backslash
    "\\\\server\\share",       # UNC root
    "config:/Users/x",         # labelled absolute path
    "~/private/project",       # current-user POSIX home shorthand
    "~synthetic-user/private/project",  # named-user POSIX home shorthand
    "~\\synthetic\\private\\project",  # PowerShell home shorthand
    "config:~synthetic-user/private",  # labelled home shorthand
)

#: Strings that must NOT be read as absolute paths, or the guard becomes a denial
#: of service on this surface's own output.
SPEC_NON_PATHS = (
    "github:smarzban/herdr-file-viewer@" + FILE_VIEWER_COMMIT,
    "install/enable",
    "relative/path.yaml",
    "prefix~synthetic-user/private",  # tilde is inside a relative token
    "owner/repo",
    "plain diagnostic prose with no separator",
    "",
)


class PathDetectorOracleTests(unittest.TestCase):
    """The detector, judged against a specification-derived corpus."""

    def test_every_specified_absolute_path_is_detected(self):
        missed = [
            value
            for value in SPEC_ABSOLUTE_PATHS
            if not ops.contains_absolute_path(value)
        ]
        self.assertEqual(missed, [], f"absolute path(s) not detected: {missed}")

    def test_no_legitimate_string_is_detected_as_a_path(self):
        flagged = [
            value for value in SPEC_NON_PATHS if ops.contains_absolute_path(value)
        ]
        self.assertEqual(flagged, [], f"non-path(s) wrongly detected: {flagged}")

    def test_a_path_after_a_line_break_is_detected(self):
        # The bug reusing the hardened patterns without the hardened structure:
        # `$` also matches before a trailing newline, so the character ending the
        # PREVIOUS line satisfied the relative-continuation proof.
        for value in SPEC_ABSOLUTE_PATHS:
            with self.subTest(value=value):
                self.assertTrue(ops.contains_absolute_path(f"a line\n{value} tail"))

    def test_the_detector_is_the_repository_wide_rule(self):
        # Review j#92241 F3: the previous version asserted only that the two
        # modules shared the regex OBJECTS — which the claim "one rule, one place"
        # survives while the *positive-proof predicate* is implemented twice. A
        # test that cannot falsify the claim it guards is not guarding it. Both
        # halves of the rule are now pinned by function identity.
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction as redaction
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_identity as identity

        self.assertIs(redaction._ABS_ROOT_RE, identity._ABS_ROOT_RE)
        self.assertIs(
            redaction._RELATIVE_CONTINUATION_RE, identity._RELATIVE_CONTINUATION_RE
        )
        self.assertIs(redaction._keeps_absolute_root, identity.keeps_absolute_root)
        for value in SPEC_ABSOLUTE_PATHS:
            with self.subTest(value=value):
                self.assertNotEqual(redaction.redact_probe_paths(value), value)

    def test_both_consumers_agree_on_the_whole_corpus(self):
        # Behavioural half of the same claim: sharing the callables is the
        # mechanism, agreeing on every input is the property.
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_probe_redaction as redaction

        for value in SPEC_ABSOLUTE_PATHS:
            with self.subTest(value=value, expected="path"):
                self.assertTrue(ops.contains_absolute_path(value))
                self.assertNotEqual(redaction.redact_probe_paths(value), value)
        for value in SPEC_NON_PATHS:
            with self.subTest(value=value, expected="not a path"):
                self.assertFalse(ops.contains_absolute_path(value))
                self.assertEqual(redaction.redact_probe_paths(value), value)


class EdgeMatrixTests(unittest.TestCase):
    """Every named edge from the j#92149 sweep, pinned directly.

    Review j#92194 F3: the previous version enumerated dataclasses automatically
    and so silently dropped `InventoryReadError` — which is not a dataclass, and
    was the one edge I had found myself. The matrix is now written by name, and a
    coverage assertion checks it against the sweep's edge list in both directions.
    """

    SWEPT_EDGES = {
        "E1_inventory_record",
        "E2_operand_factory",
        "E3_enable_plan_direct",
        "E4_install_plan_direct",
        "E5_dataclasses_replace",
        "E6_malformed_entry_detail",
        "E7_policy_decision_detail",
        "E8_plugin_verdict_anchor",
        "E9_inventory_read_error_detail",
        "E10_inventory_document_unreadable",
    }

    def _edges(self, hostile):
        """Each named edge as a callable producing the artifact it can render."""
        empty = ops.PolicyStatus(verdicts=(), malformed=())

        def rendered(status):
            return json.dumps(status.as_payload()) + ops.format_status_text(status)

        def e1():
            record = plugin_record()
            record["version"] = hostile
            record["source"] = {"kind": hostile}
            return rendered(
                ops.classify_inventory(
                    ops.parse_inventory(inventory_document(record))
                )
            )

        def e2():
            plan = ops.plan_enable(empty, hostile)
            return json.dumps(plan.as_payload()) + ops.format_enable_plan_text(plan)

        def e3():
            plan = ops.EnablePlan(
                plugin_id=hostile,
                found=True,
                verdict=None,
                decision=PolicyDecision.deny(REASON_TARGET_NOT_INSTALLED, "x"),
            )
            return json.dumps(plan.as_payload()) + ops.format_enable_plan_text(plan)

        def e4():
            plan = ops.InstallPlan(
                spec=hostile,
                ref=None,
                decision=PolicyDecision.deny(REASON_UNPINNED_SOURCE, "x"),
            )
            return json.dumps(plan.as_payload()) + ops.format_install_plan_text(plan)

        def e5():
            status = ops.classify_inventory(
                ops.parse_inventory(inventory_document(plugin_record()))
            )
            plan = dataclasses.replace(
                ops.plan_enable(status, "herdr-file-viewer"), plugin_id=hostile
            )
            return json.dumps(plan.as_payload()) + ops.format_enable_plan_text(plan)

        def e6():
            return rendered(
                ops.PolicyStatus(
                    verdicts=(),
                    malformed=(ops.MalformedEntry(index=0, detail=hostile),),
                )
            )

        def e7():
            return PolicyDecision.admit(hostile).detail

        def e8():
            status = ops.classify_inventory(
                ops.parse_inventory(inventory_document(plugin_record()))
            )
            return dataclasses.replace(
                status.verdicts[0], review_anchor=hostile
            ).review_anchor

        def e9():
            return ops.format_read_error_text(
                ops.InventoryReadError(ops.READ_HERDR_ERROR, hostile)
            )

        def e10():
            try:
                ops.read_inventory_document(Path(hostile))
            except ops.InventoryReadError as exc:
                return ops.format_read_error_text(exc)
            except (OSError, ValueError):
                return ""
            return ""

        return {
            "E1_inventory_record": e1,
            "E2_operand_factory": e2,
            "E3_enable_plan_direct": e3,
            "E4_install_plan_direct": e4,
            "E5_dataclasses_replace": e5,
            "E6_malformed_entry_detail": e6,
            "E7_policy_decision_detail": e7,
            "E8_plugin_verdict_anchor": e8,
            "E9_inventory_read_error_detail": e9,
            "E10_inventory_document_unreadable": e10,
        }

    def test_the_matrix_covers_exactly_the_swept_edges(self):
        # Both directions: a swept edge with no probe, and a probe for an edge the
        # sweep never named, are each a gap.
        self.assertEqual(set(self._edges("x")), self.SWEPT_EDGES)

    def test_no_edge_renders_a_path_or_a_forged_line(self):
        # The two invariants, asked separately. Conflating them with `or` is how I
        # misread two edges as leaking during the sweep itself.
        # The forged-line probe must use a marker that ONLY the hostile input can
        # produce. An earlier version looked for a line starting with `BREACH:` —
        # but that is a line this report legitimately writes when an enabled plugin
        # is inadmissible, so every edge that produced a real breach read as a
        # forgery. Same mistake as during the sweep itself: a predicate that cannot
        # tell our own output from injected output measures nothing.
        forge_marker = "ZZFORGEDLINEZZ"
        hostile_values = (
            LEAK_MARKER,
            "/etc",
            "/",
            "/秘密",
            f"a\n{forge_marker}\nb",
            "bell\x07here",
        )
        path_leaks, forged_lines = [], []
        for hostile in hostile_values:
            for name, edge in self._edges(hostile).items():
                try:
                    artifact = edge()
                except HerdrPluginPolicyError:
                    continue  # refused at construction is closed
                if ops.contains_absolute_path(artifact):
                    path_leaks.append((name, hostile[:16]))
                if any(
                    line.strip().startswith(forge_marker)
                    for line in artifact.splitlines()
                ) or any(ch in artifact for ch in "\r\x07\x00"):
                    forged_lines.append((name, hostile[:16]))
        self.assertEqual(path_leaks, [], f"path reached a report: {path_leaks}")
        self.assertEqual(forged_lines, [], f"forged line: {forged_lines}")


class RelationalInvariantTests(unittest.TestCase):
    """Field-wise validity is not consistency (review j#92194 F2).

    Every DTO here passed its per-field checks while carrying a combination the
    policy would never produce — an observation that could not recognize its own
    source yet classified as `ux_only`, an admitted plan with nothing behind it, a
    verdict whose class said `unknown` while its decision said admit (so an
    enabled, unreviewed plugin reported `breach=False`).
    """

    def _status(self):
        return ops.classify_inventory(
            ops.parse_inventory(inventory_document(plugin_record()))
        )

    def test_an_unrecognized_source_cannot_carry_a_resolved_reference(self):
        reviewed_pin = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "smarzban", "herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        for kind in (SOURCE_KIND_UNRECOGNIZED, SOURCE_KIND_LINK, SOURCE_KIND_ABSENT):
            with self.subTest(kind=kind):
                with self.assertRaises(HerdrPluginPolicyError):
                    PluginObservation(
                        plugin_id="herdr-file-viewer",
                        enabled=True,
                        source_kind=kind,
                        ref=reviewed_pin,
                        declares_build=True,
                        declares_panes=True,
                        declares_actions=True,
                        manifest_digest=TEST_MANIFEST_DIGEST,
                        manifest_warnings=False,
                    )

    def test_a_verdict_cannot_disagree_with_the_policy(self):
        real = self._status().verdicts[0]
        divergences = (
            dict(plugin_class=CLASS_UNKNOWN),
            dict(build_provenance=BUILD_NONE),
            dict(review_anchor="not the reviewed anchor"),
            dict(enable=PolicyDecision.admit("invented")),
            dict(install=PolicyDecision.admit("invented")),
            dict(enable=PolicyDecision.deny(REASON_AGENT_INPUT_WRITER, "invented")),
        )
        for divergence in divergences:
            with self.subTest(divergence=sorted(divergence)):
                with self.assertRaises(HerdrPluginPolicyError):
                    dataclasses.replace(real, **divergence)

    def test_an_unreviewed_plugin_cannot_be_given_an_admitting_verdict(self):
        unknown = observe_plugin(
            plugin_record(plugin_id="x", owner="s", repo="x", commit=OTHER_COMMIT)
        )
        with self.assertRaises(HerdrPluginPolicyError):
            PluginVerdict(
                observation=unknown,
                plugin_class=CLASS_UNKNOWN,
                build_provenance=BUILD_UNREVIEWED,
                review_anchor="",
                enable=PolicyDecision.admit("invented"),
                install=PolicyDecision.admit("invented"),
            )

    def test_the_real_verdict_still_constructs(self):
        # Positive control: recomputation must accept what the policy produces, or
        # the invariant is a denial of service rather than a boundary.
        real = self._status().verdicts[0]
        self.assertTrue(real.enable.admitted)
        dataclasses.replace(real, observation=real.observation)

    def test_an_admitted_enable_plan_needs_something_behind_it(self):
        status = self._status()
        real = ops.plan_enable(status, "herdr-file-viewer")
        self.assertTrue(real.ok)
        broken = (
            dict(found=False),
            dict(verdict=None),
            dict(plugin_id=None),
            dict(plugin_id="a-different-plugin"),
        )
        for divergence in broken:
            with self.subTest(divergence=sorted(divergence)):
                with self.assertRaises(HerdrPluginPolicyError):
                    dataclasses.replace(real, **divergence)
        with self.assertRaises(HerdrPluginPolicyError):
            ops.EnablePlan(
                plugin_id=None,
                found=False,
                verdict=None,
                decision=PolicyDecision.admit("nothing behind it"),
            )

    def test_an_admitted_install_plan_needs_a_pinned_reference(self):
        for ref in (
            None,
            PluginSourceRef.repository(SOURCE_KIND_GITHUB, "smarzban", "x"),
        ):
            with self.subTest(ref=ref):
                with self.assertRaises(HerdrPluginPolicyError):
                    ops.InstallPlan(
                        spec="smarzban/x",
                        ref=ref,
                        decision=PolicyDecision.admit("nothing pinned"),
                    )

    def test_a_denied_plan_may_carry_no_reference(self):
        # A denial legitimately has nothing behind it, and refusing that would
        # break every deny path. What it may NOT do is disagree with the policy —
        # see the recomputation tests below.
        ops.InstallPlan(spec=None, ref=None, decision=plan_install(None))
        ops.EnablePlan(
            plugin_id=None,
            found=False,
            verdict=None,
            decision=PolicyDecision.deny(REASON_INVALID_TARGET_ID, "not an id"),
        )

    def test_an_install_plan_cannot_invert_the_policy(self):
        # Review j#92241 F1: the admitted precondition asked only for a pinned
        # ref, so a reference the policy DENIES (`unpinned_remote_build`) could be
        # handed an invented admit and rendered `ok=true`. `PluginVerdict` already
        # recomputed; the plans got a weaker rule for the same question.
        reviewed = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "smarzban", "herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        self.assertFalse(plan_install(reviewed).admitted)
        with self.assertRaises(HerdrPluginPolicyError):
            ops.InstallPlan(
                spec="smarzban/herdr-file-viewer",
                ref=reviewed,
                decision=PolicyDecision.admit("invented"),
            )
        # A *denial* that is not the policy's denial is equally inconsistent.
        with self.assertRaises(HerdrPluginPolicyError):
            ops.InstallPlan(
                spec="smarzban/herdr-file-viewer",
                ref=reviewed,
                decision=PolicyDecision.deny(REASON_AGENT_INPUT_WRITER, "invented"),
            )

    def test_an_install_plan_spec_must_name_its_reference(self):
        reviewed = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "smarzban", "herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        with self.assertRaises(HerdrPluginPolicyError):
            ops.InstallPlan(
                spec="someone/else",
                ref=reviewed,
                decision=plan_install(reviewed),
            )
        unit_board = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            "hollySizzle",
            "mozyo_bridge",
            UNIT_BOARD_COMMIT,
            subdir=UNIT_BOARD_SUBDIR,
        )
        with self.assertRaises(HerdrPluginPolicyError):
            ops.InstallPlan(
                spec="hollySizzle/mozyo_bridge/herdr-plugins/another-plugin",
                ref=unit_board,
                decision=plan_install(unit_board),
            )

    def test_the_real_install_plan_still_constructs(self):
        for spec, ref in (
            ("smarzban/herdr-file-viewer", FILE_VIEWER_COMMIT),
            ("persiyanov/herdr-reviewr", OTHER_COMMIT),
            ("someone/unreviewed", OTHER_COMMIT),
        ):
            with self.subTest(spec=spec):
                plan = ops.plan_candidate_install(spec, ref)
                self.assertFalse(plan.ok)

    def test_an_enable_plan_cannot_disagree_with_its_verdict(self):
        # Review j#92241 F2: the deny path was left unconstrained, so a plan could
        # carry an enable-admitted verdict while its own decision denied — and the
        # two renderers then answered the same question oppositely.
        status = self._status()
        real = ops.plan_enable(status, "herdr-file-viewer")
        for divergence in (
            dict(found=False),
            dict(decision=PolicyDecision.deny(REASON_TARGET_NOT_INSTALLED, "x")),
            dict(decision=PolicyDecision.admit("different detail")),
            dict(plugin_id="another-plugin"),
        ):
            with self.subTest(divergence=sorted(divergence)):
                with self.assertRaises(HerdrPluginPolicyError):
                    dataclasses.replace(real, **divergence)

    def _planner_verdictless_states(self):
        """Drive the planner and collect the verdictless states it actually reaches.

        Derived from the planner rather than hand-listed. Review j#92285 F1: the
        previous version of this test used one `(id present, found=False)` shape
        for all four reasons, so it never looked at what each reason *means* — and
        the constructor, checking only the reason set, accepted `found=True` beside
        `target_not_installed`.
        """
        clean = ops.classify_inventory(ops.parse_inventory(inventory_document()))
        installed = ops.classify_inventory(
            ops.parse_inventory(inventory_document(plugin_record()))
        )
        duplicated = ops.classify_inventory(
            ops.parse_inventory(
                inventory_document(
                    plugin_record(),
                    plugin_record(
                        plugin_id="herdr-file-viewer",
                        owner="persiyanov",
                        repo="herdr-reviewr",
                        commit=OTHER_COMMIT,
                    ),
                )
            )
        )
        unreadable = ops.PolicyStatus(
            verdicts=(), malformed=(ops.MalformedEntry(index=0, detail="x"),)
        )
        drives = (
            (clean, LEAK_MARKER),
            (clean, "herdr-file-viewer"),
            (unreadable, "herdr-file-viewer"),
            (duplicated, "herdr-file-viewer"),
            (installed, "herdr-file-viewer"),
        )
        states = {}
        for status, operand in drives:
            plan = ops.plan_enable(status, operand)
            if plan.verdict is None:
                states[plan.decision.reason] = (plan.plugin_id is not None, plan.found)
        return states

    #: What each verdictless situation MEANS, written from the situation rather
    #: than read out of the table. Once the planner started deriving its state
    #: from `VERDICTLESS_ENABLE_STATES`, comparing planner output against that
    #: table became comparing the table with itself — a mutation of the table
    #: passed unnoticed (measured). An expectation has to come from somewhere the
    #: subject cannot reach.
    EXPECTED_VERDICTLESS_MEANING = {
        # reason: (is the id echoed back?, did anything answer to it?)
        # not an identifier at all -> nothing to echo, nothing found
        REASON_INVALID_TARGET_ID: (False, False),
        # a real id, but the inventory could not be fully read -> no answer either way
        REASON_INVENTORY_INCOMPLETE: (True, False),
        # more than one plugin answers to it -> plugins WERE found, just not one
        REASON_AMBIGUOUS_TARGET: (True, True),
        # no plugin answers to it -> nothing found
        REASON_TARGET_NOT_INSTALLED: (True, False),
    }

    def test_the_verdictless_table_says_what_each_situation_means(self):
        self.assertEqual(
            dict(ops.VERDICTLESS_ENABLE_STATES), self.EXPECTED_VERDICTLESS_MEANING
        )

    def test_the_planner_reaches_exactly_those_situations(self):
        # source-to-set coverage: every verdictless outcome the planner can
        # produce is in the table, and every table entry is reachable.
        self.assertEqual(
            self._planner_verdictless_states(), self.EXPECTED_VERDICTLESS_MEANING
        )

    def test_each_verdictless_reason_accepts_only_its_own_state(self):
        for reason, (id_present, found) in ops.VERDICTLESS_ENABLE_STATES.items():
            plugin_id = "herdr-file-viewer" if id_present else None
            with self.subTest(reason=reason, control="positive"):
                ops.EnablePlan(
                    plugin_id=plugin_id,
                    found=found,
                    verdict=None,
                    decision=PolicyDecision.deny(reason, "d"),
                )
            # One field at a time, so each mutation is attributable.
            for mutation in ("plugin_id", "found"):
                with self.subTest(reason=reason, mutated=mutation):
                    kwargs = dict(
                        plugin_id=plugin_id,
                        found=found,
                        verdict=None,
                        decision=PolicyDecision.deny(reason, "d"),
                    )
                    if mutation == "plugin_id":
                        kwargs["plugin_id"] = None if id_present else "herdr-file-viewer"
                    else:
                        kwargs["found"] = not found
                    with self.assertRaises(HerdrPluginPolicyError):
                        ops.EnablePlan(**kwargs)

    def test_a_per_plugin_reason_cannot_be_reached_without_its_plugin(self):
        for reason in (REASON_AGENT_INPUT_WRITER, REASON_UNPINNED_SOURCE):
            with self.subTest(reason=reason):
                with self.assertRaises(HerdrPluginPolicyError):
                    ops.EnablePlan(
                        plugin_id="herdr-file-viewer",
                        found=False,
                        verdict=None,
                        decision=PolicyDecision.deny(reason, "d"),
                    )

    def test_an_install_plan_names_a_target_or_names_none(self):
        # Review j#92285 F2: one-sided presence let the record say "<withheld>"
        # while disclosing the exact source, or name a target while reporting
        # source=null / class=unknown. The factory can produce neither.
        reviewed = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "smarzban", "herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        with self.assertRaises(HerdrPluginPolicyError):
            ops.InstallPlan(spec=None, ref=reviewed, decision=plan_install(reviewed))
        with self.assertRaises(HerdrPluginPolicyError):
            ops.InstallPlan(
                spec="smarzban/herdr-file-viewer",
                ref=None,
                decision=plan_install(None),
            )
        real = ops.plan_candidate_install(
            "smarzban/herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        for divergence in (dict(spec=None), dict(ref=None)):
            with self.subTest(divergence=sorted(divergence)):
                with self.assertRaises(HerdrPluginPolicyError):
                    dataclasses.replace(real, **divergence)

    def test_the_factory_never_produces_a_one_sided_install_plan(self):
        for spec, ref in (
            ("smarzban/herdr-file-viewer", FILE_VIEWER_COMMIT),
            ("smarzban/herdr-file-viewer", None),
            ("smarzban/herdr-file-viewer", "not-a-commit"),
            ("no-separator", FILE_VIEWER_COMMIT),
            ("bad owner/repo", FILE_VIEWER_COMMIT),
            (LEAK_MARKER, FILE_VIEWER_COMMIT),
        ):
            with self.subTest(spec=spec[:24], ref=ref):
                plan = ops.plan_candidate_install(spec, ref)
                self.assertEqual(plan.spec is None, plan.ref is None)

    def test_the_two_renderings_never_disagree_about_admission(self):
        # The property the inconsistency actually broke: a machine-readable and a
        # human-readable artifact answering the same question oppositely.
        status = self._status()
        plans = [
            ops.plan_enable(status, "herdr-file-viewer"),
            ops.plan_enable(status, "not-installed"),
            ops.plan_enable(status, LEAK_MARKER),
            ops.plan_enable(
                ops.PolicyStatus(
                    verdicts=(),
                    malformed=(ops.MalformedEntry(index=0, detail="unreadable"),),
                ),
                "herdr-file-viewer",
            ),
        ]
        for plan in plans:
            with self.subTest(plugin_id=plan.plugin_id):
                payload = plan.as_payload()
                text = ops.format_enable_plan_text(plan)
                # Read the plan's OWN answer line. Scanning the whole block for
                # "ADMITTED"/"DENIED" was imprecise: the verdict context
                # legitimately reports a denied *install* beside an admitted
                # enable, so a whole-text scan measured the wrong thing.
                answer = [
                    line for line in text.splitlines() if line.startswith("enable ")
                ]
                self.assertEqual(len(answer), 1, f"expected one answer line: {answer}")
                admitted_in_text = "ADMITTED" in answer[0]
                self.assertEqual(
                    payload["ok"],
                    admitted_in_text,
                    f"JSON says ok={payload['ok']} but text says "
                    f"admitted={admitted_in_text}",
                )


class ImmutableAuthorityTests(unittest.TestCase):
    """A policy authority must be unwritable, not merely single (review j#92330).

    Collecting the planner and the constructor onto one table was the right fix,
    and it made this worse: a mutable shared table moves *both* readers at once,
    so a state outside the closed vocabulary becomes justified on both sides
    simultaneously. "One authority" and "an authority nobody can rewrite" are
    different properties; the round that delivered the first claimed the second.
    """

    def test_no_module_level_mapping_authority_is_writable(self):
        # Mechanical, because three hand-written names is exactly the enumeration
        # that has been wrong every time. Every public module-level mapping in
        # this subsystem is checked, so one added later is covered without anyone
        # remembering.
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.absolute_path_rule as rule
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_identity as identity
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_policy as policy

        writable = []
        for module in (rule, identity, policy, ops):
            for name in dir(module):
                if name.startswith("__"):
                    continue
                value = getattr(module, name)
                if isinstance(value, dict):
                    writable.append(f"{module.__name__.rsplit('.', 1)[-1]}.{name}")
        self.assertEqual(
            writable, [], f"writable module-level mapping authority: {writable}"
        )

    def _assert_read_only_mapping(self, mapping, sample_key, sample_value):
        """Assert a mapping refuses writes — WITHOUT risking a successful one.

        The type is checked first and the mutations are attempted only once it is
        known to be read-only. Written the other way round, this test corrupted
        module state whenever it failed: the assignment simply succeeded, the
        `assertRaises` reported a failure, and the injected entry stayed in the
        registry for every later test in the process (33 unrelated failures,
        measured). A regression that damages the run when it fails cannot be
        trusted to report anything.
        """
        self.assertIsInstance(mapping, MappingProxyType)
        with self.assertRaises(TypeError):
            mapping[sample_key] = sample_value
        with self.assertRaises(TypeError):
            del mapping[next(iter(mapping))]

    def test_the_verdictless_table_rejects_every_mutation(self):
        self._assert_read_only_mapping(
            ops.VERDICTLESS_ENABLE_STATES, REASON_AGENT_INPUT_WRITER, (True, False)
        )
        with self.assertRaises(TypeError):
            ops.VERDICTLESS_ENABLE_STATES[REASON_TARGET_NOT_INSTALLED] = (False, True)

    def test_the_reviewers_probe_no_longer_admits_a_forbidden_reason(self):
        # The exact three-step probe from j#92330, as a regression.
        self.assertIsInstance(ops.VERDICTLESS_ENABLE_STATES, MappingProxyType)
        with self.assertRaises(TypeError):
            ops.VERDICTLESS_ENABLE_STATES[REASON_AGENT_INPUT_WRITER] = (True, False)
        self.assertNotIn(REASON_AGENT_INPUT_WRITER, ops.VERDICTLESS_ENABLE_STATES)
        with self.assertRaises(HerdrPluginPolicyError):
            ops.EnablePlan(
                plugin_id="herdr-file-viewer",
                found=False,
                verdict=None,
                decision=PolicyDecision.deny(REASON_AGENT_INPUT_WRITER, "d"),
            )

    def test_the_reason_view_can_never_go_stale(self):
        # It was an import-time snapshot beside a mutable table, so the two could
        # disagree. Derived from the table, they cannot.
        self.assertEqual(set(ops.VERDICTLESS_ENABLE_STATES), ops.VERDICTLESS_ENABLE_REASONS)

    def test_the_reviewed_registry_rejects_every_mutation(self):
        # Self-reported alongside j#92330 and heavier than it: injecting an allow
        # entry made an arbitrary plugin `ux_only` and enable-admitted, which is
        # the close condition ("admit is reviewed ux_only x established identity,
        # and nothing else") broken outright. Construction-time checks on the
        # registry are worth nothing if it can be rewritten afterwards.
        forged_ref = PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB, "attacker", "evil-plugin", FILE_VIEWER_COMMIT
        )
        entry = ReviewedPlugin(
            ref=forged_ref,
            plugin_id="evil-plugin",
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_NONE,
            review_anchor="#0 j#0",
            rationale="injected",
            manifest_digest=TEST_MANIFEST_DIGEST,
        )
        self._assert_read_only_mapping(REVIEWED_PLUGINS, forged_ref, entry)
        verdict = classify_plugin(
            observe_plugin(
                plugin_record(
                    plugin_id="evil-plugin",
                    owner="attacker",
                    repo="evil-plugin",
                    commit=FILE_VIEWER_COMMIT,
                    build=False,
                )
            )
        )
        self.assertEqual(verdict.plugin_class, CLASS_UNKNOWN)
        self.assertFalse(verdict.enable.admitted)

    def test_the_observation_validator_table_rejects_every_mutation(self):
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_plugin_identity as identity

        self._assert_read_only_mapping(
            identity._OBSERVATION_FIELD_CHECKS, "declares_build", lambda v, n: None
        )


class SinkGuardTests(unittest.TestCase):
    """The second layer: the one place everything leaves through."""

    def test_the_guard_refuses_a_forged_artifact(self):
        for artifact in (
            f"line\n{LEAK_MARKER} tail",
            "line\x07bell",
            "a\x00b",
        ):
            with self.subTest(artifact=artifact[:20]):
                with self.assertRaises(ops.RenderGuardError):
                    ops.guard_rendered_text(artifact)

    def test_the_guard_refuses_a_forged_payload_at_any_depth(self):
        for payload in (
            {"a": LEAK_MARKER},
            {"a": [{"b": LEAK_MARKER}]},
            {"a": ["ok", "line\nbreak"]},
            {LEAK_MARKER: "v"},
        ):
            with self.subTest(payload=str(payload)[:30]):
                with self.assertRaises(ops.RenderGuardError):
                    ops.guard_rendered_payload(payload)

    def test_the_guard_passes_the_real_reports(self):
        # It must not fire on legitimate output, or it is a denial of service
        # rather than a boundary. The real status text contains "HOME /
        # XDG_CONFIG_HOME" and a "github:owner/repo@sha" identity, neither of
        # which is a path.
        status = ops.classify_inventory(
            ops.parse_inventory(inventory_document(plugin_record()))
        )
        ops.guard_rendered_text(ops.format_status_text(status))
        ops.guard_rendered_payload(status.as_payload())
        plan = ops.plan_enable(status, "herdr-file-viewer")
        ops.guard_rendered_text(ops.format_enable_plan_text(plan))
        ops.guard_rendered_payload(plan.as_payload())
        install = ops.plan_candidate_install(
            "smarzban/herdr-file-viewer", FILE_VIEWER_COMMIT
        )
        ops.guard_rendered_text(ops.format_install_plan_text(install))
        ops.guard_rendered_payload(install.as_payload())


class CliTests(unittest.TestCase):
    """The command surface: exit codes, and that it mutates nothing."""

    def setUp(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        register_herdr_plugin_policy_parser(sub)
        self.parser = parser
        temp_dir = _temp_dir()
        self.addCleanup(temp_dir.cleanup)
        self.inventory = Path(temp_dir.name) / "inventory.json"

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

    def test_empty_plan_operands_are_denied_without_reading_inventory(self):
        absent = str(self.inventory.parent / "must-not-be-read.json")
        cases = (
            ("--plan-install", "spec", REASON_UNPINNED_SOURCE),
            ("--plan-enable", "plugin_id", REASON_INVALID_TARGET_ID),
        )
        for flag, target_field, reason in cases:
            for as_json in (False, True):
                with self.subTest(flag=flag, json=as_json):
                    argv = ["--from-json", absent, flag, ""]
                    if as_json:
                        argv.append("--json")
                    with mock.patch.object(ops.subprocess, "run") as run, mock.patch(
                        "builtins.print"
                    ) as printed:
                        code = cmd_herdr_plugin_policy(self._parse(*argv))
                    self.assertEqual(code, 1)
                    run.assert_not_called()
                    rendered = printed.call_args.args[0]
                    if as_json:
                        payload = json.loads(rendered)
                        self.assertFalse(payload["ok"])
                        self.assertEqual(payload[target_field], "<withheld>")
                        decision = payload.get("decision") or payload.get("install")
                        self.assertEqual(decision["reason"], reason)
                    else:
                        self.assertIn("<withheld>", rendered.splitlines()[0])
                        self.assertIn(reason, rendered)

    def test_plan_install_without_a_ref_is_denied(self):
        args = self._parse("--plan-install", "smarzban/herdr-file-viewer")
        self.assertEqual(cmd_herdr_plugin_policy(args), 1)

    def test_exact_unit_board_plan_install_exits_zero_without_subprocess(self):
        args = self._parse(
            "--plan-install", UNIT_BOARD_SPEC, "--ref", UNIT_BOARD_COMMIT
        )
        with mock.patch.object(ops.subprocess, "run") as run:
            self.assertEqual(cmd_herdr_plugin_policy(args), 0)
        run.assert_not_called()

    def test_no_mode_ever_issues_a_mutating_herdr_subcommand(self):
        # The adversarial guard: whatever the mode, the only subprocess this surface
        # may reach for is the read-only inventory query.
        path = self._write(plugin_record())
        modes = (
            ["--from-json", path],
            ["--from-json", path, "--plan-enable", "herdr-file-viewer"],
            ["--plan-install", UNIT_BOARD_SPEC, "--ref", UNIT_BOARD_COMMIT],
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

    def test_the_cli_emits_nothing_when_the_sink_guard_fires(self):
        # Fail-closed at the exit: a report that would carry a private path or a
        # forged line must not be printed at all. Emitting a scrubbed version
        # would be worse — this text is written to be pasted into a record.
        args = self._parse("--from-json", self._write(plugin_record()))
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_distribution as cli

        with mock.patch.object(
            cli, "format_status_text", return_value=f"x\n{LEAK_MARKER}/y"
        ), mock.patch("builtins.print") as printed:
            code = cmd_herdr_plugin_policy(args)
        self.assertEqual(code, 1)
        emitted = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertNotIn(LEAK_MARKER, emitted)
        self.assertIn("render_guard", emitted)

    def test_enable_and_install_report_the_same_token_in_text_and_json(self):
        # Review j#92141 finding 2: JSON said `<withheld>` while the enable text
        # said `None`, because only the install formatter used the closed label.
        empty_inventory = self._write()
        for argv, extract in (
            (["--plan-enable", LEAK_MARKER], "plugin_id"),
            (["--plan-install", LEAK_MARKER], "spec"),
        ):
            with self.subTest(argv=argv[0]):
                args = self._parse("--from-json", empty_inventory, *argv, "--json")
                with mock.patch("builtins.print") as printed:
                    cmd_herdr_plugin_policy(args)
                token = json.loads(printed.call_args.args[0])[extract]
                args = self._parse("--from-json", empty_inventory, *argv)
                with mock.patch("builtins.print") as printed:
                    cmd_herdr_plugin_policy(args)
                text = printed.call_args.args[0]
                self.assertEqual(token, "<withheld>")
                self.assertIn(token, text.splitlines()[0])
                self.assertNotIn("None", text.splitlines()[0])

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
