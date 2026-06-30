"""CLI surface for the single standard `workflow step` command (Redmine #12755).

`mozyo-bridge workflow step` is the one standard command an AI / operator runs to
advance one safe workflow step. It reads the current lane identity, resolves the
next safe action from the pure :func:`resolve_workflow_step` state machine, and
either dispatches the named internal primitive (``project-gateway consult`` /
``child-intake``) for an executable forward leg, or fails closed with a structured
``state`` / ``next_action`` / ``execution`` / ``reason`` / ``next_owner`` and the
next responsible owner.

The as-is primitives (``project-gateway consult`` / ``child-intake`` /
``handoff send`` / ``handoff ticketless-callback`` / ``handoff q-enter`` /
``delegate-*`` / debug ``%pane`` / ``message`` / ``type`` / ``keys``) stay
available as **internal / compatibility / debug** surfaces; this command is the
normal user-facing entrypoint that dispatches to them. ``%pane``, ``q-enter``,
``queue-enter``, ``--mode``, and raw pane mutation are not part of this surface
(design ``vibes/docs/logics/workflow-step-command-design.md``).

Execution model:

- ``workflow step`` (default) resolves the step and, for an executable forward
  leg, dispatches the internal primitive as if it had been typed — the AI never
  selects the command family, rail, pane, or role transition.
- ``workflow step --dry-run`` resolves and reports what *would* be done without
  mutating any pane / Redmine state (``execution=dry_run``).
- ``workflow step --json`` emits exactly one structured outcome envelope.

The standard surface is argument-free. ``--issue`` / ``--journal`` are the
*already-determined* Redmine anchor a child coordinator passes for the anchored
worker-dispatch leg (``workflow step`` never selects or creates an issue itself);
omit them and the child lane fails closed ``anchor_required``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json as _json

from mozyo_bridge.application.commands import (
    _agents_target_candidates,
    current_pane,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_admission import (
    cmd_workflow_admission,
    register_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_fill import (
    cmd_workflow_fill_decision,
    register_fill_decision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_resume import (
    cmd_workflow_resume,
    register_resume,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_runtime import (
    cmd_workflow_runtime,
    register_runtime,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_DRY_RUN,
    EXECUTION_EXECUTED,
    PRIMITIVE_CHILD_INTAKE,
    PRIMITIVE_CONSULT,
    PRIMITIVE_HANDOFF_SEND,
    PRIMITIVE_TICKETLESS_CALLBACK,
    PendingCallback,
    WorkflowAnchor,
    WorkflowStepOutcome,
    callback_rail_fields,
    resolve_workflow_step,
)


def _discover_candidates() -> list:
    """All classified target candidates across every session (no pre-filter).

    Mirrors ``cli_project_gateway._discover_candidates``: discovery is unfiltered
    so the state machine's resolvers apply the role / repo / project / session
    predicates themselves and their near-miss reasons stay visible. Patched in tests.
    """
    return _agents_target_candidates(argparse.Namespace(agent=None, session=None))


def _anchor_from_args(args: argparse.Namespace) -> WorkflowAnchor | None:
    """Build the already-determined Redmine anchor from optional flags, or None.

    ``--issue`` (and optional ``--journal``) is the anchor a child coordinator has
    already decided out of band; it is NOT ``workflow step`` selecting an issue.
    Absent, the child / grandchild lanes fail closed (``anchor_required`` /
    ``worker_runs_without_anchor``).
    """
    issue = (getattr(args, "issue", None) or "").strip()
    if not issue:
        return None
    return WorkflowAnchor(issue=issue, journal=(getattr(args, "journal", None) or "").strip())


def _pending_callback_from_args(args: argparse.Namespace) -> PendingCallback | None:
    """Build the already-determined pending callback from the optional flag, or None.

    ``--callback <classification>`` is the lane's *already-determined* consultation /
    work-intake result class (``consultation_result`` / ``no_dispatch`` / ``blocked``
    / ``anchor_required``); it is NOT ``workflow step`` deciding a domain/design
    answer. Absent, the lane forwards a new step instead of returning a callback. The
    caller lane to return to is derived from the current lane role by the state
    machine, so no role flag is needed here.
    """
    classification = (getattr(args, "callback", None) or "").strip()
    if not classification:
        return None
    return PendingCallback(classification=classification)


def _print_outcome_text(outcome: WorkflowStepOutcome) -> None:
    print(f"state: {outcome.state}")
    print(f"execution: {outcome.execution}")
    print(f"reason: {outcome.reason}")
    print(f"next_owner: {outcome.next_owner}")
    print(f"primitive: {outcome.primitive}")
    print(f"durable_anchor: {outcome.durable_anchor}")
    print(
        "lane: "
        f"caller_role={outcome.caller_role or '<unresolved>'} "
        f"self_pane={outcome.self_pane or '<none>'} "
        f"repo_root={outcome.repo_root or '<none>'} "
        f"project_scope={outcome.project_scope or '<none>'}"
    )
    if outcome.detail:
        print(f"detail: {outcome.detail}")
    print(f"next_action: {outcome.next_action}")


def _primitive_argv(
    outcome: WorkflowStepOutcome, args: argparse.Namespace, *, session: str | None
) -> list[str]:
    """Build the internal-primitive argv for an executable leg.

    The AI never types these — ``workflow step`` composes the resolved primitive
    invocation from the state machine's outcome. The forward legs use the semantic
    ``--target-repo`` + ``--target-project`` route (pane resolved by the primitive,
    never typed); the parent leg adds the ``--from-pane`` same-lane self-fence; the
    anchored worker dispatch carries the already-available Redmine anchor + the
    resolved worker pane; the determined callback derives the no-anchor rail's
    structured fields from the classification (:func:`callback_rail_fields`).
    """
    if outcome.primitive == PRIMITIVE_CONSULT:
        argv = [
            "project-gateway", "consult",
            "--to", "codex",
            "--target-repo", outcome.repo_root,
            "--target-project", outcome.project_scope,
        ]
        if session:
            argv += ["--gateway-session", session]
        return argv
    if outcome.primitive == PRIMITIVE_CHILD_INTAKE:
        argv = [
            "project-gateway", "child-intake",
            "--to", "codex",
            "--target-repo", outcome.repo_root,
            "--target-project", outcome.project_scope,
            "--from-pane", outcome.self_pane,
        ]
        if session:
            argv += ["--gateway-session", session]
        return argv
    if outcome.primitive == PRIMITIVE_HANDOFF_SEND:
        # Anchored child -> grandchild worker dispatch. The anchor came from the
        # caller (already-determined; --issue/--journal), the worker pane was
        # resolved semantically by the state machine.
        issue = (getattr(args, "issue", None) or "").strip()
        journal = (getattr(args, "journal", None) or "").strip()
        argv = [
            "handoff", "send",
            "--to", "claude",
            "--target", outcome.target_pane,
            "--target-repo", outcome.repo_root,
            "--source", "redmine",
            "--issue", issue,
            "--kind", "implementation_request",
        ]
        if journal:
            argv += ["--journal", journal]
        return argv
    if outcome.primitive == PRIMITIVE_TICKETLESS_CALLBACK:
        # Determined no-anchor callback back to the caller lane (a Codex coordinator).
        # The caller pane was resolved semantically by the state machine and is passed
        # as an explicit --target so delivery never falls back to an implicit
        # same-session `codex` label (Redmine #12755 review j#67585).
        fields = callback_rail_fields(outcome.callback_classification)
        argv = [
            "handoff", "ticketless-callback",
            "--to", "codex",
            "--target", outcome.target_pane,
            "--target-repo", outcome.repo_root,
            "--classification", fields["classification"],
            "--dispatch-decision", fields["dispatch_decision"],
            "--workflow-next-owner", fields["workflow_next_owner"],
            "--callback-reason", fields["callback_reason"],
            "--read-contract", outcome.callback_to_role,
        ]
        return argv
    raise AssertionError(  # pragma: no cover - guarded by outcome.executable
        f"non-executable primitive {outcome.primitive!r}"
    )


def _execute_primitive(
    outcome: WorkflowStepOutcome, args: argparse.Namespace, *, session: str | None
) -> tuple[int, str]:
    """Dispatch the internal primitive as if typed; return (rc, captured_stdout).

    Reuses the real top-level parser so the primitive runs with its exact defaults
    and gating (``orchestrate_handoff`` repo/project re-verification still applies).
    Stdout is captured so the ``workflow step`` envelope stays the single structured
    output; the captured primitive text rides in the envelope / is echoed in text
    mode.
    """
    # Lazy import to avoid an import cycle (cli -> cli_modules -> this module).
    from mozyo_bridge.application.cli import build_parser

    parser = build_parser()
    primitive_args = parser.parse_args(_primitive_argv(outcome, args, session=session))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = primitive_args.func(primitive_args)
    return int(rc or 0), buf.getvalue()


def cmd_workflow_step(args: argparse.Namespace) -> int:
    """Resolve and advance one safe workflow step (Redmine #12755 standard entrypoint).

    Reads the current lane identity (``current_pane`` + the discovered inventory),
    resolves the next safe action with the pure state machine, then:

    - ``--dry-run`` -> report the resolved outcome (``execution=dry_run``), no mutation;
    - executable leg (consultation / work-intake forward, determined callback, or
      anchored worker dispatch) -> dispatch the internal primitive and report executed;
    - otherwise (blocked / grandchild no-op) -> report the structured outcome and
      the next responsible owner.

    Returns 0 for a forward step (executed / ready / dry_run / no_op) and 1 for a
    fail-closed blocked outcome.
    """
    require_tmux()
    self_pane = current_pane()
    session = getattr(args, "session", None)
    as_json = getattr(args, "as_json", False)
    dry_run = getattr(args, "dry_run", False)

    outcome = resolve_workflow_step(
        _discover_candidates(),
        self_pane=self_pane,
        anchor=_anchor_from_args(args),
        pending_callback=_pending_callback_from_args(args),
        session=session,
    )

    # Dry-run, or a non-executable outcome (blocked / grandchild Redmine-work no-op):
    # report the resolved outcome, mutate nothing.
    if dry_run or not outcome.executable:
        reported = outcome
        if dry_run and outcome.executable:
            # Reflect that the executable leg was not actually run.
            reported = dataclasses.replace(outcome, execution=EXECUTION_DRY_RUN)
        if as_json:
            print(_json.dumps(reported.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_outcome_text(reported)
        return 0 if reported.ok else 1

    # Executable leg: dispatch the internal primitive.
    rc, primitive_out = _execute_primitive(outcome, args, session=session)
    executed = dataclasses.replace(outcome, execution=EXECUTION_EXECUTED)
    if as_json:
        payload = executed.as_payload()
        payload["primitive_rc"] = rc
        payload["primitive_output"] = primitive_out
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_outcome_text(executed)
        print(f"primitive_rc: {rc}")
        if primitive_out.strip():
            print("--- primitive output ---")
            print(primitive_out, end="" if primitive_out.endswith("\n") else "\n")
    # Surface the primitive's own rc: the step resolved an executable leg, but the
    # delivery's success/fail-closed result is the primitive's (e.g. a gateway that
    # vanished between resolution and delivery), so the caller sees the real outcome.
    return rc


def register(sub) -> None:
    """Register ``workflow`` (``step`` / ``fill-decision`` / ``admission`` / ``runtime``).

    ``workflow step`` (Redmine #12755) advances one safe workflow step;
    ``workflow fill-decision`` (Redmine #12855) reports the advisory Post-Dispatch
    Fill Loop decision for an already-classified lane set; ``workflow admission``
    (Redmine #12856) is the Redmine-aware companion that classifies each lane from its
    durable-record facts first; ``workflow runtime`` (Redmine #12857) is the stateful
    slice that replays an ordered durable event log (with duplicate suppression) into
    current lane state and the overall next action; ``workflow resume`` (Redmine #12671)
    reads the *persisted* mozyo-DB runtime state ``workflow runtime --persist`` wrote and
    reports the current state plus the enriched ``workflow.next_action`` (route_identity /
    anchor / risk_level / requires_confirmation / blocked_reason). The fill-decision /
    admission / runtime / resume subcommands are registered from their sibling modules so
    this file stays focused on the step state machine.
    """
    workflow = sub.add_parser(
        "workflow",
        help=(
            "Single standard agent/operator workflow entrypoint (Redmine #12755). "
            "`workflow step` advances one safe workflow step: it reads the current "
            "lane identity + durable gate + route identity and either executes the "
            "next safe routing/transport action or fails closed with the next owner "
            "and reason. `workflow fill-decision` reports the advisory Post-Dispatch "
            "Fill Loop decision (Redmine #12855); `workflow admission` classifies each "
            "lane from its durable-record facts first, then reports the same advisory "
            "admission/fill decision (Redmine #12856). The standard surface hides %%pane / "
            "q-enter / queue-enter / --mode; the existing project-gateway / handoff "
            "primitives stay as internal / compatibility / debug surfaces. See "
            "vibes/docs/logics/workflow-step-command-design.md."
        ),
    )
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    register_fill_decision(workflow_sub)
    register_admission(workflow_sub)
    register_runtime(workflow_sub)
    register_resume(workflow_sub)

    step = workflow_sub.add_parser(
        "step",
        description=(
            "Advance exactly one safe workflow step from the current lane. Infers the "
            "current lane role (grandparent / parent / child / grandchild) from "
            "registered lane/workspace metadata — never from caller guesswork — then "
            "resolves the one-step-down transition: grandparent forwards a ticketless "
            "consultation to the project gateway; parent forwards a ticketless "
            "work-intake to the child coordinator; child detects the Redmine-anchor "
            "requirement for worker dispatch (fail-closed when the anchor is "
            "undecided); a pending callback returns via the no-anchor callback rail. "
            "Fails closed (structured next owner) on ambiguity, missing/same-lane "
            "route, unsafe provider binding, or anchor-required worker dispatch."
        ),
        help=(
            "Advance one safe workflow step from the current lane (the normal "
            "AI/operator action). Resolves the next safe routing/transport action "
            "from lane identity + durable gate + route identity, or fails closed with "
            "the next owner and reason. Hides %%pane / q-enter / queue-enter / --mode; "
            "dispatches the existing project-gateway / handoff primitives internally."
        ),
    )
    # The standard surface is exactly `step` / `--dry-run` / `--json` (the design /
    # issue conceptual surface). The remaining knobs are internal/debug escapes —
    # route disambiguation (--session), the child's already-determined worker-dispatch
    # anchor (--issue/--journal), and a lane's already-determined callback class
    # (--callback). They stay functional but are hidden from `--help` (argparse.SUPPRESS)
    # so the normal AI/operator flow is not handed pane/rail/anchor/role decisions
    # (Redmine #12755 review j#67579 finding 3).
    step.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Resolve and report what would be done without mutating any pane / "
            "Redmine state (execution=dry_run)."
        ),
    )
    step.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured WorkflowStepOutcome envelope as JSON.",
    )
    step.add_argument(
        "--session",
        default=None,
        help=argparse.SUPPRESS,
    )
    step.add_argument(
        "--issue",
        default=None,
        help=argparse.SUPPRESS,
    )
    step.add_argument(
        "--journal",
        default=None,
        help=argparse.SUPPRESS,
    )
    step.add_argument(
        "--callback",
        default=None,
        help=argparse.SUPPRESS,
    )
    step.set_defaults(func=cmd_workflow_step)


__all__ = (
    "cmd_workflow_step",
    "cmd_workflow_fill_decision",
    "cmd_workflow_admission",
    "cmd_workflow_runtime",
    "cmd_workflow_resume",
    "register",
)
