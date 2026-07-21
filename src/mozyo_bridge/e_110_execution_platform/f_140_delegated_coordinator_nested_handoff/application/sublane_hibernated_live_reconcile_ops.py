"""Live observation adapter for the hibernated live-contradiction reconcile (Redmine #13842).

Split out of :mod:`...sublane_hibernated_live_reconcile` to keep that orchestration module under
the module-health line threshold. This is the thin live :class:`ReconcileOps` — the raw
``agent list`` inventory, the ``agent get`` runtime state, a content-free composer observation
over ``read_pane``, and the startup self-attestation store — reusing the same herdr readers the
#13763 quarantine inspection uses so the reconcile and the quarantine read one runtime the same
way. Every provider import stays lazy inside the methods (the pure decision + orchestration never
require the terminal infrastructure).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_ghost_composer_gate import (  # noqa: E501
    GhostComposerRenderPolicy,
    RenderGhostFacts,
)


@dataclass
class LiveReconcileOps:
    """The live :class:`ReconcileOps`: raw inventory + per-slot runtime / composer / attestation.

    Reuses the same readers the #13763 quarantine inspection uses — the raw ``agent list``
    inventory, the ``agent get`` runtime state, a content-free composer observation over
    ``read_pane``, and the startup self-attestation store — so the reconcile and the quarantine
    read one runtime the same way.
    """

    repo_root: Path
    env: Optional[Mapping[str, str]] = None
    #: Redmine #14065 Phase 2: injected ghost-composer render policy. ``None`` (default)
    #: keeps the render gate OFF — a text pending composer is preserved exactly as before
    #: — so this rail is byte-unchanged unless a caller opts in.
    ghost_policy: Optional[GhostComposerRenderPolicy] = None
    #: Optional facts reader for hermetic tests; ``None`` uses the authority-resolved read.
    render_facts_reader: Optional[Callable[[str], RenderGhostFacts]] = None

    def _environ(self) -> Mapping[str, str]:
        return self.env if self.env is not None else os.environ

    def agent_rows(self) -> Sequence[Mapping[str, object]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        return list_herdr_agent_rows(self._environ())

    def read_attestation(self, assigned_name: str):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        try:
            return HerdrIdentityAttestationStore().read(assigned_name)
        except Exception:  # noqa: BLE001 - unreadable attestation fails closed (absent)
            return None

    def _reader(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            _resolve_binary_or_die,
        )

        return _resolve_binary_or_die(self._environ())

    def runtime_state(self, locator: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E501
            HerdrCliAgentStateReader,
        )

        try:
            binary = self._reader()
            state = HerdrCliAgentStateReader(binary).read_agent_state(locator)
            return state.state if state.ok else "unknown"
        except Exception:  # noqa: BLE001 - a failed runtime read is fail-soft to unknown
            return "unknown"

    def observe_composer(self, locator: str) -> tuple[bool, Optional[bool]]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
            observe_composer_text,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            HerdrCliTransport,
        )

        try:
            binary = self._reader()
            read = HerdrCliTransport(binary).read_pane(locator, lines=80)
            if not read.ok:
                return (False, None)
            observation = observe_composer_text(read.content)
            # Redmine #14065 Phase 2: a dim ghost the provider declares empties the text
            # candidate at action time; everything else preserves (see apply_ghost_empty).
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
                apply_ghost_empty,
            )

            has_pending = apply_ghost_empty(
                observation.has_pending,
                policy=self.ghost_policy,
                repo_root=self.repo_root,
                env=self._environ(),
                locator=locator,
                facts_reader=self.render_facts_reader,
            )
            return (observation.readable, has_pending)
        except Exception:  # noqa: BLE001 - a failed composer read is fail-soft to unreadable
            return (False, None)


__all__ = ("LiveReconcileOps",)
