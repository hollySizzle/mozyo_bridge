"""Ticketless transition role/action boundary payload (Redmine #12706).

GK3500 exploratory smoke #12698 surfaced a lane-boundary defect: a receiver that
could read the workflow contract docs classified a ticketless consultation into a
project, then *also* made the parent project gateway's ``no_dispatch`` /
``anchor_required`` decision itself. The receiver was effectively a grandparent
(department-root) coordinator, whose only job is to classify the consultation and
resolve / start / hand off to the parent project gateway — the project-domain
``no_dispatch`` decision belongs to the gateway.

The root cause was that the ticketless transition payload carried **no explicit
role binding**, so the receiver inferred its lane role from pane / docs-readable
context and over-stepped. This module is the pure, fail-closed source of truth for
the explicit transition role/action boundary that is carried on the standard
handoff transition payload and durable delivery record so the receiver never has
to *infer* what it may and may not do.

Design boundaries (Redmine #12706 j#67045):

- This boundary is the *transition / action* payload and stays **separate** from
  :mod:`...domain.role_profile`. ``role_profile`` is the receiver's custom
  *instruction template* (a free-text body); this boundary is a small, fixed,
  machine-readable set of ``current_role`` / ``allowed_actions`` /
  ``forbidden_actions`` / ``handoff_target_role`` tokens. Both can travel on the
  same handoff; neither subsumes the other.
- The boundaries are pinned as code constants (no filesystem path guessing at
  send time) and fail closed: an unknown role token raises
  :class:`TransitionRoleError`, and a malformed boundary (blank role, empty
  action set, blank action token, allowed/forbidden overlap) cannot be
  constructed. Omitting the boundary is the explicit fallback of no role binding.
- Every field is a fixed lower-snake-case token with no operator-supplied free
  text, so the whole boundary — including the full allowed/forbidden lists — is
  durable-record safe and may be persisted verbatim (unlike the role-profile
  contract body, which can embed operator field values). Because the boundary is
  a builtin resolved from a fixed token, a manual / assisted role payload is
  never required and is not the product evidence: the standard transition payload
  carries it (Redmine #12706 acceptance).

The grandparent boundary mirrors the ``classify_ticketless_consultation`` /
``resolve_project_gateway`` / ``start_project_gateway`` /
``handoff_to_project_gateway`` swimlane functions in
``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`` (Department Root
Coordinator lane); the project-gateway boundary mirrors the Project Gateway lane
that owns the project-domain ``decide_implementation_need`` /
``ensure_redmine_anchor`` decisions the grandparent must not pre-empt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


class TransitionRoleError(ValueError):
    """A transition role boundary could not be resolved or is malformed."""


# Role tokens for the ticketless department-root -> project-gateway transition.
# Insertion order matches the swimlane authority ordering (the grandparent hands
# off to the gateway).
ROLE_GRANDPARENT_COORDINATOR = "grandparent_coordinator"
ROLE_PROJECT_GATEWAY = "project_gateway"


def _clean_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransitionRoleError(
            f"transition role {field} must be a non-empty token; got {value!r}"
        )
    return value.strip()


def _clean_actions(actions: Iterable[object], *, field: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for action in actions:
        token = _clean_token(action, field=f"{field} entry")
        if token not in cleaned:
            cleaned.append(token)
    if not cleaned:
        raise TransitionRoleError(
            f"transition role {field} must list at least one action token"
        )
    return tuple(cleaned)


@dataclass(frozen=True)
class TransitionRoleBoundary:
    """Explicit role/action boundary carried on a ticketless transition (#12706).

    ``current_role`` names the receiver's lane role for *this* transition;
    ``allowed_actions`` / ``forbidden_actions`` bound what that role may finalize
    locally; ``handoff_target_role`` names the role the request must be handed off
    to. The receiver reads this instead of inferring its role from ``%pane`` /
    active pane / docs-only context.

    All four fields are fixed lower-snake-case tokens (no operator free text), so
    instances are durable-record safe in full. Construction fails closed on a
    blank role, an empty action set, a blank action token, or an allowed/forbidden
    overlap.
    """

    current_role: str
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    handoff_target_role: str

    def __post_init__(self) -> None:
        # Normalize + fail-closed validate via object.__setattr__ (frozen).
        object.__setattr__(
            self, "current_role", _clean_token(self.current_role, field="current_role")
        )
        object.__setattr__(
            self,
            "allowed_actions",
            _clean_actions(self.allowed_actions, field="allowed_actions"),
        )
        object.__setattr__(
            self,
            "forbidden_actions",
            _clean_actions(self.forbidden_actions, field="forbidden_actions"),
        )
        object.__setattr__(
            self,
            "handoff_target_role",
            _clean_token(self.handoff_target_role, field="handoff_target_role"),
        )
        overlap = set(self.allowed_actions) & set(self.forbidden_actions)
        if overlap:
            raise TransitionRoleError(
                "transition role allowed_actions and forbidden_actions must be "
                f"disjoint; overlapping tokens: {sorted(overlap)}"
            )

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free fields for the handoff transition payload."""
        return {
            "current_role": self.current_role,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "handoff_target_role": self.handoff_target_role,
        }

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full allowed/forbidden boundary stays in the durable delivery
        record. Names the role and the handoff target, and points at the durable
        record for the full action boundary.
        """
        return (
            f"transition role: {self.current_role} -> {self.handoff_target_role} "
            f"(allowed: {len(self.allowed_actions)}, forbidden: "
            f"{len(self.forbidden_actions)}; full action boundary is in the durable "
            "delivery record)"
        )


# Builtin boundaries for the ticketless department-root -> project-gateway
# transition. The grandparent boundary is the one #12698 needed: classify +
# resolve/start/handoff are allowed, but the project-domain / parent-gateway
# no_dispatch decision is forbidden. The project-gateway boundary is its
# complement: it OWNS the project-domain / no_dispatch / anchor decisions the
# grandparent must not pre-empt.
TRANSITION_ROLE_BOUNDARIES: dict[str, TransitionRoleBoundary] = {
    ROLE_GRANDPARENT_COORDINATOR: TransitionRoleBoundary(
        current_role=ROLE_GRANDPARENT_COORDINATOR,
        allowed_actions=(
            "classify_ticketless_consultation",
            "resolve_or_start_parent_project_gateway",
            "handoff_to_parent_gateway",
            "return_blocked_if_gateway_unavailable",
        ),
        forbidden_actions=(
            "project_domain_decision",
            "parent_gateway_no_dispatch_decision",
            "local_probe",
            "implementation",
            "direct_Claude_send",
        ),
        handoff_target_role=ROLE_PROJECT_GATEWAY,
    ),
    ROLE_PROJECT_GATEWAY: TransitionRoleBoundary(
        current_role=ROLE_PROJECT_GATEWAY,
        allowed_actions=(
            "receive_ticketless_consultation",
            "project_domain_decision",
            "parent_gateway_no_dispatch_decision",
            "anchor_required_decision",
            "dispatch_redmine_anchored_worker",
            "reply_consultation_result",
            "return_blocked_if_worker_unavailable",
        ),
        forbidden_actions=(
            "implementation",
            "owner_approval_collection",
            "parent_issue_close",
        ),
        handoff_target_role="implementation_worker",
    ),
}

TRANSITION_ROLE_TOKENS: tuple[str, ...] = tuple(TRANSITION_ROLE_BOUNDARIES.keys())

# The two project-domain decisions that #12698 leaked across the boundary: they
# are forbidden for the grandparent and owned by the project gateway. Pinned here
# so a test can assert the boundary stays coherent (the grandparent must never be
# allowed these, the gateway must always be).
PROJECT_DOMAIN_DECISIONS: tuple[str, ...] = (
    "project_domain_decision",
    "parent_gateway_no_dispatch_decision",
)


def resolve_transition_role(role: str) -> TransitionRoleBoundary:
    """Resolve a builtin transition role boundary by token.

    Fails closed with :class:`TransitionRoleError` when ``role`` has no builtin
    boundary, so a caller never silently treats an unknown role as "no boundary".
    The function is pure and deterministic over its input.
    """
    boundary = TRANSITION_ROLE_BOUNDARIES.get(role)
    if boundary is None:
        raise TransitionRoleError(
            f"unknown transition role: {role!r}; expected one of "
            f"{list(TRANSITION_ROLE_TOKENS)}"
        )
    return boundary


def transition_role_from_payload(
    payload: Mapping[str, object],
) -> TransitionRoleBoundary:
    """Rebuild a boundary from a structured payload (round-trips to_structured_dict).

    Fails closed (:class:`TransitionRoleError`) on a missing/malformed field, so a
    receiver that parses the transition payload cannot silently accept a partial
    boundary. Action lists must be sequences of tokens.
    """
    try:
        current = payload["current_role"]
        allowed = payload["allowed_actions"]
        forbidden = payload["forbidden_actions"]
        target = payload["handoff_target_role"]
    except KeyError as exc:
        raise TransitionRoleError(
            f"transition role payload missing required field: {exc.args[0]!r}"
        ) from exc
    if not isinstance(allowed, (list, tuple)) or not isinstance(
        forbidden, (list, tuple)
    ):
        raise TransitionRoleError(
            "transition role payload allowed_actions / forbidden_actions must be "
            "sequences of tokens"
        )
    return TransitionRoleBoundary(
        current_role=current,  # type: ignore[arg-type]
        allowed_actions=tuple(allowed),
        forbidden_actions=tuple(forbidden),
        handoff_target_role=target,  # type: ignore[arg-type]
    )


__all__: Iterable[str] = (
    "TransitionRoleError",
    "ROLE_GRANDPARENT_COORDINATOR",
    "ROLE_PROJECT_GATEWAY",
    "PROJECT_DOMAIN_DECISIONS",
    "TransitionRoleBoundary",
    "TRANSITION_ROLE_BOUNDARIES",
    "TRANSITION_ROLE_TOKENS",
    "resolve_transition_role",
    "transition_role_from_payload",
)
