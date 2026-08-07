"""Reboot residue convergence: the single-snapshot join + per-lane plan (Redmine #14499).

After a Mac reboot the host is left in a shape no single existing surface describes. Live
audit #13490 j#89060 measured it: of 23 assigned herdr panes, **15 carry no Codex / Claude
process at all** — a foreground ``-zsh``, cwd ``$HOME``, revision 0, status unknown (the
#13518 *shell residue*). Those 15 span 8 issue lanes whose public ``sublane list`` reads
``detached`` while every lifecycle row still reads ``active / process_release=not_requested``,
and whose recorded worktrees — all under ``/private/tmp`` — are gone, leaving prunable git
administrative entries. Branches and commits survived.

Converging that by hand meant reading four authorities separately (Redmine close state, git
branch / origin reachability, the durable lifecycle row, and the live assigned-name +
process inventory) and then guessing which of the five retire intents applied. This module is
the **pure** half of doing it once: a lane's facts from all four authorities join into one
:class:`RebootLaneFacts`, and :func:`plan_lane_convergence` returns the typed disposition.

Three properties are load-bearing:

- **Unknown is not absence.** Every axis that can fail to read is ``Optional`` and an
  unreadable one yields :data:`CONVERGE_UNKNOWN`, never a plan. A plan built on an
  unreadable Redmine would propose terminalizing a lane whose issue is still open.
- **The plan never proposes a bulk close.** Each verdict is scoped to ONE lane unit. There
  is deliberately no workspace-level or all-lanes verdict in the vocabulary (Required
  behavior 5): the reboot shape's whole risk is that 15 residue panes look like one problem
  with one sweeping answer, and 8 of those lanes have different correct answers.
- **Cleanup is downstream of terminalization** (Required behavior 8).
  :attr:`RebootLanePlan.cleanup_permitted` is true only for a lane whose lifecycle row is
  already terminal, and the cleanup steps this module emits never delete a branch or a
  commit — only the worktree checkout and git's administrative entry for it.

This module performs no I/O and imports nothing that does; the live fact-gathering and the
CLI live in :mod:`...application.sublane_reboot_audit`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_UNKNOWN,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_LIVE,
    SLOT_STALE,
)

# ---------------------------------------------------------------------------
# Convergence vocabulary (closed).
# ---------------------------------------------------------------------------

#: A required axis could not be read. No plan is produced and nothing may be actuated.
CONVERGE_UNKNOWN = "unknown"
#: The lifecycle row is already ``retired``. Nothing to converge; worktree / administrative
#: cleanup is now permitted (Required behavior 8).
CONVERGE_ALREADY_TERMINAL = "already_terminal"
#: An open issue whose lifecycle row is already hibernated needs no repeated hibernate or
#: active-only supersede action. The row is already in the desired non-terminal state.
CONVERGE_ALREADY_HIBERNATED = "already_hibernated"
#: Closed issue, live managed pair present: drain it the ordinary way
#: (``sublane retire --execute``). Never a metadata terminalize — there are processes.
CONVERGE_GUARDED_CLOSE = "guarded_close"
#: Closed issue, no live agent, but the lane's own slots survive as shell residue. Close
#: exactly those (``sublane close-residue --execute``) before terminalizing, so the
#: terminal write is taken against a genuinely empty unit.
CONVERGE_CLOSE_RESIDUE = "close_shell_residue"
#: Closed issue, live-zero, BOUND row, and the recorded worktree is GONE. Restoring the
#: exact worktree re-enables the bound rails' worktree attestation (and lets the coordinator
#: see the checkout's real state) without touching the branch.
CONVERGE_RESTORE_WORKTREE = "restore_worktree"
#: Closed issue, live-zero, BOUND row whose worktree is present: the existing metadata-only
#: terminal retire applies (``--retire-active-live-zero`` / ``--retire-hibernated-bound``).
CONVERGE_TERMINALIZE_BOUND = "terminalize_bound_metadata"
#: Closed issue, live-zero, UNBOUND row (empty ``worktree_identity``) — the #14456 j#87973
#: shape that every existing terminal rail refuses. Converged by
#: ``--retire-active-unbound-live-zero``.
CONVERGE_TERMINALIZE_UNBOUND = "terminalize_unbound_metadata"
#: Closed issue, live-zero, HIBERNATED + RELEASED + UNBOUND row. This is deliberately a
#: distinct rail from the active-only unbound terminalization above.
CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND = (
    "terminalize_hibernated_unbound_metadata"
)
#: Open issue with a live managed pair: the lane is working. Route the next action to it.
CONVERGE_RESUME = "resume"
#: Open issue, no live agent: record the desired hibernated state so capacity accounting
#: matches reality. The worktree, branch and commits survive for a cold restart.
CONVERGE_HIBERNATE = "hibernate"
#: Open issue owned by MORE THAN ONE active lane: a successor must take ownership
#: atomically (``sublane supersede``). Never resolved by closing one side by hand.
CONVERGE_SUPERSEDE = "supersede"
#: A fact contradicts the model and no safe action follows. Carries ``reason``.
CONVERGE_BLOCKED = "blocked"

CONVERGENCES = frozenset(
    {
        CONVERGE_UNKNOWN,
        CONVERGE_ALREADY_TERMINAL,
        CONVERGE_ALREADY_HIBERNATED,
        CONVERGE_GUARDED_CLOSE,
        CONVERGE_CLOSE_RESIDUE,
        CONVERGE_RESTORE_WORKTREE,
        CONVERGE_TERMINALIZE_BOUND,
        CONVERGE_TERMINALIZE_UNBOUND,
        CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND,
        CONVERGE_RESUME,
        CONVERGE_HIBERNATE,
        CONVERGE_SUPERSEDE,
        CONVERGE_BLOCKED,
    }
)

# -- reasons ----------------------------------------------------------------

#: The lane's Redmine issue open/closed state could not be read.
REASON_ISSUE_STATE_UNREADABLE = "issue_state_unreadable"
#: The live herdr inventory could not be read, so liveness is unmeasured.
REASON_INVENTORY_UNREADABLE = "inventory_unreadable"
#: Whether the recorded worktree still exists could not be determined.
REASON_WORKTREE_PRESENCE_UNKNOWN = "worktree_presence_unknown"
#: The lane's branch is not integrated (or integration could not be proven), so no terminal
#: disposition may be proposed: terminalizing would strand unintegrated work.
REASON_HEAD_NOT_INTEGRATED = "head_not_integrated"
#: A foreign / unexpected provider occupies one of the lane's units.
REASON_FOREIGN_OCCUPANT = "foreign_occupant"
#: The row is ``superseded``: it is not an owner any more and never returns to active. Its
#: only legal edge is ``retired``, and only once its issue is closed.
REASON_SUPERSEDED_ROW = "superseded_row"
#: A release generation is open (``requested`` / ``partial``), so liveness reads may be
#: observing a mid-actuation state.
REASON_RELEASE_IN_FLIGHT = "release_in_flight"
#: A process release token outside the lifecycle schema cannot be treated as settled or live.
REASON_UNKNOWN_PROCESS_RELEASE = "unknown_process_release"
#: The lane owns no issue, so its Redmine axis is not even askable.
REASON_LANE_OWNS_NO_ISSUE = "lane_owns_no_issue"
#: A lifecycle disposition outside the version understood by this planner is never treated
#: as active or hibernated by default.
REASON_UNKNOWN_LIFECYCLE_DISPOSITION = "unknown_lifecycle_disposition"
#: Hibernated terminalization requires the independent durable release witness.
REASON_HIBERNATED_RELEASE_UNPROVEN = "hibernated_release_unproven"
#: A hibernated row with a live pair needs the dedicated reconcile path, not active close.
REASON_HIBERNATED_LIVE_PAIR_PRESENT = "hibernated_live_pair_present"


# ---------------------------------------------------------------------------
# Facts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RebootSlotFact:
    """One assigned-name row observed in a lane's targeted unit.

    ``liveness`` is the #13518 :func:`classify_named_slot` verdict — :data:`SLOT_LIVE` or
    :data:`SLOT_STALE`. ``runtime_status`` is the row's mapped receiver state, kept
    SEPARATELY: the residue close (Required behavior 6) refuses to close anything reporting
    a recognised active state even when the liveness classifier called it stale, so the two
    signals must not be collapsed into one field.

    ``foreign`` marks a row that occupies the lane's unit without being one of the lane's
    expected managed slots. A foreign row is never a close target and never counts as the
    lane's liveness; it only blocks.
    """

    role: str
    assigned_name: str
    locator: str
    liveness: str
    runtime_status: str = RUNTIME_UNKNOWN
    foreign: bool = False

    @property
    def is_live_agent(self) -> bool:
        """Backed by a real managed agent (not residue, not foreign)."""
        return self.liveness == SLOT_LIVE and not self.foreign and bool(self.locator)

    @property
    def is_shell_residue(self) -> bool:
        """The lane's own slot, name and locator intact, with no agent behind it."""
        return self.liveness == SLOT_STALE and not self.foreign and bool(self.locator)

    def as_payload(self) -> dict:
        return {
            "role": self.role,
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "liveness": self.liveness,
            "runtime_status": self.runtime_status,
            "foreign": self.foreign,
        }


