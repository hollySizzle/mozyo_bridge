"""CLI surface for `workflow supervisor` — workspace callback supervisor (Redmine #13683 Phase A).

`mozyo-bridge workflow supervisor` is the mozyo **semantic facade** over the workspace callback
supervisor composition root (:mod:`...application.workspace_callback_supervisor`). It is the
user-scoped owner that enumerates the whole workspace registry and, per leased workspace, supplies
durable workflow events (so `workflow glance` / `workflow resume` stop reporting `unknown`) and
drains that workspace's callback-outbox partition — without an agent ever touching a raw Herdr /
tmux primitive.

Actions (mutually exclusive):

- ``--run-once`` — one **bounded supervised sweep** across the registry: for each workspace it can
  lease, supply events + deliver the callback outbox once (a refused lease -> the workspace is
  skipped, zero delivery — the duplicate-supervisor fence). Actuates. ``--wake WORKSPACE:ISSUE``
  (repeatable) switches to ``local_wake`` mode (supervise only the wake-named active-lane issues).
- ``--status`` — read-only: the registry workspaces, current supervisor leases, and the
  home-scoped runtime-store event count + callback-outbox backlog. Mutates nothing.
- ``--service-status`` / ``--install`` / ``--restart`` / ``--uninstall`` — the **service lifecycle
  command contract**, realized by whichever OS scheduler owns the host
  (:mod:`...application.supervisor_service_backend`): ONE owned macOS LaunchAgent
  (:mod:`...application.supervisor_launchd`) or ONE owned Linux systemd **user** service + timer
  (:mod:`...application.supervisor_systemd`). Since Redmine #15192 both register exactly one
  bounded ``--run-once`` tick at the same portable cadence (``--tick-interval``, default 180s), so
  the number of registrations, what they run, and what the verbs mean are the same on both hosts.
  ``--service-status`` prints a redacted host projection (installed / enabled / loaded / pid / next
  run / last exit result / scheduled interval / executable-match / credential readiness / installed
  command) + the secret-free declarative definition. ``--install`` / ``--restart`` / ``--uninstall``
  drive the owned service: the scheduled sweep is wired run-at-load + fixed-interval (never a
  KeepAlive / ``Restart=`` relaunch loop) with **no** environment block in any unit. They exit 0 on
  a performed action and non-zero on a fail-closed refusal (wrong platform, no host service manager,
  missing executable, restart-not-scheduled, an unidentifiable retired registration), touching
  nothing but the owned labels / unit files. On Linux an unconfigured Redmine does **not** block
  installing the timer: readiness is projected, not gated, so the local work a tick can safely do
  keeps running.

A source / store error is a ``SystemExit`` with a redacted message (never a credential / URL /
pane id / absolute path).
"""

from __future__ import annotations

import argparse
import dataclasses
import json as _json
import os
import socket
import time
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_OS_TICK_INTERVAL_SECONDS,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_DRAIN,
    SUPERVISION_LOCAL_WAKE,
    build_service_definition,
)


def _home_from_args(args: argparse.Namespace) -> Optional[Path]:
    """Resolve the ``--home`` override (test/debug), or ``None`` for the default mozyo home."""
    raw = (getattr(args, "home", None) or "").strip()
    return Path(raw) if raw else None


def _store_path_from_args(args: argparse.Namespace) -> Optional[Path]:
    raw = (getattr(args, "store_path", None) or "").strip()
    return Path(raw) if raw else None


def _default_holder() -> str:
    """A stable-per-process supervisor lease holder id (host + pid).

    Each supervisor process is a distinct lease holder, so a concurrent duplicate is fenced; a
    later invocation (a new pid) re-acquires cleanly after the prior process released its leases.
    """
    try:
        host = socket.gethostname() or "host"
    except OSError:
        host = "host"
    return f"{host}:{os.getpid()}"


def _parse_wake_hint(spec: str) -> tuple[str, str]:
    """Parse a ``WORKSPACE_ID:ISSUE`` wake hint (structured; no prose)."""
    raw = (spec or "").strip()
    ws, sep, issue = raw.partition(":")
    if not sep or not ws.strip() or not issue.strip():
        raise argparse.ArgumentTypeError(
            f"--wake expects WORKSPACE_ID:ISSUE (e.g. a1b2c3:13683), got {spec!r}"
        )
    return ws.strip(), issue.strip()


