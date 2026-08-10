"""Owner-approved disposition for a generation-mismatched receiver holding pending input.

Redmine #15193. **Proposed term — `generation mismatch disposition`**: the owner-approved,
exact-generation-bound action that releases a *stopped* managed receiver which the canonical
rails cannot converge because it carries a generation mismatch **and** an unsent composer
input at the same time. It is NOT a new lifecycle state, NOT a retire, and NOT a widening of
quarantine candidacy — it is a separate authorization that reuses the existing quarantine
close/replace actuation once the owner has approved the exact observed condition, including
what becomes of the pending input.

## The deadlock this exists to break

``sublane hibernate --execute`` re-probes the composer at its release boundary. A real unsent
input blocks the release with ``composer_pending_real`` and names ``owner_approved_quarantine``
as the safe next action (:mod:`...application.sublane_hibernate_toctou`). But
``sublane quarantine-inspect`` classifies that same receiver through
:func:`...domain.sublane_pending_composer.classify_pending_composer`, whose precedence puts
``generation_mismatch`` **above** the pending fact — so the receiver is
``not_quarantine_candidate`` and no approval template is minted
(:func:`...domain.quarantine_approval.decide_approval_readiness`).

Hibernate therefore points at quarantine, quarantine refuses, and the operator is left with
no supported rail — reproduced independently on #15110 j#102068, #15140 j#102064 and
#15195 j#102193 / j#102218. The prohibited "solutions" (force kill, raw Herdr/tmux, blind
Enter, discarding the composer unconditionally) are exactly what a missing rail invites.

## Why the precedence is still right

Refusing to replace a receiver whose generation cannot be proven is correct **by default**:
the approval names one composer of one agent generation, and a mismatched generation means
the thing now live may not be the thing the approval described. This module does not relax
that. It requires the owner to approve *over an explicitly named mismatch* — every axis of
the mismatch is a bound token, re-compared at action time. An approval that says "replace
this receiver" is refused; only one that says "replace this receiver, whose generation
mismatches on exactly these axes, discarding the pending input it holds" is admitted, and
only while that exact condition still holds.

## Invariants

- **The pending input is never silently discarded.** Every minted approval carries an
  explicit :data:`PENDING_EFFECT_DISCARDED_ON_REPLACE` / :data:`PENDING_EFFECT_PRESERVED`
  token, rendered in the template as a literal sentence. An unreadable composer yields
  :data:`DISPOSITION_COMPOSER_UNREADABLE`, never an assumed-empty discard.
- **A known deliverable is never destroyed.** A composer correlating to a known delivery
  marker routes back to the q-enter rail (:data:`DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER`),
  exactly as the quarantine approval already does.
- **Zero mutation on ambiguous / foreign / working / unreadable.** Each is its own typed
  refusal below, and a refusal never renders a template.
- **Exact-bind + action-time re-verification.** :func:`revalidate_disposition` re-compares
  every bound token — including the mismatch axis set and the pending identity — and returns
  typed drift reasons. Any drift is a refusal, so an approval minted for one generation can
  never act on another.
- **Value non-exposure.** :class:`DispositionFacts` has no field able to carry a composer
  body, digest, length or excerpt. Delivery-marker identities are the only free-form values,
  and those are ledger identities the surrounding surfaces already emit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    GENERATION_MISMATCH,
    PendingComposerClassification,
    ordered_generation_axes,
)

# ---------------------------------------------------------------------------
# Closed readiness vocabulary. Exactly one value is reported; only ``ready`` mints a template.
# ---------------------------------------------------------------------------

DISPOSITION_READY = "ready"
#: The repo scope could not be resolved to a workspace, so no identity can be pinned.
DISPOSITION_WORKSPACE_UNRESOLVED = "workspace_unresolved"
#: The managed inventory could not be read — proves nothing, never read as absence.
DISPOSITION_INVENTORY_UNREADABLE = "inventory_unreadable"
#: The composer / agent state could not be read, so the pending fact is unknown. Distinct
#: from "no pending input": an unprovable composer must never authorize a discard.
DISPOSITION_COMPOSER_UNREADABLE = "composer_unreadable"
#: No live managed receiver matches the exact (workspace, lane, role) identity.
DISPOSITION_RECEIVER_ABSENT = "receiver_absent"
#: Several live rows claim that identity — ambiguous / foreign; never pick one.
DISPOSITION_DUPLICATE_RECEIVER = "duplicate_receiver"
#: The row carries no readable integer revision, so an approval could not bind a generation.
DISPOSITION_REVISION_UNREADABLE = "revision_unreadable"
#: No usable identity attestation, so the attested generation cannot be bound.
DISPOSITION_ATTESTATION_UNREADABLE = "attestation_unreadable"
#: A live worker is mid-turn. Active work is never disposed of; wait for quiescence.
DISPOSITION_AGENT_WORKING = "agent_working"
#: The composer holds a KNOWN delivered marker: the remedy is q-enter, not replacement.
DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER = "known_marker_requires_q_enter"
#: The receiver is NOT in the #15193 shape — its generation matches, or its composer is
#: empty. Both are already served by the canonical rails, so this one refuses to duplicate
#: them: a matching generation goes to ``sublane quarantine``, an empty composer straight to
#: ``sublane hibernate``.
DISPOSITION_NOT_MISMATCH_WITH_PENDING = "not_generation_mismatch_with_pending"
#: The mismatch could not be attributed to any known axis, so an approval could not name the
#: condition it authorizes. Fail closed rather than bind an unnamed mismatch.
DISPOSITION_AXES_UNATTRIBUTED = "generation_axes_unattributed"
DISPOSITION_LIFECYCLE_UNREADABLE = "lane_lifecycle_unreadable"
DISPOSITION_LIFECYCLE_ABSENT = "lane_lifecycle_absent"
DISPOSITION_LIFECYCLE_PINS_INVALID = "lane_lifecycle_pins_invalid"
DISPOSITION_APPROVAL_INCOMPLETE = "disposition_approval_incomplete"

DISPOSITION_REASONS = frozenset(
    {
        DISPOSITION_READY,
        DISPOSITION_WORKSPACE_UNRESOLVED,
        DISPOSITION_INVENTORY_UNREADABLE,
        DISPOSITION_COMPOSER_UNREADABLE,
        DISPOSITION_RECEIVER_ABSENT,
        DISPOSITION_DUPLICATE_RECEIVER,
        DISPOSITION_REVISION_UNREADABLE,
        DISPOSITION_ATTESTATION_UNREADABLE,
        DISPOSITION_AGENT_WORKING,
        DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER,
        DISPOSITION_NOT_MISMATCH_WITH_PENDING,
        DISPOSITION_AXES_UNATTRIBUTED,
        DISPOSITION_LIFECYCLE_UNREADABLE,
        DISPOSITION_LIFECYCLE_ABSENT,
        DISPOSITION_LIFECYCLE_PINS_INVALID,
    }
)

# ---------------------------------------------------------------------------
# What the approval does to the pending input (Redmine #15193 acceptance: never silent).
# ---------------------------------------------------------------------------

#: The approved action replaces the receiver, and the unsent composer input it holds is lost
#: as a direct consequence. The template states this in words; the token makes it machine-
#: checkable so a reviewer can confirm the approval acknowledged a discard.
PENDING_EFFECT_DISCARDED_ON_REPLACE = "discarded_on_replace"
#: The approved action leaves the pending input in place (no replacement is authorized).
PENDING_EFFECT_PRESERVED = "preserved"

PENDING_EFFECTS = frozenset({PENDING_EFFECT_DISCARDED_ON_REPLACE, PENDING_EFFECT_PRESERVED})

#: The literal sentence the rendered template carries for a discarding disposition. Fixed
#: text: it must not be assembled from observed values, so it can never leak one.
PENDING_EFFECT_SENTENCE = {
    PENDING_EFFECT_DISCARDED_ON_REPLACE: (
        "この承認は、対象 receiver が保持する未送信 composer input を破棄する。"
        "破棄される入力は復元できない。"
    ),
    PENDING_EFFECT_PRESERVED: (
        "この承認は未送信 composer input を破棄しない。"
    ),
}

#: The placeholder the template leaves for the approval journal id — it does not exist until
#: the owner posts, so it is never predicted or fabricated.
DISPOSITION_JOURNAL_PLACEHOLDER = "<approval-journal-id>"

# ---------------------------------------------------------------------------
# Action-time drift vocabulary (Redmine #15193 requirement 3).
# ---------------------------------------------------------------------------

DRIFT_WORKSPACE = "workspace_drift"
DRIFT_LANE = "lane_drift"
DRIFT_ROLE = "role_drift"
DRIFT_ASSIGNED_NAME = "assigned_name_drift"
DRIFT_LOCATOR = "locator_drift"
DRIFT_AGENT_REVISION = "agent_revision_drift"
DRIFT_LANE_GENERATION = "lane_generation_drift"
DRIFT_LIFECYCLE_REVISION = "lifecycle_revision_drift"
DRIFT_ATTESTED_AT = "attested_generation_drift"
DRIFT_ACTION_GENERATION = "action_generation_drift"
#: The mismatch axes are no longer the ones the owner approved over — the receiver's
#: condition changed (an axis healed, or a new one appeared), so the approval describes a
#: state that no longer exists.
DRIFT_GENERATION_AXES = "generation_axes_drift"
#: The pending input is no longer the one observed at approval time (its correlated marker
#: set changed, or it appeared / vanished). Discarding a DIFFERENT input than the one the
#: owner saw is exactly the silent discard this rail forbids.
DRIFT_PENDING_IDENTITY = "pending_identity_drift"

DISPOSITION_DRIFT_REASONS = frozenset(
    {
        DRIFT_WORKSPACE,
        DRIFT_LANE,
        DRIFT_ROLE,
        DRIFT_ASSIGNED_NAME,
        DRIFT_LOCATOR,
        DRIFT_AGENT_REVISION,
        DRIFT_LANE_GENERATION,
        DRIFT_LIFECYCLE_REVISION,
        DRIFT_ATTESTED_AT,
        DRIFT_ACTION_GENERATION,
        DRIFT_GENERATION_AXES,
        DRIFT_PENDING_IDENTITY,
    }
)


def pending_identity(
    *, pending_observed: Optional[bool], correlated_marker_ids: tuple[str, ...]
) -> str:
    """A stable, content-free identity for the pending input being disposed of (pure).

    Redmine #15193 requirement 3 binds the approval to a "pending generation": re-running an
    approved discard must be refused once the input has *changed*, otherwise an approval the
    owner granted over one input would silently destroy a different one that arrived later.

    The identity is derived ONLY from facts that already cross this boundary — whether an
    input was observed at all, and the delivery-marker identities correlated to it. The body
    is never read, so this is deliberately NOT a content digest: it cannot distinguish two
    different uncorrelated inputs. That weakness is covered by the other bound tokens (a new
    input on a live receiver advances the revision / attestation the approval also pins), and
    it is the correct trade: hashing the body would put a body-derived value on a surface
    whose contract is that no such value exists.

    ``""`` when nothing pending was observed; ``"unreadable"`` when the fact was unknown, so
    an unreadable observation can never compare equal to a readable one.
    """
    if pending_observed is None:
        return "unreadable"
    if not pending_observed:
        return ""
    markers = tuple(dict.fromkeys(m for m in correlated_marker_ids if m))
    if not markers:
        return "pending:uncorrelated"
    digest = hashlib.sha256("\n".join(sorted(markers)).encode("utf-8")).hexdigest()[:16]
    return f"pending:markers:{digest}"


@dataclass(frozen=True)
class DispositionFacts:
    """The exact tokens a generation-mismatch disposition approval binds. Content-free.

    Every field is an identity / revision / generation / axis token. There is deliberately no
    field for composer text, a digest of it, a length, a pane excerpt or a filesystem path,
    so a renderer over this record cannot expose one.

    Beyond the quarantine approval's tokens this adds the three things #15193 requires an
    approval to pin: the mismatch ``generation_axes`` it is granted over, the
    ``pending_identity`` of the input it disposes of, and the lane's ``lane_generation`` /
    ``lifecycle_revision`` so an approval cannot cross into a superseded incarnation.
    """

    issue: str = ""
    lane: str = ""
    role: str = ""
    workspace_id: str = ""
    assigned_name: str = ""
    locator: str = ""
    agent_revision: int = -1
    lane_generation: int = -1
    lifecycle_revision: int = -1
    attested_at: str = ""
    action_generation: str = ""
    generation_axes: tuple[str, ...] = ()
    pending_identity: str = ""
    pending_effect: str = PENDING_EFFECT_PRESERVED
    observed_at: str = ""

    @property
    def revision_readable(self) -> bool:
        return isinstance(self.agent_revision, int) and self.agent_revision >= 0

    @property
    def lifecycle_pins_positive(self) -> bool:
        return all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (self.lane_generation, self.lifecycle_revision)
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "role": self.role,
            "workspace_id": self.workspace_id,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "agent_revision": self.agent_revision,
            "lane_generation": self.lane_generation,
            "lifecycle_revision": self.lifecycle_revision,
            "attested_at": self.attested_at,
            "action_generation": self.action_generation,
            "generation_axes": list(self.generation_axes),
            "pending_identity": self.pending_identity,
            "pending_effect": self.pending_effect,
            "observed_at": self.observed_at,
        }


def decide_disposition_readiness(
    *,
    facts: DispositionFacts,
    classification: PendingComposerClassification,
    receiver_present: Optional[bool],
    inventory_readable: bool,
    composer_readable: bool,
    agent_working: bool = False,
    duplicate_receiver: bool = False,
    lifecycle_reason: str = "",
) -> str:
    """The typed readiness reason for minting a disposition approval (pure).

    Precedence is most-fundamental first, so the reported reason names the ROOT refusal rather
    than a downstream symptom. The order deliberately mirrors
    :func:`...domain.quarantine_approval.decide_approval_readiness` — an operator moving
    between the two surfaces should not have to learn a second precedence — with the
    #15193-specific gates appended at the end.

    ``receiver_present is None`` means the inventory could not prove presence either way. This
    rail requires POSITIVE proof of presence and refuses on ``None``, which is deliberately
    stricter than :func:`...domain.quarantine_approval.decide_approval_readiness` (that one
    admits an unproven presence once the name and locator are known). The asymmetry is the
    stakes: an ordinary quarantine approval authorizes replacing a receiver, while this one
    additionally authorizes DISCARDING an unsent input. An input may only be discarded over a
    receiver we positively observed, never over one the inventory merely failed to disprove.
    """
    if not facts.workspace_id:
        return DISPOSITION_WORKSPACE_UNRESOLVED
    if not inventory_readable:
        return DISPOSITION_INVENTORY_UNREADABLE
    if duplicate_receiver:
        return DISPOSITION_DUPLICATE_RECEIVER
    if receiver_present is not True or not facts.assigned_name or not facts.locator:
        return DISPOSITION_RECEIVER_ABSENT
    if not composer_readable or classification.pending_observed is None:
        # An unprovable pending fact must never open a path that discards it.
        return DISPOSITION_COMPOSER_UNREADABLE
    if not facts.revision_readable:
        return DISPOSITION_REVISION_UNREADABLE
    if not facts.attested_at:
        return DISPOSITION_ATTESTATION_UNREADABLE
    if lifecycle_reason in {
        DISPOSITION_LIFECYCLE_UNREADABLE,
        DISPOSITION_LIFECYCLE_ABSENT,
        DISPOSITION_LIFECYCLE_PINS_INVALID,
    }:
        return lifecycle_reason
    if not facts.lifecycle_pins_positive:
        return DISPOSITION_LIFECYCLE_PINS_INVALID
    if agent_working:
        # Active work is never disposed of, regardless of what else is true.
        return DISPOSITION_AGENT_WORKING
    if classification.correlated_marker_id:
        # A known delivered marker is recoverable by re-submitting it; replacing the receiver
        # would destroy a real queued handoff. The remedy is q-enter, not this disposition.
        return DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER
    if not classification.generation_mismatch_with_pending:
        return DISPOSITION_NOT_MISMATCH_WITH_PENDING
    if not facts.generation_axes:
        return DISPOSITION_AXES_UNATTRIBUTED
    if not facts.action_generation:
        return DISPOSITION_AXES_UNATTRIBUTED
    return DISPOSITION_READY


def observed_facts_match(approved: DispositionFacts, observed: DispositionFacts) -> tuple[str, ...]:
    """Every bound token that drifted between approval and action time (pure).

    Returns typed drift reasons in a fixed order; empty means the approval still describes
    exactly what is live. The caller performs zero mutation on any non-empty result.

    Each token is compared for EQUALITY, never for "close enough": an approval is authority
    over one exact generation, so a token that moved means the owner approved a state that no
    longer exists. ``lane_generation`` / ``lifecycle_revision`` are mandatory positive
    approval pins, so they are always compared; an invalid approval cannot weaken this bind.
    """
    reasons: list[str] = []
    if approved.workspace_id != observed.workspace_id:
        reasons.append(DRIFT_WORKSPACE)
    if approved.lane != observed.lane:
        reasons.append(DRIFT_LANE)
    if approved.role != observed.role:
        reasons.append(DRIFT_ROLE)
    if approved.assigned_name != observed.assigned_name:
        reasons.append(DRIFT_ASSIGNED_NAME)
    if approved.locator != observed.locator:
        reasons.append(DRIFT_LOCATOR)
    if approved.agent_revision != observed.agent_revision:
        reasons.append(DRIFT_AGENT_REVISION)
    if approved.lane_generation != observed.lane_generation:
        reasons.append(DRIFT_LANE_GENERATION)
    if approved.lifecycle_revision != observed.lifecycle_revision:
        reasons.append(DRIFT_LIFECYCLE_REVISION)
    if approved.attested_at != observed.attested_at:
        reasons.append(DRIFT_ATTESTED_AT)
    if approved.action_generation != observed.action_generation:
        reasons.append(DRIFT_ACTION_GENERATION)
    if ordered_generation_axes(approved.generation_axes) != ordered_generation_axes(
        observed.generation_axes
    ):
        reasons.append(DRIFT_GENERATION_AXES)
    if approved.pending_identity != observed.pending_identity:
        reasons.append(DRIFT_PENDING_IDENTITY)
    return tuple(reasons)


def disposition_command(facts: DispositionFacts, *, journal: str = "") -> tuple[str, ...]:
    """The exact ``sublane quarantine --execute`` argv this disposition authorizes (pure).

    Returned as a token tuple so a caller cannot reshape quoting and tests compare tokens
    rather than formatting. The disposition rides the EXISTING quarantine actuation — the
    added flags are what make it a disposition rather than an ordinary quarantine, and the
    execute path refuses the combination unless every one of them is present.
    """
    return (
        "mozyo-bridge",
        "sublane",
        "quarantine",
        "--issue", facts.issue,
        "--lane", facts.lane,
        "--role", facts.role,
        "--assigned-name", facts.assigned_name,
        "--locator", facts.locator,
        "--action-generation", facts.action_generation,
        "--approved-revision", str(facts.agent_revision),
        "--approval-observed-at", facts.attested_at,
        "--approved-generation-axes", ",".join(facts.generation_axes),
        "--approved-pending-identity", facts.pending_identity,
        "--approved-pending-effect", facts.pending_effect,
        "--approved-lane-generation", str(facts.lane_generation),
        "--approved-lifecycle-revision", str(facts.lifecycle_revision),
        "--journal", journal or DISPOSITION_JOURNAL_PLACEHOLDER,
        "--execute",
    )


def render_disposition_template(facts: DispositionFacts, *, journal: str = "") -> str:
    """The pasteable owner-approval record for a READY disposition (pure).

    Deliberately NOT a ``[mozyo:workflow-event:gate=...]`` marker: the governed gate
    vocabulary is closed, and this is an action authorization rather than a workflow gate —
    the same choice :func:`...domain.quarantine_approval.render_approval_template` makes.

    The pending-input effect is rendered as a literal sentence AND as a bound token, so the
    approval cannot be read as authorizing a replacement without also authorizing what
    happens to the unsent input. Callers must only render this for :data:`DISPOSITION_READY`.
    """
    argv = " ".join(disposition_command(facts, journal=journal))
    return "\n".join(
        (
            "## Owner Approval — sublane generation-mismatch disposition (generation-bound)",
            "",
            f"- issue: {facts.issue}",
            f"- lane: `{facts.lane}`",
            f"- role: `{facts.role}`",
            f"- workspace_id: `{facts.workspace_id}`",
            f"- assigned_name: `{facts.assigned_name}`",
            f"- locator: `{facts.locator}`",
            f"- agent_revision: {facts.agent_revision}",
            f"- lane_generation: {facts.lane_generation}",
            f"- lifecycle_revision: {facts.lifecycle_revision}",
            f"- attested_at: `{facts.attested_at}`",
            f"- action_generation: `{facts.action_generation}`",
            f"- generation_axes: `{','.join(facts.generation_axes)}`",
            f"- pending_identity: `{facts.pending_identity}`",
            f"- pending_effect: `{facts.pending_effect}`",
            f"- observed_at: `{facts.observed_at}`",
            "- approved_action: replace this exact managed receiver over the named "
            "generation mismatch (no force kill, no raw Herdr/tmux, no blind Enter)",
            "",
            PENDING_EFFECT_SENTENCE.get(facts.pending_effect, ""),
            "",
            "承認は上記 exact generation と mismatch axes に束縛される。receiver の "
            "revision / attested generation / locator / axes / pending identity が変化した "
            "場合、`--execute` は action-time 再照合で fail-closed になる。同一 token での "
            "再実行は idempotent。",
            "",
            "```",
            argv,
            "```",
        )
    )


__all__ = (
    "DISPOSITION_AGENT_WORKING",
    "DISPOSITION_APPROVAL_INCOMPLETE",
    "DISPOSITION_ATTESTATION_UNREADABLE",
    "DISPOSITION_AXES_UNATTRIBUTED",
    "DISPOSITION_COMPOSER_UNREADABLE",
    "DISPOSITION_DRIFT_REASONS",
    "DISPOSITION_DUPLICATE_RECEIVER",
    "DISPOSITION_INVENTORY_UNREADABLE",
    "DISPOSITION_JOURNAL_PLACEHOLDER",
    "DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER",
    "DISPOSITION_LIFECYCLE_ABSENT",
    "DISPOSITION_LIFECYCLE_PINS_INVALID",
    "DISPOSITION_LIFECYCLE_UNREADABLE",
    "DISPOSITION_NOT_MISMATCH_WITH_PENDING",
    "DISPOSITION_READY",
    "DISPOSITION_REASONS",
    "DISPOSITION_RECEIVER_ABSENT",
    "DISPOSITION_REVISION_UNREADABLE",
    "DISPOSITION_WORKSPACE_UNRESOLVED",
    "DRIFT_ACTION_GENERATION",
    "DRIFT_AGENT_REVISION",
    "DRIFT_ASSIGNED_NAME",
    "DRIFT_ATTESTED_AT",
    "DRIFT_GENERATION_AXES",
    "DRIFT_LANE",
    "DRIFT_LANE_GENERATION",
    "DRIFT_LIFECYCLE_REVISION",
    "DRIFT_LOCATOR",
    "DRIFT_PENDING_IDENTITY",
    "DRIFT_ROLE",
    "DRIFT_WORKSPACE",
    "PENDING_EFFECTS",
    "PENDING_EFFECT_DISCARDED_ON_REPLACE",
    "PENDING_EFFECT_PRESERVED",
    "PENDING_EFFECT_SENTENCE",
    "DispositionFacts",
    "decide_disposition_readiness",
    "disposition_command",
    "observed_facts_match",
    "pending_identity",
    "render_disposition_template",
)
