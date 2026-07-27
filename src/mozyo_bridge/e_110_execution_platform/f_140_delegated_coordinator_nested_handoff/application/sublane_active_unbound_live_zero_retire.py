"""Active UNBOUND live-zero terminal retire (Redmine #14499).

The fifth retire intent, for the lane shape the other four leave permanently stuck: an
**ACTIVE, issue-bound lifecycle row whose ``worktree_identity`` is EMPTY** (a pre-#13754
row) whose managed pair is already positively gone, on a lane whose issue is closed and
whose head is integrated.

Live evidence #14456 j#87973, recorded immediately after that issue's ``task_close``:
``issue 14456`` / ``disposition active`` / ``process_release not_requested`` /
``binding_kind issue`` / ``generation 1`` / ``revision 1`` / **worktree identity empty**, zero
managed panes live, only the stale locators ``pF`` / ``pG`` remaining. The journal names the
gap directly: *"ordinary retire は binding 必須、``--retire-active-live-zero`` も BOUND row
専用、legacy migration は HIBERNATED+released 専用。本 row は ACTIVE+UNBOUND+live-zero の
ため該当 rail なし"*. Redmine #14499's audit found the same shape across the post-reboot
lanes, so this is a recurring residue class rather than one row's accident.

**What stands in for the worktree attestation.** Every bound rail proves it is aiming at the
right lane by attesting the caller's ``--worktree`` against the row's recorded canonical
binding. This row has none, so that proof is unavailable — and inventing one (deriving a
token from a path the caller supplies, unbacked by the durable record) would be a *weaker*
guarantee wearing the same name. Instead the caller must declare the exact
``(lane_generation, revision)`` it measured the live-zero read against, and the CAS applies
only if the row is still at both. A lane that was re-incarnated between the read and the
write — the legitimate way an "empty" lane comes back — loses the CAS rather than being
terminalized on a stale reading.

The lane UNIT is resolved from ``--repo`` (the coordinator's main checkout), not from a
worktree: after a reboot the recorded worktree is typically gone, and requiring it would
reproduce the very ``worktree_binding_unverified`` dead end this surface exists to escape.
``--worktree``, when supplied, is used for ONE thing — deriving the pre-#13377 legacy twin
token so the live-zero scan also covers that unit. It never attests anything here.

Everything else mirrors #14242 deliberately, so an operator reads one vocabulary across all
five retire intents and a reviewer can diff the two surfaces line for line: the same
launch/terminalize exclusion (the #13882 boundary-3 lock taken EXCLUSIVE across the whole
action-time half), the same shared four-fence live-zero measurement, the same
re-verified idempotent replay, and metadata only — no process launch / close / resume, no
worktree or branch removal.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E501
        PatchEquivalentResolution,
    )

# -- terminal retire verdict vocabulary --------------------------------------

#: The lane was terminalized: the bounded CAS moved the active unbound row to the #13689
#: terminal ``retired`` disposition. Metadata only — no process was touched.
UNBOUND_RETIRE_RETIRED = "retired"
#: A verified idempotent no-op: the row is already ``retired`` and owns this exact issue, so
#: a duplicate replay succeeds without a second write (re-verified live-zero first).
UNBOUND_RETIRE_ALREADY_RETIRED = "already_retired"
#: Fail-closed: the retire proved nothing and wrote nothing. Never exit 0.
UNBOUND_RETIRE_BLOCKED = "blocked"

#: Blocked reasons. The liveness ones are the shared four-fence vocabulary (#14242), reused
#: verbatim so one set of strings spans every terminal retire intent.
UNBOUND_RETIRE_LIVE_PAIR_PRESENT = "live_pair_present"
UNBOUND_RETIRE_FOREIGN_INVENTORY_PRESENT = "foreign_inventory_present"
UNBOUND_RETIRE_DUPLICATE_INVENTORY = "duplicate_inventory"
UNBOUND_RETIRE_EXPECTED_IDENTITY_UNRESOLVED = "expected_identity_unresolved"
UNBOUND_RETIRE_HEAD_NOT_INTEGRATED = "head_not_integrated"
UNBOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED = "patch_equivalence_unverified"
UNBOUND_RETIRE_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
#: The caller did not declare the exact ``(generation, revision)`` it measured against. That
#: declaration is this surface's whole identity fence, so its absence is refused rather than
#: defaulted — a defaulted fence is no fence.
UNBOUND_RETIRE_FENCE_NOT_DECLARED = "generation_fence_not_declared"
#: The bounded CAS refused: the row is not the exact ACTIVE / issue-bound / EMPTY-binding
#: signature — e.g. a ``hibernated`` row, a ``superseded`` row, a project-gateway binding, a
#: different issue, or a row that DOES record a worktree binding (which belongs to #14242,
#: where it can be attested).
UNBOUND_RETIRE_NOT_ACTIVE_UNBOUND_STATE = "not_active_unbound_state"
#: The bounded CAS refused: a process release is in flight or a receiver replacement is
#: unsettled, so the live-zero read may be observing a mid-actuation state.
UNBOUND_RETIRE_RELEASE_IN_FLIGHT = "release_in_flight"
#: The bounded CAS refused: no durable lifecycle owner row.
UNBOUND_RETIRE_LANE_NOT_DECLARED = "lane_not_declared"
#: The bounded CAS refused: the row's generation or revision no longer matches what the
#: caller declared — a concurrent declare / transition / generation open moved it.
UNBOUND_RETIRE_GENERATION_RACE = "generation_race"
#: The bounded CAS raised a store error (surfaced, not swallowed).
UNBOUND_RETIRE_STORE_ERROR = "store_error"
#: The home's attestation-store lock could not be taken EXCLUSIVELY, so a managed launch (or
#: a self-attestation write, or maintenance) is in flight on this home right now.
UNBOUND_RETIRE_LAUNCH_IN_FLIGHT = "launch_in_flight"
#: Advisory file locking is unavailable on this platform, so the launch/terminalize
#: exclusion protocol cannot be honored.
UNBOUND_RETIRE_EXCLUSION_UNAVAILABLE = "exclusion_unavailable"
#: The lane unit's workspace identity could not be resolved from ``--repo``.
UNBOUND_RETIRE_WORKSPACE_UNRESOLVED = "workspace_unresolved"
#: The issue's ACTIVE owning lane does not resolve to exactly one row, or resolves to a
#: DIFFERENT lane than ``--lane-label`` (Redmine #14499 review j#89191 finding 1). This is
#: the *lane selection* proof, as opposed to the generation/revision *freshness* proof: the
#: CAS below verifies the row at ``(workspace, lane_label)`` is fresh and owns the issue, but
#: on its own it cannot tell a correctly-aimed retire from one aimed at a sibling lane of the
#: same issue that happens to carry the same generation and revision (``1``/``1`` for every
#: freshly declared lane). The durable store's write paths do refuse to create two ACTIVE
#: owners for one issue — measured: ``declare_lane`` and the hibernated→active rehydrate both
#: return ``owner_conflict`` — so that shape is not reachable today. This surface nonetheless
#: checks it, because it is the ONLY terminal rail with no second identity axis (the four
#: bound rails attest a worktree token), and the codebase's posture is to verify the
#: invariant it depends on rather than inherit it: #14242's duplicate-inventory fence exists
#: even though "a herdr assigned name is unique by construction".
UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN = "lane_selection_unproven"
#: ``--branch`` is not the branch this lane's durable metadata records, so the head-integration
#: evidence describes some other branch (Redmine #14499 review j#89191 finding 2). Measured:
#: ``branch_integrated("integration", "integration")`` is ``True``, so without this check a
#: caller passing the integration branch as ``--branch`` clears the head gate for a lane whose
#: real head was never integrated.
UNBOUND_RETIRE_BRANCH_NOT_LANE_BOUND = "branch_not_lane_bound"


@dataclass(frozen=True)
class ActiveUnboundLiveZeroRetireVerdict:
    """The fail-closed verdict of the metadata-only active UNBOUND live-zero retire.

    ``ok`` (the command's exit-code authority) is true only for a real terminalization or a
    verified idempotent no-op; every other outcome is :data:`UNBOUND_RETIRE_BLOCKED`.
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    expected_live: tuple[str, ...] = ()
    foreign_names: tuple[str, ...] = ()
    lifecycle_migration: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.state in (UNBOUND_RETIRE_RETIRED, UNBOUND_RETIRE_ALREADY_RETIRED)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "expected_live": list(self.expected_live),
            "foreign_names": list(self.foreign_names),
            "lifecycle_migration": self.lifecycle_migration,
        }


