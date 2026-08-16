"""Redmine #15527 — `scaffold apply --backend` declares the terminal backend on day one.

Measured before this issue: the scaffold had no backend surface. A freshly scaffolded
target always selected the tmux default, and adopting herdr meant hand-writing
`terminal_transport.backend: herdr` into a config the scaffold only shipped as an
`.example`. The flag closes that gap without touching the default: omitted, nothing
changes; given, one operator-owned `config.yaml` is written outside the manifest.

Two review rounds shaped how this suite is built (j#106056):

- finding_2: the first version tolerated `CommandAbort`, so under the canonical
  isolated runner — whose task-local home has no installed preset — every success-path
  assertion silently skipped and the suite was green while proving nothing. Each case
  now installs the packaged presets into its own temp home and passes it as
  ``--home``, and a completed run is MANDATORY: an abort is a failure, not a return.
- finding_1: `Path.exists()` answers False for a DANGLING symlink, and following one
  on write created a file OUTSIDE the target (reproduced). The command now treats any
  directory entry — symlink included — as an existing declaration, and the write is an
  `O_EXCL` exclusive create so even a link planted after the check cannot redirect it.
  ``SymlinkEntryIsAnExistingConfigTest`` pins both the refusal and the absence of the
  outside file.

The load-bearing assertions delegate to the runtime's own reader
(`herdr_backend_selected_for`) rather than re-parsing the YAML — the same anti-drift
rule as #15508/#15520: what matters is what the send path will actually select.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.commands_docs_scaffold import (  # noqa: E402,E501
    cmd_scaffold_apply,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.scaffold_backend_declaration import (  # noqa: E402,E501
    backend_declaration,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E402,E501
    herdr_backend_selected_for,
)
from mozyo_bridge.scaffold.rules import (  # noqa: E402
    install_rules,
    resolve_rules_store,
    scaffold_status,
)
from mozyo_bridge.shared.errors import CommandAbort  # noqa: E402


class BackendDeclarationDecisionTest(unittest.TestCase):
    """The pure decision: exact path, refusal on operator config, closed choices."""

    TARGET = Path("/repo")

    def test_the_declaration_targets_the_repo_local_config(self) -> None:
        decided = backend_declaration(self.TARGET, "herdr", config_exists=False)

        self.assertTrue(decided.ok)
        self.assertEqual(self.TARGET / ".mozyo-bridge/config.yaml", decided.path)
        self.assertIn("backend: herdr", decided.content)

    def test_an_existing_config_refuses_and_names_the_file(self) -> None:
        decided = backend_declaration(self.TARGET, "herdr", config_exists=True)

        self.assertFalse(decided.ok)
        self.assertIn(str(decided.path), decided.refusal)
        self.assertIn("operator-owned", decided.refusal)

    def test_an_unknown_backend_refuses(self) -> None:
        self.assertFalse(
            backend_declaration(self.TARGET, "screen", config_exists=False).ok
        )


class _AppliedScaffoldCase(unittest.TestCase):
    """Command-level cases run against a home that REALLY has the presets.

    The canonical isolated runner's task-local home carries no installed preset, so a
    suite that tolerates the resulting abort proves nothing there (review j#106056
    finding_2). Installing into a per-test temp home makes the success path
    unconditional in every environment.
    """

    def _home(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve() / "mozyo-home"
        install_rules(store=resolve_rules_store(home=home))
        return home

    def _repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        return repo

    def _apply(self, target: Path, home: Path, *, backend=None, dry_run: bool = False):
        args = argparse.Namespace(
            preset="redmine-governed",
            repo=str(target),
            dry_run=dry_run,
            backup=False,
            force=False,
            home=str(home),
            repo_local=False,
            backend=backend,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_scaffold_apply(args)
        self.assertEqual(0, code)
        return out.getvalue()


class ScaffoldApplyBackendFlagTest(_AppliedScaffoldCase):
    """Drive the real command; judge with the runtime's own backend reader."""

    def test_backend_herdr_is_what_the_send_path_selects(self) -> None:
        repo = self._repo()

        output = self._apply(repo, self._home(), backend="herdr")

        self.assertTrue(herdr_backend_selected_for(repo))
        self.assertIn("(backend declaration; not in manifest)", output)

    def test_backend_tmux_writes_a_declaration_that_still_selects_tmux(self) -> None:
        repo = self._repo()

        self._apply(repo, self._home(), backend="tmux")

        self.assertTrue((repo / ".mozyo-bridge" / "config.yaml").exists())
        self.assertFalse(herdr_backend_selected_for(repo))

    def test_omitting_the_flag_writes_no_config_and_no_extra_line(self) -> None:
        repo = self._repo()

        output = self._apply(repo, self._home())

        self.assertFalse((repo / ".mozyo-bridge" / "config.yaml").exists())
        self.assertNotIn("backend declaration", output)

    def test_an_existing_config_fails_closed_with_zero_writes(self) -> None:
        repo = self._repo()
        config_dir = repo / ".mozyo-bridge"
        config_dir.mkdir()
        original = "terminal_transport:\n  backend: herdr\n"
        (config_dir / "config.yaml").write_text(original)

        with self.assertRaises(CommandAbort):
            self._apply(repo, self._home(), backend="tmux")

        self.assertEqual(original, (config_dir / "config.yaml").read_text())
        # Refused BEFORE the scaffold write: no manifest either.
        self.assertFalse((config_dir / "scaffold.json").exists())

    def test_dry_run_reports_but_never_writes(self) -> None:
        repo = self._repo()

        output = self._apply(repo, self._home(), backend="herdr", dry_run=True)

        self.assertFalse((repo / ".mozyo-bridge" / "config.yaml").exists())
        self.assertIn("would write", output)
        self.assertIn("(backend declaration; not in manifest)", output)


