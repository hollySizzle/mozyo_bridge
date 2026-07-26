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
    LAUNCH_AUTHORITY_BRANCH_DRIFTED,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE,
    LAUNCH_AUTHORITY_WORKTREE_UNREADABLE,
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
from herdr_workspace_fixtures import anchored_repo_root  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E402,E501
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worktree_binding_repair import (  # noqa: E402,E501
    BLOCK_ALREADY_BOUND,
    BLOCK_BRANCH_DRIFTED,
    BLOCK_FOREIGN_WORKSPACE,
    BLOCK_NOT_HIBERNATED,
    BLOCK_BINDING_KIND,
    BLOCK_INVALID_PINS,
    BLOCK_PINS_NOT_CANONICAL,
    BLOCK_PROJECT_SCOPE,
    BLOCK_RELEASE_NOT_SETTLED,
    BLOCK_REPLACEMENT_IN_FLIGHT,
    BLOCK_WORKTREE_NOT_ROOT,
    BLOCK_WORKTREE_UNREADABLE,
    BLOCK_WRONG_ISSUE,
    REPAIR_APPLIED,
    REPAIR_PREFLIGHT,
    _current_branch,
    run_worktree_binding_repair,
)

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E402,E501
    BLOCK_WORKTREE_BINDING,
    EFFECT_CLOSED,
    EFFECT_REDISPATCHED,
    EFFECT_RELAUNCHED,
    EFFECT_RESUME_COMMITTED,
    REDISPATCH_ALREADY,
    REDISPATCH_DELIVERED,
    REDISPATCH_SKIPPED,
)
from tests.regressions.test_issue_13847_pair_recovery_orchestration import (  # noqa: E402
    GATEWAY_ROLE,
    WORKER_ROLE,
    _FakeOps,
    _REQ,
    _absent,
    _obs,
    _use_case,
)
from tests.regressions.test_issue_13879_hibernated_bound_pin_repair import (  # noqa: E402
    _seed_hibernated_released_bound,
)
from mozyo_bridge.core.state.lane_pin_role import (  # noqa: E402
    PIN_ROLE_GATEWAY,
    PIN_ROLE_WORKER,
)

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


class RecoverPairWorktreeBindingTests(unittest.TestCase):
    """Review j#88477 F1-1: ``recover-pair`` must not relaunch into an unbound lane.

    Reuses the #13847 canonical fakes so the axis is exercised against the SAME orchestration
    the pre-existing recover-pair contract is pinned by, not a private mock of it.
    """

    def _ops(self, reason=None):
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        if reason is not None:
            ops._worktree_binding_reason = reason
        return ops

    def test_an_unbound_lane_blocks_with_zero_close_and_zero_relaunch(self):
        # THE #14462 path this finding reopened: hibernated, pins present, both slots
        # recoverable — and the lane bound to no worktree. Pre-fix this ran the whole
        # close -> relaunch -> resume -> active sequence and handed an unbound row onward.
        ops = self._ops(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertFalse(out.preflight.may_recover)
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)
        self.assertIsNone(ops.redispatched)

    def test_a_mismatched_worktree_also_blocks_with_zero_effect(self):
        ops = self._ops(LAUNCH_AUTHORITY_WORKTREE_MISMATCH)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)

    def test_the_blocker_names_the_axis_and_carries_a_runbook(self):
        out = _use_case(self._ops(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)).run(
            _REQ, execute=True
        )
        self.assertIn(
            f"{BLOCK_WORKTREE_BINDING}:{LAUNCH_AUTHORITY_WORKTREE_UNBOUND}",
            out.preflight.blocked_reasons,
        )
        payload = out.preflight.as_payload()
        self.assertIs(payload["worktree_binding_current"], False)
        self.assertEqual(
            payload["worktree_binding_reason"], LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        self.assertTrue(payload["worktree_binding_runbook"].strip())

    def test_an_ops_adapter_without_the_probe_fails_closed(self):
        # A pre-#14475 adapter must not ride a green default.
        ops = self._ops()
        del type(ops).lane_worktree_binding_reason
        try:
            out = _use_case(ops).run(_REQ, execute=True)
            self.assertTrue(out.is_blocked)
            self.assertEqual(
                out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_UNKNOWN
            )
            self.assertEqual(ops.closed, [])
            self.assertFalse(ops.relaunched)
        finally:
            type(ops).lane_worktree_binding_reason = (
                lambda self, *, lane, record: getattr(
                    self, "_worktree_binding_reason", LAUNCH_AUTHORITY_OK
                )
            )

    def test_a_raising_probe_fails_closed(self):
        ops = self._ops()
        ops.lane_worktree_binding_reason = lambda **_kw: (_ for _ in ()).throw(
            RuntimeError("lifecycle unreadable")
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_UNKNOWN)
        self.assertEqual(ops.closed, [])

    def test_a_bound_lane_still_recovers(self):
        # The positive control: with the axis green the pre-existing recovery still runs, so
        # the zero-effect assertions above are not passing on a surface that never actuates.
        ops = self._ops(LAUNCH_AUTHORITY_OK)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, out.detail)
        self.assertTrue(out.preflight.may_recover)
        self.assertTrue(ops.relaunched)


class PinBearingUnboundRowRecoveryTests(SupersededLaneWorktreeBindingTests):
    """Review j#88477 F1-2: the documented runbook must work on a PIN-BEARING unbound row.

    The #14462 shape. Inherits the store-home isolation of the producer case above.
    """

    def _pins(self):
        from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin

        return (
            ProcessGenerationPin(
                role="implementation_gateway", provider="codex",
                assigned_name="mzb1_ws1_codex_issue_14475_lane_r1", locator="w3N:p1K",
                runtime_revision="1",
            ),
            ProcessGenerationPin(
                role="implementation_worker", provider="claude",
                assigned_name="mzb1_ws1_claude_issue_14475_lane_r1", locator="w3N:p1M",
                runtime_revision="1",
            ),
        )

    def _mint_pin_bearing_unbound_row(self):
        """An ``active`` row with pins present and worktree EMPTY — the #14462 state."""
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore

        outcome = LaneDeclarationStore(home=self.home).declare_lane(
            self.key,
            decision=DecisionPointer(
                source="redmine", issue_id=self.issue, journal_id="88465"
            ),
            issue_id=self.issue,
            declared_slots=self._pins(),
            worktree_identity="",
        )
        self.assertTrue(outcome.applied)
        record = self._store().get(self.key)
        self.assertEqual(record.worktree_identity, "")
        self.assertTrue(record.declared_slots, "the row must carry pins for this shape")
        return record

    def test_a_create_rerun_binds_a_pin_bearing_unbound_row(self):
        # THE F1-2 regression. Pre-fix the backfill passed the EMPTY slot set, so the store's
        # "non-empty different slot snapshot" fence made this a guaranteed zero-write and the
        # documented runbook ("re-run the lane's own declaration surface") could never work.
        before = self._mint_pin_bearing_unbound_row()
        self._declare(token=self.token)
        after = self._store().get(self.key)
        self.assertEqual(after.worktree_identity, self.token)
        self.assertEqual(after.revision, before.revision + 1)

    def test_the_backfill_preserves_the_pins_byte_for_byte(self):
        # The binding is filled WITHOUT rewriting a pin: the snapshot must be untouched.
        before = self._mint_pin_bearing_unbound_row()
        self._declare(token=self.token)
        after = self._store().get(self.key)
        self.assertEqual(after.declared_slots, before.declared_slots)
        self.assertEqual(
            [(p.role, p.provider, p.assigned_name, p.locator) for p in after.declared_pins],
            [(p.role, p.provider, p.assigned_name, p.locator) for p in before.declared_pins],
        )

    def test_it_is_idempotent_on_a_pin_bearing_row(self):
        self._mint_pin_bearing_unbound_row()
        self._declare(token=self.token)
        settled = self._store().get(self.key).revision
        self._declare(token=self.token)
        self.assertEqual(self._store().get(self.key).revision, settled)

    def test_a_pin_bearing_row_already_bound_elsewhere_is_never_rebound(self):
        # The clobber fence still stands on the pin-bearing shape too.
        self._mint_pin_bearing_unbound_row()
        self._declare(token=self.token)
        before = self._store().get(self.key)
        self._declare(token="wt_some_other_lane_token")
        after = self._store().get(self.key)
        self.assertEqual(after.worktree_identity, self.token)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.declared_slots, before.declared_slots)


