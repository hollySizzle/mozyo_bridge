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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_glance import (
    cmd_workflow_glance,
    register_glance,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_drain import (
    register_drain_queue,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_review_escalation import (
    register_review_escalation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_fences import (
    register_fence_operator_parsers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_dispatch_plan import (
    cmd_workflow_dispatch_plan,
    register_dispatch_plan,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_dispatch_ir import (
    cmd_workflow_dispatch_ir,
    register_dispatch_ir,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_lane_admission import (
    cmd_workflow_lane_admission,
    register_lane_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_resume import (
    cmd_workflow_resume,
    register_resume,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_runtime import (
    cmd_workflow_runtime,
    register_runtime,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_watch import (
    cmd_workflow_watch,
    register_watch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callbacks import (
    register_callbacks,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_supervisor import (
    register_supervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_DRY_RUN,
    EXECUTION_EXECUTED,
    EXECUTION_READY,
    PRIMITIVE_CHILD_INTAKE,
    PRIMITIVE_CONSULT,
    PRIMITIVE_HANDOFF_SEND,
    PRIMITIVE_HERDR_DISPATCH_WORKER,
    PRIMITIVE_OPERATOR_STARTUP_RESUME,
    PRIMITIVE_TICKETLESS_CALLBACK,
    PendingCallback,
    WorkflowAnchor,
    WorkflowStepOutcome,
    callback_rail_fields,
    resolve_workflow_step,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (
    STORE_ABSENT,
    STORE_PRESENT,
    STORE_UNAVAILABLE,
    reconcile_step_with_store,
)


def _discover_candidates() -> list:
    """All classified target candidates across every session (no pre-filter).

    Mirrors ``cli_project_gateway._discover_candidates``: discovery is unfiltered
    so the state machine's resolvers apply the role / repo / project / session
    predicates themselves and their near-miss reasons stay visible. Patched in tests.
    """
    return _agents_target_candidates(argparse.Namespace(agent=None, session=None))


def _load_store_action(
    args: argparse.Namespace, *, repo_root: str = ""
) -> tuple[object | None, str]:
    """Read the persisted runtime store's overall pending action, fail-open (#13291).

    Returns ``(WorkflowNextAction | None, store_status)`` where ``store_status`` is one of
    the :mod:`...domain.workflow_step_reconcile` ``STORE_*`` tokens. The read is
    non-mutating and degrades non-destructively: a missing store DB is
    :data:`STORE_ABSENT`; any read / schema / fold error is :data:`STORE_UNAVAILABLE`.
    Only when the store folds cleanly is the overall :func:`derive_workflow_next_action`
    result returned with :data:`STORE_PRESENT`. Patched in tests to keep the step CLI
    hermetic from the home store. ``--store-path`` (hidden) overrides the default home
    store.

    The store fold resolves the **same** repo-local role->provider binding
    ``workflow resume`` threads (Redmine #13291 review j#72693): the reconcile input must
    be folded identically to resume, otherwise a provider-rebind repo (#13157) folds the
    same store two different ways and step misclassifies a non-gating action as gating.
    ``repo_root`` is the current self lane's repo root (from the live outcome); an empty /
    config-less repo threads :meth:`RoleProviderBinding.default`, so an unconfigured repo
    is unchanged. A broken config fails open here (``STORE_UNAVAILABLE`` -> live outcome),
    keeping a live routing step working regardless of the runtime-store config.
    """
    # Lazy import: the reconcile read reuses the resume use case / store + binding source,
    # kept out of the step module's import surface until a step actually consults the store.
    from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStoreError
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_resume import (
        _store_from_args,
        resume_command_result,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
        load_workflow_binding,
    )

    try:
        store = _store_from_args(args)
        if not store.path.exists():
            return None, STORE_ABSENT
        binding, _warnings = load_workflow_binding(repo_root or None)
        result = resume_command_result(store, binding=binding)
        return result.next_action, STORE_PRESENT
    except WorkflowRuntimeStoreError:
        return None, STORE_UNAVAILABLE
    except Exception:  # noqa: BLE001 - fail-open: a store read never breaks a live step
        return None, STORE_UNAVAILABLE


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


def _anchor_issue_of(durable_anchor: str) -> str | None:
    """The Redmine issue id from a ``redmine:issue=<id>:journal=<id>`` pointer, or ``None``.

    Used to issue-correlate the store reconcile with a herdr live outcome's verified anchor
    (Redmine #13489 F3c). ``"none"`` / an empty / a non-Redmine pointer yields ``None`` (no
    correlation constraint), so a herdr lane with no verified anchor reconciles unchanged.
    """
    s = (durable_anchor or "").strip()
    if not s or s == "none" or not s.startswith("redmine:"):
        return None
    for field in s.split(":"):
        field = field.strip()
        if field.startswith("issue="):
            issue = field[len("issue="):].strip()
            return issue or None
    return None


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


def _is_herdr_dispatch_leg(outcome: WorkflowStepOutcome) -> bool:
    """True when the outcome is the executable increment-2 herdr worker-dispatch leg.

    This leg is deliberately NOT part of the generic ``WorkflowStepOutcome.executable`` set —
    it carries its own reserve+send+outcome idempotency fence and is driven by a dedicated CLI
    executor, never the generic ``_primitive_argv`` rail (Redmine #13489 increment 2).
    """
    return (
        outcome.primitive == PRIMITIVE_HERDR_DISPATCH_WORKER
        and outcome.execution == EXECUTION_READY
    )


def _execute_herdr_dispatch_leg(
    outcome: WorkflowStepOutcome, args: argparse.Namespace
) -> tuple[int, str]:
    """Run the fenced one-step herdr worker dispatch; return (rc, summary text).

    Delegates to :func:`...herdr_dispatch_cli.execute_herdr_dispatch`, which re-resolves the
    dispatch decision at action time and drives the idempotency fence around one send. ``rc`` is
    ``0`` for a delivered / never-send (skipped) outcome and ``1`` for a fence-unavailable /
    uncertain outcome the operator must reconcile.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_cli import (
        execute_herdr_dispatch,
    )

    result = execute_herdr_dispatch(args, outcome.durable_anchor)
    text = (
        f"dispatch_result: {result.result}\n"
        f"fence_state: {result.fence_state}\n"
        f"sent: {result.sent}\n"
        f"detail: {result.detail}"
    )
    return (0 if result.ok else 1), text


def _is_startup_resume_leg(outcome: WorkflowStepOutcome) -> bool:
    """True when the outcome is the executable startup-clear exactly-once resume leg (#13813).

    Like the herdr dispatch leg, this is NOT part of the generic ``executable`` set: it rides
    the resume leg's own ``DispatchOutboxFence`` reserve+send+outcome and a dedicated CLI
    executor, never the ``_primitive_argv`` rail.
    """
    return (
        outcome.primitive == PRIMITIVE_OPERATOR_STARTUP_RESUME
        and outcome.execution == EXECUTION_READY
    )


def _execute_startup_resume_leg(
    outcome: WorkflowStepOutcome, args: argparse.Namespace
) -> tuple[int, str]:
    """Run the fenced startup-clear exactly-once resume; return (rc, summary text).

    Delegates to :func:`...operator_startup_resume_leg.execute_startup_resume`, which re-reads
    the durable gate at action time, re-resolves the exact live target / generation, preflights
    the durable writer, and drives the exactly-once fence around one high-level send. ``rc`` is
    ``0`` for a delivered / never-send (skipped) outcome and ``1`` otherwise (uncertain /
    fence- or writer-unavailable / not-resumable — the operator reconciles).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (
        execute_startup_resume,
    )

    issue = _anchor_issue_of(outcome.durable_anchor) or ""
    result = execute_startup_resume(args, issue)
    text = (
        f"resume_result: {result.result}\n"
        f"fence_state: {result.fence_state}\n"
        f"sent: {result.sent}\n"
        f"record_failed: {result.record_failed}\n"
        f"needs_reconcile: {result.needs_reconcile}\n"
        f"detail: {result.detail}"
    )
    # An operator-reconcile-required outcome must surface a non-zero process status even when
    # the delivery itself was positive (review j#79366 F2): a `record_failed` durable append or
    # any `needs_reconcile` state (uncertain / fence row lost) is NOT a success for automation,
    # independent of the delivery receipt.
    rc = 0 if (result.ok and not result.needs_reconcile) else 1
    return rc, text


def _maybe_operator_startup_resume_outcome(
    args: argparse.Namespace, outcome: WorkflowStepOutcome
) -> "WorkflowStepOutcome | None":
    """A resume-primitive outcome iff a durable startup gate awaits resume (#13813), else None.

    Reads the latest durable operator startup gate for the step's issue at action time. It routes
    to the exactly-once resume leg (:data:`PRIMITIVE_OPERATOR_STARTUP_RESUME`, which re-reads and
    re-resolves authoritatively) in two cases so NO other primitive runs while a startup gate is
    outstanding:

    * a resumable ``operator_reported_done`` v3 gate -> the leg reserves + re-issues exactly once;
    * a READABLE legacy v1/v2 gate (:data:`GATE_READ_LEGACY`) -> the leg runs the fixed
      ``legacy_gate_reapproval_required`` disposition (reserve/send 0) and guides reapproval
      (review j#79481 F1 — a legacy latest must not let a normal primitive execute).

    Only a DEFINITIVE ``no_gate`` read (the journal read succeeded and carries no gate marker) — or
    a pre-clear / terminal / in-flight v3 gate — leaves the normal outcome unchanged (returns None,
    the normal primitive runs). Every INDETERMINATE / outstanding read — a corrupt newest gate, an
    UNREADABLE ticket-provider read, or a source error — is ALSO routed to the resume leg, which
    zero-actuates (RESUME_NOT_RESUMABLE) so no normal primitive, reserve, send, or write occurs
    (review j#79504 F1: an indeterminate startup state must fail closed at the top level, per
    j#79214 Required implementation 2, not fall through to a normal primitive).
    """
    issue = (_anchor_issue_of(outcome.durable_anchor) or "").strip()
    if not issue:
        issue = (getattr(args, "issue", None) or "").strip()
    if not issue:
        return None

    def _route(journal_fallback: str) -> WorkflowStepOutcome:
        anchor = outcome.durable_anchor
        if not anchor or anchor == "none":
            journal = (getattr(args, "journal", None) or journal_fallback or "").strip()
            anchor = f"redmine:issue={issue}:journal={journal}"
        return dataclasses.replace(
            outcome,
            primitive=PRIMITIVE_OPERATOR_STARTUP_RESUME,
            execution=EXECUTION_READY,
            durable_anchor=anchor,
        )

    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (
            _default_gate_source,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (
            GATE_READ_GATE,
            GATE_READ_NONE,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
            STATE_OPERATOR_REPORTED_DONE,
        )
        import os

        read = _default_gate_source("", os.environ)(issue)
    except Exception:  # noqa: BLE001 - a source error is INDETERMINATE -> fail closed (route to leg)
        return _route("")
    if read.status == GATE_READ_NONE:
        return None  # definitively no startup gate -> proceed with the normal primitive.
    if read.status == GATE_READ_GATE and read.gate is not None:
        if read.gate.state != STATE_OPERATOR_REPORTED_DONE:
            # A pre-clear / terminal / in-flight v3 gate is not a resume candidate; the #13760
            # pre-send admission gate guards a normal primitive's actuation, so proceed.
            return None
        return _route(read.gate.original_request.journal)  # resumable v3 gate.
    # GATE_READ_LEGACY (reapproval), GATE_READ_CORRUPT, GATE_READ_UNREADABLE, or any unknown status:
    # route to the (zero-actuating) resume leg so no normal primitive executes.
    return _route("")


def _is_herdr_forward_leg(outcome: WorkflowStepOutcome) -> bool:
    """True when the outcome is the executable Increment-3 herdr coordinator-forward leg (#13583).

    A resolved grandparent / project-gateway lane forwards a single ticketless consultation /
    work-intake. Like the herdr worker-dispatch leg, this rides its own dedicated duplicate fence
    and is driven by a dedicated executor — it is deliberately NOT part of the generic
    ``WorkflowStepOutcome.executable`` set (which is the tmux ``_primitive_argv`` rail).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (
        PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE,
        PRIMITIVE_HERDR_FORWARD_CONSULT,
    )

    return (
        outcome.execution == EXECUTION_READY
        and outcome.primitive
        in (PRIMITIVE_HERDR_FORWARD_CONSULT, PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE)
    )


def _execute_herdr_forward_leg(
    outcome: WorkflowStepOutcome, args: argparse.Namespace
) -> tuple[int, str]:
    """Run the fenced one-step herdr coordinator forward; return (rc, summary text) (#13583 Inc 3).

    Delegates to :func:`...herdr_workflow_step.execute_herdr_forward_leg`, which re-resolves the
    sender identity + durable role authority at action time (safety-contract point 2), resolves the
    single live target from the herdr inventory, and drives the dedicated forward fence around one
    ticketless send. ``rc`` is ``0`` for a delivered / never-send (zero-send) outcome and ``1`` for
    a fence-unavailable / uncertain outcome the operator must reconcile.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (
        execute_herdr_forward_leg,
    )

    result = execute_herdr_forward_leg(args, outcome)
    text = (
        f"forward_result: {'sent' if result.sent else 'zero_send'}\n"
        f"target_status: {result.target_status}\n"
        f"fence_state: {result.fence_state}\n"
        f"reason: {result.reason}\n"
        f"detail: {result.detail}"
    )
    # rc 0 = a resolved step: a delivered forward, or a legitimate fail-closed zero-send (missing /
    # duplicate / drift / self — a valid "nothing to send" step). rc 1 = an outcome the operator
    # must reconcile: an uncertain / failed send, or a do-not-send fence-unavailable.
    if result.sent:
        ok = result.send is not None and result.send.result == "delivered"
    else:
        ok = result.reason != "herdr_forward_fence_unavailable"
    return (0 if ok else 1), text


def _herdr_step_preflight(args: argparse.Namespace) -> WorkflowStepOutcome | None:
    """Herdr-native ``workflow step`` resolution for the current lane, or ``None`` under tmux.

    ``workflow step`` resolves the current lane from ``current_pane()`` — the tmux
    ``TMUX_PANE`` ``%pane`` — matched against the tmux discovery inventory. Under
    ``terminal_transport.backend: herdr`` there is no ``TMUX_PANE`` (or the pane is not in
    the tmux inventory), so the standard entrypoint would die on ``TMUX_PANE is not set`` or
    fold to a tmux-shaped ``self_lane_unresolved`` — the #13435 j#74176 -> j#74177 / #13494
    recurrence.

    Redmine #13489 replaces the #13446 fail-closed dead end (which merely pointed the
    operator at ``sublane create/start --execute``) with herdr-native resolution: when the
    repo selects the herdr backend, this delegates to
    :func:`...herdr_workflow_step.resolve_herdr_step_outcome`, which classifies the lane role
    from the launch-time sender identity (``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``) — only a
    non-default lane slot gets a class (``codex`` -> sublane gateway, ``claude`` -> worker); a
    default-lane pair or unknown provider fails closed — verifies the lane's Redmine
    ``issue+journal`` anchor against the durable workflow gate (runtime store), and for a
    gateway resolves the same-lane worker cardinality. It returns a role-appropriate
    :class:`WorkflowStepOutcome` (worker reads its verified anchor; gateway dispatches /
    monitors its single live same-lane worker), or fails closed on an unattested identity /
    unclassifiable lane / unverified anchor / missing-or-duplicate worker. Returns ``None``
    under the tmux backend so the tmux path (and its byte-identical output) is unchanged.

    Increment 1 (Redmine #13489 j#74685 design_boundary) is resolution-only: the outcome
    names the next action / owner / herdr surface but performs no sublane lifecycle mutation
    and no delivery. The policy-permitted one-step auto-execution of ``sublane
    create/start/dispatch`` (and the fail-closed destructive drain/retire boundary) is
    increment 2, gated behind the mandatory task-level design mid-review.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (
        resolve_herdr_step_outcome,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (
        herdr_backend_active,
    )

    repo_root = repo_root_from_args(args)
    if not herdr_backend_active(repo_root):
        return None
    return resolve_herdr_step_outcome(args)


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
    as_json = getattr(args, "as_json", False)
    session = getattr(args, "session", None)
    dry_run = getattr(args, "dry_run", False)

    # Resolve the LIVE lane outcome. The backend difference is confined here (mid-review
    # #13489 j#74748 F2): under the herdr backend the lane is resolved herdr-natively from the
    # launch-time sender identity; otherwise the tmux rail resolves it from `current_pane` +
    # the tmux inventory. Everything after this — the store reconcile, the dry-run / executable
    # branch, the output envelope — is backend-agnostic, so herdr no longer runs a second,
    # divergent next-action state machine. The tmux path stays byte-identical (herdr_live is
    # None under `backend: tmux`, so `require_tmux()` and the tmux resolution run exactly as
    # before).
    herdr_live = _herdr_step_preflight(args)
    if herdr_live is not None:
        live = herdr_live
        # The herdr live anchor was verified against source-of-truth Redmine; issue-correlate the
        # store reconcile against it so a caller-supplied store's cross-issue pending action is not
        # surfaced onto this lane (Redmine #13489 F3c). The tmux path passes None (byte-invariant).
        live_anchor_issue = _anchor_issue_of(live.durable_anchor)
    else:
        require_tmux()
        self_pane = current_pane()
        live = resolve_workflow_step(
            _discover_candidates(),
            self_pane=self_pane,
            anchor=_anchor_from_args(args),
            pending_callback=_pending_callback_from_args(args),
            session=session,
        )
        live_anchor_issue = None

    # Reconcile the live routing outcome with the persisted runtime store's pending
    # action (Redmine #13291). The store is read fail-open: absent / unreadable degrades
    # to the live outcome unchanged (backward compatible), a gating pending action
    # fail-closed-gates a live forward leg, a pending non-gating action is surfaced.
    # Fold the store with the SAME repo-local binding resume uses, resolved from the
    # current self lane's repo root (review j#72693), so the reconcile input matches resume.
    store_action, store_status = _load_store_action(args, repo_root=live.repo_root)
    reconciled = reconcile_step_with_store(
        live, store_action, store_status=store_status, live_anchor_issue=live_anchor_issue
    )
    outcome = reconciled.outcome

    # Redmine #13813: a durable operator startup gate takes precedence — route this step to the
    # exactly-once resume leg. The check reads the latest gate at action time and overrides the
    # outcome for a resumable gate (``operator_reported_done``), a readable legacy gate (reapproval),
    # AND every INDETERMINATE read (corrupt / unreadable / source error), which route to the leg's
    # zero-actuating fail-closed disposition so no normal primitive runs (review j#79504 / j#79524
    # F1). Only a DEFINITIVE no_gate (or a pre-clear / terminal / in-flight v3 gate) leaves the
    # normal outcome to execute.
    resume_outcome = _maybe_operator_startup_resume_outcome(args, outcome)
    if resume_outcome is not None:
        outcome = resume_outcome

    # Redmine #13892: gateway-owned disposition intake (the writer's ONLY production caller);
    # its result rides the envelope because an unseen refusal is an unrecoverable zero-write.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_disposition_intake import disposition_payload_fields, disposition_text_lines, maybe_record_gateway_disposition  # noqa: E501
    disposition = maybe_record_gateway_disposition(args, outcome, dry_run=dry_run)
    # The increment-2 herdr worker-dispatch leg and the increment-3 herdr coordinator-forward leg
    # are executable but ride their own dedicated fences, not the generic `executable` set (Redmine
    # #13489 / #13583). The #13813 resume leg is the same shape. Treat each as an executable leg.
    is_herdr_dispatch = _is_herdr_dispatch_leg(outcome)
    is_herdr_forward = _is_herdr_forward_leg(outcome)
    is_startup_resume = _is_startup_resume_leg(outcome)

    # Dry-run, or a non-executable outcome (blocked / gated / grandchild Redmine-work
    # no-op): report the resolved outcome, mutate nothing.
    if dry_run or (
        not outcome.executable
        and not is_herdr_dispatch
        and not is_herdr_forward
        and not is_startup_resume
    ):
        reported = outcome
        if dry_run and (
            outcome.executable or is_herdr_dispatch or is_herdr_forward or is_startup_resume
        ):
            # Reflect that the executable leg was not actually run.
            reported = dataclasses.replace(outcome, execution=EXECUTION_DRY_RUN)
        if as_json:
            payload = reported.as_payload()
            payload.update({**reconciled.reconcile_payload_fields(), **disposition_payload_fields(disposition)})  # noqa: E501
            print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_outcome_text(reported)
            for line in reconciled.reconcile_text_lines() + disposition_text_lines(disposition):
                print(line)
        return 0 if reported.ok else 1

    # Executable leg: dispatch the internal primitive, the fenced herdr worker dispatch, or the
    # fenced herdr coordinator forward.
    if is_herdr_dispatch:
        rc, primitive_out = _execute_herdr_dispatch_leg(outcome, args)
    elif is_herdr_forward:
        rc, primitive_out = _execute_herdr_forward_leg(outcome, args)
    elif is_startup_resume:
        rc, primitive_out = _execute_startup_resume_leg(outcome, args)
    else:
        rc, primitive_out = _execute_primitive(outcome, args, session=session)
    executed = dataclasses.replace(outcome, execution=EXECUTION_EXECUTED)
    if as_json:
        payload = executed.as_payload()
        payload["primitive_rc"] = rc
        payload["primitive_output"] = primitive_out
        payload.update({**reconciled.reconcile_payload_fields(), **disposition_payload_fields(disposition)})  # noqa: E501
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_outcome_text(executed)
        print(f"primitive_rc: {rc}")
        if primitive_out.strip():
            print("--- primitive output ---")
            print(primitive_out, end="" if primitive_out.endswith("\n") else "\n")
        for line in reconciled.reconcile_text_lines() + disposition_text_lines(disposition):
            print(line)
    # Surface the primitive's own rc: the step resolved an executable leg, but the
    # delivery's success/fail-closed result is the primitive's (e.g. a gateway that
    # vanished between resolution and delivery), so the caller sees the real outcome.
    return rc


def cmd_workflow_dispatch_fence(args: argparse.Namespace) -> int:
    """Operator surface for the increment-2 dispatch idempotency fence (Redmine #13489 F1).

    Distinguishes an initial ``--bootstrap`` (safe first init) from a deliberate ``--recover``
    (loss recovery: mints a fresh store under a new nonce — invoke ONLY after reconciling the
    lost action in Redmine + issuing a new ``action_id``). With no flag, reports status. The
    reserve path never auto-creates the store, so this is the sanctioned way to (re)initialize it.
    """
    from mozyo_bridge.core.state.dispatch_outbox_fence import (
        DispatchOutboxFence,
        DispatchOutboxFenceError,
    )

    fence = DispatchOutboxFence()
    try:
        if getattr(args, "fence_recover", False):
            fence.recover()
            print(f"dispatch fence recovered (fresh store) at {fence.path}")
            print("reconcile the lost action + issue a NEW action_id upstream before re-dispatch")
            return 0
        if getattr(args, "fence_bootstrap", False):
            fence.bootstrap()
            print(f"dispatch fence bootstrapped at {fence.path}")
            return 0
    except DispatchOutboxFenceError as exc:
        print(f"dispatch fence error: {exc}")
        print("a store loss/replacement needs `workflow dispatch-fence --recover`")
        return 1
    state = "bootstrapped" if fence.is_bootstrapped() else "absent / not bootstrapped"
    print(f"dispatch fence: {state} at {fence.path}")
    return 0


def cmd_workflow_forward_fence(args: argparse.Namespace) -> int:
    """Operator surface for the herdr coordinator-forward generation store (Redmine #13583 R1-F2).

    The forward store's ``workflow step`` execution path NEVER auto-creates it (an auto-create after
    a total loss would resurrect a lost store and let an already-delivered forward re-send). This is
    the sanctioned explicit init: ``--bootstrap`` (safe first init — both DB + sidecar absent) or
    ``--recover`` (deliberate loss recovery: a fresh store under a new nonce; invoke ONLY after
    reconciling the lost forward). With no flag, reports status.
    """
    from mozyo_bridge.core.state.forward_outbox_fence import (
        ForwardOutboxFence,
        ForwardOutboxFenceError,
    )

    fence = ForwardOutboxFence()
    try:
        if getattr(args, "fence_recover", False):
            fence.recover()
            print(f"forward store recovered (fresh store) at {fence.path}")
            print("reconcile the lost forward before relying on the new generation series")
            return 0
        if getattr(args, "fence_bootstrap", False):
            fence.bootstrap()
            print(f"forward store bootstrapped at {fence.path}")
            return 0
    except ForwardOutboxFenceError as exc:
        print(f"forward store error: {exc}")
        print("a store loss/replacement needs `workflow forward-fence --recover`")
        return 1
    state = "bootstrapped" if fence.is_bootstrapped() else "absent / not bootstrapped"
    print(f"forward store: {state} at {fence.path}")
    return 0


from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callback_publication import (
    register_callback_publication_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callback_lease import (
    cmd_workflow_callback_lease,
)


def register(sub) -> None:
    """Register ``workflow`` (``step`` / ``fill-decision`` / ``admission`` / ...).

    ``workflow step`` (Redmine #12755) advances one safe workflow step;
    ``workflow fill-decision`` (Redmine #12855) reports the advisory Post-Dispatch
    Fill Loop decision for an already-classified lane set; ``workflow admission``
    (Redmine #12856) is the Redmine-aware companion that classifies each lane from its
    durable-record facts first; ``workflow lane-admission`` (Redmine #12921) decides for
    one candidate lane whether to allow_dispatch / serialize / block / escalate based on
    concrete engineering/workflow risk (not coordinator convenience); ``workflow runtime`` (Redmine #12857) is the stateful
    slice that replays an ordered durable event log (with duplicate suppression) into
    current lane state and the overall next action; ``workflow resume`` (Redmine #12671)
    reads the *persisted* mozyo-DB runtime state ``workflow runtime --persist`` wrote and
    reports the current state plus the enriched ``workflow.next_action`` (route_identity /
    anchor / risk_level / requires_confirmation / blocked_reason); ``workflow watch``
    (Redmine #12672) ingests structured Redmine journal markers (deduped by the
    ``redmine:<issue>:<journal>`` anchor) into that same store and reports the resulting
    pending action, recording a fail-closed ``failed`` state for a missing / ambiguous route
    rather than sending. ``workflow glance`` (Redmine #13435) is the read-only companion that
    projects every active lane/US into one view — workflow_state (folded from the durable
    Redmine record) + next_action + next_owner + delivery_anomaly — so a coordinator can spot
    a "looks stopped but is really delivery-stuck" lane without hand-correlating status + each
    journal + a herdr pane read; it mutates nothing. The fill-decision / admission / runtime /
    resume / watch / glance subcommands are registered from their sibling modules so this file
    stays focused on the step state machine.
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
    register_lane_admission(workflow_sub)
    register_dispatch_plan(workflow_sub)
    register_dispatch_ir(workflow_sub)
    register_runtime(workflow_sub)
    register_resume(workflow_sub)
    register_watch(workflow_sub)
    register_glance(workflow_sub)
    register_drain_queue(workflow_sub)
    register_review_escalation(workflow_sub)
    register_callbacks(workflow_sub)
    register_supervisor(workflow_sub)

    register_fence_operator_parsers(workflow_sub)

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
    step.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=argparse.SUPPRESS,  # test/debug override for the #13291 runtime-store read
    )
    step.set_defaults(func=cmd_workflow_step)


__all__ = (
    "cmd_workflow_step",
    "cmd_workflow_fill_decision",
    "cmd_workflow_admission",
    "cmd_workflow_lane_admission",
    "cmd_workflow_dispatch_plan",
    "cmd_workflow_dispatch_ir",
    "cmd_workflow_runtime",
    "cmd_workflow_resume",
    "cmd_workflow_watch",
    "cmd_workflow_glance",
    "register",
)
