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
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import __version__
from mozyo_bridge.application.cli import build_parser
from tests.support.private_path_fixtures import linux_home_path, macos_home_path


def _disable_background_git_maintenance(root: Path) -> None:
    """Keep synthetic repos hermetic through TemporaryDirectory teardown.

    Git 2.47+ may detach ``git maintenance run --auto`` after a commit and
    continue writing under ``.git`` after the foreground command returns.  It
    raced cleanup in the production release matrix (#14982), matching the
    measured #14685 mechanism.  These fixtures do not test maintenance, so
    every repository in this module opts out before its first commit; teardown
    errors remain visible rather than being suppressed.
    """
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "config",
            "--local",
            "maintenance.auto",
            "false",
        ],
        check=True,
    )


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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_check_tree

        self.assertIs(args.func, cmd_release_check_tree)

    def test_release_check_scaffold(self) -> None:
        args = self.parse("release", "check", "scaffold")
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_check_scaffold

        self.assertIs(args.func, cmd_release_check_scaffold)

    def test_release_check_artifact(self) -> None:
        args = self.parse("release", "check", "artifact")
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_check_artifact

        self.assertIs(args.func, cmd_release_check_artifact)

    def test_release_check_drift(self) -> None:
        args = self.parse("release", "check", "drift")
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release_drift import cmd_release_check_drift

        self.assertIs(args.func, cmd_release_check_drift)

    def test_release_check_workflow_requires_run_id(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "check", "workflow")
        args = self.parse("release", "check", "workflow", "--run-id", "42")
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_check_workflow

        self.assertIs(args.func, cmd_release_check_workflow)
        self.assertEqual("42", args.run_id)

    def test_release_workflow_runs_requires_workflow(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "runs")
        args = self.parse("release", "workflow", "runs", "--workflow", "testpypi.yml")
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_workflow_runs

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_workflow_wait

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
        _disable_background_git_maintenance(root)
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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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

    def test_shared_home_path_fixtures_are_what_this_gate_blocks(self) -> None:
        """The suite's private-path negative controls compose their home-shaped
        values with `tests.support.private_path_fixtures` instead of writing the
        literal, so that the tracked bytes carry nothing this gate blocks while
        the code under test still receives exactly such a path (Redmine #14656).

        That indirection is only a negative control while the composed value is
        still blocker-shaped: a helper quietly degraded to a neutral path would
        leave every call site green and testing nothing. Pin it against the real
        command, not against a second copy of the pattern.
        """
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        for fixture, composed in (
            ("macos_home_path", macos_home_path("someone", "secret", "path")),
            ("linux_home_path", linux_home_path("someone", ".claude")),
        ):
            with self.subTest(fixture=fixture):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._init_repo(root)
                    self._commit_file(
                        root, "AGENTS.md", f"see {composed} for context\n"
                    )
                    args = argparse.Namespace(repo=str(root))
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_check_tree(args)
                    self.assertEqual(release_mod.EXIT_BLOCKER, rc)
                    self.assertIn(composed, out.getvalue())

    def test_secret_value_shape_in_tracked_file_is_blocker(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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

    def test_call_terminated_literal_secret_still_blocks(self) -> None:
        # Redmine #13695: a real credential literal passed as the final call
        # argument leaves a call-closing `)` glued to the captured value. The
        # tree scan must separate that punctuation and still flag the leaked
        # literal instead of misreading the `)` as an expression and returning
        # clean (the pre-fix blind spot that let the canary through).
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        secret_key = "sup" + "er-secret-key123"
        leak_line = 'cache = RedmineContextCache(api_key="' + secret_key + '")'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "config.py", leak_line + "\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            output = out.getvalue()
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn("result: blocker", output)
            self.assertIn(secret_key, output)

    def test_underscore_and_dict_env_key_literals_still_block(self) -> None:
        # Redmine #13716: the first-stage grep anchored the credential keyword
        # on a bare word boundary, so a leading-underscore identifier
        # (`_API_KEY = ...`) and an ENV-name dict key (`API_KEY_ENV: ...`) never
        # surfaced as candidates and a real literal in either shape returned
        # clean. Both must now block. Key/separator are split so this test
        # source never carries a contiguous matchable assignment token.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        token = "sk" + "-live-abc123def456"
        underscore_line = "_API" + "_KEY = \"" + token + "\""
        dict_env_line = "environ = {" + "API" + "_KEY_ENV: \"" + token + "\"}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(
                root, "leaky.py", underscore_line + "\n" + dict_env_line + "\n"
            )
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            output = out.getvalue()
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn("result: blocker", output)
            # Both leaked shapes are reported.
            self.assertEqual(2, output.count(token))

    def test_r1_segment_bounded_scan_end_to_end(self) -> None:
        # Redmine #13716 R1: end-to-end through the POSIX-ERE grep first stage.
        # F1 — a real UPPER_SNAKE credential under a non-`*_ENV` key must block
        # (the env-name exemption is key-scoped, not value-global). F2 — glued
        # substrings of a credential keyword must NOT surface as candidates.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        real_secret = "REAL" + "_SECRET_123"
        leak_line = "API" + "_KEY = \"" + real_secret + "\""
        glued = "\n".join(
            (
                "password" + "_length = 16",
                "password" + "less = \"" + "ab" + "c123\"",
                "access" + "_tokenizer = \"" + "ab" + "c123\"",
                # Allowlisted env-var name definition (exempt).
                "API" + "_KEY_ENV = \"" + "MOZYO_REDMINE" + "_API_KEY\"",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "leak.py", leak_line + "\n")
            self._commit_file(root, "safe.py", glued + "\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            output = out.getvalue()
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn(real_secret, output)
            # The glued-substring / allowlisted env-name lines are not reported.
            self.assertNotIn("password_length", output)
            self.assertNotIn("passwordless", output)
            self.assertNotIn("access_tokenizer", output)
            self.assertNotIn("MOZYO_REDMINE_API_KEY", output)

    def test_r2_tree_artifact_parity(self) -> None:
        # Redmine #13716 R2/R3: the first-stage grep and the shared second-stage
        # classifier use the same segment-bounded grammar, so a line's tree and
        # artifact verdicts always agree. R3-F1: a `*_ENV` key exempts only a
        # value in the known env-name allowlist; any other literal (`REAL_..._123`)
        # under a `*_ENV` key still blocks. R2-F2: a glued provider prefix
        # (`mygithub_token`) is a candidate in neither scan.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        env_key = "API" + "_KEY_ENV"
        # The allowlisted production env-var name (kept safe); split so this test
        # source carries no contiguous credential-shaped assignment.
        known_env_name = "MOZYO_REDMINE" + "_API_KEY"
        self.assertIn(known_env_name, release_mod._KNOWN_CREDENTIAL_ENV_NAMES)
        cases = (
            (env_key + " = \"" + "REAL" + "_API_KEY_123\"", True),  # secret under *_ENV
            (env_key + " = \"" + known_env_name + "\"", False),     # allowlisted env-name
            ("mygithub" + "_token = \"" + "ab" + "c123\"", False),  # glued provider prefix
        )
        personal_pattern = re.compile("|".join(release_mod._PERSONAL_PATH_PATTERNS))
        for line, expect_block in cases:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._init_repo(root)
                self._commit_file(root, "f.py", line + "\n")
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = release_mod.cmd_release_check_tree(
                        argparse.Namespace(repo=str(root))
                    )
                tree_block = rc == release_mod.EXIT_BLOCKER
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "f.py").write_text(line + "\n", encoding="utf-8")
                artifact_block = bool(
                    release_mod._grep_artifact_tree(root, personal_pattern)
                )
            self.assertEqual(
                tree_block, artifact_block,
                msg=f"tree/artifact parity broken for {line!r}",
            )
            self.assertEqual(
                expect_block, tree_block, msg=f"unexpected verdict for {line!r}"
            )


class SecretValueClassifierTest(unittest.TestCase):
    """Redmine #12175: pin the second-stage credential-value classifier that
    separates real leaked literals from code that merely names a credential.
    Real-secret-shaped tokens are assembled by concatenation so this test file
    does not itself carry a contiguous secret-shaped literal.
    """

    def test_rejects_code_identifier_and_sentinel_values(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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

    def test_accepts_call_terminated_literals_and_still_rejects_expressions(
        self,
    ) -> None:
        # Redmine #13695: a real credential literal passed as the final call
        # argument, the last list/dict element, or a trailing-comma assignment
        # leaves a closing `)` `]` `}` or separator `,` `;` glued to the captured
        # value. That punctuation is enclosing syntax, not part of the secret, so
        # it must be separated before classification — quoted and unquoted alike.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        token = "ab" + "c123"
        accept_values = (
            '"' + token + '")',  # quoted literal, call-closing paren
            "'" + token + "')",  # single-quoted, call-closing paren
            '"' + token + '"]',  # quoted literal, list-closing bracket
            '"' + token + '"}',  # quoted literal, dict/set-closing brace
            '"' + token + '");',  # quoted literal, call close + semicolon
            token + ")",  # unquoted token, call-closing paren
            token + "],",  # unquoted token, list close + comma
        )
        for value in accept_values:
            self.assertTrue(
                release_mod._secret_value_is_real(value),
                msg=f"expected real call-terminated secret: {value!r}",
            )

        # Separating trailing closers must not re-admit a genuine expression
        # that merely ends in one: its unmatched *opening* bracket survives the
        # strip and still marks it as code structure, not a literal.
        reject_values = (
            "get_key()",  # call expression as last arg
            "os.environ[API_KEY])",  # index expression ending in a closer
            'config["API_KEY"])',  # subscript expression
            "build({key})",  # nested call / dict expression
            "factory(make())",  # nested call
        )
        for value in reject_values:
            self.assertFalse(
                release_mod._secret_value_is_real(value),
                msg=f"expected non-secret expression: {value!r}",
            )

    def test_assignment_classifier_pins_request_cases(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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

    def test_13716_underscore_and_dict_env_key_shapes(self) -> None:
        # Redmine #13716: the credential keyword must be caught inside a wider
        # identifier — a leading underscore / name prefix, and a trailing ENV
        # segment used as a dict key — while the value classifier still rejects
        # the widened key when its value is a reference / keyword / placeholder.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        token = "ab" + "c123"
        # Real literals in the newly covered identifier shapes must be flagged.
        real_lines = (
            "_API" + "_KEY = \"" + token + "\"",          # leading underscore
            "OPENAI" + "_API_KEY = " + token,             # name prefix
            "_GITHUB" + "_TOKEN = '" + token + "'",       # provider, leading _
            "environ = {" + "API" + "_KEY_ENV: \"" + token + "\"}",  # dict ENV key
            "GITHUB" + "_TOKEN_ENV = \"" + token + "\"",  # provider, trailing seg
        )
        for line in real_lines:
            self.assertTrue(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected real credential literal: {line!r}",
            )

        # The widened key with a non-literal value stays safe: an env read, a
        # bare identifier reference, a type annotation, or a None default.
        safe_lines = (
            "_API" + "_KEY = os.environ.get(" + "API" + "_KEY_ENV)",
            "_api" + "_key: str | None = None",
            "environ = {" + "API" + "_KEY_ENV: " + "API_KEY" + "}",
            "environ = {" + "API" + "_KEY_ENV: None}",
        )
        for line in safe_lines:
            self.assertFalse(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected safe widened-key line: {line!r}",
            )

    def test_13716_r1f1_env_name_exemption_is_key_scoped(self) -> None:
        # Redmine #13716 R1-F1: the env-var-name exemption must be scoped to the
        # key context (`*_ENV` key + UPPER_SNAKE value), NOT applied to every
        # UPPER_SNAKE value. A real credential shaped as an underscore-joined
        # uppercase literal must still block; only the `*_ENV` name-constant is
        # safe, and a non-name value under a `*_ENV` key still blocks.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        # Key/value split so the test source carries no contiguous assignment.
        real_secret = "REAL" + "_SECRET_123"  # UPPER_SNAKE real credential value
        blocks = (
            "API" + "_KEY = \"" + real_secret + "\"",              # non-ENV key -> real
            "API" + "_KEY_ENV = \"" + "sk" + "-live-x9\"",        # ENV key, non-name value
        )
        for line in blocks:
            self.assertTrue(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected real credential to block: {line!r}",
            )
        # `*_ENV` key bound to an allowlisted env-var name is a reference.
        env_name_defs = (
            "API" + "_KEY_ENV = \"" + "MOZYO_REDMINE" + "_API_KEY\"",
        )
        for line in env_name_defs:
            self.assertFalse(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected env-name definition to be safe: {line!r}",
            )
        # The value classifier alone no longer treats UPPER_SNAKE as safe: the
        # exemption lives in the key-aware assignment classifier.
        self.assertTrue(release_mod._secret_value_is_real("REAL" + "_SECRET_123"))

    def test_13716_r1f2_segment_bounded_no_glued_substring(self) -> None:
        # Redmine #13716 R1-F2: the identifier grammar is segment-bounded, so a
        # credential keyword that is merely a glued substring of an unrelated
        # identifier must NOT be flagged.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        token = "ab" + "c123"
        non_credentials = (
            "password" + "_length = 16",           # `password` + non-ENV suffix
            "password" + "less = \"" + token + "\"",  # glued suffix, no boundary
            "access" + "_tokenizer = \"" + token + "\"",  # `access_token` + glued
        )
        for line in non_credentials:
            self.assertFalse(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected non-credential identifier to be safe: {line!r}",
            )

    def test_13716_r3f1_env_name_exemption_is_allowlist_not_shape(self) -> None:
        # Redmine #13716 R3-F1: the `*_ENV` exemption is an explicit allowlist of
        # known env-var names, NOT a value-shape rule. Only an allowlisted name is
        # safe; any other literal under a `*_ENV` key — even one that is
        # UPPER_SNAKE and carries a credential keyword (`REAL_API_KEY_123`) —
        # still blocks, because shape cannot separate an env-name from a secret.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        env_key = "API" + "_KEY_ENV"
        blocks = (
            env_key + " = \"" + "REAL" + "_API_KEY_123\"",   # UPPER_SNAKE + keyword, not listed
            env_key + " = \"" + "UPPER" + "_SNAKE_SECRET\"",  # UPPER_SNAKE, not listed
            env_key + " = \"" + "REDMINE" + "_API_KEY\"",     # env-name shape, not listed
        )
        for line in blocks:
            self.assertTrue(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected non-allowlisted literal under *_ENV to block: {line!r}",
            )
        # Only the allowlisted production env-var name is safe.
        known = "MOZYO_REDMINE" + "_API_KEY"
        self.assertIn(known, release_mod._KNOWN_CREDENTIAL_ENV_NAMES)
        env_names = (
            env_key + " = \"" + known + "\"",
        )
        for line in env_names:
            self.assertFalse(
                release_mod._secret_assignment_is_real(line),
                msg=f"expected allowlisted env-var name to be safe: {line!r}",
            )

    def test_12693_field_name_false_positives_are_safe(self) -> None:
        # Redmine #12693: the concrete v0.9.1 release-gate false positives —
        # a same-name keyword pass-through, a None sentinel inside a string,
        # and a snake_case identifier assignment — name a credential field but
        # carry no literal value, so they must not block the tree scan.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        pattern = re.compile(release_mod._artifact_grep_pattern())
        fake_secret = "REDMINE" + "_API_KEY=" + "abc123"
        self.assertIsNone(pattern.search("Do not store tokens or secrets."))
        self.assertIsNotNone(pattern.search(fake_secret))

    def test_artifact_tree_scan_filters_identifier_false_positives(self) -> None:
        # Redmine #12175: the extracted-artifact scan applies the same
        # credential classifier as `release check tree`, so packaged source
        # that names a credential identifier is not flagged, while a personal
        # path or a real literal secret still is.
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            self._init_artifact_repo(repo)
            (repo / "dist").mkdir()
            sentinel = repo / "dist" / "preexisting.whl"
            sentinel.write_bytes(b"preexisting")

            recorded: list[dict] = []
            original_run = release_mod._run

            def fake_run(argv, cwd=None, check=False, env=None):
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
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

    @staticmethod
    def _init_artifact_repo(root: Path) -> None:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        _disable_background_git_maintenance(root)
        (root / ".gitignore").write_text(
            "dist/\n*.egg-info/\n", encoding="utf-8"
        )
        (root / "src").mkdir()
        (root / "src" / "package.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(root), "add", ".gitignore", "src/package.py"],
            check=True,
        )

    @staticmethod
    def _worktree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
        rows: list[tuple[object, ...]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] == ".git":
                continue
            if path.is_symlink():
                rows.append((relative.as_posix(), "symlink", os.readlink(path)))
            elif path.is_file():
                rows.append(
                    (
                        relative.as_posix(),
                        "file",
                        path.stat().st_mode,
                        path.read_bytes(),
                    )
                )
            elif path.is_dir():
                rows.append((relative.as_posix(), "dir", path.stat().st_mode))
        return tuple(rows)

    @staticmethod
    def _status_with_ignored(root: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--ignored"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout

    def test_artifact_check_preserves_repo_on_success_failure_and_scan_blocker(
        self,
    ) -> None:
        """The build may write metadata, but only inside the temporary source.

        Exercise all three exits that previously risked leaking
        ``src/*.egg-info`` into the caller's checkout: clean artifact, build
        failure, and an artifact-scan blocker.  Byte/mode identity and git's
        ignored-status projection must survive every path.
        """
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        original_run = release_mod._run
        for outcome, expected in (
            ("success", release_mod.EXIT_CLEAN),
            ("build_failure", release_mod.EXIT_BLOCKER),
            ("scan_blocker", release_mod.EXIT_BLOCKER),
        ):
            with self.subTest(outcome=outcome):
                with tempfile.TemporaryDirectory() as repo_str:
                    repo = Path(repo_str).resolve()
                    self._init_artifact_repo(repo)
                    (repo / "dist").mkdir()
                    (repo / "dist" / "preexisting.whl").write_bytes(b"sentinel")
                    before = self._worktree_fingerprint(repo)
                    status_before = self._status_with_ignored(repo)
                    build_cwds: list[Path] = []

                    def fake_run(argv, cwd=None, check=False, env=None):
                        if list(argv[:2]) == ["git", "ls-files"]:
                            return original_run(
                                argv, cwd=cwd, check=check, env=env
                            )
                        self.assertIn("build", argv)
                        build_cwd = Path(cwd).resolve()
                        build_cwds.append(build_cwd)
                        (build_cwd / "src" / "package.egg-info").mkdir(
                            parents=True, exist_ok=True
                        )
                        metadata = (
                            build_cwd / "src" / "package.egg-info" / "PKG-INFO"
                        )
                        metadata.write_text("generated\n", encoding="utf-8")
                        outdir = Path(argv[argv.index("--outdir") + 1])
                        outdir.mkdir(parents=True, exist_ok=True)
                        if outcome == "build_failure":
                            return subprocess.CompletedProcess(
                                args=argv,
                                returncode=1,
                                stdout="",
                                stderr="build failed",
                            )
                        wheel = outdir / "package-0.1-py3-none-any.whl"
                        body = "VALUE = 1\n"
                        if outcome == "scan_blocker":
                            body = (
                                "home = "
                                + macos_home_path("example", "project")
                                + "\n"
                            )
                        with zipfile.ZipFile(wheel, "w") as archive:
                            archive.writestr("package.py", body)
                        return subprocess.CompletedProcess(
                            args=argv, returncode=0, stdout="", stderr=""
                        )

                    with patch.object(release_mod, "_run", side_effect=fake_run):
                        with contextlib.redirect_stdout(io.StringIO()):
                            rc = release_mod.cmd_release_check_artifact(
                                argparse.Namespace(repo=str(repo))
                            )

                    self.assertEqual(expected, rc)
                    self.assertEqual(before, self._worktree_fingerprint(repo))
                    self.assertEqual(status_before, self._status_with_ignored(repo))
                    self.assertEqual(1, len(build_cwds))
                    with self.assertRaises(ValueError):
                        build_cwds[0].relative_to(repo)

    def test_artifact_check_rejects_absolute_symlink_without_running_build(
        self,
    ) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            self._init_artifact_repo(repo)
            victim = repo / "src" / "victim.txt"
            victim.write_text("source\n", encoding="utf-8")
            link = repo / "src" / "absolute-link.txt"
            link.symlink_to(victim)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "add",
                    "src/victim.txt",
                    "src/absolute-link.txt",
                ],
                check=True,
            )
            before = self._worktree_fingerprint(repo)
            original_run = release_mod._run
            build_called = False

            def fake_run(argv, cwd=None, check=False, env=None):
                nonlocal build_called
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_called = True
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertFalse(build_called)
            self.assertIn("absolute symlink", out.getvalue())
            self.assertEqual(before, self._worktree_fingerprint(repo))

    def test_artifact_check_rejects_relative_symlink_escape(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as outer_str:
            outer = Path(outer_str).resolve()
            repo = outer / "repo"
            self._init_artifact_repo(repo)
            (outer / "outside.txt").write_text("outside\n", encoding="utf-8")
            link = repo / "escape.txt"
            link.symlink_to("../outside.txt")
            subprocess.run(
                ["git", "-C", str(repo), "add", "escape.txt"], check=True
            )
            original_run = release_mod._run
            build_called = False

            def fake_run(argv, cwd=None, check=False, env=None):
                nonlocal build_called
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_called = True
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertFalse(build_called)
            self.assertIn("escapes the repository snapshot", out.getvalue())
            self.assertEqual("outside\n", (outer / "outside.txt").read_text())

    def test_artifact_check_rejects_relative_escape_that_reenters_repo(
        self,
    ) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as outer_str:
            repo = (Path(outer_str) / "repo").resolve()
            self._init_artifact_repo(repo)
            victim = repo / "src" / "victim.txt"
            victim.write_text("source\n", encoding="utf-8")
            link = repo / "src" / "reenter.txt"
            target = "../" * 64 + victim.as_posix().lstrip("/")
            link.symlink_to(target)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "add",
                    "src/victim.txt",
                    "src/reenter.txt",
                ],
                check=True,
            )
            original_run = release_mod._run
            build_called = False

            def fake_run(argv, cwd=None, check=False, env=None):
                nonlocal build_called
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_called = True
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertFalse(build_called)
            self.assertIn("escapes the repository snapshot", out.getvalue())
            self.assertEqual("source\n", victim.read_text(encoding="utf-8"))

    def test_artifact_check_keeps_internal_relative_symlink_inside_snapshot(
        self,
    ) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            self._init_artifact_repo(repo)
            victim = repo / "src" / "victim.txt"
            victim.write_text("source\n", encoding="utf-8")
            link = repo / "src" / "relative-link.txt"
            link.symlink_to("victim.txt")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "add",
                    "src/victim.txt",
                    "src/relative-link.txt",
                ],
                check=True,
            )
            before = self._worktree_fingerprint(repo)
            original_run = release_mod._run

            def fake_run(argv, cwd=None, check=False, env=None):
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_root = Path(cwd)
                (build_root / "src" / "relative-link.txt").write_text(
                    "snapshot\n", encoding="utf-8"
                )
                outdir = Path(argv[argv.index("--outdir") + 1])
                outdir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(
                    outdir / "package-0.1-py3-none-any.whl", "w"
                ) as archive:
                    archive.writestr("package.py", "VALUE = 1\n")
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertEqual(before, self._worktree_fingerprint(repo))
            self.assertEqual("source\n", victim.read_text(encoding="utf-8"))

    def test_artifact_check_materializes_tracked_file_replaced_by_directory(
        self,
    ) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            self._init_artifact_repo(repo)
            shape = repo / "shape"
            shape.write_text("tracked file\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "shape"], check=True)
            shape.unlink()
            shape.mkdir()
            (shape / "child.txt").write_text("current tree\n", encoding="utf-8")
            before = self._worktree_fingerprint(repo)
            original_run = release_mod._run
            copied_child: list[bytes] = []

            def fake_run(argv, cwd=None, check=False, env=None):
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_root = Path(cwd)
                copied_child.append((build_root / "shape" / "child.txt").read_bytes())
                outdir = Path(argv[argv.index("--outdir") + 1])
                outdir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(
                    outdir / "package-0.1-py3-none-any.whl", "w"
                ) as archive:
                    archive.writestr("package.py", "VALUE = 1\n")
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertEqual([b"current tree\n"], copied_child)
            self.assertEqual(before, self._worktree_fingerprint(repo))

    def _assert_replaced_directory_without_current_content_is_omitted(
        self, *, ignored_child: bool
    ) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            self._init_artifact_repo(repo)
            shape = repo / "shape"
            shape.write_text("tracked file\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "shape"], check=True)
            shape.unlink()
            shape.mkdir()
            if ignored_child:
                with (repo / ".gitignore").open("a", encoding="utf-8") as stream:
                    stream.write("shape/\n")
                (shape / "ignored.txt").write_text("ignored\n", encoding="utf-8")

            listed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.split("\0")
            self.assertIn("shape", listed, "the stale cached file path remains listed")
            self.assertFalse(
                any(item.startswith("shape/") for item in listed if item),
                "Git lists no current source child for an empty/ignored-only directory",
            )

            before = self._worktree_fingerprint(repo)
            original_run = release_mod._run
            copied_shape: list[bool] = []

            def fake_run(argv, cwd=None, check=False, env=None):
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_root = Path(cwd)
                copied_shape.append((build_root / "shape").exists())
                outdir = Path(argv[argv.index("--outdir") + 1])
                outdir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(
                    outdir / "package-0.1-py3-none-any.whl", "w"
                ) as archive:
                    archive.writestr("package.py", "VALUE = 1\n")
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertEqual([False], copied_shape)
            self.assertEqual(before, self._worktree_fingerprint(repo))

    def test_artifact_check_omits_tracked_file_replaced_by_empty_directory(
        self,
    ) -> None:
        self._assert_replaced_directory_without_current_content_is_omitted(
            ignored_child=False
        )

    def test_artifact_check_omits_tracked_file_replaced_by_ignored_only_directory(
        self,
    ) -> None:
        self._assert_replaced_directory_without_current_content_is_omitted(
            ignored_child=True
        )

    def test_artifact_check_still_rejects_a_tracked_gitlink_directory(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            self._init_artifact_repo(repo)
            nested = repo / "nested-repository"
            subprocess.run(["git", "init", "-q", str(nested)], check=True)
            subprocess.run(
                ["git", "-C", str(nested), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(nested), "config", "user.name", "Test"],
                check=True,
            )
            (nested / "content.txt").write_text("nested\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(nested), "add", "content.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(nested), "commit", "-qm", "nested"], check=True
            )
            subprocess.run(
                ["git", "-C", str(repo), "add", "nested-repository"],
                check=True,
                capture_output=True,
            )
            original_run = release_mod._run
            build_called = False

            def fake_run(argv, cwd=None, check=False, env=None):
                nonlocal build_called
                if list(argv[:2]) == ["git", "ls-files"]:
                    return original_run(argv, cwd=cwd, check=check, env=env)
                build_called = True
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertFalse(build_called)
            self.assertIn("gitlink", output.getvalue())


class ReleaseCheckWorkflowTest(unittest.TestCase):
    def test_success_exits_zero(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        runs = [
            {
                "databaseId": 1,
                "name": "TestPyPI exact 0.10.0 @ deadbeef (nonce abc123)",
                "createdAt": "2026-05-14T00:00:00Z",
                "status": "completed",
                "conclusion": "success",
                "headSha": "abc",
                "url": "https://example/1",
            },
            {
                "databaseId": 2,
                "name": "TestPyPI dev (auto main-CI)",
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
        # The RUN_NAME column carries the run-name (with the dispatch nonce) so
        # operators can correlate a dispatch to its run deterministically.
        self.assertIn(
            "RUN_ID\tCREATED_AT\tSTATUS\tCONCLUSION\tHEAD_SHA\tHTML_URL\tRUN_NAME",
            text,
        )
        self.assertIn(
            "1\t2026-05-14T00:00:00Z\tcompleted\tsuccess\tabc\thttps://example/1\t"
            "TestPyPI exact 0.10.0 @ deadbeef (nonce abc123)",
            text,
        )
        self.assertIn(
            "2\t2026-05-14T01:00:00Z\tin_progress\t\tdef\thttps://example/2\t"
            "TestPyPI dev (auto main-CI)",
            text,
        )


class ReleaseWorkflowWaitTest(unittest.TestCase):
    def test_wait_returns_zero_when_run_completes_successfully(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_bump

        self.assertIs(args.func, cmd_release_bump)
        self.assertTrue(args.check)
        self.assertIsNone(args.to)

    def test_release_bump_to(self) -> None:
        args = self.parse("release", "bump", "--to", "0.3.0a1")
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import cmd_release_bump

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
        # Exact-candidate dispatch (Redmine #13601): source-sha / expected-version
        # / source-ref parse through; the legacy --version alias still parses.
        args = self.parse(
            "release",
            "publish",
            "--testpypi",
            "--source-sha",
            "a" * 40,
            "--expected-version",
            "0.10.0",
            "--source-ref",
            "int_13472_session_continuity",
        )
        self.assertTrue(args.testpypi)
        self.assertEqual("a" * 40, args.source_sha)
        self.assertEqual("0.10.0", args.expected_version)
        self.assertEqual("int_13472_session_continuity", args.source_ref)
        self.assertFalse(args.execute)

    def test_release_publish_testpypi_version_alias(self) -> None:
        args = self.parse(
            "release", "publish", "--testpypi", "--version", "0.3.0a1"
        )
        self.assertTrue(args.testpypi)
        self.assertEqual("0.3.0a1", args.version)
        self.assertIsNone(args.expected_version)

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
        _disable_background_git_maintenance(root)
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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        _disable_background_git_maintenance(root)
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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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

    def test_fake_repo_disables_detached_auto_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            configured = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "config",
                    "--local",
                    "--get",
                    "maintenance.auto",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(
                "false",
                configured,
                "synthetic release-bump repo can spawn detached Git maintenance "
                "and race TemporaryDirectory.cleanup",
            )

    def test_invalid_version_shape_is_rejected(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

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

    # Exact-candidate dispatch inputs (Redmine #13601).
    SOURCE_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    NONCE = "deadbeefcafef00d"

    def test_plan_renders_a_complete_reparseable_testpypi_command(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        source_sha = self.SOURCE_SHA

        def fake_run(argv, cwd=None, check=False, env=None):
            if list(argv[:4]) == ["git", "rev-parse", "--verify", "HEAD"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout=source_sha + "\n", stderr=""
                )
            if list(argv[:4]) == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout="refs/heads/main\n",
                    stderr="",
                )
            if list(argv[:3]) == ["git", "remote"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="origin\n", stderr=""
                )
            if list(argv[:3]) == ["gh", "run", "list"]:
                self.assertEqual(ROOT.resolve(), Path(cwd).resolve())
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="[]", stderr=""
                )
            self.fail(f"unexpected command: {argv!r}")

        with patch.object(release_mod, "_run", side_effect=fake_run):
            with patch.object(release_mod, "_require_command"):
                with patch.object(
                    release_mod, "_testpypi_existing_version", return_value="absent"
                ):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_publish(
                            argparse.Namespace(
                                testpypi=False,
                                pypi=False,
                                plan=True,
                                repo=str(ROOT),
                            )
                        )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        option_line = next(
            line
            for line in out.getvalue().splitlines()
            if line.startswith("- TestPyPI rehearsal:")
        )
        command = re.search(r"`([^`]+)`", option_line).group(1)
        argv = shlex.split(command)
        self.assertEqual("mozyo-bridge", argv[0])
        parsed = build_parser().parse_args(argv[1:])
        self.assertEqual(source_sha, parsed.source_sha)
        self.assertEqual(__version__, parsed.expected_version)
        self.assertEqual("refs/heads/main", parsed.source_ref)
        self.assertEqual(str(ROOT.resolve()), parsed.repo)
        with patch.object(
            release_mod, "_publish_testpypi", return_value=0
        ) as handler:
            self.assertEqual(0, parsed.func(parsed))
        handler.assert_called_once_with(parsed)

    def test_plan_refuses_detached_head_without_printing_incomplete_command(
        self,
    ) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        def fake_run(argv, cwd=None, check=False, env=None):
            if list(argv[:4]) == ["git", "rev-parse", "--verify", "HEAD"]:
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout=self.SOURCE_SHA + "\n",
                    stderr="",
                )
            if list(argv[:4]) == ["git", "symbolic-ref", "--quiet", "HEAD"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=1, stdout="", stderr=""
                )
            self.fail(f"unexpected command: {argv!r}")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(release_mod, "_run", side_effect=fake_run):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=False,
                            plan=True,
                            repo=str(ROOT),
                        )
                    )
        self.assertIn("detached HEAD", stderr.getvalue())
        self.assertNotIn("TestPyPI rehearsal", stdout.getvalue())

    def _stub_source_ref_policy(self) -> None:
        """Neutralize the origin preflight for dispatch-shape tests.

        `_publish_testpypi` resolves `--source-ref` against the real origin
        before dispatching (Redmine #13883). These tests cover the dispatch
        argv and nonce correlation with a synthetic SHA, so the preflight is
        stubbed here to keep them focused and offline; the preflight's own
        behaviour is covered against a real isolated origin in
        tests/regressions/test_issue_13883_source_ref_preflight.py.
        """
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        for name, result in (("validate", None), ("preflight", "refs/heads/int_13472_session_continuity")):
            patcher = patch.object(
                release_mod.source_ref_policy, name, return_value=result
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _testpypi_namespace(self, **overrides) -> argparse.Namespace:
        self._stub_source_ref_policy()
        base = dict(
            testpypi=True,
            pypi=False,
            plan=False,
            tag=None,
            notes_file=None,
            execute=False,
            source_sha=self.SOURCE_SHA,
            expected_version="0.10.0",
            source_ref="int_13472_session_continuity",
            version=None,
            repo=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_testpypi_dispatch_passes_exact_inputs_and_correlates_by_nonce(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        calls = []

        def fake_run(argv, cwd=None, check=False, env=None):
            calls.append(list(argv))
            if "workflow" in argv and "run" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )
            # gh run list: one run whose run-name carries the nonce.
            payload = json.dumps(
                [
                    {
                        "databaseId": 9999,
                        "name": f"TestPyPI exact 0.10.0 @ {self.SOURCE_SHA} (nonce {self.NONCE})",
                        "url": "https://example/run/9999",
                        "createdAt": "2026-05-14T11:00:00Z",
                        "headSha": "mainhead",
                        "status": "queued",
                    },
                    {
                        "databaseId": 8888,
                        "name": "TestPyPI dev (auto main-CI)",
                        "url": "https://example/run/8888",
                        "createdAt": "2026-05-14T10:00:00Z",
                        "headSha": "otherhead",
                        "status": "completed",
                    },
                ]
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=payload, stderr=""
            )

        with patch.object(release_mod, "_run", side_effect=fake_run):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod, "_new_dispatch_nonce", return_value=self.NONCE):
                    with patch.object(release_mod.time, "sleep"):
                        with contextlib.redirect_stdout(io.StringIO()) as out:
                            rc = release_mod.cmd_release_publish(
                                self._testpypi_namespace()
                            )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        dispatch_argv = calls[0]
        self.assertEqual(
            dispatch_argv,
            [
                "gh",
                "workflow",
                "run",
                "testpypi.yml",
                "--ref",
                "main",
                "-f",
                f"source_sha={self.SOURCE_SHA}",
                "-f",
                "expected_version=0.10.0",
                "-f",
                "source_ref=int_13472_session_continuity",
                "-f",
                f"dispatch_nonce={self.NONCE}",
            ],
        )
        text = out.getvalue()
        # Correlated deterministically to the nonce-matching run, not the latest.
        self.assertIn("run_id: 9999", text)
        self.assertIn(self.NONCE, text)

    def test_testpypi_dispatch_fail_closed_when_no_nonce_match(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        def fake_run(argv, cwd=None, check=False, env=None):
            if "workflow" in argv and "run" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )
            # No run carries the nonce -> must NOT fall back to latest-one.
            payload = json.dumps(
                [
                    {
                        "databaseId": 8888,
                        "name": "TestPyPI dev (auto main-CI)",
                        "url": "https://example/run/8888",
                        "createdAt": "2026-05-14T10:00:00Z",
                        "headSha": "otherhead",
                        "status": "completed",
                    }
                ]
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=payload, stderr=""
            )

        with patch.object(release_mod, "_run", side_effect=fake_run):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod, "_new_dispatch_nonce", return_value=self.NONCE):
                    with patch.object(release_mod.time, "sleep"):
                        with contextlib.redirect_stdout(io.StringIO()) as out:
                            rc = release_mod.cmd_release_publish(
                                self._testpypi_namespace()
                            )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)
        text = out.getvalue()
        self.assertIn("not deterministically correlated", text)
        self.assertNotIn("run_id: 8888", text)

    def test_testpypi_dispatch_fail_closed_when_multiple_nonce_matches(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        def fake_run(argv, cwd=None, check=False, env=None):
            if "workflow" in argv and "run" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )
            payload = json.dumps(
                [
                    {"databaseId": 1, "name": f"a nonce {self.NONCE}", "status": "queued"},
                    {"databaseId": 2, "name": f"b nonce {self.NONCE}", "status": "queued"},
                ]
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=payload, stderr=""
            )

        with patch.object(release_mod, "_run", side_effect=fake_run):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod, "_new_dispatch_nonce", return_value=self.NONCE):
                    with patch.object(release_mod.time, "sleep"):
                        with contextlib.redirect_stdout(io.StringIO()) as out:
                            rc = release_mod.cmd_release_publish(
                                self._testpypi_namespace()
                            )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)
        self.assertIn("multiple runs matched", out.getvalue())

    def test_testpypi_requires_source_sha_expected_version_and_ref(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        for missing in ("source_sha", "expected_version", "source_ref"):
            with self.subTest(missing=missing):
                ns = self._testpypi_namespace(**{missing: None})
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        release_mod.cmd_release_publish(ns)

    def test_testpypi_version_alias_supplies_expected_version(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        captured = {}

        def fake_dispatch(source_sha, expected_version, source_ref, nonce):
            captured["expected_version"] = expected_version
            return {"match": "one", "run_id": "77", "name": "", "url": "", "head_sha": "", "status": ""}

        ns = self._testpypi_namespace(expected_version=None, version="0.10.0")
        with patch.object(release_mod, "_gh_dispatch_testpypi", side_effect=fake_dispatch):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_publish(ns)
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertEqual("0.10.0", captured["expected_version"])

    def test_testpypi_rejects_non_hex_source_sha(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import release as release_mod

        ns = self._testpypi_namespace(source_sha="not-a-sha")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                release_mod.cmd_release_publish(ns)


#: Repo-relative legacy mirror reference dir, as the sub-check prints it.
LEGACY_MIRROR_REL = ".claude/skills/mozyo-bridge-agent/references"


class ReleaseCheckDriftTest(unittest.TestCase):
    """Pin Redmine #10688: `mozyo-bridge release check drift` runs every
    pre-existing drift gate and strict-fails on any side.

    The unittest suite already gates each drift surface independently:
    - `CanonicalRendererTest::test_committed_templates_match_canonical_render`
      and `GovernedWorkflowCanonicalTest::test_both_governed_outputs_match_canonical_render`
      for `scaffold canonical --check`;
    - `PluginMarketplaceTest::test_plugin_skill_mirror_matches_canonical`
      and `test_sync_script_check_mode_*` for the plugin mirror;
    - `LegacyProjectSkillMirrorTest` for the legacy project Claude skill
      partial mirror (Redmine #14580).

    This class pins the *release helper* surface: the operator-facing
    command that bundles the checks into one call (mirroring the
    `release check tree` / `release check scaffold` / `release check
    artifact` pattern). A future helper edit that, for example, swallows
    a sub-check's non-zero exit and reports `result: clean` would slip
    past the per-surface tests but fails here.

    Redmine #14580 also pins a staging invariant: every sub-check's inputs
    must be staged into the temp repo. When `.claude/skills/` and the legacy
    sync script were absent from `SOURCE_TREE_PATHS`, the drift tests below
    still saw exit 1 — but partly because an unstaged gate blocked, not only
    because the mutation they injected did. Each drift test therefore asserts
    that the *other* gates report up to date, so a masked failure cannot pass
    for the wrong reason.
    """

    SOURCE_TREE_PATHS = (
        Path("src/mozyo_bridge"),
        Path("scripts/sync_plugin_skill.sh"),
        Path("scripts/sync_legacy_project_skill.sh"),
        Path("skills/mozyo-bridge-agent"),
        Path("plugins/mozyo-bridge-agent"),
        Path(".claude/skills"),
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

    def test_clean_tree_exits_zero_and_reports_all_checks(self) -> None:
        result, stdout, stderr = self._run_helper(ROOT)
        self.assertEqual(0, result, msg=stdout + stderr)
        # Every sub-check section header must appear so operators can
        # see what ran without re-reading the source.
        self.assertIn("scaffold canonical --check", stdout)
        self.assertIn("sync_plugin_skill.sh --check", stdout)
        self.assertIn("sync_legacy_project_skill.sh --check", stdout)
        # Every sub-check must report up-to-date on a clean tree.
        self.assertIn("AGENTS.md is up to date", stdout)
        self.assertIn("plugin skill mirror is up to date", stdout)
        self.assertIn("legacy project skill mirror is up to date", stdout)
        self.assertIn("result: clean", stdout)

    def test_staged_repo_is_clean_before_any_mutation(self) -> None:
        """The staging fixture itself must produce a clean drift result.

        Without this, a drift test's `assertEqual(1, result)` can pass
        because an unstaged gate blocked rather than because the injected
        mutation was detected — the exact vacuity Redmine #14580 hit when
        the new legacy-mirror gate landed against an incomplete
        `SOURCE_TREE_PATHS`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            result, stdout, stderr = self._run_helper(repo)
            self.assertEqual(0, result, msg=stdout + stderr)
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
            # The mirror checks must still have run AND still be clean, so
            # the exit 1 is attributable to the canonical mutation alone.
            self.assertIn("plugin skill mirror is up to date", stdout)
            self.assertIn("legacy project skill mirror is up to date", stdout)

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
            # the other defeats the bundled-helper purpose. The legacy
            # mirror stays clean, isolating the injected mutation.
            self.assertIn("scaffold canonical --check", stdout)
            self.assertIn("legacy project skill mirror is up to date", stdout)

    def test_legacy_project_mirror_drift_causes_strict_fail(self) -> None:
        """Redmine #14580: a canonical-only edit must block the release gate.

        This reproduces the confirmed defect's shape exactly — canonical
        moves, the legacy `.claude/skills/` partial mirror does not — and
        pins that `release check drift` now catches it instead of leaving
        detection to a full-suite run nobody ran before commit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            canonical = repo / "skills/mozyo-bridge-agent/references/workflow.md"
            canonical.write_text(
                canonical.read_text(encoding="utf-8") + "\nCANONICAL-ONLY EDIT\n",
                encoding="utf-8",
            )
            # Keep the plugin mirror in lockstep, so the ONLY drift left is
            # the legacy partial mirror. Otherwise this test would also pass
            # on the plugin gate's blocker and prove nothing new.
            plugin_mirror = (
                repo
                / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md"
            )
            plugin_mirror.write_text(
                canonical.read_text(encoding="utf-8"), encoding="utf-8"
            )

            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("legacy project skill mirror drift detected", stdout)
            self.assertIn("[F/content_drift]", stdout)
            self.assertIn("workflow.md", stdout)
            self.assertIn("result: blocker", stdout)
            # Recovery hint must be repo-root runnable, matching the plugin
            # gate's contract.
            self.assertIn("scripts/sync_legacy_project_skill.sh", stdout)
            self.assertIn("from the repo root", stdout)
            # The other gates ran and stayed clean.
            self.assertIn("plugin skill mirror is up to date", stdout)
            self.assertNotIn("scaffold canonical drift detected", stdout)

    def test_legacy_mirror_gate_recovers_after_running_the_sync(self) -> None:
        """Running the sync script turns the blocker back into `clean`.

        Pins the round trip the Acceptance names: canonical-only mutation
        goes red, the documented recovery command makes it green again.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            canonical = repo / "skills/mozyo-bridge-agent/references/workflow.md"
            canonical.write_text(
                canonical.read_text(encoding="utf-8") + "\nCANONICAL-ONLY EDIT\n",
                encoding="utf-8",
            )
            plugin_mirror = (
                repo
                / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md"
            )
            plugin_mirror.write_text(
                canonical.read_text(encoding="utf-8"), encoding="utf-8"
            )

            before, stdout_before, _ = self._run_helper(repo)
            self.assertEqual(1, before, msg=stdout_before)

            recovery = subprocess.run(
                ["sh", str(repo / "scripts/sync_legacy_project_skill.sh")],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, recovery.returncode, msg=recovery.stderr)

            after, stdout_after, stderr_after = self._run_helper(repo)
            self.assertEqual(0, after, msg=stdout_after + stderr_after)
            self.assertIn("legacy project skill mirror is up to date", stdout_after)

    def test_legacy_mirror_blocker_names_a_recovery_that_fits_the_drift(self) -> None:
        """Review j#90322 F1: per-gate recovery, not one line for every class.

        An unpinned mirrored reference is the one legacy drift class the sync
        refuses to resolve. A blocker bullet that just says "rerun the sync"
        sends the operator to a command that exits 1 on the same tree.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            (
                repo / ".claude/skills/mozyo-bridge-agent/references/unpinned.md"
            ).write_text("smuggled in\n", encoding="utf-8")
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("[D/unpinned_entry]", stdout)
            self.assertIn("unpinned.md", stdout)
            self.assertIn("result: blocker", stdout)
            # The bullet must say the sync will NOT clear this class.
            self.assertIn("refuses while one is present", stdout)
            self.assertIn("never deletes it for you", stdout)
            # The plugin gate keeps its own, still-correct recovery.
            self.assertIn("plugin skill mirror is up to date", stdout)

    def test_legacy_mirror_dangling_symlink_is_a_release_blocker(self) -> None:
        """Review j#90342 R2-F1 condition 4: the gate must see it too.

        A dangling `unpinned.md` symlink previously passed the sub-check's
        file-set audit, so `release check drift` reported `result: clean` with
        an unpinned entry in the mirror.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            (
                repo / ".claude/skills/mozyo-bridge-agent/references/unpinned.md"
            ).symlink_to("missing-target")
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("[D/unpinned_entry]", stdout)
            self.assertIn("unpinned.md", stdout)
            self.assertIn("result: blocker", stdout)
            self.assertIn("never deletes it for you", stdout)
            self.assertIn("plugin skill mirror is up to date", stdout)

    def test_legacy_mirror_invalid_entry_type_is_a_release_blocker(self) -> None:
        """Review j#90342 R3-F1 condition 4: the gate must see bad topology.

        A directory under a pinned reference name is not something the sync can
        resolve, so `release check drift` has to report it rather than leave it
        to whoever next runs the script.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            pinned = (
                repo / ".claude/skills/mozyo-bridge-agent/references/safety.md"
            )
            pinned.unlink()
            pinned.mkdir()
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("[E/entry_not_regular]", stdout)
            self.assertIn("safety.md", stdout)
            self.assertIn("result: blocker", stdout)
            self.assertIn("plugin skill mirror is up to date", stdout)

    def test_legacy_mirror_non_md_entry_is_a_release_blocker(self) -> None:
        """Review j#90378 R4-F1 condition 3: the gate needs the full domain.

        `unpinned.txt`, a dotfile and a stale temp all sat in the mirror with
        `release check drift` reporting `result: clean`, because the sub-check
        audited `*.md` only and a shell glob also skips hidden entries.
        """
        for name in ("unpinned.txt", ".unpinned.md"):
            with self.subTest(entry=name):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = self._stage_repo(Path(tmp) / "repo")
                    (
                        repo / ".claude/skills/mozyo-bridge-agent/references" / name
                    ).write_text("smuggled\n", encoding="utf-8")
                    result, stdout, _stderr = self._run_helper(repo)
                    self.assertEqual(1, result)
                    self.assertIn("[D/unpinned_entry]", stdout)
                    self.assertIn(name, stdout)
                    self.assertIn("result: blocker", stdout)
                    self.assertIn("plugin skill mirror is up to date", stdout)

    def test_legacy_mirror_aliased_canonical_source_is_a_release_blocker(self) -> None:
        """Review j#90378 R4-F2 condition 2: the source side gates too.

        `-f "$src/$name"` follows symlinks, so a canonical reference pointed at
        an external file was accepted and its bytes copied into the mirror
        while the gate reported clean.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            external = repo / "external-body.md"
            external.write_text("EXTERNAL BODY\n", encoding="utf-8")
            source = repo / "skills/mozyo-bridge-agent/references/safety.md"
            source.unlink()
            source.symlink_to(external)

            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("[B/source_symlink]", stdout)
            self.assertIn("result: blocker", stdout)

    def test_legacy_mirror_unreadable_state_is_a_typed_release_blocker(self) -> None:
        """Review j#90418 R6-F3: the gate must get a disposition, not a crash.

        A mode-000 canonical file raised out of the sub-check, so the bullet
        telling the operator to "follow the disposition the sub-check printed"
        pointed at a traceback.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            target = repo / "skills/mozyo-bridge-agent/references/safety.md"
            target.chmod(0o000)
            try:
                result, stdout, _stderr = self._run_helper(repo)
            finally:
                # Restore inside the temp dir's lifetime; an addCleanup would
                # run after it is gone.
                target.chmod(0o644)

            self.assertEqual(1, result)
            self.assertNotIn("Traceback", stdout)
            self.assertIn("[B/source_unreadable]", stdout)
            self.assertIn("Restore read access", stdout)
            self.assertIn("result: blocker", stdout)
            # The legacy bullet must still name its own gate. The plugin gate
            # legitimately also trips here — the unreadable file is in the
            # canonical body both mirrors read — so its state is not asserted.
            self.assertIn("legacy project skill mirror drift detected", stdout)

    def test_missing_legacy_sync_script_is_release_blocker(self) -> None:
        """A deleted legacy sync script must block, not silently pass.

        The plugin gate already pins this; the legacy gate needs its own
        assertion because a `not script.is_file()` branch that `return`s
        without appending a blocker reads as success.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            (repo / "scripts/sync_legacy_project_skill.sh").unlink()
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("missing sync script", stdout)
            self.assertIn("legacy project skill mirror sync script missing", stdout)
            self.assertIn("result: blocker", stdout)

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
