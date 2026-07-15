"""Redmine #13810 — lane lifecycle binding / generation / typed process pins.

The v5 additive tranche of the #13780 Design Verdict (j#78386 / owner decisions j#78405):
the existing ``lane_lifecycle`` CAS row is extended, NOT forked into a separate
project-gateway owner component. Pins:

- the pure model: binding-kind vocabulary, the typed :class:`ProcessGenerationPin` and its
  versioned ``declared_slots`` codec, the ``lane_generation`` field, and the
  ``is_legacy_unbound`` classifier;
- the schema: v5 additive migration (backup-first), the second project-gateway owner
  index verified by exact constraint, and downgrade fail-closed;
- the store: the common ``declare_lane`` declaration / backfill service (issue AND
  project-gateway, idempotent exact duplicate, fail-closed owner conflict), and the
  explicit ``open_next_generation`` re-incarnation CAS.

All state lives under an isolated home — never the shared ``$HOME/.mozyo_bridge``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    BINDING_KIND_ISSUE,
    BINDING_KIND_PROJECT_GATEWAY,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_OWNER_CONFLICT,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DecisionPointer,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ReleasePin,
    lane_lifecycle_path,
)
from mozyo_bridge.core.state.lane_declaration import (  # noqa: E402
    LaneDeclarationStore,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DECLARED_SLOTS_VERSION,
    ProcessPinError,
    decode_declared_slots,
    decode_release_pins,
    encode_declared_slots,
    validate_declared_slots,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (  # noqa: E402
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_SCHEMA_VERSION,
)

WS = "wsMain"
ISSUE = "13810"
SCOPE = "giken-cloud-drive/project-x"


def _issue_decision(journal: str = "78860", issue: str = ISSUE) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _gw_decision(journal: str = "78386") -> DecisionPointer:
    # A project-gateway lane owns no issue, but its decision anchor is still a complete
    # Redmine pointer (issue-addressable journal) — the design's durable-record rule.
    return DecisionPointer(source="redmine", issue_id="13780", journal_id=journal)


def _pin(role: str, locator: str, runtime_revision: str = "1.0") -> ProcessGenerationPin:
    return ProcessGenerationPin(
        role=role,
        provider=role,
        assigned_name=f"mzb1_{WS}_{role}",
        locator=locator,
        runtime_revision=runtime_revision,
        attested_at="2026-07-15T00:00:00+00:00",
    )


def _slots() -> tuple[ProcessGenerationPin, ...]:
    return (_pin("codex", "%7"), _pin("claude", "%6"))


class ProcessGenerationPinModelTest(unittest.TestCase):
    """The typed pin and its versioned declared-slots codec (pure)."""

    def test_identity_and_evidence_fields_are_required(self) -> None:
        for missing in ("role", "provider", "assigned_name", "locator", "runtime_revision"):
            kwargs = dict(
                role="claude",
                provider="claude",
                assigned_name="n",
                locator="%1",
                runtime_revision="1.0",
            )
            kwargs[missing] = ""
            with self.subTest(missing=missing):
                with self.assertRaises(ProcessPinError):
                    ProcessGenerationPin(**kwargs)

    def test_attested_at_is_optional_evidence(self) -> None:
        pin = ProcessGenerationPin(
            role="claude",
            provider="claude",
            assigned_name="n",
            locator="%1",
            runtime_revision="1.0",
        )
        self.assertEqual(pin.attested_at, "")
        self.assertEqual(pin.stable_identity, ("claude", "claude", "n"))
        self.assertEqual(pin.match_key, ("claude", "claude", "n", "%1", "1.0"))

    def test_declared_slots_round_trip_is_deterministic(self) -> None:
        forward = encode_declared_slots(_slots())
        reversed_order = encode_declared_slots(tuple(reversed(_slots())))
        self.assertEqual(forward, reversed_order)  # order-insensitive
        self.assertEqual(decode_declared_slots(forward), tuple(sorted(
            _slots(), key=lambda p: p.stable_identity)))

    def test_empty_declared_slots_serialize_to_empty_string(self) -> None:
        # A byte-identical match with the migrated pre-v5 default keeps the row stable.
        self.assertEqual(encode_declared_slots(()), "")
        self.assertEqual(decode_declared_slots(""), ())

    def test_duplicate_slot_identity_is_rejected(self) -> None:
        with self.assertRaises(ProcessPinError):
            validate_declared_slots((_pin("codex", "%1"), _pin("codex", "%2")))

    def test_corrupt_declared_slots_raise_not_a_shorter_list(self) -> None:
        for raw in ("{not json", "[]", '{"version": 1}', '{"slots": []}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ProcessPinError):
                    decode_declared_slots(raw)

    def test_newer_snapshot_version_fails_closed(self) -> None:
        newer = (
            '{"version": %d, "slots": []}' % (DECLARED_SLOTS_VERSION + 1)
        )
        with self.assertRaises(ProcessPinError):
            decode_declared_slots(newer)

    def test_release_and_replacement_pins_still_decode_backward_compatibly(self) -> None:
        # #13810 introduces ProcessGenerationPin for declared_slots but must NOT break the
        # existing 3-field ReleasePin decode used by the release / replacement axes.
        legacy = '[{"role": "codex", "assigned_name": "n", "locator": "%1"}]'
        pins = decode_release_pins(legacy)
        self.assertEqual(len(pins), 1)
        self.assertIsInstance(pins[0], ReleasePin)
        self.assertEqual(pins[0].locator, "%1")


class BindingClassifierTest(unittest.TestCase):
    """binding_kind vocabulary + the legacy-unbound derived classifier."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = LaneLifecycleStore(home=self.home)
        self.decl = LaneDeclarationStore(home=self.home)

    def test_migrated_issue_lane_is_issue_kind_generation_one(self) -> None:
        key = LaneLifecycleKey(WS, "issue_lane")
        self.store.declare_active(key, decision=_issue_decision(), issue_id=ISSUE)
        rec = self.store.get(key)
        self.assertEqual(rec.binding_kind, BINDING_KIND_ISSUE)
        self.assertEqual(rec.project_scope, "")
        self.assertEqual(rec.lane_generation, 1)
        self.assertEqual(rec.declared_slots, "")
        self.assertFalse(rec.is_legacy_unbound)

    def test_unbound_issue_lane_is_legacy_unbound_without_scope_backfill(self) -> None:
        # An empty-issue issue lane is surfaced as legacy_unbound; its project scope is
        # NOT auto-completed (j#78386 §6) and its kind stays issue (never a guessed gateway).
        key = LaneLifecycleKey(WS, "unbound_lane")
        self.store.declare_active(key, decision=_issue_decision(), issue_id="")
        rec = self.store.get(key)
        self.assertEqual(rec.binding_kind, BINDING_KIND_ISSUE)
        self.assertEqual(rec.project_scope, "")
        self.assertTrue(rec.is_legacy_unbound)


