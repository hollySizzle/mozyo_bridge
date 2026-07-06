"""Fake-port / pure specifications for the cockpit membership boundary (#12976).

These exercise the ``cockpit_membership_command`` use cases and pure projection
directly with synthetic ports — no real tmux server / registry. They pin:

- the pure ``build_membership_observations`` reshape (per-Unit grouping, role-less
  skip, lane normalization, and the injected ``repo_root_for`` resolution),
- ``UnitRepoRootUseCase`` (first resolvable pane cwd wins; empties skipped),
- ``RegistryFactsUseCase`` (record → facts; missing / raising reads fail closed to
  "unresolved" / anchor-absent),
- ``CockpitMembershipUseCase`` (``collect`` composition + per-workspace facts
  dedup, and the ``list`` / ``status`` outcomes' render + exit conventions).

The end-to-end behavior over the live ``commands.*`` / ``workspace_registry``
seams stays pinned by the cockpit membership characterization tests
(``test_cockpit_membership``); this file pins the boundary in isolation.
"""

from __future__ import annotations

import json
import unittest
import unittest.mock
from types import SimpleNamespace

from mozyo_bridge.application.cockpit_membership_command import (
    CockpitListOutcome,
    CockpitMembershipOps,
    CockpitMembershipUseCase,
    CockpitStatusOutcome,
    LiveCockpitMembershipOps,
    LiveRegistryFactsOps,
    LiveUnitRepoRootOps,
    RegistryFactsOps,
    RegistryFactsUseCase,
    UnitRepoRootOps,
    UnitRepoRootUseCase,
    build_membership_observations,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
    RegistryFacts,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    LaneIdentity,
)


def _cockpit_window(**over):
    base = {
        "window_id": "@1",
        "window": "cockpit",
        "group_id": "",
        "columns": [
            {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "pane_left": 0, "pane_width": 80},
            {"pane_id": "%100", "workspace_id": "wsA", "role": "claude",
             "lane_id": "default", "pane_left": 0, "pane_width": 80},
        ],
    }
    base.update(over)
    return base


def _geo_panes():
    return [
        {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
         "lane_id": "default", "pane_left": 0, "pane_top": 0,
         "pane_width": 80, "pane_height": 40},
        {"pane_id": "%100", "workspace_id": "wsA", "role": "claude",
         "lane_id": "default", "pane_left": 0, "pane_top": 40,
         "pane_width": 80, "pane_height": 20},
    ]


# --- Pure reshape. -----------------------------------------------------------


