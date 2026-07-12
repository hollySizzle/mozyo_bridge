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
    build_herdr_event_wait,
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


class BuildHerdrEventWaitTest(unittest.TestCase):
    """#13520 review F1b: the production wait binds the stable `herdr wait agent-status` event."""

    def test_builds_the_stable_wait_argv(self):
        calls = []
        wait = build_herdr_event_wait(
            "/opt/herdr", "mzb1_ws_codex_default",
            status="working", timeout_ms=50000, runner=lambda argv: (calls.append(argv) or (0, "")),
        )
        self.assertTrue(wait())  # rc 0 -> observed the change (woke)
        self.assertEqual(
            calls[0],
            ["/opt/herdr", "wait", "agent-status", "mzb1_ws_codex_default",
             "--status", "working", "--timeout", "50000"],
        )

    def test_herdr_bounded_timeout_is_falsy_timed_out(self):
        # A non-zero exit WITH a timeout indicator is herdr's own bounded --timeout elapse.
        wait = build_herdr_event_wait("/opt/herdr", "t", runner=lambda argv: (1, "wait timed out"))
        sig = resolve_wake(wait)
        self.assertEqual(sig.kind, WAKE_TIMED_OUT)
        self.assertTrue(sig.should_reread)

    def test_nonzero_non_timeout_exit_is_wake_error_not_timeout(self):
        # #13520 review R2-F2: a non-zero exit with NO timeout indicator must be an error, not
        # collapsed to timeout.
        wait = build_herdr_event_wait("/opt/herdr", "t", runner=lambda argv: (3, "connection refused"))
        sig = resolve_wake(wait)
        self.assertEqual(sig.kind, WAKE_ERROR)
        self.assertTrue(sig.should_reread)  # correctness unaffected: still re-reads Redmine

    def test_runner_exception_propagates_to_wake_error(self):
        def boom(argv):
            raise OSError("no herdr binary")

        wait = build_herdr_event_wait("/opt/herdr", "t", runner=boom)
        # resolve_wake catches it -> WAKE_ERROR, fail-safe, still re-reads.
        sig = resolve_wake(wait)
        self.assertEqual(sig.kind, WAKE_ERROR)
        self.assertTrue(sig.should_reread)

    def test_outer_subprocess_timeout_propagates_to_wake_error(self):
        import subprocess

        def hang(argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)  # herdr hung past the outer bound

        wait = build_herdr_event_wait("/opt/herdr", "t", runner=hang)
        sig = resolve_wake(wait)
        self.assertEqual(sig.kind, WAKE_ERROR)  # a hung child is an error, not a benign timeout


if __name__ == "__main__":
    unittest.main()
