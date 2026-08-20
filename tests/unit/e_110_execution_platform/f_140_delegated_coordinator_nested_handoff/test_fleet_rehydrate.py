"""Unit tests for the governed fleet rehydrate decision (Redmine #15745).

Three isolated subjects, all with fakes and no I/O:

- the pure per-lane planner (:func:`plan_lane_rehydrate`) — its full skip / block / action
  matrix, and the ordering property that an unreadable axis blocks where it is consulted
  rather than degrading into a value a later gate reads as permission;
- the durable causal-key fold (:mod:`...domain.fleet_rehydrate_dispatch_fold`) — that a
  delivered key is never re-issued, an ``uncertain_partial`` one is never replayed, a
  non-canonical marker is not this key's evidence, and an unreadable authority is not an
  absence;
- the actuation use case (:class:`FleetRehydrateUseCase`) over a fake ops port — action-time
  identity re-join, truthful partial failure, the single composed create that covers both
  heal and dispatch, and per-lane isolation of a block.

Plus the drift guard tying the planner's fixed delegated-coordinator field set to the live
role-profile template, so adding a placeholder upstream fails here rather than silently
shipping a brief with an unresolved ``<...>`` token.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_delivery_ledger import (  # noqa: E402
    ENTRY_DISPOSITION,
    RAIL_EVENT,
    RAIL_QUEUE_ENTER,
    HerdrDeliveryLedgerRecord,
)
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    BINDING_KIND_PROJECT_GATEWAY,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    RELEASE_REQUESTED,
    REPLACEMENT_REQUESTED,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (  # noqa: E402
    REDMINE_PROJECT_FIELD,
    template_placeholders,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E402
    ACTION_HEAL_PAIR,
    ACTION_RESTORE_DISPATCH,
    ACTION_RESUME_BRIEF,
    BLOCKED,
    BLOCK_AMBIGUOUS_INVENTORY,
    BLOCK_AMBIGUOUS_OWNER,
    BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN,
    BLOCK_BRANCH_UNRESOLVED,
    BLOCK_DISPATCH_UNCERTAIN,
    BLOCK_DISPATCH_UNREADABLE,
    BLOCK_FOREIGN_SLOT,
    BLOCK_INVENTORY_UNREADABLE,
    BLOCK_ISSUE_STATE_UNKNOWN,
    BLOCK_ISSUE_UNBOUND,
    BLOCK_LANE_KIND_INVALID,
    BLOCK_LANE_MOVED,
    BLOCK_RELEASE_IN_FLIGHT,
    BLOCK_REPLACEMENT_IN_FLIGHT,
    BLOCK_RESUME_ANCHOR_UNRESOLVED,
    BLOCK_RESUME_PROFILE_INCOMPLETE,
    BLOCK_STARTUP_INTERACTION,
    BLOCK_STARTUP_SCREEN_UNVERIFIED,
    BLOCK_UNKNOWN_DISPOSITION,
    BLOCK_WORKTREE_MISSING,
    BLOCK_WORKTREE_UNBOUND,
    BLOCK_WORKTREE_UNREADABLE,
    DELEGATED_COORDINATOR_BRIEF_FIELDS,
    DISPATCH_ATTRIBUTION_UNKNOWN,
    DISPATCH_DELIVERED,
    DISPATCH_NOT_APPLICABLE,
    DISPATCH_OWED,
    DISPATCH_UNCERTAIN,
    DISPATCH_UNREADABLE,
    FleetLaneFacts,
    LaneDispatchFact,
    REHYDRATE,
    SKIP,
    SKIP_HIBERNATED,
    SKIP_IDLE,
    SKIP_ISSUE_CLOSED,
    SKIP_PROJECT_GATEWAY_BINDING,
    SKIP_RETIRED,
    SKIP_SUPERSEDED,
    STARTUP_SCREEN_BLOCKED,
    STARTUP_SCREEN_CLEAR,
    STARTUP_SCREEN_NOT_PROBED,
    STARTUP_SCREEN_UNPROFILED,
    STARTUP_SCREEN_UNREADABLE,
    plan_lane_rehydrate,
    summarize_rehydrate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate_dispatch_fold import (  # noqa: E402
    ATTRIB_CURRENT,
    ATTRIB_RETIRED,
    ATTRIB_UNKNOWN,
    KIND_IMPLEMENTATION_REQUEST,
    KIND_REPLY,
    ReceiverBinding,
    attribute_record,
    dispatch_fact,
    fold_dispatch_state,
    latest_anchor_journal,
    redmine_marker,
    select_key_records,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E402
    RebootLaneFacts,
    RebootSlotFact,
    SLOT_LIVE,
    SLOT_STALE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_fleet_rehydrate_ops import (  # noqa: E402
    FleetRehydrateUseCase,
    HealResult,
    LaneIdentityPin,
    REFUSED_DISPATCH_STATE_MOVED,
    REFUSED_GATEWAY_UNRESOLVED,
    REFUSED_HEAL_FAILED,
    REFUSED_RESUME_BRIEF_FAILED,
    STATUS_APPLIED,
    STATUS_BLOCKED,
    STATUS_SKIPPED,
)

WORKSPACE = "ws-abc"
GATEWAY_ROLE = "codex"
WORKER_ROLE = "claude"
ROLES = (GATEWAY_ROLE, WORKER_ROLE)


def slot(role, *, live=True, foreign=False, locator="w1V:pF"):
    return RebootSlotFact(
        role=role,
        assigned_name=f"mzb1_{role}",
        locator=locator,
        liveness=SLOT_LIVE if live else SLOT_STALE,
        foreign=foreign,
    )


def reboot_facts(**overrides):
    """A healthy, open, bound, live-zero lane — the post-restart baseline shape."""
    base = dict(
        workspace_id=WORKSPACE,
        lane_id="issue_1_demo",
        issue_id="1",
        worktree_identity="wt_token",
        recorded_worktree="/tmp/lane",
        worktree_present=True,
        branch="issue_1_demo",
        branch_exists=True,
        issue_closed=False,
        slots=(),
        lane_generation=2,
        revision=5,
    )
    base.update(overrides)
    return RebootLaneFacts(**base)


def fleet_facts(*, reboot=None, **overrides):
    base = dict(
        reboot=reboot if reboot is not None else reboot_facts(),
        lane_kind=LANE_KIND_IMPLEMENTATION,
        managed_roles=ROLES,
        dispatch=LaneDispatchFact(
            state=DISPATCH_OWED, anchor_issue="1", anchor_journal="900"
        ),
    )
    base.update(overrides)
    return FleetLaneFacts(**base)


def delegated_facts(**overrides):
    base = dict(
        lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        dispatch=LaneDispatchFact(
            state=DISPATCH_DELIVERED, anchor_issue="1", anchor_journal="900"
        ),
        resume_brief=LaneDispatchFact(
            state=DISPATCH_OWED, anchor_issue="1", anchor_journal="901"
        ),
        resume_profile_fields=tuple(
            (name, f"value-{name}") for name in DELEGATED_COORDINATOR_BRIEF_FIELDS
        ),
    )
    base.update(overrides)
    return fleet_facts(**base)


class PlannerScopeTests(unittest.TestCase):
    """Which rows this rail refuses to touch at all, and why each is typed."""

    def test_project_gateway_binding_is_a_typed_skip(self):
        facts = fleet_facts(
            reboot=reboot_facts(binding_kind=BINDING_KIND_PROJECT_GATEWAY)
        )
        plan = plan_lane_rehydrate(facts)
        self.assertEqual(plan.disposition, SKIP)
        self.assertEqual(plan.reason, SKIP_PROJECT_GATEWAY_BINDING)
        self.assertEqual(plan.actions, ())

    def test_terminal_and_parked_dispositions_skip_not_block(self):
        for disposition, reason in (
            (DISPOSITION_RETIRED, SKIP_RETIRED),
            (DISPOSITION_SUPERSEDED, SKIP_SUPERSEDED),
            (DISPOSITION_HIBERNATED, SKIP_HIBERNATED),
        ):
            with self.subTest(disposition=disposition):
                plan = plan_lane_rehydrate(
                    fleet_facts(reboot=reboot_facts(lane_disposition=disposition))
                )
                self.assertEqual(plan.disposition, SKIP)
                self.assertEqual(plan.reason, reason)

    def test_off_vocabulary_disposition_is_never_treated_as_active(self):
        plan = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(lane_disposition="quiesced"))
        )
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_UNKNOWN_DISPOSITION)

    def test_generation_in_flight_blocks(self):
        release = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(process_release=RELEASE_REQUESTED))
        )
        self.assertEqual(release.reason, BLOCK_RELEASE_IN_FLIGHT)
        replacement = plan_lane_rehydrate(
            fleet_facts(replacement_state=REPLACEMENT_REQUESTED)
        )
        self.assertEqual(replacement.reason, BLOCK_REPLACEMENT_IN_FLIGHT)

    def test_duplicate_active_owner_blocks_rather_than_healing_one_side(self):
        plan = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(peer_active_lanes=("issue_1_other",)))
        )
        self.assertEqual(plan.reason, BLOCK_AMBIGUOUS_OWNER)
        self.assertEqual(plan.actions, ())

    def test_off_vocabulary_lane_kind_blocks(self):
        plan = plan_lane_rehydrate(fleet_facts(lane_kind="grandchild"))
        self.assertEqual(plan.reason, BLOCK_LANE_KIND_INVALID)


class PlannerIssueAxisTests(unittest.TestCase):
    def test_unread_issue_state_blocks_and_is_never_read_as_open(self):
        plan = plan_lane_rehydrate(fleet_facts(reboot=reboot_facts(issue_closed=None)))
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_ISSUE_STATE_UNKNOWN)

    def test_closed_issue_is_a_typed_skip(self):
        plan = plan_lane_rehydrate(fleet_facts(reboot=reboot_facts(issue_closed=True)))
        self.assertEqual(plan.reason, SKIP_ISSUE_CLOSED)

    def test_issue_unbound_active_row_blocks(self):
        plan = plan_lane_rehydrate(fleet_facts(reboot=reboot_facts(issue_id="")))
        self.assertEqual(plan.reason, BLOCK_ISSUE_UNBOUND)


class PlannerCheckoutAxisTests(unittest.TestCase):
    def test_unbound_worktree_blocks(self):
        plan = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(worktree_identity=""))
        )
        self.assertEqual(plan.reason, BLOCK_WORKTREE_UNBOUND)

    def test_unreadable_worktree_is_not_a_missing_one(self):
        plan = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(worktree_present=None))
        )
        self.assertEqual(plan.reason, BLOCK_WORKTREE_UNREADABLE)

    def test_missing_worktree_blocks_and_names_no_restore_action(self):
        plan = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(worktree_present=False))
        )
        self.assertEqual(plan.reason, BLOCK_WORKTREE_MISSING)
        self.assertEqual(plan.actions, ())

    def test_moved_or_unresolved_branch_blocks(self):
        for override in ({"branch": ""}, {"branch_exists": False}, {"branch_exists": None}):
            with self.subTest(**override):
                plan = plan_lane_rehydrate(fleet_facts(reboot=reboot_facts(**override)))
                self.assertEqual(plan.reason, BLOCK_BRANCH_UNRESOLVED)


class PlannerInventoryAxisTests(unittest.TestCase):
    def test_unreadable_inventory_blocks(self):
        plan = plan_lane_rehydrate(fleet_facts(reboot=reboot_facts(slots=None)))
        self.assertEqual(plan.reason, BLOCK_INVENTORY_UNREADABLE)

    def test_unresolvable_managed_roles_block_and_never_read_as_whole(self):
        facts = fleet_facts(managed_roles=())
        self.assertFalse(facts.pair_whole)
        plan = plan_lane_rehydrate(facts)
        self.assertEqual(plan.reason, BLOCK_INVENTORY_UNREADABLE)

    def test_foreign_occupant_blocks(self):
        plan = plan_lane_rehydrate(
            fleet_facts(reboot=reboot_facts(slots=(slot("", foreign=True),)))
        )
        self.assertEqual(plan.reason, BLOCK_FOREIGN_SLOT)

    def test_duplicate_live_slot_for_a_role_blocks(self):
        plan = plan_lane_rehydrate(
            fleet_facts(
                reboot=reboot_facts(
                    slots=(
                        slot(GATEWAY_ROLE, locator="w1:p1"),
                        slot(GATEWAY_ROLE, locator="w1:p2"),
                        slot(WORKER_ROLE),
                    )
                )
            )
        )
        self.assertEqual(plan.reason, BLOCK_AMBIGUOUS_INVENTORY)

    def test_a_live_startup_screen_blocks(self):
        plan = plan_lane_rehydrate(
            fleet_facts(startup_screen=STARTUP_SCREEN_BLOCKED)
        )
        self.assertEqual(plan.reason, BLOCK_STARTUP_INTERACTION)

    def test_an_unclassifiable_startup_screen_blocks_under_its_own_token(self):
        """#13760: "could not tell" must never read as "no screen"."""
        for screen in (STARTUP_SCREEN_UNREADABLE, STARTUP_SCREEN_UNPROFILED):
            with self.subTest(screen=screen):
                plan = plan_lane_rehydrate(fleet_facts(startup_screen=screen))
                self.assertEqual(plan.disposition, BLOCKED)
                self.assertEqual(plan.reason, BLOCK_STARTUP_SCREEN_UNVERIFIED)

    def test_an_off_vocabulary_startup_token_fails_closed(self):
        plan = plan_lane_rehydrate(fleet_facts(startup_screen="probably-fine"))
        self.assertEqual(plan.reason, BLOCK_STARTUP_SCREEN_UNVERIFIED)

    def test_a_clear_or_unprobed_screen_does_not_block(self):
        for screen in (STARTUP_SCREEN_CLEAR, STARTUP_SCREEN_NOT_PROBED):
            with self.subTest(screen=screen):
                plan = plan_lane_rehydrate(fleet_facts(startup_screen=screen))
                self.assertEqual(plan.disposition, REHYDRATE)


