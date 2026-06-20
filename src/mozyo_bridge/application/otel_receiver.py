"""Self-built OTLP/HTTP receiver for the OTel event store (Redmine #11672).

The owner decision (#11639 journal #56088) is explicit: no official OTel
Collector. A dozen agents in a two-person org get a small OTLP/HTTP
endpoint that decodes the standard wire shapes and writes the normalized
events into the SQLite store. Both endpoints speak the same OTLP, so the
escape hatch stays open — agents' env vars are unchanged if a real
Collector ever replaces this.

Operational shape:

- ``mozyo-bridge otel serve`` runs the receiver in the foreground; daemon
  management (launchd plist, restart-on-upgrade) is a follow-up task and
  this process is deliberately launchd-friendly (foreground, SIGTERM
  clean exit, single port).
- Binds 127.0.0.1 only. Telemetry never leaves the machine and nothing
  remote can write into the store.
- ``http.server.HTTPServer`` is single-threaded, which *is* the SQLite
  single-writer guarantee — requests serialize by construction.
- OTLP/HTTP JSON (``application/json``) decodes with the stdlib. OTLP/HTTP
  protobuf (``application/x-protobuf``) decodes when the optional
  ``opentelemetry-proto`` extra is installed (``pip install
  'mozyo-bridge[otel]'``); without it the receiver answers 415 with the
  remediation (install the extra or set
  ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json`` on the agent).
- Best-effort by contract: events sent while this process is down are
  lost and the store is never the source of truth (#11639 constraint 1).
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mozyo_bridge.otel_store import (
    OtelEvent,
    OtelEventStore,
    filter_attributes,
)

DEFAULT_OTEL_HOST = "127.0.0.1"
# The OTLP/HTTP standard port, so agent-side config stays zero-surprise
# (`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`).
DEFAULT_OTEL_PORT = 4318

# The receiver is localhost-only by contract (#11639 / review #56128):
# telemetry never leaves the machine and nothing remote can write into
# the store. A non-loopback bind would need a separate owner decision
# and security model, so it is rejected at the library layer rather
# than merely not-offered by the CLI.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class OtelReceiverError(RuntimeError):
    """User-actionable receiver configuration error."""


def _require_loopback(host: str) -> str:
    candidate = (host or DEFAULT_OTEL_HOST).strip()
    if candidate.lower() in _LOOPBACK_HOSTS or candidate.startswith("127."):
        return candidate
    raise OtelReceiverError(
        f"refusing to bind the OTel receiver to {candidate!r}: the receiver "
        "is localhost-only by contract (telemetry must not leave the "
        "machine, and nothing remote may write into the store). Use "
        "127.0.0.1 / localhost / ::1."
    )

_SIGNAL_PATHS = {
    "/v1/logs": "logs",
    "/v1/metrics": "metrics",
    "/v1/traces": "traces",
}

_RESOURCE_KEYS = {
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
}


def _decode_any_value(value: object) -> object:
    """Decode an OTLP AnyValue mapping to a scalar (nested values dropped)."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    # arrayValue / kvlistValue / bytesValue: nested structures are where
    # free-form content hides; the store only accepts scalars.
    return None


def _decode_attributes(raw: object) -> dict:
    out: dict = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if isinstance(key, str):
            out[key] = _decode_any_value(item.get("value"))
    return out


def _nano_to_iso(value: object) -> str | None:
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        return None
    if nanos <= 0:
        return None
    return datetime.fromtimestamp(nanos / 1e9, timezone.utc).isoformat(
        timespec="seconds"
    )


def _event_from(
    signal: str,
    event_name: str,
    event_time: str | None,
    resource_attrs: dict,
    record_attrs: dict,
) -> OtelEvent:
    merged = {**resource_attrs, **record_attrs}
    filtered = filter_attributes(merged)
    pid = merged.get("process.pid")
    return OtelEvent(
        signal=signal,
        event_name=event_name,
        event_time=event_time,
        service_name=(
            merged.get("service.name")
            if isinstance(merged.get("service.name"), str)
            else None
        ),
        session_id=(
            merged.get("session.id")
            if isinstance(merged.get("session.id"), str)
            else None
        ),
        pid=str(pid) if isinstance(pid, (str, int)) else None,
        cwd=merged.get("cwd") if isinstance(merged.get("cwd"), str) else None,
        attrs=filtered,
    )