def _blocked(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
    expected_live: tuple[str, ...] = (),
    foreign_names: tuple[str, ...] = (),
    lifecycle_migration: Optional[dict] = None,
) -> ActiveUnboundLiveZeroRetireVerdict:
    return ActiveUnboundLiveZeroRetireVerdict(
        state=UNBOUND_RETIRE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_live=expected_live,
        foreign_names=foreign_names,
        lifecycle_migration=lifecycle_migration,
    )


def run_active_unbound_live_zero_retire(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    head_integrated: Optional[bool],
    patch_equivalent: Optional["PatchEquivalentResolution"] = None,
):
    """Metadata-only terminalize an ACTIVE UNBOUND lane whose pair is proven gone (#14499).

    Returns an :class:`ActiveUnboundLiveZeroRetireVerdict`, or ``None`` when the repo is not
    on the herdr backend.

    The command runs this only when its ``may_retire`` preflight already passed, so the
    "closed + no review / owner / callback debt" axes are established upstream and are not
    restated here. This adds the axes the preflight cannot: the caller's declared
    generation/revision fence, head integration, the positive live-zero inventory read, and
    the active-unbound-state CAS.

    Note that ``--worktree`` is deliberately NOT required and never attested: the whole
    point of this surface is the row that has no binding to attest, and after a reboot the
    recorded worktree is typically gone. There is correspondingly no worktree-dirty or
    worktree/branch identity gate here — neither is measurable — which is exactly why the
    generation/revision fence is mandatory rather than optional.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
        repo_scope_workspace_id,
    )

    if not repo_backend_is_herdr(repo_root):
        return None
    lane_label = (getattr(args, "lane_label", "") or "").strip()
    issue = (getattr(args, "issue", "") or "").strip()

    # The declared fence, read BEFORE anything else: it is this surface's identity proof, so
    # an undeclared or non-positive one refuses without reading or measuring anything.
    try:
        expect_generation = int(getattr(args, "expect_lane_generation", 0) or 0)
        expect_revision = int(getattr(args, "expect_lane_revision", 0) or 0)
    except (TypeError, ValueError):
        expect_generation = expect_revision = 0
    if expect_generation < 1 or expect_revision < 1:
        return _blocked(
            UNBOUND_RETIRE_FENCE_NOT_DECLARED,
            detail=(
                "--expect-lane-generation and --expect-lane-revision must both name the "
                "positive values the caller read from the lane's durable row (see "
                "`sublane reboot-audit`). This surface has no worktree binding to attest, "
                "so that declaration IS its identity fence; defaulting it would mean "
                "terminalizing whatever row happens to sit at this address"
            ),
            lane_id=lane_label,
        )

    workspace_id = repo_scope_workspace_id(repo_root)
    if not workspace_id:
        return _blocked(
            UNBOUND_RETIRE_WORKSPACE_UNRESOLVED,
            detail=(
                "the lane unit's workspace identity cannot be resolved from --repo; the "
                "terminal retire fails closed rather than guessing which workspace's lane "
                "of this name it means"
            ),
            lane_id=lane_label,
        )

    # ---- branch <-> lane binding (Redmine #14499 review j#89191 finding 2) -------------
    #
    # `head_integrated` is computed from the caller's --branch alone. The bound rails tie
    # that branch to the lane by requiring the ATTESTED worktree to be checked out on it;
    # this rail has no worktree, and the first version simply dropped the tie instead of
    # replacing it. Measured consequence: `branch_integrated("integration", "integration")`
    # is True, so `--branch <integration-branch>` cleared the head gate for any lane,
    # including one whose real head was never integrated.
    #
    # The lane's branch is not recorded in the lifecycle (authority) store at all — only in
    # the lane METADATA store. That store is documented as a display join and reads fail-open
    # to empty, which would normally disqualify it as an authority. It is sound here because
    # it is used ONLY to REFUSE: an unreadable/absent/empty record blocks, and a mismatch
    # blocks. A fail-open read used to narrow can never widen what is permitted.
    branch_ok, branch_detail = _verify_branch_binds_to_lane(
        args, workspace_id=workspace_id, lane_label=lane_label
    )
    if not branch_ok:
        return _blocked(
            UNBOUND_RETIRE_BRANCH_NOT_LANE_BOUND,
            detail=branch_detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    # Head integration is an action-time invariant the retire preflight (run with
    # merge_on_retire=False) does not check. Identical to the bound surfaces.
    if head_integrated is not True:
        if patch_equivalent is None:
            return _blocked(
                UNBOUND_RETIRE_HEAD_NOT_INTEGRATED,
                detail=(
                    "--branch is not a verified ancestor of --integration-branch "
                    "(unintegrated or the ancestry probe could not answer); the lane's head "
                    "must be integrated before a terminal retire. The branch and its "
                    "commits are preserved either way"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        if not patch_equivalent.admissible:
            return _blocked(
                UNBOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED,
                detail=(
                    "--branch is not a literal ancestor of --integration-branch and the "
                    "supplied patch-equivalent integration disposition did not verify at "
                    f"action-time ({patch_equivalent.reason}): {patch_equivalent.detail}"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )

    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        AttestationStoreLockBusy,
        AttestationStoreLockUnavailable,
        attestation_store_lock,
    )
    from mozyo_bridge.shared.paths import mozyo_bridge_home

    # ---- the launch / terminalize exclusion (Redmine #14242 j#85269, reused) ------------
    #
    # Boundary 3 of the #13882 three-boundary protocol. Every managed launch holds this
    # home's attestation-store lock SHARED, non-blocking, from before its first attestation
    # read through its last actuation, so taking it EXCLUSIVE here is a reader-writer
    # exclusion over the relaunch window the revision fence alone cannot see (a launch does
    # not mutate the lifecycle row, so ``revision`` would be unchanged). A launch already
    # holding shared makes this acquire fail (zero-write); this terminalize holding
    # exclusive makes the launch's acquire fail at admission (zero-spawn). A holder crash
    # releases it at the OS level, so there is no stale claim or TTL to get wrong.
    try:
        with attestation_store_lock(
            mozyo_bridge_home(), exclusive=True, blocking=False
        ):
            return _terminalize_under_exclusion(
                args,
                repo_root,
                workspace_id=workspace_id,
                lane_label=lane_label,
                issue=issue,
                journal=(getattr(args, "journal", "") or "").strip(),
                expect_generation=expect_generation,
                expect_revision=expect_revision,
            )
    except AttestationStoreLockBusy as exc:
        return _blocked(
            UNBOUND_RETIRE_LAUNCH_IN_FLIGHT,
            detail=(
                f"the home's attestation-store lock is held by another operation ({exc}); a "
                "managed launch, a self-attestation write, or maintenance is in flight, so a "
                "live-zero measurement taken now could be invalidated before the write. "
                "Nothing was read or written; re-run once it finishes"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    except AttestationStoreLockUnavailable as exc:
        return _blocked(
            UNBOUND_RETIRE_EXCLUSION_UNAVAILABLE,
            detail=(
                f"advisory file locking is unavailable on this platform ({exc}), so the "
                "launch / terminalize exclusion cannot be honored. Terminalizing without it "
                "would advertise a guarantee that is not there"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )


def _verify_branch_binds_to_lane(
    args: argparse.Namespace, *, workspace_id: str, lane_label: str
) -> tuple[bool, str]:
    """Is ``--branch`` the branch this lane's durable metadata records? (#14499 finding 2)

    Returns ``(ok, detail)``; ``detail`` is the refusal reason when ``ok`` is false. Every
    failure mode — no ``--branch``, no metadata record for the lane unit, a record carrying
    no branch, a mismatch, or a metadata read error — refuses. The check only ever narrows,
    so the metadata store's fail-open read cannot turn into a fail-open *permit*.

    ``--branch == --integration-branch`` is refused separately and first: that is a branch
    which is trivially its own ancestor, so the head-integration probe returns ``True``
    without measuring anything about the lane. Even when the lane's record happened to name
    the integration branch, "the integration branch is integrated" is not evidence that this
    lane's work reached it.
    """
    from mozyo_bridge.core.state.lane_metadata import (
        lane_records_by_unit,
        load_lane_records,
    )

    branch = (getattr(args, "branch", "") or "").strip()
    integration = (getattr(args, "integration_branch", "") or "").strip()
    if not branch:
        return False, (
            "--branch is required: head integration is measured against it, and without it "
            "there is nothing to bind to this lane"
        )
    if integration and branch == integration:
        return False, (
            f"--branch and --integration-branch are both {branch!r}; a branch is trivially "
            "its own ancestor, so the head-integration probe would pass without measuring "
            "anything about this lane's work"
        )
    try:
        recorded = lane_records_by_unit(load_lane_records()).get(
            (workspace_id, lane_label)
        )
    except Exception:  # noqa: BLE001 - an unreadable display store refuses, never permits
        return False, (
            "the lane metadata store could not be read, so --branch cannot be bound to this "
            "lane; the terminal retire fails closed rather than trusting the caller's branch"
        )
    if recorded is None:
        return False, (
            f"no lane metadata record exists for unit ({workspace_id}, {lane_label}), so "
            "there is no durable record of which branch this lane owns; --branch cannot be "
            "verified and the head-integration evidence cannot be attributed to this lane"
        )
    lane_branch = (recorded.branch or "").strip()
    if not lane_branch:
        return False, (
            "this lane's metadata record carries no branch, so --branch cannot be bound to "
            "it; the head-integration evidence cannot be attributed to this lane"
        )
    if lane_branch != branch:
        return False, (
            f"--branch {branch!r} is not this lane's recorded branch {lane_branch!r}; the "
            "head-integration evidence would describe a different branch's history"
        )
    return True, ""


def _terminalize_under_exclusion(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    workspace_id: str,
    lane_label: str,
    issue: str,
    journal: str,
    expect_generation: int,
    expect_revision: int,
):
    """The action-time half, run while HOLDING the exclusive launch-exclusion lock (#14499).

    Everything here is re-read under the lock: the durable lifecycle row, the live
    inventory, and every liveness gate. Split into its own function so the lock's scope is
    the function boundary — it is impossible to add a gate that accidentally runs outside
    the exclusion.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        DISPOSITION_ACTIVE,
        DISPOSITION_RETIRED,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import OWNER_RESOLVED
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_PROVIDER_UNRESOLVED,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_live_zero_measurement import (  # noqa: E501
        measure_live_zero,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
        is_lane_workspace_token,
    )

    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
    except ValueError:
        return _blocked(
            UNBOUND_RETIRE_WORKSPACE_UNRESOLVED,
            detail=(
                "the lane unit cannot be keyed (empty workspace / lane); its identity cannot "
                "be established before a terminal retire"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        record = LaneLifecycleStore().get(key)
    except (LaneLifecycleError, OSError) as exc:
        return _blocked(
            UNBOUND_RETIRE_LIFECYCLE_UNREADABLE,
            detail=(
                f"the lifecycle store is unreadable ({type(exc).__name__}); the lane's state "
                "cannot be verified, so the terminal retire fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            UNBOUND_RETIRE_LANE_NOT_DECLARED,
            detail=(
                "the lane unit has no durable lifecycle owner row; there is no active state "
                "to terminalize"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    # ---- lane SELECTION proof (Redmine #14499 review j#89191 finding 1) ----------------
    #
    # The CAS below proves the row at this address is FRESH (generation + revision) and owns
    # this issue. It does not prove the caller aimed at the right lane: a sibling lane of the
    # same issue, freshly declared, carries the same generation and revision, so a mistyped
    # --lane-label would satisfy every one of the CAS's predicates. The four bound rails do
    # not have this hole because their worktree token is a second, independent identity axis;
    # this rail has none, so the proof is taken from the durable owner index instead.
    #
    # Resolved under the same exclusive lock as the CAS, so the answer cannot move between the
    # check and the write. Exactly-one is required: OWNER_AMBIGUOUS means the invariant the
    # store's write paths maintain has been violated, and no terminal write may be licensed by
    # an index that is not holding; OWNER_ABSENT means no ACTIVE row owns the issue at all.
    #
    # Scoped to ``active`` — the ONLY disposition from which this surface writes (review
    # j#89238 finding 1). Applying it unconditionally, as the first version did, broke the
    # idempotent replay: a terminalized row is ``retired``, which drops it out of the ACTIVE
    # owner index, so the second run resolved OWNER_ABSENT and returned
    # ``lane_selection_unproven`` instead of ``already_retired`` (measured: first
    # ``retired``, replay ``blocked``). A fence guarding a write must not run on a path that
    # performs none. The two non-active dispositions keep their own, more precise refusals:
    # a ``retired`` row owning this issue is the idempotent success below (still re-verified
    # against a fresh live-zero read first), and ``hibernated`` / ``superseded`` fall to the
    # CAS's ``not_active_unbound_state``, which names the disposition rather than blaming
    # lane selection for it.
    if record.lane_disposition == DISPOSITION_ACTIVE:
        owner = LaneLifecycleStore().resolve_owner(workspace_id, issue)
        if owner.status != OWNER_RESOLVED:
            return _blocked(
                UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN,
                detail=(
                    f"issue #{issue} does not resolve to exactly one ACTIVE owning lane "
                    f"({owner.status}: {owner.detail}); with no worktree binding to attest, a "
                    "unique owner is this surface's only proof that --lane-label names the "
                    "lane the audit selected"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        if owner.lane_id != lane_label:
            return _blocked(
                UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN,
                detail=(
                    f"issue #{issue} is owned by lane {owner.lane_id!r}, not the requested "
                    f"{lane_label!r}; the requested lane is not this issue's owner, so "
                    "terminalizing it would retire a lane the caller did not select"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )

    # The legacy twin token, derived from --worktree when one was supplied. Used ONLY to
    # widen the live-zero scan to the pre-#13377 unit, never to attest anything: an unbound
    # row has no binding to compare it against, and a caller-supplied path is not evidence.
    legacy_token = ""
    worktree = getattr(args, "worktree", None)
    if worktree:
        try:
            candidate = derive_lane_workspace_token(
                str(Path(worktree).expanduser().resolve())
            )
            legacy_token = candidate if is_lane_workspace_token(candidate) else ""
        except (OSError, ValueError):
            legacy_token = ""

    # The live-zero read. With no release witness AND no worktree binding available, this is
    # the only liveness authority, so it runs BEFORE the idempotent already-retired success
    # too: a persisted ``retired`` does not prove the pair is currently gone.
    try:
        measurement = measure_live_zero(
            repo_root,
            workspace_id=workspace_id,
            lane_label=lane_label,
            legacy_workspace_id=legacy_token,
            env=os.environ,
        )
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live herdr inventory unreadable ({exc}); liveness cannot be measured",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    except WorkflowProviderUnresolved as exc:
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            detail=f"workflow provider binding unresolved ({exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if not measurement.proven:
        return _blocked(
            measurement.reason,
            detail=measurement.detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
            expected_live=measurement.expected_live,
            foreign_names=measurement.foreign_names,
        )
    # Only now is a persisted terminal state a verified success.
    if record.lane_disposition == DISPOSITION_RETIRED and record.issue_id == issue:
        return ActiveUnboundLiveZeroRetireVerdict(
            state=UNBOUND_RETIRE_ALREADY_RETIRED,
            detail=(
                "the lane is already terminally retired and its expected managed slots "
                "measure positively absent; duplicate replay is an idempotent no-op with "
                "zero writes"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )

    from mozyo_bridge.core.state.lane_active_unbound_retire import (
        LaneActiveUnboundRetireStore,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import (
        CAS_FORBIDDEN_TRANSITION,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        DecisionPointer,
        DecisionPointerError,
    )

    try:
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except DecisionPointerError as exc:
        return _blocked(
            UNBOUND_RETIRE_NOT_ACTIVE_UNBOUND_STATE,
            detail=(
                f"the retire decision anchor is incomplete ({exc}); a terminal retire must "
                "name the durable journal that authorized it"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    store = LaneActiveUnboundRetireStore()
    try:
        outcome = store.retire_active_unbound_live_zero(
            key,
            expected_revision=expect_revision,
            expected_generation=expect_generation,
            issue_id=issue,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ValueError) as exc:
        return _blocked(
            UNBOUND_RETIRE_STORE_ERROR,
            detail=f"the bounded unbound retire CAS failed ({type(exc).__name__}: {exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    migration_payload = _migration_payload(
        getattr(store, "last_write_preparation", None)
    )
    if outcome.applied:
        return ActiveUnboundLiveZeroRetireVerdict(
            state=UNBOUND_RETIRE_RETIRED,
            detail=(
                "the active unbound row was terminalized to retired (metadata only); its "
                "empty worktree binding, declared pins and generation are preserved, and no "
                "process was launched, closed or resumed. No worktree, branch or commit was "
                "removed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=migration_payload,
        )
    reason_map = {
        CAS_NOT_FOUND: UNBOUND_RETIRE_LANE_NOT_DECLARED,
        CAS_STALE_REVISION: UNBOUND_RETIRE_GENERATION_RACE,
        CAS_FORBIDDEN_TRANSITION: UNBOUND_RETIRE_RELEASE_IN_FLIGHT,
    }
    reason = reason_map.get(outcome.reason, UNBOUND_RETIRE_NOT_ACTIVE_UNBOUND_STATE)
    return _blocked(
        reason,
        detail=(
            f"the bounded active unbound live-zero retire CAS refused ({outcome.reason}); "
            "the durable row is not the exact active / issue-bound / EMPTY-binding "
            "signature, or its generation / revision moved under a concurrent write"
        ),
        workspace_id=workspace_id,
        lane_id=lane_label,
        lifecycle_migration=migration_payload,
    )


def _migration_payload(migration) -> Optional[dict]:
    """The typed schema-migration audit record, when this write performed one (#13844 R3-F2)."""
    if migration is None:
        return None
    try:
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            lifecycle_migration_payload,
        )

        return lifecycle_migration_payload(migration)
    except Exception:  # noqa: BLE001 - an audit record must never fail the verdict
        return None


def format_unbound_retire_text(verdict: ActiveUnboundLiveZeroRetireVerdict) -> str:
    """Human rendering of the active unbound live-zero terminal retire verdict."""
    lines = [
        f"active_unbound_live_zero_retire: {verdict.state}",
        f"  workspace: {verdict.workspace_id or '-'}",
        f"  lane: {verdict.lane_id or '-'}",
    ]
    if verdict.reason:
        lines.append(f"  reason: {verdict.reason}")
    if verdict.detail:
        lines.append(f"  detail: {verdict.detail}")
    if verdict.expected_live:
        lines.append(f"  expected_live: {', '.join(verdict.expected_live)}")
    if verdict.foreign_names:
        lines.append(f"  foreign_names: {', '.join(verdict.foreign_names)}")
    return "\n".join(lines)


__all__ = (
    "UNBOUND_RETIRE_ALREADY_RETIRED",
    "UNBOUND_RETIRE_BLOCKED",
    "UNBOUND_RETIRE_BRANCH_NOT_LANE_BOUND",
    "UNBOUND_RETIRE_DUPLICATE_INVENTORY",
    "UNBOUND_RETIRE_EXCLUSION_UNAVAILABLE",
    "UNBOUND_RETIRE_EXPECTED_IDENTITY_UNRESOLVED",
    "UNBOUND_RETIRE_FENCE_NOT_DECLARED",
    "UNBOUND_RETIRE_FOREIGN_INVENTORY_PRESENT",
    "UNBOUND_RETIRE_GENERATION_RACE",
    "UNBOUND_RETIRE_HEAD_NOT_INTEGRATED",
    "UNBOUND_RETIRE_LANE_NOT_DECLARED",
    "UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN",
    "UNBOUND_RETIRE_LAUNCH_IN_FLIGHT",
    "UNBOUND_RETIRE_LIFECYCLE_UNREADABLE",
    "UNBOUND_RETIRE_LIVE_PAIR_PRESENT",
    "UNBOUND_RETIRE_NOT_ACTIVE_UNBOUND_STATE",
    "UNBOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED",
    "UNBOUND_RETIRE_RELEASE_IN_FLIGHT",
    "UNBOUND_RETIRE_RETIRED",
    "UNBOUND_RETIRE_STORE_ERROR",
    "UNBOUND_RETIRE_WORKSPACE_UNRESOLVED",
    "ActiveUnboundLiveZeroRetireVerdict",
    "format_unbound_retire_text",
    "run_active_unbound_live_zero_retire",
)
