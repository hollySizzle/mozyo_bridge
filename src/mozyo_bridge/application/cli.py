from __future__ import annotations

import argparse

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_agents_list,
    cmd_config,
    cmd_doctor,
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_id,
    cmd_init,
    cmd_instruction_doctor,
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
    cmd_rules_home,
    cmd_rules_install,
    cmd_rules_status,
    cmd_docs_audit_impact,
    cmd_docs_generate,
    cmd_docs_resolve,
    cmd_docs_validate,
    cmd_scaffold_apply,
    cmd_scaffold_canonical,
    cmd_scaffold_diff,
    cmd_scaffold_status,
    cmd_workspace_defaults,
    cmd_status,
    cmd_tmux_ui_install,
    cmd_tmux_ui_status,
    cmd_tmux_ui_uninstall,
    cmd_type,
)
from mozyo_bridge.application.instruction_doctor import (
    KNOWN_PROFILES,
    PROFILE_REDMINE_CODEX,
)
from mozyo_bridge.application.release import (
    cmd_release_bump,
    cmd_release_check_artifact,
    cmd_release_check_drift,
    cmd_release_check_scaffold,
    cmd_release_check_tree,
    cmd_release_check_workflow,
    cmd_release_publish,
    cmd_release_workflow_runs,
    cmd_release_workflow_wait,
)
from mozyo_bridge.domain.agent_discovery import AGENT_KINDS
from mozyo_bridge.domain.handoff import (
    KIND_LABELS,
    MODE_QUEUE_ENTER,
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
    parser.add_argument(
        "--landing-timeout",
        type=float,
        default=8.0,
        help=(
            "Seconds to wait for the header marker to render in the target "
            "pane before pressing Enter. Larger values absorb Claude/Codex "
            "TUI redraw delay; the command proceeds as soon as the marker is "
            "observed. Marker observation is not a delivery guarantee."
        ),
    )
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

    agents = sub.add_parser(
        "agents",
        help=(
            "Cross-workspace agent discovery (Redmine #10332). Read-only "
            "structured surface of every tmux pane carrying session, window, "
            "pane id, process, cwd, inferred repo_root, and classified agent "
            "kind. Use before issuing a Codex-gated cross-workspace handoff "
            "with `mozyo-bridge handoff send`."
        ),
    )
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)

    agents_list = agents_sub.add_parser(
        "list",
        help=(
            "Enumerate every tmux pane with structured discovery fields. "
            "Does not modify tmux state; safe to call from any session. "
            "Distinct from `mozyo-bridge list` (raw single-session pane "
            "table) and `mozyo-bridge status` (current session diagnostics)."
        ),
    )
    agents_list.add_argument(
        "--session",
        help=(
            "Filter to panes whose tmux session matches this name exactly. "
            "Omit to enumerate every visible session."
        ),
    )
    agents_list.add_argument(
        "--agent",
        choices=sorted(AGENT_KINDS),
        help=(
            "Filter by classified agent kind. `claude` and `codex` match "
            "panes whose tmux window name equals that agent label "
            "(the window-only model identity rail); `unknown` matches every "
            "other pane. Omit the filter to list all panes."
        ),
    )
    agents_list.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output instead of the tab-separated table.",
    )
    agents_list.set_defaults(func=cmd_agents_list)

    config = sub.add_parser("tmux-ui-config")
    add_repo_option(config)
    config.add_argument("--path")
    config.set_defaults(func=cmd_config)

    tmux_ui = sub.add_parser(
        "tmux-ui",
        help=(
            "Host-side wiring helper for the governed preset's "
            "`.mozyo-bridge/tmux/agent-ui.conf` snippet. Adds or removes "
            "a managed source-file block in the host tmux config "
            "(default ~/.tmux.conf) without touching surrounding settings."
        ),
    )
    tmux_ui_sub = tmux_ui.add_subparsers(dest="tmux_ui_command", required=True)

    def _add_tmux_ui_common(parser_: argparse.ArgumentParser, *, include_repo: bool = True) -> None:
        if include_repo:
            parser_.add_argument(
                "--repo",
                help=(
                    "Repo root that ships the `.mozyo-bridge/tmux/agent-ui.conf` "
                    "snippet. Defaults to MOZYO_REPO or the current working "
                    "directory."
                ),
            )
            parser_.add_argument(
                "--target",
                dest="repo",
                help="Alias for --repo.",
            )
        parser_.add_argument(
            "--tmux-conf",
            dest="tmux_conf",
            help=(
                "Host tmux config file to edit. Defaults to ~/.tmux.conf. "
                "Only the managed block (between mozyo-bridge tmux-ui markers) "
                "is created, replaced, or removed; surrounding settings stay "
                "untouched."
            ),
        )

    tmux_ui_install = tmux_ui_sub.add_parser(
        "install",
        help=(
            "Insert a managed `source-file` block for the repo's "
            "agent-ui.conf snippet into the host tmux config. Idempotent on "
            "the same repo path; --force replaces a block pointing elsewhere."
        ),
    )
    _add_tmux_ui_common(tmux_ui_install)
    tmux_ui_install.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show the planned change without writing to the host tmux config.",
    )
    tmux_ui_install.add_argument(
        "--backup",
        action="store_true",
        help=(
            "Copy the current tmux config to "
            "`<path>.bak.<timestamp>` before writing the new content."
        ),
    )
    tmux_ui_install.add_argument(
        "--force",
        action="store_true",
        help=(
            "Replace an existing managed block that points to a different "
            "snippet path (drift). Without --force the command exits with a "
            "drift error so the operator confirms the intent."
        ),
    )
    tmux_ui_install.set_defaults(func=cmd_tmux_ui_install)

    tmux_ui_uninstall = tmux_ui_sub.add_parser(
        "uninstall",
        help=(
            "Remove the managed `mozyo-bridge tmux-ui` block from the host "
            "tmux config. Leaves surrounding content untouched and is a "
            "no-op when the block is not present."
        ),
    )
    _add_tmux_ui_common(tmux_ui_uninstall, include_repo=False)
    tmux_ui_uninstall.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show the planned removal without writing to the host tmux config.",
    )
    tmux_ui_uninstall.add_argument(
        "--backup",
        action="store_true",
        help="Copy the tmux config to `<path>.bak.<timestamp>` before removal.",
    )
    tmux_ui_uninstall.set_defaults(func=cmd_tmux_ui_uninstall)

    tmux_ui_status = tmux_ui_sub.add_parser(
        "status",
        help=(
            "Report whether the host tmux config currently wires the repo's "
            "agent-ui.conf snippet: not-installed / installed / drift."
        ),
    )
    _add_tmux_ui_common(tmux_ui_status)
    tmux_ui_status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output instead of human-readable text.",
    )
    tmux_ui_status.set_defaults(func=cmd_tmux_ui_status)

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
        default=8.0,
        help=(
            "Seconds to wait for the header marker to render in the target "
            "pane before pressing Enter. Larger values absorb Claude/Codex "
            "TUI redraw delay; the command proceeds as soon as the marker is "
            "observed. Marker observation is not a delivery guarantee. "
            "Claude TUI redraw-delay environments may also want "
            "`--submit-delay 0.5`."
        ),
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
    message.add_argument(
        "--attempt",
        dest="attempt",
        type=int,
        default=None,
        help=(
            "Optional retry counter for the per-preset `--no-submit` retry "
            "budget. Pass `--attempt N` on each retry so gate-failure stderr "
            "trailers can report `N/cap` remaining accurately; omit on the "
            "first call. Counter is operator-tracked because the CLI is "
            "stateless across invocations."
        ),
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
            "--target-repo",
            dest="target_repo",
            help=(
                "Optional cross-workspace gate (Redmine #10332): the target "
                "pane's cwd must resolve to this repo root, otherwise the "
                "handoff is rejected with `target_repo_mismatch`. Use when "
                "the sender wants to assert which workspace the target lives "
                "in before delivery. Drop the flag to skip the repo gate."
            ),
        )
        parser_.add_argument(
            "--mode",
            choices=sorted(MODES),
            default=MODE_QUEUE_ENTER,
            help=(
                "`queue-enter` (default since v0.4; Claude/Codex agent "
                "panes only, --force not allowed) types and presses "
                "Enter regardless of marker observation, emitting "
                "reason=queue_enter on marker miss without rollback; "
                "`standard` (strict explicit fallback) types and presses "
                "Enter after the landing marker, with C-u rollback on "
                "marker timeout; "
                "`pending` types but leaves the input pending"
            ),
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
        parser_.add_argument(
            "--landing-timeout",
            dest="landing_timeout",
            type=float,
            default=8.0,
            help=(
                "Seconds to wait for the landing marker to render before "
                "pressing Enter. Larger values absorb Claude/Codex TUI redraw "
                "delay; delivery proceeds as soon as the marker is observed."
            ),
        )
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

    instruction = sub.add_parser(
        "instruction",
        help="Opt-in checks for repo-local LLM runtime config (read-only)",
    )
    instruction_sub = instruction.add_subparsers(
        dest="instruction_command", required=True
    )
    instruction_doctor = instruction_sub.add_parser(
        "doctor",
        help=(
            "Profile-aware, read-only check that a Redmine/Codex workspace "
            "carries the repo-root runtime config the bootstrap docs require "
            "(`<repo>/.codex/config.toml`, optional `<repo>/.mcp.json`). Does "
            "not call the network, autogenerate, or write home config."
        ),
    )
    instruction_doctor.add_argument(
        "--target",
        dest="target",
        help="Project root to check. Defaults to MOZYO_REPO or the current "
        "working directory.",
    )
    instruction_doctor.add_argument(
        "--repo",
        dest="target",
        help="Alias for --target.",
    )
    instruction_doctor.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Config profile to check. Only `redmine-codex` is defined today; "
        "other presets are intentionally not failed by this command.",
    )
    instruction_doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    instruction_doctor.set_defaults(func=cmd_instruction_doctor)

    rules = sub.add_parser("rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_install = rules_sub.add_parser("install")
    rules_install_store = rules_install.add_mutually_exclusive_group()
    rules_install_store.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    rules_install_store.add_argument(
        "--repo-local",
        dest="repo_local",
        metavar="REPO",
        help=(
            "Install central preset rules into REPO/.mozyo-bridge/rules/presets/ "
            "instead of the user home. Use this for Dev Container / "
            "ephemeral-home workspaces where ~/.mozyo_bridge is not persisted. "
            "Mutually exclusive with --home."
        ),
    )
    rules_install.set_defaults(func=cmd_rules_install)
    rules_status = rules_sub.add_parser("status")
    rules_status_store = rules_status.add_mutually_exclusive_group()
    rules_status_store.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    rules_status_store.add_argument(
        "--repo-local",
        dest="repo_local",
        metavar="REPO",
        help=(
            "Read the rules store from REPO/.mozyo-bridge instead of the user "
            "home. Mutually exclusive with --home."
        ),
    )
    rules_status.set_defaults(func=cmd_rules_status)
    rules_home_help = (
        "Print the mozyo-bridge home root. Default output is the portable "
        "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` expression safe to paste "
        "into committed docs. Use --resolved to expand the env override "
        "and `~` for local diagnostics; that output may contain the "
        "operator's $HOME and must not be committed."
    )
    rules_home = rules_sub.add_parser(
        "home",
        help=rules_home_help,
        description=rules_home_help,
    )
    rules_home.add_argument(
        "--resolved",
        action="store_true",
        help=(
            "Print the resolved absolute path honoring MOZYO_BRIDGE_HOME "
            "and expanding `~`. Intended for local debugging only; do not "
            "paste the output into committed documents."
        ),
    )
    rules_home.set_defaults(func=cmd_rules_home)

    scaffold = sub.add_parser(
        "scaffold",
        help=(
            "Generate, inspect, and audit the project routers + manifest for "
            "a ticket-system preset. Use `apply` to write, `diff` to preview, "
            "and `status` to detect drift."
        ),
    )
    scaffold_sub = scaffold.add_subparsers(dest="scaffold_command", required=True)
    from mozyo_bridge.scaffold.rules import PRESETS

    scaffold_apply = scaffold_sub.add_parser(
        "apply",
        help=(
            "Write `AGENTS.md`, `CLAUDE.md`, and the scaffold manifest for "
            "the chosen preset into the target workspace. Use `scaffold diff "
            "<preset>` first to preview the change."
        ),
    )
    scaffold_apply.add_argument("preset", choices=PRESETS)
    add_scaffold_target_option(scaffold_apply)
    apply_store_group = scaffold_apply.add_mutually_exclusive_group()
    apply_store_group.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    apply_store_group.add_argument(
        "--repo-local",
        dest="repo_local",
        action="store_true",
        help=(
            "Read the rules store from the target repo's `.mozyo-bridge/` "
            "directory and embed a repo-local `rule_path` in the generated "
            "routers and manifest. Use this for Dev Container / "
            "ephemeral-home workspaces. Run `mozyo-bridge rules install "
            "--repo-local <target>` first to populate that store. Mutually "
            "exclusive with --home."
        ),
    )
    scaffold_apply.add_argument("--dry-run", action="store_true")
    apply_replace_group = scaffold_apply.add_mutually_exclusive_group()
    apply_replace_group.add_argument("--backup", action="store_true", help="Back up existing scaffold files before replacing them")
    apply_replace_group.add_argument("--force", action="store_true", help="Replace existing scaffold files without backup")
    scaffold_apply.add_argument(
        "--skip-tmux-ui",
        dest="skip_tmux_ui",
        action="store_true",
        help=(
            "Omit the governed preset's `.mozyo-bridge/tmux/` artifacts "
            "(agent-window status colouring snippet). The artifacts are "
            "default-on; pass this flag when the project does not want "
            "the tmux UI helper installed."
        ),
    )
    scaffold_apply.add_argument(
        "--skip-nagger",
        dest="skip_nagger",
        action="store_true",
        help=(
            "Omit the governed preset's `.claude-nagger/` artifacts "
            "(Claude Nagger config / convention skeletons). The artifacts "
            "are default-on; pass this flag when the project does not "
            "use Claude Nagger."
        ),
    )
    scaffold_apply.set_defaults(func=cmd_scaffold_apply)

    scaffold_diff = scaffold_sub.add_parser(
        "diff",
        help=(
            "Print a unified diff of what `scaffold apply <preset>` would "
            "change in the target workspace. Exit 0 when clean, exit 1 when "
            "the workspace would change."
        ),
    )
    scaffold_diff.add_argument("preset", choices=PRESETS)
    add_scaffold_target_option(scaffold_diff)
    diff_store_group = scaffold_diff.add_mutually_exclusive_group()
    diff_store_group.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    diff_store_group.add_argument(
        "--repo-local",
        dest="repo_local",
        action="store_true",
        help=(
            "Preview against the target repo's `.mozyo-bridge/` rules store "
            "and embed a repo-local `rule_path`. Mutually exclusive with --home."
        ),
    )
    scaffold_diff.add_argument(
        "--skip-tmux-ui",
        dest="skip_tmux_ui",
        action="store_true",
        help="Preview the diff as if `scaffold apply --skip-tmux-ui` were run.",
    )
    scaffold_diff.add_argument(
        "--skip-nagger",
        dest="skip_nagger",
        action="store_true",
        help="Preview the diff as if `scaffold apply --skip-nagger` were run.",
    )
    scaffold_diff.set_defaults(func=cmd_scaffold_diff)

    scaffold_canonical = scaffold_sub.add_parser(
        "canonical",
        help=(
            "Render or drift-check the canonical-sourced router templates. "
            "Operates on the mozyo-bridge source tree (`--repo`, default cwd); "
            "use `render` to regenerate `_router/AGENTS.md` and `_router/CLAUDE.md` "
            "from `scaffold/canonical_sources/router.yaml`, or `--check` to "
            "verify the committed outputs match (exit 1 on drift)."
        ),
    )
    add_repo_option(scaffold_canonical)
    scaffold_canonical.add_argument(
        "--check",
        action="store_true",
        help=(
            "Re-render every canonical source in memory and compare against "
            "the committed output. Exit 1 on drift; writes nothing."
        ),
    )
    scaffold_canonical.set_defaults(func=cmd_scaffold_canonical)

    scaffold_status = scaffold_sub.add_parser("status")
    add_scaffold_target_option(scaffold_status)
    scaffold_status.add_argument(
        "--home",
        help=(
            "mozyo-bridge home for central-mode manifests. Defaults to "
            "MOZYO_BRIDGE_HOME or ~/.mozyo_bridge. Rejected against "
            "repo-local manifests (the rules store is the target repo's "
            ".mozyo-bridge); rerun without --home."
        ),
    )
    scaffold_status.add_argument("--json", action="store_true", help="Emit structured JSON output instead of human-readable text")
    scaffold_status.set_defaults(func=cmd_scaffold_status)

    docs = sub.add_parser(
        "docs",
        help=(
            "Docs catalog tooling for governed scaffolds. Replaces the "
            "Python source previously vendor-copied to the target repo "
            "under `.mozyo-bridge/tools/`; the same logic now ships in "
            "the mozyo-bridge package so upgrades follow the CLI."
        ),
    )
    docs_sub = docs.add_subparsers(dest="docs_command", required=True)

    def _add_docs_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--repo",
            help=(
                "Target project root. Defaults to the cwd. The catalog is "
                "resolved relative to this root."
            ),
        )
        parser.add_argument(
            "--catalog",
            help=(
                "Catalog YAML path. Defaults to "
                "`<repo>/.mozyo-bridge/docs/catalog.yaml`."
            ),
        )

    docs_validate = docs_sub.add_parser(
        "validate",
        help="Validate the docs catalog (structure, refs, canonical paths, coverage roots).",
    )
    _add_docs_common(docs_validate)
    docs_validate.add_argument(
        "--strict-metadata",
        action="store_true",
        help="Require purpose / audit_role / related_document_refs on active rule/spec/task documents.",
    )
    docs_validate.add_argument(
        "--check-file-coverage",
        action="store_true",
        help="Require source files under coverage roots to match at least one file_convention.",
    )
    docs_validate.add_argument(
        "--coverage-root",
        action="append",
        default=None,
        help=(
            "Override the catalog / default coverage roots. Repeatable. "
            "CLI takes precedence over the catalog's `coverage_roots`."
        ),
    )
    docs_validate.set_defaults(func=cmd_docs_validate)

    docs_resolve = docs_sub.add_parser(
        "resolve",
        help="Resolve active docs for one or more changed paths.",
    )
    _add_docs_common(docs_resolve)
    docs_resolve.add_argument(
        "paths",
        nargs="+",
        help="Repository-relative or absolute file paths to resolve.",
    )
    docs_resolve.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format (default: text).",
    )
    docs_resolve.set_defaults(func=cmd_docs_resolve)

    docs_generate = docs_sub.add_parser(
        "generate-file-conventions",
        help="Render the catalog's file_conventions to a generated YAML.",
    )
    _add_docs_common(docs_generate)
    docs_generate.add_argument(
        "--output",
        help=(
            "Generated YAML path. Defaults to "
            "`<repo>/.mozyo-bridge/docs/file_conventions.generated.yaml`."
        ),
    )
    docs_generate.add_argument(
        "--check",
        action="store_true",
        help="Verify the recorded output matches the catalog; exit 1 on drift.",
    )
    docs_generate.set_defaults(func=cmd_docs_generate)

    docs_impact = docs_sub.add_parser(
        "audit-impact",
        help="Resolve docs for git-changed paths and optionally drift-check the generated file.",
    )
    _add_docs_common(docs_impact)
    impact_scope = docs_impact.add_mutually_exclusive_group()
    impact_scope.add_argument("--staged", action="store_true", help="Use staged changes only.")
    impact_scope.add_argument(
        "--all-changed",
        dest="all_changed",
        action="store_true",
        help="Use staged + unstaged + untracked changes.",
    )
    docs_impact.add_argument(
        "--check-generated",
        dest="check_generated",
        action="store_true",
        help="Also run the generate-file-conventions drift check.",
    )
    docs_impact.add_argument(
        "--generated-output",
        dest="generated_output",
        help=(
            "Override the generated file path for --check-generated. "
            "Defaults to the same path as `docs generate-file-conventions`."
        ),
    )
    docs_impact.set_defaults(func=cmd_docs_audit_impact)

    workspace_defaults = sub.add_parser(
        "workspace-defaults",
        help=(
            "Render or drift-check the workspace-local Redmine default-"
            "project snippet (Redmine #10689). Single source is "
            "`<repo>/.mozyo-bridge/workspace-defaults.yaml`; default "
            "output is `.mozyo-bridge/redmine-defaults.md`. Distributed "
            "mozyo_bridge code does not carry project-specific values; "
            "the workspace YAML does. Pass `--check` to verify drift; "
            "default action regenerates the output(s)."
        ),
    )
    add_repo_option(workspace_defaults)
    workspace_defaults.add_argument(
        "--check",
        action="store_true",
        help=(
            "Re-render in memory and compare against the committed "
            "output(s). Exit 1 on drift; writes nothing."
        ),
    )
    workspace_defaults.set_defaults(func=cmd_workspace_defaults)

    release = sub.add_parser(
        "release",
        help=(
            "Read-only release helper surfaces (`check tree|scaffold|"
            "artifact|workflow`, `workflow runs|wait`). Helpers do not "
            "dispatch workflows, bump versions, commit, push, tag, or "
            "create GitHub releases."
        ),
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)

    release_check = release_sub.add_parser(
        "check",
        help="Read-only release guardrail checks (tree / scaffold / artifact / workflow)",
    )
    release_check_sub = release_check.add_subparsers(
        dest="release_check_command", required=True
    )

    release_check_tree = release_check_sub.add_parser(
        "tree",
        help=(
            "Run Source Tree Hygiene from release-flow.md. Strict-fail on "
            "personal home paths or secret-shape tokens in tracked files."
        ),
    )
    add_repo_option(release_check_tree)
    release_check_tree.set_defaults(func=cmd_release_check_tree)

    release_check_scaffold = release_check_sub.add_parser(
        "scaffold",
        help=(
            "Run Fresh Scaffold Smoke for every preset in an isolated home "
            "and target. Strict-fail on host-path leakage, missing portable "
            "rule path, or scaffold-status drift."
        ),
    )
    release_check_scaffold.set_defaults(func=cmd_release_check_scaffold)

    release_check_artifact = release_check_sub.add_parser(
        "artifact",
        help=(
            "Run python -m build, extract every produced artifact, and scan "
            "for personal home paths and secret-shape tokens. Strict-fail on "
            "any match; the operator records false-positive disposition in "
            "Asana before re-running."
        ),
    )
    add_repo_option(release_check_artifact)
    release_check_artifact.set_defaults(func=cmd_release_check_artifact)

    release_check_drift = release_check_sub.add_parser(
        "drift",
        help=(
            "Run canonical renderer + plugin mirror drift gates as one "
            "release check. Reproduces `mozyo-bridge scaffold canonical "
            "--check` (router pair + governed workflow pair) and "
            "`scripts/sync_plugin_skill.sh --check` (plugin mirror). "
            "Strict-fail on either drift; recovery hints name the "
            "real CLI commands operators copy-paste."
        ),
    )
    add_repo_option(release_check_drift)
    release_check_drift.set_defaults(func=cmd_release_check_drift)

    release_check_workflow = release_check_sub.add_parser(
        "workflow",
        help=(
            "Fetch a single GitHub Actions run's status and conclusion via "
            "`gh run view`. No dispatch, no judgment; success exits 0 and "
            "every other state exits non-zero."
        ),
    )
    release_check_workflow.add_argument(
        "--run-id",
        dest="run_id",
        required=True,
        help="GitHub Actions run id to inspect (databaseId, not the URL fragment)",
    )
    release_check_workflow.set_defaults(func=cmd_release_check_workflow)

    release_workflow = release_sub.add_parser(
        "workflow",
        help="GitHub Actions polling / summary helpers (read-only)",
    )
    release_workflow_sub = release_workflow.add_subparsers(
        dest="release_workflow_command", required=True
    )

    release_workflow_runs = release_workflow_sub.add_parser(
        "runs",
        help=(
            "List the most recent runs of a workflow with created_at / "
            "status / conclusion / head_sha / html_url."
        ),
    )
    release_workflow_runs.add_argument(
        "--workflow",
        required=True,
        help="Workflow file name or id (e.g. `testpypi.yml`, `publish.yml`, `Test`)",
    )
    release_workflow_runs.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of runs to list (default 10)",
    )
    release_workflow_runs.set_defaults(func=cmd_release_workflow_runs)

    release_workflow_wait = release_workflow_sub.add_parser(
        "wait",
        help=(
            "Poll a single run-id until it reaches `completed` or until "
            "--timeout elapses. Resumable; no judgment. Exit 124 on timeout."
        ),
    )
    release_workflow_wait.add_argument(
        "--run-id",
        dest="run_id",
        required=True,
        help="GitHub Actions run id to wait on",
    )
    release_workflow_wait.add_argument(
        "--timeout",
        type=float,
        required=True,
        help="Maximum seconds to wait before exiting with code 124",
    )
    release_workflow_wait.add_argument(
        "--poll",
        type=float,
        default=5.0,
        help="Polling interval in seconds (default 5.0)",
    )
    release_workflow_wait.set_defaults(func=cmd_release_workflow_wait)

    release_bump = release_sub.add_parser(
        "bump",
        help=(
            "Atomically rewrite the contract-declared release-version "
            "mirror set in the worktree (`--to VERSION`) or print its "
            "current state (`--check`). Never commits, pushes, or tags."
        ),
    )
    add_repo_option(release_bump)
    bump_mode = release_bump.add_mutually_exclusive_group(required=True)
    bump_mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only: print each mirror file's current version literal, "
            "the latest `Release vX.Y.Z` commit, and the `v*` tag list. "
            "Exits non-zero when mirror-set values disagree."
        ),
    )
    bump_mode.add_argument(
        "--to",
        metavar="VERSION",
        help=(
            "Rewrite every mirror-set file to VERSION in the worktree. "
            "Strict-fail if any mirror-set file's version literal cannot "
            "be located. Idempotent on same value. Operator still owns "
            "`git commit` / `git push` / `git tag -a`."
        ),
    )
    release_bump.set_defaults(func=cmd_release_bump)

    release_publish = release_sub.add_parser(
        "publish",
        help=(
            "Release publish helpers: TestPyPI workflow dispatch, "
            "production GitHub Release trigger (default dry-run), and "
            "plan summarization. No GA/beta judgment is automated."
        ),
    )
    add_repo_option(release_publish)
    publish_mode = release_publish.add_mutually_exclusive_group(required=True)
    publish_mode.add_argument(
        "--testpypi",
        action="store_true",
        help=(
            "Dispatch the TestPyPI workflow via `gh workflow run "
            "testpypi.yml --ref main -f version=<X.Y.Z>`. Requires "
            "--version. Run-id polling is delegated to "
            "`release check workflow` / `release workflow wait`."
        ),
    )
    publish_mode.add_argument(
        "--pypi",
        action="store_true",
        help=(
            "Assemble the `gh release create vX.Y.Z --verify-tag "
            "--title vX.Y.Z --notes-file PATH` invocation. Default "
            "dry-run; --execute required to actually create the "
            "GitHub Release. Requires --tag and --notes-file."
        ),
    )
    publish_mode.add_argument(
        "--plan",
        action="store_true",
        help=(
            "Enumerate operator-takeable options based on current git "
            "ref / pyproject version / latest `Test` workflow run / "
            "TestPyPI existing version. No judgment."
        ),
    )
    release_publish.add_argument(
        "--version",
        help="Version literal X.Y.Z for `--testpypi` workflow dispatch input",
    )
    release_publish.add_argument(
        "--tag",
        help="Annotated tag `vX.Y.Z` for `--pypi` GitHub Release",
    )
    release_publish.add_argument(
        "--notes-file",
        dest="notes_file",
        help=(
            "Path to the release notes markdown file passed to "
            "`gh release create --notes-file`. Required for `--pypi`."
        ),
    )
    release_publish.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Required to actually invoke `gh release create` under "
            "`--pypi`. Without this flag the helper only prints the "
            "command it would run."
        ),
    )
    release_publish.set_defaults(func=cmd_release_publish)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not getattr(args, "command", None):
        return cmd_mozyo(args)
    args = normalize_paths(args)
    return args.func(args)
