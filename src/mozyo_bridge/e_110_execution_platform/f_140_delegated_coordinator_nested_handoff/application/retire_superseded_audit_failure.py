"""The SUPERSEDED AUDIT FAILURE terminal-retire route (Redmine #15166).

A no-change verification lane whose round-1 verdict was recorded by an INDEPENDENT AUDIT journal
rather than by a formal ``## Gate: review``, whose acceptance was then reached by a SUCCESSOR issue
that acknowledges the supersession, and which is closed, clean and carries zero commits. It has no
review generation at all, so the ordinary fence can only ever refuse it — and the two escapes from
that (a false ``--latest-generation-admissible`` assert, or borrowing the successor's approval) are
exactly what the reproduction #15164 refused (j#101825, ``stale_review_generation``, zero mutation).

Its own module for the reason :mod:`.retire_superseded_failure` is its own module: the host
:mod:`.retire_admissibility` already sits inside the oversized-module gate's reach, and a route's
home is the route. :mod:`.retire_admissibility` re-exports the resolver, so the CLI import site and
the existing route tests are unchanged.

Boundary: reads the target and successor issues LIVE over the credential-gated Redmine read and
runs read-only probes inside the lane checkout; every decision is delegated to the pure domain
fence. Every failure mode — unconfigured credentials, an unreadable Redmine, an unresolvable
target, an unmeasurable repository, a malformed record — resolves to a typed refusal (fail-closed).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_superseded_failure import (  # noqa: E501
    committed_integration_branch,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only; a runtime import would be a cycle
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        GenerationAdmissibility,
        RetireEvidenceTarget,
    )


#: The audit-failure route could not read its own inputs (unreadable Redmine, no recognized gate on
#: the target issue). Distinct from a refusal the domain reasoned about (Redmine #15166).
REASON_AUDIT_ROUTE_UNREADABLE = "superseded_audit_failure_route_evidence_unreadable"
#: The retire target's lane identity could not be measured from durable state, so the declaration
#: had nothing independent to be correlated against.
REASON_AUDIT_TARGET_UNRESOLVED = "superseded_audit_failure_retire_target_unresolved"


def _read_live_issue_journals(issue: str) -> "list[tuple[str, str]]":
    """One issue's full durable history as ``(journal_id, notes)`` pairs, read LIVE (IO).

    Returns ``[]`` on any failure (unconfigured credentials, an unreadable Redmine, a provider
    error). An empty history is never "a record that says nothing is owed": the caller treats it as
    unreadable evidence, not as evidence of absence — which matters doubly here, because two of
    this route's conjuncts are NEGATIVE claims over the whole record (no review round anywhere, no
    change declared anywhere) and a subset satisfies a negative claim by omission alone.
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        entries = LiveRedmineJournalSource.from_environment().read_entries(str(issue))
    except Exception:  # noqa: BLE001 - unreadable live state -> no evidence, never a crash
        return []
    return [
        (str(getattr(e, "journal_id", "")), str(getattr(e, "notes", "") or ""))
        for e in entries or ()
    ]


def _read_live_issue_closed(issue: str) -> Optional[bool]:
    """Whether the TRACKER currently reports ``issue`` closed, from one fresh GET (IO).

    Review j#101880 finding 2. The journal fold answers "did this issue record a Close gate"; this
    answers "is it closed right now". A status-only reopen changes ``status.is_closed`` and adds no
    ``## Gate:`` note, so the two axes cannot substitute for one another — and the successor side
    had no current-status input at all, so a re-opened successor still counted as complete on the
    strength of its past Close gate.

    Same discipline as :func:`...sublane_hibernated_unbound_live_zero_retire._fresh_closed_decision_snapshot`
    (#14716): one read-only issue-detail GET per call — never a cached or reused payload, so this is
    a real action-time observation — and the response must IDENTIFY the exact issue that was asked
    for before its status is believed. ``None`` on any failure (unconfigured credentials, an
    unreadable Redmine, a response that is not an issue-detail object, a response about a different
    issue, an absent or non-mapping status): the pure fence treats that as its own refusal rather
    than as "not closed", because the two send an operator to different places.
    """
    from typing import Mapping

    wanted = str(issue or "").strip()
    if not wanted:
        return None
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )

        source = LiveRedmineJournalSource.from_environment()
        payload = source.transport(
            base_url=source.base_url, api_key=source.api_key, issue_id=wanted, since=None
        )
    except Exception:  # noqa: BLE001 - unreadable live state -> unmeasured, never a crash
        return None
    if not isinstance(payload, Mapping):
        return None
    record = payload.get("issue")
    if not isinstance(record, Mapping) or str(record.get("id", "")).strip() != wanted:
        # A response that does not identify the exact issue asked for cannot testify about it.
        return None
    status = record.get("status")
    if not isinstance(status, Mapping):
        return None
    return status.get("is_closed") is True


