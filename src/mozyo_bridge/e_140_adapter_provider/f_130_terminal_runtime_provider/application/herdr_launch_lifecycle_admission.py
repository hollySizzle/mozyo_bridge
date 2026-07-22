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

from mozyo_bridge.core.state.lane_kind import LaneKindError, checked_lane_kind
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    _norm,
)

#: The lane dispositions a managed launch may spawn into. ``retired`` is TERMINAL:
#: re-incarnation is the explicit ``open_next_generation`` path only, so spawning into a retired
#: row would create a live pair the durable record says is gone.
LAUNCH_ADMISSIBLE_DISPOSITIONS = frozenset({"active", "superseded", "hibernated"})


def _checked_stored_lane_kind(
    stored: str, *, lane: str, error_type: type
) -> Optional[str]:
    """The row's ``lane_kind`` as a CANONICAL token, or fail closed (#13647 review j#85848 F2).

    ``stored`` is the row's byte-exact value: it is never trimmed or otherwise normalized
    before the check (review j#85852 F1), because normalizing first would decide the closed
    vocabulary on a value the store does not hold — ``" implementation "`` would pass as a
    canonical kind and ``"   "`` would pass as the legacy blank.

    EXACTLY ``""`` is the one legitimate absence: a pre-v7 / legacy lane simply has no
    durable kind fact, so the launch falls back to ``lane_class`` geometry exactly as
    before. Anything ELSE that is not one of the three canonical tokens — including a padded
    or whitespace-only token — is an authority value this build cannot interpret — a tampered row, a foreign writer, or a vocabulary from a future build
    — and this component's standing rule is that uninterpretable is NOT absent. Silently
    treating it as "no kind" would place the pair by a geometry the durable record does not
    actually say, which is the guess the whole design exists to prevent (disposition j#85650:
    invalid / ambiguous is zero-start, only blank falls back).

    Raised here, at the same pre-side-effect boundary as the disposition refusal, so the
    refusal costs no workspace / tab / agent.
    """
    if stored == "":
        return None  # no durable kind fact: the sanctioned lane_class fallback
    try:
        return checked_lane_kind(stored, source=f"lane {lane!r} stored lane_kind")
    except LaneKindError as exc:
        raise error_type(
            f"managed-launch admission refused: lane {lane!r} has a durably recorded "
            f"lane-kind this build cannot interpret ({exc}). An uninterpretable authority "
            f"value is not an absent one, so the lane's pane geometry cannot be resolved "
            f"without guessing. Repair the lifecycle record (re-declare, or re-bind at a "
            f"generation boundary) before launching. No workspace / tab / agent was created."
        ) from exc


