"""Read-only whole managed-launch admission before a replacement close."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    resolve_attest_launcher,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    replacement_store_admission,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_preflight import (  # noqa: E501
    preflight_managed_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    HerdrLauncherIncompatibleError,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


REPLACEMENT_LAUNCHER_UNAVAILABLE = "launcher_runtime_incompatible"


def replacement_managed_launch_admission(
    workspace_id: str,
    lane_id: str,
    *,
    repo_root: Path,
    env: Mapping[str, str],
    runner,
    timeout: float,
    lifecycle_home: str = "",
    attestation_home: str = "",
) -> str | None:
    """Return a pre-close refusal from the same conjunction the launch will use.

    Store shape is checked first without a subprocess. If it is current, the exact selected
    wrapper, environment, cwd and home are passed to :func:`preflight_managed_launch`.
    Replacement recovery requires a wrapper: an unwrapped launch cannot create the terminal-
    bound attestation/generation proof needed to complete the already-destructive action.
    """
    refusal = replacement_store_admission(
        workspace_id,
        lane_id,
        lifecycle_home=lifecycle_home,
        attestation_home=attestation_home,
    )
    if refusal:
        return refusal
    launcher = resolve_attest_launcher(env)
    if not launcher:
        return REPLACEMENT_LAUNCHER_UNAVAILABLE
    selected_home = Path(attestation_home) if attestation_home else mozyo_bridge_home()
    try:
        preflight_managed_launch(
            launcher,
            runner if runner is not None else subprocess.run,
            timeout,
            env,
            repo_root=Path(repo_root),
            store_home=selected_home,
            workspace_id=workspace_id,
            lane_id=lane_id,
            replacement_action_id="replacement-preclose",
            launch_planned=True,
        )
    except HerdrLauncherIncompatibleError as exc:
        return exc.reason
    except (HerdrSessionStartError, OSError, ValueError):
        return REPLACEMENT_LAUNCHER_UNAVAILABLE
    return None


__all__ = (
    "REPLACEMENT_LAUNCHER_UNAVAILABLE",
    "replacement_managed_launch_admission",
)
