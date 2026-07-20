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
  command contract**, realized in Phase B1 as the owned macOS LaunchAgent lifecycle
  (:mod:`...application.supervisor_launchd`). ``--service-status`` prints a redacted host-service
  projection (plist / loaded / pid / scheduled interval / executable-match / credential readiness) +
  the secret-free declarative definition. ``--install`` / ``--restart`` / ``--uninstall`` drive the
  owned LaunchAgent: the scheduled ``--run-once`` sweep is wired with ``RunAtLoad`` + ``StartInterval``
  (never ``KeepAlive``) and a plist carrying **no** ``EnvironmentVariables``. They exit 0 on a
  performed action and non-zero on a fail-closed refusal (non-darwin, missing executable, non-ready
  credential, restart-not-loaded), touching nothing but the owned label / plist.

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
    DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS,
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
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
    if as_json:
        print(_json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for line in text_lines:
            print(line)


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
                f"deferred={w.deferred} provider_reads={w.provider_reads}"
                + (f" [{w.skipped_reason}]" if w.skipped_reason else "")
            )
        else:
            lines.append(f"  ws {w.workspace_id}: skipped ({w.skipped_reason})")
    _emit(payload, as_json=bool(getattr(args, "as_json", False)), text_lines=lines)
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
    _emit(payload, as_json=bool(getattr(args, "as_json", False)), text_lines=lines)
    return 0


def _service_definition(args: argparse.Namespace):
    interval = int(
        getattr(args, "reconciliation_interval", None)
        or DEFAULT_RECONCILIATION_INTERVAL_SECONDS
    )
    return build_service_definition(reconciliation_interval_seconds=interval)


def _drain_service_definition(args: argparse.Namespace):
    """The LOCAL-drain service definition (Redmine #14150): finer cadence, same bounded command adapter."""
    interval = int(getattr(args, "drain_interval", None) or DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS)
    return build_service_definition(reconciliation_interval_seconds=interval, local_drain=True)


