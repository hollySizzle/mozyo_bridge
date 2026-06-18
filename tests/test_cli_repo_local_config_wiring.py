"""Repo-local YAML config -> CLI composition wiring tests (Redmine #12191).

Pins the staged wiring that connects the repo-local config loader (#12190) and
its schema (#12189) to the ``mozyo-bridge`` CLI composition entrypoint:

- **config absent / default is behavior-preserving.** ``build_parser()`` with no
  config, ``build_parser(RepoLocalConfig.default())``, and a real repo that has
  no ``.mozyo-bridge/config.yaml`` all produce the same full top-level
  subcommand tree — a missing config never changes the default CLI.
- **config present may disable only a non-mandatory CLI family.** A real config
  file disabling an optional family drops exactly its subcommands; a config that
  tries to disable a mandatory (core / authority-bearing) family fails closed.
- **broken / rejected config fails closed with actionable text.** ``main()``
  turns a parse / schema / family-resolution failure into a single actionable
  stderr line and the conventional ``2`` exit code — never a raw traceback, and
  never a silent fall-through to the default CLI — for *every* invocation,
  including ``--version``.
- **the broader config (presentation surface) is read without disturbing the
  parser.** Selecting a built-in presentation surface loads cleanly and leaves
  the CLI subcommand tree unchanged (its runtime resolution is a later stage).

This file exercises only parser composition and the ``main`` fail-closed seam;
no tmux, network, or command handler runs.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import cli as cli_module
from mozyo_bridge.application.cli import _root_repo_override, build_parser, main
from mozyo_bridge.application.repo_local_config_loader import (
    CONFIG_FILE_RELPATH,
    load_repo_local_config,
)
from mozyo_bridge.domain.module_registry import (
    CliCompositionConfig,
    ModuleRegistryError,
)
from mozyo_bridge.domain.repo_local_config import (
    PresentationSelectionConfig,
    RepoLocalConfig,
    RepoLocalConfigError,
)


def _top_level_subcommands(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.keys())
    raise AssertionError("no subparsers action on top-level parser")


def _write_config(repo_root: Path, body: str) -> None:
    config_path = repo_root / CONFIG_FILE_RELPATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body, encoding="utf-8")


class ConfigAbsentIsBehaviorPreservingTest(unittest.TestCase):
    """A missing / default config must reproduce the full default CLI exactly."""

    def test_default_config_matches_no_config_argument(self) -> None:
        # The new optional ``config`` parameter must default to the pre-#12191
        # behavior: no config and an explicit default config compose the same
        # full subcommand tree, in the same order.
        self.assertEqual(
            _top_level_subcommands(build_parser()),
            _top_level_subcommands(build_parser(RepoLocalConfig.default())),
        )

    def test_repo_without_config_file_resolves_to_full_cli(self) -> None:
        # A real repo root with no ``.mozyo-bridge/config.yaml`` loads the
        # behavior-preserving default, so the composed CLI equals the no-config
        # CLI — the missing-file path never alters the help/subcommand tree.
        with tempfile.TemporaryDirectory() as tmp:
            config = load_repo_local_config(Path(tmp))
        self.assertEqual(config, RepoLocalConfig.default())
        self.assertEqual(
            _top_level_subcommands(build_parser(config)),
            _top_level_subcommands(build_parser()),
        )


class ConfigPresentSelectsOptionalFamilyTest(unittest.TestCase):
    """A present config may disable only a non-mandatory CLI family."""

    def test_config_file_disables_an_optional_family_end_to_end(self) -> None:
        # Round-trips a real file through the #12190 loader into the parser:
        # disabling the optional ``agents`` family drops exactly its subcommand
        # while every mandatory family stays present.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_config(
                repo_root,
                "version: 1\ncli:\n  disabled:\n    - agents\n",
            )
            config = load_repo_local_config(repo_root)

        self.assertEqual(config.cli, CliCompositionConfig(disabled=frozenset({"agents"})))
        names = _top_level_subcommands(build_parser(config))
        self.assertNotIn("agents", names)
        # Mandatory / unrelated families are untouched.
        self.assertIn("handoff", names)
        self.assertIn("status", names)
        self.assertIn("release", names)

    def test_presentation_surface_loads_without_changing_parser(self) -> None:
        # The broader config (here a built-in presentation surface) is read by
        # the loader, but selecting it is parser-neutral: the CLI subcommand
        # tree is identical to the default. Presentation runtime resolution is a
        # later staged surface; this only pins that reading it never disturbs
        # the composition entrypoint.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_config(repo_root, "presentation:\n  surface: text\n")
            config = load_repo_local_config(repo_root)

        self.assertEqual(
            config.presentation, PresentationSelectionConfig(surface="text")
        )
        self.assertEqual(
            _top_level_subcommands(build_parser(config)),
            _top_level_subcommands(build_parser()),
        )

    def test_config_disabling_mandatory_family_fails_closed_at_compose(self) -> None:
        # Shape validation accepts the family id, but resolving it against the
        # registry rejects disabling a mandatory family — build_parser must
        # surface that as ModuleRegistryError, not silently compose it away.
        config = RepoLocalConfig(cli=CliCompositionConfig(disabled=frozenset({"handoff"})))
        with self.assertRaises(ModuleRegistryError):
            build_parser(config)


class MainFailsClosedTest(unittest.TestCase):
    """``main`` converts every repo-local-config failure into actionable text."""

    def _run_main(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        old_argv = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stderr(stderr):
                code = main()
        finally:
            sys.argv = old_argv
        return code, stderr.getvalue()

    def test_invalid_config_fails_closed_with_actionable_text(self) -> None:
        def _boom(*_args, **_kwargs):
            raise RepoLocalConfigError("could not parse config: bad yaml")

        original = cli_module.load_repo_local_config
        cli_module.load_repo_local_config = _boom  # type: ignore[assignment]
        try:
            code, err = self._run_main(["mozyo-bridge", "status"])
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", err)
        self.assertIn(str(CONFIG_FILE_RELPATH), err)
        # The text tells the user how to recover (fix or remove the file).
        self.assertIn("remove", err)
        self.assertIn("bad yaml", err)

    def test_mandatory_family_disable_fails_closed_through_main(self) -> None:
        bad = RepoLocalConfig(cli=CliCompositionConfig(disabled=frozenset({"handoff"})))

        original = cli_module.load_repo_local_config
        cli_module.load_repo_local_config = lambda *a, **k: bad  # type: ignore[assignment]
        try:
            code, err = self._run_main(["mozyo-bridge", "status"])
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", err)

    def test_broken_config_fails_closed_even_for_version_flag(self) -> None:
        # Fail-closed is global: a broken config blocks even ``--version`` rather
        # than letting argparse exit 0, so a misconfigured repo can never run any
        # subset of the CLI as if the config were valid.
        def _boom(*_args, **_kwargs):
            raise RepoLocalConfigError("schema violation")

        original = cli_module.load_repo_local_config
        cli_module.load_repo_local_config = _boom  # type: ignore[assignment]
        try:
            code, err = self._run_main(["mozyo-bridge", "--version"])
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", err)

    def test_default_config_lets_version_flag_run(self) -> None:
        # With a behavior-preserving default config, ``main`` composes and parses
        # normally: ``--version`` reaches argparse and exits 0 (SystemExit), so
        # the wiring is transparent when the config is absent/default.
        original = cli_module.load_repo_local_config
        cli_module.load_repo_local_config = (
            lambda *a, **k: RepoLocalConfig.default()
        )  # type: ignore[assignment]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                old_argv = sys.argv
                sys.argv = ["mozyo-bridge", "--version"]
                try:
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                finally:
                    sys.argv = old_argv
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]
        self.assertEqual(ctx.exception.code, 0)


class RootRepoOverrideExtractionTest(unittest.TestCase):
    """``_root_repo_override`` reads only the root-level ``--repo`` (review j#60857)."""

    def test_root_repo_extracted_before_subcommand(self) -> None:
        self.assertEqual(_root_repo_override(["--repo", "/T", "--help"]), "/T")
        self.assertEqual(_root_repo_override(["--repo=/T", "status"]), "/T")

    def test_root_repo_after_other_root_options(self) -> None:
        # A value-taking root option before --repo must not swallow it.
        self.assertEqual(
            _root_repo_override(["--session", "s", "--repo", "/T", "status"]), "/T"
        )
        self.assertEqual(
            _root_repo_override(["--no-attach", "--repo", "/T", "status"]), "/T"
        )

    def test_subcommand_local_repo_is_ignored(self) -> None:
        # The root --repo wins; a --repo after the subcommand applies to that
        # command and must not change which families compose.
        self.assertEqual(
            _root_repo_override(["--repo", "/T", "status", "--repo", "/Y"]), "/T"
        )

    def test_only_subcommand_local_repo_is_not_a_root_override(self) -> None:
        self.assertIsNone(_root_repo_override(["status", "--repo", "/Y"]))
        self.assertIsNone(_root_repo_override(["handoff", "--repo", "/Y", "send"]))

    def test_absent_repo_is_none(self) -> None:
        for argv in ([], ["--version"], ["--help"], ["status"], ["--no-attach"]):
            self.assertIsNone(_root_repo_override(argv))


class RootRepoOverrideDrivesConfigSourceTest(unittest.TestCase):
    """An explicit root ``--repo`` selects the repo-local config source (j#60857)."""

    def test_target_repo_config_drives_composition(self) -> None:
        # --repo <target> must read <target>/.mozyo-bridge/config.yaml: a target
        # that disables 'agents' drops it from the composed parser, even though
        # the cwd has no such config.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_config(target, "version: 1\ncli:\n  disabled:\n    - agents\n")
            repo = _root_repo_override(["--repo", str(target), "--help"])
            config = load_repo_local_config(repo)
        names = _top_level_subcommands(build_parser(config))
        self.assertNotIn("agents", names)
        self.assertIn("handoff", names)

    def test_main_fails_closed_on_broken_target_config(self) -> None:
        # cwd/MOZYO_REPO has no config; --repo points at a *broken* one. main
        # must fail closed on the target config, proving --repo is the config
        # source (a cwd-only loader would have seen the clean cwd and passed).
        with tempfile.TemporaryDirectory() as clean_cwd, \
                tempfile.TemporaryDirectory() as broken_target:
            _write_config(Path(broken_target), "cli: [unterminated\n")
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"MOZYO_REPO": clean_cwd}):
                with contextlib.redirect_stderr(stderr):
                    code = main(["--repo", broken_target, "status"])
        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", stderr.getvalue())

    def test_main_root_repo_overrides_broken_cwd_config(self) -> None:
        # The inverse: cwd/MOZYO_REPO config is broken, but --repo points at a
        # clean target. main must use the target and proceed to argparse (here
        # --version exits 0), not fail on the cwd config.
        with tempfile.TemporaryDirectory() as broken_cwd, \
                tempfile.TemporaryDirectory() as clean_target:
            _write_config(Path(broken_cwd), "cli: [unterminated\n")
            with mock.patch.dict(os.environ, {"MOZYO_REPO": broken_cwd}):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        main(["--repo", clean_target, "--version"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
