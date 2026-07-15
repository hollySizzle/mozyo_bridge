"""Redmine #13809 — standard live-adopt owner-row backfill via the common service.

The #13810 F1 correction (dispute adjudication j#78878): the standard live-adopt path
(``sublane create --no-dispatch --execute`` onto a live gateway+worker pair) skipped
``append_lane_column`` and so never declared the lane's lifecycle owner row — the measured
``original_identity_unknown`` that permanently blocked ``sublane hibernate`` (#13809). The
adopt path now backfills the owner binding through the common
:class:`...lane_declaration.LaneDeclarationStore.declare_lane`, fail-closed and idempotent.

This is the **isolated synthetic official-path regression** j#78878 required: it drives
the real gate + declaration logic (:func:`declare_adopted_owner_row`) and the real ops
method (:meth:`HerdrSublaneActuatorOps.declare_adopted_lane_lifecycle`) with a synthetic
inventory and an ISOLATED home — never the shared ``$HOME/.mozyo_bridge/state.sqlite`` and
never a live pane / process / route mutation. It covers the rowless live pair (the
installed→source skew that created a lane an older CLI never declared), owner conflict,
ambiguous / incomplete inventory, duplicate-adopt idempotency, and the hibernate retry.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_ACTIVE,
    LaneLifecycleKey,
    LaneLifecycleStore,
    OWNER_ABSENT,
    OWNER_RESOLVED,
    DecisionPointer,
    resolve_lane_owner,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402
    ADOPT_DECL_AMBIGUOUS_LOCATORS,
    ADOPT_DECL_DECLARED,
    ADOPT_DECL_INCOMPLETE_PAIR,
    ADOPT_DECL_NO_ANCHOR,
    ADOPT_DECL_OWNER_CONFLICT,
    ADOPT_DECL_UNRESOLVED_UNIT,
    declare_adopted_owner_row,
)

WS = "ws-shared-project"
ISSUE = "13735"
LANE = "issue_13735_parallel_ci"
JOURNAL = "78400"
PROVIDERS = ("codex", "claude")


def _slots(gw="w1:pG", wk="w1:pW"):
    """A readable, unambiguous live gateway+worker pair (role -> (locator, placement))."""
    return {
        "codex": (gw, ("w1", "t1")),
        "claude": (wk, ("w1", "t1")),
    }


class DeclareAdoptedOwnerRowTest(unittest.TestCase):
    """The gate + declaration logic, hermetic via an injected isolated-home store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.coord = Path(self._tmp.name) / "coord"
        self.coord.mkdir()
        self.worktree = str(Path(self._tmp.name) / "wt_lane")
        self.addCleanup(self._tmp.cleanup)

    def _call(self, **overrides) -> str:
        kwargs = dict(
            journal=JOURNAL,
            issue=ISSUE,
            lane_label=LANE,
            repo_root=self.coord,
            worktree_path=self.worktree,
            providers=PROVIDERS,
            resolved=(WS, LANE, _slots()),
            store_factory=lambda: LaneDeclarationStore(home=self.home),
        )
        kwargs.update(overrides)
        return declare_adopted_owner_row(**kwargs)

    def _owner(self):
        return resolve_lane_owner(WS, ISSUE, home=self.home)

    def test_rowless_live_pair_is_backfilled(self) -> None:
        # The installed->source skew: a lane created by an older CLI has live slots but no
        # lifecycle row. The adopt declares the owner binding CAS-safely.
        self.assertEqual(self._owner().status, OWNER_ABSENT)  # rowless precondition
        self.assertEqual(self._call(), ADOPT_DECL_DECLARED)
        owner = self._owner()
        self.assertEqual(owner.status, OWNER_RESOLVED)
        self.assertEqual(owner.lane_id, LANE)
        row = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(row.lane_disposition, DISPOSITION_ACTIVE)
        self.assertTrue(row.worktree_identity)  # a canonical worktree token was stored

    def test_hibernate_blocker_is_cleared(self) -> None:
        # The #13809 symptom: before the backfill the issue has no resolvable owner
        # (hibernate reads `original_identity_unknown`); after, it resolves to this lane.
        self.assertEqual(self._owner().status, OWNER_ABSENT)
        self._call()
        self.assertTrue(self._owner().resolved)

    def test_duplicate_exact_adopt_is_idempotent(self) -> None:
        self.assertEqual(self._call(), ADOPT_DECL_DECLARED)
        row1 = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(self._call(), ADOPT_DECL_DECLARED)  # no conflict, no second row
        row2 = LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(row1.revision, row2.revision)  # unchanged
        self.assertEqual(len(LaneLifecycleStore(home=self.home).records()), 1)

    def test_missing_anchor_is_zero_write(self) -> None:
        for field in ("journal", "issue", "lane_label"):
            with self.subTest(field=field):
                self.assertEqual(self._call(**{field: ""}), ADOPT_DECL_NO_ANCHOR)
        self.assertEqual(self._owner().status, OWNER_ABSENT)  # nothing written

    def test_incomplete_live_pair_is_zero_write(self) -> None:
        # Only the gateway slot resolved — not the exact live pair this adopt would own.
        self.assertEqual(
            self._call(resolved=(WS, LANE, {"codex": ("w1:pG", ("w1", "t1"))})),
            ADOPT_DECL_INCOMPLETE_PAIR,
        )
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_colliding_locators_are_zero_write(self) -> None:
        # Both slots decode to one locator — an ambiguous / recycled target.
        self.assertEqual(
            self._call(resolved=(WS, LANE, _slots(gw="w1:pX", wk="w1:pX"))),
            ADOPT_DECL_AMBIGUOUS_LOCATORS,
        )
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_unresolved_unit_is_zero_write(self) -> None:
        self.assertEqual(
            self._call(resolved=("", "", _slots())), ADOPT_DECL_UNRESOLVED_UNIT
        )
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_existing_owner_conflict_is_zero_write(self) -> None:
        # Another lane already actively owns the issue: the adopt is refused, nothing
        # written for the adopting lane, and the original owner is untouched.
        other = LaneLifecycleKey(WS, "issue_13735_original")
        LaneDeclarationStore(home=self.home).declare_lane(
            other,
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="1"),
            issue_id=ISSUE,
        )
        self.assertEqual(self._call(), ADOPT_DECL_OWNER_CONFLICT)
        owner = self._owner()
        self.assertEqual(owner.lane_id, "issue_13735_original")  # unchanged
        self.assertIsNone(
            LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
        )


