"""Specs for the ``observe reload`` herdr snapshot source (#13355).

The snapshot mapper is exercised with synthetic inventory views; the command
gating (`--source all` includes herdr only under the herdr backend, tmux
byte-invariance) is exercised with the capture helpers patched — no live herdr
binary, no live tmux, no OTel store.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application import (  # noqa: E501
    commands_runtime_observation as cro,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain import (  # noqa: E501
    runtime_observation as ro,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    project_observed_agents,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_TRANSPORT_ERROR,
)

_NOW = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat(timespec="seconds")


def _snapshot(view: HerdrInventoryView) -> ro.RuntimeObservationSnapshot:
    return cro._herdr_snapshot(
        inventory=view,
        now=_NOW,
        now_iso=_NOW_ISO,
        max_age_seconds=30.0,
        expired_after_seconds=300.0,
    )


class HerdrSnapshotTest(unittest.TestCase):
    def test_readable_inventory_is_a_fresh_strong_live_query(self) -> None:
        name = encode_assigned_name("ws-a", "claude", "")
        snap = _snapshot(
            HerdrInventoryView(
                backend_selected=True,
                ok=True,
                workspace_segment="ws-a",
                agents=project_observed_agents(
                    [{"name": name, "agent_status": "working"}]
                ),
            )
        )

        self.assertEqual(ro.SOURCE_HERDR, snap.source)
        self.assertEqual(ro.METHOD_LIVE_QUERY, snap.method)
        self.assertEqual(ro.FRESHNESS_FRESH, snap.freshness)
        self.assertEqual(ro.STRENGTH_STRONG_RUNTIME_SIGNAL, snap.strength)
        self.assertEqual(ro.DISPLAY_STATE_HEALTHY, snap.display_state)
        self.assertFalse(snap.needs_reload)
        self.assertIn("1 managed agent(s)", snap.notes[0])

    def test_unreadable_inventory_fails_closed(self) -> None:
        snap = _snapshot(
            HerdrInventoryView(
                backend_selected=True,
                ok=False,
                reason=REASON_TRANSPORT_ERROR,
                detail="herdr agent list timed out",
            )
        )
        self.assertEqual(ro.READABILITY_UNREADABLE, snap.readability)
        self.assertEqual(ro.DISPLAY_STATE_RELOAD_REQUIRED, snap.display_state)
        self.assertTrue(snap.needs_reload)
        self.assertIn("transport_error", snap.notes[0])

    def test_unselected_backend_fails_closed_with_note(self) -> None:
        snap = _snapshot(HerdrInventoryView(backend_selected=False))
        self.assertEqual(ro.READABILITY_UNREADABLE, snap.readability)
        self.assertTrue(snap.needs_reload)
        self.assertIn("not selected", snap.notes[0])


def _args(source: str) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        max_age=None,
        expired_after=None,
        db=None,
        home=None,
        as_json=False,
    )


def _stub_snapshot(source: str) -> ro.RuntimeObservationSnapshot:
    return ro.make_snapshot(
        source=source,
        method=ro.METHOD_LIVE_QUERY,
        observed_at=_NOW_ISO,
        readability=ro.READABILITY_READABLE,
        strength=ro.STRENGTH_STRONG_RUNTIME_SIGNAL,
        now=_NOW,
        max_age_seconds=30.0,
        expired_after_seconds=300.0,
    )


class ObserveReloadHerdrGatingTest(unittest.TestCase):
    def _run(self, source: str, *, selected: bool) -> str:
        captured = {"herdr_calls": 0}

        def fake_capture_herdr(**kwargs):
            captured["herdr_calls"] += 1
            return _stub_snapshot(ro.SOURCE_HERDR)

        with patch.object(
            cro, "_capture_tmux", lambda **k: _stub_snapshot(ro.SOURCE_TMUX)
        ), patch.object(
            cro, "_capture_otel", lambda **k: _stub_snapshot(ro.SOURCE_CACHE)
        ), patch.object(
            cro, "_herdr_selected", lambda args: selected
        ), patch.object(
            cro, "_capture_herdr", fake_capture_herdr
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            cro.cmd_observe_reload(_args(source))
        return stdout.getvalue()

    def test_all_under_tmux_backend_has_no_herdr_snapshot(self) -> None:
        output = self._run("all", selected=False)
        self.assertNotIn("herdr", output)

    def test_all_under_herdr_backend_includes_the_snapshot(self) -> None:
        output = self._run("all", selected=True)
        self.assertIn("herdr", output)

    def test_explicit_source_herdr_always_captures(self) -> None:
        # Even when the selection gate says False, an explicit --source herdr
        # captures (and the capture itself fails closed on an unselected repo).
        output = self._run("herdr", selected=False)
        self.assertIn("herdr", output)
        self.assertNotIn("tmux", output.split("\n")[1])

    def test_source_choices_carry_herdr(self) -> None:
        self.assertIn(cro.SOURCE_HERDR, cro.SOURCE_CHOICES)


if __name__ == "__main__":
    unittest.main()
