"""Prepare and live-resolve a recovered gateway continuation (#14741 B6b3-1/2a(2)).

Between "the gateway is live and attested" and "the original implementation request has
been delivered" there is one question worth isolating: WHICH request, exactly. This module
answers it, then joins its stored participant to one fresh live inventory row, and stops.
It sends nothing, reads no delivery ledger or attestation store, and never reports a
transaction as completed -- those are later tranches.

The answer comes only from the stored row. A caller's plan, its anchor, and anything it
would like the continuation to be are not consulted: the row was written when the recovery
was planned, and re-deriving the pointer from today's inputs is how a retry ends up
delivering something the transaction never agreed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from mozyo_bridge.core.state.replacement_transaction_model import (
    ContinuationPointer,
    PARTICIPANT_REPLACED,
    ParticipantPin,
)
from mozyo_bridge.core.state.lane_pin_role import PIN_ROLE_GATEWAY
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E501
    RECOVERY_ACTION_GENERATION,
    stored_row_is_this_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E501
    RECOVERED_READY,
    RecoveryActuation,
    actuate_vanished_gateway_recovery,
    recovery_lease_holder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E501
    REDISPATCH_GATEWAY_ONCE,
    RESUME_GATE,
    RequestAnchor,
    recovery_action_id_for_pin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
    resolve_gateway_provider,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_LIVE,
    classify_named_slot,
)

#: The stored continuation is exact and this recovery is ready to deliver it. NOT delivered,
#: NOT confirmed, NOT completed -- naming it any of those would be a claim about a send that
#: has not happened.
CONTINUATION_READY = "continuation_ready"

#: The stored row is gone, unreadable, or is not this recovery any more.
STOPPED_TRANSACTION_UNAVAILABLE = "transaction_unavailable"
#: The stored continuation pointer is not the exact one this rail delivers.
STOPPED_CONTINUATION_INVALID = "continuation_invalid"

#: The one live-inventory snapshot, repo identity, or provider binding could not be read.
STOPPED_INVENTORY_UNAVAILABLE = "inventory_unavailable"
#: Inventory was readable but did not prove one exact fresh gateway generation.
STOPPED_INVENTORY_INVALID = "inventory_invalid"
#: A fresh, live locator has been joined to the stored participant. This is not an
#: attestation, send, ledger write, or completion claim.
INVENTORY_JOINED = "inventory_joined"


@dataclass(frozen=True)
class ContinuationPreparation:
    """The stored pointer to deliver, or the closed reason not to. (pure value)"""

    outcome: str = ""
    stopped: str = ""
    detail: str = ""
    action_id: str = ""
    holder: str = ""
    #: The STORED :class:`ContinuationPointer` itself, carried rather than re-assembled
    #: (audit j#97223). The next legs hand this straight to `drive_continuation_once`; a
    #: flattened copy would mean each of them rebuilding a pointer from raw columns again,
    #: which is the re-derivation this rail exists to avoid. Always exactly that type or
    #: ``None`` -- see the type gate below (audit j#97226).
    pointer: Optional["ContinuationPointer"] = None
    #: The canonical stored gateway participant, from the SAME verified row as the pointer
    #: (audit j#97233 item 1). The target resolver needs its workspace/lane/provider/
    #: assigned_name/old_locator, and taking them from anywhere else -- a caller's pin, a
    #: re-decoded manifest -- would let the send address a participant this transaction
    #: never pinned. Always exactly a `ParticipantPin` or ``None``.
    participant: Optional["ParticipantPin"] = None

    @property
    def ready(self) -> bool:
        return self.outcome == CONTINUATION_READY


@dataclass(frozen=True)
class VanishedGatewayInventoryJoin:
    """The fresh live locator joined to the stored recovery participant (pure value)."""

    outcome: str = ""
    stopped: str = ""
    detail: str = ""
    action_id: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    provider: str = ""
    assigned_name: str = ""
    fresh_locator: str = ""
    old_locator: str = ""

    @property
    def joined(self) -> bool:
        return self.outcome == INVENTORY_JOINED


def _raw(value: object) -> str:
    """Plain exact text, or ``""``. No strip, no subclass, no coercion."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


