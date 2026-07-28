"""Terminal-retire integration admissibility, resolved from durable observations.

The ``sublane retire`` integration decision is fenced on ``latest_generation_admissible``: a
lane may not retire on a STALE last-write-wins approval (#13518 review R2-F7 / R3-F2). This
module owns how that single boolean is RESOLVED for a CLI invocation, from two independent
kinds of durable evidence measured at action time, plus the operator's fallback assertion:

- :func:`_resolve_review_generation_admissible` — the review-generation fence (#13518): the
  LATEST generation is approved and carries no unresolved blocking finding;
- :func:`_resolve_review_exemption_admissible` — the review-EXEMPTION fence (#14539): a lane
  covered by a valid ``codex_direct_edit`` gate with ``follow_up_review: false`` has no review
  generation at all, so it is admitted on exemption + Close + complete integration instead of
  on a review that never happened.

Carved out of :mod:`.sublane_lifecycle_command` (Redmine #14539) when adding the second route
pushed that module past the oversized-module gate. This is a pure move plus the new route: the
CLI module re-exports :func:`_resolve_latest_generation_admissible`, so its import site and the
existing #13518 tests are unchanged.

Boundary: reads a caller-named JSON observation file and delegates every decision to the pure
domain fences. Every failure mode — absent flag, unreadable / malformed file, inadmissible
evidence — resolves to ``False`` (fail-closed).
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _resolve_review_exemption_admissible(args: argparse.Namespace) -> bool:
    """Re-verify a review-EXEMPT lane's terminal-retire admissibility at action time (#14539).

    ``--review-exemption-json`` supplies the issue's durable journals
    (``{issue, journals: [{journal_id, notes}]}``). They are folded with the SAME grammar the
    glance projection uses — no second reader — and admitted only when all three durable facts
    hold together: an in-force ``codex_direct_edit`` exemption, a recorded Close gate, and a
    COMPLETE integration disposition
    (:func:`...review_exemption.evaluate_exemption_integration_admissible`).

    This is what removes the false assert the issue names: an exempt lane has no review generation,
    so ``--latest-generation-admissible`` ("the latest generation is approved with no unresolved
    blocking finding") could only ever be asserted untruthfully for it.

    Two fences make the evidence actually belong to THIS retire (Redmine #14539 review j#90137):

    - **F2, issue correlation.** The observation MUST declare an ``issue`` and it must literal
      exact-match the retire's ``--issue``. Without this, durable evidence from a *different*
      issue — a closed, merged, exempt one — unlocked the ``stale_review_generation`` fence for
      any target. An absent / blank / mismatched issue on either side is fail-closed.
    - **F3, supersession.** Admissibility uses ``GateFacts.review_exempt`` — the supersession-aware
      fact the glance classifier consumes — not the bare gate state. A review round opened AFTER
      the exemption re-owes the review, and the retire must agree with the glance about that.

    A third fence makes the three durable facts belong to the same WORK, not merely to the same
    issue (review j#91577 finding 2): the Close gate's commit must equal the commit the
    exemption's coverage was proven for, and the integration disposition must not predate the
    declaration of that commit's change scope. Conjoining three booleans admitted a lane whose
    Close and merge both belonged to an earlier commit while the current one was never integrated.

    **The integration half of that fence reads the STRICT evidence** (review j#91696 findings 2
    and 3). The lenient :func:`fold_integration_disposition` is a display projection: it resolves
    a journal declaring two different dispositions by line order, and it cannot see which commit
    the disposition is about. This route therefore (a) refuses any record carrying a conflicting
    disposition declaration, and (b) requires lane-enveloped evidence from
    :mod:`...domain.hibernate_evidence_integration` whose reviewed ``head`` is the covered commit.
    A legacy, lane-unbound ``## Integration disposition`` note remains perfectly valid for the
    glance and is simply not sufficient to auto-admit a terminal retire.

    Every other failure mode — unreadable file, malformed journals, invalid gate, unproven path
    coverage, ``follow_up_review: true``, missing Close, incomplete integration — is likewise
    fail-closed to ``False``.
    """
    path = (getattr(args, "review_exemption_json", None) or "").strip()
    if not path:
        return False
    try:
        import json

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (  # noqa: E501
            fold_integration_disposition,
            has_conflicting_disposition_declaration,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_integration import (  # noqa: E501
            IntegrationEvidenceError,
            resolve_integration_evidence,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            MARKER_CHANNEL_WORKFLOW_EVENT,
            marker_fields_in_note,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
            fold_issue_gate_facts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (  # noqa: E501
            evaluate_exemption_integration_admissible,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            GATE_CLOSE,
        )

        raw = json.loads(Path(path).read_text(encoding="utf-8"))

        # F2: the observation must be ABOUT the issue being retired. Both sides must be present
        # and equal as literals — a blank on either side correlates to nothing.
        target_issue = str(getattr(args, "issue", "") or "").strip()
        observed_issue = str(raw.get("issue", "") or "").strip()
        if not target_issue or not observed_issue or target_issue != observed_issue:
            return False

        journals = [
            (str(entry.get("journal_id", "")), str(entry.get("notes", "")))
            for entry in (raw.get("journals") or [])
        ]
        gate_facts = fold_issue_gate_facts(journals)
        if gate_facts is None:
            # No recognized gate at all -> no Close evidence, no exemption authority.
            return False
        # j#91696 F3: the lenient fold resolves a journal that declares two DIFFERENT dispositions
        # by line order, so the same durable record admitted or refused depending on which was
        # written first. An authority consumer asks the strict question before trusting the fold.
        if has_conflicting_disposition_declaration(journals):
            return False

        integration = fold_integration_disposition(journals)

        # j#91696 F2 / j#91747 F2: the STRICT integration evidence (#14219 T2b), read from the
        # CURRENT declaration only. ``fold_integration_disposition`` already decided which journal
        # is current, and `hibernate_basis_producer._latest_disposition_declaration` reads exactly
        # that journal's markers for the same reason — one authority selection, not two. Resolving
        # every marker in the issue instead let an OLD enveloped merge supply the source head while
        # a NEWER heading-only legacy note supplied the freshness journal id, so the ordering fence
        # was satisfied by a journal that carried no evidence at all. A current declaration with no
        # marker (heading-only / legacy) therefore yields no strict evidence — for THIS journal,
        # never a fallback to a stale one.
        current_notes = next(
            (notes for jid, notes in journals if jid.strip() == integration.journal), ""
        )
        marker_fields = [
            fields
            for channel, fields in marker_fields_in_note(current_notes or "")
            if channel == MARKER_CHANNEL_WORKFLOW_EVENT
        ]
        evidence = resolve_integration_evidence(marker_fields)
        if isinstance(evidence, IntegrationEvidenceError):
            source_head = ""
        else:
            # j#91747 F3: the envelope is the reason this evidence is trustworthy, so it must
            # actually name THIS retire's lane. Requiring a lane-enveloped marker and then dropping
            # the envelope is not an identity fence — a marker bound to a foreign workspace / lane /
            # generation, on an unrelated integration branch, admitted on its source head alone.
            #
            # The expected identity comes from the retire's OWN arguments, never from the
            # observation file: a value the same untrusted document supplies could not fence it.
            # ``--lane-label`` is deliberately NOT reused for the envelope's ``lane`` — the lane
            # registry distinguishes ``lane_id`` from ``lane_label`` and the envelope carries the
            # former, so equating them would be a category error rather than a fence. Each expected
            # value must be present; an absent one fails closed rather than skipping its check.
            expected = (
                (str(getattr(args, "evidence_workspace", "") or "").strip(),
                 evidence.envelope.workspace),
                (str(getattr(args, "evidence_lane", "") or "").strip(), evidence.envelope.lane),
                (str(getattr(args, "evidence_lane_generation", "") or "").strip(),
                 str(evidence.envelope.lane_generation)),
                (str(getattr(args, "integration_branch", "") or "").strip(),
                 evidence.integration_branch),
            )
            bound = all(want and want == got for want, got in expected)
            source_head = evidence.source_head if bound else ""

        return bool(
            evaluate_exemption_integration_admissible(
                gate_facts.review_exemption,
                # F3: the SAME supersession-aware fact the glance classifier consumes.
                currently_in_force=gate_facts.review_exempt,
                close_recorded=gate_facts.latest_gate == GATE_CLOSE,
                integration_complete=integration.complete,
                # j#91577 F2: the identity the three facts must share. ``latest_gate_commit`` is
                # the Close journal's own commit precisely because ``close_recorded`` above is
                # "the LATEST gate is Close", so the two read the same journal.
                close_commit=gate_facts.latest_gate_commit,
                integration_journal=integration.journal,
                integration_source_head=source_head,
            ).admissible
        )
    except Exception:  # noqa: BLE001 - unreadable / malformed durable observation -> fail closed
        return False


def _resolve_latest_generation_admissible(args: argparse.Namespace) -> bool:
    """Resolve the latest-generation integration admissibility for a retire (#13518 R3-F2).

    Priority: (1) a coordinator-supplied durable review observation (``--review-generation-json``)
    is MEASURED at action-time through the pure review-generation fence
    (:func:`...review_generation.evaluate_integration_admissible`) — an unreadable / malformed file
    or an inadmissible latest generation fails closed. (2) A durable review EXEMPTION observation
    (``--review-exemption-json``) is MEASURED the same way (#14539). (3) Otherwise the operator's
    durable-record assertion (``--latest-generation-admissible``). (4) Absent all, ``False``
    (fail-closed) — the actual integration decision never default-admits a stale last-write-wins
    approval.

    When EITHER measured input is supplied, the measurement decides and the operator assertion is
    NOT consulted: a supplied-but-failing measurement must never fall back to a hand assert. The
    two measured routes are independent evidence for the same fence (a lane either passed a review
    generation or was exempt from one), so either one admitting is sufficient.
    """
    exemption_path = (getattr(args, "review_exemption_json", None) or "").strip()
    path = (getattr(args, "review_generation_json", None) or "").strip()
    if path or exemption_path:
        return _resolve_review_generation_admissible(args) or (
            _resolve_review_exemption_admissible(args)
        )
    return bool(getattr(args, "latest_generation_admissible", False))


def _resolve_review_generation_admissible(args: argparse.Namespace) -> bool:
    """Measure latest-generation admissibility from ``--review-generation-json`` (#13518 R3-F2).

    Returns ``False`` when the flag is absent, when the file is unreadable / malformed, or when
    the fence finds the latest generation inadmissible.
    """
    path = (getattr(args, "review_generation_json", None) or "").strip()
    if path:
        try:
            import json

            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
                ReviewDecision,
                ReviewGeneration,
                evaluate_integration_admissible,
            )

            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            gen = ReviewGeneration(
                issue=str(raw.get("issue", "")),
                review_request_journal=str(raw.get("review_request_journal", "")),
                target_head=str(raw.get("target_head", "")),
            )
            decisions = [
                ReviewDecision(
                    generation=ReviewGeneration(
                        issue=str(d.get("issue", raw.get("issue", ""))),
                        review_request_journal=str(
                            d.get("review_request_journal", raw.get("review_request_journal", ""))
                        ),
                        target_head=str(d.get("target_head", raw.get("target_head", ""))),
                    ),
                    kind=str(d.get("kind", "")),
                    seq=int(d.get("seq", 0)),
                    blocking=bool(d.get("blocking", False)),
                    disposition=str(d.get("disposition", "unresolved")),
                    journal_id=str(d.get("journal_id", "")),
                )
                for d in (raw.get("decisions") or [])
            ]
            return bool(evaluate_integration_admissible(gen, decisions).admissible)
        except Exception:  # noqa: BLE001 - unreadable / malformed durable observation -> fail closed
            return False
    return False


__all__ = (
    "_resolve_latest_generation_admissible",
    "_resolve_review_exemption_admissible",
    "_resolve_review_generation_admissible",
)
