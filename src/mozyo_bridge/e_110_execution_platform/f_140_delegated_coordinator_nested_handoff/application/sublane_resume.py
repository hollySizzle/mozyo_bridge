"""`mozyo-bridge sublane resume` — bring a hibernated lane back to active (Redmine #13682).

The inverse of ``sublane hibernate``: a lane whose processes were released while its issue
stayed open is brought back to ``active`` once a **fresh** managed pair has been relaunched
on the same lane / worktree (Design Answer j#76629 Q4, Implementation Request j#77485).

Resume is deliberately a *verify + flip*, never a launch. The fresh gateway/worker pair is
minted by the existing actuator (``sublane start`` on the preserved worktree — its
``declare_active`` re-run is refused ``already_declared`` idempotently, so the hibernated
lifecycle row is untouched), exactly as the recovery successor is launched separately in
``sublane supersede``. Resume's job is the fail-closed gate on that fresh pair and the
disposition CAS:

1. **preflight (fail-closed)** — the lane is ``hibernated`` and owns this issue; its release
   generation is settled (``not_requested`` / ``released`` — never resume onto a lane whose
   panes an actuator is still closing); the issue was not re-owned by another lane while it
   slept; and the relaunched pair is **both-slots live, generation-matched attested, AND
   self-attested after the lane hibernated** (#13637 locator-bound startup self-attestation
   plus a hibernation timestamp anchor). The locator pin alone is *not* sufficient: a pane
   that **survived** the release keeps its tmux pane-id and would still match its own
   pre-hibernate attestation — so the fresh-pair proof also requires the self-attestation's
   ``observed_at`` to post-date the lane's hibernation, which a genuine relaunch satisfies
   and a survivor never does (correcting the design's "the locator IS the generation",
   Q4 — true only for a *killed-and-relaunched* pane, not a survived one).
2. **commit point** — :meth:`LaneLifecycleStore.transition_disposition` CAS-moves the lane
   ``hibernated -> active``, clearing the (finished) release generation on rehydrate. The
   substrate refuses the rehydrate while a generation is still in flight (R1-F3) and refuses
   a second active owner (owner index) — belt-and-suspenders behind the preflight gates.

Resume closes nothing, launches nothing, and touches no worktree / branch / issue / commit.
Default is preflight only; ``--execute`` performs the flip. Idempotent when already active.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    emit_lifecycle_migration_advisory,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    OWNER_RESOLVED,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    evaluate_pair_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)

# Blocked-reason vocabulary (fail-closed preflight).
BLOCK_NOT_HIBERNATED = "lane_not_hibernated"
BLOCK_RELEASE_IN_FLIGHT = "release_generation_in_flight"
BLOCK_ISSUE_REOWNED = "issue_reowned_by_another_lane"
BLOCK_PAIR_SLOTS = "pair_not_both_slots_live"
BLOCK_PAIR_ATTESTATION = "pair_not_attested"


# ---------------------------------------------------------------------------
# Pure preflight decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumePreflight:
    """The fail-closed inputs + verdict of a resume preflight (pure)."""

    lane_hibernated: bool
    release_settled: bool
    issue_not_reowned: bool
    pair_both_slots_live: bool
    pair_attested: bool
    pair_attestation_detail: str = ""

    @property
    def may_resume(self) -> bool:
        return (
            self.lane_hibernated
            and self.release_settled
            and self.issue_not_reowned
            and self.pair_both_slots_live
            and self.pair_attested
        )

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.lane_hibernated:
            reasons.append(BLOCK_NOT_HIBERNATED)
        if not self.release_settled:
            reasons.append(BLOCK_RELEASE_IN_FLIGHT)
        if not self.issue_not_reowned:
            reasons.append(BLOCK_ISSUE_REOWNED)
        if not self.pair_both_slots_live:
            reasons.append(BLOCK_PAIR_SLOTS)
        elif not self.pair_attested:
            reasons.append(BLOCK_PAIR_ATTESTATION)
        return tuple(reasons)

    def as_payload(self) -> dict[str, Any]:
        return {
            "may_resume": self.may_resume,
            "lane_hibernated": self.lane_hibernated,
            "release_settled": self.release_settled,
            "issue_not_reowned": self.issue_not_reowned,
            "pair_both_slots_live": self.pair_both_slots_live,
            "pair_attested": self.pair_attested,
            "pair_attestation_detail": self.pair_attestation_detail,
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class ResumeOutcome:
    """The full result: preflight verdict and the disposition commit."""

    executed: bool
    preflight: ResumePreflight
    issue: str
    lane: str
    already_active: bool = False
    transition: Optional[CasOutcome] = None
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        if self.already_active:
            return False
        if not self.preflight.may_resume:
            return True
        # A commit that was attempted but not applied (a lost CAS race / rehydrate refusal)
        # is a block.
        if self.executed and self.transition is not None and not self.transition.applied:
            return True
        return False

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "issue": self.issue,
            "lane": self.lane,
            "already_active": self.already_active,
            "is_blocked": self.is_blocked,
            "preflight": self.preflight.as_payload(),
            "transition": (
                {"applied": self.transition.applied, "reason": self.transition.reason,
                 "revision": self.transition.revision}
                if self.transition is not None
                else None
            ),
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Injected IO port + live adapter.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneResumeOps(Protocol):
    """Every side effect the resume use case needs, injected so tests drive fakes.

    Read-only over the live world — resume closes nothing and launches nothing.
    """

    def workspace_id(self) -> str: ...

    def live_rows(self) -> Sequence[Mapping[str, object]]: ...

    def read_attestation(
        self, assigned_name: str
    ) -> Optional[IdentityAttestationRecord]: ...


@dataclass
class LiveSublaneResumeOps:
    """Live adapter: project workspace segment + live herdr inventory + attestation read."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def workspace_id(self) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            herdr_workspace_segment,
        )

        try:
            return herdr_workspace_segment(self.repo_root)
        except (OSError, ValueError):
            return ""

    def live_rows(self) -> Sequence[Mapping[str, object]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        try:
            return list_herdr_agent_rows(self.env)
        except Exception:  # noqa: BLE001 — inventory unavailable -> no live slots (fail closed)
            return ()

    def read_attestation(
        self, assigned_name: str
    ) -> Optional[IdentityAttestationRecord]:
        return HerdrIdentityAttestationStore().read(assigned_name)


# ---------------------------------------------------------------------------
# Use case.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeRequest:
    issue: str
    lane: str
    journal: str


@dataclass
class SublaneResumeUseCase:
    """Preflight (fresh attested pair) + disposition CAS (hibernated -> active)."""

    ops: SublaneResumeOps
    store: LaneLifecycleStore

    def _decision(self, request: ResumeRequest) -> Optional[DecisionPointer]:
        try:
            return DecisionPointer(
                source="redmine",
                issue_id=_norm(request.issue),
                journal_id=_norm(request.journal),
            )
        except DecisionPointerError:
            return None

    def run(self, request: ResumeRequest, *, execute: bool) -> ResumeOutcome:
        issue = _norm(request.issue)
        lane = _norm(request.lane)
        workspace_id = _norm(self.ops.workspace_id())

        # A malformed identity / anchor can address nothing — fail closed before any read.
        decision = self._decision(request)
        if not issue or not lane or not workspace_id or decision is None:
            preflight = ResumePreflight(
                lane_hibernated=False,
                release_settled=False,
                issue_not_reowned=False,
                pair_both_slots_live=False,
                pair_attested=False,
                pair_attestation_detail="identity / decision anchor incomplete",
            )
            return ResumeOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail="incomplete resume identity or decision anchor",
            )

        key = LaneLifecycleKey(workspace_id, lane)

        try:
            rec = self.store.get(key)
            owner = self.store.resolve_owner(workspace_id, issue)
        except (LaneLifecycleError, OSError):
            preflight = ResumePreflight(
                lane_hibernated=False,
                release_settled=False,
                issue_not_reowned=False,
                pair_both_slots_live=False,
                pair_attested=False,
                pair_attestation_detail="lifecycle store unreadable",
            )
            return ResumeOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail="lifecycle store unreadable; fail closed",
            )

        # Idempotent: the lane is already the active owner. Resume already ran (or the lane
        # never hibernated) — a no-op, not a block.
        already_active = (
            rec is not None
            and rec.lane_disposition == DISPOSITION_ACTIVE
            and rec.issue_id == issue
            and owner.status == OWNER_RESOLVED
            and owner.lane_id == lane
        )
        if already_active:
            preflight = ResumePreflight(
                lane_hibernated=False,
                release_settled=True,
                issue_not_reowned=True,
                pair_both_slots_live=True,
                pair_attested=True,
                pair_attestation_detail="lane already active",
            )
            return ResumeOutcome(
                executed=execute,
                preflight=preflight,
                issue=issue,
                lane=lane,
                already_active=True,
                detail="lane already active; nothing to resume",
            )

        lane_hibernated = (
            rec is not None
            and rec.lane_disposition == DISPOSITION_HIBERNATED
            and rec.issue_id == issue
        )
        # Never resume onto a lane whose release generation is still in flight: its panes
        # may still be closing, and a lingering pre-hibernate pane could masquerade as the
        # fresh pair. Only a settled generation (never opened, or fully released) resumes.
        release_settled = rec is not None and rec.process_release in (
            RELEASE_NOT_REQUESTED,
            RELEASE_RELEASED,
        )
        # While it slept another lane may have taken the issue (a fresh declare_active).
        # Coming back as a second active owner is the state the owner index forbids — block
        # with a clear reason (the CAS is the backstop).
        issue_not_reowned = owner.status != OWNER_RESOLVED or owner.lane_id == lane

        rows = self.ops.live_rows()
        # The hibernation timestamp anchors the freshness gate: only a pair self-attested
        # AFTER the lane hibernated is a genuine relaunch (a survivor's record predates it).
        hibernation_anchor = rec.updated_at if rec is not None else ""
        both_live, attested, attest_detail = evaluate_pair_attestation(
            rows,
            workspace_id,
            lane,
            self.ops.read_attestation,
            fresh_after=hibernation_anchor,
        )
        preflight = ResumePreflight(
            lane_hibernated=lane_hibernated,
            release_settled=release_settled,
            issue_not_reowned=issue_not_reowned,
            pair_both_slots_live=both_live,
            pair_attested=attested,
            pair_attestation_detail=attest_detail,
        )
        if not preflight.may_resume or not execute:
            return ResumeOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                lane=lane,
                detail=(
                    "preflight only (no --execute)"
                    if preflight.may_resume
                    else "fail-closed: resume blocked"
                ),
            )

        # Commit point: CAS hibernated -> active, clearing the settled release generation on
        # rehydrate. Guarded on the lane's exact state + revision and the durable anchor.
        assert rec is not None  # guaranteed by lane_hibernated
        transition = self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=rec.revision,
            target=DISPOSITION_ACTIVE,
            decision=decision,
        )
        # Redmine #13844 R2: resume is a schema-needing mutation — surface any forward migration
        # of the shared store and its active peer-reader risk (same advisory adopt uses).
        emit_lifecycle_migration_advisory(
            getattr(self.store, "last_write_preparation", None), stream=sys.stderr
        )
        if not transition.applied:
            return ResumeOutcome(
                executed=True,
                preflight=preflight,
                issue=issue,
                lane=lane,
                transition=transition,
                detail=f"resume commit refused ({transition.reason})",
            )
        return ResumeOutcome(
            executed=True,
            preflight=preflight,
            issue=issue,
            lane=lane,
            transition=transition,
            detail="lane resumed to active (fresh attested pair)",
        )


