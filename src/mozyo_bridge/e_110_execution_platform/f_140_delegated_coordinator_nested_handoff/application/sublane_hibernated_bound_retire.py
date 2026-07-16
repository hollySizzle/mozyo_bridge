"""Public high-level hibernated bound-lane terminal retire (Redmine #13845).

The action-time verification + orchestration half of the metadata-only terminalization, kept
apart from the destructive ``retire --execute`` guarded close
(:mod:`...sublane_retire_actuation`) it must never be confused with: this path launches /
closes / resumes **no** process, removes no worktree, deletes no branch. It moves a
hibernated / released **bound** lane directly to the #13689 terminal ``retired`` disposition
through the bounded :class:`...lane_bound_retire.LaneBoundRetireStore` CAS.

The problem (Redmine #13845, live evidence #13810 j#79416): a hibernated / released **bound**
owner row — the coordinator hibernated the lane, its process release completed durably, its
issue is closed, its worktree clean + integrated, its live pair gone — but whose
``worktree_identity`` is **non-empty** (a #13754 / #13809 / #13810-bound row) is terminalized
by no existing path:

- ``retire --execute`` attests the binding, then plans a close that finds nothing to close;
  a zero-close is only a retire when the durable row ALREADY says ``retired``, so it returns
  ``zero_close_unproven`` / ``closed: []`` / ``durable_retirement: ""`` forever;
- ``--migrate-hibernated-legacy`` (#13841) requires an EMPTY worktree binding — a bound row is
  refused there;
- ``--reconcile-hibernated-live`` (#13842) requires an empty binding AND targets the opposite
  liveness case (an exact pair observed live).

Action-time verification, every axis fail-closed (nothing is written unless ALL hold):

- **exact issue / lane / workspace** — the lane unit is keyed ``(workspace_id, lane_label)``
  and the bounded CAS requires the row to own **this exact** issue (a different issue / lane /
  a non-``issue`` binding is refused zero-write).
- **bound worktree agreement** — the #13754 :func:`attest_retire_target` proves the caller's
  ``--worktree`` resolves to the lane's **recorded** canonical worktree binding. An empty
  binding (:data:`REASON_WORKTREE_BINDING_UNVERIFIED`) routes to #13841 rather than passing
  here; a mismatch (:data:`REASON_WORKTREE_BINDING_MISMATCH`) means the ``--worktree`` belongs
  to a different lane. Re-checked under the row lock by the CAS — this is the diagnostic half.
- **canonical worktree ↔ actual branch** — the ``--worktree``'s real checked-out branch must
  equal ``--branch``, so the clean + integrated evidence describes the lane's real head
  (:data:`BOUND_RETIRE_WORKTREE_BRANCH_MISMATCH`, the #13841 review j#79150 F1 invariant).
- **origin ancestry** — the caller's ``--branch`` is a read-only ancestor of the
  ``--integration-branch``; an unknown / non-ancestor probe fails closed
  (:data:`BOUND_RETIRE_HEAD_NOT_INTEGRATED`). The clean-worktree / issue-closed / latest-review
  / callback-drain invariants are the command's ``may_retire`` preflight (which gates whether
  this path runs at all), so a dirty / open / unapproved lane never reaches here.
- **live-zero** — the live herdr inventory is read (read-only) and MUST show **every** expected
  managed slot absent for the lane unit. A live slot is :data:`BOUND_RETIRE_LIVE_PAIR_PRESENT`
  — a durable ``released`` record is *not* liveness (``lane_lifecycle`` boundary), so the
  durable proof is paired with this live-zero read. An unreadable inventory is NOT an empty
  one (:data:`REASON_INVENTORY_UNREADABLE`).
- **unoccupied** — the targeted units must additionally carry **no foreign / unexpected
  occupant** (:data:`BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT`, review j#80115 F1). ``expected
  live slots absent`` and ``the unit is empty`` are DIFFERENT facts: ``expected_live_slots``
  measures only the *managed* roles, so a unit occupied solely by an unexpected provider
  measures zero live and would otherwise terminalize the row while that process is still
  running. The lane unit is not provably quiescent while anything occupies it.
- **released bound state** — the bounded CAS additionally requires ``hibernated`` + durable
  ``released`` + a **non-empty, matching** ``worktree_identity`` + settled replacement, guarded
  on the row's exact revision (a revision race loses :data:`BOUND_RETIRE_REVISION_RACE`).

A duplicate replay is idempotent: an already-``retired`` row owning this issue is a verified
no-op success (:data:`BOUND_RETIRE_ALREADY_RETIRED`), reported only AFTER the live-zero read,
so a completed terminalization never reports success while a pair was relaunched under it
(the #13841 review j#79150 F2 invariant).

The row's declared pins, worktree identity, generation, and release / replacement axes are
**preserved** — only the disposition + decision anchor move. This surface does not erode
#13841 (empty-binding migration), #13842 (live reconcile), or the ordinary #13754 active
retire: each refuses this shape and this refuses each of theirs.

Boundary (Redmine #13845): no process launch / close / resume, no worktree / branch removal,
no raw Herdr / tmux, no origin/main, no production / tag / publish. Synthetic regression only.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# -- terminal retire verdict vocabulary --------------------------------------

#: The lane was terminalized: the bounded CAS moved the hibernated / released bound row to the
#: #13689 terminal ``retired`` disposition. Metadata only — no process was touched.
BOUND_RETIRE_RETIRED = "retired"
#: A verified idempotent no-op: the row is already ``retired`` and owns this exact issue, so a
#: duplicate replay succeeds without a second write (re-verified live-zero first).
BOUND_RETIRE_ALREADY_RETIRED = "already_retired"
#: Fail-closed: the retire proved nothing and wrote nothing. Never exit 0.
BOUND_RETIRE_BLOCKED = "blocked"

#: Blocked reasons (bound-retire-specific). The lane-resolution / attestation reasons are
#: reused from the guarded close (:mod:`...sublane_herdr_retire`) so an operator reads one
#: vocabulary across every retire intent.
BOUND_RETIRE_LIVE_PAIR_PRESENT = "live_pair_present"
#: A foreign / unexpected provider occupies one of the targeted units (review j#80115 F1).
#: Distinct from :data:`BOUND_RETIRE_LIVE_PAIR_PRESENT`, which names an *expected managed*
#: slot: ``expected_live_slots`` only aggregates the managed roles, so a unit holding solely
#: an unexpected provider measures zero live. Terminalizing then would record the lane as
#: permanently gone while a real process is still running in its unit — the acceptance's
#: "foreign inventory is zero-write" axis.
BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT = "foreign_inventory_present"
BOUND_RETIRE_HEAD_NOT_INTEGRATED = "head_not_integrated"
#: The caller's ``--worktree`` is not actually checked out on the caller's ``--branch`` (a
#: mismatch, a detached HEAD, or an unresolvable checkout). The clean / integrated evidence
#: would then describe a branch other than the worktree's real head, so the identity is
#: refused zero-write (the #13841 review j#79150 F1 invariant, carried over unweakened).
BOUND_RETIRE_WORKTREE_BRANCH_MISMATCH = "worktree_branch_mismatch"
BOUND_RETIRE_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
#: The bounded CAS refused: the row is not the exact hibernated / released / bound-worktree
#: signature (a different issue / disposition / binding, or an EMPTY worktree binding — the
#: #13841 legacy shape, which terminalizes through ``--migrate-hibernated-legacy``).
BOUND_RETIRE_NOT_BOUND_STATE = "not_hibernated_bound_state"
#: The bounded CAS refused: the process release is not durably ``released`` (never requested,
#: or still in flight), or a receiver replacement is in flight.
BOUND_RETIRE_RELEASE_NOT_PROVEN = "release_not_proven"
#: The bounded CAS refused: the row is not present (no durable lifecycle owner row).
BOUND_RETIRE_LANE_NOT_DECLARED = "lane_not_declared"
#: The bounded CAS refused: a concurrent declare / transition / generation open moved the row.
BOUND_RETIRE_REVISION_RACE = "revision_race"
#: The bounded CAS raised a store error (surfaced, not swallowed).
BOUND_RETIRE_STORE_ERROR = "store_error"


@dataclass(frozen=True)
class HibernatedBoundRetireVerdict:
    """The fail-closed verdict of the metadata-only hibernated bound terminal retire.

    ``ok`` (the command's exit-code authority) is true only for a real terminalization or a
    verified idempotent no-op — every other outcome is :data:`BOUND_RETIRE_BLOCKED` with the
    ``reason`` that could not be established, never a success.
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    expected_live: tuple[str, ...] = ()
    #: The foreign / unexpected occupants observed in the targeted units (review j#80115 F1).
    #: Surfaced next to ``expected_live`` because the two are different measurements and the
    #: blocked verdict must say WHICH one refused: an operator reading ``expected_live: []``
    #: alone cannot tell a quiescent unit from one occupied by an unexpected provider.
    foreign_names: tuple[str, ...] = ()
    #: The shared-store schema migration this retire's write gate performed, if any (Redmine
    #: #13844 R3-F2): the typed audit record (from/to version, backup, peer-reader risk) so the
    #: migration is legible in JSON/text, not only the pre-migration stderr advisory.
    lifecycle_migration: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.state in (BOUND_RETIRE_RETIRED, BOUND_RETIRE_ALREADY_RETIRED)

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
) -> HibernatedBoundRetireVerdict:
    return HibernatedBoundRetireVerdict(
        state=BOUND_RETIRE_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_live=expected_live,
        foreign_names=foreign_names,
        lifecycle_migration=lifecycle_migration,
    )


