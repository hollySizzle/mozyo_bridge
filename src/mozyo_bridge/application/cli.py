from __future__ import annotations

import argparse

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_config,
    cmd_doctor,
    cmd_ensure,
    cmd_ensure_pair,
    cmd_id,
    cmd_init,
    cmd_keys,
    cmd_list,
    cmd_message,
    cmd_name,
    cmd_notify_claude,
    cmd_notify_claude_legacy_task,
    cmd_notify_claude_review_result,
    cmd_notify_codex,
    cmd_notify_codex_legacy_task,
    cmd_notify_codex_review,
    cmd_open,
    cmd_read,
    cmd_resolve,
    cmd_rules_install,
    cmd_rules_status,
    cmd_scaffold_rules,
    cmd_scaffold_status,
    cmd_setup,
    cmd_spawn,
    cmd_status,
    cmd_type,
)
from mozyo_bridge.shared.paths import default_queue_path, default_tmux_conf, resolve_repo_root


def add_repo_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Project root. Defaults to MOZYO_REPO or the nearest cwd parent with .git/.tmux.conf/pyproject.toml")


def add_scaffold_target_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Project root to scaffold. Defaults to the current working directory")
    parser.add_argument("--target", dest="repo", help="Project root to scaffold. Alias for --repo")


def repo_root_from_args(args: argparse.Namespace):
    return resolve_repo_root(getattr(args, "repo", None))


def normalize_paths(args: argparse.Namespace) -> argparse.Namespace:
    repo_root = repo_root_from_args(args)
    if hasattr(args, "cwd") and args.cwd is None:
        args.cwd = str(repo_root)
    if hasattr(args, "config_path") and args.config_path is None:
        args.config_path = str(default_tmux_conf(repo_root))
    if hasattr(args, "queue") and args.queue is None:
        args.queue = str(default_queue_path(repo_root))
    return args


def add_agent_spawn_options(parser: argparse.ArgumentParser) -> None:
    add_repo_option(parser)
    parser.add_argument("--cwd")
    parser.add_argument("--vertical", action="store_true")
    parser.add_argument("--config", action="store_true", help="Load mozyo-bridge tmux config before running")
    parser.add_argument("--config-path")
    parser.add_argument("--ready-timeout", type=float, default=10.0)


def add_notify_delivery_options(parser: argparse.ArgumentParser, issue_required: bool = False) -> None:
    parser.add_argument("--issue", required=issue_required)
    parser.add_argument("--commit")
    parser.add_argument("--target")
    parser.add_argument("--prompt")
    parser.add_argument("--read-lines", type=int, default=20)
    parser.add_argument("--landing-timeout", type=float, default=5.0)
    parser.add_argument("--submit-delay", type=float, default=0.2, help="Seconds to wait after text is observed before pressing Enter")
    parser.add_argument("--force", action="store_true", help="Allow sending to a non-agent-looking pane")


def add_notify_options(parser: argparse.ArgumentParser, issue_required: bool = False) -> None:
    parser.add_argument("--journal", help="Redmine journal id used as the canonical gate")
    add_notify_delivery_options(parser, issue_required=issue_required)