@dataclass(frozen=True)
class RebootLaneFacts:
    """One lane's joined facts from all four authorities (Redmine #14499 RB2).

    Every axis that can fail to read is ``Optional[bool]``, and ``None`` means *unknown*.
    Unknown is never folded into ``False``: "the issue is not closed" and "we could not ask
    whether the issue is closed" license completely different actions.
    """

    # -- durable lifecycle row ------------------------------------------------
    workspace_id: str
    lane_id: str
    issue_id: str = ""
    lane_disposition: str = DISPOSITION_ACTIVE
    process_release: str = RELEASE_NOT_REQUESTED
    binding_kind: str = BINDING_KIND_ISSUE
    #: The canonical worktree binding token. Empty == UNBOUND (the #14456 shape).
    worktree_identity: str = ""
    lane_generation: int = 1
    revision: int = 1

    # -- git ------------------------------------------------------------------
    #: The worktree path the lane metadata recorded (display / restore material).
    recorded_worktree: str = ""
    #: Does that path still exist as a git checkout? ``None`` == not probed / unknowable.
    worktree_present: Optional[bool] = None
    branch: str = ""
    #: Does the local branch ref still resolve? ``None`` == unknown.
    branch_exists: Optional[bool] = None
    #: Is the lane's head reachable from the integration branch on ``origin``?
    #: ``None`` == unknown. Required before ANY terminal disposition is proposed.
    head_integrated: Optional[bool] = None

    # -- Redmine --------------------------------------------------------------
    #: Is the lane's issue durably closed? ``None`` == unread / unconfigured.
    issue_closed: Optional[bool] = None

    # -- live inventory -------------------------------------------------------
    #: ``None`` == the inventory could not be read (never an empty tuple for that case).
    slots: Optional[Sequence[RebootSlotFact]] = None
    #: Other ACTIVE lanes recorded as owning the SAME issue (excluding this one).
    peer_active_lanes: tuple[str, ...] = ()

    @property
    def is_bound(self) -> bool:
        return bool((self.worktree_identity or "").strip())

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "issue_id": self.issue_id,
            "lane_disposition": self.lane_disposition,
            "process_release": self.process_release,
            "binding_kind": self.binding_kind,
            "worktree_identity": self.worktree_identity,
            "bound": self.is_bound,
            "lane_generation": self.lane_generation,
            "revision": self.revision,
            "recorded_worktree": self.recorded_worktree,
            "worktree_present": self.worktree_present,
            "branch": self.branch,
            "branch_exists": self.branch_exists,
            "head_integrated": self.head_integrated,
            "issue_closed": self.issue_closed,
            "slots": (
                None if self.slots is None else [s.as_payload() for s in self.slots]
            ),
            "peer_active_lanes": list(self.peer_active_lanes),
        }


