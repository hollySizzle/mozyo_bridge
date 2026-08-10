"""Redmine #15193 — a stopped lane whose receiver has BOTH a generation mismatch and a
pending composer input could not be converged by any supported rail.

## The three reproductions

All three reduce to one state, and these tests pin that reduction:

- **#15110 j#102068** «Redmine anchor provider拒否がdispatch fenceを誤ってuncertain化する» —
  issue open and explicitly parked, clean worktree, gateway and worker both live and
  `awaiting_input`. `sublane hibernate` dry-run returned `may_hibernate=true`; `--execute`
  then blocked with `composer_pending_real` / `release_boundary_mutation`; and the
  `quarantine-inspect` it pointed at answered `generation_mismatch` /
  `not_quarantine_candidate`, minting no approval.
- **#15140 j#102064** «remote hostとDev ContainerのUnit統合表示・操作を軽量実機確認する» —
  the same contradiction reproduced independently on a different lane.
- **#15195 j#102193 / j#102218** — the managed worker classified `generation_mismatch`, the
  first `dispatch-worker` preflight was a zero-send `worker_liveness_authority_conflict`, and
  `quarantine-inspect` again refused with `not_quarantine_candidate`.

## Why it deadlocked

`classify_pending_composer`'s precedence puts `generation_mismatch` ABOVE the pending fact,
so the label carried no evidence that an unsent input existed; `decide_approval_readiness`
then refused because the classification is not quarantine-eligible. Meanwhile hibernate's
release boundary probes the composer directly, blocks on the pending input, and names
`owner_approved_quarantine` as the next action. Hibernate pointed at quarantine, quarantine
refused, and no supported rail could dispose of the input — which is precisely when an
operator reaches for the prohibited moves (force kill, raw Herdr/tmux, blind Enter,
discarding the composer).

## What these tests pin

The convergence, and every refusal that must survive it. The disposition rail must NOT become
a general-purpose override: a working agent, a foreign lane, an ambiguous inventory, an
unreadable composer, a drifted generation and a stale approval each still produce zero
mutation, and a duplicate execute is idempotent rather than a second close.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_RETIRED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    LaneLifecycleError,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    REPLACEMENT_NOT_REQUESTED,
    REPLACEMENT_REPLACED,
    REPLACEMENT_REQUESTED,
)
from mozyo_bridge.core.state.lane_replacement import LaneReplacementStore
from mozyo_bridge.core.state.lane_replacement_model import quarantine_action_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_quarantine as quarantine_module,
    sublane_quarantine_inspect as inspect_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_preflight import (  # noqa: E501
    HibernatePreflight,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    BLOCK_COMPOSER_PENDING_REAL,
    BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,
    NEXT_ACTION_OWNER_APPROVED_QUARANTINE,
    release_boundary_next_actions,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    CloseReceiverResult,
    FreshReceiverVerification,
    QuarantineInspection,
    QuarantineRequest,
    SublaneQuarantineUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine_inspect import (  # noqa: E501
    QuarantineInspectRequest,
    SublaneQuarantineInspectUseCase,
    format_inspect_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.generation_mismatch_disposition import (  # noqa: E501
    DISPOSITION_AGENT_WORKING,
    DISPOSITION_APPROVAL_INCOMPLETE,
    DISPOSITION_COMPOSER_UNREADABLE,
    DISPOSITION_LIFECYCLE_ABSENT,
    DISPOSITION_LIFECYCLE_PINS_INVALID,
    DISPOSITION_LIFECYCLE_UNREADABLE,
    DISPOSITION_READY,
    DISPOSITION_RECEIVER_ABSENT,
    DRIFT_LANE_GENERATION,
    DRIFT_LIFECYCLE_REVISION,
    PENDING_EFFECT_DISCARDED_ON_REPLACE,
    PENDING_EFFECT_PRESERVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.quarantine_approval import (  # noqa: E501
    APPROVAL_NOT_QUARANTINE_CANDIDATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    GEN_AXIS_PAIR,
    GENERATION_MISMATCH,
    PendingComposerSignal,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
ISSUE = "15193"
FOREIGN_ISSUE = "15166"
LANE = "issue_15193_generation_mismatch_disposition"
ROLE = "claude"
JOURNAL = "102219"
APPROVAL_JOURNAL = "102900"

NAME = encode_assigned_name(WS, ROLE, LANE)
OLD_LOCATOR = f"{WS}:p18"
FRESH_LOCATOR = f"{WS}:p24"
ATTESTED_AT = "2026-08-10T07:00:00+00:00"
APPROVED_AT = "2026-08-10T07:30:00+00:00"
AGENT_REVISION = 4
LANE_GENERATION = 1
LIFECYCLE_REVISION = 1
ACTION = quarantine_action_id(lane_id=LANE, role=ROLE, locator=OLD_LOCATOR)

#: The measured axis in j#102624: the worker was live but the gateway/worker pair did not
#: resolve to one shared placement, so `generation_matches` folded to False.
AXES = (GEN_AXIS_PAIR,)
#: An uncorrelated pending input — nothing in the delivery ledger matches it, which is why
#: the q-enter rail cannot drive it either.
PENDING_ID = "pending:uncorrelated"

#: Never allowed to reach any output on any path (the value-non-exposure contract).
SECRET_BODY = "未送信の下書き — private composer body"


def _signal(**kw) -> PendingComposerSignal:
    """The exact #15193 receiver: attested, idle, generation-mismatched, holding real input."""
    base = dict(
        inventory_readable=True,
        has_pending=True,
        agent_state="idle",
        identity_attested=True,
        generation_matches=False,
        correlated_marker_ids=(),
        correlation_ambiguous=False,
        generation_axes=AXES,
    )
    base.update(kw)
    return PendingComposerSignal(**base)