def _admitted_lane_kind(
    *,
    workspace_id: str,
    lane_id: str,
    store_home: str,
    error_type: Optional[type] = None,
) -> Optional[str]:
    """Refuse a spawn into a terminally ``retired`` lane, before any Herdr write (#14242 F3).

    Called from ``_prepare_session_locked`` after the canonical ``(workspace_id, lane_id)``
    resolve and BEFORE the first Herdr workspace / tab / agent write, so it runs inside the
    caller-held shared lock on BOTH entry paths — the ordinary one and the v1 replacement's
    direct ``admission_lock_held=True`` call. Placing it on ``prepare_session`` instead would let
    that v1 path bypass it.

    ``error_type`` defaults to the launch module's own error (resolved by deferred import, so
    there is no module-level cycle) and may be injected by a caller that wants its own type.

    Returns the lane's stored generation-bound ``lane_kind`` (Redmine #13647 Tranche 1b) — the
    lane-role placement **heal authority** — or ``None`` when there is no row / no durable kind
    fact. Additive: the admission decision is unchanged, and a caller that only wants the
    refusal ignores the value. This is the one read the boundary already performs, so the heal
    authority costs no second open and is resolved from the SAME snapshot the admission used.
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
        return None  # the rowless coordinator / scratch pair: unchanged by contract
    try:
        key = LaneLifecycleKey(_norm(workspace_id), lane)
    except ValueError:
        return None  # not a keyable lane unit; nothing durable to contradict
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
        return None  # a rowless lane (scratch / not yet declared): unchanged
    disposition = _norm(record.lane_disposition)
    if disposition in LAUNCH_ADMISSIBLE_DISPOSITIONS:
        # The RAW stored token — deliberately NOT trimmed (review j#85852 F1). Trimming
        # first would make the closed-vocabulary check judge a value the store does not
        # actually hold, so `' implementation '` would be silently normalized into a valid
        # kind and a whitespace-only token would masquerade as the legacy blank.
        return _checked_stored_lane_kind(
            getattr(record, "lane_kind", "") or "", lane=lane, error_type=error_type
        )
    raise error_type(
        f"managed-launch admission refused: lane {lane!r} is durably {disposition!r} and a "
        f"retired generation is terminal. Re-incarnate it explicitly (open_next_generation) "
        f"before launching. No workspace / tab / agent was created."
    )


def admit_launch_against_lifecycle(
    *,
    workspace_id: str,
    lane_id: str,
    store_home: str,
    launch_context: object = None,
    dry_run: bool = False,
    error_type: Optional[type] = None,
) -> Optional[str]:
    """Admit the launch AND resolve the lane-kind this launch places by (Redmine #13647 T1b).

    The #14242 F3 admission entry point, unchanged in name and in refusal semantics (its
    callers, including that issue's ordered regressions, keep calling it exactly as before).
    Redmine #13647 T1b widens it additively: the boundary read it already performs also
    yields the lane's placement geometry, so the launch resolves BOTH from one snapshot.

    The single call the launch chokepoint makes at its pre-side-effect boundary. It returns
    the ``lane_kind`` placement geometry must key on, resolved from exactly two authorities
    and never from a display cache / provider / pane proximity (disposition j#85650):

    - the **fresh-launch** authority is the caller-supplied ``launch_context`` — the creating
      caller's durable governance fact, resolved at the create boundary;
    - the **heal** authority is the lane's generation-bound stored ``lane_kind``, read here
      OFFLINE from the lifecycle authority record (never re-read from Redmine). A relaunch of
      an existing lane therefore reproduces the geometry that lane was created with, with no
      network and no caller state, which is the whole point of storing it (P1, j#85650).

    Reconciliation is fail-closed, not last-writer-wins: when BOTH are present and they
    **differ**, this is a genuine contradiction about the same generation of the same lane —
    one of the two facts is stale — so the launch refuses with zero Herdr side effect rather
    than silently picking one and placing the pair somewhere the durable record contradicts.
    A caller that genuinely re-binds a lane's geometry does so at the generation boundary
    (``open_next_generation(lane_kind=…)``), which is the only sanctioned re-bind.

    Otherwise whichever single fact is present wins (they agree when both are), and ``None``
    — neither authority has a kind — falls through to ``lane_class`` geometry, byte-for-byte
    the pre-#13647 placement.

    ``dry_run`` consults NO durable state and returns the caller's context kind alone: a dry
    run is side-effect free *and* store-free by contract (Redmine #13595 / #14242 — it does
    not consult the disposition either), so it plans from fresh-launch authority only.
    """
    # The context validated its own token on construction (`LaneLaunchContext` runs the same
    # closed-vocabulary check), so it is already canonical or None — nothing to normalize.
    context_kind = getattr(launch_context, "lane_kind", None) or None
    if dry_run:
        return context_kind
    stored_kind = _admitted_lane_kind(
        workspace_id=workspace_id,
        lane_id=lane_id,
        store_home=store_home,
        error_type=error_type,
    )
    if stored_kind is None or context_kind is None or stored_kind == context_kind:
        return context_kind or stored_kind
    if error_type is None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError as error_type,
        )
    raise error_type(
        f"managed-launch admission refused: lane {_norm(lane_id)!r} is durably recorded as "
        f"lane-kind {stored_kind!r} for its current generation, but this launch was handed "
        f"{context_kind!r}. One of the two is stale, so the lane's pane geometry cannot be "
        f"resolved without guessing. Re-bind it explicitly at a generation boundary "
        f"(open_next_generation) or launch with the recorded kind. No workspace / tab / "
        f"agent was created."
    )


__all__ = ("LAUNCH_ADMISSIBLE_DISPOSITIONS", "admit_launch_against_lifecycle")
