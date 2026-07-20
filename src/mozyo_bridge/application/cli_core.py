"""CLI parser registration for the core (non-feature) command set.

Split out of ``application/cli.py`` (Redmine #12155) so the residual inline
``build_parser()`` blocks compose through the internal module registry like the
feature families (Redmine #12153 / #12154) already do. Behavior-preserving: the
block text is moved verbatim from ``build_parser()`` so help / choices /
defaults / dest / ``func`` bindings are unchanged, and the registrars are called
in the same order, so the top-level subcommand sequence is identical.

The core families are the hard command set — pane discovery / I/O / lifecycle /
diagnostics — that the registry marks ``core`` (mandatory, never config-disabled).
They are interleaved with the feature families in ``build_parser()``, so they are
registered as four ordered entry points rather than one block:

- :func:`register_top` — ``status`` / ``list``
- :func:`register_pane_io` — ``id`` / ``resolve`` / ``read`` / ``type``
- :func:`register_keys` — ``keys``
- :func:`register_lifecycle` — ``init`` / ``doctor`` (+ ``doctor instruction``) / ``sublane``
"""
from __future__ import annotations

import argparse

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    agent_provider_ids,
)
from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands import (
    cmd_doctor,
    cmd_doctor_instruction,
    cmd_id,
    cmd_init,
    cmd_keys,
    cmd_list,
    cmd_read,
    cmd_resolve,
    cmd_status,
    cmd_type,
)
from mozyo_bridge.application.doctor_runtime import cmd_doctor_runtime
from mozyo_bridge.application.instruction_doctor import (
    KNOWN_PROFILES,
    PROFILE_REDMINE_CODEX,
)
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (
    register_sublane_resume_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (
    register_sublane_quarantine_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_cli import (  # noqa: E501
    register_sublane_recover_stale_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E501
    register_sublane_recover_pair_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pin_repair import register_sublane_repair_pins_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import register_sublane_converge_bound_pair_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import register_sublane_prepare_bound_pair_parser  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_CHOICES,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_cli import (
    cmd_herdr_session_start,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (
    cmd_herdr_agent_attest,
)


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


def register_top(sub) -> None:
    """Register the `status` and `list` core commands onto ``sub``."""
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


def register_pane_io(sub) -> None:
    """Register the `id` / `resolve` / `read` / `type` pane I/O commands onto ``sub``."""
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


def register_keys(sub) -> None:
    """Register the `keys` core command onto ``sub``."""
    keys = sub.add_parser("keys")
    keys.add_argument("target")
    keys.add_argument("keys", nargs="+")
    keys.set_defaults(func=cmd_keys)


def register_lifecycle(sub, *, snapshot=None) -> None:
    """Register the `init` / `doctor` / `sublane` lifecycle commands onto ``sub``.

    ``snapshot`` (Redmine #13569 R1-F1) supplies the ``init`` / ``herdr session-start``
    ``--agent`` choice vocabulary from the composition root's single injected snapshot;
    ``None`` uses the built-in provider ids (byte-identical).
    """
    agent_choices = (
        sorted(snapshot.provider_ids) if snapshot is not None else sorted(agent_provider_ids())
    )
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
    init.add_argument("agent", choices=agent_choices)
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

    # `doctor runtime` is the runtime fingerprint (Redmine #12612): it proves
    # which executable surface is under test (source tree vs installed pipx /
    # site-packages) and fails when the active runtime and the repo-local source
    # report the same version but differ on gate-critical feature probes
    # (#12597 standard_target_admission / --no-target-activation). Read-only.
    doctor_runtime = doctor_sub.add_parser(
        "runtime",
        help=(
            "Read-only runtime fingerprint: classify the active executable "
            "surface (source vs installed), report version / executable / "
            "package path / git anchor, and probe gate-critical behavior so a "
            "stale install cannot pass a dogfood/smoke gate while reporting the "
            "same version as source. Does not install or hit the network."
        ),
    )
    _add_doctor_diagnostic_options(doctor_runtime)
    doctor_runtime.set_defaults(func=cmd_doctor_runtime)

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

    def _add_lifecycle_json(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit structured JSON output instead of human-readable text",
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
        "standard; leaf_issue only for the governed task-level exceptions; "
        "epic/feature require --work-unit-decision-journal).",
    )
    sublane_create.add_argument(
        "--work-unit-decision-journal",
        dest="work_unit_decision_journal",
        default=None,
        help="Durable journal id of the explicit owner/operator decision that "
        "authorizes an epic/feature-sized implementation dispatch. Required "
        "for --work-unit epic|feature; ignored otherwise.",
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
    # j#72677 / 5-example dispatch-loss failure mode). Never hard-blocks the queue-enter
    # rail: an unconfirmed readiness within the window degrades to gateway_ready=false
    # and dispatches anyway. 0 disables the wait (back-compat immediate dispatch).
    sublane_create.add_argument(
        "--gateway-ready-timeout",
        dest="gateway_ready_timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the gateway TUI to become ready before the --execute "
        "dispatch (default: 10.0; 0 disables the wait). Never hard-blocks — an "
        "unconfirmed readiness dispatches anyway and records gateway_ready=false.",
    )
    _add_dispatch_admission_flags(sublane_create)
    add_repo_option(sublane_create)
    _add_lifecycle_json(sublane_create)
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
            "(Redmine #12988): only a submit-complete send yields "
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
    # dispatch-loss failure mode; 3/4 lanes in the 2026-07-06 second wave). Never
    # hard-blocks: an unconfirmed readiness forwards anyway and records
    # worker_ready=false. 0 disables the wait (back-compat immediate forward).
    sublane_dispatch_worker.add_argument(
        "--worker-ready-timeout",
        dest="worker_ready_timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the same-lane worker TUI to become ready before the "
        "--execute forward (default: 10.0; 0 disables the wait). Never hard-blocks — an "
        "unconfirmed readiness forwards anyway and records worker_ready=false.",
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
    _add_lifecycle_json(sublane_dispatch_worker)
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
    _add_lifecycle_json(sublane_list)
    sublane_list.set_defaults(func=cmd_sublane_list)

    # Redmine #13754: the retire parser lives with the feature that owns it
    # (the same convention cli_agents / cli_handoff / cli_release follow); the two
    # shared option helpers stay owned here and are injected.
    register_sublane_retire(
        sublane_sub,
        add_repo_option=add_repo_option,
        add_lifecycle_json=_add_lifecycle_json,
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
    _add_lifecycle_json(sublane_supersede)
    sublane_supersede.set_defaults(func=cmd_sublane_supersede)

    register_sublane_hibernate_parser(sublane_sub)
    register_sublane_resume_parser(sublane_sub)
    register_sublane_quarantine_parser(sublane_sub)
    register_sublane_recover_stale_parser(sublane_sub)
    register_sublane_recover_pair_parser(sublane_sub)
    register_sublane_repair_pins_parser(sublane_sub)
    register_sublane_converge_bound_pair_parser(sublane_sub)
    register_sublane_prepare_bound_pair_parser(sublane_sub)
    # `herdr` groups the pure-herdr session helpers (Redmine #13261). `session-start`
    # is the opt-in write side: it mints durable herdr assigned names for the
    # workspace's `claude` / `codex` agents and injects their self-identity env so the
    # herdr-native target resolution has stable identities to resolve against. Not
    # coupled to the `terminal_transport.backend` flag; in pure-herdr operation both
    # are used together.
    herdr = sub.add_parser(
        "herdr",
        help=(
            "Pure-herdr session helpers (Redmine #13261): mint durable herdr "
            "assigned names for the workspace's agents (session-start)."
        ),
    )
    herdr_sub = herdr.add_subparsers(dest="herdr_command", required=True)
    # `attestation-store` (Redmine #13882) is the public maintenance rail for the
    # home-scoped self-attestation store. A feature-local parser module, per the
    # `cli_sublane_retire` precedent, so this near-ceiling module gains no flags.
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_attestation_store import (  # noqa: E501
        register_herdr_attestation_store_parser,
    )

    register_herdr_attestation_store_parser(herdr_sub, add_repo_option=add_repo_option)
    # Redmine #13892 / #13948: every herdr session recovery surface, in one call.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_recovery import (  # noqa: E501
        register_herdr_recovery_surfaces,
    )

    register_herdr_recovery_surfaces(herdr_sub, add_repo_option=add_repo_option)
    # Redmine #13249: the distribution surface — the supply-chain pin posture
    # (pin-posture) and the opt-in Claude/Codex session-hook installer
    # (integration-install). One registrar call, mirroring the recovery surfaces.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_distribution import (  # noqa: E501
        register_herdr_distribution_surfaces,
    )

    register_herdr_distribution_surfaces(herdr_sub, add_repo_option=add_repo_option)
    # Redmine #14065: the read-only composer-render measurement diagnostic (phase 1).
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_composer_render_cli import (  # noqa: E501
        register_herdr_composer_render_parser,
    )

    register_herdr_composer_render_parser(herdr_sub)
    herdr_session_start = herdr_sub.add_parser(
        "session-start",
        help=(
            "Prepare a pure-herdr session: register the workspace, launch (or adopt) "
            "the requested `claude` / `codex` agents as herdr-managed panes pinned to "
            "the repo root, mint their durable `mzb1_...` assigned names, and inject "
            "the self-identity env (MOZYO_WORKSPACE_ID / MOZYO_AGENT_ROLE / "
            "MOZYO_LANE_ID). Idempotent: an agent already carrying the slot's durable "
            "name is adopted, not re-launched. The herdr binary comes only from the "
            "trusted environment (MOZYO_HERDR_BINARY)."
        ),
    )
    herdr_session_start.add_argument(
        "--agent",
        dest="agent",
        action="append",
        choices=agent_choices,
        help="Provider agent to prepare (repeatable). Default: both claude and codex.",
    )
    herdr_session_start.add_argument(
        "--lane",
        dest="lane",
        default=None,
        help="Lane id for the minted identities (default: the workspace-default lane).",
    )
    herdr_session_start.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Plan only: report which slots would launch / adopt without any side "
        "effect — no launch, no rename, and no workspace registration / anchor write "
        "(Redmine #13595). An unregistered workspace fails closed with actionable "
        "guidance rather than being registered.",
    )
    add_repo_option(herdr_session_start)
    _add_lifecycle_json(herdr_session_start)
    herdr_session_start.set_defaults(func=cmd_herdr_session_start)

    # `agent-attest` is the managed-launch wrapper (Redmine #13637): the launch
    # execs the provider THROUGH this command so the agent's own process can
    # self-inspect its injected identity env before `exec`ing the provider, and
    # record a generation-bound startup self-attestation. It is not an operator
    # command — it is the wrapper the launch argv points at.
    # Redmine #13847: advertise the attestation-store schema/capability contract in this
    # subcommand's `--help` so the managed-launch preflight can verify a probed launcher's
    # store schema matches the source runtime's — not just that the subcommand exists. The
    # token is built from the store's schema constant so it can never drift from the store
    # it gates, and is whitespace-free so `--help` wrapping cannot split it. A launcher
    # predating this token (e.g. the v1 installed launcher) advertises none and fails the
    # preflight closed, before any process launch.
    # The #13882 companion token advertises the store shapes this build can WRITE, built
    # from the same store module's recognized-version set so it cannot drift either. The
    # native-schema token alone cannot separate a pre-#13882 build (writes v2 only) from a
    # v1-compatible one — both say `2` — yet only the latter is safe against a v1 shared
    # home, so the store join consults the SET.
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
        RECOGNIZED_SCHEMA_VERSIONS,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (
        build_attest_capability_contract_line,
        build_attest_capability_stores_line,
    )

    herdr_agent_attest = herdr_sub.add_parser(
        "agent-attest",
        help=(
            "Managed-launch internal wrapper (Redmine #13637): self-inspect this "
            "agent's injected identity env, record a generation-bound startup "
            "self-attestation, then exec the provider given after `--`."
        ),
        # RawDescriptionHelpFormatter so the capability contract token in the epilog is
        # emitted VERBATIM: argparse's default formatter reflows the epilog and would split
        # the token across lines (measured), making a capable launcher's probe read as
        # incapable. The token is on its own line, unwrapped, so a launcher's `--help` (the
        # #13847 preflight probe input) carries it intact.
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "capability contract (Redmine #13847):\n"
            + build_attest_capability_contract_line(
                HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
            )
            + "\nwritable attestation store shapes (Redmine #13882):\n"
            + build_attest_capability_stores_line(RECOGNIZED_SCHEMA_VERSIONS)
        ),
    )
    herdr_agent_attest.add_argument("--assigned-name", dest="assigned_name", default="")
    herdr_agent_attest.add_argument("--workspace-id", dest="workspace_id", default="")
    herdr_agent_attest.add_argument("--role", dest="role", default="")
    herdr_agent_attest.add_argument("--lane", dest="lane", default="")
    herdr_agent_attest.add_argument(
        "--replacement-action-id",
        dest="replacement_action_id",
        default="",
        help=(
            "Redmine #13806 tranche D: the replacement transaction action_id that launched "
            "this process (empty on a normal launch); recorded into the startup self-attestation "
            "so a recovery can verify the exact action bound its fresh worker."
        ),
    )
    herdr_agent_attest.add_argument(
        "provider_argv",
        nargs=argparse.REMAINDER,
        help="The provider command to exec, after a `--` separator.",
    )
    herdr_agent_attest.set_defaults(func=cmd_herdr_agent_attest)
