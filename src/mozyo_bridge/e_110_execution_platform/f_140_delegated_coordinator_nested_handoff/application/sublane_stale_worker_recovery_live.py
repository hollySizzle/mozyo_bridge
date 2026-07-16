"""Live adapters for the stale standard-sublane worker recovery (Redmine #13806 tranche D R1-F1).

The public ``sublane recover-stale`` command is only useful if it actually observes the live
inventory and drives the real close/launch/attest + redispatch — a fail-closed staged seam
would leave the j#79435 product gap open (review j#79528 F1). This module wires the pure use
case (:mod:`...sublane_stale_worker_recovery`) to the real runtime by REUSING the #13763
receiver-replacement live ops (:class:`...sublane_quarantine.LiveSublaneQuarantineOps` — the
reviewer's cited precedent) for the exact-generation close / relaunch / fresh attestation, the
herdr inventory + slot-liveness predicate for the preflight classification, and the herdr
delivery ledger + transport for the exactly-once gate redispatch.

Consistent with the tranche boundary (j#79485: no dogfood actuation during the request), the
adapters are exercised by isolated tests with a fake herdr runner / isolated home — they never
require a real managed worker to run. The *destructive* effects still fail closed: an
unreadable inventory is never degraded to a positive absence, a same-name recycle is never
closed, and a redispatch never blind-resends (the durable gate ledger is the idempotency
oracle).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
from mozyo_bridge.core.state.lane_lifecycle import ReleasePin, ReleasePinError
from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    identity_observation_for,
)
from mozyo_bridge.core.state.replacement_transaction import (
    ContinuationPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    probe_worktree_resolved,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    CloseReceiverResult,
    LiveSublaneQuarantineOps,
    QuarantineRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
    RecoveryRequest,
    StaleWorkerRecoveryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
    CLOSE_DONE,
    CLOSE_ERROR,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    RecoveryObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (  # noqa: E501
    HerdrWorkerDispatchOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_BUSY,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

_STATUS_KEYS = ("agent_status", "status", "state")


def _recorded_after(recorded_at: object, boundary: str) -> bool:
    """Is ``recorded_at`` strictly after ``boundary``? (parsed, fail-closed)

    Both are ISO-8601 timestamps. An unparseable / empty either side returns ``False`` — a
    ledger record whose ordering against the post-launch boundary cannot be established is
    never treated as the redispatch (Redmine #13806 R2-F3).
    """
    from datetime import datetime

    left = _norm(recorded_at)
    right = _norm(boundary)
    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) > datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False


def _row_runtime_state(row: Mapping[str, object]) -> str:
    for key in _STATUS_KEYS:
        if key in row:
            return map_agent_status(row.get(key))
    return ""


def _quarantine_request(request: RecoveryRequest) -> QuarantineRequest:
    """Adapt a :class:`RecoveryRequest` to the #13763 quarantine request the live ops take."""
    return QuarantineRequest(
        issue=_norm(request.issue),
        lane=_norm_lane(request.lane),
        journal=_norm(request.journal),
        role=_norm(request.role),
        assigned_name=_norm(request.assigned_name),
        locator=_norm(request.locator),
        action_generation=_norm(request.action_id),
        approval_observed_at="",
        approved_revision=-1,
    )


@dataclass
class LiveRecoveryActuatorPort:
    """The live exact-generation close / launch / attest port (reuses the #13763 live ops).

    Constructed per recovery with the approved :class:`RecoveryRequest`, so the actuator's
    per-participant steps (``observe_old_slot`` / ``observe_preservation`` /
    ``close_exact_generation`` / ``launch_action_bound`` / ``verify_attestation``) resolve
    against the exact pinned worker. The three destructive effects delegate to
    :class:`LiveSublaneQuarantineOps`; the two observations read the live herdr inventory
    directly, never degrading an unreadable / ambiguous inventory to a positive absence.
    """

    repo_root: Path
    request: RecoveryRequest
    store: ReplacementTransactionStore
    key: ReplacementTransactionKey
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS
    #: The lane-lifecycle store home the close boundary re-verifies the pinned ``(revision,
    #: generation)`` against (Redmine #13806 R1-F2). ``None`` = the real state home; tests inject
    #: an isolated one.
    lifecycle_home: Optional[Path] = None
    #: The startup-attestation store home the action-binding verify reads (Redmine #13806 R2-F2).
    #: ``None`` = the real state home; tests inject an isolated one.
    attestation_home: Optional[Path] = None

    def _q(self) -> LiveSublaneQuarantineOps:
        return LiveSublaneQuarantineOps(
            repo_root=self.repo_root, env=self.env, runner=self.runner,
            timeout=self.timeout,
        )

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _exact_and_matches(self, pin: ParticipantPin):
        rows = self._rows()
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(pin.assigned_name)
        ]
        exact = [r for r in matches if _agent_locator(r) == _norm(pin.old_locator)]
        return rows, matches, exact

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        try:
            _rows, matches, exact = self._exact_and_matches(pin)
        except Exception:  # noqa: BLE001 - an unreadable inventory is never a positive absence
            return OLD_SLOT_AMBIGUOUS
        if exact:
            # Live at the exact pinned locator; ambiguous only if the name is not unique.
            return OLD_SLOT_PRESENT if len(exact) == 1 and len(matches) == 1 else OLD_SLOT_AMBIGUOUS
        # The exact old generation is gone. A same-name row at a DIFFERENT locator is a recycle
        # (a new agent took the name) — never close it; otherwise a positive absence.
        return OLD_SLOT_RECYCLED if matches else OLD_SLOT_ABSENT

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        try:
            _rows, matches, exact = self._exact_and_matches(pin)
        except Exception:  # noqa: BLE001 - unreadable => fail closed (identity not matched)
            return PreservationObservation(identity_matches=False)
        if len(exact) != 1 or len(matches) != 1:
            return PreservationObservation(identity_matches=False, detail="ambiguous_or_absent")
        row = exact[0]
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        # Re-verify the pinned lane lifecycle (revision, generation) against the LIVE lane
        # lifecycle at the close boundary (Redmine #13806 R1-F2): an unreadable / absent /
        # moved lifecycle fails the identity fence (a missing observed value defaults empty, so
        # a pin that carries a lifecycle generation the live store no longer matches blocks).
        live_rev, live_gen = self._live_lifecycle_generation(pin)
        identity_ok = bool(
            decoded.ok
            and decoded.identity is not None
            and identity_observation_for(
                pin,
                observed_lane_id=decoded.identity.lane_id,
                observed_role=decoded.identity.role,
                # herdr's assigned-name identity carries no separate provider (its `role` is the
                # provider); provider is not a herdr-observable discriminator, so the observable
                # lane / role / assigned-name / locator carry the identity fence. Pass the pin's
                # own provider so it is not spuriously treated as a divergence.
                observed_provider=pin.provider,
                observed_assigned_name=_norm(row.get(AGENT_KEY_NAME)),
                observed_locator=_agent_locator(row),
                observed_lane_revision=live_rev,
                observed_lane_generation=live_gen,
            )
        )
        # For a worker recovery only running_process / identity_mismatch block (the recovery
        # preservation policy byte-preserves a dirty worktree). attestation_fresh is set True so
        # the (unused-by-recovery-policy) attestation fence never spuriously fires.
        return PreservationObservation(
            running_process=_row_runtime_state(row) == RUNTIME_BUSY,
            identity_matches=identity_ok,
            attestation_fresh=True,
        )

    def _live_lifecycle_generation(self, pin: ParticipantPin) -> tuple[str, str]:
        """The live lane lifecycle ``(revision, generation)`` as strings, or ``("", "")``.

        Fail-closed: an unreadable / absent lane lifecycle row yields empty strings, so a pin
        that carries a lane ``(revision, generation)`` no longer backed by the live store fails
        the identity fence (never a silent pass).
        """
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleError,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            record = LaneLifecycleStore(home=self.lifecycle_home).get(
                LaneLifecycleKey(workspace_id, _norm_lane(pin.lane_id))
            )
        except (LaneLifecycleError, ValueError, OSError):
            return "", ""
        if record is None:
            return "", ""
        return str(record.revision), str(record.lane_generation)

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        try:
            release = ReleasePin(
                role=pin.role, assigned_name=pin.assigned_name, locator=pin.old_locator
            )
        except ReleasePinError:
            return CLOSE_ERROR
        result: CloseReceiverResult = self._q().close_receiver(
            _quarantine_request(self.request), release
        )
        # A positively-absent old slot is treated as "already closed" by the tranche B step
        # only via observe_old_slot; here a close request that finds the exact slot gone
        # (old_absent) is not an error — the caller advances via bounded recovery.
        return CLOSE_DONE if (result.closed or result.old_absent) else CLOSE_ERROR

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        """Relaunch the fresh worker carrying the exact ``action_id`` (Redmine #13806 R2-F2).

        Constructs the herdr lane actuator with ``replacement_action_id=action_id`` so the
        fresh process's startup self-attestation records it — the durable action binding
        :meth:`verify_attestation` re-checks. Not the plain ``heal_receiver`` (which drops the
        action id): a fresh relaunch that does not carry the exact replacement action can never
        be verified as THIS recovery's worker.
        """
        try:
            HerdrSublaneActuatorOps(
                repo_root=self.repo_root, lane_label=_norm(self.request.lane),
                issue=_norm(self.request.issue), journal=_norm(self.request.journal),
                env=self.env, runner=self.runner, timeout=self.timeout,
                replacement_action_id=_norm(action_id),
            ).heal_lane_column(str(self.repo_root))
        except Exception:  # noqa: BLE001 - a fixed launch failure, no body persisted
            return LAUNCH_ERROR
        return LAUNCH_DONE

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        """Verify the fresh worker is fresh AND bound to THIS action (Redmine #13806 R2-F2).

        Fresh identity / locator / post-transaction freshness (the #13763 join) is necessary
        but not sufficient — the fresh process's startup self-attestation must also record the
        exact replacement ``action_id`` (option B, Design Answer j#79556):

        - no fresh attestation yet (still booting / not fresh) -> :data:`ATTEST_PENDING`;
        - fresh, but the ``replacement_action_id`` is missing / a different action ->
          :data:`ATTEST_MISMATCH` (a fresh slot NOT launched by this recovery is never adopted);
        - fresh AND exact action match -> :data:`ATTEST_BOUND`.
        """
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        rec = self.store.get(self.key)
        fresh_after = rec.created_at if rec is not None else ""
        verification = self._q().verify_fresh_receiver(
            _quarantine_request(self.request), fresh_after=fresh_after
        )
        if not verification.ok:
            return ATTEST_PENDING  # fresh attestation not present / not fresh yet
        try:
            record = HerdrIdentityAttestationStore(home=self.attestation_home).read(
                _norm(self.request.assigned_name)
            )
        except Exception:  # noqa: BLE001 - unreadable attestation fails closed (not bound)
            return ATTEST_PENDING
        if record is None:
            return ATTEST_PENDING
        if _norm(record.replacement_action_id) != _norm(action_id):
            # A fresh, attested slot whose startup did NOT bind this exact action — a different
            # (or no) replacement authority launched it. Never complete the participant on it.
            return ATTEST_MISMATCH
        return ATTEST_BOUND