class HibernatedWorktreeRepairChainTests(unittest.TestCase):
    """Review j#88490 F1-3: the hibernated blocker's runbook must name a REAL public action.

    Pins the whole chain in one place, because that is the claim being made: a hibernated,
    pin-bearing, worktree-unbound row -> the public repair command -> the token recorded with
    the pins byte-unchanged -> ``recover-pair`` preflight green. Before this surface existed
    the chain had no middle step: ``backfill_active_binding`` is active-only, so the blocker
    named a recovery nobody could perform.
    """

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.issue = "14475"
        self.lane = "issue_14475_repair_chain_r1"
        # An ANCHORED root (the #13924 CI-hermeticity fixture): the workspace identity is a
        # fact the fixture states through the production writer, so the production resolver
        # still runs on its real read path but no ambient operator registration participates.
        self.repo = anchored_repo_root(self, workspace_id="fixture-14475-workspace")
        self._init_lane_worktree(self.repo)
        # The lane record is keyed on the REAL resolved workspace of the lane worktree, exactly
        # as ``sublane create`` stamps it. A synthetic id would make the workspace axis under
        # test unsatisfiable, and the chain would be green only against a shape production
        # never produces.
        self.workspace_id = repo_scope_workspace_id(self.repo)
        self.assertTrue(self.workspace_id, "the anchored lane worktree must resolve a workspace")
        self.key = LaneLifecycleKey(self.workspace_id, self.lane)

    def _init_lane_worktree(self, root: Path, *, branch: str | None = None):
        """Make ``root`` a real git checkout whose branch IS the lane id.

        The branch is the evidence axis the command demands; the anchor (written by
        :func:`anchored_repo_root`) is the workspace axis.
        """
        import subprocess

        name = branch or self.lane
        for argv in (
            ["git", "init", "-q", "-b", name, str(root)],
            ["git", "-C", str(root), "config", "user.email", "t@example.invalid"],
            ["git", "-C", str(root), "config", "user.name", "t"],
            ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "base"],
        ):
            subprocess.run(argv, check=True, capture_output=True)

    def _pins(self):
        from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin

        return (
            # CANONICAL pin roles (``lane_pin_role.PIN_ROLE_GATEWAY`` / ``PIN_ROLE_WORKER``).
            # Review j#88505 F1: the previous fixture used the role-profile spelling, which the
            # real use case's pin-role boundary rejects as ``foreign_pin_role`` — so a chain
            # test built on it could never have reached the axis it claimed to prove.
            ProcessGenerationPin(
                role=PIN_ROLE_GATEWAY, provider="codex",
                assigned_name="mzb1_wsrepair_codex_" + self.lane, locator="w3N:p1K",
                runtime_revision="1",
            ),
            ProcessGenerationPin(
                role=PIN_ROLE_WORKER, provider="claude",
                assigned_name="mzb1_wsrepair_claude_" + self.lane, locator="w3N:p1M",
                runtime_revision="1",
            ),
        )

    def _mint_hibernated_unbound_row(self, *, worktree_identity=""):
        """hibernated + released + pins present + worktree EMPTY — the unconverged shape.

        Built with the #13879 suite's OWN seed helper, driven through the real store
        transitions, so this chain cannot be green against a hand-assembled row shape the
        production surfaces would never produce.
        """
        from mozyo_bridge.core.state.lane_lifecycle import (
            DISPOSITION_HIBERNATED,
            LaneLifecycleStore,
        )

        _seed_hibernated_released_bound(
            path=LaneLifecycleStore(home=self.home).path,
            key=self.key,
            issue=self.issue,
            worktree_identity=worktree_identity,
            declared_slots=self._pins(),
        )
        record = self._record()
        self.assertEqual(record.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(record.worktree_identity, worktree_identity)
        self.assertTrue(record.declared_slots)
        return record

    def _repair(self, *, execute, **overrides):
        kw = dict(
            repo_root=self.repo, issue=self.issue, lane=self.lane, journal="88490",
            worktree=str(self.repo), execute=execute,
            lifecycle_home=self.home,
        )
        kw.update(overrides)
        return run_worktree_binding_repair(**kw)

    def _record(self):
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore

        return LaneLifecycleStore(home=self.home).get(self.key)

    def test_the_active_only_backfill_really_cannot_reach_a_hibernated_row(self):
        # Pins the PREMISE this whole surface exists for, rather than assuming it.
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
        from mozyo_bridge.core.state.lane_lifecycle_model import CAS_UNEXPECTED_STATE

        record = self._mint_hibernated_unbound_row()
        outcome = LaneDeclarationStore(home=self.home).backfill_active_binding(
            self.key, expected_revision=record.revision, issue_id=self.issue,
            worktree_identity="wt_token_for_this_lane", declared_slots=self._pins(),
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_UNEXPECTED_STATE)

    def test_the_full_chain_repair_then_recover_pair_preflight_green(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        before = self._mint_hibernated_unbound_row()

        # 1. the read-only preflight recognises the shape and writes nothing
        pre = self._repair(execute=False)
        self.assertEqual(pre.state, REPAIR_PREFLIGHT)
        self.assertEqual(self._record().worktree_identity, "")

        # 2. the public repair records the canonical token — and ONLY that
        done = self._repair(execute=True)
        self.assertEqual(done.state, REPAIR_APPLIED, done.detail)
        after = self._record()
        self.assertEqual(
            after.worktree_identity, derive_lane_workspace_token(str(self.repo))
        )
        self.assertEqual(after.declared_slots, before.declared_slots)  # pins byte-unchanged
        self.assertEqual(after.lane_disposition, before.lane_disposition)
        self.assertEqual(after.lane_generation, before.lane_generation)

        # 3. the recover-pair worktree axis the blocker fenced on is now green, from the SAME
        #    live probe the real adapter uses — the point of the chain.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
            LiveHibernatedPairRecoveryOps,
        )

        ops = LiveHibernatedPairRecoveryOps(
            repo_root=self.repo, request_issue=self.issue, request_lane=self.lane,
            request_journal="88490", lifecycle_home=self.home,
        )
        self.assertEqual(
            ops.lane_worktree_binding_reason(lane=self.lane, record=after),
            LAUNCH_AUTHORITY_OK,
        )
        # ...and it was NOT green before the repair (the negative half of the same probe).
        self.assertEqual(
            ops.lane_worktree_binding_reason(lane=self.lane, record=before),
            LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
        )

    def test_the_repair_is_idempotent(self):
        self._mint_hibernated_unbound_row()
        self.assertEqual(self._repair(execute=True).state, REPAIR_APPLIED)
        settled = self._record().revision
        self.assertEqual(self._repair(execute=True).state, REPAIR_APPLIED)
        self.assertEqual(self._record().revision, settled)

    def test_an_already_bound_lane_is_refused_and_never_rebound(self):
        # Seed the row ALREADY bound to a different worktree's token, then repair from this
        # (workspace- and branch-valid) worktree: every other axis passes, so the already-bound
        # fence is what is actually being measured. This surface fills a gap; it never moves a
        # lane to another worktree.
        bound = self._mint_hibernated_unbound_row(
            worktree_identity="wt_some_other_lane_token"
        )
        out = self._repair(execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_ALREADY_BOUND)
        self.assertEqual(
            self._record().worktree_identity, bound.worktree_identity
        )

    def test_a_worktree_on_the_wrong_branch_is_refused(self):
        # The command never trusts an asserted --worktree: it demands positive evidence.
        import subprocess

        self._mint_hibernated_unbound_row()
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "-b", "some_other_branch"],
            check=True, capture_output=True,
        )
        out = self._repair(execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_BRANCH_DRIFTED)
        self.assertEqual(self._record().worktree_identity, "")

    def test_a_non_checkout_worktree_is_refused(self):
        self._mint_hibernated_unbound_row()
        out = self._repair(execute=True, worktree=str(self.home / "not-a-checkout"))
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_WORKTREE_UNREADABLE)
        self.assertEqual(self._record().worktree_identity, "")

    def test_an_active_row_is_routed_away_from_this_surface(self):
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore

        LaneDeclarationStore(home=self.home).declare_lane(
            self.key,
            decision=DecisionPointer(
                source="redmine", issue_id=self.issue, journal_id="88490"
            ),
            issue_id=self.issue, declared_slots=self._pins(), worktree_identity="",
        )  # active, pins present, unbound — the #13809 backfill's target, not this one's
        out = self._repair(execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_NOT_HIBERNATED)
        self.assertEqual(self._record().worktree_identity, "")

    def test_a_foreign_repo_with_the_same_branch_name_is_refused(self):
        """Review j#88493: a branch NAME is not an identity.

        The fail-open this closes: any repository can hold a branch called ``issue_…``. With
        only "some git checkout whose branch matches", a FOREIGN repo's token could be written
        into this workspace's lane row — the exact class of defect (an unverified binding) the
        whole ticket exists to prevent.
        """
        import subprocess

        self._mint_hibernated_unbound_row()
        foreign = anchored_repo_root(self, workspace_id="fixture-14475-foreign")
        self._init_lane_worktree(foreign)
        # Same branch name, real checkout, checkout ROOT — and still not this lane.
        self.assertEqual(
            _current_branch(str(foreign)), _current_branch(str(self.repo))
        )
        out = self._repair(execute=True, worktree=str(foreign))
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_FOREIGN_WORKSPACE)
        self.assertEqual(self._record().worktree_identity, "")

    def test_a_subdirectory_of_the_right_worktree_is_refused(self):
        # A subdir answers the same branch query but derives a DIFFERENT canonical token, so
        # accepting it would bind the lane to a token no launch would ever re-derive.
        self._mint_hibernated_unbound_row()
        subdir = self.repo / "nested"
        subdir.mkdir()
        out = self._repair(execute=True, worktree=str(subdir))
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_WORKTREE_NOT_ROOT)
        self.assertEqual(self._record().worktree_identity, "")

    def test_the_repair_journal_becomes_the_rows_decision_anchor(self):
        """Review j#88493 item 2: state the decision-anchor contract, do not leave it implied.

        This component's rule (``transition_disposition`` R1-F5) is that a row always names
        the durable record that put it in its CURRENT state. The repair therefore re-anchors
        the decision to its own approval journal — the same behaviour as the #13879 sibling.
        My j#88492 verdict said the anchor was preserved; that statement was wrong, and this
        test is what stops the contract from being ambiguous again.
        """
        before = self._mint_hibernated_unbound_row()
        self.assertNotEqual(before.decision_journal, "88490")
        self._repair(execute=True, journal="88490")
        after = self._record()
        self.assertEqual(after.decision_journal, "88490")
        self.assertEqual(after.decision_issue_id, self.issue)
        # ...and the axes that ARE preserved really are.
        self.assertEqual(after.declared_slots, before.declared_slots)
        self.assertEqual(after.lane_disposition, before.lane_disposition)
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.process_release, before.process_release)

    def test_an_unborn_head_is_unreadable_even_when_the_lane_is_named_default(self):
        """The raw-branch fence, at the one shape that actually reaches it.

        ``_norm_lane("")`` yields ``"default"``. A checkout with NO commits answers
        ``rev-parse --abbrev-ref HEAD`` with an error (empty), so normalizing BEFORE the
        emptiness check turns "this worktree has no branch" into "this worktree is on the
        default lane" — and for a lane literally named ``default`` that passes the branch
        fence and binds the row to a token derived from an unborn repository. Checking the
        RAW value first is what closes it.
        """
        import subprocess

        lane = "default"
        root = anchored_repo_root(self, workspace_id="fixture-14475-unborn")
        subprocess.run(["git", "init", "-q", "-b", lane, str(root)],
                       check=True, capture_output=True)  # no commit -> unborn HEAD
        self.assertEqual(_current_branch(str(root)), "", "the fixture must have no branch")
        key = LaneLifecycleKey(repo_scope_workspace_id(root), lane)
        _seed_hibernated_released_bound(
            path=LaneLifecycleStore(home=self.home).path, key=key, issue=self.issue,
            worktree_identity="", declared_slots=self._pins(),
        )
        out = run_worktree_binding_repair(
            repo_root=root, issue=self.issue, lane=lane, journal="88493",
            worktree=str(root), execute=True, lifecycle_home=self.home,
        )
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.reason, BLOCK_WORKTREE_UNREADABLE)
        self.assertEqual(
            LaneLifecycleStore(home=self.home).get(key).worktree_identity, ""
        )

    def test_a_symlink_alias_records_the_same_canonical_token(self):
        """Review j#88494: the token must come from the CANONICAL root.

        A symlink to the worktree root passes the root and workspace gates (git resolves it),
        so hashing the raw ``--worktree`` string would record a token derived from the alias.
        Every later live probe resolves the path before deriving, so that binding would read
        as ``worktree_identity_mismatch`` forever — a repair that writes a binding no launch
        can ever satisfy is worse than the gap it closed.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
            LiveHibernatedPairRecoveryOps,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        self._mint_hibernated_unbound_row()
        alias = Path(tempfile.mkdtemp()) / "lane-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.addCleanup(alias.unlink, True)
        self.assertNotEqual(str(alias), str(self.repo))

        # Repair THROUGH the alias...
        done = self._repair(execute=True, worktree=str(alias))
        self.assertEqual(done.state, REPAIR_APPLIED, done.detail)
        # ...and the recorded token is the canonical root's, not the alias string's.
        self.assertEqual(
            self._record().worktree_identity,
            derive_lane_workspace_token(str(self.repo.resolve())),
        )

        # The live probe agrees from BOTH paths — the property the mismatch would break.
        record = self._record()
        for root in (self.repo, alias):
            with self.subTest(root=str(root)):
                ops = LiveHibernatedPairRecoveryOps(
                    repo_root=root, request_issue=self.issue, request_lane=self.lane,
                    request_journal="88494", lifecycle_home=self.home,
                )
                self.assertEqual(
                    ops.lane_worktree_binding_reason(lane=self.lane, record=record),
                    LAUNCH_AUTHORITY_OK,
                )

    # -- the REAL use case, not just the probe (review j#88505 F1) --------------

    def _readable_empty_inventory_env(self, *, live_pair: bool = False) -> dict:
        """Env pointing at the stateful fake herdr CLI.

        ``live_pair`` seeds the two declared slots as live-but-unattested, so the recovery
        has something to close; the default empty inventory makes them vanished.
        """
        import json
        import stat

        from tests.support.herdr_fake import FakeHerdr

        # ``"> "`` renders a readable, NON-pending composer; the default render text reads as
        # real unsent input and would preserve (never close) every slot.
        fake = FakeHerdr(read_text="> ")
        ws = fake.seed_workspace(cwd=str(self.repo))
        if live_pair:
            # Live-but-UNATTESTED slots at the declared assigned names: they classify as the
            # pair's own bad generation with a real locator, so the recovery actually CLOSES
            # them. Without this every slot is ``slot_absent`` (nothing to close) and the
            # per-close fence would never be exercised.
            # Seeded under the DERIVED assigned name the observer computes (not the pin's
            # stored one) — that derivation is what resolves a live slot.
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
                encode_assigned_name,
            )

            for pin in self._pins():
                fake.seed_agent(
                    encode_assigned_name(self.workspace_id, pin.provider, self.lane),
                    workspace_id=ws, provider=pin.provider, cwd=str(self.repo),
                )
        state_path = self.home / "herdr-state.json"
        state_path.write_text(json.dumps(fake.to_state()), encoding="utf-8")
        adapter = Path(__file__).resolve().parents[2] / "smoke" / "support" / "fake_herdr_cli.py"
        binary = self.home / "fake-herdr"
        # The state path is baked into the wrapper rather than passed through the ops env:
        # the inventory read spawns the binary and the fake CLI reads the variable from ITS
        # own environment, so relying on propagation would leave it unset.
        binary.write_text(
            f'#!/bin/sh\nMOZYO_FAKE_HERDR_STATE="{state_path}" '
            f'exec python3 "{adapter}" "$@"\n',
            encoding="utf-8",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        return {"MOZYO_HERDR_BINARY": str(binary)}

    def _real_use_case(self, *, live_pair: bool = False):
        """The production ``SublaneRecoverPairUseCase`` over the LIVE ops and this store.

        The R2 chain test asserted against the live probe directly, so it never exercised
        ``may_recover`` / the actuation at all. Driving the real use case is the only way the
        "close / relaunch / resume / send are all 0" claim can actually be measured.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            SublaneRecoverPairUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
            LiveHibernatedPairRecoveryOps,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
            LiveSublaneResumeOps,
            SublaneResumeUseCase,
        )

        # Assembled exactly like ``build_live_recover_pair_use_case``, with the stores pointed
        # at this test's isolated home. A test-only assembly could diverge from the shape the
        # product actually runs.
        store = LaneLifecycleStore(home=self.home)
        # A READABLE but empty inventory. Without it every slot classifies
        # ``preserve_ambiguous`` (an unreadable inventory fails closed), ``may_recover`` is
        # False for that reason alone, and a "zero close / relaunch" assertion would pass even
        # with the binding axis deleted — vacuous. Empty + readable makes both declared slots
        # ``slot_absent`` -> SLOT_RECOVER, so ``may_recover`` turns on the binding axis alone.
        # ``list_herdr_agent_rows`` resolves the binary from the ENV (a runner injection does
        # not reach it), so this uses the repo's stateful fake-herdr CLI fixture.
        env = self._readable_empty_inventory_env(live_pair=live_pair)
        ops = LiveHibernatedPairRecoveryOps(
            repo_root=self.repo, request_issue=self.issue, request_lane=self.lane,
            request_journal="88505", lifecycle_home=self.home, env=env,
        )
        resume = SublaneResumeUseCase(
            ops=LiveSublaneResumeOps(repo_root=self.repo, env=env), store=store
        )
        return SublaneRecoverPairUseCase(ops=ops, store=store, resume=resume), ops

    def _recover_request(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            RecoverPairRequest,
        )

        return RecoverPairRequest(
            issue=self.issue, lane=self.lane, journal="88505",
            implementation_request_journal="88465",
        )

    def _switch_branch(self, name: str):
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "-b", name],
            check=True, capture_output=True,
        )

    def test_the_real_use_case_blocks_a_wrong_branch_checkout_with_zero_effect(self):
        """THE j#88505 F1 reproduction, through the production use case.

        Canonical token, canonical pins, hibernated+released — and the checkout switched to
        another branch. The token still matches (it is derived from the PATH), so a probe that
        checks only the token reports ``ok`` and the recovery would relaunch the pair onto
        whatever is checked out there.
        """
        self._mint_hibernated_unbound_row()
        self._repair(execute=True)  # bind it, so the token axis is genuinely satisfied
        self._switch_branch("wrong_branch")
        use_case, ops = self._real_use_case()
        out = use_case.run(self._recover_request(), execute=True)
        self.assertTrue(out.is_blocked)
        self.assertFalse(out.preflight.may_recover)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_BRANCH_DRIFTED
        )
        self.assertIn(
            f"{BLOCK_WORKTREE_BINDING}:{LAUNCH_AUTHORITY_BRANCH_DRIFTED}",
            out.preflight.blocked_reasons,
        )
        self.assertFalse(out.executed)
        self.assertEqual(out.closed_roles, ())
        self.assertFalse(out.relaunched)
        self.assertIsNone(out.resume)
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)

    def test_the_real_use_case_blocks_an_unreadable_root_with_zero_effect(self):
        import shutil

        self._mint_hibernated_unbound_row()
        self._repair(execute=True)
        shutil.rmtree(self.repo / ".git")  # still a directory, no longer a checkout
        use_case, ops = self._real_use_case()
        out = use_case.run(self._recover_request(), execute=True)
        self.assertTrue(out.is_blocked)
        self.assertFalse(out.preflight.may_recover)
        self.assertIn(
            out.preflight.worktree_binding_reason,
            (LAUNCH_AUTHORITY_WORKTREE_UNREADABLE, LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE),
        )
        self.assertFalse(out.executed)
        self.assertEqual(out.closed_roles, ())
        self.assertFalse(out.relaunched)

    def test_the_real_use_case_blocks_an_unbound_row_with_zero_effect(self):
        # The same measurement for the axis R2 already had, now through the real use case.
        self._mint_hibernated_unbound_row()
        use_case, ops = self._real_use_case()
        out = use_case.run(self._recover_request(), execute=True)
        self.assertTrue(out.is_blocked)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        self.assertEqual(out.closed_roles, ())
        self.assertFalse(out.relaunched)

    def test_the_real_use_case_reaches_the_binding_axis_at_all(self):
        """The premise check: canonical pins must actually resolve, or every assertion above
        would be passing on ``hibernated_record_missing_pins`` instead of the axis it names."""
        self._mint_hibernated_unbound_row()
        self._repair(execute=True)
        use_case, ops = self._real_use_case()
        out = use_case.run(self._recover_request(), execute=False)
        self.assertTrue(
            out.preflight.record_has_pins,
            "the fixture's pins must resolve through the canonical pin-role boundary",
        )
        self.assertEqual(out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_OK)

    def test_a_binding_that_drifts_after_the_preflight_stops_before_any_close(self):
        """The action-time re-join (j#88505 F1): a drift between preflight and actuation.

        The preflight axis was read before the operator decided; the destructive close is what
        must be fenced. The use case re-reads immediately before it.
        """
        self._mint_hibernated_unbound_row()
        self._repair(execute=True)
        use_case, ops = self._real_use_case()
        seen = {"n": 0}
        real = ops.lane_worktree_binding_reason

        def drifting(*, lane, record):
            seen["n"] += 1
            if seen["n"] == 1:
                return LAUNCH_AUTHORITY_OK  # preflight: current
            return LAUNCH_AUTHORITY_BRANCH_DRIFTED  # action-time: moved

        ops.lane_worktree_binding_reason = drifting  # type: ignore[assignment]
        try:
            out = use_case.run(self._recover_request(), execute=True)
        finally:
            ops.lane_worktree_binding_reason = real  # type: ignore[assignment]
        self.assertGreaterEqual(seen["n"], 2, "the actuation must re-join the axis")
        # The reported preflight carries the ACTION-TIME axis, not the admitting read — the
        # same discipline as the refresh's typed reason: report what stopped it, not the
        # observation the run no longer reflects.
        self.assertFalse(out.preflight.may_recover)
        self.assertIn("moved between preflight and actuation", out.detail)
        self.assertFalse(out.executed)
        self.assertEqual(out.closed_roles, ())
        self.assertFalse(out.relaunched)
        self.assertIsNone(out.resume)
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_BRANCH_DRIFTED
        )

    # -- F2: the preflight must project the STORE's whole readable signature ----

    def test_an_unsettled_release_is_a_preflight_blocker_not_an_execute_surprise(self):
        """Review j#88505 F2: a dry-run green an owner could approve from.

        With ``process_release='requested'`` the store's CAS refuses, but the preflight used to
        report "--execute would record". A false green is the same class of defect this whole
        ticket is about: a preflight that does not predict its own effect.
        """
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            RELEASE_PARTIAL,
            RELEASE_REQUESTED,
        )

        for target in (RELEASE_REQUESTED, RELEASE_PARTIAL):
            with self.subTest(release=target):
                self.setUp()  # a fresh anchored lane per release shape
                _seed_hibernated_released_bound(
                    path=LaneLifecycleStore(home=self.home).path, key=self.key,
                    issue=self.issue, worktree_identity="",
                    declared_slots=self._pins(), release_target=target,
                )
                pre = self._repair(execute=False)
                self.assertTrue(pre.is_blocked, "the preflight must not report a false green")
                self.assertEqual(pre.reason, BLOCK_RELEASE_NOT_SETTLED)
                # ...and the execute agrees with it, having written nothing.
                out = self._repair(execute=True)
                self.assertTrue(out.is_blocked)
                self.assertEqual(out.reason, BLOCK_RELEASE_NOT_SETTLED)
                self.assertEqual(self._record().worktree_identity, "")

    def test_the_preflight_and_the_execute_agree_on_the_exact_signature(self):
        # The positive control for F2: on the exact signature the preflight's green is real.
        self._mint_hibernated_unbound_row()
        self.assertEqual(self._repair(execute=False).state, REPAIR_PREFLIGHT)
        self.assertEqual(self._repair(execute=True).state, REPAIR_APPLIED)
        self.assertTrue(self._record().worktree_identity)

    def _pair_ops_with_scripted_binding(self, script):
        """The #13847 canonical fakes (live, closable slots) with a scripted binding axis.

        The REAL ``SublaneRecoverPairUseCase`` is what is under test here — j#88526 F1 is about
        WHERE that use case re-joins the axis. The live adapter's slot observation is covered
        separately; driving it here would need an attested live pair and would measure the
        observer, not the fence placement.
        """
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        ops.lane_worktree_binding_reason = script  # type: ignore[assignment]
        return ops

    def test_a_drift_after_the_first_close_stops_every_remaining_effect(self):
        """Review j#88526 F1: the fence must guard EVERY destructive effect, not the first.

        R3 re-joined the axis once before the close loop, so a checkout switched between the
        first close and the relaunch let the pair be relaunched onto the wrong branch — the
        reviewer's injection produced closed_roles=(gateway, worker) / relaunched / redispatched
        with the reported axis still ``ok``.
        """
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            # Joins, in order: preflight build, pre-loop action-time, before close#1,
            # before close#2, before relaunch. Drift from the 4th, i.e. after ONE close.
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 3 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        ops = self._pair_ops_with_scripted_binding(drifting)
        out = _use_case(ops).run(_REQ, execute=True)

        self.assertGreaterEqual(calls["n"], 3, "each destructive effect must re-join the axis")
        self.assertEqual(len(ops.closed), 1, "exactly the first close ran")
        self.assertEqual(len(out.closed_roles), 1)
        self.assertFalse(ops.relaunched)
        self.assertIsNone(out.resume)
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)
        self.assertIsNone(ops.redispatched)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_BRANCH_DRIFTED
        )
        self.assertIn("moved during actuation", out.detail)
        # The partial close stays reported, so a re-run can converge it.
        self.assertTrue(out.executed)

    def test_a_drift_before_the_relaunch_stops_the_relaunch(self):
        # Both closes ran under a current axis; the relaunch is the next irreversible effect.
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            # Both close joins hold; the 5th join — the relaunch's — is the one that moves.
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 4 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        ops = self._pair_ops_with_scripted_binding(drifting)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(len(ops.closed), 2, "both closes ran under a current axis")
        self.assertFalse(ops.relaunched, "the relaunch must not ride a moved binding")
        self.assertIsNone(out.resume)
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_BRANCH_DRIFTED
        )

    def test_a_stable_binding_still_completes_the_whole_recovery(self):
        # The positive control: the per-effect fence must not stop a healthy recovery.
        ops = self._pair_ops_with_scripted_binding(
            lambda *, lane, record: LAUNCH_AUTHORITY_OK
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertFalse(out.is_blocked, out.detail)
        self.assertEqual(len(ops.closed), 2)
        self.assertTrue(ops.relaunched)

    def test_a_drift_after_the_relaunch_stops_the_resume_and_the_send(self):
        """Review j#88532 F1: the resume and the send are NOT checkout-independent.

        The resume flips the lane to ``active`` on the premise that the fresh pair stands in
        THIS worktree (its own preflight never re-reads the branch), and the send delivers the
        work anchor to that pair. R4 re-joined only through the relaunch, so a branch moved
        afterwards still reached the active flip and the delivery.
        """
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            # Joins: preflight build, pre-loop, close#1, close#2, relaunch, resume, send.
            # Everything through the relaunch holds; the resume's join is the one that moves.
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 5 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        ops = self._pair_ops_with_scripted_binding(drifting)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(len(ops.closed), 2)
        self.assertTrue(ops.relaunched, "the relaunch ran under a current axis")
        self.assertIsNone(out.resume, "the active flip must not ride a moved binding")
        self.assertIsNone(ops.redispatched, "and nothing may be sent")
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_BRANCH_DRIFTED
        )
        # The relaunch stays reported, so a re-run resumes from there.
        self.assertTrue(out.relaunched)

    def test_a_drift_after_the_resume_stops_the_send(self):
        # The resume applied; the send is the last owed effect and gets its own join.
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 6 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        ops = self._pair_ops_with_scripted_binding(drifting)
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertTrue(ops.relaunched)
        self.assertIsNotNone(out.resume, "the resume ran under a current axis")
        self.assertIsNone(ops.redispatched, "the send must be zero")
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)
        self.assertEqual(
            out.preflight.worktree_binding_reason, LAUNCH_AUTHORITY_BRANCH_DRIFTED
        )

    def test_the_transport_direct_fence_cancels_the_reserve_on_a_moved_binding(self):
        """The live ops' own ``pre_send_authority`` seam (review j#88532 F1).

        The seam existed and neither redispatch caller passed it. It fires AFTER the outbox
        reserve is won and BEFORE transport, so a moved binding is a typed zero-send with the
        reserve cancelled — never a reservation left dangling as an unresolved send fate.
        """
        import tempfile as _tempfile
        from unittest.mock import patch

        from mozyo_bridge.core.state.dispatch_outbox_fence import (
            DispatchOutboxFence,
            FenceKey,
            dispatch_outbox_fence_path,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_FAILED,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        with _tempfile.TemporaryDirectory() as tmp:
            fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(Path(tmp)))
            fence.bootstrap()
            ops = live.LiveHibernatedPairRecoveryOps(
                repo_root=self.repo, request_issue=self.issue, request_lane=self.lane,
                request_journal="88532", lifecycle_home=self.home, fence=fence,
            )
            gw = encode_assigned_name(self.workspace_id, "codex", self.lane)
            sent = []

            def _deliver(_self, request):
                sent.append(request)
                raise AssertionError("transport must not be reached on a moved binding")

            # The gateway MUST resolve, otherwise the send would fail for want of a target and
            # this case would pass with the fence removed (measured: it did).
            gw_row = {
                "name": gw, "pane_id": "wZ:p3G", "agent_status": "idle",
                "cwd": str(self.repo), "revision": "7",
            }
            # ``deliver`` is patched to FAIL if it is ever reached: without that, an unwired
            # seam lets the real transport run and fail for its own reasons, and this case
            # passes with the fence removed (measured: it did).
            with patch.object(
                live.LiveHibernatedPairRecoveryOps,
                "_checkout_authority_current", return_value=False,
            ), patch.object(
                live, "list_herdr_agent_rows", return_value=[gw_row],
            ), patch.object(
                delivery_live.LiveRecoveryAnchorDeliveryService, "deliver", _deliver,
            ):
                result = ops.redispatch_to_gateway(
                    action_id="recover-pair:14475:x:1:1", gateway_assigned_name=gw,
                    issue=self.issue, lane=self.lane, journal="88465",
                    workspace_id=self.workspace_id,
                )
            self.assertEqual(result.status, REDISPATCH_FAILED)
            self.assertEqual(sent, [], "zero transport")
            key = FenceKey(
                workspace_id=self.workspace_id, lane_id=self.lane, issue=self.issue,
                journal="88465", action_id="recover-pair:14475:x:1:1",
                target_assigned_name=gw,
            )
            self.assertEqual(
                fence.state_of(key), "cancelled",
                "the reserve must be cancelled, not left as an unresolved send fate",
            )

            # Premise control: with the axis CURRENT the same setup reaches the transport, so
            # the assertions above measure the fence and not an unresolvable target.
            reached = []

            def _deliver_ok(_self, request):
                reached.append(request)
                raise RuntimeError("transport reached")

            with patch.object(
                live.LiveHibernatedPairRecoveryOps,
                "_checkout_authority_current", return_value=True,
            ), patch.object(
                live, "list_herdr_agent_rows", return_value=[gw_row],
            ), patch.object(
                delivery_live.LiveRecoveryAnchorDeliveryService, "deliver", _deliver_ok,
            ):
                ops.redispatch_to_gateway(
                    action_id="recover-pair:14475:x:1:2", gateway_assigned_name=gw,
                    issue=self.issue, lane=self.lane, journal="88465",
                    workspace_id=self.workspace_id,
                )
            self.assertEqual(len(reached), 1, "the axis is the only thing that stopped it")

    def test_a_whitespace_only_binding_blocks_in_preflight_and_execute_alike(self):
        """Review j#88532 F2: the already-bound predicate lives OUTSIDE the classifier.

        The classifier closed the four in-signature shapes, but this residual predicate kept
        the old normalized comparison, so a row persisted as ``'   '`` read as unbound in the
        preflight and was refused ``repair_cas_refused`` at execute — the same false green,
        just outside the classifier.
        """
        self._mint_hibernated_unbound_row()
        before = self._record()
        self._raw_update("worktree_identity", "   ")
        pre = self._repair(execute=False)
        self.assertTrue(pre.is_blocked, "the preflight must not report a false green")
        self.assertEqual(pre.reason, BLOCK_ALREADY_BOUND)
        out = self._repair(execute=True)
        self.assertEqual(out.reason, BLOCK_ALREADY_BOUND)
        after = self._record()
        self.assertEqual(after.worktree_identity, "   ", "zero write")
        self.assertEqual(after.revision, before.revision)

    def test_the_store_partitions_every_classifier_axis(self):
        """Totality: a new classifier token must not fall through the store's mapping.

        Recommended by j#88532. The store maps signature axes onto two CAS reasons; an axis in
        neither set would be silently accepted by the CAS while the command still blocked it.
        """
        from mozyo_bridge.core.state import lane_worktree_binding_repair as store_mod
        from mozyo_bridge.core.state.lane_worktree_binding_signature import (
            SIGNATURE_BLOCKERS,
        )

        partitioned = (
            store_mod._UNEXPECTED_STATE_SIGNATURES
            | store_mod._FORBIDDEN_TRANSITION_SIGNATURES
        )
        self.assertEqual(
            sorted(SIGNATURE_BLOCKERS - partitioned), [],
            "every signature blocker must map to a CAS refusal reason",
        )
        self.assertEqual(
            sorted(partitioned - SIGNATURE_BLOCKERS), [],
            "the store must not partition tokens the classifier does not define",
        )

    # The resume-commit seam is measured in the canonical resume suite
    # (``test_sublane_resume.test_a_moved_commit_authority_leaves_the_active_flip_zero``),
    # over that surface's own harness rather than a private fake of its ops protocol.

    def test_a_drift_during_the_delivery_preflight_is_a_zero_send(self):
        """Review j#88538 F1: the LAST external observation before transport is the delivery
        preflight, so the re-join has to sit after it — not at the reserve edge."""
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
            DETAIL_AUTHORITY_MOVED,
            DISPOSITION_ZERO_SEND,
            KIND_REPLY,
            RecoveryAnchorDeliveryRequest,
        )

        drives = []

        class _Rail:
            def drive_turn_start(self, *a, **kw):
                drives.append(a)
                raise AssertionError("transport must not be reached on a moved authority")

        service = delivery_live.LiveRecoveryAnchorDeliveryService(
            repo_root=self.repo, env={}, attestation_home=self.home,
            pre_transport_authority=lambda: False,
        )
        request = RecoveryAnchorDeliveryRequest(
            issue=self.issue, journal="88465", kind=KIND_REPLY,
            workspace_id=self.workspace_id, lane_id=self.lane, provider="codex",
            target_assigned_name="mzb1_x_codex_" + self.lane, target_locator="wZ:p3G",
            target_revision="7", target_action_id="a",
        )
        ready = delivery_live._DeliveryPreflight(
            rail=_Rail(), marker="[marker]", blocker=None,
        )
        with patch.object(
            delivery_live.LiveRecoveryAnchorDeliveryService, "_preflight", return_value=ready
        ), patch.object(
            delivery_live.LiveRecoveryAnchorDeliveryService, "_record", return_value=None
        ):
            outcome = service.deliver(request)
        self.assertEqual(outcome.disposition, DISPOSITION_ZERO_SEND)
        self.assertEqual(outcome.detail, DETAIL_AUTHORITY_MOVED)
        self.assertEqual(drives, [], "zero transport")

        # Premise control: with the authority CURRENT the same setup reaches transport, so the
        # assertions above measure the seam and not an unusable rail.
        service_ok = delivery_live.LiveRecoveryAnchorDeliveryService(
            repo_root=self.repo, env={}, attestation_home=self.home,
            pre_transport_authority=lambda: True,
        )
        with patch.object(
            delivery_live.LiveRecoveryAnchorDeliveryService, "_preflight", return_value=ready
        ), patch.object(
            delivery_live.LiveRecoveryAnchorDeliveryService, "_record", return_value=None
        ):
            # ``drive_turn_start`` failures are absorbed into an ``uncertain`` outcome, so the
            # reach is measured by the recorded call, not by the exception escaping.
            service_ok.deliver(request)
        self.assertEqual(len(drives), 1, "the authority is the only thing that stopped it")

    def test_a_post_relaunch_drift_reports_executed_truthfully(self):
        """Review j#88538 F2: ``executed`` must describe what this run actually applied.

        VANISHED slots, so there are ZERO closes — deriving ``executed`` from the closes then
        reports ``False`` on a run that has already relaunched the pair. (Measured: with the
        close-only derivation this case is exactly what stays green in the earlier tests,
        because those close two slots first.)
        """
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            # Joins with no closable slot: preflight build, pre-loop, relaunch, resume, send.
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 3 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _absent(), WORKER_ROLE: _absent()})
        ops.lane_worktree_binding_reason = drifting  # type: ignore[assignment]
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(ops.closed, [], "vanished slots are never closed")
        self.assertTrue(ops.relaunched, "but the pair WAS relaunched")
        self.assertIsNone(out.resume, "and the resume was fenced")
        self.assertTrue(
            out.executed, "a run that relaunched the pair has executed something"
        )

    def test_an_applied_resume_commit_reports_executed_true(self):
        """Review j#88547 F2 (a): a run whose disposition CAS APPLIED has executed something."""
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            # Joins with a healthy pair: preflight build, pre-loop, resume, send.
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 3 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy})
        ops.lane_worktree_binding_reason = drifting  # type: ignore[assignment]
        out = _use_case(ops, resume_transition="applied").run(_REQ, execute=True)
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)
        self.assertIsNotNone(out.resume.transition, "the CAS ran")
        self.assertTrue(out.resume.transition.applied, "and applied")
        self.assertIsNone(ops.redispatched, "the send was fenced")
        self.assertTrue(out.executed, "an applied resume commit is an executed effect")

    def test_an_already_active_resume_reports_executed_false(self):
        """Review j#88547 F2 (b): an idempotent no-op applies NOTHING.

        R6 set the resume effect unconditionally, so this path reported ``executed=True`` for a
        run that closed nothing, relaunched nothing and committed nothing.
        """
        calls = {"n": 0}

        def drifting(*, lane, record):
            calls["n"] += 1
            return LAUNCH_AUTHORITY_OK if calls["n"] <= 3 else LAUNCH_AUTHORITY_BRANCH_DRIFTED

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy})
        ops.lane_worktree_binding_reason = drifting  # type: ignore[assignment]
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)
        self.assertTrue(out.resume.already_active)
        self.assertIsNone(out.resume.transition, "no CAS ran")
        self.assertIsNone(ops.redispatched)
        self.assertFalse(
            out.executed, "a run that applied no effect must not report executed"
        )

    def _authority_lost_resume(self):
        """A resume stopped AT the commit edge: no transition, typed detail."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
            BLOCK_COMMIT_AUTHORITY_MOVED,
            ResumeOutcome,
            ResumePreflight,
        )

        class _AuthorityLostResume:
            def run(self, request, *, execute):
                pf = ResumePreflight(
                    lane_hibernated=True, release_settled=True, issue_not_reowned=True,
                    pair_both_slots_live=True, pair_attested=True,
                )
                return ResumeOutcome(
                    executed=True, preflight=pf, issue=request.issue, lane=request.lane,
                    detail=BLOCK_COMMIT_AUTHORITY_MOVED,
                )

        return _AuthorityLostResume()

    def _use_case_with_resume(self, ops, resume):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            SublaneRecoverPairUseCase,
        )
        from tests.regressions.test_issue_13847_pair_recovery_orchestration import (
            _FakeStore,
            _Record,
        )

        return SublaneRecoverPairUseCase(
            ops=ops, store=_FakeStore(_Record()), resume=resume
        )

    def test_a_commit_authority_loss_on_a_healthy_pair_is_blocked_with_zero_effects(self):
        """Review j#88554 F1: blocked AND zero-effect, on the exact zero-effect premise.

        A HEALTHY pair — nothing to close, nothing to relaunch — stopped at the resume commit
        edge. R7 reported ``executed=True`` here, and the naive fix (truthful ``executed``)
        would have flipped ``is_blocked`` to False because the predicate short-circuited on
        ``executed`` first. Both facts are asserted together so neither can regress alone.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            BLOCK_RESUME_REFUSED,
        )

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy})
        out = self._use_case_with_resume(ops, self._authority_lost_resume()).run(
            _REQ, execute=True
        )
        self.assertEqual(ops.closed, [], "nothing to close")
        self.assertFalse(ops.relaunched, "nothing to relaunch")
        self.assertIsNone(out.resume.transition, "the CAS never ran")
        self.assertIsNone(ops.redispatched, "and the send never happened")
        self.assertEqual(out.effects, (), "this run applied nothing")
        self.assertFalse(out.executed, "so it must not claim to have executed")
        self.assertTrue(out.is_blocked, "and it IS blocked")
        self.assertEqual(out.detail, BLOCK_RESUME_REFUSED)
        self.assertEqual(out.redispatch, REDISPATCH_SKIPPED)

    def test_a_commit_authority_loss_after_real_effects_still_reports_them(self):
        # The complement: the same block, but on a pair that WAS closed and relaunched first.
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        out = self._use_case_with_resume(ops, self._authority_lost_resume()).run(
            _REQ, execute=True
        )
        self.assertEqual(len(ops.closed), 2)
        self.assertTrue(ops.relaunched)
        self.assertTrue(out.executed, "closes and a relaunch ARE effects")
        self.assertTrue(out.is_blocked)
        self.assertIn(EFFECT_CLOSED, out.effects)
        self.assertIn(EFFECT_RELAUNCHED, out.effects)
        self.assertNotIn(EFFECT_RESUME_COMMITTED, out.effects)
        self.assertNotIn(EFFECT_REDISPATCHED, out.effects)

    def test_an_all_idempotent_replay_reports_no_effects(self):
        """Review j#88554 F2: a replay that changed nothing must not read as a recovery.

        Healthy pair + already-active resume + already-redispatched send: zero closes, zero
        relaunch, no CAS, no delivery. R7 reported ``executed=True`` and "pair recovered".
        """
        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=REDISPATCH_ALREADY,
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)
        self.assertTrue(out.resume.already_active)
        self.assertEqual(out.redispatch, REDISPATCH_ALREADY)
        self.assertEqual(out.effects, (), "an idempotent replay applies nothing")
        self.assertFalse(out.executed)
        self.assertFalse(out.is_blocked, "and it is not an error either")
        self.assertIn("idempotent replay", out.detail)

    def test_a_fresh_delivery_is_an_applied_effect(self):
        # The separation j#88554 asks for: the same healthy/already-active lane, but a send
        # that actually delivered, is an executed run.
        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=REDISPATCH_DELIVERED,
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(out.effects, (EFFECT_REDISPATCHED,))
        self.assertTrue(out.executed)
        self.assertFalse(out.is_blocked)
        self.assertIn(EFFECT_REDISPATCHED, out.detail)

    def test_an_unresolved_redispatch_is_a_FATE_not_an_effect(self):
        """Review j#88563 F1: ``uncertain`` is not a known-applied effect.

        It spans a pre-reserve zero-write AND a post-send unknown fate, so listing it among
        the applied effects (as R8 did) asserts a write the status cannot support — and raises
        ``executed`` on a run that may have changed nothing.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            FATE_REDISPATCH_UNRESOLVED,
            REDISPATCH_UNCERTAIN,
        )

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=REDISPATCH_UNCERTAIN,
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(out.effects, (), "nothing is KNOWN to have been applied")
        self.assertFalse(out.executed)
        self.assertEqual(out.unresolved, (FATE_REDISPATCH_UNRESOLVED,))
        self.assertTrue(out.is_blocked, "an unresolved send needs operator reconcile")
        self.assertIn("could not be established", out.detail)

    def test_a_retiring_target_is_a_blocked_zero_send(self):
        """Review j#88563 F2: ``target_retiring`` was classified as neither effect nor block.

        It is a reserve-cancelled zero-send, so R8 reported it as an unblocked idempotent
        replay whose detail claimed the request was already delivered.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_TARGET_RETIRING,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        # Review j#88579 F4/F5: the status ALONE cannot say whether the cancel landed, so the
        # settled shape is stated explicitly. This scenario is the WRITTEN cancel.
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=RedispatchEdgeResult(
                status=REDISPATCH_TARGET_RETIRING, zero_send=True
            ),
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(out.effects, ())
        self.assertFalse(out.executed)
        self.assertEqual(out.unresolved, (), "a WRITTEN cancel is a KNOWN zero-send")
        self.assertTrue(out.is_blocked)
        self.assertIn("retirement transaction", out.detail)
        self.assertNotIn("already delivered", out.detail)

    def test_a_first_close_failure_applies_nothing_and_blocks(self):
        """Review j#88563 F1: the close-failure branch bypassed the composer."""
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()}, close_ok=False
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(ops.closed, [], "the first close failed")
        self.assertEqual(out.effects, (), "so nothing was applied")
        self.assertFalse(out.executed)
        self.assertTrue(out.attempted, "but the actuation WAS entered")
        self.assertTrue(out.is_blocked)

    def test_a_second_close_failure_still_reports_the_first_close(self):
        # The complement: the branch must not lose an effect that DID happen.
        ops = _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        ops.close_result = {}

        real_close = ops.close_bad_slot

        def failing_second(**kw):
            if ops.closed:
                return False
            return real_close(**kw)

        ops.close_bad_slot = failing_second  # type: ignore[assignment]
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(len(ops.closed), 1, "exactly one close landed")
        self.assertEqual(out.effects, (EFFECT_CLOSED,), "and it must be reported")
        self.assertTrue(out.executed)
        self.assertTrue(out.is_blocked)

    def test_a_vanished_slot_relaunch_failure_applies_nothing_and_blocks(self):
        # No closable slot, and the relaunch fails: zero effects, still blocked.
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: _absent(), WORKER_ROLE: _absent()},
            relaunch_ok=False,
        )
        out = _use_case(ops).run(_REQ, execute=True)
        self.assertEqual(ops.closed, [])
        self.assertEqual(out.effects, ())
        self.assertFalse(out.executed)
        self.assertTrue(out.attempted)
        self.assertTrue(out.is_blocked)

    def test_the_outcome_cannot_contradict_itself(self):
        """Structural guarantee (review j#88563 F1): no branch can hand-write a fixed value."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            RecoverPairOutcome,
            RecoverPairPreflight,
        )

        pf = RecoverPairPreflight(
            lane_hibernated=True, record_has_pins=True, gateway=None, worker=None,
            action_id="a",
        )
        with self.assertRaises(ValueError):  # executed true with no effects
            RecoverPairOutcome(executed=True, preflight=pf, issue="1", lane="l")
        with self.assertRaises(ValueError):  # effects present but executed false
            RecoverPairOutcome(
                executed=False, preflight=pf, issue="1", lane="l",
                effects=(EFFECT_CLOSED,),
            )
        with self.assertRaises(ValueError):  # off-vocabulary token
            RecoverPairOutcome(
                executed=True, preflight=pf, issue="1", lane="l", effects=("invented",),
            )

    # -- j#88571 F1: the edge reports what it observed --------------------------

    def test_a_started_transport_keeps_its_effect_when_the_ledger_write_fails(self):
        """Review j#88571 F1 / probe j#88570: the decisive case.

        The transport POSITIVELY started and then ``mark_delivered`` failed. The status is the
        same ``uncertain`` a never-bootstrapped fence produces, so an application that infers
        effects FROM the status loses a redelivery it is known to have made.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            EFFECT_REDISPATCHED,
            FATE_REDISPATCH_UNRESOLVED,
            RedispatchEdgeResult,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_UNCERTAIN,
        )

        started_then_lost = RedispatchEdgeResult(
            status=REDISPATCH_UNCERTAIN, delivered=True, unknown_fate=True
        )
        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=started_then_lost,
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(
            out.effects, (EFFECT_REDISPATCHED,), "the redelivery IS known to have happened"
        )
        self.assertTrue(out.executed)
        self.assertEqual(out.unresolved, (FATE_REDISPATCH_UNRESOLVED,), "and its fate is not")
        self.assertTrue(out.is_blocked, "an unresolved ledger write needs reconcile")

    def test_an_uncertain_before_the_reserve_applies_nothing(self):
        # The SAME status, the opposite facts: nothing was sent, so no effect may be claimed.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            FATE_REDISPATCH_UNRESOLVED,
            RedispatchEdgeResult,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_UNCERTAIN,
        )

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=RedispatchEdgeResult(
                status=REDISPATCH_UNCERTAIN, unknown_fate=True
            ),
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(out.effects, (), "nothing was sent")
        self.assertFalse(out.executed)
        self.assertEqual(out.unresolved, (FATE_REDISPATCH_UNRESOLVED,))
        self.assertTrue(out.is_blocked)

    def test_a_settled_zero_send_carries_no_unresolved_fate(self):
        # A cancelled reserve is a KNOWN zero-send: nothing applied AND nothing unknown.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_FAILED,
        )

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=RedispatchEdgeResult(status=REDISPATCH_FAILED, zero_send=True),
        )
        out = _use_case(ops, resume_transition="already_active").run(_REQ, execute=True)
        self.assertEqual(out.effects, ())
        self.assertEqual(out.unresolved, (), "a settled zero-send is not an unknown fate")
        self.assertTrue(out.is_blocked)

    def test_the_edge_result_refuses_contradictory_observations(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )

        with self.assertRaises(ValueError):
            RedispatchEdgeResult(status="x", delivered=True, zero_send=True)
        with self.assertRaises(ValueError):
            RedispatchEdgeResult(status="x", zero_send=True, unknown_fate=True)

    # -- j#88571 F2: the contract validates attempted, one success policy --------

    def test_the_validator_requires_attempted_for_any_effect_or_fate(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            EFFECT_CLOSED as _CLOSED,
            FATE_REDISPATCH_UNRESOLVED as _FATE,
            validate_effect_contract,
        )

        with self.assertRaises(ValueError):  # effects without attempted
            validate_effect_contract(
                executed=True, effects=(_CLOSED,), unresolved=(), attempted=False
            )
        with self.assertRaises(ValueError):  # unresolved without attempted
            validate_effect_contract(
                executed=False, effects=(), unresolved=(_FATE,), attempted=False
            )
        # ...and the consistent shapes still pass.
        validate_effect_contract(
            executed=True, effects=(_CLOSED,), unresolved=(), attempted=True
        )
        validate_effect_contract(
            executed=False, effects=(), unresolved=(), attempted=False
        )

    def test_both_surfaces_share_one_terminal_success_policy(self):
        """Review j#88571 F2: main and retry must not classify the same status differently."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            REDISPATCH_TERMINAL_SUCCESS,
            redispatch_is_success,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_ALREADY,
            REDISPATCH_DELIVERED,
            REDISPATCH_FAILED,
            REDISPATCH_TARGET_RETIRING,
            REDISPATCH_UNCERTAIN,
        )

        table = {
            REDISPATCH_DELIVERED: True,
            REDISPATCH_ALREADY: True,
            REDISPATCH_TARGET_RETIRING: False,
            REDISPATCH_FAILED: False,
            REDISPATCH_UNCERTAIN: False,
            "a_token_invented_later": False,  # unknown is blocked by default
        }
        for status, ok in table.items():
            with self.subTest(status=status):
                self.assertEqual(redispatch_is_success(status), ok)
        self.assertEqual(
            REDISPATCH_TERMINAL_SUCCESS, {REDISPATCH_DELIVERED, REDISPATCH_ALREADY}
        )

    # -- j#88571 F3: the text surface, on every path ----------------------------

    def test_the_text_renderer_shows_applied_and_unresolved_on_every_path(self):
        """Review j#88571 F3: including a PREFLIGHT-BLOCKED run, which showed neither.

        There were no renderer assertions at all, so the "all paths" claim in the commit and
        the runbook was never measured.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_cli import (  # noqa: E501
            format_recover_pair_text,
        )

        # 1. preflight-blocked (not hibernated): never entered the actuation.
        from tests.regressions.test_issue_13847_pair_recovery_orchestration import _Record

        blocked = _use_case(
            _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()}),
            record=_Record(lane_disposition="active"),
        ).run(_REQ, execute=True)
        text = format_recover_pair_text(blocked)
        self.assertIn("applied: nothing", text)
        self.assertIn("unresolved: none", text)

        # 2. a completed recovery.
        done = _use_case(
            _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        ).run(_REQ, execute=True)
        text = format_recover_pair_text(done)
        self.assertIn(EFFECT_CLOSED, text)
        self.assertIn("unresolved: none", text)

        # 3. an all-idempotent replay.
        healthy = _obs(already_healthy=True, is_bad_generation=False)
        replay = _use_case(
            _FakeOps(
                per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
                redispatch=REDISPATCH_ALREADY,
            ),
            resume_transition="already_active",
        ).run(_REQ, execute=True)
        text = format_recover_pair_text(replay)
        self.assertIn("applied: nothing", text)
        self.assertIn("unresolved: none", text)

        # 4. an unresolved fate.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            FATE_REDISPATCH_UNRESOLVED,
            RedispatchEdgeResult,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_UNCERTAIN,
        )

        unknown = _use_case(
            _FakeOps(
                per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
                redispatch=RedispatchEdgeResult(
                    status=REDISPATCH_UNCERTAIN, unknown_fate=True
                ),
            ),
            resume_transition="already_active",
        ).run(_REQ, execute=True)
        text = format_recover_pair_text(unknown)
        self.assertIn(FATE_REDISPATCH_UNRESOLVED, text)

    def test_the_json_payload_carries_the_same_three_facts(self):
        payload = _use_case(
            _FakeOps(per_slot_obs={GATEWAY_ROLE: _obs(), WORKER_ROLE: _obs()})
        ).run(_REQ, execute=True).as_payload()
        for key in ("executed", "attempted", "effects", "unresolved"):
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_the_preflight_matches_the_store_on_raw_persisted_bytes(self):
        """Review j#88526 F2: the 4 padded shapes, preflight and execute must agree.

        Each row below is byte-malformed in exactly one field. The store's CAS compares the
        RAW persisted value, so a preflight that normalized first reported "--execute would
        record" and the execute then refused ``repair_cas_refused`` — a dry-run green an owner
        could approve from.
        """
        from mozyo_bridge.core.state.lane_lifecycle_model import encode_declared_slots

        canonical_pins = encode_declared_slots(self._pins())
        shapes = (
            ("issue_id", f" {self.issue} ", BLOCK_WRONG_ISSUE),
            ("project_scope", "   ", BLOCK_PROJECT_SCOPE),
            ("process_release", " released ", BLOCK_RELEASE_NOT_SETTLED),
            ("declared_slots", f" {canonical_pins} ", BLOCK_PINS_NOT_CANONICAL),
        )
        for column, raw, expected in shapes:
            with self.subTest(column=column):
                self.setUp()
                _seed_hibernated_released_bound(
                    path=LaneLifecycleStore(home=self.home).path, key=self.key,
                    issue=self.issue, worktree_identity="", declared_slots=self._pins(),
                )
                before = self._record()
                self._raw_update(column, raw)
                pre = self._repair(execute=False)
                self.assertTrue(pre.is_blocked, "the preflight must not report a false green")
                self.assertEqual(pre.reason, expected)
                # ...and the execute reaches the SAME typed verdict, having written nothing.
                out = self._repair(execute=True)
                self.assertEqual(out.reason, expected)
                after = self._record()
                self.assertEqual(after.worktree_identity, "")
                self.assertEqual(after.revision, before.revision)

    def test_the_store_and_the_preflight_share_one_classifier(self):
        """The structural claim: neither surface re-derives the signature (j#88526 F2).

        Three rounds were lost to hand-written parity, so the anti-regression is that both
        modules reference the same pure classifier and the command maps EVERY axis it defines.
        """
        import inspect

        from mozyo_bridge.core.state import lane_worktree_binding_repair as store_mod
        from mozyo_bridge.core.state.lane_worktree_binding_signature import (
            SIGNATURE_BLOCKERS,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_worktree_binding_repair as app_mod,
        )

        for module in (store_mod, app_mod):
            with self.subTest(module=module.__name__):
                self.assertIn(
                    "classify_repair_signature", inspect.getsource(module),
                    "this surface must classify through the shared classifier",
                )
        unmapped = sorted(a for a in SIGNATURE_BLOCKERS if a not in app_mod._SIGNATURE_BLOCKERS)
        self.assertEqual(unmapped, [], "every signature axis needs a command-facing blocker")

    def test_the_preflight_projects_the_whole_store_signature(self):
        """Review j#88512 / j#88513: the required predicate matrix, one shape per axis.

        Each row below satisfies every OTHER axis, so a green here would be a dry-run the
        ``--execute`` then refuses — the false-green class this ticket exists to remove. The
        exact-signature positive control is asserted last so the matrix cannot pass by
        blocking everything.
        """
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            BINDING_KIND_PROJECT_GATEWAY,
            RELEASE_NOT_REQUESTED,
            RELEASE_PARTIAL,
            RELEASE_REQUESTED,
        )

        def _seed(**kw):
            self.setUp()
            defaults = dict(
                path=LaneLifecycleStore(home=self.home).path, key=self.key,
                issue=self.issue, worktree_identity="", declared_slots=self._pins(),
            )
            defaults.update(kw)
            _seed_hibernated_released_bound(**defaults)

        # 1-3. release not settled, in every unsettled shape.
        for target in (RELEASE_REQUESTED, RELEASE_PARTIAL):
            with self.subTest(axis="release", release=target):
                _seed(release_target=target)
                out = self._repair(execute=False)
                self.assertTrue(out.is_blocked)
                self.assertEqual(out.reason, BLOCK_RELEASE_NOT_SETTLED)
                self.assertEqual(self._repair(execute=True).reason, BLOCK_RELEASE_NOT_SETTLED)
                self.assertEqual(self._record().worktree_identity, "")

        with self.subTest(axis="release", release=RELEASE_NOT_REQUESTED):
            # A hibernated row whose release was NEVER requested: unproven, so unsafe.
            self.setUp()
            from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
            from mozyo_bridge.core.state.lane_lifecycle import (
                DISPOSITION_ACTIVE,
                DISPOSITION_HIBERNATED,
            )

            decision = DecisionPointer(
                source="redmine", issue_id=self.issue, journal_id="88513"
            )
            declared = LaneDeclarationStore(home=self.home).declare_lane(
                self.key, decision=decision, issue_id=self.issue,
                declared_slots=self._pins(), worktree_identity="",
            )
            LaneLifecycleStore(home=self.home).transition_disposition(
                self.key, expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=declared.revision, target=DISPOSITION_HIBERNATED,
                decision=decision,
            )
            out = self._repair(execute=False)
            self.assertTrue(out.is_blocked)
            self.assertEqual(out.reason, BLOCK_RELEASE_NOT_SETTLED)
            self.assertEqual(self._record().worktree_identity, "")

        # 4. a project-gateway binding KIND with an EMPTY scope — the store checks the kind as
        #    an independent axis, so a scope-only projection would read this green (j#88512).
        with self.subTest(axis="binding_kind"):
            self.setUp()
            _seed_hibernated_released_bound(
                path=LaneLifecycleStore(home=self.home).path, key=self.key,
                issue=self.issue, worktree_identity="", declared_slots=self._pins(),
            )
            self._force_binding_kind(BINDING_KIND_PROJECT_GATEWAY)
            out = self._repair(execute=False)
            self.assertTrue(out.is_blocked)
            self.assertEqual(out.reason, BLOCK_BINDING_KIND)
            self.assertEqual(self._repair(execute=True).reason, BLOCK_BINDING_KIND)
            self.assertEqual(self._record().worktree_identity, "")

        # 5. pins that decode but do NOT survive the store's own validator (j#88513 F2).
        with self.subTest(axis="invalid_pins"):
            self.setUp()
            _seed_hibernated_released_bound(
                path=LaneLifecycleStore(home=self.home).path, key=self.key,
                issue=self.issue, worktree_identity="", declared_slots=self._pins(),
            )
            self._force_duplicate_pins()
            out = self._repair(execute=False)
            self.assertTrue(out.is_blocked)
            self.assertEqual(out.reason, BLOCK_INVALID_PINS)
            self.assertEqual(self._repair(execute=True).reason, BLOCK_INVALID_PINS)
            self.assertEqual(self._record().worktree_identity, "")

        # 4b. a non-empty project scope (review j#88517): the row owns a scope, not an issue.
        with self.subTest(axis="project_scope"):
            self.setUp()
            _seed_hibernated_released_bound(
                path=LaneLifecycleStore(home=self.home).path, key=self.key,
                issue=self.issue, worktree_identity="", declared_slots=self._pins(),
            )
            before = self._record()
            self._raw_update("project_scope", "some/project/scope")
            out = self._repair(execute=False)
            self.assertTrue(out.is_blocked)
            self.assertEqual(out.reason, BLOCK_PROJECT_SCOPE)
            self.assertEqual(self._repair(execute=True).reason, BLOCK_PROJECT_SCOPE)
            after = self._record()
            self.assertEqual(after.worktree_identity, "")
            self.assertEqual(after.revision, before.revision)

        # 5b. a receiver replacement in flight (review j#88517): an actuator may be mutating
        #     this lane's slots right now, so the metadata write must not ride along.
        with self.subTest(axis="replacement_in_flight"):
            from mozyo_bridge.core.state.lane_lifecycle_model import (
                REPLACEMENT_REQUESTED,
                replacement_settled,
            )

            self.setUp()
            _seed_hibernated_released_bound(
                path=LaneLifecycleStore(home=self.home).path, key=self.key,
                issue=self.issue, worktree_identity="", declared_slots=self._pins(),
            )
            before = self._record()
            self._raw_update("replacement_state", REPLACEMENT_REQUESTED)
            self.assertFalse(
                replacement_settled(self._record().replacement_state),
                "the fixture must actually be in-flight for this axis to mean anything",
            )
            out = self._repair(execute=False)
            self.assertTrue(out.is_blocked)
            self.assertEqual(out.reason, BLOCK_REPLACEMENT_IN_FLIGHT)
            self.assertEqual(
                self._repair(execute=True).reason, BLOCK_REPLACEMENT_IN_FLIGHT
            )
            after = self._record()
            self.assertEqual(after.worktree_identity, "")
            self.assertEqual(after.revision, before.revision)

        # 6. the exact signature still goes green and writes — the positive control.
        with self.subTest(axis="exact_signature"):
            self.setUp()
            self._mint_hibernated_unbound_row()
            self.assertEqual(self._repair(execute=False).state, REPAIR_PREFLIGHT)
            self.assertEqual(self._repair(execute=True).state, REPAIR_APPLIED)
            self.assertTrue(self._record().worktree_identity)

    def _raw_update(self, column: str, value: str):
        """Write one column directly — ONLY to synthesize a malformed / legacy row shape that
        no public surface can produce, so the preflight's fence for it can be measured."""
        import sqlite3

        from mozyo_bridge.core.state.lane_lifecycle_schema import TABLE

        path = LaneLifecycleStore(home=self.home).path
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f"UPDATE {TABLE} SET {column} = ? WHERE repo_workspace_id = ? AND lane_id = ?",
                (value, self.key.repo_workspace_id, self.key.lane_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _force_binding_kind(self, kind: str):
        self._raw_update("binding_kind", kind)

    def _force_duplicate_pins(self):
        """A snapshot that DECODES but fails ``validate_declared_slots`` (duplicate slots)."""
        from mozyo_bridge.core.state.lane_lifecycle_model import encode_declared_slots

        gateway = self._pins()[0]
        self._raw_update("declared_slots", encode_declared_slots((gateway, gateway)))

    def test_the_use_case_module_exports_only_what_it_defines(self):
        """Review j#88516: ``__all__`` parity after the CLI leaf split.

        The CLI symbols moved to ``*_cli.py`` but stayed listed in the use-case module's
        ``__all__``, so ``import *`` would raise on the missing attributes.
        """
        import importlib

        for name in (
            "sublane_hibernated_pair_recovery",
            "sublane_hibernated_pair_recovery_cli",
            "sublane_worktree_binding_repair",
        ):
            module = importlib.import_module(
                "mozyo_bridge.e_110_execution_platform"
                ".f_140_delegated_coordinator_nested_handoff.application." + name
            )
            with self.subTest(module=name):
                missing = [n for n in getattr(module, "__all__", ()) if not hasattr(module, n)]
                self.assertEqual(missing, [], f"{name}.__all__ names undefined symbols")

    def test_an_unreadable_branch_read_never_normalizes_to_the_default_lane(self):
        """Review j#88513 F1, on the live PAIR probe (the repair surface already had this).

        ``_norm_lane("")`` is ``"default"``. If the branch read fails at action time — a
        TOCTOU the readability probe cannot exclude — normalizing before the emptiness check
        makes a lane named ``default`` read as ``ok``.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
            LiveHibernatedPairRecoveryOps,
        )

        self._mint_hibernated_unbound_row()
        self._repair(execute=True)
        record = self._record()
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            lane_checkout_authority,
        )

        ops = LiveHibernatedPairRecoveryOps(
            repo_root=self.repo, request_issue=self.issue, request_lane="default",
            request_journal="88513", lifecycle_home=self.home,
        )
        # Patched at the leaf that actually reads the branch — the seam the axis uses.
        with patch.object(lane_checkout_authority, "current_branch", return_value=""):
            self.assertEqual(
                ops.lane_worktree_binding_reason(lane="default", record=record),
                LAUNCH_AUTHORITY_WORKTREE_UNREADABLE,
            )

    def test_the_unbound_runbook_names_both_dispositions(self):
        # The runbook is the operator's only pointer out of the blocker; it must name the
        # action that actually works for the row they are looking at.
        runbook = launch_authority_runbook(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        self.assertIn("repair-worktree-binding", runbook)
        self.assertIn("sublane create", runbook)


class LaunchAuthorityTypedOutcomeTests(_RefreshCase):
    """Review j#88477 F2: the axis + runbook are TYPED outcome fields, not prose."""

    def _ops(self, reason):
        ops = FakeGatewayOps()
        ops.lane_authority_reason = lambda request, _r=reason: _r  # type: ignore[assignment]
        return ops

    def test_a_blocking_axis_is_typed_in_the_payload_with_its_runbook(self):
        payload = self._use_case(
            self._ops(LAUNCH_AUTHORITY_WORKTREE_UNBOUND)
        ).run(self._request(), execute=True).as_payload()
        self.assertEqual(
            payload["launch_authority_reason"], LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        self.assertEqual(
            payload["launch_authority_runbook"],
            launch_authority_runbook(LAUNCH_AUTHORITY_WORKTREE_UNBOUND),
        )

    def test_the_read_only_preflight_carries_the_same_typed_fields(self):
        payload = self._use_case(
            self._ops(LAUNCH_AUTHORITY_WORKTREE_MISMATCH)
        ).run(self._request(), execute=False).as_payload()
        self.assertEqual(
            payload["launch_authority_reason"], LAUNCH_AUTHORITY_WORKTREE_MISMATCH
        )
        self.assertTrue(payload["launch_authority_runbook"])

    def test_a_healthy_axis_is_typed_ok_with_no_runbook(self):
        # Emitted on EVERY outcome, not only the blocking one — an automation reading the
        # field must not have to infer "absent means fine".
        payload = self._use_case(self._ops(LAUNCH_AUTHORITY_OK)).run(
            self._request(), execute=True
        ).as_payload()
        self.assertEqual(payload["status"], REFRESH_STATUS_COMPLETED)
        self.assertEqual(payload["launch_authority_reason"], LAUNCH_AUTHORITY_OK)
        self.assertIsNone(payload["launch_authority_runbook"])

    def test_an_unclassifiable_axis_is_typed_unknown(self):
        ops = FakeGatewayOps()

        def _boom(request):
            raise RuntimeError("unreadable")

        ops.lane_authority_reason = _boom  # type: ignore[assignment]
        payload = self._use_case(ops).run(self._request(), execute=True).as_payload()
        self.assertEqual(payload["launch_authority_reason"], LAUNCH_AUTHORITY_UNKNOWN)
        self.assertTrue(payload["launch_authority_runbook"])

    def test_a_post_close_stop_reports_the_ACTION_TIME_axis_not_the_preflight_one(self):
        """Review j#88485: the typed reason must name why it STOPPED, not what preflight saw.

        The exact #14462 j#88463 transition: the axis is ``ok`` when the preflight runs and the
        approval is written, the actuator commits the close, and the launch leg's re-join then
        finds the lane unbound. Reporting the preflight-time ``ok`` would tell an operator the
        lane authority was fine while the gateway sits closed and unrelaunchable.
        """
        ops = FakeGatewayOps()
        # [preflight=ok, launch=unbound, ...] — the fake's single evaluator drives both the
        # preflight axis and the actuator's action-time authority join, as the live one does.
        reasons = [LAUNCH_AUTHORITY_OK, LAUNCH_AUTHORITY_WORKTREE_UNBOUND]
        ops.lane_authority_reason = (  # type: ignore[assignment]
            lambda request: reasons.pop(0) if reasons else LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        outcome = self._use_case(ops).run(self._request(), execute=True)
        payload = outcome.as_payload()
        # The preflight admitted it (the fence cannot predict a move that happens later)...
        self.assertEqual(outcome.verdict, REFRESH_ACTIONABLE)
        # ...the close committed, the launch did not, and the run stopped.
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertTrue(outcome.closed_old_gateway)
        self.assertFalse(outcome.fresh_slot_attested)
        # THE assertion: the typed axis names the action-time reason, with its runbook.
        self.assertEqual(
            payload["launch_authority_reason"], LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        )
        self.assertEqual(
            payload["launch_authority_runbook"],
            launch_authority_runbook(LAUNCH_AUTHORITY_WORKTREE_UNBOUND),
        )

    def test_a_stop_whose_lane_authority_still_holds_stays_typed_ok(self):
        """The other half of j#88485: a non-authority stop must NOT be blamed on the lane.

        The gateway's assigned name is occupied by a foreign live process, so the launch leg
        refuses while the lane authority is perfectly current. Re-reading the evaluator is
        exactly what keeps this ``ok`` — the field reports an observation, not the fact that
        something went wrong.
        """
        ops = FakeGatewayOps(name_free=False)
        ops.lane_authority_reason = (  # type: ignore[assignment]
            lambda request: LAUNCH_AUTHORITY_OK
        )
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertFalse(outcome.fresh_slot_attested)
        payload = outcome.as_payload()
        self.assertEqual(payload["launch_authority_reason"], LAUNCH_AUTHORITY_OK)
        self.assertIsNone(payload["launch_authority_runbook"])

    def test_a_resume_leg_authority_move_is_also_reported_action_time(self):
        # The send leg re-joins the same authority; a move there gets the same treatment.
        ops = FakeGatewayOps()
        reasons = [LAUNCH_AUTHORITY_OK, LAUNCH_AUTHORITY_OK, LAUNCH_AUTHORITY_WORKTREE_MISMATCH]
        ops.lane_authority_reason = (  # type: ignore[assignment]
            lambda request: reasons.pop(0) if reasons else LAUNCH_AUTHORITY_WORKTREE_MISMATCH
        )
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(ops.resumes, [], "an authority move before the send is a zero-send")
        self.assertEqual(
            outcome.as_payload()["launch_authority_reason"],
            LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
        )

    def test_the_typed_fields_never_leak_a_path_or_identity(self):
        for reason in sorted(LAUNCH_AUTHORITY_REASONS):
            ops = FakeGatewayOps()
            ops.lane_authority_reason = lambda request, _r=reason: _r  # type: ignore[assignment]
            payload = self._use_case(ops).run(self._request(), execute=False).as_payload()
            with self.subTest(reason=reason):
                self.assertIn(payload["launch_authority_reason"], LAUNCH_AUTHORITY_REASONS)
                runbook = payload["launch_authority_runbook"] or ""
                self.assertNotIn(str(self.home), runbook)
                self.assertNotIn(GATEWAY["assigned_name"], runbook)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RedispatchWriteObservationTests(HibernatedWorktreeRepairChainTests):
    """Review j#88579 F1/F2: the fence's WRITE and the loser's STATE are observations.

    ``mark_delivered`` / ``mark_cancelled`` return a bool — the UPDATE's rowcount. Discarding
    it reported a settled durable state that was never written. And the reserve loser's
    ``current_state`` was classified by a single negative test (``!= "uncertain"``), so a
    ``cancelled`` row — a durable zero-send whose request was never delivered — was promoted to
    ``already_redispatched``, a terminal success.

    These cases drive the REAL live edge against a REAL fence.
    """

    def _fence(self):
        from mozyo_bridge.core.state.dispatch_outbox_fence import (
            DispatchOutboxFence,
            dispatch_outbox_fence_path,
        )

        fence = DispatchOutboxFence(path=dispatch_outbox_fence_path(self.home))
        fence.bootstrap()
        return fence

    def _key(self, action_id, gw, journal="88579"):
        from mozyo_bridge.core.state.dispatch_outbox_fence import FenceKey

        return FenceKey(
            workspace_id=self.workspace_id, lane_id=self.lane, issue=self.issue,
            journal=journal, action_id=action_id, target_assigned_name=gw,
        )

    def _gw(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        return encode_assigned_name(self.workspace_id, "codex", self.lane)

    def _gw_row(self, gw):
        return {
            "name": gw, "pane_id": "wZ:p9K", "agent_status": "idle",
            "cwd": str(self.repo), "revision": "7",
        }

    def _drop_row(self, fence, key):
        """DELETE the reserved row: the store-level single-row loss the bool reports.

        Deliberately NOT a state transition — ``_set_state`` matches on the key alone, so an
        ``UPDATE`` still hits a cancelled/delivered row. Only a vanished row produces the
        rowcount-0 this finding is about, and the fence exposes no delete, so the fixture
        removes it directly from its own temporary store.
        """
        import sqlite3

        conn = sqlite3.connect(str(fence.path))
        try:
            conn.execute(
                "DELETE FROM dispatch_outbox WHERE workspace_id=? AND lane_id=? AND issue=? "
                "AND journal=? AND action_id=? AND target_assigned_name=?",
                key.as_row(),
            )
            conn.commit()
        finally:
            conn.close()

    def _ops(self, fence, journal="88579"):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_hibernated_pair_recovery_live as live,
        )

        self._mint_hibernated_unbound_row()
        self._repair(execute=True)
        return live.LiveHibernatedPairRecoveryOps(
            repo_root=self.repo, request_issue=self.issue, request_lane=self.lane,
            request_journal=journal, lifecycle_home=self.home, fence=fence,
        )

    # -- F1: the write's own return value ---------------------------------------

    def test_a_started_transport_whose_row_vanished_is_delivered_and_unresolved(self):
        """The decisive F1 case: the send happened, the ledger write matched no row.

        Reporting the plain ``redispatched`` here claims a durable record that does not exist;
        reporting a bare ``uncertain`` loses a redelivery that positively happened.
        """
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_DELIVERED,
            REDISPATCH_UNCERTAIN,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            EFFECT_REDISPATCHED,
            FATE_REDISPATCH_UNRESOLVED,
        )

        fence = self._fence()
        ops = self._ops(fence)
        gw = self._gw()
        action = "recover-pair:14475:vanish:1:1"
        key = self._key(action, gw)

        class _Started:
            started, zero_send, detail = True, False, "turn-start confirmed"

        def _deliver(_self, _request):
            # The transport POSITIVELY ran; the row disappears underneath it.
            self._drop_row(fence, key)
            return _Started()

        with patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(
                    delivery_live.LiveRecoveryAnchorDeliveryService, "deliver", _deliver
                ):
            result = ops.redispatch_to_gateway(
                action_id=action, gateway_assigned_name=gw, issue=self.issue,
                lane=self.lane, journal="88579", workspace_id=self.workspace_id,
            )

        self.assertEqual(result.status, REDISPATCH_UNCERTAIN)
        self.assertNotEqual(
            result.status, REDISPATCH_DELIVERED, "no durable record backs a delivered claim"
        )
        self.assertTrue(result.delivered, "the transport positively started")
        self.assertTrue(result.unknown_fate, "and its durable fate was never written")
        self.assertEqual(result.effects, (EFFECT_REDISPATCHED,))
        self.assertEqual(result.unresolved, (FATE_REDISPATCH_UNRESOLVED,))
        # The exactly-once hold must SURVIVE the loss: re-asserted as the fail-closed terminal
        # so a later reserve for the same key can never send again.
        self.assertEqual(fence.state_of(key), "uncertain")

    def test_a_written_delivery_is_a_plain_delivered(self):
        """Premise control for the case above: the SAME setup with the row intact.

        Without it, a ``uncertain`` verdict could come from any unrelated failure in the setup
        and the row-vanish assertion would measure nothing.
        """
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_DELIVERED,
        )

        fence = self._fence()
        ops = self._ops(fence)
        gw = self._gw()
        action = "recover-pair:14475:vanish:1:2"

        class _Started:
            started, zero_send, detail = True, False, "turn-start confirmed"

        with patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(
                    delivery_live.LiveRecoveryAnchorDeliveryService,
                    "deliver", lambda _s, _r: _Started(),
                ):
            result = ops.redispatch_to_gateway(
                action_id=action, gateway_assigned_name=gw, issue=self.issue,
                lane=self.lane, journal="88579", workspace_id=self.workspace_id,
            )

        self.assertEqual(result.status, REDISPATCH_DELIVERED)
        self.assertTrue(result.delivered)
        self.assertFalse(result.unknown_fate, "the ledger write landed")
        self.assertEqual(fence.state_of(self._key(action, gw)), "delivered")

    def test_a_pre_send_cancel_whose_row_vanished_is_not_a_settled_zero_send(self):
        """The pre-send half of F1: nothing was sent, but the cancel wrote nothing either.

        Calling that a settled zero-send asserts a durable ``cancelled`` record the store does
        not hold, so a reconcile pass would see nothing owed.
        """
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_FAILED,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            FATE_REDISPATCH_UNRESOLVED,
        )

        fence = self._fence()
        ops = self._ops(fence)
        gw = self._gw()
        action = "recover-pair:14475:vanish:2:1"
        key = self._key(action, gw)
        sent = []

        def _moved(_ops_self, _lane):
            # The authority check fires after the reserve and before transport; the row is lost
            # in that same window. (Bound as an unbound function, so it takes the adapter and
            # the lane exactly as the production call site passes them — a signature mismatch
            # here would be swallowed by the caller's ``except`` and silently pass this case
            # with the fence intact: measured.)
            self._drop_row(fence, key)
            return False

        with patch.object(
            live.LiveHibernatedPairRecoveryOps, "_checkout_authority_current", _moved
        ), patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(
                    delivery_live.LiveRecoveryAnchorDeliveryService,
                    "deliver", lambda _s, r: sent.append(r),
                ):
            result = ops.redispatch_to_gateway(
                action_id=action, gateway_assigned_name=gw, issue=self.issue,
                lane=self.lane, journal="88579", workspace_id=self.workspace_id,
            )

        self.assertEqual(sent, [], "zero transport")
        self.assertEqual(result.status, REDISPATCH_FAILED)
        self.assertFalse(result.delivered)
        self.assertFalse(result.zero_send, "no durable cancel backs a settled claim")
        self.assertTrue(result.unknown_fate)
        self.assertEqual(result.unresolved, (FATE_REDISPATCH_UNRESOLVED,))
        self.assertEqual(fence.state_of(key), "uncertain", "the hold is re-asserted")

    def test_a_written_pre_send_cancel_is_a_settled_zero_send(self):
        # Premise control: the same moved-authority path with the row intact IS settled.
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_FAILED,
        )

        fence = self._fence()
        ops = self._ops(fence)
        gw = self._gw()
        action = "recover-pair:14475:vanish:2:2"

        with patch.object(
            live.LiveHibernatedPairRecoveryOps,
            "_checkout_authority_current", return_value=False,
        ), patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(
                    delivery_live.LiveRecoveryAnchorDeliveryService,
                    "deliver", lambda _s, _r: None,
                ):
            result = ops.redispatch_to_gateway(
                action_id=action, gateway_assigned_name=gw, issue=self.issue,
                lane=self.lane, journal="88579", workspace_id=self.workspace_id,
            )

        self.assertEqual(result.status, REDISPATCH_FAILED)
        self.assertTrue(result.zero_send)
        self.assertFalse(result.unknown_fate)
        self.assertEqual(fence.state_of(self._key(action, gw)), "cancelled")

    # -- F2: the reserve loser's state, classified TOTALLY -----------------------

    def _loser(self, fence, ops, gw, action, pre_state):
        """Put the key in ``pre_state``, then run the edge so its reserve LOSES."""
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )

        key = self._key(action, gw)
        fence.reserve(key)
        if pre_state == "delivered":
            fence.mark_delivered(key)
        elif pre_state == "cancelled":
            fence.mark_cancelled(key)
        elif pre_state == "uncertain":
            fence.mark_uncertain(key)
        elif pre_state != "reserved":  # pragma: no cover - fixture guard
            raise AssertionError(f"unhandled pre-state {pre_state!r}")
        self.assertEqual(fence.state_of(key), pre_state, "the fixture's premise")

        def _deliver(_self, _request):
            raise AssertionError("a losing reserve must never reach the transport")

        with patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(
                    delivery_live.LiveRecoveryAnchorDeliveryService, "deliver", _deliver
                ):
            return ops.redispatch_to_gateway(
                action_id=action, gateway_assigned_name=gw, issue=self.issue,
                lane=self.lane, journal="88579", workspace_id=self.workspace_id,
            )

    def test_the_reserve_loser_state_matrix_is_total(self):
        """Every state the loser can report, classified — the whole matrix in one place.

        ``cancelled`` is the finding: a durable ZERO-SEND. Reading it as
        ``already_redispatched`` told the caller the implementation_request had been delivered
        by an earlier run when the store says the opposite.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_ALREADY,
            REDISPATCH_FAILED,
            REDISPATCH_UNCERTAIN,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            redispatch_is_success,
        )

        fence = self._fence()
        ops = self._ops(fence)
        gw = self._gw()
        # (pre-state, expected status, delivered, zero_send, unknown_fate, success?)
        matrix = (
            ("delivered", REDISPATCH_ALREADY, False, True, False, True),
            ("cancelled", REDISPATCH_FAILED, False, True, False, False),
            ("uncertain", REDISPATCH_UNCERTAIN, False, False, True, False),
            ("reserved", REDISPATCH_UNCERTAIN, False, False, True, False),
        )
        for i, (pre, status, delivered, zero, unknown, ok) in enumerate(matrix):
            with self.subTest(prior_state=pre):
                result = self._loser(
                    fence, ops, gw, f"recover-pair:14475:loser:{i}", pre
                )
                self.assertEqual(result.status, status)
                self.assertEqual(result.delivered, delivered)
                self.assertEqual(result.zero_send, zero)
                self.assertEqual(result.unknown_fate, unknown)
                self.assertEqual(redispatch_is_success(result.status), ok)

    def test_an_unrecognised_loser_state_is_never_a_success(self):
        """The residual arm: a state outside the fence's vocabulary fails closed.

        A future fence state must not fall through to ``already_redispatched`` by default —
        that is the shape of the defect this finding names.
        """
        from unittest.mock import patch

        from mozyo_bridge.core.state.dispatch_outbox_fence import ReserveResult
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_hibernated_pair_recovery_live as live,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_UNCERTAIN,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            redispatch_is_success,
        )

        fence = self._fence()
        ops = self._ops(fence)
        gw = self._gw()
        lost = ReserveResult(
            won=False, prior_state="quarantined", current_state="quarantined",
            needs_reconcile=False,
        )
        with patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(type(fence), "reserve", return_value=lost):
            result = ops.redispatch_to_gateway(
                action_id="recover-pair:14475:loser:unknown", gateway_assigned_name=gw,
                issue=self.issue, lane=self.lane, journal="88579",
                workspace_id=self.workspace_id,
            )
        self.assertEqual(result.status, REDISPATCH_UNCERTAIN)
        self.assertTrue(result.unknown_fate)
        self.assertFalse(redispatch_is_success(result.status))


class RedispatchSuccessPolicyTests(unittest.TestCase):
    """Review j#88579 F3/F4/F5: one success policy, a total fact table, a typed boundary."""

    # -- F3: the shared policy is the ONLY policy --------------------------------

    def test_a_skipped_send_after_a_successful_resume_blocks(self):
        """Review j#88579 F3 / probe j#88577: the main surface's local whitelist.

        ``redispatch_is_success`` exists so both surfaces agree, and the main use case then
        OR-ed a local ``or self.redispatch == REDISPATCH_SKIPPED`` on top of it. The retry
        surface blocked the same outcome. ``skipped`` after an APPLIED resume means the run
        entered actuation, changed the pair, and never redelivered the implementation_request:
        an owed send, reported as an unblocked success.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_SKIPPED,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            EFFECT_RESUME_COMMITTED,
            redispatch_is_success,
        )

        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy},
            redispatch=REDISPATCH_SKIPPED,
        )
        out = _use_case(ops, resume_transition="applied").run(_REQ, execute=True)

        self.assertIn(EFFECT_RESUME_COMMITTED, out.effects, "the run DID change the pair")
        self.assertTrue(out.executed)
        self.assertTrue(
            out.is_blocked, "an attempted run that never redelivered is not a success"
        )
        self.assertFalse(redispatch_is_success(REDISPATCH_SKIPPED))

    def test_the_success_policy_admits_exactly_two_statuses(self):
        # The closed set, stated positively: a future token blocks by default.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_hibernated_pair_recovery as main,
            sublane_recover_pair_delivery as retry,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            REDISPATCH_TERMINAL_SUCCESS,
            redispatch_is_success,
        )

        self.assertEqual(
            REDISPATCH_TERMINAL_SUCCESS,
            frozenset({"redispatched", "already_redispatched"}),
        )
        self.assertFalse(redispatch_is_success("a_future_token"))
        for module in (main, retry):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertNotIn(
                    "self.redispatch ==", source,
                    "a per-surface equality test is how the two policies drifted",
                )

    # -- F4: the status x fact table, in full ------------------------------------

    def test_every_status_fact_combination_is_decided(self):
        """The whole product of statuses and fact shapes: each admitted or refused.

        Enumerating the full table (not just the shapes the code happens to build) is what
        makes this a contract rather than a description of today's call sites.
        """
        import itertools

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            REDISPATCH_EDGE_FACT_SHAPES,
            REDISPATCH_EDGE_STATUSES,
            RedispatchEdgeResult,
        )

        shapes = tuple(itertools.product((False, True), repeat=3))
        self.assertEqual(len(shapes), 8)
        admitted = 0
        for status in sorted(REDISPATCH_EDGE_STATUSES):
            for delivered, zero_send, unknown_fate in shapes:
                allowed = (delivered, zero_send, unknown_fate) in (
                    REDISPATCH_EDGE_FACT_SHAPES[status]
                )
                with self.subTest(status=status, shape=(delivered, zero_send, unknown_fate)):
                    if allowed:
                        admitted += 1
                        result = RedispatchEdgeResult(
                            status=status, delivered=delivered,
                            zero_send=zero_send, unknown_fate=unknown_fate,
                        )
                        # The derived facts follow from the flags, never from the status.
                        self.assertEqual(bool(result.effects), delivered)
                        self.assertEqual(bool(result.unresolved), unknown_fate)
                    else:
                        with self.assertRaises(ValueError):
                            RedispatchEdgeResult(
                                status=status, delivered=delivered,
                                zero_send=zero_send, unknown_fate=unknown_fate,
                            )
        self.assertEqual(
            admitted, sum(len(v) for v in REDISPATCH_EDGE_FACT_SHAPES.values())
        )
        # Guard the table itself: it must stay total over the statuses and never admit
        # everything (an all-permitting entry would make the validation vacuous).
        for status, allowed in REDISPATCH_EDGE_FACT_SHAPES.items():
            with self.subTest(status=status):
                self.assertTrue(allowed, f"{status} admits no shape at all")
                self.assertLess(len(allowed), len(shapes), f"{status} admits everything")

    def test_a_terminal_success_must_carry_the_fact_that_makes_it_one(self):
        # The concrete asymmetry the table encodes: a success with nothing behind it.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )

        with self.assertRaises(ValueError):
            RedispatchEdgeResult(status="redispatched")  # no delivery behind the success
        with self.assertRaises(ValueError):
            RedispatchEdgeResult(status="already_redispatched", delivered=True)
        with self.assertRaises(ValueError):
            RedispatchEdgeResult(status="invented_status", zero_send=True)

    # -- F5: the production boundary is typed ------------------------------------

    def test_the_ops_protocol_and_its_production_implementations_are_typed(self):
        """The Protocol, the live adapter and both consumers agree on the typed result.

        Annotating only the concrete class leaves the CONTRACT saying ``str``, so a second
        implementation written against the Protocol satisfies the declared type and breaks the
        consumer. Both the annotation and the runtime value are pinned.
        """
        import typing

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            HibernatedPairRecoveryOps,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_live import (  # noqa: E501
            LiveHibernatedPairRecoveryOps,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )

        for owner in (HibernatedPairRecoveryOps, LiveHibernatedPairRecoveryOps):
            for name in ("redispatch_to_gateway", "retry_redispatch_to_gateway"):
                with self.subTest(owner=owner.__name__, method=name):
                    method = getattr(owner, name)
                    hints = typing.get_type_hints(method)
                    self.assertIs(
                        hints.get("return"), RedispatchEdgeResult,
                        f"{owner.__name__}.{name} must DECLARE the typed result",
                    )

    def test_no_production_consumer_adapts_a_bare_status(self):
        """The legacy-string constructor is test-support, and must stay out of ``src``.

        A production path that reaches for it re-introduces exactly the loss the typed result
        exists to prevent: a status alone cannot distinguish a settled zero-send from an
        unknown fate.
        """
        import subprocess

        root = Path(__file__).resolve().parents[2] / "src"
        found = subprocess.run(
            ["grep", "-rn", "--include=*.py", "edge_result_from_status", str(root)],
            text=True, capture_output=True,
        ).stdout.splitlines()
        # Positive control: the sweep must be able to FIND the symbol, otherwise an empty
        # ``offenders`` proves only that the grep was misaimed.
        self.assertTrue(
            [ln for ln in found if "recovery_effect_contract.py" in ln.split(":")[0]],
            "the sweep found nothing at all; it is not measuring src",
        )
        offenders = [
            line for line in found
            if "recovery_effect_contract.py" not in line.split(":")[0]
        ]
        self.assertEqual(offenders, [], "production code must observe the edge, not a status")

    def test_the_consumers_use_the_result_without_re_deriving_its_facts(self):
        # Both consumers read ``.effects`` / ``.unresolved`` off the edge rather than mapping
        # the status back to facts — the re-derivation that lost them in the first place.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_hibernated_pair_recovery as main,
            sublane_recover_pair_delivery as retry,
        )

        for module in (main, retry):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertIn("edge.effects", source)
                self.assertIn("edge.unresolved", source)


class UncertainWriteObservationTests(RedispatchWriteObservationTests):
    """Review j#88587 F1: ``mark_uncertain`` is an outcome write like the other two.

    R11 routed ``mark_delivered`` / ``mark_cancelled`` through the observer and left the two
    ``mark_uncertain`` sites calling the fence directly, so a vanished row on those branches
    dropped the exactly-once hold entirely and a later reserve for the same key WON.
    """

    def _run_with_outcome(self, action, *, raise_it, drop_before_write):
        """Drive the edge to a ``mark_uncertain`` branch, optionally losing the row first."""
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            recovery_anchor_delivery_live as delivery_live,
            sublane_hibernated_pair_recovery_live as live,
        )

        # Seed once per test: ``_ops`` mints the lifecycle row, and a second mint is refused
        # ``already_declared``. The fence is shared with it so both branches of a subTest run
        # against the same store.
        if getattr(self, "_cached", None) is None:
            fence = self._fence()
            self._cached = (fence, self._ops(fence))
        fence, ops = self._cached
        gw = self._gw()
        key = self._key(action, gw)

        class _Unresolved:
            started, zero_send, detail = False, False, "no turn-start observed"

        def _deliver(_self, _request):
            if drop_before_write:
                self._drop_row(fence, key)
            if raise_it:
                raise RuntimeError("delivery service exploded")
            return _Unresolved()

        with patch.object(live, "list_herdr_agent_rows", return_value=[self._gw_row(gw)]), \
                patch.object(
                    delivery_live.LiveRecoveryAnchorDeliveryService, "deliver", _deliver
                ):
            result = ops.redispatch_to_gateway(
                action_id=action, gateway_assigned_name=gw, issue=self.issue,
                lane=self.lane, journal="88579", workspace_id=self.workspace_id,
            )
        return fence, key, result

    def test_a_raising_delivery_service_keeps_the_hold_when_the_row_vanished(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_UNCERTAIN,
        )

        fence, key, result = self._run_with_outcome(
            "recover-pair:14475:uncertain:raise:vanished",
            raise_it=True, drop_before_write=True,
        )
        self.assertEqual(result.status, REDISPATCH_UNCERTAIN)
        self.assertTrue(result.unknown_fate)
        self.assertEqual(
            fence.state_of(key), "uncertain",
            "the hold must be re-asserted, not left absent",
        )
        # The decisive consequence: a later run must NOT be cleared to send again.
        self.assertFalse(
            fence.reserve(key).won, "a re-run won the reserve; exactly-once was lost"
        )

    def test_an_unresolved_delivery_outcome_keeps_the_hold_when_the_row_vanished(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_UNCERTAIN,
        )

        fence, key, result = self._run_with_outcome(
            "recover-pair:14475:uncertain:outcome:vanished",
            raise_it=False, drop_before_write=True,
        )
        self.assertEqual(result.status, REDISPATCH_UNCERTAIN)
        self.assertTrue(result.unknown_fate)
        self.assertEqual(fence.state_of(key), "uncertain")
        self.assertFalse(fence.reserve(key).won)

    def test_the_same_branches_with_the_row_intact_settle_uncertain(self):
        # Premise controls: without the row loss both branches already reach ``uncertain``,
        # so the assertions above measure the vanished-row repair and not the branch itself.
        for label, raise_it in (("raise", True), ("outcome", False)):
            with self.subTest(branch=label):
                fence, key, result = self._run_with_outcome(
                    f"recover-pair:14475:uncertain:{label}:intact",
                    raise_it=raise_it, drop_before_write=False,
                )
                self.assertTrue(result.unknown_fate)
                self.assertEqual(fence.state_of(key), "uncertain")
                self.assertFalse(fence.reserve(key).won)

    def test_the_edge_routes_every_fence_write_through_the_observer(self):
        """The structural pin: no fence write may bypass ``_record``.

        The recurrence this prevents is exactly R11's — the observer was introduced and two
        write sites were left calling the fence directly, while the journal claimed all of
        them had been routed. Counting the call sites is what makes that claim checkable.
        """
        import ast

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_recover_pair_redispatch_edge as edge_mod,
        )

        source = Path(edge_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Parsed, not grepped: four of the seven observer call sites wrap across lines, so a
        # source-substring count silently under-reports and the pin passes while writes are
        # missing.
        direct, observed = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr.startswith("mark_"):
                # CALLING fence.mark_* directly is the bypass this pin forbids.
                direct.append((func.attr, node.lineno))
            if isinstance(func, ast.Name) and func.id == "_record" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Attribute):
                    # PASSING it to the observer is the sanctioned form.
                    observed.append((first.attr, node.lineno))
        self.assertEqual(direct, [], f"these fence writes bypass _record: {direct}")
        # Positive control: the observer IS used, for every write the edge performs, and the
        # set of write kinds is the whole fence outcome vocabulary.
        self.assertEqual(len(observed), 7, f"observer call sites changed: {observed}")
        self.assertEqual(
            {name for name, _ in observed},
            {"mark_cancelled", "mark_delivered", "mark_uncertain"},
        )


class RedispatchDetailFactOrderTests(unittest.TestCase):
    """Review j#88587 F2: the detail states OBSERVED facts, not a status ranking.

    Both surfaces ranked statuses before consulting ``unresolved``, so a ``target_retiring``
    whose cancel never wrote announced a settled "the outbox reserve was cancelled". The same
    ordering also let a settled refusal with nothing applied fall through to "the
    implementation_request already delivered" (found while verifying this finding).
    """

    def _main_detail(self, edge, *, resume="already_active"):
        """Run the main use case. ``resume="applied"`` makes the run APPLY something.

        The effects dimension is what review j#88592 F3 turned on: the same status means
        different things to the operator depending on whether this run changed the pair.
        """
        healthy = _obs(already_healthy=True, is_bad_generation=False)
        ops = _FakeOps(
            per_slot_obs={GATEWAY_ROLE: healthy, WORKER_ROLE: healthy}, redispatch=edge
        )
        return _use_case(ops, resume_transition=resume).run(_REQ, execute=True)

    def _retry_outcome(self, edge):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_recover_pair_delivery import (  # noqa: E501
            SublaneRecoverPairDeliveryUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            RecoverPairDeliveryRetryRequest,
        )

        class _Ops:
            def workspace_id(self):
                return "ws-14475"

            def preflight_retry_redispatch_to_gateway(self, **kw):
                return True, "ready"

            def retry_redispatch_to_gateway(self, **kw):
                return edge

        request = RecoverPairDeliveryRetryRequest(
            issue="14475", lane="issue_14475_detail", journal="88587",
            implementation_request_journal="88465",
            retry_of_action_id="recover-pair:14475:issue_14475_detail:3:2",
            prior_zero_send_journal="88579",
        )
        out = SublaneRecoverPairDeliveryUseCase(ops=_Ops()).run(request, execute=True)
        self.assertTrue(
            out.attempted, "the fixture must actually reach the redispatch edge"
        )
        return out

    def test_an_unresolved_fate_is_never_reported_as_a_settled_cancel(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_TARGET_RETIRING,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )

        edge = RedispatchEdgeResult(
            status=REDISPATCH_TARGET_RETIRING, unknown_fate=True
        )
        for label, detail in (
            ("main", self._main_detail(edge).detail),
            ("retry", self._retry_outcome(edge).detail),
        ):
            with self.subTest(surface=label):
                self.assertIn("could not be established", detail)
                self.assertIn("reconcile", detail)
                self.assertNotIn(
                    "reserve was cancelled", detail,
                    "the cancel is exactly what did NOT durably happen",
                )

    def test_a_settled_retirement_cancel_still_names_the_retirement(self):
        # Premise control: with the cancel WRITTEN, the retirement-specific phrasing is the
        # right answer — the fix must not flatten every status into one message.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_TARGET_RETIRING,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            RedispatchEdgeResult,
        )

        edge = RedispatchEdgeResult(status=REDISPATCH_TARGET_RETIRING, zero_send=True)
        for label, detail in (
            ("main", self._main_detail(edge).detail),
            ("retry", self._retry_outcome(edge).detail),
        ):
            with self.subTest(surface=label):
                self.assertIn("retirement transaction", detail)
                self.assertNotIn("could not be established", detail)

    def test_no_settled_refusal_claims_a_delivery_on_either_surface(self):
        """The whole fact table, both surfaces: only a real delivery may say so.

        Checking the two statuses the finding names would leave the sibling branch — a
        settled ``send_failed`` reporting "the implementation_request already delivered" —
        exactly as it was.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            REDISPATCH_EDGE_FACT_SHAPES,
            RedispatchEdgeResult,
            redispatch_is_success,
        )

        claims = ("already delivered", "delivered under a new recovery action")
        checked = 0
        for status, shapes in sorted(REDISPATCH_EDGE_FACT_SHAPES.items()):
            for delivered, zero_send, unknown_fate in sorted(shapes):
                edge = RedispatchEdgeResult(
                    status=status, delivered=delivered,
                    zero_send=zero_send, unknown_fate=unknown_fate,
                )
                # A delivery may be claimed ONLY when the fence positively holds one and the
                # fate is settled.
                may_claim = redispatch_is_success(status) and not unknown_fate
                # Review j#88592 F3: the same sweep, run once with this recovery applying
                # nothing and once with it committing the resume. The first pass alone let
                # "nothing is owed" stand on a run that had changed the pair.
                cases = [
                    ("main", self._main_detail(edge)),
                    ("main+applied", self._main_detail(edge, resume="applied")),
                    ("retry", self._retry_outcome(edge)),
                ]
                for label, out in cases:
                    with self.subTest(surface=label, status=status,
                                      shape=(delivered, zero_send, unknown_fate)):
                        checked += 1
                        claimed = any(c in out.detail for c in claims)
                        if not may_claim:
                            self.assertFalse(
                                claimed,
                                f"{label} claimed a delivery for {status} "
                                f"{(delivered, zero_send, unknown_fate)}: {out.detail}",
                            )
                        # An unresolved fate always routes to reconcile, whatever the status.
                        if unknown_fate:
                            self.assertIn("reconcile", out.detail)
                        self.assertEqual(bool(out.unresolved), unknown_fate)
                        # A run that APPLIED something and did not deliver owes the
                        # redelivery; it may never announce that nothing is outstanding.
                        if out.effects and not may_claim:
                            self.assertNotIn(
                                "nothing was applied", out.detail,
                                f"{label} denied its own effects: {out.detail}",
                            )
                            self.assertNotIn(
                                "no outbox reservation is outstanding", out.detail,
                                f"{label} called an owed send settled: {out.detail}",
                            )
        self.assertEqual(checked, 27, "the sweep must cover the whole table on every surface")

    def test_a_committed_resume_that_never_sent_reports_the_send_as_owed(self):
        """The named case: applied resume + ``skipped`` (review j#88592 F3).

        The machine state blocks precisely because the redelivery is owed; the detail said
        "nothing is owed to the outbox", telling the operator the opposite. This case is a
        real production shape — the run's own drift re-join returns ``skipped`` AFTER the
        resume commits.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_SKIPPED,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_effect_contract import (  # noqa: E501
            EFFECT_RESUME_COMMITTED,
        )

        out = self._main_detail(REDISPATCH_SKIPPED, resume="applied")
        self.assertIn(EFFECT_RESUME_COMMITTED, out.effects)
        self.assertTrue(out.is_blocked)
        self.assertIn("still owed", out.detail)
        self.assertNotIn("nothing is owed", out.detail)
        self.assertNotIn("no outbox reservation is outstanding", out.detail)

    def test_the_same_status_with_nothing_applied_reports_nothing_outstanding(self):
        # Premise control: the fix must not make every ``skipped`` claim an owed send. With
        # the resume already satisfied, this run applied nothing and owes nothing.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
            REDISPATCH_SKIPPED,
        )

        out = self._main_detail(REDISPATCH_SKIPPED, resume="already_active")
        self.assertEqual(out.effects, ())
        self.assertIn("no outbox reservation is outstanding", out.detail)
        self.assertNotIn("still owed", out.detail)

    def test_the_skipped_token_documents_what_it_actually_means(self):
        """The cross-layer half of the finding: the comment contradicted production.

        ``REDISPATCH_SKIPPED`` was documented as "resume did not apply -> nothing to
        redeliver" while ``run`` itself returns it after a COMMITTED resume when the binding
        drifted. A reader who trusted the comment would reproduce exactly this defect.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_hibernated_pair_recovery as main,
        )

        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            'REDISPATCH_SKIPPED = "redispatch_not_reached"  # resume did not apply', source
        )
        self.assertNotIn('"resume did not apply"', source)
        # And production really does produce the combination the old comment denied.
        out = self._main_detail(
            __import__(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested"
                "_handoff.application.sublane_hibernated_pair_recovery",
                fromlist=["REDISPATCH_SKIPPED"],
            ).REDISPATCH_SKIPPED,
            resume="applied",
        )
        self.assertTrue(out.effects, "resume committed alongside a not-reached send")
