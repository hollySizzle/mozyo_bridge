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

**The launch race, and how it is closed** (Redmine #14242 review j#85219 F1, design answer
j#85269). The revision fence alone does NOT see a process relaunch: a launch does not mutate the
lifecycle row (``declare_active`` on an existing row is ``CAS_ALREADY_DECLARED`` zero-write,
``declare_lane`` is idempotent), so ``revision`` is unchanged and the terminal write would apply
— recording a lane as ``retired`` while its pair is live. A second inventory read does not help;
the same window simply moves.

The exclusion is therefore taken from the existing #13882 three-boundary protocol rather than a
new durable claim: every managed launch already holds the home's attestation-store lock SHARED
and non-blocking from before its first attestation read through its last actuation, so this
surface takes that same lock EXCLUSIVE and non-blocking for its whole action-time half. A launch
in flight makes this acquire fail (zero-write); this terminalize in flight makes the launch's
acquire fail at admission, before any workspace / tab / agent exists (zero-spawn). A holder crash
releases it at the OS level, so there is no stale claim, TTL, or takeover recovery to get wrong.

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
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_absent_worktree_evidence import (  # noqa: E501
        AbsentWorktreeEvidence,
    )
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
#: The home's attestation-store lock could not be taken EXCLUSIVELY, so a managed launch (or a
#: self-attestation write, or maintenance) is in flight on this home right now (Redmine #14242
#: review j#85219 F1, design answer j#85269). The terminalizer never queues ahead of an in-flight
#: launch: it reports blocked and writes nothing, exactly as attestation maintenance does.
ACTIVE_RETIRE_LAUNCH_IN_FLIGHT = "launch_in_flight"
#: Advisory file locking is unavailable on this platform, so the launch/terminalize exclusion
#: protocol cannot be honored. Proceeding would advertise a guarantee that is not there.
ACTIVE_RETIRE_EXCLUSION_UNAVAILABLE = "exclusion_unavailable"
#: ``--worktree-absent`` (Redmine #15789) was supplied but the absent-checkout evidence did not
#: verify and produced no typed reason of its own. The resolver names its own refusals
#: (``worktree_present`` / ``worktree_not_registered`` / ``worktree_not_prunable`` /
#: ``worktree_branch_mismatch`` / ``worktree_list_unreadable``) and those are reported verbatim —
#: the same discipline as ``latest_generation_blocked_reason`` (#14695 j#93807 F2), so a route's
#: precise diagnosis is never collapsed into a generic token. This is only the fallback.
ACTIVE_RETIRE_ABSENT_WORKTREE_UNPROVEN = "absent_worktree_unproven"


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
    absent_worktree: Optional["AbsentWorktreeEvidence"] = None,
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

    ``absent_worktree`` (Redmine #15789) is the opt-in evidence for the reboot shape whose
    recorded checkout is GONE — the ``terminalize_bound_metadata`` alternative
    ``sublane reboot-audit`` prescribes and that this rail otherwise refuses upstream with
    ``worktree_missing_after_reboot``. When supplied and admissible it substitutes exactly TWO
    facts, both re-derived from git's own surviving worktree administrative entry rather than
    from the checkout: the worktree ↔ branch tie, and the ``wt_`` binding-token family that a
    live disk probe can no longer determine. Everything else is unchanged — head integration is
    still measured from real refs, the binding is still attested byte-for-byte against the
    durable row, and the live-zero read, launch exclusion and CAS are untouched. An inadmissible
    evidence is reported with the resolver's own typed reason and writes nothing.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
        declared_lane_root_identity,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_NO_WORKTREE_ANCHOR,
        REASON_WORKSPACE_UNRESOLVED,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E501
        attest_retire_target,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        herdr_workspace_segment,
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
    # Redmine #15789: the absent-checkout opt-in proves its own premises or the run ends here,
    # before any lane resolution, inventory read or lock acquisition. Its typed reason is
    # reported verbatim so `worktree_present` (use the ordinary rail) is never confused with
    # `worktree_not_registered` (the tie to --branch is genuinely gone).
    if absent_worktree is not None and not absent_worktree.admissible:
        return _blocked(
            absent_worktree.reason or ACTIVE_RETIRE_ABSENT_WORKTREE_UNPROVEN,
            detail=absent_worktree.detail,
            lane_id=lane_label,
        )
    # Lane-unit resolution, identical to the #13754 guarded close / #13845 bound retire.
    try:
        resolved_worktree = Path(worktree).expanduser().resolve()
        # #15789: `herdr_workspace_segment` finds a lane's shared project workspace id by walking
        # a LINKED worktree back to its main checkout. A wiped path is not a linked worktree, so
        # asking it would yield "" and key the lane as unresolvable. The evidence has already
        # proven the path is a worktree OF THIS REPO, so the id it inherits IS the repo's — ask
        # the repo root, which is the same authority the present-checkout case walks back to.
        workspace_id = herdr_workspace_segment(
            repo_root if absent_worktree is not None else resolved_worktree,
            home=getattr(args, "home", None),
        )
    except (OSError, ValueError) as exc:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=f"--worktree does not resolve ({type(exc).__name__})",
            lane_id=lane_label,
        )
    # Redmine #14715: the ``wt_`` / ``dl_`` family comes from the SAME canonical helper the
    # create / adopt writers recorded the binding with — probed on the ``--worktree`` root's
    # own kind. The retired ``resolved_worktree == repo_root`` proxy described the operator's
    # cwd instead, so a normal run from inside a linked worktree (``--repo`` omitted) derived
    # ``dl_`` against a ``wt_`` row and this retire could never attest its own lane.
    if absent_worktree is not None:
        # #15789: the `wt_` family is ASSERTED from git's surviving entry rather than probed on
        # a path that is no longer a directory (`is_git_worktree_root` would answer `dl_` and no
        # `wt_` row could ever attest). Asserting the family is not admitting the binding: the
        # token still has to equal the row's recorded one byte-for-byte at `attest_retire_target`
        # below, and again inside the CAS under the exclusion lock.
        legacy_token = absent_worktree.legacy_token
        metadata_token = absent_worktree.metadata_token
    else:
        identity = declared_lane_root_identity(resolved_worktree, lane_label)
        legacy_token = identity.legacy_token
        metadata_token = identity.metadata_token
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
    #
    # #15789: with no checkout there is no HEAD to read, so the tie is taken from git's
    # surviving administrative entry instead — the resolver has already refused unless that
    # entry exists for this exact path, is prunable, and records `refs/heads/<--branch>`. The
    # requirement is not dropped, only read from the surviving half of the same authority.
    want_branch = (getattr(args, "branch", "") or "").strip()
    actual_branch = (worktree_branch or "").strip()
    if absent_worktree is None and (
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
        home=getattr(args, "home", None),
    )
    if not attested:
        return _blocked(
            attest_reason,
            detail=attest_detail,
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        AttestationStoreLockBusy,
        AttestationStoreLockUnavailable,
        attestation_store_lock,
    )
    from mozyo_bridge.shared.paths import mozyo_bridge_home

    # ---- the launch / terminalize exclusion (Redmine #14242 j#85269) -------------------
    #
    # Boundary 3 of the #13882 three-boundary protocol, reused rather than reinvented. Every
    # managed launch — ordinary create / heal, the v1 replacement binding, quarantine's
    # heal_receiver, and the lane-identity-less bare / scratch / shared-space session starts —
    # holds this home's attestation-store lock SHARED, non-blocking, from before its first
    # attestation read through its last actuation. Taking it EXCLUSIVE here is therefore a
    # reader-writer exclusion over the exact window F1 left open:
    #
    #   - a launch already holding shared -> this acquire fails -> zero-write here;
    #   - this terminalize holding exclusive -> the launch's shared acquire fails at
    #     admission, before any workspace / tab / agent exists -> zero-spawn there.
    #
    # It is held across the lifecycle read, the action-time inventory read, every gate below,
    # and the terminal CAS, so nothing can start between the live-zero measurement and the
    # write. A holder crash releases it at the OS level, so there is no stale claim, no TTL and
    # no takeover recovery to get wrong. The window is home-wide and brief; over-blocking a
    # concurrent unrelated launch for that window is strictly safer than terminalizing a lane
    # whose pair just came back.
    #
    # Residual, inherited from the lock's own contract and NOT claimed to be solved here: a
    # launcher of another vintage does not know this protocol. A durable claim column would not
    # fix that either — an old binary would not read it (j#85269).
    try:
        with attestation_store_lock(
            Path(getattr(args, "home", None) or mozyo_bridge_home()),
            exclusive=True,
            blocking=False,
        ):
            return _terminalize_under_exclusion(
                args,
                repo_root,
                workspace_id=workspace_id,
                legacy_token=legacy_token,
                metadata_token=metadata_token,
                lane_label=lane_label,
                issue=issue,
                journal=journal,
            )
    except AttestationStoreLockBusy as exc:
        return _blocked(
            ACTIVE_RETIRE_LAUNCH_IN_FLIGHT,
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
            ACTIVE_RETIRE_EXCLUSION_UNAVAILABLE,
            detail=(
                f"advisory file locking is unavailable on this platform ({exc}), so the "
                "launch / terminalize exclusion cannot be honored. Terminalizing without it "
                "would advertise a guarantee that is not there"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )


def _terminalize_under_exclusion(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    workspace_id: str,
    legacy_token: str,
    metadata_token: str,
    lane_label: str,
    issue: str,
    journal: str,
):
    """The action-time half, run while HOLDING the exclusive launch-exclusion lock (#14242).

    Everything here is re-read under the lock: the durable lifecycle row (and the revision the
    CAS is fenced on), the live inventory, and every liveness gate. Split into its own function
    so the lock's scope is the function boundary — it is impossible to add a gate that
    accidentally runs outside the exclusion.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_PROVIDER_UNRESOLVED,
        REASON_WORKSPACE_UNRESOLVED,
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
        record = LaneLifecycleStore(home=getattr(args, "home", None)).get(key)
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
    #
    # Redmine #14499: the four fences below (duplicate slot, live expected slot, locator-less
    # expected row, foreign occupant — in that order) now live in the shared
    # :func:`measure_live_zero`, because the #14499 unbound terminalizer must prove exactly the
    # same thing and a second copy of this logic would drift from the reviewed original. The
    # ordering, the refusal strings and the payload fields are unchanged; only the location is.
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
    store = LaneActiveRetireStore(home=getattr(args, "home", None))
    try:
        outcome = store.retire_active_live_zero(
            key,
            # The revision the live-zero read above was measured against. On its own this
            # catches a concurrent lifecycle-row mutation, NOT a process relaunch (review
            # j#85219 F1). The relaunch window is closed by the two halves around it: the
            # caller-held EXCLUSIVE lock serializes the concurrent case, and the launch funnel's
            # retired admission refuses the post-terminal case (review j#85296 F3).
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
    "ACTIVE_RETIRE_LAUNCH_IN_FLIGHT",
    "ACTIVE_RETIRE_EXCLUSION_UNAVAILABLE",
    "ACTIVE_RETIRE_ABSENT_WORKTREE_UNPROVEN",
    "ActiveLiveZeroRetireVerdict",
    "format_active_retire_text",
    "run_active_live_zero_retire",
)
