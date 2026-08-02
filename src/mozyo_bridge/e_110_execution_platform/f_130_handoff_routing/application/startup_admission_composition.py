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
Whether the authority fence is **armed at all** for a given receiver:

- a provider with a trusted built-in updater binding (today: Codex/npm) is armed with the
  built-in resolver, and gets the full ``aligned`` / ``split`` / ``unknown`` treatment;
- a provider with no binding is left **unarmed** — ``not_evaluated``, this ticket's gate
  simply does not apply to it (D2 item 1). It is emphatically NOT promoted to ``unknown``:
  that conflation is what refused every Claude send on every host.

Unarmed is not the same as vouched-for, and the distinction is confined to the *generic
ready send*. An update screen actually observed on the receiver is still refused by the
#13760 startup-blocker path regardless of binding, and an update-caused exit / self-heal
arms the fence explicitly (D2 item 2) — a provider whose update state we cannot describe
must not be actuated *on an update-relevant path*, whatever its binding.
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
    """The typed resolver to arm the authority fence with, or ``None`` to leave it unarmed.

    ``None`` means "this ticket's gate does not apply to this provider" and reaches the
    classifier as ``not_evaluated``. It never means "assume it is fine": nothing about the
    provider's update authority is claimed, and any update-relevant path must arm the
    fence explicitly rather than inherit this decision.
    """
    return builtin_updater_target_probe() if is_supported_provider(receiver) else None


def resolve_generation_key(receiver: str, locator: str, *, home: Any = None):
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
    )
    from mozyo_bridge.core.state.launch_identity_receipt import GenerationKey

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
    key = resolve_generation_key(receiver, target)
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
    """Compose the provider-specific resolver, then run the provider-neutral gate.

    Signature-compatible with the gate itself, so the command module's call is unchanged.
    An explicit ``updater_targets`` from the caller (tests, and any path that arms the
    fence deliberately) always wins — this only supplies the default for a real send.
    """
    kwargs.setdefault("updater_targets", updater_target_resolver_for(receiver))
    kwargs.setdefault("on_startup_blocker", record_update_evidence)
    _admit_with_gate(receiver=receiver, **kwargs)


__all__ = (
    "admit_receiver_startup_or_die",
    "record_update_evidence",
    "resolve_generation_key",
    "updater_target_resolver_for",
)