class PlannerActionCompositionTests(unittest.TestCase):
    def test_live_zero_open_lane_heals_and_restores_its_owed_dispatch(self):
        plan = plan_lane_rehydrate(fleet_facts())
        self.assertEqual(plan.disposition, REHYDRATE)
        self.assertEqual(plan.actions, (ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH))
        self.assertEqual(plan.dispatch_anchor_journal, "900")

    def test_delivered_dispatch_is_never_replayed(self):
        plan = plan_lane_rehydrate(
            fleet_facts(
                dispatch=LaneDispatchFact(
                    state=DISPATCH_DELIVERED, anchor_issue="1", anchor_journal="900"
                )
            )
        )
        self.assertEqual(plan.actions, (ACTION_HEAL_PAIR,))
        self.assertNotIn(ACTION_RESTORE_DISPATCH, plan.actions)

    def test_uncertain_dispatch_blocks_the_whole_lane(self):
        plan = plan_lane_rehydrate(
            fleet_facts(
                dispatch=LaneDispatchFact(
                    state=DISPATCH_UNCERTAIN, anchor_issue="1", anchor_journal="900"
                )
            )
        )
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_DISPATCH_UNCERTAIN)
        self.assertEqual(plan.actions, ())

    def test_unreadable_delivery_record_blocks_rather_than_assuming_undelivered(self):
        plan = plan_lane_rehydrate(
            fleet_facts(dispatch=LaneDispatchFact(state=DISPATCH_UNREADABLE))
        )
        self.assertEqual(plan.reason, BLOCK_DISPATCH_UNREADABLE)

    def test_intact_pair_with_everything_delivered_is_a_typed_idle_skip(self):
        plan = plan_lane_rehydrate(
            fleet_facts(
                reboot=reboot_facts(slots=(slot(GATEWAY_ROLE), slot(WORKER_ROLE))),
                dispatch=LaneDispatchFact(
                    state=DISPATCH_DELIVERED, anchor_issue="1", anchor_journal="900"
                ),
            )
        )
        self.assertEqual(plan.disposition, SKIP)
        self.assertEqual(plan.reason, SKIP_IDLE)
        self.assertTrue(plan.pair_whole)

    def test_owed_dispatch_without_an_anchor_is_not_planned(self):
        plan = plan_lane_rehydrate(
            fleet_facts(
                reboot=reboot_facts(slots=(slot(GATEWAY_ROLE), slot(WORKER_ROLE))),
                dispatch=LaneDispatchFact(state=DISPATCH_OWED, anchor_journal=""),
            )
        )
        self.assertEqual(plan.disposition, SKIP)
        self.assertEqual(plan.reason, SKIP_IDLE)


