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
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

from mozyo_bridge.application.cockpit_membership_command import (
    CockpitListOutcome,
    CockpitMembershipOps,
    CockpitMembershipUseCase,
    CockpitStatusOutcome,
    HerdrColumnOps,
    LiveCockpitMembershipOps,
    LiveHerdrColumnOps,
    LiveRegistryFactsOps,
    LiveUnitRepoRootOps,
    NullHerdrColumnOps,
    RegistryFactsOps,
    RegistryFactsUseCase,
    UnitRepoRootOps,
    UnitRepoRootUseCase,
    build_membership_observations,
    herdr_membership_observations,
)
from mozyo_bridge.core.state.lane_metadata import LaneMetadataRecord
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    WARN_HERDR_INVENTORY_UNAVAILABLE,
    WARN_HERDR_LANE_RECORD_MISSING,
    RegistryFacts,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    LaneIdentity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
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


# --- Pure herdr-column projection (#13303). ----------------------------------


def _herdr_row(workspace_id, role, lane="", locator="w1:p1"):
    return {"name": encode_assigned_name(workspace_id, role, lane), "pane_id": locator}


class HerdrMembershipObservationsTest(unittest.TestCase):
    def test_groups_codex_and_claude_into_one_herdr_unit(self) -> None:
        obs = herdr_membership_observations(
            [
                _herdr_row("wsA", "codex", "laneX"),
                _herdr_row("wsA", "claude", "laneX"),
            ]
        )
        self.assertEqual(1, len(obs))
        self.assertEqual("wsA", obs[0].workspace_id)
        self.assertEqual("laneX", obs[0].lane_id)
        self.assertEqual(BACKEND_HERDR, obs[0].backend)

    def test_transient_locator_is_not_carried_onto_pane_fields(self) -> None:
        # The herdr locator is cache/evidence, never route authority (#13297), and
        # the degrade projection tokenises the pane fields anyway -> keep them empty.
        obs = herdr_membership_observations([_herdr_row("wsA", "codex", "laneX")])
        self.assertEqual("", obs[0].codex_pane)
        self.assertEqual("", obs[0].claude_pane)

    def test_foreign_non_scheme_agents_are_dropped(self) -> None:
        obs = herdr_membership_observations(
            [
                {"name": "poc_claude", "pane_id": "w9:p9"},  # not a mzb1 scheme name
                {"name": "", "pane_id": "w9:p8"},
                _herdr_row("wsA", "codex", "laneX"),
            ]
        )
        self.assertEqual(1, len(obs))
        self.assertEqual("wsA", obs[0].workspace_id)

    def test_empty_lane_normalizes_to_default(self) -> None:
        obs = herdr_membership_observations([_herdr_row("wsB", "claude", "")])
        self.assertEqual("default", obs[0].lane_id)

    def test_distinct_lanes_are_distinct_units(self) -> None:
        obs = herdr_membership_observations(
            [
                _herdr_row("wsA", "codex", "a"),
                _herdr_row("wsA", "codex", "b"),
            ]
        )
        self.assertEqual({"a", "b"}, {o.lane_id for o in obs})

    def test_unit_with_missing_locator_still_tagged(self) -> None:
        # A decoded agent with no live locator is still a loaded herdr Unit.
        obs = herdr_membership_observations([_herdr_row("wsC", "claude", "z", locator="")])
        self.assertEqual(1, len(obs))
        self.assertEqual(BACKEND_HERDR, obs[0].backend)

    def test_empty_input_yields_no_observations(self) -> None:
        self.assertEqual([], herdr_membership_observations(None))
        self.assertEqual([], herdr_membership_observations([]))

    # -- lane-record display join (#13367). ----------------------------------

    def test_lane_record_join_fills_label_and_issue(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            issue_id="13367",
            lane_label="issue_13367_cockpit_herdr_polish",
        )
        obs = herdr_membership_observations(
            [_herdr_row("wt_abc", "codex", "default")],
            resolve_lane_record={"wt_abc": record}.get,
        )
        self.assertEqual("issue_13367_cockpit_herdr_polish", obs[0].lane_label)
        self.assertEqual("13367", obs[0].issue)
        self.assertFalse(obs[0].lane_record_missing)

    def test_lane_record_issue_falls_back_to_label_parse(self) -> None:
        # A record with no explicit issue_id but a parseable label still resolves.
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            lane_label="issue_13367_polish",
        )
        obs = herdr_membership_observations(
            [_herdr_row("wt_abc", "codex", "default")],
            resolve_lane_record={"wt_abc": record}.get,
        )
        self.assertEqual("13367", obs[0].issue)

    def test_missing_record_degrades_to_token_and_flags(self) -> None:
        obs = herdr_membership_observations(
            [_herdr_row("wt_orphan", "codex", "default")],
            resolve_lane_record={}.get,
        )
        # Fail-open: the raw workspace token is the lane label, issue unknown, and
        # the missing-record flag is set so the projection emits the advisory.
        self.assertEqual("wt_orphan", obs[0].lane_label)
        self.assertEqual("", obs[0].issue)
        self.assertTrue(obs[0].lane_record_missing)

    def test_no_resolver_takes_the_fail_open_path(self) -> None:
        obs = herdr_membership_observations([_herdr_row("wt_x", "codex", "default")])
        self.assertEqual("wt_x", obs[0].lane_label)
        self.assertTrue(obs[0].lane_record_missing)


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


