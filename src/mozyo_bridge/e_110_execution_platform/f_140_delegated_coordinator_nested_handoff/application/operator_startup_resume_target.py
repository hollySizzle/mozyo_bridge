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


def _live_locator_for(rows: Sequence[Mapping[str, object]], assigned_name: str) -> Optional[str]:
    """The single live locator for ``assigned_name``, or None (missing / duplicate -> fail)."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
        _agent_locator,
    )

    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get(AGENT_KEY_NAME, "")).strip() == assigned_name
    ]
    if len(matches) != 1:
        return None
    locator = str(_agent_locator(matches[0])).strip()
    return locator or None


@dataclass(frozen=True)
class ResumeTargetResolver:
    """Resolves the action-time live target for a gate, or None (fail-soft zero-send)."""

    env: Mapping[str, str]
    lifecycle_get: LifecycleGet = field(default=_default_lifecycle_get)
    inventory: InventoryList = field(default=_default_inventory)
    attestation_read: AttestationRead = field(default=_default_attestation_read)
    capture: CaptureVisible = field(default=_default_capture)
    read_lines: int = RESUME_READ_LINES

    def resolve(self, gate: OperatorStartupGate, env: Mapping[str, str]) -> Optional[object]:
        target = gate.target

        # 1. Lane lifecycle record: exists, active, issue binding, exact generation.
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

        # 3. Exactly one live inventory row for the assigned name; its locator matches the pin.
        live_locator = _live_locator_for(self.inventory(env), target.target_assigned_name)
        if live_locator is None or live_locator != str(getattr(pin, "locator", "")).strip():
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

        # Confirmed. Build the live target (gate identity + confirmed live generation) and
        # bind the #13760 visible read to the live locator.
        live_target = dataclasses.replace(target, agent_generation=record.lane_generation)
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
    "ResumeTargetResolver",
)
