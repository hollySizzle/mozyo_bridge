"""Active live-zero terminal retire (Redmine #14242).

The fourth retire intent, for the lane shape the other three leave permanently stuck: an
**ACTIVE bound** lifecycle row whose managed pair is already positively gone, on a lane whose
issue is closed and whose head is integrated. Live evidence #14222 j#85208-j#85209 — issue and
children closed, owner close / review / integration / CI green, worktree clean, head an ancestor
of the integration branch, ``sublane list`` reporting ``state=detached`` / ``panes=[]`` — and:

- ``retire --execute`` (#13754) correctly refuses: there is nothing to close, and a zero-close is
  only a retire when the row ALREADY says ``retired``. It returns ``zero_close_unproven`` /
  ``closed: []`` / ``durable_retirement: ""`` forever. That fail-closed behaviour is right; it
  simply offers no convergence path.
- ``--retire-hibernated-bound`` (#13845) correctly refuses with ``not_hibernated_bound_state``:
  its CAS requires ``hibernated`` AND a durable ``process_release == released``.
- ``--migrate-hibernated-legacy`` (#13841) / ``--reconcile-hibernated-live`` (#13842) require an
  EMPTY worktree binding and ``hibernated``.

This surface moves such a row **directly** to the #13689 terminal ``retired`` disposition via one
bounded CAS — metadata only. No process launch / close / resume, no worktree or branch removal.

**Why the bar is higher here than in #13845.** That surface pairs its live-zero read with a
second, independent witness: a durable ``process_release == released`` record proving a release
command actually completed. An ACTIVE row has ``process_release == not_requested`` by
construction — nothing ever requested a release — so **the live-inventory read is the only
liveness authority available**. Everything the aggregate read would paper over therefore has to
be refused explicitly, and the CAS's expected-revision fence has to carry the race:

- an unreadable inventory is not an empty one;
- a duplicate slot means the inventory itself is unsound, so no measurement from it can license
  a terminal write;
- a locator-less expected row is "cannot resolve", never "absent", unless the shared liveness
  contract positively calls it dead;
- a foreign occupant in a targeted unit means a real process is still running there;
- and the revision the zero read was measured against is passed to the CAS.

.. warning::
   **Known open window (Redmine #14242 review j#85219 F1).** That revision fence does NOT close
   the read -> write race for a *process relaunch*: a launch does not mutate the lifecycle row
   (``declare_active`` on an existing row is ``CAS_ALREADY_DECLARED`` zero-write, ``declare_lane``
   is idempotent), so ``revision`` is unchanged and the terminal write applies — recording a lane
   as ``retired`` while its pair is live. A second inventory read would not help; the same window
   simply moves. Closing it requires an exclusion the launch / resume / adopt admission path
   participates in, which is a cross-surface design decision raised as a design consultation on
   #14242. Until it is resolved this surface must not be run against a lane that could be
   relaunched concurrently.

Gate order mirrors #13845 deliberately, so an operator reads one vocabulary across every retire
intent and a reviewer can diff the two surfaces line for line.
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

#: The lane was terminalized: the bounded CAS moved the active bound row to the #13689 terminal
#: ``retired`` disposition. Metadata only — no process was touched.
ACTIVE_RETIRE_RETIRED = "retired"
#: A verified idempotent no-op: the row is already ``retired`` and owns this exact issue, so a
#: duplicate replay succeeds without a second write (re-verified live-zero first).
ACTIVE_RETIRE_ALREADY_RETIRED = "already_retired"
#: Fail-closed: the retire proved nothing and wrote nothing. Never exit 0.
ACTIVE_RETIRE_BLOCKED = "blocked"

#: Blocked reasons. Lane-resolution / attestation reasons are reused from the guarded close
#: (:mod:`...sublane_herdr_retire`) so one vocabulary spans every retire intent.
ACTIVE_RETIRE_LIVE_PAIR_PRESENT = "live_pair_present"
#: A foreign / unexpected provider occupies one of the targeted units. ``expected_live_slots``
#: only aggregates the managed roles, so a unit holding solely an unexpected provider measures
#: zero live; terminalizing then would record the lane permanently gone while a real process
#: still runs in its unit.
ACTIVE_RETIRE_FOREIGN_INVENTORY_PRESENT = "foreign_inventory_present"
#: Two rows in the targeted units carry the SAME canonical slot. A herdr assigned name is unique
#: by construction, so this is a corrupt / ambiguous inventory — and with no release witness to
#: fall back on, no measurement taken from it may license a terminal write.
ACTIVE_RETIRE_DUPLICATE_INVENTORY = "duplicate_inventory"
#: An expected managed slot's row exists but carries NO locator, and the shared liveness contract
#: does not positively call it dead. That is "cannot be resolved", never "absent".
ACTIVE_RETIRE_EXPECTED_IDENTITY_UNRESOLVED = "expected_identity_unresolved"
ACTIVE_RETIRE_HEAD_NOT_INTEGRATED = "head_not_integrated"
#: The literal-ancestor probe failed AND a supplied ``patch_equivalent`` disposition (#14066) did
#: not verify at action time.
ACTIVE_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED = "patch_equivalence_unverified"
#: The caller's ``--worktree`` is not actually checked out on ``--branch`` (mismatch, detached
#: HEAD, or unresolvable), so the clean / integrated evidence describes a different head.
ACTIVE_RETIRE_WORKTREE_BRANCH_MISMATCH = "worktree_branch_mismatch"
ACTIVE_RETIRE_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
#: The bounded CAS refused: the row is not the exact ACTIVE / issue-bound / matching-worktree
#: signature — e.g. a ``hibernated`` row (the #13845 / #13841 / #13842 target), a ``superseded``
#: row, a project-gateway binding, a different issue, or an EMPTY worktree binding.
ACTIVE_RETIRE_NOT_ACTIVE_BOUND_STATE = "not_active_bound_state"
#: The bounded CAS refused: a process release is in flight (``requested`` / ``partial``) or a
#: receiver replacement is unsettled, so the live-zero read may be observing a mid-actuation state.
ACTIVE_RETIRE_RELEASE_IN_FLIGHT = "release_in_flight"
#: The bounded CAS refused: no durable lifecycle owner row.
ACTIVE_RETIRE_LANE_NOT_DECLARED = "lane_not_declared"
#: The bounded CAS refused: a concurrent declare / transition / generation open moved the row —
#: the live-zero measurement was taken against a revision that is no longer current.
ACTIVE_RETIRE_REVISION_RACE = "revision_race"
#: The bounded CAS raised a store error (surfaced, not swallowed).
ACTIVE_RETIRE_STORE_ERROR = "store_error"


@dataclass(frozen=True)
class ActiveLiveZeroRetireVerdict:
    """The fail-closed verdict of the metadata-only active live-zero terminal retire.

    ``ok`` (the command's exit-code authority) is true only for a real terminalization or a
    verified idempotent no-op; every other outcome is :data:`ACTIVE_RETIRE_BLOCKED`.
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
        return self.state in (ACTIVE_RETIRE_RETIRED, ACTIVE_RETIRE_ALREADY_RETIRED)

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
) -> ActiveLiveZeroRetireVerdict:
    return ActiveLiveZeroRetireVerdict(
        state=ACTIVE_RETIRE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_live=expected_live,
        foreign_names=foreign_names,
        lifecycle_migration=lifecycle_migration,
    )


