"""Provider-neutral conversation loop tests (Redmine #13497).

The loop validates every provider turn against the closed schema, rejects a bad
candidate back into the conversation (never mutating), and returns ``Ready`` only
for a validated, decided, confirmed intent. A provider that fails closed aborts
the loop with no mutation. All exercised with a scripted human + a fake provider.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.conversation_loop import (
    Aborted,
    Cancelled,
    Ready,
    run_onboarding_conversation,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.conversation_port import (
    PROVIDER_UNAVAILABLE,
    ConversationContext,
    ConversationProviderError,
    Explain,
    IntentCandidate,
    SanitizedFacts,
    build_intent_schema,
    build_tool_schema,
)

_READY_INTENT = {
    "schema_version": 1,
    "action": "confirm_plan",
    "preset": "none",
    "backend": "herdr",
    "git_mode": "none",
    "rules_store": "central",
    "free_text_summary": "fresh non-git sync onboarding",
}


def _context() -> ConversationContext:
    return ConversationContext(
        facts=SanitizedFacts(
            state="unadopted",
            root_kind="non_git",
            path_risk="sync_or_cloud",
            adoption_marker="absent",
            herdr_available=True,
        ),
        intent_schema=build_intent_schema(),
        tool_schema=build_tool_schema(),
    )


class ScriptedProvider:
    """Yields a fixed list of turns; records how many contexts it saw."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.seen = []

    def converse(self, context):
        self.seen.append(context)
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class ScriptedIO:
    def __init__(self, replies):
        self._replies = list(replies)
        self.shown = []

    def show(self, text):
        self.shown.append(text)

    def prompt(self):
        return self._replies.pop(0) if self._replies else None


class LoopHappyPathTest(unittest.TestCase):
    def test_confirmed_decided_intent_is_ready(self):
        provider = ScriptedProvider([IntentCandidate(_READY_INTENT)])
        outcome = run_onboarding_conversation(provider, _context(), ScriptedIO([]))
        self.assertIsInstance(outcome, Ready)
        self.assertEqual(outcome.intent.preset, "none")

    def test_explain_then_intent(self):
        provider = ScriptedProvider(
            [Explain("what folder is this?"), IntentCandidate(_READY_INTENT)]
        )
        io = ScriptedIO(["a fresh folder"])
        outcome = run_onboarding_conversation(provider, _context(), io)
        self.assertIsInstance(outcome, Ready)
        self.assertIn("what folder is this?", io.shown)
        # The human reply was threaded into the second context.
        self.assertEqual(provider.seen[1].messages[-1]["text"], "a fresh folder")

    def test_explain_text_is_sanitized_before_render(self):
        # Even a provider that returns raw control chars is escaped at the loop's
        # render boundary — no raw ESC reaches the IO (j#74970 F2).
        provider = ScriptedProvider(
            [Explain("danger\x1b[2Jclear"), IntentCandidate(_READY_INTENT)]
        )
        io = ScriptedIO(["ok"])
        run_onboarding_conversation(provider, _context(), io)
        shown = "".join(io.shown)
        self.assertNotIn("\x1b", shown)
        self.assertIn("\\x1b", shown)


class LoopRejectionTest(unittest.TestCase):
    def test_invalid_candidate_loops_back_as_structured_error(self):
        bad = dict(_READY_INTENT, extra_key="oops")
        provider = ScriptedProvider(
            [IntentCandidate(bad), IntentCandidate(_READY_INTENT)]
        )
        outcome = run_onboarding_conversation(provider, _context(), ScriptedIO([]))
        self.assertIsInstance(outcome, Ready)
        # The rejection was fed back to the provider as a structured error.
        self.assertTrue(provider.seen[1].errors)
        self.assertEqual(provider.seen[1].errors[0]["error"], "unknown_key")

    def test_tool_overreach_shaped_value_rejected(self):
        overreach = dict(_READY_INTENT, preset="none; rm -rf /")
        provider = ScriptedProvider(
            [IntentCandidate(overreach), IntentCandidate(_READY_INTENT)]
        )
        outcome = run_onboarding_conversation(provider, _context(), ScriptedIO([]))
        self.assertIsInstance(outcome, Ready)
        self.assertEqual(
            provider.seen[1].errors[0]["error"], "field_shaped_like_injection"
        )

    def test_undecided_preset_keeps_asking(self):
        undecided = dict(_READY_INTENT, preset="undecided")
        provider = ScriptedProvider(
            [IntentCandidate(undecided), IntentCandidate(_READY_INTENT)]
        )
        io = ScriptedIO(["let's use none"])
        outcome = run_onboarding_conversation(provider, _context(), io)
        self.assertIsInstance(outcome, Ready)
        self.assertTrue(any("need more" in s for s in io.shown))

    def test_non_confirm_action_keeps_asking(self):
        proposing = dict(_READY_INTENT, action="propose")
        provider = ScriptedProvider(
            [IntentCandidate(proposing), IntentCandidate(_READY_INTENT)]
        )
        io = ScriptedIO(["yes go ahead"])
        outcome = run_onboarding_conversation(provider, _context(), io)
        self.assertIsInstance(outcome, Ready)


class LoopTerminationTest(unittest.TestCase):
    def test_cancel_action_cancels(self):
        cancel = dict(_READY_INTENT, action="cancel")
        provider = ScriptedProvider([IntentCandidate(cancel)])
        outcome = run_onboarding_conversation(provider, _context(), ScriptedIO([]))
        self.assertIsInstance(outcome, Cancelled)

    def test_eof_on_explain_cancels(self):
        provider = ScriptedProvider([Explain("hello?")])
        outcome = run_onboarding_conversation(provider, _context(), ScriptedIO([]))
        self.assertIsInstance(outcome, Cancelled)

    def test_provider_error_aborts_without_mutation(self):
        provider = ScriptedProvider(
            [ConversationProviderError(PROVIDER_UNAVAILABLE, "down")]
        )
        outcome = run_onboarding_conversation(provider, _context(), ScriptedIO([]))
        self.assertIsInstance(outcome, Aborted)
        self.assertEqual(outcome.code, PROVIDER_UNAVAILABLE)

    def test_non_convergence_aborts(self):
        provider = ScriptedProvider([Explain("q?")] * 50)
        io = ScriptedIO(["more"] * 50)
        outcome = run_onboarding_conversation(
            provider, _context(), io, max_turns=3
        )
        self.assertIsInstance(outcome, Aborted)
        self.assertEqual(outcome.code, "conversation_did_not_converge")


if __name__ == "__main__":
    unittest.main()
