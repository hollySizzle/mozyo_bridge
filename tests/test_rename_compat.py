"""Workspace anchor / project defaults rename compatibility (Redmine #11920 / #11921).

Pins the old/new filename compatibility contract from
`vibes/docs/logics/workspace-anchor-project-defaults-migration.md`:

- new name is primary, old name is a read-only fallback;
- new writes only ever create the new name (no dual-write);
- both names existing fails closed for mutating / explicit commands and is a
  red doctor diagnostic — never a silent merge;
- read-only hot paths prefer the new name and degrade quietly.

Everything runs against temp dirs — no tmux, no real ``~/.mozyo_bridge``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import workspace_registry as wr
from mozyo_bridge import workspace_defaults as wd
from mozyo_bridge.domain.session_naming import (
    SOURCE_WORKSPACE_DEFAULTS,
    derive_session_name,
    read_redmine_identifier,
)
from mozyo_bridge.redmine_context import read_redmine_project
from mozyo_bridge.shared.name_compat import resolve_compat_path


_DEFAULTS_YAML = (
    "schema_version: 1\n"
    "redmine:\n"
    "  default_project:\n"
    "    identifier: {identifier}\n"
    "    name: Example\n"
    "    url: https://redmine.giken.or.jp/projects/{identifier}\n"
    "    parent_label: parent\n"
    "  verification:\n"
    "    verified: true\n"
    '    verification_date: "2026-05-28"\n'
    "    verified_by: tester\n"
    "outputs:\n"
    "  - kind: redmine_markdown\n"
    "    target: .mozyo-bridge/redmine-defaults.md\n"
)


def _capture_stderr(func):
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        result = func()
    return result, stream.getvalue()


class CompatResolutionTest(unittest.TestCase):
    """The shared resolver: new wins, legacy is fallback, both is flagged."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.new = Path("a/new.txt")
        self.old = Path("a/old.txt")
        (self.repo / "a").mkdir()

    def _resolve(self):
        return resolve_compat_path(self.repo, self.new, self.old)

    def test_neither(self) -> None:
        r = self._resolve()
        self.assertTrue(r.neither_exists)
        self.assertIsNone(r.read_path)
        self.assertFalse(r.both_exist)
        self.assertFalse(r.using_legacy)

    def test_new_only(self) -> None:
        (self.repo / self.new).write_text("x", encoding="utf-8")
        r = self._resolve()
        self.assertEqual(r.read_path, self.repo / self.new)
        self.assertFalse(r.using_legacy)
        self.assertFalse(r.both_exist)

    def test_legacy_only(self) -> None:
        (self.repo / self.old).write_text("x", encoding="utf-8")
        r = self._resolve()
        self.assertEqual(r.read_path, self.repo / self.old)
        self.assertTrue(r.using_legacy)
        self.assertFalse(r.both_exist)

    def test_both_prefers_new_for_read(self) -> None:
        (self.repo / self.new).write_text("x", encoding="utf-8")
        (self.repo / self.old).write_text("y", encoding="utf-8")
        r = self._resolve()
        self.assertTrue(r.both_exist)
        self.assertFalse(r.using_legacy)
        self.assertEqual(r.read_path, self.repo / self.new)


class AnchorRenameCompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.repo = base / "ws"
        (self.repo / ".mozyo-bridge").mkdir(parents=True)

    def _write_anchor(self, relative: Path, *, workspace_id: str = "a" * 32) -> None:
        (self.repo / relative).write_text(
            json.dumps(
                {
                    "schema_version": wr.ANCHOR_SCHEMA_VERSION,
                    "workspace_id": workspace_id,
                    "canonical_session": "mozyo-demo",
                    "project_name": "demo",
                    "created_at": "2026-06-14T00:00:00+00:00",
                    "updated_at": "2026-06-14T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    def test_read_new_only(self) -> None:
        self._write_anchor(wr.ANCHOR_RELATIVE, workspace_id="n" * 32)
        anchor = wr.read_anchor(self.repo)
        self.assertEqual(anchor["workspace_id"], "n" * 32)

    def test_read_legacy_fallback(self) -> None:
        self._write_anchor(wr.ANCHOR_LEGACY_RELATIVE, workspace_id="l" * 32)
        anchor = wr.read_anchor(self.repo)
        self.assertEqual(anchor["workspace_id"], "l" * 32)

    def test_read_both_prefers_new(self) -> None:
        self._write_anchor(wr.ANCHOR_RELATIVE, workspace_id="n" * 32)
        self._write_anchor(wr.ANCHOR_LEGACY_RELATIVE, workspace_id="l" * 32)
        anchor = wr.read_anchor(self.repo)
        self.assertEqual(anchor["workspace_id"], "n" * 32)

    def test_register_writes_new_name_only(self) -> None:
        result = wr.register_workspace(self.repo, home=self.home)
        self.assertTrue((self.repo / wr.ANCHOR_RELATIVE).exists())
        self.assertFalse((self.repo / wr.ANCHOR_LEGACY_RELATIVE).exists())
        self.assertEqual(
            result.anchor_path.resolve(),
            (self.repo / wr.ANCHOR_RELATIVE).resolve(),
        )

    def test_register_both_exist_fails_closed(self) -> None:
        self._write_anchor(wr.ANCHOR_RELATIVE, workspace_id="n" * 32)
        self._write_anchor(wr.ANCHOR_LEGACY_RELATIVE, workspace_id="l" * 32)
        with self.assertRaises(SystemExit):
            _, err = _capture_stderr(
                lambda: wr.register_workspace(self.repo, home=self.home)
            )

    def test_register_legacy_only_migrates_identity_to_new(self) -> None:
        self._write_anchor(wr.ANCHOR_LEGACY_RELATIVE, workspace_id="l" * 32)
        result = wr.register_workspace(self.repo, home=self.home)
        # Identity recovered from the legacy anchor, written to the new name.
        self.assertEqual(result.record.workspace_id, "l" * 32)
        self.assertTrue((self.repo / wr.ANCHOR_RELATIVE).exists())
        new_anchor = json.loads(
            (self.repo / wr.ANCHOR_RELATIVE).read_text(encoding="utf-8")
        )
        self.assertEqual(new_anchor["workspace_id"], "l" * 32)
        self.assertIn("legacy anchor", " ".join(result.notes))

    def test_doctor_reports_both_exist_red(self) -> None:
        import argparse

        from mozyo_bridge.application.doctor import doctor_workspace_registry_section

        self._write_anchor(wr.ANCHOR_RELATIVE, workspace_id="n" * 32)
        self._write_anchor(wr.ANCHOR_LEGACY_RELATIVE, workspace_id="l" * 32)
        args = argparse.Namespace(repo=str(self.repo), home=str(self.home))
        section = doctor_workspace_registry_section(args)
        self.assertEqual(section["anchor"]["name_state"], "both")
        self.assertEqual(section["status"], "drifted")

    def test_doctor_reports_legacy_only(self) -> None:
        import argparse

        from mozyo_bridge.application.doctor import doctor_workspace_registry_section

        self._write_anchor(wr.ANCHOR_LEGACY_RELATIVE, workspace_id="l" * 32)
        args = argparse.Namespace(repo=str(self.repo), home=str(self.home))
        section = doctor_workspace_registry_section(args)
        self.assertEqual(section["anchor"]["name_state"], "legacy")
        self.assertTrue(
            any("legacy name" in action for action in section["next_action"])
        )


class DefaultsRenameCompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / ".mozyo-bridge").mkdir(parents=True)

    def _write(self, relative: Path, *, identifier: str = "demo-project") -> None:
        (self.repo / relative).write_text(
            _DEFAULTS_YAML.format(identifier=identifier), encoding="utf-8"
        )

    # --- load_repo_defaults (command-level contract) -----------------------

    def test_load_new_only(self) -> None:
        self._write(wd.PROJECT_DEFAULTS_INPUT_RELATIVE, identifier="new-proj")
        defaults, err = _capture_stderr(lambda: wd.load_repo_defaults(self.repo))
        self.assertEqual(defaults.default_project.identifier, "new-proj")
        self.assertEqual("", err)

    def test_load_legacy_only_warns(self) -> None:
        self._write(wd.WORKSPACE_DEFAULTS_LEGACY_RELATIVE, identifier="old-proj")
        defaults, err = _capture_stderr(lambda: wd.load_repo_defaults(self.repo))
        self.assertEqual(defaults.default_project.identifier, "old-proj")
        self.assertIn("legacy name", err)

    def test_load_both_exist_fails_closed(self) -> None:
        self._write(wd.PROJECT_DEFAULTS_INPUT_RELATIVE, identifier="new-proj")
        self._write(wd.WORKSPACE_DEFAULTS_LEGACY_RELATIVE, identifier="old-proj")
        with self.assertRaises(SystemExit):
            _capture_stderr(lambda: wd.load_repo_defaults(self.repo))

    # --- generated markdown references the actual source name --------------

    def test_render_references_new_source_name(self) -> None:
        self._write(wd.PROJECT_DEFAULTS_INPUT_RELATIVE)
        results = wd.collect_render_results(self.repo)
        body = results[0].rendered
        self.assertIn(".mozyo-bridge/project-defaults.yaml", body)
        self.assertNotIn(".mozyo-bridge/workspace-defaults.yaml", body)

    def test_render_references_legacy_source_name(self) -> None:
        self._write(wd.WORKSPACE_DEFAULTS_LEGACY_RELATIVE)
        results = wd.collect_render_results(self.repo)
        body = results[0].rendered
        self.assertIn(".mozyo-bridge/workspace-defaults.yaml", body)

    # --- read-only hot readers prefer new, fall back to legacy ------------

    def test_session_naming_reads_new(self) -> None:
        self._write(wd.PROJECT_DEFAULTS_INPUT_RELATIVE, identifier="new-proj")
        self.assertEqual(read_redmine_identifier(self.repo), "new-proj")
        derived = derive_session_name(self.repo)
        self.assertEqual(derived.source, SOURCE_WORKSPACE_DEFAULTS)
        self.assertEqual(derived.name, "mozyo-new-proj")

    def test_session_naming_reads_legacy_fallback(self) -> None:
        self._write(wd.WORKSPACE_DEFAULTS_LEGACY_RELATIVE, identifier="old-proj")
        self.assertEqual(read_redmine_identifier(self.repo), "old-proj")

    def test_session_naming_prefers_new_when_both(self) -> None:
        self._write(wd.PROJECT_DEFAULTS_INPUT_RELATIVE, identifier="new-proj")
        self._write(wd.WORKSPACE_DEFAULTS_LEGACY_RELATIVE, identifier="old-proj")
        self.assertEqual(read_redmine_identifier(self.repo), "new-proj")

    def test_redmine_context_reads_new_then_legacy(self) -> None:
        self._write(wd.WORKSPACE_DEFAULTS_LEGACY_RELATIVE, identifier="old-proj")
        identifier, base = read_redmine_project(self.repo)
        self.assertEqual(identifier, "old-proj")
        self.assertEqual(base, "https://redmine.giken.or.jp")


if __name__ == "__main__":
    unittest.main()
