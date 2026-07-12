"""Pure durable workflow-role authority tests (Redmine #13583).

Pins the pure role authority the herdr default lane pair lacked (j#75707): the closed role
vocabulary + ``root_coordinator`` compat alias, the versioned deterministic
``project_scope -> lane_id`` derivation, the fail-closed parse/validation of the static binding
declaration (unknown role / grandparent-with-scope / gateway-without-scope / duplicate grandparent
/ slot collision / schema / version), and the lane resolution matrix (resolved / missing /
ambiguous / invalid / provider mismatch). No IO — the loader is tested separately.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    DEFAULT_LANE,
    LANE_SCHEME,
    REASON_ROLE_BINDING_AMBIGUOUS,
    REASON_ROLE_BINDING_INVALID,
    REASON_ROLE_PROVIDER_MISMATCH,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    STATUS_AMBIGUOUS,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_PROVIDER_MISMATCH,
    STATUS_RESOLVED,
    ParsedRoleBindings,
    WorkflowRoleAuthorityError,
    WorkflowRoleBinding,
    normalize_role,
    parse_role_bindings,
    project_gateway_lane_id,
    resolve_role_for_lane,
)

CODEX = lambda role: "codex"  # noqa: E731 - a fixed expected-provider resolver for the tests


def _record(*bindings):
    return {"schema": SCHEMA_NAME, "version": SCHEMA_VERSION, "bindings": list(bindings)}


class NormalizeRoleTest(unittest.TestCase):
    def test_canonical_roles_pass_through(self):
        self.assertEqual(normalize_role("grandparent_coordinator"), ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(normalize_role("project_gateway"), ROLE_PROJECT_GATEWAY)

    def test_root_coordinator_is_compat_alias_for_grandparent(self):
        self.assertEqual(normalize_role("root_coordinator"), ROLE_GRANDPARENT_COORDINATOR)

    def test_whitespace_trimmed(self):
        self.assertEqual(normalize_role("  project_gateway  "), ROLE_PROJECT_GATEWAY)

    def test_unknown_and_empty_are_blank(self):
        self.assertEqual(normalize_role("delegated_coordinator"), "")
        self.assertEqual(normalize_role(""), "")
        self.assertEqual(normalize_role(None), "")


class ProjectGatewayLaneIdTest(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(
            project_gateway_lane_id("cloud-drive-management"),
            project_gateway_lane_id("cloud-drive-management"),
        )

    def test_versioned_prefix_and_readable_slug(self):
        lane = project_gateway_lane_id("Cloud Drive Management")
        self.assertTrue(lane.startswith(f"{LANE_SCHEME}_"))
        self.assertIn("cloud-drive-management", lane)

    def test_never_equals_default_lane(self):
        self.assertNotEqual(project_gateway_lane_id("default"), DEFAULT_LANE)

    def test_distinct_scopes_do_not_collide_even_when_slug_matches(self):
        # "a.b" and "a-b" slug to the same readable core; the digest keeps them distinct.
        self.assertNotEqual(project_gateway_lane_id("a.b"), project_gateway_lane_id("a-b"))

    def test_empty_scope_fails_closed(self):
        with self.assertRaises(WorkflowRoleAuthorityError):
            project_gateway_lane_id("")
        with self.assertRaises(WorkflowRoleAuthorityError):
            project_gateway_lane_id("   ")


class ParseRoleBindingsTest(unittest.TestCase):
    def test_absent_record_is_empty_valid(self):
        parsed = parse_role_bindings(None)
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.bindings, ())

    def test_grandparent_and_gateway_parse(self):
        parsed = parse_role_bindings(
            _record(
                {"role": "grandparent_coordinator", "source_pointer": "redmine:#13583"},
                {"role": "project_gateway", "project_scope": "cloud-drive-management"},
            )
        )
        self.assertTrue(parsed.ok)
        roles = {b.role for b in parsed.bindings}
        self.assertEqual(roles, {ROLE_GRANDPARENT_COORDINATOR, ROLE_PROJECT_GATEWAY})
        gp = next(b for b in parsed.bindings if b.role == ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(gp.lane_id, DEFAULT_LANE)
        self.assertEqual(gp.project_scope, "")
        pg = next(b for b in parsed.bindings if b.role == ROLE_PROJECT_GATEWAY)
        self.assertEqual(pg.lane_id, project_gateway_lane_id("cloud-drive-management"))

    def test_root_coordinator_alias_accepted(self):
        parsed = parse_role_bindings(_record({"role": "root_coordinator"}))
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.bindings[0].role, ROLE_GRANDPARENT_COORDINATOR)

    def test_unknown_role_fails_closed(self):
        parsed = parse_role_bindings(_record({"role": "delegated_coordinator"}))
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.reason, REASON_ROLE_BINDING_INVALID)

    def test_grandparent_with_scope_fails_closed(self):
        parsed = parse_role_bindings(
            _record({"role": "grandparent_coordinator", "project_scope": "x"})
        )
        self.assertFalse(parsed.ok)

    def test_gateway_without_scope_fails_closed(self):
        parsed = parse_role_bindings(_record({"role": "project_gateway"}))
        self.assertFalse(parsed.ok)

    def test_two_grandparents_fail_closed(self):
        parsed = parse_role_bindings(
            _record({"role": "grandparent_coordinator"}, {"role": "root_coordinator"})
        )
        self.assertFalse(parsed.ok)

    def test_slot_collision_fails_closed(self):
        parsed = parse_role_bindings(
            _record(
                {"role": "project_gateway", "project_scope": "same"},
                {"role": "project_gateway", "project_scope": "same"},
            )
        )
        self.assertFalse(parsed.ok)

    def test_two_distinct_gateways_ok(self):
        parsed = parse_role_bindings(
            _record(
                {"role": "project_gateway", "project_scope": "alpha"},
                {"role": "project_gateway", "project_scope": "beta"},
            )
        )
        self.assertTrue(parsed.ok)
        self.assertEqual(len({b.lane_id for b in parsed.bindings}), 2)

    def test_wrong_schema_fails_closed(self):
        self.assertFalse(parse_role_bindings({"schema": "other", "version": 1}).ok)

    def test_wrong_version_fails_closed(self):
        self.assertFalse(parse_role_bindings({"schema": SCHEMA_NAME, "version": 99}).ok)

    def test_non_object_record_fails_closed(self):
        self.assertFalse(parse_role_bindings([1, 2, 3]).ok)

    def test_bindings_not_a_list_fails_closed(self):
        self.assertFalse(
            parse_role_bindings({"schema": SCHEMA_NAME, "version": SCHEMA_VERSION, "bindings": {}}).ok
        )

    def test_entry_not_a_mapping_fails_closed(self):
        self.assertFalse(parse_role_bindings(_record("grandparent_coordinator")).ok)


class ResolveRoleForLaneTest(unittest.TestCase):
    def _parsed(self, *bindings):
        return parse_role_bindings(_record(*bindings))

    def test_default_lane_resolves_grandparent(self):
        parsed = self._parsed({"role": "grandparent_coordinator"})
        res = resolve_role_for_lane(parsed, lane_id="default", provider="codex", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_RESOLVED)
        self.assertEqual(res.role, ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(res.project_scope, "")
        self.assertTrue(res.resolved)

    def test_empty_lane_id_maps_to_default(self):
        parsed = self._parsed({"role": "grandparent_coordinator"})
        res = resolve_role_for_lane(parsed, lane_id="", provider="codex", expected_provider=CODEX)
        self.assertEqual(res.role, ROLE_GRANDPARENT_COORDINATOR)

    def test_project_scoped_lane_resolves_gateway(self):
        parsed = self._parsed({"role": "project_gateway", "project_scope": "cloud-drive-management"})
        lane = project_gateway_lane_id("cloud-drive-management")
        res = resolve_role_for_lane(parsed, lane_id=lane, provider="codex", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_RESOLVED)
        self.assertEqual(res.role, ROLE_PROJECT_GATEWAY)
        self.assertEqual(res.project_scope, "cloud-drive-management")

    def test_lane_without_binding_is_missing(self):
        parsed = self._parsed({"role": "grandparent_coordinator"})
        res = resolve_role_for_lane(parsed, lane_id="issue_1", provider="claude", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_MISSING)
        self.assertTrue(res.missing)
        self.assertFalse(res.resolved)

    def test_empty_declaration_is_missing(self):
        res = resolve_role_for_lane(ParsedRoleBindings.empty(), lane_id="default", provider="codex", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_MISSING)

    def test_invalid_declaration_blocks(self):
        parsed = self._parsed({"role": "unknown"})
        res = resolve_role_for_lane(parsed, lane_id="default", provider="codex", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_INVALID)
        self.assertTrue(res.blocked)
        self.assertEqual(res.reason, REASON_ROLE_BINDING_INVALID)

    def test_provider_mismatch_blocks(self):
        parsed = self._parsed({"role": "grandparent_coordinator"})
        res = resolve_role_for_lane(parsed, lane_id="default", provider="claude", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_PROVIDER_MISMATCH)
        self.assertEqual(res.reason, REASON_ROLE_PROVIDER_MISMATCH)

    def test_unresolvable_expected_provider_blocks(self):
        parsed = self._parsed({"role": "grandparent_coordinator"})
        res = resolve_role_for_lane(parsed, lane_id="default", provider="codex", expected_provider=lambda r: None)
        self.assertEqual(res.status, STATUS_PROVIDER_MISMATCH)

    def test_duplicate_match_is_ambiguous_defensive(self):
        # Validation rejects a slot collision; resolution still never guesses if handed two.
        b = WorkflowRoleBinding(role=ROLE_PROJECT_GATEWAY, project_scope="x", lane_id="lane_x")
        parsed = ParsedRoleBindings.valid([b, b])
        res = resolve_role_for_lane(parsed, lane_id="lane_x", provider="codex", expected_provider=CODEX)
        self.assertEqual(res.status, STATUS_AMBIGUOUS)
        self.assertEqual(res.reason, REASON_ROLE_BINDING_AMBIGUOUS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
