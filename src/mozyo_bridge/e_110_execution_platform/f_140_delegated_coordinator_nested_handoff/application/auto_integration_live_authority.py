"""The live :class:`DurableAuthorityReader` for the #13686 actuator (Redmine #14825, item 1).

#13686 declared the port and left the implementation to this issue, so until now every durable
fact an integration is gated on was establishable only by injecting a fake — which is to say the
success path existed only in tests. This is the production reader.

**Every read is fresh, and every read is at action time.** There is no cache and no snapshot
field: each method calls its ports again, because the actuator re-measures before every step
precisely so that a gate raised by somebody else between two steps is seen. A port that cannot
answer returns ``None`` and the corresponding field keeps its unsatisfied default — an unreadable
world blocks rather than admits.

**The caller's claims are not inputs.** The action record supplies identity and nothing else, and
even that identity is checked before anything is read: a record naming an issue or a lane
generation other than the ones this reader was CONSTRUCTED for establishes nothing at all. The
lane envelope every piece of durable evidence is required to carry is likewise this reader's own
:class:`~...domain.auto_integration_authority.LaneScope`, never the record's.

Where each fact comes from, and why that source is the authority for it:

``review_generation_admissible`` / ``reviewed_head``
    The issue's durable journals, folded by the shared conjunct producer
    (:mod:`...domain.auto_integration_authority`). Marker-borne, correlated to the request it
    answers, written by the same-lane gateway.

``source_ci`` / :meth:`read_integration_ci`
    TWO things, conjoined (review j#96650 finding 5). A ``required_ci_green`` record about the
    EXACT commit, written by the coordinator — per-head rather than latest-wins, for the reason
    :mod:`...domain.auto_integration_authority` documents — AND the CI provider's CURRENT
    verdict for that commit, read now (:mod:`.auto_integration_ci_source`). The marker cannot
    express a head that went red after it was attested, so on its own it is a gate that opens on
    stale evidence; the provider cannot express an authority decision, so on its own it is not
    the attestation the preset requires. Neither alone; both.

``target_identity_known``
    The committed repository configuration, re-read at action time — a durable, reviewed,
    coordinator-owned record, and the same file the issuer policy already anchors authority to.
    This is a DIFFERENT question from ``policy.integration_branch``: that value is what this
    actuator instance was constructed with, this one is what the repository currently declares,
    and an actuator constructed against a branch the repository no longer declares fails closed
    on the disagreement instead of integrating on its own construction.

    **This is where the ``integration_branch: null`` runtime resolution was withdrawn** (item 6).
    The config record described ``None`` as deferring to "runtime resolution"; no resolver ever
    existed, and the honest choice between building one and retracting the declaration is not
    close. The value being resolved is the TARGET OF A PUSH, and #13686 spent its review history
    removing exactly this shape — a late-bound name standing in for an identity — from every
    mutation it performs. An unset branch is therefore not a deferral: it is an unconfigured
    target, and an unconfigured target integrates nothing.

``callbacks_drained``
    The workspace callback outbox's unresolved debt, which is the workspace's existing authority
    for that question (an unreadable outbox is not a drained one).

``owner_gates_resolved``
    The issue's own canonical gate fold (:func:`~...domain.glance_journal_grammar.fold_issue_gate_facts`)
    — the same projection the workflow glance reads. The actuator does not get a second opinion
    about whether an issue is stopped: a recorded ``blocked`` gate, or a review round the record
    shows unresolved, is the durable statement that something is owed, and neither is a fact this
    module re-derives from prose of its own.

``issue_closed``
    The provider's own issue status. A ``close`` gate journal says the lane BELIEVES it is closed;
    the tracker says whether it is.

``authorizing_action_key``
    The actuator's own durable ledger — which integration action actually ran to completion for
    this issue, generation and source head. See :class:`CleanupAuthority` for why this may not
    come from the record being authorized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    CiVerdict,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ports import (  # noqa: E501
    CleanupAuthority,
    IntegrationAuthority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_authority import (  # noqa: E501
    CiRecord,
    LaneScope,
    ci_record_for_head,
    fold_durable_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    IntegrationActionRecord,
    IntegrationCiEvidence,
    normalized_branch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
    GateFacts,
    fold_issue_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    EvidenceJournal,
    as_pairs,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (  # noqa: E501
    CleanupActionRecord,
)

#: Reads one issue's durable journals with their writers resolved. ``None`` means the source could
#: not be read — never an empty page, which would read as "the issue records nothing".
JournalReader = Callable[[str], Optional[Sequence[EvidenceJournal]]]
#: The tracker's own answer to "is this issue closed". ``None`` when it could not be asked.
IssueClosedReader = Callable[[str], Optional[bool]]
#: The workspace's unresolved callback debt. ``None`` when the outbox could not be read.
CallbackDebtReader = Callable[[], Optional[int]]
#: The integration branches the repository currently declares, re-read at action time.
IntegrationBranchReader = Callable[[], Tuple[str, ...]]
#: The CI provider's CURRENT verdict for an exact commit. Conjoined with the durable marker,
#: because the marker cannot express a head that went red after it was attested (j#96650
#: finding 5).
CiVerdictReader = Callable[..., "CiVerdict"]
#: Which integration action the durable ledger says completed for this lane and source head,
#: given the head the COORDINATOR's record says landed. Both sides are required: the ledger
#: names the action, the tracker corroborates the commit (review j#96650 finding 1).
AuthorizingActionReader = Callable[[CleanupActionRecord, str], str]


@dataclass(frozen=True)
class LiveDurableAuthorityReader:
    """Answers the actuator's durable questions from the source of truth, fresh, per call."""

    scope: LaneScope
    lane_issue: str
    journals_fn: JournalReader
    integration_branches_fn: IntegrationBranchReader
    callback_debt_fn: CallbackDebtReader
    issue_closed_fn: IssueClosedReader
    authorizing_action_fn: AuthorizingActionReader
    #: The lane branch whose source-CI run the coordinator attested.  Integration CI is instead
    #: bound to ``record.target_ref``; a fast-forward may have the same SHA on both branches, but
    #: the issue-branch quick lane and target-branch integration batch are not the same run.
    source_branch: str = ""
    #: ``None`` means no action-time CI authority is wired, and CI evidence then establishes
    #: nothing — the same fail-closed direction as every other unwired port here. It is NOT
    #: optional in production; the composition root binds it.
    ci_verdict_fn: Optional[CiVerdictReader] = None

    # -- integration -------------------------------------------------------

    def read_integration_authority(
        self, *, record: IntegrationActionRecord
    ) -> IntegrationAuthority:
        """Every durable gate an integration passes, or the unsatisfied defaults."""
        if not self._record_is_ours(
            issue=record.issue, lane_generation=record.lane_generation
        ):
            return IntegrationAuthority()
        journals = self.journals_fn(record.issue)
        if journals is None:
            return IntegrationAuthority()

        facts = fold_durable_authority(journals, scope=self.scope)
        return IntegrationAuthority(
            review_generation_admissible=facts.review.admissible,
            reviewed_head=facts.review.head,
            target_identity_known=self._target_is_declared(record.target_ref),
            callbacks_drained=self._callbacks_drained(),
            owner_gates_resolved=self._owner_gates_resolved(journals),
            source_ci=self._ci_evidence(
                journals,
                head=record.source_head,
                branch=normalized_branch(self.source_branch),
                bind_attested_run=True,
            ),
        )

    def read_integration_ci(
        self, *, record: IntegrationActionRecord, integration_head: str
    ) -> Optional[IntegrationCiEvidence]:
        """The green required-CI record about the commit the push landed, or ``None``.

        ``integration_head`` is the head the LEDGER recorded landing, so this asks about what
        actually landed rather than about what was offered. A head this reader is handed but no
        record names is ``None``: the asynchronous gate has not settled, which is the state the
        actuator records ``pending`` for and re-runs on.
        """
        if not self._record_is_ours(
            issue=record.issue, lane_generation=record.lane_generation
        ):
            return None
        journals = self.journals_fn(record.issue)
        if journals is None:
            return None
        return self._ci_evidence(
            journals,
            head=integration_head,
            branch=normalized_branch(record.target_ref),
            bind_attested_run=False,
        )

    def required_ci_workflow(self, *, record: IntegrationActionRecord) -> str:
        """The durable source-CI marker's workflow, without treating it as live-green.

        This is recovery metadata, not authority: the normal reads still conjoin the marker with
        the provider's current verdict.  A supervisor uses this narrow projection only to recover
        the required workflow after a process dies between the accepted push receipt and the
        ``awaiting_ci`` registry transition.
        """
        if not self._record_is_ours(
            issue=record.issue, lane_generation=record.lane_generation
        ):
            return ""
        journals = self.journals_fn(record.issue)
        if journals is None:
            return ""
        found = ci_record_for_head(journals, head=record.source_head, scope=self.scope)
        return found.workflow if isinstance(found, CiRecord) else ""

    # -- cleanup -----------------------------------------------------------

    def read_cleanup_authority(
        self, *, record: CleanupActionRecord
    ) -> CleanupAuthority:
        """Every durable gate a post-close process release passes, or the unsatisfied defaults."""
        if not self._record_is_ours(
            issue=record.issue, lane_generation=record.lane_generation
        ):
            return CleanupAuthority()
        journals = self.journals_fn(record.issue)
        if journals is None:
            return CleanupAuthority()

        facts = fold_durable_authority(journals, scope=self.scope)
        integration = facts.integration
        # The integration record has to be about the head this cleanup was authorized for. A
        # confirmed integration of some OTHER commit says nothing about this lane's work being
        # on the target, and it is the head the cleanup record names that the authorization was
        # formed around.
        confirmed = bool(
            integration.confirmed
            and integration.source_head
            and integration.source_head == record.recorded_source_head
        )
        ci = (
            self._ci_evidence(
                journals,
                head=integration.integration_head,
                branch=normalized_branch(integration.integration_branch),
                bind_attested_run=False,
            )
            if confirmed and integration.integration_head
            else None
        )
        # The commit the COORDINATOR says landed — a merge names its integration head, a
        # fast-forward names the source head. The ledger's push receipt must agree with it, so
        # a forged receipt alone authorizes nothing.
        proof_head = (
            (integration.integration_head or integration.source_head) if confirmed else ""
        )
        return CleanupAuthority(
            issue_closed=self.issue_closed_fn(record.issue) is True,
            integration_confirmed=confirmed,
            # Settled AND green. The marker vocabulary can only render a success, so the record's
            # presence for the exact landed commit is the green; its absence is "not settled",
            # which is the same refusal and never a pass.
            integration_ci_settled_green=ci is not None,
            callbacks_drained=self._callbacks_drained(),
            owner_gates_resolved=self._owner_gates_resolved(journals),
            authorizing_action_key=self.authorizing_action_fn(record, proof_head),
        )

    # -- shared reads ------------------------------------------------------

    def _record_is_ours(self, *, issue: str, lane_generation: int) -> bool:
        """Whether the record names the exact issue and generation this reader is bound to.

        Checked before a journal is fetched. The reader is constructed for one lane at one
        generation; answering questions about a different action would mean reading one issue's
        durable record and returning it as another's, and the actuator has no second check that
        would catch it (the action key does not carry the lane's identity apart from these).
        """
        return (
            bool(self.lane_issue)
            and str(issue) == str(self.lane_issue)
            and isinstance(lane_generation, int)
            and not isinstance(lane_generation, bool)
            and lane_generation == self.scope.lane_generation
            and self.scope.is_complete
        )

    def _target_is_declared(self, target_ref: str) -> bool:
        """Whether the repository currently declares ``target_ref`` an integration branch.

        Compared on the normalized bare branch name, so ``refs/heads/main`` and ``main`` are one
        target rather than two. An empty declaration matches nothing — see the module docstring
        for why an unset branch is an unconfigured target and not a deferral.
        """
        declared = {
            normalized_branch(branch)
            for branch in (self.integration_branches_fn() or ())
            if normalized_branch(branch)
        }
        wanted = normalized_branch(target_ref)
        return bool(wanted) and wanted in declared

    def _callbacks_drained(self) -> bool:
        debt = self.callback_debt_fn()
        return debt == 0

    def _owner_gates_resolved(self, journals: Sequence[EvidenceJournal]) -> bool:
        """Whether the issue's own gate record shows nothing owed.

        Read from the canonical fold rather than re-derived: a ``blocked`` gate and an unresolved
        review round are the durable ways this workspace says a lane is stopped, and the glance
        already reads them that way. A fold that yields nothing (no recognized gate journal at
        all) is not an issue with nothing owed — it is an issue this reader cannot characterize,
        so it fails closed.
        """
        facts: Optional[GateFacts] = fold_issue_gate_facts(as_pairs(journals))
        if facts is None:
            return False
        return not facts.blocker_recorded and not facts.review_round_unresolved

    def _ci_evidence(
        self,
        journals: Sequence[EvidenceJournal],
        *,
        head: str,
        branch: str,
        bind_attested_run: bool,
    ) -> Optional[IntegrationCiEvidence]:
        """The CI record about ``head`` as the actuator's evidence type, or ``None``.

        A typed gap becomes ``None`` here rather than a partially-filled evidence record: the
        actuator distinguishes "evidence incomplete" from "run not green" by the evidence's own
        ``completeness_errors``, and handing it a half-built record would put a gap of ours into
        that vocabulary.
        """
        found = ci_record_for_head(journals, head=head, scope=self.scope)
        if not isinstance(found, CiRecord):
            return None
        # The attestation is necessary and NOT sufficient (j#96650 finding 5). The marker says
        # the coordinator recorded a required check green; it cannot say the commit went red
        # afterwards, because the producer renders no failure. So the provider is asked about
        # this exact commit now, and anything short of a current success withdraws the evidence.
        if self.ci_verdict_fn is None:
            return None
        verdict = self.ci_verdict_fn(
            head,
            workflow=found.workflow,
            attested_run=found.run if bind_attested_run else "",
            branch=branch,
        )
        if getattr(verdict, "blocks", True):
            return None
        # Source CI is the exact coordinator-attested run. Integration CI uses that marker to
        # authorize the required WORKFLOW, then requires the provider's successful run on the
        # TARGET branch. This is what lets a fast-forward distinguish issue-branch quick CI from
        # the same SHA's post-push integration batch without inventing a second conflicting
        # required_ci_green marker for one head.
        run = found.run if bind_attested_run else str(getattr(verdict, "run", "") or "")
        conclusion = (
            found.conclusion
            if bind_attested_run
            else str(getattr(verdict, "conclusion", "") or "")
        )
        if not bind_attested_run and (
            not run
            or str(getattr(verdict, "commit", "") or "") != found.head
            or str(getattr(verdict, "branch", "") or "") != branch
            or str(getattr(verdict, "workflow", "") or "") != found.workflow
        ):
            return None
        return IntegrationCiEvidence(
            integration_head=found.head,
            workflow=found.workflow,
            run=run,
            conclusion=conclusion,
        )


__all__ = (
    "AuthorizingActionReader",
    "CallbackDebtReader",
    "IntegrationBranchReader",
    "IssueClosedReader",
    "JournalReader",
    "LiveDurableAuthorityReader",
)
