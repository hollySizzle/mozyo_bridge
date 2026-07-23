from __future__ import annotations

import argparse
import difflib
import os
import sys
import time
from pathlib import Path

# ``run_doctor`` / ``format_doctor_text`` stay importable here as the preserved
# ``commands.run_doctor`` / ``commands.format_doctor_text`` monkeypatch seams:
# the ``cmd_doctor`` adapter (moved to ``doctor_command``, #13104) resolves them
# through this module's globals at call time. ``DoctorCommandUseCase`` /
# ``cmd_doctor`` are re-exported for the pre-move import surface.
from mozyo_bridge.application.doctor import format_doctor_text, run_doctor  # noqa: F401
from mozyo_bridge.application.doctor_command import (  # noqa: F401
    DoctorCommandUseCase,
    cmd_doctor,
)
# The ``cmd_id`` / ``cmd_resolve`` / ``cmd_read`` / ``cmd_type`` / ``cmd_message``
# / ``cmd_keys`` thin adapters and their ``_emit_pane_primitive_outcome`` print
# helper moved to ``pane_primitive_command`` next to the use case they compose
# (#13121); all are re-exported so the ``commands.cmd_*`` import surface, the
# cli / cli_core / cli_handoff parser bindings, and the tests that patch
# ``commands.cmd_read`` / ``.cmd_message`` / ``.cmd_keys`` are unchanged (the
# notify / session-bootstrap internal callers resolve the handlers through this
# module's attributes at call time). ``LivePanePrimitiveOps`` keeps routing every
# tmux / pane / read-marker primitive through this module's globals at call time,
# so the ``commands.require_tmux`` / ``.run_tmux`` / ``.wait_for_text`` /
# ``.capture_pane`` monkeypatch seams are unchanged (#12932).
from mozyo_bridge.application.pane_primitive_command import (  # noqa: F401
    LivePanePrimitiveOps,
    PanePrimitiveOutcome,
    PanePrimitiveUseCase,
    _emit_pane_primitive_outcome,
    cmd_id,
    cmd_keys,
    cmd_message,
    cmd_read,
    cmd_resolve,
    cmd_type,
)
# The handoff delivery-record output/persistence seams moved bodily into the
# ``handoff_delivery_command`` boundary (#13123, after the #12981 body carve);
# they are re-exported under the historical ``_``-prefixed names so the
# ``orchestrate_handoff`` call sites (including the ``emit=`` callback handed to
# the f_140 gateway-route gate) and any ``commands.*`` importer are unchanged.
# ``_emit_receipt`` has no remaining caller here (the receipt is emitted inside
# ``DeliveryRecordUseCase.maybe_persist``) and is kept importable for
# compatibility only.
from mozyo_bridge.application.handoff_delivery_command import (  # noqa: F401
    DeliveryRecordUseCase,
    LiveDeliveryRecordOps,
    deliver_outcome as _emit_outcome,
    deliver_receipt as _emit_receipt,
    maybe_persist_delivery_record as _maybe_persist_delivery_record,
    record_command_from_args as _record_command_from_args,
    record_format_from_args as _record_format_from_args,
    record_format_from_value as _record_format_from_value,
    record_herdr_send_ledger as _record_herdr_send_ledger,
    submit_lines_for as _submit_lines_for,
)
from mozyo_bridge.application.turn_start_observation import (  # noqa: F401
    observe_queue_enter_turn_start as _observe_queue_enter_turn_start,
    observe_standard_turn_start as _observe_standard_turn_start,
    project_herdr_turn_start as _project_herdr_turn_start,
    queue_enter_turn_start_record_lines as _queue_enter_turn_start_record_lines,
    resolve_turn_start_window as _resolve_turn_start_window,
    turn_start_record_lines as _turn_start_record_lines,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (  # noqa: F401
    turn_start_rail_record_lines as _turn_start_rail_record_lines,
)
from mozyo_bridge.application.handoff_target_activation_command import (
    LiveTargetActivationOps,
    TargetActivationUseCase,
)
from mozyo_bridge.application.commands_common import (
    config_path_from_args,
    repo_root_from_args,
    scaffold_target_from_args,
)
from mozyo_bridge.application.commands_tmux_ui import (
    cmd_config,
    cmd_tmux_ui_install,
    cmd_tmux_ui_status,
    cmd_tmux_ui_uninstall,
)
from mozyo_bridge.application.commands_agents import (
    ResolveAgentTargetsUseCase,
    _attention_for_candidate,
    cmd_agents_attention_project,
    cmd_agents_list,
    cmd_agents_targets,
    cmd_list,
)
from mozyo_bridge.application.agent_discovery_port import LiveAgentDiscovery
from mozyo_bridge.application.status_session_helper import (
    LegacyBasenameNoticeUseCase,
    LiveStatusSessionHelperReads,
    ResolveStatusSessionUseCase,
    SessionCwdMismatchUseCase,
    cwd_is_under_repo,
)
from mozyo_bridge.application.commands_status import (
    StatusCommandRequest,
    live_status_handler,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.commands_workspace import (
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.commands_session import (
    cmd_session_boundary_prompt,
    cmd_session_list,
    cmd_session_name,
    cmd_session_pane_decision,
    cmd_session_vscode_settings,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_UNKNOWN,
    VIEW_KIND_COCKPIT_PANE,
    infer_repo_root,
    project_preflight_target,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    KIND_LABELS,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    MODES,
    QueueEnterRetryOutcome,
    RECEIVERS,
    SOURCES,
    SOURCE_TICKETLESS,
    TargetActivationOutcome,
    make_outcome,
    resolve_queue_enter_retry_policy,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import effective_send_mode, send_semantic_gap, send_semantic_message  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_command_input_adapter import (
    HandoffNamespaceAdapter,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_envelope_planner import (
    EnvelopePlanError,
    HandoffEnvelopePlanner,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_herdr_standard_rail import (
    HerdrStandardRailRequest,
    run_herdr_standard_rail,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_tmux_transport_rail import (
    TmuxTransportRailRequest,
    run_tmux_transport_rail,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_target_resolution import (
    TargetResolutionRequest,
    run_target_resolution,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_admission_pipeline import (
    AdmissionPipelineRequest,
    run_admission_pipeline,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.workspace_registry import (
    resolve_canonical_session,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    AGENT_COMMANDS,
    clear_read,
    current_pane,
    current_session_name,
    ensure_agent_target,
    find_agent_window,
    is_agent_process,
    is_tmux_target,
    mark_read,
    pane_info,
    require_read,
    resolve_target,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.queue_reader import find_handoff_task
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    capture_pane,
    pane_lines,
    pane_location,
    pane_window_name,
    rename_session,
    rename_window,
    require_tmux,
    run_tmux,
    session_exists,
    source_tmux_conf,
)
# Runtime terminal-transport backend switch (Redmine #13253). The wiring lives in
# its own module so ``commands.py`` does not grow; the decorator resolves the
# repo-local ``terminal_transport`` selection and, only for the opt-in herdr
# backend, swaps this module's ``run_tmux`` / ``capture_pane`` for a tmux-shaped
# herdr shim around the send — the tmux default installs nothing (byte-for-byte).
from mozyo_bridge.application.handoff_transport_wiring import bind_runtime_transport

# Redmine #13255: the herdr event-driven turn-start rail installed by
# ``bind_runtime_transport`` for the duration of a herdr send. ``None`` for the
# tmux default (and outside a send); the herdr+standard branch of
# ``orchestrate_handoff`` reads it to drive the rail in place of the capture-based
# ``_observe_standard_turn_start``. Module-global (like the ``run_tmux`` /
# ``capture_pane`` swap) so the decorator can install and restore it without
# changing ``orchestrate_handoff``'s signature.
active_herdr_turn_start_rail = None
# Redmine #13261 (increment 2): under `terminal_transport.backend: herdr` the send
# target is resolved herdr-natively (launch-time sender identity + live inventory)
# instead of via the tmux pane resolver, so a pure herdr session (no tmux server)
# routes. Strictly config-guarded; the tmux path is untouched.
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    herdr_auto_target_repo,
    herdr_effective_backend_selected,
    resolve_herdr_send_target,
)
from mozyo_bridge.scaffold.rules import (
    PORTABLE_HOME_EXPRESSION,
    install_rules,
    mozyo_bridge_home,
    render_scaffold_files,
    resolve_rules_store,
    rules_status,
    scaffold_status,
    write_scaffold,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import (
    default_queue_path,
    default_tmux_conf,
)


def queue_path_from_args(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "queue", None) or default_queue_path(repo_root_from_args(args))).expanduser()


def load_tmux_conf_for(args: argparse.Namespace) -> bool:
    """Auto-startup config loader.

    Skips silently when the resolved path is the default and the file is
    missing, so bare ``mozyo`` and ``notify-*`` paths do not block on a
    missing tmux config. An explicit user-supplied ``--config-path`` still
    errors when the file is missing.
    """
    optional = bool(getattr(args, "config_path_was_default", False))
    return source_tmux_conf(config_path_from_args(args), optional=optional)


def _agents_target_candidates(args: argparse.Namespace) -> list:
    """Shared discovery → ``TargetRecord`` candidate pipeline (#11811 / #11907).

    Thin wrapper over :class:`~mozyo_bridge.application.commands_agents.ResolveAgentTargetsUseCase`
    (over the injected :class:`~mozyo_bridge.application.agent_discovery_port.AgentDiscoveryPort`
    — the #12749 / #12638 / #12785 OOP-first read tranche); keeps the public name so the
    ``agents targets`` / attention handlers and the delegated-coordinator / project-gateway
    callers (and tests) that import ``commands._agents_target_candidates`` are unchanged. The
    composition-injected ``args.snapshot`` (Redmine #13569 R2-F1) is threaded into the discovery
    adapter and the use-case validation so the runtime read uses the SAME provider vocabulary
    the CLI choices came from; ``None`` uses the built-in providers.
    """
    _s = getattr(args, "snapshot", None)
    return ResolveAgentTargetsUseCase(LiveAgentDiscovery(snapshot=_s)).resolve(
        agent_filter=getattr(args, "agent", None),
        session_filter=getattr(args, "session", None),
        snapshot=_s,
    )


# ``cmd_agents_attention_project`` (and the shared ``_attention_for_candidate``
# helper) were relocated to :mod:`mozyo_bridge.application.commands_agents` as the
# OOP-first attention-projection tranche (Redmine #12749 / #12638 / #12785): the
# tmux pane-option *write* now goes through a ``TmuxOptionWriterPort`` driven by a
# ``ProjectAttentionUseCase`` returning typed ``AttentionProjectionEntry`` value
# objects (fake-port tested), replacing the naked ``run_tmux`` apply loop. Both are
# re-exported at the top of this module so
# ``mozyo_bridge.application.commands.cmd_agents_attention_project`` /
# ``_attention_for_candidate`` keep their identity. The shared
# ``_agents_target_candidates`` discovery pipeline and the ``agents list`` /
# ``agents targets`` read handlers below remain here (residual to #12638 / #12785).


# The ``agents list`` / ``agents targets`` render handlers (and the attention
# projection earlier) were relocated to
# :mod:`mozyo_bridge.application.commands_agents` (Redmine #12749 / #12638 /
# #12785). They are re-exported at the top of this module so
# ``mozyo_bridge.application.commands.cmd_agents_list`` / ``cmd_agents_targets``
# and the cli / cli_agents parser registrar keep their identity and
# ``func.__name__``. The thin ``_agents_target_candidates`` discovery wrapper
# (driving ``ResolveAgentTargetsUseCase`` via the ``AgentDiscoveryPort``) stays
# here as the shared seam the delegated-coordinator / project-gateway callers and
# their tests import.


# The ``tmux-ui-config`` and ``tmux-ui install/uninstall/status`` command
# handlers were relocated to :mod:`mozyo_bridge.application.commands_tmux_ui`
# as the OOP-first first-conversion tranche (Redmine #12749 / #12638 / #12785):
# a ``TmuxControlPort`` (port-adapter), an ``ApplyTmuxConfigUseCase`` (use case
# with port injection + fake-port test), and typed request/result value objects
# replace the old naked ``require_tmux`` / ``source_tmux_conf`` procedural
# handlers. They are re-exported at the top of this module so
# ``mozyo_bridge.application.commands.cmd_config`` / ``cmd_tmux_ui_*`` and the
# cli_cockpit parser registrar keep their identity and ``func.__name__``.


# The ``cmd_id`` / ``cmd_resolve`` / ``cmd_read`` / ``cmd_type`` / ``cmd_message``
# / ``cmd_keys`` pane primitive adapters (the #12932 / #12638 OOP-first carve)
# and their ``_emit_pane_primitive_outcome`` print helper moved to
# ``pane_primitive_command.py`` next to the ``PanePrimitiveUseCase`` they compose
# (#13121); all are re-exported at the top of this module. The sibling
# ``_emit_handoff_marker_timeout_guidance`` below stays here (its caller is the
# out-of-scope ``orchestrate_handoff`` strict rail).


def _emit_handoff_marker_timeout_guidance(receiver: str) -> None:
    """Thin seam over :meth:`DeliveryRecordUseCase.emit_marker_timeout_guidance`.

    The strict-rail ``handoff send`` marker_timeout stderr trailer (Asana task
    1214779823377861) moved to ``application/handoff_delivery_command.py`` (#12981)
    as the pure :func:`~mozyo_bridge.application.handoff_delivery_command.marker_timeout_guidance_lines`
    plus this use-case emit; kept module-level because its caller is the
    ``orchestrate_handoff`` strict rail.
    """
    DeliveryRecordUseCase(LiveDeliveryRecordOps()).emit_marker_timeout_guidance(receiver)


# Per-agent window status-bar styling under the bare-`mozyo` window model.
# The window-name rail (`claude` / `codex`) is the resolver and notification
# routing key, so renaming is not an option for "make it easier to tell which
# window is which". Instead we attach a subtle per-window status style to the
# windows we create / promote, so the tmux status bar entry for that window
# is colored without touching the window name, the pane content background,
# or the user's global status bar config.
#
# Colors are picked from the 256-color palette and stay restrained:
#   claude → muted sage green  (colour108, fg only)
#   codex  → muted slate blue  (colour67,  fg only)
# Other windows are intentionally left at the tmux default (neutral) so the
# contrast is "agent windows look subtly tinted, anything else looks default".
# No background fill, no blinking, no icon glyphs, no high-saturation hues.
AGENT_WINDOW_STATUS_COLORS = {
    "claude": "colour108",
    "codex": "colour67",
}


def apply_window_subtle_style(session: str, window_name: str) -> bool:
    """Attach a restrained per-window status-bar style to ``session:window_name``.

    Returns True when a style was applied for a recognized agent window name
    (``claude`` / ``codex``). Returns False for any other window — including
    user-created legacy windows in the same session — so the helper is a
    no-op for windows the operator owns.

    Idempotent: tmux ``set-window-option`` overwrites the prior value, so
    repeated invocations from ``ensure_repo_session_windows`` are safe.
    Window-scoped (``-t session:window``) so the user's global
    ``set -g window-status-style`` from their ``.tmux.conf`` is preserved
    for every other window in the session.
    """
    color = AGENT_WINDOW_STATUS_COLORS.get(window_name)
    if color is None:
        return False
    target = f"{session}:{window_name}"
    # Non-current window: just the foreground color so the status entry
    # reads as a quietly tinted name. No background fill.
    run_tmux(
        "set-window-option",
        "-t",
        target,
        "window-status-style",
        f"fg={color}",
        check=False,
    )
    # Current (focused) window: same hue, made slightly heavier with bold so
    # operators can still tell "this is the active agent" at a glance. tmux's
    # default current-window style normally flips fg/bg; this override keeps
    # the subtle hue and replaces the inversion with a typographic cue.
    run_tmux(
        "set-window-option",
        "-t",
        target,
        "window-status-current-style",
        f"fg={color},bold",
        check=False,
    )
    return True


def otel_bootstrap_env(
    agent: str, session: str, cwd: str | None = None
) -> dict[str, str]:
    """OTel env injected at agent launch (Redmine #11676).

    The session bootstrap is the source of truth for telemetry env: the
    measured CLIs carry no pid/cwd in their OTel payloads, so the join
    keys (`mozyo.session` / `mozyo.agent` / `mozyo.workspace_id`) ride in
    OTEL_RESOURCE_ATTRIBUTES. Every injected value is a tmux-safe ASCII
    identifier — never a path or free-form text — so no baggage-encoding
    ambiguity exists. `OTEL_LOG_USER_PROMPTS` is deliberately never set:
    prompt-content recording stays OFF by contract (#11639 constraint 4).
    Telemetry export is best-effort; a down receiver costs nothing but
    lost events.
    """
    attributes = [f"mozyo.session={session}", f"mozyo.agent={agent}"]
    if cwd:
        try:
            workspace_id = resolve_canonical_session(cwd).workspace_id
        except Exception:
            workspace_id = None
        if workspace_id:
            attributes.append(f"mozyo.workspace_id={workspace_id}")
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_RESOURCE_ATTRIBUTES": ",".join(attributes),
    }


def new_agent_session_window(agent: str, session: str, cwd: str | None = None) -> str:
    """Open a fresh detached session whose first window runs ``agent``.

    Thin wrapper over the ``launch_command`` agent-window boundary (#12970); the
    ``require_tmux`` / ``run_tmux`` / ``_agent_launch_command`` /
    ``_record_managed_pane_created`` / ``die`` seams stay ``commands.*`` names
    (the launch helper tail is re-exported from ``launch_command`` since #13120)
    and the live adapter resolves them through this module at call time, so the
    boundary tests that patch ``commands.<fn>`` are unchanged.
    """
    from mozyo_bridge.application.launch_command import (
        AgentWindowLaunchUseCase,
        LiveAgentWindowLaunchOps,
    )

    return AgentWindowLaunchUseCase(LiveAgentWindowLaunchOps()).new_session_window(
        agent, session, cwd
    )


def new_agent_window(agent: str, session: str, cwd: str | None = None) -> str:
    """Add an ``agent`` window to an existing session.

    Thin wrapper over the ``launch_command`` agent-window boundary (#12970);
    mirrors :func:`new_agent_session_window`.
    """
    from mozyo_bridge.application.launch_command import (
        AgentWindowLaunchUseCase,
        LiveAgentWindowLaunchOps,
    )

    return AgentWindowLaunchUseCase(LiveAgentWindowLaunchOps()).new_window(
        agent, session, cwd
    )


# The bare ``mozyo`` / repo session bootstrap helper tail (``list_session_windows``
# / ``wait_for_agent_terminal_pane`` / ``wait_for_text`` / ``_marker_visible_in``
# / ``rollback_unsubmitted_input`` / ``ensure_repo_session_windows``) was carved
# into an OOP-first boundary (Redmine #12975 / #12638): a ``SessionBootstrapOps``
# port over the tmux / pane / session primitives, a ``LiveSessionBootstrapOps``
# adapter that routes those primitives through this module's globals at call time
# (so the existing ``commands.*`` monkeypatch seams — and the notify /
# pane-primitive / status / launch boundaries that reach ``commands.wait_for_text``
# / ``.rollback_unsubmitted_input`` / ``.list_session_windows`` /
# ``.ensure_repo_session_windows`` at call time — are unchanged), the pure
# ``marker_visible_in`` / ``project_session_window_names`` projections, and a
# ``SessionBootstrapUseCase`` owning the five behavior-preserving flows. The
# functions below stay thin module-level wrappers (build the live ops, run the use
# case) so the parser bindings, the ``orchestrate_handoff`` strict-rail callers of
# ``wait_for_text`` / ``_marker_visible_in``, and the tests that patch these
# ``commands.*`` names keep working unchanged.


def _session_bootstrap_use_case():
    from mozyo_bridge.application.session_bootstrap_command import (
        LiveSessionBootstrapOps,
        SessionBootstrapUseCase,
    )

    return SessionBootstrapUseCase(LiveSessionBootstrapOps())


def list_session_windows(session: str) -> list[str]:
    return _session_bootstrap_use_case().list_session_windows(session)


def wait_for_agent_terminal_pane(pane_id: str, agent: str, timeout: float) -> None:
    _session_bootstrap_use_case().wait_for_agent_terminal_pane(pane_id, agent, timeout)


def wait_for_text(target: str, text: str, lines: int, timeout: float) -> bool:
    return _session_bootstrap_use_case().wait_for_text(target, text, lines, timeout)


def _marker_visible_in(captured: str, text: str) -> bool:
    from mozyo_bridge.application.session_bootstrap_command import marker_visible_in

    return marker_visible_in(captured, text)


def rollback_unsubmitted_input(target: str) -> None:
    _session_bootstrap_use_case().rollback_unsubmitted_input(target)


def ensure_repo_session_windows(args: argparse.Namespace) -> list[str]:
    return _session_bootstrap_use_case().ensure_repo_session_windows(args)


def cmd_mozyo(args: argparse.Namespace) -> int:
    """Bare ``mozyo`` entrypoint: repo-aware session with one window per agent.

    Resolves the repo root — failing closed when that root carries no mozyo
    adoption marker (``.mozyo-bridge/config.yaml`` / scaffold manifest /
    workspace anchor, Redmine #13379), so an unadopted directory can no longer
    silently resolve up to an incidental ancestor (the home directory in the
    observed trap) and start real agents there — resolves the session name via
    :func:`resolve_canonical_session` (the registered canonical session name
    from the home registry / workspace anchor when this workspace was
    registered with ``mozyo-bridge workspace register`` (Redmine #11429);
    otherwise derived from the path — the workspace-defaults Redmine
    identifier when present, else a collision-safe repo-path fallback, never
    a low-information ``____``-style name), ensures a single repo-scoped
    session containing a ``claude`` window and a ``codex`` window, and
    attaches unless ``--no-attach`` was given. An explicit ``--session NAME``
    still overrides the resolved name (Redmine #10796).

    The session-name resolution guards, the JSON payload, the session/window-table
    rendering, the attach-command form, and the outcome delivery — stdout, the
    fail-closed ``die``, and the terminal ``os.execvp`` attach — live behind the
    ``launch_command`` boundary (#12933 / #12984 / #13105); this handler is a
    parser-bound wrapper over run + deliver. The live adapter routes ``die``
    through this module and ``attach`` through :func:`os.execvp` at call time, so
    the ``commands.die`` / ``commands.os.execvp`` patch seams keep intercepting.
    """
    from mozyo_bridge.application.launch_command import (
        LiveLaunchOps,
        MozyoLaunchUseCase,
        deliver_mozyo_launch_outcome,
    )

    ops = LiveLaunchOps()
    return deliver_mozyo_launch_outcome(MozyoLaunchUseCase(ops).run(args), ops)


def _resolve_project_scope_fields(
    cwd: str | None, repo_root: str | None
) -> tuple[str | None, tuple[str | None, str | None, str | None], str | None]:
    """Thin wrapper over the #12977 :func:`resolve_project_scope_fields` leaf.

    The fail-soft Git-root + project-scope resolution (Redmine #12658) lives
    behind the ``cockpit_planner_command`` boundary; this stays here as the
    ``commands._resolve_project_scope_fields`` name so ``cmd_cockpit`` / the
    workspace resolver — and the tests that patch this seam — keep intercepting.
    """
    from mozyo_bridge.application.cockpit_planner_command import (
        resolve_project_scope_fields,
    )

    return resolve_project_scope_fields(cwd, repo_root)


def _resolve_cockpit_workspaces(args: argparse.Namespace) -> list:
    """Thin wrapper over the #12977 :class:`CockpitWorkspacesUseCase` boundary.

    The live adapter routes ``resolve_canonical_session`` /
    ``_resolve_workspace_lane`` / ``_resolve_project_scope_fields`` through this
    module (and ``take_inventory`` through its source) at call time, so the
    ``layout apply`` characterization tests that patch those seams still feed the
    explicit-``--repo`` and live-inventory column resolution unchanged (#11788 /
    #11820).
    """
    from mozyo_bridge.application.cockpit_planner_command import (
        CockpitWorkspacesUseCase,
        LiveCockpitWorkspacesOps,
    )

    return CockpitWorkspacesUseCase(LiveCockpitWorkspacesOps()).resolve(args)


def execute_cockpit_plan(plan, run, *, cleanup_captured: bool = False) -> dict:
    """Thin wrapper over the #12977 :class:`CockpitPlanExecutorUseCase` boundary.

    The live adapter binds the passed-in ``run`` (``run_tmux`` or a test fake) and
    routes ``commands.die`` at call time, so the token-resolution / fail-fast /
    ``cleanup_captured`` rollback behavior (#11788 / #11803) is byte-for-byte
    preserved and the executors that call this directly with a fake runner — and
    any that patch ``commands.die`` — keep intercepting.
    """
    from mozyo_bridge.application.cockpit_planner_command import (
        CockpitPlanExecutorUseCase,
        LiveCockpitPlanExecutorOps,
    )

    return CockpitPlanExecutorUseCase(LiveCockpitPlanExecutorOps(run)).execute(
        plan, cleanup_captured=cleanup_captured
    )


def _cockpit_repair_use_case(run):
    """Build the #12972 :class:`CockpitRepairUseCase` over the passed-in ``run``.

    The live adapter binds this call's ``run`` (the module-level ``run_tmux`` or a
    test's fake) and routes ``die`` through this module at call time, so the
    characterization tests that call ``execute_*_plan`` directly with a fake
    runner — and any that patch ``commands.die`` — keep intercepting unchanged.
    """
    from mozyo_bridge.application.cockpit_repair_command import (
        CockpitRepairUseCase,
        LiveCockpitRepairOps,
    )

    return CockpitRepairUseCase(LiveCockpitRepairOps(run))


def execute_cockpit_adopt_plan(plan, run) -> dict:
    """Thin wrapper over the #12972 :class:`CockpitRepairUseCase` adopt path (#11898)."""
    return _cockpit_repair_use_case(run).execute_adopt(plan)


def execute_peer_adopt_plan(plan, run) -> None:
    """Thin wrapper over the #12972 :class:`CockpitRepairUseCase` peer-adopt path (#12133)."""
    _cockpit_repair_use_case(run).execute_peer_adopt(plan)


def execute_cockpit_reset_plan(plan, run) -> None:
    """Thin wrapper over the #12972 :class:`CockpitRepairUseCase` reset path (#11814)."""
    _cockpit_repair_use_case(run).execute_reset(plan)


def execute_cockpit_rebalance_plan(plan, run) -> None:
    """Thin wrapper over the #12972 :class:`CockpitRepairUseCase` rebalance path (#12135)."""
    _cockpit_repair_use_case(run).execute_rebalance(plan)


def execute_cockpit_reconcile_plan(plan, run) -> None:
    """Thin wrapper over the #12972 :class:`CockpitRepairUseCase` reconcile path (#12136)."""
    _cockpit_repair_use_case(run).execute_reconcile(plan)


def cmd_layout_apply(args: argparse.Namespace) -> int:
    """`mozyo layout apply cockpit` — build/focus the cockpit layout (#11788).

    Active workspaces become horizontal columns; within each column the agents
    are a vertical split (Codex top, Claude bottom) at `--ratio`. tmux state is
    the layout's source of truth; `--cc` only swaps the attach for control mode.
    `--json` / `--dry-run` emit the planned tmux commands without touching tmux.

    The preset/workspace guards, the plan JSON, the dry-run text, and the outcome
    delivery — stdout, the fail-closed ``die``, and the terminal ``os.execvp``
    attach — live behind the ``launch_command`` boundary (#12933 / #13105); this
    handler is a parser-bound wrapper over run + deliver. The live adapter routes
    ``die`` through this module and ``attach`` through :func:`os.execvp` at call
    time, so the ``commands.die`` / ``commands.os.execvp`` patch seams keep
    intercepting.
    """
    from mozyo_bridge.application.launch_command import (
        CockpitLayoutUseCase,
        LiveLaunchOps,
        deliver_layout_launch_outcome,
    )

    ops = LiveLaunchOps()
    return deliver_layout_launch_outcome(CockpitLayoutUseCase(ops).run(args), ops)


def _read_cockpit_columns(session: str, window: str | None = None):
    """Read a cockpit window's panes with their workspace+lane identity (#11803, #11820).

    Returns a list of ``{pane_id, workspace_id, role, lane_id, pane_left,
    pane_width, project_scope, project_path, project_label}`` (one per pane
    carrying the `@mozyo_workspace_id` user option), or ``None`` when the window
    does not exist. The project triple (Redmine #12658 stamp) rides on the read
    so duplicate / Unit-membership detection can distinguish a department-root
    pane (empty ``project_scope``) from a project-scoped gateway pane that shares
    the same Git root and lane (Redmine #12739). ``window`` defaults to the shared
    `cockpit` window. Pass an explicit window to read a Project Group window
    (#12330): a tmux **window id** (``@N``, server-globally unique) targets that
    exact window unambiguously and is used directly; any other value is taken as a
    window name under ``session`` (``session:name``). Discovery passes the window
    id so a duplicate display name can never make the *name* a routing dependency
    (#12330 review j#62380). Identity is read from the tmux user options, not the
    title. ``lane_id`` is absent (empty) on pre-#11820 panes and normalizes to the
    ``default`` lane at comparison time. The ``pane_left`` / ``pane_width``
    geometry (Redmine #11849) lets append pick the visually rightmost column
    instead of trusting list-panes order.
    """
    from mozyo_bridge.application.cockpit_read_command import (
        CockpitReadUseCase,
        LiveCockpitReadOps,
    )

    # Thin wrapper over the #12971 :class:`CockpitReadUseCase` boundary; the live
    # adapter routes ``run_tmux`` through this module at call time, so the tests
    # patching ``commands.run_tmux`` still intercept the read.
    return CockpitReadUseCase(LiveCockpitReadOps()).read_columns(session, window)


def _read_managed_cockpit_windows(session: str):
    """Read every cockpit-session window that holds a mozyo-managed pane (#12330).

    Faithful multi-window discovery for per-Project-Group windows: lists the
    session's windows by their stable ``#{window_id}`` (plus the display name and
    the mozyo-written ``@mozyo_group_id`` marker), reads each one's panes by that
    **window id**, and returns a list of
    ``{"window_id": <@N>, "window": <name>, "group_id": <hint or "">,
    "columns": [<column>, ...]}`` for every window that carries at least one
    ``@mozyo_workspace_id`` pane — the shared `cockpit` window AND any Project
    Group window.

    The window id is the identifier everything keys on, so a duplicate display
    name (two groups whose labels sanitize to the same string) can never collapse
    or hide a window or make the name a routing dependency (#12330 review j#62380).
    UNIT identity stays on the pane options in ``columns``; ``group_id`` is the
    mozyo-stamped window-level marker the launcher uses to locate a group's
    existing window; ``window`` is display only.

    Read-only and tolerant: a missing tmux binary / server, or an unreadable
    window list, degrades to ``[]`` (no managed windows) rather than raising, so
    `--dry-run` / `--json` stay non-mutating. A window whose pane read fails or
    carries no managed pane is simply omitted.
    """
    from mozyo_bridge.application.cockpit_read_command import (
        CockpitReadUseCase,
        LiveCockpitReadOps,
    )

    # Thin wrapper over the #12971 :class:`CockpitReadUseCase` boundary. The use
    # case reads each window's panes through the live adapter's ``read_columns``
    # seam, which routes ``commands._read_cockpit_columns`` at call time — so the
    # group-window tests that patch that seam with a ``side_effect`` still feed
    # this discovery.
    return CockpitReadUseCase(LiveCockpitReadOps()).read_managed_windows(session)


def _read_cockpit_geometry(session: str):
    """Read every cockpit-window pane with full 2D geometry + identity (#12131).

    Unlike :func:`_read_cockpit_columns` (which serves append/reset and only needs
    the x-axis), the read-only ``doctor-geometry`` diagnostic needs the whole
    rectangle and must also see panes that carry NO ``@mozyo_*`` markers — a
    manually-created / half-bound pane (the #12130 ``%1106`` case) is exactly what
    the role-less detection reports. So this lists every pane in the cockpit window
    and reads ``pane_left`` / ``pane_top`` / ``pane_width`` / ``pane_height`` plus
    the identity options. Returns one ``{pane_id, workspace_id, role, lane_id,
    pane_left, pane_top, pane_width, pane_height}`` per pane, or ``None`` when the
    cockpit window does not exist.

    Read-only and tolerant: a missing tmux binary / server, or a missing cockpit
    window, degrade to ``None`` (no cockpit) rather than raising, so the diagnostic
    never mutates and never aborts.
    """
    from mozyo_bridge.application.cockpit_read_command import (
        CockpitReadUseCase,
        LiveCockpitReadOps,
    )

    # Thin wrapper over the #12971 :class:`CockpitReadUseCase` boundary; the live
    # adapter routes ``run_tmux`` through this module at call time, so the tests
    # patching ``commands.run_tmux`` still intercept the read.
    return CockpitReadUseCase(LiveCockpitReadOps()).read_geometry(session)


def _rightmost_codex_anchor(codex_columns) -> str | None:
    """The codex pane id of the visually rightmost cockpit column (#11849).

    `tmux list-panes` enumeration order is NOT layout order, so anchoring an
    append on the last-listed codex pane can split a middle column and crush the
    layout. Pick by geometry instead: the largest ``pane_left``, tie-broken by
    the right edge (``pane_left + pane_width``) then ``pane_id`` so it stays
    deterministic even when geometry is missing (defaults to 0 → a stable
    pane-id ordering).
    """
    from mozyo_bridge.application.cockpit_read_command import rightmost_codex_anchor

    # Thin wrapper over the pure #13106 boundary pick; the append / adopt /
    # dispatcher live adapters keep routing ``commands._rightmost_codex_anchor``
    # at call time, so their monkeypatch seam is unchanged.
    return rightmost_codex_anchor(codex_columns)


def _cockpit_session_present(session: str) -> bool:
    """Tolerant `has-session` for the cockpit (#11803 review).

    Distinguishes "session absent" from "session present but cockpit window
    missing", so a real-run create never `new-session`s against (and the
    cleanup never kills) a pre-existing session. Tolerant: any tmux error
    degrades to ``False``.
    """
    from mozyo_bridge.application.cockpit_read_command import (
        CockpitReadUseCase,
        LiveCockpitReadOps,
    )

    # Thin wrapper over the #13106 session-helper reads on the cockpit read
    # boundary; the live adapter resolves ``commands.session_exists`` at call
    # time, so the tests patching that name still intercept.
    return CockpitReadUseCase(LiveCockpitReadOps()).session_present(session)


def _session_attached_clients_result(session: str) -> tuple[tuple[str, ...], bool]:
    """``(clients, known)`` for ``session`` — distinguishes "no client" from "could not read".

    ``known`` is ``False`` when the tmux ``list-clients`` read failed (exception
    or non-zero exit), so a caller can fail closed on an *unknown* client state
    instead of mistaking it for "no client attached". The destructive cockpit
    reset/rebuild gate needs that distinction (Redmine #11814 review j#57928);
    adopt's tolerant :func:`_session_attached_clients` keeps the old "any error ->
    no client" shape, which is safe there because a failed read on a source
    session that may not even exist means there is no live client to protect.
    """
    from mozyo_bridge.application.cockpit_read_command import (
        CockpitReadUseCase,
        LiveCockpitReadOps,
    )

    # Thin wrapper over the #13106 session-helper reads on the cockpit read
    # boundary; the live adapter resolves ``commands.run_tmux`` at call time,
    # so the tests patching that name still feed this read.
    return CockpitReadUseCase(LiveCockpitReadOps()).attached_clients_result(session)


def _session_attached_clients(session: str) -> tuple[str, ...]:
    """tty names of clients attached to ``session`` (Redmine #11898, tolerant).

    Adopt fails closed when the source normal session has an attached client:
    moving its panes out from under a live client is disruptive (the client may
    be left blank or the session torn down beneath it). Any tmux error degrades
    to ``()`` — there is no client to protect when tmux can't be queried. When the
    success/failure distinction matters (destructive reset/rebuild), use
    :func:`_session_attached_clients_result` instead.
    """
    return _session_attached_clients_result(session)[0]


def _source_session_cleanup_note(source_session: str) -> str:
    """Explicit (never-implicit-kill) report of the source session after adopt (#11898).

    Adopt moves only the two agent panes; tmux destroys a window/session whose
    last pane is moved away, so an emptied source session is cleaned up *by tmux*,
    not by an explicit ``kill-session`` from this tool (acceptance: cleanup must
    be explicit and logged, never an implicit kill). This reports the resulting
    state — gone (tmux closed it) or still alive with N remaining pane(s), left
    intact — so the operator sees exactly what happened. Tolerant / read-only.
    """
    from mozyo_bridge.application.cockpit_read_command import (
        CockpitReadUseCase,
        LiveCockpitReadOps,
    )

    # Thin wrapper over the #13106 session-helper reads on the cockpit read
    # boundary; the live adapter resolves ``commands.session_exists`` /
    # ``commands.run_tmux`` at call time, so the adopt tests patching those
    # names still drive both the presence probe and the pane count.
    return CockpitReadUseCase(LiveCockpitReadOps()).source_session_cleanup_note(
        source_session
    )


def _probe_checkout_facts(repo_root: str) -> dict:
    """Best-effort git checkout facts for lane identity (Redmine #11820).

    Tolerant: a non-git workspace, a missing path, a missing git binary, or any
    git error yields empty facts so :func:`resolve_lane_identity` falls back to
    the backward-compatible ``default`` lane. Never raises.
    """
    import subprocess

    facts = {"git_dir": None, "git_common_dir": None, "branch": None}
    try:
        if not os.path.isdir(repo_root):
            return facts
    except OSError:
        return facts

    def _git(*git_args):
        try:
            result = subprocess.run(
                ["git", "-C", repo_root, *git_args],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    def _abs(path):
        if not path:
            return None
        return os.path.realpath(
            path if os.path.isabs(path) else os.path.join(repo_root, path)
        )

    facts["git_dir"] = _abs(_git("rev-parse", "--git-dir"))
    facts["git_common_dir"] = _abs(_git("rev-parse", "--git-common-dir"))
    facts["branch"] = _git("branch", "--show-current") or _git(
        "rev-parse", "--short", "HEAD"
    )
    return facts


def _registered_canonical_path(workspace_id):
    """Registered canonical checkout path for ``workspace_id`` (tolerant).

    Used to flag a *relocated* checkout (a clone/copy that shares the workspace
    id via a duplicated anchor) as a distinct lane. Any registry error / absent
    record yields ``None`` so derivation degrades to the ``default`` lane.
    """
    if not workspace_id:
        return None
    try:
        from mozyo_bridge.workspace_registry import load_workspace_by_id

        record = load_workspace_by_id(workspace_id)
    except Exception:
        return None
    return getattr(record, "canonical_path", None) if record is not None else None


def _resolve_workspace_lane(repo_root: str, workspace_id):
    """Derive the lane identity for ``repo_root`` (Redmine #11820).

    Ties the tolerant git probe + registered canonical path to the pure
    :func:`resolve_lane_identity`. Returns the backward-compatible ``default``
    lane for the primary checkout / a non-git workspace.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import resolve_lane_identity

    facts = _probe_checkout_facts(repo_root)
    return resolve_lane_identity(
        repo_root=repo_root,
        canonical_path=_registered_canonical_path(workspace_id),
        git_dir=facts.get("git_dir"),
        git_common_dir=facts.get("git_common_dir"),
        branch=facts.get("branch"),
    )


def _coexisting_normal_observations(cockpit_session: str):
    """Project the live inventory into normal-`mozyo` adopt observations (#11897).

    Thin wrapper (Redmine #12987): the projection body lives in
    :mod:`mozyo_bridge.application.cockpit_adopt_command`
    (:meth:`CockpitAdoptUseCase.coexisting_normal_observations` over the pure
    ``project_normal_session_observations``). Kept here with the original
    signature so the integration tests and the advisory keep calling through
    ``commands.*``; the live ops route ``take_inventory`` from its source module
    and ``_resolve_workspace_lane`` back through this module at call time, so
    those patch seams are unchanged.
    """
    from mozyo_bridge.application.cockpit_adopt_command import (
        CockpitAdoptUseCase,
        LiveCockpitAdoptOps,
    )

    return CockpitAdoptUseCase(LiveCockpitAdoptOps()).coexisting_normal_observations(
        cockpit_session
    )


def _cockpit_adopt_advisory(workspace, cockpit_session: str):
    """Detect a co-existing normal `mozyo` session for ``workspace`` (#11897).

    Thin wrapper (Redmine #12987) over
    :meth:`~mozyo_bridge.application.cockpit_adopt_command.CockpitAdoptUseCase.adopt_advisory`.
    Kept here with the original signature because the cockpit tests patch
    ``commands._cockpit_adopt_advisory`` and ``cmd_cockpit`` / the adopt handler
    reach the advisory through this seam.
    """
    from mozyo_bridge.application.cockpit_adopt_command import (
        CockpitAdoptUseCase,
        LiveCockpitAdoptOps,
    )

    return CockpitAdoptUseCase(LiveCockpitAdoptOps()).adopt_advisory(
        workspace, cockpit_session
    )


def _resolve_cockpit_adopt(
    workspace, session, *, columns, session_present, already_in_cockpit,
    existing_codex, advisory, codex_ratio=70,
):
    """Decide the adopt move for ``mozyo cockpit adopt`` — plan or fail-closed (#11898).

    Thin wrapper (Redmine #12987) over
    :meth:`~mozyo_bridge.application.cockpit_adopt_command.CockpitAdoptUseCase.resolve_adopt`.
    Returns ``(plan, blocked_reason, source_clients)`` unchanged; the live ops
    route the ``_session_attached_clients`` / ``_rightmost_codex_anchor`` reads
    back through this module at call time, so those patch seams are unchanged.
    """
    from mozyo_bridge.application.cockpit_adopt_command import (
        CockpitAdoptUseCase,
        LiveCockpitAdoptOps,
    )

    return CockpitAdoptUseCase(LiveCockpitAdoptOps()).resolve_adopt(
        workspace, session, columns=columns, session_present=session_present,
        already_in_cockpit=already_in_cockpit, existing_codex=existing_codex,
        advisory=advisory, codex_ratio=codex_ratio,
    )


def _handle_cockpit_adopt(
    args, workspace, session, *, columns, session_present, already_in_cockpit,
    existing_codex,
):
    """Route `mozyo cockpit adopt` — detect-only preview vs confirm-gated move (#11898).

    Thin wrapper (Redmine #12987) over
    :meth:`~mozyo_bridge.application.cockpit_adopt_command.CockpitAdoptUseCase.handle`.
    Kept here with the original signature for the ``cmd_cockpit`` adopt
    dispatch; the live ops route the advisory / client / anchor / tmux /
    executor seams back through this module at call time (and render through a
    plain ``print`` sink), so the patch seams and the preview / json / confirm
    output are byte-for-byte unchanged.
    """
    from mozyo_bridge.application.cockpit_adopt_command import (
        CockpitAdoptUseCase,
        LiveCockpitAdoptOps,
    )

    return CockpitAdoptUseCase(LiveCockpitAdoptOps()).handle(
        args, workspace, session, columns=columns, session_present=session_present,
        already_in_cockpit=already_in_cockpit, existing_codex=existing_codex,
    )


def _assess_cockpit_reset(session, *, columns, session_present):
    """Grade the cockpit session for `mozyo cockpit reset` / `rebuild` (#11814).

    Thin wrapper (Redmine #12989) over
    :meth:`~mozyo_bridge.application.cockpit_reset_command.CockpitResetUseCase.assess`.
    Kept here with the original signature; the live ops route the
    ``_session_attached_clients_result`` / ``list_session_windows`` reads back
    through this module at call time, so those patch seams are unchanged.
    """
    from mozyo_bridge.application.cockpit_reset_command import (
        CockpitResetUseCase,
        LiveCockpitResetOps,
    )

    return CockpitResetUseCase(LiveCockpitResetOps()).assess(
        session, columns=columns, session_present=session_present
    )


def _cockpit_extra_windows(target):
    """Managed-session windows a reset's `kill-session` destroys beyond `cockpit` (#12330).

    Thin wrapper (Redmine #12989) over the pure
    :func:`~mozyo_bridge.application.cockpit_reset_command.cockpit_extra_windows`.
    Kept here because the group-window characterization test calls
    ``commands._cockpit_extra_windows`` directly.
    """
    from mozyo_bridge.application.cockpit_reset_command import cockpit_extra_windows

    return cockpit_extra_windows(target)


def _handle_cockpit_reset(
    args, workspace, session, *, columns, session_present, rebuild, launch,
    codex_ratio,
):
    """Route `mozyo cockpit reset` / `rebuild` — preview vs confirm-gated teardown (#11814).

    Thin wrapper (Redmine #12989) over
    :meth:`~mozyo_bridge.application.cockpit_reset_command.CockpitResetUseCase.handle`.
    Kept here with the original signature for the ``cmd_cockpit`` reset/rebuild
    dispatch; the live ops route the grade / tmux / executor seams back through
    this module at call time (and render through a plain ``print`` sink), so the
    patch seams and the preview / json / confirm output are byte-for-byte
    unchanged. The terminal attach stays here — the use case returns the session
    to attach on the outcome and this wrapper performs the ``os.execvp`` process
    replacement, preserving the ``commands.os.execvp`` patch seam.
    """
    from mozyo_bridge.application.cockpit_reset_command import (
        CockpitResetUseCase,
        LiveCockpitResetOps,
    )

    outcome = CockpitResetUseCase(LiveCockpitResetOps()).handle(
        args, workspace, session, columns=columns, session_present=session_present,
        rebuild=rebuild, launch=launch, codex_ratio=codex_ratio,
    )
    if outcome.attach_session is not None:
        os.execvp("tmux", ["tmux", "-CC", "attach", "-t", outcome.attach_session])
        raise AssertionError("unreachable")
    return outcome.exit_code


def _handle_cockpit_doctor_geometry(session: str, *, json_output: bool) -> int:
    """`mozyo cockpit doctor-geometry` — read-only geometry drift diagnosis (#12131).

    Reads the live cockpit window panes and diagnoses display-geometry drift
    (missing role, role-less pane, split / mixed-Unit columns, width imbalance).
    Strictly read-only: it runs no tmux mutation and never repairs / rebalances /
    moves panes. Mirrors `doctor`'s exit convention — JSON (or text) is emitted
    regardless, and the exit code is ``0`` when no warning-level drift is found,
    ``1`` otherwise — so a script can branch on the code while still parsing the
    full diagnosis from stdout.
    """
    import json as _json

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
        diagnose_cockpit_geometry,
        format_geometry_text,
    )

    panes = _read_cockpit_geometry(session)
    diagnosis = diagnose_cockpit_geometry(session=session, panes=panes)
    if json_output:
        print(_json.dumps(diagnosis.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_geometry_text(diagnosis))
    return 0 if diagnosis.ok else 1


def _cockpit_unit_repo_root(session: str, *pane_ids: str) -> str:
    """The live checkout root of a cockpit Unit, from its pane cwd (#12341, read-only).

    A worktree / lane shares its workspace id with the main checkout, so the
    registry's single canonical path mislabels it (review j#62643). The live truth
    is the pane's working directory, walked up to the repo root — the same signal
    `agents targets` already uses. Reads the Unit's panes in order (codex first,
    then claude) and returns the first resolvable repo root, or ``""`` when no
    pane cwd is readable (the caller then falls back to the registry path).
    Tolerant: any tmux failure degrades to ``""``.
    """
    from mozyo_bridge.application.cockpit_membership_command import (
        LiveUnitRepoRootOps,
        UnitRepoRootUseCase,
    )

    # Thin wrapper over the #12976 membership boundary; the live adapter routes
    # ``commands._read_cockpit_pane_runtime`` at call time, so the tests patching
    # that seam still intercept the read.
    return UnitRepoRootUseCase(LiveUnitRepoRootOps()).resolve(session, *pane_ids)


def _cockpit_peer_adopt_use_case():
    """Build the #12978 :class:`CockpitPeerAdoptUseCase` over the live ops.

    The live adapter routes ``_read_cockpit_geometry`` / ``_read_cockpit_pane_runtime``
    / ``require_tmux`` / ``run_tmux`` / ``execute_peer_adopt_plan`` / ``die`` through
    this module at call time, so the characterization tests patching those seams keep
    intercepting and the boundary module never imports :mod:`commands` at module scope.
    """
    from mozyo_bridge.application.cockpit_peer_adopt_command import (
        CockpitPeerAdoptUseCase,
        LiveCockpitPeerAdoptOps,
    )

    return CockpitPeerAdoptUseCase(LiveCockpitPeerAdoptOps())


def _read_cockpit_pane_runtime(session: str, pane_id: str) -> dict:
    """Read one cockpit pane's cwd / foreground process / lane label (#12133, read-only).

    Thin wrapper over the #12978 boundary's :func:`read_pane_runtime` (routing this
    module's ``run_tmux`` at call time). The geometry reader
    (:func:`_read_cockpit_geometry`) deliberately does not read cwd / process (a
    privacy + scope choice); peer adopt needs them for its fail-closed preflight and
    to mirror the destination Unit's lane label. Also the shared read seam for
    :func:`_cockpit_unit_repo_root`. Tolerant: any tmux failure degrades to empties so
    the planner treats the facts as "unknown". Returns ``{cwd, process, lane_label}``.
    """
    from mozyo_bridge.application.cockpit_peer_adopt_command import read_pane_runtime

    return read_pane_runtime(run_tmux, pane_id)


def _resolve_peer_adopt_candidate(session: str, pane_id: str):
    """Resolve the role-less candidate pane's preflight facts (#12133).

    Thin wrapper over the #12978 :class:`CockpitPeerAdoptUseCase` resolver; the live
    adapter reads the runtime through ``commands._read_cockpit_pane_runtime`` and
    resolves the cwd through the registry → anchor → lane chain at call time.
    """
    return _cockpit_peer_adopt_use_case().resolve_candidate(session, pane_id)


def _resolve_peer_adopt_target(session: str, diagnosis, workspace_id: str, lane_id: str, role: str):
    """Build the destination :class:`PeerAdoptTarget`, mirroring its peer's metadata (#12133).

    Thin wrapper over the #12978 :class:`CockpitPeerAdoptUseCase` resolver; the peer's
    lane label is read through ``commands._read_cockpit_pane_runtime`` at call time.
    """
    return _cockpit_peer_adopt_use_case().resolve_target(
        session, diagnosis, workspace_id, lane_id, role
    )


def _handle_cockpit_peer_adopt(
    session: str, args: argparse.Namespace, *, json_output: bool, dry_run: bool
) -> int:
    """`mozyo cockpit peer-adopt` — bind a role-less pane as a Unit's missing peer (#12133).

    Thin wrapper over the #12978 :class:`CockpitPeerAdoptUseCase` boundary: it builds
    the live ops, runs the confirm-gated handler, and renders the returned
    :class:`PeerAdoptOutcome` (json payload or the pre-rendered text). The fail-closed
    guard order, the confirm-gated apply (reusing the #12972 ``execute_peer_adopt_plan``
    executor), and the CLI output + exit conventions are unchanged.
    """
    import json as _json

    outcome = _cockpit_peer_adopt_use_case().handle(
        session, args, json_output=json_output, dry_run=dry_run
    )
    if outcome.json_payload is not None:
        print(_json.dumps(outcome.json_payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif outcome.text:
        print(outcome.text)
    return outcome.exit_code


def _read_cockpit_window_layout(session: str):
    """Read the cockpit window's tmux ``window_layout`` string (#12135).

    The layout tree is the source of truth for which boundaries are resizable
    (unlike :func:`_read_cockpit_geometry`'s flat pane list, it shows the nested
    split structure). Returns the raw layout string, or ``None`` when the cockpit
    window does not exist or tmux cannot be read. Read-only and tolerant: a
    missing tmux binary / server / window degrades to ``None`` rather than
    raising, so the preview never mutates and never aborts.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import COCKPIT_WINDOW

    try:
        result = run_tmux(
            "display-message",
            "-p",
            "-t",
            f"{session}:{COCKPIT_WINDOW}",
            "-F",
            "#{window_layout}",
            check=False,
        )
    except (Exception, SystemExit):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    layout = (getattr(result, "stdout", "") or "").strip()
    return layout or None


def _cockpit_rebalance_columns(session: str):
    """``(present, columns)`` — top-level cockpit columns from the live layout tree.

    ``present`` is ``False`` when the cockpit window is absent (a benign no-op);
    ``columns`` is the :func:`top_level_columns` projection of the parsed
    ``window_layout`` otherwise.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        parse_window_layout,
        top_level_columns,
    )

    layout = _read_cockpit_window_layout(session)
    if layout is None:
        return False, ()
    root = parse_window_layout(layout)
    return True, top_level_columns(root)


def _handle_cockpit_rebalance(
    session: str, *, confirm: bool, json_output: bool, dry_run: bool
) -> int:
    """`mozyo cockpit rebalance` — preview/confirm equal fair-share width restore (#12135).

    Thin wrapper over the #13009 :class:`CockpitRebalanceUseCase` boundary; the
    live adapter routes the column read (``commands._cockpit_rebalance_columns``,
    which reads through the ``commands._read_cockpit_window_layout`` patch seam),
    the tmux gate, the #12135 executor, and ``commands.die`` through this module
    at call time, so the rebalance characterization tests keep intercepting. The
    preview-first / confirm-gated safety contract lives in the use case.
    """
    from mozyo_bridge.application.cockpit_rebalance_command import (
        CockpitRebalanceUseCase,
        LiveCockpitRebalanceOps,
    )

    return CockpitRebalanceUseCase(LiveCockpitRebalanceOps()).handle(
        session, confirm=confirm, json_output=json_output, dry_run=dry_run
    )


def _handle_cockpit_reconcile(
    session: str, *, confirm: bool, json_output: bool, dry_run: bool,
    codex_ratio: int = 70,
) -> int:
    """`mozyo cockpit reconcile` — preview/confirm structural layout-tree repair (#12136).

    Thin wrapper (Redmine #13008) over
    :meth:`~mozyo_bridge.application.cockpit_reconcile_command.CockpitReconcileUseCase.handle`.
    Kept here with the original signature for the ``cmd_cockpit`` reconcile
    dispatch; the live ops route the ``_read_cockpit_window_layout`` /
    ``_read_cockpit_geometry`` / ``require_tmux`` / executor seams back through
    this module at call time (and render through a plain ``print`` sink), so
    the patch seams and the preview / json / confirm output are byte-for-byte
    unchanged.
    """
    from mozyo_bridge.application.cockpit_reconcile_command import (
        CockpitReconcileUseCase,
        LiveCockpitReconcileOps,
    )

    return CockpitReconcileUseCase(LiveCockpitReconcileOps()).handle(
        session, confirm=confirm, json_output=json_output, dry_run=dry_run,
        codex_ratio=codex_ratio,
    )


def _cockpit_group_window_action(
    workspace, session, *, decision, codex_ratio, launch
):
    """Resolve the faithful per-Project-Group tmux-window action (#12330).

    Thin wrapper over the #12982 :class:`CockpitGroupWindowUseCase` boundary; the
    live adapter routes the #12330 multi-window discovery
    (``commands._read_managed_cockpit_windows``) and the rightmost-codex geometry
    pick (``commands._rightmost_codex_anchor``) through this module at call time,
    so the group-window characterization tests that patch
    ``commands._read_managed_cockpit_windows`` keep intercepting the read. Returns
    the same ``(action, plan, blocked_reason, window_name)`` tuple as before.
    """
    from mozyo_bridge.application.cockpit_group_window_command import (
        CockpitGroupWindowUseCase,
        LiveCockpitGroupWindowOps,
    )

    return CockpitGroupWindowUseCase(LiveCockpitGroupWindowOps()).resolve(
        workspace,
        session,
        decision=decision,
        codex_ratio=codex_ratio,
        launch=launch,
    )


def _handle_cockpit_restamp(args: argparse.Namespace) -> int:
    """`mozyo cockpit restamp` — re-derive drifted pane lane identity (#13160).

    Thin wrapper over
    :class:`~mozyo_bridge.application.cockpit_restamp_command.CockpitRestampUseCase`.
    Resolves the cockpit session + the target ``workspace_id`` (from ``--repo`` /
    cwd via :func:`resolve_canonical_session`), then re-derives the lane identity
    of that workspace's cockpit panes and re-applies ``set-option`` only where the
    stamp drifted. Kept as a module-level function so the ``commands`` seam is the
    live-ops boundary (``run_tmux`` / ``_resolve_workspace_lane`` patch points).
    ``--dry-run`` / ``--json`` preview without mutating.
    """
    from mozyo_bridge.application.cockpit_restamp_command import (
        CockpitRestampUseCase,
        LiveCockpitRestampOps,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        COCKPIT_SESSION_DEFAULT,
    )

    session = getattr(args, "cockpit_session", None) or COCKPIT_SESSION_DEFAULT
    repo = getattr(args, "repo", None) or getattr(args, "cwd", None) or os.getcwd()
    repo_root = str(Path(repo).expanduser().resolve())
    canon = resolve_canonical_session(repo_root)
    workspace_id = getattr(canon, "workspace_id", None) or canon.name
    return CockpitRestampUseCase(LiveCockpitRestampOps()).handle(
        session,
        workspace_id,
        json_output=bool(getattr(args, "json_output", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def cmd_cockpit(args: argparse.Namespace) -> int:
    """`mozyo cockpit` — append/focus the current workspace in the cockpit (#11803).

    Daily entry: `cd <project> && mozyo cockpit` adds the current workspace as a
    column to the shared `mozyo-cockpit` (creating it on first use), focuses it
    if already present, and never spawns a duplicate iTerm window for an
    existing cockpit. `mozyo --cc` is unchanged (#11729). `--dry-run` / `--json`
    are read-only and non-mutating: they read live cockpit state to report the
    action but run no tmux mutation and never abort on a stale cockpit.

    The sub-action routing and the create/append/focus launcher flow live in
    :class:`mozyo_bridge.application.cockpit_dispatcher_command.
    CockpitDispatchUseCase` (#13011); this stays a thin handler — the live
    adapters route the ``_handle_cockpit_*`` sub-action wrappers and the
    ``commands.*`` read/executor seams through this module at call time, and the
    fresh-create terminal attach comes back as
    ``CockpitDispatchOutcome.attach_session`` so the ``os.execvp`` process
    replacement (and its ``commands.os.execvp`` patch seam) stays here.

    The ``restamp`` sub-action (#13160) short-circuits before the dispatcher: it
    is a whole-cockpit lane-identity maintenance action that shares no code path
    with the create/append/focus flow (and keeps the 1000-line dispatcher module
    untouched).
    """
    if getattr(args, "action", None) == "restamp":
        return _handle_cockpit_restamp(args)

    from mozyo_bridge.application.cockpit_dispatcher_command import (
        CockpitDispatchUseCase,
        LiveCockpitLaunchFlowOps,
        LiveCockpitSubactionRoutes,
    )

    outcome = CockpitDispatchUseCase(
        LiveCockpitSubactionRoutes(), LiveCockpitLaunchFlowOps()
    ).run(args)
    if outcome.attach_session is None:
        return outcome.exit_code
    os.execvp("tmux", ["tmux", "-CC", "attach", "-t", outcome.attach_session])
    raise AssertionError("unreachable")


def session_cwd_mismatch(session: str, repo_root: Path) -> list[str]:
    """Thin adapter over the status/session helper boundary (#12974).

    The pane-snapshot read + the pure "every pane outside the repo" decision
    live in :class:`~mozyo_bridge.application.status_session_helper.
    SessionCwdMismatchUseCase`; this stays a module-level function so the
    ``commands.session_cwd_mismatch`` monkeypatch seam (and the ``LiveLaunchOps``
    caller that routes through it) is unchanged, and the live use case resolves
    ``commands.pane_lines`` at call time.
    """
    return SessionCwdMismatchUseCase(LiveStatusSessionHelperReads()).resolve(
        session, repo_root
    )


def legacy_basename_session_notice(repo_root: Path, derived_session: str) -> str | None:
    """Thin adapter over the status/session helper boundary (#12974).

    The advisory migration-notice decision + wording live in
    :class:`~mozyo_bridge.application.status_session_helper.
    LegacyBasenameNoticeUseCase`; this stays a module-level function so the
    ``LiveLaunchOps`` caller and the ``commands.session_exists`` /
    ``commands.session_cwd_mismatch`` seams the use case resolves at call time
    are unchanged. The notice is advisory only — it never blocks the
    bare-``mozyo`` flow.
    """
    return LegacyBasenameNoticeUseCase(LiveStatusSessionHelperReads()).resolve(
        repo_root, derived_session
    )


def resolve_status_session(args: argparse.Namespace) -> str:
    """Thin adapter over the status/session helper boundary (#12974).

    The explicit > current-tmux > canonical-name selection lives in
    :class:`~mozyo_bridge.application.status_session_helper.
    ResolveStatusSessionUseCase`; this stays a module-level function so the
    ``commands.resolve_status_session`` monkeypatch seam is unchanged and the
    live use case resolves ``commands.current_session_name`` /
    ``commands.repo_root_from_args`` / ``commands.resolve_canonical_session`` at
    call time.
    """
    return ResolveStatusSessionUseCase(LiveStatusSessionHelperReads()).resolve(args)


def cmd_status(args: argparse.Namespace) -> int:
    require_tmux()
    # OOP-first command boundary (Redmine #12831, atop #12830 / #12825 / #12785):
    # the handler composes the session-read use case (StatusSessionPort), the
    # cockpit-membership projection (StatusCockpitMembershipPort), the
    # doctor-tail continuation (StatusDoctorContinuation), and the #13355 herdr
    # backend port — all constructed by `live_status_handler` in
    # commands_status.py so this adapter stays a fixed-size composition root.
    # It prints the rendered StatusReport, then defers the exit code to the
    # doctor-tail continuation (live adapter routes to cmd_doctor at call time).
    handler = live_status_handler(args)
    report = handler.handle(StatusCommandRequest(session=resolve_status_session(args)))
    print(report.report_text, end="")
    return handler.continue_with_doctor().exit_code


def notify_agent(args: argparse.Namespace, agent: str) -> int:
    """Legacy-queue notify path (`notify-*-legacy-task`).

    The type-observe-marker-Enter TUI orchestration and its byte-for-byte
    behavior live in :class:`mozyo_bridge.application.notify_command.
    LegacyQueueNotifyUseCase` (#12931); this stays a thin adapter so importers
    (`test_mozyo_bridge`) and the ``commands.*`` monkeypatch seams the use case
    resolves at call time are unchanged.
    """
    from mozyo_bridge.application.notify_command import (
        LegacyQueueNotifyUseCase,
        LiveNotifyOps,
    )

    return LegacyQueueNotifyUseCase(LiveNotifyOps()).run(args, agent)


def _notify_command_use_case():
    """Build the notify command-entry use case over the live ``NotifyOps``.

    The six ``cmd_notify_*`` entry bodies (the per-subcommand receiver /
    default-kind / legacy-flag entry policy, plus the standard adapter formerly
    named ``_notify_standard_via_handoff``) live in
    :class:`mozyo_bridge.application.notify_command.NotifyCommandUseCase` (#12983);
    these stay thin, module-level wrappers so the public ``commands.cmd_notify_*``
    identity and the parser bindings are unchanged. Imported lazily so no import
    cycle is introduced (``notify_command`` resolves ``commands`` only at call
    time through :class:`LiveNotifyOps`).
    """
    from mozyo_bridge.application.notify_command import (
        LiveNotifyOps,
        NotifyCommandUseCase,
    )

    return NotifyCommandUseCase(LiveNotifyOps())


def cmd_notify_codex(args: argparse.Namespace) -> int:
    return _notify_command_use_case().run_codex(args)


def cmd_notify_claude(args: argparse.Namespace) -> int:
    return _notify_command_use_case().run_claude(args)


def cmd_notify_codex_review(args: argparse.Namespace) -> int:
    return _notify_command_use_case().run_codex_review(args)


def cmd_notify_claude_review_result(args: argparse.Namespace) -> int:
    return _notify_command_use_case().run_claude_review_result(args)


def cmd_notify_codex_legacy_task(args: argparse.Namespace) -> int:
    return _notify_command_use_case().run_codex_legacy_task(args)


def cmd_notify_claude_legacy_task(args: argparse.Namespace) -> int:
    return _notify_command_use_case().run_claude_legacy_task(args)


# The delivery-record output/persistence helper tail (``_emit_outcome`` /
# ``_submit_lines_for`` / ``_record_format_from_args`` /
# ``_record_command_from_args`` / ``_emit_receipt`` /
# ``_maybe_persist_delivery_record``) moved bodily into the
# ``handoff_delivery_command`` boundary (#13123) and is re-exported at the top of
# this module, so the ``orchestrate_handoff`` terminal paths below keep calling
# the historical names unchanged.


def _window_active_pane_id(target_info: dict) -> str | None:
    """Thin seam over :meth:`TargetActivationUseCase.window_active_pane_id` (#13124).

    The best-effort previously-active-pane observation (Redmine #12597) moved to
    ``application/handoff_target_activation_command.py``; the live adapter reads
    the ``pane_lines()`` snapshot through :mod:`pane_resolver` at call time, so
    the observation seam and the never-break-delivery degrade are unchanged.
    """
    return TargetActivationUseCase(LiveTargetActivationOps()).window_active_pane_id(
        target_info
    )


def _activate_target_pane(target_info: dict) -> TargetActivationOutcome:
    """Thin seam over :meth:`TargetActivationUseCase.activate_target_pane` (#13124).

    The standard_target_admission `tmux select-pane` activation (Redmine #12597
    — pane SELECTION only, never raw key injection) moved to
    ``application/handoff_target_activation_command.py``; the live adapter
    routes ``run_tmux`` through this module at call time. Kept module-level
    because ``orchestrate_handoff`` calls it on the deferred-activation path.
    """
    return TargetActivationUseCase(LiveTargetActivationOps()).activate_target_pane(
        target_info
    )


def _maybe_restore_previous_active(
    target_activation: TargetActivationOutcome | None,
    *,
    restore_previous_active: bool,
) -> TargetActivationOutcome | None:
    """Thin seam over :meth:`TargetActivationUseCase.maybe_restore_previous_active` (#13124).

    The post-delivery focus restore (Redmine #12597 — pane selection only,
    best-effort: a vanished pane must not break the already-completed send)
    moved to ``application/handoff_target_activation_command.py``. Kept
    module-level because ``orchestrate_handoff`` calls it on the sent terminal
    path.
    """
    return TargetActivationUseCase(
        LiveTargetActivationOps()
    ).maybe_restore_previous_active(
        target_activation, restore_previous_active=restore_previous_active
    )


# Redmine #13583 R2-F2 positive-delivery gate (rc 0 is NOT proof of delivery). The predicate +
# terminal-path publisher live in `f_130_.../application/delivery_outcome_gate.py`; re-exported.
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.delivery_outcome_gate import (  # noqa: E402,E501
    delivery_was_positive,
    make_publishing_emitter as _make_publishing_emitter,
    publish_delivery_outcome as _publish_delivery_outcome,
)


@bind_runtime_transport
def orchestrate_handoff(
    args: argparse.Namespace,
    *,
    default_kind: str | None = None,
    require_receiver_binding: bool = False,
    ticketless: bool = False,
    ticketless_consultation: bool = False,
    ticketless_work_intake: bool = False,
) -> int:
    """High-level handoff/reply primitive.

    Owns: receiver-pane resolution, agent-target validation, internal pane
    snapshot, marker-prefixed type, landing wait, fail-closed C-u rollback,
    and structured outcome emission.

    Durable delivery-record persistence is an explicit, opt-in seam (Redmine
    #12311): with ``--persist-delivery`` the typed terminal paths
    (``pending_input`` / ``sent``) hand the redacted record to a fail-closed
    ticket sink (:mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink`) via
    :func:`_maybe_persist_delivery_record`. It is best-effort and never alters
    the send. The live Redmine journal-write transport remains a credential-gated
    follow-up under per-task review (``redmine_context`` is read-only by design),
    so by default this primitive still performs no ticket-system API write:
    production resolves to a ``write_optin_unset`` receipt (Redmine #13262; the
    live-write env opt-in is unset) and ``source=asana`` to ``unsupported_source``.

    The standard path does its own pre-type capture so callers do not need to
    run ``mozyo-bridge read`` first.

    ``require_receiver_binding`` forces the explicit-``--target`` role-binding
    gate (``binds_receiver``) to run in **every** mode, not just under the
    relaxed ``queue-enter`` rail. The standard `handoff send` rail leaves
    ``standard`` / ``pending`` to the generic agent check (a marker_timeout
    C-u rollback covers a misaddressed standard send), but a wrapper whose
    contract is "this receiver and no other" (the cross-workspace consult
    primitive, Redmine #11779) must bind the target to the receiver before
    typing regardless of mode — otherwise `--mode standard` / `--mode pending`
    would let an explicit `%pane` for a foreign Claude pane be typed into under
    a ``to=codex`` marker, defeating the gateway boundary.
    """
    # Redmine #13729 (tranche 1): the Namespace ends here. Scalar inputs + entry
    # policy convert once into the typed `HandoffCommandInput`; `args` still
    # carries the mutated `target_repo` and threads to the not-yet-extracted
    # target/transport helpers (design j#78394 Tasks 3-4).
    inp = HandoffNamespaceAdapter.from_namespace(
        args,
        default_kind=default_kind,
        require_receiver_binding=require_receiver_binding,
        ticketless=ticketless,
        ticketless_consultation=ticketless_consultation,
        ticketless_work_intake=ticketless_work_intake,
    )
    # Redmine #13729 tranche 2: the Anchor/Profile Envelope Planner (design j#78394).
    envelope_planner = HandoffEnvelopePlanner()

    # Redmine #13261 (increment 2): resolve the backend once and gate every tmux-only
    # step on it. Under herdr a pure session has no tmux server, so `require_tmux()`
    # (and the tmux gates below) must not run; the target is resolved herdr-natively.
    # #13320 (a-narrow): an explicit `%pane` target still rides the tmux rail even under
    # herdr — the effective predicate (also read by `@bind_runtime_transport`) narrows.
    # R3-F1: every terminal outcome (incl. the herdr event rail) publishes via this emitter.
    # Redmine #13729: the emitter takes a facade-owned publish callback (not the
    # Namespace); the facade writes the delivery outcome onto its own `args` — its
    # caller wrappers read it back via `delivery_was_positive(args)` after this
    # returns — so the outcome hand-back stays byte-identical while the emitter and
    # every deep helper are Namespace-free.
    _emit = _make_publishing_emitter(
        lambda outcome: _publish_delivery_outcome(args, outcome), _emit_outcome
    )
    # Redmine #13729: the facade's single Namespace->Path boundary conversion. The
    # herdr backend predicate already resolves this repo root unconditionally, so
    # computing it once here (and passing it to the herdr helpers + the profile
    # block) is behaviour-neutral and keeps `repo_root_from_args` off the deep path.
    repo_root = repo_root_from_args(args)
    herdr_send = herdr_effective_backend_selected(repo_root=repo_root, target=inp.target)
    if not herdr_send:
        require_tmux()

    record_format = _record_format_from_value(inp.record_format)
    record_command = str(inp.record_command) if inp.record_command else None

    # Redmine #13729: `--target-repo auto` / herdr-auto resolution mutated
    # `args.target_repo` in place and later gates re-read it. Model that as an
    # explicit typed facade-local resolution scalar so no gate reads a mutated
    # Namespace attribute. Seeded from the initial parsed value.
    resolved_target_repo = inp.target_repo

    receiver = inp.to
    if receiver not in RECEIVERS:
        die(f"--to must be one of {sorted(RECEIVERS)}; got {receiver!r}")

    if inp.ticketless:
        # Redmine #12703: the ticketless no-anchor callback rail carries no Redmine
        # / Asana anchor, so it does not accept `--source` and is not in `SOURCES`.
        # The source token is fixed to `ticketless` for the marker / outcome. This
        # rail never reaches `normalize_anchor`, so the anchored send/reply rails'
        # anchor requirement is untouched.
        source = SOURCE_TICKETLESS
    else:
        source = inp.source
        if source not in SOURCES:
            die(f"--source must be one of {sorted(SOURCES)}; got {source!r}")

    kind = inp.kind or inp.default_kind
    mode = effective_send_mode(inp.mode)
    if mode not in MODES:
        die(f"--mode must be one of {sorted(MODES)}; got {mode!r}")

    send_gap = send_semantic_gap(
        mode=inp.mode, force=bool(inp.force), submit_delay=inp.submit_delay,
        submit_delay_consumed=not (herdr_send and mode == MODE_STANDARD),
    )
    if send_gap is not None:
        # Shared send-semantics authority: queue-enter refuses --force, and a
        # non-finite submit delay never reaches Enter. Message text comes from
        # the same authority so it cannot drift from the rule.
        _emit(
            make_outcome(
                status="blocked",
                reason="invalid_args",
                receiver=receiver,
                target=None,
                anchor=None,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        die(send_semantic_message(send_gap))

    if kind not in KIND_LABELS:
        _emit(
            make_outcome(
                status="blocked",
                reason="invalid_args",
                receiver=receiver,
                target=None,
                anchor=None,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        die(f"--kind must be one of {sorted(KIND_LABELS)}; got {kind!r}")

    summary = inp.summary

    # Redmine #13729 tranche 2: the Anchor/Profile Envelope Planner owns the typed
    # anchor + ticketless payload build. On malformed input it raises with no extra
    # outcome fields, matching the original early anchor block (target/anchor None).
    try:
        _anchor_plan = envelope_planner.plan_anchor(inp)
    except EnvelopePlanError as exc:
        _emit(
            make_outcome(
                status="blocked",
                reason=exc.reason,
                receiver=receiver,
                target=None,
                anchor=None,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        die(exc.message)
        raise AssertionError("unreachable")
    anchor = _anchor_plan.anchor
    ticketless_callback_payload = _anchor_plan.callback_payload
    ticketless_consultation_payload = _anchor_plan.consultation_payload
    ticketless_work_intake_payload = _anchor_plan.work_intake_payload
    # Redmine #13729 tranche 5: the handoff target-resolution preflight slice owns the herdr /
    # tmux target resolution, the `target_unavailable` `<session>:codex` gateway diagnostic, the
    # same-lane duplicate diagnostics, the `--target-repo auto` resolution, and the canonical
    # `project_preflight_target` projection. It is carved into the typed
    # ``handoff_target_resolution`` use case — the facade only assembles the typed request (the
    # resolved `repo_root` + `herdr_send` backend predicate + the raw target scalars + the
    # terminal-outcome context) and reads the resolved values back off the typed result, so no
    # downstream gate reads a mutated Namespace attribute. The emitted blocked outcomes, the
    # printed diagnostics, the re-raised tmux resolver ``SystemExit``, and every ``die`` message
    # are byte-identical to the original inline block.
    _target_resolution = run_target_resolution(
        TargetResolutionRequest(
            repo_root=repo_root,
            target=inp.target,
            target_repo=inp.target_repo,
            target_lane=inp.target_lane,
            receiver=receiver,
            anchor=anchor,
            mode=mode,
            kind=kind,
            source=source,
            record_format=record_format,
            record_command=record_command,
            resolved_target_repo=resolved_target_repo,
            herdr_send=herdr_send,
        ),
        emit=_emit,
    )
    target_info = _target_resolution.target_info
    target = _target_resolution.target
    duplicate_lane_panes = _target_resolution.duplicate_lane_panes
    resolved_target_repo = _target_resolution.resolved_target_repo
    preflight_target = _target_resolution.preflight_target

    # Redmine #13729 tranche 6: the handoff admission-pipeline slice owns the fixed-order sequence
    # of die-able admission gates over the resolved target — the main-lane implementation guard, the
    # receiver / session / cross-workspace `--to claude` binding gates, the gateway-route
    # enforcement, the `--target-repo` / `--target-project` identity gates, the
    # standard_target_admission inactive-split plan, and the foreground-agent binding. It is carved
    # into the typed ``handoff_admission_pipeline`` use case: the facade only assembles the typed
    # request (the resolved target / preflight projection from the tranche-5 slice + the backend
    # predicate + the terminal-outcome context + the raw entry scalars each gate reads) and reads
    # the two values that cross the boundary — the resolved admission policy and the deferred
    # inactive-split activation plan — back off the typed result. The gate evaluation order, the
    # emitted blocked outcomes (reason / extras / recovery_command), the printed diagnostics, the
    # re-raised agent-gate ``SystemExit``, and every ``die`` message are byte-identical to the
    # original inline block. The inactive split is NOT actuated here: the real ``select-pane`` stays
    # deferred to the facade below, after every die-able gate AND the startup-admission gate pass.
    _admission = run_admission_pipeline(
        AdmissionPipelineRequest(
            receiver=receiver,
            kind=kind,
            mode=mode,
            source=source,
            anchor=anchor,
            target=target,
            target_info=target_info,
            preflight_target=preflight_target,
            herdr_send=herdr_send,
            resolved_target_repo=resolved_target_repo,
            record_format=record_format,
            record_command=record_command,
            raw_target=inp.target,
            require_receiver_binding=bool(inp.require_receiver_binding),
            has_main_lane_exception=bool(inp.main_lane_exception),
            allow_direct_worker=bool(inp.allow_direct_worker),
            target_project=inp.target_project,
            no_target_activation=bool(inp.no_target_activation),
            restore_previous_active=bool(inp.restore_previous_active),
            force=bool(inp.force),
        ),
        emit=_emit,
    )
    admission_policy = _admission.admission_policy
    activate_inactive_target = _admission.activate_inactive_target
    # Redmine #12597: the inactive-split activation is actuated below (after startup admission), so
    # the facade owns the `TargetActivationOutcome` local the transport rail threads onto the record.
    target_activation: TargetActivationOutcome | None = None

    # Redmine #13729 tranche 2: the Anchor/Profile Envelope Planner resolves the
    # pre-send envelope (execution root / role profile / transition role / workflow
    # contract / notification body / landing marker). On malformed input it raises with
    # the exact cumulative partial-state extras each original stage emitted; the facade
    # merges them with its base context for a byte-identical blocked outcome + die.
    try:
        envelope = envelope_planner.plan_delivery_envelope(
            inp,
            anchor=anchor,
            callback_payload=ticketless_callback_payload,
            consultation_payload=ticketless_consultation_payload,
            work_intake_payload=ticketless_work_intake_payload,
            repo_root=repo_root,
            resolved_target_repo=resolved_target_repo,
            target_cwd=target_info.get("cwd") or "",
            summary=summary,
            receiver=receiver,
            kind=kind,
        )
    except EnvelopePlanError as exc:
        _emit(
            make_outcome(
                status="blocked",
                reason=exc.reason,
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
                **exc.outcome_extra,
            ),
            record_format=record_format,
            command=record_command,
            **exc.emit_extra,
        )
        die(exc.message)
        raise AssertionError("unreachable")
    execution_root = envelope.execution_root
    role_profile_resolution = envelope.role_profile_resolution
    role_profile_contract = envelope.role_profile_contract
    transition_role_boundary = envelope.transition_role_boundary
    workflow_contract_bundle = envelope.workflow_contract_bundle
    body = envelope.body
    marker = envelope.marker

    read_lines = int(inp.read_lines or 50)
    # Internal pane snapshot preflight (the standard path must not require callers to run
    # `mozyo-bridge read` first) AND — under herdr — the Redmine #13760 pre-send startup
    # admission: the same single action-time read is classified against the receiver
    # provider's declared startup screens, and a trust / first-run / login screen refuses
    # the send with ZERO bytes typed. The gate body lives in the f_130 seam (module-health;
    # and it keeps every provider-specific string in profile DATA, out of this module).
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_gate import (
        admit_receiver_startup_or_die,
    )

    admit_receiver_startup_or_die(
        herdr_send=herdr_send,
        receiver=receiver,
        target=target,
        read_lines=read_lines,
        capture_pane=capture_pane,
        emit=_emit,
        record_format=record_format,
        record_command=record_command,
        anchor=anchor,
        mode=mode,
        kind=kind,
        source=source,
        execution_root=execution_root,
        role_profile_contract=role_profile_contract,
        duplicate_lane_panes=duplicate_lane_panes,
        ledger=_record_herdr_send_ledger,
    )

    # Redmine #12597: activate an admitted inactive split now — after every
    # die-able gate above — so we never steal the operator's focus for a send
    # that then fails. Pane selection only; no raw key injection.
    if activate_inactive_target:
        target_activation = _activate_target_pane(target_info)

    # Redmine #13255: under `terminal_transport.backend: herdr` AND `--mode standard`,
    # the strict rail is driven by the event-driven `HerdrTurnStartRail` INSTEAD OF
    # the common tmux body injection + landing-marker gate + capture-based
    # `_observe_standard_turn_start` + tmux Enter below. The rail OWNS injection
    # (snapshot → arm wait → send_text(marker+body) → send_keys(enter) → collect
    # through the herdr transport port), so this branch runs BEFORE the common
    # `run_tmux("send-keys", ... marker+body)` injection and returns / dies without
    # falling through. `precondition_not_idle` (receiver not idle at the pre-snapshot)
    # refuses to inject at all (no body, no Enter) and fails closed. tmux stays
    # byte-identical, and herdr non-standard sends (pending / queue-enter) keep the
    # common shim-backed choreography (auditor j#72602 decisions 1/2/3/5). The rail is
    # stashed on `active_herdr_turn_start_rail` by `bind_runtime_transport`.
    if herdr_send and mode == MODE_STANDARD:
        # Redmine #13729 tranche 3: the herdr event-driven turn-start rail slice owns its own
        # control flow (returns / dies without falling through). It is carved into the typed
        # ``handoff_herdr_standard_rail`` use case — the facade only assembles the typed request
        # (the resolved envelope value objects + terminal outcome context) and hands it the
        # stashed rail + the per-call publishing emitter. The emit / ledger / persist / die side
        # effects, the marker+body-once-only choreography, and both die messages are unchanged.
        return run_herdr_standard_rail(
            active_herdr_turn_start_rail,
            HerdrStandardRailRequest(
                target=target,
                marker=marker,
                body=body,
                receiver=receiver,
                anchor=anchor,
                mode=mode,
                kind=kind,
                execution_root=execution_root,
                role_profile_resolution=role_profile_resolution,
                role_profile_contract=role_profile_contract,
                transition_role_boundary=transition_role_boundary,
                workflow_contract_bundle=workflow_contract_bundle,
                ticketless_callback=ticketless_callback_payload,
                ticketless_consultation=ticketless_consultation_payload,
                ticketless_work_intake=ticketless_work_intake_payload,
                record_format=record_format,
                record_command=record_command,
                duplicate_lane_panes=duplicate_lane_panes,
                submit_intent=inp.submit_intent,
                submit_delivery_id=inp.submit_delivery_id,
                persist_delivery=bool(inp.persist_delivery),
            ),
            emit=_emit,
        )

    # Redmine #13729 tranche 4: the common tmux transport rail slice owns its own control flow
    # (every path returns / dies without falling through). It is carved into the typed
    # ``handoff_tmux_transport_rail`` use case — the facade only assembles the typed request (the
    # resolved envelope value objects + terminal outcome context + the raw landing / submit /
    # retry scalars + the pre-resolved focus-restore activation) and hands it the per-call
    # publishing emitter. The inject / marker gate / C-u rollback / Enter-only retry / standard
    # turn-start confirmation / final sent assembly choreography, the emit / persist / ledger /
    # restore side effects, and both ``die`` messages are unchanged.
    return run_tmux_transport_rail(
        TmuxTransportRailRequest(
            target=target,
            marker=marker,
            body=body,
            receiver=receiver,
            anchor=anchor,
            mode=mode,
            kind=kind,
            execution_root=execution_root,
            role_profile_resolution=role_profile_resolution,
            role_profile_contract=role_profile_contract,
            transition_role_boundary=transition_role_boundary,
            workflow_contract_bundle=workflow_contract_bundle,
            ticketless_callback=ticketless_callback_payload,
            ticketless_consultation=ticketless_consultation_payload,
            ticketless_work_intake=ticketless_work_intake_payload,
            record_format=record_format,
            record_command=record_command,
            duplicate_lane_panes=duplicate_lane_panes,
            submit_intent=inp.submit_intent,
            submit_delivery_id=inp.submit_delivery_id,
            persist_delivery=bool(inp.persist_delivery),
            herdr_send=herdr_send,
            read_lines=read_lines,
            landing_timeout=inp.landing_timeout,
            submit_delay=inp.submit_delay,
            queue_enter_retry_window=inp.queue_enter_retry_window,
            queue_enter_retry_interval=inp.queue_enter_retry_interval,
            target_activation=target_activation,
            restore_previous_active=admission_policy.restore_previous_active,
        ),
        emit=_emit,
    )


# ``CONSULT_DEFAULT_KIND`` and the four ``handoff`` command entry bodies moved to
# the OOP-first ``application/handoff_command.py`` boundary (Redmine #12936). The
# constant is re-exported here so existing ``commands.CONSULT_DEFAULT_KIND``
# references stay valid; the wrappers below stay thin so their importers (``cli`` /
# ``cli_handoff`` bind the ``cmd_handoff_*`` entry points; ``test_handoff_orchestrator``
# compares ``cmd_handoff_reply`` by identity) and the ``commands.*`` monkeypatch
# seams the live adapter resolves at call time are unchanged.
from mozyo_bridge.application.handoff_command import (  # noqa: E402
    CONSULT_DEFAULT_KIND,
)


def cmd_handoff_send(args: argparse.Namespace) -> int:
    from mozyo_bridge.application.handoff_command import (
        HandoffCommandUseCase,
        LiveHandoffCommandOps,
    )

    return HandoffCommandUseCase(LiveHandoffCommandOps()).run_send(args)


def cmd_handoff_reply(args: argparse.Namespace) -> int:
    from mozyo_bridge.application.handoff_command import (
        HandoffCommandUseCase,
        LiveHandoffCommandOps,
    )

    return HandoffCommandUseCase(LiveHandoffCommandOps()).run_reply(args)


def cmd_handoff_ticketless_callback(args: argparse.Namespace) -> int:
    """Thin adapter: the body lives in ``HandoffCommandUseCase.run_ticketless_callback``.

    Redmine #13583 R1-F1: on a positively-delivered callback that echoes a ``forward_action_id``,
    complete the correlated forward generation (best-effort; never alters the callback's own rc).
    """
    from mozyo_bridge.application.handoff_command import (
        HandoffCommandUseCase,
        LiveHandoffCommandOps,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (
        complete_forward_generation_on_callback,
    )

    rc = HandoffCommandUseCase(LiveHandoffCommandOps()).run_ticketless_callback(args)
    # R2-F2: gate on the transport's structured `sent`/`ok`, never on rc (see delivery_outcome_gate).
    complete_forward_generation_on_callback(args, delivered=delivery_was_positive(args))
    return rc


def cmd_handoff_cross_workspace_consult(args: argparse.Namespace) -> int:
    """Thin adapter: the body lives in ``HandoffCommandUseCase.run_cross_workspace_consult``."""
    from mozyo_bridge.application.handoff_command import (
        HandoffCommandUseCase,
        LiveHandoffCommandOps,
    )

    return HandoffCommandUseCase(
        LiveHandoffCommandOps()
    ).run_cross_workspace_consult(args)


def cmd_init(args: argparse.Namespace) -> int:
    """Adopt the current/target pane into its workspace as a ``claude`` / ``codex`` agent.

    Smart default (Redmine #11367): resolve the workspace root from the pane's
    cwd, derive the expected mozyo session name, pin it into the workspace's
    ``.vscode/settings.json``, rename a low-information tmux-integrated fallback
    session (e.g. ``___________``) into the derived session, rename the window to
    the agent name, and apply the agent window style.

    The smart path fails closed when adoption is not provably safe: an
    unidentifiable workspace root, a meaningful (non-fallback) current session
    name that differs from the expected one, an expected session that already
    exists as a separate tmux session, or a same-session window already named
    ``claude`` / ``codex``. All such checks run before any mutation so a guard
    stop never leaves a half-adopted pane.

    ``--window-only`` keeps the legacy low-level behavior: rename only the
    current/target window, with no session rename and no ``.vscode`` write — for
    manual / debug workflows and meaningful-foreign sessions.
    ``--no-vscode-settings`` runs the smart session/window adoption but skips the
    settings write.

    The adoption policy and side effects live in
    :mod:`mozyo_bridge.application.init_command` (#12926); this handler stays thin
    — it builds the request, runs the use case, and renders the outcome.
    """
    from mozyo_bridge.application.init_command import (
        InitRequest,
        InitUseCase,
        LiveInitWorkspaceOps,
    )

    outcome = InitUseCase(LiveInitWorkspaceOps()).run(InitRequest.from_args(args))
    if outcome.refused_message is not None:
        die(outcome.refused_message)
    for warning in outcome.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(outcome.success_line)
    for note in outcome.notes:
        print(f"  - {note}")
    return 0


# Compatibility re-export (#12979, #13103): the workspace/session config helper
# tail (`_confident_workspace_root` / `_is_fallback_session_name` /
# `_agent_window_conflict` / `_write_vscode_session_name`) and the pane-option
# marker stamping (`_bind_agent_pane_markers`) moved into the `init_command`
# boundary that consumes them. Re-export the legacy names so existing imports
# (`commands._confident_workspace_root` /
# `commands._is_fallback_session_name` are exercised directly by tests) keep
# resolving byte-for-byte. `init_command` imports `commands` only lazily, so this
# top-level import introduces no cycle.
from mozyo_bridge.application.init_command import (  # noqa: E402
    _agent_window_conflict,
    _bind_agent_pane_markers,
    _confident_workspace_root,
    _is_fallback_session_name,
    _write_vscode_session_name,
)

# Compatibility re-export (#12982): the faithful per-Project-Group tmux-window
# action vocabulary + routing moved into the `cockpit_group_window_command`
# boundary. Re-export the `GROUP_ACTION_*` / `GROUP_ACTIONS` names so the
# executor dispatch in `cmd_cockpit` and existing imports
# (`commands.GROUP_ACTION_FOCUS` are exercised directly by tests) keep resolving
# to one source of truth. `cockpit_group_window_command` imports `commands` only
# lazily (in its live adapter), so this top-level import introduces no cycle.
from mozyo_bridge.application.cockpit_group_window_command import (  # noqa: E402,F401
    GROUP_ACTION_APPEND,
    GROUP_ACTION_CREATE,
    GROUP_ACTION_FOCUS,
    GROUP_ACTIONS,
)


# Compatibility re-export (#13104): the doctor/instruction thin command
# adapters moved into their bounded command modules (``cmd_doctor`` is
# re-exported with ``DoctorCommandUseCase`` in the line-1 import block above).
# Re-export the ``runtime-config`` family adapters so the parser bindings
# (``cli.py`` / ``cli_runtime_config.py`` import them from here) and existing
# ``commands.cmd_*`` imports keep resolving. Both modules import their
# diagnostic/install runners only lazily inside the adapter bodies, so these
# top-level imports introduce no cycle.
from mozyo_bridge.application.doctor_instruction_command import (  # noqa: E402,F401
    cmd_doctor_instruction,
    cmd_instruction_doctor,
)
from mozyo_bridge.application.instruction_install_command import (  # noqa: E402,F401
    cmd_instruction_install,
)

# Compatibility re-export (#13122): the cockpit membership/status read thin
# adapters moved into the `cockpit_membership_command` boundary they were
# wrapping (#12976). Re-export the legacy names so the existing seams keep
# resolving: the `cockpit_dispatcher_command` routes
# `commands._handle_cockpit_list` / `._handle_cockpit_status` and the status
# integration routes `commands._collect_cockpit_membership` /
# `._resolve_registry_facts` at call time, and the membership characterization
# tests patch those names on this module. `cockpit_membership_command` imports
# `commands` only lazily (in its live adapter / repo-root routing), so this
# top-level import introduces no cycle.
from mozyo_bridge.application.cockpit_membership_command import (  # noqa: E402,F401
    _collect_cockpit_membership,
    _handle_cockpit_list,
    _handle_cockpit_status,
    _membership_observations_from_windows,
    _resolve_registry_facts,
)

# Compatibility re-export (#13120): the agent launch helper tail
# (`_claude_permission_mode_flag` / `_agent_launch_command` /
# `_record_managed_pane_created`) moved into the `launch_command` boundary
# that consumes it. Re-export the legacy names so existing imports and
# monkeypatch targets (`commands._agent_launch_command` is patched by the
# cockpit layout tests and imported directly by the permission-policy / otel
# characterization tests) keep resolving to one source of truth. The moved
# bodies resolve their side-effect seams (`die` / `otel_bootstrap_env` /
# `resolve_canonical_session`) through this module only lazily at call time,
# so this top-level import introduces no cycle.
from mozyo_bridge.application.launch_command import (  # noqa: E402,F401
    _agent_launch_command,
    _claude_permission_mode_flag,
    _record_managed_pane_created,
)


# --- Compatibility facade: handlers split into family modules (#12142, #12154). ---
# Re-export so existing imports and monkeypatch targets
# (`mozyo_bridge.application.commands.cmd_*`) keep resolving.
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.commands_otel import (  # noqa: E402
    _events_store,
    _render_timeline,
    cmd_events_query,
    cmd_events_tail,
    cmd_otel_activity,
    cmd_otel_events,
    cmd_otel_launchd,
    cmd_otel_serve,
    cmd_otel_status,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.commands_runtime_observation import (  # noqa: E402
    cmd_observe_reload,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.commands_docs_scaffold import (  # noqa: E402
    cmd_docs_audit_impact,
    cmd_docs_generate,
    cmd_docs_resolve,
    cmd_docs_validate,
    cmd_rules_home,
    cmd_rules_install,
    cmd_rules_status,
    cmd_scaffold_apply,
    cmd_scaffold_canonical,
    cmd_scaffold_diff,
    cmd_scaffold_status,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.commands_workspace import (  # noqa: E402
    cmd_workspace_defaults,
)