def decode_otlp_json(signal: str, payload: dict) -> list[OtelEvent]:
    """Normalize an OTLP/JSON export request into store events.

    Log record *bodies* are intentionally never read into the event — that
    is where ``OTEL_LOG_USER_PROMPTS``-style prompt content would live
    (#11639 constraint 4).
    """
    resource_key, scope_key, record_key = _RESOURCE_KEYS[signal]
    events: list[OtelEvent] = []
    for resource_entry in payload.get(resource_key) or []:
        if not isinstance(resource_entry, dict):
            continue
        resource = resource_entry.get("resource") or {}
        resource_attrs = _decode_attributes(
            resource.get("attributes") if isinstance(resource, dict) else None
        )
        for scope_entry in resource_entry.get(scope_key) or []:
            if not isinstance(scope_entry, dict):
                continue
            for record in scope_entry.get(record_key) or []:
                if not isinstance(record, dict):
                    continue
                if signal == "metrics":
                    events.extend(
                        _metric_events(record, resource_attrs)
                    )
                    continue
                record_attrs = _decode_attributes(record.get("attributes"))
                if signal == "logs":
                    name = record.get("eventName") or record_attrs.get(
                        "event.name"
                    )
                    event_name = name if isinstance(name, str) and name else "log"
                    event_time = _nano_to_iso(
                        record.get("timeUnixNano")
                        or record.get("observedTimeUnixNano")
                    )
                else:  # traces
                    name = record.get("name")
                    event_name = (
                        name if isinstance(name, str) and name else "span"
                    )
                    event_time = _nano_to_iso(
                        record.get("endTimeUnixNano")
                        or record.get("startTimeUnixNano")
                    )
                events.append(
                    _event_from(
                        signal, event_name, event_time, resource_attrs,
                        record_attrs,
                    )
                )
    return events


def _metric_events(record: dict, resource_attrs: dict) -> list[OtelEvent]:
    """One event per metric datapoint, named after the metric."""
    name = record.get("name")
    metric_name = name if isinstance(name, str) and name else "metric"
    events: list[OtelEvent] = []
    for container_key in ("sum", "gauge", "histogram"):
        container = record.get(container_key)
        if not isinstance(container, dict):
            continue
        for point in container.get("dataPoints") or []:
            if not isinstance(point, dict):
                continue
            point_attrs = _decode_attributes(point.get("attributes"))
            events.append(
                _event_from(
                    "metrics",
                    metric_name,
                    _nano_to_iso(point.get("timeUnixNano")),
                    resource_attrs,
                    point_attrs,
                )
            )
    return events


def decode_otlp_protobuf(signal: str, body: bytes) -> list[OtelEvent] | None:
    """Decode an OTLP protobuf request; ``None`` when the extra is absent.

    The optional ``opentelemetry-proto`` dependency (``mozyo-bridge[otel]``)
    converts the message to the camelCase dict shape and reuses the JSON
    normalizer, so both wire encodings share one filtering path.
    """
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError:
        return None
    message_types = {
        "logs": ExportLogsServiceRequest,
        "metrics": ExportMetricsServiceRequest,
        "traces": ExportTraceServiceRequest,
    }
    message = message_types[signal]()
    message.ParseFromString(body)
    payload = MessageToDict(message)
    return decode_otlp_json(signal, payload)


