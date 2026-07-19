"""Specs for the v3 provider ghost-composer signal schema (Redmine #14065 Phase 2)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_ghost_composer_signal import (  # noqa: E501
    ADMITTED_GHOST_COMPOSER_SIGNALS,
    normalize_ghost_composer_signals,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
    require_profile,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
    AgentProviderProfile,
    AgentProviderProfileError,
)


def _profile(record, *, schema_version="3"):
    base = {"protocol": "interactive_cli_tui", "executable": {"command": "x", "env_override": "MOZYO_X"}}
    base.update(record)
    return AgentProviderProfile.from_record("x", base, schema_version=schema_version)


class AdmittedSetTest(unittest.TestCase):
    def test_admitted_set_is_dim_only(self) -> None:
        self.assertEqual(frozenset({"dim"}), ADMITTED_GHOST_COMPOSER_SIGNALS)


class NormalizeTest(unittest.TestCase):
    def test_dim_accepted(self) -> None:
        self.assertEqual(("dim",), normalize_ghost_composer_signals(["dim"], provider_id="x"))

    def test_empty_is_valid(self) -> None:
        self.assertEqual((), normalize_ghost_composer_signals([], provider_id="x"))

    def test_non_admitted_rejected(self) -> None:
        for bad in ("normal", "mixed", "unknown", "bright"):
            with self.assertRaises(AgentProviderProfileError):
                normalize_ghost_composer_signals([bad], provider_id="x")

    def test_non_list_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            normalize_ghost_composer_signals("dim", provider_id="x")

    def test_duplicate_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            normalize_ghost_composer_signals(["dim", "dim"], provider_id="x")


class ProfileFieldTest(unittest.TestCase):
    def test_v3_profile_admits_declared_dim(self) -> None:
        p = _profile({"ghost_composer_signals": ["dim"]})
        self.assertEqual(("dim",), p.ghost_composer_signals)
        self.assertTrue(p.admits_ghost_signal("dim"))
        self.assertFalse(p.admits_ghost_signal("normal"))
        self.assertFalse(p.admits_ghost_signal(None))

    def test_absent_field_admits_nothing(self) -> None:
        p = _profile({})
        self.assertEqual((), p.ghost_composer_signals)
        self.assertFalse(p.admits_ghost_signal("dim"))

    def test_field_on_v2_fails_closed(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            _profile({"ghost_composer_signals": ["dim"]}, schema_version="2")

    def test_non_admitted_value_fails_closed(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            _profile({"ghost_composer_signals": ["normal"]})

    def test_directly_built_profile_cannot_hold_bad_signal(self) -> None:
        # The full invariant is on the record, not just the loader.
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
            InteractionProtocol,
            TrustedExecutable,
        )

        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfile(
                provider_id="x",
                protocol=InteractionProtocol.INTERACTIVE_CLI_TUI,
                executable=TrustedExecutable(command="x", env_override="MOZYO_X"),
                ghost_composer_signals=("normal",),
            )


class PackagedProfilesTest(unittest.TestCase):
    def test_both_builtin_providers_admit_dim(self) -> None:
        for provider in ("claude", "codex"):
            self.assertEqual(("dim",), require_profile(provider).ghost_composer_signals)
            self.assertTrue(require_profile(provider).admits_ghost_signal("dim"))


if __name__ == "__main__":
    unittest.main()
