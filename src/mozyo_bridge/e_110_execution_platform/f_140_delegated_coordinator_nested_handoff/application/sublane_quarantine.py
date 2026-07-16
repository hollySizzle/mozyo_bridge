"""Owner-approved pending-composer quarantine and receiver replacement (#13763).

The use case never submits, clears, types, or sends a key.  Its only mutation is
closing one exact generation-pinned managed process and asking the existing
adopt-or-launch actuator to recreate that same lane/provider slot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
    ReleasePin,
    ReleasePinError,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    lifecycle_migration_payload,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (
    REPLACEMENT_NOT_REQUESTED,
    REPLACEMENT_PENDING,
    REPLACEMENT_REPLACED,
    REPLACEMENT_REQUESTED,
)
from mozyo_bridge.core.state.lane_replacement import LaneReplacementStore
from mozyo_bridge.core.state.lane_replacement_model import quarantine_action_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireClosePlan,
    execute_herdr_retire_close,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    pin_matched_close_plan,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    WorkflowProviderUnresolved,
    resolve_gateway_provider,
    resolve_worker_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    AMBIGUOUS,
    UNCORRELATED,
    PendingComposerClassification,
    PendingComposerSignal,
    classify_pending_composer,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _tab_id_of_row,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    _resolve_binary_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E501
    HerdrCliAgentStateReader,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    HerdrCliTransport,
    Runner,
)


_PROMPT_RE = re.compile(r"^\s*[›❯>]\s*(?P<body>.*)$")
_HANDOFF_MARKER_RE = re.compile(r"\[mozyo:handoff:[^\]]+\]")


@dataclass(frozen=True)
class ComposerObservation:
    """Transient adapter result; body text is intentionally absent."""

    readable: bool
    has_pending: Optional[bool]
    marker_ids: tuple[str, ...] = ()


def observe_composer_text(content: object) -> ComposerObservation:
    """Extract only pending/marker facts from the last rendered composer prompt.

    This is the only function that receives pane text.  It returns no body, hash,
    length, or excerpt, so callers cannot accidentally persist the input.
    """
    if not isinstance(content, str) or not content:
        return ComposerObservation(False, None)
    lines = content.splitlines()
    prompt_index = -1
    prompt_body = ""
    for index, line in enumerate(lines):
        match = _PROMPT_RE.match(line)
        if match:
            prompt_index = index
            prompt_body = match.group("body").strip()
    if prompt_index < 0:
        return ComposerObservation(False, None)
    if not prompt_body:
        return ComposerObservation(True, False)
    # A marker may be hard-wrapped mid-token. Collapse whitespace only inside the
    # current composer tail, never the whole pane/scrollback.
    composer_tail = "".join("".join(lines[prompt_index:]).split())
    markers = tuple(dict.fromkeys(_HANDOFF_MARKER_RE.findall(composer_tail)))
    return ComposerObservation(True, True, markers)


@dataclass(frozen=True)
class QuarantineRequest:
    issue: str
    lane: str
    journal: str
    role: str
    assigned_name: str
    locator: str
    action_generation: str
    approval_observed_at: str
    approved_revision: int


@dataclass(frozen=True)
class QuarantineInspection:
    workspace_id: str
    signal: PendingComposerSignal
    row_revision: int = -1
    attested_at: str = ""
    #: Is a managed process still live at the exact pinned ``(assigned name, locator)``?
    #: ``None`` means the inventory could not prove either way — never read as absence
    #: (R1-F1 j#78347): only a POSITIVE absence lets a redrive skip the owed close.
    receiver_present: Optional[bool] = None
    detail: str = ""

    @property
    def classification(self) -> PendingComposerClassification:
        return classify_pending_composer(self.signal)


@dataclass(frozen=True)
class CloseReceiverResult:
    closed: bool
    old_absent: bool = False
    detail: str = ""


@dataclass(frozen=True)
class FreshReceiverVerification:
    ok: bool
    locator: str = ""
    detail: str = ""


@dataclass(frozen=True)
class QuarantineOutcome:
    issue: str
    lane: str
    role: str
    action_generation: str
    classification: PendingComposerClassification
    executed: bool = False
    replacement_state: str = REPLACEMENT_NOT_REQUESTED
    closed_old_receiver: bool = False
    fresh_locator: str = ""
    detail: str = ""
    #: The shared-store schema migration this quarantine's replacement write gate performed, if
    #: any (Redmine #13844 R3-F2): the typed audit record so the migration is legible in JSON/text,
    #: not only the pre-migration stderr advisory.
    lifecycle_migration: Optional[dict[str, Any]] = None

    @property
    def is_blocked(self) -> bool:
        if not self.executed:
            return False
        return self.replacement_state != REPLACEMENT_REPLACED

    def as_payload(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "role": self.role,
            "action_generation": self.action_generation,
            **self.classification.as_payload(),
            "executed": self.executed,
            "replacement_state": self.replacement_state,
            "closed_old_receiver": self.closed_old_receiver,
            "fresh_locator": self.fresh_locator or None,
            "is_blocked": self.is_blocked,
            "detail": self.detail,
            "lifecycle_migration": self.lifecycle_migration,
        }


@runtime_checkable
class SublaneQuarantineOps(Protocol):
    def inspect(self, request: QuarantineRequest) -> QuarantineInspection: ...

    def close_receiver(
        self, request: QuarantineRequest, pin: ReleasePin
    ) -> CloseReceiverResult: ...

    def heal_receiver(self, request: QuarantineRequest) -> None: ...

    def verify_fresh_receiver(
        self, request: QuarantineRequest, *, fresh_after: str
    ) -> FreshReceiverVerification: ...


def _parse_time(value: object) -> Optional[datetime]:
    token = _norm(value)
    if not token:
        return None
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return None


def _not_after(candidate: str, boundary: str) -> bool:
    left = _parse_time(candidate)
    right = _parse_time(boundary)
    return left is not None and right is not None and left <= right


@dataclass
class SublaneQuarantineUseCase:
    ops: SublaneQuarantineOps
    store: LaneReplacementStore

    def _base_outcome(
        self,
        request: QuarantineRequest,
        classification: PendingComposerClassification,
        **changes: Any,
    ) -> QuarantineOutcome:
        # Redmine #13844 R5-F1: carry the schema migration THIS run performed into the outcome at
        # EVERY return, read from the OPERATION-SCOPED capture (reset at run() start, set only
        # after this run's migrating write) — NOT from the store's mutable / potentially reused
        # ``last_write_preparation``. So a preflight-only run, or a reused store whose earlier run
        # migrated, reports ``None`` here (no side effect fabricated); a run that actually migrated
        # keeps it across its later ``intact`` writes.
        changes.setdefault(
            "lifecycle_migration", getattr(self, "_operation_migration", None)
        )
        return QuarantineOutcome(
            issue=_norm(request.issue),
            lane=_norm_lane(request.lane),
            role=_norm(request.role),
            action_generation=_norm(request.action_generation),
            classification=classification,
            **changes,
        )

    @staticmethod
    def _approval_stale_reason(
        request: QuarantineRequest,
        inspection: QuarantineInspection,
        classification: PendingComposerClassification,
    ) -> str:
        """Why this approval may not act on the receiver that is live RIGHT NOW.

        The approval names one composer of one agent generation.  These three fences
        are what make it that narrow, so they must hold at every moment we are about
        to close — not only when the generation was opened (R1-F1 j#78347: a crash
        between the request CAS and the close leaves an owed close whose target may
        since have taken new input or started working; killing it then would destroy
        an input the owner never approved discarding).
        """
        if not classification.quarantine_candidate:
            return "classification is not quarantine-eligible; zero actuation"
        if inspection.row_revision != request.approved_revision:
            return "approval is stale for the current agent/composer revision"
        if inspection.attested_at and not _not_after(
            inspection.attested_at, request.approval_observed_at
        ):
            return "approval predates the current attested agent generation"
        return ""

    def run(self, request: QuarantineRequest, *, execute: bool) -> QuarantineOutcome:
        # Redmine #13844 R5-F1: the schema migration this ONE command performs is captured
        # operation-scoped — reset at the start of every run(), so a REUSED use case / store never
        # carries a PAST run's migration into this action's audit. It is set only after this run's
        # migrating write below (a read-only / preflight run never sets it).
        self._operation_migration: Optional[dict[str, Any]] = None
        inspection = self.ops.inspect(request)
        classification = inspection.classification
        if not execute:
            return self._base_outcome(
                request,
                classification,
                detail=(
                    "preflight only; known marker requires q-enter"
                    if classification.q_enter_recommended
                    else "preflight only"
                ),
            )

        # Positive durable approval and exact generation are mandatory before any
        # lifecycle write or process close.
        try:
            decision = DecisionPointer(
                source="redmine",
                issue_id=_norm(request.issue),
                journal_id=_norm(request.journal),
            )
        except DecisionPointerError:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                detail="approval journal is not a complete Redmine pointer",
            )
        try:
            expected_action = quarantine_action_id(
                lane_id=request.lane, role=request.role, locator=request.locator
            )
        except ValueError:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                detail="action generation inputs do not identify one exact receiver",
            )
        if _norm(request.action_generation) != expected_action:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                detail="action generation does not match the exact approved receiver",
            )
        approval_time = _parse_time(request.approval_observed_at)
        if approval_time is None or request.approved_revision < 0:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                detail="approval timestamp / agent revision is incomplete",
            )

        try:
            key = LaneLifecycleKey(
                inspection.workspace_id, _norm_lane(request.lane)
            )
        except ValueError:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                detail="workspace / lane identity is incomplete",
            )
        try:
            current = self.store.get_replacement(key)
        except (LaneLifecycleError, ReleasePinError, OSError):
            current = None
        if current is None or not current.lane_active or current.issue_id != decision.issue_id:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                detail="lane lifecycle owner is absent / foreign / inactive",
            )

        try:
            pin = ReleasePin(
                role=request.role,
                assigned_name=request.assigned_name,
                locator=request.locator,
            )
        except ReleasePinError:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                replacement_state=current.state,
                detail="approved receiver pin is incomplete",
            )

        exact_generation = (
            current.action_id == expected_action and current.pins == (pin,)
        )
        if current.state == REPLACEMENT_REPLACED and exact_generation:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                replacement_state=REPLACEMENT_REPLACED,
                detail="replacement generation already replaced (idempotent)",
            )
        if current.state in (REPLACEMENT_REQUESTED, REPLACEMENT_PENDING):
            if not exact_generation or current.decision != decision:
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    replacement_state=current.state,
                    detail=(
                        "a different replacement generation / approval is already "
                        "in flight"
                    ),
                )
        else:
            # Opening a NEW generation depends on the current transient composer
            # observation. A stored generation is resumed instead of re-opened — but
            # resuming never means acting blind: an owed close re-runs these same
            # fences below (R1-F1 j#78347).
            stale = self._approval_stale_reason(request, inspection, classification)
            if stale:
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    replacement_state=current.state,
                    detail=stale,
                )
            opened = self.store.request_replacement(
                key,
                expected_revision=current.revision,
                action_id=expected_action,
                pins=(pin,),
                decision=decision,
            )
            # Redmine #13844 R5-F1: capture the migration THIS run's write gate performed (the
            # store is most-recent, so read it right after the migrating write). ``or`` keeps the
            # first migration if any later write in this run reads back ``intact``. request_replacement
            # is this command's first (and only migration-capable) write on the shared store.
            self._operation_migration = self._operation_migration or lifecycle_migration_payload(
                self.store.last_write_preparation
            )
            if not opened.applied:
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    replacement_state=current.state,
                    detail=f"replacement request refused ({opened.reason})",
                )
            current = self.store.get_replacement(key)
            if current is None:
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    detail="replacement row vanished after request",
                )

        closed = current.state == REPLACEMENT_PENDING
        if current.state == REPLACEMENT_REQUESTED:
            # The close is still owed, so it is about to kill whatever is live at the
            # pinned locator *now* — not necessarily what the owner looked at (R1-F1
            # j#78347). Re-run the approval fences at this edge unless the old receiver
            # is POSITIVELY gone: an absent one is the crash-after-close case (contract
            # 5), whose redrive owes only the launch. An inventory that cannot prove
            # absence is not absence — it re-validates and fails closed.
            if inspection.receiver_present is not False:
                stale = self._approval_stale_reason(
                    request, inspection, classification
                )
                if stale:
                    return self._base_outcome(
                        request,
                        classification,
                        executed=True,
                        replacement_state=REPLACEMENT_REQUESTED,
                        detail=f"{stale}; owed close withheld",
                    )
            close = self.ops.close_receiver(request, pin)
            if not (close.closed or close.old_absent):
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    replacement_state=REPLACEMENT_REQUESTED,
                    detail="exact old receiver close failed; replacement remains requested",
                )
            pending = self.store.record_replacement_outcome(
                key,
                action_id=expected_action,
                expected_revision=current.revision,
                target=REPLACEMENT_PENDING,
            )
            if not pending.applied:
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    replacement_state=REPLACEMENT_REQUESTED,
                    closed_old_receiver=close.closed,
                    detail=f"replacement pending CAS refused ({pending.reason})",
                )
            closed = close.closed
            current = self.store.get_replacement(key)
            if current is None:
                return self._base_outcome(
                    request,
                    classification,
                    executed=True,
                    replacement_state=REPLACEMENT_PENDING,
                    closed_old_receiver=closed,
                    detail="replacement row vanished after pending record",
                )

        # ``pending`` is deliberately the durable partial-launch state. A redrive
        # starts here and never closes the old locator a second time.
        try:
            self.ops.heal_receiver(request)
        except Exception as exc:  # noqa: BLE001 - fixed type only, no body/detail persisted
            return self._base_outcome(
                request,
                classification,
                executed=True,
                replacement_state=REPLACEMENT_PENDING,
                closed_old_receiver=closed,
                detail=f"fresh receiver launch failed ({type(exc).__name__}); redrive launch only",
            )
        verification = self.ops.verify_fresh_receiver(
            request, fresh_after=current.updated_at
        )
        if not verification.ok:
            return self._base_outcome(
                request,
                classification,
                executed=True,
                replacement_state=REPLACEMENT_PENDING,
                closed_old_receiver=closed,
                detail="fresh receiver verification failed; redrive launch only",
            )
        replaced = self.store.record_replacement_outcome(
            key,
            action_id=expected_action,
            expected_revision=current.revision,
            target=REPLACEMENT_REPLACED,
        )
        return self._base_outcome(
            request,
            classification,
            executed=True,
            replacement_state=(
                REPLACEMENT_REPLACED if replaced.applied else REPLACEMENT_PENDING
            ),
            closed_old_receiver=closed,
            fresh_locator=verification.locator if replaced.applied else "",
            detail=(
                "receiver replaced and generation-attested"
                if replaced.applied
                else f"replacement completion CAS refused ({replaced.reason})"
            ),
        )


@dataclass
class LiveSublaneQuarantineOps:
    repo_root: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS

    def _rows(self) -> Sequence[Mapping[str, object]]:
        return list_herdr_agent_rows(self.env)

    def _providers(self) -> tuple[str, str]:
        return (
            resolve_gateway_provider(str(self.repo_root)),
            resolve_worker_provider(str(self.repo_root)),
        )

    @staticmethod
    def _cwd_matches(row: Mapping[str, object], repo_root: Path) -> bool:
        raw = _norm(row.get("foreground_cwd") or row.get("cwd"))
        if not raw:
            return False
        try:
            return Path(raw).expanduser().resolve() == repo_root.expanduser().resolve()
        except OSError:
            return False

    @staticmethod
    def _placement(row: Mapping[str, object]) -> tuple[str, str]:
        return (_norm(row.get("workspace_id")), _tab_id_of_row(row))

    def _pair_ok(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        workspace_id: str,
        lane: str,
    ) -> bool:
        try:
            providers = self._providers()
        except WorkflowProviderUnresolved:
            return False
        found: dict[str, Mapping[str, object]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decoded.ok or decoded.identity is None:
                continue
            identity = decoded.identity
            if (
                identity.workspace_id == workspace_id
                and _norm_lane(identity.lane_id) == _norm_lane(lane)
                and identity.role in providers
            ):
                if identity.role in found:
                    return False
                found[identity.role] = row
        return (
            all(provider in found for provider in providers)
            and self._placement(found[providers[0]]) == self._placement(found[providers[1]])
        )

    def inspect(self, request: QuarantineRequest) -> QuarantineInspection:
        workspace_id = repo_scope_workspace_id(self.repo_root)
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001 - inventory failure is a fixed classification
            return QuarantineInspection(
                workspace_id=workspace_id,
                signal=PendingComposerSignal(False, None, "unknown", False, False),
                receiver_present=None,  # unreadable proves nothing; never read as absence
                detail="inventory_unreadable",
            )
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.assigned_name)
        ]
        exact = [row for row in matches if _agent_locator(row) == _norm(request.locator)]
        # Presence is the exact pinned pair being live AT ALL — deliberately not the
        # unique-row test below. An ambiguous inventory still means something is live at
        # that locator, so an owed close must re-validate rather than treat it as the
        # crash-after-close case (R1-F1 j#78347).
        present = bool(exact)
        row = exact[0] if len(exact) == 1 and len(matches) == 1 else None
        if row is None:
            return QuarantineInspection(
                workspace_id=workspace_id,
                signal=PendingComposerSignal(True, None, "unknown", False, False),
                receiver_present=present,
                detail="generation_mismatch",
            )
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        identity_ok = bool(
            decoded.ok
            and decoded.identity is not None
            and decoded.identity.workspace_id == workspace_id
            and _norm_lane(decoded.identity.lane_id) == _norm_lane(request.lane)
            and decoded.identity.role == _norm(request.role)
        )
        revision_raw = row.get("revision")
        revision = (
            int(revision_raw)
            if isinstance(revision_raw, int) and not isinstance(revision_raw, bool)
            else -1
        )
        generation_ok = (
            identity_ok
            and revision == request.approved_revision
            and self._cwd_matches(row, self.repo_root)
            and self._pair_ok(rows, workspace_id=workspace_id, lane=request.lane)
        )
        attestation_record = None
        try:
            attestation_record = HerdrIdentityAttestationStore().read(
                _norm(request.assigned_name)
            )
        except Exception:  # noqa: BLE001 - unreadable attestation fails closed
            pass
        attestation = evaluate_attestation(
            attestation_record,
            live_locator=_norm(request.locator),
            expected_workspace_id=workspace_id,
            expected_role=_norm(request.role),
            expected_lane=_norm_lane(request.lane),
        )
        try:
            binary = _resolve_binary_or_die(self.env)
            state = HerdrCliAgentStateReader(
                binary, runner=self.runner, timeout=self.timeout
            ).read_agent_state(_norm(request.locator))
            runtime_state = state.state if state.ok else "unknown"
            read = HerdrCliTransport(
                binary, runner=self.runner, timeout=self.timeout
            ).read_pane(_norm(request.locator), lines=80)
            observation = (
                observe_composer_text(read.content)
                if read.ok
                else ComposerObservation(False, None)
            )
        except Exception:  # noqa: BLE001 - transport failure is inventory_unreadable
            runtime_state = "unknown"
            observation = ComposerObservation(False, None)
        correlated: list[str] = []
        ledger = HerdrDeliveryLedger()
        for marker in observation.marker_ids:
            records = ledger.records_for_marker(marker)
            if any(
                _norm(record.target) in (
                    _norm(request.locator),
                    _norm(request.assigned_name),
                )
                for record in records
            ):
                correlated.append(marker)
        signal = PendingComposerSignal(
            inventory_readable=observation.readable,
            has_pending=observation.has_pending,
            agent_state=runtime_state,
            identity_attested=attestation.ok,
            generation_matches=generation_ok,
            correlated_marker_ids=tuple(correlated),
            correlation_ambiguous=len(observation.marker_ids) > 1,
        )
        return QuarantineInspection(
            workspace_id=workspace_id,
            signal=signal,
            row_revision=revision,
            attested_at=_norm(
                attestation_record.observed_at if attestation_record else ""
            ),
            receiver_present=present,
            detail="classified_without_persisting_composer_body",
        )

    def close_receiver(
        self, request: QuarantineRequest, pin: ReleasePin
    ) -> CloseReceiverResult:
        workspace_id = repo_scope_workspace_id(self.repo_root)
        try:
            rows = self._rows()
            plan = pin_matched_close_plan(
                (pin,), rows, workspace_id=workspace_id, lane_id=request.lane
            )
        except Exception:  # noqa: BLE001 - close preflight failure is zero close
            return CloseReceiverResult(False, detail="close_preflight_unreadable")
        if plan is None:
            return CloseReceiverResult(False, detail="close_pin_inconsistent")
        if not plan.close_targets:
            # Old exact locator already vanished. A recycled assigned name at a
            # different locator is NOT absence; it is a newer generation and stale approval.
            recycled = any(
                isinstance(row, Mapping)
                and _norm(row.get(AGENT_KEY_NAME)) == pin.assigned_name
                and _agent_locator(row) != pin.locator
                for row in rows
            )
            return CloseReceiverResult(
                closed=False,
                old_absent=not recycled,
                detail="old_receiver_absent" if not recycled else "assigned_name_recycled",
            )
        result = execute_herdr_retire_close(
            HerdrRetireClosePlan(
                workspace_id=plan.workspace_id,
                lane_id=plan.lane_id,
                close_targets=plan.close_targets,
            ),
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
        )
        return CloseReceiverResult(
            closed=bool(result.closed) and not result.failed,
            detail="closed" if result.closed and not result.failed else "close_failed",
        )

    def heal_receiver(self, request: QuarantineRequest) -> None:
        HerdrSublaneActuatorOps(
            repo_root=self.repo_root,
            lane_label=request.lane,
            issue=request.issue,
            journal=request.journal,
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
        ).heal_lane_column(str(self.repo_root))

    def verify_fresh_receiver(
        self, request: QuarantineRequest, *, fresh_after: str
    ) -> FreshReceiverVerification:
        workspace_id = repo_scope_workspace_id(self.repo_root)
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001
            return FreshReceiverVerification(False, detail="inventory_unreadable")
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _norm(row.get(AGENT_KEY_NAME)) == _norm(request.assigned_name)
        ]
        if len(matches) != 1:
            return FreshReceiverVerification(False, detail="fresh_slot_not_unique")
        row = matches[0]
        locator = _agent_locator(row)
        if not locator or locator == _norm(request.locator):
            return FreshReceiverVerification(False, detail="locator_not_fresh")
        if not self._cwd_matches(row, self.repo_root) or not self._pair_ok(
            rows, workspace_id=workspace_id, lane=request.lane
        ):
            return FreshReceiverVerification(False, detail="fresh_pair_or_cwd_mismatch")
        try:
            record = HerdrIdentityAttestationStore().read(_norm(request.assigned_name))
        except Exception:  # noqa: BLE001
            record = None
        joined = evaluate_attestation(
            record,
            live_locator=locator,
            expected_workspace_id=workspace_id,
            expected_role=request.role,
            expected_lane=request.lane,
        )
        if not joined.ok or record is None or not _not_after(fresh_after, record.observed_at or ""):
            return FreshReceiverVerification(False, detail="fresh_attestation_missing_or_old")
        return FreshReceiverVerification(True, locator=locator, detail="fresh_attested_pair")


def format_quarantine_text(outcome: QuarantineOutcome) -> str:
    lines = [
        f"sublane quarantine: {outcome.lane} / {outcome.role} (issue {outcome.issue})",
        f"  classification: {outcome.classification.label}",
        f"  executed: {outcome.executed} replacement_state: {outcome.replacement_state}",
    ]
    if outcome.classification.q_enter_recommended:
        lines.append("  next: use the existing q-enter rail for the correlated marker")
    elif outcome.classification.quarantine_candidate and not outcome.executed:
        lines.append("  candidate only; --execute requires exact positive approval fields")
    if outcome.detail:
        lines.append(f"  detail: {outcome.detail}")
    if outcome.lifecycle_migration:
        mig = outcome.lifecycle_migration
        lines.append(
            "  - shared lifecycle store forward-migrated "
            f"v{mig['from_version']} -> v{mig['to_version']} "
            f"(peer lanes at read-fail-closed risk: {mig['peer_active_lanes'] or 'none'})"
        )
    return "\n".join(lines)


def cmd_sublane_quarantine(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    request = QuarantineRequest(
        issue=getattr(args, "issue", "") or "",
        lane=getattr(args, "lane", "") or "",
        journal=getattr(args, "journal", "") or "",
        role=getattr(args, "role", "") or "",
        assigned_name=getattr(args, "assigned_name", "") or "",
        locator=getattr(args, "locator", "") or "",
        action_generation=getattr(args, "action_generation", "") or "",
        approval_observed_at=getattr(args, "approval_observed_at", "") or "",
        approved_revision=int(getattr(args, "approved_revision", -1)),
    )
    use_case = SublaneQuarantineUseCase(
        ops=LiveSublaneQuarantineOps(repo_root=repo_root),
        store=LaneReplacementStore(),
    )
    # Redmine #13844 R4-F1: the use case already carries the schema migration (from the store's
    # ACCUMULATED preparation) in ``outcome.lifecycle_migration`` — the CLI does NOT re-read the
    # mutable "last write" (which a later ``intact`` write would have cleared).
    outcome = use_case.run(request, execute=bool(getattr(args, "execute", False)))
    if bool(getattr(args, "json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_quarantine_text(outcome), file=sys.stdout)
    return 1 if outcome.is_blocked else 0


def register_sublane_quarantine_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "quarantine",
        help=(
            "Redmine #13763: classify one exact pending composer and, only with a "
            "positive generation-bound owner approval, replace that managed receiver "
            "without generic Enter/C-u/body typing. Default is read-only preflight."
        ),
    )
    for flag, dest, help_text in (
        ("--issue", "issue", "Redmine issue id owning the lane"),
        ("--lane", "lane", "Exact lane id/label"),
        ("--journal", "journal", "Positive owner approval journal id"),
        ("--role", "role", "Exact provider role of the receiver"),
        ("--assigned-name", "assigned_name", "Exact managed assigned name"),
        ("--locator", "locator", "Exact approved old process locator"),
        (
            "--action-generation",
            "action_generation",
            "Exact quarantine:<lane>:<role>:<locator> generation",
        ),
        (
            "--approval-observed-at",
            "approval_observed_at",
            "Approval journal timestamp (ISO-8601) used by the stale-generation fence",
        ),
    ):
        parser.add_argument(flag, dest=dest, required=True, help=help_text)
    parser.add_argument(
        "--approved-revision",
        dest="approved_revision",
        required=True,
        type=int,
        help="Herdr agent/composer revision observed by the approval",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply owner-approved process replacement; otherwise classify only",
    )
    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_quarantine)


__all__ = (
    "CloseReceiverResult",
    "ComposerObservation",
    "FreshReceiverVerification",
    "LiveSublaneQuarantineOps",
    "QuarantineInspection",
    "QuarantineOutcome",
    "QuarantineRequest",
    "SublaneQuarantineOps",
    "SublaneQuarantineUseCase",
    "cmd_sublane_quarantine",
    "format_quarantine_text",
    "observe_composer_text",
    "register_sublane_quarantine_parser",
)
