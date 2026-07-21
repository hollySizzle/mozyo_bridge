"""Launch admission against the durable lane disposition (Redmine #14242 review j#85296 F3).

The ORDER half of the launch / terminalize exclusion, extracted as its own leaf so the launch
funnel stays under the module-health budget.

The #13882 attestation-store lock serializes a launch and a terminal retire while both are *in
flight*, but it is released once the terminal CAS commits. Without this admission an ordinary
``prepare_session`` could then acquire the shared lock and spawn into the lane that was just
terminalized, recreating a live pair under a ``retired`` row (reproduced in review j#85296:
``before_disposition=retired`` -> ``launched`` -> ``after_disposition=retired``). Serializing the
concurrent window is not sufficient; the resulting ORDER has to be admitted from the durable
disposition as well.

Deliberately narrow — this runs on every managed launch, so it refuses exactly one thing:

- the **default / coordinator** lane and any **rowless** lane are unaffected. A scratch ``herdr
  session-start`` pair and the bare ``mozyo`` coordinator pair own no lifecycle row by design
  (#13882), so there is nothing to contradict and their behaviour is byte-unchanged.
- ``active`` / ``superseded`` / ``hibernated`` launch exactly as before — including a lane
  re-incarnated by an explicit ``open_next_generation``, which returns the row to ``active`` with
  the next generation (the sanctioned re-launch path, ``managed-state-model.md``).
- only a ``retired`` row refuses, with zero Herdr side effect.

An UNREADABLE store refuses for a named lane rather than launching blind: this component's
standing rule is that unreadable is not absent. The blast radius is bounded to named lanes — the
default / coordinator lane never consults the store at all, so an operator can always still start
a coordinator pair on a broken store.

Boundary: one non-migrating read (Redmine #13844) and a decision. No write, no process effect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    _norm,
)

#: The lane dispositions a managed launch may spawn into. ``retired`` is TERMINAL:
#: re-incarnation is the explicit ``open_next_generation`` path only, so spawning into a retired
#: row would create a live pair the durable record says is gone.
LAUNCH_ADMISSIBLE_DISPOSITIONS = frozenset({"active", "superseded", "hibernated"})


def admit_launch_against_lifecycle(
    *,
    workspace_id: str,
    lane_id: str,
    store_home: str,
    error_type: Optional[type] = None,
) -> None:
    """Refuse a spawn into a terminally ``retired`` lane, before any Herdr write (#14242 F3).

    Called from ``_prepare_session_locked`` after the canonical ``(workspace_id, lane_id)``
    resolve and BEFORE the first Herdr workspace / tab / agent write, so it runs inside the
    caller-held shared lock on BOTH entry paths — the ordinary one and the v1 replacement's
    direct ``admission_lock_held=True`` call. Placing it on ``prepare_session`` instead would let
    that v1 path bypass it.

    ``error_type`` defaults to the launch module's own error (resolved by deferred import, so
    there is no module-level cycle) and may be injected by a caller that wants its own type.
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    if error_type is None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError as error_type,
        )

    lane = _norm(lane_id)
    if not lane or lane == DEFAULT_LANE:
        return  # the rowless coordinator / scratch pair: unchanged by contract
    try:
        key = LaneLifecycleKey(_norm(workspace_id), lane)
    except ValueError:
        return  # not a keyable lane unit; nothing durable to contradict
    home: Optional[Path] = Path(store_home) if store_home else None
    try:
        record = LaneLifecycleStore(home=home).get(key)
    except (LaneLifecycleError, OSError) as exc:
        raise error_type(
            f"managed-launch admission refused: the lane lifecycle store is unreadable "
            f"({type(exc).__name__}), so it cannot be proven that lane {lane!r} is not "
            f"terminally retired. No workspace / tab / agent was created."
        ) from exc
    if record is None:
        return  # a rowless lane (scratch / not yet declared): unchanged
    disposition = _norm(record.lane_disposition)
    if disposition in LAUNCH_ADMISSIBLE_DISPOSITIONS:
        return
    raise error_type(
        f"managed-launch admission refused: lane {lane!r} is durably {disposition!r} and a "
        f"retired generation is terminal. Re-incarnate it explicitly (open_next_generation) "
        f"before launching. No workspace / tab / agent was created."
    )


__all__ = ("LAUNCH_ADMISSIBLE_DISPOSITIONS", "admit_launch_against_lifecycle")
