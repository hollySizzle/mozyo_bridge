"""Use case for the side-effect-zero Herdr offline rollout plan (#14838)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
    OfflineRolloutCapture,
    OfflineRolloutPlanResult,
    build_offline_rollout_plan,
)


def run_offline_rollout_plan(
    *,
    repo_root: Path,
    home: Path,
    candidate_version: str,
    candidate_source_sha: str = "",
    candidate_source_ref: str = "",
    candidate_workflow_run_id: str = "",
    candidate_wheel_sha256: str = "",
    candidate_sdist_sha256: str = "",
    env: Optional[Mapping[str, str]] = None,
    snapshot_reader: Optional[Callable] = None,
) -> OfflineRolloutPlanResult:
    """Capture and plan; never stop, migrate, install, publish or relaunch anything."""
    if snapshot_reader is None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_snapshot import (  # noqa: E501
            capture_offline_rollout_snapshot,
        )

        snapshot_reader = capture_offline_rollout_snapshot
    captured = snapshot_reader(
        repo_root=repo_root,
        home=home,
        candidate_version=candidate_version,
        candidate_source_sha=candidate_source_sha,
        candidate_source_ref=candidate_source_ref,
        candidate_workflow_run_id=candidate_workflow_run_id,
        candidate_wheel_sha256=candidate_wheel_sha256,
        candidate_sdist_sha256=candidate_sdist_sha256,
        env=env,
    )
    if isinstance(captured, OfflineRolloutPlanResult):
        return captured
    if not isinstance(captured, OfflineRolloutCapture):
        raise TypeError("snapshot_reader must return OfflineRolloutCapture or plan refusal")
    return build_offline_rollout_plan(captured)


__all__ = ("run_offline_rollout_plan",)
