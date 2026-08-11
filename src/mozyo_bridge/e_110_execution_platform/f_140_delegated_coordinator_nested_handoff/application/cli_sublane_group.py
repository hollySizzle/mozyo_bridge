"""``sublane`` CLI parser group (Redmine #14654).

Feature-local parser registration, following the convention the other bounded
contexts already use (``cli_agents`` / ``cli_handoff`` / ``cli_release`` /
``cli_sublane_retire``): a command group's parser lives with the feature that owns
it, not in the shared ``cli_core`` assembly site. ``cli_core`` composes it by
calling :func:`register_sublane_group` from ``register_lifecycle``.

Moved here rather than allowlisted (Redmine #14654): merging the #13249 herdr CLI
registration into ``main-next`` composed two independently-green branches into a
1007-line ``cli_core``, tripping the module-health ``new_oversized`` gate. The gate's
remedy is to reduce, and this group's home is the delegated-coordinator feature that
owns every command in it. Pure relocation — the parsers, their flags, help text,
defaults, ``func`` bindings and registration order are moved verbatim.

The two shared option helpers (``--repo`` and the lifecycle ``--json``) stay owned by
``cli_core`` — every family shares them — and are injected, so this module adds no
import back into the CLI core.
"""
from __future__ import annotations

import argparse
from typing import Callable

