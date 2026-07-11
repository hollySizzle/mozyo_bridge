"""Bare-`mozyo` target-root selection + reload-at-launch tests (#13497 j#74936 / j#74934).

The onboarding gate must not hardcode the cwd: it honors `--repo` / `MOZYO_REPO`,
launches an adopted ancestor when invoked from a subdirectory, and otherwise
targets the canonical cwd itself (never an incidental unadopted ancestor). The
completion launch reloads the *selected root's* config so a freshly written herdr
config is honored rather than a stale pre-adoption tmux selection.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.application import cli

_HERDR_CONFIG = "version: 1\nterminal_transport:\n  backend: herdr\n"


class SelectTargetRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self._cwd = os.getcwd()
        self.addCleanup(lambda: os.chdir(self._cwd))
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ.pop("MOZYO_REPO", None)

    def _adopt(self, root: Path) -> None:
        d = root / ".mozyo-bridge"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(_HERDR_CONFIG, encoding="utf-8")

    def test_explicit_repo_wins(self):
        target = self.base / "explicit"
        target.mkdir()
        root = cli._select_bare_target_root(["--repo", str(target)])
        self.assertEqual(root, target.resolve())

    def test_mozyo_repo_env_used_without_override(self):
        target = self.base / "env"
        target.mkdir()
        os.environ["MOZYO_REPO"] = str(target)
        root = cli._select_bare_target_root([])
        self.assertEqual(root, target.resolve())

    def test_adopted_ancestor_from_subdirectory(self):
        self._adopt(self.base)
        sub = self.base / "pkg" / "deep"
        sub.mkdir(parents=True)
        os.chdir(sub)
        root = cli._select_bare_target_root([])
        self.assertEqual(root, self.base)

    def test_incidental_unadopted_ancestor_is_not_adopted(self):
        # A plain (non-mozyo) marker ancestor must not be silently adopted; the
        # fresh target is the canonical cwd itself.
        (self.base / ".git").mkdir()
        sub = self.base / "nested"
        sub.mkdir()
        os.chdir(sub)
        root = cli._select_bare_target_root([])
        self.assertEqual(root, sub.resolve())

    def test_explicit_repo_beats_env_and_cwd(self):
        self._adopt(self.base)  # cwd is adopted...
        os.chdir(self.base)
        other = self.base / "other"
        other.mkdir()
        os.environ["MOZYO_REPO"] = str(self.base / "env")
        root = cli._select_bare_target_root(["--repo", str(other)])
        self.assertEqual(root, other.resolve())  # ...but --repo still wins


class BackendReloadTest(unittest.TestCase):
    """Reload-at-invocation resolves the *written* backend (j#74934)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.args = mock.Mock()

    def test_written_herdr_config_takes_herdr_path(self):
        (self.root / ".mozyo-bridge").mkdir()
        (self.root / ".mozyo-bridge" / "config.yaml").write_text(
            _HERDR_CONFIG, encoding="utf-8"
        )
        with mock.patch(
            "mozyo_bridge.application.herdr_launch_command.cmd_mozyo_herdr",
            return_value=7,
        ) as herdr, mock.patch.object(cli, "cmd_mozyo", return_value=3) as tmux:
            rc = cli._backend_aware_launch(self.args, self.root)
        self.assertEqual(rc, 7)
        herdr.assert_called_once()
        tmux.assert_not_called()

    def test_absent_config_takes_tmux_path(self):
        # No config written (fresh/default) → the byte-invariant tmux path.
        with mock.patch(
            "mozyo_bridge.application.herdr_launch_command.cmd_mozyo_herdr",
            return_value=7,
        ) as herdr, mock.patch.object(cli, "cmd_mozyo", return_value=3) as tmux:
            rc = cli._backend_aware_launch(self.args, self.root)
        self.assertEqual(rc, 3)
        tmux.assert_called_once()
        herdr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
