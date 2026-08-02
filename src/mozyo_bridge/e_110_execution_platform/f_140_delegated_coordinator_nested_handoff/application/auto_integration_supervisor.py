"""Bounded supervisor owner for durable auto-integration continuations (#14825).

The CLI forms and registers the exact action frame.  This leg is the long-lived owner that the
R2 review found missing: on a normal workspace-supervisor sweep it discovers registered actions,
recovers a crashed admission, runs an action that died before reaching its asynchronous gate, and
polls the required workflow until the same action can be continued.  It never derives identity
from a pane or branch name; the frame is read from the append-only ledger and its workspace is
checked against the canonical workspace registry before composition.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    LaneLifecycleError,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    declared_worktree_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    GhCliCiStatusReader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    CONTINUATION_CI_FAILED,
    CONTINUATION_INTEGRATED,
    CiSettlementTrigger,
    LaneBinding,
    build_auto_integration_use_case,
    load_committed_repo_local_config,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    ACTION_AWAITING_CI,
    ACTION_CI_FAILED,
    ACTION_INTEGRATED,
    ACTION_REGISTERED,
    AutoIntegrationLedgerError,
    DurableIntegrationAction,
    SqliteLedgerStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_reconcile import (  # noqa: E501
    RECONCILED_AMBIGUOUS,
    RECONCILED_STORE_REFUSED,
    RECONCILED_UNKNOWN_STEP,
    StrandedActionReconciler,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (  # noqa: E501
    OUTCOME_DONE,
    STEP_PUSH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    completed_steps,
)


SUPERVISION_DEFERRED = "deferred"
SUPERVISION_BLOCKED = "blocked"
SUPERVISION_AWAITING_CI = "awaiting_ci"
SUPERVISION_INTEGRATED = "integrated"
SUPERVISION_CI_FAILED = "ci_failed"
SUPERVISION_REFUSED = "refused"
SUPERVISION_ERROR = "error"


@dataclass(frozen=True)
class ActionSupervisionOutcome:
    action_key: str
    status: str
    detail: str = ""
    mutated: bool = False
    uncertain: bool = False


@dataclass(frozen=True)
class AutoIntegrationSupervisionOutcome:
    workspace: str
    issue: str
    actions: tuple[ActionSupervisionOutcome, ...] = ()

    @property
    def mutated(self) -> bool:
        return any(item.mutated for item in self.actions)

    @property
    def uncertain(self) -> bool:
        return any(item.uncertain for item in self.actions)


WorkspaceRootReader = Callable[[str], Optional[Path]]


def _default_ci_reader(repo_root: Path):
    return GhCliCiStatusReader(repo_root=repo_root)


def _git_common_dir(repo_root: Path) -> Optional[Path]:
    """Return Git's canonical common directory without accepting an unreadable root."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


