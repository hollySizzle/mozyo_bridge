"""Live observation of a lane pair's post-launch startup self-attestation (Redmine #13847).

The create/start post-launch attestation gate (``pair_attestation_admission``) and the
hibernated exact-pair recovery both need the same live observation: for a lane's gateway +
worker slots, read each slot's #13637 startup self-attestation and join it against the
slot's live locator (``evaluate_attestation``), producing the pure
:class:`...pair_launch_attestation.SlotAttestation` pair the decision consumes.

Homed here (not inline in :mod:`sublane_actuator_herdr_ops`, already at the module-health
ceiling) as one cohesive IO helper the herdr ops delegates to, so the pure decision stays
in the domain and the subprocess/store IO stays in one place. It reads only — no launch, no
close, no store write.

Store home: the managed launch injects ``MOZYO_BRIDGE_HOME=<store_home>`` where
``store_home = str(mozyo_bridge_home())`` in the launching (coordinator) process
(``herdr_session_start``), so the wrapper's self-attestation lands in the SAME store this
reader resolves from ``mozyo_bridge_home()``. An injected ``attestation_store`` overrides it
for hermetic tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.core.state.herdr_identity_attestation import (
    ATTEST_ABSENT,
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
    SlotAttestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

#: A live-inventory lister: returns raw herdr ``agent list`` rows.
Lister = Callable[[], Sequence[Mapping[str, object]]]
#: Resolves ``(workspace_id, lane_id, {provider_role: (locator, placement_key)})`` for a
#: worktree — the herdr ops' own ``_resolve_lane_slots``.
SlotResolver = Callable[
    [str, Sequence[Mapping[str, object]], Tuple[str, str]],
    Tuple[str, str, "dict[str, tuple[str, str]]"],
]


def _slot_attestation(
    *,
    domain_role: str,
    provider: str,
    workspace_id: str,
    lane_id: str,
    locator: str,
    store: HerdrIdentityAttestationStore,
) -> SlotAttestation:
    """Read + join one slot's self-attestation into a pure :class:`SlotAttestation`.

    A slot with no live locator (not in the resolved inventory) is ``unobserved`` (a
    fail-closed absence — the pair never passes on a slot we could not even see). Otherwise
    the record is read for the slot's durable assigned name and joined against the live
    locator by the shared :func:`evaluate_attestation` policy (present + locator-matched =
    ``ATTEST_OK``; every other state fails closed).
    """
    if not locator:
        return SlotAttestation(
            role=domain_role,
            assigned_name="",
            ok=False,
            state="unobserved",
            detail="the slot has no live locator in the herdr inventory "
            "(not launched / already gone); its self-attestation cannot be confirmed",
            locator="",
        )
    assigned_name = encode_assigned_name(workspace_id, provider, lane_id)
    record = store.read(assigned_name)
    join = evaluate_attestation(
        record,
        live_locator=locator,
        expected_workspace_id=workspace_id,
        expected_role=provider,
        expected_lane=lane_id,
    )
    return SlotAttestation(
        role=domain_role,
        assigned_name=assigned_name,
        ok=join.ok,
        state=join.state,
        detail=join.reason,
        locator=locator,
    )


def observe_lane_pair_attestation(
    *,
    worktree_path: str,
    gateway_provider: str,
    worker_provider: str,
    list_rows: Lister,
    resolve_slots: SlotResolver,
    attestation_store: Optional[HerdrIdentityAttestationStore] = None,
) -> Tuple[SlotAttestation, SlotAttestation]:
    """Observe the ``(gateway, worker)`` slots' post-launch self-attestation (read-only).

    Resolves the lane's live slots (``resolve_slots`` — the herdr ops' binding-aware
    ``_resolve_lane_slots``, keyed on the exact ``(gateway_provider, worker_provider)`` pair
    the lane launched), then reads + joins each slot's self-attestation. Returns the pure
    ``(gateway, worker)`` :class:`SlotAttestation` pair for
    :func:`...pair_launch_attestation.decide_pair_launch_attestation`.

    Any read failure degrades to a fail-closed ``unobserved`` / ``absent`` slot, never a
    false attestation — the caller (the create gate / the recovery verify) blocks on it.
    """
    store = attestation_store or HerdrIdentityAttestationStore(home=Path(mozyo_bridge_home()))
    try:
        rows = list_rows()
    except Exception:  # noqa: BLE001 — a read failure fails closed, never a false pass.
        rows = ()
    workspace_id, lane_id, slots = resolve_slots(
        worktree_path, rows, (gateway_provider, worker_provider)
    )
    if not workspace_id:
        # No resolvable lane unit: both slots unobserved (fail-closed).
        empty = SlotAttestation(
            role=GATEWAY_ROLE,
            assigned_name="",
            ok=False,
            state=ATTEST_ABSENT,
            detail="the lane unit did not resolve from the worktree; no slot observable",
            locator="",
        )
        return empty, SlotAttestation(
            role=WORKER_ROLE,
            assigned_name="",
            ok=False,
            state=ATTEST_ABSENT,
            detail=empty.detail,
            locator="",
        )
    gateway = _slot_attestation(
        domain_role=GATEWAY_ROLE,
        provider=gateway_provider,
        workspace_id=workspace_id,
        lane_id=lane_id,
        locator=(slots.get(gateway_provider) or ("", ""))[0],
        store=store,
    )
    worker = _slot_attestation(
        domain_role=WORKER_ROLE,
        provider=worker_provider,
        workspace_id=workspace_id,
        lane_id=lane_id,
        locator=(slots.get(worker_provider) or ("", ""))[0],
        store=store,
    )
    return gateway, worker


__all__ = ("observe_lane_pair_attestation",)
