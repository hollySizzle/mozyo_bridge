"""Stale standard-sublane worker recovery — the pure preflight decision (Redmine #13806 tranche D).

The public ``sublane recover-stale`` surface (residual j#79435, Implementation Request
j#79485) lets the current coordinator recover the exact stale standard-sublane worker of a
lane whose worker process vanished after a turn — leaving an Implementation Done / Review
Request diff un-durable-ized — WITHOUT touching the coordinator itself, the lane gateway, or
any foreign slot. This module is the pure half: the closed typed-blocker vocabulary and the
ordered fail-closed classification an action-time preflight makes over a *positive-fact*
observation of the live target.

Every gate is a **positive** fact the observer must confirm; each defaults to the unsafe
side, so a missing / unreadable observation blocks rather than actuates
(:mod:`...replacement_preservation` discipline). The destructive actuation (close → launch →
attest → redispatch) is the tranche A/B store + tranche B actuator; this module never opens
the store, reads the live inventory, or mutates a process — a caller can pin every branch
with tests that touch no process and no DB.

The blockers map one-to-one onto the Implementation Request's required "zero-close typed
blocker" scenarios (j#79485 §2): productive foreground provider / tool-child, unknown /
unresolvable identity, an authority conflict, an unreadable (never a *dirty*) worktree, a
gateway or foreign slot, a wrong issue-lane, and a stale generation.
"""

from __future__ import annotations

from mozyo_bridge.core.state.replacement_transaction_model import norm

# -- recovery preflight verdict vocabulary (a closed set) -----------------------

#: Every gate holds: the target is the exact stale standard-sublane worker the approval
#: names, and a ``--execute`` may proceed to the owner-approval + guarded actuation.
RECOVER_ACTIONABLE = "actionable"

#: The live inventory cannot uniquely resolve the pinned worker identity
#: (workspace / lane / issue / provider / assigned-name / locator) — unreadable or ambiguous.
#: Never degraded to "absent" and launched blind (j#78384 §4).
RECOVER_BLOCK_UNKNOWN = "identity_unknown"
#: The pinned slot is NOT a standard sublane worker — it is a lane gateway, the default
#: coordinator / companion, or a foreign slot. The recovery closes only the exact worker
#: (j#79485 §3): a gateway / foreign slot is protected, zero actuation.
RECOVER_BLOCK_GATEWAY_OR_FOREIGN = "gateway_or_foreign_protected"
#: The lane's durable issue owner does not match the approval's issue-lane — a stale approval
#: acting on a lane the coordinator has since re-owned. Zero actuation.
RECOVER_BLOCK_WRONG_ISSUE_LANE = "wrong_issue_lane"
#: The live worker's revision / generation no longer matches the approved generation — a
#: newer generation superseded this approval (or the slot was recycled). Zero actuation.
RECOVER_BLOCK_STALE_GENERATION = "stale_generation"
#: The pinned slot is a live *productive* foreground provider or an active tool-child — it is
#: doing work, not shell residue. Recovering it would destroy an in-flight turn; zero actuation.
RECOVER_BLOCK_PRODUCTIVE = "productive_provider_or_tool_child"
#: The slot does not present the positive shell-residue signal (``stale_named_slot``) — it is
#: not actually stale. Only a genuine residue is recovered (never a live worker); zero actuation.
RECOVER_BLOCK_NOT_STALE = "not_stale"
#: The worker's worktree state cannot be read — byte preservation requires a readable worktree
#: (a *dirty* worktree is fine and is preserved; an UNREADABLE one is not). Zero actuation.
RECOVER_BLOCK_DIRTY_UNREADABLE = "dirty_state_unreadable"
#: Another replacement authority (a different approved generation / in-flight transaction) is
#: already acting on this slot — never race two authorities. Zero actuation.
RECOVER_BLOCK_AUTHORITY_CONFLICT = "authority_conflict"

