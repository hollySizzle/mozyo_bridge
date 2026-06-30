"""Tests for the runtime observation reload surface (Redmine #12224).

Covers the snapshot envelope semantics codified in
``mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation`` and the ``observe reload`` command
handler: freshness derivation, the fail-closed display-state rule (never
``healthy`` when stale / unreadable / contradictory), the absence of truth-like
generic fields, and the non-zero exit on a fail-closed snapshot.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

# Self-contained src bootstrap so isolated discovery (unittest discover
# scoped to this subpackage or a single file) imports mozyo_bridge without
# relying on a sibling test inserting src first (Redmine #12490 j#64426).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application import commands_runtime_observation as cro
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain import runtime_observation as ro

NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta_seconds: float) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).isoformat(timespec="seconds")


class FreshnessDerivationTest(unittest.TestCase):
    def test_recent_observation_is_fresh(self) -> None:
        self.assertEqual(
            ro.derive_freshness(
                _iso(-5), now=NOW, max_age_seconds=30, expired_after_seconds=300
            ),
            ro.FRESHNESS_FRESH,
        )

    def test_aged_observation_is_stale_then_expired(self) -> None:
        self.assertEqual(
            ro.derive_freshness(
                _iso(-120), now=NOW, max_age_seconds=30, expired_after_seconds=300
            ),
            ro.FRESHNESS_STALE,
        )
        self.assertEqual(
            ro.derive_freshness(
                _iso(-600), now=NOW, max_age_seconds=30, expired_after_seconds=300
            ),
            ro.FRESHNESS_EXPIRED,
        )

    def test_unreadable_or_missing_timestamp_is_unknown(self) -> None:
        self.assertEqual(
            ro.derive_freshness(
                _iso(-5),
                now=NOW,
                max_age_seconds=30,
                expired_after_seconds=300,
                readable=False,
            ),
            ro.FRESHNESS_UNKNOWN,
        )
        self.assertEqual(
            ro.derive_freshness(
                None, now=NOW, max_age_seconds=30, expired_after_seconds=300
            ),
            ro.FRESHNESS_UNKNOWN,
        )
        self.assertEqual(
            ro.derive_freshness(
                "not-a-timestamp",
                now=NOW,
                max_age_seconds=30,
                expired_after_seconds=300,
            ),
            ro.FRESHNESS_UNKNOWN,
        )

    def test_future_timestamp_clamps_to_fresh(self) -> None:
        self.assertEqual(
            ro.derive_freshness(
                _iso(60), now=NOW, max_age_seconds=30, expired_after_seconds=300
            ),
            ro.FRESHNESS_FRESH,
        )


class DisplayStateFailClosedTest(unittest.TestCase):
    """The core invariant: never `healthy` unless fresh + readable + no conflict."""

    def test_fresh_readable_is_healthy(self) -> None:
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_FRESH,
                readability=ro.READABILITY_READABLE,
                contradiction=None,
            ),
            ro.DISPLAY_STATE_HEALTHY,
        )

    def test_stale_is_fail_closed_not_soft(self) -> None:
        # A stale (but not expired) readable observation must derive
        # reload_required, never a soft state that reads as current (j#61240).
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_STALE,
                readability=ro.READABILITY_READABLE,
                contradiction=None,
            ),
            ro.DISPLAY_STATE_RELOAD_REQUIRED,
        )

    def test_partial_readability_is_fail_closed_even_when_fresh(self) -> None:
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_FRESH,
                readability=ro.READABILITY_PARTIAL,
                contradiction=None,
            ),
            ro.DISPLAY_STATE_RELOAD_REQUIRED,
        )

    def test_expired_requires_reload(self) -> None:
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_EXPIRED,
                readability=ro.READABILITY_READABLE,
                contradiction=None,
            ),
            ro.DISPLAY_STATE_RELOAD_REQUIRED,
        )

    def test_unreadable_requires_reload(self) -> None:
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_UNKNOWN,
                readability=ro.READABILITY_UNREADABLE,
                contradiction=None,
            ),
            ro.DISPLAY_STATE_RELOAD_REQUIRED,
        )

    def test_contradiction_is_unknown_not_healthy(self) -> None:
        # Even a fresh, readable observation degrades to unknown under conflict.
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_FRESH,
                readability=ro.READABILITY_READABLE,
                contradiction=ro.CONTRADICTION_SOURCE_CONFLICT,
            ),
            ro.DISPLAY_STATE_UNKNOWN,
        )

    def test_unknown_freshness_is_unknown(self) -> None:
        self.assertEqual(
            ro.derive_display_state(
                freshness=ro.FRESHNESS_UNKNOWN,
                readability=ro.READABILITY_READABLE,
                contradiction=None,
            ),
            ro.DISPLAY_STATE_UNKNOWN,
        )


class SnapshotEnvelopeTest(unittest.TestCase):
    def test_fresh_snapshot_payload_and_stale_reason(self) -> None:
        snap = ro.make_snapshot(
            source=ro.SOURCE_TMUX,
            method=ro.METHOD_LIVE_QUERY,
            observed_at=_iso(-2),
            readability=ro.READABILITY_READABLE,
            strength=ro.STRENGTH_STRONG_RUNTIME_SIGNAL,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_HEALTHY)
        self.assertIsNone(snap.stale_reason)
        self.assertFalse(snap.needs_reload)
        payload = snap.as_payload()
        for key in (
            "observed_at",
            "source",
            "method",
            "freshness",
            "readability",
            "strength",
            "stale_reason",
            "contradiction",
            "display_state",
            "source_refs",
        ):
            self.assertIn(key, payload)

    def test_stale_snapshot_keeps_label_but_fails_closed(self) -> None:
        # -120s with max_age 30 / expired_after 300 => stale (not expired).
        snap = ro.make_snapshot(
            source=ro.SOURCE_CACHE,
            method=ro.METHOD_PROJECTION_READ,
            observed_at=_iso(-120),
            readability=ro.READABILITY_READABLE,
            strength=ro.STRENGTH_PROJECTION_ONLY,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        # The "stale" diagnostic label survives in the freshness field ...
        self.assertEqual(snap.freshness, ro.FRESHNESS_STALE)
        self.assertEqual(snap.stale_reason, ro.STALE_REASON_AGE_EXCEEDED)
        # ... but the derived state is fail-closed and is counted for reload.
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_RELOAD_REQUIRED)
        self.assertTrue(snap.needs_reload)

    def test_expired_snapshot_reason_and_needs_reload(self) -> None:
        snap = ro.make_snapshot(
            source=ro.SOURCE_CACHE,
            method=ro.METHOD_PROJECTION_READ,
            observed_at=_iso(-600),
            readability=ro.READABILITY_READABLE,
            strength=ro.STRENGTH_PROJECTION_ONLY,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.freshness, ro.FRESHNESS_EXPIRED)
        self.assertEqual(snap.stale_reason, ro.STALE_REASON_RELOAD_REQUIRED)
        self.assertTrue(snap.needs_reload)

    def test_unreadable_snapshot_reason(self) -> None:
        snap = ro.make_snapshot(
            source=ro.SOURCE_CACHE,
            method=ro.METHOD_PROJECTION_READ,
            observed_at=None,
            readability=ro.READABILITY_UNREADABLE,
            strength=ro.STRENGTH_PROJECTION_ONLY,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.stale_reason, ro.STALE_REASON_SOURCE_UNREADABLE)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_RELOAD_REQUIRED)

    def test_readable_but_no_timestamp_is_missing_source(self) -> None:
        snap = ro.make_snapshot(
            source=ro.SOURCE_CACHE,
            method=ro.METHOD_PROJECTION_READ,
            observed_at=None,
            readability=ro.READABILITY_READABLE,
            strength=ro.STRENGTH_PROJECTION_ONLY,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.freshness, ro.FRESHNESS_UNKNOWN)
        self.assertEqual(snap.stale_reason, ro.STALE_REASON_MISSING_SOURCE)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_UNKNOWN)

    def test_payload_has_no_truth_like_generic_fields(self) -> None:
        snap = ro.make_snapshot(
            source=ro.SOURCE_TMUX,
            method=ro.METHOD_LIVE_QUERY,
            observed_at=_iso(-2),
            readability=ro.READABILITY_READABLE,
            strength=ro.STRENGTH_STRONG_RUNTIME_SIGNAL,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(ro.forbidden_generic_fields(snap.as_payload()), [])


class TmuxMappingTest(unittest.TestCase):
    def test_live_runtime_is_strong_and_healthy(self) -> None:
        from mozyo_bridge.session_inventory import SOURCE_RUNTIME

        snap = cro._tmux_snapshot(
            inv_source=SOURCE_RUNTIME,
            collected_at=_iso(0),
            record_count=3,
            notes=(),
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.source, ro.SOURCE_TMUX)
        self.assertEqual(snap.method, ro.METHOD_LIVE_QUERY)
        self.assertEqual(snap.strength, ro.STRENGTH_STRONG_RUNTIME_SIGNAL)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_HEALTHY)

    def test_degraded_cache_with_records_is_partial_projection(self) -> None:
        from mozyo_bridge.session_inventory import SOURCE_CACHE

        snap = cro._tmux_snapshot(
            inv_source=SOURCE_CACHE,
            collected_at=_iso(-600),
            record_count=2,
            notes=("tmux unavailable; serving the last cached snapshot",),
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.source, ro.SOURCE_CACHE)
        self.assertEqual(snap.readability, ro.READABILITY_PARTIAL)
        self.assertEqual(snap.strength, ro.STRENGTH_PROJECTION_ONLY)
        self.assertTrue(snap.needs_reload)

    def test_degraded_with_no_cache_is_unreadable(self) -> None:
        from mozyo_bridge.session_inventory import SOURCE_CACHE

        snap = cro._tmux_snapshot(
            inv_source=SOURCE_CACHE,
            collected_at=None,
            record_count=0,
            notes=("tmux unavailable and no cached snapshot exists",),
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.readability, ro.READABILITY_UNREADABLE)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_RELOAD_REQUIRED)


class _FakeInventory:
    """Minimal stand-in for an InventorySnapshot (source/collected_at/records/notes)."""

    def __init__(self, *, source, collected_at, record_count, notes=()):
        self.source = source
        self.collected_at = collected_at
        self.records = [object()] * record_count
        self.notes = notes


class SnapshotFromInventoryTest(unittest.TestCase):
    """The public inventory -> envelope wrapper shared by the CLI and cockpit (#12225)."""

    def test_live_inventory_maps_to_healthy_tmux_snapshot(self) -> None:
        from mozyo_bridge.session_inventory import SOURCE_RUNTIME

        snap = cro.snapshot_from_inventory(
            _FakeInventory(
                source=SOURCE_RUNTIME, collected_at=_iso(0), record_count=2
            ),
            now=NOW,
        )
        self.assertEqual(snap.source, ro.SOURCE_TMUX)
        self.assertEqual(snap.method, ro.METHOD_LIVE_QUERY)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_HEALTHY)

    def test_stale_cache_inventory_is_fail_closed(self) -> None:
        from mozyo_bridge.session_inventory import SOURCE_CACHE

        snap = cro.snapshot_from_inventory(
            _FakeInventory(
                source=SOURCE_CACHE, collected_at=_iso(-600), record_count=1
            ),
            now=NOW,
        )
        self.assertEqual(snap.source, ro.SOURCE_CACHE)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_RELOAD_REQUIRED)
        self.assertTrue(snap.needs_reload)