@dataclass(frozen=True)
class RebootLanePlan:
    """The typed per-lane convergence disposition (Redmine #14499 RB3 / RB4 / RB5).

    ``convergence`` is the primary verdict; ``alternatives`` names other legitimate rails
    for the same lane (a missing-worktree bound lane can be restored OR terminalized as
    metadata — both are safe, and the choice is the coordinator's).

    ``cleanup_permitted`` gates Required behavior 8: worktree / git-administrative cleanup
    is offered ONLY once the lifecycle row is terminal. ``steps`` never contains a branch
    or commit deletion.
    """

    workspace_id: str
    lane_id: str
    issue_id: str
    convergence: str
    reason: str = ""
    detail: str = ""
    alternatives: tuple[str, ...] = ()
    residue_slots: tuple[str, ...] = ()
    live_slots: tuple[str, ...] = ()
    foreign_slots: tuple[str, ...] = ()
    cleanup_permitted: bool = False
    steps: tuple[str, ...] = field(default=())

    @property
    def actionable(self) -> bool:
        """Is there a concrete rail to run? (``unknown`` / ``blocked`` are not)"""
        return self.convergence not in (
            CONVERGE_UNKNOWN,
            CONVERGE_BLOCKED,
            CONVERGE_ALREADY_HIBERNATED,
            CONVERGE_ALREADY_TERMINAL,
        )

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "issue_id": self.issue_id,
            "convergence": self.convergence,
            "reason": self.reason,
            "detail": self.detail,
            "alternatives": list(self.alternatives),
            "residue_slots": list(self.residue_slots),
            "live_slots": list(self.live_slots),
            "foreign_slots": list(self.foreign_slots),
            "cleanup_permitted": self.cleanup_permitted,
            "steps": list(self.steps),
        }


