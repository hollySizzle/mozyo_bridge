"""Redmine #14475 — the empty ``worktree_identity`` close-before-fence regression.

The live failure this pins (#14462 j#88463): a TestPyPI-installed ``sublane recover-gateway
--execute`` ran against a lane whose lifecycle ``worktree_identity`` was EMPTY. The read-only
preflight had no launch-authority axis at all, so it reported ``actionable`` and every fence
green; the guarded actuation then closed the exact old gateway, and only THEN — at the
action-time launch authority join — refused with ``preservation_blocked:
launch_authority_moved``. The destructive leg had run; the recovery leg never could.

Two independent defects, pinned separately:

* **the producer** — a ``sublane supersede`` handover mints the recovery lane's ``active``
  lifecycle row itself with an intentionally empty ``worktree_identity`` ("written when that
  lane is actually created"), but the later create's ``declare_active`` hits the existing row,
  returns ``already_declared`` zero-write, and the outcome was discarded. The canonical token
  the create had already computed never reached the row;
* **the fence placement** — the launch authority was joined only AFTER the close.

The tests below are ordered producer-first, then the pre-close fence, then the boundary the
fence must NOT move (a genuinely LATE authority move still stops post-close, because no
pre-close read can predict it).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    CAS_ALREADY_DECLARED,
    DISPOSITION_ACTIVE,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E402,E501
    declare_created_lane_lifecycle,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E402,E501
    GatewayRefreshObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_LAUNCH_AUTHORITY,
    REFRESH_BLOCK_STALE_GENERATION,
    REFRESH_BLOCK_UNKNOWN,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    TURN_CLASS_FAILED,
    TURN_CLASS_PRODUCTIVE,
    decide_gateway_refresh,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E402,E501
    LAUNCH_AUTHORITY_BLOCKERS,
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_REASONS,
    LAUNCH_AUTHORITY_UNKNOWN,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    launch_authority_current,
    launch_authority_runbook,
    normalize_launch_authority_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E402,E501
    REFRESH_STATUS_COMPLETED,
    REFRESH_STATUS_REFUSED,
    REFRESH_STATUS_STOPPED,
)

# The ONE canonical set of gateway-refresh fakes lives with the #14203 regression; reuse it
# rather than minting a second, so a contract change breaks both suites together.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    LAUNCH_ERROR,
)
from tests.regressions.test_issue_14203_gateway_refresh import (  # noqa: E402
    GATEWAY,
    FakeGatewayOps,
    _RefreshCase,
)

# `release check tree` strict-fails on a home-path-shaped literal in ANY tracked file; the
# leaky-error-string fixture composes that shape at runtime instead.
from private_path_fixtures import macos_home_path  # noqa: E402

# The canonical all-facts-hold target observation. Negative controls SUBTRACT from this one
# builder rather than being hand-assembled, so a later axis added to the decision cannot leave
# a hand-built fixture vacuously green.
def _target(**overrides) -> GatewayRefreshObservation:
    facts = dict(
        identity_resolved=True,
        is_lane_implementation_gateway=True,
        issue_lane_matches=True,
        generation_matches=True,
        settled_idle=True,
        composer_clear=True,
        resume_anchor_present=True,
        worker_distinct_preserved=True,
        no_authority_conflict=True,
        launch_authority_current=True,
    )
    facts.update(overrides)
    return GatewayRefreshObservation(**facts)


class SupersededLaneWorktreeBindingTests(unittest.TestCase):
    """The producer: a create MUST bind the supersede-minted row's empty worktree."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.key = LaneLifecycleKey("ws1", "issue_14475_lane_r1")
        self.issue = "14475"
        self.token = "wt_canonical_token_14475"
        self._orig_store = LaneLifecycleStore
        # ``declare_created_lane_lifecycle`` constructs its stores with no ``home``; point
        # both at the isolated test home so no real state store is touched.
        self._patch_homes()

    def _patch_homes(self):
        import mozyo_bridge.core.state.lane_declaration as decl_mod
        import mozyo_bridge.core.state.lane_lifecycle as life_mod

        home = self.home
        real_life_init = life_mod.LaneLifecycleStore.__init__
        real_decl_init = decl_mod.LaneDeclarationStore.__init__

        def life_init(inner, *, home=None, path=None):
            real_life_init(inner, home=Path(home) if home else Path(home_default), path=path)

        home_default = str(home)

        def decl_init(inner, *, home=None, path=None):
            real_decl_init(inner, home=Path(home) if home else Path(home_default), path=path)

        life_mod.LaneLifecycleStore.__init__ = life_init
        decl_mod.LaneDeclarationStore.__init__ = decl_init
        self.addCleanup(
            lambda: setattr(life_mod.LaneLifecycleStore, "__init__", real_life_init)
        )
        self.addCleanup(
            lambda: setattr(decl_mod.LaneDeclarationStore, "__init__", real_decl_init)
        )

    def _store(self) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=self.home)

    def _mint_supersede_style_row(self) -> int:
        """An ``active`` generation-1 row with EMPTY worktree + slots.

        This is byte-for-byte the shape the supersede handover INSERTs for a recovery lane
        (``lane_lifecycle.py`` supersede branch: worktree binding and declared slots are left
        on their empty defaults, to be "written when that lane is actually created").
        """
        outcome = self._store().declare_active(
            self.key,
            decision=DecisionPointer(
                source="redmine", issue_id=self.issue, journal_id="88465"
            ),
            issue_id=self.issue,
            worktree_identity="",
        )
        self.assertTrue(outcome.applied)
        record = self._store().get(self.key)
        self.assertEqual(record.worktree_identity, "")
        self.assertEqual(record.declared_slots, "")
        return record.revision

    def _declare(self, *, token: str, journal: str = "88465") -> None:
        declare_created_lane_lifecycle(
            repo_workspace_id=self.key.repo_workspace_id,
            lane_label=self.key.lane_id,
            issue=self.issue,
            journal=journal,
            worktree_identity=token,
        )

    def test_a_create_binds_the_supersede_minted_rows_empty_worktree(self):
        # The #14475 defect: declare_active is refused ``already_declared`` on the
        # supersede-minted row, and pre-fix the create's canonical token was dropped on the
        # floor. It must now land through the bounded backfill CAS.
        revision = self._mint_supersede_style_row()
        self._declare(token=self.token)
        record = self._store().get(self.key)
        self.assertEqual(record.worktree_identity, self.token)
        self.assertEqual(record.revision, revision + 1)
        # Only the binding moved: disposition / generation / issue / decision are untouched.
        self.assertEqual(record.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(record.lane_generation, 1)
        self.assertEqual(record.issue_id, self.issue)
        self.assertEqual(record.decision_journal, "88465")

    def test_the_backfill_is_idempotent_and_stops_bumping_revisions(self):
        # A self-heal re-run (#13378) must converge: the second create sees the binding
        # already exactly present and writes nothing.
        self._mint_supersede_style_row()
        self._declare(token=self.token)
        settled = self._store().get(self.key).revision
        self._declare(token=self.token)
        record = self._store().get(self.key)
        self.assertEqual(record.worktree_identity, self.token)
        self.assertEqual(record.revision, settled)

    def test_an_established_different_binding_is_never_overwritten(self):
        # The anti-regression for the relaxation this fix must NOT be: a row already bound to
        # a DIFFERENT worktree is a divergent re-declare, and stays zero-write.
        self._mint_supersede_style_row()
        self._declare(token=self.token)
        before = self._store().get(self.key)
        self._declare(token="wt_some_other_lane_token")
        after = self._store().get(self.key)
        self.assertEqual(after.worktree_identity, self.token)
        self.assertEqual(after.revision, before.revision)

    def test_a_create_with_no_token_never_guesses_a_binding(self):
        self._mint_supersede_style_row()
        self._declare(token="")
        self.assertEqual(self._store().get(self.key).worktree_identity, "")

    def test_a_row_owned_by_a_different_issue_is_zero_write(self):
        # The backfill target must own THIS exact issue; a foreign row is never coerced.
        self._mint_supersede_style_row()
        before = self._store().get(self.key)
        declare_created_lane_lifecycle(
            repo_workspace_id=self.key.repo_workspace_id,
            lane_label=self.key.lane_id,
            issue="99999",
            journal="88465",
            worktree_identity=self.token,
        )
        after = self._store().get(self.key)
        self.assertEqual(after.worktree_identity, "")
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.issue_id, self.issue)

    def test_the_refusal_this_backfill_rides_is_really_already_declared(self):
        # Pins the premise rather than assuming it: the surface the fix hangs off is the
        # ``already_declared`` refusal, not some other CAS reason.
        self._mint_supersede_style_row()
        outcome = self._store().declare_active(
            self.key,
            decision=DecisionPointer(
                source="redmine", issue_id=self.issue, journal_id="88465"
            ),
            issue_id=self.issue,
            worktree_identity=self.token,
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_ALREADY_DECLARED)
        # ... and the bounded surface the fix reuses is the #13809 one, unchanged.
        self.assertTrue(
            hasattr(LaneDeclarationStore(home=self.home), "backfill_active_binding")
        )