def _emit(payload: dict, *, as_json: bool, text_lines) -> None:
    """Emit the canonical payload as JSON, or the human text rows (Redmine #14150 j#85115).

    ``text_lines`` may be a sequence OR a zero-argument callable returning one. In JSON mode the
    text rows are **never built**, so a defect confined to the text formatter cannot take down
    the machine-readable output.

    That laziness is the structural fix for the run-once crash: every caller used to build its
    text rows eagerly and pass the finished list, so a single stale attribute in a text row
    (``w.provider_reads`` after the R3 rename to ``provider_calls``) raised before ``_emit`` was
    even reached — killing ``--json`` and, with it, the LaunchAgent ``--run-once`` reconcile pass
    that never got to report a terminal outcome. Passing a builder keeps the text path's failure
    modes inside the text path.
    """
    if as_json:
        print(_json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    for line in (text_lines() if callable(text_lines) else text_lines):
        print(line)


def _run_once_text_lines(report, action_label: str) -> list:
    """The human text rows for a run-once / drain report (pure; built only in text mode).

    Every per-workspace value here is read from the CANONICAL report contract. The workspace row
    reports ``provider_calls`` — the ACTUAL provider read count the R3 split introduced
    (:class:`...domain.workspace_supervisor.WorkspaceSupervisionOutcome`). It previously read a
    ``provider_reads`` attribute that no longer existed on that contract, which is the defect
    Redmine #14150 j#85115 recorded.
    """
    lines = [
        f"action: {action_label}",
        f"mode: {report.mode}",
        f"duration_ms: {report.duration_ms}",
        f"workspaces_total: {len(report.workspaces)}",
        f"workspaces_supervised: {report.workspaces_supervised}",
        f"workspaces_skipped: {report.workspaces_skipped}",
        f"events_supplied: {report.events_supplied}",
        f"delivered: {report.delivered}",
        # Receipt truth (Redmine #13683 R2): claimed rows that did NOT wake the receiver (busy /
        # uncertain / reconciled-away), held as retryable / uncertain receipts — surfaced alongside
        # ``delivered`` so the projection never presents a non-wake as a delivery.
        f"blocked: {report.blocked}",
        # Redmine #14150 observability: provider (Redmine) reads this pass (0 for a drain), rows
        # deferred to the reconciliation leg, and whether the whole pass was empty.
        f"deferred: {report.deferred}",
        f"provider_calls: {report.provider_calls}",
        f"empty_pass: {report.empty_pass}",
    ]
    for w in report.workspaces:
        if w.lease_acquired:
            lines.append(
                f"  ws {w.workspace_id}: supervised {len(w.supervised_issues)} issue(s), "
                f"events={w.events_supplied} delivered={w.delivered} blocked={w.blocked} "
                f"deferred={w.deferred} provider_calls={w.provider_calls}"
                + (f" [{w.skipped_reason}]" if w.skipped_reason else "")
            )
        else:
            lines.append(f"  ws {w.workspace_id}: skipped ({w.skipped_reason})")
    return lines


def _cmd_run_once(args: argparse.Namespace) -> int:
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
        build_supervisor,
    )

    holder = (getattr(args, "holder", None) or "").strip() or _default_holder()
    wake_hints = tuple(getattr(args, "wake", None) or ())
    # Redmine #14150: --drain-only selects the LOCAL outbox drain (local state only, zero
    # ticket-provider reads). Otherwise local_wake mode is selected explicitly (--local-wake, the
    # wake-driven consume path) or implicitly when explicit --wake hints are supplied; the default is
    # the bounded provider reconciliation sweep.
    drain_only = bool(getattr(args, "drain_only", False))
    local_wake = bool(getattr(args, "local_wake", False)) or bool(wake_hints)
    if drain_only:
        mode = SUPERVISION_LOCAL_DRAIN
    elif local_wake:
        mode = SUPERVISION_LOCAL_WAKE
    else:
        mode = SUPERVISION_BOUNDED_RECONCILIATION
    supervisor = build_supervisor(
        holder=holder, home=_home_from_args(args), store_path=_store_path_from_args(args)
    )
    started = time.monotonic()
    report = supervisor.run_once(mode=mode, wake_hints=() if drain_only else wake_hints)
    # duration_ms is the reconcile / drain duration close condition 5 asks be measurable (secret-safe).
    report = dataclasses.replace(report, duration_ms=int((time.monotonic() - started) * 1000))
    payload = report.as_payload()
    action_label = "drain" if drain_only else "run-once"
    _emit(
        payload,
        as_json=bool(getattr(args, "as_json", False)),
        text_lines=lambda: _run_once_text_lines(report, action_label),
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
    from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore, supervisor_lease_path
    from mozyo_bridge.core.state.workflow_runtime_store import (
        CALLBACK_DEAD_LETTER,
        CALLBACK_PENDING,
        CALLBACK_UNCERTAIN,
        WorkflowRuntimeStore,
        WorkflowRuntimeStoreError,
        workflow_runtime_store_path,
    )
    from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseError
    from mozyo_bridge.core.state.workspace_registry import list_workspaces

    home = _home_from_args(args)
    store_path = _store_path_from_args(args) or workflow_runtime_store_path(home)
    try:
        workspaces = list_workspaces(home=home)
        leases = SupervisorLeaseStore(path=supervisor_lease_path(home)).leases()
        store = WorkflowRuntimeStore(path=store_path)
        outbox = CallbackOutbox(path=store_path)
        event_count = len(store.read_events())
        pending = len(outbox.read(states=[CALLBACK_PENDING]))
        uncertain = len(outbox.read(states=[CALLBACK_UNCERTAIN]))
        dead_letter = len(outbox.read(states=[CALLBACK_DEAD_LETTER]))
    except (WorkflowRuntimeStoreError, SupervisorLeaseError) as exc:
        raise SystemExit(f"workflow supervisor status: store unavailable ({exc})") from exc

    lease_holders = {lease.workspace_id: lease for lease in leases}
    payload = {
        "action": "status",
        "workspaces_total": len(workspaces),
        "leases_held": len(leases),
        "runtime_events": event_count,
        "callback_pending": pending,
        "callback_uncertain": uncertain,
        "callback_dead_letter": dead_letter,
        "workspaces": [
            {
                "workspace_id": rec.workspace_id,
                "project_name": rec.project_name,
                "lease_held": rec.workspace_id in lease_holders,
                "lease_holder": (
                    lease_holders[rec.workspace_id].holder
                    if rec.workspace_id in lease_holders
                    else ""
                ),
                "lease_expires_at": (
                    lease_holders[rec.workspace_id].expires_at
                    if rec.workspace_id in lease_holders
                    else ""
                ),
            }
            for rec in workspaces
        ],
    }
    lines = [
        "action: status",
        f"workspaces_total: {len(workspaces)}",
        f"leases_held: {len(leases)}",
        f"runtime_events: {event_count}",
        f"callback_pending: {pending}",
        f"callback_uncertain: {uncertain}",
        f"callback_dead_letter: {dead_letter}",
    ]
    for rec in workspaces:
        lease = lease_holders.get(rec.workspace_id)
        held = f"leased by {lease.holder} until {lease.expires_at}" if lease else "unleased"
        lines.append(f"  ws {rec.workspace_id} ({rec.project_name}): {held}")
    _emit(
        payload, as_json=bool(getattr(args, "as_json", False)), text_lines=lambda: lines
    )
    return 0


#: The deprecated cadence flag and what now carries its meaning (Redmine #15192 / j#102151 F3).
DEPRECATED_INTERVAL_FLAGS = {
    "reconciliation_interval": (
        "--reconciliation-interval",
        "--reconciliation-interval is deprecated; the OS registration cadence is --tick-interval. "
        "Using the supplied value as the tick interval.",
    ),
    "drain_interval": (
        "--drain-interval",
        "--drain-interval is deprecated and ignored; no OS scheduler registers a --drain-only "
        "service (a --run-once tick already does the drain leg).",
    ),
}


def _effective_tick_interval(args: argparse.Namespace) -> Optional[int]:
    """The OS tick cadence, honouring the deprecated ``--reconciliation-interval`` synonym.

    An explicit ``--tick-interval`` always wins. Otherwise a supplied ``--reconciliation-interval``
    is used, because on the previous release that flag DID set the interval carried by the service
    definition — so silently ignoring it would change what an existing invocation configures.
    """
    explicit = getattr(args, "tick_interval", None)
    if explicit:
        return int(explicit)
    legacy = getattr(args, "reconciliation_interval", None)
    return int(legacy) if legacy else None


def _deprecation_notices(args: argparse.Namespace) -> list:
    """Fixed, secret-free notices for any deprecated flag the caller supplied."""
    notices = []
    for dest, (_flag, message) in DEPRECATED_INTERVAL_FLAGS.items():
        if getattr(args, dest, None):
            notices.append(message)
    return notices


def _service_definition(args: argparse.Namespace):
    """The declarative definition of the service this host would own.

    Its interval is the **OS tick** (``--tick-interval``, else the deprecated synonym, else the
    shared portable default), because the definition describes the owned registration — and since
    #15192 that registration ticks on the OS cadence, not the provider one. Carrying the provider
    cadence here would advertise 300s beside a `scheduled_interval_seconds` of 180s for the same
    service. The provider cadence keeps its own key in the status projection
    (`provider_reconcile_interval_seconds`).
    """
    interval = int(_effective_tick_interval(args) or DEFAULT_OS_TICK_INTERVAL_SECONDS)
    return build_service_definition(reconciliation_interval_seconds=interval)


def _service_status_lines(host: dict, index: int) -> list:
    """The redacted text rows for one owned service in the status projection.

    Reads only keys the adapter is guaranteed to emit, and shows the systemd-only observability
    (next run, last exit result) when the host adapter supplies it — the acceptance contract asks an
    operator to be able to see 導入・有効化状態 / 次回起動 / 直近の終了結果 / 実行内容 without secrets
    (Redmine #15183). Every value here is a boolean, count, fixed token, timestamp, or a non-secret
    argv (executable path + fixed flags + a config directory).
    """
    tag = f"[{index}]"
    lines = [
        f"{tag} service_label: {host.get('label', '')}",
        f"{tag} installed: {host.get('installed')} loaded: {host.get('loaded')} "
        f"pid: {host.get('pid')}",
        f"{tag} scheduled_interval_seconds: {host.get('scheduled_interval_seconds')}",
        f"{tag} home_pin: {host.get('home_pin')} "
        f"executable_matches: {host.get('executable_matches')}",
        f"{tag} keep_alive_present: {host.get('keep_alive_present')}",
        f"{tag} credential_readiness: {host.get('credential_readiness')}",
    ]
    if "timer_enabled" in host:
        lines.append(f"{tag} timer_enabled: {host['timer_enabled']}")
    if "next_elapse" in host:
        # The basis rides WITH the value, never separately. A monotonic figure is measured since
        # boot, not a wall clock, so `next_elapse: 4w 1d 5h` alone is actively misleading — an
        # operator reads it as "in 4 weeks". The JSON payload carried the basis from the start; the
        # text path dropped it, which is the defect review j#102053 Finding 5 recorded.
        basis = host.get("next_elapse_basis") or "unknown"
        lines.append(f"{tag} next_elapse: {host['next_elapse'] or '(none)'} (basis: {basis})")
    if "last_trigger" in host:
        # The wall-clock companion that makes a monotonic next_elapse actionable.
        lines.append(f"{tag} last_trigger: {host['last_trigger'] or '(none)'}")
    if "last_result" in host:
        lines.append(
            f"{tag} last_result: {host['last_result']} "
            f"exit_status: {host.get('last_exit_status')} at: {host.get('last_exit_at')}"
        )
    if "provider_reconcile_interval_seconds" in host:
        lines.append(
            f"{tag} provider_reconcile_interval_seconds: "
            f"{host['provider_reconcile_interval_seconds']} (Redmine cadence; the OS tick is local)"
        )
    if host.get("installed_command"):
        lines.append(f"{tag} installed_command: {' '.join(host['installed_command'])}")
    return lines


def _cmd_service(args: argparse.Namespace, *, verb: str) -> int:
    """The service lifecycle command contract, on whichever OS scheduler owns this host.

    Redmine #15183 / #15192: the host adapter is resolved by platform
    (:mod:`...application.supervisor_service_backend`) so one operator command means the same thing
    everywhere, and a host with neither adapter is a typed zero-mutation refusal rather than a
    silent no-op. Both realizations register exactly ONE bounded ``--run-once`` tick — a macOS
    LaunchAgent, or a Linux systemd user service + timer — at the same portable cadence. Their
    *internals* stay their own (launchd and systemd are not made to mirror each other, and neither
    is forced onto cron). The backend module normalizes both into a one-row ``agents`` roster, so
    the rendering below never branches on platform; the resolved ``backend`` token rides in every
    payload.

    The OS tick is not a Redmine poll: the supervisor body gates provider reads behind its own
    durable ~300s cadence, so an in-window tick works from local state with zero provider calls.

    ``--service-status`` is a redacted projection + the secret-free declarative definitions (exit 0,
    mutates nothing). ``--install`` / ``--restart`` / ``--uninstall`` drive the owned services and
    exit non-zero on a fail-closed refusal (wrong platform / no host service manager / missing
    executable / not-scheduled), touching nothing but the owned artifacts.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
        supervisor_service_backend,
    )

    as_json = bool(getattr(args, "as_json", False))
    # The supervisor CLI's ``--home`` is the **mozyo home** override (registry / store / credential
    # root); the plist / unit files always live under the OS user home, which the service verbs
    # resolve from ``Path.home()`` (never relocated by ``--home``) — j#79092 R2-F1.
    mozyo_home = _home_from_args(args)
    definition = _service_definition(args)
    tick_interval = _effective_tick_interval(args)
    deprecations = _deprecation_notices(args)

    if verb == "service-status":
        status = supervisor_service_backend.service_status(
            mozyo_home=mozyo_home, interval_hint=tick_interval
        )
        backend = status["backend"]
        payload = dict(status)
        # ``phase`` names the supervisor lifecycle phase of the product (Redmine #13683 Phase B1),
        # not the adapter shape, so it is preserved verbatim: #15183 adds a host realization and has
        # no reason to drop a key an existing reader may consume.
        payload["phase"] = "B1"
        # The declarative definitions must describe what this backend actually OWNS. Emitting the
        # drain definition unconditionally told a Linux reader that a `--drain-only` service exists
        # when the host runs one `--run-once` timer (review j#102053 Finding 6). ``definitions`` is
        # the roster, aligned 1:1 with ``agents``.
        #
        # Deriving it per backend — rather than seeding it with the primary definition and adding to
        # it — is what makes the unsupported host correct too: a host with no adapter owns NOTHING,
        # so it must advertise an empty roster next to its empty ``agents``. Seeding produced
        # ``agents=0`` beside ``definitions=1``, breaking the very invariant this key introduced
        # (review j#102069 Finding 8).
        #
        # Since #15192 every supported backend owns exactly ONE service, so the roster is one entry
        # on both hosts and empty on an unsupported one. There is no `drain_definition` on any host:
        # nothing registers a `--drain-only` service with an OS scheduler any more, and emitting a
        # definition for a service nobody owns is the exact claim review j#102053 Finding 6 removed
        # for Linux — the same rule now simply has no host left to exempt.
        owned_definitions = {
            supervisor_service_backend.BACKEND_LAUNCHD: [definition],
            supervisor_service_backend.BACKEND_SYSTEMD: [definition],
            supervisor_service_backend.BACKEND_UNSUPPORTED: [],
        }[backend]
        # ``definition`` stays an always-present scalar for readers that predate the roster; it is
        # the would-be primary definition, not a claim that a service is installed.
        payload["definition"] = definition.as_payload()
        payload["definitions"] = [d.as_payload() for d in owned_definitions]
        # `drain_definition` was a public key on the previous release, so it is still emitted rather
        # than dropped (j#102151 Finding 3) — but it must not re-assert that a drain service exists,
        # which is the claim review j#102053 Finding 6 removed. The honest compatibility shape is a
        # RETIRED marker: the key survives for readers that index it, and says plainly that nothing
        # registers it, instead of describing a service no host owns.
        payload["drain_definition"] = {
            "retired": True,
            "retired_by": "issue_15192",
            "registered": False,
            "note": (
                "no OS scheduler registers a --drain-only service on any host; a --run-once tick "
                "already performs the drain leg. --drain-only remains a manual action."
            ),
        }
        if deprecations:
            payload["deprecations"] = list(deprecations)
        lines = ["action: service-status", f"backend: {backend}"]
        lines += [f"deprecation: {n}" for n in deprecations]
        for index, host in enumerate(status.get("agents", ())):
            lines += _service_status_lines(host, index)
        lines.append(f"definition_command: {' '.join(definition.command)}")
        if backend == supervisor_service_backend.BACKEND_UNSUPPORTED:
            lines.append(
                f"reason: {supervisor_service_backend.REASON_NO_BACKEND} "
                "(no owned OS scheduler adapter for this host)"
            )
        _emit(payload, as_json=as_json, text_lines=lambda: lines)
        return 0

    if verb == "install":
        result = supervisor_service_backend.install(
            mozyo_home=mozyo_home, interval_seconds=tick_interval
        )
    elif verb == "restart":
        result = supervisor_service_backend.restart(mozyo_home=mozyo_home)
    else:  # uninstall
        result = supervisor_service_backend.uninstall()

    payload = dict(result)
    if deprecations:
        payload["deprecations"] = list(deprecations)
    performed = bool(result.get("performed"))
    lines = [
        f"action: {result.get('action', verb)}",
        f"backend: {result['backend']}",
        f"performed: {performed}",
        f"effect_state: {result['effect_state']}",
    ]
    lines += [f"deprecation: {n}" for n in deprecations]
    if result.get("reason"):
        lines.append(f"reason: {result['reason']}")
    if result.get("rolled_back"):
        lines.append("rolled_back: True (partial-failure fail-closed)")
    if result.get("scheduled_interval_seconds"):
        lines.append(f"scheduled_interval_seconds: {result['scheduled_interval_seconds']}")
    for a in result.get("agents", []):
        detail = f"  service {a.get('label', '')}: performed={a.get('performed')}"
        if a.get("reason"):
            detail += f" reason={a['reason']}"
        if "effect_state" in a:
            detail += f" effect_state={a['effect_state']}"
        if "removed" in a:
            detail += f" removed={a['removed']}"
        if a.get("plist_state") and a["plist_state"] not in ("absent", "owned"):
            # The service definition at our own path is not identifiable as ours, so a mutating verb
            # refused and left it alone. The operator has to see *which* state, because "someone
            # else's file is here" and "I cannot parse what is here" need different fixes
            # (review j#102496 r12f2). Quiet in the ordinary absent / owned cases.
            detail += f" plist_state={a['plist_state']}"
        if "credential_readiness" in a:
            # Reported, not gated on Linux: an unconfigured Redmine does not block installing the
            # timer, so an operator sees the state without the install being refused (#15183).
            detail += f" credential_readiness={a['credential_readiness']}"
        if a.get("legacy_drain") and a["legacy_drain"] != "absent":
            # A retired pre-#15192 registration was found. Whether it was removed or refused, the
            # operator has to see it: it is the difference between "one agent runs here" and "two
            # do". Suppressed in the ordinary `absent` case so a migrated host stays quiet.
            detail += f" legacy_drain={a['legacy_drain']}"
            if "legacy_drain_removed" in a:
                detail += f" legacy_drain_removed={a['legacy_drain_removed']}"
        lines.append(detail)
    _emit(payload, as_json=as_json, text_lines=lambda: lines)
    return 0 if performed else 1


def _resolve_watch_wait_binary() -> str:
    """Resolve the sanctioned trusted-environment herdr binary for the event wait (review R6-F1).

    Uses the single shared :func:`resolve_herdr_binary` (``MOZYO_HERDR_BINARY`` -> trusted-PATH
    ``herdr``), the same resolver ``workflow callbacks --watch`` binds its wake to, so the pump
    spawns ``herdr agent wait TARGET --until STATUS --timeout MS`` and never
    ``mozyo-bridge`` (which has no ``agent wait``
    subcommand). Fail-safe: an unconfigured / unresolvable binary returns ``""`` so the pump
    degrades to a bounded timeout-only wait (still runs the whole-roster reconciliation) instead of
    launching a bogus executable.
    """
    try:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
            resolve_herdr_binary,
        )

        return resolve_herdr_binary(os.environ).path
    except Exception:  # noqa: BLE001 - binary unconfigured / unresolvable -> timeout-only degrade
        return ""


def _cmd_watch(args: argparse.Namespace) -> int:
    """Run the bounded supervisor event pump: Herdr turn events drive the reconcile passes.

    The event-driven PRIMARY activation (Redmine #13758 Q1 / j#79507): the shared supervisor is
    the sole reconcile owner, driven by a bounded multiplex Herdr ``agent wait TARGET --until
    done --timeout MS`` per active-lane target. ``--max-iterations`` bounds the pump (never an unbounded poll);
    the StartInterval one-shot ``--run-once`` remains the loss-recovery fallback.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_event_pump import (
        build_event_pump_seams,
        default_pump_targets,
        run_event_pump,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
        build_supervisor,
    )

    holder = (getattr(args, "holder", None) or "").strip() or _default_holder()
    home = _home_from_args(args)
    max_iterations = int(getattr(args, "max_iterations", None) or 1)
    timeout_ms = int(getattr(args, "wait_timeout_ms", None) or 50000)
    # release_after=False: the pump keeps the workspace lease across its bounded iterations (it
    # is the single long-ish-lived reconcile owner), renewing rather than releasing each pass.
    supervisor = build_supervisor(
        holder=holder, home=home,
        store_path=_store_path_from_args(args), release_after=False,
    )
    # Review R6-F1: the event wait spawns herdr's ``agent wait --until`` surface, so the seam must
    # get the sanctioned trusted-environment herdr binary — never ``mozyo-bridge`` (no ``agent wait``
    # subcommand). If it is not configured, pass an empty binary so the pump degrades to a
    # timeout-only wait (still runs the bounded whole-roster reconciliation) rather than spawning a
    # bogus executable (mirrors the ``workflow callbacks --watch`` fail-safe).
    wait_binary = _resolve_watch_wait_binary()
    supervisor_pass, targets_fn, wait_multiplex_fn = build_event_pump_seams(
        supervisor=supervisor,
        targets_fn=lambda: default_pump_targets(home=home),
        wait_binary=wait_binary,
        timeout_ms=timeout_ms,
    )
    # Redmine #14150 (live evidence j#83437 / j#83443): the bounded watch holds workspace leases
    # across its iterations (release_after=False) so it keeps ownership between wakes — but when it
    # TERMINATES (normal end, an exception, or a wake=error edge) those leases MUST be released, or
    # the fallback --run-once starves every workspace as lease_held_by_other until the ~5-min TTL.
    # The release is token-conditional, so a workspace taken over by a NEW live owner is never
    # evicted: only this terminated holder's own leases drop, and the duplicate-owner fence stands.
    try:
        results = run_event_pump(
            supervisor_pass=supervisor_pass,
            targets_fn=targets_fn,
            wait_multiplex_fn=wait_multiplex_fn,
            max_iterations=max_iterations,
        )
    finally:
        supervisor.release_all_leases()
    as_json = bool(getattr(args, "as_json", False))
    if as_json:
        print(_json.dumps({"action": "watch", "iterations": results}, ensure_ascii=False, sort_keys=True))
    else:
        print(f"action: watch (bounded event pump, {len(results)} iteration(s))")
        for i, r in enumerate(results):
            print(f"  [{i}] mode={r['mode']} pass_ok={r['pass_ok']} wake={r['wake']} woke={r['woke_target']}")
    return 0


def cmd_workflow_supervisor(args: argparse.Namespace) -> int:
    """Run one `workflow supervisor` action (run-once / watch / status / service lifecycle contract)."""
    if getattr(args, "watch", False):
        return _cmd_watch(args)
    if getattr(args, "drain_only", False):
        return _cmd_run_once(args)
    if getattr(args, "run_once", False):
        return _cmd_run_once(args)
    if getattr(args, "status", False):
        return _cmd_status(args)
    if getattr(args, "service_status", False):
        return _cmd_service(args, verb="service-status")
    if getattr(args, "install", False):
        return _cmd_service(args, verb="install")
    if getattr(args, "restart", False):
        return _cmd_service(args, verb="restart")
    if getattr(args, "uninstall", False):
        return _cmd_service(args, verb="uninstall")
    raise SystemExit(
        "workflow supervisor requires an action: --run-once | --drain-only | --watch | --status | "
        "--service-status | --install | --restart | --uninstall"
    )


def register_supervisor(workflow_sub) -> None:
    """Register ``workflow supervisor`` onto the ``workflow`` subparser (Redmine #13683 Phase A)."""
    p = workflow_sub.add_parser(
        "supervisor",
        description=(
            "Workspace callback supervisor (Redmine #13683 Phase A). The user-scoped owner that "
            "enumerates the whole workspace registry and, per workspace it can lease, supplies "
            "durable workflow events (so `workflow glance` / `workflow resume` stop reporting "
            "`unknown`) and drains that workspace's callback-outbox partition. `--run-once` runs "
            "one bounded supervised sweep (a refused lease skips the workspace: the "
            "duplicate-supervisor fence); `--wake WORKSPACE:ISSUE` switches to local_wake mode. "
            "`--status` is a read-only registry / lease / backlog view. The service lifecycle "
            "contract (`--service-status` / `--install` / `--restart` / `--uninstall`) runs on the "
            "OS scheduler that owns this host: ONE macOS LaunchAgent, or ONE Linux systemd user "
            "service + timer, both ticking `--run-once` every --tick-interval seconds (#15183 / "
            "#15192; Redmine reads stay on the supervisor's own ~300s cadence, so an in-window "
            "tick is local-only). "
            "`--service-status` is a redacted projection (installed / enabled / next run / last "
            "exit result / installed command) + secret-free definition; the mutating verbs drive "
            "the one-shot run-at-load + fixed-interval services (no KeepAlive / Restart= relaunch "
            "loop, no environment block) and fail-closed on a wrong platform / no host service "
            "manager / missing executable. An unconfigured Redmine blocks the install on neither "
            "host: readiness is reported, not gated."
        ),
        help=(
            "Workspace callback supervisor: run-once / status / service lifecycle contract. "
            "Supplies durable glance/resume state + drains callbacks per leased workspace."
        ),
    )
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--run-once", dest="run_once", action="store_true",
        help="One bounded supervised sweep across the registry (supply events + deliver callbacks).",
    )
    action.add_argument(
        "--watch", dest="watch", action="store_true",
        help="Bounded event pump (Redmine #13758): Herdr turn events drive the reconcile passes "
             "(supervisor is the sole reconcile owner). --max-iterations bounds it; --run-once is "
             "the loss-recovery fallback.",
    )
    action.add_argument(
        "--drain-only", dest="drain_only", action="store_true",
        help="Local outbox drain (Redmine #14150): read LOCAL state only and deliver already-enqueued, "
             "locally-attestable coordinator rows through a provider-free sender. Makes ZERO "
             "ticket-provider calls (an empty pass and a safe-pending pass both). A row it cannot "
             "attest from local state is deferred to the provider reconciliation leg (--run-once).",
    )
    p.add_argument(
        "--max-iterations", dest="max_iterations", type=int, default=1,
        help="Event-pump bound: number of (multiplex wait -> reconcile pass) iterations after the "
             "startup bootstrap reconcile (--watch; default 1 -> bootstrap + one observed edge "
             "consumed in-invocation).",
    )
    p.add_argument(
        "--wait-timeout-ms", dest="wait_timeout_ms", type=int, default=50000,
        help="Per-target bounded Herdr wait window in ms (--watch; default 50000, within the "
             "user-commentary SLA).",
    )
    action.add_argument(
        "--status", action="store_true",
        help="Read-only: registry workspaces, supervisor leases, runtime-event + callback backlog.",
    )
    action.add_argument(
        "--service-status", dest="service_status", action="store_true",
        help="Report the resolved (secret-free) service definition and host-activation status.",
    )
    action.add_argument(
        "--install", action="store_true",
        help="Install the ONE owned scheduled one-shot service on this host's OS scheduler: a macOS "
             "LaunchAgent, or a Linux systemd user service + timer, both running `--run-once` every "
             "--tick-interval seconds (default 180). Both fail-closed on a wrong platform / no host "
             "service manager / missing executable. On NEITHER host does an unconfigured Redmine "
             "block the install: readiness is reported, not gated, so the local work a tick can "
             "safely do keeps running. On macOS a retired pre-#15192 `--drain-only` agent is removed "
             "as part of the install; an unidentifiable plist at that path, or a retired agent that "
             "is still loaded after its bootout, refuses instead of leaving two registrations.",
    )
    action.add_argument(
        "--restart", action="store_true",
        help="Re-run the scheduled bounded sweep now. Fail-closed if the service is not scheduled "
             "or the installed command drifted (reinstall to change it). A non-ready credential "
             "does not refuse on either host; readiness is reported, not gated.",
    )
    action.add_argument(
        "--uninstall", action="store_true",
        help="Remove exactly the owned scheduler artifacts (the LaunchAgent plist, or the systemd "
             "user units) after stopping them, plus any retired pre-#15192 drain registration this "
             "adapter can identify as its own. No credential required.",
    )
    p.add_argument(
        "--local-wake", dest="local_wake", action="store_true",
        help="Wake-driven consume: drain the durable local-wake queue (gate-emit produced) and "
             "supervise only those active-lane issues (local_wake mode). Loss is recovered by a "
             "plain --run-once (bounded reconciliation).",
    )
    p.add_argument(
        "--wake", action="append", type=_parse_wake_hint, metavar="WORKSPACE_ID:ISSUE",
        help="An explicit local wake hint (repeatable): supervise these active-lane issues "
             "(implies local_wake mode; merged with the drained wake queue).",
    )
    p.add_argument(
        "--holder", default=None,
        help="Override the supervisor lease holder id (default: host:pid). One holder per supervisor process.",
    )
    # Compatibility inputs (Redmine #15192 review j#102151 Finding 3). Neither ever configured an
    # OS registration — both only shaped a declarative display — but they were public parser surface
    # on the previous release, and `release.md` binds a minor feature to backward compatibility. They
    # are therefore still ACCEPTED, with `--reconciliation-interval` folded onto the one cadence knob
    # it is a synonym for and `--drain-interval` recorded as inert, so an existing invocation keeps
    # working and the operator is told what actually happened rather than being failed at parse time.
    p.add_argument(
        "--reconciliation-interval", dest="reconciliation_interval", type=int, default=None,
        help="DEPRECATED (#15192): the OS registration's cadence is now --tick-interval, which this "
             "is treated as a synonym for when --tick-interval is not given. It never set the "
             "Redmine cadence; the supervisor body gates provider reads behind its own ~300s "
             "watermark, reported as provider_reconcile_interval_seconds.",
    )
    p.add_argument(
        "--drain-interval", dest="drain_interval", type=int, default=None,
        help="DEPRECATED and inert (#15192): no OS scheduler registers a `--drain-only` service on "
             "any host — a `--run-once` tick already does the drain leg — so there is no drain "
             "cadence to configure. Accepted so existing invocations keep working; a deprecation "
             "notice is emitted and the value is ignored.",
    )
    p.add_argument(
        "--tick-interval", dest="tick_interval", type=int, default=None,
        help="OS tick cadence in seconds for the installed scheduler (Redmine #15183 / #15192; "
             "portable default 180). ONE knob for both hosts: it is the macOS LaunchAgent's "
             "StartInterval and the Linux systemd timer's OnUnitActiveSec. This is the LOCAL "
             "cadence: each tick runs one bounded `--run-once` sweep over SQLite + Herdr. It does "
             "NOT set the Redmine cadence — the supervisor gates provider reads behind its own "
             "~300s watermark, so a tick inside that window makes zero provider calls.",
    )
    p.add_argument("--json", action="store_true", dest="as_json", help="Emit a structured JSON result.")
    p.add_argument("--home", default=None, help=argparse.SUPPRESS)  # test/debug: override mozyo home
    p.add_argument("--store-path", dest="store_path", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_workflow_supervisor)


__all__ = ("cmd_workflow_supervisor", "register_supervisor")
