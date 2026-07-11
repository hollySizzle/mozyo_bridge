"""Callback wake adapter tests (Redmine #13520 / US #13518, design answer j#75098 Q1).

The Herdr CLI event is a hint only: every wake outcome (change / timeout / restart-error)
re-reads Redmine (``should_reread`` always True), and a wait failure is fail-safe (never raises).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
    WAKE_ERROR,
    WAKE_TIMED_OUT,
    WAKE_WOKE,
    resolve_wake,
)


class ResolveWakeTest(unittest.TestCase):
    def test_observed_change_is_woke_and_rereads(self):
        sig = resolve_wake(lambda: True)
        self.assertEqual(sig.kind, WAKE_WOKE)
        self.assertTrue(sig.should_reread)

    def test_timeout_still_rereads(self):
        sig = resolve_wake(lambda: False)
        self.assertEqual(sig.kind, WAKE_TIMED_OUT)
        self.assertTrue(sig.should_reread)  # a herdr timeout never means "nothing to do"

    def test_wait_failure_is_fail_safe_error_and_still_rereads(self):
        def boom():
            raise RuntimeError("cli event stream dropped")

        sig = resolve_wake(boom)
        self.assertEqual(sig.kind, WAKE_ERROR)
        self.assertTrue(sig.should_reread)  # a restart/error re-reads Redmine, does not crash

    def test_every_outcome_rereads(self):
        for wait_fn in (lambda: True, lambda: False, lambda: (_ for _ in ()).throw(OSError())):
            self.assertTrue(resolve_wake(wait_fn).should_reread)


if __name__ == "__main__":
    unittest.main()
