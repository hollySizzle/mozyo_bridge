"""Herdr global-inventory adapter for workspace retirement (#14877)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_retirement import (
    WorkspaceRetirementInventory,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (
    read_herdr_inventory,
)


def _agent_set_digest(names: tuple[str, ...]) -> str:
    encoded = json.dumps(
        names,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HerdrWorkspaceRetirementInventory:
    def __init__(
        self,
        *,
        repo_root: Path,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._repo_root = repo_root
        self._env = dict(os.environ if env is None else env)

    def observe(self, workspace_id: str) -> WorkspaceRetirementInventory:
        try:
            view = read_herdr_inventory(self._repo_root, env=self._env)
        except Exception:  # external adapter: every unknown failure is fail-closed
            return WorkspaceRetirementInventory(False, False, 0, _agent_set_digest(()))
        raw_count = view.raw_row_count
        invalid_count = view.invalid_row_count
        complete = (
            isinstance(raw_count, int)
            and not isinstance(raw_count, bool)
            and isinstance(invalid_count, int)
            and not isinstance(invalid_count, bool)
            and raw_count >= 0
            and invalid_count == 0
            and raw_count == len(view.agents)
            and all(getattr(agent, "managed", False) is True for agent in view.agents)
        )
        matching = tuple(
            agent for agent in view.agents if agent.workspace_id == workspace_id
        )
        names = tuple(
            sorted(
                (
                    agent.name
                    if isinstance(agent.name, str) and agent.name
                    else "<unidentified>"
                )
                for agent in matching
            )
        )
        return WorkspaceRetirementInventory(
            readable=bool(view.backend_selected and view.ok),
            projection_complete=complete,
            live_agent_count=len(matching),
            target_agent_set_digest=_agent_set_digest(names),
        )


__all__ = ("HerdrWorkspaceRetirementInventory",)
