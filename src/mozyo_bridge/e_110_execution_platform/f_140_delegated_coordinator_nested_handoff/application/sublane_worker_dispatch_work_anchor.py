"""Post-resume work-anchor authority for same-lane worker dispatch (#14981).

The lane lifecycle decision answers why a lane is active.  A work anchor answers what
the current gateway was asked to send to its worker.  Fresh-create flows normally use the
same Redmine journal for both, but hibernate/resume deliberately leaves the lifecycle
pointer on the resume decision.  This module owns the stricter alternative join used when
those pointers differ; it never writes lifecycle state and never treats a lossy delivery
receipt as authority by itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
    LiveRedmineJournalError,
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    resolve_gateway_provider,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_worker_delivery import (  # noqa: E501
    is_exact_implementation_request_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneLaneView,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    WorkerDispatchRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    encode_assigned_name,
)


def _norm(value: object) -> str:
    return str(value or "").strip()


def gateway_delivery_receipt_matches(
    records,
    *,
    issue: str,
    journal: str,
    assigned_name: str,
    locator: str,
    provider: str,
) -> bool:
    """True only for a positive delivery receipt bound to this gateway generation.

    The Redmine entry says *what* work was delegated; the queue-enter receipt says that
    exact entry reached the currently attested gateway process.  Neither fact is sufficient
    alone.  An older receipt for the same lane name but a previous locator never authorizes
    the current process, and a lossy/unreadable ledger simply yields ``False``.
    """
    wanted = {
        "assigned_name": _norm(assigned_name),
        "locator": _norm(locator),
        "provider": _norm(provider),
    }
    if not all(wanted.values()) or not _norm(issue) or not _norm(journal):
        return False
    for record in records or ():
        if _norm(getattr(record, "issue_id", "")) != _norm(issue):
            continue
        if _norm(getattr(record, "journal_id", "")) != _norm(journal):
            continue
        receiver = _norm(getattr(record, "receiver", ""))
        recorded_provider = _norm(getattr(record, "provider", ""))
        if wanted["provider"] not in {receiver, recorded_provider}:
            continue
        if _norm(getattr(record, "status", "")) != "sent":
            continue
        if _norm(getattr(record, "reason", "")) != "ok":
            continue
        if _norm(getattr(record, "target", "")) not in {
            wanted["assigned_name"],
            wanted["locator"],
        }:
            continue
        observation = getattr(record, "queue_enter_observation", None)
        if not isinstance(observation, Mapping):
            continue
        if observation.get("read_ok") is not True:
            continue
        if observation.get("runtime_state") != "busy":
            continue
        binding = observation.get("gateway_binding")
        if not isinstance(binding, Mapping):
            continue
        if any(_norm(binding.get(key)) != value for key, value in wanted.items()):
            continue
        # The v2 receipt carries both process-generation observation tokens. They are
        # not caller authority; requiring them excludes older generic ACK rows that never
        # observed a managed gateway generation.
        if not _norm(binding.get("startup_action_id")):
            continue
        if not _norm(binding.get("row_revision")):
            continue
        return True
    return False


def received_work_anchor_is_current(
    *,
    repo_root: Path,
    env: Mapping[str, str],
    lane: SublaneLaneView,
    request: WorkerDispatchRequest,
    lifecycle,
    rows,
    delivery_records,
) -> bool:
    """Join one named work entry to the current, attested gateway generation.

    Every failure is a zero-send ``False``: unreadable Redmine, an old/generic receipt,
    a different locator, an unattested gateway, or a marker naming another lane/generation.
    """
    journal = _norm(request.journal)
    try:
        generation = int(getattr(lifecycle, "lane_generation", 0) or 0)
    except (TypeError, ValueError):
        return False
    gateway_provider = resolve_gateway_provider(str(repo_root))
    if not journal or generation <= 0 or not gateway_provider:
        return False

    assigned_name = encode_assigned_name(
        lane.workspace_id, gateway_provider, lane.lane_id
    )
    matches = [
        row
        for row in rows or ()
        if isinstance(row, Mapping)
        and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
    ]
    if len(matches) != 1:
        return False
    locator = _agent_locator(matches[0])
    if not locator or locator != _norm(lane.gateway_pane):
        return False

    attestation = HerdrIdentityAttestationStore().read(assigned_name)
    joined = evaluate_attestation(
        attestation,
        live_locator=locator,
        expected_workspace_id=lane.workspace_id,
        expected_role=gateway_provider,
        expected_lane=lane.lane_id,
    )
    if not joined.ok:
        return False
    if not gateway_delivery_receipt_matches(
        delivery_records,
        issue=request.issue,
        journal=journal,
        assigned_name=assigned_name,
        locator=locator,
        provider=gateway_provider,
    ):
        return False

    try:
        entries = LiveRedmineJournalSource.from_environment(
            environ=env
        ).read_entries(request.issue)
    except LiveRedmineJournalError:
        return False
    except Exception:  # noqa: BLE001 - any unreadable authority is a zero-send
        return False
    exact = [entry for entry in entries if _norm(getattr(entry, "journal_id", "")) == journal]
    return len(exact) == 1 and is_exact_implementation_request_anchor(
        exact[0],
        issue=request.issue,
        journal=journal,
        lane=request.lane_label,
        lane_generation=generation,
    )


__all__ = ("gateway_delivery_receipt_matches", "received_work_anchor_is_current")
