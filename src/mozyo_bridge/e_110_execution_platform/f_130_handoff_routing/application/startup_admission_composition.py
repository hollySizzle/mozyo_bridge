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


def admit_receiver_startup_or_die(*, receiver: str, **kwargs: Any) -> None:
    """Compose the provider-specific resolver, then run the provider-neutral gate.

    Signature-compatible with the gate itself, so the command module's call is unchanged.
    An explicit ``updater_targets`` from the caller (tests, and any path that arms the
    fence deliberately) always wins — this only supplies the default for a real send.
    """
    kwargs.setdefault("updater_targets", updater_target_resolver_for(receiver))
    _admit_with_gate(receiver=receiver, **kwargs)


__all__ = ("admit_receiver_startup_or_die", "updater_target_resolver_for")
