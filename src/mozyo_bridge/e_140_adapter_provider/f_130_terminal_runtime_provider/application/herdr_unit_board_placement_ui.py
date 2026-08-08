"""Keyboard UI for preview-first placement of one Unit Board row (#15116).

The UI keeps volatile pane locators out of its contract.  A selection retains
only the managed ``(workspace_id, lane_id)`` identity already carried by the
public-safe board row, while the placement service resolves and revalidates the
live panes at preview and again at apply time.
"""

from __future__ import annotations

import math
import sys
from typing import Protocol, TextIO

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    UnitBoardRow,
    UnitBoardSnapshot,
    safe_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_live_pair_placement import (
    PlacementApplyResult,
    PlacementPlan,
)


class UnitBoardSource(Protocol):
    def snapshot(self) -> UnitBoardSnapshot: ...


class PairPlacementActions(Protocol):
    def preview(self, workspace_id: str, lane_id: str = "default") -> PlacementPlan: ...

    def apply(
        self, workspace_id: str, lane_id: str = "default"
    ) -> PlacementApplyResult: ...


def _ratio(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    number = float(value)
    return f"{number:.3g}" if math.isfinite(number) else "unknown"


def _order(values: object) -> str:
    if not isinstance(values, (tuple, list)):
        return "unknown"
    rendered = [safe_text(value) for value in values]
    return ",".join(rendered) if rendered else "none"


def _unit_key(row: UnitBoardRow) -> tuple[str, str]:
    return row.workspace_id, row.lane_id


def _unit_line(row: UnitBoardRow, *, selected: bool, number: int) -> str:
    agents = ", ".join(
        f"{safe_text(agent.provider)}={safe_text(agent.runtime_state)}"
        for agent in row.agents
    ) or "none"
    marker = ">" if selected else " "
    return (
        f"{marker} {number}. project={safe_text(row.project_label)} | "
        f"role={safe_text(row.workflow_role)} | "
        f"responsibility={safe_text(row.responsibility)} | "
        f"work={safe_text(row.work_label)} | agents={agents}"
    )


def _plan_lines(plan: PlacementPlan) -> tuple[str, ...]:
    lines = (
        f"preview: status={safe_text(plan.status)} reason={safe_text(plan.reason)}",
        f"detail: {safe_text(plan.detail)}",
        "current: "
        f"split={safe_text(plan.current_split, fallback='unknown')} "
        f"order={_order(plan.current_order)} ratio={_ratio(plan.current_ratio)}",
    )
    if plan.target is None:
        return lines
    return lines + (
        "target: "
        f"split={safe_text(plan.target.split)} "
        f"order={_order(plan.target.order)} ratio={_ratio(plan.target.ratio)}",
        "operations: "
        + (", ".join(safe_text(item) for item in plan.operations) or "none"),
    )


def _result_lines(result: PlacementApplyResult) -> tuple[str, ...]:
    lines = (
        f"apply: status={safe_text(result.status)} reason={safe_text(result.reason)}",
        f"detail: {safe_text(result.detail)}",
    )
    if result.recovery:
        lines += (f"recovery: {safe_text(result.recovery)}",)
    return lines


class HerdrUnitBoardPlacementUI:
    """Small terminal interaction loop owned by the Unit Board plugin pane."""

    def __init__(
        self,
        board: UnitBoardSource,
        placement: PairPlacementActions,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._board = board
        self._placement = placement
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout

    def _read_command(self) -> str:
        self._output.write("command> ")
        self._output.flush()
        if not self._input.isatty():
            return self._input.readline().strip().casefold()
        try:
            import termios
            import tty

            descriptor = self._input.fileno()
            previous = termios.tcgetattr(descriptor)
            try:
                tty.setcbreak(descriptor)
                value = self._input.read(1)
            finally:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        except (AttributeError, OSError, termios.error):
            value = self._input.readline()
        self._output.write(f"{value}\n")
        self._output.flush()
        return value.strip().casefold()

    def _snapshot(self) -> UnitBoardSnapshot | None:
        try:
            snapshot = self._board.snapshot()
        except Exception:
            return None
        return snapshot if snapshot.ok and snapshot.units else None

    def _render(
        self,
        snapshot: UnitBoardSnapshot,
        selected: int,
        preview: PlacementPlan | None,
        result: PlacementApplyResult | None,
        message: str,
    ) -> None:
        if self._output.isatty():
            self._output.write("\x1b[2J\x1b[H")
        self._output.write("mozyo Unit board — project / role / responsibility\n")
        for index, row in enumerate(snapshot.units):
            self._output.write(
                _unit_line(row, selected=index == selected, number=index + 1) + "\n"
            )
        self._output.write("\nkeys: j/k select, p preview, a apply, r refresh, q close\n")
        if preview is not None:
            self._output.write("\n" + "\n".join(_plan_lines(preview)) + "\n")
        if result is not None:
            self._output.write("\n" + "\n".join(_result_lines(result)) + "\n")
        if message:
            self._output.write("\n" + safe_text(message) + "\n")
        self._output.flush()

    def run(self) -> int:
        snapshot = self._snapshot()
        if snapshot is None:
            self._output.write("Herdr Unit board is unavailable or has no managed Units.\n")
            return 1
        selected = 0
        preview: PlacementPlan | None = None
        preview_key: tuple[str, str] | None = None
        result: PlacementApplyResult | None = None
        message = ""
        while True:
            self._render(snapshot, selected, preview, result, message)
            try:
                command = self._read_command()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not command or command == "q":
                return 0
            result = None
            message = ""
            if command in {"j", "down"}:
                selected = min(selected + 1, len(snapshot.units) - 1)
                preview = None
                preview_key = None
                continue
            if command in {"k", "up"}:
                selected = max(selected - 1, 0)
                preview = None
                preview_key = None
                continue
            if command == "r":
                selected_key = _unit_key(snapshot.units[selected])
                refreshed = self._snapshot()
                if refreshed is None:
                    message = "Unit board refresh was unavailable; no pane was changed."
                    preview = None
                    preview_key = None
                    continue
                snapshot = refreshed
                selected = next(
                    (
                        index
                        for index, row in enumerate(snapshot.units)
                        if _unit_key(row) == selected_key
                    ),
                    0,
                )
                preview = None
                preview_key = None
                continue
            row = snapshot.units[selected]
            selected_key = _unit_key(row)
            if command == "p":
                try:
                    preview = self._placement.preview(*selected_key)
                    preview_key = selected_key
                except Exception:
                    preview = None
                    preview_key = None
                    message = "Placement preview is unavailable; no pane was changed."
                continue
            if command == "a":
                if preview is None or preview_key != selected_key or not preview.can_apply:
                    message = "Preview this selected Unit before applying a change."
                    continue
                try:
                    result = self._placement.apply(*selected_key)
                except Exception:
                    message = "Placement apply is unavailable; inspect the Unit and preview again."
                preview = None
                preview_key = None
                refreshed = self._snapshot()
                if refreshed is not None:
                    snapshot = refreshed
                    selected = next(
                        (
                            index
                            for index, item in enumerate(snapshot.units)
                            if _unit_key(item) == selected_key
                        ),
                        0,
                    )
                continue
            message = "Unknown key; no pane was changed."


__all__ = (
    "HerdrUnitBoardPlacementUI",
    "PairPlacementActions",
    "UnitBoardSource",
)