def _measure_audit_record(journals, audit_journal: str):
    """What the issue's OWN history says about the journal the declaration names (pure).

    ``present`` is whether that journal id appears in the history at all. ``declares_lifecycle_gate``
    asks the SAME grammar every other consumer asks — :func:`...glance_journal_grammar.fold_issue_gate_facts`
    over that one journal — rather than a second reader, because a route-local notion of "is this a
    gate" would eventually disagree with the fold that decides the issue's lifecycle, and this
    conjunct exists precisely to keep the audit record and a Review Gate from being confused.

    Both fields default to the fail-closed value when the journal is absent: an unread journal has
    not established that it is not a gate.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
        fold_issue_gate_facts,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
        GATE_NONE,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_audit_failure_terminal import (  # noqa: E501
        AuditRecordEvidence,
    )

    wanted = str(audit_journal or "").strip()
    if not wanted:
        return AuditRecordEvidence()
    matches = [notes for jid, notes in journals if str(jid).strip() == wanted]
    if len(matches) != 1:
        # Zero: the declaration points at a journal this history does not carry. Two: the history
        # is not addressable by journal id, so "which record is the audit" has no single answer.
        return AuditRecordEvidence()
    facts = fold_issue_gate_facts([(wanted, matches[0])])
    return AuditRecordEvidence(
        present=True,
        declares_lifecycle_gate=facts is not None and facts.latest_gate != GATE_NONE,
    )


def resolve_superseded_audit_failure_admissible(
    args: argparse.Namespace,
    *,
    target: Optional[RetireEvidenceTarget] = None,
    repo_root: Optional[Path] = None,
) -> "GenerationAdmissibility":
    """Re-verify an AUDIT-FAILURE lane's terminal-retire admissibility (Redmine #15166).

    #15164's round-1 verdict is ``## Independent audit — round 1`` j#101792, which records in as
    many words that no formal ``## Gate: review`` was created because the ``review_request`` was
    missing; the acceptance it failed to reach was obtained by #15165, whose own ``## Gate: review``
    j#101810 concluded ``approved``; both issues are task_closed and closed; the lane never
    committed. The retire refuses anyway with ``stale_review_generation`` (j#101825) — because the
    fence reads a review generation and this lane has none at all.

    The pure fence
    (:func:`...superseded_audit_failure_terminal.evaluate_superseded_audit_failure_admissible`)
    states the whole contract; this function only MEASURES its inputs, and every one of them comes
    from somewhere the declaration does not control:

    - the target issue's full history, read LIVE (never a caller-supplied file — two conjuncts are
      negative claims over the WHOLE record, and a caller-supplied subset satisfies those by
      omission alone, the #14695 j#93406 rule), folded with the same glance grammar every other
      consumer uses, so "no review round" and "the record declares no change" mean what they mean
      everywhere;
    - the named audit journal's own shape, from that same history and that same fold;
    - the SUCCESSOR issue's full history, read live and folded the same way. This is the half a
      source-side declaration cannot write for itself without also writing into another issue's
      record, and it is why the successor evidence is a correlation rather than a claim;
    - the lane identity, from the retire target's own lifecycle row — never argv, because an
      identity the caller chooses fences nothing (#14539 j#91797 F2) — and the integration branch
      from the COMMITTED config for the same reason
      (:func:`...retire_superseded_failure.committed_integration_branch`);
    - the live repository facts, from the read-only probes :func:`measure_lane_change` runs entirely
      inside the lane checkout.

    Fail-closed on everything: unconfigured credentials, an unreadable Redmine, an unresolvable
    target, an unmeasurable repository, a malformed marker, a review round that exists after all, a
    re-opened lane, an absent or gate-shaped audit record, a change-bearing record, an
    unacknowledged or incomplete successor, a moved head, a lane still carrying commits.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        GenerationAdmissibility,
        measure_lane_change,
    )

    if not bool(getattr(args, "superseded_audit_failure_terminal", False)):
        return GenerationAdmissibility(False, "")
    if target is None or repo_root is None:
        return GenerationAdmissibility(False, REASON_AUDIT_TARGET_UNRESOLVED)
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
            fold_issue_gate_facts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            GATE_CLOSE,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_audit_failure_terminal import (  # noqa: E501
            TrackerIssueStatus,
            evaluate_superseded_audit_failure_admissible,
            fold_audit_supersession_acknowledgement,
            fold_superseded_audit_failure,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_terminal import (  # noqa: E501
            SuccessorEvidence,
        )

        issue = str(getattr(args, "issue", "") or "").strip()
        if not issue:
            return GenerationAdmissibility(False, REASON_AUDIT_ROUTE_UNREADABLE)

        journals = _read_live_issue_journals(issue)
        gate_facts = fold_issue_gate_facts(journals) if journals else None
        if gate_facts is None:
            # An empty history is a read that produced nothing, and no recognized gate means no
            # Close evidence and no zero-change verdict — neither may be read as "nothing is owed".
            return GenerationAdmissibility(False, REASON_AUDIT_ROUTE_UNREADABLE)

        declaration = fold_superseded_audit_failure(journals)

        # The successor's own record. Read only once the declaration named one: an unnamed or
        # unreadable successor is a refusal the pure fence diagnoses, not a second live read.
        successor_journals = (
            _read_live_issue_journals(declaration.successor_issue)
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

        outcome = evaluate_superseded_audit_failure_admissible(
            declaration,
            audit=_measure_audit_record(journals, declaration.audit_journal),
            acknowledgement=fold_audit_supersession_acknowledgement(successor_journals),
            successor=successor,
            # The head the successor's approved round actually examined, from the SAME grammar and
            # the SAME Marker Contract v2 correlation that decided its conclusion — the conjunct
            # this route rests on after review j#101880 finding 1.
            successor_review_head=(
                successor_facts.review_round_head if successor_facts else ""
            ),
            # The tracker's own current answer for BOTH issues, each from its own fresh read. The
            # successor's is not optional: it had no current-status input at all before finding 2.
            tracker=TrackerIssueStatus(
                source_closed=_read_live_issue_closed(issue),
                successor_closed=(
                    _read_live_issue_closed(declaration.successor_issue)
                    if declaration.successor_issue
                    else None
                ),
            ),
            review_round_journals=tuple(gate_facts.review_round_journals or ()),
            latest_gate_journal=gate_facts.latest_gate_journal,
            close_recorded=gate_facts.latest_gate == GATE_CLOSE,
            # The SAME zero-change verdict #14695 measures and the glance consumes, so the two
            # cannot disagree about what "this lane produced no repository change" means.
            zero_change_proven=bool(gate_facts.zero_change.proven),
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
        return GenerationAdmissibility(False, REASON_AUDIT_ROUTE_UNREADABLE)


__all__ = (
    "REASON_AUDIT_ROUTE_UNREADABLE",
    "REASON_AUDIT_TARGET_UNRESOLVED",
    "resolve_superseded_audit_failure_admissible",
)

# The two live reads this route performs are named in the module docstring's boundary paragraph:
# both issues' full journal histories, and both issues' CURRENT tracker status. Nothing else
# touches the network, and every failure of either read resolves to a typed refusal.
