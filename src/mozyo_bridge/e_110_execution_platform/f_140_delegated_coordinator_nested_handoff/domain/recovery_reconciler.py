"""Host-restart multi-authority recovery reconciler (Redmine #13520 j#75276 / review F4).

The full recovery reconciler design j#75276 required — the stale-slot classifier
(:func:`...herdr_slot_liveness.classify_named_slot`, commit ``33e3190``) was only the runtime
liveness axis of it. This module is the **read-only recovery plan** that j#75276 mandated: it
reconciles the several sources of truth (never a single DB) and emits a fail-closed, never-clobber
plan whose apply steps are all safe / idempotent. It performs NO I/O and mutates nothing; the
application layer probes the authorities and (only on operator/coordinator authority) executes the
plan's safe steps through existing commands.

Authority matrix (j#75276) — reconciled here, none trusted alone:

- **Redmine issue/journal** — the workflow gate + durable anchor + next action.
- **Git worktree / ref / diff** — code + dirty state (never reset/stash/recreate: never-clobber).
- **registry + repo-local anchor** — stable workspace identity.
- **state DB (callback outbox)** — restore material (pending / uncertain backlog), NOT workflow truth.
- **Herdr assigned-name + live inventory** — runtime identity / liveness (composite, via the
  stale-slot classifier).
- **launch-time sender env** — a process-local input re-attested from the authorities above, never
  a persistent authority.

Fail-closed / never-clobber (j#75276): a workspace mismatch, an unreadable Redmine anchor, an
ambiguous live slot, or a DB-vs-Redmine/Git contradiction STOPS the plan (no apply steps). The
dirty worktree is always preserved. The security-sensitive steps — a stale-slot relaunch and a
verified one-time sender re-attestation — are emitted as steps flagged ``requires_owner_approval``
/ ``requires_verified_reattestation``; the reconciler never auto-executes an identity mutation or a
destructive pane close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

# --- Plan status -----------------------------------------------------------
#: Every authority agrees and nothing needs recovery — resume directly on the durable anchor.
RECOVERY_READY = "ready_to_resume"
#: The authorities are consistent but recovery steps are needed (stale slot / missing env / backlog).
RECOVERY_NEEDS_RECOVERY = "needs_recovery"
#: A contradiction between authorities — STOP (fail-closed); never guess, never clobber.
RECOVERY_FAIL_CLOSED = "fail_closed"

# --- Fail-closed blocker reasons (closed vocabulary) -----------------------
BLOCK_WORKSPACE_MISMATCH = "workspace_mismatch"
BLOCK_ANCHOR_UNREADABLE = "anchor_unreadable"
BLOCK_WORKTREE_ABSENT = "worktree_absent"
BLOCK_AMBIGUOUS_LIVE_SLOT = "ambiguous_live_slot"
BLOCK_DB_CONTRADICTION = "db_authority_contradiction"

# --- Safe / idempotent apply step kinds (closed vocabulary) ----------------
STEP_RELAUNCH_STALE_SLOT = "relaunch_stale_slot"
STEP_REATTEST_SENDER = "reattest_sender_identity"
STEP_RESTART_WATCHER = "restart_watcher"
STEP_REPLAY_OUTBOX = "replay_outbox"
STEP_RESUME_EXACT_JOURNAL = "resume_exact_journal"
STEP_PRESERVE_DIRTY_WORKTREE = "preserve_dirty_worktree"
#: Uncertain rows are NEVER replayed (they may already be injected — #13520 j#75276 / review
#: F2/R2-F4). They are surfaced for an operator/coordinator reconcile against the exact journal +
#: delivery evidence; any resend needs a NEW durable authorization, not a replay of this row.
STEP_RECONCILE_UNCERTAIN = "reconcile_uncertain"


@dataclass(frozen=True)
class RuntimeSlot:
    """One durable-name runtime slot observation (from the Herdr live inventory).

    ``liveness`` is the composite verdict from
    :func:`...herdr_slot_liveness.classify_named_slot` (``live`` / ``stale_named_slot``).
    ``count`` is how many live agents carry this name (``>1`` is an ambiguous slot -> fail-closed).
    """

    name: str
    liveness: str
    count: int = 1


@dataclass(frozen=True)
class AuthorityObservation:
    """A point-in-time read of every recovery authority (pure input; the app layer probes it).

    All fields are plain, redaction-safe values (no absolute paths / credentials / pane ids). The
    reconciler compares them; it never reads them from the environment itself.
    """

    workspace_id_expected: str  # from the durable Redmine anchor / handoff
    workspace_id_registry: str  # from registry + repo-local anchor
    redmine_anchor_readable: bool  # the exact gate journal is readable
    git_worktree_present: bool
    git_dirty: bool
    outbox_present: bool
    outbox_pending: int
    outbox_uncertain: int
    outbox_workspace_id: str = ""  # the workspace the outbox rows belong to ("" = not checked)
    runtime_slots: Sequence[RuntimeSlot] = ()
    sender_env_present: bool = True  # MOZYO_WORKSPACE_ID / MOZYO_AGENT_ROLE present in this process


@dataclass(frozen=True)
class RecoveryStep:
    """One safe / idempotent apply step in the plan (a recommendation, never auto-executed here)."""

    kind: str
    detail: str = ""
    requires_owner_approval: bool = False
    requires_verified_reattestation: bool = False

    def as_payload(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "requires_owner_approval": self.requires_owner_approval,
            "requires_verified_reattestation": self.requires_verified_reattestation,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    """A read-only, fail-closed recovery plan (never-clobber; safe apply steps only)."""

    status: str
    steps: tuple[RecoveryStep, ...] = ()
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the plan is safe to act on (not fail-closed)."""
        return self.status != RECOVERY_FAIL_CLOSED

    def as_payload(self) -> dict:
        return {
            "status": self.status,
            "steps": [s.as_payload() for s in self.steps],
            "blockers": list(self.blockers),
            "notes": list(self.notes),
        }


