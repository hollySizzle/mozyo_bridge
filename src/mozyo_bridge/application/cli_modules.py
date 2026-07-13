"""Built-in CLI module registry binding and parser composition (Redmine #12155).

This is the application-layer half of the internal CLI module registry. The
pure classification — :class:`~mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry.CliFamily`,
:class:`~mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry.CliCompositionConfig`, and the
ordered :class:`~mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry.BuiltinCliModuleRegistry`
— lives in :mod:`mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry` and imports no
application code. This module binds each classified family *name* to the
built-in registrar callable that adds its subparsers, and composes the parser
by walking the registry in order.

Why the split: the domain registry stays a pure catalogue (no callables, no
argparse), so the dependency only ever points application -> domain. The
registrar map here references only statically-imported built-in functions
(``cli_core`` plus the feature-family modules). There is **no** runtime import,
no module-path lookup, and no entry point — composing the CLI can never load or
execute foreign code. This is the configuration-aware baseline the issue asks
for; external plugin loading stays a non-goal (see
``vibes/docs/logics/plugin-ready-adapter-boundary.md``).

The seeded order reproduces the pre-registry ``build_parser()`` subcommand
sequence exactly, so default composition is byte-compatible with the prior CLI.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Optional

from mozyo_bridge.application import (
    cli_core,
    cli_runtime_config,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application import (
    cli_session,
    cli_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import cli_agents
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import cli_handoff
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application import cli_observability
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application import cli_state
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application import (
    cli_onboarding,
)
from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application import cli_cockpit
from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.application import cli_presentation
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application import cli_docs_scaffold
from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import cli_release
from mozyo_bridge.e_150_quality_architecture.f_130_module_health.application import cli_module_health
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
    cli_project_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.application import (
    cli_redmine_version,
)
from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry import (
    BuiltinCliModuleRegistry,
    CliCompositionConfig,
    CliFamily,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (
    cli_test_impact,
)

# Each entry is (CliFamily description, registrar). The registrar takes the
# top-level subparsers action and adds this family's subparsers. The order is
# the pre-registry ``build_parser()`` order and is the composition order. The
# ``authorities`` / ``core`` flags drive the registry's mandatory rule: families
# carrying a core-owned authority (send safety, workflow / routing authority)
# and the hard core command set cannot be disabled by config.
#
# Registrar references are statically imported built-in functions only — never a
# runtime-resolved module path — so composition never loads foreign code.
_FAMILY_BINDINGS: tuple[tuple[CliFamily, Callable[[object], None]], ...] = (
    (
        CliFamily(
            name="core-base",
            summary="Core status/list commands (read-only session overview).",
            core=True,
        ),
        cli_core.register_top,
    ),
    (
        CliFamily(
            name="cockpit",
            summary="Cockpit layout/projection + tmux-ui-config presentation family.",
        ),
        cli_cockpit.register,
    ),
    (
        CliFamily(
            name="agents",
            summary="Cross-workspace agent discovery (read-only structured surface).",
        ),
        cli_agents.register,
    ),
    (
        CliFamily(
            name="presentation",
            summary=(
                "Desired presentation current-table seed/inspect family "
                "(home-scoped DB; display-only, no routing/approval authority)."
            ),
        ),
        cli_presentation.register,
    ),
    (
        CliFamily(
            name="tmux-ui",
            summary="tmux-ui status-line install/uninstall presentation family.",
        ),
        cli_cockpit.register_tmux_ui,
    ),
    (
        CliFamily(
            name="pane-io",
            summary="Core pane I/O commands (id/resolve/read/type).",
            # `type` delivers input into a live pane: a send primitive.
            authorities=frozenset({"send_safety"}),
            core=True,
        ),
        cli_core.register_pane_io,
    ),
    (
        CliFamily(
            name="message",
            summary="Direct pane `message` delivery command.",
            authorities=frozenset({"send_safety"}),
        ),
        cli_handoff.register_message,
    ),
    (
        CliFamily(
            name="keys",
            summary="Core `keys` raw key-send command.",
            authorities=frozenset({"send_safety"}),
            core=True,
        ),
        cli_core.register_keys,
    ),
    (
        CliFamily(
            name="handoff",
            summary="notify-* / handoff / reply gated cross-agent routing family.",
            authorities=frozenset(
                {
                    "send_safety",
                    "workflow_authority",
                    "routing_authority",
                    "review_authority",
                }
            ),
        ),
        cli_handoff.register,
    ),
    (
        CliFamily(
            name="project-gateway",
            summary=(
                "Semantic department-root -> project-gateway route family "
                "(Redmine #12668): discover / start / handoff a project-scoped "
                "gateway unit across separate window/session surfaces by identity, "
                "fail-closed on missing/ambiguous, without a %pane copy."
            ),
            authorities=frozenset({"send_safety", "routing_authority"}),
        ),
        cli_project_gateway.register,
    ),
    (
        CliFamily(
            name="workflow",
            summary=(
                "Single standard agent/operator workflow entrypoint (Redmine "
                "#12755): `workflow step` advances one safe workflow step by "
                "resolving the next routing/transport action from lane identity + "
                "durable gate + route identity, fail-closed with the next owner. "
                "Dispatches the project-gateway / handoff primitives internally; "
                "hides %pane / q-enter / queue-enter / --mode."
            ),
            authorities=frozenset(
                {"send_safety", "routing_authority", "workflow_authority"}
            ),
        ),
        cli_workflow.register,
    ),
    (
        CliFamily(
            name="lifecycle",
            summary="Core init/doctor/sublane adoption + diagnostics commands.",
            core=True,
        ),
        cli_core.register_lifecycle,
    ),
    (
        CliFamily(
            name="runtime-config",
            summary="runtime-config + instruction-alias runbook family.",
        ),
        cli_runtime_config.register,
    ),
    (
        CliFamily(
            name="docs-scaffold",
            summary="rules/scaffold/docs governance family.",
        ),
        cli_docs_scaffold.register,
    ),
    (
        CliFamily(
            name="onboarding",
            summary="deterministic project onboarding inspect/plan/apply/resume family.",
        ),
        cli_onboarding.register,
    ),
    (
        CliFamily(
            name="observability",
            summary="events/otel observability family.",
        ),
        cli_observability.register,
    ),
    (
        CliFamily(
            name="session",
            summary="session naming/boundary/vscode helpers family.",
        ),
        cli_session.register,
    ),
    (
        CliFamily(
            name="workspace",
            summary="workspace register/inspect/defaults family.",
        ),
        cli_workspace.register,
    ),
    (
        CliFamily(
            name="state",
            summary=(
                "home-scoped state store inspect/migrate/cleanup family "
                "(legacy SQLite consolidation; migration is backup-first / "
                "non-destructive, cleanup is separately gated)."
            ),
        ),
        cli_state.register,
    ),
    (
        CliFamily(
            name="release",
            summary="release check/bump/publish/workflow family (durable release gates).",
            authorities=frozenset({"close_approval", "workflow_authority"}),
        ),
        cli_release.register,
    ),
    (
        CliFamily(
            name="health",
            summary=(
                "module-health report + oversized-module gate family "
                "(read-only LOC/complexity measurement; no routing/approval "
                "authority)."
            ),
        ),
        cli_module_health.register,
    ),
    (
        CliFamily(
            name="tests",
            summary=(
                "test verification helpers family (Redmine #12752 / #12754 / "
                "#13733): module-to-test impact resolver (`tests resolve`), test "
                "runtime profiling against the slow-test budget (`tests profile`), "
                "and the isolated-shard parallel runner (`tests parallel`) for "
                "local/CI reuse. Read-only; no routing, approval, or close "
                "authority."
            ),
        ),
        cli_test_impact.register,
    ),
    (
        CliFamily(
            name="redmine-version",
            summary=(
                "Redmine Version metadata operations family (Redmine #12651): "
                "open-leaf enumeration + fail-closed rename/close/lock/delete "
                "preflight over operator-exported snapshots. Advisory / read-only; "
                "no Redmine write, no routing/approval/close authority."
            ),
        ),
        cli_redmine_version.register,
    ),
)


def _seed_registry() -> BuiltinCliModuleRegistry:
    """Build the ordered registry of CLI families this codebase ships today."""
    registry = BuiltinCliModuleRegistry()
    for family, _registrar in _FAMILY_BINDINGS:
        registry.register(family)
    return registry


# Module-level singletons: the classification of today's built-in CLI families
# and the name -> registrar binding. Seeded once at import from static
# references; nothing here loads or executes a family by path.
BUILTIN_CLI_MODULE_REGISTRY = _seed_registry()
_REGISTRARS: dict[str, Callable[[object], None]] = {
    family.name: registrar for family, registrar in _FAMILY_BINDINGS
}


def load_composition_config(
    record: "Optional[Mapping[str, object]]" = None,
) -> CliCompositionConfig:
    """Resolve a repo-local config record into a validated CliCompositionConfig.

    Two-stage fail-closed read layer: ``CliCompositionConfig.from_record``
    validates the record *shape* (closed schema, typed values, no
    module/callable/authority/secret fields), then the built-in registry
    validates its *meaning* — an unknown or mandatory family fails closed here,
    not only later at compose time. ``None``/empty resolves to the
    behavior-preserving full composition, so a missing config never changes the
    default ``mozyo-bridge`` CLI.

    This is a read/normalize layer only: it never loads, imports, or executes a
    family, and the config it returns can only select built-in families — it
    cannot reorder, add, supply a registrar, or grant authority.
    """
    config = CliCompositionConfig.from_record(record)
    # Validate against the families this build actually ships now (fail-closed
    # early on unknown / mandatory family names), then discard the result — we
    # only want the validation side effect; compose_parser re-resolves at use.
    BUILTIN_CLI_MODULE_REGISTRY.resolve_enabled(config)
    return config


def compose_parser(sub, config: Optional[CliCompositionConfig] = None, *, snapshot=None) -> None:
    """Compose the top-level subparsers from the registry, in order.

    Walks :data:`BUILTIN_CLI_MODULE_REGISTRY` in registration order, resolving
    the enabled families for ``config`` (default: all enabled), and invokes each
    family's built-in registrar against ``sub``. With the default config this
    reproduces the pre-registry ``build_parser()`` subcommand sequence exactly.

    ``config`` may only select/deselect non-mandatory families; the registry
    rejects a config that tries to disable a mandatory (core / authority-bearing)
    family, so owner approval / review / close / send safety stay non-configurable.

    ``snapshot`` (Redmine #13569 R1-F1) is the single agent-provider runtime snapshot the
    composition root built. It is threaded to every registrar whose signature declares a
    ``snapshot`` keyword — the provider-vocabulary registrars (``agents`` / ``handoff`` /
    lifecycle ``init`` + ``herdr`` choices) — so their ``--agent`` / ``--to`` choices come
    from the ONE injected snapshot rather than each reading an import-time global. A
    registrar that does not take ``snapshot`` is called unchanged, so this is byte-identical
    for every non-provider family.
    """
    import inspect

    for name in BUILTIN_CLI_MODULE_REGISTRY.resolve_enabled(config):
        registrar = _REGISTRARS[name]
        if snapshot is not None and "snapshot" in inspect.signature(registrar).parameters:
            registrar(sub, snapshot=snapshot)
        else:
            registrar(sub)


__all__ = (
    "BUILTIN_CLI_MODULE_REGISTRY",
    "compose_parser",
    "load_composition_config",
)
