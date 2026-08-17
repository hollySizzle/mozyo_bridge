"""Typed application service for `sublane create/start` actuation (Redmine #15152).

The shared body behind the two mutating sublane entries — the CLI's
``cmd_sublane_start`` (``--dry-run`` / ``--execute``) and the local MCP
``sublane_start`` tool. #15149 established the boundary rule ("judgement in one
place, two entries") for the handoff family; this module applies it to the
sublane actuation surface, because ``SublaneActuateUseCase.run`` alone is NOT the
full gate set: three admissions lived in the CLI handler in front of it —

1. the #13002 work-unit granularity resolution (fail-closed on a
   present-but-broken repo-local config);
2. the #15146 delegated_coordinator **parent-authority admission** — a
   ``delegated_coordinator`` lane asserts a parent project gateway, and creation
   is refused with a typed verdict before any worktree / pane / dispatch side
   effect when that parent is not durably declared AND verified;
3. the #13569 provider launchability preflight (an unbound role or an
   unlaunchable provider refuses with zero side effects).

A second caller composing ``SublaneActuateUseCase`` directly would skip all
three — exactly the plan/execute drift #14224 was filed over, and for gate 2 the
false-topology condition #15146 was filed over. So the admissions and the use
case run live HERE, once, and both entries call :func:`run_sublane_start`.

Typed in, typed out: no ``argparse.Namespace``, no stdout/stderr writes, no
prose parsing. Admission refusals come back as a closed ``reason`` token plus
the same fixed message the CLI prints; the actuation outcome is the unchanged
:class:`~...domain.sublane_actuation.SublaneActuationOutcome`. This service
grants no authority — every gate keeps its own decision, and the MCP entry can
reach no gate the CLI cannot and skip none the CLI runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneActuationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneCreateRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (  # noqa: E501
    FillDecisionInputs,
)

#: The repo-local config declared a work-unit granularity but could not be read.
REFUSAL_INVALID_REPO_CONFIG = "invalid_repo_local_config"
#: A lane role's provider binding did not resolve (Redmine #13569).
REFUSAL_PROVIDER_UNRESOLVED = "provider_unresolved"
#: A lane role's bound provider is not a launchable agent provider (#13569).
REFUSAL_PROVIDER_NOT_LAUNCHABLE = "provider_not_launchable"

#: Admission refusal tokens minted by this service itself. The parent-authority
#: tokens (``parent_*``) come from the #15146 domain vocabulary and are NOT
#: restated here — the verdict's own ``reason`` is forwarded.
SERVICE_REFUSAL_REASONS = frozenset(
    {
        REFUSAL_INVALID_REPO_CONFIG,
        REFUSAL_PROVIDER_UNRESOLVED,
        REFUSAL_PROVIDER_NOT_LAUNCHABLE,
    }
)

STATUS_COMPLETED = "completed"
STATUS_REFUSED = "refused"


@dataclass(frozen=True)
class SublaneStartCommand:
    """One typed sublane create/start invocation (dry-run or execute)."""

    repo_root: Path
    issue: str
    lane_label: str
    branch: str = ""
    worktree_path: str = ""
    journal: Optional[str] = None
    upstream_coordinator: Optional[str] = None
    #: Explicit work-unit override; ``None`` resolves the repo-local config
    #: (`work_unit.granularity`), else the governed ``user_story`` default —
    #: exactly the CLI's flag > config > default precedence.
    work_unit: Optional[str] = None
    work_unit_decision_anchor: Optional[str] = None
    leaf_standalone: bool = False
    base_ref: Optional[str] = None
    lane_kind: str = ""
    execute: bool = False
    dispatch: bool = True
    target_repo: str = "auto"
    gateway_ready_timeout: float = 10.0
    fill_inputs: Optional[FillDecisionInputs] = None
    override_fill_stop: Optional[str] = None
    #: Confine composed sub-CLI progress text to stderr (the CLI's --json mode;
    #: the MCP entry always sets it, because its stdout carries MCP frames only).
    quiet_stdout: bool = False
    #: Test seam: the composition-injected agent-provider snapshot (#13569
    #: R2-F2b / R3-F1). ``None`` resolves the built-in snapshot.
    provider_snapshot: Optional[object] = None


@dataclass(frozen=True)
class SublaneStartRefusal:
    """A typed admission refusal decided before ANY side effect."""

    reason: str
    message: str


@dataclass(frozen=True)
class SublaneStartResult:
    """The typed result of one sublane create/start run.

    Exactly one of ``refusal`` / ``outcome`` is set. ``exit_code`` carries the
    CLI contract (1 for a refusal or a blocked outcome, else 0) so the CLI
    adapter maps without re-deciding.
    """

    status: str
    exit_code: int
    refusal: Optional[SublaneStartRefusal] = None
    outcome: Optional[SublaneActuationOutcome] = None

    @property
    def refused(self) -> bool:
        return self.status == STATUS_REFUSED


def provider_preflight_refusal(
    repo_root: Path, snapshot: Optional[object] = None
) -> Optional[SublaneStartRefusal]:
    """The #13569 launchability preflight as a typed refusal, or ``None``.

    Same decision, same fixed wording as the CLI's historical stderr message;
    the CLI adapter prints ``refusal.message`` verbatim so the operator-facing
    text is unchanged.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )

    if snapshot is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E501
            BUILTIN_AGENT_PROVIDER_SNAPSHOT,
        )

        snapshot = BUILTIN_AGENT_PROVIDER_SNAPSHOT
    root = str(repo_root)
    try:
        providers = (
            ("gateway", resolve_gateway_provider(root)),
            ("worker", resolve_worker_provider(root)),
        )
    except WorkflowProviderUnresolved as exc:
        return SublaneStartRefusal(
            reason=REFUSAL_PROVIDER_UNRESOLVED,
            message=f"sublane start refused: {exc}; no lane was created.",
        )
    for role, provider in providers:
        if not snapshot.is_launchable(provider):
            return SublaneStartRefusal(
                reason=REFUSAL_PROVIDER_NOT_LAUNCHABLE,
                message=(
                    f"sublane start refused: the {role} provider {provider!r} is "
                    f"not a launchable agent provider (unknown, or a "
                    f"non-interactive protocol / missing capability); no lane "
                    f"was created."
                ),
            )
    return None


