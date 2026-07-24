"""Live adapters for the guarded gateway refresh (Redmine #14203 review j#87356 F1).

The public ``sublane recover-gateway`` command is only useful if it actually observes the
live inventory / durable sources and drives the real close → same-slot launch → attestation →
callback recovery (the #13806 R1-F1 lesson, re-learned here: a fail-closed staged seam leaves
the product gap open). This module wires :class:`...sublane_gateway_recovery.GatewayRefreshUseCase`
to the real runtime by REUSING the proven #13806 live adapters:

* the exact-generation close / relaunch / attestation port is the #13806
  :class:`...sublane_stale_worker_recovery_live.LiveRecoveryActuatorPort` itself, constructed
  over a field-adapted pin request (the port pins identity + lane evidence; it carries no
  worker-vs-gateway semantics — the role protection lives in the preflight decision);
* the lane-authority / name-liveness probes delegate to
  :class:`...sublane_stale_worker_recovery_live.LiveStaleWorkerRecoveryOps` (same axes:
  lifecycle ``(revision, generation)``, worktree token, branch, slot liveness);
* the resume delivers the EXISTING durable anchor to the FRESH gateway through the governed
  ``handoff send`` rail (the coordinator→lane-gateway leg shape:
  :meth:`...sublane_actuator_herdr_ops.HerdrSublaneActuatorOps.dispatch_argv`) and confirms
  landing against the REAL herdr delivery ledger — never a bare send, never a self-authored
  ``sent`` record, never a regenerated gate.

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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
    read_render_ghost_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E501
    GatewayRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
    LiveStaleWorkerRecoveryOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    GatewayRefreshObservation,
    GatewayTurnObservation,
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

_LANE_ISSUE_RE = __import__("re").compile(r"^issue_?(\d+)(?:_|$)")


def _lane_owning_issue(lane_id: object) -> str:
    """The lane label's owning issue id, EXACT-parsed (review j#87364 F3). (pure)

    ``issue_13490_single_entry_e2e_r1`` -> ``13490``. The id component is bounded by a
    ``_`` separator (or end), so a prefix (``1349``) can never match — the destructive
    authorization boundary compares parsed-id equality, never substring containment.
    An unparsable label yields ``""`` (never equal to a real issue; fail-closed).
    """
    m = _LANE_ISSUE_RE.match(_norm_lane(lane_id))
    return m.group(1) if m else ""


def port_pin_request(request: GatewayRefreshRequest) -> RecoveryRequest:
    """Adapt the gateway refresh pin to the #13806 port/probe request shape. (pure)

    The live actuation port and the lane-authority / name-liveness probes consume only the
    identity pin (lane / role / provider / assigned_name / locator) + the revision evidence
    (row revision, lane lifecycle ``(revision, generation)``) — none of the gate semantics.
    The gateway's live row revision maps onto the port's ``worker_revision`` field (the same
    live-inventory-row authority, #13806 revision-authority split); the worker-vs-gateway
    protection is NOT this adapter's job — it is the preflight decision's ordered gate.
    """
    return RecoveryRequest(
        issue=request.issue, lane=request.lane, role=request.role,
        provider=request.provider, assigned_name=request.assigned_name,
        locator=request.locator, journal=request.journal,
        action_id=request.action_id, action_generation=request.action_generation,
        worker_revision=request.gateway_revision,
        lane_revision=request.lane_revision, lane_generation=request.lane_generation,
    )


def _row_runtime_state(row: Mapping[str, object]) -> str:
    for key in _STATUS_KEYS:
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            return map_agent_status(raw)
    return map_agent_status(None)


@dataclass
class LiveGatewayRecoveryOps:
    """Live observe + exactly-once anchor resume (:class:`GatewayRecoveryOps`).

    ``observe_turn`` classifies the delivered callback's provider turn from the REAL herdr
    delivery ledger (callback outcome), the OTel activity timeline (turn-start evidence), and
    a FRESH durable journal read (the expected-gate authority). ``observe_target`` classifies
    the exact pinned gateway from the live herdr inventory + render observation. The resume
    delivers the EXISTING anchor to the fresh gateway via the governed ``handoff send`` rail
    and confirms landing against the durable ledger, never blind-resending.
    """

    repo_root: Path
    request: GatewayRefreshRequest
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS
    ledger: Optional[HerdrDeliveryLedger] = None
    #: Isolated store homes for tests; ``None`` = the real state homes.
    attestation_home: Optional[Path] = None
    lifecycle_home: Optional[Path] = None
    otel_store: Optional[object] = None
    #: A FRESH durable journal reader: ``journal_reader(issue) -> Sequence[entry]`` where each
    #: entry carries ``journal_id`` + ``notes`` (the RedmineJournalSource shape). ``None`` =
    #: no live durable source is wired in this environment — the turn observation then leaves
    #: the absence facts ``False`` (classifies ``turn_unobservable``, never actuated).
    journal_reader: Optional[object] = None
    #: Marks the ``journal_reader`` as a FRESH (non-snapshot) source (#13889: only a source
    #: declaring freshness may back the absence-of-gate fact).
    journal_reader_fresh: bool = False

    # -- delegation to the proven #13806 probes --------------------------------

    def _delegate(self) -> LiveStaleWorkerRecoveryOps:
        return LiveStaleWorkerRecoveryOps(
            repo_root=self.repo_root, request=port_pin_request(self.request),
            env=self.env, runner=self.runner, timeout=self.timeout, ledger=self.ledger,
            attestation_home=self.attestation_home, lifecycle_home=self.lifecycle_home,
        )

    def resume_lane_authority(self, request: GatewayRefreshRequest) -> bool:
        return self._delegate().resume_lane_authority(port_pin_request(request))

    def gateway_name_free_of_live_process(self, request: GatewayRefreshRequest) -> bool:
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

    def observe_target(self, request: GatewayRefreshRequest) -> GatewayRefreshObservation:
        try:
            workspace_id = repo_scope_workspace_id(self.repo_root)
            rows = list(self._rows())
        except Exception:  # noqa: BLE001 - unreadable inventory => identity_unknown
            return GatewayRefreshObservation()
        matches = [
            row for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.assigned_name)
        ]
        exact = [r for r in matches if _agent_locator(r) == _norm(request.locator)]
        if len(exact) != 1 or len(matches) != 1:
            return GatewayRefreshObservation()  # ambiguous / absent => identity_unknown
        row = exact[0]
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            return GatewayRefreshObservation()
        identity = decoded.identity
        identity_resolved = (
            identity.workspace_id == workspace_id
            and _norm_lane(identity.lane_id) == _norm_lane(request.lane)
            and identity.role == _norm(request.role)
        )
        if not identity_resolved:
            return GatewayRefreshObservation()
        # The lane IMPLEMENTATION_GATEWAY (the recover-stale mirror): positively the
        # configured gateway provider on EVERY axis — the live slot's role AND the approval's
        # own role/provider pins must all equal the gateway provider and none the worker
        # provider — and never the default coordinator lane. Fail-closed on an unresolvable
        # binding.
        worker_provider, gateway_provider = self._providers()
        is_gateway = bool(gateway_provider) and (
            _norm_lane(identity.lane_id) != "default"
            and identity.role == gateway_provider
            and identity.role != worker_provider
            and _norm(request.role) == gateway_provider
            and _norm(request.provider) == gateway_provider
            and _norm(request.provider) != worker_provider
        )
        issue_lane_matches = _lane_owning_issue(identity.lane_id) == _norm(request.issue)
        revision_raw = row.get("revision")
        row_revision = _norm(revision_raw) if not isinstance(revision_raw, bool) else ""
        # Review j#87364 F5: the pinned gateway inventory row revision is a REQUIRED exact
        # authority — an empty pin never matches (fail-closed), so a destructive refresh can
        # never ride an unpinned generation.
        generation_matches = bool(row_revision) and (
            row_revision == _norm(request.gateway_revision)
        )
        runtime_state = _row_runtime_state(row)
        settled_idle = runtime_state in (RUNTIME_TURN_ENDED, RUNTIME_AWAITING_INPUT)
        composer_clear = self._composer_clear(request)
        resume_anchor_present = bool(
            _norm(request.resume_anchor_journal) and _norm(request.resume_gate)
        )
        worker_distinct = self._worker_distinct_preserved(rows, request, worker_provider)
        return GatewayRefreshObservation(
            identity_resolved=identity_resolved,
            is_lane_implementation_gateway=is_gateway,
            issue_lane_matches=issue_lane_matches,
            generation_matches=generation_matches,
            settled_idle=settled_idle,
            composer_clear=composer_clear,
            resume_anchor_present=resume_anchor_present,
            worker_distinct_preserved=worker_distinct,
            no_authority_conflict=True,  # a competing txn is caught by the store's CAS
        )

    def _composer_clear(self, request: GatewayRefreshRequest) -> bool:
        """No REAL unsent composer input at the gateway. (fail-closed)

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

    @staticmethod
    def _worker_distinct_preserved(
        rows: Sequence[Mapping[str, object]],
        request: GatewayRefreshRequest,
        worker_provider: str,
    ) -> bool:
        """The lane's WORKER slot is positively a LIVE, DIFFERENT slot than the close target."""
        if not worker_provider:
            return False
        lane = _norm_lane(request.lane)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decoded.ok or decoded.identity is None:
                continue
            identity = decoded.identity
            if (
                _norm_lane(identity.lane_id) == lane
                and identity.role == worker_provider
                and _agent_locator(row) != _norm(request.locator)
                and classify_named_slot(row) == SLOT_LIVE
            ):
                return True
        return False

    # -- live turn observation -------------------------------------------------

    def _anchor_issue(self) -> str:
        """The issue carrying the anchor/approval journals (F1 authority split)."""
        return self.request.effective_anchor_issue

    def _ledger(self) -> HerdrDeliveryLedger:
        return self.ledger if self.ledger is not None else HerdrDeliveryLedger()

    def _anchor_marker(self, gateway_provider: str) -> str:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
            build_marker,
        )

        return build_marker(
            RedmineAnchor(
                issue=self._anchor_issue(),
                journal=_norm(self.request.resume_anchor_journal),
            ),
            _norm(self.request.resume_gate),
            gateway_provider,
        )

    def _delivery_recorded_at(self, gateway_provider: str) -> str:
        """The durable callback-outcome boundary: the anchor's confirmed delivery to the
        pinned (old) gateway locator — ``status=sent`` with an accepted reason. Empty when no
        such record exists / the ledger is unreadable (fail-closed)."""
        marker = self._anchor_marker(gateway_provider)
        try:
            records = self._ledger().records_for_marker(marker)
        except Exception:  # noqa: BLE001 - unreadable ledger => unconfirmed
            return ""
        for rec in records:
            if (
                _norm(rec.notification_marker) == marker
                and _norm(rec.source) == "redmine"
                and _norm(rec.issue_id) == self._anchor_issue()
                and _norm(rec.journal_id) == _norm(self.request.resume_anchor_journal)
                and _norm(rec.receiver) == gateway_provider
                and _norm(rec.target) == _norm(self.request.locator)
                and _norm(rec.status) == "sent"
                and _norm(rec.reason) == "ok"
            ):
                return str(rec.recorded_at or "")
        return ""

    def _turn_started_after(self, boundary: str, gateway_provider: str) -> bool:
        """OTel turn-start evidence: an activity event from the gateway provider in THIS lane
        worktree strictly after the delivery boundary. Best-effort telemetry — a quiet source
        leaves this ``False`` (classifies unconfirmed, never failed). Fail-closed."""
        if not boundary:
            return False
        store = self.otel_store
        if store is None:
            try:
                from mozyo_bridge.core.state.otel_store import OtelEventStore

                store = OtelEventStore()
            except Exception:  # noqa: BLE001 - no telemetry store => unconfirmed
                return False
        try:
            rows = store.query_events(since=boundary)
        except Exception:  # noqa: BLE001 - unreadable telemetry => unconfirmed
            return False
        lane_cwd = str(self.repo_root)
        for _row_id, event in rows:
            try:
                service = _norm(event.service_name)
                cwd = str(event.cwd or "")
            except Exception:  # noqa: BLE001 - a malformed event never confirms
                continue
            if cwd == lane_cwd and gateway_provider and gateway_provider in service:
                return True
        return False

    def observe_turn(self, request: GatewayRefreshRequest) -> GatewayTurnObservation:
        _worker_provider, gateway_provider = self._providers()
        if not gateway_provider:
            return GatewayTurnObservation()  # unresolvable binding => unobservable
        boundary = self._delivery_recorded_at(gateway_provider)
        delivery_confirmed = bool(boundary)
        turn_started = self._turn_started_after(boundary, gateway_provider)
        settled = False
        try:
            rows = self._rows()
            for row in rows:
                if (
                    isinstance(row, Mapping)
                    and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.assigned_name)
                    and _agent_locator(row) == _norm(request.locator)
                ):
                    settled = _row_runtime_state(row) in (
                        RUNTIME_TURN_ENDED, RUNTIME_AWAITING_INPUT,
                    )
                    break
        except Exception:  # noqa: BLE001 - unreadable inventory => not settled (fail-closed)
            settled = False
        landed, absent, fresh = self._expected_gate_facts(request)
        return GatewayTurnObservation(
            delivery_confirmed=delivery_confirmed,
            turn_started=turn_started,
            settled_turn_ended=settled,
            expected_gate_landed=landed,
            expected_gate_absent=absent,
            durable_source_fresh=fresh,
            reason_token=request.reason_token,
        )

    def _expected_gate_facts(
        self, request: GatewayRefreshRequest
    ) -> tuple[bool, bool, bool]:
        """(landed, absent, fresh): the anchored + ordered fresh durable re-read (#13889).

        A qualifying gate is ONLY one causally linked to the anchor (review j#87364 F4) —
        never "any workflow gate after it" (an unrelated concurrent journal must not read as
        the failed turn's response and suppress a needed recovery). The v1 closed causal
        contract: a ``review_request`` anchor's expected response is a ``review_result``
        marker carrying ``req=<anchor>``. Anchor kinds without a defined causal-response
        vocabulary classify UNOBSERVABLE (fail-closed in BOTH directions: no fabricated
        productivity, no fabricated failure). Comparison stays ordered on durable journal
        ids, never wall-clock. No reader / an unreadable read leaves all facts ``False``.
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
        if _norm(request.resume_gate) != "review_request":
            # No defined causal-response vocabulary for this anchor kind yet — unobservable
            # (fail-closed), never a guess in either direction.
            return False, False, False
        needle_gate = "gate=review_result"
        needle_req = f":req={anchor}"
        landed = False
        for entry in entries:
            try:
                jid = int(_norm(getattr(entry, "journal_id", "")))
                notes = str(getattr(entry, "notes", "") or "")
            except (TypeError, ValueError):
                continue
            if jid > anchor and needle_gate in notes and needle_req in notes:
                landed = True
                break
        return landed, not landed, True

    # -- exactly-once anchor resume (the governed rail + the REAL ledger oracle) ---

    def _fresh_gateway_locator(self) -> str:
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

    _SENDER_ENV_TRIAD = ("MOZYO_WORKSPACE_ID", "MOZYO_AGENT_ROLE", "MOZYO_LANE_ID")

    def resume_rail_ready(self, request: GatewayRefreshRequest) -> bool:
        """Pre-close resume-rail capability (review j#87364 F2). (read-only, fail-closed)

        The governed send rail requires the attested launch-time sender identity; a shell
        without it would only discover ``missing_sender_env`` AFTER the destructive close.
        Verified up front: every triad var must be present and non-empty in THIS process
        env. An operator runs the execute from an attested pane context (the documented
        contract) — env spoofing is never performed here.
        """
        return all(_norm(self.env.get(name, "")) for name in self._SENDER_ENV_TRIAD)

    def _resume_argv(self, continuation: ContinuationPointer, locator: str) -> list[str]:
        """The recovery-family resume argv: ONE ``handoff send --kind reply`` pointer at the
        EXISTING anchor (the #14203 j#84223 owner-approved resume shape — the anchor journal
        is the truth, the notification a pointer; no gate is regenerated). Same governed rail
        the callback-recovery family drives (:func:`...callback_sweep.build_recovery_sender`
        precedent), lane- and target-pinned."""
        _worker, gateway_provider = self._providers()
        return [
            "handoff", "send",
            "--to", gateway_provider,
            "--source", "redmine",
            "--issue", _norm(continuation.issue_id),
            "--journal", _norm(continuation.journal_id),
            "--kind", "reply",
            "--target", locator,
            "--target-repo", str(self.repo_root),
            "--target-lane", _norm(self.request.lane),
            "--mode", "queue-enter",
        ]

    def resume_once(self, continuation: ContinuationPointer) -> str:
        locator = self._fresh_gateway_locator()
        if not locator or locator == _norm(self.request.locator):
            # No fresh gateway resolved yet (or still the old locator) — never send blind.
            return DRAIN_SEND_ERROR
        _worker, gateway_provider = self._providers()
        if not gateway_provider:
            return DRAIN_SEND_ERROR
        try:
            rc = self._drive_cli(self._resume_argv(continuation, locator))
        except Exception:  # noqa: BLE001 - a failed drive is a failed send, ledger untouched
            return DRAIN_SEND_ERROR
        return DRAIN_SEND_OK if rc == 0 else DRAIN_SEND_ERROR

    def _drive_cli(self, argv: list[str]) -> int:
        """Parse + run through the composed CLI (the ``dispatch_argv`` precedent) so the
        resume is byte-for-byte the governed ``handoff send`` an operator would run."""
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = build_parser().parse_args(argv)
        args = normalize_paths(args)
        with contextlib.redirect_stdout(sys.stderr):
            return int(args.func(args))

    def resume_confirmed(self, continuation: ContinuationPointer) -> bool:
        """CONFIRMED-landed on the exact FRESH gateway (the #13806 R2-F3 oracle, adapted).

        Fail-closed on every axis: the exact marker (anchor + gate kind + gateway provider),
        receiver == gateway provider, a fresh locator DISTINCT from the closed one,
        ``status=sent`` with the accepted reason, and recorded AFTER the fresh gateway's
        startup attestation (the temporal fence against the pre-refresh delivery).
        """
        _worker, gateway_provider = self._providers()
        if not gateway_provider:
            return False
        fresh_observed_at = self._fresh_attestation_observed_at()
        if not fresh_observed_at:
            return False
        fresh_locator = self._fresh_gateway_locator()
        if not fresh_locator or fresh_locator == _norm(self.request.locator):
            return False
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
            build_marker,
        )

        # The resume DELIVERY is a reply pointer at the anchor (the j#84223 shape), so the
        # confirmation marker is the reply-kind marker — the anchor's own gate kind stays the
        # continuation authority, never the transport kind.
        marker = build_marker(
            RedmineAnchor(
                issue=_norm(continuation.issue_id), journal=_norm(continuation.journal_id)
            ),
            "reply",
            gateway_provider,
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
                and _norm(rec.receiver) == gateway_provider
                and _norm(rec.provider) in ("", gateway_provider)
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
    "LiveGatewayRecoveryOps",
    "port_pin_request",
)
