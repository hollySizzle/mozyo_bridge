"""Delegated-coordinator lane launch/adopt decision core (Redmine #12447).

US #12437 wants a parent project (e.g. ``gk-3500-it-operations``) to start or
explicitly adopt a *visible* child ``delegated_coordinator`` lane in the canonical
project (``giken-3800-mozyo-bridge``), instead of treating a route smoke to a
pre-existing Codex pane as completion (#12439 j#63505 / #12437 j#63530 scope
correction). The existing ``handoff delegate-coordinator`` route (Redmine #12438)
resolves a canonical Codex gateway and sends a delegated handoff, but it
*silently selects* whatever unique Codex pane already lives in the canonical repo
— so "an existing lane answered" reads as PASS even when no fresh child lane was
launched and no explicit adoption was recorded.

This module is the pure, fail-closed decision core that closes that gap. It does
no tmux / git / filesystem I/O of its own (mozyo-bridge core is **not** a git
worktree manager — ``coordinator-sublane-development-flow.md``: worktree add /
remove is plain git, the visible launch is an operator/cockpit action). The
caller resolves the canonical target (``project_router.resolve_delegation_target``),
discovers candidate panes, and checks whether the canonical repo root is present
locally; this module decides **launch vs adopt vs fail-closed** and produces a
replayable durable record plus the delegation-reference projection.

Three pure pieces:

- :func:`decide_delegation_lane` requires an *explicit* ``launch`` / ``adopt``
  decision and never auto-reuses an existing lane as PASS (``lane_decision_required``
  when the decision is omitted). ``adopt`` resolves the unique existing canonical
  Codex lane (or an explicit operator-named pane) and fails closed on absent /
  ambiguous targets. ``launch`` requires the canonical repo root to be present
  locally and a replayable lane identity (child issue + branch/worktree), and
  never selects an existing lane. Returns a :class:`DelegationLaneDecision`.
- :func:`build_delegation_lane_record` projects the decision onto a replayable
  durable record (the launch/adopt selection, target issue, parent issue, target
  project, lane/worktree identity, callback route, no-hidden-subagent guarantee)
  that a coordinator pastes into the Redmine journal so the route replays.
- :func:`build_delegation_display_record` projects the decision onto the
  ``delegation_display_record`` schema from
  ``vibes/docs/logics/delegated-coordinator-cockpit-display.md`` (lane_kind /
  delegation_root / delegation_parent / delegation_depth / retire_owner /
  source_refs) so cockpit / ``agents targets`` can show the parent → child
  relationship as a *derived projection* (the governance truth stays the Redmine
  parent link + dispatch journal; the projection is re-derivable, never the source
  of routing identity).

Safety invariants this core preserves (it weakens none):

- No hidden subagent: the decision always names a *visible* lane (an existing
  pane for adopt, or an operator-materialized lane identity for launch). The
  record carries ``no_hidden_subagent = True`` as an explicit marker.
- No cross-project Claude direct send: this module decides the lane only; the
  actual handoff stays a Codex-gateway send (the command wires
  ``role_profile=delegated_coordinator`` and ``receiver=codex``).
- Owner approval / parent close authority stay with the parent coordinator: the
  delegation reference records ``delegation_parent`` / ``delegation_root`` and the
  delegated lane's retire owner as the parent, never granting the child close
  authority (``delegated-coordinator-role-profile.md`` fixed invariants).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.domain.project_router import (
    CODE_AMBIGUOUS_TARGET,
    CODE_NO_TARGET,
    DelegationTarget,
    ProjectRouterError,
    select_delegation_codex_pane,
)
from mozyo_bridge.domain.role_profile import ROLE_DELEGATED_COORDINATOR


class DelegationLaneError(ValueError):
    """A launch/adopt lane decision could not be resolved (fail-closed).

    Carries a stable :attr:`code` so the command layer can map the fail-closed
    reason onto a structured-outcome ``reason`` without string matching, mirroring
    :class:`mozyo_bridge.domain.project_router.ProjectRouterError`.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# Lane decision modes (the explicit operator choice).
LANE_LAUNCH = "launch"
LANE_ADOPT = "adopt"
LANE_MODES = (LANE_LAUNCH, LANE_ADOPT)

