"""Cockpit dispatcher boundary — pure decisions + fake-port use case (Redmine #13011).

Pins the #13011 carve of the ``cmd_cockpit`` dispatcher residual out of
``commands.py`` into :mod:`mozyo_bridge.application.cockpit_dispatcher_command`:
the pure sub-action routing, the #11820/#12739 duplicate detection, the shared
create/append/focus action resolution, the ``--json`` / ``--dry-run``
projections, and the dispatch use case over fake
:class:`CockpitSubactionRoutes` / :class:`CockpitLaunchFlowOps` ports (no tmux,
no monkeypatch). The ``commands.cmd_cockpit`` thin-wrapper behavior (including
the ``os.execvp`` attach tail and the live ``commands.*`` patch seams) stays
pinned by the existing ``test_cockpit_decision`` / ``test_cockpit_append`` /
``test_cockpit_presentation_placement`` characterization suites. Synthetic,
neutral identifiers only.
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_dispatcher_command import (
    CockpitDispatchOutcome,
    CockpitDispatchUseCase,
    CockpitLaunchFlowOps,
    CockpitSubactionRoutes,
    LiveCockpitLaunchFlowOps,
    LiveCockpitSubactionRoutes,
    ROUTE_ADOPT,
    ROUTE_DOCTOR_GEOMETRY,
    ROUTE_LAUNCH,
    ROUTE_LIST,
    ROUTE_PEER_ADOPT,
    ROUTE_REBALANCE,
    ROUTE_RECONCILE,
    ROUTE_RESET,
    ROUTE_STATUS,
    build_cockpit_json_payload,
    find_same_unit_column,
    render_cockpit_dry_run_lines,
    resolve_cockpit_route,
    resolve_shared_cockpit_action,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    ADOPT_STATUS_NONE,
    AdoptAdvisory,
    CockpitWorkspace,
    DEFAULT_LANE,
    LaneIdentity,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfig,
    RepoLocalConfigError,
)


def _ws(**over):
    base = dict(
        workspace_id="wsX", label="sessX", repo_root="/workspace/project-alpha",
        lane_id=DEFAULT_LANE, lane_label=None,
    )
    base.update(over)
    return CockpitWorkspace(**base)


def _column(pane_id="%1", *, workspace_id="wsX", role="codex", lane_id=DEFAULT_LANE,
            project_scope=None):
    col = {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "pane_left": 0,
        "pane_width": 100,
    }
    if project_scope is not None:
        col["project_scope"] = project_scope
    return col


def _args(**over):
    base = dict(
        action=None, repo="/repoX", codex_ratio=70, cockpit_session=None,
        dry_run=False, json_output=False, no_attach=False, confirm=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class FakeRoutes:
    """Recording :class:`CockpitSubactionRoutes` fake — every route returns 7."""

    def __init__(self):
        self.calls: list[tuple] = []

    def doctor_geometry(self, session, *, json_output):
        self.calls.append(("doctor_geometry", session, json_output))
        return 7

    def membership_list(self, session, *, json_output):
        self.calls.append(("membership_list", session, json_output))
        return 7

    def membership_status(self, args, session, *, json_output):
        self.calls.append(("membership_status", args, session, json_output))
        return 7

    def peer_adopt(self, session, args, *, json_output, dry_run):
        self.calls.append(("peer_adopt", session, args, json_output, dry_run))
        return 7

    def rebalance(self, session, *, confirm, json_output, dry_run):
        self.calls.append(("rebalance", session, confirm, json_output, dry_run))
        return 7

    def reconcile(self, session, *, confirm, json_output, dry_run, codex_ratio):
        self.calls.append(
            ("reconcile", session, confirm, json_output, dry_run, codex_ratio)
        )
        return 7

    def adopt(self, args, workspace, session, *, columns, session_present,
              already_in_cockpit, existing_codex):
        self.calls.append(
            ("adopt", session, columns, session_present, already_in_cockpit,
             existing_codex)
        )
        return 7

    def reset(self, args, workspace, session, *, columns, session_present,
              rebuild, launch, codex_ratio):
        self.calls.append(
            ("reset", session, columns, session_present, rebuild, codex_ratio)
        )
        self.reset_launch = launch
        return 7


class FakeOps:
    """Recording :class:`CockpitLaunchFlowOps` fake — no tmux, no filesystem."""

    def __init__(self, *, columns=None, session_present=False, ws_id="wsX",
                 grouping_config=None, grouping_error=None, advisory=None,
                 anchor="auto"):
        self.columns = columns
        self.session_present = session_present
        self.ws_id = ws_id
        self.grouping_config = grouping_config or RepoLocalConfig.default()
        self.grouping_error = grouping_error
        self.advisory = advisory if advisory is not None else AdoptAdvisory(
            ws_id, DEFAULT_LANE, ADOPT_STATUS_NONE, (), None
        )
        self.anchor = anchor
        self.emitted: list[str] = []
        self.died: list[str] = []
        self.require_tmux_calls = 0
        self.executed: list[tuple] = []
        self.group_action = None  # (action, plan, blocked_reason, window)
        self.group_action_calls: list[dict] = []

    def resolve_project_scope_fields(self, cwd, repo_root):
        return repo_root, (None, None, None), None

    def resolve_canonical_session(self, repo_root):
        return SimpleNamespace(name="sessX", workspace_id=self.ws_id)

    def resolve_workspace_lane(self, repo_root, workspace_id):
        return LaneIdentity(DEFAULT_LANE, None)

    def agent_launch_command(self, role, session, repo_root):
        return f"{role}-cmd"

    def require_tmux(self):
        self.require_tmux_calls += 1

    def read_cockpit_columns(self, session):
        return self.columns

    def cockpit_session_present(self, session):
        return self.session_present

    def adopt_advisory(self, workspace, session):
        return self.advisory

    def load_presentation_grouping(self, repo_root):
        if self.grouping_error is not None:
            raise self.grouping_error
        return self.grouping_config.presentation.grouping

    def group_window_action(self, workspace, session, *, decision, codex_ratio,
                            launch):
        self.group_action_calls.append(
            {"session": session, "decision": decision, "codex_ratio": codex_ratio}
        )
        return self.group_action

    def rightmost_codex_anchor(self, codex_columns):
        if self.anchor == "auto":
            return codex_columns[-1]["pane_id"] if codex_columns else None
        return self.anchor

    def execute_plan(self, plan, *, cleanup_captured=False):
        self.executed.append((plan, cleanup_captured))
        return {}

    def die(self, message):
        self.died.append(message)
        raise SystemExit(2)

    def emit(self, text):
        self.emitted.append(text)


class ResolveCockpitRouteTest(unittest.TestCase):
    """Pure sub-action routing (#13011)."""

    def test_pre_workspace_routes_map_to_themselves(self) -> None:
        for action, route in (
            ("doctor-geometry", ROUTE_DOCTOR_GEOMETRY),
            ("list", ROUTE_LIST),
            ("status", ROUTE_STATUS),
            ("peer-adopt", ROUTE_PEER_ADOPT),
            ("rebalance", ROUTE_REBALANCE),
            ("reconcile", ROUTE_RECONCILE),
        ):
            self.assertEqual(resolve_cockpit_route(action), route)

    def test_adopt_routes_adopt(self) -> None:
        self.assertEqual(resolve_cockpit_route("adopt"), ROUTE_ADOPT)

    def test_reset_and_rebuild_share_the_reset_route(self) -> None:
        self.assertEqual(resolve_cockpit_route("reset"), ROUTE_RESET)
        self.assertEqual(resolve_cockpit_route("rebuild"), ROUTE_RESET)

    def test_absent_or_unknown_action_falls_through_to_launch(self) -> None:
        self.assertEqual(resolve_cockpit_route(None), ROUTE_LAUNCH)
        self.assertEqual(resolve_cockpit_route("no-such-action"), ROUTE_LAUNCH)


class FindSameUnitColumnTest(unittest.TestCase):
    """Pure #11820/#12739 duplicate detection."""

    def test_matches_same_workspace_lane_and_scope(self) -> None:
        cols = [_column("%1"), _column("%2", workspace_id="wsB")]
        existing, same = find_same_unit_column(cols, _ws())
        self.assertEqual([c["pane_id"] for c in existing], ["%1", "%2"])
        self.assertEqual(same["pane_id"], "%1")

    def test_different_lane_is_not_a_duplicate(self) -> None:
        cols = [_column("%1", lane_id="lane-alt")]
        _existing, same = find_same_unit_column(cols, _ws())
        self.assertIsNone(same)

    def test_different_project_scope_is_not_a_duplicate(self) -> None:
        cols = [_column("%1", project_scope="")]
        _existing, same = find_same_unit_column(
            cols, _ws(project_scope="projects/alpha")
        )
        self.assertIsNone(same)

    def test_missing_lane_and_scope_normalize_to_default_and_empty(self) -> None:
        col = _column("%1")
        del col["lane_id"]
        _existing, same = find_same_unit_column([col], _ws())
        self.assertEqual(same["pane_id"], "%1")

    def test_non_codex_columns_and_absent_read_are_ignored(self) -> None:
        cols = [_column("%1", role="claude")]
        existing, same = find_same_unit_column(cols, _ws())
        self.assertEqual(existing, [])
        self.assertIsNone(same)
        self.assertEqual(find_same_unit_column(None, _ws()), ([], None))


class ResolveSharedCockpitActionTest(unittest.TestCase):
    """Pure create/append/focus action resolution (#11803/#11849)."""

    def _resolve(self, *, columns, session_present=False, same=None,
                 existing_codex=(), anchor=None):
        return resolve_shared_cockpit_action(
            _ws(),
            "cockpit-s",
            columns=columns,
            session_present=session_present,
            same=same,
            existing_codex=list(existing_codex),
            codex_ratio=70,
            launch=lambda role, ws: f"{role}-cmd",
            rightmost_codex_anchor=lambda cols: anchor,
        )

    def test_stale_session_without_cockpit_window_blocks_create(self) -> None:
        action, plan, blocked = self._resolve(columns=None, session_present=True)
        self.assertEqual(action, "create")
        self.assertIsNone(plan)
        self.assertIn("already exists but has no cockpit window", blocked)

    def test_absent_cockpit_builds_the_create_plan(self) -> None:
        action, plan, blocked = self._resolve(columns=None)
        self.assertEqual(action, "create")
        self.assertIsNone(blocked)
        self.assertTrue(any("new-session" in cmd.argv for cmd in plan.commands))

    def test_same_unit_column_focuses_that_pane(self) -> None:
        action, plan, blocked = self._resolve(
            columns=[_column("%9")], same=_column("%9")
        )
        self.assertEqual(action, "focus")
        self.assertIsNone(blocked)
        self.assertTrue(any("%9" in cmd.argv for cmd in plan.commands))

    def test_append_splits_from_the_injected_anchor(self) -> None:
        cols = [_column("%1"), _column("%2", workspace_id="wsB")]
        action, plan, blocked = self._resolve(
            columns=cols, existing_codex=cols, anchor="%2"
        )
        self.assertEqual(action, "append")
        self.assertIsNone(blocked)
        split = next(cmd for cmd in plan.commands if "split-window" in cmd.argv)
        self.assertIn("%2", split.argv)

    def test_append_without_codex_anchor_blocks(self) -> None:
        action, plan, blocked = self._resolve(
            columns=[_column("%1", role="claude")], existing_codex=[], anchor=None
        )
        self.assertEqual(action, "append")
        self.assertIsNone(plan)
        self.assertIn("no mozyo-identified codex column", blocked)


class RenderingProjectionTest(unittest.TestCase):
    """Pure `--json` payload / `--dry-run` text projections."""

    def test_json_payload_carries_the_projection_fields(self) -> None:
        payload = build_cockpit_json_payload(
            plan=None,
            action="create",
            workspace=_ws(project_scope="projects/alpha"),
            session="cockpit-s",
            blocked_reason="stale",
            adopt_advisory=None,
            presentation_decision=None,
            presentation_blocked=None,
            group_window=None,
        )
        self.assertEqual(payload["action"], "create")
        self.assertEqual(payload["workspace_id"], "wsX")
        self.assertEqual(payload["lane_id"], DEFAULT_LANE)
        self.assertIsNone(payload["lane_label"])
        self.assertEqual(payload["project_scope"], "projects/alpha")
        self.assertEqual(payload["session"], "cockpit-s")
        self.assertEqual(payload["blocked"], "stale")
        self.assertIsNone(payload["adopt_advisory"])
        self.assertIsNone(payload["presentation"])
        self.assertIsNone(payload["presentation_blocked"])
        self.assertIsNone(payload["group_window"])
        json.dumps(payload)  # payload stays JSON-serializable

    def test_dry_run_lines_render_blocked_and_notices(self) -> None:
        advisory = AdoptAdvisory(
            "wsX", DEFAULT_LANE, "candidate", ("sessX",), "adopt hint"
        )
        lines = render_cockpit_dry_run_lines(
            plan=None,
            action="append",
            workspace=_ws(),
            session="cockpit-s",
            blocked_reason="no anchor",
            adopt_advisory=advisory,
            presentation_decision=None,
            presentation_blocked="bad config",
            group_window="grp",
        )
        self.assertEqual(
            lines[0],
            "cockpit plan: action=append session=cockpit-s "
            f"workspace=wsX (sessX) lane={DEFAULT_LANE}",
        )
        self.assertIn("  (blocked: no anchor)", lines)
        self.assertIn("  (presentation blocked: bad config)", lines)
        self.assertTrue(any("Project Group window 'grp'" in ln for ln in lines))
        self.assertIn("  adopt hint", lines)

    def test_dry_run_lines_render_the_plan_commands(self) -> None:
        plan = SimpleNamespace(
            commands=[SimpleNamespace(argv=["select-pane", "-t", "%1"])]
        )
        lines = render_cockpit_dry_run_lines(
            plan=plan,
            action="focus",
            workspace=_ws(),
            session="cockpit-s",
            blocked_reason=None,
            adopt_advisory=None,
            presentation_decision=None,
            presentation_blocked=None,
            group_window=None,
        )
        self.assertIn("  tmux select-pane -t %1", lines)


class CockpitDispatchUseCaseTest(unittest.TestCase):
    """Dispatch use case over the fake ports."""

    def _run(self, args, ops):
        routes = FakeRoutes()
        outcome = CockpitDispatchUseCase(routes, ops).run(args)
        return outcome, routes

    def test_pre_workspace_subactions_short_circuit_before_any_read(self) -> None:
        for action, method in (
            ("doctor-geometry", "doctor_geometry"),
            ("list", "membership_list"),
            ("status", "membership_status"),
            ("peer-adopt", "peer_adopt"),
            ("rebalance", "rebalance"),
            ("reconcile", "reconcile"),
        ):
            ops = FakeOps()
            outcome, routes = self._run(_args(action=action, json_output=True), ops)
            self.assertEqual(outcome, CockpitDispatchOutcome(exit_code=7))
            self.assertEqual(routes.calls[0][0], method)
            # Short-circuits before workspace resolution: no cockpit read ran.
            self.assertEqual(ops.require_tmux_calls, 0)
            self.assertEqual(ops.emitted, [])

    def test_reconcile_forwards_confirm_and_ratio(self) -> None:
        ops = FakeOps()
        _outcome, routes = self._run(
            _args(action="reconcile", confirm=True, codex_ratio=55), ops
        )
        self.assertEqual(routes.calls, [("reconcile", "mozyo-cockpit", True,
                                         False, False, 55)])

    def test_adopt_routes_after_the_column_read_with_duplicate_context(self) -> None:
        cols = [_column("%1")]
        ops = FakeOps(columns=cols, session_present=True)
        outcome, routes = self._run(_args(action="adopt"), ops)
        self.assertEqual(outcome.exit_code, 7)
        kind, session, columns, present, already, existing = routes.calls[0]
        self.assertEqual(kind, "adopt")
        self.assertEqual(session, "mozyo-cockpit")
        self.assertIs(columns, cols)
        self.assertTrue(present)
        self.assertTrue(already)
        self.assertEqual(existing, cols)
        # adopt never gates on tmux being mutable up front.
        self.assertEqual(ops.require_tmux_calls, 0)

    def test_rebuild_routes_reset_with_rebuild_flag_and_launch(self) -> None:
        ops = FakeOps()
        outcome, routes = self._run(_args(action="rebuild"), ops)
        self.assertEqual(outcome.exit_code, 7)
        kind, _session, _columns, _present, rebuild, ratio = routes.calls[0]
        self.assertEqual(kind, "reset")
        self.assertTrue(rebuild)
        self.assertEqual(ratio, 70)
        self.assertEqual(routes.reset_launch("codex", _ws()), "codex-cmd")
        self.assertEqual(ops.require_tmux_calls, 0)

    def test_dry_run_reads_but_never_gates_or_mutates(self) -> None:
        ops = FakeOps(columns=None)
        outcome, _routes = self._run(_args(dry_run=True), ops)
        self.assertEqual(outcome, CockpitDispatchOutcome(exit_code=0))
        self.assertEqual(ops.require_tmux_calls, 0)
        self.assertEqual(ops.executed, [])
        self.assertTrue(ops.emitted[0].startswith("cockpit plan: action=create"))

    def test_json_run_emits_the_payload(self) -> None:
        ops = FakeOps(columns=None, session_present=True)
        outcome, _routes = self._run(_args(json_output=True), ops)
        self.assertEqual(outcome.exit_code, 0)
        payload = json.loads("\n".join(ops.emitted))
        self.assertEqual(payload["action"], "create")
        self.assertIn("already exists but has no cockpit window", payload["blocked"])

    def test_real_create_executes_with_rollback_and_hands_back_attach(self) -> None:
        ops = FakeOps(columns=None)
        outcome, _routes = self._run(_args(), ops)
        self.assertEqual(ops.require_tmux_calls, 1)
        self.assertEqual(len(ops.executed), 1)
        self.assertTrue(ops.executed[0][1])  # cleanup_captured
        self.assertIn(
            "cockpit created: session=mozyo-cockpit workspace=sessX", ops.emitted
        )
        self.assertEqual(outcome, CockpitDispatchOutcome(0, "mozyo-cockpit"))

    def test_no_attach_prints_the_attach_command_instead(self) -> None:
        ops = FakeOps(columns=None)
        outcome, _routes = self._run(_args(no_attach=True), ops)
        self.assertIn("attach: tmux -CC attach -t mozyo-cockpit", ops.emitted)
        self.assertEqual(outcome, CockpitDispatchOutcome(exit_code=0))

    def test_real_focus_selects_the_existing_pane_without_rollback(self) -> None:
        ops = FakeOps(columns=[_column("%9")])
        outcome, _routes = self._run(_args(), ops)
        self.assertEqual(outcome, CockpitDispatchOutcome(exit_code=0))
        self.assertFalse(ops.executed[0][1])  # no cleanup_captured on focus
        self.assertIn(
            "workspace 'sessX' already in cockpit 'mozyo-cockpit'; "
            "focused pane %9",
            ops.emitted,
        )

    def test_real_append_emits_the_column_notice(self) -> None:
        ops = FakeOps(columns=[_column("%1", workspace_id="wsB")])
        outcome, _routes = self._run(_args(), ops)
        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(ops.executed[0][1])
        self.assertTrue(ops.emitted[0].startswith("appended 'sessX' as a new column"))

    def test_blocked_real_run_fails_closed(self) -> None:
        ops = FakeOps(columns=None, session_present=True)
        with self.assertRaises(SystemExit):
            self._run(_args(), ops)
        self.assertIn("already exists but has no cockpit window", ops.died[0])

    def test_invalid_presentation_config_fails_closed_on_a_real_run(self) -> None:
        ops = FakeOps(
            columns=None, grouping_error=RepoLocalConfigError("bad placement")
        )
        with self.assertRaises(SystemExit):
            self._run(_args(), ops)
        self.assertEqual(
            ops.died,
            ["invalid .mozyo-bridge/config.yaml presentation config: bad placement"],
        )

    def test_faithful_group_window_routes_through_the_group_action(self) -> None:
        grouping = RepoLocalConfig.from_record(
            {"presentation": {"project_group_presentation":
                              "project_group_tmux_window"}}
        )
        ops = FakeOps(
            columns=[_column("%1", workspace_id="wsB")], grouping_config=grouping
        )
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_create", plan, None, "grp-win")
        outcome, _routes = self._run(_args(), ops)
        self.assertEqual(outcome, CockpitDispatchOutcome(exit_code=0))
        self.assertEqual(len(ops.group_action_calls), 1)
        self.assertTrue(ops.executed[0][1])  # group create uses rollback
        self.assertTrue(
            ops.emitted[0].startswith("created Project Group window 'grp-win'")
        )

    def test_group_focus_never_uses_rollback(self) -> None:
        grouping = RepoLocalConfig.from_record(
            {"presentation": {"project_group_presentation":
                              "project_group_tmux_window"}}
        )
        ops = FakeOps(columns=[_column("%1")], grouping_config=grouping)
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_focus", plan, None, "grp-win")
        outcome, _routes = self._run(_args(), ops)
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(ops.executed[0][1])
        self.assertIn("(window 'grp-win'); focused it.", ops.emitted[0])


class SublaneSeparateWindowTest(unittest.TestCase):
    """Sublane window actuation through the dispatcher (Redmine #13015 / #13085).

    Under the opt-in `delegation_window_policy: separate` a sublane lane whose
    repo faithfully executes `project_group_tmux_window` routes through the
    group-window action with the lane-scoped decision — its own sublane
    window. Under `shared` (the #13085 default) the sublane reuses the single
    project/common host window, and every `separate` fallback stays the shared
    column with the reason recorded machine-readably on the `sublane_window`
    payload field.
    """

    GROUP_ON = {"presentation": {"project_group_presentation":
                                 "project_group_tmux_window"}}

    def _sublane_ops(self, record=None, *, columns="default", policy=None,
                     lane=LaneIdentity("lane-abc", "issue_42_topic")):
        rec = dict(record if record is not None else self.GROUP_ON)
        if policy is not None:
            rec.setdefault("presentation", {})
            rec["presentation"] = dict(rec["presentation"])
            rec["presentation"]["delegation_window_policy"] = policy
        ops = FakeOps(
            columns=(
                [_column("%1", workspace_id="wsB")] if columns == "default"
                else columns
            ),
            grouping_config=RepoLocalConfig.from_record(rec),
        )
        ops.resolve_workspace_lane = lambda repo_root, workspace_id: lane
        return ops

    def _run(self, args, ops):
        routes = FakeRoutes()
        outcome = CockpitDispatchUseCase(routes, ops).run(args)
        return outcome, routes

    def test_sublane_routes_to_its_own_window_with_the_lane_key(self) -> None:
        # Opt-in `separate` (#13015); no longer the default (#13085).
        ops = self._sublane_ops(policy="separate")
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_create", plan, None, "issue_42_topic")
        outcome, _routes = self._run(_args(), ops)
        self.assertEqual(outcome, CockpitDispatchOutcome(exit_code=0))
        decision = ops.group_action_calls[0]["decision"]
        self.assertTrue(decision.separated)
        self.assertEqual(decision.group_id, "lane:wsX/lane-abc")
        self.assertEqual(decision.desired_window_name, "issue_42_topic")
        self.assertTrue(ops.executed[0][1])  # create keeps the rollback boundary
        self.assertTrue(
            ops.emitted[0].startswith("created sublane window 'issue_42_topic'")
        )

    def test_sublane_append_notice_names_the_sublane_window(self) -> None:
        ops = self._sublane_ops(policy="separate")
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_append", plan, None, "issue_42_topic")
        _outcome, _routes = self._run(_args(), ops)
        self.assertIn(
            "as a new column to sublane window 'issue_42_topic'", ops.emitted[0]
        )

    def test_shared_policy_keeps_the_project_group_column(self) -> None:
        ops = self._sublane_ops(policy="shared")
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_append", plan, None, "grp-win")
        _outcome, _routes = self._run(_args(), ops)
        # The group-window flow still runs, but with the PRESENTATION decision
        # (the project window), never the lane-scoped one.
        decision = ops.group_action_calls[0]["decision"]
        self.assertFalse(hasattr(decision, "separated"))
        self.assertIn("to Project Group window 'grp-win'", ops.emitted[0])

    def test_default_policy_reuses_the_project_host_window(self) -> None:
        # #13085 acceptance: with NO delegation_window_policy configured, a
        # second sublane appends into the single project/common host window
        # instead of spawning its own lane window.
        ops = self._sublane_ops()
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_append", plan, None, "grp-win")
        _outcome, _routes = self._run(_args(), ops)
        decision = ops.group_action_calls[0]["decision"]
        self.assertFalse(hasattr(decision, "separated"))
        self.assertIn("to Project Group window 'grp-win'", ops.emitted[0])

    def test_default_policy_payload_is_shared_and_not_degraded(self) -> None:
        # #13085: the default host-window reuse is the faithful execution —
        # recorded as `shared`, never a degraded fallback.
        ops = self._sublane_ops()
        plan = SimpleNamespace(commands=[], as_dict=lambda: {})
        ops.group_action = ("group_append", plan, None, "grp-win")
        outcome, _routes = self._run(_args(json_output=True), ops)
        self.assertEqual(outcome.exit_code, 0)
        payload = json.loads("\n".join(ops.emitted))
        sub = payload["sublane_window"]
        self.assertEqual(sub["window_policy"], "shared")
        self.assertFalse(sub["separated"])
        self.assertFalse(sub["degraded"])
        self.assertIsNone(sub["diagnostic"])

    def test_same_column_compat_records_the_fallback_machine_readably(self) -> None:
        ops = self._sublane_ops(record={"presentation": {}}, policy="separate")
        outcome, _routes = self._run(_args(json_output=True), ops)
        self.assertEqual(outcome.exit_code, 0)
        payload = json.loads("\n".join(ops.emitted))
        sub = payload["sublane_window"]
        self.assertEqual(sub["window_policy"], "separate")
        self.assertFalse(sub["separated"])
        self.assertTrue(sub["degraded"])
        self.assertIn("project_group_tmux_window", sub["diagnostic"])
        # The shared-column action itself is unchanged (append beside wsB).
        self.assertEqual(payload["action"], "append")

    def test_same_column_real_run_emits_the_fallback_notice(self) -> None:
        ops = self._sublane_ops(record={"presentation": {}}, policy="separate")
        _outcome, _routes = self._run(_args(), ops)
        self.assertTrue(ops.emitted[0].startswith("appended 'sessX' as a new column"))
        self.assertTrue(
            any("delegation_window_policy 'separate'" in ln for ln in ops.emitted)
        )

    def test_bootstrap_degrades_and_still_creates_the_session(self) -> None:
        ops = self._sublane_ops(columns=None, policy="separate")
        outcome, _routes = self._run(_args(json_output=True), ops)
        self.assertEqual(outcome.exit_code, 0)
        payload = json.loads("\n".join(ops.emitted))
        self.assertEqual(payload["action"], "create")
        self.assertTrue(payload["sublane_window"]["degraded"])
        self.assertIn("bootstrap", payload["sublane_window"]["diagnostic"])

    def test_primary_checkout_payload_carries_no_sublane_window(self) -> None:
        ops = FakeOps(columns=[_column("%1", workspace_id="wsB")])
        routes = FakeRoutes()
        outcome = CockpitDispatchUseCase(routes, ops).run(_args(json_output=True))
        self.assertEqual(outcome.exit_code, 0)
        payload = json.loads("\n".join(ops.emitted))
        self.assertIsNone(payload["sublane_window"])

    def test_dry_run_renders_the_sublane_window_line(self) -> None:
        ops = self._sublane_ops(policy="separate")
        plan = SimpleNamespace(commands=[])
        ops.group_action = ("group_create", plan, None, "issue_42_topic")
        _outcome, _routes = self._run(_args(dry_run=True), ops)
        self.assertEqual(ops.executed, [])  # dry-run never mutates
        self.assertTrue(
            any(
                "delegation_window_policy=separate -> sublane window "
                "'issue_42_topic'" in ln
                for ln in ops.emitted
            )
        )


class BoundaryWiringTest(unittest.TestCase):
    """The live adapters and the `commands` thin wrapper stay wired (#13011)."""

    def test_live_adapters_satisfy_the_ports(self) -> None:
        self.assertIsInstance(LiveCockpitSubactionRoutes(), CockpitSubactionRoutes)
        self.assertIsInstance(LiveCockpitLaunchFlowOps(), CockpitLaunchFlowOps)

    def test_fakes_satisfy_the_ports(self) -> None:
        self.assertIsInstance(FakeRoutes(), CockpitSubactionRoutes)
        self.assertIsInstance(FakeOps(), CockpitLaunchFlowOps)

    def test_cmd_cockpit_stays_the_public_entry(self) -> None:
        from mozyo_bridge.application import commands

        self.assertTrue(callable(commands.cmd_cockpit))


if __name__ == "__main__":
    unittest.main()
