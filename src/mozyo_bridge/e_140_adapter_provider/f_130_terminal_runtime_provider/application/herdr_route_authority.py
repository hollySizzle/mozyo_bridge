"""herdr real-send route-authority convergence (Redmine #13305).

The real ``handoff send`` path (:func:`~mozyo_bridge.application.commands.orchestrate_handoff`)
used to resolve its herdr target through the **lane-less** projection
:func:`~...domain.herdr_target_resolution.resolve_herdr_target`, whose match key is
``(workspace_id, provider role)`` — lane deliberately excluded (herdr-native-identity
spec §3, "multi-lane cross routing は後続 US"). Meanwhile the tmux path resolves through
the route-identity ledger / backend-neutral resolver, whose authority is the stable
tuple ``(workspace_id, lane_id, role, pane_name)``. Two parallel authorities, and the
lane-less one is non-unique the moment two lanes run the same role concurrently — the
operational norm today (1–5 lanes).

This module is the #13305 convergence (design record #13305 j#73008): it resolves the
herdr real-send target through the **single** backend-neutral route authority, so both
backends share one contract. The auditor ruling, restated:

- **route authority is lane-in-match.** The herdr send re-resolves against the ledger's
  ``(workspace_id, lane_id, role, pane_name)`` via
  :func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver.resolve_route_neutral`
  (``backend=herdr``): the live ``agent list`` rows are decoded (#13247) into the ledger
  row shape, the canonical assigned name plays ``pane_name`` (the durable stable label),
  and the transient herdr locator is cache / evidence only — never the authority.
- **lane is derived deterministically, never scanned.** A lane-unspecified send derives
  a single lane first (:func:`~...domain.herdr_target_resolution.derive_target_lane`,
  precedence explicit > sender same-lane > coordinator default > legacy default) and
  re-resolves *that* slot. A slot that is not live fails closed
  (``target_unavailable`` / ``target_ambiguous`` / ``route_locator_missing``) rather than
  falling back to an all-lane ``(ws, role)`` scan.
- **fail-closed vocabulary is the #13302 ledger vocabulary.** No new reason token is
  minted (design record: re-consult if one is ever required); the resolution projects the
  ledger's :data:`RESOLVE_OK` / fail-closed statuses.

The lane-less :func:`resolve_herdr_target` is retained as a **legacy compatibility
adapter** (auditor ruling #3) for the translator fallback in
:mod:`mozyo_bridge.application.handoff_transport_wiring`; it is no longer the real-send
route authority.

Purity: this module is a pure projection over the sender identity, the receiver label,
and the live ``agent list`` rows the caller supplies. It opens no subprocess, reads no
env, scans no tmux; recovering the live inventory and env is the application caller's job
(:mod:`...application.herdr_send_entry`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (
    BACKEND_HERDR,
    herdr_route_identity,
    resolve_route_neutral,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    RESOLVE_OK,
    TARGET_UNAVAILABLE,
    RouteIdentity,
    RouteResolution,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    HerdrIdentityError,
    _norm,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    LaneDerivation,
    SenderIdentity,
    derive_target_lane,
    resolve_target_role,
)


@dataclass(frozen=True)
class HerdrRouteAuthorityResolution:
    """The fail-closed result of resolving a herdr send target via the route authority.

    On success (:attr:`ok`) :attr:`locator` is the **transient** live herdr locator to
    address the target now, :attr:`identity` is the ledger :class:`RouteIdentity` refreshed
    from the live inventory (its ``pane_name`` is the canonical assigned name), and
    :attr:`status` is :data:`RESOLVE_OK`. On failure :attr:`reason` is the token to project
    — a pre-resolution role failure (``unknown_receiver`` / ``coordinator_binding_unresolved``)
    or a ledger fail-closed status (``target_unavailable`` / ``target_ambiguous`` /
    ``route_locator_missing`` / ...) — and the locator / identity are empty. The lane the
    send resolved against and *why* (:attr:`lane`, :attr:`lane_basis`) are always recorded
    for audit, even on failure.
    """

    ok: bool
    status: str
    reason: Optional[str] = None
    locator: str = ""
    identity: Optional[RouteIdentity] = None
    lane: str = ""
    lane_basis: str = ""
    considered: int = 0
    detail: str = ""

    @property
    def is_fail(self) -> bool:
        return not self.ok

    @property
    def assigned_name(self) -> str:
        """The target's canonical durable herdr name (the ledger ``pane_name``), or ``""``."""
        return self.identity.pane_name if self.identity is not None else ""

    @classmethod
    def role_failure(
        cls, reason: str, detail: str, *, derivation: Optional[LaneDerivation] = None
    ) -> "HerdrRouteAuthorityResolution":
        return cls(
            ok=False,
            status=reason,
            reason=reason,
            lane=derivation.lane if derivation else "",
            lane_basis=derivation.basis if derivation else "",
            detail=detail,
        )

    @classmethod
    def from_route_resolution(
        cls, resolution: RouteResolution, derivation: LaneDerivation
    ) -> "HerdrRouteAuthorityResolution":
        if resolution.status == RESOLVE_OK:
            return cls(
                ok=True,
                status=RESOLVE_OK,
                locator=resolution.resolved_pane_id,
                identity=resolution.identity,
                lane=derivation.lane,
                lane_basis=derivation.basis,
                considered=resolution.considered,
                detail=resolution.detail,
            )
        return cls(
            ok=False,
            status=resolution.status,
            reason=resolution.status,
            lane=derivation.lane,
            lane_basis=derivation.basis,
            considered=resolution.considered,
            detail=resolution.detail,
        )


