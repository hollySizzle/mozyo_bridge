"""Regression pins for the #13845 hibernated bound live-zero terminal retire.

Redmine #13845 (parent #13490), live evidence #13810 j#79416. A hibernated / released
**BOUND** lifecycle row — the coordinator hibernated the lane, its process release completed
durably (``process_release`` reached ``released``), its issue is closed, worktree clean +
integrated, its live pair gone — but whose ``worktree_identity`` is **non-empty** (a #13754 /
#13809 / #13810-bound row that DID record its canonical worktree binding) is terminalized by
NO existing path:

- ``sublane retire --execute`` (Redmine #13754) attests the binding, then plans a close that
  finds nothing to close; a zero-close is only a retire when the durable row ALREADY says
  ``retired``, so it returns ``zero_close_unproven`` / ``closed: []`` /
  ``durable_retirement: ""`` forever (the j#79416 observation);
- ``--migrate-hibernated-legacy`` (Redmine #13841) requires an EMPTY ``worktree_identity`` —
  the defining legacy signature — so a bound row is refused there;
- ``--reconcile-hibernated-live`` (Redmine #13842) requires an empty binding AND targets the
  opposite liveness case (an exact pair observed live).

Re-launching a fresh pair only to close it again is the needless actuation the ticket forbids.
The metadata-only terminal retire moves such a row DIRECTLY to the #13689 terminal ``retired``
disposition through a bounded CAS — no process launch / close / resume, no worktree / branch
removal — while **preserving** the row's declared pins, worktree identity, and generation.

Two layers are pinned, both synthetic (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr inventory,
never the shared ``$HOME/.mozyo_bridge`` and never a live pane / process / route mutation):

1. the bounded store CAS guard matrix (``LaneBoundRetireStore``): the exact bound signature
   retires and preserves every other field; every off-signature shape (EMPTY binding, a
   MISMATCHED binding, active / superseded / already-retired disposition, unproven /
   in-flight release, pending replacement, different issue, project binding, revision race,
   absent row) is refused zero-write; and
2. the command boundary (``sublane retire --retire-hibernated-bound``): the JSON verdict +
   exit code over real roots, with the bound-worktree attestation, the live-inventory zero
   read, the **foreign-occupant fence** (review j#80115 F1), the head-integration probe,
   idempotent replay, and non-regression of the #13754 guarded close / #13841 migration
   (mutually exclusive, disjoint signatures).

The two inventory axes are deliberately pinned apart, because conflating them is exactly the
j#80115 F1 defect: ``expected_live_slots`` aggregates only the MANAGED roles, so "no expected
slot is live" does NOT mean "the unit is quiescent". A unit occupied solely by an unexpected
provider measures zero live and, before the fix, terminalized the row while that process kept
running.

Boundary (Redmine #13845): no process launch / close / resume, no worktree / branch removal,
no raw Herdr / tmux, no origin/main, no production / tag / publish.
"""

from __future__ import annotations

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

from mozyo_bridge.core.state.lane_bound_retire import (  # noqa: E402
    LaneBoundRetireStore,
)
from mozyo_bridge.core.state.lane_declaration import (  # noqa: E402
    LaneDeclarationStore,
)
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
    DecisionPointerError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    BINDING_KIND_PROJECT_GATEWAY,
    RECONCILE_PHASE_NONE,
    REPLACEMENT_REQUESTED,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_replacement import (  # noqa: E402
    LaneReplacementStore,
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
    REASON_INVENTORY_UNREADABLE,
    REASON_ISSUE_LANE_MISMATCH,
    REASON_NO_WORKTREE_ANCHOR,
    REASON_WORKTREE_BINDING_MISMATCH,
    REASON_WORKTREE_BINDING_UNVERIFIED,
    REASON_ZERO_CLOSE_UNPROVEN,
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_retire import (  # noqa: E402,E501
    BOUND_RETIRE_ALREADY_RETIRED,
    BOUND_RETIRE_BLOCKED,
    BOUND_RETIRE_DUPLICATE_INVENTORY,
    BOUND_RETIRE_EXPECTED_IDENTITY_UNRESOLVED,
    BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT,
    BOUND_RETIRE_HEAD_NOT_INTEGRATED,
    BOUND_RETIRE_LIVE_PAIR_PRESENT,
    BOUND_RETIRE_RETIRED,
    BOUND_RETIRE_WORKTREE_BRANCH_MISMATCH,
    HibernatedBoundRetireVerdict,
    format_bound_retire_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    derive_lane_workspace_token,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E402,E501
    SLOT_STALE,
    classify_named_slot,
)

_WORKSPACE_ID = "b3d17ac95e6f4802"
_LANE = "issue_13810_lifecycle_binding_generation"
_ISSUE = "13810"
_JOURNAL = "79416"
_OTHER_ISSUE = "13999"
#: The canonical worktree binding the bound row records (the defining #13845 signature).
_BOUND_WT = "wt_c0ffee1234abcd"
_OTHER_WT = "wt_deadbeef567890"


def _decision(issue: str = _ISSUE, journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _pins() -> tuple[ProcessGenerationPin, ...]:
    """The declared slot snapshot a #13810-bound row carries (preserved by the retire)."""
    return (
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "codex", _LANE),
            locator="w28:p3S",
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "claude", _LANE),
            locator="w28:p3T",
        ),
    )


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


