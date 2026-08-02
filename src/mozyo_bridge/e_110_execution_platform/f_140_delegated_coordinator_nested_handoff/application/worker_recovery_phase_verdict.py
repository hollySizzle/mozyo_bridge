"""Which worker-recovery phases may be driven, and which are already done (#14741).

Split out of :mod:`.replacement_actuator` when that module reached the module-health
ceiling. It answers ONE question about a stored row -- what its phase entitles a worker
recovery to do -- and it answers it in closed tokens so the caller keeps ownership of the
actuation result type (which lives in the module this was split from).

Three things this classification exists to prevent, all measured:

* every phase outside the drivable three being reported as recovered, including a SELF
  replacement phase and one this build does not know (j#97190 F2);
* ``draining_continuation`` / ``completed`` taken at face value -- they mean the redispatch
  leg already ran, which is only true if every non-self participant really is ``replaced``
  (j#97196 F2);
* a VALID progressed row being let through to the lease claim, so an idempotent replay
  rewrote the row it was only reading (j#97207).
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_REPLACED,
    PHASE_CLAIMED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_PLANNED,
    PHASE_REPLACING_NONSELF,
)

#: Drive it: the row is in the flow and has work left.
VERDICT_DRIVABLE = "drivable"
#: Answer recovered WITHOUT claiming: the redispatch leg already ran, provably.
VERDICT_ALREADY_RECOVERED = "already_recovered"
#: A progressed phase whose participants contradict it.
VERDICT_PROGRESSED_INCONSISTENT = "progressed_inconsistent"
#: A phase this flow does not own at all.
VERDICT_PHASE_FOREIGN = "phase_foreign"

def _phase_token(value: object) -> str:
    """A phase only when it is already plain exact text; otherwise no phase at all."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


#: The phases a worker recovery may be driven from.
_RECOVERY_DRIVABLE_PHASES = (PHASE_PLANNED, PHASE_CLAIMED, PHASE_REPLACING_NONSELF)
#: The phases that MEAN the redispatch leg already ran.
_RECOVERY_PROGRESSED_PHASES = (PHASE_DRAINING_CONTINUATION, PHASE_COMPLETED)


def worker_recovery_phase_verdict(rec) -> str:
    """This row's phase verdict, decided before anything is claimed. (pure)

    Three things this used to get wrong, all measured (j#97190 F2, j#97201, j#97207):

    * every phase outside the drivable three was reported as ``recovered`` -- including a
      SELF-replacement phase and one this build does not know -- for a participant that was
      never launched or attested;
    * ``draining_continuation`` / ``completed`` were taken at face value. They mean the
      redispatch leg already ran, which is only true if every non-self participant really is
      ``replaced``; a completed row whose worker is still ``close_owed`` is a contradiction,
      not an idempotent success;
    * a VALID progressed row was let through to the claim, so an idempotent replay rewrote
      the row it was only reading -- it is answered here instead, with zero write.
    """
    phase = _phase_token(getattr(rec, "phase", ""))
    if phase in _RECOVERY_PROGRESSED_PHASES:
        participants = tuple(getattr(rec, "participants", ()) or ())
        if participants and all(
            _phase_token(getattr(p, "phase", "")) == PARTICIPANT_REPLACED
            for p in participants
        ):
            # Recovered, and returned WITHOUT claiming (audit j#97207). This row is already
            # past the redispatch leg, so an idempotent replay has nothing to do -- and
            # claiming it anyway rewrote the durable authority on every replay: measured,
            # revision 9 -> 10 with the lease re-taken, for an answer that was "nothing
            # changed".
            return VERDICT_ALREADY_RECOVERED
        return VERDICT_PROGRESSED_INCONSISTENT
    if phase in _RECOVERY_DRIVABLE_PHASES:
        return VERDICT_DRIVABLE
    return VERDICT_PHASE_FOREIGN




__all__ = (
    "VERDICT_ALREADY_RECOVERED",
    "VERDICT_DRIVABLE",
    "VERDICT_PHASE_FOREIGN",
    "VERDICT_PROGRESSED_INCONSISTENT",
    "worker_recovery_phase_verdict",
)
