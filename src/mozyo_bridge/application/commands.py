from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
from pathlib import Path

from mozyo_bridge.application.doctor import format_doctor_text, run_doctor
from mozyo_bridge.application import tmux_ui as tmux_ui_module
from mozyo_bridge.domain.agent_discovery import (
    AGENT_KINDS,
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    AGENT_KIND_UNKNOWN,
    discover_agents,
    filter_agents,
    infer_repo_root,
)
from mozyo_bridge.domain.handoff import (
    AUTO_TARGET_REPO,
    AnchorError,
    KIND_LABELS,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODES,
    NO_SUBMIT_RETRY_BUDGET,
    RECEIVERS,
    RECORD_FORMAT_BOTH,
    RECORD_FORMAT_JSON,
    RECORD_FORMAT_TEXT,
    RECORD_FORMATS,
    SOURCES,
    build_delivery_record,
    build_marker,
    build_notification_body,
    is_explicit_pane_target,
    make_outcome,
    normalize_anchor,
)
from mozyo_bridge.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.workspace_registry import resolve_canonical_session
from mozyo_bridge.domain.pane_resolver import (
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
from mozyo_bridge.infrastructure.queue_reader import find_handoff_task
from mozyo_bridge.infrastructure.tmux_client import (
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
    resolve_repo_root,
)


def repo_root_from_args(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def scaffold_target_from_args(args: argparse.Namespace) -> Path:
    target = getattr(args, "repo", None)
    if target:
        return Path(target).expanduser().resolve()
    return Path.cwd().resolve()


def config_path_from_args(args: argparse.Namespace) -> str:
    return str(Path(getattr(args, "config_path", None) or default_tmux_conf(repo_root_from_args(args))).expanduser())


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


def cmd_list(_: argparse.Namespace) -> int:
    require_tmux()
    print("TARGET\tLOCATION\tPROCESS\tWINDOW\tCWD")
    for pane in pane_lines():
        print(
            "\t".join(
                [
                    pane["id"],
                    pane["location"],
                    pane["command"],
                    pane.get("window_name") or "-",
                    pane["cwd"],
                ]
            )
        )
    return 0


def cmd_agents_list(args: argparse.Namespace) -> int:
    """Cross-session agent discovery surface (Redmine #10332, #11628).

    Emits one row per ``pane_id`` — the agent identity key (Redmine #11628):
    a pane that belongs to several grouped tmux sessions is ONE agent whose
    memberships are folded into ``views``; the top-level session / window
    fields describe the canonical view (the session matching the workspace's
    canonical session name, resolved registry → anchor → derivation). Each
    row carries the structured fields a sender needs to name an explicit
    cross-workspace handoff target: session, window name and index, pane id
    and index, active flag, classified ``agent_kind`` (``claude`` /
    ``codex`` / ``unknown``), foreground process, inferred ``repo_root``
    (walked up via REPO_ROOT_MARKERS from the pane's ``cwd``), the pane's
    ``cwd``, and an ``ambiguous`` flag when any view's ``(session,
    window_name)`` pair spans multiple windows in its session. ``--session``
    matches the canonical session or any grouped view.

    Read-only. Does not change tmux state, does not interact with
    Asana / Redmine, and is intentionally separate from the legacy ``list`` /
    ``status`` surfaces so existing scripts that scrape those outputs keep
    working. Single tmux server assumed; a multi-server deployment would
    key on ``(socket, pane_id)``.
    """
    from mozyo_bridge.domain.agent_discovery import fold_agents_by_pane

    require_tmux()
    agent_filter = getattr(args, "agent", None)
    if agent_filter is not None and agent_filter not in AGENT_KINDS:
        die(f"--agent must be one of {sorted(AGENT_KINDS)}; got {agent_filter!r}")
    session_filter = getattr(args, "session", None)
    records = filter_agents(
        fold_agents_by_pane(
            discover_agents(),
            resolve_canonical=lambda root: resolve_canonical_session(root).name,
        ),
        session=session_filter,
        agent_kind=agent_filter,
    )
    if getattr(args, "as_json", False):
        import json as _json

        payload = [record.to_dict() for record in records]
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(
        "SESSION\tWINDOW\tIDX\tPANE\tACTIVE\tKIND\tPROCESS\tREPO_ROOT\tCWD\t"
        "AMBIGUOUS\tOTHER_VIEWS"
    )
    for record in records:
        other_views = ",".join(
            view.session for view in record.views if not view.canonical
        )
        print(
            "\t".join(
                [
                    record.session or "-",
                    record.window_name or "-",
                    record.window_index or "-",
                    record.pane_id or "-",
                    "1" if record.pane_active else "0",
                    record.agent_kind,
                    record.process or "-",
                    record.repo_root or "-",
                    record.cwd or "-",
                    "1" if record.ambiguous else "0",
                    other_views or "-",
                ]
            )
        )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    require_tmux()
    path = args.path or config_path_from_args(args)
    source_tmux_conf(path)
    print(f"loaded tmux config: {Path(path).expanduser()}")
    return 0


def _tmux_ui_repo_root(args: argparse.Namespace) -> Path:
    return resolve_repo_root(getattr(args, "repo", None))


def _tmux_ui_host_conf(args: argparse.Namespace) -> Path:
    return tmux_ui_module.resolve_host_tmux_conf(getattr(args, "tmux_conf", None))


def cmd_tmux_ui_install(args: argparse.Namespace) -> int:
    repo_root = _tmux_ui_repo_root(args)
    tmux_conf = _tmux_ui_host_conf(args)
    try:
        result = tmux_ui_module.apply_install(
            repo_root=repo_root,
            tmux_conf=tmux_conf,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            backup=bool(getattr(args, "backup", False)),
        )
    except tmux_ui_module.TmuxUiError as exc:
        die(str(exc))
        return 2  # unreachable; die() raises SystemExit
    suffix = " (dry-run)" if result.dry_run else ""
    if result.action == "noop":
        print(
            f"tmux-ui install: already wired to {result.expected_snippet} "
            f"in {result.tmux_conf}; no change"
        )
    else:
        print(
            f"tmux-ui install: {result.action} managed block in "
            f"{result.tmux_conf} → {result.expected_snippet}{suffix}"
        )
        if result.previous_source_path and result.action == "replaced":
            print(f"  previous source path: {result.previous_source_path}")
        if result.backup_path:
            print(f"  backup written: {result.backup_path}")
    return 0


def cmd_tmux_ui_uninstall(args: argparse.Namespace) -> int:
    tmux_conf = _tmux_ui_host_conf(args)
    try:
        result = tmux_ui_module.apply_uninstall(
            tmux_conf=tmux_conf,
            dry_run=bool(getattr(args, "dry_run", False)),
            backup=bool(getattr(args, "backup", False)),
        )
    except tmux_ui_module.TmuxUiError as exc:
        die(str(exc))
        return 2  # unreachable
    suffix = " (dry-run)" if result.dry_run else ""
    if result.action == "noop":
        print(
            f"tmux-ui uninstall: no managed block found in {result.tmux_conf}; "
            "nothing to do"
        )
    else:
        print(f"tmux-ui uninstall: removed managed block from {result.tmux_conf}{suffix}")
        if result.backup_path:
            print(f"  backup written: {result.backup_path}")
    return 0


def cmd_tmux_ui_status(args: argparse.Namespace) -> int:
    repo_root = _tmux_ui_repo_root(args)
    tmux_conf = _tmux_ui_host_conf(args)
    info = tmux_ui_module.compute_status(repo_root, tmux_conf)
    if getattr(args, "as_json", False):
        import json as _json

        print(_json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if info["state"] != tmux_ui_module.STATE_DRIFT else 1
    print(f"tmux-ui status: {info['state']}")
    print(f"  tmux_conf: {info['tmux_conf']} (exists={info['tmux_conf_exists']})")
    print(
        f"  expected_snippet: {info['expected_snippet']} (exists={info['snippet_exists']})"
    )
    if info.get("current_source_path"):
        print(f"  current_source_path: {info['current_source_path']}")
    if info.get("drift_reason"):
        print(f"  drift_reason: {info['drift_reason']}")
    return 0 if info["state"] != tmux_ui_module.STATE_DRIFT else 1


def cmd_id(_: argparse.Namespace) -> int:
    print(current_pane())
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    require_tmux()
    print(resolve_target(args.target))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    require_tmux()
    target = resolve_target(args.target)
    print(capture_pane(target, args.lines), end="")
    mark_read(target)
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    require_tmux()
    target = resolve_target(args.target)
    require_read(target)
    run_tmux("send-keys", "-t", target, "-l", "--", args.text)
    clear_read(target)
    return 0


def _emit_message_gate_guidance(
    target: str,
    *,
    attempt: int | None = None,
    no_submit: bool = False,
) -> None:
    """Print the stderr guidance trailer after a `mozyo-bridge message` gate failure.

    The base ``error: ...`` line already names the literal next-action verb
    ("read target again", "must read target before interacting", etc.). This
    trailer is the structural anti-shortcut required by Asana task
    1214779823377861: it spells out the retry path (``mozyo-bridge read``,
    then re-run) and the per-preset ``--no-submit`` retry budget so an agent
    cannot conflate the budget with the ``handoff send`` retry pool or jump
    straight to the preset's ``Notification fails`` branch after one transient
    failure.
    """
    cap = NO_SUBMIT_RETRY_BUDGET
    print(
        f"hint: retry path: `mozyo-bridge read {target}` to refresh the read "
        f"marker, then re-run the failed `mozyo-bridge message` command.",
        file=sys.stderr,
    )
    if not no_submit:
        return
    if attempt is not None:
        used = max(0, int(attempt))
        remaining = max(0, cap - used)
        print(
            f"hint: --no-submit retry budget: attempt {used}/{cap} just "
            f"failed; {remaining}/{cap} attempts remaining per preset "
            "contract. Do not borrow from the `mozyo-bridge handoff send` "
            "retry pool — they are separate budgets.",
            file=sys.stderr,
        )
    else:
        print(
            f"hint: --no-submit retry budget: up to {cap} attempts per "
            "preset contract; pass `--attempt N` on each retry to track "
            "the remaining budget. Do not borrow from the `mozyo-bridge "
            "handoff send` retry pool — they are separate budgets.",
            file=sys.stderr,
        )


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
    require_tmux()
    target = resolve_target(args.target)
    attempt = getattr(args, "attempt", None)
    no_submit = not getattr(args, "submit", True)
    try:
        require_read(target)
    except SystemExit:
        # `require_read` dies before returning; intercept so the structural
        # guidance trailer lands on stderr right after the base `error:` line.
        # Re-raise to preserve the SystemExit exit code.
        _emit_message_gate_guidance(target, attempt=attempt, no_submit=no_submit)
        raise
    sender = current_pane()
    sender_id = pane_window_name(sender) or sender
    header = f"[mozyo-bridge from:{sender_id} pane:{sender} at:{pane_location(sender)}]"
    run_tmux("send-keys", "-t", target, "-l", "--", f"{header} {args.text}")
    if getattr(args, "submit", True):
        landing_timeout = float(getattr(args, "landing_timeout", 8.0) or 8.0)
        read_lines = int(getattr(args, "read_lines", 50) or 50)
        landing_lines = max(read_lines, 200)
        if not wait_for_text(target, header, landing_lines, landing_timeout):
            run_tmux("send-keys", "-t", target, "C-u")
            clear_read(target)
            _emit_message_gate_guidance(target, attempt=attempt, no_submit=no_submit)
            die(
                "message marker was not observed in target pane; input was cleared and Enter was not pressed. "
                f"target={target} marker={header}"
            )
        submit_delay = max(0.0, float(getattr(args, "submit_delay", 0.2) or 0.0))
        if submit_delay:
            time.sleep(submit_delay)
        run_tmux("send-keys", "-t", target, "Enter")
    clear_read(target)
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    require_tmux()
    target = resolve_target(args.target)
    require_read(target)
    run_tmux("send-keys", "-t", target, *args.keys)
    clear_read(target)
    return 0


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


def _agent_launch_command(agent: str, session: str, cwd: str | None) -> str:
    """The shell command tmux runs for a new agent pane, with OTel env."""
    import shlex

    env_pairs = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(otel_bootstrap_env(agent, session, cwd).items())
    )
    return f"env {env_pairs} {AGENT_COMMANDS[agent]}"


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
        from mozyo_bridge.domain.managed_marker import mark_target
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
        captured = capture_pane(target, lines)
        if text in captured:
            return True
        if text in _WRAP_INDENT.sub(" ", captured):
            return True
        if text in _WRAP_INDENT.sub("", captured):
            return True
        time.sleep(0.2)
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


def _resolve_cockpit_workspaces(args: argparse.Namespace) -> list:
    """Resolve the active workspaces to summon into the cockpit (Redmine #11788).

    Explicit ``--repo`` columns win (deterministic order). Otherwise the active
    mozyo workspaces are discovered from the live session inventory — one column
    per distinct workspace that currently carries a codex/claude agent pane.
    """
    from mozyo_bridge.domain.cockpit_layout import CockpitWorkspace

    repos = getattr(args, "layout_repos", None)
    out: list = []
    if repos:
        for repo in repos:
            resolved = str(Path(repo).expanduser().resolve())
            canon = resolve_canonical_session(resolved)
            wsid = getattr(canon, "workspace_id", None) or canon.name
            out.append(
                CockpitWorkspace(
                    workspace_id=wsid, label=canon.name, repo_root=resolved
                )
            )
        return out

    from mozyo_bridge.session_inventory import take_inventory

    snapshot = take_inventory()
    by_ws: dict[str, object] = {}
    for rec in snapshot.records:
        if rec.agent_kind not in (AGENT_KIND_CODEX, AGENT_KIND_CLAUDE):
            continue
        wsid = (
            (rec.workspace.workspace_id if rec.workspace else None)
            or rec.repo_root
            or rec.session
        )
        if wsid not in by_ws:
            by_ws[wsid] = CockpitWorkspace(
                workspace_id=wsid, label=rec.session, repo_root=rec.repo_root
            )
    return list(by_ws.values())


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


def cmd_layout_apply(args: argparse.Namespace) -> int:
    """`mozyo layout apply cockpit` — build/focus the cockpit layout (#11788).

    Active workspaces become horizontal columns; within each column the agents
    are a vertical split (Codex top, Claude bottom) at `--ratio`. tmux state is
    the layout's source of truth; `--cc` only swaps the attach for control mode.
    `--json` / `--dry-run` emit the planned tmux commands without touching tmux.
    """
    import shlex as _shlex

    from mozyo_bridge.domain.cockpit_layout import (
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
        return _agent_launch_command(role, session, ws.repo_root)

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


def _read_cockpit_columns(session: str):
    """Read the cockpit window's panes with their workspace identity (#11803).

    Returns a list of ``{pane_id, workspace_id, role}`` (one per pane carrying
    the `@mozyo_workspace_id` user option), or ``None`` when the cockpit window
    does not exist. Identity is read from the tmux user options, not the title.
    """
    from mozyo_bridge.domain.cockpit_layout import COCKPIT_WINDOW

    # Read-only and tolerant: a missing tmux binary / server, or a missing
    # cockpit window, all degrade to "no cockpit" (None) rather than raising —
    # so `--dry-run` / `--json` stay non-mutating and never abort (#11803 review).
    try:
        result = run_tmux(
            "list-panes",
            "-t",
            f"{session}:{COCKPIT_WINDOW}",
            "-F",
            "#{pane_id}\t#{@mozyo_workspace_id}\t#{@mozyo_agent_role}",
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
                {"pane_id": parts[0], "workspace_id": parts[1], "role": parts[2]}
            )
    return columns


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

    from mozyo_bridge.domain.cockpit_layout import (
        COCKPIT_SESSION_DEFAULT,
        CockpitWorkspace,
        build_cockpit_append_plan,
        build_cockpit_focus_plan,
        build_cockpit_plan,
    )

    session = getattr(args, "cockpit_session", None) or COCKPIT_SESSION_DEFAULT
    codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)
    json_output = bool(getattr(args, "json_output", False))
    dry_run = bool(getattr(args, "dry_run", False))
    inspect_only = dry_run or json_output
    no_attach = bool(getattr(args, "no_attach", False))

    repo = getattr(args, "repo", None) or getattr(args, "cwd", None) or os.getcwd()
    repo_root = str(Path(repo).expanduser().resolve())
    canon = resolve_canonical_session(repo_root)
    workspace = CockpitWorkspace(
        workspace_id=getattr(canon, "workspace_id", None) or canon.name,
        label=canon.name,
        repo_root=repo_root,
    )

    def launch(role: str, ws) -> str:
        return _agent_launch_command(role, session, ws.repo_root)

    # Read-only state read drives the create/append/focus decision. `--dry-run`
    # / `--json` only read (never mutate) — `_read_cockpit_columns` /
    # `_cockpit_session_present` are tolerant so a missing/stale cockpit
    # degrades gracefully instead of aborting.
    if not inspect_only:
        require_tmux()
    columns = _read_cockpit_columns(session)
    session_present = _cockpit_session_present(session)

    existing_codex = [c for c in (columns or []) if c.get("role") == "codex"]
    same = next(
        (c for c in existing_codex if c.get("workspace_id") == workspace.workspace_id),
        None,
    )

    # `plan is None` marks a blocked action (stale cockpit) — fail-closed on a
    # real run, reported (not aborted) under --dry-run / --json.
    plan = None
    blocked_reason = None
    if columns is None and session_present:
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
        anchor = existing_codex[-1]["pane_id"] if existing_codex else None
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
        payload["session"] = session
        payload["blocked"] = blocked_reason
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if dry_run:
        print(
            f"cockpit plan: action={action} session={session} "
            f"workspace={workspace.workspace_id} ({workspace.label})"
        )
        if plan is None:
            print(f"  (blocked: {blocked_reason})")
        else:
            for cmd in plan.commands:
                print("  tmux " + " ".join(_shlex.quote(token) for token in cmd.argv))
        return 0

    if blocked_reason:
        die(blocked_reason)

    if action == "create":
        # cleanup_captured kills only the panes THIS attempt created (closing
        # the freshly-created session) — never a blanket `kill-session` that
        # could destroy a pre-existing `mozyo-cockpit` we did not create. If
        # `new-session` itself fails, nothing was captured and nothing is
        # killed, so an existing session is left intact (#11803 review).
        execute_cockpit_plan(plan, run_tmux, cleanup_captured=True)
        print(f"cockpit created: session={session} workspace={workspace.label}")
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
    session = resolve_status_session(args)
    if session_exists(session):
        print(f"session: {session}")
        windows = list_session_windows(session)
        agent_windows = [name for name in windows if name in AGENT_LABELS]
        if agent_windows:
            result = run_tmux(
                "list-panes",
                "-s",
                "-t",
                session,
                "-F",
                "#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_active}\t"
                "#{pane_current_command}\t#{pane_current_path}",
                check=False,
            )
            print("WINDOW\tNAME\tTARGET\tACTIVE\tPROCESS\tCWD")
            if result.returncode == 0:
                print(result.stdout, end="")
            missing = [agent for agent in AGENT_LABELS if agent not in agent_windows]
            for agent in sorted(missing):
                print(
                    f"  {agent} window missing; run `mozyo` to create it, "
                    f"or `mozyo-bridge init {agent}` from the right pane to rename it."
                )
        else:
            print(
                "  no agent windows in this session. "
                "Run `mozyo` from the repo to create one window per agent, "
                "or `mozyo-bridge init claude|codex` from an existing pane to rename "
                "its window into an agent target."
            )
    else:
        print(f"session: {session} (missing)")
    print("")
    return cmd_doctor(args)


def notify_agent(args: argparse.Namespace, agent: str) -> int:
    require_tmux()
    validate_notify_gate(args)
    task = None if getattr(args, "journal", None) else find_handoff_task(args, agent)
    target_name = args.target or agent
    if getattr(args, "config", False):
        load_tmux_conf_for(args)
    target_info = pane_info(target_name)
    ensure_agent_target(target_info, agent, force=args.force)
    target = target_info["id"]
    read_lines = str(args.read_lines)
    cmd_read(argparse.Namespace(target=target, lines=args.read_lines))
    prompt = build_prompt(args, agent, task)
    cmd_message(argparse.Namespace(target=target, text=prompt, submit=False))
    cmd_read(argparse.Namespace(target=target, lines=args.read_lines))
    marker = landing_marker(args, task)
    landing_lines = max(args.read_lines, 200)
    if not wait_for_text(target, marker, landing_lines, args.landing_timeout):
        rollback_unsubmitted_input(target)
        die(
            "notification marker was not observed in target pane; input was cleared and Enter was not pressed. "
            f"target={target} marker={marker}"
        )
    submit_delay = max(0.0, float(getattr(args, "submit_delay", 0.0) or 0.0))
    if submit_delay:
        time.sleep(submit_delay)
    cmd_keys(argparse.Namespace(target=target, keys=["Enter"]))
    gate = f"task={task.get('id')}" if task else f"journal={args.journal}"
    print(f"notified {agent}: {gate} target={target} read_lines={read_lines}")
    return 0


def _notify_standard_via_handoff(args: argparse.Namespace, agent: str, default_kind: str) -> int:
    """Adapter for the standard `notify-*` subcommands.

    Maps the legacy Redmine-shaped CLI flags onto ``orchestrate_handoff``'s
    normalized contract so the standard notify path shares a single
    orchestration rail with `mozyo-bridge handoff` / `mozyo-bridge reply`.
    Legacy queue notifications (`notify-*-legacy-task`) intentionally stay on
    ``notify_agent``; they remain wrapper-only cleanup paths, not the
    standard path.
    """
    validate_notify_gate(args)
    type_str = getattr(args, "type", None)
    if type_str in KIND_LABELS:
        kind = type_str
        summary = None
    else:
        kind = default_kind
        summary = f"legacy --type={type_str}" if type_str else None
    forwarded = argparse.Namespace(
        to=agent,
        source="redmine",
        kind=kind,
        issue=getattr(args, "issue", None),
        journal=getattr(args, "journal", None),
        task_id=None,
        comment_id=None,
        anchor_url=None,
        target=getattr(args, "target", None),
        mode=MODE_QUEUE_ENTER,
        summary=summary,
        force=bool(getattr(args, "force", False)),
        landing_timeout=float(getattr(args, "landing_timeout", 8.0) or 8.0),
        submit_delay=float(getattr(args, "submit_delay", 0.2) or 0.0),
        read_lines=int(getattr(args, "read_lines", 50) or 50),
        record_format=getattr(args, "record_format", RECORD_FORMAT_BOTH),
        record_command=getattr(args, "record_command", None),
    )
    rc = orchestrate_handoff(forwarded)
    # Preserve the legacy success line so external scripts and the in-repo
    # smoke (`smoke/real_tmux_notify_smoke.py`) that grep `notified <agent>:
    # journal=...` keep working. The new primitive owns the durable record
    # and structured outcome; this wrapper line is purely a back-compat
    # courtesy and only fires on successful return from orchestrate_handoff
    # (which dies on marker_timeout, so failure paths never reach this).
    if rc == 0:
        try:
            target = pane_info(getattr(args, "target", None) or agent)["id"]
        except SystemExit:
            target = "-"
        read_lines = int(getattr(args, "read_lines", 50) or 50)
        journal = getattr(args, "journal", None)
        print(f"notified {agent}: journal={journal} target={target} read_lines={read_lines}")
    return rc


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


def _emit_outcome(outcome, *, record_format: str = RECORD_FORMAT_BOTH, command: str | None = None) -> None:
    """Emit the structured outcome and/or the durable delivery-record text.

    ``record_format=both`` (default) prints the multi-line record first, a
    blank separator line, and the single-line JSON outcome last so existing
    callers that scrape the last JSON-looking line keep working while humans
    can paste the record block verbatim into the source-of-truth ticket
    system. ``json`` preserves the prior CLI shape for scripts; ``text`` is
    for callers that only want the markdown.
    """
    if record_format not in RECORD_FORMATS:
        die(f"--record-format must be one of {sorted(RECORD_FORMATS)}; got {record_format!r}")
    if record_format in (RECORD_FORMAT_TEXT, RECORD_FORMAT_BOTH):
        print(build_delivery_record(outcome, command=command))
        if record_format == RECORD_FORMAT_BOTH:
            print("")
    if record_format in (RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH):
        print(outcome.to_json())


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


def orchestrate_handoff(args: argparse.Namespace, *, default_kind: str | None = None) -> int:
    """High-level handoff/reply primitive.

    Owns: receiver-pane resolution, agent-target validation, internal pane
    snapshot, marker-prefixed type, landing wait, fail-closed C-u rollback,
    and structured outcome emission. Does NOT touch Asana/Redmine APIs; that
    durable delivery record integration lives in a follow-up task.

    The standard path does its own pre-type capture so callers do not need to
    run ``mozyo-bridge read`` first.
    """
    require_tmux()

    record_format = _record_format_from_args(args)
    record_command = _record_command_from_args(args)

    receiver = getattr(args, "to", None)
    if receiver not in RECEIVERS:
        die(f"--to must be one of {sorted(RECEIVERS)}; got {receiver!r}")

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
                from mozyo_bridge.domain import pane_resolver as _pr
                from mozyo_bridge.domain.agent_discovery import (
                    codex_gateway_candidates,
                )
                from mozyo_bridge.domain.handoff import (
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
        auto_root = infer_repo_root(auto_cwd)
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

    if mode == MODE_QUEUE_ENTER and target_info.get("window_name") != receiver:
        # Step 9 (v0.2). Under the relaxed queue-enter rail, marker miss does
        # NOT roll back, so an explicit `--target %X` that lands in a different
        # agent's window would silently press Enter into the wrong receiver's
        # pane. The agent gate (`ensure_agent_target`) only verifies the pane
        # is running *some* agent process (claude / codex / node) and does not
        # bind the pane to the intended receiver. The window-name resolver
        # already enforces `window_name == receiver` when no `--target` is
        # supplied; this guard restores the same invariant for explicit pane
        # targets under queue-enter, matching the contract's "Allowed Targets"
        # enumeration of "Claude/Codex agent panes for the intended receiver".
        observed_window = target_info.get("window_name") or "<unknown>"
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
            "--mode queue-enter requires the explicit --target pane to live in "
            f"the receiver's window; --to={receiver!r} but pane {target} is in "
            f"window {observed_window!r}. Drop --target to use window-name "
            "resolution, or pass a pane in the receiver's window."
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
            from mozyo_bridge.domain import pane_resolver as _pr
            from mozyo_bridge.domain.agent_discovery import (
                codex_gateway_candidates,
            )
            from mozyo_bridge.domain.handoff import cross_session_gateway_hint

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

    expected_target_repo = getattr(args, "target_repo", None)
    if expected_target_repo:
        expected_resolved = str(Path(expected_target_repo).expanduser().resolve())
        observed_repo = infer_repo_root(target_info.get("cwd") or "")
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

    if mode == MODE_QUEUE_ENTER and target_info.get("pane_active") != "1":
        # Step 11 (v0.3): active-pane binding. tmux only delivers keystrokes
        # to the pane addressed by `-t`; an inactive split in the right window
        # would still accept the typing but the receiver agent is, by
        # construction, not the foreground process the operator is looking at.
        # queue-enter requires the receiver pane to be the active split.
        observed_active = target_info.get("pane_active") or "<unknown>"
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
            "--mode queue-enter requires the target pane to be the active "
            f"split of its window; pane {target} has pane_active="
            f"{observed_active!r}. Activate the receiver pane in tmux, or "
            "drop --target to use window-name resolution."
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

    try:
        body = build_notification_body(anchor, kind, summary, receiver)
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
            ),
            record_format=record_format,
            command=record_command,
        )
        die(str(exc))
        raise AssertionError("unreachable")

    marker = build_marker(anchor, kind, receiver)

    read_lines = int(getattr(args, "read_lines", 50) or 50)
    # Internal pane snapshot preflight. The standard path must not require
    # callers to run `mozyo-bridge read` first.
    capture_pane(target, read_lines)

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
        )
        _emit_outcome(outcome, record_format=record_format, command=record_command)
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
        )
        _emit_outcome(outcome, record_format=record_format, command=record_command)
        _emit_handoff_marker_timeout_guidance(receiver)
        die(
            "handoff marker was not observed in target pane; input was cleared and Enter was not pressed. "
            f"target={target} marker={marker}"
        )
        raise AssertionError("unreachable")

    submit_delay = max(0.0, float(getattr(args, "submit_delay", 0.2) or 0.0))
    if submit_delay:
        time.sleep(submit_delay)
    run_tmux("send-keys", "-t", target, "Enter")

    # Wording-layer differentiation under the relaxed `queue-enter` rail:
    # marker observed → strict `sent`/`ok`; marker unobserved → `sent`/
    # `queue_enter` (sender did not pre-confirm landing). The receiver-side
    # contract and `next_action_owner` stay identical to strict `sent` per
    # the contract.
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
    )
    _emit_outcome(outcome, record_format=record_format, command=record_command)
    return 0


