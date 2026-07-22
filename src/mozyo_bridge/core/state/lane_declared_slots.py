"""The declared-slot snapshot value + codec (Redmine #13810; leaf of the lane model).

The typed :class:`ProcessGenerationPin` a lane generation records when it is DECLARED,
and the JSON envelope it round-trips through the lifecycle row's ``declared_slots``
column. A declaration snapshot, never a liveness fact.

Carved out of :mod:`mozyo_bridge.core.state.lane_lifecycle_model` unchanged (Redmine
#13647 Tranche 1b, module-health leaf extraction — the model module sat one line under
the 1000-line ceiling, so the v7 ``lane_kind`` authority field could not be added in
place). This group is the natural seam: a self-contained value type plus its encode /
decode / validate codec, with no reference to the rest of the model. The model module
re-exports every public name, so the import surface is unchanged for every caller.

Pure: literals, a frozen dataclass, a typed error, and total codec functions over
``json``. No I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Sequence


def norm(value: object) -> str:
    """Trim a raw field to a comparable token (``None`` -> ``""``).

    The lifecycle component's single field normalizer, defined here (the lowest leaf)
    and re-exported by :mod:`...lane_lifecycle_model` so every historical importer of
    ``lane_lifecycle_model.norm`` is unaffected.
    """
    return str(value).strip() if value is not None else ""


class ProcessPinError(ValueError):
    """A typed process-generation pin is unusable; fail closed (never degraded)."""


#: The declared-slot snapshot envelope version (Redmine #13810). Bumped when the pin
#: shape changes so an older build reading a newer snapshot fails closed rather than
#: dropping fields it does not understand.
DECLARED_SLOTS_VERSION = 1


@dataclass(frozen=True)
class ProcessGenerationPin:
    """One provider-bound slot as observed when a lane generation was declared.

    The richer successor to :class:`ReleasePin` (Redmine #13810, Design Answer j#78386):
    a slot is matched not by ``locator`` alone but by the whole tuple
    ``(role, provider, assigned_name, locator, runtime_revision)`` — a slot recycled into
    a *new* provider process, or the same name re-launched at a newer runtime revision, is
    a different pin and is never actuated on a stale approval.

    ``role`` / ``provider`` / ``assigned_name`` are the stable identity and ``locator`` is
    the live-generation evidence — all four are required, because a pin missing any of them
    cannot express the identity the action-time preflight re-resolves against the live
    inventory, so it is refused rather than stored as an un-actionable slot (the
    :class:`ReleasePin` R1-F4 discipline, extended).

    ``runtime_revision`` and ``attested_at`` are supplementary *evidence, not identity*, so
    both may be empty (Redmine #13810 R3-F1): the herdr process-generation discriminant is
    the **live locator** (``herdr-native-identity.md`` / the startup self-attestation store,
    which deliberately records NO runtime version), not a runtime-version string. A live
    adopt whose inventory carries only the slot name + locator declares a pin with an empty
    ``runtime_revision`` — an honest "not observed", never a fabricated version — and the
    locator alone still distinguishes a recycled generation. A caller that DOES observe a
    runtime revision (a richer declaration surface) may supply it and it enters
    :attr:`match_key`.

    This is a **declaration snapshot**, never a liveness fact: whether the slot still
    exists is re-read from the live Herdr inventory every time (``managed-state-model.md``
    ``### 正本境界``). ``declared_slots`` is "what was observed / authorized then".
    """

    role: str
    provider: str
    assigned_name: str
    locator: str
    runtime_revision: str = ""
    attested_at: str = ""

    def __post_init__(self) -> None:
        for name in ("role", "provider", "assigned_name", "locator", "runtime_revision",
                     "attested_at"):
            object.__setattr__(self, name, norm(getattr(self, name)))
        missing = [
            name
            for name in ("role", "provider", "assigned_name", "locator")
            if not getattr(self, name)
        ]
        if missing:
            raise ProcessPinError(
                "a process generation pin requires a non-empty role / provider / "
                "assigned_name / locator "
                f"(missing: {', '.join(missing)}); an unresolvable slot is never pinned"
            )

    @property
    def stable_identity(self) -> tuple[str, str, str]:
        """The provider-bound ``(role, provider, assigned_name)`` slot identity."""
        return (self.role, self.provider, self.assigned_name)

    @property
    def match_key(self) -> tuple[str, str, str, str, str]:
        """The full tuple the actuator re-resolves against a live process (evidence)."""
        return (self.role, self.provider, self.assigned_name, self.locator,
                self.runtime_revision)

    def binds_same_generation(self, live: "ProcessGenerationPin") -> bool:
        """Same process generation as ``live`` (#13846): bind on the four identity fields;
        ``runtime_revision`` is supplementary evidence, so an empty revision on either side is
        never a discriminant and only a both-observed mismatch (a re-launched newer generation)
        fails closed. Full ``match_key`` equality would wrongly reject a fresh generation whose
        declared revision is empty while the live row surfaces one (the #13846 false conflict)."""
        if (self.role, self.provider, self.assigned_name, self.locator) != (
            live.role, live.provider, live.assigned_name, live.locator
        ):
            return False
        if self.runtime_revision and live.runtime_revision:
            return self.runtime_revision == live.runtime_revision
        return True

    def as_payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "runtime_revision": self.runtime_revision,
            "attested_at": self.attested_at,
        }


def validate_declared_slots(
    slots: Sequence[ProcessGenerationPin],
) -> tuple[ProcessGenerationPin, ...]:
    """The provider-bound slots a declaration may carry (no duplicate slot identity).

    Two pins sharing a ``(role, provider, assigned_name)`` identity would make the
    declared set ambiguous — which locator/runtime revision is authoritative for that
    slot? Reject rather than pick (the :func:`validate_release_pins` discipline). An
    *empty* set is allowed here: an issue lane declares no slots at create time; the
    per-binding-kind requirement (a project gateway must declare its slot set) is the
    declaration service's, not this pure validator's.
    """
    declared = tuple(slots)
    seen: set[tuple[str, str, str]] = set()
    for pin in declared:
        if pin.stable_identity in seen:
            raise ProcessPinError(
                f"duplicate declared slot {pin.stable_identity!r} in one generation"
            )
        seen.add(pin.stable_identity)
    return declared


def encode_declared_slots(slots: Sequence[ProcessGenerationPin]) -> str:
    """Serialize the declared slot set as a versioned envelope (deterministic).

    Empty slots serialize to ``""`` (an issue lane with no declared slots), so a v5 row
    is byte-identical to the migrated pre-v5 default and the round-trip is stable.
    """
    declared = tuple(slots)
    if not declared:
        return ""
    return json.dumps(
        {
            "version": DECLARED_SLOTS_VERSION,
            "slots": [
                p.as_payload()
                for p in sorted(declared, key=lambda p: p.stable_identity)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def decode_declared_slots(raw: str) -> tuple[ProcessGenerationPin, ...]:
    """Read the declared slot set back. Empty means none; corrupt / unknown **raises**.

    Fail-closed like :func:`decode_release_pins`: a malformed or newer-versioned snapshot
    must never decode to a *shorter* / dropped-field slot list, which would let a caller
    believe it had authorized fewer slots than the row records. An unreadable snapshot is
    a fail-closed condition, not a degraded one.
    """
    if not norm(raw):
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProcessPinError(f"declared slots are not readable JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProcessPinError("declared slots must be a versioned object")
    version = loaded.get("version")
    # An EXACT integer version only (Redmine #13810 R1-F4): Python folds ``True == 1`` and
    # ``1.0 == 1``, so a bare ``version != DECLARED_SLOTS_VERSION`` would accept a JSON
    # ``true`` / ``1.0`` as v1 — the same closed-schema trap #13754 R4 fixed for the
    # component version. ``bool`` is an ``int`` subclass and is not a version.
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != DECLARED_SLOTS_VERSION
    ):
        raise ProcessPinError(
            f"declared slots version {version!r} is not exactly {DECLARED_SLOTS_VERSION} "
            "(unknown / newer / malformed snapshot); fail closed"
        )
    slots = loaded.get("slots")
    if not isinstance(slots, list):
        raise ProcessPinError("declared slots envelope has no slot list")
    pins: list[ProcessGenerationPin] = []
    for item in slots:
        if not isinstance(item, dict):
            raise ProcessPinError(f"declared slot is not an object: {item!r}")
        pins.append(
            ProcessGenerationPin(
                role=norm(item.get("role")),
                provider=norm(item.get("provider")),
                assigned_name=norm(item.get("assigned_name")),
                locator=norm(item.get("locator")),
                runtime_revision=norm(item.get("runtime_revision")),
                attested_at=norm(item.get("attested_at")),
            )
        )
    return tuple(pins)


__all__ = (
    "DECLARED_SLOTS_VERSION",
    "norm",
    "ProcessGenerationPin",
    "ProcessPinError",
    "decode_declared_slots",
    "encode_declared_slots",
    "validate_declared_slots",
)