class _FakeOps:
    """Fake quarantine IO port: canned classification + recorded actuation."""

    def __init__(self, *, signal=None, receiver_present=True, row_revision=AGENT_REVISION):
        self._signal = signal if signal is not None else _signal()
        self._receiver_present = receiver_present
        self._row_revision = row_revision
        self.closed_pins: list[ReleasePin] = []
        self.heals = 0

    def inspect(self, request: QuarantineRequest) -> QuarantineInspection:
        return QuarantineInspection(
            workspace_id=WS,
            signal=self._signal,
            row_revision=self._row_revision,
            attested_at=ATTESTED_AT,
            receiver_present=self._receiver_present,
        )

    def close_receiver(self, request, pin) -> CloseReceiverResult:
        self.closed_pins.append(pin)
        return CloseReceiverResult(True)

    def heal_receiver(self, request) -> None:
        self.heals += 1

    def verify_fresh_receiver(self, request, *, fresh_after) -> FreshReceiverVerification:
        return FreshReceiverVerification(True, locator=FRESH_LOCATOR)


def _request(**kw) -> QuarantineRequest:
    """A COMPLETE disposition request (all three #15193 tokens present)."""
    base = dict(
        issue=ISSUE,
        lane=LANE,
        journal=APPROVAL_JOURNAL,
        role=ROLE,
        assigned_name=NAME,
        locator=OLD_LOCATOR,
        action_generation=ACTION,
        approval_observed_at=APPROVED_AT,
        approved_revision=AGENT_REVISION,
        approved_generation_axes=AXES,
        approved_pending_identity=PENDING_ID,
        approved_pending_effect=PENDING_EFFECT_DISCARDED_ON_REPLACE,
        approved_lane_generation=LANE_GENERATION,
        approved_lifecycle_revision=LIFECYCLE_REVISION,
    )
    base.update(kw)
    return QuarantineRequest(**base)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, LANE)
        self.lifecycle = LaneLifecycleStore(home=self.home)
        self.store = LaneReplacementStore(home=self.home)

    def _active_lane(self, issue: str = ISSUE) -> None:
        self.lifecycle.declare_active(
            self.key,
            decision=DecisionPointer(source="redmine", issue_id=issue, journal_id=JOURNAL),
            issue_id=issue,
        )

    def _run(self, ops, *, execute=True, request=None):
        return SublaneQuarantineUseCase(ops=ops, store=self.store).run(
            request or _request(), execute=execute
        )

    def _inspect(self, *, signal=None, receiver_present=True, rows=None):
        """Run the read-only preflight over a canned observation."""
        ops = _FakeOps(signal=signal, receiver_present=receiver_present)
        inspection = ops.inspect(_request())
        rows = rows if rows is not None else [{"name": NAME, "pane_id": OLD_LOCATOR,
                                               "revision": AGENT_REVISION}]

        class _Ops:
            def inspect(self, request):
                return inspection

        use_case = SublaneQuarantineInspectUseCase(
            repo_root=Path("/tmp/repo"),
            rows_reader=lambda: rows,
            ops_factory=lambda _rows: _Ops(),
            lifecycle_reader=lambda _ws, _lane: (
                LANE_GENERATION,
                LIFECYCLE_REVISION,
            ),
        )
        original = inspect_module.repo_scope_workspace_id
        inspect_module.repo_scope_workspace_id = lambda _root: WS
        try:
            return use_case.run(QuarantineInspectRequest(issue=ISSUE, lane=LANE, role=ROLE))
        finally:
            inspect_module.repo_scope_workspace_id = original


