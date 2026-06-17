"""CLI parser registration for the session identity / lifecycle family.

Split out of ``application/cli.py`` (Redmine #12153). Behavior-preserving;
the handlers themselves live in ``application/commands.py``. Block text is
moved verbatim from ``build_parser()`` so help / choices / defaults / dest /
``func`` bindings are unchanged.
"""
from __future__ import annotations

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands import (
    cmd_session_boundary_prompt,
    cmd_session_list,
    cmd_session_name,
    cmd_session_pane_decision,
    cmd_session_vscode_settings,
)
from mozyo_bridge.domain.session_boundary import SESSION_BOUNDARY_SIGNALS


def register(sub) -> None:
    """Register the `session` subcommand tree onto ``sub``."""
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
