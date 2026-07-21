"""Redmine #13948 R3 — a nested replacement-launch failure must surface losslessly.

R2 (j#81717) taught the embedded ``sublane create/start`` path to stop before dispatch on a
non-positive embedded ``session-start`` and to point at the SAME action's public rollback.
R3 (j#81811) closes the *nested* leak: when ``prepare-bound-pair --execute`` converges a
hibernated bound pair through the #13933 v1 replacement-binding adapter and the fresh
participant does not reach bounded startup health, the inner :class:`SessionStartResult`
carried the typed startup ``action_id`` / per-role health / rollback debt, but the outer
public outcome collapsed it to a generic ``replacement_binding_launch_unhealthy`` detail
string with no actionable rollback pointer.

Review j#82700 (Finding 1, mutation-probed): the earlier regressions patched
``heal_lane_column`` itself and hand-built the ``PreparationDrive``, so a broken production
connection stayed green. :class:`RealDriveWiringTest` therefore drives the REAL
``run_bound_pair_preparation(execute=True)`` over the REAL
``LiveBoundPairPreparationOps.drive`` and the REAL ``ReplacementActuatorUseCase``, mocking
ONLY the external boundaries (live herdr inventory, git, provider resolution, the
attestation-store lock, and the actual process launch). Every production line under test —
``heal_lane_column``'s v1 catch/projection, the port's ``launch_startup_health`` stash, the
drive's ``startup=getattr(...)``, and the outcome's ``rollback_pointer`` — runs for real, and
the action id from the PUBLIC pointer drives the REAL ``run_session_rollback`` rail.

Contract (IR j#81811 Required correction 1-5):

1. the nested unhealthy launch propagates the typed ``action_id`` / role health / rollback
   debt / explicit rollback pointer outward losslessly — no raw locator / detail / secret;
2. the public pointer is exactly ``mozyo-bridge herdr session-rollback --action-id <id>``
   with NO ``--execute`` — a launch closes nothing (Answer j#80991);
3. after the PUBLIC rollback rail discharges the debt the SAME replacement action binding
   recognises the rolled-back reservation and replays toward a fresh launch (new action id);
4. only a fresh participant this action started ever owes a rollback (adopted / surfaced /
   healthy roles are zero-close), so the pointer never invites closing someone else's slot;
5. the negative matrix (action-id loss, replay before rollback) is deterministic.
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops as herdr_ops  # noqa: E501
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard_live as compdisclive  # noqa: E501
import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live as convlive  # noqa: E501
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    HerdrIdentityReplacementBindingStore,
)
from mozyo_bridge.core.state.replacement_preservation import PreservationObservation
from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionStore
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_startup_projection import (  # noqa: E501
    project_sublane_startup,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (  # noqa: E501
    PreparationObservation,
    PreparationOutcome,
    PrepareBoundPairRequest,
    _blocked,
    format_preparation_text,
    run_bound_pair_preparation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (  # noqa: E501
    BLOCK_REPLACEMENT_STOPPED,
    STATE_BLOCKED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (  # noqa: E501
    BoundSlot,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SLOT_PRESERVE_PENDING,
    SLOT_RECOVER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    CLOSE_DONE,
    OLD_SLOT_PRESENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    REASON_OK,
    REASON_PREFLIGHT,
    run_session_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding import (  # noqa: E501
    V1_BINDING_LAUNCH_UNHEALTHY,
    V1_BINDING_STARTUP_DEBT,
    V1ReplacementBindingFailure,
    launch_or_resume_v1_replacement,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    HEALTH_HEALTHY,
    HEALTH_PROVIDER_EXITED,
)

WS = "mzb1_workspace"
LANE = "issue_13948_nested_rollback_pointer_r3"
_MANAGED = ("codex", "claude")
_REQ = PrepareBoundPairRequest(
    issue="13948", journal="81811", lane=LANE, worktree="/tmp/wt-13948", branch=LANE
)


def _unhealthy_result(*, action_id, assigned, owed=True, locator="w2G:p3"):
    """A nested replacement launch: an adopted healthy sibling + a fresh, dead participant.

    Carries a private locator + raw launch detail so the projection can be proven to strip
    them; only the fresh claude slot owes a rollback (item 4).
    """
    return SessionStartResult(
        workspace_id=WS,
        lane_id=LANE,
        action_id=action_id,
        slots=[
            SlotResult(
                provider="codex",
                assigned_name=encode_assigned_name(WS, "codex", LANE),
                outcome=SLOT_ADOPTED,
                locator="private:codex:locator",
                detail="RAW-LAUNCH-DETAIL",
                health=HEALTH_HEALTHY,
                compensation=COMPENSATION_NOT_NEEDED,
            ),
            SlotResult(
                provider="claude",
                assigned_name=assigned,
                outcome=SLOT_LAUNCHED,
                locator=locator,
                detail="RAW-LAUNCH-DETAIL",
                health=HEALTH_PROVIDER_EXITED,
                compensation=(
                    COMPENSATION_ROLLBACK_OWED if owed else COMPENSATION_NOT_NEEDED
                ),
            ),
        ],
    )


class NestedStartupProjectionTest(unittest.TestCase):
    """Item 4: only the fresh participant this action started ever owes a rollback."""

    def test_mixed_adopted_and_fresh_owes_only_the_fresh_rollback(self):
        obs = project_sublane_startup(
            _unhealthy_result(action_id="a", assigned="mzb1_ws_claude")
        )
        self.assertFalse(obs.ok)
        self.assertTrue(obs.rollback_owed)
        self.assertEqual(obs.action_id, "a")
        self.assertEqual(obs.roles[0].disposition, "adopted")
        self.assertEqual(obs.roles[0].compensation, COMPENSATION_NOT_NEEDED)
        self.assertEqual(obs.roles[1].compensation, COMPENSATION_ROLLBACK_OWED)

    def test_projection_strips_the_backend_locator_and_raw_detail(self):
        payload = str(
            project_sublane_startup(
                _unhealthy_result(action_id="a", assigned="mzb1_ws_claude")
            ).as_payload()
        )
        self.assertNotIn("private:", payload)
        self.assertNotIn("RAW-LAUNCH-DETAIL", payload)


class PreparationOutcomeRollbackPointerTest(unittest.TestCase):
    """Items 2/4/5 at the public outcome surface, without any live machinery."""

    def _blocked_outcome(self, obs: SublaneStartupObservation | None) -> PreparationOutcome:
        return _blocked(
            _REQ,
            BLOCK_REPLACEMENT_STOPPED,
            detail="launch:replacement_binding_launch_unhealthy",
            action_id="approval_action_7",
            executed=True,
            replacement_status="effect_failed",
            startup=obs,
        )

    def test_pointer_names_the_startup_action_and_never_executes(self):
        obs = SublaneStartupObservation(
            ok=False, action_id="startup_act_x", roles=(), rollback_owed=True
        )
        outcome = self._blocked_outcome(obs)
        self.assertEqual(
            outcome.rollback_pointer,
            "mozyo-bridge herdr session-rollback --action-id startup_act_x",
        )
        self.assertNotIn("--execute", outcome.rollback_pointer)
        # The pointer targets the STARTUP action id, never the composer-approval action id.
        self.assertNotIn("approval_action_7", outcome.rollback_pointer)

    def test_no_debt_owed_offers_no_pointer(self):
        obs = SublaneStartupObservation(
            ok=False, action_id="startup_act_x", roles=(), rollback_owed=False
        )
        self.assertIsNone(self._blocked_outcome(obs).rollback_pointer)

    def test_action_id_loss_offers_no_pointer(self):
        obs = SublaneStartupObservation(
            ok=False, action_id="", roles=(), rollback_owed=True
        )
        self.assertIsNone(self._blocked_outcome(obs).rollback_pointer)

    def test_a_non_startup_block_is_byte_for_byte_unchanged(self):
        payload = self._blocked_outcome(None).as_payload()
        self.assertNotIn("startup", payload)
        self.assertNotIn("rollback_pointer", payload)


# --- The REAL LiveBoundPairPreparationOps.drive + REAL rollback rail (review j#82700) ---


def _observation() -> PreparationObservation:
    return PreparationObservation(
        workspace_id=WS, worktree_path=_REQ.worktree, worktree_identity="wt_test",
        branch=LANE, revision=4, generation=1, lifecycle_exact=True, pins_empty=True,
        inventory_readable=True, worktree_readable=True, worktree_clean=True,
        branch_matches=True,
        slots=(
            BoundSlot("gateway", "codex", "gw", "w1:p1", SLOT_RECOVER),
            BoundSlot("worker", "claude", "wk", "w1:p2", SLOT_PRESERVE_PENDING),
        ),
        discard_roles=("worker",),
    )


class _CloseResult:
    def __init__(self, closed=(), failed=()):
        self.closed = tuple(closed)
        self.failed = tuple(failed)


class _RollbackOps:
    """The five reads + one close the public rollback rail is allowed; close is stateful."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.inventory_readable = True
        self.close_calls = []

    def agent_rows(self):
        return list(self.rows)

    def runtime_state(self, locator):
        return "turn_ended"

    def observe_composer(self, locator):
        return (True, False)  # idle, no pending input

    def startup_blocker(self, provider, locator):
        return ""

    def open_obligations(self, workspace_id, assigned_names):
        return ()

    def close(self, workspace_id, lane_id, targets):
        self.close_calls.append(list(targets))
        for _role, locator in targets:
            self.rows = [r for r in self.rows if r.get("pane_id") != locator]
        return _CloseResult(closed=list(targets))


