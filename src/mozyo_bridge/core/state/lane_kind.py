"""Canonical lane-kind vocabulary (Redmine #13647, Design Answer j#85645 / disposition j#85650).

The single source of truth for the three-token lane-kind vocabulary
``coordinator | delegated_coordinator | implementation`` — the workflow-role
*geometry* axis of a lane (親 / 子 / 孫, delegation depth 0 / 1 / 2).

It lives in ``core/state`` — not ``shared`` — per the shared-kernel freeze policy
(``vibes/docs/logics/shared-kernel-freeze.md``, Redmine #12640): ``shared/**`` is a
frozen kernel and a new cross-cutting value belongs in a bounded context or
``core/state``. ``lane_kind`` is a **managed-state / lane-identity value** (a lane's
delegation-tree position), co-located with the lifecycle authority record
(:mod:`mozyo_bridge.core.state.lane_lifecycle_model`) that stores the resolved kind
generation-bound for heal (Redmine #13647 Tranche 1b). ``core/state`` is a
foundational layer below the ``e_*`` bounded contexts, so every layer that needs the
vocabulary imports *downward* and no layer reverse-imports another:

- the cockpit delegation projection (``e_110`` ``delegation_projection``) re-exports
  these tokens for its existing display consumers (``@mozyo_lane_kind`` cache /
  ``agents targets`` columns), so its public names are unchanged;
- the repo-local ``lane_placement`` config schema (``e_130``) validates its
  ``by_lane_kind`` keys against :data:`LANE_KINDS`;
- the herdr launch path (``e_140``) resolves a lane's placement geometry by this
  key without reverse-importing the display module (Design Answer j#85645 point 4).

Vocabulary boundary (kept enforced in code):

- **Closed, exactly three tokens.** There is deliberately no ``unknown`` member: a
  caller without a durable kind fact fails closed (its own typed error), never
  emits an off-contract value. This mirrors the pre-existing
  ``delegation_projection.LANE_KINDS`` contract (Redmine #12465 review j#63800),
  whose definition this module now owns.
- **Geometry axis, not routing / provider authority.** ``lane_kind`` names a lane's
  delegation-tree position for *placement geometry* and (later) role-profile
  selection; it is never an mzb1 name, ``MOZYO_AGENT_ROLE`` (a provider token), a
  route / attestation / retire authority, or a display cache promoted to truth
  (disposition j#85650). It is coarser than the four routing roles of the
  role-profile contract (it folds gateway + worker into the single kind
  ``implementation``).
- **No config alias.** The machine vocabulary is exactly these three tokens
  (disposition j#85650 P3); owner-facing docs / displays may render 親 / 子 / 孫 but
  must not grow the machine vocabulary with ``parent`` / ``child`` / ``grandchild``
  aliases.

Pure: literals + a ``ValueError`` subclass + small total predicates. It imports
nothing, so any layer may depend on it.
"""

from __future__ import annotations

from typing import Optional

#: The coordinator (親, delegation depth 0) lane — the default-lane coordinator pair.
LANE_KIND_COORDINATOR = "coordinator"
#: The delegated coordinator (子, delegation depth 1) lane.
LANE_KIND_DELEGATED_COORDINATOR = "delegated_coordinator"
#: The implementation (孫, delegation depth 2) realization lane. Folds
#: implementation_gateway + implementation_worker into one geometry kind.
LANE_KIND_IMPLEMENTATION = "implementation"

#: The CLOSED lane-kind vocabulary. No ``unknown`` member by design — a caller
#: without a durable kind fact fails closed rather than emitting an off-contract
#: value (Redmine #12465 review j#63800; disposition j#85650).
LANE_KINDS: frozenset[str] = frozenset(
    {
        LANE_KIND_COORDINATOR,
        LANE_KIND_DELEGATED_COORDINATOR,
        LANE_KIND_IMPLEMENTATION,
    }
)


class LaneKindError(ValueError):
    """A value is not a canonical lane-kind token (fail-closed)."""


def is_lane_kind(value: object) -> bool:
    """True iff ``value`` is exactly one of the three canonical lane-kind tokens."""
    return isinstance(value, str) and value in LANE_KINDS


def checked_lane_kind(value: object, *, source: str) -> str:
    """Return ``value`` when it is a canonical lane-kind token, else fail closed.

    ``source`` names the caller surface for the error message (e.g. a config path
    or a launch-context field). A non-string or an off-vocabulary token raises
    :class:`LaneKindError` — the closed vocabulary is never silently normalized.
    """
    if not is_lane_kind(value):
        raise LaneKindError(
            f"{source} must be one of {sorted(LANE_KINDS)} (the canonical lane-kind "
            f"vocabulary), got {value!r}"
        )
    return value  # type: ignore[return-value]


def optional_lane_kind(value: object, *, source: str) -> Optional[str]:
    """Return a canonical token, or ``None`` when ``value`` is absent (``None`` / "").

    An empty / absent value is a legitimate "no durable kind fact" marker (the
    caller then falls back to ``lane_class`` geometry); any *present* non-empty
    value must be a canonical token or fails closed via :func:`checked_lane_kind`.
    """
    if value is None or value == "":
        return None
    return checked_lane_kind(value, source=source)


__all__ = (
    "LANE_KIND_COORDINATOR",
    "LANE_KIND_DELEGATED_COORDINATOR",
    "LANE_KIND_IMPLEMENTATION",
    "LANE_KINDS",
    "LaneKindError",
    "checked_lane_kind",
    "is_lane_kind",
    "optional_lane_kind",
)
