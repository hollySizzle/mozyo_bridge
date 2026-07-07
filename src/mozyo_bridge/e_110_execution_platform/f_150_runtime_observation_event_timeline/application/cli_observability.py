"""CLI parser registration for the events / otel observability family.

Split out of ``application/cli.py`` (Redmine #12153). Behavior-preserving;
the handlers themselves live in ``application/commands.py``. Block text is
moved verbatim from ``build_parser()`` so help / choices / defaults / dest /
``func`` bindings are unchanged.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.commands import (
    cmd_events_query,
    cmd_events_tail,
    cmd_observe_reload,
    cmd_otel_activity,
    cmd_otel_events,
    cmd_otel_launchd,
    cmd_otel_serve,
    cmd_otel_status,
)


def register(sub) -> None:
    """Register `events` then `otel` onto ``sub``."""
    events = sub.add_parser(
        "events",
        help=(
            "Consumer event timeline source (Redmine #11813): a stable, "
            "redacted, source-layer-tagged projection over the OTel runtime "
            "store for display consumers (cockpit / private GUI / iTerm "
            "WebViewer). Distinct from `otel events`, which exposes the raw "
            "OTLP shape for debugging — this face decouples consumers from "
            "the OTel internal schema. Read-only and best-effort: the store "
            "is a cache, never the source of truth (gate state stays with "
            "Redmine, liveness with `agents list` / `session list`). JSON "
            "carries identifiers, event kinds and numeric usage only — never "
            "prompt bodies or full filesystem paths."
        ),
    )
    events_sub = events.add_subparsers(dest="events_command", required=True)

    def add_events_db_option(parser_obj: argparse.ArgumentParser) -> None:
        parser_obj.add_argument(
            "--db",
            help=(
                "Event store path override. Default: "
                "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`."
            ),
        )

    events_tail = events_sub.add_parser(
        "tail",
        help=(
            "Tail the most recent timeline events (default 50), newest "
            "first. Use `--json` for the stable TimelineEvent envelope that "
            "display consumers code against."
        ),
    )
    events_tail.add_argument(
        "--limit", help="Max events to show (default 50)."
    )
    add_events_db_option(events_tail)
    events_tail.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the timeline as the JSON TimelineEvent envelope.",
    )
    events_tail.set_defaults(func=cmd_events_tail)

    events_query = events_sub.add_parser(
        "query",
        help=(
            "Filtered timeline query. `--since` keeps events at or after a "
            "UTC ISO timestamp (the receiver clock); `--source` matches the "
            "emitting service exactly. Same redacted envelope as `tail`."
        ),
    )
    events_query.add_argument(
        "--since",
        help=(
            "Keep events whose observed_at is >= this UTC ISO timestamp "
            "(e.g. 2026-06-14T00:00:00+00:00)."
        ),
    )
    events_query.add_argument(
        "--source",
        help="Keep only events from this service_name (exact match).",
    )
    events_query.add_argument(
        "--limit", help="Max events to show (default 200)."
    )
    add_events_db_option(events_query)
    events_query.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the timeline as the JSON TimelineEvent envelope.",
    )
    events_query.set_defaults(func=cmd_events_query)

    otel = sub.add_parser(
        "otel",
        help=(
            "OTel event store (Redmine #11639 / #11672): a self-built, "
            "localhost-only OTLP/HTTP receiver persists agent telemetry "
            "(usage / event kinds only, never prompt bodies) into "
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`. "
            "Best-effort: events sent while the receiver is down are lost; "
            "the store is a cache, never the source of truth. Liveness "
            "stays with `agents list` / `session list`; workflow state "
            "stays with Redmine."
        ),
    )
    otel_sub = otel.add_subparsers(dest="otel_command", required=True)

    def add_otel_db_option(parser_obj: argparse.ArgumentParser) -> None:
        parser_obj.add_argument(
            "--db",
            help=(
                "Event store path override. Default: "
                "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`."
            ),
        )

    otel_serve = otel_sub.add_parser(
        "serve",
        help=(
            "Run the OTLP/HTTP receiver in the foreground (single-threaded "
            "= SQLite single-writer; binds 127.0.0.1 only). JSON encoding "
            "is built-in; protobuf needs `pip install 'mozyo-bridge[otel]'` "
            "or set OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the agent. "
            "launchd wiring is a follow-up task; this process is designed "
            "to be launchd-managed (foreground, clean shutdown)."
        ),
    )
    otel_serve.add_argument(
        "--host",
        help=(
            "Bind address. Loopback only (127.0.0.1 / localhost / ::1); "
            "any other value is rejected — the receiver is localhost-only "
            "by contract. Default 127.0.0.1."
        ),
    )
    otel_serve.add_argument(
        "--port", help="Port (default 4318, the OTLP/HTTP standard)."
    )
    add_otel_db_option(otel_serve)
    otel_serve.set_defaults(func=cmd_otel_serve)

    otel_status = otel_sub.add_parser(
        "status",
        help=(
            "Store counts plus receiver /healthz reachability. Read-only. "
            "An unreachable receiver means telemetry is being lost "
            "(by design) until it is restarted."
        ),
    )
    otel_status.add_argument("--host", help="Receiver host (default 127.0.0.1).")
    otel_status.add_argument("--port", help="Receiver port (default 4318).")
    add_otel_db_option(otel_status)
    otel_status.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the status as JSON.",
    )
    otel_status.set_defaults(func=cmd_otel_status)

    otel_events = otel_sub.add_parser(
        "events",
        help=(
            "Tail recent normalized events. Read-only; for debugging and "
            "for measuring per-CLI event depth."
        ),
    )
    otel_events.add_argument(
        "--limit", help="Max events to show (default 50)."
    )
    add_otel_db_option(otel_events)
    otel_events.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the events as JSON.",
    )
    otel_events.set_defaults(func=cmd_otel_events)

    otel_activity = otel_sub.add_parser(
        "activity",
        help=(
            "Per-source activity / idle judgement (Redmine #11673). "
            "`idle` / `unknown` never mean dead — OTel silence cannot "
            "distinguish waiting from dead; consult `agents list` for "
            "liveness. Sources are (service, session); the pane_id join "
            "is phase 2 (`match_hints` carries pid / cwd for it)."
        ),
    )
    otel_activity.add_argument(
        "--window",
        help="Active window in seconds (default 120).",
    )
    add_otel_db_option(otel_activity)
    otel_activity.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit activity records as JSON.",
    )
    otel_activity.set_defaults(func=cmd_otel_activity)

    otel_launchd = otel_sub.add_parser(
        "launchd",
        help=(
            "macOS launchd residency for the receiver (Redmine #11690): "
            "install / uninstall / status / restart. The LaunchAgent plist "
            "carries no environment variables (no secrets possible), keeps "
            "the loopback-only default bind, and `restart` is the upgrade "
            "step after `pipx upgrade mozyo-bridge`. Receiver health stays "
            "with `otel status`."
        ),
    )
    otel_launchd_sub = otel_launchd.add_subparsers(
        dest="launchd_command", required=True
    )
    launchd_install = otel_launchd_sub.add_parser(
        "install",
        help=(
            "Write ~/Library/LaunchAgents/"
            "biz.asile.mozyo-bridge.otel.plist and bootstrap it "
            "(RunAtLoad + KeepAlive). Idempotent; re-running re-bootstraps."
        ),
    )
    launchd_install.add_argument(
        "--port",
        help="Receiver port override written into the plist (default 4318).",
    )
    launchd_install.set_defaults(func=cmd_otel_launchd)
    launchd_uninstall = otel_launchd_sub.add_parser(
        "uninstall",
        help="Boot the agent out and remove exactly our plist file.",
    )
    launchd_uninstall.set_defaults(func=cmd_otel_launchd)
    launchd_status = otel_launchd_sub.add_parser(
        "status",
        help=(
            "launchd-side wiring state (plist presence, loaded, pid). "
            "Additive to `otel status`, which owns receiver health."
        ),
    )
    launchd_status.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the status as JSON.",
    )
    launchd_status.set_defaults(func=cmd_otel_launchd)
    launchd_restart = otel_launchd_sub.add_parser(
        "restart",
        help=(
            "Kickstart (kill + relaunch) the loaded agent — the documented "
            "upgrade step after updating the package."
        ),
    )
    launchd_restart.set_defaults(func=cmd_otel_launchd)

    observe = sub.add_parser(
        "observe",
        help=(
            "Runtime observation snapshots (Redmine #12224): explicitly "
            "refresh a diagnostic/display view of runtime state and see how "
            "old it is. A snapshot is a timestamped observation, never "
            "workflow truth: it does not move any Redmine gate and does not "
            "authorize action (side-effecting commands run their own "
            "action-time live preflight). Stale/unreadable sources derive "
            "`unknown` / `reload_required`, never `healthy`."
        ),
    )
    observe_sub = observe.add_subparsers(dest="observe_command", required=True)
    observe_reload = observe_sub.add_parser(
        "reload",
        help=(
            "Re-capture runtime observation snapshots for the chosen "
            "source(s) and print their observed_at / source / method / "
            "freshness / readability envelope. Read-only; exit code is "
            "non-zero when any requested snapshot is fail-closed "
            "(`unknown` / `reload_required`), so a stale snapshot is never "
            "reported as healthy. This refreshes display/diagnostic state "
            "only — it never updates workflow truth, approval, routing, "
            "close, or completion."
        ),
    )
    observe_reload.add_argument(
        "--source",
        choices=["tmux", "otel", "herdr", "all"],
        default="all",
        help=(
            "Which observation source to reload: `tmux` (live runtime "
            "liveness, degrading to the inventory cache), `otel` (the "
            "best-effort OTel event store cache), `herdr` (the live herdr "
            "agent-list inventory; fail-closed when the herdr backend is not "
            "selected or the server is unreachable), or `all` (default; "
            "includes herdr only when the repo-local config selects the "
            "herdr backend)."
        ),
    )
    observe_reload.add_argument(
        "--max-age",
        dest="max_age",
        help=(
            "Seconds before an observation is considered no longer `fresh` "
            "(default 30)."
        ),
    )
    observe_reload.add_argument(
        "--expired-after",
        dest="expired_after",
        help=(
            "Seconds before a stale observation becomes `expired` / "
            "`reload_required` (default 300)."
        ),
    )
    observe_reload.add_argument(
        "--db",
        help=(
            "OTel event store path override. Default: "
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`."
        ),
    )
    observe_reload.add_argument(
        "--home",
        help=(
            "mozyo-bridge home override (inventory cache / store location). "
            "Default: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}`."
        ),
    )
    observe_reload.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the snapshots as the JSON runtime observation envelope.",
    )
    observe_reload.set_defaults(func=cmd_observe_reload)
