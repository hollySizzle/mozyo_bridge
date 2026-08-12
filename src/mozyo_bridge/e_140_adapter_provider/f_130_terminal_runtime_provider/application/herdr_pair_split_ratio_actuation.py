"""Effect-edge layout fence for one managed pair resize (#15227)."""

from __future__ import annotations

from typing import Callable, Optional


def _topology_key(split) -> tuple[object, ...]:
    rect = split.rect
    return (
        split.split_id,
        split.direction,
        rect.x,
        rect.y,
        rect.width,
        rect.height,
    )


def apply_pair_ratio(
    pair,
    split,
    first,
    *,
    direction: str,
    target: float,
    binary: str,
    runner,
    timeout: float,
    env,
    authority_check: Optional[Callable[[], bool]] = None,
) -> tuple[str, str]:
    """Resize only while the pair's fresh subtree and outside boundary stay exact."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_managed_column_scope import (  # noqa: E501
        managed_column_scope,
        managed_column_scope_matches,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
        MAX_RESIZE_PASSES,
        RATIO_APPLIED,
        RATIO_FAILED,
        RATIO_MATCHED,
        RESIZE_CHANGED,
        RESIZE_REFUSED,
        RESIZE_UNCHANGED,
        _GROW_DIRECTION,
        _measure_pair,
        _read_layout,
        _resize,
        ratio_verdict,
        resize_step,
    )

    pane_group = ((pair.first_pane, pair.second_pane),)
    opening_layout = _read_layout(
        pair.first_pane, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if opening_layout is None:
        return RATIO_FAILED, "pane layout could not be read or parsed"
    opening_scope = managed_column_scope(opening_layout, pane_group)
    current_split, current_first, reason = _measure_pair(
        opening_layout, pair, direction
    )
    if (
        opening_scope is None
        or current_split is None
        or current_first is None
        or current_split != split
        or current_first != first
    ):
        return RATIO_FAILED, reason or "the pair layout changed before ratio actuation"
    split, first = current_split, current_first
    topology = _topology_key(split)
    matched, detail = ratio_verdict(split, first, target)
    if matched:
        return RATIO_MATCHED, detail

    for _ in range(MAX_RESIZE_PASSES):
        fresh_layout = _read_layout(
            pair.first_pane, binary=binary, runner=runner, timeout=timeout, env=env
        )
        fresh_split, fresh_first, reason = _measure_pair(
            fresh_layout, pair, direction
        ) if fresh_layout is not None else (None, None, "pane layout could not be read or parsed")
        if (
            fresh_layout is None
            or not managed_column_scope_matches(fresh_layout, opening_scope)
            or fresh_split is None
            or fresh_first is None
            or fresh_split != split
            or fresh_first != first
            or _topology_key(fresh_split) != topology
        ):
            return RATIO_FAILED, reason or "the pair layout changed before resize"
        if authority_check is not None and not authority_check():
            return RATIO_FAILED, "the pair's terminal generation changed before resize"

        distance = abs(split.ratio - target)
        token, amount = resize_step(split.ratio, target, direction)
        actuator_pane = (
            pair.first_pane
            if token == _GROW_DIRECTION[direction]
            else pair.second_pane
        )
        effect = _resize(
            actuator_pane,
            token,
            amount,
            binary=binary,
            runner=runner,
            timeout=timeout,
            env=env,
        )
        if effect == RESIZE_UNCHANGED:
            return RATIO_FAILED, f"herdr reported no change for pane resize; {detail}"
        if effect == RESIZE_REFUSED:
            return RATIO_FAILED, f"herdr refused pane resize; {detail}"
        if effect != RESIZE_CHANGED:
            return RATIO_FAILED, f"herdr did not prove the pane resize effect; {detail}"

        closing_layout = _read_layout(
            pair.first_pane, binary=binary, runner=runner, timeout=timeout, env=env
        )
        if authority_check is not None and not authority_check():
            return RATIO_FAILED, "the pair's terminal generation changed after resize"
        closing_split, closing_first, reason = _measure_pair(
            closing_layout, pair, direction
        ) if closing_layout is not None else (None, None, "pane layout could not be read or parsed")
        if (
            closing_layout is None
            or not managed_column_scope_matches(closing_layout, opening_scope)
            or closing_split is None
            or closing_first is None
            or _topology_key(closing_split) != topology
        ):
            return RATIO_FAILED, reason or "the pair layout changed after resize"
        split, first = closing_split, closing_first
        matched, detail = ratio_verdict(split, first, target)
        if matched:
            return RATIO_APPLIED, detail
        if abs(split.ratio - target) >= distance:
            return RATIO_FAILED, f"herdr stopped moving short of the target; {detail}"
    return RATIO_FAILED, f"the divider did not reach the declared ratio; {detail}"


__all__ = ("apply_pair_ratio",)