# --- Dual-backend transition: `status` keeps every matching backend row (#13317). --


class _FakeHerdrColumnOps:
    """A herdr supply that returns canned `agent list` rows, or raises.

    Defined once here and reused by the #13317 dual-presence and the #13303
    collect specifications below (same construction).
    """

    def __init__(self, rows, *, error=None):
        self._rows = rows
        self._error = error

    def read_herdr_agent_rows(self):
        if self._error is not None:
            raise self._error
        return self._rows


class StatusDualPresenceTest(unittest.TestCase):
    """#13317 auditor j#73083 decision (a): during the herdr backend swap the same
    ``(workspace_id, lane_id)`` slot can carry both a tmux rollback-lever Unit and a
    live herdr Unit; ``status`` must keep BOTH rows so the live herdr agent is never
    hidden behind the first-match tmux row (`cockpit list` already shows both). A
    tmux-only / herdr-only / absent slot still yields a single row (byte-invariant).
    """

    def _ops(self, *, ws_id="wsA", lane_id="default", **over):
        canon = SimpleNamespace(name="alpha", workspace_id=ws_id)
        lane = LaneIdentity(lane_id, None)
        base = dict(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()}, canon=canon, lane=lane,
        )
        base.update(over)
        return _FakeMembershipOps(**base)

    def _herdr_rows_same_slot(self):
        # A herdr Unit occupying the SAME (workspace_id, lane_id) as the tmux Unit
        # in ``_cockpit_window`` (wsA / default), i.e. the dual-presence collision.
        return [
            {"name": encode_assigned_name("wsA", "codex", "default"), "pane_id": "w1:p1"},
            {"name": encode_assigned_name("wsA", "claude", "default"), "pane_id": "w1:p2"},
        ]

    def test_dual_presence_json_keeps_both_backend_rows(self) -> None:
        outcome = CockpitMembershipUseCase(
            self._ops(), _FakeHerdrColumnOps(self._herdr_rows_same_slot())
        ).status(session="s", repo="/repo/alpha")
        payload = json.loads(outcome.render(json_output=True))
        # Both backend rows survive — the live herdr agent is not hidden.
        self.assertEqual(2, payload["workspace_count"])
        backends = {w["backend"] for w in payload["workspaces"]}
        self.assertEqual({BACKEND_TMUX, BACKEND_HERDR}, backends)
        # The query verdict aggregates the rows: the slot is a member, exit 0.
        self.assertTrue(payload["query"]["member"])
        self.assertEqual(0, outcome.exit_code)
        self.assertTrue(outcome.ok)

    def test_dual_presence_rows_pin_repo_root_to_queried_checkout(self) -> None:
        # Every matching row echoes the queried checkout (review j#62643), including
        # the herdr row (whose live repo_root is otherwise the registry canonical).
        outcome = CockpitMembershipUseCase(
            self._ops(), _FakeHerdrColumnOps(self._herdr_rows_same_slot())
        ).status(session="s", repo="/repo/alpha")
        payload = json.loads(outcome.render(json_output=True))
        queried = payload["query"]["repo_root"]
        self.assertTrue(all(w["repo_root"] == queried for w in payload["workspaces"]))

    def test_dual_presence_text_shows_tmux_row_and_herdr_backend_line(self) -> None:
        text = CockpitMembershipUseCase(
            self._ops(), _FakeHerdrColumnOps(self._herdr_rows_same_slot())
        ).status(session="s", repo="/repo/alpha").render(json_output=False)
        # The tmux row's live pane and the herdr backend aux line both appear.
        self.assertIn("%99", text)
        self.assertIn("backend: herdr", text)

    def _degraded_tmux_plus_healthy_herdr(self):
        # A tmux Unit missing its claude peer (geometry warning -> ok=False) sharing
        # the queried slot with a healthy herdr Unit (ok=True): the reviewer's
        # `all(w.ok)` vs `any(w.ok)` split case (j#73096).
        one_peer = _cockpit_window(columns=[
            {"pane_id": "%99", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "pane_left": 0, "pane_width": 80},
        ])
        ops = self._ops(windows=[one_peer], geo_panes=[])
        return CockpitMembershipUseCase(
            ops, _FakeHerdrColumnOps(self._herdr_rows_same_slot())
        ).status(session="s", repo="/repo/alpha")

    def test_dual_presence_member_true_even_if_only_herdr_is_ok(self) -> None:
        # A tmux Unit missing its claude peer (geometry warning -> not ok) alongside
        # a healthy herdr Unit: the slot is still a member and OK (any row ok).
        outcome = self._degraded_tmux_plus_healthy_herdr()
        payload = json.loads(outcome.render(json_output=True))
        self.assertEqual(2, payload["workspace_count"])
        self.assertEqual([False, True], sorted(w["ok"] for w in payload["workspaces"]))
        self.assertTrue(payload["query"]["member"])
        self.assertEqual(0, outcome.exit_code)

    def test_dual_presence_json_query_ok_matches_exit_verdict(self) -> None:
        # Regression (review j#73096): with a degraded tmux row (ok=False) beside a
        # healthy herdr row (ok=True), the JSON machine-readable query verdict must
        # not contradict the exit code. `query.ok` mirrors exit (any row ok = True /
        # exit 0); the top-level `ok` stays report-health (all rows -> False here),
        # documenting the split so a consumer keys on `query.ok`, never `ok`.
        outcome = self._degraded_tmux_plus_healthy_herdr()
        payload = json.loads(outcome.render(json_output=True))
        self.assertTrue(payload["query"]["ok"])
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual(payload["query"]["ok"], outcome.exit_code == 0)
        # The whole-view verdict honestly reflects the degraded tmux row.
        self.assertFalse(payload["ok"])

    def test_query_ok_agrees_with_top_level_ok_for_single_healthy_row(self) -> None:
        # A single healthy backend row: `query.ok` and the report-level `ok` agree
        # (no split), so the new field is not a surprise on the common path.
        outcome = CockpitMembershipUseCase(self._ops()).status(
            session="s", repo="/repo/alpha"
        )
        payload = json.loads(outcome.render(json_output=True))
        self.assertTrue(payload["query"]["ok"])
        self.assertTrue(payload["ok"])
        self.assertEqual(0, outcome.exit_code)

    def test_tmux_only_status_is_single_row_byte_invariant(self) -> None:
        # herdr off (default null supply) -> exactly the prior single-row projection.
        baseline = CockpitMembershipUseCase(self._ops()).status(
            session="s", repo="/repo/alpha"
        )
        with_off = CockpitMembershipUseCase(
            self._ops(), _FakeHerdrColumnOps(None)
        ).status(session="s", repo="/repo/alpha")
        self.assertEqual(
            baseline.render(json_output=True), with_off.render(json_output=True)
        )
        payload = json.loads(baseline.render(json_output=True))
        self.assertEqual(1, payload["workspace_count"])
        self.assertEqual(BACKEND_TMUX, payload["workspaces"][0]["backend"])

    def test_herdr_only_status_is_single_row(self) -> None:
        # No tmux managed windows; only a herdr Unit in the queried slot -> one row.
        ops = self._ops(windows=[], geo_panes=None)
        outcome = CockpitMembershipUseCase(
            ops, _FakeHerdrColumnOps(self._herdr_rows_same_slot())
        ).status(session="s", repo="/repo/alpha")
        payload = json.loads(outcome.render(json_output=True))
        self.assertEqual(1, payload["workspace_count"])
        self.assertEqual(BACKEND_HERDR, payload["workspaces"][0]["backend"])
        self.assertTrue(payload["query"]["member"])
        self.assertEqual(0, outcome.exit_code)

    def test_absent_status_is_single_row_byte_invariant(self) -> None:
        # A queried slot with no matching backend row on either side stays the single
        # absent row (herdr Units for a different workspace do not divert it).
        ops = self._ops()
        ops._canon = SimpleNamespace(name="zeta", workspace_id="wsZ")
        other_herdr = [
            {"name": encode_assigned_name("wsQ", "codex", "default"), "pane_id": "w1:p1"},
        ]
        outcome = CockpitMembershipUseCase(
            ops, _FakeHerdrColumnOps(other_herdr)
        ).status(session="s", repo="/repo/zeta")
        payload = json.loads(outcome.render(json_output=True))
        self.assertEqual(1, payload["workspace_count"])
        self.assertFalse(payload["workspaces"][0]["member"])
        self.assertEqual(1, outcome.exit_code)
        self.assertIn("not loaded", outcome.render(json_output=False).lower())


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

    def test_herdr_column_ops_contract(self) -> None:
        self.assertIsInstance(NullHerdrColumnOps(), HerdrColumnOps)
        self.assertIsInstance(LiveHerdrColumnOps(), HerdrColumnOps)
        self.assertIsInstance(_FakeHerdrColumnOps(None), HerdrColumnOps)


