"""Pure action-time classifier for the hibernated live-contradiction reconcile (#13842).

The fail-closed decision half of the reconcile: given content-free facts about the lane's
expected managed pair as observed in the live Herdr inventory RIGHT NOW, decide whether that
pair is the exact, unique, idle, settled, attested pair the reconcile may re-bind and close —
or whether some axis fails, in which case nothing is written / closed.

This module holds NO IO: the application adapter gathers the per-slot facts (candidate
multiplicity, slot liveness, startup self-attestation, runtime receiver-state, and a
content-free pending-composer observation) and hands them here as a :class:`PairObservation`.
The composer body never crosses this boundary — only the ``has_pending`` fact does, exactly
as the #13763 pending-composer classifier keeps it.

Precedence is fail-closed (a lane is reconcilable only when EVERY axis holds):

1. an unreadable inventory proves nothing (never folded to "no pair");
2. a foreign (non-managed) provider standing at the lane's own position is a substitution —
   never reconciled past;
3. zero expected slots present is a **positive absence** (:data:`STATE_ABSENT`) — the caller
   decides between an owed-retirement resume and routing to the #13841 live-zero migration;
4. a partial pair (some present, some absent), a duplicate assigned name, a stale shell
   residue, a missing locator, an unattested slot, a busy / blocked / unknown (not idle /
   turn-ended) agent, or a pending composer each fails closed with its own reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
)

# -- verdict state + reason vocabulary ---------------------------------------

#: Every expected managed slot is present, unique, live, idle / turn-ended, settled, and its
#: startup self-attestation is generation-bound to the live locator — the pair may be re-bound
#: and closed.
STATE_GREEN = "green"
#: Zero expected managed slots are present in a readable inventory — a positive absence. NOT a
#: success on its own: the caller resumes an owed retirement (a closed partial replay) or
#: routes to the #13841 live-zero migration, never fabricating a live pair.
STATE_ABSENT = "absent"
#: Some axis failed closed; ``reason`` names it. Nothing is written / closed.
STATE_BLOCKED = "blocked"

#: The live inventory could not be read — liveness cannot be measured (never "no pair").
RECON_INVENTORY_UNREADABLE = "inventory_unreadable"
#: A non-managed provider stands at the lane's own ``(workspace, lane)`` position: a
#: substitution the reconcile never closes past (foreign provider / name).
RECON_FOREIGN_PROVIDER = "foreign_provider"
#: Some expected slot is present and some absent — a partial / half pair.
RECON_PAIR_INCOMPLETE = "pair_incomplete"
#: An expected assigned name matches more than one live row (a herdr name-uniqueness
#: violation), or two slots share one locator — ambiguous, never guessed past.
RECON_PAIR_AMBIGUOUS = "pair_ambiguous"
#: An expected slot is a stale shell residue (a name-matched row with no live agent).
RECON_SLOT_STALE = "slot_stale"
#: An expected slot's row carries no usable pane locator.
RECON_SLOT_MISSING_LOCATOR = "slot_missing_locator"
#: An expected slot's startup self-attestation is absent / stale / missing / conflicting.
RECON_IDENTITY_UNATTESTED = "identity_unattested"
#: An expected slot's agent is not idle / turn-ended (busy, blocked, or an unknown runtime).
RECON_AGENT_NOT_IDLE = "agent_not_idle"
#: An expected slot has (or may have) a pending composer — a working / pending / unreadable
#: composer the reconcile must never close over.
RECON_PENDING_COMPOSER = "pending_composer"

RECON_BLOCKED_REASONS = frozenset(
    {
        RECON_INVENTORY_UNREADABLE,
        RECON_FOREIGN_PROVIDER,
        RECON_PAIR_INCOMPLETE,
        RECON_PAIR_AMBIGUOUS,
        RECON_SLOT_STALE,
        RECON_SLOT_MISSING_LOCATOR,
        RECON_IDENTITY_UNATTESTED,
        RECON_AGENT_NOT_IDLE,
        RECON_PENDING_COMPOSER,
    }
)

#: The runtime receiver-states an idle, drivable managed agent may be in for a reconcile: it
#: is quiet awaiting input, or its assistant turn finished. ``busy`` / ``blocked`` / ``unknown``
#: are NOT settled — an actuator must never close a pair mid-turn or over an unreadable runtime.
_SETTLED_RUNTIME_STATES = frozenset({RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED})


@dataclass(frozen=True)
class SlotObservation:
    """Content-free facts about ONE expected managed slot at action time.

    ``role`` is the workflow role (``gateway`` / ``worker``); ``provider`` is the bound
    provider (``codex`` / ``claude``) — the mzb1 ``role`` segment. ``candidate_count`` is how
    many live rows carry this slot's expected assigned name (0 = absent, 1 = unique, >1 =
    ambiguous). The remaining facts describe the single unique candidate (meaningful only when
    ``candidate_count == 1``): ``slot_live`` is :func:`classify_named_slot` != stale;
    ``locator`` / ``assigned_name`` / ``attested_at`` carry the pin identity; ``attested`` is
    the startup self-attestation join; ``runtime_state`` is the herdr runtime receiver-state;
    and ``composer_readable`` / ``has_pending`` are the content-free pending-composer facts
    (``has_pending`` ``None`` = unreadable, never read as "no pending").
    """

    role: str
    provider: str
    candidate_count: int
    slot_live: bool = False
    locator: str = ""
    assigned_name: str = ""
    attested_at: str = ""
    attested: bool = False
    runtime_state: str = ""
    composer_readable: bool = False
    has_pending: Optional[bool] = None

    @property
    def present(self) -> bool:
        return self.candidate_count >= 1

    @property
    def agent_idle(self) -> bool:
        return self.runtime_state in _SETTLED_RUNTIME_STATES

    @property
    def composer_settled(self) -> bool:
        return self.composer_readable and self.has_pending is False


@dataclass(frozen=True)
class PairObservation:
    """The action-time observation of a lane's whole expected managed pair.

    ``inventory_readable`` is the single gating fact that the live inventory was read at all;
    ``foreign_at_position`` is True when a non-managed provider decoded to the lane's own
    ``(workspace, lane)`` position (a substitution); ``slots`` is one
    :class:`SlotObservation` per expected managed provider role.
    """

    inventory_readable: bool
    foreign_at_position: bool = False
    slots: tuple[SlotObservation, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReconcilePairVerdict:
    """The fail-closed verdict: :data:`STATE_GREEN` / :data:`STATE_ABSENT` / a blocked reason."""

    state: str
    reason: str = ""

    @property
    def green(self) -> bool:
        return self.state == STATE_GREEN

    @property
    def absent(self) -> bool:
        return self.state == STATE_ABSENT


def _blocked(reason: str) -> ReconcilePairVerdict:
    return ReconcilePairVerdict(state=STATE_BLOCKED, reason=reason)


def decide_pair_reconcile(observation: PairObservation) -> ReconcilePairVerdict:
    """Decide whether the observed expected pair may be re-bound and closed (pure, #13842).

    Fail-closed precedence (see the module docstring). Returns :data:`STATE_GREEN` only when
    every expected slot is present, unique, live, idle / turn-ended, settled (no pending
    composer), and generation-bound attested; :data:`STATE_ABSENT` for a positive zero-present
    inventory; otherwise :data:`STATE_BLOCKED` with the failing reason.
    """
    if not observation.inventory_readable:
        return _blocked(RECON_INVENTORY_UNREADABLE)
    if observation.foreign_at_position:
        return _blocked(RECON_FOREIGN_PROVIDER)
    slots = observation.slots
    present = [slot for slot in slots if slot.present]
    if not present:
        # A positive absence: the inventory is readable and no expected slot is live.
        return ReconcilePairVerdict(state=STATE_ABSENT)
    if len(present) != len(slots):
        # Some expected slot is present while another is absent — a half pair.
        return _blocked(RECON_PAIR_INCOMPLETE)
    if any(slot.candidate_count > 1 for slot in slots):
        return _blocked(RECON_PAIR_AMBIGUOUS)
    if any(not slot.slot_live for slot in slots):
        return _blocked(RECON_SLOT_STALE)
    if any(not slot.locator for slot in slots):
        return _blocked(RECON_SLOT_MISSING_LOCATOR)
    if any(not slot.attested for slot in slots):
        return _blocked(RECON_IDENTITY_UNATTESTED)
    if any(not slot.agent_idle for slot in slots):
        return _blocked(RECON_AGENT_NOT_IDLE)
    if any(not slot.composer_settled for slot in slots):
        return _blocked(RECON_PENDING_COMPOSER)
    locators = [slot.locator for slot in slots]
    if len(set(locators)) != len(locators):
        # Two managed slots resolved to one locator — an ambiguous / recycled target.
        return _blocked(RECON_PAIR_AMBIGUOUS)
    return ReconcilePairVerdict(state=STATE_GREEN)


__all__ = (
    "STATE_GREEN",
    "STATE_ABSENT",
    "STATE_BLOCKED",
    "RECON_INVENTORY_UNREADABLE",
    "RECON_FOREIGN_PROVIDER",
    "RECON_PAIR_INCOMPLETE",
    "RECON_PAIR_AMBIGUOUS",
    "RECON_SLOT_STALE",
    "RECON_SLOT_MISSING_LOCATOR",
    "RECON_IDENTITY_UNATTESTED",
    "RECON_AGENT_NOT_IDLE",
    "RECON_PENDING_COMPOSER",
    "RECON_BLOCKED_REASONS",
    "SlotObservation",
    "PairObservation",
    "ReconcilePairVerdict",
    "decide_pair_reconcile",
)
