"""Built-in CLI module registry binding and parser composition (Redmine #12155).

This is the application-layer half of the internal CLI module registry. The
pure classification — :class:`~mozyo_bridge.domain.module_registry.CliFamily`,
:class:`~mozyo_bridge.domain.module_registry.CliCompositionConfig`, and the
ordered :class:`~mozyo_bridge.domain.module_registry.BuiltinCliModuleRegistry`
— lives in :mod:`mozyo_bridge.domain.module_registry` and imports no
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
    cli_agents,
    cli_cockpit,
    cli_core,
    cli_docs_scaffold,
    cli_handoff,
    cli_observability,
    cli_release,
    cli_runtime_config,
    cli_session,
    cli_workspace,
)
from mozyo_bridge.domain.module_registry import (
    BuiltinCliModuleRegistry,
    CliCompositionConfig,
    CliFamily,
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
            name="release",
            summary="release check/bump/publish/workflow family (durable release gates).",
            authorities=frozenset({"close_approval", "workflow_authority"}),
        ),
        cli_release.register,
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


def compose_parser(sub, config: Optional[CliCompositionConfig] = None) -> None:
    """Compose the top-level subparsers from the registry, in order.

    Walks :data:`BUILTIN_CLI_MODULE_REGISTRY` in registration order, resolving
    the enabled families for ``config`` (default: all enabled), and invokes each
    family's built-in registrar against ``sub``. With the default config this
    reproduces the pre-registry ``build_parser()`` subcommand sequence exactly.

    ``config`` may only select/deselect non-mandatory families; the registry
    rejects a config that tries to disable a mandatory (core / authority-bearing)
    family, so owner approval / review / close / send safety stay non-configurable.
    """
    for name in BUILTIN_CLI_MODULE_REGISTRY.resolve_enabled(config):
        _REGISTRARS[name](sub)


__all__ = (
    "BUILTIN_CLI_MODULE_REGISTRY",
    "compose_parser",
    "load_composition_config",
)
