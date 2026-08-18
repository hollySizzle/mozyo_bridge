"""Pin the test process's mozyo home to the test's own temp home (Redmine #15709).

Production reserve->send edges resolve their authority stores through
``home or mozyo_bridge_home()`` — e.g. the retirement gate every callback deliver
edge runs (``_callback_target_is_retiring`` -> ``target_is_retiring`` ->
``ScratchRetirementFence()``). A test that exercises a deliver edge WITHOUT
pinning the process home couples its verdict to whatever home the process
ambient-resolves to: the operator's real store in a raw ``python -m unittest``
run (phantom zero-send — "retirement authority unreadable / unsafe owner, mode,
or file type"; Redmine #15631 j#107289), or the run-shared fence home under
``mozyo-bridge tests run``. Either way the verdict differs between standalone,
subset, and full runs on the same code.

Pinning each test to its own fresh temp home makes every ambient resolution land
on the same empty store — an absent authority means "no retirement was ever
recorded", so the send proceeds and the verdict is deterministic across run
shapes. This keeps the production fail-closed contract intact: nothing is
disabled, the gate simply reads a home the test owns.

``unittest.TestCase.addCleanup`` unwinds LIFO, so a test that re-invokes
``self.setUp()`` mid-test (fresh stores) restores the environment correctly.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch


def pin_process_home(case: unittest.TestCase, home: Path) -> None:
    """Pin ``MOZYO_BRIDGE_HOME`` to ``home`` until the current test finishes."""
    env = patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False)
    env.start()
    case.addCleanup(env.stop)
