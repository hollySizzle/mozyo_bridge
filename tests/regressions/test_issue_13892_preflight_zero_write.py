"""Redmine #13892 — a read-only `session-retire` preflight writes NOTHING, anywhere.

The contract is "verdict only; closes nothing and writes nothing". Two rounds have now caught
me scoping this too narrowly:

- R4-F4: the preflight bootstrapped the retirement authority, and the test only watched
  close/audit calls, so it sailed through;
- R5-F4: after fixing that, the test asserted the *retirement fence's* artifacts only — while
  a newly added obligation read migrated the workflow-runtime store on the way past.

The lesson is that the test must be scoped to the CONTRACT (nothing is written) rather than to
the implementation I happened to fix. So this fingerprints EVERY authority the preflight can
touch — bytes, mtime and artifact set — and fails if any of them moves.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.dispatch_outbox_fence import DispatchOutboxFence, FenceKey
from mozyo_bridge.core.state.forward_outbox_fence import ForwardOutboxFence
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    run_session_retire,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

LANE, GW, WK = "dogfood13892", "codex", "claude"


def _fingerprint(home: Path) -> dict:
    """Every file under the home: name -> (size, mtime_ns, sha256). Nothing may move."""
    out = {}
    for p in sorted(home.rglob("*")):
        if not p.is_file():
            continue
        st = p.stat()
        out[str(p.relative_to(home))] = (
            st.st_size,
            st.st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
    return out


class PreflightZeroWriteTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        self.repo = Path(__file__).resolve().parents[2]
        self.ws = herdr_workspace_segment(self.repo)
        self.gw = encode_assigned_name(self.ws, GW, LANE)
        self.wk = encode_assigned_name(self.ws, WK, LANE)
        for mod in (
            "mozyo_bridge.core.state.dispatch_outbox_fence",
            "mozyo_bridge.core.state.forward_outbox_fence",
            "mozyo_bridge.core.state.scratch_retirement_fence",
            "mozyo_bridge.core.state.managed_events",
        ):
            patcher = mock.patch(f"{mod}.mozyo_bridge_home", return_value=self.home)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _seed_every_authority(self):
        """Create each store the preflight can read, so 'absent' does not hide a write."""
        d = DispatchOutboxFence(home=self.home)
        d.bootstrap()
        d.reserve(FenceKey(self.ws, LANE, "13999", "42", "act1", "mzb1_other_claude_x"))
        ForwardOutboxFence(home=self.home).bootstrap()
        # An OLDER workflow-runtime store: the shape whose read used to migrate it.
        wr = self.home / "workflow-runtime.sqlite"
        conn = sqlite3.connect(wr)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

    def _ops(self, rows):
        test = self

        class _R:
            def __init__(self, closed=(), failed=()):
                self.closed, self.failed = tuple(closed), tuple(failed)

        class Ops:
            def __init__(self):
                self.close_calls = []
                self.recorded = []

            def agent_rows(self):
                return list(rows)

            def runtime_state(self, loc):
                return "awaiting_input"

            def observe_composer(self, loc):
                return (True, False)

            def lifecycle_record_absent(self, ws, lane):
                return True

            def open_obligations(self, ws, names):
                from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
                    scratch_pair_obligations as spo,
                )

                try:
                    return spo.all_pair_obligations(
                        workspace_id=ws, lane_id=LANE, assigned_names=tuple(names),
                        roles=(GW, WK),
                    )
                except spo.ObligationStoreUnreadable:
                    return None

            def retirement_transaction(self, unit, *, live_pair_present):
                from mozyo_bridge.core.state.scratch_retirement_fence import (
                    ScratchRetirementFence,
                )

                return ScratchRetirementFence(home=test.home).transaction(
                    unit, live_pair_present=live_pair_present
                )

            def peek_retirement(self, unit):
                from mozyo_bridge.core.state.scratch_retirement_fence import (
                    ScratchRetirementFence,
                )

                return ScratchRetirementFence(home=test.home).peek(unit)

            def close(self, ws, lane, targets):
                self.close_calls.append(tuple(targets))
                return _R(closed=tuple(targets))

            def record_retirement(self, **kw):
                self.recorded.append(kw)
                return "recorded"

        return Ops()

    def _pair(self):
        return [
            {"name": self.gw, "pane": "%1", "agent": GW},
            {"name": self.wk, "pane": "%2", "agent": WK},
        ]

    def test_preflight_leaves_every_authority_byte_identical(self):
        self._seed_every_authority()
        before = _fingerprint(self.home)
        ops = self._ops(self._pair())
        run_session_retire(
            argparse.Namespace(lane=LANE, execute=False, json=False, repo=None),
            self.repo, ops=ops,
        )
        after = _fingerprint(self.home)
        self.assertEqual(
            before, after,
            "a --execute-less preflight must leave every authority byte-identical "
            "(the R4-F4 / R5-F4 class: a read that migrates or bootstraps is a write)",
        )
        self.assertEqual(ops.close_calls, [])
        self.assertEqual(ops.recorded, [])

    def test_preflight_creates_no_new_artifact_over_an_empty_home(self):
        before = _fingerprint(self.home)
        self.assertEqual(before, {}, "the fixture starts empty")
        ops = self._ops(self._pair())
        run_session_retire(
            argparse.Namespace(lane=LANE, execute=False, json=False, repo=None),
            self.repo, ops=ops,
        )
        self.assertEqual(
            _fingerprint(self.home), {},
            "no DB, seal, lock or temp may be created by a read-only preflight",
        )

    def test_an_older_workflow_runtime_store_is_not_migrated_by_a_preflight(self):
        """R5-F4 exactly: `CallbackOutbox.read()` migrates; the obligation gate must not."""
        self._seed_every_authority()
        wr = self.home / "workflow-runtime.sqlite"
        before = (wr.read_bytes(), wr.stat().st_mtime_ns)
        ops = self._ops(self._pair())
        run_session_retire(
            argparse.Namespace(lane=LANE, execute=False, json=False, repo=None),
            self.repo, ops=ops,
        )
        after = (wr.read_bytes(), wr.stat().st_mtime_ns)
        self.assertEqual(before, after, "asking a question must not migrate the store")
        conn = sqlite3.connect(wr)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 1,
                "the store's schema version must be untouched",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
