"""Tests for the grandchild lane realization stamp actuator (Redmine #12473).

Covers the pure stamp-plan resolver
(:mod:`mozyo_bridge.domain.grandchild_stamp`): the realization shape gate
(launch / adopt + adopt-reason), the declared-tree validation reused from the
#12465 projection foundation (fail-closed on unknown parent / cycle / depth > 2 /
off-contract kind), the grandchild acceptance shape (a depth-2 `implementation`
lane), and the pure `set-option -p` plan that stamps only the two options the
discovery read path consumes. Also covers the CLI actuator
(:mod:`mozyo_bridge.application.grandchild_stamp`): lane-spec parsing,
preview-by-default / `--apply` / `--dry-run`, the JSON surface, and the
replayable realization record.

The centerpiece is the #12460 regression: a grandchild dispatch decision / a
same-lane worker handoff alone leaves `KIND` / `DEPTH` / `PARENT` blank in
`agents targets`; only after the stamp actuator writes the projection-cache
options does `delegation_display` derive `KIND=implementation` / `DEPTH=2` /
`PARENT=<delegated coordinator lane>`. A decision record is not a display PASS.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.application.grandchild_stamp import (
    _parse_lane_spec,
    cmd_handoff_grandchild_gate,
    cmd_handoff_grandchild_stamp,
)
from mozyo_bridge.domain.delegation_projection import (
    OPTION_DELEGATION_PARENT,
    OPTION_LANE_KIND,
)
from mozyo_bridge.domain.grandchild_stamp import (
    DeclaredLane,
    GATE_BLOCKED,
    GATE_REALIZED,
    GATE_SAME_LANE_OK,
    GrandchildStampError,
    REALIZATION_ADOPT,
    REALIZATION_LAUNCH,
    evaluate_grandchild_realization_gate,
    find_realized_grandchild_unit,
    resolve_grandchild_stamp_plan,
)

# Per-lane unit pointers use the `<workspace_id>/<lane_id>` display convention.
PARENT_UNIT = "gk/lane-parent"
DELEG_UNIT = "mozyo/lane-deleg"
GC_UNIT = "mozyo/lane-grandchild"


def _chain(*, gc_panes=("%16",), deleg_panes=("%15",), parent_panes=()):
    """A valid parent -> delegated -> grandchild declared chain."""
    return [
        DeclaredLane(unit_id=PARENT_UNIT, lane_kind="coordinator", panes=parent_panes),
        DeclaredLane(
            unit_id=DELEG_UNIT,
            lane_kind="delegated_coordinator",
            delegation_parent=PARENT_UNIT,
            panes=deleg_panes,
        ),
        DeclaredLane(
            unit_id=GC_UNIT,
            lane_kind="implementation",
            delegation_parent=DELEG_UNIT,
            panes=gc_panes,
        ),
    ]


class ResolveStampPlanTest(unittest.TestCase):
    def test_valid_chain_derives_depth_2_implementation(self) -> None:
        plan = resolve_grandchild_stamp_plan(
            _chain(), grandchild_unit=GC_UNIT, realization=REALIZATION_ADOPT,
            adopt_reason="same-lane worker adopted",
        )
        self.assertEqual("implementation", plan.grandchild_lane_kind)
        self.assertEqual(2, plan.grandchild_depth)
        self.assertEqual(DELEG_UNIT, plan.grandchild_parent)
        self.assertEqual(PARENT_UNIT, plan.grandchild_root)
        self.assertTrue(plan.is_adopt)
        self.assertEqual("same-lane worker adopted", plan.adopt_reason)

    def test_plan_stamps_only_read_surface_options(self) -> None:
        plan = resolve_grandchild_stamp_plan(
            _chain(), grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
        )
        options = {argv[4] for argv in plan.commands}
        self.assertEqual({OPTION_LANE_KIND, OPTION_DELEGATION_PARENT}, options)
        # depth / root are derived, never stamped.
        self.assertNotIn("@mozyo_delegation_depth", options)
        self.assertNotIn("@mozyo_delegation_root", options)

    def test_plan_stamps_each_declared_pane(self) -> None:
        plan = resolve_grandchild_stamp_plan(
            _chain(gc_panes=("%16", "%17"), deleg_panes=("%15",)),
            grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
        )
        # 3 panes x 2 options each.
        self.assertEqual(("%15", "%16", "%17"), plan.stamped_panes)
        self.assertEqual(6, len(plan.commands))
        # Every command is a pane-scoped set-option.
        for argv in plan.commands:
            self.assertEqual(("set-option", "-p", "-t"), argv[:3])
        # The grandchild panes carry kind=implementation + parent=<delegated>.
        gc_kind = [
            argv for argv in plan.commands
            if argv[3] == "%16" and argv[4] == OPTION_LANE_KIND
        ]
        self.assertEqual([("set-option", "-p", "-t", "%16", OPTION_LANE_KIND,
                           "implementation")], gc_kind)
        gc_parent = [
            argv for argv in plan.commands
            if argv[3] == "%16" and argv[4] == OPTION_DELEGATION_PARENT
        ]
        self.assertEqual([("set-option", "-p", "-t", "%16",
                           OPTION_DELEGATION_PARENT, DELEG_UNIT)], gc_parent)

    def test_derivation_only_lane_has_no_panes_not_stamped(self) -> None:
        # The parent coordinator declared for derivation only (no panes) anchors
        # the chain so the grandchild depth derives, but is not stamped.
        plan = resolve_grandchild_stamp_plan(
            _chain(parent_panes=()), grandchild_unit=GC_UNIT,
            realization=REALIZATION_LAUNCH,
        )
        self.assertEqual(2, plan.grandchild_depth)
        # No command TARGETS the parent coordinator (it declared no panes), even
        # though its unit still appears as the delegated coordinator's parent
        # pointer value.
        self.assertEqual(("%15", "%16"), plan.stamped_panes)
        targeted_panes = {argv[3] for argv in plan.commands}
        self.assertEqual({"%15", "%16"}, targeted_panes)

    # --- realization shape gate ------------------------------------------

    def test_adopt_requires_reason(self) -> None:
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                _chain(), grandchild_unit=GC_UNIT, realization=REALIZATION_ADOPT,
            )

    def test_launch_rejects_reason(self) -> None:
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                _chain(), grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
                adopt_reason="should not be here",
            )

    def test_unknown_realization(self) -> None:
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                _chain(), grandchild_unit=GC_UNIT, realization="teleport",
            )

    def test_empty_declared_lanes(self) -> None:
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                [], grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
            )

    # --- grandchild acceptance shape -------------------------------------

    def test_grandchild_unit_absent(self) -> None:
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                _chain(), grandchild_unit="mozyo/nope",
                realization=REALIZATION_LAUNCH,
            )

    def test_grandchild_must_be_implementation(self) -> None:
        # Pointing at the delegated coordinator (kind=delegated_coordinator).
        with self.assertRaises(GrandchildStampError) as ctx:
            resolve_grandchild_stamp_plan(
                _chain(), grandchild_unit=DELEG_UNIT,
                realization=REALIZATION_LAUNCH,
            )
        self.assertIn("implementation", str(ctx.exception))

    def test_grandchild_must_derive_depth_2(self) -> None:
        # A two-level chain: the "grandchild" implementation lane sits at depth 1,
        # i.e. a same-lane worker directly under the coordinator — the #12460
        # PARTIAL-display shape — which is not a full display PASS.
        lanes = [
            DeclaredLane(unit_id=PARENT_UNIT, lane_kind="coordinator"),
            DeclaredLane(
                unit_id=GC_UNIT, lane_kind="implementation",
                delegation_parent=PARENT_UNIT, panes=("%16",),
            ),
        ]
        with self.assertRaises(GrandchildStampError) as ctx:
            resolve_grandchild_stamp_plan(
                lanes, grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
            )
        self.assertIn("depth 2", str(ctx.exception))

    def test_grandchild_must_declare_a_live_pane(self) -> None:
        # A valid depth-2 implementation grandchild, but declared for derivation
        # only (no pane): stamping it would write no live grandchild breadcrumb,
        # reintroducing the #12460 PARTIAL-display gap. Fail closed (j#64105).
        with self.assertRaises(GrandchildStampError) as ctx:
            resolve_grandchild_stamp_plan(
                _chain(gc_panes=()), grandchild_unit=GC_UNIT,
                realization=REALIZATION_LAUNCH,
            )
        self.assertIn("no live pane", str(ctx.exception))

    def test_grandchild_empty_string_pane_is_not_a_live_pane(self) -> None:
        # An empty-string pane is not a live pane and must not satisfy the guard.
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                _chain(gc_panes=("",)), grandchild_unit=GC_UNIT,
                realization=REALIZATION_LAUNCH,
            )

    def test_unknown_parent_fails_closed(self) -> None:
        lanes = [
            DeclaredLane(
                unit_id=GC_UNIT, lane_kind="implementation",
                delegation_parent="mozyo/missing", panes=("%16",),
            ),
        ]
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                lanes, grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
            )

    def test_depth_beyond_ceiling_fails_closed(self) -> None:
        # A 4-level chain exceeds the shallow-delegation maximum.
        lanes = [
            DeclaredLane(unit_id="a", lane_kind="coordinator"),
            DeclaredLane(unit_id="b", lane_kind="delegated_coordinator",
                         delegation_parent="a"),
            DeclaredLane(unit_id="c", lane_kind="implementation",
                         delegation_parent="b"),
            DeclaredLane(unit_id="d", lane_kind="implementation",
                         delegation_parent="c", panes=("%9",)),
        ]
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                lanes, grandchild_unit="d", realization=REALIZATION_LAUNCH,
            )

    def test_off_contract_kind_fails_closed(self) -> None:
        lanes = [
            DeclaredLane(unit_id=PARENT_UNIT, lane_kind="overlord"),
            DeclaredLane(unit_id=GC_UNIT, lane_kind="implementation",
                         delegation_parent=PARENT_UNIT, panes=("%16",)),
        ]
        with self.assertRaises(GrandchildStampError):
            resolve_grandchild_stamp_plan(
                lanes, grandchild_unit=GC_UNIT, realization=REALIZATION_LAUNCH,
            )


class ParseLaneSpecTest(unittest.TestCase):
    def test_full_spec_with_repeated_pane(self) -> None:
        lane = _parse_lane_spec(
            "kind=implementation,unit=mozyo/g,parent=mozyo/d,pane=%16,pane=%17"
        )
        self.assertEqual("implementation", lane.lane_kind)
        self.assertEqual("mozyo/g", lane.unit_id)
        self.assertEqual("mozyo/d", lane.delegation_parent)
        self.assertEqual(("%16", "%17"), lane.panes)

    def test_root_parent_tokens_become_none(self) -> None:
        for token in ("-", "none", "root", ""):
            lane = _parse_lane_spec(f"kind=coordinator,unit=gk/p,parent={token}")
            self.assertIsNone(lane.delegation_parent, token)

    def test_missing_kind_or_unit(self) -> None:
        with self.assertRaises(GrandchildStampError):
            _parse_lane_spec("unit=mozyo/g,pane=%16")
        with self.assertRaises(GrandchildStampError):
            _parse_lane_spec("kind=implementation,pane=%16")

    def test_malformed_field(self) -> None:
        with self.assertRaises(GrandchildStampError):
            _parse_lane_spec("kind=implementation,bogus")
        with self.assertRaises(GrandchildStampError):
            _parse_lane_spec("kind=implementation,unit=mozyo/g,weird=x")


def _stamp_args(**over) -> argparse.Namespace:
    base = dict(
        lane=[
            f"kind=coordinator,unit={PARENT_UNIT},parent=-",
            f"kind=delegated_coordinator,unit={DELEG_UNIT},parent={PARENT_UNIT},pane=%15",
            f"kind=implementation,unit={GC_UNIT},parent={DELEG_UNIT},pane=%16",
        ],
        grandchild_unit=GC_UNIT,
        realization=REALIZATION_ADOPT,
        adopt_reason="same-lane worker adopted",
        parent_issue="12454",
        child_issue="12472",
        delegated_coordinator=DELEG_UNIT,
        dispatch_anchor="redmine:#12473#journal-64052",
        apply=False,
        dry_run=False,
        as_json=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class CmdStampTest(unittest.TestCase):
    def _run(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_handoff_grandchild_stamp(args)
        return rc, buf.getvalue()

    def test_preview_default_no_tmux_write(self) -> None:
        with mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux"
        ) as run, mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.require_tmux"
        ):
            rc, out = self._run(_stamp_args())
        self.assertEqual(0, rc)
        run.assert_not_called()
        self.assertIn("(dry-run)", out)
        self.assertIn("## Grandchild lane realization", out)
        self.assertIn("preview (no tmux mutation)", out)
        self.assertIn("set-option -p -t %16 @mozyo_lane_kind implementation", out)

    def test_apply_writes_each_option(self) -> None:
        with mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux",
            return_value=types.SimpleNamespace(returncode=0),
        ) as run, mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.require_tmux"
        ):
            rc, out = self._run(_stamp_args(apply=True))
        self.assertEqual(0, rc)
        # 2 panes x 2 options.
        self.assertEqual(4, run.call_count)
        self.assertIn("stamp_result: applied", out)

    def test_apply_partial_when_a_write_fails(self) -> None:
        def _flaky(*argv, check=True):
            rc = 0 if argv[3] != "%16" else 1
            return types.SimpleNamespace(returncode=rc)

        with mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux", side_effect=_flaky
        ), mock.patch("mozyo_bridge.infrastructure.tmux_client.require_tmux"):
            rc, out = self._run(_stamp_args(apply=True))
        self.assertEqual(0, rc)
        self.assertIn("partial", out)

    def test_dry_run_wins_over_apply(self) -> None:
        with mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux"
        ) as run, mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.require_tmux"
        ):
            rc, out = self._run(_stamp_args(apply=True, dry_run=True))
        self.assertEqual(0, rc)
        run.assert_not_called()
        self.assertIn("preview (no tmux mutation)", out)

    def test_grandchild_without_pane_fails_closed(self) -> None:
        # The CLI must reject a grandchild lane declared with no pane= (j#64105):
        # a realization record without a live grandchild breadcrumb is not a PASS.
        args = _stamp_args(
            lane=[
                f"kind=coordinator,unit={PARENT_UNIT},parent=-",
                f"kind=delegated_coordinator,unit={DELEG_UNIT},parent={PARENT_UNIT},pane=%15",
                f"kind=implementation,unit={GC_UNIT},parent={DELEG_UNIT}",
            ],
            realization=REALIZATION_LAUNCH,
            adopt_reason=None,
        )
        with self.assertRaises(SystemExit):
            self._run(args)

    def test_json_surface(self) -> None:
        rc, out = self._run(_stamp_args(as_json=True))
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual("adopt", payload["realization"])
        self.assertEqual(2, payload["delegation_depth"])
        self.assertEqual(DELEG_UNIT, payload["delegation_parent"])
        self.assertEqual(["%15", "%16"], payload["stamped_panes"])
        self.assertFalse(payload["applied"])
        self.assertEqual(4, len(payload["plan"]))


class ParserRegistrationTest(unittest.TestCase):
    def test_subcommand_registered(self) -> None:
        parser = build_parser()
        ns = parser.parse_args([
            "handoff", "delegate-grandchild-stamp",
            "--lane", f"kind=implementation,unit={GC_UNIT},parent={DELEG_UNIT},pane=%16",
            "--grandchild-unit", GC_UNIT,
            "--realization", "launch",
        ])
        self.assertEqual("cmd_handoff_grandchild_stamp", ns.func.__name__)
        self.assertEqual("launch", ns.realization)


class Issue12460RegressionTest(unittest.TestCase):
    """A decision / same-lane worker only is NOT a full display PASS (#12460).

    The display columns stay blank until the stamp actuator writes the
    projection-cache options; only then does `agents targets` /
    `delegation_display` project the grandchild lane as KIND=implementation /
    DEPTH=2 / PARENT=<delegated coordinator lane>.
    """

    def _cand(self, pane_id, *, lane_id, workspace_id, lane_kind="",
              delegation_parent=""):
        from mozyo_bridge.domain.agent_discovery import TargetCandidate

        return TargetCandidate(
            pane_id=pane_id, role="codex", role_source="pane_option",
            confidence="strong", ambiguous=False, session="mozyo-cockpit",
            window_name="cockpit", window_index="0", pane_index="0", active=True,
            workspace_id=workspace_id, workspace_label="mozyo-bridge",
            lane_id=lane_id, lane_label=None, repo_short="repo",
            repo_root="/work/repo", cwd="/work/repo", host="local",
            view_kind="cockpit_pane", branch="main", lane_kind=lane_kind,
            delegation_parent=delegation_parent,
        )

    def _unstamped_chain_candidates(self):
        # Three discovered panes with NO delegation facts (the post-dispatch,
        # pre-stamp state): grandchild dispatch decided + a worker exists, but no
        # @mozyo_* options were written.
        return [
            self._cand("%14", lane_id="lane-parent", workspace_id="gk"),
            self._cand("%15", lane_id="lane-deleg", workspace_id="mozyo"),
            self._cand("%16", lane_id="lane-grandchild", workspace_id="mozyo"),
        ]

    def _apply_plan_to_candidates(self, plan):
        # Simulate the live tmux stamp: fold the plan's set-option writes into a
        # {pane: {option: value}} map and rebuild the candidates carrying them.
        stamped: dict[str, dict[str, str]] = {}
        for argv in plan.commands:
            _, _, _, pane, option, value = argv
            stamped.setdefault(pane, {})[option] = value
        unit_to_ids = {
            "%14": ("lane-parent", "gk"),
            "%15": ("lane-deleg", "mozyo"),
            "%16": ("lane-grandchild", "mozyo"),
        }
        out = []
        for pane, (lane_id, ws) in unit_to_ids.items():
            opts = stamped.get(pane, {})
            out.append(self._cand(
                pane, lane_id=lane_id, workspace_id=ws,
                lane_kind=opts.get(OPTION_LANE_KIND, ""),
                delegation_parent=opts.get(OPTION_DELEGATION_PARENT, ""),
            ))
        return out

    def test_unstamped_grandchild_shows_blank_columns(self) -> None:
        from mozyo_bridge.domain.delegation_display import (
            delegation_cells,
            derive_targets_delegation,
        )

        display = derive_targets_delegation(self._unstamped_chain_candidates())
        # The grandchild pane has no delegation fact -> blank KIND/DEPTH/PARENT.
        self.assertEqual("none", display["%16"].status)
        self.assertEqual(("-", "-", "-"), delegation_cells(display["%16"]))

    def test_stamp_makes_grandchild_show_kind_depth_parent(self) -> None:
        from mozyo_bridge.domain.delegation_display import (
            delegation_cells,
            derive_targets_delegation,
        )

        # The stamp actuator's plan, for the same chain, stamps every lane.
        lanes = [
            DeclaredLane(unit_id="gk/lane-parent", lane_kind="coordinator",
                         panes=("%14",)),
            DeclaredLane(unit_id="mozyo/lane-deleg",
                         lane_kind="delegated_coordinator",
                         delegation_parent="gk/lane-parent", panes=("%15",)),
            DeclaredLane(unit_id="mozyo/lane-grandchild",
                         lane_kind="implementation",
                         delegation_parent="mozyo/lane-deleg", panes=("%16",)),
        ]
        plan = resolve_grandchild_stamp_plan(
            lanes, grandchild_unit="mozyo/lane-grandchild",
            realization=REALIZATION_ADOPT, adopt_reason="adopted same-lane worker",
        )
        stamped_candidates = self._apply_plan_to_candidates(plan)
        display = derive_targets_delegation(stamped_candidates)

        # Now the grandchild projects the full delegated-tree breadcrumb.
        kind, depth, parent = delegation_cells(display["%16"])
        self.assertEqual("implementation", kind)
        self.assertEqual("2", depth)
        self.assertEqual("mozyo/lane-deleg", parent)
        self.assertEqual("derived", display["%16"].status)


class RealizationGateTest(unittest.TestCase):
    """The realize-or-blocked gate (#12473 j#64151 / #12474 QA)."""

    def test_not_required_is_same_lane_ok(self) -> None:
        r = evaluate_grandchild_realization_gate(
            grandchild_required=False, realized_grandchild_unit=None
        )
        self.assertEqual(GATE_SAME_LANE_OK, r.verdict)
        self.assertFalse(r.is_blocked)

    def test_required_and_realized(self) -> None:
        r = evaluate_grandchild_realization_gate(
            grandchild_required=True, realized_grandchild_unit="mozyo/gc"
        )
        self.assertEqual(GATE_REALIZED, r.verdict)
        self.assertTrue(r.is_realized)
        self.assertEqual("mozyo/gc", r.realized_grandchild_unit)

    def test_required_and_not_realized_is_blocked(self) -> None:
        # The #12474 failure shape: grandchild required, none realized.
        r = evaluate_grandchild_realization_gate(
            grandchild_required=True, realized_grandchild_unit=None
        )
        self.assertEqual(GATE_BLOCKED, r.verdict)
        self.assertTrue(r.is_blocked)
        self.assertIn("grandchild_required_but_not_realized", r.reason)


class FindRealizedGrandchildTest(unittest.TestCase):
    def _rows(self):
        return [
            ("gk/p", "coordinator", 0, None, "derived"),
            ("mozyo/d", "delegated_coordinator", 1, "gk/p", "derived"),
            ("mozyo/gc", "implementation", 2, "mozyo/d", "derived"),
        ]

    def test_finds_realized_grandchild(self) -> None:
        self.assertEqual(
            "mozyo/gc",
            find_realized_grandchild_unit(self._rows(), delegated_coordinator_unit="mozyo/d"),
        )

    def test_wrong_parent_no_match(self) -> None:
        self.assertIsNone(
            find_realized_grandchild_unit(self._rows(), delegated_coordinator_unit="other/x")
        )

    def test_diagnostic_status_no_match(self) -> None:
        rows = [("mozyo/gc", "implementation", 2, "mozyo/d", "diagnostic")]
        self.assertIsNone(
            find_realized_grandchild_unit(rows, delegated_coordinator_unit="mozyo/d")
        )

    def test_wrong_depth_no_match(self) -> None:
        # A same-lane worker masquerading at depth 1 is not a realized grandchild.
        rows = [("mozyo/gc", "implementation", 1, "mozyo/d", "derived")]
        self.assertIsNone(
            find_realized_grandchild_unit(rows, delegated_coordinator_unit="mozyo/d")
        )

    def test_none_depth_no_match(self) -> None:
        rows = [("mozyo/gc", "implementation", None, "mozyo/d", "derived")]
        self.assertIsNone(
            find_realized_grandchild_unit(rows, delegated_coordinator_unit="mozyo/d")
        )


def _gate_args(**over) -> argparse.Namespace:
    base = dict(
        delegated_coordinator_unit="mozyo/d",
        require_grandchild=True,
        parent_issue="12454",
        child_issue="12484",
        session=None,
        as_json=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class CmdGateTest(unittest.TestCase):
    _PATCH = "mozyo_bridge.application.grandchild_stamp._discover_delegation_units"

    def _run(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_handoff_grandchild_gate(args)
        return rc, buf.getvalue()

    def test_blocked_when_required_and_no_grandchild(self) -> None:
        # Exactly the #12474 live shape: only delegated coordinator, no grandchild.
        rows = [("mozyo/d", "delegated_coordinator", 1, "gk/p", "derived")]
        with mock.patch(self._PATCH, return_value=rows):
            rc, out = self._run(_gate_args())
        self.assertEqual(3, rc)
        self.assertIn("verdict: blocked", out)
        self.assertIn("## Grandchild realization gate", out)
        self.assertIn("remediation:", out)

    def test_realized_when_grandchild_present(self) -> None:
        rows = [
            ("mozyo/d", "delegated_coordinator", 1, "gk/p", "derived"),
            ("mozyo/gc", "implementation", 2, "mozyo/d", "derived"),
        ]
        with mock.patch(self._PATCH, return_value=rows):
            rc, out = self._run(_gate_args())
        self.assertEqual(0, rc)
        self.assertIn("verdict: realized", out)

    def test_same_lane_ok_when_not_required(self) -> None:
        with mock.patch(self._PATCH, return_value=[]):
            rc, out = self._run(_gate_args(require_grandchild=False))
        self.assertEqual(0, rc)
        self.assertIn("verdict: same_lane_ok", out)

    def test_json_surface(self) -> None:
        with mock.patch(self._PATCH, return_value=[]):
            rc, out = self._run(_gate_args(as_json=True))
        self.assertEqual(3, rc)
        payload = json.loads(out)
        self.assertEqual("blocked", payload["verdict"])
        self.assertTrue(payload["blocked"])
        self.assertIsNone(payload["realized_grandchild_unit"])


class GateParserRegistrationTest(unittest.TestCase):
    def test_gate_subcommand_registered(self) -> None:
        parser = build_parser()
        ns = parser.parse_args([
            "handoff", "delegate-grandchild-gate",
            "--delegated-coordinator-unit", "mozyo/d",
            "--no-require-grandchild",
        ])
        self.assertEqual("cmd_handoff_grandchild_gate", ns.func.__name__)
        self.assertFalse(ns.require_grandchild)


if __name__ == "__main__":
    unittest.main()
