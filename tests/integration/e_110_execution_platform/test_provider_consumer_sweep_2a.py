"""Increment 2A acceptance: one injected snapshot flows through every consumer
(Redmine #13569, Coordinator Answer j#76969).

The load-bearing acceptance for Increment 2A:

1. a synthetic same-protocol provider, present only in an injected snapshot, is
   recognized by CLI choices, discovery, pane resolution, target selection, the
   handoff receiver vocabulary, and herdr target resolution — **with no provider
   literal added to any consumer source and no global monkeypatch** (every consumer is
   handed the SAME injected snapshot instance);
2. that provider is *recognizable* but is NOT auto-added to the expected launch
   topology (status "missing" / launch "ready" / doctor judge the expected topology,
   not the full registry — the known-vs-expected split);
3. an unknown or unlaunchable provider fails closed (no selectable role, not launchable)
   before any side effect.

The synthetic provider is fake, explicit placeholder data (strengthened-scanner rule).
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.cli_agents import (  # noqa: E402
    register as register_agents,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import (  # noqa: E402
    agent_discovery,
    pane_resolver,
    target_selector,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E402
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_receiver_vocab import (  # noqa: E402
    receiver_choices,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (  # noqa: E402
    herdr_target_resolution,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E402
    build_runtime_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E402
    AgentProviderProfileConfig,
)
from mozyo_bridge.application.commands_status import (  # noqa: E402
    ResolveSessionStatusUseCase,
    StatusQuery,
)


def _snapshot(profiles: dict):
    return build_runtime_snapshot(
        AgentProviderProfileConfig.from_record(
            {"version": "t", "source": "test-fixture", "profiles": profiles}
        ).to_registry()
    )


# A synthetic same-protocol provider whose process basename differs from its id.
SYNTH = _snapshot(
    {
        "mistral-cli": {
            "protocol": "interactive_cli_tui",
            "executable": {
                "command": "mistral",
                "env_override": "MOZYO_AGENT_MISTRAL_BINARY",
            },
            "discovery_aliases": ["mistral-cli"],
            "process_names": ["mistral"],
            "capabilities": ["interactive_tui", "launch_argv_override"],
        }
    }
)


class _FakeStatusSession:
    def __init__(self, *, windows):
        self._windows = list(windows)

    def session_exists(self, session):
        return True

    def list_windows(self, session):
        return list(self._windows)

    def capture_panes(self, session):
        return (True, "panes")


class OneSnapshotFlowsThroughEveryConsumer(unittest.TestCase):
    """The synthetic provider is recognized everywhere via the SAME injected snapshot."""

    def test_cli_agent_choices_include_the_injected_provider(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register_agents(sub, snapshot=SYNTH)
        ns = parser.parse_args(["agents", "list", "--agent", "mistral-cli"])
        self.assertEqual(ns.agent, "mistral-cli")

    def test_handoff_receiver_choices_include_the_injected_provider(self) -> None:
        self.assertEqual(receiver_choices(SYNTH), ["mistral-cli"])
        # default (no snapshot) stays the built-in pair — byte-identical
        self.assertEqual(receiver_choices(), ["claude", "codex"])

    def test_discovery_classifies_and_resolves_the_injected_provider(self) -> None:
        self.assertEqual(
            agent_discovery.classify_agent_kind("mistral-cli", snapshot=SYNTH),
            "mistral-cli",
        )
        res = agent_discovery.resolve_agent_role(
            pane_option_role="mistral-cli", snapshot=SYNTH
        )
        self.assertEqual(res.role, "mistral-cli")
        # inferred from the process basename that differs from the id
        self.assertEqual(
            agent_discovery.resolve_agent_role(process="mistral", snapshot=SYNTH).role,
            "mistral-cli",
        )

    def test_pane_resolver_receiver_identity_uses_the_injected_snapshot(self) -> None:
        self.assertTrue(
            pane_resolver.is_receiver_agent_process(
                "/x/mistral", "mistral-cli", snapshot=SYNTH
            )
        )
        # cross-binding still detectable: a claude process is not the mistral receiver
        self.assertFalse(
            pane_resolver.is_receiver_agent_process(
                "/x/claude", "mistral-cli", snapshot=SYNTH
            )
        )
        self.assertTrue(pane_resolver.is_agent_process("mistral", snapshot=SYNTH))

    def test_target_selector_makes_the_injected_provider_selectable(self) -> None:
        candidate = TargetCandidate(
            pane_id="%1",
            role="mistral-cli",
            role_source=ROLE_SOURCE_PANE_OPTION,
            confidence=CONFIDENCE_STRONG,
            ambiguous=False,
            session="s",
            window_name="cockpit",
            window_index="0",
            pane_index="0",
            active=False,
            workspace_id="w",
            workspace_label="w",
            lane_id="default",
            lane_label=None,
            repo_short="repo",
            repo_root="/repo",
            cwd="/repo",
            host="local",
            view_kind="cockpit_pane",
            branch="main",
        )
        query = target_selector.TargetSelectorQuery(role="mistral-cli")
        sel = target_selector.select_target([candidate], query, snapshot=SYNTH)
        self.assertEqual(sel.status, target_selector.SELECT_RESOLVED)
        self.assertEqual(sel.pane_id, "%1")

    def test_herdr_target_resolution_accepts_the_injected_provider(self) -> None:
        res = herdr_target_resolution.resolve_target_role(
            "mistral-cli", coordinator_provider="codex", snapshot=SYNTH
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.role, "mistral-cli")

    def test_no_source_literal_and_no_global_monkeypatch_needed(self) -> None:
        # Without the injected snapshot the synthetic provider is invisible everywhere —
        # proving recognition came from the injected value, not a global mutation.
        self.assertEqual(
            agent_discovery.classify_agent_kind("mistral-cli"), "unknown"
        )
        self.assertEqual(receiver_choices(), ["claude", "codex"])
        self.assertNotIn("mistral-cli", target_selector.SELECTABLE_ROLES)


class KnownVsExpectedSplit(unittest.TestCase):
    """A recognizable provider is not auto-added to the expected topology (missing)."""

    def test_status_missing_follows_expected_not_known(self) -> None:
        use_case = ResolveSessionStatusUseCase(
            _FakeStatusSession(windows=["claude", "codex"])
        )
        # Default expected == built-in pair: both present, nothing missing.
        self.assertEqual(use_case.resolve(StatusQuery(session="s")).missing_agents, ())
        # A profile-only provider added to the EXPECTED set shows as missing only because
        # it is expected — a provider that is merely known/recognizable never is.
        view = use_case.resolve(
            StatusQuery(session="s"),
            expected_providers=("claude", "codex", "mistral-cli"),
        )
        self.assertEqual(view.missing_agents, ("mistral-cli",))

    def test_status_recognizes_known_windows_but_only_reports_expected_missing(
        self,
    ) -> None:
        # Only claude present; codex (expected) is missing, and an unrelated window is not
        # invented as an agent.
        view = ResolveSessionStatusUseCase(
            _FakeStatusSession(windows=["claude", "shell"])
        ).resolve(StatusQuery(session="s"))
        self.assertEqual(view.agent_windows, ("claude",))
        self.assertEqual(view.missing_agents, ("codex",))


class UnknownAndUnlaunchableFailClosed(unittest.TestCase):
    def test_unknown_role_is_not_selectable(self) -> None:
        query = target_selector.TargetSelectorQuery(role="nope")
        sel = target_selector.select_target([], query, snapshot=SYNTH)
        self.assertEqual(sel.status, target_selector.SELECT_INVALID_ROLE)
        self.assertIsNone(sel.pane_id)

    def test_unknown_receiver_fails_closed_in_herdr_resolution(self) -> None:
        res = herdr_target_resolution.resolve_target_role(
            "claude", coordinator_provider="codex", snapshot=SYNTH
        )
        # 'claude' is NOT in the synthetic snapshot, so it is an unknown receiver there.
        self.assertFalse(res.ok)

    def test_unlaunchable_provider_is_recognized_but_not_launchable(self) -> None:
        snap = _snapshot(
            {
                "batchbot": {
                    "protocol": "interactive_cli_tui",
                    "executable": {
                        "command": "batchbot",
                        "env_override": "MOZYO_AGENT_BATCHBOT_BINARY",
                    },
                    "capabilities": ["launch_argv_override"],
                }
            }
        )
        self.assertTrue(snap.is_provider("batchbot"))
        self.assertFalse(snap.is_launchable("batchbot"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