# ---------------------------------------------------------------------------
# Text rendering + thin CLI handler.
# ---------------------------------------------------------------------------


def format_resume_text(outcome: ResumeOutcome) -> str:
    lines = [
        f"sublane resume: {outcome.lane} (issue {outcome.issue})",
        f"  may_resume: {outcome.preflight.may_resume} executed: {outcome.executed}",
    ]
    if outcome.already_active:
        lines.append("  lane already active (idempotent no-op)")
    if outcome.is_blocked:
        lines.append(
            "  -> fail-closed blocked: " + ", ".join(outcome.preflight.blocked_reasons)
        )
        if outcome.preflight.pair_attestation_detail:
            lines.append(f"  pair: {outcome.preflight.pair_attestation_detail}")
        if outcome.transition is not None and not outcome.transition.applied:
            lines.append(f"  commit refused: {outcome.transition.reason}")
        return "\n".join(lines)
    if outcome.transition is not None:
        lines.append(
            f"  commit: applied={outcome.transition.applied} "
            f"reason={outcome.transition.reason}"
        )
    if not outcome.executed and outcome.preflight.may_resume:
        lines.append("  (preflight only; re-run with --execute to resume the lane)")
    return "\n".join(lines)


def cmd_sublane_resume(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = ResumeRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
    )
    json_mode = bool(getattr(args, "json", False))
    ops = LiveSublaneResumeOps(repo_root=repo_root, env=dict(os.environ))
    use_case = SublaneResumeUseCase(ops=ops, store=LaneLifecycleStore())
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if json_mode:
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_resume_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_resume_parser(sublane_sub: Any) -> None:
    """Register ``sublane resume`` outside the at-ceiling core CLI module."""
    parser = sublane_sub.add_parser(
        "resume",
        help=(
            "Redmine #13682: verify a fresh managed pair and bring a hibernated "
            "lane back to active. Default is preflight only."
        ),
    )
    parser.add_argument(
        "--issue", required=True, help="Redmine issue id the hibernated lane owns"
    )
    parser.add_argument(
        "--lane", required=True, help="Hibernated lane label to resume"
    )
    parser.add_argument(
        "--journal", required=True, help="Redmine journal authorizing the resume"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="CAS hibernated->active after the fresh-pair verification",
    )
    add_repo_option(parser)
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )
    parser.set_defaults(func=cmd_sublane_resume)


__all__ = (
    "BLOCK_ISSUE_REOWNED",
    "BLOCK_NOT_HIBERNATED",
    "BLOCK_PAIR_ATTESTATION",
    "BLOCK_PAIR_SLOTS",
    "BLOCK_RELEASE_IN_FLIGHT",
    "LiveSublaneResumeOps",
    "ResumeOutcome",
    "ResumePreflight",
    "ResumeRequest",
    "SublaneResumeOps",
    "SublaneResumeUseCase",
    "cmd_sublane_resume",
    "format_resume_text",
    "register_sublane_resume_parser",
)
