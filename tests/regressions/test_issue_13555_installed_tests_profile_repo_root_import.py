"""Regression pin: installed ``tests profile`` resolves repo-root ``tests.*``.

Redmine #13555 (Bug #13556, Test #13557). The main CI **full lane** runs
``mozyo-bridge tests profile`` from a fresh ``pip install .`` — an installed
console-script entry point, which (unlike ``python -m unittest``) does *not* put
the invocation cwd (the repo root) on ``sys.path``. Repo-root test packages that
do ``from tests.support ...`` / ``from tests.unit ...`` therefore failed to
import at collection with ``ModuleNotFoundError: No module named 'tests'`` across
the whole Python 3.10–3.13 matrix (2 collection errors, run ``29129232080``).

These tests reproduce the installed-path condition in isolation — a self-contained
fake repo whose ``test*.py`` files cross-import a sibling ``support`` package —
with the repo root deliberately kept *off* ``sys.path`` (a unique top-level
package name, never the real ``tests``, so nothing leaks between suites). They
pin both halves of the contract:

- **the defect** — plain ``unittest.discover`` under the installed-path condition
  raises the ``No module named ...`` collection error; and
- **the fix** — ``cmd_tests_profile`` bootstraps the repo root onto ``sys.path``
  for discovery, so the same cross-package import resolves, the suite verdict /
  runtime summary are unchanged, discovered dotted module names / test IDs stay
  relative to ``top_level_dir`` (``scenarios.test_probe``, *not*
  ``<pkg>.scenarios.test_probe``), and ``sys.path`` is restored afterwards.

Characterization only; the handler lives in
``src/mozyo_bridge/e_150_quality_architecture/f_150_ci_verification/application``.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_runtime import (  # noqa: E402
    cmd_tests_profile,
)

# Unique package names: never ``tests`` / ``scenarios`` (the real suite's
# namespaces). ``_PKG`` is the top-level package the cross-import goes through;
# ``_SUBPKG`` is the discovered sub-package (== top_level_dir-relative module
# prefix). Both are unique so the in-process fake discovery can never collide in
# ``sys.modules`` with the real suite or between this file's own test methods.
_PKG = "probe_installed_pkg_13555"
_SUBPKG = "probe_scenarios_13555"


class InstalledTestsProfileRepoRootImportTest(unittest.TestCase):
    def _write_fake_repo(self) -> Path:
        """A minimal repo whose scenario test cross-imports a sibling package.

        Layout mirrors the real failure: a scenario ``test*.py`` that resolves a
        helper via the *top-level package* (``from <pkg>.support...``), which only
        works when the repo root (the package's parent) is importable.
        """
        tmp = Path(tempfile.mkdtemp(prefix="mzb13555_"))
        self.addCleanup(self._cleanup_repo, tmp)
        pkg = tmp / _PKG
        (pkg / "support").mkdir(parents=True)
        (pkg / _SUBPKG).mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "support" / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "support" / "helper.py").write_text(
            "PROBE_MARKER = 'repo-root-import-ok'\n", encoding="utf-8"
        )
        (pkg / _SUBPKG / "__init__.py").write_text("", encoding="utf-8")
        # Cross-package import through the top-level package — the exact shape
        # that fails when the repo root is not on sys.path.
        (pkg / _SUBPKG / "test_probe.py").write_text(
            "import unittest\n"
            f"from {_PKG}.support.helper import PROBE_MARKER\n\n\n"
            "class ProbeScenario(unittest.TestCase):\n"
            "    def test_marker_resolves(self) -> None:\n"
            "        self.assertEqual(PROBE_MARKER, 'repo-root-import-ok')\n\n"
            "    def test_second_case(self) -> None:\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        return tmp

    def _cleanup_repo(self, tmp: Path) -> None:
        import shutil

        # Drop any fake-package modules discovery imported, and any path entries
        # unittest/our bootstrap may have left, so suites stay isolated. Both the
        # top-level package and the discovered sub-package namespace are purged.
        stale = [
            n
            for n in sys.modules
            if n in (_PKG, _SUBPKG)
            or n.startswith(_PKG + ".")
            or n.startswith(_SUBPKG + ".")
        ]
        for name in stale:
            del sys.modules[name]
        for entry in (str(tmp), str(tmp / _PKG)):
            while entry in sys.path:
                sys.path.remove(entry)
        shutil.rmtree(tmp, ignore_errors=True)

    def _args(self, repo: Path, **overrides: object) -> argparse.Namespace:
        base: dict[str, object] = {
            "repo": str(repo),
            "budget": None,
            "threshold": None,
            "slowest": 20,
            "enforce": False,
            "format": "json",
            "start_dir": _PKG,
            "pattern": "test*.py",
            "top_level_dir": None,
            "failfast": False,
            "verbosity": 0,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def _assert_repo_off_path(self, repo: Path) -> None:
        # The installed console-script condition: neither the repo root nor the
        # start-dir package's parent are already importable.
        self.assertNotIn(str(repo), sys.path)

    def test_installed_path_without_bootstrap_reproduces_collection_error(self) -> None:
        # Negative control: the pre-fix discovery (no repo-root bootstrap,
        # top_level_dir=None) fails exactly as CI did.
        repo = self._write_fake_repo()
        self._assert_repo_off_path(repo)
        start_dir = repo / _PKG
        loader = unittest.TestLoader()
        suite = loader.discover(start_dir=str(start_dir), pattern="test*.py")
        # unittest wraps the import failure in a _FailedTest; run it and confirm
        # the ModuleNotFoundError surfaces for the top-level package.
        result = unittest.TestResult()
        suite.run(result)
        self.assertFalse(result.wasSuccessful())
        blob = "".join(msg for _, msg in result.errors)
        self.assertIn("No module named", blob)
        self.assertIn(_PKG, blob)

    def test_installed_path_with_bootstrap_collects_and_preserves_verdict(self) -> None:
        repo = self._write_fake_repo()
        self._assert_repo_off_path(repo)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cmd_tests_profile(self._args(repo))

        # The cross-package import resolved: suite is green, both cases ran.
        self.assertEqual(code, 0)
        import json

        payload = json.loads(out.getvalue())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["test_count"], 2)

        # Selection / test IDs are unchanged: module names stay relative to
        # top_level_dir (the start-dir package), i.e. `<subpkg>.test_probe.*`,
        # NOT `<pkg>.<subpkg>.test_probe.*`. This proves the bootstrap only
        # *enabled* the import; it did not shift top_level_dir.
        ids = {t["test_id"] for t in payload["slowest"]}
        self.assertEqual(
            ids,
            {
                f"{_SUBPKG}.test_probe.ProbeScenario.test_marker_resolves",
                f"{_SUBPKG}.test_probe.ProbeScenario.test_second_case",
            },
        )
        for test_id in ids:
            self.assertFalse(test_id.startswith(_PKG + "."))

        # The bootstrap cleaned up after itself: the repo root it inserted is not
        # left on sys.path. (unittest's own discover separately leaves
        # top_level_dir — the start-dir package — on the path; that is
        # pre-existing stdlib behaviour, not this fix, and _cleanup_repo strips
        # it so suites stay isolated.)
        self.assertNotIn(str(repo), sys.path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
