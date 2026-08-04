"""The SUPERSEDED FAILURE terminal-retire route (Redmine #14755).

A lane whose latest review generation concluded ``changes_requested``, whose findings were all
received and accepted, and whose acceptance target was obtained by a SUCCESSOR issue that
acknowledges the supersession. Its round can never be approved, so the ordinary review-generation
fence can only ever refuse it — and the two escapes from that (a false
``--latest-generation-admissible`` assert, or reading the successor's approval as this lane's) are
exactly what the reproduction #14577 refused, three preflights running.

Its own module for the reason :mod:`.retire_admissibility` is its own module: adding a route pushed
the host past the oversized-module gate (#14539 did the same carve one level up). This is a move
plus the #14971 authority wiring; :mod:`.retire_admissibility` re-exports every name, so the CLI
import site and the existing route tests are unchanged.

Boundary: reads the target and successor issues LIVE over the credential-gated Redmine read and
runs read-only probes inside the lane checkout; every decision is delegated to the pure domain
fences. Every failure mode — unconfigured credentials, an unreadable Redmine, an unresolvable
target, an unmeasurable repository, a malformed record — resolves to a typed refusal (fail-closed).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - annotations only; a runtime import would be a cycle
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        GenerationAdmissibility,
        RetireEvidenceTarget,
    )


#: The superseded-failure route could not read its own inputs (unreadable Redmine, no recognized
#: gate on the target issue). Distinct from a refusal the domain reasoned about (Redmine #14755).
REASON_SUPERSEDED_ROUTE_UNREADABLE = "superseded_failure_route_evidence_unreadable"
#: The retire target's lane identity could not be measured from durable state, so the
#: superseded-failure declaration had nothing independent to be correlated against.
REASON_SUPERSEDED_TARGET_UNRESOLVED = "superseded_failure_retire_target_unresolved"


def _read_live_issue_entries(issue: str) -> list:
    """One issue's full durable history as ENTRIES, read LIVE over the Redmine read (IO).

    Entries rather than ``(journal_id, notes)`` pairs because #14971's finding authority needs the
    issue identity each record carries — a manifest is cross-checked against the journal's OWN
    ``issue_id``, and the legacy attestation chain refuses a history whose records do not all
    belong to one issue. A pair-shaped reader cannot supply that, and inferring the issue from the
    id we asked for would hand the check back the very value it exists to verify.

    Returns ``[]`` on any failure (unconfigured credentials, an unreadable Redmine, a provider
    error). An empty history is never "a record that says nothing is owed": each caller treats it
    as unreadable evidence, not as evidence of absence.
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        entries = LiveRedmineJournalSource.from_environment().read_entries(str(issue))
    except Exception:  # noqa: BLE001 - unreadable live state -> no evidence, never a crash
        return []
    return list(entries or ())


def _journal_pairs(entries: Sequence[object]) -> "list[tuple[str, str]]":
    """Project entries to the ``(journal_id, notes)`` shape the pure folds consume (pure)."""
    return [
        (str(getattr(e, "journal_id", "")), str(getattr(e, "notes", "") or ""))
        for e in entries or ()
    ]


def committed_integration_branch(repo_root: Path) -> str:
    """The integration branch the repository's COMMITTED config declares, or ``""`` (IO).

    An expectation the retire's invoker does not choose. Redmine #14755 needs one because the
    live "carries 0 commits over the integration branch" measurement is taken against whatever
    ``--integration-branch`` names: a caller free to name it can point it at the lane's own branch
    and make that measurement trivially true. Reading it from the committed
    ``sublane_integration.integration_branch`` block is the same principle
    :class:`RetireEvidenceTarget` applies to the lane identity (#14539 j#91797 F2) — an identity
    the caller supplies fences nothing.

    ``""`` on an absent / unreadable / unset value, which fails the route closed: a config that
    declares no integration branch supplies no expectation, and guessing one would be inventing
    the very thing this is here to avoid.
    """
    try:
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        config = load_repo_local_config(repo_root).sublane_integration
    except Exception:  # noqa: BLE001 - an unreadable config is a typed zero, not a crash
        return ""
    return str(getattr(config, "integration_branch", "") or "").strip()


