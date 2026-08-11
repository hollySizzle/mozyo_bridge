"""Regression pin for terminal-runtime expected-stderr hygiene (Redmine #14241)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests/unit/e_140_adapter_provider/f_130_terminal_runtime_provider"
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_parallel import (  # noqa: E402,E501
    _OUTPUT_TAIL_LIMIT,
)


class ExpectedStderrHygieneRegressionTest(unittest.TestCase):
    def test_green_terminal_runtime_suite_has_no_diagnostic_stderr(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "mozyo_bridge",
                "tests",
                "parallel",
                "--repo",
                str(ROOT),
                "--start-dir",
                str(SUITE),
                "--pattern",
                "test_*.py",
                "--jobs",
                "8",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["success"], payload)
        self.assertGreater(payload["aggregate"]["total_ran_tests"], 0)
        self.assertIsNone(
            re.search(r"(?m)^(?:error|warning):", completed.stderr),
            "the parallel parent leaked an application diagnostic",
        )
        for shard in payload["shards"]:
            stderr = shard["stderr_tail"]
            # The runner retains the final _OUTPUT_TAIL_LIMIT characters. A shorter
            # value is therefore the complete shard stream; equality would make the
            # hygiene proof inconclusive and must fail closed rather than hiding an
            # early diagnostic behind truncation.
            self.assertLess(len(stderr), _OUTPUT_TAIL_LIMIT, shard)
            self.assertRegex(stderr, r"(?m)^Ran \d+ tests? in ")
            self.assertRegex(stderr, r"(?m)^OK$")
            self.assertIsNone(
                re.search(r"(?m)^(?:error|warning):", stderr),
                "a green terminal-runtime shard leaked application diagnostics",
            )


if __name__ == "__main__":
    unittest.main()
