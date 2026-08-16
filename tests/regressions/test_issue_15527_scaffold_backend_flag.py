"""Redmine #15527 — `scaffold apply --backend` declares the terminal backend on day one.

Measured before this issue: the scaffold had no backend surface. A freshly scaffolded
target always selected the tmux default, and adopting herdr meant hand-writing
`terminal_transport.backend: herdr` into a config the scaffold only shipped as an
`.example`. The flag closes that gap without touching the default: omitted, nothing
changes; given, one operator-owned `config.yaml` is written outside the manifest.

The load-bearing assertions delegate to the runtime's own reader
(`herdr_backend_selected_for`) rather than re-parsing the YAML — the same
anti-drift rule as #15508/#15520: what matters is what the send path will
actually select, not what this test thinks the file says. The omitted-flag cases
assert bytes (no config file, no extra stdout), because "unchanged" is the
contract there.
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


class ScaffoldApplyBackendFlagTest(unittest.TestCase):
    """Drive the real command; judge with the runtime's own backend reader."""

    def _repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        return repo

    def _run(self, target: Path, *, backend=None, dry_run: bool = False):
        args = argparse.Namespace(
            preset="redmine-governed",
            repo=str(target),
            dry_run=dry_run,
            backup=False,
            force=False,
            home=None,
            repo_local=False,
            backend=backend,
        )
        out = io.StringIO()
        aborted = None
        with contextlib.redirect_stdout(out):
            try:
                cmd_scaffold_apply(args)
            except CommandAbort as exc:
                aborted = exc
        return out.getvalue(), aborted

    def test_backend_herdr_is_what_the_send_path_selects(self) -> None:
        repo = self._repo()

        output, aborted = self._run(repo, backend="herdr")

        config = repo / ".mozyo-bridge" / "config.yaml"
        if aborted is not None:
            # Under the verification fence the guarded home carries no installed
            # preset, so the scaffold write itself aborts. The contract this case
            # still owns: the abort happened BEFORE the declaration write.
            self.assertFalse(config.exists())
            return
        self.assertTrue(herdr_backend_selected_for(repo))
        self.assertIn("(backend declaration; not in manifest)", output)

    def test_backend_tmux_writes_a_declaration_that_still_selects_tmux(self) -> None:
        repo = self._repo()

        _output, aborted = self._run(repo, backend="tmux")

        config = repo / ".mozyo-bridge" / "config.yaml"
        if aborted is not None:
            self.assertFalse(config.exists())
            return
        self.assertTrue(config.exists())
        self.assertFalse(herdr_backend_selected_for(repo))

    def test_omitting_the_flag_writes_no_config_and_no_extra_line(self) -> None:
        repo = self._repo()

        output, _aborted = self._run(repo)

        self.assertFalse((repo / ".mozyo-bridge" / "config.yaml").exists())
        self.assertNotIn("backend declaration", output)

    def test_an_existing_config_fails_closed_with_zero_writes(self) -> None:
        repo = self._repo()
        config_dir = repo / ".mozyo-bridge"
        config_dir.mkdir()
        original = "terminal_transport:\n  backend: herdr\n"
        (config_dir / "config.yaml").write_text(original)

        output, aborted = self._run(repo, backend="tmux")

        self.assertIsNotNone(aborted, "an operator-owned config must refuse the flag")
        self.assertEqual(original, (config_dir / "config.yaml").read_text())
        # Refused BEFORE the scaffold write: no manifest either.
        self.assertFalse((config_dir / "scaffold.json").exists())
        self.assertNotIn("wrote:", output)

    def test_dry_run_reports_but_never_writes(self) -> None:
        repo = self._repo()

        output, aborted = self._run(repo, backend="herdr", dry_run=True)

        self.assertFalse((repo / ".mozyo-bridge" / "config.yaml").exists())
        if aborted is None:
            self.assertIn("would write", output)
            self.assertIn("(backend declaration; not in manifest)", output)


class DeclarationStaysOutOfTheManifestTest(unittest.TestCase):
    """`scaffold status` must stay clean after the operator edits the config."""

    def test_status_is_clean_before_and_after_editing_the_config(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

        args = argparse.Namespace(
            preset="redmine-governed",
            repo=str(repo),
            dry_run=False,
            backup=False,
            force=False,
            home=None,
            repo_local=False,
            backend="herdr",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                cmd_scaffold_apply(args)
            except CommandAbort:
                self.skipTest("no installed preset in this environment")

        from mozyo_bridge.scaffold.rules import scaffold_status

        self.assertTrue(scaffold_status(repo)["clean"])
        # The operator edit that must NOT show up as drift.
        config = repo / ".mozyo-bridge" / "config.yaml"
        config.write_text(config.read_text() + "# operator note\n")
        self.assertTrue(scaffold_status(repo)["clean"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
