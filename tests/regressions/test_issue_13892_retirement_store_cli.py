"""Redmine #13892 R3-F5 — the retirement authority's status must reach an operator.

The method existed before this; nothing production-side called it, so `fence.status()` was a
Python API and not visibility. The authority fails closed on damage and this issue ships no
generic recover/reset by design, so an operator who cannot see WHY a retire refuses has no
move at all.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    slot_digest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_retirement_store import (  # noqa: E501
    cmd_herdr_retirement_store_status,
)


class RetirementStoreCliTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        self.unit = RetirementUnit("ws1", "lane1", slot_digest(["mzb1_a", "mzb1_b"]))
        patcher = mock.patch(
            "mozyo_bridge.core.state.scratch_retirement_fence.mozyo_bridge_home",
            return_value=self.home,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, json_out=False):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cmd_herdr_retirement_store_status(
                argparse.Namespace(json=json_out, repo=None)
            )
        return code, buf.getvalue()

    def test_absent_store_reports_absent_and_creates_nothing(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("absent", out)
        f = ScratchRetirementFence(home=self.home)
        self.assertFalse(f.path.exists(), "status must create nothing")
        self.assertFalse(f.seal_path.exists())

    def test_pending_attempt_is_visible(self):
        f = ScratchRetirementFence(home=self.home)
        with f.transaction(self.unit, live_pair_present=True) as txn:
            txn.reserve(pinned=(("codex", "%1"),))
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("present", out)
        self.assertIn("pending", out)

    def test_damaged_store_is_visible_and_exits_nonzero(self):
        f = ScratchRetirementFence(home=self.home)
        with f.transaction(self.unit, live_pair_present=True):
            pass
        f.seal_path.unlink()  # identity seal lost
        code, out = self._run()
        self.assertEqual(code, 1, "a damaged authority must not report success")
        self.assertIn("damaged", out)
        self.assertIn("fails closed", out, "the operator is told why retire refuses")

    def test_json_surface(self):
        code, out = self._run(json_out=True)
        self.assertEqual(code, 0)
        self.assertIn('"store_state": "absent"', out)


if __name__ == "__main__":
    unittest.main()
