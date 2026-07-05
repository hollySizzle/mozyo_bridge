"""Unit tests for the standard-rail turn-start observation (Redmine #13166 / #13262).

Fully hermetic: the pane capture and sleep are injected fakes, so nothing here
touches a real tmux — matching the pane_resolver hermetic-test convention.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.application.turn_start_observation import (
    TURN_START_OBSERVE_INTERVAL_SECONDS,
    HERDR_TURN_START_PROJECTION,
    TurnStartObservation,
    observe_standard_turn_start,
    project_herdr_turn_start,
    resolve_turn_start_window,
    submit_activity_observed,
    turn_start_record_lines,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    TURN_START_OUTCOMES,
    TurnStartResult,
)


class ResolveTurnStartWindowTest(unittest.TestCase):
    """Raw ``--landing-timeout`` arg -> observation window (j#71985 finding 1)."""

    def test_unset_arg_keeps_coerced_default(self) -> None:
        self.assertEqual(resolve_turn_start_window(None, 8.0), 8.0)

    def test_explicit_zero_disables_observation(self) -> None:
        # The legacy marker-gate coercion turns an explicit 0 into 8.0; the
        # observation window must not inherit that swallow.
        self.assertEqual(resolve_turn_start_window(0.0, 8.0), 0.0)

    def test_explicit_negative_disables_observation(self) -> None:
        self.assertEqual(resolve_turn_start_window(-1.0, 8.0), 0.0)

    def test_explicit_positive_uses_coerced_window(self) -> None:
        self.assertEqual(resolve_turn_start_window(5.0, 5.0), 5.0)


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
        obs = observe_standard_turn_start(
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
        obs = observe_standard_turn_start(
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
        obs = observe_standard_turn_start(
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

        obs = observe_standard_turn_start(
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
        observe_standard_turn_start(
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

    def test_default_rail_label_is_codex_standard_rail_byte_compat(self) -> None:
        # Redmine #13262: the default label preserves the #13166 codex wording
        # byte-for-byte so an existing codex-standard record is unchanged.
        lines = turn_start_record_lines(
            TurnStartObservation(
                confirmed=True, polls=1, window_seconds=8.0, interval_seconds=0.5
            )
        )
        self.assertIn("codex standard-rail submit observation", lines[0])

    def test_rail_label_renders_claude_standard_rail(self) -> None:
        # Redmine #13262: a claude send passes "claude standard-rail" so the
        # telemetry is not mislabelled as codex.
        lines = turn_start_record_lines(
            TurnStartObservation(
                confirmed=False, polls=2, window_seconds=8.0, interval_seconds=0.5
            ),
            rail_label="claude standard-rail",
        )
        self.assertIn("claude standard-rail submit observation", lines[0])
        self.assertNotIn("codex", lines[0])

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


class HerdrTurnStartProjectionTest(unittest.TestCase):
    """Redmine #13255: the herdr rail outcome -> handoff ``(status, reason)`` wire."""

    def test_projection_covers_every_closed_outcome(self) -> None:
        # Total over the closed rail vocabulary: no outcome falls through to a
        # generic reason, and no stale key lingers.
        self.assertEqual(set(HERDR_TURN_START_PROJECTION), set(TURN_START_OUTCOMES))

    def test_each_outcome_maps_to_expected_status_reason(self) -> None:
        expected = {
            "started": ("sent", "ok"),
            "delivered_not_started": ("blocked", "turn_start_unconfirmed"),
            "blocked": ("blocked", "receiver_blocked"),
            "absent": ("blocked", "turn_start_absent"),
            "precondition_not_idle": ("blocked", "precondition_not_idle"),
            "inject_failed": ("blocked", "inject_failed"),
        }
        for outcome, (status, reason) in expected.items():
            result = TurnStartResult(outcome=outcome)
            self.assertEqual(project_herdr_turn_start(result), (status, reason), outcome)

    def test_only_started_projects_to_sent(self) -> None:
        statuses = {
            status for status, _ in HERDR_TURN_START_PROJECTION.values()
        }
        self.assertEqual(statuses, {"sent", "blocked"})
        self.assertEqual(HERDR_TURN_START_PROJECTION["started"][0], "sent")


if __name__ == "__main__":
    unittest.main()
