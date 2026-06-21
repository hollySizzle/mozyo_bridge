"""Cockpit membership status / list commands (Redmine #12341).

`mozyo cockpit list` / `cockpit status --repo <repo>` give an operator a one-shot
answer to "is this workspace loaded in the cockpit, are its Codex/Claude panes
present, is the geometry healthy?" — the #12339 mis-read this US closes. These
tests cover the pure projection (`domain.cockpit_membership`) and the read-only
CLI handlers (`cmd_cockpit` `list` / `status`, plus the `status` membership line).
Every test stubs tmux + the registry, so it is hermetic (no live tmux, no
destructive operations).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain import cockpit_membership as membership
from mozyo_bridge.domain.cockpit_geometry import diagnose_cockpit_geometry


def _facts(label="alpha", repo="/repo/alpha", reg=True, anchor=True):
    return membership.RegistryFacts(
        label=label, repo_root=repo, registry_present=reg, anchor_present=anchor
    )


def _obs(ws="wsA", lane="default", codex="%99", claude="%100", window="cockpit", wid="@1"):
    return membership.MembershipObservation(
        workspace_id=ws,
        lane_id=lane,
        lane_label="",
        codex_pane=codex,
        claude_pane=claude,
        window=window,
        window_id=wid,
    )


def _healthy_geometry(session="mozyo-cockpit"):
    return diagnose_cockpit_geometry(
        session=session,
        panes=[
            {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "pane_left": 0, "pane_top": 0,
             "pane_width": 80, "pane_height": 40},
            {"pane_id": "%100", "workspace_id": "wsA", "role": "claude",
             "lane_id": "default", "pane_left": 0, "pane_top": 40,
             "pane_width": 80, "pane_height": 20},
        ],
    )


class MembershipProjectionTest(unittest.TestCase):
    """The pure `domain.cockpit_membership` projection."""

    def test_loaded_healthy_workspace_is_member_ok(self) -> None:
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs()],
            facts_by_workspace={"wsA": _facts()},
            geometry=_healthy_geometry(),
        )
        self.assertEqual(1, len(report.workspaces))
        ws = report.workspaces[0]
        self.assertTrue(ws.member)
        self.assertTrue(ws.panes_present)
        self.assertEqual(membership.GEOM_OK, ws.geometry_status)
        self.assertEqual("%99", ws.codex_pane)
        self.assertEqual("%100", ws.claude_pane)
        self.assertEqual("alpha", ws.label)
        self.assertEqual("/repo/alpha", ws.repo_root)
        self.assertEqual((), ws.warnings)
        self.assertTrue(ws.ok)
        self.assertTrue(report.ok)

    def test_required_fields_in_json(self) -> None:
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs()],
            facts_by_workspace={"wsA": _facts()},
            geometry=_healthy_geometry(),
        )
        ws = report.as_dict()["workspaces"][0]
        # Acceptance: each of these fields must be present for UI / tests.
        for key in (
            "workspace_id", "label", "repo_root", "lane_id", "session",
            "window", "codex_pane", "claude_pane", "geometry_status",
            "registry_present", "anchor_present", "member",
        ):
            self.assertIn(key, ws, key)

    def test_unregistered_workspace_splits_warnings_but_stays_member(self) -> None:
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs()],
            facts_by_workspace={},  # nothing resolved -> unresolved facts
            geometry=_healthy_geometry(),
        )
        ws = report.workspaces[0]
        self.assertTrue(ws.member)  # still loaded
        self.assertFalse(ws.registry_present)
        self.assertFalse(ws.anchor_present)
        codes = {w.code for w in ws.warnings}
        self.assertIn(membership.WARN_NOT_REGISTERED, codes)
        self.assertIn(membership.WARN_ANCHOR_ABSENT, codes)
        # Label falls back to the workspace id when the registry has no record.
        self.assertEqual("wsA", ws.label)

    def test_group_window_unit_is_geometry_unknown_but_ok(self) -> None:
        # A Unit in a Project-Group window (#12330) is not covered by the
        # cockpit-window geometry diagnosis -> `unknown`, but still ok (loaded).
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs(ws="wsB", window="proj", wid="@9",
                               codex="%200", claude="%201")],
            facts_by_workspace={"wsB": _facts(label="beta", repo="/repo/beta")},
            geometry=_healthy_geometry(),  # only knows wsA
        )
        ws = report.workspaces[0]
        self.assertEqual(membership.GEOM_UNKNOWN, ws.geometry_status)
        self.assertTrue(ws.ok)

    def test_missing_peer_in_group_window_is_warning(self) -> None:
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs(ws="wsC", window="proj", wid="@9",
                               codex="%300", claude="")],
            facts_by_workspace={"wsC": _facts()},
            geometry=None,
        )
        ws = report.workspaces[0]
        self.assertFalse(ws.panes_present)
        self.assertEqual(membership.GEOM_WARNING, ws.geometry_status)
        self.assertFalse(ws.ok)
        self.assertIn(
            membership.WARN_MISSING_PEER, {w.code for w in ws.warnings}
        )

    def test_cockpit_window_missing_peer_uses_geometry_finding(self) -> None:
        # A codex-only Unit in the cockpit window: the geometry diagnosis emits a
        # warning finding that the membership view surfaces (not the derived one).
        geo = diagnose_cockpit_geometry(
            session="mozyo-cockpit",
            panes=[{"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
                    "lane_id": "default", "pane_left": 0, "pane_top": 0,
                    "pane_width": 80, "pane_height": 40}],
        )
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs(claude="")],
            facts_by_workspace={"wsA": _facts()},
            geometry=geo,
        )
        ws = report.workspaces[0]
        self.assertEqual(membership.GEOM_WARNING, ws.geometry_status)
        self.assertIn("missing_claude", {w.code for w in ws.warnings})

    def test_role_less_pane_is_report_level_warning(self) -> None:
        geo = diagnose_cockpit_geometry(
            session="mozyo-cockpit",
            panes=[
                {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
                 "lane_id": "default", "pane_left": 0, "pane_top": 0,
                 "pane_width": 80, "pane_height": 40},
                {"pane_id": "%100", "workspace_id": "wsA", "role": "claude",
                 "lane_id": "default", "pane_left": 0, "pane_top": 40,
                 "pane_width": 80, "pane_height": 20},
                # A role-less pane: no workspace/role markers.
                {"pane_id": "%101", "workspace_id": "", "role": "",
                 "lane_id": "", "pane_left": 80, "pane_top": 0,
                 "pane_width": 80, "pane_height": 60},
            ],
        )
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs()],
            facts_by_workspace={"wsA": _facts()},
            geometry=geo,
        )
        codes = {w.code for w in report.warnings}
        self.assertIn(membership.WARN_ROLE_LESS_PANE, codes)
        self.assertFalse(report.ok)

    def test_absent_membership_says_not_loaded(self) -> None:
        ws = membership.absent_membership(
            session="mozyo-cockpit",
            workspace_id="wsZ",
            label="zeta",
            repo_root="/repo/zeta",
            lane_id="default",
            lane_label="",
            registry_present=True,
            anchor_present=True,
        )
        self.assertFalse(ws.member)
        self.assertEqual(membership.GEOM_ABSENT, ws.geometry_status)
        self.assertFalse(ws.ok)
        self.assertIn(membership.WARN_NOT_LOADED, {w.code for w in ws.warnings})

    def test_text_carries_projection_note(self) -> None:
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[_obs()],
            facts_by_workspace={"wsA": _facts()},
            geometry=_healthy_geometry(),
        )
        text = membership.format_membership_text(report)
        self.assertIn("display/liveness projection", text)
        self.assertIn("not Redmine workflow", text)
        self.assertIn("wsA", text)

    def test_workspaces_sorted_by_label(self) -> None:
        report = membership.project_membership_report(
            session="mozyo-cockpit",
            cockpit_present=True,
            observations=[
                _obs(ws="wsB", codex="%1", claude="%2"),
                _obs(ws="wsA", codex="%3", claude="%4"),
            ],
            facts_by_workspace={
                "wsB": _facts(label="zeta"),
                "wsA": _facts(label="alpha"),
            },
            geometry=None,
        )
        self.assertEqual(
            ["alpha", "zeta"], [w.label for w in report.workspaces]
        )


class CockpitListStatusCliTest(unittest.TestCase):
    """The read-only `cmd_cockpit` `list` / `status` handlers (hermetic)."""

    def _args(self, **over):
        base = dict(
            action=None, repo=None, codex_ratio=70, cockpit_session=None,
            dry_run=False, json_output=False, no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, windows, geo_panes, facts):
        from mozyo_bridge.application import commands

        def fake_facts(workspace_id):
            return facts.get(workspace_id) or membership.RegistryFacts.unresolved(
                workspace_id
            )

        with patch.object(commands, "_read_managed_cockpit_windows",
                          return_value=windows), \
            patch.object(commands, "_read_cockpit_geometry",
                         return_value=geo_panes), \
            patch.object(commands, "_resolve_registry_facts",
                         side_effect=fake_facts):
            yield commands

    def _cockpit_window(self):
        return [{
            "window_id": "@1", "window": "cockpit", "group_id": "",
            "columns": [
                {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
                 "lane_id": "default", "pane_left": 0, "pane_width": 80},
                {"pane_id": "%100", "workspace_id": "wsA", "role": "claude",
                 "lane_id": "default", "pane_left": 0, "pane_width": 80},
            ],
        }]

    def _geo_panes(self):
        return [
            {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "pane_left": 0, "pane_top": 0,
             "pane_width": 80, "pane_height": 40},
            {"pane_id": "%100", "workspace_id": "wsA", "role": "claude",
             "lane_id": "default", "pane_left": 0, "pane_top": 40,
             "pane_width": 80, "pane_height": 20},
        ]

    def _run(self, commands, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = commands.cmd_cockpit(args)
        return rc, out.getvalue()

    def test_list_text(self) -> None:
        with self._patched(
            windows=self._cockpit_window(),
            geo_panes=self._geo_panes(),
            facts={"wsA": _facts(label="alpha", repo="/repo/alpha")},
        ) as commands:
            rc, out = self._run(commands, self._args(action="list"))
        self.assertEqual(0, rc)
        self.assertIn("alpha", out)
        self.assertIn("%99", out)
        self.assertIn("%100", out)
        self.assertIn("display/liveness projection", out)

    def test_list_json(self) -> None:
        with self._patched(
            windows=self._cockpit_window(),
            geo_panes=self._geo_panes(),
            facts={"wsA": _facts(label="alpha", repo="/repo/alpha")},
        ) as commands:
            rc, out = self._run(commands, self._args(action="list", json_output=True))
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(1, payload["workspace_count"])
        ws = payload["workspaces"][0]
        self.assertEqual("wsA", ws["workspace_id"])
        self.assertEqual("%99", ws["codex_pane"])
        self.assertEqual("ok", ws["geometry_status"])
        self.assertTrue(ws["member"])

    def test_list_no_cockpit_exits_zero(self) -> None:
        # No cockpit session running at all: still exit 0 (a valid state), and
        # say so plainly rather than aborting.
        with self._patched(windows=[], geo_panes=None, facts={}) as commands:
            rc, out = self._run(commands, self._args(action="list"))
        self.assertEqual(0, rc)
        self.assertIn("nothing loaded", out.lower())

    def test_list_present_but_empty_exits_zero(self) -> None:
        # Cockpit window exists (geometry present) but carries no managed Unit.
        with self._patched(windows=[], geo_panes=[], facts={}) as commands:
            rc, out = self._run(commands, self._args(action="list"))
        self.assertEqual(0, rc)
        self.assertIn("no workspaces", out.lower())

    def _status_args(self, repo="/repo/alpha", **over):
        return self._args(action="status", repo=repo, **over)

    @contextlib.contextmanager
    def _status_identity(self, commands, *, ws_id="wsA", name="alpha", lane_id="default"):
        from mozyo_bridge.domain.cockpit_layout import LaneIdentity

        canon = argparse.Namespace(name=name, workspace_id=ws_id)
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_resolve_workspace_lane",
                         return_value=LaneIdentity(lane_id, None)):
            yield

    def test_status_member_exit_zero(self) -> None:
        with self._patched(
            windows=self._cockpit_window(),
            geo_panes=self._geo_panes(),
            facts={"wsA": _facts(label="alpha", repo="/repo/alpha")},
        ) as commands:
            with self._status_identity(commands):
                rc, out = self._run(commands, self._status_args())
        self.assertEqual(0, rc)
        self.assertIn("alpha", out)
        self.assertIn("%99", out)

    def test_status_absent_exit_one(self) -> None:
        with self._patched(
            windows=self._cockpit_window(),
            geo_panes=self._geo_panes(),
            facts={"wsA": _facts()},
        ) as commands:
            with self._status_identity(commands, ws_id="wsZ", name="zeta"):
                rc, out = self._run(commands, self._status_args(repo="/repo/zeta"))
        self.assertEqual(1, rc)
        self.assertIn("not loaded", out.lower())

    def test_status_json_has_query_block(self) -> None:
        with self._patched(
            windows=self._cockpit_window(),
            geo_panes=self._geo_panes(),
            facts={"wsA": _facts(label="alpha", repo="/repo/alpha")},
        ) as commands:
            with self._status_identity(commands):
                rc, out = self._run(
                    commands, self._status_args(json_output=True)
                )
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertIn("query", payload)
        self.assertEqual("wsA", payload["query"]["workspace_id"])
        self.assertTrue(payload["query"]["member"])


class CockpitMembershipParserTest(unittest.TestCase):
    """`cockpit list` / `cockpit status` parse and bind to `cmd_cockpit`."""

    def setUp(self) -> None:
        from mozyo_bridge.application.cli import build_parser

        self.parser = build_parser()

    def test_list_action_parses(self) -> None:
        from mozyo_bridge.application.commands import cmd_cockpit

        args = self.parser.parse_args(["cockpit", "list", "--json"])
        self.assertEqual("list", args.action)
        self.assertTrue(args.json_output)
        self.assertIs(args.func, cmd_cockpit)

    def test_status_action_parses_with_repo(self) -> None:
        args = self.parser.parse_args(["cockpit", "status", "--repo", "/x"])
        self.assertEqual("status", args.action)
        self.assertEqual("/x", args.repo)


if __name__ == "__main__":
    unittest.main()
