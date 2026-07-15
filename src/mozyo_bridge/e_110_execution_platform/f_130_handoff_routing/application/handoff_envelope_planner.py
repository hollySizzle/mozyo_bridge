"""Anchor / Profile envelope planner for the handoff orchestration (Redmine #13729 tranche 2).

Design j#78394 / Coordinator Verdict j#78404 factor the *envelope planning* out of the
1699-line ``orchestrate_handoff`` monolith into a small, typed, fake-port-testable
application service, ahead of the target-resolution (Task 3) and transport (Task 4)
tranches. The planner owns exactly two concerns, in the two places the facade needs them:

- :meth:`HandoffEnvelopePlanner.plan_anchor` — the *early* anchor step: build the typed
  Redmine / Asana / ticketless (callback / consultation / work-intake) anchor + payloads
  from the typed :class:`HandoffCommandInput`. Runs before target resolution.
- :meth:`HandoffEnvelopePlanner.plan_delivery_envelope` — the *pre-send* step: resolve the
  execution root, role profile, transition role, workflow contract, notification body, and
  landing marker into a frozen :class:`HandoffEnvelope`. Runs after the route/target/admission
  gates and before the transport rail (which Task 4 will consume the envelope from).

Namespace confinement (tranche 1 / review j#78706) is preserved: the planner takes the typed
``HandoffCommandInput`` and plain scalars — never an ``argparse.Namespace``.

Byte-compatibility: on any malformed input the planner raises :class:`EnvelopePlanError`
carrying the *exact* ``make_outcome`` / ``_emit`` extra kwargs the original inline block
passed at that stage (the cumulative partial state — anchor stage carries nothing beyond the
base context; later stages carry ``execution_root`` / ``role_profile`` / ``transition_role`` /
``workflow_contract`` / ticketless payloads / ``role_profile_contract`` exactly as computed so
far). The facade merges those with its base context (receiver / target / anchor / mode / kind /
source) so the emitted outcome and ``die`` wording are identical to the pre-extraction body.

The planner depends only on :class:`EnvelopePlannerOps`; :class:`LiveEnvelopePlannerOps` binds
the real domain / application functions (imported from the same modules ``commands.py`` used,
so any existing monkeypatch behaviour is unchanged), and unit tests inject a fake port.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AUTO_TARGET_REPO,
    AnchorError,
    ExecutionRoot,
    NormalizedAnchor,
    TicketlessAnchor,
    TicketlessConsultationAnchor,
    TicketlessWorkIntakeAnchor,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (
    HandoffCommandInput,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileError,
    RoleProfileResolution,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    TicketlessCallback,
    TicketlessCallbackError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    TicketlessConsultation,
    TicketlessConsultationError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
    TicketlessWorkIntake,
    TicketlessWorkIntakeError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    TransitionRoleBoundary,
    TransitionRoleError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
    WorkflowContractBundle,
    WorkflowContractError,
)


class EnvelopePlanError(Exception):
    """A malformed-input failure while planning the handoff envelope.

    Carries the exact structured-outcome / emit extras the original inline block passed at
    the failing stage, so the facade reproduces a byte-identical blocked outcome + ``die``.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        outcome_extra: dict[str, Any] | None = None,
        emit_extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.outcome_extra = outcome_extra or {}
        self.emit_extra = emit_extra or {}


@dataclass(frozen=True)
class AnchorPlan:
    """The typed anchor + the (at most one) ticketless payload derived from the input.

    ``anchor`` is a :data:`NormalizedAnchor` — the union already covers the Redmine /
    Asana normalized forms AND the three ticketless anchors this planner derives.
    """

    anchor: NormalizedAnchor
    callback_payload: TicketlessCallback | None = None
    consultation_payload: TicketlessConsultation | None = None
    work_intake_payload: TicketlessWorkIntake | None = None


@dataclass(frozen=True)
class HandoffEnvelope:
    """The resolved pre-send envelope: everything the transport rail types + records."""

    execution_root: ExecutionRoot | None
    role_profile_resolution: RoleProfileResolution | None
    role_profile_contract: str | None
    transition_role_boundary: TransitionRoleBoundary | None
    workflow_contract_bundle: WorkflowContractBundle | None
    body: str
    marker: str


