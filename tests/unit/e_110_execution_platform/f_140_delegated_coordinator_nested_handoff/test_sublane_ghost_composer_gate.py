"""Specs for the Phase 2 dim-ghost pending-composer empty gate (Redmine #14065).

Covers the pure render gate (policy + facts + decision), the e140 policy factory, and
the reconcile-ops seam wiring — the adversarial matrix the IR (j#82181) requires:
Claude/Codex dim ghost empties; exact-same-text normal input preserves; mixed / unknown
/ unreadable / no-prompt / unobserved / foreign-provider / no-policy all preserve; and a
text observation that never reported pending is untouched. All sanitized / hermetic — no
herdr spawn, no pane body in any fixture.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile_ops import (  # noqa: E501
    LiveReconcileOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_ghost_composer_gate import (  # noqa: E501
    GhostComposerRenderPolicy,
    RenderGhostFacts,
    render_admits_empty,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    PaneReadResult,
)


def _facts(**overrides) -> RenderGhostFacts:
    base = dict(
        observed=True,
        readable=True,
        prompt_present=True,
        style_provenance="dim",
        provider_id="claude",
        reason="ok",
    )
    base.update(overrides)
    return RenderGhostFacts(**base)


_POLICY = GhostComposerRenderPolicy.from_pairs(
    {"claude": frozenset({"dim"}), "codex": frozenset({"dim"})}
)


class RenderAdmitsEmptyMatrixTest(unittest.TestCase):
    def _empties(self, *, text_has_pending=True, facts=None, policy=_POLICY) -> bool:
        return render_admits_empty(
            text_has_pending=text_has_pending,
            facts=facts if facts is not None else _facts(),
            policy=policy,
        )

    def test_claude_dim_ghost_with_text_pending_empties(self) -> None:
        self.assertTrue(self._empties())

    def test_codex_dim_ghost_empties(self) -> None:
        self.assertTrue(self._empties(facts=_facts(provider_id="codex")))

    def test_text_not_pending_never_empties(self) -> None:
        self.assertFalse(self._empties(text_has_pending=False))
        self.assertFalse(self._empties(text_has_pending=None))

    def test_normal_real_input_preserves(self) -> None:
        self.assertFalse(self._empties(facts=_facts(style_provenance="normal")))

    def test_mixed_preserves(self) -> None:
        self.assertFalse(self._empties(facts=_facts(style_provenance="mixed")))

    def test_unknown_unreadable_preserves(self) -> None:
        self.assertFalse(
            self._empties(facts=_facts(readable=False, style_provenance="unknown"))
        )

    def test_no_prompt_preserves(self) -> None:
        self.assertFalse(self._empties(facts=_facts(prompt_present=False)))

    def test_unobserved_preserves(self) -> None:
        self.assertFalse(self._empties(facts=RenderGhostFacts.unobserved()))

    def test_foreign_provider_preserves(self) -> None:
        self.assertFalse(self._empties(facts=_facts(provider_id="mystery")))

    def test_empty_policy_preserves(self) -> None:
        self.assertFalse(self._empties(policy=GhostComposerRenderPolicy.empty()))

    def test_provider_that_admits_only_dim_rejects_normal(self) -> None:
        self.assertFalse(_POLICY.admits("claude", "normal"))
        self.assertTrue(_POLICY.admits("claude", "dim"))

    def test_admits_non_string_is_false(self) -> None:
        self.assertFalse(_POLICY.admits(None, "dim"))
        self.assertFalse(_POLICY.admits("claude", None))


class PolicyFactoryTest(unittest.TestCase):
    def test_built_from_v3_profiles_admits_dim_for_both_providers(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_ghost_policy import (  # noqa: E501
            build_ghost_composer_policy,
        )

        policy = build_ghost_composer_policy()
        self.assertTrue(policy.admits("claude", "dim"))
        self.assertTrue(policy.admits("codex", "dim"))
        self.assertFalse(policy.admits("claude", "normal"))
        self.assertFalse(policy.admits("nonprovider", "dim"))

    def test_provider_without_signals_admits_nothing(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_ghost_policy import (  # noqa: E501
            build_ghost_composer_policy,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
            AgentProviderProfile,
            AgentProviderProfileRegistry,
        )

        bare = AgentProviderProfile.from_record(
            "claude",
            {
                "protocol": "interactive_cli_tui",
                "executable": {
                    "command": "claude",
                    "env_override": "MOZYO_AGENT_CLAUDE_BINARY",
                },
            },
            schema_version="3",
        )
        registry = AgentProviderProfileRegistry()
        registry.register(bare)
        policy = build_ghost_composer_policy(registry)
        self.assertFalse(policy.admits("claude", "dim"))


class RedactionTest(unittest.TestCase):
    def test_facts_and_policy_carry_no_body(self) -> None:
        facts = _facts()
        # The facts type exposes only closed enums / bools / identity — no body fields.
        for banned in ("body", "content", "text", "ansi", "hash", "length", "excerpt"):
            self.assertFalse(hasattr(facts, banned))


class FakeTransport:
    def __init__(self, content: str):
        self._content = content

    def __call__(self, *args, **kwargs):  # constructed as HerdrCliTransport(binary, ...)
        return self

    def read_pane(self, locator, lines=80):
        return PaneReadResult.success(self._content)


class ReconcileOpsSeamTest(unittest.TestCase):
    """The site-D seam: a dim ghost empties has_pending; everything else preserves."""

    _PENDING = "some scrollback\n› pending looking body text"

    def _observe(self, *, facts, policy, content=_PENDING):
        ops = LiveReconcileOps(
            repo_root=Path("/nonexistent"),
            env={},
            ghost_policy=policy,
            render_facts_reader=(lambda locator: facts),
        )
        transport = FakeTransport(content)
        with mock.patch.object(LiveReconcileOps, "_reader", return_value="binary"), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "infrastructure.herdr_transport.HerdrCliTransport",
            transport,
        ):
            return ops.observe_composer("w1:p2")

    def test_dim_ghost_empties_pending(self) -> None:
        readable, has_pending = self._observe(facts=_facts(), policy=_POLICY)
        self.assertTrue(readable)
        self.assertFalse(has_pending)  # emptied

    def test_normal_input_preserves_pending(self) -> None:
        readable, has_pending = self._observe(
            facts=_facts(style_provenance="normal"), policy=_POLICY
        )
        self.assertTrue(readable)
        self.assertTrue(has_pending)  # preserved

    def test_unobserved_render_preserves_pending(self) -> None:
        readable, has_pending = self._observe(
            facts=RenderGhostFacts.unobserved(), policy=_POLICY
        )
        self.assertTrue(has_pending)

    def test_no_policy_preserves_pending(self) -> None:
        # The default (no injected policy) is byte-unchanged: the render read is skipped.
        readable, has_pending = self._observe(facts=_facts(), policy=None)
        self.assertTrue(has_pending)

    def test_empty_composer_not_pending_unaffected(self) -> None:
        # A composer the text observer already reports as not-pending never runs the gate.
        readable, has_pending = self._observe(
            facts=_facts(), policy=_POLICY, content="› "
        )
        self.assertFalse(bool(has_pending))


class RenderGhostFactsReaderTest(unittest.TestCase):
    """The default facts reader maps the e140 render view onto the domain facts."""

    def _repo(self, tmp, *, herdr):
        repo = Path(tmp) / "repo"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        body = "version: 1\nterminal_transport:\n  backend: herdr\n" if herdr else "version: 1\n"
        (repo / ".mozyo-bridge" / "config.yaml").write_text(body, encoding="utf-8")
        return repo

    def test_non_herdr_backend_is_unobserved(self) -> None:
        import tempfile

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
            read_render_ghost_facts,
        )

        with tempfile.TemporaryDirectory() as tmp:
            facts = read_render_ghost_facts(self._repo(tmp, herdr=False), "w1:p2", env={})
        self.assertFalse(facts.observed)
        self.assertFalse(
            render_admits_empty(text_has_pending=True, facts=facts, policy=_POLICY)
        )

    def test_maps_view_to_facts(self) -> None:
        import tempfile

        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation as helper
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            ComposerRenderView,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.pane_render_observation import (  # noqa: E501
            PaneRenderObservation,
        )

        view = ComposerRenderView(
            backend_selected=True,
            target="w1:p2",
            provider="codex",
            observation=PaneRenderObservation.classified("dim"),
        )
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_observability.read_composer_render",
            return_value=view,
        ), tempfile.TemporaryDirectory() as tmp:
            facts = helper.read_render_ghost_facts(self._repo(tmp, herdr=True), "w1:p2", env={})
        self.assertTrue(facts.observed)
        self.assertTrue(facts.readable)
        self.assertTrue(facts.prompt_present)
        self.assertEqual("dim", facts.style_provenance)
        self.assertEqual("codex", facts.provider_id)
        self.assertTrue(
            render_admits_empty(text_has_pending=True, facts=facts, policy=_POLICY)
        )


if __name__ == "__main__":
    unittest.main()
