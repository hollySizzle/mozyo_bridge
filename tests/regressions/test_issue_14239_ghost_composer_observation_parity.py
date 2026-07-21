"""Regression: ghost-empty composer verdicts agree across rails (Redmine #14239).

#14222 j#84950: six staged lanes' `sublane hibernate --execute` all stopped on
`composer_pending_real` while the public `composer-render` diagnostic (j#84954)
reported every gateway composer as a provider-declared `dim` ghost placeholder and
every worker composer as empty. Root cause: `read_live_lane_activity` passed the raw
3-arg `read_render_ghost_facts` as `apply_ghost_empty`'s 1-arg `facts_reader`; the
arity `TypeError` was swallowed by the gate's fail-safe preserve, silently disabling
the #14065 Phase-2 ghost refinement on the hibernate action-time boundary only.
Separately (#14225 j#84928 shape), the scratch `herdr session-retire` composer
observation was text-only and never consulted the ghost gate at all, so an idle
scratch pair demanded a direct-owner discard approval for a ghost placeholder.

These tests run the REAL `apply_ghost_empty` + the REAL provider ghost policy (the
packaged v3 profiles declare `dim` for both built-in providers) and stub only the
process-spawning leaves (binary resolution, herdr state/pane reads, and the live
render read). The prior unit tests patched `apply_ghost_empty` itself, which is why
the arity defect could not be caught there — do not "fix" these tests by patching
the gate seam.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    read_live_lane_activity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_ghost_composer_gate import (  # noqa: E501
    RenderGhostFacts,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
    LiveSessionRetireOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wGhostParity"
LANE = "issue_14239_lane"

_HSS = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application"
    ".herdr_session_start._resolve_binary_or_die"
)
_STATE_READER = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure"
    ".herdr_state.HerdrCliAgentStateReader"
)
_TRANSPORT = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure"
    ".herdr_transport.HerdrCliTransport"
)
_OBSERVE = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_quarantine.observe_composer_text"
)
# The single live leaf under the REAL gate: module-global the default facts closure
# resolves at call time inside `apply_ghost_empty`.
_RENDER_FACTS = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_ghost_composer_observation.read_render_ghost_facts"
)


def _facts(style: str, provider: str = "codex", reason: str = "ok") -> RenderGhostFacts:
    return RenderGhostFacts(
        observed=True,
        readable=True,
        prompt_present=True,
        style_provenance=style,
        provider_id=provider,
        reason=reason,
    )


def _row(role: str, locator: str) -> dict:
    return {"name": encode_assigned_name(WS, role, LANE), "pane_id": locator}


class _Obs:
    def __init__(self, readable: bool, has_pending: Optional[bool]):
        self.readable = readable
        self.has_pending = has_pending
        self.marker_ids = ()


def _state_reader(state: str = "turn_ended") -> mock.MagicMock:
    inst = mock.MagicMock()
    inst.read_agent_state.return_value = mock.MagicMock(ok=True, state=state)
    return mock.MagicMock(return_value=inst)


def _transport(content: str = "ghost text") -> mock.MagicMock:
    inst = mock.MagicMock()
    inst.read_pane.return_value = mock.MagicMock(ok=True, content=content)
    return mock.MagicMock(return_value=inst)


class HibernateBoundaryGhostParityTest(unittest.TestCase):
    """The j#84954 seven-lane shape: text-pending + provider-declared dim render must
    classify as the safe `composer_ghost_observed`, never `composer_pending` — through
    the live activity path with the REAL gate and REAL packaged ghost policy."""

    def _rows(self):
        return [_row("codex", f"{WS}:p2"), _row("claude", f"{WS}:p3")]

    def _run(self, facts_by_locator, *, has_pending=True):
        def facts_reader(_repo_root, locator, *, env=None):  # live-read signature
            value = facts_by_locator(locator)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch(_HSS, return_value="herdr"), mock.patch(
            _STATE_READER, _state_reader()
        ), mock.patch(_TRANSPORT, _transport()), mock.patch(
            _OBSERVE, return_value=_Obs(True, has_pending)
        ), mock.patch(
            _RENDER_FACTS, side_effect=facts_reader
        ):
            return read_live_lane_activity(
                self._rows(), WS, LANE,
                repo_root=Path("."), env={}, runner=None, timeout=5.0,
            )

    def test_dim_ghost_is_ghost_observed_not_pending(self) -> None:
        # Both slots render a provider-declared dim ghost (the exact j#84954 gateway
        # shape). On the pre-#14239 code the arity TypeError silently preserved
        # pending and this asserted False fails with composer_pending=True.
        providers = {f"{WS}:p2": "codex", f"{WS}:p3": "claude"}
        act = self._run(lambda loc: _facts("dim", providers[loc]))
        self.assertTrue(act.readable)
        self.assertFalse(act.composer_pending)
        self.assertTrue(act.composer_ghost_observed)

    def test_normal_render_still_blocks_as_pending(self) -> None:
        # Real unsent input renders `normal` — never emptied, still a block signal.
        act = self._run(lambda loc: _facts("normal"))
        self.assertTrue(act.readable)
        self.assertTrue(act.composer_pending)
        self.assertFalse(act.composer_ghost_observed)

    def test_render_read_error_preserves_pending(self) -> None:
        # A failing live render read must preserve the text verdict (fail-closed),
        # exactly like the pre-fix behaviour — but now only for REAL read failures.
        act = self._run(lambda loc: RuntimeError("render read failed"))
        self.assertTrue(act.readable)
        self.assertTrue(act.composer_pending)

    def test_unresolved_provider_preserves_pending(self) -> None:
        # An authority-unresolved provider admits nothing (policy fail-closed).
        act = self._run(lambda loc: _facts("dim", provider=""))
        self.assertTrue(act.readable)
        self.assertTrue(act.composer_pending)

    def test_text_empty_composer_never_reads_render(self) -> None:
        # The render read runs only when the text observation reported pending.
        with mock.patch(_HSS, return_value="herdr"), mock.patch(
            _STATE_READER, _state_reader()
        ), mock.patch(_TRANSPORT, _transport()), mock.patch(
            _OBSERVE, return_value=_Obs(True, False)
        ), mock.patch(_RENDER_FACTS) as render:
            act = read_live_lane_activity(
                self._rows(), WS, LANE,
                repo_root=Path("."), env={}, runner=None, timeout=5.0,
            )
        self.assertTrue(act.readable)
        self.assertFalse(act.composer_pending)
        render.assert_not_called()


class ScratchRetireGhostParityTest(unittest.TestCase):
    """The #14225 j#84928 scratch-pair shape: `session-retire`'s composer observation
    must reuse the SAME provider ghost gate, so a dim ghost alone never demands a
    composer-discard approval while real (`normal`) input still does."""

    def _observe(self, facts_or_error, *, has_pending=True):
        ops = LiveSessionRetireOps(repo_root=Path("."), env={})

        def facts_reader(_repo_root, locator, *, env=None):
            if isinstance(facts_or_error, Exception):
                raise facts_or_error
            return facts_or_error

        with mock.patch(_HSS, return_value="herdr"), mock.patch(
            _TRANSPORT, _transport()
        ), mock.patch(_OBSERVE, return_value=_Obs(True, has_pending)), mock.patch(
            _RENDER_FACTS, side_effect=facts_reader
        ):
            return ops.observe_composer(f"{WS}:p9")

    def test_dim_ghost_reports_settled(self) -> None:
        # Pre-#14239 the text-only observation returned has_pending=True here and the
        # retire fence demanded a direct-owner discard approval for a ghost.
        readable, has_pending = self._observe(_facts("dim"))
        self.assertTrue(readable)
        self.assertIs(has_pending, False)

    def test_normal_render_still_pending(self) -> None:
        readable, has_pending = self._observe(_facts("normal"))
        self.assertTrue(readable)
        self.assertIs(has_pending, True)

    def test_render_read_error_preserves_pending(self) -> None:
        readable, has_pending = self._observe(RuntimeError("render read failed"))
        self.assertTrue(readable)
        self.assertIs(has_pending, True)

    def test_text_empty_composer_stays_settled(self) -> None:
        readable, has_pending = self._observe(_facts("dim"), has_pending=False)
        self.assertTrue(readable)
        self.assertIs(has_pending, False)


class CrossRailParityTest(unittest.TestCase):
    """Acceptance 1-2: the hibernate boundary and the scratch retire observation share
    one typed observation authority — the same dim-ghost facts yield the same empty
    verdict on both rails."""

    def test_same_facts_same_verdict_on_both_rails(self) -> None:
        facts = _facts("dim", provider="codex")

        hib = HibernateBoundaryGhostParityTest("test_dim_ghost_is_ghost_observed_not_pending")
        act = hib._run(lambda loc: facts)
        ret = ScratchRetireGhostParityTest("test_dim_ghost_reports_settled")
        readable, has_pending = ret._observe(facts)

        self.assertFalse(act.composer_pending)
        self.assertTrue(act.composer_ghost_observed)
        self.assertTrue(readable)
        self.assertIs(has_pending, False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
