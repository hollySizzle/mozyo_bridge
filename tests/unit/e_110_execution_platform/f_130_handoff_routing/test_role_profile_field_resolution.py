"""Send-side role-profile field resolution + verified-default auto-fill (Redmine #13477).

Task #13477 auto-resolves a role template's ``redmine_project`` placeholder from
the verified workspace-local Redmine default when the operator did not pass an
explicit ``--profile-field redmine_project=``. Priority mirrors the workspace
default-project contract: explicit wins; else the *verified* default; a missing
/ unverified / ambiguous default fails closed (never silently substituted).

Everything runs against temp dirs — no tmux, no real ``~/.mozyo_bridge``.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_defaults import (
    DEFAULT_PROJECT_CONFLICT,
    DEFAULT_PROJECT_MISSING,
    DEFAULT_PROJECT_UNVERIFIED,
    DEFAULT_PROJECT_VERIFIED,
    resolve_default_project,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.role_profile_field_resolution import (
    resolve_handoff_profile_fields,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileError,
)

_ANCHOR = "#13477 j#74480"

_DEFAULTS_YAML = (
    "schema_version: 1\n"
    "redmine:\n"
    "  default_project:\n"
    "    identifier: {identifier}\n"
    "    name: Example\n"
    "    url: https://redmine.giken.or.jp/projects/{identifier}\n"
    "    parent_label: parent\n"
    "  verification:\n"
    "    verified: {verified}\n"
    '    verification_date: "{date}"\n'
    '    verified_by: "{by}"\n'
    "outputs:\n"
    "  - kind: redmine_markdown\n"
    "    target: .mozyo-bridge/redmine-defaults.md\n"
)


def _write_defaults(
    repo: Path,
    *,
    identifier: str = "giken-3800-mozyo-bridge",
    verified: str = "true",
    date: str = "2026-07-10",
    by: str = "tester",
    name: str = "project-defaults.yaml",
) -> None:
    (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (repo / ".mozyo-bridge" / name).write_text(
        _DEFAULTS_YAML.format(identifier=identifier, verified=verified, date=date, by=by),
        encoding="utf-8",
    )


class ResolveDefaultProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_missing_defaults_file(self) -> None:
        resolution = resolve_default_project(self.repo)
        self.assertEqual(resolution.status, DEFAULT_PROJECT_MISSING)
        self.assertIsNone(resolution.identifier)
        self.assertFalse(resolution.is_verified)

    def test_verified_default_carries_identifier(self) -> None:
        _write_defaults(self.repo, identifier="giken-3800-mozyo-bridge")
        resolution = resolve_default_project(self.repo)
        self.assertEqual(resolution.status, DEFAULT_PROJECT_VERIFIED)
        self.assertTrue(resolution.is_verified)
        self.assertEqual(resolution.identifier, "giken-3800-mozyo-bridge")

    def test_unverified_default_withholds_identifier(self) -> None:
        # An incomplete verification record (verified: false) is a suggestion
        # only — the identifier is never surfaced as usable.
        _write_defaults(self.repo, verified="false")
        resolution = resolve_default_project(self.repo)
        self.assertEqual(resolution.status, DEFAULT_PROJECT_UNVERIFIED)
        self.assertIsNone(resolution.identifier)
        self.assertFalse(resolution.is_verified)

    def test_incomplete_verification_fields_are_unverified(self) -> None:
        # verified: true but a blank verified_by is the same as unverified for
        # resolution purposes (mirrors Verification.is_complete).
        _write_defaults(self.repo, by="")
        resolution = resolve_default_project(self.repo)
        self.assertEqual(resolution.status, DEFAULT_PROJECT_UNVERIFIED)

    def test_legacy_name_reads(self) -> None:
        _write_defaults(self.repo, name="workspace-defaults.yaml")
        resolution = resolve_default_project(self.repo)
        self.assertEqual(resolution.status, DEFAULT_PROJECT_VERIFIED)
        self.assertEqual(resolution.identifier, "giken-3800-mozyo-bridge")

    def test_both_names_conflict(self) -> None:
        _write_defaults(self.repo, name="project-defaults.yaml")
        _write_defaults(self.repo, name="workspace-defaults.yaml")
        resolution = resolve_default_project(self.repo)
        self.assertEqual(resolution.status, DEFAULT_PROJECT_CONFLICT)
        self.assertIsNone(resolution.identifier)


class ResolveHandoffProfileFieldsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_durable_anchor_autofilled(self) -> None:
        fields = resolve_handoff_profile_fields(
            "implementation_worker", ["lane=alpha"], _ANCHOR, self.repo
        )
        self.assertEqual(fields["durable_anchor"], _ANCHOR)
        self.assertEqual(fields["lane"], "alpha")

    def test_explicit_durable_anchor_wins(self) -> None:
        fields = resolve_handoff_profile_fields(
            "implementation_worker",
            ["durable_anchor=explicit"],
            _ANCHOR,
            self.repo,
        )
        self.assertEqual(fields["durable_anchor"], "explicit")

    def test_redmine_project_autoresolved_from_verified_default(self) -> None:
        _write_defaults(self.repo, identifier="giken-3800-mozyo-bridge")
        fields = resolve_handoff_profile_fields(
            "coordinator", ["project=alpha"], _ANCHOR, self.repo
        )
        self.assertEqual(fields["redmine_project"], "giken-3800-mozyo-bridge")

    def test_explicit_redmine_project_wins_over_default(self) -> None:
        _write_defaults(self.repo, identifier="giken-3800-mozyo-bridge")
        fields = resolve_handoff_profile_fields(
            "coordinator",
            ["redmine_project=explicit-project"],
            _ANCHOR,
            self.repo,
        )
        self.assertEqual(fields["redmine_project"], "explicit-project")

    def test_explicit_redmine_project_skips_default_read_when_missing(self) -> None:
        # No defaults file at all: an explicit value must still succeed (the
        # verified-default gate is only for the auto-fill path).
        fields = resolve_handoff_profile_fields(
            "delegated_coordinator",
            ["redmine_project=explicit-project"],
            _ANCHOR,
            self.repo,
        )
        self.assertEqual(fields["redmine_project"], "explicit-project")

    def test_explicit_empty_redmine_project_fails_closed(self) -> None:
        # Redmine #13477 review j#74496 finding_1: an explicit empty value is not
        # a valid identifier, so it fails closed before send (not silently left
        # unresolved) even when a verified default exists.
        _write_defaults(self.repo, identifier="giken-3800-mozyo-bridge")
        with self.assertRaises(RoleProfileError) as ctx:
            resolve_handoff_profile_fields(
                "coordinator", ["redmine_project="], _ANCHOR, self.repo
            )
        self.assertIn("redmine_project", str(ctx.exception))

    def test_explicit_whitespace_redmine_project_fails_closed(self) -> None:
        # Whitespace-only would otherwise be substituted as if resolved by the
        # pure resolver; it must fail closed like a missing default.
        _write_defaults(self.repo, identifier="giken-3800-mozyo-bridge")
        with self.assertRaises(RoleProfileError):
            resolve_handoff_profile_fields(
                "coordinator", ["redmine_project=   "], _ANCHOR, self.repo
            )

    def test_conflict_default_fails_closed(self) -> None:
        # Both new + legacy names present is ambiguous -> fail closed for the
        # auto-resolve path (omitted explicit value).
        _write_defaults(self.repo, name="project-defaults.yaml")
        _write_defaults(self.repo, name="workspace-defaults.yaml")
        with self.assertRaises(RoleProfileError):
            resolve_handoff_profile_fields(
                "delegated_coordinator", None, _ANCHOR, self.repo
            )

    def test_role_without_redmine_project_placeholder_no_default_read(self) -> None:
        # implementation_worker has no redmine_project placeholder, so a missing
        # default must not fail the send.
        fields = resolve_handoff_profile_fields(
            "implementation_worker", ["lane=alpha"], _ANCHOR, self.repo
        )
        self.assertNotIn("redmine_project", fields)

    def test_missing_default_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError) as ctx:
            resolve_handoff_profile_fields("coordinator", None, _ANCHOR, self.repo)
        self.assertIn("redmine_project", str(ctx.exception))

    def test_unverified_default_fails_closed(self) -> None:
        _write_defaults(self.repo, verified="false")
        with self.assertRaises(RoleProfileError) as ctx:
            resolve_handoff_profile_fields("coordinator", None, _ANCHOR, self.repo)
        self.assertIn("not verified", str(ctx.exception))

    def test_malformed_profile_field_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError):
            resolve_handoff_profile_fields(
                "coordinator", ["noequals"], _ANCHOR, self.repo
            )

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(RoleProfileError):
            resolve_handoff_profile_fields("bogus_role", None, _ANCHOR, self.repo)


if __name__ == "__main__":
    unittest.main()
