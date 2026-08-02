"""Plan (or resume) the durable recovery of a vanished gateway (Redmine #14741 j#97147).

The authority half of the vanished-gateway heal, with no live effect of any kind: nothing
here launches, closes, sends or appends. It answers two questions and writes at most one
durable row.

**Is this launch's recovery a plain heal, or does it owe an identity receipt?** Read from
the participant's own CURRENT launch-generation row under an explicit home, matched on every
identity axis, and classified from the action id's SHAPE (j#96892 / j#97105). Only an exact
legacy ``startup-<64hex>`` is `legacy_direct`, and that path opens no receipt store and
writes no transaction -- which is what keeps every pre-#14741 heal byte-invariant. Missing,
unreadable, pending, mismatched or unclassifiable is a typed refusal; there is no fallback,
because "assume legacy" is the fail-open this whole ticket exists to close.

**Is this a new recovery or the same one again?** The action id is deterministic
(:mod:`...domain.vanished_gateway_recovery`), so a retry addresses the row the first attempt
wrote. A stored row is compared as a whole and RESUMED -- never re-planned, re-read against
the current generation, enriched or superseded (j#97121): past the plan, the manifest is the
authority, and the world is allowed to have moved on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E501
    OUTCOME_LEGACY_DIRECT,
    is_recovery_action_id,
    recovery_action_id_for_pin,
    OUTCOME_RECEIPT_PLANNED,
    OUTCOME_REPLAYED,
    REDISPATCH_GATEWAY_ONCE,
    REFUSE_EVIDENCE_UNAVAILABLE,
    REFUSE_FOREIGN_TRANSACTION,
    REFUSE_GENERATION_MISMATCH,
    REFUSE_GENERATION_UNAVAILABLE,
    REFUSE_REQUEST_INVALID,
    REFUSE_UNKNOWN_ACTION_SHAPE,
    RESUME_GATE,
    ParticipantAuthority,
    RecoveryDecision,
    RequestAnchor,
    recovery_action_id,
    refuse,
)

#: The transaction authority could not be opened, read or decoded.
REFUSE_TRANSACTION_UNAVAILABLE = "transaction_unavailable"
#: ``home`` is not a plain explicit path.
REFUSE_HOME_INVALID = "home_invalid"
#: An explicit replay was asked for an id that is not a recovery action id.
REFUSE_ACTION_ID_INVALID = "action_id_invalid"
#: The caller pre-pinned evidence that is not what the planner proved.
REFUSE_EVIDENCE_DIVERGENT = "evidence_divergent"

#: A recovery is its own first generation. It is never a re-anchor of someone else's action,
#: so there is no earlier generation for it to supersede.
RECOVERY_ACTION_GENERATION = 1


@dataclass(frozen=True)
class RecoveryPlan:
    """The durable outcome of planning, with the row it addresses. (pure value)"""

    decision: RecoveryDecision
    action_id: str = ""
    participants: tuple = ()

    @property
    def refused(self) -> bool:
        return self.decision.refused


def _pointers(anchor: RequestAnchor):
    """The decision and continuation a recovery row carries.

    Both name the ORIGINAL implementation request: the decision is the durable record that
    authorised the work, and the continuation is what a completed recovery must re-deliver.
    The gateway's own semantic action is used, never the worker's -- two continuations that
    differ only in who redispatches would otherwise be indistinguishable in a stored row.
    """
    from mozyo_bridge.core.state.replacement_transaction_model import (
        ContinuationPointer,
        DecisionPointer,
    )

    decision = DecisionPointer(
        source=anchor.source, issue_id=anchor.issue_id, journal_id=anchor.journal_id
    )
    continuation = ContinuationPointer(
        source=anchor.source,
        issue_id=anchor.issue_id,
        journal_id=anchor.journal_id,
        expected_gate=RESUME_GATE,
        next_semantic_action=REDISPATCH_GATEWAY_ONCE,
    )
    return decision, continuation


def _current_generation(home: Optional[Path], authority: ParticipantAuthority):
    """The participant's own current launch-generation row, or a typed refusal."""
    from mozyo_bridge.core.state.herdr_launch_generation import (
        GENERATION_ATTESTED,
        HerdrLaunchGenerationStore,
    )

    try:
        row = HerdrLaunchGenerationStore(home=home).read(authority.assigned_name)
    except Exception:  # noqa: BLE001 - an unreadable authority is a refusal, not a guess
        return refuse(REFUSE_GENERATION_UNAVAILABLE, "the launch generation authority could not be read")
    if row is None:
        return refuse(REFUSE_GENERATION_UNAVAILABLE, "no launch generation is recorded")
    for attr, expected in (
        ("workspace_id", authority.workspace_id),
        ("lane_id", authority.lane_id),
        ("role", authority.role),
        ("assigned_name", authority.assigned_name),
        ("locator", authority.old_locator),
    ):
        if getattr(row, attr, None) != expected:
            return refuse(
                REFUSE_GENERATION_MISMATCH,
                "the current launch generation is not the participant this recovery names",
            )
    if getattr(row, "phase", None) != GENERATION_ATTESTED:
        return refuse(
            REFUSE_GENERATION_MISMATCH, "the current launch generation is not attested"
        )
    return row