class HerdrAdoptOwnerRowWiringTest(unittest.TestCase):
    """The official ops method wiring, isolated home, synthetic inventory resolution."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.coord = Path(self._tmp.name) / "coord"
        self.coord.mkdir()
        self.worktree = str(Path(self._tmp.name) / "wt_lane")
        self.addCleanup(self._tmp.cleanup)

    def _ops(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )

        return HerdrSublaneActuatorOps(
            repo_root=self.coord,
            lane_label=LANE,
            issue=ISSUE,
            journal=JOURNAL,
            env={"MOZYO_BRIDGE_HOME": str(self.home)},
            runner=lambda *a, **k: None,
        )

    def _drive(self, *, adopted, slots):
        """Run the real ops method with the inventory resolution mocked to ``slots``."""
        ops = self._ops()
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False), \
                patch.object(type(ops), "_live_rows", return_value=[]), \
                patch.object(type(ops), "_launch_providers", return_value=PROVIDERS), \
                patch.object(
                    type(ops), "_resolve_lane_slots", return_value=(WS, LANE, slots)
                ):
            ops.declare_adopted_lane_lifecycle(self.worktree, adopted=adopted)

    def _owner(self):
        return resolve_lane_owner(WS, ISSUE, home=self.home)

    def test_official_adopt_path_backfills_the_owner_row(self) -> None:
        self._drive(adopted=True, slots=_slots())
        owner = self._owner()
        self.assertEqual(owner.status, OWNER_RESOLVED)
        self.assertEqual(owner.lane_id, LANE)

    def test_create_path_adopted_false_is_a_no_op(self) -> None:
        self._drive(adopted=False, slots=_slots())
        self.assertEqual(self._owner().status, OWNER_ABSENT)  # create declared via append

    def test_incomplete_inventory_writes_nothing_on_the_official_path(self) -> None:
        self._drive(adopted=True, slots={"codex": ("w1:pG", ("w1", "t1"))})
        self.assertEqual(self._owner().status, OWNER_ABSENT)


if __name__ == "__main__":
    unittest.main()
