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
from herdr_workspace_fixtures import anchored_repo_root  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E402,E501
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worktree_binding_repair import (  # noqa: E402,E501
    BLOCK_ALREADY_BOUND,
    BLOCK_BRANCH_DRIFTED,
    BLOCK_FOREIGN_WORKSPACE,
    BLOCK_NOT_HIBERNATED,
    BLOCK_WORKTREE_NOT_ROOT,
    BLOCK_WORKTREE_UNREADABLE,
    REPAIR_APPLIED,
    REPAIR_PREFLIGHT,
    _current_branch,
    run_worktree_binding_repair,
)

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E402,E501
    BLOCK_WORKTREE_BINDING,
)
from tests.regressions.test_issue_13847_pair_recovery_orchestration import (  # noqa: E402
    GATEWAY_ROLE,
    WORKER_ROLE,
    _FakeOps,
    _REQ,
    _obs,
    _use_case,
)
from tests.regressions.test_issue_13879_hibernated_bound_pin_repair import (  # noqa: E402
    _seed_hibernated_released_bound,
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
            ProcessGenerationPin(
                role="implementation_gateway", provider="codex",
                assigned_name="mzb1_wsrepair_codex_" + self.lane, locator="w3N:p1K",
                runtime_revision="1",
            ),
            ProcessGenerationPin(
                role="implementation_worker", provider="claude",
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
