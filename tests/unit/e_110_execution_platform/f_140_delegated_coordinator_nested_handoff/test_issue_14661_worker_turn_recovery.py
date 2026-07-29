"""Unit: live turn-ended WORKER classification + refresh decision domain (Redmine #14661).

Pins the pure half of ``sublane refresh-worker``: the identity-bound turn classification (which
must remain the #14203 vocabulary, not a third dialect), the ordered fail-closed refresh gates
(the lane gateway / default coordinator / foreign slot are protected, and an unreadable
worktree refuses), the worker-progress gate vocabulary, and the exact action id. It also pins
the two properties the #14661 j#92369 design constraints demand structurally: the existing
vanished-worker admission is NOT loosened, and no blocker token is a new spelling of one the
sibling surfaces already own. No process, no DB, no I/O.
"""

from __future__ import annotations

import itertools
import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    GatewayTurnObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_AUTHORITY_CONFLICT,
    REFRESH_BLOCK_LAUNCH_AUTHORITY,
    REFRESH_BLOCK_NO_RESUME_ANCHOR,
    REFRESH_BLOCK_PENDING_COMPOSER,
    REFRESH_BLOCK_STALE_GENERATION,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    REFRESH_BLOCK_UNKNOWN,
    REFRESH_BLOCK_WRONG_ISSUE_LANE,
    TURN_CLASS_FAILED,
    TURN_CLASS_NOT_SETTLED,
    TURN_CLASS_PRODUCTIVE,
    TURN_CLASS_UNCONFIRMED,
    TURN_CLASS_UNOBSERVABLE,
    TURN_CLASSES,
    classify_gateway_turn,
    gateway_refresh_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    GATE_BEARING_KINDS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    RECOVER_BLOCK_DIRTY_UNREADABLE,
    RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
    RECOVER_BLOCK_NOT_STALE,
    RecoveryObservation,
    decide_recovery,
    stale_worker_recovery_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_PROGRESS_GATES,
    WORKER_REFRESH_ACTIONABLE,
    WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT,
    WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE,
    WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED,
    WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN,
    WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY,
    WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR,
    WORKER_REFRESH_BLOCK_NOT_SETTLED,
    WORKER_REFRESH_BLOCK_PENDING_COMPOSER,
    WORKER_REFRESH_BLOCK_STALE_GENERATION,
    WORKER_REFRESH_BLOCK_TURN_NOT_FAILED,
    WORKER_REFRESH_BLOCK_UNKNOWN,
    WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE,
    WORKER_REFRESH_BLOCKERS,
    WORKER_REFRESH_VERDICTS,
    WorkerRefreshObservation,
    WorkerTurnObservation,
    classify_worker_turn,
    decide_worker_refresh,
    is_worker_refresh_actionable,
    worker_refresh_action_id,
)

#: The shared turn axes, at the fully-confirmed-failure setting.
_TURN_FAILED_FACTS = dict(
    delivery_confirmed=True,
    turn_started=True,
    settled_turn_ended=True,
    expected_gate_landed=False,
    expected_gate_absent=True,
    durable_source_fresh=True,
)

#: The #14661 identity bindings, all established.
_BOUND_FACTS = dict(
    anchor_bound=True,
    lane_generation_bound=True,
    participant_revision_bound=True,
)

#: The six shared boolean axes the classifier reads, in a stable order (for the 2**6 sweep).
_SHARED_AXES = (
    "delivery_confirmed",
    "turn_started",
    "settled_turn_ended",
    "expected_gate_landed",
    "expected_gate_absent",
    "durable_source_fresh",
)


def _turn(**overrides) -> WorkerTurnObservation:
    facts = dict(_TURN_FAILED_FACTS)
    facts.update(_BOUND_FACTS)
    facts.update(overrides)
    return WorkerTurnObservation(**facts)


def _target(**overrides) -> WorkerRefreshObservation:
    facts = dict(
        identity_resolved=True,
        is_standard_sublane_worker=True,
        issue_lane_matches=True,
        generation_matches=True,
        settled_idle=True,
        composer_clear=True,
        resume_anchor_present=True,
        worktree_readable=True,
        gateway_distinct_preserved=True,
        no_authority_conflict=True,
        launch_authority_current=True,
    )
    facts.update(overrides)
    return WorkerRefreshObservation(**facts)