def _seed_hibernated_released_bound(
    *,
    path: Path | None,
    key: LaneLifecycleKey,
    issue: str = _ISSUE,
    worktree_identity: str = _BOUND_WT,
    declared_slots=None,
    release_target: str = RELEASE_RELEASED,
) -> None:
    """Drive a row to hibernated + <release_target> via the REAL store transitions.

    ``worktree_identity`` defaults to the bound token (the #13845 signature; pass "" for the
    #13841 legacy shape). ``release_target`` selects how far the release generation got:
    ``released`` (the retirable proof), ``requested`` / ``partial`` (in flight — the
    release-not-proven fail-closed shapes).
    """
    dec = _decision(issue)
    lifecycle = LaneLifecycleStore(path=path)
    declaration = LaneDeclarationStore(path=path)
    slots = _pins() if declared_slots is None else declared_slots
    out = declaration.declare_lane(
        key,
        decision=dec,
        issue_id=issue,
        declared_slots=slots,
        worktree_identity=worktree_identity,
    )
    assert out.applied, f"seed declare_lane refused: {out.reason}"
    rec = lifecycle.get(key)
    lifecycle.transition_disposition(
        key,
        expected_disposition=DISPOSITION_ACTIVE,
        expected_revision=rec.revision,
        target=DISPOSITION_HIBERNATED,
        decision=dec,
    )
    rec = lifecycle.get(key)
    lifecycle.request_release(
        key,
        expected_revision=rec.revision,
        action_id="rel-1",
        pins=[
            ReleasePin("gateway", "codex-mzb1", "w28:p3S"),
            ReleasePin("worker", "claude-mzb1", "w28:p3T"),
        ],
    )
    if release_target == RELEASE_REQUESTED:
        return
    rec = lifecycle.get(key)
    lifecycle.record_release_outcome(
        key,
        action_id="rel-1",
        expected_revision=rec.revision,
        target=release_target,
    )


# ---------------------------------------------------------------------------
# 1. The bounded store CAS guard matrix (pure of the CLI).
# ---------------------------------------------------------------------------


