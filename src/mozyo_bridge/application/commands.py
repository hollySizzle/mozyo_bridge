from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from pathlib import Path

from mozyo_bridge.application.doctor import format_doctor_text, run_doctor
from mozyo_bridge.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.domain.pane_resolver import (
    AGENT_COMMANDS,
    AGENT_LABELS,
    clear_read,
    current_pane,
    current_session_name,
    ensure_agent_target,
    find_agent_window,
    find_labeled_pane,
    is_agent_process,
    is_tmux_target,
    mark_read,
    pane_info,
    require_read,
    resolve_agent_label,
    resolve_target,
)
from mozyo_bridge.infrastructure.queue_reader import find_handoff_task
from mozyo_bridge.infrastructure.tmux_client import (
    capture_pane,
    pane_label,
    pane_lines,
    pane_location,
    require_tmux,
    run_tmux,
    session_exists,
    set_pane_label,
    source_tmux_conf,
)
from mozyo_bridge.scaffold.rules import install_rules, rules_status, scaffold_status, write_scaffold
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import LABEL_OPTION, default_queue_path, default_tmux_conf, resolve_repo_root


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
    missing, so `open-here` / `tmux-ui-open` / spawn / ensure / notify paths
    do not block on a missing tmux config. An explicit user-supplied
    `--config-path` still errors when the file is missing.
    """
    optional = bool(getattr(args, "config_path_was_default", False))
    return source_tmux_conf(config_path_from_args(args), optional=optional)


def cmd_list(_: argparse.Namespace) -> int:
    require_tmux()
    print("TARGET\tLOCATION\tPROCESS\tLABEL\tCWD")
    for pane in pane_lines():
        print("\t".join([pane["id"], pane["location"], pane["command"], pane["label"] or "-", pane["cwd"]]))
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


def cmd_name(args: argparse.Namespace) -> int:
    require_tmux()
    target = resolve_target(args.target) if args.target else current_pane()
    set_pane_label(target, args.label)
    print(f"named {target} as {args.label}")
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
    sender_label = pane_label(sender) or sender
    header = f"[mozyo-bridge from:{sender_label} pane:{sender} at:{pane_location(sender)}]"
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


def new_agent_session(agent: str, session: str, cwd: str | None = None) -> str:
    require_tmux()
    if agent not in AGENT_COMMANDS:
        die(f"unsupported agent: {agent}")
    args = ["new-session", "-d", "-s", session, "-P", "-F", "#{pane_id}"]
    if cwd:
        args.extend(["-c", cwd])
    args.append(AGENT_COMMANDS[agent])
    result = run_tmux(*args, check=False)
    if result.returncode != 0:
        die(f"tmux new-session failed: {result.stderr.strip() or result.stdout.strip()}")
    pane_id = result.stdout.strip()
    if not pane_id:
        die("tmux new-session did not return a pane id")
    set_pane_label(pane_id, agent)
    return pane_id


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
    set_pane_label(pane_id, agent)
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
    set_pane_label(pane_id, agent)
    return pane_id


def list_session_windows(session: str) -> list[str]:
    result = run_tmux("list-windows", "-t", session, "-F", "#{window_name}", check=False)
    if result.returncode != 0:
        return []
    return [name.strip() for name in result.stdout.splitlines() if name.strip()]


def session_panes_with_window(session: str) -> list[dict[str, str]]:
    """Return per-pane info including the containing window's name for `session`.

    Distinct from ``pane_lines()`` (which is repo-wide and does not expose the
    window name) — bare ``mozyo`` needs window-name granularity to enforce its
    one-window-per-agent guarantee.
    """
    result = run_tmux(
        "list-panes",
        "-s",
        "-t",
        session,
        "-F",
        "#{window_index}\t#{window_name}\t#{pane_id}\t#{@agent_name}",
        check=False,
    )
    if result.returncode != 0:
        return []
    panes: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = (line.split("\t", 3) + [""] * 4)[:4]
        window_index, window_name, pane_id, label = parts
        panes.append(
            {
                "window_index": window_index,
                "window_name": window_name,
                "id": pane_id,
                "label": label,
            }
        )
    return panes


def detect_legacy_pane_split(session: str) -> list[str]:
    """Describe agent-labeled panes that do not live in their per-agent window.

    A non-empty return means the session was created with the old pane-split
    layout (e.g., `open-here`) and the bare-``mozyo`` window model would
    otherwise be silently violated by reusing it.
    """
    descriptions: list[str] = []
    for pane in session_panes_with_window(session):
        label = pane.get("label") or ""
        window_name = pane.get("window_name") or ""
        if label in ("claude", "codex") and window_name != label:
            location = window_name or pane.get("window_index") or "?"
            descriptions.append(
                f"window={location} pane={pane.get('id')} label={label}"
            )
    return descriptions


def spawn_agent_terminal_pane(
    agent: str,
    cwd: str | None = None,
    vertical: bool = False,
    target: str | None = None,
) -> str:
    require_tmux()
    if agent not in AGENT_COMMANDS:
        die(f"unsupported agent: {agent}")
    split_args = ["split-window", "-P", "-F", "#{pane_id}"]
    if target:
        split_args.extend(["-t", target])
    if not vertical:
        split_args.append("-h")
    if cwd:
        split_args.extend(["-c", cwd])
    split_args.append(AGENT_COMMANDS[agent])
    result = run_tmux(*split_args, check=False)
    if result.returncode != 0:
        die(f"tmux split-window failed: {result.stderr.strip() or result.stdout.strip()}")
    pane_id = result.stdout.strip()
    if not pane_id:
        die("tmux split-window did not return a pane id")
    set_pane_label(pane_id, agent)
    return pane_id


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
    # Receiver TUIs (codex CLI, Claude Code) word-wrap long input at the
    # visible pane width, emitting a literal newline + continuation indent
    # inside the captured text. tmux capture-pane -J only rejoins lines
    # tmux itself wrapped, so a raw substring search would miss a marker
    # split by the TUI wrap even though it landed cleanly on the wire.
    # Try the raw match first (cheap, scrollback-safe); fall back to a
    # wrap-normalized match before declaring the marker absent. Both paths
    # still return False when the marker is genuinely missing, preserving
    # the fail-closed rollback contract.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        captured = capture_pane(target, lines)
        if text in captured:
            return True
        if text in _WRAP_INDENT.sub(" ", captured):
            return True
        time.sleep(0.2)
    return False


def rollback_unsubmitted_input(target: str) -> None:
    cmd_keys(argparse.Namespace(target=target, keys=["C-u"]))


def cmd_spawn(args: argparse.Namespace) -> int:
    if args.config:
        load_tmux_conf_for(args)
    pane_id = spawn_agent_terminal_pane(args.agent, cwd=args.cwd, vertical=args.vertical)
    if args.ready_timeout:
        wait_for_agent_terminal_pane(pane_id, args.agent, args.ready_timeout)
    print(f"spawned {args.agent}: {pane_id}")
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    require_tmux()
    if args.config:
        load_tmux_conf_for(args)
    existing = resolve_agent_label(args.agent, current_session_name())
    if existing:
        ensure_agent_target(existing, args.agent, force=args.force)
        print(f"found {args.agent}: {existing['id']}")
        return 0
    pane_id = spawn_agent_terminal_pane(args.agent, cwd=args.cwd, vertical=args.vertical)
    if args.ready_timeout:
        wait_for_agent_terminal_pane(pane_id, args.agent, args.ready_timeout)
    print(f"spawned {args.agent}: {pane_id}")
    return 0


def cmd_ensure_pair(args: argparse.Namespace) -> int:
    require_tmux()
    config_loaded = False
    if args.config and session_exists(args.session):
        load_tmux_conf_for(args)
        config_loaded = True
    created: list[str] = []
    if not session_exists(args.session):
        claude_pane = new_agent_session("claude", args.session, cwd=args.cwd)
        created.append(f"claude:{claude_pane}")
    if args.config and not config_loaded:
        load_tmux_conf_for(args)
    claude = find_labeled_pane("claude", session=args.session, fallback=False)
    if not claude:
        claude_pane = spawn_agent_terminal_pane("claude", cwd=args.cwd, vertical=args.vertical, target=f"{args.session}:0")
        created.append(f"claude:{claude_pane}")
    codex = find_labeled_pane("codex", session=args.session, fallback=False)
    if not codex:
        codex_pane = spawn_agent_terminal_pane("codex", cwd=args.cwd, vertical=args.vertical, target=f"{args.session}:0")
        created.append(f"codex:{codex_pane}")
    for agent in ["claude", "codex"]:
        pane = find_labeled_pane(agent, session=args.session, fallback=False)
        if pane:
            ensure_agent_target(pane, agent, force=args.force)
            if args.ready_timeout:
                wait_for_agent_terminal_pane(pane["id"], agent, args.ready_timeout)
    run_tmux("select-window", "-t", f"{args.session}:0", check=False)
    result = run_tmux(
        "list-panes",
        "-t",
        f"{args.session}:0",
        "-F",
        "#{pane_id}\t#{pane_index}\t#{pane_current_command}\t#{@agent_name}",
    )
    print(f"session={args.session} created={','.join(created) if created else '-'}")
    print("TARGET\tINDEX\tPROCESS\tLABEL")
    print(result.stdout, end="")
    print(f"attach: tmux attach -t {args.session}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    args.config = True
    args.config_path = config_path_from_args(args)
    return cmd_ensure_pair(args)


def ensure_repo_session_windows(args: argparse.Namespace) -> list[str]:
    """Ensure `args.session` exists with one window per agent (claude, codex).

    Each agent runs in its own tmux window in a single repo-scoped session.
    The window-model guarantee is gated on tmux window names, not pane labels:
    a session reused from the legacy pane-split layout (both agents labeled
    inside one window) is rejected explicitly so the operator can migrate
    instead of attaching to a non-compliant session. Returns the list of newly
    created "agent:pane_id" entries.
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
    legacy_conflicts = detect_legacy_pane_split(args.session)
    if legacy_conflicts:
        die(
            f"session '{args.session}' has agent-labeled panes outside the expected "
            f"`claude` / `codex` windows: {'; '.join(legacy_conflicts)}. "
            "This looks like a legacy pane-split layout. To switch to the bare "
            f"`mozyo` window model, kill the session (`tmux kill-session -t {args.session}`) "
            "and re-run `mozyo`. To keep the pane-split layout, use `mozyo-bridge open-here` instead."
        )
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
                "Re-run from the matching repo root, or use `mozyo-bridge open-here --session NAME` to disambiguate."
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


