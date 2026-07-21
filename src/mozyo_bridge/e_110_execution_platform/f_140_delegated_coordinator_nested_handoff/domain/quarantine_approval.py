"""Generation-bound quarantine approval readiness + template (pure; Redmine #14234).

``sublane quarantine --execute`` requires five exact tokens the operator cannot obtain from
any public read-only surface: ``--assigned-name``, ``--locator``, ``--action-generation``,
``--approval-observed-at`` and ``--approved-revision``. The quarantine preflight *observes*
all of them internally, but its ``QuarantineOutcome`` payload surfaces only the collapsed
classification label, and ``sublane list`` returns neither the assigned name, the agent
revision, nor the attested generation. So a positive generation-bound approval could only be
assembled from raw Herdr, the internal Python API, pane body, or a guess — which is what
stalled the #14163 six-lane drain.

This module is the **pure** half of the fix: given the exact observed facts it decides whether
a positive approval may be minted at all, and renders the pasteable approval record. It holds
no IO and no Redmine/Herdr knowledge, so every branch is test-pinnable.

Two invariants:

- **Value non-exposure.** Only identity / revision / generation tokens and a classification
  cross this boundary. The composer body, its hash, its length, raw ANSI, filesystem paths and
  credentials never enter — :class:`ApprovalFacts` has no field that could carry them, so the
  renderer cannot leak what it was never given (the same shape rule
  :class:`...domain.sublane_pending_composer.PendingComposerSignal` uses).
- **Fail-closed minting.** :func:`decide_approval_readiness` returns a typed reason from a
  CLOSED vocabulary, and a template is rendered ONLY for :data:`APPROVAL_READY`. An unreadable
  inventory / composer / attestation, a duplicate or foreign receiver, and an unreadable
  revision each refuse the approval and say so, rather than emitting a template whose tokens
  the execute-time fence would then reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (
    CORRELATED_KNOWN_MARKER,
    PendingComposerClassification,
)

# ---------------------------------------------------------------------------
# Closed readiness vocabulary (machine-readable; literal regardless of UI language).
#
# Exactly one of these is reported. ``ready`` is the ONLY value that mints a template; every
# other value is a fail-closed refusal that names why a positive approval cannot be built.
# ---------------------------------------------------------------------------

APPROVAL_READY = "ready"
#: The repo scope could not be resolved to a workspace, so no identity can be pinned.
APPROVAL_WORKSPACE_UNRESOLVED = "workspace_unresolved"
#: The managed inventory itself could not be read — proves nothing, never read as absence.
APPROVAL_INVENTORY_UNREADABLE = "inventory_unreadable"
#: The composer / agent state could not be read, so the pending fact is unknown.
APPROVAL_COMPOSER_UNREADABLE = "composer_unreadable"
#: No live managed receiver matches the exact (workspace, lane, role) identity.
APPROVAL_RECEIVER_ABSENT = "receiver_absent"
#: More than one live row claims that identity — ambiguous / foreign; never pick one.
APPROVAL_DUPLICATE_RECEIVER = "duplicate_receiver"
#: The row carries no readable integer revision, so an approval could not bind a generation.
APPROVAL_REVISION_UNREADABLE = "revision_unreadable"
#: No usable identity attestation, so the attested generation cannot be bound.
APPROVAL_ATTESTATION_UNREADABLE = "attestation_unreadable"
#: The composer holds a KNOWN delivered marker: the remedy is q-enter, not replacement.
APPROVAL_KNOWN_MARKER_REQUIRES_Q_ENTER = "known_marker_requires_q_enter"
#: A readable receiver whose classification is not quarantine-eligible (working agent,
#: unattested identity, generation mismatch, or simply no pending composer).
APPROVAL_NOT_QUARANTINE_CANDIDATE = "not_quarantine_candidate"

APPROVAL_REASONS = frozenset(
    {
        APPROVAL_READY,
        APPROVAL_WORKSPACE_UNRESOLVED,
        APPROVAL_INVENTORY_UNREADABLE,
        APPROVAL_COMPOSER_UNREADABLE,
        APPROVAL_RECEIVER_ABSENT,
        APPROVAL_DUPLICATE_RECEIVER,
        APPROVAL_REVISION_UNREADABLE,
        APPROVAL_ATTESTATION_UNREADABLE,
        APPROVAL_KNOWN_MARKER_REQUIRES_Q_ENTER,
        APPROVAL_NOT_QUARANTINE_CANDIDATE,
    }
)

#: The placeholder the rendered template leaves for the approval journal id. The id does not
#: exist until the owner actually posts the approval, so it is NEVER predicted or fabricated —
#: the operator substitutes the real journal id after posting.
APPROVAL_JOURNAL_PLACEHOLDER = "<approval-journal-id>"


@dataclass(frozen=True)
class ApprovalFacts:
    """The exact observed tokens a generation-bound approval binds. Content-free by shape.

    Every field is an identity / revision / generation token or a classification label. There
    is deliberately no field for composer text, a digest, a length, a pane excerpt, or a
    filesystem path, so a renderer over this record cannot expose one.

    ``observed_at`` is when this inspection ran; ``attested_at`` is the receiver's attested
    generation timestamp, which is what the execute-time stale-generation fence compares.
    """

    issue: str = ""
    lane: str = ""
    role: str = ""
    workspace_id: str = ""
    assigned_name: str = ""
    locator: str = ""
    agent_revision: int = -1
    attested_at: str = ""
    action_generation: str = ""
    observed_at: str = ""

    @property
    def revision_readable(self) -> bool:
        return isinstance(self.agent_revision, int) and self.agent_revision >= 0

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "role": self.role,
            "workspace_id": self.workspace_id,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "agent_revision": self.agent_revision,
            "attested_at": self.attested_at,
            "action_generation": self.action_generation,
            "observed_at": self.observed_at,
        }


def decide_approval_readiness(
    *,
    facts: ApprovalFacts,
    classification: PendingComposerClassification,
    receiver_present: Optional[bool],
    inventory_readable: bool,
    composer_readable: bool,
    duplicate_receiver: bool = False,
) -> str:
    """The typed readiness reason for minting a positive approval (pure).

    Precedence is most-fundamental first, so the reported reason names the ROOT refusal rather
    than a downstream symptom (an unreadable inventory would otherwise surface as the
    classification's collapsed ``inventory_unreadable``, hiding whether the inventory or the
    composer was the unreadable one — they are indistinguishable in the classification label).

    ``receiver_present is None`` means the inventory could not prove presence either way; it is
    never read as absence.
    """
    if not facts.workspace_id:
        return APPROVAL_WORKSPACE_UNRESOLVED
    if not inventory_readable:
        return APPROVAL_INVENTORY_UNREADABLE
    if duplicate_receiver:
        return APPROVAL_DUPLICATE_RECEIVER
    if receiver_present is False or not facts.assigned_name or not facts.locator:
        return APPROVAL_RECEIVER_ABSENT
    if not composer_readable:
        return APPROVAL_COMPOSER_UNREADABLE
    if not facts.revision_readable:
        return APPROVAL_REVISION_UNREADABLE
    if not facts.attested_at:
        return APPROVAL_ATTESTATION_UNREADABLE
    if classification.label == CORRELATED_KNOWN_MARKER:
        # A known delivered marker is recoverable by re-submitting it; replacing the receiver
        # would destroy a real queued handoff. The remedy is q-enter, not this approval.
        return APPROVAL_KNOWN_MARKER_REQUIRES_Q_ENTER
    if not classification.quarantine_candidate:
        return APPROVAL_NOT_QUARANTINE_CANDIDATE
    if not facts.action_generation:
        return APPROVAL_NOT_QUARANTINE_CANDIDATE
    return APPROVAL_READY


def approval_command(facts: ApprovalFacts, *, journal: str = "") -> tuple[str, ...]:
    """The exact ``sublane quarantine --execute`` argv the approval authorizes (pure).

    Returned as a token tuple (not a joined string) so a caller cannot accidentally reshape
    quoting, and so tests compare tokens rather than formatting. The journal id defaults to
    :data:`APPROVAL_JOURNAL_PLACEHOLDER` because it does not exist until the owner posts.
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
        "--journal", journal or APPROVAL_JOURNAL_PLACEHOLDER,
        "--execute",
    )


