"""Redmine #13647 Tranche 1b — the generation-bound `lane_kind` lifecycle authority.

Design answer j#85645 / disposition j#85650 P1: the lane-role (親 / 子 / 孫) pane geometry a
lane was CREATED with is stored on the lane's lifecycle authority record, generation-bound,
so a heal resolves the same placement OFFLINE — never by re-reading Redmine at launch time,
and never from ``lane_metadata`` / a display projection (which declare themselves "never
routing authority"; the j#85644 → j#85645 correction).

What these pin, in the order the value moves:

1. **schema v7 / migration** — a genuine v6 store migrates additively, backup-first, to v7
   with an EMPTY ``lane_kind`` (no durable fact, never a guessed kind), and a v6 store is
   still *read* compatibly without being migrated;
2. **write surfaces** — ``declare_active`` / ``declare_lane`` store the creating caller's
   kind, fail closed on an off-vocabulary token, and treat the kind as part of the
   declaration identity (a divergent re-declare never overwrites it);
3. **generation binding** — no ordinary transition mutates the stored kind; a re-incarnation
   carries it forward by default and re-binds ONLY when the caller says so explicitly;
4. **create threading** — ``sublane create --lane-kind`` reaches the declaration, and the
   CLI vocabulary is exactly the canonical three tokens (no alias, P3).

The launch-side half of Tranche 1b (offline heal read + fail-closed reconciliation at
``prepare_session``) is pinned in
``tests/unit/.../test_herdr_session_start.py::LaneKindHealAuthorityLaunchTest``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_kind import (
    LANE_KIND_COORDINATOR,
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
    LaneKindError,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_SCHEMA_VERSION,
    lane_lifecycle_path,
)

WS = "ws13647"
LANE = "issue_13647_lane"
ISSUE = "13647"


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="85826")


class LaneKindStorageTest(unittest.TestCase):
    """`declare_active` / `declare_lane` store the creating caller's geometry kind."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, LANE)

    def test_declare_active_stores_and_reads_back_the_kind(self) -> None:
        store = LaneLifecycleStore(home=self.home)
        self.assertTrue(
            store.declare_active(
                self.key,
                decision=_decision(),
                issue_id=ISSUE,
                lane_kind=LANE_KIND_IMPLEMENTATION,
            ).applied
        )
        self.assertEqual(store.get(self.key).lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_declare_active_without_a_kind_is_byte_invariant(self) -> None:
        # Every pre-#13647 caller: no kind fact -> empty, never a guessed default.
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(self.key, decision=_decision(), issue_id=ISSUE)
        self.assertEqual(store.get(self.key).lane_kind, "")

    def test_off_vocabulary_kind_fails_closed_zero_write(self) -> None:
        store = LaneLifecycleStore(home=self.home)
        for bad in ("parent", "child", "grandchild", "coordinator_assistant", "COORDINATOR"):
            with self.assertRaises(LaneKindError):
                store.declare_active(
                    self.key, decision=_decision(), issue_id=ISSUE, lane_kind=bad
                )
        # The refusal wrote nothing: the lane is still undeclared, so a later correct
        # declaration is not blocked by a half-written row.
        self.assertIsNone(store.get(self.key))
        self.assertTrue(
            store.declare_active(
                self.key,
                decision=_decision(),
                issue_id=ISSUE,
                lane_kind=LANE_KIND_COORDINATOR,
            ).applied
        )

    def test_declare_lane_stores_the_kind_and_redeclare_is_idempotent(self) -> None:
        store = LaneDeclarationStore(home=self.home)
        first = store.declare_lane(
            self.key,
            decision=_decision(),
            issue_id=ISSUE,
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        self.assertEqual(first.reason, CAS_APPLIED)
        again = store.declare_lane(
            self.key,
            decision=_decision(),
            issue_id=ISSUE,
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        self.assertTrue(again.applied)  # exact duplicate -> idempotent success
        self.assertEqual(
            LaneLifecycleStore(home=self.home).get(self.key).lane_kind,
            LANE_KIND_DELEGATED_COORDINATOR,
        )

    def test_redeclare_with_a_different_kind_is_divergent_and_zero_write(self) -> None:
        # The stored geometry authority is not overwritten by a later caller's guess:
        # a re-declare carrying a DIFFERENT kind is a divergent re-declare, refused.
        store = LaneDeclarationStore(home=self.home)
        store.declare_lane(
            self.key,
            decision=_decision(),
            issue_id=ISSUE,
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        outcome = store.declare_lane(
            self.key,
            decision=_decision(),
            issue_id=ISSUE,
            lane_kind=LANE_KIND_IMPLEMENTATION,
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(
            LaneLifecycleStore(home=self.home).get(self.key).lane_kind,
            LANE_KIND_DELEGATED_COORDINATOR,
        )


class LaneKindGenerationBindingTest(unittest.TestCase):
    """The stored kind is immutable WITHIN a generation; only a reopen may re-bind it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, LANE)
        self.store = LaneLifecycleStore(home=self.home)
        self.store.declare_active(
            self.key,
            decision=_decision(),
            issue_id=ISSUE,
            lane_kind=LANE_KIND_IMPLEMENTATION,
        )

    def _retire(self) -> None:
        for target in (DISPOSITION_HIBERNATED, DISPOSITION_RETIRED):
            record = self.store.get(self.key)
            outcome = self.store.transition_disposition(
                self.key,
                expected_disposition=record.lane_disposition,
                expected_revision=record.revision,
                target=target,
                decision=_decision(),
            )
            self.assertTrue(outcome.applied, outcome.reason)

    def test_disposition_transitions_never_mutate_the_kind(self) -> None:
        self._retire()
        self.assertEqual(self.store.get(self.key).lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_reopen_carries_the_kind_forward_by_default(self) -> None:
        # A re-incarnation is the SAME lane: its geometry is preserved exactly as its
        # binding is, so a heal of generation 2 places like generation 1.
        self._retire()
        record = self.store.get(self.key)
        outcome = LaneDeclarationStore(home=self.home).open_next_generation(
            self.key,
            expected_revision=record.revision,
            expected_generation=record.lane_generation,
            decision=_decision(),
        )
        self.assertTrue(outcome.applied, outcome.reason)
        reopened = self.store.get(self.key)
        self.assertEqual(reopened.lane_generation, 2)
        self.assertEqual(reopened.lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_reopen_rebinds_only_when_explicitly_asked(self) -> None:
        self._retire()
        record = self.store.get(self.key)
        outcome = LaneDeclarationStore(home=self.home).open_next_generation(
            self.key,
            expected_revision=record.revision,
            expected_generation=record.lane_generation,
            decision=_decision(),
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        self.assertTrue(outcome.applied, outcome.reason)
        self.assertEqual(
            self.store.get(self.key).lane_kind, LANE_KIND_DELEGATED_COORDINATOR
        )

    def test_reopen_can_clear_the_kind_explicitly(self) -> None:
        # An explicit empty token is "this lane no longer has a durable kind fact" —
        # distinct from `None` (carry forward), so a governance retraction is expressible.
        self._retire()
        record = self.store.get(self.key)
        LaneDeclarationStore(home=self.home).open_next_generation(
            self.key,
            expected_revision=record.revision,
            expected_generation=record.lane_generation,
            decision=_decision(),
            lane_kind="",
        )
        self.assertEqual(self.store.get(self.key).lane_kind, "")

    def test_reopen_rejects_an_off_vocabulary_rebind_zero_write(self) -> None:
        self._retire()
        record = self.store.get(self.key)
        with self.assertRaises(LaneKindError):
            LaneDeclarationStore(home=self.home).open_next_generation(
                self.key,
                expected_revision=record.revision,
                expected_generation=record.lane_generation,
                decision=_decision(),
                lane_kind="grandchild",
            )
        unchanged = self.store.get(self.key)
        self.assertEqual(unchanged.lane_kind, LANE_KIND_IMPLEMENTATION)
        self.assertEqual(unchanged.lane_generation, 1)  # the reopen did not happen
        self.assertEqual(unchanged.lane_disposition, DISPOSITION_RETIRED)


class LaneKindSchemaMigrationTest(unittest.TestCase):
    """v6 -> v7: additive, backup-first, empty kind — and v6 stays READABLE unmigrated."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, LANE)

    def _columns(self) -> set:
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            return {r[1] for r in conn.execute("PRAGMA table_info(lane_lifecycle_records)")}
        finally:
            conn.close()

    def _recorded(self) -> object:
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            row = conn.execute(
                "SELECT schema_version FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

    def _rewind_to_v6(self) -> Path:
        """A healthy current store rewound to a GENUINE v6 signature (a real pre-#13647 store).

        Everything v6 had is kept — only ``lane_kind`` is removed and the recorded version
        is stamped back to 6 — so this is the exact ``_SHAPE_V6`` branch a v7 build must
        read compatibly and migrate additively, not a newer shape merely re-stamped.
        """
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            self.key,
            decision=_decision(),
            issue_id=ISSUE,
            worktree_identity="wt_13647",
        )
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN lane_kind")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 6 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_v7_reader_reads_a_v6_store_without_migrating_it(self) -> None:
        path = self._rewind_to_v6()
        before = path.read_bytes()
        record = LaneLifecycleStore(home=self.home).get(self.key)
        self.assertEqual(record.lane_kind, "")  # padded additive default, not guessed
        self.assertEqual(record.worktree_identity, "wt_13647")  # v6 fields intact
        # The read touched no byte: version stays 6, the column is still absent.
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self._recorded(), 6)
        self.assertNotIn("lane_kind", self._columns())

    def test_v6_migrates_additively_backup_first_with_an_empty_kind(self) -> None:
        path = self._rewind_to_v6()
        before = path.read_bytes()

        LaneLifecycleStore(home=self.home).ensure_schema()

        self.assertEqual(self._recorded(), LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertEqual(LANE_LIFECYCLE_SCHEMA_VERSION, 7)
        # backup-first: the pre-migration snapshot was preserved before the first write
        backups = sorted((self.home / "backups").glob("state-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "state.sqlite").read_bytes(), before)
        # the v7 column landed additively with the neutral default on the existing row
        self.assertIn("lane_kind", self._columns())
        record = LaneLifecycleStore(home=self.home).get(self.key)
        self.assertEqual(record.lane_kind, "")  # a legacy lane has NO kind, not a guess
        # every v6 field on the existing row survived untouched
        self.assertEqual(record.worktree_identity, "wt_13647")
        self.assertEqual(record.issue_id, ISSUE)
        self.assertEqual(record.lane_generation, 1)

    def test_a_migrated_legacy_lane_can_then_record_a_kind_at_its_next_generation(
        self,
    ) -> None:
        # The migration path's forward exit: a legacy lane acquires its geometry fact at
        # the sanctioned re-bind point rather than by an in-place authority overwrite.
        self._rewind_to_v6()
        store = LaneLifecycleStore(home=self.home)
        store.ensure_schema()
        for target in (DISPOSITION_HIBERNATED, DISPOSITION_RETIRED):
            record = store.get(self.key)
            store.transition_disposition(
                self.key,
                expected_disposition=record.lane_disposition,
                expected_revision=record.revision,
                target=target,
                decision=_decision(),
            )
        record = store.get(self.key)
        LaneDeclarationStore(home=self.home).open_next_generation(
            self.key,
            expected_revision=record.revision,
            expected_generation=record.lane_generation,
            decision=_decision(),
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
        )
        self.assertEqual(
            store.get(self.key).lane_kind, LANE_KIND_DELEGATED_COORDINATOR
        )


class LaneKindCreateThreadingTest(unittest.TestCase):
    """`sublane create --lane-kind` reaches the durable declaration (create threading)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _declare(self, **kwargs) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E501
            declare_created_lane_lifecycle,
        )

        from unittest.mock import patch
        import os

        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            declare_created_lane_lifecycle(
                repo_workspace_id=WS,
                lane_label=LANE,
                issue=ISSUE,
                journal="85826",
                worktree_identity="wt_13647",
                **kwargs,
            )

    def test_create_declaration_records_the_callers_kind(self) -> None:
        self._declare(lane_kind=LANE_KIND_IMPLEMENTATION)
        record = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertIsNotNone(record)
        self.assertEqual(record.lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_create_declaration_without_a_kind_is_unchanged(self) -> None:
        self._declare()
        record = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(record.lane_kind, "")

    def test_an_off_vocabulary_kind_leaves_the_lane_owner_unbound(self) -> None:
        # Best-effort contract: the store refuses, the actuation does not break, and the
        # lane honestly reads as owner-unbound rather than carrying a bogus authority.
        self._declare(lane_kind="grandchild")
        self.assertIsNone(
            LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        )

    def test_cli_vocabulary_is_the_canonical_three_tokens(self) -> None:
        # No parent / child / grandchild alias on the operator surface either (P3).
        import argparse

        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "sublane",
                "create",
                "--issue",
                ISSUE,
                "--lane-label",
                LANE,
                "--lane-kind",
                LANE_KIND_IMPLEMENTATION,
            ]
        )
        self.assertEqual(args.lane_kind, LANE_KIND_IMPLEMENTATION)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "sublane",
                    "create",
                    "--issue",
                    ISSUE,
                    "--lane-label",
                    LANE,
                    "--lane-kind",
                    "grandchild",
                ]
            )
        self.assertIsInstance(parser, argparse.ArgumentParser)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