def cmd_open(args: argparse.Namespace) -> int:
    require_tmux()
    if not session_exists(args.session):
        setup_args = argparse.Namespace(
            session=args.session,
            cwd=args.cwd,
            vertical=args.vertical,
            config=True,
            config_path=config_path_from_args(args),
            config_path_was_default=getattr(args, "config_path_was_default", False),
            ready_timeout=args.ready_timeout,
            force=args.force,
        )
        cmd_ensure_pair(setup_args)
    elif args.config:
        load_tmux_conf_for(args)
    os.execvp("tmux", ["tmux", "attach", "-t", args.session])
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


def cmd_open_here(args: argparse.Namespace) -> int:
    require_tmux()
    repo_root = repo_root_from_args(args)
    user_session = getattr(args, "session", None)
    if not user_session:
        derived = repo_root.name
        if not derived:
            die("could not derive a session name from repo root; pass --session explicitly")
        args.session = derived
    if not getattr(args, "cwd", None):
        args.cwd = str(repo_root)
    if not user_session and session_exists(args.session):
        offending = session_cwd_mismatch(args.session, repo_root)
        if offending:
            die(
                f"session '{args.session}' already exists but its panes are outside repo root "
                f"{repo_root} (cwds: {', '.join(offending)}). "
                "Re-run with an explicit --session to disambiguate; this command will not auto-attach."
            )
    return cmd_open(args)


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
                "#{pane_current_command}\t#{@agent_name}\t#{pane_current_path}",
                check=False,
            )
            print("WINDOW\tNAME\tTARGET\tACTIVE\tPROCESS\tLABEL\tCWD")
            if result.returncode == 0:
                print(result.stdout, end="")
        else:
            print("  no agent windows in this session (compatibility / pane-split path)")
            result = run_tmux(
                "list-panes",
                "-s",
                "-t",
                session,
                "-F",
                "#{pane_id}\t#{window_index}.#{pane_index}\t#{pane_current_command}\t"
                "#{@agent_name}\t#{pane_current_path}",
                check=False,
            )
            print("TARGET\tINDEX\tPROCESS\tLABEL\tCWD")
            if result.returncode == 0:
                print(result.stdout, end="")
        legacy = detect_legacy_pane_split(session)
        if legacy:
            if agent_windows:
                # Some agent windows exist alongside label-vs-window mismatches:
                # genuine mixed state the operator must resolve.
                print(
                    "  legacy pane-split layout detected (mixed: agent window + "
                    "label in non-matching window):"
                )
            else:
                # Pure compat / pane-split session: the layout is operable but
                # the operator may want to migrate to the bare-`mozyo` window
                # model. Informational, not an error.
                print(
                    "  pane-split compat layout (labels resolve, no agent "
                    "windows — `mozyo` migrates to the window model):"
                )
            for entry in legacy:
                print(f"    {entry}")
    else:
        print(f"session: {session} (missing)")
    print("")
    return cmd_doctor(args)