def _stopped(reason: str, detail: str = "", action_id: str = "") -> ContinuationPreparation:
    return ContinuationPreparation(stopped=reason, detail=detail, action_id=action_id)


def _inventory_stopped(reason: str, detail: str = "") -> VanishedGatewayInventoryJoin:
    return VanishedGatewayInventoryJoin(stopped=reason, detail=detail)


def _resolved_repo_root(repo_root: object) -> Optional[Path]:
    """Return an already-canonical absolute directory, without repairing caller input."""
    concrete_path_type = type(Path())
    if type(repo_root) is str:
        raw = _raw(repo_root)
        if not raw:
            return None
        candidate = Path(raw)
    elif type(repo_root) is concrete_path_type:
        candidate = repo_root
    else:
        return None
    try:
        resolved = candidate.resolve(strict=True)
        if not candidate.is_absolute() or candidate != resolved or not resolved.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def resolve_vanished_gateway_inventory(
    preparation: object,
    *,
    repo_root: object,
    list_rows: Callable[[], object],
) -> VanishedGatewayInventoryJoin:
    """Join one stored participant to one fresh live inventory row, with no actuation.

    The inventory callable is invoked exactly once. All other authority comes from the
    canonical repository root, its configured coordinator-provider binding, and the exact
    :class:`ContinuationPreparation` produced above. A successful result still authorises
    no attestation, delivery, ledger write, CAS, or completion transition.
    """
    if type(preparation) is not ContinuationPreparation:
        return _inventory_stopped(
            STOPPED_CONTINUATION_INVALID,
            "the continuation preparation is not canonical",
        )
    pointer = preparation.pointer
    pin = preparation.participant
    action_id = _raw(preparation.action_id)
    if (
        type(preparation.outcome) is not str
        or preparation.outcome != CONTINUATION_READY
        or type(preparation.stopped) is not str
        or preparation.stopped != ""
        or type(preparation.detail) is not str
        or preparation.detail != ""
        or type(pointer) is not ContinuationPointer
        or type(pin) is not ParticipantPin
        or not action_id
        or preparation.holder != recovery_lease_holder(action_id)
    ):
        return _inventory_stopped(
            STOPPED_CONTINUATION_INVALID,
            "the continuation preparation is not ready and exact",
        )

    root = _resolved_repo_root(repo_root)
    if root is None:
        return _inventory_stopped(
            STOPPED_INVENTORY_UNAVAILABLE,
            "the lane runtime repository root is unavailable",
        )
    try:
        workspace_id = repo_scope_workspace_id(root)
        provider = resolve_gateway_provider(str(root))
    except Exception:  # noqa: BLE001 - resolver details are never exposed
        return _inventory_stopped(
            STOPPED_INVENTORY_UNAVAILABLE,
            "the canonical workspace or gateway provider is unavailable",
        )
    workspace_id = _raw(workspace_id)
    provider = _raw(provider)
    if not workspace_id or not provider:
        return _inventory_stopped(
            STOPPED_INVENTORY_UNAVAILABLE,
            "the canonical workspace or gateway provider is unavailable",
        )

    source = _raw(getattr(pointer, "source", None))
    issue_id = _raw(getattr(pointer, "issue_id", None))
    journal_id = _raw(getattr(pointer, "journal_id", None))
    if (
        not source
        or not issue_id
        or not journal_id
        or _raw(getattr(pointer, "expected_gate", None)) != RESUME_GATE
        or _raw(getattr(pointer, "next_semantic_action", None))
        != REDISPATCH_GATEWAY_ONCE
        or _raw(getattr(pin, "role", None)) != PIN_ROLE_GATEWAY
        or _raw(getattr(pin, "provider", None)) != provider
        or _raw(getattr(pin, "lane_id", None)) == ""
        or _raw(getattr(pin, "assigned_name", None)) == ""
        or _raw(getattr(pin, "old_locator", None)) == ""
        or type(getattr(pin, "is_self", None)) is not bool
        or pin.is_self
        or getattr(pin, "phase", None) != PARTICIPANT_REPLACED
        or type(getattr(pin, "evidence_workspace_id", None)) is not str
        or pin.evidence_workspace_id not in ("", workspace_id)
    ):
        return _inventory_stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored pointer or participant is not exact",
        )

    try:
        anchor = RequestAnchor(source=source, issue_id=issue_id, journal_id=journal_id)
        rebound = recovery_action_id_for_pin(anchor, pin, workspace_id=workspace_id)
    except Exception:  # noqa: BLE001 - forged values carry no authority
        return _inventory_stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored participant cannot be bound to this action",
        )
    if rebound != action_id:
        return _inventory_stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored participant is not bound to this action",
        )

    assigned_name = pin.assigned_name
    decoded = decode_assigned_name(assigned_name)
    identity = decoded.identity if decoded.ok else None
    if (
        identity is None
        or identity.workspace_id != workspace_id
        or identity.lane_id != pin.lane_id
        or identity.role != provider
    ):
        return _inventory_stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored assigned name does not encode the pinned identity",
        )

    try:
        rows = list_rows()
    except Exception:  # noqa: BLE001 - inventory failures are fixed, value-free refusals
        return _inventory_stopped(
            STOPPED_INVENTORY_UNAVAILABLE,
            "the live agent inventory is unavailable",
        )
    if type(rows) not in (list, tuple) or any(type(row) is not dict for row in rows):
        return _inventory_stopped(
            STOPPED_INVENTORY_INVALID,
            "the live agent inventory is not canonical",
        )
    candidates = [
        row
        for row in rows
        if type(row.get(AGENT_KEY_NAME)) is str
        and row.get(AGENT_KEY_NAME) == assigned_name
    ]
    if len(candidates) != 1:
        return _inventory_stopped(
            STOPPED_INVENTORY_INVALID,
            "the live agent inventory does not contain one exact participant",
        )
    row = candidates[0]
    locator = row.get(AGENT_KEY_LOCATOR)
    revision = row.get("revision")
    if (
        type(locator) is not str
        or not locator
        or locator != locator.strip()
        or locator == pin.old_locator
        or type(revision) is not int
        or revision < 0
        or type(row.get("agent")) is not str
        or row.get("agent") != provider
        or (
            "provider" in row
            and (type(row.get("provider")) is not str or row.get("provider") != provider)
        )
        or classify_named_slot(row) != SLOT_LIVE
    ):
        return _inventory_stopped(
            STOPPED_INVENTORY_INVALID,
            "the named participant is not one fresh live gateway generation",
        )

    return VanishedGatewayInventoryJoin(
        outcome=INVENTORY_JOINED,
        action_id=action_id,
        workspace_id=workspace_id,
        lane_id=pin.lane_id,
        provider=provider,
        assigned_name=assigned_name,
        fresh_locator=locator,
        old_locator=pin.old_locator,
    )


