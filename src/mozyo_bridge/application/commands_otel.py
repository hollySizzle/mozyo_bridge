"""Command handlers for the OTel / event-timeline command family.

Split out of ``application/commands.py`` (Redmine #12154) so the telemetry
surface (OTLP receiver, raw `otel events`, the redacted consumer event
timeline, per-source activity, and launchd residency) lives in one focused
module instead of the giant compatibility facade. ``commands.py`` re-exports
these handlers so existing imports and monkeypatch targets
(``mozyo_bridge.application.commands.cmd_otel_*`` /
``mozyo_bridge.application.commands.cmd_events_*``) keep resolving.

Behavior-preserving: the handler bodies (with their lazy local imports) are
moved verbatim. The store is a best-effort cache, never the source of truth;
gate state stays with Redmine and liveness with the tmux layer.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from mozyo_bridge.shared.errors import die


def cmd_otel_serve(args: argparse.Namespace) -> int:
    """Run the OTLP/HTTP receiver in the foreground (Redmine #11672).

    Localhost-only, single-threaded (= the SQLite single-writer), OTLP
    JSON natively and protobuf via the optional `mozyo-bridge[otel]`
    extra. Best-effort by contract: events sent while the receiver is
    down are lost; the store is a cache, not a source of truth.
    """
    from mozyo_bridge.application.otel_receiver import OtelReceiverError, serve

    try:
        serve(
            host=getattr(args, "host", None) or "127.0.0.1",
            port=int(getattr(args, "port", None) or 4318),
            db_path=(
                Path(args.db).expanduser() if getattr(args, "db", None) else None
            ),
        )
    except OtelReceiverError as exc:
        die(str(exc))
    return 0


def cmd_otel_status(args: argparse.Namespace) -> int:
    """Show store counts and receiver reachability. Read-only."""
    import json as _json
    import urllib.error
    import urllib.request

    from mozyo_bridge.otel_store import OtelEventStore

    store = OtelEventStore(
        Path(args.db).expanduser() if getattr(args, "db", None) else None
    )
    host = getattr(args, "host", None) or "127.0.0.1"
    port = int(getattr(args, "port", None) or 4318)
    receiver: dict = {"url": f"http://{host}:{port}/healthz"}
    try:
        with urllib.request.urlopen(receiver["url"], timeout=2) as response:
            receiver["reachable"] = True
            receiver["health"] = _json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        receiver["reachable"] = False
        receiver["error"] = str(exc)
    payload = {
        "store_path": str(store.path),
        "store_exists": store.path.exists(),
        **store.counts(),
        "receiver": receiver,
    }
    if getattr(args, "as_json", False):
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"store: {payload['store_path']} (exists: {payload['store_exists']})")
    print(f"events: {payload['total']} {payload['events_by_signal']}")
    print(f"last_write: {payload['last_write'] or '-'}")
    if receiver["reachable"]:
        print(f"receiver: reachable at {receiver['url']}")
    else:
        print(
            f"receiver: NOT reachable at {receiver['url']} "
            "(start with `mozyo-bridge otel serve`; telemetry sent while "
            "it is down is lost by design)"
        )
    return 0


def cmd_otel_events(args: argparse.Namespace) -> int:
    """Tail recent normalized events. Read-only; debugging / depth measurement."""
    import json as _json

    from mozyo_bridge.otel_store import OtelEventStore

    store = OtelEventStore(
        Path(args.db).expanduser() if getattr(args, "db", None) else None
    )
    events = store.recent_events(limit=int(getattr(args, "limit", None) or 50))
    if getattr(args, "as_json", False):
        print(
            _json.dumps(
                [event.as_payload() for event in events],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print("RECEIVED\tSIGNAL\tEVENT\tSERVICE\tSESSION\tPID\tCWD")
    for event in events:
        print(
            "\t".join(
                [
                    event.received_at or "-",
                    event.signal,
                    event.event_name,
                    event.service_name or "-",
                    (event.session_id or "-")[:12],
                    event.pid or "-",
                    event.cwd or "-",
                ]
            )
        )
    return 0


def _events_store(args: argparse.Namespace):
    from mozyo_bridge.otel_store import OtelEventStore

    return OtelEventStore(
        Path(args.db).expanduser() if getattr(args, "db", None) else None
    )


def _render_timeline(events, *, as_json: bool) -> int:
    """Shared text/JSON renderer for the consumer event timeline (#11813)."""
    import json as _json

    if as_json:
        print(
            _json.dumps(
                [event.as_payload() for event in events],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print("OBSERVED\tLAYER\tCATEGORY\tEVENT\tSERVICE\tSESSION\tWORKSPACE\tTOKENS")
    for event in events:
        agent = event.agent or {}
        tokens = event.usage.get("total_tokens")
        print(
            "\t".join(
                [
                    event.observed_at or "-",
                    event.source_layer,
                    event.category,
                    event.event_name,
                    (agent.get("service") or "-"),
                    (agent.get("session") or "-")[:12],
                    event.workspace_hint or "-",
                    str(tokens) if tokens is not None else "-",
                ]
            )
        )
    return 0


def cmd_events_tail(args: argparse.Namespace) -> int:
    """Tail the consumer event timeline (Redmine #11813). Read-only.

    A stable, redacted, source-layer-tagged projection over the OTel
    runtime store for display consumers (cockpit / private GUI / iTerm
    WebViewer). Distinct from `otel events`, which exposes the raw OTLP
    shape for debugging. The store is a best-effort cache, never the source
    of truth; gate state stays with Redmine and liveness with the tmux
    layer.
    """
    from mozyo_bridge.domain.event_timeline import project_rows

    store = _events_store(args)
    rows = store.query_events(limit=int(getattr(args, "limit", None) or 50))
    events = project_rows(rows)
    return _render_timeline(events, as_json=getattr(args, "as_json", False))


def cmd_events_query(args: argparse.Namespace) -> int:
    """Filtered consumer event timeline query (Redmine #11813). Read-only.

    `--since` filters on the receiver clock (`observed_at >= ISO`);
    `--source` matches the emitting service exactly. Same redacted,
    source-layer-tagged envelope as `events tail`.
    """
    from mozyo_bridge.domain.event_timeline import project_rows

    store = _events_store(args)
    rows = store.query_events(
        since=getattr(args, "since", None) or None,
        source=getattr(args, "source", None) or None,
        limit=int(getattr(args, "limit", None) or 200),
    )
    events = project_rows(rows)
    return _render_timeline(events, as_json=getattr(args, "as_json", False))


def cmd_otel_activity(args: argparse.Namespace) -> int:
    """Per-source activity/idle judgement (Redmine #11673). Read-only.

    `idle` and `unknown` are NOT death: OTel silence cannot distinguish
    waiting from dead, so callers degrade to the tmux liveness layer
    (`agents list` / `session list`).
    """
    import json as _json

    from mozyo_bridge.domain.agent_activity import summarize_activity
    from mozyo_bridge.otel_store import OtelEventStore

    store = OtelEventStore(
        Path(args.db).expanduser() if getattr(args, "db", None) else None
    )
    records = summarize_activity(
        store,
        active_window_seconds=int(getattr(args, "window", None) or 120),
    )
    if getattr(args, "as_json", False):
        print(
            _json.dumps(
                [record.as_payload() for record in records],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not records:
        print(
            "no telemetry sources observed (env not injected, receiver "
            "down, or store empty) — this means UNKNOWN, not dead; check "
            "`mozyo-bridge agents list` for liveness"
        )
        return 0
    print("STATE\tLAST_EVENT_AT\tSECONDS\tEVENT\tSERVICE\tSESSION\tPID\tCWD")
    for record in records:
        seconds = record.seconds_since_event
        print(
            "\t".join(
                [
                    record.state,
                    record.last_event_at or "-",
                    f"{seconds:.0f}" if seconds is not None else "-",
                    record.last_event_name or "-",
                    record.service_name or "-",
                    (record.session_id or "-")[:12],
                    record.match_hints.get("pid") or "-",
                    record.match_hints.get("cwd") or "-",
                ]
            )
        )
    return 0


def cmd_otel_launchd(args: argparse.Namespace) -> int:
    """launchd residency management for the receiver (Redmine #11690).

    Minimal face: install / uninstall / status / restart. The plist
    carries no environment block (no secrets possible by construction),
    keeps the loopback default bind, and restart is the documented
    upgrade step. macOS only.
    """
    import json as _json
    import sys as _sys

    from mozyo_bridge.application import otel_launchd

    if _sys.platform != "darwin" and getattr(args, "launchd_command", "") != "status":
        die("launchd management is macOS-only")
    action = getattr(args, "launchd_command", None)
    if action == "install":
        port = getattr(args, "port", None)
        result = otel_launchd.install(port=int(port) if port else None)
        print(f"installed: {result['plist']}")
        print(f"  command: {' '.join(result['command'])}")
        print(
            "  note: the plist carries no environment variables. To enable "
            "the cockpit's Redmine layer under launchd, deliver the key/URL "
            "via the home-scoped credential file:\n"
            "    umask 077 && $EDITOR ~/.mozyo_bridge/redmine-credentials.yaml\n"
            "  with `redmine: {url: <https-url>, api_key: <key>}` (chmod 600; "
            "the file is refused if group/other-readable). Without it the "
            "Redmine layer stays `unconfigured`; the other two layers are "
            "unaffected."
        )
        return 0
    if action == "uninstall":
        result = otel_launchd.uninstall()
        print(
            f"uninstalled: {result['plist']} "
            f"(plist removed: {result['removed']})"
        )
        return 0
    if action == "restart":
        otel_launchd.restart()
        print(f"restarted: {otel_launchd.LAUNCHD_LABEL}")
        return 0
    payload = otel_launchd.status()
    if getattr(args, "as_json", False):
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"label: {payload['label']}")
    print(f"plist: {payload['plist']} (exists: {payload['plist_exists']})")
    print(f"loaded: {payload['loaded']} pid: {payload['pid'] or '-'}")
    print(f"log: {payload['log']}")
    print(
        "receiver health is `mozyo-bridge otel status`; this surface only "
        "answers whether launchd is wired."
    )
    return 0
