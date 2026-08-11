"""Three-bracket binding for the executable identity receipt (Redmine #14741).

Design Answer j#96917 as corrected by j#96899 and j#96966 C12/C13. The receipt needs THREE
moments, not the two the launch generation uses, and the reason is a code fact measured in
j#97001:

1. **reserve** — before the first Herdr side effect, record ``unbound_pending`` with the
   identity the preflight already pinned (:func:`reserve_session_launch_identities`);
2. *(the existing launch-generation finalize runs here, unchanged — the identity is
   deliberately NOT promoted at this point, per C13: an identity must never become
   authority after the generation finalize it depends on has failed);*
3. **finalize** — after the lane's lifecycle row is DECLARED, promote to ``attested`` with
   the lane's actual generation and revision (:func:`finalize_lane_identity_receipts`).

Why three. ``finalize_session_launch_generations`` runs inside ``prepare_session``, while
``_declare_lane_lifecycle`` runs in the actuator *after* ``prepare_session`` returns — so at
the launch-generation finalize the lifecycle row **does not exist yet**. A fresh sublane
declares it after launch and a default lane has none at all, which is the same fact that
forced the two-phase receipt in the first place. Attesting at bracket 2 could only be done
by writing a blank revision or guessing one, and j#96899/C13 forbid both.

Fail-closed, and only where it applies (C12)
--------------------------------------------
A **receipt-capable** action — one whose id carries the ``ir1`` capability tag — must probe
and reserve before anything is started, and any failure is a typed zero-actuation refusal.
A **legacy** action never touches the receipt store at all, which is what keeps every
pre-#14741 launch byte-invariant: the capability lives in the action id's shape, outside the
store, so losing the store cannot make a capable action look legacy (j#96892).

Because a capability-tagged action can only be minted on a v2 store with a launch manifest,
this wiring is dormant until that enablement happens — and it is dormant by *skipping*,
never by swallowing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


def _capable(action_id: object) -> bool:
    """True iff this action promised identity receipts. An unknown shape raises."""
    from mozyo_bridge.core.state.startup_transaction_fence import requires_identity_receipt

    return requires_identity_receipt(action_id)


def _key(workspace_id: str, lane_id: str, provider: str, assigned: str, action_id: str):
    from mozyo_bridge.core.state.launch_identity_receipt import GenerationKey

    return GenerationKey(
        workspace_id=workspace_id,
        lane_id=lane_id,
        provider=provider,
        assigned_name=assigned,
        startup_action_id=action_id,
    )


def _pinned_identity(provider: str, resolved: Optional[Mapping]) -> str:
    """The identity the PREFLIGHT pinned for ``provider`` (j#96886), or ``""``.

    Never a separate disk re-resolution: re-resolving would record an identity that could
    already differ from the one about to launch, which is the race this whole ticket is
    about. The exec target comes from the ``ResolvedProviderLaunch`` the launch plan is
    built from, and only the manifest read (which cannot change what that plan will run)
    happens here.
    """
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
        is_supported_provider,
        resolve_launched_identity,
    )

    if not is_supported_provider(provider):
        return ""
    launch = (resolved or {}).get(provider)
    exec_target = getattr(launch, "exec_target", "") or ""
    if not exec_target:
        return ""
    identity = resolve_launched_identity(provider, exec_target)
    return identity.digest if identity.resolved else ""


def reserve_session_launch_identities(
    *,
    store_home: Path,
    transaction: Any,
    launch_plans: Iterable,
    workspace_id: str,
    lane_id: str,
    resolved: Optional[Mapping] = None,
    attest_launcher: str = "",
) -> None:
    """Bracket 1 — reserve ``unbound_pending`` before the first Herdr side effect.

    Raises :class:`HerdrSessionStartError` on ANY failure for a receipt-capable action, so
    the launch refuses with zero actuation (C12). A legacy action returns immediately and
    touches nothing.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        HerdrSessionStartError,
    )

    if not (transaction is not None and attest_launcher and launch_plans):
        return
    if not _capable(transaction.action_id):
        return

    from mozyo_bridge.core.state.launch_identity_receipt import (
        LaunchIdentityReceiptError,
        LaunchIdentityReceiptStore,
    )

    store = LaunchIdentityReceiptStore(home=Path(store_home))
    for plan in launch_plans:
        provider = getattr(plan, "provider", "")
        assigned = getattr(plan, "assigned_name", "")
        digest = _pinned_identity(provider, resolved)
        if not digest:
            # An unbound provider carries no receipt obligation. It is recorded as such in
            # the action's manifest, so this is an absence the manifest already states.
            continue
        try:
            store.reserve(
                _key(workspace_id, lane_id, provider, assigned, transaction.action_id),
                identity_digest=digest,
            )
        except LaunchIdentityReceiptError as exc:
            raise HerdrSessionStartError(
                f"this launch owes an identity receipt for {provider!r} but the receipt "
                f"authority could not record it ({exc}); refusing to start a lane whose "
                "update-derived relaunch could never be proven — nothing was actuated"
            ) from exc


