from __future__ import annotations

import argparse

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_config,
    cmd_doctor,
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_id,
    cmd_init,
    cmd_keys,
    cmd_list,
    cmd_message,
    cmd_mozyo,
    cmd_notify_claude,
    cmd_notify_claude_legacy_task,
    cmd_notify_claude_review_result,
    cmd_notify_codex,
    cmd_notify_codex_legacy_task,
    cmd_notify_codex_review,
    cmd_read,
    cmd_resolve,
    cmd_rules_install,
    cmd_rules_status,
    cmd_scaffold_rules,
    cmd_scaffold_status,
    cmd_status,
    cmd_type,
)
from mozyo_bridge.domain.handoff import (
    KIND_LABELS,
    MODE_STANDARD,
    MODES,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
    SOURCES,
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
    if hasattr(args, "config_path"):
        args.config_path_was_default = args.config_path is None
        if args.config_path is None:
            args.config_path = str(default_tmux_conf(repo_root))
    if hasattr(args, "queue") and args.queue is None:
        args.queue = str(default_queue_path(repo_root))
    return args


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
    # The standard notify-* wrappers route through `orchestrate_handoff` so
    # they accept the same record knobs as `mozyo-bridge handoff send/reply`.
    # Legacy queue notify-* commands stay on `notify_agent` and intentionally
    # do not expose these flags.
    parser.add_argument(
        "--record-format",
        dest="record_format",
        choices=sorted(RECORD_FORMATS),
        default=RECORD_FORMAT_BOTH,
        help=(
            "Format of the durable delivery-record emitted alongside the "
            "structured outcome. Defaults to `both`; pass `json` for the "
            "prior single-line JSON shape that scripts expect."
        ),
    )
    parser.add_argument(
        "--record-command",
        dest="record_command",
        help=(
            "Optional literal command string included in the generated "
            "delivery record under `- Command:` for audit replay."
        ),
    )


def add_legacy_notify_options(parser: argparse.ArgumentParser) -> None:
    add_notify_delivery_options(parser, issue_required=True)
    parser.add_argument("--task-id", required=True, help="Retired queue task id used only for legacy cleanup")
    add_repo_option(parser)
    parser.add_argument("--queue", help="Retired queue path used only with --task-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mozyo-bridge",
        description=(
            "Repo-aware tmux session bootstrap plus Asana/Redmine-gated pane "
            "notification bridge for ClaudeCode/Codex terminals. "
            "Run with no subcommand to ensure a repo-scoped session with "
            "claude/codex windows and attach."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--no-attach",
        action="store_true",
        default=False,
        dest="no_attach",
        help="Bare `mozyo`: ensure the repo session and agent windows but do not attach. Ignored when a subcommand is given.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "Bare `mozyo`: override the repo root resolution (otherwise MOZYO_REPO env "
            "or a `.git` / `.tmux.conf` / `pyproject.toml` parent of the cwd). "
            "Subcommands accept their own `--repo` after the subcommand name."
        ),
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "Bare `mozyo`: override the tmux session name. Defaults to the repo "
            "root basename; pass an explicit name to disambiguate when two repos "
            "share a basename."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=False)

    status = sub.add_parser("status")
    add_repo_option(status)
    status.add_argument(
        "--session",
        default=None,
        help=(
            "Tmux session to describe. Defaults to the current session when "
            "run inside tmux, or the repo basename (bare-`mozyo` window model)."
        ),
    )
    status.set_defaults(func=cmd_status)

    sub.add_parser("list").set_defaults(func=cmd_list)
    config = sub.add_parser("tmux-ui-config")
    add_repo_option(config)
    config.add_argument("--path")
    config.set_defaults(func=cmd_config)

    sub.add_parser("id").set_defaults(func=cmd_id)

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
    message.add_argument(
        "--no-submit",
        dest="submit",
        action="store_false",
        help="Type the message but do not press Enter; leave the input pending at the target prompt",
    )
    message.add_argument(
        "--landing-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for the header marker to appear in the target pane before pressing Enter",
    )
    message.add_argument(
        "--submit-delay",
        type=float,
        default=0.2,
        help="Seconds to wait after the marker is observed before pressing Enter",
    )
    message.add_argument(
        "--read-lines",
        type=int,
        default=50,
        help="Number of pane lines to inspect when waiting for the header marker",
    )
    message.set_defaults(func=cmd_message, submit=True)

    keys = sub.add_parser("keys")
    keys.add_argument("target")
    keys.add_argument("keys", nargs="+")
    keys.set_defaults(func=cmd_keys)

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

    def configure_handoff_parser(
        parser_: argparse.ArgumentParser,
        *,
        kind_required: bool,
    ) -> None:
        parser_.add_argument("--to", required=True, choices=["claude", "codex"], help="Semantic receiver agent")
        parser_.add_argument("--source", required=True, choices=sorted(SOURCES), help="Durable record source system")
        parser_.add_argument(
            "--kind",
            required=kind_required,
            choices=sorted(KIND_LABELS),
            help="Durable intent label. Required for `handoff send`; defaults to `reply` for `handoff reply` / `reply`",
        )
        parser_.add_argument("--task-id", dest="task_id", help="Asana task gid (source=asana)")
        parser_.add_argument("--comment-id", dest="comment_id", help="Asana story/comment gid (source=asana)")
        parser_.add_argument(
            "--anchor-url",
            dest="anchor_url",
            help="Asana task permalink + comment timestamp/context when a stable comment id is unavailable",
        )
        parser_.add_argument("--issue", help="Redmine issue id (source=redmine)")
        parser_.add_argument("--journal", help="Redmine journal id (source=redmine)")
        parser_.add_argument(
            "--target",
            help="Optional tmux target override; defaults to same-session agent-window resolution from --to",
        )
        parser_.add_argument(
            "--mode",
            choices=sorted(MODES),
            default=MODE_STANDARD,
            help="`standard` types and presses Enter after the landing marker; `pending` types but leaves the input pending",
        )
        parser_.add_argument(
            "--summary",
            help="Optional short hint appended to the generated notification; required for --kind custom",
        )
        parser_.add_argument(
            "--force",
            action="store_true",
            help="Allow sending to a non-agent-looking pane",
        )
        parser_.add_argument("--landing-timeout", dest="landing_timeout", type=float, default=5.0)
        parser_.add_argument("--submit-delay", dest="submit_delay", type=float, default=0.2)
        parser_.add_argument("--read-lines", dest="read_lines", type=int, default=50)
        parser_.add_argument(
            "--record-format",
            dest="record_format",
            choices=sorted(RECORD_FORMATS),
            default=RECORD_FORMAT_BOTH,
            help=(
                "Format of the durable delivery-record emitted alongside the "
                "structured outcome. `both` (default) prints the markdown "
                "record then the JSON outcome; `text` prints only the markdown "
                "record; `json` preserves the prior single-line JSON shape."
            ),
        )
        parser_.add_argument(
            "--record-command",
            dest="record_command",
            help=(
                "Optional literal command string included in the generated "
                "delivery record under `- Command:` for audit replay."
            ),
        )

    handoff = sub.add_parser(
        "handoff",
        help="High-level cross-agent notification primitive anchored at a durable record",
    )
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_send = handoff_sub.add_parser("send", help="Send a handoff notification from sender to receiver")
    configure_handoff_parser(handoff_send, kind_required=True)
    handoff_send.set_defaults(func=cmd_handoff_send)

    handoff_reply = handoff_sub.add_parser(
        "reply",
        help="Send a reply notification from sender to receiver (kind defaults to `reply`)",
    )
    configure_handoff_parser(handoff_reply, kind_required=False)
    handoff_reply.set_defaults(func=cmd_handoff_reply)

    reply_alias = sub.add_parser(
        "reply",
        help="Alias for `mozyo-bridge handoff reply` (kind defaults to `reply`)",
    )
    configure_handoff_parser(reply_alias, kind_required=False)
    reply_alias.set_defaults(func=cmd_handoff_reply)

    init = sub.add_parser(
        "init",
        help=(
            "Rename the target pane's tmux window to the agent name so it "
            "becomes resolvable as `claude` / `codex`. Defaults to the "
            "current pane when no target is given."
        ),
    )
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
    args = build_parser().parse_args()
    if not getattr(args, "command", None):
        return cmd_mozyo(args)
    args = normalize_paths(args)
    return args.func(args)
