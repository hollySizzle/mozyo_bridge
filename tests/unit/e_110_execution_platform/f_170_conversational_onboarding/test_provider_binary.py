"""Trusted provider-executable resolver tests (Redmine #13497 j#74942 / j#74946).

Executable identity is an outer boundary: the production binding must resolve the
provider CLI to a verified **realpath** before any subprocess. Pins all three
addendum corrections — realpath (not abspath) argv[0], absolute-only override,
and distinct-realpath ambiguity — plus the provider's no-subprocess-on-failure.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.onboarding_providers.claude_cli_provider import (
    SafeClaudeCliProvider,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.onboarding_providers.provider_binary import (
    CLAUDE_BINARY_ENV,
    resolve_claude_binary,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.conversation_port import (
    PROVIDER_UNAVAILABLE,
    ConversationContext,
    ConversationProviderError,
    SanitizedFacts,
    build_intent_schema,
    build_tool_schema,
)


def _context():
    return ConversationContext(
        facts=SanitizedFacts(state="unadopted", root_kind="non_git",
                             path_risk="normal", adoption_marker="absent",
                             herdr_available=True),
        intent_schema=build_intent_schema(), tool_schema=build_tool_schema(),
    )


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _exe(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    # --- explicit override ---
    def test_absolute_override_returns_realpath(self):
        exe = self._exe(self.base / "bin" / "claude")
        got = resolve_claude_binary({CLAUDE_BINARY_ENV: str(exe)})
        self.assertEqual(got, os.path.realpath(str(exe)))
        self.assertTrue(os.path.isabs(got))

    def test_relative_override_is_rejected(self):
        # A relative override must never be resolved against the cwd.
        exe = self._exe(self.base / "claude")
        os.chdir(self.base)
        self.addCleanup(lambda: os.chdir(Path(__file__).parent))
        with self.assertRaises(ConversationProviderError) as ctx:
            resolve_claude_binary({CLAUDE_BINARY_ENV: "claude"})
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_non_executable_override_is_rejected(self):
        plain = self.base / "bin" / "claude"
        plain.parent.mkdir(parents=True)
        plain.write_text("not exec", encoding="utf-8")  # no exec bit
        with self.assertRaises(ConversationProviderError):
            resolve_claude_binary({CLAUDE_BINARY_ENV: str(plain)})

    def test_symlink_override_returns_target_realpath(self):
        target = self._exe(self.base / "real" / "claude-0.1")
        link = self.base / "bin" / "claude"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        got = resolve_claude_binary({CLAUDE_BINARY_ENV: str(link)})
        self.assertEqual(got, os.path.realpath(str(target)))

    # --- trusted PATH search ---
    def test_path_single_match_resolves(self):
        d = self.base / "bin"
        self._exe(d / "claude")
        got = resolve_claude_binary({"PATH": str(d)})
        self.assertEqual(got, os.path.realpath(str(d / "claude")))

    def test_path_missing_is_unavailable(self):
        with self.assertRaises(ConversationProviderError) as ctx:
            resolve_claude_binary({"PATH": str(self.base / "empty")})
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)

    def test_two_distinct_realpaths_are_ambiguous(self):
        d1, d2 = self.base / "a", self.base / "b"
        self._exe(d1 / "claude")
        self._exe(d2 / "claude")  # a *different* real file
        with self.assertRaises(ConversationProviderError) as ctx:
            resolve_claude_binary({"PATH": os.pathsep.join([str(d1), str(d2)])})
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)
        self.assertIn("ambiguous", ctx.exception.message)

    def test_two_symlinks_to_same_target_resolve(self):
        target = self._exe(self.base / "real" / "claude")
        d1, d2 = self.base / "a", self.base / "b"
        d1.mkdir(parents=True); d2.mkdir(parents=True)
        (d1 / "claude").symlink_to(target)
        (d2 / "claude").symlink_to(target)
        got = resolve_claude_binary({"PATH": os.pathsep.join([str(d1), str(d2)])})
        self.assertEqual(got, os.path.realpath(str(target)))

    def test_unsafe_relative_path_component_is_rejected(self):
        with self.assertRaises(ConversationProviderError):
            resolve_claude_binary({"PATH": os.pathsep.join([str(self.base), "relative/dir"])})

    def test_empty_path_no_override_is_unavailable(self):
        with self.assertRaises(ConversationProviderError):
            resolve_claude_binary({"PATH": ""})


class ProviderResolverIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _exe(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_no_subprocess_when_resolution_fails(self):
        calls = []

        def _recording(argv, stdin, timeout):
            calls.append(argv)
            from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.onboarding_providers.claude_cli_provider import (
                RunResult,
            )
            return RunResult(returncode=0, stdout="{}")

        # binary=None + an env with no PATH/override → resolver fails closed.
        provider = SafeClaudeCliProvider(binary=None, env={"PATH": ""}, runner=_recording)
        with self.assertRaises(ConversationProviderError) as ctx:
            provider.converse(_context())
        self.assertEqual(ctx.exception.code, PROVIDER_UNAVAILABLE)
        self.assertEqual(calls, [])  # never spawned a subprocess

    def test_argv0_is_resolved_realpath(self):
        exe = self._exe(self.base / "bin" / "claude")
        provider = SafeClaudeCliProvider(
            binary=None, env={"PATH": str(self.base / "bin")},
            runner=lambda a, s, t: None,
        )
        self.assertEqual(provider.argv()[0], os.path.realpath(str(exe)))


if __name__ == "__main__":
    unittest.main()