def slot_fact_from_row(
    row: Mapping[str, object],
    *,
    role: str,
    assigned_name: str,
    locator: str,
    liveness: str,
    foreign: bool = False,
) -> RebootSlotFact:
    """Build a :class:`RebootSlotFact`, reading the row's runtime status separately (pure).

    The status is mapped through the shared :func:`map_agent_status` so an unrecognised /
    absent one degrades to :data:`RUNTIME_UNKNOWN` rather than a confident wrong state.
    """
    status = RUNTIME_UNKNOWN
    for key in ("agent_status", "status", "state"):
        if key in row:
            status = map_agent_status(row.get(key))
            break
    return RebootSlotFact(
        role=role,
        assigned_name=assigned_name,
        locator=locator,
        liveness=liveness,
        runtime_status=status,
        foreign=foreign,
    )


# ---------------------------------------------------------------------------
# The decision.
# ---------------------------------------------------------------------------


def _unknown(facts: RebootLaneFacts, reason: str, detail: str) -> RebootLanePlan:
    return RebootLanePlan(
        workspace_id=facts.workspace_id,
        lane_id=facts.lane_id,
        issue_id=facts.issue_id,
        convergence=CONVERGE_UNKNOWN,
        reason=reason,
        detail=detail,
    )


def _blocked(facts: RebootLaneFacts, reason: str, detail: str, **kw) -> RebootLanePlan:
    return RebootLanePlan(
        workspace_id=facts.workspace_id,
        lane_id=facts.lane_id,
        issue_id=facts.issue_id,
        convergence=CONVERGE_BLOCKED,
        reason=reason,
        detail=detail,
        **kw,
    )


