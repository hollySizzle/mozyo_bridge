"""Caller-supplied durable launch context for a herdr lane (Redmine #13647).

The pure, immutable value the create / heal *boundary* resolves from durable
governance facts and hands to the launch path (``prepare_session``), so the
launch chokepoint reads a *pre-resolved* context instead of inferring a lane's
workflow position from provider / pane / display cache (Design Answer j#85645,
disposition j#85650).

Authority model (kept enforced by what this type does and does NOT carry):

- **`lane_kind` is the caller's durable governance fact, not a guess.** A fresh
  create resolves it from the Redmine issue parent link + dispatch / Start journal
  (the same governance inputs as ``DelegationSource``); a heal reads it from the
  generation-bound lifecycle authority record. It is NEVER derived from
  ``lane_metadata`` / ``@mozyo_*`` (display caches) or from provider / pane
  proximity (disposition j#85650 P1).
- **Absent is a legitimate state, not an error.** ``lane_kind is None`` means the
  caller has no durable kind fact (a legacy / bare launch); the launch path then
  resolves placement geometry by ``lane_class`` — the byte-for-byte pre-#13647
  fallback the issue's close condition fixes. A *present* value is always a
  canonical :data:`~mozyo_bridge.core.state.lane_kind.LANE_KINDS` token or fails closed.
- **Geometry / plan input only.** This context selects placement geometry (Tranche
  1) and — later — per-slot role→profile (Tranche 2). It is never promoted to an
  mzb1 assigned name, ``MOZYO_AGENT_ROLE`` (a provider token), or a
  route / attestation / retire authority; those stay provider-token-bound.

Pure: a frozen dataclass validated on construction; no I/O. Tranche 2 adds the
per-slot role-profile axis (``source_anchor`` / ``slot_specs``); both default to
absent, so a geometry-only or context-free launch stays byte-invariant.
"""

from __future__ import annotations

from collections import abc
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.core.state.lane_kind import optional_lane_kind
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_plan import (  # noqa: E501
    LaneLaunchPlanError,
    SlotLaunchSpec,
)


def _kind(value: object) -> str:
    """The type name of a caller's value, for a message — never a way to fail (j#86049)."""
    try:
        return type(value).__name__
    except Exception:  # pragma: no cover - a type whose own name cannot be read
        return "<unnameable type>"


def _shown(value: object) -> str:
    """``repr(value)`` for a refusal message, unable to change the OUTCOME (j#86049).

    Building a refusal must not re-enter the caller's code in a way that can replace the
    refusal: ``repr()`` runs ``__repr__``, ``f"{exc}"`` runs ``__str__``, and either raising
    turns this carrier's typed refusal into a raw exception. Re-stated here rather than
    imported from the plan module for the same reason its retype is: one module's guard must
    not be what makes the other module's tests pass.
    """
    try:
        # The base method explicitly, never `str()` / `+` / slicing: those dispatch on the
        # caller's own type, which is how a `str` subclass's `__radd__` escaped (j#86068).
        return str.__str__(repr(value))
    except Exception:
        return f"<unprintable {_kind(value)}>"


def _checked_elements(value: object, expected: type, *, field: str) -> tuple:
    """An owned ordered tuple whose every element is ``expected``, or a typed refusal.

    Ordered because ``slot_specs`` carries the pair's slot order; a set would make the
    context's meaning depend on iteration order. The refusal type is the plan module's, so
    the launch's single ``except`` still turns every plan-shaped defect into a typed
    zero-start.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, abc.Sequence):
        raise LaneLaunchPlanError(
            f"{field} must be an ordered sequence of {expected.__name__}, got "
            f"{_kind(value)}"
        )
    # Reading the caller's sequence can itself fail even when its SHAPE is impeccable — an
    # `abc.Sequence` whose `__iter__` raises satisfies every guard above and then explodes
    # here (review j#86008). That refusal has to arrive as the plan module's error like every
    # other one, or the launch's single `except` misses it. Deliberately re-stated here
    # rather than shared with the plan module's helper: these are two boundaries, and one
    # module's retype must not be what makes the other module's tests pass.
    try:
        owned = tuple(value)
    except Exception as exc:  # not BaseException: an interrupt is not a refusal
        raise LaneLaunchPlanError(
            f"{field} could not be read ({_kind(exc)}); the context is not built from "
            "an input it cannot even materialise"
        ) from exc
    for element in owned:
        if not isinstance(element, expected):
            raise LaneLaunchPlanError(
                f"{field} entry {_shown(element)} is {_kind(element)}, not "
                f"{expected.__name__}"
            )
    return owned


@dataclass(frozen=True)
class LaneLaunchContext:
    """One lane launch's caller-resolved durable context (pure value).

    :attr:`lane_kind` is the canonical delegation-geometry token
    (``coordinator`` / ``delegated_coordinator`` / ``implementation``) the caller
    resolved from durable governance, or ``None`` when it has no durable kind fact
    (the launch path then falls back to ``lane_class`` geometry). A present value
    that is not a canonical token fails closed on construction.
    """

    lane_kind: Optional[str] = None
    #: The durable governance record(s) this context was resolved from (Tranche 2). A
    #: caller that resolved the plan from more than one *different* anchor is refused at
    #: plan time rather than guessing which decision authorizes the launch; one anchor (or
    #: several naming the same record) resolves. Empty for a geometry-only context.
    anchors: tuple[DecisionPointer, ...] = ()
    #: The per-slot role -> profile -> provider -> argv intents this launch must satisfy
    #: (Tranche 2). Empty (the default) means the caller supplies no role-bearing plan and
    #: the launch is byte-for-byte the pre-#13647 one.
    slot_specs: tuple[SlotLaunchSpec, ...] = ()

    def __post_init__(self) -> None:
        # Normalize + fail-closed validate the one field: a present value must be a
        # canonical lane-kind token; absent (None / "") stays None (no-kind marker).
        object.__setattr__(
            self,
            "lane_kind",
            optional_lane_kind(self.lane_kind, source="LaneLaunchContext.lane_kind"),
        )

        # This value is an AUTHORITY carrier, so it verifies what it carries (review
        # j#85875 F2): a list of look-alike strings used to become the context's governance
        # anchors, and a non-SlotLaunchSpec entry travelled as far as the resolver. The
        # module docstring has claimed "validated on construction" since Tranche 1; these
        # two fields now honour it. The plan resolver keeps its own independent guards —
        # each boundary is separately tested so neither masks the other's loss.
        object.__setattr__(
            self,
            "anchors",
            _checked_elements(
                self.anchors, DecisionPointer, field="LaneLaunchContext.anchors"
            ),
        )
        object.__setattr__(
            self,
            "slot_specs",
            _checked_elements(
                self.slot_specs, SlotLaunchSpec, field="LaneLaunchContext.slot_specs"
            ),
        )

    @property
    def has_lane_kind(self) -> bool:
        """True iff a durable lane-kind fact was supplied (a concrete token)."""
        return self.lane_kind is not None

    @property
    def has_slot_plan(self) -> bool:
        """True iff the caller supplied a role-bearing per-slot plan (Tranche 2)."""
        return bool(self.slot_specs)


__all__ = ("LaneLaunchContext",)
