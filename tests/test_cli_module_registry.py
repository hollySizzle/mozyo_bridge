"""Internal built-in CLI module registry / configuration-aware baseline tests
(Redmine #12155).

Pins the registry-driven parser composition added in #12155:

- the pure domain classification (`CliFamily`, `CliCompositionConfig`,
  `BuiltinCliModuleRegistry`) and its safety invariants — a family may only
  declare core-owned authorities, and config may never disable a mandatory
  (core / authority-bearing) family, so owner approval / review / close / send
  safety stay non-configurable;
- that `build_parser()` now composes through `cli_modules.compose_parser`, that
  the seeded registry order matches the observed top-level subcommand order, and
  that default composition is behavior-preserving;
- the explicit non-goal: composition binds to statically-imported built-in
  registrars only — there is no dynamic loading / external plugin entry point.

No tmux, network, or command handler is exercised here.
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import cli_modules
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.domain.module_registry import (
    COMPOSITION_RECORD_VERSION,
    CORE_OWNED_AUTHORITIES,
    BuiltinCliModuleRegistry,
    CliCompositionConfig,
    CliFamily,
    ModuleRegistryError,
)


def _top_level_subcommands(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.keys())
    raise AssertionError("no subparsers action on top-level parser")


class CliFamilyDescriptionTest(unittest.TestCase):
    def test_name_required(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliFamily(name="", summary="x")

    def test_only_core_owned_authorities_are_expressible(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliFamily(name="x", summary="x", authorities=frozenset({"made_up"}))

    def test_bare_string_authorities_rejected(self) -> None:
        # A bare string would be iterated char-by-char and bypass the subset
        # check; it must be rejected, not normalized.
        with self.assertRaises(ModuleRegistryError):
            CliFamily(name="x", summary="x", authorities="send_safety")

    def test_mandatory_is_core_or_authority_bearing(self) -> None:
        self.assertTrue(CliFamily(name="a", summary="s", core=True).mandatory)
        self.assertTrue(
            CliFamily(name="b", summary="s", authorities=frozenset({"send_safety"})).mandatory
        )
        self.assertFalse(CliFamily(name="c", summary="s").mandatory)


class RegistryOrderingAndSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = BuiltinCliModuleRegistry()
        self.reg.register(CliFamily(name="core", summary="s", core=True))
        self.reg.register(CliFamily(name="feat", summary="s"))
        self.reg.register(
            CliFamily(name="send", summary="s", authorities=frozenset({"send_safety"}))
        )

    def test_registration_order_is_composition_order(self) -> None:
        # Unlike the provider registry (sorted by id), CLI order must follow
        # registration order because it is observable in --help.
        self.assertEqual(self.reg.names(), ("core", "feat", "send"))

    def test_duplicate_name_rejected(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            self.reg.register(CliFamily(name="feat", summary="dup"))

    def test_register_rejects_non_description(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            self.reg.register(object())  # type: ignore[arg-type]

    def test_default_config_enables_everything_in_order(self) -> None:
        self.assertEqual(self.reg.resolve_enabled(), ("core", "feat", "send"))

    def test_config_may_disable_a_non_mandatory_family(self) -> None:
        cfg = CliCompositionConfig(disabled=frozenset({"feat"}))
        self.assertEqual(self.reg.resolve_enabled(cfg), ("core", "send"))

    def test_config_may_not_disable_a_core_family(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            self.reg.resolve_enabled(CliCompositionConfig(disabled=frozenset({"core"})))

    def test_config_may_not_disable_an_authority_bearing_family(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            self.reg.resolve_enabled(CliCompositionConfig(disabled=frozenset({"send"})))

    def test_config_disabling_unknown_family_fails_closed(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            self.reg.resolve_enabled(CliCompositionConfig(disabled=frozenset({"nope"})))


class BuiltinRegistrySeedTest(unittest.TestCase):
    """The shipped registry classification and its binding to registrars."""

    def setUp(self) -> None:
        self.reg = cli_modules.BUILTIN_CLI_MODULE_REGISTRY

    def test_every_family_has_a_bound_registrar(self) -> None:
        for name in self.reg.names():
            self.assertIn(name, cli_modules._REGISTRARS)
            self.assertTrue(callable(cli_modules._REGISTRARS[name]))

    def test_safety_critical_families_are_mandatory(self) -> None:
        # handoff / message / keys / pane-io carry send / routing / review /
        # workflow authority; release carries close approval. None may be
        # configured away.
        mandatory = set(self.reg.mandatory_names())
        for name in ["core-base", "pane-io", "message", "keys", "handoff", "lifecycle", "release"]:
            self.assertIn(name, mandatory, f"{name} must be mandatory")

    def test_feature_families_are_configurable(self) -> None:
        configurable = set(self.reg.names()) - set(self.reg.mandatory_names())
        for name in ["cockpit", "agents", "tmux-ui", "runtime-config", "docs-scaffold",
                     "observability", "session", "workspace"]:
            self.assertIn(name, configurable, f"{name} should be configurable")

    def test_declared_authorities_are_core_owned(self) -> None:
        for fam in self.reg:
            self.assertTrue(fam.authorities <= CORE_OWNED_AUTHORITIES)


class ComposeParserBehaviorTest(unittest.TestCase):
    def test_build_parser_composes_default_full_cli(self) -> None:
        # The composed default order must equal the registry order.
        self.assertEqual(
            cli_modules.BUILTIN_CLI_MODULE_REGISTRY.resolve_enabled(),
            cli_modules.BUILTIN_CLI_MODULE_REGISTRY.names(),
        )
        # And build_parser() must produce a usable subparser surface.
        subs = _top_level_subcommands(build_parser())
        self.assertIn("handoff", subs)
        self.assertIn("status", subs)
        self.assertIn("release", subs)

    def test_compose_parser_honors_a_config_that_drops_a_feature_family(self) -> None:
        # Composing with a feature family disabled removes exactly its
        # subcommands and nothing else — proving the baseline is genuinely
        # configuration-aware while staying safe.
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cli_modules.compose_parser(sub, CliCompositionConfig(disabled=frozenset({"agents"})))
        names = _top_level_subcommands(parser)
        self.assertNotIn("agents", names)
        self.assertIn("handoff", names)  # mandatory family still present
        self.assertIn("status", names)

    def test_compose_parser_refuses_to_drop_a_mandatory_family(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        with self.assertRaises(ModuleRegistryError):
            cli_modules.compose_parser(
                sub, CliCompositionConfig(disabled=frozenset({"handoff"}))
            )


class CompositionConfigRecordTest(unittest.TestCase):
    """The typed repo-local config record loader/normalizer (Redmine #12183).

    `CliCompositionConfig.from_record` turns an already-parsed YAML/TOML-shaped
    mapping into a `CliCompositionConfig`, validating record *shape*: a closed
    schema, typed values, and rejection of any field that would name a module /
    callable / authority / credential. Family *existence* and the mandatory rule
    stay with `resolve_enabled` (exercised via `load_composition_config`).
    """

    # --- default preserves full composition --------------------------------

    def test_none_record_is_the_behavior_preserving_default(self) -> None:
        self.assertEqual(CliCompositionConfig.from_record(None), CliCompositionConfig.default())
        self.assertEqual(CliCompositionConfig.from_record(None).disabled, frozenset())

    def test_empty_record_is_the_default(self) -> None:
        self.assertEqual(CliCompositionConfig.from_record({}), CliCompositionConfig.default())

    def test_explicit_supported_version_with_no_disable_is_default(self) -> None:
        cfg = CliCompositionConfig.from_record({"version": COMPOSITION_RECORD_VERSION})
        self.assertEqual(cfg.disabled, frozenset())

    def test_default_record_preserves_full_builtin_composition(self) -> None:
        # The end-to-end read layer: a missing config resolves to every shipped
        # family enabled, in registration order — the unchanged default CLI.
        cfg = cli_modules.load_composition_config(None)
        self.assertEqual(
            cli_modules.BUILTIN_CLI_MODULE_REGISTRY.resolve_enabled(cfg),
            cli_modules.BUILTIN_CLI_MODULE_REGISTRY.names(),
        )

    # --- valid optional disable --------------------------------------------

    def test_valid_optional_disable_resolves_to_config(self) -> None:
        cfg = CliCompositionConfig.from_record({"disabled": ["agents", "session"]})
        self.assertEqual(cfg.disabled, frozenset({"agents", "session"}))

    def test_load_composition_config_validates_optional_disable_against_registry(self) -> None:
        cfg = cli_modules.load_composition_config({"disabled": ["agents"]})
        enabled = cli_modules.BUILTIN_CLI_MODULE_REGISTRY.resolve_enabled(cfg)
        self.assertNotIn("agents", enabled)
        self.assertIn("handoff", enabled)

    # --- unknown family / mandatory disable fail closed (via registry) ------

    def test_unknown_family_fails_closed(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            cli_modules.load_composition_config({"disabled": ["not-a-real-family"]})

    def test_mandatory_family_disable_fails_closed(self) -> None:
        # handoff carries send/routing/review/workflow authority — config may
        # never disable it, so the typed record cannot weaken authority.
        with self.assertRaises(ModuleRegistryError):
            cli_modules.load_composition_config({"disabled": ["handoff"]})

    # --- closed schema / typed values --------------------------------------

    def test_non_mapping_record_is_rejected(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record(["agents"])  # type: ignore[arg-type]

    def test_unknown_top_level_key_fails_closed(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"enabled": ["agents"]})

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"version": COMPOSITION_RECORD_VERSION + 1})

    def test_non_integer_version_is_rejected(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"version": "1"})
        # bool is an int subclass but must not read as version 1.
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"version": True})

    def test_disabled_must_be_a_list_not_a_bare_string(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"disabled": "agents"})

    def test_disabled_must_be_a_list_not_a_mapping(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"disabled": {"agents": True}})

    def test_disabled_entries_must_be_strings(self) -> None:
        with self.assertRaises(ModuleRegistryError):
            CliCompositionConfig.from_record({"disabled": [123]})

    # --- secret-shaped / authority-changing / code-loading fields rejected --

    def test_authority_changing_field_is_rejected(self) -> None:
        # A record that tries to grant authority, name a registrar, or supply a
        # module path is rejected with a boundary-specific message — these are
        # not "unknown future keys", they are boundaries this surface owns.
        for key in ("authorities", "registrar", "module_path", "approval", "routing"):
            with self.subTest(key=key):
                with self.assertRaises(ModuleRegistryError):
                    CliCompositionConfig.from_record({key: ["x"]})

    def test_code_loading_field_is_rejected(self) -> None:
        for key in ("import", "plugin_path", "entry_point", "callable", "exec"):
            with self.subTest(key=key):
                with self.assertRaises(ModuleRegistryError):
                    CliCompositionConfig.from_record({key: "anything"})

    def test_secret_shaped_field_is_rejected(self) -> None:
        # Field *names* only — no real secret values — but a record may never
        # carry a credential-shaped field.
        for key in ("api_token", "secret", "password", "credential"):
            with self.subTest(key=key):
                with self.assertRaises(ModuleRegistryError):
                    CliCompositionConfig.from_record({key: "x"})

    def test_secret_or_path_shaped_disable_value_is_rejected(self) -> None:
        # A value not shaped like a family id is rejected before resolution: an
        # uppercase secret-shaped sentinel, a filesystem path, and a dotted
        # module path all fail the family-identifier guard.
        for entry in ("DROP-SECRET-SENTINEL", "/workspace/project-alpha", "workspace.project_alpha"):
            with self.subTest(entry=entry):
                with self.assertRaises(ModuleRegistryError):
                    CliCompositionConfig.from_record({"disabled": [entry]})


if __name__ == "__main__":
    unittest.main()