class LaunchAuthorityVocabularyTests(unittest.TestCase):
    """The typed axis vocabulary: closed, fail-closed, and secret-safe."""

    def test_only_the_exact_ok_token_authorizes_a_launch(self):
        self.assertTrue(launch_authority_current(LAUNCH_AUTHORITY_OK))
        for token in sorted(LAUNCH_AUTHORITY_BLOCKERS):
            with self.subTest(token=token):
                self.assertFalse(launch_authority_current(token))

    def test_off_vocabulary_tokens_collapse_to_unknown_and_refuse(self):
        # Fail-closed in the direction that matters: an unrecognized reason must never be
        # promoted to "current", and the raw string is never carried onward (so an error
        # string bearing a path or a secret cannot reach a durable record through this path).
        leaky = "sqlite3.OperationalError: " + macos_home_path("op", "state.sqlite") + " locked"
        for raw in ("", "   ", "OK", "okay", leaky):
            with self.subTest(raw=raw):
                self.assertEqual(
                    normalize_launch_authority_reason(raw), LAUNCH_AUTHORITY_UNKNOWN
                )
                self.assertFalse(launch_authority_current(raw))
        # Surrounding whitespace is stripped, as in the sibling
        # ``normalize_turn_failure_reason`` — these tokens are produced by this codebase's own
        # evaluator, not parsed from operator input, so padding tolerance is the convention
        # here (unlike the deliberately byte-exact ``lane_kind`` check, review j#85852 F1).
        self.assertEqual(normalize_launch_authority_reason(" ok "), LAUNCH_AUTHORITY_OK)

    def test_every_blocker_carries_a_runbook_and_ok_carries_none(self):
        self.assertEqual(launch_authority_runbook(LAUNCH_AUTHORITY_OK), "")
        for token in sorted(LAUNCH_AUTHORITY_BLOCKERS):
            with self.subTest(token=token):
                self.assertTrue(launch_authority_runbook(token).strip())
        # An off-vocabulary token yields the ``unknown`` runbook, never an echo of the input.
        self.assertEqual(
            launch_authority_runbook("../../etc/passwd"),
            launch_authority_runbook(LAUNCH_AUTHORITY_UNKNOWN),
        )

    def test_no_reason_token_leaks_a_path_or_identity_shape(self):
        # Every token is an axis NAME. A token carrying a path / home / URL separator would
        # mean the vocabulary had started echoing observed state into durable records.
        for token in sorted(LAUNCH_AUTHORITY_REASONS):
            with self.subTest(token=token):
                self.assertNotIn("/", token)
                self.assertNotIn("\\", token)
                self.assertNotIn(":", token)
                self.assertEqual(token, token.strip().lower())


