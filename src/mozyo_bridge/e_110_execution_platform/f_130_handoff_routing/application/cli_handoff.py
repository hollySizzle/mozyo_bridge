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
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_handoff_ticketless_callback,
    cmd_message,
    cmd_notify_claude,
    cmd_notify_claude_legacy_task,
    cmd_notify_claude_review_result,
    cmd_notify_codex,
    cmd_notify_codex_legacy_task,
    cmd_notify_codex_review,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegation_launch_adopt import (
    cmd_handoff_delegate_launch_adopt,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_handoff_grandchild_realization import (
    register_grandchild_realization,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.grandchild_dispatch import (
    cmd_handoff_grandchild_dispatch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import LAUNCH_ADOPT_MODES
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_dispatch import RECORD_POLICIES
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    KIND_LABELS,
    MODE_QUEUE_ENTER,
    MODES,
    QUEUE_ENTER_RETRY_INTERVAL_SECONDS,
    QUEUE_ENTER_RETRY_WINDOW_SECONDS,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
    SOURCES,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import ROLE_PROFILE_TOKENS
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_ticketless import (
    configure_ticketless_callback_parser,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_q_enter import (
    register_q_enter,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_select import (
    add_handoff_select_args,
    add_message_select_args,
)


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
    source_required: bool = True,
) -> None:
    if include_to:
        parser_.add_argument("--to", required=True, choices=["claude", "codex"], help="Semantic receiver agent")
    parser_.add_argument(
        "--source",
        required=source_required,
        choices=sorted(SOURCES),
        help="Durable record source system",
    )
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
        "--target-project",
        dest="target_project",
        help=(
            "Optional project-scope gate (Redmine #12658), layered ON TOP of "
            "the Git `--target-repo` gate, never replacing it. REQUIRES "
            "`--target-repo` (or `--target-repo auto`) — project scope is layered "
            "under workspace identity and must not be the sole identity gate. "
            "When set, the target pane must (1) pass the repo-root gate and (2) "
            "resolve to this adopted project scope (its `redmine_project` id) with "
            "its cwd under the project path; otherwise the handoff is rejected "
            "with `target_project_mismatch`. A target in the correct Git repo but "
            "outside the expected project path fails closed. A stamped "
            "`@mozyo_project_scope` pane option is trusted only when the pane cwd "
            "is actually under the stamped project path. Drop the flag to gate on "
            "the Git repo root only."
        ),
    )
    parser_.add_argument(
        "--allow-direct-worker",
        dest="allow_direct_worker",
        action="store_true",
        help=(
            "Explicit durable exception to the gateway-route enforcement gate "
            "(Redmine #12918). By default a governed implementation_request / "
            "review_result addressed `--to claude` directly to a worker in a "
            "different lane than the sender fails closed with "
            "`gateway_route_blocked`, because the governed route is coordinator -> "
            "sublane Codex gateway -> same-lane Claude worker. Pass this flag ONLY "
            "when a direct cross-lane worker delivery is genuinely required; the "
            "send is then admitted but recorded distinctly as a "
            "`gateway_route_exception` so the bypass is auditable. It does not "
            "relax any cross-session / `--target-repo` / project gate."
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
        "--queue-enter-retry-window",
        dest="queue_enter_retry_window",
        type=float,
        default=QUEUE_ENTER_RETRY_WINDOW_SECONDS,
        help=(
            "queue-enter Enter-only retry window in seconds (default "
            f"{QUEUE_ENTER_RETRY_WINDOW_SECONDS:g}). When the landing marker is "
            "not observed, Enter — and only Enter; the marker+body is never "
            "re-typed — is re-issued every --queue-enter-retry-interval seconds "
            "until the marker is observed or this window elapses. `0` disables "
            "the retry (single Enter). Ignored under --mode standard/pending."
        ),
    )
    parser_.add_argument(
        "--queue-enter-retry-interval",
        dest="queue_enter_retry_interval",
        type=float,
        default=QUEUE_ENTER_RETRY_INTERVAL_SECONDS,
        help=(
            "Seconds between Enter-only retries on the queue-enter rail "
            f"(default {QUEUE_ENTER_RETRY_INTERVAL_SECONDS:g}). `0` disables the "
            "retry."
        ),
    )
    parser_.add_argument(
        "--no-target-activation",
        dest="no_target_activation",
        action="store_true",
        help=(
            "Disable standard_target_admission activation (Redmine #12597): an "
            "inactive registered agent pane stays fail-closed exactly like the "
            "pre-#12597 active-split gate instead of being activated via tmux "
            "`select-pane` and delivered to. By default the queue-enter rail "
            "admits an inactive split that passes the minimal admission contract "
            "(live pane / strong role match / workspace_id / unambiguous) and "
            "activates it before typing. Ignored under --mode standard/pending."
        ),
    )
    parser_.add_argument(
        "--restore-previous-active",
        dest="restore_previous_active",
        action="store_true",
        help=(
            "After standard_target_admission activates an inactive split "
            "(Redmine #12597), re-select the pane that was the active split of "
            "the target's window before delivery. Off by default — the receiver "
            "pane is left active, which resolves the original active-split "
            "concern. Pane selection only; no raw key injection."
        ),
    )
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
    # `target` is optional under `--select-role` semantic resolution (Redmine
    # #12663): an operator/ticketless message can name the Codex gateway by role
    # + repo instead of a hand-copied `%pane`. Exactly one of `target` /
    # `--select-role` must be given (validated in `cmd_message`).
    message.add_argument("target", nargs="?")
    message.add_argument("text")
    add_message_select_args(message)
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
    # Semantic target selection (Redmine #12663): resolve the pane from `--to` +
    # `--target-repo` (+ session/project) instead of a `%pane`; fail-closed on
    # 0/many and never weakens the `--target-repo`/`--target-project` gates.
    add_handoff_select_args(handoff_send)
    handoff_send.set_defaults(func=cmd_handoff_send)

    handoff_reply = handoff_sub.add_parser(
        "reply",
        help="Send a reply notification from sender to receiver (kind defaults to `reply`)",
    )
    configure_handoff_parser(handoff_reply, kind_required=False)
    handoff_reply.set_defaults(func=cmd_handoff_reply)

    handoff_ticketless = handoff_sub.add_parser(
        "ticketless-callback",
        help=(
            "Standard ticketless no-anchor callback / hands-off transport — return "
            "a consultation result to the caller lane without a Redmine anchor"
        ),
        description=(
            "Standard product primitive for the ticketless consultation-phase "
            "callback (#12703 ticketless no-anchor callback transport). "
            "#12698 GK3500 ticketless exploratory smoke surfaced that a "
            "ticketless `no_dispatch` / consultation hands-off result could not be "
            "returned over `handoff reply`, which requires a Redmine anchor "
            "(`--issue` + `--journal`) and so failed closed with `invalid_anchor`. "
            "This rail carries the structured callback result "
            "(`--classification` / `--dispatch-decision` / `--workflow-next-owner` "
            "/ `--callback-reason` / `--read-contract`, with `redmine_anchor_"
            "required` derived) over the SAME standard delivery rail (queue-enter "
            "/ standard semantics, the same target-admission / repo-identity / "
            "cross-session gates) WITHOUT a Redmine anchor and without fabricating "
            "one. The transport outcome (status / reason / marker) is recorded "
            "distinctly from the workflow result (the ticketless callback fields).\n\n"
            "Boundary preserved: this does NOT touch the Redmine-governed "
            "`handoff reply` / `reply` rail (those still require `--issue` + "
            "`--journal`), and it fails closed if `--dispatch-decision` is an "
            "actual child->grandchild worker dispatch — that still requires a real "
            "Redmine anchor via `handoff send --kind implementation_request`."
        ),
        epilog=(
            "Example (GK3500 grandparent gateway returns a no_dispatch result to "
            "the caller Codex):\n"
            "  mozyo-bridge handoff ticketless-callback \\\n"
            "    --to codex --target %0 --target-repo auto \\\n"
            "    --classification no_dispatch \\\n"
            "    --dispatch-decision hand_back_to_caller \\\n"
            "    --workflow-next-owner caller \\\n"
            "    --callback-reason no_dispatch_decided \\\n"
            "    --read-contract grandparent_coordinator \\\n"
            "    --summary 'ticketless consultation: no implementation dispatch'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configure_ticketless_callback_parser(handoff_ticketless)
    handoff_ticketless.set_defaults(func=cmd_handoff_ticketless_callback)

    register_q_enter(handoff_sub)

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

    _register_delegate_launch_adopt(handoff_sub)
    _register_grandchild_dispatch(handoff_sub)
    register_grandchild_realization(handoff_sub)

    reply_alias = sub.add_parser(
        "reply",
        help="Alias for `mozyo-bridge handoff reply` (kind defaults to `reply`)",
    )
    configure_handoff_parser(reply_alias, kind_required=False)
    reply_alias.set_defaults(func=cmd_handoff_reply)


def _register_delegate_launch_adopt(handoff_sub) -> None:
    """Register `handoff delegate-launch-adopt` (Redmine #12457).

    Read-only decision primitive for the parent -> delegated coordinator route.
    It resolves a fail-closed launch/adopt decision over `agents targets`
    candidate discovery and prints the decision + the durable parent-delegation
    record + (for an adopt outcome) the gated `handoff send --to codex` command
    the operator runs. It never sends and never targets a child Claude directly.
    """
    parser = handoff_sub.add_parser(
        "delegate-launch-adopt",
        help=(
            "Resolve a fail-closed delegated coordinator launch/adopt decision "
            "from durable policy + `agents targets` candidate discovery (Redmine "
            "#12457)"
        ),
        description=(
            "Decision primitive for the parent -> delegated coordinator route "
            "(Redmine #12457, US #12454). It is READ-ONLY and never sends: it "
            "runs the `agents targets` discovery pipeline, deterministically "
            "filters candidates by the Codex gateway role, the canonical child "
            "repo identity (`--target-repo`), lane state, and uniqueness, and "
            "resolves `--launch-adopt-mode` (disabled / adopt_existing / "
            "launch_new / launch_or_adopt) to an adopt / launch / fail_closed "
            "outcome. `agents targets` is candidate discovery only — selection "
            "fails closed on a disabled policy, a missing repo identity, a weak / "
            "ambiguous identity, zero candidates (unless the mode launches), or "
            "more than one match. For an adopt outcome it prints the gated "
            "`handoff send --to codex` command to run manually; it never targets "
            "a child Claude directly and uses no window / session / title / "
            "display proximity as routing authority. The durable anchor stays "
            "the Redmine issue / journal; this command prints a pointer + a "
            "pasteable parent delegation decision record."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--launch-adopt-mode",
        dest="launch_adopt_mode",
        required=True,
        choices=sorted(LAUNCH_ADOPT_MODES),
        help=(
            "Durable launch/adopt policy mode (read from the durable record, not "
            "pane proximity): `disabled` forms no route (fail-closed missing "
            "policy); `adopt_existing` adopts exactly one matching child Codex "
            "gateway; `launch_new` always launches a new lane; `launch_or_adopt` "
            "adopts a unique match else launches, and fails closed on more than "
            "one match."
        ),
    )
    parser.add_argument(
        "--target-repo",
        dest="target_repo",
        required=True,
        help=(
            "Mandatory canonical child repo identity gate: a candidate is "
            "adoptable only when its pane cwd resolves to this repo root. Without "
            "it the decision fails closed (selecting a pane from layout alone "
            "would recreate the #12455 missing-context violation). Pass the "
            "explicit canonical child repo root path."
        ),
    )
    parser.add_argument(
        "--parent-coordinator-route",
        dest="parent_coordinator_route",
        required=True,
        help=(
            "Durable route anchor of the parent coordinator (the mandatory "
            "`delegation_parent` callback target). The parent retains parent "
            "issue close / owner approval authority, so every route must be "
            "callbackable to it."
        ),
    )
    parser.add_argument(
        "--callback-target",
        dest="callback_target",
        action="append",
        metavar="PURPOSE=ROUTE",
        help=(
            "Repeatable additional callback target "
            "(`owning_us_coordinator=<route>` / `audit_coordinator=<route>`) for "
            "the child project's owning-US / audit coordinator when it is a "
            "different lane than the delegation parent."
        ),
    )
    parser.add_argument(
        "--child-project",
        dest="child_project",
        help="Child project identifier recorded in the delegation decision.",
    )
    parser.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help="Parent issue / US id recorded in the delegation decision.",
    )
    parser.add_argument(
        "--child-issue",
        dest="child_issue",
        help=(
            "Child project issue id used in the recommended `handoff send` "
            "anchor (defaults to a placeholder in the printed command)."
        ),
    )
    parser.add_argument(
        "--parent-project",
        dest="parent_project",
        help="Parent project identifier for the role-profile `parent_project` field.",
    )
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        default="redmine",
        help="Durable record source system for the recommended command (default redmine).",
    )
    parser.add_argument(
        "--journal",
        help="Optional Redmine journal id for the recommended command anchor.",
    )
    parser.add_argument(
        "--excluded-lane",
        dest="excluded_lane",
        action="append",
        metavar="LANE_ID",
        help=(
            "Repeatable lane id to exclude from adoption (e.g. a retired or "
            "incompatible-active lane) so it never becomes a candidate."
        ),
    )
    parser.add_argument(
        "--session",
        help="Restrict candidate discovery to this tmux session (read-only filter).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the decision, callback targets, recommended command, and record as JSON.",
    )
    parser.set_defaults(func=cmd_handoff_delegate_launch_adopt)


def _register_grandchild_dispatch(handoff_sub) -> None:
    """Register `handoff delegate-grandchild-dispatch` (Redmine #12458).

    Read-only decision primitive for the delegated coordinator -> grandchild
    implementation lane route (depth 2). It resolves the delegation policy gate
    (`enable_grandchild_dispatch` / `max_delegation_depth: 2` / master gate /
    active-lane capacity) and a fail-closed launch/adopt decision over `agents
    targets` candidate discovery, and prints the decision + the durable
    `## Delegated dispatch decision` (decision-records §2) + `## Delegated
    callback targets` (§4) records + (for an adopt outcome) the gated `handoff
    send --to codex` command the operator runs. It never sends, never targets a
    grandchild Claude directly, and the grandchild lane is always a declared
    durable-anchored cockpit lane, never a hidden subagent.
    """
    parser = handoff_sub.add_parser(
        "delegate-grandchild-dispatch",
        help=(
            "Resolve a fail-closed delegated coordinator -> grandchild dispatch "
            "decision from delegation policy + `agents targets` candidate "
            "discovery (Redmine #12458)"
        ),
        description=(
            "Decision primitive for the delegated coordinator -> grandchild "
            "implementation lane route (Redmine #12458, US #12454, depth 2). It "
            "is READ-ONLY and never sends. It first resolves the delegation "
            "policy gate — the `enable_delegated_coordinator` master gate, the "
            "`enable_grandchild_dispatch` depth-2 permission, the "
            "`max_delegation_depth` hop ceiling (hard ceiling 2), and the "
            "`max_active_child_lanes` capacity — and fails closed with an "
            "explicit reason if depth-2 dispatch is not permitted. When "
            "permitted it runs the `agents targets` discovery pipeline, "
            "deterministically filters candidates by the Codex gateway role, the "
            "canonical child repo identity (`--target-repo`), lane state, and "
            "uniqueness, and resolves `--launch-adopt-mode` to a dispatch_adopt / "
            "dispatch_launch / fail_closed outcome; `--no-dispatch REASON` records "
            "the `grandchild_dispatch: avoided` path instead. `agents targets` is "
            "candidate discovery only; selection never targets a grandchild Claude "
            "directly and uses no window / session / title / display proximity as "
            "routing authority. The grandchild lane it decides to launch / adopt "
            "is always a declared durable-anchored cockpit lane, never a hidden "
            "subagent. The durable anchor stays the Redmine issue / journal; this "
            "command prints a pointer + the §2 dispatch decision record and the "
            "§4 multi-coordinator callback targets record (the GK parent route "
            "and the mozyo_bridge coordinator route are both required and "
            "replayable)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--launch-adopt-mode",
        dest="launch_adopt_mode",
        default="launch_or_adopt",
        choices=sorted(LAUNCH_ADOPT_MODES),
        help=(
            "Durable launch/adopt policy mode for the grandchild lane (read from "
            "the durable record, not pane proximity): `disabled` forms no route; "
            "`adopt_existing` adopts exactly one matching grandchild Codex "
            "gateway; `launch_new` always launches a new lane; `launch_or_adopt` "
            "(default) adopts a unique match else launches, and fails closed on "
            "more than one match. Ignored for `--no-dispatch`."
        ),
    )
    parser.add_argument(
        "--target-repo",
        dest="target_repo",
        help=(
            "Mandatory canonical child repo identity gate for an adopt decision: "
            "a candidate is adoptable only when its pane cwd resolves to this "
            "repo root. Without it the dispatch fails closed (selecting a pane "
            "from layout alone would recreate the #12455 missing-context "
            "violation). Not required for `--no-dispatch`."
        ),
    )
    # --- delegation policy knobs (durable-record-derived; config loader is the
    #     #12390 follow-up). Out-of-range values clamp fail-closed in the domain.
    parser.add_argument(
        "--enable-delegated-coordinator",
        dest="enable_delegated_coordinator",
        action="store_true",
        help="Master gate: nested delegation is permitted (default off / safety-biased).",
    )
    parser.add_argument(
        "--enable-grandchild-dispatch",
        dest="enable_grandchild_dispatch",
        action="store_true",
        help="Permit depth-2 (grandchild) dispatch (default off; requires the master gate and depth>=2).",
    )
    parser.add_argument(
        "--max-delegation-depth",
        dest="max_delegation_depth",
        type=int,
        default=1,
        help="Root-relative delegation hop ceiling (0..2; hard ceiling 2). Grandchild needs >=2. Default 1.",
    )
    parser.add_argument(
        "--max-active-child-lanes",
        dest="max_active_child_lanes",
        type=int,
        default=1,
        help="Max concurrent child/grandchild lanes one delegated coordinator may hold (>=1). Default 1.",
    )
    parser.add_argument(
        "--decision-record-policy",
        dest="decision_record_policy",
        default="minimal",
        choices=sorted(RECORD_POLICIES),
        help="No-dispatch / context-neutral record granularity (`minimal` | `verbose`). Default minimal.",
    )
    parser.add_argument(
        "--current-depth",
        dest="current_depth",
        type=int,
        default=1,
        help="Depth of the dispatching delegated coordinator (default 1; grandchild lands at current+1).",
    )
    parser.add_argument(
        "--active-grandchild-lanes",
        dest="active_grandchild_lanes",
        type=int,
        default=0,
        help="Count of active grandchild lanes the delegated coordinator already holds (capacity check).",
    )
    parser.add_argument(
        "--no-dispatch",
        dest="no_dispatch",
        metavar="REASON",
        help=(
            "Record an explicit `grandchild_dispatch: avoided` no-dispatch "
            "decision with this reason (decision-records §3; e.g. "
            "`context_cost_low` / `single_pass_no_iteration` / "
            "`urgent_minimal_correction` or a borderline `<具体記述>`). Skips tmux "
            "discovery — the delegated coordinator keeps the work in its own lane."
        ),
    )
    # --- multi-coordinator callback coverage (decision-records §4.1) ----------
    parser.add_argument(
        "--parent-coordinator-route",
        dest="parent_coordinator_route",
        required=True,
        help=(
            "Durable route anchor of the GK parent coordinator (the mandatory "
            "`delegation_parent` callback target). The parent retains parent "
            "issue close / owner approval authority, so every route is "
            "callbackable to it."
        ),
    )
    parser.add_argument(
        "--owning-coordinator-route",
        dest="owning_coordinator_route",
        help=(
            "Durable route anchor of the mozyo_bridge owning-US / audit "
            "coordinator (the required `owning_us_coordinator` callback target) "
            "when it is a different lane than the GK parent. Both this route and "
            "the parent route must be replayable; supply this OR "
            "`--owning-same-as-parent`."
        ),
    )
    parser.add_argument(
        "--owning-same-as-parent",
        dest="owning_same_as_parent",
        action="store_true",
        help=(
            "Declare the owning-US / audit coordinator callback route is the same "
            "as the delegation parent route (explicit `same_as_delegation_parent` "
            "— coverage is never omitted by assumption)."
        ),
    )
    parser.add_argument(
        "--callback-target",
        dest="callback_target",
        action="append",
        metavar="PURPOSE=ROUTE",
        help=(
            "Repeatable additional callback target "
            "(`owning_us_coordinator=<route>` / `audit_coordinator=<route>`)."
        ),
    )
    parser.add_argument(
        "--child-project",
        dest="child_project",
        help="Child project identifier recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--delegated-coordinator",
        dest="delegated_coordinator",
        help="Delegated coordinator lane pointer recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help="Parent issue / US id recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--child-issue",
        dest="child_issue",
        help="Child (grandchild-target) issue id recorded in the decision / recommended command.",
    )
    parser.add_argument(
        "--dispatch-anchor",
        dest="dispatch_anchor",
        help="Durable dispatch anchor pointer recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        default="redmine",
        help="Durable record source system for the recommended command (default redmine).",
    )
    parser.add_argument(
        "--journal",
        help="Optional Redmine journal id for the recommended command anchor.",
    )
    parser.add_argument(
        "--excluded-lane",
        dest="excluded_lane",
        action="append",
        metavar="LANE_ID",
        help="Repeatable lane id to exclude from adoption so it never becomes a candidate.",
    )
    parser.add_argument(
        "--session",
        help="Restrict candidate discovery to this tmux session (read-only filter).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the decision, policy gate, callback targets, recommended command, and records as JSON.",
    )
    parser.set_defaults(func=cmd_handoff_grandchild_dispatch)
