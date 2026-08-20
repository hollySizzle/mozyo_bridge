"""`workflow glance` fresh-home integration tests (Redmine #15747).

The read-only glance ran ``_reconcile_index`` -> ``ReconcileStateStore._connect`` ->
``connect_state_container_rw``, so a glance in a fresh home minted ``state.sqlite``
(and the home directories) as a side effect of a pure read — the #15711 j#108206
measured least-surprise violation. These tests wire the REAL CLI against a pinned,
never-created temp home and assert the read stays a read:

- ``workflow glance`` exits 0 and creates NOTHING under the pinned home — not the
  home directory, not ``state.sqlite``, not any other store file;
- the same run against a home where the reconcile write side already recorded a row
  still projects that row (the non-creating read is not an empty stub).

Hermetic collaborators, matching ``test_workflow_glance_cli``: ``--issue`` skips the
live sublane roster enumeration, ``--no-redmine`` skips the live Redmine adapter, and
the repo-scoped residue read is pinned empty (it enumerates live sublane views, which
is machine-dependent — #14813 j#96306).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.reconcile_state import ReconcileStateKey, ReconcileStateStore
from mozyo_bridge.core.state.state_store import state_store_path
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow_glance as cli_glance,
)
from tests.support.process_home_pin import pin_process_home


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


def _pin_residue_read_empty(test_case) -> None:
    original = cli_glance.enumerate_detached_residue_for_repo
    cli_glance.enumerate_detached_residue_for_repo = lambda _root: ((), None)
    test_case.addCleanup(
        setattr, cli_glance, "enumerate_detached_residue_for_repo", original
    )


class FreshHomeGlanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The pinned home deliberately does NOT exist: the defect first minted the
        # directories, then the store file. A pure read must create neither.
        self.home = Path(self._tmp.name) / "fresh-home"
        pin_process_home(self, self.home)
        _pin_residue_read_empty(self)

    def test_glance_creates_zero_files_under_a_fresh_home(self):
        rc, out = _run(["workflow", "glance", "--issue", "15747", "--no-redmine"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())
        self.assertFalse(
            self.home.exists(),
            f"read-only glance created files under the fresh home: "
            f"{sorted(p.relative_to(self.home) for p in self.home.rglob('*'))}"
            if self.home.exists()
            else "",
        )

    def test_glance_still_projects_a_writer_recorded_reconcile_row(self):
        # The write side (reconcile supervisor's open_cycle) legitimately creates the
        # store; the non-creating glance read must then see the recorded row — the
        # #15747 fix removes creation, not the projection.
        store = ReconcileStateStore(path=state_store_path(self.home))
        outcome = store.open_cycle(
            ReconcileStateKey(
                workspace_id="ws-15747",
                lane_id="issue_15747_lane",
                dispatch_anchor="j#108206",
            ),
            issue_id="15747",
            expected_gate="review_request",
        )
        self.assertTrue(outcome.applied)
        rc, out = _run(
            ["workflow", "glance", "--issue", "15747", "--no-redmine", "--json"]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        rows = {row["issue_id"]: row for row in payload["rows"]}
        self.assertIn("15747", rows)
        self.assertEqual(rows["15747"]["reconcile"]["expected_gate"], "review_request")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
