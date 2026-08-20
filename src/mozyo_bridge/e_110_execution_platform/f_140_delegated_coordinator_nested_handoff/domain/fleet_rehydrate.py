"""Governed fleet rehydrate: the pure per-lane plan (Redmine #15745).

A host reboot expires every pane's terminal attestation. ``herdr session-start`` restores
only the **default coordinator pair**; re-forming the three-tier fleet (#15631 j#108474 /
j#108484 measured it) was a manual chain — coordinator restart -> L2 heal -> resume brief ->
L3 heal -> implementation_request re-dispatch. Every step of that chain already exists as a
governed rail; what did not exist was the read-only decision that says, for the *set* of
lanes the manifest calls active, **which** of those rails each lane owes.

This module is that decision, pure. :func:`plan_lane_rehydrate` turns one lane's joined
facts into a typed :class:`FleetLanePlan` naming an ordered, closed set of actions
(:data:`ACTION_HEAL_PAIR` / :data:`ACTION_RESTORE_DISPATCH` / :data:`ACTION_RESUME_BRIEF`),
or a typed skip / block. It performs no I/O and imports nothing that does; the live join and
the actuation live in :mod:`...application.sublane_fleet_rehydrate` and
:mod:`...application.sublane_fleet_rehydrate_ops`.

Relationship to ``sublane reboot-audit`` (Redmine #14499)
--------------------------------------------------------
#14499 deliberately offers **no all-lanes action**: a reboot leaves lanes needing different
answers, so its roll-up is a count. That contract is unchanged and this module does not
touch :func:`...reboot_residue_convergence.plan_lane_convergence`. What #15745 adds is not a
bulk button over one verdict — it is a *second* per-lane decision, on the same per-lane
facts, restricted to the one shape #14499 classifies as ``resume`` / ``hibernate`` (an OPEN
issue on an ACTIVE row) and answering a different question: not "what disposition should
this lane converge to" but "what undelivered action does this lane owe". Every lane whose
answer is not that shape is a typed skip or block here, so the "different lanes, different
answers" property is preserved rather than flattened.

Three properties are load-bearing:

- **Nothing is inferred from a pane, a display cache, or an issue status alone.** Every
  input is a durable authority: the lifecycle row, the lane metadata, git, the Redmine
  open/closed read, the live assigned-name inventory, and the durable delivery record. An
  axis that could not be read is ``None`` and yields a block, never a value.
- **No blind replay.** A dispatch / brief action is planned only when the lane's own durable
  causal key classifies :data:`DISPATCH_OWED` — i.e. the shared
  :mod:`...f_130_handoff_routing.domain.injection_stage` authority says nothing reached the
  receiver. ``uncertain_partial`` (body and/or Enter may have landed) and an unreadable
  ledger are blocks, not retries, and a delivered key is never re-used.
- **The plan is an effect budget of zero.** Producing a plan opens no transaction, reserves
  no fence, and names no destructive step: the whole vocabulary here is additive (adopt or
  launch a pair, re-issue an undelivered anchored send). Nothing in this module can close a
  pane, delete a branch, move a worktree, or write a lifecycle row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_kind import (
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KINDS,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
    REPLACEMENT_NOT_REQUESTED,
    REPLACEMENT_REPLACED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
    RebootLaneFacts,
)

# ---------------------------------------------------------------------------
# Durable dispatch state (closed).
#
# The fold of a lane's own causal key in the durable delivery record. It is NOT a
# receiver-state observation and not a lane-progress verdict: it answers exactly one
# question — "may this exact anchored send be issued now, without duplicating one that
# already reached the receiver?"
# ---------------------------------------------------------------------------

#: Nothing reached the receiver for this causal key: every recorded attempt classified
#: ``not_sent`` under the shared injection-stage authority, or no attempt was ever
#: recorded. This is the ONLY state that licenses issuing the send.
DISPATCH_OWED = "owed"
#: The key's submission was positively confirmed. Re-issuing would duplicate; the lane owes
#: no send on this anchor. (Delivery is an ACK, never task completion.)
DISPATCH_DELIVERED = "delivered"
#: Body and/or Enter may have reached the receiver and submission was not confirmed
#: (``uncertain_partial``). Blind replay is prohibited — this is a block, not a retry.
DISPATCH_UNCERTAIN = "uncertain"
#: The durable delivery authority could not be read. Not observing a delivery is never the
#: same as there being none.
DISPATCH_UNREADABLE = "unreadable"
#: A recorded attempt exists that cannot be attributed to — or ruled out for — the receiver
#: this lane would send to NOW (Redmine #15745 review j#108920 ``finding_generationfence``).
#: Neither "already delivered" nor "owed" is provable, so the lane blocks. Never folded into
#: :data:`DISPATCH_OWED`: an unattributable attempt is exactly the case where re-issuing
#: might duplicate and skipping might strand a fresh generation.
DISPATCH_ATTRIBUTION_UNKNOWN = "attribution_unknown"
#: This lane owes no such send by construction (e.g. no resume brief for a non-delegated
#: lane). Distinct from ``owed`` so an absent obligation never reads as a pending one.
DISPATCH_NOT_APPLICABLE = "not_applicable"

DISPATCH_STATES: frozenset[str] = frozenset(
    {
        DISPATCH_OWED,
        DISPATCH_DELIVERED,
        DISPATCH_UNCERTAIN,
        DISPATCH_UNREADABLE,
        DISPATCH_ATTRIBUTION_UNKNOWN,
        DISPATCH_NOT_APPLICABLE,
    }
)

# ---------------------------------------------------------------------------
# Live startup-screen observation (closed).
#
# Redmine #15745 review j#108920 ``finding_startupinteraction``. A bool could not carry
# this: "no screen is up" and "we could not tell" license opposite actions, and #13760's
# whole lesson is that an unreadable receiver must never decay into a clear one.
# ---------------------------------------------------------------------------

#: No live slot to read (the post-restart main case: a lane with no processes cannot be
#: showing a startup screen). Not a success verdict about anything — just nothing to ask.
STARTUP_SCREEN_NOT_PROBED = "not_probed"
#: Every live slot was read and none matched a declared provider startup blocker.
STARTUP_SCREEN_CLEAR = "clear"
#: A declared startup screen (trust / login / theme) is up on a live slot. The operator owns
#: it in the provider's own UI; mozyo never answers one.
STARTUP_SCREEN_BLOCKED = "blocked"
#: A live slot's visible pane could not be read. Fail-closed, and deliberately NOT
#: :data:`STARTUP_SCREEN_BLOCKED`: it says the question is undecided, never that a screen
#: was shown.
STARTUP_SCREEN_UNREADABLE = "unreadable"
#: A live slot's provider has no profile, so its startup screens cannot be described at all.
STARTUP_SCREEN_UNPROFILED = "unprofiled"

STARTUP_SCREENS: frozenset[str] = frozenset(
    {
        STARTUP_SCREEN_NOT_PROBED,
        STARTUP_SCREEN_CLEAR,
        STARTUP_SCREEN_BLOCKED,
        STARTUP_SCREEN_UNREADABLE,
        STARTUP_SCREEN_UNPROFILED,
    }
)


# ---------------------------------------------------------------------------
# Plan vocabulary (closed).
# ---------------------------------------------------------------------------

#: The lane owes at least one rehydrate action; :attr:`FleetLanePlan.actions` names them.
REHYDRATE = "rehydrate"
#: The lane is deliberately out of this rail's scope, or owes nothing. Never an error.
SKIP = "skip"
#: A fact contradicts the model, or an authority could not be read. Nothing may be actuated.
BLOCKED = "blocked"

DISPOSITIONS_PLANNED: frozenset[str] = frozenset({REHYDRATE, SKIP, BLOCKED})

#: Adopt-or-launch the lane's gateway + worker pair through the existing ``sublane create``
#: rail. Additive by construction: it never closes a surviving slot.
ACTION_HEAL_PAIR = "heal_pair"
#: Re-issue the lane's EXISTING anchored ``implementation_request`` — the same durable
#: anchor, through the same governed primitive. Only ever planned on :data:`DISPATCH_OWED`.
ACTION_RESTORE_DISPATCH = "restore_dispatch"
#: Re-deliver the delegated-coordinator lane's resume pointer, carrying the fixed role
#: profile resolved from the lane's CURRENT durable resume anchor.
ACTION_RESUME_BRIEF = "resume_brief"

ACTIONS: tuple[str, ...] = (
    ACTION_HEAL_PAIR,
    ACTION_RESTORE_DISPATCH,
    ACTION_RESUME_BRIEF,
)

# -- skip reasons (closed) ---------------------------------------------------

#: The lane's issue is durably closed. A closed issue is converged by the retire rails
#: (``sublane reboot-audit`` names which), never rehydrated.
SKIP_ISSUE_CLOSED = "issue_closed"
#: The lifecycle row is terminal.
SKIP_RETIRED = "retired"
#: A recovery successor owns the issue; this row is never a send target again.
SKIP_SUPERSEDED = "superseded"
#: The lane's processes were deliberately released while the issue stayed open. Waking it
#: is ``sublane resume``'s owner-visible decision, not a fleet-wide heal side effect.
SKIP_HIBERNATED = "hibernated"
#: The pair is intact and every causal key the lane owns is delivered: an idle lane with
#: nothing owed. Recorded as a typed skip so "no action" is never an unexplained gap.
SKIP_IDLE = "idle"
#: A project-gateway-bound row. This rail rehydrates issue lanes; a declared project gateway
#: is re-established by its own declaration rail.
SKIP_PROJECT_GATEWAY_BINDING = "project_gateway_binding"
#: The caller's lane filter excluded this lane.
SKIP_FILTERED = "filtered"

SKIP_REASONS: frozenset[str] = frozenset(
    {
        SKIP_ISSUE_CLOSED,
        SKIP_RETIRED,
        SKIP_SUPERSEDED,
        SKIP_HIBERNATED,
        SKIP_IDLE,
        SKIP_PROJECT_GATEWAY_BINDING,
        SKIP_FILTERED,
    }
)

# -- blocked reasons (closed) ------------------------------------------------

#: A lifecycle disposition outside the vocabulary this planner understands.
BLOCK_UNKNOWN_DISPOSITION = "unknown_disposition"
#: A release generation is open, so the lane's slots may be mid-actuation.
BLOCK_RELEASE_IN_FLIGHT = "release_in_flight"
#: A receiver-replacement generation is open on this row.
BLOCK_REPLACEMENT_IN_FLIGHT = "replacement_in_flight"
#: More than one ACTIVE row claims this issue. Resolved by ``sublane supersede``, never by
#: healing one side.
BLOCK_AMBIGUOUS_OWNER = "ambiguous_owner"
#: An active issue-bound row with no issue binding: the Redmine axis is not even askable.
BLOCK_ISSUE_UNBOUND = "issue_unbound"
#: The issue's open/closed state could not be read. Never defaulted to "open".
BLOCK_ISSUE_STATE_UNKNOWN = "issue_state_unknown"
#: The row carries no canonical worktree binding, so no heal can prove which checkout it is.
BLOCK_WORKTREE_UNBOUND = "worktree_unbound"
#: Whether the recorded worktree still exists could not be determined.
BLOCK_WORKTREE_UNREADABLE = "worktree_unreadable"
#: The recorded worktree is gone. Restoring it is ``sublane reboot-audit``'s
#: ``restore_worktree`` rail; a heal must not recreate a checkout under a lane's feet.
BLOCK_WORKTREE_MISSING = "worktree_missing"
#: The lane's branch could not be resolved, or no longer resolves.
BLOCK_BRANCH_UNRESOLVED = "branch_unresolved"
#: The live assigned-name inventory could not be read.
BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"
#: A foreign provider occupies one of the lane's units.
BLOCK_FOREIGN_SLOT = "foreign_slot"
#: The lane's own slots are not uniquely resolvable (duplicate assigned names).
BLOCK_AMBIGUOUS_INVENTORY = "ambiguous_inventory"
#: A provider startup interaction (trust / login / theme) is pending. mozyo never answers a
#: provider UI; the operator does, and then this rail is re-run.
BLOCK_STARTUP_INTERACTION = "startup_interaction_required"
#: A live slot's startup screen could not be classified (unreadable pane, or a provider with
#: no profile). Distinct from :data:`BLOCK_STARTUP_INTERACTION`: that one says a screen WAS
#: shown, this one says the question is undecided. #13760's failure was exactly an
#: unclassifiable receiver being treated as clear.
BLOCK_STARTUP_SCREEN_UNVERIFIED = "startup_screen_unverified"
#: A recorded delivery attempt could not be attributed to the receiver this lane would send
#: to now (review j#108920 ``finding_generationfence``).
BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN = "dispatch_attribution_unknown"
#: The durable delivery authority could not be read for one of the lane's causal keys.
BLOCK_DISPATCH_UNREADABLE = "dispatch_record_unreadable"
#: A causal key classified ``uncertain_partial``: the payload may already be at the
#: receiver. Reconcile the receiver / durable anchor first; never replay.
BLOCK_DISPATCH_UNCERTAIN = "dispatch_uncertain"
#: No durable anchor could be resolved for a send this lane would otherwise owe. A send is
#: never unanchored, and an anchor is never synthesised from prose.
BLOCK_DISPATCH_ANCHOR_UNRESOLVED = "dispatch_anchor_unresolved"
#: A delegated-coordinator lane whose current durable resume anchor could not be resolved.
BLOCK_RESUME_ANCHOR_UNRESOLVED = "resume_anchor_unresolved"
#: A delegated-coordinator lane whose fixed role profile cannot be carried complete
#: (parent / child project, parent callback target, parent issue). A partially resolved
#: delegation contract is worse than none: it reads as authoritative.
BLOCK_RESUME_PROFILE_INCOMPLETE = "resume_profile_incomplete"
#: The row's ``lane_kind`` is present but outside the canonical vocabulary.
BLOCK_LANE_KIND_INVALID = "lane_kind_invalid"
#: Action time only: the lifecycle row moved between the plan and the effect.
BLOCK_LANE_MOVED = "lane_moved"

BLOCK_REASONS: frozenset[str] = frozenset(
    {
        BLOCK_UNKNOWN_DISPOSITION,
        BLOCK_RELEASE_IN_FLIGHT,
        BLOCK_REPLACEMENT_IN_FLIGHT,
        BLOCK_AMBIGUOUS_OWNER,
        BLOCK_ISSUE_UNBOUND,
        BLOCK_ISSUE_STATE_UNKNOWN,
        BLOCK_WORKTREE_UNBOUND,
        BLOCK_WORKTREE_UNREADABLE,
        BLOCK_WORKTREE_MISSING,
        BLOCK_BRANCH_UNRESOLVED,
        BLOCK_INVENTORY_UNREADABLE,
        BLOCK_FOREIGN_SLOT,
        BLOCK_AMBIGUOUS_INVENTORY,
        BLOCK_STARTUP_INTERACTION,
        BLOCK_STARTUP_SCREEN_UNVERIFIED,
        BLOCK_DISPATCH_UNREADABLE,
        BLOCK_DISPATCH_UNCERTAIN,
        BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN,
        BLOCK_DISPATCH_ANCHOR_UNRESOLVED,
        BLOCK_RESUME_ANCHOR_UNRESOLVED,
        BLOCK_RESUME_PROFILE_INCOMPLETE,
        BLOCK_LANE_KIND_INVALID,
        BLOCK_LANE_MOVED,
    }
)

#: The lifecycle release states that mean no release generation is in flight.
_SETTLED_RELEASE: frozenset[str] = frozenset({RELEASE_NOT_REQUESTED, RELEASE_RELEASED})
#: The replacement states that mean no receiver replacement is in flight.
_SETTLED_REPLACEMENT: frozenset[str] = frozenset(
    {REPLACEMENT_NOT_REQUESTED, REPLACEMENT_REPLACED}
)

#: The ``delegated_coordinator`` role-profile placeholders THIS RAIL must carry, i.e. the
#: template's placeholder set minus ``redmine_project``, which the send-side resolver
#: auto-fills from the verified workspace-local default
#: (:func:`...f_130_handoff_routing.application.role_profile_field_resolution.resolve_handoff_profile_fields`).
#: Named here rather than derived, because this planner is pure and must not import the
#: template loader — and pinned by a drift guard that asserts
#: ``set(DELEGATED_COORDINATOR_BRIEF_FIELDS) | {redmine_project}`` equals the live template's
#: placeholders, so adding a placeholder upstream fails the guard instead of silently
#: shipping a brief with an unresolved ``<...>`` token in it.
DELEGATED_COORDINATOR_BRIEF_FIELDS: tuple[str, ...] = (
    "parent_project",
    "child_project",
    "parent_callback_target",
    "parent_issue",
)


# ---------------------------------------------------------------------------
# Facts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneDispatchFact:
    """One causal key's durable delivery state (pure value).

    ``anchor_journal`` is the exact durable journal the key is bound to; ``marker`` is the
    canonical landing marker the key was (or would be) sent under. Both are carried so a
    reader can replay the classification, and so an actuation binds to the SAME key the plan
    classified rather than re-deriving one.
    """

    state: str = DISPATCH_NOT_APPLICABLE
    anchor_issue: str = ""
    anchor_journal: str = ""
    marker: str = ""
    #: How many durable attempts were folded (0 == never sent, which is legitimately owed).
    attempts: int = 0
    #: Free-of-free-text token detail for the durable record (empty when nothing to add).
    detail: str = ""

    @property
    def sendable(self) -> bool:
        """May this exact send be issued now without risking a duplicate?"""
        return self.state == DISPATCH_OWED and bool(self.anchor_journal)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "anchor_issue": self.anchor_issue,
            "anchor_journal": self.anchor_journal,
            "marker": self.marker,
            "attempts": self.attempts,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FleetLaneFacts:
    """One lane's joined facts for the rehydrate decision (Redmine #15745).

    Embeds the #14499 four-authority join (:class:`RebootLaneFacts`) rather than restating
    it, so the two surfaces describe exactly the same lanes and the same slot scoping, and
    adds only the axes the rehydrate decision needs that a convergence audit does not:

    - ``lane_kind`` / ``parent_lane_id`` — the durable delegation geometry, read from the
      lifecycle row (generation-bound at declare time), never from a display cache;
    - ``managed_roles`` — the provider roles the lane's pair is expected to occupy, so
      "pair whole" is a positive count rather than the absence of a complaint;
    - ``dispatch`` / ``resume_brief`` — the two causal keys and their durable folds;
    - ``resume_profile_fields`` — the resolved fixed-role-profile field set for a delegated
      coordinator lane (empty for every other kind);
    - ``startup_interaction_pending`` — a provider UI is waiting on a human.
    """

    reboot: RebootLaneFacts
    lane_kind: str = ""
    parent_lane_id: str = ""
    #: The row's receiver-replacement generation. Kept out of :class:`RebootLaneFacts`
    #: because #14499's convergence does not consult it; a rehydrate must, since healing a
    #: pair whose receiver an actuator is mid-way through exchanging would race it.
    replacement_state: str = REPLACEMENT_NOT_REQUESTED
    managed_roles: tuple[str, ...] = ()
    dispatch: LaneDispatchFact = field(default_factory=LaneDispatchFact)
    resume_brief: LaneDispatchFact = field(default_factory=LaneDispatchFact)
    resume_profile_fields: tuple[tuple[str, str], ...] = ()
    #: The lane-level fold of the live startup-screen read, one of
    #: :data:`STARTUP_SCREENS`. A closed token rather than a bool because "no screen" and
    #: "could not tell" license opposite actions (review j#108920
    #: ``finding_startupinteraction``).
    startup_screen: str = STARTUP_SCREEN_NOT_PROBED

    # -- convenience projections of the embedded join ------------------------

    @property
    def workspace_id(self) -> str:
        return self.reboot.workspace_id

    @property
    def lane_id(self) -> str:
        return self.reboot.lane_id

    @property
    def issue_id(self) -> str:
        return self.reboot.issue_id

    @property
    def lane_generation(self) -> int:
        return self.reboot.lane_generation

    @property
    def revision(self) -> int:
        return self.reboot.revision

    @property
    def is_delegated_coordinator(self) -> bool:
        return self.lane_kind == LANE_KIND_DELEGATED_COORDINATOR

    def live_roles(self) -> tuple[str, ...]:
        """The managed roles backed by exactly one live agent slot (pure)."""
        slots = self.reboot.slots or ()
        live: list[str] = []
        for role in self.managed_roles:
            matching = [s for s in slots if s.role == role and s.is_live_agent]
            if len(matching) == 1:
                live.append(role)
        return tuple(live)

    @property
    def pair_whole(self) -> bool:
        """Is every expected managed role backed by exactly one live agent?

        ``False`` for an empty ``managed_roles``: a lane whose expected roles could not be
        resolved has not been shown to be whole. The caller blocks on that separately; this
        property never reports "whole" from an absence of expectations.
        """
        return bool(self.managed_roles) and len(self.live_roles()) == len(
            self.managed_roles
        )

    def as_payload(self) -> dict:
        # `recorded_worktree` is dropped rather than copied: this payload is the pasteable
        # durable-record shape, and a host-local absolute worktree path may not appear in a
        # journal (`vibes/docs/rules/public-private-boundary.md`). Its three derived facts
        # (`bound` / `worktree_present` / `worktree_identity`) carry every axis the plan
        # actually decided on, so nothing is lost for replay.
        reboot = self.reboot.as_payload()
        reboot.pop("recorded_worktree", None)
        return {
            **reboot,
            "lane_kind": self.lane_kind,
            "parent_lane_id": self.parent_lane_id,
            "replacement_state": self.replacement_state,
            "managed_roles": list(self.managed_roles),
            "live_roles": list(self.live_roles()),
            "pair_whole": self.pair_whole,
            "dispatch": self.dispatch.as_payload(),
            "resume_brief": self.resume_brief.as_payload(),
            "resume_profile_fields": [
                {"field": k, "resolved": bool(v)}
                for k, v in self.resume_profile_fields
            ],
            "startup_screen": self.startup_screen,
        }


@dataclass(frozen=True)
class FleetLanePlan:
    """The typed per-lane rehydrate plan (Redmine #15745 acceptance 1 / 5).

    ``disposition`` is the primary verdict (:data:`REHYDRATE` / :data:`SKIP` /
    :data:`BLOCKED`); ``actions`` is the ordered, closed action set a
    :data:`REHYDRATE` lane owes and is empty for every other disposition. ``reason`` carries
    the closed skip / block token — never a sentence — so a consumer matches on it.
    """

    workspace_id: str
    lane_id: str
    issue_id: str
    disposition: str
    actions: tuple[str, ...] = ()
    reason: str = ""
    detail: str = ""
    lane_kind: str = ""
    lane_generation: int = 1
    revision: int = 1
    pair_whole: bool = False
    live_roles: tuple[str, ...] = ()
    dispatch_state: str = DISPATCH_NOT_APPLICABLE
    resume_brief_state: str = DISPATCH_NOT_APPLICABLE
    dispatch_anchor_journal: str = ""
    resume_anchor_journal: str = ""

    @property
    def actionable(self) -> bool:
        return self.disposition == REHYDRATE and bool(self.actions)

    def has(self, action: str) -> bool:
        return action in self.actions

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "issue_id": self.issue_id,
            "disposition": self.disposition,
            "actions": list(self.actions),
            "reason": self.reason,
            "detail": self.detail,
            "lane_kind": self.lane_kind,
            "lane_generation": self.lane_generation,
            "revision": self.revision,
            "pair_whole": self.pair_whole,
            "live_roles": list(self.live_roles),
            "dispatch_state": self.dispatch_state,
            "resume_brief_state": self.resume_brief_state,
            "dispatch_anchor_journal": self.dispatch_anchor_journal,
            "resume_anchor_journal": self.resume_anchor_journal,
        }


# ---------------------------------------------------------------------------
# The decision.
# ---------------------------------------------------------------------------


def _plan(
    facts: FleetLaneFacts,
    disposition: str,
    *,
    actions: Sequence[str] = (),
    reason: str = "",
    detail: str = "",
) -> FleetLanePlan:
    return FleetLanePlan(
        workspace_id=facts.workspace_id,
        lane_id=facts.lane_id,
        issue_id=facts.issue_id,
        disposition=disposition,
        actions=tuple(actions),
        reason=reason,
        detail=detail,
        lane_kind=facts.lane_kind,
        lane_generation=facts.lane_generation,
        revision=facts.revision,
        pair_whole=facts.pair_whole,
        live_roles=facts.live_roles(),
        dispatch_state=facts.dispatch.state,
        resume_brief_state=facts.resume_brief.state,
        dispatch_anchor_journal=facts.dispatch.anchor_journal,
        resume_anchor_journal=facts.resume_brief.anchor_journal,
    )


def _skip(facts: FleetLaneFacts, reason: str, detail: str = "") -> FleetLanePlan:
    return _plan(facts, SKIP, reason=reason, detail=detail)


def _blocked(facts: FleetLaneFacts, reason: str, detail: str = "") -> FleetLanePlan:
    return _plan(facts, BLOCKED, reason=reason, detail=detail)


def _lifecycle_scope_verdict(facts: FleetLaneFacts) -> Optional[FleetLanePlan]:
    """Is this row in scope at all? (disposition / binding / generation settledness)"""
    reboot = facts.reboot
    if reboot.binding_kind != BINDING_KIND_ISSUE:
        return _skip(
            facts,
            SKIP_PROJECT_GATEWAY_BINDING,
            "a declared project gateway is re-established by its own declaration rail",
        )
    disposition = reboot.lane_disposition
    if disposition == DISPOSITION_RETIRED:
        return _skip(facts, SKIP_RETIRED)
    if disposition == DISPOSITION_SUPERSEDED:
        return _skip(facts, SKIP_SUPERSEDED)
    if disposition == DISPOSITION_HIBERNATED:
        return _skip(
            facts,
            SKIP_HIBERNATED,
            "waking a hibernated lane is `sublane resume`, not a fleet heal side effect",
        )
    if disposition != DISPOSITION_ACTIVE:
        # Every recognised non-active disposition returned a typed skip above, so anything
        # reaching here is outside the vocabulary this planner understands (a legacy /
        # hand-edited / newer-schema value). It is never treated as active by default.
        return _blocked(facts, BLOCK_UNKNOWN_DISPOSITION, f"disposition={disposition!r}")
    if reboot.process_release not in _SETTLED_RELEASE:
        return _blocked(
            facts, BLOCK_RELEASE_IN_FLIGHT, f"process_release={reboot.process_release!r}"
        )
    if facts.replacement_state not in _SETTLED_REPLACEMENT:
        return _blocked(
            facts,
            BLOCK_REPLACEMENT_IN_FLIGHT,
            f"replacement_state={facts.replacement_state!r}",
        )
    if reboot.peer_active_lanes:
        return _blocked(
            facts,
            BLOCK_AMBIGUOUS_OWNER,
            f"{len(reboot.peer_active_lanes) + 1} active owners for this issue",
        )
    if facts.lane_kind and facts.lane_kind not in LANE_KINDS:
        return _blocked(facts, BLOCK_LANE_KIND_INVALID, f"lane_kind={facts.lane_kind!r}")
    return None


def _issue_verdict(facts: FleetLaneFacts) -> Optional[FleetLanePlan]:
    reboot = facts.reboot
    if not (reboot.issue_id or "").strip():
        return _blocked(facts, BLOCK_ISSUE_UNBOUND)
    if reboot.issue_closed is None:
        return _blocked(facts, BLOCK_ISSUE_STATE_UNKNOWN)
    if reboot.issue_closed:
        return _skip(
            facts,
            SKIP_ISSUE_CLOSED,
            "a closed issue converges through the retire rails, never a rehydrate",
        )
    return None


def _checkout_verdict(facts: FleetLaneFacts) -> Optional[FleetLanePlan]:
    """The lane must still be the checkout it was declared against, or nothing is safe."""
    reboot = facts.reboot
    if not reboot.is_bound:
        return _blocked(facts, BLOCK_WORKTREE_UNBOUND)
    if reboot.worktree_present is None:
        return _blocked(facts, BLOCK_WORKTREE_UNREADABLE)
    if not reboot.worktree_present:
        return _blocked(
            facts,
            BLOCK_WORKTREE_MISSING,
            "restore the recorded worktree first (`sublane reboot-audit` names the rail)",
        )
    if not (reboot.branch or "").strip() or reboot.branch_exists is not True:
        return _blocked(
            facts,
            BLOCK_BRANCH_UNRESOLVED,
            f"branch={reboot.branch or '-'} exists={reboot.branch_exists}",
        )
    return None


def _inventory_verdict(facts: FleetLaneFacts) -> Optional[FleetLanePlan]:
    reboot = facts.reboot
    if reboot.slots is None:
        return _blocked(facts, BLOCK_INVENTORY_UNREADABLE)
    if not facts.managed_roles:
        return _blocked(
            facts,
            BLOCK_INVENTORY_UNREADABLE,
            "the lane's expected gateway / worker roles could not be resolved",
        )
    foreign = [s.assigned_name for s in reboot.slots if s.foreign]
    if foreign:
        return _blocked(
            facts, BLOCK_FOREIGN_SLOT, f"{len(foreign)} foreign occupant(s) in the unit"
        )
    for role in facts.managed_roles:
        matching = [s for s in reboot.slots if s.role == role and s.is_live_agent]
        if len(matching) > 1:
            return _blocked(
                facts,
                BLOCK_AMBIGUOUS_INVENTORY,
                f"{len(matching)} live slots resolve the {role} role",
            )
    if facts.startup_screen == STARTUP_SCREEN_BLOCKED:
        return _blocked(
            facts,
            BLOCK_STARTUP_INTERACTION,
            "a declared provider startup screen is up on a live slot; the operator clears "
            "it in the provider's own UI and re-runs this rail",
        )
    if facts.startup_screen in (
        STARTUP_SCREEN_UNREADABLE,
        STARTUP_SCREEN_UNPROFILED,
    ):
        # #13760: an unclassifiable receiver must never be treated as a clear one. This is
        # a SEPARATE token from "a screen is up" so the refusal names the real cause.
        return _blocked(
            facts,
            BLOCK_STARTUP_SCREEN_UNVERIFIED,
            f"a live slot's startup screen could not be classified ({facts.startup_screen})",
        )
    if facts.startup_screen not in STARTUP_SCREENS:
        return _blocked(
            facts,
            BLOCK_STARTUP_SCREEN_UNVERIFIED,
            f"startup_screen={facts.startup_screen!r} is outside the closed vocabulary",
        )
    return None


def _dispatch_verdict(
    facts: FleetLaneFacts, fact: LaneDispatchFact, *, required: bool
) -> Optional[FleetLanePlan]:
    """Turn one causal key's durable fold into a block, or ``None`` when it is usable.

    ``required`` marks a key the lane definitely owes an anchor for (the delegated
    coordinator's resume brief once its pair has been healed). A non-required key that
    resolves no anchor is simply not planned.
    """
    if fact.state == DISPATCH_UNREADABLE:
        return _blocked(facts, BLOCK_DISPATCH_UNREADABLE, fact.detail)
    if fact.state == DISPATCH_ATTRIBUTION_UNKNOWN:
        return _blocked(
            facts,
            BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN,
            fact.detail
            or "a recorded attempt could not be attributed to the receiver this lane "
            "would send to now; neither delivered nor owed is provable",
        )
    if fact.state == DISPATCH_UNCERTAIN:
        return _blocked(
            facts,
            BLOCK_DISPATCH_UNCERTAIN,
            "a recorded attempt classified uncertain_partial; reconcile before any resend",
        )
    if required and fact.state == DISPATCH_OWED and not fact.anchor_journal:
        return _blocked(facts, BLOCK_DISPATCH_ANCHOR_UNRESOLVED, fact.detail)
    return None


def _resume_brief_verdict(facts: FleetLaneFacts) -> Optional[FleetLanePlan]:
    """Fail closed when a delegated-coordinator brief cannot be carried complete."""
    if not facts.is_delegated_coordinator:
        return None
    if facts.resume_brief.state == DISPATCH_NOT_APPLICABLE:
        return _blocked(
            facts,
            BLOCK_RESUME_ANCHOR_UNRESOLVED,
            "no current durable resume anchor resolves for this delegated coordinator lane",
        )
    resolved = {name for name, value in facts.resume_profile_fields if value}
    missing = [f for f in DELEGATED_COORDINATOR_BRIEF_FIELDS if f not in resolved]
    if missing:
        return _blocked(
            facts,
            BLOCK_RESUME_PROFILE_INCOMPLETE,
            "unresolved role profile fields: " + ", ".join(missing),
        )
    return None


def plan_lane_rehydrate(facts: FleetLaneFacts) -> FleetLanePlan:
    """The typed rehydrate disposition for one lane (pure, fail-closed).

    Ordered so the coarsest scope question is answered first and no later gate can promote
    an out-of-scope lane: lifecycle scope -> issue state -> checkout identity -> live
    inventory -> durable delivery state -> the composed action set. An axis that could not
    be read blocks at the point it is consulted; it never degrades into a value that a later
    gate reads as permission.
    """
    for gate in (
        _lifecycle_scope_verdict,
        _issue_verdict,
        _checkout_verdict,
        _inventory_verdict,
    ):
        verdict = gate(facts)
        if verdict is not None:
            return verdict

    dispatch_block = _dispatch_verdict(facts, facts.dispatch, required=False)
    if dispatch_block is not None:
        return dispatch_block
    brief_block = _dispatch_verdict(facts, facts.resume_brief, required=False)
    if brief_block is not None:
        return brief_block

    actions: list[str] = []
    if not facts.pair_whole:
        actions.append(ACTION_HEAL_PAIR)
    if facts.dispatch.sendable:
        actions.append(ACTION_RESTORE_DISPATCH)
    if facts.is_delegated_coordinator and facts.resume_brief.sendable:
        actions.append(ACTION_RESUME_BRIEF)

    if ACTION_RESUME_BRIEF in actions or (
        facts.is_delegated_coordinator and ACTION_HEAL_PAIR in actions
    ):
        # A healed delegated-coordinator pair is a COLD restart: its provider context is
        # gone, so the lane cannot resume from conversation state. The brief is therefore
        # obligatory whenever we relaunch one — and an obligatory brief that cannot be
        # carried complete is a block, not a partial send.
        brief_verdict = _resume_brief_verdict(facts)
        if brief_verdict is not None:
            return brief_verdict
        anchor_verdict = _dispatch_verdict(facts, facts.resume_brief, required=True)
        if anchor_verdict is not None:
            return anchor_verdict
        if ACTION_RESUME_BRIEF not in actions:
            # The pair is being relaunched but the brief's own key is already delivered:
            # the anchor has not moved since the last brief, so re-sending it would
            # duplicate. Name it rather than silently healing a lane that will wake up
            # with no instructions.
            return _blocked(
                facts,
                BLOCK_RESUME_ANCHOR_UNRESOLVED,
                "the delegated coordinator pair needs a relaunch, but its current resume "
                "anchor is already delivered; record a fresh resume anchor first",
            )

    if not actions:
        return _skip(
            facts,
            SKIP_IDLE,
            "pair intact and every durable causal key delivered",
        )
    return _plan(facts, REHYDRATE, actions=actions)


def summarize_rehydrate(plans: Sequence[FleetLanePlan]) -> dict:
    """Count-only roll-up: dispositions, action counts, and the reason histogram."""
    dispositions: dict[str, int] = {}
    actions: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for plan in plans:
        dispositions[plan.disposition] = dispositions.get(plan.disposition, 0) + 1
        for action in plan.actions:
            actions[action] = actions.get(action, 0) + 1
        if plan.reason:
            reasons[plan.reason] = reasons.get(plan.reason, 0) + 1
    return {
        "lane_count": len(plans),
        "dispositions": dispositions,
        "actions": actions,
        "reasons": reasons,
        "actionable": sum(1 for p in plans if p.actionable),
    }


__all__ = (
    "ACTION_HEAL_PAIR",
    "ACTION_RESTORE_DISPATCH",
    "ACTION_RESUME_BRIEF",
    "ACTIONS",
    "BLOCKED",
    "BLOCK_AMBIGUOUS_INVENTORY",
    "BLOCK_AMBIGUOUS_OWNER",
    "BLOCK_BRANCH_UNRESOLVED",
    "BLOCK_DISPATCH_ANCHOR_UNRESOLVED",
    "BLOCK_DISPATCH_ATTRIBUTION_UNKNOWN",
    "BLOCK_DISPATCH_UNCERTAIN",
    "BLOCK_DISPATCH_UNREADABLE",
    "BLOCK_FOREIGN_SLOT",
    "BLOCK_INVENTORY_UNREADABLE",
    "BLOCK_ISSUE_STATE_UNKNOWN",
    "BLOCK_ISSUE_UNBOUND",
    "BLOCK_LANE_KIND_INVALID",
    "BLOCK_LANE_MOVED",
    "BLOCK_REASONS",
    "BLOCK_RELEASE_IN_FLIGHT",
    "BLOCK_REPLACEMENT_IN_FLIGHT",
    "BLOCK_RESUME_ANCHOR_UNRESOLVED",
    "BLOCK_RESUME_PROFILE_INCOMPLETE",
    "BLOCK_STARTUP_INTERACTION",
    "BLOCK_STARTUP_SCREEN_UNVERIFIED",
    "BLOCK_UNKNOWN_DISPOSITION",
    "BLOCK_WORKTREE_MISSING",
    "BLOCK_WORKTREE_UNBOUND",
    "BLOCK_WORKTREE_UNREADABLE",
    "DELEGATED_COORDINATOR_BRIEF_FIELDS",
    "DISPATCH_ATTRIBUTION_UNKNOWN",
    "DISPATCH_DELIVERED",
    "DISPATCH_NOT_APPLICABLE",
    "DISPATCH_OWED",
    "DISPATCH_STATES",
    "DISPATCH_UNCERTAIN",
    "DISPATCH_UNREADABLE",
    "FleetLaneFacts",
    "FleetLanePlan",
    "LaneDispatchFact",
    "REHYDRATE",
    "SKIP",
    "SKIP_FILTERED",
    "SKIP_HIBERNATED",
    "SKIP_IDLE",
    "SKIP_ISSUE_CLOSED",
    "SKIP_PROJECT_GATEWAY_BINDING",
    "SKIP_REASONS",
    "SKIP_RETIRED",
    "SKIP_SUPERSEDED",
    "STARTUP_SCREENS",
    "STARTUP_SCREEN_BLOCKED",
    "STARTUP_SCREEN_CLEAR",
    "STARTUP_SCREEN_NOT_PROBED",
    "STARTUP_SCREEN_UNPROFILED",
    "STARTUP_SCREEN_UNREADABLE",
    "plan_lane_rehydrate",
    "summarize_rehydrate",
)
