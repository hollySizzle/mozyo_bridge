from __future__ import annotations

import argparse
import sys

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_agents_attention_project,
    cmd_agents_list,
    cmd_agents_targets,
    cmd_config,
    cmd_doctor,
    cmd_doctor_instruction,
    cmd_events_query,
    cmd_events_tail,
    cmd_handoff_cross_workspace_consult,
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_id,
    cmd_init,
    cmd_cockpit,
    cmd_instruction_doctor,
    cmd_instruction_install,
    cmd_keys,
    cmd_layout_apply,
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
    cmd_otel_activity,
    cmd_otel_events,
    cmd_otel_launchd,
    cmd_otel_serve,
    cmd_otel_status,
    cmd_session_boundary_prompt,
    cmd_session_list,
    cmd_session_name,
    cmd_session_pane_decision,
    cmd_session_vscode_settings,
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
from mozyo_bridge.application import (
    cli_docs_scaffold,
    cli_release,
    cli_workspace,
)
from mozyo_bridge.application.sublane_diagnostics import (
    cmd_sublane_callback_recovery,
    cmd_sublane_readiness,
)
from mozyo_bridge.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_CHOICES,
)
from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.domain.agent_discovery import AGENT_KINDS
from mozyo_bridge.domain.handoff import (
    KIND_LABELS,
    MODE_QUEUE_ENTER,
    MODES,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
    SOURCES,
)
from mozyo_bridge.domain.session_boundary import SESSION_BOUNDARY_SIGNALS
from mozyo_bridge.shared.paths import default_queue_path, default_tmux_conf, resolve_repo_root

# --- Backward-compatible import surface (Redmine #12138 / #12141). ---
# Before the parser split, these handler / helper symbols were importable as
# ``mozyo_bridge.application.cli.<name>`` because ``cli.py`` imported them
# directly. The parser *registration* moved to the family modules
# (`cli_release` / `cli_docs_scaffold` / `cli_workspace`), but the module-level
# import path is preserved here so downstream imports / monkeypatch targets that
# referenced them through ``application.cli`` keep working. This is the #12138
# scope guard "do not retire legacy import paths" applied to ``cli.py``; it does
# not affect parser behavior.
from mozyo_bridge.application.cli_common import add_scaffold_target_option  # noqa: F401,E402
from mozyo_bridge.application.commands import (  # noqa: F401,E402
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
    cmd_workspace_defaults,
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
)
from mozyo_bridge.application.release import (  # noqa: F401,E402
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


def _add_doctor_diagnostic_options(parser: argparse.ArgumentParser) -> None:
    """Shared --target/--repo/--home/--json for `doctor` and `doctor instruction`."""
    parser.add_argument(
        "--target",
        dest="repo",
        help="Project root to check for scaffold and Claude project-skill readiness. "
        "Defaults to MOZYO_REPO or the current working directory.",
    )
    parser.add_argument(
        "--repo",
        dest="repo",
        help="Alias for --target.",
    )
    parser.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )


def _add_runtime_config_check_parser(
    subparsers: argparse._SubParsersAction,
    *,
    name: str,
    deprecated_alias: str | None = None,
    canonical_command: str | None = None,
) -> argparse.ArgumentParser:
    """Add the read-only runtime-config check parser under `name`.

    Used twice: as the canonical `runtime-config check`, and as the deprecated
    `instruction doctor` alias. The alias path records deprecation metadata so
    `main()` can warn before dispatch.
    """
    parser = subparsers.add_parser(
        name,
        help=(
            "Profile-aware, read-only check that a Redmine/Codex workspace "
            "carries the repo-root runtime config the bootstrap docs require "
            "(`<repo>/.codex/config.toml`, optional `<repo>/.mcp.json`). Does "
            "not call the network, autogenerate, or write home config."
            + ("" if deprecated_alias is None else " [deprecated alias]")
        ),
    )
    parser.add_argument(
        "--target",
        dest="target",
        help="Project root to check. Defaults to MOZYO_REPO or the current "
        "working directory.",
    )
    parser.add_argument("--repo", dest="target", help="Alias for --target.")
    parser.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Config profile to check. Only `redmine-codex` is defined today; "
        "other presets are intentionally not failed by this command.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    parser.set_defaults(
        func=cmd_instruction_doctor,
        deprecated_alias=deprecated_alias,
        canonical_command=canonical_command,
    )
    return parser


