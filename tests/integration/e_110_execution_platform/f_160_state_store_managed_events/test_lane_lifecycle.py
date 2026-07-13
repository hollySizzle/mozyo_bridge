"""Lane lifecycle component tests (Redmine #13689, Design Answer j#76741 Increment 1).

Pins the native ``state.sqlite`` component: the closed disposition / release
vocabularies and their transition matrix, the CAS guards (expected state, exact
revision, exact release action generation), the workspace-scoped owner index that
makes double ownership *unrepresentable*, atomic supersession, fail-closed reads,
and the ``state_schema_components`` registration.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    CAS_ACTION_MISMATCH,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_OWNER_CONFLICT,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_RECOVERY_POLICY,
    LANE_LIFECYCLE_SCHEMA_VERSION,
    OWNER_ABSENT,
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
    ReleasePinError,
    decode_release_pins,
    disposition_transition_allowed,
    encode_release_pins,
    lane_lifecycle_path,
    load_lane_lifecycle,
    load_lane_lifecycle_readonly,
    rehydrate_allowed,
    release_transition_allowed,
    resolve_lane_owner,
    validate_release_pins,
)
from mozyo_bridge.core.state.state_store import (  # noqa: E402
    STATE_CONTAINER_VERSION,
    STATE_STORE_FILENAME,
)

WS = "wsMain"
LANE_A = "issue_13689_lane_a"
LANE_B = "issue_13689_lane_b"
ISSUE = "13689"


def _decision(journal: str = "76741", issue: str = ISSUE) -> DecisionPointer:
    """The durable record authorizing one lifecycle write."""
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


# An UNBOUND lane owns no issue, but its decision anchor is still complete (R2-F1):
# the anchor names the record that declared the lane, not an ownership it lacks.
_UNBOUND = ""


def _pins() -> tuple[ReleasePin, ...]:
    """The lane's two managed slots, given to the store in arbitrary order."""
    return (
        ReleasePin(role="codex", assigned_name=f"mzb1_{WS}_codex_{LANE_A}", locator="%1"),
        ReleasePin(role="claude", assigned_name=f"mzb1_{WS}_claude_{LANE_A}", locator="%2"),
    )


def _stored_pins() -> tuple[ReleasePin, ...]:
    """The same slots as the store persists them: role-sorted, so the row is stable."""
    return tuple(sorted(_pins(), key=lambda p: p.role))


class TransitionMatrixTest(unittest.TestCase):
    """The pure edges (no store)."""

    def test_disposition_edges(self) -> None:
        for target in (
            DISPOSITION_SUPERSEDED,
            DISPOSITION_HIBERNATED,
            DISPOSITION_RETIRED,
        ):
            self.assertTrue(disposition_transition_allowed(DISPOSITION_ACTIVE, target))
        self.assertTrue(
            disposition_transition_allowed(DISPOSITION_HIBERNATED, DISPOSITION_ACTIVE)
        )
        self.assertTrue(
            disposition_transition_allowed(DISPOSITION_SUPERSEDED, DISPOSITION_RETIRED)
        )

    def test_superseded_never_returns_to_active(self) -> None:
        # Reviving a superseded lane would re-create two active owners for one issue.
        self.assertFalse(
            disposition_transition_allowed(DISPOSITION_SUPERSEDED, DISPOSITION_ACTIVE)
        )

    def test_retired_is_terminal(self) -> None:
        for target in (
            DISPOSITION_ACTIVE,
            DISPOSITION_HIBERNATED,
            DISPOSITION_SUPERSEDED,
        ):
            self.assertFalse(
                disposition_transition_allowed(DISPOSITION_RETIRED, target)
            )

    def test_release_edges(self) -> None:
        self.assertTrue(
            release_transition_allowed(RELEASE_NOT_REQUESTED, RELEASE_REQUESTED)
        )
        self.assertTrue(release_transition_allowed(RELEASE_REQUESTED, RELEASE_PARTIAL))
        self.assertTrue(release_transition_allowed(RELEASE_REQUESTED, RELEASE_RELEASED))
        # A pane close is idempotent, so a partial retry that closes more is progress.
        self.assertTrue(release_transition_allowed(RELEASE_PARTIAL, RELEASE_PARTIAL))
        self.assertTrue(release_transition_allowed(RELEASE_PARTIAL, RELEASE_RELEASED))
        self.assertFalse(
            release_transition_allowed(RELEASE_RELEASED, RELEASE_REQUESTED)
        )
        self.assertFalse(
            release_transition_allowed(RELEASE_NOT_REQUESTED, RELEASE_RELEASED)
        )

    def test_release_pins_round_trip(self) -> None:
        self.assertEqual(
            decode_release_pins(encode_release_pins(_pins())), _stored_pins()
        )
        self.assertEqual(decode_release_pins(""), ())
        # R1-F4: a corrupt pin set must not decode to a *shorter* list -- the caller
        # would close some slots and call the generation done, leaving the rest alive.
        with self.assertRaises(ReleasePinError):
            decode_release_pins("not json")
        with self.assertRaises(ReleasePinError):
            decode_release_pins('[{"role": "codex"}]')


class LaneLifecycleStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = LaneLifecycleStore(home=self.home)
        self.key_a = LaneLifecycleKey(WS, LANE_A)
        self.key_b = LaneLifecycleKey(WS, LANE_B)
        self.addCleanup(self._tmp.cleanup)

    # -- component registration ---------------------------------------------

    def test_lives_in_state_sqlite_and_registers_as_native_component(self) -> None:
        self.store.ensure_schema()
        path = lane_lifecycle_path(self.home)
        self.assertEqual(path.name, STATE_STORE_FILENAME)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                STATE_CONTAINER_VERSION,
            )
            row = conn.execute(
                "SELECT schema_version, recovery_policy, migrated_from "
                "FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertEqual(row[1], LANE_LIFECYCLE_RECOVERY_POLICY)
        self.assertIsNone(row[2])  # native: no legacy file to migrate from

    def test_ensure_schema_is_idempotent(self) -> None:
        self.store.ensure_schema()
        self.store.ensure_schema()
        self.assertEqual(self.store.records(), ())

    def test_key_rejects_a_lane_without_a_lane_id(self) -> None:
        # A legacy (lane-id-less) lane is #13685's scope, not silently keyed here.
        with self.assertRaises(ValueError):
            LaneLifecycleKey(WS, "")

    # -- declare -------------------------------------------------------------

    def test_declare_active_starts_at_revision_one(self) -> None:
        outcome = self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.revision, 1)
        record = self.store.get(self.key_a)
        self.assertEqual(record.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(record.process_release, RELEASE_NOT_REQUESTED)
        self.assertEqual(record.issue_id, ISSUE)
        self.assertEqual(record.decision_journal, "76741")
        self.assertEqual(record.revision, 1)

    def test_redeclare_is_refused_never_a_silent_overwrite(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        again = self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.assertFalse(again.applied)
        self.assertEqual(again.reason, CAS_ALREADY_DECLARED)

    def test_second_active_owner_of_one_issue_is_unrepresentable(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        conflict = self.store.declare_active(self.key_b, decision=_decision(), issue_id=ISSUE)
        self.assertFalse(conflict.applied)
        self.assertEqual(conflict.reason, CAS_OWNER_CONFLICT)
        self.assertIsNone(self.store.get(self.key_b))

    def test_owner_index_is_workspace_scoped(self) -> None:
        # The same issue number in a DIFFERENT workspace is a legitimate, unrelated
        # lane — a home-global unique index would have wrongly rejected it.
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        other = self.store.declare_active(LaneLifecycleKey("wsOther", LANE_A), decision=_decision(), issue_id=ISSUE)
        self.assertTrue(other.applied)

    def test_issueless_lanes_do_not_collide(self) -> None:
        self.assertTrue(self.store.declare_active(self.key_a, decision=_decision(), issue_id=_UNBOUND).applied)
        self.assertTrue(self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND).applied)

    # -- disposition CAS -----------------------------------------------------

    def test_transition_requires_the_exact_revision(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        stale = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=99,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertFalse(stale.applied)
        self.assertEqual(stale.reason, CAS_STALE_REVISION)
        self.assertEqual(stale.revision, 1)
        self.assertEqual(
            self.store.get(self.key_a).lane_disposition, DISPOSITION_ACTIVE
        )

    def test_transition_requires_the_expected_state(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        wrong = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=1,
            target=DISPOSITION_RETIRED,
            decision=_decision(),
        )
        self.assertFalse(wrong.applied)
        self.assertEqual(wrong.reason, CAS_UNEXPECTED_STATE)

    def test_duplicate_transition_is_a_no_op_not_a_clobber(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        first = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertTrue(first.applied)
        replay = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertFalse(replay.applied)
        self.assertEqual(replay.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key_a).revision, 2)

    def test_forbidden_edge_is_refused(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_RETIRED,
            decision=_decision(),
        )
        revived = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_RETIRED,
            expected_revision=2,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
        )
        self.assertFalse(revived.applied)
        self.assertEqual(revived.reason, CAS_FORBIDDEN_TRANSITION)

    def test_transition_on_an_undeclared_lane_is_not_found(self) -> None:
        outcome = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_NOT_FOUND)

    def test_hibernated_lane_releases_its_issue_ownership_slot(self) -> None:
        # Ownership is *active* ownership: once hibernated, the issue has no active
        # owner, so a fresh lane may take it (the index no longer covers the row).
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertTrue(self.store.declare_active(self.key_b, decision=_decision(), issue_id=ISSUE).applied)

    def test_rehydrate_is_refused_when_another_lane_took_the_issue(self) -> None:
        # The hibernate race: while lane A slept, lane B was declared owner of the
        # issue. Waking A must not produce a second active owner.
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertTrue(self.store.declare_active(self.key_b, decision=_decision(), issue_id=ISSUE).applied)
        wake = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=2,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
        )
        self.assertFalse(wake.applied)
        self.assertEqual(wake.reason, CAS_OWNER_CONFLICT)
        self.assertEqual(
            self.store.get(self.key_a).lane_disposition, DISPOSITION_HIBERNATED
        )
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, LANE_B)

    def test_supersession_refuses_when_a_third_lane_owns_the_issue(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        third = LaneLifecycleKey(WS, "issue_13689_lane_c")
        self.store.declare_active(third, decision=_decision(), issue_id=ISSUE)
        # A hibernated lane is not `active`, so the guard refuses on state before it
        # ever reaches the owner check — either way ownership does not move.
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=2,
            recovery=self.key_b,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, "issue_13689_lane_c")

    def test_rehydrate_resets_the_release_generation(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.store.request_release(
            self.key_a, expected_revision=2, action_id="act-1", pins=_pins()
        )
        self.store.record_release_outcome(
            self.key_a,
            action_id="act-1",
            expected_revision=3,
            target=RELEASE_RELEASED,
        )
        back = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=4,
            target=DISPOSITION_ACTIVE,
            decision=_decision(),
        )
        self.assertTrue(back.applied)
        record = self.store.get(self.key_a)
        # A terminal `released` must not leak into the lane's next life.
        self.assertEqual(record.process_release, RELEASE_NOT_REQUESTED)
        self.assertEqual(record.release_action_id, "")
        self.assertEqual(record.pins, ())

    # -- supersession --------------------------------------------------------

    def test_supersession_moves_ownership_atomically(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            decision=_decision(),
        )
        self.assertTrue(outcome.applied)
        self.assertEqual(
            self.store.get(self.key_a).lane_disposition, DISPOSITION_SUPERSEDED
        )
        self.assertEqual(self.store.get(self.key_b).lane_disposition, DISPOSITION_ACTIVE)
        owner = self.store.resolve_owner(WS, ISSUE)
        self.assertTrue(owner.resolved)
        self.assertEqual(owner.lane_id, LANE_B)

    def test_supersession_on_a_stale_revision_changes_nothing(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=42,
            recovery=self.key_b,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_STALE_REVISION)
        # Neither side moved: the old lane still owns the issue, the new one is absent.
        self.assertEqual(
            self.store.get(self.key_a).lane_disposition, DISPOSITION_ACTIVE
        )
        self.assertIsNone(self.store.get(self.key_b))
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, LANE_A)

    def test_supersession_refuses_a_foreign_issue(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            decision=_decision(issue="99999"),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_UNEXPECTED_STATE)

    def test_supersession_can_promote_a_hibernated_recovery_lane(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND)
        self.store.transition_disposition(
            self.key_b,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            recovery_expected_disposition=DISPOSITION_HIBERNATED,
            recovery_expected_revision=2,
            decision=_decision(),
        )
        self.assertTrue(outcome.applied)
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, LANE_B)

    def test_supersession_refuses_a_retired_recovery_lane(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND)
        self.store.transition_disposition(
            self.key_b,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_RETIRED,
            decision=_decision(),
        )
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            recovery_expected_disposition=DISPOSITION_RETIRED,
            recovery_expected_revision=2,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_FORBIDDEN_TRANSITION)
        # The old owner must survive a refused handover.
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, LANE_A)

    # -- release generation --------------------------------------------------

    def _hibernated(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )

    def test_release_cannot_be_requested_for_an_active_lane(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        outcome = self.store.request_release(
            self.key_a, expected_revision=1, action_id="act-1", pins=_pins()
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_UNEXPECTED_STATE)

    def test_request_release_pins_the_slots(self) -> None:
        self._hibernated()
        outcome = self.store.request_release(
            self.key_a, expected_revision=2, action_id="act-1", pins=_pins()
        )
        self.assertTrue(outcome.applied)
        record = self.store.get(self.key_a)
        self.assertEqual(record.process_release, RELEASE_REQUESTED)
        self.assertEqual(record.release_action_id, "act-1")
        self.assertEqual(record.pins, _stored_pins())

    def test_request_release_needs_an_action_and_pins(self) -> None:
        self._hibernated()
        with self.assertRaises(ValueError):
            self.store.request_release(
                self.key_a, expected_revision=2, action_id="", pins=_pins()
            )
        with self.assertRaises(ValueError):
            self.store.request_release(
                self.key_a, expected_revision=2, action_id="act-1", pins=()
            )

    def test_partial_release_is_retryable_to_released(self) -> None:
        self._hibernated()
        self.store.request_release(
            self.key_a, expected_revision=2, action_id="act-1", pins=_pins()
        )
        partial = self.store.record_release_outcome(
            self.key_a, action_id="act-1", expected_revision=3, target=RELEASE_PARTIAL
        )
        self.assertTrue(partial.applied)
        done = self.store.record_release_outcome(
            self.key_a, action_id="act-1", expected_revision=4, target=RELEASE_RELEASED
        )
        self.assertTrue(done.applied)
        self.assertEqual(self.store.get(self.key_a).process_release, RELEASE_RELEASED)

    def test_a_stale_generation_cannot_complete_a_newer_one(self) -> None:
        # The crash/retry hazard: an old release action must never mark a *new*
        # generation done (it would report slots closed that are still live).
        self._hibernated()
        self.store.request_release(
            self.key_a, expected_revision=2, action_id="act-1", pins=_pins()
        )
        outcome = self.store.record_release_outcome(
            self.key_a, action_id="act-OLD", expected_revision=3, target=RELEASE_RELEASED
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_ACTION_MISMATCH)
        self.assertEqual(self.store.get(self.key_a).process_release, RELEASE_REQUESTED)

    def test_release_outcome_rejects_a_non_outcome_target(self) -> None:
        self._hibernated()
        with self.assertRaises(ValueError):
            self.store.record_release_outcome(
                self.key_a,
                action_id="act-1",
                expected_revision=2,
                target=RELEASE_REQUESTED,
            )

    def test_released_is_terminal_within_the_generation(self) -> None:
        self._hibernated()
        self.store.request_release(
            self.key_a, expected_revision=2, action_id="act-1", pins=_pins()
        )
        self.store.record_release_outcome(
            self.key_a, action_id="act-1", expected_revision=3, target=RELEASE_RELEASED
        )
        again = self.store.record_release_outcome(
            self.key_a, action_id="act-1", expected_revision=4, target=RELEASE_PARTIAL
        )
        self.assertFalse(again.applied)
        self.assertEqual(again.reason, CAS_FORBIDDEN_TRANSITION)

    # -- owner resolution / durability --------------------------------------

    def test_owner_resolution_is_exact_one(self) -> None:
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).status, OWNER_ABSENT)
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        resolved = self.store.resolve_owner(WS, ISSUE)
        self.assertEqual(resolved.status, OWNER_RESOLVED)
        self.assertEqual(resolved.lane_id, LANE_A)
        # A superseded lane is no longer the owner.
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_SUPERSEDED,
            decision=_decision(),
        )
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).status, OWNER_ABSENT)

    def test_owner_resolution_needs_both_workspace_and_issue(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.assertEqual(self.store.resolve_owner("", ISSUE).status, OWNER_ABSENT)
        self.assertEqual(self.store.resolve_owner(WS, "").status, OWNER_ABSENT)

    def test_state_survives_a_reopen(self) -> None:
        self._hibernated()
        self.store.request_release(
            self.key_a, expected_revision=2, action_id="act-1", pins=_pins()
        )
        reopened = LaneLifecycleStore(home=self.home)
        record = reopened.get(self.key_a)
        self.assertEqual(record.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(record.process_release, RELEASE_REQUESTED)
        self.assertEqual(record.release_action_id, "act-1")
        self.assertEqual(record.revision, 3)

    def test_records_lists_every_disposition_for_diagnostics(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND)
        self.store.transition_disposition(
            self.key_b,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        got = {r.lane_id: r.lane_disposition for r in self.store.records()}
        self.assertEqual(
            got, {LANE_A: DISPOSITION_ACTIVE, LANE_B: DISPOSITION_HIBERNATED}
        )


class R1RegressionTest(unittest.TestCase):
    """The R1 findings (j#76765), each pinned by the exact scenario that reproduced it.

    Every one of these returned ``CasOutcome(applied=True)`` before the correction —
    the CAS looked like a guard but did not refuse anything.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = LaneLifecycleStore(home=self.home)
        self.key_a = LaneLifecycleKey(WS, LANE_A)
        self.key_b = LaneLifecycleKey(WS, LANE_B)
        self.addCleanup(self._tmp.cleanup)

    def _hibernate(self, key: LaneLifecycleKey, decision: DecisionPointer) -> None:
        self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=decision,
        )

    # -- R1-F1: ownership must not be stolen across a workspace or an issue --

    def test_f1_supersession_across_workspaces_is_refused(self) -> None:
        self.store.declare_active(LaneLifecycleKey("ws-a", LANE_A), decision=_decision(), issue_id=ISSUE)
        with self.assertRaises(ValueError):
            self.store.supersede_and_activate(
                superseded=LaneLifecycleKey("ws-a", LANE_A),
                expected_revision=1,
                recovery=LaneLifecycleKey("ws-b", LANE_B),
                decision=_decision(),
            )
        # ws-a keeps its owner; ws-b never gained one.
        self.assertEqual(self.store.resolve_owner("ws-a", ISSUE).lane_id, LANE_A)
        self.assertEqual(self.store.resolve_owner("ws-b", ISSUE).status, OWNER_ABSENT)

    def test_f1_recovery_lane_owning_another_issue_is_refused(self) -> None:
        # Promoting laneB would have left #99999 with no owner at all.
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(issue="99999"), issue_id="99999")
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            recovery_expected_disposition=DISPOSITION_ACTIVE,
            recovery_expected_revision=1,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_OWNER_CONFLICT)
        self.assertEqual(self.store.resolve_owner(WS, "99999").lane_id, LANE_B)
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, LANE_A)

    # -- R1-F2: the recovery lane is a CAS target too --

    def test_f2_supersession_without_recovery_expectations_is_refused(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND)
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.resolve_owner(WS, ISSUE).lane_id, LANE_A)

    def test_f2_stale_recovery_revision_is_refused(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND)
        self._hibernate(self.key_b, _decision())  # recovery is now at revision 2
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            recovery_expected_disposition=DISPOSITION_HIBERNATED,
            recovery_expected_revision=1,  # stale
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_STALE_REVISION)

    def test_f2_supersession_cannot_wipe_an_in_flight_release(self) -> None:
        # The reproducer: a caller holding only the OLD lane's revision must not be
        # able to clear a release generation the actuator may be executing right now.
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.declare_active(self.key_b, decision=_decision(), issue_id=_UNBOUND)
        self._hibernate(self.key_b, _decision())
        self.store.request_release(
            self.key_b, expected_revision=2, action_id="act-live", pins=_pins()
        )
        outcome = self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=self.key_b,
            recovery_expected_disposition=DISPOSITION_HIBERNATED,
            recovery_expected_revision=3,
            decision=_decision(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_FORBIDDEN_TRANSITION)
        survivor = self.store.get(self.key_b)
        self.assertEqual(survivor.process_release, RELEASE_REQUESTED)
        self.assertEqual(survivor.release_action_id, "act-live")
        self.assertEqual(survivor.pins, _stored_pins())

    # -- R1-F3: rehydrate must not cancel a release generation --

    def test_f3_rehydrate_is_refused_while_a_release_is_in_flight(self) -> None:
        for state in (RELEASE_REQUESTED, RELEASE_PARTIAL):
            with self.subTest(process_release=state):
                store = LaneLifecycleStore(home=Path(tempfile.mkdtemp()))
                key = LaneLifecycleKey(WS, LANE_A)
                store.declare_active(key, decision=_decision(), issue_id=ISSUE)
                store.transition_disposition(
                    key,
                    expected_disposition=DISPOSITION_ACTIVE,
                    expected_revision=1,
                    target=DISPOSITION_HIBERNATED,
                    decision=_decision(),
                )
                store.request_release(
                    key, expected_revision=2, action_id="act-live", pins=_pins()
                )
                revision = 3
                if state == RELEASE_PARTIAL:
                    store.record_release_outcome(
                        key,
                        action_id="act-live",
                        expected_revision=3,
                        target=RELEASE_PARTIAL,
                    )
                    revision = 4
                outcome = store.transition_disposition(
                    key,
                    expected_disposition=DISPOSITION_HIBERNATED,
                    expected_revision=revision,
                    target=DISPOSITION_ACTIVE,
                    decision=_decision(),
                )
                self.assertFalse(outcome.applied)
                self.assertEqual(outcome.reason, CAS_FORBIDDEN_TRANSITION)
                record = store.get(key)
                self.assertEqual(record.lane_disposition, DISPOSITION_HIBERNATED)
                self.assertEqual(record.process_release, state)
                self.assertEqual(record.release_action_id, "act-live")

    def test_f3_rehydrate_is_allowed_once_the_release_finished(self) -> None:
        key = self.key_a
        self.store.declare_active(key, decision=_decision(), issue_id=ISSUE)
        self._hibernate(key, _decision())
        self.store.request_release(
            key, expected_revision=2, action_id="act-1", pins=_pins()
        )
        self.store.record_release_outcome(
            key, action_id="act-1", expected_revision=3, target=RELEASE_RELEASED
        )
        outcome = self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=4,
            target=DISPOSITION_ACTIVE,
            decision=_decision(journal="76767"),
        )
        self.assertTrue(outcome.applied)
        self.assertEqual(self.store.get(key).process_release, RELEASE_NOT_REQUESTED)

    def test_f3_policy_is_pure_and_shared(self) -> None:
        self.assertTrue(rehydrate_allowed(RELEASE_NOT_REQUESTED))
        self.assertTrue(rehydrate_allowed(RELEASE_RELEASED))
        self.assertFalse(rehydrate_allowed(RELEASE_REQUESTED))
        self.assertFalse(rehydrate_allowed(RELEASE_PARTIAL))

    # -- R1-F4: a pin must name a slot the actuator can re-resolve --

    def test_f4_incomplete_pin_is_rejected(self) -> None:
        for kwargs in (
            {"role": "", "assigned_name": "n", "locator": "%1"},
            {"role": "codex", "assigned_name": "", "locator": "%1"},
            {"role": "codex", "assigned_name": "n", "locator": ""},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ReleasePinError):
                    ReleasePin(**kwargs)

    def test_f4_duplicate_slot_in_one_generation_is_rejected(self) -> None:
        dup = (
            ReleasePin(role="codex", assigned_name="mzb1_x", locator="%1"),
            ReleasePin(role="codex", assigned_name="mzb1_x", locator="%9"),
        )
        with self.assertRaises(ReleasePinError):
            validate_release_pins(dup)

    def test_f4_request_release_rejects_an_empty_pin_set(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self._hibernate(self.key_a, _decision())
        with self.assertRaises(ReleasePinError):
            self.store.request_release(
                self.key_a, expected_revision=2, action_id="act-1", pins=()
            )
        self.assertEqual(
            self.store.get(self.key_a).process_release, RELEASE_NOT_REQUESTED
        )

    # -- R1-F5: every authority write names its durable record --

    def test_f5_decision_pointer_is_validated(self) -> None:
        with self.assertRaises(DecisionPointerError):
            DecisionPointer(source="", issue_id=ISSUE, journal_id="1")
        with self.assertRaises(DecisionPointerError):
            DecisionPointer(source="asana", issue_id=ISSUE, journal_id="1")
        with self.assertRaises(DecisionPointerError):
            DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="")

    def test_f5_transition_never_inherits_a_stale_pointer(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(journal="76741"))
        self._hibernate(self.key_a, _decision(journal="76750"))
        self.assertEqual(self.store.get(self.key_a).decision_journal, "76750")
        # The rehydrate decision must name its OWN journal, not the hibernate's.
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=2,
            target=DISPOSITION_ACTIVE,
            decision=_decision(journal="76767"),
        )
        self.assertEqual(self.store.get(self.key_a).decision_journal, "76767")

    def test_f5_decision_must_bind_the_lane_s_issue(self) -> None:
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        outcome = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(issue="99999"),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(
            self.store.get(self.key_a).lane_disposition, DISPOSITION_ACTIVE
        )


class R2RegressionTest(unittest.TestCase):
    """R2-F1 (j#76781): the owner binding and the decision anchor are different facts.

    Before the correction they shared one field, so an unbound lane -- legitimately
    owning no issue -- also stored an anchor with no issue, and a Redmine journal is
    only addressable through its issue. The stored "durable pointer" pointed at
    nothing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.store = LaneLifecycleStore(home=self.home)
        self.key_a = LaneLifecycleKey(WS, LANE_A)
        self.addCleanup(self._tmp.cleanup)

    def test_an_unbound_lane_still_stores_a_complete_anchor(self) -> None:
        outcome = self.store.declare_active(
            self.key_a, decision=_decision(journal="76786"), issue_id=_UNBOUND
        )
        self.assertTrue(outcome.applied)
        record = self.store.get(self.key_a)
        self.assertEqual(record.issue_id, _UNBOUND)  # owns no issue ...
        # ... but the decision that declared it is still re-readable from Redmine.
        self.assertEqual(record.decision_source, "redmine")
        self.assertEqual(record.decision_issue_id, ISSUE)
        self.assertEqual(record.decision_journal, "76786")
        self.assertEqual(record.decision, _decision(journal="76786"))

    def test_an_anchor_without_an_issue_is_rejected(self) -> None:
        # The reproducer: this pointer cannot be re-read -- a Redmine journal is only
        # reachable as /issues/<id>.json, so an issueless anchor names nothing.
        with self.assertRaises(DecisionPointerError):
            DecisionPointer(source="redmine", issue_id="", journal_id="76741")

    def test_redmine_ids_must_be_positive_decimals(self) -> None:
        for issue, journal in (
            ("not-an-issue", "not-a-journal"),
            ("0", "76741"),
            ("-5", "76741"),
            (" ", "76741"),
            (ISSUE, "0"),
            (ISSUE, "-1"),
            (ISSUE, "abc"),
        ):
            with self.subTest(issue=issue, journal=journal):
                with self.assertRaises(DecisionPointerError):
                    DecisionPointer(
                        source="redmine", issue_id=issue, journal_id=journal
                    )

    def test_a_bound_lane_refuses_an_anchor_on_another_issue(self) -> None:
        with self.assertRaises(DecisionPointerError):
            self.store.declare_active(
                self.key_a, decision=_decision(issue="99999"), issue_id=ISSUE
            )
        self.assertIsNone(self.store.get(self.key_a))

    def test_an_unbound_lane_accepts_any_complete_anchor(self) -> None:
        # It owns no issue, so no ownership is being authorized -- only the lane's
        # own state change, which the anchor documents.
        self.store.declare_active(
            self.key_a, decision=_decision(issue="99999"), issue_id=_UNBOUND
        )
        outcome = self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(issue="12345", journal="99"),
        )
        self.assertTrue(outcome.applied)
        record = self.store.get(self.key_a)
        self.assertEqual(record.decision_issue_id, "12345")
        self.assertEqual(record.decision_journal, "99")

    def test_rehydrate_replaces_the_whole_anchor(self) -> None:
        self.store.declare_active(
            self.key_a, decision=_decision(journal="76741"), issue_id=ISSUE
        )
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(journal="76750"),
        )
        self.store.transition_disposition(
            self.key_a,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=2,
            target=DISPOSITION_ACTIVE,
            decision=_decision(journal="76786"),
        )
        record = self.store.get(self.key_a)
        self.assertEqual(record.decision_journal, "76786")
        self.assertEqual(record.decision_issue_id, ISSUE)
        self.assertEqual(record.decision, _decision(journal="76786"))

    def test_supersession_anchors_both_lanes(self) -> None:
        recovery = LaneLifecycleKey(WS, LANE_B)
        self.store.declare_active(self.key_a, decision=_decision(), issue_id=ISSUE)
        self.store.supersede_and_activate(
            superseded=self.key_a,
            expected_revision=1,
            recovery=recovery,
            decision=_decision(journal="76786"),
        )
        for key in (self.key_a, recovery):
            record = self.store.get(key)
            self.assertEqual(record.decision_issue_id, ISSUE)
            self.assertEqual(record.decision_journal, "76786")

    def test_schema_carries_a_distinct_decision_issue_column(self) -> None:
        self.store.ensure_schema()
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(lane_lifecycle_records)")
            ]
            version = conn.execute(
                "SELECT schema_version FROM state_schema_components "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("issue_id", columns)  # owner binding
        self.assertIn("decision_issue_id", columns)  # decision anchor
        self.assertEqual(version, 2)

    def test_v1_table_migrates_additively(self) -> None:
        # A v1 row's anchor kept only the journal. The migration adds the column; it
        # does not invent an issue for it.
        path = lane_lifecycle_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                "CREATE TABLE state_schema_components (component TEXT PRIMARY KEY, "
                "schema_version INTEGER NOT NULL, owner TEXT NOT NULL, "
                "recovery_policy TEXT NOT NULL, migrated_from TEXT, "
                "updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE lane_lifecycle_records ("
                "repo_workspace_id TEXT NOT NULL, lane_id TEXT NOT NULL, "
                "issue_id TEXT NOT NULL DEFAULT '', lane_disposition TEXT NOT NULL, "
                "process_release TEXT NOT NULL, revision INTEGER NOT NULL, "
                "release_action_id TEXT NOT NULL DEFAULT '', "
                "release_pins TEXT NOT NULL DEFAULT '', "
                "decision_source TEXT NOT NULL DEFAULT '', "
                "decision_journal TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY (repo_workspace_id, lane_id))"
            )
            conn.execute(
                "INSERT INTO lane_lifecycle_records VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    WS,
                    LANE_A,
                    ISSUE,
                    DISPOSITION_ACTIVE,
                    RELEASE_NOT_REQUESTED,
                    1,
                    "",
                    "",
                    "redmine",
                    "76741",
                    "2026-07-13T00:00:00+00:00",
                    "2026-07-13T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        record = LaneLifecycleStore(home=self.home).get(self.key_a)
        self.assertEqual(record.issue_id, ISSUE)
        self.assertEqual(record.decision_journal, "76741")
        # The legacy anchor is visibly incomplete, not silently back-filled.
        self.assertEqual(record.decision_issue_id, "")
        self.assertIsNone(record.decision)


class R3RegressionTest(unittest.TestCase):
    """R3 (j#76810) + R4 (j#76879): a guard that never met its own boundary conditions.

    R3-F1 -- the container guard was mistaken for a component guard, so a *newer*
    component schema was silently re-stamped as v2 and its authority rows became
    writable by a build that does not know their semantics.
    R3-F2 -- ``str.isdigit()`` is not an ASCII-decimal test, so Unicode digits were
    stored as anchors Redmine can never resolve, and ``int()`` leaked a raw
    ``ValueError`` out of an error contract that promises ``DecisionPointerError``.
    R4-F1 -- the same version guard trusted ``int()`` to read the recorded version, so
    a REAL ``2.5`` truncated to ``2`` and passed the recognized-version check.
    R5-F1 -- and it collapsed a present-but-NULL row (and a failed version query) into
    "never registered", re-stamping an unknown store as fresh.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, LANE_A)
        self.addCleanup(self._tmp.cleanup)

    def _v3_store(self) -> Path:
        """A store an imagined future build upgraded past us."""
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(self.key, decision=_decision(), issue_id=ISSUE)
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 3 "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.execute(
                "ALTER TABLE lane_lifecycle_records "
                "ADD COLUMN future_guard TEXT NOT NULL DEFAULT 'v3'"
            )
            conn.commit()
        finally:
            conn.close()
        return path

    # -- R3-F1: a newer component schema is never downgraded ------------------

    def test_f1_newer_component_schema_fails_closed(self) -> None:
        self._v3_store()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()

    def test_f1_refusal_leaves_the_store_byte_equivalent(self) -> None:
        path = self._v3_store()
        before = path.read_bytes()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(path.read_bytes(), before)

    def test_f1_version_and_future_columns_survive(self) -> None:
        path = self._v3_store()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        conn = sqlite3.connect(path)
        try:
            version = conn.execute(
                "SELECT schema_version FROM state_schema_components "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(lane_lifecycle_records)")
            ]
            rows = conn.execute(
                "SELECT COUNT(*) FROM lane_lifecycle_records"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 3)  # not re-stamped down to 2
        self.assertIn("future_guard", columns)
        self.assertEqual(rows, 1)

    def test_f1_no_write_reaches_a_newer_component(self) -> None:
        # The point of the refusal: rows here are lifecycle AUTHORITY. A build that
        # does not know v3's semantics must not be able to move them.
        self._v3_store()
        store = LaneLifecycleStore(home=self.home)
        with self.assertRaises(LaneLifecycleError):
            store.transition_disposition(
                self.key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(journal="76816"),
            )

    def test_f1_readers_fail_closed_never_assume_active(self) -> None:
        self._v3_store()
        # `absent` would read as "no owner, go ahead"; unsupported must be `unknown`.
        self.assertEqual(
            resolve_lane_owner(WS, ISSUE, home=self.home).status, OWNER_UNKNOWN
        )
        self.assertIsNone(load_lane_lifecycle(home=self.home))
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).records()

    def test_f1_a_known_v1_still_migrates(self) -> None:
        # Fail-closed on *newer* must not break the supported v1 -> v2 path.
        store = LaneLifecycleStore(home=self.home)
        store.ensure_schema()
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 1 "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        LaneLifecycleStore(home=self.home).ensure_schema()
        conn = sqlite3.connect(path)
        try:
            version = conn.execute(
                "SELECT schema_version FROM state_schema_components "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 2)

    # -- R4-F1: a present-but-malformed version is not coerced to a known one --

    def _corrupt_v2_version(self, sql_literal: str) -> Path:
        """A v2 store whose component version was overwritten with a raw SQL value."""
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(self.key, decision=_decision(), issue_id=ISSUE)
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = "
                + sql_literal
                + " WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.execute(
                "ALTER TABLE lane_lifecycle_records "
                "ADD COLUMN future_guard TEXT NOT NULL DEFAULT 'future'"
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_f1_real_2_5_is_not_truncated_to_2(self) -> None:
        # int(2.5) == 2 would pass the {1, 2} check and re-stamp an unknown schema.
        path = self._corrupt_v2_version("2.5")
        before = path.read_bytes()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(path.read_bytes(), before)
        conn = sqlite3.connect(path)
        try:
            storage_class, value = conn.execute(
                "SELECT typeof(schema_version), schema_version "
                "FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(lane_lifecycle_records)")
            ]
            rows = conn.execute(
                "SELECT COUNT(*) FROM lane_lifecycle_records"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(storage_class, "real")  # not re-stamped to integer 2
        self.assertEqual(value, 2.5)
        self.assertIn("future_guard", columns)
        self.assertEqual(rows, 1)

    def test_f1_malformed_versions_fail_closed(self) -> None:
        # Every present-but-non-integer shape is unknown, not a coerced number.
        for literal in ("2.5", "1.9", "'abc'", "x'02'"):
            with self.subTest(literal=literal):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    key = LaneLifecycleKey(WS, LANE_A)
                    store = LaneLifecycleStore(home=home)
                    store.declare_active(key, decision=_decision(), issue_id=ISSUE)
                    path = lane_lifecycle_path(home)
                    conn = sqlite3.connect(path)
                    try:
                        conn.execute(
                            "UPDATE state_schema_components SET schema_version = "
                            + literal
                            + " WHERE component = ?",
                            (LANE_LIFECYCLE_COMPONENT,),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    with self.assertRaises(LaneLifecycleError):
                        LaneLifecycleStore(home=home).ensure_schema()

    def test_f1_malformed_version_readers_fail_closed(self) -> None:
        self._corrupt_v2_version("2.5")
        self.assertEqual(
            resolve_lane_owner(WS, ISSUE, home=self.home).status, OWNER_UNKNOWN
        )
        self.assertIsNone(load_lane_lifecycle(home=self.home))
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).transition_disposition(
                self.key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(journal="76887"),
            )

    # -- R5-F1: a present NULL row is not "never registered" -----------------

    def _null_version_store(self) -> Path:
        """A v2 store whose component row exists but records a NULL version.

        Rebuilds ``state_schema_components`` with a nullable ``schema_version`` (an
        imagined future shape) so the NULL can be stored, then keeps the lane's
        authority row and a future column.
        """
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(self.key, decision=_decision(), issue_id=ISSUE)
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE state_schema_components RENAME TO _old")
            conn.execute(
                "CREATE TABLE state_schema_components ("
                "component TEXT PRIMARY KEY, schema_version INTEGER, owner TEXT, "
                "recovery_policy TEXT, migrated_from TEXT, updated_at TEXT)"
            )
            conn.execute("INSERT INTO state_schema_components SELECT * FROM _old")
            conn.execute("DROP TABLE _old")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = NULL "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.execute(
                "ALTER TABLE lane_lifecycle_records "
                "ADD COLUMN future_guard TEXT NOT NULL DEFAULT 'future'"
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_f1_present_null_version_fails_closed(self) -> None:
        # A present row with a NULL version is unknown, not "never registered".
        path = self._null_version_store()
        before = path.read_bytes()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(path.read_bytes(), before)
        conn = sqlite3.connect(path)
        try:
            storage_class, value = conn.execute(
                "SELECT typeof(schema_version), schema_version "
                "FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(lane_lifecycle_records)")
            ]
            rows = conn.execute(
                "SELECT COUNT(*) FROM lane_lifecycle_records"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(storage_class, "null")  # not re-stamped to integer 2
        self.assertIsNone(value)
        self.assertIn("future_guard", columns)
        self.assertEqual(rows, 1)

    def test_f1_present_null_version_readers_fail_closed(self) -> None:
        self._null_version_store()
        self.assertEqual(
            resolve_lane_owner(WS, ISSUE, home=self.home).status, OWNER_UNKNOWN
        )
        self.assertIsNone(load_lane_lifecycle(home=self.home))
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).transition_disposition(
                self.key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(journal="76901"),
            )

    def test_f1_version_query_failure_is_not_treated_as_fresh(self) -> None:
        # A query failing after the container was initialized is a broken store, not
        # a fresh one -- it must not fall through to the create/register path.
        from mozyo_bridge.core.state import lane_lifecycle_schema as schema

        class _BoomConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.DatabaseError("boom")

        self.assertEqual(schema._recorded_version(_BoomConn()), schema._VERSION_MALFORMED)
        self.assertNotIn(schema._VERSION_MALFORMED, schema._RECOGNIZED_SCHEMA_VERSIONS)

    def test_f1_absent_row_is_still_a_fresh_create(self) -> None:
        # The one case that IS fresh: no component row at all.
        LaneLifecycleStore(home=self.home).ensure_schema()
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            version = conn.execute(
                "SELECT schema_version FROM state_schema_components "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 2)

    def test_f1_integer_stored_as_real_2_0_is_still_accepted(self) -> None:
        # SQLite folds a lossless REAL 2.0 to integer 2; that is a real v2, not
        # malformed -- the guard must not become so strict it rejects a valid store.
        path = self._corrupt_v2_version("2.0")
        conn = sqlite3.connect(path)
        try:
            storage_class = conn.execute(
                "SELECT typeof(schema_version) FROM state_schema_components "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(storage_class, "integer")  # SQLite already folded it
        LaneLifecycleStore(home=self.home).ensure_schema()  # no raise

    # -- R3-F2: the id validator has a closed error contract ------------------

    def test_f2_unicode_digits_are_rejected(self) -> None:
        # Every one of these is `str.isdigit() == True` -- and none is a Redmine id.
        for value in ("²", "１２", "١٢"):
            with self.subTest(value=value):
                self.assertTrue(value.isdigit())  # the trap, pinned
                with self.assertRaises(DecisionPointerError):
                    DecisionPointer(
                        source="redmine", issue_id=value, journal_id="76741"
                    )
                with self.assertRaises(DecisionPointerError):
                    DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=value)

    def test_f2_oversize_input_raises_the_declared_error(self) -> None:
        # CPython raises a raw ValueError converting a huge string with int(); the
        # contract says every rejection is a DecisionPointerError.
        huge = "1" * 5000
        with self.assertRaises(DecisionPointerError):
            DecisionPointer(source="redmine", issue_id=huge, journal_id="76741")
        with self.assertRaises(DecisionPointerError):
            DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=huge)

    def test_f2_zero_and_empty_are_rejected(self) -> None:
        for value in ("", "0", "00", " ", "-1", "1.0", "1e3", "12a"):
            with self.subTest(value=value):
                with self.assertRaises(DecisionPointerError):
                    DecisionPointer(
                        source="redmine", issue_id=value, journal_id="76741"
                    )

    def test_f2_a_corrupt_anchor_reads_as_none_not_a_raw_error(self) -> None:
        # The docstring on `.decision` promises None for an unreadable anchor. Before
        # the fix a Unicode-digit anchor made simply *reading* the row raise.
        store = LaneLifecycleStore(home=self.home)
        store.ensure_schema()
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            conn.execute(
                "INSERT INTO lane_lifecycle_records (repo_workspace_id, lane_id, "
                "issue_id, lane_disposition, process_release, revision, "
                "release_action_id, release_pins, decision_source, "
                "decision_issue_id, decision_journal, created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?, 1, '', '', 'redmine', ?, '76741', ?, ?)",
                (
                    WS,
                    LANE_B,
                    DISPOSITION_ACTIVE,
                    RELEASE_NOT_REQUESTED,
                    "²",
                    "2026-07-13T00:00:00+00:00",
                    "2026-07-13T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        record = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE_B))
        self.assertEqual(record.decision_issue_id, "²")
        self.assertIsNone(record.decision)

    def test_f2_valid_ids_still_pass(self) -> None:
        pointer = DecisionPointer(source="redmine", issue_id="13689", journal_id="1")
        self.assertEqual(pointer.issue_id, "13689")
        self.assertEqual(pointer.journal_id, "1")


class FailClosedReadTest(unittest.TestCase):
    """An unusable store must never read as "active" / "no conflict"."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _corrupt(self) -> None:
        path = lane_lifecycle_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database")

    def test_owner_lookup_on_a_corrupt_store_is_unknown_not_absent(self) -> None:
        # `absent` would read as "no other owner, go ahead"; `unknown` blocks.
        self._corrupt()
        self.assertEqual(resolve_lane_owner(WS, ISSUE, home=self.home).status, OWNER_UNKNOWN)

    def test_load_on_a_corrupt_store_is_none_not_empty(self) -> None:
        # `()` would read as "no lanes"; `None` says "we cannot know".
        self._corrupt()
        self.assertIsNone(load_lane_lifecycle(home=self.home))

    def test_store_raises_on_a_corrupt_db(self) -> None:
        self._corrupt()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).records()

    def test_wrappers_read_a_healthy_store(self) -> None:
        LaneLifecycleStore(home=self.home).declare_active(
            LaneLifecycleKey(WS, LANE_A), decision=_decision(), issue_id=ISSUE
        )
        self.assertEqual(resolve_lane_owner(WS, ISSUE, home=self.home).lane_id, LANE_A)
        self.assertEqual(len(load_lane_lifecycle(home=self.home)), 1)


class ReadonlyLoaderGuardTest(unittest.TestCase):
    """R3-F1 (j#77307): the non-creating reader honours the schema-version downgrade guard."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.store = LaneLifecycleStore(home=self.home)

    def _seed_row(self) -> None:
        self.store.declare_active(
            LaneLifecycleKey(WS, LANE_A), decision=_decision(), issue_id=ISSUE
        )

    def test_absent_store_reads_empty_without_creating(self) -> None:
        # No store file at all: () and nothing created.
        self.assertEqual(load_lane_lifecycle_readonly(home=self.home), ())
        self.assertFalse(lane_lifecycle_path(self.home).exists())

    def test_recognized_version_reads_rows(self) -> None:
        self._seed_row()
        rows = load_lane_lifecycle_readonly(home=self.home)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 1)

    def test_unsupported_component_version_fails_closed(self) -> None:
        # A future / unknown component schema must fail closed to None — never read the
        # authority rows under semantics this build does not understand (matching the
        # guarded write path). Mirrors load_lane_lifecycle's behaviour.
        self._seed_row()
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 999 "
                "WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(load_lane_lifecycle(home=self.home))  # guarded path
        self.assertIsNone(load_lane_lifecycle_readonly(home=self.home))  # readonly path

    def test_table_present_without_metadata_fails_closed(self) -> None:
        # A partial store (lifecycle table exists, but its component metadata row is gone)
        # is unsupported, not a fresh empty read.
        self._seed_row()
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            conn.execute(
                "DELETE FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(load_lane_lifecycle_readonly(home=self.home))


if __name__ == "__main__":
    unittest.main()