class TurnClassificationBindingTests(unittest.TestCase):
    def test_all_defaults_fail_closed_to_unobservable(self):
        self.assertEqual(classify_worker_turn(WorkerTurnObservation()), TURN_CLASS_UNOBSERVABLE)

    def test_each_missing_identity_binding_is_unobservable(self):
        for axis in _BOUND_FACTS:
            with self.subTest(axis=axis):
                self.assertEqual(
                    classify_worker_turn(_turn(**{axis: False})), TURN_CLASS_UNOBSERVABLE
                )

    def test_the_binding_axes_are_exactly_the_three_the_acceptance_names(self):
        # Redmine #14661 acceptance: the classification is typed by the Redmine anchor, the
        # lane generation, and the participant revision. Pinning the SET (not just each member)
        # means a fourth silent binding — or a dropped one — fails here.
        self.assertEqual(
            set(_BOUND_FACTS),
            {"anchor_bound", "lane_generation_bound", "participant_revision_bound"},
        )
        payload = WorkerTurnObservation().as_payload()
        for axis in _BOUND_FACTS:
            self.assertIn(axis, payload)

    def test_a_fully_bound_and_confirmed_turn_is_failed(self):
        self.assertEqual(classify_worker_turn(_turn()), TURN_CLASS_FAILED)

    def test_identity_bound_property_requires_all_three(self):
        self.assertTrue(_turn().identity_bound)
        for axis in _BOUND_FACTS:
            self.assertFalse(_turn(**{axis: False}).identity_bound)


class TurnClassificationSharedDialectTests(unittest.TestCase):
    """The worker classifier must BE the #14203 classifier, not a copy of it."""

    def test_every_bound_observation_matches_the_shared_classifier_exactly(self):
        # A differential oracle over the complete 2**6 space of the shared axes: for a fully
        # bound observation the worker classification must equal the shared one on EVERY
        # combination. A forked branch, a reordered gate, or a renamed class token anywhere in
        # the space fails here — hand-enumerated cases would not.
        for combo in itertools.product((False, True), repeat=len(_SHARED_AXES)):
            facts = dict(zip(_SHARED_AXES, combo))
            with self.subTest(**facts):
                worker = classify_worker_turn(_turn(**facts))
                shared = classify_gateway_turn(GatewayTurnObservation(**facts))
                self.assertEqual(worker, shared)

    def test_shared_axes_round_trip_onto_the_shared_observation(self):
        for combo in itertools.product((False, True), repeat=len(_SHARED_AXES)):
            facts = dict(zip(_SHARED_AXES, combo))
            with self.subTest(**facts):
                projected = _turn(**facts).shared_axes()
                self.assertIsInstance(projected, GatewayTurnObservation)
                for axis, value in facts.items():
                    self.assertEqual(getattr(projected, axis), value)

    def test_the_class_vocabulary_is_not_extended(self):
        # Every reachable classification is a member of the SHARED closed set: #14661 adds no
        # turn-class token, so downstream consumers keep reading one dialect.
        seen = set()
        for combo in itertools.product((False, True), repeat=len(_SHARED_AXES)):
            for bound in (False, True):
                obs = _turn(
                    **dict(zip(_SHARED_AXES, combo)),
                    anchor_bound=bound,
                    lane_generation_bound=bound,
                    participant_revision_bound=bound,
                )
                seen.add(classify_worker_turn(obs))
        self.assertTrue(seen <= TURN_CLASSES)
        # The classifier is discriminating: the four classes reachable from these axes all show.
        self.assertEqual(
            seen,
            {
                TURN_CLASS_PRODUCTIVE,
                TURN_CLASS_FAILED,
                TURN_CLASS_UNCONFIRMED,
                TURN_CLASS_NOT_SETTLED,
                TURN_CLASS_UNOBSERVABLE,
            },
        )

    def test_the_payload_carries_the_normalized_reason_never_the_raw_token(self):
        payload = _turn(reason_token="secret-provider-error-blob").as_payload()
        self.assertEqual(payload["reason"], "unknown")
        self.assertNotIn("secret-provider-error-blob", str(payload))


class WorkerProgressGateTests(unittest.TestCase):
    def test_progress_gates_are_derived_from_the_shared_gate_vocabulary(self):
        # Derived, never re-listed: a gate-bearing kind added upstream automatically counts as
        # worker progress, which can only ever REFUSE more refreshes (the safe direction).
        self.assertTrue(WORKER_PROGRESS_GATES <= GATE_BEARING_KINDS)
        self.assertEqual(GATE_BEARING_KINDS - WORKER_PROGRESS_GATES, {"review_result"})

    def test_review_result_is_not_worker_progress(self):
        # A review_result landing after the anchor is the reviewer's output — the very thing
        # delivered TO a worker. Counting it would let an incoming review suppress the recovery
        # of the worker that never answered it.
        self.assertNotIn("review_result", WORKER_PROGRESS_GATES)

    def test_the_worker_authored_gates_are_present(self):
        for gate in ("implementation_done", "review_request", "blocked"):
            self.assertIn(gate, WORKER_PROGRESS_GATES)