class DeadlockReproductionTest(_Case):
    """The #15110 / #15140 / #15195 shape now converges instead of dead-ending."""

    def test_quarantine_still_refuses_this_receiver(self) -> None:
        # UNCHANGED and deliberate: a generation-mismatched receiver is not a quarantine
        # candidate. The bug was never that this refusal was wrong — it was that it was the
        # ONLY answer available.
        out = self._inspect()
        self.assertEqual(out.classification.label, GENERATION_MISMATCH)
        self.assertFalse(out.classification.quarantine_candidate)
        self.assertEqual(out.approval_reason, APPROVAL_NOT_QUARANTINE_CANDIDATE)
        self.assertFalse(out.approval_ready)

    def test_the_disposition_rail_converges_where_quarantine_cannot(self) -> None:
        out = self._inspect()
        self.assertEqual(out.disposition_reason, DISPOSITION_READY)
        self.assertTrue(out.disposition_ready)
        # The whole point: the command no longer reports a dead end as a failure.
        self.assertFalse(out.is_blocked)

    def test_the_pending_input_survives_the_classification(self) -> None:
        # The root defect: the collapsed label destroyed the co-observed pending fact, so no
        # surface could tell "mismatch, nothing at stake" from "mismatch, real unsent input".
        out = self._inspect()
        self.assertTrue(out.classification.pending_observed)
        self.assertEqual(out.classification.generation_axes, AXES)
        self.assertTrue(out.classification.generation_mismatch_with_pending)

    def test_the_approval_names_the_exact_mismatch_and_the_discard(self) -> None:
        out = self._inspect()
        facts = out.disposition_facts
        self.assertEqual(facts.generation_axes, AXES)
        self.assertEqual(facts.pending_identity, PENDING_ID)
        self.assertEqual(facts.pending_effect, PENDING_EFFECT_DISCARDED_ON_REPLACE)
        self.assertEqual(facts.lane_generation, LANE_GENERATION)
        self.assertEqual(facts.lifecycle_revision, LIFECYCLE_REVISION)
        # Stated in words, not only as a token — the operator approves a discard knowingly.
        self.assertIn("破棄する", out.disposition_template)

    def test_hibernate_next_action_points_at_a_rail_that_can_converge(self) -> None:
        # Hibernate's boundary told the operator to go to quarantine, which refuses this
        # receiver. It now routes through quarantine-inspect, which answers for both rails.
        actions = release_boundary_next_actions((BLOCK_COMPOSER_PENDING_REAL,))
        self.assertEqual(actions.primary, NEXT_ACTION_OWNER_APPROVED_QUARANTINE)
        detail = actions.details[NEXT_ACTION_OWNER_APPROVED_QUARANTINE]
        self.assertIn("quarantine-inspect", detail)
        self.assertIn("15193", detail)

    def test_no_composer_body_reaches_any_output(self) -> None:
        out = self._inspect(signal=_signal(correlated_marker_ids=()))
        rendered = format_inspect_text(out)
        payload = repr(out.as_payload())
        for surface in (rendered, payload, out.disposition_template):
            self.assertNotIn(SECRET_BODY, surface)
            self.assertNotIn("下書き", surface)


