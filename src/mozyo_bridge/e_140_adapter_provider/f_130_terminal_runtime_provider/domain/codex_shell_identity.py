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

    @staticmethod
    def _toml_string(value: str) -> str:
        """Render ``value`` as a TOML basic string accepted by Codex ``-c``."""

        # JSON and TOML share the escapes used here, but JSON's default ASCII
        # rendering represents non-BMP characters as UTF-16 surrogate pairs.
        # Surrogates are not TOML Unicode scalar values, so retain real Unicode.
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise ValueError("Codex shell identity contains a non-scalar surrogate")
        return json.dumps(value, ensure_ascii=False)

    def launch_argv(self) -> tuple[str, ...]:
        values = (
            (MOZYO_WORKSPACE_ID_ENV, self.workspace_id),
            (MOZYO_AGENT_ROLE_ENV, PROVIDER_CODEX),
            (MOZYO_LANE_ID_ENV, self.lane_id),
        )
        argv: list[str] = []
        for key, value in values:
            argv.extend(
                (
                    "-c",
                    f"shell_environment_policy.set.{key}="
                    f"{self._toml_string(value)}",
                )
            )
        return tuple(argv)


__all__ = ["CodexShellIdentity"]
