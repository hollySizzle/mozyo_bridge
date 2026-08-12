"""Parent-launcher 2-phase binding for the launch-generation pointer (Redmine #14203).

The launch-path half of the design-consultation answer (j#87472): the parent launcher, not
the wrapper, owns the home-scoped current-generation pointer
(:mod:`~mozyo_bridge.core.state.herdr_launch_generation`). Two calls bracket a managed launch:

* :func:`reserve_launch_generations` — BEFORE the first Herdr side effect, reserve a
  ``pending`` generation for every wrapped launch slot. A newer generation atomically
  supersedes the old ``attested`` current pointer, so the relaunch window reads ``pending``
  (fail-closed), never the stale generation. A store write that cannot proceed raises, which
  the session-start boundary turns into a typed zero-actuation launch refusal — nothing has
  actuated yet, so the launch simply does not start.
* :func:`finalize_launch_generations` — AFTER the launch, CAS each reservation to
  ``attested`` ONLY when the whole composite authority agrees: the launch receipt +
  startup-transaction participant (not closed) + the wrapper's own
  ``attestation_write_succeeded`` execution event + the exact main-attestation
  identity/locator with ``verdict == present``. This is BEST-EFFORT: a missing evidence
  piece or a CAS refused by a newer reservation leaves the row ``pending`` (recovery then
  fails closed) and never breaks a launch whose agents are already live — the wrapper is
  never made the thing that flips the phase.

Both are gated by the caller on ``attest_launcher and launch_plans`` — the same boundary as
the #13748/#13882 launcher-capability preflight. An unwrapped / adopt-only / dry-run run
establishes no generation and stays byte-invariant; it simply has no generation to recover,
which fails closed exactly as an un-attested launch already does. The generation protocol is
a capability of the *managed wrapper* and requires the terminal-bound v2 cache. A legacy v1
cache is backup/rebuilt only inside the four-store offline rollout; managed launch refuses it
before reserving a startup transaction so mixed runtimes cannot silently drop the binding.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Iterable, Optional

from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationError,
    HerdrLaunchGenerationStore,
    verified_generation_token,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    read_execution_events,
)
from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding import (  # noqa: E501
    reserve_session_launch_identities,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    PaneBoundReceiptError,
    parse_pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    terminal_identity_of_live_slot,
)


def reserve_launch_generations(
    *,
    store_home: Path,
    startup_action_id: str,
    launch_plans: Iterable,
    workspace_id: str,
    lane_id: str,
    effect_fence=None,
) -> None:
    """Reserve a ``pending`` generation for every wrapped launch slot (fail-closed).

    Called after the startup transaction is reserved and BEFORE the first Herdr side effect.
    Raises :class:`HerdrLaunchGenerationError` if any reservation cannot be written — the
    caller converts that into a typed zero-actuation launch refusal.
    """
    token = _norm(startup_action_id)
    if not token:
        # No startup action means no generation to bind (an unwrapped / dry-run run never
        # reaches here through the caller's gate, but stay defensive and no-op rather than
        # write a tokenless row).
        return
    store = HerdrLaunchGenerationStore(home=store_home)
    for plan in launch_plans:
        if effect_fence is not None:
            effect_fence()
        store.reserve_pending(
            assigned_name=_norm(getattr(plan, "assigned_name", "")),
            startup_action_id=token,
            workspace_id=_norm(workspace_id),
            role=_norm(getattr(plan, "provider", "")),
            lane_id=_norm_lane(lane_id),
        )


def finalize_launch_generations(
    *,
    store_home: Path,
    startup_action_id: str,
    slots: Iterable,
    workspace_id: str,
    lane_id: str,
    attestation_read: Optional[Callable[[str], object]],
    inventory_rows: Iterable,
    effect_fence=None,
) -> None:
    """CAS each launched slot's reservation to ``attested`` when the evidence agrees.

    Store/evidence failures remain best-effort and leave the row ``pending``.  A supplied
    action-time effect fence is different: it runs immediately before each physical CAS
    and its refusal propagates, so offline restore cannot continue past fresh drift.
    """
    token = _norm(startup_action_id)
    if not token or attestation_read is None:
        return
    try:
        store = HerdrLaunchGenerationStore(home=store_home)
        fence = StartupTransactionFence(home=store_home)
    except Exception:  # noqa: BLE001 - a store we cannot even construct leaves pending
        return
    try:
        action = fence.read(token)
    except Exception:  # noqa: BLE001 - unreadable transaction => leave every row pending
        action = None
    if action is None:
        return
    try:
        events = read_execution_events(fence, token)
    except Exception:  # noqa: BLE001 - read_execution_events never raises, stay defensive
        events = None
    attested_names = {
        _norm(getattr(event, "participant", ""))
        for event in (events or ())
        if _norm(getattr(event, "stage", "")) == STAGE_ATTESTATION_WRITE_SUCCEEDED
    }
    ws = _norm(workspace_id)
    lane = _norm_lane(lane_id)
    try:
        snapshot = tuple(inventory_rows)
    except Exception:  # noqa: BLE001 - unreadable inventory leaves every row pending
        return
    for slot in slots:
        # Only a slot THIS transaction actually launched recorded a participant (adopt /
        # dry-run / unattested slots never call `record_launch`), so the participant check
        # below is itself the launch-only filter — no separate slot-kind guard is needed.
        name = _norm(getattr(slot, "assigned_name", ""))
        role = _norm(getattr(slot, "provider", ""))
        locator = _norm(getattr(slot, "locator", ""))
        launch_terminal_id = _norm(getattr(slot, "launch_terminal_id", ""))
        if not (name and role and locator and launch_terminal_id):
            continue
        matches = [
            row
            for row in snapshot
            if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == name
        ]
        if len(matches) != 1 or _norm(_agent_locator(matches[0])) != locator:
            continue
        live_terminal_id = terminal_identity_of_live_slot(name, locator, snapshot)
        if live_terminal_id is None:
            continue
        # 1. launch receipt + startup-transaction participant (not closed), for this slot.
        participant = action.participant_for(role)
        if participant is None or getattr(participant, "closed", True):
            continue
        if not (
            _norm(getattr(participant, "assigned_name", "")) == name
            and _norm(getattr(participant, "locator", "")) == locator
            and _norm(getattr(participant, "receipt", ""))
        ):
            continue
        try:
            pane_receipt = parse_pane_bound_receipt(participant.receipt)
        except PaneBoundReceiptError:
            continue
        if (
            pane_receipt is None
            or pane_receipt.native_name != native_name_for(name)
            or _norm(pane_receipt.terminal_id) != launch_terminal_id
        ):
            continue
        # 2. the wrapper's OWN attestation_write_succeeded execution event.
        if name not in attested_names:
            continue
        # 3. the exact main-attestation identity/locator with verdict == present.
        try:
            record = attestation_read(name)
        except Exception:  # noqa: BLE001 - unreadable attestation => leave pending
            continue
        if record is None:
            continue
        observed_at = _norm(str(getattr(record, "observed_at", "") or ""))
        attested_terminal_id = getattr(record, "terminal_id", None)
        if not (
            _norm(getattr(record, "verdict", "")) == VERDICT_PRESENT
            and observed_at
            and _norm(getattr(record, "assigned_name", "")) == name
            and _norm(getattr(record, "role", "")) == role
            and _norm(getattr(record, "workspace_id", "")) == ws
            and _norm_lane(getattr(record, "lane_id", "")) == lane
            and _norm(getattr(record, "locator", "")) == locator
            and type(attested_terminal_id) is str
            and bool(attested_terminal_id)
            and attested_terminal_id.strip() == attested_terminal_id
            and attested_terminal_id == launch_terminal_id
            and attested_terminal_id == live_terminal_id
        ):
            continue
        # 4. CAS to attested. A refusal (a newer pending reservation superseded this one)
        #    leaves the row pending — never overwrite the newer generation.
        try:
            if effect_fence is not None:
                effect_fence()
            store.finalize(
                assigned_name=name,
                startup_action_id=token,
                workspace_id=ws,
                role=role,
                lane_id=lane,
                locator=locator,
                terminal_id=attested_terminal_id,
                verdict=VERDICT_PRESENT,
                observed_at=observed_at,
            )
        except HerdrLaunchGenerationError:
            continue


def verified_terminal_generation_token(
    home: Path | None,
    *,
    assigned_name: str,
    workspace_id: str,
    role: str,
    lane_id: str,
    locator: str,
    terminal_id: str,
    norm=_norm,
    norm_lane=_norm_lane,
) -> str:
    """Return the launch token only when its atomic participant receipt owns terminal.

    The durable generation row binds the action token to the logical slot and locator.
    The completed startup transaction binds that same token to the ``pane_bound_v2``
    receipt written from the prepared pane's own response.  Requiring the current
    terminal id to match that receipt prevents a restored terminal from borrowing the
    stale token of the process that previously occupied the same name and locator.
    """
    expected_terminal = norm(terminal_id)
    if not expected_terminal:
        return ""

    def _receipt_matches(raw: object) -> bool:
        try:
            receipt = parse_pane_bound_receipt(raw)
        except PaneBoundReceiptError:
            return False
        return (
            receipt is not None
            and receipt.native_name == native_name_for(assigned_name)
            and norm(receipt.terminal_id) == expected_terminal
        )

    return verified_generation_token(
        home,
        assigned_name=assigned_name,
        workspace_id=workspace_id,
        role=role,
        lane_id=lane_id,
        locator=locator,
        live_terminal_id=expected_terminal,
        norm=norm,
        norm_lane=norm_lane,
        participant_receipt_matches=_receipt_matches,
    )


def reserve_session_launch_generations(
    *, store_home, transaction, launch_plans, workspace_id, lane_id, attest_launcher,
    effect_fence=None,
) -> None:
    """Session-boundary reserve: gate on a wrapped managed launch, then reserve every slot's
    ``pending`` generation. Gated identically to the #13748/#13882 launcher-capability
    preflight (``attest_launcher and launch_plans``) so an unwrapped / adopt-only / dry-run
    run stays byte-invariant. A store write that cannot proceed becomes a typed zero-actuation
    launch refusal (:class:`HerdrSessionStartError`) — nothing has actuated yet."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        HerdrSessionStartError,
    )

    if not (transaction is not None and attest_launcher and launch_plans):
        return
    try:
        reserve_launch_generations(
            store_home=store_home, startup_action_id=transaction.action_id,
            launch_plans=launch_plans, workspace_id=workspace_id, lane_id=lane_id,
            effect_fence=effect_fence,
        )
    except HerdrLaunchGenerationError as exc:
        raise HerdrSessionStartError(
            f"could not reserve the launch generation before starting the lane ({exc}); "
            f"refusing to launch a pair the gateway recovery could never generation-bind "
            f"(nothing was actuated)"
        ) from exc


