"""Interaction tests for the preview-first Unit Board placement UI (#15116)."""

from __future__ import annotations

import unittest
from io import StringIO

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AgentCell,
    SOURCE_LIVE,
    UnitBoardRow,
    UnitBoardSnapshot,
    unavailable_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_live_pair_placement import (
    APPLY_APPLIED,
    APPLY_PARTIAL,
    APPLY_REFUSED,
    PLAN_MATCHED,
    PLAN_READY,
    PLAN_REFUSED,
    REASON_OK,
    REASON_POSTCONDITION_FAILED,
    REASON_STALE,
    PlacementApplyResult,
    PlacementPlan,
    PlacementTarget,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_unit_board_placement_ui import (
    HerdrUnitBoardPlacementUI,
)


WORKSPACE_A = "a" * 32
WORKSPACE_B = "b" * 32


def row(
    workspace_id: str,
    project: str,
    pane: str,
    *,
    lane_id: str = "default",
    unit_id: str | None = None,
) -> UnitBoardRow:
    return UnitBoardRow(
        unit_id=unit_id or f"unit-{workspace_id[:8]}",
        workspace_id=workspace_id,
        lane_id=lane_id,
        project_label=project,
        workflow_role="coordinator",
        responsibility=f"operate {project}",
        work_label="default lane",
        authority_state="resolved",
        identity_state="resolved",
        agents=(AgentCell("codex", "idle", True, pane),),
    )


def snapshot(*rows: UnitBoardRow) -> UnitBoardSnapshot:
    return UnitBoardSnapshot(SOURCE_LIVE, "now", tuple(rows))


def ready(workspace_id: str, *, detail: str = "placement differs") -> PlacementPlan:
    return PlacementPlan(
        PLAN_READY,
        REASON_OK,
        detail,
        workspace_id,
        "default",
        target=PlacementTarget("down", ("codex", "claude"), 0.5),
        current_split="right",
        current_order=("claude", "codex"),
        current_ratio=0.7,
        operations=("change_split", "resize_ratio"),
    )


class FakeBoard:
    def __init__(
        self,
        *snapshots: UnitBoardSnapshot | Exception,
        identities: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0
        first = snapshots[0] if snapshots else None
        self.identities = (
            identities
            if identities is not None
            else (
                {
                    item.unit_id: (item.workspace_id, item.lane_id)
                    for item in first.units
                }
                if isinstance(first, UnitBoardSnapshot)
                else {}
            )
        )

    def snapshot(self) -> UnitBoardSnapshot:
        self.calls += 1
        if len(self.snapshots) > 1:
            current = self.snapshots.pop(0)
        else:
            current = self.snapshots[0]
        if isinstance(current, Exception):
            raise current
        return current

    def action_identity(self, unit_id: str) -> tuple[str, str] | None:
        return self.identities.get(unit_id)


class FakePlacement:
    def __init__(self, plan: PlacementPlan, result: PlacementApplyResult | None = None) -> None:
        self.plan = plan
        self.result = result
        self.preview_calls: list[tuple[str, str]] = []
        self.apply_calls: list[tuple[str, str]] = []
        self.raise_preview = False
        self.raise_apply = False

    def preview(self, workspace_id: str, lane_id: str = "default") -> PlacementPlan:
        self.preview_calls.append((workspace_id, lane_id))
        if self.raise_preview:
            raise RuntimeError("/synthetic/private/preview")
        return self.plan

    def apply(self, workspace_id: str, lane_id: str = "default") -> PlacementApplyResult:
        self.apply_calls.append((workspace_id, lane_id))
        if self.raise_apply:
            raise RuntimeError("/synthetic/private/apply")
        if self.result is None:
            raise AssertionError("test did not provide an apply result")
        return self.result


def run_ui(board, placement, commands: str) -> tuple[int, str]:
    output = StringIO()
    result = HerdrUnitBoardPlacementUI(
        board,
        placement,
        input_stream=StringIO(commands),
        output_stream=output,
    ).run()
    return result, output.getvalue()


class HerdrUnitBoardPlacementUITests(unittest.TestCase):
    def test_board_identifies_project_role_responsibility_without_pane_locator(self) -> None:
        placement = FakePlacement(ready(WORKSPACE_A))

        result, output = run_ui(
            FakeBoard(snapshot(row(WORKSPACE_A, "accounting", "w1:p-secret"))),
            placement,
            "q\n",
        )

        self.assertEqual(result, 0)
        self.assertIn("project=accounting", output)
        self.assertIn("role=coordinator", output)
        self.assertIn("responsibility=operate accounting", output)
        self.assertNotIn("w1:p-secret", output)
        self.assertEqual(placement.preview_calls, [])
        self.assertEqual(placement.apply_calls, [])

    def test_ready_preview_then_explicit_apply_uses_selected_unit_identity(self) -> None:
        plan = ready(WORKSPACE_B)
        after = PlacementPlan(
            PLAN_MATCHED,
            REASON_OK,
            "matched",
            WORKSPACE_B,
            "default",
            target=plan.target,
            current_split="down",
            current_order=("codex", "claude"),
            current_ratio=0.5,
        )
        applied = PlacementApplyResult(
            APPLY_APPLIED, REASON_OK, "measured", plan, after
        )
        placement = FakePlacement(plan, applied)
        units = snapshot(
            row(WORKSPACE_A, "accounting", "w1:p1"),
            row(WORKSPACE_B, "it-operations", "w1:p2"),
        )

        result, output = run_ui(FakeBoard(units), placement, "j\np\na\nq\n")

        self.assertEqual(result, 0)
        self.assertEqual(placement.preview_calls, [(WORKSPACE_B, "default")])
        self.assertEqual(placement.apply_calls, [(WORKSPACE_B, "default")])
        self.assertIn("preview: status=ready", output)
        self.assertIn("apply: status=applied", output)
        self.assertNotIn("w1:p2", output)

    def test_truncated_display_identity_never_selects_the_wrong_long_lane(self) -> None:
        lane_a = "x" * 80
        lane_b = "x" * 81
        unit_a = "unit-long-a"
        unit_b = "unit-long-b"
        units = snapshot(
            row(WORKSPACE_A, "accounting", "w1:p1", lane_id=lane_a, unit_id=unit_a),
            row(WORKSPACE_A, "accounting", "w1:p2", lane_id=lane_a, unit_id=unit_b),
        )
        plan = PlacementPlan(
            PLAN_READY,
            REASON_OK,
            "placement differs",
            WORKSPACE_A,
            lane_b,
            target=PlacementTarget("down", ("codex", "claude"), 0.5),
        )
        placement = FakePlacement(plan)
        board = FakeBoard(
            units,
            identities={
                unit_a: (WORKSPACE_A, lane_a),
                unit_b: (WORKSPACE_A, lane_b),
            },
        )

        result, _ = run_ui(board, placement, "j\np\nq\n")

        self.assertEqual(result, 0)
        self.assertEqual(placement.preview_calls, [(WORKSPACE_A, lane_b)])

    def test_unresolved_action_identity_is_zero_io(self) -> None:
        unit = row(WORKSPACE_A, "accounting", "w1:p1")
        placement = FakePlacement(ready(WORKSPACE_A))

        result, output = run_ui(
            FakeBoard(snapshot(unit), identities={}), placement, "p\na\nq\n"
        )

        self.assertEqual(result, 0)
        self.assertEqual(placement.preview_calls, [])
        self.assertEqual(placement.apply_calls, [])
        self.assertIn("Placement preview is unavailable", output)

    def test_apply_without_ready_preview_is_zero_write(self) -> None:
        placement = FakePlacement(ready(WORKSPACE_A))

        result, output = run_ui(
            FakeBoard(snapshot(row(WORKSPACE_A, "accounting", "w1:p1"))),
            placement,
            "a\nq\n",
        )

        self.assertEqual(result, 0)
        self.assertEqual(placement.preview_calls, [])
        self.assertEqual(placement.apply_calls, [])
        self.assertIn("Preview this selected Unit", output)

    def test_refused_or_matched_preview_cannot_apply(self) -> None:
        for status in (PLAN_REFUSED, PLAN_MATCHED):
            with self.subTest(status=status):
                plan = PlacementPlan(
                    status,
                    REASON_STALE if status == PLAN_REFUSED else REASON_OK,
                    "not applicable",
                    WORKSPACE_A,
                    "default",
                )
                placement = FakePlacement(plan)
                result, _ = run_ui(
                    FakeBoard(snapshot(row(WORKSPACE_A, "accounting", "w1:p1"))),
                    placement,
                    "p\na\nq\n",
                )

                self.assertEqual(result, 0)
                self.assertEqual(placement.preview_calls, [(WORKSPACE_A, "default")])
                self.assertEqual(placement.apply_calls, [])

    def test_selection_change_and_refresh_clear_preview(self) -> None:
        units = snapshot(
            row(WORKSPACE_A, "accounting", "w1:p1"),
            row(WORKSPACE_B, "it-operations", "w1:p2"),
        )
        for commands in ("p\nj\na\nq\n", "p\nr\na\nq\n"):
            with self.subTest(commands=commands):
                placement = FakePlacement(ready(WORKSPACE_A))
                result, _ = run_ui(FakeBoard(units, units), placement, commands)

                self.assertEqual(result, 0)
                self.assertEqual(placement.apply_calls, [])

    def test_backend_exceptions_and_private_details_are_not_reflected(self) -> None:
        private_path = "/synthetic/private/placement-secret"
        placement = FakePlacement(ready(WORKSPACE_A, detail=private_path))
        placement.raise_apply = True

        result, output = run_ui(
            FakeBoard(snapshot(row(WORKSPACE_A, "accounting", "w1:p1"))),
            placement,
            "p\na\nq\n",
        )

        self.assertEqual(result, 0)
        self.assertNotIn(private_path, output)
        self.assertNotIn("/synthetic/private/apply", output)
        self.assertIn("[redacted]", output)
        self.assertIn(f"apply: status={APPLY_PARTIAL}", output)
        self.assertIn(f"reason={REASON_POSTCONDITION_FAILED}", output)
        self.assertIn("Do not retry", output)
        self.assertEqual(placement.apply_calls, [(WORKSPACE_A, "default")])

    def test_apply_result_survives_board_refresh_failure_without_blind_retry(self) -> None:
        plan = ready(WORKSPACE_A)
        after = PlacementPlan(
            PLAN_MATCHED,
            REASON_OK,
            "matched",
            WORKSPACE_A,
            "default",
            target=plan.target,
        )
        placement = FakePlacement(
            plan, PlacementApplyResult(APPLY_APPLIED, REASON_OK, "measured", plan, after)
        )

        result, output = run_ui(
            FakeBoard(
                snapshot(row(WORKSPACE_A, "accounting", "w1:p1")),
                RuntimeError("/synthetic/private/refresh"),
            ),
            placement,
            "p\na\nq\n",
        )

        self.assertEqual(result, 0)
        self.assertEqual(placement.apply_calls, [(WORKSPACE_A, "default")])
        self.assertIn("apply: status=applied", output)
        self.assertIn("do not retry until refresh succeeds", output)
        self.assertNotIn("/synthetic/private/refresh", output)

    def test_unavailable_or_empty_board_exits_without_placement_io(self) -> None:
        cases = (
            unavailable_snapshot("unavailable", observed_at="now", detail="private"),
            snapshot(),
        )
        for current in cases:
            with self.subTest(source=current.source_state, count=len(current.units)):
                placement = FakePlacement(ready(WORKSPACE_A))
                result, output = run_ui(FakeBoard(current), placement, "p\na\n")

                self.assertEqual(result, 1)
                self.assertIn("unavailable or has no managed Units", output)
                self.assertEqual(placement.preview_calls, [])
                self.assertEqual(placement.apply_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
