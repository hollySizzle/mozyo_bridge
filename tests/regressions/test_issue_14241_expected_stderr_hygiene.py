"""Regression pin for terminal-runtime expected-stderr hygiene (Redmine #14241)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests/unit/e_140_adapter_provider/f_130_terminal_runtime_provider"


class ExpectedStderrHygieneRegressionTest(unittest.TestCase):
    def test_green_terminal_runtime_suite_has_no_diagnostic_stderr(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(SUITE),
                "-p",
                "test_*.py",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertRegex(completed.stderr, r"(?m)^Ran \d+ tests? in ")
        self.assertRegex(completed.stderr, r"(?m)^OK$")
        self.assertIsNone(
            re.search(r"(?m)^(?:error|warning):", completed.stderr),
            "a green terminal-runtime suite leaked application diagnostics to parent stderr",
        )


if __name__ == "__main__":
    unittest.main()