def plan_lane_convergence(facts: RebootLaneFacts) -> RebootLanePlan:
    """The typed convergence disposition for ONE lane (pure, fail-closed).

    Decision order, most fundamental first:

    1. **Already terminal.** A ``retired`` row needs no lifecycle action, and is the only
       state in which worktree / administrative cleanup is offered (Required behavior 8).
    2. **Unknown axes.** An unreadable inventory or Redmine state yields
       :data:`CONVERGE_UNKNOWN`. Measured last-but-checked-first on purpose: a plan built
       on an unread authority is worse than no plan.
    3. **Release in flight.** ``requested`` / ``partial`` means an actuator may be closing
       panes right now, so every liveness read is untrustworthy.
    4. **Foreign occupant.** A non-managed process in the lane's unit blocks every
       disposition — closing or terminalizing around it would record the lane gone while a
       real process keeps running.
    5. **Open issue** → ``supersede`` (a peer active owner exists) / ``resume`` (a live
       pair) / ``hibernate`` (live-zero). Never a terminal disposition, and never a
       workspace-wide action.
    6. **Closed issue** → ``guarded_close`` (live pair) / ``close_shell_residue`` (the
       lane's own residue survives) / a terminal metadata rail, chosen by whether the row
       carries a worktree binding and whether that worktree still exists.

    Head integration is required before any *terminal* verdict (5 and 6's terminal
    branches): a lane whose work never reached the integration branch must not be
    terminalized, because that is the state from which its branch would later look
    abandoned. It is deliberately NOT required for ``resume`` / ``hibernate`` /
    ``close_shell_residue``, none of which are terminal.
    """
    disposition = facts.lane_disposition or ""

    # 1. Already terminal. Cleanup becomes available here and ONLY here.
    if disposition == DISPOSITION_RETIRED:
        return RebootLanePlan(
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            issue_id=facts.issue_id,
            convergence=CONVERGE_ALREADY_TERMINAL,
            detail=(
                "the lifecycle row is terminally retired; worktree and git administrative "
                "cleanup are now permitted. The branch and its commits are NOT part of "
                "cleanup and must not be deleted"
            ),
            cleanup_permitted=True,
            steps=_cleanup_steps(facts),
        )

    # 2. Unknown axes — no plan may be built on an unread authority.
    if facts.slots is None:
        return _unknown(
            facts,
            REASON_INVENTORY_UNREADABLE,
            "the live herdr inventory could not be read; liveness is unmeasured, so no "
            "disposition (not even a read-only recommendation) is produced",
        )
    if not (facts.issue_id or "").strip():
        return _blocked(
            facts,
            REASON_LANE_OWNS_NO_ISSUE,
            "the lifecycle row owns no issue, so its Redmine open/closed axis cannot be "
            "asked; re-declare the lane's owner binding before converging it",
        )
    if facts.issue_closed is None:
        return _unknown(
            facts,
            REASON_ISSUE_STATE_UNREADABLE,
            "the lane's Redmine issue open/closed state could not be read (unconfigured "
            "credentials or an unreachable Redmine); an unread issue is never treated as "
            "closed",
        )

    slots = tuple(facts.slots)
    live = tuple(sorted({s.role for s in slots if s.is_live_agent}))
    residue = tuple(sorted({s.assigned_name for s in slots if s.is_shell_residue}))
    foreign = tuple(sorted({s.assigned_name for s in slots if s.foreign}))
    common = dict(live_slots=live, residue_slots=residue, foreign_slots=foreign)

    # 3. A release generation in flight makes every liveness read provisional.
    if facts.process_release in (RELEASE_REQUESTED, RELEASE_PARTIAL):
        return _blocked(
            facts,
            REASON_RELEASE_IN_FLIGHT,
            f"a process release is in flight (process_release={facts.process_release}); "
            "a liveness read taken now may be observing a mid-actuation state. Let the "
            "release settle, then re-audit",
            **common,
        )
    if facts.process_release not in (RELEASE_NOT_REQUESTED, RELEASE_RELEASED):
        return _blocked(
            facts,
            REASON_UNKNOWN_PROCESS_RELEASE,
            f"the lifecycle row carries an unknown process_release token "
            f"{facts.process_release!r}; the planner will not normalize a future or "
            "malformed release state into settled authority",
            **common,
        )

    # 4. A foreign occupant blocks every disposition.
    if foreign:
        return _blocked(
            facts,
            REASON_FOREIGN_OCCUPANT,
            "a foreign / unexpected provider occupies one of the lane's units "
            f"({', '.join(foreign)}); converging around it would record the lane gone "
            "while a real process keeps running there",
            **common,
        )

    known_dispositions = {
        DISPOSITION_ACTIVE,
        DISPOSITION_HIBERNATED,
        DISPOSITION_RETIRED,
        DISPOSITION_SUPERSEDED,
    }
    if disposition not in known_dispositions:
        return _blocked(
            facts,
            REASON_UNKNOWN_LIFECYCLE_DISPOSITION,
            f"the lifecycle row carries an unknown disposition {disposition!r}; this "
            "planner will not normalize a future or malformed state into an existing "
            "destructive rail",
            **common,
        )

    # 5. Open issue: disposition first, then active-only resume/hibernate/supersede.
    if not facts.issue_closed:
        if disposition == DISPOSITION_HIBERNATED:
            return RebootLanePlan(
                workspace_id=facts.workspace_id,
                lane_id=facts.lane_id,
                issue_id=facts.issue_id,
                convergence=CONVERGE_ALREADY_HIBERNATED,
                detail=(
                    f"issue #{facts.issue_id} is open and the lane is already hibernated; "
                    "no lifecycle action is needed. An active successor does not make this "
                    "hibernated original eligible for the active-only supersede rail"
                ),
                **common,
            )
        if disposition == DISPOSITION_SUPERSEDED:
            return _blocked(
                facts,
                REASON_SUPERSEDED_ROW,
                "the row is superseded and can never return to active; it converges only "
                "to retired, and only once its issue is closed",
                **common,
            )
        if facts.peer_active_lanes:
            return RebootLanePlan(
                workspace_id=facts.workspace_id,
                lane_id=facts.lane_id,
                issue_id=facts.issue_id,
                convergence=CONVERGE_SUPERSEDE,
                detail=(
                    f"issue #{facts.issue_id} is open and is recorded as owned by more "
                    f"than one active lane ({', '.join(facts.peer_active_lanes)}); "
                    "ownership must move atomically via `sublane supersede`, never by "
                    "closing one side by hand"
                ),
                steps=(
                    "mozyo-bridge sublane supersede --issue "
                    f"{facts.issue_id} --from-lane {facts.lane_id} "
                    "--to-lane <successor> --execute",
                ),
                **common,
            )
        if live:
            return RebootLanePlan(
                workspace_id=facts.workspace_id,
                lane_id=facts.lane_id,
                issue_id=facts.issue_id,
                convergence=CONVERGE_RESUME,
                detail=(
                    f"issue #{facts.issue_id} is open and the lane's managed slots "
                    f"({', '.join(live)}) are backed by live agents; the lane is working "
                    "— route its next action to it rather than converging it"
                ),
                **common,
            )
        return RebootLanePlan(
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            issue_id=facts.issue_id,
            convergence=CONVERGE_HIBERNATE,
            detail=(
                f"issue #{facts.issue_id} is open but no managed agent is live"
                + (f" ({len(residue)} shell residue slot(s))" if residue else "")
                + "; record the desired hibernated state so capacity accounting matches "
                "reality. The worktree, branch and commits survive for a cold restart — "
                "an open issue is never terminalized"
            ),
            steps=(
                f"mozyo-bridge sublane hibernate --issue {facts.issue_id} "
                f"--lane-label {facts.lane_id} --execute",
            ),
            **common,
        )

    # 6. Closed issue. Disposition and release authority still precede liveness routing.
    if disposition == DISPOSITION_SUPERSEDED:
        return _blocked(
            facts,
            REASON_SUPERSEDED_ROW,
            "the row is superseded; no active- or hibernated-owner terminal rail applies "
            "to it. Preserve the row until a dedicated superseded-to-retired authority is "
            "available",
            **common,
        )
    if (
        disposition == DISPOSITION_HIBERNATED
        and facts.process_release != RELEASE_RELEASED
    ):
        return _blocked(
            facts,
            REASON_HIBERNATED_RELEASE_UNPROVEN,
            "the row is hibernated but its process release is not durably `released`; the "
            "independent release witness required by the hibernated terminal rails is "
            "missing, so the planner refuses a terminal action",
            **common,
        )
    if live:
        if disposition == DISPOSITION_HIBERNATED:
            return _blocked(
                facts,
                REASON_HIBERNATED_LIVE_PAIR_PRESENT,
                "the row is hibernated but a managed pair is live; the active-only guarded "
                "close is not a valid rail for this state. Reconcile the hibernated/live "
                "contradiction before terminalization",
                **common,
            )
        return RebootLanePlan(
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            issue_id=facts.issue_id,
            convergence=CONVERGE_GUARDED_CLOSE,
            detail=(
                f"issue #{facts.issue_id} is closed and the lane still runs live managed "
                f"agents ({', '.join(live)}); drain them through the ordinary guarded "
                "close, which is the only rail authorized to close a live pair"
            ),
            steps=(
                f"mozyo-bridge sublane retire --issue {facts.issue_id} "
                f"--lane-label {facts.lane_id} --execute ...",
            ),
            **common,
        )
    if residue:
        return RebootLanePlan(
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            issue_id=facts.issue_id,
            convergence=CONVERGE_CLOSE_RESIDUE,
            detail=(
                f"issue #{facts.issue_id} is closed and no managed agent is live, but the "
                f"lane's own assigned-name slots survive as shell residue "
                f"({', '.join(residue)}). Close exactly those first, so the terminal write "
                "is taken against a genuinely empty unit"
            ),
            steps=(
                f"mozyo-bridge sublane close-residue --issue {facts.issue_id} "
                f"--lane-label {facts.lane_id} --execute",
            ),
            **common,
        )

    # Live-zero on a closed issue: a terminal disposition is in scope, so head integration
    # becomes mandatory. An unintegrated (or unprovable) head is never terminalized.
    if facts.head_integrated is not True:
        return _blocked(
            facts,
            REASON_HEAD_NOT_INTEGRATED,
            "the lane's head is not a proven ancestor of the integration branch (or the "
            "ancestry could not be measured); terminalizing now would strand its work. "
            "Integrate it — or record a patch-equivalent integration disposition — first. "
            "The branch and its commits are preserved either way",
            **common,
        )
    if not facts.is_bound:
        hibernated = disposition == DISPOSITION_HIBERNATED
        retire_flag = (
            "--retire-hibernated-unbound-live-zero"
            if hibernated
            else "--retire-active-unbound-live-zero"
        )
        return RebootLanePlan(
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            issue_id=facts.issue_id,
            convergence=(
                CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND
                if hibernated
                else CONVERGE_TERMINALIZE_UNBOUND
            ),
            detail=(
                f"issue #{facts.issue_id} is closed, the lane measures live-zero, and its "
                "row records NO canonical worktree binding (a pre-#13754 row). No "
                "worktree can be attested, so the terminal write is fenced on the row's "
                f"exact generation ({facts.lane_generation}) and revision "
                f"({facts.revision}) instead. "
                + (
                    "The hibernated/released-specific unbound rail also re-reads the exact "
                    "closed issue and decision journal. Metadata only"
                    if hibernated
                    else "Metadata only"
                )
            ),
            steps=(
                f"mozyo-bridge sublane retire --issue {facts.issue_id} "
                f"--lane-label {facts.lane_id} {retire_flag} "
                f"--expect-lane-generation {facts.lane_generation} "
                f"--expect-lane-revision {facts.revision} ...",
            ),
            **common,
        )
    if facts.worktree_present is None:
        return _unknown(
            facts,
            REASON_WORKTREE_PRESENCE_UNKNOWN,
            "the lane is bound to a canonical worktree but whether that worktree still "
            "exists could not be determined; the bound rails attest against it, so the "
            "disposition is left unresolved rather than guessed",
        )
    if facts.worktree_present:
        return RebootLanePlan(
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            issue_id=facts.issue_id,
            convergence=CONVERGE_TERMINALIZE_BOUND,
            detail=(
                f"issue #{facts.issue_id} is closed, the lane measures live-zero, and its "
                "recorded worktree is present, so the existing bound terminal retire can "
                "attest it. Metadata only — no worktree or branch is removed"
            ),
            steps=(
                f"mozyo-bridge sublane retire --issue {facts.issue_id} "
                f"--lane-label {facts.lane_id} --worktree {facts.recorded_worktree} "
                + (
                    "--retire-hibernated-bound ..."
                    if disposition == DISPOSITION_HIBERNATED
                    else "--retire-active-live-zero ..."
                ),
            ),
            **common,
        )
    # BOUND, closed, live-zero, worktree GONE — the reboot's characteristic shape.
    restore_step = (
        f"git worktree add {facts.recorded_worktree} {facts.branch}"
        if facts.recorded_worktree and facts.branch
        else "git worktree add <recorded worktree> <lane branch>"
    )
    return RebootLanePlan(
        workspace_id=facts.workspace_id,
        lane_id=facts.lane_id,
        issue_id=facts.issue_id,
        convergence=CONVERGE_RESTORE_WORKTREE,
        detail=(
            f"issue #{facts.issue_id} is closed and the lane measures live-zero, but its "
            f"recorded worktree {facts.recorded_worktree or '<unrecorded>'} is gone "
            "(the reboot shape). Its branch and commits survived. Restoring the EXACT "
            "recorded path re-enables the bound rails' worktree attestation; the "
            "metadata-only terminalization is the equally safe alternative when the "
            "checkout is not wanted back"
        ),
        alternatives=(CONVERGE_TERMINALIZE_BOUND,),
        steps=(
            restore_step,
            f"mozyo-bridge sublane retire --issue {facts.issue_id} "
            f"--lane-label {facts.lane_id} --worktree {facts.recorded_worktree} "
            + (
                "--retire-hibernated-bound ..."
                if disposition == DISPOSITION_HIBERNATED
                else "--retire-active-live-zero ..."
            ),
        ),
        **common,
    )


