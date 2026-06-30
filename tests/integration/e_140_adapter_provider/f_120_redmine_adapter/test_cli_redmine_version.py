"""CLI integration for the ``redmine-version`` family (Redmine #12651).

Covers: registry/parser presence of the new family, and the two handlers driven
through the public ``main()`` against temp JSON snapshots — open-leaf
enumeration output and the fail-closed preflight exit codes (blocked -> non-zero,
allowed -> zero), proving the advisory surface executes nothing destructive.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import cli_modules
from mozyo_bridge.application.cli import build_parser, main


def _top_level_subcommands(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


class FamilyRegistrationTest(unittest.TestCase):
    def test_family_registered_with_a_registrar(self) -> None:
        names = cli_modules.BUILTIN_CLI_MODULE_REGISTRY.names()
        self.assertIn("redmine-version", names)
        self.assertIn("redmine-version", cli_modules._REGISTRARS)

    def test_family_is_not_mandatory(self) -> None:
        # Advisory / read-only: carries no core or authority flag, so config may
        # disable it without touching send/routing/approval families.
        mandatory = set(cli_modules.BUILTIN_CLI_MODULE_REGISTRY.mandatory_names())
        self.assertNotIn("redmine-version", mandatory)

    def test_subcommand_present_in_default_parser(self) -> None:
        self.assertIn("redmine-version", _top_level_subcommands(build_parser()))


class _SnapshotCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.issues_path = base / "issues.json"
        self.versions_path = base / "versions.json"
        self.issues_path.write_text(
            json.dumps(
                {
                    "issues": [
                        {"id": 900, "tracker": {"name": "UserStory"}, "status": {"name": "x", "is_closed": False}, "parent": {"id": 800}},
                        {"id": 901, "tracker": {"name": "Task"}, "status": {"name": "x", "is_closed": False}, "parent": {"id": 900}},
                        {"id": 902, "tracker": {"name": "Bug"}, "status": {"name": "x", "is_closed": True}, "parent": {"id": 900}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.versions_path.write_text(
            json.dumps(
                {
                    "versions": [
                        {"id": "248", "name": "v0.10.14 ...", "status": "open", "issues_count": 11, "open_issues_count": 4, "closed_issues_count": 7},
                        {"id": "281", "name": "cockpit UX follow-up", "status": "open", "issues_count": 0, "open_issues_count": 0, "closed_issues_count": 0},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()


class ListOpenLeafCliTest(_SnapshotCase):
    def test_text_output_lists_only_the_open_task_leaf(self) -> None:
        code, out = self._run(
            [
                "redmine-version",
                "list-open-leaf",
                "--version-id",
                "248",
                "--issues-json",
                str(self.issues_path),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("#901", out)
        self.assertIn("Task=1", out)
        self.assertNotIn("#902", out)  # closed bug excluded

    def test_json_output(self) -> None:
        code, out = self._run(
            [
                "redmine-version",
                "list-open-leaf",
                "--version-id",
                "248",
                "--issues-json",
                str(self.issues_path),
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["open_leaf_count"], 1)


class PreflightCliTest(_SnapshotCase):
    def test_non_empty_delete_blocks_nonzero(self) -> None:
        code, out = self._run(
            [
                "redmine-version",
                "preflight",
                "--version-id",
                "248",
                "--op",
                "delete",
                "--versions-json",
                str(self.versions_path),
                "--confirm",
                "delete:248",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("version_not_empty", out)

    def test_empty_delete_with_confirm_allows_zero(self) -> None:
        code, out = self._run(
            [
                "redmine-version",
                "preflight",
                "--version-id",
                "281",
                "--op",
                "delete",
                "--versions-json",
                str(self.versions_path),
                "--confirm",
                "delete:281",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("DELETE /versions/281.json", out)

    def test_missing_confirm_blocks_and_shows_token(self) -> None:
        code, out = self._run(
            [
                "redmine-version",
                "preflight",
                "--version-id",
                "281",
                "--op",
                "close",
                "--versions-json",
                str(self.versions_path),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("close:281", out)

    def test_inline_delete_without_counts_fails_closed(self) -> None:
        # Regression (j#69311): no --versions-json and no inline counts must not
        # let an irreversible delete preflight pass on defaulted-zero counts.
        code, out = self._run(
            [
                "redmine-version",
                "preflight",
                "--version-id",
                "999",
                "--op",
                "delete",
                "--confirm",
                "delete:999",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("counts_required", out)
        self.assertNotIn("DELETE /versions/999.json", out)

    def test_inline_delete_with_all_counts_zero_allows(self) -> None:
        code, out = self._run(
            [
                "redmine-version",
                "preflight",
                "--version-id",
                "999",
                "--op",
                "delete",
                "--confirm",
                "delete:999",
                "--issues-count",
                "0",
                "--open-issues-count",
                "0",
                "--closed-issues-count",
                "0",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("DELETE /versions/999.json", out)

    def test_unknown_version_in_snapshot_fails_closed(self) -> None:
        code, _ = self._run(
            [
                "redmine-version",
                "preflight",
                "--version-id",
                "999",
                "--op",
                "delete",
                "--versions-json",
                str(self.versions_path),
                "--confirm",
                "delete:999",
            ]
        )
        self.assertEqual(code, 2)  # fail-closed snapshot resolution


if __name__ == "__main__":
    unittest.main()
