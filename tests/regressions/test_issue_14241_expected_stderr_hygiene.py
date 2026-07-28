"""Regression pin for terminal-runtime expected-stderr hygiene (Redmine #14241).

Hermeticity (Redmine #14645): the suite this file runs as a child process reads the
**operator-scoped** mozyo-bridge home — ``cmd_herdr_session_start`` resolves
``$MOZYO_BRIDGE_HOME/coordinator-placement.yaml`` before the guard its CLI tests
describe. Inheriting ``os.environ`` wholesale therefore made this regression's verdict
depend on whoever ran it: under ``MOZYO_BRIDGE_HOME=/dev/null`` the inner suite reported
``Ran 894 tests`` / ``FAILED (failures=1)``, the one failure being a CLI scenario that
died on the placement read before reaching the boundary it describes; a home holding a
malformed placement file broke that same scenario. Every child here is given an explicit
temp home instead, and that isolation is itself pinned below.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests/unit/e_140_adapter_provider/f_130_terminal_runtime_provider"

#: Read back the operator-scoped roots the suite would resolve, through the SAME
#: production resolvers the suite's subjects call — not a re-implementation of the home
#: contract. Printed one per line so the pin below compares exact paths.
_HOME_PROBE = (
    "from mozyo_bridge.shared.paths import mozyo_bridge_home;"
    "from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
    ".application.coordinator_placement_loader import coordinator_placement_path;"
    "print(mozyo_bridge_home());print(coordinator_placement_path())"
)


class ExpectedStderrHygieneRegressionTest(unittest.TestCase):
    def _run_child(self, argv, home):
        """Run ``argv`` under the env this regression hands its children.

        The one operator-scoped input the suite reads is pinned: ``MOZYO_BRIDGE_HOME``
        points at ``home``, a temp directory the caller owns, so the ambient value (and
        the ``~/.mozyo_bridge`` fallback when it is unset) never reaches the child.
        """
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        env["MOZYO_BRIDGE_HOME"] = str(home)
        return subprocess.run(
            [sys.executable, *argv],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_green_terminal_runtime_suite_has_no_diagnostic_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            completed = self._run_child(
                [
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(SUITE),
                    "-p",
                    "test_*.py",
                ],
                home,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stderr, r"(?m)^Ran \d+ tests? in ")
        self.assertRegex(completed.stderr, r"(?m)^OK$")
        self.assertIsNone(
            re.search(r"(?m)^(?:error|warning):", completed.stderr),
            "a green terminal-runtime suite leaked application diagnostics to parent stderr",
        )

    def test_child_env_does_not_carry_the_operator_home(self) -> None:
        """Redmine #14645: pin the ISOLATION, not just today's clean operator home.

        The suite above is hermetic only because ``_run_child`` overrides
        ``MOZYO_BRIDGE_HOME``; the scenario that used to fail now pins a temp home of its
        own as well, so suite greenness alone can no longer tell whether THIS layer still
        isolates. Assert the fact directly instead: under an ambient home that would
        otherwise reach the child and break it, the env
        this regression builds must still resolve the operator home — and the
        coordinator-placement file under it — to the temp directory. Dropping the
        override reds this test.

        ``/dev/null`` is the shape #14569 R8 hit: not a directory, so resolving the
        placement file under it raises ``NotADirectoryError`` rather than taking the
        missing-file default. The malformed home is the other shape that broke the suite.
        """
        malformed = Path(tempfile.mkdtemp(prefix="mzb14645-hostile-home-"))
        self.addCleanup(shutil.rmtree, malformed, True)
        (malformed / "coordinator-placement.yaml").write_text(
            "mode: [not, a, string]\n", encoding="utf-8"
        )
        for hostile in ("/dev/null", str(malformed)):
            with self.subTest(ambient_home=hostile):
                with tempfile.TemporaryDirectory() as home:
                    with patch.dict(
                        os.environ, {"MOZYO_BRIDGE_HOME": hostile}, clear=False
                    ):
                        completed = self._run_child(["-c", _HOME_PROBE], home)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    resolved = Path(home).resolve()
                    self.assertEqual(
                        completed.stdout.splitlines(),
                        [str(resolved), str(resolved / "coordinator-placement.yaml")],
                    )


if __name__ == "__main__":
    unittest.main()
