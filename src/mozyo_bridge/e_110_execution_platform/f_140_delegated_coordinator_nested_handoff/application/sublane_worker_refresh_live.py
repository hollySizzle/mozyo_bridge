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

import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
from mozyo_bridge.core.state.replacement_transaction import ContinuationPointer
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_PROGRESS_GATES,
    WorkerRefreshObservation,
    WorkerTurnObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
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
        )

    def _anchor_bound(self, request: WorkerRefreshRequest) -> bool:
        """Is this observation bound to a resolvable EXACT durable anchor? (#14661)

        Positively true only when the anchor pointer names one exact Redmine journal under a
        closed resumable gate kind: a non-empty anchor issue, a NUMERIC anchor journal id (the
        ordered durable comparison below is meaningless otherwise), and a
        :data:`...gateway_turn_recovery.RESUMABLE_GATES` member. Given that, every fact this
        observation carries is anchor-pinned by construction — the delivery record is matched
        on a marker built from the anchor, and the progress re-read is ordered against the
        anchor journal id.
        """
        if not _norm(self._anchor_issue()):
            return False
        raw = _norm(request.resume_anchor_journal)
        if not raw.isdigit():
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
        delivery_confirmed = record is not None
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
        """(landed, absent, fresh): the anchored + ordered fresh durable re-read (#13889).

        Worker progress is a structured gate marker of a :data:`WORKER_PROGRESS_GATES` kind on
        a journal STRICTLY AFTER the anchor (ordered on durable journal ids, never wall-clock),
        in the anchor issue. No worker gate marker carries a causal back-pointer to the request
        it answers, so the causal link is ordering + lane binding, resolved in the SAFE
        direction:

        - a marker carrying the #14219 lane envelope must match BOTH the pinned lane and the
          pinned lane generation — a different lane's or a superseded generation's gate is not
          this turn's progress;
        - a marker WITHOUT an envelope still counts as progress. Unknown provenance classifies
          ``turn_productive``, which REFUSES the refresh: the only mistake this direction can
          make is declining to close a worker, while the reverse would close one that had in
          fact delivered its gate.

        No reader / an unreadable read / a non-fresh (snapshot) reader leaves all facts
        ``False`` — unobservable, never "absent".
        """
        reader = self.journal_reader
        if reader is None or not self.journal_reader_fresh:
            return False, False, False
        try:
            anchor = int(_norm(request.resume_anchor_journal))
        except (TypeError, ValueError):
            return False, False, False
        try:
            entries = reader(request.effective_anchor_issue)
        except Exception:  # noqa: BLE001 - unreadable durable source => unobservable
            return False, False, False
        for entry in entries:
            try:
                jid = int(_norm(getattr(entry, "journal_id", "")))
            except (TypeError, ValueError):
                continue
            if jid <= anchor:
                continue
            notes = str(getattr(entry, "notes", "") or "")
            if self._notes_carry_worker_progress(request, notes):
                return True, False, True
        return False, True, True

    @staticmethod
    def _notes_carry_worker_progress(request: WorkerRefreshRequest, notes: str) -> bool:
        """Does this journal note carry a worker-progress gate marker for this lane? (pure)"""
        try:
            markers = marker_fields_in_note(notes)
        except Exception:  # noqa: BLE001 - an unparsable note carries no structured marker
            return False
        for channel, fields in markers:
            if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
                continue
            if _norm(fields.get("gate")) not in WORKER_PROGRESS_GATES:
                continue
            lane = fields.get("lane")
            generation = fields.get("lane_generation")
            if lane is None or generation is None:
                # Unenveloped — or only PARTIALLY enveloped, which the canonical producer
                # cannot emit at all (the lane envelope is all-or-none). Either way the lane
                # provenance is unreadable, so it counts as progress: the safe direction is
                # ``turn_productive`` (refuse the refresh). Requiring an exact match here
                # would silently skip a half-enveloped worker gate and admit a close of the
                # worker that had in fact delivered it.
                return True
            if (
                _norm_lane(lane) == _norm_lane(request.lane)
                and _norm(generation) == _norm(request.lane_generation)
            ):
                return True
        return False

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

    def _governed_sender_resolves(self) -> bool:
        """Does the GOVERNED send rail resolve from THIS context? (read-only)

        Verified with the SAME authority the real send uses — :func:`resolve_sender_identity`
        over this process env + the repo anchor workspace — never a bare env-presence check
        (the #14203 j#87370 F1 lesson).
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
            resolve_sender_identity,
        )

        try:
            anchor_ws = repo_scope_workspace_id(self.repo_root)
        except Exception:  # noqa: BLE001 - unreadable anchor => the resolver fails closed
            anchor_ws = None
        try:
            return bool(resolve_sender_identity(self.env, anchor_workspace_id=anchor_ws).ok)
        except Exception:  # noqa: BLE001 - a resolver error is a non-resolving context
            return False

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

        The check deliberately introduces NO new marker grammar: it requires the approval to
        quote a token this module already derives, rather than inventing an approval
        vocabulary a leaf task has no standing to define.
        """
        reader = self.journal_reader
        if reader is None or not self.journal_reader_fresh:
            return False
        wanted_journal = _norm(request.journal)
        token = _norm(request.holder)
        # A holder over an empty action id / generation would be a token that matches loosely;
        # the caller validates both before reaching here, but this seam refuses independently.
        if not wanted_journal or not _norm(request.action_id) or not token:
            return False
        try:
            entries = reader(request.effective_anchor_issue)
        except Exception:  # noqa: BLE001 - unreadable durable source => never approved
            return False
        for entry in entries:
            if _norm(getattr(entry, "journal_id", "")) != wanted_journal:
                continue
            notes = str(getattr(entry, "notes", "") or "")
            return token in notes
        return False

    def resume_rail_ready(self, request: WorkerRefreshRequest) -> bool:
        """Pre-close resume-rail capability. (read-only)

        True when EITHER rail can deliver from THIS context: the governed send rail (verified
        through the REAL sender-identity resolver, same authority as the send), or the
        operator-capable guarded one-shot rail (sender-env independent; needs only a reachable
        herdr transport, with the exact target identity verified action-time at the send).
        Fail-closed when neither resolves.
        """
        if self._governed_sender_resolves():
            return True
        return self._recovery_delivery_service().ready()

    def _resume_argv(self, continuation: ContinuationPointer, locator: str) -> list[str]:
        """The recovery-family resume argv: ONE ``handoff send --kind reply`` pointer at the
        EXISTING anchor (the #14203 j#84223 owner-approved resume shape — the anchor journal is
        the truth, the notification a pointer; no gate is regenerated). Same governed rail the
        callback-recovery family drives, lane- and target-pinned."""
        worker_provider, _gateway = self._providers()
        return [
            "handoff", "send",
            "--to", worker_provider,
            "--source", "redmine",
            "--issue", _norm(continuation.issue_id),
            "--journal", _norm(continuation.journal_id),
            "--kind", _RESUME_TRANSPORT_KIND,
            "--target", locator,
            "--target-repo", str(self.repo_root),
            "--target-lane", _norm(self.request.lane),
            "--mode", "queue-enter",
        ]

    def resume_once(self, continuation: ContinuationPointer) -> str:
        locator = self._fresh_worker_locator()
        if not locator or locator == _norm(self.request.locator):
            # No fresh worker resolved yet (or still the old locator) — never send blind.
            return DRAIN_SEND_ERROR
        worker_provider, _gateway = self._providers()
        if not worker_provider:
            return DRAIN_SEND_ERROR
        if self._governed_sender_resolves():
            try:
                rc = self._drive_cli(self._resume_argv(continuation, locator))
            except Exception:  # noqa: BLE001 - a failed drive is a failed send
                return DRAIN_SEND_ERROR
            return DRAIN_SEND_OK if rc == 0 else DRAIN_SEND_ERROR
        return self._oneshot_resume(continuation, locator, worker_provider)

    def _oneshot_resume(
        self, continuation: ContinuationPointer, locator: str, worker_provider: str
    ) -> str:
        """Operator-capable guarded one-shot: sender-env independent, target-identity verified
        action-time, read-back before Enter, real ledger writer (the #14203 j#87370 F1 /
        j#85972 formalization of the owner-approved break-glass shape)."""
        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            rows = self._rows()
        except Exception:  # noqa: BLE001
            return DRAIN_SEND_ERROR
        matches = [
            candidate
            for candidate in rows
            if (
                isinstance(candidate, Mapping)
                and _norm(candidate.get(AGENT_KEY_NAME)) == _norm(self.request.assigned_name)
                and _agent_locator(candidate) == locator
            )
        ]
        if len(matches) != 1:
            return DRAIN_SEND_ERROR
        revision = _row_revision(matches[0])
        if not revision:
            return DRAIN_SEND_ERROR
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_anchor_delivery import (  # noqa: E501
            KIND_REPLY,
            RecoveryAnchorDeliveryRequest,
        )

        try:
            outcome = self._recovery_delivery_service().deliver(
                RecoveryAnchorDeliveryRequest(
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
            )
        except Exception:  # noqa: BLE001 - invalid request/service failure is zero-success
            return DRAIN_SEND_ERROR
        return DRAIN_SEND_OK if outcome.started else DRAIN_SEND_ERROR

    def _drive_cli(self, argv: list[str]) -> int:
        """Parse + run through the composed CLI so the resume is byte-for-byte the governed
        ``handoff send`` an operator would run."""
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = build_parser().parse_args(argv)
        args = normalize_paths(args)
        with contextlib.redirect_stdout(sys.stderr):
            return int(args.func(args))

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
        fresh_observed_at = self._fresh_attestation_observed_at()
        if not fresh_observed_at:
            return False
        fresh_locator = self._fresh_worker_locator()
        if not fresh_locator or fresh_locator == _norm(self.request.locator):
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
                and _norm(rec.target) == fresh_locator
                and _norm(rec.status) == "sent"
                and _norm(rec.reason) == "ok"
                and _recorded_after(rec.recorded_at, fresh_observed_at)
            ):
                return True
        return False

    def _fresh_attestation_observed_at(self) -> str:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        try:
            record = HerdrIdentityAttestationStore(home=self.attestation_home).read(
                _norm(self.request.assigned_name)
            )
        except Exception:  # noqa: BLE001 - unreadable attestation => no boundary
            return ""
        if record is None:
            return ""
        return _norm(getattr(record, "observed_at", ""))


__all__ = (
    "LiveWorkerRefreshOps",
    "port_pin_request",
)
