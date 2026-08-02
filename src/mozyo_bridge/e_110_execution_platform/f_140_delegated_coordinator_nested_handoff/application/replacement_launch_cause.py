"""Which typed launch cause a replacement's participant may arm (Redmine #14741 j#97171).

One question, split out of the live recovery port when that module reached the module-health
ceiling: given a stored participant, what cause -- if any -- may its relaunch carry?

It is deliberately not "read the pin's field". The evidence triplet is the only thing that
may arm the update-authority fence, and it arms it on an EXACT closed token or not at all.
"""

from __future__ import annotations

def launch_cause_for_pin(pin) -> str:
    """The typed launch cause this relaunch carries, or ``""`` to refuse. (pure)

    The pin's evidence triplet is the only thing that may arm the update fence: an EMPTY
    cause is a legacy / generic participant and launches unarmed, exactly as before; the
    exact closed update token arms it. Anything else -- padded, a ``str`` subclass, a number,
    an unknown word -- is refused rather than normalised, because the value it would arm on
    is one nobody recorded (Redmine #14741 j#97074 / j#97171).
    """
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
        LAUNCH_CAUSE_GENERIC_FRESH,
        LAUNCH_CAUSE_UPDATE_RELAUNCH,
    )

    value = getattr(pin, "evidence_cause", "")
    if type(value) is not str:
        return ""
    if value == "":
        return LAUNCH_CAUSE_GENERIC_FRESH
    if value == LAUNCH_CAUSE_UPDATE_RELAUNCH:
        return LAUNCH_CAUSE_UPDATE_RELAUNCH
    return ""


__all__ = ("launch_cause_for_pin",)