# Fail-closed reason codes (see :class:`DelegationLaneError.code`).
CODE_DECISION_REQUIRED = "lane_decision_required"
CODE_ADOPT_NO_EXISTING_LANE = "adopt_no_existing_lane"
CODE_ADOPT_TARGET_AMBIGUOUS = "adopt_target_ambiguous"
CODE_LAUNCH_ROOT_ABSENT = "launch_canonical_root_absent"
CODE_LAUNCH_IDENTITY_INCOMPLETE = "launch_identity_incomplete"

# The delegated coordinator sits one hop below the parent coordinator (root 0).
# ``delegated-coordinator-cockpit-display.md``: depth 0 root / 1 delegated /
# 2 grandchild. This core only produces the depth-1 delegated coordinator lane.
DELEGATED_COORDINATOR_DEPTH = 1


@dataclass(frozen=True)
class DelegationLaneDecision:
    """A resolved launch/adopt decision for a delegated coordinator lane (#12447).

    ``mode`` is the explicit operator choice (:data:`LANE_LAUNCH` /
    :data:`LANE_ADOPT`). For ``adopt``, ``adopt_target`` is the visible existing
    Codex pane being adopted as the child delegated coordinator. For ``launch``,
    ``child_issue`` + ``branch`` / ``worktree`` form the replayable identity of the
    lane the operator will materialize (this core never spawns it).

    ``delegation_root`` / ``delegation_parent`` are display / audit breadcrumbs
    (the parent coordinator's unit pointer), never routing identity. ``lane_kind``
    is fixed to ``delegated_coordinator`` and ``no_hidden_subagent`` is always
    ``True`` — the decision always names a visible lane.
    """

    mode: str
    lane_kind: str
    target_project: str
    canonical_repo_root: str
    child_project: str
    redmine_project: Optional[str]
    parent_project: Optional[str]
    parent_issue: Optional[str]
    parent_callback_target: Optional[str]
    delegation_root: Optional[str]
    delegation_parent: Optional[str]
    delegation_depth: int
    adopt_target: Optional[str]
    child_issue: Optional[str]
    branch: Optional[str]
    worktree: Optional[str]
    lane_id: Optional[str]
    no_hidden_subagent: bool = True

    @property
    def callback_route(self) -> Optional[str]:
        """Where the delegated coordinator returns handoff-worthy state.

        Alias for ``parent_callback_target`` — the callback route is the parent
        coordinator route; the delegated lane never aggregates owner approval or
        closes the parent issue itself.
        """
        return self.parent_callback_target

    @property
    def retire_owner(self) -> Optional[str]:
        """The unit responsible for retiring this delegated lane.

        Per ``delegated-coordinator-cockpit-display.md`` the delegated
        coordinator's retire owner is its parent coordinator (grandchildren are
        owned by the delegated coordinator). Falls back to ``parent_project`` when
        no explicit parent unit pointer was supplied.
        """
        return self.delegation_parent or self.delegation_root or self.parent_project


