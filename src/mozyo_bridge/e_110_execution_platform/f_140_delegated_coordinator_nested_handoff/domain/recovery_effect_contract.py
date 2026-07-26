"""The recovery outcome's applied-effect / unresolved-fate contract (Redmine #14475).

An outcome answers three questions that kept getting collapsed into one another across reviews
j#88538, j#88554 and j#88563:

* **what did this run apply?** — :data:`RECOVERY_EFFECTS`, membership meaning "this happened";
* **what did it attempt without learning the outcome?** — :data:`RECOVERY_UNRESOLVED_FATES`.
  A redispatch reported ``uncertain`` spans a zero-write refusal *before* the outbox reserve
  and an unknown write fate *after* the send, so calling it an applied effect asserts a write
  the status cannot support;
* **did it act at all?** — the caller's ``attempted`` flag. Using "did anything change" as a
  proxy for "did we act" made a first-close failure — which applies nothing — read as *not*
  blocked.

Keeping the vocabulary and its validator here means an outcome that contradicts itself cannot
be constructed, so a later branch cannot quietly reintroduce a fixed ``executed=True`` or an
off-vocabulary token: :func:`validate_effect_contract` fails loudly instead.

Pure: no store, no process, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

# -- what a run is KNOWN to have applied ---------------------------------------

EFFECT_CLOSED = "closed_bad_generation"
EFFECT_RELAUNCHED = "relaunched_pair"
EFFECT_RESUME_COMMITTED = "resume_disposition_committed"
EFFECT_REDISPATCHED = "implementation_request_redelivered"

#: Membership means the effect DID happen. Nothing whose outcome is merely attempted or
#: unknown belongs here (review j#88563 F1).
RECOVERY_EFFECTS = frozenset(
    {
        EFFECT_CLOSED,
        EFFECT_RELAUNCHED,
        EFFECT_RESUME_COMMITTED,
        EFFECT_REDISPATCHED,
    }
)

# -- what a run attempted without establishing the outcome ---------------------

#: The redelivery neither demonstrably delivered nor demonstrably no-opped.
FATE_REDISPATCH_UNRESOLVED = "redispatch_fate_unresolved"

RECOVERY_UNRESOLVED_FATES = frozenset({FATE_REDISPATCH_UNRESOLVED})


def validate_effect_contract(
    *,
    executed: bool,
    effects: Sequence[str],
    unresolved: Sequence[str],
    attempted: bool,
) -> None:
    """Raise unless the outcome's facts are mutually consistent. (pure)

    - every token comes from its own closed vocabulary;
    - ``executed`` is exactly the non-emptiness of ``effects`` — an outcome claiming a change
      with nothing applied (or the reverse) contradicts itself;
    - anything applied OR attempted-with-unknown-fate implies the actuation was entered
      (review j#88571 F2). Without this the validator was weaker than its own docstring: an
      outcome with effects and ``attempted=False`` constructed fine and then read as unblocked.
    """
    unknown = tuple(e for e in effects if e not in RECOVERY_EFFECTS)
    if unknown:
        raise ValueError(f"unknown recovery effect(s): {unknown}")
    stray = tuple(f for f in unresolved if f not in RECOVERY_UNRESOLVED_FATES)
    if stray:
        raise ValueError(f"unknown unresolved fate(s): {stray}")
    if executed != bool(effects):
        raise ValueError(
            "executed must be exactly the non-emptiness of the applied effects "
            f"(executed={executed!r}, effects={tuple(effects)!r})"
        )
    if (effects or unresolved) and not attempted:
        raise ValueError(
            "effects / unresolved fates imply the actuation was attempted "
            f"(attempted={attempted!r}, effects={tuple(effects)!r}, "
            f"unresolved={tuple(unresolved)!r})"
        )


# -- the redispatch edge's own observation --------------------------------------

@dataclass(frozen=True)
class RedispatchEdgeResult:
    """What the reserve -> send -> record edge OBSERVED, before any status collapse.

    Review j#88571 F1: collapsing that edge into a single ``REDISPATCH_*`` string throws away
    facts the application then cannot recover. A ``uncertain`` status covers both "the fence
    was never bootstrapped, nothing was sent" and "the transport started and the delivered
    record failed to write" — the same token for a zero-send and for a KNOWN redelivery. The
    edge reports what it knows; nothing downstream re-infers it.

    - :attr:`status` — the public ``REDISPATCH_*`` token (unchanged contract for callers).
    - :attr:`delivered` — the transport positively started for this action: a known-applied
      redelivery, whatever happened to the ledger write afterwards.
    - :attr:`zero_send` — positively nothing was sent AND the durable state is settled (e.g. a
      cancelled reserve): no unknown fate remains.
    - :attr:`unknown_fate` — the durable fate could not be established.
    """

    status: str
    delivered: bool = False
    zero_send: bool = False
    unknown_fate: bool = False

    def __post_init__(self) -> None:
        if self.zero_send and self.delivered:
            raise ValueError("a zero-send cannot also be a delivery")
        if self.zero_send and self.unknown_fate:
            raise ValueError("a settled zero-send has no unknown fate")

    @property
    def effects(self) -> Tuple[str, ...]:
        """The KNOWN-applied effects this edge observed. (pure)"""
        return (EFFECT_REDISPATCHED,) if self.delivered else ()

    @property
    def unresolved(self) -> Tuple[str, ...]:
        """The unresolved fates this edge observed. (pure)"""
        return (FATE_REDISPATCH_UNRESOLVED,) if self.unknown_fate else ()


def as_edge_result(value: object) -> RedispatchEdgeResult:
    """Adapt an ops return to the typed edge result. (pure, conservative)

    A :class:`RedispatchEdgeResult` passes through. A bare status string — what a fake that
    does not model the edge returns — is adapted with the only inference a string supports:
    ``redispatched`` is a delivery, ``already`` / ``skipped`` / ``target_retiring`` are settled
    zero-sends, and ``failed`` / ``uncertain`` are unknown. Production adapters return the
    typed result, so this inference never stands in for a real observation.
    """
    if isinstance(value, RedispatchEdgeResult):
        return value
    status = str(value or "")
    if status == "redispatched":
        return RedispatchEdgeResult(status=status, delivered=True)
    if status in ("already_redispatched", "redispatch_not_reached", "redispatch_target_retiring"):
        return RedispatchEdgeResult(status=status, zero_send=True)
    return RedispatchEdgeResult(status=status, unknown_fate=True)


# -- terminal success policy, shared by both surfaces ---------------------------

#: The ONLY redispatch statuses that leave an attempted run unblocked (review j#88571 F2).
#: One closed set for the main recovery and the retry surface, so they cannot drift, and a
#: future token is blocked by default rather than silently succeeding.
REDISPATCH_TERMINAL_SUCCESS = frozenset({"redispatched", "already_redispatched"})


def redispatch_is_success(status: str) -> bool:
    """Does this terminal status leave an attempted run unblocked? (pure, fail-closed)"""
    return status in REDISPATCH_TERMINAL_SUCCESS


__all__ = (
    "EFFECT_CLOSED",
    "EFFECT_RELAUNCHED",
    "EFFECT_RESUME_COMMITTED",
    "EFFECT_REDISPATCHED",
    "RECOVERY_EFFECTS",
    "FATE_REDISPATCH_UNRESOLVED",
    "RECOVERY_UNRESOLVED_FATES",
    "validate_effect_contract",
    "RedispatchEdgeResult",
    "as_edge_result",
    "REDISPATCH_TERMINAL_SUCCESS",
    "redispatch_is_success",
)
