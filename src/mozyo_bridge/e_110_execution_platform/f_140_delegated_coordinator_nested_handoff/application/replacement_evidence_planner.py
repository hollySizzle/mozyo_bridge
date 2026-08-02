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
from typing import Any, Callable, Optional, Protocol, Sequence

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
#: The bound evidence exists but is not in the ``bound`` phase (consumed / unknown).
REFUSE_EVIDENCE_NOT_BOUND = "evidence_not_bound"
#: The observed screen does not map to an update-derived launch cause.
REFUSE_CAUSE_NOT_UPDATE_DERIVED = "cause_not_update_derived"
#: The planning context itself is not a set of exact non-empty tokens.
REFUSE_CONTEXT_INVALID = "context_invalid"
#: A participant names a lane the context is not scoped to.
REFUSE_LANE_OUT_OF_CONTEXT = "lane_out_of_context"


class EvidencePlanRefused(RuntimeError):
    """A receipt-capable participant could not be planned. Zero plan, zero launch.

    ``reason`` is the outward authority and is always one of the fixed tokens below.
    ``detail`` is a FIXED clause chosen by this module — never an exception string or a
    host path (audit j#97062 finding 5), so a refusal is safe to put on a durable record
    verbatim.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class EvidencePlan:
    """The participants a transaction may be planned with, and what was decided."""

    participants: tuple
    outcome: str


@dataclass(frozen=True)
class PlanningContext:
    """The authority this plan is scoped to. Immutable, and supplied — never inferred.

    ``workspace_id`` is here rather than read off the participant because a fresh pin has
    no evidence fields yet: deriving the lookup workspace from the very field this planner
    is about to write is circular, and it made the production lookup ask for a blank
    workspace (audit j#97062 finding 1).
    """

    workspace_id: str
    lane_id: str
    #: The exact closed launch-cause token a receipt-capable participant may be pinned
    #: with, supplied by the composition (audit j#97065 finding 2). A port answer is
    #: accepted only when it byte-equals this: "the port returned something non-empty" is
    #: not an authority, it is a shape. Carried here rather than re-spelled in e_110 so the
    #: token keeps exactly one owner.
    expected_update_cause: str

    def validate(self) -> "PlanningContext":
        """Every axis is a non-empty exact token, or the whole plan is refused."""
        for attr in ("workspace_id", "lane_id", "expected_update_cause"):
            value = getattr(self, attr)
            if type(value) is not str or value != value.strip() or not value:
                raise EvidencePlanRefused(
                    REFUSE_CONTEXT_INVALID,
                    "the planning context is not a set of exact non-empty tokens",
                )
        return self


class GenerationPort(Protocol):
    """``(assigned_name) -> LaunchGeneration | None``. Raising is a refusal."""

    def __call__(self, assigned_name: str) -> Any: ...  # pragma: no cover - shape only


class LifecyclePort(Protocol):
    """``(lane_id) -> (lane_generation, lifecycle_revision) | None``."""

    def __call__(self, lane_id: str) -> Any: ...  # pragma: no cover - shape only


class EvidencePort(Protocol):
    """The lane's live bound evidence for one provider, or ``None``."""

    def __call__(
        self,
        *,
        workspace_id: str,
        lane_id: str,
        provider: str,
        lane_generation: str,
        lifecycle_revision: str,
    ) -> Any: ...  # pragma: no cover - shape only


class UpdateCausePort(Protocol):
    """``(provider, blocker_id) -> typed launch cause | ""``.

    Injected rather than imported (audit j#97062 finding 4). The mapping from an observed
    screen to a launch cause is provider-registry knowledge in e_140; importing it here
    would add an e_110 -> e_140 reverse dependency, and re-spelling the vocabulary locally
    would give the same token two owners.
    """

    def __call__(self, provider: str, blocker_id: str) -> str: ...  # pragma: no cover


def _exact(value: object) -> str:
    """The token EXACTLY as the authority holds it, or ``""`` when it is not one.

    No strip, no coercion. The first cut normalised with ``str(value or "").strip()`` and
    then compared the RESULT, which laundered a padded, non-text or foreign representation
    into a canonical token before the authority check ever ran (audit j#97074):
    ``" issue_14741 "`` became authority for lane ``issue_14741``. A value that is not
    already canonical text is not a near-miss to be repaired into one — it is a different
    value, and on a receipt-capable path that is the difference between proving a relaunch
    and asserting one.

    ``type(value) is str``, NOT ``isinstance``. The first cut used ``isinstance`` and its
    docstring justified it: "a ``str`` subclass still compares byte-wise, so it is the same
    token". That was asserted, not measured, and it is false — a subclass can override
    ``__eq__``, so the comparison is whatever the subclass decides, including raising. A
    ``str`` subclass therefore is not plain text here. A bool, a number, ``bytes`` and an
    object with a helpful ``__str__`` are rejected for the older reason: each only becomes
    a token by being rendered into one.

    Nothing foreign is compared, rendered or truth-tested on the way to that decision. The
    type test comes first precisely so a hostile ``__eq__`` never runs (audit j#97083).
    """
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


def _present(value: object) -> bool:
    """Whether this slot carries ANYTHING, canonical or not.

    Deliberately not :func:`_exact`: for "a legacy participant must carry no evidence" the
    question is presence, not well-formedness. Asking ``_exact`` there would read a padded
    or non-text triplet as absent and wave the participant through as legacy — the same
    laundering this correction is about, inverted.

    Absent means literally ``None`` or literally empty plain text, and the test for it
    touches nothing else. The first cut asked ``value != ""``, which HANDS CONTROL to the
    value: an object whose ``__ne__`` raises turned a presence question into a raw
    ``OSError`` carrying a host path (audit j#97083). ``len`` on an exact ``str`` is the
    only operation here that a foreign object could influence, and the type test rules it
    out first. Everything else — non-text, ``str`` subclasses, padded text — is present,
    which is the fail-closed direction: present means "explain this", not "accept it".
    """
    if value is None:
        return False
    if type(value) is str and len(value) == 0:
        return False
    return True


class ReplacementEvidencePlanner:
    """Plan participants with their update evidence, or refuse (never partially)."""

    def __init__(
        self,
        *,
        generations: GenerationPort,
        lifecycle: LifecyclePort,
        evidence: EvidencePort,
        update_cause: UpdateCausePort,
        capability: Optional[Callable[[object], bool]] = None,
    ) -> None:
        self._generations = generations
        self._lifecycle = lifecycle
        self._evidence = evidence
        self._update_cause = update_cause
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
            answer = classify(action_id)
            if answer is not True and answer is not False:
                raise TypeError("capability answer is not an exact boolean")
            return answer
        except Exception as exc:  # noqa: BLE001 - an unclassifiable action is never legacy
            raise EvidencePlanRefused(
                REFUSE_UNKNOWN_ACTION_SHAPE,
                "the startup action id matches no known shape",
            ) from exc

    # -- planning -----------------------------------------------------------------------

    def plan(self, participants: Sequence[Any], context: PlanningContext) -> EvidencePlan:
        """Return the participants to plan the transaction with.

        Legacy plans come back byte-exact with the receipt store untouched. A
        receipt-capable participant is either planned WITH its evidence triplet or the
        whole plan is refused — there is no middle result, because a transaction holding
        some proven participants and some unproven ones would have to decide at run time
        which kind it is.
        """
        context = context.validate()
        pinned = () if participants is None else tuple(participants)
        if not pinned:
            return EvidencePlan(participants=(), outcome=PLAN_LEGACY_UNCHANGED)

        planned = []
        touched = False
        for pin in pinned:
            # Audit j#97065 finding 1: the context's lane was never compared, so a
            # participant from another lane planned happily inside this lane's transaction.
            pin_lane = _exact(getattr(pin, "lane_id", None))
            if not pin_lane or pin_lane != context.lane_id:
                raise EvidencePlanRefused(
                    REFUSE_LANE_OUT_OF_CONTEXT,
                    "a participant names a lane this plan is not scoped to",
                )
            generation = self._generation_for(pin)
            action_id = _exact(getattr(generation, "startup_action_id", None))
            if not action_id:
                # Refused BEFORE classification on purpose. Handing a padded id to the
                # capability port would let a shape test answer "not receipt-capable" and
                # route a capable action down the legacy path — fail-open in the one
                # direction this whole ticket exists to close.
                raise EvidencePlanRefused(
                    REFUSE_UNKNOWN_ACTION_SHAPE,
                    "the startup action id matches no known shape",
                )
            if not self._requires_receipt(action_id):
                planned.append(self._legacy_pin(pin))
                continue
            self._require_generation_identity(pin, generation, context)
            planned.append(self._receipt_pin(pin, action_id, context))
            touched = True
        return EvidencePlan(
            participants=tuple(planned),
            outcome=PLAN_EVIDENCE_PINNED if touched else PLAN_LEGACY_UNCHANGED,
        )

    def _generation_for(self, pin: Any) -> Any:
        """The startup action this participant's live generation belongs to.

        Read from the launch-generation authority, which is the store where a relaunch
        atomically supersedes the previous row — so it answers for the generation that is
        actually there, not for one that used to be.
        """
        assigned = _exact(getattr(pin, "assigned_name", None))
        if not assigned:
            raise EvidencePlanRefused(
                REFUSE_GENERATION_UNAVAILABLE, "participant has no canonical assigned name"
            )
        try:
            generation = self._generations(assigned)
        except Exception as exc:  # noqa: BLE001 - an unreadable authority is a refusal
            # The cause chain is kept for a debugger; the outward message never renders the
            # exception body, a host path, or the assigned name (audit j#97065 finding 3).
            raise EvidencePlanRefused(
                REFUSE_GENERATION_UNAVAILABLE,
                "the launch generation authority could not be read",
            ) from exc
        if generation is None:
            raise EvidencePlanRefused(
                REFUSE_GENERATION_UNAVAILABLE,
                "no launch generation is recorded for this participant",
            )
        if _exact(getattr(generation, "phase", None)) != "attested":
            # A pending generation has not proven it came up. Planning a replacement whose
            # evidence rests on it would rest on a launch that may never have happened.
            raise EvidencePlanRefused(
                REFUSE_GENERATION_NOT_ATTESTED,
                "the launch generation for this participant is not attested",
            )
        return generation

    def _require_generation_identity(self, pin: Any, generation: Any, context) -> None:
        """The live generation must be EXACTLY the participant this plan names.

        Audit j#97062 finding 1: comparing only role and lane let a foreign generation
        through. Every axis the generation carries is compared — including the workspace
        and the locator, which are the two a same-named slot in another workspace, or a
        recycled pane, would differ on.
        """
        for attr, expected in (
            ("workspace_id", context.workspace_id),
            ("lane_id", context.lane_id),
            ("role", _exact(getattr(pin, "role", None))),
            ("assigned_name", _exact(getattr(pin, "assigned_name", None))),
            ("locator", _exact(getattr(pin, "old_locator", None))),
        ):
            # An empty `expected` means the PARTICIPANT's own axis is not canonical, and it
            # must never be allowed to match an authority that is equally empty.
            if not expected or _exact(getattr(generation, attr, None)) != expected:
                raise EvidencePlanRefused(
                    REFUSE_GENERATION_MISMATCH,
                    "the live launch generation is not the participant this plan names",
                )

    def _legacy_pin(self, pin: Any):
        """A pre-#14741 participant: byte-exact, and the receipt store is never opened."""
        if any(
            _present(getattr(pin, attr, ""))
            for attr in (
                "evidence_workspace_id",
                "evidence_startup_action_id",
                "evidence_cause",
            )
        ):
            # A legacy generation carrying evidence is not a legacy generation. Refuse
            # rather than silently strip it or silently keep it.
            raise EvidencePlanRefused(
                REFUSE_DIVERGENT_PRE_PIN,
                "a participant whose action is not receipt-capable already carries an "
                "evidence triplet",
            )
        return pin

    def _receipt_pin(self, pin: Any, action_id: str, context: PlanningContext):
        from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

        lane_id = _exact(getattr(pin, "lane_id", None))
        provider = _exact(getattr(pin, "provider", None))
        assigned = _exact(getattr(pin, "assigned_name", None))
        workspace_id = context.workspace_id

        lane_generation, lifecycle_revision = self._current_lifecycle(lane_id)
        # Audit j#97062 finding 2: a receipt-capable participant must ALREADY pin the
        # lifecycle it is acting on, and that pin must be the lane's current one. The first
        # cut only compared when the pin was non-empty, so an empty pin was "consistent"
        # with anything — the transaction would then carry no lifecycle authority at all
        # while the plan believed it had checked one.
        pinned_generation = _exact(getattr(pin, "lane_generation", None))
        pinned_revision = _exact(getattr(pin, "lane_revision", None))
        if not pinned_generation or not pinned_revision:
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_MISMATCH,
                "a receipt-capable participant must pin the lane lifecycle it acts on",
            )
        if pinned_generation != lane_generation or pinned_revision != lifecycle_revision:
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_MISMATCH,
                "the participant's pinned lifecycle is not the lane's current one",
            )

        found = self._bound_evidence(
            workspace_id=workspace_id,
            lane_id=lane_id,
            provider=provider,
            lane_generation=lane_generation,
            lifecycle_revision=lifecycle_revision,
        )
        self._require_evidence_identity(
            found,
            action_id=action_id,
            workspace_id=workspace_id,
            lane_id=lane_id,
            provider=provider,
            assigned=assigned,
        )
        cause = self._typed_cause(
            provider,
            _exact(getattr(found, "blocker_id", None)),
            context.expected_update_cause,
        )
        self._require_no_divergent_pre_pin(pin, workspace_id, action_id, cause)

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
            evidence_workspace_id=workspace_id,
            evidence_startup_action_id=action_id,
            evidence_cause=cause,
            phase=pin.phase,
        )

    def _current_lifecycle(self, lane_id: str) -> tuple:
        try:
            lifecycle = self._lifecycle(lane_id)
        except Exception as exc:  # noqa: BLE001
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE,
                "the lane lifecycle authority could not be read",
            ) from exc
        if lifecycle is None:
            # `if not lifecycle` would ask the ANSWER whether it is empty, and a foreign
            # object answers that question with whatever it likes, including an exception.
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE,
                "the lane has no declared lifecycle generation and revision",
            )
        # Exactly two text tokens (audit j#97065 finding 4). A 1- or 3-element answer used
        # to escape as a raw `ValueError` from tuple unpacking, which is not a typed
        # refusal — and a bool or a number must not be rendered into something that looks
        # like a token.
        if isinstance(lifecycle, (str, bytes)) or not isinstance(lifecycle, (tuple, list)):
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE,
                "the lane lifecycle authority answered in an unusable shape",
            )
        if len(lifecycle) != 2 or not all(type(v) is str for v in lifecycle):
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE,
                "the lane lifecycle authority answered in an unusable shape",
            )
        lane_generation, lifecycle_revision = lifecycle
        if (
            lane_generation != lane_generation.strip()
            or lifecycle_revision != lifecycle_revision.strip()
            or not lane_generation
            or not lifecycle_revision
        ):
            raise EvidencePlanRefused(
                REFUSE_LIFECYCLE_UNAVAILABLE,
                "the lane's declared lifecycle pair is incomplete or not canonical",
            )
        return (lane_generation, lifecycle_revision)

    def _bound_evidence(self, **lookup):
        """The lane's live evidence, proven to be in the ``bound`` phase.

        Audit j#97062 finding 3: the production reader only returns bound rows, but this
        planner takes an injected port — so it verifies the phase itself rather than
        inheriting a guarantee from an implementation it does not control. Consumed
        evidence must never re-arm a plan.
        """
        try:
            found = self._evidence(**lookup)
        except Exception as exc:  # noqa: BLE001
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_UNAVAILABLE, "the receipt authority could not be read"
            ) from exc
        if found is None:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_UNAVAILABLE,
                "no live update evidence is bound to this exact generation",
            )
        if getattr(found, "bound", None) is not True:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_NOT_BOUND,
                "the update evidence for this generation is not in the bound phase",
            )
        return found

    def _require_evidence_identity(
        self, found, *, action_id, workspace_id, lane_id, provider, assigned
    ) -> None:
        key = getattr(found, "key", None)
        if key is None:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_MISMATCH, "the bound evidence carries no generation key"
            )
        for attr, expected in (
            ("startup_action_id", action_id),
            ("workspace_id", workspace_id),
            ("lane_id", lane_id),
            ("provider", provider),
            ("assigned_name", assigned),
        ):
            if not expected or _exact(getattr(key, attr, None)) != expected:
                raise EvidencePlanRefused(
                    REFUSE_EVIDENCE_MISMATCH,
                    "the bound evidence names a different generation than this participant",
                )

    def _typed_cause(self, provider: str, blocker_id: str, expected: str) -> str:
        """The typed LAUNCH cause for an observed screen (audit j#97062 finding 4).

        ``blocker_id`` is what was SEEN (``update_prompt_available`` /
        ``update_in_progress``); the cause a replacement pins is the closed launch token
        (``update_relaunch``). The first cut stored the screen id as the cause, which put a
        second vocabulary into a field every other surface reads with the first. The
        mapping is provider-registry knowledge, so it arrives through a port.
        """
        if not blocker_id:
            raise EvidencePlanRefused(
                REFUSE_EVIDENCE_MISMATCH, "the bound evidence carries no observed screen"
            )
        try:
            cause = self._update_cause(provider, blocker_id)
        except Exception as exc:  # noqa: BLE001
            raise EvidencePlanRefused(
                REFUSE_CAUSE_NOT_UPDATE_DERIVED,
                "the observed screen could not be classified",
            ) from exc
        if type(cause) is not str or cause != expected:
            # Non-empty is a shape, not an authority, and neither is "strips to the right
            # thing": a port answering with any token at all, or with a padded one, would
            # otherwise have been pinned as the cause.
            raise EvidencePlanRefused(
                REFUSE_CAUSE_NOT_UPDATE_DERIVED,
                "the observed screen is not the expected update-derived launch cause",
            )
        return cause

    def _require_no_divergent_pre_pin(self, pin, workspace_id, action_id, cause) -> None:
        for attr, expected in (
            ("evidence_workspace_id", workspace_id),
            ("evidence_startup_action_id", action_id),
            ("evidence_cause", cause),
        ):
            existing = getattr(pin, attr, "")
            if _present(existing) and (
                type(existing) is not str or existing != expected
            ):
                raise EvidencePlanRefused(
                    REFUSE_DIVERGENT_PRE_PIN,
                    "the participant already carries a different evidence triplet",
                )


__all__ = (
    "EvidencePlan",
    "EvidencePlanRefused",
    "PlanningContext",
    "REFUSE_CAUSE_NOT_UPDATE_DERIVED",
    "REFUSE_CONTEXT_INVALID",
    "REFUSE_LANE_OUT_OF_CONTEXT",
    "REFUSE_EVIDENCE_NOT_BOUND",
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