@dataclass(frozen=True)
class AutoIntegrationSupervisorLeg:
    """One bounded, idempotent pass over one workspace/issue continuation partition."""

    workspace_root_fn: WorkspaceRootReader
    callback_outbox: object
    lifecycle_store: LaneLifecycleStore
    home: Optional[Path] = None
    environ: Optional[Mapping[str, str]] = None
    compose_fn: Optional[Callable[[DurableIntegrationAction, Path], object]] = None
    ci_reader_fn: Callable[[Path], object] = _default_ci_reader

    def __call__(
        self, workspace: str, issue: str, _source: object = None
    ) -> AutoIntegrationSupervisionOutcome:
        reader = SqliteLedgerStore(home=self.home)
        try:
            actions = reader.resumable_actions(workspace=workspace, issue=issue)
        except AutoIntegrationLedgerError as exc:
            return AutoIntegrationSupervisionOutcome(
                workspace=workspace,
                issue=issue,
                actions=(
                    ActionSupervisionOutcome("", SUPERVISION_ERROR, str(exc)),
                ),
            )
        if not actions:
            return AutoIntegrationSupervisionOutcome(workspace=workspace, issue=issue)

        root = self.workspace_root_fn(workspace)
        if root is None:
            return AutoIntegrationSupervisionOutcome(
                workspace=workspace,
                issue=issue,
                actions=tuple(
                    ActionSupervisionOutcome(
                        action.action_key,
                        SUPERVISION_REFUSED,
                        "the workspace registry did not resolve exactly one canonical repo root",
                    )
                    for action in actions
                ),
            )
        canonical_root = Path(root).resolve()

        outcomes = []
        mutation_spent = False
        for action in actions:
            if mutation_spent and action.state == ACTION_REGISTERED:
                outcomes.append(
                    ActionSupervisionOutcome(
                        action.action_key,
                        SUPERVISION_DEFERRED,
                        "this bounded leg already performed its one external mutation",
                    )
                )
                continue
            outcome = self._supervise_action(action, canonical_root)
            outcomes.append(outcome)
            mutation_spent = mutation_spent or outcome.mutated or outcome.uncertain
        return AutoIntegrationSupervisionOutcome(
            workspace=workspace, issue=issue, actions=tuple(outcomes)
        )

    def _supervise_action(
        self, action: DurableIntegrationAction, canonical_root: Path
    ) -> ActionSupervisionOutcome:
        execution_root, root_refusal = self._execution_root(action, canonical_root)
        if execution_root is None:
            return ActionSupervisionOutcome(
                action.action_key,
                SUPERVISION_REFUSED,
                root_refusal,
            )
        problems = action.validation_errors()
        if problems:
            return ActionSupervisionOutcome(
                action.action_key,
                SUPERVISION_REFUSED,
                "invalid durable action frame: " + "; ".join(problems),
            )

        try:
            if self.compose_fn is not None:
                use_case = self.compose_fn(action, execution_root)
            else:
                config = load_committed_repo_local_config(execution_root)
                use_case = build_auto_integration_use_case(
                    binding=LaneBinding(
                        issue=action.issue,
                        workspace=action.workspace,
                        lane=action.lane,
                        lane_generation=action.lane_generation,
                        branch=action.branch,
                        worktree=action.worktree,
                    ),
                    config=config.auto_integration,
                    repo_root=execution_root,
                    lifecycle_store=self.lifecycle_store,
                    callback_outbox=self.callback_outbox,
                    admission_record=action.record,
                    home=self.home,
                    environ=dict(self.environ if self.environ is not None else os.environ),
                )
            return self._drive(use_case, action)
        except Exception as exc:  # noqa: BLE001 - one action never aborts a supervisor sweep
            uncertain = self._push_intent_open(action.action_key)
            return ActionSupervisionOutcome(
                action.action_key,
                SUPERVISION_ERROR,
                f"the continuation pass failed ({exc.__class__.__name__})",
                uncertain=uncertain,
            )

    def _execution_root(
        self, action: DurableIntegrationAction, canonical_root: Path
    ) -> tuple[Optional[Path], str]:
        """Resolve the only repository root this durable action may execute in.

        A default-lane action runs in the workspace registry's canonical root.  A standard
        sublane instead runs in a linked worktree while deliberately inheriting that same
        workspace id.  The latter is admitted only when three independent authorities agree:
        the immutable action frame names its own worktree, the current lifecycle row binds that
        exact path token to the same issue/lane/generation, and Git says both paths share one
        common repository.  A caller-selected sibling path therefore never becomes authority.
        """
        try:
            recorded_root = Path(action.repo_root).resolve()
            recorded_worktree = Path(action.worktree).resolve()
        except (OSError, RuntimeError):
            return None, "the registered repo root could not be resolved canonically"
        if recorded_root == canonical_root:
            return recorded_root, ""
        if recorded_root != recorded_worktree:
            return None, "the registered repo root does not equal the action's worktree root"

        try:
            rows = self.lifecycle_store.records()
        except (LaneLifecycleError, OSError):
            return None, "the lifecycle authority for the registered worktree is unreadable"
        matches = [
            row
            for row in rows
            if str(row.repo_workspace_id) == str(action.workspace)
            and str(row.lane_id) == str(action.lane)
            and str(row.issue_id) == str(action.issue)
            and int(row.lane_generation) == int(action.lane_generation)
            and str(row.lane_disposition) == DISPOSITION_ACTIVE
        ]
        if len(matches) != 1:
            return None, (
                "the registered worktree does not have one current active lifecycle binding "
                "for this exact workspace/lane/issue/generation"
            )
        expected_identity = declared_worktree_identity(
            str(recorded_root), str(action.lane)
        )
        if not expected_identity or str(matches[0].worktree_identity) != expected_identity:
            return None, "the registered worktree does not match its durable lifecycle identity"
        recorded_common = _git_common_dir(recorded_root)
        canonical_common = _git_common_dir(canonical_root)
        if (
            recorded_common is None
            or canonical_common is None
            or recorded_common != canonical_common
        ):
            return None, (
                "the registered worktree and workspace canonical root do not share one "
                "readable Git common directory"
            )
        return recorded_root, ""

    def _drive(self, use_case, action: DurableIntegrationAction) -> ActionSupervisionOutcome:
        record = action.record
        mutated = False
        current = action

        if current.state == ACTION_REGISTERED:
            intents = use_case.ledger.unresolved_intents(action_key=record.action_key)
            if intents:
                recovery = StrandedActionReconciler(use_case=use_case).reconcile(record)
                if recovery.status in (
                    RECONCILED_AMBIGUOUS,
                    RECONCILED_STORE_REFUSED,
                    RECONCILED_UNKNOWN_STEP,
                ):
                    return ActionSupervisionOutcome(
                        action.action_key, SUPERVISION_BLOCKED, recovery.detail
                    )

            landed = self._landed(use_case, action)
            if landed is None:
                report = use_case.run_integration(record)
                mutated = any(
                    row.step == STEP_PUSH and row.outcome == OUTCOME_DONE
                    for row in report.outcomes
                )
                landed = self._landed(use_case, action)
                if landed is None:
                    state = report.final_decision.state if report.final_decision else ""
                    return ActionSupervisionOutcome(
                        action.action_key,
                        SUPERVISION_BLOCKED,
                        f"the action stopped before an accepted push ({state or 'unknown'})",
                        mutated=mutated,
                    )

            workflow = self._required_workflow(use_case, record)
            if not workflow:
                raise AutoIntegrationLedgerError(
                    "the durable source-CI marker does not name the workflow to supervise"
                )
            use_case.mark_action_awaiting_ci(
                action_key=record.action_key,
                landed_head=landed.head,
                ci_workflow=workflow,
            )
            current = use_case.ledger.action(record.action_key)

        if current is None or current.state != ACTION_AWAITING_CI:
            return ActionSupervisionOutcome(
                action.action_key,
                SUPERVISION_REFUSED,
                "the action is not in a resumable continuation state",
                mutated=mutated,
            )

        outcome = CiSettlementTrigger(
            use_case=use_case,
            ci_reader=self.ci_reader_fn(canonical_path(use_case)),
        ).settle(
            record,
            workflow=current.ci_workflow,
            branch=current.target_ref,
        )
        if outcome.status == CONTINUATION_CI_FAILED:
            use_case.mark_action_terminal(
                action_key=record.action_key,
                state=ACTION_CI_FAILED,
                landed_head=outcome.landed_head,
                detail=outcome.detail,
            )
            return ActionSupervisionOutcome(
                action.action_key, SUPERVISION_CI_FAILED, outcome.detail, mutated=mutated
            )
        if outcome.status == CONTINUATION_INTEGRATED:
            use_case.mark_action_terminal(
                action_key=record.action_key,
                state=ACTION_INTEGRATED,
                landed_head=outcome.landed_head,
                detail=outcome.detail,
            )
            return ActionSupervisionOutcome(
                action.action_key, SUPERVISION_INTEGRATED, outcome.detail, mutated=mutated
            )
        return ActionSupervisionOutcome(
            action.action_key, SUPERVISION_AWAITING_CI, outcome.detail, mutated=mutated
        )

    @staticmethod
    def _landed(use_case, action: DurableIntegrationAction):
        return completed_steps(
            use_case.ledger.read(action_key=action.action_key),
            action_key=action.action_key,
            recorded_by=use_case.recorder_id,
        ).get(STEP_PUSH)

    @staticmethod
    def _required_workflow(use_case, record) -> str:
        reader = getattr(use_case.authority, "required_ci_workflow", None)
        return str(reader(record=record) or "").strip() if reader is not None else ""

    def _push_intent_open(self, action_key: str) -> bool:
        try:
            return any(
                intent.step == STEP_PUSH
                for intent in SqliteLedgerStore(home=self.home).unresolved_intents(
                    action_key=action_key
                )
            )
        except AutoIntegrationLedgerError:
            return True


