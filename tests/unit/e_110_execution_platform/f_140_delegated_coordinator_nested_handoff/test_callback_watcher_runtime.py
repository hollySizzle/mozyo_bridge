"""Managed callback-watcher composition root tests (Redmine #13520 review R2-F2).

The composition root resolves the watcher (source issue / workspace / wake target / attested sender)
fail-closed and owns the bounded restart-resilient run loop. Production composition: launcher ->
event/timeout/error wake -> exact-journal reread (per pass) -> at most one callback; an error pass
does not crash the loop.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_watcher_runtime import (
    WatcherConfigError,
    resolve_watcher_config,
    run_managed_watch,
)


class ResolveWatcherConfigTest(unittest.TestCase):
    def test_full_composition_resolves(self):
        cfg = resolve_watcher_config(
            source_issue="13518", workspace_id="ws1", wake_target="mzb1_ws_codex_default",
            sender_attested=True, max_passes=3,
        )
        self.assertEqual((cfg.source_issue, cfg.workspace_id, cfg.wake_target), ("13518", "ws1", "mzb1_ws_codex_default"))
        self.assertTrue(cfg.sender_attested)
        self.assertEqual(cfg.max_passes, 3)

    def test_missing_required_inputs_fail_closed(self):
        for kw in (
            dict(source_issue="", workspace_id="ws1", wake_target="t"),
            dict(source_issue="13518", workspace_id="", wake_target="t"),
            dict(source_issue="13518", workspace_id="ws1", wake_target=""),
        ):
            with self.subTest(kw=kw):
                with self.assertRaises(WatcherConfigError):
                    resolve_watcher_config(sender_attested=True, **kw)

    def test_unattested_sender_is_recorded_not_rejected(self):
        cfg = resolve_watcher_config(
            source_issue="13518", workspace_id="ws1", wake_target="t", sender_attested=False,
        )
        self.assertFalse(cfg.sender_attested)  # allowed to observe; downstream sends fail-closed

    def test_bad_max_passes_fail_closed(self):
        with self.assertRaises(WatcherConfigError):
            resolve_watcher_config(source_issue="13518", workspace_id="ws1", wake_target="t",
                                   sender_attested=True, max_passes=0)


class RunManagedWatchTest(unittest.TestCase):
    def test_production_composition_event_timeout_error_each_rereads_one_callback(self):
        cfg = resolve_watcher_config(
            source_issue="13518", workspace_id="ws1", wake_target="t", sender_attested=True, max_passes=3,
        )
        # Three wakes: a change (truthy), a bounded timeout (falsy), then a wait error (raises).
        wakes = iter([lambda: True, lambda: False, _boom])
        rereads = []

        def wait_fn():
            return next(wakes)()

        def run_pass():
            # Each pass re-reads Redmine (the authority) and delivers at most once.
            rereads.append(1)
            return {"deliver": {"delivered": [{"journal": "75094"}]}}

        passes = run_managed_watch(cfg, run_pass=run_pass, wait_fn=wait_fn)
        # Every wake outcome ran a pass (re-read Redmine): event, timeout, AND error.
        self.assertEqual(len(rereads), 3)
        self.assertEqual([p["wake"] for p in passes], ["woke", "timed_out", "error"])
        # At most one delivered per pass (the outbox fence caps it); no crash on the error wake.
        self.assertTrue(all(len(p["pass"]["deliver"]["delivered"]) == 1 for p in passes))

    def test_a_raising_pass_does_not_crash_the_managed_loop(self):
        cfg = resolve_watcher_config(
            source_issue="13518", workspace_id="ws1", wake_target="t", sender_attested=True, max_passes=2,
        )
        seen = []

        def flaky():
            seen.append(1)
            if len(seen) == 1:
                raise RuntimeError("transient redmine read")
            return {"deliver": {"delivered": []}}

        passes = run_managed_watch(cfg, run_pass=flaky, wait_fn=lambda: True)
        self.assertEqual(len(seen), 2)  # survived the raising pass to the next bounded wake
        self.assertEqual(passes[0]["pass"], {"error": "RuntimeError"})


def _boom():
    raise RuntimeError("herdr wait stream dropped")


if __name__ == "__main__":
    unittest.main()