# --- Live herdr Units flow into the collect projection (#13303). -------------


class CollectHerdrMembershipTest(unittest.TestCase):
    def _tmux_only_ops(self):
        # A cockpit with one tmux Unit, so we can assert the tmux rows are untouched.
        return _FakeMembershipOps(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()},
        )

    def _herdr_rows(self):
        return [
            {"name": encode_assigned_name("wsH", "codex", "L"), "pane_id": "w1:p1"},
            {"name": encode_assigned_name("wsH", "claude", "L"), "pane_id": "w1:p2"},
        ]

    def test_live_herdr_unit_is_tagged_and_degraded(self) -> None:
        report = CockpitMembershipUseCase(
            self._tmux_only_ops(), _FakeHerdrColumnOps(self._herdr_rows())
        ).collect("s")
        herdr = next(w for w in report.workspaces if w.workspace_id == "wsH")
        self.assertEqual(BACKEND_HERDR, herdr.backend)
        self.assertTrue(herdr.member)
        # tmux-only fields degraded, never a stale tmux pane / geometry.
        self.assertEqual("backend_unavailable", herdr.geometry_status)
        self.assertEqual("unsupported", herdr.codex_pane)
        self.assertFalse(herdr.panes_present)
        # A loaded herdr Unit is ok (degraded liveness is honest, not a fault).
        self.assertTrue(herdr.ok)

    def test_herdr_facts_resolved_for_its_workspace(self) -> None:
        ops = self._tmux_only_ops()
        CockpitMembershipUseCase(ops, _FakeHerdrColumnOps(self._herdr_rows())).collect("s")
        self.assertIn("wsH", ops.facts_calls)

    def test_tmux_units_are_byte_invariant_alongside_herdr(self) -> None:
        # The pre-existing tmux Unit projects exactly as it does without herdr.
        baseline = CockpitMembershipUseCase(self._tmux_only_ops()).collect("s")
        with_herdr = CockpitMembershipUseCase(
            self._tmux_only_ops(), _FakeHerdrColumnOps(self._herdr_rows())
        ).collect("s")
        base_tmux = next(w for w in baseline.workspaces if w.workspace_id == "wsA")
        live_tmux = next(w for w in with_herdr.workspaces if w.workspace_id == "wsA")
        self.assertEqual(base_tmux.as_dict(), live_tmux.as_dict())
        self.assertEqual(BACKEND_TMUX, live_tmux.backend)

    def test_herdr_off_is_byte_invariant(self) -> None:
        # None (herdr backend off) -> identical to the default null supply.
        default = CockpitMembershipUseCase(self._tmux_only_ops()).collect("s")
        off = CockpitMembershipUseCase(
            self._tmux_only_ops(), _FakeHerdrColumnOps(None)
        ).collect("s")
        self.assertEqual(default.as_dict(), off.as_dict())

    def test_default_use_case_has_no_herdr_units(self) -> None:
        report = CockpitMembershipUseCase(self._tmux_only_ops()).collect("s")
        self.assertTrue(all(w.backend == BACKEND_TMUX for w in report.workspaces))

    def test_unreadable_herdr_snapshot_degrades_to_warning(self) -> None:
        report = CockpitMembershipUseCase(
            self._tmux_only_ops(),
            _FakeHerdrColumnOps(None, error=TerminalTransportError("server down")),
        ).collect("s")
        # No herdr Units, but an explicit advisory (never a silent empty inventory).
        self.assertTrue(all(w.backend == BACKEND_TMUX for w in report.workspaces))
        codes = {w.code for w in report.warnings}
        self.assertIn(WARN_HERDR_INVENTORY_UNAVAILABLE, codes)
        self.assertFalse(report.ok)

    def test_herdr_only_environment_is_present_not_nothing_loaded(self) -> None:
        # No tmux managed windows / geometry, herdr `agent list` returns a Unit:
        # cockpit_present must be True so the projection does not both list a
        # `member` herdr row and say "nothing loaded" (review j#72953).
        from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
            format_membership_text,
        )

        ops = _FakeMembershipOps(windows=[], geo_panes=None, facts={})
        report = CockpitMembershipUseCase(
            ops, _FakeHerdrColumnOps(self._herdr_rows())
        ).collect("s")
        self.assertTrue(report.cockpit_present)
        self.assertEqual(1, len(report.workspaces))
        self.assertTrue(report.workspaces[0].member)
        text = format_membership_text(report)
        self.assertNotIn("nothing loaded", text)

    def test_no_units_on_any_backend_still_says_nothing_loaded(self) -> None:
        # herdr off + no tmux -> genuinely nothing loaded, byte-invariant.
        default = CockpitMembershipUseCase(
            _FakeMembershipOps(windows=[], geo_panes=None, facts={})
        ).collect("s")
        off = CockpitMembershipUseCase(
            _FakeMembershipOps(windows=[], geo_panes=None, facts={}),
            _FakeHerdrColumnOps(None),
        ).collect("s")
        self.assertFalse(default.cockpit_present)
        self.assertEqual(default.as_dict(), off.as_dict())

    def test_live_herdr_ops_off_returns_none(self) -> None:
        # A repo whose config selects no herdr backend (here: no config at all)
        # keeps the live supply None (byte-invariant), not an empty list or a
        # raise — pinned to a hermetic repo_root so this checkout's committed
        # backend selection (#13307 herdr re-cutover) cannot leak in.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                LiveHerdrColumnOps(repo_root=tmp).read_herdr_agent_rows()
            )


