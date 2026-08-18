"""Parent-authority admission for a delegated_coordinator lane (pure, Redmine #15146).

Measured on the server-management lane (0.20.1 / Herdr 0.8.0, #15146): with a durable
role binding declaring only ``coordinator`` — no ``project_gateway`` anywhere —
``sublane create --lane-kind delegated_coordinator`` succeeded, and the resulting Unit
then projected three different roles on three surfaces (handoff:
``implementation_gateway``, route-plan: ``delegated_coordinator``, Unit board:
``unknown``/``missing``). The three-tier geometry LOOKED established while its parent
tier did not exist, and nothing failed closed.

``delegated_coordinator`` is a *geometry assertion*: it claims this lane answers to a
parent project gateway. This module is the admission that makes the assertion earn
itself — the parent must be durably DECLARED (a ``project_gateway`` role binding) and
VERIFIED (that binding's derived lane owns an active lifecycle owner row, which only
``sublane declare-project-gateway`` writes, from a live attested pair). Every gap is a
typed refusal decided BEFORE any worktree / pair / dispatch side effect.

Single-workspace setups are not squeezed out (close condition 3, then Redmine #15700):
the default coordinator and plain ``implementation`` lanes carry no parent claim and
are untouched by this admission. Since #15700 a workspace that declares NO
``project_gateway`` binding at all — a single-workspace topology, where no parent tier
was ever claimed — may still create a delegated_coordinator lane by hanging it from
the workspace's own default-lane coordinator: the declared ``coordinator`` role must
resolve AND exactly one live, attested default-lane occupant must exist (the same
exactly-one policy the coordinator proxy rail enforces). The moment a
``project_gateway`` binding IS declared, the monorepo three-tier claim applies
unchanged — a declared topology never silently falls back to the coordinator branch.

Pure: bindings arrive parsed; owner-row existence and the coordinator-parent facts are
injected — the application layer owns every read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    ParsedRoleBindings,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E501
    ROLE_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING,
    TARGET_MISSING,
    TARGET_OK,
    TARGET_UNATTESTED,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (  # noqa: E501
    ROLE_PROJECT_GATEWAY,
)

#: The lane kind whose creation asserts a parent project gateway.
DELEGATED_COORDINATOR_LANE_KIND = "delegated_coordinator"

# --- parent kinds (which branch admitted / refused; #15700, never silent) --- #

#: The monorepo three-tier parent: a declared + verified project gateway.
PARENT_KIND_PROJECT_GATEWAY = "project_gateway"
#: The single-workspace parent: the workspace's own default-lane coordinator.
PARENT_KIND_DEFAULT_COORDINATOR = "default_lane_coordinator"

# --- refusal vocabulary (closed) ------------------------------------------- #

#: The binding declaration exists but cannot be trusted (unreadable / malformed).
PARENT_BINDINGS_INVALID = "parent_authority_bindings_invalid"
#: A ``project_gateway`` topology is declared but this reason is historical: since
#: #15700 an entirely undeclared workspace routes to the coordinator branch instead.
#: The token is kept for the declared-gateway vocabulary's completeness and for
#: durable records written before #15700.
PARENT_GATEWAY_UNDECLARED = "parent_gateway_binding_missing"
#: Declared, but no declared gateway lane owns an ACTIVE lifecycle owner row.
PARENT_GATEWAY_UNVERIFIED = "parent_gateway_owner_row_missing"
#: The workspace identity needed to scope the owner-row lookup did not resolve.
PARENT_SCOPE_UNRESOLVED = "parent_workspace_scope_unresolved"

# --- coordinator-branch refusal vocabulary (closed; #15700) ----------------- #

#: Neither a ``project_gateway`` binding nor a default-lane binding is declared.
PARENT_AUTHORITY_UNDECLARED = "parent_authority_undeclared"
#: The default-lane declaration is invalid / ambiguous, or the probe was unavailable.
PARENT_COORDINATOR_BLOCKED = "parent_coordinator_authority_blocked"
#: The default lane is bound, but not to ``coordinator`` (e.g. a monorepo root's
#: ``grandparent_coordinator``) — that topology must declare its gateway tier.
PARENT_COORDINATOR_ROLE_MISMATCH = "parent_coordinator_role_mismatch"
#: No provider is bound for the resolved coordinator role.
PARENT_COORDINATOR_PROVIDER_UNRESOLVED = "parent_coordinator_provider_unresolved"
#: Zero live default-lane coordinator occupants (or no herdr inventory at all).
PARENT_COORDINATOR_LIVE_MISSING = "parent_coordinator_live_missing"
#: Two or more live default-lane occupants — never guess one.
PARENT_COORDINATOR_LIVE_AMBIGUOUS = "parent_coordinator_live_ambiguous"
#: Exactly one live occupant, but no usable live locator.
PARENT_COORDINATOR_LOCATOR_MISSING = "parent_coordinator_locator_missing"
#: Exactly one live occupant, but no generation-matched startup self-attestation.
PARENT_COORDINATOR_UNATTESTED = "parent_coordinator_unattested"

#: The mechanical live-status -> refusal map (TARGET_OK admits; anything outside the
#: closed set folds to BLOCKED, because an unrecognized liveness verifies nothing).
_LIVE_STATUS_REFUSALS = {
    TARGET_MISSING: PARENT_COORDINATOR_LIVE_MISSING,
    TARGET_AMBIGUOUS: PARENT_COORDINATOR_LIVE_AMBIGUOUS,
    TARGET_LOCATOR_MISSING: PARENT_COORDINATOR_LOCATOR_MISSING,
    TARGET_UNATTESTED: PARENT_COORDINATOR_UNATTESTED,
}


@dataclass(frozen=True)
class CoordinatorParentProbe:
    """The coordinator-branch facts, read by the application layer (#15700).

    ``authority_status`` / ``authority_reason`` come from the default-lane authority
    resolution; ``role`` / ``provider`` from the declaration + provider binding;
    ``live_status`` from the exactly-one live-attested resolution (the coordinator
    proxy rail's own policy), with ``live_detail`` naming what stopped it and
    ``verified_target`` the attested occupant's assigned name when live.
    """

    authority_status: str = ""
    authority_reason: str = ""
    role: str = ""
    provider: str = ""
    live_status: str = ""
    live_detail: str = ""
    verified_target: str = ""


@dataclass(frozen=True)
class ParentAuthorityVerdict:
    """Whether a delegated_coordinator creation may proceed, or the typed reason not."""

    ok: bool
    reason: str = ""
    detail: str = ""
    #: The declared gateway lane ids that verified, when ``ok`` (diagnostic only).
    verified_gateway_lanes: Tuple[str, ...] = ()
    #: Which parent branch decided (#15700): ``project_gateway`` when a gateway
    #: topology is declared, ``default_lane_coordinator`` when none is — set on
    #: refusals too, so the taken branch is observable and never silent.
    parent_kind: str = ""
    #: The attested default-lane occupant, when the coordinator branch admitted.
    verified_coordinator: str = ""


def decide_delegated_parent_authority(
    parsed: ParsedRoleBindings,
    *,
    owner_row_active: Callable[[str, str], bool],
    coordinator_parent_probe: Optional[Callable[[], CoordinatorParentProbe]] = None,
) -> ParentAuthorityVerdict:
    """Decide the admission from the parsed declaration and the injected reads.

    Fail-closed order:

    1. an invalid declaration refuses outright — an untrustworthy authority admits
       nothing;
    2. one or more ``project_gateway`` bindings declare the monorepo topology: the
       declared tier must VERIFY (an ACTIVE canonical owner row), exactly as before
       #15700 — a declared topology never falls back to the coordinator branch;
    3. no ``project_gateway`` binding at all is the single-workspace topology
       (#15700): the parent is the workspace's own default-lane coordinator, and it
       must be DECLARED (``coordinator`` bound to the default lane, with a bound
       provider) and LIVE (exactly one live, attested default-lane occupant — the
       coordinator proxy rail's own exactly-one policy). Every gap refuses typed;
       with no probe injected the branch refuses BLOCKED, never admits.

    ``owner_row_active(lane_id, project_scope)`` answers whether the gateway
    lane's CANONICAL lifecycle owner row exists — a row that is not merely
    active, but carries ``binding_kind=project_gateway`` and this binding's own
    canonical project scope (review j#106254 finding_parentownerrowtype: a
    stale or foreign issue-kind row occupying the derived key must never stand
    in for the parent). The application layer resolves the store and the
    workspace scope, and folds its own read failures to ``False`` (an
    unreadable authority verifies nothing).
    """
    if not parsed.ok:
        return ParentAuthorityVerdict(
            False,
            reason=PARENT_BINDINGS_INVALID,
            detail=parsed.detail or "the workflow-role binding declaration is invalid",
        )
    gateways = [b for b in parsed.bindings if b.role == ROLE_PROJECT_GATEWAY]
    if not gateways:
        return _decide_coordinator_parent(parsed, coordinator_parent_probe)
    verified = tuple(
        b.lane_id for b in gateways if owner_row_active(b.lane_id, b.project_scope)
    )
    if not verified:
        return ParentAuthorityVerdict(
            False,
            reason=PARENT_GATEWAY_UNVERIFIED,
            detail=(
                f"{len(gateways)} project_gateway binding(s) are declared, but no "
                "declared gateway lane owns an ACTIVE canonical owner row "
                "(binding_kind=project_gateway with the binding's own project "
                "scope)"
            ),
            parent_kind=PARENT_KIND_PROJECT_GATEWAY,
        )
    return ParentAuthorityVerdict(
        True,
        verified_gateway_lanes=verified,
        parent_kind=PARENT_KIND_PROJECT_GATEWAY,
    )


def _decide_coordinator_parent(
    parsed: ParsedRoleBindings,
    coordinator_parent_probe: Optional[Callable[[], CoordinatorParentProbe]],
) -> ParentAuthorityVerdict:
    """The single-workspace branch (#15700): no gateway tier was ever declared."""

    def refuse(reason: str, detail: str) -> ParentAuthorityVerdict:
        return ParentAuthorityVerdict(
            False,
            reason=reason,
            detail=detail,
            parent_kind=PARENT_KIND_DEFAULT_COORDINATOR,
        )

    if coordinator_parent_probe is None:
        return refuse(
            PARENT_COORDINATOR_BLOCKED,
            "no coordinator-parent probe was supplied, so the default-lane "
            "coordinator cannot be verified",
        )
    probe = coordinator_parent_probe()
    if probe.authority_status == AUTHORITY_MISSING:
        return refuse(
            PARENT_AUTHORITY_UNDECLARED,
            "neither a project_gateway role binding nor a default-lane role "
            "binding is declared for this workspace",
        )
    if probe.authority_status != AUTHORITY_RESOLVED:
        return refuse(
            PARENT_COORDINATOR_BLOCKED,
            "the default-lane role declaration did not resolve"
            + (f" ({probe.authority_reason})" if probe.authority_reason else ""),
        )
    if probe.role != ROLE_COORDINATOR:
        return refuse(
            PARENT_COORDINATOR_ROLE_MISMATCH,
            f"the default lane is bound to {probe.role!r}, not "
            f"{ROLE_COORDINATOR!r}; a monorepo topology establishes its parent "
            "tier via `mozyo-bridge sublane declare-project-gateway` instead",
        )
    if not probe.provider:
        return refuse(
            PARENT_COORDINATOR_PROVIDER_UNRESOLVED,
            "no provider is bound for the default-lane coordinator role",
        )
    if probe.live_status == TARGET_OK:
        return ParentAuthorityVerdict(
            True,
            parent_kind=PARENT_KIND_DEFAULT_COORDINATOR,
            verified_coordinator=probe.verified_target,
        )
    reason = _LIVE_STATUS_REFUSALS.get(probe.live_status, PARENT_COORDINATOR_BLOCKED)
    return refuse(
        reason,
        probe.live_detail
        or "the default-lane coordinator's live exactly-one attestation did not "
        "verify",
    )


def parent_authority_refusal_text(verdict: ParentAuthorityVerdict) -> str:
    """The operator-facing refusal for a non-ok verdict (fixed wording + tokens).

    Names the legitimate routes out through CLI surfaces rather than file paths so
    the guidance resolves in any adopter environment. The remedy differs by branch
    (#15700): a declared-gateway refusal points at the gateway tier; a
    coordinator-branch refusal points at the default-lane coordinator (bind the
    role, then have its live attested pane up) — and both name the no-claim escape
    (the default coordinator and ``implementation`` lane kinds carry no parent
    requirement).
    """
    if verdict.parent_kind == PARENT_KIND_DEFAULT_COORDINATOR:
        return (
            f"sublane create refused: lane_kind delegated_coordinator asserts a "
            f"parent, and in this single-workspace topology (no project_gateway "
            f"binding declared) that parent is the default-lane coordinator, "
            f"which is not established ({verdict.reason}: {verdict.detail}). No "
            "worktree, pane, or dispatch was created. Either establish the "
            "coordinator parent — bind the coordinator role via `mozyo-bridge "
            "workflow role-authority --mint-coordinator` and have its live "
            "attested default-lane pane up — or create the lane without the "
            "parent claim (the default coordinator and `implementation` lane "
            "kinds carry no parent requirement)."
        )
    return (
        f"sublane create refused: lane_kind delegated_coordinator asserts a parent "
        f"project gateway, and that parent is not established ({verdict.reason}: "
        f"{verdict.detail}). No worktree, pane, or dispatch was created. Either "
        "establish the parent tier first — declare the project_gateway role "
        "binding and its canonical owner row via `mozyo-bridge sublane "
        "declare-project-gateway` — or, for a single-workspace topology, create "
        "the lane without the three-tier claim (the default coordinator and "
        "`implementation` lane kinds carry no parent requirement)."
    )


__all__ = (
    "DELEGATED_COORDINATOR_LANE_KIND",
    "PARENT_AUTHORITY_UNDECLARED",
    "PARENT_BINDINGS_INVALID",
    "PARENT_COORDINATOR_BLOCKED",
    "PARENT_COORDINATOR_LIVE_AMBIGUOUS",
    "PARENT_COORDINATOR_LIVE_MISSING",
    "PARENT_COORDINATOR_LOCATOR_MISSING",
    "PARENT_COORDINATOR_PROVIDER_UNRESOLVED",
    "PARENT_COORDINATOR_ROLE_MISMATCH",
    "PARENT_COORDINATOR_UNATTESTED",
    "PARENT_GATEWAY_UNDECLARED",
    "PARENT_GATEWAY_UNVERIFIED",
    "PARENT_KIND_DEFAULT_COORDINATOR",
    "PARENT_KIND_PROJECT_GATEWAY",
    "PARENT_SCOPE_UNRESOLVED",
    "CoordinatorParentProbe",
    "ParentAuthorityVerdict",
    "decide_delegated_parent_authority",
    "parent_authority_refusal_text",
)
