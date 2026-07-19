"""Redmine #13948 R3 — a nested replacement-launch failure must surface losslessly.

R2 (j#81717) taught the embedded ``sublane create/start`` path to stop before dispatch on a
non-positive embedded ``session-start`` and to point at the SAME action's public rollback.
R3 (j#81811) closes the *nested* leak: when ``prepare-bound-pair --execute`` converges a
hibernated bound pair through the #13933 v1 replacement-binding adapter and the fresh
participant does not reach bounded startup health, the inner
:class:`SessionStartResult` carried the typed startup ``action_id`` / per-role health /
rollback debt, but the outer public outcome collapsed it to a generic
``replacement_binding_launch_unhealthy`` detail string with no actionable rollback pointer.

These regressions pin the R3 contract:

1. the nested unhealthy launch propagates the typed ``action_id`` / role health / rollback
   debt / explicit rollback pointer outward losslessly — no raw locator / detail / secret;
2. the public pointer is exactly ``mozyo-bridge herdr session-rollback --action-id <id>``
   with NO ``--execute`` — a launch closes nothing (Answer j#80991);
3. after a public rollback the SAME replacement action binding recognises the rolled-back
   reservation and replays toward a fresh launch (it does not dead-end on startup debt);
4. only a fresh participant this action started ever owes a rollback (adopted / surfaced /
   healthy roles are zero-close), so the pointer never invites closing someone else's slot;
5. the negative matrix (action-id loss, replay before rollback) is deterministic.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    HerdrIdentityReplacementBindingStore,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_HEALTH_CHECK,
    PHASE_ROLLBACK_OWED,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_startup_projection import (  # noqa: E501
    project_sublane_startup,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (  # noqa: E501
    PreparationDrive,
    PreparationOutcome,
    PrepareBoundPairRequest,
    _blocked,
    format_preparation_text,
    run_bound_pair_preparation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (  # noqa: E501
    BLOCK_REPLACEMENT_STOPPED,
    STATE_BLOCKED,
    expectation_for,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
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
    HEALTH_RECEIVER_UNREADABLE,
)


def _nested_result(*, action_id="startup_act_13948", owed=True):
    """A nested replacement launch: an adopted healthy sibling + a fresh, dead participant.

    The fresh participant carries a private locator and a raw launch detail exactly so the
    projection can be proven to strip them.
    """
    return SessionStartResult(
        workspace_id="mzb1_workspace",
        lane_id="issue_13948_nested_rollback_pointer_r3",
        action_id=action_id,
        slots=[
            SlotResult(
                provider="codex",
                assigned_name="mzb1_workspace_codex_issue",
                outcome=SLOT_ADOPTED,
                locator="private:codex:locator",
                detail="RAW-LAUNCH-DETAIL",
                health=HEALTH_HEALTHY,
                compensation=COMPENSATION_NOT_NEEDED,
            ),
            SlotResult(
                provider="claude",
                assigned_name="mzb1_workspace_claude_issue",
                outcome=SLOT_LAUNCHED,
                locator="private:claude:locator",
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
        obs = project_sublane_startup(_nested_result())
        self.assertFalse(obs.ok)
        self.assertTrue(obs.rollback_owed)
        self.assertEqual(obs.action_id, "startup_act_13948")
        # The adopted sibling never carries a compensation; only the fresh, dead slot does.
        self.assertEqual(obs.roles[0].disposition, "adopted")
        self.assertEqual(obs.roles[0].compensation, COMPENSATION_NOT_NEEDED)
        self.assertEqual(obs.roles[1].compensation, COMPENSATION_ROLLBACK_OWED)

    def test_projection_strips_the_backend_locator_and_raw_detail(self):
        payload = str(project_sublane_startup(_nested_result()).as_payload())
        self.assertNotIn("private:", payload)
        self.assertNotIn("RAW-LAUNCH-DETAIL", payload)


class PreparationOutcomeRollbackPointerTest(unittest.TestCase):
    """Items 1/2/4/5 at the public outcome surface, without any live machinery."""

    _REQ = PrepareBoundPairRequest(
        issue="13948",
        journal="81811",
        lane="issue_13948_nested_rollback_pointer_r3",
        worktree="/tmp/wt-13948",
        branch="issue_13948_nested_rollback_pointer_r3",
    )

    def _blocked_outcome(self, obs: SublaneStartupObservation | None) -> PreparationOutcome:
        return _blocked(
            self._REQ,
            BLOCK_REPLACEMENT_STOPPED,
            detail="launch:replacement_binding_launch_unhealthy",
            action_id="approval_action_7",
            executed=True,
            replacement_status="effect_failed",
            startup=obs,
        )

    def test_pointer_names_the_startup_action_and_never_executes(self):
        outcome = self._blocked_outcome(project_sublane_startup(_nested_result()))
        self.assertEqual(
            outcome.rollback_pointer,
            "mozyo-bridge herdr session-rollback --action-id startup_act_13948",
        )
        self.assertNotIn("--execute", outcome.rollback_pointer)
        # The pointer targets the STARTUP action id, never the composer-approval action id.
        self.assertNotIn("approval_action_7", outcome.rollback_pointer)

    def test_payload_carries_startup_and_pointer_without_leaking_raw_facts(self):
        outcome = self._blocked_outcome(project_sublane_startup(_nested_result()))
        payload = outcome.as_payload()
        self.assertIn("startup", payload)
        self.assertEqual(
            payload["rollback_pointer"],
            "mozyo-bridge herdr session-rollback --action-id startup_act_13948",
        )
        self.assertNotIn("private:", str(payload))
        self.assertNotIn("RAW-LAUNCH-DETAIL", str(payload))

    def test_text_surfaces_the_health_and_the_pointer(self):
        text = format_preparation_text(
            self._blocked_outcome(project_sublane_startup(_nested_result()))
        )
        self.assertIn("startup_action_id: startup_act_13948", text)
        self.assertIn("startup_rollback_owed: true", text)
        self.assertIn(
            "rollback_pointer: mozyo-bridge herdr session-rollback "
            "--action-id startup_act_13948",
            text,
        )

    def test_no_debt_owed_offers_no_pointer(self):
        # An uncertain (unreadable-pane) launch that owes no rollback still blocks, but there
        # is nothing this action may compensate, so no pointer is invented.
        obs = project_sublane_startup(_nested_result(owed=False))
        self.assertFalse(obs.rollback_owed)
        outcome = self._blocked_outcome(obs)
        self.assertIsNone(outcome.rollback_pointer)

    def test_action_id_loss_offers_no_pointer(self):
        # Item 5 negative: a rollback-owed observation whose action id was lost cannot name a
        # target, so the rail refuses to print a pointer that would roll back nothing.
        obs = SublaneStartupObservation(
            ok=False, action_id="", roles=(), rollback_owed=True
        )
        outcome = self._blocked_outcome(obs)
        self.assertIsNone(outcome.rollback_pointer)

    def test_a_non_startup_block_is_byte_for_byte_unchanged(self):
        # Every other blocked outcome keeps its historical payload: the additive keys appear
        # only when a nested startup failure was actually observed.
        outcome = self._blocked_outcome(None)
        payload = outcome.as_payload()
        self.assertNotIn("startup", payload)
        self.assertNotIn("rollback_pointer", payload)
        self.assertIsNone(outcome.rollback_pointer)


# --- Integration through the public run_bound_pair_preparation ------------------------

_REQ = PrepareBoundPairRequest(
    issue="13948",
    journal="81811",
    lane="issue_13948_nested_rollback_pointer_r3",
    worktree="/tmp/wt-13948",
    branch="issue_13948_nested_rollback_pointer_r3",
)


def _slot(role: str, disposition: str) -> BoundSlot:
    provider = "codex" if role == "gateway" else "claude"
    locator = "w1:p1" if role == "gateway" else "w1:p2"
    return BoundSlot(role, provider, f"managed-{role}", locator, disposition)


def _observation(**changes):
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (  # noqa: E501
        PreparationObservation,
    )

    values = dict(
        workspace_id="mzb1_workspace",
        worktree_path=_REQ.worktree,
        worktree_identity="wt_deadbeef",
        branch=_REQ.branch,
        revision=4,
        generation=1,
        lifecycle_exact=True,
        pins_empty=True,
        inventory_readable=True,
        worktree_readable=True,
        worktree_clean=True,
        branch_matches=True,
        slots=(
            _slot("gateway", SLOT_PRESERVE_PENDING),
            _slot("worker", SLOT_RECOVER),
        ),
        discard_roles=("gateway",),
    )
    values.update(changes)
    return PreparationObservation(**values)


class _FakeOps:
    def __init__(self):
        self.observation = _observation()
        self.markers = ()
        self.drive_result = PreparationDrive(True, "recovered")

    def observe(self, request, *, action_id=""):
        return self.observation

    def approval_fields(self, issue, journal):
        return self.markers

    def drive(self, request, expectation, initial):
        return self.drive_result


class PrepareBoundPairSurfaceIntegrationTest(unittest.TestCase):
    """Item 1 end-to-end: a nested unhealthy launch reaches the public outcome as a pointer."""

    def _authorize(self, ops: _FakeOps):
        preflight = run_bound_pair_preparation(_REQ, execute=False, ops=ops)
        [(channel, fields)] = marker_fields_in_note(preflight.approval_marker)
        assert channel == "workflow-event"
        ops.markers = (fields,)

    def test_nested_launch_unhealthy_surfaces_the_pointer_on_execute(self):
        ops = _FakeOps()
        self._authorize(ops)
        obs = project_sublane_startup(_nested_result())
        ops.drive_result = PreparationDrive(
            False,
            "effect_failed",
            "launch:replacement_binding_launch_unhealthy",
            startup=obs,
        )
        outcome = run_bound_pair_preparation(_REQ, execute=True, ops=ops)
        self.assertEqual(outcome.state, STATE_BLOCKED)
        self.assertEqual(outcome.reason, BLOCK_REPLACEMENT_STOPPED)
        self.assertEqual(outcome.replacement_status, "effect_failed")
        self.assertEqual(
            outcome.rollback_pointer,
            "mozyo-bridge herdr session-rollback --action-id startup_act_13948",
        )
        self.assertEqual(outcome.as_payload()["startup"]["action_id"], "startup_act_13948")

    def test_a_launch_stop_without_startup_health_keeps_the_bare_typed_status(self):
        # A non-v1 launch stop carries no startup observation; the outcome is still a typed
        # replacement_status block, just with no pointer (unchanged from #13933).
        ops = _FakeOps()
        self._authorize(ops)
        ops.drive_result = PreparationDrive(False, "effect_failed", "launch:launch_error")
        outcome = run_bound_pair_preparation(_REQ, execute=True, ops=ops)
        self.assertEqual(outcome.replacement_status, "effect_failed")
        self.assertIsNone(outcome.rollback_pointer)
        self.assertNotIn("startup", outcome.as_payload())


# --- The v1 replacement-binding adapter seam ------------------------------------------


class V1ReplacementBindingRollbackDebtTest(unittest.TestCase):
    """Items 1/3/5 at the adapter that produces the nested failure."""

    WORKSPACE = "mzb1_workspace"
    LANE = "issue_13948_nested_rollback_pointer_r3"
    PROVIDER = "claude"
    MANAGED_PAIR = ("codex", "claude")
    OLD_LOCATOR = "w1:p9"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.action_id = "replacement_action_13948"
        self.assigned = encode_assigned_name(self.WORKSPACE, self.PROVIDER, self.LANE)
        self.launch_calls = []

    def _unhealthy_launch(self, nonce, startup_fence):
        # The real closure runs prepare_session; the fake returns exactly the shape the adapter
        # inspects: one fresh launched participant, at the reserved startup action id, that did
        # not reach bounded health (ok=False).
        self.launch_calls.append(nonce)
        action = startup_action_id(
            StartupUnit(self.WORKSPACE, self.LANE, self.MANAGED_PAIR), nonce
        )
        return SessionStartResult(
            workspace_id=self.WORKSPACE,
            lane_id=self.LANE,
            action_id=action,
            slots=[
                SlotResult(
                    provider=self.PROVIDER,
                    assigned_name=self.assigned,
                    outcome=SLOT_LAUNCHED,
                    locator="w1:pFresh",
                    health=HEALTH_RECEIVER_UNREADABLE,
                    compensation=COMPENSATION_ROLLBACK_OWED,
                )
            ],
        )

    def _launch_or_resume(self, launch):
        launch_or_resume_v1_replacement(
            home=self.home,
            action_id=self.action_id,
            assigned_name=self.assigned,
            old_locator=self.OLD_LOCATOR,
            target_provider=self.PROVIDER,
            workspace_id=self.WORKSPACE,
            lane_id=self.LANE,
            managed_pair=self.MANAGED_PAIR,
            rows=(),
            existing={self.PROVIDER: ("", "")},
            launch=launch,
        )

    def _intent(self):
        return HerdrIdentityReplacementBindingStore(home=self.home).read(
            self.action_id, self.assigned
        )

    def test_unhealthy_fresh_launch_carries_the_nested_result(self):
        # Item 1: the fresh-launch failure raises the typed reason AND carries the raw
        # SessionStartResult so the caller can project the rollback pointer.
        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(self._unhealthy_launch)
        exc = caught.exception
        self.assertEqual(exc.reason, V1_BINDING_LAUNCH_UNHEALTHY)
        self.assertIsNotNone(exc.startup_result)
        self.assertFalse(exc.startup_result.ok)
        # And the carried result projects into an owed, pointer-bearing observation.
        obs = project_sublane_startup(exc.startup_result)
        self.assertTrue(obs.rollback_owed)
        self.assertEqual(obs.action_id, self._intent().startup_action_id)

    def test_replay_before_rollback_is_startup_debt_and_never_relaunches(self):
        # Item 5 (terminal replay): after the first unhealthy attempt the binding is reserved,
        # but the startup transaction is not durably rolled back, so a bare replay fails closed
        # on startup debt and launches nothing.
        with self.assertRaises(V1ReplacementBindingFailure):
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(len(self.launch_calls), 1)

        def _must_not_launch(nonce, startup_fence):  # pragma: no cover - asserted absent
            raise AssertionError("a replay before rollback must not relaunch")

        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(_must_not_launch)
        self.assertEqual(caught.exception.reason, V1_BINDING_STARTUP_DEBT)

    def test_a_rolled_back_reservation_is_recognised_and_replayed(self):
        # Item 3: once the SAME startup action is durably rolled back, the reservation is
        # replaced and a fresh launch (a new startup nonce / action id) is attempted — the
        # convergence path, not a permanent startup-debt dead end.
        with self.assertRaises(V1ReplacementBindingFailure):
            self._launch_or_resume(self._unhealthy_launch)
        first_startup_id = self._intent().startup_action_id

        # Durably roll back that exact startup action (what `herdr session-rollback --execute`
        # records), then replay.
        fence = StartupTransactionFence(home=self.home)
        nonce1 = self._intent().startup_nonce
        fence.reserve(StartupUnit(self.WORKSPACE, self.LANE, self.MANAGED_PAIR), nonce1)
        fence.set_phase(first_startup_id, PHASE_HEALTH_CHECK)
        fence.set_phase(first_startup_id, PHASE_ROLLBACK_OWED)
        fence.set_phase(first_startup_id, PHASE_COMPLETED_ROLLED_BACK)

        self.launch_calls.clear()
        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(self._unhealthy_launch)
        # The rolled-back reservation was recognised (it replayed) rather than dead-ending on
        # startup debt, and it minted a NEW startup action id for the fresh attempt.
        self.assertEqual(caught.exception.reason, V1_BINDING_LAUNCH_UNHEALTHY)
        self.assertEqual(len(self.launch_calls), 1)
        self.assertNotEqual(self._intent().startup_action_id, first_startup_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
