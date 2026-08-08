"""Pure parser for Herdr 0.8 pane-command mutation evidence (#14608).

Successful process exit is not proof that a pane changed.  The bundled Herdr
0.8 schema carries that evidence under a command-specific nested envelope, so
callers share one strict parser and treat schema drift as unknown.
"""

from __future__ import annotations

import json


EFFECT_CHANGED = "changed"
EFFECT_UNCHANGED = "unchanged"
EFFECT_UNKNOWN = "unknown"


def parse_changed_effect(
    raw: str, *, result_type: str, envelope: str
) -> str:
    """Return a closed effect from ``result.<envelope>.changed``.

    The result type and boolean must both match exactly.  Missing, malformed,
    future, or wrong-command responses are unknown; raw response text is never
    retained or rendered.
    """

    try:
        result = json.loads(raw)["result"]
        if result["type"] != result_type:
            return EFFECT_UNKNOWN
        changed = result[envelope]["changed"]
    except (KeyError, TypeError, ValueError):
        return EFFECT_UNKNOWN
    if changed is True:
        return EFFECT_CHANGED
    if changed is False:
        return EFFECT_UNCHANGED
    return EFFECT_UNKNOWN


__all__ = (
    "EFFECT_CHANGED",
    "EFFECT_UNCHANGED",
    "EFFECT_UNKNOWN",
    "parse_changed_effect",
)