def open_startup_transaction_and_reserve_generations(
    *, workspace_id, lane_id, providers, dry_run, home, fence, nonce, launch_plans,
    attest_launcher, env=None, resolved=None, effect_fence=None,
    completion_fence=None, refuse_nonterminal_slot_overlap=False,
):
    """Reserve BOTH pre-side-effect identity records in one step — the immutable startup
    action (#13948) and each wrapped slot's launch generation (#14203 j#87472) — the LAST
    thing before the first Herdr write, after every fail-closed launch preflight. Returns the
    ``StartupTransaction`` (``None`` on a dry run). A generation reservation that cannot be
    written becomes a typed zero-actuation launch refusal."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
        open_startup_transaction,
    )

    transaction = open_startup_transaction(
        workspace_id=workspace_id, lane_id=lane_id, providers=providers, dry_run=dry_run,
        home=home, fence=fence, nonce=nonce, effect_fence=effect_fence,
        completion_fence=completion_fence,
        refuse_nonterminal_slot_overlap=refuse_nonterminal_slot_overlap,
    )
    reserve_session_launch_generations(
        store_home=home, transaction=transaction, launch_plans=launch_plans,
        workspace_id=workspace_id, lane_id=lane_id, attest_launcher=attest_launcher,
        effect_fence=effect_fence,
    )
    # Redmine #14741 bracket 1 (j#96917 / j#96966 C12): the identity reservation is
    # established at the SAME pre-side-effect moment as the generation, from the identity
    # the preflight already PINNED — never a separate disk re-resolution (j#96886). A
    # receipt-capable action that cannot record it refuses the launch outright.
    reserve_session_launch_identities(
        store_home=home, transaction=transaction, launch_plans=launch_plans,
        workspace_id=workspace_id, lane_id=lane_id, attest_launcher=attest_launcher,
        resolved=resolved, effect_fence=effect_fence,
    )
    return transaction


def finalize_session_launch_generations(
    *, store_home, transaction, slots, workspace_id, lane_id, attestation_read,
    inventory_rows, attest_launcher, launch_plans, dry_run, effect_fence=None,
) -> None:
    """Session-boundary finalize after a fresh per-slot action-time admission.

    Generation evidence/CAS failures leave ``pending`` for completion readback to reject;
    an explicit effect-fence refusal propagates before the affected CAS.
    """
    if dry_run or transaction is None or not attest_launcher or not launch_plans:
        return
    if effect_fence is not None:
        effect_fence()
    finalize_launch_generations(
        store_home=store_home, startup_action_id=transaction.action_id, slots=slots,
        workspace_id=workspace_id, lane_id=lane_id, attestation_read=attestation_read,
        inventory_rows=inventory_rows, effect_fence=effect_fence,
    )


__all__ = (
    "finalize_launch_generations",
    "finalize_session_launch_generations",
    "open_startup_transaction_and_reserve_generations",
    "reserve_launch_generations",
    "reserve_session_launch_generations",
    "verified_terminal_generation_token",
)
