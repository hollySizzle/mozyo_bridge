"""AgentProviderRuntimeSnapshot value object + factory (Redmine #13569 Increment 2A).

Pins the mechanical projection the composition root injects into every consumer:
the snapshot is an immutable projection of ONE registry, the built-in snapshot equals
the registry's derived vocabulary, and a synthetic same-protocol provider is projected
without any source literal. Launchability is derived from the same predicate the launch
preflight uses (protocol + interactive-TUI capability), so the two can never disagree.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (
    AgentProviderRuntimeSnapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (
    BUILTIN_AGENT_PROVIDER_SNAPSHOT,
    build_runtime_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    agent_commands,
    agent_discovery_aliases,
    agent_process_owners,
    agent_provider_ids,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentProviderProfileConfig,
)


def _registry_from(profiles: dict) -> object:
    """A synthetic registry from a profiles mapping (fake, explicit placeholder data)."""
    return AgentProviderProfileConfig.from_record(
        {"version": "1", "source": "test-fixture", "profiles": profiles}
    ).to_registry()


# A synthetic same-protocol provider whose process basename differs from its id, so the
# tests also exercise the process-owner map (not just id==process built-ins). Explicit
# fake placeholder per the strengthened scanner guidance.
SYNTH_PROFILES = {
    "mistral-cli": {
        "protocol": "interactive_cli_tui",
        "executable": {
            "command": "mistral",
            "env_override": "MOZYO_AGENT_MISTRAL_BINARY",
        },
        "discovery_aliases": ["mistral-cli"],
        "process_names": ["mistral"],
        "capabilities": ["interactive_tui", "launch_argv_override"],
    }
}

# A different-protocol-shaped provider is impossible to declare (the enum is closed),
# so "unlaunchable" is modeled by a provider that declares NO interactive_tui capability.
NON_TUI_PROFILES = {
    "batchbot": {
        "protocol": "interactive_cli_tui",
        "executable": {"command": "batchbot", "env_override": "MOZYO_AGENT_BATCHBOT_BINARY"},
        "capabilities": ["launch_argv_override"],
    }
}


class BuiltinSnapshotMatchesRegistry(unittest.TestCase):
    def test_builtin_snapshot_projects_the_registry_vocabulary(self) -> None:
        snap = BUILTIN_AGENT_PROVIDER_SNAPSHOT
        self.assertEqual(snap.provider_ids, agent_provider_ids())
        self.assertEqual(snap.commands(), agent_commands())
        self.assertEqual(snap.process_owners(), agent_process_owners())
        self.assertEqual(
            {a: snap.provider_for_alias(a) for a in agent_discovery_aliases()},
            agent_discovery_aliases(),
        )
        self.assertEqual(snap.sorted_provider_ids(), ("claude", "codex"))

    def test_builtin_providers_are_launchable(self) -> None:
        snap = BUILTIN_AGENT_PROVIDER_SNAPSHOT
        self.assertTrue(snap.is_launchable("claude"))
        self.assertTrue(snap.is_launchable("codex"))

    def test_node_is_a_receiver_agnostic_agent_process_but_owns_no_provider(self) -> None:
        snap = BUILTIN_AGENT_PROVIDER_SNAPSHOT
        self.assertTrue(snap.is_agent_process("node"))
        self.assertIsNone(snap.provider_for_process("node"))
        self.assertIsNone(snap.provider_for_alias("node"))


class SnapshotImmutability(unittest.TestCase):
    def test_provider_ids_and_maps_are_read_only(self) -> None:
        snap = build_runtime_snapshot(_registry_from(SYNTH_PROFILES))
        with self.assertRaises(AttributeError):
            snap.provider_ids.add("x")  # frozenset has no add
        # The mapping copies handed out are independent; mutating them does not leak.
        commands = snap.commands()
        commands["mistral-cli"] = "hacked"
        self.assertEqual(snap.command_for("mistral-cli"), "mistral")


class SyntheticProviderProjection(unittest.TestCase):
    def test_synthetic_same_protocol_provider_is_fully_projected(self) -> None:
        snap = build_runtime_snapshot(_registry_from(SYNTH_PROFILES))
        self.assertEqual(snap.provider_ids, frozenset({"mistral-cli"}))
        self.assertEqual(snap.command_for("mistral-cli"), "mistral")
        self.assertEqual(snap.provider_for_alias("mistral-cli"), "mistral-cli")
        self.assertEqual(snap.provider_for_process("mistral"), "mistral-cli")
        self.assertTrue(snap.is_agent_process("mistral"))
        self.assertTrue(snap.is_launchable("mistral-cli"))

    def test_unknown_provider_answers_are_fail_closed_never_raise(self) -> None:
        snap = build_runtime_snapshot(_registry_from(SYNTH_PROFILES))
        self.assertFalse(snap.is_provider("nope"))
        self.assertIsNone(snap.command_for("nope"))
        self.assertIsNone(snap.provider_for_alias("nope"))
        self.assertIsNone(snap.provider_for_process("nope"))
        self.assertFalse(snap.is_launchable("nope"))
        self.assertFalse(snap.is_launchable(None))

    def test_launchability_follows_the_interactive_tui_capability(self) -> None:
        # A provider without the interactive_tui capability is expressible/recognizable
        # but NOT launchable — the same predicate `require_launchable` enforces.
        snap = build_runtime_snapshot(_registry_from(NON_TUI_PROFILES))
        self.assertTrue(snap.is_provider("batchbot"))
        self.assertFalse(snap.is_launchable("batchbot"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
