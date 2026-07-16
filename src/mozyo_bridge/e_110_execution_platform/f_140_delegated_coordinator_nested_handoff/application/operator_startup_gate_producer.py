"""Authoritative v3 gate producer (Redmine #13813 review j#79481 F2).

The Design Answer (j#79405 §A/§B) requires that a v3 operator-startup gate's runtime fields be
populated from ONE authoritative observation — the lane lifecycle record + the repo's current
provider binding + the exact declared :class:`ProcessGenerationPin` — never hand-assembled. This
module is that production producer (the analogue the reviewer found missing): given a single
lifecycle observation it resolves the workflow role's provider slot, reads
``(runtime_role, provider_id, assigned_name)`` from the declared pin, and the
``agent_generation`` / ``lane_revision`` / ``workspace_id`` from the same record, then builds the
``required`` gate. A drift (unbound role / no-or-many pins for the slot) fails closed with a typed
error — the producer never guesses a runtime role.

:func:`reissue_supersedes_note` renders the narrative pointer that names the superseded legacy
journal, so a fresh v3 gate re-issued after a legacy v1/v2 gate carries an explicit ``supersedes``
reference (no implicit backfill of the legacy gate's exact revision — j#79405 §B).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    GateClassification,
    GateTarget,
    OperatorStartupGate,
    OriginalRequest,
    build_required_gate,
    repo_identity_digest,
)


class GateProducerError(ValueError):
    """The authoritative observation could not yield an exact v3 target (fail-closed).

    Raised for a drift the producer must never paper over: an unbound workflow role, or a declared
    pin set that does not name exactly one pin for the resolved provider slot. The caller treats
    this as "no fresh gate" — it never fabricates a runtime role.
    """


def _provider_slot_pin(record: object, provider: str):
    """The single declared pin whose provider == ``provider`` (the workflow role's slot), or error.

    A missing / duplicate slot pin fails closed: the runtime identity must be observed, not guessed.
    """
    try:
        pins = [
            pin
            for pin in getattr(record, "declared_pins", ())
            if str(getattr(pin, "provider", "")).strip() == provider
        ]
    except Exception as exc:  # noqa: BLE001 - malformed declared slots -> fail closed
        raise GateProducerError(f"declared pins unreadable: {exc}") from exc
    if len(pins) != 1:
        raise GateProducerError(
            f"expected exactly one declared pin for provider slot {provider!r}, found {len(pins)}"
        )
    return pins[0]


def build_v3_target_from_observation(
    *,
    record: object,
    binding: object,
    workflow_role: str,
    execution_root: str,
) -> GateTarget:
    """Build the exact v3 :class:`GateTarget` from ONE authoritative lifecycle observation.

    The repo's ``binding`` resolves ``workflow_role`` to a provider; the record's declared pin for
    that provider slot supplies ``(runtime_role, provider_id, assigned_name)``; the record supplies
    ``workspace_id`` (its ``repo_workspace_id``, the registry authority), ``lane_id``,
    ``agent_generation`` (``lane_generation``) and ``lane_revision`` (``revision``). ``runtime_role``
    is the pin's ``role`` (the provider role), kept distinct from the workflow ``target_role``
    (j#79405 §A). ``execution_root`` is the repo-relative pointer the gate is pinned to. Any drift
    raises :class:`GateProducerError` (fail-closed) rather than fabricating a runtime role.
    """
    provider = None
    try:
        provider = binding.provider_for(workflow_role)
    except Exception as exc:  # noqa: BLE001 - unreadable binding -> fail closed
        raise GateProducerError(f"provider binding unreadable for {workflow_role!r}: {exc}") from exc
    provider = str(provider or "").strip()
    if not provider:
        raise GateProducerError(f"workflow role {workflow_role!r} is unbound in the provider binding")

    pin = _provider_slot_pin(record, provider)
    runtime_role = str(getattr(pin, "role", "")).strip()
    provider_id = str(getattr(pin, "provider", "")).strip()
    assigned_name = str(getattr(pin, "assigned_name", "")).strip()
    workspace_id = str(getattr(record, "repo_workspace_id", "")).strip()

    return GateTarget(
        workspace_id=workspace_id,
        repo_identity_digest=repo_identity_digest(workspace_id) if workspace_id else "",
        execution_root=execution_root,
        lane_id=str(getattr(record, "lane_id", "")).strip(),
        target_role=workflow_role,
        target_assigned_name=assigned_name,
        provider_id=provider_id,
        runtime_role=runtime_role,
        agent_generation=getattr(record, "lane_generation", None),
        lane_revision=getattr(record, "revision", None),
    )


def build_v3_required_gate_from_observation(
    *,
    record: object,
    binding: object,
    workflow_role: str,
    execution_root: str,
    gate_id: str,
    action_generation: int,
    original_request: OriginalRequest,
    classification: GateClassification,
) -> OperatorStartupGate:
    """Build a fresh ``required`` v3 gate from one authoritative observation (the production path).

    Populates the target via :func:`build_v3_target_from_observation` (runtime_role / provider_id /
    assigned_name from the declared pin; generation / revision / workspace from the same record) and
    wraps it in a ``required`` gate. Every field of the returned gate traces to the single
    observation — the caller records it durably and the owner approves it via the normal lattice.
    """
    target = build_v3_target_from_observation(
        record=record,
        binding=binding,
        workflow_role=workflow_role,
        execution_root=execution_root,
    )
    return build_required_gate(
        gate_id=gate_id,
        action_generation=action_generation,
        original_request=original_request,
        target=target,
        classification=classification,
    )


#: Narrative marker naming the legacy gate JOURNAL a fresh v3 gate supersedes (j#79405 §B). A
#: human pasteable pointer only — the exact revision / runtime role is re-observed, never
#: backfilled. The supersede MECHANISM is the append-only newest-wins chain; this line names the
#: exact superseded durable journal (review j#79524 F1) so the reissue is auditable and unique.
SUPERSEDES_MARKER = "supersedes-legacy-startup-gate"


def reissue_supersedes_note(*, superseded_journal: str) -> str:
    """The ``supersedes`` narrative line for a fresh v3 gate re-issued over a legacy v1/v2 gate.

    Names the legacy gate's durable ``journal_id`` explicitly so the reissue points at the EXACT
    superseded journal record (not a gate identity, which cannot disambiguate a duplicate /
    correction journal — review j#79524 F1), WITHOUT copying the legacy gate's approval / revision
    (a fresh owner approval + a freshly observed generation supersede it).
    """
    journal = str(superseded_journal or "").strip()
    if not journal:
        raise GateProducerError("reissue_supersedes_note requires a non-empty superseded journal id")
    return (
        f"[mozyo:{SUPERSEDES_MARKER}:journal={journal}] fresh v3 gate re-observed (no legacy backfill)"
    )


__all__ = (
    "GateProducerError",
    "build_v3_target_from_observation",
    "build_v3_required_gate_from_observation",
    "reissue_supersedes_note",
    "SUPERSEDES_MARKER",
)
