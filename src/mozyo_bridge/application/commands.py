from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

from mozyo_bridge.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.domain.pane_resolver import (
    AGENT_COMMANDS,
    clear_read,
    current_pane,
    current_session_name,
    ensure_agent_target,
    find_labeled_pane,
    is_agent_process,
    is_tmux_target,
    mark_read,
    pane_info,
    require_read,
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


def wait_for_text(target: str, text: str, lines: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if text in capture_pane(target, lines):
            return True
        time.sleep(0.2)
    return False


def rollback_unsubmitted_input(target: str) -> None:
    cmd_keys(argparse.Namespace(target=target, keys=["C-u"]))


def cmd_spawn(args: argparse.Namespace) -> int:
    if args.config:
        source_tmux_conf(config_path_from_args(args))
    pane_id = spawn_agent_terminal_pane(args.agent, cwd=args.cwd, vertical=args.vertical)
    if args.ready_timeout:
        wait_for_agent_terminal_pane(pane_id, args.agent, args.ready_timeout)
    print(f"spawned {args.agent}: {pane_id}")
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    require_tmux()
    if args.config:
        source_tmux_conf(config_path_from_args(args))
    existing = find_labeled_pane(args.agent, session=current_session_name(), fallback=False)
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
        source_tmux_conf(config_path_from_args(args))
        config_loaded = True
    created: list[str] = []
    if not session_exists(args.session):
        claude_pane = new_agent_session("claude", args.session, cwd=args.cwd)
        created.append(f"claude:{claude_pane}")
    if args.config and not config_loaded:
        source_tmux_conf(config_path_from_args(args))
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


def cmd_open(args: argparse.Namespace) -> int:
    require_tmux()
    if not session_exists(args.session):
        setup_args = argparse.Namespace(
            session=args.session,
            cwd=args.cwd,
            vertical=args.vertical,
            config=True,
            config_path=config_path_from_args(args),
            ready_timeout=args.ready_timeout,
            force=args.force,
        )
        cmd_ensure_pair(setup_args)
    elif args.config:
        source_tmux_conf(config_path_from_args(args))
    os.execvp("tmux", ["tmux", "attach", "-t", args.session])
    raise AssertionError("unreachable")


def cmd_status(args: argparse.Namespace) -> int:
    require_tmux()
    session = args.session
    if session_exists(session):
        print(f"session: {session}")
        result = run_tmux(
            "list-panes",
            "-t",
            f"{session}:0",
            "-F",
            "#{pane_id}\t#{pane_index}\t#{pane_current_command}\t#{@agent_name}\t#{pane_current_path}",
        )
        print("TARGET\tINDEX\tPROCESS\tLABEL\tCWD")
        print(result.stdout, end="")
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
        source_tmux_conf(config_path_from_args(args))
    if getattr(args, "ensure", False) and not is_tmux_target(target_name) and target_name != agent:
        die("--ensure only supports the default agent label; omit --target or pass an explicit tmux pane id")
    if should_ensure and not find_labeled_pane(target_name, session=current_session_name(), fallback=False):
        pane_id = spawn_agent_terminal_pane(agent, cwd=args.cwd, vertical=args.vertical)
        wait_for_agent_terminal_pane(pane_id, agent, args.ready_timeout)
    target_info = pane_info(target_name)
    ensure_agent_target(target_info, agent, force=args.force)
    target = target_info["id"]
    read_lines = str(args.read_lines)
    cmd_read(argparse.Namespace(target=target, lines=args.read_lines))
    prompt = build_prompt(args, agent, task)
    cmd_message(argparse.Namespace(target=target, text=prompt))
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
    target = args.target or current_pane()
    run_tmux("set-option", "-p", "-t", target, LABEL_OPTION, args.agent)
    print(f"initialized {target} as {args.agent}")
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
    ok = True
    if subprocess.run(["sh", "-c", "command -v tmux >/dev/null 2>&1"]).returncode != 0:
        print("tmux: missing")
        return 1
    print("tmux: ok")
    print(f"TMUX_PANE: {os.environ.get('TMUX_PANE', '')}")
    result = run_tmux("list-panes", "-a", "-F", "#{pane_id} #{@agent_name}", check=False)
    if result.returncode != 0:
        print(f"tmux list-panes: failed: {result.stderr.strip()}")
        ok = False
    else:
        labeled = [line for line in result.stdout.splitlines() if len(line.split(" ", 1)) == 2 and line.split(" ", 1)[1]]
        print(f"panes: {len(result.stdout.splitlines())}")
        print(f"labeled_panes: {len(labeled)}")
        panes = pane_lines()
        for agent in ["claude", "codex"]:
            matches = [pane for pane in panes if pane["label"] == agent]
            if not matches:
                print(f"{agent}_pane: missing")
                ok = False
            elif len(matches) > 1:
                print(f"{agent}_pane: duplicate ({len(matches)})")
                ok = False
            else:
                pane = matches[0]
                command = Path(pane["command"]).name
                status = "ok" if is_agent_process(command) else "not-agent-process"
                print(f"{agent}_pane: {pane['id']} process={command} status={status}")
                if agent == "claude":
                    repo_root = repo_root_from_args(args)
                    if (repo_root / ".claude" / "skills").exists() and not cwd_is_under_repo(pane.get("cwd", ""), repo_root):
                        print(
                            "warning: claude_pane cwd is outside repo root; "
                            "project skills may not resolve. "
                            f"cwd={pane.get('cwd', '') or '-'} repo={repo_root}"
                        )
                        ok = False
    queue = queue_path_from_args(args)
    print(f"queue: {queue} ({'exists' if queue.exists() else 'missing'})")
    return 0 if ok else 1


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