def add_legacy_notify_options(parser: argparse.ArgumentParser) -> None:
    add_notify_delivery_options(parser, issue_required=True)
    parser.add_argument("--task-id", required=True, help="Retired queue task id used only for legacy cleanup")
    add_repo_option(parser)
    parser.add_argument("--queue", help="Retired queue path used only with --task-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mozyo-bridge",
        description="Redmine-gated pane notification bridge for ClaudeCode/Codex terminals",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("tmux-ui-setup")
    add_repo_option(setup)
    setup.add_argument("--session", default="agents")
    setup.add_argument("--cwd")
    setup.add_argument("--vertical", action="store_true")
    setup.add_argument("--config-path")
    setup.add_argument("--ready-timeout", type=float, default=10.0)
    setup.add_argument("--force", action="store_true", help="Accept existing non-agent-looking labeled panes")
    setup.set_defaults(func=cmd_setup)

    open_cmd = sub.add_parser("tmux-ui-open")
    add_repo_option(open_cmd)
    open_cmd.add_argument("--session", default="agents")
    open_cmd.add_argument("--cwd")
    open_cmd.add_argument("--vertical", action="store_true")
    open_cmd.add_argument("--config", action="store_true", default=True, help="Load mozyo-bridge tmux config before opening")
    open_cmd.add_argument("--config-path")
    open_cmd.add_argument("--ready-timeout", type=float, default=10.0)
    open_cmd.add_argument("--force", action="store_true", help="Accept existing non-agent-looking labeled panes")
    open_cmd.set_defaults(func=cmd_open)

    status = sub.add_parser("status")
    add_repo_option(status)
    status.add_argument("--session", default="agents")
    status.set_defaults(func=cmd_status)

    sub.add_parser("list").set_defaults(func=cmd_list)
    config = sub.add_parser("tmux-ui-config")
    add_repo_option(config)
    config.add_argument("--path")
    config.set_defaults(func=cmd_config)

    sub.add_parser("id").set_defaults(func=cmd_id)

    name = sub.add_parser("name")
    name.add_argument("label")
    name.add_argument("target", nargs="?")
    name.set_defaults(func=cmd_name)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("target")
    resolve.set_defaults(func=cmd_resolve)

    read = sub.add_parser("read")
    read.add_argument("target")
    read.add_argument("lines", type=int, nargs="?", default=50)
    read.set_defaults(func=cmd_read)

    type_cmd = sub.add_parser("type")
    type_cmd.add_argument("target")
    type_cmd.add_argument("text")
    type_cmd.set_defaults(func=cmd_type)

    message = sub.add_parser("message")
    message.add_argument("target")
    message.add_argument("text")
    message.set_defaults(func=cmd_message)

    keys = sub.add_parser("keys")
    keys.add_argument("target")
    keys.add_argument("keys", nargs="+")
    keys.set_defaults(func=cmd_keys)

    spawn = sub.add_parser("tmux-ui-spawn")
    spawn.add_argument("agent", choices=["claude", "codex"])
    add_agent_spawn_options(spawn)
    spawn.set_defaults(func=cmd_spawn)

    ensure = sub.add_parser("tmux-ui-ensure")
    ensure.add_argument("agent", choices=["claude", "codex"])
    add_agent_spawn_options(ensure)
    ensure.add_argument("--force", action="store_true", help="Accept an existing non-agent-looking labeled pane")
    ensure.set_defaults(func=cmd_ensure)

    ensure_pair = sub.add_parser("tmux-ui-ensure-pair")
    add_repo_option(ensure_pair)
    ensure_pair.add_argument("--session", default="agents")
    ensure_pair.add_argument("--cwd")
    ensure_pair.add_argument("--vertical", action="store_true")
    ensure_pair.add_argument("--config", action="store_true", help="Load mozyo-bridge tmux config before ensuring")
    ensure_pair.add_argument("--config-path")
    ensure_pair.add_argument("--ready-timeout", type=float, default=10.0)
    ensure_pair.add_argument("--force", action="store_true", help="Accept existing non-agent-looking labeled panes")
    ensure_pair.set_defaults(func=cmd_ensure_pair)

    for name_, func in [("notify-codex", cmd_notify_codex), ("notify-claude", cmd_notify_claude)]:
        notify = sub.add_parser(name_)
        add_notify_options(notify)
        notify.add_argument("--type")
        notify.set_defaults(func=func)

    for name_, func in [
        ("notify-codex-review", cmd_notify_codex_review),
        ("notify-claude-review-result", cmd_notify_claude_review_result),
    ]:
        notify = sub.add_parser(name_)
        add_notify_options(notify, issue_required=True)
        notify.set_defaults(func=func)

    for name_, func in [
        ("notify-codex-legacy-task", cmd_notify_codex_legacy_task),
        ("notify-claude-legacy-task", cmd_notify_claude_legacy_task),
    ]:
        notify = sub.add_parser(name_)
        add_legacy_notify_options(notify)
        notify.add_argument("--type")
        notify.set_defaults(func=func)

    init = sub.add_parser("init")
    init.add_argument("agent", choices=["claude", "codex"])
    init.add_argument("target", nargs="?")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser(
        "doctor",
        help="Diagnose CLI, central rules, agent skills, and scaffold readiness",
    )
    doctor.add_argument(
        "--target",
        dest="repo",
        help="Project root to check for scaffold and Claude project-skill readiness. "
        "Defaults to MOZYO_REPO or the current working directory.",
    )
    doctor.add_argument(
        "--repo",
        dest="repo",
        help="Alias for --target.",
    )
    doctor.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    doctor.set_defaults(func=cmd_doctor)

    rules = sub.add_parser("rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_install = rules_sub.add_parser("install")
    rules_install.add_argument("--home", help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge")
    rules_install.set_defaults(func=cmd_rules_install)
    rules_status = rules_sub.add_parser("status")
    rules_status.add_argument("--home", help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge")
    rules_status.set_defaults(func=cmd_rules_status)

    scaffold = sub.add_parser("scaffold")
    scaffold_sub = scaffold.add_subparsers(dest="scaffold_command", required=True)
    scaffold_rules = scaffold_sub.add_parser("rules")
    scaffold_rules.add_argument("preset", choices=["asana", "redmine", "none"])
    add_scaffold_target_option(scaffold_rules)
    scaffold_rules.add_argument("--home", help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge")
    scaffold_rules.add_argument("--dry-run", action="store_true")
    replace_group = scaffold_rules.add_mutually_exclusive_group()
    replace_group.add_argument("--backup", action="store_true", help="Back up existing scaffold files before replacing them")
    replace_group.add_argument("--force", action="store_true", help="Replace existing scaffold files without backup")
    scaffold_rules.set_defaults(func=cmd_scaffold_rules)

    scaffold_status = scaffold_sub.add_parser("status")
    add_scaffold_target_option(scaffold_status)
    scaffold_status.add_argument("--home", help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge")
    scaffold_status.add_argument("--json", action="store_true", help="Emit structured JSON output instead of human-readable text")
    scaffold_status.set_defaults(func=cmd_scaffold_status)
    return parser


def main() -> int:
    args = normalize_paths(build_parser().parse_args())
    return args.func(args)