class PlannerResumeBriefTests(unittest.TestCase):
    def test_delegated_coordinator_relaunch_carries_a_resume_brief(self):
        plan = plan_lane_rehydrate(delegated_facts())
        self.assertEqual(plan.disposition, REHYDRATE)
        self.assertEqual(plan.actions, (ACTION_HEAL_PAIR, ACTION_RESUME_BRIEF))
        self.assertEqual(plan.resume_anchor_journal, "901")

    def test_implementation_lane_never_gets_a_brief(self):
        plan = plan_lane_rehydrate(
            fleet_facts(
                resume_brief=LaneDispatchFact(
                    state=DISPATCH_OWED, anchor_issue="1", anchor_journal="901"
                )
            )
        )
        self.assertNotIn(ACTION_RESUME_BRIEF, plan.actions)

    def test_incomplete_role_profile_blocks_rather_than_half_briefing(self):
        partial = tuple(
            (name, "" if name == "child_project" else "v")
            for name in DELEGATED_COORDINATOR_BRIEF_FIELDS
        )
        plan = plan_lane_rehydrate(delegated_facts(resume_profile_fields=partial))
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_RESUME_PROFILE_INCOMPLETE)
        self.assertIn("child_project", plan.detail)
        self.assertEqual(plan.actions, ())

    def test_relaunch_with_an_already_delivered_anchor_blocks(self):
        """A cold-restarted L2 with no fresh anchor would wake up with no instructions."""
        plan = plan_lane_rehydrate(
            delegated_facts(
                resume_brief=LaneDispatchFact(
                    state=DISPATCH_DELIVERED, anchor_issue="1", anchor_journal="901"
                )
            )
        )
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_RESUME_ANCHOR_UNRESOLVED)

    def test_delegated_lane_with_no_resume_anchor_blocks(self):
        plan = plan_lane_rehydrate(
            delegated_facts(resume_brief=LaneDispatchFact(state=DISPATCH_NOT_APPLICABLE))
        )
        self.assertEqual(plan.reason, BLOCK_RESUME_ANCHOR_UNRESOLVED)

    def test_whole_delegated_pair_with_a_fresh_anchor_briefs_without_healing(self):
        plan = plan_lane_rehydrate(
            delegated_facts(
                reboot=reboot_facts(slots=(slot(GATEWAY_ROLE), slot(WORKER_ROLE)))
            )
        )
        self.assertEqual(plan.actions, (ACTION_RESUME_BRIEF,))