class PreCloseLaunchAuthorityFenceTests(unittest.TestCase):
    """The decision: an unbound / mismatched lane blocks BEFORE any close."""

    def test_an_unbound_lane_is_a_zero_close_blocker_not_actionable(self):
        # THE #14462 j#88463 shape: every other fence green, a classified failed turn, and an
        # empty lifecycle worktree binding. Pre-fix this returned ``actionable``.
        self.assertEqual(
            decide_gateway_refresh(
                _target(launch_authority_current=False), TURN_CLASS_FAILED
            ),
            REFRESH_BLOCK_LAUNCH_AUTHORITY,
        )

    def test_the_same_axis_covers_mismatch_and_unbound_alike(self):
        # The decision consumes the boolean projection, so both live reasons — an unbound row
        # and a wrong-worktree mismatch — reach it as the same zero-close blocker.
        for reason in (LAUNCH_AUTHORITY_WORKTREE_UNBOUND, LAUNCH_AUTHORITY_WORKTREE_MISMATCH):
            with self.subTest(reason=reason):
                self.assertEqual(
                    decide_gateway_refresh(
                        _target(launch_authority_current=launch_authority_current(reason)),
                        TURN_CLASS_FAILED,
                    ),
                    REFRESH_BLOCK_LAUNCH_AUTHORITY,
                )

    def test_a_bound_current_lane_still_reaches_actionable(self):
        # The negative control for the fence itself: with the axis green the verdict is
        # unchanged, so the new gate cannot be trivially satisfied by blocking everything.
        self.assertEqual(
            decide_gateway_refresh(_target(), TURN_CLASS_FAILED), REFRESH_ACTIONABLE
        )

    def test_the_axis_defaults_to_blocking(self):
        # A caller that never joins the axis must not ride a green default.
        self.assertFalse(GatewayRefreshObservation().launch_authority_current)

    def test_a_productive_turn_still_reports_turn_not_failed(self):
        # Deliberate ordering: the launch-authority gate sits AFTER the turn classification,
        # so a lane that needs NO refresh is not renamed as a launch-authority problem.
        self.assertEqual(
            decide_gateway_refresh(
                _target(launch_authority_current=False), TURN_CLASS_PRODUCTIVE
            ),
            REFRESH_BLOCK_TURN_NOT_FAILED,
        )

    def test_a_more_fundamental_identity_blocker_still_wins(self):
        # ... and the identity/generation fences stay ahead of it: a stale generation on an
        # unbound lane is still named ``stale_generation``.
        self.assertEqual(
            decide_gateway_refresh(
                _target(generation_matches=False, launch_authority_current=False),
                TURN_CLASS_FAILED,
            ),
            REFRESH_BLOCK_STALE_GENERATION,
        )

    def test_the_axis_is_carried_in_the_emitted_payload(self):
        # The operator-visible preflight JSON must show the axis, otherwise an approval can
        # again be written against a green-looking observation.
        self.assertIs(
            _target(launch_authority_current=False).as_payload()["launch_authority_current"],
            False,
        )

    def test_with_launch_authority_replaces_only_that_axis(self):
        joined = _target(settled_idle=False).with_launch_authority(False)
        self.assertFalse(joined.launch_authority_current)
        self.assertFalse(joined.settled_idle)
        self.assertTrue(joined.identity_resolved)
        self.assertTrue(joined.worker_distinct_preserved)


