"""Hibernated exact-pair recovery — the pure per-slot decision (Redmine #13847 items 3/4).

The public ``sublane recover-pair`` surface recovers the exact gateway + worker pair of a
**hibernated** lane whose fresh launch booted partially (unattested / stale) — the state
``sublane resume`` reports ``pair_not_attested`` and ``recover-stale`` cannot touch (it
protects the gateway). Unlike ``recover-stale`` (worker-only, gateway protected), BOTH
slots are recoverable here, but only their **bad generation** — pinned to the hibernated
lifecycle record's exact issue / lane / revision / generation and its declared gateway /
worker pins, gated by an owner approval.

This is the pure half: the closed per-slot disposition vocabulary and the ordered
fail-closed classification a preflight makes over a *positive-fact* observation of each
live slot. Every gate is a positive fact defaulting to the unsafe (preserve) side, so a
missing / unreadable / ambiguous observation **preserves** (zero-close) rather than closes
— the byte-preservation discipline of :mod:`...replacement_preservation`. The actuation
(close bad generation → relaunch → re-attest → resume CAS → redispatch) is the caller; this
module never opens a store, reads the live inventory, or mutates a process.

The dispositions map one-to-one onto the Implementation Request's zero-close scenarios
(j#79612 §4): a productive provider / tool-child, a pending composer, a foreign slot, an
ambiguous / unreadable identity, and a NEWER generation are all **preserved** (zero-close,
worktree bytes kept); only a slot that is positively the hibernated pair's own stale /
unattested bad generation is closed + relaunched.
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import norm

# -- per-slot recovery disposition vocabulary (a closed set) --------------------

#: The slot is positively the hibernated pair's own stale / unattested bad generation: it is
#: the slot the fresh launch left partial. Close it (byte-preserving) and relaunch the exact
#: generation. The ONLY disposition that actuates.
SLOT_RECOVER = "recover_bad_generation"
#: The slot already presents the correct, live, locator-matched, attested generation — the
#: recovery target is already healthy. No action (never close a good slot).
SLOT_HEALTHY = "healthy_no_action"

#: The live inventory cannot uniquely resolve the pinned slot (unreadable / more than one
#: candidate at the pinned identity). Never degraded to "absent" and closed blind — preserve.
SLOT_PRESERVE_AMBIGUOUS = "preserve_ambiguous"
#: The resolved slot is NOT this hibernated lane's slot (a foreign workspace / lane / issue /
#: provider). Preserve — the recovery closes only the pair the approval pins.
SLOT_PRESERVE_FOREIGN = "preserve_foreign"
#: The live slot's generation is NEWER than the approved (hibernated) generation — a fresher
#: generation superseded this approval. Preserve (never close a newer generation).
SLOT_PRESERVE_NEWER = "preserve_newer_generation"
#: The slot is a live *productive* foreground provider or active tool-child — it is doing
#: work, not partial-launch residue. Preserve (never destroy an in-flight turn).
SLOT_PRESERVE_PRODUCTIVE = "preserve_productive"
#: The slot has a pending composer (unsent operator/agent input). Preserve (closing it would
#: drop un-committed input).
SLOT_PRESERVE_PENDING = "preserve_pending_composer"
#: The slot's worktree state cannot be read — byte preservation requires a readable worktree
#: (a *dirty* worktree is fine and is preserved; an UNREADABLE one is not). Preserve.
SLOT_PRESERVE_UNREADABLE = "preserve_worktree_unreadable"

SLOT_DISPOSITIONS = frozenset(
    {
        SLOT_RECOVER,
        SLOT_HEALTHY,
        SLOT_PRESERVE_AMBIGUOUS,
        SLOT_PRESERVE_FOREIGN,
        SLOT_PRESERVE_NEWER,
        SLOT_PRESERVE_PRODUCTIVE,
        SLOT_PRESERVE_PENDING,
        SLOT_PRESERVE_UNREADABLE,
    }
)

#: The dispositions that forbid any close/relaunch of the slot (everything but
#: :data:`SLOT_RECOVER`). ``SLOT_HEALTHY`` is non-actuating too (already good).
SLOT_PRESERVE_DISPOSITIONS = frozenset(SLOT_DISPOSITIONS - {SLOT_RECOVER})


class SlotRecoveryObservation:
    """The action-time facts a preflight observes about ONE pinned hibernated-pair slot.

    Every field is a **positive** fact defaulting to the unsafe (preserve) side (``False``),
    so a missing / unreadable / ambiguous observation preserves at :func:`decide_slot_recovery`:

    - ``slot_absent`` — the live inventory resolves ZERO panes at the slot's pinned name (the
      slot's process is gone — e.g. it was closed in a prior partial recovery run, or the
      launch never produced it). A vanished pair slot is RELAUNCH-recoverable (no close), so a
      partial close/relaunch is replayable (Redmine #13847 R1-F1);
    - ``identity_resolved`` — the live inventory resolves EXACTLY one candidate at the slot's
      pinned identity (never ambiguous / unreadable). Mutually exclusive with ``slot_absent``
      (0 vs 1 panes); more than one pane leaves both ``False`` -> ambiguous;
    - ``belongs_to_pair`` — that candidate is THIS hibernated lane's slot (matching the
      declared workspace / lane / issue / provider pin), not a foreign slot;
    - ``generation_not_newer`` — the live slot's generation is NOT newer than the approved
      (hibernated) generation (a newer generation superseded the approval → preserve);
    - ``not_productive`` — the slot is NOT a live productive foreground provider / tool-child;
    - ``no_pending_composer`` — the slot has no pending (unsent) composer input;
    - ``worktree_readable`` — the slot's worktree state can be read (dirty is fine; only an
      *unreadable* one preserves);
    - ``is_bad_generation`` — the slot positively presents the hibernated pair's own stale /
      unattested bad-generation signal (the partial-launch residue this recovery closes);
    - ``already_healthy`` — the slot already presents the correct live, locator-matched,
      attested generation (no action needed).
    """

    __slots__ = (
        "slot_absent",
        "identity_resolved",
        "belongs_to_pair",
        "generation_not_newer",
        "not_productive",
        "no_pending_composer",
        "worktree_readable",
        "is_bad_generation",
        "already_healthy",
    )

    def __init__(
        self,
        *,
        slot_absent: bool = False,
        identity_resolved: bool = False,
        belongs_to_pair: bool = False,
        generation_not_newer: bool = False,
        not_productive: bool = False,
        no_pending_composer: bool = False,
        worktree_readable: bool = False,
        is_bad_generation: bool = False,
        already_healthy: bool = False,
    ) -> None:
        self.slot_absent = bool(slot_absent)
        self.identity_resolved = bool(identity_resolved)
        self.belongs_to_pair = bool(belongs_to_pair)
        self.generation_not_newer = bool(generation_not_newer)
        self.not_productive = bool(not_productive)
        self.no_pending_composer = bool(no_pending_composer)
        self.worktree_readable = bool(worktree_readable)
        self.is_bad_generation = bool(is_bad_generation)
        self.already_healthy = bool(already_healthy)

    def as_payload(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in self.__slots__}


def decide_slot_recovery(observation: SlotRecoveryObservation) -> str:
    """Classify one slot's recovery disposition. (pure, fail-closed, ordered)

    Returns :data:`SLOT_RECOVER` (the only actuating disposition) ONLY when the slot is
    positively this hibernated pair's own stale / unattested bad generation and every
    preserve-gate has been cleared. Otherwise the first failing gate's preserve disposition
    (most-fundamental first), so the durable record names exactly why the slot was not
    closed. No gate defaults to the actuating side — a missing observation preserves.

    Order (each preserves a distinct zero-close class before the actuating check):

    0. a VANISHED pair slot (``slot_absent`` — zero live panes) is RELAUNCH-recovered (no
       close), UNLESS a newer lane generation superseded the approval (preserve). This is
       what makes a partial close/relaunch replayable: a slot closed in a prior run comes
       back ``slot_absent`` and is relaunched, not stuck ``preserve_ambiguous`` (R1-F1);
    1. identity must resolve uniquely (ambiguous / unreadable → preserve);
    2. the slot must belong to this pair (a foreign slot is preserved);
    3. the generation must not be newer (a superseded approval preserves the newer slot);
    4. the slot must not be a productive provider / tool-child (never destroy in-flight work);
    5. the slot must have no pending composer (never drop un-sent input);
    6. the worktree must be readable (byte preservation needs a readable worktree);
    7. an already-healthy slot needs no action (never close a good generation);
    8. only then, a slot positively presenting the bad-generation residue is recovered.
    """
    if observation.slot_absent:
        # A vanished pair slot is relaunch-recoverable — but a newer lane generation that
        # superseded the approval still preserves it (never relaunch onto a superseded lane).
        return SLOT_RECOVER if observation.generation_not_newer else SLOT_PRESERVE_NEWER
    if not observation.identity_resolved:
        return SLOT_PRESERVE_AMBIGUOUS
    if not observation.belongs_to_pair:
        return SLOT_PRESERVE_FOREIGN
    if not observation.generation_not_newer:
        return SLOT_PRESERVE_NEWER
    if not observation.not_productive:
        return SLOT_PRESERVE_PRODUCTIVE
    if not observation.no_pending_composer:
        return SLOT_PRESERVE_PENDING
    if not observation.worktree_readable:
        return SLOT_PRESERVE_UNREADABLE
    if observation.already_healthy:
        return SLOT_HEALTHY
    if not observation.is_bad_generation:
        # Not newer, not productive, not pending, readable, not already-healthy, yet no
        # positive bad-generation signal: an indeterminate slot is preserved, never closed
        # on the absence of a positive residue signal.
        return SLOT_PRESERVE_AMBIGUOUS
    return SLOT_RECOVER


def slot_recovers(disposition: str) -> bool:
    """Does this per-slot disposition close+relaunch the slot? (pure)"""
    return norm(disposition) == SLOT_RECOVER


def hibernated_pair_recovery_action_id(
    *, issue: str, lane_id: str, revision: str, generation: str
) -> str:
    """The deterministic action id naming ONE exact hibernated pair generation. (pure)

    A ``recover-pair:<issue>:<lane>:<revision>:<generation>`` token pinned to the exact
    hibernated lifecycle record (issue / lane / revision / generation). Two recoveries of the
    same hibernated pair at the same revision+generation share the key (idempotent, replayable
    resume); a different revision / generation is a different key. Every component must be
    present — an under-specified target could never identify one exact pair, so it raises
    rather than emit an ambiguous id (the ``stale_worker_recovery_action_id`` precedent).
    """
    parts = {
        "issue": norm(issue),
        "lane_id": norm(lane_id),
        "revision": norm(revision),
        "generation": norm(generation),
    }
    missing = [name for name, value in parts.items() if not value]
    if missing:
        raise ValueError(
            "a hibernated pair recovery action id requires a non-empty issue / lane_id / "
            f"revision / generation (missing: {', '.join(missing)})"
        )
    return "recover-pair:" + ":".join(
        parts[name] for name in ("issue", "lane_id", "revision", "generation")
    )


__all__ = (
    "SLOT_RECOVER",
    "SLOT_HEALTHY",
    "SLOT_PRESERVE_AMBIGUOUS",
    "SLOT_PRESERVE_FOREIGN",
    "SLOT_PRESERVE_NEWER",
    "SLOT_PRESERVE_PRODUCTIVE",
    "SLOT_PRESERVE_PENDING",
    "SLOT_PRESERVE_UNREADABLE",
    "SLOT_DISPOSITIONS",
    "SLOT_PRESERVE_DISPOSITIONS",
    "SlotRecoveryObservation",
    "decide_slot_recovery",
    "slot_recovers",
    "hibernated_pair_recovery_action_id",
)
