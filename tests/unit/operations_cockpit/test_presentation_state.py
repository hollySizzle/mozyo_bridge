"""Tests for the home-scoped desired-presentation current-table store (#12304).

Pins the acceptance conditions of Redmine #12304:

- the seed from static repo-local config into ``cockpit_group_membership`` /
  ``projection_preferences`` is **idempotent** (a re-run writes nothing, not even
  ``updated_at``) and **non-destructive** (it never deletes a row);
- the seed **records the source config version** (provenance);
- it reads only explicit ``unit_overrides``, never live tmux geometry, and never
  seeds ``membership_rules`` (no live-geometry-as-truth);
- the store carries **no handoff / liveness / approval / close / routing column**;
- the read-model display policy folds desired rows into
  ``present`` / ``stale`` / ``desired_but_missing``;
- a present-but-broken config **fails closed**;
- an unrecognized schema version **fails closed** rather than dropping state.
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import sys

# Self-contained src bootstrap so isolated discovery (unittest discover
# scoped to this subpackage or a single file) imports mozyo_bridge without
# relying on a sibling test inserting src first (Redmine #12490 j#64426).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from mozyo_bridge.domain.presentation_grouping import PresentationGroupingConfig
from mozyo_bridge.domain.repo_local_config import REPO_LOCAL_CONFIG_VERSION
from mozyo_bridge.presentation_state import (
    PRESENTATION_STATE_SCHEMA_VERSION,
    SOURCE_REPO_LOCAL_CONFIG,
    STATUS_DESIRED_BUT_MISSING,
    STATUS_PRESENT,
    STATUS_STALE,
    GroupMembershipRow,
    PresentationStateError,
    PresentationStateStore,
    classify_membership,
    presentation_state_path,
    unit_id_for,
)


def _config(overrides, *, groups=None):
    """Build a PresentationGroupingConfig from override dicts."""
    record = {
        "project_groups": groups
        or [{"group_id": "project:alpha", "label": "Alpha"}],
        "grouping": {"unit_overrides": overrides},
    }
    return PresentationGroupingConfig.from_record(record)


class PresentationStateStoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.store = PresentationStateStore(home=self.home)


class PathTest(PresentationStateStoreBase):
    def test_path_under_home(self) -> None:
        self.assertEqual(
            presentation_state_path(self.home),
            self.home / "presentation.sqlite",
        )


class SeedBasicsTest(PresentationStateStoreBase):
    def test_seed_writes_membership_and_projection_and_provenance(self) -> None:
        config = _config(
            [
                {
                    "workspace_id": "ws-alpha",
                    "lane_id": "default",
                    "preferred_group": "project:alpha",
                    "position": 10,
                    "pinned": True,
                    "preferred_projection": "cockpit_pane",
                }
            ]
        )
        result = self.store.seed_from_grouping_config(
            config,
            source_config_version=REPO_LOCAL_CONFIG_VERSION,
            now="2026-06-20T00:00:00+00:00",
        )
        self.assertEqual(result.membership_inserted, 1)
        self.assertEqual(result.projection_inserted, 1)
        self.assertEqual(result.changed, 2)

        membership = self.store.list_group_membership()
        self.assertEqual(len(membership), 1)
        row = membership[0]
        self.assertEqual(row.group_id, "project:alpha")
        self.assertEqual(row.unit_id, unit_id_for("ws-alpha", "default"))
        self.assertEqual(row.position, 10)
        self.assertTrue(row.pinned)
        self.assertFalse(row.hidden)

        projections = self.store.list_projection_preferences()
        self.assertEqual(len(projections), 1)
        self.assertEqual(projections[0].preferred_projection, "cockpit_pane")

        provenance = self.store.get_provenance()
        self.assertIsNotNone(provenance)
        self.assertEqual(
            provenance.source_config_version, REPO_LOCAL_CONFIG_VERSION
        )
        self.assertEqual(provenance.source, SOURCE_REPO_LOCAL_CONFIG)
        self.assertEqual(provenance.membership_rows, 1)
        self.assertEqual(provenance.projection_rows, 1)

    def test_override_with_nothing_to_seed_is_skipped(self) -> None:
        config = _config(
            [{"workspace_id": "ws-x", "lane_id": "default"}]
        )
        result = self.store.seed_from_grouping_config(
            config, source_config_version=1
        )
        self.assertEqual(result.skipped_overrides, 1)
        self.assertEqual(result.changed, 0)
        self.assertEqual(self.store.list_group_membership(), ())
        # Provenance is still recorded on the first seed (so "we seeded v1" is
        # auditable even when nothing was written).
        self.assertIsNotNone(self.store.get_provenance())


class IdempotencyTest(PresentationStateStoreBase):
    def test_reseed_unchanged_config_writes_nothing(self) -> None:
        config = _config(
            [
                {
                    "workspace_id": "ws-alpha",
                    "lane_id": "default",
                    "preferred_group": "project:alpha",
                    "position": 10,
                    "preferred_projection": "cockpit_pane",
                }
            ]
        )
        self.store.seed_from_grouping_config(
            config, source_config_version=1, now="2026-06-20T00:00:00+00:00"
        )
        first = self.store.list_group_membership()
        first_proj = self.store.list_projection_preferences()
        first_prov = self.store.get_provenance()

        # Re-seed with a *different* timestamp: a content-comparing upsert must
        # not even rewrite updated_at, so the rows stay byte-identical.
        result = self.store.seed_from_grouping_config(
            config, source_config_version=1, now="2099-01-01T00:00:00+00:00"
        )
        self.assertEqual(result.changed, 0)
        self.assertEqual(result.membership_unchanged, 1)
        self.assertEqual(result.projection_unchanged, 1)
        self.assertEqual(self.store.list_group_membership(), first)
        self.assertEqual(self.store.list_projection_preferences(), first_proj)
        # A no-op re-seed leaves provenance untouched too (its seeded_at does not
        # advance to the new timestamp).
        self.assertEqual(self.store.get_provenance(), first_prov)

    def test_changed_override_updates_and_bumps_timestamp(self) -> None:
        self.store.seed_from_grouping_config(
            _config(
                [
                    {
                        "workspace_id": "ws-alpha",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                        "position": 10,
                    }
                ]
            ),
            source_config_version=1,
            now="2026-06-20T00:00:00+00:00",
        )
        result = self.store.seed_from_grouping_config(
            _config(
                [
                    {
                        "workspace_id": "ws-alpha",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                        "position": 20,
                    }
                ]
            ),
            source_config_version=1,
            now="2026-06-21T00:00:00+00:00",
        )
        self.assertEqual(result.membership_updated, 1)
        row = self.store.list_group_membership()[0]
        self.assertEqual(row.position, 20)
        self.assertEqual(row.updated_at, "2026-06-21T00:00:00+00:00")


class NonDestructiveTest(PresentationStateStoreBase):
    def test_removed_override_row_survives_reseed(self) -> None:
        self.store.seed_from_grouping_config(
            _config(
                [
                    {
                        "workspace_id": "ws-a",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                    },
                    {
                        "workspace_id": "ws-b",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                    },
                ]
            ),
            source_config_version=1,
        )
        self.assertEqual(len(self.store.list_group_membership()), 2)
        # Config now drops ws-b: the seed must NOT delete the existing row.
        self.store.seed_from_grouping_config(
            _config(
                [
                    {
                        "workspace_id": "ws-a",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                    }
                ]
            ),
            source_config_version=1,
        )
        unit_ids = {r.unit_id for r in self.store.list_group_membership()}
        self.assertIn(unit_id_for("ws-b", "default"), unit_ids)
        self.assertEqual(len(unit_ids), 2)


class LiveGeometryBoundaryTest(PresentationStateStoreBase):
    def test_membership_rules_are_not_seeded(self) -> None:
        # A membership_rule derives a group from launch-time facts; it must be
        # resolved at launch, never frozen into durable membership by the seed.
        config = PresentationGroupingConfig.from_record(
            {
                "project_groups": [
                    {"group_id": "project:alpha", "label": "Alpha"}
                ],
                "grouping": {
                    "membership_rules": [
                        {
                            "when": {"repo_label": "alpha"},
                            "group_id": "project:alpha",
                        }
                    ]
                },
            }
        )
        result = self.store.seed_from_grouping_config(
            config, source_config_version=1
        )
        self.assertEqual(result.changed, 0)
        self.assertEqual(self.store.list_group_membership(), ())


class DryRunTest(PresentationStateStoreBase):
    def test_dry_run_reports_but_does_not_write(self) -> None:
        config = _config(
            [
                {
                    "workspace_id": "ws-alpha",
                    "lane_id": "default",
                    "preferred_group": "project:alpha",
                }
            ]
        )
        result = self.store.seed_from_grouping_config(
            config, source_config_version=1, dry_run=True
        )
        self.assertEqual(result.membership_inserted, 1)
        # Rolled back: no row, no provenance persisted.
        self.assertEqual(self.store.list_group_membership(), ())
        self.assertIsNone(self.store.get_provenance())


class ClassifyMembershipTest(unittest.TestCase):
    def _row(self, unit_id: str) -> GroupMembershipRow:
        return GroupMembershipRow(group_id="project:alpha", unit_id=unit_id)

    def test_present_stale_and_missing(self) -> None:
        uid = unit_id_for("ws-alpha")
        rows = (self._row(uid),)

        present = classify_membership(
            rows,
            {uid: "2026-06-20T00:00:00+00:00"},
            now="2026-06-20T00:00:10+00:00",
            stale_after_seconds=300,
        )
        self.assertEqual(present[0].status, STATUS_PRESENT)

        stale = classify_membership(
            rows,
            {uid: "2026-06-20T00:00:00+00:00"},
            now="2026-06-20T01:00:00+00:00",
            stale_after_seconds=300,
        )
        self.assertEqual(stale[0].status, STATUS_STALE)

        missing = classify_membership(
            rows,
            {},
            now="2026-06-20T00:00:10+00:00",
            stale_after_seconds=300,
        )
        self.assertEqual(missing[0].status, STATUS_DESIRED_BUT_MISSING)

    def test_observed_without_threshold_is_present(self) -> None:
        uid = unit_id_for("ws-alpha")
        result = classify_membership((self._row(uid),), {uid: None})
        self.assertEqual(result[0].status, STATUS_PRESENT)

    def test_observed_but_unknown_freshness_with_threshold_is_stale(self) -> None:
        # If a freshness threshold is in force but the observation carries no
        # timestamp, it cannot be proven fresh -> stale, never silently present.
        uid = unit_id_for("ws-alpha")
        result = classify_membership(
            (self._row(uid),),
            {uid: None},
            now="2026-06-20T00:00:00+00:00",
            stale_after_seconds=300,
        )
        self.assertEqual(result[0].status, STATUS_STALE)


class NoAuthorityColumnsTest(PresentationStateStoreBase):
    def test_schema_has_no_routing_or_authority_columns(self) -> None:
        # Force schema creation.
        self.store.seed_from_grouping_config(_config([]), source_config_version=1)
        conn = sqlite3.connect(self.store.path)
        try:
            cols = set()
            for table in ("cockpit_group_membership", "projection_preferences"):
                for row in conn.execute(f"PRAGMA table_info({table})"):
                    cols.add(row[1].lower())
        finally:
            conn.close()
        forbidden = {
            "pane",
            "pane_id",
            "session",
            "window",
            "route",
            "target",
            "approval",
            "review",
            "close",
            "owner",
            "handoff",
            "credential",
            "token",
        }
        self.assertEqual(
            cols & forbidden,
            set(),
            "presentation current tables must carry no routing/liveness/approval "
            "column",
        )


class SchemaVersionFailClosedTest(PresentationStateStoreBase):
    def _bump_version(self) -> None:
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(
                f"PRAGMA user_version = {PRESENTATION_STATE_SCHEMA_VERSION + 1}"
            )
            conn.commit()
        finally:
            conn.close()

    def test_write_path_unknown_schema_version_fails_closed(self) -> None:
        # Create the DB, then bump user_version beyond what this build knows.
        self.store.seed_from_grouping_config(_config([]), source_config_version=1)
        self._bump_version()
        with self.assertRaises(PresentationStateError):
            self.store.seed_from_grouping_config(
                _config([]), source_config_version=1
            )

    def test_read_path_unknown_schema_version_fails_closed(self) -> None:
        # Regression for #12304 review j#62220: the read-only path must NOT read
        # an unknown-schema desired-state DB as an empty result.
        self.store.seed_from_grouping_config(
            _config(
                [
                    {
                        "workspace_id": "ws-a",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                        "preferred_projection": "cockpit_pane",
                    }
                ]
            ),
            source_config_version=1,
        )
        self._bump_version()
        with self.assertRaises(PresentationStateError):
            self.store.list_group_membership()
        with self.assertRaises(PresentationStateError):
            self.store.list_projection_preferences()
        with self.assertRaises(PresentationStateError):
            self.store.get_provenance()

    def test_corrupt_db_is_not_treated_as_missing(self) -> None:
        # A corrupt (non-sqlite) file must fail closed, distinct from a missing
        # file (which is a legitimate empty result).
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_bytes(b"this is definitely not a sqlite database\n" * 8)
        with self.assertRaises(PresentationStateError):
            self.store.list_group_membership()

    def test_missing_db_reads_empty(self) -> None:
        # No file at all -> legitimate empty result, never an error.
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.store.list_group_membership(), ())
        self.assertEqual(self.store.list_projection_preferences(), ())
        self.assertIsNone(self.store.get_provenance())


class CliHandlerTest(unittest.TestCase):
    """End-to-end handler tests over a temp home + repo config."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.repo = base / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True)
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _write_config(self, text: str) -> None:
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            text, encoding="utf-8"
        )

    def _run(self, func, **kwargs):
        args = argparse.Namespace(repo=str(self.repo), **kwargs)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()

    def test_seed_then_show(self) -> None:
        from mozyo_bridge.application.commands_presentation import (
            cmd_presentation_seed,
            cmd_presentation_show,
        )

        self._write_config(
            "presentation:\n"
            "  project_groups:\n"
            "    - group_id: \"project:alpha\"\n"
            "      label: \"Alpha\"\n"
            "  grouping:\n"
            "    unit_overrides:\n"
            "      - workspace_id: \"ws-alpha\"\n"
            "        lane_id: \"default\"\n"
            "        preferred_group: \"project:alpha\"\n"
            "        preferred_projection: \"cockpit_pane\"\n"
        )
        code, out, _ = self._run(
            cmd_presentation_seed, as_json=False, dry_run=False
        )
        self.assertEqual(code, 0)
        self.assertIn("presentation seed", out)

        code, out, _ = self._run(cmd_presentation_show, as_json=False)
        self.assertEqual(code, 0)
        self.assertIn("project:alpha", out)
        self.assertIn("cockpit_pane", out)

    def test_invalid_config_fails_closed(self) -> None:
        from mozyo_bridge.application.commands_presentation import (
            cmd_presentation_seed,
        )

        # An authority-shaped key in the presentation block is rejected by the
        # closed schema; the handler must exit non-zero, not silently seed.
        self._write_config(
            "presentation:\n  approval: \"yes\"\n"
        )
        code, _out, err = self._run(
            cmd_presentation_seed, as_json=False, dry_run=False
        )
        self.assertEqual(code, 1)
        self.assertIn("invalid repo-local config", err)

    def test_missing_config_is_a_clean_no_op_seed(self) -> None:
        from mozyo_bridge.application.commands_presentation import (
            cmd_presentation_seed,
        )

        # No config.yaml at all -> behavior-preserving default -> nothing to seed.
        code, out, _ = self._run(
            cmd_presentation_seed, as_json=False, dry_run=False
        )
        self.assertEqual(code, 0)
        self.assertIn("no changes", out)

    def _corrupt_home_db(self) -> None:
        """Seed a DB under the temp home, then bump its schema version."""
        self._write_config(
            "presentation:\n"
            "  project_groups:\n"
            "    - group_id: \"project:alpha\"\n"
            "      label: \"Alpha\"\n"
            "  grouping:\n"
            "    unit_overrides:\n"
            "      - workspace_id: \"ws-alpha\"\n"
            "        lane_id: \"default\"\n"
            "        preferred_group: \"project:alpha\"\n"
        )
        from mozyo_bridge.application.commands_presentation import (
            cmd_presentation_seed,
        )

        self._run(cmd_presentation_seed, as_json=False, dry_run=False)
        db = self.home / "presentation.sqlite"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                f"PRAGMA user_version = {PRESENTATION_STATE_SCHEMA_VERSION + 1}"
            )
            conn.commit()
        finally:
            conn.close()

    def test_show_fails_closed_on_unknown_schema(self) -> None:
        # Regression for #12304 review j#62220: `presentation show` must not
        # print an unknown-schema desired-state DB as empty + success.
        from mozyo_bridge.application.commands_presentation import (
            cmd_presentation_show,
        )

        self._corrupt_home_db()
        code, _out, err = self._run(cmd_presentation_show, as_json=False)
        self.assertEqual(code, 1)
        self.assertIn("presentation state unreadable", err)

    def test_seed_fails_closed_on_unknown_schema(self) -> None:
        from mozyo_bridge.application.commands_presentation import (
            cmd_presentation_seed,
        )

        self._corrupt_home_db()
        code, _out, err = self._run(
            cmd_presentation_seed, as_json=False, dry_run=False
        )
        self.assertEqual(code, 1)
        self.assertIn("presentation state unwritable", err)


if __name__ == "__main__":
    unittest.main()
