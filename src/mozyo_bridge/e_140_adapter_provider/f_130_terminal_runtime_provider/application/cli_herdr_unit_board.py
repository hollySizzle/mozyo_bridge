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
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_aggregate import (
    format_multi_source_board,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSourceError,
    UnitBoardSourcesConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.unit_board_sources_loader import (
    load_unit_board_sources,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.remote_unit_action import (
    ACTION_KINDS,
    DEFAULT_ACTION_KIND,
    RemoteUnitActionRail,
    RemoteUnitActionRequest,
    render_preview,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
    MultiSourceUnitBoardRuntime,
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


#: Floor on the ``watch`` refresh cadence once more than the local server is
#: observed.  Remote observation is a connection per source per refresh.
MIN_MULTI_SOURCE_INTERVAL_SECONDS = 5.0


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


def _snapshot(
    runtime: HerdrUnitBoardRuntime | MultiSourceUnitBoardRuntime,
) -> UnitBoardSnapshot:
    try:
        return runtime.snapshot()
    except Exception:
        return _runtime_failure_snapshot()


def _sources_config() -> tuple[UnitBoardSourcesConfig | None, str]:
    """Load the operator's observable sources, or explain why it failed.

    A present-but-broken source file is not silently downgraded to local-only:
    an operator who configured remote hosts and then broke the file would
    otherwise be shown a local board that looks complete.
    """
    try:
        return load_unit_board_sources(), ""
    except UnitBoardSourceError as exc:
        return None, str(exc)


def _multi_source_runtime(
    config: UnitBoardSourcesConfig,
) -> MultiSourceUnitBoardRuntime:
    """Build the merged-board runtime without pre-resolving the local server.

    The local Herdr binary is resolved lazily inside the runtime so that a local
    server the client cannot reach degrades to one visible ``unavailable`` source
    row instead of hiding every remote source behind a local failure.
    """
    return MultiSourceUnitBoardRuntime(config)


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
    if getattr(args, "local_only", False):
        # This host answering for itself alone.  A client aggregating several
        # servers asks with this flag so the answer describes one server; without
        # it, a host that has its own sources would answer with *its* merged
        # board and its rows would describe servers the caller never asked about
        # (Redmine #15138 review j#101787 f2).
        config: UnitBoardSourcesConfig | None = UnitBoardSourcesConfig.default()
        config_error = ""
    else:
        config, config_error = _sources_config()
    if config is None:
        print(f"error: {config_error}", file=sys.stderr)
        return 2
    if config.is_local_only:
        runtime, failure = _load_runtime()
        if failure is not None:
            snapshot = failure
        else:
            assert runtime is not None
            snapshot = _snapshot(runtime)
    else:
        try:
            snapshot = _multi_source_runtime(config).snapshot()
        except Exception:
            snapshot = _runtime_failure_snapshot()
    if getattr(args, "json", False):
        print(json.dumps(snapshot.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        width = int(getattr(args, "width", 0) or shutil.get_terminal_size((120, 24)).columns)
        renderer = format_board if config.is_local_only else format_multi_source_board
        print(renderer(snapshot, width=width))
    return 0 if snapshot.ok else 1


def cmd_herdr_unit_board_sources(args: argparse.Namespace) -> int:
    """Show each configured source's identity and whether it may be acted on.

    Diagnostics only: it prints host identity, kind, state, and Unit count, and
    never the ssh destination, container name, or remote binary that reached it.
    Exits non-zero when any source is not live, because this surface exists to
    answer "can I act on all of them right now?".
    """
    config, config_error = _sources_config()
    if config is None:
        print(f"error: {config_error}", file=sys.stderr)
        return 2
    try:
        statuses = tuple(
            observation.status
            for observation in _multi_source_runtime(config).observe()
        )
    except Exception:
        print("error: Herdr Unit board sources could not be observed", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"sources": [status.as_payload() for status in statuses]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        for status in statuses:
            marker = "ok " if status.actionable else "!! "
            line = (
                f"{marker}{status.host_label} [{status.host_kind}] "
                f"{status.source_state} units={status.unit_count}"
            )
            if status.detail:
                line = f"{line} — {status.detail}"
            print(line)
    return 0 if all(status.actionable for status in statuses) else 1


def cmd_herdr_unit_board_action(args: argparse.Namespace) -> int:
    """Preview — and only with ``--apply`` deliver — one remote Unit action."""
    config, config_error = _sources_config()
    if config is None:
        print(f"error: {config_error}", file=sys.stderr)
        return 2
    multi = _multi_source_runtime(config)
    request = RemoteUnitActionRequest(
        unit_id=str(getattr(args, "unit", "") or ""),
        issue=str(getattr(args, "issue", "") or ""),
        journal=str(getattr(args, "journal", "") or ""),
        summary=str(getattr(args, "summary", "") or ""),
        target_project=str(getattr(args, "target_project", "") or ""),
        kind=str(getattr(args, "kind", DEFAULT_ACTION_KIND)),
    )
    rail = RemoteUnitActionRail(multi)
    preview = rail.preview(request)
    as_json = bool(getattr(args, "json", False))
    if not getattr(args, "apply", False):
        if as_json:
            print(json.dumps(preview.as_payload(), ensure_ascii=False, sort_keys=True))
        else:
            for line in render_preview(preview):
                print(line)
        return 0 if preview.applicable else 1
    result = rail.apply(preview)
    if as_json:
        print(json.dumps(result.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"remote Unit action: {result.state} ({result.reason})")
        print(f"  {result.detail}")
    return 0 if result.delivered else 1


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
    config, config_error = _sources_config()
    if config is None:
        print(f"error: {config_error}", file=sys.stderr)
        return 2
    if not config.is_local_only:
        # Each remote source costs a connection per refresh.  A 2-second local
        # cadence would turn the board into a connection storm, so the floor
        # rises with the fan-out instead of silently hammering the hosts.
        interval = max(interval, MIN_MULTI_SOURCE_INTERVAL_SECONDS)
    if config.is_local_only:
        runtime, failure = _load_runtime()
        if failure is not None:
            width = shutil.get_terminal_size((120, 24)).columns
            print(format_board(failure, width=width), flush=True)
            return 1
        assert runtime is not None
        board: HerdrUnitBoardRuntime | MultiSourceUnitBoardRuntime = runtime
        renderer = format_board
    else:
        board = _multi_source_runtime(config)
        renderer = format_multi_source_board
    try:
        while True:
            snapshot = _snapshot(board)
            width = shutil.get_terminal_size((120, 24)).columns
            if sys.stdout.isatty():
                print("\x1b[2J\x1b[H", end="")
            print(renderer(snapshot, width=width), flush=True)
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
        "--local-only",
        action="store_true",
        dest="local_only",
        help=(
            "Report only this host's own Herdr server, ignoring any configured "
            "observation sources. Used by a client aggregating this host, so the "
            "answer always describes exactly one server."
        ),
    )
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

    sources = sub.add_parser(
        "sources",
        help=(
            "Show each configured Herdr observation source, its state, and "
            "whether it is currently action authority."
        ),
        description=(
            "Read-only diagnostics for the operator-scoped source set. Prints "
            "host identity, kind, state, and Unit count only — never the ssh "
            "destination, container name, or remote binary used to reach it."
        ),
    )
    sources.add_argument("--json", action="store_true", help="Emit structured JSON.")
    sources.set_defaults(func=cmd_herdr_unit_board_sources)

    action = sub.add_parser(
        "action",
        help=(
            "Preview (default) or apply one handoff to a remote Unit through "
            "the target environment's own project gateway."
        ),
        description=(
            "Route one durable-anchor handoff to a Unit observed on another "
            "Herdr server. Delivery always goes through that environment's own "
            "project gateway to its Codex unit; the remote worker is never "
            "direct-sent and no remote pane is addressed from here. The preview "
            "is not a permit: applying re-observes the source, the Unit, and the "
            "repository identity, and sends nothing if any of them changed."
        ),
    )
    action.add_argument(
        "--unit",
        required=True,
        help="Opaque unit_id from `unit-board show --json`.",
    )
    action.add_argument(
        "--target-project",
        dest="target_project",
        required=True,
        help=(
            "Adopted project scope of the target repository. Declared, never "
            "derived: the registry project name and the board label are display "
            "values and cannot stand in for a scope authority."
        ),
    )
    action.add_argument("--issue", required=True, help="Redmine issue id.")
    action.add_argument("--journal", required=True, help="Redmine journal id.")
    action.add_argument(
        "--kind",
        choices=ACTION_KINDS,
        default=DEFAULT_ACTION_KIND,
        help=f"Durable intent label (default: {DEFAULT_ACTION_KIND}).",
    )
    action.add_argument(
        "--summary",
        required=True,
        help="Short pointer text; the durable record stays the source of truth.",
    )
    action.add_argument(
        "--apply",
        action="store_true",
        help="Deliver after re-verifying the preview. Omit to preview only.",
    )
    action.add_argument("--json", action="store_true", help="Emit structured JSON.")
    action.set_defaults(func=cmd_herdr_unit_board_action)


__all__ = (
    "cmd_herdr_unit_board_action",
    "cmd_herdr_unit_board_show",
    "cmd_herdr_unit_board_sources",
    "cmd_herdr_unit_board_sync",
    "cmd_herdr_unit_board_watch",
    "cmd_herdr_unit_board_interact",
    "register_herdr_unit_board_parser",
)
