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
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    replacement_action_bound_after_identity_join,
)
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_BRANCH_DRIFTED,
    LAUNCH_AUTHORITY_GENERATION_MOVED,
    LAUNCH_AUTHORITY_LIFECYCLE_ABSENT,
    LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE,
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_PINS_UNPINNED,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE,
    LAUNCH_AUTHORITY_WORKTREE_UNREADABLE,
    launch_authority_current,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_launch_failure import (  # noqa: E501
    LAUNCH_FAILURE_NONE,
    LAUNCH_FAILURE_UNTYPED,
    normalize_launch_failure_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    RecoveryObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneLauncherIncompatibleError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    SublaneHealError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_launch_cause import (  # noqa: E501
    launch_cause_for_pin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (  # noqa: E501
    HerdrWorkerDispatchOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    replacement_store_admission as _replacement_store_admission,
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
    SLOT_LIVE,
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
    #: The typed, value-free reason the LAST :meth:`launch_action_bound` fenced on, as the
    #: optional port capability :func:`...domain.replacement_launch_failure.
    #: port_launch_failure_reason` reads (Redmine #14480). Empty after a successful launch and
    #: before the first one — the generic actuator's hardcoded ``detail="launch"`` carries no
    #: information, so without this the operator cannot tell a binding-context fence from a
    #: transient pane failure (#14479 j#88695). Never part of the constructor signature: it is
    #: an observation this port makes, not authority a caller supplies.
    launch_failure_reason: str = field(default=LAUNCH_FAILURE_NONE, init=False)
    #: The nested locator-free startup observation of an UNHEALTHY fresh launch (Redmine
    #: #13948 R3), so a caller can surface the explicit public rollback pointer for the same
    #: startup action. ``None`` unless the last launch fenced on a launch-health reason.
    launch_startup_health: object = field(default=None, init=False)

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
        # #14203 review j#87370 F2: re-verify the pinned live-inventory ROW revision at the
        # close boundary. A NON-EMPTY pin must exactly equal the live row's own revision — a
        # same-name/-locator slot recycled at a new process generation blocks HERE (action-time,
        # immediately before the close) instead of being closed under the old approval. An empty
        # pin preserves the #13806 recover-stale contract (the row shape may not carry one).
        pinned_row_rev = _norm(getattr(self.request, "worker_revision", ""))
        if pinned_row_rev:
            raw_rev = row.get("revision")
            live_row_rev = _norm(raw_rev) if not isinstance(raw_rev, bool) else ""
            if live_row_rev != pinned_row_rev:
                return PreservationObservation(
                    identity_matches=False,
                    detail=(
                        "row_revision_drift:pinned=" + pinned_row_rev
                        + ":live=" + (live_row_rev or "absent")
                    ),
                )
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
        # When the identity fence fires, name the comparison AXIS in the (never-secret) detail
        # so a durable ``identity_mismatch`` says which authority diverged and its observed vs
        # pinned values (Redmine #13806 recover-stale: the lane lifecycle is the axis the
        # revision-authority split exposed). Only the lane-lifecycle counters + locator are
        # emitted — no worktree bytes, journal content, or credentials.
        detail = "" if identity_ok else self._preservation_axis_detail(pin, row, live_rev, live_gen)
        # For a worker recovery only running_process / identity_mismatch block (the recovery
        # preservation policy byte-preserves a dirty worktree). attestation_fresh is set True so
        # the (unused-by-recovery-policy) attestation fence never spuriously fires.
        return PreservationObservation(
            running_process=_row_runtime_state(row) == RUNTIME_BUSY,
            identity_matches=identity_ok,
            attestation_fresh=True,
            detail=detail,
        )

    @staticmethod
    def _preservation_axis_detail(
        pin: ParticipantPin, row: Mapping[str, object], live_rev: str, live_gen: str
    ) -> str:
        """Name the diverging identity axis: lane lifecycle first, then locator (no secrets)."""
        if pin.lane_revision and pin.lane_revision != _norm(live_rev):
            return (
                f"lane_lifecycle_revision observed={_norm(live_rev)!r} "
                f"pinned={pin.lane_revision!r}"
            )
        if pin.lane_generation and pin.lane_generation != _norm(live_gen):
            return (
                f"lane_lifecycle_generation observed={_norm(live_gen)!r} "
                f"pinned={pin.lane_generation!r}"
            )
        observed_locator = _agent_locator(row)
        if pin.old_locator != _norm(observed_locator):
            return f"locator observed={_norm(observed_locator)!r} pinned={pin.old_locator!r}"
        return "stable_identity_mismatch"

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
                role=pin.provider, assigned_name=pin.assigned_name, locator=pin.old_locator
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
        """Relaunch the fresh participant carrying the exact ``action_id`` (#13806 R2-F2).

        Constructs the herdr lane actuator with ``replacement_action_id=action_id`` so the
        fresh process's startup self-attestation records it — the durable action binding
        :meth:`verify_attestation` re-checks. Not the plain ``heal_receiver`` (which drops the
        action id): a fresh relaunch that does not carry the exact replacement action can never
        be verified as THIS recovery's worker.

        **The exact pin IS the launch's binding context (Redmine #14480).** While the selected
        identity-attestation store is v1, the action binding is a side record keyed on the exact
        participant, so ``launch_or_resume_v1_replacement`` requires the target's ``provider`` /
        ``assigned_name`` / ``old_locator`` and refuses a partial context with
        ``replacement_binding_context_missing``. Passing only the action id therefore did not
        launch "generically" — it could not launch AT ALL under v1, which is what #14479 j#88695
        measured: two consecutive ``effect_failed: launch`` on a committed-close replay whose
        lane authority was ``ok``. The pin is the single authority already carried through
        close and verify; the launch now reads its context from that same pin instead of
        re-deriving a narrower one (the sibling ``_BoundPairActuatorPort`` shape).

        ``target_provider`` scopes the same-tab postcondition to THIS owed participant. The
        actuator drives exactly ONE participant per launch, so the pair is partial *by
        construction* at this edge (recover-gateway: gateway closed, worker live; recover-stale:
        worker closed, gateway live). The pair-level launcher still adopts the surviving sibling
        rather than relaunching it — ``prepare_session`` is adopt-or-launch idempotent per slot —
        and a LIVE split is still fail-closed (:func:`...enforce_heal_postcondition` only relaxes
        the *absent*-sibling case, never the split one). Scoping is not optional here: an empty /
        unscoped provider is not in the managed pair and re-raises the same v1 context refusal.

        A fenced launch stashes the fence's stable, value-free ``reason`` token (and, for an
        unhealthy launch, its locator-free startup observation) so the public outcome can name
        WHY instead of collapsing every cause into a bare ``effect_failed: launch``. The
        broad final ``except`` stays — it is the fail-closed floor — but it no longer swallows
        the typed reasons the fences above it raise.
        """
        cause = launch_cause_for_pin(pin)
        if not cause:
            # An unusable cause is decided BEFORE the actuator is built, so the first Herdr
            # write never happens (Redmine #14741 j#97171). Value-free: the offending token
            # is not carried into the public reason.
            self.launch_failure_reason = LAUNCH_FAILURE_UNTYPED
            self.launch_startup_health = None
            return LAUNCH_ERROR
        try:
            HerdrSublaneActuatorOps(
                repo_root=self.repo_root, lane_label=_norm(self.request.lane),
                issue=_norm(self.request.issue), journal=_norm(self.request.journal),
                env=self.env, runner=self.runner, timeout=self.timeout,
                replacement_action_id=_norm(action_id),
                replacement_assigned_name=_norm(pin.assigned_name),
                replacement_old_locator=_norm(pin.old_locator),
                replacement_launch_cause=cause,
            ).heal_lane_column(
                str(self.repo_root), target_provider=_norm(pin.provider) or None
            )
        except SublaneHealError as exc:
            # The typed heal / v1-binding fence (context missing, authority conflict, startup
            # debt, unhealthy launch, pair split, ...). Its reason is a closed token by
            # contract, so it is projected verbatim; the shape guard in the projector is the
            # backstop against anything that is not one.
            self.launch_failure_reason = (
                normalize_launch_failure_reason(exc.reason) or LAUNCH_FAILURE_UNTYPED
            )
            self.launch_startup_health = getattr(exc, "startup", None)
            return LAUNCH_ERROR
        except SublaneLauncherIncompatibleError as exc:
            self.launch_failure_reason = (
                normalize_launch_failure_reason(exc.reason) or LAUNCH_FAILURE_UNTYPED
            )
            self.launch_startup_health = None
            return LAUNCH_ERROR
        except Exception:  # noqa: BLE001 - a fixed launch failure, no body persisted
            # Untyped (a transport / adapter error, or the heal preflight's plain RuntimeError).
            # Reported as a failure with an honest "we do not know why" — never as no failure,
            # and never by parsing the exception's prose into a public field.
            self.launch_failure_reason = LAUNCH_FAILURE_UNTYPED
            self.launch_startup_health = None
            return LAUNCH_ERROR
        self.launch_failure_reason = LAUNCH_FAILURE_NONE
        self.launch_startup_health = None
        return LAUNCH_DONE

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        """Verify the fresh worker is fresh AND bound to THIS action (Redmine #13806 R2-F2).

        Fresh identity / locator / post-transaction freshness (the #13763 join) is necessary
        but not sufficient — the fresh process's startup self-attestation must also record the
        exact replacement ``action_id`` (option B, Design Answer j#79556):

        - no fresh attestation yet (still booting / not fresh) -> :data:`ATTEST_PENDING`;
        - fresh, but not bound to this action -> :data:`ATTEST_MISMATCH` (a fresh slot NOT
          launched by this recovery is never adopted);
        - fresh AND exact action binding -> :data:`ATTEST_BOUND`.

        **The action binding has two shapes, and reading only the direct field sees one of
        them (Redmine #14485).** Under a selected v2 store the fresh row carries
        ``replacement_action_id`` itself; under v1 it CANNOT (#13882 holds the on-disk shape
        while older installed launchers are live), so the launch writes a normal v1 attestation
        plus a separate bound side record. This method used to compare only
        ``record.replacement_action_id``, so a v1 row — whose direct field is empty *by design*
        — could never match, and a correctly bound v1 replacement was permanently
        ``attestation_mismatch``. #14484 measured exactly that on installed 0.14.0a4: the old
        gateway closed, the fresh one launched and attested ``present``, the side record was
        ``phase=bound`` on the same exact action / name / locator / old locator, and the
        execute still stopped partial. #14480 fixed this authority model on the LAUNCH side;
        this is its post-launch half.

        The judgement itself is NOT re-implemented here — it is
        :func:`...replacement_action_bound_after_identity_join`, the same function the
        bound-pair convergence rail calls. Two post-launch verifications reading one rule is
        the point: a second local copy is how they would drift.

        ``verify_fresh_receiver`` above is the identity join, and it is what supplies the
        FRESH ``locator`` the v1 side record must agree with — the old pinned locator would
        match the side record's ``old_locator`` instead, which is precisely what the evaluator
        refuses. Fail-closed at every unresolved input: an unresolvable workspace identity is
        passed through as an empty token, which can never equal the record's workspace, so the
        v1 leg refuses rather than guessing (and the v2 leg, already covered by the join above,
        is left exactly as it was).
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
        try:
            workspace_id = _norm(repo_scope_workspace_id(self.repo_root))
        except Exception:  # noqa: BLE001 - see the docstring: empty never joins, so v1 refuses
            workspace_id = ""
        if replacement_action_bound_after_identity_join(
            record,
            action_id=_norm(action_id),
            live_locator=_norm(verification.locator),
            workspace_id=workspace_id,
            role=_norm(self.request.role),
            lane=_norm_lane(self.request.lane),
            assigned_name=_norm(self.request.assigned_name),
            old_locator=_norm(pin.old_locator),
            home=self.attestation_home,
        ):
            return ATTEST_BOUND
        # A fresh, attested slot whose startup did NOT bind this exact action — a different
        # (or no) replacement authority launched it. Never complete the participant on it.
        return ATTEST_MISMATCH


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
    #: The lane-lifecycle store home the post-close resume re-verification reads (Redmine #13806
    #: R3-F1). ``None`` = the real state home; tests inject an isolated one.
    lifecycle_home: Optional[Path] = None

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
        # A STANDARD sublane WORKER (Redmine #13806 R2-F1 / R2-R1): positively the configured
        # worker (implementer) provider on EVERY axis — the live slot's role AND the approval's
        # own independent ``role`` and ``provider`` fields must all equal the worker provider and
        # none may be the gateway (coordinator) provider — NOT the default coordinator lane. A
        # same-issue-lane gateway (a non-``default`` lane but the gateway provider), a foreign
        # slot, OR an approval whose provider pin points at the gateway / a foreign provider, is
        # rejected as ``gateway_or_foreign_protected`` (never closed as a worker). An unresolvable
        # provider binding fails closed (not a worker). ``request.provider`` is validated here
        # BECAUSE it is the pin that enters the transaction authority yet is not a herdr-observable
        # field downstream — so an unchecked foreign provider pin would otherwise pass unseen.
        worker_provider, gateway_provider = self._worker_gateway_providers()
        is_standard = bool(worker_provider) and (
            _norm_lane(identity.lane_id) != "default"
            and identity.role == worker_provider
            and identity.role != gateway_provider
            and _norm(request.role) == worker_provider
            and _norm(request.provider) == worker_provider
            and _norm(request.provider) != gateway_provider
        )
        # A live worker-row generation match: the live worker inventory row's OWN ``revision``
        # against the approval's pinned WORKER revision — a distinct authority from the lane
        # lifecycle (Redmine #13806 recover-stale revision-authority split). Conflating the two
        # under one ``--lane-revision`` left an installed binary unable to satisfy both this
        # preflight gate and the close-boundary lane-lifecycle preservation fence with one value.
        # A same-name recycle at a bumped row revision is a stale generation. Empty pin matches
        # any present row revision (the row shape may not carry one).
        revision_raw = row.get("revision")
        row_revision = (
            _norm(revision_raw) if not isinstance(revision_raw, bool) else ""
        )
        generation_matches = bool(row_revision) and (
            row_revision == _norm(request.worker_revision)
            or _norm(request.worker_revision) == ""  # revision not carried in the row shape
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

    @staticmethod
    def _current_branch(path: str) -> str:
        """The worktree's current branch name, or ``""`` (fail-closed). Read-only."""
        import subprocess

        if not path or not Path(path).is_dir():
            return ""
        try:
            result = subprocess.run(
                ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                text=True, capture_output=True,
            )
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def lane_authority_reason(self, request: RecoveryRequest) -> str:
        """WHICH lane-authority axis fails right now? (read-only, #13806 R3-F1 / #14475)

        Re-joins EVERY exact lane-authority axis (Review j#82731 F2 / Answer j#82708), old-slot-
        independent, and names the FIRST failing one as a closed
        :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` token:

        - the LIVE lane lifecycle exists and its ``(revision, generation)`` equals the pinned
          ``lane_revision`` / ``lane_generation``;
        - the lifecycle's canonical ``worktree_identity`` token is non-empty AND equals the token
          freshly derived from the recovery worktree (``derive_lane_workspace_token(repo_root)``) —
          a sibling / wrong / moved worktree fails here;
        - the recovery worktree resolves to a live git checkout AND its current branch equals the
          lane's expected branch (the issue lane id IS its branch) — an unreadable worktree or a
          drifted branch fails here.

        Dirtiness is deliberately NOT an axis (Answer j#82708 Option A). Fail-closed: any absent /
        mismatched axis returns its blocking token, and only a fully-joined authority returns
        :data:`...lane_launch_authority.LAUNCH_AUTHORITY_OK`.

        This is the SINGLE evaluator behind both the action-time launch fence
        (:meth:`resume_lane_authority`) and the read-only preflight axis a guarded refresh
        reports (Redmine #14475): a preflight backed by a second implementation is how a
        preflight drifts away from the effect it claims to predict — which is exactly how
        #14462 j#88463 closed a gateway it could never relaunch.
        """
        pinned_rev = _norm(request.lane_revision)
        pinned_gen = _norm(request.lane_generation)
        if not pinned_rev or not pinned_gen:
            return LAUNCH_AUTHORITY_PINS_UNPINNED
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleError,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        try:
            record = LaneLifecycleStore(home=self.lifecycle_home).get(
                LaneLifecycleKey(repo_scope_workspace_id(self.repo_root), _norm_lane(request.lane))
            )
        except (LaneLifecycleError, ValueError, OSError):
            # An unreadable authority is never degraded to a proven-absent one.
            return LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE
        if record is None:
            return LAUNCH_AUTHORITY_LIFECYCLE_ABSENT
        if str(record.revision) != pinned_rev or str(record.lane_generation) != pinned_gen:
            return LAUNCH_AUTHORITY_GENERATION_MOVED
        # Exact worktree-token authority: the lane's canonical token must be present AND equal the
        # token freshly derived from the recovery worktree (a wrong / sibling worktree differs).
        pinned_token = _norm(record.worktree_identity)
        if not pinned_token:
            # Redmine #14475: an unbound row (a supersede-minted recovery lane whose later
            # create declaration was refused ``already_declared`` zero-write) can never attest
            # a worktree. Distinct from a MISMATCH: nothing to compare, not a wrong compare.
            return LAUNCH_AUTHORITY_WORKTREE_UNBOUND
        try:
            # Canonical (symlink-resolved) root, per ``derive_lane_workspace_token``'s stated
            # contract (review j#88494). Unresolved, a lane invoked through a symlink alias
            # reads as ``worktree_identity_mismatch`` and its guarded recovery is refused for
            # a binding that is in fact exact.
            live_token = _norm(
                derive_lane_workspace_token(str(Path(self.repo_root).resolve()))
            )
        except Exception:  # noqa: BLE001 - an underivable token fails closed
            return LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE
        if not live_token:
            return LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE
        if live_token != pinned_token:
            return LAUNCH_AUTHORITY_WORKTREE_MISMATCH
        # Readable worktree on the lane's expected branch (the issue lane id is the branch).
        try:
            if probe_worktree_resolved(str(self.repo_root)) is not True:
                return LAUNCH_AUTHORITY_WORKTREE_UNREADABLE
        except Exception:  # noqa: BLE001 - unreadable worktree fails closed
            return LAUNCH_AUTHORITY_WORKTREE_UNREADABLE
        if _norm_lane(self._current_branch(str(self.repo_root))) != _norm_lane(request.lane):
            return LAUNCH_AUTHORITY_BRANCH_DRIFTED
        return LAUNCH_AUTHORITY_OK

    def replacement_store_admission(self, key, pin) -> Optional[str]:
        """The pre-close epoch/store verdict, against THIS ops object's homes (#14756).

        Both homes are passed explicitly rather than left ambient. This is exactly the case
        where they can differ from the process-wide ones — the live ops carries isolated
        homes under test — and a fence that read the real shared home there would be
        measuring the wrong store while appearing to work.
        """
        return _replacement_store_admission(
            key.workspace_id,
            pin.lane_id,
            lifecycle_home=str(self.lifecycle_home) if self.lifecycle_home else "",
            attestation_home=str(self.attestation_home) if self.attestation_home else "",
        )

    def resume_lane_authority(self, request: RecoveryRequest) -> bool:
        """Is the lane's ambient authority EXACT and current, right now? (read-only)

        The boolean projection of :meth:`lane_authority_reason` — byte-for-byte the pre-#14475
        contract (ONLY a fully-joined authority is ``True``), now expressed as the single
        closed-vocabulary projection so the action-time fence and the preflight axis can never
        disagree about what "current" means.
        """
        return launch_authority_current(self.lane_authority_reason(request))

    def lane_free_of_live_process(self, request: RecoveryRequest) -> bool:
        """Is the lane free of ANY foreign live process (busy OR idle)? (read-only, #13806 R3-F1)

        Scan the live herdr inventory for a LIVE slot at the recovery's assigned name (old-slot-
        independent). A pre-launch fence: the old worker is closed and the fresh worker is not yet
        launched, so ANY row at the name that classifies :data:`SLOT_LIVE` — busy OR idle — is a
        foreign process the relaunch must not collide with. A positive shell-residue (:data:`SLOT_STALE`,
        what recover-stale recovers) is not live and does not fence. Fail-closed: an unreadable
        inventory returns ``False``.
        """
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001 - unreadable inventory fails closed
            return False
        name = _norm(request.assigned_name)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if _norm(row.get(AGENT_KEY_NAME)) != name:
                continue
            if classify_named_slot(row) == SLOT_LIVE:
                return False
        return True

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

    def _redispatch_marker(self, continuation: ContinuationPointer, worker_provider: str) -> str:
        """The EXACT ``[mozyo:handoff:...]`` marker ``dispatch_to_worker`` writes (byte-for-byte).

        Built through the CANONICAL :func:`...handoff.build_marker` from the continuation's
        immutable ``expected_gate`` (Redmine #13806 R3-F1) + the exact Redmine anchor + the
        resolved worker provider — the same authority the rail uses, so it stays byte-identical
        even if the marker format evolves. The use case has already fenced ``expected_gate ==
        implementation_request`` (the only kind the worker-forward rail sends), so the marker
        kind, the send kind, and the pointer's gate kind are one closed token. A delivery of a
        different gate kind / anchor / receiver produces a different marker and can never be
        mistaken for THIS redispatch (R2-R2).
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
            build_marker,
        )

        return build_marker(
            RedmineAnchor(
                issue=_norm(continuation.issue_id), journal=_norm(continuation.journal_id)
            ),
            _norm(continuation.expected_gate),
            worker_provider,
        )

    def gate_redispatched(self, continuation: ContinuationPointer) -> bool:
        """Has the original gate CONFIRMED-landed on the exact FRESH worker? (durable idempotency)

        Reads the REAL herdr delivery ledger (written by the dispatch rail, never self-written)
        and confirms ONLY a record that is unmistakably THIS redispatch to the fresh worker
        (Redmine #13806 R2-F3 / R2-R2). Every axis is matched fail-closed — a single mismatch is
        not confirmed (the use case then reports ``uncertain`` and never blind-resends):

        - the resolved worker provider (unresolved binding => never confirmed, not skipped);
        - a live fresh worker locator distinct from the vanished old one;
        - the **exact deterministic notification marker** (source=redmine + exact issue/journal
          anchor + ``kind=implementation_request`` + ``to=<worker_provider>``) — a wrong gate
          kind / anchor / receiver is a different marker;
        - the ledger anchor (``source=redmine`` / ``issue_id`` / ``journal_id``), ``receiver``
          == the worker provider, ``backend=herdr``, ``rail=queue_enter_rail``;
        - the ``provider`` column as a **compatibility-aware optional assertion** (Design Answer
          j#79584): ``_norm(rec.provider) in ("", worker_provider)``. The generic herdr send path
          leaves ``provider`` empty (only ``receiver`` carries the binding-resolved provider), so
          the canonical real record's empty ``provider`` is honoured; a *present-but-contradictory*
          ``provider`` (e.g. ``codex``) is rejected. Empty-allowed is generic-writer
          compatibility, NOT fail-open — the positive provider authority is the exact marker's
          ``to=<worker_provider>`` and the populated ``receiver``;
        - ``target`` == the **current fresh worker locator** — so a delivery to any other pane
          (incl. the pre-recovery delivery to the now-vanished old worker) is rejected;
        - ``status=sent`` AND an **accepted reason** (``ok`` — a landing-marker-observed submit;
          a bare ``queue_enter`` / unconfirmed reason is NOT confirmed);
        - recorded **after the fresh worker's startup attestation** — a second, temporal fence
          against the same-anchor pre-recovery delivery.
        """
        worker_provider, _gateway = self._worker_gateway_providers()
        if not worker_provider:
            return False  # unresolved provider binding => fail-closed (never skip the check)
        fresh_observed_at = self._fresh_attestation_observed_at()
        if not fresh_observed_at:
            return False  # no fresh attested worker => cannot establish the post-launch boundary
        fresh_locator = self._fresh_worker_locator()
        if not fresh_locator or fresh_locator == _norm(self.request.locator):
            return False  # no distinct fresh worker resolved
        marker = self._redispatch_marker(continuation, worker_provider)
        try:
            records = self._ledger().records_for_marker(marker)
        except Exception:  # noqa: BLE001 - unreadable ledger => not confirmed (never assume sent)
            return False
        for rec in records:
            if (
                _norm(rec.notification_marker) == marker
                and _norm(rec.source) == "redmine"
                and _norm(rec.issue_id) == _norm(continuation.issue_id)
                and _norm(rec.journal_id) == _norm(continuation.journal_id)
                and _norm(rec.receiver) == worker_provider
                # provider is caller-supplied optional metadata the generic writer leaves empty;
                # a present-but-contradictory value is rejected (Design Answer j#79584).
                and _norm(rec.provider) in ("", worker_provider)
                and _norm(rec.backend) == "herdr"
                and _norm(rec.rail) == "queue_enter_rail"
                and _norm(rec.target) == fresh_locator
                and _norm(rec.status) == "sent"
                and _norm(rec.reason) == "ok"  # accepted (marker-observed submit), not queue_enter
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