def _cleanup_steps(facts: RebootLaneFacts) -> tuple[str, ...]:
    """Post-terminalization cleanup steps for a retired lane (Required behavior 8).

    Emitted ONLY from the ``already_terminal`` branch. Deliberately limited to the worktree
    checkout and git's administrative entry for it: ``git worktree prune`` removes only the
    bookkeeping for a checkout that is already gone, and ``git worktree remove`` removes only
    the checkout. **No branch delete and no commit removal is ever emitted** — the reboot
    audit's evidence (#13490 j#89060) is that branches and commits survived the reboot
    intact, and they are the lane's only durable work product.
    """
    steps: list[str] = []
    if facts.worktree_present is False:
        steps.append(
            "git worktree prune  # removes the administrative entry only; the checkout is "
            "already gone"
        )
    elif facts.worktree_present is True and facts.recorded_worktree:
        steps.append(f"git worktree remove {facts.recorded_worktree}")
    steps.append(
        f"# branch {facts.branch or '<lane branch>'} and its commits are PRESERVED: "
        "cleanup never deletes them"
    )
    return tuple(steps)


def summarize_convergences(plans: Sequence[RebootLanePlan]) -> dict:
    """Count plans per convergence verdict (pure) — the snapshot's roll-up.

    Deliberately a count, never a bulk action: the roll-up exists so an operator can see
    that 8 lanes need 4 different rails, not so a single command can be run over all of
    them (Required behavior 5).
    """
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.convergence] = counts.get(plan.convergence, 0) + 1
    return dict(sorted(counts.items()))


