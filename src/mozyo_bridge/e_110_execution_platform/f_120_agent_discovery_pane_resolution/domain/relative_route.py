"""Current-Unit relative delegation route + cockpit-visible startup evidence (Redmine #12699).

GK3500 exploratory smoke #12698 surfaced two gaps the #12668 / #12708 gateway
resolvers do not close:

1. **Reading the slice as absolute roots.** ``grandparent`` was treated as a fixed
   department root, which makes a monorepo / subproject delegation *slice* hard to
   express. The Lane Registry in
   ``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`` is explicit that
   the canonical vocabulary is the **relative** ``grandparent`` / ``parent`` /
   ``child`` / ``grandchild`` — *"ある 4 階層 slice の grandparent が system 全体の
   root とは限らない"*. The runtime surface to say *"from the current Unit, resolve
   the next-step-down ``project_gateway`` / ``delegated_coordinator`` /
   ``implementation_worker``"* was weak.
2. **Detached startup mistaken for a route.** When the executor escaped to
   ``mozyo --repo ... --no-attach --json`` it created a normal tmux session but
   *not* a cockpit-visible project gateway / lane projection, then read that as if
   the grandparent -> parent transition had a green path. ``cockpit --json`` is a
   preview that does not mutate, and a detached ``--no-attach`` *normal* session is
   a ``normal_window``, not a cockpit Unit.

This module is the pure, fail-closed layer for both:

- :func:`resolve_relative_route` resolves the one-step-down delegation target from
  the **current Unit's role** (the caller is the anchor, never an absolute root),
  reusing #12708's :func:`resolve_launch_or_adopt` for the coordinator-class
  targets so no divergent identity model grows. It enforces single-step delegation:
  a grandparent cannot reach a grandchild worker directly (the doc's
  ``parent_sends_to_grandchild_directly`` / direct-send prohibition).
- :func:`classify_startup_evidence` separates a ``cockpit --json`` preview and a
  detached ``--no-attach`` normal session from a real cockpit-visible Unit, so a
  detached/preview startup can **never** be accepted as green-path route evidence
  (issue acceptance: *"detached normal session 作成だけでは successful route
  evidence として扱われない"*). The cockpit-visible signal is derived from the
  already-discovered candidate's ``view_kind`` (``cockpit_pane`` vs
  ``normal_window``) / a cockpit membership probe — never a copied ``%pane`` or an
  ``active`` pane.

Design invariants (carried from #12668 / #12708, never weakened):

- The current Unit is the **relative anchor**; positions are slices, not roots.
- The implementation worker is **dispatched against a Redmine anchor**, never
  launched-or-adopted as a cockpit gateway — so its relative route returns the
  anchor-required dispatch contract, not a launch command.
- A preview or a detached normal session is not green-path evidence; only a real
  cockpit-visible Unit is. Fail closed (``none``) when nothing is observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    TARGET_KIND_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    ACTION_ADOPT,
    TARGET_KIND_WORKER,
    GatewayLaneIdentity,
    LaunchOrAdoptDecision,
    resolve_launch_or_adopt,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)


class RelativeRouteError(ValueError):
    """A relative delegation route input is malformed (unknown / off-by-N caller).

    Fail-closed like the sibling gateway / transition-role domain errors: a caller
    role with no one-step-down delegation, or a request to skip a delegation step,
    raises rather than silently routing.
    """


# Relative delegation positions (the doc's canonical vocabulary). They are
# *relative* to the current Unit slice, never absolute system roots.
POSITION_GRANDPARENT = "grandparent"
POSITION_PARENT = "parent"
POSITION_CHILD = "child"
POSITION_GRANDCHILD = "grandchild"

# The two caller roles that have no builtin transition-role token of their own
# (#12706 only pinned the grandparent / project-gateway boundaries). The Lane
# Registry binds parent=project_gateway -> child=delegated_coordinator ->
# grandchild=implementation_worker, so these name the lower two lanes.
ROLE_DELEGATED_COORDINATOR = "delegated_coordinator"
ROLE_IMPLEMENTATION_WORKER = "implementation_worker"

# One-step-down delegation target bindings (the three the issue requires resolving
# from the current Unit). They are the *current bindings* of the parent / child /
# grandchild positions, not the abstract lane names.
TARGET_PROJECT_GATEWAY = "project_gateway"
TARGET_DELEGATED_COORDINATOR = "delegated_coordinator"
TARGET_IMPLEMENTATION_WORKER = "implementation_worker"


@dataclass(frozen=True)
class RelativeDelegationStep:
    """The contract for one one-step-down delegation edge in a 4-tier slice.

    Keyed by ``caller_role`` (the current Unit's role): from there the slice
    uniquely names the ``target_*`` it delegates to. ``anchor_required`` is whether
    a Redmine work anchor must exist *before* this delegation (false only for the
    grandparent -> gateway *consultation* hop; the doc's Redmine Work Item 作成境界).
    ``coordinator_class`` is true when the target is a project-scoped Codex
    coordinator (gateway or delegated coordinator) that can be launched-or-adopted
    as a cockpit Unit; the implementation worker is false — it is dispatched
    against an anchor, never launched as a gateway.
    """

    caller_position: str
    caller_role: str
    target_position: str
    target_binding: str
    target_role: str
    target_kind: str
    anchor_required: bool
    coordinator_class: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "caller_position": self.caller_position,
            "caller_role": self.caller_role,
            "target_position": self.target_position,
            "target_binding": self.target_binding,
            "target_role": self.target_role,
            "target_kind": self.target_kind,
            "anchor_required": self.anchor_required,
            "coordinator_class": self.coordinator_class,
        }


# The relative delegation slice, keyed by the current Unit's role. Mirrors the Lane
# Registry rows: grandparent -> parent(project_gateway), parent -> child(delegated
# coordinator), child -> grandchild(implementation_worker). A grandchild has no
# one-step-down delegation, so it is absent (its callback goes *up*).
RELATIVE_DELEGATION_STEPS: dict[str, RelativeDelegationStep] = {
    ROLE_GRANDPARENT_COORDINATOR: RelativeDelegationStep(
        caller_position=POSITION_GRANDPARENT,
        caller_role=ROLE_GRANDPARENT_COORDINATOR,
        target_position=POSITION_PARENT,
        target_binding=TARGET_PROJECT_GATEWAY,
        target_role=AGENT_KIND_CODEX,
        target_kind=TARGET_KIND_PROJECT_GATEWAY,
        anchor_required=False,
        coordinator_class=True,
    ),
    ROLE_PROJECT_GATEWAY: RelativeDelegationStep(
        caller_position=POSITION_PARENT,
        caller_role=ROLE_PROJECT_GATEWAY,
        target_position=POSITION_CHILD,
        target_binding=TARGET_DELEGATED_COORDINATOR,
        target_role=AGENT_KIND_CODEX,
        # Live discovery cannot tell a delegated coordinator from a project gateway
        # (both are a strong project-scoped Codex); the relative position is the
        # contract label, the live identity is the same coordinator-class kind.
        target_kind=TARGET_KIND_PROJECT_GATEWAY,
        anchor_required=True,
        coordinator_class=True,
    ),
    ROLE_DELEGATED_COORDINATOR: RelativeDelegationStep(
        caller_position=POSITION_CHILD,
        caller_role=ROLE_DELEGATED_COORDINATOR,
        target_position=POSITION_GRANDCHILD,
        target_binding=TARGET_IMPLEMENTATION_WORKER,
        target_role=AGENT_KIND_CLAUDE,
        target_kind=TARGET_KIND_WORKER,
        anchor_required=True,
        coordinator_class=False,
    ),
}

RELATIVE_CALLER_ROLES: tuple[str, ...] = tuple(RELATIVE_DELEGATION_STEPS.keys())


def resolve_relative_step(caller_role: str) -> RelativeDelegationStep:
    """Resolve the one-step-down delegation contract for ``caller_role`` (fail-closed).

    Raises :class:`RelativeRouteError` when the role has no one-step-down
    delegation — a grandchild ``implementation_worker`` (it callbacks up, never
    delegates down) or any unknown role — so a caller can never silently skip the
    slice or invent a delegation edge.
    """
    step = RELATIVE_DELEGATION_STEPS.get(caller_role)
    if step is None:
        raise RelativeRouteError(
            f"no one-step-down delegation for caller role {caller_role!r}; a "
            "relative route is anchored on the current Unit and delegates exactly "
            f"one step. Expected one of {list(RELATIVE_CALLER_ROLES)}."
        )
    return step


# ---------------------------------------------------------------------------
# Cockpit-visible startup evidence.
# ---------------------------------------------------------------------------

# How a project-gateway startup attempt manifested for route-evidence purposes.
# Only ``cockpit_visible`` is a green path; a preview proves nothing was created,
# and a detached normal session is not a cockpit Unit.
STARTUP_COCKPIT_VISIBLE = "cockpit_visible"
STARTUP_JSON_PREVIEW = "json_preview"
STARTUP_DETACHED_NO_ATTACH = "detached_no_attach"
STARTUP_NONE = "none"


@dataclass(frozen=True)
class StartupEvidence:
    """Whether an observed startup counts as cockpit-visible green-path evidence.

    ``mode`` is one of the ``STARTUP_*`` tokens. :attr:`is_green_path` is the single
    boolean a grandparent / coordinator reads to decide whether the
    grandparent -> parent transition actually realized a cockpit-visible Unit, or
    whether it only saw a preview / a detached normal session (neither of which is
    route evidence — #12699 acceptance).
    """

    mode: str
    detail: str = ""

    @property
    def is_green_path(self) -> bool:
        return self.mode == STARTUP_COCKPIT_VISIBLE

    def as_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "is_green_path": self.is_green_path,
            "detail": self.detail,
        }


def cockpit_visible_from_candidate(candidate: Optional[TargetCandidate]) -> bool:
    """True only when ``candidate`` is a cockpit pane (not a detached normal window).

    The already-discovered ``view_kind`` is the authority: a ``cockpit_pane`` is a
    cockpit-visible Unit; a ``normal_window`` is a detached / standalone session
    (the ``mozyo --no-attach`` escape), which is not a cockpit Unit. ``None`` (no
    candidate resolved) is not cockpit-visible.
    """
    return candidate is not None and candidate.view_kind == VIEW_KIND_COCKPIT_PANE


def classify_startup_evidence(
    *,
    preview_only: bool = False,
    cockpit_visible: bool = False,
    session_present: bool = False,
) -> StartupEvidence:
    """Classify a startup observation into green-path / not (pure, #12699).

    Precedence (first match wins), so a preview or a detached session can never be
    mistaken for a realized cockpit Unit:

    1. ``preview_only`` (a ``cockpit --json`` preview) -> :data:`STARTUP_JSON_PREVIEW`
       — a preview does not mutate, so it proves no startup regardless of what it
       reports.
    2. ``cockpit_visible`` -> :data:`STARTUP_COCKPIT_VISIBLE` — the only green path
       (a real cockpit Unit: a ``cockpit_pane`` candidate / a cockpit ``member``
       with both peer panes).
    3. ``session_present`` (a session/window exists but is not a cockpit Unit) ->
       :data:`STARTUP_DETACHED_NO_ATTACH` — a detached ``--no-attach`` normal
       session, explicitly **not** green-path route evidence.
    4. otherwise -> :data:`STARTUP_NONE` (nothing observed; fail closed).

    ``cockpit_visible`` is supplied by the caller from the discovered candidate's
    ``view_kind`` (:func:`cockpit_visible_from_candidate`) or a cockpit membership
    probe (``member`` and ``panes_present``); this function stays provider-agnostic.
    """
    if preview_only:
        return StartupEvidence(
            mode=STARTUP_JSON_PREVIEW,
            detail=(
                "cockpit --json is a preview and does not mutate; it is not "
                "evidence that a cockpit-visible gateway was started"
            ),
        )
    if cockpit_visible:
        return StartupEvidence(
            mode=STARTUP_COCKPIT_VISIBLE,
            detail="a cockpit-visible Unit is present (cockpit pane / member with peer panes)",
        )
    if session_present:
        return StartupEvidence(
            mode=STARTUP_DETACHED_NO_ATTACH,
            detail=(
                "a detached normal session exists but is not a cockpit Unit "
                "(view_kind=normal_window); --no-attach normal session is not "
                "green-path route evidence"
            ),
        )
    return StartupEvidence(
        mode=STARTUP_NONE,
        detail="no startup observed; fail closed (not a route)",
    )


# ---------------------------------------------------------------------------
# Relative route plan.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelativeRoutePlan:
    """The resolved one-step-down delegation plan from the current Unit (#12699).

    ``launch_or_adopt`` carries the #12708 decision for a coordinator-class target
    (adopt a live lane / launch one / blocked); it is ``None`` for the
    implementation worker, which is dispatched against a Redmine anchor rather than
    launched as a cockpit Unit. ``startup_evidence`` is whether what the route
    resolved to is cockpit-visible green-path evidence (relevant to the
    coordinator-class adopt outcome). ``next_action`` is the concrete next command
    guidance; ``anchor_required`` echoes the step contract.
    """

    step: RelativeDelegationStep
    repo_root: str
    project_scope: str
    launch_or_adopt: Optional[LaunchOrAdoptDecision]
    startup_evidence: StartupEvidence
    anchor_required: bool
    next_action: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the relative route resolved to a forward action.

        Coordinator-class: forward when the launch-or-adopt decision is adopt /
        launch (blocked is not ok). Worker: the route is always *expressible* (the
        anchor-gated dispatch contract), so ok is true; whether it may proceed is
        gated by :attr:`anchor_required`, surfaced separately.
        """
        if self.step.coordinator_class:
            return self.launch_or_adopt is not None and self.launch_or_adopt.ok
        return True

    @property
    def green_path(self) -> bool:
        """True only when the route resolved to a cockpit-visible Unit.

        Distinct from :attr:`ok`: adopting a *detached normal-window* coordinator
        is ok (a real lane) but is **not** a green path until a cockpit-visible
        Unit is brought up (#12699). Never green for the worker hop (anchor-gated).
        """
        return self.startup_evidence.is_green_path

    def as_payload(self) -> dict[str, object]:
        return {
            "step": self.step.as_payload(),
            "repo_root": self.repo_root,
            "project_scope": self.project_scope,
            "launch_or_adopt": (
                self.launch_or_adopt.as_payload()
                if self.launch_or_adopt is not None
                else None
            ),
            "startup_evidence": self.startup_evidence.as_payload(),
            "anchor_required": self.anchor_required,
            "next_action": self.next_action,
            "ok": self.ok,
            "green_path": self.green_path,
            "detail": self.detail,
        }


def _worker_next_action(repo_root: str, project_scope: str) -> str:
    """The anchor-gated dispatch guidance for the implementation-worker hop.

    A worker is never launched-or-adopted as a cockpit gateway; it runs only
    against a Redmine work anchor (ticketless rail explicitly cannot carry a worker
    dispatch — ``### Ticketless No-Anchor Callback Primitive``). So the next action
    names the standard Redmine-anchored ``handoff send``.
    """
    return (
        "ensure_redmine_anchor, then dispatch the worker on the standard rail: "
        "mozyo-bridge handoff send --to claude --kind implementation_request "
        f"--target-repo {repo_root} "
        "--source redmine --issue <id> --journal <id>  "
        "# worker is anchor-gated; never launched as a cockpit gateway"
    )


def resolve_relative_route(
    candidates: Iterable[TargetCandidate],
    *,
    caller_role: str,
    repo_root: str,
    project_scope: str,
    session: Optional[str] = None,
) -> RelativeRoutePlan:
    """Resolve the one-step-down delegation route from the current Unit (pure, #12699).

    The current Unit is the relative anchor: ``caller_role`` selects the slice's
    one-step-down :class:`RelativeDelegationStep`. For a coordinator-class target
    (``project_gateway`` / ``delegated_coordinator``) this reuses #12708's
    :func:`resolve_launch_or_adopt` over the live candidates and classifies the
    adopted lane's cockpit visibility as startup evidence. For the implementation
    worker it returns the anchor-gated dispatch contract (no launch-or-adopt).

    Fails closed via :class:`RelativeRouteError` when ``caller_role`` has no
    one-step-down delegation (so a grandparent can never reach a grandchild worker
    directly — the doc's direct-send prohibition).
    """
    step = resolve_relative_step(caller_role)
    candidates = list(candidates)

    if not step.coordinator_class:
        # Implementation worker: anchor-gated dispatch, never a cockpit launch.
        return RelativeRoutePlan(
            step=step,
            repo_root=repo_root,
            project_scope=project_scope,
            launch_or_adopt=None,
            startup_evidence=StartupEvidence(
                mode=STARTUP_NONE,
                detail=(
                    "the implementation worker is dispatched against a Redmine "
                    "anchor, not launched as a cockpit Unit"
                ),
            ),
            anchor_required=step.anchor_required,
            next_action=_worker_next_action(repo_root, project_scope),
            detail=(
                f"{step.caller_position} -> {step.target_position}: dispatch the "
                "implementation worker only after a Redmine anchor exists"
            ),
        )

    identity = GatewayLaneIdentity(
        project_scope=project_scope,
        project_label=project_scope,
        project_path="",
        repo_root=repo_root,
        target_kind=step.target_kind,
    )
    decision = resolve_launch_or_adopt(candidates, identity, session=session)

    if decision.action == ACTION_ADOPT:
        evidence = classify_startup_evidence(
            cockpit_visible=cockpit_visible_from_candidate(decision.adopted),
            session_present=decision.adopted is not None,
        )
        if evidence.is_green_path:
            next_action = (
                "adopt the live cockpit-visible lane and hand off through "
                "mozyo-bridge project-gateway handoff"
            )
            detail = (
                f"{step.caller_position} -> {step.target_position}: adopt the "
                f"cockpit-visible {step.target_binding} lane (resolved by identity)"
            )
        else:
            # A matching lane exists but it is a detached normal window: real lane,
            # but NOT cockpit-visible green-path evidence (#12699).
            next_action = (
                "the resolved lane is a detached normal window, not a cockpit "
                "Unit; bring it up as a cockpit-visible Unit before treating the "
                "route as green: "
                + (
                    decision.launch_command
                    or "cd <project workdir> && mozyo-bridge cockpit"
                )
            )
            detail = (
                f"{step.caller_position} -> {step.target_position}: a "
                f"{step.target_binding} lane matched but is not cockpit-visible "
                "(detached normal session is not green-path route evidence)"
            )
    elif decision.ok:  # ACTION_LAUNCH
        evidence = classify_startup_evidence()  # nothing live yet -> none
        next_action = (
            "launch a cockpit-visible Unit (NOT cockpit --json preview, NOT a "
            "detached --no-attach normal session): " + decision.launch_command
        )
        detail = (
            f"{step.caller_position} -> {step.target_position}: no live "
            f"{step.target_binding}; launch one as a cockpit-visible Unit"
        )
    else:  # ACTION_BLOCKED
        evidence = classify_startup_evidence()
        next_action = decision.detail
        detail = (
            f"{step.caller_position} -> {step.target_position}: blocked — "
            "disambiguate or complete the route before launch-or-adopt"
        )

    return RelativeRoutePlan(
        step=step,
        repo_root=repo_root,
        project_scope=project_scope,
        launch_or_adopt=decision,
        startup_evidence=evidence,
        anchor_required=step.anchor_required,
        next_action=next_action,
        detail=detail,
    )


__all__ = (
    "RelativeRouteError",
    "POSITION_GRANDPARENT",
    "POSITION_PARENT",
    "POSITION_CHILD",
    "POSITION_GRANDCHILD",
    "ROLE_DELEGATED_COORDINATOR",
    "ROLE_IMPLEMENTATION_WORKER",
    "TARGET_PROJECT_GATEWAY",
    "TARGET_DELEGATED_COORDINATOR",
    "TARGET_IMPLEMENTATION_WORKER",
    "RelativeDelegationStep",
    "RELATIVE_DELEGATION_STEPS",
    "RELATIVE_CALLER_ROLES",
    "resolve_relative_step",
    "STARTUP_COCKPIT_VISIBLE",
    "STARTUP_JSON_PREVIEW",
    "STARTUP_DETACHED_NO_ATTACH",
    "STARTUP_NONE",
    "StartupEvidence",
    "cockpit_visible_from_candidate",
    "classify_startup_evidence",
    "RelativeRoutePlan",
    "resolve_relative_route",
)