class DeclareLaneServiceTest(unittest.TestCase):
    """The common declaration / backfill service (issue AND project-gateway)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = LaneLifecycleStore(home=self.home)
        self.decl = LaneDeclarationStore(home=self.home)

    # -- issue binding -------------------------------------------------------

    def test_issue_declaration_and_exact_duplicate_is_idempotent(self) -> None:
        key = LaneLifecycleKey(WS, "issue_lane")
        first = self.decl.declare_lane(
            key, decision=_issue_decision(), binding_kind=BINDING_KIND_ISSUE, issue_id=ISSUE
        )
        self.assertTrue(first.applied)
        again = self.decl.declare_lane(
            key, decision=_issue_decision(), binding_kind=BINDING_KIND_ISSUE, issue_id=ISSUE
        )
        # A live-adopt of the exact same lane is a no-op success, not a conflict (#13809).
        self.assertTrue(again.applied)
        self.assertEqual(again.reason, CAS_APPLIED)
        self.assertEqual(len(self.store.records()), 1)  # no second row

    def test_issue_scope_must_be_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.decl.declare_lane(
                LaneLifecycleKey(WS, "l"),
                decision=_issue_decision(),
                binding_kind=BINDING_KIND_ISSUE,
                issue_id=ISSUE,
                project_scope=SCOPE,
            )

    def test_issue_owner_conflict_is_zero_write(self) -> None:
        self.decl.declare_lane(
            LaneLifecycleKey(WS, "a"),
            decision=_issue_decision(),
            issue_id=ISSUE,
        )
        conflict = self.decl.declare_lane(
            LaneLifecycleKey(WS, "b"),
            decision=_issue_decision(),
            issue_id=ISSUE,
        )
        self.assertFalse(conflict.applied)
        self.assertEqual(conflict.reason, CAS_OWNER_CONFLICT)
        self.assertEqual(len(self.store.records()), 1)

    def test_a_different_declaration_at_the_same_key_never_overwrites(self) -> None:
        key = LaneLifecycleKey(WS, "issue_lane")
        self.decl.declare_lane(key, decision=_issue_decision(), issue_id=ISSUE)
        # Same key, different owner binding: refused, not silently overwritten.
        out = self.decl.declare_lane(
            key, decision=_issue_decision(issue="99999", journal="1"), issue_id="99999"
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self.store.get(key).issue_id, ISSUE)  # unchanged

    def test_idempotency_ignores_generation_count(self) -> None:
        # A re-adopt of a lane that has advanced generations still matches on binding
        # identity + declared slots, not on the incarnation number.
        key = LaneLifecycleKey(WS, "gw")
        self.decl.declare_lane(
            key,
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        rec = self.store.get(key)
        self.store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=rec.revision,
            target=DISPOSITION_RETIRED, decision=_gw_decision(),
        )
        rec = self.store.get(key)
        self.decl.open_next_generation(
            key, expected_revision=rec.revision, expected_generation=rec.lane_generation,
            decision=_gw_decision(), declared_slots=_slots(),
        )
        self.assertEqual(self.store.get(key).lane_generation, 2)
        again = self.decl.declare_lane(
            key,
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        self.assertTrue(again.applied)  # idempotent despite generation == 2

    # -- project-gateway binding --------------------------------------------

    def test_project_gateway_declaration_stores_full_scope_and_slots(self) -> None:
        key = LaneLifecycleKey(WS, "pgwv1_x")
        out = self.decl.declare_lane(
            key,
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        self.assertTrue(out.applied)
        rec = self.store.get(key)
        self.assertEqual(rec.binding_kind, BINDING_KIND_PROJECT_GATEWAY)
        self.assertEqual(rec.project_scope, SCOPE)
        self.assertEqual(rec.issue_id, "")
        self.assertEqual(len(rec.declared_pins), 2)
        self.assertFalse(rec.is_legacy_unbound)  # a gateway is not a legacy unbound issue

    def test_project_gateway_requires_scope_no_issue_and_slots(self) -> None:
        with self.assertRaises(ValueError):  # no scope
            self.decl.declare_lane(
                LaneLifecycleKey(WS, "g1"),
                decision=_gw_decision(),
                binding_kind=BINDING_KIND_PROJECT_GATEWAY,
                declared_slots=_slots(),
            )
        with self.assertRaises(ValueError):  # carries an issue
            self.decl.declare_lane(
                LaneLifecycleKey(WS, "g2"),
                decision=_gw_decision(),
                binding_kind=BINDING_KIND_PROJECT_GATEWAY,
                project_scope=SCOPE,
                issue_id=ISSUE,
                declared_slots=_slots(),
            )
        with self.assertRaises(ValueError):  # no declared slot set
            self.decl.declare_lane(
                LaneLifecycleKey(WS, "g3"),
                decision=_gw_decision(),
                binding_kind=BINDING_KIND_PROJECT_GATEWAY,
                project_scope=SCOPE,
            )

    def test_second_active_owner_of_one_project_scope_is_zero_write(self) -> None:
        self.decl.declare_lane(
            LaneLifecycleKey(WS, "pgwv1_x"),
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        # A different derived lane (a 48-bit digest alias) claiming the SAME full scope is
        # refused by the storage index, not accepted as a second owner (j#78386 §6).
        conflict = self.decl.declare_lane(
            LaneLifecycleKey(WS, "pgwv1_x_alias"),
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        self.assertFalse(conflict.applied)
        self.assertEqual(conflict.reason, CAS_OWNER_CONFLICT)

    def test_same_derived_lane_with_a_different_full_scope_never_overwrites(self) -> None:
        # The cross-revision alias case: the SAME derived lane id declared with a DIFFERENT
        # full scope is refused (never re-stamped over the stored scope).
        key = LaneLifecycleKey(WS, "pgwv1_x")
        self.decl.declare_lane(
            key,
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        out = self.decl.declare_lane(
            key,
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope="giken-cloud-drive/other-project",
            declared_slots=_slots(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self.store.get(key).project_scope, SCOPE)  # unchanged

    def test_a_project_gateway_and_an_issue_lane_share_no_owner_space(self) -> None:
        # The two owner indexes are independent: an issue lane owning ISSUE and a gateway
        # lane owning SCOPE coexist in one workspace.
        self.decl.declare_lane(
            LaneLifecycleKey(WS, "issue_lane"), decision=_issue_decision(), issue_id=ISSUE
        )
        out = self.decl.declare_lane(
            LaneLifecycleKey(WS, "pgwv1_x"),
            decision=_gw_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE,
            declared_slots=_slots(),
        )
        self.assertTrue(out.applied)

    def test_declaration_fails_closed_on_an_unreadable_store(self) -> None:
        path = lane_lifecycle_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(LaneLifecycleError):
            self.decl.declare_lane(
                LaneLifecycleKey(WS, "l"), decision=_issue_decision(), issue_id=ISSUE
            )


class OpenNextGenerationTest(unittest.TestCase):
    """Explicit re-incarnation of a retired lane (never an implicit revive)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = LaneLifecycleStore(home=self.home)
        self.decl = LaneDeclarationStore(home=self.home)
        self.key = LaneLifecycleKey(WS, "issue_lane")
        self.store.declare_active(self.key, decision=_issue_decision(), issue_id=ISSUE)

    def _retire(self) -> None:
        rec = self.store.get(self.key)
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=rec.lane_disposition,
            expected_revision=rec.revision,
            target=DISPOSITION_RETIRED,
            decision=_issue_decision(),
        )
        self.assertTrue(out.applied)

    def test_retired_to_active_is_forbidden_via_transition(self) -> None:
        # The disposition edge is terminal: an implicit revive is refused (owner decision 3).
        self._retire()
        rec = self.store.get(self.key)
        out = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_RETIRED,
            expected_revision=rec.revision,
            target=DISPOSITION_ACTIVE,
            decision=_issue_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_open_next_generation_reincarnates_and_resets_axes(self) -> None:
        self._retire()
        rec = self.store.get(self.key)
        out = self.decl.open_next_generation(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation,
            decision=_issue_decision(journal="79000"),
            declared_slots=(_pin("claude", "%9"),),
        )
        self.assertTrue(out.applied)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(rec.lane_generation, 2)  # bumped, monotonic
        self.assertEqual(rec.process_release, "not_requested")  # reset
        self.assertEqual(rec.replacement_state, "not_requested")  # reset
        self.assertEqual(len(rec.declared_pins), 1)  # new generation's snapshot
        self.assertEqual(rec.decision.journal_id, "79000")  # anchor updated
        # binding is preserved (same lane, re-incarnated)
        self.assertEqual(rec.issue_id, ISSUE)
        self.assertEqual(rec.binding_kind, BINDING_KIND_ISSUE)

    def test_open_next_generation_requires_a_retired_lane(self) -> None:
        rec = self.store.get(self.key)  # still active
        out = self.decl.open_next_generation(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation,
            decision=_issue_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_open_next_generation_guards_revision_and_generation(self) -> None:
        self._retire()
        rec = self.store.get(self.key)
        stale_rev = self.decl.open_next_generation(
            self.key,
            expected_revision=rec.revision + 5,
            expected_generation=rec.lane_generation,
            decision=_issue_decision(),
        )
        self.assertEqual(stale_rev.reason, CAS_STALE_REVISION)
        stale_gen = self.decl.open_next_generation(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation + 9,
            decision=_issue_decision(),
        )
        self.assertEqual(stale_gen.reason, CAS_GENERATION_MISMATCH)
        # neither refusal moved the lane
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_RETIRED)

    def test_stale_generation_approval_cannot_reopen_a_newer_incarnation(self) -> None:
        # A caller holding generation 1's view cannot re-open after another actor already
        # advanced the lane to generation 2.
        self._retire()
        rec_gen1 = self.store.get(self.key)
        self.decl.open_next_generation(
            self.key,
            expected_revision=rec_gen1.revision,
            expected_generation=rec_gen1.lane_generation,
            decision=_issue_decision(),
        )
        self._retire()  # retire generation 2
        stale = self.decl.open_next_generation(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            expected_generation=1,  # the stale approval's generation
            decision=_issue_decision(),
        )
        self.assertFalse(stale.applied)
        self.assertEqual(stale.reason, CAS_GENERATION_MISMATCH)

    def test_reopen_refused_when_another_lane_took_the_issue(self) -> None:
        self._retire()
        # While this lane is retired, another lane takes ISSUE.
        self.store.declare_active(
            LaneLifecycleKey(WS, "recovery_lane"), decision=_issue_decision(), issue_id=ISSUE
        )
        rec = self.store.get(self.key)
        out = self.decl.open_next_generation(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation,
            decision=_issue_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_OWNER_CONFLICT)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_RETIRED)

    def test_reopen_missing_lane_is_not_found(self) -> None:
        out = self.decl.open_next_generation(
            LaneLifecycleKey(WS, "ghost"),
            expected_revision=1,
            expected_generation=1,
            decision=_issue_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)


