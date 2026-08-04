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
   past the applicable generation fence**. Before Redmine #14756 that fence was the
   timestamp + released-locator pair described below. A lane with a minted authority-grade
   epoch instead uses the exact attested epoch as the generation proof, so clock and locator
   evidence are no longer consulted. The locator pin alone is *not* sufficient: a pane that
   **survived** the release keeps its tmux pane-id and would still match its own
   pre-hibernate attestation. On the legacy path, a self-attestation ``observed_at`` that
   post-dates the lane's hibernation is required as a LIVENESS boundary — it is NOT the
   generation proof, because no timestamp can be one (review j#94531 R2-F1: a backdated CAS
   stamp, a regressed host clock and a self-written ``observed_at`` each defeat it). That hibernation
   timestamp is the **immutable hibernate-transition stamp** (``hibernated_at``, schema v8),
   not the generic lifecycle ``updated_at``: Redmine #14477 measured a metadata-only
   ``repair-pins`` moving the mutable column past the self-attestation of the exact live pair
   it had just verified, which refused that pair ``stale_generation`` until an operator
   glass-break. A row carrying NO such stamp (a pre-v8 / older-build hibernation) has no
   boundary at all, and the freshness half then fails CLOSED — no other column stands in for
   it. See :mod:`mozyo_bridge.core.state.lane_hibernation_anchor`.

   **The timestamp is a liveness boundary, NOT the generation proof** (coordinator disposition
   j#94544 A.3). Review j#94531 R2-F1 showed no clock can carry that proof: a backdated CAS
   stamp, a regressed host clock, and a self-written ``observed_at`` each defeat it. The
   pre-#14756 generation proof was the CLOCK-INDEPENDENT released-locator fence — a survivor
   keeps the pane-id hibernate's release closed, a relaunch does not
   (:mod:`mozyo_bridge.core.state.lane_released_locator_fence`). Absent / unreadable /
   incomplete release evidence REFUSES (A.2), and a recycled pane-id yields a false refusal in
   the safe direction (A.4). Resume needs that fence AND every existing attestation / provider
   / generation / declared-pin fence. Redmine #14756 replaced only the timestamp and
   released-locator generation proof with an authority-grade lane epoch; identity, provider,
   multiplicity, declared-pin, release-settled, ownership, and CAS fences remain unchanged.
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
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_hibernation_anchor import (
    ANCHOR_HIBERNATE_TRANSITION,
    resume_freshness_anchor,
)
from mozyo_bridge.core.state.lane_epoch import EPOCH_OK, required_resume_epoch
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
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.core.state.lane_released_locator_fence import (
    released_locator_verdict,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
    resolve_declared_pins,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    evaluate_pair_attestation,
    unit_slots,
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
BLOCK_PAIR_PINS = "fresh_pair_pins_unresolved"
#: The caller's action-time authority (e.g. the lane's checkout binding) was no longer current
#: at the disposition CAS itself (Redmine #14475, review j#88538 F1). The preflight above makes
#: external observations, so a check before ``run()`` is not a check before the COMMIT — this
#: token names a drift observed at the irreversible edge, with the CAS never executed.
BLOCK_COMMIT_AUTHORITY_MOVED = "commit_authority_moved"


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
    fresh_pair_pins: tuple[ProcessGenerationPin, ...] = ()
    fresh_pair_pins_required: bool = False
    pair_attestation_detail: str = ""

    @property
    def may_resume(self) -> bool:
        return (
            self.lane_hibernated
            and self.release_settled
            and self.issue_not_reowned
            and self.pair_both_slots_live
            and self.pair_attested
            and (
                not self.fresh_pair_pins_required
                or len(self.fresh_pair_pins) == 2
            )
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
        elif self.fresh_pair_pins_required and len(self.fresh_pair_pins) != 2:
            reasons.append(BLOCK_PAIR_PINS)
        return tuple(reasons)

    def as_payload(self) -> dict[str, Any]:
        return {
            "may_resume": self.may_resume,
            "lane_hibernated": self.lane_hibernated,
            "release_settled": self.release_settled,
            "issue_not_reowned": self.issue_not_reowned,
            "pair_both_slots_live": self.pair_both_slots_live,
            "pair_attested": self.pair_attested,
            "fresh_pair_pins": [
                pin.as_payload() for pin in self.fresh_pair_pins
            ],
            "fresh_pair_pins_required": self.fresh_pair_pins_required,
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
        # Redmine #14475 (review j#88547 F1): an execute that reached the commit edge and was
        # stopped there by the action-time authority has NO transition to inspect, so the
        # branch above cannot see it. Without this the caller's ``if resume.is_blocked`` reads
        # green and proceeds to the send — the exact effect the seam exists to stop.
        if self.executed and self.detail == BLOCK_COMMIT_AUTHORITY_MOVED:
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

    def provider_pair(self) -> tuple[str, str]: ...


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

    def provider_pair(self) -> tuple[str, str]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_gateway_provider,
            resolve_worker_provider,
        )

        return (
            resolve_gateway_provider(str(self.repo_root)),
            resolve_worker_provider(str(self.repo_root)),
        )


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
    #: Optional action-time authority, re-joined IMMEDIATELY before the disposition CAS
    #: (Redmine #14475, review j#88538 F1). ``None`` keeps every pre-#14475 caller
    #: byte-invariant. A callable that returns ``False`` — or raises — stops the commit with
    #: :data:`BLOCK_COMMIT_AUTHORITY_MOVED` and zero active flip.
    commit_authority: Optional[Callable[[], bool]] = None

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
                fresh_pair_pins=(),
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
                fresh_pair_pins=(),
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
                fresh_pair_pins=(),
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
        # Classify the lifecycle authority before selecting a generation proof. A minted
        # epoch is the clock- and locator-independent replacement #14756 introduced. An
        # unminted / malformed epoch remains fail-closed; legacy evidence is not promoted to
        # substitute authority.
        _required_epoch, epoch_authority = required_resume_epoch(rec)
        epoch_authoritative = epoch_authority == EPOCH_OK

        # The hibernation timestamp is the legacy LIVENESS boundary, not a generation proof
        # (disposition j#94544 A.3): a pair self-attested after the lane hibernated is merely
        # consistent with a relaunch. Once an epoch is minted, consulting this clock again
        # would recreate the permanent false refusals #14756 was accepted to remove.
        # Redmine #14477: that boundary is the IMMUTABLE hibernate-transition stamp, never the
        # generic lifecycle ``updated_at`` every metadata write moves — reading the mutable
        # column let a pins repair invalidate the exact fresh pair it had just verified
        # (#14476 j#88614-j#88618).
        _hibernation_anchor, anchor_authority = resume_freshness_anchor(rec)
        both_live, attested, attest_detail = evaluate_pair_attestation(
            rows,
            workspace_id,
            lane,
            self.ops.read_attestation,
            # #14756 removes the timestamp from generation authority for every v10 row.
            # A minted epoch may pass; an unminted / malformed one fails in
            # ``lane_epoch_verdict`` before any process-side reason can hide that authority
            # absence. Legacy timestamp/locator evidence below is diagnostic only for that
            # fail-closed compatibility shape and can never substitute for the epoch.
            fresh_after=None,
            # Redmine #14756: the authority-grade generation proof.
            # The lifecycle row mints a monotonic epoch inside the hibernate CAS from its
            # own stored value, the launch injects it into the fresh processes' env, and
            # each slot self-attests what it actually received. A survivor's env cannot be
            # rewritten (POSIX), so it can only hold a pre-advance epoch. This defeats all
            # three vectors the timestamp could not (backdated CAS stamp, regressed host
            # clock, self-written ``observed_at``) WITHOUT depending on pane-id
            # non-reuse. For a minted epoch it replaces the two legacy generation fences
            # below; all non-generation fences remain unchanged.
            epoch_record=rec,
        )
        # Redmine #14477 disposition j#94544 A: the legacy CLOCK-INDEPENDENT half of the
        # proof. It remains diagnostic for rows without minted epoch authority, but it is not
        # consulted once the exact epoch can answer the generation question.
        # A survivor keeps its tmux pane-id, so its locator is among the ones hibernate's release
        # closed; a genuine relaunch gets a new one. This refuses all three vectors a timestamp
        # cannot (backdated CAS stamp, regressed host clock, self-written ``observed_at`` —
        # review j#94531 R2-F1). Absent / unreadable / incomplete evidence refuses too: the row
        # cannot tell "no process existed" from "a survivor was never recorded" (A.2).
        if not epoch_authoritative:
            observed_slots = unit_slots(rows, workspace_id, lane)
            fence_ok, fence_reason = released_locator_verdict(
                rec, (locator for _name, locator in observed_slots.values())
            )
            if not fence_ok:
                attested = False
                attest_detail = f"{attest_detail}; {fence_reason}"
        if (
            not epoch_authoritative
            and rec is not None
            and anchor_authority != ANCHOR_HIBERNATE_TRANSITION
        ):
            # No boundary exists for this row (a pre-v8 / older-build hibernation). FAIL the
            # freshness half CLOSED and name the reason: ``evaluate_pair_attestation`` skips
            # that half on an empty threshold, so without this a survivor would be admitted on
            # the locator pin alone — measured in review j#94515 when ``updated_at`` stood in
            # for the boundary. Such a lane resumes only after a v8 hibernate transition; it is
            # never waved through on a substitute timestamp.
            attested = False
            attest_detail = f"{attest_detail}; freshness anchor: {anchor_authority}"
        fresh_pair_pins: tuple[ProcessGenerationPin, ...] = ()
        if both_live and attested:
            try:
                providers = self.ops.provider_pair()
                declared_pair = read_declared_pin_pair(rec)
                stored_providers = (
                    (
                        declared_pair.gateway.provider,
                        declared_pair.worker.provider,
                    )
                    if (
                        declared_pair.ok
                        and declared_pair.gateway is not None
                        and declared_pair.worker is not None
                    )
                    else None
                )
                if stored_providers is not None and stored_providers != providers:
                    resolved = None
                    pin_reason = "provider_binding_drift"
                else:
                    resolved, pin_reason = resolve_declared_pins(
                        rows,
                        workspace_id=workspace_id,
                        lane_id=lane,
                        providers=providers,
                        attestation_store=SimpleNamespace(
                            read=self.ops.read_attestation
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - unresolved authority fails closed
                resolved = None
                pin_reason = type(exc).__name__
            if resolved is not None:
                fresh_pair_pins = tuple(resolved)
            elif pin_reason:
                attest_detail = f"{attest_detail}; pins: {pin_reason}"
        preflight = ResumePreflight(
            lane_hibernated=lane_hibernated,
            release_settled=release_settled,
            issue_not_reowned=issue_not_reowned,
            pair_both_slots_live=both_live,
            pair_attested=attested,
            fresh_pair_pins=fresh_pair_pins,
            fresh_pair_pins_required=True,
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
        # Redmine #14475 (review j#88538 F1): the LAST re-join before the irreversible edge.
        # Everything above this line is observation — the live pair read, the attestation join,
        # the pin resolution — so an authority checked before ``run()`` is not an authority
        # checked before the COMMIT. A drift observed here means the active flip never happens.
        if self.commit_authority is not None:
            try:
                still_authorized = bool(self.commit_authority())
            except (Exception, SystemExit):  # noqa: BLE001 - an unreadable authority is not current
                still_authorized = False
            if not still_authorized:
                return ResumeOutcome(
                    executed=True,
                    preflight=preflight,
                    issue=issue,
                    lane=lane,
                    detail=BLOCK_COMMIT_AUTHORITY_MOVED,
                )
        # Redmine #13844 R3: resume opens through the universal `_connect_write` gate, which emits
        # the PRE-migration peer-reader advisory before the shared store is migrated.
        transition = self.store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=rec.revision,
            target=DISPOSITION_ACTIVE,
            decision=decision,
            rehydrated_declared_slots=preflight.fresh_pair_pins,
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
        # Redmine #14475 (review j#88547 F1): a commit-edge authority loss passes the preflight,
        # so ``preflight.blocked_reasons`` is empty for it. Falling back to the typed ``detail``
        # keeps the operator-facing line from reading "fail-closed blocked:" with no reason.
        reasons = ", ".join(outcome.preflight.blocked_reasons) or outcome.detail
        lines.append(f"  -> fail-closed blocked: {reasons}")
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
