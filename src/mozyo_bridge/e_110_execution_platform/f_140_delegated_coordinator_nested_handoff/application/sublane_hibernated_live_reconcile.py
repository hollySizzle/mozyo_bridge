"""Public high-level hibernated live-contradiction reconcile (Redmine #13842).

The action-time verification + orchestration of the reconcile that closes the gap #13756
j#79188 exposed: a hibernated / released **legacy** owner row (empty ``worktree_identity``)
whose exact managed pair is observed **live** in the action-time Herdr inventory. Three
contracts leave it with no convergence path — the #13841 live-zero migration refuses on
``live_pair_present``, the #13754 guarded close refuses on ``worktree_binding_unverified``,
and the #13809 backfill is active-row only — so the lane can be neither migrated nor retired
and stalls permanently.

This surface converges it in ONE replayable flow, and ONLY when the exact live pair is
unique, idle / turn-ended, settled, and generation-bound attested:

1. **action-time conjunctive verification** — the lane's ``(workspace, lane, issue)`` identity,
   the ``--worktree``'s actual checked-out branch == ``--branch``, ``--branch`` integrated into
   ``--integration-branch``, the expected assigned names / roles / providers, per-slot startup
   self-attestation (generation-bound to the live locator), pair completeness + uniqueness,
   each agent idle / turn-ended, no pending composer, a settled receiver replacement, and the
   exact lifecycle revision. Every axis fails closed (:mod:`...domain.sublane_hibernated_live_reconcile`
   holds the pure pair decision);
2. **bounded rebind CAS** — re-establish the missing worktree + ``declared_slots`` binding on
   the hibernated row (:class:`...lane_reconcile_binding.LaneReconcileBindingStore`), mutating
   no disposition and re-anchoring the row's decision to **this reconcile** (the owed-state
   provenance, review j#79244 F1);
3. **exact-pair close + revision-guarded terminal ``retired``** — the reconcile OWNS the close
   and the retire (it does NOT delegate to the name-based #13754 guarded close, review j#79244
   F2/F3). It re-observes the live inventory at close time, re-runs the full pair decision, and
   pin-matches the close to the EXACT verified ``(assigned_name, locator)`` pins
   (:func:`...sublane_process_release.pin_matched_close_plan`) — a duplicate / recycled newer
   locator / foreign pair is zero-closed. It then records the #13689 terminal ``retired``
   disposition via a ``transition_disposition`` CAS guarded on the **exact revision** the
   reconcile verified (its rebind post-revision) — a concurrent rehydrate / move is
   ``revision_race`` zero-write, never retiring a newer generation.

Replayability (the "one replayable owed-state flow"): the rebind CAS is idempotent, and a
crash AFTER the pair was closed but BEFORE the retirement was recorded is resumed from the
**durable owed state** — a hibernated row whose binding equals this lane's derived token, whose
``declared_slots`` are present, AND whose decision anchor names THIS reconcile — when the live
pair is now **positively absent**, by recording the owed retirement directly (guarded on the
exact revision), never closing a second time. The decision-anchor provenance is what keeps a
#13809-backfilled row (identical binding shape, different decision) from being mistaken for the
reconcile's owed state (review j#79244 F1). A duplicate replay of the completed flow is an
idempotent ``already_reconciled`` no-op.

Boundary (Redmine #13842): NO process launch / resume, no worktree / branch removal, no raw
Herdr / tmux, no origin/main, no production / tag / publish. The only process mutation is the
pin-matched close of the lane's own exact managed pair.
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
RECON_BINDING_CONFLICT = "binding_conflict"
RECON_RELEASE_NOT_PROVEN = "release_not_proven"
RECON_STORE_ERROR = "store_error"
RECON_CLOSE_FAILED = "close_failed"
#: The exact pinned pair verified at initial observation is no longer intact at close time —
#: a slot was recycled to a newer locator generation, or the pinned name is now ambiguous /
#: duplicated. Zero-close (review j#79244 F2): never close a changed / newer generation.
RECON_PAIR_CHANGED = "pair_changed"


@dataclass(frozen=True)
class HibernatedLiveReconcileVerdict:
    """The fail-closed verdict of the hibernated live-contradiction reconcile.

    ``ok`` (the command's exit-code authority) is true only for a real reconcile or a verified
    idempotent no-op — every other outcome is :data:`RECONCILE_BLOCKED` with the ``reason`` that
    could not be established, never a success.
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    closed: tuple[str, ...] = ()

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
        }


