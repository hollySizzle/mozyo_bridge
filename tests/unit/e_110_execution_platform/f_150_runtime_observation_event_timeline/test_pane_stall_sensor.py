"""Unit tests for the screen-difference primary sensor (Redmine #15843).

Pure: no I/O, no tempfile, no subprocess, no repo fixture. Samples are constructed
in-memory and every threshold is passed explicitly.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.pane_stall_sensor import (  # noqa: E501
    DEFAULT_CHROME_SIMILARITY,
    DIFF_CHANGED,
    DIFF_CHROME_ONLY,
    DIFF_IDENTICAL,
    DIFF_INCOMPARABLE,
    NON_ADVANCING_STATES,
    READ_UNREADABLE,
    PaneStallSensorError,
    ScreenSample,
    compare_samples,
    normalize_screen,
)


def _busy_screen(seconds: int, tokens: str) -> str:
    """A realistically sized pane: 40 transcript lines plus one animated status line."""
    body = "\n".join(
        f"  {i:3d} some transcript line of a working agent session here"
        for i in range(40)
    )
    return f"{body}\n✳ Thinking… ({seconds}s · {tokens} tokens)"


class NormalizeScreenTest(unittest.TestCase):
    def test_trailing_whitespace_and_blank_tail_are_render_padding(self):
        self.assertEqual(normalize_screen("a  \nb\t\n\n\n"), "a\nb")

    def test_crlf_is_normalised_so_a_redraw_does_not_read_as_change(self):
        self.assertEqual(normalize_screen("a\r\nb"), normalize_screen("a\nb"))

    def test_interior_blank_lines_are_preserved(self):
        # Only the TAIL is padding; an interior gap is content the diff must see.
        self.assertEqual(normalize_screen("a\n\nb"), "a\n\nb")


class CompareSamplesTest(unittest.TestCase):
    def test_byte_identical_screen_is_identical_not_merely_similar(self):
        screen = _busy_screen(12, "1.2k")
        diff = compare_samples(
            ScreenSample("p", 0.0, screen), ScreenSample("p", 50.0, screen)
        )
        self.assertEqual(diff.state, DIFF_IDENTICAL)
        self.assertEqual(diff.similarity, 1.0)
        self.assertEqual(diff.elapsed_seconds, 50.0)

    def test_animated_chrome_on_a_full_pane_is_chrome_only(self):
        # The counters advanced; the content did not. This is what legitimate busy looks
        # like, and it must NOT collapse onto the frozen-screen state.
        diff = compare_samples(
            ScreenSample("p", 0.0, _busy_screen(12, "1.2k")),
            ScreenSample("p", 50.0, _busy_screen(62, "1.9k")),
        )
        self.assertEqual(diff.state, DIFF_CHROME_ONLY)
        self.assertLess(diff.similarity, 1.0)
        self.assertGreaterEqual(diff.similarity, DEFAULT_CHROME_SIMILARITY)

    def test_new_output_lines_are_changed(self):
        later = _busy_screen(62, "1.9k") + "\n" + "\n".join(
            f"  new output line {i}" for i in range(12)
        )
        diff = compare_samples(
            ScreenSample("p", 0.0, _busy_screen(12, "1.2k")),
            ScreenSample("p", 50.0, later),
        )
        self.assertEqual(diff.state, DIFF_CHANGED)
        self.assertTrue(diff.advancing)
        self.assertFalse(diff.triggers_classification)

    def test_both_non_advancing_states_trigger_classification(self):
        self.assertEqual(NON_ADVANCING_STATES, {DIFF_CHROME_ONLY, DIFF_IDENTICAL})

    def test_unreadable_sample_is_incomparable_never_identical(self):
        # "Could not read" must not become "nothing changed" — that is the one confusion
        # that would turn a transport fault into a fabricated stall report.
        for earlier_ok in (True, False):
            with self.subTest(earlier_ok=earlier_ok):
                if earlier_ok:
                    pair = (
                        ScreenSample("p", 0.0, "content"),
                        ScreenSample("p", 50.0, read_state=READ_UNREADABLE),
                    )
                else:
                    pair = (
                        ScreenSample("p", 0.0, read_state=READ_UNREADABLE),
                        ScreenSample("p", 50.0, "content"),
                    )
                diff = compare_samples(*pair)
                self.assertEqual(diff.state, DIFF_INCOMPARABLE)
                self.assertFalse(diff.advancing)
                self.assertFalse(diff.triggers_classification)

    def test_threshold_is_an_argument_not_a_constant(self):
        earlier, later = _busy_screen(12, "1.2k"), _busy_screen(62, "1.9k")
        strict = compare_samples(
            ScreenSample("p", 0.0, earlier),
            ScreenSample("p", 1.0, later),
            chrome_similarity=1.0,
        )
        self.assertEqual(strict.state, DIFF_CHANGED)

    def test_telemetry_carries_no_pane_content(self):
        diff = compare_samples(
            ScreenSample("p", 0.0, "secret transcript"),
            ScreenSample("p", 1.0, "secret transcript"),
        )
        self.assertNotIn("secret", repr(diff.telemetry()))
        self.assertEqual(
            set(diff.telemetry()),
            {"target", "screen_diff", "similarity", "elapsed_seconds"},
        )


class SensorFailClosedTest(unittest.TestCase):
    def test_mismatched_targets_refuse_to_diff(self):
        with self.assertRaises(PaneStallSensorError):
            compare_samples(ScreenSample("a", 0.0, "x"), ScreenSample("b", 1.0, "x"))

    def test_reversed_samples_refuse_to_diff(self):
        with self.assertRaises(PaneStallSensorError):
            compare_samples(ScreenSample("a", 5.0, "x"), ScreenSample("a", 1.0, "x"))

    def test_out_of_range_threshold_is_rejected(self):
        for threshold in (0.0, -0.5, 1.5):
            with self.subTest(threshold=threshold):
                with self.assertRaises(PaneStallSensorError):
                    compare_samples(
                        ScreenSample("a", 0.0, "x"),
                        ScreenSample("a", 1.0, "y"),
                        chrome_similarity=threshold,
                    )

    def test_unreadable_sample_may_not_carry_content(self):
        with self.assertRaises(PaneStallSensorError):
            ScreenSample("a", 0.0, "content", read_state=READ_UNREADABLE)

    def test_unknown_read_state_is_rejected(self):
        with self.assertRaises(PaneStallSensorError):
            ScreenSample("a", 0.0, read_state="probably_fine")

    def test_empty_target_is_rejected(self):
        with self.assertRaises(PaneStallSensorError):
            ScreenSample("", 0.0, "x")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
