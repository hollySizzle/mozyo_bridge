"""When a run of stall observations is allowed to wake the coordinator (Redmine #15855).

:mod:`...domain.stall_disposition` answers "what is this screen doing, and what would a
remedy be". That is a per-pass verdict about one screen. This module answers the *next*
question, which no single pass can answer: **has this unit been stuck long enough, and
consistently enough, that a human-facing escalation is justified?**

The separation is the whole point. #15843 shipped the sensor and stopped deliberately at
``present_only`` — the watcher reports and an actor with authority decides. #15855 is the
operational wiring that gives a timer-driven watcher a way to reach that actor, and the
risk it introduces is new: a classifier that was safe to be wrong about (nobody was
listening) becomes a classifier that can page a coordinator every five minutes. So the
escalation gate is deliberately *narrower* than the classifier, and everything below
exists to keep it that way.

Three rules shape the fold, all traceable to the issue's acceptance conditions:

**A verdict fires the gate, never raw screen-sameness.** #15855 acceptance 2 says the
trigger is the *classifier's* verdict, not "the screen did not change"; #15843's spec
`## センサー: 3 値であって boolean ではない` is the reason. :data:`ESCALATION_EFFECTS`
maps every one of the nine declared classes onto exactly one effect, and only
:data:`STREAK_ADVANCE` classes can ever accumulate toward a threshold.
``busy_likely`` — reasoning, a tool call, a long test run — resets, so a lane that is
working cannot be aged into an escalation no matter how long it works.

**Absence of evidence is not evidence.** ``screen_unreadable`` and ``unknown`` are
:data:`STREAK_HOLD`: they neither advance nor reset. Advancing on them would let a wedged
*reader* manufacture a stall verdict about a unit nobody could see; resetting on them
would let one unreadable sample erase a genuine five-pass stall. Holding is the only
reading consistent with ``stall_disposition``'s own words ("unreadable is evidence in
neither direction"), and it has a mechanical consequence worth stating: **a target that is
only ever unreadable can never reach the threshold**, because nothing ever increments it.

**The streak is per class, not per target.** A unit that is classified
``content_refusal``, then ``unsent_composer``, then ``content_refusal`` again has a screen
that is not moving, but a *diagnosis that is flapping* — and the prescriptions for those
two classes are mutually destructive (``stall_disposition`` module docstring). Counting
that as three consecutive detections would escalate on the strength of an unstable
classification. :func:`fold_observation` restarts the count whenever the class changes, so
the threshold means "the same positively-identified stall, N times running".

Identity: what a streak is actually *about*
-------------------------------------------
A run is bound to a :class:`WatchIdentity` — ``workspace_id`` + ``lane_id`` + ``role``,
carrying the terminal ``generation`` — and **not** to a pane locator. This follows the
identity discipline the routing layer already enforces (``backend_neutral_resolver``: the
stable key is ``(workspace_id, lane_id, role, pane_name)`` and "the transient herdr locator
rides on ``id`` — cache / evidence only, never the identity"). Two failures follow directly
from getting this wrong, and both are the kind a watcher would report as a stall:

- a locator is **reused**. Pane ids are recycled; a run accumulated against a dead unit's
  locator would keep counting against whatever landed on it next, and escalate about a
  lane that was never stuck.
- a locator **changes** for a unit that did not. A relaunch or a rebind moves the same
  logical agent to a new locator, and a locator-keyed run would silently restart — so a
  unit that has been wedged across a rebind could never reach any threshold.

``generation`` is bound rather than merely recorded: a **generation change restarts the
run**, exactly as a class change does. A new generation is a new process with a new screen;
inheriting the old run would let a fresh agent be escalated for its predecessor's stall.
The identity's ``target`` field is the transient locator, kept for evidence only — nothing
in this module compares it.

Latching, and the residual it leaves
------------------------------------
A streak that reaches the threshold fires **once** (:attr:`StreakState.escalated_at` is
the latch) and keeps counting without firing again. A stall that outlives the
coordinator's attention therefore produces one escalation, not one every tick.

That is a deliberate trade with a named residual: **this layer will not re-escalate an
unacknowledged escalation.** Re-firing on a cadence is how a watcher becomes noise that
gets muted, and a muted watcher is worse than none — but it does mean a dropped escalation
is not retried *here*. The recovery is the durable record: the escalation row stays open
with a climbing :attr:`StreakState.consecutive`, and closing that loop is the consuming
actor's obligation, not something this layer may infer (ADR-0014 — the upper layer
recovers facts and never guesses completion). Do not "fix" this by adding a re-fire
interval without deciding what acknowledgement means first.

This module is pure: no clock, no store, no I/O. ``observed_at`` is supplied by the caller
and every decision is a function of (previous state, class, threshold).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_BUSY_LIKELY,
    CLASS_CONTENT_REFUSAL,
    CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
    CLASS_SCREEN_PROGRESSING,
    CLASS_SCREEN_UNREADABLE,
    CLASS_STARTUP_INTERACTION,
    CLASS_UNKNOWN,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    CLASS_UNSENT_COMPOSER,
    STALL_CLASSES,
)

# --------------------------------------------------------------------------------------
# Effect vocabulary
# --------------------------------------------------------------------------------------

#: The class is a positively-identified stall. Count it toward the threshold.
STREAK_ADVANCE = "advance"
#: Positive evidence the unit is alive. Clear the streak and the latch.
STREAK_RESET = "reset"
#: Evidence in neither direction. Leave the streak exactly as it was — a held observation
#: can neither reach a threshold nor erase progress toward one.
STREAK_HOLD = "hold"

STREAK_EFFECTS: frozenset[str] = frozenset({STREAK_ADVANCE, STREAK_RESET, STREAK_HOLD})


class StallEscalationPolicyError(ValueError):
    """Raised on a class outside the declared vocabulary or a structurally invalid threshold."""


#: Every declared stall class mapped onto exactly one effect.
#:
#: The ADVANCE set is the issue's "本物の停滞クラス" (#15855 acceptance 2: cyber 拒否 /
#: 未送信 composer / server-down / frozen 等) read against the classifier's actual
#: vocabulary. ``startup_interaction`` is in it for a reason worth stating: a unit sitting
#: on a trust dialog or a login screen is not slow, it is **stopped forever** — nothing it
#: does will clear that screen, and the declared prescription (``operator_resolves_
#: startup_screen``) already says a human is the only remedy. A watcher whose whole purpose
#: is to stop the owner from being the stall detector must surface exactly that.
#:
#: The RESET set is the two classes that are positive evidence of a live render loop.
#: The HOLD set is the two that carry no evidence at all.
ESCALATION_EFFECTS: Mapping[str, str] = MappingProxyType(
    {
        CLASS_SCREEN_PROGRESSING: STREAK_RESET,
        CLASS_BUSY_LIKELY: STREAK_RESET,
        CLASS_SCREEN_UNREADABLE: STREAK_HOLD,
        CLASS_UNKNOWN: STREAK_HOLD,
        CLASS_STARTUP_INTERACTION: STREAK_ADVANCE,
        CLASS_CONTENT_REFUSAL: STREAK_ADVANCE,
        CLASS_UNSENT_COMPOSER: STREAK_ADVANCE,
        CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED: STREAK_ADVANCE,
        CLASS_UNRESPONSIVE_INDETERMINATE: STREAK_ADVANCE,
    }
)

#: The classes that may accumulate toward an escalation, derived from the map rather than
#: written twice. Kept as a name because it is the set a reviewer most wants to see.
ESCALATING_CLASSES: frozenset[str] = frozenset(
    name for name, effect in ESCALATION_EFFECTS.items() if effect == STREAK_ADVANCE
)

# Exhaustiveness is checked at import, not asserted in prose. A class added to
# ``STALL_CLASSES`` without an effect here would otherwise reach ``fold_observation`` and
# raise at run time inside a watcher tick — i.e. in the one place that must not crash.
_UNMAPPED = STALL_CLASSES - frozenset(ESCALATION_EFFECTS)
if _UNMAPPED:  # pragma: no cover - a build-time invariant, not a runtime branch
    raise StallEscalationPolicyError(
        f"stall classes with no declared escalation effect: {sorted(_UNMAPPED)}"
    )
_UNDECLARED = frozenset(ESCALATION_EFFECTS) - STALL_CLASSES
if _UNDECLARED:  # pragma: no cover - a build-time invariant, not a runtime branch
    raise StallEscalationPolicyError(
        f"escalation effects declared for unknown stall classes: {sorted(_UNDECLARED)}"
    )

#: Portable default for "how many consecutive same-class detections justify waking a human".
#:
#: Two, not one: a single pass is one interval of screen-sameness, and #15843's calibration
#: section already records that one interval mistakes a slow render for a stall. Two passes
#: of the *same* positively-identified class is the cheapest evidence that survives that.
#:
#: This is a **portable default, not a policy**. ``stall-watcher-screen-diff.md``
#: `## 既存正本との境界` places "how long to wait before escalating" in operator runtime
#: policy, so an operator raises it; the shipped value only has to be defensible when
#: nobody configured anything.
DEFAULT_ESCALATION_THRESHOLD = 2


@dataclass(frozen=True)
class WatchIdentity:
    """The stable identity a streak is bound to.

    ``workspace_id`` + ``lane_id`` + ``role`` is the durable slot; :meth:`key` is that
    triple and is what a store must key on. ``generation`` rides along as a **bound**
    value, not a keyed one: keying on it would strand a row per relaunch, while binding it
    means the run restarts when the process behind the slot is replaced.

    ``target`` is the transient locator. It is carried so an escalation record can say
    where the screen was read, and is deliberately excluded from :meth:`key`, from
    :meth:`same_slot` and from every comparison in this module.
    """

    workspace_id: str
    lane_id: str
    role: str
    generation: str = ""
    target: str = ""

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise StallEscalationPolicyError("watch identity requires a workspace_id")
        if not self.lane_id:
            raise StallEscalationPolicyError("watch identity requires a lane_id")
        if not self.role:
            raise StallEscalationPolicyError("watch identity requires a role")

    def key(self) -> tuple[str, str, str]:
        """The durable slot key. Locator- and generation-independent, by construction."""
        return (self.workspace_id, self.lane_id, self.role)

    def same_slot(self, other: "WatchIdentity") -> bool:
        return self.key() == other.key()

    def same_generation(self, other: "WatchIdentity") -> bool:
        return self.generation == other.generation

    @property
    def slot_label(self) -> str:
        """A stable, content-free label for a durable record."""
        return f"{self.workspace_id}/{self.lane_id}/{self.role}"


@dataclass(frozen=True)
class StreakState:
    """One slot's run of same-class, same-generation stall observations.

    ``escalated_at`` is the latch described in the module docstring: empty while the streak
    has not yet fired, and the exact ``observed_at`` of the firing pass afterwards. It is
    part of the *state* rather than a side table so that clearing the streak and clearing
    the latch cannot drift apart — a reset returns ``None`` and both vanish together.
    """

    identity: WatchIdentity
    stall_class: str
    consecutive: int
    first_observed_at: str
    last_observed_at: str
    escalated_at: str = ""

    def __post_init__(self) -> None:
        if self.stall_class not in STALL_CLASSES:
            raise StallEscalationPolicyError(f"unknown stall class {self.stall_class!r}")
        if self.consecutive < 1:
            raise StallEscalationPolicyError(
                f"a recorded streak is at least one observation; got {self.consecutive!r}"
            )

    @property
    def escalated(self) -> bool:
        """Whether this streak has already fired (and must not fire again)."""
        return bool(self.escalated_at)


@dataclass(frozen=True)
class StreakDecision:
    """What one observation does to one slot's streak.

    ``next_state`` is ``None`` exactly when the row should be **deleted** — the unit proved
    it is alive, so keeping a zeroed row would preserve nothing and would leave a stale
    ``stall_class`` readable in the store. ``escalates`` is true on the single pass that
    crosses the threshold, never on the ones after it.
    """

    identity: WatchIdentity
    effect: str
    next_state: Optional[StreakState]
    escalates: bool = False

    @property
    def consecutive(self) -> int:
        """The run length after this observation (``0`` once the streak is cleared)."""
        return self.next_state.consecutive if self.next_state is not None else 0

    def telemetry(self) -> dict[str, object]:
        """Classification-token-only projection, safe to paste into a durable record."""
        payload: dict[str, object] = {
            "slot": self.identity.slot_label,
            "generation": self.identity.generation,
            "streak_effect": self.effect,
            "consecutive": self.consecutive,
            "escalates": self.escalates,
        }
        if self.next_state is not None:
            payload["stall_class"] = self.next_state.stall_class
            payload["first_observed_at"] = self.next_state.first_observed_at
            payload["last_observed_at"] = self.next_state.last_observed_at
            payload["already_escalated"] = self.next_state.escalated
        return payload


def escalation_effect(stall_class: str) -> str:
    """The declared effect of one classification on a streak."""
    try:
        return ESCALATION_EFFECTS[stall_class]
    except KeyError:
        raise StallEscalationPolicyError(
            f"unknown stall class {stall_class!r}"
        ) from None


def fold_observation(
    previous: Optional[StreakState],
    *,
    identity: WatchIdentity,
    stall_class: str,
    observed_at: str,
    threshold: int = DEFAULT_ESCALATION_THRESHOLD,
) -> StreakDecision:
    """Fold one pass's classification into ``previous`` and say whether it escalates.

    ``previous`` is ``None`` for a slot with no recorded streak. ``threshold`` is the
    operator's N; a threshold below 1 is rejected rather than clamped, because silently
    treating ``0`` as ``1`` would turn a misconfiguration into an escalation on the very
    first non-advancing screen.

    A ``previous`` recorded against a **different slot** is a caller error, not a state to
    reconcile: it would silently transplant one unit's run length onto another. A previous
    recorded against a different *generation* of the same slot is a normal occurrence and
    restarts the run.
    """
    if threshold < 1:
        raise StallEscalationPolicyError(
            f"escalation threshold must be at least 1; got {threshold!r}"
        )
    if previous is not None and not previous.identity.same_slot(identity):
        raise StallEscalationPolicyError(
            f"streak state for {previous.identity.slot_label!r} cannot be folded into "
            f"{identity.slot_label!r}"
        )

    effect = escalation_effect(stall_class)

    if effect == STREAK_RESET:
        # Alive. Drop the row and the latch together.
        return StreakDecision(identity=identity, effect=effect, next_state=None)

    if effect == STREAK_HOLD:
        # No evidence either way: the row is returned unchanged, so a caller that writes
        # it back writes the same bytes. Notably ``last_observed_at`` is NOT refreshed --
        # this pass observed nothing about the unit, and claiming otherwise would let an
        # unreadable target look freshly confirmed to anything reading the timestamps.
        #
        # The stored identity is likewise NOT refreshed to this pass's generation: a held
        # pass proves nothing about which process is behind the slot, so adopting the newly
        # reported generation here would let an unreadable screen silently re-bind an
        # existing run onto a different process. The next advancing or resetting pass
        # settles it.
        return StreakDecision(identity=identity, effect=effect, next_state=previous)

    # STREAK_ADVANCE. Two things restart the run, for the same reason: the evidence is no
    # longer about the same thing.
    #   - a CLASS change (see the module docstring: a flapping diagnosis is not a
    #     consistent detection);
    #   - a GENERATION change: a new process behind the slot has its own screen, and
    #     inheriting the old run would escalate a fresh agent for its predecessor's stall.
    # A restart also drops the latch, so the newly identified stall may escalate on its
    # own merits.
    restarts = (
        previous is None
        or previous.stall_class != stall_class
        or not previous.identity.same_generation(identity)
    )
    if restarts:
        state = StreakState(
            identity=identity,
            stall_class=stall_class,
            consecutive=1,
            first_observed_at=observed_at,
            last_observed_at=observed_at,
        )
    else:
        state = StreakState(
            identity=identity,
            stall_class=stall_class,
            consecutive=previous.consecutive + 1,
            first_observed_at=previous.first_observed_at,
            last_observed_at=observed_at,
            escalated_at=previous.escalated_at,
        )

    already_fired = state.escalated
    fires = (not already_fired) and state.consecutive >= threshold
    if fires:
        state = StreakState(
            identity=state.identity,
            stall_class=state.stall_class,
            consecutive=state.consecutive,
            first_observed_at=state.first_observed_at,
            last_observed_at=state.last_observed_at,
            escalated_at=observed_at,
        )

    return StreakDecision(
        identity=identity, effect=effect, next_state=state, escalates=fires
    )


__all__ = (
    "DEFAULT_ESCALATION_THRESHOLD",
    "ESCALATING_CLASSES",
    "ESCALATION_EFFECTS",
    "STREAK_ADVANCE",
    "STREAK_EFFECTS",
    "STREAK_HOLD",
    "STREAK_RESET",
    "StallEscalationPolicyError",
    "StreakDecision",
    "StreakState",
    "WatchIdentity",
    "escalation_effect",
    "fold_observation",
)
