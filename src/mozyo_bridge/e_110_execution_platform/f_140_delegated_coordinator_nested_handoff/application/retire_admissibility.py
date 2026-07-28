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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class RetireEvidenceTarget:
    """The lane identity a retire's integration evidence must name, measured from durable state.

    Redmine #14539 review j#91797 finding 2. The point of this type is WHERE its values come from:
    the lane lifecycle record of the lane being retired, never the caller's argv and never the
    observation file. An identity the caller supplies fences nothing — it can simply be pointed at
    whatever the evidence happens to say — and an identity the observation supplies certifies
    itself. Only a value read from durable state is an independent expectation.

    ``policy_pointer`` is the committed-config anchor the issuer resolution is basised on
    (:func:`...hibernate_issuer_policy.config_policy_pointer`); an empty one resolves every issuer
    to unknown, which is the fail-closed direction.

    Every field is required. A partially-resolved target is not a target: the caller returns
    ``None`` instead, and the fence refuses.
    """

    workspace: str
    lane: str
    lane_generation: int
    policy_pointer: str


def resolve_retire_evidence_target(
    args: argparse.Namespace, repo_root: Path
) -> Optional[RetireEvidenceTarget]:
    """Measure the retire target's lane identity from durable state, or ``None`` (fail-closed).

    Reads the lane lifecycle record for the lane ``--lane-label`` names, in the workspace the
    command's own repo root resolves to, and takes the generation from that row rather than from
    anything the caller said. Any gap — no workspace, no lane label, an unreadable store, no row,
    a non-positive generation, or an unresolvable committed-config pointer — yields ``None``, and
    the exemption route then refuses. The retire's other fences are unaffected: this returns an
    expectation for ONE optional route, it does not gate the command.
    """
    lane_label = str(getattr(args, "lane_label", "") or "").strip()
    if not lane_label:
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleKey,
            LaneLifecycleStore,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            herdr_workspace_segment,
        )

        workspace = str(herdr_workspace_segment(repo_root) or "").strip()
        if not workspace:
            return None
        record = LaneLifecycleStore().get(LaneLifecycleKey(workspace, lane_label))
    except Exception:  # noqa: BLE001 - an unresolvable target is a typed zero, not a crash
        return None
    if record is None:
        return None
    generation = getattr(record, "lane_generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        return None
    pointer = ""
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
            committed_config_policy_pointer,
        )

        pointer = str(committed_config_policy_pointer(repo_root) or "").strip()
    except Exception:  # noqa: BLE001 - an unresolvable basis is a typed zero, not a crash
        pointer = ""
    if not pointer:
        return None
    return RetireEvidenceTarget(
        workspace=workspace,
        lane=lane_label,
        lane_generation=generation,
        policy_pointer=pointer,
    )


