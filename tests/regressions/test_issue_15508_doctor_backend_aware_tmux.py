"""Redmine #15508 — doctor judges tmux against the backend the target selects.

`doctor_health` collects the `herdr` section conditionally (absent unless the
target selects herdr, to keep tmux output byte-invariant) but collected `tmux`
unconditionally. So a herdr-only host reported `tmux: missing` — an unhealthy
status — and `doctor --json` answered `ok: false` with every other section
green. Observed on the macOS operator workstation during the 1.0.0 production
install QA (#15255 j#105440).

The fix judges the SELECTED backend. The rejected alternative was "healthy if
tmux OR herdr is installed": that reads green for a target with no declared
backend (which resolves to the tmux default) on a herdr-only host, where the
`herdr` section is absent by design and nothing else would catch it — a host
that cannot deliver a single tmux handoff would carry a clean bill of health.
The OR intuition survives only as guidance text, never as the verdict.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.doctor_health import (  # noqa: E402
    evaluate_doctor_health,
)
from mozyo_bridge.application.doctor_tmux import evaluate_tmux_section  # noqa: E402


def _view(*, backend: str, herdr_available: bool) -> dict[str, object]:
    """A read-view for a host with NO tmux installed."""
    return {
        "tmux_pane": "",
        "tmux_installed": False,
        "selected_backend": backend,
        "herdr_available": herdr_available,
    }


class BackendAwareTmuxVerdictTest(unittest.TestCase):
    def _is_unhealthy(self, section: dict[str, object]) -> bool:
        verdict = evaluate_doctor_health({"tmux": section})
        return not verdict.ok

    def test_herdr_target_without_tmux_is_healthy(self) -> None:
        # The case that made every herdr-only host read `needs attention`.
        section = evaluate_tmux_section(_view(backend="herdr", herdr_available=True))
        self.assertEqual("skipped", section["status"])
        self.assertIn("tmux is optional here", section["detail"])
        self.assertEqual([], section["next_action"])
        self.assertFalse(self._is_unhealthy(section))

    def test_tmux_target_without_tmux_stays_unhealthy_even_with_herdr(self) -> None:
        # The rejected OR rule would pass this host. It cannot run the backend
        # this target selects, and no `herdr` section exists to catch it.
        section = evaluate_tmux_section(_view(backend="tmux", herdr_available=True))
        self.assertEqual("missing", section["status"])
        self.assertTrue(self._is_unhealthy(section))
        # The OR intuition lives here: guidance, not verdict.
        joined = " ".join(section["next_action"])
        self.assertIn("terminal_transport.backend: herdr", joined)

    def test_tmux_target_without_tmux_or_herdr_is_unchanged(self) -> None:
        section = evaluate_tmux_section(_view(backend="tmux", herdr_available=False))
        self.assertEqual("missing", section["status"])
        self.assertEqual(
            ["install tmux to use mozyo-bridge pane notifications"],
            section["next_action"],
        )
        self.assertTrue(self._is_unhealthy(section))

    def test_a_view_without_the_new_keys_keeps_legacy_behaviour(self) -> None:
        # Callers that predate the backend-aware view (older section fixtures)
        # must keep the historical tmux verdict rather than crash or silently
        # turn green.
        section = evaluate_tmux_section({"tmux_pane": "", "tmux_installed": False})
        self.assertEqual("missing", section["status"])
        self.assertEqual(
            ["install tmux to use mozyo-bridge pane notifications"],
            section["next_action"],
        )
        self.assertTrue(self._is_unhealthy(section))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
