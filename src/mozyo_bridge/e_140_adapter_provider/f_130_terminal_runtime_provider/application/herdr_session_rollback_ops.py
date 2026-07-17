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

from pathlib import Path
from typing import Mapping, Optional, Sequence


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

    def close(self, workspace_id: str, lane_id: str, targets):
        return self._retire_ops.close(workspace_id, lane_id, targets)

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


__all__ = ("LiveStartupRollbackOps",)