def _pin(authority: ParticipantAuthority, *, with_evidence: bool = True):
    from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

    return ParticipantPin(
        lane_id=authority.lane_id,
        role=authority.role,
        provider=authority.provider,
        assigned_name=authority.assigned_name,
        old_locator=authority.old_locator,
        is_self=False,
        lane_revision=authority.lane_revision,
        lane_generation=authority.lane_generation,
        evidence_workspace_id=authority.evidence_workspace_id if with_evidence else "",
        evidence_startup_action_id=(
            authority.evidence_startup_action_id if with_evidence else ""
        ),
        evidence_cause=authority.evidence_cause if with_evidence else "",
    )


def _require_home(home) -> Optional[Path]:
    """A plain explicit path, or nothing.

    ``None`` is rejected rather than resolved (audit j#97151 R3): "wherever this build keeps
    its home" is the operator's SHARED home, and a recovery that silently planned against it
    because a caller forgot an argument is the kind of thing that is only noticed afterwards.
    A RELATIVE path is rejected for the same reason one layer down (j#97157 R7): it names a
    different authority from every directory.
    """
    if not isinstance(home, Path):
        return None
    text = str(home)
    if not text or text != text.strip():
        return None
    if not home.is_absolute():
        # A relative home is resolved against the CURRENT DIRECTORY, so the same recovery
        # would address a different authority depending on where it ran (audit j#97157 R7).
        return None
    return home


def _open(store_factory):
    """Open the transaction authority, lazily and only when one is genuinely needed.

    Every refusal above this line -- bad home, missing generation, mismatch, unknown shape,
    legacy, unplannable evidence -- returns before it, so none of them creates
    ``state.sqlite``. The first cut called ``store.get`` before classifying anything, and a
    refused recovery left a database behind (measured).
    """
    try:
        return store_factory()
    except Exception:  # noqa: BLE001 - KI / SystemExit / GeneratorExit propagate
        return None


def _get(store, key):
    """``store.get``, with the store's answer treated as input rather than as truth."""
    try:
        return True, store.get(key)
    except Exception:  # noqa: BLE001
        return False, None


