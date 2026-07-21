"""Exact-generation actuation port (Redmine #13806 tranche B).

The injected side-effect boundary the generic exact-generation actuator drives — the
generalization of the #13763 receiver-replacement Herdr ops. The pure fail-closed decision
flow lives in :class:`...application.replacement_actuator.ReplacementActuatorUseCase`, which
drives this port and never touches live IO; the closed vocabularies it returns are the pure
:mod:`...domain.replacement_actuation`.

There is deliberately **no self-participant close / kill method and no fresh-coordinator
claim** on this port: tranche B replaces the *non-self* participants and arms the
transaction up to ``self_close_armed``. The current coordinator's self-close is actuated by
a process-external executor, and the fresh coordinator claim + continuation drain are
tranche C (j#78384 §5). A live Herdr adapter for this port is out of tranche B scope (live
process mutation is non-scope, j#79121) — tests drive a synthetic fake.

Every method is an **evidence probe or an additive/idempotent effect**:

- :meth:`observe_old_slot` re-resolves the pinned old generation against the live inventory
  (present / positive-absent / recycled / ambiguous) — evidence, never authority;
- :meth:`observe_preservation` gathers the preservation signals for a *new* close;
- :meth:`close_exact_generation` closes ONLY the exact pinned old generation (called only
  when :meth:`observe_old_slot` reported it present);
- :meth:`launch_action_bound` launches a fresh slot bound to the replacement ``action_id``;
- :meth:`verify_attestation` checks the fresh slot's startup attestation binds that
  ``action_id`` — a normal name/role/lane attestation is not proof of THIS replacement.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mozyo_bridge.core.state.replacement_preservation import PreservationObservation
from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin


@runtime_checkable
class ExactGenerationActuatorPort(Protocol):
    """Every action-time effect the exact-generation actuator needs, injected for fakes.

    All returns are values from :mod:`...domain.replacement_actuation` (or a
    :class:`PreservationObservation`), so the use case's decisions stay pure and the live
    inventory / process mutation lives entirely behind this boundary.
    """

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        """Re-resolve the pinned old generation against the live inventory.

        Returns one of :data:`...domain.replacement_actuation.OLD_SLOT_PRESENT` /
        ``OLD_SLOT_ABSENT`` / ``OLD_SLOT_RECYCLED`` / ``OLD_SLOT_AMBIGUOUS`` — never
        degrading an ambiguous or recycled inventory to a positive absence.
        """
        ...

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        """Gather the preservation signals for a NEW close of ``pin``.

        The use case re-evaluates this before every new process close (j#78384 §3). A
        Redmine-unreadable / unrecorded continuation state is surfaced as an
        ``unrecorded_journal`` signal so the fence blocks rather than closing blind.
        """
        ...

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        """Close the EXACT pinned old generation (called only when it is present).

        Returns :data:`...domain.replacement_actuation.CLOSE_DONE` or ``CLOSE_ERROR``.
        Never closes a recycled / ambiguous slot — the use case gates the call on
        :meth:`observe_old_slot`.
        """
        ...

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        """Launch a fresh slot for ``pin`` bound to the replacement ``action_id``.

        Returns :data:`...domain.replacement_actuation.LAUNCH_DONE` or ``LAUNCH_ERROR``.
        The receipt's binding of ``action_id`` is the adapter's concern; the use case
        confirms it via :meth:`verify_attestation`.
        """
        ...

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        """Verify the fresh slot's startup attestation binds ``action_id``.

        Returns :data:`...domain.replacement_actuation.ATTEST_BOUND` (binds the action) /
        ``ATTEST_PENDING`` (still booting) / ``ATTEST_MISMATCH`` (attested but not to this
        action). Only ``bound`` completes the participant (j#78384 §4).
        """
        ...


__all__ = ("ExactGenerationActuatorPort",)
