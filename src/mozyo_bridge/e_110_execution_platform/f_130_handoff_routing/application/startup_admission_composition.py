"""Composition root for the pre-send startup gate (Redmine #14741, Answer D2 j#96288).

``orchestrate_handoff`` calls exactly one thing to admit a receiver, and this module is
that thing. It exists because of item 3 of the D2 amendment, which ruled two constraints
that the previous round satisfied by breaking a third:

- the **generic gate must not construct an ambient probe of its own**. R3 defaulted the
  updater-target probe inside :mod:`.startup_admission_gate`, which armed the authority
  fence for every caller including every unit test — so tests that had nothing to do with
  a package manager began consulting the real host's ``PATH`` and ``npm``, and 29 of them
  died (j#96202). A production default that reads ambient state is not hermetic;
- ``commands.py`` is at its module-health ceiling and **the baseline may not be raised**,
  so the wiring cannot simply be inlined there.

Both hold if the provider-specific composition moves *here* and ``commands.py`` keeps a
single neutral dependency: it imports the same symbol name from this module instead of
from the gate, so its line count is unchanged and it names no provider, no package
manager, and no query.

What this decides, and only this
--------------------------------
An ordinary handoff is a **generic ready send**, not an update action. It therefore does
not arm the updater-authority fence merely because the receiver happens to have a trusted
built-in updater binding. The action-time screen classifier still proves that the pane is
an input composer; an observed update, login, trust, or setup screen is refused before the
first byte.

Callers that possess a typed update cause may still pass ``updater_targets`` explicitly
and get the full ``aligned`` / ``split`` / ``unknown`` treatment. The launch/relaunch
composition already does exactly that for an update-derived cause. Keeping that authority
check on update paths, instead of every message send, avoids turning package-manager
ownership into a prerequisite for using an already-running receiver.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_gate import (  # noqa: E501
    admit_receiver_startup_or_die as _admit_with_gate,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
    builtin_updater_target_probe,
    is_supported_provider,
)


def updater_target_resolver_for(receiver: str) -> Optional[Callable[[str], Any]]:
    """Resolve the explicit update-scoped fence for ``receiver``, if one is built in.

    This helper is not an ambient default for ordinary sends. A caller must first have a
    typed update cause and then pass the returned resolver as ``updater_targets``.
    ``None`` means "this ticket's gate does not apply to this provider" and reaches the
    classifier as ``not_evaluated``; it never means "assume it is fine".
    """
    return builtin_updater_target_probe() if is_supported_provider(receiver) else None


def resolve_generation_key(
    receiver: str, locator: str, *, live_rows, home: Any = None
):
    """The EXACT generation living at ``locator`` right now, or ``None`` (audit j#96966 C14).

    The launch-generation store is the authority for this question, and the receipt store
    deliberately is not. A reservation there atomically supersedes the previous row for the
    same slot, so "which generation is at this pane" has exactly one current answer; the
    receipt store, by contrast, keeps every generation's row, so searching IT by locator
    could attach a live screen to a stale attested row from an earlier generation. That is
    the defect C14 names, and the fix is to ask a different store — not to search the same
    one more carefully.

    Requires exactly one ATTESTED generation at the locator. Zero means nothing to bind to;
    more than one means the host is in a state where the question has no answer, and both
    are ``None`` rather than a guess.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import (
        GENERATION_ATTESTED,
        HerdrLaunchGenerationError,
        HerdrLaunchGenerationStore,
        verified_generation_token,
    )
    from mozyo_bridge.core.state.launch_identity_receipt import GenerationKey
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        _norm,
        _norm_lane,
        terminal_identity_of_live_slot,
    )

    provider = str(receiver or "").strip()
    pane = str(locator or "").strip()
    if not provider or not pane:
        return None
    store = HerdrLaunchGenerationStore(home=home)
    try:
        names = store.assigned_names()
        if not names:
            return None
        matches = []
        for name in sorted(names):
            generation = store.read(name)
            if (
                generation is not None
                and generation.phase == GENERATION_ATTESTED
                and generation.locator == pane
                and generation.role == provider
            ):
                matches.append(generation)
    except (HerdrLaunchGenerationError, OSError):
        return None
    if len(matches) != 1:
        return None
    found = matches[0]
    live_terminal_id = terminal_identity_of_live_slot(
        found.assigned_name, pane, live_rows
    )
    if (
        live_terminal_id is None
        or found.terminal_id != live_terminal_id
        or verified_generation_token(
            home, assigned_name=found.assigned_name,
            workspace_id=found.workspace_id, role=found.role,
            lane_id=found.lane_id, locator=pane,
            live_terminal_id=live_terminal_id, norm=_norm, norm_lane=_norm_lane,
        ) != found.startup_action_id
    ):
        return None
    return GenerationKey(
        workspace_id=found.workspace_id,
        lane_id=found.lane_id,
        provider=found.role,
        assigned_name=found.assigned_name,
        startup_action_id=found.startup_action_id,
    )


def record_update_evidence(receiver: str, target: str, blocker_id: str) -> None:
    """Bind durable update-relaunch evidence to the EXACT generation at ``target``.

    The answer to j#96871 Q3: a self-heal that fires after the gateway has vanished has no
    pane left to read, but the screen WAS observed here, while the process was alive.

    Only an update-derived screen produces evidence — a trust or login prompt says nothing
    about which binary an update would reach. And nothing is swallowed (audit j#96966 C14 /
    C12): a receipt-capable generation whose evidence cannot be recorded surfaces its
    failure. The send is being refused either way at this point, so the only thing a
    swallow would buy is a quieter refusal that lost the fact.
    """
    from mozyo_bridge.core.state.launch_identity_receipt import LaunchIdentityReceiptStore
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
        is_update_derived_blocker,
    )

    if not is_update_derived_blocker(receiver, blocker_id):
        return
    try:
        import os

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        live_rows = list_herdr_agent_rows(os.environ)
    except Exception:  # noqa: BLE001 - no fresh inventory means no evidence binding
        return
    key = resolve_generation_key(receiver, target, live_rows=live_rows)
    if key is None:
        # No single attested generation at this pane: there is nothing this observation can
        # be bound TO. Recording it against a guess is the mis-binding C14 forbids.
        return
    store = LaunchIdentityReceiptStore()
    receipt = store.read_receipt(key)
    if receipt is None or not receipt.attested:
        return
    store.bind_evidence(
        key, blocker_id=blocker_id, identity_digest=receipt.identity_digest
    )


def admit_receiver_startup_or_die(*, receiver: str, **kwargs: Any) -> None:
    """Run the provider-neutral gate for an ordinary handoff.

    Signature-compatible with the gate itself, so the command module's call is unchanged.
    An explicit ``updater_targets`` from an update-scoped caller is preserved, but the
    generic send does not construct one from ambient host state. Actual startup/update
    screens are still classified and refused independently through the blocker sink.
    """
    kwargs.setdefault("on_startup_blocker", record_update_evidence)
    _admit_with_gate(receiver=receiver, **kwargs)


__all__ = (
    "admit_receiver_startup_or_die",
    "record_update_evidence",
    "resolve_generation_key",
    "updater_target_resolver_for",
)