class RefreshDecisionTests(unittest.TestCase):
    #: axis -> the exact blocker its absence must name. Checked for total coverage below, so a
    #: new observation axis cannot be added without pinning its blocker.
    _EXPECTED = {
        "identity_resolved": WORKER_REFRESH_BLOCK_UNKNOWN,
        "is_standard_sublane_worker": WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN,
        "issue_lane_matches": WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE,
        "generation_matches": WORKER_REFRESH_BLOCK_STALE_GENERATION,
        "launch_authority_current": WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY,
        "settled_idle": WORKER_REFRESH_BLOCK_NOT_SETTLED,
        "composer_clear": WORKER_REFRESH_BLOCK_PENDING_COMPOSER,
        "resume_anchor_present": WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR,
        "worktree_readable": WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE,
        "gateway_distinct_preserved": WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED,
        "no_authority_conflict": WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT,
    }

    def test_all_positive_with_a_failed_turn_is_actionable(self):
        verdict = decide_worker_refresh(_target(), TURN_CLASS_FAILED)
        self.assertEqual(verdict, WORKER_REFRESH_ACTIONABLE)
        self.assertTrue(is_worker_refresh_actionable(verdict))

    def test_all_defaults_fail_closed_to_identity_unknown(self):
        self.assertEqual(
            decide_worker_refresh(WorkerRefreshObservation(), TURN_CLASS_FAILED),
            WORKER_REFRESH_BLOCK_UNKNOWN,
        )

    def test_the_blocker_map_covers_every_observation_axis(self):
        # The oracle for "did a new axis get pinned?" lives outside the hand-written list: the
        # observation's own payload keys. Adding an axis without an expectation fails here
        # rather than passing silently untested.
        self.assertEqual(set(self._EXPECTED), set(WorkerRefreshObservation().as_payload()))

    def test_each_single_fact_off_names_its_exact_blocker(self):
        for axis, expected in self._EXPECTED.items():
            with self.subTest(axis=axis):
                self.assertEqual(
                    decide_worker_refresh(_target(**{axis: False}), TURN_CLASS_FAILED),
                    expected,
                )

    def test_every_non_failed_turn_class_blocks_the_refresh(self):
        for turn_class in TURN_CLASSES - {TURN_CLASS_FAILED}:
            with self.subTest(turn_class=turn_class):
                self.assertEqual(
                    decide_worker_refresh(_target(), turn_class),
                    WORKER_REFRESH_BLOCK_TURN_NOT_FAILED,
                )

    def test_an_unbound_classification_reaches_the_decision_as_turn_not_failed(self):
        # The end-to-end statement of the acceptance's zero-close rule for a drifted identity:
        # an unbound observation classifies unobservable, and an unobservable turn refuses.
        unbound = classify_worker_turn(_turn(participant_revision_bound=False))
        self.assertEqual(unbound, TURN_CLASS_UNOBSERVABLE)
        self.assertEqual(
            decide_worker_refresh(_target(), unbound), WORKER_REFRESH_BLOCK_TURN_NOT_FAILED
        )

    #: The documented total order of the observation gates. The turn-class gate sits between
    #: ``generation_matches`` and ``launch_authority_current`` and is pinned separately below
    #: (it is not an observation axis).
    _ORDER = (
        "identity_resolved",
        "is_standard_sublane_worker",
        "issue_lane_matches",
        "generation_matches",
        "launch_authority_current",
        "settled_idle",
        "composer_clear",
        "resume_anchor_present",
        "worktree_readable",
        "gateway_distinct_preserved",
        "no_authority_conflict",
    )

    #: Where the turn-class gate sits in :attr:`_ORDER` — every axis strictly before it wins
    #: over a non-failed turn, every axis after it loses to one.
    _TURN_GATE_INDEX = 4

    def test_the_order_covers_every_axis_exactly_once(self):
        self.assertEqual(len(self._ORDER), len(set(self._ORDER)))
        self.assertEqual(set(self._ORDER), set(WorkerRefreshObservation().as_payload()))

    def test_every_gate_pair_resolves_to_the_earlier_gate(self):
        # A single-fact-off table cannot see an ADJACENT gate swap: with only one axis off,
        # two neighbouring gates in either order produce the same verdict. Turning off every
        # PAIR and demanding the earlier gate's blocker pins the complete total order, so no
        # reordering anywhere in the sequence survives.
        for i, earlier in enumerate(self._ORDER):
            for later in self._ORDER[i + 1:]:
                with self.subTest(earlier=earlier, later=later):
                    verdict = decide_worker_refresh(
                        _target(**{earlier: False, later: False}), TURN_CLASS_FAILED
                    )
                    self.assertEqual(verdict, self._EXPECTED[earlier])

    def test_the_turn_gate_sits_exactly_between_generation_and_launch_authority(self):
        # Pins the turn gate's position against EVERY axis, not just its two neighbours.
        for index, axis in enumerate(self._ORDER):
            with self.subTest(axis=axis, index=index):
                verdict = decide_worker_refresh(
                    _target(**{axis: False}), TURN_CLASS_UNOBSERVABLE
                )
                expected = (
                    self._EXPECTED[axis]
                    if index < self._TURN_GATE_INDEX
                    else WORKER_REFRESH_BLOCK_TURN_NOT_FAILED
                )
                self.assertEqual(verdict, expected)

    def test_the_gateway_protection_gate_fires_before_the_runtime_gates(self):
        # A lane gateway / foreign slot that is ALSO unsettled, composer-dirty, worktree-
        # unreadable and conflicted must still report the protection blocker: the protected set
        # is named before anything else about the slot is inspected.
        verdict = decide_worker_refresh(
            _target(
                is_standard_sublane_worker=False,
                settled_idle=False,
                composer_clear=False,
                worktree_readable=False,
                gateway_distinct_preserved=False,
                no_authority_conflict=False,
            ),
            TURN_CLASS_FAILED,
        )
        self.assertEqual(verdict, WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN)

    def test_the_turn_classification_outranks_the_launch_authority_axis(self):
        # A productive turn on an authority-broken lane reports turn_not_classified_failed: no
        # refresh is needed there, and naming a launch-authority gap would imply one is
        # (the #14475 ordering rule, mirrored).
        self.assertEqual(
            decide_worker_refresh(
                _target(launch_authority_current=False), TURN_CLASS_PRODUCTIVE
            ),
            WORKER_REFRESH_BLOCK_TURN_NOT_FAILED,
        )

    def test_the_launch_authority_axis_outranks_the_destructive_feasibility_gates(self):
        self.assertEqual(
            decide_worker_refresh(
                _target(launch_authority_current=False, settled_idle=False, worktree_readable=False),
                TURN_CLASS_FAILED,
            ),
            WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY,
        )

    def test_a_dirty_but_readable_worktree_is_actionable(self):
        # Byte preservation is the point of this surface: only an UNREADABLE worktree blocks.
        # ``worktree_readable`` carries no cleanliness claim, so the actionable case above is
        # exactly the dirty-worktree case the #14658 lane presented.
        self.assertEqual(
            decide_worker_refresh(_target(worktree_readable=True), TURN_CLASS_FAILED),
            WORKER_REFRESH_ACTIONABLE,
        )

    def test_with_launch_authority_replaces_only_that_axis(self):
        base = _target(launch_authority_current=False)
        joined = base.with_launch_authority(True)
        self.assertTrue(joined.launch_authority_current)
        for axis, value in base.as_payload().items():
            if axis != "launch_authority_current":
                self.assertEqual(joined.as_payload()[axis], value)


