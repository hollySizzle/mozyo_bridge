"""CLI and plugin entrypoints for the Herdr coordinator Unit board."""

from __future__ import annotations

import argparse
import json
import shutil
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


def _runtime() -> HerdrUnitBoardRuntime:
    return HerdrUnitBoardRuntime(resolve_unit_board_binary())


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


def register_herdr_unit_board_parser(herdr_sub) -> None:
    board = herdr_sub.add_parser(
        "unit-board",
        help=(
            "Display managed Herdr Units by project, workflow role, work label, and "
            "runtime state; optionally refresh display-only pane metadata."
        ),
        description=(
            "Read the live Herdr inventory and reviewable mozyo identity metadata, "
            "then render a public-safe Unit board. This is presentation only: it "
            "does not send agent input, move panes, or write Redmine/workflow state."
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


__all__ = (
    "cmd_herdr_unit_board_show",
    "cmd_herdr_unit_board_sync",
    "cmd_herdr_unit_board_watch",
    "register_herdr_unit_board_parser",
)
