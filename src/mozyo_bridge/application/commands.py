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
    make_outcome,
    normalize_anchor,
)
from mozyo_bridge.domain.notification import build_prompt, landing_marker, validate_notify_gate
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
    rename_window,
    require_tmux,
    run_tmux,
    session_exists,
    source_tmux_conf,
)
from mozyo_bridge.scaffold.rules import (
    install_rules,
    render_scaffold_files,
    resolve_rules_store,
    rules_status,
    scaffold_status,
    write_scaffold,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import default_queue_path, default_tmux_conf, resolve_repo_root


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
    """Cross-session agent discovery surface (Redmine #10332).

    Emits one row per tmux pane carrying the structured fields a sender needs
    in order to name an explicit cross-workspace handoff target: session,
    window name and index, pane id and index, active flag, classified
    ``agent_kind`` (``claude`` / ``codex`` / ``unknown``), foreground process,
    inferred ``repo_root`` (walked up via PROJECT_MARKERS from the pane's
    ``cwd``), the pane's ``cwd``, and an ``ambiguous`` flag when the
    ``(session, window_name)`` pair spans multiple windows in the session.

    Read-only. Does not change tmux state, does not interact with
    Asana / Redmine, and is intentionally separate from the legacy ``list`` /
    ``status`` surfaces so existing scripts that scrape those outputs keep
    working.
    """
    require_tmux()
    agent_filter = getattr(args, "agent", None)
    if agent_filter is not None and agent_filter not in AGENT_KINDS:
        die(f"--agent must be one of {sorted(AGENT_KINDS)}; got {agent_filter!r}")
    session_filter = getattr(args, "session", None)
    records = filter_agents(
        discover_agents(),
        session=session_filter,
        agent_kind=agent_filter,
    )
    if getattr(args, "as_json", False):
        import json as _json

        payload = [record.to_dict() for record in records]
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(
        "SESSION\tWINDOW\tIDX\tPANE\tACTIVE\tKIND\tPROCESS\tREPO_ROOT\tCWD\tAMBIGUOUS"
    )
    for record in records:
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
        landing_timeout = float(getattr(args, "landing_timeout", 5.0) or 5.0)
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


def new_agent_session_window(agent: str, session: str, cwd: str | None = None) -> str:
    require_tmux()
    if agent not in AGENT_COMMANDS:
        die(f"unsupported agent: {agent}")
    args = ["new-session", "-d", "-s", session, "-n", agent, "-P", "-F", "#{pane_id}"]
    if cwd:
        args.extend(["-c", cwd])
    args.append(AGENT_COMMANDS[agent])
    result = run_tmux(*args, check=False)
    if result.returncode != 0:
        die(f"tmux new-session failed: {result.stderr.strip() or result.stdout.strip()}")
    pane_id = result.stdout.strip()
    if not pane_id:
        die("tmux new-session did not return a pane id")
    return pane_id


def new_agent_window(agent: str, session: str, cwd: str | None = None) -> str:
    require_tmux()
    if agent not in AGENT_COMMANDS:
        die(f"unsupported agent: {agent}")
    args = ["new-window", "-d", "-t", f"{session}:", "-n", agent, "-P", "-F", "#{pane_id}"]
    if cwd:
        args.extend(["-c", cwd])
    args.append(AGENT_COMMANDS[agent])
    result = run_tmux(*args, check=False)
    if result.returncode != 0:
        die(f"tmux new-window failed: {result.stderr.strip() or result.stdout.strip()}")
    pane_id = result.stdout.strip()
    if not pane_id:
        die("tmux new-window did not return a pane id")
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

    Resolves the repo root, derives the session name from the repo basename,
    ensures a single repo-scoped session containing a ``claude`` window and a
    ``codex`` window, and attaches unless ``--no-attach`` was given.
    """
    require_tmux()
    repo_root = repo_root_from_args(args)
    derived = repo_root.name
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
    print(f"session={session} created={','.join(created) if created else '-'}")
    print("INDEX\tNAME\tPROCESS")
    if result.returncode == 0:
        print(result.stdout, end="")
    if getattr(args, "no_attach", False):
        print(f"attach: tmux attach -t {session}")
        return 0
    os.execvp("tmux", ["tmux", "attach", "-t", session])
    raise AssertionError("unreachable")


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


def resolve_status_session(args: argparse.Namespace) -> str:
    """Pick the session ``cmd_status`` should describe.

    Order: explicit ``--session`` > current tmux session (when run inside
    tmux) > repo basename (window-model derived). The hard-coded ``agents``
    default is intentionally not used; it produced misleading
    ``session: agents (missing)`` output under the bare-``mozyo`` window
    model (see Asana task 1214758916882465).
    """
    explicit = getattr(args, "session", None)
    if explicit:
        return explicit
    current = current_session_name()
    if current:
        return current
    repo_root = repo_root_from_args(args)
    derived = repo_root.name
    if derived:
        return derived
    die("could not derive a session name; pass --session explicitly or run from inside a tmux pane")
    raise AssertionError("unreachable")


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
        landing_timeout=float(getattr(args, "landing_timeout", 5.0) or 5.0),
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
        raise

    target = target_info["id"]

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
        # Step 10 (v0.3): same-session binding. queue-enter must not deliver
        # across tmux sessions: an explicit `--target %X` could otherwise land
        # in a different repo's session under marker miss, and tmux-outside
        # invocations have no sender session to compare against. Reject before
        # typing.
        sender_session = current_session_name()
        target_location = target_info.get("location") or ""
        target_session = (
            target_location.split(":", 1)[0] if ":" in target_location else ""
        )
        if not sender_session or not target_session or sender_session != target_session:
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
                "sender's tmux session; "
                f"sender_session={(sender_session or '<unset>')!r} "
                f"target_session={(target_session or '<unknown>')!r}. "
                "Run `mozyo-bridge` from inside the receiver's tmux session, "
                "or pass a pane id in the sender's session."
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
        die(
            "cross-session handoff to Claude is not allowed; "
            f"sender_session={sender_session_xw!r} target_session={target_session_xw!r}. "
            "Route through the target session's Codex window with `--to codex` "
            "and ask that Codex to perform the local Claude handoff. See the "
            "Cross-Workspace Handoff rule in the agent workflow."
        )
        raise AssertionError("unreachable")

    expected_target_repo = getattr(args, "target_repo", None)
    if expected_target_repo:
        expected_resolved = str(Path(expected_target_repo).expanduser().resolve())
        observed_repo = infer_repo_root(target_info.get("cwd") or "")
        if observed_repo != expected_resolved:
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
                "target pane is not in the expected repo; "
                f"expected={expected_resolved!r} "
                f"observed={(observed_repo or '<unknown>')!r} "
                f"target_cwd={(target_info.get('cwd') or '<unknown>')!r}. "
                "Pass a target pane whose cwd resolves under the expected "
                "repo root, or drop `--target-repo` to skip the check."
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

    landing_timeout = float(getattr(args, "landing_timeout", 5.0) or 5.0)
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


def cmd_init(args: argparse.Namespace) -> int:
    """Rename the target pane's tmux window to ``args.agent``.

    Under the window-only model an agent is identified by its tmux window
    name. ``init`` is the entrypoint for bringing an existing pane — VS Code
    tmux terminal, hand-managed tmux session, external script — into the
    window model without going through bare ``mozyo``. The pane stays where
    it is; only its containing window's name changes.
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

    same_session_windows = []
    for pane in pane_lines():
        pane_location_value = pane.get("location") or ""
        pane_session, _, pane_rest = pane_location_value.partition(":")
        if pane_session != target_session:
            continue
        pane_window_index = pane_rest.split(".", 1)[0]
        if pane_window_index == target_window_index:
            continue
        if pane.get("window_name") == args.agent:
            same_session_windows.append(
                f"{pane_session}:{pane_window_index}({pane.get('id')})"
            )

    if same_session_windows:
        existing = ", ".join(sorted(set(same_session_windows)))
        die(
            f"session '{target_session}' already has a window named '{args.agent}' at "
            f"{existing}. Rename or kill that window before running `mozyo-bridge init "
            f"{args.agent}` on {target}; tmux tolerates duplicate window names but the "
            "resolver does not."
        )

    rename_window(f"{target_session}:{target_window_index}", args.agent)
    # `init` is the second entry point that promotes an existing pane into
    # the agent-window rail (the first being bare `mozyo`). Apply the same
    # subtle status-bar tint so panes brought in via `init` look identical
    # in the status bar to panes created via `mozyo`.
    apply_window_subtle_style(target_session, args.agent)
    print(
        f"initialized {target} as {args.agent} (renamed window "
        f"{target_session}:{target_window_index} -> {args.agent})"
    )
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