def _blocked(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
) -> HibernatedLiveReconcileVerdict:
    return HibernatedLiveReconcileVerdict(
        state=RECONCILE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
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


def _observe_pair(
    rows: Sequence[Mapping[str, object]],
    ops: ReconcileOps,
    *,
    workspace_id: str,
    lane_id: str,
    managed_pairs: tuple[tuple[str, str], ...],
) -> PairObservation:
    """Gather the content-free per-slot facts for the lane's expected managed pair (#13842).

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
        BINDING_KIND_ISSUE,
        CAS_ALREADY_DECLARED,
        CAS_FORBIDDEN_TRANSITION,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        DISPOSITION_HIBERNATED,
        DISPOSITION_RETIRED,
        RELEASE_RELEASED,
        DecisionPointer,
        DecisionPointerError,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
        ProcessGenerationPin,
        ProcessPinError,
        ReleasePin,
        norm,
        replacement_settled,
    )
    from mozyo_bridge.core.state.lane_reconcile_binding import LaneReconcileBindingStore
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
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
        GATEWAY_ROLE,
        WORKER_ROLE,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_directory_lane_token,
        derive_lane_workspace_token,
    )

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
        workspace_id = herdr_workspace_segment(resolved_worktree)
    except (OSError, ValueError) as exc:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=f"--worktree does not resolve ({type(exc).__name__})",
            lane_id=lane_label,
        )
    try:
        collapsed_to_root = resolved_worktree == repo_root.expanduser().resolve()
    except OSError:
        collapsed_to_root = False
    if collapsed_to_root:
        metadata_token = derive_directory_lane_token(str(resolved_worktree), lane_label)
    else:
        metadata_token = derive_lane_workspace_token(str(resolved_worktree))
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
        record = LaneLifecycleStore().get(key)
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
    live_ops = ops if ops is not None else LiveReconcileOps(repo_root=repo_root)
    try:
        rows = live_ops.agent_rows()
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live herdr inventory unreadable ({exc}); liveness cannot be measured",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    managed_pairs = (
        (gateway_provider, GATEWAY_ROLE),
        (worker_provider, WORKER_ROLE),
    )
    observation = _observe_pair(
        rows,
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_label,
        managed_pairs=managed_pairs,
    )
    verdict = decide_pair_reconcile(observation)

    # Idempotent terminal replay: an already-``retired`` row owning this issue is a verified
    # no-op success ONLY once the readable inventory shows the pair positively absent (a
    # persisted ``retired`` does not prove non-liveness — the #13841 review j#79150 finding 2
    # boundary). A pair (re)appearing live / blocked under a retired row withholds the success.
    if record.lane_disposition == DISPOSITION_RETIRED and (
        record.issue_id or ""
    ).strip() == issue:
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
                "the lane is durably retired but its expected managed pair is live / not "
                "settled; a persisted retired disposition does not prove non-liveness, so "
                "the idempotent success is withheld"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    # The reconcilable base signature: hibernated + durably released + settled replacement +
    # issue binding + owns this exact issue + no project scope. Any other shape is not this
    # reconcile's target (an active lane backfills through #13809; a live pair is retired
    # through #13754; a live-zero legacy row migrates through #13841).
    if (
        record.lane_disposition != DISPOSITION_HIBERNATED
        or norm(record.binding_kind) != BINDING_KIND_ISSUE
        or (record.issue_id or "").strip() != issue
        or record.project_scope
        or record.process_release != RELEASE_RELEASED
        or not replacement_settled(record.replacement_state)
    ):
        return _blocked(
            RECON_NOT_RECONCILABLE_STATE,
            detail=(
                "the durable row is not the hibernated + released + settled + owns-issue "
                "legacy signature the reconcile targets"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    def _terminal_retire(expected_revision: int, *, closed=(), resumed: bool = False):
        """The revision-guarded ``hibernated -> retired`` terminal write (review j#79244 F3).

        CAS'd on the EXACT ``expected_revision`` the reconcile verified (the rebind post-revision
        on the forward path, or the read revision on an owed resume) — NOT a re-read latest
        revision. A concurrent rehydrate / move since that verification bumps the revision (or
        leaves the row non-``hibernated``), so the guard refuses and the reconcile reports
        ``revision_race`` zero-write rather than retiring a newer active generation.
        """
        try:
            outcome = LaneLifecycleStore().transition_disposition(
                key,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=expected_revision,
                target=DISPOSITION_RETIRED,
                decision=decision,
            )
        except (LaneLifecycleError, DecisionPointerError, ValueError, OSError) as exc:
            return _blocked(
                RECON_STORE_ERROR,
                detail=f"the terminal retire CAS raised ({type(exc).__name__}); fail closed",
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        if outcome.applied:
            detail = (
                "resumed the owed retirement from durable owed state (reconcile provenance + "
                "positive absence): the pair was already closed, so the terminal retired "
                "disposition was recorded on the exact verified revision"
                if resumed
                else (
                    "the hibernated / released legacy lane's binding was re-established, its "
                    "exact live pair closed, and the terminal retired disposition recorded on "
                    "the exact verified revision (one replayable flow)"
                )
            )
            return HibernatedLiveReconcileVerdict(
                state=RECONCILE_RECONCILED,
                detail=detail,
                workspace_id=workspace_id,
                lane_id=lane_label,
                closed=tuple(f"{role} {loc}" for role, loc in closed),
            )
        return _blocked(
            RECON_REVISION_RACE,
            detail=(
                f"the terminal retire CAS refused ({outcome.reason}): the row moved since the "
                "reconcile verified its revision (a concurrent rehydrate / transition) — never "
                "retiring a newer generation"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    if verdict.absent:
        # Positive absence. Resume an owed retirement ONLY from reconcile-specific durable
        # provenance (review j#79244 F1): the binding is re-established to THIS lane's derived
        # token, a pair was committed (declared_slots present), AND the row's decision anchor
        # names THIS reconcile — so a #13809 backfill row (identical worktree + declared_slots
        # shape, but its decision names the declare / hibernate journal) is NEVER mistaken for
        # the reconcile's owed close. A positive absence with all three is the crash-after-close
        # window: record the terminal retirement guarded on the exact revision, never a 2nd
        # close. Otherwise there is no reconcile owed state: route the live-zero legacy row to
        # the #13841 migration.
        if (
            record.worktree_identity == metadata_token
            and record.declared_slots
            and record.decision_source == decision.source
            and record.decision_issue_id == decision.issue_id
            and record.decision_journal == decision.journal_id
        ):
            return _terminal_retire(record.revision, resumed=True)
        return _blocked(
            RECON_LIVE_PAIR_ABSENT,
            detail=(
                "no expected managed slot is live and the row carries no reconcile owed-state "
                "provenance (binding token / declared slots / this reconcile's decision "
                "anchor): there is no live pair to reconcile — migrate the live-zero legacy "
                "row via --migrate-hibernated-legacy (#13841) instead"
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
    # generation-bound attested. Re-establish the missing worktree + process binding (with this
    # reconcile's decision anchor as provenance), then close the EXACT verified pair and record
    # the terminal ``retired`` disposition — the reconcile owns the close + retire so both are
    # bound to the exact generation it verified (review j#79244 F2/F3), never delegated to a
    # name-based / latest-revision path.
    try:
        pins = [
            ProcessGenerationPin(
                role=slot.role,
                provider=slot.provider,
                assigned_name=slot.assigned_name,
                locator=slot.locator,
                attested_at=slot.attested_at,
            )
            for slot in observation.slots
        ]
    except ProcessPinError as exc:
        return _blocked(
            RECON_STORE_ERROR,
            detail=f"the observed live pair could not be pinned ({exc}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        rebind = LaneReconcileBindingStore().rebind_released_hibernated_legacy(
            key,
            expected_revision=record.revision,
            issue_id=issue,
            worktree_identity=metadata_token,
            declared_slots=pins,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ProcessPinError, ValueError, OSError) as exc:
        return _blocked(
            RECON_STORE_ERROR,
            detail=f"the reconcile rebind CAS raised ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not rebind.applied:
        reason_map = {
            CAS_NOT_FOUND: RECON_LANE_NOT_DECLARED,
            CAS_STALE_REVISION: RECON_REVISION_RACE,
            CAS_FORBIDDEN_TRANSITION: RECON_RELEASE_NOT_PROVEN,
            CAS_ALREADY_DECLARED: RECON_BINDING_CONFLICT,
        }
        return _blocked(
            reason_map.get(rebind.reason, RECON_NOT_RECONCILABLE_STATE),
            detail=(
                f"the reconcile rebind CAS refused ({rebind.reason}); the row is not the "
                "exact hibernated / released / rebindable legacy signature"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    rebind_revision = rebind.revision

    # Close-time re-verification (review j#79244 F2): re-read the live inventory and re-run the
    # FULL pair decision, so a duplicate / recycled newer locator / foreign / working pair that
    # appeared BETWEEN the initial observation and the close is caught here — the initial
    # observation is not trusted as the close authority.
    try:
        rows2 = live_ops.agent_rows()
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=(
                f"live herdr inventory unreadable at close time ({exc}); the binding is "
                "re-established (resumable), but nothing is closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    observation2 = _observe_pair(
        rows2,
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_label,
        managed_pairs=managed_pairs,
    )
    verdict2 = decide_pair_reconcile(observation2)
    if verdict2.absent:
        # The pair vanished after the rebind (nothing to close). The binding + provenance are
        # durable, so record the owed terminal retirement guarded on the rebind revision.
        return _terminal_retire(rebind_revision, resumed=True)
    if verdict2.state == STATE_BLOCKED:
        # A duplicate / foreign / not-idle / pending pair appeared at close time: zero-close.
        # The binding + provenance persist, so a later reconcile resumes once the pair settles.
        return _blocked(
            verdict2.reason,
            detail=(
                "the exact live pair changed between the initial observation and the close "
                "(re-verification failed); zero-close, the binding persists (resumable)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if sorted(slot.locator for slot in observation2.slots) != sorted(
        p.locator for p in pins
    ):
        # Still a clean pair, but at DIFFERENT locators than the ones the reconcile verified and
        # pinned — a recycled newer generation. Zero-close (never close a generation the
        # reconcile did not attest).
        return _blocked(
            RECON_PAIR_CHANGED,
            detail=(
                "the live pair was recycled to a newer locator generation since the reconcile "
                "verified it; zero-close (the reconcile only closes its exact pinned pair)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Pin-matched close of the EXACT verified pair (review j#79244 F2): exact
    # ``(assigned_name, locator)`` targets, re-resolved against the live inventory. A pinned
    # name live at more than one locator, or a foreign / undecodable pin, fails the WHOLE plan
    # closed (``None``); any pin whose exact locator is gone leaves the plan short of the full
    # pair. Either way -> zero-close, never a name-based sweep and never a partial close.
    plan = pin_matched_close_plan(
        [
            ReleasePin(role=p.provider, assigned_name=p.assigned_name, locator=p.locator)
            for p in pins
        ],
        rows2,
        workspace_id=workspace_id,
        lane_id=lane_label,
    )
    if plan is None or len(plan.close_targets) != len(pins):
        return _blocked(
            RECON_PAIR_CHANGED,
            detail=(
                "the exact pinned pair is no longer intact / is ambiguous at close time; "
                "zero-close (the reconcile never closes a changed or duplicate generation)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        close = execute_herdr_retire_close(plan)
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=(
                f"the pin-matched close could not run ({exc}); the binding persists (resumable)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if close.failed:
        return _blocked(
            RECON_CLOSE_FAILED,
            detail=(
                f"{len(close.failed)} managed slot(s) failed to close; the lane still holds "
                "live agents (binding persists, resumable)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    return _terminal_retire(rebind_revision, closed=close.closed)


# ---------------------------------------------------------------------------
# Live observation adapter (thin; reuses the established herdr readers).
# ---------------------------------------------------------------------------


@dataclass
class LiveReconcileOps:
    """The live :class:`ReconcileOps`: raw inventory + per-slot runtime / composer / attestation.

    Reuses the same readers the #13763 quarantine inspection uses — the raw ``agent list``
    inventory, the ``agent get`` runtime state, a content-free composer observation over
    ``read_pane``, and the startup self-attestation store — so the reconcile and the quarantine
    read one runtime the same way.
    """

    repo_root: Path
    env: Optional[Mapping[str, str]] = None

    def _environ(self) -> Mapping[str, str]:
        return self.env if self.env is not None else os.environ

    def agent_rows(self) -> Sequence[Mapping[str, object]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        return list_herdr_agent_rows(self._environ())

    def read_attestation(self, assigned_name: str):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        try:
            return HerdrIdentityAttestationStore().read(assigned_name)
        except Exception:  # noqa: BLE001 - unreadable attestation fails closed (absent)
            return None

    def _reader(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            _resolve_binary_or_die,
        )

        return _resolve_binary_or_die(self._environ())

    def runtime_state(self, locator: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E501
            HerdrCliAgentStateReader,
        )

        try:
            binary = self._reader()
            state = HerdrCliAgentStateReader(binary).read_agent_state(locator)
            return state.state if state.ok else "unknown"
        except Exception:  # noqa: BLE001 - a failed runtime read is fail-soft to unknown
            return "unknown"

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            observe_composer_text,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            HerdrCliTransport,
        )

        try:
            binary = self._reader()
            read = HerdrCliTransport(binary).read_pane(locator, lines=80)
            if not read.ok:
                return (False, None)
            observation = observe_composer_text(read.content)
            return (observation.readable, observation.has_pending)
        except Exception:  # noqa: BLE001 - a failed composer read is fail-soft to unreadable
            return (False, None)


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
    if not result.ok:
        lines.append(
            "    -> fail-closed: lane NOT reconciled; nothing was written or closed"
        )
    for closed in result.closed:
        lines.append(f"    - closed {closed}")
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
    "RECON_BINDING_CONFLICT",
    "RECON_RELEASE_NOT_PROVEN",
    "RECON_STORE_ERROR",
    "RECON_CLOSE_FAILED",
    "RECON_PAIR_CHANGED",
    "HibernatedLiveReconcileVerdict",
    "ReconcileOps",
    "LiveReconcileOps",
    "run_hibernated_live_reconcile",
    "format_reconcile_text",
)
