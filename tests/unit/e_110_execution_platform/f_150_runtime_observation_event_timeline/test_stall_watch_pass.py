"""Unit tests for the stall-watch classification pass (Redmine #15843).

Fakes only: the screen reader, the clock and the sleeper are injected, and the signature
registry is built from an in-memory record rather than the packaged artifact, so nothing
here touches a pane, a binary, or the filesystem.

The three ordered intersections the classifier declares are pinned explicitly. A
first-match tree whose intersections are only implied by the ordering is the shape that
silently hides a co-applicable case, so each is exercised with BOTH conditions true at
once and asserted to resolve the documented way.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
    StallWatchError,
    StallWatchTarget,
    classify_static_screen,
    observe_target,
    run_stall_watch_pass,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.pane_stall_sensor import (  # noqa: E501
    DIFF_CHROME_ONLY,
    DIFF_IDENTICAL,
    DIFF_INCOMPARABLE,
    READ_UNREADABLE,
    ScreenSample,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_BUSY_LIKELY,
    CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
    CLASS_SCREEN_PROGRESSING,
    CLASS_SCREEN_UNREADABLE,
    CLASS_STARTUP_INTERACTION,
    CLASS_UNKNOWN,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    CLASS_UNSENT_COMPOSER,
    RX_ENTER_ONLY_RETRY,
    RX_NO_ACTION,
    RX_OPERATOR_RESOLVES_SCREEN,
    RX_PATIENT_WAIT_RETRY,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_stall_signature import (  # noqa: E501
    StallSignatureRegistry,
)

#: The literal render of Claude Code's workspace trust dialog, as declared in the
#: (separate, rendered-confirmed) startup-blocker profile data.
TRUST_SCREEN = (
    "Is this a project you created or one you trust\n"
    "Claude will be able to read, edit, and execute files here"
)
RETRY_LINE = "✳ Thinking… · Retrying in 4s · attempt 2/10"
BODY_MARKER = "[mozyo:handoff:issue=15843] implementation request"


def _registry() -> StallSignatureRegistry:
    return StallSignatureRegistry.from_record(
        {
            "version": "1",
            "providers": {
                "claude": {
                    "stall_signatures": [
                        {
                            "id": "api_retry_backoff_capitalised",
                            "asserts": CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
                            "evidence": "binary_read_unrendered",
                            "all_of": ["· Retrying in ", "· attempt "],
                        }
                    ]
                }
            },
        }
    )


def _classify(screen, *, marker="", diff_state=DIFF_IDENTICAL, provider="claude"):
    return classify_static_screen(
        provider_id=provider,
        screen=screen,
        diff_state=diff_state,
        pending_body_marker=marker,
        signatures=_registry(),
    )


class ClassifyStaticScreenTest(unittest.TestCase):
    def test_a_frozen_screen_with_nothing_matched_is_the_patient_indeterminate_class(self):
        stall_class, matched, evidence = _classify("Working on it…")
        self.assertEqual(stall_class, CLASS_UNRESPONSIVE_INDETERMINATE)
        self.assertEqual((matched, evidence), ("", ""))

    def test_chrome_movement_with_nothing_matched_is_busy_not_a_stall(self):
        stall_class, _, _ = _classify("Working on it…", diff_state=DIFF_CHROME_ONLY)
        self.assertEqual(stall_class, CLASS_BUSY_LIKELY)

    def test_a_blank_readable_screen_is_unknown_not_frozen(self):
        # An empty pane has nothing on it to be frozen; reporting a stall for it would be
        # a finding about the read, dressed up as a finding about the lane.
        self.assertEqual(_classify("   \n\n  ")[0], CLASS_UNKNOWN)

    def test_a_declared_startup_screen_is_classified_by_the_existing_authority(self):
        stall_class, matched, _ = _classify(TRUST_SCREEN)
        self.assertEqual(stall_class, CLASS_STARTUP_INTERACTION)
        self.assertEqual(matched, "workspace_trust_confirmation")

    def test_a_declared_stall_signature_reports_its_id_and_evidence_tier(self):
        stall_class, matched, evidence = _classify(f"transcript\n{RETRY_LINE}")
        self.assertEqual(stall_class, CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED)
        self.assertEqual(matched, "api_retry_backoff_capitalised")
        self.assertEqual(evidence, "binary_read_unrendered")

    def test_a_retained_dispatched_body_is_the_unsent_composer_signature(self):
        stall_class, _, _ = _classify(f"> {BODY_MARKER}", marker=BODY_MARKER)
        self.assertEqual(stall_class, CLASS_UNSENT_COMPOSER)

    def test_without_a_supplied_marker_unsent_composer_is_never_guessed(self):
        # Absence of the durable-record fact is not a degraded mode: the class simply is
        # not asserted, rather than being inferred from some substring of the screen.
        self.assertEqual(
            _classify(f"> {BODY_MARKER}")[0], CLASS_UNRESPONSIVE_INDETERMINATE
        )

    def test_an_unprofiled_provider_falls_through_to_patience_not_to_a_guess(self):
        stall_class, _, _ = _classify(TRUST_SCREEN, provider="some-new-cli")
        self.assertEqual(stall_class, CLASS_UNRESPONSIVE_INDETERMINATE)


class DeclaredIntersectionsTest(unittest.TestCase):
    """Both conditions true at once; each must resolve the documented way."""

    def test_startup_screen_beats_a_retained_body(self):
        # least_effect_first: an Enter at a startup screen answers the dialog's default,
        # which is the #13760 / #14741 defect. The Enter prescription must be unreachable
        # while a startup screen is up.
        stall_class, matched, _ = _classify(
            f"{TRUST_SCREEN}\n> {BODY_MARKER}", marker=BODY_MARKER
        )
        self.assertEqual(stall_class, CLASS_STARTUP_INTERACTION)
        self.assertEqual(matched, "workspace_trust_confirmation")

    def test_startup_screen_beats_a_stall_signature(self):
        # role_precedence: rendered-confirmed evidence with an operator-owned remedy
        # outranks a lower-tier suspicion.
        stall_class, _, _ = _classify(f"{TRUST_SCREEN}\n{RETRY_LINE}")
        self.assertEqual(stall_class, CLASS_STARTUP_INTERACTION)

    def test_a_retained_body_beats_a_stall_signature(self):
        # direct_evidence_over_suspicion: the body's presence is an observation about
        # THIS dispatch; the banner is an inference about the provider.
        stall_class, _, _ = _classify(
            f"{RETRY_LINE}\n> {BODY_MARKER}", marker=BODY_MARKER
        )
        self.assertEqual(stall_class, CLASS_UNSENT_COMPOSER)


class ObserveTargetTest(unittest.TestCase):
    def test_progress_outranks_every_static_screen_inference(self):
        # A signature glimpsed while content is moving is transcript, not state.
        target = StallWatchTarget(target="p", provider_id="claude")
        obs = observe_target(
            target,
            ScreenSample("p", 0.0, "line one"),
            ScreenSample(
                "p", 50.0, "\n".join([TRUST_SCREEN, RETRY_LINE] + ["x"] * 40)
            ),
            signatures=_registry(),
        )
        self.assertEqual(obs.stall_class, CLASS_SCREEN_PROGRESSING)
        self.assertEqual(obs.prescription.action, RX_NO_ACTION)

    def test_an_unreadable_pair_is_screen_unreadable_and_prescribes_nothing(self):
        obs = observe_target(
            StallWatchTarget(target="p", provider_id="claude"),
            ScreenSample("p", 0.0, read_state=READ_UNREADABLE),
            ScreenSample("p", 50.0, read_state=READ_UNREADABLE),
            signatures=_registry(),
        )
        self.assertEqual(obs.stall_class, CLASS_SCREEN_UNREADABLE)
        self.assertEqual(obs.prescription.action, RX_NO_ACTION)

    def test_telemetry_reports_tokens_and_never_pane_content(self):
        obs = observe_target(
            StallWatchTarget(target="p", provider_id="claude"),
            ScreenSample("p", 0.0, TRUST_SCREEN),
            ScreenSample("p", 50.0, TRUST_SCREEN),
            signatures=_registry(),
        )
        payload = obs.telemetry()
        self.assertEqual(payload["stall_class"], CLASS_STARTUP_INTERACTION)
        self.assertEqual(payload["prescription"], RX_OPERATOR_RESOLVES_SCREEN)
        self.assertEqual(payload["posture"], "present_only")
        self.assertNotIn("trust", repr(payload).lower().replace("workspace_trust", ""))


class RunPassTest(unittest.TestCase):
    def _pass(self, screens, targets=None, **kwargs):
        cursors = {name: iter(pair) for name, pair in screens.items()}
        ticks = [0.0]

        def clock():
            ticks[0] += 1.0
            return ticks[0]

        slept: list[float] = []
        targets = targets or [
            StallWatchTarget(target=name, provider_id="claude") for name in screens
        ]
        observations = run_stall_watch_pass(
            targets,
            read_screen=lambda name: (True, next(cursors[name])),
            clock=clock,
            sleep=slept.append,
            signatures=_registry(),
            interval_seconds=kwargs.pop("interval_seconds", 0.0),
            **kwargs,
        )
        return observations, slept

    def test_the_interval_is_slept_once_per_pass_not_once_per_target(self):
        # Otherwise the watcher's cadence degrades linearly as the cockpit grows, which
        # is the property that made hand-polling unworkable.
        screens = {f"p{i}": ["same", "same"] for i in range(5)}
        _, slept = self._pass(screens, interval_seconds=7.0)
        self.assertEqual(slept, [7.0])

    def test_an_empty_target_list_sleeps_not_at_all(self):
        observations = run_stall_watch_pass(
            [],
            read_screen=lambda name: (True, ""),
            clock=lambda: 0.0,
            sleep=lambda seconds: self.fail("must not sleep with no targets"),
            signatures=_registry(),
        )
        self.assertEqual(observations, ())

    def test_a_raising_read_degrades_that_target_and_not_the_pass(self):
        # One wedged target must not blind the watcher to the rest of the cockpit.
        def read(name):
            if name == "bad":
                raise RuntimeError("herdr read failed")
            return True, "frozen screen"

        observations = run_stall_watch_pass(
            [
                StallWatchTarget(target="bad", provider_id="claude"),
                StallWatchTarget(target="good", provider_id="claude"),
            ],
            read_screen=read,
            clock=lambda: 0.0,
            sleep=lambda seconds: None,
            signatures=_registry(),
            interval_seconds=0.0,
        )
        by_target = {obs.target: obs for obs in observations}
        self.assertEqual(by_target["bad"].stall_class, CLASS_SCREEN_UNREADABLE)
        self.assertEqual(
            by_target["good"].stall_class, CLASS_UNRESPONSIVE_INDETERMINATE
        )
        self.assertEqual(
            by_target["good"].prescription.action, RX_PATIENT_WAIT_RETRY
        )

    def test_a_reader_reporting_not_readable_is_unreadable_not_a_blank_screen(self):
        # The reader contract is (readable, content), and a reader may report False
        # without raising. That branch must reach `screen_unreadable` (INCOMPARABLE),
        # not the readable-but-blank `unknown` — the two share a prescription today, so
        # only the class and the diff state distinguish them, and the CLI's blocked exit
        # code is derived from the diff state.
        observations = run_stall_watch_pass(
            [StallWatchTarget(target="p", provider_id="claude")],
            read_screen=lambda name: (False, ""),
            clock=lambda: 0.0,
            sleep=lambda seconds: None,
            signatures=_registry(),
            interval_seconds=0.0,
        )
        self.assertEqual(observations[0].stall_class, CLASS_SCREEN_UNREADABLE)
        self.assertEqual(observations[0].diff.state, DIFF_INCOMPARABLE)
        self.assertNotEqual(observations[0].stall_class, CLASS_UNKNOWN)

    def test_per_target_markers_are_applied_to_the_right_target(self):
        screens = {
            "unsent": [f"> {BODY_MARKER}", f"> {BODY_MARKER}"],
            "other": [f"> {BODY_MARKER}", f"> {BODY_MARKER}"],
        }
        targets = [
            StallWatchTarget(
                target="unsent", provider_id="claude", pending_body_marker=BODY_MARKER
            ),
            StallWatchTarget(target="other", provider_id="claude"),
        ]
        observations, _ = self._pass(screens, targets=targets)
        by_target = {obs.target: obs for obs in observations}
        self.assertEqual(by_target["unsent"].stall_class, CLASS_UNSENT_COMPOSER)
        self.assertEqual(
            by_target["unsent"].prescription.action, RX_ENTER_ONLY_RETRY
        )
        self.assertEqual(
            by_target["other"].stall_class, CLASS_UNRESPONSIVE_INDETERMINATE
        )

    def test_a_negative_interval_is_refused(self):
        with self.assertRaises(StallWatchError):
            run_stall_watch_pass(
                [StallWatchTarget(target="p")],
                read_screen=lambda name: (True, ""),
                clock=lambda: 0.0,
                sleep=lambda seconds: None,
                signatures=_registry(),
                interval_seconds=-1.0,
            )

    def test_a_target_without_an_identity_is_refused(self):
        with self.assertRaises(StallWatchError):
            StallWatchTarget(target="")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