from mozyo_bridge.core.state.lane_kind import LANE_KINDS
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (
    cmd_sublane_callback_recovery,
    cmd_sublane_readiness,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (
    cmd_sublane_start,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (
    cmd_sublane_dispatch_worker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_sublane_retire import (
    register_sublane_retire,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    cmd_sublane_list,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_supersede import (
    cmd_sublane_supersede,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_cli import (  # noqa: E501
    register_sublane_hibernate_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_project_gateway_declare import (  # noqa: E501
    register_sublane_declare_project_gateway_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (
    register_sublane_resume_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (
    register_sublane_quarantine_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine_inspect import (  # noqa: E501
    register_sublane_quarantine_inspect_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_cli import (  # noqa: E501
    register_sublane_recover_stale_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery_cli import register_sublane_recover_gateway_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_cli import register_sublane_refresh_worker_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery_cli import (  # noqa: E501
    register_sublane_recover_pair_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pin_repair import register_sublane_repair_pins_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worktree_binding_repair import register_sublane_repair_worktree_binding_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import register_sublane_converge_bound_pair_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import register_sublane_prepare_bound_pair_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_reboot_audit import register_sublane_reboot_audit_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_residue_close import register_sublane_close_residue_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_cli import register_sublane_recover_restored_pair_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_CHOICES,
)


def _add_dispatch_admission_flags(parser: argparse.ArgumentParser) -> None:
    """Add the #13290 dispatch-admission gate flags to a live dispatch subparser.

    When any of these are supplied the ``--execute`` dispatch consults the single
    #12855 fill-decision authority and fails closed on a concrete stop unless an
    explicit ``--override-fill-stop REASON`` is given (recorded to the durable
    anchor). When none are supplied the gate is not armed and the dispatch proceeds
    unchanged. The advisory ``workflow fill-decision`` command is untouched.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_fill import (  # noqa: E501
        _parse_lane,
    )

    parser.add_argument(
        "--lane",
        action="append",
        type=_parse_lane,
        metavar="ISSUE:STATE",
        help="An active lane as ISSUE:STATE (repeatable) for the dispatch "
        "admission gate. Same vocabulary as `workflow fill-decision`. Supplying "
        "any admission flag arms the gate on --execute.",
    )
    parser.add_argument(
        "--ready-independent",
        dest="ready_independent",
        type=int,
        default=0,
        help="Count of ready implementation work items not overlapping an active "
        "lane (dispatch admission gate).",
    )
    parser.add_argument(
        "--ready-overlap",
        dest="ready_overlap",
        type=int,
        default=0,
        help="Count of ready work items overlapping an active lane (dispatch "
        "admission gate).",
    )
    parser.add_argument(
        "--capacity",
        dest="capacity",
        type=int,
        default=0,
        help="Remaining local soft-profile slots for another active sublane "
        "(dispatch admission gate).",
    )
    parser.add_argument(
        "--owner-or-release-gate",
        dest="owner_or_release_gate",
        action="store_true",
        help="An owner-decision / release / credential / destructive gate is "
        "active (forces a stop in the dispatch admission gate).",
    )
    parser.add_argument(
        "--override-fill-stop",
        dest="override_fill_stop",
        metavar="REASON",
        default=None,
        help="Proceed past a fill-decision stop with an explicit reason (recorded "
        "to the durable anchor). Required to dispatch when the gate resolves to a "
        "stop_* decision.",
    )


def register_sublane_group(
    sub,
    *,
    add_repo_option: Callable[[argparse.ArgumentParser], None],
    add_lifecycle_json: Callable[[argparse.ArgumentParser], None],
    snapshot=None,
) -> None:
    """Register the `sublane` command group onto the top-level subparsers ``sub``.

    ``snapshot`` is the composition root's single injected agent-provider runtime
    snapshot; it is carried onto the ``sublane create`` namespace so the launchability
    preflight resolves against the injected registry (Redmine #13569 R3-F1).
    """
    # `sublane` groups the read-only sublane startup / callback-stall
    # diagnostics (Redmine #12159) and the lifecycle MVP (create / list / retire,
    # Redmine #12955). The diagnostics subcommands are pure; the lifecycle
    # subcommands are discovery / planning / preflight only — they never actuate
    # `git worktree add/remove`, pane kill, or a merge (the destructive actuator is
    # gated behind a Design Consultation per worktree-lifecycle-boundary.md).
    sublane = sub.add_parser(
        "sublane",
        help=(
            "Sublane lifecycle (create / list / retire, Redmine #12955) plus "
            "startup readiness and callback-stall recovery diagnostics (#12159)"
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
        help="ASSERTED (not dispatch-anchored): a newer durable gate / Progress "
        "Log journal appeared after the dispatch. A hand-set boolean derived from "
        "a read you already made, so a gate landing after that read is invisible "
        "(Redmine #13889). Prefer --journals-json, which derives this from the "
        "durable record. Ignored when --journals-json is supplied.",
    )
    sublane_callback.add_argument(
        "--journals-json",
        dest="journals_json",
        help="Path to an `include=journals` issue snapshot (Redmine REST or MCP "
        "shape). Derives the progress verdict from the durable record: anchored on "
        "this lane+generation's exact dispatch marker and ordered by durable "
        "journal id, so a gate that landed seconds before the sweep is still seen. "
        "Use with --lane / --lane-generation.",
    )
    sublane_callback.add_argument(
        "--issue",
        dest="issue",
        default="",
        help="Issue id for the --journals-json snapshot (default: read from the payload).",
    )
    sublane_callback.add_argument(
        "--lane",
        dest="lane",
        default="",
        help="Lane id whose dispatch marker anchors the watermark (with --journals-json).",
    )
    sublane_callback.add_argument(
        "--lane-generation",
        dest="lane_generation",
        default="",
        help="Lane generation whose dispatch marker anchors the watermark (with --journals-json).",
    )
    sublane_callback.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="ACTUATE the recovery: read Redmine LIVE, re-read immediately before mutating, "
        "record the classification durably, then deliver AT MOST ONE notification per dispatch "
        "anchor pointing at that record. Requires --issue / --lane / --lane-generation / "
        "--target, live Redmine read credentials, and the note-write opt-in. Refuses "
        "--journals-json: a snapshot is frozen, so its re-read cannot see a gate that landed "
        "after the decision. Zero-sends on a landed gate, opaque post-anchor journals, a "
        "superseded round, an unattested workspace, an unwritable record, or a held / "
        "unavailable fence.",
    )
    sublane_callback.add_argument(
        "--target",
        dest="target",
        default="",
        help="Pane target the single recovery notification is delivered to (with --execute).",
    )
    sublane_callback.add_argument(
        "--workspace-id",
        dest="workspace_id",
        default="",
        help="Assert the workspace id that partitions the recovery fence key (with --execute). "
        "It is MATCHED against the durable workspace anchor, never used to override it: a "
        "mismatch fails closed rather than minting a fresh fence partition.",
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

    # --- lifecycle MVP (Redmine #12955): create / start, list / status, retire ---

    sublane_create = sublane_sub.add_parser(
        "create",
        aliases=["start"],
        help=(
            "Plan a sublane (default) or, with --execute, actuate it in one action "
            "(Redmine #12973): from issue / lane-label / branch / worktree, emit the "
            "fail-closed, replayable worktree + gateway pane + worker pane + dispatch "
            "steps; --execute creates/adopts the worktree + cockpit column and "
            "dispatches the implementation_request. Fails closed on missing identity "
            "or an unverified target. This is the standard sublane entrypoint; the "
            "raw `cockpit append` / `handoff send` primitives are debug surfaces."
        ),
    )
    sublane_create.add_argument("--issue", required=True, help="Redmine issue id")
    sublane_create.add_argument(
        "--lane-label",
        dest="lane_label",
        required=True,
        help="Lane label (e.g. issue_<id>_<slug>)",
    )
    # #13432: `--branch` / `--worktree` are the Git worktree identity. They are required in
    # a Git workspace (a missing field fails closed in the create/actuate use case with a
    # `missing_field:*` diagnostic), but OPTIONAL in a non-Git directory-scaffold workspace,
    # where the lane has no worktree and runs in the workspace root itself (#13392 論点1).
    # argparse cannot condition `required` on the runtime git probe, so the requirement is
    # enforced downstream (post-probe) instead of at parse time; omit them for a non-Git
    # lane and `--worktree` defaults to the workspace root.
    sublane_create.add_argument(
        "--branch",
        default="",
        help="Branch name for the lane worktree (required in a Git workspace; optional "
        "for a non-Git directory-scaffold lane, which has no worktree)",
    )
    sublane_create.add_argument(
        "--worktree",
        default="",
        help="Worktree path for the lane (required in a Git workspace; optional for a "
        "non-Git lane, where it defaults to the workspace root — the lane runtime root)",
    )
    # #13293: pin the base the lane worktree is cut from. Default (omit) keeps the
    # historical `git worktree add <path> -b <branch>` behavior (branch off the main
    # checkout's current HEAD); supply e.g. `origin/main` or a stacked-lane base commit
    # so a stale checkout can never silently cut the lane from an unintended base
    # (the j#72677 base trap). Only affects a create; a reuse/adopt ignores it.
    sublane_create.add_argument(
        "--base-ref",
        dest="base_ref",
        default=None,
        help="Explicit git base ref the lane worktree branches from (default: the main "
        "checkout HEAD, historical behavior). Use origin/main or a stacked-lane base "
        "commit to avoid cutting the lane from a stale checkout.",
    )
    sublane_create.add_argument(
        "--journal", default=None, help="Durable-anchor journal id for the dispatch step"
    )
    # Lane-role aware pane placement (Redmine #13647 Tranche 1b, disposition j#85650):
    # the creating coordinator ASSERTS where in the delegation tree the new lane sits
    # (親 / 子 / 孫) — a governance fact no probe can infer at create time and which the
    # display cache must never supply. It is stored generation-bound on the lane's
    # lifecycle authority row, so every later heal places the lane's panes by the same
    # kind, offline. Omitted (the default) records no kind: placement stays lane-class derived
    # — unchanged on the lane-KIND axis, but NOT the pre-#13646 placement, since Redmine
    # #14568 resolves an undeclared lane class to the product default (`split: down`).
    # `choices` is the canonical 3-token vocabulary — no parent/child/grandchild alias (P3).
    sublane_create.add_argument(
        "--lane-kind",
        dest="lane_kind",
        choices=sorted(LANE_KINDS),
        default="",
        help="Delegation-geometry kind of the lane being created (coordinator / "
        "delegated_coordinator / implementation), recorded as the durable heal "
        "authority for this lane's pane placement (default: unrecorded)",
    )
    sublane_create.add_argument(
        "--upstream-coordinator",
        dest="upstream_coordinator",
        default=None,
        help="Coordinator route the gateway calls back to (default: the stable "
        "`coordinator` route token, resolved workspace-scoped and fail-closed; "
        "pass an explicit pane/route to override)",
    )
    # Governed work-unit granularity (Redmine #13002): the standard dispatch unit
    # is one UserStory (1US=1作業単位). `leaf_issue` is the task-level-exception
    # unit; `epic` / `feature` are oversized and fail closed without an explicit
    # owner/operator decision anchor. Default: repo-local config
    # (`work_unit.granularity`), else `user_story`.
    sublane_create.add_argument(
        "--work-unit",
        dest="work_unit",
        choices=["epic", "feature", "user_story", "leaf_issue"],
        default=None,
        help="Granularity of the dispatched work unit (default: repo-local "
        "config `work_unit.granularity`, else user_story — the governed "
        "standard; leaf_issue requires either --leaf-standalone or "
        "--work-unit-decision-journal, see below; epic/feature require "
        "--work-unit-decision-journal).",
    )
    sublane_create.add_argument(
        "--work-unit-decision-journal",
        dest="work_unit_decision_journal",
        default=None,
        help="Durable journal id of the explicit owner/operator decision that "
        "authorizes an epic/feature-sized implementation dispatch, or a "
        "leaf_issue dispatch for an issue that HAS a parent UserStory. "
        "Required for --work-unit epic|feature, and for --work-unit "
        "leaf_issue unless --leaf-standalone is also passed; ignored "
        "otherwise. Redmine #14224: a task-level review need alone is never "
        "this anchor.",
    )
    # Redmine #14224: leaf-lane admission fence. `leaf_issue` is no longer an
    # unconditional exception (#14222's 8/9-active-lanes-leaf-sized problem) -- a
    # leaf dispatch for an issue WITH a parent UserStory now requires the same
    # explicit-decision-anchor mechanism epic/feature already use.
    # `--leaf-standalone` is the other escape valve: a genuinely standalone leaf
    # issue (no parent US) dispatches freely, no anchor needed.
    sublane_create.add_argument(
        "--leaf-standalone",
        dest="leaf_standalone",
        action="store_true",
        help="Declare that --work-unit leaf_issue names an issue with NO parent "
        "UserStory (a standalone issue) -- allows the leaf dispatch without "
        "--work-unit-decision-journal. Ignored for work units other than "
        "leaf_issue.",
    )
    # Live actuator (Redmine #12973): opt-in `--execute` performs the additive
    # worktree + cockpit column + gateway dispatch; without it the surface stays the
    # #12955 plan-only default (side-effect-free, back-compat).
    sublane_create.add_argument(
        "--execute",
        action="store_true",
        help="Actuate the plan (create/adopt the worktree + cockpit gateway/worker "
        "column and dispatch the implementation_request). Default: plan only, no side "
        "effects. Requires --journal for the dispatch anchor.",
    )
    sublane_create.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview the one-action actuation plan (worktree + cockpit column + "
        "dispatch) without any side effect. Wins over --execute when both are given.",
    )
    sublane_create.add_argument(
        "--no-dispatch",
        dest="no_dispatch",
        action="store_true",
        help="With --execute, create/adopt the lane but skip the gateway dispatch.",
    )
    sublane_create.add_argument(
        "--target-repo",
        dest="target_repo",
        default="auto",
        help="Target-repo resolution for the --execute dispatch (default: auto).",
    )
    # #13293: bounded pre-dispatch gateway readiness wait. Before the --execute
    # queue-enter dispatch, poll the freshly-launched gateway TUI until it is booted +
    # rendered (so the input lands on a live composer, not a still-booting one — the
    # j#72677 / 5-example dispatch-loss failure mode). The readiness observation itself
    # is advisory: an unconfirmed probe records gateway_ready=false and proceeds to the
    # governed send. That send still returns its own typed outcome — confirmed, not_sent,
    # or uncertain — and may fail closed. 0 disables the wait (back-compat immediate send).
    sublane_create.add_argument(
        "--gateway-ready-timeout",
        dest="gateway_ready_timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the gateway TUI to become ready before the --execute "
        "dispatch (default: 10.0; 0 disables the wait). This probe is advisory: an "
        "unconfirmed readiness records gateway_ready=false and continues to the governed "
        "send, which can still confirm submission or fail closed.",
    )
    _add_dispatch_admission_flags(sublane_create)
    add_repo_option(sublane_create)
    add_lifecycle_json(sublane_create)
    # Redmine #13569 R3-F1: carry the SAME composition-injected snapshot onto the parsed
    # namespace so `cmd_sublane_start`'s launchability preflight resolves against the
    # injected registry (not the built-in fallback). Without this `set_defaults`,
    # `args.snapshot` is absent in the real parser path and a rebound provider that exists
    # only in the injected snapshot is misjudged unknown -> zero-start (the R2-F2b bug,
    # unresolved in real composition because only the agents subparser was wired).
    sublane_create.set_defaults(func=cmd_sublane_start, snapshot=snapshot)

    # Worker-dispatch ack drive (Redmine #12988): the lane gateway forwards the
    # anchored implementation_request to its same-lane worker and records the
    # measured delivery ACK as `worker_dispatched` / worker_dispatch_confirmed=
    # true; any failure keeps the fail-closed `gateway_notified` semantics.
    sublane_dispatch_worker = sublane_sub.add_parser(
        "dispatch-worker",
        help=(
            "Drive the same-lane gateway -> worker implementation_request "
            "forward and record the measured worker-dispatch delivery ACK "
            "(Redmine #12988): only a causally confirmed submission yields "
            "`worker_dispatched` / worker_dispatch_confirmed=true; a failed or "
            "unresolved drive fails closed and the lane's recorded state stays "
            "`gateway_notified`. Default is a dry-run preview; --execute sends. "
            "Run from (or with --repo pointing at) the lane worktree."
        ),
    )
    sublane_dispatch_worker.add_argument(
        "--issue", required=True, help="Redmine issue id"
    )
    sublane_dispatch_worker.add_argument(
        "--lane-label",
        dest="lane_label",
        required=True,
        help="Lane label the resolved lane must match (e.g. issue_<id>_<slug>)",
    )
    sublane_dispatch_worker.add_argument(
        "--journal",
        default=None,
        help="Durable-anchor journal id the forwarded implementation_request "
        "carries. Required for --execute (worker dispatch is never unanchored).",
    )
    sublane_dispatch_worker.add_argument(
        "--execute",
        action="store_true",
        help="Drive the live same-lane worker send. Default: dry-run preview, "
        "no side effects.",
    )
    sublane_dispatch_worker.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview the resolved worker transfer without sending. Wins over "
        "--execute when both are given.",
    )
    sublane_dispatch_worker.add_argument(
        "--target-repo",
        dest="target_repo",
        default="auto",
        help="Target-repo identity gate for the worker send (default: auto).",
    )
    # #13301: bounded pre-forward worker readiness wait. Before the --execute
    # queue-enter forward, poll the freshly-launched same-lane worker TUI until it is
    # booted + rendered (so the anchored implementation_request lands on a live
    # composer, not a still-booting one — the worker-side analog of the #13293 gateway
    # dispatch-loss failure mode; 3/4 lanes in the 2026-07-06 second wave). Readiness is
    # an action-time gate: an unconfirmed worker records worker_ready=false and blocks
    # before injection (zero-send). 0 disables the wait (back-compat immediate forward).
    sublane_dispatch_worker.add_argument(
        "--worker-ready-timeout",
        dest="worker_ready_timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the same-lane worker TUI to become ready before the "
        "--execute forward (default: 10.0; 0 disables the wait). An unconfirmed readiness "
        "records worker_ready=false and blocks before injection (zero-send).",
    )
    # #13301: thread the explicit --allow-direct-worker gateway-route exception
    # (#12918) into the same-lane worker send so a drive from a pane whose lane Unit
    # differs from the worker's (e.g. a coordinator stall-drive) is admitted and
    # recorded distinctly as a gateway_route_exception instead of failing closed. The
    # same-lane gateway drive omits it (default), keeping the #12988 contract unchanged.
    sublane_dispatch_worker.add_argument(
        "--allow-direct-worker",
        dest="allow_direct_worker",
        action="store_true",
        help="Thread the explicit --allow-direct-worker gateway-route exception "
        "(Redmine #12918) into the worker send so a cross-lane drive (e.g. a "
        "coordinator stall-drive) is admitted and recorded distinctly as a "
        "gateway_route_exception instead of failing closed with gateway_route_blocked.",
    )
    _add_dispatch_admission_flags(sublane_dispatch_worker)
    add_repo_option(sublane_dispatch_worker)
    add_lifecycle_json(sublane_dispatch_worker)
    sublane_dispatch_worker.set_defaults(func=cmd_sublane_dispatch_worker)

    sublane_list = sublane_sub.add_parser(
        "list",
        aliases=["status"],
        help=(
            "Read-only: list live sublanes (issue / worktree / gateway pane / "
            "worker pane / branch / state / host window) from the tmux pane "
            "inventory, with machine-readable stale/retire hints (pane missing, "
            "window split, duplicate issue lane, unresolved worktree; with "
            "--integration-branch also branch-integrated). Advisory diagnosis "
            "only: never retires, kills, or routes."
        ),
    )
    sublane_list.add_argument(
        "--lane",
        default=None,
        help="Filter to a single lane by lane id, lane label, or issue id",
    )
    sublane_list.add_argument(
        "--integration-branch",
        dest="integration_branch",
        default=None,
        help=(
            "Opt-in read-only ancestry probe (git merge-base --is-ancestor): flag "
            "lanes whose branch is already reachable from this integration branch "
            "as branch_integrated retire candidates. Never guessed when omitted."
        ),
    )
    add_repo_option(sublane_list)
    add_lifecycle_json(sublane_list)
    sublane_list.set_defaults(func=cmd_sublane_list)

    # Redmine #13754: the retire parser lives with the feature that owns it
    # (the same convention cli_agents / cli_handoff / cli_release follow); the two
    # shared option helpers stay owned here and are injected.
    register_sublane_retire(
        sublane_sub,
        add_repo_option=add_repo_option,
        add_lifecycle_json=add_lifecycle_json,
    )

    sublane_supersede = sublane_sub.add_parser(
        "supersede",
        help=(
            "Redmine #13681: hand an issue's ownership from a superseded lane to its "
            "recovery successor, then release the superseded lane's managed processes "
            "(tombstone-free — never removes a worktree, deletes a branch, or closes "
            "the issue). Fail-closed preflight (both identities known, successor "
            "attested, same issue, original idle). Default is preflight only; "
            "--execute performs the handover. Exits non-zero when blocked."
        ),
    )
    sublane_supersede.add_argument(
        "--issue", required=True, help="Redmine issue id whose ownership moves"
    )
    sublane_supersede.add_argument(
        "--original-lane",
        dest="original_lane",
        required=True,
        help="Lane label being superseded (e.g. issue_<id>_<slug>)",
    )
    sublane_supersede.add_argument(
        "--recovery-lane",
        dest="recovery_lane",
        required=True,
        help="Recovery successor lane label taking ownership",
    )
    sublane_supersede.add_argument(
        "--journal",
        required=True,
        help="Redmine journal id that authorizes the handover (durable anchor)",
    )
    # Durable-record invariants the operator asserts the original lane is idle under
    # (each defaults to unsatisfied so an omitted flag fails closed).
    sublane_supersede.add_argument(
        "--callbacks-drained",
        dest="callbacks_drained",
        action="store_true",
        help="The original lane owes no outstanding coordinator callback.",
    )
    sublane_supersede.add_argument(
        "--no-pending-prompt",
        dest="no_pending_prompt",
        action="store_true",
        help="The original lane has no composer input pending.",
    )
    sublane_supersede.add_argument(
        "--not-working",
        dest="not_working",
        action="store_true",
        help="The original lane has no work in flight.",
    )
    sublane_supersede.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help=(
            "Perform the handover: CAS the ownership move (original active->superseded, "
            "recovery->active owner) and release the original's managed processes. "
            "Without it this is preflight only (no mutation)."
        ),
    )
    add_repo_option(sublane_supersede)
    add_lifecycle_json(sublane_supersede)
    sublane_supersede.set_defaults(func=cmd_sublane_supersede)

    register_sublane_hibernate_parser(sublane_sub)
    register_sublane_declare_project_gateway_parser(sublane_sub)
    register_sublane_resume_parser(sublane_sub)
    register_sublane_quarantine_parser(sublane_sub)
    register_sublane_quarantine_inspect_parser(sublane_sub)
    register_sublane_recover_stale_parser(sublane_sub)
    register_sublane_recover_gateway_parser(sublane_sub)
    # Redmine #14661: the WORKER mirror of recover-gateway — a LIVE turn-ended worker that
    # produced no durable progress. A separate admission from recover-stale (vanished worker),
    # which it deliberately does not loosen.
    register_sublane_refresh_worker_parser(sublane_sub)
    register_sublane_recover_pair_parser(sublane_sub)
    register_sublane_repair_pins_parser(sublane_sub)
    register_sublane_repair_worktree_binding_parser(sublane_sub)
    register_sublane_converge_bound_pair_parser(sublane_sub)
    register_sublane_prepare_bound_pair_parser(sublane_sub)
    # Redmine #14499: the post-reboot convergence pair — a read-only four-authority snapshot
    # and the lane-scoped shell-residue close it recommends.
    register_sublane_reboot_audit_parser(sublane_sub)
    register_sublane_close_residue_parser(sublane_sub)
    register_sublane_recover_restored_pair_parser(sublane_sub)
