"""Tests for the externalized workflow-contract config (Redmine #12953).

#12953 moved the per-transition-role bundle composition (refs / obligations /
``contract_set_version``) out of builtin module constants and into the packaged
``workflow_contract_config.yaml``, which the runtime resolver reads at import
time as the single source of truth. These tests pin:

- the shipped bundles are DERIVED from the config file (source-of-truth proof);
- the loader fails closed on a malformed config (non-mapping top level, missing /
  non-int version, missing / blank roles, missing bundle field, duplicate
  contract id, unreadable file);
- ``validate_bundles_against_catalog`` fails closed on a missing catalog ref or a
  canonical_path that drifts from the catalog, and passes for the real catalog.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import (
    workflow_contract as wc,
)


class ConfigSourceOfTruthTest(unittest.TestCase):
    def _raw_config(self) -> dict:
        text = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "e_110_execution_platform"
            / "f_130_handoff_routing"
            / "domain"
            / wc.WORKFLOW_CONTRACT_CONFIG_FILENAME
        ).read_text(encoding="utf-8")
        return yaml.safe_load(text)

    def test_shipped_bundles_are_derived_from_the_config_file(self) -> None:
        raw = self._raw_config()
        self.assertEqual(wc.WORKFLOW_CONTRACT_SET_VERSION, raw["contract_set_version"])
        self.assertEqual(
            set(wc.WORKFLOW_CONTRACT_BUNDLES), set(raw["roles"])
        )
        for role, spec in raw["roles"].items():
            bundle = wc.WORKFLOW_CONTRACT_BUNDLES[role]
            self.assertEqual(bundle.current_role, role)
            self.assertEqual(bundle.read_obligation, spec["read_obligation"])
            self.assertEqual(bundle.callback_obligation, spec["callback_obligation"])
            self.assertEqual(
                [ref.canonical_path for ref in bundle.refs],
                [entry["canonical_path"] for entry in spec["refs"]],
            )
            self.assertEqual(
                [ref.contract_id for ref in bundle.refs],
                [entry["contract_id"] for entry in spec["refs"]],
            )

    def test_config_version_is_the_source_of_the_set_version(self) -> None:
        # Change only the config text and the loader reflects it — the version is
        # not a duplicated Python literal.
        raw = self._raw_config()
        raw["contract_set_version"] = 99
        with mock.patch.object(wc, "_config_text", return_value=yaml.safe_dump(raw)):
            version, bundles = wc._load_bundles_from_config()
        self.assertEqual(version, 99)
        for bundle in bundles.values():
            self.assertEqual(bundle.contract_set_version, 99)


class ConfigFailClosedTest(unittest.TestCase):
    def _load_with_text(self, text: str) -> None:
        with mock.patch.object(wc, "_config_text", return_value=text):
            wc._load_bundles_from_config()

    def test_non_mapping_top_level_fails_closed(self) -> None:
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text("- just\n- a\n- list\n")

    def test_missing_version_fails_closed(self) -> None:
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text("roles:\n  r:\n    read_obligation: a\n")

    def test_non_int_version_fails_closed(self) -> None:
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text("contract_set_version: '2'\nroles: {}\n")

    def test_bool_version_fails_closed(self) -> None:
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text("contract_set_version: true\nroles: {}\n")

    def test_missing_or_empty_roles_fails_closed(self) -> None:
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text("contract_set_version: 2\n")
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text("contract_set_version: 2\nroles: {}\n")

    def test_blank_role_token_fails_closed(self) -> None:
        text = (
            "contract_set_version: 2\n"
            "roles:\n"
            "  '  ':\n"
            "    read_obligation: read\n"
            "    callback_obligation: cb\n"
            "    refs:\n"
            "      - contract_id: logic-x\n"
            "        canonical_path: vibes/docs/logics/x.md\n"
        )
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text(text)

    def test_role_missing_bundle_field_fails_closed(self) -> None:
        text = (
            "contract_set_version: 2\n"
            "roles:\n"
            "  r:\n"
            "    read_obligation: read\n"
            # callback_obligation missing
            "    refs:\n"
            "      - contract_id: logic-x\n"
            "        canonical_path: vibes/docs/logics/x.md\n"
        )
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text(text)

    def test_ref_missing_field_fails_closed(self) -> None:
        text = (
            "contract_set_version: 2\n"
            "roles:\n"
            "  r:\n"
            "    read_obligation: read\n"
            "    callback_obligation: cb\n"
            "    refs:\n"
            "      - contract_id: logic-x\n"  # canonical_path missing
        )
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text(text)

    def test_duplicate_contract_id_in_config_fails_closed(self) -> None:
        text = (
            "contract_set_version: 2\n"
            "roles:\n"
            "  r:\n"
            "    read_obligation: read\n"
            "    callback_obligation: cb\n"
            "    refs:\n"
            "      - contract_id: logic-x\n"
            "        canonical_path: vibes/docs/logics/x.md\n"
            "      - contract_id: logic-x\n"
            "        canonical_path: vibes/docs/logics/y.md\n"
        )
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text(text)

    def test_non_sequence_refs_fails_closed(self) -> None:
        text = (
            "contract_set_version: 2\n"
            "roles:\n"
            "  r:\n"
            "    read_obligation: read\n"
            "    callback_obligation: cb\n"
            "    refs: not-a-list\n"
        )
        with self.assertRaises(wc.WorkflowContractError):
            self._load_with_text(text)

    def test_unreadable_config_fails_closed(self) -> None:
        with mock.patch.object(
            wc, "WORKFLOW_CONTRACT_CONFIG_PACKAGE", "mozyo_bridge.not_a_real_package"
        ):
            with self.assertRaises(wc.WorkflowContractError):
                wc._config_text()


class CatalogValidationTest(unittest.TestCase):
    def _catalog_map(self) -> dict[str, str]:
        raw = yaml.safe_load(
            (ROOT / ".mozyo-bridge" / "docs" / "catalog.yaml").read_text(
                encoding="utf-8"
            )
        )
        return {
            doc["id"]: doc["canonical_path"]
            for doc in raw["documents"]
            if "id" in doc and "canonical_path" in doc
        }

    def test_shipped_bundles_match_the_real_catalog(self) -> None:
        # Every ref's contract_id must resolve in the docs catalog with the same
        # canonical_path the bundle carries — no dangling / drifted pointer ships.
        wc.validate_bundles_against_catalog(self._catalog_map())

    def test_missing_catalog_ref_fails_closed(self) -> None:
        catalog = self._catalog_map()
        # Drop one id a bundle depends on.
        catalog.pop("logic-coordinator-sublane-development-flow", None)
        with self.assertRaises(wc.WorkflowContractError):
            wc.validate_bundles_against_catalog(catalog)

    def test_canonical_path_mismatch_fails_closed(self) -> None:
        catalog = self._catalog_map()
        catalog["logic-coordinator-sublane-development-flow"] = "vibes/docs/logics/WRONG.md"
        with self.assertRaises(wc.WorkflowContractError):
            wc.validate_bundles_against_catalog(catalog)

    def test_defaults_to_shipped_bundles(self) -> None:
        # A catalog missing everything fails closed against the default bundles.
        with self.assertRaises(wc.WorkflowContractError):
            wc.validate_bundles_against_catalog({})


if __name__ == "__main__":
    unittest.main()