def _cmd_service(args: argparse.Namespace, *, verb: str) -> int:
    """The service lifecycle command contract (Phase B1: macOS LaunchAgent lifecycle, DUAL agent).

    Redmine #14150: the split runs TWO owned bounded one-shot LaunchAgents — the coarse
    provider-reconciliation agent (``--run-once``) and the finer local-drain agent (``--drain-only``).
    ``--service-status`` reports the redacted host projection of BOTH agents + both secret-free
    definitions (exit 0, mutates nothing). ``--install`` / ``--restart`` / ``--uninstall`` drive the
    owned PAIR (:mod:`...application.supervisor_launchd` ``*_pair``): install is atomic-or-nothing (a
    partial failure rolls the first agent back), so an operator never ends up with a half-installed
    pair. They exit 0 only when BOTH agents performed, and non-zero on any fail-closed refusal
    (non-darwin / missing executable / non-ready credential / not-loaded), touching nothing but the
    two owned labels / plists.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
        supervisor_launchd,
    )

    as_json = bool(getattr(args, "as_json", False))
    # The supervisor CLI's ``--home`` is the **mozyo home** override (registry / store / credential
    # root); the plist / log always live under the OS user home, which the service verbs resolve
    # from ``Path.home()`` (never relocated by ``--home``) — j#79092 R2-F1.
    mozyo_home = _home_from_args(args)
    definition = _service_definition(args)
    drain_definition = _drain_service_definition(args)

    if verb == "service-status":
        status = supervisor_launchd.service_status_pair(
            mozyo_home=mozyo_home,
            reconcile_interval_hint=definition.reconciliation_interval_seconds,
            drain_interval_hint=drain_definition.reconciliation_interval_seconds,
        )
        agents = status["agents"]
        reconcile_host, drain_host = agents[0], agents[1]
        payload = dict(status)
        payload["phase"] = "B1"
        payload["definition"] = definition.as_payload()
        payload["drain_definition"] = drain_definition.as_payload()
        lines = ["action: service-status", "phase: B1 (dual owned LaunchAgent pair; #14150)"]
        for host, defn, kind in (
            (reconcile_host, definition, "reconciliation"),
            (drain_host, drain_definition, "drain"),
        ):
            lines += [
                f"[{kind}] service_label: {host['label']}",
                f"[{kind}] installed: {host['installed']} loaded: {host['loaded']} pid: {host['pid']}",
                f"[{kind}] scheduled_interval_seconds: {host['scheduled_interval_seconds']}",
                f"[{kind}] home_pin: {host['home_pin']} executable_matches: {host['executable_matches']}",
                f"[{kind}] keep_alive_present: {host['keep_alive_present']}",
                f"[{kind}] credential_readiness: {host['credential_readiness']}",
                f"[{kind}] command: {' '.join(defn.command)}",
            ]
        _emit(payload, as_json=as_json, text_lines=lines)
        return 0

    if verb == "install":
        result = supervisor_launchd.install_pair(
            mozyo_home=mozyo_home,
            reconcile_interval_seconds=definition.reconciliation_interval_seconds,
            drain_interval_seconds=drain_definition.reconciliation_interval_seconds,
        )
    elif verb == "restart":
        result = supervisor_launchd.restart_pair(mozyo_home=mozyo_home)
    else:  # uninstall
        result = supervisor_launchd.uninstall_pair()

    payload = dict(result)
    performed = bool(result.get("performed"))
    lines = [
        f"action: {result.get('action', verb)}",
        f"performed: {performed}",
    ]
    if result.get("reason"):
        lines.append(f"reason: {result['reason']}")
    if result.get("rolled_back"):
        lines.append("rolled_back: True (partial-failure fail-closed)")
    for a in result.get("agents", []):
        detail = f"  agent {a.get('label', '')}: performed={a.get('performed')}"
        if a.get("reason"):
            detail += f" reason={a['reason']}"
        if "removed" in a:
            detail += f" removed={a['removed']}"
        if "credential_readiness" in a:
            detail += f" credential_readiness={a['credential_readiness']}"
        lines.append(detail)
    _emit(payload, as_json=as_json, text_lines=lines)
    return 0 if performed else 1


def _resolve_watch_wait_binary() -> str:
    """Resolve the sanctioned trusted-environment herdr binary for the event wait (review R6-F1).

    Uses the single shared :func:`resolve_herdr_binary` (``MOZYO_HERDR_BINARY`` -> trusted-PATH
    ``herdr``), the same resolver ``workflow callbacks --watch`` binds its wake to, so the pump
    spawns ``herdr wait agent-status`` and never ``mozyo-bridge`` (which has no ``wait``
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
    the sole reconcile owner, driven by a bounded multiplex Herdr ``wait agent-status --status
    done`` per active-lane target. ``--max-iterations`` bounds the pump (never an unbounded poll);
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
    # Review R6-F1: the event wait spawns herdr's ``wait agent-status`` surface, so the seam must
    # get the sanctioned trusted-environment herdr binary — never ``mozyo-bridge`` (no ``wait``
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
            "contract (`--service-status` / `--install` / `--restart` / `--uninstall`) is the owned "
            "macOS LaunchAgent lifecycle (Phase B1): `--service-status` is a redacted projection + "
            "secret-free definition; the mutating verbs drive the one-shot RunAtLoad + StartInterval "
            "agent (no KeepAlive, no EnvironmentVariables) and fail-closed on non-darwin / missing "
            "executable / non-ready credential."
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
        help="Install the owned LaunchAgent (RunAtLoad + StartInterval one-shot sweep). Fail-closed "
             "on non-darwin / missing executable / non-ready credential.",
    )
    action.add_argument(
        "--restart", action="store_true",
        help="Kickstart the loaded LaunchAgent. Fail-closed if not loaded / non-darwin / non-ready.",
    )
    action.add_argument(
        "--uninstall", action="store_true",
        help="Boot out and remove exactly the owned LaunchAgent plist (no credential required).",
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
    p.add_argument(
        "--reconciliation-interval", dest="reconciliation_interval", type=int, default=None,
        help="Bounded provider-reconciliation interval seconds for the service definition "
             "(default: portable default). The low-frequency ticket-provider fallback cadence.",
    )
    p.add_argument(
        "--drain-interval", dest="drain_interval", type=int, default=None,
        help="Local-drain interval seconds for the service definition (Redmine #14150; default: "
             "portable default). Finer than the reconciliation cadence — the local drain reads no "
             "provider, so it delivers already-safe pending rows more promptly at zero provider cost.",
    )
    p.add_argument("--json", action="store_true", dest="as_json", help="Emit a structured JSON result.")
    p.add_argument("--home", default=None, help=argparse.SUPPRESS)  # test/debug: override mozyo home
    p.add_argument("--store-path", dest="store_path", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_workflow_supervisor)


__all__ = ("cmd_workflow_supervisor", "register_supervisor")
