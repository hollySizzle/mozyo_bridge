"""Delegate, run, and inspect a replayable Herdr offline rollout (#14838)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Protocol

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_action import (  # noqa: E501
    OfflineRolloutActionError,
    approval_manifest,
    deterministic_action_id,
    mark_blocked,
    mark_delegated,
    mark_phase_completed,
    mark_phase_started,
    mark_running,
    new_action,
    next_phase,
    parse_approval_pointer,
    public_status,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.infrastructure.offline_rollout_action_store import (  # noqa: E501
    OfflineRolloutActionStore,
    OfflineRolloutActionStoreError,
)


@dataclass(frozen=True)
class PhaseExecutionResult:
    ok: bool
    reason: str = ""
    detail: str = ""
    receipt: Mapping[str, object] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class OfflineRolloutCommandResult:
    ok: bool
    state: str
    reason: str = ""
    detail: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            "ok": self.ok,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            **dict(self.payload),
        }


class OfflineRolloutExecutionPort(Protocol):
    """All host effects. Tests use a fake; production uses the terminal adapter."""

    def verify_owner_approval(
        self,
        *,
        issue: str,
        journal: str,
        manifest: Mapping[str, object],
    ) -> PhaseExecutionResult: ...

    def capture_private_bindings(
        self, *, plan: Mapping[str, object]
    ) -> PhaseExecutionResult: ...

    def prepare_external_runner(
        self,
        *,
        action_id: str,
        action_directory: Path,
        plan: Mapping[str, object],
    ) -> PhaseExecutionResult: ...

    def launch_external_runner(
        self,
        *,
        action_id: str,
        action_directory: Path,
    ) -> PhaseExecutionResult: ...

    def attest_external_runner(self, *, action_id: str) -> PhaseExecutionResult: ...

    def execute_phase(
        self,
        *,
        phase: Mapping[str, object],
        action: Mapping[str, object],
        action_directory: Path,
        replaying: bool,
    ) -> PhaseExecutionResult: ...


def _blocked(reason: str, detail: str = "", **payload) -> OfflineRolloutCommandResult:
    # Raw adapter detail belongs only in the sealed private action record.  It can carry
    # subprocess stderr, paths, or locators and must never cross the public CLI/status
    # boundary.  The fixed typed reason is the public diagnosis.
    return OfflineRolloutCommandResult(
        ok=False, state="blocked", reason=reason, detail="", payload=payload
    )


def adopt_legacy_lanes(
    *, home: Path, targets, replaying: bool
) -> PhaseExecutionResult:
    """Adopt exact pre-v10 hibernated rows inside the already-offline global window."""
    from mozyo_bridge.core.state.lane_epoch_adoption import (
        ADOPTED_LEGACY_EPOCH,
        LaneEpochAdoptionStore,
    )
    from mozyo_bridge.core.state.lane_lifecycle_model import (
        DISPOSITION_HIBERNATED,
        RELEASE_RELEASED,
        DecisionPointer,
        LaneLifecycleKey,
    )
    from mozyo_bridge.core.state.lane_lifecycle_readonly import LaneLifecycleReader

    store = LaneEpochAdoptionStore(home=home)
    reader = LaneLifecycleReader(home=home)
    adopted = []
    for target in targets:
        key = LaneLifecycleKey(target["workspace_id"], target["lane_id"])
        decision = DecisionPointer("redmine", target["issue_id"], target["journal_id"])
        outcome = store.adopt_legacy_epoch(
            key,
            expected_revision=target["expected_revision"],
            issue_id=target["issue_id"],
            decision=decision,
        )
        if not outcome.applied:
            current = reader.get(key) if replaying else None
            replay_exact = current is not None and (
                current.issue_id == target["issue_id"]
                and current.lane_disposition == DISPOSITION_HIBERNATED
                and current.process_release == RELEASE_RELEASED
                and current.lane_epoch == str(ADOPTED_LEGACY_EPOCH)
                and current.revision == target["expected_revision"] + 1
                and current.decision_source == "redmine"
                and current.decision_issue_id == target["issue_id"]
                and current.decision_journal == target["journal_id"]
            )
            if not replay_exact:
                return PhaseExecutionResult(
                    False,
                    reason="legacy_lane_epoch_adoption_failed",
                    detail=f"issue_{target['issue_id']}_{outcome.reason}",
                )
        adopted.append(
            {
                "issue_id": target["issue_id"],
                "workspace_id": target["workspace_id"],
                "lane_id": target["lane_id"],
                "epoch": ADOPTED_LEGACY_EPOCH,
            }
        )
    return PhaseExecutionResult(
        True, receipt={"legacy_lane_epochs_adopted": adopted}
    )


def merge_legacy_recovery_agent_bindings(*, plan, agents) -> PhaseExecutionResult:
    """Add planned-but-currently-absent recovery slots to sealed private bindings."""
    merged = list(agents)
    by_name = {row["assigned_name"]: row for row in merged}
    for recovery in plan.get("legacy_recoveries", ()):
        for recovery_agent in recovery.get("agents", ()):
            name = recovery_agent["assigned_name"]
            expected = {
                "assigned_name": name,
                "workspace_id": recovery["workspace_id"],
                "lane_id": recovery["lane_id"],
                "provider": recovery_agent["provider"],
            }
            existing = by_name.get(name)
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    return PhaseExecutionResult(
                        False,
                        reason="legacy_recovery_agent_identity_drift",
                        detail=name,
                    )
                existing["recovery_issue_id"] = recovery["issue_id"]
                continue
            row = {
                **expected,
                "locator": "",
                "recovery_issue_id": recovery["issue_id"],
            }
            merged.append(row)
            by_name[name] = row
    return PhaseExecutionResult(True, receipt={"agents": merged})


def prepare_store_migration_proofs(
    *, action_directory: Path, store_paths: Mapping[str, Path]
) -> PhaseExecutionResult:
    """Migrate private logical clones and pin each deterministic target content digest."""
    import hashlib
    import sqlite3

    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        migrate_attestation_store,
    )
    from mozyo_bridge.core.state.lane_lifecycle_schema import (
        ensure_lane_lifecycle_schema,
    )

    proof_root = action_directory / "migration-proofs"
    try:
        proof_root.mkdir(mode=0o700, exist_ok=True)
        digests = {}
        for name in ("attestation", "lane_lifecycle"):
            source = store_paths[name]
            target = proof_root / f"{name}.sqlite3"
            target.unlink(missing_ok=True)
            with (
                sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src,
                sqlite3.connect(target) as dst,
            ):
                src.backup(dst)
            target.chmod(0o600)
            if name == "attestation":
                migrate_attestation_store(target)
            else:
                ensure_lane_lifecycle_schema(target)
            digest = hashlib.sha256()
            with sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                for statement in conn.iterdump():
                    digest.update(statement.encode("utf-8"))
                    digest.update(b"\n")
            digests[name] = digest.hexdigest()
    except Exception as exc:  # noqa: BLE001 - private proof failure is a typed zero-live-effect refusal
        return PhaseExecutionResult(
            False, reason="migration_proof_failed", detail=type(exc).__name__
        )
    return PhaseExecutionResult(True, receipt={"migration_post_digests": digests})


def expected_migration_post_digest(action, store_name: str) -> str:
    """Read one proof only from the completed verified-backup phase receipt."""
    receipt = action.get("phase_receipts", {}).get("verified_backup", {})
    digests = receipt.get("migration_post_digests", {})
    value = digests.get(store_name, "") if isinstance(digests, Mapping) else ""
    return value if isinstance(value, str) else ""


def store_phase_authority(
    action, observed, *, store_name: str, phase_name: str, replaying: bool
) -> PhaseExecutionResult:
    """Admit predecessor bytes or a replay candidate with store-specific proof.

    Startup replay is only admitted to the startup migration primitive.  That
    primitive, not this layer, must validate the typed completion receipt before
    returning success.
    """
    planned = action["plan"]["stores"][store_name]
    if observed == planned:
        return PhaseExecutionResult(
            True, receipt={"store_authority": "planned_predecessor"}
        )
    replay_candidate = (
        isinstance(observed, Mapping)
        and observed.get("state") == "recognized"
        and type(observed.get("version")) is int
        and type(planned.get("target_version")) is int
        and observed.get("version") == planned.get("target_version")
        and replaying is True
        and action.get("active_phase") == phase_name
        and "verified_backup" in action.get("completed_phases", ())
    )
    if replay_candidate and store_name == "startup_transaction":
        return PhaseExecutionResult(
            True,
            receipt={
                "store_authority": "startup_completion_check_deferred_to_primitive"
            },
        )
    expected = expected_migration_post_digest(action, store_name)
    if replay_candidate and expected and observed.get("content_digest") == expected:
        return PhaseExecutionResult(
            True, receipt={"store_authority": "exact_post_digest_replay"}
        )
    return PhaseExecutionResult(False, reason=f"{store_name}_plan_drift")


def verify_migrated_store(action, store_name: str, observed) -> PhaseExecutionResult:
    """Require the live target to equal the deterministic clone migrated before effect."""
    planned = action["plan"]["stores"][store_name]
    expected = expected_migration_post_digest(action, store_name)
    if (
        isinstance(observed, Mapping)
        and observed.get("state") == "recognized"
        and type(observed.get("version")) is int
        and type(planned.get("target_version")) is int
        and observed.get("version") == planned.get("target_version")
        and expected
        and observed.get("content_digest") == expected
    ):
        return PhaseExecutionResult(True, receipt={"content_digest": expected})
    return PhaseExecutionResult(False, reason=f"{store_name}_migration_unverified")


def _live_ops(*, home: Path, repo_root: Optional[Path] = None):
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_executor import (  # noqa: E501
        LiveOfflineRolloutExecutionPort,
    )

    return LiveOfflineRolloutExecutionPort(home=home, repo_root=repo_root)


def delegate_offline_rollout(
    *,
    plan: Mapping[str, object],
    plan_digest: str,
    owner_approval: str,
    home: Path,
    repo_root: Path,
    execute: bool,
    ops: Optional[OfflineRolloutExecutionPort] = None,
) -> OfflineRolloutCommandResult:
    """Validate exact approval, persist the private action, and launch the one-shot.

    Without ``execute`` this is a zero-write preflight that returns the canonical
    approval manifest.  The caller cannot supply private paths or locators; the live
    adapter captures them and proves their public identities match the plan.
    """
    try:
        manifest = approval_manifest(plan, plan_digest)
        issue, journal = parse_approval_pointer(owner_approval)
    except OfflineRolloutActionError as exc:
        return _blocked(str(exc))
    port = ops or _live_ops(home=home, repo_root=repo_root)
    approval = port.verify_owner_approval(
        issue=issue, journal=journal, manifest=manifest
    )
    if not approval.ok:
        return _blocked(approval.reason or "owner_approval_unverified", approval.detail)
    if not execute:
        return OfflineRolloutCommandResult(
            ok=True,
            state="planned",
            payload={
                "plan_digest": plan_digest,
                "approval_pointer": owner_approval,
                "approval_manifest": manifest,
                "side_effect_zero": True,
            },
        )

    bindings = port.capture_private_bindings(plan=plan)
    if not bindings.ok:
        return _blocked(bindings.reason or "private_binding_capture_failed", bindings.detail)
    # The plan is a host-global operation authority, so its action identity is deterministic.
    # Concurrent delegates of the same plan therefore contend on one action lock/record instead
    # of launching independent global-stop runners from the same approval.
    token = deterministic_action_id(plan_digest, owner_approval)
    store = OfflineRolloutActionStore(home)
    try:
        action = new_action(
            action_id=token,
            plan=plan,
            plan_digest=plan_digest,
            approval_pointer=owner_approval,
            private_bindings=dict(bindings.receipt),
        )
        store.create(action)
        action_directory = store.action_directory(token)
        prepared = port.prepare_external_runner(
            action_id=token, action_directory=action_directory, plan=plan
        )
        with store.locked(token) as directory:
            action = store.load_locked(directory)
            if not prepared.ok:
                action = mark_blocked(
                    action,
                    prepared.reason or "runner_prepare_failed",
                    prepared.detail,
                )
                store.save_locked(directory, action)
                return _blocked(
                    action["last_reason"], action["last_detail"], **public_status(action)
                )
            private = dict(action["private_bindings"])
            private["runner"] = dict(prepared.receipt)
            action = dict(action)
            action["private_bindings"] = private
            action = mark_delegated(action)
            store.save_locked(directory, action)
        launched = port.launch_external_runner(
            action_id=token, action_directory=action_directory
        )
        if not launched.ok:
            with store.locked(token) as directory:
                action = store.load_locked(directory)
                action = mark_blocked(
                    action,
                    launched.reason or "runner_launch_failed",
                    launched.detail,
                )
                store.save_locked(directory, action)
            return _blocked(
                action["last_reason"], action["last_detail"], **public_status(action)
            )
        action = store.load(token)
        return OfflineRolloutCommandResult(
            ok=True,
            state="delegated",
            payload={**public_status(action), "runner_receipt": dict(launched.receipt)},
        )
    except (OfflineRolloutActionError, OfflineRolloutActionStoreError) as exc:
        return _blocked(str(exc))


def run_offline_rollout_action(
    *,
    action_id: str,
    home: Path,
    ops: Optional[OfflineRolloutExecutionPort] = None,
) -> OfflineRolloutCommandResult:
    """Run or resume one action under its exclusive lock, forward only."""
    store = OfflineRolloutActionStore(home)
    port = ops or _live_ops(home=home)
    try:
        attested = port.attest_external_runner(action_id=action_id)
        if not attested.ok:
            return _blocked(
                attested.reason or "external_runner_unattested", attested.detail
            )
        with store.locked(action_id) as directory:
            action = store.load_locked(directory)
            action = mark_running(action)
            store.save_locked(directory, action)
            while True:
                phase = next_phase(action)
                if phase is None:
                    return OfflineRolloutCommandResult(
                        ok=True, state="completed", payload=public_status(action)
                    )
                replaying = action.get("active_phase") == phase["phase"]
                action = mark_phase_started(action, str(phase["phase"]))
                store.save_locked(directory, action)
                result = port.execute_phase(
                    phase=phase,
                    action=action,
                    action_directory=directory,
                    replaying=replaying,
                )
                if not result.ok:
                    action = mark_blocked(
                        action,
                        result.reason or "phase_failed",
                        result.detail,
                    )
                    store.save_locked(directory, action)
                    return _blocked(
                        action["last_reason"], action["last_detail"], **public_status(action)
                    )
                action = mark_phase_completed(
                    action, str(phase["phase"]), dict(result.receipt)
                )
                store.save_locked(directory, action)
    except (OfflineRolloutActionError, OfflineRolloutActionStoreError) as exc:
        return _blocked(str(exc))
    except Exception as exc:  # noqa: BLE001 - never leave an untyped runner crash
        try:
            with store.locked(action_id) as directory:
                action = store.load_locked(directory)
                action = mark_blocked(
                    action, "runner_exception", type(exc).__name__
                )
                store.save_locked(directory, action)
                return _blocked(
                    action["last_reason"], action["last_detail"], **public_status(action)
                )
        except Exception:  # noqa: BLE001 - original typed result is the best available fact
            return _blocked("runner_exception", type(exc).__name__)


def status_offline_rollout_action(
    *, action_id: str, home: Path
) -> OfflineRolloutCommandResult:
    try:
        action = OfflineRolloutActionStore(home).load(action_id)
    except OfflineRolloutActionStoreError as exc:
        return _blocked(str(exc))
    status = public_status(action)
    return OfflineRolloutCommandResult(ok=True, state=str(status["state"]), payload=status)


__all__ = (
    "OfflineRolloutCommandResult",
    "OfflineRolloutExecutionPort",
    "PhaseExecutionResult",
    "delegate_offline_rollout",
    "run_offline_rollout_action",
    "status_offline_rollout_action",
)