class DryRunActionTimeParityTest(unittest.TestCase):
    """Acceptance: the dry-run explains its difference from action time, and matches it."""

    def _preflight(self, **kw) -> HibernatePreflight:
        base = dict(
            original_identity_known=True,
            park_satisfied=True,
            obligations_satisfied=True,
            lane_idle=True,
            boundary_ok=True,
        )
        base.update(kw)
        return HibernatePreflight(**base)

    def test_dry_run_returns_the_same_token_the_boundary_would(self) -> None:
        # #15110 j#102068: dry-run said `may_hibernate=true`, `--execute` said
        # `composer_pending_real`. Both now say `composer_pending_real`.
        preflight = self._preflight(live_composer_pending=True)
        self.assertFalse(preflight.may_hibernate)
        self.assertIn(BLOCK_COMPOSER_PENDING_REAL, preflight.blocked_reasons)

    def test_a_quiescent_lane_is_unaffected(self) -> None:
        preflight = self._preflight()
        self.assertTrue(preflight.may_hibernate)
        self.assertEqual(preflight.unverified_axes, ())

    def test_an_unreadable_probe_does_not_block_the_dry_run_but_says_so(self) -> None:
        # A dry-run is a diagnostic: failing it closed on an unreadable probe would deny the
        # operator the report they ran it for. The gap is reported instead of hidden.
        preflight = self._preflight(live_activity_readable=False)
        self.assertTrue(preflight.may_hibernate)
        self.assertEqual(
            preflight.unverified_axes, (BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,)
        )

    def test_the_payload_carries_the_parity_facts(self) -> None:
        payload = self._preflight(live_composer_pending=True).as_payload()
        self.assertTrue(payload["live_composer_pending"])
        self.assertIn(BLOCK_COMPOSER_PENDING_REAL, payload["blocked_reasons"])


