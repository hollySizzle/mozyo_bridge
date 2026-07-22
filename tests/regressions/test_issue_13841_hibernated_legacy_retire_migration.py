"""Regression pins for the #13841 hibernated legacy-lane retire migration.

Redmine #13841 (parent #12499), live evidence #13756 j#79114–j#79115. A hibernated /
released **legacy** lifecycle row — the coordinator hibernated the lane, its process
release completed durably (``process_release`` reached ``released``), its issue is closed,
worktree clean + integrated, its live pair gone — but whose ``worktree_identity`` is EMPTY
(a pre-#13754 row that never recorded one) can be retired by NEITHER existing path:

- ``sublane retire --execute`` (Redmine #13754) attests ``--worktree`` against the recorded
  binding first, so an empty binding fails closed on ``worktree_binding_unverified`` forever
  (and there is no live pair to close anyway);
- ``backfill_active_binding`` (Redmine #13809) fills an **active** owner row only.

Re-launching a fresh pair only to retire it is the needless actuation the ticket forbids.
The metadata-only migration moves such a row DIRECTLY to the #13689 terminal ``retired``
disposition through a bounded CAS — no process launch / close / resume, no worktree / branch
removal.

Two layers are pinned, both synthetic (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr
inventory, never the shared ``$HOME/.mozyo_bridge`` and never a live pane / process / route
mutation):

1. the bounded store CAS guard matrix (``LaneRetireMigrationStore``): the exact legacy
   signature migrates; every off-signature shape (non-empty worktree, active / superseded
   disposition, unproven / in-flight release, different issue, revision race, absent row) is
   refused zero-write; and
2. the command boundary (``sublane retire --migrate-hibernated-legacy``): the JSON verdict +
   exit code over real roots, with the live-inventory zero read, the head-integration probe,
   idempotent replay, and non-regression of the #13754 guarded close (mutually exclusive).

Boundary (Redmine #13841): no process launch / close / resume, no worktree / branch removal,
no raw Herdr / tmux, no origin/main, no production / tag / publish.
"""

from __future__ import annotations

# The build's current lifecycle schema version: these fixtures pin "a v5 store the
# write gate forward-migrates to CURRENT", not a frozen target version number.
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    LANE_LIFECYCLE_SCHEMA_VERSION,
)

