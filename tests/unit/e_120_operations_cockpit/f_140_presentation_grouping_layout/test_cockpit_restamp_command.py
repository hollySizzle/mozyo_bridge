"""Cockpit lane-identity restamp boundary — fake-port use case tests (#13160).

Pins the ``mozyo cockpit restamp`` re-derivation path: recompute each in-scope
cockpit pane's lane identity from its authoritative ``@mozyo_repo_root``, diff
against the stamped ``@mozyo_lane_id`` / ``@mozyo_lane_label``, and re-apply
``set-option`` ONLY to the drifted panes. Everything runs against a fake
:class:`CockpitRestampOps` port (no tmux, no monkeypatch). Synthetic, neutral
identifiers only.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_restamp_command import (
    CockpitRestampOps,
    CockpitRestampUseCase,
    LiveCockpitRestampOps,
    build_restamp_plan,
    project_restamp_panes,
    render_restamp_lines,
    restamp_payload,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    LaneIdentity,
)


_WS = "wsMain"


def _pane(pane_id, workspace_id=_WS, lane_id="default", lane_label="", repo_root="/checkout/main"):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "lane_label": lane_label,
        "repo_root": repo_root,
    }


class _Recompute:
    """Deterministic recompute stub keyed by ``repo_root``."""

    def __init__(self, table):
        # table: repo_root -> LaneIdentity
        self._table = table
        self.calls = []

    def __call__(self, repo_root, workspace_id):
        self.calls.append((repo_root, workspace_id))
        return self._table[repo_root]


class FakeRestampOps:
    """Recording :class:`CockpitRestampOps` fake — no tmux."""

    def __init__(self, *, panes, recompute):
        self._panes = panes
        self._recompute = recompute
        self.emitted: list[str] = []
        self.applied: list[tuple] = []
        self.require_tmux_calls = 0

    def read_panes(self, session):
        return self._panes

    def recompute_lane(self, repo_root, workspace_id):
        return self._recompute(repo_root, workspace_id)

    def require_tmux(self):
        self.require_tmux_calls += 1

    def apply_command(self, argv):
        self.applied.append(tuple(argv))

    def emit(self, text):
        self.emitted.append(text)


class PortContractTest(unittest.TestCase):
    def test_live_and_fake_satisfy_port(self) -> None:
        self.assertIsInstance(LiveCockpitRestampOps(), CockpitRestampOps)
        self.assertIsInstance(
            FakeRestampOps(panes=[], recompute=lambda r, w: None), CockpitRestampOps
        )


class ProjectRestampPanesTest(unittest.TestCase):
    def test_parses_five_fields(self) -> None:
        stdout = "%1\twsMain\tlane-abc123def456\tfeat\t/checkout/main\n"
        self.assertEqual(
            [
                {
                    "pane_id": "%1",
                    "workspace_id": "wsMain",
                    "lane_id": "lane-abc123def456",
                    "lane_label": "feat",
                    "repo_root": "/checkout/main",
                }
            ],
            project_restamp_panes(stdout),
        )

    def test_short_line_right_pads_and_skips_id_less_rows(self) -> None:
        stdout = "%2\twsMain\n\t\t\t\t\n"
        self.assertEqual(
            [
                {
                    "pane_id": "%2",
                    "workspace_id": "wsMain",
                    "lane_id": "",
                    "lane_label": "",
                    "repo_root": "",
                }
            ],
            project_restamp_panes(stdout),
        )


class BuildRestampPlanTest(unittest.TestCase):
    def test_polluted_main_checkout_pane_drifts_to_default(self) -> None:
        # The #13152 scenario: a main-checkout pane hashed to a lane during the
        # registry pollution; the recompute now yields the `default` lane.
        panes = [_pane("%1", lane_id="lane-deadbeef0000", lane_label="issue_x")]
        recompute = _Recompute({"/checkout/main": LaneIdentity("default", None)})
        plan = build_restamp_plan(
            panes, session="mozyo-cockpit", workspace_id=_WS, recompute=recompute
        )
        self.assertEqual(1, plan.considered)
        self.assertEqual(1, len(plan.drifts))
        drift = plan.drifts[0]
        self.assertEqual("lane-deadbeef0000", drift.stamped_lane_id)
        self.assertEqual("default", drift.recomputed_lane_id)
        # lane_id restamp + a label *unset* (recomputed label is empty).
        self.assertEqual(
            (
                ("set-option", "-p", "-t", "%1", "@mozyo_lane_id", "default"),
                ("set-option", "-p", "-u", "-t", "%1", "@mozyo_lane_label"),
            ),
            drift.commands,
        )

    def test_in_sync_pane_yields_no_drift(self) -> None:
        panes = [_pane("%1", lane_id="default", lane_label="")]
        recompute = _Recompute({"/checkout/main": LaneIdentity("default", None)})
        plan = build_restamp_plan(
            panes, session="mozyo-cockpit", workspace_id=_WS, recompute=recompute
        )
        self.assertEqual(1, plan.considered)
        self.assertEqual((), plan.drifts)
        self.assertFalse(plan.would_apply)

    def test_different_workspace_and_mozyo_less_panes_are_out_of_scope(self) -> None:
        panes = [
            _pane("%1", workspace_id="wsOther", lane_id="lane-x", repo_root="/other"),
            _pane("%2", workspace_id="", repo_root=""),
            _pane("%3", workspace_id=_WS, repo_root=""),  # no root to recompute
        ]
        recompute = _Recompute({"/other": LaneIdentity("default", None)})
        plan = build_restamp_plan(
            panes, session="mozyo-cockpit", workspace_id=_WS, recompute=recompute
        )
        self.assertEqual(0, plan.considered)
        self.assertEqual((), plan.drifts)
        # The out-of-scope panes were never recomputed.
        self.assertEqual([], recompute.calls)

    def test_worktree_pane_relabelled_sets_label(self) -> None:
        panes = [_pane("%1", lane_id="default", lane_label="", repo_root="/wt/a")]
        recompute = _Recompute({"/wt/a": LaneIdentity("lane-abc123", "feature-a")})
        plan = build_restamp_plan(
            panes, session="mozyo-cockpit", workspace_id=_WS, recompute=recompute
        )
        drift = plan.drifts[0]
        self.assertEqual(
            (
                ("set-option", "-p", "-t", "%1", "@mozyo_lane_id", "lane-abc123"),
                ("set-option", "-p", "-t", "%1", "@mozyo_lane_label", "feature-a"),
            ),
            drift.commands,
        )


class RenderAndPayloadTest(unittest.TestCase):
    def _drift_plan(self):
        panes = [_pane("%1", lane_id="lane-old0000", lane_label="old")]
        recompute = _Recompute({"/checkout/main": LaneIdentity("default", None)})
        return build_restamp_plan(
            panes, session="mozyo-cockpit", workspace_id=_WS, recompute=recompute
        )

    def test_dry_run_render_shows_diff_and_apply_hint(self) -> None:
        lines = render_restamp_lines(self._drift_plan(), dry_run=True, applied=False)
        text = "\n".join(lines)
        self.assertIn("cockpit restamp: session=mozyo-cockpit workspace=wsMain", text)
        self.assertIn("lane_id 'lane-old0000' -> 'default'", text)
        self.assertIn("    tmux set-option -p -t %1 @mozyo_lane_id default", text)
        self.assertIn("run `mozyo cockpit restamp` (without --dry-run) to apply.", text)

    def test_applied_render_reports_count(self) -> None:
        lines = render_restamp_lines(self._drift_plan(), dry_run=False, applied=True)
        self.assertIn("  restamped 1 pane(s).", lines)

    def test_no_drift_render_is_nothing_to_restamp(self) -> None:
        panes = [_pane("%1", lane_id="default", lane_label="")]
        recompute = _Recompute({"/checkout/main": LaneIdentity("default", None)})
        plan = build_restamp_plan(
            panes, session="mozyo-cockpit", workspace_id=_WS, recompute=recompute
        )
        lines = render_restamp_lines(plan, dry_run=False, applied=False)
        self.assertIn("nothing to restamp", "\n".join(lines))

    def test_payload_shape(self) -> None:
        payload = restamp_payload(
            self._drift_plan(), present=True, applied=True, dry_run=False
        )
        self.assertEqual("cockpit restamp", payload["command"])
        self.assertEqual("wsMain", payload["workspace_id"])
        self.assertTrue(payload["cockpit_present"])
        self.assertTrue(payload["applied"])
        self.assertEqual(1, payload["considered"])
        self.assertEqual(1, payload["drift_count"])
        drift = payload["drifts"][0]
        self.assertEqual("%1", drift["pane_id"])
        self.assertEqual("lane-old0000", drift["stamped"]["lane_id"])
        self.assertEqual("default", drift["recomputed"]["lane_id"])
        self.assertEqual(
            ["set-option", "-p", "-t", "%1", "@mozyo_lane_id", "default"],
            drift["commands"][0],
        )


class HandleRestampUseCaseTest(unittest.TestCase):
    def _ops(self, *, panes, table):
        return FakeRestampOps(panes=panes, recompute=_Recompute(table))

    def _handle(self, ops, *, json_output=False, dry_run=False):
        rc = CockpitRestampUseCase(ops).handle(
            "mozyo-cockpit", _WS, json_output=json_output, dry_run=dry_run
        )
        return rc, ops

    def test_polluted_pane_is_restamped_to_default(self) -> None:
        ops = self._ops(
            panes=[_pane("%1", lane_id="lane-poison00", lane_label="issue_x")],
            table={"/checkout/main": LaneIdentity("default", None)},
        )
        rc, ops = self._handle(ops)
        self.assertEqual(0, rc)
        self.assertEqual(1, ops.require_tmux_calls)
        self.assertEqual(
            [
                ("set-option", "-p", "-t", "%1", "@mozyo_lane_id", "default"),
                ("set-option", "-p", "-u", "-t", "%1", "@mozyo_lane_label"),
            ],
            ops.applied,
        )
        self.assertIn("restamped 1 pane(s).", "\n".join(ops.emitted))

    def test_in_sync_pane_issues_no_set_option(self) -> None:
        ops = self._ops(
            panes=[_pane("%1", lane_id="default", lane_label="")],
            table={"/checkout/main": LaneIdentity("default", None)},
        )
        rc, ops = self._handle(ops)
        self.assertEqual(0, rc)
        self.assertEqual([], ops.applied)
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertIn("nothing to restamp", "\n".join(ops.emitted))

    def test_dry_run_shows_diff_but_issues_no_set_option(self) -> None:
        ops = self._ops(
            panes=[_pane("%1", lane_id="lane-poison00", lane_label="issue_x")],
            table={"/checkout/main": LaneIdentity("default", None)},
        )
        rc, ops = self._handle(ops, dry_run=True)
        self.assertEqual(0, rc)
        self.assertEqual([], ops.applied)
        self.assertEqual(0, ops.require_tmux_calls)
        text = "\n".join(ops.emitted)
        self.assertIn("lane_id 'lane-poison00' -> 'default'", text)
        self.assertIn("without --dry-run", text)

    def test_different_workspace_pane_is_untouched(self) -> None:
        ops = self._ops(
            panes=[
                _pane("%9", workspace_id="wsOther", lane_id="lane-x", repo_root="/o"),
            ],
            table={"/o": LaneIdentity("default", None)},
        )
        rc, ops = self._handle(ops)
        self.assertEqual(0, rc)
        self.assertEqual([], ops.applied)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_json_is_single_parseable_preview_document(self) -> None:
        ops = self._ops(
            panes=[_pane("%1", lane_id="lane-poison00", lane_label="issue_x")],
            table={"/checkout/main": LaneIdentity("default", None)},
        )
        rc, ops = self._handle(ops, json_output=True)
        self.assertEqual(0, rc)
        # --json is preview-only: never applies.
        self.assertEqual([], ops.applied)
        self.assertEqual(0, ops.require_tmux_calls)
        payload = json.loads(ops.emitted[0])
        self.assertFalse(payload["applied"])
        self.assertEqual(1, payload["drift_count"])
        self.assertEqual("cockpit restamp", payload["command"])

    def test_absent_cockpit_is_benign_noop(self) -> None:
        ops = self._ops(panes=None, table={})
        rc, ops = self._handle(ops)
        self.assertEqual(0, rc)
        self.assertEqual([], ops.applied)
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertIn("no cockpit window", "\n".join(ops.emitted))


if __name__ == "__main__":
    unittest.main()