class DispositionExecuteTest(_Case):
    """The owner-approved rail actuates exactly once, over exactly the approved state."""

    def test_a_complete_disposition_replaces_the_exact_receiver(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_REPLACED)
        self.assertEqual(len(ops.closed_pins), 1)
        self.assertEqual(ops.closed_pins[0].locator, OLD_LOCATOR)
        self.assertEqual(ops.closed_pins[0].assigned_name, NAME)

    def test_preserving_the_input_is_refused_because_a_replacement_cannot_honour_it(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(
            ops, request=_request(approved_pending_effect=PENDING_EFFECT_PRESERVED)
        )
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("pending effect", outcome.detail)

    def test_a_duplicate_execute_is_idempotent_and_never_closes_twice(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        self._run(ops)
        first_closes = len(ops.closed_pins)
        second = self._run(ops)
        self.assertEqual(len(ops.closed_pins), first_closes)
        self.assertEqual(second.replacement_state, REPLACEMENT_REPLACED)
        self.assertIn("idempotent", second.detail)

    def test_preflight_without_execute_mutates_nothing(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(ops, execute=False)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(ops.heals, 0)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_NOT_REQUESTED)


class ZeroMutationTest(_Case):
    """Ambiguous / foreign / active work / unreadable authority -> zero mutation."""

    def _assert_no_mutation(self, ops, outcome) -> None:
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(ops.heals, 0)
        self.assertEqual(self.lifecycle.get(self.key).replacement_state,
                         REPLACEMENT_NOT_REQUESTED)
        self.assertNotEqual(outcome.replacement_state, REPLACEMENT_REPLACED)

    def test_a_partial_token_set_is_not_a_disposition(self) -> None:
        # Every token or none: a partial set must never unlock the mismatch path while
        # leaving the pending input's fate unstated.
        self._active_lane()
        for missing in (
            dict(approved_generation_axes=()),
            dict(approved_pending_identity=""),
            dict(approved_pending_effect=""),
            dict(approved_lane_generation=-1),
            dict(approved_lifecycle_revision=-1),
        ):
            with self.subTest(missing=missing):
                ops = _FakeOps()
                outcome = self._run(ops, request=_request(**missing))
                self.assertIn(DISPOSITION_APPROVAL_INCOMPLETE, outcome.detail)
                self._assert_no_mutation(ops, outcome)

    def test_non_positive_lifecycle_pin_is_typed_zero_mutation(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(
            ops, request=_request(approved_lifecycle_revision=0)
        )
        self.assertIn(DISPOSITION_LIFECYCLE_PINS_INVALID, outcome.detail)
        self._assert_no_mutation(ops, outcome)

    def test_absent_lifecycle_row_is_typed_zero_mutation(self) -> None:
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertIn(DISPOSITION_LIFECYCLE_ABSENT, outcome.detail)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(ops.heals, 0)

    def test_unreadable_lifecycle_is_typed_zero_mutation(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        with mock.patch.object(
            quarantine_module.LaneLifecycleReader,
            "get",
            side_effect=LaneLifecycleError("unreadable"),
        ):
            outcome = self._run(ops)
        self.assertIn(DISPOSITION_LIFECYCLE_UNREADABLE, outcome.detail)
        self._assert_no_mutation(ops, outcome)

    def test_a_working_agent_is_never_disposed_of(self) -> None:
        # PRECEDENCE TRAP: `generation_mismatch` outranks `agent_working` in the classifier,
        # so this receiver's LABEL is `generation_mismatch` even though its worker is
        # mid-turn — and it therefore satisfies `generation_mismatch_with_pending`. A rail
        # that inferred idleness from the label would close a pane on a running turn. Both
        # the preflight and the execute path must read the RAW agent state instead.
        self._active_lane()
        ops = _FakeOps(signal=_signal(agent_state="busy"))
        self.assertEqual(ops.inspect(_request()).classification.label, GENERATION_MISMATCH)
        outcome = self._run(ops)
        self.assertIn("live worker turn", outcome.detail)
        self._assert_no_mutation(ops, outcome)

    def test_a_working_agent_is_refused_for_every_recognised_working_state(self) -> None:
        self._active_lane()
        for state in ("busy", "Working", "  BUSY "):
            with self.subTest(state=state):
                ops = _FakeOps(signal=_signal(agent_state=state))
                outcome = self._run(ops)
                self._assert_no_mutation(ops, outcome)

    def test_a_working_agent_mints_no_disposition_approval(self) -> None:
        out = self._inspect(signal=_signal(agent_state="busy", generation_matches=True))
        self.assertEqual(out.disposition_reason, DISPOSITION_AGENT_WORKING)
        self.assertEqual(out.disposition_template, "")
        self.assertTrue(out.is_blocked)

    def test_an_unreadable_composer_mints_no_approval_and_never_assumes_empty(self) -> None:
        out = self._inspect(signal=_signal(has_pending=None))
        self.assertEqual(out.disposition_reason, DISPOSITION_COMPOSER_UNREADABLE)
        self.assertEqual(out.disposition_template, "")
        self.assertTrue(out.is_blocked)

    def test_an_absent_receiver_mints_no_approval(self) -> None:
        out = self._inspect(receiver_present=False, rows=[])
        self.assertEqual(out.disposition_reason, DISPOSITION_RECEIVER_ABSENT)
        self.assertTrue(out.is_blocked)

    def test_an_unreadable_inventory_mints_no_approval(self) -> None:
        # An inventory that could not be read proves nothing and is never read as absence.
        ops = _FakeOps()
        inspection = ops.inspect(_request())

        class _Ops:
            def inspect(self, request):
                return inspection

        def _raise():
            raise OSError("inventory unavailable")

        use_case = SublaneQuarantineInspectUseCase(
            repo_root=Path("/tmp/repo"),
            rows_reader=_raise,
            ops_factory=lambda _rows: _Ops(),
        )
        original = inspect_module.repo_scope_workspace_id
        inspect_module.repo_scope_workspace_id = lambda _root: WS
        try:
            out = use_case.run(QuarantineInspectRequest(issue=ISSUE, lane=LANE, role=ROLE))
        finally:
            inspect_module.repo_scope_workspace_id = original
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.disposition_template, "")

    def test_a_foreign_lane_owner_is_refused(self) -> None:
        self._active_lane(issue=FOREIGN_ISSUE)
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertIn("foreign", outcome.detail)
        self._assert_no_mutation(ops, outcome)


class StaleApprovalTest(_Case):
    """Exact-bind: an approval may only act on the generation it was minted for."""

    def test_a_healed_mismatch_axis_is_refused(self) -> None:
        # The owner approved a disposition over a `pair` mismatch. If the pair healed, the
        # receiver becomes an ORDINARY quarantine candidate — and that is exactly the trap:
        # the disposition tokens must still be re-verified, or an approval granted over a
        # named condition would silently execute after that condition disappeared.
        self._active_lane()
        ops = _FakeOps(signal=_signal(generation_matches=True, generation_axes=()))
        self.assertTrue(ops.inspect(_request()).classification.quarantine_candidate)
        outcome = self._run(ops)
        self.assertIn("does not match live state", outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_a_different_mismatch_axis_is_refused(self) -> None:
        self._active_lane()
        ops = _FakeOps(signal=_signal(generation_axes=("identity",)))
        outcome = self._run(ops)
        self.assertIn("does not match live state", outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_a_different_pending_input_is_never_silently_discarded(self) -> None:
        # THE acceptance criterion: the owner approved discarding the input they SAW. An
        # input that arrived since must not be destroyed under that approval.
        self._active_lane()
        ops = _FakeOps(signal=_signal(correlated_marker_ids=("m-arrived-later",)))
        outcome = self._run(ops)
        self.assertIn("does not match live state", outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_a_vanished_pending_input_is_refused(self) -> None:
        self._active_lane()
        ops = _FakeOps(signal=_signal(has_pending=False))
        outcome = self._run(ops)
        self.assertEqual(ops.closed_pins, [])

    def test_an_advanced_agent_revision_is_refused(self) -> None:
        self._active_lane()
        ops = _FakeOps(row_revision=AGENT_REVISION + 1)
        outcome = self._run(ops)
        self.assertIn("stale", outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_an_advanced_lifecycle_revision_is_refused_before_the_open_cas(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(
            ops,
            request=_request(approved_lifecycle_revision=LIFECYCLE_REVISION + 1),
        )
        self.assertIn(DRIFT_LIFECYCLE_REVISION, outcome.detail)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(self.lifecycle.get(self.key).revision, LIFECYCLE_REVISION)

    def test_old_approval_cannot_cross_a_reopened_lane_incarnation(self) -> None:
        self._active_lane()
        row = self.lifecycle.get(self.key)
        retired = self.lifecycle.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=row.revision,
            target=DISPOSITION_RETIRED,
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=JOURNAL
            ),
        )
        self.assertTrue(retired.applied)
        row = self.lifecycle.get(self.key)
        reopened = LaneDeclarationStore(home=self.home).open_next_generation(
            self.key,
            expected_revision=row.revision,
            expected_generation=row.lane_generation,
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=JOURNAL
            ),
        )
        self.assertTrue(reopened.applied)
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertIn(DRIFT_LANE_GENERATION, outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_partial_replay_rechecks_lifecycle_revision_at_the_owed_close(self) -> None:
        self._active_lane()
        opened = self.store.request_replacement(
            self.key,
            expected_revision=LIFECYCLE_REVISION,
            action_id=ACTION,
            pins=(ReleasePin(role=ROLE, assigned_name=NAME, locator=OLD_LOCATOR),),
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=APPROVAL_JOURNAL
            ),
        )
        self.assertTrue(opened.applied)
        ops = _FakeOps()
        outcome = self._run(
            ops,
            request=_request(approved_lifecycle_revision=LIFECYCLE_REVISION + 1),
        )
        self.assertIn(DRIFT_LIFECYCLE_REVISION, outcome.detail)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(
            self.lifecycle.get(self.key).replacement_state,
            REPLACEMENT_REQUESTED,
        )

    def test_a_cross_lane_action_generation_is_refused(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        foreign_action = quarantine_action_id(
            lane_id="issue_15166_failed_audit_terminal_retire",
            role=ROLE,
            locator=OLD_LOCATOR,
        )
        outcome = self._run(ops, request=_request(action_generation=foreign_action))
        self.assertIn("action generation", outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_a_cross_role_action_generation_is_refused(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(
            ops,
            request=_request(
                action_generation=quarantine_action_id(
                    lane_id=LANE, role="codex", locator=OLD_LOCATOR
                )
            ),
        )
        self.assertIn("action generation", outcome.detail)
        self.assertEqual(ops.closed_pins, [])

    def test_a_recycled_locator_is_refused(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(ops, request=_request(locator=FRESH_LOCATOR))
        self.assertEqual(ops.closed_pins, [])


if __name__ == "__main__":
    unittest.main()
