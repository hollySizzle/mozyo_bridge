"""What a stored row's phase says about a worker recovery -- as fact, not as policy.

Split out of :mod:`.replacement_actuator` when that module reached the module-health
ceiling. It answers ONE question -- what state this row is in, as far as a worker recovery
is concerned -- and returns closed tokens. It decides NOTHING about what the caller may then
do: whether a verdict leads to a claim, to an effect, or to an immediate answer is the
caller's policy, and the two callers deliberately differ.

Where the policy actually lives (ruling j#97210):

* :func:`...sublane_vanished_gateway_recovery_live.actuate_vanished_gateway_recovery`
  answers a consistent progressed row itself, writing nothing;
* :meth:`...replacement_actuator.ReplacementActuatorUseCase.drive_worker_recovery` re-claims
  it under the same holder, because an existing recovery whose send is confirmed but whose
  lease expired can only finish that way.

Both comments are the record. Do not fold either policy back into this module: an earlier
round did exactly that and stranded a confirmed-send row in ``draining_continuation``.

The three facts this classification exists to state, all measured:

* a phase outside the drivable three is NOT the recovered state -- including a SELF
  replacement phase and one this build does not know (j#97190 F2);
* ``draining_continuation`` / ``completed`` are only the progressed state if every non-self
  participant really is ``replaced``; otherwise the row contradicts itself (j#97196 F2);
* the two are distinct verdicts, because their callers treat them differently.
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
#: A progressed phase whose participants agree with it: the redispatch leg provably ran.
#: A FACT about the row -- it says nothing about whether the caller may claim, write or
#: answer immediately. See the module docstring for where that is decided.
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

    Facts only. Two things this classification used to get wrong, both measured
    (j#97190 F2, j#97196 F2):

    * every phase outside the drivable three was reported as ``recovered`` -- including a
      SELF-replacement phase and one this build does not know -- for a participant that was
      never launched or attested;
    * ``draining_continuation`` / ``completed`` were taken at face value. They mean the
      redispatch leg already ran, which is only true if every non-self participant really is
      ``replaced``; a completed row whose worker is still ``close_owed`` is a contradiction,
      not an idempotent success.
    """
    phase = _phase_token(getattr(rec, "phase", ""))
    if phase in _RECOVERY_PROGRESSED_PHASES:
        participants = tuple(getattr(rec, "participants", ()) or ())
        if participants and all(
            _phase_token(getattr(p, "phase", "")) == PARTICIPANT_REPLACED
            for p in participants
        ):
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