RECOVER_VERDICTS = frozenset(
    {
        RECOVER_ACTIONABLE,
        RECOVER_BLOCK_UNKNOWN,
        RECOVER_BLOCK_GATEWAY_OR_FOREIGN,
        RECOVER_BLOCK_WRONG_ISSUE_LANE,
        RECOVER_BLOCK_STALE_GENERATION,
        RECOVER_BLOCK_PRODUCTIVE,
        RECOVER_BLOCK_NOT_STALE,
        RECOVER_BLOCK_DIRTY_UNREADABLE,
        RECOVER_BLOCK_AUTHORITY_CONFLICT,
    }
)

#: The verdicts that forbid any actuation (everything but :data:`RECOVER_ACTIONABLE`).
RECOVER_BLOCKERS = frozenset(RECOVER_VERDICTS - {RECOVER_ACTIONABLE})


class RecoveryObservation:
    """The action-time facts a preflight observes about the pinned stale worker.

    Every field is a **positive** fact defaulting to the unsafe side (``False``), so a
    missing / unreadable observation fails closed at :func:`decide_recovery`:

    - ``identity_resolved`` — the live inventory resolves EXACTLY one slot at the pinned
      ``(workspace, lane, issue, provider, assigned_name, locator)`` (never ambiguous /
      unreadable).
    - ``is_standard_sublane_worker`` — that slot is a standard sublane *worker* (not a lane
      gateway, not the default coordinator / companion, not a foreign slot).
    - ``issue_lane_matches`` — the lane's durable issue owner matches the approval's issue.
    - ``generation_matches`` — the live slot's revision / generation matches the approved
      generation (a same-name recycle at a different generation does NOT match).
    - ``not_productive`` — the slot is NOT a live productive foreground provider or an active
      tool-child (it is not doing work).
    - ``is_stale`` — the slot presents the positive ``stale_named_slot`` shell-residue signal.
    - ``worktree_readable`` — the worker's worktree state can be read (dirty or clean; only an
      *unreadable* one blocks — a dirty one is preserved).
    - ``no_authority_conflict`` — no other approved generation / in-flight replacement
      transaction is already acting on this slot.
    """

    __slots__ = (
        "identity_resolved",
        "is_standard_sublane_worker",
        "issue_lane_matches",
        "generation_matches",
        "not_productive",
        "is_stale",
        "worktree_readable",
        "no_authority_conflict",
    )

    def __init__(
        self,
        *,
        identity_resolved: bool = False,
        is_standard_sublane_worker: bool = False,
        issue_lane_matches: bool = False,
        generation_matches: bool = False,
        not_productive: bool = False,
        is_stale: bool = False,
        worktree_readable: bool = False,
        no_authority_conflict: bool = False,
    ) -> None:
        self.identity_resolved = bool(identity_resolved)
        self.is_standard_sublane_worker = bool(is_standard_sublane_worker)
        self.issue_lane_matches = bool(issue_lane_matches)
        self.generation_matches = bool(generation_matches)
        self.not_productive = bool(not_productive)
        self.is_stale = bool(is_stale)
        self.worktree_readable = bool(worktree_readable)
        self.no_authority_conflict = bool(no_authority_conflict)

    def as_payload(self) -> dict[str, bool]:
        return {
            "identity_resolved": self.identity_resolved,
            "is_standard_sublane_worker": self.is_standard_sublane_worker,
            "issue_lane_matches": self.issue_lane_matches,
            "generation_matches": self.generation_matches,
            "not_productive": self.not_productive,
            "is_stale": self.is_stale,
            "worktree_readable": self.worktree_readable,
            "no_authority_conflict": self.no_authority_conflict,
        }


