"""`mozyo-bridge sublane repair-pins` — hibernated bound declared-pin repair (Redmine #13879).

The public high-level surface the measured #13846 j#79915 shape needs: a **hibernated /
released BOUND** lifecycle row (rev4 / gen1, ``worktree_identity`` present) whose
``declared_slots`` snapshot is **empty**, while the lane's exact managed pair is observed
**live**. ``sublane recover-pair`` (#13847) requires the declared pins and so fails
``hibernated_record_missing_pins`` forever; #13809's backfill is active-row only; #13841 /
#13842 require an EMPTY worktree binding; #13845 targets the live-zero case and terminalizes.
The lane therefore has no way to start a recovery.

This surface repairs **only** the empty pin snapshot, from the exact live pair, so that
recover-pair's re-preflight can proceed. It **composes already-reviewed pieces** rather than
inventing a parallel semantics:

1. **action-time pair observation** — :func:`...sublane_hibernated_live_reconcile.observe_pair`
   gathers the same content-free per-slot facts the #13842 reconcile gathers (raw candidate
   multiplicity BEFORE liveness, slot liveness, startup self-attestation generation-bound to the
   live locator, runtime receiver-state, and a content-free pending-composer observation), and
   the pure :func:`...domain.sublane_hibernated_live_reconcile.decide_pair_reconcile` renders the
   verdict. It is GREEN only when every expected slot is present, unique, live, idle /
   turn-ended, composer-settled, and generation-bound attested, with no foreign provider at the
   lane's position and a readable inventory. Nothing about name or cache is inferred (Redmine
   #13879 acceptance 1): identity comes from the decoded assigned name joined to the slot's own
   startup self-attestation, and liveness from live ``agent get`` / ``read_pane`` reads that a
   dead pane cannot satisfy.
2. **bounded repair CAS** — :meth:`...lane_pin_repair.LanePinRepairStore.repair_hibernated_bound_pins`
   writes the typed pins under the exact ``(revision, generation)`` guard and the literal
   hibernated / released / bound / pins-empty signature, re-checking the worktree token under the
   row lock. The observation above is a **diagnostic**; the CAS is the authority.

**Metadata only** (acceptance 3): this module has no close / launch / resume / send path at all.
The lane stays ``hibernated``, its ``process_release`` / ``lane_generation`` /
``worktree_identity`` / ``replacement_*`` / ``reconcile_phase`` are preserved, and the worktree
and branch are untouched. Acting on the repaired pins remains ``recover-pair``'s job — this
surface deliberately does **not** weaken that command's declared-pins precondition (Redmine
#13847 owns it); it repairs the metadata the precondition reads.

Default is preflight only; ``--execute`` performs the guarded CAS. Replay is byte-equal-only
idempotent (acceptance 4): a re-run that observes the same pair reports ``already_repaired``,
while a row already pinned to a **different** set is refused zero-write rather than overwritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.application.cli_common import add_repo_option

# -- repair verdict vocabulary -----------------------------------------------

#: The empty pin snapshot was filled from the exact verified live pair; recover-pair may now
#: re-run its preflight.
REPAIR_REPAIRED = "repaired"
#: A verified idempotent no-op: the row is already pinned to EXACTLY the observed live pair
#: (byte-equal), so the repair has already happened. Zero write.
REPAIR_ALREADY = "already_repaired"
#: A green PREFLIGHT (no ``--execute``): every axis holds and the repair would apply, but
#: nothing was written. Distinct from :data:`REPAIR_REPAIRED` so a preflight payload never
#: claims a durable write it did not perform.
REPAIR_REPAIRABLE = "repairable"
#: Fail-closed: the repair proved nothing and wrote nothing. Never exit 0.
REPAIR_BLOCKED = "blocked"

#: The durable row is not the hibernated + released + settled + issue-owner + BOUND signature
#: (a different disposition / binding / issue, an unproven / in-flight release, a receiver
#: replacement in flight, an EMPTY worktree binding, or a binding naming a different worktree).
REPAIR_NOT_REPAIRABLE_STATE = "not_repairable_state"
#: No expected managed slot is live: there is no pair to pin from. A repair never fabricates
#: pins from a name or a cache (acceptance 1).
REPAIR_LIVE_PAIR_ABSENT = "live_pair_absent"
#: The row already carries a NON-EMPTY pin snapshot that differs from the observed live pair —
#: a recycled / foreign generation. Never overwritten (acceptance 4).
REPAIR_PINS_DIVERGENT = "declared_pins_divergent"
REPAIR_REVISION_RACE = "revision_race"
#: The row was re-incarnated since the pair was observed; its empty snapshot belongs to a
#: different generation.
REPAIR_GENERATION_RACE = "generation_race"
REPAIR_RELEASE_NOT_PROVEN = "release_not_proven"
REPAIR_LIFECYCLE_UNREADABLE = "lifecycle_unreadable"
REPAIR_LANE_NOT_DECLARED = "lane_not_declared"
REPAIR_STORE_ERROR = "store_error"


@dataclass(frozen=True)
class PinRepairVerdict:
    """The fail-closed verdict of the hibernated bound pin repair (Redmine #13879).

    ``ok`` (the command's exit-code authority) is true only for a real repair, a verified
    idempotent no-op, or a green preflight — every other outcome is :data:`REPAIR_BLOCKED` with
    the ``reason`` that could not be established, never a success.

    ``repaired`` reports whether a durable pin write ACTUALLY happened, so a caller / renderer
    never claims a write on a preflight or an idempotent replay. It is the write-side authority:
    the three ``ok`` states are deliberately distinct because only one of them wrote.
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    executed: bool = False
    #: Did this run durably write the pin snapshot? False for a preflight, a blocked verdict,
    #: and a byte-equal idempotent replay (which writes nothing).
    repaired: bool = False
    #: The observed pins this repair would write / did write, as content-free payloads.
    pins: tuple[dict, ...] = ()
    #: The shared-store schema migration this repair's write gate performed, if any (Redmine
    #: #13844 R3-F2): ``None`` when nothing was forward-migrated.
    lifecycle_migration: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.state in (REPAIR_REPAIRED, REPAIR_ALREADY, REPAIR_REPAIRABLE)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "executed": self.executed,
            "repaired": self.repaired,
            "pins": [dict(p) for p in self.pins],
            "lifecycle_migration": self.lifecycle_migration,
        }


def _blocked(
    reason: str,
    *,
    detail: str = "",
    workspace_id: str = "",
    lane_id: str = "",
    pins: tuple[dict, ...] = (),
    lifecycle_migration: Optional[dict] = None,
) -> PinRepairVerdict:
    return PinRepairVerdict(
        state=REPAIR_BLOCKED,
        reason=reason,
        detail=detail,
        workspace_id=workspace_id,
        lane_id=lane_id,
        pins=pins,
        lifecycle_migration=lifecycle_migration,
    )


def run_hibernated_pin_repair(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    ops: Optional[Any] = None,
) -> Optional[PinRepairVerdict]:
    """Repair a hibernated bound lane's empty declared pins from its live pair (#13879).

    Returns a :class:`PinRepairVerdict`, or ``None`` when the repo is not on the herdr backend.
    ``ops`` is the injected :class:`...sublane_hibernated_live_reconcile.ReconcileOps` (tests
    drive fakes; the default is the live adapter). No process is launched, closed, resumed, or
    sent to on any path.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        BINDING_KIND_ISSUE,
        CAS_ALREADY_DECLARED,
        CAS_FORBIDDEN_TRANSITION,
        CAS_GENERATION_MISMATCH,
        CAS_NOT_FOUND,
        CAS_STALE_REVISION,
        CAS_UNEXPECTED_STATE,
        DISPOSITION_HIBERNATED,
        RELEASE_RELEASED,
        DecisionPointer,
        DecisionPointerError,
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
        ProcessGenerationPin,
        ProcessPinError,
        encode_declared_slots,
        norm,
        replacement_settled,
        validate_declared_slots,
    )
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        lifecycle_migration_payload,
    )
    from mozyo_bridge.core.state.lane_pin_repair import LanePinRepairStore
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        is_git_worktree_root,
        repo_backend_is_herdr,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_INVENTORY_UNREADABLE,
        REASON_NO_WORKTREE_ANCHOR,
        REASON_PROVIDER_NOT_LAUNCHABLE,
        REASON_PROVIDER_UNRESOLVED,
        REASON_WORKSPACE_UNRESOLVED,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
        default_ghost_policy,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile import (  # noqa: E501
        LiveReconcileOps,
        observe_pair,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_hibernated_live_reconcile import (  # noqa: E501
        STATE_BLOCKED,
        decide_pair_reconcile,
    )
    # The canonical declared-pin slot vocabulary, from the ONE boundary that owns it
    # (Redmine #13920). Producer and consumer must spell a slot the same way or the consumer
    # silently reads the row as pin-less — the trap this repair was built to clear, and which
    # `domain.sublane_lifecycle`'s same-NAMED constants (valued `codex` / `claude`) still set
    # for anyone who copies an import from a sibling module.
    from mozyo_bridge.core.state.lane_pin_role import (
        PIN_ROLE_GATEWAY,
        PIN_ROLE_WORKER,
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
    lane_label = (getattr(args, "lane", "") or "").strip()
    issue = (getattr(args, "issue", "") or "").strip()
    journal = (getattr(args, "journal", "") or "").strip()
    execute = bool(getattr(args, "execute", False))
    if not worktree:
        return _blocked(
            REASON_NO_WORKTREE_ANCHOR,
            detail=(
                "the repair needs the lane's --worktree anchor to resolve the lane unit and "
                "its canonical binding token; without it no lane identity can be established"
            ),
            lane_id=lane_label,
        )
    # Resolve the lane unit from the --worktree anchor exactly as the #13842 reconcile and the
    # #13845 bound retire do: the worktree inherits the project workspace identity (#13377) that
    # scopes the live slots, and its stable path token (``wt_`` / ``dl_``) is the canonical
    # worktree binding the row must ALREADY carry (the bound signature).
    try:
        resolved_worktree = Path(worktree).expanduser().resolve()
        workspace_id = herdr_workspace_segment(resolved_worktree)
    except (OSError, ValueError) as exc:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=f"--worktree does not resolve ({type(exc).__name__})",
            lane_id=lane_label,
        )
    # Redmine #13933 j#81046 Decision 1: the token family is decided by whether the target
    # root IS a git worktree, probed on that root -- not by ``resolved == repo_root``, which
    # only holds when repo_root is the coordinator workspace root and otherwise flips the
    # family on the caller's cwd (the #13846 j#81024 identity split).
    collapsed_to_root = not is_git_worktree_root(resolved_worktree)
    if collapsed_to_root:
        metadata_token = derive_directory_lane_token(str(resolved_worktree), lane_label)
    else:
        metadata_token = derive_lane_workspace_token(str(resolved_worktree))
    if not workspace_id:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            detail=(
                "the --worktree root carries no herdr project workspace anchor; the lane's "
                "exact live pair cannot be scoped (point --repo / --worktree at the lane's "
                "own checkout)"
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
            REPAIR_LIFECYCLE_UNREADABLE,
            detail=f"the lifecycle store is unreadable ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record is None:
        return _blocked(
            REPAIR_LANE_NOT_DECLARED,
            detail="the lane unit has no durable lifecycle owner row; nothing to repair",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    try:
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except DecisionPointerError:
        return _blocked(
            REPAIR_LIFECYCLE_UNREADABLE,
            detail=(
                "no re-readable Redmine decision anchor (--issue / --journal) to record the "
                "repair with; the repair fails closed"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # The repairable base signature: hibernated + durably released + settled replacement + issue
    # binding + owns this exact issue + no project scope + **NON-EMPTY worktree binding equal to
    # the resolved token** (the bound signature — the inverse of #13841's / #13842's EMPTY one,
    # so no row is ever a target of both). ``declared_slots`` emptiness is deliberately NOT
    # pre-checked: a byte-equal replay legitimately finds a NON-empty snapshot, and the CAS is
    # the authority that distinguishes it from a divergent one. This is a diagnostic pre-gate
    # producing precise reasons; the CAS re-checks the same axes under the row lock.
    if (
        record.lane_disposition != DISPOSITION_HIBERNATED
        or norm(record.binding_kind) != BINDING_KIND_ISSUE
        or (record.issue_id or "").strip() != issue
        or record.project_scope
        or norm(record.worktree_identity) != metadata_token
        or not norm(record.worktree_identity)
    ):
        return _blocked(
            REPAIR_NOT_REPAIRABLE_STATE,
            detail=(
                "the durable row is not the hibernated + owns-issue + BOUND (non-empty "
                "worktree binding equal to the --worktree token) signature the repair targets "
                "(an active row backfills through #13809; an EMPTY-binding legacy row is "
                "#13841 / #13842's; a retired row is terminal)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if record.process_release != RELEASE_RELEASED or not replacement_settled(
        record.replacement_state
    ):
        return _blocked(
            REPAIR_RELEASE_NOT_PROVEN,
            detail=(
                "the row's process release is unproven / in flight, or a receiver replacement "
                "is in flight: an actuator may be mutating this lane's slots right now, so the "
                "observed pair is not a settled generation to pin"
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
    live_ops = (
        ops
        if ops is not None
        else LiveReconcileOps(repo_root=repo_root, ghost_policy=default_ghost_policy())
    )
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
    if verdict.absent:
        # A positive absence: the inventory is readable and no expected slot is live. There is no
        # pair to pin from, and the repair NEVER fabricates pins from a name or a cache
        # (acceptance 1). A live-zero bound row terminalizes through #13845 instead.
        return _blocked(
            REPAIR_LIVE_PAIR_ABSENT,
            detail=(
                "no expected managed slot is live: there is no action-time attested pair to "
                "repair the pins from (a live-zero bound row terminalizes via "
                "--retire-hibernated-bound (#13845); pins are never inferred from a name)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    if verdict.state == STATE_BLOCKED:
        return _blocked(
            verdict.reason,
            detail=(
                "the exact live pair is not unique / live / idle / settled / attested; the "
                "repair fails closed zero-write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    # GREEN: the exact live pair is present, unique, live, idle / turn-ended, composer-settled,
    # and generation-bound attested at distinct locators, with no foreign provider at the lane's
    # position. Pin it from that evidence alone (role / provider / assigned_name / locator +
    # the verified attestation's observed_at); ``runtime_revision`` stays empty because herdr's
    # generation discriminant is the locator and no runtime-version surface exists to observe
    # (#13809 / #13810 R4-F1 — it is never fabricated).
    try:
        pins = [
            ProcessGenerationPin(
                role=s.role,
                provider=s.provider,
                assigned_name=s.assigned_name,
                locator=s.locator,
                attested_at=s.attested_at,
            )
            for s in observation.slots
        ]
    except ProcessPinError as exc:
        return _blocked(
            REPAIR_STORE_ERROR,
            detail=f"the observed live pair could not be pinned ({exc}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
        )
    pin_payloads = tuple(p.as_payload() for p in pins)
    # The EXACT bytes the CAS will compare the row's snapshot against, computed through the SAME
    # validate -> encode path the CAS uses (Redmine #13879 review j#80547 F1). A preflight that
    # compared any other way could report "byte-equal" where the authority reports "divergent",
    # which is the very split this fixes.
    try:
        encoded_pins = encode_declared_slots(validate_declared_slots(tuple(pins)))
    except ProcessPinError as exc:
        return _blocked(
            REPAIR_STORE_ERROR,
            detail=f"the observed live pair could not be encoded ({exc}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
        )
    if not execute:
        # A preflight must predict what --execute would do, on EVERY axis the CAS decides —
        # including the ``declared_slots`` axis the base signature above deliberately leaves
        # unchecked (a byte-equal replay legitimately finds a NON-empty snapshot, so emptiness
        # is not a precondition). Reporting a bare "repairable" here regardless of the persisted
        # snapshot made the public default disagree with --execute: a divergent row previewed as
        # exit 0 while the CAS refused it (review j#80547 F1), and a byte-equal row previewed as
        # "would repair" when the CAS writes nothing. The comparison stays a DIAGNOSTIC: it is
        # confined to this preflight branch, so --execute always reaches the CAS, which re-reads
        # the row under its lock and remains the sole authority (a stale preview must never
        # refuse a row the authority would accept).
        persisted = norm(record.declared_slots)
        if persisted and persisted != encoded_pins:
            return _blocked(
                REPAIR_PINS_DIVERGENT,
                detail=(
                    "the row already carries a DIFFERENT pin snapshot than the observed live "
                    "pair (a recycled / foreign generation); --execute would refuse it "
                    "zero-write and never overwrite it"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
                pins=pin_payloads,
            )
        if persisted == encoded_pins:
            # Non-empty and byte-equal (``pins`` always carries both slots, so an equal snapshot
            # is never the empty one): the repair has already happened.
            return PinRepairVerdict(
                state=REPAIR_ALREADY,
                detail=(
                    "the row is already pinned to EXACTLY the observed live pair (byte-equal); "
                    "the repair has already happened and --execute would write nothing"
                ),
                workspace_id=workspace_id,
                lane_id=lane_label,
                executed=False,
                repaired=False,
                pins=pin_payloads,
            )
        return PinRepairVerdict(
            state=REPAIR_REPAIRABLE,
            detail=(
                "preflight only (no --execute): the exact live pair is verified and the row is "
                "the hibernated bound pins-empty signature; re-run with --execute to write the "
                "pins, then re-run recover-pair's preflight"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            executed=False,
            repaired=False,
            pins=pin_payloads,
        )
    # Redmine #14065 Phase 2 item 4: re-observe the pair at action time, immediately
    # before the CAS, through the SAME authority (the dim-ghost render gate rides in
    # observe_composer). The repair rail otherwise had NO composer re-read between the
    # decision and the CAS. Any drift — a slot that no longer settles green (e.g. a slot
    # that was a dim ghost at decision time but now renders real input), a vanished /
    # non-unique pair, or a pin set different from the decision-time one — fails the
    # repair closed zero-write, mirroring the hibernate rail's pre-close re-verification.
    try:
        recheck = observe_pair(
            live_ops.agent_rows(),
            live_ops,
            workspace_id=workspace_id,
            lane_id=lane_label,
            managed_pairs=managed_pairs,
        )
    except HerdrSessionStartError as exc:
        return _blocked(
            REASON_INVENTORY_UNREADABLE,
            detail=(
                f"action-time re-observation could not read the live inventory ({exc}); "
                f"the repair fails closed zero-write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
        )
    recheck_verdict = decide_pair_reconcile(recheck)
    if recheck_verdict.absent or recheck_verdict.state == STATE_BLOCKED:
        return _blocked(
            recheck_verdict.reason,
            detail=(
                "action-time re-observation changed: the live pair no longer settles green "
                "just before the write (a slot drifted — e.g. a ghost composer is now real "
                "unsent input); the repair fails closed zero-write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
        )
    try:
        recheck_pins = encode_declared_slots(
            validate_declared_slots(
                tuple(
                    ProcessGenerationPin(
                        role=s.role,
                        provider=s.provider,
                        assigned_name=s.assigned_name,
                        locator=s.locator,
                        attested_at=s.attested_at,
                    )
                    for s in recheck.slots
                )
            )
        )
    except ProcessPinError as exc:
        return _blocked(
            REPAIR_STORE_ERROR,
            detail=f"the re-observed live pair could not be encoded ({exc}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
        )
    if recheck_pins != encoded_pins:
        return _blocked(
            REPAIR_PINS_DIVERGENT,
            detail=(
                "action-time re-observation changed: the live pair drifted from the "
                "decision-time pair just before the write; the repair fails closed zero-write"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
        )
    repair_store = LanePinRepairStore()
    try:
        outcome = repair_store.repair_hibernated_bound_pins(
            key,
            expected_revision=record.revision,
            expected_generation=record.lane_generation,
            issue_id=issue,
            worktree_identity=metadata_token,
            declared_slots=pins,
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
            REPAIR_STORE_ERROR,
            detail=f"the pin repair CAS raised ({type(exc).__name__}); fail closed",
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
            lifecycle_migration=lifecycle_migration_payload(
                repair_store.last_write_preparation
            ),
        )
    migration = lifecycle_migration_payload(repair_store.last_write_preparation)
    if not outcome.applied:
        reason_map = {
            CAS_NOT_FOUND: REPAIR_LANE_NOT_DECLARED,
            CAS_STALE_REVISION: REPAIR_REVISION_RACE,
            CAS_GENERATION_MISMATCH: REPAIR_GENERATION_RACE,
            CAS_UNEXPECTED_STATE: REPAIR_NOT_REPAIRABLE_STATE,
            CAS_FORBIDDEN_TRANSITION: REPAIR_RELEASE_NOT_PROVEN,
            CAS_ALREADY_DECLARED: REPAIR_PINS_DIVERGENT,
        }
        return _blocked(
            reason_map.get(outcome.reason, REPAIR_NOT_REPAIRABLE_STATE),
            detail=(
                f"the pin repair CAS refused ({outcome.reason}); the row moved since the repair "
                "read it, is not the exact hibernated / released / bound / pins-empty signature, "
                "or already carries a DIFFERENT pin snapshot (never overwritten) — zero lane-row "
                "write (any shared-store schema migration is reported separately)"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            pins=pin_payloads,
            lifecycle_migration=migration,
        )
    # Applied. A byte-equal replay is an idempotent no-op that does NOT bump the revision, so the
    # unchanged revision is what distinguishes it from a real fill (acceptance 4).
    if outcome.revision == record.revision:
        return PinRepairVerdict(
            state=REPAIR_ALREADY,
            detail=(
                "the row is already pinned to EXACTLY the observed live pair (byte-equal); the "
                "repair is an idempotent no-op and wrote nothing"
            ),
            workspace_id=workspace_id,
            lane_id=lane_label,
            executed=True,
            repaired=False,
            pins=pin_payloads,
            lifecycle_migration=migration,
        )
    return PinRepairVerdict(
        state=REPAIR_REPAIRED,
        detail=(
            "the empty declared-pin snapshot was filled from the exact verified live pair under "
            "the exact revision + generation guard; the lane stays hibernated (metadata only) "
            "and recover-pair's preflight may now proceed"
        ),
        workspace_id=workspace_id,
        lane_id=lane_label,
        executed=True,
        repaired=True,
        pins=pin_payloads,
        lifecycle_migration=migration,
    )


def format_pin_repair_text(result: PinRepairVerdict) -> str:
    """Render the pin repair verdict (Redmine #13879), leading with the verdict."""
    unit = result.workspace_id or "<unresolved>"
    if result.lane_id:
        unit = f"{unit} lane={result.lane_id}"
    header = f"sublane repair-pins: {result.state}"
    if result.reason:
        header += f" ({result.reason})"
    lines = [f"{header} workspace={unit}"]
    if result.detail:
        lines.append(f"  {result.detail}")
    for pin in result.pins:
        lines.append(
            f"  - {pin.get('role')}: provider={pin.get('provider')} "
            f"name={pin.get('assigned_name')} locator={pin.get('locator')}"
        )
    if result.repaired:
        lines.append("  - durable write: declared pins repaired (metadata only)")
    if result.lifecycle_migration:
        mig = result.lifecycle_migration
        lines.append(
            "  - shared lifecycle store forward-migrated "
            f"v{mig['from_version']} -> v{mig['to_version']} "
            f"(peer lanes at read-fail-closed risk: {mig['peer_active_lanes'] or 'none'})"
        )
    if not result.ok:
        if result.lifecycle_migration:
            # Redmine #13844 R4-F2: the lane row was NOT repaired, but the write gate already
            # forward-migrated the shared-store SCHEMA — "nothing was written" would deny it.
            lines.append(
                "  -> fail-closed: pins NOT repaired; no lane-row write (the shared-store "
                "schema migration above is a separate side effect)"
            )
        else:
            lines.append("  -> fail-closed: pins NOT repaired; nothing was written")
    elif result.state == REPAIR_REPAIRABLE:
        lines.append("  (preflight only; re-run with --execute to repair the pins)")
    elif result.state == REPAIR_ALREADY and not result.executed:
        # Do NOT advertise --execute here: the row is already byte-equal, so --execute would
        # write nothing. Telling the operator to re-run it would misdescribe the outcome
        # (review j#80547 F1 — the preflight must predict what --execute actually does).
        lines.append("  (preflight only; nothing to repair — the pins are already exact)")
    return "\n".join(lines)


def cmd_sublane_repair_pins(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    result = run_hibernated_pin_repair(args, repo_root)
    if result is None:
        print(
            "sublane repair-pins: the repo is not on the herdr terminal backend; "
            "nothing to repair",
            file=sys.stderr,
        )
        return 1
    if bool(getattr(args, "json", False)):
        print(json.dumps(result.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_pin_repair_text(result), file=sys.stdout)
    return 0 if result.ok else 1


def register_sublane_repair_pins_parser(sublane_sub: Any) -> None:
    """Register ``sublane repair-pins`` outside the at-ceiling core CLI module."""
    parser = sublane_sub.add_parser(
        "repair-pins",
        help=(
            "Redmine #13879: repair the EMPTY declared-pin snapshot of a hibernated / released "
            "BOUND lane from its exact live, idle, attested pair, so recover-pair (#13847) can "
            "re-run its preflight. Metadata only — nothing is launched, closed, resumed, or "
            "sent. Default is preflight only; --execute performs the guarded CAS."
        ),
    )
    parser.add_argument("--issue", required=True, help="Redmine issue the hibernated lane owns")
    parser.add_argument("--lane", required=True, help="Hibernated lane label to repair")
    parser.add_argument(
        "--journal",
        required=True,
        help="Redmine journal authorizing this repair (the decision anchor recorded on the row)",
    )
    parser.add_argument(
        "--worktree",
        required=True,
        help="The lane's worktree checkout; resolves the lane unit and the canonical binding "
        "token the row's non-empty worktree_identity must equal",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the guarded pin-repair CAS (default: preflight only)",
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.set_defaults(func=cmd_sublane_repair_pins)


__all__ = (
    "REPAIR_REPAIRED",
    "REPAIR_ALREADY",
    "REPAIR_REPAIRABLE",
    "REPAIR_BLOCKED",
    "REPAIR_NOT_REPAIRABLE_STATE",
    "REPAIR_LIVE_PAIR_ABSENT",
    "REPAIR_PINS_DIVERGENT",
    "REPAIR_REVISION_RACE",
    "REPAIR_GENERATION_RACE",
    "REPAIR_RELEASE_NOT_PROVEN",
    "REPAIR_LIFECYCLE_UNREADABLE",
    "REPAIR_LANE_NOT_DECLARED",
    "REPAIR_STORE_ERROR",
    "PinRepairVerdict",
    "cmd_sublane_repair_pins",
    "format_pin_repair_text",
    "register_sublane_repair_pins_parser",
    "run_hibernated_pin_repair",
)
