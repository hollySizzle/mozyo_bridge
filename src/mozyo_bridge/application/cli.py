from __future__ import annotations

import argparse
import sys

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_doctor,
    cmd_doctor_instruction,
    cmd_id,
    cmd_init,
    cmd_keys,
    cmd_list,
    cmd_mozyo,
    cmd_read,
    cmd_resolve,
    cmd_status,
    cmd_type,
)
from mozyo_bridge.application.instruction_doctor import (
    KNOWN_PROFILES,
    PROFILE_REDMINE_CODEX,
)
from mozyo_bridge.application import (
    cli_agents,
    cli_cockpit,
    cli_docs_scaffold,
    cli_handoff,
    cli_observability,
    cli_release,
    cli_runtime_config,
    cli_session,
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
from mozyo_bridge.shared.paths import default_queue_path, default_tmux_conf, resolve_repo_root

# --- Backward-compatible import surface (Redmine #12138 / #12141 / #12153). ---
# Before the parser split, handler / helper / constant symbols were importable
# as ``mozyo_bridge.application.cli.<name>`` because ``cli.py`` imported them
# directly for the monolithic ``build_parser()``. The parser *registration* now
# lives in the family modules (``cli_agents`` / ``cli_cockpit`` / ``cli_handoff``
# / ``cli_observability`` / ``cli_runtime_config`` / ``cli_session`` plus the
# earlier ``cli_release`` / ``cli_docs_scaffold`` / ``cli_workspace``), but the
# module-level import path is preserved here so downstream imports / monkeypatch
# targets that referenced them through ``application.cli`` keep working. This is
# the #12138 scope guard "do not retire legacy import paths" applied to
# ``cli.py``; it does not affect parser behavior.
from mozyo_bridge.application.cli_common import add_scaffold_target_option  # noqa: F401,E402
from mozyo_bridge.application.cli_handoff import (  # noqa: F401,E402
    add_legacy_notify_options,
    add_notify_delivery_options,
    add_notify_options,
)
from mozyo_bridge.application.cli_runtime_config import (  # noqa: F401,E402
    _add_runtime_config_check_parser,
    _add_runtime_config_install_parser,
)
from mozyo_bridge.domain.agent_discovery import AGENT_KINDS  # noqa: F401,E402
from mozyo_bridge.domain.handoff import (  # noqa: F401,E402
    KIND_LABELS,
    MODE_QUEUE_ENTER,
    MODES,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
    SOURCES,
)
from mozyo_bridge.domain.session_boundary import SESSION_BOUNDARY_SIGNALS  # noqa: F401,E402
from mozyo_bridge.application.commands import (  # noqa: F401,E402
    cmd_agents_attention_project,
    cmd_agents_list,
    cmd_agents_targets,
    cmd_cockpit,
    cmd_config,
    cmd_docs_audit_impact,
    cmd_docs_generate,
    cmd_docs_resolve,
    cmd_docs_validate,
    cmd_events_query,
    cmd_events_tail,
    cmd_handoff_cross_workspace_consult,
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_instruction_doctor,
    cmd_instruction_install,
    cmd_layout_apply,
    cmd_message,
    cmd_notify_claude,
    cmd_notify_claude_legacy_task,
    cmd_notify_claude_review_result,
    cmd_notify_codex,
    cmd_notify_codex_legacy_task,
    cmd_notify_codex_review,
    cmd_otel_activity,
    cmd_otel_events,
    cmd_otel_launchd,
    cmd_otel_serve,
    cmd_otel_status,
    cmd_rules_home,
    cmd_rules_install,
    cmd_rules_status,
    cmd_scaffold_apply,
    cmd_scaffold_canonical,
    cmd_scaffold_diff,
    cmd_scaffold_status,
    cmd_session_boundary_prompt,
    cmd_session_list,
    cmd_session_name,
    cmd_session_pane_decision,
    cmd_session_vscode_settings,
    cmd_tmux_ui_install,
    cmd_tmux_ui_status,
    cmd_tmux_ui_uninstall,
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

    # `layout` + `cockpit` (first cockpit-family half); `agents` sits between
    # them and the `tmux-ui` half, so registration is ordered explicitly to
    # keep the pre-split top-level subcommand sequence (Redmine #12153).
    cli_cockpit.register(sub)

    cli_agents.register(sub)

    cli_cockpit.register_tmux_ui(sub)

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

    # `message` registers before `keys`; the notify-* / handoff / reply block
    # registers after it (Redmine #12153 ordering guard).
    cli_handoff.register_message(sub)

    keys = sub.add_parser("keys")
    keys.add_argument("target")
    keys.add_argument("keys", nargs="+")
    keys.set_defaults(func=cmd_keys)

    cli_handoff.register(sub)

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

    cli_runtime_config.register(sub)

    cli_docs_scaffold.register(sub)

    cli_observability.register(sub)

    cli_session.register(sub)

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
