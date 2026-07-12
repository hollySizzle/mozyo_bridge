"""Durable workflow-role binding loader tests (Redmine #13583).

Hermetic: writes the tracked static artifact under a temp repo root. Pins the IO contract — an
absent file resolves to an empty (valid) declaration (behavior-preserving), a present-but-broken
file fails closed to an invalid declaration (never silently "no bindings"), and a well-formed
file round-trips through the pure parser. Error detail is path-free.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (
    load_parsed_role_bindings,
    role_bindings_path,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
)


class LoadParsedRoleBindingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _write(self, text: str):
        path = role_bindings_path(self.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_absent_file_is_empty_valid(self):
        parsed = load_parsed_role_bindings(self.repo)
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.bindings, ())

    def test_well_formed_file_parses(self):
        self._write(
            json.dumps(
                {
                    "schema": SCHEMA_NAME,
                    "version": SCHEMA_VERSION,
                    "bindings": [{"role": "grandparent_coordinator"}],
                }
            )
        )
        parsed = load_parsed_role_bindings(self.repo)
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.bindings[0].role, ROLE_GRANDPARENT_COORDINATOR)

    def test_malformed_json_fails_closed(self):
        self._write("{ not json ]")
        parsed = load_parsed_role_bindings(self.repo)
        self.assertFalse(parsed.ok)
        self.assertIn("not valid JSON", parsed.detail)
        # Path-free: the temp repo root must never leak into the error detail.
        self.assertNotIn(str(self.repo), parsed.detail)

    def test_semantically_invalid_file_fails_closed(self):
        self._write(
            json.dumps({"schema": SCHEMA_NAME, "version": SCHEMA_VERSION, "bindings": [{"role": "x"}]})
        )
        self.assertFalse(load_parsed_role_bindings(self.repo).ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
