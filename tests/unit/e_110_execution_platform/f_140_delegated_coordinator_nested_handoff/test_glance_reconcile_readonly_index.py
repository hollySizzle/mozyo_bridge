"""Glance reconcile-index read-path decision tests (Redmine #15747).

``_reconcile_index`` is the glance-family seam #15711 j#108206 measured creating
``state.sqlite`` on a pure read (it called ``ReconcileStateStore.records()``, whose
connect runs the read-write schema ensure). #15747 moved it onto the store's
NON-CREATING ``records_readonly``. These tests pin the pure decision surface of the
index over that read:

- ``None`` store (no adapter) and a ``None`` read result (unsupported / unreadable
  component, fail closed) both project the empty index — never a fabricated join;
- a raising read degrades to the empty index (the store is a ``rebuildable_cache``);
- the typed empty read ``()`` (absent store) projects the empty index;
- recognized rows index by issue with the existing relevance rule (a non-terminal
  record wins over a terminal one) — the #15747 change swaps the read, not the fold;
- the read goes through ``records_readonly`` and never falls back to the creating
  ``records()``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
    _reconcile_index,
)


class _FakeRecord:
    def __init__(self, issue_id, phase="reconcile_wait", updated_at="2026-08-20T00:00:00+00:00"):
        self.issue_id = issue_id
        self.phase = phase
        self.updated_at = updated_at
        self.reconcile_failure_count = 1
        self.expected_next_owner = "implementation_worker"
        self.deadline = ""
        self.last_disposition = ""
        self.escalated = False


class _FakeStore:
    """A reconcile store double exposing ONLY the non-creating read."""

    def __init__(self, result):
        self._result = result
        self.readonly_calls = 0

    def records_readonly(self):
        self.readonly_calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def records(self):  # pragma: no cover - the read path must never take this
        raise AssertionError(
            "#15747: the glance read path must use records_readonly, not the "
            "creating records()"
        )


class ReconcileIndexReadPathTest(unittest.TestCase):
    def test_no_store_projects_the_empty_index(self):
        self.assertEqual(_reconcile_index(None), {})

    def test_typed_empty_read_projects_the_empty_index(self):
        store = _FakeStore(())
        self.assertEqual(_reconcile_index(store), {})
        self.assertEqual(store.readonly_calls, 1)

    def test_none_read_fails_closed_to_the_empty_index(self):
        # records_readonly() -> None is the unsupported/unreadable-component verdict.
        self.assertEqual(_reconcile_index(_FakeStore(None)), {})

    def test_raising_read_degrades_to_the_empty_index(self):
        self.assertEqual(_reconcile_index(_FakeStore(RuntimeError("boom"))), {})

    def test_recognized_rows_index_by_issue_with_the_relevance_rule(self):
        terminal = _FakeRecord("15747", phase="notified", updated_at="2026-08-20T09:00:00+00:00")
        active = _FakeRecord("15747", phase="reconcile_wait", updated_at="2026-08-20T01:00:00+00:00")
        index = _reconcile_index(_FakeStore((terminal, active)))
        self.assertEqual(set(index), {"15747"})
        # The non-terminal (live self-heal) record wins over the newer terminal one.
        self.assertEqual(index["15747"].reconcile_attempt, active.reconcile_failure_count)

    def test_legacy_store_without_the_readonly_read_degrades(self):
        # A duck-typed store that predates #15747 (records() only) must degrade to the
        # empty index rather than silently reopening the creating read path.
        class _LegacyStore:
            def records(self):
                raise AssertionError("must not be called")

        self.assertEqual(_reconcile_index(_LegacyStore()), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
