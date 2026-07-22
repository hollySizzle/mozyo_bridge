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

Pure: a frozen dataclass validated on construction; no I/O. The per-slot
role-profile fields (``source_anchor`` / ``slot_specs``) land in Tranche 2 —
this Tranche-1 shape carries only the geometry axis so the default (no context)
stays byte-invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.core.state.lane_kind import optional_lane_kind


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

    def __post_init__(self) -> None:
        # Normalize + fail-closed validate the one field: a present value must be a
        # canonical lane-kind token; absent (None / "") stays None (no-kind marker).
        object.__setattr__(
            self,
            "lane_kind",
            optional_lane_kind(self.lane_kind, source="LaneLaunchContext.lane_kind"),
        )

    @property
    def has_lane_kind(self) -> bool:
        """True iff a durable lane-kind fact was supplied (a concrete token)."""
        return self.lane_kind is not None


__all__ = ("LaneLaunchContext",)
