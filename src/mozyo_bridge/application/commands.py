from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
from pathlib import Path

from mozyo_bridge.application.doctor import format_doctor_text, run_doctor
from mozyo_bridge.application.doctor_command import DoctorCommandUseCase
from mozyo_bridge.application.pane_primitive_command import (
    LivePanePrimitiveOps,
    PanePrimitiveOutcome,
    PanePrimitiveUseCase,
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
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    AGENT_KIND_UNKNOWN,
    ROLE_SOURCE_WINDOW_NAME,
    VIEW_KIND_COCKPIT_PANE,
    infer_repo_root,
    project_preflight_target,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
    InvalidPermissionMode,
    permission_mode_flag,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AUTO_TARGET_REPO,
    AnchorError,
    KIND_LABELS,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODES,
    NO_SUBMIT_RETRY_BUDGET,
    QueueEnterRetryOutcome,
    RECEIVERS,
    RECORD_FORMAT_BOTH,
    RECORD_FORMAT_JSON,
    RECORD_FORMAT_TEXT,
    RECORD_FORMATS,
    SOURCES,
    SOURCE_TICKETLESS,
    TargetActivationOutcome,
    TicketlessAnchor,
    TicketlessConsultationAnchor,
    TicketlessWorkIntakeAnchor,
    build_delivery_record,
    build_execution_root,
    build_inactive_pane_fallback_command,
    build_marker,
    build_notification_body,
    evaluate_standard_target_admission,
    is_explicit_pane_target,
    main_lane_implementation_request_blocked,
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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
    submit_record_lines,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.workspace_registry import (
    resolve_canonical_session,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    AGENT_COMMANDS,
    AGENT_LABELS,
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
    REPO_ROOT_MARKERS,
    default_queue_path,
    default_tmux_conf,
    find_repo_root,
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
# / ``cmd_keys`` low-level pane/debug primitives were carved into an OOP-first
# command boundary (Redmine #12932 / #12638): a ``PanePrimitiveOps`` port over
# every tmux / pane / read-marker primitive, a ``LivePanePrimitiveOps`` adapter
# that routes those primitives through this module's globals at call time (so the
# existing ``commands.*`` monkeypatch seams are unchanged), a ``PanePrimitiveUseCase``
# owning the six behavior-preserving flows, and a ``PanePrimitiveOutcome`` value
# object. The ``cmd_*`` handlers below stay thin composition roots (kept as
# module-level names so the cli_core / cli / cli_handoff parser bindings and the
# ``rollback_unsubmitted_input`` / notify internal callers that reference them —
# and the tests that patch ``commands.cmd_read`` / ``.cmd_message`` / ``.cmd_keys``
# — are unchanged). The ``_emit_message_gate_guidance`` trailer moved with the
# family to ``pane_primitive_command.py`` (routed via the port); the sibling
# ``_emit_handoff_marker_timeout_guidance`` below stays here (its caller is the
# out-of-scope ``orchestrate_handoff`` strict rail).


def _emit_pane_primitive_outcome(outcome: PanePrimitiveOutcome) -> int:
    if outcome.stdout is not None:
        print(outcome.stdout, end=outcome.stdout_end)
    return outcome.exit_code


def cmd_id(_: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).id(_))


def cmd_resolve(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).resolve(args))


def cmd_read(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).read(args))


def cmd_type(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).type_text(args))


def _emit_handoff_marker_timeout_guidance(receiver: str) -> None:
    """Print the stderr trailer after a strict-rail `handoff send` marker_timeout.

    Required by Asana task 1214779823377861 to keep agents from collapsing a
    single transient ``marker_timeout`` into the preset's ``Notification
    fails`` branch. The structured outcome and durable record already enumerate
    the fallback path; this trailer surfaces it on the failure stream so the
    agent sees it even when the durable record is consumed by a downstream
    process and not re-read.
    """
    cap = NO_SUBMIT_RETRY_BUDGET
    print(
        f"hint: fallback path: `mozyo-bridge read {receiver}` then "
        f"`mozyo-bridge message {receiver} \"<resubmit text>\" --no-submit "
        f"--attempt <N>` (up to {cap} attempts per preset contract; track "
        "remaining with `--attempt N`).",
        file=sys.stderr,
    )
    print(
        "hint: --no-submit retry budget and the `mozyo-bridge handoff send` "
        "retry pool are separate budgets; do not borrow attempts across them.",
        file=sys.stderr,
    )
    print(
        f"hint: only after the {cap}-attempt --no-submit budget is exhausted "
        "AND the last gate error lacks a literal next-action verb (`read "
        "target again`, `retry`, `refresh`) may the preset's `Notification "
        "fails` branch fire. Record every attempted command and observed "
        "error verbatim in the durable record before escalating.",
        file=sys.stderr,
    )


def cmd_message(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).message(args))


def cmd_keys(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).keys(args))


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


# Redmine #11857 / #11925: reproducible permission mode for managed Claude
# panes. Operators kept forgetting to Shift+Tab cockpit / sublane Claude
# panes into auto mode, which stalled multi-sublane dogfooding. The launch
# command appends `--permission-mode <mode>` at every managed-pane
# chokepoint (cockpit, layout, sublane, standalone agent windows). Cockpit
# / sublane creation passes a launch-context policy default of `auto`
# (#11925) so future managed Claude panes are reproducibly auto without an
# env var; the standalone `mozyo` window path passes no default, so its
# historical bare `claude` launch never changes silently. The env var
# `MOZYO_CLAUDE_PERMISSION_MODE` remains the compatibility / explicit
# override rail and wins when set. The flag is Claude-only — Codex launches
# are untouched. A CLI `--permission-mode` flag overrides settings.json's
# permissions.defaultMode for that one session only; it neither reads nor
# writes any user / project settings file, so it cannot conflict with
# local on-disk settings, and it is non-retroactive (already-running panes
# keep their mode). Resolution lives in the pure policy module so `doctor`
# introspects the same precedence.


def _claude_permission_mode_flag(
    agent: str, *, policy_default: str | None = None
) -> str:
    """`--permission-mode <mode>` suffix for managed Claude panes, or ``""``.

    Delegates to the pure policy resolver (env override > launch-context
    policy default > none) and turns an invalid value into a hard CLI error
    so a typo cannot silently fall back to a default-permission pane the
    operator did not intend.
    """
    try:
        return permission_mode_flag(agent, policy_default=policy_default)
    except InvalidPermissionMode as exc:
        die(str(exc))


def _agent_launch_command(
    agent: str,
    session: str,
    cwd: str | None,
    *,
    permission_mode_default: str | None = None,
) -> str:
    """The shell command tmux runs for a new agent pane, with OTel env.

    ``permission_mode_default`` is the launch-context policy default for the
    Claude permission mode (cockpit / sublane pass ``auto``; the standalone
    path passes ``None`` to preserve the historical bare ``claude`` launch).
    The ``MOZYO_CLAUDE_PERMISSION_MODE`` env var still overrides it.
    """
    import shlex

    env_pairs = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(otel_bootstrap_env(agent, session, cwd).items())
    )
    return (
        f"env {env_pairs} {AGENT_COMMANDS[agent]}"
        f"{_claude_permission_mode_flag(agent, policy_default=permission_mode_default)}"
    )


def _record_managed_pane_created(
    agent: str, session: str, pane_id: str, cwd: str | None
) -> None:
    """Append a desired-state ``created`` event at the pane-creation boundary.

    Redmine #11726: this is the one mozyo command boundary that *creates*
    a managed pane, so it is where the desired-state event log records the
    intent (what mozyo built, in which session, for which agent). Strictly
    best-effort — any failure is swallowed (``record_managed_event``
    returns None) so the desired-state log can never break session
    creation, exactly like the OTel/telemetry posture. It records intent
    only; it does not read or write liveness, handoff target resolution,
    or preflight, which stay live-tmux-authoritative (#11698 invariant).
    The pane also gets the secondary ``@mozyo_managed`` runtime marker, so
    a running managed pane is classifiable even before registry
    registration.
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.managed_marker import mark_target
        from mozyo_bridge.managed_events import (
            KIND_CREATED,
            record_managed_event,
        )

        workspace_id = None
        if cwd:
            workspace_id = resolve_canonical_session(cwd).workspace_id
        # repo_root is NFD-normalized inside record_managed_event (#11625).
        record_managed_event(
            command="mozyo",
            event_kind=KIND_CREATED,
            pane_id=pane_id,
            mozyo_session=session,
            workspace_id=workspace_id,
            repo_root=cwd,
            intent={"agent": agent, "window": agent},
        )
        # Secondary runtime marker; primary managed signal is the registry
        # anchor. Non-fatal — a marker failure must not fail creation.
        mark_target(pane_id)
    except Exception:
        # Whole boundary is best-effort: desired-state recording must never
        # break the session/pane the operator asked mozyo to create.
        pass


def new_agent_session_window(agent: str, session: str, cwd: str | None = None) -> str:
    require_tmux()
    if agent not in AGENT_COMMANDS:
        die(f"unsupported agent: {agent}")
    args = ["new-session", "-d", "-s", session, "-n", agent, "-P", "-F", "#{pane_id}"]
    if cwd:
        args.extend(["-c", cwd])
    args.append(_agent_launch_command(agent, session, cwd))
    result = run_tmux(*args, check=False)
    if result.returncode != 0:
        die(f"tmux new-session failed: {result.stderr.strip() or result.stdout.strip()}")
    pane_id = result.stdout.strip()
    if not pane_id:
        die("tmux new-session did not return a pane id")
    _record_managed_pane_created(agent, session, pane_id, cwd)
    return pane_id


def new_agent_window(agent: str, session: str, cwd: str | None = None) -> str:
    require_tmux()
    if agent not in AGENT_COMMANDS:
        die(f"unsupported agent: {agent}")
    args = ["new-window", "-d", "-t", f"{session}:", "-n", agent, "-P", "-F", "#{pane_id}"]
    if cwd:
        args.extend(["-c", cwd])
    args.append(_agent_launch_command(agent, session, cwd))
    result = run_tmux(*args, check=False)
    if result.returncode != 0:
        die(f"tmux new-window failed: {result.stderr.strip() or result.stdout.strip()}")
    pane_id = result.stdout.strip()
    if not pane_id:
        die("tmux new-window did not return a pane id")
    _record_managed_pane_created(agent, session, pane_id, cwd)
    return pane_id


def list_session_windows(session: str) -> list[str]:
    result = run_tmux("list-windows", "-t", session, "-F", "#{window_name}", check=False)
    if result.returncode != 0:
        return []
    return [name.strip() for name in result.stdout.splitlines() if name.strip()]


def wait_for_agent_terminal_pane(pane_id: str, agent: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = pane_info(pane_id)
        command = Path(info.get("command") or "").name
        if is_agent_process(command):
            return
        time.sleep(0.2)
    die(f"timed out waiting for {agent} pane startup: {pane_id}")


_WRAP_INDENT = re.compile(r"\n\s+")


def wait_for_text(target: str, text: str, lines: int, timeout: float) -> bool:
    # Receiver TUIs (codex CLI, Claude Code) wrap long input at the visible
    # pane width, emitting a literal newline + continuation indent inside
    # the captured text. tmux capture-pane -J only rejoins lines tmux itself
    # wrapped, so a raw substring search would miss a marker split by the
    # TUI wrap even though it landed cleanly on the wire.
    #
    # Two wrap shapes are observed in practice and require different
    # normalize functions:
    #   1. word-boundary wrap (`mozyo-bridge message` markers like
    #      `[mozyo-bridge from:claude pane:%110 at:mozyo_bridge:2.0]`,
    #      ~60 chars, contain whitespace) — TUI wraps at a space, so
    #      collapsing `\n\s+` into a single ` ` reconstructs the original.
    #   2. character-wrap (`mozyo-bridge handoff` markers like
    #      `[mozyo:handoff:source=asana:task=...:comment=...:kind=...:to=...]`,
    #      100+ chars, contain no whitespace) — TUI wraps at an arbitrary
    #      character boundary, so the only normalize that reconstructs the
    #      original is collapsing `\n\s+` to the empty string.
    # Try the raw match first (cheap, scrollback-safe), then both wrap
    # normalizes before declaring the marker absent. All three paths still
    # return False when the marker is genuinely missing, preserving the
    # fail-closed rollback contract.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _marker_visible_in(capture_pane(target, lines), text):
            return True
        time.sleep(0.2)
    return False


def _marker_visible_in(captured: str, text: str) -> bool:
    """One-shot check whether ``text`` is visible in ``captured`` pane text.

    Mirrors the three wrap normalizations :func:`wait_for_text` polls with: raw
    substring, word-boundary wrap (``\\n\\s+`` -> ``" "``), and character-wrap
    (``\\n\\s+`` -> ``""``). Shared so the queue-enter Enter-only retry can probe
    for the marker once per interval without re-implementing the wrap handling
    or paying a second polling timeout.
    """
    if text in captured:
        return True
    if text in _WRAP_INDENT.sub(" ", captured):
        return True
    if text in _WRAP_INDENT.sub("", captured):
        return True
    return False


def rollback_unsubmitted_input(target: str) -> None:
    cmd_keys(argparse.Namespace(target=target, keys=["C-u"]))


def ensure_repo_session_windows(args: argparse.Namespace) -> list[str]:
    """Ensure `args.session` exists with one window per agent (claude, codex).

    Each agent runs in its own tmux window in a single repo-scoped session.
    The window-model guarantee is gated on tmux window names; missing agent
    windows are created. Pre-existing non-agent windows (zsh, custom names)
    are left untouched and stay reachable through their indices — they just
    are not agent targets. Returns the list of newly created
    ``agent:pane_id`` entries.
    """
    require_tmux()
    config_loaded = False
    if args.config and session_exists(args.session):
        load_tmux_conf_for(args)
        config_loaded = True
    created: list[str] = []
    if not session_exists(args.session):
        claude_pane = new_agent_session_window("claude", args.session, cwd=args.cwd)
        created.append(f"claude:{claude_pane}")
    if args.config and not config_loaded:
        load_tmux_conf_for(args)
    windows = list_session_windows(args.session)
    for agent in ("claude", "codex"):
        if agent in windows:
            continue
        pane_id = new_agent_window(agent, args.session, cwd=args.cwd)
        created.append(f"{agent}:{pane_id}")
    for agent in ("claude", "codex"):
        pane = find_agent_window(agent, args.session)
        if pane:
            ensure_agent_target(pane, agent, force=args.force)
            if args.ready_timeout:
                wait_for_agent_terminal_pane(pane["id"], agent, args.ready_timeout)
            # Apply the subtle per-window status-bar tint after the window
            # exists, the user's `.tmux.conf` has been sourced (above), and
            # the agent pane is settled. Window-scoped — only the agent
            # windows we manage are tinted; legacy windows in the same
            # session stay at the user's global style.
            apply_window_subtle_style(args.session, agent)
    return created


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
    """
    require_tmux()
    repo_root = repo_root_from_args(args)
    derived = resolve_canonical_session(repo_root).name
    if not derived:
        die("could not derive a session name from repo root; cd into a project directory or pass a subcommand explicitly")
    user_session = getattr(args, "session", None)
    session = user_session or derived
    cwd = getattr(args, "cwd", None) or str(repo_root)
    if not user_session and session_exists(session):
        offending = session_cwd_mismatch(session, repo_root)
        if offending:
            die(
                f"session '{session}' already exists but its panes are outside repo root "
                f"{repo_root} (cwds: {', '.join(offending)}). "
                "Re-run from the matching repo root, or pass an explicit `--session NAME` "
                "to bare `mozyo` to disambiguate."
            )
    json_output = bool(getattr(args, "json_output", False))
    notice = None
    if not user_session:
        notice = legacy_basename_session_notice(repo_root, session)
        if notice and not json_output:
            print(notice)
    config_path = getattr(args, "config_path", None)
    config_path_was_default = config_path is None
    resolved_config_path = config_path or str(default_tmux_conf(repo_root))
    setup_args = argparse.Namespace(
        session=session,
        cwd=cwd,
        config=True,
        config_path=resolved_config_path,
        config_path_was_default=config_path_was_default,
        ready_timeout=float(getattr(args, "ready_timeout", 10.0) or 0.0),
        force=bool(getattr(args, "force", False)),
    )
    created = ensure_repo_session_windows(setup_args)
    select = run_tmux("select-window", "-t", f"{session}:claude", check=False)
    if select.returncode != 0:
        die(
            f"failed to select `claude` window in session '{session}'. "
            "The window-model guarantee did not hold. "
            f"stderr={select.stderr.strip() or select.stdout.strip()}"
        )
    result = run_tmux(
        "list-windows",
        "-t",
        session,
        "-F",
        "#{window_index}\t#{window_name}\t#{pane_current_command}",
        check=False,
    )
    # iTerm2 control mode (Redmine #11729): `--cc` swaps the plain
    # `tmux attach` for `tmux -CC attach` so iTerm2 drives tmux windows as
    # native windows/panes. It only changes the *attach* form — session
    # derivation, the legacy-collision guard, env injection, and the
    # claude/codex window ensure above are all unchanged. `--no-attach` and
    # `--json` still win (ensure only, never exec); the printed / JSON attach
    # command just reflects the `-CC` variant so a launcher copies the right
    # command.
    control_mode = bool(getattr(args, "cc", False))
    attach_command = (
        f"tmux -CC attach -t {session}"
        if control_mode
        else f"tmux attach -t {session}"
    )
    if json_output:
        windows = _parse_mozyo_window_rows(result.stdout if result.returncode == 0 else "")
        present = {window["name"] for window in windows}
        # `--json` always returns without attaching (it never reaches the
        # `os.execvp` below), so the effective no-attach behavior is true even
        # when the raw `--no-attach` flag was not passed. Report the effective
        # value here — a launcher parsing this schema cares about what actually
        # happened, not the raw flag (Redmine #11313 review #54111).
        no_attach_effective = bool(getattr(args, "no_attach", False)) or json_output
        payload = {
            "session": session,
            "repo_root": str(repo_root),
            "cwd": cwd,
            "created": list(created),
            "windows": windows,
            "ready": AGENT_LABELS.issubset(present),
            "attach": attach_command,
            "attach_target": session,
            "attached": False,
            "control_mode": control_mode,
            "no_attach": no_attach_effective,
            "legacy_session_notice": notice,
        }
        import json as _json

        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"session={session} created={','.join(created) if created else '-'}")
    print("INDEX\tNAME\tPROCESS")
    if result.returncode == 0:
        print(result.stdout, end="")
    if getattr(args, "no_attach", False):
        print(f"attach: {attach_command}")
        return 0
    if control_mode:
        os.execvp("tmux", ["tmux", "-CC", "attach", "-t", session])
    os.execvp("tmux", ["tmux", "attach", "-t", session])
    raise AssertionError("unreachable")


