"""Evidence-aware participant planning for a replacement transaction (Redmine #14741).

Design Answer j#97047 decision 1. Five production sites construct :class:`ParticipantPin`
(gateway recovery, stale-worker recovery, worker refresh, hibernated convergence,
composer-discard), and the update-evidence triplet has to be pinned the same way at every
one of them. Putting the rule in one named service is what makes "the same way" checkable:
a site that skips it is a site that plans a transaction which cannot prove what it is
replacing, and with five constructors that is not something review can be relied on to
catch each time.

Where the IO lives
------------------
Here, behind injected ports — never in :mod:`...replacement_transaction_model` or the
receipt store. The model is a pure value layer whose codec is a closed schema, and the
receipt store is an authority; giving either of them a reader for the other would make two
stores' availability decide one store's contract.

What it will and will not do
----------------------------
- a **legacy / generic** generation is returned byte-exact and the receipt store is never
  opened. That is what keeps every pre-#14741 replacement identical, including its cost;
- a **receipt-capable** generation is planned only when the whole identity agrees —
  workspace, lane, role, provider, assigned name, old locator, the lifecycle
  ``(generation, revision)``, the startup action id, and the bound evidence's own key and
  cause. Then, and only then, a NEW pin is returned carrying the triplet, with every
  existing field of the input pin untouched;
- anything else is a typed refusal. Not a pin without evidence — a refusal, because a
  receipt-capable generation whose evidence cannot be established is exactly the state the
  #14741 loop was invisible in. Zero transaction plan, zero launch, zero store write.

Deterministic and idempotent: the same exact inputs produce the same exact pins, and a pin
that already carries the correct triplet is returned unchanged rather than re-derived. An
existing transaction is never enriched after the fact (j#97047 decision 5) — this plans, it
does not amend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

#: Every generation in the plan predates identity receipts; the pins are unchanged.
PLAN_LEGACY_UNCHANGED = "legacy_unchanged"
#: At least one receipt-capable generation was planned with its evidence triplet.
PLAN_EVIDENCE_PINNED = "evidence_pinned"

# --- Refusal reasons. Fixed tokens, safe on a durable record. ---------------------------
REFUSE_UNKNOWN_ACTION_SHAPE = "unknown_action_shape"
REFUSE_GENERATION_UNAVAILABLE = "generation_unavailable"
REFUSE_GENERATION_NOT_ATTESTED = "generation_not_attested"
REFUSE_GENERATION_MISMATCH = "generation_mismatch"
REFUSE_LIFECYCLE_UNAVAILABLE = "lifecycle_unavailable"
REFUSE_LIFECYCLE_MISMATCH = "lifecycle_mismatch"
REFUSE_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
REFUSE_EVIDENCE_MISMATCH = "evidence_mismatch"
REFUSE_DIVERGENT_PRE_PIN = "divergent_pre_pin"


class EvidencePlanRefused(RuntimeError):
    """A receipt-capable participant could not be planned. Zero plan, zero launch."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class EvidencePlan:
    """The participants a transaction may be planned with, and what was decided."""

    participants: tuple
    outcome: str


#: ``assigned_name -> LaunchGeneration | None``. Raising is a refusal, not an absence.
GenerationPort = Callable[[str], Any]
#: ``lane_id -> (lane_generation, lifecycle_revision) | None``.
LifecyclePort = Callable[[str], Any]
#: ``(workspace_id, lane_id, provider, lane_generation, lifecycle_revision) ->
#: UpdateRelaunchEvidence | None``.
EvidencePort = Callable[..., Any]


def _norm(value: object) -> str:
    return str(value or "").strip()


