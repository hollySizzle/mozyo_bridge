"""The live composition root for the startup rollback rail (Redmine #13948).

Deliberately thin: four of the six ports it needs are already implemented, reviewed and
live-exercised by :class:`LiveSessionRetireOps` (#13892), so this delegates rather than
re-deriving an inventory read, a runtime read, a composer read, an obligation read or a
pin-matched close. Re-implementing those would fork exactly the observations whose
fail-closed semantics were the expensive part.

What it does NOT inherit is the retirement *policy*: no lifecycle read, no worktree gate,
no ``composer_discard_approval``. Those belong to `session-retire`'s authority, and this
rail must not be able to reach them — an authority you cannot call is one you cannot
accidentally exercise (Answer j#80991: the startup transaction does not extend the generic
pending-composer discard authority).

The one genuinely new port is :meth:`startup_blocker`, which classifies the visible pane
through the #13760 profile matcher so a recognised startup screen can be told apart from
somebody's unsent input. It returns the fixed blocker id and never the pane's text.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    PREPARED_PANE_ABSENT,
    PREPARED_PANE_PRESENT,
    PREPARED_PANE_UNREADABLE,
    PreparedPaneObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_contract import (  # noqa: E501
    StartupRollbackAgentTarget,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_inventory import (  # noqa: E501
    strict_pane_rows as _strict_pane_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_rollback import (  # noqa: E501
    ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE,
    ROLLBACK_DETAIL,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    valid_target,
)


class LiveStartupRollbackOps:
    """Live herdr + state-store ports for :func:`run_session_rollback`."""

    def __init__(self, *, repo_root: Path, env: Optional[Mapping[str, str]] = None) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
            LiveSessionRetireOps,
        )

        self._env = env
        self._retire_ops = LiveSessionRetireOps(repo_root=repo_root, env=env)

    def agent_rows(self) -> Sequence[Mapping[str, object]]:
        return self._retire_ops.agent_rows()

    def runtime_state(self, locator: str) -> str:
        return self._retire_ops.runtime_state(locator)

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]:
        return self._retire_ops.observe_composer(locator)

    def open_obligations(self, workspace_id: str, assigned_names: Sequence[str]):
        return self._retire_ops.open_obligations(workspace_id, assigned_names)

    def supports_conditional_close(self) -> bool:
        """Herdr's locator close has no server-side generation predicate."""
        return False

    def close_agent_participant(
        self,
        *,
        workspace_id: str,
        lane_id: str,
        target: StartupRollbackAgentTarget,
    ) -> tuple[bool, str]:
        return False, ROLLBACK_DETAIL[ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE]

    def close_prepared_pane(
        self,
        *,
        locator: str,
        workspace_id: str,
        tab_id: str,
        expected_terminal_id: str = "",
    ) -> tuple[bool, str]:
        return False, ROLLBACK_DETAIL[ROLLBACK_CONDITIONAL_CLOSE_UNAVAILABLE]

    def current_generation_targets_absent(self, action, targets, *, store_home) -> bool:
        """Use one fresh full snapshot to prove every durable target terminal absent."""
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
            _terminal_bound_action_target_absent,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            terminal_identity_snapshot_complete,
        )

        participants = {participant.role: participant for participant in action.participants}
        try:
            rows = tuple(self.agent_rows())
            return bool(
                terminal_identity_snapshot_complete(rows)
                and all(
                    role in participants
                    and participants[role].locator == locator
                    and _terminal_bound_action_target_absent(
                        Path(store_home), action, participants[role], rows
                    )
                    for role, locator in targets
                )
            )
        except Exception:  # noqa: BLE001 - absence is a positive proof
            return False

    def _environ(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def prepared_pane(
        self, *, locator: str, workspace_id: str, tab_id: str,
        expected_terminal_id: str = "",
    ) -> PreparedPaneObservation:
        """Read the exact Herdr 0.8 pane facts; never infer an empty input buffer."""
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
            _invoke,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            COMMAND_TIMEOUT_SECONDS,
        )

        if (
            not valid_target(locator)
            or not valid_target(workspace_id)
            or not valid_target(tab_id)
            or _workspace_prefix(locator) != workspace_id
        ):
            return PreparedPaneObservation(
                state=PREPARED_PANE_UNREADABLE,
                detail="prepared pane authority contains an invalid container identity",
            )
        try:
            binary = self._retire_ops._binary()
            listed = _invoke(
                binary,
                ["pane", "list"],
                subprocess.run,
                COMMAND_TIMEOUT_SECONDS,
                env=self._environ(),
            )
            rows = _strict_pane_rows(listed.stdout)
        except Exception:  # noqa: BLE001 - unreadable inventory is never absence
            return PreparedPaneObservation(
                state=PREPARED_PANE_UNREADABLE,
                detail="the complete Herdr pane inventory could not be read",
            )
        matches = [row for row in rows if row["pane_id"] == locator]
        terminal_reclaimed = (
            any(
                row["pane_id"] != locator
                and row.get("terminal_id") == expected_terminal_id
                for row in rows
            )
            if expected_terminal_id
            else None
        )
        if not matches:
            return PreparedPaneObservation(
                state=PREPARED_PANE_ABSENT,
                terminal_reclaimed=terminal_reclaimed,
            )
        if len(matches) != 1:
            return PreparedPaneObservation(
                state=PREPARED_PANE_UNREADABLE,
                detail="the Herdr pane inventory contains a duplicate locator",
            )
        row = matches[0]
        row_workspace = row["workspace_id"]
        row_tab = row["tab_id"]
        terminal_id = (
            row.get("terminal_id")
            if type(row.get("terminal_id")) is str
            else ""
        )
        if row_workspace != workspace_id or row_tab != tab_id:
            return PreparedPaneObservation(
                state=PREPARED_PANE_PRESENT,
                locator=locator,
                workspace_id=row_workspace,
                tab_id=row_tab,
                terminal_id=terminal_id,
                terminal_reclaimed=terminal_reclaimed,
                detail="the recorded container does not match the live pane inventory",
            )
        agent_absent = "agent" not in row
        if not agent_absent:
            return PreparedPaneObservation(
                state=PREPARED_PANE_PRESENT,
                locator=locator,
                workspace_id=row_workspace,
                tab_id=row_tab,
                terminal_id=terminal_id,
                terminal_reclaimed=terminal_reclaimed,
                detail="the prepared pane now contains an agent",
            )
        shell_only = _read_shell_only(
            binary=binary,
            locator=locator,
            env=self._environ(),
        )
        if shell_only is not True:
            return PreparedPaneObservation(
                state=PREPARED_PANE_PRESENT,
                locator=locator,
                workspace_id=row_workspace,
                tab_id=row_tab,
                terminal_id=terminal_id,
                terminal_reclaimed=terminal_reclaimed,
                agent_absent=True,
                shell_only=False,
                detail="the prepared pane could not be proven to contain only its shell",
            )
        # Herdr 0.8 exposes rendered text but no authoritative shell input-buffer fact.
        # `recent` is empty even with unsent text, while prompt parsing would be heuristic.
        # Preserve the pane until Herdr provides an explicit positive surface.
        return PreparedPaneObservation(
            state=PREPARED_PANE_PRESENT,
            locator=locator,
            workspace_id=row_workspace,
            tab_id=row_tab,
            terminal_id=terminal_id,
            terminal_reclaimed=terminal_reclaimed,
            agent_absent=True,
            shell_only=True,
            input_empty=None,
            detail=(
                "Herdr 0.8 does not expose an authoritative empty-input fact for a "
                "shell pane; refusing to infer one from rendered prompt text"
            ),
        )

    def startup_blocker(self, provider: str, locator: str) -> str:
        """The matched #13760 startup-blocker id for this pane, or ``""``.

        Never returns pane text and never answers the screen. An unreadable pane, an
        unprofiled provider and a clear screen all yield ``""`` — the caller's composer
        read is what then distinguishes "unreadable" from "empty", so a failure here can
        never be mistaken for evidence that the pane is clear.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
            ADMISSION_BLOCKED,
            evaluate_startup_admission,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501
            live_visible_reader,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            COMMAND_TIMEOUT_SECONDS,
            resolve_herdr_binary,
        )

        import subprocess

        try:
            binary = resolve_herdr_binary(self._env or {})
        except Exception:  # noqa: BLE001 - an unresolvable binary classifies nothing
            return ""
        reader = live_visible_reader(binary, subprocess.run, COMMAND_TIMEOUT_SECONDS)
        admission = evaluate_startup_admission(
            provider_id=provider, read_visible=lambda: reader(locator)
        )
        return admission.blocker_id if admission.outcome == ADMISSION_BLOCKED else ""


def _read_shell_only(
    *, binary: str, locator: str, env: Optional[Mapping[str, str]]
) -> Optional[bool]:
    """Return ``True`` only for Herdr's exact one-foreground-shell process shape."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
        _invoke,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
        COMMAND_TIMEOUT_SECONDS,
    )

    try:
        completed = _invoke(
            binary,
            ["pane", "process-info", "--pane", locator],
            subprocess.run,
            COMMAND_TIMEOUT_SECONDS,
            env=env,
        )
        payload = json.loads(completed.stdout)
        result = payload.get("result") if isinstance(payload, Mapping) else None
        info = result.get("process_info") if isinstance(result, Mapping) else None
        if not isinstance(result, Mapping) or result.get("type") != "pane_process_info":
            return None
        if not isinstance(info, Mapping) or info.get("pane_id") != locator:
            return None
        shell_pid = info.get("shell_pid")
        foreground_group = info.get("foreground_process_group_id")
        foreground = info.get("foreground_processes")
        foreground_pid = (
            foreground[0].get("pid")
            if isinstance(foreground, list)
            and len(foreground) == 1
            and isinstance(foreground[0], Mapping)
            else None
        )
        if (
            isinstance(shell_pid, bool)
            or not isinstance(shell_pid, int)
            or shell_pid <= 0
            or isinstance(foreground_group, bool)
            or not isinstance(foreground_group, int)
            or foreground_group != shell_pid
            or not isinstance(foreground, list)
            or len(foreground) != 1
            or not isinstance(foreground[0], Mapping)
            or isinstance(foreground_pid, bool)
            or not isinstance(foreground_pid, int)
            or foreground_pid != shell_pid
            or not isinstance(foreground[0].get("argv0"), str)
            or not foreground[0].get("argv0")
        ):
            return False
        return True
    except Exception:  # noqa: BLE001 - unreadable process facts prove nothing
        return None


__all__ = ("LiveStartupRollbackOps",)