def notify_agent(args: argparse.Namespace, agent: str) -> int:
    require_tmux()
    validate_notify_gate(args)
    task = None if getattr(args, "journal", None) else find_handoff_task(args, agent)
    target_name = args.target or agent
    should_ensure = getattr(args, "ensure", False) and not is_tmux_target(target_name)
    if getattr(args, "config", False):
        load_tmux_conf_for(args)
    if getattr(args, "ensure", False) and not is_tmux_target(target_name) and target_name != agent:
        die("--ensure only supports the default agent label; omit --target or pass an explicit tmux pane id")
    if should_ensure and not resolve_agent_label(agent, current_session_name()):
        pane_id = spawn_agent_terminal_pane(agent, cwd=args.cwd, vertical=args.vertical)
        wait_for_agent_terminal_pane(pane_id, agent, args.ready_timeout)
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


def cmd_notify_codex(args: argparse.Namespace) -> int:
    return notify_agent(args, "codex")


def cmd_notify_claude(args: argparse.Namespace) -> int:
    return notify_agent(args, "claude")


def cmd_notify_codex_review(args: argparse.Namespace) -> int:
    args.type = "review_request"
    return notify_agent(args, "codex")


def cmd_notify_claude_review_result(args: argparse.Namespace) -> int:
    args.type = "review_result"
    return notify_agent(args, "claude")


