"""Safe Claude local-CLI binding tests (Redmine #13497 j#74915 / j#74919 / j#74928).

Focused mid-review evidence for the pre-commit safety audit: the fixed
zero-authority / closed-output argv, sanitized input, and fail-closed handling of
timeout / non-zero exit / malformed output — all without a live CLI (the live
probe is deferred to #13490). Every safety-bearing flag is asserted present, and
any argv mutation that would re-enable tools / MCP / customization is asserted
absent.
"""

from __future__ import annotations

import json
import subprocess
import unittest

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.onboarding_providers.claude_cli_provider import (
    EMPTY_MCP_CONFIG,
    RunResult,
    SafeClaudeCliProvider,
    build_safe_argv,
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
    build_turn_json_schema,
)


def _context(*, messages=()) -> ConversationContext:
    return ConversationContext(
        facts=SanitizedFacts(
            state="unadopted",
            root_kind="non_git",
            path_risk="sync_or_cloud",
            adoption_marker="absent",
            herdr_available=True,
            caution_reason="sync_or_cloud",
        ),
        intent_schema=build_intent_schema(),
        tool_schema=build_tool_schema(),
        messages=messages,
    )


def _envelope(result, *, is_error=False, stderr="") -> RunResult:
    payload = {"type": "result", "is_error": is_error, "result": result}
    return RunResult(returncode=0, stdout=json.dumps(payload), stderr=stderr)


def _argv_pairs(argv):
    """Map each ``--flag`` to the token immediately after it (or None)."""
    pairs = {}
    for i, tok in enumerate(argv):
        if tok.startswith("--"):
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            pairs.setdefault(tok, nxt)
    return pairs


class BuildSafeArgvTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.dumps(build_turn_json_schema(), sort_keys=True)
        self.argv = build_safe_argv("claude", "claude-opus-4-8", "SYS", self.schema)
        self.pairs = _argv_pairs(self.argv)

    def test_zero_tool_selector_is_tools_empty_not_allowed_tools(self):
        # --tools "" is the CLI's explicit disable-all selector.
        self.assertIn("--tools", self.argv)
        self.assertEqual(self.pairs["--tools"], "")
        # Reliance on --allowed-tools is forbidden (j#74928 finding item 2).
        self.assertNotIn("--allowed-tools", self.argv)
        self.assertNotIn("--allowedTools", self.argv)
        # Never "default" (all tools).
        self.assertNotEqual(self.pairs["--tools"], "default")

    def test_customization_isolation_flags_present(self):
        self.assertIn("--safe-mode", self.argv)
        self.assertIn("--disable-slash-commands", self.argv)
        self.assertIn("--exclude-dynamic-system-prompt-sections", self.argv)
        self.assertEqual(self.pairs["--setting-sources"], "")

    def test_zero_mcp_by_explicit_empty_config(self):
        self.assertIn("--strict-mcp-config", self.argv)
        self.assertEqual(self.pairs["--mcp-config"], EMPTY_MCP_CONFIG)
        # The empty config declares no server.
        self.assertEqual(json.loads(EMPTY_MCP_CONFIG), {"mcpServers": {}})

    def test_closed_turn_json_schema_constrains_generation(self):
        self.assertEqual(self.pairs["--json-schema"], self.schema)
        parsed = json.loads(self.pairs["--json-schema"])
        self.assertIn("oneOf", parsed)
        turns = {b["properties"]["turn"]["const"] for b in parsed["oneOf"]}
        self.assertEqual(turns, {"explain", "intent"})

    def test_print_json_nonpersistence_and_defense_in_depth(self):
        self.assertIn("--print", self.argv)
        self.assertEqual(self.pairs["--output-format"], "json")
        self.assertIn("--no-session-persistence", self.argv)
        self.assertEqual(self.pairs["--permission-mode"], "plan")
        self.assertEqual(self.pairs["--system-prompt"], "SYS")

    def test_no_authority_granting_flags(self):
        for forbidden in (
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
            "--add-dir",
            "--allowed-tools",
            "--fork-session",
        ):
            self.assertNotIn(forbidden, self.argv)

    def test_provider_argv_matches_builder(self):
        provider = SafeClaudeCliProvider(binary="claude", model="claude-opus-4-8")
        self.assertIn("--tools", provider.argv())
        self.assertIn("--safe-mode", provider.argv())
        self.assertNotIn("--allowed-tools", provider.argv())


class SanitizedPromptTest(unittest.TestCase):
    def test_prompt_carries_only_sanitized_facts_and_transcript(self):
        provider = SafeClaudeCliProvider()
        ctx = _context(messages=({"role": "human", "text": "help me set up"},))
        prompt = provider.build_prompt(ctx)
        parsed = json.loads(prompt)
        # No canonical path / hash / herdr realpath / secret channel exists at all.
        self.assertEqual(
            set(parsed["facts"]),
            {
                "state",
                "root_kind",
                "path_risk",
                "adoption_marker",
                "herdr_available",
                "caution_reason",
            },
        )
        self.assertEqual(parsed["transcript"][0]["text"], "help me set up")

    def test_prompt_never_embeds_a_path_or_secret(self):
        provider = SafeClaudeCliProvider()
        prompt = provider.build_prompt(_context())
        self.assertNotIn("/Users/", prompt)
        self.assertNotIn("MOZYO_ONBOARDING_GATE_SECRET", prompt)