class SymlinkEntryIsAnExistingConfigTest(_AppliedScaffoldCase):
    """Review j#106056 finding_1 — the escape this suite exists to keep closed.

    A dangling symlink at the config path answered False to `exists()`, so the
    pre-fix command created the file at the symlink's target, OUTSIDE the repo
    (reproduced live before the fix). Any entry — symlink included, dangling or
    not — is an existing declaration and refuses.
    """

    def test_a_dangling_symlink_refuses_and_creates_nothing_outside(self) -> None:
        repo = self._repo()
        outside_dir = self._repo()  # second temp tree standing in for "anywhere else"
        outside = outside_dir / "evil.yaml"
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").symlink_to(outside)

        with self.assertRaises(CommandAbort) as caught:
            self._apply(repo, self._home(), backend="herdr")

        self.assertFalse(outside.exists(), "the write escaped the target")
        # The entry itself is untouched: still a symlink, still dangling.
        self.assertTrue((repo / ".mozyo-bridge" / "config.yaml").is_symlink())
        # The PREFLIGHT refusal, not the O_EXCL race backstop: both layers refuse,
        # but only the preflight tells the operator the config is theirs to edit.
        # Pinning the wording is what makes an `exists()` regression — where the
        # dangling link slips past the check and only the backstop fires — visible
        # to this suite instead of an equivalent mutant.
        self.assertIn("operator-owned", str(caught.exception))

    def test_a_symlink_to_an_existing_file_refuses_without_touching_it(self) -> None:
        repo = self._repo()
        outside_dir = self._repo()
        outside = outside_dir / "real.yaml"
        outside.write_text("terminal_transport:\n  backend: tmux\n")
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").symlink_to(outside)

        with self.assertRaises(CommandAbort):
            self._apply(repo, self._home(), backend="herdr")

        self.assertEqual(
            "terminal_transport:\n  backend: tmux\n", outside.read_text()
        )


class DeclarationStaysOutOfTheManifestTest(_AppliedScaffoldCase):
    """`scaffold status` must stay clean after the operator edits the config."""

    def test_status_is_clean_before_and_after_editing_the_config(self) -> None:
        repo = self._repo()
        home = self._home()

        self._apply(repo, home, backend="herdr")

        self.assertTrue(scaffold_status(repo, home=home)["clean"])
        # The operator edit that must NOT show up as drift.
        config = repo / ".mozyo-bridge" / "config.yaml"
        config.write_text(config.read_text() + "# operator note\n")
        self.assertTrue(scaffold_status(repo, home=home)["clean"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
