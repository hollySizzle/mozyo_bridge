"""`mozyo-bridge sublane` lifecycle command boundary (Redmine #12955).

Wires the pure #12955 lifecycle core
(:mod:`...domain.sublane_lifecycle`) to the runtime through an injected port,
mirroring the established OOP-first command boundary (#12933 ``launch_command`` /
#12604 ``sublane_integration``): the use cases hold the decision flow and never touch
IO; the :class:`SublaneLifecycleOps` port owns every side effect (tmux pane inventory,
git probes); a typed ``*Outcome`` carries the render payload; and the thin ``cmd_*``
handlers own stdout and the exit code.

Three subcommands, matching the issue scope:

- ``sublane list`` / ``sublane status`` — read-only: fold the live tmux pane inventory
  into one row per sublane (issue / worktree / gateway pane / worker pane / branch /
  state). Pure discovery; exits 0.
- ``sublane create`` / ``sublane start`` — resolve the operator-supplied identity, probe
  git for the launch action (the pure #12604 :func:`decide_worktree_launch`), and emit a
  fail-closed, replayable :class:`SublaneCreatePlan`. It **plans**; it does not actuate
  ``git worktree add`` / pane creation / dispatch. Exits non-zero on a blocked plan.
- ``sublane retire`` — evaluate the pure #12604 retire decision from git probes + the
  durable-record invariants the operator asserts as flags, and emit the fail-closed
  preflight verdict + durable journal + retirement runbook. It **preflights**; the
  destructive actuator (pane kill / ``git worktree remove`` / local branch delete) is
  gated behind a Design Consultation per ``vibes/docs/logics/worktree-lifecycle-boundary.md``
  and is never run here. Exits non-zero when retirement is blocked.

Boundary: this command surface is discovery / planning / safety only. It never removes a
worktree, kills a pane, deletes a branch (local or remote), or attempts a merge; it never
self-authorizes a close / carve-out / owner decision. Remote branch deletion is never
even emitted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LaunchPreflight,
    RetirePreflight,
    SublaneIntegrationPolicy,
    decide_retire_integration,
    decide_worktree_launch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    CREATE_BLOCKED,
    SublaneCreatePlan,
    SublaneCreateRequest,
    SublaneLaneView,
    SublaneRetirePreflight,
    plan_sublane_create,
    preflight_sublane_retire,
    project_sublanes,
)


# ---------------------------------------------------------------------------
# Injected operations port.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneLifecycleOps(Protocol):
    """Every side effect the lifecycle use cases need, injected so tests drive fakes.

    ``pane_rows`` is the read-only tmux pane inventory (the ``pane_lines`` row dicts;
    an empty list when tmux is unavailable — the surface degrades, it does not die).
    ``is_git_workspace`` / ``worktree_exists`` / ``worktree_dirty`` are the git read
    probes for the current repo root. ``branch_for`` resolves the current branch of a
    checkout path (``None`` when it is not a resolvable git worktree). There is
    intentionally no create / remove / merge / pane-kill method — the actuating half of
    the lifecycle is gated (worktree-lifecycle-boundary.md).
    """

    def pane_rows(self) -> list[dict[str, str]]: ...

    def is_git_workspace(self) -> bool: ...

    def worktree_exists(self, branch: str) -> bool: ...

    def worktree_dirty(self) -> bool: ...

    def branch_for(self, checkout_path: str) -> Optional[str]: ...


@dataclass(frozen=True)
class LiveSublaneLifecycleOps:
    """Live adapter: tmux pane inventory + subprocess git probes for ``repo_root``."""

    repo_root: Path

    def _git(self) -> LiveSublaneGitOperations:
        return LiveSublaneGitOperations(repo_root=self.repo_root)

    def pane_rows(self) -> list[dict[str, str]]:
        # Imported lazily so the pure use cases / tests never require the tmux
        # infrastructure module.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
            try_pane_lines,
        )

        rows = try_pane_lines()
        return rows if rows is not None else []

    def is_git_workspace(self) -> bool:
        return self._git().is_git_workspace()

    def worktree_exists(self, branch: str) -> bool:
        return self._git().worktree_exists(branch)

    def worktree_dirty(self) -> bool:
        return self._git().worktree_dirty()

    def branch_for(self, checkout_path: str) -> Optional[str]:
        if not checkout_path:
            return None
        try:
            result = subprocess.run(
                ["git", "-C", checkout_path, "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                capture_output=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        # A detached HEAD prints ``HEAD``; report that verbatim so it is not confused
        # with a named branch.
        return branch or None


# ---------------------------------------------------------------------------
# Durable-record invariants the operator asserts for a retire (flag-driven).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetireAssertions:
    """The config-undisableable retire invariants, asserted from the durable record.

    These mirror the #12604 :class:`RetireInvariants`: facts no probe can infer, supplied
    by the coordinator from the Redmine issue / journal state. Every default is the
    unsatisfied (safe-failing) value, so a caller that omits a flag fails closed.
    """

    issue_closed: bool = False
    owner_approval_present: bool = False
    callbacks_drained: bool = False
    verification_passed: bool = False
    durable_record_recorded: bool = False
    target_identity_known: bool = False


# ---------------------------------------------------------------------------
# Outcomes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SublaneListOutcome:
    lanes: tuple[SublaneLaneView, ...]

    def as_payload(self) -> dict[str, Any]:
        return {"sublanes": [lane.as_payload() for lane in self.lanes]}


@dataclass(frozen=True)
class SublaneCreateOutcome:
    plan: SublaneCreatePlan

    def as_payload(self) -> dict[str, Any]:
        return self.plan.as_payload()


@dataclass(frozen=True)
class SublaneRetireOutcome:
    preflight: SublaneRetirePreflight

    def as_payload(self) -> dict[str, Any]:
        return self.preflight.as_payload()


# ---------------------------------------------------------------------------
# Use cases.
# ---------------------------------------------------------------------------


@dataclass
class SublaneListUseCase:
    """Read-only ``list`` / ``status`` projection over the :class:`SublaneLifecycleOps`."""

    ops: SublaneLifecycleOps

    def run(self, *, lane_filter: Optional[str] = None) -> SublaneListOutcome:
        rows = self.ops.pane_rows()
        # First pass discovers the lanes (and their repo roots); resolve each lane's
        # branch through the port, then re-project with the branch lookup.
        base = project_sublanes(rows)
        branches: dict[str, str] = {}
        for view in base:
            if view.repo_root:
                branch = self.ops.branch_for(view.repo_root)
                if branch:
                    branches[view.lane_id] = branch
        lanes = project_sublanes(rows, branches=branches)
        if lane_filter:
            needle = lane_filter.strip()
            lanes = [
                lane
                for lane in lanes
                if needle in (lane.lane_id, lane.lane_label)
                or needle == lane.issue
            ]
        return SublaneListOutcome(lanes=tuple(lanes))


@dataclass
class SublaneCreateUseCase:
    """``create`` / ``start`` planning over the port (no actuation).

    Probes git for the launch action via the pure #12604
    :func:`decide_worktree_launch` — exactly the preflight
    :meth:`SublaneIntegrationUseCase.plan_launch` builds, **minus** the ``create_worktree``
    side effect — then composes the fail-closed :class:`SublaneCreatePlan`.
    """

    ops: SublaneLifecycleOps
    policy: SublaneIntegrationPolicy = SublaneIntegrationPolicy.default()

    def run(self, request: SublaneCreateRequest) -> SublaneCreateOutcome:
        # A missing identity field short-circuits before any git probe (fail-closed).
        if request.missing_fields():
            plan = plan_sublane_create(
                request,
                decide_worktree_launch(
                    self.policy, LaunchPreflight(is_git_workspace=False)
                ),
            )
            return SublaneCreateOutcome(plan=plan)
        is_git = self.ops.is_git_workspace()
        identity_known = bool(request.branch) and bool(request.worktree_path)
        worktree_exists = (
            self.ops.worktree_exists(request.branch)
            if is_git and identity_known
            else False
        )
        preflight = LaunchPreflight(
            is_git_workspace=is_git,
            worktree_exists=worktree_exists,
            branch_resolved=bool(request.branch),
            target_identity_known=identity_known,
        )
        decision = decide_worktree_launch(self.policy, preflight)
        return SublaneCreateOutcome(plan=plan_sublane_create(request, decision))


@dataclass
class SublaneRetireUseCase:
    """``retire`` fail-closed preflight over the port (no destructive actuation).

    Evaluates the pure #12604 :func:`decide_retire_integration` from git probes + the
    operator-asserted durable-record invariants, with ``merge_on_retire=False`` (retire
    cleans up an already-integrated lane; it never attempts a merge here), then renders
    the preflight verdict + journal + retirement runbook.
    """

    ops: SublaneLifecycleOps

    def run(
        self,
        *,
        issue: str,
        lane_label: str,
        worktree_path: Optional[str],
        branch: Optional[str],
        integration_branch: Optional[str],
        assertions: RetireAssertions,
    ) -> SublaneRetireOutcome:
        is_git = self.ops.is_git_workspace()
        worktree_dirty = self.ops.worktree_dirty() if is_git else False
        policy = SublaneIntegrationPolicy(
            manage_worktree=True,
            integration_branch=integration_branch,
            merge_on_retire=False,
        )
        preflight = RetirePreflight(
            is_git_workspace=is_git,
            worktree_dirty=worktree_dirty,
            integration_branch_resolved=True,
            merge_conflict=False,
            target_identity_known=assertions.target_identity_known,
            verification_passed=assertions.verification_passed,
            issue_closed=assertions.issue_closed,
            owner_approval_present=assertions.owner_approval_present,
            callbacks_drained=assertions.callbacks_drained,
            durable_record_recorded=assertions.durable_record_recorded,
        )
        decision = decide_retire_integration(policy, preflight)
        result = preflight_sublane_retire(
            decision,
            issue=issue,
            lane_label=lane_label,
            worktree_path=worktree_path,
            branch=branch,
            integration_branch=integration_branch,
            is_git_workspace=is_git,
        )
        return SublaneRetireOutcome(preflight=result)


# ---------------------------------------------------------------------------
# Text rendering (pure).
# ---------------------------------------------------------------------------


def format_list_text(outcome: SublaneListOutcome) -> str:
    if not outcome.lanes:
        return "sublanes: none"
    lines = [f"sublanes: {len(outcome.lanes)}"]
    for lane in outcome.lanes:
        lines.append(
            f"  {lane.lane_label or lane.lane_id} [{lane.state}]"
            f" issue={lane.issue or '-'} branch={lane.branch or '-'}"
        )
        lines.append(
            f"    gateway={lane.gateway_pane or '-'} worker={lane.worker_pane or '-'}"
            f" worktree={lane.repo_root or '-'}"
        )
    return "\n".join(lines)


def format_create_text(outcome: SublaneCreateOutcome) -> str:
    plan = outcome.plan
    lines = [f"sublane create: {plan.status}", f"  reason: {plan.reason}"]
    if plan.launch_action:
        lines.append(f"  launch_action: {plan.launch_action}")
    if plan.is_blocked:
        for reason in plan.blocked_reasons:
            lines.append(f"  -> blocked: {reason}")
        return "\n".join(lines)
    lines.append("  plan (not actuated):")
    for step in plan.steps:
        lines.append(f"    {step.order}. {step.title}: {step.detail}")
        if step.command:
            lines.append(f"       $ {step.command}")
    return "\n".join(lines)


def format_retire_text(outcome: SublaneRetireOutcome) -> str:
    pre = outcome.preflight
    decision = pre.decision
    lines = [
        f"sublane retire: {decision.state} (may_retire={decision.may_retire})",
    ]
    if decision.is_blocked:
        lines.append(f"  primary_reason: {decision.primary_reason}")
        lines.append("  blocked_reasons: " + ", ".join(decision.blocked_reasons))
        lines.append("  -> fail-closed: lane NOT retired; call the coordinator back")
    else:
        lines.append("  runbook (coordinator-executed by hand; not actuated here):")
        for step in pre.runbook:
            lines.append(f"    {step.order}. {step.title}: {step.detail}")
            if step.command:
                lines.append(f"       $ {step.command}")
    lines.append("  durable journal:")
    for jline in pre.journal.splitlines():
        lines.append(f"    {jline}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thin CLI handlers.
# ---------------------------------------------------------------------------


def _repo_root(args: argparse.Namespace) -> Path:
    repo = getattr(args, "repo", None)
    return Path(repo).expanduser() if repo else Path.cwd()


def cmd_sublane_list(args: argparse.Namespace) -> int:
    use_case = SublaneListUseCase(LiveSublaneLifecycleOps(repo_root=_repo_root(args)))
    outcome = use_case.run(lane_filter=getattr(args, "lane", None))
    if getattr(args, "json", False):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_list_text(outcome))
    return 0


def cmd_sublane_create(args: argparse.Namespace) -> int:
    request = SublaneCreateRequest(
        issue=getattr(args, "issue", "") or "",
        lane_label=getattr(args, "lane_label", "") or "",
        branch=getattr(args, "branch", "") or "",
        worktree_path=getattr(args, "worktree", "") or "",
        journal=getattr(args, "journal", None),
        upstream_coordinator=getattr(args, "upstream_coordinator", None),
    )
    use_case = SublaneCreateUseCase(LiveSublaneLifecycleOps(repo_root=_repo_root(args)))
    outcome = use_case.run(request)
    if getattr(args, "json", False):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_create_text(outcome))
    return 1 if outcome.plan.status == CREATE_BLOCKED else 0


def cmd_sublane_retire(args: argparse.Namespace) -> int:
    assertions = RetireAssertions(
        issue_closed=bool(getattr(args, "issue_closed", False)),
        owner_approval_present=bool(getattr(args, "owner_approved", False)),
        callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
        verification_passed=bool(getattr(args, "verified", False)),
        durable_record_recorded=bool(getattr(args, "durable_record", False)),
        target_identity_known=bool(getattr(args, "target_identity_known", False)),
    )
    use_case = SublaneRetireUseCase(LiveSublaneLifecycleOps(repo_root=_repo_root(args)))
    outcome = use_case.run(
        issue=getattr(args, "issue", "") or "",
        lane_label=getattr(args, "lane_label", "") or "",
        worktree_path=getattr(args, "worktree", None),
        branch=getattr(args, "branch", None),
        integration_branch=getattr(args, "integration_branch", None),
        assertions=assertions,
    )
    if getattr(args, "json", False):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_retire_text(outcome))
    return 0 if outcome.preflight.may_retire else 1


__all__ = (
    "SublaneLifecycleOps",
    "LiveSublaneLifecycleOps",
    "RetireAssertions",
    "SublaneListOutcome",
    "SublaneCreateOutcome",
    "SublaneRetireOutcome",
    "SublaneListUseCase",
    "SublaneCreateUseCase",
    "SublaneRetireUseCase",
    "format_list_text",
    "format_create_text",
    "format_retire_text",
    "cmd_sublane_list",
    "cmd_sublane_create",
    "cmd_sublane_retire",
)
