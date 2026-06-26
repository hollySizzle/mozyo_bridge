"""CLI parser registration for the cross-workspace agent-discovery family.

Split out of ``application/cli.py`` (Redmine #12153). Behavior-preserving;
the `agents` handlers themselves live in ``application/commands.py``. The
block text is moved verbatim from ``build_parser()`` so help / choices /
defaults / dest / ``func`` bindings are unchanged.
"""
from __future__ import annotations

from mozyo_bridge.application.commands import (
    cmd_agents_attention_project,
    cmd_agents_list,
    cmd_agents_targets,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import AGENT_KINDS


def register(sub) -> None:
    """Register the `agents` subcommand tree onto ``sub``."""
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
