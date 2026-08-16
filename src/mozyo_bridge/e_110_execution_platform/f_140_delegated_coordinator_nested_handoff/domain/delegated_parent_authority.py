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

Single-workspace setups are not squeezed out (close condition 3): the default
coordinator and plain ``implementation`` lanes carry no parent claim and are untouched
by this admission. What is no longer possible is claiming the three-tier shape without
the tier above existing on durable record.

Pure: bindings arrive parsed, and owner-row existence is an injected predicate — the
application layer owns every read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    ParsedRoleBindings,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (  # noqa: E501
    ROLE_PROJECT_GATEWAY,
)

#: The lane kind whose creation asserts a parent project gateway.
DELEGATED_COORDINATOR_LANE_KIND = "delegated_coordinator"

# --- refusal vocabulary (closed) ------------------------------------------- #

#: The binding declaration exists but cannot be trusted (unreadable / malformed).
PARENT_BINDINGS_INVALID = "parent_authority_bindings_invalid"
#: No ``project_gateway`` role binding is declared at all — the filed reproduction.
PARENT_GATEWAY_UNDECLARED = "parent_gateway_binding_missing"
#: Declared, but no declared gateway lane owns an ACTIVE lifecycle owner row.
PARENT_GATEWAY_UNVERIFIED = "parent_gateway_owner_row_missing"
#: The workspace identity needed to scope the owner-row lookup did not resolve.
PARENT_SCOPE_UNRESOLVED = "parent_workspace_scope_unresolved"


@dataclass(frozen=True)
class ParentAuthorityVerdict:
    """Whether a delegated_coordinator creation may proceed, or the typed reason not."""

    ok: bool
    reason: str = ""
    detail: str = ""
    #: The declared gateway lane ids that verified, when ``ok`` (diagnostic only).
    verified_gateway_lanes: Tuple[str, ...] = ()


def decide_delegated_parent_authority(
    parsed: ParsedRoleBindings,
    *,
    owner_row_active: Callable[[str, str], bool],
) -> ParentAuthorityVerdict:
    """Decide the admission from the parsed declaration and an owner-row predicate.

    Fail-closed order:

    1. an invalid declaration refuses outright — an untrustworthy authority admits
       nothing;
    2. no ``project_gateway`` binding refuses as UNDECLARED (the reproduction the
       issue was filed on: a ``coordinator``-only declaration);
    3. bindings whose derived gateway lane has no ACTIVE lifecycle owner row refuse
       as UNVERIFIED — a declaration alone is intent, not an existing parent; the
       owner row is what ``sublane declare-project-gateway`` writes from a live,
       attested pair, and it is the fact the child geometry hangs from.

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
        return ParentAuthorityVerdict(
            False,
            reason=PARENT_GATEWAY_UNDECLARED,
            detail=(
                "no durable project_gateway role binding is declared for this "
                "workspace"
            ),
        )
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
        )
    return ParentAuthorityVerdict(True, verified_gateway_lanes=verified)


def parent_authority_refusal_text(verdict: ParentAuthorityVerdict) -> str:
    """The operator-facing refusal for a non-ok verdict (fixed wording + tokens).

    Names both legitimate routes out (close conditions 3 and 5), through CLI
    surfaces rather than file paths so the guidance resolves in any adopter
    environment:

    - establish the parent tier: declare the gateway's owner row with
      ``mozyo-bridge sublane declare-project-gateway`` (after adding the
      ``project_gateway`` role binding, when it is missing);
    - or, for a single-workspace setup, create the lane without the three-tier
      claim — the default coordinator and ``implementation`` lane kinds carry no
      parent requirement.
    """
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
    "PARENT_BINDINGS_INVALID",
    "PARENT_GATEWAY_UNDECLARED",
    "PARENT_GATEWAY_UNVERIFIED",
    "PARENT_SCOPE_UNRESOLVED",
    "ParentAuthorityVerdict",
    "decide_delegated_parent_authority",
    "parent_authority_refusal_text",
)
