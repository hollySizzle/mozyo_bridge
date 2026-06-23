"""CLI parser registration for the handoff / notify / message / reply family.

Split out of ``application/cli.py`` (Redmine #12153). Behavior-preserving;
the handlers themselves live in ``application/commands.py``. Block text is
moved verbatim from ``build_parser()`` so help / choices / defaults / dest /
``func`` bindings are unchanged.

The ``message`` command sits *before* ``keys`` in the pre-split parser, while
the ``notify-*`` / ``handoff`` / ``reply`` block sits *after* it. Top-level
subcommand order is observable in ``--help``, so registration is split:
:func:`register_message` emits ``message``; the caller then registers ``keys``;
:func:`register` emits the notify / handoff / reply block. Call them in that
order to reproduce the pre-split sequence exactly.

``add_notify_delivery_options`` / ``add_notify_options`` /
``add_legacy_notify_options`` are re-exported from ``application/cli.py`` to
preserve the pre-split module-level import surface (Redmine #12138 scope guard).
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands import (
    cmd_handoff_cross_workspace_consult,
    cmd_handoff_delegate_coordinator,
    cmd_handoff_delegate_coordinator_lane,
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_message,
    cmd_notify_claude,
    cmd_notify_claude_legacy_task,
    cmd_notify_claude_review_result,
    cmd_notify_codex,
    cmd_notify_codex_legacy_task,
    cmd_notify_codex_review,
)
from mozyo_bridge.domain.handoff import (
    KIND_LABELS,
    MODE_QUEUE_ENTER,
    MODES,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
    SOURCES,
)
from mozyo_bridge.domain.role_profile import ROLE_PROFILE_TOKENS


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
        "--role-profile",
        dest="role_profile",
        choices=list(ROLE_PROFILE_TOKENS),
        help=(
            "Optional fixed role profile to resolve and expand for the receiver "
            "(Redmine #12388). Resolves the builtin template from "
            "`vibes/docs/specs/delegated-coordinator-role-profile.md` (US "
            "#12387), substitutes `<...>` placeholders from `--profile-field` "
            "values (and auto-fills `durable_anchor` from the anchor), and "
            "carries the resolved role contract in the durable delivery record "
            "plus a compact single-line pointer in the notification body so the "
            "receiver reads its role contract without guessing a template path. "
            "Fails closed on an unknown role; omit the flag for the explicit "
            "fallback of no profile expansion. The role profile is the "
            "receiver's custom instruction and never enters the routing landing "
            "marker."
        ),
    )
    parser_.add_argument(
        "--profile-field",
        dest="profile_field",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "Repeatable `KEY=VALUE` substitution for a `--role-profile` "
            "template placeholder (e.g. `--profile-field parent_project=alpha`). "
            "`durable_anchor` is auto-filled from the anchor when not supplied. "
            "Unsupplied placeholders are left as literal `<name>` tokens and "
            "listed as unresolved in the record. Keep values repo-relative / "
            "redacted: they may reach the pasteable delivery record."
        ),
    )
    parser_.add_argument(
        "--main-lane-exception",
        dest="main_lane_exception",
        help=(
            "Authorize a `--to claude --kind implementation_request` send to the "
            "repo's default/main lane (Redmine #12441). Implementation-shaped "
            "work defaults to a cockpit-visible sublane, so a direct main-lane "
            "Claude implementation dispatch fails closed unless this flag "
            "references a durable owner/operator `main_lane_exception` decision "
            "(e.g. a Redmine journal pointer). Prefer routing via the target-lane "
            "Codex gateway (`--to codex`) or a sublane instead; this is the "
            "narrow, audited escape hatch, not the default path."
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
            "`pending` (operator/debug fallback, NOT the standard "
            "dispatch path) types but leaves the input pending for an "
            "operator to submit"
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
    parser_.add_argument(
        "--persist-delivery",
        dest="persist_delivery",
        action="store_true",
        help=(
            "Opt-in (Redmine #12311): durably persist the delivery record to "
            "the anchor's ticket system (a Redmine journal note) in addition "
            "to printing it, emitting a persistence receipt. Off by default, "
            "so the send is byte-identical without it. The durable record is a "
            "delivery pointer only — never a review / completion / approval — "
            "and persistence is best-effort and never blocks or alters the "
            "pane send. The live Redmine write transport (Redmine #12347) is "
            "wired behind a second explicit opt-in: set the trusted-environment "
            "`MOZYO_REDMINE_DELIVERY_WRITE` flag to enable the live journal "
            "write (it reuses the trusted `MOZYO_REDMINE_URL` / "
            "`MOZYO_REDMINE_API_KEY` credential boundary and fails closed on "
            "missing / unauthorized credentials). Without that env opt-in this "
            "stays a fail-closed `provider_unavailable` receipt, and "
            "`source=asana` has no write provider in v0.8 (`unsupported_source`)."
        ),
    )


def register_message(sub) -> None:
    """Register the `message` subcommand onto ``sub`` (pre-`keys` position)."""
    message = sub.add_parser("message")
    message.add_argument("target")
    message.add_argument("text")
    message.add_argument(
        "--no-submit",
        dest="submit",
        action="store_false",
        help=(
            "Operator/debug fallback: type the message but do not press Enter, "
            "leaving the input pending at the target prompt for an operator to "
            "submit. NOT the standard handoff path — standard same-lane dispatch "
            "/ handoff/reply submit-completes (`mozyo-bridge handoff send` on the "
            "default queue-enter rail, or marker-observed `--mode standard`). "
            "Sanctioned only as the per-preset `--no-submit` marker_timeout retry "
            "path or explicit operator debugging (Redmine #12207)."
        ),
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


def register(sub) -> None:
    """Register the notify-* / handoff / reply block onto ``sub`` (post-`keys`)."""
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

    handoff_delegate = handoff_sub.add_parser(
        "delegate-coordinator",
        help=(
            "Delegate work to a canonical project's Codex as "
            "`delegated_coordinator`, resolved from project-router metadata"
        ),
        description=(
            "High-level project-router delegation route (Redmine #12438 / US "
            "#12437). A coordinator names an external-submodule target (e.g. "
            "`giken-3800-mozyo-bridge`) from a gk-style `projects.yaml` instead "
            "of editing the submodule directly; this command reads that routing "
            "config, resolves the canonical project's Codex gateway pane by "
            "canonical-repo-root match, and sends a delegated handoff with the "
            "`delegated_coordinator` role profile. It is a boundary-preserving "
            "wrapper over `handoff send`: the receiver is fixed to `codex` (never "
            "a direct cross-project Claude send), the repo-identity gate is "
            "preserved (the chosen pane's cwd must resolve to the canonical repo "
            "root), and a missing / ambiguous gateway fails closed — the route "
            "never auto-launches a Unit and never creates a hidden worker. The "
            "role-profile fields (`parent_project` / `child_project` / "
            "`parent_callback_target` / `parent_issue` / `redmine_project`) are "
            "auto-filled from the router decision, with explicit "
            "`--profile-field` overrides winning. `--kind` defaults to "
            "`implementation_request`. Grandchild dispatch stays the delegated "
            "coordinator's own policy decision."
        ),
        epilog=(
            "Operational route:\n"
            "  1. Record the delegation request on the durable source of truth "
            "(Redmine issue/journal) first; the pane notification is only the "
            "pointer.\n"
            "  2. Ensure the canonical project's Codex Unit is loaded "
            "(`mozyo-bridge agents targets` shows it); this route never "
            "auto-launches it.\n"
            "  3. Run this command with `--projects-config` and "
            "`--target-project`; the target Codex pane is resolved "
            "automatically (or pass an explicit `--target %pane`).\n\n"
            "Example:\n"
            "  mozyo-bridge handoff delegate-coordinator \\\n"
            "    --source redmine --issue 12438 --journal 63431 \\\n"
            "    --projects-config ../gk-3500-it-operations/projects.yaml \\\n"
            "    --target-project giken-3800-mozyo-bridge \\\n"
            "    --parent-issue 12437 \\\n"
            "    --summary 'delegated coordinator handoff route'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configure_handoff_parser(
        handoff_delegate,
        kind_required=False,
        include_to=False,
        include_force=False,
        target_required=False,
        target_repo_required=False,
    )
    handoff_delegate.add_argument(
        "--projects-config",
        dest="projects_config",
        required=True,
        help=(
            "Path to the gk-style `projects.yaml` project-router config that "
            "classifies the target as an external-submodule and declares its "
            "canonical repo root / project."
        ),
    )
    handoff_delegate.add_argument(
        "--target-project",
        dest="target_project",
        required=True,
        help=(
            "External-submodule project id to delegate (e.g. "
            "`giken-3800-mozyo-bridge`); looked up in the `--projects-config`."
        ),
    )
    handoff_delegate.add_argument(
        "--parent-project",
        dest="parent_project",
        help=(
            "Override the delegating (parent) project id for the "
            "`parent_project` role-profile field; defaults to the project id "
            "declared at the top of `--projects-config` when present."
        ),
    )
    handoff_delegate.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help=(
            "Parent coordinator issue pointer for the `parent_issue` "
            "role-profile field (the issue the delegated coordinator must not "
            "close)."
        ),
    )
    handoff_delegate.add_argument(
        "--parent-callback-target",
        dest="parent_callback_target",
        help=(
            "Parent coordinator callback route for the "
            "`parent_callback_target` role-profile field (where the delegated "
            "coordinator returns handoff-worthy state / owner-approval needs)."
        ),
    )
    handoff_delegate.set_defaults(func=cmd_handoff_delegate_coordinator)

    handoff_delegate_lane = handoff_sub.add_parser(
        "delegate-coordinator-lane",
        help=(
            "Launch or explicitly adopt a visible delegated coordinator child "
            "lane (requires an explicit --lane launch|adopt decision)"
        ),
        description=(
            "Launch/adopt harness in front of `handoff delegate-coordinator` "
            "(Redmine #12447 / US #12437). The plain `delegate-coordinator` route "
            "silently selects whatever unique Codex pane already lives in the "
            "canonical repo, so an existing-lane route reads as PASS even when no "
            "fresh child lane was launched and no adoption was recorded (#12437 "
            "j#63530). This command requires an explicit `--lane {launch,adopt}` "
            "decision — there is no auto mode, so a pre-existing lane is never "
            "silently reused — and emits a replayable durable record (launch/adopt "
            "selection, target/parent issue, target project, canonical repo root, "
            "lane/worktree identity, callback route, parent->child delegation "
            "breadcrumb, no-hidden-subagent guarantee). `--lane adopt` resolves the "
            "visible existing canonical Codex lane (explicit `--adopt-target %pane` "
            "wins, else the unique unambiguous match) and hands off to it through "
            "the Codex gateway with the `delegated_coordinator` role profile (no "
            "direct cross-project Claude send). `--lane launch` produces a fresh "
            "lane identity (`--child-issue` + `--branch`/`--worktree`) and emits the "
            "launch plan: it never spawns the lane (mozyo-bridge core is not a git "
            "worktree manager) — the operator materializes the visible worktree / "
            "cockpit Unit and the live run is verified separately. Fails closed when "
            "the canonical root is absent locally or the launch/adopt identity is "
            "ambiguous. Owner approval and parent close authority stay on the parent "
            "coordinator. The durable record models purpose-tagged required callback "
            "targets (Redmine #12449): `--parent-callback-target` (delegation_parent) "
            "plus `--owning-us-coordinator` / `--audit-coordinator` when a separate US "
            "owns the child issue, so a single parent-only callback cannot pass."
        ),
        epilog=(
            "Examples:\n"
            "  # explicitly adopt the existing canonical Codex lane\n"
            "  mozyo-bridge handoff delegate-coordinator-lane --lane adopt \\\n"
            "    --source redmine --issue 12447 --journal 63531 \\\n"
            "    --projects-config ../gk-3500-it-operations/projects.yaml \\\n"
            "    --target-project giken-3800-mozyo-bridge \\\n"
            "    --parent-issue 12437 --parent-callback-target %8 \\\n"
            "    --owning-us-coordinator %6 \\\n"
            "    --adopt-target %10 --summary 'adopt child delegated coordinator'\n\n"
            "  # plan a fresh child lane launch (operator then materializes it)\n"
            "  mozyo-bridge handoff delegate-coordinator-lane --lane launch \\\n"
            "    --source redmine --issue 12447 --journal 63531 \\\n"
            "    --projects-config ../gk-3500-it-operations/projects.yaml \\\n"
            "    --target-project giken-3800-mozyo-bridge \\\n"
            "    --parent-issue 12437 --parent-callback-target %8 \\\n"
            "    --owning-us-coordinator %6 \\\n"
            "    --child-issue 12448 --branch issue_12448_live_verify \\\n"
            "    --worktree mozyo_bridge-12448"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configure_handoff_parser(
        handoff_delegate_lane,
        kind_required=False,
        include_to=False,
        include_force=False,
        target_required=False,
        target_repo_required=False,
    )
    handoff_delegate_lane.add_argument(
        "--lane",
        dest="lane",
        required=True,
        choices=["launch", "adopt"],
        help=(
            "Explicit lane decision. `launch` plans a fresh visible delegated "
            "coordinator lane; `adopt` explicitly adopts an existing one. There is "
            "no auto mode: a pre-existing lane is never silently reused as PASS."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--projects-config",
        dest="projects_config",
        required=True,
        help=(
            "Path to the gk-style `projects.yaml` project-router config that "
            "classifies the target as an external-submodule and declares its "
            "canonical repo root / project."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--target-project",
        dest="target_project",
        required=True,
        help=(
            "External-submodule project id to delegate (e.g. "
            "`giken-3800-mozyo-bridge`); looked up in the `--projects-config`."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--adopt-target",
        dest="adopt_target",
        help=(
            "(--lane adopt) Explicit `%%pane` of the existing canonical Codex lane "
            "to adopt; overrides discovery. Omit to adopt the unique unambiguous "
            "canonical Codex lane (fail-closed on absent / ambiguous)."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--child-issue",
        dest="child_issue",
        help=(
            "(--lane launch) The child issue the fresh lane will work; part of the "
            "replayable lane identity."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--branch",
        dest="branch",
        help="(--lane launch) Branch identity for the fresh lane.",
    )
    handoff_delegate_lane.add_argument(
        "--worktree",
        dest="worktree",
        help="(--lane launch) Worktree identity for the fresh lane.",
    )
    handoff_delegate_lane.add_argument(
        "--lane-id",
        dest="lane_id",
        help=(
            "Optional explicit lane id (e.g. `lane-<hash>`); defaults to the "
            "adopted lane's discovered id, or is derived when the lane is "
            "materialized."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--parent-project",
        dest="parent_project",
        help=(
            "Override the delegating (parent) project id; defaults to the project "
            "declared at the top of `--projects-config` when present."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help=(
            "Parent coordinator issue pointer (the issue the delegated coordinator "
            "must not close); also seeds the delegation breadcrumb."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--parent-callback-target",
        dest="parent_callback_target",
        help=(
            "Parent coordinator callback route (the `delegation_parent` callback "
            "target: where the delegated coordinator returns handoff-worthy state "
            "/ owner-approval needs)."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--owning-us-coordinator",
        dest="owning_us_coordinator",
        help=(
            "(#12449) Callback route for the coordinator owning the child issue's "
            "US-level audit / disposition, when distinct from the parent project "
            "coordinator. Recorded as a separate required callback target so a "
            "single parent-only callback cannot pass. If it resolves to the same "
            "route as `--parent-callback-target`, both purposes are recorded "
            "explicitly on that one target."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--audit-coordinator",
        dest="audit_coordinator",
        help=(
            "(#12449) Explicit audit coordinator callback route, when distinct "
            "from both the parent and the owning-US coordinator. Recorded as an "
            "additional required callback target."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--delegation-root",
        dest="delegation_root",
        help=(
            "Optional explicit delegation-tree root unit pointer (display / audit "
            "breadcrumb, not routing identity); defaults to the parent pointer."
        ),
    )
    handoff_delegate_lane.add_argument(
        "--delegation-parent",
        dest="delegation_parent",
        help=(
            "Optional explicit direct-parent unit pointer (display / audit "
            "breadcrumb, not routing identity); defaults to the delegation root."
        ),
    )
    handoff_delegate_lane.set_defaults(func=cmd_handoff_delegate_coordinator_lane)

    reply_alias = sub.add_parser(
        "reply",
        help="Alias for `mozyo-bridge handoff reply` (kind defaults to `reply`)",
    )
    configure_handoff_parser(reply_alias, kind_required=False)
    reply_alias.set_defaults(func=cmd_handoff_reply)
