"""Regression pins for the #13897 hibernated legacy-migration foreign-inventory gate.

Redmine #13897 (parent #13490), first observed at #13845 j#80123. The metadata-only
``sublane retire --migrate-hibernated-legacy`` path (Redmine #13841) read its live-inventory
zero condition off ``expected_live_slots`` alone, which aggregates ONLY the managed roles.
A lane unit occupied SOLELY by a foreign / unexpected provider therefore measured zero live
and the migration terminalized the durable row to ``retired`` — recording the lane
permanently gone while a real foreign process was still running in its unit. The same
aggregate also drops duplicate slot multiplicity (roles collapse into a set) and rows with
no locator (skipped), so a corrupt / unreadable inventory could terminalize too.

The fix reads the raw ``expected_slot_rows`` scan alongside the aggregate and fails closed on
each dropped fact, exactly as the #13845 bound-retire sibling already does against the same
shared primitives. The foreign gate is an ADDITIONAL conjunctive condition — exact
managed-slot absence is still required and never relaxed:

- a unit holding solely a foreign occupant -> ``foreign_inventory_present`` (this ticket's
  reproduction), zero-write;
- two rows for the same canonical managed slot -> ``duplicate_inventory``, zero-write;
- an expected slot's row with no readable locator the liveness contract does not call dead ->
  ``expected_identity_unresolved``, zero-write;
- a positively-stale (present-but-blank) residue row does NOT block (positive proof of
  deadness, never absence of proof of liveness);
- the foreign gate is scoped to the TARGETED units and never reads the coordinator
  default-lane pair or another lane's occupant as foreign (green path unchanged);
- the ordinary #13841 legacy migration and its idempotent replay still succeed on a
  genuinely quiescent unit.

Synthetic only (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr inventory; never the shared
``$HOME/.mozyo_bridge`` and never a live pane / process / route mutation). Boundary
(Redmine #13897): no process launch / close / resume, no worktree / branch removal, no raw
Herdr / tmux, no origin/main, no production / tag / publish.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
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
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
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
    MIGRATE_DUPLICATE_INVENTORY,
    MIGRATE_EXPECTED_IDENTITY_UNRESOLVED,
    MIGRATE_FOREIGN_INVENTORY_PRESENT,
    MIGRATE_LIVE_PAIR_PRESENT,
    MIGRATE_RETIRED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E402,E501
    SLOT_STALE,
    classify_named_slot,
)

_WORKSPACE_ID = "e1487dcb1f2d4412"
_LANE = "issue_13756_fill_actionability"
_ISSUE = "13756"
_JOURNAL = "79115"


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


def _seed_hibernated_released(store: LaneLifecycleStore, *, key: LaneLifecycleKey) -> None:
    """Drive a row to the exact legacy signature: hibernated + released + empty worktree."""
    dec = _decision()
    store.declare_active(key, decision=dec, issue_id=_ISSUE, worktree_identity="")
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
    rec = store.get(key)
    store.record_release_outcome(
        key, action_id="rel-1", expected_revision=rec.revision, target=RELEASE_RELEASED
    )


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


class LegacyMigrationForeignInventoryTests(unittest.TestCase):
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

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        # A fake herdr inventory: the coordinator's default-lane pair only (never a lane slot)
        # — so the lane unit measures ZERO live managed slots by default.
        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
        ]
        self._real_rows = sublane_herdr_projection.list_herdr_agent_rows
        self._real_execute = sublane_herdr_retire.execute_herdr_retire_close

        def fake_rows(env):
            return list(self.rows)

        def fake_execute(plan, **kwargs):
            # The migration path never actuates a close; a guarded-close call in this suite is
            # a bug, so surface it rather than silently closing.
            raise AssertionError("the legacy migration must never call the guarded close")

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

    def _seed_row(self) -> None:
        _seed_hibernated_released(LaneLifecycleStore(), key=self._key())

    def _disposition(self) -> str:
        rec = LaneLifecycleStore().get(self._key())
        return "" if rec is None else rec.lane_disposition

    def _migrate(self):
        args = argparse.Namespace(
            repo=str(self.primary),
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_worktree),
            branch=_LANE,
            integration_branch="main",
            execute=False,
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
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        return code, json.loads(buffer.getvalue())

    def _mig(self, payload) -> dict:
        return payload.get("hibernated_legacy_retire_migration", {})

    # -- the foreign-inventory reproduction (Redmine #13897) --------------

    def test_foreign_only_live_inventory_blocks_zero_write(self) -> None:
        """A unit occupied ONLY by an unexpected provider must not terminalize (#13897).

        The exact reproduction: ``expected_live_slots`` aggregates only the MANAGED roles, so a
        foreign-only unit measures zero live. Before the fix this exited 0 and recorded the row
        ``retired`` while the foreign process kept running.
        """
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "gemini", _LANE, "w28:pFOREIGN"))
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        verdict = self._mig(payload)
        self.assertEqual(verdict["state"], MIGRATE_BLOCKED)
        self.assertEqual(verdict["reason"], MIGRATE_FOREIGN_INVENTORY_PRESENT)
        # The measurement that refused is named: zero managed slots live, yet NOT quiescent.
        self.assertEqual(verdict["expected_live"], [])
        self.assertTrue(verdict["foreign_names"])
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_foreign_occupant_alongside_live_pair_reports_live_first(self) -> None:
        # Both axes non-empty: still zero-write. The live check fires first (it names the more
        # specific problem), and the foreign occupants ride alongside rather than being dropped.
        self._seed_row()
        self.rows += [
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
            _row(_WORKSPACE_ID, "gemini", _LANE, "w28:pFOREIGN"),
        ]
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        verdict = self._mig(payload)
        self.assertEqual(verdict["reason"], MIGRATE_LIVE_PAIR_PRESENT)
        self.assertTrue(verdict["foreign_names"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_foreign_occupant_blocks_the_idempotent_replay_too(self) -> None:
        # An already-retired row must not report success while a foreign occupant runs: a
        # persisted ``retired`` does not prove the unit is quiescent now (the #13841 review
        # j#79150 F2 invariant, extended to the foreign axis by #13897).
        self._seed_row()
        self.assertEqual(self._migrate()[0], 0)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        self.rows.append(_row(_WORKSPACE_ID, "gemini", _LANE, "w28:pFOREIGN"))
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_FOREIGN_INVENTORY_PRESENT)
        self.assertFalse(payload["retire_ok"])
        # Zero-write: the row stays retired, only the success verdict is withheld.
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- scope: the fence is scoped to the TARGETED units -----------------

    def test_foreign_occupant_in_another_lane_does_not_block(self) -> None:
        # A foreign provider sitting in a DIFFERENT lane's unit is none of this migration's
        # business and must not block it.
        self._seed_row()
        self.rows.append(
            _row(_WORKSPACE_ID, "gemini", "issue_99999_other_lane", "w28:pOTHER")
        )
        code, payload = self._migrate()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._mig(payload)["state"], MIGRATE_RETIRED)
        self.assertEqual(self._mig(payload)["foreign_names"], [])
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_coordinator_default_lane_pair_is_not_foreign(self) -> None:
        # Green-path non-regression: the project workspace's default-lane coordinator pair is
        # always in the inventory and must never be read as a foreign occupant of the lane unit
        # (that would make every legacy migration permanently blocked).
        self._seed_row()
        code, payload = self._migrate()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._mig(payload)["state"], MIGRATE_RETIRED)
        self.assertEqual(self._mig(payload)["foreign_names"], [])
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- the duplicate / unresolved-identity axes -------------------------

    def test_duplicate_expected_rows_block_zero_write(self) -> None:
        # Two rows claiming the same canonical managed slot is a corrupt / ambiguous inventory,
        # and no reading of it can license a terminal write. The aggregate collapses roles into
        # a set and cannot express it; the duplicate check runs BEFORE the live read.
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, "w28:pA"))
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, "w28:pB"))
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        self.assertEqual(self._mig(payload)["reason"], MIGRATE_DUPLICATE_INVENTORY)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_single_locatorless_expected_row_blocks_zero_write(self) -> None:
        # A minimal locator-less expected row is "cannot resolve", not "absent": the shared
        # liveness contract reads it as LIVE. Terminalizing off it would rest on the absence of
        # proof of liveness rather than proof of absence.
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, ""))
        code, payload = self._migrate()
        self.assertEqual(code, 1)
        verdict = self._mig(payload)
        self.assertEqual(verdict["reason"], MIGRATE_EXPECTED_IDENTITY_UNRESOLVED)
        self.assertEqual(verdict["expected_live"], [])
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_positively_stale_locatorless_row_does_not_block(self) -> None:
        # The other half of the fence: proceeding requires positive proof of DEADNESS, and a
        # present-but-blank detected-agent row is exactly that. Blocking it would recreate this
        # ticket's own defect (a lane stuck un-terminalizable) in a new shape.
        self._seed_row()
        residue = _row(_WORKSPACE_ID, "codex", _LANE, "")
        residue["agent"] = ""  # present-but-blank == the positive shell-residue signal
        self.assertEqual(classify_named_slot(residue), SLOT_STALE)
        self.rows.append(residue)
        code, payload = self._migrate()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._mig(payload)["state"], MIGRATE_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- the ordinary quiescent path still succeeds -----------------------

    def test_quiescent_unit_still_migrates_and_replays(self) -> None:
        # Non-regression for #13841: a genuinely quiescent legacy row migrates, and a duplicate
        # replay is an idempotent verified no-op.
        self._seed_row()
        code, payload = self._migrate()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._mig(payload)["state"], MIGRATE_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        code2, payload2 = self._migrate()
        self.assertEqual(code2, 0)
        self.assertEqual(self._mig(payload2)["state"], MIGRATE_ALREADY_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)


if __name__ == "__main__":
    unittest.main()