def run_hibernated_bound_retire(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    head_integrated: Optional[bool],
    worktree_branch: Optional[str],
):
    """Metadata-only terminalize a hibernated / released BOUND lane (Redmine #13845).

    Returns a :class:`HibernatedBoundRetireVerdict`, or ``None`` when the repo is not on the
    herdr backend (this is a herdr lane-lifecycle operation, like the guarded close).

    ``head_integrated`` is the command's read-only ancestry probe result (``--branch`` reachable
    from ``--integration-branch``); ``None`` / ``False`` fails closed. ``worktree_branch`` is the
    ``--worktree``'s ACTUAL checked-out branch (``git rev-parse --abbrev-ref HEAD``, ``None``
    when unresolvable / detached): it must equal ``--branch``, so the clean + integrated
    evidence describes the worktree's real head and not an unrelated branch name.

    The command runs this only when its ``may_retire`` preflight already passed (issue closed,
    worktree clean, latest review admissible, callbacks drained, target identity known), so
    those axes are established upstream. This adds the axes the preflight cannot: the bound
    worktree agreement, the worktree ↔ branch identity, head integration, a live-inventory zero
    read, and the released-bound-state CAS.
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
    # Resolve the lane unit from the --worktree anchor exactly as the #13754 guarded close
    # does: the worktree inherits the project workspace identity (#13377), a legacy pre-#13377
    # lane keeps its path-derived ``wt_`` token, and a non-git (#13392) lane collapsed to the
    # workspace root is keyed on the lane-scoped ``dl_`` token. The metadata token is the one
    # the recorded canonical worktree binding is attested against, so it must be derived the
    # same way the create site recorded it.
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
                "the --worktree root carries no herdr workspace anchor and no lane token; the "
                "lane unit cannot be identified (point --repo / --worktree at the lane's own "
                "checkout)"
            ),
            lane_id=lane_label,
        )
    # Worktree ↔ branch identity (the #13841 review j#79150 F1 invariant): the clean probe
    # measures the --worktree while the integration probe measures --branch, so unless the
    # --worktree is ACTUALLY checked out on --branch the two describe different heads — an
    # unrelated branch's clean / integrated evidence could then license the retire. Require the
    # worktree's real branch to equal --branch; a mismatch, a detached HEAD ("HEAD"), an
    # unresolvable checkout (None), or an empty --branch fails closed zero-write.
    want_branch = (getattr(args, "branch", "") or "").strip()
    actual_branch = (worktree_branch or "").strip()
    if (
        not want_branch
        or not actual_branch
        or actual_branch == "HEAD"
        or actual_branch != want_branch
    ):
        return _blocked(
            BOUND_RETIRE_WORKTREE_BRANCH_MISMATCH,
            detail=(
                f"the --worktree is not checked out on --branch {want_branch or '<none>'} "
                f"(actual head: {actual_branch or '<unresolved/detached>'}); its clean + "
                "integrated evidence cannot be attributed to the lane's branch, so the "
                "terminal retire fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Head-integration is an action-time invariant (Redmine #13845 origin-ancestry acceptance):
    # the retire preflight runs with merge_on_retire=False, so it does NOT check integration —
    # this probe does. Unknown (probe could not answer) or non-ancestor fails closed.
    if head_integrated is not True:
        return _blocked(
            BOUND_RETIRE_HEAD_NOT_INTEGRATED,
            detail=(
                "--branch is not a verified ancestor of --integration-branch (unintegrated or "
                "the ancestry probe could not answer); the lane's head must be integrated "
                "before a terminal retire"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The bound-worktree agreement axis (Redmine #13845), reusing the #13754 attestation rather
    # than restating it: prove the requested (issue, lane, --worktree) name ONE durable lane
    # unit against the fail-closed #13689 lifecycle store. This is what makes the surface a
    # BOUND retire — an empty binding fails closed here (worktree_binding_unverified) and is
    # #13841's target, not this one's. It is a diagnostic pre-gate producing precise reasons;
    # the authority is the CAS below, which re-checks the same token under the row lock.
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
    # Read the durable row for the CAS revision + the idempotency check, but do NOT decide the
    # already-retired success yet: that is gated on the live-inventory zero read below (the
    # #13841 review j#79150 F2 invariant), so a persisted ``retired`` never reports success
    # while a pair was relaunched under it.
    try:
        record = LaneLifecycleStore().get(key)
    except (LaneLifecycleError, OSError) as exc:
        return _blocked(
            BOUND_RETIRE_LIFECYCLE_UNREADABLE,
            detail=(
                f"the lifecycle store is unreadable ({type(exc).__name__}); the lane's state "
                "cannot be verified, so the terminal retire fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            BOUND_RETIRE_LANE_NOT_DECLARED,
            detail=(
                "the lane unit has no durable lifecycle owner row; there is no bound state to "
                "terminalize"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Every expected managed slot must be ABSENT (Redmine #13845 live-zero acceptance). A
    # durable ``released`` record is NOT liveness (``lane_lifecycle`` boundary), so the
    # released-state CAS below is paired with this live-inventory zero read. An unreadable
    # inventory is NOT an empty one — folding it to "nothing live" is the #13682 R1-F1 /
    # #13754 anti-pattern. This runs BEFORE the idempotent already-retired success (the #13841
    # review j#79150 F2 invariant): a persisted ``retired`` disposition does not prove the pair
    # is currently gone, so even a duplicate replay is only a success once the live inventory
    # is readable AND measures every expected slot absent.
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
    live = expected_live_slots(rows, plan, managed_roles=managed_roles)
    if live:
        return _blocked(
            BOUND_RETIRE_LIVE_PAIR_PRESENT,
            detail=(
                f"expected managed slot(s) are still live ({', '.join(live)}); the lane unit "
                "is not live-zero, so this metadata-only terminal retire fails closed "
                "(release / hibernate the pair first, or reconcile it)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            expected_live=live,
            foreign_names=plan.foreign_names,
        )
    # No foreign / unexpected occupant may remain either (review j#80115 F1, the acceptance's
    # "foreign inventory is zero-write" axis). ``expected_live_slots`` above aggregates ONLY the
    # managed roles, so a unit occupied solely by an unexpected provider measures zero live and
    # would sail past that check — recording the lane permanently ``retired`` while a real
    # process still runs in its unit. "No expected slot is live" and "the unit is quiescent" are
    # different facts, and only the second licenses a terminal disposition. Refused zero-write
    # here rather than coerced: this surface closes nothing, so it cannot make the unit empty,
    # and the occupant is a foreign process it must never touch. The #13842 reconcile gates the
    # same class through its own ``foreign_at_position`` observation.
    if plan.foreign_names:
        return _blocked(
            BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT,
            detail=(
                "foreign / unexpected occupant(s) are live in the lane unit "
                f"({', '.join(plan.foreign_names)}); the unit is not quiescent, so a terminal "
                "retire would record the lane gone while a real process still runs there. "
                "This surface never closes a foreign agent — resolve the occupant first"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            expected_live=live,
            foreign_names=plan.foreign_names,
        )
    if record.lane_disposition == DISPOSITION_RETIRED and (
        record.issue_id or ""
    ).strip() == issue:
        # Idempotent duplicate replay: the row already reached the terminal disposition and
        # owns this exact issue. A verified no-op success — reported only AFTER the live-zero
        # read above confirmed every expected slot is absent (the #13841 review j#79150 F2
        # invariant), so a persisted ``retired`` never reports success while a pair was
        # relaunched under it.
        return HibernatedBoundRetireVerdict(
            state=BOUND_RETIRE_ALREADY_RETIRED,
            detail=(
                "the lane is already durably retired and every expected managed slot is "
                "absent; the terminal retire is an idempotent no-op"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The released-bound-state CAS: hibernated + durable released + a non-empty worktree
    # binding equal to the attested token + this exact issue + settled replacement, guarded on
    # the row's exact revision. Every other shape is refused zero-write. The row's declared
    # pins / worktree identity / generation are preserved (Redmine #13845 acceptance).
    from mozyo_bridge.core.state.lane_bound_retire import LaneBoundRetireStore
    from mozyo_bridge.core.state.lane_lifecycle import (
        CAS_FORBIDDEN_TRANSITION,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        DecisionPointer,
        DecisionPointerError,
    )

    try:
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except DecisionPointerError:
        return _blocked(
            BOUND_RETIRE_LIFECYCLE_UNREADABLE,
            detail=(
                "no re-readable Redmine decision anchor (--issue / --journal) to record the "
                "retirement with; the terminal retire fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # Redmine #13844 R3-F2: retain the store so the migration record its write gate produced is
    # surfaced in the structured verdict (JSON/text), not only the pre-migration stderr advisory.
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        lifecycle_migration_payload,
    )

    retire_store = LaneBoundRetireStore()
    try:
        outcome = retire_store.retire_released_hibernated_bound(
            key,
            expected_revision=record.revision,
            issue_id=issue,
            worktree_identity=metadata_token,
            decision=decision,
        )
    except (LaneLifecycleError, DecisionPointerError, ValueError, OSError) as exc:
        return _blocked(
            BOUND_RETIRE_STORE_ERROR,
            detail=f"the bound terminal retire CAS raised ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=lifecycle_migration_payload(
                retire_store.last_write_preparation
            ),
        )
    migration = lifecycle_migration_payload(retire_store.last_write_preparation)
    if outcome.applied:
        return HibernatedBoundRetireVerdict(
            state=BOUND_RETIRE_RETIRED,
            detail=(
                "hibernated / released bound lane terminalized to retired (metadata only; no "
                "process launched / closed / resumed; declared pins + worktree binding "
                "preserved)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            lifecycle_migration=migration,
        )
    # Map the CAS refusal to a diagnostic reason (each is a distinct fail-closed shape).
    reason_map = {
        CAS_NOT_FOUND: BOUND_RETIRE_LANE_NOT_DECLARED,
        CAS_STALE_REVISION: BOUND_RETIRE_REVISION_RACE,
        CAS_FORBIDDEN_TRANSITION: BOUND_RETIRE_RELEASE_NOT_PROVEN,
    }
    reason = reason_map.get(outcome.reason, BOUND_RETIRE_NOT_BOUND_STATE)
    return _blocked(
        reason,
        detail=(
            f"the bound terminal retire CAS refused ({outcome.reason}); the row is not the "
            "exact hibernated / released / bound-worktree signature (an EMPTY binding is the "
            "#13841 legacy shape — use --migrate-hibernated-legacy)"
        ),
        workspace_id=workspace_id,
        lane_id=lane_label,
        lifecycle_migration=migration,
    )


def format_bound_retire_text(result: HibernatedBoundRetireVerdict) -> str:
    """Render the bound terminal retire verdict (Redmine #13845), leading with the verdict."""
    unit = result.workspace_id or "<unresolved>"
    if result.lane_id:
        unit = f"{unit} lane={result.lane_id}"
    header = f"  hibernated bound terminal retire: {result.state}"
    if result.reason:
        header += f" ({result.reason})"
    lines = [f"{header} workspace={unit}"]
    if result.detail:
        lines.append(f"    {result.detail}")
    if not result.ok:
        # Redmine #13844 R4-F2: "nothing was written" is scoped to the lane-lifecycle ROW CAS —
        # a forward schema migration is a SEPARATE side effect the write gate may already have
        # committed, so a blocked run that migrated must not claim nothing happened.
        if result.lifecycle_migration:
            lines.append(
                "    -> fail-closed: the lane-lifecycle row was NOT retired (the row CAS did "
                "not apply); the shared-store SCHEMA was already forward-migrated by the write "
                "gate — see below"
            )
        else:
            lines.append(
                "    -> fail-closed: lane NOT retired; no lane-row write and no schema migration"
            )
    if result.expected_live:
        lines.append(
            "    live expected managed slots: " + ", ".join(result.expected_live)
        )
    if result.foreign_names:
        # Named separately from the managed slots (review j#80115 F1): an operator must be able
        # to tell "no expected slot is live" from "the unit is empty".
        lines.append(
            "    foreign / unexpected occupants in the lane unit (never closed here): "
            + ", ".join(result.foreign_names)
        )
    if result.lifecycle_migration:
        mig = result.lifecycle_migration
        lines.append(
            "    - shared lifecycle store forward-migrated "
            f"v{mig['from_version']} -> v{mig['to_version']} "
            f"(peer lanes at read-fail-closed risk: {mig['peer_active_lanes'] or 'none'})"
        )
    return "\n".join(lines)


__all__ = (
    "BOUND_RETIRE_RETIRED",
    "BOUND_RETIRE_ALREADY_RETIRED",
    "BOUND_RETIRE_BLOCKED",
    "BOUND_RETIRE_LIVE_PAIR_PRESENT",
    "BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT",
    "BOUND_RETIRE_HEAD_NOT_INTEGRATED",
    "BOUND_RETIRE_WORKTREE_BRANCH_MISMATCH",
    "BOUND_RETIRE_LIFECYCLE_UNREADABLE",
    "BOUND_RETIRE_NOT_BOUND_STATE",
    "BOUND_RETIRE_RELEASE_NOT_PROVEN",
    "BOUND_RETIRE_LANE_NOT_DECLARED",
    "BOUND_RETIRE_REVISION_RACE",
    "BOUND_RETIRE_STORE_ERROR",
    "HibernatedBoundRetireVerdict",
    "run_hibernated_bound_retire",
    "format_bound_retire_text",
)