def cmd_notify_codex_legacy_task(args: argparse.Namespace) -> int:
    args.journal = None
    return notify_agent(args, "codex")


def cmd_notify_claude_legacy_task(args: argparse.Namespace) -> int:
    args.journal = None
    return notify_agent(args, "claude")


def cmd_init(args: argparse.Namespace) -> int:
    require_tmux()
    raw_target = args.target or current_pane()
    if not is_tmux_target(raw_target):
        die(f"init target must be a tmux pane id or location, not a label: {raw_target}")
    resolved = run_tmux("display-message", "-t", raw_target, "-p", "#{pane_id}", check=False)
    if resolved.returncode != 0 or not resolved.stdout.strip():
        die(f"invalid tmux target: {raw_target}")
    target = resolved.stdout.strip()
    target_session = pane_location(target).split(":", 1)[0]

    collisions = [
        pane
        for pane in pane_lines()
        if pane["label"] == args.agent
        and (pane.get("location") or "").split(":", 1)[0] == target_session
        and pane["id"] != target
    ]
    force = bool(getattr(args, "force", False))
    if collisions and not force:
        ids = ", ".join(pane["id"] for pane in collisions)
        die(
            f"pane in session {target_session} already labeled '{args.agent}': {ids}. "
            "Clear that label first or re-run with --force to relabel and unset siblings."
        )
    if force:
        for pane in collisions:
            run_tmux("set-option", "-p", "-t", pane["id"], LABEL_OPTION, "")
    set_pane_label(target, args.agent)
    cleared_note = f" cleared={','.join(p['id'] for p in collisions)}" if collisions else ""
    print(f"initialized {target} as {args.agent}{cleared_note}")
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
