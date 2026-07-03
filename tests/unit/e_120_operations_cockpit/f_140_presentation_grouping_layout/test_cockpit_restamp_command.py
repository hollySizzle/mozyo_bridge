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


class _Result:
    """Minimal ``run_tmux``-style result the use case inspects."""

    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class FakeRestampOps:
    """Recording :class:`CockpitRestampOps` fake — no tmux."""

    def __init__(
        self, *, panes, recompute, fail_pane=None, fail_option=None, fail_stderr="boom"
    ):
        self._panes = panes
        self._recompute = recompute
        self._fail_pane = fail_pane
        # When set, only the ``set-option`` touching this option fails (so a pane
        # with two commands can fail on its SECOND one — the REV3 mid-pane case).
        self._fail_option = fail_option
        self._fail_stderr = fail_stderr
        self.emitted: list[str] = []
        self.applied: list[tuple] = []
        self.died: list[str] = []
        self.require_tmux_calls = 0

    def read_panes(self, session):
        return self._panes

    def recompute_lane(self, repo_root, workspace_id):
        return self._recompute(repo_root, workspace_id)

    def require_tmux(self):
        self.require_tmux_calls += 1

    def apply_command(self, argv):
        self.applied.append(tuple(argv))
        pane = argv[argv.index("-t") + 1]
        option = argv[-1] if "-u" in argv else argv[-2]
        if self._fail_pane is not None and pane == self._fail_pane:
            if self._fail_option is None or option == self._fail_option:
                return _Result(returncode=1, stderr=self._fail_stderr)
        return _Result(returncode=0)

    def die(self, message):
        self.died.append(message)
        raise SystemExit(2)

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

    def test_apply_failure_fails_closed_with_partial_detail(self) -> None:
        # Two drifted panes; the set-option on the SECOND one fails.
        ops = FakeRestampOps(
            panes=[
                _pane("%1", lane_id="lane-old1", lane_label=""),
                _pane("%2", lane_id="lane-old2", lane_label="", repo_root="/checkout/two"),
            ],
            recompute=_Recompute(
                {
                    "/checkout/main": LaneIdentity("default", None),
                    "/checkout/two": LaneIdentity("default", None),
                }
            ),
            fail_pane="%2",
            fail_stderr="tmux: pane not found",
        )
        with self.assertRaises(SystemExit) as cm:
            CockpitRestampUseCase(ops).handle(
                "mozyo-cockpit", _WS, json_output=False, dry_run=False
            )
        # (c) non-zero CLI exit.
        self.assertEqual(2, cm.exception.code)
        # (a) never reported a successful "restamped N pane(s)." line.
        self.assertNotIn("restamped 2 pane(s).", "\n".join(ops.emitted))
        # (b) the abort names the failing pane, its exact command, the detail,
        # and the half-restamp reality (pane %1 landed before %2 failed).
        self.assertEqual(1, len(ops.died))
        message = ops.died[0]
        self.assertIn("pane %2", message)
        self.assertIn("tmux set-option -p -t %2 @mozyo_lane_id default", message)
        self.assertIn("tmux: pane not found", message)
        self.assertIn("Restamped 1 of 2 pane(s) fully.", message)
        # %2's only command failed (nothing landed on it) -> not PARTIAL.
        self.assertNotIn("PARTIALLY restamped", message)
        # %1 was applied before the abort; %2's failing command was attempted.
        self.assertIn(
            ("set-option", "-p", "-t", "%1", "@mozyo_lane_id", "default"), ops.applied
        )

    def test_apply_failure_on_first_command_reports_attempted_not_partial(self) -> None:
        # Single pane, single command fails -> zero applied on it: it is
        # "attempted but left unchanged" (never PARTIAL, and never counted as
        # "not attempted" — the failed command WAS issued; #13160 REV4 / j#71854).
        ops = FakeRestampOps(
            panes=[_pane("%1", lane_id="lane-old1", lane_label="issue_x")],
            recompute=_Recompute({"/checkout/main": LaneIdentity("default", None)}),
            fail_pane="%1",
        )
        with self.assertRaises(SystemExit) as cm:
            CockpitRestampUseCase(ops).handle(
                "mozyo-cockpit", _WS, json_output=False, dry_run=False
            )
        self.assertEqual(2, cm.exception.code)
        message = ops.died[0]
        self.assertIn("Restamped 0 of 1 pane(s) fully.", message)
        self.assertNotIn("PARTIALLY restamped", message)
        self.assertIn("Pane %1 was attempted but left unchanged: '@mozyo_lane_id default' failed.", message)
        self.assertIn("0 pane(s) were left unchanged (not attempted)", message)

    def test_mid_pane_command_failure_is_reported_as_partial(self) -> None:
        # REV3 core case: a pane whose recompute yields a lane *label* emits two
        # commands (@mozyo_lane_id then @mozyo_lane_label); the SECOND fails.
        # A first, unrelated pane restamps fully before it.
        ops = FakeRestampOps(
            panes=[
                _pane("%4", lane_id="lane-old4", lane_label=""),
                _pane("%5", lane_id="default", lane_label="", repo_root="/wt/five"),
            ],
            recompute=_Recompute(
                {
                    "/checkout/main": LaneIdentity("default", None),
                    "/wt/five": LaneIdentity("lane-abc123", "feature-5"),
                }
            ),
            fail_pane="%5",
            fail_option="@mozyo_lane_label",
            fail_stderr="tmux: option write failed",
        )
        with self.assertRaises(SystemExit) as cm:
            CockpitRestampUseCase(ops).handle(
                "mozyo-cockpit", _WS, json_output=False, dry_run=False
            )
        # (iii) non-zero CLI exit.
        self.assertEqual(2, cm.exception.code)
        message = ops.died[0]
        # (ii) fully-restamped / not-attempted counts are correct: %4 fully done,
        # %5 partial, none left unattempted.
        self.assertIn("Restamped 1 of 2 pane(s) fully.", message)
        self.assertIn("0 pane(s) were left unchanged", message)
        # (i) the failing pane's partial state shows BOTH the applied command and
        # the failed command (never "left unchanged" for %5).
        self.assertIn("Pane %5 is PARTIALLY restamped", message)
        self.assertIn("'@mozyo_lane_id lane-abc123' applied", message)
        self.assertIn("'@mozyo_lane_label feature-5' failed", message)
        # The @mozyo_lane_id set landed on %5 before the label set failed.
        self.assertIn(
            ("set-option", "-p", "-t", "%5", "@mozyo_lane_id", "lane-abc123"),
            ops.applied,
        )

    def test_mid_pane_unset_label_failure_is_partial(self) -> None:
        # A polluted pane recomputes to `default` with no label: commands are
        # (@mozyo_lane_id set, @mozyo_lane_label unset); the unset fails.
        ops = FakeRestampOps(
            panes=[_pane("%6", lane_id="lane-poison", lane_label="stale")],
            recompute=_Recompute({"/checkout/main": LaneIdentity("default", None)}),
            fail_pane="%6",
            fail_option="@mozyo_lane_label",
        )
        with self.assertRaises(SystemExit) as cm:
            CockpitRestampUseCase(ops).handle(
                "mozyo-cockpit", _WS, json_output=False, dry_run=False
            )
        self.assertEqual(2, cm.exception.code)
        message = ops.died[0]
        self.assertIn("Pane %6 is PARTIALLY restamped", message)
        self.assertIn("'@mozyo_lane_id default' applied", message)
        self.assertIn("'@mozyo_lane_label (unset)' failed", message)


if __name__ == "__main__":
    unittest.main()
