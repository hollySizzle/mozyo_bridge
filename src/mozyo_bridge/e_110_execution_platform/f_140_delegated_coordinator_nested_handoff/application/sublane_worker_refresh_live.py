"""Live adapters for the guarded live-worker refresh (Redmine #14661).

The public ``sublane refresh-worker`` command is only useful if it actually observes the live
inventory / durable sources and drives the real close → same-slot launch → attestation →
anchor resume (the #13806 R1-F1 / #14203 j#87356 F1 lesson, re-learned twice: a fail-closed
staged seam leaves the product gap open). This module wires
:class:`...sublane_worker_refresh.WorkerRefreshUseCase` to the real runtime by REUSING the
proven adapters of the two sibling surfaces rather than re-deriving them:

* the exact-generation close / relaunch / attestation port is the #13806
  :class:`...sublane_stale_worker_recovery_live.LiveRecoveryActuatorPort` over a field-adapted
  pin (it carries no worker-vs-gateway semantics — the role protection lives in the preflight
  decision);
* the slot-identity / worker-provider / worktree-readability axes and the lane-authority /
  name-liveness probes delegate to
  :class:`...sublane_stale_worker_recovery_live.LiveStaleWorkerRecoveryOps` — the SAME
  evaluator, never a second implementation;
* the resume delivers the EXISTING durable anchor to the FRESH worker through the governed
  ``handoff send`` rail as a ``reply`` pointer (the #14203 j#84223 owner-approved resume shape)
  and confirms landing against the REAL herdr delivery ledger.

Why the resume is NOT :meth:`...LiveStaleWorkerRecoveryOps.redispatch_gate`: that rail
(:meth:`HerdrWorkerDispatchOps.dispatch_to_worker`) hardcodes ``kind="implementation_request"``
while its confirmation marker is built from the continuation's ``expected_gate`` — which is why
#13806 fenced that surface to an ``implementation_request`` anchor. A live worker refresh must
resume whatever resumable gate the lane is actually blocked on (a ``review_result`` round is
the #14658 shape), so it takes the same uniform ``reply``-pointer transport its gateway sibling
uses: the anchor's own gate kind stays the continuation authority, and the transport kind is
one closed token for every anchor.

Every observation fails closed: an unreadable inventory / ledger / render / durable source
leaves the positive fact ``False`` (identity_unknown / turn_unobservable — never actuated).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
from mozyo_bridge.core.state.replacement_transaction import ContinuationPointer
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
    DRAIN_SEND_ZERO,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    gateway_generation_authority as _gen_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
    read_render_ghost_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
    LiveStaleWorkerRecoveryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WorkerRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    RESUMABLE_GATES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_GENERATION_MOVED,
    LAUNCH_AUTHORITY_LIFECYCLE_ABSENT,
    LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE,
    LAUNCH_AUTHORITY_PINS_UNPINNED,
    LAUNCH_AUTHORITY_UNKNOWN,
    launch_authority_current,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
    is_journal_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_refresh_approval import (  # noqa: E501
    WorkerRefreshApprovalError,
    verify_worker_refresh_approval,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_PROGRESS_GATES,
    WorkerRefreshObservation,
    WorkerTurnObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_replacement_launch_admission import (  # noqa: E501
    replacement_managed_launch_admission as _replacement_store_admission,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
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
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_close_boundary import (  # noqa: E501
    CLOSE_REFUSED_PROGRESS_MOVED,
    SettledCloseBoundaryPort,
)

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_durable_read import (  # noqa: E501
    notes_carry_worker_progress,
    worker_progress_facts,
)

_STATUS_KEYS = ("agent_status", "status", "state")

#: The transport kind the anchor resume is delivered under (the #14203 j#84223 shape, shared).
_RESUME_TRANSPORT_KIND = "reply"

#: The launch-authority tokens that mean the pinned LANE GENERATION could not be joined to the
#: live lifecycle at all (unpinned / unreadable / absent / moved / unknown). Projected from the
#: SINGLE :meth:`lane_authority_reason` evaluator rather than re-reading the lifecycle store, so
#: the #14661 ``lane_generation_bound`` classification binding and the #14475 launch fence can
#: never disagree about what the lane's generation is. The worktree / branch tokens are a
#: DIFFERENT axis and deliberately do not unbind the generation.
_GENERATION_UNBOUND_REASONS = frozenset(
    {
        LAUNCH_AUTHORITY_PINS_UNPINNED,
        LAUNCH_AUTHORITY_LIFECYCLE_UNREADABLE,
        LAUNCH_AUTHORITY_LIFECYCLE_ABSENT,
        LAUNCH_AUTHORITY_GENERATION_MOVED,
        LAUNCH_AUTHORITY_UNKNOWN,
    }
)


def port_pin_request(request: WorkerRefreshRequest) -> RecoveryRequest:
    """Adapt the worker refresh pin to the #13806 port/probe request shape. (pure)

    The live actuation port and the delegated probes consume only the identity pin (lane /
    role / provider / assigned_name / locator) + the revision evidence (row revision, lane
    lifecycle ``(revision, generation)``) — none of the gate semantics. Both surfaces pin the
    same LIVE WORKER INVENTORY row revision, so the field maps across by name.
    """
    return RecoveryRequest(
        issue=request.issue, lane=request.lane, role=request.role,
        provider=request.provider, assigned_name=request.assigned_name,
        locator=request.locator, journal=request.journal,
        action_id=request.action_id, action_generation=request.action_generation,
        worker_revision=request.worker_revision,
        lane_revision=request.lane_revision, lane_generation=request.lane_generation,
    )


def _row_runtime_state(row: Mapping[str, object]) -> str:
    for key in _STATUS_KEYS:
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            return map_agent_status(raw)
    return map_agent_status(None)


def _row_revision(row: Mapping[str, object]) -> str:
    raw = row.get("revision")
    return _norm(raw) if not isinstance(raw, bool) else ""


@dataclass
class LiveWorkerRefreshOps:
    """Live observe + exactly-once anchor resume (:class:`WorkerRefreshOps`).

    ``observe_turn`` classifies the delivered anchor's worker provider turn from the REAL herdr
    delivery ledger (callback outcome), the OTel activity timeline (turn-start evidence), a
    FRESH durable journal read (the worker-progress authority), and the three #14661 identity
    bindings. ``observe_target`` classifies the exact pinned worker from the live herdr
    inventory + render observation, delegating the identity / worker-provider / worktree axes
    to the proven #13806 observer. The resume delivers the EXISTING anchor to the fresh worker
    via the governed ``handoff send`` rail and confirms landing against the durable ledger,
    never blind-resending.
    """

    repo_root: Path
    request: WorkerRefreshRequest
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS
    ledger: Optional[HerdrDeliveryLedger] = None
    #: Isolated store homes for tests; ``None`` = the real state homes.
    attestation_home: Optional[Path] = None
    lifecycle_home: Optional[Path] = None
    #: A FRESH durable journal reader: ``journal_reader(issue) -> Sequence[entry]`` where each
    #: entry carries ``journal_id`` + ``notes`` (the RedmineJournalSource shape). ``None`` = no
    #: live durable source is wired in this environment — the turn observation then leaves the
    #: absence facts ``False`` (classifies ``turn_unobservable``, never actuated).
    journal_reader: Optional[object] = None
    #: Marks the ``journal_reader`` as a FRESH (non-snapshot) source (#13889: only a source
    #: declaring freshness may back the absence-of-progress fact).
    journal_reader_fresh: bool = False
    #: ``issuer_resolver(entry) -> ResolvedIssuer`` override for the approval authority
    #: (#14661 j#92494 / j#92601 F1). ``None`` uses the repo's own issuer-resolution policy.
    issuer_resolver: Optional[object] = None

    # -- delegation to the proven #13806 probes --------------------------------

    def _delegate(self) -> LiveStaleWorkerRecoveryOps:
        return LiveStaleWorkerRecoveryOps(
            repo_root=self.repo_root, request=port_pin_request(self.request),
            env=self.env, runner=self.runner, timeout=self.timeout, ledger=self.ledger,
            attestation_home=self.attestation_home, lifecycle_home=self.lifecycle_home,
        )

    def lane_authority_reason(self, request: WorkerRefreshRequest) -> str:
        """The closed launch-authority axis token, from the #13806/#14475 evaluator.

        Delegated — never re-implemented — so the pre-close preflight axis, the action-time
        launch fence, and the #14661 lane-generation binding are all backed by one join.
        """
        return self._delegate().lane_authority_reason(port_pin_request(request))

    def resume_lane_authority(self, request: WorkerRefreshRequest) -> bool:
        return launch_authority_current(self.lane_authority_reason(request))

    def worker_name_free_of_live_process(self, request: WorkerRefreshRequest) -> bool:
        return self._delegate().lane_free_of_live_process(port_pin_request(request))

    def replacement_store_admission(self, key, pin) -> Optional[str]:
        """The pre-close epoch/store verdict, against THIS ops object's homes (#14756).

        Both homes are passed explicitly. A worker refresh is exactly the case where they can
        differ from the ambient ones — the live ops carries isolated homes under test — and a
        fence that read the real shared home there would be measuring the wrong store while
        appearing to work.
        """
        return _replacement_store_admission(
            key.workspace_id,
            pin.lane_id,
            repo_root=self.repo_root,
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
            lifecycle_home=str(self.lifecycle_home) if self.lifecycle_home else "",
            attestation_home=str(self.attestation_home) if self.attestation_home else "",
        )

    # -- live target observation ----------------------------------------------

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _providers(self) -> tuple[str, str]:
        try:
            return (
                resolve_worker_provider(str(self.repo_root)),
                resolve_gateway_provider(str(self.repo_root)),
            )
        except WorkflowProviderUnresolved:
            return "", ""

    def _pinned_row(self, request: WorkerRefreshRequest) -> Optional[Mapping[str, object]]:
        """The EXACTLY-ONE live row at the pinned assigned name + locator, or ``None``.

        The same uniqueness rule the #13806 observer applies: an ambiguous (multi-row) name or
        a locator that no longer resolves yields ``None``, which fails every fact that reads it.
        """
        try:
            rows = list(self._rows())
        except Exception:  # noqa: BLE001 - unreadable inventory => no pinned row
            return None
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.assigned_name)
        ]
        exact = [r for r in matches if _agent_locator(r) == _norm(request.locator)]
        if len(exact) != 1 or len(matches) != 1:
            return None
        return exact[0]

    def pinned_runtime_state(self, request: WorkerRefreshRequest) -> str:
        """The pinned worker's FRESH runtime state, or ``unknown``. (read-only, fail-closed)

        Public because the close-boundary fence (:class:`SettledCloseBoundaryPort`) re-reads
        it at the destructive edge, and it must be the SAME derivation the preflight's
        ``settled_idle`` axis uses — a boundary backed by a second reading would not fence the
        state the preflight predicted. An absent / ambiguous row yields ``unknown``, which is
        not settled and therefore refuses.
        """
        row = self._pinned_row(request)
        if row is None:
            return RUNTIME_UNKNOWN
        return _row_runtime_state(row)

    def _participant_revision_matches(self, request: WorkerRefreshRequest) -> bool:
        """Does the pinned row revision EXACTLY equal the approval's pinned revision?

        Strict on both sides (the #14203 j#87364 F5 rule): an empty pin never matches, so a
        destructive refresh can never ride an unpinned generation. This is deliberately
        stricter than ``recover-stale``'s same-named gate, whose empty pin is tolerated because
        it recovers a slot that is already dead.
        """
        row = self._pinned_row(request)
        if row is None:
            return False
        pinned = _norm(request.worker_revision)
        return bool(pinned) and _row_revision(row) == pinned

    def observe_target(self, request: WorkerRefreshRequest) -> WorkerRefreshObservation:
        # The identity / standard-worker / issue-lane / worktree-readability axes come from the
        # proven #13806 observer (one implementation, one drift surface). Its ``is_stale`` /
        # ``not_productive`` axes are deliberately NOT read: this admission exists precisely
        # because the target is a LIVE worker, and reusing the residue gate would recreate the
        # ``not_stale`` refusal this surface was opened to resolve. Its ``generation_matches``
        # is not read either — it tolerates an empty pin (see
        # :meth:`_participant_revision_matches`).
        base = self._delegate().observe_target(port_pin_request(request))
        if not base.identity_resolved:
            return WorkerRefreshObservation()
        row = self._pinned_row(request)
        if row is None:
            return WorkerRefreshObservation()
        runtime_state = _row_runtime_state(row)
        return WorkerRefreshObservation(
            identity_resolved=True,
            is_standard_sublane_worker=base.is_standard_sublane_worker,
            issue_lane_matches=base.issue_lane_matches,
            generation_matches=self._participant_revision_matches(request),
            settled_idle=runtime_state in (RUNTIME_TURN_ENDED, RUNTIME_AWAITING_INPUT),
            composer_clear=self._composer_clear(request),
            resume_anchor_present=bool(
                _norm(request.resume_anchor_journal) and _norm(request.resume_gate)
            ),
            worktree_readable=base.worktree_readable,
            gateway_distinct_preserved=self._gateway_distinct_preserved(request),
            no_authority_conflict=True,  # a competing txn is caught by the store's CAS
        )

    def _composer_clear(self, request: WorkerRefreshRequest) -> bool:
        """No REAL unsent composer input at the worker. (fail-closed)

        A dim (idle ghost placeholder) render is clear; a NORMAL / mixed-intensity prompt is
        real unsent input (never destroyed by a close); an unobserved / unreadable render is
        NOT clear (fail-closed — closing behind an unreadable composer could destroy input).
        """
        try:
            facts = read_render_ghost_facts(
                self.repo_root, _norm(request.locator), env=self.env
            )
        except Exception:  # noqa: BLE001 - a failed render read fails closed
            return False
        if not facts.observed or not facts.readable:
            return False
        if not facts.prompt_present:
            return True
        return _norm(str(facts.style_provenance)) == "dim"

    def _gateway_distinct_preserved(self, request: WorkerRefreshRequest) -> bool:
        """The lane's GATEWAY slot is positively a LIVE, DIFFERENT slot than the close target.

        The mirror of the gateway refresh's worker-preservation axis: a worker refresh must
        leave the same-lane implementation_gateway running, so an unresolvable gateway binding
        or an indistinguishable pair fails closed.

        Bound to THIS repo's canonical workspace and to EXACTLY ONE candidate (review j#92443
        F3). The herdr inventory is host-global, so lane labels are only unique *within* a
        workspace: without the workspace join, a foreign workspace that happens to run a lane
        of the same name satisfied this axis on its own — the close target's own workspace
        could have no gateway at all and the worker would still read as "preserved". And
        without the uniqueness join, two live same-lane gateway rows also passed, though the
        axis cannot then name WHICH slot it is preserving. The stronger form already existed
        one function away in the sibling adapter (``_same_lane_worker_locator``'s
        workspace + ``len(found) == 1`` join); this is that form, not a new invention.
        """
        _worker_provider, gateway_provider = self._providers()
        if not gateway_provider:
            return False
        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            rows = self._rows()
        except Exception:  # noqa: BLE001 - unreadable workspace / inventory fails closed
            return False
        if not _norm(workspace_id):
            return False
        lane = _norm_lane(request.lane)
        found = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decoded.ok or decoded.identity is None:
                continue
            identity = decoded.identity
            if (
                identity.workspace_id == workspace_id
                and _norm_lane(identity.lane_id) == lane
                and identity.role == gateway_provider
                and _agent_locator(row) != _norm(request.locator)
                and classify_named_slot(row) == SLOT_LIVE
            ):
                found.append(_agent_locator(row))
        return len(found) == 1

    # -- live turn observation -------------------------------------------------

    def _anchor_issue(self) -> str:
        """The issue carrying the anchor/approval journals (the F1 authority split)."""
        return self.request.effective_anchor_issue

    def _ledger(self) -> HerdrDeliveryLedger:
        return self.ledger if self.ledger is not None else HerdrDeliveryLedger()

    def _anchor_marker(self, kind: str, worker_provider: str) -> str:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
            build_marker,
        )

        return build_marker(
            RedmineAnchor(
                issue=self._anchor_issue(),
                journal=_norm(self.request.resume_anchor_journal),
            ),
            _norm(kind),
            worker_provider,
        )

    def _anchor_delivery_record(self, worker_provider: str):
        """The durable callback-outcome record for the anchor's delivery to the PINNED worker.

        Matched fail-closed on every axis: the exact anchor marker, ``source=redmine``, the
        exact issue / journal, ``receiver`` == the worker provider, ``target`` == the pinned
        (old) locator, ``status=sent`` with the accepted reason. ``None`` when no such record
        exists / the ledger is unreadable.

        TWO transport kinds qualify, because both are deliveries of the SAME durable anchor to
        the SAME exact worker: the anchor's own gate kind (the original forward) and
        :data:`_RESUME_TRANSPORT_KIND` (a previous guarded refresh's resume pointer). Accepting
        only the first would make a *second* refresh of a lane that failed the same way twice
        permanently ``turn_unconfirmed`` — which is the exactly-once gap #14661 was opened on,
        reappearing one round later. Both markers are anchor-pinned, so a neighbouring handoff
        can never match either.

        This SAME record is the turn-start authority's carrier (the #14203 j#87397 discipline):
        the observation is bound to the exact anchor marker + exact target, never a global
        timeline.
        """
        markers = [
            self._anchor_marker(_norm(self.request.resume_gate), worker_provider),
            self._anchor_marker(_RESUME_TRANSPORT_KIND, worker_provider),
        ]
        for marker in markers:
            try:
                records = self._ledger().records_for_marker(marker)
            except Exception:  # noqa: BLE001 - unreadable ledger => unconfirmed
                return None
            for rec in records:
                if (
                    _norm(rec.notification_marker) == marker
                    and _norm(rec.source) == "redmine"
                    and _norm(rec.issue_id) == self._anchor_issue()
                    and _norm(rec.journal_id) == _norm(self.request.resume_anchor_journal)
                    and _norm(rec.receiver) == worker_provider
                    and _norm(rec.target) == _norm(self.request.locator)
                    and _norm(rec.status) == "sent"
                    and _norm(rec.reason) == "ok"
                ):
                    return rec
        return None

    def _record_observed_turn_start(self, rec) -> bool:
        """Delegate to the shared generation-authority leaf (the #14203 module-health split).

        The pinned row revision is passed EXPLICITLY as this surface's ``worker_revision``
        (review j#92443 F1). The seam previously read ``request.gateway_revision`` through a
        defaulted attribute lookup, so a worker request — which has no such field — bound to
        ``""`` and could NEVER observe a turn start: the whole surface was inert in
        production while every fake-backed test stayed green.
        """
        return _gen_authority.record_observed_turn_start(
            rec, request=self.request, repo_root=self.repo_root,
            attestation_home=self.attestation_home,
            pin_revision=self.request.worker_revision,
            live_terminal_id=self._live_terminal_identity(),
        )

    def _record_generation_bound(self, rec) -> bool:
        return _gen_authority.record_generation_bound(
            rec, request=self.request, repo_root=self.repo_root,
            attestation_home=self.attestation_home,
            pin_revision=self.request.worker_revision,
            live_terminal_id=self._live_terminal_identity(),
        )

    def _live_terminal_identity(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            terminal_identity_of_live_slot,
        )

        try:
            return terminal_identity_of_live_slot(
                self.request.assigned_name, self.request.locator, self._rows()
            )
        except Exception:  # noqa: BLE001 - unreadable/ambiguous inventory fails closed
            return None

    def _anchor_bound(self, request: WorkerRefreshRequest) -> bool:
        """Is this observation bound to a resolvable EXACT durable anchor? (#14661)

        Positively true only when the anchor pointer names one exact Redmine journal under a
        closed resumable gate kind: a non-empty anchor issue, a NUMERIC anchor journal id (the
        ordered durable comparison below is meaningless otherwise), and a
        :data:`...gateway_turn_recovery.RESUMABLE_GATES` member. Given that, every fact this
        observation carries is anchor-pinned by construction — the delivery record is matched
        on a marker built from the anchor, and the progress re-read is ordered against the
        anchor journal id.

        "NUMERIC" is :func:`...marker_value_contract.is_journal_id`, not ``str.isdigit()``, which
        was the test here. The two disagree exactly where this classification matters: ``"²"`` is
        ``isdigit()`` and ``int()`` REFUSES it, so the ordered durable comparison this predicate
        exists to justify could not run — the reader below caught the ``ValueError`` and reported
        *unobservable* while this said the observation was anchor-bound (Redmine #14753). Same
        answer from the same predicate on both sides, so a bound anchor is one that can be
        compared.
        """
        if not _norm(self._anchor_issue()):
            return False
        if not is_journal_id(_norm(request.resume_anchor_journal)):
            return False
        return _norm(request.resume_gate) in RESUMABLE_GATES

    def _lane_generation_bound(self, request: WorkerRefreshRequest) -> bool:
        """Is the pinned LANE GENERATION joined to the live lane lifecycle right now? (#14661)

        Projected from the single :meth:`lane_authority_reason` evaluator: the generation is
        bound unless that evaluator reports one of the tokens that mean it could not be joined
        (unpinned / unreadable / absent / moved / unknown). A worktree or branch failure is a
        different axis and leaves the generation binding intact — that axis blocks the refresh
        through ``launch_authority_unavailable``, not by unbinding the classification.
        """
        return _norm(self.lane_authority_reason(request)) not in _GENERATION_UNBOUND_REASONS

    def observe_turn(self, request: WorkerRefreshRequest) -> WorkerTurnObservation:
        worker_provider, _gateway_provider = self._providers()
        if not worker_provider:
            return WorkerTurnObservation()  # unresolvable binding => unobservable
        anchor_bound = self._anchor_bound(request)
        record = self._anchor_delivery_record(worker_provider) if anchor_bound else None
        delivery_confirmed = record is not None and self._record_generation_bound(record)
        turn_started = record is not None and self._record_observed_turn_start(record)
        row = self._pinned_row(request)
        settled = row is not None and _row_runtime_state(row) in (
            RUNTIME_TURN_ENDED, RUNTIME_AWAITING_INPUT,
        )
        landed, absent, fresh = (
            self._progress_facts(request) if anchor_bound else (False, False, False)
        )
        return WorkerTurnObservation(
            delivery_confirmed=delivery_confirmed,
            turn_started=turn_started,
            settled_turn_ended=settled,
            expected_gate_landed=landed,
            expected_gate_absent=absent,
            durable_source_fresh=fresh,
            reason_token=request.reason_token,
            anchor_bound=anchor_bound,
            lane_generation_bound=self._lane_generation_bound(request),
            participant_revision_bound=self._participant_revision_matches(request),
        )

    def _progress_facts(self, request: WorkerRefreshRequest) -> tuple[bool, bool, bool]:
        """(landed, absent, fresh) — delegated to the durable-read leaf (module-health split)."""
        return worker_progress_facts(
            request, journal_reader=self.journal_reader,
            journal_reader_fresh=self.journal_reader_fresh,
        )

    @staticmethod
    def _notes_carry_worker_progress(request: WorkerRefreshRequest, notes: str) -> bool:
        """Delegated to the durable-read leaf (module-health split)."""
        return notes_carry_worker_progress(request, notes)

    # -- exactly-once anchor resume (the governed rail + the REAL ledger oracle) ---

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

    def _recovery_delivery_service(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
            LiveRecoveryAnchorDeliveryService,
        )

        return LiveRecoveryAnchorDeliveryService(
            repo_root=self.repo_root,
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
            attestation_home=self.attestation_home,
        )

    def approval_verified(self, request: WorkerRefreshRequest) -> bool:
        """A FRESH durable read proving the pinned journal approves THIS exact action.

        (Review j#92443 F2.) Every axis is a positive fact and every failure is fail-closed:

        - a durable reader must be wired AND declare itself FRESH (a snapshot re-read cannot
          establish an authority for a destructive action — the #13889 discipline);
        - the read must complete;
        - the ANCHOR issue must actually contain a journal whose id is exactly the pinned
          approval journal (a fabricated / mistyped id resolves to nothing);
        - that journal's notes must name :attr:`WorkerRefreshRequest.holder` — the single
          token carrying the exact action id AND the approved generation. Because the action
          id is derived from lane / role / provider / assigned name / locator / row revision,
          naming it transitively binds the approval to every participant pin, so an approval
          written for a different worker, a different generation, or a different round can
          never authorize this close.

        Verification is delegated to :func:`...worker_refresh_approval.verify_worker_refresh_approval`,
        which copies the repo's already-hardened composer-discard approval shape: the journal
        must exist uniquely, carry exactly ONE canonical structured approval marker of this
        surface's gate, and match every expected field by exact equality including a positive
        ``decision`` and the exact ``action_digest``.

        This replaces an R2 implementation that asked whether a token appeared anywhere in the
        notes (review j#92487 F1). That admitted a negation, a quoted retry command, a log
        line, and a ``:g30`` approval standing in for ``:g3`` — prose containment is not a
        decision, and a substring is not a field.
        """
        reader = self.journal_reader
        if reader is None or not self.journal_reader_fresh:
            return False
        # The expected issuer comes from the DURABLE RECORD, never from the caller (#14661
        # j#92494): the actor asking for a destructive close must not be able to name its own
        # approver. Unresolvable => not approved.
        try:
            entries = reader(request.effective_anchor_issue)
        except Exception:  # noqa: BLE001 - unreadable durable source => never approved
            return False
        try:
            found = [
                e for e in entries
                if _norm(getattr(e, "journal_id", "")) == _norm(request.journal)
            ]
            verify_worker_refresh_approval(
                list(entries),
                journal=request.journal,
                issuer=self._resolved_issuer(found[0]) if len(found) == 1 else None,
                issue=request.issue,
                lane=request.lane,
                action_id=request.action_id,
                action_generation=request.action_generation,
                lane_revision=request.lane_revision,
                lane_generation=request.lane_generation,
                anchor_issue=request.effective_anchor_issue,
                resume_anchor_journal=request.resume_anchor_journal,
                resume_gate=request.resume_gate,
            )
        except WorkerRefreshApprovalError:
            return False
        except Exception:  # noqa: BLE001 - a malformed history is never an approval
            return False
        return True

    def _resolved_issuer(self, entry) -> object:
        """The approval journal's writer, resolved to an ANCHORED authority. (fail-closed)

        Uses the repo's existing issuer-resolution policy (#14661 j#92601 F1) rather than a
        model of this module's own: a role resolved from the record's canonical gate structure
        plus the committed policy pointer, carrying the durable anchor it was resolved from.
        An earlier revision compared the journal's author id to the issue's author id, which on
        a single-account workspace every journal satisfies.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            ResolvedIssuer,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            resolve_journal_issuer,
        )

        resolver = self.issuer_resolver
        if resolver is not None:
            try:
                return resolver(entry)
            except Exception:  # noqa: BLE001 - an unreadable authority is never an authority
                return ResolvedIssuer()
        try:
            return resolve_journal_issuer(
                notes=str(getattr(entry, "notes", "") or ""),
                journal_id=str(getattr(entry, "journal_id", "") or ""),
                policy_pointer=self._issuer_policy_pointer(),
            )
        except Exception:  # noqa: BLE001
            return ResolvedIssuer()

    def _issuer_policy_pointer(self) -> str:
        """The committed-config blob the issuer resolution is anchored to, or ``""``.

        ``config_policy_pointer`` names the exact committed blob of the provider-binding config
        the gate->role contract is bound to, so the resolved role carries a record anyone can
        re-check (Redmine #14661 Design Answer j#92641). Read from git as a tracked-object
        lookup — never the working-tree file, whose content an actor requesting a destructive
        action could edit. An unresolvable blob yields ``""``, which leaves every resolution
        unanchored and therefore refuses.
        """
        import subprocess

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
            CONFIG_RELPATH,
            config_policy_pointer,
        )

        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", f"HEAD:{CONFIG_RELPATH}"],
                text=True, capture_output=True,
            )
        except OSError:
            return ""
        if result.returncode != 0:
            return ""
        blob = result.stdout.strip()
        return config_policy_pointer(blob) if blob else ""

    def resume_rail_ready(self, request: WorkerRefreshRequest) -> bool:
        """Can the ACTION-BOUND resume rail deliver from THIS context? (read-only, pre-close)

        Verified BEFORE the destructive close so a context that cannot resume is a typed
        up-front refusal, never a post-close ``stopped`` discovery.

        Only the action-bound delivery service counts (review j#92601 F4). The governed
        ``handoff send`` CLI resolves in more contexts, but it cannot carry the replacement
        action into the transport, so admitting a close on its availability would authorise a
        close whose resume can be delivered to a recycled slot. Availability of a weaker rail is
        not availability.
        """
        return self._recovery_delivery_service_ready()

    def _delivery_request(
        self, continuation: ContinuationPointer, locator: str, worker_provider: str
    ):
        """The action-bound delivery request for the fresh slot, or ``None``. (read-only)"""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
            KIND_REPLY,
            RecoveryAnchorDeliveryRequest,
        )

        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            rows = self._rows()
        except Exception:  # noqa: BLE001 - unreadable identity / inventory
            return None
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(self.request.assigned_name)
            and _agent_locator(row) == locator
        ]
        if len(matches) != 1:
            return None
        revision = _row_revision(matches[0])
        if not revision:
            return None
        try:
            return RecoveryAnchorDeliveryRequest(
                issue=_norm(continuation.issue_id),
                journal=_norm(continuation.journal_id),
                kind=KIND_REPLY,
                workspace_id=workspace_id,
                lane_id=_norm_lane(self.request.lane),
                provider=_norm(worker_provider),
                target_assigned_name=_norm(self.request.assigned_name),
                target_locator=_norm(locator),
                target_revision=revision,
                target_action_id=_norm(self.request.action_id),
            )
        except Exception:  # noqa: BLE001 - an invalid request is not a deliverable one
            return None

    def _fresh_slot_action_bound(
        self, continuation: ContinuationPointer, locator: str, worker_provider: str
    ) -> bool:
        """Is the FRESH slot's attestation bound to THIS replacement action? (fail-closed)

        Review j#92487 F3: the governed rail re-resolved the logical slot and sent, and the
        confirmation only fenced on the fresh attestation's ``observed_at`` — a seconds-precision
        timestamp that #14203 j#87445 already rejected as a generation identity (two launches in
        the same second share it). A slot recycled after the actuator's attestation therefore
        read as this refresh's fresh worker on both the send and the confirm edge.

        The one-shot rail never had that gap because it passes ``target_action_id`` and
        :class:`LiveRecoveryAnchorDeliveryService` verifies the terminal-bound v4 direct
        ``replacement_action_id`` equality. This routes the
        governed rail through that SAME public authority — ``preflight`` re-runs every
        read-only action gate without injecting or writing — instead of re-implementing the
        current-authority rule here and letting the two drift.
        """
        request = self._delivery_request(continuation, locator, worker_provider)
        if request is None:
            return False
        try:
            return bool(self._recovery_delivery_service().preflight(request).may_deliver)
        except Exception:  # noqa: BLE001 - an unverifiable binding is never a bound one
            return False

    def resume_once(self, continuation: ContinuationPointer) -> str:
        locator = self._fresh_worker_locator()
        if not locator or locator == _norm(self.request.locator):
            # No fresh worker resolved yet (or still the old locator) — never send blind.
            return DRAIN_SEND_ERROR
        worker_provider, _gateway = self._providers()
        if not worker_provider:
            return DRAIN_SEND_ERROR
        # The action binding is required on BOTH rails (j#92487 F3), verified immediately
        # before the send so a slot recycled after the actuator's attestation cannot receive
        # this resume.
        if not self._fresh_slot_action_bound(continuation, locator, worker_provider):
            return DRAIN_SEND_ERROR
        # The action-bound rail is the ONLY rail this destructive surface may resume on
        # (review j#92601 F4). The governed CLI cannot carry the target revision or the
        # replacement action into the transport — its argv is locator + lane — so a slot that
        # recycles between the last binding check and the injection receives the resume anyway.
        # An earlier revision kept it as a "fallback with a late re-join", but "just before
        # ``_drive_cli``" is not "just before transport", and keeping an unsafe rail available
        # is the wrong trade for a close/relaunch surface. Contexts where only the governed
        # sender resolves are refused UP FRONT by ``resume_rail_ready`` instead, so nothing is
        # ever stranded mid-transaction.
        if not self._recovery_delivery_service_ready():
            return DRAIN_SEND_ERROR
        return self._oneshot_resume(continuation, locator, worker_provider)

    def _recovery_delivery_service_ready(self) -> bool:
        """Can the action-bound one-shot delivery service run here? (read-only, fail-closed)"""
        try:
            return bool(self._recovery_delivery_service().ready())
        except Exception:  # noqa: BLE001 - an unavailable service is not a ready one
            return False

    def _oneshot_resume(
        self, continuation: ContinuationPointer, locator: str, worker_provider: str
    ) -> str:
        """Operator-capable guarded one-shot: sender-env independent, target-identity verified
        action-time, read-back before Enter, real ledger writer (the #14203 j#87370 F1 /
        j#85972 formalization of the owner-approved break-glass shape)."""
        request = self._delivery_request(continuation, locator, worker_provider)
        if request is None:
            return DRAIN_SEND_ERROR
        try:
            outcome = self._recovery_delivery_service().deliver(request)
        except Exception:  # noqa: BLE001 - an unknown fate is never a proven zero-send
            return DRAIN_SEND_ERROR
        if outcome.started:
            return DRAIN_SEND_OK
        # The delivery domain distinguishes ``zero_send`` (it proved nothing was transmitted)
        # from ``uncertain`` (it cannot tell). Collapsing both into the generic error threw that
        # away, and because the drain keeps a recorded attempt on error, a proven zero-send left
        # the transaction permanently unresumable (Redmine #14661 j#92601 F5). Carry the typed
        # fact across the boundary instead of re-deriving it.
        if bool(getattr(outcome, "zero_send", False)):
            return DRAIN_SEND_ZERO
        return DRAIN_SEND_ERROR

    def resume_confirmed(self, continuation: ContinuationPointer) -> bool:
        """CONFIRMED-landed on the exact FRESH worker (the #13806 R2-F3 oracle, adapted).

        Fail-closed on every axis: the exact reply-kind marker (anchor + worker provider),
        ``receiver`` == the worker provider, a fresh locator DISTINCT from the closed one,
        ``status=sent`` with the accepted reason, and recorded AFTER the fresh worker's startup
        attestation (the temporal fence against the pre-refresh delivery).
        """
        worker_provider, _gateway = self._providers()
        if not worker_provider:
            return False
        boundary = self._fresh_attestation_identity()
        if boundary is None or boundary.locator == _norm(self.request.locator):
            return False
        # The timestamp boundary below is necessary but NOT sufficient as a generation
        # identity (#14203 j#87445: two same-second launches share ``observed_at``). Require
        # the fresh slot to be attested for THIS replacement action as well, so a recycled
        # slot's delivery is never confirmed as this refresh's resume (j#92487 F3).
        if not self._fresh_slot_action_bound(continuation, boundary.locator, worker_provider):
            return False
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
            build_marker,
        )

        # The resume DELIVERY is a reply pointer at the anchor, so the confirmation marker is
        # the reply-kind marker — the anchor's own gate kind stays the continuation authority,
        # never the transport kind.
        marker = build_marker(
            RedmineAnchor(
                issue=_norm(continuation.issue_id), journal=_norm(continuation.journal_id)
            ),
            _RESUME_TRANSPORT_KIND,
            worker_provider,
        )
        try:
            records = self._ledger().records_for_marker(marker)
        except Exception:  # noqa: BLE001 - unreadable ledger => not confirmed
            return False
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
            _recorded_after,
        )

        for rec in records:
            if (
                _norm(rec.notification_marker) == marker
                and _norm(rec.source) == "redmine"
                and _norm(rec.issue_id) == _norm(continuation.issue_id)
                and _norm(rec.journal_id) == _norm(continuation.journal_id)
                and _norm(rec.receiver) == worker_provider
                and _norm(rec.provider) in ("", worker_provider)
                and _norm(rec.backend) == "herdr"
                and _norm(rec.target) == boundary.locator
                and _norm(rec.status) == "sent"
                and _norm(rec.reason) == "ok"
                and _recorded_after(rec.recorded_at, boundary.observed_at)
                and boundary.matches_delivery(rec)
            ):
                return True
        return False

    def _fresh_attestation_identity(self):
        try:
            from .herdr_live_attestation_time import fresh_attestation_identity
            rows = self._rows()
            return fresh_attestation_identity(
                home=self.attestation_home, rows=rows,
                assigned_name=self.request.assigned_name,
                workspace_id=repo_scope_workspace_id(self.repo_root),
                role=self.request.provider, lane=self.request.lane,
            )
        except Exception:  # noqa: BLE001 - unreadable authority fails closed
            return None


__all__ = (
    "CLOSE_REFUSED_PROGRESS_MOVED",
    "LiveWorkerRefreshOps",
    "SettledCloseBoundaryPort",
    "port_pin_request",
)
