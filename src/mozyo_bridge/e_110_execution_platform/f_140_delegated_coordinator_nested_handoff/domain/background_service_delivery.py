"""Background-service callback delivery authority (Redmine #13683 R2-F3 design answer j#77216, Model A').

The coordinator's design answer selected **Model A'**: the workspace callback supervisor delivers
callbacks as a dedicated ``background_service`` authority — NOT an agent :class:`SenderIdentity`
(``claude`` / ``codex`` provider). This module is the **pure** core of that authority: the origin
class, the resolved target tuple, and the fail-closed authorization decision. All I/O (the lease
read, the route re-resolution, the transport) lives in the application sender.

Fixed boundaries the design answer pins (j#77216), enforced here:

- **not a provider role** (boundary 1): the origin is :data:`BACKGROUND_SERVICE_ORIGIN`, distinct
  from the agent provider vocabulary — it is never injected as ``MOZYO_AGENT_ROLE`` and never added
  to ``resolve_sender_identity``'s role set.
- **lease + claim authority** (boundary 2): a delivery is authorized ONLY when the caller holds a
  valid workspace supervisor lease AND the row carries a durable outbox claim token for the same
  workspace partition. Either missing/expired -> zero-send.
- **persisted rows only** (boundary 3): the authority delivers an existing classifier-produced,
  outbox-persisted callback row — it can never originate an arbitrary body / issue / target / new
  request. (Enforced by construction: the sender only ever handles :class:`CallbackOutboxRow`s the
  processor claimed.)
- **re-resolve the target, fail-closed** (boundary 4): the exact target is re-resolved against the
  route ledger + live inventory immediately before send; 0 / multiple / foreign-workspace /
  generation-mismatch -> fail-closed.

The authorization is a pure function over already-observed facts (has_lease / has_claim / the
resolution result), so every fail-closed branch is deterministically testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

#: The delivery origin class for a supervisor-originated callback. Deliberately NOT a member of the
#: agent provider vocabulary (``claude`` / ``codex``): a background service is not a lane agent, so
#: it never presents an agent sender identity (design answer j#77216 boundary 1).
BACKGROUND_SERVICE_ORIGIN = "background_service"

# ---------------------------------------------------------------------------
# Authorization reason vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

AUTH_OK = "authorized"
AUTH_NO_LEASE = "no_workspace_lease"  # the caller does not hold a valid supervisor lease
AUTH_NO_CLAIM = "no_outbox_claim"  # the row carries no durable outbox claim token
AUTH_FOREIGN_WORKSPACE = "foreign_workspace"  # the row / resolved target is a different workspace
AUTH_NO_TARGET = "no_target_resolved"  # the route re-resolution found no live target
AUTH_AMBIGUOUS_TARGET = "ambiguous_target"  # the route re-resolution found more than one target
AUTH_ANCHOR_MISMATCH = "anchor_mismatch"  # the resolved target's issue/journal != the row's anchor
AUTH_GENERATION_MISMATCH = "generation_mismatch"  # the resolved target's generation is unknown/stale

#: The fail-closed reasons — every one is a deterministic zero-send (nothing was delivered).
FAIL_CLOSED_REASONS = frozenset(
    {
        AUTH_NO_LEASE,
        AUTH_NO_CLAIM,
        AUTH_FOREIGN_WORKSPACE,
        AUTH_NO_TARGET,
        AUTH_AMBIGUOUS_TARGET,
        AUTH_ANCHOR_MISMATCH,
        AUTH_GENERATION_MISMATCH,
    }
)


@dataclass(frozen=True)
class DeliveryTarget:
    """A re-resolved exact delivery target tuple (design answer j#77216 boundary 4).

    ``workspace_id`` / ``lane`` / ``receiver`` (a binding-resolved provider or receiver role) /
    ``issue`` + ``journal`` (the source anchor) / ``generation`` (correlation, empty when the row
    carries none — forward-compatible with #13684 without a schema column). ``locator`` is the live
    pane/agent id the route authority resolved (evidence, re-resolved at send time — never a stored
    routing authority).
    """

    workspace_id: str
    lane: str
    receiver: str
    issue: str
    journal: str
    generation: str = ""
    locator: str = ""


@dataclass(frozen=True)
class TargetResolution:
    """The result of re-resolving a row's route against the ledger + live inventory (evidence).

    ``targets`` is every live target the route authority matched — 0 (nothing live), 1 (the exact
    route), or >1 (ambiguous). The authorization treats anything but exactly one same-workspace
    target as fail-closed.
    """

    targets: tuple[DeliveryTarget, ...] = ()

    @classmethod
    def of(cls, targets: Sequence[DeliveryTarget]) -> "TargetResolution":
        return cls(targets=tuple(targets))


@dataclass(frozen=True)
class BackgroundDeliveryDecision:
    """The authorization outcome: whether to deliver, why not, and the resolved target if authorized."""

    authorized: bool
    reason: str
    target: Optional[DeliveryTarget] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "authorized": self.authorized,
            "reason": self.reason,
            "target_workspace": self.target.workspace_id if self.target else "",
            "target_receiver": self.target.receiver if self.target else "",
        }


def authorize_background_delivery(
    *,
    expected_workspace: str,
    row_workspace: str,
    row_issue: str = "",
    row_journal: str = "",
    has_lease: bool,
    has_claim: bool,
    resolution: TargetResolution,
    expected_generation: str = "",
) -> BackgroundDeliveryDecision:
    """Decide whether a background-service delivery is authorized (pure, fail-closed).

    Ordered, fail-closed checks (design answer j#77216 boundaries 2 + 4):

    1. the row must belong to the workspace this authority owns (a foreign row is never delivered);
    2. the caller must hold a valid workspace supervisor lease (:data:`AUTH_NO_LEASE` otherwise);
    3. the row must carry a durable outbox claim token (:data:`AUTH_NO_CLAIM` otherwise);
    4. the route re-resolution must yield **exactly one** live target in this workspace — 0
       (:data:`AUTH_NO_TARGET`), >1 (:data:`AUTH_AMBIGUOUS_TARGET`), or a foreign-workspace target
       (:data:`AUTH_FOREIGN_WORKSPACE`) all fail closed;
    5. the resolved target's **source anchor** (issue + journal) must exact-match the row's durable
       anchor (:data:`AUTH_ANCHOR_MISMATCH` otherwise) — a re-resolution that drifted to a different
       issue / journal is never delivered (review R3-F3: the delivery is bound to the row's anchor,
       not a resolver-supplied one);
    6. generation is **strict**: when the row expects a generation, the resolved target must carry
       exactly that generation (:data:`AUTH_GENERATION_MISMATCH` otherwise — an unknown / empty /
       stale target generation fails closed). No expectation = no constraint (forward hook for
       #13684's correlated generation).

    Only when every check passes is the delivery :data:`AUTH_OK` with the single resolved target.
    """
    expected = str(expected_workspace or "").strip()
    if str(row_workspace or "").strip() != expected or not expected:
        return BackgroundDeliveryDecision(False, AUTH_FOREIGN_WORKSPACE)
    if not has_lease:
        return BackgroundDeliveryDecision(False, AUTH_NO_LEASE)
    if not has_claim:
        return BackgroundDeliveryDecision(False, AUTH_NO_CLAIM)
    targets = resolution.targets
    if not targets:
        return BackgroundDeliveryDecision(False, AUTH_NO_TARGET)
    if len(targets) > 1:
        return BackgroundDeliveryDecision(False, AUTH_AMBIGUOUS_TARGET)
    target = targets[0]
    if str(target.workspace_id or "").strip() != expected:
        return BackgroundDeliveryDecision(False, AUTH_FOREIGN_WORKSPACE)
    # Bind the delivery to the ROW's durable anchor — a re-resolution that returns a different
    # issue / journal is not this row's callback and is never delivered (R3-F3).
    if (
        str(target.issue or "").strip() != str(row_issue or "").strip()
        or str(target.journal or "").strip() != str(row_journal or "").strip()
    ):
        return BackgroundDeliveryDecision(False, AUTH_ANCHOR_MISMATCH)
    want_gen = str(expected_generation or "").strip()
    if want_gen and want_gen != str(target.generation or "").strip():
        # Strict: an expected generation must match exactly — an unknown / empty / stale target
        # generation fails closed (R3-F3), never authorized.
        return BackgroundDeliveryDecision(False, AUTH_GENERATION_MISMATCH)
    return BackgroundDeliveryDecision(True, AUTH_OK, target)


__all__ = (
    "BACKGROUND_SERVICE_ORIGIN",
    "AUTH_OK",
    "AUTH_NO_LEASE",
    "AUTH_NO_CLAIM",
    "AUTH_FOREIGN_WORKSPACE",
    "AUTH_NO_TARGET",
    "AUTH_AMBIGUOUS_TARGET",
    "AUTH_ANCHOR_MISMATCH",
    "AUTH_GENERATION_MISMATCH",
    "FAIL_CLOSED_REASONS",
    "DeliveryTarget",
    "TargetResolution",
    "BackgroundDeliveryDecision",
    "authorize_background_delivery",
)