class BoundRetireCasMatrix(unittest.TestCase):
    """``LaneBoundRetireStore.retire_released_hibernated_bound`` fail-closed matrix."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.sqlite"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.store = LaneLifecycleStore(path=self.path)
        self.bound = LaneBoundRetireStore(path=self.path)

    def _seed(self, **kwargs) -> None:
        _seed_hibernated_released_bound(path=self.path, key=self.key, **kwargs)

    def _retire(
        self,
        *,
        expected_revision=None,
        issue=_ISSUE,
        worktree_identity=_BOUND_WT,
        decision=None,
    ):
        rec = self.store.get(self.key)
        rev = (
            expected_revision
            if expected_revision is not None
            else (rec.revision if rec is not None else 1)
        )
        return self.bound.retire_released_hibernated_bound(
            self.key,
            expected_revision=rev,
            issue_id=issue,
            worktree_identity=worktree_identity,
            decision=decision if decision is not None else _decision(issue),
        )

    # -- the exact bound signature ---------------------------------------

    def test_exact_bound_signature_retires_to_terminal(self) -> None:
        self._seed()
        out = self._retire()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)
        self.assertEqual(rec.decision_journal, _JOURNAL)

    def test_declared_pins_worktree_and_generation_are_preserved(self) -> None:
        """The #13845 acceptance: a bound row keeps its pins / worktree identity."""
        self._seed()
        before = self.store.get(self.key)
        self.assertEqual(before.declared_pins, _pins())
        out = self._retire()
        self.assertTrue(out.applied)
        after = self.store.get(self.key)
        self.assertEqual(after.lane_disposition, DISPOSITION_RETIRED)
        # Every non-disposition axis survives the terminalization byte-identical.
        self.assertEqual(after.declared_pins, _pins())
        self.assertEqual(after.worktree_identity, _BOUND_WT)
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.process_release, RELEASE_RELEASED)
        self.assertEqual(after.binding_kind, before.binding_kind)
        self.assertEqual(after.issue_id, _ISSUE)
        # reconcile_phase stays empty: this is an ORDINARY terminal retire, not a #13842
        # reconcile-owed close, and the empty phase is what keeps the two distinguishable
        # (the #13842 review j#79320 R4 collision-proof invariant).
        self.assertEqual(after.reconcile_phase, RECONCILE_PHASE_NONE)

    # -- the worktree-binding axis (the inverse of #13841's) --------------

    def test_empty_worktree_binding_is_refused_and_left_for_13841(self) -> None:
        # The #13841 LEGACY signature. This surface must never terminalize it: that path is
        # --migrate-hibernated-legacy, whose own guards (empty binding) are the authority.
        self._seed(worktree_identity="")
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    def test_mismatched_worktree_binding_is_refused(self) -> None:
        # The row is bound to a DIFFERENT worktree: the caller's --worktree names another
        # lane's checkout, so the retire is refused rather than coerced.
        self._seed(worktree_identity=_OTHER_WT)
        out = self._retire(worktree_identity=_BOUND_WT)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(rec.worktree_identity, _OTHER_WT)

    def test_empty_worktree_token_argument_is_rejected(self) -> None:
        # An empty token is the #13841 signature, never a caller promise this surface accepts.
        self._seed()
        with self.assertRaises(ValueError):
            self._retire(worktree_identity="")
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    # -- the disposition axis --------------------------------------------

    def test_active_disposition_is_refused(self) -> None:
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity=_BOUND_WT,
        )
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_ACTIVE)

    def test_superseded_disposition_is_refused(self) -> None:
        recovery = LaneLifecycleKey(_WORKSPACE_ID, "issue_13810_recovery")
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity=_BOUND_WT,
        )
        rec = self.store.get(self.key)
        self.store.supersede_and_activate(
            superseded=self.key,
            expected_revision=rec.revision,
            recovery=recovery,
            decision=_decision(),
        )
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_SUPERSEDED
        )
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_already_retired_row_is_refused_by_the_cas(self) -> None:
        # Idempotency is the CALLER's (a live-zero-verified no-op), never a second write:
        # this CAS stays strictly hibernated -> retired.
        self._seed()
        self.assertTrue(self._retire().applied)
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_project_gateway_binding_is_refused(self) -> None:
        key = LaneLifecycleKey(_WORKSPACE_ID, "project_gateway_lane")
        LaneDeclarationStore(path=self.path).declare_lane(
            key,
            decision=_decision(),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope="giken-3800-mozyo-bridge",
            declared_slots=_pins(),
            worktree_identity=_BOUND_WT,
        )
        rec = self.store.get(key)
        out = self.bound.retire_released_hibernated_bound(
            key,
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    # -- the issue / decision axis ---------------------------------------

    def test_different_issue_is_refused(self) -> None:
        self._seed(issue=_ISSUE)
        out = self._retire(issue=_OTHER_ISSUE, decision=_decision(_OTHER_ISSUE))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    def test_empty_issue_argument_is_rejected(self) -> None:
        self._seed()
        with self.assertRaises(ValueError):
            self._retire(issue="")

    def test_decision_anchored_to_another_issue_is_rejected(self) -> None:
        self._seed()
        with self.assertRaises(DecisionPointerError):
            self._retire(issue=_ISSUE, decision=_decision(_OTHER_ISSUE))
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    # -- the release / replacement axis ----------------------------------

    def test_release_not_requested_is_refused(self) -> None:
        dec = _decision()
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=dec,
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity=_BOUND_WT,
        )
        rec = self.store.get(self.key)
        self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=dec,
        )
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_release_in_flight_requested_is_refused(self) -> None:
        self._seed(release_target=RELEASE_REQUESTED)
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_release_partial_is_refused(self) -> None:
        self._seed(release_target=RELEASE_PARTIAL)
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_hibernating_with_an_open_replacement_is_already_impossible(self) -> None:
        """The rails never MAKE a hibernated row with an unsettled replacement.

        Pinned because it is what bounds the reachability of the CAS's
        ``replacement_settled`` guard: ``request_replacement`` requires an ``active`` owner,
        and ``transition_disposition`` refuses ``active -> hibernated`` while the replacement
        is unsettled. So the shape the guard refuses cannot be reached through the public
        lifecycle rails at all — the guard below is defense in depth, not a live path.
        """
        dec = _decision()
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=dec,
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity=_BOUND_WT,
        )
        rec = self.store.get(self.key)
        opened = LaneReplacementStore(path=self.path).request_replacement(
            self.key,
            expected_revision=rec.revision,
            action_id="repl-1",
            pins=[ReleasePin("worker", "claude-mzb1", "w28:p3T")],
            decision=dec,
        )
        self.assertTrue(opened.applied)
        rec = self.store.get(self.key)
        self.assertEqual(rec.replacement_state, REPLACEMENT_REQUESTED)
        blocked = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=dec,
        )
        self.assertFalse(blocked.applied)
        self.assertEqual(blocked.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_ACTIVE)

    def test_pending_replacement_is_refused_even_off_rail(self) -> None:
        """The ``replacement_settled`` guard bites on a row the rails cannot produce.

        The sibling surfaces (#13841 / #13842) carry the same guard with no test, so its
        refusal has never actually been observed. Since the rails cannot build the shape
        (see the test above), construct it directly at the storage layer — a hibernated /
        released / bound row whose replacement is stuck ``requested`` — and prove the CAS
        refuses it zero-write rather than terminalizing a lane with a receiver swap in
        flight. This is the only route to the guard, so without this the guard would be
        untested code asserting an unverified claim.
        """
        self._seed()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "UPDATE lane_lifecycle_records SET replacement_state = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ?",
                (REPLACEMENT_REQUESTED, _WORKSPACE_ID, _LANE),
            )
            conn.commit()
        finally:
            conn.close()
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(rec.process_release, RELEASE_RELEASED)
        self.assertEqual(rec.replacement_state, REPLACEMENT_REQUESTED)
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    # -- the revision fence ----------------------------------------------

    def test_revision_race_is_refused(self) -> None:
        self._seed()
        rec = self.store.get(self.key)
        out = self._retire(expected_revision=rec.revision - 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    def test_absent_row_is_refused(self) -> None:
        out = self._retire(expected_revision=1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_duplicate_replay_at_the_same_revision_loses(self) -> None:
        self._seed()
        rec = self.store.get(self.key)
        first = self.bound.retire_released_hibernated_bound(
            self.key,
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertTrue(first.applied)
        second = self.bound.retire_released_hibernated_bound(
            self.key,
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertFalse(second.applied)
        self.assertEqual(second.reason, CAS_STALE_REVISION)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_RETIRED)


class BoundRetireDoesNotErodeSiblings(unittest.TestCase):
    """#13845 and #13841 cover disjoint shapes; neither admits the other's row."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.sqlite"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.store = LaneLifecycleStore(path=self.path)
        self.bound = LaneBoundRetireStore(path=self.path)
        self.legacy = LaneRetireMigrationStore(path=self.path)

    def test_13841_still_refuses_a_bound_row(self) -> None:
        # Non-regression: the #13841 empty-binding guard is untouched by #13845.
        _seed_hibernated_released_bound(
            path=self.path, key=self.key, worktree_identity=_BOUND_WT
        )
        out = self.legacy.retire_released_hibernated_legacy(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            issue_id=_ISSUE,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(
            self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED
        )

    def test_bound_row_with_empty_pins_is_still_terminalizable(self) -> None:
        """A bound row whose ``declared_slots`` are empty is in scope when live is zero.

        The defining #13845 signature is the **worktree** binding, not the pin snapshot, so a
        bound-but-pinless row (the #13809 "pins-only gap" shape) terminalizes here rather than
        staying stuck forever. This does NOT collide with #13879 (bound ∧ pins absent ∧ live
        **non-empty**): that shape is refused by this surface's live-zero read, and #13879
        names live-zero terminalization as an explicit non-goal. The two partition on liveness.
        """
        _seed_hibernated_released_bound(
            path=self.path,
            key=self.key,
            worktree_identity=_BOUND_WT,
            declared_slots=(),
        )
        self.assertEqual(self.store.get(self.key).declared_pins, ())
        out = self.bound.retire_released_hibernated_bound(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertTrue(out.applied)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)
        self.assertEqual(rec.worktree_identity, _BOUND_WT)

    def test_13845_refuses_the_legacy_row_13841_migrates(self) -> None:
        # The exact same row: #13845 refuses it, #13841 migrates it. The two signatures
        # partition the hibernated / released live-zero space; neither erodes the other.
        _seed_hibernated_released_bound(
            path=self.path, key=self.key, worktree_identity="", declared_slots=()
        )
        refused = self.bound.retire_released_hibernated_bound(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertFalse(refused.applied)
        self.assertEqual(refused.reason, CAS_UNEXPECTED_STATE)
        migrated = self.legacy.retire_released_hibernated_legacy(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            issue_id=_ISSUE,
            decision=_decision(),
        )
        self.assertTrue(migrated.applied)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_RETIRED)


# ---------------------------------------------------------------------------
# 2. The command boundary: `sublane retire --retire-hibernated-bound`.
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
                    "created_at": "2026-07-16T00:00:00+00:00",
                    "updated_at": "2026-07-16T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "base", cwd=root)


class BoundRetireCommandTests(unittest.TestCase):
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
        # The canonical worktree token the lane's row must be bound to: derived the same way
        # the create site records it, from the lane worktree's resolved path.
        self.bound_token = derive_lane_workspace_token(
            str(self.lane_worktree.resolve())
        )

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        # A fake herdr inventory: the coordinator's default-lane pair only (never a lane
        # slot) — so the lane unit measures ZERO live managed slots by default, which is the
        # #13845 live-zero shape.
        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
        ]
        self.rows_error: Exception | None = None
        self._real_rows = sublane_herdr_projection.list_herdr_agent_rows
        self._real_execute = sublane_herdr_retire.execute_herdr_retire_close
        self.executed_closes: list[tuple[str, str]] = []

        def fake_rows(env):
            if self.rows_error is not None:
                raise self.rows_error
            return list(self.rows)

        def fake_execute(plan, **kwargs):
            # No real herdr binary in the test env. The bound retire must NEVER reach this:
            # any call here is a boundary violation (it is metadata-only by contract), and
            # the #13754 non-regression test asserts the guarded close still does.
            closed = []
            for role, locator in plan.close_targets:
                self.rows = [r for r in self.rows if r["pane_id"] != locator]
                closed.append((role, locator))
                self.executed_closes.append((role, locator))
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
        kwargs.setdefault("worktree_identity", self.bound_token)
        _seed_hibernated_released_bound(path=None, key=self._key(), **kwargs)

    def _record(self):
        return LaneLifecycleStore().get(self._key())

    def _disposition(self) -> str:
        rec = self._record()
        return "" if rec is None else rec.lane_disposition

    def _add_live_pair(self) -> None:
        """Put the lane unit's exact managed pair back in the live inventory."""
        self.rows.extend(
            [
                _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3S"),
                _row(_WORKSPACE_ID, "claude", _LANE, "w28:p3T"),
            ]
        )

    def _lane_ahead_of_main(self) -> None:
        """Advance the lane branch past main so its head is NOT integrated."""
        (self.lane_worktree / "wip.txt").write_text("wip\n", encoding="utf-8")
        _git("add", "-A", cwd=self.lane_worktree)
        _git("commit", "-m", "lane wip", cwd=self.lane_worktree)

    def _retire(
        self,
        *,
        repo: Path | None = None,
        worktree: Path | None = "__lane__",
        issue: str = _ISSUE,
        branch: str = _LANE,
        integration_branch: str = "main",
        preflight_green: bool = True,
        also_execute: bool = False,
        also_migrate: bool = False,
        bound: bool = True,
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
            migrate_hibernated_legacy=also_migrate,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=bound,
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

    def _bound(self, payload) -> dict:
        return payload.get("hibernated_bound_retire", {})

    # -- the green path ---------------------------------------------------

    def test_bound_live_zero_row_terminalizes_and_exits_zero(self) -> None:
        self._seed_row()
        code, payload = self._retire()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._bound(payload)["state"], BOUND_RETIRE_RETIRED)
        self.assertTrue(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # Metadata only: the guarded close was never reached.
        self.assertEqual(self.executed_closes, [])

    def test_green_path_preserves_pins_and_binding_through_the_command(self) -> None:
        self._seed_row()
        code, _ = self._retire()
        self.assertEqual(code, 0)
        rec = self._record()
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)
        self.assertEqual(rec.declared_pins, _pins())
        self.assertEqual(rec.worktree_identity, self.bound_token)
        self.assertEqual(rec.reconcile_phase, RECONCILE_PHASE_NONE)

    def test_idempotent_replay_is_a_verified_noop(self) -> None:
        self._seed_row()
        self.assertEqual(self._retire()[0], 0)
        code, payload = self._retire()
        self.assertEqual(code, 0)
        self.assertEqual(self._bound(payload)["state"], BOUND_RETIRE_ALREADY_RETIRED)
        self.assertEqual(self.executed_closes, [])

    def test_replay_with_a_relaunched_live_pair_fails_closed(self) -> None:
        """A persisted ``retired`` never reports success while a pair is live again.

        The #13841 review j#79150 F2 invariant, carried into #13845: the live-zero read runs
        BEFORE the idempotent already-retired success.
        """
        self._seed_row()
        self.assertEqual(self._retire()[0], 0)
        self._add_live_pair()
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(self._bound(payload)["state"], BOUND_RETIRE_BLOCKED)
        self.assertEqual(self._bound(payload)["reason"], BOUND_RETIRE_LIVE_PAIR_PRESENT)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self.executed_closes, [])

    # -- the live-zero axis ----------------------------------------------

    def test_live_pair_present_blocks_zero_write(self) -> None:
        self._seed_row()
        self._add_live_pair()
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(self._bound(payload)["reason"], BOUND_RETIRE_LIVE_PAIR_PRESENT)
        self.assertTrue(self._bound(payload)["expected_live"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    # -- the foreign-occupant axis (review j#80115 F1) --------------------

    def test_foreign_only_live_inventory_blocks_zero_write(self) -> None:
        """A unit occupied ONLY by an unexpected provider must not terminalize (j#80115 F1).

        The regression for the review's reproduction: ``expected_live_slots`` aggregates only
        the MANAGED roles, so a foreign-only unit measures zero live. Before the fix this
        exited 0 and recorded the row ``retired`` while the foreign process kept running.
        Distinct from ``test_foreign_worktree_binding_blocks_zero_write``, which covers the
        foreign *worktree binding* axis — this is the foreign *provider inventory* axis.
        """
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "gemini", _LANE, "w28:pFOREIGN"))
        code, payload = self._retire()
        self.assertEqual(code, 1)
        verdict = self._bound(payload)
        self.assertEqual(verdict["state"], BOUND_RETIRE_BLOCKED)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT)
        # The measurement that refused is named: zero managed slots live, yet NOT quiescent.
        self.assertEqual(verdict["expected_live"], [])
        self.assertTrue(verdict["foreign_names"])
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_foreign_occupant_alongside_live_pair_blocks(self) -> None:
        # Both axes non-empty: still zero-write (the live check fires first, and the foreign
        # occupants are reported alongside it rather than dropped).
        self._seed_row()
        self._add_live_pair()
        self.rows.append(_row(_WORKSPACE_ID, "gemini", _LANE, "w28:pFOREIGN"))
        code, payload = self._retire()
        self.assertEqual(code, 1)
        verdict = self._bound(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_LIVE_PAIR_PRESENT)
        self.assertTrue(verdict["foreign_names"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_foreign_occupant_blocks_the_idempotent_replay_too(self) -> None:
        """An already-retired row must not report success while a foreign occupant runs.

        Same shape as the #13841 review j#79150 F2 invariant for relaunched pairs: a persisted
        ``retired`` does not prove the unit is quiescent now, so the replay is only a verified
        no-op once the unit is measured empty.
        """
        self._seed_row()
        self.assertEqual(self._retire()[0], 0)
        self.rows.append(_row(_WORKSPACE_ID, "gemini", _LANE, "w28:pFOREIGN"))
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(
            self._bound(payload)["reason"], BOUND_RETIRE_FOREIGN_INVENTORY_PRESENT
        )
        self.assertFalse(payload["retire_ok"])

    def test_foreign_occupant_in_another_lane_does_not_block(self) -> None:
        # The fence is scoped to the TARGETED units: a foreign provider sitting in a different
        # lane's unit is none of this retire's business and must not block it.
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "gemini", "issue_99999_other_lane", "w28:pOTHER"))
        code, payload = self._retire()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._bound(payload)["state"], BOUND_RETIRE_RETIRED)
        self.assertEqual(self._bound(payload)["foreign_names"], [])
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_coordinator_default_lane_pair_is_not_foreign(self) -> None:
        # Non-regression for the green path: the project workspace's default-lane coordinator
        # pair is always in the inventory and must never be read as a foreign occupant of the
        # lane unit (that would make every terminal retire permanently blocked).
        self._seed_row()
        code, payload = self._retire()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._bound(payload)["foreign_names"], [])
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- the duplicate / unresolved-identity axes (review j#80148 R2-F1) --

    def test_duplicate_locatorless_expected_rows_block_zero_write(self) -> None:
        """Two rows claiming the same expected role is a corrupt inventory (j#80148 R2-F1).

        Before the fix this exited 0 and terminalized: ``expected_live_slots`` skips
        locator-less rows AND collapses roles into a set, so the duplicate measured as zero
        live. A herdr assigned name is unique by construction — this is ambiguity, and no
        reading of an ambiguous inventory can license a terminal write.
        """
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, ""))
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, ""))
        code, payload = self._retire()
        self.assertEqual(code, 1)
        verdict = self._bound(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_DUPLICATE_INVENTORY)
        self.assertEqual(verdict["expected_live"], [])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_duplicate_expected_rows_with_locators_name_the_duplicate(self) -> None:
        # The duplicate check runs BEFORE the live read: such a unit was already blocked, but
        # as `live_pair_present`, which names the wrong problem. The inventory itself is
        # unsound, and that is what the operator must be told.
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, "w28:pA"))
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, "w28:pB"))
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(
            self._bound(payload)["reason"], BOUND_RETIRE_DUPLICATE_INVENTORY
        )
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_single_locatorless_expected_row_blocks_zero_write(self) -> None:
        """A minimal locator-less row is "cannot resolve", not "absent" (j#80148 R2-F1).

        The shared `classify_named_slot` reads a row with neither a detected-agent field nor a
        status field as LIVE, and `herdr_target_resolution` refuses to send to a row with no
        locator (`missing_locator`). Terminalizing off it would rest on the absence of proof
        of liveness rather than proof of absence.
        """
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "codex", _LANE, ""))
        code, payload = self._retire()
        self.assertEqual(code, 1)
        verdict = self._bound(payload)
        self.assertEqual(
            verdict["reason"], BOUND_RETIRE_EXPECTED_IDENTITY_UNRESOLVED
        )
        self.assertEqual(verdict["expected_live"], [])
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_positively_stale_locatorless_row_does_not_block(self) -> None:
        """Residue the liveness contract POSITIVELY calls dead must not block forever.

        The other half of the fence: proceeding requires positive proof of deadness, and a row
        whose detected-agent field is present-but-blank is exactly that (herdr reports the pane
        carries no managed agent). Blocking it would recreate this ticket's own defect — a lane
        stuck un-terminalizable — in a new shape.
        """
        self._seed_row()
        residue = _row(_WORKSPACE_ID, "codex", _LANE, "")
        residue["agent"] = ""  # present-but-blank == the positive shell-residue signal
        self.assertEqual(classify_named_slot(residue), SLOT_STALE)
        self.rows.append(residue)
        code, payload = self._retire()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._bound(payload)["state"], BOUND_RETIRE_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        self.assertEqual(self.executed_closes, [])

    def test_locatorless_row_in_another_lane_does_not_block(self) -> None:
        # Scoping: the fences read only the TARGETED units.
        self._seed_row()
        self.rows.append(_row(_WORKSPACE_ID, "codex", "issue_99999_other_lane", ""))
        self.rows.append(_row(_WORKSPACE_ID, "codex", "issue_99999_other_lane", ""))
        code, payload = self._retire()
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._bound(payload)["state"], BOUND_RETIRE_RETIRED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_unreadable_inventory_is_not_an_empty_one(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        self._seed_row()
        self.rows_error = HerdrSessionStartError("herdr unavailable")
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(self._bound(payload)["reason"], REASON_INVENTORY_UNREADABLE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    # -- the bound-worktree attestation axis ------------------------------

    def test_empty_binding_row_is_routed_to_13841_not_terminalized(self) -> None:
        # The #13841 legacy shape reaching THIS flag: the #13754 attestation fails closed on
        # the empty binding, so nothing is written and the operator is pointed at the right
        # surface rather than silently terminalized here.
        self._seed_row(worktree_identity="", declared_slots=())
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(
            self._bound(payload)["reason"], REASON_WORKTREE_BINDING_UNVERIFIED
        )
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_foreign_worktree_binding_blocks_zero_write(self) -> None:
        # The row is bound to a DIFFERENT lane's worktree token.
        self._seed_row(worktree_identity=_OTHER_WT)
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(
            self._bound(payload)["reason"], REASON_WORKTREE_BINDING_MISMATCH
        )
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_issue_lane_mismatch_blocks_zero_write(self) -> None:
        self._seed_row()
        code, payload = self._retire(issue=_OTHER_ISSUE)
        self.assertEqual(code, 1)
        self.assertEqual(self._bound(payload)["reason"], REASON_ISSUE_LANE_MISMATCH)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_missing_worktree_anchor_blocks(self) -> None:
        self._seed_row()
        code, payload = self._retire(worktree=None)
        self.assertEqual(code, 1)
        self.assertEqual(self._bound(payload)["reason"], REASON_NO_WORKTREE_ANCHOR)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    # -- the branch / integration axes ------------------------------------

    def test_branch_mismatch_blocks_zero_write(self) -> None:
        self._seed_row()
        code, payload = self._retire(branch="some_other_branch")
        self.assertEqual(code, 1)
        self.assertEqual(
            self._bound(payload)["reason"], BOUND_RETIRE_WORKTREE_BRANCH_MISMATCH
        )
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_unintegrated_head_blocks_zero_write(self) -> None:
        self._seed_row()
        self._lane_ahead_of_main()
        code, payload = self._retire()
        self.assertEqual(code, 1)
        self.assertEqual(
            self._bound(payload)["reason"], BOUND_RETIRE_HEAD_NOT_INTEGRATED
        )
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    # -- the preflight gate + intent exclusivity --------------------------

    def test_red_preflight_never_runs_the_bound_retire(self) -> None:
        self._seed_row()
        code, payload = self._retire(preflight_green=False)
        self.assertEqual(code, 1)
        self.assertNotIn("hibernated_bound_retire", payload)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_bound_retire_with_execute_is_a_zero_write_error(self) -> None:
        self._seed_row()
        code, raw = self._retire(also_execute=True, json_out=False)
        self.assertEqual(code, 1)
        self.assertEqual(raw, "")  # nothing ran: no preflight, no actuation
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_bound_retire_with_migrate_is_a_zero_write_error(self) -> None:
        self._seed_row()
        code, raw = self._retire(also_migrate=True, json_out=False)
        self.assertEqual(code, 1)
        self.assertEqual(raw, "")
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    # -- non-regression of the #13754 guarded close -----------------------

    def test_13754_guarded_close_still_blocks_the_bound_live_zero_row(self) -> None:
        """The gap #13845 exists to close: --execute alone still cannot terminalize it.

        This pins the j#79416 observation as the *unchanged* behaviour of --execute: the
        bound live-zero row is a zero-close it cannot prove, so it stays blocked. #13845 adds
        a new surface; it does not weaken the guarded close's fence.
        """
        self._seed_row()
        code, payload = self._retire(bound=False, also_execute=True)
        self.assertEqual(code, 1)
        close = payload["herdr_retire_close"]
        self.assertEqual(close["reason"], REASON_ZERO_CLOSE_UNPROVEN)
        self.assertEqual(close["closed"], [])
        self.assertEqual(close["durable_retirement"], "")
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_13754_guarded_close_still_closes_a_live_pair(self) -> None:
        # Non-regression: --execute's real close path is untouched by #13845.
        self._seed_row()
        self._add_live_pair()
        code, payload = self._retire(bound=False, also_execute=True)
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(len(self.executed_closes), 2)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)


class BoundRetireTextRendering(unittest.TestCase):
    """The text surface leads with the verdict (no "retired" for a blocked run)."""

    def test_blocked_says_not_retired_and_nothing_written(self) -> None:
        text = format_bound_retire_text(
            HibernatedBoundRetireVerdict(
                state=BOUND_RETIRE_BLOCKED,
                reason=BOUND_RETIRE_LIVE_PAIR_PRESENT,
                detail="expected managed slot(s) are still live",
                workspace_id=_WORKSPACE_ID,
                lane_id=_LANE,
                expected_live=("codex", "claude"),
            )
        )
        self.assertIn(BOUND_RETIRE_BLOCKED, text)
        self.assertIn(BOUND_RETIRE_LIVE_PAIR_PRESENT, text)
        self.assertIn("lane NOT retired", text)
        self.assertIn("no lane-row write and no schema migration", text)
        self.assertIn("codex, claude", text)

    def test_retired_verdict_renders_the_terminal_state(self) -> None:
        text = format_bound_retire_text(
            HibernatedBoundRetireVerdict(
                state=BOUND_RETIRE_RETIRED,
                detail="metadata only",
                workspace_id=_WORKSPACE_ID,
                lane_id=_LANE,
            )
        )
        self.assertIn(BOUND_RETIRE_RETIRED, text)
        self.assertNotIn("fail-closed", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