class BuildMembershipObservationsTest(unittest.TestCase):
    def test_groups_codex_and_claude_into_one_unit(self) -> None:
        obs = build_membership_observations(
            [_cockpit_window()], "s", lambda c, cl: "/repo/live"
        )
        self.assertEqual(1, len(obs))
        o = obs[0]
        self.assertEqual("wsA", o.workspace_id)
        self.assertEqual("default", o.lane_id)
        self.assertEqual("%99", o.codex_pane)
        self.assertEqual("%100", o.claude_pane)
        self.assertEqual("cockpit", o.window)
        self.assertEqual("@1", o.window_id)
        self.assertEqual("/repo/live", o.repo_root)

    def test_repo_root_for_receives_unit_panes(self) -> None:
        seen = []

        def repo_root_for(codex, claude):
            seen.append((codex, claude))
            return "/x"

        build_membership_observations([_cockpit_window()], "s", repo_root_for)
        self.assertEqual([("%99", "%100")], seen)

    def test_role_less_column_is_skipped(self) -> None:
        window = _cockpit_window(columns=[
            {"pane_id": "%1", "workspace_id": "", "role": "", "lane_id": ""},
        ])
        self.assertEqual(
            [], build_membership_observations([window], "s", lambda c, cl: "")
        )

    def test_empty_lane_id_normalizes_to_default(self) -> None:
        window = _cockpit_window(columns=[
            {"pane_id": "%1", "workspace_id": "wsA", "role": "codex", "lane_id": ""},
        ])
        obs = build_membership_observations([window], "s", lambda c, cl: "")
        self.assertEqual("default", obs[0].lane_id)

    def test_distinct_lanes_are_distinct_units(self) -> None:
        window = _cockpit_window(columns=[
            {"pane_id": "%1", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default"},
            {"pane_id": "%2", "workspace_id": "wsA", "role": "codex",
             "lane_id": "lane-x"},
        ])
        obs = build_membership_observations([window], "s", lambda c, cl: "")
        self.assertEqual(2, len(obs))
        self.assertEqual({"default", "lane-x"}, {o.lane_id for o in obs})

    def test_backend_defaults_to_tmux(self) -> None:
        # No `backend` column key -> the observation is tmux (byte-invariant).
        obs = build_membership_observations([_cockpit_window()], "s", lambda c, cl: "")
        self.assertEqual("tmux", obs[0].backend)

    def test_herdr_column_marks_unit_backend(self) -> None:
        # A `backend: herdr` column threads through to the observation (#13298).
        window = _cockpit_window(columns=[
            {"pane_id": "%1", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "backend": "herdr"},
            {"pane_id": "%2", "workspace_id": "wsA", "role": "claude",
             "lane_id": "default", "backend": "herdr"},
        ])
        obs = build_membership_observations([window], "s", lambda c, cl: "")
        self.assertEqual(1, len(obs))
        self.assertEqual("herdr", obs[0].backend)

    def test_any_non_tmux_column_marks_whole_unit(self) -> None:
        # A single non-tmux column is enough to mark the Unit non-tmux.
        window = _cockpit_window(columns=[
            {"pane_id": "%1", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default"},
            {"pane_id": "%2", "workspace_id": "wsA", "role": "claude",
             "lane_id": "default", "backend": "herdr"},
        ])
        obs = build_membership_observations([window], "s", lambda c, cl: "")
        self.assertEqual("herdr", obs[0].backend)

    def test_empty_windows_yield_no_observations(self) -> None:
        self.assertEqual([], build_membership_observations(None, "s", lambda c, cl: ""))
        self.assertEqual([], build_membership_observations([], "s", lambda c, cl: ""))


# --- Unit repo-root resolver. ------------------------------------------------


class _FakeUnitRepoRootOps:
    def __init__(self, runtimes):
        self._runtimes = runtimes
        self.reads = []

    def read_pane_runtime(self, session, pane_id):
        self.reads.append(pane_id)
        return self._runtimes.get(pane_id, {"cwd": "", "process": "", "lane_label": ""})


class UnitRepoRootUseCaseTest(unittest.TestCase):
    def test_first_resolvable_cwd_wins(self) -> None:
        ops = _FakeUnitRepoRootOps({
            "%1": {"cwd": "/repo/a/sub"},
            "%2": {"cwd": "/repo/b"},
        })
        with unittest.mock.patch(
            "mozyo_bridge.application.cockpit_membership_command.infer_repo_root",
            side_effect=lambda cwd: cwd.split("/sub")[0],
        ):
            root = UnitRepoRootUseCase(ops).resolve("s", "%1", "%2")
        self.assertEqual("/repo/a", root)
        # Short-circuits on the first resolvable pane.
        self.assertEqual(["%1"], ops.reads)

    def test_empty_pane_ids_skipped(self) -> None:
        ops = _FakeUnitRepoRootOps({"%2": {"cwd": "/repo/b"}})
        with unittest.mock.patch(
            "mozyo_bridge.application.cockpit_membership_command.infer_repo_root",
            side_effect=lambda cwd: cwd,
        ):
            root = UnitRepoRootUseCase(ops).resolve("s", "", "%2")
        self.assertEqual("/repo/b", root)
        self.assertEqual(["%2"], ops.reads)

    def test_unreadable_cwd_degrades_to_empty(self) -> None:
        ops = _FakeUnitRepoRootOps({})
        self.assertEqual("", UnitRepoRootUseCase(ops).resolve("s", "%1", "%2"))

    def test_unresolvable_root_skips_to_next(self) -> None:
        ops = _FakeUnitRepoRootOps({
            "%1": {"cwd": "/not/a/repo"},
            "%2": {"cwd": "/repo/b"},
        })
        with unittest.mock.patch(
            "mozyo_bridge.application.cockpit_membership_command.infer_repo_root",
            side_effect=lambda cwd: "" if cwd == "/not/a/repo" else cwd,
        ):
            root = UnitRepoRootUseCase(ops).resolve("s", "%1", "%2")
        self.assertEqual("/repo/b", root)


# --- Registry facts resolver. ------------------------------------------------


class _FakeRegistryFactsOps:
    def __init__(self, *, record=None, load_raises=None,
                 anchor=False, anchor_raises=None):
        self._record = record
        self._load_raises = load_raises
        self._anchor = anchor
        self._anchor_raises = anchor_raises
        self.anchor_calls = []

    def load_workspace(self, workspace_id):
        if self._load_raises is not None:
            raise self._load_raises
        return self._record

    def anchor_present(self, repo_root):
        self.anchor_calls.append(repo_root)
        if self._anchor_raises is not None:
            raise self._anchor_raises
        return self._anchor


class RegistryFactsUseCaseTest(unittest.TestCase):
    def test_present_record_maps_to_facts(self) -> None:
        ops = _FakeRegistryFactsOps(
            record=SimpleNamespace(canonical_path="/repo/a", project_name="alpha"),
            anchor=True,
        )
        facts = RegistryFactsUseCase(ops).resolve("wsA")
        self.assertEqual("alpha", facts.label)
        self.assertEqual("/repo/a", facts.repo_root)
        self.assertTrue(facts.registry_present)
        self.assertTrue(facts.anchor_present)
        self.assertEqual(["/repo/a"], ops.anchor_calls)

    def test_missing_record_is_unresolved(self) -> None:
        facts = RegistryFactsUseCase(_FakeRegistryFactsOps(record=None)).resolve("wsA")
        self.assertEqual(RegistryFacts.unresolved("wsA"), facts)
        self.assertFalse(facts.registry_present)

    def test_load_raise_fails_closed_to_unresolved(self) -> None:
        ops = _FakeRegistryFactsOps(load_raises=RuntimeError("boom"))
        facts = RegistryFactsUseCase(ops).resolve("wsA")
        self.assertEqual(RegistryFacts.unresolved("wsA"), facts)

    def test_label_falls_back_to_id_when_project_name_empty(self) -> None:
        ops = _FakeRegistryFactsOps(
            record=SimpleNamespace(canonical_path="/repo/a", project_name="")
        )
        self.assertEqual("wsA", RegistryFactsUseCase(ops).resolve("wsA").label)

    def test_empty_repo_root_skips_anchor_read(self) -> None:
        ops = _FakeRegistryFactsOps(
            record=SimpleNamespace(canonical_path="", project_name="alpha")
        )
        facts = RegistryFactsUseCase(ops).resolve("wsA")
        self.assertTrue(facts.registry_present)
        self.assertFalse(facts.anchor_present)
        self.assertEqual([], ops.anchor_calls)

    def test_anchor_raise_degrades_to_absent(self) -> None:
        ops = _FakeRegistryFactsOps(
            record=SimpleNamespace(canonical_path="/repo/a", project_name="alpha"),
            anchor_raises=OSError("nope"),
        )
        self.assertFalse(RegistryFactsUseCase(ops).resolve("wsA").anchor_present)


# --- Membership use case (collect / list / status). --------------------------


class _FakeMembershipOps:
    def __init__(self, *, windows, geo_panes, facts, unit_repo_root="",
                 canon=None, lane=None):
        self._windows = windows
        self._geo_panes = geo_panes
        self._facts = facts
        self._unit_repo_root = unit_repo_root
        self._canon = canon
        self._lane = lane
        self.facts_calls = []

    def read_managed_windows(self, session):
        return self._windows

    def read_geometry(self, session):
        return self._geo_panes

    def unit_repo_root(self, session, *pane_ids):
        return self._unit_repo_root

    def resolve_registry_facts(self, workspace_id):
        self.facts_calls.append(workspace_id)
        return self._facts.get(workspace_id) or RegistryFacts.unresolved(workspace_id)

    def resolve_canonical_session(self, repo_root):
        return self._canon

    def resolve_workspace_lane(self, repo_root, workspace_id):
        return self._lane


def _facts(label="alpha", repo="/repo/alpha"):
    return RegistryFacts(
        label=label, repo_root=repo, registry_present=True, anchor_present=True
    )


class CollectMembershipTest(unittest.TestCase):
    def test_collect_projects_loaded_member(self) -> None:
        ops = _FakeMembershipOps(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()},
        )
        report = CockpitMembershipUseCase(ops).collect("s")
        self.assertTrue(report.cockpit_present)
        self.assertEqual(1, len(report.workspaces))
        self.assertEqual("wsA", report.workspaces[0].workspace_id)
        self.assertTrue(report.workspaces[0].member)

    def test_registry_facts_resolved_once_per_workspace(self) -> None:
        window = _cockpit_window(columns=[
            {"pane_id": "%1", "workspace_id": "wsA", "role": "codex", "lane_id": "a"},
            {"pane_id": "%2", "workspace_id": "wsA", "role": "codex", "lane_id": "b"},
        ])
        ops = _FakeMembershipOps(
            windows=[window], geo_panes=_geo_panes(), facts={"wsA": _facts()},
        )
        CockpitMembershipUseCase(ops).collect("s")
        self.assertEqual(["wsA"], ops.facts_calls)

    def test_no_cockpit_degrades_to_empty(self) -> None:
        ops = _FakeMembershipOps(windows=[], geo_panes=None, facts={})
        report = CockpitMembershipUseCase(ops).collect("s")
        self.assertFalse(report.cockpit_present)
        self.assertEqual((), report.workspaces)

    def test_present_but_empty_cockpit(self) -> None:
        ops = _FakeMembershipOps(windows=[], geo_panes=_geo_panes(), facts={})
        report = CockpitMembershipUseCase(ops).collect("s")
        self.assertTrue(report.cockpit_present)
        self.assertEqual((), report.workspaces)

    def test_live_repo_root_flows_into_observation(self) -> None:
        ops = _FakeMembershipOps(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts(repo="/repo/alpha-main")},
            unit_repo_root="/repo/alpha-worktree",
        )
        report = CockpitMembershipUseCase(ops).collect("s")
        ws = report.workspaces[0]
        self.assertEqual("/repo/alpha-worktree", ws.repo_root)
        self.assertEqual("/repo/alpha-main", ws.registry_canonical_path)


