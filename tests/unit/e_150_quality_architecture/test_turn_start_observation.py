"""Unit tests for the codex standard-rail turn-start observation (Redmine #13166).

Fully hermetic: the pane capture and sleep are injected fakes, so nothing here
touches a real tmux — matching the pane_resolver hermetic-test convention.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.application.turn_start_observation import (
    TURN_START_OBSERVE_INTERVAL_SECONDS,
    TurnStartObservation,
    observe_codex_turn_start,
    submit_activity_observed,
    turn_start_record_lines,
)


class SubmitActivityObservedTest(unittest.TestCase):
    def test_identical_capture_is_no_activity(self) -> None:
        self.assertFalse(submit_activity_observed("marker body", "marker body"))

    def test_new_output_is_activity(self) -> None:
        self.assertTrue(
            submit_activity_observed("marker body", "marker body\nassistant: ...")
        )

    def test_trailing_whitespace_churn_is_not_activity(self) -> None:
        # A redraw that only re-pads trailing whitespace must not be mistaken for
        # a turn start.
        self.assertFalse(submit_activity_observed("line   \n", "line\n"))

    def test_empty_baseline_and_empty_post_is_no_activity(self) -> None:
        self.assertFalse(submit_activity_observed("", ""))


class ObserveCodexTurnStartTest(unittest.TestCase):
    def _capture_sequence(self, outputs):
        seq = list(outputs)

        def capture(_target: str, _lines: int) -> str:
            return seq.pop(0) if seq else (outputs[-1] if outputs else "")

        return capture

    def test_confirms_when_pane_advances(self) -> None:
        slept: list[float] = []
        obs = observe_codex_turn_start(
            "%2",
            baseline_capture="marker body",
            capture=self._capture_sequence(["marker body\n<turn>"]),
            sleep=slept.append,
            window_seconds=8.0,
            lines=200,
        )
        self.assertTrue(obs.confirmed)
        self.assertEqual(1, obs.polls)
        self.assertEqual([TURN_START_OBSERVE_INTERVAL_SECONDS], slept)

    def test_unconfirmed_when_pane_frozen(self) -> None:
        # The reported bug: Enter absorbed, composer unchanged, no turn started.
        slept: list[float] = []
        obs = observe_codex_turn_start(
            "%2",
            baseline_capture="marker body",
            capture=lambda _t, _l: "marker body",
            sleep=slept.append,
            window_seconds=1.0,
            lines=200,
            interval_seconds=0.5,
        )
        self.assertFalse(obs.confirmed)
        self.assertEqual(2, obs.polls)  # 0.5 + 0.5 fills the 1.0s window

    def test_confirms_on_later_poll(self) -> None:
        obs = observe_codex_turn_start(
            "%2",
            baseline_capture="b",
            capture=self._capture_sequence(["b", "b", "b\nnew"]),
            sleep=lambda _s: None,
            window_seconds=10.0,
            lines=200,
            interval_seconds=1.0,
        )
        self.assertTrue(obs.confirmed)
        self.assertEqual(3, obs.polls)

    def test_nonpositive_window_disables_and_confirms(self) -> None:
        calls: list[tuple[str, int]] = []

        def capture(target: str, lines: int) -> str:
            calls.append((target, lines))
            return "x"

        obs = observe_codex_turn_start(
            "%2",
            baseline_capture="x",
            capture=capture,
            sleep=lambda _s: None,
            window_seconds=0.0,
            lines=200,
        )
        self.assertTrue(obs.confirmed)
        self.assertEqual(0, obs.polls)
        self.assertEqual([], calls)  # no capture / no sleep when disabled

    def test_nonpositive_interval_falls_back_to_default(self) -> None:
        slept: list[float] = []
        observe_codex_turn_start(
            "%2",
            baseline_capture="a",
            capture=lambda _t, _l: "a",
            sleep=slept.append,
            window_seconds=1.0,
            lines=200,
            interval_seconds=0.0,
        )
        self.assertTrue(all(s == TURN_START_OBSERVE_INTERVAL_SECONDS for s in slept))
        self.assertTrue(slept)


class TurnStartRecordLinesTest(unittest.TestCase):
    def test_confirmed_line_reports_confirmed(self) -> None:
        lines = turn_start_record_lines(
            TurnStartObservation(
                confirmed=True, polls=1, window_seconds=8.0, interval_seconds=0.5
            )
        )
        self.assertEqual(1, len(lines))
        self.assertIn("turn start confirmed", lines[0])
        self.assertIn("no Enter re-issue and no auto-resend", lines[0])

    def test_unconfirmed_line_reports_unconfirmed(self) -> None:
        lines = turn_start_record_lines(
            TurnStartObservation(
                confirmed=False, polls=4, window_seconds=8.0, interval_seconds=0.5
            )
        )
        self.assertIn("turn start unconfirmed", lines[0])
        self.assertIn("4 poll(s)", lines[0])

    def test_record_line_carries_no_absolute_path(self) -> None:
        # Redaction: the pasteable record must not embed a filesystem path. The
        # telemetry is numbers + fixed tokens only (the sole "/" is the
        # "window / interval" separator, not a path).
        lines = turn_start_record_lines(
            TurnStartObservation(
                confirmed=True, polls=1, window_seconds=8.0, interval_seconds=0.5
            )
        )
        self.assertNotIn("/Users", lines[0])
        self.assertNotIn("/home", lines[0])


if __name__ == "__main__":
    unittest.main()