class RealDriveWiringTest(unittest.TestCase):
    """Item 1/2/4 end-to-end through the REAL drive, then the REAL public rollback rail.

    The mocks are ONLY external boundaries (live herdr inventory, provider resolution, the
    attestation-store lock/home, and the nested pane launch). The heal-catch projection, the
    port ``launch_startup_health`` stash, the drive's ``startup=getattr(...)``, the outcome
    ``rollback_pointer``, and ``run_session_rollback`` all execute for real, so a broken
    production connection turns this red (review j#82700 F1).
    """

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.obs = _observation()
        self.unit = StartupUnit(WS, LANE, _MANAGED)
        self.nonce = "n1"
        self.action_id = startup_action_id(self.unit, self.nonce)
        self.assigned = encode_assigned_name(WS, "claude", LANE)
        self.fresh_locator = "w2G:p3"
        # A REAL rollback-owed startup transaction the public pointer will target — exactly
        # the durable debt the real nested launch would have recorded.
        self.fence = StartupTransactionFence(home=self.home)
        action = self.fence.reserve(self.unit, self.nonce)
        self.fence.record_participant(
            action.action_id,
            Participant(
                role="claude", assigned_name=self.assigned,
                locator=self.fresh_locator, receipt="workspace=w2G",
            ),
        )
        self.fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)

    def _real_ops(self):
        outer = self

        class _Ops(compdisclive.LiveBoundPairPreparationOps):
            # Only the three pure external reads are overridden; drive/actuator/heal are real.
            def observe(self, request, *, action_id=""):
                return outer.obs

            def approval_fields(self, issue, journal):
                return self._markers

            def _composer_discardable(self, request, *, role, provider, assigned_name,
                                      locator, rows=None, action_closed_roles=()):
                return True

        ops = _Ops(
            repo_root=self.home, env={},
            transaction_store=ReplacementTransactionStore(home=self.home),
        )
        ops._markers = ()
        # The exact structured owner approval the read-only preflight mints for this pair.
        preflight = run_bound_pair_preparation(_REQ, execute=False, ops=ops)
        ops._markers = tuple(f for _c, f in marker_fields_in_note(preflight.approval_marker))
        return ops

    @contextlib.contextmanager
    def _mocked_external_boundaries(self):
        _test = self

        @contextlib.contextmanager
        def _nolock(*a, **k):
            yield

        def _fenced_nested_launch(**_kwargs):
            # The nested pane launch came up dead: fail closed carrying the raw result, exactly
            # as the real `session-start` -> v1 adapter would (this is the ONLY seam a live
            # herdr backend would own). heal_lane_column's REAL catch projects it downstream.
            raise V1ReplacementBindingFailure(
                "fenced", "nested unhealthy",
                startup_result=_unhealthy_result(
                    action_id=self.action_id, assigned=self.assigned,
                    locator=self.fresh_locator,
                ),
            )

        not_preserved = PreservationObservation(
            dirty_diff=False, running_process=False, pending_approval=False,
            identity_matches=True, attestation_fresh=True,
        )
        port = compdisclive._ComposerDiscardActuatorPort
        with contextlib.ExitStack() as stack:
            for target, name, value in [
                (compdisclive, "list_herdr_agent_rows", lambda env: []),
                (convlive, "list_herdr_agent_rows", lambda env: []),
                (herdr_ops, "evaluate_heal_runtime_fence",
                 lambda *a, **k: SimpleNamespace(ok=True, reason="", detail="")),
                (herdr_ops, "selected_attestation_store_is_v1", lambda home: True),
                (herdr_ops, "attestation_store_lock", _nolock),
                (herdr_ops, "mozyo_bridge_home", lambda: self.home),
                (herdr_ops, "launch_or_resume_v1_replacement", _fenced_nested_launch),
            ]:
                stack.enter_context(mock.patch.object(target, name, value))
            for cls, name, value in [
                (herdr_ops.HerdrSublaneActuatorOps, "_live_rows", lambda self: []),
                (herdr_ops.HerdrSublaneActuatorOps, "_launch_providers",
                 lambda self: _MANAGED),
                (herdr_ops.HerdrSublaneActuatorOps, "_resolve_lane_slots",
                 lambda self, wt, rows, managed=None: (
                     WS, LANE, {"codex": ("", ""), "claude": ("", "")})),
                (port, "observe_old_slot", lambda self, pin: OLD_SLOT_PRESENT),
                (port, "observe_preservation", lambda self, pin: not_preserved),
                (port, "close_exact_generation", lambda self, pin: CLOSE_DONE),
                (port, "_fresh_authority",
                 lambda port_self, *, require_attested_roles=(): _test.obs),
            ]:
                stack.enter_context(mock.patch.object(cls, name, value))
            yield

    def test_nested_failure_surfaces_pointer_and_the_real_rail_discharges_it(self):
        ops = self._real_ops()
        with self._mocked_external_boundaries():
            outcome = run_bound_pair_preparation(_REQ, execute=True, ops=ops)

        # (1) the public outcome surfaces the SAME startup action's typed pointer, projected
        # through the real heal catch / port stash / drive getattr — no raw fact leaks.
        self.assertEqual(outcome.state, STATE_BLOCKED)
        self.assertEqual(outcome.reason, BLOCK_REPLACEMENT_STOPPED)
        self.assertEqual(
            outcome.rollback_pointer,
            f"mozyo-bridge herdr session-rollback --action-id {self.action_id}",
        )
        self.assertNotIn("--execute", outcome.rollback_pointer)
        payload = outcome.as_payload()
        self.assertTrue(payload["startup"]["rollback_owed"])
        self.assertNotIn("private:", str(payload))
        self.assertNotIn("RAW-LAUNCH-DETAIL", str(payload))
        self.assertIn(
            f"rollback_pointer: mozyo-bridge herdr session-rollback --action-id {self.action_id}",
            format_preparation_text(outcome),
        )

        # (2) the action id taken FROM the public pointer drives the REAL rollback rail:
        # read-only preflight closes nothing; execute discharges the debt.
        action_id = outcome.rollback_pointer.split("--action-id ", 1)[1]
        rows = [{
            "name": self.assigned, "pane_id": self.fresh_locator,
            "agent": "claude", "agent_status": "idle",
        }]
        preflight = run_session_rollback(
            action_id=action_id, ops=_RollbackOps(rows),
            fence=StartupTransactionFence(home=self.home), execute=False,
        )
        self.assertEqual(preflight.reason, REASON_PREFLIGHT)
        self.assertFalse(preflight.executed)

        exec_ops = _RollbackOps(rows)
        discharged = run_session_rollback(
            action_id=action_id, ops=exec_ops,
            fence=StartupTransactionFence(home=self.home), execute=True,
        )
        self.assertEqual(discharged.reason, REASON_OK)
        self.assertTrue(exec_ops.close_calls)


