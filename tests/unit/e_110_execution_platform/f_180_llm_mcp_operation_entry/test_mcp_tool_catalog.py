"""Tool catalog + negative-surface specifications (Redmine #15161 / #15163).

The acceptance this file exists for: *no arbitrary command, shell argv, raw pane /
tmux operation, or mutating operation exists in the schema*. Proving absence needs
two halves, and the second is the one that usually gets skipped:

1. the shipped catalog reports no violation; and
2. the guard actually detects one when it is present.

A guard that has never rejected anything is indistinguishable from a guard that
cannot reject anything, so every forbidden category is fed to
:func:`catalog_surface_violations` through a synthetic catalog and asserted to be
caught.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E402,E501
    FORBIDDEN_PROPERTY_TOKENS,
    MUTATING_TOOL_NAMES,
    SUPPORTED_SCHEMA_KEYWORDS,
    TOOL_CATALOG,
    TOOL_NAMES,
    TOOL_UNIT_STATE,
    ToolDefinition,
    UnknownToolError,
    catalog_surface_violations,
    default_arguments,
    list_tools_payload,
    resolve_arguments,
    tool_definition,
    validate_arguments,
)


def synthetic(name: str, input_schema: dict, *, read_only: bool = True) -> dict:
    return {
        name: ToolDefinition(
            name=name,
            title=name,
            description="synthetic",
            input_schema=input_schema,
            output_schema={"type": "object"},
            read_only=read_only,
        )
    }


class ShippedCatalogTests(unittest.TestCase):
    # Contract updated by #15152 (was: "the closed four-tool vocabulary, every
    # tool read-only, no tool name suggests a mutating operation"). The catalog
    # now publishes the four read/plan tools PLUS the three DECLARED mutating
    # tools; the negative surface (no pane / tmux / command capability) is
    # unchanged and still guard-enforced on every tool, mutating included.
    def test_catalog_is_the_closed_seven_tool_vocabulary(self) -> None:
        self.assertEqual(tuple(TOOL_CATALOG), TOOL_NAMES)
        self.assertEqual(len(TOOL_NAMES), 7)

    def test_shipped_catalog_publishes_no_forbidden_capability(self) -> None:
        self.assertEqual(catalog_surface_violations(), ())

    def test_read_only_split_matches_the_declared_mutating_set(self) -> None:
        # Was `test_every_shipped_tool_is_read_only` before #15152: read-only is
        # now exactly the complement of the closed MUTATING_TOOL_NAMES set, and
        # the annotations a client plans around match the declaration.
        for name, definition in TOOL_CATALOG.items():
            expected_read_only = name not in MUTATING_TOOL_NAMES
            self.assertEqual(definition.read_only, expected_read_only, name)
            annotations = definition.as_payload()["annotations"]
            self.assertEqual(annotations["readOnlyHint"], expected_read_only, name)
            self.assertEqual(annotations["idempotentHint"], expected_read_only, name)
            self.assertFalse(annotations["destructiveHint"], name)

    def test_no_read_tool_name_suggests_a_mutating_operation(self) -> None:
        """The read/plan tool names stay free of mutating verbs (#15152 keeps
        the mutating names to exactly the declared closed set)."""
        for forbidden in ("send", "reply", "create", "retire", "dispatch", "execute"):
            for name in TOOL_NAMES:
                if name in MUTATING_TOOL_NAMES:
                    continue
                self.assertNotIn(forbidden, name, f"{name} looks mutating")
        self.assertEqual(
            MUTATING_TOOL_NAMES,
            frozenset({"handoff_send", "handoff_reply", "sublane_start"}),
        )

    def test_tools_list_payload_is_json_serializable_plain_containers(self) -> None:
        import json

        payload = list_tools_payload()
        self.assertEqual(len(payload), 7)
        json.dumps(payload)  # must not raise on MappingProxyType / tuple
        for tool in payload:
            self.assertIsInstance(tool["inputSchema"], dict)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_catalog_cannot_be_mutated_through_a_published_schema(self) -> None:
        schema = TOOL_CATALOG[TOOL_UNIT_STATE].input_schema
        with self.assertRaises(TypeError):
            schema["properties"] = {}  # type: ignore[index]

    def test_unknown_tool_fails_closed(self) -> None:
        # `handoff_send` became a published tool in #15152; the fail-closed
        # probe now uses names that stay outside the closed vocabulary.
        for unknown in ("workflow_step_run", "sublane_retire", "cockpit_append"):
            with self.assertRaises(UnknownToolError):
                tool_definition(unknown)


class SurfaceGuardDetectionTests(unittest.TestCase):
    """The guard must actually catch each forbidden category."""

    def _violations_for(self, schema: dict) -> tuple:
        return catalog_surface_violations(synthetic("probe", schema))

    def test_arbitrary_command_property_is_caught(self) -> None:
        violations = self._violations_for(
            {"type": "object", "properties": {"command": {"type": "string"}}}
        )
        self.assertTrue(violations)
        self.assertIn("command", violations[0])

    def test_shell_argv_property_is_caught(self) -> None:
        self.assertTrue(
            self._violations_for(
                {"type": "object", "properties": {"argv": {"type": "array"}}}
            )
        )

    def test_raw_pane_and_tmux_properties_are_caught(self) -> None:
        for prop in ("pane_id", "target_pane", "tmux_session", "send_keys"):
            self.assertTrue(
                self._violations_for(
                    {"type": "object", "properties": {prop: {"type": "string"}}}
                ),
                prop,
            )

    def test_a_nested_forbidden_property_is_caught(self) -> None:
        self.assertTrue(
            self._violations_for(
                {
                    "type": "object",
                    "properties": {
                        "unit": {
                            "type": "object",
                            "properties": {"pane": {"type": "string"}},
                        }
                    },
                }
            )
        )

    def test_a_forbidden_property_inside_array_items_is_caught(self) -> None:
        self.assertTrue(
            self._violations_for(
                {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"shell": {"type": "string"}},
                            },
                        }
                    },
                }
            )
        )

    def test_a_forbidden_enum_value_is_caught(self) -> None:
        self.assertTrue(
            self._violations_for(
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["read", "exec"]}
                    },
                }
            )
        )

    def test_a_non_read_only_tool_is_caught(self) -> None:
        violations = catalog_surface_violations(
            synthetic("probe", {"type": "object"}, read_only=False)
        )
        self.assertTrue(any("non-read-only" in v for v in violations))

    def test_an_unenforceable_schema_keyword_is_caught(self) -> None:
        """A declared constraint the validator cannot check is a catalog defect."""
        violations = self._violations_for(
            {"type": "object", "properties": {"n": {"type": "integer", "maximum": 5}}}
        )
        self.assertTrue(any("unsupported schema keyword" in v for v in violations))

    def test_every_shipped_schema_keyword_is_supported(self) -> None:
        def keywords(schema, acc):
            if isinstance(schema, dict) or hasattr(schema, "keys"):
                acc.update(schema.keys())
                props = schema.get("properties")
                if props is not None:
                    for sub in props.values():
                        keywords(sub, acc)
                items = schema.get("items")
                if items is not None:
                    keywords(items, acc)
            return acc

        used: set = set()
        for definition in TOOL_CATALOG.values():
            keywords(definition.input_schema, used)
        self.assertEqual(used - SUPPORTED_SCHEMA_KEYWORDS, set())

    def test_forbidden_tokens_cover_the_named_boundary_categories(self) -> None:
        for token in ("command", "argv", "shell", "tmux", "pane", "exec", "send"):
            self.assertIn(token, FORBIDDEN_PROPERTY_TOKENS)


class ArgumentValidationTests(unittest.TestCase):
    def test_valid_arguments_produce_no_violations(self) -> None:
        definition = tool_definition("docs_resolve")
        self.assertEqual(validate_arguments(definition, {"paths": ["a.py"]}), ())

    def test_missing_required_property_is_reported(self) -> None:
        definition = tool_definition("docs_resolve")
        self.assertTrue(validate_arguments(definition, {}))

    def test_every_violation_is_reported_not_just_the_first(self) -> None:
        definition = tool_definition(TOOL_UNIT_STATE)
        violations = validate_arguments(definition, {"unit": {"workspace_id": "w"}})
        self.assertEqual(len(violations), 2)
        self.assertTrue(any("lane_id" in v for v in violations))
        self.assertTrue(any("project_id" in v for v in violations))

    def test_unknown_property_is_refused(self) -> None:
        definition = tool_definition("docs_resolve")
        violations = validate_arguments(definition, {"paths": ["a"], "extra": 1})
        self.assertTrue(any("unknown property" in v for v in violations))

    def test_wrong_type_is_refused(self) -> None:
        definition = tool_definition("docs_resolve")
        self.assertTrue(validate_arguments(definition, {"paths": "a.py"}))

    def test_a_boolean_is_not_accepted_as_an_integer(self) -> None:
        definition = ToolDefinition(
            name="probe",
            title="probe",
            description="",
            input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            output_schema={"type": "object"},
        )
        self.assertTrue(validate_arguments(definition, {"n": True}))

    def test_empty_array_violates_min_items(self) -> None:
        definition = tool_definition("docs_resolve")
        violations = validate_arguments(definition, {"paths": []})
        self.assertTrue(any("at least 1" in v for v in violations))

    def test_enum_violation_is_reported_with_the_allowed_set(self) -> None:
        definition = tool_definition(TOOL_UNIT_STATE)
        violations = validate_arguments(
            definition,
            {
                "unit": {"workspace_id": "w", "lane_id": "l", "project_id": "p"},
                "axes": ["nope"],
            },
        )
        self.assertTrue(any("not one of" in v for v in violations))

    def test_a_string_is_not_treated_as_an_array(self) -> None:
        definition = ToolDefinition(
            name="probe",
            title="probe",
            description="",
            input_schema={
                "type": "object",
                "properties": {"xs": {"type": "array", "items": {"type": "string"}}},
            },
            output_schema={"type": "object"},
        )
        self.assertTrue(validate_arguments(definition, {"xs": "abc"}))

    def test_defaults_are_applied_without_overriding_the_caller(self) -> None:
        definition = tool_definition("docs_resolve")
        self.assertEqual(default_arguments(definition), {"include_local": True})
        self.assertEqual(
            resolve_arguments(definition, {"paths": ["a"], "include_local": False}),
            {"paths": ["a"], "include_local": False},
        )
        self.assertTrue(
            resolve_arguments(definition, {"paths": ["a"]})["include_local"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
