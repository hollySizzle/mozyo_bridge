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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    cmd_sublane_list,
    cmd_sublane_retire,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_CHOICES,
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


def register_lifecycle(sub) -> None:
    """Register the `init` / `doctor` / `sublane` lifecycle commands onto ``sub``."""
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

    # --- lifecycle MVP (Redmine #12955): create / start, list / status, retire ---

    def _add_lifecycle_json(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit structured JSON output instead of human-readable text",
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
    sublane_create.add_argument(
        "--branch", required=True, help="Branch name for the lane worktree"
    )
    sublane_create.add_argument(
        "--worktree", required=True, help="Worktree path for the lane"
    )
    sublane_create.add_argument(
        "--journal", default=None, help="Durable-anchor journal id for the dispatch step"
    )
    sublane_create.add_argument(
        "--upstream-coordinator",
        dest="upstream_coordinator",
        default=None,
        help="Coordinator pane the gateway calls back to",
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
    add_repo_option(sublane_create)
    _add_lifecycle_json(sublane_create)
    sublane_create.set_defaults(func=cmd_sublane_start)

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

    sublane_retire = sublane_sub.add_parser(
        "retire",
        help=(
            "Fail-closed retire preflight: evaluate the retire decision from git "
            "probes + durable-record invariants and emit the verdict + journal + "
            "retirement runbook. Does NOT actuate pane kill / worktree remove / "
            "branch delete (gated); never deletes remote branches. Exits non-zero "
            "when retirement is blocked."
        ),
    )
    sublane_retire.add_argument("--issue", required=True, help="Redmine issue id")
    sublane_retire.add_argument(
        "--lane-label",
        dest="lane_label",
        required=True,
        help="Lane label to retire (e.g. issue_<id>_<slug>)",
    )
    sublane_retire.add_argument(
        "--worktree", default=None, help="Worktree path to include in the runbook"
    )
    sublane_retire.add_argument(
        "--branch", default=None, help="Local branch to include in the runbook"
    )
    sublane_retire.add_argument(
        "--integration-branch",
        dest="integration_branch",
        default=None,
        help="Integration branch name (recorded in the durable journal)",
    )
    # Durable-record invariants the operator asserts (each defaults to unsatisfied
    # so an omitted flag fails closed).
    sublane_retire.add_argument(
        "--issue-closed",
        dest="issue_closed",
        action="store_true",
        help="The lane's Redmine issue is durably closed.",
    )
    sublane_retire.add_argument(
        "--owner-approved",
        dest="owner_approved",
        action="store_true",
        help="The owner close-approval journal exists.",
    )
    sublane_retire.add_argument(
        "--callbacks-drained",
        dest="callbacks_drained",
        action="store_true",
        help="No outstanding coordinator callback is owed.",
    )
    sublane_retire.add_argument(
        "--verified",
        dest="verified",
        action="store_true",
        help="The lane's verification (tests / checks) passed.",
    )
    sublane_retire.add_argument(
        "--durable-record",
        dest="durable_record",
        action="store_true",
        help="The durable retire record / anchor is present.",
    )
    sublane_retire.add_argument(
        "--target-identity-known",
        dest="target_identity_known",
        action="store_true",
        help="The lane / worktree / pane target is positively resolved.",
    )
    add_repo_option(sublane_retire)
    _add_lifecycle_json(sublane_retire)
    sublane_retire.set_defaults(func=cmd_sublane_retire)
