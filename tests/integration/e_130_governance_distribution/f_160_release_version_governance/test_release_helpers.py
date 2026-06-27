"""Release helper command-family tests (Redmine #12139, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of the release helper parser / check / workflow /
bump / publish / drift test classes out of the monolithic test spine, per
the #12138 first-wave split and vibes/docs/logics/refactor-split-strategy.md.
No test logic changed."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import __version__
from mozyo_bridge.application.cli import build_parser

class ReleaseHelperParserTest(unittest.TestCase):
    """The contract-admitted release helper subcommands must round-trip
    through ``build_parser``. Argparse will raise SystemExit if a required
    flag is missing or a subparser was wired wrong, so this is a cheap
    structural check that ``release check`` / ``release workflow`` exist as
    documented in `release-helper-contract.md`.
    """

    def parse(self, *argv: str) -> argparse.Namespace:
        return build_parser().parse_args(list(argv))

    def test_release_check_tree(self) -> None:
        args = self.parse("release", "check", "tree")
        from mozyo_bridge.application.release import cmd_release_check_tree

        self.assertIs(args.func, cmd_release_check_tree)

    def test_release_check_scaffold(self) -> None:
        args = self.parse("release", "check", "scaffold")
        from mozyo_bridge.application.release import cmd_release_check_scaffold

        self.assertIs(args.func, cmd_release_check_scaffold)

    def test_release_check_artifact(self) -> None:
        args = self.parse("release", "check", "artifact")
        from mozyo_bridge.application.release import cmd_release_check_artifact

        self.assertIs(args.func, cmd_release_check_artifact)

    def test_release_check_drift(self) -> None:
        args = self.parse("release", "check", "drift")
        from mozyo_bridge.application.release import cmd_release_check_drift

        self.assertIs(args.func, cmd_release_check_drift)

    def test_release_check_workflow_requires_run_id(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "check", "workflow")
        args = self.parse("release", "check", "workflow", "--run-id", "42")
        from mozyo_bridge.application.release import cmd_release_check_workflow

        self.assertIs(args.func, cmd_release_check_workflow)
        self.assertEqual("42", args.run_id)

    def test_release_workflow_runs_requires_workflow(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "runs")
        args = self.parse("release", "workflow", "runs", "--workflow", "testpypi.yml")
        from mozyo_bridge.application.release import cmd_release_workflow_runs

        self.assertIs(args.func, cmd_release_workflow_runs)
        self.assertEqual("testpypi.yml", args.workflow)
        self.assertEqual(10, args.limit)

    def test_release_workflow_wait_requires_run_id_and_timeout(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "wait", "--run-id", "42")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "wait", "--timeout", "10")
        args = self.parse(
            "release",
            "workflow",
            "wait",
            "--run-id",
            "42",
            "--timeout",
            "30",
        )
        from mozyo_bridge.application.release import cmd_release_workflow_wait

        self.assertIs(args.func, cmd_release_workflow_wait)
        self.assertEqual("42", args.run_id)
        self.assertEqual(30.0, args.timeout)

    def test_release_check_subparser_requires_subcommand(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "check")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow")


class ReleaseCheckTreeTest(unittest.TestCase):
    """`release check tree` runs three git probes inside a real git repo and
    is strict-fail on the git grep blocker pattern. The tests build a tiny
    git checkout with `subprocess`, then verify both clean and blocker exit
    codes against real git behavior — no subprocess mocking, so the regex
    and pathspec wiring stay honest.
    """

    def _init_repo(self, root: Path) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "--allow-empty", "-m", "init", "-q"],
            check=True,
            env=env,
        )

    def _commit_file(self, root: Path, rel: str, body: str) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", rel], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", f"add {rel}", "-q"],
            check=True,
            env=env,
        )

    def test_clean_tree_returns_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "README.md", "Hello world\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())

    def test_personal_path_in_tracked_file_is_blocker(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            personal_path = "/Users" + "/example/project"
            self._commit_file(root, "AGENTS.md", f"see {personal_path} for context\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn(personal_path, out.getvalue())
            self.assertIn("result: blocker", out.getvalue())

    def test_secret_value_shape_in_tracked_file_is_blocker(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            fake_secret = "REDMINE" + "_API_KEY=" + "abc123"
            self._commit_file(root, "AGENTS.md", f"{fake_secret}\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn(fake_secret, out.getvalue())
            self.assertIn("result: blocker", out.getvalue())

    def test_secret_guidance_words_do_not_block_tree_check(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(
                root,
                "README.md",
                "Do not store credentials, tokens, secrets, or passwords.\n",
            )
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())

    def test_pathspec_excludes_skip_generated_trees(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            # Files inside excluded pathspecs (build/, dist/, tmp/) must not
            # trigger the blocker even if they contain personal paths, so
            # the helper does not flag artifacts that will be rebuilt or
            # excluded from publication anyway.
            personal_path = "/Users" + "/example/leak"
            self._commit_file(root, "build/log.txt", f"{personal_path}\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())

    def test_credential_identifier_code_does_not_block_tree_check(self) -> None:
        # Redmine #12175: lines that merely name a credential identifier are not
        # leaked values. Env reads, type annotations, keyword/identifier
        # defaults, constant references, and explicit non-secret sentinels must
        # all pass the tree check cleanly.
        from mozyo_bridge.application import release as release_mod

        false_positives = "\n".join(
            (
                "api_key=os.environ.get(API_KEY_ENV) or None",
                "def __init__(self, *, api_key: str | None, base_url=None):",
                "self._api_key = api_key",
                'API_KEY = "test-key-not-a-real-credential"',
                "cache = RedmineContextCache(api_key=None, base_url=TRUSTED)",
                "cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "service.py", false_positives + "\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())

    def test_real_secret_among_identifier_code_still_blocks(self) -> None:
        # Mixing safe identifier lines with one real literal credential must
        # still block, and only the real line is reported as a hit.
        from mozyo_bridge.application import release as release_mod

        real_secret = "REDMINE" + "_API_KEY=" + "abc123"
        body = "\n".join(
            (
                "api_key=os.environ.get(API_KEY_ENV) or None",
                "cache = RedmineContextCache(api_key=None, base_url=TRUSTED)",
                real_secret,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "config.env", body + "\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            output = out.getvalue()
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn("result: blocker", output)
            self.assertIn(real_secret, output)
            # The safe identifier lines are filtered out and not reported.
            self.assertNotIn("os.environ.get(API_KEY_ENV)", output)

    def test_token_shaped_secret_with_punctuation_still_blocks(self) -> None:
        # Redmine #12175 j#60466: a real credential literal carrying token
        # punctuation (slash/base64, dotted) must still block the tree check.
        from mozyo_bridge.application import release as release_mod

        slash_secret = "REDMINE" + "_API_KEY=" + "ab" + "c+def/123="
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "config.env", slash_secret + "\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            output = out.getvalue()
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn("result: blocker", output)
            self.assertIn(slash_secret, output)


class SecretValueClassifierTest(unittest.TestCase):
    """Redmine #12175: pin the second-stage credential-value classifier that
    separates real leaked literals from code that merely names a credential.
    Real-secret-shaped tokens are assembled by concatenation so this test file
    does not itself carry a contiguous secret-shaped literal.
    """

    def test_rejects_code_identifier_and_sentinel_values(self) -> None:
        from mozyo_bridge.application import release as release_mod

        reject_values = (
            "os.environ.get(API_KEY_ENV)",  # env read / call expression
            "os.environ.get(API_KEY_ENV) or None",
            "None",  # keyword default
            "str",  # type annotation
            "str | None",
            "API_KEY",  # uppercase constant reference
            "TRUSTED",
            "os.environ",  # dotted attribute reference
            "config.API_KEY",  # dotted constant reference
            "self.api_key",  # dotted instance-attr reference
            '"test-key-not-a-real-credential"',  # explicit non-secret sentinel
            '"<your-api-key>"',  # placeholder
            "",  # empty
        )
        for value in reject_values:
            self.assertFalse(
                release_mod._secret_value_is_real(value),
                msg=f"expected non-secret: {value!r}",
            )

    def test_accepts_opaque_literal_values(self) -> None:
        from mozyo_bridge.application import release as release_mod

        token = "ab" + "c123"
        accept_values = (
            token,  # bare env-style right-hand side of a *_API_KEY assignment
            "'" + token + "'",  # quoted literal
            '"' + token + '"',
        )
        for value in accept_values:
            self.assertTrue(
                release_mod._secret_value_is_real(value),
                msg=f"expected real secret: {value!r}",
            )

    def test_accepts_token_shaped_literals_with_punctuation(self) -> None:
        # Redmine #12175 j#60466: real credential tokens routinely contain
        # `.`, `/`, `+`, and padding `=`. These must stay classified as real
        # secrets — rejecting on token punctuation suppressed actual leaks.
        from mozyo_bridge.application import release as release_mod

        accept_values = (
            "ab" + "c/123",  # slash / base64-ish
            "ab" + "c+def/123=",  # base64 with padding
            "ab" + "c.def.123",  # dotted, digit segment -> token not a ref
            "sk." + "live." + "ab" + "c123",  # provider-style dotted key
            "eyJ" + "hbGci.eyJ" + "zdWIi.sig123",  # JWT-like header.payload.sig
        )
        for value in accept_values:
            self.assertTrue(
                release_mod._secret_value_is_real(value),
                msg=f"expected real token secret: {value!r}",
            )

    def test_assignment_classifier_pins_request_cases(self) -> None:
        from mozyo_bridge.application import release as release_mod

        safe_lines = (
            "api_key=os.environ.get(API_KEY_ENV) or None",
            "    def __init__(self, *, api_key: str | None, base_url=None):",
            "self._api_key = api_key",
            'API_KEY = "test-key-not-a-real-credential"',
            "cache = RedmineContextCache(api_key=None, base_url=TRUSTED)",
            "cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)",
            "Do not store credentials, tokens, secrets, or passwords.",
        )
        for line in safe_lines:
            self.assertFalse(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected safe line: {line!r}",
            )

        # Split key/separator so this test source never carries a contiguous
        # matchable `key[:=]value` token that the repo's own tree scan would
        # flag; the runtime strings below still reconstruct real assignments.
        token = "ab" + "c123"
        unsafe_lines = (
            "REDMINE" + "_API_KEY=" + token,
            "api_key" + ": " + token,
            "client" + "_secret = '" + token + "'",
        )
        for line in unsafe_lines:
            self.assertTrue(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected unsafe line: {line!r}",
            )

    def test_12693_field_name_false_positives_are_safe(self) -> None:
        # Redmine #12693: the concrete v0.9.1 release-gate false positives —
        # a same-name keyword pass-through, a None sentinel inside a string,
        # and a snake_case identifier assignment — name a credential field but
        # carry no literal value, so they must not block the tree scan.
        from mozyo_bridge.application import release as release_mod

        safe_field_name_lines = (
            "cache = RedmineContextCache(api_key=api_key, base_url=base_url)",
            'self.assertIn("api_key=None", repr(creds))',
            "api_key = env_key if env_key is not None else file_key",
            "creds = Creds(client_secret=client_secret, host=host)",
        )
        for line in safe_field_name_lines:
            self.assertFalse(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected safe field-name line: {line!r}",
            )

        # Digit-free identifier values and a string-embedded None sentinel are
        # references, not literals; a digit-bearing token stays a real secret so
        # detection is not weakened.
        for value in ("env_key", "file_key", "api_key", 'None"'):
            self.assertFalse(
                release_mod._secret_value_is_real(value),
                msg=f"expected non-secret value: {value!r}",
            )
        self.assertTrue(release_mod._secret_value_is_real("ab" + "c123"))

    def test_real_secret_grep_line_filter_keeps_only_real_hits(self) -> None:
        from mozyo_bridge.application import release as release_mod

        token = "ab" + "c123"
        grep_stdout = "\n".join(
            (
                "service.py:10:api_key=os.environ.get(API_KEY_ENV) or None",
                "service.py:11:    api_key: str | None",
                "config.env:1:REDMINE" + "_API_KEY=" + token,
            )
        )
        kept = release_mod._real_secret_grep_lines(grep_stdout)
        self.assertEqual(1, len(kept))
        self.assertIn(token, kept[0])
        self.assertTrue(kept[0].startswith("config.env:1:"))


class ReleaseCheckScaffoldTest(unittest.TestCase):
    def test_scaffold_check_uses_isolated_home_and_targets(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = release_mod.cmd_release_check_scaffold(argparse.Namespace())
        # Fresh scaffold smoke runs against an isolated home / target every
        # invocation. On a healthy package it must report clean for all
        # presets and exit zero.
        self.assertEqual(release_mod.EXIT_CLEAN, rc, msg=out.getvalue())
        text = out.getvalue()
        from mozyo_bridge.scaffold.rules import PRESETS

        for preset in PRESETS:
            self.assertIn(f"scaffold status: clean ({preset})", text)


class ReleaseCheckArtifactTest(unittest.TestCase):
    """The `release check` family is contractually read-only: invocations
    must not mutate the repo worktree (including the repo's ``dist/``
    directory). This test locks in that invariant by setting up a sentinel
    file in a fake repo's dist/, mocking ``sys.executable -m build``, and asserting
    (a) the sentinel survives, (b) ``--outdir`` is passed to build, and
    (c) the outdir lives outside the repo root.
    """

    def test_artifact_secret_pattern_matches_values_not_guidance_words(self) -> None:
        from mozyo_bridge.application import release as release_mod

        pattern = re.compile(release_mod._artifact_grep_pattern())
        fake_secret = "REDMINE" + "_API_KEY=" + "abc123"
        self.assertIsNone(pattern.search("Do not store tokens or secrets."))
        self.assertIsNotNone(pattern.search(fake_secret))

    def test_artifact_tree_scan_filters_identifier_false_positives(self) -> None:
        # Redmine #12175: the extracted-artifact scan applies the same
        # credential classifier as `release check tree`, so packaged source
        # that names a credential identifier is not flagged, while a personal
        # path or a real literal secret still is.
        from mozyo_bridge.application import release as release_mod

        real_secret = "REDMINE" + "_API_KEY=" + "abc123"
        personal_path = "/Users" + "/example/project"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "client.py").write_text(
                "\n".join(
                    (
                        "api_key=os.environ.get(API_KEY_ENV) or None",
                        "def __init__(self, *, api_key: str | None):",
                        'API_KEY = "test-key-not-a-real-credential"',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "pkg" / "leak.txt").write_text(
                f"home: {personal_path}\n{real_secret}\n", encoding="utf-8"
            )
            personal_pattern = re.compile(
                "|".join(release_mod._PERSONAL_PATH_PATTERNS)
            )
            hits = release_mod._grep_artifact_tree(root, personal_pattern)
            hit_lines = [line for _path, _lineno, line in hits]
            self.assertTrue(any(real_secret in line for line in hit_lines))
            self.assertTrue(any(personal_path in line for line in hit_lines))
            self.assertFalse(
                any("os.environ.get(API_KEY_ENV)" in line for line in hit_lines)
            )
            self.assertFalse(
                any("test-key-not-a-real-credential" in line for line in hit_lines)
            )

    def test_does_not_mutate_repo_dist_directory(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            (repo / "dist").mkdir()
            sentinel = repo / "dist" / "preexisting.whl"
            sentinel.write_bytes(b"preexisting")

            recorded: list[dict] = []

            def fake_run(argv, cwd=None, check=False, env=None):
                recorded.append(
                    {"argv": list(argv), "cwd": str(cwd) if cwd else None}
                )
                # Pretend build succeeded but wrote nothing to the outdir.
                # The helper's no-mutation invariant is what we're testing;
                # producing no artifacts just routes us through the
                # `no artifacts` blocker path, which is fine for this test.
                outdir = argv[argv.index("--outdir") + 1]
                Path(outdir).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertTrue(
                sentinel.exists(),
                "release check artifact mutated the repo's dist/ directory",
            )
            build_calls = [c for c in recorded if "build" in c["argv"]]
            self.assertEqual(1, len(build_calls), msg=recorded)
            argv = build_calls[0]["argv"]
            self.assertEqual(sys.executable, argv[0])
            self.assertIn("--outdir", argv)
            outdir = Path(argv[argv.index("--outdir") + 1]).resolve()
            try:
                outdir.relative_to(repo)
                inside_repo = True
            except ValueError:
                inside_repo = False
            self.assertFalse(
                inside_repo,
                f"--outdir {outdir} must not live inside repo {repo}",
            )
            # rc is blocker because the mocked build produced no artifacts;
            # the load-bearing assertions are the sentinel + outdir checks
            # above.
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)


class ReleaseCheckWorkflowTest(unittest.TestCase):
    def test_success_exits_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        payload = {
            "status": "completed",
            "conclusion": "success",
            "workflowName": "Test",
            "headSha": "abc123",
            "url": "https://example/run/42",
        }
        with patch.object(release_mod, "_gh_run_view", return_value=payload):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_workflow(
                    argparse.Namespace(run_id="42")
                )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("status: completed", out.getvalue())
        self.assertIn("conclusion: success", out.getvalue())

    def test_failure_exits_non_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        payload = {
            "status": "completed",
            "conclusion": "failure",
            "workflowName": "Test",
            "headSha": "abc123",
            "url": "https://example/run/42",
        }
        with patch.object(release_mod, "_gh_run_view", return_value=payload):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_check_workflow(
                    argparse.Namespace(run_id="42")
                )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)

    def test_in_progress_exits_non_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        payload = {
            "status": "in_progress",
            "conclusion": None,
            "workflowName": "Test",
            "headSha": "abc123",
            "url": "https://example/run/42",
        }
        with patch.object(release_mod, "_gh_run_view", return_value=payload):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_check_workflow(
                    argparse.Namespace(run_id="42")
                )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)


class ReleaseWorkflowRunsTest(unittest.TestCase):
    def test_runs_listing_renders_columns(self) -> None:
        from mozyo_bridge.application import release as release_mod

        runs = [
            {
                "databaseId": 1,
                "createdAt": "2026-05-14T00:00:00Z",
                "status": "completed",
                "conclusion": "success",
                "headSha": "abc",
                "url": "https://example/1",
            },
            {
                "databaseId": 2,
                "createdAt": "2026-05-14T01:00:00Z",
                "status": "in_progress",
                "conclusion": None,
                "headSha": "def",
                "url": "https://example/2",
            },
        ]
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(runs), stderr=""
        )
        with patch.object(release_mod, "_run", return_value=completed):
            with patch.object(release_mod, "_require_command"):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_workflow_runs(
                        argparse.Namespace(workflow="testpypi.yml", limit=10)
                    )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        text = out.getvalue()
        self.assertIn("RUN_ID\tCREATED_AT\tSTATUS\tCONCLUSION\tHEAD_SHA\tHTML_URL", text)
        self.assertIn("1\t2026-05-14T00:00:00Z\tcompleted\tsuccess\tabc\thttps://example/1", text)
        self.assertIn("2\t2026-05-14T01:00:00Z\tin_progress\t\tdef\thttps://example/2", text)


class ReleaseWorkflowWaitTest(unittest.TestCase):
    def test_wait_returns_zero_when_run_completes_successfully(self) -> None:
        from mozyo_bridge.application import release as release_mod

        sequence = [
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ]
        with patch.object(release_mod, "_gh_run_view", side_effect=sequence):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_workflow_wait(
                            argparse.Namespace(run_id="42", timeout=30.0, poll=0.0)
                        )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("conclusion: success", out.getvalue())

    def test_wait_returns_timeout_code_when_deadline_elapses(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with patch.object(
            release_mod,
            "_gh_run_view",
            return_value={"status": "in_progress", "conclusion": None},
        ):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_workflow_wait(
                            argparse.Namespace(
                                run_id="42", timeout=0.0, poll=0.0
                            )
                        )
        self.assertEqual(release_mod.EXIT_TIMEOUT, rc)
        self.assertIn("timeout: exceeded", out.getvalue())

    def test_wait_returns_blocker_when_run_fails(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with patch.object(
            release_mod,
            "_gh_run_view",
            return_value={"status": "completed", "conclusion": "failure"},
        ):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = release_mod.cmd_release_workflow_wait(
                            argparse.Namespace(
                                run_id="42", timeout=30.0, poll=0.0
                            )
                        )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)


class ReleaseBumpPublishParserTest(unittest.TestCase):
    """The bump/publish CLI must enforce mutually-exclusive mode flags and
    pass through per-mode args. Argparse will raise on the missing/
    conflicting-mode cases below if the wiring is wrong, so this is a cheap
    structural check.
    """

    def parse(self, *argv: str) -> argparse.Namespace:
        return build_parser().parse_args(list(argv))

    def test_release_bump_requires_mode(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "bump")

    def test_release_bump_mode_is_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "bump", "--check", "--to", "0.3.0")

    def test_release_bump_check(self) -> None:
        args = self.parse("release", "bump", "--check")
        from mozyo_bridge.application.release import cmd_release_bump

        self.assertIs(args.func, cmd_release_bump)
        self.assertTrue(args.check)
        self.assertIsNone(args.to)

    def test_release_bump_to(self) -> None:
        args = self.parse("release", "bump", "--to", "0.3.0a1")
        from mozyo_bridge.application.release import cmd_release_bump

        self.assertIs(args.func, cmd_release_bump)
        self.assertFalse(args.check)
        self.assertEqual("0.3.0a1", args.to)

    def test_release_publish_requires_mode(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "publish")

    def test_release_publish_mode_is_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "publish", "--testpypi", "--pypi")

    def test_release_publish_testpypi(self) -> None:
        args = self.parse(
            "release", "publish", "--testpypi", "--version", "0.3.0a1"
        )
        self.assertTrue(args.testpypi)
        self.assertEqual("0.3.0a1", args.version)
        self.assertFalse(args.execute)

    def test_release_publish_pypi_dryrun(self) -> None:
        args = self.parse(
            "release",
            "publish",
            "--pypi",
            "--tag",
            "v0.3.0",
            "--notes-file",
            "/tmp/notes.md",
        )
        self.assertTrue(args.pypi)
        self.assertEqual("v0.3.0", args.tag)
        self.assertEqual("/tmp/notes.md", args.notes_file)
        self.assertFalse(args.execute)

    def test_release_publish_pypi_execute(self) -> None:
        args = self.parse(
            "release",
            "publish",
            "--pypi",
            "--tag",
            "v0.3.0",
            "--notes-file",
            "/tmp/notes.md",
            "--execute",
        )
        self.assertTrue(args.execute)

    def test_release_publish_plan(self) -> None:
        args = self.parse("release", "publish", "--plan")
        self.assertTrue(args.plan)


class ReleaseBumpCheckTest(unittest.TestCase):
    """`release bump --check` must (a) read the mirror set from the contract
    doc, (b) report version literals from each mirror file, (c) strict-fail
    when the mirror values disagree. Tests build a fake repo with both a
    contract doc and the mirror-set files.
    """

    def _build_fake_repo(
        self,
        root: Path,
        *,
        pyproject_version: str = "0.3.0",
        module_version: str = "0.3.0",
    ) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "fake"\nversion = "{pyproject_version}"\n',
            encoding="utf-8",
        )
        module_dir = root / "src" / "mozyo_bridge"
        module_dir.mkdir(parents=True)
        (module_dir / "__init__.py").write_text(
            f'__version__ = "{module_version}"\n', encoding="utf-8"
        )
        contract_dir = root / "vibes" / "docs" / "logics"
        contract_dir.mkdir(parents=True)
        (contract_dir / "release-helper-contract.md").write_text(
            "# Contract\n\n"
            "release-version mirror set は以下の 2 file に固定する。\n\n"
            "- `pyproject.toml` の `[project].version`\n"
            "- `src/mozyo_bridge/__init__.py` の `__version__`\n\n"
            "Other section.\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(root), "add", "."],
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "Release v" + pyproject_version, "-q"],
            check=True,
            env=env,
        )

    def test_clean_check_reports_each_mirror_file(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root, pyproject_version="0.3.0", module_version="0.3.0")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=True, to=None)
                )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            text = out.getvalue()
            self.assertIn("pyproject.toml", text)
            self.assertIn("[project].version", text)
            self.assertIn("src/mozyo_bridge/__init__.py", text)
            self.assertIn("__version__", text)
            self.assertIn("0.3.0", text)
            self.assertIn("result: clean", text)

    def test_mirror_set_drift_is_blocker(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(
                root, pyproject_version="0.3.0", module_version="0.2.9"
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=True, to=None)
                )
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn("mirror set values disagree", out.getvalue())

    def test_contract_missing_anchor_is_fatal(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            # Strip the anchor sentence from the contract doc. The helper
            # must refuse to operate rather than guess at the mirror set.
            contract_path = root / "vibes" / "docs" / "logics" / "release-helper-contract.md"
            contract_path.write_text(
                "# Contract\n\n"
                "(mirror-set section removed for this test)\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_bump(
                        argparse.Namespace(repo=str(root), check=True, to=None)
                    )


class ReleaseBumpToTest(unittest.TestCase):
    """`release bump --to` must rewrite every mirror-set file in the
    worktree and never commit/push/tag. Tests assert (a) post-bump file
    contents, (b) absence of any new commits in the fake repo, (c)
    idempotency when called with the existing version.
    """

    def _build_fake_repo(
        self,
        root: Path,
        *,
        pyproject_version: str = "0.3.0",
        module_version: str = "0.3.0",
    ) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "fake"\nversion = "{pyproject_version}"\n',
            encoding="utf-8",
        )
        module_dir = root / "src" / "mozyo_bridge"
        module_dir.mkdir(parents=True)
        (module_dir / "__init__.py").write_text(
            f'__version__ = "{module_version}"\n', encoding="utf-8"
        )
        contract_dir = root / "vibes" / "docs" / "logics"
        contract_dir.mkdir(parents=True)
        (contract_dir / "release-helper-contract.md").write_text(
            "release-version mirror set は以下の 2 file に固定する。\n\n"
            "- `pyproject.toml` の `[project].version`\n"
            "- `src/mozyo_bridge/__init__.py` の `__version__`\n\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init", "-q"],
            check=True,
            env=env,
        )
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()

    def test_rewrites_every_mirror_file_without_committing(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial_head = self._build_fake_repo(root)

            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=False, to="0.4.0")
                )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn(
                '"0.4.0"',
                (root / "pyproject.toml").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"0.4.0"',
                (root / "src" / "mozyo_bridge" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
            )
            head_after = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(
                initial_head,
                head_after,
                "release bump --to created a commit; helper must leave commit "
                "authority with the operator",
            )

    def test_same_version_is_idempotent_noop(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(
                root, pyproject_version="0.4.0", module_version="0.4.0"
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=False, to="0.4.0")
                )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("already at 0.4.0", out.getvalue())
            self.assertIn(
                "no-op (mirror set was already at 0.4.0)", out.getvalue()
            )

    def test_invalid_version_shape_is_rejected(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_bump(
                        argparse.Namespace(
                            repo=str(root), check=False, to="not-a-version"
                        )
                    )

    def test_missing_version_literal_strict_fails(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            # Drop the __version__ literal from the python mirror file so
            # the helper cannot find it. The helper must strict-fail rather
            # than partially rewrite the mirror set — pyproject.toml must
            # still carry the pre-bump version.
            pyproject_before = (root / "pyproject.toml").read_text(encoding="utf-8")
            (root / "src" / "mozyo_bridge" / "__init__.py").write_text(
                "# version moved elsewhere\n", encoding="utf-8"
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        release_mod.cmd_release_bump(
                            argparse.Namespace(
                                repo=str(root), check=False, to="0.4.0"
                            )
                        )
            self.assertEqual(
                pyproject_before,
                (root / "pyproject.toml").read_text(encoding="utf-8"),
                "release bump --to partially rewrote the mirror set on strict-fail",
            )


class ReleasePublishTest(unittest.TestCase):
    """`release publish --pypi` must default to dry-run; `--execute` must
    be required to invoke `gh release create`. `--testpypi` and `--plan`
    are smoke-tested for argv shape via mock.
    """

    def test_pypi_dry_run_does_not_invoke_gh(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            notes.write_text("# v0.3.0\nNotes\n", encoding="utf-8")
            recorded = []

            def fake_run(argv, cwd=None, check=False, env=None):
                recorded.append(list(argv))
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=True,
                            plan=False,
                            tag="v0.3.0",
                            notes_file=str(notes),
                            execute=False,
                            version=None,
                            repo=None,
                        )
                    )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("(dry-run)", out.getvalue())
            self.assertEqual(
                recorded,
                [],
                "dry-run must NOT invoke `gh release create`",
            )
            self.assertIn("Re-run with `--execute`", out.getvalue())

    def test_pypi_execute_invokes_gh_release_create(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            notes.write_text("# v0.3.0\nNotes\n", encoding="utf-8")
            recorded = []

            def fake_run(argv, cwd=None, check=False, env=None):
                recorded.append(list(argv))
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="created\n", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with patch.object(release_mod, "_require_command"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = release_mod.cmd_release_publish(
                            argparse.Namespace(
                                testpypi=False,
                                pypi=True,
                                plan=False,
                                tag="v0.3.0",
                                notes_file=str(notes),
                                execute=True,
                                version=None,
                                repo=None,
                            )
                        )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertEqual(1, len(recorded))
            argv = recorded[0]
            self.assertEqual(argv[0], "gh")
            self.assertEqual(argv[1:4], ["release", "create", "v0.3.0"])
            self.assertIn("--verify-tag", argv)
            self.assertIn("--notes-file", argv)

    def test_pypi_rejects_missing_notes_file(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.md"
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=True,
                            plan=False,
                            tag="v0.3.0",
                            notes_file=str(missing),
                            execute=False,
                            version=None,
                            repo=None,
                        )
                    )

    def test_pypi_rejects_invalid_tag(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            notes.write_text("notes", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=True,
                            plan=False,
                            tag="0.3.0",  # missing `v` prefix
                            notes_file=str(notes),
                            execute=False,
                            version=None,
                            repo=None,
                        )
                    )

    def test_testpypi_dispatch_validates_version_without_workflow_input(self) -> None:
        from mozyo_bridge.application import release as release_mod

        dispatch_call = []

        def fake_run(argv, cwd=None, check=False, env=None):
            dispatch_call.append(list(argv))
            if "workflow" in argv and "run" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )
            # gh run list response
            payload = json.dumps(
                [
                    {
                        "databaseId": 9999,
                        "url": "https://example/run/9999",
                        "createdAt": "2026-05-14T11:00:00Z",
                        "headSha": "abc",
                        "status": "queued",
                    }
                ]
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=payload, stderr=""
            )

        with patch.object(release_mod, "_run", side_effect=fake_run):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_publish(
                            argparse.Namespace(
                                testpypi=True,
                                pypi=False,
                                plan=False,
                                tag=None,
                                notes_file=None,
                                execute=False,
                                version="0.3.0a1",
                                repo=None,
                            )
                        )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertEqual(2, len(dispatch_call))
        dispatch_argv = dispatch_call[0]
        self.assertEqual(
            dispatch_argv,
            [
                "gh",
                "workflow",
                "run",
                "testpypi.yml",
                "--ref",
                "main",
            ],
        )
        self.assertIn("9999", out.getvalue())


class ReleaseCheckDriftTest(unittest.TestCase):
    """Pin Redmine #10688: `mozyo-bridge release check drift` runs both
    pre-existing drift gates and strict-fails on either side.

    The unittest suite already gates each drift surface independently:
    - `CanonicalRendererTest::test_committed_templates_match_canonical_render`
      and `GovernedWorkflowCanonicalTest::test_both_governed_outputs_match_canonical_render`
      for `scaffold canonical --check`;
    - `PluginMarketplaceTest::test_plugin_skill_mirror_matches_canonical`
      and `test_sync_script_check_mode_*` for the plugin mirror.

    This class pins the *release helper* surface: the operator-facing
    command that bundles both checks into one call (mirroring the
    `release check tree` / `release check scaffold` / `release check
    artifact` pattern). A future helper edit that, for example, swallows
    a sub-check's non-zero exit and reports `result: clean` would slip
    past the per-surface tests but fails here.
    """

    SOURCE_TREE_PATHS = (
        Path("src/mozyo_bridge"),
        Path("scripts/sync_plugin_skill.sh"),
        Path("skills/mozyo-bridge-agent"),
        Path("plugins/mozyo-bridge-agent"),
        Path("vibes/docs/logics"),
        Path(".mozyo-bridge/docs/catalog.yaml"),
        Path(".mozyo-bridge/docs/file_conventions.generated.yaml"),
        Path(".mozyo-bridge/scaffold.json"),
        Path("AGENTS.md"),
        Path("CLAUDE.md"),
        Path("pyproject.toml"),
        Path("README.md"),
        Path(".claude-plugin"),
    )

    def _stage_repo(self, dest: Path) -> Path:
        """Copy just the slices the drift helper needs into ``dest``.

        Copying the full repo is wasteful when the helper only consumes
        the source tree, canonical sources, presets, scaffold, sync
        script, skill body, plugin mirror, and the docs catalog. A
        minimal stage also keeps the test fast.
        """
        for relative in self.SOURCE_TREE_PATHS:
            src = ROOT / relative
            if not src.exists():
                continue
            target = dest / relative
            if src.is_dir():
                shutil.copytree(
                    src,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        return dest

    def _run_helper(self, repo: Path) -> tuple[int, str, str]:
        parser = build_parser()
        args = parser.parse_args(
            ["release", "check", "drift", "--repo", str(repo)]
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_clean_tree_exits_zero_and_reports_both_checks(self) -> None:
        result, stdout, stderr = self._run_helper(ROOT)
        self.assertEqual(0, result, msg=stdout + stderr)
        # Both sub-check section headers must appear so operators can
        # see what ran without re-reading the source.
        self.assertIn("scaffold canonical --check", stdout)
        self.assertIn("sync_plugin_skill.sh --check", stdout)
        # Both sub-checks must report up-to-date on a clean tree.
        self.assertIn("AGENTS.md is up to date", stdout)
        self.assertIn("plugin skill mirror is up to date", stdout)
        self.assertIn("result: clean", stdout)

    def test_canonical_drift_causes_strict_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            agents = repo / "src/mozyo_bridge/scaffold/presets/_router/AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nDRIFT\n",
                encoding="utf-8",
            )
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md is out of date", stdout)
            self.assertIn("result: blocker", stdout)
            # Recovery hint must name the real CLI verbatim so the
            # operator can copy-paste from the release-flow doc.
            self.assertIn("mozyo-bridge scaffold canonical", stdout)
            # The mirror check must still have run; its section header
            # is the proof.
            self.assertIn("sync_plugin_skill.sh --check", stdout)

    def test_mirror_drift_causes_strict_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            mirror = (
                repo
                / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md"
            )
            mirror.write_text(
                mirror.read_text(encoding="utf-8") + "\nDRIFT\n",
                encoding="utf-8",
            )
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("plugin skill mirror drift detected", stdout)
            self.assertIn("result: blocker", stdout)
            # Recovery hint must be repo-root runnable per Codex review
            # #50344 (correction landed in #10663 commit 867396a).
            self.assertIn("scripts/sync_plugin_skill.sh", stdout)
            self.assertIn("from the repo root", stdout)
            # The canonical check must still have run on the same
            # invocation; failing fast on one side without reporting
            # the other defeats the bundled-helper purpose.
            self.assertIn("scaffold canonical --check", stdout)

    def test_helper_reports_both_drifts_in_one_run(self) -> None:
        """When both sides drift, the operator sees both findings in
        one run rather than chasing two separate failures."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            agents = repo / "src/mozyo_bridge/scaffold/presets/_router/AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nDRIFT-A\n",
                encoding="utf-8",
            )
            mirror = (
                repo
                / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md"
            )
            mirror.write_text(
                mirror.read_text(encoding="utf-8") + "\nDRIFT-B\n",
                encoding="utf-8",
            )
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md is out of date", stdout)
            self.assertIn("plugin skill mirror drift detected", stdout)
            # Two blocker bullets, one per side.
            self.assertIn("scaffold canonical drift detected", stdout)
            self.assertIn("plugin skill mirror drift detected", stdout)

    def test_missing_sync_script_is_release_blocker(self) -> None:
        """The helper must fail loudly when the sync script is absent,
        not silently pass the mirror gate."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            (repo / "scripts/sync_plugin_skill.sh").unlink()
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("missing sync script", stdout)
            self.assertIn("result: blocker", stdout)