def _add_runtime_config_install_parser(
    subparsers: argparse._SubParsersAction,
    *,
    name: str,
    deprecated_alias: str | None = None,
    canonical_command: str | None = None,
) -> argparse.ArgumentParser:
    """Add the write-capable runtime-config install parser under `name`.

    Used twice: as the canonical `runtime-config install`, and as the
    deprecated `instruction install` alias.
    """
    parser = subparsers.add_parser(
        name,
        help=(
            "Project the verified Redmine default project from "
            "`<repo>/.mozyo-bridge/workspace-defaults.yaml` into the repo-root "
            "`<repo>/.codex/config.toml` so `runtime-config check` turns green. "
            "Source of truth stays workspace-defaults; only the repo-root config "
            "is written (never home config), no credentials are generated, and "
            "the default is a dry-run (pass `--write` to apply)."
            + ("" if deprecated_alias is None else " [deprecated alias]")
        ),
    )
    parser.add_argument(
        "--target",
        dest="target",
        help="Project root to install into. Defaults to MOZYO_REPO or the "
        "current working directory.",
    )
    parser.add_argument("--repo", dest="target", help="Alias for --target.")
    parser.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Config profile to install. Only `redmine-codex` is defined today.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the change to `<repo>/.codex/config.toml` (default: dry-run).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "When the managed [redmine] / [mcp_servers.redmine_epic_grid] tables "
            "already exist but disagree with workspace-defaults, regenerate them "
            "(other tables are preserved). Without --force a conflict fails."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    parser.set_defaults(
        func=cmd_instruction_install,
        deprecated_alias=deprecated_alias,
        canonical_command=canonical_command,
    )
    return parser


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
        "--cc",
        action="store_true",
        default=False,
        dest="cc",
        help=(
            "Bare `mozyo`: attach via iTerm2 control mode (`tmux -CC attach`) "
            "instead of a plain `tmux attach`, so iTerm2 manages tmux windows "
            "as native windows/panes. Ensure behavior is unchanged. "
            "`--no-attach` and `--json` both win: they ensure only and never "
            "exec, so the printed/JSON attach command just reflects the `-CC` "
            "variant. Ignored when a subcommand is given."
        ),
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
            "Bare `mozyo`: override the tmux session name. Defaults to the "
            "derived collision-safe name (`mozyo-bridge session name`): the "
            "workspace-defaults Redmine identifier when present, else a "
            "hash-suffixed repo-path name. Pass an explicit name to override."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json_output",
        help=(
            "Bare `mozyo`: emit machine-readable JSON describing the resolved "
            "session, current windows, and a `ready` flag (claude/codex windows "
            "present) instead of the human table. Implies no attach so a launcher "
            "capturing stdout is never replaced by `tmux attach`. Ignored when a "
            "subcommand is given."
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
            "run inside tmux, else the bare-`mozyo` derived session name "
            "(`mozyo-bridge session name`)."
        ),
    )
    status.set_defaults(func=cmd_status)

    sub.add_parser("list").set_defaults(func=cmd_list)

    layout = sub.add_parser(
        "layout",
        help=(
            "Cockpit layout presets (Redmine #11788): arrange active "
            "workspaces as horizontal columns with a Codex-top / Claude-bottom "
            "vertical split per workspace. tmux state is the layout's source of "
            "truth; `--cc` only changes how it is attached."
        ),
    )
    layout_sub = layout.add_subparsers(dest="layout_command", required=True)
    layout_apply = layout_sub.add_parser(
        "apply",
        help="Build (or focus) a cockpit layout for the active workspaces.",
    )
    layout_apply.add_argument(
        "preset",
        choices=["cockpit"],
        help="Layout preset. Currently only `cockpit`.",
    )
    layout_apply.add_argument(
        "--ratio",
        dest="codex_ratio",
        type=int,
        default=70,
        help=(
            "Codex pane height percentage per column (Claude takes the rest). "
            "Default 70 (Codex 70%% / Claude 30%%); clamped to 10..90."
        ),
    )
    layout_apply.add_argument(
        "--session",
        dest="cockpit_session",
        default=None,
        help="Cockpit tmux session name. Defaults to `mozyo-cockpit`.",
    )
    layout_apply.add_argument(
        "--repo",
        dest="layout_repos",
        action="append",
        default=None,
        help=(
            "Explicit workspace repo root to summon as a column (repeatable). "
            "When omitted, active mozyo workspaces are discovered from the live "
            "session inventory."
        ),
    )
    layout_apply.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the planned tmux commands without running them.",
    )
    layout_apply.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Emit the plan as JSON (implies no tmux execution and no attach).",
    )
    layout_apply.add_argument(
        "--cc",
        dest="cc",
        action="store_true",
        default=False,
        help="Attach the built cockpit via iTerm2 control mode (`tmux -CC`).",
    )
    layout_apply.add_argument(
        "--no-attach",
        dest="no_attach",
        action="store_true",
        default=False,
        help="Build the cockpit but do not attach.",
    )
    layout_apply.set_defaults(func=cmd_layout_apply)

    cockpit = sub.add_parser(
        "cockpit",
        help=(
            "Add the current project/workspace to the shared cockpit and view "
            "it in iTerm2 control mode (Redmine #11803). `cd <project> && mozyo "
            "cockpit` appends a column to `mozyo-cockpit` (creating it on first "
            "use), focuses the column if the workspace is already present, and "
            "never opens a duplicate iTerm window for an existing cockpit. "
            "`mozyo cockpit reset` / `rebuild` (Redmine #11814) safely tear down "
            "a stale / broken cockpit instead of a manual `tmux kill-session`. "
            "`mozyo --cc` is unchanged."
        ),
    )
    cockpit.add_argument(
        "action",
        nargs="?",
        choices=["append", "adopt", "reset", "rebuild", "doctor-geometry", "peer-adopt"],
        default=None,
        help=(
            "Optional explicit sub-action. `append` is the same append/focus "
            "behavior as bare `mozyo cockpit`; both auto-decide create / append "
            "/ focus from the live cockpit state. `adopt` reports a co-existing "
            "normal `mozyo` session for this workspace+lane as an adopt candidate "
            "and, with `--confirm` (Redmine #11898, Phase 2), atomically moves "
            "its live codex/claude panes into the cockpit as a column. `reset` "
            "and `rebuild` (Redmine #11814) safely tear down a stale / broken "
            "cockpit: `reset` kills the mozyo-identified cockpit session, "
            "`rebuild` then recreates a fresh one for the current workspace. Both "
            "act only on a session proven mozyo-managed by its identity markers "
            "(never by name) and only with `--confirm`. `doctor-geometry` "
            "(Redmine #12131) is a read-only diagnosis of cockpit display-geometry "
            "drift (missing codex/claude, role-less pane, a Unit's codex/claude "
            "not sharing one column, a column carrying more than one Unit, width "
            "imbalance); it observes geometry only — identity/routing stay on the "
            "pane options — and never repairs / rebalances / moves panes. "
            "`peer-adopt` (Redmine #12133) is the first safe repair slice: it binds "
            "a role-less cockpit pane (`--pane`) as the missing peer role (`--role`) "
            "of an existing Unit (`--unit workspace/lane`), via pane-option identity "
            "binding only — never a pane move / kill / split / rebalance — and "
            "fail-closed (it applies only with `--confirm` when exactly one missing "
            "peer and the selected candidate pass every guard, including a "
            "cwd/process preflight). Without `--confirm` every other sub-action is "
            "detect-only / preview and mutates nothing; `--dry-run` / `--json` "
            "always preview without mutating."
        ),
    )
    cockpit.add_argument(
        "--repo",
        default=None,
        help="Workspace repo root to add. Defaults to the current directory.",
    )
    cockpit.add_argument(
        "--ratio",
        dest="codex_ratio",
        type=int,
        default=70,
        help="Codex pane height percentage for the column (default 70).",
    )
    cockpit.add_argument(
        "--session",
        dest="cockpit_session",
        default=None,
        help="Cockpit tmux session name. Defaults to `mozyo-cockpit`.",
    )
    cockpit.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the planned action and tmux commands without running them.",
    )
    cockpit.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Emit the planned action + commands as JSON (no tmux execution).",
    )
    cockpit.add_argument(
        "--no-attach",
        dest="no_attach",
        action="store_true",
        default=False,
        help="On first-time create, build the cockpit but do not attach.",
    )
    cockpit.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        default=False,
        help=(
            "Explicitly confirm a destructive cockpit sub-action. For `adopt` "
            "(Redmine #11898) it moves the co-existing normal session's live "
            "codex/claude panes into the cockpit; for `reset` / `rebuild` "
            "(Redmine #11814) it kills (and, for rebuild, recreates) the "
            "mozyo-identified cockpit session. Required to mutate; without it "
            "these sub-actions are detect-only / preview. `--dry-run` / `--json` "
            "still only preview."
        ),
    )
    cockpit.add_argument(
        "--pane",
        dest="peer_pane",
        default=None,
        help=(
            "For `peer-adopt` (Redmine #12133): the role-less cockpit pane id "
            "(`%%id`, as reported by `cockpit doctor-geometry`) to adopt as a "
            "Unit's missing peer."
        ),
    )
    cockpit.add_argument(
        "--unit",
        dest="peer_unit",
        default=None,
        help=(
            "For `peer-adopt` (Redmine #12133): the destination Unit as "
            "`workspace_id/lane_id` (the lane is optional and defaults to "
            "`default`). The Unit must already exist and be missing exactly the "
            "`--role` peer."
        ),
    )
    cockpit.add_argument(
        "--role",
        dest="peer_role",
        default=None,
        choices=["claude", "codex"],
        help=(
            "For `peer-adopt` (Redmine #12133): the missing peer role to bind the "
            "`--pane` as (`claude` or `codex`)."
        ),
    )
    cockpit.set_defaults(func=cmd_cockpit)

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
            "Enumerate agents with structured discovery fields, one row per "
            "pane_id (Redmine #11628): a pane shared by grouped tmux "
            "sessions is one agent whose memberships are folded into "
            "`views`; the top-level session is the canonical view (the "
            "workspace's canonical session name when one matches). Single "
            "tmux server assumed. Does not modify tmux state; safe to call "
            "from any session. Distinct from `mozyo-bridge list` (raw "
            "single-session pane table) and `mozyo-bridge status` (current "
            "session diagnostics)."
        ),
    )
    agents_list.add_argument(
        "--session",
        help=(
            "Filter to agents that are members of this tmux session (exact "
            "name; matches the canonical session or any grouped view). "
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

    agents_targets = agents_sub.add_parser(
        "targets",
        help=(
            "Compact handoff-target discovery for LLM / operator use (Redmine "
            "#11811). Lists classified agent panes (claude / codex) as candidate "
            "targets with role + resolver provenance (role_source / confidence / "
            "ambiguous), workspace id + label, checkout lane, a short repo "
            "identifier, liveness, and location — enough to choose an explicit "
            "pane_id without parsing titles. Read-only. Listing is non-selecting: "
            "same-role candidates stay distinguishable by workspace / lane / pane, "
            "so a natural name never auto-crosses a safety boundary. Compact text "
            "hides absolute paths; --json adds repo_root / cwd."
        ),
    )
    agents_targets.add_argument(
        "--session",
        help=(
            "Filter to candidates that are members of this tmux session (exact "
            "name; matches the canonical session or any grouped view)."
        ),
    )
    agents_targets.add_argument(
        "--agent",
        choices=sorted(AGENT_KINDS),
        help="Filter by classified agent kind (claude / codex).",
    )
    agents_targets.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON candidates instead of the compact table.",
    )
    agents_targets.set_defaults(func=cmd_agents_targets)

    agents_attention = agents_sub.add_parser(
        "attention-project",
        help=(
            "Project derived attention state onto tmux pane user options as a "
            "re-derivable cache (Redmine #11954): @mozyo_attention_state / "
            "_severity / _reason / _updated_at. The cache is never the source of "
            "truth and is never used for routing / handoff preflight. Safe by "
            "default: previews the set-option plan without mutating tmux; pass "
            "--apply to write. Reuses the conservative #11952 derivation (no "
            "fabricated owner/review signals yet)."
        ),
    )
    agents_attention.add_argument(
        "--session",
        help="Filter to candidates that are members of this tmux session.",
    )
    agents_attention.add_argument(
        "--agent",
        choices=sorted(AGENT_KINDS),
        help="Filter by classified agent kind (claude / codex).",
    )
    agents_attention.add_argument(
        "--apply",
        action="store_true",
        help="Write the tmux user options (default previews the plan only).",
    )
    agents_attention.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Force preview even with --apply (preview is already the default).",
    )
    agents_attention.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the derived attention records and set-option plan as JSON.",
    )
    agents_attention.set_defaults(func=cmd_agents_attention_project)

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
        include_to: bool = True,
        include_force: bool = True,
        target_required: bool = False,
        target_repo_required: bool = False,
    ) -> None:
        if include_to:
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
            required=target_required,
            help=(
                "Required explicit tmux target (an explicit `%%pane` for the "
                "Codex gateway); the target pane must resolve to the fixed "
                "receiver in every mode"
                if target_required
                else "Optional tmux target override; defaults to same-session "
                "agent-window resolution from --to"
            ),
        )
        parser_.add_argument(
            "--target-repo",
            dest="target_repo",
            required=target_repo_required,
            help=(
                "Optional cross-workspace gate (Redmine #10332): the target "
                "pane's cwd must resolve to this repo root, otherwise the "
                "handoff is rejected with `target_repo_mismatch`. Use when "
                "the sender wants to assert which workspace the target lives "
                "in before delivery. Drop the flag to skip the repo gate. "
                "Pass `auto` (Redmine #11778) to infer the root from an "
                "explicit `%%pane` target's own cwd instead of running "
                "`tmux display-message ... pane_current_path` by hand; "
                "`auto` requires an explicit `%%pane` target and stays "
                "fail-closed when no workspace/repo marker is reachable."
            ),
        )
        parser_.add_argument(
            "--workdir",
            dest="workdir",
            help=(
                "Optional target execution root / workdir for the receiver "
                "(Redmine #12098). Use when the work target is a nested project "
                "below the pane cwd / workspace root (e.g. a cockpit workspace "
                "whose pane cwd is the workspace anchor, not the nested "
                "checkout). The resolved root is carried in the notification "
                "body and durable delivery record — as a repo-root-relative "
                "pointer when it lives under `--target-repo` (or the pane's "
                "inferred repo root) — so the receiver recovers the execution "
                "root from the durable record instead of pane scrollback. This "
                "is record/wording only: it does not change pane selection or "
                "relax any cross-session / cross-lane gate."
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
        if include_force:
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

    handoff_consult = handoff_sub.add_parser(
        "cross-workspace-consult",
        help=(
            "Cross-workspace design-consultation route through a target "
            "workspace's Codex gateway pane"
        ),
        description=(
            "Standard cross-workspace design-consultation primitive (Redmine "
            "#11779). It is a boundary-preserving wrapper over `handoff send`: "
            "the receiver is fixed to `codex` (the consult lands on the target "
            "workspace's Codex gateway pane, never directly in a foreign Claude "
            "pane), and the cross-workspace identity gate is mandatory — both "
            "`--target` and `--target-repo` are required, so the gate that "
            "`handoff send` only runs when `--target-repo` is supplied always "
            "runs here. `--kind` defaults to `design_consultation`. Every "
            "actual safety gate (cross-session Claude block, repo identity "
            "gate, receiver-process binding, landing rail) is delegated to the "
            "same `handoff send` orchestration and is neither hidden nor "
            "weakened by this wrapper."
        ),
        epilog=(
            "Operational route:\n"
            "  1. Discover the target workspace's Codex pane with "
            "`mozyo-bridge agents list` / `agents targets` (read-only).\n"
            "  2. Record the consult request on the durable source of truth "
            "(Redmine issue/journal or Asana task/comment) first; the pane "
            "notification is only the pointer.\n"
            "  3. Run this command with an explicit `%pane` target and "
            "`--target-repo` (or `--target-repo auto` to infer the root from "
            "that `%pane`'s cwd).\n"
            "  4. The target Codex reads the durable anchor and, if "
            "implementation is needed, performs the local same-session Claude "
            "handoff inside its own workspace.\n\n"
            "Example:\n"
            "  mozyo-bridge handoff cross-workspace-consult \\\n"
            "    --source redmine --issue 11779 --journal 58668 \\\n"
            "    --target %42 --target-repo auto \\\n"
            "    --summary 'cross-workspace gateway primitive design'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configure_handoff_parser(
        handoff_consult,
        kind_required=False,
        include_to=False,
        include_force=False,
        target_required=True,
        target_repo_required=True,
    )
    handoff_consult.set_defaults(func=cmd_handoff_cross_workspace_consult)

    reply_alias = sub.add_parser(
        "reply",
        help="Alias for `mozyo-bridge handoff reply` (kind defaults to `reply`)",
    )
    configure_handoff_parser(reply_alias, kind_required=False)
    reply_alias.set_defaults(func=cmd_handoff_reply)

    init = sub.add_parser(
        "init",
        help=(
            "Adopt the current/target pane into its workspace as a `claude` / "
            "`codex` agent. Smart default: derive the workspace's expected tmux "
            "session, pin it into `.vscode/settings.json`, rename a "
            "tmux-integrated fallback session (e.g. `___________`) into the "
            "derived name, then rename the window to the agent. Fails closed when "
            "adoption is not provably safe (meaningful foreign session, "
            "expected-session collision, unidentifiable workspace root). Defaults "
            "to the current pane when no target is given."
        ),
    )
    init.add_argument("agent", choices=["claude", "codex"])
    init.add_argument("target", nargs="?")
    init.add_argument(
        "--window-only",
        action="store_true",
        default=False,
        dest="window_only",
        help=(
            "Legacy low-level behavior: only rename the current/target window, "
            "with no session rename and no `.vscode/settings.json` write. Use for "
            "manual / debug workflows or to adopt into a meaningful (non-fallback) "
            "session in place."
        ),
    )
    init.add_argument(
        "--no-vscode-settings",
        action="store_true",
        default=False,
        dest="no_vscode_settings",
        help=(
            "Run the smart session/window adoption but do not write "
            "`<workspace>/.vscode/settings.json`."
        ),
    )
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser(
        "doctor",
        help="Diagnose CLI, central rules, agent skills, and scaffold readiness",
    )
    _add_doctor_diagnostic_options(doctor)
    doctor.set_defaults(func=cmd_doctor)

    # `doctor instruction` is the read-only recovery runbook (Redmine #11051):
    # given the doctor diagnostics, it prints the ordered fix procedure with
    # primary vs legacy-fallback commands. Bare `doctor` keeps running the
    # diagnostics (subparser is optional so set_defaults(func=cmd_doctor) wins).
    doctor_sub = doctor.add_subparsers(dest="doctor_command", required=False)
    doctor_instruction = doctor_sub.add_parser(
        "instruction",
        help=(
            "Read-only recovery runbook: orders the fix steps for the current "
            "doctor diagnostics, distinguishing primary (Claude plugin) from "
            "legacy fallback paths and routing scaffold drift through "
            "review-before-restore. Does not write, install, or hit the network."
        ),
    )
    _add_doctor_diagnostic_options(doctor_instruction)
    doctor_instruction.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Runtime-config profile to fold into the runbook. Only "
        "`redmine-codex` is defined today.",
    )
    doctor_instruction.set_defaults(func=cmd_doctor_instruction)

    # `sublane` groups the read-only sublane startup / callback-stall
    # diagnostics (Redmine #12159). Both subcommands are pure over their inputs
    # and never change handoff / queue-enter / launch behavior.
    sublane = sub.add_parser(
        "sublane",
        help=(
            "Read-only sublane startup readiness and callback-stall recovery "
            "diagnostics (Redmine #12159)"
        ),
    )
    sublane_sub = sublane.add_subparsers(dest="sublane_command", required=True)

    sublane_readiness = sublane_sub.add_parser(
        "readiness",
        help=(
            "Report whether future managed Claude panes launch in auto mode, "
            "the coordinator-callback states this lane owes, and where the "
            "stall-recovery path lives. Exits non-zero when permission mode is "
            "not reproducible auto."
        ),
    )
    sublane_readiness.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    sublane_readiness.set_defaults(func=cmd_sublane_readiness)

    sublane_callback = sublane_sub.add_parser(
        "callback-recovery",
        help=(
            "Classify a delivered-but-quiet unit of work into the four "
            "callback-stall states from durable-record facts and print the "
            "standard recovery path. Exits non-zero on a genuine stall."
        ),
    )
    sublane_callback.add_argument(
        "--dispatch-delivered",
        dest="dispatch_delivered",
        action="store_true",
        help="A durable dispatch journal (Start / implementation_request / "
        "coordinator routing) exists on the issue.",
    )
    sublane_callback.add_argument(
        "--progress",
        dest="progress",
        action="store_true",
        help="A newer durable gate / Progress Log journal appeared after the "
        "dispatch (implementation_done, review_request, ...).",
    )
    sublane_callback.add_argument(
        "--callback",
        dest="callback",
        choices=CALLBACK_CHOICES,
        default=CALLBACK_ABSENT,
        help="What the durable record shows about the cross-lane coordinator "
        "callback. Default: absent.",
    )
    sublane_callback.add_argument(
        "--stale-cli",
        dest="stale_cli",
        action="store_true",
        help="Corroborating signal that a recorded callback attempt failed on "
        "a stale installed CLI (only meaningful with `--callback delivery_failed`).",
    )
    sublane_callback.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    sublane_callback.set_defaults(func=cmd_sublane_callback_recovery)

    # `runtime-config` is the canonical repo-local LLM runtime config group
    # (renamed from `instruction` in Redmine #11051 to free the word
    # "instruction" for the `doctor instruction` runbook). `check` is read-only,
    # `install` is write-capable (dry-run by default). The legacy `instruction`
    # group below is a deprecated alias that warns and is a removal candidate
    # next minor.
    runtime_config = sub.add_parser(
        "runtime-config",
        help=(
            "Repo-local LLM runtime config commands: `check` (read-only) and "
            "`install` (write-capable, dry-run by default)"
        ),
    )
    runtime_config_sub = runtime_config.add_subparsers(
        dest="runtime_config_command", required=True
    )
    _add_runtime_config_check_parser(runtime_config_sub, name="check")
    _add_runtime_config_install_parser(runtime_config_sub, name="install")

    instruction = sub.add_parser(
        "instruction",
        help=(
            "Deprecated alias for `runtime-config` (write-capable). Use "
            "`runtime-config check` / `runtime-config install`; the old names "
            "still run but warn and are a removal candidate next minor."
        ),
    )
    instruction_sub = instruction.add_subparsers(
        dest="instruction_command", required=True
    )
    _add_runtime_config_check_parser(
        instruction_sub,
        name="doctor",
        deprecated_alias="mozyo-bridge instruction doctor",
        canonical_command="mozyo-bridge runtime-config check",
    )
    _add_runtime_config_install_parser(
        instruction_sub,
        name="install",
        deprecated_alias="mozyo-bridge instruction install",
        canonical_command="mozyo-bridge runtime-config install",
    )

    cli_docs_scaffold.register(sub)

    events = sub.add_parser(
        "events",
        help=(
            "Consumer event timeline source (Redmine #11813): a stable, "
            "redacted, source-layer-tagged projection over the OTel runtime "
            "store for display consumers (cockpit / private GUI / iTerm "
            "WebViewer). Distinct from `otel events`, which exposes the raw "
            "OTLP shape for debugging — this face decouples consumers from "
            "the OTel internal schema. Read-only and best-effort: the store "
            "is a cache, never the source of truth (gate state stays with "
            "Redmine, liveness with `agents list` / `session list`). JSON "
            "carries identifiers, event kinds and numeric usage only — never "
            "prompt bodies or full filesystem paths."
        ),
    )
    events_sub = events.add_subparsers(dest="events_command", required=True)

    def add_events_db_option(parser_obj: argparse.ArgumentParser) -> None:
        parser_obj.add_argument(
            "--db",
            help=(
                "Event store path override. Default: "
                "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`."
            ),
        )

    events_tail = events_sub.add_parser(
        "tail",
        help=(
            "Tail the most recent timeline events (default 50), newest "
            "first. Use `--json` for the stable TimelineEvent envelope that "
            "display consumers code against."
        ),
    )
    events_tail.add_argument(
        "--limit", help="Max events to show (default 50)."
    )
    add_events_db_option(events_tail)
    events_tail.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the timeline as the JSON TimelineEvent envelope.",
    )
    events_tail.set_defaults(func=cmd_events_tail)

    events_query = events_sub.add_parser(
        "query",
        help=(
            "Filtered timeline query. `--since` keeps events at or after a "
            "UTC ISO timestamp (the receiver clock); `--source` matches the "
            "emitting service exactly. Same redacted envelope as `tail`."
        ),
    )
    events_query.add_argument(
        "--since",
        help=(
            "Keep events whose observed_at is >= this UTC ISO timestamp "
            "(e.g. 2026-06-14T00:00:00+00:00)."
        ),
    )
    events_query.add_argument(
        "--source",
        help="Keep only events from this service_name (exact match).",
    )
    events_query.add_argument(
        "--limit", help="Max events to show (default 200)."
    )
    add_events_db_option(events_query)
    events_query.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the timeline as the JSON TimelineEvent envelope.",
    )
    events_query.set_defaults(func=cmd_events_query)

    otel = sub.add_parser(
        "otel",
        help=(
            "OTel event store (Redmine #11639 / #11672): a self-built, "
            "localhost-only OTLP/HTTP receiver persists agent telemetry "
            "(usage / event kinds only, never prompt bodies) into "
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`. "
            "Best-effort: events sent while the receiver is down are lost; "
            "the store is a cache, never the source of truth. Liveness "
            "stays with `agents list` / `session list`; workflow state "
            "stays with Redmine."
        ),
    )
    otel_sub = otel.add_subparsers(dest="otel_command", required=True)

    def add_otel_db_option(parser_obj: argparse.ArgumentParser) -> None:
        parser_obj.add_argument(
            "--db",
            help=(
                "Event store path override. Default: "
                "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`."
            ),
        )

    otel_serve = otel_sub.add_parser(
        "serve",
        help=(
            "Run the OTLP/HTTP receiver in the foreground (single-threaded "
            "= SQLite single-writer; binds 127.0.0.1 only). JSON encoding "
            "is built-in; protobuf needs `pip install 'mozyo-bridge[otel]'` "
            "or set OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the agent. "
            "launchd wiring is a follow-up task; this process is designed "
            "to be launchd-managed (foreground, clean shutdown)."
        ),
    )
    otel_serve.add_argument(
        "--host",
        help=(
            "Bind address. Loopback only (127.0.0.1 / localhost / ::1); "
            "any other value is rejected — the receiver is localhost-only "
            "by contract. Default 127.0.0.1."
        ),
    )
    otel_serve.add_argument(
        "--port", help="Port (default 4318, the OTLP/HTTP standard)."
    )
    add_otel_db_option(otel_serve)
    otel_serve.set_defaults(func=cmd_otel_serve)

    otel_status = otel_sub.add_parser(
        "status",
        help=(
            "Store counts plus receiver /healthz reachability. Read-only. "
            "An unreachable receiver means telemetry is being lost "
            "(by design) until it is restarted."
        ),
    )
    otel_status.add_argument("--host", help="Receiver host (default 127.0.0.1).")
    otel_status.add_argument("--port", help="Receiver port (default 4318).")
    add_otel_db_option(otel_status)
    otel_status.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the status as JSON.",
    )
    otel_status.set_defaults(func=cmd_otel_status)

    otel_events = otel_sub.add_parser(
        "events",
        help=(
            "Tail recent normalized events. Read-only; for debugging and "
            "for measuring per-CLI event depth."
        ),
    )
    otel_events.add_argument(
        "--limit", help="Max events to show (default 50)."
    )
    add_otel_db_option(otel_events)
    otel_events.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the events as JSON.",
    )
    otel_events.set_defaults(func=cmd_otel_events)

    otel_activity = otel_sub.add_parser(
        "activity",
        help=(
            "Per-source activity / idle judgement (Redmine #11673). "
            "`idle` / `unknown` never mean dead — OTel silence cannot "
            "distinguish waiting from dead; consult `agents list` for "
            "liveness. Sources are (service, session); the pane_id join "
            "is phase 2 (`match_hints` carries pid / cwd for it)."
        ),
    )
    otel_activity.add_argument(
        "--window",
        help="Active window in seconds (default 120).",
    )
    add_otel_db_option(otel_activity)
    otel_activity.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit activity records as JSON.",
    )
    otel_activity.set_defaults(func=cmd_otel_activity)

    otel_launchd = otel_sub.add_parser(
        "launchd",
        help=(
            "macOS launchd residency for the receiver (Redmine #11690): "
            "install / uninstall / status / restart. The LaunchAgent plist "
            "carries no environment variables (no secrets possible), keeps "
            "the loopback-only default bind, and `restart` is the upgrade "
            "step after `pipx upgrade mozyo-bridge`. Receiver health stays "
            "with `otel status`."
        ),
    )
    otel_launchd_sub = otel_launchd.add_subparsers(
        dest="launchd_command", required=True
    )
    launchd_install = otel_launchd_sub.add_parser(
        "install",
        help=(
            "Write ~/Library/LaunchAgents/"
            "biz.asile.mozyo-bridge.otel.plist and bootstrap it "
            "(RunAtLoad + KeepAlive). Idempotent; re-running re-bootstraps."
        ),
    )
    launchd_install.add_argument(
        "--port",
        help="Receiver port override written into the plist (default 4318).",
    )
    launchd_install.set_defaults(func=cmd_otel_launchd)
    launchd_uninstall = otel_launchd_sub.add_parser(
        "uninstall",
        help="Boot the agent out and remove exactly our plist file.",
    )
    launchd_uninstall.set_defaults(func=cmd_otel_launchd)
    launchd_status = otel_launchd_sub.add_parser(
        "status",
        help=(
            "launchd-side wiring state (plist presence, loaded, pid). "
            "Additive to `otel status`, which owns receiver health."
        ),
    )
    launchd_status.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the status as JSON.",
    )
    launchd_status.set_defaults(func=cmd_otel_launchd)
    launchd_restart = otel_launchd_sub.add_parser(
        "restart",
        help=(
            "Kickstart (kill + relaunch) the loaded agent — the documented "
            "upgrade step after updating the package."
        ),
    )
    launchd_restart.set_defaults(func=cmd_otel_launchd)

    session = sub.add_parser(
        "session",
        help=(
            "Resolve the mozyo-bridge tmux session name for a repo "
            "(Redmine #10796, #11429) and inventory running sessions across "
            "workspaces (Redmine #11422). Read-only towards tmux; `list` "
            "refreshes the home inventory cache."
        ),
    )
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_name = session_sub.add_parser(
        "name",
        help=(
            "Print the tmux session name for the repo. Registry-first "
            "(Redmine #11429): a canonical session name registered via "
            "`mozyo-bridge workspace register` (home registry, else the "
            "workspace-local anchor) wins. A never-registered workspace falls "
            "back to path derivation: `redmine.default_project.identifier` in "
            "`<repo>/.mozyo-bridge/workspace-defaults.yaml` when present, "
            "otherwise a hash-suffixed repo-path name so non-ASCII or "
            "duplicate basenames never collapse to a low-information "
            "`____`-style name. Use it as the VS Code `tmux-integrated` "
            "session name (per workspace) instead of a sanitized basename or "
            "a user-global fixed value. Single-line output by default; pass "
            "`--json` for the name plus resolution source."
        ),
    )
    add_repo_option(session_name)
    session_name.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit structured JSON (name, source, identifier, repo_root, "
            "workspace_id) instead of the bare name."
        ),
    )
    session_name.set_defaults(func=cmd_session_name)

    session_list = session_sub.add_parser(
        "list",
        help=(
            "Cross-workspace session inventory (Redmine #11422). One row per "
            "tmux pane, folded by pane_id: grouped tmux sessions count as "
            "views of one agent, not extra rows (Redmine #11628). Each pane "
            "carries its workspace identity (registry → anchor → derivation, "
            "Unicode-normalization-safe path matching). The live tmux runtime "
            "is the source of truth; each run refreshes the SQLite cache in "
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite`, and "
            "when tmux is unavailable the cached snapshot is served, marked "
            "stale. Generic inventory for operators and external UIs; not a "
            "backend for a specific VS Code extension."
        ),
    )
    session_list.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit the structured snapshot (schema_version, source, stale, "
            "collected_at, panes with workspace identity and views) instead "
            "of the table."
        ),
    )
    session_list.set_defaults(func=cmd_session_list)

    session_vscode = session_sub.add_parser(
        "vscode-settings",
        help=(
            "Pin `tmux-integrated.sessionName` in the workspace-local "
            "`<repo>/.vscode/settings.json` to the derived session name so VS "
            "Code stops sanitizing the basename to a `____`-style name. "
            "Workspace-local only; user-global settings are never touched. "
            "Without `--write` it prints what would change; `--write` applies "
            "it (refuses to clobber a JSONC file with comments)."
        ),
    )
    add_repo_option(session_vscode)
    session_vscode.add_argument(
        "--write",
        action="store_true",
        help="Apply the change to `<repo>/.vscode/settings.json` (default: dry-run print only).",
    )
    session_vscode.set_defaults(func=cmd_session_vscode_settings)

    session_boundary = session_sub.add_parser(
        "boundary-prompt",
        help=(
            "Emit the compact next-session boundary prompt (Redmine #12122) so "
            "the next Codex session resumes from the durable Redmine journal "
            "plus repo / execution root, not pane scrollback or window/session "
            "naming. The repo is referenced by its portable canonical session "
            "name; absolute paths appear only under --json. Read-only towards "
            "tmux, git, and Redmine."
        ),
    )
    add_repo_option(session_boundary)
    session_boundary.add_argument(
        "--issue", required=True, help="Active Redmine issue id (durable anchor)."
    )
    session_boundary.add_argument(
        "--journal",
        required=True,
        help="Latest Redmine journal id on the issue (the anchor to read first).",
    )
    session_boundary.add_argument(
        "--parent", help="Parent UserStory issue id, when the active issue is a child Task."
    )
    session_boundary.add_argument(
        "--commit", help="Latest relevant commit hash, when one exists."
    )
    session_boundary.add_argument(
        "--target-lane",
        dest="target_lane",
        help="Target lane / branch label (e.g. the sublane worktree branch).",
    )
    session_boundary.add_argument(
        "--execution-root",
        dest="execution_root",
        help=(
            "Absolute target execution root / workdir when it differs from the "
            "repo root (Redmine #12098). Rendered as a portable repo-relative "
            "pointer in the prompt; the absolute form stays in --json only."
        ),
    )
    session_boundary.add_argument(
        "--gate", help="Current gate state (e.g. implementation_done, review_request)."
    )
    session_boundary.add_argument(
        "--verification", help="Verification state summary (e.g. tests green / pending)."
    )
    session_boundary.add_argument(
        "--residual",
        action="append",
        help="A residual risk line (repeatable).",
    )
    session_boundary.add_argument(
        "--pending-action",
        dest="pending_action",
        help="The next pending action to carry into the next session.",
    )
    session_boundary.add_argument(
        "--next-actor",
        dest="next_actor",
        choices=["owner", "claude", "codex"],
        help="Who owns the next action.",
    )
    session_boundary.add_argument(
        "--signal",
        action="append",
        choices=list(SESSION_BOUNDARY_SIGNALS),
        help="A boundary candidate signal that fired (repeatable).",
    )
    session_boundary.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit structured JSON (prompt fields + prompt_markdown + absolute "
            "repo_root) instead of the pasteable markdown prompt."
        ),
    )
    session_boundary.set_defaults(func=cmd_session_boundary_prompt)

    session_pane = session_sub.add_parser(
        "pane-decision",
        help=(
            "Decide the guarded Claude-pane lifecycle action (Redmine #12122): "
            "reuse / new / orphan / guarded_kill / blocked. Default leans to a "
            "new pane; kill/discard is blocked while unfinished durable state "
            "is present or no owner kill approval is recorded. Exits 3 when "
            "blocked so a kill cannot silently proceed. Read-only."
        ),
    )
    session_pane.add_argument(
        "--requested",
        choices=["reuse", "new", "orphan", "kill", "discard"],
        default="new",
        help="The pane action under consideration (default: new).",
    )
    session_pane.add_argument(
        "--same-lane",
        dest="same_lane",
        action="store_true",
        help="The existing pane belongs to the same issue / lane / worktree.",
    )
    session_pane.add_argument(
        "--dirty-diff",
        dest="dirty_diff",
        action="store_true",
        help="The pane has uncommitted changes (preservation signal).",
    )
    session_pane.add_argument(
        "--running-process",
        dest="running_process",
        action="store_true",
        help="The pane has a running process (preservation signal).",
    )
    session_pane.add_argument(
        "--pending-approval",
        dest="pending_approval",
        action="store_true",
        help="The pane is waiting on a pending approval (preservation signal).",
    )
    session_pane.add_argument(
        "--unrecorded-journal",
        dest="unrecorded_journal",
        action="store_true",
        help="The pane has work not yet recorded to a durable journal (preservation signal).",
    )
    session_pane.add_argument(
        "--owner-approved-kill",
        dest="owner_approved_kill",
        action="store_true",
        help=(
            "An owner kill/close approval has been recorded through the Codex "
            "window. Required (with a clean pane) before guarded_kill."
        ),
    )
    session_pane.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (decision, blockers, rationale).",
    )
    session_pane.set_defaults(func=cmd_session_pane_decision)

    cli_workspace.register(sub)

    cli_release.register(sub)
    return parser


def _warn_deprecated_alias(args: argparse.Namespace) -> None:
    """Emit a stderr migration warning when a deprecated command alias is used.

    The warning goes to stderr only, so JSON output on stdout stays additive /
    unbroken for existing `jq` consumers (Redmine #11051 / #53306).
    """
    alias = getattr(args, "deprecated_alias", None)
    if not alias:
        return
    canonical = getattr(args, "canonical_command", None) or "the renamed command"
    print(
        f"deprecated: `{alias}` is a deprecated alias; use `{canonical}` instead "
        "(the alias is a removal candidate next minor).",
        file=sys.stderr,
    )


def main() -> int:
    args = build_parser().parse_args()
    if not getattr(args, "command", None):
        return cmd_mozyo(args)
    args = normalize_paths(args)
    _warn_deprecated_alias(args)
    return args.func(args)