class VerdictVocabularyTests(unittest.TestCase):
    def test_the_verdict_vocabulary_is_closed_and_distinct(self):
        self.assertEqual(len(WORKER_REFRESH_VERDICTS), 13)
        self.assertEqual(len(WORKER_REFRESH_BLOCKERS), 12)
        self.assertNotIn(WORKER_REFRESH_ACTIONABLE, WORKER_REFRESH_BLOCKERS)

    def test_every_reachable_verdict_is_in_the_closed_set(self):
        reachable = {decide_worker_refresh(_target(), TURN_CLASS_FAILED)}
        for axis in WorkerRefreshObservation().as_payload():
            reachable.add(decide_worker_refresh(_target(**{axis: False}), TURN_CLASS_FAILED))
        reachable.add(decide_worker_refresh(_target(), TURN_CLASS_UNOBSERVABLE))
        self.assertEqual(reachable, WORKER_REFRESH_VERDICTS)

    def test_shared_tokens_are_the_sibling_surfaces_tokens_not_new_spellings(self):
        # #14661 j#92369: share the existing closed vocabulary, never invent a third dialect.
        # These are identity assertions against the module that FIRST defined each token.
        self.assertIs(WORKER_REFRESH_ACTIONABLE, REFRESH_ACTIONABLE)
        self.assertIs(WORKER_REFRESH_BLOCK_UNKNOWN, REFRESH_BLOCK_UNKNOWN)
        self.assertIs(WORKER_REFRESH_BLOCK_WRONG_ISSUE_LANE, REFRESH_BLOCK_WRONG_ISSUE_LANE)
        self.assertIs(WORKER_REFRESH_BLOCK_STALE_GENERATION, REFRESH_BLOCK_STALE_GENERATION)
        self.assertIs(WORKER_REFRESH_BLOCK_TURN_NOT_FAILED, REFRESH_BLOCK_TURN_NOT_FAILED)
        self.assertIs(WORKER_REFRESH_BLOCK_LAUNCH_AUTHORITY, REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertIs(WORKER_REFRESH_BLOCK_PENDING_COMPOSER, REFRESH_BLOCK_PENDING_COMPOSER)
        self.assertIs(WORKER_REFRESH_BLOCK_NO_RESUME_ANCHOR, REFRESH_BLOCK_NO_RESUME_ANCHOR)
        self.assertIs(WORKER_REFRESH_BLOCK_AUTHORITY_CONFLICT, REFRESH_BLOCK_AUTHORITY_CONFLICT)
        self.assertIs(WORKER_REFRESH_BLOCK_GATEWAY_OR_FOREIGN, RECOVER_BLOCK_GATEWAY_OR_FOREIGN)
        self.assertIs(WORKER_REFRESH_BLOCK_DIRTY_UNREADABLE, RECOVER_BLOCK_DIRTY_UNREADABLE)

    def test_only_the_two_mirrored_axes_are_new_tokens(self):
        new = {
            WORKER_REFRESH_BLOCK_NOT_SETTLED,
            WORKER_REFRESH_BLOCK_GATEWAY_NOT_DISTINGUISHED,
        }
        self.assertEqual(new, {"worker_not_settled", "gateway_not_distinguished"})


class ExistingAdmissionUnchangedTests(unittest.TestCase):
    """#14661 j#92369: the vanished-worker recovery must NOT be loosened."""

    def test_a_live_worker_is_still_not_stale_for_recover_stale(self):
        # The observation a LIVE turn-ended worker presents to ``recover-stale``: everything
        # holds except the positive shell-residue signal. That surface must still refuse it —
        # this issue opened a separate admission precisely because it does.
        live = RecoveryObservation(
            identity_resolved=True,
            is_standard_sublane_worker=True,
            issue_lane_matches=True,
            generation_matches=True,
            not_productive=True,
            is_stale=False,
            worktree_readable=True,
            no_authority_conflict=True,
        )
        self.assertEqual(decide_recovery(live), RECOVER_BLOCK_NOT_STALE)


class ActionIdTests(unittest.TestCase):
    _PIN = dict(
        lane_id="issue_14658_disposable_smoke",
        role="claude",
        provider="claude",
        assigned_name="mzb1_worker",
        locator="w4B:p10",
        revision="7",
    )

    def test_the_exact_id_shape_pins_the_row_revision(self):
        self.assertEqual(
            worker_refresh_action_id(**self._PIN),
            "refresh-worker:issue_14658_disposable_smoke:claude:claude:mzb1_worker:w4B:p10:r7",
        )

    def test_a_recycled_row_revision_derives_a_different_key(self):
        other = dict(self._PIN, revision="8")
        self.assertNotEqual(
            worker_refresh_action_id(**self._PIN), worker_refresh_action_id(**other)
        )

    def test_a_missing_component_raises(self):
        for field in self._PIN:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    worker_refresh_action_id(**dict(self._PIN, **{field: "  "}))

    def test_never_collides_with_the_sibling_recovery_keys(self):
        mine = worker_refresh_action_id(**self._PIN)
        stale = stale_worker_recovery_action_id(
            lane_id=self._PIN["lane_id"], role=self._PIN["role"],
            provider=self._PIN["provider"], assigned_name=self._PIN["assigned_name"],
            locator=self._PIN["locator"],
        )
        gateway = gateway_refresh_action_id(**self._PIN)
        self.assertEqual(len({mine, stale, gateway}), 3)
        self.assertTrue(mine.startswith("refresh-worker:"))
        self.assertFalse(mine.startswith(("recover:", "refresh-gateway:")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
