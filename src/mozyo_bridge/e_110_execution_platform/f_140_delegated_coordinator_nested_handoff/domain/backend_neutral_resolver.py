"""Backend-neutral live liveness / identity resolver (Redmine #13297).

The route-identity ledger
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger`,
spec ``vibes/docs/specs/route-identity-ledger.md``, Redmine #12553) fixed the
*tmux* identity contract: a ``pane_id`` is a cache / snapshot only and is never
the route authority; the authority is the stable tuple
``(workspace_id, lane_id, role, pane_name)`` re-matched against a live pane
inventory every handoff / callback. The herdr identity domain
(:mod:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity`,
Redmine #13247) fixed the same rule for a pure herdr session: the durable handle
is the restart-surviving *assigned name*, and the ``pane_id`` / locator recovered
from ``agent list`` is transient.

This module is the #13263 j#72594 "operational default 前の必須線": it lifts the
ledger's live resolver to a **backend-neutral abstraction** over *either* a tmux
pane inventory *or* a herdr ``agent list`` inventory, without teaching the ledger
about herdr and without duplicating its fail-closed logic.

Design
------
The ledger's :func:`resolve_route` already consumes a *normalized inventory row*
shape (``id`` / ``workspace_id`` / ``lane_id`` / ``agent_role`` / ``route_label``)
and owns the whole fail-closed outcome table
(:data:`RESOLVE_OK` / :data:`TARGET_UNAVAILABLE` / :data:`TARGET_AMBIGUOUS` /
:data:`TARGET_STALE` / :data:`ROUTE_LABEL_MISSING`). So the backend-neutral seam
is a thin **adapter**, not a fork:

- **tmux backend** — the live ``try_pane_lines`` rows are already that shape, so
  they pass through untouched. :func:`resolve_route_neutral` delegates directly
  to :func:`resolve_route`; the tmux path is byte-for-byte the existing behaviour
  (US 拘束: "tmux backend の既存挙動は byte 不変").
- **herdr backend** — :func:`herdr_inventory` decodes each ``agent list`` row's
  assigned name (#13247) into the ledger's row shape:

  - the decoded ``(workspace_id, lane_id, role)`` are the *identity source* (the
    assigned name / env self identity / live herdr inventory), **not** the tmux
    ``pane_id`` (US 拘束: "tmux ``pane_id`` を route authority にしない");
  - the canonical assigned name plays the ``pane_name`` / ``route_label`` role —
    the durable, restart-surviving stable label;
  - the transient herdr locator is carried as the ``id`` — cache / evidence only,
    exactly as ``last_seen_pane_id`` is on the tmux side;
  - a row whose name is not a mzb1 scheme name (a foreign / unmanaged herdr
    agent) is dropped — it occupies no managed identity slot.

The single backend branch lives here in :func:`neutral_inventory`; the ledger
stays backend-agnostic and its resolution semantics (ambiguity fail-closed,
stale-cache detection, `last_seen` as evidence-not-authority) are inherited
unchanged for both backends.

Locator-missing parity
----------------------
A decoded herdr agent can appear in a slot with **no** usable live locator (its
``agent list`` row carries no ``pane_id`` / ``pane`` / ``location``). The tmux
ledger never hits this — a ``try_pane_lines`` row always has a pane id — but the
herdr-identity domain treats it as a first-class fail-closed case
(``rebind_missing_locator``: "refuse to report success with a blank target").
:func:`resolve_route_neutral` preserves that fidelity on the herdr backend: a clean
single-identity match whose recovered locator is empty is downgraded from
:data:`RESOLVE_OK` to :data:`ROUTE_LOCATOR_MISSING` rather than resolving to a blank
target. It is kept distinct from :data:`TARGET_UNAVAILABLE` (the agent *is* live,
just unaddressable) — the same distinction the herdr rebind draws between
``missing_locator`` and ``not_found``.

This downgrade is **backend-conditional (herdr only)** (Redmine #13302): the tmux
backend never applies it, so ``resolve_route_neutral(tmux)`` is byte-for-byte the
ledger's :func:`resolve_route` even for a malformed tmux row whose ``id`` is blank.
This closes the #13297 j#72871 residual (a synthetic blank-id tmux row no longer
diverges from :func:`resolve_route`) and makes the US 拘束 "tmux backend の解決結果
は byte 不変" structural rather than dependent on the tmux input domain always
carrying a pane id.

Purity (mirrors the ledger + herdr-identity contracts): this module opens no
subprocess, reads no env, scans no tmux, sends nothing. It is total functions over
plain row mappings and returns the ledger's typed :class:`RouteResolution` /
:class:`RouteIdentity`. Recovering the live inventory (``agents targets`` for
tmux, ``agent list`` for herdr) and wiring the resolution into the live handoff
path is the actuator follow-up, exactly as the ledger itself is a staged seam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    PANE_KEY_ID,
    PANE_KEY_LANE,
    PANE_KEY_ROLE,
    PANE_KEY_ROUTE_LABEL,
    PANE_KEY_WORKSPACE,
    RESOLVE_OK,
    ROUTE_LOCATOR_MISSING,
    RouteIdentity,
    RouteResolution,
    enforce_route_target_guards,
    resolve_route,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    decode_assigned_name,
    encode_assigned_name,
)

# ---------------------------------------------------------------------------
# Backend selector tokens (closed set). The `terminal_transport.backend` flag is
# the same vocabulary used elsewhere; herdr stays default-off and is selected
# only when a caller passes it explicitly.
# ---------------------------------------------------------------------------
BACKEND_TMUX: str = "tmux"
BACKEND_HERDR: str = "herdr"

#: The liveness backends this neutral resolver can re-resolve against.
SUPPORTED_BACKENDS: frozenset[str] = frozenset({BACKEND_TMUX, BACKEND_HERDR})


class BackendNeutralResolverError(ValueError):
    """A backend-neutral resolution was requested for an unsupported backend.

    Inherits :class:`ValueError` for the fail-closed semantics shared by the
    sibling route-identity / herdr-identity domain errors. Raised only for an
    illegal backend token — resolution itself never raises, it returns a typed
    fail-closed :class:`RouteResolution`.
    """


# ---------------------------------------------------------------------------
# herdr `agent list` row -> ledger inventory row adapter (pure).
# ---------------------------------------------------------------------------
def herdr_agent_to_pane_row(row: Mapping[str, object]) -> Optional[dict[str, str]]:
    """Adapt one live herdr ``agent list`` row to the ledger's inventory row shape.

    The assigned name is the identity source: it is decoded (#13247) into the
    stable ``(workspace_id, lane_id, role)`` slot, and its *canonical* form is
    used as the ``route_label`` so a non-canonically-escaped-but-decodable name
    still maps to its canonical slot label (and two rows decoding to the same slot
    collide into a fail-closed ambiguity rather than silently picking one). The
    transient herdr locator rides on ``id`` — cache / evidence only, never the
    identity.

    Returns ``None`` for a row whose name is not a mzb1 scheme name (a foreign /
    unmanaged herdr agent): it occupies no managed identity slot, so it is dropped
    rather than misfiled into one. A decoded row with no usable locator is still
    emitted (with an empty ``id``) so the caller can distinguish "present but
    unaddressable" from "absent"; see :func:`resolve_route_neutral`.
    """
    if not isinstance(row, Mapping):
        return None
    decoded = decode_assigned_name(_norm(row.get(AGENT_KEY_NAME)))
    if not decoded.ok or decoded.identity is None:
        return None
    identity = decoded.identity
    return {
        PANE_KEY_ID: _agent_locator(row),
        PANE_KEY_WORKSPACE: identity.workspace_id,
        PANE_KEY_LANE: identity.lane_id,
        PANE_KEY_ROLE: identity.role,
        # Canonical assigned name = the durable stable label (herdr's pane_name).
        PANE_KEY_ROUTE_LABEL: encode_assigned_name(
            identity.workspace_id, identity.role, identity.lane_id
        ),
    }


def herdr_inventory(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    """Normalize a whole live herdr ``agent list`` snapshot into ledger rows.

    Foreign / unmanaged agents (names that are not mzb1 scheme names) are dropped,
    so the result carries only rows that occupy a managed identity slot.
    """
    normalized: list[dict[str, str]] = []
    for row in rows:
        pane_row = herdr_agent_to_pane_row(row)
        if pane_row is not None:
            normalized.append(pane_row)
    return normalized


def neutral_inventory(
    inventory: Sequence[Mapping[str, object]], *, backend: str
) -> list[dict[str, object]]:
    """Present a backend's live inventory in the ledger's neutral row shape.

    The sole backend branch: tmux rows pass through unchanged (they are already
    the ledger shape); herdr rows are adapted through :func:`herdr_inventory`. An
    unsupported backend fails closed with :class:`BackendNeutralResolverError`.
    """
    normalized_backend = _norm(backend)
    if normalized_backend == BACKEND_TMUX:
        return [dict(row) for row in inventory if isinstance(row, Mapping)]
    if normalized_backend == BACKEND_HERDR:
        return list(herdr_inventory(inventory))
    raise BackendNeutralResolverError(
        f"unsupported liveness backend {backend!r}; expected one of "
        f"{sorted(SUPPORTED_BACKENDS)}"
    )


# ---------------------------------------------------------------------------
# herdr identity slot -> ledger RouteIdentity bridge (pure).
# ---------------------------------------------------------------------------
def herdr_route_identity(
    *,
    workspace_id: str,
    role: str,
    route_id: str,
    lane_id: str = "",
    observed_at: str = "",
    last_seen_locator: str = "",
) -> RouteIdentity:
    """Build a ledger :class:`RouteIdentity` for a herdr identity slot.

    The stable ``pane_name`` is set to the slot's *deterministic* canonical
    assigned name (:func:`encode_assigned_name`), so a herdr route identity's
    stable label is, by construction, exactly the durable handle the live
    ``agent list`` rows carry — a caller cannot mint a herdr route identity whose
    label drifts from its slot. ``last_seen_locator`` is the transient herdr
    locator recorded as cache / evidence (the ``last_seen_pane_id`` slot), never
    authority. Fails closed via :class:`~...herdr_identity.HerdrIdentityError`
    when a required component is empty (an empty ``workspace_id`` / ``role``
    cannot mint a durable handle).
    """
    pane_name = encode_assigned_name(workspace_id, role, lane_id)
    return RouteIdentity(
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role,
        pane_name=pane_name,
        route_id=route_id,
        observed_at=observed_at,
        last_seen_pane_id=last_seen_locator,
    )


# ---------------------------------------------------------------------------
# Backend-neutral re-resolution (pure).
# ---------------------------------------------------------------------------
def resolve_route_neutral(
    identity: RouteIdentity,
    inventory: Sequence[Mapping[str, object]],
    *,
    backend: str,
) -> RouteResolution:
    """Re-resolve one stable route identity against a backend's live inventory.

    Normalizes the inventory for the selected backend
    (:func:`neutral_inventory`) and delegates to the ledger's
    :func:`resolve_route`, so both backends inherit the identical fail-closed
    outcome table (ambiguity fail-closed, stale-cache detection, `last_seen` used
    only as evidence). For the herdr backend a clean single match whose recovered
    live locator is empty is downgraded to :data:`ROUTE_LOCATOR_MISSING` rather
    than resolving to a blank target (parity with the herdr rebind's
    ``missing_locator``); the tmux path never triggers this and is byte-for-byte
    the existing behaviour.

    An unsupported ``backend`` raises :class:`BackendNeutralResolverError`; every
    other outcome is a typed :class:`RouteResolution`.

    The ``route_locator_missing`` downgrade is **backend-conditional (herdr only)**
    (Redmine #13302, closing the #13297 j#72871 residual): the tmux backend never
    downgrades, so ``resolve_route_neutral(tmux)`` is byte-for-byte the ledger's
    :func:`resolve_route` even for a malformed tmux row whose ``id`` is blank. A
    blank live locator is a first-class fail-closed case only on the herdr side,
    where a decoded ``agent list`` row can genuinely lack a locator; a tmux
    ``try_pane_lines`` row always carries a pane id, so gating the downgrade to
    herdr changes no real tmux input and makes the "tmux 解決結果 byte 不変" 拘束
    structural rather than input-domain-dependent.
    """
    normalized_backend = _norm(backend)
    normalized = neutral_inventory(inventory, backend=backend)
    resolution = resolve_route(identity, normalized)
    if (
        normalized_backend == BACKEND_HERDR
        and resolution.status == RESOLVE_OK
        and not _norm(resolution.resolved_pane_id)
    ):
        refreshed_identity = resolution.identity
        if refreshed_identity is not None:
            # Roll the cache back off the (blank) locator: a fail-closed result
            # must not advance `last_seen_pane_id` to an empty target.
            refreshed_identity = replace(
                refreshed_identity, last_seen_pane_id=identity.last_seen_pane_id
            )
        return RouteResolution(
            status=ROUTE_LOCATOR_MISSING,
            route_id=identity.route_id,
            identity=refreshed_identity,
            considered=resolution.considered,
            detail=(
                "one live unit matched the stable identity but carries no usable "
                "live locator; refuse to route to a blank target"
            ),
        )
    return resolution


def resolve_for_route_target_neutral(
    target_token: str,
    identity: RouteIdentity,
    inventory: Sequence[Mapping[str, object]],
    *,
    backend: str,
    cross_project: bool = False,
) -> RouteResolution:
    """Re-resolve a #12550 logical route target against a backend's live inventory.

    The backend-neutral analogue of the ledger's
    :func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger.resolve_for_route_target`
    (Redmine #13302): it runs the identical fail-closed role / cross-project guards
    (:func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger.enforce_route_target_guards`)
    and then delegates to :func:`resolve_route_neutral` for the selected backend.

    For ``backend=tmux`` this is byte-for-byte the ledger's
    :func:`resolve_for_route_target` — the same guards, and (since the
    ``route_locator_missing`` downgrade is herdr-only) the same
    :func:`resolve_route` outcome. For ``backend=herdr`` the target is re-resolved
    against a live ``agent list`` inventory through the herdr adapter. This is the
    live-executor wiring seam: the executor threads its ``ExecutionContext.backend``
    here so a single re-resolution call site serves both backends without the
    executor learning either row shape. An unsupported ``backend`` fails closed with
    :class:`BackendNeutralResolverError`; a guard violation raises
    :class:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner.DelegationRoutePlanError`.
    """
    enforce_route_target_guards(target_token, identity, cross_project=cross_project)
    return resolve_route_neutral(identity, inventory, backend=backend)


__all__ = (
    "BACKEND_HERDR",
    "BACKEND_TMUX",
    "SUPPORTED_BACKENDS",
    "BackendNeutralResolverError",
    "herdr_agent_to_pane_row",
    "herdr_inventory",
    "herdr_route_identity",
    "neutral_inventory",
    "resolve_route_neutral",
    "resolve_for_route_target_neutral",
)
