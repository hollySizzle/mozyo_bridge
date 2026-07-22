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

from dataclasses import dataclass, field
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_kind import optional_lane_kind
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_plan import (  # noqa: E501
    SlotLaunchSpec,
)


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

        object.__setattr__(self, "anchors", tuple(self.anchors))
        object.__setattr__(self, "slot_specs", tuple(self.slot_specs))

    @property
    def has_lane_kind(self) -> bool:
        """True iff a durable lane-kind fact was supplied (a concrete token)."""
        return self.lane_kind is not None

    @property
    def has_slot_plan(self) -> bool:
        """True iff the caller supplied a role-bearing per-slot plan (Tranche 2)."""
        return bool(self.slot_specs)


__all__ = ("LaneLaunchContext",)
