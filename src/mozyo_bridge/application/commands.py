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
from mozyo_bridge.application.status_session_port import LiveStatusSession
from mozyo_bridge.application.status_session_helper import (
    LegacyBasenameNoticeUseCase,
    LiveStatusSessionHelperReads,
    ResolveStatusSessionUseCase,
    SessionCwdMismatchUseCase,
    cwd_is_under_repo,
)
from mozyo_bridge.application.commands_status import (
    LiveStatusCockpitMembership,
    LiveStatusDoctorContinuation,
    StatusCommandHandler,
    StatusCommandRequest,
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
    AUTO_TARGET_REPO,
    AnchorError,
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
    TicketlessAnchor,
    TicketlessConsultationAnchor,
    TicketlessWorkIntakeAnchor,
    build_execution_root,
    build_inactive_pane_fallback_command,
    build_marker,
    build_notification_body,
    evaluate_standard_target_admission,
    is_explicit_pane_target,
    make_outcome,
    normalize_anchor,
    resolve_queue_enter_retry_policy,
    resolve_standard_target_admission_policy,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileError,
    parse_profile_fields,
    resolve_role_profile,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    TransitionRoleError,
    resolve_transition_role,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
    WorkflowContractError,
    resolve_workflow_contract,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    TicketlessCallback,
    TicketlessCallbackError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    TicketlessConsultation,
    TicketlessConsultationError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
    TicketlessWorkIntake,
    TicketlessWorkIntakeError,
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
    is_receiver_agent_process,
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
    HerdrSendEntryError,
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
    normalize_path_unicode,
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

    Thin wrapper: the discovery orchestration was extracted to
    :class:`mozyo_bridge.application.commands_agents.ResolveAgentTargetsUseCase`,
    which depends on the injected
    :class:`mozyo_bridge.application.agent_discovery_port.AgentDiscoveryPort`
    instead of the four naked external reads (live discovery / canonical session /
    git checkout probe / project scope) — the OOP-first read-discovery tranche
    (Redmine #12749 / #12638 / #12785). This wrapper keeps the public name so the
    ``agents targets`` / attention handlers and the delegated-coordinator /
    project-gateway callers that ``from ...commands import _agents_target_candidates``
    (and the tests that patch ``commands._agents_target_candidates``) are unchanged.
    Behavior-preserving.
    """
    return ResolveAgentTargetsUseCase(LiveAgentDiscovery()).resolve(
        agent_filter=getattr(args, "agent", None),
        session_filter=getattr(args, "session", None),
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

    Resolves the repo root, resolves the session name via
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
    # cockpit-membership projection (StatusCockpitMembershipPort), and the
    # doctor-tail continuation (StatusDoctorContinuation) — all in
    # commands_status.py. This thin adapter prints the rendered StatusReport, then
    # defers the exit code to the continuation. #12831 isolated the
    # `cmd_doctor(args)` tail behind that typed port + StatusContinuationResult VO
    # (live adapter routes to cmd_doctor at call time; broad doctor body = #12638).
    handler = StatusCommandHandler(
        sessions=LiveStatusSession(),
        membership=LiveStatusCockpitMembership(args),
        continuation=LiveStatusDoctorContinuation(args),
    )
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
    # Redmine #13261 (increment 2): resolve the backend once and gate every tmux-only
    # step on it. Under herdr a pure session has no tmux server, so `require_tmux()`
    # (and the tmux gates below) must not run; the target is resolved herdr-natively.
    # #13320 (a-narrow): an explicit `%pane` target still rides the tmux rail even under
    # herdr — the effective predicate (also read by `@bind_runtime_transport`) narrows.
    herdr_send = herdr_effective_backend_selected(args)
    if not herdr_send:
        require_tmux()

    record_format = _record_format_from_args(args)
    record_command = _record_command_from_args(args)

    receiver = getattr(args, "to", None)
    if receiver not in RECEIVERS:
        die(f"--to must be one of {sorted(RECEIVERS)}; got {receiver!r}")

    if ticketless:
        # Redmine #12703: the ticketless no-anchor callback rail carries no Redmine
        # / Asana anchor, so it does not accept `--source` and is not in `SOURCES`.
        # The source token is fixed to `ticketless` for the marker / outcome. This
        # rail never reaches `normalize_anchor`, so the anchored send/reply rails'
        # anchor requirement is untouched.
        source = SOURCE_TICKETLESS
    else:
        source = getattr(args, "source", None)
        if source not in SOURCES:
            die(f"--source must be one of {sorted(SOURCES)}; got {source!r}")

    kind = getattr(args, "kind", None) or default_kind
    mode = getattr(args, "mode", MODE_QUEUE_ENTER) or MODE_QUEUE_ENTER
    if mode not in MODES:
        die(f"--mode must be one of {sorted(MODES)}; got {mode!r}")

    if mode == MODE_QUEUE_ENTER and bool(getattr(args, "force", False)):
        # Per the relaxed queue-enter rail contract, the agent gate must be
        # stricter than strict `standard`: `--force` cannot be used to bypass
        # non-agent target checks under this rail. The rail only makes sense
        # for Claude/Codex agent panes whose prompt queue accepts Enter.
        _emit_outcome(
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
        die(
            "--force is not allowed under --mode queue-enter; queue-enter is "
            "restricted to Claude/Codex agent panes and rejects non-agent "
            "targets even with operator override."
        )

    if kind not in KIND_LABELS:
        _emit_outcome(
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

    summary = getattr(args, "summary", None)

    # Redmine #12703: the structured ticketless callback result (return leg), built
    # distinctly from the transport outcome. `None` for every anchored send/reply.
    ticketless_callback_payload: TicketlessCallback | None = None
    # Redmine #12740: the structured forward ticketless consultation (forward leg),
    # built distinctly from the transport outcome and from the return-leg callback
    # above. `None` unless `ticketless_consultation=True`.
    ticketless_consultation_payload: TicketlessConsultation | None = None
    # Redmine #12748: the structured forward ticketless work-intake (parent -> child
    # forward leg), built distinctly from the transport outcome and from the
    # grandparent->parent consultation / return callback above. `None` unless
    # `ticketless_work_intake=True`.
    ticketless_work_intake_payload: TicketlessWorkIntake | None = None

    if ticketless and ticketless_work_intake:
        # Redmine #12748: build the structured FORWARD work-intake payload, then
        # derive the no-anchor `TicketlessWorkIntakeAnchor` from it. Construction
        # fails closed (blocked / invalid_args) on an unknown token or an empty /
        # unknown callback-method set. No anchor is fabricated (the child owns the
        # anchor create/select/blocked decision) and the worker-dispatch anchor gate
        # is not relaxed (a worker dispatch is not expressible on this work-intake
        # rail; the payload restates the invariants for the child).
        try:
            ticketless_work_intake_payload = TicketlessWorkIntake(
                work_shape=getattr(args, "work_shape", None),
                callback_to_role=getattr(args, "callback_to_role", None),
                callback_methods=getattr(args, "callback_methods", None),
                read_contract=getattr(args, "read_contract", None),
            )
        except TicketlessWorkIntakeError as exc:
            _emit_outcome(
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
            die(str(exc))
            raise AssertionError("unreachable")
        anchor = TicketlessWorkIntakeAnchor(
            work_shape=ticketless_work_intake_payload.work_shape,
            callback_to_role=ticketless_work_intake_payload.callback_to_role,
        )
    elif ticketless and ticketless_consultation:
        # Redmine #12740: build the structured FORWARD consultation payload, then
        # derive the no-anchor `TicketlessConsultationAnchor` from it. Construction
        # fails closed (blocked / invalid_args) on an unknown token or an empty /
        # unknown callback-method set. No anchor is fabricated and the worker-dispatch
        # anchor gate is not relaxed (a worker dispatch is not expressible on this
        # consultation rail; the payload restates the invariant for the receiver).
        try:
            ticketless_consultation_payload = TicketlessConsultation(
                consultation_kind=getattr(args, "consultation_kind", None),
                callback_to_role=getattr(args, "callback_to_role", None),
                callback_methods=getattr(args, "callback_methods", None),
                read_contract=getattr(args, "read_contract", None),
            )
        except TicketlessConsultationError as exc:
            _emit_outcome(
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
            die(str(exc))
            raise AssertionError("unreachable")
        anchor = TicketlessConsultationAnchor(
            consultation_kind=ticketless_consultation_payload.consultation_kind,
            callback_to_role=ticketless_consultation_payload.callback_to_role,
        )
    elif ticketless:
        # Build the structured callback result, then derive the no-anchor
        # `TicketlessAnchor` from it. Construction fails closed (blocked /
        # invalid_args) on an unknown token or — critically — on a dispatch
        # decision that is an actual worker dispatch (which still requires a real
        # Redmine anchor; the child -> grandchild boundary is not relaxed).
        try:
            ticketless_callback_payload = TicketlessCallback(
                classification=getattr(args, "classification", None),
                dispatch_decision=getattr(args, "dispatch_decision", None),
                next_action_owner=getattr(args, "workflow_next_owner", None),
                callback_reason=getattr(args, "callback_reason", None),
                read_contract=getattr(args, "read_contract", None),
            )
        except TicketlessCallbackError as exc:
            _emit_outcome(
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
            die(str(exc))
            raise AssertionError("unreachable")
        anchor = TicketlessAnchor(
            classification=ticketless_callback_payload.classification,
            dispatch_decision=ticketless_callback_payload.dispatch_decision,
        )
    else:
        try:
            anchor = normalize_anchor(
                source,
                task_id=getattr(args, "task_id", None),
                comment_id=getattr(args, "comment_id", None),
                anchor_url=getattr(args, "anchor_url", None),
                issue=getattr(args, "issue", None),
                journal=getattr(args, "journal", None),
            )
        except AnchorError as exc:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_anchor",
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
            die(str(exc))
            raise AssertionError("unreachable")

    if herdr_send:
        # Redmine #13261 (increment 2): pure-herdr target resolution. There is no
        # tmux pane to read, so resolve the receiver against the live herdr inventory
        # scoped by the launch-time sender identity (env + anchor) and synthesize a
        # `project_preflight_target`-compatible pane record whose `id` is the live
        # herdr locator. Fail-closed (un-attested sender / unavailable inventory /
        # no single live agent) emits a `target_unavailable` blocked outcome and dies
        # — never a silent tmux fallback.
        try:
            target_info = resolve_herdr_send_target(args, receiver=receiver)
        except HerdrSendEntryError as exc:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_unavailable",
                    receiver=receiver,
                    target=None,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(str(exc))
            raise AssertionError("unreachable")
    else:
        target_arg = getattr(args, "target", None) or receiver
        try:
            target_info = pane_info(target_arg)
        except SystemExit:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_unavailable",
                    receiver=receiver,
                    target=None,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            # Diagnostics only (Redmine #11776): when a `<session>:codex` gateway
            # location fails to resolve, distinguish exact tmux window-name
            # resolution from inventory agent_kind classification and list the
            # session's Codex-like candidate panes. Best-effort and additive — the
            # original resolver failure (already printed) and the blocked outcome
            # are unchanged.
            try:
                if (
                    ":" in target_arg
                    and not target_arg.startswith("%")
                    and target_arg.split(":", 1)[1].split(".", 1)[0] == "codex"
                ):
                    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr
                    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
                        codex_gateway_candidates,
                    )
                    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
                        target_unavailable_codex_diagnostic,
                    )

                    _sess = target_arg.split(":", 1)[0]
                    _cands = [
                        rec.to_dict()
                        for rec in codex_gateway_candidates(_sess, _pr.pane_lines())
                    ]
                    _diag = target_unavailable_codex_diagnostic(
                        _sess, "codex", _cands
                    )
                    print(_diag, file=sys.stderr)
            except (Exception, SystemExit):
                # Diagnostics are strictly best-effort. `pane_lines()` calls
                # `die()` (SystemExit) when tmux is absent, so catch SystemExit too
                # — a diagnostics failure must never replace the original
                # `target_unavailable` outcome (Redmine #11778).
                pass
            raise

    target = target_info["id"]

    # Redmine #12229: surface duplicate same-lane receiver panes in the durable
    # record so the receiver pane and any stale-input duplicate stay both
    # visible and the receiver/actor record cannot silently diverge (a cockpit
    # gateway repair can leave two same-lane Claude panes, #12226 j#61213). This
    # reads a LIVE tmux snapshot at action time
    # (`vibes/docs/logics/runtime-observability-boundary.md`), never a stored
    # projection. It is strictly diagnostic and best-effort: it never blocks the
    # send and never replaces an outcome (an explicit `--target %pane` is the
    # documented escape hatch, and queue-enter's Step 11 active-split gate
    # already fail-closes the inactive duplicate). A snapshot read failure must
    # not change delivery, so swallow any error.
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr

    duplicate_lane_panes: list[str] = []
    if not herdr_send:
        # Redmine #13261: this same-lane-duplicate diagnostic reads a LIVE tmux pane
        # snapshot, which has no meaning in a pure herdr session — so it is an
        # explicit no-op under the herdr backend (an empty list), not a swallowed
        # tmux-absence error. herdr identity uniqueness is enforced upstream by the
        # assigned-name decode (a duplicate assigned name fails closed).
        try:
            duplicate_lane_panes = [
                _pr.duplicate_pane_record_row(pane)
                for pane in _pr.same_lane_receiver_duplicates(
                    target_info, _pr.pane_lines(), receiver
                )
            ]
        except (Exception, SystemExit):
            duplicate_lane_panes = []

    # `--target-repo auto` (Redmine #11778): resolve the cross-workspace
    # identity gate from the explicitly-named pane's own cwd so the operator
    # does not hand-run `tmux display-message -p -t %pane '#{pane_current_path}'`
    # before a safe gateway send. Strictly limited to an explicit `%pane`
    # target — never a receiver label, a `session:window` location, or implicit
    # discovery — and fail-closed when the pane cwd has no inferable
    # workspace/repo root. The resolved root then flows through the SAME
    # cross-session admission and `target_repo_mismatch` gates below as a
    # hand-passed `--target-repo <root>`; auto cannot weaken them (a pane with
    # no reachable marker is rejected here, and `--to claude` cross-session is
    # still blocked downstream regardless).
    if getattr(args, "target_repo", None) == AUTO_TARGET_REPO and herdr_send:
        # Redmine #13331 (j#73312 #2): herdr has no `%pane` to infer from, so `auto`
        # resolves to the sender's own repo root (the same-workspace target's repo). tmux
        # `auto` is untouched (guarded on `herdr_send`). See `herdr_auto_target_repo`.
        args.target_repo = herdr_auto_target_repo(args)
    elif getattr(args, "target_repo", None) == AUTO_TARGET_REPO:
        raw_target = getattr(args, "target", None)
        if not is_explicit_pane_target(raw_target):
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_args",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(
                "`--target-repo auto` requires an explicit `%pane` target; "
                f"target={(raw_target or '<receiver-window>')!r} is not a "
                "`%pane` id. Auto never widens to receiver-label, "
                "`session:window`, or discovery targets — name the exact pane, "
                "or pass an explicit `--target-repo <root>`."
            )
            raise AssertionError("unreachable")
        auto_cwd = target_info.get("cwd") or ""
        # Prefer the real Git worktree root over a nested project-local scaffold
        # marker (Redmine #12658 j#66504): a target pane inside a monorepo project
        # subdir that carries its own `.mozyo-bridge/scaffold.json` must still
        # resolve `--target-repo auto` to the Git repo root, not the subdir, so the
        # repo gate gates on the Git root as documented. Non-git scaffold
        # workspaces still fall back to the marker resolver (#11301).
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            resolve_workspace_root as _resolve_workspace_root,
        )

        auto_root = _resolve_workspace_root(auto_cwd)
        if not auto_root:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_repo_mismatch",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(
                "`--target-repo auto` could not infer a workspace/repo root "
                f"from target_cwd={(auto_cwd or '<unknown>')!r}; identity "
                "unestablished, fail-closed. Scaffold the target workspace so "
                "it carries a `.mozyo-bridge/scaffold.json` / git marker, or "
                "pass an explicit `--target-repo <root>`."
            )
            raise AssertionError("unreachable")
        # Diagnostics: record the resolved cwd and inferred root so the auto
        # decision is auditable, then hand the concrete root to the gates below.
        print(
            f"--target-repo auto resolved: target_pane={target} "
            f"target_cwd={auto_cwd!r} -> repo_root={auto_root!r}",
            file=sys.stderr,
        )
        args.target_repo = auto_root

    # Explicit-pane preflight projection (Redmine #11908): resolve the target
    # pane onto the canonical `TargetRecord` identity vocabulary
    # (`vibes/docs/logics/unit-target-model.md` "Resolver priority") via the same
    # projection `agents targets` uses, so normal-local and cockpit panes share
    # one resolver. Pane option role/workspace/lane is primary; the window name
    # is a compatibility fallback (`role_source == window_name`); ambiguous /
    # unknown is surfaced for fail-closed handling below.
    preflight_target = project_preflight_target(target_info)

    # Main-lane implementation-dispatch guard (Redmine #12441; prevention note
    # #12438 j#63436; role-based since Redmine #13174). In the managed cockpit /
    # sublane operating model (epic #12366;
    # `vibes/docs/logics/coordinator-sublane-development-flow.md`) the main-unit
    # implementer surface is not where implementation runs; implementation-shaped
    # work defaults to a cockpit-visible sublane, so a direct `implementation_request`
    # into the cockpit's default/main-lane implementer pane is a process gap (#12438
    # j#63432/j#63434). The guard reasons about the implementer *role*: the boundary
    # resolves the implementer's runtime provider from the repo-local RoleProviderBinding
    # (#12673/#13157; default -> `claude`, byte-identical) and fails closed in EVERY mode
    # (the resolved target's lane/view is known here, before the mode-scoped binding gate
    # below) unless an explicit `--main-lane-exception` references an owner/operator
    # decision. Deliberately scoped to cockpit panes: a plain `normal_window` agent, a
    # same-lane *sublane* implementer (non-`default` lane), a dispatch to any non-implementer
    # provider (the gateway route), and any non-`implementation_request` notification are all
    # unaffected. The binding wiring lives in the f_140 `main_lane_guard_gate` seam.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
        main_lane_guard_blocked,
    )

    if main_lane_guard_blocked(
        args, receiver=receiver, kind=kind, preflight_target=preflight_target
    ):
        _emit_outcome(
            make_outcome(
                status="blocked",
                reason="main_lane_implementation_blocked",
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        die(
            "blocked: `--to claude --kind implementation_request` resolved to "
            f"the repo's default/main lane (pane {target}, lane="
            f"{preflight_target.lane_id!r}). Implementation-shaped work defaults "
            "to a cockpit-visible sublane — \"pane already open\" is not an "
            "exception. Dispatch through the target-lane Codex gateway "
            "(`--to codex --target <session>:codex --target-repo <root>`), which "
            "performs the same-lane Claude handoff, or — only with a genuine "
            "owner/operator decision recorded in the durable anchor — pass "
            "`--main-lane-exception <journal-ref>`. A same-lane sublane Claude "
            "dispatch (non-default lane) is unaffected."
        )
        raise AssertionError("unreachable")

    if (mode == MODE_QUEUE_ENTER or require_receiver_binding) and not preflight_target.binds_receiver(receiver):
        # Step 9 (v0.2; role-aware since Redmine #11822, projection since #11908;
        # mode-independent for receiver-locked wrappers since Redmine #11779).
        # Under the relaxed queue-enter rail, marker miss does NOT roll back, so
        # an explicit `--target %X` that resolves to a different agent would
        # silently press Enter into the wrong receiver's pane. The agent gate
        # (`ensure_agent_target`) only verifies the pane is running *some* agent
        # process (claude / codex / node) and does not bind the pane to the
        # intended receiver. `binds_receiver` binds the explicit target to the
        # receiver via the canonical projection: a strong, non-ambiguous role ==
        # receiver from either the `@mozyo_agent_role` pane option (cockpit /
        # `cockpit_pane` view) or the `<agent>` window name (normal `mozyo` /
        # `normal_window` view). A cockpit pane no longer needs `--force`; a weak
        # / ambiguous / mismatched signal stays fail-closed, matching the
        # contract's "Allowed Targets".
        #
        # `require_receiver_binding` extends this gate to `standard` / `pending`
        # for wrappers whose contract fixes the receiver (cross-workspace
        # consult): without it, `--mode standard` / `--mode pending` would skip
        # the binding and let an explicit foreign-Claude `%pane` be typed into
        # under a `to=codex` marker.
        observed_window = preflight_target.window_name or "<unknown>"
        observed_role = preflight_target.pane_option_role or "<none>"
        _emit_outcome(
            make_outcome(
                status="blocked",
                reason="invalid_args",
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        gate_label = (
            f"--mode {MODE_QUEUE_ENTER}"
            if mode == MODE_QUEUE_ENTER
            else "this handoff primitive"
        )
        die(
            f"{gate_label} requires the explicit --target pane to resolve "
            f"to the receiver; --to={receiver!r} but pane {target} resolved to "
            f"role={preflight_target.role!r} (source={preflight_target.role_source}, "
            f"confidence={preflight_target.confidence}, "
            f"ambiguous={preflight_target.ambiguous}, view={preflight_target.view_kind}; "
            f"window={observed_window!r}, @mozyo_agent_role={observed_role!r}). "
            "Drop --target to use role resolution, or pass a pane that resolves "
            "to the receiver."
        )
        raise AssertionError("unreachable")

    if mode == MODE_QUEUE_ENTER and not herdr_send:
        # Redmine #13261: this session-binding gate binds the target to the sender's
        # *tmux session* — a concept that does not exist in a pure herdr session. It
        # is an explicit no-op under the herdr backend: the herdr target is addressed
        # by its live locator and is already scoped to the sender's workspace + role
        # by the inventory decode (WU1), which supersedes tmux-session binding.
        #
        # Step 10 (v0.3; constrained cross-session admission added in
        # Redmine #11301): session binding. queue-enter is bound to the
        # sender's tmux session by default — under marker miss it does not roll
        # back, so an explicit `--target %X` in a foreign session could
        # otherwise land in a different repo's agent, and tmux-outside
        # invocations have no sender session to compare against.
        #
        # A cross-session target is admitted ONLY under the constrained rail:
        # both sender and target sessions must be resolvable, `--target` must
        # be an explicit pane / tmux target (not receiver auto-discovery), and
        # `--target-repo` must be supplied so the workspace identity gate runs.
        # When admitted, the request still flows through every downstream gate:
        # the cross-session `--to claude` gate keeps Claude on the codex-gateway
        # path, the `--target-repo` gate fails closed on identity mismatch, and
        # Steps 11 / 12 bind the active pane and the foreground agent process.
        # This lets a configured workspace skip the manual `--mode standard`
        # fallback while ambiguous / unconfigured states stay fail-closed.
        sender_session = current_session_name()
        target_location = target_info.get("location") or ""
        target_session = (
            target_location.split(":", 1)[0] if ":" in target_location else ""
        )
        same_session = (
            bool(sender_session)
            and bool(target_session)
            and sender_session == target_session
        )
        if not same_session:
            both_sessions_known = bool(sender_session) and bool(target_session)
            explicit_target = bool(getattr(args, "target", None))
            has_target_repo = bool(getattr(args, "target_repo", None))
            cross_session_admitted = (
                both_sessions_known and explicit_target and has_target_repo
            )
            if not cross_session_admitted:
                _emit_outcome(
                    make_outcome(
                        status="blocked",
                        reason="invalid_args",
                        receiver=receiver,
                        target=target,
                        anchor=anchor,
                        mode=mode,
                        kind=kind,
                        notification_marker=None,
                        source=source,
                    ),
                    record_format=record_format,
                    command=record_command,
                )
                die(
                    "--mode queue-enter requires the target pane to live in the "
                    "sender's tmux session, or a constrained cross-session "
                    "target (an explicit --target pane id plus --target-repo "
                    "identity gate); "
                    f"sender_session={(sender_session or '<unset>')!r} "
                    f"target_session={(target_session or '<unknown>')!r} "
                    f"explicit_target={explicit_target} "
                    f"target_repo={'set' if has_target_repo else 'unset'}. "
                    "Run `mozyo-bridge` from inside the receiver's tmux "
                    "session, pass an explicit pane id together with "
                    "--target-repo, or use `--mode standard`."
                )
                raise AssertionError("unreachable")

    # Cross-Workspace Handoff Gate (Redmine #10332).
    #
    # When the resolved target lives in a different tmux session from the
    # sender, ``--to claude`` is rejected at the CLI. The cross-workspace
    # path must route through the target session's Codex window so the
    # target workspace's audit boundary is preserved; an origin Codex typing
    # directly into another workspace's Claude pane bypasses that boundary.
    #
    # Same-session ``--to claude`` is unaffected (existing window-only
    # resolver). Cross-session ``--to codex`` is the explicit gateway path.
    # When the sender is outside tmux (`sender_session` is None) the check
    # is skipped because we cannot prove cross-session intent; the
    # queue-enter rail's own session check below still applies in that
    # mode. The optional ``--target-repo`` check below adds repo-mismatch
    # fail-closed on top of this gate.
    # Redmine #13261: the cross-session `--to claude` gate compares tmux session
    # names. Under the herdr backend there is no tmux session (sender_session_xw is
    # empty), so the gate below is an explicit no-op — the herdr target's audit
    # boundary is enforced by the workspace-scoped inventory decode, not tmux-session
    # membership.
    sender_session_xw = "" if herdr_send else current_session_name()
    target_location_xw = target_info.get("location") or ""
    target_session_xw = (
        target_location_xw.split(":", 1)[0] if ":" in target_location_xw else ""
    )
    if (
        sender_session_xw
        and target_session_xw
        and sender_session_xw != target_session_xw
        and receiver == "claude"
    ):
        _emit_outcome(
            make_outcome(
                status="blocked",
                reason="cross_session_claude",
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
            ),
            record_format=record_format,
            command=record_command,
        )
        # Diagnostics only (Redmine #11776): point the operator at the safe
        # Codex gateway route with concrete candidate pane(s). Best-effort —
        # any discovery failure falls back to the boundary message unchanged,
        # and the cross-session `--to claude` block itself is untouched.
        gateway_hint = ""
        try:
            from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr
            from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
                codex_gateway_candidates,
            )
            from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import cross_session_gateway_hint

            _cands = [
                rec.to_dict()
                for rec in codex_gateway_candidates(
                    target_session_xw, _pr.pane_lines()
                )
            ]
            gateway_hint = cross_session_gateway_hint(target_session_xw, _cands)
        except (Exception, SystemExit):
            # Best-effort: `pane_lines()` calls `die()` (SystemExit) when tmux
            # is absent. Catch SystemExit too so the diagnostics path can never
            # pre-empt the `cross_session_claude` boundary message that must be
            # the command's terminal output (Redmine #11778).
            gateway_hint = ""
        die(
            "cross-session handoff to Claude is not allowed; "
            f"sender_session={sender_session_xw!r} target_session={target_session_xw!r}. "
            "Naming a foreign workspace's Claude pane directly bypasses its "
            "audit boundary. Route through the target session's Codex window "
            "with `--to codex --target <target_session>:codex --target-repo "
            "<target_workspace_root>` and ask that Codex to perform the local "
            "Claude handoff. With an explicit --target and a passing "
            "--target-repo identity gate, that gateway send is admitted on the "
            "default queue-enter rail (Redmine #11301); `--mode standard` (or "
            "`--mode pending`) remains an available fallback, e.g. when you "
            "cannot assert --target-repo. See the Cross-Workspace Handoff rule "
            "in the agent workflow."
            + (f"\n\n{gateway_hint}" if gateway_hint else "")
        )
        raise AssertionError("unreachable")

    # Gateway Route Enforcement Gate (Redmine #12918): fail closed when a governed
    # implementation_request / review_result is sent `--to claude` directly to a
    # worker in a different lane than the sender, bypassing that lane's Codex
    # gateway. The whole gate (policy + emit + die) lives in the f_140
    # `application/gateway_route_gate` seam so this oversized module keeps only the
    # one call.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_route_gate import (
        enforce_gateway_route,
    )

    # Redmine #13261 (increment 4): under the herdr backend resolve the sender lane
    # Unit from the env-derived SenderIdentity (already resolved for the target above)
    # so the gate enforces on the env sender lane and makes ZERO tmux calls; under tmux
    # `sender_lane_unit` is None and the gate keeps its `current_pane_lane_unit()` path
    # byte-identical.
    herdr_sender_lane_unit = (
        (target_info.get("herdr_sender_workspace_id"), target_info.get("herdr_sender_lane_id"))
        if herdr_send
        else None
    )
    enforce_gateway_route(
        args,
        kind=kind,
        receiver=receiver,
        preflight_target=preflight_target,
        source=source,
        mode=mode,
        anchor=anchor,
        target=target,
        record_format=record_format,
        record_command=record_command,
        emit=_emit_outcome,
        sender_lane_unit=herdr_sender_lane_unit,
    )

    expected_target_repo = getattr(args, "target_repo", None)
    if expected_target_repo:
        expected_resolved = str(Path(expected_target_repo).expanduser().resolve())
        # Prefer the real Git worktree root over a nested project-local scaffold
        # marker (Redmine #12658 j#66504) so a target pane inside a monorepo
        # project subdir (which may carry its own `.mozyo-bridge/scaffold.json`)
        # still gates against the Git repo root — otherwise an explicit
        # `--target-repo <Git root>` would fail closed before the project gate can
        # run. Non-git scaffold workspaces still fall back to the marker resolver.
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            resolve_workspace_root as _resolve_workspace_root,
        )

        observed_repo = _resolve_workspace_root(target_info.get("cwd") or "")
        # Identity comparison goes through the shared Unicode normalization
        # (Redmine #11625): an NFC-spelled --target-repo must match an NFD
        # pane cwd for the same directory instead of fail-closing on bytes.
        if observed_repo is None or normalize_path_unicode(
            observed_repo
        ) != normalize_path_unicode(expected_resolved):
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_repo_mismatch",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            if observed_repo is None:
                # Identity could not be established at all: the target cwd does
                # not walk up to any git / pyproject / scaffold marker. Keep
                # fail-closed, but hand back a concrete setup action instead of
                # forcing the operator to reason about repo-root heuristics.
                setup_hint = (
                    "the target workspace has no identity marker reachable "
                    f"from target_cwd={(target_info.get('cwd') or '<unknown>')!r}. "
                    "For a non-git workspace, scaffold it so it carries "
                    "`.mozyo-bridge/scaffold.json` (run `mozyo-bridge scaffold "
                    f"apply <preset> --target {expected_resolved}`), then retry. "
                    "Or drop `--target-repo` to skip the check."
                )
            else:
                setup_hint = (
                    f"target pane resolves to repo root {observed_repo!r}. "
                    "Pass a target pane whose cwd resolves under the expected "
                    "repo root, or drop `--target-repo` to skip the check."
                )
            die(
                "target pane is not in the expected repo; "
                f"expected={expected_resolved!r} "
                f"observed={(observed_repo or '<unknown>')!r} "
                f"target_cwd={(target_info.get('cwd') or '<unknown>')!r}. "
                + setup_hint
            )
            raise AssertionError("unreachable")

    # Project-Scope Handoff Gate (Redmine #12658). LAYERED ON TOP of the Git
    # `--target-repo` gate above, never replacing it: the repo gate stays the
    # fail-closed Git-repo-root identity check, and this adds an additional
    # constraint that the target resolve to a specific adopted project scope. A
    # target in the correct Git repository but OUTSIDE the expected project path
    # fails closed here. `--target-repo auto` is not repurposed to resolve project
    # paths (it still gates on the Git repo root); the project scope is derived
    # separately from the target pane's cwd via the bounded project discovery, or
    # read from a stamped `@mozyo_project_scope` pane option when present.
    expected_project = getattr(args, "target_project", None)
    if expected_project:
        target_cwd = target_info.get("cwd") or ""
        # Project scope is layered UNDER the Git repo identity and is never a
        # substitute for repo preflight (Redmine #12658 review j#66481 blocker 2):
        # `--target-project` requires an explicit `--target-repo` (incl. `auto`)
        # gate so the same adopted project id in an unrelated repo can never become
        # the sole identity gate. `--target-repo` has already been validated above
        # when present.
        if not expected_target_repo:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_args",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(
                "`--target-project` requires an explicit `--target-repo` "
                "(or `--target-repo auto`) Git-repo gate; project scope is layered "
                "under workspace identity and must not be the sole identity gate. "
                f"target_project={expected_project!r} was given without "
                "`--target-repo`. Add `--target-repo <root>` / `--target-repo auto`."
            )
            raise AssertionError("unreachable")

        observed_scope = None
        observed_path = None
        # Default to the explicit repo gate value so the fail-closed die() message
        # below always has a concrete git_repo_root, even if discovery raises.
        git_root = str(Path(expected_target_repo).expanduser().resolve())
        try:
            from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
                project_scope_for_cwd,
                resolve_workspace_root,
            )
            from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
                path_under_repo_relative,
            )

            # The project path is repo-relative to the real Git worktree root, so
            # the stamped cwd-under-project check resolves the Git root (preferring
            # it over a nested project-local scaffold marker, #12658 j#66499). The
            # repo gate above already enforced `--target-repo`.
            git_root = resolve_workspace_root(target_cwd) or git_root
            stamped_scope = (target_info.get("project_scope") or "").strip()
            stamped_path = (target_info.get("project_path") or "").strip()
            # A stamped pane option is a projection cache, not authority: it is only
            # trusted when the pane's cwd is actually under the stamped project path
            # within the verified Git repo root (Redmine #12658 review j#66481
            # blocker 1) — a stale / wrong option can never bypass the
            # cwd-under-project condition. Otherwise (or on no stamp) the scope is
            # re-derived from the live project.yaml sources, which is itself
            # cwd-under-project by construction and fail-closes on cache drift.
            if stamped_scope and stamped_path and path_under_repo_relative(
                target_cwd, repo_root=git_root, project_path=stamped_path
            ):
                observed_scope = stamped_scope
                observed_path = stamped_path
            else:
                resolved = project_scope_for_cwd(target_cwd, git_root)
                if resolved is not None:
                    observed_scope = resolved.scope
                    observed_path = resolved.path
        except Exception:  # noqa: BLE001 - fail closed below on any discovery error
            observed_scope = None
            observed_path = None

        if observed_scope != expected_project:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_project_mismatch",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(
                "target pane is not in the expected project scope; "
                f"expected_project={expected_project!r} "
                f"observed_project={(observed_scope or '<none>')!r} "
                f"observed_project_path={(observed_path or '<none>')!r} "
                f"git_repo_root={(git_root or '<unknown>')!r} "
                f"target_cwd={(target_cwd or '<unknown>')!r}. "
                "The target must be inside the expected adopted project (its cwd "
                "under the project path) with a passing Git repo gate. A target in "
                "the correct repo but outside the project path fails closed. Pass "
                "a pane whose cwd is under the project, ensure the project carries "
                "a `runtime_identity.enabled: true` opt-in, or drop "
                "`--target-project` to gate on the Git repo root only."
            )
            raise AssertionError("unreachable")

    # Step 11 (v0.5, Redmine #12597): standard_target_admission. Replaces the
    # v0.3 unconditional active-split fail-closed. tmux delivers keystrokes to
    # the pane addressed by `-t` even when it is an inactive split, so the old
    # gate's concern was visibility (the receiver agent is, by construction, not
    # the foreground process the operator is looking at), not deliverability.
    # The owner (j#65493) judged the hard block over-strict: an inactive
    # registered agent pane that passes the minimal admission contract (live
    # pane + strong role match + workspace_id + unambiguous target) is now
    # *activated* by the rail (via `tmux select-pane` — pane selection only,
    # never raw key injection) and delivered to, with the active化 / restore
    # facts recorded in the durable record. `lane_id` / the Step 12 foreground
    # allowlist / repo-cwd checks stay as additional hardening, not minimal
    # admission conditions, so a git-less / non-scaffolded unit is not broken.
    # The policy is config-driven through the single
    # `resolve_standard_target_admission_policy` seam (constants + optional CLI
    # overrides), not scattered per caller/wrapper.
    admission_policy = resolve_standard_target_admission_policy(
        activate_inactive=(
            False if getattr(args, "no_target_activation", False) else None
        ),
        restore_previous_active=(
            True if getattr(args, "restore_previous_active", False) else None
        ),
    )
    admission = evaluate_standard_target_admission(
        target_info, receiver=receiver, preflight=preflight_target
    )
    activate_inactive_target = False
    target_activation: TargetActivationOutcome | None = None
    if mode == MODE_QUEUE_ENTER and target_info.get("pane_active") != "1":
        if admission.admitted and admission_policy.activate_inactive:
            # Admitted inactive split: defer the actual `select-pane` until just
            # before typing (after the remaining die-able gates) so we never
            # steal focus for a send that then fails a later gate.
            activate_inactive_target = True
        else:
            observed_active = target_info.get("pane_active") or "<unknown>"
            # Concrete strict-rail recovery (Redmine #12162). `target` is the
            # already-resolved pane id (an explicit `%pane`), so `--target-repo
            # auto` can pin its identity, and the command carries the same
            # receiver / source / kind / anchor.
            recovery_command = build_inactive_pane_fallback_command(
                receiver=receiver,
                kind=kind,
                target=target,
                anchor=anchor,
            )
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_args",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
                recovery_command=recovery_command,
            )
            if not admission_policy.activate_inactive:
                reason_clause = (
                    "target-pane activation is disabled by policy "
                    "(--no-target-activation), so an inactive split stays "
                    "fail-closed exactly like the pre-#12597 active-split gate"
                )
            else:
                reason_clause = (
                    "standard_target_admission did not admit the inactive "
                    "split; unmet minimal conditions: "
                    f"{', '.join(admission.unmet_conditions()) or '—'} "
                    "(register the workspace so the pane carries a workspace_id, "
                    "or use a pane that resolves strongly to the receiver)"
                )
            if recovery_command:
                fallback_hint = (
                    " The safest retry is the strict rail, which does not "
                    "require the receiver pane to be the active split (it "
                    f"observes the landing marker instead): `{recovery_command}`"
                )
            else:
                fallback_hint = (
                    " As a fallback you can pin the pane and re-check identity "
                    "with `--target %pane --target-repo auto` and retry under "
                    "`--mode standard`, which does not require the active split."
                )
            die(
                "--mode queue-enter requires the target pane to be the active "
                "split of its window or to pass standard_target_admission; pane "
                f"{target} has pane_active={observed_active!r} and "
                f"{reason_clause}. Activate the receiver pane in tmux, or drop "
                "--target to use window-name resolution." + fallback_hint
            )
            raise AssertionError("unreachable")

    if mode == MODE_QUEUE_ENTER:
        # Step 12 (v0.3): per-receiver foreground process binding. Stricter
        # than the generic `ensure_agent_target` agent gate. Literal basenames
        # (`claude` / `node` for `claude`, `codex` for `codex`) give strong
        # receiver identity. Versioned native binary basenames give only weak
        # identity (receiver-agnostic regex) — see
        # `is_receiver_agent_process` and Open Question 8 in the contract.
        # The CLI does not advertise the weak case as strong; it just admits
        # under Step 9 + Layer A discipline.
        pane_command = target_info.get("command") or ""
        if not is_receiver_agent_process(pane_command, receiver):
            observed_command = Path(pane_command).name or "<none>"
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_not_agent",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(
                "--mode queue-enter requires the foreground process to match "
                f"the {receiver} agent; pane {target} has process "
                f"{observed_command!r}. Restart the receiver agent in the "
                "pane, or pass a pane that is running the agent."
            )
            raise AssertionError("unreachable")
    else:
        try:
            ensure_agent_target(target_info, receiver, force=bool(getattr(args, "force", False)))
        except SystemExit:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="target_not_agent",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                ),
                record_format=record_format,
                command=record_command,
            )
            raise

    # Target execution root / workdir propagation (Redmine #12098). When the
    # operator asserts a `--workdir`, carry it as an explicit execution root so
    # the receiver can recover a nested project root (distinct from the pane cwd
    # / cross-workspace repo root) from the durable record instead of grepping
    # pane scrollback. The relative pointer is computed against the strongest
    # available repo anchor: an explicit `--target-repo` (already resolved from
    # `auto` above when used), else the target pane's inferred repo root. This
    # is wording/record-layer only — it does not gate pane selection and does
    # not relax any cross-session / cross-lane boundary.
    execution_root = None
    workdir_arg = getattr(args, "workdir", None)
    if workdir_arg:
        workdir_abs = str(Path(workdir_arg).expanduser().resolve())
        repo_anchor = getattr(args, "target_repo", None)
        if repo_anchor and repo_anchor != AUTO_TARGET_REPO:
            repo_anchor_abs = str(Path(repo_anchor).expanduser().resolve())
        else:
            repo_anchor_abs = infer_repo_root(target_info.get("cwd") or "") or None
        execution_root = build_execution_root(
            workdir_abs, repo_root_abs=repo_anchor_abs
        )

    # Redmine #12388: resolve the requested fixed role profile before any pane
    # send. Auto-fill `durable_anchor` from the anchor so the most common
    # placeholder needs no `--profile-field`. Fail closed (blocked /
    # invalid_args) on an unknown role or a malformed `--profile-field`; omitting
    # `--role-profile` is the explicit fallback of no profile expansion.
    role_profile_resolution = None
    role_profile_arg = getattr(args, "role_profile", None)
    if role_profile_arg:
        try:
            profile_fields = parse_profile_fields(getattr(args, "profile_field", None))
            profile_fields.setdefault("durable_anchor", anchor.human_pointer())
            role_profile_resolution = resolve_role_profile(
                role_profile_arg, profile_fields
            )
        except RoleProfileError as exc:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_args",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                    execution_root=execution_root,
                ),
                record_format=record_format,
                command=record_command,
            )
            die(str(exc))
            raise AssertionError("unreachable")

    role_profile_contract = (
        role_profile_resolution.resolved_text if role_profile_resolution else None
    )

    # Redmine #12706: resolve the explicit transition role/action boundary before
    # any pane send. The token is set programmatically by the routing command (the
    # `project-gateway handoff` route injects `grandparent_coordinator` on a
    # successful gateway resolution), never typed manually as product evidence.
    # Fail closed (blocked / invalid_args) on an unknown token; omitting it is the
    # explicit fallback of no role binding.
    transition_role_boundary = None
    transition_role_arg = getattr(args, "transition_role", None)
    if transition_role_arg:
        try:
            transition_role_boundary = resolve_transition_role(transition_role_arg)
        except TransitionRoleError as exc:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_args",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                    execution_root=execution_root,
                    role_profile=role_profile_resolution,
                ),
                record_format=record_format,
                command=record_command,
                role_profile_contract=role_profile_contract,
            )
            die(str(exc))
            raise AssertionError("unreachable")

    # Redmine #12700: resolve the workflow-contract reference bundle before any
    # pane send. The token is set programmatically by the routing command (the
    # `project-gateway handoff` route injects the grandparent bundle on a
    # successful gateway resolution), never typed manually. Fail closed (blocked /
    # invalid_args) on an unknown token; omitting it is the explicit fallback of no
    # contract binding. The bundle carries resolvable doc pointers (catalog ids +
    # canonical + monorepo-nested paths), never doc bodies.
    workflow_contract_bundle = None
    workflow_contract_arg = getattr(args, "workflow_contract", None)
    if workflow_contract_arg:
        try:
            workflow_contract_bundle = resolve_workflow_contract(workflow_contract_arg)
        except WorkflowContractError as exc:
            _emit_outcome(
                make_outcome(
                    status="blocked",
                    reason="invalid_args",
                    receiver=receiver,
                    target=target,
                    anchor=anchor,
                    mode=mode,
                    kind=kind,
                    notification_marker=None,
                    source=source,
                    execution_root=execution_root,
                    role_profile=role_profile_resolution,
                    transition_role=transition_role_boundary,
                ),
                record_format=record_format,
                command=record_command,
                role_profile_contract=role_profile_contract,
            )
            die(str(exc))
            raise AssertionError("unreachable")

    try:
        body = build_notification_body(
            anchor,
            kind,
            summary,
            receiver,
            execution_root=execution_root,
            role_profile=role_profile_resolution,
            transition_role=transition_role_boundary,
            workflow_contract=workflow_contract_bundle,
            ticketless_callback=ticketless_callback_payload,
            ticketless_consultation=ticketless_consultation_payload,
            ticketless_work_intake=ticketless_work_intake_payload,
        )
    except AnchorError as exc:
        _emit_outcome(
            make_outcome(
                status="blocked",
                reason="invalid_args",
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=None,
                source=source,
                execution_root=execution_root,
                role_profile=role_profile_resolution,
                transition_role=transition_role_boundary,
                workflow_contract=workflow_contract_bundle,
                ticketless_callback=ticketless_callback_payload,
                ticketless_consultation=ticketless_consultation_payload,
                ticketless_work_intake=ticketless_work_intake_payload,
            ),
            record_format=record_format,
            command=record_command,
            role_profile_contract=role_profile_contract,
        )
        die(str(exc))
        raise AssertionError("unreachable")

    marker = build_marker(anchor, kind, receiver)

    read_lines = int(getattr(args, "read_lines", 50) or 50)
    # Internal pane snapshot preflight. The standard path must not require
    # callers to run `mozyo-bridge read` first.
    capture_pane(target, read_lines)

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
        rail = active_herdr_turn_start_rail
        if rail is None:  # defensive: the decorator always stashes it under herdr
            die(
                "herdr backend selected for a --mode standard send but no turn-start "
                "rail was installed; refusing to fall back to the capture rail. "
                f"target={target}"
            )
            raise AssertionError("unreachable")
        turn_start = rail.drive_turn_start(target, f"{marker} {body}")
        status, reason = _project_herdr_turn_start(turn_start)
        # Machine-readable turn-start telemetry (turn_start_outcome / snapshot_state /
        # wait_kind / enter_resends / reclassified_blocked) for EVERY rail outcome,
        # rendered redaction-safe by the rail's own record renderer and persisted on
        # the delivery record (auditor j#72602 decision 4).
        turn_start_lines = _turn_start_rail_record_lines(turn_start)
        # Redmine #13255 j#72695: carry the SAME telemetry as a structured field on
        # the outcome so it lands in the JSON / persisted record (an auditor / the
        # future #12656 ledger reads this, not the reused `(status, reason)` wire
        # alone) AND so the delivery-record wording discriminates a herdr
        # `delivered_not_started` from the tmux/capture standard rail's
        # `turn_start_unconfirmed`.
        turn_start_telemetry = turn_start.to_telemetry_dict()
        outcome = make_outcome(
            status=status,
            reason=reason,
            receiver=receiver,
            target=target,
            anchor=anchor,
            mode=mode,
            kind=kind,
            notification_marker=marker,
            execution_root=execution_root,
            role_profile=role_profile_resolution,
            transition_role=transition_role_boundary,
            workflow_contract=workflow_contract_bundle,
            ticketless_callback=ticketless_callback_payload,
            ticketless_consultation=ticketless_consultation_payload,
            ticketless_work_intake=ticketless_work_intake_payload,
            turn_start_outcome=turn_start_telemetry,
        )
        _emit_outcome(
            outcome,
            record_format=record_format,
            command=record_command,
            duplicate_lane_panes=duplicate_lane_panes or None,
            role_profile_contract=role_profile_contract,
            submit_lines=_submit_lines_for(args, outcome),
            turn_start_lines=turn_start_lines,
        )
        # Redmine #13300: persist every event-rail outcome (incl. delivered-not-started)
        # before the sent/die branch, so no terminal event-rail path escapes the ledger.
        _record_herdr_send_ledger(outcome)
        if status == "sent":
            _maybe_persist_delivery_record(
                args,
                outcome,
                duplicate_lane_panes=duplicate_lane_panes,
                record_format=record_format,
                turn_start_lines=turn_start_lines,
            )
            return 0
        die(
            "handoff was routed through the herdr event-driven turn-start rail but "
            f"no turn start was confirmed (rail outcome {turn_start.outcome}); the "
            f"{receiver} receiver was not observed starting a turn. The marker+body "
            "was typed at most once and only Enter was sent (no C-u rollback, no "
            f"re-send). Read the receiver before re-issuing. target={target} "
            f"marker={marker}"
        )
        raise AssertionError("unreachable")

    run_tmux("send-keys", "-t", target, "-l", "--", f"{marker} {body}")

    if mode == MODE_PENDING:
        outcome = make_outcome(
            status="pending_input",
            reason="ok",
            receiver=receiver,
            target=target,
            anchor=anchor,
            mode=mode,
            kind=kind,
            notification_marker=marker,
            execution_root=execution_root,
            role_profile=role_profile_resolution,
            transition_role=transition_role_boundary,
            workflow_contract=workflow_contract_bundle,
            ticketless_callback=ticketless_callback_payload,
            ticketless_consultation=ticketless_consultation_payload,
            ticketless_work_intake=ticketless_work_intake_payload,
        )
        _emit_outcome(
            outcome,
            record_format=record_format,
            command=record_command,
            duplicate_lane_panes=duplicate_lane_panes or None,
            role_profile_contract=role_profile_contract,
            submit_lines=_submit_lines_for(args, outcome),
        )
        _maybe_persist_delivery_record(
            args,
            outcome,
            duplicate_lane_panes=duplicate_lane_panes,
            record_format=record_format,
        )
        return 0

    landing_timeout = float(getattr(args, "landing_timeout", 8.0) or 8.0)
    landing_lines = max(read_lines, 200)
    marker_observed = wait_for_text(target, marker, landing_lines, landing_timeout)

    if not marker_observed and mode != MODE_QUEUE_ENTER:
        run_tmux("send-keys", "-t", target, "C-u")
        outcome = make_outcome(
            status="blocked",
            reason="marker_timeout",
            receiver=receiver,
            target=target,
            anchor=anchor,
            mode=mode,
            kind=kind,
            notification_marker=marker,
            execution_root=execution_root,
            role_profile=role_profile_resolution,
            transition_role=transition_role_boundary,
            workflow_contract=workflow_contract_bundle,
            ticketless_callback=ticketless_callback_payload,
            ticketless_consultation=ticketless_consultation_payload,
            ticketless_work_intake=ticketless_work_intake_payload,
        )
        _emit_outcome(
            outcome,
            record_format=record_format,
            command=record_command,
            duplicate_lane_panes=duplicate_lane_panes or None,
            role_profile_contract=role_profile_contract,
            submit_lines=_submit_lines_for(args, outcome),
        )
        _emit_handoff_marker_timeout_guidance(receiver)
        die(
            "handoff marker was not observed in target pane; a C-u rollback was issued and Enter was not pressed (the receiver composer state was not verified). "
            f"target={target} marker={marker}"
        )
        raise AssertionError("unreachable")

    submit_delay = max(0.0, float(getattr(args, "submit_delay", 0.2) or 0.0))
    if submit_delay:
        time.sleep(submit_delay)

    # Redmine #13166 / #13262: on the strict `--mode standard` rail, snapshot the
    # receiver pane immediately before Enter so the post-Enter turn-start
    # observation below has a pre-submit baseline. The marker was already observed
    # (a marker miss died at `marker_timeout` above), so this baseline holds the
    # marker+body sitting in the composer. #13166 gated this on codex only; #13262
    # generalizes it to any `--mode standard` send (claude and codex) since the
    # observation is receiver-agnostic. The queue-enter rail keeps its prior
    # behavior untouched (its marker-unobserved path stays `sent` / `queue_enter`).
    standard_rail = mode == MODE_STANDARD
    turn_start_window = _resolve_turn_start_window(
        getattr(args, "landing_timeout", None), landing_timeout
    )
    turn_start_baseline = (
        capture_pane(target, landing_lines) if standard_rail else None
    )

    run_tmux("send-keys", "-t", target, "Enter")
    enter_attempts = 1

    # Enter-only retry (Redmine #12580 / #12581). Only the `queue-enter` rail,
    # and only when the landing marker was not observed: a busy or redrawing
    # Claude/Codex TUI can drop the first Enter even though the marker+body
    # landed cleanly. Re-issue Enter — and ONLY Enter; the marker+body typed
    # once above is never re-injected, and an empty Enter on an idle agent
    # composer is a no-op, so the payload cannot be duplicated — on the policy
    # interval until the marker is observed or the window elapses. The
    # `standard` / `pending` rails never reach this branch, so their semantics
    # are untouched. The 30s/2s defaults live behind
    # `resolve_queue_enter_retry_policy` (the config boundary) and are
    # overridable via `--queue-enter-retry-window` / `-interval`.
    retry_policy = resolve_queue_enter_retry_policy(
        getattr(args, "queue_enter_retry_window", None),
        getattr(args, "queue_enter_retry_interval", None),
    )
    retry_engaged = (
        mode == MODE_QUEUE_ENTER and not marker_observed and retry_policy.enabled
    )
    if retry_engaged:
        for _ in range(retry_policy.max_retries):
            if retry_policy.interval_seconds:
                time.sleep(retry_policy.interval_seconds)
            if _marker_visible_in(capture_pane(target, landing_lines), marker):
                marker_observed = True
                break
            run_tmux("send-keys", "-t", target, "Enter")
            enter_attempts += 1

    # Redmine #13166 / #13262: standard-rail turn-start verification. Marker
    # observed + Enter issued proves the sender pressed Enter, not that the receiver
    # TUI submitted the prompt and started a turn — a busy / redrawing composer can
    # absorb the Enter and leave the marker+body unsubmitted while the rail still
    # reported `sent` / `ok` (the false-positive delivery this bug fixes). The rail
    # now observes the receiver pane for post-Enter turn-start activity (read-only;
    # no re-typed marker+body, no re-issued Enter, no auto-resend). #13166 ran this
    # for codex only; #13262 generalizes it to any `--mode standard` send (claude
    # and codex). The queue-enter rail is untouched: its marker-unobserved path
    # stays `sent` / `queue_enter`, never blocking.
    turn_start_lines = None
    if standard_rail:
        turn_start = _observe_standard_turn_start(
            target,
            baseline_capture=turn_start_baseline or "",
            capture=capture_pane,
            sleep=time.sleep,
            window_seconds=turn_start_window,
            lines=landing_lines,
        )
        turn_start_lines = _turn_start_record_lines(
            turn_start, rail_label=f"{receiver} standard-rail"
        )
        if not turn_start.confirmed:
            outcome = make_outcome(
                status="blocked",
                reason="turn_start_unconfirmed",
                receiver=receiver,
                target=target,
                anchor=anchor,
                mode=mode,
                kind=kind,
                notification_marker=marker,
                execution_root=execution_root,
                role_profile=role_profile_resolution,
                transition_role=transition_role_boundary,
                workflow_contract=workflow_contract_bundle,
                ticketless_callback=ticketless_callback_payload,
                ticketless_consultation=ticketless_consultation_payload,
                ticketless_work_intake=ticketless_work_intake_payload,
            )
            _emit_outcome(
                outcome,
                record_format=record_format,
                command=record_command,
                duplicate_lane_panes=duplicate_lane_panes or None,
                role_profile_contract=role_profile_contract,
                submit_lines=_submit_lines_for(args, outcome),
                turn_start_lines=turn_start_lines,
            )
            die(
                "handoff landing marker was observed and Enter was pressed, but the "
                f"{receiver} receiver pane showed no turn-start activity within the "
                "observation window; the Enter may have been absorbed by a busy / "
                "redrawing composer. No C-u rollback and no re-send were issued (the "
                "marker+body was typed once). Read the receiver to confirm whether "
                "the turn started before re-issuing under --mode standard. "
                f"target={target} marker={marker}"
            )
            raise AssertionError("unreachable")

    # Redmine #13292: additive, telemetry-only queue-enter turn-start observation
    # under the herdr backend (j#72602 decision 5's deferred follow-up, design
    # confirmed #13292 j#72759). The queue-enter inject → Enter → Enter-only retry
    # choreography above is left BYTE-IDENTICAL; only AFTER it, and only for a herdr
    # send, do we take a read-only `agent get` runtime-state snapshot (#13246
    # `read_agent_state`, borrowed from the already-resolved rail's reader — no
    # second config read, no `drive_turn_start`, no injection ownership, no
    # `precondition_not_idle` fail-close). The result is recorded as additive
    # telemetry ONLY: it never changes `status` / `reason` / `next_action_owner`
    # (they stay `sent` / `ok`|`queue_enter` / `receiver`) and never blocks the send
    # — a read failure / `unknown` / `awaiting_input` all just record telemetry. It
    # is kept structurally separate from the event rail's `turn_start_outcome` (a
    # post-hoc snapshot does not prove causality like an armed `wait agent-status`
    # transition). The tmux backend and every non-queue-enter rail are untouched
    # (`queue_enter_observation` stays `None`).
    queue_enter_observation = None
    if herdr_send and mode == MODE_QUEUE_ENTER:
        rail = active_herdr_turn_start_rail
        if rail is not None:  # always installed under herdr; skip defensively if not
            queue_enter_snapshot = _observe_queue_enter_turn_start(
                target,
                read=rail.reader.read_agent_state,
                sleep=time.sleep,
            )
            queue_enter_observation = queue_enter_snapshot.to_telemetry_dict()
            # Reuse the additive `turn_start_lines` record channel (appended, never
            # overrides `next_action`); the queue-enter renderer labels itself
            # telemetry-only and does not reuse the event rail's wording.
            turn_start_lines = _queue_enter_turn_start_record_lines(queue_enter_snapshot)

    # Wording-layer differentiation under the relaxed `queue-enter` rail:
    # marker observed (possibly via the Enter-only retry above) → strict
    # `sent`/`ok`; marker still unobserved → `sent`/`queue_enter` (sender did
    # not pre-confirm landing). The receiver-side contract and
    # `next_action_owner` stay identical to strict `sent` per the contract.
    relaxed_unobserved = mode == MODE_QUEUE_ENTER and not marker_observed
    outcome = make_outcome(
        status="sent",
        reason="queue_enter" if relaxed_unobserved else "ok",
        receiver=receiver,
        target=target,
        anchor=anchor,
        mode=mode,
        kind=kind,
        notification_marker=marker,
        execution_root=execution_root,
        role_profile=role_profile_resolution,
        # Redmine #12706 carried the transition boundary on the pending /
        # marker_timeout paths but dropped it on this successful-delivery path, so
        # the durable record / JSON wire showed no boundary on a real send. Thread
        # it here so the standard transition payload carries the boundary on the
        # delivery that matters; #12700 adds the workflow-contract bundle alongside.
        transition_role=transition_role_boundary,
        workflow_contract=workflow_contract_bundle,
        ticketless_callback=ticketless_callback_payload,
        ticketless_consultation=ticketless_consultation_payload,
        ticketless_work_intake=ticketless_work_intake_payload,
        # Redmine #13292: additive, telemetry-only herdr queue-enter turn-start
        # snapshot (`None` for tmux / non-queue-enter). Never influences the wire.
        queue_enter_turn_start_observation=queue_enter_observation,
    )
    # Durable retry telemetry (policy + attempted count + interval) is recorded
    # in the delivery record / narrative only when the Enter-only retry actually
    # engaged. It is wording-layer only: it never reaches the wire enums or the
    # inspector projection (`Status` / `reason` / `next_action_owner` are
    # unchanged), matching the contract's strong boundary.
    retry_record = (
        QueueEnterRetryOutcome(
            window_seconds=retry_policy.window_seconds,
            interval_seconds=retry_policy.interval_seconds,
            enter_attempts=enter_attempts,
            marker_observed=marker_observed,
        )
        if retry_engaged
        else None
    )
    # Redmine #12597: if standard_target_admission activated an inactive split
    # and the policy asks to restore focus, re-select the previously-active pane
    # after delivery. Pane selection only, best-effort (a vanished pane must not
    # break the already-completed send), and the restore fact is recorded.
    target_activation = _maybe_restore_previous_active(
        target_activation,
        restore_previous_active=admission_policy.restore_previous_active,
    )
    _emit_outcome(
        outcome,
        record_format=record_format,
        command=record_command,
        duplicate_lane_panes=duplicate_lane_panes or None,
        role_profile_contract=role_profile_contract,
        retry=retry_record,
        activation=target_activation,
        submit_lines=_submit_lines_for(args, outcome),
        turn_start_lines=turn_start_lines,
    )
    _maybe_persist_delivery_record(
        args,
        outcome,
        duplicate_lane_panes=duplicate_lane_panes,
        record_format=record_format,
        retry=retry_record,
        activation=target_activation,
        turn_start_lines=turn_start_lines,
    )
    # Redmine #13300: persist the herdr queue-enter outcome to the #13296 ledger. This
    # terminal block is shared with tmux, so the emission is guarded on `herdr_send`
    # (tmux 経路不変); the Enter-only retry telemetry enriches the same entry.
    if herdr_send:
        _record_herdr_send_ledger(outcome, retry_outcome=retry_record)
    return 0


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
    """Thin adapter: the body lives in ``HandoffCommandUseCase.run_ticketless_callback``."""
    from mozyo_bridge.application.handoff_command import (
        HandoffCommandUseCase,
        LiveHandoffCommandOps,
    )

    return HandoffCommandUseCase(
        LiveHandoffCommandOps()
    ).run_ticketless_callback(args)


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