def _clean(value: Optional[str]) -> Optional[str]:
    """Return a stripped non-empty string, else ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_parent_pointer(
    *,
    explicit: Optional[str],
    parent_project: Optional[str],
    parent_issue: Optional[str],
) -> Optional[str]:
    """Derive a parent-coordinator unit pointer for the delegation breadcrumb.

    Prefers an explicit pointer; otherwise composes a stable, redaction-safe
    pointer from the parent project id and issue (e.g. ``gk-3500#12437``). Returns
    ``None`` when nothing identifies the parent, so the projection reports it as
    unresolved rather than fabricating one.
    """
    explicit_clean = _clean(explicit)
    if explicit_clean:
        return explicit_clean
    project = _clean(parent_project)
    issue = _clean(parent_issue)
    if project and issue:
        return f"{project}#{issue}"
    return project or (f"#{issue}" if issue else None)


def decide_delegation_lane(
    target: DelegationTarget,
    *,
    mode: Optional[str],
    candidates: Iterable[object] = (),
    explicit_adopt_target: Optional[str] = None,
    canonical_root_present: bool = False,
    child_issue: Optional[str] = None,
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
    lane_id: Optional[str] = None,
    parent_project: Optional[str] = None,
    parent_issue: Optional[str] = None,
    parent_callback_target: Optional[str] = None,
    delegation_root: Optional[str] = None,
    delegation_parent: Optional[str] = None,
) -> DelegationLaneDecision:
    """Decide whether to launch a fresh delegated lane or adopt an existing one.

    ``target`` is the resolved external-submodule delegation target
    (:func:`mozyo_bridge.domain.project_router.resolve_delegation_target`).
    ``mode`` is the explicit operator decision — there is **no auto mode**: an
    omitted decision fails closed (:data:`CODE_DECISION_REQUIRED`) so a
    pre-existing lane is never silently reused as PASS (#12437 j#63530).

    ``adopt`` resolves the visible existing canonical Codex lane: an explicit
    operator-named ``explicit_adopt_target`` wins, otherwise the unique
    unambiguous canonical Codex pane is selected from ``candidates`` via
    :func:`select_delegation_codex_pane`. Fails closed when none exist
    (:data:`CODE_ADOPT_NO_EXISTING_LANE` — launch instead) or more than one is
    usable (:data:`CODE_ADOPT_TARGET_AMBIGUOUS` — name the pane).

    ``launch`` produces a replayable lane identity for a *fresh* lane and never
    consults existing candidates. It fails closed when the canonical repo root is
    not present locally (:data:`CODE_LAUNCH_ROOT_ABSENT` — cannot materialize a
    lane where the repo is not checked out; adopt an existing loaded lane instead)
    or when the lane identity is incomplete (:data:`CODE_LAUNCH_IDENTITY_INCOMPLETE`
    — a launch needs at least a child issue and a branch or worktree to be
    auditable). The actual worktree / pane creation is an operator/cockpit action,
    verified live in the separate test issue.

    Pure and deterministic: no tmux / git / filesystem access.
    """
    decision_mode = _clean(mode)
    if decision_mode not in LANE_MODES:
        existing = sorted(
            getattr(c, "pane_id", "?")
            for c in candidates
            if getattr(c, "pane_id", None)
        )
        existing_note = (
            f" (existing canonical Codex pane(s) {existing} are NOT auto-adopted)"
            if existing
            else ""
        )
        raise DelegationLaneError(
            "an explicit lane decision is required: choose "
            f"{LANE_LAUNCH!r} (start a fresh visible delegated coordinator lane) "
            f"or {LANE_ADOPT!r} (explicitly adopt a named existing lane)"
            f"{existing_note}. A pre-existing lane route is not launch/adopt PASS.",
            code=CODE_DECISION_REQUIRED,
        )

    resolved_parent_project = _clean(parent_project) or _clean(target.parent_project)
    resolved_root_pointer = _resolve_parent_pointer(
        explicit=delegation_root,
        parent_project=resolved_parent_project,
        parent_issue=parent_issue,
    )
    # For a depth-1 delegated lane the direct parent IS the delegation root (the
    # parent coordinator). An explicit delegation_parent still wins if supplied.
    resolved_parent_pointer = (
        _clean(delegation_parent) or resolved_root_pointer
    )

    common = dict(
        lane_kind=ROLE_DELEGATED_COORDINATOR,
        target_project=target.target_project,
        canonical_repo_root=target.canonical_repo_root,
        child_project=target.child_project,
        redmine_project=target.redmine_project,
        parent_project=resolved_parent_project,
        parent_issue=_clean(parent_issue),
        parent_callback_target=_clean(parent_callback_target),
        delegation_root=resolved_root_pointer,
        delegation_parent=resolved_parent_pointer,
        delegation_depth=DELEGATED_COORDINATOR_DEPTH,
    )

    if decision_mode == LANE_ADOPT:
        adopt_target = _clean(explicit_adopt_target)
        adopted_lane_id = _clean(lane_id)
        if not adopt_target:
            try:
                chosen = select_delegation_codex_pane(
                    candidates, canonical_repo_root=target.canonical_repo_root
                )
            except ProjectRouterError as exc:
                if exc.code == CODE_NO_TARGET:
                    raise DelegationLaneError(
                        "adopt found no existing canonical Codex lane to adopt: "
                        f"{exc}. Launch a fresh lane with --lane launch (after "
                        "loading the canonical Unit), then retry, or name the "
                        "pane explicitly.",
                        code=CODE_ADOPT_NO_EXISTING_LANE,
                    ) from exc
                if exc.code == CODE_AMBIGUOUS_TARGET:
                    raise DelegationLaneError(
                        f"adopt is ambiguous: {exc}. Name the exact lane pane to "
                        "adopt.",
                        code=CODE_ADOPT_TARGET_AMBIGUOUS,
                    ) from exc
                raise
            adopt_target = getattr(chosen, "pane_id", None)
            adopted_lane_id = adopted_lane_id or _clean(
                getattr(chosen, "lane_id", None)
            )
        return DelegationLaneDecision(
            mode=LANE_ADOPT,
            adopt_target=adopt_target,
            child_issue=_clean(child_issue),
            branch=_clean(branch),
            worktree=_clean(worktree),
            lane_id=adopted_lane_id,
            **common,
        )

    # LANE_LAUNCH: never reuse an existing lane; require local root + identity.
    if not canonical_root_present:
        raise DelegationLaneError(
            f"cannot launch a fresh delegated coordinator lane: canonical repo "
            f"root {target.canonical_repo_root!r} is not present locally. Check "
            "out the canonical repository first, or use --lane adopt against an "
            "existing loaded lane.",
            code=CODE_LAUNCH_ROOT_ABSENT,
        )
    launch_child_issue = _clean(child_issue)
    launch_branch = _clean(branch)
    launch_worktree = _clean(worktree)
    if not launch_child_issue or not (launch_branch or launch_worktree):
        raise DelegationLaneError(
            "launch needs a replayable lane identity: --child-issue and at least "
            "one of --branch / --worktree so the fresh lane is auditable.",
            code=CODE_LAUNCH_IDENTITY_INCOMPLETE,
        )
    return DelegationLaneDecision(
        mode=LANE_LAUNCH,
        adopt_target=None,
        child_issue=launch_child_issue,
        branch=launch_branch,
        worktree=launch_worktree,
        lane_id=_clean(lane_id),
        **common,
    )


def build_delegation_lane_record(decision: DelegationLaneDecision) -> dict:
    """Project a decision onto a replayable durable record (Redmine journal).

    Carries every field the acceptance requires to replay the route from the
    durable record: the launch/adopt selection, target project, target / parent
    issue, canonical repo root, lane / worktree identity, callback route, the
    delegation breadcrumb, and the no-hidden-subagent guarantee. Optional identity
    fields are omitted when unset so the record stays readable, but the decision
    mode and boundary markers are always present.
    """
    record: dict = {
        "lane_decision": decision.mode,
        "lane_kind": decision.lane_kind,
        "target_project": decision.target_project,
        "canonical_repo_root": decision.canonical_repo_root,
        "child_project": decision.child_project,
        "delegation_depth": decision.delegation_depth,
        "no_hidden_subagent": decision.no_hidden_subagent,
    }
    optional = {
        "redmine_project": decision.redmine_project,
        "parent_project": decision.parent_project,
        "parent_issue": decision.parent_issue,
        "callback_route": decision.callback_route,
        "delegation_root": decision.delegation_root,
        "delegation_parent": decision.delegation_parent,
        "adopt_target": decision.adopt_target,
        "child_issue": decision.child_issue,
        "branch": decision.branch,
        "worktree": decision.worktree,
        "lane_id": decision.lane_id,
    }
    for key, value in optional.items():
        if value is not None:
            record[key] = value
    return record


def build_delegation_display_record(
    decision: DelegationLaneDecision,
    *,
    unit_id: str,
    source_refs: Iterable[str] = (),
) -> dict:
    """Project a decision onto the ``delegation_display_record`` schema.

    Matches ``vibes/docs/logics/delegated-coordinator-cockpit-display.md`` so
    cockpit / ``agents targets`` can render the parent → child relationship as a
    derived projection (``lane_kind`` / ``delegation_root`` / ``delegation_parent``
    / ``delegation_depth`` / ``retire_owner`` / ``source_refs``). This is a
    breadcrumb, not routing identity: the governance truth stays the Redmine
    parent link + dispatch journal and the record must be re-derivable from it.
    """
    return {
        "unit_id": unit_id,
        "lane_kind": decision.lane_kind,
        "delegation_root": decision.delegation_root,
        "delegation_parent": decision.delegation_parent,
        "delegation_depth": decision.delegation_depth,
        "retire_owner": decision.retire_owner,
        "source_refs": [ref for ref in source_refs if ref],
    }


__all__ = (
    "DelegationLaneError",
    "LANE_LAUNCH",
    "LANE_ADOPT",
    "LANE_MODES",
    "CODE_DECISION_REQUIRED",
    "CODE_ADOPT_NO_EXISTING_LANE",
    "CODE_ADOPT_TARGET_AMBIGUOUS",
    "CODE_LAUNCH_ROOT_ABSENT",
    "CODE_LAUNCH_IDENTITY_INCOMPLETE",
    "DELEGATED_COORDINATOR_DEPTH",
    "DelegationLaneDecision",
    "decide_delegation_lane",
    "build_delegation_lane_record",
    "build_delegation_display_record",
)
