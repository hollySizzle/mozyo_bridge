"""Action-time exact target / generation resolver for the resume leg (Redmine #13813 F2).

The Design Answer (j#79332 §2) requires re-resolving the exact live target + generation at
action time — never from a saved projection / pane title / cache — and failing soft to
zero-send on any drift. This resolver, given the durable gate, validates the live world
against the gate's pins in order and returns ``None`` (the leg then feeds an ``unresolved``
observation to the orchestrator -> zero-send) unless every check passes:

1. the lane lifecycle record exists, is **active**, is bound to the gate's original issue,
   and its ``lane_generation`` equals the gate's pinned ``agent_generation`` (the exact
   generation gate; a recycled lane bumps ``lane_generation`` and fails closed);
2. exactly one declared pin has the gate's ``(role, provider, assigned_name)``
   ``stable_identity``;
3. exactly one live inventory row carries that ``assigned_name``, and its live locator
   equals the declared pin's ``locator`` (the ``ProcessGenerationPin`` live-generation
   discriminant) — missing / duplicate / foreign / recycled fail closed;
4. the herdr identity self-attestation for that live locator is ``ok`` for the gate's
   workspace / role / lane.

Only then does it bind the #13760 visible read to the live locator and return the live
:class:`GateTarget` (the gate's pinned identity with ``agent_generation = lane_generation``,
which the projection re-verifies against the pin). Every live read is an injectable seam so
the leg is proven through the production composition root with fakes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    GateTarget,
    OperatorStartupGate,
)

#: How many visible lines to read for the startup-clear classification (matches the pre-send
#: admission gate's read budget).
RESUME_READ_LINES = 60

#: Read the lane lifecycle record for (workspace_id, lane_id), or None. Injectable.
LifecycleGet = Callable[[str, str], Optional[object]]
#: List the live inventory rows. Injectable.
InventoryList = Callable[[Mapping[str, str]], Sequence[Mapping[str, object]]]
#: Read the identity attestation record for an assigned name, or None. Injectable.
AttestationRead = Callable[[str], Optional[object]]
#: Read a live pane's visible content (``locator, lines -> text``). Injectable.
CaptureVisible = Callable[[str, int], object]


def _default_lifecycle_get(workspace_id: str, lane_id: str) -> Optional[object]:
    try:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

        return LaneLifecycleStore().get(
            LaneLifecycleKey(repo_workspace_id=workspace_id, lane_id=lane_id)
        )
    except Exception:  # noqa: BLE001 - unreadable lifecycle store -> no target (zero-send)
        return None


def _default_inventory(env: Mapping[str, str]) -> Sequence[Mapping[str, object]]:
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
            list_herdr_agent_rows,
        )

        return list(list_herdr_agent_rows(env))
    except Exception:  # noqa: BLE001 - inventory failure -> empty (zero-send)
        return []


def _default_attestation_read(assigned_name: str) -> Optional[object]:
    try:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        return HerdrIdentityAttestationStore().read(assigned_name)
    except Exception:  # noqa: BLE001 - attestation store unreadable -> None (fails the ok gate)
        return None


def _default_capture(locator: str, lines: int) -> object:
    """Read the live locator's visible pane via the runtime transport binding (herdr-bound).

    Builds the same backend-neutral binding the send rail uses (``run_tmux`` / ``capture_pane``
    mapped onto the herdr ``read_pane`` visible source) and reads the pane. Fail-soft to ``""``
    on any error — the projection treats an unreadable pane as unreadable and zero-sends,
    never decaying to "clear".
    """
    try:
        from mozyo_bridge.application import commands
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
            resolve_runtime_transport_binding,
        )

        binding = resolve_runtime_transport_binding(
            tmux_run_tmux=commands.run_tmux,
            tmux_capture_pane=commands.capture_pane,
        )
        return binding.capture_pane(locator, lines)
    except Exception:  # noqa: BLE001 - unreadable pane / unconfigured backend -> "" (zero-send)
        return ""


#: Re-resolve the live action-time repo identity: ``(workspace_id, repo_identity_digest,
#: execution_root)``, or None (unresolved -> zero-send). Injectable.
WorkspaceResolve = Callable[[Mapping[str, str]], Optional["tuple[str, str, str]"]]


def _default_workspace_resolve(env: Mapping[str, str]) -> Optional["tuple[str, str, str]"]:
    """Re-resolve the live action-time repo identity, or None (fail-soft zero-send).

    Resolves the live workspace id from the action-time sender identity (registry authority),
    derives the pasteable ``repo_identity_digest`` over it (the documented convention — the
    digest is over the registry workspace id, so it is re-derivable without a checkout path),
    and reports the repo root ``execution_root`` ``"."``. Any resolution failure returns None
    so the resolver zero-sends rather than trust the gate's own (possibly forged) identity.
    """
    try:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
            resolve_sender_identity,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
            repo_identity_digest,
        )

        res = resolve_sender_identity(env, anchor_workspace_id=None)
        identity = getattr(res, "identity", None)
        if not getattr(res, "ok", False) or identity is None:
            return None
        workspace_id = str(getattr(identity, "workspace_id", "")).strip()
        if not workspace_id:
            return None
        return (workspace_id, repo_identity_digest(workspace_id), ".")
    except Exception:  # noqa: BLE001 - unresolved live identity -> None (zero-send)
        return None


def _live_row_for(
    rows: Sequence[Mapping[str, object]], assigned_name: str
) -> Optional[Mapping[str, object]]:
    """The single live inventory row for ``assigned_name``, or None (missing / duplicate)."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
    )

    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get(AGENT_KEY_NAME, "")).strip() == assigned_name
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ResumeTargetResolver:
    """Resolves the action-time live target for a gate, or None (fail-soft zero-send)."""

    env: Mapping[str, str]
    lifecycle_get: LifecycleGet = field(default=_default_lifecycle_get)
    inventory: InventoryList = field(default=_default_inventory)
    attestation_read: AttestationRead = field(default=_default_attestation_read)
    capture: CaptureVisible = field(default=_default_capture)
    workspace_resolve: WorkspaceResolve = field(default=_default_workspace_resolve)
    read_lines: int = RESUME_READ_LINES

    def resolve(self, gate: OperatorStartupGate, env: Mapping[str, str]) -> Optional[object]:
        target = gate.target

        # 1. Lane lifecycle record: exists, active, issue binding, exact generation AND exact
        #    CAS revision (review j#79366 F1 — a recycled/mutated lane fails closed).
        record = self.lifecycle_get(target.workspace_id, target.lane_id)
        if record is None:
            return None
        try:
            from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE
        except Exception:  # noqa: BLE001
            return None
        if getattr(record, "lane_disposition", None) != DISPOSITION_ACTIVE:
            return None
        if str(getattr(record, "issue_id", "")).strip() != gate.original_request.issue:
            return None
        if getattr(record, "lane_generation", None) != target.agent_generation:
            return None
        if getattr(record, "revision", None) != target.lane_revision:
            return None

        # 2. Exactly one declared pin with the gate's (role, provider, assigned_name).
        wanted = (target.target_role, target.provider_id, target.target_assigned_name)
        try:
            pins = [
                pin
                for pin in getattr(record, "declared_pins", ())
                if getattr(pin, "stable_identity", None) == wanted
            ]
        except Exception:  # noqa: BLE001 - malformed declared slots -> fail closed
            return None
        if len(pins) != 1:
            return None
        pin = pins[0]

        # 3. Exactly one live inventory row for the assigned name, and its FULL
        #    ProcessGenerationPin.match_key (role/provider/name/locator/runtime_revision) equals
        #    the declared pin's (review j#79366 F1 — a foreign provider / runtime-revision drift
        #    fails closed, not just a locator match).
        from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            _agent_locator,
        )

        row = _live_row_for(self.inventory(env), target.target_assigned_name)
        if row is None:
            return None
        live_locator = str(_agent_locator(row)).strip()
        if not live_locator:
            return None
        try:
            live_pin = ProcessGenerationPin(
                role=str(row.get("role") or target.target_role).strip(),
                provider=str(row.get("provider") or target.provider_id).strip(),
                assigned_name=target.target_assigned_name,
                locator=live_locator,
                runtime_revision=str(row.get("runtime_revision") or "").strip(),
            )
        except Exception:  # noqa: BLE001 - malformed live pin -> fail closed
            return None
        if live_pin.match_key != pin.match_key:
            return None

        # 4. Identity self-attestation for the live locator is ok.
        try:
            from mozyo_bridge.core.state.herdr_identity_attestation import evaluate_attestation

            join = evaluate_attestation(
                self.attestation_read(target.target_assigned_name),
                live_locator=live_locator,
                expected_workspace_id=target.workspace_id,
                expected_role=target.target_role,
                expected_lane=target.lane_id,
            )
        except Exception:  # noqa: BLE001 - attestation evaluation failure -> fail closed
            return None
        if not getattr(join, "ok", False):
            return None

        # 5. Re-resolve the live action-time repo identity and require it EXACTLY matches the
        #    gate's workspace_id / repo_identity_digest / execution_root (review j#79366 F1 — a
        #    wrong repo / execution root fails closed; the gate's own identity is not trusted).
        live_identity = self.workspace_resolve(env)
        if live_identity is None:
            return None
        live_workspace_id, live_digest, live_execution_root = live_identity
        if (
            live_workspace_id != target.workspace_id
            or live_digest != target.repo_identity_digest
            or live_execution_root != target.execution_root
        ):
            return None

        # Confirmed. Build the live target (gate identity + confirmed live generation +
        # revision) and bind the #13760 visible read to the live locator.
        live_target = dataclasses.replace(
            target, agent_generation=record.lane_generation, lane_revision=record.revision
        )
        captured_locator = live_locator
        read_lines = self.read_lines
        capture = self.capture

        def _read_visible() -> object:
            return capture(captured_locator, read_lines)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (
            RESOLUTION_RESOLVED,
            ObservedStartupTarget,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (
            ObservedTargetResolution,
        )

        return ObservedTargetResolution(
            observed=ObservedStartupTarget(resolution=RESOLUTION_RESOLVED, target=live_target),
            read_visible=_read_visible,
            profile_version=gate.classification.profile_version,
            classifier_version=gate.classification.classifier_version,
            locator=captured_locator,
        )


__all__ = (
    "RESUME_READ_LINES",
    "LifecycleGet",
    "InventoryList",
    "AttestationRead",
    "CaptureVisible",
    "WorkspaceResolve",
    "ResumeTargetResolver",
)