def _declared_lifecycle_revision(store_home: Path, lane_id: str) -> str:
    """The lane's ACTUAL declared revision, or ``""`` (fail-closed, non-creating).

    ``load_lane_lifecycle_readonly`` returns ``None`` for an unknown / newer / malformed /
    partial component schema, which is already the direction this bracket needs: no
    revision means no attestation, and the receipt stays non-authority.
    """
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        load_lane_lifecycle_readonly,
    )

    records = load_lane_lifecycle_readonly(home=Path(store_home))
    if not records:
        return ""
    wanted = str(lane_id or "").strip()
    matched = [
        r for r in records if str(getattr(r, "lane_id", "") or "").strip() == wanted
    ]
    if len(matched) != 1:
        return ""
    return str(getattr(matched[0], "revision", "") or "").strip()


def finalize_lane_identity_receipts(
    *,
    store_home: Path,
    result: Any,
    lane_generation: str = "",
    lifecycle_revision: str = "",
    live_rows=(),
) -> None:
    """Bracket 3 — attest, after the lifecycle row exists, with the composite proof.

    Never raises: the launch has already happened, so a failure here cannot un-start it.
    What a failure DOES do is leave the receipt ``unbound_pending``, which is not authority
    — so no evidence can bind to it and no relaunch is ever armed from it. That is the
    fail-closed direction for this bracket, and it is the reason bracket 1 is the one that
    refuses: refusing before actuation is free, refusing after it is not.

    ``lane_generation`` / ``lifecycle_revision`` are the lane's ACTUAL declared values, read
    by the caller from the lifecycle authority. A blank is not passed through — the store
    refuses it, which is what makes "declared" mean declared.
    """
    action_id = getattr(result, "action_id", "") or ""
    if not action_id:
        return
    try:
        if not _capable(action_id):
            return
    except Exception:  # noqa: BLE001 - an unclassifiable action attests nothing
        return
    lane_generation = lane_generation or action_id
    if not lifecycle_revision:
        lifecycle_revision = _declared_lifecycle_revision(
            store_home, getattr(result, "lane_id", "")
        )
    if not lane_generation or not lifecycle_revision:
        return

    from mozyo_bridge.core.state.launch_identity_receipt import (
        LaunchIdentityReceiptError,
        LaunchIdentityReceiptStore,
    )

    store = LaunchIdentityReceiptStore(home=Path(store_home))
    for slot in getattr(result, "slots", None) or ():
        provider = getattr(slot, "provider", "")
        assigned = getattr(slot, "assigned_name", "")
        locator = getattr(slot, "locator", "") or ""
        if not provider or not assigned or not locator:
            continue
        if not _generation_attested(
            store_home, assigned=assigned, action_id=action_id, locator=locator,
            workspace_id=result.workspace_id, lane_id=result.lane_id,
            role=provider, live_rows=live_rows,
        ):
            # The launch generation for this slot is not finalized against THIS action and
            # THIS locator, so the composite proof does not hold — C13's "never promote the
            # identity after the generation finalize failed".
            continue
        key = _key(result.workspace_id, result.lane_id, provider, assigned, action_id)
        try:
            receipt = store.read_receipt(key)
        except LaunchIdentityReceiptError:
            continue
        if receipt is None:
            continue
        try:
            store.finalize(
                key,
                identity_digest=receipt.identity_digest,
                locator=locator,
                lane_generation=lane_generation,
                lifecycle_revision=lifecycle_revision,
                composite_proof=True,
            )
        except LaunchIdentityReceiptError:
            continue


def _generation_attested(
    store_home: Path, *, assigned: str, action_id: str, locator: str,
    workspace_id: str, lane_id: str, role: str, live_rows
) -> bool:
    """True iff the launch-generation authority attested THIS action at THIS locator.

    This is the composite proof's load-bearing half, and it is read from the #14203
    generation store rather than re-derived: that store only reaches ``attested`` once the
    launch receipt, the startup-transaction participant, the wrapper's own execution event
    and the exact main attestation all agreed. Asking it is asking all of them.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import verified_generation_token
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        _norm, _norm_lane, terminal_identity_of_live_slot,
    )
    terminal_id = terminal_identity_of_live_slot(assigned, locator, live_rows)
    return verified_generation_token(
        Path(store_home), assigned_name=assigned, workspace_id=workspace_id,
        role=role, lane_id=lane_id, locator=locator, live_terminal_id=terminal_id,
        norm=_norm, norm_lane=_norm_lane,
    ) == action_id


def finalize_lane_receipts_from_inventory(*, store_home: Path, result: Any, env) -> None:
    """Finalize from one fresh view; unreadable inventory leaves receipts pending."""
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )
        rows = tuple(list_herdr_agent_rows(env))
    except Exception:  # noqa: BLE001
        rows = ()
    finalize_lane_identity_receipts(store_home=store_home, result=result, live_rows=rows)


__all__ = (
    "finalize_lane_identity_receipts",
    "finalize_lane_receipts_from_inventory",
    "reserve_session_launch_identities",
)
