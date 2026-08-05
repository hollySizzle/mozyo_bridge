"""Redmine #14996 R2 — an appended coordinator pair owns its own project column.

The live finding (j#99833): a second project's coordinator pair launched into the
shared ``project-coordinators`` workspace produced an L — the first project's
Claude spanning the whole bottom row while the new pair sat stacked in the top
right — instead of two full-height columns. Routing and identity were correct;
only the geometry was wrong, and it defeats the one-screen overview the mode
exists for.

These tests drive the real nested split tree (:mod:`tests.support.herdr_pane_tree`)
rather than an argv assertion, because the defect is only expressible in a nested
tree: the previous suite checked the ``--split`` argv and passed while the screen
was an L (j#99845).

Every scenario is backed by the same durable authorities production reads — a
registered workspace per project, a self-attestation per foreign pane, and a cwd
under that project's registry root. Reviews j#99885 and j#99904 both landed on
panes this module had not actually proved anything about, so a fixture that
skipped the evidence would be testing a weaker rail than the one that ships.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support.herdr_pane_tree import PaneTreeHerdr  # noqa: E402

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_CONFLICT,
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
)
from mozyo_bridge.core.state.workspace_registry import (  # noqa: E402
    read_anchor,
    register_workspace,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E402,E501
    COLUMN_APPLIED,
    COLUMN_FAILED,
    COLUMN_MATCHED,
    COLUMN_NOT_APPLICABLE,
    reflow_project_columns,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E402,E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_cli import (  # noqa: E402,E501
    _render_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    DEFAULT_LANE,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E402,E501
    HEALTH_HEALTHY,
)

TOP = "top"
PROJECT_A = "project-a"
PROJECT_B = "project-b"
PROJECT_C = "project-c"


class _Env:
    """A home with registered project workspaces and an evidence-backed herdr.

    Labels are stable across a test; the mozyo ``workspace_id`` behind each is the
    real minted registry id, so ``load_workspace_by_id`` resolves the same root the
    panes report as their cwd.
    """

    def __init__(self, case: unittest.TestCase, *labels: str) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.ids: dict = {}
        self.roots: dict = {}
        for label in labels:
            root = base / label
            root.mkdir()
            register_workspace(root, home=self.home)
            self.ids[label] = read_anchor(root)["workspace_id"]
            self.roots[label] = root
        self.herdr = PaneTreeHerdr()
        self.herdr.cwd_by_workspace = {
            self.ids[label]: str(self.roots[label]) for label in labels
        }
        self.store = HerdrIdentityAttestationStore(home=self.home)

    def name(self, label: str, provider: str, lane: str = "") -> str:
        return encode_assigned_name(self.ids[label], provider, lane)

    def attest(
        self, pane: str, label: str, provider: str, lane: str = "",
        verdict: str = VERDICT_PRESENT,
    ) -> None:
        self.store.upsert(
            IdentityAttestationRecord(
                assigned_name=self.name(label, provider, lane),
                workspace_id=self.ids[label],
                role=provider,
                lane_id=lane or DEFAULT_LANE,
                locator=pane,
                verdict=verdict,
            )
        )

    def declare_lane(self, label: str, lane: str, kind: str) -> None:
        LaneLifecycleStore(home=self.home).declare_active(
            LaneLifecycleKey(self.ids[label], lane),
            decision=DecisionPointer(
                source="redmine", issue_id="14996", journal_id="99904"
            ),
            issue_id="14996",
            lane_kind=kind,
        )

    # -- scenario building ------------------------------------------------
    def seed_columns(self, tab, *columns) -> list:
        """Already-columnar projects, each ``(label, lane)`` a full-height column."""
        names = [
            [self.name(label, provider, lane) for provider in ("codex", "claude")]
            for label, lane in columns
        ]
        built = self.herdr.seed_columns(tab, names)
        for (label, lane), panes in zip(columns, built):
            for provider, pane in zip(("codex", "claude"), panes):
                self.attest(pane, label, provider, lane)
        return built

    def append_pair(self, tab, label: str, lane: str = "") -> tuple:
        """Launch a pair exactly as the pre-fix rail does: split the ACTIVE pane."""
        first = self.herdr.launch_into(tab, self.name(label, "codex", lane), focus=True)
        second = self.herdr.launch_into(
            tab, self.name(label, "claude", lane), split="down"
        )
        return first, second

    def result(self, label: str, launched, lane: str = "") -> SessionStartResult:
        result = SessionStartResult(
            workspace_id=self.ids[label], lane_id=lane or DEFAULT_LANE
        )
        result.herdr_workspace_id = self.herdr.workspace_id
        for provider, locator in zip(("codex", "claude"), launched):
            result.slots.append(
                SlotResult(
                    provider=provider,
                    assigned_name=self.name(label, provider, lane),
                    outcome=SLOT_LAUNCHED,
                    locator=locator,
                )
            )
        return result

    def run(self, result, **overrides):
        kwargs = {
            "project_coordinator": True,
            "home": self.home,
            "top_workspace_id": self.ids.get(TOP, ""),
            "launched": 2,
            "initial_occupancy": 0,
            "dry_run": False,
            "binary": "herdr",
            "runner": self.herdr,
            "timeout": 5.0,
            "env": None,
        }
        kwargs.update(overrides)
        return reflow_project_columns(result, **kwargs)

    def moves(self) -> list:
        return [call for call in self.herdr.calls if call[:2] == ["pane", "move"]]


class ProjectColumnGeometryTest(unittest.TestCase):
    def _columns(self, env: _Env, panes: dict) -> dict:
        """``{label: (x, width)}`` after asserting each pair is one full column."""
        rects = env.herdr.rects()
        bottoms = {y + h for _x, y, _w, h in rects.values()}
        tops = {y for _x, y, _w, _h in rects.values()}
        columns = {}
        for label, members in panes.items():
            xs = {rects[pane][0] for pane in members}
            widths = {rects[pane][2] for pane in members}
            self.assertEqual(len(xs), 1, f"{label} panes are not vertically aligned")
            self.assertEqual(len(widths), 1, f"{label} panes have different widths")
            stacked = sorted(members, key=lambda pane: rects[pane][1])
            self.assertEqual(rects[stacked[0]][1], min(tops))
            self.assertEqual(rects[stacked[-1]][1] + rects[stacked[-1]][3], max(bottoms))
            columns[label] = (xs.pop(), widths.pop())
        return columns

    def test_appended_pair_becomes_its_own_full_height_column(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (a_top, a_bottom), = env.seed_columns(tab, (PROJECT_A, ""))
        b_top, b_bottom = env.append_pair(tab, PROJECT_B)

        # The defect, reproduced: project A's lower pane spans the whole tab width
        # while the appended pair is nested inside project A's upper half.
        before = env.herdr.rects()
        self.assertEqual(before[a_bottom][2], 54)
        self.assertEqual(before[a_top][2], 27)
        self.assertGreater(before[b_top][0], before[a_top][0])

        outcome, detail = env.run(env.result(PROJECT_B, [b_top, b_bottom]))
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

        columns = self._columns(
            env, {PROJECT_A: [a_top, a_bottom], PROJECT_B: [b_top, b_bottom]}
        )
        # Two equal columns that tile the tab: the even 2x2 the owner accepted.
        self.assertEqual(columns[PROJECT_A], (0, 27))
        self.assertEqual(columns[PROJECT_B], (27, 27))
        rects = env.herdr.rects()
        self.assertLess(rects[a_top][1], rects[a_bottom][1])
        self.assertLess(rects[b_top][1], rects[b_bottom][1])
        # Every pane is back in the one shared tab; no temp tab survived.
        self.assertEqual(len(env.herdr.tabs), 1)
        self.assertEqual(sorted(tab.panes()), sorted([a_top, a_bottom, b_top, b_bottom]))

    def test_result_is_independent_of_which_pane_was_active_before_launch(self):
        """j#99845: the geometry must not depend on pre-launch focus."""
        geometries = []
        for focus_bottom in (False, True):
            env = _Env(self, PROJECT_A, PROJECT_B)
            tab = env.herdr.new_tab()
            (a_top, a_bottom), = env.seed_columns(tab, (PROJECT_A, ""))
            if focus_bottom:
                tab.focused = a_bottom
            b_top, b_bottom = env.append_pair(tab, PROJECT_B)
            outcome, detail = env.run(env.result(PROJECT_B, [b_top, b_bottom]))
            self.assertEqual(outcome, COLUMN_APPLIED, detail)
            geometries.append(
                self._columns(
                    env, {PROJECT_A: [a_top, a_bottom], PROJECT_B: [b_top, b_bottom]}
                )
            )
        self.assertEqual(geometries[0], geometries[1])

    def test_third_pair_gets_its_own_column_without_crossing_the_others(self):
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C)
        tab = env.herdr.new_tab()
        (a_pair, b_pair) = env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_B, ""))
        c_top, c_bottom = env.append_pair(tab, PROJECT_C)
        outcome, detail = env.run(env.result(PROJECT_C, [c_top, c_bottom]))
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

        columns = self._columns(
            env,
            {PROJECT_A: a_pair, PROJECT_B: b_pair, PROJECT_C: [c_top, c_bottom]},
        )
        spans = sorted(columns.values())
        for (x, width), (next_x, _w) in zip(spans, spans[1:]):
            self.assertEqual(x + width, next_x, "project columns overlap or leave a gap")
        # The newest project is appended on the right; existing columns keep their order.
        self.assertEqual(max(columns, key=lambda key: columns[key][0]), PROJECT_C)
        self.assertLess(columns[PROJECT_A][0], columns[PROJECT_B][0])

    def test_already_columnar_tab_moves_no_pane(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        b_top, b_bottom = env.append_pair(tab, PROJECT_B)
        self.assertEqual(env.run(env.result(PROJECT_B, [b_top, b_bottom]))[0], COLUMN_APPLIED)
        before = len(env.moves())
        outcome, _detail = env.run(env.result(PROJECT_B, [b_top, b_bottom]))
        self.assertEqual(outcome, COLUMN_MATCHED)
        self.assertEqual(len(env.moves()), before)


class ProjectColumnRestraintTest(unittest.TestCase):
    """The reflow must never fire outside the one case it was authorised for."""

    def _shared(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        return env, env.append_pair(tab, PROJECT_B)

    def _assert_untouched(self, env, outcome, expected=COLUMN_NOT_APPLICABLE):
        self.assertEqual(outcome, expected)
        self.assertEqual(env.moves(), [])

    def test_non_role_grouped_placement_reads_nothing(self):
        env, launched = self._shared()
        env.herdr.calls.clear()
        outcome, _detail = env.run(
            env.result(PROJECT_B, list(launched)), project_coordinator=False
        )
        self._assert_untouched(env, outcome)
        self.assertEqual(env.herdr.calls, [])

    def test_dry_run_reads_nothing(self):
        env, launched = self._shared()
        env.herdr.calls.clear()
        outcome, _detail = env.run(env.result(PROJECT_B, list(launched)), dry_run=True)
        self._assert_untouched(env, outcome)
        self.assertEqual(env.herdr.calls, [])

    def test_adopt_only_run_moves_nothing(self):
        env, launched = self._shared()
        result = SessionStartResult(workspace_id=env.ids[PROJECT_B], lane_id=DEFAULT_LANE)
        result.herdr_workspace_id = env.herdr.workspace_id
        result.slots.append(
            SlotResult(
                provider="codex",
                assigned_name=env.name(PROJECT_B, "codex"),
                outcome=SLOT_ADOPTED,
                locator=launched[0],
            )
        )
        self._assert_untouched(env, env.run(result, launched=0)[0])

    def test_single_provider_heal_beside_a_live_sibling_moves_nothing(self):
        env, launched = self._shared()
        outcome, _detail = env.run(
            env.result(PROJECT_B, list(launched)), launched=1, initial_occupancy=1
        )
        self._assert_untouched(env, outcome)

    def test_first_project_in_the_shared_workspace_moves_nothing(self):
        env = _Env(self, PROJECT_A)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        self._assert_untouched(env, env.run(env.result(PROJECT_A, pair))[0])


class ColumnOperatorSurfaceTest(unittest.TestCase):
    """Review j#99885 finding_1 — a failing column must be readable, not inferred.

    The defect: the run exited non-zero on the column axis while the text named
    only role health and the split ratio as possible causes, and never printed the
    detail that carries the stranded pane. Both stated causes were false, so the
    operator was pointed away from the only recoverable fact the run held.
    """

    def _healthy(self, **fields) -> SessionStartResult:
        result = SessionStartResult(workspace_id="ws", lane_id=DEFAULT_LANE)
        for provider in ("codex", "claude"):
            result.slots.append(
                SlotResult(
                    provider=provider,
                    assigned_name=f"mzb1_ws_{provider}_default",
                    outcome=SLOT_LAUNCHED,
                    locator="w1:p4",
                    health=HEALTH_HEALTHY,
                )
            )
        for key, value in fields.items():
            setattr(result, key, value)
        return result

    def test_a_failed_column_prints_its_detail_and_the_stranded_pane(self):
        detail = "pane(s) ['w1:p5'] are NOT in the shared project-coordinator tab"
        text = _render_text(
            self._healthy(column_outcome=COLUMN_FAILED, column_detail=detail)
        )
        self.assertIn("project column: failed", text)
        self.assertIn(detail, text)
        self.assertIn("w1:p5", text)

    def test_the_failure_sentence_names_the_column_as_a_possible_cause(self):
        result = self._healthy(column_outcome=COLUMN_FAILED, column_detail="x")
        self.assertFalse(result.ok)
        sentence = [
            line for line in _render_text(result).splitlines()
            if line.startswith("session-start did NOT fully succeed")
        ]
        self.assertEqual(len(sentence), 1)
        self.assertIn("column", sentence[0])

    def test_a_successful_column_is_still_reported_so_the_measurement_is_visible(self):
        text = _render_text(
            self._healthy(column_outcome=COLUMN_APPLIED, column_detail="2 pair(s)")
        )
        self.assertIn("project column: applied (2 pair(s))", text)
        self.assertNotIn("did NOT fully succeed", text)

    def test_the_resting_value_prints_no_column_line(self):
        self.assertNotIn("project column:", _render_text(self._healthy()))

    def test_the_column_axis_reaches_the_json_payload(self):
        payload = self._healthy(
            column_outcome=COLUMN_FAILED, column_detail="stranded w1:p5"
        ).as_payload()
        self.assertEqual(payload["column_outcome"], COLUMN_FAILED)
        self.assertEqual(payload["column_detail"], "stranded w1:p5")
        self.assertFalse(payload["ok"])

    def test_a_failed_column_keeps_the_run_from_reporting_success(self):
        result = SessionStartResult(workspace_id="ws", lane_id=DEFAULT_LANE)
        result.column_outcome = COLUMN_FAILED
        self.assertFalse(result.column_ok)
        self.assertFalse(result.ok)

    def test_an_unrecognised_column_token_is_not_a_success(self):
        result = SessionStartResult(workspace_id="ws", lane_id=DEFAULT_LANE)
        result.column_outcome = "appllied"
        self.assertFalse(result.column_ok)


class ColumnPaneAuthorityTest(unittest.TestCase):
    """Reviews j#99885 finding_2 / j#99904 — what may become a project pair.

    An assigned name's ``role`` is a provider token, so a decodable row says
    nothing about whether the pane belongs to a project coordinator. Every refusal
    below must land BEFORE the first pane move: j#99904 finding_2 measured four
    moves executed ahead of a closing failure, which is the property these pin.
    """

    def _assert_zero_move_failure(self, env, outcome, detail, fragment):
        self.assertEqual(outcome, COLUMN_FAILED, detail)
        self.assertIn(fragment, detail)
        self.assertIn("no live pane was moved", detail)
        self.assertEqual(env.moves(), [])

    def _with_named_lane(self, lane: str):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_A, lane))
        return env, env.append_pair(tab, PROJECT_B)

    def test_the_top_coordinator_in_the_shared_tab_is_refused_not_reshaped(self):
        """Review j#99904 finding_1 — six panes moved before this refusal existed."""
        env = _Env(self, TOP, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (TOP, ""))
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "configured top coordinator")

    def test_a_named_lane_without_a_durable_kind_is_never_treated_as_coordinator(self):
        env, launched = self._with_named_lane("implementation-1")
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "has no durable lane-kind")

    def test_an_implementation_lane_in_the_shared_tab_is_refused_not_reshaped(self):
        env, launched = self._with_named_lane("implementation-1")
        env.declare_lane(PROJECT_A, "implementation-1", LANE_KIND_IMPLEMENTATION)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(
            env, outcome, detail, "not 'delegated_coordinator'"
        )

    def test_a_delegated_coordinator_lane_joins_the_coordinator_role_group(self):
        env, launched = self._with_named_lane("delegated-1")
        env.declare_lane(PROJECT_A, "delegated-1", LANE_KIND_DELEGATED_COORDINATOR)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self.assertEqual(outcome, COLUMN_APPLIED, detail)
        self.assertIn("3 project pair(s)", detail)

    def test_this_run_s_own_named_lane_is_not_re_derived_from_the_store(self):
        """A managed ``delegated_coordinator`` appending its own column.

        The lane kind was already proved by the caller before anything launched —
        that authority is what routed this pair to the shared workspace at all —
        and its durable row is written on a different edge than the launch. Asking
        the lifecycle store to re-prove it here failed the live actuator path
        (measured against ``HerdrSublaneActuatorOps.append_lane_column``), so the
        store join is FOREIGN-only.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        own = "delegated-b"
        launched = env.append_pair(tab, PROJECT_B, lane=own)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched), lane=own))
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

    def test_a_stale_sibling_refuses_the_whole_set_before_any_move(self):
        """Review j#99904 finding_2 — filtering it away moved four panes first."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        env.herdr.stale_panes.add(pair[1])
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "shell residue")

    def test_a_foreign_pane_without_a_self_attestation_is_refused(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        # Seed WITHOUT attesting: the pair is live and decodable, and that is all.
        env.herdr.seed_columns(
            tab, [[env.name(PROJECT_A, "codex"), env.name(PROJECT_A, "claude")]]
        )
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "no durable self-attestation")

    def test_a_conflicting_self_attestation_is_not_a_proof(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        env.attest(pair[1], PROJECT_A, "claude", verdict=VERDICT_CONFLICT)
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "self-attested 'conflict'")

    def test_a_foreign_pane_running_outside_its_project_root_is_refused(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        env.herdr.cwd_by_workspace[env.ids[PROJECT_A]] = str(Path("/"))
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "runs outside the registry root")

    def test_duplicate_providers_in_one_group_are_an_identity_conflict(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.herdr.seed_columns(
            tab, [[env.name(PROJECT_A, "codex"), env.name(PROJECT_A, "codex")]]
        )
        env.attest("unused", PROJECT_A, "codex")
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "duplicate provider")

    def test_a_project_short_one_slot_still_owns_a_verified_column(self):
        """The disputed half of j#99885 finding_3, accepted by Answer j#99900.

        A GENUINE live one-pane group — nothing filtered away, every authority
        resolved — still tiles a full-height column, which ``_column_span`` proves
        from the layout. Failing here would report another project's missing slot
        as THIS run's column failure.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        alone = env.herdr.seed_pane(tab, env.name(PROJECT_A, "codex"))
        env.attest(alone, PROJECT_A, "codex")
        b_top, b_bottom = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, [b_top, b_bottom]))
        self.assertEqual(outcome, COLUMN_MATCHED, detail)
        rects = env.herdr.rects()
        self.assertEqual(rects[alone][3], rects[b_top][3] + rects[b_bottom][3])
        self.assertEqual(env.moves(), [])


class ProjectColumnFailClosedTest(unittest.TestCase):
    """A reflow that could not be established is never reported as a success."""

    def _scenario(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        return env, tab, pair, env.append_pair(tab, PROJECT_B)

    def test_refused_detach_is_typed_and_keeps_every_pane_in_the_shared_tab(self):
        env, tab, project_a, project_b = self._scenario()
        env.herdr.move_refusals.add(project_a[1])
        result = env.result(PROJECT_B, list(project_b))
        outcome, detail = env.run(result)
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("refused", detail)
        self.assertIn("returned to the shared tab", detail)
        self.assertEqual(len(env.herdr.tabs), 1)
        self.assertEqual(sorted(tab.panes()), sorted(list(project_a) + list(project_b)))
        result.column_outcome, result.column_detail = outcome, detail
        self.assertFalse(result.column_ok)

    def test_a_pane_left_outside_the_shared_tab_is_named_in_the_detail(self):
        env, tab, _project_a, project_b = self._scenario()
        env.herdr.refuse_from_move = 2
        outcome, detail = env.run(env.result(PROJECT_B, list(project_b)))
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("NOT in the shared", detail)
        self.assertIn("live-relayout runbook", detail)
        self.assertIn(project_b[1], detail)
        self.assertNotIn(project_b[1], tab.panes())

    def test_same_tab_no_op_move_is_not_read_as_a_completed_move(self):
        env, _tab, _project_a, project_b = self._scenario()
        env.herdr.move_unchanged.add(project_b[1])
        outcome, detail = env.run(env.result(PROJECT_B, list(project_b)))
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("no completed move", detail)

    def test_identity_change_across_the_reflow_is_not_claimed_as_geometry(self):
        env, _tab, project_a, project_b = self._scenario()
        env.herdr.rename_after_moves[6] = (
            project_a[0], encode_assigned_name("ws-impostor", "codex"),
        )
        outcome, detail = env.run(env.result(PROJECT_B, list(project_b)))
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("inventory changed across the reflow", detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