def canonical_path(use_case) -> Path:
    """The live Git adapter's already-validated repository root."""
    return Path(getattr(use_case.operations, "repo_root"))


def build_auto_integration_supervisor_leg(
    *,
    home: Optional[Path],
    callback_outbox: object,
    lifecycle_store: LaneLifecycleStore,
    workspaces_fn: Callable[[], Sequence[object]],
) -> AutoIntegrationSupervisorLeg:
    """Production wiring over the same registry/outbox/lifecycle stores as the supervisor."""

    def workspace_root(workspace_id: str) -> Optional[Path]:
        matches = [
            Path(str(getattr(item, "canonical_path", "")))
            for item in workspaces_fn()
            if str(getattr(item, "workspace_id", "") or "") == str(workspace_id)
            and str(getattr(item, "canonical_path", "") or "").strip()
        ]
        return matches[0] if len(matches) == 1 else None

    return AutoIntegrationSupervisorLeg(
        workspace_root_fn=workspace_root,
        callback_outbox=callback_outbox,
        lifecycle_store=lifecycle_store,
        home=home,
    )


__all__ = (
    "ActionSupervisionOutcome",
    "AutoIntegrationSupervisionOutcome",
    "AutoIntegrationSupervisorLeg",
    "SUPERVISION_AWAITING_CI",
    "SUPERVISION_BLOCKED",
    "SUPERVISION_CI_FAILED",
    "SUPERVISION_DEFERRED",
    "SUPERVISION_ERROR",
    "SUPERVISION_INTEGRATED",
    "SUPERVISION_REFUSED",
    "build_auto_integration_supervisor_leg",
)
