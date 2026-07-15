"""R3 correction acceptance (Redmine #13569 j#77298 — R3-F1, R3-F2).

R3 found the R2 fixes were still entrance-only:

- R3-F1: the accepted R2-F2b preflight seam was never reached in the REAL parser path —
  the ``sublane create`` subparser did not ``set_defaults(snapshot=)``, so
  ``cmd_sublane_start`` always saw ``args.snapshot is None`` and the preflight fell back to
  the built-in snapshot. These tests drive the real ``build_parser -> parsed handler ->
  preflight`` seam and pin both the positive (injected snapshot recognizes a rebound
  provider) and negative (built-in snapshot rejects it) outcomes.
- R3-F2: the e_110 discovery / pane-resolution domain still imported the e_140 registry
  singleton — the R2 leaf just moved the import from module level to five function-local
  imports, which is still a domain -> registry dependency and still five separate caches.
  These tests pin that NO e_140 registry import exists anywhere in the e_110 f_120 subtree
  (module OR function-local, via full-AST walk), and that the fallback is one core-owned
  immutable snapshot supplied by the composition.

Providers are fake, explicit placeholders (strengthened-scanner rule).
"""

from __future__ import annotations

import argparse
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E402
    build_runtime_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E402
    AgentProviderProfileConfig,
)

F120_SUBTREE = (
    ROOT
    / "src"
    / "mozyo_bridge"
    / "e_110_execution_platform"
    / "f_120_agent_discovery_pane_resolution"
)


def _snapshot(profiles: dict):
    return build_runtime_snapshot(
        AgentProviderProfileConfig.from_record(
            {"version": "1", "source": "test-fixture", "profiles": profiles}
        ).to_registry()
    )


# A synthetic launchable provider that is present ONLY in the injected snapshot — the
# built-in snapshot does not know it, so it is the discriminator between "keyed on the
# injection" and "fell back to the built-in set".
SYNTH = _snapshot(
    {
        "mistral-cli": {
            "protocol": "interactive_cli_tui",
            "executable": {"command": "mistral", "env_override": "MOZYO_AGENT_MISTRAL_BINARY"},
            "discovery_aliases": ["mistral-cli"],
            "process_names": ["mistral"],
            "capabilities": ["interactive_tui"],
        }
    }
)


class R3F1RealCompositionThreadsSnapshotToSublanePreflight(unittest.TestCase):
    def _parse_create(self, snapshot):
        import mozyo_bridge.application.cli as cli

        parser = cli.build_parser(snapshot=snapshot)
        return parser.parse_args(
            ["sublane", "create", "--issue", "1", "--lane-label", "L", "--execute",
             "--journal", "9"]
        )

    def test_parser_threads_the_same_snapshot_onto_the_sublane_handler(self) -> None:
        # The real parser wires the injected snapshot onto the parsed namespace (the R3-F1
        # gap: previously `set_defaults` set only `func`, so `args.snapshot` was absent).
        ns = self._parse_create(SYNTH)
        self.assertEqual(ns.func.__name__, "cmd_sublane_start")
        self.assertIs(ns.snapshot, SYNTH)

    def test_real_handler_delivers_the_injected_snapshot_to_the_preflight(self) -> None:
        # Drive the REAL parser -> parsed handler -> preflight seam and capture what the
        # preflight received. Blocking (return True) makes cmd_sublane_start return before
        # any side effect, so the test stays hermetic.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        captured = {}
        orig = act._sublane_start_provider_preflight_blocked

        def _spy(repo_root, *, snapshot=None):
            captured["snapshot"] = snapshot
            return True

        act._sublane_start_provider_preflight_blocked = _spy
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                ns = self._parse_create(SYNTH)
                ns.repo = tmp  # a clean, config-less repo -> work-unit resolves to default
                rc = ns.func(ns)
        finally:
            act._sublane_start_provider_preflight_blocked = orig
        self.assertEqual(rc, 1)  # blocked -> zero side effect
        self.assertIs(captured["snapshot"], SYNTH)  # the injection reached the preflight

    def test_preflight_keys_on_the_injected_snapshot_positive_and_negative(self) -> None:
        # With the bound gateway/worker provider present-and-launchable ONLY in the injected
        # snapshot, the preflight PASSES under the injection and BLOCKS under the built-in
        # snapshot — the exact rebound-provider outcome R2-F2b/R3-F1 protect.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution as wpr  # noqa: E501

        og, ow = wpr.resolve_gateway_provider, wpr.resolve_worker_provider
        wpr.resolve_gateway_provider = lambda root=None: "mistral-cli"
        wpr.resolve_worker_provider = lambda root=None: "mistral-cli"
        try:
            # injected snapshot knows mistral-cli as launchable -> NOT blocked
            self.assertFalse(
                act._sublane_start_provider_preflight_blocked("/repo", snapshot=SYNTH)
            )
            # built-in snapshot does not -> blocked (zero-start)
            self.assertTrue(
                act._sublane_start_provider_preflight_blocked("/repo", snapshot=None)
            )
        finally:
            wpr.resolve_gateway_provider, wpr.resolve_worker_provider = og, ow


class R3F2NoE140RegistryDependencyInF120Subtree(unittest.TestCase):
    def test_no_e140_import_anywhere_in_the_f120_subtree(self) -> None:
        # Full-AST walk over EVERY module in the subtree, catching module-level AND
        # function-local imports (the R2 assert only checked two module bodies, which is why
        # the leaf's five function-local e_140 imports survived).
        offenders = []
        for path in sorted(F120_SUBTREE.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "e_140" in node.module:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} from {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "e_140" in alias.name:
                            offenders.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} import {alias.name}"
                            )
        self.assertEqual(offenders, [], "e_110 f_120 subtree must not import the e_140 registry")

    def test_fallback_is_one_immutable_snapshot_all_accessors_project_off(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import (  # noqa: E501
            agent_provider_vocab as vocab,
        )

        prior = vocab._default_snapshot
        vocab.set_default_snapshot(SYNTH)
        try:
            # Every accessor is a projection of the ONE default snapshot (not 5 caches).
            self.assertIs(vocab.default_snapshot(), SYNTH)
            self.assertEqual(vocab.builtin_provider_ids(), SYNTH.provider_ids)
            self.assertEqual(vocab.builtin_discovery_aliases(), SYNTH.discovery_aliases())
            self.assertEqual(vocab.builtin_process_owners(), SYNTH.process_owners())
            self.assertEqual(vocab.builtin_agent_commands(), SYNTH.commands())
            self.assertEqual(
                vocab.builtin_agent_processes(),
                set(SYNTH.agent_process_names()) | {"node"},
            )
        finally:
            vocab.set_default_snapshot(prior)

    def test_composition_bootstrap_supplies_the_builtin_default(self) -> None:
        # Importing the package runs the composition bootstrap, which registers the built-in
        # snapshot as the fallback — no e_140 import in the domain required.
        import mozyo_bridge  # noqa: F401
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import (  # noqa: E501
            agent_provider_vocab as vocab,
        )

        self.assertEqual(set(vocab.default_snapshot().provider_ids), {"claude", "codex"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