def resolve_superseded_failure_admissible(
    args: argparse.Namespace,
    *,
    target: Optional[RetireEvidenceTarget] = None,
    repo_root: Optional[Path] = None,
) -> "GenerationAdmissibility":
    """Re-verify a SUPERSEDED FAILURE round's terminal-retire admissibility (Redmine #14755).

    #14577's latest same-lane Review concluded ``changes_requested``; both findings were verified
    and accepted; the acceptance it failed to reach was obtained by the successor #14697, whose
    own Review was approved; the issue was task_closed as a superseded failure and its head is
    already an ancestor of the integration branch with zero commits of its own. The retire refused
    anyway, three times, with ``stale_review_generation`` (j#93759 / j#94006 / j#94319) — because
    the fence reads only a review generation and this lane's will never be approved.

    So the failure is terminalized AS a failure. The pure fence
    (:func:`...superseded_failure_terminal.evaluate_superseded_failure_admissible`) states the
    whole contract; this function only MEASURES its inputs, and every one of them comes from
    somewhere the declaration does not control:

    - the target issue's full history, read LIVE (never a caller-supplied file — see
      :func:`_read_live_issue_entries`), folded with the same glance grammar every other consumer
      uses, so "the newest round concluded ``changes_requested``" means what it means everywhere;
    - the failed round's FINDING SET, from #14971's canonical authority
      (:func:`...review_finding_legacy_authority.resolve_review_finding_authority`) — an in-journal
      manifest the review producer emits atomically with the review prose, or, for a review that
      predates that contract, an append-only attestation selected by a direct-owner ruling. This
      route consumes that authority; the route-local enumeration it carried before (#14755 review
      j#99065) let the record naming the set be the same record that wrote the findings;
    - the SUCCESSOR issue's full history, read live and folded the same way. This is the half a
      source-side declaration cannot write for itself without also writing into another issue's
      record, and it is why the successor evidence is a correlation rather than a claim;
    - the lane identity, from the retire target's own lifecycle row — never argv, because an
      identity the caller chooses fences nothing (#14539 j#91797 F2), and the integration branch
      from the COMMITTED config for the same reason (:func:`committed_integration_branch`);
    - the live repository facts, from the read-only probes :func:`measure_lane_change` runs
      entirely inside the lane checkout.

    Fail-closed on everything: unconfigured credentials, an unreadable Redmine, an unresolvable
    target, an unmeasurable repository, a malformed marker, a re-opened round, a disputed finding,
    an unacknowledged or incomplete successor, a moved head, a lane still carrying commits.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        GenerationAdmissibility,
        measure_lane_change,
    )

    if not bool(getattr(args, "superseded_failure_terminal", False)):
        return GenerationAdmissibility(False, "")
    if target is None or repo_root is None:
        return GenerationAdmissibility(False, REASON_SUPERSEDED_TARGET_UNRESOLVED)
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
            fold_issue_gate_facts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            GATE_CLOSE,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_legacy_authority import (  # noqa: E501
            resolve_review_finding_authority,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_legacy_issuer import (  # noqa: E501
            resolve_legacy_ruling_issuers,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
            fold_finding_verdicts,
            fold_successor_acknowledgement,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
            SuccessorEvidence,
            declaration_current,
            evaluate_superseded_failure_admissible,
            fold_superseded_failure,
        )

        issue = str(getattr(args, "issue", "") or "").strip()
        if not issue:
            return GenerationAdmissibility(False, REASON_SUPERSEDED_ROUTE_UNREADABLE)

        entries = _read_live_issue_entries(issue)
        journals = _journal_pairs(entries)
        gate_facts = fold_issue_gate_facts(journals) if journals else None
        if gate_facts is None:
            # An empty history is a read that produced nothing, and no recognized gate means no
            # Close evidence and no review round — neither may be read as "nothing is owed".
            return GenerationAdmissibility(False, REASON_SUPERSEDED_ROUTE_UNREADABLE)

        declaration = fold_superseded_failure(journals)
        rounds = list(gate_facts.review_round_journals or ())
        # The newest round's identity, from the SAME ids the ordering question uses, so the two
        # cannot disagree about which journals are rounds.
        latest_round_journal = str(max(rounds)) if rounds else ""

        # The successor's own record. Read only once the declaration named one: an unnamed or
        # unreadable successor is a refusal the pure fence diagnoses, not a second live read.
        successor_journals = (
            _journal_pairs(_read_live_issue_entries(declaration.successor_issue))
            if declaration.successor_issue
            else []
        )
        successor_facts = (
            fold_issue_gate_facts(successor_journals) if successor_journals else None
        )
        successor_rounds = (
            list(successor_facts.review_round_journals or ()) if successor_facts else []
        )
        successor = SuccessorEvidence(
            review_journal=str(max(successor_rounds)) if successor_rounds else "",
            review_gate=successor_facts.review_round_gate if successor_facts else "",
            review_conclusion=(
                successor_facts.review_round_conclusion if successor_facts else ""
            ),
            close_recorded=bool(
                successor_facts is not None and successor_facts.latest_gate == GATE_CLOSE
            ),
        )

        measured = measure_lane_change(
            repo_root,
            branch=str(getattr(args, "branch", "") or ""),
            integration_branch=str(getattr(args, "integration_branch", "") or ""),
            worktree=str(getattr(args, "worktree", "") or ""),
        )

        # The finding set the verdicts must cover, from #14971's canonical authority rather than
        # from anything this route declares. Resolved over the ENTRIES, because that module reads
        # each record's own issue identity and needs the ``ruling_issuers`` port — the mapping that
        # decides which journals are eligible to carry a legacy direct-owner ruling — which is the
        # consumer's obligation to fill (:mod:`...review_finding_legacy_issuer` states what that
        # resolution does and does not establish).
        authority = resolve_review_finding_authority(
            entries,
            review_journal=declaration.review_journal,
            ruling_issuers=resolve_legacy_ruling_issuers(entries),
        )

        outcome = evaluate_superseded_failure_admissible(
            declaration,
            currently_current=declaration_current(declaration, rounds),
            verdicts=fold_finding_verdicts(
                journals,
                review_journal=declaration.review_journal,
                authority=authority,
            ),
            acknowledgement=fold_successor_acknowledgement(successor_journals),
            successor=successor,
            latest_round_journal=latest_round_journal,
            latest_round_gate=gate_facts.review_round_gate,
            latest_round_conclusion=gate_facts.review_round_conclusion,
            close_recorded=gate_facts.latest_gate == GATE_CLOSE,
            target_issue=issue,
            integration_branch=str(getattr(args, "integration_branch", "") or "").strip(),
            committed_integration_branch=committed_integration_branch(repo_root),
            measured_branch=str(getattr(args, "branch", "") or "").strip(),
            expected_workspace=target.workspace,
            expected_lane=target.lane,
            expected_lane_generation=target.lane_generation,
            live_head=measured.head,
            live_commits_ahead=measured.commits_ahead,
            worktree_clean=measured.worktree_clean,
            callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
        )
        return GenerationAdmissibility(
            admissible=bool(outcome.admissible), reason=str(outcome.reason or "")
        )
    except Exception:  # noqa: BLE001 - unreadable durable / live state -> fail closed
        return GenerationAdmissibility(False, REASON_SUPERSEDED_ROUTE_UNREADABLE)


__all__ = (
    "REASON_SUPERSEDED_ROUTE_UNREADABLE",
    "REASON_SUPERSEDED_TARGET_UNRESOLVED",
    "committed_integration_branch",
    "resolve_superseded_failure_admissible",
)