# --- Live herdr lane-record display join through the use case (#13367). -------


class CollectHerdrLaneRecordJoinTest(unittest.TestCase):
    """The lane metadata store LEFT JOINs onto the live herdr rows in `collect`.

    Patches ``load_lane_records`` (the store read ``_herdr_observations`` performs)
    so the join is exercised end to end: the herdr row's JSON carries the recorded
    ``lane_label`` / ``issue``; a missing record fails open to the raw token + the
    ``lane_record_missing`` advisory; and the tmux row's JSON stays byte-invariant.
    """

    _LOAD = "mozyo_bridge.core.state.lane_metadata.load_lane_records"

    def _ops(self):
        return _FakeMembershipOps(
            windows=[_cockpit_window()], geo_panes=_geo_panes(),
            facts={"wsA": _facts()},
        )

    def _herdr_rows(self):
        return [
            {"name": encode_assigned_name("wt_abc", "codex", "default"),
             "pane_id": "w1:p1"},
            {"name": encode_assigned_name("wt_abc", "claude", "default"),
             "pane_id": "w1:p2"},
        ]

    def _collect(self, records):
        with unittest.mock.patch(self._LOAD, return_value=records):
            return CockpitMembershipUseCase(
                self._ops(), _FakeHerdrColumnOps(self._herdr_rows())
            ).collect("s")

    def _herdr_row(self, report):
        return next(w for w in report.workspaces if w.workspace_id == "wt_abc")

    def test_json_carries_recorded_lane_label_and_issue(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            issue_id="13367",
            lane_label="issue_13367_cockpit_herdr_polish",
        )
        report = self._collect({"wt_abc": record})
        row = self._herdr_row(report).as_dict()
        self.assertEqual("issue_13367_cockpit_herdr_polish", row["lane_label"])
        self.assertEqual("13367", row["issue"])
        # No fail-open advisory when the record joined cleanly.
        codes = {w["code"] for w in row["warnings"]}
        self.assertNotIn(WARN_HERDR_LANE_RECORD_MISSING, codes)

    def test_text_shows_lane_label_and_issue(self) -> None:
        from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
            format_membership_text,
        )

        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            issue_id="13367",
            lane_label="issue_13367_cockpit_herdr_polish",
        )
        text = format_membership_text(self._collect({"wt_abc": record}))
        self.assertIn("lane: issue_13367_cockpit_herdr_polish (issue 13367)", text)

    def test_missing_record_degrades_to_token_with_warning(self) -> None:
        report = self._collect({})
        row = self._herdr_row(report)
        self.assertEqual("wt_abc", row.lane_label)  # raw token, fail-open
        payload = row.as_dict()
        self.assertEqual("", payload["issue"])
        codes = {w["code"] for w in payload["warnings"]}
        self.assertIn(WARN_HERDR_LANE_RECORD_MISSING, codes)

    def test_tmux_row_json_omits_issue_key_byte_invariant(self) -> None:
        # The `issue` field is a herdr-only key: a tmux row's JSON must not carry it
        # (the acceptance's "tmux 表示 byte-compatible").
        report = self._collect({})
        tmux_row = next(w for w in report.workspaces if w.workspace_id == "wsA")
        self.assertNotIn("issue", tmux_row.as_dict())
        self.assertEqual(BACKEND_TMUX, tmux_row.backend)


if __name__ == "__main__":
    unittest.main()
