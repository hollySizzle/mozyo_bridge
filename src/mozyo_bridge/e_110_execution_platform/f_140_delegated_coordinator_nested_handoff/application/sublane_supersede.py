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
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_SUPERSEDED,
    OWNER_RESOLVED,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    CasOutcome,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
    ReleasePinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
    execute_herdr_retire_close,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

#: The managed slots a lane unit must carry for the recovery successor to be "ready".
_LANE_ROLES = (GATEWAY_ROLE, WORKER_ROLE)

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
class ReleaseOutcome:
    """The outcome of the tombstone-free process release on the superseded lane."""

    action_id: str
    process_release: str
    closed: tuple[tuple[str, str], ...] = ()
    failed: tuple[tuple[str, str, str], ...] = ()
    foreign_names: tuple[str, ...] = ()
    detail: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "process_release": self.process_release,
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "foreign_names": list(self.foreign_names),
            "detail": self.detail,
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


def _unit_slots(
    rows: Sequence[Mapping[str, object]], workspace_id: str, lane_id: str
) -> dict[str, tuple[str, str]]:
    """``{role: (assigned_name, locator)}`` for a lane unit's live managed slots."""
    want = _norm_lane(lane_id)
    slots: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get(AGENT_KEY_NAME)
        decode = decode_assigned_name(name)
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id:
            continue
        if _norm_lane(identity.lane_id) != want:
            continue
        if identity.role not in _LANE_ROLES:
            continue
        locator = _agent_locator(row)
        if not locator:
            continue
        slots.setdefault(identity.role, (_norm(name), locator))
    return slots


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
        slots = _unit_slots(rows, workspace_id, recovery_lane)
        if GATEWAY_ROLE not in slots or WORKER_ROLE not in slots:
            return False, False, "recovery lane is not both-slots live"
        for role in _LANE_ROLES:
            assigned_name, locator = slots[role]
            record = self.ops.read_attestation(assigned_name)
            join = evaluate_attestation(
                record,
                live_locator=locator,
                expected_workspace_id=workspace_id,
                expected_role=role,
                expected_lane=recovery_lane,
            )
            if not join.ok:
                return True, False, f"recovery {role}: {join.state}"
        return True, True, "recovery both slots attested and generation-matched"

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

        Tombstone-free: closes the managed panes through
        :func:`execute_herdr_retire_close` and never removes a worktree, deletes a
        branch, or writes a metadata tombstone. A partial close leaves the generation
        open and re-drivable — a re-run resumes it (pane close is idempotent).
        """
        action_id = f"supersede:{original_key.lane_id}"
        try:
            rec = self.store.get(original_key)
        except (LaneLifecycleError, OSError):
            return ReleaseOutcome(
                action_id=action_id,
                process_release=RELEASE_NOT_REQUESTED,
                detail="lifecycle store unreadable during release",
            )
        if rec is None or rec.lane_disposition == DISPOSITION_ACTIVE:
            # Not superseded — nothing to release (never release an active owner).
            return ReleaseOutcome(
                action_id=action_id,
                process_release=(rec.process_release if rec else RELEASE_NOT_REQUESTED),
                detail="original is not superseded; no release",
            )

        rows = self.ops.live_rows()
        if rec.process_release == RELEASE_NOT_REQUESTED:
            pins = _release_pins(rows, workspace_id, original_lane)
            if not pins:
                # No live managed slots to release — the processes are already gone.
                # The superseded lane already draws zero capacity (W4), so leaving the
                # generation unopened is honest, not a gap.
                return ReleaseOutcome(
                    action_id=action_id,
                    process_release=RELEASE_NOT_REQUESTED,
                    detail="no live managed slots to release",
                )
            try:
                opened = self.store.request_release(
                    original_key,
                    expected_revision=rec.revision,
                    action_id=action_id,
                    pins=pins,
                )
            except (ReleasePinError, LaneLifecycleError, OSError) as exc:
                return ReleaseOutcome(
                    action_id=action_id,
                    process_release=rec.process_release,
                    detail=f"release request failed ({type(exc).__name__})",
                )
            if not opened.applied:
                return ReleaseOutcome(
                    action_id=action_id,
                    process_release=rec.process_release,
                    detail=f"release request refused ({opened.reason})",
                )
            action_id = action_id
            rec = self.store.get(original_key) or rec
        elif rec.process_release in (RELEASE_REQUESTED, RELEASE_PARTIAL):
            # Resume the open generation, closing whatever slots remain live.
            action_id = rec.release_action_id or action_id
        else:  # RELEASE_RELEASED — the generation already finished.
            return ReleaseOutcome(
                action_id=rec.release_action_id or action_id,
                process_release=RELEASE_RELEASED,
                detail="release generation already released",
            )

        # F1 (R1 j#77247): close only the slots this generation durably pinned, and
        # only when their live locator STILL matches the pinned locator. A live unit scan
        # (`plan_herdr_retire_close`) would close whatever managed slot happens to occupy
        # the lane unit now — killing a pane recycled into a NEW agent generation between
        # a partial close and its resume. The pins, not a live scan, are the authority for
        # what this stale action may close (lane_lifecycle_model.py ReleasePin contract).
        # Corrupt pins fail closed (never degrade to fewer targets, leaving slots alive).
        try:
            stored_pins = rec.pins
        except ReleasePinError:
            return ReleaseOutcome(
                action_id=action_id,
                process_release=rec.process_release,
                detail="release pins unreadable; fail closed (no slots closed)",
            )
        plan = _pin_matched_close_plan(
            stored_pins, rows, workspace_id=workspace_id, lane_id=original_lane
        )
        if plan is None:
            # R2-F1: the pin set is semantically inconsistent with the lane unit — fail
            # closed (close nothing) rather than risk killing a foreign pane.
            return ReleaseOutcome(
                action_id=action_id,
                process_release=rec.process_release,
                detail="release pins inconsistent with lane unit; fail closed (no slots closed)",
            )
        close = self.ops.execute_close(plan)
        target = RELEASE_RELEASED if not close.failed else RELEASE_PARTIAL
        try:
            recorded = self.store.record_release_outcome(
                original_key,
                action_id=action_id,
                expected_revision=rec.revision,
                target=target,
            )
        except (LaneLifecycleError, OSError) as exc:
            return ReleaseOutcome(
                action_id=action_id,
                process_release=rec.process_release,
                closed=close.closed,
                failed=close.failed,
                foreign_names=close.foreign_names,
                detail=f"release outcome record failed ({type(exc).__name__})",
            )
        return ReleaseOutcome(
            action_id=action_id,
            process_release=target if recorded.applied else rec.process_release,
            closed=close.closed,
            failed=close.failed,
            foreign_names=close.foreign_names,
            detail=(
                "release recorded"
                if recorded.applied
                else f"release outcome refused ({recorded.reason})"
            ),
        )


def _pin_matched_close_plan(
    pins: Sequence[ReleasePin],
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    lane_id: str,
) -> Optional[HerdrRetireClosePlan]:
    """Close plan honoring the durable pins with full stable-identity re-resolution.

    R1 F1 (j#77247) + R2-F1 (j#77292): a pinned slot is a close target ONLY when BOTH

    - the pin's assigned name **decodes to exactly this generation's unit and role**
      ``(workspace_id, lane_id, pin.role)`` — the full ``ReleasePin`` stable identity, not
      just a name string; and
    - a live row with that same assigned name still carries the pin's **exact locator**
      (the slot was not recycled into a new agent generation and is not gone).

    A single semantically-inconsistent pin — one that decodes to a foreign unit / role, or
    is undecodable — is a corrupt pin set: the WHOLE generation fails closed (returns
    ``None`` so the caller closes nothing), rather than a partial set that might include a
    foreign pane. The pins, re-resolved against the live inventory, are the sole authority
    for what this stale action may close (``ReleasePin`` contract).

    The live inventory is matched as a **set of exact ``(assigned_name, locator)`` pairs**
    (R2-F1 j#77292 + R3-F2 j#77307), never a name→last-locator map: a pin is a target iff
    its exact pair is live, which is independent of the row order and never lets an
    already-recycled locator masquerade as the pinned one. If the same assigned name is
    live at **more than one locator** (an ambiguous inventory), the generation fails closed
    rather than guess which live pane is the pinned process — so a still-live pinned slot is
    never silently dropped and recorded ``released``.
    """
    want_lane = _norm_lane(lane_id)

    def _decodes_to_unit(name: str, role: str) -> bool:
        decode = decode_assigned_name(name)
        if not decode.ok or decode.identity is None:
            return False
        identity = decode.identity
        return (
            identity.workspace_id == workspace_id
            and _norm_lane(identity.lane_id) == want_lane
            and identity.role == role
        )

    live_pairs: set[tuple[str, str]] = set()
    locators_by_name: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _norm(row.get(AGENT_KEY_NAME))
        locator = _agent_locator(row)
        if name and locator:
            live_pairs.add((name, locator))
            locators_by_name.setdefault(name, set()).add(locator)

    targets: list[tuple[str, str]] = []
    for pin in pins:
        if pin.role not in _LANE_ROLES or not _decodes_to_unit(
            pin.assigned_name, pin.role
        ):
            # A pin naming a foreign unit / role, or an undecodable one: the pin set is
            # corrupt. Fail the whole generation closed rather than risk a foreign close.
            return None
        if len(locators_by_name.get(pin.assigned_name, ())) > 1:
            # The pinned assigned name is live at more than one locator — an ambiguous
            # inventory. Fail the whole generation closed rather than guess which live pane
            # is the pinned process (and never record `released` over an unresolved slot).
            return None
        if (pin.assigned_name, pin.locator) in live_pairs:
            targets.append((pin.role, pin.locator))
    return HerdrRetireClosePlan(
        workspace_id=workspace_id, lane_id=lane_id, close_targets=tuple(targets)
    )


def _release_pins(
    rows: Sequence[Mapping[str, object]], workspace_id: str, original_lane: str
) -> list[ReleasePin]:
    """Pin the original lane's live managed slots as the release generation's targets."""
    pins: list[ReleasePin] = []
    for role, (assigned_name, locator) in _unit_slots(
        rows, workspace_id, original_lane
    ).items():
        try:
            pins.append(
                ReleasePin(role=role, assigned_name=assigned_name, locator=locator)
            )
        except ReleasePinError:
            continue
    return pins


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