class SummaryTests(unittest.TestCase):
    def test_rollup_is_counts_only(self):
        plans = [
            plan_lane_rehydrate(fleet_facts()),
            plan_lane_rehydrate(fleet_facts(reboot=reboot_facts(issue_closed=True))),
        ]
        summary = summarize_rehydrate(plans)
        self.assertEqual(summary["lane_count"], 2)
        self.assertEqual(summary["actionable"], 1)
        self.assertEqual(summary["dispositions"], {REHYDRATE: 1, SKIP: 1})
        self.assertEqual(
            summary["actions"], {ACTION_HEAL_PAIR: 1, ACTION_RESTORE_DISPATCH: 1}
        )
        self.assertEqual(summary["reasons"], {SKIP_ISSUE_CLOSED: 1})


# ---------------------------------------------------------------------------
# The durable causal-key fold.
# ---------------------------------------------------------------------------


#: The receiver a lane would send to now, in the fold tests.
LIVE_BINDING = ReceiverBinding(
    role=GATEWAY_ROLE,
    assigned_name="mzb1_gw",
    locator="w1V:pF",
    revision="7",
)


def _recorded_binding(*, assigned_name="mzb1_gw", revision="7"):
    """The queue-enter rail's own gateway_binding, as the ledger stores it."""
    return {"gateway_binding": {"assigned_name": assigned_name, "row_revision": revision}}


def ledger_record(marker, *, status="sent", reason="ok", rail=RAIL_EVENT, **kw):
    base = dict(
        entry_id=kw.pop("entry_id", 1),
        notification_marker=marker,
        receiver=kw.pop("receiver", GATEWAY_ROLE),
        source=kw.pop("source", "redmine"),
        issue_id=kw.pop("issue_id", "1"),
        journal_id=kw.pop("journal_id", "900"),
        status=status,
        reason=reason,
        rail=rail,
        target=kw.pop("target", LIVE_BINDING.locator),
        turn_start_outcome=kw.pop("turn_start_outcome", None),
        queue_enter_observation=kw.pop("queue_enter_observation", _recorded_binding()),
    )
    base.update(kw)
    return HerdrDeliveryLedgerRecord(**base)