class ConverseParsingTest(unittest.TestCase):
    def _provider(self, run):
        # Explicit binary keeps the parse tests hermetic (no resolver / real CLI).
        return SafeClaudeCliProvider(binary="claude", runner=lambda a, s, t: run(a, s, t))

    def test_explain_turn(self):
        provider = self._provider(
            lambda a, s, t: _envelope(json.dumps({"turn": "explain", "text": "hi?"}))
        )
        turn = provider.converse(_context())
        self.assertIsInstance(turn, Explain)
        self.assertEqual(turn.text, "hi?")

    def test_intent_turn_passthrough(self):
        intent = {"turn": "intent", "intent": {"action": "confirm_plan", "preset": "none"}}
        provider = self._provider(lambda a, s, t: _envelope(json.dumps(intent)))
        turn = provider.converse(_context())
        self.assertIsInstance(turn, IntentCandidate)
        self.assertEqual(turn.intent["preset"], "none")

    def test_result_as_object_not_string(self):
        # Under --json-schema the CLI may return result as a parsed object.
        provider = self._provider(
            lambda a, s, t: _envelope({"turn": "explain", "text": "obj"})
        )
        turn = provider.converse(_context())
        self.assertIsInstance(turn, Explain)
        self.assertEqual(turn.text, "obj")

    def test_unknown_turn_kind_fails_closed(self):
        provider = self._provider(
            lambda a, s, t: _envelope(json.dumps({"turn": "mutate", "cmd": "rm -rf /"}))
        )
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_intent_turn_with_extra_outer_key_rejected(self):
        # An intent turn smuggling an out-of-band key (e.g. a tool call) must be
        # rejected, not passed on the two type-checks alone (j#74970 F1).
        payload = {"turn": "intent", "intent": {"action": "confirm_plan"},
                   "tool_call": {"name": "Bash"}}
        provider = self._provider(lambda a, s, t: _envelope(json.dumps(payload)))
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_explain_turn_with_extra_outer_key_rejected(self):
        payload = {"turn": "explain", "text": "hi", "exfiltrate": "secret"}
        provider = self._provider(lambda a, s, t: _envelope(json.dumps(payload)))
        with self.assertRaises(ConversationProviderError):
            provider.converse(_context())

    def test_extra_outer_key_rejected_for_object_result(self):
        # Same rejection whether result is a JSON string or a parsed object.
        payload = {"turn": "intent", "intent": {"action": "confirm_plan"}, "x": 1}
        provider = self._provider(lambda a, s, t: _envelope(payload))
        with self.assertRaises(ConversationProviderError):
            provider.converse(_context())

    def test_explain_text_control_chars_are_sanitized(self):
        # An OSC title-set / bell escape in model text must not reach the terminal.
        payload = {"turn": "explain", "text": "hi\x1b]0;pwn\x07 there‮"}
        provider = self._provider(lambda a, s, t: _envelope(json.dumps(payload)))
        turn = provider.converse(_context())
        self.assertIsInstance(turn, Explain)
        self.assertNotIn("\x1b", turn.text)
        self.assertNotIn("\x07", turn.text)
        self.assertNotIn("‮", turn.text)
        self.assertIn("\\x1b", turn.text)
        self.assertIn("there", turn.text)


class ConverseFailClosedTest(unittest.TestCase):
    def test_nonzero_exit_fails_closed(self):
        provider = SafeClaudeCliProvider(
            binary="claude",
            runner=lambda a, s, t: RunResult(returncode=2, stdout="", stderr="boom"),
        )
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_stderr_and_raw_payload_never_leaked_into_error(self):
        secret_payload = "SENSITIVE-STDERR-abc123"
        provider = SafeClaudeCliProvider(
            binary="claude",
            runner=lambda a, s, t: RunResult(
                returncode=3, stdout="garbage", stderr=secret_payload
            ),
        )
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertNotIn(secret_payload, str(ctx.exception))
        self.assertNotIn("garbage", str(ctx.exception))

    def test_timeout_fails_closed(self):
        def _timeout(argv, stdin, timeout):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        provider = SafeClaudeCliProvider(binary="claude", runner=_timeout)
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_missing_binary_fails_closed(self):
        def _missing(argv, stdin, timeout):
            raise FileNotFoundError(2, "No such file", argv[0])

        provider = SafeClaudeCliProvider(binary="claude", runner=_missing)
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_non_json_output_fails_closed(self):
        provider = SafeClaudeCliProvider(
            binary="claude",
            runner=lambda a, s, t: RunResult(returncode=0, stdout="not json", stderr=""),
        )
        with self.assertRaises(ConversationProviderError):
            provider.converse(_context())

    def test_error_envelope_fails_closed(self):
        provider = SafeClaudeCliProvider(
            binary="claude",
            runner=lambda a, s, t: _envelope("x", is_error=True),
        )
        with self.assertRaises(ConversationProviderError):
            provider.converse(_context())

    def test_missing_result_fails_closed(self):
        def _no_result(argv, stdin, timeout):
            return RunResult(returncode=0, stdout=json.dumps({"type": "result"}))

        provider = SafeClaudeCliProvider(binary="claude", runner=_no_result)
        with self.assertRaises(ConversationProviderError):
            provider.converse(_context())

    def test_malformed_turn_json_fails_closed(self):
        provider = SafeClaudeCliProvider(
            binary="claude",
            runner=lambda a, s, t: _envelope("{not-json"),
        )
        with self.assertRaises(ConversationProviderError):
            provider.converse(_context())


if __name__ == "__main__":
    unittest.main()
