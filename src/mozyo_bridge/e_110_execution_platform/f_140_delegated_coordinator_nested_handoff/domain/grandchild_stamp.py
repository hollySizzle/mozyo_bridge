"""Grandchild lane realization -> live delegation metadata stamp plan (Redmine #12473).

US #12454 split the parent -> delegated coordinator -> grandchild route into a
durable policy shape (#12456), the depth-1 launch/adopt primitive (#12457), the
depth-2 grandchild **dispatch decision** primitive (#12458), the projection read
model (#12465), and the ``agents targets`` display columns (#12466). #12460 then
found the missing seam: a grandchild dispatch *decision* (or a same-lane worker
handoff) does not by itself make ``agents targets`` show the grandchild lane with
``KIND=implementation`` / ``DEPTH=2`` / ``PARENT=<delegated coordinator lane>`` —
nothing **stamps** the live ``@mozyo_lane_kind`` / ``@mozyo_delegation_parent``
projection-cache options that the discovery read path
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display`) consumes
(``delegated-coordinator-cockpit-display.md`` ``### separate は actuator ではない``;
``delegation-policy-project-config.md`` follow-up #6).

This module is the **pure plan** half of that actuator: given the declared
delegation tree (the governance truth read from the Redmine issue parent link +
dispatch journal, never inferred from pane proximity) and which lane is the
realized grandchild, it

1. validates the declared chain through the closed #12465
   :func:`~mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_projection.derive_delegation_tree`
   foundation (fail-closed on a duplicate / unknown parent / cycle / a depth
   beyond the shallow-delegation maximum / an off-contract ``lane_kind``), and
2. asserts the realized grandchild lane derives to the acceptance shape — a
   ``LANE_KIND_IMPLEMENTATION`` lane at ``delegation_depth`` 2 — so a stamp that
   would project a half-formed tree fails closed instead, and
3. builds the pure ``tmux set-option -p`` argv plan that stamps the two options
   the discovery read path actually reads (``@mozyo_lane_kind`` and
   ``@mozyo_delegation_parent``; ``delegation_depth`` / ``delegation_root`` are
   *derived* from the parent chain by the read model, never read from a pane
   option, so they are deliberately not stamped).

Boundaries (the same non-authoritative contract the #12465/#12466 layer pins):

- **Pure, no I/O.** No tmux / filesystem / send. It returns argv tuples and a
  structured plan; the side-effecting writer (the CLI handler) executes them.
  The mechanics mirror :mod:`mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.application.attention_projection`, the
  established pane-option projection-cache precedent.
- **Display / audit breadcrumb, never routing authority.** The stamped options
  are a projection cache; cross-lane handoff stays bound to the live
  ``--target-repo`` preflight, and ``KIND`` / ``DEPTH`` / ``PARENT`` / window /
  session / proximity are never promoted into a send target.
- **Declared tree, never inferred.** The chain comes from the durable record so
  the realization is replayable; this module never reads tmux to guess who the
  parent is.
- **No hidden subagent.** A realized grandchild lane is a declared,
  durable-anchored, cockpit-visible lane; this module carries its
  :data:`REALIZATION_LAUNCH` / :data:`REALIZATION_ADOPT` provenance for the
  replayable realization record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_projection import (
    LANE_KIND_IMPLEMENTATION,
    OPTION_DELEGATION_PARENT,
    OPTION_LANE_KIND,
    DelegationProjection,
    DelegationProjectionError,
    DelegationSource,
    derive_delegation_tree,
)

#: A newly created grandchild worktree/lane/window (the caller performs the
#: worktree / cockpit creation; this plan stamps its live delegation metadata and
#: records the launch provenance).
REALIZATION_LAUNCH = "launch"
#: An existing visible lane explicitly adopted as the grandchild (the adoption
#: reason is recorded for replay).
REALIZATION_ADOPT = "adopt"
REALIZATIONS: frozenset[str] = frozenset({REALIZATION_LAUNCH, REALIZATION_ADOPT})

#: The depth a realized grandchild lane must occupy (parent 0 -> delegated 1 ->
#: grandchild 2). Mirrors :data:`delegation_projection.MAX_DELEGATION_DEPTH`; a
#: realized grandchild at any other depth fails the acceptance and is rejected.
GRANDCHILD_DEPTH = 2


class GrandchildStampError(ValueError):
    """A grandchild realization stamp plan could not be built (invalid tree / shape)."""


@dataclass(frozen=True)
class DeclaredLane:
    """One lane in the declared delegation chain, read from the durable record.

    ``unit_id`` is the per-lane projection pointer the read model keys on
    (``<workspace_id>/<lane_id>`` by the :mod:`delegation_display` convention).
    ``lane_kind`` is a :data:`delegation_projection.LANE_KINDS` display kind.
    ``delegation_parent`` is the ``unit_id`` of the direct parent lane, or
    ``None`` for the tree root. ``panes`` are the live tmux pane ids to stamp;
    an empty tuple means the lane is declared **for derivation only** (e.g. an
    ancestor already stamped, or out of stamping scope) and emits no write while
    still anchoring the parent chain so the grandchild's depth derives.
    """

    unit_id: str
    lane_kind: str
    delegation_parent: Optional[str] = None
    panes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GrandchildStampPlan:
    """The validated, pure stamp plan for a grandchild realization.

    ``commands`` are ``("set-option", "-p", "-t", pane, option, value)`` argv
    tuples (one ``@mozyo_lane_kind`` + one ``@mozyo_delegation_parent`` per
    declared pane, in a stable order); nothing is executed here. ``projections``
    is the derived tree (display / audit only), and the ``grandchild_*`` fields
    summarize the realized grandchild for the replayable realization record.
    """

    realization: str
    grandchild_unit: str
    grandchild_lane_kind: str
    grandchild_depth: int
    grandchild_parent: Optional[str]
    grandchild_root: str
    adopt_reason: Optional[str]
    commands: tuple[tuple[str, ...], ...]
    stamped_panes: tuple[str, ...]
    projections: tuple[DelegationProjection, ...]

    @property
    def is_launch(self) -> bool:
        return self.realization == REALIZATION_LAUNCH

    @property
    def is_adopt(self) -> bool:
        return self.realization == REALIZATION_ADOPT


def resolve_grandchild_stamp_plan(
    declared_lanes: Sequence[DeclaredLane],
    *,
    grandchild_unit: str,
    realization: str,
    adopt_reason: Optional[str] = None,
) -> GrandchildStampPlan:
    """Resolve a fail-closed grandchild-realization stamp plan.

    The plan is the composition of three gates, fail-closed at each step:

    1. **Realization shape.** ``realization`` must be in :data:`REALIZATIONS`;
       :data:`REALIZATION_ADOPT` requires a non-empty ``adopt_reason`` (the
       replayable "why this lane was adopted"), and :data:`REALIZATION_LAUNCH`
       must not carry one (a launch has no adoption to justify).
    2. **Tree validity.** The declared chain is run through the closed #12465
       :func:`derive_delegation_tree`, which fails closed on a duplicate
       ``unit_id``, an unknown ``lane_kind``, an unknown parent pointer, a cycle,
       or a depth beyond the shallow-delegation maximum. A
       :class:`DelegationProjectionError` is re-raised as a
       :class:`GrandchildStampError` so the caller has one error type.
    3. **Grandchild acceptance shape.** ``grandchild_unit`` must be present in
       the tree, be a :data:`LANE_KIND_IMPLEMENTATION` lane, derive to
       :data:`GRANDCHILD_DEPTH` (2), **and declare at least one live pane** —
       the #12473 acceptance (``KIND`` / ``DEPTH`` / ``PARENT`` on a *visible*
       grandchild lane). Ancestor lanes may be derivation-only (no panes), but
       the realized grandchild may not: a derivation-only grandchild would emit
       a realization record without writing any live grandchild breadcrumb,
       reintroducing the #12460 ``PARTIAL-display`` gap (Redmine #12473 review
       j#64105). A decision / same-lane-worker-only realization that does not
       form the depth-2 implementation lane with a stampable pane fails closed
       here rather than stamping a half-formed tree.

    The stamp values come straight from the declared lanes (the durable
    declaration), so the written options equal the derived projection; only the
    two options the discovery read path consumes are stamped. Pure and
    deterministic over its inputs; raises :class:`GrandchildStampError` on any
    violation.
    """
    if realization not in REALIZATIONS:
        raise GrandchildStampError(
            f"unknown realization {realization!r}; expected one of {sorted(REALIZATIONS)}"
        )
    reason = (adopt_reason or "").strip()
    if realization == REALIZATION_ADOPT and not reason:
        raise GrandchildStampError(
            "an adopt realization must carry a non-empty --adopt-reason "
            "(the replayable reason the existing lane was adopted as grandchild)"
        )
    if realization == REALIZATION_LAUNCH and reason:
        raise GrandchildStampError(
            "a launch realization must not carry an --adopt-reason "
            "(there is no adoption to justify; record the launch provenance instead)"
        )

    if not declared_lanes:
        raise GrandchildStampError("no declared lanes; cannot stamp a delegation tree")

    sources = [
        DelegationSource(
            unit_id=lane.unit_id,
            lane_kind=lane.lane_kind,
            delegation_parent=lane.delegation_parent,
        )
        for lane in declared_lanes
    ]
    try:
        tree = derive_delegation_tree(sources)
    except DelegationProjectionError as exc:
        raise GrandchildStampError(
            f"declared delegation tree is invalid (not replayable): {exc}"
        ) from exc

    grandchild = tree.get(grandchild_unit)
    if grandchild is None:
        raise GrandchildStampError(
            f"grandchild_unit {grandchild_unit!r} is not among the declared lanes"
        )
    if grandchild.lane_kind != LANE_KIND_IMPLEMENTATION:
        raise GrandchildStampError(
            f"grandchild_unit {grandchild_unit!r} must be a "
            f"{LANE_KIND_IMPLEMENTATION!r} lane; got {grandchild.lane_kind!r}"
        )
    if grandchild.delegation_depth != GRANDCHILD_DEPTH:
        raise GrandchildStampError(
            f"grandchild_unit {grandchild_unit!r} must derive to depth "
            f"{GRANDCHILD_DEPTH} (parent -> delegated -> grandchild); got depth "
            f"{grandchild.delegation_depth}. A decision / same-lane-worker-only "
            "route does not form a depth-2 grandchild display (Redmine #12460)."
        )
    # The realized grandchild lane must carry at least one live pane to stamp.
    # Ancestors may be derivation-only (panes=()), but a derivation-only
    # grandchild would emit a realization record without writing any live
    # grandchild KIND/DEPTH/PARENT breadcrumb — the #12460 PARTIAL-display gap
    # (Redmine #12473 review j#64105). grandchild_unit is in the tree, so exactly
    # one declared lane carries it (derive_delegation_tree rejects duplicates).
    grandchild_lane = next(
        lane for lane in declared_lanes if lane.unit_id == grandchild_unit
    )
    if not any(pane for pane in grandchild_lane.panes):
        raise GrandchildStampError(
            f"grandchild_unit {grandchild_unit!r} declares no live pane to stamp; "
            "a realized grandchild must carry at least one pane= so its live "
            "KIND/DEPTH/PARENT breadcrumb is written. A derivation-only grandchild "
            "would record a realization without a visible grandchild lane "
            "(Redmine #12460 PARTIAL-display / #12473 j#64105)."
        )

    commands: list[tuple[str, ...]] = []
    stamped_panes: list[str] = []
    for lane in declared_lanes:
        for pane in lane.panes:
            if not pane:
                continue
            stamped_panes.append(pane)
            commands.append(
                ("set-option", "-p", "-t", pane, OPTION_LANE_KIND, lane.lane_kind)
            )
            commands.append(
                (
                    "set-option",
                    "-p",
                    "-t",
                    pane,
                    OPTION_DELEGATION_PARENT,
                    lane.delegation_parent or "",
                )
            )

    return GrandchildStampPlan(
        realization=realization,
        grandchild_unit=grandchild_unit,
        grandchild_lane_kind=grandchild.lane_kind,
        grandchild_depth=grandchild.delegation_depth,
        grandchild_parent=grandchild.delegation_parent,
        grandchild_root=grandchild.delegation_root,
        adopt_reason=reason or None,
        commands=tuple(commands),
        stamped_panes=tuple(stamped_panes),
        projections=tuple(tree[lane.unit_id] for lane in declared_lanes),
    )


# --- grandchild realization gate (Redmine #12474 QA / #12473 j#64151) ---------
# The stamp plan above realizes a *declared* grandchild lane. This gate closes
# the runtime-path hole the #12474 smoke exposed: the delegated coordinator could
# resolve a grandchild dispatch decision and then silently fall through to a
# same-lane worker handoff, leaving the grandchild unrealized and KIND/DEPTH/
# PARENT blank. The gate maps "did the dispatch decision require a grandchild" +
# "is one actually realized/stamped" to a replayable verdict, so a same-lane
# handoff alone can never be treated as display acceptance when policy required
# grandchild realization.

#: A grandchild was required and a route-bound, stamped depth-2 implementation
#: lane is realized: the worker handoff may proceed to that lane.
GATE_REALIZED = "realized"
#: A grandchild was required but none is realized/stamped: the runtime must
#: record blocked replayably, never treat the same-lane handoff as acceptance.
GATE_BLOCKED = "blocked"
#: No grandchild was required (the dispatch decision was no_dispatch): a same-lane
#: worker is the legitimate, policy-correct outcome.
GATE_SAME_LANE_OK = "same_lane_ok"


@dataclass(frozen=True)
class RealizationGateResult:
    """The realize-or-blocked verdict for a delegated-coordinator worker handoff."""

    verdict: str
    reason: str
    grandchild_required: bool
    realized_grandchild_unit: Optional[str]

    @property
    def is_blocked(self) -> bool:
        return self.verdict == GATE_BLOCKED

    @property
    def is_realized(self) -> bool:
        return self.verdict == GATE_REALIZED


def find_realized_grandchild_unit(
    units: Sequence[tuple[str, str, Optional[int], str, str]],
    *,
    delegated_coordinator_unit: str,
) -> Optional[str]:
    """Return the unit_id of a route-bound, realized grandchild lane, or ``None``.

    ``units`` is a sequence of ``(unit_id, lane_kind, delegation_depth,
    delegation_parent, status)`` rows — one per discovered lane unit, derived by
    the caller from ``delegation_display.derive_targets_delegation``. A *realized*
    grandchild is a ``derived`` (not diagnostic / none) depth-:data:`GRANDCHILD_DEPTH`
    :data:`LANE_KIND_IMPLEMENTATION` lane whose ``delegation_parent`` is
    ``delegated_coordinator_unit``. Pure; the first match wins (the route is
    one-grandchild-per-delegated-coordinator under the shallow-delegation model).
    """
    for unit_id, lane_kind, depth, parent, status in units:
        if (
            status == "derived"
            and lane_kind == LANE_KIND_IMPLEMENTATION
            and depth == GRANDCHILD_DEPTH
            and parent == delegated_coordinator_unit
        ):
            return unit_id
    return None


def evaluate_grandchild_realization_gate(
    *, grandchild_required: bool, realized_grandchild_unit: Optional[str]
) -> RealizationGateResult:
    """Gate the delegated-coordinator worker handoff on grandchild realization.

    The #12474 QA finding (#12473 j#64151): a successful same-lane worker handoff
    alone must NOT satisfy display acceptance when policy requires grandchild
    realization. This maps the dispatch decision's grandchild requirement + the
    live realization evidence to a replayable verdict:

    - ``grandchild_required`` false (the dispatch decision was no_dispatch) ->
      :data:`GATE_SAME_LANE_OK`: a same-lane worker is the legitimate outcome.
    - required AND a route-bound depth-2 implementation lane is realized ->
      :data:`GATE_REALIZED`: the worker handoff may proceed to that lane.
    - required AND none realized -> :data:`GATE_BLOCKED`: the runtime must record
      blocked replayably (create/adopt + ``delegate-grandchild-stamp`` first),
      never treat the same-lane handoff as acceptance.

    Pure and deterministic.
    """
    if not grandchild_required:
        return RealizationGateResult(
            verdict=GATE_SAME_LANE_OK,
            reason=(
                "dispatch decision did not require a grandchild lane "
                "(no_dispatch); a same-lane worker is the legitimate outcome"
            ),
            grandchild_required=False,
            realized_grandchild_unit=None,
        )
    if realized_grandchild_unit:
        return RealizationGateResult(
            verdict=GATE_REALIZED,
            reason=(
                "a route-bound depth-2 implementation grandchild lane is realized "
                "and stamped; the worker handoff may proceed"
            ),
            grandchild_required=True,
            realized_grandchild_unit=realized_grandchild_unit,
        )
    return RealizationGateResult(
        verdict=GATE_BLOCKED,
        reason=(
            "grandchild_required_but_not_realized: the dispatch decision requires "
            "a grandchild lane, but no route-bound depth-2 implementation lane is "
            "stamped/visible. A same-lane worker handoff alone does not satisfy "
            "display acceptance (Redmine #12460 / #12474 j#64151); create/adopt a "
            "grandchild lane and run delegate-grandchild-stamp, or record blocked."
        ),
        grandchild_required=True,
        realized_grandchild_unit=None,
    )


__all__ = (
    "REALIZATION_LAUNCH",
    "REALIZATION_ADOPT",
    "REALIZATIONS",
    "GRANDCHILD_DEPTH",
    "GrandchildStampError",
    "DeclaredLane",
    "GrandchildStampPlan",
    "resolve_grandchild_stamp_plan",
    "GATE_REALIZED",
    "GATE_BLOCKED",
    "GATE_SAME_LANE_OK",
    "RealizationGateResult",
    "find_realized_grandchild_unit",
    "evaluate_grandchild_realization_gate",
)