def run_active_live_zero_retire(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    head_integrated: Optional[bool],
    worktree_branch: Optional[str],
    patch_equivalent: Optional["PatchEquivalentResolution"] = None,
):
    """Metadata-only terminalize an ACTIVE bound lane whose pair is proven gone (#14242).

    Returns an :class:`ActiveLiveZeroRetireVerdict`, or ``None`` when the repo is not on the
    herdr backend.

    The command runs this only when its ``may_retire`` preflight already passed (issue closed,
    worktree clean, latest review admissible, callbacks drained, durable record present, target
    identity known), so the "closed + no review / owner / callback debt" axes are established
    upstream and are not restated here. This adds the axes the preflight cannot: the bound
    worktree agreement, the worktree ↔ branch identity, head integration, the positive live-zero
    inventory read, and the active-bound-state CAS.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
        repo_backend_is_herdr,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_NO_WORKTREE_ANCHOR,
        REASON_PROVIDER_NOT_LAUNCHABLE,
        REASON_PROVIDER_UNRESOLVED,
        REASON_WORKSPACE_UNRESOLVED,
        expected_live_slots,
        expected_slot_rows,
        plan_herdr_retire_close,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E501
        attest_retire_target,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        HerdrSessionStartError,
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_directory_lane_token,
        derive_lane_workspace_token,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
        SLOT_STALE,
        classify_named_slot,
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
                "the terminal retire needs the lane's --worktree anchor to resolve the lane "
                "unit and attest its recorded binding; without it no lane identity can be "
                "established"
            ),
            lane_id=lane_label,
        )
    # Lane-unit resolution, identical to the #13754 guarded close / #13845 bound retire.
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
        legacy_token = ""
        metadata_token = derive_directory_lane_token(str(resolved_worktree), lane_label)
    else:
        legacy_token = derive_lane_workspace_token(str(resolved_worktree))
        metadata_token = legacy_token
    if not workspace_id and not legacy_token:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the lane unit's workspace identity cannot be derived from --worktree; the "
                "terminal retire fails closed"
            ),
            lane_id=lane_label,
        )
    # worktree ↔ branch identity: the dirty probe measures --worktree while the integration probe
    # measures --branch, so unless the worktree is ACTUALLY on --branch the two describe
    # different heads and an unrelated branch's evidence could license the retire.
    want_branch = (getattr(args, "branch", "") or "").strip()
    actual_branch = (worktree_branch or "").strip()
    if (
        not want_branch
        or not actual_branch
        or actual_branch == "HEAD"
        or actual_branch != want_branch
    ):
        return _blocked(
            ACTIVE_RETIRE_WORKTREE_BRANCH_MISMATCH,
            detail=(
                f"the --worktree is not checked out on --branch {want_branch or '<none>'} "
                f"(actual head: {actual_branch or '<unresolved/detached>'}); its clean + "
                "integrated evidence cannot be attributed to the lane's branch, so the "
                "terminal retire fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Head integration is an action-time invariant the retire preflight (merge_on_retire=False)
    # does not check.
    if head_integrated is not True:
        if patch_equivalent is None:
            return _blocked(
                ACTIVE_RETIRE_HEAD_NOT_INTEGRATED,
                detail=(
                    "--branch is not a verified ancestor of --integration-branch (unintegrated "
                    "or the ancestry probe could not answer); the lane's head must be "
                    "integrated before a terminal retire"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
        if not patch_equivalent.admissible:
            return _blocked(
                ACTIVE_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED,
                detail=(
                    "--branch is not a literal ancestor of --integration-branch and the supplied "
                    "patch-equivalent integration disposition did not verify at action-time "
                    f"({patch_equivalent.reason}): {patch_equivalent.detail}"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
            )
    # The bound-worktree agreement axis, reusing the #13754 attestation. A diagnostic pre-gate
    # producing precise reasons; the authority is the CAS below, which re-checks under the lock.
    attested, attest_reason, attest_detail = attest_retire_target(
        workspace_id,
        lane_label,
        issue=issue,
        worktree_identity=metadata_token,
    )
    if not attested:
        return _blocked(
            attest_reason,
            detail=attest_detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    from mozyo_bridge.core.state.lane_lifecycle import (
        DISPOSITION_RETIRED,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    try:
        key = LaneLifecycleKey(workspace_id, lane_label)
    except ValueError:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
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
            ACTIVE_RETIRE_LIFECYCLE_UNREADABLE,
            detail=(
                f"the lifecycle store is unreadable ({type(exc).__name__}); the lane's state "
                "cannot be verified, so the terminal retire fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            ACTIVE_RETIRE_LANE_NOT_DECLARED,
            detail=(
                "the lane unit has no durable lifecycle owner row; there is no active state to "
                "terminalize"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The live-zero read. With no release witness available this is the ONLY liveness authority,
    # so it runs BEFORE the idempotent already-retired success too: a persisted ``retired`` does
    # not prove the pair is currently gone.
    try:
        rows = list_herdr_agent_rows(os.environ)
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=f"live herdr inventory unreadable ({exc}); liveness cannot be measured",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        managed_roles = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
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

    if not all(BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable(p) for p in managed_roles):
        return _blocked(
            REASON_PROVIDER_NOT_LAUNCHABLE,
            detail=(
                "the binding assigns a provider that is not mechanically launchable; the lane "
                "unit's managed pair cannot be measured"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    plan = plan_herdr_retire_close(
        rows,
        workspace_id=workspace_id,
        lane_id=lane_label,
        legacy_workspace_id=legacy_token,
        managed_roles=managed_roles,
    )
    candidates = expected_slot_rows(rows, plan, managed_roles=managed_roles)
    # Duplicate check FIRST: a duplicate carrying locators would otherwise report as an ordinary
    # live_pair_present, naming the wrong problem. Keyed on the decoded canonical slot (NOT on
    # role), so a shared unit and its legacy compatibility twin stay two legitimate slots.
    seen_slots: dict[tuple[str, str, str], int] = {}
    for found in candidates:
        seen_slots[found.slot_key] = seen_slots.get(found.slot_key, 0) + 1
    duplicates = sorted(
        f"{role}@{ws}/{lane or '<default>'}"
        for (ws, lane, role), count in seen_slots.items()
        if count > 1
    )
    if duplicates:
        return _blocked(
            ACTIVE_RETIRE_DUPLICATE_INVENTORY,
            detail=(
                "the live inventory carries more than one row for the same canonical managed "
                f"slot ({', '.join(duplicates)}); a herdr assigned name is unique by "
                "construction, so the inventory is ambiguous and no measurement taken from it "
                "can license a terminal write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    live = expected_live_slots(rows, plan, managed_roles=managed_roles)
    if live:
        return _blocked(
            ACTIVE_RETIRE_LIVE_PAIR_PRESENT,
            detail=(
                "the lane's expected managed slots are still live "
                f"({', '.join(live)}); an active lane with a live pair is not this surface's "
                "target — drain it through the ordinary guarded close"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            expected_live=live,
            foreign_names=tuple(plan.foreign_names),
        )
    # A locator-less expected row is "cannot resolve", never "absent", unless the shared liveness
    # contract positively calls it dead.
    unresolved = sorted(
        {
            found.role
            for found in candidates
            if not found.locator and classify_named_slot(found.row) != SLOT_STALE
        }
    )
    if unresolved:
        return _blocked(
            ACTIVE_RETIRE_EXPECTED_IDENTITY_UNRESOLVED,
            detail=(
                f"an expected managed slot ({', '.join(unresolved)}) has a row in the targeted "
                "units but no locator, and the liveness contract does not positively call it "
                "dead; that is absence of proof of liveness, not proof of absence"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            foreign_names=tuple(plan.foreign_names),
        )
    if plan.foreign_names:
        return _blocked(
            ACTIVE_RETIRE_FOREIGN_INVENTORY_PRESENT,
            detail=(
                "a foreign / unexpected provider occupies one of the lane's targeted units "
                f"({', '.join(plan.foreign_names)}); terminalizing would record the lane "
                "permanently gone while a real process is still running in it"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            foreign_names=tuple(plan.foreign_names),
        )
    # Only now is a persisted terminal state a verified success.
    if record.lane_disposition == DISPOSITION_RETIRED and record.issue_id == issue:
        return ActiveLiveZeroRetireVerdict(
            state=ACTIVE_RETIRE_ALREADY_RETIRED,
            detail=(
                "the lane is already terminally retired and its expected managed slots measure "
                "positively absent; duplicate replay is an idempotent no-op with zero writes"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    from mozyo_bridge.core.state.lane_active_retire import LaneActiveRetireStore
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
            ACTIVE_RETIRE_NOT_ACTIVE_BOUND_STATE,
            detail=(
                f"the retire decision anchor is incomplete ({exc}); a terminal retire must name "
                "the durable journal that authorized it"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    store = LaneActiveRetireStore()
    try:
        outcome = store.retire_active_live_zero(
            key,
            # The revision the live-zero read above was measured against. NOTE (review j#85219
            # F1): this catches a concurrent lifecycle-row mutation, NOT a process relaunch —
            # that window is still open, see the module warning.
            expected_revision=record.revision,
            issue_id=issue,
            worktree_identity=metadata_token,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ValueError) as exc:
        return _blocked(
            ACTIVE_RETIRE_STORE_ERROR,
            detail=f"the bounded active retire CAS failed ({type(exc).__name__}: {exc})",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    migration = getattr(store, "last_write_preparation", None)
    migration_payload = _migration_payload(migration)
    if outcome.applied:
        return ActiveLiveZeroRetireVerdict(
            state=ACTIVE_RETIRE_RETIRED,
            detail=(
                "the active bound row was terminalized to retired (metadata only); its "
                "worktree binding, declared pins and generation are preserved, and no process "
                "was launched, closed or resumed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=migration_payload,
        )
    reason_map = {
        CAS_NOT_FOUND: ACTIVE_RETIRE_LANE_NOT_DECLARED,
        CAS_STALE_REVISION: ACTIVE_RETIRE_REVISION_RACE,
        CAS_FORBIDDEN_TRANSITION: ACTIVE_RETIRE_RELEASE_IN_FLIGHT,
    }
    reason = reason_map.get(outcome.reason, ACTIVE_RETIRE_NOT_ACTIVE_BOUND_STATE)
    return _blocked(
        reason,
        detail=(
            f"the bounded active live-zero retire CAS refused ({outcome.reason}); the durable "
            "row is not the exact active / issue-bound / matching-worktree signature, or it "
            "moved under a concurrent write"
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


def format_active_retire_text(verdict: ActiveLiveZeroRetireVerdict) -> str:
    """Human rendering of the active live-zero terminal retire verdict."""
    lines = [
        f"active_live_zero_retire: {verdict.state}",
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
    "ACTIVE_RETIRE_RETIRED",
    "ACTIVE_RETIRE_ALREADY_RETIRED",
    "ACTIVE_RETIRE_BLOCKED",
    "ACTIVE_RETIRE_LIVE_PAIR_PRESENT",
    "ACTIVE_RETIRE_FOREIGN_INVENTORY_PRESENT",
    "ACTIVE_RETIRE_DUPLICATE_INVENTORY",
    "ACTIVE_RETIRE_EXPECTED_IDENTITY_UNRESOLVED",
    "ACTIVE_RETIRE_HEAD_NOT_INTEGRATED",
    "ACTIVE_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED",
    "ACTIVE_RETIRE_WORKTREE_BRANCH_MISMATCH",
    "ACTIVE_RETIRE_LIFECYCLE_UNREADABLE",
    "ACTIVE_RETIRE_NOT_ACTIVE_BOUND_STATE",
    "ACTIVE_RETIRE_RELEASE_IN_FLIGHT",
    "ACTIVE_RETIRE_LANE_NOT_DECLARED",
    "ACTIVE_RETIRE_REVISION_RACE",
    "ACTIVE_RETIRE_STORE_ERROR",
    "ActiveLiveZeroRetireVerdict",
    "format_active_retire_text",
    "run_active_live_zero_retire",
)