def _resolve_project_scope_fields(
    cwd: str | None, repo_root: str | None
) -> tuple[str | None, tuple[str | None, str | None, str | None], str | None]:
    """Resolve a cockpit column's Git repo root + project scope (Redmine #12658).

    When ``cwd`` resolves inside an adopted monorepo project, the workspace
    identity is the *Git repository root* (the umbrella/department workspace) and
    the project scope (the project's ``redmine_project`` id) is carried
    separately — so the cockpit / dry-run JSON keeps ``repo_root`` and the project
    path distinct. Returns ``(effective_repo_root, (scope, path, label),
    launch_cwd)`` where ``launch_cwd`` is the absolute project workdir a launched
    pane should start in (Redmine #12658 j#66505) so its cwd is under the project
    path and a ``--target-project`` handoff gate can pass.

    Fail-soft and compatibility-preserving: when no adopted project contains the
    cwd (a single-repo workspace, an un-scanned root, or any discovery error) the
    original ``repo_root`` is returned unchanged with an empty project triple and
    a ``None`` launch_cwd, so existing single-repo cockpit behavior is identical.
    """
    none_triple: tuple[str | None, str | None, str | None] = (None, None, None)
    if not cwd:
        return repo_root, none_triple, None
    try:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            project_scope_for_cwd,
            resolve_workspace_root,
        )

        # Prefer the real Git worktree root over a nested project-local scaffold
        # marker (Redmine #12658 j#66499): a monorepo project subdir may carry its
        # own `.mozyo-bridge/scaffold.json`, at which `infer_repo_root` would stop
        # and collapse the workspace onto the project. The Git root is the
        # workspace; the scaffold marker is the fallback only when there is no Git
        # root above (non-git scaffolded workspace, #11301).
        git_root = resolve_workspace_root(cwd) or repo_root
        if not git_root:
            return repo_root, none_triple, None
        scope = project_scope_for_cwd(cwd, git_root)
    except Exception:  # noqa: BLE001 - project scope is additive; never block cockpit
        return repo_root, none_triple, None
    if scope is None:
        return repo_root, none_triple, None
    # Launch the pane at the project workdir (repo-relative ``scope.workdir``
    # resolved against the Git root) so the pane cwd is under the project path.
    launch_cwd = str(Path(git_root) / scope.workdir)
    return git_root, (scope.scope, scope.path, scope.label), launch_cwd


