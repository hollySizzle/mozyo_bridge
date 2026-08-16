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

The wiring cases below drive the LIVE collector against a real repo config, not
a hand-built view: the first version of this regression exercised the policy
alone, so a selector that always answered "tmux" would have kept it green
(review j#105776 finding_1).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import doctor  # noqa: E402
from mozyo_bridge.application.doctor_health import (  # noqa: E402
    evaluate_doctor_health,
)
from mozyo_bridge.application.doctor_tmux import (  # noqa: E402
    LiveTmuxPaneHealthReads,
    evaluate_tmux_section,
)


def _view(*, backend: str, herdr_available: bool) -> dict[str, object]:
    """A read-view for a host with NO tmux installed."""
    return {
        "tmux_pane": "",
        "tmux_installed": False,
        "selected_backend": backend,
        "herdr_available": herdr_available,
    }


class BackendAwareTmuxVerdictTest(unittest.TestCase):
    """Policy: what each (backend, tmux, herdr) combination must decide."""

    def _is_unhealthy(self, section: dict[str, object]) -> bool:
        return not evaluate_doctor_health({"tmux": section}).ok

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


class BackendSelectorWiringTest(unittest.TestCase):
    """Wiring: a real repo config must reach the verdict through the collector.

    These drive :class:`LiveTmuxPaneHealthReads` — the same adapter doctor uses —
    so a selector that stopped reading the config (always answering "tmux", say)
    fails here instead of passing a policy-only suite.
    """

    def _repo(self, config_text: str | None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        if config_text is not None:
            (repo / ".mozyo-bridge").mkdir(parents=True)
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                config_text, encoding="utf-8"
            )
        return repo

    def _describe(self, repo: Path, *, tmux_installed: bool) -> dict[str, object]:
        with mock.patch(
            "mozyo_bridge.application.doctor.subprocess.run",
            return_value=types.SimpleNamespace(returncode=0 if tmux_installed else 1),
        ), mock.patch.object(
            doctor,
            "run_tmux",
            mock.Mock(return_value=types.SimpleNamespace(returncode=1, stdout="")),
        ), mock.patch.object(
            doctor, "pane_lines", mock.Mock()
        ), mock.patch.dict(
            "os.environ", {"TMUX_PANE": ""}, clear=False
        ):
            return LiveTmuxPaneHealthReads(
                argparse.Namespace(repo=str(repo))
            ).describe()

    HERDR_CONFIG = "version: 2\nterminal_transport:\n  backend: herdr\n"
    TMUX_CONFIG = "version: 2\nterminal_transport:\n  backend: tmux\n"

    def test_declared_herdr_config_reaches_a_healthy_verdict_without_tmux(self) -> None:
        repo = self._repo(self.HERDR_CONFIG)
        view = self._describe(repo, tmux_installed=False)
        self.assertEqual("herdr", view["selected_backend"])

        section = evaluate_tmux_section(view)
        self.assertEqual("skipped", section["status"])
        # The whole point: doctor's overall verdict is ok on a herdr-only host.
        self.assertTrue(evaluate_doctor_health({"tmux": section}).ok)

    def test_a_target_with_no_config_resolves_to_the_herdr_default(self) -> None:
        # Since 2.0 (Redmine #15531) an undeclared target resolves to herdr;
        # the tmux section is then "just a fact about the host" (skipped) and
        # the fail-closed verdict for the missing herdr binary belongs to the
        # herdr section, not this one.
        repo = self._repo(None)
        view = self._describe(repo, tmux_installed=False)
        self.assertEqual("herdr", view["selected_backend"])

        section = evaluate_tmux_section(view)
        self.assertEqual("skipped", section["status"])

    def test_a_declared_tmux_target_without_tmux_stays_unhealthy(self) -> None:
        # Pins the selector against a hardcode: a target that explicitly
        # declares tmux (the 2.0 opt-out, #15531) must be judged as tmux — a
        # selector that stopped reading the config (always answering "herdr"
        # after the default flip) would turn this host green and let the OR
        # rule's fail-open back in.
        repo = self._repo(self.TMUX_CONFIG)
        view = self._describe(repo, tmux_installed=False)
        self.assertEqual("tmux", view["selected_backend"])

        section = evaluate_tmux_section(view)
        self.assertEqual("missing", section["status"])
        self.assertFalse(evaluate_doctor_health({"tmux": section}).ok)

    def test_tmux_present_output_is_identical_under_either_backend(self) -> None:
        # The acceptance condition this change had to preserve: with tmux
        # available, a herdr-selected target's tmux section must be byte-identical
        # to a tmux-selected one — the backend branch exists only for absence.
        herdr_view = self._describe(self._repo(self.HERDR_CONFIG), tmux_installed=True)
        tmux_view = self._describe(self._repo(self.TMUX_CONFIG), tmux_installed=True)
        self.assertEqual("herdr", herdr_view["selected_backend"])
        self.assertEqual("tmux", tmux_view["selected_backend"])

        self.assertEqual(
            evaluate_tmux_section(tmux_view), evaluate_tmux_section(herdr_view)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