def _resolve_review_exemption_admissible(
    args: argparse.Namespace, *, target: Optional[RetireEvidenceTarget] = None
) -> bool:
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
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (  # noqa: E501
            MARKER_GATE_INTEGRATION_DISPOSITION,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            check_issuer,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_integration import (  # noqa: E501
            IntegrationEvidenceError,
            resolve_integration_evidence,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            resolve_journal_issuer,
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
        integration = fold_integration_disposition(journals)

        # ONE current declaration, selected once, feeding EVERY question about the integration
        # (Redmine #14539 review j#91797 finding 3). R7-F2 asked for strict evidence, lenient
        # disposition, conflict and journal id to share a declaration; the conflict check was the
        # one left issue-global, so a superseded OLD malformed record blocked forever and a valid
        # current correction could not repair it — the opposite of latest-wins.
        current_notes = next(
            (notes for jid, notes in journals if jid.strip() == integration.journal), ""
        )
        current_declaration = (
            [(integration.journal, current_notes)] if integration.journal else []
        )

        # j#91696 F3 / j#91797 F4: the lenient fold resolves a journal that declares two DIFFERENT
        # dispositions by line order — across surfaces AND inside a single marker body. An
        # authority consumer asks the strict question before trusting the fold.
        if has_conflicting_disposition_declaration(current_declaration):
            return False

        # j#91696 F2 / j#91747 F2: the STRICT integration evidence (#14219 T2b), read from the
        # CURRENT declaration only. ``fold_integration_disposition`` already decided which journal
        # is current, and `hibernate_basis_producer._latest_disposition_declaration` reads exactly
        # that journal's markers for the same reason — one authority selection, not two. Resolving
        # every marker in the issue instead let an OLD enveloped merge supply the source head while
        # a NEWER heading-only legacy note supplied the freshness journal id, so the ordering fence
        # was satisfied by a journal that carried no evidence at all. A current declaration with no
        # marker (heading-only / legacy) therefore yields no strict evidence — for THIS journal,
        # never a fallback to a stale one.
        marker_fields = [
            fields
            for channel, fields in marker_fields_in_note(current_notes or "")
            if channel == MARKER_CHANNEL_WORKFLOW_EVENT
        ]
        evidence = resolve_integration_evidence(marker_fields)
        if isinstance(evidence, IntegrationEvidenceError):
            source_head = ""
        else:
            # j#91747 F3 / j#91797 F2: the envelope is the reason this evidence is trustworthy, so
            # it must name THE LANE BEING RETIRED — not merely some lane the caller also named.
            #
            # R9 compared the envelope against three dedicated argv flags. That is still a value
            # the CALLER chooses: pointing all three at the foreign envelope's own tuple admitted
            # it while ``--lane-label`` still named the real target. The expectation must come from
            # the retire target RESOLVED FROM DURABLE STATE, which is what ``target`` carries; the
            # flags are gone. ``target`` is None when the identity could not be resolved, which
            # fails closed — an unresolvable target cannot fence anything.
            #
            # ``integration_branch`` stays an argv comparison because it is the retire's real
            # policy input (it drives ``SublaneIntegrationPolicy``), not a value invented for this
            # fence.
            expected = (
                (target.workspace if target else "", evidence.envelope.workspace),
                (target.lane if target else "", evidence.envelope.lane),
                (str(target.lane_generation) if target else "",
                 str(evidence.envelope.lane_generation)),
                (str(getattr(args, "integration_branch", "") or "").strip(),
                 evidence.integration_branch),
            )
            bound = all(want and want == got for want, got in expected)

            # j#91797 F1: the ISSUER. The central `### Hibernate Evidence Marker Contract` fixes
            # this gate's writer to the coordinator, and #14219 already owns the resolution — R7-F3
            # said in as many words that reusing the Hibernate contract as automated evidence must
            # not drop its issuer condition, and R9 dropped it anyway.
            #
            # The role is resolved as POLICY from the note's own gate structure (that resolver takes
            # no author parameter on purpose), anchored to the committed config blob the caller
            # resolved. No policy pointer means no basis, which resolves every issuer to unknown and
            # fails closed. A note claiming two different authority gates proves neither.
            issuer_refusal = check_issuer(
                MARKER_GATE_INTEGRATION_DISPOSITION,
                resolve_journal_issuer(
                    integration.journal,
                    current_notes,
                    policy_pointer=(target.policy_pointer if target else ""),
                ),
                envelope=evidence.envelope,
            )
            source_head = evidence.source_head if bound and issuer_refusal is None else ""

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


def _resolve_latest_generation_admissible(
    args: argparse.Namespace, *, target: Optional[RetireEvidenceTarget] = None
) -> bool:
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
            _resolve_review_exemption_admissible(args, target=target)
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
    "RetireEvidenceTarget",
    "resolve_retire_evidence_target",
    "_resolve_latest_generation_admissible",
    "_resolve_review_exemption_admissible",
    "_resolve_review_generation_admissible",
)