class EnvelopePlannerOps(Protocol):
    """Port: the domain / application functions the planner needs from its environment."""

    def normalize_anchor(
        self,
        source: str | None,
        *,
        task_id: str | None,
        comment_id: str | None,
        anchor_url: str | None,
        issue: str | None,
        journal: str | None,
    ) -> NormalizedAnchor: ...

    def build_execution_root(
        self, workdir_abs: str, *, repo_root_abs: str | None
    ) -> ExecutionRoot: ...

    def infer_repo_root(self, cwd: str) -> str | None: ...

    def resolve_handoff_profile_fields(
        self,
        role_profile: str,
        profile_field: Iterable[str] | None,
        human_pointer: str,
        repo_root: Path,
    ) -> dict[str, str]: ...

    def resolve_role_profile(
        self, role_profile: str, profile_fields: Mapping[str, str] | None
    ) -> RoleProfileResolution: ...

    def resolve_transition_role(self, transition_role: str) -> TransitionRoleBoundary: ...

    def resolve_workflow_contract(self, workflow_contract: str) -> WorkflowContractBundle: ...

    def build_notification_body(
        self,
        anchor: NormalizedAnchor,
        kind: str | None,
        summary: str | None,
        receiver: str,
        *,
        execution_root: ExecutionRoot | None,
        role_profile: RoleProfileResolution | None,
        transition_role: TransitionRoleBoundary | None,
        workflow_contract: WorkflowContractBundle | None,
        ticketless_callback: TicketlessCallback | None,
        ticketless_consultation: TicketlessConsultation | None,
        ticketless_work_intake: TicketlessWorkIntake | None,
    ) -> str: ...

    def build_marker(
        self, anchor: NormalizedAnchor, kind: str | None, receiver: str
    ) -> str: ...


class LiveEnvelopePlannerOps:
    """Live :class:`EnvelopePlannerOps` over the real domain / application functions.

    Imports each function from the same module ``commands.py`` imported it from, so any
    existing monkeypatch behaviour (e.g. ``infer_repo_root`` patched at its source) is
    unchanged, and the extracted calls stay byte-identical to the inline body.
    """

    @staticmethod
    def normalize_anchor(source, *, task_id, comment_id, anchor_url, issue, journal):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            normalize_anchor,
        )

        return normalize_anchor(
            source,
            task_id=task_id,
            comment_id=comment_id,
            anchor_url=anchor_url,
            issue=issue,
            journal=journal,
        )

    @staticmethod
    def build_execution_root(workdir_abs, *, repo_root_abs):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            build_execution_root,
        )

        return build_execution_root(workdir_abs, repo_root_abs=repo_root_abs)

    @staticmethod
    def infer_repo_root(cwd):
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            infer_repo_root,
        )

        return infer_repo_root(cwd)

    @staticmethod
    def resolve_handoff_profile_fields(role_profile, profile_field, human_pointer, repo_root):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.role_profile_field_resolution import (
            resolve_handoff_profile_fields,
        )

        return resolve_handoff_profile_fields(
            role_profile, profile_field, human_pointer, repo_root
        )

    @staticmethod
    def resolve_role_profile(role_profile, profile_fields):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
            resolve_role_profile,
        )

        return resolve_role_profile(role_profile, profile_fields)

    @staticmethod
    def resolve_transition_role(transition_role):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
            resolve_transition_role,
        )

        return resolve_transition_role(transition_role)

    @staticmethod
    def resolve_workflow_contract(workflow_contract):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
            resolve_workflow_contract,
        )

        return resolve_workflow_contract(workflow_contract)

    @staticmethod
    def build_notification_body(
        anchor,
        kind,
        summary,
        receiver,
        *,
        execution_root,
        role_profile,
        transition_role,
        workflow_contract,
        ticketless_callback,
        ticketless_consultation,
        ticketless_work_intake,
    ):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            build_notification_body,
        )

        return build_notification_body(
            anchor,
            kind,
            summary,
            receiver,
            execution_root=execution_root,
            role_profile=role_profile,
            transition_role=transition_role,
            workflow_contract=workflow_contract,
            ticketless_callback=ticketless_callback,
            ticketless_consultation=ticketless_consultation,
            ticketless_work_intake=ticketless_work_intake,
        )

    @staticmethod
    def build_marker(anchor, kind, receiver):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            build_marker,
        )

        return build_marker(anchor, kind, receiver)