def decide_recovery(observation: RecoveryObservation) -> str:
    """Classify the recovery target. (pure, fail-closed, ordered)

    Returns :data:`RECOVER_ACTIONABLE` only when EVERY gate holds; otherwise the first failing
    gate's closed blocker (checked most-fundamental first) so the durable record names exactly
    which fence stopped the recovery. No gate defaults to the safe side — a missing observation
    blocks.

    The order is deliberate:

    1. identity must resolve at all (nothing else is meaningful without it);
    2. the slot must be a standard worker (protect the gateway / coordinator / foreign slot
       before inspecting anything else about it);
    3. the issue-lane owner must match (a stale approval on a re-owned lane);
    4. the generation must match (a superseded / recycled generation);
    5. the slot must NOT be a live productive provider / tool-child (never destroy in-flight
       work — checked before "is it residue" so a *live* worker never falls through as stale);
    6. the slot must present the positive shell-residue signal (only genuine residue is
       recovered);
    7. the worktree must be readable (byte preservation needs a readable worktree; a dirty one
       is fine);
    8. no competing authority may already be acting on it.
    """
    if not observation.identity_resolved:
        return RECOVER_BLOCK_UNKNOWN
    if not observation.is_standard_sublane_worker:
        return RECOVER_BLOCK_GATEWAY_OR_FOREIGN
    if not observation.issue_lane_matches:
        return RECOVER_BLOCK_WRONG_ISSUE_LANE
    if not observation.generation_matches:
        return RECOVER_BLOCK_STALE_GENERATION
    if not observation.not_productive:
        return RECOVER_BLOCK_PRODUCTIVE
    if not observation.is_stale:
        return RECOVER_BLOCK_NOT_STALE
    if not observation.worktree_readable:
        return RECOVER_BLOCK_DIRTY_UNREADABLE
    if not observation.no_authority_conflict:
        return RECOVER_BLOCK_AUTHORITY_CONFLICT
    return RECOVER_ACTIONABLE


def is_recovery_actionable(verdict: str) -> bool:
    """Does this verdict permit the guarded actuation? (pure)"""
    return norm(verdict) == RECOVER_ACTIONABLE


def stale_worker_recovery_action_id(
    *, lane_id: str, role: str, provider: str, assigned_name: str, locator: str
) -> str:
    """The deterministic action id that names ONE exact stale worker generation. (pure)

    The transaction key's ``action_id`` (Redmine #13806 tranche A) for a worker recovery: a
    ``recover:<lane>:<role>:<provider>:<assigned_name>:<locator>`` token pinned to the exact
    live generation the approval names. Two recoveries of the same worker at the same
    generation share the key (idempotent resume); a different worker / locator is a different
    key. Every component must be present — an under-specified target could never identify one
    exact receiver, so it raises rather than emit an ambiguous id (the ``quarantine_action_id``
    precedent, #13763).
    """
    parts = {
        "lane_id": norm(lane_id),
        "role": norm(role),
        "provider": norm(provider),
        "assigned_name": norm(assigned_name),
        "locator": norm(locator),
    }
    missing = [name for name, value in parts.items() if not value]
    if missing:
        raise ValueError(
            "a stale worker recovery action id requires a non-empty lane_id / role / "
            f"provider / assigned_name / locator (missing: {', '.join(missing)})"
        )
    return "recover:" + ":".join(
        parts[name] for name in ("lane_id", "role", "provider", "assigned_name", "locator")
    )


__all__ = (
    "RECOVER_ACTIONABLE",
    "RECOVER_BLOCK_UNKNOWN",
    "RECOVER_BLOCK_GATEWAY_OR_FOREIGN",
    "RECOVER_BLOCK_WRONG_ISSUE_LANE",
    "RECOVER_BLOCK_STALE_GENERATION",
    "RECOVER_BLOCK_PRODUCTIVE",
    "RECOVER_BLOCK_NOT_STALE",
    "RECOVER_BLOCK_DIRTY_UNREADABLE",
    "RECOVER_BLOCK_AUTHORITY_CONFLICT",
    "RECOVER_VERDICTS",
    "RECOVER_BLOCKERS",
    "RecoveryObservation",
    "decide_recovery",
    "is_recovery_actionable",
    "stale_worker_recovery_action_id",
)
