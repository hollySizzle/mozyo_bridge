"""Fresh terminal-bound generation boundary for ledger joins (#15227)."""

from dataclasses import dataclass
from typing import Mapping, Optional

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    AGENT_KEY_REVISION,
    _agent_locator,
    _norm,
    _norm_lane,
    terminal_identity_of_live_slot,
)


@dataclass(frozen=True)
class FreshGenerationBoundary:
    """Non-secret identity axes proven together from one fresh inventory snapshot."""

    assigned_name: str
    locator: str
    provider: str
    row_revision: str
    observed_at: str
    startup_action_id: str

    def matches_delivery(self, record) -> bool:
        observation = getattr(record, "queue_enter_observation", None)
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
            canonical_v2_generation_binding,
        )
        if not canonical_v2_generation_binding(observation):
            return False
        binding = (
            observation.get("gateway_binding")
            if isinstance(observation, Mapping)
            else None
        )
        if not isinstance(binding, Mapping):
            return False
        wanted = {
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "provider": self.provider,
            "row_revision": self.row_revision,
            "startup_action_id": self.startup_action_id,
            "attestation_observed_at": self.observed_at,
        }
        return all(
            value and _norm(binding.get(key)) == value
            for key, value in wanted.items()
        )


def correlated_delivery_markers(markers, ledger, boundary) -> tuple[str, ...]:
    """Return markers with one successful receipt for ``boundary``."""
    if boundary is None:
        return ()
    correlated = []
    for marker in markers:
        try:
            records = ledger.records_for_marker(marker)
        except Exception:  # noqa: BLE001 - unreadable ledger proves no correlation
            continue
        if any(
            _norm(record.notification_marker) == _norm(marker)
            and _norm(record.backend) == "herdr"
            and _norm(record.rail) == "queue_enter_rail"
            and _norm(record.status) == "sent"
            and _norm(record.reason) == "ok"
            and _norm(record.target) in (boundary.locator, boundary.assigned_name)
            and boundary.matches_delivery(record)
            for record in records
        ):
            correlated.append(marker)
    return tuple(correlated)


def fresh_attestation_identity(
    *, home, rows, assigned_name, workspace_id, role, lane
) -> Optional[FreshGenerationBoundary]:
    """Return a terminal-verified generation boundary from exactly one snapshot."""
    try:
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            if _norm(row.get(AGENT_KEY_NAME)) == _norm(assigned_name)
        ]
        if len(matches) != 1:
            return None
        locator = _norm(_agent_locator(matches[0]))
        revision_raw = matches[0].get(AGENT_KEY_REVISION)
        revision = "" if isinstance(revision_raw, bool) else _norm(revision_raw)
        terminal_id = terminal_identity_of_live_slot(assigned_name, locator, rows)
        if not locator or not revision or not terminal_id:
            return None
        record = HerdrIdentityAttestationStore(home=home).read(_norm(assigned_name))
        join = evaluate_attestation(
            record,
            live_locator=_norm(locator),
            live_terminal_id=terminal_id,
            expected_workspace_id=_norm(workspace_id),
            expected_role=_norm(role),
            expected_lane=_norm_lane(lane),
        )
    except Exception:  # noqa: BLE001 - every unreadable authority fails closed
        return None
    if not join.ok:
        return None
    from mozyo_bridge.core.state.herdr_launch_generation import verified_generation_token

    generation = verified_generation_token(
        home, assigned_name=assigned_name, workspace_id=workspace_id, role=role,
        lane_id=lane, locator=locator,
        live_terminal_id=terminal_id,
        norm=_norm, norm_lane=_norm_lane,
    )
    observed_at = _norm(getattr(record, "observed_at", ""))
    if not generation or not observed_at:
        return None
    return FreshGenerationBoundary(
        assigned_name=_norm(assigned_name), locator=locator, provider=_norm(role),
        row_revision=revision, observed_at=observed_at,
        startup_action_id=generation,
    )


__all__ = (
    "FreshGenerationBoundary", "correlated_delivery_markers",
    "fresh_attestation_identity",
)
