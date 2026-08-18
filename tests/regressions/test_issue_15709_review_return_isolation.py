"""Regression pin: review-return test verdicts must not couple to the ambient home (Redmine #15709).

Fixed defect: the review-return / callback delivery test family exercised the production
deliver edge — whose reserve->send retirement gate resolves ``ScratchRetirementFence()``
through the AMBIENT process home (``home or mozyo_bridge_home()``) — without pinning
``MOZYO_BRIDGE_HOME``. The verdict then depended on whatever home the process resolved:
the operator's real store in a raw ``python -m unittest`` standalone / subset run
(phantom zero-send, "retirement authority unreadable / unsafe owner, mode, or file
type" — Redmine #15631 j#107289), or the run-shared fence home under
``mozyo-bridge tests run``. Standalone / partial / full runs of the same code could
disagree (the #15631 / ADR-0011 trade-off 1 false positive). The unpinned modules were
born with their originating features (c0f33247 #13684, and the #13683 / #14094 / #13974
pins); af3d5159 pinned only the #13684 scenario module, leaving the rest coupled.

The fix pins each test to its own temp home (``tests/support/process_home_pin.py``).
This file pins the recurrence: it re-runs the family in a nested interpreter whose
ambient home is ADVERSARIAL — the retirement-fence path is unreadable (a directory in
place of the sqlite file), so ANY test that reaches the retirement gate through the
ambient home fail-closes to a zero-send and goes red. Green here means every family
test's verdict is independent of the ambient home, in any run shape. The pin also
asserts the family writes nothing into the ambient home (a leak in the other direction
would poison the shared fence home across a partial run).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from tests.support.nested_python import hermetic_python_env

#: The review-return / callback delivery family whose verdicts #15709 decoupled from the
#: ambient home. Every module here exercises (or composes with) the production deliver
#: edge and MUST pin its own process home.
FAMILY_MODULES = (
    "tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_review_return_scenario",
    "tests.scenarios.test_single_workspace_delegated_coordinator_acceptance",
    "tests.regressions.test_issue_13683_lane_gateway_route",
    "tests.regressions.test_issue_14094_current_generation_callback",
    "tests.regressions.test_issue_13974_legacy_pending_terminal",
)


class ReviewReturnAmbientHomeIndependenceTest(unittest.TestCase):
    def test_family_is_green_under_an_adversarial_ambient_home(self) -> None:
        adv_home = Path(tempfile.mkdtemp())
        # The trap: the retirement authority path exists but is unreadable as a store (a
        # directory where the sqlite file belongs). The gate fail-closes on it, so any
        # family test whose deliver edge resolves the AMBIENT home zero-sends and fails.
        (adv_home / "scratch-retirement-fence.sqlite").mkdir()
        planted = {p.name for p in adv_home.iterdir()}

        env = hermetic_python_env()
        env["MOZYO_BRIDGE_HOME"] = str(adv_home)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *FAMILY_MODULES],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "review-return family verdict coupled to the ambient home:\n" + proc.stderr[-4000:],
        )
        # And the family must not WRITE into the ambient home either — under the shared
        # fence home of a partial run that write would leak into sibling modules.
        self.assertEqual({p.name for p in adv_home.iterdir()}, planted)


if __name__ == "__main__":
    unittest.main()
