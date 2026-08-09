"""Typed application API for the high-level handoff operations (Redmine #15149).

The boundary a **local MCP server calls in-process** — the shared application
processing #15148 requires so workflow / identity / authority / send-safety
judgement is not implemented twice, once behind the CLI and once behind MCP.

Two entry points, one body:

- :func:`orchestrate_handoff_input` is the shared orchestration entry. It takes
  the typed
  :class:`~...domain.handoff_command_input.HandoffCommandInput`, installs the
  runtime transport binding for the send, and runs
  ``commands.run_handoff_orchestration``. The CLI's ``orchestrate_handoff``
  Namespace adapter and the typed API below both go through it, so *every* gate
  runs identically for both callers and neither entry can weaken one.
- :func:`run_handoff` is the typed operation API. It takes a
  :class:`HandoffRequest` (a core-owned operation name + the typed input + the
  resolved repo root) and returns a :class:`HandoffResult` (exit code, the
  structured :class:`DeliveryOutcome`\\ s, the injection-stage delivery verdict,
  and the fail-closed message). It applies the operation's entry policy from the
  core-owned ``handoff_operation`` table the CLI entries read.

What this API deliberately does **not** depend on:

- **no shell argv / no argparse.** The request is a typed value object; this
  module imports no ``argparse`` and builds no ``Namespace``.
- **no TTY.** Nothing here reads a terminal, and the default record sink is a
  collector rather than the CLI's stdout printer, so a caller with no stdout
  channel loses no information.
- **no stdout parse.** The outcome comes from the structured ``DeliveryOutcome``
  the orchestration publishes at every terminal path, and the fail-closed reason
  comes from :class:`~mozyo_bridge.shared.errors.CommandAbort`, which carries the
  ``die`` message as an attribute. Neither is recovered from printed text.
- **no subprocess.** The orchestration runs in this process; nothing shells out
  to ``mozyo-bridge``.

What it does **not** own: any decision. The entry policy is read from the core
table, and every workflow / identity / authority / gateway-route / send-safety
gate stays exactly where it is inside the orchestration. This API is an entry,
not an authority — an MCP tool built on it can reach no gate the CLI cannot, and
can skip none the CLI runs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (
    HandoffCommandInput,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_operation import (
    HandoffEntryPolicy,
    entry_policy_for,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (
    SEND_SEMANTIC_SELECT_TARGET,
    send_semantic_gap,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    STAGE_SUBMITTED_CONFIRMED,
    injection_stage_for_outcome,
)
from mozyo_bridge.shared.errors import CommandAbort

#: The run reached a typed terminal path and returned an exit code.
STATUS_COMPLETED = "completed"

#: A gate refused the run: nothing further ran, and ``error_message`` carries the
#: refusal. This is the typed shape of the CLI's ``error: ...`` + non-zero exit.
STATUS_FAIL_CLOSED = "fail_closed"


# --------------------------------------------------------------------------- #
# Shared orchestration entry (the CLI and the typed API both run this)
# --------------------------------------------------------------------------- #


def orchestrate_handoff_input(
    inp: HandoffCommandInput,
    *,
    repo_root: Path,
    publish: Callable[[Any], None],
    resolved_herdr_target_capability: Any = None,
    emit_outcome: Optional[Callable[..., None]] = None,
) -> int:
    """Run the shared handoff orchestration over a typed input.

    Installs the config-selected runtime transport binding (Redmine #13253 /
    #13255) for the duration of the send — the step that used to be a Namespace
    decorator on ``orchestrate_handoff`` — and then runs the orchestration body.
    The binding selection reads the same repo-local ``terminal_transport``
    config through the same module seam as before, from a typed context rather
    than a parsed Namespace, so a CLI send and an API send resolve the same
    backend.

    ``publish`` is the delivery-outcome hand-back channel (the CLI writes it onto
    its ``args``; the typed API captures it); ``emit_outcome`` is the record sink
    (the CLI's printer by default).
    """
    from mozyo_bridge.application import commands
    from mozyo_bridge.application.handoff_transport_wiring import (
        HandoffTransportContext,
        runtime_transport_binding,
    )

    context = HandoffTransportContext(
        repo_root=repo_root,
        to=inp.to,
        target=inp.target,
        target_repo=inp.target_repo,
        target_lane=inp.target_lane,
        resolved_target_capability=resolved_herdr_target_capability,
    )
    with runtime_transport_binding(context):
        return commands.run_handoff_orchestration(
            inp,
            repo_root=repo_root,
            publish=publish,
            resolved_herdr_target_capability=resolved_herdr_target_capability,
            emit_outcome=emit_outcome,
        )


# --------------------------------------------------------------------------- #
# Typed request / result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HandoffTargetSelection:
    """Semantic target selection inputs for the `send` operation (Redmine #12663).

    The CLI expresses this as ``--select`` plus ``--target-session``, resolved
    against the sender's own cwd. The API takes ``sender_cwd`` explicitly rather
    than reading the process cwd, so a server process's cwd can never silently
    become the sender's workspace identity.
    """

    sender_cwd: str
    session: Optional[str] = None


@dataclass(frozen=True)
class HandoffRequest:
    """A typed high-level handoff invocation."""

    operation: str
    input: HandoffCommandInput
    repo_root: Path
    selection: Optional[HandoffTargetSelection] = None
    resolved_herdr_target_capability: Any = None


@dataclass(frozen=True)
class HandoffEmission:
    """One structured record the orchestration emitted, with its context.

    ``outcome`` is the ``DeliveryOutcome``; ``context`` is the emit context the
    CLI would have rendered alongside it (recovery command, duplicate lane panes,
    role-profile contract, retry / activation telemetry, submit / turn-start /
    startup-admission lines). Captured as data so a non-CLI caller needs no
    rendered text.
    """

    outcome: Any
    context: Mapping[str, Any]


@dataclass(frozen=True)
class HandoffResult:
    """The typed result of a high-level handoff operation."""

    operation: str
    status: str
    exit_code: int
    outcome: Any = None
    emissions: tuple[HandoffEmission, ...] = ()
    delivered: bool = False
    error_message: Optional[str] = None

    @property
    def fail_closed(self) -> bool:
        """True when a gate refused the run before it reached a normal return."""
        return self.status == STATUS_FAIL_CLOSED


# --------------------------------------------------------------------------- #
# Port + live adapter
# --------------------------------------------------------------------------- #


class HandoffApplicationOps(Protocol):
    """Port: what the typed API needs from its environment.

    Exercisable with a synthetic fake, so the API's entry policy, capture, and
    fail-closed mapping are testable without tmux, herdr, or a receiver.
    """

    def orchestrate(
        self,
        inp: HandoffCommandInput,
        *,
        repo_root: Path,
        publish: Callable[[Any], None],
        resolved_herdr_target_capability: Any,
        emit_outcome: Callable[..., None],
    ) -> int: ...

    def select_semantic_target(
        self,
        *,
        role: Optional[str],
        repo: Optional[str],
        session: Optional[str],
        project: Optional[str],
        sender_cwd: str,
    ) -> Any: ...


class LiveHandoffApplicationOps:
    """Live :class:`HandoffApplicationOps`.

    Both dependencies resolve *at call time* through the same modules the CLI
    entry uses, so the existing monkeypatch seams keep intercepting and the API
    cannot end up on a different code path than the CLI.
    """

    def orchestrate(
        self,
        inp: HandoffCommandInput,
        *,
        repo_root: Path,
        publish: Callable[[Any], None],
        resolved_herdr_target_capability: Any,
        emit_outcome: Callable[..., None],
    ) -> int:
        return orchestrate_handoff_input(
            inp,
            repo_root=repo_root,
            publish=publish,
            resolved_herdr_target_capability=resolved_herdr_target_capability,
            emit_outcome=emit_outcome,
        )

    def select_semantic_target(
        self,
        *,
        role: Optional[str],
        repo: Optional[str],
        session: Optional[str],
        project: Optional[str],
        sender_cwd: str,
    ) -> Any:
        from mozyo_bridge.application.commands_target_select import (
            select_semantic_target,
        )

        return select_semantic_target(
            role=role,
            repo=repo,
            session=session,
            project=project,
            sender_cwd=sender_cwd,
        )


# --------------------------------------------------------------------------- #
# The typed operation API
# --------------------------------------------------------------------------- #


def apply_entry_policy(
    inp: HandoffCommandInput, policy: HandoffEntryPolicy
) -> HandoffCommandInput:
    """Normalize ``inp``'s entry-policy fields to ``policy`` (fail-closed).

    Every entry-policy field is set from the operation's policy rather than
    carried through from the caller, so a request cannot smuggle in a rail its
    operation does not have (e.g. asking for ``send`` while setting the
    anchorless ticketless flag, which would skip the anchor requirement). The
    receiver pin and the consult kind default are applied the same way the CLI
    entry applies them.
    """
    updated = replace(
        inp,
        default_kind=policy.default_kind,
        require_receiver_binding=policy.require_receiver_binding,
        ticketless=policy.ticketless,
        ticketless_consultation=False,
        ticketless_work_intake=False,
    )
    if policy.pinned_receiver is not None:
        updated = replace(updated, to=policy.pinned_receiver)
    if policy.pinned_kind is not None and updated.kind is None:
        updated = replace(updated, kind=policy.pinned_kind)
    return updated


def run_handoff(
    request: HandoffRequest,
    *,
    ops: Optional[HandoffApplicationOps] = None,
) -> HandoffResult:
    """Run one high-level handoff operation and return its typed result.

    Raises :class:`~...domain.handoff_operation.UnknownHandoffOperation` for an
    operation outside the core-owned vocabulary — a schema violation, not a
    workflow refusal. Every *workflow* refusal comes back as a
    :data:`STATUS_FAIL_CLOSED` result carrying the gate's message and the
    structured blocked outcome the gate emitted, never as a traceback and never
    as text the caller must parse.
    """
    resolved_ops = ops or LiveHandoffApplicationOps()
    policy = entry_policy_for(request.operation)
    inp = apply_entry_policy(request.input, policy)

    published: list[Any] = []
    emissions: list[HandoffEmission] = []

    def _publish(outcome: Any) -> None:
        published.append(outcome)

    def _emit(outcome: Any, **context: Any) -> None:
        emissions.append(
            HandoffEmission(outcome=outcome, context=MappingProxyType(dict(context)))
        )

    def _result(status: str, exit_code: int, error_message: Optional[str]) -> HandoffResult:
        outcome = published[-1] if published else None
        return HandoffResult(
            operation=request.operation,
            status=status,
            exit_code=exit_code,
            outcome=outcome,
            emissions=tuple(emissions),
            delivered=(
                outcome is not None
                and injection_stage_for_outcome(outcome) == STAGE_SUBMITTED_CONFIRMED
            ),
            error_message=error_message,
        )

    try:
        if policy.semantic_selection and request.selection is not None:
            inp = _apply_semantic_selection(inp, request.selection, resolved_ops)
        exit_code = resolved_ops.orchestrate(
            inp,
            repo_root=request.repo_root,
            publish=_publish,
            resolved_herdr_target_capability=request.resolved_herdr_target_capability,
            emit_outcome=_emit,
        )
    except CommandAbort as exc:
        return _result(STATUS_FAIL_CLOSED, _exit_code_of(exc), exc.message)
    except SystemExit as exc:  # a gate that exits without the typed carrier
        return _result(STATUS_FAIL_CLOSED, _exit_code_of(exc), None)
    return _result(STATUS_COMPLETED, int(exit_code), None)


def _exit_code_of(exc: SystemExit) -> int:
    """The abort's exit code, defaulting to the CLI's fail-closed ``2``."""
    code = exc.code
    return code if isinstance(code, int) else 2


def _apply_semantic_selection(
    inp: HandoffCommandInput,
    selection: HandoffTargetSelection,
    ops: HandoffApplicationOps,
) -> HandoffCommandInput:
    """Resolve the semantic target onto ``inp`` (the `--select` equivalent).

    Runs the same shared send-semantics authority for the select/explicit-target
    mutual exclusion and the same fail-closed
    ``commands_target_select.select_semantic_target`` resolver the CLI uses, then
    writes the resolved pane and matched repo root onto the typed input so the
    unchanged identity gates re-validate the chosen pane. The selector narrows;
    the gates still enforce.
    """
    if (
        send_semantic_gap(select=True, target=inp.target)
        == SEND_SEMANTIC_SELECT_TARGET
    ):
        raise CommandAbort(
            "--select resolves the target pane semantically and is mutually "
            "exclusive with an explicit --target; drop one of them."
        )
    selected = ops.select_semantic_target(
        role=inp.to,
        repo=inp.target_repo,
        session=selection.session,
        project=inp.target_project,
        sender_cwd=selection.sender_cwd,
    )
    updated = replace(inp, target=selected.pane_id)
    if selected.repo_root:
        updated = replace(updated, target_repo=selected.repo_root)
    return updated


__all__ = (
    "HandoffApplicationOps",
    "HandoffEmission",
    "HandoffRequest",
    "HandoffResult",
    "HandoffTargetSelection",
    "LiveHandoffApplicationOps",
    "STATUS_COMPLETED",
    "STATUS_FAIL_CLOSED",
    "apply_entry_policy",
    "orchestrate_handoff_input",
    "run_handoff",
)