def _raw(value: object) -> str:
    """The value only if it is already plain exact text -- no strip, no subclass."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


def stored_row_is_this_recovery(stored, *, key, anchor, action_id) -> bool:
    """Is this stored row THIS recovery, compared as raw stored fields? (pure)

    Audit j#97151 R4: the first cut compared value objects, which normalise, so a row whose
    raw ``decision_source`` was ``" redmine "`` and whose participant lane was padded read
    as an exact match. The record exposes its raw columns; those are what the durable
    authority actually is, so those are what get compared.
    """
    from mozyo_bridge.core.state.replacement_transaction_model import (
        ParticipantPinError,
        decode_participants,
        encode_participants,
    )

    if _raw(getattr(stored, "workspace_id", None)) != key.workspace_id:
        return False
    if _raw(getattr(stored, "action_id", None)) != action_id:
        return False
    generation = getattr(stored, "action_generation", None)
    if type(generation) is not int or generation != RECOVERY_ACTION_GENERATION:
        # `type is int` before the comparison: `True == 1` in Python, so a bool would
        # otherwise pass as generation 1.
        return False
    for attr, expected in (
        ("decision_source", anchor.source),
        ("decision_issue_id", anchor.issue_id),
        ("decision_journal", anchor.journal_id),
        ("continuation_source", anchor.source),
        ("continuation_issue_id", anchor.issue_id),
        ("continuation_journal", anchor.journal_id),
        ("continuation_expected_gate", RESUME_GATE),
        ("continuation_next_action", REDISPATCH_GATEWAY_ONCE),
    ):
        if _raw(getattr(stored, attr, None)) != expected:
            return False
    manifest = _raw(getattr(stored, "participants_manifest", None))
    if not manifest:
        return False
    try:
        pins = decode_participants(manifest)
        if encode_participants(pins) != manifest:
            # A manifest that does not round-trip is not the manifest this build writes:
            # padding, key order or an extra field survived somewhere.
            return False
    except (ParticipantPinError, ValueError, TypeError):
        return False
    if len(pins) != 1:
        return False
    return (
        recovery_action_id_for_pin(anchor, pins[0], workspace_id=key.workspace_id)
        == action_id
    )


def _replayed(stored, key, anchor, action_id, *, outcome=OUTCOME_REPLAYED) -> RecoveryPlan:
    """Verify a stored row and build the resume, with the record itself as untrusted input.

    Audit j#97157 R5: only ``store.get`` was guarded, so a record whose ``workspace_id``
    property raised carried a host path and a workflow marker straight out. A row is data
    that arrived from outside; reading its attributes, decoding its manifest and recomputing
    its id are all part of trusting it, so all three happen inside this boundary.
    """
    from mozyo_bridge.core.state.replacement_transaction_model import decode_participants

    try:
        if not stored_row_is_this_recovery(stored, key=key, anchor=anchor, action_id=action_id):
            return RecoveryPlan(
                decision=refuse(
                    REFUSE_FOREIGN_TRANSACTION,
                    "the stored row at this key is not this recovery",
                ),
                action_id=action_id,
            )
        pin = decode_participants(stored.participants_manifest)[0]
    except Exception:  # noqa: BLE001 - KI / SystemExit / GeneratorExit propagate
        return RecoveryPlan(
            decision=refuse(
                REFUSE_TRANSACTION_UNAVAILABLE,
                "the stored transaction could not be read",
            ),
            action_id=action_id,
        )
    return RecoveryPlan(
        decision=RecoveryDecision(outcome=outcome, action_id=action_id),
        action_id=action_id,
        participants=(pin,),
    )


def replay_explicit_recovery(
    *, reader, workspace_id: str, action_id: str, anchor: RequestAnchor
) -> RecoveryPlan:
    """Resume the recovery filed under an EXPLICIT action id. Reads no world state.

    The separate entry point audit j#97151 R1 asks for. A replay must address the row that
    already exists, and re-deriving the id from today's authority cannot do that: once the
    generation, the locator or the evidence has moved on, the derived id is a DIFFERENT
    action and the stored row becomes unreachable. So the id is an input here, its grammar
    is checked before anything is opened, and the current launch generation, the lifecycle
    and the receipt store are never consulted at all.

    ``reader`` is a NON-CREATING read (audit j#97157 R6). The first cut opened the
    read-write store, so asking about a recovery that does not exist created the database --
    a lookup that writes is not a zero-write replay.
    """
    if not is_recovery_action_id(action_id):
        return RecoveryPlan(
            decision=refuse(REFUSE_ACTION_ID_INVALID, "not a recovery action id")
        )
    if not isinstance(anchor, RequestAnchor) or not _raw(workspace_id):
        return RecoveryPlan(decision=refuse(REFUSE_REQUEST_INVALID, "not an exact request"))

    from mozyo_bridge.core.state.replacement_transaction import (
        ReplacementTransactionKey,
    )

    key = ReplacementTransactionKey(workspace_id, action_id)
    try:
        rows = reader()
    except Exception:  # noqa: BLE001
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the transaction authority could not be read"),
            action_id=action_id,
        )
    if rows is None:
        # The non-creating reader's fail-closed answer: an unknown / newer / partial
        # component schema. Not "no such row".
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the transaction component is not readable"),
            action_id=action_id,
        )
    try:
        matched = [
            row
            for row in rows
            if _raw(getattr(row, "action_id", None)) == action_id
            and _raw(getattr(row, "workspace_id", None)) == key.workspace_id
        ]
    except Exception:  # noqa: BLE001 - a hostile row is input, not truth
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the stored transaction could not be read"),
            action_id=action_id,
        )
    if len(matched) != 1:
        return RecoveryPlan(
            decision=refuse(REFUSE_FOREIGN_TRANSACTION, "no such recovery transaction"),
            action_id=action_id,
        )
    return _replayed(matched[0], key, anchor, action_id)


def plan_fresh_recovery(
    *, store_factory, home, anchor: RequestAnchor, authority: ParticipantAuthority
) -> RecoveryPlan:
    """Classify a recovery from today's authority and, if it owes a receipt, plan it.

    FRESH only: it never resumes an id it was handed, and it never looks for one. What it
    may write is a single ``plan_transaction``, and only after the evidenced manifest is
    fixed -- so the action id is the identity of what the row actually contains.
    """
    resolved_home = _require_home(home)
    if resolved_home is None:
        return RecoveryPlan(
            decision=refuse(REFUSE_HOME_INVALID, "an explicit plain home path is required")
        )
    if not isinstance(anchor, RequestAnchor) or not isinstance(
        authority, ParticipantAuthority
    ):
        return RecoveryPlan(decision=refuse(REFUSE_REQUEST_INVALID, "not an exact request"))

    generation = _current_generation(resolved_home, authority)
    if isinstance(generation, RecoveryDecision):
        return RecoveryPlan(decision=generation)

    from mozyo_bridge.core.state.startup_action_capability import (
        CAPABILITY_IDENTITY_RECEIPT,
        CAPABILITY_LEGACY,
        action_capability,
    )

    try:
        capability = action_capability(getattr(generation, "startup_action_id", None))
    except Exception:  # noqa: BLE001 - an unclassifiable action is never legacy
        return RecoveryPlan(
            decision=refuse(
                REFUSE_UNKNOWN_ACTION_SHAPE, "the startup action id matches no known shape"
            )
        )

    if capability == CAPABILITY_LEGACY:
        if authority.carries_evidence:
            # A legacy launch cannot have produced update evidence, so a caller offering
            # some is describing a different participant than the one that is there.
            return RecoveryPlan(
                decision=refuse(
                    REFUSE_EVIDENCE_DIVERGENT,
                    "a legacy participant carries no update evidence",
                )
            )
        # The pre-#14741 heal: no receipt store, no transaction store, no row.
        return RecoveryPlan(
            decision=RecoveryDecision(
                outcome=OUTCOME_LEGACY_DIRECT,
                action_id=recovery_action_id(anchor, authority),
            ),
            action_id=recovery_action_id(anchor, authority),
        )
    if capability != CAPABILITY_IDENTITY_RECEIPT:
        return RecoveryPlan(
            decision=refuse(
                REFUSE_UNKNOWN_ACTION_SHAPE, "unrecognised startup action capability"
            )
        )

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner_composition import (  # noqa: E501
        plan_participants_with_evidence,
    )

    # The BASE participant: whatever evidence the caller offered is not authority, so it is
    # not what gets planned. It is compared against the planner's answer afterwards.
    planning = plan_participants_with_evidence(
        [_pin(authority, with_evidence=False)],
        home=resolved_home,
        workspace_id=authority.workspace_id,
        lane_id=authority.lane_id,
    )
    if planning.refused:
        return RecoveryPlan(
            decision=refuse(REFUSE_EVIDENCE_UNAVAILABLE, planning.refusal)
        )
    planned = tuple(planning.participants)
    if len(planned) != 1 or not planned[0].evidence_startup_action_id:
        return RecoveryPlan(
            decision=refuse(
                REFUSE_EVIDENCE_UNAVAILABLE,
                "a receipt-capable recovery must pin exactly one evidenced participant",
            )
        )
    pin = planned[0]
    if authority.carries_evidence and (
        authority.evidence_workspace_id != pin.evidence_workspace_id
        or authority.evidence_startup_action_id != pin.evidence_startup_action_id
        or authority.evidence_cause != pin.evidence_cause
    ):
        return RecoveryPlan(
            decision=refuse(
                REFUSE_EVIDENCE_DIVERGENT,
                "the offered evidence is not the evidence this launch proved",
            )
        )

    # The id of the FINAL manifest, and only now is a transaction store opened.
    action_id = recovery_action_id_for_pin(
        anchor, pin, workspace_id=authority.workspace_id
    )
    from mozyo_bridge.core.state.replacement_transaction import (
        ReplacementTransactionKey,
    )

    store = _open(store_factory)
    if store is None:
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the transaction authority could not be opened"),
            action_id=action_id,
        )
    key = ReplacementTransactionKey(authority.workspace_id, action_id)
    ok, existing = _get(store, key)
    if not ok:
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the transaction authority could not be read"),
            action_id=action_id,
        )
    if existing is not None:
        return _replayed(existing, key, anchor, action_id)

    decision, continuation = _pointers(anchor)
    try:
        store.plan_transaction(
            key,
            action_generation=RECOVERY_ACTION_GENERATION,
            decision=decision,
            continuation=continuation,
            participants=[pin],
        )
    except Exception:  # noqa: BLE001
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the transaction could not be planned"),
            action_id=action_id,
        )
    ok, current = _get(store, key)
    if not ok or current is None:
        return RecoveryPlan(
            decision=refuse(REFUSE_TRANSACTION_UNAVAILABLE, "the planned row could not be read back"),
            action_id=action_id,
        )
    # OBSERVATIONAL, not causal (ruling j#97162). `plan_transaction` answers
    # `applied=True` for a pristine re-plan too, so nothing at this seam can say WHICH run
    # inserted the row -- and with a deterministic id both rows are byte-identical anyway.
    # So `receipt_planned` means exactly "this fresh path confirmed an exact durable row is
    # ready after its own plan call", and claims nothing about authorship. Writer identity,
    # if it is ever needed, wants an atomic result from the store rather than a guess here.
    return _replayed(current, key, anchor, action_id, outcome=OUTCOME_RECEIPT_PLANNED)


__all__ = (
    "RECOVERY_ACTION_GENERATION",
    "stored_row_is_this_recovery",
    "REFUSE_ACTION_ID_INVALID",
    "REFUSE_EVIDENCE_DIVERGENT",
    "REFUSE_HOME_INVALID",
    "REFUSE_TRANSACTION_UNAVAILABLE",
    "RecoveryPlan",
    "plan_fresh_recovery",
    "replay_explicit_recovery",
)
