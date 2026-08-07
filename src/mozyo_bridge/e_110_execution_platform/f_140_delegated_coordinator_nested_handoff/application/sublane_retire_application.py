"""Typed ``sublane retire`` application facade shared by CLI and supervisor (#15066).

The command used to own the preflight and destructive-intent dispatch inline.  That made the
workspace supervisor choose between shelling out to the CLI (and parsing prose) or duplicating the
retire contract.  This module is the single programmatic boundary instead: a typed request goes in
and a typed result reports whether the lane retired, was already retired, was blocked, was deferred,
or became uncertain.

No Git cleanup is performed here.  Worktree and branch cleanup remain the operator runbook because
Git has no non-force primitive that atomically checks the lane identity while removing the path/ref.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


RETIRE_INTENT_PREFLIGHT = "preflight"
RETIRE_INTENT_EXECUTE = "execute"
RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY = "migrate_hibernated_legacy"
RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE = "reconcile_hibernated_live"
RETIRE_INTENT_HIBERNATED_BOUND = "retire_hibernated_bound"
RETIRE_INTENT_ACTIVE_LIVE_ZERO = "retire_active_live_zero"
RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO = "retire_active_unbound_live_zero"
RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO = (
    "retire_hibernated_unbound_live_zero"
)

RETIRE_INTENTS = frozenset(
    {
        RETIRE_INTENT_PREFLIGHT,
        RETIRE_INTENT_EXECUTE,
        RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY,
        RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE,
        RETIRE_INTENT_HIBERNATED_BOUND,
        RETIRE_INTENT_ACTIVE_LIVE_ZERO,
        RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
        RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
    }
)

RETIRE_RESULT_RETIRED = "retired"
RETIRE_RESULT_BLOCKED = "blocked"
RETIRE_RESULT_DEFERRED = "deferred"
RETIRE_RESULT_UNCERTAIN = "uncertain"
RETIRE_RESULT_ALREADY_RETIRED = "already_retired"

REASON_PREFLIGHT_ONLY = "preflight_only"
REASON_INTENT_NOT_APPLICABLE = "retire_intent_not_applicable"
REASON_IDENTITY_UNRESOLVED = "retire_identity_unresolved"
REASON_IDENTITY_CHANGED = "retire_identity_changed"
REASON_APPLICATION_ERROR = "retire_application_error"
REASON_CLEANUP_ATOMIC_GUARD_UNAVAILABLE = "cleanup_atomic_guard_unavailable"


@dataclass(frozen=True)
class RetireAssertions:
    """Durable facts the common preflight enforces; every default fails closed."""

    issue_closed: bool = False
    callbacks_drained: bool = False
    verification_passed: bool = False
    durable_record_recorded: bool = False
    target_identity_known: bool = False
    latest_generation_admissible: bool = False
    latest_generation_blocked_reason: str = ""


@dataclass(frozen=True)
class RetireIdentity:
    """The exact lifecycle identity measured independently of the action request."""

    workspace: str
    issue: str
    lane: str
    lane_generation: int
    revision: int

    @property
    def complete(self) -> bool:
        return bool(
            self.workspace
            and self.issue
            and self.lane
            and isinstance(self.lane_generation, int)
            and not isinstance(self.lane_generation, bool)
            and self.lane_generation > 0
            and isinstance(self.revision, int)
            and not isinstance(self.revision, bool)
            and self.revision > 0
        )


@dataclass(frozen=True)
class RetireApplicationRequest:
    """One exact retire request, independent of argparse and stdout rendering."""

    repo_root: Path
    issue: str
    lane_label: str
    assertions: RetireAssertions
    home: Optional[Path] = None
    intent: str = RETIRE_INTENT_PREFLIGHT
    worktree: Optional[str] = None
    branch: Optional[str] = None
    integration_branch: Optional[str] = None
    journal: Optional[str] = None
    expect_lane_generation: int = 0
    expect_lane_revision: int = 0
    integration_journal: Optional[str] = None
    expected_identity: Optional[RetireIdentity] = None

    def __post_init__(self) -> None:
        if self.intent not in RETIRE_INTENTS:
            raise ValueError(f"unknown retire intent: {self.intent!r}")

    def as_namespace(self):
        """Compatibility shape for the seven existing, independently reviewed intent rails."""
        import argparse

        selected = self.intent
        return argparse.Namespace(
            repo=str(self.repo_root),
            home=self.home,
            issue=self.issue,
            lane_label=self.lane_label,
            worktree=self.worktree,
            branch=self.branch,
            integration_branch=self.integration_branch,
            journal=self.journal,
            expect_lane_generation=self.expect_lane_generation,
            expect_lane_revision=self.expect_lane_revision,
            integration_journal=self.integration_journal,
            execute=selected == RETIRE_INTENT_EXECUTE,
            migrate_hibernated_legacy=(
                selected == RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY
            ),
            reconcile_hibernated_live=(
                selected == RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE
            ),
            retire_hibernated_bound=selected == RETIRE_INTENT_HIBERNATED_BOUND,
            retire_active_live_zero=selected == RETIRE_INTENT_ACTIVE_LIVE_ZERO,
            retire_active_unbound_live_zero=(
                selected == RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO
            ),
            retire_hibernated_unbound_live_zero=(
                selected == RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO
            ),
        )


@dataclass(frozen=True)
class RetireApplicationResult:
    """Programmatic retire result; exceptions never masquerade as a deterministic refusal."""

    state: str
    reason: str = ""
    mutated: bool = False
    uncertain: bool = False
    preflight: Optional[object] = None
    intents: Optional[object] = None

    @property
    def retire_ok(self) -> bool:
        return self.state in (RETIRE_RESULT_RETIRED, RETIRE_RESULT_ALREADY_RETIRED)

    @property
    def legacy_cli_ok(self) -> bool:
        """Preserve the historical successful read-only preflight exit status."""
        if self.state == RETIRE_RESULT_DEFERRED and self.reason in (
            REASON_PREFLIGHT_ONLY,
            REASON_INTENT_NOT_APPLICABLE,
        ):
            return bool(self.preflight and self.preflight.preflight.may_retire)
        return self.retire_ok

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "mutated": self.mutated,
            "uncertain": self.uncertain,
            "cleanup": {
                "state": "cleanup_blocked" if self.retire_ok else "not_started",
                "reason": (
                    REASON_CLEANUP_ATOMIC_GUARD_UNAVAILABLE if self.retire_ok else ""
                ),
                "worktree_removed": False,
                "local_branch_deleted": False,
                "remote_branch_deleted": False,
            },
        }


def _measured_identity(target, *, issue: str) -> Optional[RetireIdentity]:
    if target is None:
        return None
    measured_issue = getattr(target, "issue", None)
    identity = RetireIdentity(
        workspace=str(getattr(target, "workspace", "") or ""),
        # Production evidence targets carry the lane row's owner issue. The fallback keeps
        # backward-compatible injected test doubles usable; an actual row with a blank owner
        # remains blank and therefore incomplete/fail-closed.
        issue=str(issue if measured_issue is None else measured_issue or ""),
        lane=str(getattr(target, "lane", "") or ""),
        lane_generation=getattr(target, "lane_generation", 0),
        revision=getattr(target, "revision", 0),
    )
    return identity if identity.complete else None


def _is_already_state(value: object) -> bool:
    state = str(getattr(value, "state", "") or "")
    return state in ("already_retired", "verified_noop")


def run_retire_application(request: RetireApplicationRequest) -> RetireApplicationResult:
    """Run common preflight + one intent with an action-time exact-identity fence."""
    # Local imports avoid a command/application import cycle while retaining the already-reviewed
    # use case and intent rails as the single implementation of their respective contracts.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
        resolve_retire_evidence_target,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
        LiveSublaneGitOperations,
        LiveSublaneLifecycleOps,
        SublaneRetireUseCase,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_intents import (  # noqa: E501
        dispatch_retire_intent,
    )

    args = request.as_namespace()
    try:
        target = resolve_retire_evidence_target(
            args, request.repo_root, home=request.home
        )
        measured = _measured_identity(target, issue=request.issue)
        if request.expected_identity is not None:
            if measured is None:
                return RetireApplicationResult(
                    state=RETIRE_RESULT_BLOCKED, reason=REASON_IDENTITY_UNRESOLVED
                )
            if measured != request.expected_identity:
                return RetireApplicationResult(
                    state=RETIRE_RESULT_BLOCKED, reason=REASON_IDENTITY_CHANGED
                )

        checkout_in_scope = request.intent not in (
            RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
            RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
        )
        worktree_dirty_override = None
        worktree_missing = False
        if request.worktree and checkout_in_scope:
            try:
                worktree_missing = not Path(request.worktree).expanduser().is_dir()
            except OSError:
                worktree_missing = False
            worktree_dirty_override = LiveSublaneGitOperations(
                repo_root=Path(request.worktree)
            ).worktree_dirty()

        outcome = SublaneRetireUseCase(
            LiveSublaneLifecycleOps(repo_root=request.repo_root)
        ).run(
            issue=request.issue,
            lane_label=request.lane_label,
            worktree_path=request.worktree,
            branch=request.branch,
            integration_branch=request.integration_branch,
            assertions=request.assertions,
            worktree_dirty_override=worktree_dirty_override,
            worktree_missing=worktree_missing,
            checkout_in_scope=checkout_in_scope,
        )
        if not outcome.preflight.may_retire:
            return RetireApplicationResult(
                state=RETIRE_RESULT_BLOCKED,
                reason=str(outcome.preflight.decision.primary_reason or "retire_preflight_blocked"),
                preflight=outcome,
            )
        if request.intent == RETIRE_INTENT_PREFLIGHT:
            return RetireApplicationResult(
                state=RETIRE_RESULT_DEFERRED,
                reason=REASON_PREFLIGHT_ONLY,
                preflight=outcome,
            )

        intents = dispatch_retire_intent(
            args,
            request.repo_root,
            may_retire=True,
            worktree=request.worktree,
            evidence_target=target,
        )
        verdict = intents.actuated
        if verdict is None:
            return RetireApplicationResult(
                # Preserve the historical CLI contract for an intent outside the repository's
                # backend while making the programmatic result explicitly non-retired.  The
                # supervisor therefore performs no cleanup and retries only after fresh facts.
                state=RETIRE_RESULT_DEFERRED,
                reason=REASON_INTENT_NOT_APPLICABLE,
                preflight=outcome,
                intents=intents,
            )
        if not bool(getattr(verdict, "ok", False)):
            return RetireApplicationResult(
                state=RETIRE_RESULT_BLOCKED,
                reason=str(getattr(verdict, "reason", "") or "retire_intent_blocked"),
                preflight=outcome,
                intents=intents,
            )
        already = _is_already_state(verdict)
        return RetireApplicationResult(
            state=(RETIRE_RESULT_ALREADY_RETIRED if already else RETIRE_RESULT_RETIRED),
            mutated=not already,
            preflight=outcome,
            intents=intents,
        )
    except Exception:  # noqa: BLE001 - an exception may be after a side effect
        return RetireApplicationResult(
            state=RETIRE_RESULT_UNCERTAIN,
            reason=REASON_APPLICATION_ERROR,
            uncertain=True,
        )


__all__ = (
    "RetireApplicationRequest",
    "RetireApplicationResult",
    "RetireAssertions",
    "RetireIdentity",
    "RETIRE_INTENT_PREFLIGHT",
    "RETIRE_INTENT_EXECUTE",
    "RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY",
    "RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE",
    "RETIRE_INTENT_HIBERNATED_BOUND",
    "RETIRE_INTENT_ACTIVE_LIVE_ZERO",
    "RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO",
    "RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO",
    "RETIRE_RESULT_RETIRED",
    "RETIRE_RESULT_BLOCKED",
    "RETIRE_RESULT_DEFERRED",
    "RETIRE_RESULT_UNCERTAIN",
    "RETIRE_RESULT_ALREADY_RETIRED",
    "REASON_CLEANUP_ATOMIC_GUARD_UNAVAILABLE",
    "REASON_INTENT_NOT_APPLICABLE",
    "run_retire_application",
)
