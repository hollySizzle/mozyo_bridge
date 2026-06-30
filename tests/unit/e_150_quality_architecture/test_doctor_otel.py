"""Fake-port / pure-policy specifications for the doctor OTel receiver-health /
observation-gap boundary (#12892).

These exercise the ``doctor_otel`` verdict authority directly with a synthetic
read-view — without a real OTel store, without an HTTP receiver, without a real
tmux topology. They pin the legacy ``doctor_otel_section`` dict shape, the
receiver-unreachable note, the observation-gap detection, the
``unobserved_agents`` list, and the gap note wording.
"""

from __future__ import annotations

import argparse
import unittest
from typing import Any

from mozyo_bridge.application.doctor_otel import (
    RECEIVER_UNREACHABLE_NOTE,
    TMUX_UNAVAILABLE_NOTE,
    LiveOtelDoctorReads,
    OtelDoctorReads,
    OtelSectionUseCase,
    evaluate_otel_section,
)


def _record(
    *,
    pane_id: str = "%1",
    session: str = "mozyo-demo",
    agent_kind: str = "claude",
    view_sessions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "session": session,
        "agent_kind": agent_kind,
        "view_sessions": [session] if view_sessions is None else view_sessions,
    }


def _view(
    *,
    receiver_reachable: bool = True,
    receiver_error: str | None = None,
    observed_pairs: set[tuple[str, str]] | None = None,
    agent_records: list[dict[str, Any]] | None = None,
    gap_check_error: str | None = None,
    counts: dict[str, Any] | None = None,
    store_path: str = "/home/.mozyo-bridge/otel-events.sqlite",
    store_exists: bool = True,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "store_path": store_path,
        "store_exists": store_exists,
        "counts": {"total": 0} if counts is None else counts,
        "receiver_reachable": receiver_reachable,
        "observed_pairs": set() if observed_pairs is None else observed_pairs,
        "agent_records": agent_records,
        "gap_check_error": gap_check_error,
    }
    if not receiver_reachable:
        view["receiver_error"] = receiver_error or "boom"
    return view


class FakeOtelDoctorReads:
    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        self.calls += 1
        return self._view


class EvaluateOtelSectionTest(unittest.TestCase):
    def test_reachable_receiver_no_gaps_is_ok_with_legacy_shape(self) -> None:
        section = evaluate_otel_section(
            _view(
                receiver_reachable=True,
                agent_records=[],
                counts={"total": 3, "spans": 2},
            )
        )
        self.assertEqual("ok", section["status"])
        self.assertTrue(section["receiver_reachable"])
        self.assertEqual([], section["notes"])
        self.assertEqual([], section["unobserved_agents"])
        # Counts are merged in; key order is the legacy collector's order.
        self.assertEqual(3, section["total"])
        self.assertEqual(2, section["spans"])
        self.assertEqual(
            [
                "status",
                "store_path",
                "store_exists",
                "notes",
                "total",
                "spans",
                "receiver_reachable",
                "unobserved_agents",
            ],
            list(section.keys()),
        )

    def test_unreachable_receiver_records_error_and_by_design_note(self) -> None:
        section = evaluate_otel_section(
            _view(
                receiver_reachable=False,
                receiver_error="<urlopen error Connection refused>",
                agent_records=[],
            )
        )
        # A down receiver is NOT an error: the section status stays ok.
        self.assertEqual("ok", section["status"])
        self.assertFalse(section["receiver_reachable"])
        self.assertEqual(
            "<urlopen error Connection refused>", section["receiver_error"]
        )
        self.assertIn(RECEIVER_UNREACHABLE_NOTE, section["notes"])
        self.assertTrue(any("BY DESIGN" in note for note in section["notes"]))
        # ``receiver_error`` lands right after ``receiver_reachable``.
        keys = list(section.keys())
        self.assertEqual(
            keys.index("receiver_reachable") + 1, keys.index("receiver_error")
        )

    def test_unobserved_agent_pane_becomes_a_gap_with_note(self) -> None:
        section = evaluate_otel_section(
            _view(
                observed_pairs=set(),
                agent_records=[_record(pane_id="%1", session="mozyo-demo")],
            )
        )
        self.assertEqual(
            [{"pane_id": "%1", "session": "mozyo-demo", "agent": "claude"}],
            section["unobserved_agents"],
        )
        self.assertTrue(
            any("never emitted telemetry" in note for note in section["notes"])
        )

    def test_observed_pair_is_not_a_gap(self) -> None:
        section = evaluate_otel_section(
            _view(
                observed_pairs={("mozyo-demo", "claude")},
                agent_records=[_record(session="mozyo-demo", agent_kind="claude")],
            )
        )
        self.assertEqual([], section["unobserved_agents"])
        self.assertEqual([], section["notes"])

    def test_unknown_agent_kind_is_skipped(self) -> None:
        section = evaluate_otel_section(
            _view(agent_records=[_record(agent_kind="unknown")])
        )
        self.assertEqual([], section["unobserved_agents"])
        self.assertEqual([], section["notes"])

    def test_gap_keyed_on_any_view_session(self) -> None:
        # An agent observed under one of its view sessions is not a gap; the
        # intersection is over every ``view_sessions`` entry.
        section = evaluate_otel_section(
            _view(
                observed_pairs={("alt", "claude")},
                agent_records=[
                    _record(
                        session="mozyo-demo",
                        agent_kind="claude",
                        view_sessions=["mozyo-demo", "alt"],
                    )
                ],
            )
        )
        self.assertEqual([], section["unobserved_agents"])

    def test_tmux_unavailable_skips_the_gap_check(self) -> None:
        section = evaluate_otel_section(_view(agent_records=None))
        self.assertEqual([], section["unobserved_agents"])
        self.assertIn(TMUX_UNAVAILABLE_NOTE, section["notes"])

    def test_gap_check_error_is_a_note_not_a_raise(self) -> None:
        section = evaluate_otel_section(
            _view(agent_records=None, gap_check_error="discovery exploded")
        )
        self.assertEqual([], section["unobserved_agents"])
        self.assertIn(
            "observation-gap check failed: discovery exploded", section["notes"]
        )

    def test_gap_check_error_takes_precedence_over_records(self) -> None:
        # If the read raised, the error note wins even if partial records exist.
        section = evaluate_otel_section(
            _view(
                agent_records=[_record()],
                gap_check_error="boom",
            )
        )
        self.assertEqual([], section["unobserved_agents"])
        self.assertTrue(
            any("observation-gap check failed" in n for n in section["notes"])
        )
        self.assertFalse(
            any("never emitted telemetry" in n for n in section["notes"])
        )


class OtelSectionUseCaseTest(unittest.TestCase):
    def test_use_case_returns_legacy_section_dict(self) -> None:
        reads = FakeOtelDoctorReads(_view(agent_records=[]))
        section = OtelSectionUseCase(reads).execute()
        self.assertEqual("ok", section["status"])
        self.assertEqual(1, reads.calls)
        self.assertIn("unobserved_agents", section)

    def test_use_case_propagates_receiver_down(self) -> None:
        reads = FakeOtelDoctorReads(
            _view(receiver_reachable=False, agent_records=[])
        )
        section = OtelSectionUseCase(reads).execute()
        self.assertFalse(section["receiver_reachable"])
        self.assertEqual(1, reads.calls)

    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(
            LiveOtelDoctorReads(argparse.Namespace()), OtelDoctorReads
        )


if __name__ == "__main__":
    unittest.main()