def cmd_handoff_send(args: argparse.Namespace) -> int:
    return orchestrate_handoff(args)


def cmd_handoff_reply(args: argparse.Namespace) -> int:
    return orchestrate_handoff(args, default_kind="reply")


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
    from mozyo_bridge.domain.session_naming import (
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


def _init_target_suffix(args: argparse.Namespace) -> str:
    """Render the explicit target for use in a suggested re-run command.

    Preserves an explicit ``target`` so escape-hatch suggestions do not silently
    drop it and rename the current pane instead (Redmine #11367 review #54498).
    """
    target = getattr(args, "target", None)
    return f" {target}" if target else ""


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
    """
    require_tmux()
    raw_target = args.target or current_pane()
    if not is_tmux_target(raw_target):
        die(f"init target must be a tmux pane id or location, not a label: {raw_target}")
    resolved = run_tmux("display-message", "-t", raw_target, "-p", "#{pane_id}", check=False)
    if resolved.returncode != 0 or not resolved.stdout.strip():
        die(f"invalid tmux target: {raw_target}")
    target = resolved.stdout.strip()
    location = pane_location(target)
    target_session, _, rest = location.partition(":")
    if not target_session or not rest:
        die(f"could not parse tmux location for {target}: {location!r}")
    target_window_index = rest.split(".", 1)[0]

    panes = pane_lines()
    agent = args.agent

    def refuse_on_agent_window_conflict(session: str) -> None:
        conflicts = _agent_window_conflict(panes, session, target_window_index, agent)
        if conflicts:
            existing = ", ".join(sorted(set(conflicts)))
            die(
                f"session '{session}' already has a window named '{agent}' at "
                f"{existing}. Rename or kill that window before running "
                f"`mozyo-bridge init {agent}` on {target}; tmux tolerates duplicate "
                "window names but the resolver does not."
            )

    # --- Legacy window-only path: rename the window in place, nothing else. ---
    if getattr(args, "window_only", False):
        refuse_on_agent_window_conflict(target_session)
        rename_window(f"{target_session}:{target_window_index}", agent)
        apply_window_subtle_style(target_session, agent)
        print(
            f"initialized {target} as {agent} (renamed window "
            f"{target_session}:{target_window_index} -> {agent}; window-only)"
        )
        return 0

    # --- Smart adoption path. -------------------------------------------------
    target_cwd = ""
    for pane in panes:
        if pane.get("id") == target:
            target_cwd = pane.get("cwd") or ""
            break
    root = _confident_workspace_root(target_cwd)
    if root is None:
        die(
            f"refusing to init {target}: cannot confidently determine this pane's "
            f"workspace root from its cwd ({target_cwd or 'unknown'}). Smart `init` "
            "adopts the pane into the workspace's derived session, which needs a "
            "confident root (a `.git` / `.tmux.conf` / `pyproject.toml` / "
            "`.mozyo-bridge/scaffold.json` ancestor).\n"
            "Next actions:\n"
            f"  - cd into the workspace root and re-run `mozyo-bridge init {agent}`.\n"
            f"  - Or rename only this window in place: "
            f"`mozyo-bridge init {agent}{_init_target_suffix(args)} --window-only`."
        )
    expected = resolve_canonical_session(root)
    expected_name = expected.name

    # All fail-closed checks run before any mutation.
    refuse_on_agent_window_conflict(target_session)
    need_session_rename = target_session != expected_name
    if need_session_rename:
        if not _is_fallback_session_name(target_session):
            die(
                f"refusing to init {target}: it is in tmux session "
                f"'{target_session}', which has a meaningful name that differs from "
                f"this workspace's expected session '{expected_name}'. Smart `init` "
                "only adopts low-information tmux-integrated fallback sessions "
                "(all-underscore names); it will not rename a named session.\n"
                f"  current session:  {target_session}\n"
                f"  expected session: {expected_name}\n"
                "Next actions:\n"
                "  - Start the workspace session: `mozyo` (attach) or "
                "`mozyo --no-attach`.\n"
                f"  - Rename only this window in the current session: "
                f"`mozyo-bridge init {agent}{_init_target_suffix(args)} --window-only`."
            )
        if session_exists(expected_name):
            die(
                f"refusing to init {target}: the expected session '{expected_name}' "
                f"already exists as a separate tmux session, so the fallback session "
                f"'{target_session}' cannot be renamed into it without colliding.\n"
                "Next actions:\n"
                f"  - Attach the existing session and run agents there: "
                f"`tmux attach -t {expected_name}`.\n"
                f"  - Or rename only this window in place: "
                f"`mozyo-bridge init {agent}{_init_target_suffix(args)} --window-only`."
            )

    # --- Mutations: vscode settings -> session rename -> window rename -> style.
    notes: list[str] = []
    if not getattr(args, "no_vscode_settings", False):
        settings_path, written, warning = _write_vscode_session_name(root, expected_name)
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        elif written:
            notes.append(
                f'pinned tmux-integrated.sessionName="{expected_name}" in {settings_path}'
            )

    final_session = target_session
    if need_session_rename:
        rename_session(target_session, expected_name)
        final_session = expected_name
        notes.append(f"renamed session '{target_session}' -> '{expected_name}'")

    rename_window(f"{final_session}:{target_window_index}", agent)
    # `init` is the second entry point that promotes an existing pane into the
    # agent-window rail (the first being bare `mozyo`). Apply the same subtle
    # status-bar tint so adopted panes look identical to `mozyo`-created ones.
    apply_window_subtle_style(final_session, agent)

    print(f"adopted {target} into session '{final_session}' as {agent}")
    for note in notes:
        print(f"  - {note}")
    return 0


def cwd_is_under_repo(cwd: str, repo_root: Path) -> bool:
    if not cwd:
        return True
    try:
        Path(cwd).expanduser().resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return True


def cmd_doctor(args: argparse.Namespace) -> int:
    result = run_doctor(args)
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_doctor_text(result))
    return 0 if result["ok"] else 1


def cmd_doctor_instruction(args: argparse.Namespace) -> int:
    from mozyo_bridge.application.doctor_instruction import (
        format_doctor_instruction_text,
        run_doctor_instruction,
    )

    result = run_doctor_instruction(args)
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_doctor_instruction_text(result))
    return 0 if result["ok"] else 1


def cmd_instruction_doctor(args: argparse.Namespace) -> int:
    from mozyo_bridge.application.instruction_doctor import (
        format_instruction_doctor_text,
        run_instruction_doctor,
    )

    result = run_instruction_doctor(args)
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_instruction_doctor_text(result))
    return 0 if result["ok"] else 1


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


def _rules_store_from_args(args: argparse.Namespace):
    """Resolve the rules store the CLI command should operate against.

    The CLI parser already enforces ``--home`` / ``--repo-local`` as a
    mutually exclusive group; this helper just translates whichever was
    supplied into a ``RulesStore`` so command bodies stay declarative.
    """
    home = getattr(args, "home", None)
    repo_local = getattr(args, "repo_local", None)
    return resolve_rules_store(home=home, repo_local=repo_local)


def cmd_rules_install(args: argparse.Namespace) -> int:
    store = _rules_store_from_args(args)
    written = install_rules(store=store)
    if written:
        for path in written:
            print(f"installed: {path}")
    else:
        print("rules: already up to date")
    return 0


def cmd_rules_status(args: argparse.Namespace) -> int:
    store = _rules_store_from_args(args)
    print("PRESET\tSTATUS\tINSTALLED\tPACKAGED\tPATH")
    ok = True
    for row in rules_status(store=store):
        print("\t".join([row["preset"], row["status"], row["installed"], row["packaged"], row["path"]]))
        if row["status"] != "ok":
            ok = False
    return 0 if ok else 1


def cmd_rules_home(args: argparse.Namespace) -> int:
    if getattr(args, "resolved", False):
        print(str(mozyo_bridge_home()))
    else:
        print(PORTABLE_HOME_EXPRESSION)
    return 0


def _skip_categories_from_args(args: argparse.Namespace) -> set[str]:
    """Collect skip-category flags off the parsed argparse namespace.

    Each `--skip-<category>` flag is namespaced as `skip_<category>` on
    the argparse object. We only forward labels for flags that are
    actually present *and* true, so callers building Namespace objects
    programmatically (tests, library entry points) don't have to know
    every flag name to opt in to the default behaviour.
    """
    labels: set[str] = set()
    if getattr(args, "skip_tmux_ui", False):
        labels.add("tmux-ui")
    if getattr(args, "skip_nagger", False):
        labels.add("nagger")
    return labels


def cmd_scaffold_apply(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    repo_local = bool(getattr(args, "repo_local", False))
    target = scaffold_target_from_args(args)
    paths = write_scaffold(
        args.preset,
        target,
        dry_run=args.dry_run,
        backup=args.backup,
        force=args.force,
        home=home,
        repo_local=repo_local,
        skip_categories=_skip_categories_from_args(args),
    )
    action = "would write" if args.dry_run else "wrote"
    for path in paths:
        print(f"{action}: {path}")
    return 0


def cmd_scaffold_diff(args: argparse.Namespace) -> int:
    """Print a unified diff of what ``scaffold apply <preset>`` would change.

    Compares each rendered router / manifest file against the on-disk
    content (treated as empty when missing). Returns 0 when the worktree
    already matches the rendered output and 1 when at least one file would
    change, mirroring ``git diff --exit-code`` so callers can gate.
    """
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    repo_local = bool(getattr(args, "repo_local", False))
    target = scaffold_target_from_args(args)
    rendered = render_scaffold_files(
        args.preset,
        target,
        home=home,
        repo_local=repo_local,
        skip_categories=_skip_categories_from_args(args),
    )
    any_changes = False
    for item in rendered:
        on_disk_path = target / item.path
        if on_disk_path.exists():
            current = on_disk_path.read_text(encoding="utf-8")
        else:
            current = ""
        if current == item.content:
            continue
        any_changes = True
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            item.content.splitlines(keepends=True),
            fromfile=f"a/{item.path}",
            tofile=f"b/{item.path}",
        )
        for line in diff:
            print(line, end="" if line.endswith("\n") else "\n")
    if not any_changes:
        print(f"scaffold diff: clean ({args.preset} -> {target})")
        return 0
    return 1


def cmd_scaffold_status(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    target = scaffold_target_from_args(args)
    status = scaffold_status(target, home=home)

    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if status.get("clean") else 1

    print(f"target: {status['target']}")
    print(f"manifest: {status['manifest']}")
    if status["manifest"] != "present":
        if status["manifest"] == "missing":
            print(f"  no scaffold manifest at {status['manifest_path']}")
            print("  run `mozyo-bridge scaffold apply <preset>` first")
        elif status["manifest"] == "invalid":
            print(f"  manifest at {status['manifest_path']} is invalid")
            if "error" in status:
                print(f"  {status['error']}")
        return 1

    print(f"preset: {status['preset']}")
    print(f"schema_version: {status.get('schema_version')}")
    print(f"mode: {status.get('mode')}")
    print(f"rule_path: {status['rule_path']}")
    print(
        "central preset version: "
        f"manifest={status.get('manifest_preset_version')!r} "
        f"installed={status.get('installed_preset_version')!r}"
    )
    print(
        "central preset hash: "
        f"manifest={status.get('manifest_preset_hash')!r} "
        f"installed={status.get('installed_preset_hash')!r}"
    )
    print(f"central status: {status.get('central_status')}")
    # Manifest tracks every file scaffold writes — routers plus the
    # repo-local artifacts shipped by governed presets — so use a neutral
    # label rather than "router files:" which understated the scope.
    print("tracked files:")
    for row in status.get("files", []):
        print(f"  {row['path']}: {row['status']}")

    if status.get("clean"):
        print("result: clean")
        return 0

    print("result: drift detected")
    central_status = status.get("central_status")
    if central_status == "missing":
        if status.get("mode") == "repo-local":
            print(
                "  - repo-local preset is missing on disk; run "
                f"`mozyo-bridge rules install --repo-local {status['target']}`"
            )
        else:
            print("  - central preset is missing on disk; run `mozyo-bridge rules install`")
    elif central_status == "drifted-content":
        print("  - central preset content has changed since scaffold time")
        print(
            "    run `mozyo-bridge scaffold apply <preset> --backup` to regenerate routers,"
            " or `--force` to accept the new central preset"
        )
    elif central_status == "drifted-version":
        print("  - central preset version label changed since scaffold time")
    elif central_status == "ok-version-only":
        print(
            "  - manifest is schema v1 (no preset_hash); cannot detect content drift."
            " Regenerate the manifest by running `mozyo-bridge scaffold apply <preset> --backup` to upgrade."
        )
    for row in status.get("files", []):
        if row["status"] == "drifted":
            print(f"  - router {row['path']} was modified locally")
        elif row["status"] == "missing":
            print(f"  - router {row['path']} is missing on disk")
        elif row["status"] == "manifest-missing-hash":
            print(f"  - manifest entry for {row['path']} has no recorded hash")
    return 1


def _docs_context_from_args(args: argparse.Namespace):
    """Build a CatalogContext from argparse `--repo` / `--catalog` values.

    `--repo` defaults to cwd. The catalog defaults to the standard
    governed-preset path; the import stays local so the docs_tools
    package only gets pulled in when the operator uses a `docs ...`
    subcommand.
    """
    from mozyo_bridge.docs_tools import CatalogContext

    repo_raw = getattr(args, "repo", None) or os.getcwd()
    catalog_raw = getattr(args, "catalog", None)
    return CatalogContext.build(repo_raw, catalog_raw)


def cmd_docs_validate(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import (
        validate_catalog,
        validate_file_coverage,
    )

    context = _docs_context_from_args(args)
    errors = validate_catalog(
        context, strict_metadata=bool(getattr(args, "strict_metadata", False))
    )
    notices: list[str] = []
    if getattr(args, "check_file_coverage", False):
        coverage_errors, coverage_notices = validate_file_coverage(
            context, roots=getattr(args, "coverage_root", None)
        )
        errors.extend(coverage_errors)
        notices.extend(coverage_notices)
    for notice in notices:
        print(f"notice: {notice}")
    if errors:
        print("catalog validation failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("catalog validation passed")
    return 0


def cmd_docs_resolve(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import (
        render_resolution_json,
        render_resolution_markdown,
        render_resolution_text,
        resolve_paths,
    )

    context = _docs_context_from_args(args)
    results = resolve_paths(context, list(args.paths))
    fmt = getattr(args, "format", "text")
    if fmt == "json":
        print(render_resolution_json(results))
    elif fmt == "markdown":
        print(render_resolution_markdown(results))
    else:
        print(render_resolution_text(results))
    return 0


def cmd_docs_generate(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import generate_file_conventions, run_generate_check

    context = _docs_context_from_args(args)
    output = getattr(args, "output", None)
    if getattr(args, "check", False):
        ok, output_path, detail = run_generate_check(context, output)
        if not ok:
            print(detail, file=sys.stderr)
            return 1
        print(detail)
        return 0
    output_path = generate_file_conventions(context, output)
    print(output_path.as_posix())
    return 0


def cmd_scaffold_canonical(args: argparse.Namespace) -> int:
    """Render or check the canonical-sourced scaffold artifacts.

    Operates on the mozyo-bridge source tree pointed at by ``--repo``
    (default cwd). ``--check`` re-renders every canonical source and
    fails on drift; without ``--check`` the rendered outputs are
    written to disk.
    """
    from mozyo_bridge.scaffold.canonical import (
        collect_render_results,
        write_render_results,
    )

    repo_root = repo_root_from_args(args)
    results = collect_render_results(repo_root)
    check_only = bool(getattr(args, "check", False))

    def _relative(path: Path) -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    if check_only:
        drifted = [result for result in results if result.drift]
        if drifted:
            for result in drifted:
                print(
                    f"{_relative(result.output_path)} is {result.reason}; rerun "
                    f"`mozyo-bridge scaffold canonical` (without --check) to regenerate.",
                    file=sys.stderr,
                )
            return 1
        for result in results:
            print(f"{_relative(result.output_path)} is up to date")
        return 0

    written = write_render_results(results)
    for path in written:
        print(_relative(path))
    return 0


def cmd_session_name(args: argparse.Namespace) -> int:
    """Print the tmux session name for the repo (Redmine #10796, #11429).

    Resolves the repo root from ``--repo`` (default cwd) and resolves the
    session name registry-first: the canonical session name registered in the
    home registry (or the workspace-local anchor) wins; a never-registered
    workspace falls back to deriving a collision-safe ASCII name (preferring
    the workspace-defaults Redmine identifier, otherwise a hash-suffixed
    repo-path fallback). Prints it on a single line for shell use. ``--json``
    emits the name plus its resolution source and workspace id. Read-only:
    does not touch tmux, Redmine, or write to disk.
    """
    repo_root = repo_root_from_args(args)
    result = resolve_canonical_session(repo_root)
    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "name": result.name,
            "source": result.source,
            "identifier": result.identifier,
            "repo_root": str(result.repo_root),
            "workspace_id": result.workspace_id,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(result.name)
    return 0


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
            "  note: the plist carries no environment variables; the "
            "cockpit's Redmine layer stays `unconfigured` under launchd "
            "until a secure key-delivery follow-up lands."
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


def cmd_session_list(args: argparse.Namespace) -> int:
    """Cross-workspace session inventory (Redmine #11422).

    Lists every tmux pane folded by ``pane_id`` (Redmine #11628: grouped
    sessions are views of one agent, not extra rows) together with the
    workspace identity its repo root resolves to (registry → anchor →
    derivation, NFC-normalized per Redmine #11625). The live tmux runtime is
    the source of truth; each runtime listing refreshes the SQLite cache in
    ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite``, and when tmux
    is unavailable the cache is served instead, explicitly marked stale.
    Read-only towards tmux and the workspace registry.
    """
    from mozyo_bridge.session_inventory import take_inventory

    snapshot = take_inventory()
    if getattr(args, "as_json", False):
        import json as _json

        print(
            _json.dumps(
                snapshot.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return 0
    for note in snapshot.notes:
        print(f"note: {note}", file=sys.stderr)
    if snapshot.stale:
        print(
            f"stale: cached snapshot from {snapshot.collected_at or 'unknown'} "
            "(tmux runtime unavailable)",
            file=sys.stderr,
        )
    print(
        "PANE\tSESSION\tWINDOW\tKIND\tACTIVITY\tPROCESS\tWORKSPACE\t"
        "REPO_ROOT\tOTHER_VIEWS"
    )
    for record in snapshot.records:
        workspace = record.workspace
        workspace_label = "-"
        if workspace is not None:
            workspace_label = workspace.project_name or workspace.canonical_session
        other_views = ",".join(
            view.session for view in record.views if not view.canonical
        )
        activity = record.activity or {}
        print(
            "\t".join(
                [
                    record.pane_id or "-",
                    record.session or "-",
                    record.window_name or "-",
                    record.agent_kind,
                    activity.get("state") or "unknown",
                    record.process or "-",
                    workspace_label,
                    record.repo_root or "-",
                    other_views or "-",
                ]
            )
        )
    return 0


def cmd_session_vscode_settings(args: argparse.Namespace) -> int:
    """Pin the workspace-local VS Code `tmux-integrated` session name (#10796).

    Sets ``tmux-integrated.sessionName`` in ``<repo>/.vscode/settings.json`` to
    the resolved session name (registered canonical identity first, derived
    collision-safe name as fallback), so the VS Code `tmux-integrated`
    extension stops sanitizing the workspace basename down to a low-information
    ``____``-style name. Only the **workspace-local** settings file is ever
    touched — user-global settings (which can carry credentials) are never
    read or written. Without ``--write`` it prints what would be set;
    ``--write`` applies it. An existing settings file with comments/trailing
    commas (JSONC) is refused rather than clobbered.
    """
    from mozyo_bridge.domain.session_naming import (
        VSCODE_SESSION_NAME_KEY,
        VSCODE_SETTINGS_RELATIVE,
        merge_vscode_session_name,
    )

    repo_root = repo_root_from_args(args)
    result = resolve_canonical_session(repo_root)
    settings_path = repo_root / VSCODE_SETTINGS_RELATIVE
    existing = (
        settings_path.read_text(encoding="utf-8") if settings_path.exists() else None
    )

    if not getattr(args, "write", False):
        verb = "would update" if existing is not None else "would create"
        print(
            f'{verb} {settings_path}: "{VSCODE_SESSION_NAME_KEY}": "{result.name}"'
        )
        print(
            "re-run with --write to apply (workspace-local only; "
            "user-global VS Code settings are never touched)"
        )
        return 0

    try:
        new_text = merge_vscode_session_name(existing, result.name)
    except ValueError as exc:
        die(
            f"{settings_path} is not plain JSON ({exc}); it likely contains "
            "comments or trailing commas (JSONC). Add "
            f'"{VSCODE_SESSION_NAME_KEY}": "{result.name}" by hand to avoid '
            "clobbering the existing content."
        )
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(new_text, encoding="utf-8")
    print(f'wrote "{VSCODE_SESSION_NAME_KEY}": "{result.name}" to {settings_path}')
    return 0


def cmd_workspace_register(args: argparse.Namespace) -> int:
    """Register (or refresh) this workspace in the home registry (#11429).

    The only write surface of the workspace registry: upserts the registry
    row in ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/registry.sqlite`` and
    rewrites the workspace-local anchor
    (``<repo>/.mozyo-bridge/workspace.json``). Idempotent: re-running keeps
    the existing workspace id and canonical session name; when the home
    registry was lost, the anchor restores the same identity. The canonical
    session name is derived from the path only on first registration.
    """
    from mozyo_bridge.workspace_registry import register_workspace

    repo_root = repo_root_from_args(args)
    result = register_workspace(
        repo_root, project_name=getattr(args, "name", None)
    )
    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "outcome": result.outcome,
            "registry_path": str(result.registry_path),
            "anchor_path": str(result.anchor_path),
            "workspace": result.record.as_payload(),
            "notes": list(result.notes),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    record = result.record
    print(
        f"{result.outcome}: workspace '{record.project_name}' "
        f"({record.display_path})"
    )
    print(f"  workspace_id:      {record.workspace_id}")
    print(f"  canonical_session: {record.canonical_session}")
    if record.preset:
        version = f" {record.preset_version}" if record.preset_version else ""
        print(f"  preset:            {record.preset}{version}")
    print(f"  registry:          {result.registry_path}")
    print(f"  anchor:            {result.anchor_path}")
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def cmd_workspace_list(args: argparse.Namespace) -> int:
    """List registered workspaces from the home registry (#11429). Read-only."""
    from mozyo_bridge.workspace_registry import list_workspaces, registry_path

    records = list_workspaces()
    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "registry_path": str(registry_path()),
            "workspaces": [record.as_payload() for record in records],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not records:
        print(
            f"no workspaces registered in {registry_path()} "
            "(run `mozyo-bridge workspace register` from a workspace root)"
        )
        return 0
    print("SESSION\tNAME\tPATH\tLAST_SEEN")
    for record in records:
        print(
            f"{record.canonical_session}\t{record.project_name}\t"
            f"{record.display_path}\t{record.last_seen or '-'}"
        )
    return 0


def cmd_workspace_inspect(args: argparse.Namespace) -> int:
    """Show how this workspace's identity resolves (#11429). Read-only.

    Surfaces all three identity layers side by side — home-registry row,
    workspace-local anchor, and the path-derived fallback — plus the
    effective resolution, so registry/anchor drift is visible before it
    bites a handoff gate.
    """
    from mozyo_bridge.domain.session_naming import derive_session_name as _derive
    from mozyo_bridge.workspace_registry import (
        anchor_path,
        load_workspace_by_path,
        read_anchor,
        registry_path,
        resolve_canonical_session,
    )

    repo_root = repo_root_from_args(args)
    record = load_workspace_by_path(repo_root)
    anchor = read_anchor(repo_root)
    derived = _derive(repo_root)
    resolved = resolve_canonical_session(repo_root)

    if getattr(args, "as_json", False):
        import json as _json

        payload = {
            "repo_root": str(resolved.repo_root),
            "registry_path": str(registry_path()),
            "anchor_path": str(anchor_path(resolved.repo_root)),
            "registered": record.as_payload() if record else None,
            "anchor": anchor,
            "derived_fallback": {
                "name": derived.name,
                "source": derived.source,
                "identifier": derived.identifier,
            },
            "resolved": {
                "name": resolved.name,
                "source": resolved.source,
                "workspace_id": resolved.workspace_id,
            },
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"repo_root: {resolved.repo_root}")
    print(f"resolved session: {resolved.name} (source: {resolved.source})")
    if record:
        print(
            f"registry: {record.canonical_session} "
            f"(workspace_id {record.workspace_id}, last_seen {record.last_seen or '-'})"
        )
    else:
        print(f"registry: not registered in {registry_path()}")
    if anchor:
        print(
            f"anchor: {anchor['canonical_session']} "
            f"(workspace_id {anchor['workspace_id']})"
        )
    else:
        print(f"anchor: none at {anchor_path(resolved.repo_root)}")
    print(f"derived fallback: {derived.name} (source: {derived.source})")
    if record and anchor and record.workspace_id != anchor["workspace_id"]:
        print(
            "warning: registry row and anchor disagree on workspace_id; "
            "re-run `mozyo-bridge workspace register` to reconcile "
            "(the anchor wins)."
        )
    return 0


def cmd_workspace_defaults(args: argparse.Namespace) -> int:
    """Render or check the workspace-local Redmine default-project snippet.

    Operates on the workspace at ``--repo`` (default cwd). The single
    source is ``<repo>/.mozyo-bridge/workspace-defaults.yaml`` and the
    generated output is whatever target(s) the YAML declares (default:
    ``.mozyo-bridge/redmine-defaults.md``). ``--check`` re-renders in
    memory and fails on drift; without ``--check`` the rendered output
    is written to disk.
    """
    from mozyo_bridge.workspace_defaults import (
        collect_render_results,
        write_render_results,
    )

    repo_root = repo_root_from_args(args)
    results = collect_render_results(repo_root)
    check_only = bool(getattr(args, "check", False))

    def _relative(path: Path) -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    if check_only:
        drifted = [result for result in results if result.drift]
        if drifted:
            for result in drifted:
                print(
                    f"{_relative(result.output_path)} is {result.reason}; rerun "
                    f"`mozyo-bridge workspace-defaults` (without --check, from the repo root) to regenerate.",
                    file=sys.stderr,
                )
            return 1
        for result in results:
            print(f"{_relative(result.output_path)} is up to date")
        return 0

    written = write_render_results(results)
    for path in written:
        print(_relative(path))
    return 0


def cmd_docs_audit_impact(args: argparse.Namespace) -> int:
    from mozyo_bridge.docs_tools import (
        audit_doc_impact,
        run_generate_check,
    )

    context = _docs_context_from_args(args)
    results = audit_doc_impact(
        context,
        staged=bool(getattr(args, "staged", False)),
        all_changed=bool(getattr(args, "all_changed", False)),
    )
    if not results:
        print("No changed paths.")
    for result in results:
        print(f"[{result['path']}]")
        documents = result["documents"]
        if documents:
            print("documents_to_read:")
            for document in documents:
                sources = ", ".join(document["sources"])
                print(
                    f"- {document['type']} {document['id']} -> {document['canonical_path']} (source: {sources})"
                )
        else:
            print("documents_to_read:")
            print("- none")
        notes = result["notes"]
        if notes:
            print("notes:")
            for note in notes:
                print(f"- {note}")
        print()
    if getattr(args, "check_generated", False):
        ok, _, detail = run_generate_check(
            context, getattr(args, "generated_output", None)
        )
        if not ok:
            print(detail, file=sys.stderr)
            return 1
        print(detail)
    return 0
