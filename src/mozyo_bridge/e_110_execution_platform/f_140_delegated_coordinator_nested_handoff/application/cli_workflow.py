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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_DRY_RUN,
    EXECUTION_EXECUTED,
    PRIMITIVE_CHILD_INTAKE,
    PRIMITIVE_CONSULT,
    WorkflowAnchor,
    WorkflowStepOutcome,
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


def _primitive_argv(outcome: WorkflowStepOutcome, *, session: str | None) -> list[str]:
    """Build the internal-primitive argv for an executable forward leg.

    The AI never types these — ``workflow step`` composes the resolved primitive
    invocation from the state machine's outcome (semantic ``--target-repo`` +
    ``--target-project``; the pane is resolved by the primitive, never typed). The
    parent leg adds the ``--from-pane`` same-lane self-fence.
    """
    if outcome.primitive == PRIMITIVE_CONSULT:
        argv = [
            "project-gateway",
            "consult",
            "--to",
            "codex",
            "--target-repo",
            outcome.repo_root,
            "--target-project",
            outcome.project_scope,
        ]
    elif outcome.primitive == PRIMITIVE_CHILD_INTAKE:
        argv = [
            "project-gateway",
            "child-intake",
            "--to",
            "codex",
            "--target-repo",
            outcome.repo_root,
            "--target-project",
            outcome.project_scope,
            "--from-pane",
            outcome.self_pane,
        ]
    else:  # pragma: no cover - guarded by outcome.executable before call
        raise AssertionError(f"non-executable primitive {outcome.primitive!r}")
    if session:
        argv += ["--gateway-session", session]
    return argv


def _execute_primitive(outcome: WorkflowStepOutcome, *, session: str | None) -> tuple[int, str]:
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
    args = parser.parse_args(_primitive_argv(outcome, session=session))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


def cmd_workflow_step(args: argparse.Namespace) -> int:
    """Resolve and advance one safe workflow step (Redmine #12755 standard entrypoint).

    Reads the current lane identity (``current_pane`` + the discovered inventory),
    resolves the next safe action with the pure state machine, then:

    - ``--dry-run`` -> report the resolved outcome (``execution=dry_run``), no mutation;
    - executable forward leg -> dispatch the internal primitive and report executed;
    - otherwise (blocked / anchor-gated / grandchild no-op) -> report the structured
      outcome and the next responsible owner.

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
        session=session,
    )

    # Dry-run, or a non-executable outcome (blocked / anchored worker dispatch /
    # grandchild Redmine-work no-op): report the resolved outcome, mutate nothing.
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

    # Executable forward leg: dispatch the internal primitive.
    rc, primitive_out = _execute_primitive(outcome, session=session)
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
    """Register the ``workflow`` family (``workflow step``) onto ``sub`` (#12755)."""
    workflow = sub.add_parser(
        "workflow",
        help=(
            "Single standard agent/operator workflow entrypoint (Redmine #12755). "
            "`workflow step` advances one safe workflow step: it reads the current "
            "lane identity + durable gate + route identity and either executes the "
            "next safe routing/transport action or fails closed with the next owner "
            "and reason. The standard surface hides %%pane / q-enter / queue-enter / "
            "--mode; the existing project-gateway / handoff primitives stay as "
            "internal / compatibility / debug surfaces. See "
            "vibes/docs/logics/workflow-step-command-design.md."
        ),
    )
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)

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
        help=(
            "Optional session or cockpit group to narrow route resolution to one "
            "candidate (debug/disambiguation). Omit to resolve across separate "
            "windows/sessions (the normal path)."
        ),
    )
    step.add_argument(
        "--issue",
        default=None,
        help=(
            "The already-determined Redmine issue id for the anchored worker-dispatch "
            "leg (the child coordinator's decision, not selected by `workflow step`). "
            "Omit on the standard surface; without it the child lane fails closed "
            "anchor_required."
        ),
    )
    step.add_argument(
        "--journal",
        default=None,
        help=(
            "Optional Redmine journal id paired with --issue for the already-"
            "determined worker-dispatch anchor."
        ),
    )
    step.set_defaults(func=cmd_workflow_step)


__all__ = ("cmd_workflow_step", "register")