def _resolve_cockpit_workspaces(args: argparse.Namespace) -> list:
    """Resolve the active workspaces to summon into the cockpit (Redmine #11788).

    Explicit ``--repo`` columns win (deterministic order). Otherwise the active
    mozyo workspaces are discovered from the live session inventory — one column
    per distinct workspace that currently carries a codex/claude agent pane.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import CockpitWorkspace, normalize_lane

    repos = getattr(args, "layout_repos", None)
    out: list = []
    if repos:
        for repo in repos:
            resolved = str(Path(repo).expanduser().resolve())
            # Project-scoped identity (#12658): re-root to the Git repo root and
            # carry the project scope separately when this column is an adopted
            # monorepo project; single-repo columns are unchanged.
            effective_root, (p_scope, p_path, p_label), p_launch = (
                _resolve_project_scope_fields(resolved, resolved)
            )
            canon = resolve_canonical_session(effective_root)
            wsid = getattr(canon, "workspace_id", None) or canon.name
            lane = _resolve_workspace_lane(effective_root, getattr(canon, "workspace_id", None))
            out.append(
                CockpitWorkspace(
                    workspace_id=wsid,
                    label=canon.name,
                    repo_root=effective_root,
                    lane_id=lane.lane_id,
                    lane_label=lane.lane_label,
                    project_scope=p_scope,
                    project_path=p_path,
                    project_label=p_label,
                    launch_cwd=p_launch,
                )
            )
        return out

    from mozyo_bridge.session_inventory import take_inventory

    snapshot = take_inventory()
    # One column per distinct workspace+lane (Redmine #11820). Keying by
    # `workspace_id` alone would collapse same-workspace-different-lane checkouts
    # (e.g. a main worktree and a linked worktree) into a single column, which
    # contradicts the append-as-separate-column contract this US adds — so the
    # dedupe key carries the normalized lane id too.
    by_lane: dict[tuple, object] = {}
    for rec in snapshot.records:
        if rec.agent_kind not in (AGENT_KIND_CODEX, AGENT_KIND_CLAUDE):
            continue
        wsid = (
            (rec.workspace.workspace_id if rec.workspace else None)
            or rec.repo_root
            or rec.session
        )
        lane = _resolve_workspace_lane(
            rec.repo_root or "",
            rec.workspace.workspace_id if rec.workspace else None,
        )
        # Project-scoped identity (#12658): a discovered pane carries its own cwd,
        # so resolve the project scope from it; the Git repo_root is kept as the
        # workspace authority.
        _eff_root, (p_scope, p_path, p_label), p_launch = _resolve_project_scope_fields(
            rec.cwd, rec.repo_root
        )
        key = (wsid, normalize_lane(lane.lane_id), p_scope or "")
        if key not in by_lane:
            by_lane[key] = CockpitWorkspace(
                workspace_id=wsid,
                label=rec.session,
                repo_root=rec.repo_root,
                lane_id=lane.lane_id,
                lane_label=lane.lane_label,
                project_scope=p_scope,
                project_path=p_path,
                project_label=p_label,
                launch_cwd=p_launch,
            )
    return list(by_lane.values())


def execute_cockpit_plan(plan, run, *, cleanup_captured: bool = False) -> dict:
    """Run a :class:`CockpitPlan`'s tmux commands, resolving logical tokens.

    ``run`` is a ``run_tmux``-style callable. Each command's logical pane tokens
    (``@colN_role``) are substituted with the real ``%pane`` id captured from an
    earlier ``-P -F '#{pane_id}'`` command. Returns the token -> pane id map.

    Fail-fast (Redmine #11788 review): a tmux step that exits non-zero, or a
    capturing step that does not return a ``%pane`` id, is fatal. Continuing
    would run later steps against an empty / wrong target and present a broken
    half-built layout as if it succeeded, so the layout — whose source of truth
    is tmux state — must abort instead.

    ``cleanup_captured`` (Redmine #11803 review): when appending into an
    existing shared cockpit the session must not be killed, so a mid-append
    failure would otherwise orphan the new panes already created. With this
    flag, every pane captured so far is ``kill-pane``'d (best-effort) before
    aborting, leaving the shared cockpit's other columns intact.
    """
    ids: dict[str, str] = {}

    def _abort(message: str):
        if cleanup_captured:
            for pane_id in ids.values():
                run("kill-pane", "-t", pane_id, check=False)
        die(message)

    for cmd in plan.commands:
        argv = [ids.get(token, token) for token in cmd.argv]
        result = run(*argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            detail = (getattr(result, "stderr", "") or "").strip() or (
                getattr(result, "stdout", "") or ""
            ).strip()
            _abort(
                f"cockpit layout step failed ({cmd.purpose}): "
                f"`tmux {' '.join(argv)}` -> {detail or 'nonzero exit'}"
            )
        if cmd.captures:
            pane_id = (getattr(result, "stdout", "") or "").strip()
            if not pane_id.startswith("%"):
                _abort(
                    f"cockpit layout step did not return a pane id "
                    f"({cmd.purpose}): got {pane_id!r}"
                )
            ids[cmd.captures] = pane_id
    return ids


def execute_cockpit_adopt_plan(plan, run) -> dict:
    """Run a :class:`CockpitAdoptPlan`, atomically, with best-effort rollback (#11898).

    The two ``join_commands`` move the live codex/claude panes and are treated as
    a transaction: if the first join (codex) lands but a later join fails, the
    codex pane is **moved back** beside the still-present source claude pane —
    never ``kill-pane``'d, because it carries a live agent (this is the crucial
    difference from :func:`execute_cockpit_plan`'s ``cleanup_captured``, which
    kills freshly-*created* panes). ``stamp_commands`` re-apply identity after
    both joins succeed and are best-effort: a stamp failure leaves the pair
    adopted and is reported as a warning, not rolled back. Returns
    ``{"stamp_warnings": [...]}``.
    """

    def _detail(result) -> str:
        return (getattr(result, "stderr", "") or "").strip() or (
            getattr(result, "stdout", "") or ""
        ).strip()

    def _rollback(joined_codex: bool) -> str | None:
        # Only the codex pane can be mid-adopted: it joins first, so if a later
        # step fails it is alone in the cockpit while the claude pane is still in
        # the source session. Move it back beside that source pane. Best-effort —
        # a failed rollback leaves the live codex pane in the cockpit (reported),
        # never killed.
        if not joined_codex:
            return None
        result = run(
            "join-pane", "-h", "-s", plan.source_codex_pane,
            "-t", plan.source_claude_pane, check=False,
        )
        if getattr(result, "returncode", 0) != 0:
            return (
                f"rollback failed: codex pane {plan.source_codex_pane} could not "
                f"be moved back to source session {plan.source_session!r} "
                f"({_detail(result) or 'nonzero exit'}); it is now live in the "
                f"cockpit — move it manually, it was NOT killed."
            )
        return None

    joined_codex = False
    for cmd in plan.join_commands:
        result = run(*cmd.argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            rollback_note = _rollback(joined_codex)
            message = (
                f"cockpit adopt step failed ({cmd.purpose}): "
                f"`tmux {' '.join(cmd.argv)}` -> {_detail(result) or 'nonzero exit'}"
            )
            if rollback_note:
                message += f"\n{rollback_note}"
            elif joined_codex:
                message += (
                    f"\nrolled back: codex pane {plan.source_codex_pane} moved "
                    f"back to source session {plan.source_session!r}."
                )
            die(message)
        joined_codex = True

    # Both joins landed — the pair is adopted. Identity re-stamp is best-effort.
    stamp_warnings: list[str] = []
    for cmd in plan.stamp_commands:
        result = run(*cmd.argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            stamp_warnings.append(
                f"{cmd.purpose}: {_detail(result) or 'nonzero exit'}"
            )
    return {"stamp_warnings": stamp_warnings}


def execute_peer_adopt_plan(plan, run) -> None:
    """Run a :class:`PeerAdoptPlan`'s identity binds, fail-closed (Redmine #12133).

    Peer adopt only ``set-option`` (+ ``select-pane -T``) binds the role-less
    candidate pane — there is no pane move / kill / split, so the pane and any
    agent in it are untouched. The binds are treated as a small transaction: if a
    later bind fails after earlier ones landed, the earlier identity options are
    **unset** (best-effort) so the pane returns to its pre-adopt role-less state
    rather than being left half-bound (the very #12130 drift this repairs). Any
    failure raises via :func:`die`; a clean run returns ``None``.
    """

    def _detail(result) -> str:
        return (getattr(result, "stderr", "") or "").strip() or (
            getattr(result, "stdout", "") or ""
        ).strip()

    # Track the identity options we successfully set so a mid-sequence failure can
    # roll them back. The title (`select-pane -T`) is cosmetic and not rolled back.
    set_options: list[str] = []
    for cmd in plan.stamp_commands:
        result = run(*cmd.argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            for option in reversed(set_options):
                run("set-option", "-p", "-u", "-t", plan.pane_id, option, check=False)
            rolled = (
                f" rolled back {len(set_options)} identity option(s) on "
                f"{plan.pane_id} to restore its role-less state."
                if set_options
                else ""
            )
            die(
                f"cockpit peer-adopt step failed ({cmd.purpose}): "
                f"`tmux {' '.join(cmd.argv)}` -> {_detail(result) or 'nonzero exit'}."
                f"{rolled}"
            )
        if cmd.argv[:1] == ("set-option",):
            # argv is ("set-option", "-p", "-t", pane, OPTION, value).
            set_options.append(cmd.argv[4])


def execute_cockpit_reset_plan(plan, run) -> None:
    """Run a :class:`CockpitResetPlan`'s ``kill-session`` (#11814), fail-fast.

    ``run`` is a ``run_tmux``-style callable. The plan is built only after the
    target was graded mozyo-managed, so this just executes the destructive
    teardown and aborts (``die``) on a non-zero tmux exit rather than reporting a
    half-killed session as success.
    """
    for cmd in plan.commands:
        result = run(*cmd.argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            detail = (getattr(result, "stderr", "") or "").strip() or (
                getattr(result, "stdout", "") or ""
            ).strip()
            die(
                f"cockpit reset step failed ({cmd.purpose}): "
                f"`tmux {' '.join(cmd.argv)}` -> {detail or 'nonzero exit'}"
            )


def execute_cockpit_rebalance_plan(plan, run) -> None:
    """Run a :class:`CockpitRebalancePlan`'s ``resize-pane`` commands (#12135), fail-fast.

    ``run`` is a ``run_tmux``-style callable. The plan already targets real
    ``%pane`` ids (no token resolution needed) and touches no identity option, so
    this just runs each ``resize-pane -x`` in left-to-right order and aborts
    (``die``) on a non-zero tmux exit rather than reporting a half-rebalanced
    layout as success.
    """
    for cmd in plan.commands:
        result = run(*cmd.argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            detail = (getattr(result, "stderr", "") or "").strip() or (
                getattr(result, "stdout", "") or ""
            ).strip()
            die(
                f"cockpit rebalance step failed ({cmd.purpose}): "
                f"`tmux {' '.join(cmd.argv)}` -> {detail or 'nonzero exit'}"
            )


def execute_cockpit_reconcile_plan(plan, run) -> None:
    """Run a :class:`CockpitReconcilePlan`'s swap + relayout commands (#12136), fail-fast.

    ``run`` is a ``run_tmux``-style callable. The plan's ``swap-pane`` commands
    reorder the live panes, then the single ``select-layout`` applies the per-Unit
    columns. No command kills a pane — `swap-pane` / `select-layout` only move /
    relayout live panes (identity rides with them). A non-zero tmux exit aborts
    (``die``); because nothing is killed, a partial reorder is recoverable by
    re-running reconcile (it re-sorts from the live order).
    """
    for cmd in plan.commands:
        result = run(*cmd.argv, check=False)
        if getattr(result, "returncode", 0) != 0:
            detail = (getattr(result, "stderr", "") or "").strip() or (
                getattr(result, "stdout", "") or ""
            ).strip()
            die(
                f"cockpit reconcile step failed ({cmd.purpose}): "
                f"`tmux {' '.join(cmd.argv)}` -> {detail or 'nonzero exit'}\n"
                f"No pane was killed; re-run `mozyo cockpit reconcile` to continue "
                f"from the current live layout."
            )


def cmd_layout_apply(args: argparse.Namespace) -> int:
    """`mozyo layout apply cockpit` — build/focus the cockpit layout (#11788).

    Active workspaces become horizontal columns; within each column the agents
    are a vertical split (Codex top, Claude bottom) at `--ratio`. tmux state is
    the layout's source of truth; `--cc` only swaps the attach for control mode.
    `--json` / `--dry-run` emit the planned tmux commands without touching tmux.
    """
    import shlex as _shlex

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        COCKPIT_SESSION_DEFAULT,
        build_cockpit_plan,
    )

    preset = getattr(args, "preset", "cockpit")
    if preset != "cockpit":
        die(f"unsupported layout preset: {preset!r}")
    session = getattr(args, "cockpit_session", None) or COCKPIT_SESSION_DEFAULT
    codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)

    workspaces = _resolve_cockpit_workspaces(args)
    if not workspaces:
        die(
            "no active workspace to summon into the cockpit. Pass explicit "
            "`--repo <root>` columns, or start at least one mozyo session "
            "(`mozyo`) so the inventory has a codex/claude pane to discover."
        )

    def launch(role: str, ws) -> str:
        # Cockpit managed Claude panes launch auto reproducibly (#11925);
        # env var still overrides. Codex is unaffected (Claude-only flag).
        return _agent_launch_command(
            role,
            session,
            ws.repo_root,
            permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
        )

    plan = build_cockpit_plan(
        workspaces, codex_ratio=codex_ratio, session=session, launch=launch
    )

    json_output = bool(getattr(args, "json_output", False))
    dry_run = bool(getattr(args, "dry_run", False))
    control_mode = bool(getattr(args, "cc", False))
    no_attach = bool(getattr(args, "no_attach", False))
    attach_command = (
        f"tmux -CC attach -t {session}"
        if control_mode
        else f"tmux attach -t {session}"
    )

    if json_output:
        import json as _json

        payload = plan.as_dict()
        payload["attach"] = attach_command
        payload["control_mode"] = control_mode
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if dry_run:
        print(
            f"cockpit plan: session={session} columns={plan.columns} "
            f"codex={plan.codex_ratio}% claude={plan.claude_ratio}%"
        )
        for cmd in plan.commands:
            rendered = " ".join(_shlex.quote(token) for token in cmd.argv)
            print(f"  tmux {rendered}")
        print(f"attach: {attach_command}")
        return 0

    require_tmux()
    # Reuse over duplication (Redmine #11788): when the cockpit session already
    # exists, focus/attach it instead of rebuilding a second copy of the panes.
    if session_exists(session):
        print(
            f"cockpit session {session!r} already exists; attaching without "
            "rebuild (reuse over duplicate panes)"
        )
    else:
        try:
            execute_cockpit_plan(plan, run_tmux)
        except SystemExit:
            # A layout step failed mid-build (Redmine #11788 review). Tear down
            # the partial cockpit session best-effort so a retry rebuilds
            # cleanly instead of the reuse path adopting a broken half layout.
            run_tmux("kill-session", "-t", session, check=False)
            raise
        print(
            f"cockpit built: session={session} columns={plan.columns} "
            f"codex={plan.codex_ratio}% claude={plan.claude_ratio}%"
        )
    if no_attach:
        print(f"attach: {attach_command}")
        return 0
    if control_mode:
        os.execvp("tmux", ["tmux", "-CC", "attach", "-t", session])
    os.execvp("tmux", ["tmux", "attach", "-t", session])
    raise AssertionError("unreachable")


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
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import COCKPIT_WINDOW

    target_window = COCKPIT_WINDOW if window is None else window
    # A window id (`@N`) is unique across the whole tmux server, so it targets the
    # window on its own; only a window *name* needs the `session:` qualifier.
    target = (
        target_window
        if target_window.startswith("@")
        else f"{session}:{target_window}"
    )

    def _as_int(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # Read-only and tolerant: a missing tmux binary / server, or a missing
    # cockpit window, all degrade to "no cockpit" (None) rather than raising —
    # so `--dry-run` / `--json` stay non-mutating and never abort (#11803 review).
    try:
        result = run_tmux(
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_id}\t#{@mozyo_workspace_id}\t#{@mozyo_agent_role}"
            "\t#{@mozyo_lane_id}\t#{pane_left}\t#{pane_width}"
            "\t#{@mozyo_project_scope}\t#{@mozyo_project_path}"
            "\t#{@mozyo_project_label}",
            check=False,
        )
    except (Exception, SystemExit):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    columns = []
    for line in (getattr(result, "stdout", "") or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0]:
            columns.append(
                {
                    "pane_id": parts[0],
                    "workspace_id": parts[1],
                    "role": parts[2],
                    "lane_id": parts[3] if len(parts) >= 4 else "",
                    "pane_left": _as_int(parts[4]) if len(parts) >= 5 else 0,
                    "pane_width": _as_int(parts[5]) if len(parts) >= 6 else 0,
                    "project_scope": parts[6] if len(parts) >= 7 else "",
                    "project_path": parts[7] if len(parts) >= 8 else "",
                    "project_label": parts[8] if len(parts) >= 9 else "",
                }
            )
    return columns


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
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import GROUP_WINDOW_OPTION

    try:
        result = run_tmux(
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_id}\t#{window_name}\t#{" + GROUP_WINDOW_OPTION + "}",
            check=False,
        )
    except (Exception, SystemExit):
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    managed = []
    for line in (getattr(result, "stdout", "") or "").splitlines():
        parts = line.split("\t")
        window_id = parts[0] if parts else ""
        if not window_id:
            continue
        window_name = parts[1] if len(parts) >= 2 else ""
        group_hint = parts[2] if len(parts) >= 3 else ""
        # Read panes by the unambiguous window id, never the (possibly duplicate)
        # name.
        columns = _read_cockpit_columns(session, window_id)
        if not columns:
            continue
        if any((c.get("workspace_id") or "") for c in columns):
            managed.append(
                {
                    "window_id": window_id,
                    "window": window_name,
                    "group_id": group_hint,
                    "columns": columns,
                }
            )
    return managed


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
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import COCKPIT_WINDOW

    def _as_int(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    try:
        result = run_tmux(
            "list-panes",
            "-t",
            f"{session}:{COCKPIT_WINDOW}",
            "-F",
            "#{pane_id}\t#{@mozyo_workspace_id}\t#{@mozyo_agent_role}"
            "\t#{@mozyo_lane_id}\t#{pane_left}\t#{pane_top}"
            "\t#{pane_width}\t#{pane_height}",
            check=False,
        )
    except (Exception, SystemExit):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    panes = []
    for line in (getattr(result, "stdout", "") or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        parts = (parts + [""] * 8)[:8]
        panes.append(
            {
                "pane_id": parts[0],
                "workspace_id": parts[1],
                "role": parts[2],
                "lane_id": parts[3],
                "pane_left": _as_int(parts[4]),
                "pane_top": _as_int(parts[5]),
                "pane_width": _as_int(parts[6]),
                "pane_height": _as_int(parts[7]),
            }
        )
    return panes


def _rightmost_codex_anchor(codex_columns) -> str | None:
    """The codex pane id of the visually rightmost cockpit column (#11849).

    `tmux list-panes` enumeration order is NOT layout order, so anchoring an
    append on the last-listed codex pane can split a middle column and crush the
    layout. Pick by geometry instead: the largest ``pane_left``, tie-broken by
    the right edge (``pane_left + pane_width``) then ``pane_id`` so it stays
    deterministic even when geometry is missing (defaults to 0 → a stable
    pane-id ordering).
    """
    if not codex_columns:
        return None

    def _key(col):
        left = col.get("pane_left") or 0
        width = col.get("pane_width") or 0
        return (left, left + width, col.get("pane_id") or "")

    return max(codex_columns, key=_key).get("pane_id")


def _cockpit_session_present(session: str) -> bool:
    """Tolerant `has-session` for the cockpit (#11803 review).

    Distinguishes "session absent" from "session present but cockpit window
    missing", so a real-run create never `new-session`s against (and the
    cleanup never kills) a pre-existing session. Tolerant: any tmux error
    degrades to ``False``.
    """
    try:
        return bool(session_exists(session))
    except (Exception, SystemExit):
        return False


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
    if not session:
        return (), True
    try:
        result = run_tmux(
            "list-clients", "-t", session, "-F", "#{client_tty}", check=False
        )
    except (Exception, SystemExit):
        return (), False
    if getattr(result, "returncode", 1) != 0:
        return (), False
    clients = tuple(
        line.strip()
        for line in (getattr(result, "stdout", "") or "").splitlines()
        if line.strip()
    )
    return clients, True


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
    try:
        present = bool(session_exists(source_session))
    except (Exception, SystemExit):
        present = False
    if not present:
        return (
            f"source session {source_session!r} is now empty and was closed by "
            f"tmux (both agent panes moved out); not killed explicitly."
        )
    remaining = "?"
    try:
        result = run_tmux(
            "list-panes", "-s", "-t", source_session, "-F", "#{pane_id}",
            check=False,
        )
        if getattr(result, "returncode", 1) == 0:
            remaining = str(
                len(
                    [
                        ln
                        for ln in (getattr(result, "stdout", "") or "").splitlines()
                        if ln.strip()
                    ]
                )
            )
    except (Exception, SystemExit):
        pass
    return (
        f"source session {source_session!r} still has {remaining} pane(s) and was "
        f"left intact (not killed)."
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

    Tolerant and read-only: it reuses the cross-workspace session inventory
    (`take_inventory`) and keeps only panes that are a *normal* `mozyo` agent for
    the adopt detector — a classified codex/claude pane whose role came from the
    window name (`role_source == window_name`), living outside the cockpit
    session. Cockpit panes carry the role on `@mozyo_agent_role`
    (`role_source == pane_option`) and are excluded so a cockpit column never
    looks like an adopt source. Any failure (no tmux, inventory error) degrades
    to ``[]`` so the advisory can never break the cockpit flow.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import NormalSessionObservation

    try:
        from mozyo_bridge.session_inventory import take_inventory

        snapshot = take_inventory(derive_unregistered=False, persist=False)
    except Exception:
        return []

    lane_cache: dict[str, object] = {}
    observations = []
    for rec in snapshot.records:
        if rec.agent_kind not in (AGENT_KIND_CODEX, AGENT_KIND_CLAUDE):
            continue
        if rec.session == cockpit_session:
            continue
        if rec.role_source != ROLE_SOURCE_WINDOW_NAME:
            continue
        # Mirror the target's identity fallback EXACTLY (Redmine #11897 review
        # j#57857): `cmd_cockpit` uses `canon.workspace_id or canon.name`, where
        # `canon.name` is the registry→anchor→derivation canonical session. The
        # inventory resolves identity through the *same* chain, so an unregistered
        # workspace (no registry row / anchor) has `workspace_id=None` but a
        # matching `canonical_session`. Falling back to the raw `repo_root` here
        # instead would never match that `canon.name` (detection silently fails)
        # and would also leak an absolute path as the match key. Prefer the
        # privacy-safe `canonical_session`; repo_root/session are last-resort only.
        workspace_id = (
            (rec.workspace.workspace_id if rec.workspace else None)
            or (rec.workspace.canonical_session if rec.workspace else None)
            or rec.repo_root
            or rec.session
        )
        repo_root = rec.repo_root or ""
        if repo_root not in lane_cache:
            lane_cache[repo_root] = _resolve_workspace_lane(
                repo_root, rec.workspace.workspace_id if rec.workspace else None
            )
        lane = lane_cache[repo_root]
        observations.append(
            NormalSessionObservation(
                session=rec.session,
                workspace_id=workspace_id,
                lane_id=lane.lane_id,
                role=rec.agent_kind,
                pane_id=rec.pane_id,
            )
        )
    return observations


def _cockpit_adopt_advisory(workspace, cockpit_session: str):
    """Detect a co-existing normal `mozyo` session for ``workspace`` (#11897).

    Wraps the pure :func:`detect_adopt_candidates` over the live inventory
    projection. Read-only and tolerant: it never moves a pane and always returns
    an :class:`AdoptAdvisory` (a benign ``none`` advisory when nothing is found
    or the inventory is unavailable).
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        AdoptAdvisory,
        ADOPT_STATUS_NONE,
        detect_adopt_candidates,
        normalize_lane,
    )

    lane_id = normalize_lane(workspace.lane_id)
    try:
        return detect_adopt_candidates(
            workspace_id=workspace.workspace_id,
            lane_id=workspace.lane_id,
            observations=_coexisting_normal_observations(cockpit_session),
            cockpit_session=cockpit_session,
        )
    except Exception:
        return AdoptAdvisory(
            workspace.workspace_id, lane_id, ADOPT_STATUS_NONE, (), None
        )


def _resolve_cockpit_adopt(
    workspace, session, *, columns, session_present, already_in_cockpit,
    existing_codex, advisory, codex_ratio=70,
):
    """Decide the adopt move for ``mozyo cockpit adopt`` — plan or fail-closed (#11898).

    Returns ``(plan, blocked_reason, source_clients)``. ``plan`` is a
    :class:`CockpitAdoptPlan` only when there is a single, unambiguous, fully
    paired adopt candidate, the cockpit already exists with a column to anchor
    on, and the source session has no attached client. Every fail-closed
    condition (already a column / not a clean candidate / role→pane ambiguous /
    no cockpit yet / attached client / no anchor) yields a ``blocked_reason`` and
    ``plan is None`` — the move is never planned past a closed gate.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        ADOPT_STATUS_CANDIDATE,
        adopt_pane_pair,
        build_cockpit_adopt_plan,
    )

    if already_in_cockpit:
        return None, (
            "this workspace+lane is already a cockpit column; focus it with "
            "`mozyo cockpit` — nothing to adopt."
        ), ()
    if advisory.status != ADOPT_STATUS_CANDIDATE:
        return None, (
            advisory.message
            or "no adoptable co-existing normal `mozyo` session for this "
            "workspace+lane."
        ), ()

    candidate = advisory.candidates[0]
    pair = adopt_pane_pair(candidate)
    if pair is None:
        return None, (
            f"adopt candidate {candidate.session!r} does not map exactly one "
            f"codex and one claude pane (roles={','.join(candidate.roles) or '-'}, "
            f"panes={','.join(candidate.pane_ids) or '-'}); the role→pane pairing "
            f"is ambiguous and fails closed."
        ), ()

    if not session_present or columns is None:
        return None, (
            f"cockpit session {session!r} does not exist yet (or has no cockpit "
            f"window); create it first with `mozyo cockpit` (or `mozyo layout "
            f"apply cockpit`), then re-run `mozyo cockpit adopt`. Bootstrapping a "
            f"cockpit from the moved panes is out of this Phase 2 scope."
        ), ()

    source_clients = _session_attached_clients(candidate.session)
    if source_clients:
        return None, (
            f"source session {candidate.session!r} has attached client(s) "
            f"({', '.join(source_clients)}); detach it before adopting so its "
            f"panes are not moved out from under a live client (fail-closed)."
        ), source_clients

    anchor = _rightmost_codex_anchor(existing_codex)
    if not anchor:
        return None, (
            f"cockpit session {session!r} exists but carries no mozyo-identified "
            f"codex column to anchor the adopted column beside; rebuild it with "
            f"`mozyo layout apply cockpit` or remove the stale session."
        ), ()

    codex_pane, claude_pane = pair
    plan = build_cockpit_adopt_plan(
        workspace,
        source_session=candidate.session,
        source_codex_pane=codex_pane,
        source_claude_pane=claude_pane,
        anchor_pane=anchor,
        column_index=len(existing_codex),
        codex_ratio=codex_ratio,
        session=session,
    )
    return plan, None, source_clients


def _handle_cockpit_adopt(
    args, workspace, session, *, columns, session_present, already_in_cockpit,
    existing_codex,
):
    """Route `mozyo cockpit adopt` — detect-only preview vs confirm-gated move (#11898).

    Phase 2 keeps the Phase 1 safety default: with no ``--confirm`` the command
    is read-only (detect + preview the plan, move no panes). ``--json`` and
    ``--dry-run`` stay non-mutating previews even with ``--confirm`` (the
    codebase contract: those flags never run tmux). Only an explicit
    ``--confirm`` (and not ``--dry-run`` / ``--json``) executes the atomic move.
    """
    import json as _json
    import shlex as _shlex

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import normalize_lane

    confirm = bool(getattr(args, "confirm", False))
    json_output = bool(getattr(args, "json_output", False))
    dry_run = bool(getattr(args, "dry_run", False))
    lane_id = normalize_lane(workspace.lane_id)
    codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)
    advisory = _cockpit_adopt_advisory(workspace, session)
    plan, blocked, source_clients = _resolve_cockpit_adopt(
        workspace, session, columns=columns, session_present=session_present,
        already_in_cockpit=already_in_cockpit, existing_codex=existing_codex,
        advisory=advisory, codex_ratio=codex_ratio,
    )

    if json_output:
        payload = {
            "command": "cockpit adopt",
            "phase": 2,
            # This invocation never runs tmux (json is a preview surface); a
            # confirm-run would execute only when a plan exists.
            "executes": False,
            "would_execute": bool(confirm and plan is not None),
            "confirm": confirm,
            "session": session,
            "workspace_id": workspace.workspace_id,
            "lane_id": lane_id,
            "lane_label": workspace.lane_label,
            "already_in_cockpit": already_in_cockpit,
            "source_clients": list(source_clients),
            "blocked": blocked,
            "plan": plan.as_dict() if plan is not None else None,
            "advisory": advisory.as_dict(),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    # Confirm-gated execution: the only path that moves panes. `--dry-run`
    # outranks `--confirm` as a safe preview, so it is handled below, not here.
    if confirm and not dry_run:
        if plan is None:
            die(blocked or "nothing to adopt for this workspace+lane.")
        require_tmux()
        print(
            f"cockpit adopt: moving normal session {plan.source_session!r} "
            f"(codex {plan.source_codex_pane} + claude {plan.source_claude_pane}) "
            f"into cockpit {session!r} as column {plan.column_index} "
            f"({workspace.label}, lane={lane_id})"
        )
        result = execute_cockpit_adopt_plan(plan, run_tmux)
        print(
            f"  adopted: {workspace.label!r} is now a cockpit column; switch to "
            f"the cockpit window to see it (no new iTerm window opened)."
        )
        print(f"  {_source_session_cleanup_note(plan.source_session)}")
        for warning in result.get("stamp_warnings", []):
            print(f"  warning: identity re-stamp incomplete — {warning}")
        return 0

    # Detect-only / preview (bare adopt or `--dry-run`): report and, when a clean
    # candidate exists, show the exact move `--confirm` would run. No mutation.
    print(
        f"cockpit adopt (preview; no panes moved): session={session} "
        f"workspace={workspace.workspace_id} ({workspace.label}) lane={lane_id}"
    )
    for candidate in advisory.candidates:
        print(
            f"  candidate: session={candidate.session} "
            f"roles={','.join(candidate.roles) or '-'} "
            f"panes={','.join(candidate.pane_ids) or '-'}"
        )
    if advisory.message:
        print(f"  {advisory.message}")
    if plan is not None:
        print(
            f"  adopt plan: move {plan.source_codex_pane} (codex) + "
            f"{plan.source_claude_pane} (claude) from {plan.source_session!r} "
            f"into cockpit column {plan.column_index}:"
        )
        for cmd in plan.commands:
            print("    tmux " + " ".join(_shlex.quote(tok) for tok in cmd.argv))
        print("  run `mozyo cockpit adopt --confirm` to execute this move.")
    elif blocked:
        print(f"  cannot adopt: {blocked}")
    elif not advisory.has_candidates:
        print(
            "  no co-existing normal `mozyo` session found for this workspace+lane."
        )
    return 0


def _assess_cockpit_reset(session, *, columns, session_present):
    """Grade the cockpit session for `mozyo cockpit reset` / `rebuild` (#11814).

    Thin application wrapper over the pure :func:`assess_cockpit_reset`: it reads
    the *extra* runtime facts the grade needs (attached clients + the session's
    window list) and hands them, with the already-read ``columns`` /
    ``session_present``, to the domain grader. Read-only and tolerant — it never
    raises, so a bare `cockpit reset` preview cannot break. Crucially it carries
    the client read's *success* through ``attached_clients_known``: a failed read
    is fail-closed (unknown client state), never silently "no client attached"
    (Redmine #11814 review j#57928).
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import assess_cockpit_reset

    if session_present:
        clients, clients_known = _session_attached_clients_result(session)
        windows = tuple(list_session_windows(session))
    else:
        clients, clients_known, windows = (), True, ()
    return assess_cockpit_reset(
        session=session,
        session_present=session_present,
        columns=columns,
        attached_clients=clients,
        attached_clients_known=clients_known,
        windows=windows,
    )


def _cockpit_extra_windows(target):
    """Managed-session windows a reset's `kill-session` destroys beyond `cockpit` (#12330).

    Faithful per-Project-Group windows live in the SAME session as the `cockpit`
    home window, so the reset teardown (`kill-session`) destroys them too. Return
    the window names other than the `cockpit` home window so reset can make that
    multi-window destruction visible before the confirm-gated kill (Unit 5).
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import COCKPIT_WINDOW

    return [w for w in target.windows if w and w != COCKPIT_WINDOW]


def _print_cockpit_reset_inventory(target):
    """Print the session / window / pane inventory a reset/rebuild would act on."""
    print(f"  attached clients: {', '.join(target.attached_clients) or 'none'}")
    print(f"  windows: {', '.join(target.windows) or 'none'}")
    extra = _cockpit_extra_windows(target)
    if extra:
        print(
            f"  warning: `kill-session` also destroys {len(extra)} other window(s) "
            f"in this session, including any Project Group window(s): "
            f"{', '.join(extra)}"
        )
    for pane in target.managed_panes:
        print(
            f"  pane {pane.pane_id}: workspace={pane.workspace_id} "
            f"role={pane.role or '-'} lane={pane.lane_id} (mozyo-managed)"
        )
    for pane in target.unmanaged_panes:
        print(
            f"  pane {pane.pane_id}: role={pane.role or '-'} (NOT mozyo-managed)"
        )


def _handle_cockpit_reset(
    args, workspace, session, *, columns, session_present, rebuild, launch,
    codex_ratio,
):
    """Route `mozyo cockpit reset` / `rebuild` — preview vs confirm-gated teardown (#11814).

    Safety contract (US #11814): the default path and `--dry-run` / `--json` are
    non-mutating previews; only an explicit `--confirm` (and not `--dry-run` /
    `--json`) runs the destructive `kill-session`, and only against a cockpit
    graded mozyo-managed by identity markers — never by session name. ``reset``
    tears the cockpit down; ``rebuild`` is ``reset`` composed with the normal
    create flow (a fresh cockpit seeded with the current workspace), so a broken
    cockpit can be restored in one command. ``rebuild`` against an absent cockpit
    is a plain create (nothing to kill). A fail-closed grade (foreign / unmanaged
    / attached-client) blocks both with a recovery instruction and moves nothing.
    """
    import json as _json
    import shlex as _shlex

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        build_cockpit_plan,
        build_cockpit_reset_plan,
        normalize_lane,
    )

    action = "rebuild" if rebuild else "reset"
    confirm = bool(getattr(args, "confirm", False))
    json_output = bool(getattr(args, "json_output", False))
    dry_run = bool(getattr(args, "dry_run", False))
    no_attach = bool(getattr(args, "no_attach", False))
    lane_id = normalize_lane(workspace.lane_id)

    target = _assess_cockpit_reset(
        session, columns=columns, session_present=session_present
    )
    reset_plan = (
        build_cockpit_reset_plan(session) if target.mozyo_identified else None
    )
    # rebuild always recreates a fresh cockpit from the current workspace; reset
    # never creates. The create plan is the same one bare `mozyo cockpit` builds.
    create_plan = (
        build_cockpit_plan(
            [workspace], codex_ratio=codex_ratio, session=session, launch=launch
        )
        if rebuild
        else None
    )

    # A fail-closed identity / client gate (not the benign "absent" no-op).
    blocked = (
        None if (target.resettable or target.absent) else target.blocked_reason
    )
    # Will the confirmed run mutate? A managed+detached cockpit is killed; an
    # absent cockpit is only (re)built by `rebuild`.
    would_kill = target.resettable
    would_create = bool(rebuild and (target.resettable or target.absent))
    would_execute = bool(confirm and not dry_run and (would_kill or would_create))

    if json_output:
        payload = {
            "command": f"cockpit {action}",
            "action": action,
            # This invocation never runs tmux (json is a preview surface).
            "executes": False,
            "would_execute": would_execute,
            "confirm": confirm,
            "session": session,
            "workspace_id": workspace.workspace_id,
            "lane_id": lane_id,
            "lane_label": workspace.lane_label,
            "blocked": blocked,
            "target": target.as_dict(),
            "reset_plan": reset_plan.as_dict() if reset_plan is not None else None,
            "rebuild_plan": (
                create_plan.as_dict() if create_plan is not None else None
            ),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    # Preview: bare command (no `--confirm`) or `--dry-run`. No mutation.
    if dry_run or not confirm:
        print(
            f"cockpit {action} (preview; no tmux changes): session={session} "
            f"status={target.status} workspace={workspace.workspace_id} "
            f"({workspace.label}) lane={lane_id}"
        )
        if target.session_present:
            _print_cockpit_reset_inventory(target)
        if blocked:
            print(f"  cannot {action}: {blocked}")
        elif would_kill or would_create:
            if reset_plan is not None and would_kill:
                print(f"  reset plan — kill the mozyo cockpit session {session!r}:")
                for cmd in reset_plan.commands:
                    print(
                        "    tmux "
                        + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                    )
            if create_plan is not None:
                verb = "rebuild" if would_kill else "create"
                print(f"  {verb} plan — fresh cockpit for {workspace.label!r}:")
                for cmd in create_plan.commands:
                    print(
                        "    tmux "
                        + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                    )
            print(f"  run `mozyo cockpit {action} --confirm` to execute.")
        else:
            # reset with nothing to tear down (absent cockpit).
            print(f"  nothing to {action}: {target.blocked_reason}")
        return 0

    # Confirm-gated execution: the only path that mutates tmux.
    if blocked:
        die(blocked)
    if not (would_kill or would_create):
        # `reset --confirm` on an absent cockpit: benign no-op, not an error.
        print(
            f"cockpit reset: no cockpit session {session!r} exists — nothing to do."
        )
        return 0

    require_tmux()
    if would_kill and reset_plan is not None:
        extra = _cockpit_extra_windows(target)
        print(
            f"cockpit {action}: tearing down mozyo cockpit session {session!r} "
            f"({len(target.managed_panes)} managed pane(s))"
        )
        if extra:
            print(
                f"  note: this also destroys {len(extra)} other window(s) in the "
                f"session, including any Project Group window(s): {', '.join(extra)}"
            )
        execute_cockpit_reset_plan(reset_plan, run_tmux)
        print(f"  reset: cockpit session {session!r} killed.")
    if not rebuild:
        return 0

    print(f"  rebuilding a fresh cockpit for {workspace.label!r}...")
    execute_cockpit_plan(create_plan, run_tmux, cleanup_captured=True)
    print(f"cockpit rebuilt: session={session} workspace={workspace.label}")
    if no_attach:
        print(f"attach: tmux -CC attach -t {session}")
        return 0
    os.execvp("tmux", ["tmux", "-CC", "attach", "-t", session])
    raise AssertionError("unreachable")


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
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import infer_repo_root

    for pane_id in pane_ids:
        if not pane_id:
            continue
        runtime = _read_cockpit_pane_runtime(session, pane_id)
        cwd = (runtime.get("cwd") or "").strip()
        if not cwd:
            continue
        root = infer_repo_root(cwd)
        if root:
            return root
    return ""


def _membership_observations_from_windows(managed_windows, session: str):
    """Group managed-cockpit-window columns into per-Unit observations (#12341).

    Reshapes the :func:`_read_managed_cockpit_windows` output (a list of windows,
    each with its `columns`) into one
    :class:`mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership.MembershipObservation` per
    ``(workspace_id, lane_id)`` Unit, collapsing the Unit's codex / claude panes
    and resolving each Unit's live checkout root from its pane cwd (so a worktree /
    lane reports its own path, not the registry canonical — review j#62643).
    Role-less columns (no ``workspace_id``) are skipped here — they surface as a
    cockpit-wide warning from the geometry diagnosis instead.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        ROLE_CLAUDE,
        ROLE_CODEX,
        normalize_lane,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import MembershipObservation

    observations = []
    for window in managed_windows or []:
        units: dict[tuple[str, str], dict[str, str]] = {}
        order: list[tuple[str, str]] = []
        for col in window.get("columns", []) or []:
            workspace_id = col.get("workspace_id") or ""
            if not workspace_id:
                continue
            key = (workspace_id, normalize_lane(col.get("lane_id")))
            if key not in units:
                units[key] = {"codex": "", "claude": ""}
                order.append(key)
            role = col.get("role")
            pane_id = col.get("pane_id") or ""
            if role == ROLE_CODEX and not units[key]["codex"]:
                units[key]["codex"] = pane_id
            elif role == ROLE_CLAUDE and not units[key]["claude"]:
                units[key]["claude"] = pane_id
        for key in order:
            codex_pane = units[key]["codex"]
            claude_pane = units[key]["claude"]
            observations.append(
                MembershipObservation(
                    workspace_id=key[0],
                    lane_id=key[1],
                    lane_label="",
                    codex_pane=codex_pane,
                    claude_pane=claude_pane,
                    window=window.get("window") or "",
                    window_id=window.get("window_id") or "",
                    repo_root=_cockpit_unit_repo_root(
                        session, codex_pane, claude_pane
                    ),
                )
            )
    return observations


def _resolve_registry_facts(workspace_id: str):
    """Resolve a cockpit workspace id's registry / anchor facts (#12341, read-only).

    A cockpit pane carries only its ``@mozyo_workspace_id``; the human label and
    repo root live in the home registry, and the anchor presence in the workspace
    itself. Tolerant: a missing / unreadable registry degrades to "unresolved"
    (label falls back to the id, repo root empty) rather than raising, so the
    membership view never aborts on a thin identity record.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import RegistryFacts
    from mozyo_bridge.workspace_registry import load_workspace_by_id, read_anchor

    try:
        record = load_workspace_by_id(workspace_id)
    except (Exception, SystemExit):
        record = None
    if record is None:
        return RegistryFacts.unresolved(workspace_id)
    repo_root = getattr(record, "canonical_path", "") or ""
    anchor_present = False
    if repo_root:
        try:
            anchor_present = read_anchor(Path(repo_root)) is not None
        except (Exception, SystemExit):
            anchor_present = False
    return RegistryFacts(
        label=getattr(record, "project_name", "") or workspace_id,
        repo_root=repo_root,
        registry_present=True,
        anchor_present=anchor_present,
    )


def _collect_cockpit_membership(session: str):
    """Project the live cockpit into a membership report (#12341, read-only).

    Reads every managed cockpit window (shared `cockpit` window + #12330 Project
    Group windows) for the loaded Units, runs the existing read-only geometry
    diagnosis on the `cockpit` window for drift findings, resolves each Unit's
    registry / anchor facts, and hands them all to the pure
    :func:`mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership.project_membership_report`. All
    reads are tolerant: a missing tmux / cockpit degrades to an empty report, so
    `cockpit list` / `status` never abort.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import diagnose_cockpit_geometry
    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import project_membership_report

    managed = _read_managed_cockpit_windows(session)
    geometry = diagnose_cockpit_geometry(
        session=session, panes=_read_cockpit_geometry(session)
    )
    observations = _membership_observations_from_windows(managed, session)
    cockpit_present = bool(managed) or geometry.cockpit_present

    facts: dict[str, object] = {}
    for obs in observations:
        if obs.workspace_id not in facts:
            facts[obs.workspace_id] = _resolve_registry_facts(obs.workspace_id)

    return project_membership_report(
        session=session,
        cockpit_present=cockpit_present,
        observations=observations,
        facts_by_workspace=facts,
        geometry=geometry,
    )


def _handle_cockpit_list(session: str, *, json_output: bool) -> int:
    """`mozyo cockpit list` — operator-facing cockpit membership summary (#12341).

    Read-only: enumerates the workspaces loaded in the cockpit, each with its
    workspace label / id, repo root, window, Codex / Claude pane ids, geometry
    status, and registry / anchor presence (scaffold / root-hardening notes split
    into a warning bucket). Always exits ``0`` — an empty cockpit is a valid
    state, not an error. Cockpit membership is a display / liveness projection,
    never Redmine workflow truth.
    """
    import json as _json

    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import format_membership_text

    report = _collect_cockpit_membership(session)
    if json_output:
        print(_json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_membership_text(report))
    return 0


def _handle_cockpit_status(
    args: argparse.Namespace, session: str, *, json_output: bool
) -> int:
    """`mozyo cockpit status --repo <repo>` — repo-scoped cockpit membership (#12341).

    Read-only: resolves the repo's workspace identity (registry → anchor →
    derivation, the same chain the rest of the cockpit uses) and reports whether
    it is loaded in the cockpit, with its panes / geometry / registry presence.
    When the workspace is absent it says so explicitly (the #12339 mis-read)
    instead of staying silent. Mirrors `doctor-geometry`'s exit convention: ``0``
    when the workspace is a loaded member with healthy geometry, ``1`` otherwise
    (absent, missing peer, or a geometry warning) — so a script can branch on the
    code while still parsing the full report from stdout.
    """
    import dataclasses as _dataclasses
    import json as _json

    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import infer_repo_root
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import normalize_lane
    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
        CockpitMembershipReport,
        absent_membership,
        format_membership_text,
    )

    repo = getattr(args, "repo", None) or getattr(args, "cwd", None) or os.getcwd()
    repo_root = str(Path(repo).expanduser().resolve())
    # The operator asked about THIS checkout: report the queried repo root (walked
    # to its repo top), not the registry canonical / main checkout (review j#62643).
    queried_root = infer_repo_root(repo_root) or repo_root
    canon = resolve_canonical_session(repo_root)
    workspace_id = getattr(canon, "workspace_id", None) or canon.name
    lane = _resolve_workspace_lane(repo_root, getattr(canon, "workspace_id", None))
    target_lane = normalize_lane(lane.lane_id)
    facts = _resolve_registry_facts(workspace_id)

    report = _collect_cockpit_membership(session)
    match = next(
        (
            w
            for w in report.workspaces
            if w.workspace_id == workspace_id
            and normalize_lane(w.lane_id) == target_lane
        ),
        None,
    )
    if match is None:
        label = facts.label if facts.registry_present else canon.name
        match = absent_membership(
            session=session,
            workspace_id=workspace_id,
            label=label,
            repo_root=queried_root,
            lane_id=target_lane,
            lane_label=lane.lane_label,
            registry_present=facts.registry_present,
            anchor_present=facts.anchor_present,
            registry_canonical_path=facts.repo_root,
        )
    else:
        # Pin the matched row's repo root to the queried checkout so a worktree /
        # lane query echoes the path the operator asked about, never the registry
        # canonical main checkout (review j#62643).
        match = _dataclasses.replace(match, repo_root=queried_root)

    single = CockpitMembershipReport(
        session=session,
        cockpit_present=report.cockpit_present,
        workspaces=(match,),
        warnings=report.warnings,
    )
    if json_output:
        payload = single.as_dict()
        payload["query"] = {
            "workspace_id": workspace_id,
            "label": match.label,
            "repo_root": queried_root,
            "registry_canonical_path": facts.repo_root,
            "lane_id": target_lane,
            "member": match.member,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_membership_text(single, query_label=match.label))
    return 0 if match.ok else 1


def _read_cockpit_pane_runtime(session: str, pane_id: str) -> dict:
    """Read one cockpit pane's cwd / foreground process / lane label (#12133, read-only).

    The geometry reader (:func:`_read_cockpit_geometry`) deliberately does not read
    cwd / process (a privacy + scope choice for the diagnostic). Peer adopt needs
    them for its fail-closed preflight (does the candidate's checkout / running
    agent contradict the destination?) and to mirror the destination Unit's lane
    label onto the adopted pane. Tolerant: any tmux failure degrades to empties so
    the planner simply treats the facts as "unknown" (never a fabricated match).
    Returns ``{cwd, process, lane_label}``.
    """
    # A tmux pane id (`%id`) is globally unique, so display-message targets it
    # directly (no window qualifier needed).
    try:
        result = run_tmux(
            "display-message",
            "-p",
            "-t",
            pane_id,
            "-F",
            "#{pane_current_path}\t#{pane_current_command}\t#{@mozyo_lane_label}",
            check=False,
        )
    except (Exception, SystemExit):
        return {"cwd": "", "process": "", "lane_label": ""}
    if getattr(result, "returncode", 1) != 0:
        return {"cwd": "", "process": "", "lane_label": ""}
    line = (getattr(result, "stdout", "") or "").splitlines()
    parts = ((line[0] if line else "").split("\t") + ["", "", ""])[:3]
    return {"cwd": parts[0], "process": parts[1], "lane_label": parts[2]}


def _resolve_peer_adopt_candidate(session: str, pane_id: str):
    """Resolve the role-less candidate pane's preflight facts (#12133).

    Reads the candidate's live cwd / foreground process and resolves the cwd
    through the same registry → anchor → derivation chain the rest of the cockpit
    uses, so the pure planner can fail-closed when the checkout / running agent
    contradicts the destination. Only ids / labels are carried forward — never the
    absolute path (privacy boundary). Tolerant: an unresolvable cwd yields empty
    ids ("unknown", not a contradiction).
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import PeerAdoptCandidate
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import ROLES, normalize_lane

    runtime = _read_cockpit_pane_runtime(session, pane_id)
    cwd = (runtime.get("cwd") or "").strip()
    process = (runtime.get("process") or "").strip()
    process_role = process if process in ROLES else ""

    cwd_workspace_id = ""
    cwd_lane_id = ""
    if cwd:
        try:
            repo_root = str(Path(cwd).expanduser().resolve())
            canon = resolve_canonical_session(repo_root)
            cwd_workspace_id = getattr(canon, "workspace_id", None) or ""
            if cwd_workspace_id:
                lane = _resolve_workspace_lane(repo_root, cwd_workspace_id)
                cwd_lane_id = normalize_lane(getattr(lane, "lane_id", None))
        except (Exception, SystemExit):
            cwd_workspace_id = ""
            cwd_lane_id = ""
    return PeerAdoptCandidate(
        pane_id=pane_id,
        cwd_workspace_id=cwd_workspace_id,
        cwd_lane_id=cwd_lane_id,
        process_role=process_role,
        process_name=process,
    )


def _resolve_peer_adopt_target(session: str, diagnosis, workspace_id: str, lane_id: str, role: str):
    """Build the destination :class:`PeerAdoptTarget`, mirroring its peer's metadata (#12133).

    The lane label is read off the Unit's existing opposite-role peer pane so the
    adopted pane stamps the same human-facing lane label as its sibling; the
    display label defaults to the workspace id. When the Unit / peer cannot be
    found the planner blocks anyway, so missing metadata is harmless.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import PeerAdoptTarget
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import ROLE_CLAUDE, normalize_lane

    target_lane = normalize_lane(lane_id)
    lane_label = None
    unit = next(
        (
            u
            for u in diagnosis.units
            if u.workspace_id == workspace_id
            and normalize_lane(u.lane_id) == target_lane
        ),
        None,
    )
    if unit is not None:
        peer_panes = unit.codex_panes if role == ROLE_CLAUDE else unit.claude_panes
        if peer_panes:
            label = (_read_cockpit_pane_runtime(session, peer_panes[0]).get("lane_label") or "").strip()
            lane_label = label or None
    return PeerAdoptTarget(
        workspace_id=workspace_id,
        lane_id=target_lane,
        lane_label=lane_label,
        label=workspace_id,
    )


def _handle_cockpit_peer_adopt(
    session: str, args: argparse.Namespace, *, json_output: bool, dry_run: bool
) -> int:
    """`mozyo cockpit peer-adopt` — bind a role-less pane as a Unit's missing peer (#12133).

    The first safe repair slice of US #12132: it adopts the role-less cockpit pane
    named by ``--pane`` as the ``--role`` peer of the existing Unit named by
    ``--unit workspace/lane``, by binding that pane's identity options only — never
    a pane move / kill / split / rebalance. Fail-closed: the pure planner
    (:func:`plan_peer_adopt`) must clear every guard (exactly one missing peer, the
    selected role-less candidate, and a non-contradicting cwd/process preflight),
    and the mutation runs only with ``--confirm``. ``--dry-run`` / ``--json`` and a
    bare invocation (no ``--confirm``) preview without mutating and never gate on a
    mutable tmux server. Exit ``0`` when the decision is applicable (and applied,
    when confirmed); ``1`` when fail-closed blocked.
    """
    import json as _json

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
        diagnose_cockpit_geometry,
        format_peer_adopt_text,
        plan_peer_adopt,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import normalize_lane

    pane_id = getattr(args, "peer_pane", None)
    unit_arg = getattr(args, "peer_unit", None)
    role = getattr(args, "peer_role", None)
    confirm = bool(getattr(args, "confirm", False))

    missing = [
        flag
        for flag, value in (("--pane", pane_id), ("--unit", unit_arg), ("--role", role))
        if not value
    ]
    if missing:
        die(
            "cockpit peer-adopt requires "
            + ", ".join(missing)
            + " (e.g. `mozyo cockpit peer-adopt --pane %123 --unit video/default "
            "--role claude`)."
        )

    if "/" in unit_arg:
        workspace_id, lane_token = unit_arg.rsplit("/", 1)
    else:
        workspace_id, lane_token = unit_arg, ""
    workspace_id = workspace_id.strip()
    target_lane = normalize_lane(lane_token)
    if not workspace_id:
        die("cockpit peer-adopt --unit needs a workspace id (e.g. `video/default`).")

    panes = _read_cockpit_geometry(session)
    diagnosis = diagnose_cockpit_geometry(session=session, panes=panes)
    candidate = _resolve_peer_adopt_candidate(session, pane_id)
    target = _resolve_peer_adopt_target(session, diagnosis, workspace_id, target_lane, role)
    decision = plan_peer_adopt(
        diagnosis=diagnosis,
        target=target,
        pane_id=pane_id,
        role=role,
        candidate=candidate,
    )

    will_apply = decision.ok and confirm and not dry_run and not json_output

    if json_output:
        payload = decision.as_dict()
        payload["applied"] = False
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if decision.ok else 1

    if not decision.ok:
        print(format_peer_adopt_text(decision))
        return 1

    if not will_apply:
        print(format_peer_adopt_text(decision, applied=False))
        if not confirm:
            print(
                "  (preview only — re-run with `--confirm` to bind the pane "
                "identity.)"
            )
        return 0

    require_tmux()
    execute_peer_adopt_plan(decision.plan, run_tmux)
    print(format_peer_adopt_text(decision, applied=True))
    print(
        "  smoke: re-run `mozyo cockpit doctor-geometry` and `mozyo agents targets` "
        "to confirm the missing-peer / role-less finding is resolved."
    )
    return 0


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

    Reads the live cockpit ``window_layout`` tree, projects its top-level columns,
    and plans an EQUAL fair-share width rebalance. Safety contract: the default
    path and `--dry-run` / `--json` are non-mutating previews; only an explicit
    `--confirm` (and not `--dry-run` / `--json`) runs the `resize-pane` plan. The
    plan touches column width only — it emits no `set-option` (identity pane
    options stay put) and never `select-layout even-horizontal` (the Codex/Claude
    vertical splits stay intact). It fails closed on a structurally drifted column
    (a nested 2x2 cell), deferring that repair to `mozyo cockpit reconcile`
    (#12136). A cockpit already balanced within tolerance, or with fewer than two
    columns, is a benign no-op.
    """
    import json as _json
    import shlex as _shlex

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import build_cockpit_rebalance_plan

    present, columns = _cockpit_rebalance_columns(session)
    plan = build_cockpit_rebalance_plan(columns, session=session)

    would_execute = bool(
        confirm
        and not dry_run
        and present
        and not plan.balanced
        and not plan.drift
        and plan.commands
    )

    if json_output:
        payload = {
            "command": "cockpit rebalance",
            # This invocation never runs tmux (json is a preview surface).
            "executes": False,
            "would_execute": would_execute,
            "confirm": confirm,
            "session": session,
            "cockpit_present": present,
            "balanced": plan.balanced,
            "drift": plan.drift,
            "plan": plan.as_dict(),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not present:
        print(
            f"cockpit rebalance: no cockpit window for session {session!r} — "
            "nothing to rebalance."
        )
        return 0

    # Preview: bare command (no `--confirm`) or `--dry-run`. No mutation.
    if dry_run or not confirm:
        print(
            f"cockpit rebalance (preview; no tmux changes): session={session} "
            f"columns={plan.column_count} total_width={plan.total_content_width}"
        )
        for col in plan.columns:
            flag = "" if col.clean else " [drift: not a clean full-width split]"
            print(
                f"  column {col.index}: current={col.current_width} -> "
                f"target={col.target_width} (delta {col.delta:+d}) "
                f"pane={col.target_pane or '-'}{flag}"
            )
        if plan.drift:
            print(f"  cannot rebalance: {plan.blocked_reason}")
        elif plan.balanced:
            print("  already balanced within tolerance — nothing to rebalance.")
        else:
            print("  rebalance plan (width only; identity untouched, splits kept):")
            for cmd in plan.commands:
                print(
                    "    tmux " + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                )
            print("  run `mozyo cockpit rebalance --confirm` to apply.")
        return 0

    # Confirm-gated execution: the only path that mutates tmux.
    if plan.drift:
        die(plan.blocked_reason or "cockpit rebalance blocked by structural drift.")
    if plan.balanced or not plan.commands:
        print(
            f"cockpit rebalance: session {session!r} columns already balanced "
            "within tolerance — nothing to do."
        )
        return 0

    require_tmux()
    print(
        f"cockpit rebalance: restoring {plan.column_count} columns toward "
        f"fair-share width in cockpit {session!r}..."
    )
    execute_cockpit_rebalance_plan(plan, run_tmux)
    _, after = _cockpit_rebalance_columns(session)
    widths = [c.width for c in after]
    print(f"  rebalanced: column widths now {widths}.")
    return 0


def _cockpit_pane_identity(session: str):
    """``{pane_id: {workspace_id, lane_id, role}}`` from the live cockpit panes (#12136).

    Reuses the read-only :func:`_read_cockpit_geometry` reader (it already lists
    every cockpit pane with its `@mozyo_*` options) and projects the identity map
    the reconcile planner groups Units by. Returns ``{}`` when the cockpit window
    is absent.
    """
    panes = _read_cockpit_geometry(session)
    if not panes:
        return {}
    return {
        p["pane_id"]: {
            "workspace_id": p.get("workspace_id", ""),
            "lane_id": p.get("lane_id", ""),
            "role": p.get("role", ""),
        }
        for p in panes
        if p.get("pane_id")
    }


def _handle_cockpit_reconcile(
    session: str, *, confirm: bool, json_output: bool, dry_run: bool,
    codex_ratio: int = 70,
) -> int:
    """`mozyo cockpit reconcile` — preview/confirm structural layout-tree repair (#12136).

    Reads the live cockpit ``window_layout`` tree and pane identity, and plans an
    order-preserving flatten of any nested top-level cell (a 2x2 / mixed-Unit
    drift) into clean per-Unit columns via `swap-pane` + a checksum-valid
    `select-layout`. ``codex_ratio`` (CLI ``--ratio``) sizes the codex-over-claude
    vertical split of each rebuilt column. Safety contract: the default path and
    `--dry-run` / `--json` are non-mutating previews; only an explicit `--confirm`
    (and not `--dry-run` / `--json`) runs the plan. It never kills a pane and never
    re-infers identity from geometry. Fails closed on an unidentified pane (#12133
    scope), a Unit split across cells, a duplicate same-role pane, or an
    unparseable layout. A cockpit already one-Unit-per-column, or absent, is a
    benign no-op.
    """
    import json as _json
    import shlex as _shlex

    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        build_cockpit_reconcile_plan,
        parse_window_layout,
    )

    layout = _read_cockpit_window_layout(session)
    present = layout is not None
    root = parse_window_layout(layout) if layout else None
    # Fail closed on an unparseable-but-present layout: a non-empty layout string
    # that did not parse must NOT be reported as "clean" (a no-op success); the
    # safe outcome is a blocked preview / refusal, never a mutation.
    if present and root is None:
        message = (
            f"cockpit reconcile: could not parse the live `window_layout` for "
            f"session {session!r}; refusing to reconcile (fail-closed). Re-read "
            f"and retry, or inspect the cockpit manually."
        )
        if json_output:
            # Same audit-field contract as the normal JSON branch (#12136 j#59881):
            # an unparseable layout has no cells/target, so those are empty/None.
            print(_json.dumps(
                {"command": "cockpit reconcile", "executes": False,
                 "would_execute": False, "confirm": confirm, "session": session,
                 "cockpit_present": True, "drift": False, "clean": False,
                 "blocked_reason": message, "current_layout": layout,
                 "current_cells": [], "target_layout": None,
                 "target_layout_checksum": None, "plan": None},
                ensure_ascii=False, indent=2, sort_keys=True,
            ))
            return 0
        die(message) if confirm and not dry_run else print(message)
        return 0
    identity = _cockpit_pane_identity(session)
    plan = build_cockpit_reconcile_plan(
        root, identity, session=session, codex_ratio=codex_ratio
    )

    would_execute = bool(
        confirm and not dry_run and present and plan.drift and not plan.blocked_reason
    )

    if json_output:
        target = plan.target_layout
        payload = {
            "command": "cockpit reconcile",
            "executes": False,
            "would_execute": would_execute,
            "confirm": confirm,
            "session": session,
            "cockpit_present": present,
            "drift": plan.drift,
            "clean": plan.clean,
            # Normalized audit fields: `blocked_reason` matches `plan.blocked_reason`;
            # `current_layout` / `current_cells` are the observed before-state and
            # `target_layout` / `target_layout_checksum` the planned after-state.
            "blocked_reason": plan.blocked_reason,
            "current_layout": layout,
            "current_cells": [c.as_dict() for c in plan.cells],
            "target_layout": target,
            "target_layout_checksum": (
                target.split(",", 1)[0] if target else None
            ),
            "plan": plan.as_dict(),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not present:
        print(
            f"cockpit reconcile: no cockpit window for session {session!r} — "
            "nothing to reconcile."
        )
        return 0

    units = " | ".join(f"{ws}/{lane}" for ws, lane in plan.units_in_order)

    # Preview: bare command (no `--confirm`) or `--dry-run`. No mutation.
    if dry_run or not confirm:
        print(
            f"cockpit reconcile (preview; no tmux changes): session={session} "
            f"cells={plan.cell_count} units={len(plan.units_in_order)}"
        )
        for cell in plan.cells:
            names = ", ".join(f"{ws}/{lane}" for ws, lane in cell.unit_keys) or "-"
            flag = " [tangled: >1 Unit in one cell]" if cell.tangled else ""
            extra = (
                f" unidentified={list(cell.unidentified_panes)}"
                if cell.unidentified_panes
                else ""
            )
            print(f"  cell {cell.index} (x={cell.x} w={cell.width}): {names}{flag}{extra}")
        if plan.blocked_reason:
            print(f"  cannot reconcile: {plan.blocked_reason}")
        elif plan.clean:
            print("  already one Unit per top-level column — nothing to reconcile.")
        else:
            print(f"  target Unit columns (left-to-right, order preserved): {units}")
            print("  reconcile plan (swap-pane order fix + checksum select-layout; "
                  "no pane killed, identity untouched):")
            for cmd in plan.commands:
                print(
                    "    tmux " + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                )
            print("  run `mozyo cockpit reconcile --confirm` to apply.")
        return 0

    # Confirm-gated execution: the only path that mutates tmux.
    if plan.blocked_reason:
        die(plan.blocked_reason)
    if plan.clean or not plan.commands:
        print(
            f"cockpit reconcile: session {session!r} already one Unit per "
            "top-level column — nothing to do."
        )
        return 0

    require_tmux()
    print(
        f"cockpit reconcile: flattening nested cells into {len(plan.units_in_order)} "
        f"per-Unit columns in cockpit {session!r}..."
    )
    execute_cockpit_reconcile_plan(plan, run_tmux)
    _, after = _cockpit_rebalance_columns(session)
    print(
        f"  reconciled: {len(after)} top-level columns now align with Units; "
        "run `mozyo cockpit rebalance` to even widths."
    )
    return 0


# Faithful `project_group_tmux_window` action names (#12330). Distinct from the
# shared-cockpit `create` / `focus` / `append` so the executor never confuses a
# group-window placement (which mutates a live session without a fresh attach)
# with the single-window create (which attaches a new -CC session).
GROUP_ACTION_FOCUS = "group_focus"
GROUP_ACTION_APPEND = "group_append"
GROUP_ACTION_CREATE = "group_create"
GROUP_ACTIONS = (GROUP_ACTION_FOCUS, GROUP_ACTION_APPEND, GROUP_ACTION_CREATE)


def _cockpit_group_window_action(
    workspace, session, *, decision, codex_ratio, launch
):
    """Resolve the faithful per-Project-Group tmux-window action (#12330).

    Returns ``(action, plan, blocked_reason, window_name)`` for a workspace whose
    desired presentation faithfully executes ``project_group_tmux_window`` (the
    ``decision.executed_surface == group_tmux_window`` case). The caller has
    already confirmed the cockpit ``session`` exists with a `cockpit` home window.

    Fail-closed and identity-safe:

    - Duplicate detection is **cross-window**: if this ``workspace_id + lane_id``
      already has a Codex pane in ANY managed window (the `cockpit` home window or
      a group window), the action is :data:`GROUP_ACTION_FOCUS` of that exact pane
      — never a second placement. Identity is read off the pane options, so the
      window the pane lives in is irrelevant.
    - Otherwise the group's existing window is located by the mozyo-written
      ``@mozyo_group_id`` window marker (deterministic, never the window name).
      A non-empty match -> :data:`GROUP_ACTION_APPEND` a column beside that
      window's rightmost Codex pane (same fair-share split + identity stamping the
      shared cockpit uses). No match (or an ungrouped Unit, ``group_id`` empty) ->
      :data:`GROUP_ACTION_CREATE` a fresh group window.

    Pure-ish: it reads live tmux (multi-window discovery) but mutates nothing; the
    returned plan is executed (with rollback) only on a real run.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        build_cockpit_append_plan,
        build_group_window_create_plan,
        build_group_window_focus_plan,
        normalize_lane,
        sanitize_group_window_name,
    )

    target_lane = normalize_lane(workspace.lane_id)
    managed = _read_managed_cockpit_windows(session)

    # Cross-window duplicate detection (focus priority): a Codex pane carrying the
    # same workspace+lane in any window means the Unit is already laid out.
    for win in managed:
        for col in win.get("columns") or []:
            if (
                col.get("role") == "codex"
                and col.get("workspace_id") == workspace.workspace_id
                and normalize_lane(col.get("lane_id")) == target_lane
            ):
                return (
                    GROUP_ACTION_FOCUS,
                    build_group_window_focus_plan(col["pane_id"], session=session),
                    None,
                    win.get("window") or "",
                )

    group_id = decision.group_id
    window_name = sanitize_group_window_name(decision.desired_window_name)

    # Locate the group's existing window by the deterministic group marker (never
    # the window name). Only a non-empty group id can share a window; an ungrouped
    # Unit (empty group id) always gets its own fresh window.
    host = None
    if group_id:
        for win in managed:
            if (win.get("group_id") or "") == group_id:
                host = win
                break

    if host is not None:
        codex_cols = [
            c for c in (host.get("columns") or []) if c.get("role") == "codex"
        ]
        anchor = _rightmost_codex_anchor(codex_cols)
        if not anchor:
            return (
                GROUP_ACTION_APPEND,
                None,
                (
                    f"Project Group window {host.get('window')!r} exists but carries "
                    "no mozyo-identified codex column to append beside; rebuild the "
                    "cockpit or remove the stale window."
                ),
                host.get("window") or window_name,
            )
        plan = build_cockpit_append_plan(
            workspace,
            anchor_pane=anchor,
            column_index=len(codex_cols),
            codex_ratio=codex_ratio,
            session=session,
            window=host.get("window") or window_name,
            launch=launch,
        )
        return (GROUP_ACTION_APPEND, plan, None, host.get("window") or window_name)

    plan = build_group_window_create_plan(
        workspace,
        group_id=group_id,
        window_name=window_name,
        codex_ratio=codex_ratio,
        session=session,
        launch=launch,
    )
    return (GROUP_ACTION_CREATE, plan, None, window_name)


def cmd_cockpit(args: argparse.Namespace) -> int:
    """`mozyo cockpit` — append/focus the current workspace in the cockpit (#11803).

    Daily entry: `cd <project> && mozyo cockpit` adds the current workspace as a
    column to the shared `mozyo-cockpit` (creating it on first use), focuses it
    if already present, and never spawns a duplicate iTerm window for an
    existing cockpit. `mozyo --cc` is unchanged (#11729). `--dry-run` / `--json`
    are read-only and non-mutating: they read live cockpit state to report the
    action but run no tmux mutation and never abort on a stale cockpit.
    """
    import json as _json
    import shlex as _shlex

    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        COCKPIT_SESSION_DEFAULT,
        CockpitWorkspace,
        build_cockpit_append_plan,
        build_cockpit_focus_plan,
        build_cockpit_plan,
        normalize_lane,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
        GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
        LaunchContext,
        resolve_group_window_placement,
        resolve_launch_placement,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import RepoLocalConfigError

    session = getattr(args, "cockpit_session", None) or COCKPIT_SESSION_DEFAULT
    codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)
    json_output = bool(getattr(args, "json_output", False))
    dry_run = bool(getattr(args, "dry_run", False))

    # `mozyo cockpit doctor-geometry` (Redmine #12131) is a read-only,
    # whole-cockpit diagnostic — it inspects every cockpit pane's observed
    # geometry, not the current workspace, so it short-circuits before workspace
    # resolution and never gates on tmux being mutable.
    if getattr(args, "action", None) == "doctor-geometry":
        return _handle_cockpit_doctor_geometry(session, json_output=json_output)
    # `mozyo cockpit list` / `cockpit status --repo` (Redmine #12341) are read-only
    # operator-facing membership summaries — "is this workspace loaded in the
    # cockpit, are its Codex/Claude panes present, is the geometry healthy?". Like
    # doctor-geometry they inspect the live cockpit (never mutate) and short-circuit
    # before workspace-append resolution; cockpit membership is a display/liveness
    # projection, never Redmine workflow / approval / close truth.
    if getattr(args, "action", None) == "list":
        return _handle_cockpit_list(session, json_output=json_output)
    if getattr(args, "action", None) == "status":
        return _handle_cockpit_status(args, session, json_output=json_output)
    # `mozyo cockpit peer-adopt` (Redmine #12133) is a whole-cockpit repair that
    # binds a role-less pane as a Unit's missing peer. Like doctor-geometry it
    # operates on explicit pane / unit selection (never the current workspace), so
    # it short-circuits before workspace resolution; its preview path is read-only
    # and only the confirmed apply path gates on a mutable tmux server (inside the
    # handler).
    if getattr(args, "action", None) == "peer-adopt":
        return _handle_cockpit_peer_adopt(
            session, args, json_output=json_output, dry_run=dry_run
        )
    # `mozyo cockpit rebalance` (Redmine #12135) is a whole-cockpit, confirm-gated
    # width restore — it reads every column's observed geometry, not the current
    # workspace, so it short-circuits before workspace resolution like
    # doctor-geometry; its own preview path never gates on tmux being mutable.
    if getattr(args, "action", None) == "rebalance":
        return _handle_cockpit_rebalance(
            session,
            confirm=bool(getattr(args, "confirm", False)),
            json_output=json_output,
            dry_run=dry_run,
        )
    # `mozyo cockpit reconcile` (Redmine #12136) is a whole-cockpit, confirm-gated
    # structural repair — it flattens nested top-level cells into per-Unit columns
    # so rebalance can run. Like rebalance/doctor-geometry it inspects every cell,
    # not the current workspace, so it short-circuits before workspace resolution.
    if getattr(args, "action", None) == "reconcile":
        return _handle_cockpit_reconcile(
            session,
            confirm=bool(getattr(args, "confirm", False)),
            json_output=json_output,
            dry_run=dry_run,
            codex_ratio=codex_ratio,
        )
    # `mozyo cockpit adopt` (Redmine #11897, Phase 1) is a detect-only sub-action:
    # it reports a co-existing normal `mozyo` session as an adopt candidate and
    # moves NO panes (explicit transfer is Phase 2 / #11898). Like `--dry-run` /
    # `--json` it is read-only, so it never gates on tmux being mutable.
    adopt_mode = getattr(args, "action", None) == "adopt"
    # `mozyo cockpit reset` / `rebuild` (Redmine #11814) are their own
    # confirm-gated sub-actions: like adopt, the default path is a non-mutating
    # preview, so they do not gate on tmux being mutable up front.
    reset_action = getattr(args, "action", None)
    reset_mode = reset_action in ("reset", "rebuild")
    inspect_only = dry_run or json_output
    no_attach = bool(getattr(args, "no_attach", False))

    repo = getattr(args, "repo", None) or getattr(args, "cwd", None) or os.getcwd()
    cwd_root = str(Path(repo).expanduser().resolve())
    # Project-scoped identity (#12658): when the cockpit is summoned from inside an
    # adopted monorepo project, the workspace identity is the Git repo root and the
    # project scope rides separately, so the dry-run JSON keeps repo_root and the
    # project path distinct. A single-repo workspace keeps repo_root == cwd_root.
    repo_root, (p_scope, p_path, p_label), p_launch = _resolve_project_scope_fields(
        cwd_root, cwd_root
    )
    canon = resolve_canonical_session(repo_root)
    lane = _resolve_workspace_lane(repo_root, getattr(canon, "workspace_id", None))
    workspace = CockpitWorkspace(
        workspace_id=getattr(canon, "workspace_id", None) or canon.name,
        label=canon.name,
        repo_root=repo_root,
        lane_id=lane.lane_id,
        lane_label=lane.lane_label,
        project_scope=p_scope,
        project_path=p_path,
        project_label=p_label,
        launch_cwd=p_launch,
    )

    def launch(role: str, ws) -> str:
        # Cockpit / sublane append: same reproducible auto policy (#11925).
        return _agent_launch_command(
            role,
            session,
            ws.repo_root,
            permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
        )

    # Read-only state read drives the create/append/focus decision. `--dry-run`
    # / `--json` only read (never mutate) — `_read_cockpit_columns` /
    # `_cockpit_session_present` are tolerant so a missing/stale cockpit
    # degrades gracefully instead of aborting.
    if not inspect_only and not adopt_mode and not reset_mode:
        require_tmux()
    columns = _read_cockpit_columns(session)
    session_present = _cockpit_session_present(session)

    # Duplicate detection compares `workspace_id + lane_id + project_scope`
    # (Redmine #11820, #12739): same workspace + same lane + same project scope
    # focuses the existing column; a different lane (a worktree / clone /
    # relocated checkout) OR a different project scope falls through to append as
    # its own column. The project scope is the #12658 stamp, so a department-root
    # pane (empty `project_scope`) and a project-scoped gateway pane that share
    # the same Git root and lane are distinct Units that can coexist (Redmine
    # #12739) instead of the gateway launch collapsing into a focus of the root
    # column. A pre-#11820 pane carries no lane id and normalizes to `default`,
    # matching the `default` lane of a primary checkout; a pre-#12658 pane
    # carries no project scope and normalizes to empty, matching a root launch.
    existing_codex = [c for c in (columns or []) if c.get("role") == "codex"]
    target_lane = normalize_lane(workspace.lane_id)
    target_scope = (workspace.project_scope or "").strip()
    same = next(
        (
            c
            for c in existing_codex
            if c.get("workspace_id") == workspace.workspace_id
            and normalize_lane(c.get("lane_id")) == target_lane
            and (c.get("project_scope") or "").strip() == target_scope
        ),
        None,
    )

    # `mozyo cockpit adopt` short-circuits to its own create/append/focus-free
    # path (#11897 detect / #11898 confirm-gated move): it never spawns fresh
    # columns. Without `--confirm` it is detect-only / preview (Phase 1 safety
    # default); with `--confirm` it atomically moves the co-existing normal
    # session's live panes into the cockpit. `same is not None` means the
    # workspace+lane is already a cockpit column (focus priority, j#57823), so
    # there is nothing to adopt.
    if adopt_mode:
        return _handle_cockpit_adopt(
            args,
            workspace,
            session,
            columns=columns,
            session_present=session_present,
            already_in_cockpit=same is not None,
            existing_codex=existing_codex,
        )

    # `mozyo cockpit reset` / `rebuild` (Redmine #11814) is a confirm-gated,
    # mozyo-identity-gated teardown of a stale/broken cockpit; it never spawns the
    # normal append/focus column and never silently adopts. Like adopt it short-
    # circuits to its own handler.
    if reset_mode:
        return _handle_cockpit_reset(
            args,
            workspace,
            session,
            columns=columns,
            session_present=session_present,
            rebuild=(reset_action == "rebuild"),
            launch=launch,
            codex_ratio=codex_ratio,
        )

    # Adopt advisory rides the normal create/append flow as a NON-mutating notice
    # (#11897): a co-existing normal `mozyo` session for this workspace+lane is an
    # adopt candidate the operator may prefer over a fresh column. Skipped on the
    # focus path (`same is not None`), where the cockpit already shows it.
    adopt_advisory = (
        _cockpit_adopt_advisory(workspace, session) if same is None else None
    )

    # Desired Project-Group presentation placement (Redmine #12302 / #12330). The
    # cockpit launcher / append path reads `.mozyo-bridge/config.yaml`
    # `presentation.project_group_presentation` and resolves the placement for THIS
    # workspace. `same_cockpit_column` (the default / a missing config) preserves
    # current behavior exactly. `project_group_tmux_window` now *faithfully
    # executes* (`execute_group_window=True`): the launcher places the sublane in
    # the Project Group's own tmux window (create / append / cross-window focus)
    # while keeping the same workspace+lane duplicate gate and pane-identity
    # stamping — see `_cockpit_group_window_action`. `normal_window` still records
    # the desired placement and visibly degrades to the shared column (relaunching
    # a normal window is out of this surface's scope). An invalid placement config
    # fails closed (reported under --json/--dry-run, fatal on a real run) — never a
    # silent reroute. Display-only: never a routing / approval / close authority,
    # and never a guaranteed tmux window / iTerm tab.
    presentation_decision = None
    presentation_blocked = None
    try:
        grouping = load_repo_local_config(repo_root).presentation.grouping
        placement = resolve_launch_placement(
            grouping,
            LaunchContext(
                workspace_id=workspace.workspace_id,
                lane_id=target_lane,
                repo_label=workspace.label,
            ),
        )
        presentation_decision = resolve_group_window_placement(
            grouping.project_group_presentation,
            placement,
            execute_group_window=True,
        )
    except RepoLocalConfigError as exc:
        presentation_blocked = (
            f"invalid .mozyo-bridge/config.yaml presentation config: {exc}"
        )

    # Faithful per-Project-Group tmux window (#12330): when the config opts into
    # `project_group_tmux_window` AND the cockpit session already has its `cockpit`
    # home window, route to a group-window create / append / cross-window focus
    # instead of the shared-column flow. Session bootstrap (no cockpit window yet)
    # stays behavior-preserving below: the first Unit seeds the `cockpit` home
    # window, and group windows are additive on subsequent launches — this keeps
    # reset / rebalance / reconcile / doctor's `cockpit`-window identity model
    # intact (re-evaluation note in `unit-target-model.md` #12330).
    faithful_group = (
        presentation_decision is not None
        and presentation_blocked is None
        and presentation_decision.executed_surface
        == GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW
    )

    # `plan is None` marks a blocked action (stale cockpit) — fail-closed on a
    # real run, reported (not aborted) under --dry-run / --json.
    plan = None
    blocked_reason = None
    group_window = None
    if faithful_group and columns is not None:
        action, plan, blocked_reason, group_window = _cockpit_group_window_action(
            workspace,
            session,
            decision=presentation_decision,
            codex_ratio=codex_ratio,
            launch=launch,
        )
    elif columns is None and session_present:
        # The `mozyo-cockpit` session exists but has no usable cockpit window.
        # Treating this as "create" would run `new-session` against an existing
        # session (failing) and the cleanup could then kill that pre-existing
        # session — so fail closed with a recovery action instead (#11803 review).
        action = "create"
        blocked_reason = (
            f"session {session!r} already exists but has no cockpit window to "
            f"add a column to. Rebuild it with `mozyo layout apply cockpit`, or "
            f"remove it (`tmux kill-session -t {session}`) and re-run "
            "`mozyo cockpit`."
        )
    elif columns is None:
        action = "create"
        plan = build_cockpit_plan(
            [workspace], codex_ratio=codex_ratio, session=session, launch=launch
        )
    elif same is not None:
        action = "focus"
        plan = build_cockpit_focus_plan(same["pane_id"], session=session)
    else:
        action = "append"
        # Anchor on the visually rightmost column by geometry, not list-panes
        # order (Redmine #11849): a middle-column anchor would let the
        # full-height split crush an existing column's width.
        anchor = _rightmost_codex_anchor(existing_codex)
        if anchor:
            plan = build_cockpit_append_plan(
                workspace,
                anchor_pane=anchor,
                column_index=len(existing_codex),
                codex_ratio=codex_ratio,
                session=session,
                launch=launch,
            )
        else:
            blocked_reason = (
                f"cockpit session {session!r} exists but carries no "
                "mozyo-identified codex column to append beside; rebuild it "
                "with `mozyo layout apply cockpit` or remove the stale session."
            )

    if json_output:
        payload = plan.as_dict() if plan is not None else {}
        payload["action"] = action
        payload["workspace_id"] = workspace.workspace_id
        payload["lane_id"] = normalize_lane(workspace.lane_id)
        payload["lane_label"] = workspace.lane_label
        # Project scope rides the projection (Redmine #12739) so `cockpit --json`
        # can show that a project-scoped launch appends a distinct Unit instead
        # of focusing the department-root column. Empty for a root / single-repo
        # workspace.
        payload["project_scope"] = (workspace.project_scope or "").strip()
        payload["session"] = session
        payload["blocked"] = blocked_reason
        payload["adopt_advisory"] = (
            adopt_advisory.as_dict() if adopt_advisory is not None else None
        )
        payload["presentation"] = (
            presentation_decision.as_dict()
            if presentation_decision is not None
            else None
        )
        payload["presentation_blocked"] = presentation_blocked
        payload["group_window"] = group_window
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if dry_run:
        print(
            f"cockpit plan: action={action} session={session} "
            f"workspace={workspace.workspace_id} ({workspace.label}) "
            f"lane={normalize_lane(workspace.lane_id)}"
        )
        if plan is None:
            print(f"  (blocked: {blocked_reason})")
        else:
            for cmd in plan.commands:
                print("  tmux " + " ".join(_shlex.quote(token) for token in cmd.argv))
        if presentation_blocked:
            print(f"  (presentation blocked: {presentation_blocked})")
        elif presentation_decision is not None and presentation_decision.degraded:
            print(f"  presentation: {presentation_decision.diagnostic}")
        if group_window is not None:
            print(
                f"  presentation: project_group_tmux_window -> Project Group window "
                f"{group_window!r} (tmux window requested, never guaranteed; "
                "display only)"
            )
        if adopt_advisory is not None and adopt_advisory.message:
            print(f"  {adopt_advisory.message}")
        return 0

    if presentation_blocked:
        # Fail closed on a real run: an invalid presentation config never silently
        # changes (or silently keeps) the placement.
        die(presentation_blocked)

    if blocked_reason:
        die(blocked_reason)

    if action in GROUP_ACTIONS:
        # Faithful per-Project-Group tmux window (#12330). All three actions mutate
        # the LIVE cockpit session and never spawn a fresh -CC attach (the operator
        # switches tmux windows). create / append use cleanup_captured so a
        # mid-build failure kills only the panes this attempt created — and because
        # tmux drops a window with no panes, a failed group-window create leaves no
        # orphan window (rollback boundary, acceptance #12330).
        if action == GROUP_ACTION_FOCUS:
            execute_cockpit_plan(plan, run_tmux)
            print(
                f"workspace {workspace.label!r} already in cockpit {session!r} "
                f"(window {group_window!r}); focused it."
            )
            return 0
        execute_cockpit_plan(plan, run_tmux, cleanup_captured=True)
        if action == GROUP_ACTION_CREATE:
            print(
                f"created Project Group window {group_window!r} in cockpit "
                f"{session!r} for {workspace.label!r}; switch to it with your tmux "
                "window keys (no new iTerm window opened)."
            )
        else:
            print(
                f"appended {workspace.label!r} as a new column to Project Group "
                f"window {group_window!r} in cockpit {session!r}; switch to it with "
                "your tmux window keys (no new iTerm window opened)."
            )
        if adopt_advisory is not None and adopt_advisory.message:
            print(f"  {adopt_advisory.message}")
        return 0

    if action == "create":
        # cleanup_captured kills only the panes THIS attempt created (closing
        # the freshly-created session) — never a blanket `kill-session` that
        # could destroy a pre-existing `mozyo-cockpit` we did not create. If
        # `new-session` itself fails, nothing was captured and nothing is
        # killed, so an existing session is left intact (#11803 review).
        execute_cockpit_plan(plan, run_tmux, cleanup_captured=True)
        print(f"cockpit created: session={session} workspace={workspace.label}")
        if adopt_advisory is not None and adopt_advisory.message:
            print(f"  {adopt_advisory.message}")
        if presentation_decision is not None and presentation_decision.degraded:
            print(f"  presentation: {presentation_decision.diagnostic}")
        if no_attach:
            print(f"attach: tmux -CC attach -t {session}")
            return 0
        os.execvp("tmux", ["tmux", "-CC", "attach", "-t", session])
        raise AssertionError("unreachable")

    if action == "focus":
        # Existing cockpit already shows this workspace — select it, never a
        # duplicate column, and never a second attach/iTerm window.
        execute_cockpit_plan(plan, run_tmux)
        print(
            f"workspace {workspace.label!r} already in cockpit {session!r}; "
            f"focused pane {same['pane_id']}"
        )
        return 0

    # append: add a column to the live cockpit without a new iTerm window. On a
    # mid-append failure the newly-created panes are cleaned up
    # (cleanup_captured) so a failed append never orphans panes in the shared
    # cockpit; the other workspaces' columns are left untouched (#11803 review).
    execute_cockpit_plan(plan, run_tmux, cleanup_captured=True)
    print(
        f"appended {workspace.label!r} as a new column to cockpit {session!r}; "
        "switch to your cockpit window to see it (no new iTerm window opened)"
    )
    if adopt_advisory is not None and adopt_advisory.message:
        print(f"  {adopt_advisory.message}")
    if presentation_decision is not None and presentation_decision.degraded:
        print(f"  presentation: {presentation_decision.diagnostic}")
    return 0


def _parse_mozyo_window_rows(table: str) -> list[dict]:
    """Parse ``list-windows`` rows (``index<TAB>name<TAB>process``) into dicts.

    Mirrors the human ``INDEX/NAME/PROCESS`` table emitted by bare ``mozyo`` so
    the ``--json`` payload exposes the same window facts to external launchers.
    ``index`` is an int when numeric (tmux window indices always are); a
    missing/blank process becomes ``None`` rather than an empty string.
    """
    windows: list[dict] = []
    for line in table.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        index = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        process = parts[2] if len(parts) > 2 else ""
        windows.append(
            {
                "index": int(index) if index.isdigit() else index,
                "name": name,
                "process": process or None,
            }
        )
    return windows


def session_cwd_mismatch(session: str, repo_root: Path) -> list[str]:
    """Return the cwds of panes in `session` when none of them are under `repo_root`.

    The session is considered "pointing at another work root" only when it has at
    least one pane and every pane's cwd is outside `repo_root`. Returns the list
    of offending cwds in that case; otherwise an empty list.
    """
    same_session_panes = [
        pane
        for pane in pane_lines()
        if (pane.get("location") or "").split(":", 1)[0] == session
    ]
    if not same_session_panes:
        return []
    if any(cwd_is_under_repo(pane.get("cwd") or "", repo_root) for pane in same_session_panes):
        return []
    return [pane.get("cwd") or "?" for pane in same_session_panes]


def legacy_basename_session_notice(repo_root: Path, derived_session: str) -> str | None:
    """Return a migration notice when a legacy basename-named session lingers.

    Before Redmine #10796, bare ``mozyo`` named the session after the repo
    basename. Now it derives ``derived_session``. If a session still exists
    under the old basename name *and* it belongs to this repo (at least one
    pane under ``repo_root`` / not clearly another repo's), point the operator
    at it so the old session is not silently orphaned. Returns ``None`` when
    there is nothing to migrate. The notice is advisory only — it never blocks
    the bare-``mozyo`` flow.
    """
    legacy = repo_root.name
    if not legacy or legacy == derived_session:
        return None
    if not session_exists(legacy):
        return None
    if session_cwd_mismatch(legacy, repo_root):
        # The legacy-named session's panes are all outside this repo, so it is
        # a different repo's session that merely shares the basename. Not ours.
        return None
    return (
        f"notice: legacy session '{legacy}' (named by repo basename) exists for this repo; "
        f"bare `mozyo` now derives '{derived_session}'. Attach the old one explicitly with "
        f"`mozyo --session {legacy}` (or `tmux attach -t {legacy}`), or remove it once empty "
        f"with `tmux kill-session -t {legacy}`."
    )


def resolve_status_session(args: argparse.Namespace) -> str:
    """Pick the session ``cmd_status`` should describe.

    Order: explicit ``--session`` > current tmux session (when run inside
    tmux) > the bare-``mozyo`` resolved session name (see
    :func:`resolve_canonical_session`: registered canonical identity first,
    path derivation as fallback). The final fallback matches what bare
    ``mozyo`` creates so ``status`` finds that session by name (Redmine
    #10796, #11429). The hard-coded ``agents`` default is intentionally not
    used; it produced misleading ``session: agents (missing)`` output under
    the bare-``mozyo`` window model (see Asana task 1214758916882465).
    """
    explicit = getattr(args, "session", None)
    if explicit:
        return explicit
    current = current_session_name()
    if current:
        return current
    repo_root = repo_root_from_args(args)
    return resolve_canonical_session(repo_root).name


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


def _notify_standard_via_handoff(args: argparse.Namespace, agent: str, default_kind: str) -> int:
    """Adapter for the standard `notify-*` subcommands.

    Maps the legacy Redmine-shaped CLI flags onto ``orchestrate_handoff``'s
    normalized contract so the standard notify path shares a single
    orchestration rail with `mozyo-bridge handoff` / `mozyo-bridge reply`.
    Legacy queue notifications (`notify-*-legacy-task`) intentionally stay on
    ``notify_agent``; they remain wrapper-only cleanup paths, not the
    standard path. The body lives in
    :class:`mozyo_bridge.application.notify_command.StandardNotifyUseCase`
    (#12931); this stays a thin adapter.
    """
    from mozyo_bridge.application.notify_command import (
        LiveNotifyOps,
        StandardNotifyUseCase,
    )

    return StandardNotifyUseCase(LiveNotifyOps()).run(args, agent, default_kind)


def cmd_notify_codex(args: argparse.Namespace) -> int:
    return _notify_standard_via_handoff(args, "codex", default_kind="reply")


def cmd_notify_claude(args: argparse.Namespace) -> int:
    return _notify_standard_via_handoff(args, "claude", default_kind="reply")


def cmd_notify_codex_review(args: argparse.Namespace) -> int:
    args.type = "review_request"
    return _notify_standard_via_handoff(args, "codex", default_kind="review_request")


def cmd_notify_claude_review_result(args: argparse.Namespace) -> int:
    args.type = "review_result"
    return _notify_standard_via_handoff(args, "claude", default_kind="review_result")


def cmd_notify_codex_legacy_task(args: argparse.Namespace) -> int:
    args.journal = None
    return notify_agent(args, "codex")


def cmd_notify_claude_legacy_task(args: argparse.Namespace) -> int:
    args.journal = None
    return notify_agent(args, "claude")


def _emit_outcome(
    outcome,
    *,
    record_format: str = RECORD_FORMAT_BOTH,
    command: str | None = None,
    recovery_command: str | None = None,
    duplicate_lane_panes: list[str] | None = None,
    role_profile_contract: str | None = None,
    retry: QueueEnterRetryOutcome | None = None,
    activation: TargetActivationOutcome | None = None,
    submit_lines: list[str] | None = None,
) -> None:
    """Emit the structured outcome and/or the durable delivery-record text.

    ``record_format=both`` (default) prints the multi-line record first, a
    blank separator line, and the single-line JSON outcome last so existing
    callers that scrape the last JSON-looking line keep working while humans
    can paste the record block verbatim into the source-of-truth ticket
    system. ``json`` preserves the prior CLI shape for scripts; ``text`` is
    for callers that only want the markdown.

    ``recovery_command`` (Redmine #12162) is an optional copy-pasteable
    recovery command threaded into the markdown record for failure paths whose
    structured ``(status, reason)`` is too generic to special-case inside
    ``build_delivery_record`` (e.g. the queue-enter inactive-split block, which
    emits the shared ``blocked / invalid_args`` reason). It does not affect the
    ``json`` outcome shape that scripts scrape.

    ``duplicate_lane_panes`` (Redmine #12229) is an optional list of redacted
    identity rows for live same-lane duplicate receiver panes; it renders a
    diagnostic advisory in the markdown record and likewise does not affect the
    ``json`` outcome shape.

    ``role_profile_contract`` (Redmine #12388) is the optional fully resolved
    role-profile contract body appended to the markdown record. Like the others
    it does not affect the ``json`` outcome shape; it is intentionally kept to
    the printed (pasteable) record and omitted from the opt-in auto-persist body
    because it may embed operator-supplied field values.
    """
    if record_format not in RECORD_FORMATS:
        die(f"--record-format must be one of {sorted(RECORD_FORMATS)}; got {record_format!r}")
    if record_format in (RECORD_FORMAT_TEXT, RECORD_FORMAT_BOTH):
        print(
            build_delivery_record(
                outcome,
                command=command,
                recovery_command=recovery_command,
                duplicate_lane_panes=duplicate_lane_panes,
                role_profile_contract=role_profile_contract,
                retry=retry,
                activation=activation,
                submit_lines=submit_lines,
            )
        )
        if record_format == RECORD_FORMAT_BOTH:
            print("")
    if record_format in (RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH):
        print(outcome.to_json())


def _submit_lines_for(args: argparse.Namespace, outcome) -> list[str] | None:
    """Build the additive q-enter `- Submit:` telemetry lines, or None.

    Redmine #12705: only the LLM-facing q-enter front door sets
    ``args.submit_intent`` (+ the deterministic ``args.submit_delivery_id`` it
    already printed in its own envelope), so a normal `handoff send` / `reply`
    has no submit telemetry and its record is byte-identical. The composer-residue
    classification is a pure projection of the transport ``(status, reason)``, so
    it cannot drift from the rail's own marker/rollback decision.
    """
    intent = getattr(args, "submit_intent", None)
    if not intent:
        return None
    delivery_id = getattr(args, "submit_delivery_id", None) or "—"
    return submit_record_lines(
        status=outcome.status,
        reason=outcome.reason,
        intent=intent,
        delivery_id=delivery_id,
    )


def _record_format_from_args(args: argparse.Namespace) -> str:
    raw = getattr(args, "record_format", None) or RECORD_FORMAT_BOTH
    if raw not in RECORD_FORMATS:
        die(f"--record-format must be one of {sorted(RECORD_FORMATS)}; got {raw!r}")
    return raw


def _record_command_from_args(args: argparse.Namespace) -> str | None:
    command = getattr(args, "record_command", None)
    if command:
        return str(command)
    return None


def _emit_receipt(receipt, *, record_format: str) -> None:
    """Emit the durable delivery-record persistence receipt (Redmine #12311).

    Carries no credential by construction — only the provider id, the persisted
    flag, an explicit reason, an optional ``issue/journal`` location pointer, and
    the record class. ``text`` / ``both`` print a one-line human summary; ``json``
    / ``both`` print the receipt JSON last so a script can scrape it.
    """
    if record_format in (RECORD_FORMAT_TEXT, RECORD_FORMAT_BOTH):
        if receipt.persisted:
            print(
                f"- Durable delivery record persisted to {receipt.location} "
                f"(class: {receipt.record_class})"
            )
        else:
            print(
                "- Durable delivery record not persisted "
                f"(reason: {receipt.reason})"
            )
    if record_format in (RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH):
        print(receipt.to_json())


def _maybe_persist_delivery_record(
    args: argparse.Namespace,
    outcome,
    *,
    duplicate_lane_panes: list[str] | None,
    record_format: str,
    retry: QueueEnterRetryOutcome | None = None,
    activation: TargetActivationOutcome | None = None,
) -> None:
    """Best-effort durable persistence of the delivery record (Redmine #12311).

    Opt-in via ``--persist-delivery`` and a no-op otherwise, so the default
    handoff behavior is byte-identical. Called only on the *typed* terminal
    paths (``pending_input`` / ``sent``): a blocked-before-typing outcome has no
    delivery to durably record, and its pasteable record already prints to
    stdout.

    The persisted body is rendered WITHOUT the free-text ``--record-command``
    (Finding 1, j#62549): that field is user-supplied and can carry a private
    path or a credential-shaped argument, so the opt-in durable sink must not
    auto-journal it. The printed stdout record (via ``_emit_outcome``) still
    includes ``- Command:`` for human audit-replay; only the auto-persisted body
    omits it. Every other body field is already redacted (``execution_root`` /
    duplicate-pane rows carry no absolute paths), so the persisted note carries
    no unvetted free text. The note records the delivery outcome, the
    receiver/target identity, and (only when one was live at send time) the
    duplicate same-lane advisory — the conditions Redmine #12311 fixes in tests.

    This NEVER alters the pane-send outcome: persistence runs after the outcome
    is emitted and any failure — including an unexpected sink error — is
    swallowed and reported as a ``transport_error`` receipt. The live Redmine
    journal-write transport (Redmine #12347) is wired behind a second explicit
    opt-in: ``--persist-delivery`` selects the seam, and the trusted-environment
    ``MOZYO_REDMINE_DELIVERY_WRITE`` flag enables the live write. Without the env
    opt-in the transport is ``None`` and resolution stays the byte-compatible
    staged ``provider_unavailable`` posture; with it the credential-safe
    transport reads the trusted base URL / API key from the env at write time
    and fails closed (``credential_missing`` / ``unauthorized`` /
    ``provider_unavailable`` / ``transport_error``) without ever carrying a
    credential (``vibes/docs/logics/plugin-ready-adapter-boundary.md``
    Implementation Guardrail #6; the credential boundary is reused verbatim from
    ``redmine_context``).
    """
    if not getattr(args, "persist_delivery", False):
        return
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
        DeliveryReceipt,
        PERSIST_TRANSPORT_ERROR,
        build_delivery_record_note,
        resolve_delivery_record_sink,
    )
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import SOURCE_REDMINE
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
        redmine_delivery_transport_from_env,
    )

    try:
        # `command=None`: the durable sink path must not auto-journal the
        # user-supplied free-text `--record-command` (Finding 1, j#62549). The
        # stdout record built by `_emit_outcome` keeps it for audit-replay.
        record_markdown = build_delivery_record(
            outcome,
            command=None,
            duplicate_lane_panes=duplicate_lane_panes or None,
            retry=retry,
            activation=activation,
        )
        note = build_delivery_record_note(
            outcome,
            record_markdown=record_markdown,
            has_duplicate_advisory=bool(duplicate_lane_panes),
        )
        # Live Redmine journal-write transport (Redmine #12347): built only when
        # the explicit `MOZYO_REDMINE_DELIVERY_WRITE` opt-in is set in the
        # trusted environment; otherwise `None`, so resolution stays the
        # byte-compatible staged `provider_unavailable` posture. The transport
        # reads the trusted base URL / API key from the env at write time and
        # fails closed (credential_missing / unauthorized / provider_unavailable
        # / transport_error) without ever carrying a credential.
        redmine_transport = None
        if (outcome.source or "") == SOURCE_REDMINE:
            redmine_transport = redmine_delivery_transport_from_env()
        sink = resolve_delivery_record_sink(
            enabled=True,
            source=outcome.source or "",
            redmine_transport=redmine_transport,
        )
        receipt = sink.persist(note)
    except Exception:
        # Best-effort: durable persistence must never break or alter the pane
        # send (the delivery already happened). Surface an explicit
        # transport_error receipt instead of raising.
        receipt = DeliveryReceipt(
            provider=getattr(outcome, "source", None),
            persisted=False,
            reason=PERSIST_TRANSPORT_ERROR,
        )
    _emit_receipt(receipt, record_format=record_format)


def _window_active_pane_id(target_info: dict) -> str | None:
    """Best-effort id of the currently-active pane in the target's window.

    Reads a live `pane_lines()` snapshot (Redmine #12597) and returns the id of
    the *other* pane that is the active split of the target pane's window, so the
    durable record can show which pane was active before
    standard_target_admission activated the target. Returns `None` when it cannot
    be observed (no window location, snapshot failure, or no other active pane);
    a failure here must never break delivery.
    """
    location = target_info.get("location") or ""
    window_prefix = location.rsplit(".", 1)[0] if "." in location else location
    if not window_prefix:
        return None
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import pane_resolver as _pr

    try:
        for pane in _pr.pane_lines():
            if pane.get("id") == target_info.get("id"):
                continue
            pane_loc = pane.get("location") or ""
            pane_window = (
                pane_loc.rsplit(".", 1)[0] if "." in pane_loc else pane_loc
            )
            if pane_window == window_prefix and pane.get("pane_active") == "1":
                return pane.get("id")
    except (Exception, SystemExit):
        return None
    return None


def _activate_target_pane(target_info: dict) -> TargetActivationOutcome:
    """Activate an admitted inactive split via `tmux select-pane` (Redmine #12597).

    Pane SELECTION only — never raw `send-keys` / `paste-buffer` / low-level
    `type` / `keys` as a delivery recovery path. Captures the previously-active
    pane first so the durable record can show the active化 / restore facts; the
    optional restore runs after delivery on the sent terminal path.
    """
    target = target_info["id"]
    previous = _window_active_pane_id(target_info)
    run_tmux("select-pane", "-t", target)
    return TargetActivationOutcome(
        activated=True,
        target_pane=target,
        previous_active_pane=previous,
        restored=False,
    )


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
    production resolves to a ``provider_unavailable`` receipt and ``source=asana``
    to ``unsupported_source``.

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
    if getattr(args, "target_repo", None) == AUTO_TARGET_REPO:
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
    # #12438 j#63436). In the managed cockpit / sublane operating model (epic
    # #12366; `vibes/docs/logics/coordinator-sublane-development-flow.md`) the
    # main-unit Claude is not an implementer; implementation-shaped work defaults
    # to a cockpit-visible sublane, so a direct `handoff send --to claude --kind
    # implementation_request` into the cockpit's default/main-lane Claude is a
    # process gap (#12438 j#63432/j#63434). Fail closed in EVERY mode (the
    # resolved target's lane/view is known here, before the mode-scoped binding
    # gate below) unless an explicit `--main-lane-exception` references an
    # owner/operator decision. Deliberately scoped to cockpit panes: a plain
    # `normal_window` Claude (unmanaged repo, no sublane model), a same-lane
    # *sublane* Claude (non-`default` lane), a `--to codex` gateway dispatch, and
    # any non-`implementation_request` notification to the main-lane Claude are
    # all unaffected.
    if main_lane_implementation_request_blocked(
        receiver=receiver,
        kind=kind,
        target_lane_id=preflight_target.lane_id,
        target_is_cockpit_pane=(preflight_target.view_kind == VIEW_KIND_COCKPIT_PANE),
        target_binds_claude=preflight_target.binds_receiver("claude"),
        has_main_lane_exception=bool(getattr(args, "main_lane_exception", None)),
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

    if mode == MODE_QUEUE_ENTER:
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
    sender_session_xw = current_session_name()
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
    if (
        target_activation is not None
        and admission_policy.restore_previous_active
        and target_activation.previous_active_pane
    ):
        try:
            run_tmux("select-pane", "-t", target_activation.previous_active_pane)
            target_activation = TargetActivationOutcome(
                activated=True,
                target_pane=target_activation.target_pane,
                previous_active_pane=target_activation.previous_active_pane,
                restored=True,
            )
        except (Exception, SystemExit):
            pass
    _emit_outcome(
        outcome,
        record_format=record_format,
        command=record_command,
        duplicate_lane_panes=duplicate_lane_panes or None,
        role_profile_contract=role_profile_contract,
        retry=retry_record,
        activation=target_activation,
        submit_lines=_submit_lines_for(args, outcome),
    )
    _maybe_persist_delivery_record(
        args,
        outcome,
        duplicate_lane_panes=duplicate_lane_panes,
        record_format=record_format,
        retry=retry_record,
        activation=target_activation,
    )
    return 0


def cmd_handoff_send(args: argparse.Namespace) -> int:
    # Semantic target selection (Redmine #12663): `--select` resolves the target
    # `%pane` from role/session/repo/project before the unchanged identity gates.
    from mozyo_bridge.application.commands_target_select import apply_handoff_selection

    apply_handoff_selection(args)
    return orchestrate_handoff(args)


def cmd_handoff_reply(args: argparse.Namespace) -> int:
    return orchestrate_handoff(args, default_kind="reply")


def cmd_handoff_ticketless_callback(args: argparse.Namespace) -> int:
    """Standard ticketless no-anchor callback / hands-off primitive (Redmine #12703).

    Returns a ticketless consultation hands-off result (``consultation_result`` /
    ``no_dispatch`` / ``blocked`` / ``anchor_required``) to the caller lane over
    the standard delivery rail (queue-enter / standard semantics, the same target
    admission / repo-identity / cross-session gates), WITHOUT a Redmine anchor and
    without fabricating one. The structured callback fields are carried as the
    workflow *result* (``DeliveryOutcome.ticketless_callback``), recorded
    distinctly from the transport outcome.

    It does NOT touch the Redmine-governed ``handoff reply`` / ``reply`` rail
    (those still require ``--issue`` + ``--journal``), and it fails closed if the
    dispatch decision is an actual child -> grandchild worker dispatch (which still
    requires a real anchor via ``handoff send``).
    """
    return orchestrate_handoff(args, default_kind="reply", ticketless=True)


CONSULT_DEFAULT_KIND = "design_consultation"
"""Default ``--kind`` for `handoff cross-workspace-consult` (Redmine #11779).

The cross-workspace primitive exists to carry design-consultation requests
through the target workspace's Codex gateway, so it defaults to
``design_consultation`` while still accepting any other ``KIND_LABELS`` value
(e.g. a cross-workspace ``review_request``) via an explicit ``--kind``.
"""


def cmd_handoff_cross_workspace_consult(args: argparse.Namespace) -> int:
    """Cross-workspace design-consultation primitive (Redmine #11779).

    A thin, boundary-preserving wrapper over :func:`orchestrate_handoff`. It
    encodes the *standard cross-workspace consult route* as a single command
    without re-implementing or relaxing any safety gate:

    - The receiver is fixed to ``codex``: the consult always lands on the
      target workspace's Codex gateway pane, never directly in a foreign Claude
      pane (a cross-session ``--to claude`` is blocked by the Cross-Workspace
      Handoff gate anyway). The target Codex reads the durable anchor and, if
      implementation is needed, performs the local same-session Claude handoff
      inside its own workspace.
    - ``--target`` and ``--target-repo`` are mandatory at the parser surface,
      so the cross-workspace identity gate (Redmine #10332 / #11301 / #11778)
      always runs. This *tightens* `handoff send` (which only runs the repo
      gate when ``--target-repo`` is supplied); it never relaxes it.
    - ``--kind`` defaults to ``design_consultation`` and may be overridden.
    - The durable source of truth stays the Redmine issue / Asana task; the
      pane notification is only the pointer.

    All actual gating (cross_session_claude block, target_repo identity gate,
    receiver-process binding, marker/landing rail, ``--target-repo auto``
    explicit-``%pane`` requirement) is delegated to :func:`orchestrate_handoff`
    so this wrapper cannot hide or weaken it.

    ``require_receiver_binding=True`` closes the boundary in **every** mode
    (Redmine #11779 review j#58685): the role-binding gate that `handoff send`
    runs only under ``queue-enter`` must also run under ``--mode standard`` /
    ``--mode pending`` here, or an explicit foreign-Claude ``%pane`` could be
    typed into under a ``to=codex`` marker — exactly the gateway bypass this
    primitive promises to prevent.
    """
    args.to = "codex"
    if getattr(args, "kind", None) is None:
        args.kind = CONSULT_DEFAULT_KIND
    return orchestrate_handoff(args, require_receiver_binding=True)


def _confident_workspace_root(cwd: str) -> Path | None:
    """Return the workspace root for ``cwd`` only when its identity is confident.

    Walks up from ``cwd`` using the same markers bare ``mozyo`` uses and returns
    the root only when that root actually bears a repo / workspace marker
    (``.git`` / ``.tmux.conf`` / ``pyproject.toml`` / ``.mozyo-bridge/scaffold.json``).
    Returns ``None`` when ``cwd`` is empty or the walk fell through to a
    marker-less directory, so smart ``init`` fails closed rather than adopting an
    unidentifiable cwd into a derived session. Uses ``find_repo_root`` (a pure
    cwd walk-up) rather than ``resolve_repo_root`` so the root reflects where the
    pane actually is, not a ``MOZYO_REPO`` override.
    """
    if not cwd:
        return None
    root = find_repo_root(Path(cwd))
    if any((root / marker).exists() for marker in REPO_ROOT_MARKERS):
        return root
    return None


def _is_fallback_session_name(name: str) -> bool:
    """True for a low-information tmux-integrated fallback session name.

    The VS Code ``tmux-integrated`` extension sanitizes a non-ASCII workspace
    basename down to underscores, so a fully Japanese basename becomes an
    all-underscore session like ``___________``. Such a name carries no
    workspace identity and is safe for smart ``init`` to rename into the derived
    session. A name with any non-underscore character is treated as meaningful
    and is never renamed without an explicit ``--window-only`` opt-in.
    """
    return bool(name) and all(ch == "_" for ch in name)


def _agent_window_conflict(
    panes: list[dict], session: str, skip_window_index: str, agent: str
) -> list[str]:
    """Return `session:idx(pane)` for other windows in ``session`` named ``agent``.

    The resolver keys agents on the window name, so a second window named
    ``agent`` in the same session is ambiguous even though tmux tolerates it.
    The target window itself (``skip_window_index``) is excluded.
    """
    conflicts = []
    for pane in panes:
        pane_location_value = pane.get("location") or ""
        pane_session, _, pane_rest = pane_location_value.partition(":")
        if pane_session != session:
            continue
        pane_window_index = pane_rest.split(".", 1)[0]
        if pane_window_index == skip_window_index:
            continue
        if pane.get("window_name") == agent:
            conflicts.append(f"{pane_session}:{pane_window_index}({pane.get('id')})")
    return conflicts


def _write_vscode_session_name(root: Path, session_name: str) -> tuple[Path, bool, str | None]:
    """Merge ``tmux-integrated.sessionName`` into ``<root>/.vscode/settings.json``.

    Returns ``(path, written, warning)``. ``written`` is ``False`` with a
    non-``None`` ``warning`` when the existing file is JSONC / unparseable —
    smart ``init`` warns and continues rather than clobbering operator content
    or aborting the whole adoption. Only the workspace-local settings file is
    ever touched; user-global VS Code settings are never read or written.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import (
        VSCODE_SESSION_NAME_KEY,
        VSCODE_SETTINGS_RELATIVE,
        merge_vscode_session_name,
    )

    settings_path = root / VSCODE_SETTINGS_RELATIVE
    existing = (
        settings_path.read_text(encoding="utf-8") if settings_path.exists() else None
    )
    try:
        new_text = merge_vscode_session_name(existing, session_name)
    except ValueError as exc:
        return (
            settings_path,
            False,
            (
                f"{settings_path} is not plain JSON ({exc}); left unchanged. Add "
                f'"{VSCODE_SESSION_NAME_KEY}": "{session_name}" by hand, or re-run '
                "with --no-vscode-settings to silence this."
            ),
        )
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(new_text, encoding="utf-8")
    return settings_path, True, None


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


def _bind_agent_pane_markers(
    target: str, agent: str, workspace_id: str | None, notes: list[str]
) -> None:
    """Stamp `@mozyo_agent_role` (+ `@mozyo_workspace_id`) on an adopted pane.

    Reuses the cockpit identity options (`domain.cockpit_layout`) so a normal
    `init`-adopted pane carries the same machine-readable role/workspace markers
    a cockpit pane does, which is what makes `agents`/`session list` report it as
    a `pane_option` / strong role rather than inferring the role from the window
    name alone. Best-effort: a non-zero tmux exit is noted but never aborts the
    already-completed adoption.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import ROLE_OPTION, WORKSPACE_OPTION

    role_result = run_tmux("set-option", "-p", "-t", target, ROLE_OPTION, agent, check=False)
    if role_result.returncode == 0:
        notes.append(f"bound role marker {ROLE_OPTION}={agent} on {target}")
    else:
        notes.append(
            f"warning: could not set {ROLE_OPTION} on {target} "
            f"(role still resolves from the window name)"
        )
    if workspace_id:
        run_tmux(
            "set-option", "-p", "-t", target, WORKSPACE_OPTION, workspace_id, check=False
        )


def cwd_is_under_repo(cwd: str, repo_root: Path) -> bool:
    if not cwd:
        return True
    try:
        Path(cwd).expanduser().resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return True


def cmd_doctor(args: argparse.Namespace) -> int:
    # Thin handler: the doctor run, the json/text rendering decision, and the
    # exit-code mapping live behind the ``doctor_command`` boundary (#12927).
    # ``run_doctor`` / ``format_doctor_text`` are passed as this module's globals
    # resolved at call time, so the ``commands.*`` monkeypatch tests are unchanged.
    outcome = DoctorCommandUseCase(run_doctor, format_doctor_text).execute(args)
    print(outcome.stdout)
    return outcome.exit_code


def cmd_doctor_instruction(args: argparse.Namespace) -> int:
    # Thin handler over the ``doctor_instruction_command`` boundary (#12930): the
    # run + json/text render + exit-code mapping live there. Lazy imports preserve
    # the ``doctor_instruction`` monkeypatch seams.
    from mozyo_bridge.application.doctor_instruction import (
        format_doctor_instruction_text,
        run_doctor_instruction,
    )
    from mozyo_bridge.application.doctor_instruction_command import InstructionCommandUseCase

    outcome = InstructionCommandUseCase(run_doctor_instruction, format_doctor_instruction_text).execute(args)
    print(outcome.stdout)
    return outcome.exit_code


def cmd_instruction_doctor(args: argparse.Namespace) -> int:
    # Thin handler mirroring ``cmd_doctor_instruction`` over the shared
    # ``doctor_instruction_command`` boundary (#12930). Lazy imports preserve the
    # ``instruction_doctor`` monkeypatch seams.
    from mozyo_bridge.application.instruction_doctor import (
        format_instruction_doctor_text,
        run_instruction_doctor,
    )
    from mozyo_bridge.application.doctor_instruction_command import InstructionCommandUseCase

    outcome = InstructionCommandUseCase(run_instruction_doctor, format_instruction_doctor_text).execute(args)
    print(outcome.stdout)
    return outcome.exit_code


def cmd_instruction_install(args: argparse.Namespace) -> int:
    from mozyo_bridge.application.instruction_install import (
        format_instruction_install_text,
        run_instruction_install,
    )

    result = run_instruction_install(args)
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_instruction_install_text(result))
    return 0 if result["ok"] else 1


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
