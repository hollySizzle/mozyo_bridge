"""``auto_integration`` config block schema tests (Redmine #13686).

Pins the typed field contract of the gated auto-integration knob:

- the behavior-preserving default (``mode: disabled``), so a repo that declares nothing
  keeps the fully manual coordinator integration it had before the actuator existed;
- the owner's j#96335 default: ff-only on;
- that no CI knob exists (R2 review j#96350 finding 1 — both gates are unconditional in the
  state machine, so a key that could turn one off would be a key the runtime ignores);
- that no ``delete_remote_branch`` key exists at all (R1 review j#96344 finding 1 — the
  operation had no compare-and-swap and is removed rather than defaulted off);
- fail-closed parsing — a non-mapping record, an unknown key, an unsupported version, a mode
  outside the closed vocabulary, a non-boolean flag, an empty ``integration_branch``;
- that the closed key set cannot express an authority (a boundary-shaped key is refused with
  the boundary message), and in particular that no key can request a force push, a rebase, or
  an approval / review / close override;
- that the block reaches the composed :class:`RepoLocalConfig` and its declaration-status
  projection.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (
    AUTO_INTEGRATION_KEYS,
    AUTO_INTEGRATION_MODES,
    AUTO_INTEGRATION_MODE_AUTO,
    AUTO_INTEGRATION_MODE_DISABLED,
    AutoIntegrationConfig,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (
    CONFIG_BLOCK_KEYS,
    CONFIG_LEAF_KEYS,
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    classify_config_sources,
)


class DefaultsTest(unittest.TestCase):
    def test_absent_block_is_behavior_preserving(self) -> None:
        for config in (
            AutoIntegrationConfig.default(),
            AutoIntegrationConfig.from_record(None),
            AutoIntegrationConfig.from_record({}),
        ):
            self.assertEqual(config.mode, AUTO_INTEGRATION_MODE_DISABLED)

    def test_owner_decision_defaults(self) -> None:
        config = AutoIntegrationConfig.default()
        # j#96335: fast-forward-only is the default.
        self.assertTrue(config.ff_only)
        self.assertTrue(config.remove_worktree)
        self.assertTrue(config.delete_local_branch)
        # An unset target defers to runtime resolution rather than guessing a branch.
        self.assertIsNone(config.integration_branch)


class ModeTest(unittest.TestCase):
    def test_every_mode_in_the_closed_vocabulary_parses(self) -> None:
        for mode in (
            AUTO_INTEGRATION_MODE_AUTO,
                    AUTO_INTEGRATION_MODE_DISABLED,
        ):
            self.assertEqual(
                AutoIntegrationConfig.from_record({"mode": mode}).mode, mode
            )

    def test_the_vocabulary_is_exactly_three_modes(self) -> None:
        self.assertEqual(
            AUTO_INTEGRATION_MODES,
            {
                AUTO_INTEGRATION_MODE_AUTO,
                            AUTO_INTEGRATION_MODE_DISABLED,
            },
        )

    def test_an_unknown_mode_fails_closed(self) -> None:
        for value in ("Auto", "AUTO", "enabled", "", None, True, 1):
            with self.assertRaises(RepoLocalConfigError, msg=repr(value)):
                AutoIntegrationConfig.from_record({"mode": value})


class FailClosedParsingTest(unittest.TestCase):
    def test_non_mapping_record_fails_closed(self) -> None:
        for value in ([], "auto", 3):
            with self.assertRaises(RepoLocalConfigError, msg=repr(value)):
                AutoIntegrationConfig.from_record(value)  # type: ignore[arg-type]

    def test_unknown_key_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError) as caught:
            AutoIntegrationConfig.from_record({"ff_onyl": True})
        self.assertIn("unknown key", str(caught.exception))

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            AutoIntegrationConfig.from_record({"version": 99})

    def test_non_boolean_flags_fail_closed(self) -> None:
        # `0` / `1` must not silently read as a policy change; this is the same strict-bool
        # boundary the sibling blocks enforce.
        for key in (
            "ff_only",
            "remove_worktree",
            "delete_local_branch",
        ):
            for value in (0, 1, "true", "yes", None):
                with self.assertRaises(RepoLocalConfigError, msg=f"{key}={value!r}"):
                    AutoIntegrationConfig.from_record({key: value})

    def test_integration_branch_must_be_a_non_empty_string_or_absent(self) -> None:
        for value in ("", "   ", 3, []):
            with self.assertRaises(RepoLocalConfigError, msg=repr(value)):
                AutoIntegrationConfig.from_record({"integration_branch": value})
        self.assertEqual(
            AutoIntegrationConfig.from_record({"integration_branch": "main"}).integration_branch,
            "main",
        )


class BoundaryTest(unittest.TestCase):
    def test_the_key_set_is_exactly_the_operational_fields(self) -> None:
        self.assertEqual(
            AUTO_INTEGRATION_KEYS,
            {
                "version",
                "mode",
                "integration_branch",
                "ff_only",
                "remove_worktree",
                "delete_local_branch",
            },
        )

    def test_there_is_no_remote_branch_delete_key(self) -> None:
        # R1 review j#96344 finding 1: the step had no CAS against the remote tip and a real
        # one needs the prohibited `--force-with-lease`, so the operation is gone. The key is
        # removed rather than left as a way to ask for it — declaring it is now an error.
        self.assertNotIn("delete_remote_branch", AUTO_INTEGRATION_KEYS)
        self.assertFalse(hasattr(AutoIntegrationConfig.default(), "delete_remote_branch"))
        with self.assertRaises(RepoLocalConfigError):
            AutoIntegrationConfig.from_record({"delete_remote_branch": False})

    def test_an_authority_shaped_key_is_refused_with_the_boundary_message(self) -> None:
        for key in (
            "skip_owner_approval",
            "review_exempt",
            "close_on_integrate",
            "send_target",
            "credential",
        ):
            with self.assertRaises(RepoLocalConfigError, msg=key) as caught:
                AutoIntegrationConfig.from_record({key: True})
            self.assertIn("boundary token", str(caught.exception), key)

    def test_no_key_can_request_a_force_push_or_a_rebase(self) -> None:
        # Not expressible by construction: the closed key set has no such field, so the
        # rejection is the ordinary unknown-key one rather than a special case.
        for key in (
            "force_push",
            "force",
            "auto_rebase",
            "rebase_on_conflict",
            "delete_remote_branch",
            "require_source_ci",
            "require_integration_ci",
        ):
            with self.assertRaises(RepoLocalConfigError, msg=key):
                AutoIntegrationConfig.from_record({key: True})


class ComposedConfigTest(unittest.TestCase):
    def test_the_block_reaches_the_composed_record(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "auto_integration": {
                    "mode": AUTO_INTEGRATION_MODE_AUTO,
                    "integration_branch": "main",
                    "ff_only": False,
                }
            }
        )
        self.assertEqual(
            config.auto_integration.mode, AUTO_INTEGRATION_MODE_AUTO
        )
        self.assertEqual(config.auto_integration.integration_branch, "main")
        self.assertFalse(config.auto_integration.ff_only)

    def test_an_absent_block_composes_to_the_disabled_default(self) -> None:
        config = RepoLocalConfig.from_record({})
        self.assertEqual(
            config.auto_integration.mode, AUTO_INTEGRATION_MODE_DISABLED
        )

    def test_an_invalid_block_fails_the_whole_load_closed(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"auto_integration": {"mode": "enabled"}})


class DeclarationStatusTest(unittest.TestCase):
    def test_the_block_and_its_leaves_are_classified(self) -> None:
        self.assertIn("auto_integration", CONFIG_BLOCK_KEYS)
        leaves = {dotted for dotted, _ in CONFIG_LEAF_KEYS}
        for field_name in (
            "mode",
            "integration_branch",
            "ff_only",
            "remove_worktree",
            "delete_local_branch",
        ):
            self.assertIn(f"auto_integration.{field_name}", leaves, field_name)
        for gone in (
            "auto_integration.delete_remote_branch",
            "auto_integration.require_source_ci",
            "auto_integration.require_integration_ci",
        ):
            self.assertNotIn(gone, leaves, gone)

    def test_undeclared_leaves_report_the_default_they_resolve_to(self) -> None:
        # The actuator's effective settings must be readable rather than inferred from an
        # absent block: every one of them decides whether a real side effect is attempted.
        statuses = {
            status.key: status
            for status in classify_config_sources(
                raw_record=None,
                config=RepoLocalConfig.default(),
                schema_version=2,
                legacy_migratable=False,
            )
        }
        self.assertEqual(statuses["auto_integration.mode"].source, SOURCE_DEFAULT)
        self.assertEqual(
            statuses["auto_integration.mode"].effective_value,
            AUTO_INTEGRATION_MODE_DISABLED,
        )
        self.assertIs(statuses["auto_integration.ff_only"].effective_value, True)

    def test_a_declared_leaf_reports_declared(self) -> None:
        record = {"auto_integration": {"mode": AUTO_INTEGRATION_MODE_AUTO}}
        statuses = {
            status.key: status
            for status in classify_config_sources(
                raw_record=record,
                config=RepoLocalConfig.from_record(record),
                schema_version=2,
                legacy_migratable=False,
            )
        }
        self.assertEqual(statuses["auto_integration.mode"].source, SOURCE_DECLARED)
        self.assertEqual(
            statuses["auto_integration.mode"].effective_value,
            AUTO_INTEGRATION_MODE_AUTO,
        )
        # A partially declared block never buries an undeclared leaf under a block-level
        # `declared`.
        self.assertEqual(
            statuses["auto_integration.ff_only"].source, SOURCE_DEFAULT
        )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
