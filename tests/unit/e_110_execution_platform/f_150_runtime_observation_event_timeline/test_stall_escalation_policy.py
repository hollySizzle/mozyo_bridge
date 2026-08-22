"""Stall escalation policy tests (Redmine #15855).

The gate that decides when a run of stall observations may wake a coordinator. The
properties under test are the ones that keep the gate NARROWER than the classifier:
a verdict fires it (never raw screen-sameness), absence of evidence neither advances nor
resets, the run is per class rather than per target, and a crossed threshold latches so a
long stall pages once.

Two of the checks below are deliberately **enumeration-independent**, because #15844 R3
recorded the failure mode where a sweep that enumerates values but not positions stays
green through a real defect: the effect map is checked for total coverage of the declared
class vocabulary rather than against a hand-listed set, and the latch is checked as an
invariant over an arbitrarily long run rather than at one hand-picked length.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_BUSY_LIKELY,
    CLASS_CONTENT_REFUSAL,
    CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
    CLASS_SCREEN_PROGRESSING,
    CLASS_SCREEN_UNREADABLE,
    CLASS_STARTUP_INTERACTION,
    CLASS_UNKNOWN,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    CLASS_UNSENT_COMPOSER,
    STALL_CLASSES,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_policy import (  # noqa: E501
    DEFAULT_ESCALATION_THRESHOLD,
    ESCALATING_CLASSES,
    ESCALATION_EFFECTS,
    STREAK_ADVANCE,
    STREAK_EFFECTS,
    STREAK_HOLD,
    STREAK_RESET,
    StallEscalationPolicyError,
    StreakState,
    WatchIdentity,
    escalation_effect,
    fold_observation,
)

IDENTITY = WatchIdentity(
    workspace_id="wsA",
    lane_id="issue_15855_stall_wiring",
    role="claude",
    generation="g1",
    target="w1V:pK",
)


def _identity(**overrides):
    return WatchIdentity(
        **{
            "workspace_id": IDENTITY.workspace_id,
            "lane_id": IDENTITY.lane_id,
            "role": IDENTITY.role,
            "generation": IDENTITY.generation,
            "target": IDENTITY.target,
            **overrides,
        }
    )


def _fold(
    previous,
    stall_class,
    *,
    at="2026-08-22T09:00:00+00:00",
    threshold=2,
    identity=IDENTITY,
):
    return fold_observation(
        previous,
        identity=identity,
        stall_class=stall_class,
        observed_at=at,
        threshold=threshold,
    )


class EffectMapTest(unittest.TestCase):
    """The map must be total over the declared vocabulary, not over a list written twice."""

    def test_every_declared_class_has_exactly_one_declared_effect(self) -> None:
        self.assertEqual(set(ESCALATION_EFFECTS), set(STALL_CLASSES))
        for stall_class, effect in ESCALATION_EFFECTS.items():
            with self.subTest(stall_class=stall_class):
                self.assertIn(effect, STREAK_EFFECTS)

    def test_escalating_classes_is_derived_from_the_map(self) -> None:
        self.assertEqual(
            ESCALATING_CLASSES,
            frozenset(
                name for name, eff in ESCALATION_EFFECTS.items() if eff == STREAK_ADVANCE
            ),
        )

    def test_live_render_loop_classes_reset(self) -> None:
        # A working lane must never be aged into an escalation, however long it works.
        for stall_class in (CLASS_SCREEN_PROGRESSING, CLASS_BUSY_LIKELY):
            with self.subTest(stall_class=stall_class):
                self.assertEqual(escalation_effect(stall_class), STREAK_RESET)

    def test_no_evidence_classes_hold(self) -> None:
        for stall_class in (CLASS_SCREEN_UNREADABLE, CLASS_UNKNOWN):
            with self.subTest(stall_class=stall_class):
                self.assertEqual(escalation_effect(stall_class), STREAK_HOLD)

    def test_real_stall_classes_advance(self) -> None:
        # #15855 acceptance 2's "本物の停滞クラス", including the startup screen: a unit
        # sitting on a trust dialog is stopped forever, not merely slow.
        for stall_class in (
            CLASS_STARTUP_INTERACTION,
            CLASS_CONTENT_REFUSAL,
            CLASS_UNSENT_COMPOSER,
            CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
            CLASS_UNRESPONSIVE_INDETERMINATE,
        ):
            with self.subTest(stall_class=stall_class):
                self.assertEqual(escalation_effect(stall_class), STREAK_ADVANCE)

    def test_unknown_class_is_rejected_not_defaulted(self) -> None:
        with self.assertRaises(StallEscalationPolicyError):
            escalation_effect("no_such_class")


class AdvanceTest(unittest.TestCase):
    def test_first_detection_starts_a_run_of_one(self) -> None:
        decision = _fold(None, CLASS_CONTENT_REFUSAL)
        self.assertEqual(decision.effect, STREAK_ADVANCE)
        self.assertEqual(decision.consecutive, 1)
        self.assertFalse(decision.escalates)

    def test_second_same_class_detection_reaches_the_default_threshold(self) -> None:
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        second = _fold(first.next_state, CLASS_CONTENT_REFUSAL, at="t2")
        self.assertEqual(second.consecutive, 2)
        self.assertTrue(second.escalates)
        self.assertEqual(second.next_state.first_observed_at, "t1")
        self.assertEqual(second.next_state.last_observed_at, "t2")
        self.assertEqual(second.next_state.escalated_at, "t2")

    def test_threshold_of_one_fires_on_first_detection(self) -> None:
        decision = _fold(None, CLASS_UNSENT_COMPOSER, threshold=1)
        self.assertTrue(decision.escalates)

    def test_higher_threshold_defers(self) -> None:
        state = None
        for index in range(4):
            decision = _fold(state, CLASS_UNSENT_COMPOSER, at=f"t{index}", threshold=5)
            self.assertFalse(decision.escalates)
            state = decision.next_state
        self.assertEqual(state.consecutive, 4)


class ClassChangeTest(unittest.TestCase):
    def test_a_flapping_diagnosis_restarts_the_run(self) -> None:
        # The screen is stuck but the classification is not stable; the prescriptions for
        # these two classes are mutually destructive, so this must not count as a run.
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        second = _fold(first.next_state, CLASS_UNSENT_COMPOSER, at="t2")
        self.assertEqual(second.effect, STREAK_ADVANCE)
        self.assertEqual(second.consecutive, 1)
        self.assertFalse(second.escalates)
        self.assertEqual(second.next_state.stall_class, CLASS_UNSENT_COMPOSER)
        self.assertEqual(second.next_state.first_observed_at, "t2")

    def test_class_change_drops_a_previous_latch(self) -> None:
        latched = StreakState(
            identity=IDENTITY,
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=7,
            first_observed_at="t0",
            last_observed_at="t6",
            escalated_at="t1",
        )
        decision = _fold(latched, CLASS_UNRESPONSIVE_INDETERMINATE, at="t7", threshold=1)
        # A newly identified stall escalates on its own merits.
        self.assertTrue(decision.escalates)
        self.assertEqual(decision.next_state.escalated_at, "t7")


class ResetTest(unittest.TestCase):
    def test_progress_clears_the_run_and_the_latch_together(self) -> None:
        latched = StreakState(
            identity=IDENTITY,
            stall_class=CLASS_UNRESPONSIVE_INDETERMINATE,
            consecutive=9,
            first_observed_at="t0",
            last_observed_at="t8",
            escalated_at="t2",
        )
        for stall_class in (CLASS_SCREEN_PROGRESSING, CLASS_BUSY_LIKELY):
            with self.subTest(stall_class=stall_class):
                decision = _fold(latched, stall_class, at="t9")
                self.assertEqual(decision.effect, STREAK_RESET)
                self.assertIsNone(decision.next_state)
                self.assertEqual(decision.consecutive, 0)
                self.assertFalse(decision.escalates)

    def test_a_long_test_run_never_escalates(self) -> None:
        # busy_likely is what reasoning, a tool call and a long test run all look like.
        state = None
        for index in range(50):
            decision = _fold(state, CLASS_BUSY_LIKELY, at=f"t{index}", threshold=1)
            self.assertFalse(decision.escalates)
            state = decision.next_state
        self.assertIsNone(state)


class HoldTest(unittest.TestCase):
    def test_hold_returns_the_previous_state_byte_for_byte(self) -> None:
        previous = StreakState(
            identity=IDENTITY,
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=1,
            first_observed_at="t1",
            last_observed_at="t1",
        )
        for stall_class in (CLASS_SCREEN_UNREADABLE, CLASS_UNKNOWN):
            with self.subTest(stall_class=stall_class):
                decision = _fold(previous, stall_class, at="t2")
                self.assertEqual(decision.effect, STREAK_HOLD)
                self.assertIs(decision.next_state, previous)
                self.assertFalse(decision.escalates)
                # NOT refreshed: this pass observed nothing about the unit, so claiming a
                # fresh observation timestamp would make an unreadable target look
                # freshly confirmed to anything reading the store.
                self.assertEqual(decision.next_state.last_observed_at, "t1")

    def test_hold_on_no_previous_state_stays_empty(self) -> None:
        decision = _fold(None, CLASS_SCREEN_UNREADABLE)
        self.assertEqual(decision.effect, STREAK_HOLD)
        self.assertIsNone(decision.next_state)
        self.assertFalse(decision.escalates)

    def test_an_only_ever_unreadable_target_can_never_reach_any_threshold(self) -> None:
        # The mechanical consequence of HOLD: a wedged *reader* cannot manufacture a
        # stall verdict about a unit nobody could see.
        state = None
        for index in range(100):
            decision = _fold(state, CLASS_SCREEN_UNREADABLE, at=f"t{index}", threshold=1)
            self.assertFalse(decision.escalates)
            state = decision.next_state
        self.assertIsNone(state)

    def test_hold_neither_erases_nor_completes_a_run(self) -> None:
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        held = _fold(first.next_state, CLASS_SCREEN_UNREADABLE, at="t2")
        self.assertEqual(held.consecutive, 1)
        # The run resumes from where it was; the held pass neither counted nor reset it.
        resumed = _fold(held.next_state, CLASS_CONTENT_REFUSAL, at="t3")
        self.assertEqual(resumed.consecutive, 2)
        self.assertTrue(resumed.escalates)


class LatchTest(unittest.TestCase):
    def test_a_persisting_stall_fires_exactly_once(self) -> None:
        # Enumeration-independent: the invariant is "exactly one firing over an arbitrary
        # run", not "not firing at pass 3".
        state = None
        firings = []
        for index in range(30):
            decision = _fold(state, CLASS_UNRESPONSIVE_INDETERMINATE, at=f"t{index}")
            if decision.escalates:
                firings.append(index)
            state = decision.next_state
        self.assertEqual(firings, [DEFAULT_ESCALATION_THRESHOLD - 1])
        self.assertEqual(state.consecutive, 30)
        self.assertTrue(state.escalated)

    def test_the_latch_survives_held_passes(self) -> None:
        state = None
        for stall_class in (
            CLASS_CONTENT_REFUSAL,
            CLASS_CONTENT_REFUSAL,
            CLASS_SCREEN_UNREADABLE,
            CLASS_CONTENT_REFUSAL,
        ):
            decision = _fold(state, stall_class)
            state = decision.next_state
        self.assertTrue(state.escalated)
        self.assertFalse(decision.escalates)

    def test_a_reset_then_restall_may_fire_again(self) -> None:
        state = _fold(None, CLASS_CONTENT_REFUSAL, at="t1").next_state
        fired = _fold(state, CLASS_CONTENT_REFUSAL, at="t2")
        self.assertTrue(fired.escalates)
        alive = _fold(fired.next_state, CLASS_SCREEN_PROGRESSING, at="t3")
        self.assertIsNone(alive.next_state)
        again = _fold(None, CLASS_CONTENT_REFUSAL, at="t4")
        refired = _fold(again.next_state, CLASS_CONTENT_REFUSAL, at="t5")
        self.assertTrue(refired.escalates)


class StructuralRefusalTest(unittest.TestCase):
    def test_threshold_below_one_is_rejected_not_clamped(self) -> None:
        # Silently treating 0 as 1 would turn a misconfiguration into an escalation on the
        # very first non-advancing screen.
        for threshold in (0, -1):
            with self.subTest(threshold=threshold):
                with self.assertRaises(StallEscalationPolicyError):
                    _fold(None, CLASS_CONTENT_REFUSAL, threshold=threshold)

    def test_state_from_a_different_slot_is_refused(self) -> None:
        # Transplanting one unit's run length onto another is a caller error, not a state
        # to reconcile.
        other = StreakState(
            identity=_identity(lane_id="issue_99999_other"),
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=5,
            first_observed_at="t0",
            last_observed_at="t4",
        )
        with self.assertRaises(StallEscalationPolicyError):
            _fold(other, CLASS_CONTENT_REFUSAL)

    def test_a_different_role_on_the_same_lane_is_a_different_slot(self) -> None:
        other = StreakState(
            identity=_identity(role="codex"),
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=5,
            first_observed_at="t0",
            last_observed_at="t4",
        )
        with self.assertRaises(StallEscalationPolicyError):
            _fold(other, CLASS_CONTENT_REFUSAL)

    def test_identity_requires_every_slot_component(self) -> None:
        for blank in ("workspace_id", "lane_id", "role"):
            with self.subTest(blank=blank):
                with self.assertRaises(StallEscalationPolicyError):
                    _identity(**{blank: ""})

    def test_unknown_class_is_refused(self) -> None:
        with self.assertRaises(StallEscalationPolicyError):
            _fold(None, "no_such_class")

    def test_streak_state_rejects_a_zero_run(self) -> None:
        with self.assertRaises(StallEscalationPolicyError):
            StreakState(
                identity=IDENTITY,
                stall_class=CLASS_CONTENT_REFUSAL,
                consecutive=0,
                first_observed_at="t",
                last_observed_at="t",
            )


class TelemetryTest(unittest.TestCase):
    def test_telemetry_carries_tokens_only(self) -> None:
        decision = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        payload = decision.telemetry()
        self.assertEqual(payload["slot"], IDENTITY.slot_label)
        self.assertEqual(payload["generation"], "g1")
        self.assertEqual(payload["streak_effect"], STREAK_ADVANCE)
        self.assertEqual(payload["consecutive"], 1)
        self.assertEqual(payload["stall_class"], CLASS_CONTENT_REFUSAL)
        self.assertFalse(payload["escalates"])
        self.assertFalse(payload["already_escalated"])

    def test_reset_telemetry_has_no_stale_class(self) -> None:
        state = _fold(None, CLASS_CONTENT_REFUSAL).next_state
        payload = _fold(state, CLASS_SCREEN_PROGRESSING).telemetry()
        self.assertEqual(payload["consecutive"], 0)
        self.assertNotIn("stall_class", payload)



class GenerationBindingTest(unittest.TestCase):
    """A run is bound to the process behind the slot, not just to the slot."""

    def test_the_slot_key_ignores_the_transient_locator(self) -> None:
        # herdr locators are recycled and rebound; identity must not move with them.
        rebound = _identity(target="w9Q:pZ")
        self.assertEqual(rebound.key(), IDENTITY.key())
        self.assertTrue(rebound.same_slot(IDENTITY))

    def test_a_run_survives_a_locator_change(self) -> None:
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        second = _fold(
            first.next_state,
            CLASS_CONTENT_REFUSAL,
            at="t2",
            identity=_identity(target="w9Q:pZ"),
        )
        self.assertEqual(second.consecutive, 2)
        self.assertTrue(second.escalates)

    def test_a_generation_change_restarts_the_run(self) -> None:
        # A new process behind the slot has its own screen; inheriting the old run would
        # escalate a fresh agent for its predecessor's stall.
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        second = _fold(
            first.next_state,
            CLASS_CONTENT_REFUSAL,
            at="t2",
            identity=_identity(generation="g2"),
        )
        self.assertEqual(second.consecutive, 1)
        self.assertFalse(second.escalates)
        self.assertEqual(second.next_state.first_observed_at, "t2")
        self.assertEqual(second.next_state.identity.generation, "g2")

    def test_a_generation_change_drops_a_previous_latch(self) -> None:
        latched = StreakState(
            identity=IDENTITY,
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=9,
            first_observed_at="t0",
            last_observed_at="t8",
            escalated_at="t2",
        )
        decision = _fold(
            latched,
            CLASS_CONTENT_REFUSAL,
            at="t9",
            threshold=1,
            identity=_identity(generation="g2"),
        )
        self.assertTrue(decision.escalates)
        self.assertEqual(decision.consecutive, 1)

    def test_a_held_pass_on_a_NEW_generation_discards_the_old_run(self) -> None:
        # The generation is read from the lane lifecycle store, an AUTHORITY -- not from
        # the screen. So it is settled before the class's effect is consulted: an
        # unreadable screen taken just after a relaunch must not preserve the dead
        # process's run (review j#110146 finding_3).
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        held = _fold(
            first.next_state,
            CLASS_SCREEN_UNREADABLE,
            at="t2",
            identity=_identity(generation="g2"),
        )
        self.assertEqual(held.effect, STREAK_HOLD)
        self.assertTrue(held.generation_transition)
        self.assertIsNone(held.next_state)

    def test_a_held_pass_on_the_SAME_generation_still_holds(self) -> None:
        # HOLD itself is unchanged: within one generation it neither advances nor resets.
        first = _fold(None, CLASS_CONTENT_REFUSAL, at="t1")
        held = _fold(first.next_state, CLASS_SCREEN_UNREADABLE, at="t2")
        self.assertFalse(held.generation_transition)
        self.assertIsNotNone(held.next_state)
        self.assertEqual(held.next_state.consecutive, 1)
        self.assertEqual(held.next_state.identity.generation, "g1")

    def test_a_generation_change_drops_the_latch_under_every_effect(self) -> None:
        # The latch is the dangerous half: inheriting it would SUPPRESS an escalation the
        # new process earned. Checked across all three effects rather than at one class.
        latched = StreakState(
            identity=IDENTITY,
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=9,
            first_observed_at="t0",
            last_observed_at="t8",
            escalated_at="t2",
        )
        for stall_class in (
            CLASS_SCREEN_UNREADABLE,
            CLASS_UNKNOWN,
            CLASS_BUSY_LIKELY,
            CLASS_SCREEN_PROGRESSING,
        ):
            with self.subTest(stall_class=stall_class):
                decision = _fold(
                    latched, stall_class, at="t9", identity=_identity(generation="g2")
                )
                self.assertTrue(decision.generation_transition)
                self.assertIsNone(decision.next_state)
        advancing = _fold(
            latched, CLASS_CONTENT_REFUSAL, at="t9", identity=_identity(generation="g2")
        )
        self.assertTrue(advancing.generation_transition)
        self.assertEqual(advancing.consecutive, 1)
        self.assertEqual(advancing.next_state.escalated_at, "")

    def test_the_new_generation_can_escalate_on_its_own_merits(self) -> None:
        # The failure the latch inheritance caused: a relaunched unit that is genuinely
        # stuck must still be able to reach the threshold.
        latched = StreakState(
            identity=IDENTITY,
            stall_class=CLASS_CONTENT_REFUSAL,
            consecutive=9,
            first_observed_at="t0",
            last_observed_at="t8",
            escalated_at="t2",
        )
        g2 = _identity(generation="g2")
        first = _fold(latched, CLASS_CONTENT_REFUSAL, at="t9", identity=g2)
        self.assertFalse(first.escalates)
        second = _fold(first.next_state, CLASS_CONTENT_REFUSAL, at="t10", identity=g2)
        self.assertTrue(second.escalates)

    def test_the_transition_flag_is_false_without_a_previous_run(self) -> None:
        decision = _fold(None, CLASS_CONTENT_REFUSAL)
        self.assertFalse(decision.generation_transition)
        self.assertFalse(decision.telemetry()["generation_transition"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