def prepare_vanished_gateway_continuation(
    *,
    plan: Any,
    anchor: RequestAnchor,
    store: Any,
    home: Any,
    workspace_id: str,
    actuation_port: Any,
    launch_authority: Any = None,
    store_admission: Any = None,
    clock: Optional[Any] = None,
) -> ContinuationPreparation:
    """Recover the gateway, then say exactly which request it still owes.

    Drives the B6b2 executor first: anything other than ``recovered_ready`` is that
    executor's own closed reason, returned unchanged and with the row not re-read -- there
    is nothing to prepare for a recovery that did not happen.
    """
    actuation = actuate_vanished_gateway_recovery(
        plan=plan,
        anchor=anchor,
        store=store,
        home=home,
        workspace_id=workspace_id,
        actuation_port=actuation_port,
        launch_authority=launch_authority,
        store_admission=store_admission,
        clock=clock,
    )
    if not isinstance(actuation, RecoveryActuation) or actuation.outcome != RECOVERED_READY:
        return _stopped(
            _raw(getattr(actuation, "stopped", "")) or "actuation_stopped",
            _raw(getattr(actuation, "detail", "")),
            _raw(getattr(actuation, "action_id", "")),
        )

    action_id = actuation.action_id
    from mozyo_bridge.core.state.replacement_transaction import (
        ReplacementTransactionKey,
    )

    try:
        key = ReplacementTransactionKey(_raw(workspace_id), action_id)
        stored = store.get(key)
        same = stored is not None and stored_row_is_this_recovery(
            stored, key=key, anchor=anchor, action_id=action_id
        )
    except Exception:  # noqa: BLE001 - KI / SystemExit / GeneratorExit propagate
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored transaction could not be read",
            action_id,
        )
    if not same:
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored row at this key is not this recovery",
            action_id,
        )

    # ONLY the stored pointer, and the stored OBJECT: the raw columns were already proved
    # exact by the same-action validator above, so re-flattening them here would just be a
    # second, drifting copy of the thing the next legs actually need.
    try:
        pointer = stored.continuation
    except Exception:  # noqa: BLE001 - a hostile record is input, not truth
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored continuation could not be read",
            action_id,
        )
    # The EXACT type (audit j#97226). The same-action validator proves the raw COLUMNS,
    # but the record's `continuation` property is the thing that turns them into an object,
    # and a facade can return whatever it likes from it: a look-alike with the right five
    # attributes read as canonical and would have been carried into the send closure of the
    # next leg. A subclass is refused for the same reason it is everywhere else on this
    # rail -- it decides for itself what its own members mean.
    if type(pointer) is not ContinuationPointer:
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored continuation is not a canonical pointer",
            action_id,
        )
    if (
        _raw(getattr(pointer, "source", None)) != anchor.source
        or _raw(getattr(pointer, "issue_id", None)) != anchor.issue_id
        or _raw(getattr(pointer, "journal_id", None)) != anchor.journal_id
        or _raw(getattr(pointer, "expected_gate", None)) != RESUME_GATE
        or _raw(getattr(pointer, "next_semantic_action", None)) != REDISPATCH_GATEWAY_ONCE
    ):
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored continuation is not this rail's exact pointer",
            action_id,
        )

    # The participant comes off the SAME record the pointer did, and is type-gated for the
    # same reason: a look-alike would be carried into the resolver's exact joins.
    try:
        participants = tuple(getattr(stored, "participants", ()) or ())
    except Exception:  # noqa: BLE001 - a hostile record is input, not truth
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored participants could not be read",
            action_id,
        )
    if (
        type(participants) is not tuple
        or len(participants) != 1
        or type(participants[0]) is not ParticipantPin
    ):
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored row does not carry exactly one canonical participant",
            action_id,
        )
    # Re-bound to the action id, not merely type-checked (audit j#97236). The same-action
    # validator decodes the raw MANIFEST; `participants` is a different property on the same
    # record, so a facade can hand back an exact-type pin that the manifest never contained
    # -- measured, with a foreign assigned_name. The canonical id function is the join: a pin
    # that is really this action's re-derives this action's id.
    try:
        rebound = recovery_action_id_for_pin(
            anchor, participants[0], workspace_id=key.workspace_id
        )
    except Exception:  # noqa: BLE001 - an unusable pin is not this participant
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored participant could not be bound to this action",
            action_id,
        )
    if rebound != action_id:
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored participant is not the one this action pinned",
            action_id,
        )

    return ContinuationPreparation(
        outcome=CONTINUATION_READY,
        action_id=action_id,
        participant=participants[0],
        # Derived, never accepted: the delivery leg has to take the same lease this
        # recovery already holds (j#97190 F5).
        holder=recovery_lease_holder(action_id),
        pointer=pointer,
    )


__all__ = (
    "CONTINUATION_READY",
    "INVENTORY_JOINED",
    "STOPPED_CONTINUATION_INVALID",
    "STOPPED_INVENTORY_INVALID",
    "STOPPED_INVENTORY_UNAVAILABLE",
    "STOPPED_TRANSACTION_UNAVAILABLE",
    "ContinuationPreparation",
    "VanishedGatewayInventoryJoin",
    "prepare_vanished_gateway_continuation",
    "resolve_vanished_gateway_inventory",
)
