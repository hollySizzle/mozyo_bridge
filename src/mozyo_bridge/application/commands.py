from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

from mozyo_bridge.application.doctor import format_doctor_text, run_doctor
from mozyo_bridge.domain.handoff import (
    AnchorError,
    KIND_LABELS,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    MODES,
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
from mozyo_bridge.scaffold.rules import install_rules, rules_status, scaffold_status, write_scaffold
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


def cmd_config(args: argparse.Namespace) -> int:
    require_tmux()
    path = args.path or config_path_from_args(args)
    source_tmux_conf(path)
    print(f"loaded tmux config: {Path(path).expanduser()}")
    return 0


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


def cmd_message(args: argparse.Namespace) -> int:
    require_tmux()
    target = resolve_target(args.target)
    require_read(target)
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
        mode=MODE_STANDARD,
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
    mode = getattr(args, "mode", MODE_STANDARD) or MODE_STANDARD
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


def cmd_rules_install(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    written = install_rules(home)
    if written:
        for path in written:
            print(f"installed: {path}")
    else:
        print("rules: already up to date")
    return 0


def cmd_rules_status(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    print("PRESET\tSTATUS\tINSTALLED\tPACKAGED\tPATH")
    ok = True
    for row in rules_status(home):
        print("\t".join([row["preset"], row["status"], row["installed"], row["packaged"], row["path"]]))
        if row["status"] != "ok":
            ok = False
    return 0 if ok else 1


def cmd_scaffold_rules(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else None
    target = scaffold_target_from_args(args)
    paths = write_scaffold(
        args.preset,
        target,
        dry_run=args.dry_run,
        backup=args.backup,
        force=args.force,
        home=home,
    )
    action = "would write" if args.dry_run else "wrote"
    for path in paths:
        print(f"{action}: {path}")
    return 0


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
            print("  run `mozyo-bridge scaffold rules <preset>` first")
        elif status["manifest"] == "invalid":
            print(f"  manifest at {status['manifest_path']} is invalid")
            if "error" in status:
                print(f"  {status['error']}")
        return 1

    print(f"preset: {status['preset']}")
    print(f"schema_version: {status.get('schema_version')}")
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
    print("router files:")
    for row in status.get("files", []):
        print(f"  {row['path']}: {row['status']}")

    if status.get("clean"):
        print("result: clean")
        return 0

    print("result: drift detected")
    central_status = status.get("central_status")
    if central_status == "missing":
        print("  - central preset is missing on disk; run `mozyo-bridge rules install`")
    elif central_status == "drifted-content":
        print("  - central preset content has changed since scaffold time")
        print(
            "    run `mozyo-bridge scaffold rules <preset> --backup` to regenerate routers,"
            " or `--force` to accept the new central preset"
        )
    elif central_status == "drifted-version":
        print("  - central preset version label changed since scaffold time")
    elif central_status == "ok-version-only":
        print(
            "  - manifest is schema v1 (no preset_hash); cannot detect content drift."
            " Regenerate the manifest by running `mozyo-bridge scaffold rules <preset> --backup` to upgrade."
        )
    for row in status.get("files", []):
        if row["status"] == "drifted":
            print(f"  - router {row['path']} was modified locally")
        elif row["status"] == "missing":
            print(f"  - router {row['path']} is missing on disk")
        elif row["status"] == "manifest-missing-hash":
            print(f"  - manifest entry for {row['path']} has no recorded hash")
    return 1