class HandoffEnvelopePlanner:
    """Plan the handoff anchor (early) and delivery envelope (pre-send)."""

    def __init__(self, ops: EnvelopePlannerOps | None = None) -> None:
        self._ops = ops or LiveEnvelopePlannerOps()

    def plan_anchor(self, inp: HandoffCommandInput) -> AnchorPlan:
        """Build the typed anchor + ticketless payload from the typed input.

        Raises :class:`EnvelopePlanError` (``invalid_args`` for a malformed ticketless
        payload, ``invalid_anchor`` for a bad Redmine/Asana anchor) — with no extra
        outcome fields, matching the original early anchor block (target/anchor are still
        ``None`` there).
        """
        if inp.ticketless and inp.ticketless_work_intake:
            try:
                work_intake_payload = TicketlessWorkIntake(
                    work_shape=inp.work_shape,
                    callback_to_role=inp.callback_to_role,
                    callback_methods=inp.callback_methods,
                    read_contract=inp.read_contract,
                    forward_action_id=inp.forward_action_id or "",
                )
            except TicketlessWorkIntakeError as exc:
                raise EnvelopePlanError("invalid_args", str(exc))
            anchor = TicketlessWorkIntakeAnchor(
                work_shape=work_intake_payload.work_shape,
                callback_to_role=work_intake_payload.callback_to_role,
            )
            return AnchorPlan(anchor=anchor, work_intake_payload=work_intake_payload)

        if inp.ticketless and inp.ticketless_consultation:
            try:
                consultation_payload = TicketlessConsultation(
                    consultation_kind=inp.consultation_kind,
                    callback_to_role=inp.callback_to_role,
                    callback_methods=inp.callback_methods,
                    read_contract=inp.read_contract,
                    forward_action_id=inp.forward_action_id or "",
                )
            except TicketlessConsultationError as exc:
                raise EnvelopePlanError("invalid_args", str(exc))
            anchor = TicketlessConsultationAnchor(
                consultation_kind=consultation_payload.consultation_kind,
                callback_to_role=consultation_payload.callback_to_role,
            )
            return AnchorPlan(anchor=anchor, consultation_payload=consultation_payload)

        if inp.ticketless:
            try:
                callback_payload = TicketlessCallback(
                    classification=inp.classification,
                    dispatch_decision=inp.dispatch_decision,
                    next_action_owner=inp.workflow_next_owner,
                    callback_reason=inp.callback_reason,
                    read_contract=inp.read_contract,
                    forward_action_id=inp.forward_action_id or "",
                )
            except TicketlessCallbackError as exc:
                raise EnvelopePlanError("invalid_args", str(exc))
            anchor = TicketlessAnchor(
                classification=callback_payload.classification,
                dispatch_decision=callback_payload.dispatch_decision,
            )
            return AnchorPlan(anchor=anchor, callback_payload=callback_payload)

        try:
            anchor = self._ops.normalize_anchor(
                inp.source,
                task_id=inp.task_id,
                comment_id=inp.comment_id,
                anchor_url=inp.anchor_url,
                issue=inp.issue,
                journal=inp.journal,
            )
        except AnchorError as exc:
            raise EnvelopePlanError("invalid_anchor", str(exc))
        return AnchorPlan(anchor=anchor)

    def plan_delivery_envelope(
        self,
        inp: HandoffCommandInput,
        *,
        anchor: NormalizedAnchor,
        callback_payload: TicketlessCallback | None,
        consultation_payload: TicketlessConsultation | None,
        work_intake_payload: TicketlessWorkIntake | None,
        repo_root: Path,
        resolved_target_repo: str | None,
        target_cwd: str,
        summary: str | None,
        receiver: str,
        kind: str | None,
    ) -> HandoffEnvelope:
        """Resolve the pre-send envelope (execution root / profile / contract / body / marker).

        Raises :class:`EnvelopePlanError` with the exact cumulative partial-state extras the
        original inline block emitted at each failing stage.
        """
        # Execution root / workdir propagation (Redmine #12098).
        execution_root = None
        if inp.workdir:
            workdir_abs = str(Path(inp.workdir).expanduser().resolve())
            if resolved_target_repo and resolved_target_repo != AUTO_TARGET_REPO:
                repo_anchor_abs = str(Path(resolved_target_repo).expanduser().resolve())
            else:
                repo_anchor_abs = self._ops.infer_repo_root(target_cwd) or None
            execution_root = self._ops.build_execution_root(
                workdir_abs, repo_root_abs=repo_anchor_abs
            )

        # Role profile (Redmine #12388 / #13477).
        role_profile_resolution = None
        if inp.role_profile:
            try:
                profile_fields = self._ops.resolve_handoff_profile_fields(
                    inp.role_profile,
                    inp.profile_field,
                    anchor.human_pointer(),
                    repo_root,
                )
                role_profile_resolution = self._ops.resolve_role_profile(
                    inp.role_profile, profile_fields
                )
            except RoleProfileError as exc:
                raise EnvelopePlanError(
                    "invalid_args",
                    str(exc),
                    outcome_extra={"execution_root": execution_root},
                )

        role_profile_contract = (
            role_profile_resolution.resolved_text if role_profile_resolution else None
        )

        # Transition role (Redmine #12706).
        transition_role_boundary = None
        if inp.transition_role:
            try:
                transition_role_boundary = self._ops.resolve_transition_role(
                    inp.transition_role
                )
            except TransitionRoleError as exc:
                raise EnvelopePlanError(
                    "invalid_args",
                    str(exc),
                    outcome_extra={
                        "execution_root": execution_root,
                        "role_profile": role_profile_resolution,
                    },
                    emit_extra={"role_profile_contract": role_profile_contract},
                )

        # Workflow contract (Redmine #12700).
        workflow_contract_bundle = None
        if inp.workflow_contract:
            try:
                workflow_contract_bundle = self._ops.resolve_workflow_contract(
                    inp.workflow_contract
                )
            except WorkflowContractError as exc:
                raise EnvelopePlanError(
                    "invalid_args",
                    str(exc),
                    outcome_extra={
                        "execution_root": execution_root,
                        "role_profile": role_profile_resolution,
                        "transition_role": transition_role_boundary,
                    },
                    emit_extra={"role_profile_contract": role_profile_contract},
                )

        # Notification body.
        try:
            body = self._ops.build_notification_body(
                anchor,
                kind,
                summary,
                receiver,
                execution_root=execution_root,
                role_profile=role_profile_resolution,
                transition_role=transition_role_boundary,
                workflow_contract=workflow_contract_bundle,
                ticketless_callback=callback_payload,
                ticketless_consultation=consultation_payload,
                ticketless_work_intake=work_intake_payload,
            )
        except AnchorError as exc:
            raise EnvelopePlanError(
                "invalid_args",
                str(exc),
                outcome_extra={
                    "execution_root": execution_root,
                    "role_profile": role_profile_resolution,
                    "transition_role": transition_role_boundary,
                    "workflow_contract": workflow_contract_bundle,
                    "ticketless_callback": callback_payload,
                    "ticketless_consultation": consultation_payload,
                    "ticketless_work_intake": work_intake_payload,
                },
                emit_extra={"role_profile_contract": role_profile_contract},
            )

        marker = self._ops.build_marker(anchor, kind, receiver)

        return HandoffEnvelope(
            execution_root=execution_root,
            role_profile_resolution=role_profile_resolution,
            role_profile_contract=role_profile_contract,
            transition_role_boundary=transition_role_boundary,
            workflow_contract_bundle=workflow_contract_bundle,
            body=body,
            marker=marker,
        )


__all__ = (
    "AnchorPlan",
    "EnvelopePlanError",
    "EnvelopePlannerOps",
    "HandoffEnvelope",
    "HandoffEnvelopePlanner",
    "LiveEnvelopePlannerOps",
)
