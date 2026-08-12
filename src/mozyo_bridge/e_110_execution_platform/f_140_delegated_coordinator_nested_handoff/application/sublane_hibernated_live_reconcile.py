"""Hibernated live-contradiction reconcile (Redmine #13842/#15227).

The exact idle, settled, v4-attested and completed-v2 pair is retired by one revision CAS,
which stores a dedicated versioned owed-close pin on the lifecycle row. A replay may close
only that pin after one fresh full canonical inventory rejoin; legacy/missing/mismatched pins
are diagnostic-only and close nothing. Positive full-snapshot absence remains idempotent
completion. The provider mutation is locator-only, so the final check-to-close ABA window is
a documented Herdr API residual rather than an atomic compare-and-close guarantee.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_hibernated_live_reconcile import (  # noqa: E501
    PairObservation,
    SlotObservation,
    STATE_BLOCKED,
    decide_pair_reconcile,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile_ops import (  # noqa: E501
    LiveReconcileOps,
)

# -- reconcile verdict vocabulary --------------------------------------------

#: The lane was reconciled: the exact live pair was verified, its binding re-established, the
#: pair closed, and the row moved to the #13689 terminal ``retired`` disposition.
RECONCILE_RECONCILED = "reconciled"
#: A verified idempotent no-op: the row is already ``retired`` and owns this exact issue, and no
#: expected managed slot is live — a duplicate replay of a completed reconcile.
RECONCILE_ALREADY = "already_reconciled"
#: Fail-closed: the reconcile proved nothing and wrote / closed nothing. Never exit 0.
RECONCILE_BLOCKED = "blocked"

#: Orchestration-level blocked reasons (the pair-decision reasons come from the pure domain
#: module; the lane-resolution reasons are reused from the guarded close so an operator reads
#: one vocabulary).
RECON_WORKTREE_BRANCH_MISMATCH = "worktree_branch_mismatch"
RECON_HEAD_NOT_INTEGRATED = "head_not_integrated"
RECON_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
RECON_LANE_NOT_DECLARED = "lane_not_declared"
#: The durable row is not the hibernated + released + settled + issue-owner legacy signature
#: (a different disposition / binding / issue, an unproven / in-flight release, or a receiver
#: replacement in flight).
RECON_NOT_RECONCILABLE_STATE = "not_reconcilable_state"
#: A positive absence with no durable owed state: no expected managed slot is live and the row
#: was never re-bound — there is no live pair to reconcile (route to the #13841 live-zero
#: migration instead).
RECON_LIVE_PAIR_ABSENT = "live_pair_absent"
#: A persisted ``retired`` row whose expected pair is (again) live / blocked: a durable
#: disposition does not prove non-liveness, so the idempotent success is withheld.
RECON_LIVE_PAIR_PRESENT = "live_pair_present"
RECON_REVISION_RACE = "revision_race"
RECON_RELEASE_NOT_PROVEN = "release_not_proven"
RECON_STORE_ERROR = "store_error"
RECON_CLOSE_FAILED = "close_failed"


@dataclass(frozen=True)
class HibernatedLiveReconcileVerdict:
    """The fail-closed verdict of the hibernated live-contradiction reconcile.

    ``ok`` (the command's exit-code authority) is true only for a real reconcile or a verified
    idempotent no-op — every other outcome is :data:`RECONCILE_BLOCKED` with the ``reason`` that
    could not be established, never a success.

    ``retired`` and ``closed`` report the durable side effects that ACTUALLY happened, even on a
    :data:`RECONCILE_BLOCKED` verdict (Redmine #13842 review j#79363 R7): the reconcile is
    retire-first, so a blocked run may have already committed the ``retired`` disposition and
    closed some pins (e.g. a post-close whole-unit measure that found a newer generation live).
    A caller / text renderer must not claim "nothing was written or closed" when either is set.
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    closed: tuple[str, ...] = ()
    #: Did this run (or a prior same-flow run this replays) durably retire the lane? A blocked
    #: verdict with ``retired=True`` still performed a durable write (R7).
    retired: bool = False
    #: The shared-store schema migration this reconcile's write gate performed, if any (Redmine
    #: #13844 R3-F2): ``None`` when nothing was forward-migrated; otherwise the typed audit record
    #: (from/to version, backup, peer-reader risk) so the migration is legible in JSON/text, not
    #: only the pre-migration stderr advisory.
    lifecycle_migration: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.state in (RECONCILE_RECONCILED, RECONCILE_ALREADY)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "closed": list(self.closed),
            "retired": self.retired,
            "lifecycle_migration": self.lifecycle_migration,
        }


def _blocked(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
    retired: bool = False,
    closed: tuple[str, ...] = (),
    lifecycle_migration: Optional[dict] = None,
) -> HibernatedLiveReconcileVerdict:
    return HibernatedLiveReconcileVerdict(
        state=RECONCILE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        retired=retired,
        closed=closed,
        lifecycle_migration=lifecycle_migration,
    )

# ---------------------------------------------------------------------------
# Injected live-observation port (so tests drive fakes, never a live pane).
# ---------------------------------------------------------------------------


@runtime_checkable
class ReconcileOps(Protocol):
    """Every live read the reconcile's action-time verification needs, injected.

    ``agent_rows`` is the raw ``agent list`` inventory (it MAY raise
    ``HerdrSessionStartError`` — an unreadable inventory fails closed, never "no pair").
    ``runtime_state`` maps a locator to a mozyo runtime receiver-state (fail-soft to
    ``unknown``). ``observe_composer`` returns the content-free ``(readable, has_pending)``
    pending-composer facts for a locator (the composer body never crosses this boundary;
    fail-soft to ``(False, None)``). ``read_attestation`` returns the durable startup
    self-attestation record for an assigned name, or ``None``.
    """

    def agent_rows(self) -> Sequence[Mapping[str, object]]: ...

    def runtime_state(self, locator: str) -> str: ...

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]: ...

    def read_attestation(self, assigned_name: str): ...


