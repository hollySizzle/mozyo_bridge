"""Redmine #13892 — production session-retire composition reaches its read-only fence."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E402,E501
    REASON_RETIRE_EVIDENCE_ABSENT,
    run_session_retire,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E402,E501
    LiveSessionRetireOps,
)


class LiveSessionRetireCompositionTest(unittest.TestCase):
    def test_read_only_preflight_calls_real_live_ops_peek_method(self) -> None:
        """The live class, not a protocol fake, must compose the retirement fence."""
        repo = ROOT
        ops = LiveSessionRetireOps(repo_root=repo, env={})
        args = argparse.Namespace(lane="issue_13892_live_composition", execute=False)

        with (
            mock.patch.object(ops, "agent_rows", return_value=[]),
            mock.patch.object(ops, "lifecycle_record_absent", return_value=True),
            mock.patch.object(ops, "open_obligations", return_value=()),
            mock.patch(
                "mozyo_bridge.core.state.scratch_retirement_fence."
                "ScratchRetirementFence.peek",
                autospec=True,
                return_value=None,
            ) as peek,
        ):
            result = run_session_retire(args, repo, ops=ops)

        self.assertEqual(result.reason, REASON_RETIRE_EVIDENCE_ABSENT)
        peek.assert_called_once()

    def test_live_class_implements_every_session_retire_side_effect_port(self) -> None:
        for name in (
            "retirement_transaction",
            "peek_retirement",
            "close",
            "record_retirement",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(LiveSessionRetireOps, name, None)))


if __name__ == "__main__":
    unittest.main()