def _is_loopback_origin(origin: str) -> bool:
    """Strict loopback check for a browser ``Origin`` header value.

    Parsed, exact-host comparison (review #56212): a prefix match admits
    ``http://localhost.evil.example``, because the loopback string is a
    prefix of a hostile registrable domain. Only ``http`` with a hostname
    exactly equal to ``127.0.0.1`` / ``localhost`` / ``::1`` (any port)
    qualifies.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


class _ReceiverHandler(BaseHTTPRequestHandler):
    server_version = "mozyo-otel"
    store: OtelEventStore  # injected via server attribute

    # Quiet by default; the receiver is a long-running daemon and stdout
    # noise per request would swamp launchd logs.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _respond_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            store: OtelEventStore = self.server.store  # type: ignore[attr-defined]
            self._respond_json(
                200,
                {
                    "ok": True,
                    "store": str(store.path),
                    **store.counts(),
                },
            )
            return
        # Cockpit Web UI (Redmine #11679): served by the same daemon, same
        # loopback-only bind. Imported lazily so pure-receiver usage does
        # not pay for the inventory stack.
        if self.path == "/":
            from mozyo_bridge.application.cockpit_ui import INDEX_HTML_TEMPLATE

            token = getattr(self.server, "cockpit_token", "")
            body = INDEX_HTML_TEMPLATE.replace(
                "__COCKPIT_TOKEN__", token
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/units":
            from mozyo_bridge.redmine_context import attach_redmine_context
            from mozyo_bridge.session_inventory import take_inventory

            home = getattr(self.server, "home", None)
            snapshot = take_inventory(home=home)
            tracker = getattr(self.server, "transitions", None)
            # Transitions are observed only on live runtime snapshots: a
            # stale cache could pair an outdated pane set with fresh
            # activity and fabricate transitions.
            if tracker is not None and not snapshot.stale:
                tracker.observe(list(snapshot.records))
            payload = snapshot.as_payload()
            # Third join layer (Redmine #11686): read-only gate context,
            # attached at the cockpit layer only — `session list` itself
            # never blocks on the network. Additive field; degradation
            # (unconfigured / unavailable) never touches the other layers.
            redmine_cache = getattr(self.server, "redmine_context", None)
            if redmine_cache is not None:
                payload = attach_redmine_context(payload, redmine_cache)
            # Fourth join layer (Redmine #12007): derived attention projection,
            # additive and cockpit-layer only — the same conservative
            # AttentionRecord vocabulary `agents targets --json` exposes, so a
            # cockpit frontend can triage panes from one source. Pure (no
            # network); the observation clock is stamped here at the I/O layer.
            from datetime import datetime, timezone

            from mozyo_bridge.application.cockpit_ui import (
                attach_attention,
                attach_observation,
            )

            now = datetime.now(timezone.utc)
            payload = attach_attention(
                payload,
                observed_at=now.isoformat(timespec="seconds"),
            )
            # Fifth join layer (Redmine #12225): the runtime observation
            # freshness envelope for the displayed snapshot itself (observed_at /
            # freshness / readability / display_state), so the cockpit can show
            # "last refreshed" and surface stale / unreadable / unknown rather
            # than letting a cached view read as current. Derived from the same
            # `snapshot` as the rows, via the `observe reload` mapping. Diagnostic
            # only: it moves no gate and authorizes no action.
            payload = attach_observation(payload, snapshot, now=now)
            self._respond_json(200, payload)
            return
        if self.path == "/api/grouped-units":
            # Served grouped cockpit read model (Redmine #12286): the Project
            # Group -> Unit -> Target display view, built from the repo-local
            # desired grouping config and the live tmux inventory snapshot. A
            # display projection only — no row carries a pane / target, and an
            # action re-resolves its candidate Unit live (grouped-reveal /
            # grouped-jump). An invalid repo-local config fails closed (400) so a
            # broken grouping config is visible, never silently defaulted.
            from datetime import datetime, timezone

            from mozyo_bridge.application.cockpit_ui import grouped_units_payload
            from mozyo_bridge.domain.repo_local_config import (
                RepoLocalConfigError,
            )

            home = getattr(self.server, "home", None)
            now = datetime.now(timezone.utc)
            try:
                payload = grouped_units_payload(home=home, now=now)
            except RepoLocalConfigError as exc:
                self._respond_json(
                    400,
                    {"error": f"repo-local grouping config is invalid: {exc}"},
                )
                return
            self._respond_json(200, payload)
            return
        if self.path == "/api/transitions":
            tracker = getattr(self.server, "transitions", None)
            transitions = (
                [t.as_payload() for t in tracker.recent()] if tracker else []
            )
            self._respond_json(200, {"transitions": transitions})
            return
        self._respond_json(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path in ("/api/actions/reveal", "/api/actions/jump"):
            self._handle_action(self.path.rsplit("/", 1)[1])
            return
        if self.path in (
            "/api/actions/grouped-reveal",
            "/api/actions/grouped-jump",
        ):
            self._handle_grouped_action(self.path.rsplit("/", 1)[1])
            return
        if self.path == "/api/actions/grouped-preview":
            self._handle_grouped_preview()
            return
        signal = _SIGNAL_PATHS.get(self.path)
        if signal is None:
            self._respond_json(404, {"error": "unknown path"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError:
                self._respond_json(400, {"error": "bad gzip body"})
                return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        content_type = content_type.strip().lower()
        try:
            if content_type == "application/json":
                payload = json.loads(body.decode("utf-8"))
                events = decode_otlp_json(signal, payload)
            elif content_type == "application/x-protobuf":
                events = decode_otlp_protobuf(signal, body)
                if events is None:
                    self._respond_json(
                        415,
                        {
                            "error": (
                                "protobuf decoding requires the optional "
                                "extra: pip install 'mozyo-bridge[otel]'. "
                                "Or set OTEL_EXPORTER_OTLP_PROTOCOL="
                                "http/json on the agent."
                            )
                        },
                    )
                    return
            else:
                self._respond_json(
                    415, {"error": f"unsupported content type {content_type!r}"}
                )
                return
        except (ValueError, UnicodeDecodeError) as exc:
            self._respond_json(400, {"error": f"undecodable payload: {exc}"})
            return
        except Exception as exc:  # protobuf parse errors are library-specific
            self._respond_json(400, {"error": f"undecodable payload: {exc}"})
            return
        store: OtelEventStore = self.server.store  # type: ignore[attr-defined]
        store.insert_events(events)
        # OTLP/HTTP success: 200 with an (empty) Export*ServiceResponse.
        self._respond_json(200, {})

    def _action_intent_rejected(self) -> bool:
        """Reject any action request that cannot prove same-origin UI intent.

        Review #56197 finding 1: a cross-site *simple* request (text/plain
        body, no preflight) must never reach the action handlers, because
        "actions run only from an explicit UI click" (#56164 / US
        constraint 5) is a security boundary, not just a UX statement.
        Three independent gates, each sufficient to stop the simple-request
        shape:

        - Content-Type must be ``application/json`` (simple requests are
          limited to form/text types);
        - the per-process cockpit token must ride in the custom
          ``X-Mozyo-Cockpit-Token`` header (custom headers force a CORS
          preflight, which this server never approves — it sends no CORS
          response headers at all);
        - when a browser supplies an ``Origin``, it must be this loopback
          origin.
        """
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        if content_type.strip().lower() != "application/json":
            self._respond_json(
                415,
                {
                    "error": (
                        "actions accept application/json from the cockpit "
                        "UI only"
                    )
                },
            )
            return True
        expected = getattr(self.server, "cockpit_token", None)
        supplied = self.headers.get("X-Mozyo-Cockpit-Token")
        if not expected or supplied != expected:
            self._respond_json(
                403,
                {
                    "error": (
                        "missing or invalid cockpit token; actions can "
                        "only be invoked from the served cockpit page"
                    )
                },
            )
            return True
        origin = self.headers.get("Origin")
        if origin is not None and not _is_loopback_origin(origin):
            self._respond_json(
                403, {"error": f"cross-origin action rejected ({origin})"}
            )
            return True
        return False

    def _handle_action(self, kind: str) -> None:
        """Cockpit user actions (Redmine #11680).

        Runs only as the direct result of an explicit user click in the
        UI (US constraint 5: the daemon never initiates focus changes);
        :meth:`_action_intent_rejected` enforces that the request actually
        originates from the served page. Failures are explanatory JSON,
        never silent or destructive.
        """
        from mozyo_bridge.application.cockpit_ui import (
            CockpitActionError,
            jump_to_unit,
            reveal_in_finder,
        )

        if self._action_intent_rejected():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond_json(400, {"error": "request body must be JSON"})
            return
        pane_id = payload.get("pane_id") if isinstance(payload, dict) else None
        home = getattr(self.server, "home", None)
        try:
            if kind == "reveal":
                result = reveal_in_finder(pane_id, home=home)
            else:
                result = jump_to_unit(pane_id, home=home)
        except CockpitActionError as exc:
            self._respond_json(409, {"error": str(exc)})
            return
        self._respond_json(200, result)

    def _handle_grouped_action(self, kind: str) -> None:
        """Grouped cockpit Unit actions (Redmine #12265).

        Same explicit-click security gate as :meth:`_handle_action`
        (:meth:`_action_intent_rejected`). The request body carries only a
        grouped read-model *candidate* Unit identity (``workspace_id`` /
        ``role`` / optional ``lane_id`` / ``host_id``) — never a pane id baked
        into the displayed projection. The action re-resolves that identity
        against a fresh live inventory and fails closed (409) on a stale /
        ambiguous / missing / remote / non-default-lane target, so the grouped
        projection can only *name* a candidate, never authorize a side effect.
        """
        from mozyo_bridge.application.cockpit_ui import (
            CockpitActionError,
            grouped_jump,
            grouped_reveal,
        )

        if self._action_intent_rejected():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond_json(400, {"error": "request body must be JSON"})
            return
        if not isinstance(payload, dict):
            self._respond_json(
                400, {"error": "request body must be a JSON object"}
            )
            return
        home = getattr(self.server, "home", None)
        action = grouped_reveal if kind == "grouped-reveal" else grouped_jump
        try:
            result = action(
                workspace_id=payload.get("workspace_id"),
                role=payload.get("role"),
                lane_id=payload.get("lane_id") or "default",
                host_id=payload.get("host_id") or "local",
                home=home,
            )
        except CockpitActionError as exc:
            self._respond_json(409, {"error": str(exc)})
            return
        self._respond_json(200, result)

    def _handle_grouped_preview(self) -> None:
        """Grouped cockpit command preview (Redmine #12296).

        Same explicit-click security gate as :meth:`_handle_grouped_action`
        (:meth:`_action_intent_rejected`) and the same candidate-identity body
        (``workspace_id`` / ``role`` / optional ``lane_id`` / ``host_id``). Unlike
        the action endpoints this is **non-mutating**: it runs the action-time
        live preflight read-only and reports whether the safe actions would
        currently resolve, so the Unit detail screen can show availability without
        performing a side effect. It always answers 200 — a stale / ambiguous /
        missing / remote / non-default-lane candidate is reported as
        ``available: False`` with the preflight reason, never an executed action.
        """
        from mozyo_bridge.application.cockpit_ui import grouped_action_preview

        if self._action_intent_rejected():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond_json(400, {"error": "request body must be JSON"})
            return
        if not isinstance(payload, dict):
            self._respond_json(
                400, {"error": "request body must be a JSON object"}
            )
            return
        home = getattr(self.server, "home", None)
        result = grouped_action_preview(
            workspace_id=payload.get("workspace_id"),
            role=payload.get("role"),
            lane_id=payload.get("lane_id") or "default",
            host_id=payload.get("host_id") or "local",
            home=home,
        )
        self._respond_json(200, result)


def build_server(
    *,
    host: str = DEFAULT_OTEL_HOST,
    port: int = DEFAULT_OTEL_PORT,
    db_path: Path | None = None,
    home: Path | None = None,
) -> HTTPServer:
    """Construct (and bind) the receiver without entering the serve loop.

    Raises :class:`OtelReceiverError` for any non-loopback host.
    """
    import secrets

    from mozyo_bridge.domain.agent_activity import TransitionTracker

    store = OtelEventStore(db_path, home=home)
    server = HTTPServer((_require_loopback(host), port), _ReceiverHandler)
    server.store = store  # type: ignore[attr-defined]
    server.home = home  # type: ignore[attr-defined]
    server.transitions = TransitionTracker()  # type: ignore[attr-defined]
    # Per-process action token (review #56197): embedded into the served
    # page and required on every action POST, so action intent can only
    # originate from the cockpit UI this daemon itself served.
    server.cockpit_token = secrets.token_hex(16)  # type: ignore[attr-defined]
    # Read-only Redmine gate context (Redmine #11686). Both the API key
    # AND the trusted base URL come from the daemon environment only —
    # repo-local files can never select where the key is sent (review
    # #56232). Either var absent = `unconfigured` for every unit, and the
    # cockpit's other two layers stand on their own.
    import os

    from mozyo_bridge.redmine_context import (
        API_KEY_ENV,
        BASE_URL_ENV,
        RedmineContextCache,
    )

    server.redmine_context = RedmineContextCache(  # type: ignore[attr-defined]
        api_key=os.environ.get(API_KEY_ENV) or None,
        base_url=os.environ.get(BASE_URL_ENV) or None,
    )
    return server


def serve(
    *,
    host: str = DEFAULT_OTEL_HOST,
    port: int = DEFAULT_OTEL_PORT,
    db_path: Path | None = None,
    home: Path | None = None,
    retention_days: int | None = None,
) -> None:
    """Run the receiver in the foreground until interrupted."""
    from mozyo_bridge.otel_store import DEFAULT_RETENTION_DAYS

    server = build_server(host=host, port=port, db_path=db_path, home=home)
    store: OtelEventStore = server.store  # type: ignore[attr-defined]
    pruned = store.prune(
        retention_days=(
            retention_days if retention_days is not None
            else DEFAULT_RETENTION_DAYS
        )
    )
    print(
        f"mozyo-bridge otel receiver listening on http://{host}:{port} "
        f"(store: {store.path}, pruned {pruned} expired events)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
