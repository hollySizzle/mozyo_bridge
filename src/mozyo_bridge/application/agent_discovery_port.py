"""Port boundary over the agent target-discovery reads (Redmine #12749 / #12638).

Third OOP-first port for the ``commands.py`` decomposition. The shared
``agents targets`` / attention candidate pipeline (``_agents_target_candidates``)
mixes pure classification (fold / filter / ``build_target_candidates``) with four
*external reads*: live tmux agent discovery, registry canonical-session
resolution, a git checkout probe, and bounded project-scope discovery. The
procedural pipeline called those as module-level functions, and its tests patched
``mozyo_bridge.application.commands.resolve_canonical_session`` /
``commands._probe_checkout_facts`` (plus the domain ``pane_lines``).

This module defines :class:`AgentDiscoveryPort` — the four external reads the
:class:`~mozyo_bridge.application.commands_agents.ResolveAgentTargetsUseCase`
depends on — so that use case is decoupled from the concrete reads and
unit-testable with a fake port (no monkeypatch). The pure classification stays in
the domain (``fold_agents_by_pane`` / ``filter_agents`` /
``build_target_candidates``); only the boundary is abstracted here.

Compatibility bridge (transitional): two of the live reads —
``resolve_canonical_session`` (imported into ``commands``) and the cockpit-shared
``_probe_checkout_facts`` (defined in ``commands.py``) — are still owned by
``commands``. :class:`LiveAgentDiscovery` reaches them through the ``commands``
module *at call time* so the existing read-discovery tests that patch
``commands.resolve_canonical_session`` / ``commands._probe_checkout_facts`` keep
working unchanged while the use case gains its port seam. Relocating those leaf
reads out of ``commands`` (and migrating their broad ``commands.*`` monkeypatch
tests) is the residual carried to #12638 / #12785. This is a read-only discovery
boundary; it issues no tmux send-keys / routing.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    discover_agents as _discover_agents,
)


@runtime_checkable
class AgentDiscoveryPort(Protocol):
    """The external reads the agent-target candidate use case depends on."""

    def discover(self) -> list:
        """Live agent discovery (one tmux read), returning raw agent records."""
        ...

    def canonical_session(self, repo_root: str):
        """Resolve a repo root's canonical session (read-only, no defaults open)."""
        ...

    def checkout_facts(self, repo_root: str) -> dict:
        """Tolerant git checkout probe (``branch`` etc.); never raises."""
        ...

    def project_scope(self, cwd: str, repo_root: Optional[str]):
        """Bounded project-scope discovery for a pane cwd, or ``None`` (fail-soft)."""
        ...


class LiveAgentDiscovery:
    """Live adapter for the agent-target discovery reads.

    ``discover`` and ``project_scope`` delegate straight to the domain /
    application reads. ``canonical_session`` / ``checkout_facts`` route through the
    ``commands`` module at call time (see the module docstring's compatibility
    bridge note) so the residual ``commands``-owned leaf reads — and the tests that
    patch them — stay intact during the migration.
    """

    def discover(self) -> list:
        return _discover_agents()

    def canonical_session(self, repo_root: str):
        # ``derive_unregistered=False``: read-only discovery hot path — a
        # never-registered workspace degrades to the path-hash fallback rather than
        # opening its (possibly dataless) workspace-local defaults (Redmine #12038).
        from mozyo_bridge.application import commands as _commands

        return _commands.resolve_canonical_session(repo_root, derive_unregistered=False)

    def checkout_facts(self, repo_root: str) -> dict:
        from mozyo_bridge.application import commands as _commands

        return _commands._probe_checkout_facts(repo_root)

    def project_scope(self, cwd: str, repo_root: Optional[str]):
        try:
            from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
                project_scope_for_cwd,
            )

            return project_scope_for_cwd(cwd, repo_root)
        except Exception:  # noqa: BLE001 - read-only display, never block the listing
            return None
