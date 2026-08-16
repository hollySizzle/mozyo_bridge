"""Redmine #15531 — the 2.0 default terminal backend is herdr, and the flip is honest.

The 1.x default was tmux; 2.0 flips :data:`DEFAULT_TERMINAL_BACKEND` to herdr as
part of the single 1.x -> 2.0 upgrade contract (#15529). What this file pins is
not the constant alone but the honesty around it:

- an UNDECLARED selection is distinguishable from a declared one
  (``backend_declared``), without changing equality — routing must not care HOW
  a backend was selected, but doctor must be able to tell the upgrading
  operator whose target silently became herdr how to stay on tmux;
- doctor's herdr section, failing on a host without the binary, names BOTH
  routes out for an undeclared target: install herdr (with the install route),
  or declare ``terminal_transport.backend: tmux``;
- an absent config file / absent ``terminal_transport`` block resolves to
  herdr-undeclared; an explicit declaration of either backend is marked
  declared and is untouched by the flip.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E402,E501
    BACKEND_HERDR,
    BACKEND_TMUX,
    DEFAULT_TERMINAL_BACKEND,
    TerminalTransportConfig,
)
from mozyo_bridge.application.doctor_herdr import (  # noqa: E402
    evaluate_herdr_section,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E402,E501
    HerdrInventoryView,
)


class DefaultFlipTest(unittest.TestCase):
    """The flipped constant and the declared/undeclared distinction."""

    def test_the_default_backend_is_herdr(self) -> None:
        self.assertEqual(BACKEND_HERDR, DEFAULT_TERMINAL_BACKEND)

    def test_an_absent_record_is_the_undeclared_herdr_default(self) -> None:
        for config in (
            TerminalTransportConfig.default(),
            TerminalTransportConfig.from_record(None),
            TerminalTransportConfig.from_record({"version": 1}),
        ):
            self.assertEqual(BACKEND_HERDR, config.backend)
            self.assertFalse(config.backend_declared)

    def test_an_explicit_declaration_is_marked_declared(self) -> None:
        tmux = TerminalTransportConfig.from_record({"version": 1, "backend": "tmux"})
        herdr = TerminalTransportConfig.from_record({"version": 1, "backend": "herdr"})

        self.assertEqual(BACKEND_TMUX, tmux.backend)
        self.assertTrue(tmux.backend_declared)
        self.assertEqual(BACKEND_HERDR, herdr.backend)
        self.assertTrue(herdr.backend_declared)

    def test_declaredness_is_metadata_not_identity(self) -> None:
        # Routing must not care HOW a backend was selected: a declared and an
        # undeclared selection of the same backend compare equal everywhere.
        self.assertEqual(
            TerminalTransportConfig(backend=BACKEND_HERDR),
            TerminalTransportConfig.default(),
        )

    def test_an_absent_config_file_resolves_to_the_undeclared_default(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
            RepoLocalConfig,
        )

        config = RepoLocalConfig.default().terminal_transport

        self.assertEqual(BACKEND_HERDR, config.backend)
        self.assertFalse(config.backend_declared)


class DoctorUndeclaredGuidanceTest(unittest.TestCase):
    """Doctor tells the upgrading tmux operator how to stay on tmux (#15531)."""

    def _error_view(self, *, declared: bool, reason: str) -> HerdrInventoryView:
        return HerdrInventoryView(
            backend_selected=True,
            backend_declared=declared,
            ok=False,
            reason=reason,
            detail="probe failed",
        )

    def test_an_undeclared_target_without_the_binary_names_both_routes(self) -> None:
        for reason in ("binary_unconfigured", "binary_not_found"):
            section = evaluate_herdr_section(
                self._error_view(declared=False, reason=reason)
            )

            self.assertEqual("error", section["status"])
            actions = "\n".join(section["next_action"])
            # Route forward: install herdr, with the actual install route.
            self.assertIn("brew install herdr", actions)
            self.assertIn("herdr.dev", actions)
            # Route out: stay on tmux by declaring it.
            self.assertIn("does not declare `terminal_transport.backend`", actions)
            self.assertIn("backend: tmux", actions)

    def test_a_declared_herdr_target_gets_no_stay_on_tmux_guidance(self) -> None:
        # The operator who WROTE `backend: herdr` is not the upgrading tmux
        # operator; telling them to declare tmux would be noise.
        section = evaluate_herdr_section(
            self._error_view(declared=True, reason="binary_not_found")
        )

        self.assertEqual("error", section["status"])
        actions = "\n".join(section["next_action"])
        self.assertNotIn("does not declare", actions)

    def test_a_non_binary_failure_gets_no_stay_on_tmux_guidance(self) -> None:
        # A herdr server that is down is not an "install or declare" decision.
        section = evaluate_herdr_section(
            self._error_view(declared=False, reason="transport_error")
        )

        self.assertEqual("error", section["status"])
        actions = "\n".join(section["next_action"])
        self.assertNotIn("does not declare", actions)


class InventoryDeclarednessTest(unittest.TestCase):
    """read_herdr_inventory carries declared-ness from the config it read."""

    def test_an_undeclared_repo_reads_as_selected_but_undeclared(self) -> None:
        import tempfile
        from unittest.mock import patch

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_observability as obs,
        )

        class _Lister:
            def list_agent_rows(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".mozyo-bridge").mkdir()
            with patch.object(obs, "_workspace_segment", return_value="seg"):
                view = obs.read_herdr_inventory(repo, lister=_Lister())

        self.assertTrue(view.backend_selected)
        self.assertFalse(view.backend_declared)
        self.assertTrue(view.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