@dataclass
class LiveStaleWorkerRecoveryOps:
    """Live observe + exactly-once gate redispatch (:class:`StaleWorkerRecoveryOps`).

    ``observe_target`` classifies the exact pinned worker from the live herdr inventory +
    slot-liveness predicate (the read-only preflight). The redispatch resends the ORIGINAL
    gate to the fresh worker via the herdr transport and confirms landing against the durable
    delivery ledger, never blind-resending.
    """

    repo_root: Path
    request: RecoveryRequest
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS
    ledger: Optional[HerdrDeliveryLedger] = None
    #: The startup-attestation store home the redispatch post-launch boundary reads (Redmine
    #: #13806 R2-F3). ``None`` = the real state home; tests inject an isolated one.
    attestation_home: Optional[Path] = None

    def _ledger(self) -> HerdrDeliveryLedger:
        return self.ledger if self.ledger is not None else HerdrDeliveryLedger()

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def observe_target(self, request: RecoveryRequest) -> RecoveryObservation:
        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            rows = self._rows()
        except Exception:  # noqa: BLE001 - unreadable inventory => identity_unknown, fail closed
            return RecoveryObservation()
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.assigned_name)
        ]
        exact = [r for r in matches if _agent_locator(r) == _norm(request.locator)]
        if len(exact) != 1 or len(matches) != 1:
            return RecoveryObservation()  # ambiguous / absent => identity_unknown
        row = exact[0]
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            return RecoveryObservation()
        identity = decoded.identity
        # herdr's assigned-name identity carries workspace / role / lane, not a separate
        # provider (its `role` IS the provider), so provider is validated by the exact
        # assigned-name + locator match, not a separate observable field.
        identity_resolved = (
            identity.workspace_id == workspace_id
            and _norm_lane(identity.lane_id) == _norm_lane(request.lane)
            and identity.role == _norm(request.role)
        )
        if not identity_resolved:
            return RecoveryObservation()
        # A STANDARD sublane WORKER (Redmine #13806 R2-F1): positively the configured worker
        # (implementer) provider, NOT the lane gateway (coordinator) provider, NOT the default
        # coordinator lane, and the approval itself must name the worker provider. A same-issue
        # lane gateway (a non-``default`` lane but the gateway provider) is therefore rejected as
        # ``gateway_or_foreign_protected``, never closed as a worker. An unresolvable provider
        # binding fails closed (not a worker).
        worker_provider, gateway_provider = self._worker_gateway_providers()
        is_standard = bool(worker_provider) and (
            _norm_lane(identity.lane_id) != "default"
            and identity.role == worker_provider
            and identity.role != gateway_provider
            and _norm(request.role) == worker_provider
        )
        # a live-generation revision match (a same-name recycle at a new revision is stale gen)
        revision_raw = row.get("revision")
        row_revision = (
            _norm(revision_raw) if not isinstance(revision_raw, bool) else ""
        )
        generation_matches = bool(row_revision) and (
            row_revision == _norm(request.lane_revision)
            or _norm(request.lane_revision) == ""  # revision not carried in the row shape
        )
        runtime_state = _row_runtime_state(row)
        not_productive = runtime_state != RUNTIME_BUSY
        is_stale = classify_named_slot(row) == SLOT_STALE
        worktree_readable = self._worktree_readable(row)
        no_conflict = True  # a competing transaction is caught by the store's generation CAS
        return RecoveryObservation(
            identity_resolved=identity_resolved,
            is_standard_sublane_worker=is_standard,
            issue_lane_matches=self._issue_lane_matches(identity, request),
            generation_matches=generation_matches,
            not_productive=not_productive,
            is_stale=is_stale,
            worktree_readable=worktree_readable,
            no_authority_conflict=no_conflict,
        )

    def _worker_gateway_providers(self) -> tuple[str, str]:
        """The configured ``(worker_provider, gateway_provider)`` or ``("", "")`` (fail-closed).

        An unresolvable role→provider binding yields empty strings, so a slot can never be
        classified as a standard worker without a positive binding (Redmine #13806 R2-F1).
        """
        try:
            return (
                resolve_worker_provider(str(self.repo_root)),
                resolve_gateway_provider(str(self.repo_root)),
            )
        except WorkflowProviderUnresolved:
            return "", ""

    @staticmethod
    def _issue_lane_matches(identity, request: RecoveryRequest) -> bool:
        # The lane id encodes the owning issue (``issue_<id>_...``); match it against the
        # approval's issue. A lane that does not name the approved issue is a wrong-issue-lane.
        lane = _norm_lane(identity.lane_id)
        issue = _norm(request.issue)
        return bool(issue) and (f"issue_{issue}" in lane or f"issue{issue}" in lane)

    def _worktree_readable(self, row: Mapping[str, object]) -> bool:
        raw = _norm(row.get("foreground_cwd") or row.get("cwd"))
        if not raw:
            return False
        try:
            return probe_worktree_resolved(str(raw)) is True
        except Exception:  # noqa: BLE001 - unreadable worktree fails closed
            return False

    # -- redispatch (high-level rail + REAL delivery-ledger oracle, Redmine #13806 R2-F3) ----

    def redispatch_gate(self, continuation: ContinuationPointer) -> str:
        """Redispatch the ORIGINAL gate to the fresh worker via the high-level dispatch rail.

        Uses the existing governed same-lane worker-forward rail
        (:meth:`HerdrWorkerDispatchOps.dispatch_to_worker` = ``handoff send --mode queue-enter``),
        which submit-completes to the fresh worker and records the delivery to the durable
        ledger through the REAL writer (:func:`record_herdr_delivery`) — never a bare
        ``send_text`` and never a self-authored ``status=sent`` record (R2-F3). Returns
        :data:`DRAIN_SEND_OK` only when the delivery-ACK exit code is 0 (the send fired). Landing
        is confirmed separately by :meth:`gate_redispatched` reading the real ledger — a
        successful send here is only an attempt, never promoted to completion.
        """
        locator = self._fresh_worker_locator()
        if not locator or locator == _norm(self.request.locator):
            # No fresh worker resolved yet (or still the old locator) — never dispatch blind.
            return DRAIN_SEND_ERROR
        try:
            ops = HerdrWorkerDispatchOps(
                repo_root=self.repo_root, lane_label=_norm(self.request.lane),
                issue=_norm(continuation.issue_id), env=self.env, runner=self.runner,
                timeout=self.timeout,
            )
            rc = ops.dispatch_to_worker(
                issue=_norm(continuation.issue_id), journal=_norm(continuation.journal_id),
                worker_pane=locator, lane_label=_norm(self.request.lane),
                gateway_callback_target=None, target_repo=str(self.repo_root),
            )
        except Exception:  # noqa: BLE001 - a fixed dispatch failure; the ledger is untouched
            return DRAIN_SEND_ERROR
        return DRAIN_SEND_OK if rc == 0 else DRAIN_SEND_ERROR

    def gate_redispatched(self, continuation: ContinuationPointer) -> bool:
        """Has the original gate CONFIRMED-landed on the FRESH worker? (durable idempotency)

        Reads the REAL herdr delivery ledger (written by the dispatch rail, never self-written)
        and confirms only a record that is unmistakably the redispatch to the fresh worker
        (Redmine #13806 R2-F3), matching ALL of:

        - the exact anchor (``issue_id`` + ``journal_id`` of the continuation);
        - the resolved worker ``receiver`` provider;
        - ``status == "sent"`` AND an **accepted reason** (``ok`` — a landing-marker-observed
          submit; a bare ``queue_enter`` / unconfirmed reason is NOT confirmed);
        - recorded **after the fresh worker's startup attestation** (``recorded_at`` >
          the fresh attestation ``observed_at``) — so the INITIAL delivery to the now-vanished
          old worker (same anchor, before the recovery) can never be mistaken for the redispatch.

        Any unreadable ledger / missing fresh attestation / only-``queue_enter`` record yields
        ``False`` (the use case then reports ``uncertain`` and never blind-resends).
        """
        fresh_observed_at = self._fresh_attestation_observed_at()
        if not fresh_observed_at:
            return False  # no fresh attested worker => cannot establish the post-launch boundary
        worker_provider, _gateway = self._worker_gateway_providers()
        try:
            records = self._ledger().records_for_issue(_norm(continuation.issue_id))
        except Exception:  # noqa: BLE001 - unreadable ledger => not confirmed (never assume sent)
            return False
        for rec in records:
            if (
                _norm(rec.journal_id) == _norm(continuation.journal_id)
                and _norm(rec.status) == "sent"
                and _norm(rec.reason) == "ok"  # accepted (marker-observed submit), not queue_enter
                and (not worker_provider or _norm(rec.receiver) == worker_provider)
                and _recorded_after(rec.recorded_at, fresh_observed_at)
            ):
                return True
        return False

    def _fresh_worker_locator(self) -> str:
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001
            return ""
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(self.request.assigned_name)
        ]
        if len(matches) != 1:
            return ""
        return _agent_locator(matches[0])

    def _fresh_attestation_observed_at(self) -> str:
        """The fresh worker's startup-attestation ``observed_at`` (the post-launch boundary).

        Empty when no attestation exists / is unreadable — the redispatch cannot then be
        distinguished from the initial old-worker delivery, so it is treated as unconfirmed.
        """
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        try:
            record = HerdrIdentityAttestationStore(home=self.attestation_home).read(
                _norm(self.request.assigned_name)
            )
        except Exception:  # noqa: BLE001 - unreadable attestation fails closed
            return ""
        return _norm(record.observed_at) if record is not None else ""


__all__ = (
    "LiveRecoveryActuatorPort",
    "LiveStaleWorkerRecoveryOps",
)