IR_MARKER = redmine_marker("1", "900", KIND_IMPLEMENTATION_REQUEST, GATEWAY_ROLE)


class DispatchFoldTests(unittest.TestCase):
    def test_no_recorded_attempt_is_owed(self):
        self.assertEqual(
            fold_dispatch_state(
                (),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=LIVE_BINDING,
            ),
            DISPATCH_OWED,
        )

    def test_confirmed_submission_is_delivered(self):
        record = ledger_record(IR_MARKER, status="sent", reason="ok", rail=RAIL_EVENT)
        self.assertEqual(
            fold_dispatch_state(
                (record,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=LIVE_BINDING,
            ),
            DISPATCH_DELIVERED,
        )

    def test_queue_enter_ok_without_turn_start_evidence_is_uncertain(self):
        """The rail carve-out: `ok` on queue-enter is not proof of submission."""
        record = ledger_record(
            IR_MARKER, status="sent", reason="ok", rail=RAIL_QUEUE_ENTER
        )
        self.assertEqual(
            fold_dispatch_state(
                (record,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=LIVE_BINDING,
            ),
            DISPATCH_UNCERTAIN,
        )

    def test_pre_injection_refusal_stays_owed(self):
        record = ledger_record(
            IR_MARKER, status="blocked", reason="target_unavailable", rail=RAIL_EVENT
        )
        self.assertEqual(
            fold_dispatch_state(
                (record,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=LIVE_BINDING,
            ),
            DISPATCH_OWED,
        )

    def test_one_confirmed_attempt_wins_over_later_refusals(self):
        records = (
            ledger_record(IR_MARKER, entry_id=1, status="sent", reason="ok"),
            ledger_record(
                IR_MARKER, entry_id=2, status="blocked", reason="target_unavailable"
            ),
        )
        self.assertEqual(
            fold_dispatch_state(
                records,
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=LIVE_BINDING,
            ),
            DISPATCH_DELIVERED,
        )

    def test_a_non_canonical_marker_is_not_this_keys_evidence(self):
        """A stored marker the canonical producer would not render is dropped, not trusted."""
        record = ledger_record(IR_MARKER, journal_id="999")
        self.assertEqual(
            select_key_records(
                (record,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
            ),
            (),
        )

    def test_a_different_receiver_is_not_this_keys_evidence(self):
        record = ledger_record(IR_MARKER, receiver=WORKER_ROLE)
        self.assertEqual(
            select_key_records(
                (record,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
            ),
            (),
        )

    def test_a_retry_or_disposition_entry_is_not_a_second_attempt(self):
        record = ledger_record(IR_MARKER, entry_kind=ENTRY_DISPOSITION)
        self.assertEqual(
            select_key_records(
                (record,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
            ),
            (),
        )

    def test_unreadable_authority_is_not_an_absence(self):
        fact = dispatch_fact(
            None,
            issue="1",
            journal="900",
            kind=KIND_IMPLEMENTATION_REQUEST,
            receiver=GATEWAY_ROLE,
        )
        self.assertEqual(fact.state, DISPATCH_UNREADABLE)
        self.assertFalse(fact.sendable)

    def test_absent_anchor_is_not_applicable_rather_than_owed(self):
        fact = dispatch_fact(
            (),
            issue="1",
            journal="",
            kind=KIND_REPLY,
            receiver=GATEWAY_ROLE,
            binding=LIVE_BINDING,
        )
        self.assertEqual(fact.state, DISPATCH_NOT_APPLICABLE)
        self.assertFalse(fact.sendable)

    def test_latest_anchor_binds_to_what_was_actually_dispatched(self):
        records = (
            ledger_record(
                redmine_marker("1", "800", KIND_IMPLEMENTATION_REQUEST, GATEWAY_ROLE),
                entry_id=1,
                journal_id="800",
            ),
            ledger_record(IR_MARKER, entry_id=7, journal_id="900"),
        )
        self.assertEqual(
            latest_anchor_journal(
                records,
                issue="1",
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
            ),
            "900",
        )

    def test_latest_anchor_is_empty_when_nothing_canonical_was_recorded(self):
        self.assertEqual(
            latest_anchor_journal(
                (), issue="1", kind=KIND_IMPLEMENTATION_REQUEST, receiver=GATEWAY_ROLE
            ),
            "",
        )


class ReceiverAttributionTests(unittest.TestCase):
    """Review j#108920 ``finding_generationfence``: evidence is bound to TODAY's receiver.

    The marker carries no lane and no generation and the ledger has no generation column, so
    without this join an old generation's confirmed delivery answered for a fresh one and the
    relaunched pair never got its pointer.
    """

    def test_a_record_for_the_live_receiver_is_current_evidence(self):
        record = ledger_record(IR_MARKER)
        self.assertEqual(attribute_record(record, LIVE_BINDING), ATTRIB_CURRENT)

    def test_a_record_with_no_live_receiver_at_all_is_retired(self):
        """The post-restart main case: every recorded attempt targeted a dead process."""
        record = ledger_record(IR_MARKER)
        self.assertEqual(
            attribute_record(record, ReceiverBinding.absent()), ATTRIB_RETIRED
        )

    def test_a_record_for_another_locator_is_retired(self):
        record = ledger_record(IR_MARKER, target="w9Z:pZ")
        self.assertEqual(attribute_record(record, LIVE_BINDING), ATTRIB_RETIRED)

    def test_a_relaunch_under_the_same_locator_is_retired_not_current(self):
        """The ABA hole: same pane id, different process. The row revision closes it."""
        record = ledger_record(
            IR_MARKER, queue_enter_observation=_recorded_binding(revision="6")
        )
        self.assertEqual(attribute_record(record, LIVE_BINDING), ATTRIB_RETIRED)

    def test_a_live_locator_with_no_recorded_binding_is_unknown(self):
        """A bare locator cannot tell the same process from a recycled one."""
        record = ledger_record(IR_MARKER, queue_enter_observation=None)
        self.assertEqual(attribute_record(record, LIVE_BINDING), ATTRIB_UNKNOWN)

    def test_a_record_without_a_target_is_unknown(self):
        record = ledger_record(IR_MARKER, target="")
        self.assertEqual(attribute_record(record, LIVE_BINDING), ATTRIB_UNKNOWN)

    def test_an_old_generations_confirmed_delivery_does_not_answer_for_a_fresh_pair(self):
        """The exact defect: generation 1 delivered, generation 2 relaunched elsewhere."""
        old = ledger_record(IR_MARKER, target="w1V:pOLD", status="sent", reason="ok")
        self.assertEqual(
            fold_dispatch_state(
                (old,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=ReceiverBinding(
                    role=GATEWAY_ROLE,
                    assigned_name="mzb1_gw",
                    locator="w1V:pNEW",
                    revision="9",
                ),
            ),
            DISPATCH_OWED,
            "a delivery to a receiver that is gone must not suppress the fresh dispatch",
        )

    def test_a_live_zero_lane_owes_its_dispatch_again_after_a_restart(self):
        confirmed = ledger_record(IR_MARKER, status="sent", reason="ok")
        self.assertEqual(
            fold_dispatch_state(
                (confirmed,),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=None,
            ),
            DISPATCH_OWED,
        )

    def test_an_unattributable_record_blocks_rather_than_choosing_either_answer(self):
        record = ledger_record(IR_MARKER, queue_enter_observation=None)
        state = fold_dispatch_state(
            (record,),
            marker=IR_MARKER,
            kind=KIND_IMPLEMENTATION_REQUEST,
            receiver=GATEWAY_ROLE,
            binding=LIVE_BINDING,
        )
        self.assertEqual(state, DISPATCH_ATTRIBUTION_UNKNOWN)
        plan = plan_lane_rehydrate(
            fleet_facts(
                dispatch=LaneDispatchFact(
                    state=DISPATCH_ATTRIBUTION_UNKNOWN,
                    anchor_issue="1",
                    anchor_journal="900",
                )
            )
        )
        self.assertEqual(plan.disposition, BLOCKED)
        self.assertEqual(plan.reason, BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN)
        self.assertEqual(plan.actions, ())

    def test_unknown_wins_over_a_current_confirmation(self):
        """One unplaceable attempt taints the key: fail-closed, not majority vote."""
        confirmed = ledger_record(IR_MARKER, entry_id=1)
        opaque = ledger_record(IR_MARKER, entry_id=2, queue_enter_observation=None)
        self.assertEqual(
            fold_dispatch_state(
                (confirmed, opaque),
                marker=IR_MARKER,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=GATEWAY_ROLE,
                binding=LIVE_BINDING,
            ),
            DISPATCH_ATTRIBUTION_UNKNOWN,
        )


class ActionTimeRefoldTests(unittest.TestCase):
    """Review j#108920 ``finding_actiontimefence``: re-fold immediately before each send."""

    def test_the_dispatch_key_is_refolded_before_the_composed_create(self):
        facts = fleet_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=pin_for(facts))
        FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertIn(
            (facts.lane_id, KIND_IMPLEMENTATION_REQUEST),
            ops.refold_calls,
            "the plan's fold is an observation; the send needs a fresh one",
        )

    def test_a_key_that_landed_during_the_window_is_not_sent(self):
        facts = fleet_facts(
            reboot=reboot_facts(slots=(slot(GATEWAY_ROLE), slot(WORKER_ROLE)))
        )
        plan = plan_lane_rehydrate(facts)
        self.assertEqual(plan.actions, (ACTION_RESTORE_DISPATCH,))
        ops = FakeOps(
            identity=pin_for(facts),
            fresh_states={KIND_IMPLEMENTATION_REQUEST: DISPATCH_DELIVERED},
        )
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(ops.heal_calls, [], "zero additional effect")
        self.assertEqual(outcomes[0]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[0]["reason"], REFUSED_DISPATCH_STATE_MOVED)
        self.assertEqual(outcomes[0]["applied"], [])

    def test_a_landed_dispatch_still_lets_a_needed_heal_proceed_without_sending(self):
        """Healing is additive; only the send is dropped."""
        facts = fleet_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(
            identity=pin_for(facts),
            fresh_states={KIND_IMPLEMENTATION_REQUEST: DISPATCH_DELIVERED},
        )
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(ops.heal_calls, [(facts.lane_id, False)])
        self.assertEqual(outcomes[0]["status"], STATUS_APPLIED)
        self.assertEqual(outcomes[0]["applied"], [ACTION_HEAL_PAIR])

    def test_the_brief_is_refolded_and_reidentified_after_the_heal(self):
        facts = delegated_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=pin_for(facts))
        FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertIn((facts.lane_id, KIND_REPLY), ops.refold_calls)
        self.assertEqual(
            len(ops.identity_calls),
            2,
            "once before the first effect, and again immediately before the brief send",
        )

    def test_a_brief_that_landed_during_the_heal_is_not_re_sent(self):
        facts = delegated_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(
            identity=pin_for(facts), fresh_states={KIND_REPLY: DISPATCH_DELIVERED}
        )
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(ops.brief_calls, [])
        self.assertEqual(outcomes[0]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[0]["reason"], REFUSED_DISPATCH_STATE_MOVED)
        self.assertEqual(
            outcomes[0]["applied"],
            [ACTION_HEAL_PAIR],
            "what already landed is reported truthfully",
        )

    def test_an_unreadable_authority_at_send_time_blocks_the_brief(self):
        facts = delegated_facts()
        plan = plan_lane_rehydrate(facts)
        for state in (DISPATCH_UNREADABLE, DISPATCH_ATTRIBUTION_UNKNOWN):
            with self.subTest(state=state):
                ops = FakeOps(
                    identity=pin_for(facts), fresh_states={KIND_REPLY: state}
                )
                outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
                self.assertEqual(ops.brief_calls, [])
                self.assertEqual(outcomes[0]["reason"], REFUSED_DISPATCH_STATE_MOVED)

    def test_a_lane_that_moved_during_the_heal_blocks_the_brief(self):
        facts = delegated_facts()
        plan = plan_lane_rehydrate(facts)
        pin = pin_for(facts)
        moved = LaneIdentityPin(
            disposition=pin.disposition,
            revision=pin.revision,
            lane_generation=pin.lane_generation + 1,
            worktree_identity=pin.worktree_identity,
            issue_id=pin.issue_id,
        )

        class Moving(FakeOps):
            def __init__(self):
                super().__init__(identity=pin)
                self._seen = 0

            def current_identity(self, *, workspace_id, lane_id):
                self._seen += 1
                self.identity_calls.append((workspace_id, lane_id))
                # Healthy for the first effect, moved by the time the brief would send.
                return pin if self._seen == 1 else moved

        ops = Moving()
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(ops.heal_calls, [(facts.lane_id, False)])
        self.assertEqual(ops.brief_calls, [])
        self.assertEqual(outcomes[0]["reason"], BLOCK_LANE_MOVED)
        self.assertEqual(outcomes[0]["applied"], [ACTION_HEAL_PAIR])


class RoleProfileDriftGuardTests(unittest.TestCase):
    def test_brief_field_set_matches_the_live_template(self):
        """Adding a placeholder upstream must fail HERE, not ship an unresolved token."""
        placeholders = set(template_placeholders(LANE_KIND_DELEGATED_COORDINATOR))
        carried = set(DELEGATED_COORDINATOR_BRIEF_FIELDS)
        self.assertEqual(
            carried | {REDMINE_PROJECT_FIELD},
            placeholders,
            "the rail must carry every delegated_coordinator placeholder except "
            "redmine_project (auto-filled from the verified workspace default)",
        )
        self.assertNotIn(REDMINE_PROJECT_FIELD, carried)


# ---------------------------------------------------------------------------
# The actuation use case (fake ops).
# ---------------------------------------------------------------------------


class FakeOps:
    def __init__(self, *, identity=None, heal=None, brief_code=0, fresh_states=None):
        self._identity = identity
        self._heal = heal or (lambda facts, dispatch: HealResult(ok=True, gateway_target="w1:pF"))
        self._brief_code = brief_code
        #: ``{kind: state}`` the action-time re-fold reports. Default: still owed.
        self._fresh_states = dict(fresh_states or {})
        self.heal_calls = []
        self.brief_calls = []
        self.identity_calls = []
        self.refold_calls = []

    def current_identity(self, *, workspace_id, lane_id):
        self.identity_calls.append((workspace_id, lane_id))
        return self._identity

    def heal_lane(self, facts, *, dispatch):
        self.heal_calls.append((facts.lane_id, dispatch))
        return self._heal(facts, dispatch)

    def current_dispatch_state(self, facts, *, kind):
        self.refold_calls.append((facts.lane_id, kind))
        return self._fresh_states.get(kind, DISPATCH_OWED)

    def send_resume_brief(self, facts, *, gateway_target):
        self.brief_calls.append((facts.lane_id, gateway_target))
        return self._brief_code


def pin_for(facts):
    return LaneIdentityPin.from_facts(facts)


class UseCaseTests(unittest.TestCase):
    def test_a_non_actionable_plan_attempts_nothing(self):
        facts = fleet_facts(reboot=reboot_facts(issue_closed=True))
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=pin_for(facts))
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(outcomes[0]["status"], STATUS_SKIPPED)
        self.assertEqual(ops.heal_calls, [])
        self.assertEqual(ops.brief_calls, [])
        self.assertEqual(ops.identity_calls, [])

    def test_heal_and_restore_are_one_composed_create(self):
        facts = fleet_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=pin_for(facts))
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(ops.heal_calls, [(facts.lane_id, True)])
        self.assertEqual(outcomes[0]["status"], STATUS_APPLIED)
        self.assertEqual(
            outcomes[0]["applied"], [ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH]
        )

    def test_heal_only_does_not_dispatch(self):
        facts = fleet_facts(
            dispatch=LaneDispatchFact(
                state=DISPATCH_DELIVERED, anchor_issue="1", anchor_journal="900"
            )
        )
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=pin_for(facts))
        FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(ops.heal_calls, [(facts.lane_id, False)])

    def test_identity_moved_between_plan_and_effect_refuses_zero_effect(self):
        facts = fleet_facts()
        plan = plan_lane_rehydrate(facts)
        moved = LaneIdentityPin.from_facts(facts)
        ops = FakeOps(
            identity=LaneIdentityPin(
                disposition=moved.disposition,
                revision=moved.revision + 1,
                lane_generation=moved.lane_generation,
                worktree_identity=moved.worktree_identity,
                issue_id=moved.issue_id,
            )
        )
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(outcomes[0]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[0]["reason"], BLOCK_LANE_MOVED)
        self.assertEqual(outcomes[0]["applied"], [])
        self.assertEqual(ops.heal_calls, [])

    def test_unreadable_identity_refuses(self):
        facts = fleet_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=None)
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(outcomes[0]["reason"], BLOCK_LANE_MOVED)
        self.assertEqual(ops.heal_calls, [])

    def test_failed_heal_does_not_cascade_into_a_brief(self):
        facts = delegated_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(
            identity=pin_for(facts),
            heal=lambda f, dispatch: HealResult(ok=False, reason=REFUSED_HEAL_FAILED),
        )
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(outcomes[0]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[0]["reason"], REFUSED_HEAL_FAILED)
        self.assertEqual(outcomes[0]["applied"], [])
        self.assertEqual(ops.brief_calls, [])

    def test_partial_success_reports_what_landed(self):
        facts = delegated_facts()
        plan = plan_lane_rehydrate(facts)
        ops = FakeOps(identity=pin_for(facts), brief_code=1)
        outcomes = FleetRehydrateUseCase(ops).run([facts], [plan])
        self.assertEqual(outcomes[0]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[0]["reason"], REFUSED_RESUME_BRIEF_FAILED)
        self.assertEqual(outcomes[0]["applied"], [ACTION_HEAL_PAIR])
        self.assertEqual(
            outcomes[0]["attempted"], [ACTION_HEAL_PAIR, ACTION_RESUME_BRIEF]
        )

    def test_brief_without_a_resolvable_gateway_sends_nothing(self):
        """A whole pair means no heal runs, so an unresolvable gateway leaves no target."""
        facts = delegated_facts(
            reboot=reboot_facts(
                # `is_live_agent` requires a locator, so a locator-less pair reads as
                # live-zero for role resolution while still occupying the unit.
                slots=(
                    slot(GATEWAY_ROLE, locator="w1:pF"),
                    slot(WORKER_ROLE, locator="w1:pG"),
                )
            )
        )
        plan = plan_lane_rehydrate(facts)
        self.assertEqual(plan.actions, (ACTION_RESUME_BRIEF,))
        blind = delegated_facts(
            reboot=reboot_facts(
                slots=(slot(GATEWAY_ROLE), slot(WORKER_ROLE)),
            ),
            managed_roles=(),
        )
        ops = FakeOps(identity=pin_for(blind))
        outcomes = FleetRehydrateUseCase(ops).run([blind], [plan])
        self.assertEqual(outcomes[0]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[0]["reason"], REFUSED_GATEWAY_UNRESOLVED)
        self.assertEqual(ops.brief_calls, [])

    def test_one_lanes_block_does_not_stop_the_fleet(self):
        healthy = fleet_facts()
        broken = fleet_facts(
            reboot=reboot_facts(lane_id="issue_2_demo", issue_id="2"),
            dispatch=LaneDispatchFact(
                state=DISPATCH_UNCERTAIN, anchor_issue="2", anchor_journal="901"
            ),
        )
        plans = [plan_lane_rehydrate(healthy), plan_lane_rehydrate(broken)]
        ops = FakeOps(identity=pin_for(healthy))
        outcomes = FleetRehydrateUseCase(ops).run([healthy, broken], plans)
        self.assertEqual(outcomes[0]["status"], STATUS_APPLIED)
        # A plan-level block stays BLOCKED in the outcome (it is not "deliberately out of
        # scope"), and it attempts nothing — but it does not stop the healthy lane.
        self.assertEqual(outcomes[1]["status"], STATUS_BLOCKED)
        self.assertEqual(outcomes[1]["reason"], BLOCK_DISPATCH_UNCERTAIN)
        self.assertEqual(outcomes[1]["attempted"], [])
        self.assertEqual(ops.heal_calls, [(healthy.lane_id, True)])

    def test_a_deliberate_scope_skip_is_not_a_block(self):
        facts = fleet_facts(reboot=reboot_facts(issue_closed=True))
        outcomes = FleetRehydrateUseCase(FakeOps()).run(
            [facts], [plan_lane_rehydrate(facts)]
        )
        self.assertEqual(outcomes[0]["status"], STATUS_SKIPPED)
        self.assertEqual(outcomes[0]["reason"], SKIP_ISSUE_CLOSED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
