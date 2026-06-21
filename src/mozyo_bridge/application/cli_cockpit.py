"""CLI parser registration for the cockpit / layout / tmux-ui family.

Split out of ``application/cli.py`` (Redmine #12153). Behavior-preserving;
the handlers themselves live in ``application/commands.py``. Block text is
moved verbatim from ``build_parser()`` so help / choices / defaults / dest /
``func`` bindings are unchanged.

Top-level subcommand *order* is observable in ``--help`` (the positional
``{...}`` metavar and body), so registration is split into two ordered entry
points: :func:`register` emits ``layout`` then ``cockpit``; the caller then
registers ``agents`` (which sits between the two halves in the original
parser); :func:`register_tmux_ui` emits ``tmux-ui-config`` then ``tmux-ui``.
Call them in that sequence to reproduce the pre-split order exactly.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands import (
    cmd_cockpit,
    cmd_config,
    cmd_layout_apply,
    cmd_tmux_ui_install,
    cmd_tmux_ui_status,
    cmd_tmux_ui_uninstall,
)


def register(sub) -> None:
    """Register `layout` then `cockpit` onto ``sub`` (first cockpit-family half)."""
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
        choices=[
            "append", "adopt", "reset", "rebuild", "doctor-geometry",
            "peer-adopt", "rebalance", "reconcile", "list", "status",
        ],
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
            "cwd/process preflight). `rebalance` (Redmine #12135) restores existing "
            "live columns toward an equal fair-share width with `resize-pane` and "
            "`--confirm`; it touches column width only — identity pane options are "
            "untouched, the Codex/Claude vertical splits are not flattened, and the "
            "#12133 missing/role-less drift is left to that scope. `reconcile` (Redmine "
            "#12136) repairs a structural layout-tree drift where two Units are "
            "nested in one tmux cell (a 2x2 grid): with `--confirm` it flattens "
            "each tangled cell into clean per-Unit columns (order preserved) via "
            "`swap-pane` + a checksum-valid `select-layout`, killing no pane and "
            "reading Unit identity from pane options; it fails closed on an "
            "unidentified pane (#12133 scope). `list` and `status` (Redmine "
            "#12341) are read-only operator-facing membership summaries: `list` "
            "enumerates the workspaces loaded in the cockpit (workspace label/id, "
            "repo root, window, Codex/Claude pane ids, geometry status, registry/"
            "anchor presence), and `status --repo <repo>` reports whether that one "
            "repo's workspace is loaded — saying so explicitly when it is NOT, "
            "instead of leaving you to infer it from `status`'s `agent window "
            "missing`. Both take `--json` for UI / tests; cockpit membership is a "
            "display/liveness projection, never Redmine workflow / close truth. "
            "Without "
            "`--confirm` every other sub-action is detect-only / preview and "
            "mutates nothing; `--dry-run` / `--json` always preview without "
            "mutating."
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
            "mozyo-identified cockpit session; for `rebalance` (Redmine #12135) "
            "it applies the `resize-pane` fair-share width plan; for `reconcile` "
            "(Redmine #12136) it applies the `swap-pane` + `select-layout` "
            "structural flatten. Required to mutate; without it these sub-actions "
            "are detect-only / preview. `--dry-run` / `--json` still only preview."
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


def register_tmux_ui(sub) -> None:
    """Register `tmux-ui-config` then `tmux-ui` onto ``sub`` (second half)."""
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
