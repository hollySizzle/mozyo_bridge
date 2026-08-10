"""CLI parser configuration for `handoff ticketless-callback` (#12703 ticketless no-anchor callback transport).

Split out of :mod:`...application.cli_handoff` so the ticketless no-anchor
callback rail's parser surface lives in its own module (keeping the shared
handoff registrar cohesive and under the module-health threshold). The handler
itself (``cmd_handoff_ticketless_callback``) lives in
``application/commands.py`` alongside the other handoff handlers; this module
only declares the argparse surface.

The structured callback fields are all required so the result is replayable by
construction. The ``--dispatch-decision`` choices expose ONLY the no-anchor-safe
decisions — an actual child -> grandchild worker dispatch is not expressible on
this rail (and is rejected fail-closed in the domain layer too), so the worker
anchor requirement is not relaxed. The rail carries no ``--source`` / ``--issue``
/ ``--journal`` / ``--task-id`` (it never fabricates an anchor) and no
``--persist-delivery`` (there is no ticket anchor to persist a journal note onto).
"""
from __future__ import annotations

import argparse

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (
    submit_delay_help,
)

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (
    AgentProviderRuntimeSnapshot,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_receiver_vocab import (
    receiver_choices,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    MODE_QUEUE_ENTER,
    MODES,
    QUEUE_ENTER_RETRY_INTERVAL_SECONDS,
    QUEUE_ENTER_RETRY_WINDOW_SECONDS,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    CALLBACK_REASONS,
    CLASSIFICATIONS,
    NEXT_ACTION_OWNERS,
    READ_CONTRACT_TOKENS,
    TICKETLESS_DISPATCH_DECISIONS,
)


def _add_ticketless_delivery_options(
    parser_: argparse.ArgumentParser,
    *,
    snapshot: AgentProviderRuntimeSnapshot | None = None,
) -> None:
    """Add the delivery/record knobs the ticketless callback rail shares.

    A focused subset of ``configure_handoff_parser``: it deliberately omits
    ``--source`` / ``--issue`` / ``--journal`` / ``--task-id`` / ``--comment-id``
    / ``--anchor-url`` (the ticketless rail carries no anchor and never fabricates
    one), ``--role-profile`` / ``--profile-field`` (out of scope), ``--force``
    (queue-enter rejects non-agent panes), and ``--persist-delivery`` (there is no
    Redmine anchor to persist a journal note onto). The pane-delivery semantics
    (mode / landing / queue-enter retry / target activation) are identical to
    ``handoff send`` so the callback rides the standard rail safely.
    """
    parser_.add_argument(
        "--to", required=True, choices=receiver_choices(snapshot),
        help="Semantic receiver agent — the caller lane the callback returns to",
    )
    parser_.add_argument(
        "--target",
        help=(
            "Optional tmux target override (an explicit `%%pane` for the caller "
            "lane); defaults to same-session agent-window resolution from --to"
        ),
    )
    parser_.add_argument(
        "--target-repo",
        dest="target_repo",
        help=(
            "Optional cross-workspace identity gate (#10332 cross-workspace handoff "
            "identity gate): the target "
            "pane's cwd must resolve to this repo root, else the callback is "
            "rejected with `target_repo_mismatch`. Omitting the flag skips the repo "
            "gate — but a relative `--workdir` then resolves against the SENDER's "
            "cwd, so do NOT drop it to get past an `auto` failure (Redmine #14249). "
            "`auto` resolves per backend: under tmux from an explicit `%%pane` "
            "target's own cwd, which it therefore requires; under herdr there is no "
            "pane cwd, so it resolves the TARGET's own frame — this checkout when the "
            "target is the sender's own lane, otherwise that lane's canonical worktree "
            "via its lifecycle worktree binding. Both stay fail-closed "
            "(`target_repo_mismatch` / `auto_target_repo_unresolved`) and never fall "
            "back to the sender's root; pass an explicit `--target-repo <root>` "
            "instead."
        ),
    )
    parser_.add_argument(
        "--target-project",
        dest="target_project",
        help=(
            "Optional project-scope gate (#12658 project-scoped cockpit identity), "
            "layered on top of "
            "`--target-repo` (which it requires), never replacing it."
        ),
    )
    parser_.add_argument(
        "--mode",
        choices=sorted(MODES),
        default=MODE_QUEUE_ENTER,
        help=(
            "Pane delivery rail (identical semantics to `handoff send`): "
            "`queue-enter` (default; body once, Herdr causal wait before each "
            "Enter and fail-closed `turn_start_absent` / `receiver_blocked` / "
            "`turn_start_unconfirmed` / `transport_error` result, tmux legacy "
            "marker retry). A failed post-body generation recheck, first wait "
            "arm, or deadline check means zero Enter and nonzero blocked; the "
            "staged body makes blind retry unsafe; "
            "`standard` (strict, C-u rollback on marker timeout), or `pending` "
            "(operator/debug)."
        ),
    )
    parser_.add_argument(
        "--summary",
        help=(
            "Optional human-readable narrative appended to the callback "
            "notification body. The replayable result is the structured callback "
            "fields, not this free text."
        ),
    )
    parser_.add_argument(
        "--landing-timeout", dest="landing_timeout", type=float, default=8.0,
        help="Seconds to wait for the landing marker before pressing Enter.",
    )
    parser_.add_argument(
        "--submit-delay",
        dest="submit_delay",
        type=float,
        default=0.2,
        help=submit_delay_help(),
    )
    parser_.add_argument("--read-lines", dest="read_lines", type=int, default=50)
    parser_.add_argument(
        "--queue-enter-retry-window", dest="queue_enter_retry_window",
        type=float, default=QUEUE_ENTER_RETRY_WINDOW_SECONDS,
        help=(
            "Absolute queue-enter retry budget in seconds (default "
            f"{QUEUE_ENTER_RETRY_WINDOW_SECONDS:g}); body is typed once. Herdr "
            "includes the first pre-armed wait, interval delays, and every "
            "re-armed wait; timeout retries require a fresh strict gate, while "
            "a wait error stops immediately without authorising another Enter "
            "(earlier timeout retries remain recorded). tmux keeps the "
            "legacy marker retry. Herdr queue-enter accepts only finite values "
            "up to 3600 seconds; tmux limits are unchanged. `0` disables extra "
            "Enter. Ignored under --mode standard/pending."
        ),
    )
    parser_.add_argument(
        "--queue-enter-retry-interval", dest="queue_enter_retry_interval",
        type=float, default=QUEUE_ENTER_RETRY_INTERVAL_SECONDS,
        help=(
            "Minimum seconds between queue-enter Enter presses (default "
            f"{QUEUE_ENTER_RETRY_INTERVAL_SECONDS:g}). Herdr re-arms the causal "
            "wait and re-runs the live gate before every extra Enter; Herdr "
            "accepts only finite values up to 3600 seconds. tmux keeps the "
            "legacy marker probe loop. `0` disables extra Enter."
        ),
    )
    parser_.add_argument(
        "--no-target-activation", dest="no_target_activation", action="store_true",
        help=(
            "Disable standard_target_admission activation (#12597 "
            "standard_target_admission activation): an "
            "inactive registered agent pane stays fail-closed instead of being "
            "activated via tmux `select-pane`. Ignored under --mode standard/pending."
        ),
    )
    parser_.add_argument(
        "--restore-previous-active", dest="restore_previous_active",
        action="store_true",
        help=(
            "After activating an admitted inactive split (#12597 "
            "standard_target_admission activation), "
            "re-select the previously-active pane. Pane selection only."
        ),
    )
    parser_.add_argument(
        "--record-format", dest="record_format",
        choices=sorted(RECORD_FORMATS), default=RECORD_FORMAT_BOTH,
        help=(
            "Format of the durable delivery-record emitted alongside the "
            "structured outcome. `both` (default) / `text` / `json`."
        ),
    )
    parser_.add_argument(
        "--record-command", dest="record_command",
        help=(
            "Optional literal command string included in the delivery record "
            "under `- Command:` for audit replay."
        ),
    )


def configure_ticketless_callback_parser(parser_: argparse.ArgumentParser) -> None:
    """Configure `handoff ticketless-callback` (#12703 ticketless no-anchor callback transport)."""
    _add_ticketless_delivery_options(parser_)
    parser_.add_argument(
        "--classification", required=True, choices=list(CLASSIFICATIONS),
        help=(
            "Ticketless consultation result class: `consultation_result` / "
            "`no_dispatch` / `blocked` / `anchor_required`."
        ),
    )
    parser_.add_argument(
        "--dispatch-decision", dest="dispatch_decision", required=True,
        choices=list(TICKETLESS_DISPATCH_DECISIONS),
        help=(
            "Hands-off dispatch decision. Only the no-anchor-safe decisions are "
            "offered: `no_dispatch`, `hand_back_to_caller`, "
            "`anchor_required_before_worker_dispatch`. An actual worker dispatch "
            "requires a real Redmine anchor via `handoff send`."
        ),
    )
    parser_.add_argument(
        "--workflow-next-owner", dest="workflow_next_owner", required=True,
        choices=list(NEXT_ACTION_OWNERS),
        help=(
            "Owner of the WORKFLOW next step (`caller` / `gateway` / `worker` / "
            "`operator`), distinct from the transport-layer next_action_owner."
        ),
    )
    parser_.add_argument(
        "--callback-reason", dest="callback_reason", required=True,
        choices=list(CALLBACK_REASONS),
        help="Fixed reason token for the callback.",
    )
    parser_.add_argument(
        "--read-contract", dest="read_contract", required=True,
        choices=list(READ_CONTRACT_TOKENS),
        help=(
            "Which workflow-contract set governed this result "
            "(`grandparent_coordinator` / `project_gateway` / `coordinator`); "
            "resolvable via the workflow contract refs."
        ),
    )
    parser_.add_argument(
        "--forward-action-id", dest="forward_action_id", default="",
        help=(
            "Echo the opaque forward generation id (Redmine #13583) the consultation / work-intake "
            "payload carried, so a positively-delivered callback completes the exact forward "
            "generation. Omit for a plain (non-forward) ticketless callback."
        ),
    )