def _admission_refusal(command: SublaneStartCommand) -> tuple[
    Optional[SublaneStartRefusal], Optional[str], Optional[str]
]:
    """Run the three pre-actuation admissions in the CLI's exact order.

    Returns ``(refusal, work_unit, decision_anchor)``; a non-None refusal means
    nothing after it ran and no side effect occurred.
    """
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
        RepoLocalConfigError,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
        resolve_work_unit_fields,
    )

    # 1. Work-unit granularity (#13002): the ONE shared precedence statement
    #    (flag > repo-local config > default), failing closed on a
    #    present-but-broken config.
    try:
        work_unit, decision_anchor = resolve_work_unit_fields(
            command.work_unit, command.work_unit_decision_anchor, command.repo_root
        )
    except RepoLocalConfigError as exc:
        return (
            SublaneStartRefusal(
                reason=REFUSAL_INVALID_REPO_CONFIG,
                message=f"invalid repo-local config: {exc}",
            ),
            None,
            None,
        )

    # 2. Parent-authority admission (#15146): a delegated_coordinator lane
    #    asserts a parent project gateway; refuse with the typed verdict before
    #    the provider preflight and before any worktree / pane / dispatch side
    #    effect. Same decision the plan-only surface runs.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
        delegated_parent_authority_verdict,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E501
        parent_authority_refusal_text,
    )

    verdict = delegated_parent_authority_verdict(command.repo_root, command.lane_kind)
    if verdict is not None and not verdict.ok:
        return (
            SublaneStartRefusal(
                reason=verdict.reason,
                message=parent_authority_refusal_text(verdict),
            ),
            None,
            None,
        )

    # 3. Provider binding / launchability preflight (#13569).
    refusal = provider_preflight_refusal(
        command.repo_root, snapshot=command.provider_snapshot
    )
    if refusal is not None:
        return refusal, None, None
    return None, work_unit, decision_anchor


def run_sublane_start(command: SublaneStartCommand) -> SublaneStartResult:
    """Run one sublane create/start (dry-run or execute) through every gate.

    Admission order and refusal wording are the CLI's, byte-for-byte; the use
    case then applies its own typed gates (identity, work-unit decision, anchor,
    fill admission, sender attestation, launch decision, pre-mutation admission)
    exactly as before. ``execute=False`` is the side-effect-free dry-run plan.
    """
    refusal, work_unit, decision_anchor = _admission_refusal(command)
    if refusal is not None:
        return SublaneStartResult(
            status=STATUS_REFUSED, exit_code=1, refusal=refusal
        )

    request = SublaneCreateRequest(
        issue=command.issue,
        lane_label=command.lane_label,
        branch=command.branch,
        worktree_path=command.worktree_path,
        journal=command.journal,
        upstream_coordinator=command.upstream_coordinator,
        work_unit=work_unit or "",
        work_unit_decision_anchor=decision_anchor,
        leaf_standalone=command.leaf_standalone,
        base_ref=command.base_ref,
        lane_kind=command.lane_kind,
    )

    # Resolved as MODULE ATTRIBUTES at call time — the same seams the CLI entry
    # historically exposed (`sublane_actuator._resolve_sublane_ops` /
    # `sublane_actuator.SublaneActuateUseCase`), so the existing monkeypatch /
    # capture tests keep intercepting for both entries (the
    # `LiveHandoffApplicationOps` precedent from #15149).
    import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as sublane_actuator  # noqa: E501
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
        DEFAULT_GATEWAY_READY_INTERVAL_SECONDS,
    )

    # Backend-selected actuation adapter (#13331): the existing production
    # selector stays the ONE selection point (its `args` parameter is unused by
    # the selection and is passed as None from this typed entry).
    ops = sublane_actuator._resolve_sublane_ops(
        None,
        repo_root=command.repo_root,
        request=request,
        quiet_stdout=command.quiet_stdout,
    )

    interval = DEFAULT_GATEWAY_READY_INTERVAL_SECONDS
    ready_timeout = float(command.gateway_ready_timeout or 0.0)
    ready_probes = 0 if ready_timeout <= 0 else max(1, round(ready_timeout / interval))
    use_case = sublane_actuator.SublaneActuateUseCase(
        ops,
        gateway_ready_probes=ready_probes,
        gateway_ready_interval_seconds=interval,
    )
    outcome = use_case.run(
        request,
        execute=command.execute,
        dispatch=command.dispatch,
        target_repo=command.target_repo or "auto",
        fill_inputs=command.fill_inputs,
        override_fill_stop=command.override_fill_stop,
    )
    return SublaneStartResult(
        status=STATUS_COMPLETED,
        exit_code=1 if outcome.is_blocked else 0,
        outcome=outcome,
    )


__all__ = (
    "REFUSAL_INVALID_REPO_CONFIG",
    "REFUSAL_PROVIDER_NOT_LAUNCHABLE",
    "REFUSAL_PROVIDER_UNRESOLVED",
    "SERVICE_REFUSAL_REASONS",
    "STATUS_COMPLETED",
    "STATUS_REFUSED",
    "SublaneStartCommand",
    "SublaneStartRefusal",
    "SublaneStartResult",
    "provider_preflight_refusal",
    "run_sublane_start",
)
