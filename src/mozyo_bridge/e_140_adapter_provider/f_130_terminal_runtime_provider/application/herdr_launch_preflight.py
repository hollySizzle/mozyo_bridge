"""The session-level managed-launch preflight entry point (Redmine #14756).

One named thing a composition root can say — "verify the launcher I am about to use" —
holding the argument assembly for the compatibility conjunction in
:mod:`herdr_pane_lifecycle`. The assembly is behaviour, not glue: it decides *whether* the
conjunction runs at all and derives each fail-closed flag from raw inputs, with the reasoning
attached. Keeping it beside the orchestrator meant `herdr_session_start` carried it; keeping
it inside `herdr_pane_lifecycle` pushed that module to 998 of a 1000-line gate. Neither is a
home, so this is one (the #13948 j#80989 rule: "new module, do not grow the modules already
near the ceiling" — the same rule that produced `herdr_slot_execution`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    launch_carries_lane_epoch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    preflight_launcher_compatibility,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    Runner,
)


def preflight_managed_launch(
    attest_launcher: str,
    runner: Runner,
    timeout: float,
    env: Mapping[str, str],
    *,
    repo_root,
    store_home: Path,
    workspace_id: str,
    lane_id: str,
    replacement_action_id: str = "",
    launch_planned: bool = True,
) -> None:
    """The managed-launch compatibility boundary for ONE session-start run.

    The named entry point for what a composition root actually wants to say — "verify the
    launcher I am about to use" — instead of assembling
    :func:`preflight_launcher_compatibility`'s arguments inline. The assembly is not
    incidental: it decides *whether* the conjunction runs at all, derives
    ``replacement_launch`` and ``epoch_launch`` from raw inputs, and carries the reasoning for
    each. That is behaviour, and it belongs beside the conjunction it configures rather than
    in the orchestrator (the #13948 j#80989 split rule, which is also why
    ``herdr_slot_execution`` exists).

    Gated on a resolved wrapper AND an actual launch plan. An unwrapped launch establishes no
    generation authority and remains unattested/non-green, while adopt-only / dry-run stays
    byte-invariant. For a wrapped launch the generation store probe runs before native binding
    or startup transaction writes. This is the last pre-effect compatibility boundary, so a
    skewed launcher aborts with zero durable write and zero actuation.

    ``epoch_launch`` (Redmine #14756) is resolved here through the SAME predicate the
    per-slot launch uses, so the preflight and the launch cannot disagree about whether an
    epoch is injected — a preflight with its own notion of that would either admit a launch
    that then injects one, or refuse one that would not have.
    """
    if not (attest_launcher and launch_planned):
        return
    # Redmine #14231 j#84910: probe in the SAME cwd the wrapper will get
    # (`build_agent_start_argv` passes `--cwd repo_root`), so a launcher that only fails
    # inside the lane's own config directory is caught here too.
    preflight_launcher_compatibility(
        attest_launcher,
        runner,
        timeout,
        env,
        repo_root=repo_root,
        store_home=Path(store_home),
        replacement_launch=bool((replacement_action_id or "").strip()),
        epoch_launch=launch_carries_lane_epoch(
            workspace_id, lane_id, store_home=str(store_home)
        ),
    )


__all__ = ("preflight_managed_launch",)