class ZeroCloseUseCaseTests(_RefreshCase):
    """The end-to-end obligation: ``--execute`` on an unbound lane closes NOTHING.

    Subclasses the #14203 case (and reuses its canonical fakes) deliberately: a second set of
    hand-built fakes is how a regression test drifts into asserting against a fixture instead
    of against the surface under test.
    """

    def _ops(self, reason: str) -> "FakeGatewayOps":
        ops = FakeGatewayOps()
        ops.lane_authority_reason = lambda request, _r=reason: _r  # type: ignore[assignment]
        return ops

    def test_an_unbound_lane_refuses_execute_with_zero_close(self):
        # THE regression. Pre-fix: preflight actionable -> close committed -> post-close
        # ``launch_authority_moved``. Now: refused before any actuation, nothing closed,
        # nothing launched, nothing sent.
        ops = self._ops(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertFalse(outcome.closed_old_gateway)
        self.assertFalse(outcome.fresh_slot_attested)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(self.port.launched, [])
        self.assertEqual(ops.resumes, [])

    def test_a_mismatched_worktree_also_refuses_with_zero_close(self):
        ops = self._ops(LAUNCH_AUTHORITY_WORKTREE_MISMATCH)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(self.port.launched, [])

    def test_the_refusal_names_the_axis_and_its_runbook(self):
        # The operator must learn WHICH axis stopped it — the opaque boolean is what let
        # j#88461 record "9 fences green" for a lane that could not be relaunched.
        outcome = self._use_case(
            self._ops(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        ).run(self._request(), execute=True)
        self.assertIn(LAUNCH_AUTHORITY_WORKTREE_UNBOUND, outcome.detail)
        self.assertIn(
            launch_authority_runbook(LAUNCH_AUTHORITY_WORKTREE_UNBOUND), outcome.detail
        )

    def test_the_read_only_preflight_reports_the_blocker_too(self):
        # The approval is written from the PREFLIGHT. If the preflight still said
        # ``actionable`` the owner would authorize the same destructive run again.
        outcome = self._use_case(
            self._ops(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        ).run(self._request(), execute=False)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertFalse(outcome.executed)
        self.assertIs(outcome.observation["launch_authority_current"], False)
        self.assertEqual(self.port.closed, [])

    def test_an_unbound_lane_with_no_prior_transaction_actuates_nothing(self):
        # An unbound lane whose old locator has ALSO vanished. ``identity_unknown`` is the
        # more fundamental gate and still wins the VERDICT (the ordered contract is
        # unchanged); what this pins is that no post-close replay is admitted without a
        # committed-close transaction, so nothing is closed, launched, or sent.
        ops = FakeGatewayOps(target=GatewayRefreshObservation())
        ops.lane_authority_reason = (  # type: ignore[assignment]
            lambda request: LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_UNKNOWN)
        self.assertFalse(outcome.post_close_resume)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(self.port.launched, [])
        self.assertEqual(ops.resumes, [])

    def test_a_genuine_post_close_replay_stays_stopped_and_re_closes_nothing(self):
        # Requirement 4: the committed-close post-close replay contract must NOT be weakened.
        # Stage the EXACT #14462 j#88463 state — close committed, launch owed — by failing the
        # launch leg, then replay with the authority now unbound. The replay must stay
        # ``stopped`` (never a fabricated completion), must not close a second time, and must
        # not land a launch. The pre-close fence protects the FIRST close; it cannot and must
        # not un-commit one that already happened.
        staged = self._ops(LAUNCH_AUTHORITY_OK)
        self.port.launch_result[
            (GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
             GATEWAY["assigned_name"])
        ] = LAUNCH_ERROR
        first = self._use_case(staged).run(self._request(), execute=True)
        self.assertEqual(first.status, REFRESH_STATUS_STOPPED)
        self.assertTrue(first.closed_old_gateway)
        self.assertEqual(len(self.port.closed), 1)

        replay = FakeGatewayOps(target=GatewayRefreshObservation())
        replay.lane_authority_reason = (  # type: ignore[assignment]
            lambda request: LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        outcome = self._use_case(replay).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertFalse(outcome.fresh_slot_attested)
        self.assertEqual(len(self.port.closed), 1)  # zero ADDITIONAL close
        self.assertEqual(replay.resumes, [])

    def test_an_ops_adapter_that_raises_is_unknown_and_refuses(self):
        # Fail-closed at the seam: a probe that blows up is never a green axis.
        ops = FakeGatewayOps()

        def _boom(request):
            raise RuntimeError("unreadable lifecycle store")

        ops.lane_authority_reason = _boom  # type: ignore[assignment]
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_LAUNCH_AUTHORITY)
        self.assertIn(LAUNCH_AUTHORITY_UNKNOWN, outcome.detail)
        self.assertEqual(self.port.closed, [])

    def test_an_exact_bound_lane_still_closes_launches_attests_and_resumes_once(self):
        # The positive control (requirement (c)): with the axis green the whole guarded
        # sequence still runs and the EXISTING anchor resumes exactly once. Without this the
        # zero-close assertions above would pass on a surface that simply never actuates.
        ops = self._ops(LAUNCH_AUTHORITY_OK)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.verdict, REFRESH_ACTIONABLE)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertTrue(outcome.closed_old_gateway)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(len(self.port.closed), 1)
        self.assertEqual(len(self.port.launched), 1)
        self.assertEqual(len(ops.resumes), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
