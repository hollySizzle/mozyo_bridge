"""Codex tool-shell identity propagation for herdr-managed agents.

The herdr launcher already attests the workspace/provider/lane tuple before it
starts an agent.  Codex applies a separate environment policy to tool-shell
subprocesses, so the same tuple must also be expressed as Codex configuration.
Only the three identity variables are set; ambient environment inheritance is
not widened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    MOZYO_AGENT_ROLE_ENV,
    MOZYO_LANE_ID_ENV,
    MOZYO_WORKSPACE_ID_ENV,
    PROVIDER_CODEX,
)


@dataclass(frozen=True)
class CodexShellIdentity:
    """Render an attested identity as Codex subprocess-policy overrides."""

    workspace_id: str
    lane_id: str

    def launch_argv(self) -> tuple[str, ...]:
        values = (
            (MOZYO_WORKSPACE_ID_ENV, self.workspace_id),
            (MOZYO_AGENT_ROLE_ENV, PROVIDER_CODEX),
            (MOZYO_LANE_ID_ENV, self.lane_id),
        )
        argv: list[str] = []
        for key, value in values:
            # JSON strings are valid TOML basic strings and safely preserve any
            # lane label accepted by the upstream identity resolver.
            argv.extend(
                (
                    "-c",
                    f"shell_environment_policy.set.{key}="
                    f"{json.dumps(value, ensure_ascii=True)}",
                )
            )
        return tuple(argv)


__all__ = ["CodexShellIdentity"]