def observe_pair(
    rows: Sequence[Mapping[str, object]],
    ops: ReconcileOps,
    *,
    workspace_id: str,
    lane_id: str,
    managed_pairs: tuple[tuple[str, str], ...],
) -> PairObservation:
    """Gather the content-free per-slot facts for the lane's expected managed pair (#13842).

    Public because the #13879 hibernated bound **pin repair** observes the same expected pair
    the same way (Redmine #13879). This is a **scan**, not a judgment: it gathers facts and
    leaves every verdict to :func:`decide_pair_reconcile` and to each surface's own row-shape
    CAS — sharing the scan is what #13845 j#80148 asks for, sharing a row-shape guard is what
    it forbids (the sibling CAS signatures stay literal and disjoint).

    ``managed_pairs`` is ``((gateway_provider, "gateway"), (worker_provider, "worker"))``. For
    each provider role it counts the live rows carrying the expected assigned name (a raw
    multiplicity check BEFORE liveness, so a duplicate name is caught even when one row is a
    stale residue), and for the unique candidate gathers its liveness, locator, startup
    self-attestation join, runtime receiver-state, and pending-composer facts. It also flags a
    **foreign** (non-managed) provider standing at the lane's own ``(workspace, lane)`` position
    — a substitution the reconcile never closes past.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import evaluate_attestation
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        AGENT_KEY_NAME,
        _agent_locator,
        _norm,
        _norm_lane,
        decode_assigned_name,
        terminal_identity_of_live_slot,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
        SLOT_LIVE,
        classify_named_slot,
    )

    want_lane = _norm_lane(lane_id)
    managed_providers = frozenset(provider for provider, _role in managed_pairs)
    foreign_at_position = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if (
            identity.workspace_id == workspace_id
            and _norm_lane(identity.lane_id) == want_lane
            and identity.role not in managed_providers
            and _agent_locator(row)
        ):
            foreign_at_position = True
    slots: list[SlotObservation] = []
    for provider, role in managed_pairs:
        candidates = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decode.ok or decode.identity is None:
                continue
            identity = decode.identity
            if (
                identity.workspace_id == workspace_id
                and _norm_lane(identity.lane_id) == want_lane
                and identity.role == provider
            ):
                candidates.append(row)
        count = len(candidates)
        if count != 1:
            slots.append(
                SlotObservation(role=role, provider=provider, candidate_count=count)
            )
            continue
        row = candidates[0]
        assigned_name = _norm(row.get(AGENT_KEY_NAME))
        locator = _norm(_agent_locator(row))
        slot_live = classify_named_slot(row) == SLOT_LIVE
        attested = False
        attested_at = ""
        if locator:
            record = ops.read_attestation(assigned_name)
            join = evaluate_attestation(
                record,
                live_locator=locator,
                live_terminal_id=terminal_identity_of_live_slot(assigned_name, locator, rows),
                expected_workspace_id=workspace_id,
                expected_role=provider,
                expected_lane=want_lane,
            )
            attested = join.ok
            attested_at = _norm(record.observed_at) if record is not None else ""
        runtime_state = ops.runtime_state(locator) if locator else ""
        readable, has_pending = (
            ops.observe_composer(locator) if locator else (False, None)
        )
        slots.append(
            SlotObservation(
                role=role,
                provider=provider,
                candidate_count=count,
                slot_live=slot_live,
                locator=locator,
                assigned_name=assigned_name,
                attested_at=attested_at,
                attested=attested,
                runtime_state=runtime_state,
                composer_readable=readable,
                has_pending=has_pending,
            )
        )
    return PairObservation(
        inventory_readable=True,
        foreign_at_position=foreign_at_position,
        slots=tuple(slots),
    )


def run_hibernated_live_reconcile(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    head_integrated: Optional[bool],
    worktree_branch: Optional[str],
    ops: Optional[ReconcileOps] = None,
) -> Optional[HibernatedLiveReconcileVerdict]:
    """Reconcile a hibernated / released legacy lane whose exact pair is live (Redmine #13842).

    Returns a :class:`HibernatedLiveReconcileVerdict`, or ``None`` when the repo is not on the
    herdr backend. ``head_integrated`` is the command's read-only ``--branch`` -> ``--integration
    -branch`` ancestry probe (``None`` / ``False`` fails closed); ``worktree_branch`` is the
    ``--worktree``'s ACTUAL checked-out branch (it must equal ``--branch``). The command runs
    this only when its ``may_retire`` preflight already passed (issue closed, callbacks drained,
    latest review admissible, target identity known), so those axes — the callback / review
    obligations — are established upstream.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        BINDING_KIND_ISSUE, stored_binding_kind_is,  # one check, one line (module at ceiling)
        CAS_FORBIDDEN_TRANSITION,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        CAS_UNEXPECTED_STATE,
        DISPOSITION_HIBERNATED,
        DISPOSITION_RETIRED,
        RECONCILE_PHASE_RECONCILED,
        RELEASE_RELEASED,
        DecisionPointer,
        DecisionPointerError,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
        ProcessGenerationPin,
        ProcessPinError,
        norm,
        replacement_settled,
    )
    from mozyo_bridge.core.state.lane_reconcile_binding import LaneReconcileBindingStore
    from mozyo_bridge.core.state.lane_reconcile_close_pin import (
        ReconcileClosePinError, build_reconcile_close_pin, decode_reconcile_close_pin,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_destructive_close_identity import (  # noqa: E501
        current_generation_release_pins, pinned_generation_partition,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_terminal_identity import terminal_identity_snapshot_complete  # noqa: E501
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
        declared_lane_root_identity,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_NO_WORKTREE_ANCHOR,
        REASON_PROVIDER_NOT_LAUNCHABLE,
        REASON_PROVIDER_UNRESOLVED,
        REASON_WORKSPACE_UNRESOLVED,
        execute_herdr_retire_close,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
        pin_matched_close_plan,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.core.state.lane_pin_role import (
        PIN_ROLE_GATEWAY,
        PIN_ROLE_WORKER,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import encode_assigned_name  # noqa: E501

    if not repo_backend_is_herdr(repo_root):
        return None
    worktree = getattr(args, "worktree", None)
    lane_label = (getattr(args, "lane_label", "") or "").strip()
    issue = (getattr(args, "issue", "") or "").strip()
    journal = (getattr(args, "journal", "") or "").strip()
    if not worktree:
        return _blocked(
            REASON_NO_WORKTREE_ANCHOR,
            detail=(
                "the reconcile needs the lane's --worktree anchor to resolve the lane "
                "unit; without it no lane identity can be established"
            ),
            lane_id=lane_label,
        )
    # Resolve the lane unit from the --worktree anchor, exactly as the guarded close does: the
    # worktree inherits the project workspace identity (#13377) that scopes the live slots, and
    # its stable path token (``wt_``/``dl_``) is the canonical worktree binding the rebind
    # re-establishes and the guarded close attests against.
    try:
        resolved_worktree = Path(worktree).expanduser().resolve()
        workspace_id = herdr_workspace_segment(resolved_worktree, home=getattr(args, "home", None))
    except (OSError, ValueError) as exc:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=f"--worktree does not resolve ({type(exc).__name__})",
            lane_id=lane_label,
        )
    # Redmine #14715: the binding this rebind re-establishes — and that the guarded close
    # later attests against — is derived by the canonical helper the create / adopt writers
    # use, probed on the ``--worktree`` root's own kind. The retired
    # ``resolved_worktree == repo_root`` proxy keyed the family on the operator's cwd, so a
    # normal run from inside a linked worktree (``--repo`` omitted) would have rebound a git
    # worktree's row to a ``dl_`` token no other surface derives.
    metadata_token = declared_lane_root_identity(
        resolved_worktree, lane_label
    ).metadata_token
    if not workspace_id:
        # The reconcile scopes the exact live pair to the SHARED project workspace unit
        # (#13377, the #13756 contradiction shape). A --worktree that carries no project
        # workspace anchor cannot scope the pair, so it fails closed rather than guess a unit.
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the --worktree root carries no herdr project workspace anchor; the "
                "lane's exact live pair cannot be scoped (point --repo / --worktree at "
                "the lane's own checkout)"
            ),
            lane_id=lane_label,
        )
    if not metadata_token:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail="the --worktree did not resolve to a canonical lane binding token",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Worktree <-> branch identity: the clean probe measures the --worktree while the
    # integration probe measures --branch, so unless the --worktree is ACTUALLY checked out on
    # --branch the two describe different heads. Require the worktree's real branch to equal
    # --branch; a mismatch / detached / unresolvable / empty --branch fails closed.
    want_branch = (getattr(args, "branch", "") or "").strip()
    actual_branch = (worktree_branch or "").strip()
    if (
        not want_branch
        or not actual_branch
        or actual_branch == "HEAD"
        or actual_branch != want_branch
    ):
        return _blocked(
            RECON_WORKTREE_BRANCH_MISMATCH,
            detail=(
                f"the --worktree is not checked out on --branch {want_branch or '<none>'} "
                f"(actual head: {actual_branch or '<unresolved/detached>'}); its integrated "
                "evidence cannot be attributed to the lane's branch"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if head_integrated is not True:
        return _blocked(
            RECON_HEAD_NOT_INTEGRATED,
            detail=(
                "--branch is not a verified ancestor of --integration-branch (unintegrated "
                "or the ancestry probe could not answer); the lane's head must be integrated "
                "before a reconcile"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
    except ValueError:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail="the lane unit cannot be keyed (empty workspace / lane)",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        record = LaneLifecycleStore(home=getattr(args, "home", None)).get(key)
    except (LaneLifecycleError, OSError) as exc:
        return _blocked(
            RECON_LIFECYCLE_UNREADABLE,
            detail=f"the lifecycle store is unreadable ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            RECON_LANE_NOT_DECLARED,
            detail="the lane unit has no durable lifecycle owner row; nothing to reconcile",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except DecisionPointerError:
        return _blocked(
            RECON_LIFECYCLE_UNREADABLE,
            detail=(
                "no re-readable Redmine decision anchor (--issue / --journal) to record the "
                "retirement with; the reconcile fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not decision.authorizes_binding((record.issue_id or "").strip()):
        return _blocked(
            RECON_NOT_RECONCILABLE_STATE,
            detail=(
                "the durable owner binding is a different issue than --issue; refusing to "
                "reconcile a lane the request does not name"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        gateway_provider = resolve_gateway_provider(str(repo_root))
        worker_provider = resolve_worker_provider(str(repo_root))
    except WorkflowProviderUnresolved as exc:
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            detail=f"workflow provider binding unresolved ({exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E501
        BUILTIN_AGENT_PROVIDER_SNAPSHOT,
    )

    if not all(
        BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable(p)
        for p in (gateway_provider, worker_provider)
    ):
        return _blocked(
            REASON_PROVIDER_NOT_LAUNCHABLE,
            detail=(
                "the binding assigns a provider that is not mechanically launchable; the "
                "lane unit's managed pair cannot be measured"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Redmine #14065 Phase 2: inject the dim-ghost render policy (decision + pre-close re-observe).
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import default_ghost_policy  # noqa: E501

    live_ops = ops if ops is not None else LiveReconcileOps(repo_root=repo_root, ghost_policy=default_ghost_policy())  # noqa: E501
    try:
        rows = live_ops.agent_rows()
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live herdr inventory unreadable ({exc}); liveness cannot be measured",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # ``observe_pair`` resolves each live row by its PROVIDER (decoded out of the assigned
    # name); the role is only the slot label it stamps onto the observation — and onto the
    # pins re-declared below. So the label is the canonical vocabulary the recover-pair /
    # repair-pins consumers read (Redmine #13920), which is what this function's own
    # ``observe_pair`` docstring already specified.
    managed_pairs = (
        (gateway_provider, PIN_ROLE_GATEWAY),
        (worker_provider, PIN_ROLE_WORKER),
    )
    observation = observe_pair(
        rows,
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_label,
        managed_pairs=managed_pairs,
    )
    verdict = decide_pair_reconcile(observation)

    def _pins_from(slots):
        return [
            ProcessGenerationPin(
                role=s.role,
                provider=s.provider,
                assigned_name=s.assigned_name,
                locator=s.locator,
                attested_at=s.attested_at,
            )
            for s in slots
        ]

    def _observe(rows_):
        return observe_pair(
            rows_,
            live_ops,
            workspace_id=workspace_id,
            lane_id=lane_label,
            managed_pairs=managed_pairs,
        )

    def _close_owed_pair(pins, declared_pins):
        """Close only exact-live pins; partial replay treats exact absences as completed."""
        declared = tuple(declared_pins)
        if len(declared) != 2 or any(
            pin.assigned_name
            != encode_assigned_name(workspace_id, pin.provider, lane_label)
            for pin in declared
        ) or {
            (pin.role, pin.assigned_name, pin.locator) for pin in pins
        } != {
            (pin.provider, pin.assigned_name, pin.locator) for pin in declared
        }:
            return ((), False)
        try:
            rows2 = tuple(live_ops.agent_rows())
        except HerdrSessionStartError:
            return ((), False)
        partition = pinned_generation_partition(
            pins,
            rows2,
            home=getattr(args, "home", None),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
        if partition is None:
            return ((), False)
        live_pins, absent_pins = partition
        obs2 = _observe(rows2)
        by_provider = {slot.provider: slot for slot in obs2.slots}
        live_ok = all(
            (slot := by_provider.get(pin.role)) is not None
            and slot.candidate_count == 1
            and slot.slot_live
            and slot.assigned_name == pin.assigned_name
            and slot.locator == pin.locator
            and slot.attested
            and slot.agent_idle
            and slot.composer_settled
            for pin in live_pins
        )
        absent_ok = all(
            (slot := by_provider.get(pin.role)) is not None
            and slot.candidate_count == 0
            for pin in absent_pins
        )
        if obs2.foreign_at_position or not live_ok or not absent_ok:
            # Not the exact verified pair at close time: busy / pending / foreign / duplicate, or
            # recycled to different locators. Zero-close (R2/R3) — never close an unverified or
            # changed generation.
            return ((), False)
        if not live_pins:
            return ((), True)
        plan = pin_matched_close_plan(
            live_pins,
            rows2,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
        if plan is None or len(plan.close_targets) != len(live_pins):
            return ((), False)
        try:
            close = execute_herdr_retire_close(plan)
        except HerdrSessionStartError:
            return ((), False)
        if close.failed:
            return (close.closed, False)
        # Post-close requires whole-unit and private terminal-bound positive absence.
        try:
            rows3 = live_ops.agent_rows()
        except HerdrSessionStartError:
            return (close.closed, False)
        post_partition = pinned_generation_partition(
            pins,
            rows3,
            home=getattr(args, "home", None),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
        return (
            close.closed,
            terminal_identity_snapshot_complete(rows3)
            and decide_pair_reconcile(_observe(rows3)).absent
            and post_partition is not None
            and not post_partition[0]
            and len(post_partition[1]) == len(tuple(pins)),
        )

    def _reconciled(closed, lifecycle_migration=None):
        return HibernatedLiveReconcileVerdict(
            state=RECONCILE_RECONCILED,
            detail=(
                "the exact verified live pair's lane was retired (retire-first, revision-guarded) "
                "and the pinned pair closed under the now-immutable generation"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            closed=tuple(f"{role} {loc}" for role, loc in closed),
            retired=True,
            lifecycle_migration=lifecycle_migration,
        )

    # Only this row's durable reconcile provenance resumes the terminal-bound owed close.
    if record.lane_disposition == DISPOSITION_RETIRED and (
        record.issue_id or ""
    ).strip() == issue:
        reconcile_owned = record.reconcile_phase == RECONCILE_PHASE_RECONCILED
        if not reconcile_owned:
            # An ordinary bound retired row (empty phase). A positive absence is a harmless
            # idempotent no-op; a live pair withholds (a persisted retired disposition does not
            # prove non-liveness, and the reconcile never closes a pair it cannot prove it owns —
            # review j#79320 R4). Nothing was written or closed on this run.
            if verdict.absent:
                return HibernatedLiveReconcileVerdict(
                    state=RECONCILE_ALREADY,
                    detail=(
                        "the lane is already durably retired and no expected managed slot is "
                        "live; reconcile is an idempotent no-op"
                    ),
                    workspace_id=workspace_id,
                    lane_id=lane_label,
                )
            return _blocked(
                RECON_LIVE_PAIR_PRESENT,
                detail=(
                    "the lane is durably retired with no reconcile owed-close provenance but an "
                    "expected managed pair is live; a persisted retired disposition does not "
                    "prove non-liveness, and the reconcile never closes a pair it cannot prove "
                    "it owns. Success withheld"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        try:
            recorded_close = decode_reconcile_close_pin(record.reconcile_close_pin)
            recorded_declared = tuple(record.declared_pins)
        except (ReconcileClosePinError, ProcessPinError, ValueError):
            recorded_close = None
            recorded_declared = ()
        binding_matches = recorded_close is not None and len(recorded_declared) == 2 and {
            (pin.role, pin.assigned_name, pin.locator) for pin in recorded_close.slots
        } == {
            (pin.provider, pin.assigned_name, pin.locator) for pin in recorded_declared
        }
        if not binding_matches:
            return _blocked(
                RECON_STORE_ERROR,
                detail="the reconcile-retired row carries no readable declared pins to resume",
                workspace_id=workspace_id,
                lane_id=lane_label,
                retired=True,
            )
        closed, ok = _close_owed_pair(recorded_close.slots, recorded_declared)
        if not ok:
            # The row IS durably retired and some pins may already be closed (report both, R7).
            return _blocked(
                RECON_CLOSE_FAILED,
                detail=(
                    "the reconcile-owned owed close did not complete under the full close-time "
                    "re-verification (a busy / pending / duplicate / foreign / recycled "
                    "generation, a failed close, or an unreadable inventory); the expected pair "
                    "is still live (resumable — the reconcile re-runs its own owed close)"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
                retired=True,
                closed=tuple(f"{role} {loc}" for role, loc in closed),
            )
        if verdict.absent:
            return HibernatedLiveReconcileVerdict(
                state=RECONCILE_ALREADY,
                detail="the terminal-bound reconcile owed close is positively complete",
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        return _reconciled(closed)

    # The reconcilable base signature (Redmine #13842 review j#79320 R1): hibernated + durably
    # released + settled replacement + issue binding + owns this exact issue + no project scope +
    # **EMPTY worktree binding AND empty declared_slots** (the legacy signature). A row with ANY
    # existing binding is the #13754 ordinary guarded retire's domain (non-regression), not this
    # legacy-contradiction surface's — a #13809/#13810-bound lane never routes here. Any other
    # shape is not the reconcile's target (an active lane backfills through #13809; a live-zero
    # legacy row migrates through #13841).
    if (
        record.lane_disposition != DISPOSITION_HIBERNATED
        or not stored_binding_kind_is(record.binding_kind, BINDING_KIND_ISSUE)
        or (record.issue_id or "").strip() != issue
        or record.project_scope
        or record.process_release != RELEASE_RELEASED
        or not replacement_settled(record.replacement_state)
        or record.worktree_identity
        or record.declared_slots
    ):
        return _blocked(
            RECON_NOT_RECONCILABLE_STATE,
            detail=(
                "the durable row is not the hibernated + released + settled + owns-issue + "
                "EMPTY-worktree-binding legacy signature the reconcile targets (a bound row "
                "retires through the ordinary #13754 guarded close)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    if verdict.absent:
        # Positive absence with a hibernated (not-yet-retired) row: there is NO live pair to
        # reconcile and NO reconcile owed close — retire-first NEVER leaves a hibernated owed
        # state (it retires only on a verified live pair, so a crash leaves either the pre-CAS
        # hibernated row or the post-CAS retired row, never a hibernated-but-owed one). Route the
        # live-zero legacy row to the #13841 migration; never retire on absence (review j#79282
        # R1: there is no absence -> retire path a #13809 backfill row could collide with).
        return _blocked(
            RECON_LIVE_PAIR_ABSENT,
            detail=(
                "no expected managed slot is live and the row is not reconcile-retired: there "
                "is no live pair to reconcile — migrate the live-zero legacy row via "
                "--migrate-hibernated-legacy (#13841) instead"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    if verdict.state == STATE_BLOCKED:
        return _blocked(
            verdict.reason,
            detail=(
                "the exact live pair is not unique / idle / settled / attested; the "
                "reconcile fails closed zero-write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    # GREEN: the exact live pair is present, unique, live, idle / turn-ended, settled, and
    # generation-bound attested. RETIRE-FIRST (review j#79282 R2): ONE bounded CAS retires the
    # row (writing the worktree + declared_slots binding + this reconcile's decision anchor)
    # guarded on the EXACT verified revision. A rehydrate / move that raced the verify bumps the
    # revision, so the CAS refuses (revision_race) and NOTHING is closed. On success the row is
    # retired (terminal), so the generation can no longer change while the pinned pair is closed
    # — the close therefore never touches a newer active generation (review j#79282 R2 (b)), and
    # retire happens ONLY on a verified live pair so there is no absence -> retire collision with
    # a #13809 row (review j#79282 R1).
    try:
        pins = _pins_from(observation.slots)
    except ProcessPinError as exc:
        return _blocked(
            RECON_STORE_ERROR,
            detail=f"the observed live pair could not be pinned ({exc}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    close_slots = current_generation_release_pins(
        tuple(rows),
        home=getattr(args, "home", None),
        workspace_id=workspace_id,
        lane_id=lane_label,
        targets=tuple(
            (slot.provider, slot.assigned_name, slot.locator)
            for slot in observation.slots
        ),
    )
    if close_slots is None:
        return _blocked(
            RECON_STORE_ERROR,
            detail="the observed pair lacks exact completed current-generation authority",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    close_pin = build_reconcile_close_pin(close_slots)
    # The retire CAS atomically writes the ``reconcile_phase='reconciled'`` provenance ON the row
    # (Redmine #13842 review j#79363 R6), so a crash after the CAS but before the pane close is
    # resumable in this same reconcile authority — no separate losable ledger, and the provenance
    # is recovered with the row by the component's own re-declare.
    # Redmine #13844 R3-F2: retain the store so the reconcile can surface, in its structured
    # verdict (JSON/text), the schema migration its write gate performed — the pre-migration
    # advisory already went to stderr inside ``_connect_write``; this makes it auditable too.
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        lifecycle_migration_payload,
    )

    reconcile_store = LaneReconcileBindingStore(home=getattr(args, "home", None))
    try:
        outcome = reconcile_store.retire_reconciled_hibernated_legacy(
            key,
            expected_revision=record.revision,
            issue_id=issue,
            worktree_identity=metadata_token,
            declared_slots=pins,
            close_pin=close_pin,
            decision=decision,
        )
    except (
        LaneLifecycleError,
        DecisionPointerError,
        ProcessPinError,
        ValueError,
        OSError,
    ) as exc:
        return _blocked(
            RECON_STORE_ERROR,
            detail=f"the reconcile retire CAS raised ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=lifecycle_migration_payload(
                reconcile_store.last_write_preparation
            ),
        )
    migration = lifecycle_migration_payload(reconcile_store.last_write_preparation)
    if not outcome.applied:
        reason_map = {
            CAS_NOT_FOUND: RECON_LANE_NOT_DECLARED,
            CAS_STALE_REVISION: RECON_REVISION_RACE,
            CAS_UNEXPECTED_STATE: RECON_NOT_RECONCILABLE_STATE,
            CAS_FORBIDDEN_TRANSITION: RECON_RELEASE_NOT_PROVEN,
        }
        return _blocked(
            reason_map.get(outcome.reason, RECON_NOT_RECONCILABLE_STATE),
            detail=(
                f"the reconcile retire CAS refused ({outcome.reason}); the row moved since the "
                "reconcile verified its revision, or is not the exact hibernated / released "
                "legacy signature — zero lane-row write and zero-close (any shared-store schema "
                "migration is reported separately)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=migration,
        )
    # Durably retired under the exact verified generation (terminal — no rehydrate possible), with
    # the ``reconcile_phase`` provenance written ON the row. Close the exact pinned pair,
    # re-verifying the full conjunct at close time (R2) and measuring the whole unit afterwards (R3).
    closed, ok = _close_owed_pair(close_pin.slots, pins)
    if not ok:
        # The lane IS durably retired and some pins may already be closed — report both (R7): a
        # blocked verdict here is NOT zero-write / zero-close.
        return _blocked(
            RECON_CLOSE_FAILED,
            detail=(
                "the lane was retired (retire-first) but the exact pinned pair could not be "
                "closed under the full close-time re-verification (a busy / pending / duplicate / "
                "foreign / recycled generation, a failed close, or an unreadable inventory); the "
                "expected pair is still live. The reconcile re-runs its own owed close (the "
                "reconcile_phase provenance persists on the row) — no cross-surface handoff"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            retired=True,
            closed=tuple(f"{role} {loc}" for role, loc in closed),
            lifecycle_migration=migration,
        )
    return _reconciled(closed, lifecycle_migration=migration)


def format_reconcile_text(result: HibernatedLiveReconcileVerdict) -> str:
    """Render the reconcile verdict (Redmine #13842), leading with the verdict."""
    unit = result.workspace_id or "<unresolved>"
    if result.lane_id:
        unit = f"{unit} lane={result.lane_id}"
    header = f"  hibernated live-contradiction reconcile: {result.state}"
    if result.reason:
        header += f" ({result.reason})"
    lines = [f"{header} workspace={unit}"]
    if result.detail:
        lines.append(f"    {result.detail}")
    # Report the durable side effects that ACTUALLY happened, even on a blocked verdict (Redmine
    # #13842 review j#79363 R7): a retire-first blocked run may have already committed the retired
    # disposition and closed some pins, so "nothing was written or closed" is used ONLY when both
    # are truly zero.
    if result.retired:
        lines.append("    - durable write: lane retired (retire-first)")
    for closed in result.closed:
        lines.append(f"    - closed {closed}")
    if result.lifecycle_migration:
        mig = result.lifecycle_migration
        lines.append(
            "    - shared lifecycle store forward-migrated "
            f"v{mig['from_version']} -> v{mig['to_version']} "
            f"(peer lanes at read-fail-closed risk: {mig['peer_active_lanes'] or 'none'})"
        )
    if not result.ok:
        if result.retired or result.closed:
            lines.append(
                "    -> fail-closed: the owed close is INCOMPLETE (side effects above); "
                "the reconcile re-runs its own owed close"
            )
        elif result.lifecycle_migration:
            # Redmine #13844 R4-F2: the lane row was NOT reconciled/closed, but the write gate
            # already forward-migrated the shared-store SCHEMA (a separate side effect above) —
            # "nothing was written" would deny it, so scope the claim to the lane row.
            lines.append(
                "    -> fail-closed: lane NOT reconciled; no lane-row write and no pane close "
                "(the shared-store schema migration above is a separate side effect)"
            )
        else:
            lines.append(
                "    -> fail-closed: lane NOT reconciled; nothing was written or closed"
            )
    return "\n".join(lines)


__all__ = (
    "RECONCILE_RECONCILED",
    "RECONCILE_ALREADY",
    "RECONCILE_BLOCKED",
    "RECON_WORKTREE_BRANCH_MISMATCH",
    "RECON_HEAD_NOT_INTEGRATED",
    "RECON_LIFECYCLE_UNREADABLE",
    "RECON_LANE_NOT_DECLARED",
    "RECON_NOT_RECONCILABLE_STATE",
    "RECON_LIVE_PAIR_ABSENT",
    "RECON_LIVE_PAIR_PRESENT",
    "RECON_REVISION_RACE",
    "RECON_RELEASE_NOT_PROVEN",
    "RECON_STORE_ERROR",
    "RECON_CLOSE_FAILED",
    "HibernatedLiveReconcileVerdict",
    "ReconcileOps",
    "LiveReconcileOps",
    "observe_pair",
    "run_hibernated_live_reconcile",
    "format_reconcile_text",
)
