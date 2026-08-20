"""Regression pin: the glance read path must not create the state store (Redmine #15747).

Fixed defect (measured during Redmine #15711, its journal j#108206; cause commit
`4026d7bf`, which wired the #13758 reconcile projection into the glance): the
``workflow glance`` READ path created the consolidated state store read-write when it
did not exist —

    glance's active-lane snapshot source -> ``_reconcile_index``
        -> ``ReconcileStateStore._connect`` -> ``connect_state_container_rw``

so a read-only glance in a fresh home minted ``state.sqlite`` (and its parent
directories). #15747 moved the glance-family read onto the store's NON-CREATING
``records_readonly`` (absent store -> typed empty result, filesystem untouched).

This file pins the recurrence at each layer of the measured call path, plus the
acceptance-2 writer parity: the reconcile WRITE side (``ensure_schema`` /
``open_cycle``) must keep creating the store exactly as before — the fix removes
creation from reads only.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.reconcile_state import ReconcileStateKey, ReconcileStateStore
from mozyo_bridge.core.state.state_store import state_store_path
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow_glance as cli_glance,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
    _reconcile_index,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_source_wiring import (  # noqa: E501
    build_reconcile_store,
)
from tests.support.process_home_pin import pin_process_home


class FreshHome(unittest.TestCase):
    """A pinned home whose directory tree does not exist — the defect's trigger shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "fresh-home"
        pin_process_home(self, self.home)

    def _created(self) -> list:
        return (
            sorted(p.relative_to(self.home) for p in self.home.rglob("*"))
            if self.home.exists()
            else []
        )


class GlanceReadSeamTest(FreshHome):
    def test_store_readonly_read_mints_nothing(self):
        # The innermost seam: the store read itself (was _connect -> container rw).
        store = ReconcileStateStore(path=state_store_path(self.home))
        self.assertEqual(store.records_readonly(), ())
        self.assertFalse(self.home.exists(), f"store read created: {self._created()}")

    def test_reconcile_index_over_the_wired_store_mints_nothing(self):
        # The measured call path's middle: the glance snapshot source's reconcile index
        # over the exact adapter the glance wiring builds (home-resolved store).
        self.assertEqual(_reconcile_index(build_reconcile_store()), {})
        self.assertFalse(
            self.home.exists(), f"reconcile index created: {self._created()}"
        )

    def test_cli_glance_creates_zero_store_files(self):
        # The operator-visible symptom: a read-only `workflow glance` in a fresh home.
        # Hermetic collaborators as in test_workflow_glance_cli (#14813 j#96306): the
        # residue read enumerates live sublane views and is machine-dependent.
        original = cli_glance.enumerate_detached_residue_for_repo
        cli_glance.enumerate_detached_residue_for_repo = lambda _root: ((), None)
        self.addCleanup(
            setattr, cli_glance, "enumerate_detached_residue_for_repo", original
        )
        from mozyo_bridge.application.cli import build_parser

        ns = build_parser().parse_args(
            ["workflow", "glance", "--issue", "15747", "--no-redmine"]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ns.func(ns)
        self.assertEqual(rc, 0)
        self.assertFalse(self.home.exists(), f"glance created: {self._created()}")


class WriterParityTest(FreshHome):
    """Acceptance 2: the write side keeps creating the store (unchanged behavior)."""

    def test_ensure_schema_still_creates_the_store(self):
        store = ReconcileStateStore(path=state_store_path(self.home))
        store.ensure_schema()
        self.assertTrue(store.path.exists())

    def test_open_cycle_still_creates_the_store_and_records(self):
        store = ReconcileStateStore(path=state_store_path(self.home))
        outcome = store.open_cycle(
            ReconcileStateKey(
                workspace_id="ws-15747",
                lane_id="issue_15747_lane",
                dispatch_anchor="j#108206",
            ),
            issue_id="15747",
        )
        self.assertTrue(outcome.applied)
        self.assertTrue(store.path.exists())
        # And the non-creating read now sees exactly what the writer recorded.
        records = store.records_readonly()
        self.assertIsNotNone(records)
        self.assertEqual([r.issue_id for r in records], ["15747"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
