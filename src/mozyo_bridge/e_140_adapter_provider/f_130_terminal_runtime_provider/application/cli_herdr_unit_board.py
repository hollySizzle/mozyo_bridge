"""CLI and plugin entrypoints for the Herdr coordinator Unit board."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    MAX_BOARD_WIDTH,
    SOURCE_UNAVAILABLE,
    UnitBoardSnapshot,
    format_board,
    unavailable_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_unit_board_runtime import (
    HerdrUnitBoardRuntime,
    MetadataSyncFailure,
    MetadataSyncReport,
    resolve_unit_board_binary,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_live_pair_placement import (
    production_live_pair_placement,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.coordinator_placement_loader import (
    load_coordinator_placement_for_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement import (
    HerdrProjectColumnPlacement,
    ProjectColumnPlacementPreview,
    ProjectColumnPlacementResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement_model import (
    PLACEMENT_REFUSED,
    REASON_ADJUSTMENT_INVALID,
    REASON_AUTHORITY_UNVERIFIED,
    refused_preview,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_plan import (
    UnitColumnKey,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_unit_board_placement_ui import (
    HerdrUnitBoardPlacementUI,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


def _runtime() -> HerdrUnitBoardRuntime:
    return HerdrUnitBoardRuntime(resolve_unit_board_binary())


class _UnitColumnPlacementActions:
    """Composition facade keeping Herdr workspace handles out of the popup UI."""

    def __init__(
        self,
        runtime: HerdrUnitBoardRuntime,
        *,
        binary: str,
        top_workspace_id: str,
    ) -> None:
        self._runtime = runtime
        self._binary = binary
        self._top_workspace_id = top_workspace_id

    def _service(self, target_workspace: str) -> HerdrProjectColumnPlacement:
        return HerdrProjectColumnPlacement(
            home=mozyo_bridge_home(),
            target_workspace=target_workspace,
            top_workspace_id=self._top_workspace_id,
            binary=self._binary,
            runner=subprocess.run,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )

    def preview(
        self, unit_id: str, adjustment: str
    ) -> ProjectColumnPlacementPreview:
        context = self._runtime.column_action_context(unit_id)
        if context is None:
            return refused_preview(
                REASON_AUTHORITY_UNVERIFIED,
                "the selected Unit could not be resolved in one live Herdr workspace",
            )
        return self._service(context.herdr_workspace_id).preview_adjustment(
            UnitColumnKey(context.workspace_id, context.lane_id), adjustment
        )

    def apply(
        self, preview: ProjectColumnPlacementPreview
    ) -> ProjectColumnPlacementResult:
        evidence = preview.evidence
        if evidence is None:
            return ProjectColumnPlacementResult(
                PLACEMENT_REFUSED,
                REASON_ADJUSTMENT_INVALID,
                "a fresh applicable Unit-column adjustment preview is required",
                preview,
                preview,
                "Preview the selected Unit-column adjustment again.",
            )
        return self._service(evidence.target_workspace).apply_adjustment(preview)


def _board_width_arg(value: str) -> int:
    try:
        width = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("width must be an integer") from exc
    if not 1 <= width <= MAX_BOARD_WIDTH:
        raise argparse.ArgumentTypeError(
            f"width must be between 1 and {MAX_BOARD_WIDTH}"
        )
    return width


def _runtime_failure_snapshot() -> UnitBoardSnapshot:
    return unavailable_snapshot(
        SOURCE_UNAVAILABLE,
        observed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        detail="Herdr Unit board runtime is unavailable",
    )


def _load_runtime(
) -> tuple[HerdrUnitBoardRuntime | None, UnitBoardSnapshot | None]:
    """Resolve the runtime without reflecting an exception or injected path."""
    try:
        return _runtime(), None
    except Exception:
        return None, _runtime_failure_snapshot()


def _snapshot(runtime: HerdrUnitBoardRuntime) -> UnitBoardSnapshot:
    try:
        return runtime.snapshot()
    except Exception:
        return _runtime_failure_snapshot()


def _runtime_failure_report() -> MetadataSyncReport:
    return MetadataSyncReport(
        source_state=SOURCE_UNAVAILABLE,
        attempted=0,
        updated=0,
        failures=(
            MetadataSyncFailure(
                unit_id="board",
                provider="unknown",
                reason="runtime_unavailable",
            ),
        ),
    )


def cmd_herdr_unit_board_show(args: argparse.Namespace) -> int:
    runtime, failure = _load_runtime()
    if failure is not None:
        snapshot = failure
    else:
        assert runtime is not None
        snapshot = _snapshot(runtime)
    if getattr(args, "json", False):
        print(json.dumps(snapshot.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        width = int(getattr(args, "width", 0) or shutil.get_terminal_size((120, 24)).columns)
        print(format_board(snapshot, width=width))
    return 0 if snapshot.ok else 1


def cmd_herdr_unit_board_sync(args: argparse.Namespace) -> int:
    runtime, failure = _load_runtime()
    if failure is not None:
        report = _runtime_failure_report()
    else:
        assert runtime is not None
        try:
            report = runtime.sync_metadata()
        except Exception:
            report = _runtime_failure_report()
    quiet = bool(getattr(args, "quiet", False))
    if getattr(args, "json", False):
        print(json.dumps(report.as_payload(), ensure_ascii=False, sort_keys=True))
    elif not quiet or not report.ok:
        state = "ok" if report.ok else "failed"
        print(
            f"Herdr Unit metadata sync: {state}; "
            f"updated={report.updated}/{report.attempted}; "
            f"source={report.source_state}"
        )
    return 0 if report.ok else 1


def cmd_herdr_unit_board_watch(args: argparse.Namespace) -> int:
    interval = float(getattr(args, "interval", 2.0))
    if not 0.5 <= interval <= 60.0:
        print("error: --interval must be between 0.5 and 60 seconds")
        return 2
    runtime, failure = _load_runtime()
    if failure is not None:
        width = shutil.get_terminal_size((120, 24)).columns
        print(format_board(failure, width=width), flush=True)
        return 1
    assert runtime is not None
    try:
        while True:
            snapshot = _snapshot(runtime)
            width = shutil.get_terminal_size((120, 24)).columns
            if sys.stdout.isatty():
                print("\x1b[2J\x1b[H", end="")
            print(format_board(snapshot, width=width), flush=True)
            if not snapshot.ok:
                return 1
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_herdr_unit_board_interact(args: argparse.Namespace) -> int:
    runtime, failure = _load_runtime()
    if failure is not None:
        print("Herdr Unit board is unavailable or has no managed Units.")
        return 1
    assert runtime is not None
    try:
        placement = production_live_pair_placement()
    except Exception:
        print("Herdr Unit board placement actions are unavailable.")
        return 1
    column_placement = None
    try:
        coordinator_placement = load_coordinator_placement_for_launch()
        column_placement = _UnitColumnPlacementActions(
            runtime,
            binary=resolve_unit_board_binary(),
            top_workspace_id=coordinator_placement.top_workspace_id,
        )
    except Exception:
        column_placement = None
    return HerdrUnitBoardPlacementUI(
        runtime, placement, column_placement
    ).run()


def register_herdr_unit_board_parser(herdr_sub) -> None:
    board = herdr_sub.add_parser(
        "unit-board",
        help=(
            "Display managed Herdr Units by project, workflow role, work label, and "
            "runtime state; optionally refresh labels or open preview-first placement."
        ),
        description=(
            "Read the live Herdr inventory and reviewable mozyo identity metadata, "
            "then render a public-safe Unit board. The interactive command may call "
            "identity-bound pair or shared-column placement services after preview; "
            "it does not send agent input or write Redmine/workflow state."
        ),
    )
    sub = board.add_subparsers(dest="unit_board_command", required=True)

    show = sub.add_parser("show", help="Print one read-only Unit board snapshot.")
    show.add_argument("--json", action="store_true", help="Emit structured JSON.")
    show.add_argument(
        "--width",
        type=_board_width_arg,
        default=0,
        help="Render width for text output (default: current terminal width).",
    )
    show.set_defaults(func=cmd_herdr_unit_board_show)

    sync = sub.add_parser(
        "sync",
        help="Refresh Herdr display-only metadata for each managed agent pane.",
    )
    sync.add_argument("--json", action="store_true", help="Emit structured JSON.")
    sync.add_argument(
        "--quiet",
        action="store_true",
        help="Print nothing on success (plugin startup/event hook mode).",
    )
    sync.set_defaults(func=cmd_herdr_unit_board_sync)

    watch = sub.add_parser(
        "watch",
        help="Continuously refresh the terminal Unit board until the pane closes.",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Refresh interval in seconds, 0.5..60 (default: 2).",
    )
    watch.set_defaults(func=cmd_herdr_unit_board_watch)

    interact = sub.add_parser(
        "interact",
        help=(
            "Open the keyboard Unit board; preview a selected managed pair or "
            "shared Unit-column adjustment, then apply only after confirmation."
        ),
    )
    interact.set_defaults(func=cmd_herdr_unit_board_interact)


__all__ = (
    "cmd_herdr_unit_board_show",
    "cmd_herdr_unit_board_sync",
    "cmd_herdr_unit_board_watch",
    "cmd_herdr_unit_board_interact",
    "register_herdr_unit_board_parser",
)
