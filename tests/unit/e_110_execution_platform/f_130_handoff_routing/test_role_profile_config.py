"""Tests for the role profile template config schema + packaged artifact (#12952).

US #12388 pinned the four role profile template bodies as inline Python
constants; #12952 externalizes them to a wheel-packaged, schema-validated config
artifact (``role_profile_templates.yaml``) that the resolver loads at import.
These tests pin the fail-closed schema (unknown / missing role, empty template,
placeholder mismatch, unknown keys, bad version/source) and prove the shipped
artifact reproduces the resolver's registry byte-for-byte with the durable
version/source pointers preserved.
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import (
    role_profile as rp,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile_config import (
    KNOWN_ROLE_TOKENS,
    RoleProfileConfig,
    RoleProfileConfigError,
    extract_placeholders,
)


def _valid_record():
    """A minimal-but-complete valid config record (all four roles present)."""
    return {
        "version": "2026-06-21",
        "source": "vibes/docs/specs/delegated-coordinator-role-profile.md",
        "roles": {
            "coordinator": {
                "template": "# role profile: coordinator\n- <project> / <redmine_project>",
                "placeholders": ["project", "redmine_project"],
            },
            "delegated_coordinator": {
                "template": "# role profile: delegated_coordinator\n- <parent_project>",
                "placeholders": ["parent_project"],
            },
            "implementation_gateway": {
                "template": "# role profile: implementation_gateway\n- <lane>",
                "placeholders": ["lane"],
            },
            "implementation_worker": {
                "template": "# role profile: implementation_worker\n- <lane> <durable_anchor>",
                "placeholders": ["lane", "durable_anchor"],
            },
        },
    }


class ExtractPlaceholdersTest(unittest.TestCase):
    def test_ordered_unique(self) -> None:
        self.assertEqual(
            extract_placeholders("<a> <b> <a> <c_d>"), ("a", "b", "c_d")
        )

    def test_no_placeholders(self) -> None:
        self.assertEqual(extract_placeholders("no tokens here"), ())


class FromRecordValidTest(unittest.TestCase):
    def test_valid_record_builds_config(self) -> None:
        config = RoleProfileConfig.from_record(_valid_record())
        self.assertEqual(config.version, "2026-06-21")
        self.assertEqual(
            config.source, "vibes/docs/specs/delegated-coordinator-role-profile.md"
        )
        # Registry is rebuilt in fixed authority order regardless of input order.
        self.assertEqual(tuple(config.templates.keys()), KNOWN_ROLE_TOKENS)
        self.assertEqual(config.placeholders["coordinator"], ("project", "redmine_project"))

    def test_role_order_is_fixed_not_input_order(self) -> None:
        record = _valid_record()
        # Reverse insertion order of the roles mapping.
        record["roles"] = dict(reversed(list(record["roles"].items())))
        config = RoleProfileConfig.from_record(record)
        self.assertEqual(tuple(config.templates.keys()), KNOWN_ROLE_TOKENS)

    def test_placeholders_optional_derived_from_template(self) -> None:
        record = _valid_record()
        del record["roles"]["coordinator"]["placeholders"]
        config = RoleProfileConfig.from_record(record)
        self.assertEqual(
            config.placeholders["coordinator"], ("project", "redmine_project")
        )


class FromRecordFailClosedTest(unittest.TestCase):
    def test_non_mapping_fails_closed(self) -> None:
        for bad in (None, [], "x", 3):
            with self.assertRaises(RoleProfileConfigError):
                RoleProfileConfig.from_record(bad)

    def test_unknown_top_level_key_fails_closed(self) -> None:
        record = _valid_record()
        record["extra"] = "nope"
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_missing_version_fails_closed(self) -> None:
        record = _valid_record()
        del record["version"]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_blank_version_fails_closed(self) -> None:
        record = _valid_record()
        record["version"] = "   "
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_non_string_version_fails_closed(self) -> None:
        record = _valid_record()
        record["version"] = 20260621
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_missing_source_fails_closed(self) -> None:
        record = _valid_record()
        del record["source"]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_roles_not_mapping_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"] = ["coordinator"]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_unknown_role_token_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["bogus_role"] = {"template": "# x\n- <a>"}
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_missing_role_token_fails_closed(self) -> None:
        record = _valid_record()
        del record["roles"]["implementation_worker"]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_role_entry_not_mapping_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"] = "# not a mapping"
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_unknown_role_entry_key_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["surprise"] = "x"
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_empty_template_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["template"] = ""
        record["roles"]["coordinator"]["placeholders"] = []
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_blank_template_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["template"] = "   \n  "
        record["roles"]["coordinator"]["placeholders"] = []
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_non_string_template_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["template"] = ["# role profile"]
        record["roles"]["coordinator"]["placeholders"] = []
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_placeholder_mismatch_extra_declared_fails_closed(self) -> None:
        record = _valid_record()
        # Declare a placeholder the template does not contain.
        record["roles"]["coordinator"]["placeholders"] = [
            "project",
            "redmine_project",
            "ghost",
        ]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_placeholder_mismatch_wrong_order_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["placeholders"] = [
            "redmine_project",
            "project",
        ]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_placeholder_missing_declared_fails_closed(self) -> None:
        record = _valid_record()
        # Template has two tokens but declares only one.
        record["roles"]["coordinator"]["placeholders"] = ["project"]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_placeholders_not_list_fails_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["placeholders"] = "project"
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)

    def test_placeholders_non_string_items_fail_closed(self) -> None:
        record = _valid_record()
        record["roles"]["coordinator"]["placeholders"] = ["project", 3]
        with self.assertRaises(RoleProfileConfigError):
            RoleProfileConfig.from_record(record)


class PackagedConfigTest(unittest.TestCase):
    """The shipped artifact loads and matches the resolver's live registry."""

    def setUp(self) -> None:
        self.config = rp.load_packaged_role_profile_config()

    def test_packaged_config_is_valid_and_complete(self) -> None:
        self.assertEqual(tuple(self.config.templates.keys()), KNOWN_ROLE_TOKENS)

    def test_packaged_version_and_source_match_module_pointers(self) -> None:
        self.assertEqual(self.config.version, rp.ROLE_PROFILE_VERSION)
        self.assertEqual(self.config.source, rp.ROLE_PROFILE_SOURCE)

    def test_packaged_templates_match_module_registry(self) -> None:
        # The resolver's ROLE_PROFILE_TEMPLATES is sourced from this artifact, so
        # they must be byte-for-byte identical (regression against drift).
        self.assertEqual(dict(self.config.templates), rp.ROLE_PROFILE_TEMPLATES)

    def test_packaged_declared_placeholders_match_module_derivation(self) -> None:
        for token in KNOWN_ROLE_TOKENS:
            self.assertEqual(
                self.config.placeholders[token], rp.template_placeholders(token)
            )

    def test_packaged_config_round_trips_through_from_record(self) -> None:
        # A defensive copy through from_record is idempotent (no hidden mutation).
        record = {
            "version": self.config.version,
            "source": self.config.source,
            "roles": {
                token: {
                    "template": self.config.templates[token],
                    "placeholders": list(self.config.placeholders[token]),
                }
                for token in KNOWN_ROLE_TOKENS
            },
        }
        again = RoleProfileConfig.from_record(copy.deepcopy(record))
        self.assertEqual(dict(again.templates), dict(self.config.templates))


if __name__ == "__main__":
    unittest.main()
