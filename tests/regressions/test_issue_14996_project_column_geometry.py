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

import subprocess
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
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
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
    COLUMN_PREPARED,
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
    HEALTH_NOT_PROBED,
    HEALTH_OUTCOMES,
    HEALTH_PROVIDER_EXITED,
)


def _git(*args: str) -> None:
    """A real git call — the linked-worktree regression needs a real worktree."""
    subprocess.run(("git", *args), check=True, capture_output=True)


TOP = "top"
PROJECT_A = "project-a"
PROJECT_B = "project-b"
PROJECT_C = "project-c"
PROJECT_D = "project-d"


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

    def result(self, label: str, launched, lane: str = "", health=HEALTH_HEALTHY):
        """The run's own result, as the launcher hands it to the reflow.

        ``health`` is the startup-liveness axis the canonical probe settles before
        the geometry pass runs (#14996 R3): every scenario here is a pair that came
        up, so the default is the settled, healthy verdict a real launch carries by
        the time the reflow is reached.
        """
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
                    health=health,
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
        self.assertEqual(env.herdr.resizes, [], "two equal columns need no resize")
        rects = env.herdr.rects()
        self.assertLess(rects[a_top][1], rects[a_bottom][1])
        self.assertLess(rects[b_top][1], rects[b_bottom][1])
        # Every pane is back in the one shared tab; no temp tab survived.
        self.assertEqual(len(env.herdr.tabs), 1)
        self.assertEqual(sorted(tab.panes()), sorted([a_top, a_bottom, b_top, b_bottom]))

    def test_appending_a_column_preserves_the_existing_unit_internal_ratio(self):
        """#15126: the bounce must not reset the existing Unit to Herdr's 0.5."""

        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (a_top, a_bottom), = env.seed_columns(tab, (PROJECT_A, ""))
        self.assertTrue(tab.resize(a_top, "up", 0.15))
        before = env.herdr.rects()
        self.assertEqual(before[a_top][3], 8)
        self.assertEqual(before[a_bottom][3], 15)

        b_top, b_bottom = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, [b_top, b_bottom]))
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

        after = env.herdr.rects()
        self.assertEqual(after[a_top][3], before[a_top][3])
        self.assertEqual(after[a_bottom][3], before[a_bottom][3])
        restore = next(
            call
            for call in env.moves()
            if call[2] == a_bottom and "--tab" in call
        )
        self.assertEqual(restore[restore.index("--ratio") + 1], "0.35")

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
        widths = [width for _x, width in columns.values()]
        self.assertLessEqual(max(widths) - min(widths), 1)
        self.assertTrue(env.herdr.resizes, "the 1/2 + 1/4 + 1/4 tree must be balanced")

    def test_four_columns_are_balanced_without_reordering_projects(self):
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C, PROJECT_D)
        tab = env.herdr.new_tab()
        a_pair, b_pair, c_pair = env.seed_columns(
            tab, (PROJECT_A, ""), (PROJECT_B, ""), (PROJECT_C, "")
        )
        d_top, d_bottom = env.append_pair(tab, PROJECT_D)
        outcome, detail = env.run(env.result(PROJECT_D, [d_top, d_bottom]))
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

        columns = self._columns(
            env,
            {
                PROJECT_A: a_pair,
                PROJECT_B: b_pair,
                PROJECT_C: c_pair,
                PROJECT_D: [d_top, d_bottom],
            },
        )
        ordered = [key for key, _span in sorted(columns.items(), key=lambda item: item[1])]
        self.assertEqual(ordered, [PROJECT_A, PROJECT_B, PROJECT_C, PROJECT_D])
        widths = [width for _x, width in columns.values()]
        self.assertLessEqual(max(widths) - min(widths), 1)

    def test_resize_refusal_is_a_typed_column_failure(self):
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_B, ""))
        launched = env.append_pair(tab, PROJECT_C)
        env.herdr.resize_refused = True
        outcome, detail = env.run(env.result(PROJECT_C, list(launched)))
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("refused project-column resize", detail)

    def test_resize_success_without_progress_is_a_typed_column_failure(self):
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_B, ""))
        launched = env.append_pair(tab, PROJECT_C)
        env.herdr.resize_unchanged = True
        outcome, detail = env.run(env.result(PROJECT_C, list(launched)))
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("stopped moving", detail)

    def test_unreadable_layout_after_resize_reports_the_read_failure(self):
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_B, ""))
        launched = env.append_pair(tab, PROJECT_C)
        env.herdr.layout_unreadable_after_resize = True
        outcome, detail = env.run(env.result(PROJECT_C, list(launched)))
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("pane layout could not be read after project-column resize", detail)
        self.assertNotIn("lost its right-axis divider", detail)

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

    def test_eleventh_project_is_prepared_for_configured_widths(self):
        labels = tuple(f"project-{index}" for index in range(11))
        env = _Env(self, *labels)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, *((label, "") for label in labels[:-1]))
        launched = env.append_pair(tab, labels[-1])
        outcome, detail = env.run(env.result(labels[-1], list(launched)))
        self.assertEqual(outcome, COLUMN_PREPARED)
        self.assertIn("configured placement", detail)
        self.assertTrue(env.moves())
        self.assertEqual(env.herdr.resizes, [])
        self.assertEqual(len(env.herdr.tabs), 1)


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

    def test_a_read_taken_before_the_liveness_pass_settled_is_refused(self):
        """R3 live finding j#100135 — the ordering is checked, not assumed.

        A pane herdr has just started reports the residue row shape until its
        provider boots, so the canonical startup pass holds that shape as
        *retryable* rather than as a verdict. This pass may only read the inventory
        once that verdict exists; a launched slot still at ``not_probed`` says it
        does not.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        result = env.result(PROJECT_B, list(launched), health=HEALTH_NOT_PROBED)
        outcome, detail = env.run(result)
        self._assert_zero_move_failure(
            env, outcome, detail, "the startup-liveness pass has not settled"
        )
        self.assertIn("booting provider from shell residue", detail)

    def test_only_an_admitted_launch_places_a_column(self):
        """Review j#100188 finding_1 — measured at 6 live pane moves per token.

        The first cut asked whether the canonical pass had RUN and let every
        verdict it wrote through. But the authority exempts this run's own panes
        from the startup attestation (that fact was unanswerable when the geometry
        ran first), so an own-side ``attestation_mismatch`` / ``locator_drift`` /
        ``provider_exited`` is not reconstructible from the next inventory read —
        the pair was reflowed and only afterwards reported not ok.

        The admitted set is the domain contract's own: ``healthy`` alone. The table
        is the whole non-admitting vocabulary and asserts that it is the whole one,
        because a sample would not have caught the token that was missing.
        """
        refused = sorted(HEALTH_OUTCOMES - {HEALTH_HEALTHY})
        self.assertEqual(
            sorted(HEALTH_OUTCOMES),
            sorted(refused + [HEALTH_HEALTHY]),
            "the table must cover the health vocabulary, not a sample of it",
        )
        for verdict in refused:
            with self.subTest(health=verdict):
                env = _Env(self, PROJECT_A, PROJECT_B)
                tab = env.herdr.new_tab()
                env.seed_columns(tab, (PROJECT_A, ""))
                launched = env.append_pair(tab, PROJECT_B)
                outcome, detail = env.run(
                    env.result(PROJECT_B, list(launched), health=verdict)
                )
                self.assertEqual(outcome, COLUMN_FAILED, detail)
                self.assertIn("no live pane was moved", detail)
                self.assertEqual(env.moves(), [])
                # Each refusal names ITS OWN cause: `not_probed` is the pass not
                # having answered, everything else is the answer it gave.
                if verdict == HEALTH_NOT_PROBED:
                    self.assertIn("the startup-liveness pass has not settled", detail)
                else:
                    self.assertIn("did not pass startup admission", detail)
                    self.assertIn(verdict, detail)

    def test_an_unadmitted_first_project_reports_failed_not_not_applicable(self):
        """A measured consequence of refusing before the read, pinned deliberately.

        Whether a column is owed at all is a fact about the WORKSPACE, so it needs
        the inventory read this guard is placed in front of. A run that may not
        take that read cannot claim ``not_applicable`` — that would assert
        something it did not establish — so the only pair in the workspace still
        reports ``failed`` when its own launch was not admitted. It costs zero
        moves and the run is already reporting failure on the health axis, so this
        is a second true line about one failed launch, not the j#100135 shape
        (there the pair was HEALTHY and the column axis invented a cause).
        """
        env = _Env(self, PROJECT_A)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        admitted = env.run(env.result(PROJECT_A, pair, health=HEALTH_HEALTHY))
        self.assertEqual(admitted[0], COLUMN_NOT_APPLICABLE, admitted[1])
        env = _Env(self, PROJECT_A)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        outcome, detail = env.run(
            env.result(PROJECT_A, pair, health=HEALTH_PROVIDER_EXITED)
        )
        self._assert_zero_move_failure(env, outcome, detail, "startup admission")

    def test_an_admitted_launch_is_the_positive_control(self):
        """The one token that admits — and it must actually move panes.

        Paired with the table above so the guard is shown separating inputs rather
        than merely refusing: the same scenario, the same fixture, one axis
        changed, and the effect the whole issue exists to produce still happens.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched), health=HEALTH_HEALTHY)
        )
        self.assertEqual(outcome, COLUMN_APPLIED, detail)
        self.assertTrue(env.moves(), "the admitted control must still reflow")

    def test_a_malformed_health_value_is_not_promoted_to_a_settled_verdict(self):
        """`_norm` is ``str(value).strip()``, so a non-token would read as settled.

        The same promotion j#99971 found on the inventory side: ``None`` / ``0`` /
        ``[]`` all normalise to something that is not ``not_probed``. Membership in
        the closed vocabulary is the question, not emptiness.
        """
        for value in (None, 0, False, [], {}, "healthy ", "definitely_fine"):
            with self.subTest(health=value):
                env = _Env(self, PROJECT_A, PROJECT_B)
                tab = env.herdr.new_tab()
                env.seed_columns(tab, (PROJECT_A, ""))
                launched = env.append_pair(tab, PROJECT_B)
                outcome, detail = env.run(
                    env.result(PROJECT_B, list(launched), health=value)
                )
                self._assert_zero_move_failure(
                    env, outcome, detail, "the startup-liveness pass has not settled"
                )

    def test_the_same_pair_with_a_settled_verdict_is_reflowed(self):
        """The control for the guard above: only the health axis differs.

        Without it the refusal proves nothing about which input it separates —
        a guard has to be shown passing what it must pass, in the same commit.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched), health=HEALTH_HEALTHY)
        )
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

    def test_an_admitted_launch_is_still_judged_by_the_inventory(self):
        """Admission is a precondition, not a replacement for the workspace read.

        A green startup verdict says this run's launch came up; it does not say
        what the pane looks like now. Promoting it to the liveness proof would move
        that decision out of the workspace — where the foreign panes are judged —
        and into this run's own report, leaving the two halves of one pair proved
        by different authorities. So an admitted pair whose inventory row is
        residue is still refused, on the inventory's axis.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.stale_panes.add(launched[0])
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched), health=HEALTH_HEALTHY)
        )
        self._assert_zero_move_failure(env, outcome, detail, "is shell residue")

    def test_an_unaddressable_slot_is_refused_on_its_own_axis(self):
        """A slot with no locator was never probeable, so it carries no ordering
        evidence — and it is refused by the authority under its own cause rather
        than under a premature-read cause that is not true of it (j#99955)."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        result = env.result(PROJECT_B, list(launched))
        result.slots.append(
            SlotResult(
                provider="codex",
                assigned_name=env.name(PROJECT_B, "codex"),
                outcome=SLOT_LAUNCHED,
                locator="",
                health=HEALTH_NOT_PROBED,
            )
        )
        outcome, detail = env.run(result)
        self._assert_zero_move_failure(env, outcome, detail, "with no pane locator")
        self.assertNotIn("startup-liveness pass", detail)

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

    def test_a_delegated_coordinator_in_a_linked_worktree_is_accepted(self):
        """Review j#99913 finding_3 — the shape the cwd rule used to make impossible.

        A named lane runs from a LINKED WORKTREE that inherits the main checkout's
        registry identity while living beside it, so a containment test against
        the registry root refused every legitimate managed
        ``delegated_coordinator`` — and with it the issue's own acceptance that
        one converges into this workspace. The cwd is resolved through the
        identity model's own resolver instead, which maps a worktree back to the
        workspace it inherits from.
        """
        env = _Env(self, PROJECT_B)
        main = env.roots[PROJECT_B].parent / "mainrepo"
        main.mkdir()
        _git("init", "-q", str(main))
        (main / "seed").write_text("x", encoding="utf-8")
        _git("-C", str(main), "add", "-A")
        _git(
            "-C", str(main), "-c", "user.email=t@e", "-c", "user.name=t",
            "commit", "-qm", "seed",
        )
        register_workspace(main, home=env.home)
        main_ws = read_anchor(main)["workspace_id"]
        worktree = main.parent / "worktrees" / "lane1"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git("-C", str(main), "worktree", "add", "-q", str(worktree))
        self.assertNotIn(
            main.resolve(), worktree.resolve().parents,
            "the fixture must use a real sibling worktree, not a child directory",
        )

        lane = "delegated-1"
        LaneLifecycleStore(home=env.home).declare_active(
            LaneLifecycleKey(main_ws, lane),
            decision=DecisionPointer(
                source="redmine", issue_id="14996", journal_id="99913"
            ),
            issue_id="14996",
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        env.herdr.cwd_by_workspace[main_ws] = str(worktree)
        tab = env.herdr.new_tab()
        (pair,) = env.herdr.seed_columns(
            tab,
            [[
                encode_assigned_name(main_ws, provider, lane)
                for provider in ("codex", "claude")
            ]],
        )
        for provider, pane in zip(("codex", "claude"), pair):
            env.store.upsert(
                IdentityAttestationRecord(
                    assigned_name=encode_assigned_name(main_ws, provider, lane),
                    workspace_id=main_ws,
                    role=provider,
                    lane_id=lane,
                    locator=pane,
                    verdict=VERDICT_PRESENT,
                )
            )
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
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
        self._assert_zero_move_failure(env, outcome, detail, "no usable startup self-attestation")

    def test_a_conflicting_self_attestation_is_not_a_proof(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        env.attest(pair[1], PROJECT_A, "claude", verdict=VERDICT_CONFLICT)
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "self-attestation (conflict)")

    def test_an_unregistered_foreground_helper_uses_the_stable_pane_cwd(self):
        """Herdr #1472: foreground helper cwd is not pane workspace identity."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        stable_rows = env.herdr._rows
        reads = 0

        def transient_rows():
            nonlocal reads
            reads += 1
            rows = stable_rows()
            if reads == 1:
                for row in rows:
                    if row.get("pane_id") == launched[1]:
                        row["foreground_cwd"] = str(Path("/"))
            return rows

        env.herdr._rows = transient_rows
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched)), sleeper=lambda _seconds: None
        )
        self.assertEqual(outcome, COLUMN_APPLIED, detail)
        self.assertGreaterEqual(reads, 1)
        self.assertEqual(
            env.herdr.calls[1][:2],
            ["pane", "layout"],
            "an unregistered helper cwd must not create a pointless retry",
        )

    def test_a_fresh_stable_cwd_that_settles_on_the_next_read_is_retried(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        stable_rows = env.herdr._rows
        reads = 0

        def transient_rows():
            nonlocal reads
            reads += 1
            rows = stable_rows()
            if reads == 1:
                for row in rows:
                    if row.get("pane_id") == launched[1]:
                        row["cwd"] = str(Path("/"))
            return rows

        env.herdr._rows = transient_rows
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched)), sleeper=lambda _seconds: None
        )
        self.assertEqual(outcome, COLUMN_APPLIED, detail)
        self.assertGreaterEqual(reads, 2)
        self.assertEqual(
            env.herdr.calls[:2],
            [["agent", "list"], ["agent", "list"]],
            "the stable cwd must be re-read before the first layout or move",
        )

    def test_a_persistently_unresolved_own_cwd_exhausts_a_bounded_wait(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.cwd_by_workspace[env.ids[PROJECT_B]] = str(Path("/"))
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched)),
            own_observation_retry_budget_seconds=0.2,
            own_observation_retry_interval_seconds=0.1,
            sleeper=sleep,
            monotonic=lambda: now[0],
        )
        self._assert_zero_move_failure(
            env, outcome, detail, "resolves to no registered mozyo workspace"
        )
        reads = [call for call in env.herdr.calls if call[:2] == ["agent", "list"]]
        self.assertEqual(len(reads), 2)
        self.assertAlmostEqual(now[0], 0.2)

    def test_a_foreign_pane_in_an_unregistered_directory_is_refused(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        env.herdr.cwd_by_workspace[env.ids[PROJECT_A]] = str(Path("/"))
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(
            env, outcome, detail, "resolves to no registered mozyo workspace"
        )
        reads = [call for call in env.herdr.calls if call[:2] == ["agent", "list"]]
        self.assertEqual(len(reads), 1, "foreign evidence is never launch-retried")

    def test_a_foreign_pane_running_in_another_project_is_refused(self):
        """The name claims one project; the working directory is another's."""
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        env.herdr.cwd_by_workspace[env.ids[PROJECT_A]] = str(env.roots[PROJECT_C])
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "while its assigned name claims")

    def test_a_later_foreign_conflict_outranks_an_own_retryable_cwd(self):
        """Inventory order cannot hide a non-retryable foreign contradiction."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (foreign_pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.detected_override[foreign_pair[0]] = "claude"
        stable_rows = env.herdr._rows
        reads = 0

        def own_first_transient_rows():
            nonlocal reads
            reads += 1
            rows = stable_rows()
            if reads == 1:
                for row in rows:
                    if row.get("pane_id") == launched[0]:
                        row["cwd"] = str(Path("/"))
            rows.sort(
                key=lambda row: 0 if row.get("pane_id") in launched else 1
            )
            return rows

        env.herdr._rows = own_first_transient_rows
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched)), sleeper=lambda _seconds: None
        )
        self._assert_zero_move_failure(
            env, outcome, detail, "while its assigned name claims"
        )
        reads_seen = [
            call for call in env.herdr.calls if call[:2] == ["agent", "list"]
        ]
        self.assertEqual(
            len(reads_seen), 1, "foreign contradiction must stop the first full read"
        )

    def test_foreign_attestation_outranks_an_own_retryable_cwd(self):
        """The full authority pass, not just row facts, precedes any re-read."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (foreign_pair,) = env.herdr.seed_columns(
            tab,
            [[env.name(PROJECT_A, provider) for provider in ("codex", "claude")]],
        )
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.cwd_by_workspace[env.ids[PROJECT_B]] = str(Path("/"))
        stable_rows = env.herdr._rows

        def own_first_rows():
            rows = stable_rows()
            rows.sort(
                key=lambda row: 0 if row.get("pane_id") in launched else 1
            )
            return rows

        env.herdr._rows = own_first_rows
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched)),
            sleeper=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
        self._assert_zero_move_failure(
            env, outcome, detail, "no usable startup self-attestation"
        )
        self.assertIn(foreign_pair[0], detail)
        reads = [call for call in env.herdr.calls if call[:2] == ["agent", "list"]]
        self.assertEqual(len(reads), 1, "foreign attestation must stop the first read")

    def test_foreign_named_lane_authority_outranks_an_own_retryable_cwd(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, "delegated-1"))
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.cwd_by_workspace[env.ids[PROJECT_B]] = str(Path("/"))
        stable_rows = env.herdr._rows

        def own_first_rows():
            rows = stable_rows()
            rows.sort(
                key=lambda row: 0 if row.get("pane_id") in launched else 1
            )
            return rows

        env.herdr._rows = own_first_rows
        outcome, detail = env.run(
            env.result(PROJECT_B, list(launched)),
            sleeper=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
        self._assert_zero_move_failure(env, outcome, detail, "no durable lane-kind")
        reads = [call for call in env.herdr.calls if call[:2] == ["agent", "list"]]
        self.assertEqual(len(reads), 1, "foreign lane state must stop the first read")

    def test_a_previous_generation_self_attestation_is_never_re_used(self):
        """Review j#99913 finding_1 — six panes moved on a stale record before this.

        The identity triplet and the verdict both matched; only the recorded
        locator belonged to a process that is gone. ``evaluate_attestation`` calls
        that ``stale`` and refuses to re-use it, which is exactly the conjunct the
        hand-written join had dropped.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        for provider in ("codex", "claude"):
            env.attest("w1:pOLD", PROJECT_A, provider)
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "(stale)")

    def test_a_detected_provider_that_contradicts_the_assigned_name_is_refused(self):
        """Review j#99913 finding_2 — a live marker is not a role proof."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        env.herdr.detected_override[pair[0]] = "claude"  # a Codex slot reporting claude
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "while its assigned name claims")

    def test_an_unrecognised_detected_provider_is_not_positive_liveness(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (pair,) = env.seed_columns(tab, (PROJECT_A, ""))
        env.herdr.detected_override[pair[0]] = "nethack"
        launched = env.append_pair(tab, PROJECT_B)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "no recognised provider")

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

    def test_this_run_s_own_pane_still_answers_what_its_row_already_says(self):
        """Review j#99931 finding_1 — each of these moved six panes before.

        The own exemption was argued from two facts a just-launched slot cannot
        yet answer, then applied to three it can. Liveness, the detected provider
        and the working directory come off the same inventory row for every pane
        in the workspace, so they are owed by every pane in the workspace.
        """
        for label, mutate, fragment in (
            ("stale", lambda env, own: env.herdr.stale_panes.add(own[0]),
             "shell residue"),
            ("provider",
             lambda env, own: env.herdr.detected_override.__setitem__(own[0], "claude"),
             "while its assigned name claims"),
            ("cwd",
             lambda env, own: env.herdr.cwd_by_workspace.__setitem__(
                 env.ids[PROJECT_B], str(env.roots[PROJECT_A])
             ),
             "while its assigned name claims"),
        ):
            with self.subTest(case=label):
                env = _Env(self, PROJECT_A, PROJECT_B)
                tab = env.herdr.new_tab()
                env.seed_columns(tab, (PROJECT_A, ""))
                own = env.append_pair(tab, PROJECT_B)
                mutate(env, own)
                outcome, detail = env.run(env.result(PROJECT_B, list(own)))
                self._assert_zero_move_failure(env, outcome, detail, fragment)

    def test_an_undecodable_row_in_the_shared_tab_refuses_before_any_move(self):
        """Review j#99931 finding_2 — skipping it cost six moves and a late failure."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        stray = env.herdr._mint_pane()
        env.herdr.agents[stray] = "not-a-mzb1-name"
        tab.subdivide(tab.panes()[0], "down", stray)
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "no decodable mozyo identity")

    def test_every_malformed_inventory_row_refuses_before_any_move(self):
        """Table-driven: the whole malformed-row space costs zero pane moves.

        Reviews j#99938 and j#99950 each found one more branch of the same
        two-valued scope question, so the point is no longer "these two inputs are
        fixed" but "the space is enumerated and every member of it refuses before
        a plan exists" (coordinator convergence note j#99953).
        """
        cases = (
            ("no workspace, no locator", {"pane_id": "", "workspace_id": ""}),
            ("no workspace, unparseable locator", {"pane_id": "nocolon"}),
            ("claims this workspace, no locator",
             {"pane_id": "", "workspace_id": "w1"}),
            ("claims this workspace, unparseable locator",
             {"pane_id": "nocolon", "workspace_id": "w1"}),
            ("workspace contradicts locator",
             {"pane_id": "w9:p1", "workspace_id": "w1"}),
            # Review j#99960 finding_1: both sides foreign, and disagreeing. Such a
            # row has not established that it lives ANYWHERE, so it cannot be filed
            # as out-of-scope — it used to be, and six panes moved past it.
            ("two foreign workspaces contradicting each other",
             {"pane_id": "w3:p9", "workspace_id": "w2"}),
            ("declared foreign, locator addresses this workspace",
             {"pane_id": "w1:p90", "workspace_id": "w2"}),
            # Review j#99971 finding_1: `_norm` is `str(value).strip()`, so each of
            # these would have become a perfectly good foreign workspace id.
            ("workspace_id is a list", {"pane_id": "", "workspace_id": []}),
            ("workspace_id is a dict", {"pane_id": "", "workspace_id": {}}),
            ("workspace_id is an int", {"pane_id": "", "workspace_id": 17}),
            ("workspace_id is a bool", {"pane_id": "", "workspace_id": True}),
            ("pane_id is not text", {"pane_id": 17, "workspace_id": "w1"}),
            ("undecodable name on an addressable pane",
             {"pane_id": "w1:p90", "workspace_id": "w1"}),
        )
        for label, extra in cases:
            with self.subTest(row=label):
                env = _Env(self, PROJECT_A, PROJECT_B)
                tab = env.herdr.new_tab()
                env.seed_columns(tab, (PROJECT_A, ""))
                launched = env.append_pair(tab, PROJECT_B)
                row = {"name": "not-a-mzb1-name", "agent": "codex",
                       "agent_status": "idle"}
                row.update(extra)
                env.herdr.extra_rows.append(row)
                outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
                self.assertEqual(outcome, COLUMN_FAILED, detail)
                self.assertEqual(env.moves(), [], f"{label} moved a pane")

    def test_a_foreign_row_that_resolves_elsewhere_stays_out_of_scope(self):
        """The control for the table above: out of scope is not a refusal."""
        for label, extra in (
            ("declared and locator both foreign",
             {"pane_id": "w9:p1", "workspace_id": "w9"}),
            ("declared foreign, no locator", {"pane_id": "", "workspace_id": "w9"}),
            ("locator foreign, no declared field", {"pane_id": "w9:p1"}),
            # Review j#99978 finding_1: a row that has proved it lives elsewhere is
            # out of scope, and the shape of a field only an in-scope row would be
            # read for must not take the whole workspace down with it.
            ("foreign row with a non-text name",
             {"pane_id": "w9:p1", "workspace_id": "w9", "name": 17}),
            ("foreign row with a non-text detected agent",
             {"pane_id": "w9:p1", "workspace_id": "w9", "agent": {}}),
            ("foreign row with a non-text cwd",
             {"pane_id": "w9:p1", "workspace_id": "w9", "foreground_cwd": []}),
        ):
            with self.subTest(row=label):
                env = _Env(self, PROJECT_A, PROJECT_B)
                tab = env.herdr.new_tab()
                env.seed_columns(tab, (PROJECT_A, ""))
                launched = env.append_pair(tab, PROJECT_B)
                row = {"name": "not-a-mzb1-name", "agent": "codex",
                       "agent_status": "idle"}
                row.update(extra)
                env.herdr.extra_rows.append(row)
                outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
                self.assertEqual(outcome, COLUMN_APPLIED, detail)

    def test_two_launched_slots_on_one_pane_refuse_before_any_move(self):
        """Review j#99950 finding_2 — folding them into a dict dropped one.

        A duplicate locator meant the contradicting slot never reached the exact
        join, and the survivor alone carried it. Two launches reporting one pane is
        a backend contradiction, not a set to deduplicate.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        (a_pair, _b) = env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_B, ""))
        result = SessionStartResult(
            workspace_id=env.ids[PROJECT_A], lane_id=DEFAULT_LANE
        )
        result.herdr_workspace_id = env.herdr.workspace_id
        for provider in ("codex", "claude"):
            result.slots.append(
                SlotResult(
                    provider=provider,
                    assigned_name=env.name(PROJECT_A, provider),
                    outcome=SLOT_LAUNCHED,
                    locator=a_pair[1],  # both slots point at the SAME pane
                    health=HEALTH_HEALTHY,
                )
            )
        outcome, detail = env.run(result)
        self._assert_zero_move_failure(
            env, outcome, detail, "two launched slots on pane"
        )

    def test_a_launched_slot_without_a_locator_refuses(self):
        """A launch this run reports but cannot address is a contradiction.

        Dropping it on the way into the authority would be the same silent
        exclusion the authority refuses on inside the workspace (coordinator note
        j#99955), so every ``launched`` slot is handed over and a blank locator
        refuses with zero moves.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        result = env.result(PROJECT_B, list(launched))
        result.slots.append(
            SlotResult(
                provider="codex",
                assigned_name=env.name(PROJECT_B, "codex"),
                outcome=SLOT_LAUNCHED,
                locator="",
            )
        )
        outcome, detail = env.run(result)
        self._assert_zero_move_failure(env, outcome, detail, "with no pane locator")

    def test_a_launched_slot_naming_an_identity_it_did_not_launch_refuses(self):
        """Every launched slot is joined, not just the ones that happen to fit."""
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        result = env.result(PROJECT_B, list(launched))
        # The codex slot claims the pane that is actually running claude.
        result.slots[0] = SlotResult(
            provider="codex",
            assigned_name=env.name(PROJECT_B, "codex"),
            outcome=SLOT_LAUNCHED,
            locator=launched[1],
            health=HEALTH_HEALTHY,
        )
        outcome, detail = env.run(result)
        self._assert_zero_move_failure(
            env, outcome, detail, "two launched slots on pane"
        )

    def test_a_row_claiming_this_workspace_without_a_locator_refuses(self):
        """Review j#99938 finding_1 — scope is a conjunct, not a preamble.

        Production rows state their workspace explicitly as well as inside the
        locator. A row that claims this workspace with an unusable locator fell
        out of a locator-only scope test entirely, so it rode along until the
        closing tiling check failed six moves later.
        """
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.extra_rows.append(
            {
                "name": "not-a-mzb1-name",
                "pane_id": "",
                "workspace_id": env.herdr.workspace_id,
                "agent": "codex",
                "agent_status": "idle",
            }
        )
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "carries no pane locator")

    def test_a_row_whose_workspace_contradicts_its_locator_refuses(self):
        env = _Env(self, PROJECT_A, PROJECT_B)
        tab = env.herdr.new_tab()
        env.seed_columns(tab, (PROJECT_A, ""))
        launched = env.append_pair(tab, PROJECT_B)
        env.herdr.extra_rows.append(
            {
                "name": env.name(PROJECT_A, "codex"),
                "pane_id": "w9:p1",
                "workspace_id": env.herdr.workspace_id,
                "agent": "codex",
                "agent_status": "idle",
            }
        )
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "while its locator says")

    def test_a_run_claiming_a_project_the_workspace_does_not_hold_refuses(self):
        """Review j#99938 finding_2 — it reported a column for a phantom project.

        The exact join proved the slots were self-consistent inside the inventory,
        which is not the same as proving they are the project the run says it
        launched. Two authorities for "which pair is ours" is what made that
        possible, so there is one now and the run's claim is an input to it.
        """
        env = _Env(self, PROJECT_A, PROJECT_B, PROJECT_C)
        tab = env.herdr.new_tab()
        (a_pair, _b_pair) = env.seed_columns(tab, (PROJECT_A, ""), (PROJECT_B, ""))
        # A result that names project C while carrying project A's live panes.
        result = SessionStartResult(
            workspace_id=env.ids[PROJECT_C], lane_id=DEFAULT_LANE
        )
        result.herdr_workspace_id = env.herdr.workspace_id
        for provider, locator in zip(("codex", "claude"), a_pair):
            result.slots.append(
                SlotResult(
                    provider=provider,
                    assigned_name=env.name(PROJECT_A, provider),
                    outcome=SLOT_LAUNCHED,
                    locator=locator,
                    health=HEALTH_HEALTHY,
                )
            )
        outcome, detail = env.run(result)
        self._assert_zero_move_failure(
            env, outcome, detail, "the workspace does not corroborate"
        )

    def test_a_hibernated_delegated_lane_is_not_an_active_coordinator(self):
        """Review j#99931 finding_3 — the kind alone let its survivors be moved."""
        env, launched = self._with_named_lane("delegated-1")
        env.declare_lane(PROJECT_A, "delegated-1", LANE_KIND_DELEGATED_COORDINATOR)
        store = LaneLifecycleStore(home=env.home)
        key = LaneLifecycleKey(env.ids[PROJECT_A], "delegated-1")
        store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=store.get(key).revision,
            target=DISPOSITION_HIBERNATED,
            decision=DecisionPointer(
                source="redmine", issue_id="14996", journal_id="99931"
            ),
        )
        outcome, detail = env.run(env.result(PROJECT_B, list(launched)))
        self._assert_zero_move_failure(env, outcome, detail, "not 'active'")

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

    def test_failed_attach_recovery_restores_the_existing_unit_ratio(self):
        env, tab, project_a, project_b = self._scenario()
        self.assertTrue(tab.resize(project_a[0], "up", 0.15))
        before = env.herdr.rects()
        env.herdr.refuse_move_attempts.add(4)

        outcome, detail = env.run(env.result(PROJECT_B, list(project_b)))

        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("every detached pane was returned", detail)
        after = env.herdr.rects()
        self.assertEqual(after[project_a[0]][3], before[project_a[0]][3])
        self.assertEqual(after[project_a[1]][3], before[project_a[1]][3])
        restore = next(
            call
            for call in env.moves()
            if call[2] == project_a[1] and "--tab" in call
        )
        self.assertEqual(restore[restore.index("--ratio") + 1], "0.35")

    def test_accepted_move_that_ignores_ratio_is_not_reported_as_applied(self):
        env, tab, project_a, project_b = self._scenario()
        self.assertTrue(tab.resize(project_a[0], "up", 0.15))
        env.herdr.move_ratio_ignored.add(project_a[1])

        outcome, detail = env.run(env.result(PROJECT_B, list(project_b)))

        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("internal ratio changed", detail)
        self.assertIn("observed_ratio=0.5", detail)

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