def build_recovery_plan(obs: AuthorityObservation) -> RecoveryPlan:
    """Reconcile the authorities into a fail-closed, never-clobber recovery plan (pure).

    Order: (1) collect every fail-closed contradiction first — any one stops the plan with no apply
    steps (never guess across a contradiction, never clobber). (2) Otherwise emit the safe /
    idempotent recovery steps the observed state needs, always preserving a dirty worktree.
    """
    blockers: list[str] = []
    # (1) Fail-closed contradictions between authorities.
    if _norm(obs.workspace_id_expected) != _norm(obs.workspace_id_registry):
        blockers.append(BLOCK_WORKSPACE_MISMATCH)
    if not obs.redmine_anchor_readable:
        blockers.append(BLOCK_ANCHOR_UNREADABLE)
    if not obs.git_worktree_present:
        blockers.append(BLOCK_WORKTREE_ABSENT)
    if any(int(s.count) > 1 for s in obs.runtime_slots):
        blockers.append(BLOCK_AMBIGUOUS_LIVE_SLOT)
    if (
        obs.outbox_present
        and _norm(obs.outbox_workspace_id)
        and _norm(obs.outbox_workspace_id) != _norm(obs.workspace_id_expected)
    ):
        # The restore-material DB references a different workspace than the durable anchor:
        # the DB is NOT the workflow authority, so a mismatch is a stop, never a silent adopt.
        blockers.append(BLOCK_DB_CONTRADICTION)

    if blockers:
        return RecoveryPlan(
            status=RECOVERY_FAIL_CLOSED,
            blockers=tuple(blockers),
            notes=(
                "authorities contradict; stop and reconcile manually. The dirty worktree is NOT "
                "reset/stashed/recreated (never-clobber).",
            ),
        )

    # (2) Consistent authorities -> safe / idempotent recovery steps.
    steps: list[RecoveryStep] = []
    notes: list[str] = []
    if obs.git_dirty:
        steps.append(
            RecoveryStep(
                STEP_PRESERVE_DIRTY_WORKTREE,
                detail="uncommitted work is preserved as-is; never reset/stash/recreate",
            )
        )
    for slot in obs.runtime_slots:
        if slot.liveness != "live":
            steps.append(
                RecoveryStep(
                    STEP_RELAUNCH_STALE_SLOT,
                    detail=f"slot {slot.name!r} is shell residue; owner-approved close + same-slot relaunch",
                    requires_owner_approval=True,
                )
            )
    if not obs.sender_env_present:
        steps.append(
            RecoveryStep(
                STEP_REATTEST_SENDER,
                detail="launch-time MOZYO_* env absent; one-time verified re-attestation from "
                "registry/anchor/live assigned-name (sanctioned coordinator path, durable record)",
                requires_verified_reattestation=True,
            )
        )
    # PENDING rows only are claim/deliver-eligible (the send edge was never crossed). UNCERTAIN
    # rows are terminal-until-reconciled: they may already be injected, so they are NEVER replayed
    # (#13520 j#75276 "uncertain send を blind retry しない" / review F2/R2-F4). The UNIQUE fence
    # dedups distinct rows; it does NOT make re-sending the SAME row safe.
    if obs.outbox_present and int(obs.outbox_pending) > 0:
        steps.append(
            RecoveryStep(
                STEP_REPLAY_OUTBOX,
                detail=f"{obs.outbox_pending} pending row(s); claim + deliver-once (pending only — "
                "the send edge was never crossed, so a claim cannot duplicate)",
            )
        )
        steps.append(
            RecoveryStep(
                STEP_RESUME_EXACT_JOURNAL,
                detail="re-read the exact Redmine gate journal (the authority) before delivering",
            )
        )
    if obs.outbox_present and int(obs.outbox_uncertain) > 0:
        steps.append(
            RecoveryStep(
                STEP_RECONCILE_UNCERTAIN,
                detail=f"{obs.outbox_uncertain} uncertain row(s); do NOT replay — reconcile against "
                "the exact journal + delivery evidence; a resend needs a NEW durable authorization",
                requires_owner_approval=True,
            )
        )
    # A watcher restart is safe/idempotent (it re-reads Redmine and only claims PENDING rows).
    # Recommend it when there is automated recovery to drive (pending replay, a relaunched slot, a
    # re-attestation) — NOT for a manual uncertain reconcile or a mere dirty-worktree note (a
    # watcher restart never touches uncertain rows, so it cannot advance that step).
    _non_watcher_triggers = {STEP_PRESERVE_DIRTY_WORKTREE, STEP_RECONCILE_UNCERTAIN}
    if any(s.kind not in _non_watcher_triggers for s in steps):
        steps.append(
            RecoveryStep(
                STEP_RESTART_WATCHER,
                detail="restart the bounded callback watcher (re-reads Redmine on every wake; "
                "claims pending rows only)",
            )
        )

    actionable = [s for s in steps if s.kind != STEP_PRESERVE_DIRTY_WORKTREE]
    if not actionable:
        notes.append("all authorities agree and nothing needs recovery; resume on the durable anchor.")
        return RecoveryPlan(status=RECOVERY_READY, steps=tuple(steps), notes=tuple(notes))
    return RecoveryPlan(status=RECOVERY_NEEDS_RECOVERY, steps=tuple(steps), notes=tuple(notes))


def _norm(value: object) -> str:
    return str(value or "").strip()


__all__ = (
    "RECOVERY_READY",
    "RECOVERY_NEEDS_RECOVERY",
    "RECOVERY_FAIL_CLOSED",
    "BLOCK_WORKSPACE_MISMATCH",
    "BLOCK_ANCHOR_UNREADABLE",
    "BLOCK_WORKTREE_ABSENT",
    "BLOCK_AMBIGUOUS_LIVE_SLOT",
    "BLOCK_DB_CONTRADICTION",
    "STEP_RELAUNCH_STALE_SLOT",
    "STEP_REATTEST_SENDER",
    "STEP_RESTART_WATCHER",
    "STEP_REPLAY_OUTBOX",
    "STEP_RESUME_EXACT_JOURNAL",
    "STEP_PRESERVE_DIRTY_WORKTREE",
    "STEP_RECONCILE_UNCERTAIN",
    "RuntimeSlot",
    "AuthorityObservation",
    "RecoveryStep",
    "RecoveryPlan",
    "build_recovery_plan",
)
