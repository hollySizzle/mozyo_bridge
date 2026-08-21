"""Regression pins for the two defects fixed under Redmine #15843 (review j#109937).

Both were found by the same-lane review of ``7cb19b7f`` and their verdicts are recorded in
#15843 j#109938. They are grouped in one file because they were fixed under one issue
(``tests-placement-discovery-policy.md`` R3-c).

``finding_composermarker`` — the stall classifier matched the dispatched body against the
whole visible pane. ``ack-completion-receiver-state.md`` restricts retention evidence to
the current composer tail and rejects scrollback matching, and the reason bites hardest
here: a *successfully submitted* body survives in the transcript as a user message, so a
whole-pane match reported ``unsent_composer`` / ``enter_only_retry`` most eagerly on
exactly the panes that had submitted. The prescribed remedy would then point an operator
at a phantom swallowed Enter instead of the real stall.

``finding_contentrefusal`` — no ``content_refusal`` signature was shipped, so a real
cyber-block screen fell through to ``unresponsive_indeterminate`` / patient wait and the
``context_reset_reinjection`` disposition #15816 established was unreachable. The wording
had in fact been captured verbatim in #15789 j#109183; the first implementation searched
the shipped binaries and never read that durable record.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
    StallWatchTarget,
    load_default_signatures,
    observe_target,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.pane_stall_sensor import (  # noqa: E501
    ScreenSample,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_CONTENT_REFUSAL,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    CLASS_UNSENT_COMPOSER,
    RX_CONTEXT_RESET_REINJECT,
    RX_ENTER_ONLY_RETRY,
    RX_PATIENT_WAIT_RETRY,
)

BODY = "[mozyo:handoff:source=redmine:issue=15843:journal=109881:kind=implementation_request]"

#: The screen #15789 j#109183 captured from the live pane, reproduced with its observed
#: hard wrap so the pins exercise the wrap the signature was chosen to survive.
CYBER_BLOCK_SCREEN = (
    "ⓘ This content can't be shown\n"
    "  We take extra caution with cybersecurity requests. If you're a security\n"
    "  professional, you may be able to apply for Trusted Access."
)


def _observe(screen: str, *, marker: str = "", provider: str = "codex"):
    """Classify one frozen pane (both samples identical) through the real registry."""
    return observe_target(
        StallWatchTarget(
            target="w1V:pY", provider_id=provider, pending_body_marker=marker
        ),
        ScreenSample("w1V:pY", 0.0, screen),
        ScreenSample("w1V:pY", 50.0, screen),
        signatures=load_default_signatures(),
    )


class ComposerEvidenceRegressionTest(unittest.TestCase):
    """finding_composermarker — retention evidence must be the CURRENT composer."""

    def test_a_submitted_body_left_in_the_transcript_is_not_an_unsent_composer(self):
        # The exact defect: the body was submitted (it is in the transcript, with the
        # receiver's own output rendered unindented below it) and the composer is empty.
        screen = f"› {BODY}\n• Working on the request\nthen the pane froze"
        observation = _observe(screen, marker=BODY)
        self.assertNotEqual(observation.stall_class, CLASS_UNSENT_COMPOSER)
        self.assertNotEqual(observation.prescription.action, RX_ENTER_ONLY_RETRY)
        self.assertEqual(observation.stall_class, CLASS_UNRESPONSIVE_INDETERMINATE)
        self.assertEqual(observation.prescription.action, RX_PATIENT_WAIT_RETRY)

    def test_an_older_copy_in_scrollback_does_not_arm_the_enter_prescription(self):
        # A prior dispatch of the same marker sits in scrollback while the CURRENT
        # composer is a fresh empty prompt. Whole-pane matching called this retained.
        screen = f"› {BODY}\nassistant: completed\n› "
        observation = _observe(screen, marker=BODY)
        self.assertNotEqual(observation.stall_class, CLASS_UNSENT_COMPOSER)

    def test_a_body_actually_sitting_in_the_current_composer_is_still_detected(self):
        # Non-vacuity: the fix must not have disabled the class. Here the last prompt IS
        # the composer and only indented TUI footer follows it.
        screen = f"assistant: previous turn\n› {BODY}\n\n  ? for shortcuts"
        observation = _observe(screen, marker=BODY)
        self.assertEqual(observation.stall_class, CLASS_UNSENT_COMPOSER)
        self.assertEqual(observation.prescription.action, RX_ENTER_ONLY_RETRY)

    def test_a_hard_wrapped_body_in_the_current_composer_still_matches(self):
        # The composer wraps a long marker mid-token; the shared predicate is
        # whitespace-insensitive precisely so this still counts as retained.
        head, tail = BODY[:40], BODY[40:]
        screen = f"assistant: previous turn\n› {head}\n  {tail}"
        observation = _observe(screen, marker=BODY)
        self.assertEqual(observation.stall_class, CLASS_UNSENT_COMPOSER)

    def test_no_marker_supplied_never_asserts_the_class(self):
        screen = f"assistant: previous turn\n› {BODY}\n\n  ? for shortcuts"
        self.assertNotEqual(_observe(screen).stall_class, CLASS_UNSENT_COMPOSER)


class ContentRefusalRegressionTest(unittest.TestCase):
    """finding_contentrefusal — the captured cyber-block screen must reach #15816."""

    def test_the_captured_cyber_block_screen_prescribes_context_reset(self):
        observation = _observe(CYBER_BLOCK_SCREEN)
        self.assertEqual(observation.stall_class, CLASS_CONTENT_REFUSAL)
        self.assertEqual(observation.prescription.action, RX_CONTEXT_RESET_REINJECT)
        self.assertEqual(observation.matched_id, "content_policy_refusal")

    def test_the_signature_survives_a_different_wrap_of_the_same_screen(self):
        # The fragments were chosen to sit inside one line each, so re-wrapping the same
        # message at a narrower pane width must not lose the match.
        rewrapped = (
            "ⓘ This content can't be shown\n"
            "  We take extra caution with\n"
            "  cybersecurity requests. If you're\n"
            "  a security professional, you may\n"
            "  be able to apply for Trusted Access."
        )
        self.assertEqual(_observe(rewrapped).stall_class, CLASS_CONTENT_REFUSAL)

    def test_neither_fragment_alone_classifies_a_refusal(self):
        # The AND is what keeps ordinary prose from tripping a prescription that discards
        # a live session's context.
        for screen in (
            "ⓘ This content can not be rendered in this terminal",
            "the agent is reviewing cybersecurity requests in the backlog",
        ):
            with self.subTest(screen=screen):
                self.assertNotEqual(
                    _observe(screen).stall_class, CLASS_CONTENT_REFUSAL
                )

    def test_the_refusal_screen_is_not_classified_for_an_unprofiled_provider(self):
        # Signatures are per-provider data; an unprofiled pane still fails safe.
        self.assertEqual(
            _observe(CYBER_BLOCK_SCREEN, provider="some-new-cli").stall_class,
            CLASS_UNRESPONSIVE_INDETERMINATE,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