def resolve_herdr_route_target(
    receiver: object,
    sender: SenderIdentity,
    rows: Sequence[Mapping[str, object]],
    *,
    coordinator_provider: Optional[str],
    explicit_lane: object = None,
) -> HerdrRouteAuthorityResolution:
    """Resolve a herdr send target through the single backend-neutral route authority.

    Steps (Redmine #13305 j#73008):

    1. resolve the receiver's target provider role
       (:func:`~...domain.herdr_target_resolution.resolve_target_role`; a
       ``coordinator`` receiver resolves through the role->provider binding). A role
       failure (``unknown_receiver`` / ``coordinator_binding_unresolved``) fails closed
       before any lane derivation or live match;
    2. derive the single target lane deterministically
       (:func:`~...domain.herdr_target_resolution.derive_target_lane`);
    3. mint the ledger :class:`RouteIdentity` for that slot — the canonical assigned name
       ``encode_assigned_name(workspace_id, role, lane)`` is both the stable ``pane_name``
       and the ``route_id`` (a herdr slot's durable, deterministic handle);
    4. re-resolve that identity against the live ``agent list`` rows through
       :func:`resolve_route_neutral` (``backend=herdr``): a single live match returns the
       transient locator; zero / many / locator-missing fail closed with the ledger
       vocabulary; a decoded slot in a *different* lane is simply not matched (no all-lane
       scan).

    Pure and fail-closed: never raises for a routing outcome (that rides in the returned
    result); a malformed slot that cannot mint a durable name (an empty attested workspace
    / role — unreachable for an attested sender + resolved role) is defensively folded into
    a ``target_unavailable`` fail-closed result rather than raised.
    """
    role_res = resolve_target_role(receiver, coordinator_provider=coordinator_provider)
    if not role_res.ok:
        assert role_res.reason is not None
        return HerdrRouteAuthorityResolution.role_failure(
            role_res.reason, role_res.detail
        )
    target_role = role_res.role

    derivation = derive_target_lane(receiver, sender, explicit_lane=explicit_lane)

    try:
        canonical = encode_assigned_name(
            sender.workspace_id, target_role, derivation.lane
        )
        identity = herdr_route_identity(
            workspace_id=sender.workspace_id,
            role=target_role,
            route_id=canonical,
            lane_id=derivation.lane,
            last_seen_locator="",
        )
    except HerdrIdentityError as exc:  # defensive: attested sender + resolved role
        return HerdrRouteAuthorityResolution(
            ok=False,
            status=TARGET_UNAVAILABLE,
            reason=TARGET_UNAVAILABLE,
            lane=derivation.lane,
            lane_basis=derivation.basis,
            detail=f"herdr route identity could not be minted for the slot: {exc}",
        )

    resolution = resolve_route_neutral(identity, rows, backend=BACKEND_HERDR)
    return HerdrRouteAuthorityResolution.from_route_resolution(resolution, derivation)


__all__ = (
    "HerdrRouteAuthorityResolution",
    "resolve_herdr_route_target",
)