class ListOutcomeTest(unittest.TestCase):
    def test_list_exit_zero_and_text(self) -> None:
        ops = _FakeMembershipOps(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()},
        )
        outcome = CockpitMembershipUseCase(ops).list("s")
        self.assertEqual(0, outcome.exit_code)
        text = outcome.render(json_output=False)
        self.assertIn("alpha", text)
        self.assertIn("%99", text)

    def test_list_json_shape(self) -> None:
        ops = _FakeMembershipOps(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()},
        )
        outcome = CockpitMembershipUseCase(ops).list("s")
        payload = json.loads(outcome.render(json_output=True))
        self.assertEqual(1, payload["workspace_count"])
        self.assertEqual("wsA", payload["workspaces"][0]["workspace_id"])


class StatusOutcomeTest(unittest.TestCase):
    def _ops(self, *, ws_id="wsA", lane_id="default", **over):
        canon = SimpleNamespace(name="alpha", workspace_id=ws_id)
        lane = LaneIdentity(lane_id, None)
        base = dict(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()}, canon=canon, lane=lane,
        )
        base.update(over)
        return _FakeMembershipOps(**base)

    def test_status_member_exit_zero(self) -> None:
        outcome = CockpitMembershipUseCase(self._ops()).status(
            session="s", repo="/repo/alpha"
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertTrue(outcome.ok)
        self.assertIn("alpha", outcome.render(json_output=False))

    def test_status_absent_exit_one(self) -> None:
        ops = self._ops(ws_id="wsZ")
        ops._canon = SimpleNamespace(name="zeta", workspace_id="wsZ")
        outcome = CockpitMembershipUseCase(ops).status(session="s", repo="/repo/zeta")
        self.assertEqual(1, outcome.exit_code)
        self.assertFalse(outcome.ok)
        self.assertIn("not loaded", outcome.render(json_output=False).lower())

    def test_status_json_carries_query_block(self) -> None:
        outcome = CockpitMembershipUseCase(self._ops()).status(
            session="s", repo="/repo/alpha"
        )
        payload = json.loads(outcome.render(json_output=True))
        self.assertIn("query", payload)
        self.assertEqual("wsA", payload["query"]["workspace_id"])
        self.assertTrue(payload["query"]["member"])

    def test_status_worktree_reports_queried_root(self) -> None:
        # Review j#62643: a worktree query echoes the queried root, not canonical.
        ops = self._ops(
            facts={"wsA": _facts(repo="/repo/alpha-main")},
            unit_repo_root="/repo/alpha-main",
        )
        outcome = CockpitMembershipUseCase(ops).status(
            session="s", repo="/repo/alpha-worktree"
        )
        payload = json.loads(outcome.render(json_output=True))
        ws = payload["workspaces"][0]
        self.assertEqual("/repo/alpha-worktree", ws["repo_root"])
        self.assertEqual("/repo/alpha-main", ws["registry_canonical_path"])
        self.assertEqual("/repo/alpha-worktree", payload["query"]["repo_root"])


# --- Port contracts: the fake and live adapters satisfy the protocols. -------


class PortContractTest(unittest.TestCase):
    def test_membership_ops_contract(self) -> None:
        self.assertIsInstance(LiveCockpitMembershipOps(), CockpitMembershipOps)
        self.assertIsInstance(
            _FakeMembershipOps(windows=[], geo_panes=None, facts={}),
            CockpitMembershipOps,
        )

    def test_unit_repo_root_ops_contract(self) -> None:
        self.assertIsInstance(LiveUnitRepoRootOps(), UnitRepoRootOps)
        self.assertIsInstance(_FakeUnitRepoRootOps({}), UnitRepoRootOps)

    def test_registry_facts_ops_contract(self) -> None:
        self.assertIsInstance(LiveRegistryFactsOps(), RegistryFactsOps)
        self.assertIsInstance(_FakeRegistryFactsOps(), RegistryFactsOps)

    def test_outcome_exit_codes(self) -> None:
        self.assertEqual(0, CockpitListOutcome(report=None).exit_code)
        self.assertEqual(
            1,
            CockpitStatusOutcome(
                report=None, query={}, query_label="x", ok=False
            ).exit_code,
        )


if __name__ == "__main__":
    unittest.main()
