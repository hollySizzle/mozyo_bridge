"""Discharge update evidence after a VERIFIED relaunch (Redmine #14741 j#97131).

The other half of the planner. The planner arms a replacement with the exact bound evidence
a lane's update produced; this discharges that evidence once the relaunch has actually been
attested, so a later recovery cannot arm the same update debt a second time from a receipt
nobody cleared.

**Where** is the whole design. Consuming before the launch loses the evidence to a crash --
the launch never happens and the ``launch_owed`` debt has nothing left to re-arm from
(audit j#96966 C15). Consuming after the participant reaches ``replaced`` leaves a window
where the transaction is complete and the evidence is still live. So it happens between:
after ``ATTEST_BOUND``, before the ``replaced`` CAS, behind the same re-authentication every
other destructive effect goes through.

Three things are checked before any store is opened, and each one is a fail-closed refusal
with ZERO store calls:

* the triplet is whole (a partial triplet is already impossible on a ``ParticipantPin``,
  but this module does not inherit that -- it states it);
* the evidence names THIS transaction's workspace;
* the cause byte-equals the provider registry's own
  :data:`...LAUNCH_CAUSE_UPDATE_RELAUNCH`.

The cause is imported rather than re-spelled for the same reason as in the planner
composition: a second literal is a second owner, and the two would drift silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

#: The triplet is not this transaction's to discharge.
COMPLETION_FOREIGN_WORKSPACE = "evidence_workspace_mismatch"
#: The stored cause is not the closed update-relaunch token.
COMPLETION_CAUSE_MISMATCH = "evidence_cause_mismatch"
#: The pin does not carry a whole evidence triplet.
COMPLETION_INCOMPLETE = "evidence_incomplete"
#: The receipt authority could not be reached or answered unusably.
COMPLETION_UNAVAILABLE = "evidence_authority_unavailable"


def _exact(value: object) -> str:
    """The token exactly as stored, or ``""``. No strip, no coercion (j#97074)."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


def build_update_evidence_completion(home: Optional[Path]):
    """A completion port bound to the receipt authority under ``home``.

    ``home`` is passed explicitly by every construction site, exactly as the planner
    composition is: the two must read the SAME store, and a port that resolved its own home
    from the cwd or a repo root could discharge evidence in a different one than the plan
    armed from.
    """

    def complete(key: Any, pin: Any, *, replacement_action_id: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
            LAUNCH_CAUSE_UPDATE_RELAUNCH,
        )

        evidence_workspace = _exact(getattr(pin, "evidence_workspace_id", None))
        evidence_action = _exact(getattr(pin, "evidence_startup_action_id", None))
        evidence_cause = _exact(getattr(pin, "evidence_cause", None))
        if not (evidence_workspace and evidence_action and evidence_cause):
            return COMPLETION_INCOMPLETE
        if evidence_workspace != _exact(getattr(key, "workspace_id", None)):
            return COMPLETION_FOREIGN_WORKSPACE
        if evidence_cause != LAUNCH_CAUSE_UPDATE_RELAUNCH:
            return COMPLETION_CAUSE_MISMATCH

        from mozyo_bridge.core.state.launch_identity_receipt import (
            GenerationKey,
            LaunchIdentityReceiptStore,
        )

        try:
            generation_key = GenerationKey(
                workspace_id=evidence_workspace,
                lane_id=_exact(getattr(pin, "lane_id", None)),
                provider=_exact(getattr(pin, "provider", None)),
                assigned_name=_exact(getattr(pin, "assigned_name", None)),
                startup_action_id=evidence_action,
            )
            return LaunchIdentityReceiptStore(home=home).consume_evidence(
                generation_key, consumed_by=replacement_action_id
            )
        except Exception:  # noqa: BLE001 - an unreachable authority leaves the debt owed
            # No exception reaches the actuator: it is mid-transaction, and an exception
            # there would abandon a participant between a live relaunch and its CAS. A
            # typed refusal keeps it `verify_owed`, which is replayable.
            return COMPLETION_UNAVAILABLE

    return complete


__all__ = (
    "COMPLETION_CAUSE_MISMATCH",
    "COMPLETION_FOREIGN_WORKSPACE",
    "COMPLETION_INCOMPLETE",
    "COMPLETION_UNAVAILABLE",
    "build_update_evidence_completion",
)
