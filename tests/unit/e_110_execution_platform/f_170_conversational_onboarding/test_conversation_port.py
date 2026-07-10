"""Provider-neutral conversation port tests (Redmine #13497).

Sanitization (no path / hash / secret reaches the model) and the closed schema
projections that constrain the provider.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.conversation_port import (
    ConversationContext,
    SanitizedFacts,
    build_intent_json_schema,
    build_intent_schema,
    build_tool_schema,
    build_turn_json_schema,
    sanitize_facts,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.intent import (
    INTENT_PRESETS,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.preflight import (
    HerdrBinary,
    OnboardingPreflight,
)


def _preflight(**kw) -> OnboardingPreflight:
    base = dict(
        state="unadopted",
        root_kind="non_git",
        path_risk="sync_or_cloud",
        adoption_marker="absent",
        herdr_binary=HerdrBinary(
            state="resolved", source="path", path="/opt/private/herdr"
        ),
        notes=("/Users/secret/path in a note",),
    )
    base.update(kw)
    return OnboardingPreflight(**base)


class SanitizeFactsTest(unittest.TestCase):
    def test_drops_herdr_realpath_and_notes(self):
        facts = sanitize_facts(_preflight(), caution_reason="sync_or_cloud")
        record = facts.as_prompt_facts()
        blob = repr(record)
        self.assertNotIn("/opt/private/herdr", blob)
        self.assertNotIn("/Users/secret/path", blob)
        self.assertTrue(facts.herdr_available)
        self.assertEqual(facts.caution_reason, "sync_or_cloud")

    def test_herdr_unavailable_when_not_resolved(self):
        facts = sanitize_facts(
            _preflight(herdr_binary=HerdrBinary(state="missing", source="none"))
        )
        self.assertFalse(facts.herdr_available)

    def test_prompt_facts_keys_are_closed(self):
        facts = sanitize_facts(_preflight())
        self.assertEqual(
            set(facts.as_prompt_facts()),
            {
                "state",
                "root_kind",
                "path_risk",
                "adoption_marker",
                "herdr_available",
                "caution_reason",
            },
        )


class SchemaProjectionTest(unittest.TestCase):
    def test_intent_schema_enumerates_closed_presets(self):
        schema = build_intent_schema()
        self.assertEqual(set(schema["enums"]["preset"]), set(INTENT_PRESETS))
        self.assertIn("free_text_summary", schema["required_keys"])

    def test_intent_json_schema_forbids_extra_keys(self):
        js = build_intent_json_schema()
        self.assertFalse(js["additionalProperties"])
        self.assertEqual(js["properties"]["schema_version"]["const"], 1)

    def test_turn_json_schema_is_closed_oneof(self):
        js = build_turn_json_schema()
        branches = {b["properties"]["turn"]["const"] for b in js["oneOf"]}
        self.assertEqual(branches, {"explain", "intent"})
        for b in js["oneOf"]:
            self.assertFalse(b["additionalProperties"])

    def test_tool_schema_is_names_and_mutation_only(self):
        for tool in build_tool_schema():
            self.assertEqual(set(tool), {"name", "mutation", "actor"})


class ContextAccumulationTest(unittest.TestCase):
    def _ctx(self) -> ConversationContext:
        return ConversationContext(
            facts=sanitize_facts(_preflight()),
            intent_schema=build_intent_schema(),
            tool_schema=build_tool_schema(),
        )

    def test_with_human_and_error_are_immutable(self):
        ctx = self._ctx()
        ctx2 = ctx.with_human("hi").with_error({"error": "unknown_key"})
        self.assertEqual(ctx.messages, ())
        self.assertEqual(ctx.errors, ())
        self.assertEqual(ctx2.messages[0]["text"], "hi")
        self.assertEqual(ctx2.errors[0]["error"], "unknown_key")


if __name__ == "__main__":
    unittest.main()