__all__ = (
    "CONVERGENCES",
    "CONVERGE_ALREADY_HIBERNATED",
    "CONVERGE_ALREADY_TERMINAL",
    "CONVERGE_BLOCKED",
    "CONVERGE_CLOSE_RESIDUE",
    "CONVERGE_GUARDED_CLOSE",
    "CONVERGE_HIBERNATE",
    "CONVERGE_RESTORE_WORKTREE",
    "CONVERGE_RESUME",
    "CONVERGE_SUPERSEDE",
    "CONVERGE_TERMINALIZE_BOUND",
    "CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND",
    "CONVERGE_TERMINALIZE_UNBOUND",
    "CONVERGE_UNKNOWN",
    "REASON_FOREIGN_OCCUPANT",
    "REASON_HEAD_NOT_INTEGRATED",
    "REASON_HIBERNATED_LIVE_PAIR_PRESENT",
    "REASON_HIBERNATED_RELEASE_UNPROVEN",
    "REASON_INVENTORY_UNREADABLE",
    "REASON_ISSUE_STATE_UNREADABLE",
    "REASON_LANE_OWNS_NO_ISSUE",
    "REASON_RELEASE_IN_FLIGHT",
    "REASON_SUPERSEDED_ROW",
    "REASON_UNKNOWN_LIFECYCLE_DISPOSITION",
    "REASON_UNKNOWN_PROCESS_RELEASE",
    "REASON_WORKTREE_PRESENCE_UNKNOWN",
    "RebootLaneFacts",
    "RebootLanePlan",
    "RebootSlotFact",
    "plan_lane_convergence",
    "slot_fact_from_row",
    "summarize_convergences",
)