class OtelMappingTest(unittest.TestCase):
    def test_missing_store_is_unreadable(self) -> None:
        snap = cro._otel_snapshot(
            store_exists=False,
            last_write=None,
            total=0,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.readability, ro.READABILITY_UNREADABLE)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_RELOAD_REQUIRED)

    def test_empty_store_is_unknown(self) -> None:
        snap = cro._otel_snapshot(
            store_exists=True,
            last_write=None,
            total=0,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.freshness, ro.FRESHNESS_UNKNOWN)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_UNKNOWN)

    def test_recent_store_is_healthy(self) -> None:
        snap = cro._otel_snapshot(
            store_exists=True,
            last_write=_iso(-5),
            total=12,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_HEALTHY)
        self.assertFalse(snap.needs_reload)

    def test_stale_store_is_fail_closed(self) -> None:
        snap = cro._otel_snapshot(
            store_exists=True,
            last_write=_iso(-120),
            total=12,
            now=NOW,
            max_age_seconds=30,
            expired_after_seconds=300,
        )
        self.assertEqual(snap.freshness, ro.FRESHNESS_STALE)
        self.assertEqual(snap.display_state, ro.DISPLAY_STATE_RELOAD_REQUIRED)
        self.assertTrue(snap.needs_reload)


def _run(argv: list[str]) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = args.func(args)
    return code, buf.getvalue()


