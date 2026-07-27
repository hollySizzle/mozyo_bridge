"""The typed reason a fenced action-bound launch carries out to the public surface.

Redmine #14480. The generic actuator (:meth:`...replacement_actuator.
ReplacementActuatorUseCase._step_launch_owed`) records a single hardcoded
``detail="launch"`` for ANY ``effect_failed`` at the launch leg, because at that layer the
launch is an opaque ``LAUNCH_DONE`` / ``LAUNCH_ERROR`` port call. The live ports below it
DO know why: :class:`...domain.sublane_runtime_fence.SublaneHealError` and
:class:`...domain.sublane_actuation.SublaneLauncherIncompatibleError` each carry a stable,
value-free ``reason`` token. Without a projection those tokens die at the port's ``except``
and the operator sees only ``effect_failed: launch`` — which is exactly how the #14479
j#88695 live dogfood spent two runs unable to tell ``replacement_binding_context_missing``
from a transient pane failure.

This module is the ONE authority for that projection, so the ports and the public surfaces
cannot drift into per-surface dialects of the same judgement (the #14478 lesson: the same
decision implemented twice diverges). It is pure: no I/O, no store, no live inventory.

Two deliberate boundaries:

* **The vocabulary is not re-listed here.** The reason tokens are owned by the modules that
  raise them (``pair_split`` / ``launch_target_absent`` / ``pair_incomplete`` in
  :mod:`...domain.sublane_runtime_fence`; ``replacement_binding_*`` /
  ``action_owned_startup_rollback_required`` in the v1 replacement binding adapter;
  ``launcher_*`` in the launcher capability probe). Copying that list into a third place is
  how a new fence reason silently degrades to ``unknown``. What IS enforced here is the
  token *shape* — a lowercase closed-token identifier — so a rogue / unexpected port value
  can never smuggle a path, a locator, or exception prose into a public field.
* **The field is typed, never parsed.** Callers read the port's typed
  ``launch_failure_reason`` attribute; nothing here parses an exception message.
"""

from __future__ import annotations

import re
from typing import Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_EFFECT_FAILED,
)

#: The actuator's hardcoded ``detail`` for a failed launch leg — the ONE value that marks a
#: result as "the launch effect is what failed" (:meth:`..._step_launch_owed`).
LAUNCH_LEG_DETAIL = "launch"

#: No launch fence fired: the last launch succeeded, or none ran in this drive.
LAUNCH_FAILURE_NONE = ""

#: The launch failed without a typed fence reason (a bare adapter / transport error, or a
#: port whose value did not survive the shape guard). Distinct from :data:`LAUNCH_FAILURE_NONE`
#: — "we know it failed and we do not know why" is not "nothing failed".
LAUNCH_FAILURE_UNTYPED = "launch_error"

#: The shape a projectable reason token must have: a lowercase closed-token identifier.
#: Every token the fences raise matches it; a path (``/``), a locator (``:``), a credential,
#: or exception prose (spaces, punctuation, case) does not — those degrade to
#: :data:`LAUNCH_FAILURE_UNTYPED` rather than reaching a public field.
_REASON_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_launch_failure_reason(raw: object) -> str:
    """Project ``raw`` onto the typed launch-failure field. (pure)

    ``""`` / ``None`` -> :data:`LAUNCH_FAILURE_NONE` (no fence fired — never invented).
    A well-shaped closed token -> itself, verbatim (the raising fence owns the vocabulary).
    Anything else -> :data:`LAUNCH_FAILURE_UNTYPED` (fail-closed: the failure is reported,
    its unusable description is not).
    """
    if raw is None or isinstance(raw, bool):
        return LAUNCH_FAILURE_NONE if raw is None else LAUNCH_FAILURE_UNTYPED
    text = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if not text:
        return LAUNCH_FAILURE_NONE
    return text if _REASON_TOKEN.match(text) else LAUNCH_FAILURE_UNTYPED


def port_launch_failure_reason(port: object) -> str:
    """The typed reason the actuation ``port`` stashed for its last launch. (pure)

    Read as an OPTIONAL typed port capability (the ``heal_lane_column`` /
    ``observe_pair_attestation`` precedent): a port predating #14480 — or a test fake —
    simply has no attribute and yields :data:`LAUNCH_FAILURE_NONE`, so no caller has to
    know which port implementation it was handed. A port whose attribute access itself
    raises is treated the same as absent (an unreadable diagnostic never becomes one).
    """
    try:
        raw = getattr(port, "launch_failure_reason", "")
    except Exception:  # noqa: BLE001 - an unreadable diagnostic is not a diagnostic
        return LAUNCH_FAILURE_NONE
    return normalize_launch_failure_reason(raw)


def launch_failure_detail(
    *,
    status: str,
    detail: str,
    preservation_reasons: Sequence[str] = (),
    reason: str,
) -> str:
    """The compatibility ``detail`` string for one actuation result. (pure)

    Redmine #13933 R11 j#81429 #2 established the shape and #14480 makes it shared: a launch
    leg that stopped with a typed reason renders ``launch:<reason>`` in place of the bare
    ``launch``; EVERY other status / detail is passed through byte-identically, so an
    existing consumer of ``detail`` sees no change outside the one case that previously
    carried no information at all. The typed field is the authority — this string exists so
    text renderers and pre-#14480 log readers are not silently starved.
    """
    fallback = detail or ",".join(preservation_reasons)
    if (
        status == ACTUATION_EFFECT_FAILED
        and (detail or "").strip() == LAUNCH_LEG_DETAIL
        and reason
    ):
        return f"{LAUNCH_LEG_DETAIL}:{reason}"
    return fallback


__all__ = (
    "LAUNCH_FAILURE_NONE",
    "LAUNCH_FAILURE_UNTYPED",
    "LAUNCH_LEG_DETAIL",
    "launch_failure_detail",
    "normalize_launch_failure_reason",
    "port_launch_failure_reason",
)