# --- The v1 replacement-binding adapter over the REAL public rollback rail -------------


class V1ReplacementBindingRollbackRailTest(unittest.TestCase):
    """Items 3/5 over the REAL binding adapter AND the REAL public rollback rail.

    Complementary to :class:`RealDriveWiringTest`: here ``launch_or_resume_v1_replacement``
    runs unpatched (real binding-store reserve / fence interplay), so the rolled-back
    reservation replay and the startup-debt terminal cannot be faked. Only the nested pane
    launch is a fake, and it records the fence exactly as the real ``session-start`` would.
    """

    OLD_LOCATOR = "w1:p9"
    FRESH_LOCATOR = "w2G:p3"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.action_id = "replacement_action_13948"
        self.assigned = encode_assigned_name(WS, "claude", LANE)
        self.unit = StartupUnit(WS, LANE, _MANAGED)
        self.launch_calls = []

    def _unhealthy_launch(self, nonce, startup_fence):
        """Mimic prepare_session on an unhealthy launch: record a rollback-owed fence action
        (so the REAL rollback rail can act on it) and return the unhealthy result."""
        self.launch_calls.append(nonce)
        action = startup_fence.reserve(self.unit, nonce)
        startup_fence.record_participant(
            action.action_id,
            Participant(
                role="claude", assigned_name=self.assigned,
                locator=self.FRESH_LOCATOR, receipt="workspace=w2G",
            ),
        )
        startup_fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)
        return _unhealthy_result(
            action_id=action.action_id, assigned=self.assigned, locator=self.FRESH_LOCATOR,
        )

    def _launch_or_resume(self, launch):
        launch_or_resume_v1_replacement(
            home=self.home, action_id=self.action_id, assigned_name=self.assigned,
            old_locator=self.OLD_LOCATOR, target_provider="claude", workspace_id=WS,
            lane_id=LANE, managed_pair=_MANAGED, rows=(), existing={"claude": ("", "")},
            launch=launch,
        )

    def _intent(self):
        return HerdrIdentityReplacementBindingStore(home=self.home).read(
            self.action_id, self.assigned
        )

    def _rollback_rows(self):
        return [{
            "name": self.assigned, "pane_id": self.FRESH_LOCATOR,
            "agent": "claude", "agent_status": "idle",
        }]

    def test_public_rollback_then_replay_converges_to_a_new_action_id(self):
        # (1) nested failure carries the nested result out of the real adapter.
        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(caught.exception.reason, V1_BINDING_LAUNCH_UNHEALTHY)
        first_action = self._intent().startup_action_id
        self.assertEqual(
            project_sublane_startup(caught.exception.startup_result).action_id, first_action
        )

        # (2) the REAL public rollback rail discharges the debt (never a manual set_phase).
        fence = StartupTransactionFence(home=self.home)
        pre = run_session_rollback(
            action_id=first_action, ops=_RollbackOps(self._rollback_rows()),
            fence=fence, execute=False,
        )
        self.assertEqual(pre.reason, REASON_PREFLIGHT)
        done = run_session_rollback(
            action_id=first_action, ops=_RollbackOps(self._rollback_rows()),
            fence=fence, execute=True,
        )
        self.assertEqual(done.reason, REASON_OK)

        # (3) replay: the SAME binding recognises the rolled-back reservation and relaunches
        # under a NEW startup action id (convergence, not a startup-debt dead end).
        self.launch_calls.clear()
        with self.assertRaises(V1ReplacementBindingFailure) as replay:
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(replay.exception.reason, V1_BINDING_LAUNCH_UNHEALTHY)
        self.assertEqual(len(self.launch_calls), 1)
        self.assertNotEqual(self._intent().startup_action_id, first_action)

    def test_replay_before_public_rollback_is_startup_debt_and_never_relaunches(self):
        # Item 5 terminal replay: without the public rollback the startup transaction is not
        # durably rolled back, so a bare replay fails closed on startup debt and never relaunches.
        with self.assertRaises(V1ReplacementBindingFailure):
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(len(self.launch_calls), 1)

        def _must_not_launch(nonce, startup_fence):  # pragma: no cover - asserted absent
            raise AssertionError("a replay before rollback must not relaunch")

        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(_must_not_launch)
        self.assertEqual(caught.exception.reason, V1_BINDING_STARTUP_DEBT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