class SchemaV5MigrationTest(unittest.TestCase):
    """v5 additive migration + the project-gateway owner index verified by constraint."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, "issue_lane")
        self.addCleanup(self._tmp.cleanup)

    def _columns(self) -> list:
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            return [r[1] for r in conn.execute("PRAGMA table_info(lane_lifecycle_records)")]
        finally:
            conn.close()

    def _recorded(self) -> int:
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            return conn.execute(
                "SELECT schema_version FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
        finally:
            conn.close()

    def _indexes(self) -> set:
        conn = sqlite3.connect(lane_lifecycle_path(self.home))
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='lane_lifecycle_records'"
                )
            }
        finally:
            conn.close()

    def test_fresh_store_is_v5_with_both_owner_indexes(self) -> None:
        LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(self._recorded(), LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertEqual(LANE_LIFECYCLE_SCHEMA_VERSION, 5)
        for col in ("binding_kind", "project_scope", "lane_generation", "declared_slots"):
            self.assertIn(col, self._columns())
        self.assertIn("idx_lane_lifecycle_active_owner", self._indexes())
        self.assertIn("idx_lane_lifecycle_active_project_owner", self._indexes())

    def _rewind_to_v4(self) -> Path:
        """A healthy current store rewound to a recorded v4 shape (a real pre-#13810 store)."""
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            self.key, decision=_issue_decision(), issue_id=ISSUE,
            worktree_identity="wt_bound01",
        )
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP INDEX IF EXISTS idx_lane_lifecycle_active_project_owner")
            for col in ("binding_kind", "project_scope", "lane_generation", "declared_slots"):
                conn.execute(f"ALTER TABLE lane_lifecycle_records DROP COLUMN {col}")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 4 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_v4_migrates_additively_backup_first_preserving_binding(self) -> None:
        path = self._rewind_to_v4()
        before = path.read_bytes()
        self.assertNotIn("binding_kind", self._columns())  # precondition: v4 shape
        LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(self._recorded(), LANE_LIFECYCLE_SCHEMA_VERSION)
        # the pre-migration snapshot was preserved before the first write
        backups = sorted((self.home / "backups").glob("state-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "state.sqlite").read_bytes(), before)
        # the existing issue row lands on the additive defaults (never guessed)
        rec = LaneLifecycleStore(home=self.home).get(self.key)
        self.assertEqual(rec.binding_kind, BINDING_KIND_ISSUE)
        self.assertEqual(rec.project_scope, "")
        self.assertEqual(rec.lane_generation, 1)
        self.assertEqual(rec.worktree_identity, "wt_bound01")  # v4 binding preserved
        self.assertIn("idx_lane_lifecycle_active_project_owner", self._indexes())

    def test_newer_v6_component_fails_closed_byte_unchanged(self) -> None:
        LaneLifecycleStore(home=self.home).declare_active(
            self.key, decision=_issue_decision(), issue_id=ISSUE
        )
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = ? WHERE component = ?",
                (LANE_LIFECYCLE_SCHEMA_VERSION + 1, LANE_LIFECYCLE_COMPONENT),
            )
            conn.execute(
                "ALTER TABLE lane_lifecycle_records "
                "ADD COLUMN future_guard TEXT NOT NULL DEFAULT 'future'"
            )
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(path.read_bytes(), before)

    def test_v5_store_missing_the_project_index_fails_closed(self) -> None:
        # The project-gateway owner index is a critical v5 authority constraint; a store
        # recorded v5 but missing it is corrupt, never silently repaired.
        LaneLifecycleStore(home=self.home).declare_active(
            self.key, decision=_issue_decision(), issue_id=ISSUE
        )
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP INDEX idx_lane_lifecycle_active_project_owner")
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(path.read_bytes(), before)  # no repair / re-stamp

    def test_v5_project_index_wrong_predicate_fails_closed(self) -> None:
        # Right name / unique / key columns but a wider predicate (missing the
        # project_gateway scope) constrains a different row set — it is not this constraint.
        LaneLifecycleStore(home=self.home).declare_active(
            self.key, decision=_issue_decision(), issue_id=ISSUE
        )
        path = lane_lifecycle_path(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP INDEX idx_lane_lifecycle_active_project_owner")
            conn.execute(
                "CREATE UNIQUE INDEX idx_lane_lifecycle_active_project_owner "
                "ON lane_lifecycle_records (repo_workspace_id, project_scope) "
                "WHERE project_scope <> ''"
            )
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(path.read_bytes(), before)


class ReviewCorrectionJ78868Test(unittest.TestCase):
    """Review j#78868 changes_requested — the confirmed fail-open / codec findings.

    F2 (open_next_generation abandons an in-flight release), F3 (project-gateway reopen
    with no declared slots), F4 (declared_slots version accepts ``true`` / ``1.0``), F5
    (exact-duplicate declaration ignores the worktree identity).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = LaneLifecycleStore(home=self.home)
        self.decl = LaneDeclarationStore(home=self.home)

    def _retire(self, key: LaneLifecycleKey, decision: DecisionPointer) -> None:
        rec = self.store.get(key)
        out = self.store.transition_disposition(
            key,
            expected_disposition=rec.lane_disposition,
            expected_revision=rec.revision,
            target=DISPOSITION_RETIRED,
            decision=decision,
        )
        self.assertTrue(out.applied)

    # -- F4: the declared-slots version is an EXACT integer ------------------

    def test_f4_version_true_or_float_is_not_v1(self) -> None:
        # True == 1 and 1.0 == 1 in Python; a bare `!=` check would accept both as v1.
        for literal in ("true", "1.0"):
            with self.subTest(literal=literal):
                with self.assertRaises(ProcessPinError):
                    decode_declared_slots('{"version": %s, "slots": []}' % literal)
        # a genuine v1 still decodes
        self.assertEqual(decode_declared_slots('{"version": 1, "slots": []}'), ())

    # -- F2: reopening never abandons an in-flight release -------------------

    def test_f2_open_next_generation_refuses_an_in_flight_release(self) -> None:
        key = LaneLifecycleKey(WS, "f2_lane")
        self.store.declare_active(key, decision=_issue_decision(), issue_id=ISSUE)
        rec = self.store.get(key)
        self.store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED, decision=_issue_decision(),
        )
        rec = self.store.get(key)
        # A release generation opens on the (non-active) lane and is left in flight.
        self.store.request_release(
            key, expected_revision=rec.revision, action_id="rel1",
            pins=[ReleasePin(role="codex", assigned_name="n", locator="%1")],
        )
        rec = self.store.get(key)
        self.assertEqual(rec.process_release, "requested")
        self._retire(key, _issue_decision())
        rec = self.store.get(key)
        out = self.decl.open_next_generation(
            key, expected_revision=rec.revision, expected_generation=rec.lane_generation,
            decision=_issue_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        rec = self.store.get(key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)  # zero-write
        self.assertEqual(rec.process_release, "requested")  # release not abandoned

    def test_f2_open_next_generation_allows_a_finished_release(self) -> None:
        # The guard is not over-strict: a fully-released (finished) generation is clearable.
        key = LaneLifecycleKey(WS, "f2_ok_lane")
        self.store.declare_active(key, decision=_issue_decision(), issue_id=ISSUE)
        rec = self.store.get(key)
        self.store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED, decision=_issue_decision(),
        )
        rec = self.store.get(key)
        self.store.request_release(
            key, expected_revision=rec.revision, action_id="rel1",
            pins=[ReleasePin(role="codex", assigned_name="n", locator="%1")],
        )
        rec = self.store.get(key)
        self.store.record_release_outcome(
            key, action_id="rel1", expected_revision=rec.revision, target="released",
        )
        self._retire(key, _issue_decision())
        rec = self.store.get(key)
        out = self.decl.open_next_generation(
            key, expected_revision=rec.revision, expected_generation=rec.lane_generation,
            decision=_issue_decision(),
        )
        self.assertTrue(out.applied)
        rec = self.store.get(key)
        self.assertEqual(rec.lane_generation, 2)
        self.assertEqual(rec.process_release, "not_requested")  # reset on the new generation

    # -- F3: a project-gateway reopen keeps its provider-bound slot set ------

    def test_f3_project_gateway_reopen_requires_slots(self) -> None:
        key = LaneLifecycleKey(WS, "pgwv1_f3")
        self.decl.declare_lane(
            key, decision=_gw_decision(), binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=SCOPE, declared_slots=_slots(),
        )
        self._retire(key, _gw_decision())
        rec = self.store.get(key)
        with self.assertRaises(ValueError):
            self.decl.open_next_generation(
                key, expected_revision=rec.revision,
                expected_generation=rec.lane_generation, decision=_gw_decision(),
                declared_slots=(),
            )
        # zero-write: the lane stays retired
        self.assertEqual(self.store.get(key).lane_disposition, DISPOSITION_RETIRED)
        # a slot-bearing reopen succeeds
        out = self.decl.open_next_generation(
            key, expected_revision=rec.revision, expected_generation=rec.lane_generation,
            decision=_gw_decision(), declared_slots=(_pin("codex", "%9"),),
        )
        self.assertTrue(out.applied)

    # -- F5: exact-duplicate identity includes the worktree ------------------

    def test_f5_redeclare_with_a_different_worktree_is_a_conflict(self) -> None:
        key = LaneLifecycleKey(WS, "f5_lane")
        self.decl.declare_lane(
            key, decision=_issue_decision(), issue_id=ISSUE, worktree_identity="wt_a01"
        )
        out = self.decl.declare_lane(
            key, decision=_issue_decision(), issue_id=ISSUE, worktree_identity="wt_b02"
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self.store.get(key).worktree_identity, "wt_a01")  # not overwritten
        # the exact same worktree remains idempotent
        again = self.decl.declare_lane(
            key, decision=_issue_decision(), issue_id=ISSUE, worktree_identity="wt_a01"
        )
        self.assertTrue(again.applied)


if __name__ == "__main__":
    unittest.main()
