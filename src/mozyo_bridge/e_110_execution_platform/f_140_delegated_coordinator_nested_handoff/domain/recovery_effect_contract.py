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
    *, executed: bool, effects: Sequence[str], unresolved: Sequence[str]
) -> None:
    """Raise unless the outcome's three facts are mutually consistent. (pure)

    ``executed`` must be exactly the non-emptiness of ``effects`` — an outcome claiming a
    change with nothing applied (or the reverse) is a report contradicting itself — and every
    token must come from its own closed vocabulary.
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


def redispatch_effects(redispatch: str) -> Tuple[str, ...]:
    """The KNOWN-applied effects a redispatch status proves. (pure)"""
    return (EFFECT_REDISPATCHED,) if redispatch == "redispatched" else ()


def redispatch_unresolved(redispatch: str) -> Tuple[str, ...]:
    """The unresolved fates a redispatch status carries. (pure)"""
    if redispatch in ("redispatch_send_failed", "redispatch_uncertain"):
        return (FATE_REDISPATCH_UNRESOLVED,)
    return ()


__all__ = (
    "EFFECT_CLOSED",
    "EFFECT_RELAUNCHED",
    "EFFECT_RESUME_COMMITTED",
    "EFFECT_REDISPATCHED",
    "RECOVERY_EFFECTS",
    "FATE_REDISPATCH_UNRESOLVED",
    "RECOVERY_UNRESOLVED_FATES",
    "validate_effect_contract",
    "redispatch_effects",
    "redispatch_unresolved",
)
