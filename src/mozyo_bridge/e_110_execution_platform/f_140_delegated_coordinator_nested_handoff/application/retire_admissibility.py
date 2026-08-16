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
  on a review that never happened;
- :func:`_resolve_no_change_waiver_admissible` — the no-change WAIVER fence (#14695). **It admits
  nothing today**: the record system cannot establish who WROTE a waiver, so the route always
  refuses with a typed reason (see ``no_change_review_waiver.WRITER_AUTHORITY_RESOLVABLE``). Its
  checks are implemented and tested so that a future writer-authority ruling wires in an authority
  rather than a re-implementation;
- :func:`...retire_superseded_failure.resolve_superseded_failure_admissible` — the SUPERSEDED
  FAILURE terminal (#14755), in its own module because adding the #14971 authority wiring pushed
  this one past the oversized-module gate. Re-exported here under its original private name: a
  lane whose latest review generation concluded ``changes_requested``, whose findings were all
  accepted, and whose acceptance target was obtained by a successor issue that acknowledges the
  supersession. Its round can never be approved, so the generation fence can only ever refuse it
  — and the two escapes from that (a false ``--latest-generation-admissible`` assert, or reading
  the successor's approval as this lane's) are exactly what the reproduction #14577 refused;
- :func:`...retire_superseded_audit_failure.resolve_superseded_audit_failure_admissible` — the
  SUPERSEDED AUDIT FAILURE terminal (#15166), likewise in its own module. The shape above but with
  NO formal Review Gate at all: a no-change verification lane whose round-1 verdict was recorded by
  an independent audit journal (``review_request`` was never posted, so no ``## Gate: review``
  exists), superseded by a successor whose own Review was approved. With zero review rounds the
  generation fence refuses forever and the #14755 terminal refuses too — it REQUIRES a round that
  concluded ``changes_requested`` — so the reproduction #15164 j#101825 sat permanently on
  ``stale_review_generation``.

The routes are independent and each can only ever admit; none can weaken another. A lane that
fails all of them is blocked exactly as it was before any of them existed.

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

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_superseded_audit_failure import (  # noqa: E501  (re-export)
    REASON_AUDIT_ROUTE_UNREADABLE,
    REASON_AUDIT_TARGET_UNRESOLVED,
    resolve_superseded_audit_failure_admissible as _resolve_superseded_audit_failure_admissible,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_superseded_failure import (  # noqa: E501  (re-export)
    REASON_SUPERSEDED_ROUTE_UNREADABLE,
    REASON_SUPERSEDED_TARGET_UNRESOLVED,
    committed_integration_branch,
    resolve_superseded_failure_admissible as _resolve_superseded_failure_admissible,
)


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
    #: The row's CAS revision at resolution time. Carried so the destructive close can re-read the
    #: row at the commit point and refuse if it advanced (Redmine #14539 review j#91847 finding 2).
    revision: int = 0
    #: The row's owner issue, independently measured rather than copied from the action request.
    issue: str = ""


def resolve_retire_evidence_target(
    args: argparse.Namespace, repo_root: Path, *, home: Optional[Path] = None
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

        workspace = str(herdr_workspace_segment(repo_root, home=home) or "").strip()
        if not workspace:
            return None
        record = LaneLifecycleStore(home=home).get(
            LaneLifecycleKey(workspace, lane_label)
        )
    except Exception:  # noqa: BLE001 - an unresolvable target is a typed zero, not a crash
        return None
    if record is None:
        return None
    generation = getattr(record, "lane_generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        return None
    revision = getattr(record, "revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        return None
    # The issuer basis is resolved separately and is allowed to be EMPTY here. An unreadable
    # committed config must not silently disable the generation / revision expectation the
    # destructive close depends on (review j#91847 finding 2) — those are different questions with
    # different consequences. The exemption route's own issuer check refuses an empty pointer.
    pointer = ""
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
            committed_config_policy_pointer,
        )

        pointer = str(committed_config_policy_pointer(repo_root) or "").strip()
    except Exception:  # noqa: BLE001 - an unresolvable basis is a typed zero, not a crash
        pointer = ""
    return RetireEvidenceTarget(
        workspace=workspace,
        lane=lane_label,
        lane_generation=generation,
        policy_pointer=pointer,
        revision=revision,
        issue=str(getattr(record, "issue_id", "") or ""),
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
            canonical_marker_value,
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
            marker_components_in_note,
            strict_marker_fields,
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
        # Read through THE shared strict reader, not the lenient dict fold (review j#91896
        # findings 2 and 3): an unreadable marker body — whitespace-contaminated, empty component,
        # repeated key — is refused whole rather than normalized into clean-looking fields. A
        # ``None`` here means this declaration carries no usable evidence at all.
        strict_markers = []
        for channel, components in marker_components_in_note(current_notes or ""):
            if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
                continue
            # The SAME canonicalizer the conflict detector uses, so the two consumers agree
            # about what "one declaration written twice" means.
            fields = strict_marker_fields(components, canonicalize=canonical_marker_value)
            if fields is None:
                strict_markers = None
                break
            strict_markers.append(fields)
        evidence = (
            IntegrationEvidenceError("marker_not_renderable")
            if strict_markers is None
            else resolve_integration_evidence(strict_markers)
        )
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


@dataclass(frozen=True)
class GenerationAdmissibility:
    """The resolved fence answer PLUS the reason a route refused (Redmine #14695 j#93807 F2).

    ``admissible`` is the boolean the retire has always fenced on. ``reason`` is the typed token
    the deciding route produced, or ``""`` when no route said anything more specific than "not
    admissible" — in which case the generic ``stale_review_generation`` still stands downstream.

    It exists because collapsing a route's refusal into a bare bool told the operator the wrong
    thing: the waiver route's ``waiver_writer_authority_unresolvable`` is a PERMANENT, structural
    refusal, and rendering it as a stale review generation sent them looking for an approval that
    could never exist. A boolean cannot carry a diagnosis.
    """

    admissible: bool = False
    reason: str = ""

    def __bool__(self) -> bool:
        """The fence answer, so every existing call site reads exactly as it did before.

        This resolver returned a bare ``bool`` until #14695 needed to carry a diagnosis alongside
        it. Changing the return type outright broke 46 assertions across the #13518 and #14539
        suites — tests that are correct and about other issues. Truthiness IS admissibility here,
        so honouring it keeps this change additive: callers that only need the verdict are
        untouched, and the one caller that needs the reason reads :attr:`reason` explicitly.
        """
        return bool(self.admissible)


@dataclass(frozen=True)
class LaneChangeMeasurement:
    """The live repository facts a no-change waiver is re-verified against (Redmine #14695).

    ``head`` is the lane branch's current head; ``commits_ahead`` the number of commits it carries
    that the integration branch does not; ``worktree_clean`` whether the lane checkout has no
    uncommitted change. Every field's "not measured" value is the fail-closed one — ``""`` /
    ``None`` / ``False`` — because an unreadable repository cannot testify that nothing changed.
    """

    head: str = ""
    commits_ahead: Optional[int] = None
    worktree_clean: bool = False


def measure_lane_change(
    repo_root: Path, *, branch: str, integration_branch: str, worktree: str = ""
) -> LaneChangeMeasurement:
    """Measure the lane's live change facts, read-only and fail-closed (Redmine #14695 j#93412 §2).

    **Every probe runs in the LANE CHECKOUT, and that checkout's own branch identity must be the
    branch the caller named.** Redmine #14695 review j#93576 finding 2 measured what the earlier
    split cost: the head and the ahead-count were resolved from ``--branch`` in the *repo root*
    while only the cleanliness came from ``--worktree``, and nothing checked that the two were the
    same checkout. Pointing ``--branch`` at the integration branch therefore produced a foreign
    head with zero commits ahead and a clean tree — a free "this lane changed nothing" reading for
    a checkout sitting on entirely different work. Reproduced on this very worktree: actual HEAD
    ``156b384f``, measured head ``735a5f88``, ``commits_ahead=0``, ``worktree_clean=True``.

    So identity is established FIRST and everything else is measured relative to it:

    - ``rev-parse --abbrev-ref HEAD`` in the checkout must exact-equal ``branch``. A detached HEAD
      prints ``HEAD`` and so never matches a branch name — the correct refusal, because a detached
      checkout has no branch identity to correlate. Any mismatch yields the wholly unmeasured
      value, never a partial reading;
    - ``rev-parse HEAD`` in the checkout — the head that checkout is ACTUALLY on, never a named ref
      resolved somewhere else. Compared downstream against the waiver's own head, so a lane that
      moved after the waiver was written is refused;
    - ``rev-list --count <integration_branch>..HEAD`` in the checkout — the commits this checkout
      carries that the integration branch does not. The ruling is explicit that this, not head
      equality with the integration branch, is the right question: "integration branch が後に
      進んだ場合、lane head がその ancestor であることは許容する". A lane that added nothing stays
      at zero however far the integration branch advances past it;
    - ``status --porcelain`` in the checkout — uncommitted change is repository change the waiver
      never covered.

    ``worktree`` is REQUIRED. Falling back to the repo root would reintroduce the very
    decorrelation this fixes, and the coordinator repo's cleanliness says nothing about the lane
    (the #13331 j#73338 boundary). ``repo_root`` is retained only as the caller's context; no
    probe reads it. Every probe resolves to the unmeasured value on ANY failure — a missing ref, a
    non-repository, an OS error — so a probe that cannot answer never fabricates a "nothing
    changed" reading.

    **What this still cannot decide, stated rather than implied.** Zero commits ahead does NOT
    mean the lane never produced a commit: a lane whose work was already merged is also zero
    ahead. Only the durable-record half
    (:func:`...no_change_review_waiver.fold_zero_change_record`) excludes that case, because
    integrated work necessarily leaves a commit record and an integration disposition behind. The
    two halves are conjoined for exactly this reason.
    """
    branch_s = str(branch or "").strip()
    integration_s = str(integration_branch or "").strip()
    checkout = str(worktree or "").strip()
    if not branch_s or not integration_s or not checkout:
        return LaneChangeMeasurement()

    def _git(*argv: str) -> Optional[str]:
        import subprocess

        try:
            result = subprocess.run(
                ["git", "-C", checkout, *argv], text=True, capture_output=True
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None

    # Identity FIRST: every fact below is a statement about THIS checkout, so if the checkout is
    # not on the branch the caller named, there is nothing here to say about that branch.
    #
    # Compared as a FULLY-QUALIFIED ref, not as an abbreviated name (review j#93638 finding 2).
    # ``--abbrev-ref HEAD`` prints the literal ``HEAD`` on a detached checkout, and ``--branch``
    # accepts any string — so a caller naming the branch ``HEAD`` made a detached checkout
    # "match", and a detached tree measured as a clean zero-change lane (reproduced: a detached
    # repo returned head=<full SHA>, commits_ahead=0, worktree_clean=True). R2's docstring claimed
    # a detached HEAD "never matches a branch name"; that was only true if the caller could not
    # say ``HEAD``, and it could.
    #
    # ``--symbolic-full-name HEAD`` yields ``refs/heads/<branch>`` when attached and the literal
    # ``HEAD`` when detached, so requiring the ``refs/heads/`` form rejects detachment
    # structurally rather than by blacklisting one spelling. The explicit ``HEAD`` guard stays as
    # well: a caller passing ``--branch refs/heads/HEAD`` would otherwise be back where it started.
    if branch_s == "HEAD":
        return LaneChangeMeasurement()
    symbolic = _git("rev-parse", "--symbolic-full-name", "HEAD")
    if symbolic is None or str(symbolic).strip() != f"refs/heads/{branch_s}":
        return LaneChangeMeasurement()

    head = str(_git("rev-parse", "HEAD") or "").strip().lower()

    ahead_out = _git("rev-list", "--count", f"{integration_s}..HEAD")
    ahead: Optional[int]
    try:
        ahead = int(str(ahead_out).strip()) if ahead_out is not None else None
    except (TypeError, ValueError):
        ahead = None

    status_out = _git("status", "--porcelain")
    worktree_clean = status_out is not None and not status_out.strip()

    return LaneChangeMeasurement(
        head=head, commits_ahead=ahead, worktree_clean=worktree_clean
    )


def _resolve_no_change_waiver_admissible(
    args: argparse.Namespace,
    *,
    target: Optional[RetireEvidenceTarget] = None,
    repo_root: Optional[Path] = None,
) -> bool:
    """Re-verify a NO-CHANGE waived lane's terminal-retire admissibility (Redmine #14695).

    A measured route to the one ``latest_generation_admissible`` fence — **currently always
    refusing** (see ``no_change_review_waiver.WRITER_AUTHORITY_RESOLVABLE``). #14613 produced
    zero repository change, the owner waived the independent review, and the retire still blocked
    on ``stale_review_generation`` because a lane that changed nothing has no review generation to
    be "latest" (reproduction: #14613 j#93256 / j#93262). Asserting
    ``--latest-generation-admissible`` for it would be a false assert about a review that never
    happened, so this measures the facts that carry the same safety weight instead.

    **The durable history is read LIVE, from the credential-gated Redmine read — never from a
    caller-supplied file.** This is the one place this route deliberately differs from the #14539
    exemption route, and the reason is structural, not stylistic (#14695 j#93412 §2 requires the
    "issue 全履歴"). The exemption's premise is POSITIVE — a gate exists — and a caller handing
    over a subset of the journals cannot fabricate one. This waiver's premise is NEGATIVE — no
    commit, no change scope, no integration disposition exists anywhere in the record — and a
    subset satisfies a negative claim by omission alone: drop the journal that declares the commit
    and the record "declares no change". A caller-supplied observation file would therefore be
    self-certifying for precisely the conjunct that protects ordinary development from being
    waived. The #14066 patch-equivalent retire already established the same rule for the same
    reason: the durable authority is the fresh read, never a caller-supplied file.

    Every other fence the route needs is measured, not asserted: the lane identity comes from the
    retire target's own lifecycle row (never argv — an identity the caller chooses fences
    nothing, #14539 j#91797 F2), the issuer is resolved from the gate->role policy with the
    ruling anchor j#93412 requires, and the live repository facts come from read-only git probes.

    Fail-closed on everything: unconfigured credentials, an unreadable Redmine, a malformed
    marker, an unresolvable target, an unmeasurable repository, a declared change, a recognized
    hard carve-out fact, an unresolved gate inventory, a newer review round, a moved head.
    """
    if not bool(getattr(args, "no_change_review_waiver", False)):
        return GenerationAdmissibility(False, "")
    if target is None or repo_root is None:
        return GenerationAdmissibility(False, REASON_WAIVER_TARGET_UNRESOLVED)
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
            LiveRedmineJournalSource,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
            fold_issue_gate_facts,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            GATE_NO_CHANGE_REVIEW_WAIVER,
            check_issuer,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            resolve_journal_issuer,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_carve_out import (  # noqa: E501
            fold_hard_carve_out,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_review_waiver import (  # noqa: E501
            evaluate_no_change_waiver_admissible,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            GATE_CLOSE,
        )

        issue = str(getattr(args, "issue", "") or "").strip()
        if not issue:
            return GenerationAdmissibility(False, REASON_WAIVER_ROUTE_UNREADABLE)

        entries = LiveRedmineJournalSource.from_environment().read_entries(issue)
        journals = [
            (str(getattr(e, "journal_id", "")), str(getattr(e, "notes", "") or ""))
            for e in entries or ()
        ]
        if not journals:
            # An empty history is not "a record that declares no change" — it is a read that
            # produced nothing, and the negative claim above must never be satisfied by silence.
            return GenerationAdmissibility(False, REASON_WAIVER_ROUTE_UNREADABLE)

        gate_facts = fold_issue_gate_facts(journals)
        if gate_facts is None:
            # No recognized gate at all: no Close evidence, and the gate inventory the hard
            # carve-out's resolution half requires could not be resolved either.
            return GenerationAdmissibility(False, REASON_WAIVER_ROUTE_UNREADABLE)

        waiver = gate_facts.review_waiver
        # The ISSUER, before the marker's own fields are trusted as authority. A marker naming
        # ``approval_source=direct_owner`` proves nothing about who wrote it (#14661 j#92601 F1);
        # the writer axis is resolved as POLICY from the note's gate structure, anchored to the
        # committed config blob and to THIS gate's own ruling (j#93412), and an empty policy
        # pointer resolves every issuer to unknown.
        waiver_notes = next(
            (notes for jid, notes in journals if jid.strip() == waiver.journal), ""
        )
        if waiver.envelope is None or check_issuer(
            GATE_NO_CHANGE_REVIEW_WAIVER,
            resolve_journal_issuer(
                waiver.journal, waiver_notes, policy_pointer=target.policy_pointer
            ),
            envelope=waiver.envelope,
        ) is not None:
            return GenerationAdmissibility(False, REASON_WAIVER_ISSUER_UNRESOLVED)

        measured = measure_lane_change(
            repo_root,
            branch=str(getattr(args, "branch", "") or ""),
            integration_branch=str(getattr(args, "integration_branch", "") or ""),
            worktree=str(getattr(args, "worktree", "") or ""),
        )

        outcome = (
            evaluate_no_change_waiver_admissible(
                waiver,
                # The supersession half ALONE, with zero-change passed beside it as its own
                # conjunct. The conjunction is identical to the glance's ``review_waived``, but
                # keeping the halves separate is what lets each refusal name its true cause:
                # handing the folded boolean in made a change-bearing record refuse as "superseded
                # by a newer review round", pointing an operator at a review that does not exist.
                currently_in_force=gate_facts.review_waiver_unsuperseded,
                zero_change=gate_facts.zero_change,
                # No flag: the carve-out resolves itself from the record's governed ``work_unit``
                # declaration. R1 passed ``gates_resolved=True`` here on the strength of "a
                # lifecycle gate parsed", which proved the record was readable, not that its
                # classification was resolved (review j#93576 finding 1).
                carve_out=fold_hard_carve_out(journals),
                close_recorded=gate_facts.latest_gate == GATE_CLOSE,
                target_issue=issue,
                expected_workspace=target.workspace,
                expected_lane=target.lane,
                expected_lane_generation=target.lane_generation,
                live_head=measured.head,
                live_commits_ahead=measured.commits_ahead,
                worktree_clean=measured.worktree_clean,
                callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
            )
        )
        return GenerationAdmissibility(
            admissible=bool(outcome.admissible), reason=str(outcome.reason or "")
        )
    except Exception:  # noqa: BLE001 - unreadable durable / live state -> fail closed
        return GenerationAdmissibility(False, REASON_WAIVER_ROUTE_UNREADABLE)


def _read_live_issue_journals(issue: str) -> "list[tuple[str, str]]":
    """One issue's full durable history, read LIVE over the credential-gated Redmine read (IO).

    #14695 needs it because its premise is NEGATIVE (nothing anywhere in the record declares
    change) and a subset satisfies a negative claim by omission alone. #14755 needs the same live
    history in its ENTRY form — see :func:`...retire_superseded_failure._read_live_issue_entries`
    for why that route cannot use these pairs.

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
    return [
        (str(getattr(e, "journal_id", "")), str(getattr(e, "notes", "") or ""))
        for e in entries or ()
    ]


def _resolve_latest_generation_admissible(
    args: argparse.Namespace,
    *,
    target: Optional[RetireEvidenceTarget] = None,
    repo_root: Optional[Path] = None,
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

    When ANY measured input is supplied, the measurement decides and the operator assertion is NOT
    consulted: a supplied-but-failing measurement must never fall back to a hand assert (#14695
    j#93412 §4 restates this for the waiver route). The measured routes are independent evidence
    for the same fence — a lane either passed a review generation, was exempt from one, or
    produced nothing to review — so any one of them admitting is sufficient, and none of them can
    weaken the others.
    """
    exemption_path = (getattr(args, "review_exemption_json", None) or "").strip()
    path = (getattr(args, "review_generation_json", None) or "").strip()
    # Redmine #14695: the no-change waiver is a THIRD measured input. It is a bare opt-in rather
    # than a path because its evidence is read live from the durable authority (see that
    # function's docstring for why a caller-supplied file cannot carry a negative claim).
    waiver = bool(getattr(args, "no_change_review_waiver", False))
    # Redmine #14755: the superseded-failure terminal is a FOURTH, live for the same reason.
    superseded = bool(getattr(args, "superseded_failure_terminal", False))
    # Redmine #15166: the superseded-AUDIT-failure terminal is a FIFTH, live for the same reason
    # plus one of its own — two of its conjuncts are negative claims over the whole record.
    audit_terminal = bool(getattr(args, "superseded_audit_failure_terminal", False))
    if path or exemption_path or waiver or superseded or audit_terminal:
        if _resolve_review_generation_admissible(args):
            return GenerationAdmissibility(True, "")
        if _resolve_review_exemption_admissible(args, target=target):
            return GenerationAdmissibility(True, "")
        # The two reason-carrying routes, in a fixed order. Each returns a BLANK reason when its
        # own opt-in is absent, so a caller that opted into one never receives the other's
        # diagnosis, and the FIRST non-blank reason is kept: a route that went to the trouble of
        # producing a typed refusal must reach the operator rather than being overwritten by a
        # route that was never asked (#14695 review j#93807 finding 2 established why the reason
        # matters at all — collapsing a structural refusal into ``stale_review_generation`` sent
        # an operator hunting for a review generation that cannot exist).
        answer = GenerationAdmissibility(False, "")
        for route in (
            _resolve_no_change_waiver_admissible,
            _resolve_superseded_failure_admissible,
            _resolve_superseded_audit_failure_admissible,
        ):
            result = route(args, target=target, repo_root=repo_root)
            if result.admissible:
                return result
            if not answer.reason and result.reason:
                answer = result
        return answer
    return GenerationAdmissibility(
        bool(getattr(args, "latest_generation_admissible", False)), ""
    )


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


#: The waiver route could not even read its own inputs (no target, unreadable Redmine, no
#: recognized gate). Distinct from a refusal the domain reasoned about.
REASON_WAIVER_ROUTE_UNREADABLE = "waiver_route_evidence_unreadable"
#: The retire target's lane identity could not be measured from durable state.
REASON_WAIVER_TARGET_UNRESOLVED = "waiver_retire_target_unresolved"
#: The waiver journal's issuer did not resolve to this gate's contracted writer.
REASON_WAIVER_ISSUER_UNRESOLVED = "waiver_issuer_unresolved"

__all__ = (
    "GenerationAdmissibility",
    "LaneChangeMeasurement",
    "REASON_AUDIT_ROUTE_UNREADABLE",
    "REASON_AUDIT_TARGET_UNRESOLVED",
    "REASON_SUPERSEDED_ROUTE_UNREADABLE",
    "REASON_SUPERSEDED_TARGET_UNRESOLVED",
    "REASON_WAIVER_ISSUER_UNRESOLVED",
    "REASON_WAIVER_ROUTE_UNREADABLE",
    "REASON_WAIVER_TARGET_UNRESOLVED",
    "RetireEvidenceTarget",
    "committed_integration_branch",
    "measure_lane_change",
    "resolve_retire_evidence_target",
    "_resolve_latest_generation_admissible",
    "_resolve_no_change_waiver_admissible",
    "_resolve_review_exemption_admissible",
    "_resolve_review_generation_admissible",
    "_resolve_superseded_audit_failure_admissible",
    "_resolve_superseded_failure_admissible",
)