class ObserveReloadCliTest(unittest.TestCase):
    def test_missing_otel_store_exits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "does-not-exist.sqlite"
            code, out = _run(
                ["observe", "reload", "--source", "otel", "--db", str(db), "--json"]
            )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["note"], ro.RELOAD_DIAGNOSTIC_ONLY_NOTE)
        self.assertIn("observed_now", payload)
        self.assertEqual(len(payload["snapshots"]), 1)
        snap = payload["snapshots"][0]
        self.assertEqual(snap["readability"], ro.READABILITY_UNREADABLE)
        self.assertEqual(snap["display_state"], ro.DISPLAY_STATE_RELOAD_REQUIRED)
        # No truth-like generic field leaked into the envelope.
        self.assertEqual(ro.forbidden_generic_fields(snap), [])

    def test_recent_otel_store_exits_zero(self) -> None:
        from mozyo_bridge.otel_store import OtelEvent, OtelEventStore

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "otel-events.sqlite"
            store = OtelEventStore(db)
            store.insert_events(
                [OtelEvent(signal="logs", event_name="claude_code.api_request")]
            )
            store.close()
            code, out = _run(
                ["observe", "reload", "--source", "otel", "--db", str(db), "--json"]
            )
        self.assertEqual(code, 0)
        snap = json.loads(out)["snapshots"][0]
        self.assertEqual(snap["display_state"], ro.DISPLAY_STATE_HEALTHY)
        self.assertEqual(snap["source"], ro.SOURCE_CACHE)
        self.assertEqual(snap["method"], ro.METHOD_PROJECTION_READ)

    def test_text_output_carries_boundary_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "does-not-exist.sqlite"
            code, out = _run(
                ["observe", "reload", "--source", "otel", "--db", str(db)]
            )
        self.assertEqual(code, 1)
        self.assertIn("DISPLAY_STATE", out)
        self.assertIn("reload_required", out)
        self.assertIn("action-time live preflight", out)


if __name__ == "__main__":
    unittest.main()
