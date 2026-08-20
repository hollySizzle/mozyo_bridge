"""Operator-view acceptance for the post-restart fleet rehydrate rail (Redmine #15745).

The restart scenario end to end, at the surface a coordinator actually drives
(``mozyo-bridge sublane rehydrate-fleet``): a mixed fleet of lanes in different post-restart
states goes through the read-only plan, then ``--execute``, and this suite pins what the
operator sees and what the exit code says.

Context: ``e_110_execution_platform`` / ``f_140_delegated_coordinator_nested_handoff``,
crossing the fact join, the pure planner, the actuation use case and the CLI handler. The
composed effects (``sublane create`` / ``handoff send``) are behind the injected ops port,
so nothing here launches a process, sends a notification or touches a worktree.

Scenarios pinned (the issue's acceptance 1 / 3 / 5 / 8):

- the mixed fleet plan names every lane with its typed disposition and owed actions;
- the plan stage constructs no live actuator at all — its effect budget is structurally zero,
  not merely unobserved;
- an active implementation lane heals and restores its owed dispatch in one composed action;
- a delegated-coordinator lane's resume brief rides its fresh anchor;
- an already-delivered dispatch is not replayed on the second pass (idempotent resume);
- closed / idle lanes are typed skips, and ``--lane-label`` filters as a typed skip rather
  than by dropping lanes from the report;
- a partial failure is truthful and exits non-zero, while a plan-only run of the same fleet
  exits zero (a finding is not a command failure);
- an unreadable authority is ``unavailable`` at exit 1, never an empty success;
- neither the text nor the JSON output carries a host-local worktree path.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402
    sublane_fleet_rehydrate as rehydrate,
    sublane_fleet_rehydrate_ops as ops_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E501
    ACTION_HEAL_PAIR,
    ACTION_RESTORE_DISPATCH,
    ACTION_RESUME_BRIEF,
    BLOCKED,
    DELEGATED_COORDINATOR_BRIEF_FIELDS,
    DISPATCH_DELIVERED,
    DISPATCH_OWED,
    DISPATCH_UNCERTAIN,
    FleetLaneFacts,
    LaneDispatchFact,
    REHYDRATE,
    SKIP,
    SKIP_FILTERED,
    SKIP_IDLE,
    SKIP_ISSUE_CLOSED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
    RebootLaneFacts,
    RebootSlotFact,
    SLOT_LIVE,
)

WORKSPACE = "ws-fleet"
GATEWAY = "codex"
WORKER = "claude"
ROLES = (GATEWAY, WORKER)
PRIVATE_WORKTREE = "/Users/someone/private/.worktrees/lane"


def _slot(role, locator="w1V:pF"):
    return RebootSlotFact(
        role=role,
        assigned_name=f"mzb1_{role}",
        locator=locator,
        liveness=SLOT_LIVE,
    )


def _lane(
    lane_id,
    issue,
    *,
    lane_kind=LANE_KIND_IMPLEMENTATION,
    live_pair=False,
    issue_closed=False,
    dispatch_state=DISPATCH_OWED,
    brief_state=None,
    brief_journal="",
):
    reboot = RebootLaneFacts(
        workspace_id=WORKSPACE,
        lane_id=lane_id,
        issue_id=issue,
        worktree_identity=f"wt_{lane_id}",
        recorded_worktree=PRIVATE_WORKTREE,
        worktree_present=True,
        branch=lane_id,
        branch_exists=True,
        issue_closed=issue_closed,
        slots=(_slot(GATEWAY), _slot(WORKER, "w1V:pG")) if live_pair else (),
        lane_generation=1,
        revision=3,
    )
    brief = LaneDispatchFact()
    profile = ()
    if lane_kind == LANE_KIND_DELEGATED_COORDINATOR:
        brief = LaneDispatchFact(
            state=brief_state or DISPATCH_OWED,
            anchor_issue=issue,
            anchor_journal=brief_journal or "9001",
        )
        profile = tuple((n, f"v-{n}") for n in DELEGATED_COORDINATOR_BRIEF_FIELDS)
    return FleetLaneFacts(
        reboot=reboot,
        lane_kind=lane_kind,
        managed_roles=ROLES,
        dispatch=LaneDispatchFact(
            state=dispatch_state, anchor_issue=issue, anchor_journal="9000"
        ),
        resume_brief=brief,
        resume_profile_fields=profile,
    )


def mixed_fleet():
    """One lane per post-restart shape a real fleet actually presents."""
    return (
        # L3 implementation lane: pair gone, dispatch never delivered.
        _lane("issue_101_impl", "101"),
        # L2 delegated coordinator: pair gone, IR delivered, fresh brief anchor recorded.
        _lane(
            "issue_102_l2",
            "102",
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
            dispatch_state=DISPATCH_DELIVERED,
        ),
        # Idle: pair intact and everything delivered.
        _lane(
            "issue_103_idle",
            "103",
            live_pair=True,
            dispatch_state=DISPATCH_DELIVERED,
        ),
        # Closed issue: converged by the retire rails, never rehydrated.
        _lane("issue_104_closed", "104", issue_closed=True),
        # A send whose fate is unknown: never replayed.
        _lane("issue_105_uncertain", "105", dispatch_state=DISPATCH_UNCERTAIN),
    )


class FakeOps:
    """Stands in for :class:`LiveFleetRehydrateOps` at the composed-command boundary."""

    instances: list["FakeOps"] = []

    #: Class-level override for the action-time re-fold, ``{kind: state}``.
    fresh_states: dict = {}

    def __init__(self, *, repo_root=None, quiet_stdout=False, fail_brief=False):
        self.repo_root = repo_root
        self.quiet_stdout = quiet_stdout
        self.fail_brief = fail_brief
        self.heal_calls = []
        self.brief_calls = []
        self.refold_calls = []
        FakeOps.instances.append(self)

    def current_identity(self, *, workspace_id, lane_id):
        return ops_module.LaneIdentityPin(
            disposition="active",
            revision=3,
            lane_generation=1,
            worktree_identity=f"wt_{lane_id}",
            issue_id=lane_id.split("_")[1],
        )

    def heal_lane(self, facts, *, dispatch):
        self.heal_calls.append((facts.lane_id, dispatch))
        return ops_module.HealResult(ok=True, gateway_target="w1V:pF")

    def current_dispatch_state(self, facts, *, kind):
        """The action-time re-fold; by default the key is still owed."""
        self.refold_calls.append((facts.lane_id, kind))
        return FakeOps.fresh_states.get(kind, DISPATCH_OWED)

    def send_resume_brief(self, facts, *, gateway_target):
        self.brief_calls.append((facts.lane_id, gateway_target))
        return 1 if self.fail_brief else 0


def run_cli(
    facts,
    *,
    execute=False,
    lane_label="",
    json_mode=True,
    fail_brief=False,
    fresh_states=None,
):
    """Drive the real CLI handler over an injected fleet, capturing stdout."""
    FakeOps.instances = []
    FakeOps.fresh_states = dict(fresh_states or {})
    args = argparse.Namespace(
        repo=".",
        json=json_mode,
        execute=execute,
        lane_label=lane_label,
        integration_branch="",
        resume_anchor=None,
        resume_profile_field=None,
    )

    def _ops(**kwargs):
        return FakeOps(fail_brief=fail_brief, **kwargs)

    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(
        rehydrate, "gather_fleet_facts", return_value=facts
    ), mock.patch.object(ops_module, "LiveFleetRehydrateOps", _ops):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rehydrate.cmd_sublane_rehydrate_fleet(args)
    payload = json.loads(out.getvalue()) if json_mode and out.getvalue() else None
    return rc, payload, out.getvalue() + err.getvalue()


def by_lane(payload):
    return {row["plan"]["lane_id"]: row for row in payload["lanes"]}


class PlanStageAcceptance(unittest.TestCase):
    def test_the_mixed_fleet_plan_names_every_lane_and_its_owed_actions(self):
        rc, payload, _ = run_cli(mixed_fleet())
        self.assertEqual(rc, 0, "a lane needing work is a finding, not a command failure")
        self.assertEqual(payload["state"], "plan")
        self.assertFalse(payload["execute"])
        rows = by_lane(payload)
        self.assertEqual(len(rows), 5)

        impl = rows["issue_101_impl"]["plan"]
        self.assertEqual(impl["disposition"], REHYDRATE)
        self.assertEqual(impl["actions"], [ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH])

        l2 = rows["issue_102_l2"]["plan"]
        self.assertEqual(l2["actions"], [ACTION_HEAL_PAIR, ACTION_RESUME_BRIEF])
        self.assertEqual(l2["resume_anchor_journal"], "9001")

        self.assertEqual(rows["issue_103_idle"]["plan"]["reason"], SKIP_IDLE)
        self.assertEqual(rows["issue_104_closed"]["plan"]["reason"], SKIP_ISSUE_CLOSED)
        self.assertEqual(rows["issue_105_uncertain"]["plan"]["disposition"], BLOCKED)
        self.assertEqual(rows["issue_105_uncertain"]["plan"]["actions"], [])

    def test_the_plan_stage_constructs_no_live_actuator(self):
        """The effect budget is structural: the actuator is never even built."""
        run_cli(mixed_fleet())
        self.assertEqual(FakeOps.instances, [])

    def test_the_summary_is_counts_only(self):
        _, payload, _ = run_cli(mixed_fleet())
        summary = payload["summary"]
        self.assertEqual(summary["lane_count"], 5)
        self.assertEqual(summary["actionable"], 2)
        self.assertEqual(summary["dispositions"], {REHYDRATE: 2, SKIP: 2, BLOCKED: 1})
        self.assertEqual(
            summary["actions"],
            {ACTION_HEAL_PAIR: 2, ACTION_RESTORE_DISPATCH: 1, ACTION_RESUME_BRIEF: 1},
        )

    def test_a_lane_filter_is_a_typed_skip_not_a_dropped_lane(self):
        _, payload, _ = run_cli(mixed_fleet(), lane_label="issue_101_impl")
        rows = by_lane(payload)
        self.assertEqual(len(rows), 5, "the reported lane set is always the manifest's")
        self.assertEqual(rows["issue_101_impl"]["plan"]["disposition"], REHYDRATE)
        for other in ("issue_102_l2", "issue_103_idle", "issue_104_closed"):
            self.assertEqual(rows[other]["plan"]["reason"], SKIP_FILTERED)
            self.assertEqual(rows[other]["plan"]["actions"], [])

    def test_the_text_output_states_it_touched_nothing(self):
        rc, _, text = run_cli(mixed_fleet(), json_mode=False)
        self.assertEqual(rc, 0)
        self.assertIn("plan, read-only", text)
        self.assertIn("No worktree, branch, process, store, ticket or send was", text)

    def test_no_output_carries_a_host_local_worktree_path(self):
        _, _, text = run_cli(mixed_fleet(), json_mode=False)
        _, payload, _ = run_cli(mixed_fleet())
        self.assertNotIn(PRIVATE_WORKTREE, text)
        self.assertNotIn(PRIVATE_WORKTREE, json.dumps(payload))


class ExecuteStageAcceptance(unittest.TestCase):
    def test_heal_and_dispatch_are_one_composed_create_and_the_brief_follows(self):
        rc, payload, _ = run_cli(mixed_fleet(), execute=True)
        self.assertEqual(rc, 1, "the uncertain lane could not be rehydrated")
        self.assertEqual(payload["state"], "executed")
        ops = FakeOps.instances[0]
        self.assertEqual(
            ops.heal_calls,
            [("issue_101_impl", True), ("issue_102_l2", False)],
            "the L3 lane heals+dispatches in one action; the L2 lane only heals",
        )
        self.assertEqual(ops.brief_calls, [("issue_102_l2", "w1V:pF")])

        rows = by_lane(payload)
        self.assertEqual(rows["issue_101_impl"]["outcome"]["status"], "applied")
        self.assertEqual(
            rows["issue_101_impl"]["outcome"]["applied"],
            [ACTION_HEAL_PAIR, ACTION_RESTORE_DISPATCH],
        )
        self.assertEqual(
            rows["issue_102_l2"]["outcome"]["applied"],
            [ACTION_HEAL_PAIR, ACTION_RESUME_BRIEF],
        )
        self.assertEqual(rows["issue_103_idle"]["outcome"]["status"], "skipped")
        self.assertEqual(rows["issue_105_uncertain"]["outcome"]["status"], "blocked")
        self.assertEqual(rows["issue_105_uncertain"]["outcome"]["attempted"], [])

    def test_a_partial_failure_is_truthful_and_exits_non_zero(self):
        rc, payload, _ = run_cli(
            (mixed_fleet()[1],), execute=True, fail_brief=True
        )
        self.assertEqual(rc, 1)
        outcome = by_lane(payload)["issue_102_l2"]["outcome"]
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(outcome["applied"], [ACTION_HEAL_PAIR])
        self.assertEqual(outcome["attempted"], [ACTION_HEAL_PAIR, ACTION_RESUME_BRIEF])
        self.assertEqual(outcome["reason"], ops_module.REFUSED_RESUME_BRIEF_FAILED)

    def test_a_fully_converged_fleet_executes_clean_at_exit_zero(self):
        fleet = (mixed_fleet()[2], mixed_fleet()[3])
        rc, payload, _ = run_cli(fleet, execute=True)
        self.assertEqual(rc, 0)
        self.assertEqual(FakeOps.instances[0].heal_calls, [])
        self.assertEqual(FakeOps.instances[0].brief_calls, [])

    def test_the_second_pass_does_not_replay_a_delivered_dispatch(self):
        """Idempotent resume: the authorities are re-read, so a landed send is not owed."""
        first = (_lane("issue_101_impl", "101"),)
        rc, payload, _ = run_cli(first, execute=True)
        self.assertEqual(rc, 0)
        self.assertEqual(FakeOps.instances[0].heal_calls, [("issue_101_impl", True)])

        # Second pass: the create landed the pair and the ledger now records the delivery.
        second = (
            _lane(
                "issue_101_impl",
                "101",
                live_pair=True,
                dispatch_state=DISPATCH_DELIVERED,
            ),
        )
        rc, payload, _ = run_cli(second, execute=True)
        self.assertEqual(rc, 0)
        self.assertEqual(FakeOps.instances[0].heal_calls, [])
        self.assertEqual(
            by_lane(payload)["issue_101_impl"]["outcome"]["reason"], SKIP_IDLE
        )


class UnavailableAuthorityAcceptance(unittest.TestCase):
    def test_an_unreadable_authority_exits_non_zero_and_is_not_an_empty_success(self):
        args = argparse.Namespace(
            repo=".",
            json=True,
            execute=False,
            lane_label="",
            integration_branch="",
            resume_anchor=None,
            resume_profile_field=None,
        )
        out = io.StringIO()
        with mock.patch.object(
            rehydrate,
            "gather_fleet_facts",
            side_effect=rehydrate.FleetRehydrateUnavailable("lifecycle store unreadable"),
        ):
            with contextlib.redirect_stdout(out):
                rc = rehydrate.cmd_sublane_rehydrate_fleet(args)
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["lanes"], [])
        self.assertEqual(payload["summary"]["lane_count"], 0)

    def test_a_genuinely_empty_fleet_is_a_zero_exit(self):
        rc, payload, _ = run_cli(())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["summary"]["lane_count"], 0)


class ResumeFlagParsingAcceptance(unittest.TestCase):
    def test_malformed_resume_flags_fail_closed_at_parse_time(self):
        for bad in ("no-equals", "=900", "lane="):
            with self.subTest(value=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    rehydrate.parse_resume_anchor(bad)
        for bad in ("lane:parent_project", "lane:=v", ":k=v", "lane:unknown_key=v"):
            with self.subTest(value=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    rehydrate.parse_resume_profile_field(bad)

    def test_the_flags_fold_into_one_per_lane_input(self):
        args = argparse.Namespace(
            resume_anchor=[("l2", "9001")],
            resume_profile_field=[
                ("l2", "parent_project", "p"),
                ("l2", "child_project", "c"),
            ],
        )
        inputs = rehydrate.resume_inputs_from_args(args)
        self.assertEqual(inputs["l2"].anchor_journal, "9001")
        self.assertEqual(
            inputs["l2"].field_map(), {"parent_project": "p", "child_project": "c"}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
