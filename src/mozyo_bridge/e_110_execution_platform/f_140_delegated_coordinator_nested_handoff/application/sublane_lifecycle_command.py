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
  state / host window), plus the #13086 machine-readable stale / retire hints (pane
  missing / window split / duplicate issue lane / unresolved worktree, and — opt-in
  via ``--integration-branch`` — branch already integrated). Pure discovery and
  advisory diagnosis material; it never retires anything and exits 0.
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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

# Redmine #14539: the retire admissibility resolvers live in their own module — adding the
# second measured route (the review-exemption fence) pushed this one past the oversized-module
# gate. Re-exported here so the CLI import site and the #13518 tests are unchanged.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: F401
    _resolve_latest_generation_admissible,
    resolve_retire_evidence_target,
    _resolve_review_exemption_admissible,
    _resolve_review_generation_admissible,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (
    decide_create_launch,
    default_nongit_worktree_request,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E501
    RETIRE_INTENT_ACTIVE_LIVE_ZERO,
    RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
    RETIRE_INTENT_EXECUTE,
    RETIRE_INTENT_HIBERNATED_BOUND,
    RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
    RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY,
    RETIRE_INTENT_PREFLIGHT,
    RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE,
    RetireApplicationRequest,
    RetireAssertions,
    RetireIdentity,
    run_retire_application,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    RetirePreflight,
    SublaneIntegrationPolicy,
    decide_retire_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    CREATE_BLOCKED,
    SublaneCreatePlan,
    SublaneCreateRequest,
    SublaneLaneView,
    SublaneRetirePreflight,
    plan_sublane_create,
    portable_worktree_label,
    preflight_sublane_retire,
    project_sublanes,
    redact_worktree_paths,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (
    normalize_work_unit_granularity,
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
    checkout path (``None`` when it is not a resolvable git worktree).
    ``branch_integrated`` is the read-only #13086 retire-material probe: whether
    ``branch`` is already reachable from ``integration_branch`` (``None`` when the
    probe cannot answer — unknown never fabricates a hint). There is intentionally no
    create / remove / merge / pane-kill method — the actuating half of the lifecycle
    is gated (worktree-lifecycle-boundary.md).

    Optional capability (Redmine #13432, mirroring the #13392 actuator port): an adapter
    MAY additionally provide ``canonical_workspace_root() -> str`` — the workspace root the
    command runs in. :class:`SublaneCreateUseCase` reads it (via ``getattr`` through the
    shared :func:`resolve_lane_runtime_root`) to default a non-git lane's omitted
    ``--worktree`` to the workspace root (the lane runtime root a directory-scaffold lane
    collapses to). Discovered via ``getattr`` and deliberately NOT part of this protocol so
    existing adapters / test fakes that only drive the Git path stay conformant (they fall
    back to leaving the omitted worktree blank, which a non-git plan does not require).
    """

    def pane_rows(self) -> list[dict[str, str]]: ...

    def is_git_workspace(self) -> bool: ...

    def worktree_exists(self, branch: str) -> bool: ...

    def worktree_dirty(self) -> bool: ...

    def branch_for(self, checkout_path: str) -> Optional[str]: ...

    def branch_integrated(
        self, branch: str, integration_branch: str
    ) -> Optional[bool]: ...


@dataclass(frozen=True)
class LiveSublaneLifecycleOps:
    """Live adapter: tmux pane inventory + subprocess git probes for ``repo_root``."""

    repo_root: Path

    def _git(self) -> LiveSublaneGitOperations:
        return LiveSublaneGitOperations(repo_root=self.repo_root)

    def canonical_workspace_root(self) -> str:
        # #13432 (mirrors the #13392 actuator adapter): the workspace root the command runs
        # in — the lane runtime root of a non-git (skip_no_git) lane, which has no worktree
        # and runs here, so an omitted `--worktree` defaults to it.
        return str(self.repo_root)

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

    def branch_integrated(
        self, branch: str, integration_branch: str
    ) -> Optional[bool]:
        """Read-only ancestry probe: is ``branch`` reachable from ``integration_branch``?

        ``git merge-base --is-ancestor`` answers with its exit code: 0 = ancestor
        (integrated), 1 = not an ancestor. Any other exit (unknown ref, not a
        repo) or an OS error is *unknown* -> ``None``, so a failed probe never
        fabricates retire material.
        """
        if not branch or not integration_branch:
            return None
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "merge-base",
                    "--is-ancestor",
                    branch,
                    integration_branch,
                ],
                text=True,
                capture_output=True,
            )
        except OSError:
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None


# ---------------------------------------------------------------------------
# Durable-record invariants the operator asserts for a retire (flag-driven).
# ---------------------------------------------------------------------------


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
    """Read-only ``list`` / ``status`` projection over the :class:`SublaneLifecycleOps`.

    Besides the two-pass branch lookup, the second pass feeds the pure projection
    the #13086 stale / retire lookups: a lane whose recorded worktree resolves to
    no branch is ``worktree_unresolved``, and — only when the caller names an
    ``integration_branch`` (opt-in; never guessed) — a lane whose branch the
    read-only ancestry probe reports as reachable is ``branch_integrated``.
    Advisory diagnosis output only; nothing here retires, kills, or routes.
    """

    ops: SublaneLifecycleOps

    def run(
        self,
        *,
        lane_filter: Optional[str] = None,
        integration_branch: Optional[str] = None,
    ) -> SublaneListOutcome:
        rows = self.ops.pane_rows()
        # First pass discovers the lanes (and their repo roots); resolve each lane's
        # branch through the port, then re-project with the resolved lookups.
        base = project_sublanes(rows)
        branches: dict[str, str] = {}
        unresolved_worktrees: set[str] = set()
        integrated_branches: dict[str, str] = {}
        for view in base:
            if not view.repo_root:
                continue
            branch = self.ops.branch_for(view.repo_root)
            if branch:
                branches[view.lane_id] = branch
                if integration_branch and self.ops.branch_integrated(
                    branch, integration_branch
                ):
                    integrated_branches[view.lane_id] = integration_branch
            else:
                # A recorded worktree that resolves to no branch is stale retire
                # material (removed / moved / never created).
                unresolved_worktrees.add(view.lane_id)
        lanes = project_sublanes(
            rows,
            branches=branches,
            unresolved_worktrees=unresolved_worktrees,
            integrated_branches=integrated_branches,
        )
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
        # #13432: in a non-git (directory-scaffold) workspace the lane has no worktree —
        # `--branch` / `--worktree` are optional there — and an omitted `--worktree`
        # defaults to the workspace root (the #13392 論点1 lane runtime root), so the plan /
        # dispatch carry the root the lane actually runs in. A Git workspace keeps the full
        # identity requirement, so a missing field still fails closed in
        # plan_sublane_create (byte-invariant contract). The probed git-ness is passed
        # explicitly to plan_sublane_create so the identity relaxation tracks the real
        # workspace, not the launch-action token — an operator `manage_worktree: false`
        # opt-out collapses the launch action to LAUNCH_SKIP_DISABLED before the non-git
        # branch, and inferring git-ness from that token would wrongly re-require the Git
        # identity and diverge from the actuator's resolve_create_identity path (Review
        # #13432 j#74285 finding 1). The shared decide_create_launch re-probes git for the
        # launch action.
        is_git = self.ops.is_git_workspace()
        request = default_nongit_worktree_request(self.ops, request, is_git)
        decision = decide_create_launch(self.ops, request, self.policy)
        return SublaneCreateOutcome(
            plan=plan_sublane_create(request, decision, is_git=is_git)
        )


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
        worktree_dirty_override: Optional[bool] = None,
        worktree_missing: bool = False,
        checkout_in_scope: bool = True,
    ) -> SublaneRetireOutcome:
        is_git = self.ops.is_git_workspace()
        # Redmine #13331 review j#73338 (blocking): the retire TARGET is the lane worktree
        # (`--worktree`), not the repo the command runs in. The injected `ops` is bound to
        # the command's repo_root, so `ops.worktree_dirty()` inspects the coordinator repo
        # — a clean coordinator would let a DIRTY lane worktree pass `may_retire` and (under
        # the #13331 `--execute` guarded close) get its managed agents closed. When the
        # caller supplies the target worktree's own dirty state, it is authoritative here
        # (fail-closed: an uninspectable target resolves to dirty upstream). Absent an
        # override the behaviour is byte-for-byte the prior repo_root-bound probe.
        #
        # Redmine #14499 j#89291: a **metadata-only** intent has NO checkout in scope, so
        # neither probe applies to it. Live acceptance on #14482 measured all three ways the
        # coupling went wrong for the unbound terminal retire — whose CLI contract already
        # says `--worktree` is optional and used only to widen a legacy inventory scan:
        #   - `--worktree` omitted  -> this `else` branch probed the PRIMARY repo root and
        #     blocked on unrelated user-owned dirtiness (`dirty_worktree`);
        #   - the recorded (wiped) worktree supplied -> `worktree_missing_after_reboot`;
        #   - an unrelated clean worktree supplied -> `retire_ok`, but for the wrong reason.
        # None of the three describes the lane being terminalized. Not measuring is the
        # correct answer, not a relaxation: there is no checkout whose state could bear on a
        # lifecycle-metadata write. Every other intent keeps both probes exactly as before.
        if not checkout_in_scope:
            worktree_dirty = False
        elif worktree_dirty_override is not None:
            worktree_dirty = worktree_dirty_override
        else:
            worktree_dirty = self.ops.worktree_dirty() if is_git else False
        policy = SublaneIntegrationPolicy(
            manage_worktree=True,
            integration_branch=integration_branch,
            merge_on_retire=False,
        )
        preflight = RetirePreflight(
            is_git_workspace=is_git,
            worktree_dirty=worktree_dirty,
            # Redmine #14499: a positively-measured missing worktree. The dirty probe above
            # already fails closed on it, so this only sharpens the reported diagnosis from
            # "go commit your changes" to "the checkout is gone" — the blocked/permitted
            # verdict is unchanged either way.
            worktree_missing=worktree_missing and checkout_in_scope,
            integration_branch_resolved=True,
            merge_conflict=False,
            target_identity_known=assertions.target_identity_known,
            verification_passed=assertions.verification_passed,
            issue_closed=assertions.issue_closed,
            callbacks_drained=assertions.callbacks_drained,
            durable_record_recorded=assertions.durable_record_recorded,
            latest_generation_admissible=assertions.latest_generation_admissible,
            latest_generation_blocked_reason=assertions.latest_generation_blocked_reason,
        )
        decision = decide_retire_integration(policy, preflight)
        result = preflight_sublane_retire(
            decision,
            issue=issue,
            lane_label=lane_label,
            # A metadata-only intent contributes no checkout and no branch to the runbook:
            # passing them would let it propose `git worktree remove` against a worktree it
            # does not own and `git branch -d` against a branch it must preserve (#14499
            # j#89291 case 3, measured).
            worktree_path=worktree_path if checkout_in_scope else None,
            branch=branch if checkout_in_scope else None,
            integration_branch=integration_branch,
            is_git_workspace=is_git,
            checkout_cleanup_in_scope=checkout_in_scope,
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
            # #13368: `sublane list` rows are pasted into durable records; show the
            # portable lane worktree sibling basename, not the host-local absolute path
            # (the absolute repo_root stays in the `--json` payload / local state).
            f" worktree={portable_worktree_label(lane.repo_root)}"
        )
        # Host window identity (#13086): one address when the lane is intact, the
        # full split list when its panes span windows (no guessing a host).
        if lane.host_window:
            name = f"({lane.host_window_name})" if lane.host_window_name else ""
            lines.append(f"    window={lane.host_window}{name}")
        elif lane.windows:
            lines.append("    windows=" + ",".join(lane.windows) + " [split]")
        if lane.stale_hints:
            lines.append("    stale_hints: " + ", ".join(lane.stale_hints))
    return "\n".join(lines)


def format_create_text(
    outcome: SublaneCreateOutcome, worktree_path: Optional[str] = None
) -> str:
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
    # #13368: the plan text is pasteable; redact the host-local absolute worktree path
    # from the replayable `git worktree add` / `cockpit append --repo` command lines to
    # its portable sibling basename (the exact command stays in the `--json` payload).
    return redact_worktree_paths("\n".join(lines), worktree_path)


def format_retire_text(
    outcome: SublaneRetireOutcome, worktree_path: Optional[str] = None
) -> str:
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
    # #13368: the retire runbook is pasteable; redact the host-local absolute worktree
    # path from the replayable `git worktree remove` command to its portable sibling
    # basename (the exact command stays in the `--json` payload for local replay).
    return redact_worktree_paths("\n".join(lines), worktree_path)


# ---------------------------------------------------------------------------
# Thin CLI handlers.
# ---------------------------------------------------------------------------


def _repo_root(args: argparse.Namespace) -> Path:
    repo = getattr(args, "repo", None)
    return Path(repo).expanduser() if repo else Path.cwd()


def resolve_work_unit_request_fields(
    args: argparse.Namespace, repo_root: Path
) -> tuple[str, Optional[str]]:
    """Resolve the #13002 work-unit granularity + decision anchor for a create.

    Precedence: an explicit ``--work-unit`` flag wins; otherwise the repo-local
    ``.mozyo-bridge/config.yaml`` ``work_unit.granularity`` (a missing / absent
    block is the ``user_story`` default). A present-but-broken config raises
    ``RepoLocalConfigError`` — the caller fails closed instead of silently
    dispatching with the default unit. The decision anchor
    (``--work-unit-decision-journal``) passes through verbatim; whether it is
    required is the pure :func:`decide_work_unit_dispatch` gate's call.
    """
    anchor = getattr(args, "work_unit_decision_journal", None)
    explicit = getattr(args, "work_unit", None)
    if explicit:
        return normalize_work_unit_granularity(explicit), anchor
    # Imported lazily so the pure use cases / tests never require the loader
    # (and its file IO) unless the config fallback is actually consulted.
    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config,
    )

    config = load_repo_local_config(repo_root)
    return config.work_unit.granularity, anchor


def _build_create_request(
    args: argparse.Namespace, *, work_unit: str, work_unit_decision_anchor: Optional[str]
) -> SublaneCreateRequest:
    """Assemble the create request from CLI args + the resolved work unit (pure)."""
    return SublaneCreateRequest(
        issue=getattr(args, "issue", "") or "",
        lane_label=getattr(args, "lane_label", "") or "",
        branch=getattr(args, "branch", "") or "",
        worktree_path=getattr(args, "worktree", "") or "",
        journal=getattr(args, "journal", None),
        upstream_coordinator=getattr(args, "upstream_coordinator", None),
        work_unit=work_unit,
        work_unit_decision_anchor=work_unit_decision_anchor,
        leaf_standalone=bool(getattr(args, "leaf_standalone", False)),
        base_ref=getattr(args, "base_ref", None),
        # Redmine #13647 T1b: the caller-asserted delegation-geometry kind. Both request
        # construction sites (this plan-only surface and the actuating one in
        # `sublane_actuator`) must carry it, or a plan and its `--execute` would resolve
        # different placement authority (the j#84882 drift lesson).
        lane_kind=getattr(args, "lane_kind", "") or "",
    )


def cmd_sublane_list(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    # Redmine #13331: under backend: herdr a lane is its own herdr workspace, so project
    # the live herdr inventory into the SAME read model (the tmux path below is untouched —
    # byte-invariant). A broken / absent config resolves to the tmux path.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        herdr_sublane_views,
        repo_backend_is_herdr,
    )

    if repo_backend_is_herdr(repo_root):
        lanes = herdr_sublane_views(repo_root)
        lane_filter = (getattr(args, "lane", None) or "").strip()
        if lane_filter:
            lanes = tuple(
                lane
                for lane in lanes
                if lane_filter in (lane.lane_id, lane.lane_label)
                or lane_filter == lane.issue
            )
        outcome = SublaneListOutcome(lanes=lanes)
        if getattr(args, "json", False):
            print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("sublane list (backend: herdr)")
            print(format_list_text(outcome))
        return 0

    use_case = SublaneListUseCase(LiveSublaneLifecycleOps(repo_root=repo_root))
    outcome = use_case.run(
        lane_filter=getattr(args, "lane", None),
        integration_branch=getattr(args, "integration_branch", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_list_text(outcome))
    return 0


def cmd_sublane_create(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    # Fail closed on a present-but-broken repo-local config: never silently plan
    # with the default work unit when the operator's declared config is unreadable.
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        RepoLocalConfigError,
    )

    try:
        work_unit, decision_anchor = resolve_work_unit_request_fields(args, repo_root)
    except RepoLocalConfigError as exc:
        print(f"invalid repo-local config: {exc}", file=sys.stderr)
        return 1
    request = _build_create_request(
        args, work_unit=work_unit, work_unit_decision_anchor=decision_anchor
    )
    use_case = SublaneCreateUseCase(LiveSublaneLifecycleOps(repo_root=repo_root))
    outcome = use_case.run(request)
    if getattr(args, "json", False):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_create_text(outcome, worktree_path=request.worktree_path))
    return 1 if outcome.plan.status == CREATE_BLOCKED else 0


def cmd_sublane_retire(args: argparse.Namespace) -> int:
    # Redmine #13841 review j#79150 finding 3 + #13842 + #13845: --execute (guarded pane
    # close), --migrate-hibernated-legacy (metadata-only empty-binding live-zero migration),
    # --reconcile-hibernated-live (bounded live-pair reconcile), and --retire-hibernated-bound
    # (metadata-only bound live-zero terminal retire) are conflicting destructive intents.
    # Reject any combination as a zero-write failure rather than silently resolving to one —
    # the help promises they are mutually exclusive, and nothing (no preflight, no actuation)
    # runs here.
    _intents = [
        name
        for name, flag in (
            ("--execute", "execute"),
            ("--migrate-hibernated-legacy", "migrate_hibernated_legacy"),
            ("--reconcile-hibernated-live", "reconcile_hibernated_live"),
            ("--retire-hibernated-bound", "retire_hibernated_bound"),
            # Redmine #14242: the active live-zero terminal retire is a fifth distinct intent.
            ("--retire-active-live-zero", "retire_active_live_zero"),
            # Redmine #14499: the active UNBOUND live-zero terminal retire is a sixth.
            (
                "--retire-active-unbound-live-zero",
                "retire_active_unbound_live_zero",
            ),
            (
                "--retire-hibernated-unbound-live-zero",
                "retire_hibernated_unbound_live_zero",
            ),
        )
        if getattr(args, flag, False)
    ]
    if len(_intents) > 1:
        print(
            "error: " + ", ".join(_intents) + " are mutually exclusive "
            "(each is a distinct retire intent); pass exactly one",
            file=sys.stderr,
        )
        return 1
    # Resolve durable review admissibility before entering the shared typed facade.  The facade
    # re-resolves the lane identity at the action edge and compares it byte-for-byte with this
    # first snapshot, so CLI and supervisor now share the same preflight/intent fence (#15066).
    state_home = getattr(args, "home", None)
    evidence_target = resolve_retire_evidence_target(
        args, _repo_root(args), home=state_home
    )
    _generation_admissibility = _resolve_latest_generation_admissible(
        args, target=evidence_target, repo_root=_repo_root(args)
    )
    assertions = RetireAssertions(
        issue_closed=bool(getattr(args, "issue_closed", False)),
        callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
        verification_passed=bool(getattr(args, "verified", False)),
        durable_record_recorded=bool(getattr(args, "durable_record", False)),
        target_identity_known=bool(getattr(args, "target_identity_known", False)),
        # #13518 R3-F2: when a durable review observation is supplied, MEASURE latest-generation
        # admissibility at action-time via the review-generation fence (unreadable / malformed ->
        # fail-closed). Otherwise fall back to the operator's durable-record assertion. Absent both
        # the fence stays fail-closed (False), so the actual integration never default-admits.
        # Redmine #14539 review j#91797 finding 2: the exemption route's evidence identity is
        # MEASURED from the retire target's own lifecycle row, so the resolution has to happen
        # before the fence runs — the caller's argv is not an independent expectation. An
        # unresolvable target yields None, and the exemption route then refuses.
        # Redmine #14695: the no-change waiver route additionally needs the repo root, because its
        # durable evidence is read LIVE and its zero-change half is measured from real git — a
        # caller-supplied file cannot carry a negative claim (see that route's docstring).
        latest_generation_admissible=_generation_admissibility.admissible,
        # #14695 j#93807 F2: a route's own refusal reason reaches the operator instead of being
        # collapsed into ``stale_review_generation`` — which named a review generation that, for
        # the waiver route, cannot exist.
        latest_generation_blocked_reason=_generation_admissibility.reason,
    )
    repo_root = _repo_root(args)
    worktree = getattr(args, "worktree", None)
    selected_intent = RETIRE_INTENT_PREFLIGHT
    for enabled, token in (
        (getattr(args, "execute", False), RETIRE_INTENT_EXECUTE),
        (
            getattr(args, "migrate_hibernated_legacy", False),
            RETIRE_INTENT_MIGRATE_HIBERNATED_LEGACY,
        ),
        (
            getattr(args, "reconcile_hibernated_live", False),
            RETIRE_INTENT_RECONCILE_HIBERNATED_LIVE,
        ),
        (
            getattr(args, "retire_hibernated_bound", False),
            RETIRE_INTENT_HIBERNATED_BOUND,
        ),
        (
            getattr(args, "retire_active_live_zero", False),
            RETIRE_INTENT_ACTIVE_LIVE_ZERO,
        ),
        (
            getattr(args, "retire_active_unbound_live_zero", False),
            RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
        ),
        (
            getattr(args, "retire_hibernated_unbound_live_zero", False),
            RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
        ),
    ):
        if enabled:
            selected_intent = token
            break
    identity = (
        RetireIdentity(
            workspace=evidence_target.workspace,
            # Preserve the CLI's intent-specific issue/lane mismatch diagnosis. The
            # supervisor supplies its independently selected issue as the expectation;
            # the CLI pins the row it just measured and lets the existing intent rail
            # produce its established mismatch reason/output shape.
            issue=str(
                getattr(evidence_target, "issue", None)
                or getattr(args, "issue", "")
                or ""
            ),
            lane=evidence_target.lane,
            lane_generation=evidence_target.lane_generation,
            revision=evidence_target.revision,
        )
        if evidence_target is not None
        else None
    )
    application = run_retire_application(
        RetireApplicationRequest(
            repo_root=repo_root,
            home=state_home,
            issue=str(getattr(args, "issue", "") or ""),
            lane_label=str(getattr(args, "lane_label", "") or ""),
            assertions=assertions,
            intent=selected_intent,
            worktree=worktree,
            branch=getattr(args, "branch", None),
            integration_branch=getattr(args, "integration_branch", None),
            journal=getattr(args, "journal", None),
            expect_lane_generation=getattr(args, "expect_lane_generation", 0),
            expect_lane_revision=getattr(args, "expect_lane_revision", 0),
            integration_journal=getattr(args, "integration_journal", None),
            expected_identity=identity,
        )
    )
    outcome = application.preflight
    if outcome is None:
        payload = {
            "retire_application": application.as_payload(),
            "retire_ok": False,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"retire blocked: {application.reason}", file=sys.stderr)
        return 1
    if application.intents is None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_intents import (  # noqa: E501
            RetireIntentResults,
        )

        intents = RetireIntentResults()
    else:
        intents = application.intents
    close_result = intents.close_result
    migration_result = intents.migration_result
    reconcile_result = intents.reconcile_result
    bound_retire_result = intents.bound_retire_result
    active_retire_result = intents.active_retire_result
    unbound_retire_result = intents.unbound_retire_result
    hibernated_unbound_retire_result = intents.hibernated_unbound_retire_result
    payload = outcome.as_payload()
    payload["retire_application"] = application.as_payload()
    # Redmine #13754: the ACTUATION verdict — not the preflight — decides whether the lane
    # was actually retired. ``decision.state: retire_ok`` says the retire was *permitted*;
    # reading it as "the pair is gone" is exactly the #13748 j#77473 misread (empty close,
    # exit 0, pair still live). Under --execute / --migrate-hibernated-legacy the command's
    # success is the conjunction of the preflight AND the actuation / migration verdict,
    # surfaced as one unambiguous ``retire_ok`` and in the exit code.
    if close_result is not None:
        payload["herdr_retire_close"] = close_result.as_payload()
    if migration_result is not None:
        payload["hibernated_legacy_retire_migration"] = migration_result.as_payload()
    if reconcile_result is not None:
        payload["hibernated_live_reconcile"] = reconcile_result.as_payload()
    if bound_retire_result is not None:
        payload["hibernated_bound_retire"] = bound_retire_result.as_payload()
    if active_retire_result is not None:
        payload["active_live_zero_retire"] = active_retire_result.as_payload()
    if unbound_retire_result is not None:
        payload["active_unbound_live_zero_retire"] = unbound_retire_result.as_payload()
    if hibernated_unbound_retire_result is not None:
        payload["hibernated_unbound_live_zero_retire"] = (
            hibernated_unbound_retire_result.as_payload()
        )
    payload["retire_ok"] = bool(application.legacy_cli_ok)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_retire_text(outcome, worktree_path=worktree))
        if close_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E501
                format_retire_close_text,
            )

            print(format_retire_close_text(close_result))
        if migration_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_legacy_retire import (  # noqa: E501
                format_migration_text,
            )

            print(format_migration_text(migration_result))
        if reconcile_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile import (  # noqa: E501
                format_reconcile_text,
            )

            print(format_reconcile_text(reconcile_result))
        if bound_retire_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_retire import (  # noqa: E501
                format_bound_retire_text,
            )

            print(format_bound_retire_text(bound_retire_result))
        if active_retire_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_live_zero_retire import (  # noqa: E501
                format_active_retire_text,
            )

            print(format_active_retire_text(active_retire_result))
        if unbound_retire_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
                format_unbound_retire_text,
            )

            print(format_unbound_retire_text(unbound_retire_result))
        if hibernated_unbound_retire_result is not None:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_unbound_live_zero_retire import (  # noqa: E501
                format_hibernated_unbound_retire_text,
            )

            print(
                format_hibernated_unbound_retire_text(
                    hibernated_unbound_retire_result
                )
            )
    return 0 if application.legacy_cli_ok else 1


__all__ = (
    "SublaneLifecycleOps",
    "LiveSublaneLifecycleOps",
    "RetireAssertions",
    "resolve_work_unit_request_fields",
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
