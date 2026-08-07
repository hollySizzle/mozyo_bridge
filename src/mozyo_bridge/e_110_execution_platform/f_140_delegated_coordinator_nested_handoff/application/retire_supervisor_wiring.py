"""Production candidate/evidence wiring for automatic finished-lane retirement (#15066).

Candidate discovery is read-only.  It joins the reboot convergence planner with fresh durable
review/integration/CI, callback debt, issue status and Git-origin evidence.  The returned leg takes
two complete snapshots under the workspace lease and requires exact equality before invoking the
shared :mod:`sublane_retire_application` facade.  Unknown, ambiguous or changed evidence writes
nothing.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E501
    RETIRE_INTENT_ACTIVE_LIVE_ZERO,
    RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
    RETIRE_INTENT_EXECUTE,
    RETIRE_INTENT_HIBERNATED_BOUND,
    RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
    RetireApplicationRequest,
    RetireApplicationResult,
    RetireAssertions,
    RetireIdentity,
    run_retire_application,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_retire_leg import (  # noqa: E501
    RetireAttempt,
    RetirePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
    CONVERGE_GUARDED_CLOSE,
    CONVERGE_TERMINALIZE_BOUND,
    CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND,
    CONVERGE_TERMINALIZE_UNBOUND,
)

RETIRE_CANDIDATE_AMBIGUOUS = "retire_candidate_ambiguous"
RETIRE_SNAPSHOT_CHANGED = "retire_snapshot_changed"
RETIRE_LEASE_LOST = "retire_lease_lost"

_AUTOMATIC_CONVERGENCES = frozenset(
    {
        CONVERGE_GUARDED_CLOSE,
        CONVERGE_TERMINALIZE_BOUND,
        CONVERGE_TERMINALIZE_UNBOUND,
        CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND,
    }
)


@dataclass(frozen=True)
class RetireCandidateSnapshot:
    """All identity and evidence used to authorize one automatic retire."""

    workspace: str
    issue: str
    lane: str
    lane_generation: int
    revision: int
    lane_disposition: str
    worktree: str
    branch: str
    integration_branch: str
    intent: str
    decision_journal: str
    integration_journal: str
    review_request_journal: str
    review_head: str
    integration_source_head: str
    integration_head: str
    origin_tip: str
    ci_run: str
    issue_state_known: bool
    issue_closed: bool
    inventory_known: bool
    callback_debt_known: bool
    callbacks_drained: bool
    task_close_journal: str
    owner_gates_resolved: bool
    review_admissible: bool
    integration_confirmed: bool
    integration_ci_green: bool
    worktree_clean: bool
    source_head_matches_branch: bool
    origin_reachable: bool

    @property
    def blocked_reason(self) -> str:
        for ok, reason in (
            (bool(self.workspace and self.issue and self.lane), "candidate_identity_incomplete"),
            (
                self.lane_generation > 0 and self.revision > 0,
                "candidate_generation_revision_invalid",
            ),
            (bool(self.intent), "candidate_intent_unresolved"),
            (self.issue_state_known, "issue_state_unreadable"),
            (self.issue_closed, "issue_not_closed"),
            (self.inventory_known, "inventory_unreadable"),
            (self.callback_debt_known, "callback_debt_unreadable"),
            (self.callbacks_drained, "unresolved_callback"),
            (bool(self.task_close_journal), "task_close_missing"),
            (bool(self.decision_journal), "durable_record_missing"),
            (bool(self.integration_journal), "integration_record_missing"),
            (self.owner_gates_resolved, "unresolved_owner_gate"),
            (self.review_admissible, "review_generation_inadmissible"),
            (self.integration_confirmed, "integration_unconfirmed"),
            (self.integration_ci_green, "integration_ci_unsettled"),
            (self.worktree_clean, "dirty_worktree"),
            (self.source_head_matches_branch, "source_head_mismatch"),
            (self.origin_reachable, "origin_reachability_unproven"),
        ):
            if not ok:
                return reason
        return ""

    @property
    def eligible(self) -> bool:
        return not self.blocked_reason

    @property
    def identity(self) -> RetireIdentity:
        return RetireIdentity(
            workspace=self.workspace,
            issue=self.issue,
            lane=self.lane,
            lane_generation=self.lane_generation,
            revision=self.revision,
        )

    def application_request(
        self, *, repo_root: Path, home: Optional[Path] = None
    ) -> RetireApplicationRequest:
        unbound = self.intent in (
            RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
            RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
        )
        return RetireApplicationRequest(
            repo_root=repo_root,
            home=home,
            issue=self.issue,
            lane_label=self.lane,
            assertions=RetireAssertions(
                issue_closed=self.issue_closed,
                callbacks_drained=self.callbacks_drained,
                verification_passed=self.integration_ci_green,
                durable_record_recorded=bool(
                    self.decision_journal and self.integration_journal
                ),
                target_identity_known=True,
                latest_generation_admissible=self.review_admissible,
            ),
            intent=self.intent,
            worktree=None if unbound else self.worktree,
            branch=self.branch,
            integration_branch=self.integration_branch,
            journal=self.decision_journal,
            expect_lane_generation=self.lane_generation,
            expect_lane_revision=self.revision,
            integration_journal=self.integration_journal,
            expected_identity=self.identity,
        )


SnapshotReader = Callable[[object, Optional[frozenset[str]]], Sequence[RetireCandidateSnapshot]]
RetireExecutor = Callable[[RetireApplicationRequest], RetireApplicationResult]


def _attempt(candidate: RetireCandidateSnapshot, result: RetireApplicationResult) -> RetireAttempt:
    cleanup = result.as_payload()["cleanup"]
    return RetireAttempt(
        issue=candidate.issue,
        lane=candidate.lane,
        lane_generation=candidate.lane_generation,
        revision=candidate.revision,
        state=result.state,
        reason=result.reason,
        mutated=result.mutated,
        uncertain=result.uncertain,
        cleanup_state=str(cleanup["state"]),
        cleanup_reason=str(cleanup["reason"]),
    )


def build_retire_leg(
    *, snapshot_fn: SnapshotReader, retire_fn: RetireExecutor = run_retire_application,
    home: Optional[Path] = None,
):
    """Build the two-snapshot, exactly-one retire leg over injected live readers."""

    def leg(ws, renew, budget, *, restrict_issues=None) -> RetirePassResult:
        first = tuple(snapshot_fn(ws, restrict_issues))
        if not first:
            return RetirePassResult()
        if len(first) != 1:
            candidate = first[0]
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue=candidate.issue,
                        lane=candidate.lane,
                        lane_generation=candidate.lane_generation,
                        revision=candidate.revision,
                        state="blocked",
                        reason=RETIRE_CANDIDATE_AMBIGUOUS,
                    ),
                )
            )
        candidate = first[0]
        if not candidate.eligible:
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue=candidate.issue,
                        lane=candidate.lane,
                        lane_generation=candidate.lane_generation,
                        revision=candidate.revision,
                        state="blocked",
                        reason=candidate.blocked_reason,
                    ),
                )
            )
        if not renew():
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue=candidate.issue,
                        lane=candidate.lane,
                        lane_generation=candidate.lane_generation,
                        revision=candidate.revision,
                        state="blocked",
                        reason=RETIRE_LEASE_LOST,
                    ),
                )
            )
        second = tuple(snapshot_fn(ws, restrict_issues))
        if second != first:
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue=candidate.issue,
                        lane=candidate.lane,
                        lane_generation=candidate.lane_generation,
                        revision=candidate.revision,
                        state="blocked",
                        reason=RETIRE_SNAPSHOT_CHANGED,
                    ),
                )
            )
        if not renew():
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue=candidate.issue,
                        lane=candidate.lane,
                        lane_generation=candidate.lane_generation,
                        revision=candidate.revision,
                        state="blocked",
                        reason=RETIRE_LEASE_LOST,
                    ),
                )
            )
        if budget.get("mutated") or budget.get("uncertain"):
            return RetirePassResult(
                attempts=(
                    RetireAttempt(
                        issue=candidate.issue,
                        lane=candidate.lane,
                        lane_generation=candidate.lane_generation,
                        revision=candidate.revision,
                        state="deferred",
                        reason="retire_budget_deferred",
                    ),
                )
            )
        result = retire_fn(
            candidate.application_request(
                repo_root=Path(ws.canonical_path), home=home
            )
        )
        return RetirePassResult(attempts=(_attempt(candidate, result),))

    return leg


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _clean_worktree(path: str) -> bool:
    if not path:
        return True
    root = Path(path)
    if not root.is_dir():
        return False
    status = _git(root, "status", "--porcelain")
    return status == ""


def _intent_for(facts, convergence: str) -> str:
    if convergence == CONVERGE_GUARDED_CLOSE:
        return RETIRE_INTENT_EXECUTE
    if convergence == CONVERGE_TERMINALIZE_UNBOUND:
        return RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO
    if convergence == CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND:
        return RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO
    if convergence == CONVERGE_TERMINALIZE_BOUND:
        return (
            RETIRE_INTENT_HIBERNATED_BOUND
            if str(facts.lane_disposition) == "hibernated"
            else RETIRE_INTENT_ACTIVE_LIVE_ZERO
        )
    return ""


def _blocked_snapshot(
    facts,
    *,
    integration_branch: str,
    intent: str,
    issue_state: Optional[bool],
    origin_tip: str = "",
) -> RetireCandidateSnapshot:
    """Minimal fail-closed snapshot used before expensive per-candidate evidence reads."""
    return RetireCandidateSnapshot(
        workspace=facts.workspace_id,
        issue=facts.issue_id,
        lane=facts.lane_id,
        lane_generation=facts.lane_generation,
        revision=facts.revision,
        lane_disposition=facts.lane_disposition,
        worktree=facts.recorded_worktree,
        branch=facts.branch,
        integration_branch=integration_branch,
        intent=intent,
        decision_journal="",
        integration_journal="",
        review_request_journal="",
        review_head="",
        integration_source_head="",
        integration_head="",
        origin_tip=origin_tip,
        ci_run="",
        issue_state_known=issue_state is not None,
        issue_closed=issue_state is True,
        inventory_known=facts.slots is not None,
        callback_debt_known=False,
        callbacks_drained=False,
        task_close_journal="",
        owner_gates_resolved=False,
        review_admissible=False,
        integration_confirmed=False,
        integration_ci_green=False,
        worktree_clean=False,
        source_head_matches_branch=False,
        origin_reachable=False,
    )


def default_retire_leg_fn(
    *, home=None, outbox=None, environ: Optional[Mapping[str, str]] = None
):
    """Compose the production leg from the same live authorities as integration cleanup."""
    from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
    from mozyo_bridge.core.state.lane_lifecycle_model import (
        DISPOSITION_ACTIVE,
        DISPOSITION_HIBERNATED,
        RELEASE_NOT_REQUESTED,
        RELEASE_RELEASED,
    )
    from mozyo_bridge.core.state.lane_lifecycle_readonly import load_lane_lifecycle_readonly
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
        GhCliCiStatusReader,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
        live_journal_reader,
        load_committed_repo_local_config,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_authority import (  # noqa: E501
        live_cleanup_callback_scope,
        unresolved_lane_callback_debt,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (  # noqa: E501
        LiveAutoIntegrationGitOperations,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_reboot_audit import (  # noqa: E501
        gather_reboot_facts,
        read_issue_closed_states,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_authority import (  # noqa: E501
        CiRecord,
        LaneScope,
        ci_record_for_head,
        fold_durable_authority,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
        fold_issue_gate_facts,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
        as_pairs,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
        plan_lane_convergence,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
        GATE_CLOSE,
    )

    lifecycle = LaneLifecycleStore(home=home)
    def snapshots(ws, restrict_issues=None):
        repo_root = Path(ws.canonical_path)
        try:
            config = load_committed_repo_local_config(repo_root)
            integration_branch = str(
                getattr(config.sublane_integration, "integration_branch", "") or ""
            ).strip()
        except Exception:  # noqa: BLE001 - unreadable target authorizes no candidate
            return ()
        if not integration_branch:
            return ()
        records = load_lane_lifecycle_readonly(home=home)
        if records is None:
            return ()
        # Retired/superseded/in-flight rows cannot reach any automatic terminal rail.  Drop
        # them before ``gather_reboot_facts`` performs its per-row Git probes.  This keeps a
        # supervisor pass proportional to the small live frontier, not to all historical
        # lifecycle rows retained by the host.
        frontier = tuple(
            record
            for record in records
            if record.repo_workspace_id == ws.workspace_id
            and record.issue_id
            and (
                (
                    record.lane_disposition == DISPOSITION_ACTIVE
                    and record.process_release
                    in (RELEASE_NOT_REQUESTED, RELEASE_RELEASED)
                )
                or (
                    record.lane_disposition == DISPOSITION_HIBERNATED
                    and record.process_release == RELEASE_RELEASED
                )
            )
        )
        if not frontier:
            return ()
        issue_ids = tuple(
            dict.fromkeys(record.issue_id for record in frontier)
        )
        # Build the expensive local topology once with an explicitly unknown issue axis.
        # Only rows that could become one of the automatic terminal rails when closed are
        # allowed to cause a provider read.  A workspace can retain hundreds of historical
        # lifecycle rows; polling Redmine once per row on every supervisor pass is neither
        # bounded nor operationally acceptable.
        facts_rows = gather_reboot_facts(
            repo_root,
            home=home,
            integration_branch=integration_branch,
            issue_states={issue: None for issue in issue_ids},
            lifecycle_rows=frontier,
            environ=environ if environ is not None else os.environ,
        )
        possible_facts = []
        for facts in facts_rows:
            if facts.workspace_id != ws.workspace_id:
                continue
            if restrict_issues is not None and facts.issue_id not in restrict_issues:
                continue
            # ``head_integrated`` is intentionally hypothetical here.  The authorizing
            # reachability probe below uses a fresh remote tip, never this cached local view.
            hypothetical = replace(
                facts, issue_closed=True, head_integrated=True
            )
            if (
                facts.slots is None
                or plan_lane_convergence(hypothetical).convergence
                in _AUTOMATIC_CONVERGENCES
            ):
                possible_facts.append(facts)
        if not possible_facts:
            return ()
        relevant_issues = tuple(
            dict.fromkeys(facts.issue_id for facts in possible_facts)
        )
        issue_states = read_issue_closed_states(
            relevant_issues, home=home, environ=environ
        )
        candidate_inputs = []
        for facts in possible_facts:
            issue_state = issue_states.get(facts.issue_id)
            if issue_state is False:
                continue
            # A provider failure is retained as a typed blocked candidate. A genuinely open
            # issue is not a finished candidate and was filtered immediately above.
            planning_facts = replace(
                facts,
                issue_closed=True,
                head_integrated=True,
                slots=() if facts.slots is None else facts.slots,
            )
            plan = plan_lane_convergence(planning_facts)
            if plan.convergence not in _AUTOMATIC_CONVERGENCES:
                continue
            candidate_inputs.append(
                (facts, issue_state, _intent_for(facts, plan.convergence))
            )
        if not candidate_inputs:
            return ()
        if len(candidate_inputs) > 1:
            # Ambiguity itself is the verdict. Do not make one journal/CI/Git read per stale
            # lane merely to rediscover that no unique action is authorized.
            return tuple(
                _blocked_snapshot(
                    facts,
                    integration_branch=integration_branch,
                    intent=intent,
                    issue_state=issue_state,
                )
                for facts, issue_state, intent in candidate_inputs
            )
        journal_fn = live_journal_reader(
            repo_root=repo_root, home=home, environ=environ
        )
        ci_reader = GhCliCiStatusReader(repo_root=repo_root)
        git_ops = LiveAutoIntegrationGitOperations(repo_root=repo_root)
        origin_tip = git_ops.remote_branch_tip(integration_branch)
        candidates: list[RetireCandidateSnapshot] = []
        for facts, issue_state, intent in candidate_inputs:
            journals = journal_fn(facts.issue_id)
            if journals is None:
                candidates.append(
                    _blocked_snapshot(
                        facts,
                        integration_branch=integration_branch,
                        intent=intent,
                        issue_state=issue_state,
                        origin_tip=origin_tip,
                    )
                )
                continue
            scope = LaneScope(
                workspace=facts.workspace_id,
                lane=facts.lane_id,
                lane_generation=facts.lane_generation,
            )
            durable = fold_durable_authority(journals, scope=scope)
            review = durable.review
            integration = durable.integration
            gates = fold_issue_gate_facts(as_pairs(journals))
            owner_gates_resolved = bool(
                gates is not None
                and not gates.blocker_recorded
                and not gates.review_round_unresolved
            )
            callback_scope = live_cleanup_callback_scope(
                lifecycle,
                workspace_id=facts.workspace_id,
                issue=facts.issue_id,
                lane=facts.lane_id,
                lane_generation=facts.lane_generation,
            )
            debt = (
                unresolved_lane_callback_debt(outbox, scope=callback_scope)
                if outbox is not None
                else None
            )
            ci = ci_record_for_head(
                journals, head=integration.integration_head, scope=scope
            )
            ci_run = ""
            ci_green = False
            if isinstance(ci, CiRecord) and ci.is_present:
                verdict = ci_reader.verdict_for(
                    integration.integration_head,
                    workflow=ci.workflow,
                    branch=integration_branch,
                )
                ci_run = str(getattr(verdict, "run", "") or "")
                ci_green = bool(
                    not getattr(verdict, "blocks", True)
                    and str(getattr(verdict, "commit", "") or "")
                    == integration.integration_head
                    and str(getattr(verdict, "branch", "") or "")
                    == integration_branch
                    and str(getattr(verdict, "workflow", "") or "") == ci.workflow
                )
            source_tip = git_ops.resolve_head(facts.branch)
            worktree_clean = (
                True
                if intent
                in (
                    RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
                    RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
                )
                else _clean_worktree(facts.recorded_worktree)
            )
            candidates.append(
                RetireCandidateSnapshot(
                    workspace=facts.workspace_id,
                    issue=facts.issue_id,
                    lane=facts.lane_id,
                    lane_generation=facts.lane_generation,
                    revision=facts.revision,
                    lane_disposition=facts.lane_disposition,
                    worktree=facts.recorded_worktree,
                    branch=facts.branch,
                    integration_branch=integration_branch,
                    intent=intent,
                    decision_journal=(
                        gates.latest_gate_journal
                        if gates is not None and gates.latest_gate == GATE_CLOSE
                        else ""
                    ),
                    integration_journal=integration.journal,
                    review_request_journal=review.request_journal,
                    review_head=review.head,
                    integration_source_head=integration.source_head,
                    integration_head=integration.integration_head,
                    origin_tip=origin_tip,
                    ci_run=ci_run,
                    issue_state_known=issue_state is not None,
                    issue_closed=issue_state is True,
                    inventory_known=facts.slots is not None,
                    callback_debt_known=debt is not None,
                    callbacks_drained=debt == 0,
                    task_close_journal=(
                        gates.latest_gate_journal
                        if gates is not None and gates.latest_gate == GATE_CLOSE
                        else ""
                    ),
                    owner_gates_resolved=owner_gates_resolved,
                    review_admissible=bool(
                        review.admissible
                        and review.head
                        and review.head == integration.source_head
                    ),
                    integration_confirmed=bool(
                        integration.confirmed
                        and integration.integration_branch == integration_branch
                        and integration.source_head
                        and integration.integration_head
                    ),
                    integration_ci_green=ci_green,
                    worktree_clean=worktree_clean,
                    source_head_matches_branch=bool(
                        source_tip and source_tip == integration.source_head
                    ),
                    origin_reachable=bool(
                        origin_tip
                        and git_ops.is_ancestor(
                            ancestor=integration.integration_head,
                            descendant=origin_tip,
                        )
                        and git_ops.is_ancestor(
                            ancestor=integration.source_head,
                            descendant=origin_tip,
                        )
                    ),
                )
            )
        return tuple(sorted(candidates, key=lambda candidate: (candidate.issue, candidate.lane)))

    return build_retire_leg(snapshot_fn=snapshots, home=home)


__all__ = (
    "RetireCandidateSnapshot",
    "RETIRE_CANDIDATE_AMBIGUOUS",
    "RETIRE_SNAPSHOT_CHANGED",
    "RETIRE_LEASE_LOST",
    "build_retire_leg",
    "default_retire_leg_fn",
)
