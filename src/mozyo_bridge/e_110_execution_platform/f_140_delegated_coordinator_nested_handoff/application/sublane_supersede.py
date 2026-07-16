"""`mozyo-bridge sublane supersede` — recovery lane owner handover (Redmine #13681 W2).

When a recovery lane takes over a failed lane's issue, the old owner has to hand its
ownership across and wind its processes down — without closing the issue, removing a
worktree, or deleting a branch (Design Answer j#76630, Implementation Request j#77170).
This use case is that transition, layered on the #13689 lifecycle substrate:

1. **preflight (fail-closed)** — both lane identities known, the recovery successor is
   *attested* (both managed slots live AND each carrying a generation-matched startup
   self-attestation, #13637), the same issue, and the original lane idle (no callback
   debt / pending composer / in-flight work the operator asserts from the durable
   record). Any unmet gate blocks with a reason and mutates nothing.
2. **commit point** — :meth:`LaneLifecycleStore.supersede_and_activate` moves ownership
   in one CAS transaction: the old owner goes ``active -> superseded`` and the recovery
   lane becomes the single active owner atomically. After this the original is never a
   send target again — the W3 gate turns an explicit send to it into a zero-send.
3. **process release (tombstone-free)** — a release generation is opened pinning the
   original's live slots, its managed panes are closed through the existing
   :func:`execute_herdr_retire_close` primitive (which never removes a worktree, deletes
   a branch, or writes a metadata tombstone), and the outcome is recorded ``released`` /
   ``partial``. A partial close is re-drivable: a re-run resumes the open generation
   (pane close is idempotent) rather than opening a new one.

Boundary (j#77170): this does not implement #13682 hibernate / resume, #13685 legacy
migration, worktree / branch deletion, merge, or release/publish. Default is preflight
only; ``--execute`` performs the handover.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    emit_lifecycle_migration_advisory,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_SUPERSEDED,
    OWNER_RESOLVED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
    execute_herdr_retire_close,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    ReleaseOutcome,
    drive_process_release,
    evaluate_pair_attestation,
    pin_matched_close_plan,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

#: Back-compat alias: the pin matcher now lives in the shared release module (Redmine
#: #13682). Kept so existing importers of the supersede symbol keep resolving.
_pin_matched_close_plan = pin_matched_close_plan

# Blocked-reason vocabulary (fail-closed preflight).
BLOCK_ORIGINAL_IDENTITY = "original_identity_unknown"
BLOCK_RECOVERY_SLOTS = "recovery_not_both_slots_live"
BLOCK_RECOVERY_ATTESTATION = "recovery_not_attested"
BLOCK_ORIGINAL_NOT_IDLE = "original_not_idle"


# ---------------------------------------------------------------------------
# Operator-asserted durable-record invariants (the "original idle" gate).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupersedeAssertions:
    """Durable-record facts no live probe can infer, asserted from the Redmine record.

    Mirrors :class:`RetireAssertions`: every default is the unsatisfied (safe-failing)
    value, so a caller that omits a flag fails closed. The original lane must be idle —
    its callbacks drained, no composer input pending, and no work in flight — before its
    ownership is handed away and its processes released.
    """

    callbacks_drained: bool = False
    no_pending_prompt: bool = False
    not_working: bool = False

    @property
    def original_idle(self) -> bool:
        return self.callbacks_drained and self.no_pending_prompt and self.not_working


# ---------------------------------------------------------------------------
# Pure preflight decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupersedePreflight:
    """The fail-closed inputs + verdict of a supersession preflight (pure)."""

    original_identity_known: bool
    recovery_both_slots_live: bool
    recovery_attested: bool
    original_idle: bool
    recovery_attestation_detail: str = ""

    @property
    def may_supersede(self) -> bool:
        return (
            self.original_identity_known
            and self.recovery_both_slots_live
            and self.recovery_attested
            and self.original_idle
        )

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.original_identity_known:
            reasons.append(BLOCK_ORIGINAL_IDENTITY)
        if not self.recovery_both_slots_live:
            reasons.append(BLOCK_RECOVERY_SLOTS)
        elif not self.recovery_attested:
            reasons.append(BLOCK_RECOVERY_ATTESTATION)
        if not self.original_idle:
            reasons.append(BLOCK_ORIGINAL_NOT_IDLE)
        return tuple(reasons)

    def as_payload(self) -> dict[str, Any]:
        return {
            "may_supersede": self.may_supersede,
            "original_identity_known": self.original_identity_known,
            "recovery_both_slots_live": self.recovery_both_slots_live,
            "recovery_attested": self.recovery_attested,
            "recovery_attestation_detail": self.recovery_attestation_detail,
            "original_idle": self.original_idle,
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class SupersedeOutcome:
    """The full result: preflight verdict, the commit, and the release."""

    executed: bool
    preflight: SupersedePreflight
    issue: str
    original_lane: str
    recovery_lane: str
    already_handed_over: bool = False
    supersede: Optional[CasOutcome] = None
    release: Optional[ReleaseOutcome] = None
    detail: str = ""

    @property
    def is_blocked(self) -> bool:
        if self.already_handed_over:
            return False
        if not self.preflight.may_supersede:
            return True
        # A commit that was attempted but not applied (a lost CAS race) is a block.
        if self.executed and self.supersede is not None and not self.supersede.applied:
            return True
        return False

    def as_payload(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "issue": self.issue,
            "original_lane": self.original_lane,
            "recovery_lane": self.recovery_lane,
            "already_handed_over": self.already_handed_over,
            "is_blocked": self.is_blocked,
            "preflight": self.preflight.as_payload(),
            "supersede": (
                {"applied": self.supersede.applied, "reason": self.supersede.reason,
                 "revision": self.supersede.revision}
                if self.supersede is not None
                else None
            ),
            "release": self.release.as_payload() if self.release is not None else None,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Injected IO port + live adapter.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneSupersedeOps(Protocol):
    """Every side effect the supersede use case needs, injected so tests drive fakes."""

    def workspace_id(self) -> str: ...

    def live_rows(self) -> Sequence[Mapping[str, object]]: ...

    def read_attestation(
        self, assigned_name: str
    ) -> Optional[IdentityAttestationRecord]: ...

    def execute_close(self, plan: HerdrRetireClosePlan) -> HerdrRetireCloseResult: ...


@dataclass
class LiveSublaneSupersedeOps:
    """Live adapter: project workspace segment + live herdr inventory + guarded close."""

    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS

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

    def execute_close(self, plan: HerdrRetireClosePlan) -> HerdrRetireCloseResult:
        return execute_herdr_retire_close(
            plan, env=self.env, runner=self.runner, timeout=self.timeout
        )


# ---------------------------------------------------------------------------
# Use case.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupersedeRequest:
    issue: str
    original_lane: str
    recovery_lane: str
    journal: str
    assertions: SupersedeAssertions


@dataclass
class SublaneSupersedeUseCase:
    """Preflight + atomic ownership handover + tombstone-free process release."""

    ops: SublaneSupersedeOps
    store: LaneLifecycleStore

    def _decision(self, request: SupersedeRequest) -> Optional[DecisionPointer]:
        try:
            return DecisionPointer(
                source="redmine",
                issue_id=_norm(request.issue),
                journal_id=_norm(request.journal),
            )
        except DecisionPointerError:
            return None

    def _recovery_attested(
        self,
        rows: Sequence[Mapping[str, object]],
        workspace_id: str,
        recovery_lane: str,
    ) -> tuple[bool, bool, str]:
        """(both_slots_live, attested, detail) for the recovery successor."""
        return evaluate_pair_attestation(
            rows, workspace_id, recovery_lane, self.ops.read_attestation
        )

    def run(self, request: SupersedeRequest, *, execute: bool) -> SupersedeOutcome:
        issue = _norm(request.issue)
        original_lane = _norm(request.original_lane)
        recovery_lane = _norm(request.recovery_lane)
        workspace_id = _norm(self.ops.workspace_id())

        # A malformed identity / anchor can address nothing — fail closed before any read.
        decision = self._decision(request)
        if (
            not issue
            or not original_lane
            or not recovery_lane
            or original_lane == recovery_lane
            or not workspace_id
            or decision is None
        ):
            preflight = SupersedePreflight(
                original_identity_known=False,
                recovery_both_slots_live=False,
                recovery_attested=False,
                original_idle=request.assertions.original_idle,
                recovery_attestation_detail="identity / decision anchor incomplete",
            )
            return SupersedeOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                original_lane=original_lane,
                recovery_lane=recovery_lane,
                detail="incomplete supersession identity or decision anchor",
            )

        original_key = LaneLifecycleKey(workspace_id, original_lane)
        recovery_key = LaneLifecycleKey(workspace_id, recovery_lane)

        try:
            original_rec = self.store.get(original_key)
            owner = self.store.resolve_owner(workspace_id, issue)
        except (LaneLifecycleError, OSError):
            preflight = SupersedePreflight(
                original_identity_known=False,
                recovery_both_slots_live=False,
                recovery_attested=False,
                original_idle=request.assertions.original_idle,
                recovery_attestation_detail="lifecycle store unreadable",
            )
            return SupersedeOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                original_lane=original_lane,
                recovery_lane=recovery_lane,
                detail="lifecycle store unreadable; fail closed",
            )

        # Idempotent resume: ownership already handed to this recovery lane. Skip the
        # commit (its CAS guard would refuse anyway) and re-drive the release, which is
        # itself idempotent (a pane close is, unlike a send).
        already_handed_over = (
            original_rec is not None
            and original_rec.lane_disposition == DISPOSITION_SUPERSEDED
            and owner.status == OWNER_RESOLVED
            and owner.lane_id == recovery_lane
        )
        if already_handed_over:
            release = None
            if execute:
                release = self._drive_release(original_key, original_lane, workspace_id)
            preflight = SupersedePreflight(
                original_identity_known=True,
                recovery_both_slots_live=True,
                recovery_attested=True,
                original_idle=request.assertions.original_idle,
                recovery_attestation_detail="ownership already handed over",
            )
            return SupersedeOutcome(
                executed=execute,
                preflight=preflight,
                issue=issue,
                original_lane=original_lane,
                recovery_lane=recovery_lane,
                already_handed_over=True,
                release=release,
                detail="ownership already handed over; resumed release",
            )

        rows = self.ops.live_rows()
        both_live, attested, attest_detail = self._recovery_attested(
            rows, workspace_id, recovery_lane
        )
        original_identity_known = (
            original_rec is not None
            and original_rec.lane_disposition == DISPOSITION_ACTIVE
            and original_rec.issue_id == issue
        )
        preflight = SupersedePreflight(
            original_identity_known=original_identity_known,
            recovery_both_slots_live=both_live,
            recovery_attested=attested,
            original_idle=request.assertions.original_idle,
            recovery_attestation_detail=attest_detail,
        )
        if not preflight.may_supersede or not execute:
            return SupersedeOutcome(
                executed=False,
                preflight=preflight,
                issue=issue,
                original_lane=original_lane,
                recovery_lane=recovery_lane,
                detail=(
                    "preflight only (no --execute)"
                    if preflight.may_supersede
                    else "fail-closed: supersession blocked"
                ),
            )

        # Commit point: hand ownership over atomically. The recovery lane's CAS guard is
        # supplied when it already has a row (never overwrite an in-flight recovery lane).
        recovery_rec = self.store.get(recovery_key)
        assert original_rec is not None  # guaranteed by original_identity_known
        supersede = self.store.supersede_and_activate(
            superseded=original_key,
            expected_revision=original_rec.revision,
            recovery=recovery_key,
            decision=decision,
            recovery_expected_disposition=(
                recovery_rec.lane_disposition if recovery_rec is not None else None
            ),
            recovery_expected_revision=(
                recovery_rec.revision if recovery_rec is not None else None
            ),
        )
        # Redmine #13844 R2: supersede is a schema-needing mutation — surface any forward
        # migration of the shared store and its active peer-reader risk (same advisory as adopt).
        emit_lifecycle_migration_advisory(
            getattr(self.store, "last_write_preparation", None), stream=sys.stderr
        )
        if not supersede.applied:
            return SupersedeOutcome(
                executed=True,
                preflight=preflight,
                issue=issue,
                original_lane=original_lane,
                recovery_lane=recovery_lane,
                supersede=supersede,
                detail=f"supersession commit refused ({supersede.reason})",
            )

        release = self._drive_release(original_key, original_lane, workspace_id)
        return SupersedeOutcome(
            executed=True,
            preflight=preflight,
            issue=issue,
            original_lane=original_lane,
            recovery_lane=recovery_lane,
            supersede=supersede,
            release=release,
            detail="ownership handed over; original process release driven",
        )

    def _drive_release(
        self, original_key: LaneLifecycleKey, original_lane: str, workspace_id: str
    ) -> ReleaseOutcome:
        """Open (or resume) the release generation and close the original's slots.

        Delegates to the shared tombstone-free driver (Redmine #13682): it never removes
        a worktree, deletes a branch, or writes a metadata tombstone, and a partial close
        leaves the generation open and re-drivable.
        """
        return drive_process_release(
            store=self.store,
            ops=self.ops,
            key=original_key,
            lane_id=original_lane,
            workspace_id=workspace_id,
            action_id=f"supersede:{original_key.lane_id}",
        )


# ---------------------------------------------------------------------------
# Text rendering + thin CLI handler.
# ---------------------------------------------------------------------------


def format_supersede_text(outcome: SupersedeOutcome) -> str:
    lines = [
        f"sublane supersede: {outcome.original_lane} -> {outcome.recovery_lane} "
        f"(issue {outcome.issue})",
        f"  may_supersede: {outcome.preflight.may_supersede} executed: {outcome.executed}",
    ]
    if outcome.already_handed_over:
        lines.append("  ownership already handed over (idempotent resume)")
    if outcome.is_blocked:
        lines.append(
            "  -> fail-closed blocked: " + ", ".join(outcome.preflight.blocked_reasons)
        )
        if outcome.supersede is not None and not outcome.supersede.applied:
            lines.append(f"  commit refused: {outcome.supersede.reason}")
        return "\n".join(lines)
    if outcome.supersede is not None:
        lines.append(
            f"  commit: applied={outcome.supersede.applied} "
            f"reason={outcome.supersede.reason}"
        )
    if outcome.release is not None:
        rel = outcome.release
        lines.append(f"  release: {rel.process_release} ({rel.detail})")
        for role, locator in rel.closed:
            lines.append(f"    - closed {role} {locator}")
        for role, locator, detail in rel.failed:
            lines.append(f"    ! close failed {role} {locator}: {detail}")
    if not outcome.executed and outcome.preflight.may_supersede:
        lines.append("  (preflight only; re-run with --execute to hand ownership over)")
    return "\n".join(lines)


def cmd_sublane_supersede(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = SupersedeRequest(
        issue=getattr(args, "issue", "") or "",
        original_lane=getattr(args, "original_lane", "") or "",
        recovery_lane=getattr(args, "recovery_lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        assertions=SupersedeAssertions(
            callbacks_drained=bool(getattr(args, "callbacks_drained", False)),
            no_pending_prompt=bool(getattr(args, "no_pending_prompt", False)),
            not_working=bool(getattr(args, "not_working", False)),
        ),
    )
    json_mode = bool(getattr(args, "json", False))
    ops = LiveSublaneSupersedeOps(
        repo_root=repo_root, env=dict(os.environ)
    )
    use_case = SublaneSupersedeUseCase(ops=ops, store=LaneLifecycleStore())
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if json_mode:
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_supersede_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


__all__ = (
    "BLOCK_ORIGINAL_IDENTITY",
    "BLOCK_ORIGINAL_NOT_IDLE",
    "BLOCK_RECOVERY_ATTESTATION",
    "BLOCK_RECOVERY_SLOTS",
    "LiveSublaneSupersedeOps",
    "ReleaseOutcome",
    "SublaneSupersedeOps",
    "SublaneSupersedeUseCase",
    "SupersedeAssertions",
    "SupersedeOutcome",
    "SupersedePreflight",
    "SupersedeRequest",
    "cmd_sublane_supersede",
    "format_supersede_text",
)