def render_approval_template(facts: ApprovalFacts, *, journal: str = "") -> str:
    """The pasteable owner-approval record for a READY inspection (pure).

    Deliberately NOT a new ``[mozyo:workflow-event:gate=...]`` marker: the governed gate
    vocabulary is closed, and this is an action authorization, not a workflow gate. The durable
    authority is the structured token list below — which is exactly what
    ``sublane quarantine --execute`` re-verifies against live state, so an approval minted from
    a drifted observation is refused at execute time rather than silently applied.

    Callers must only render this when :func:`decide_approval_readiness` returned
    :data:`APPROVAL_READY`; the use case enforces that.
    """
    argv = " ".join(approval_command(facts, journal=journal))
    return "\n".join(
        (
            "## Owner Approval — sublane quarantine (generation-bound)",
            "",
            f"- issue: {facts.issue}",
            f"- lane: `{facts.lane}`",
            f"- role: `{facts.role}`",
            f"- workspace_id: `{facts.workspace_id}`",
            f"- assigned_name: `{facts.assigned_name}`",
            f"- locator: `{facts.locator}`",
            f"- agent_revision: {facts.agent_revision}",
            f"- attested_at: `{facts.attested_at}`",
            f"- action_generation: `{facts.action_generation}`",
            f"- observed_at: `{facts.observed_at}`",
            "- approved_action: replace this exact managed receiver "
            "(no generic Enter / C-u / body typing)",
            "",
            "承認は上記 exact generation に束縛される。receiver の revision / attested "
            "generation / locator が変化した場合、`--execute` は再照合で fail-closed になる。",
            "",
            "```",
            argv,
            "```",
        )
    )


__all__ = (
    "APPROVAL_READY",
    "APPROVAL_WORKSPACE_UNRESOLVED",
    "APPROVAL_INVENTORY_UNREADABLE",
    "APPROVAL_COMPOSER_UNREADABLE",
    "APPROVAL_RECEIVER_ABSENT",
    "APPROVAL_DUPLICATE_RECEIVER",
    "APPROVAL_REVISION_UNREADABLE",
    "APPROVAL_ATTESTATION_UNREADABLE",
    "APPROVAL_KNOWN_MARKER_REQUIRES_Q_ENTER",
    "APPROVAL_NOT_QUARANTINE_CANDIDATE",
    "APPROVAL_REASONS",
    "APPROVAL_JOURNAL_PLACEHOLDER",
    "ApprovalFacts",
    "approval_command",
    "decide_approval_readiness",
    "render_approval_template",
)