import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_retire_migration import (  # noqa: E402
    LaneRetireMigrationStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_herdr_projection,
    sublane_herdr_retire,
    sublane_lifecycle_command,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_legacy_retire import (  # noqa: E402,E501
    MIGRATE_ALREADY_RETIRED,
    MIGRATE_BLOCKED,
    MIGRATE_HEAD_NOT_INTEGRATED,
    MIGRATE_LIVE_PAIR_PRESENT,
    MIGRATE_NOT_LEGACY_STATE,
    MIGRATE_RELEASE_NOT_PROVEN,
    MIGRATE_RETIRED,
    MIGRATE_WORKTREE_BRANCH_MISMATCH,
    format_migration_text,
)
from mozyo_bridge.core.state.lane_lifecycle import lane_lifecycle_path  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_schema import (  # noqa: E402
    LANE_LIFECYCLE_COMPONENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    REASON_NO_WORKTREE_ANCHOR,
    REASON_WORKSPACE_UNRESOLVED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_WORKSPACE_ID = "e1487dcb1f2d4412"
_LANE = "issue_13756_fill_actionability"
_ISSUE = "13756"
_JOURNAL = "79115"
_OTHER_ISSUE = "13999"


def _decision(issue: str = _ISSUE, journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _row(ws: str, role: str, lane: str, locator: str) -> dict:
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _seed_hibernated_released(
    store: LaneLifecycleStore,
    *,
    key: LaneLifecycleKey,
    issue: str = _ISSUE,
    worktree_identity: str = "",
    release_target: str = RELEASE_RELEASED,
) -> None:
    """Drive a row to hibernated + <release_target> via the REAL store transitions.

    ``worktree_identity`` defaults empty (the legacy signature). ``release_target`` selects
    how far the release generation got: ``released`` (the migratable proof), ``requested``
    (in flight), or ``partial`` (in flight) — the latter two are the release-not-proven
    fail-closed shapes.
    """
    dec = _decision(issue)
    store.declare_active(key, decision=dec, issue_id=issue, worktree_identity=worktree_identity)
    rec = store.get(key)
    store.transition_disposition(
        key,
        expected_disposition=DISPOSITION_ACTIVE,
        expected_revision=rec.revision,
        target=DISPOSITION_HIBERNATED,
        decision=dec,
    )
    rec = store.get(key)
    store.request_release(
        key,
        expected_revision=rec.revision,
        action_id="rel-1",
        pins=[
            ReleasePin("gateway", "codex-mzb1", "w1:p1"),
            ReleasePin("worker", "claude-mzb1", "w1:p2"),
        ],
    )
    if release_target == RELEASE_REQUESTED:
        return
    rec = store.get(key)
    store.record_release_outcome(
        key,
        action_id="rel-1",
        expected_revision=rec.revision,
        target=release_target,
    )


# ---------------------------------------------------------------------------
# 1. The bounded store CAS guard matrix (pure of the CLI).
# ---------------------------------------------------------------------------


class RetireMigrationCasMatrix(unittest.TestCase):
    """``LaneRetireMigrationStore.retire_released_hibernated_legacy`` fail-closed matrix."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.sqlite"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.store = LaneLifecycleStore(path=self.path)
        self.migration = LaneRetireMigrationStore(path=self.path)

    def _seed(self, **kwargs) -> None:
        _seed_hibernated_released(self.store, key=self.key, **kwargs)

    def _migrate(self, *, expected_revision=None, issue=_ISSUE):
        rec = self.store.get(self.key)
        rev = expected_revision if expected_revision is not None else (
            rec.revision if rec is not None else 1
        )
        return self.migration.retire_released_hibernated_legacy(
            self.key, expected_revision=rev, issue_id=issue, decision=_decision(issue)
        )

    def test_exact_legacy_signature_migrates_to_retired(self) -> None:
        self._seed()
        out = self._migrate()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)
        # The decision anchor is recorded; the release axis is untouched (still released).
        self.assertEqual(rec.decision_journal, _JOURNAL)
        self.assertEqual(rec.process_release, RELEASE_RELEASED)

    def test_non_empty_worktree_binding_is_refused(self) -> None:
        # A #13754-bound row (non-empty worktree) retires through the guarded close, never
        # this migration — the empty binding is the defining legacy signature.
        self._seed(worktree_identity="wt_deadbeef")
        out = self._migrate()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_active_disposition_is_refused(self) -> None:
        self.store.declare_active(
            self.key, decision=_decision(), issue_id=_ISSUE, worktree_identity=""
        )
        out = self._migrate()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_ACTIVE)

    def test_superseded_disposition_is_refused(self) -> None:
        # An active lane superseded by a recovery lane; the superseded row is not migratable.
        recovery = LaneLifecycleKey(_WORKSPACE_ID, "issue_13756_recovery")
        self.store.declare_active(
            self.key, decision=_decision(), issue_id=_ISSUE, worktree_identity=""
        )
        rec = self.store.get(self.key)
        self.store.supersede_and_activate(
            superseded=self.key,
            expected_revision=rec.revision,
            recovery=recovery,
            decision=_decision(),
        )
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_SUPERSEDED)
        out = self._migrate()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_different_issue_is_refused(self) -> None:
        self._seed(issue=_ISSUE)
        # Attest against a DIFFERENT issue than the row owns.
        out = self.migration.retire_released_hibernated_legacy(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            issue_id=_OTHER_ISSUE,
            decision=_decision(_OTHER_ISSUE),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_release_not_requested_is_refused(self) -> None:
        # Hibernated but the release was never requested -> unproven, fail closed.
        dec = _decision()
        self.store.declare_active(self.key, decision=dec, issue_id=_ISSUE, worktree_identity="")
        rec = self.store.get(self.key)
        self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=dec,
        )
        out = self._migrate()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_release_in_flight_requested_is_refused(self) -> None:
        self._seed(release_target=RELEASE_REQUESTED)
        out = self._migrate()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_release_partial_is_refused(self) -> None:
        self._seed(release_target=RELEASE_PARTIAL)
        out = self._migrate()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_revision_race_loses(self) -> None:
        self._seed()
        rec = self.store.get(self.key)
        # A caller holding a stale revision (one behind) never clobbers the newer row.
        out = self._migrate(expected_revision=rec.revision - 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_absent_row_is_not_found(self) -> None:
        out = self.migration.retire_released_hibernated_legacy(
            self.key, expected_revision=1, issue_id=_ISSUE, decision=_decision()
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_empty_issue_and_foreign_anchor_raise(self) -> None:
        self._seed()
        with self.assertRaises(ValueError):
            self.migration.retire_released_hibernated_legacy(
                self.key, expected_revision=2, issue_id="", decision=_decision()
            )
        with self.assertRaises(Exception):
            # A decision anchored to a different issue cannot authorize this binding.
            self.migration.retire_released_hibernated_legacy(
                self.key,
                expected_revision=2,
                issue_id=_ISSUE,
                decision=_decision(_OTHER_ISSUE),
            )

    def test_double_migrate_at_stale_revision_is_refused_not_reapplied(self) -> None:
        # First migrate wins; a replay at the (now stale) revision is refused. The idempotent
        # success of a genuine replay is the command layer's job (it re-reads the retired row).
        self._seed()
        rec = self.store.get(self.key)
        first = self.migration.retire_released_hibernated_legacy(
            self.key, expected_revision=rec.revision, issue_id=_ISSUE, decision=_decision()
        )
        self.assertTrue(first.applied)
        second = self.migration.retire_released_hibernated_legacy(
            self.key, expected_revision=rec.revision, issue_id=_ISSUE, decision=_decision()
        )
        self.assertFalse(second.applied)
        self.assertEqual(second.reason, CAS_STALE_REVISION)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_RETIRED)


# ---------------------------------------------------------------------------
# 2. The command boundary: `sublane retire --migrate-hibernated-legacy`.
# ---------------------------------------------------------------------------


def _init_repo(root: Path, *, anchor: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (root / ".mozyo-bridge" / "config.yaml").write_text(
        "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    if anchor:
        (root / ".mozyo-bridge" / "workspace-anchor.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": _WORKSPACE_ID,
                    "canonical_session": "mzb-test",
                    "project_name": "mozyo_bridge",
                    "created_at": "2026-07-15T00:00:00+00:00",
                    "updated_at": "2026-07-15T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "base", cwd=root)


class RetireMigrationCommandTests(unittest.TestCase):
    """The command boundary over real roots + a fake herdr inventory (isolated home)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.home.mkdir()
        self.primary = tmp / "primary"
        _init_repo(self.primary, anchor=True)
        self.lane_worktree = tmp / "lane_wt"
        _git(
            "worktree", "add", "-b", _LANE, str(self.lane_worktree), "main",
            cwd=self.primary,
        )
        # An integration worktree carrying NO workspace anchor (the #13748 mis-aimed root).
        self.integration = tmp / "integration"
        _init_repo(self.integration, anchor=False)

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        # A fake herdr inventory: the coordinator's default-lane pair only (never a lane
        # slot) — so the lane unit measures ZERO live managed slots by default.
        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
        ]
        self.rows_error: Exception | None = None
        self._real_rows = sublane_herdr_projection.list_herdr_agent_rows
        self._real_execute = sublane_herdr_retire.execute_herdr_retire_close

        def fake_rows(env):
            if self.rows_error is not None:
                raise self.rows_error
            return list(self.rows)

        def fake_execute(plan, **kwargs):
            # No real herdr binary in the test env: close the planned rows in the fake
            # inventory (only the #13754 guarded-close regression test reaches this; the
            # migration path never calls it).
            closed = []
            for role, locator in plan.close_targets:
                self.rows = [r for r in self.rows if r["pane_id"] != locator]
                closed.append((role, locator))
            return HerdrRetireCloseResult(
                workspace_id=plan.workspace_id,
                lane_id=plan.lane_id,
                closed=tuple(closed),
                foreign_names=plan.foreign_names,
            )

        sublane_herdr_projection.list_herdr_agent_rows = fake_rows
        sublane_herdr_retire.execute_herdr_retire_close = fake_execute

        def _restore():
            sublane_herdr_projection.list_herdr_agent_rows = self._real_rows
            sublane_herdr_retire.execute_herdr_retire_close = self._real_execute
            if self._prev_home is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
            self._tmp.cleanup()

        self.addCleanup(_restore)

    # -- helpers ----------------------------------------------------------

    def _key(self) -> LaneLifecycleKey:
        return LaneLifecycleKey(_WORKSPACE_ID, _LANE)

    def _seed_row(self, **kwargs) -> None:
        _seed_hibernated_released(LaneLifecycleStore(), key=self._key(), **kwargs)

    def _disposition(self) -> str:
        rec = LaneLifecycleStore().get(self._key())
        return "" if rec is None else rec.lane_disposition

    def _lane_ahead_of_main(self) -> None:
        """Advance the lane branch past main so its head is NOT integrated."""
        (self.lane_worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
        _git("add", "-A", cwd=self.lane_worktree)
        _git("commit", "-m", "lane wip", cwd=self.lane_worktree)

    def _migrate(
        self,
        *,
        repo: Path | None = None,
        worktree: Path | None = "__lane__",
        issue: str = _ISSUE,
        branch: str = _LANE,
        integration_branch: str = "main",
        preflight_green: bool = True,
        also_execute: bool = False,
        json_out: bool = True,
    ):
        repo = repo if repo is not None else self.primary
        wt = self.lane_worktree if worktree == "__lane__" else worktree
        args = argparse.Namespace(
            repo=str(repo),
            issue=issue,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(wt) if wt is not None else None,
            branch=branch,
            integration_branch=integration_branch,
            execute=also_execute,
            migrate_hibernated_legacy=True,
            json=json_out,
            issue_closed=preflight_green,
            callbacks_drained=preflight_green,
            verified=preflight_green,
            durable_record=preflight_green,
            target_identity_known=preflight_green,
            latest_generation_admissible=preflight_green,
            review_generation_json=None,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        raw = buffer.getvalue()
        return code, (json.loads(raw) if json_out else raw)

    def _mig(self, payload) -> dict:
        return payload.get("hibernated_legacy_retire_migration", {})

    # -- Redmine #13844 R5-F2: v5 + active peer + CAS refusal via the PRODUCTION entrypoint --

    def _add_active_peer_and_rewind_to_v5(self) -> None:
        LaneLifecycleStore().declare_active(
            LaneLifecycleKey(_WORKSPACE_ID, "issue_13800_peer_lane"),
            decision=_decision("13800"),
            issue_id="13800",
        )
        conn = sqlite3.connect(lane_lifecycle_path())
        try:
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN reconcile_phase")
            # v7 (Redmine #13647) added lane_kind; a faithful pre-v7 rewind drops it too,
            # or the shape is a NEWER table merely re-stamped to an old version.
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN lane_kind")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 5 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()

    def _rewind_schema_to_v5(self) -> None:
        conn = sqlite3.connect(lane_lifecycle_path())
        try:
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN reconcile_phase")
            # v7 (Redmine #13647) added lane_kind; a faithful pre-v7 rewind drops it too,
            # or the shape is a NEWER table merely re-stamped to an old version.
            conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN lane_kind")
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 5 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()

    def _schema_version(self) -> int:
        conn = sqlite3.connect(lane_lifecycle_path())
        try:
            return conn.execute(
                "SELECT schema_version FROM state_schema_components WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_r13844_v5_peer_cas_refusal_reports_migration_and_honest_text(self) -> None:
        # A faithful production run: a non-legacy (non-empty-worktree) row that reaches the retire
        # CAS, on a shared v5 store with an active peer. The write gate migrates v5 -> v6, the CAS
        # then refuses (not the empty-binding legacy signature) — a blocked verdict that DID
        # migrate. JSON must report the migration; text must separate the side effects.
        self._seed_row(worktree_identity="wt_deadbeef")
        self._add_active_peer_and_rewind_to_v5()
        self.assertEqual(self._schema_version(), 5)

        code, payload = self._migrate()  # json
        self.assertEqual(code, 1)
        mig = self._mig(payload)
        self.assertEqual(mig["state"], MIGRATE_BLOCKED)
        self.assertEqual(mig["reason"], MIGRATE_NOT_LEGACY_STATE)
        self.assertEqual(self._schema_version(), LANE_LIFECYCLE_SCHEMA_VERSION)  # the write gate migrated the shared store
        self.assertIsNotNone(mig["lifecycle_migration"])
        self.assertEqual(mig["lifecycle_migration"]["from_version"], 5)
        self.assertEqual(mig["lifecycle_migration"]["to_version"], LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertIn(
            "issue_13800_peer_lane", mig["lifecycle_migration"]["peer_active_lanes"]
        )
        # the lane ROW itself was NOT retired (the CAS refused).
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

        # Re-arm the v5 signature and re-run json=False to exercise the exact production TEXT.
        self._rewind_schema_to_v5()
        self.assertEqual(self._schema_version(), 5)
        code2, text = self._migrate(json_out=False)
        self.assertEqual(code2, 1)
        self.assertEqual(self._schema_version(), LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertNotIn("nothing was written", text)
        self.assertIn("row CAS did not apply", text)
        self.assertIn("SCHEMA was already forward-migrated", text)
        self.assertIn(f"v5 -> v{LANE_LIFECYCLE_SCHEMA_VERSION}", text)

    # -- the happy path ---------------------------------------------------

    def test_hibernated_released_legacy_row_migrates_to_retired(self) -> None:
        self._seed_row()
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        code, payload = self._migrate()
        self.assertEqual(code, 0)
        self.assertTrue(payload["retire_ok"])
        self.assertEqual(self._mig(payload)["state"], MIGRATE_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # No pane close ever happened: the migration is metadata only.
        self.assertNotIn("herdr_retire_close", payload)

    def test_duplicate_replay_is_idempotent(self) -> None:
        self._seed_row()
        self._migrate()
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        code, payload = self._migrate()
        self.assertEqual(code, 0)
        self.assertTrue(payload["retire_ok"])
        self.assertEqual(self._mig(payload)["state"], MIGRATE_ALREADY_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- the fail-closed conditions --------------------------------------

    def test_live_pair_present_blocks(self) -> None:
        # A live managed pair in the lane unit -> not an already-released row; fail closed.
        self.rows += [
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
        ]
        self._seed_row()
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._mig(payload)["state"], MIGRATE_BLOCKED)
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_LIVE_PAIR_PRESENT)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_unintegrated_head_blocks(self) -> None:
        self._seed_row()
        self._lane_ahead_of_main()  # lane is now ahead of main -> not an ancestor
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_HEAD_NOT_INTEGRATED)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_preflight_blocked_never_runs_the_migration(self) -> None:
        # A non-green preflight (issue not closed) blocks upstream: the migration never runs
        # and nothing is written.
        self._seed_row()
        code, payload = self._migrate(preflight_green=False)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertNotIn("hibernated_legacy_retire_migration", payload)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_non_empty_worktree_bound_row_is_not_migrated(self) -> None:
        # A #13754-bound row (non-empty worktree) is refused by the CAS: it retires through
        # the guarded close, not this migration.
        self._seed_row(worktree_identity="wt_deadbeef")
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_NOT_LEGACY_STATE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_release_not_proven_blocks(self) -> None:
        self._seed_row(release_target=RELEASE_REQUESTED)
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_RELEASE_NOT_PROVEN)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_missing_worktree_anchor_blocks(self) -> None:
        self._seed_row()
        code, payload = self._migrate(worktree=None)
        self.assertEqual(code, 1)
        self.assertEqual(self._mig(payload)["reason"], REASON_NO_WORKTREE_ANCHOR)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_workspace_unresolved_root_blocks(self) -> None:
        # The integration worktree carries no workspace anchor: identity is unresolvable.
        self._seed_row()
        code, payload = self._migrate(repo=self.integration, worktree=self.integration)
        self.assertEqual(code, 1)
        self.assertEqual(self._mig(payload)["reason"], REASON_WORKSPACE_UNRESOLVED)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_inventory_unreadable_blocks(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        self._seed_row()
        self.rows_error = HerdrSessionStartError("herdr down")
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_worktree_branch_mismatch_blocks(self) -> None:
        # review j#79150 finding 1: the --worktree is on the lane branch, but --branch names a
        # DIFFERENT branch. The clean + integrated evidence would then describe `main` while the
        # worktree's real head is the lane branch — refuse zero-write.
        self._seed_row()
        code, payload = self._migrate(branch="main")
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(
            self._mig(payload)["reason"], MIGRATE_WORKTREE_BRANCH_MISMATCH
        )
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_already_retired_replay_with_live_pair_blocks(self) -> None:
        # review j#79150 finding 2: a persisted `retired` does not prove the pair is currently
        # gone. After a migration, a relaunched live pair must make the idempotent replay fail
        # closed (live_pair_present), NOT report already_retired success.
        self._seed_row()
        self._migrate()
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # A pair reappears under the retired lane unit.
        self.rows += [
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
        ]
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_LIVE_PAIR_PRESENT)
        # The row stays retired (zero-write); only the success verdict is withheld.
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- non-regression of the #13754 guarded close ----------------------

    def test_both_destructive_flags_are_rejected_zero_write(self) -> None:
        # review j#79150 finding 3: --migrate-hibernated-legacy and --execute are conflicting
        # destructive intents — passing both is a zero-write error (exit 1), never a silent
        # resolution to one. Nothing is actuated and the row is untouched.
        self._seed_row()
        args = argparse.Namespace(
            repo=str(self.primary),
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_worktree),
            branch=_LANE,
            integration_branch="main",
            execute=True,
            migrate_hibernated_legacy=True,
            json=True,
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err.getvalue())
        self.assertEqual(out.getvalue().strip(), "")  # no JSON, no actuation
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_plain_execute_without_migration_flag_is_unchanged(self) -> None:
        # Regression guard: an active #13754-bound lane still retires through the guarded
        # close when --migrate-hibernated-legacy is absent (the migration path is opt-in).
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        LaneLifecycleStore().declare_active(
            self._key(),
            decision=_decision(),
            issue_id=_ISSUE,
            worktree_identity=derive_lane_workspace_token(
                str(self.lane_worktree.resolve())
            ),
        )
        self.rows += [
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
        ]
        args = argparse.Namespace(
            repo=str(self.primary),
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_worktree),
            branch=_LANE,
            integration_branch="main",
            execute=True,
            migrate_hibernated_legacy=False,
            json=True,
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        payload = json.loads(buffer.getvalue())
        # The guarded close ran (its verdict is present); the migration surface is absent.
        self.assertIn("herdr_retire_close", payload)
        self.assertNotIn("hibernated_legacy_retire_migration", payload)


if __name__ == "__main__":
    unittest.main()