class ReplacementEvidencePlanner:
    """Plan participants with their update evidence, or refuse (never partially)."""

    def __init__(
        self,
        *,
        generations: GenerationPort,
        lifecycle: LifecyclePort,
        evidence: EvidencePort,
        capability: Optional[Callable[[object], bool]] = None,
    ) -> None:
        self._generations = generations
        self._lifecycle = lifecycle
        self._evidence = evidence
        self._capability = capability

    # -- capability ---------------------------------------------------------------------

    def _requires_receipt(self, action_id: str) -> bool:
        """Whether this action promised identity receipts. An unknown shape refuses.

        Decided from the action id itself (j#96892), so deleting the receipt store cannot
        make a receipt-capable action look like a pre-feature one.
        """
        classify = self._capability
        if classify is None:
            from mozyo_bridge.core.state.startup_transaction_fence import (
                requires_identity_receipt,
            )

            classify = requires_identity_receipt
        try:
            return bool(classify(action_id))
        except Exception as exc:  # noqa: BLE001 - an unclassifiable action is never legacy
            raise EvidencePlanRefused(REFUSE_UNKNOWN_ACTION_SHAPE, str(exc)) from exc

    # -- planning -----------------------------------------------------------------------

    def plan(self, participants: Sequence[Any]) -> EvidencePlan:
        """Return the participants to plan the transaction with.

        Legacy plans come back byte-exact with the receipt store untouched. A
        receipt-capable participant is either planned WITH its evidence triplet or the
        whole plan is refused — there is no middle result, because a transaction holding
        some proven participants and some unproven ones would have to decide at run time
        which kind it is.
        """
        pinned = tuple(participants or ())
        if not pinned:
            return EvidencePlan(participants=(), outcome=PLAN_LEGACY_UNCHANGED)

        planned = []
        touched = False
        for pin in pinned:
            action_id = self._action_id_for(pin)
            if not self._requires_receipt(action_id):
                planned.append(self._legacy_pin(pin))
                continue
            planned.append(self._receipt_pin(pin, action_id))
            touched = True
        return EvidencePlan(
            participants=tuple(planned),
            outcome=PLAN_EVIDENCE_PINNED if touched else PLAN_LEGACY_UNCHANGED,
        )

    def _action_id_for(self, pin: Any) -> str:
        """The startup action this participant's live generation belongs to.

        Read from the launch-generation authority, which is the store where a relaunch
        atomically supersedes the previous row — so it answers for the generation that is
        actually there, not for one that used to be.
        """
        assigned = _norm(getattr(pin, "assigned_name", ""))
        if not assigned:
            raise EvidencePlanRefused(
                REFUSE_GENERATION_UNAVAILABLE, "participant has no assigned name"
            )
        try:
            generation = self._generations(assigned)
        except Exception as exc:  # noqa: BLE001 - an unreadable authority is a refusal
            raise EvidencePlanRefused(REFUSE_GENERATION_UNAVAILABLE, str(exc)) from exc
        if generation is None:
            raise EvidencePlanRefused(
                REFUSE_GENERATION_UNAVAILABLE,
                f"no launch generation is recorded for {assigned!r}",
            )
        if _norm(getattr(generation, "phase", "")) != "attested":
            # A pending generation has not proven it came up. Planning a replacement whose
            # evidence rests on it would rest on a launch that may never have happened.
            raise EvidencePlanRefused(
                REFUSE_GENERATION_NOT_ATTESTED,
                f"the launch generation for {assigned!r} is not attested",
            )
        self._require_generation_identity(pin, generation)
        return _norm(getattr(generation, "startup_action_id", ""))

    def _require_generation_identity(self, pin: Any, generation: Any) -> None:
        """The live generation must be the participant this plan names."""
        for field, attr in (("role", "role"), ("lane_id", "lane_id")):
            if _norm(getattr(generation, attr, "")) != _norm(getattr(pin, field, "")):
                raise EvidencePlanRefused(
                    REFUSE_GENERATION_MISMATCH,
                    f"the live generation's {attr} is not the participant's",
                )

    def _legacy_pin(self, pin: Any):
        """A pre-#14741 participant: byte-exact, and the receipt store is never opened."""
        triplet = (
            _norm(getattr(pin, "evidence_workspace_id", "")),
            _norm(getattr(pin, "evidence_startup_action_id", "")),
            _norm(getattr(pin, "evidence_cause", "")),
        )
        if any(triplet):
            # A legacy generation carrying evidence is not a legacy generation. Refuse
            # rather than silently strip it or silently keep it.
            raise EvidencePlanRefused(
                REFUSE_DIVERGENT_PRE_PIN,
                "a participant whose action is not receipt-capable already carries an "
                "evidence triplet",
            )
        return pin

    def _receipt_pin(self, pin: Any, action_id: str):
        from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

        lane_id = _norm(getattr(pin, "lane_id", ""))
        try:
            lifecycle = self._lifecycle(lane_id)
        except Exception as exc:  # noqa: BLE001
            raise EvidencePlanRefused(REFUSE_LIFECYCLE_UNAVAILABLE, str(exc)) from exc
        if not lifecycle:
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE,
                f"lane {lane_id!r} has no declared lifecycle generation/revision",
            )
        lane_generation, lifecycle_revision = (_norm(v) for v in lifecycle)
        if not lane_generation or not lifecycle_revision:
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE, "the lifecycle pair is not fully declared"
            )
        pinned_generation = _norm(getattr(pin, "lane_generation", ""))
        pinned_revision = _norm(getattr(pin, "lane_revision", ""))
        if (pinned_generation and pinned_generation != lane_generation) or (
            pinned_revision and pinned_revision != lifecycle_revision
        ):
            # The plan captured a lifecycle the lane has since moved off. Acting on it is
            # what the #13810 pin exists to prevent.
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_MISMATCH,
                "the participant's pinned lifecycle is not the lane's current one",
            )

        workspace_id = _norm(getattr(pin, "evidence_workspace_id", ""))
        try:
            found = self._evidence(
                workspace_id=workspace_id or None,
                lane_id=lane_id,
                provider=_norm(getattr(pin, "provider", "")),
                lane_generation=lane_generation,
                lifecycle_revision=lifecycle_revision,
            )
        except Exception as exc:  # noqa: BLE001
            raise EvidencePlanRefused(REFUSE_EVIDENCE_UNAVAILABLE, str(exc)) from exc
        if found is None:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_UNAVAILABLE,
                "no live update evidence is bound to this exact generation",
            )

        key = getattr(found, "key", None)
        if key is None or _norm(getattr(key, "startup_action_id", "")) != action_id:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_MISMATCH,
                "the bound evidence names a different startup action",
            )
        for attr, expected in (
            ("lane_id", lane_id),
            ("provider", _norm(getattr(pin, "provider", ""))),
            ("assigned_name", _norm(getattr(pin, "assigned_name", ""))),
        ):
            if _norm(getattr(key, attr, "")) != expected:
                raise EvidencePlanRefused(
                    REFUSE_EVIDENCE_MISMATCH,
                    f"the bound evidence's {attr} is not the participant's",
                )
        cause = _norm(getattr(found, "blocker_id", ""))
        if not cause:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_MISMATCH, "the bound evidence carries no typed cause"
            )
        evidence_workspace = _norm(getattr(key, "workspace_id", ""))
        if workspace_id and workspace_id != evidence_workspace:
            raise EvidencePlanRefused(
                REFUSE_DIVERGENT_PRE_PIN,
                "the participant already carries a different evidence workspace",
            )
        existing_action = _norm(getattr(pin, "evidence_startup_action_id", ""))
        existing_cause = _norm(getattr(pin, "evidence_cause", ""))
        if (existing_action and existing_action != action_id) or (
            existing_cause and existing_cause != cause
        ):
            raise EvidencePlanRefused(
                REFUSE_DIVERGENT_PRE_PIN,
                "the participant already carries a different evidence action or cause",
            )

        # Every existing authority on the input pin is carried across unchanged; only the
        # triplet is added. Re-planning an already-correct pin reproduces it exactly.
        return ParticipantPin(
            lane_id=pin.lane_id,
            role=pin.role,
            provider=pin.provider,
            assigned_name=pin.assigned_name,
            old_locator=pin.old_locator,
            is_self=pin.is_self,
            lane_revision=pin.lane_revision,
            lane_generation=pin.lane_generation,
            evidence_workspace_id=evidence_workspace,
            evidence_startup_action_id=action_id,
            evidence_cause=cause,
            phase=pin.phase,
        )


__all__ = (
    "EvidencePlan",
    "EvidencePlanRefused",
    "PLAN_EVIDENCE_PINNED",
    "PLAN_LEGACY_UNCHANGED",
    "REFUSE_DIVERGENT_PRE_PIN",
    "REFUSE_EVIDENCE_MISMATCH",
    "REFUSE_EVIDENCE_UNAVAILABLE",
    "REFUSE_GENERATION_MISMATCH",
    "REFUSE_GENERATION_NOT_ATTESTED",
    "REFUSE_GENERATION_UNAVAILABLE",
    "REFUSE_LIFECYCLE_MISMATCH",
    "REFUSE_LIFECYCLE_UNAVAILABLE",
    "REFUSE_UNKNOWN_ACTION_SHAPE",
    "ReplacementEvidencePlanner",
)
