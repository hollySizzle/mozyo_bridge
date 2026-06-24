"""Session naming derivation tests (Redmine #12150, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of the collision-safe tmux session-name derivation
test family out of the monolithic test spine, per #12150 and
vibes/docs/logics/refactor-split-strategy.md (Priority 1 low-risk families).
No test logic changed."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.application.commands import cmd_mozyo


class SessionNamingTest(unittest.TestCase):
    """Pin Redmine #10796: collision-safe ASCII tmux session name derivation.

    A non-ASCII workspace basename (e.g. `2026PBL_ローカル`) must never collapse
    to a low-information `____`-style name, and two distinct repos that share a
    basename must get distinct session names so the `--target-repo` handoff
    gate keeps a recoverable repo identity. The workspace-defaults Redmine
    identifier is the preferred, stable source.
    """

    from mozyo_bridge.domain.session_naming import (  # noqa: E402 (test-local import)
        SOURCE_REPO_FALLBACK,
        SOURCE_WORKSPACE_DEFAULTS,
    )

    def _write_workspace_defaults(self, repo: Path, *, identifier: str) -> None:
        (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n"
            "    name: Example\n"
            "    url: https://redmine.example.test/projects/example\n"
            "    parent_label: parent\n"
            "  verification:\n"
            "    verified: false\n"
            '    verification_date: ""\n'
            "    verified_by: \"\"\n"
            "outputs:\n"
            "  - kind: redmine_markdown\n"
            "    target: .mozyo-bridge/redmine-defaults.md\n",
            encoding="utf-8",
        )

    def test_workspace_defaults_identifier_is_preferred(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            result = derive_session_name(repo)

            self.assertEqual("mozyo-giken-3800-mozyo-bridge", result.name)
            self.assertEqual(self.SOURCE_WORKSPACE_DEFAULTS, result.source)
            self.assertEqual("giken-3800-mozyo-bridge", result.identifier)

    def test_unverified_identifier_is_still_used(self) -> None:
        # Session naming is a display/grouping identity, not an issue-creation
        # decision, so it intentionally does NOT gate on the verification flag.
        # The fixture above is written with `verified: false`.
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="some-project")

            result = derive_session_name(repo)

            self.assertEqual("mozyo-some-project", result.name)
            self.assertEqual(self.SOURCE_WORKSPACE_DEFAULTS, result.source)

    def test_japanese_basename_is_not_collapsed_to_underscores(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "2026PBL_ローカル"
            repo.mkdir()

            result = derive_session_name(repo)

            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
            self.assertIsNone(result.identifier)
            # Must NOT be the `____`-style low-information name.
            self.assertNotIn("_", result.name)
            self.assertNotRegex(result.name, r"-{2,}")
            # The ASCII-recoverable part is preserved, plus a hash suffix.
            self.assertTrue(
                result.name.startswith("mozyo-2026pbl-"),
                msg=f"unexpected fallback name {result.name!r}",
            )

    def test_nfc_and_nfd_path_spellings_derive_the_same_name(self) -> None:
        # Redmine #11625: the same directory is spelled NFD by macOS readdir /
        # shell completion but NFC by documents, Redmine, and agents. Hashing
        # the raw bytes derived two session names for one workspace.
        import unicodedata as _ud

        from mozyo_bridge.domain.session_naming import derive_session_name

        nfd_spelling = "/ws/" + _ud.normalize("NFD", "動画ドライブ")
        nfc_spelling = "/ws/" + _ud.normalize("NFC", "動画ドライブ")
        self.assertNotEqual(nfd_spelling, nfc_spelling)

        nfd_result = derive_session_name(nfd_spelling)
        nfc_result = derive_session_name(nfc_spelling)

        self.assertEqual(nfd_result.name, nfc_result.name)
        self.assertEqual(self.SOURCE_REPO_FALLBACK, nfd_result.source)

    def test_repo_path_hash_is_pinned_to_the_nfd_form(self) -> None:
        # Compatibility pin: NFD is the macOS filesystem form, so session
        # names historically derived from real filesystem paths must keep
        # their value after the #11625 fix. The hash of any spelling must
        # equal the hash of the NFD bytes.
        import hashlib as _hashlib
        import unicodedata as _ud

        from mozyo_bridge.domain.session_naming import (
            REPO_HASH_LENGTH,
            derive_session_name,
        )

        nfc_spelling = Path("/ws/" + _ud.normalize("NFC", "動画ドライブ"))
        resolved_nfd = _ud.normalize("NFD", str(nfc_spelling.resolve()))
        expected_hash = _hashlib.sha256(
            resolved_nfd.encode("utf-8")
        ).hexdigest()[:REPO_HASH_LENGTH]

        result = derive_session_name(nfc_spelling)

        self.assertTrue(
            result.name.endswith(f"-{expected_hash}"),
            msg=f"{result.name!r} does not carry the NFD-form hash {expected_hash!r}",
        )

    def test_all_non_ascii_basename_yields_hash_only_name(self) -> None:
        from mozyo_bridge.domain.session_naming import (
            REPO_HASH_LENGTH,
            derive_session_name,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "動画制作"
            repo.mkdir()

            result = derive_session_name(repo)

            # No ASCII slug to keep, so the name is just `mozyo-<hash>` — still
            # non-empty, ASCII, and never a bare `____`.
            self.assertRegex(result.name, rf"^mozyo-[0-9a-f]{{{REPO_HASH_LENGTH}}}$")
            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)

    def test_same_basename_in_different_paths_is_collision_safe(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a" / "2026PBL_ローカル"
            second = Path(tmp) / "b" / "2026PBL_ローカル"
            first.mkdir(parents=True)
            second.mkdir(parents=True)

            name_a = derive_session_name(first).name
            name_b = derive_session_name(second).name

            self.assertNotEqual(name_a, name_b)
            # Both share the recoverable basename slug but differ by hash.
            self.assertTrue(name_a.startswith("mozyo-2026pbl-"))
            self.assertTrue(name_b.startswith("mozyo-2026pbl-"))

    def test_derivation_is_deterministic_for_same_repo(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "2026PBL_ローカル"
            repo.mkdir()

            self.assertEqual(
                derive_session_name(repo).name, derive_session_name(repo).name
            )

    def test_missing_workspace_defaults_falls_back(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "plain_repo"
            repo.mkdir()

            result = derive_session_name(repo)

            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
            self.assertTrue(result.name.startswith("mozyo-plain-repo-"))

    def test_absent_or_malformed_identifier_falls_back_without_raising(self) -> None:
        from mozyo_bridge.domain.session_naming import (
            derive_session_name,
            read_redmine_identifier,
        )

        bodies = {
            "not a mapping": "- just\n- a\n- list\n",
            "no redmine key": "schema_version: 1\nother: value\n",
            "identifier absent": (
                "redmine:\n  default_project:\n    name: X\n"
            ),
            "identifier non-string": (
                "redmine:\n  default_project:\n    identifier: 12345\n"
            ),
            "broken yaml": "redmine: [unterminated\n",
            "empty identifier": (
                "redmine:\n  default_project:\n    identifier: '   '\n"
            ),
        }
        for label, body in bodies.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp) / "repo_dir"
                    (repo / ".mozyo-bridge").mkdir(parents=True)
                    (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
                        body, encoding="utf-8"
                    )
                    self.assertIsNone(read_redmine_identifier(repo.resolve()))
                    result = derive_session_name(repo)
                    self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
                    self.assertTrue(result.name.startswith("mozyo-repo-dir-"))

    def test_non_ascii_only_identifier_falls_back(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo_dir"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="動画制作")

            result = derive_session_name(repo)

            # An identifier that slugs to empty cannot anchor identity, so we
            # fall back rather than emit a bare `mozyo-` prefix.
            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
            self.assertTrue(result.name.startswith("mozyo-repo-dir-"))

    def test_derived_name_never_contains_tmux_illegal_chars(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # `.` and `:` are tmux window/pane separators; whitespace is unsafe.
            self._write_workspace_defaults(
                repo, identifier="Foo.Bar:Baz Qux_2026"
            )

            name = derive_session_name(repo).name

            for illegal in (".", ":", " "):
                self.assertNotIn(illegal, name)
            self.assertEqual("mozyo-foo-bar-baz-qux-2026", name)

    def test_session_name_cli_parses(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["session", "name", "--repo", "/some/repo"])

        self.assertEqual("session", args.command)
        self.assertEqual("name", args.session_command)
        self.assertEqual("/some/repo", args.repo)
        self.assertFalse(args.as_json)

    def test_session_subcommand_requires_action(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["session"])

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_session_name_cli_prints_bare_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            code, out = self._run_cli(["session", "name", "--repo", str(repo)])

            self.assertEqual(0, code)
            self.assertEqual("mozyo-giken-3800-mozyo-bridge", out.strip())

    def test_session_name_cli_json_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            code, out = self._run_cli(
                ["session", "name", "--repo", str(repo), "--json"]
            )

            self.assertEqual(0, code)
            payload = json.loads(out)
            self.assertEqual("mozyo-giken-3800-mozyo-bridge", payload["name"])
            self.assertEqual(
                self.SOURCE_WORKSPACE_DEFAULTS, payload["source"]
            )
            self.assertEqual("giken-3800-mozyo-bridge", payload["identifier"])
            self.assertEqual(str(repo.resolve()), payload["repo_root"])

    # ------------------------------------------------------------------
    # Bare `mozyo` / status unification (Redmine #10796 follow-up #52324)
    # ------------------------------------------------------------------

    def _run_bare_mozyo_capture_session(self, repo: Path) -> str:
        """Run bare `mozyo --no-attach` with tmux mocked; return the session.

        Captures the session name handed to `ensure_repo_session_windows` so
        the test asserts the derivation without touching a real tmux server.
        """
        args = argparse.Namespace(
            repo=str(repo),
            session=None,
            cwd=None,
            config_path=None,
            ready_timeout=0,
            force=False,
            no_attach=True,
        )
        captured: dict[str, argparse.Namespace] = {}

        def fake_ensure(inner: argparse.Namespace) -> list[str]:
            captured["args"] = inner
            return []

        list_result = argparse.Namespace(returncode=0, stdout="", stderr="")
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
            patch(
                "mozyo_bridge.application.commands.ensure_repo_session_windows",
                side_effect=fake_ensure,
            ), \
            patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
            patch(
                "mozyo_bridge.application.commands.os.execvp",
                side_effect=AssertionError("must not attach"),
            ), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_mozyo(args))
        return captured["args"].session

    def test_bare_mozyo_uses_workspace_defaults_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "any-basename").resolve()
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            self.assertEqual(
                "mozyo-giken-3800-mozyo-bridge",
                self._run_bare_mozyo_capture_session(repo),
            )

    def test_bare_mozyo_japanese_basename_is_not_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "2026PBL_ローカル").resolve()
            repo.mkdir()

            session = self._run_bare_mozyo_capture_session(repo)

            self.assertNotIn("_", session)
            self.assertTrue(session.startswith("mozyo-2026pbl-"))

    def test_bare_mozyo_respects_explicit_session_override(self) -> None:
        # The explicit `--session` override must still win over the derived
        # name; the derivation only fills the default.
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "2026PBL_ローカル").resolve()
            repo.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session="explicit-name",
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=True,
            )
            captured: dict[str, argparse.Namespace] = {}

            def fake_ensure(inner: argparse.Namespace) -> list[str]:
                captured["args"] = inner
                return []

            list_result = argparse.Namespace(returncode=0, stdout="", stderr="")
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch(
                    "mozyo_bridge.application.commands.ensure_repo_session_windows",
                    side_effect=fake_ensure,
                ), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch(
                    "mozyo_bridge.application.commands.os.execvp",
                    side_effect=AssertionError("must not attach"),
                ), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_mozyo(args))

            self.assertEqual("explicit-name", captured["args"].session)

    def test_resolve_status_session_fallback_uses_derived_name(self) -> None:
        from mozyo_bridge.application.commands import resolve_status_session
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "2026PBL_ローカル").resolve()
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), session=None)

            # Not inside tmux (no current session) and no explicit --session:
            # the fallback must match what bare `mozyo` creates.
            with patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=None,
            ):
                resolved = resolve_status_session(args)

            self.assertEqual(derive_session_name(repo).name, resolved)

    def test_legacy_basename_session_notice_cases(self) -> None:
        from mozyo_bridge.application.commands import legacy_basename_session_notice

        repo = Path("/tmp/some/2026PBL_ローカル")
        derived = "mozyo-2026pbl-deadbeef"

        # Legacy session exists and belongs to this repo -> notice.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=True
        ), patch(
            "mozyo_bridge.application.commands.session_cwd_mismatch", return_value=[]
        ):
            notice = legacy_basename_session_notice(repo, derived)
        self.assertIsNotNone(notice)
        self.assertIn("2026PBL_ローカル", notice)
        self.assertIn(derived, notice)

        # No legacy session -> no notice.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=False
        ):
            self.assertIsNone(legacy_basename_session_notice(repo, derived))

        # Legacy-named session belongs to another repo (cwd mismatch) -> no notice.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=True
        ), patch(
            "mozyo_bridge.application.commands.session_cwd_mismatch",
            return_value=["/elsewhere"],
        ):
            self.assertIsNone(legacy_basename_session_notice(repo, derived))

        # Derived name equals the basename (ASCII repo) -> nothing to migrate.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=True
        ):
            self.assertIsNone(
                legacy_basename_session_notice(Path("/tmp/foo"), "foo")
            )

    # ------------------------------------------------------------------
    # VS Code `tmux-integrated.sessionName` writer (#52324 mechanization)
    # ------------------------------------------------------------------

    def test_merge_vscode_session_name_creates_and_preserves(self) -> None:
        from mozyo_bridge.domain.session_naming import merge_vscode_session_name

        # Empty / None -> fresh object with just the key.
        for empty in (None, "", "   \n"):
            created = json.loads(merge_vscode_session_name(empty, "mozyo-x"))
            self.assertEqual({"tmux-integrated.sessionName": "mozyo-x"}, created)

        # Existing keys are preserved; the session key is updated in place.
        existing = json.dumps(
            {"editor.tabSize": 2, "tmux-integrated.sessionName": "old"}
        )
        merged = json.loads(merge_vscode_session_name(existing, "mozyo-new"))
        self.assertEqual(2, merged["editor.tabSize"])
        self.assertEqual("mozyo-new", merged["tmux-integrated.sessionName"])

    def test_merge_vscode_session_name_refuses_jsonc_and_non_object(self) -> None:
        from mozyo_bridge.domain.session_naming import merge_vscode_session_name

        with self.assertRaises(ValueError):
            merge_vscode_session_name('{\n  // comment\n  "a": 1\n}', "mozyo-x")
        with self.assertRaises(ValueError):
            merge_vscode_session_name("[1, 2, 3]", "mozyo-x")

    def test_vscode_settings_cli_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            code, out = self._run_cli(
                ["session", "vscode-settings", "--repo", str(repo)]
            )

            self.assertEqual(0, code)
            self.assertIn("mozyo-giken-3800-mozyo-bridge", out)
            self.assertFalse((repo / ".vscode" / "settings.json").exists())

    def test_vscode_settings_cli_write_creates_and_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")
            settings = repo / ".vscode" / "settings.json"

            code, _ = self._run_cli(
                ["session", "vscode-settings", "--repo", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(
                "mozyo-giken-3800-mozyo-bridge",
                data["tmux-integrated.sessionName"],
            )

            # A second write preserving an unrelated key.
            settings.write_text(
                json.dumps(
                    {
                        "editor.tabSize": 4,
                        "tmux-integrated.sessionName": "stale",
                    }
                ),
                encoding="utf-8",
            )
            code, _ = self._run_cli(
                ["session", "vscode-settings", "--repo", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(4, data["editor.tabSize"])
            self.assertEqual(
                "mozyo-giken-3800-mozyo-bridge",
                data["tmux-integrated.sessionName"],
            )

    def test_vscode_settings_cli_refuses_to_clobber_jsonc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            settings = repo / ".vscode"
            settings.mkdir()
            jsonc = settings / "settings.json"
            original = '{\n  // a comment\n  "editor.tabSize": 2\n}\n'
            jsonc.write_text(original, encoding="utf-8")

            parser = build_parser()
            args = parser.parse_args(
                ["session", "vscode-settings", "--repo", str(repo), "--write"]
            )
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    args.func(args)

            # The JSONC file must be left byte-for-byte untouched.
            self.assertEqual(original, jsonc.read_text(encoding="utf-8"))
            self.assertIn("JSONC", stderr.getvalue())

    def test_session_vscode_settings_cli_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["session", "vscode-settings", "--repo", "/r", "--write"]
        )
        self.assertEqual("vscode-settings", args.session_command)
        self.assertTrue(args.write)


if __name__ == "__main__":
    unittest.main()
