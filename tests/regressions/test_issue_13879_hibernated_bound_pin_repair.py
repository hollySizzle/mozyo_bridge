"""Regression pins for the #13879 hibernated bound declared-pin repair.

Redmine #13879 (parent #13490), live evidence #13846 j#79915. A hibernated / released
**BOUND** lifecycle row (rev4 / gen1, ``worktree_identity`` present) whose ``declared_slots``
snapshot is **empty**, while the lane's exact managed pair is observed **live**, can be
repaired by NO existing path — so ``sublane recover-pair`` (#13847), which requires the
declared pins, fails ``hibernated_record_missing_pins`` forever:

- ``backfill_active_binding`` (Redmine #13809) fills exactly this pins-only gap, but only on an
  **active** row;
- ``retire_released_hibernated_legacy`` (#13841) requires an EMPTY ``worktree_identity`` and
  terminalizes;
- ``retire_reconciled_hibernated_legacy`` (#13842) requires an EMPTY binding AND empty pins, and
  its actuation is a retire-first close;
- ``retire_released_hibernated_bound`` (#13845) matches the bound signature but targets the
  live-zero case and terminalizes.

The metadata-only repair fills ONLY the empty pin snapshot from the exact live, unique, idle,
composer-settled, generation-bound-attested pair, under an exact ``(revision, generation)`` CAS
— then the lane stays hibernated and recover-pair's preflight may proceed. It deliberately does
NOT weaken recover-pair's declared-pins precondition (#13847 owns it).

Two layers are pinned, both synthetic (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr inventory,
never the shared ``$HOME/.mozyo_bridge`` and never a live pane / process / route mutation):

1. the bounded store CAS guard matrix (``LanePinRepairStore``): the exact signature fills the
   pins and **preserves every other field**; every off-signature shape (active / superseded /
   retired disposition, EMPTY binding, a MISMATCHED binding, unproven / in-flight release,
   pending replacement, different issue, project binding, revision race, **generation race**, an
   already-pinned row, an absent row) is refused zero-write; and byte-equal replay is idempotent
   while a DIVERGENT snapshot is never overwritten;
2. the command boundary (``sublane repair-pins``): the JSON verdict + exit code, driving the
   pair observation through a fake ``ReconcileOps`` so every acceptance-1 axis (unreadable
   inventory, foreign provider, partial pair, duplicate name, stale residue, missing locator,
   unattested identity, not-idle agent, pending composer) is pinned as a zero-write refusal, plus
   preflight / replay / divergence and the **metadata-only** boundary.

The signatures of #13842 (worktree **empty**) and #13879 (worktree **non-empty AND matching**)
are mutually exclusive by construction, so no row is ever a target of both — pinned directly in
``PinRepairDoesNotErodeSiblings``.

Boundary (Redmine #13879): no process launch / close / resume / send, no worktree / branch
removal, no raw Herdr / tmux, no origin/main, no production / tag / publish.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Optional, Sequence

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_declaration import (  # noqa: E402
    LaneDeclarationStore,
)
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    CAS_ALREADY_DECLARED,
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
    CAS_GENERATION_MISMATCH,
    RECONCILE_PHASE_NONE,
    REPLACEMENT_REQUESTED,
    ProcessGenerationPin,
    encode_declared_slots,
)
from mozyo_bridge.core.state.lane_pin_repair import (  # noqa: E402
    LanePinRepairStore,
)
from mozyo_bridge.core.state.lane_reconcile_binding import (  # noqa: E402
    LaneReconcileBindingStore,
)
from mozyo_bridge.core.state.lane_replacement import (  # noqa: E402
    LaneReplacementStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_herdr_projection as herdr_projection,
    workflow_provider_resolution as provider_resolution,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    herdr_session_start as session_start,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (  # noqa: E402,E501
    herdr_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    REASON_INVENTORY_UNREADABLE,
    REASON_NO_WORKTREE_ANCHOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pin_repair import (  # noqa: E402,E501
    REPAIR_ALREADY,
    REPAIR_BLOCKED,
    REPAIR_LIVE_PAIR_ABSENT,
    REPAIR_NOT_REPAIRABLE_STATE,
    REPAIR_PINS_DIVERGENT,
    REPAIR_RELEASE_NOT_PROVEN,
    REPAIR_REPAIRABLE,
    REPAIR_REPAIRED,
    PinRepairVerdict,
    format_pin_repair_text,
    run_hibernated_pin_repair,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_hibernated_pair_recovery as recover_pair,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.pair_launch_attestation import (  # noqa: E402,E501
    GATEWAY_ROLE as RECOVER_GATEWAY_ROLE,
    WORKER_ROLE as RECOVER_WORKER_ROLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E402,E501
    GATEWAY_ROLE as LEGACY_GATEWAY_ROLE,
    WORKER_ROLE as LEGACY_WORKER_ROLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_hibernated_live_reconcile import (  # noqa: E402,E501
    RECON_AGENT_NOT_IDLE,
    RECON_FOREIGN_PROVIDER,
    RECON_IDENTITY_UNATTESTED,
    RECON_PAIR_AMBIGUOUS,
    RECON_PAIR_INCOMPLETE,
    RECON_PENDING_COMPOSER,
    RECON_SLOT_STALE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E402,E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E402,E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_WORKSPACE_ID = "b3d17ac95e6f4802"
_LANE = "issue_13879_hibernated_pin_repair"
_ISSUE = "13879"
_JOURNAL = "79919"
_OTHER_ISSUE = "13999"
#: The canonical worktree binding the bound row records (the defining #13879 signature).
_BOUND_WT = "wt_c0ffee1234abcd"
_OTHER_WT = "wt_deadbeef567890"
_GW_PROVIDER = "codex"
_WK_PROVIDER = "claude"
_GW_LOC = "w28:p4J"
_WK_LOC = "w28:p4K"


def _decision(issue: str = _ISSUE, journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _gw_name() -> str:
    return encode_assigned_name(_WORKSPACE_ID, _GW_PROVIDER, _LANE)


def _wk_name() -> str:
    return encode_assigned_name(_WORKSPACE_ID, _WK_PROVIDER, _LANE)


def _pins(gw: str = _GW_LOC, wk: str = _WK_LOC) -> tuple[ProcessGenerationPin, ...]:
    """The observed live pair the repair fills the empty snapshot with."""
    return (
        ProcessGenerationPin(
            role="gateway",
            provider=_GW_PROVIDER,
            assigned_name=_gw_name(),
            locator=gw,
        ),
        ProcessGenerationPin(
            role="worker",
            provider=_WK_PROVIDER,
            assigned_name=_wk_name(),
            locator=wk,
        ),
    )


def _seed_hibernated_released_bound(
    *,
    path: Path | None,
    key: LaneLifecycleKey,
    issue: str = _ISSUE,
    worktree_identity: str = _BOUND_WT,
    declared_slots=(),
    release_target: str = RELEASE_RELEASED,
) -> None:
    """Drive a row to hibernated + <release_target> via the REAL store transitions.

    ``declared_slots`` defaults to EMPTY — the #13879 pins-only gap this surface repairs.
    ``worktree_identity`` defaults to the bound token (pass "" for the #13841 / #13842 legacy
    shape). ``release_target`` selects how far the release generation got.
    """
    dec = _decision(issue)
    lifecycle = LaneLifecycleStore(path=path)
    declaration = LaneDeclarationStore(path=path)
    out = declaration.declare_lane(
        key,
        decision=dec,
        issue_id=issue,
        declared_slots=declared_slots,
        worktree_identity=worktree_identity,
    )
    assert out.applied, f"seed declare_lane refused: {out.reason}"
    rec = lifecycle.get(key)
    # Every seed transition is asserted: a silently-refused seed would leave the row in a
    # DIFFERENT shape than the test names, and the assertion under test would then pass or fail
    # for a reason the test never states (the harness must not lie about what it built).
    out = lifecycle.transition_disposition(
        key,
        expected_disposition=DISPOSITION_ACTIVE,
        expected_revision=rec.revision,
        target=DISPOSITION_HIBERNATED,
        decision=dec,
    )
    assert out.applied, f"seed hibernate refused: {out.reason}"
    rec = lifecycle.get(key)
    out = lifecycle.request_release(
        key,
        expected_revision=rec.revision,
        action_id="rel-1",
        pins=[
            ReleasePin("gateway", _gw_name(), "w28:p3S"),
            ReleasePin("worker", _wk_name(), "w28:p3T"),
        ],
    )
    assert out.applied, f"seed request_release refused: {out.reason}"
    if release_target == RELEASE_REQUESTED:
        return
    rec = lifecycle.get(key)
    out = lifecycle.record_release_outcome(
        key,
        action_id="rel-1",
        expected_revision=rec.revision,
        target=release_target,
    )
    assert out.applied, f"seed record_release_outcome refused: {out.reason}"


# ---------------------------------------------------------------------------
# 1. The bounded store CAS guard matrix (pure of the CLI).
# ---------------------------------------------------------------------------


class PinRepairCasMatrix(unittest.TestCase):
    """``LanePinRepairStore.repair_hibernated_bound_pins`` writes ONLY on the exact signature."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "lane_lifecycle.sqlite3"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.store = LanePinRepairStore(path=self.path)
        self.lifecycle = LaneLifecycleStore(path=self.path)

    def _seed(self, **kw) -> None:
        _seed_hibernated_released_bound(path=self.path, key=self.key, **kw)

    def _rec(self):
        return self.lifecycle.get(self.key)

    def _repair(self, **overrides):
        rec = self._rec()
        kw = dict(
            expected_revision=rec.revision if rec else 1,
            expected_generation=rec.lane_generation if rec else 1,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            declared_slots=_pins(),
            decision=_decision(),
        )
        kw.update(overrides)
        return self.store.repair_hibernated_bound_pins(self.key, **kw)

    # -- the one shape that writes -------------------------------------------

    def test_exact_signature_fills_empty_pins(self) -> None:
        self._seed()
        before = self._rec()
        self.assertEqual(before.declared_slots, "", "seed must have the pins-only gap")
        out = self._repair()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        after = self._rec()
        self.assertEqual(after.declared_slots, encode_declared_slots(_pins()))
        self.assertEqual(after.revision, before.revision + 1)

    def test_repair_preserves_every_other_axis(self) -> None:
        """Metadata-only (acceptance 3): pins are the ONLY row field the repair writes."""
        self._seed()
        before = self._rec()
        self.assertTrue(self._repair().applied)
        after = self._rec()
        self.assertEqual(after.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(after.lane_disposition, before.lane_disposition)
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.worktree_identity, before.worktree_identity)
        self.assertEqual(after.process_release, before.process_release)
        self.assertEqual(after.replacement_state, before.replacement_state)
        self.assertEqual(after.release_pins, before.release_pins)
        self.assertEqual(after.binding_kind, before.binding_kind)
        self.assertEqual(after.issue_id, before.issue_id)
        # An ordinary repaired row must stay distinguishable from a #13842 reconcile-owed close.
        self.assertEqual(after.reconcile_phase, RECONCILE_PHASE_NONE)

    def test_decision_anchor_is_recorded(self) -> None:
        self._seed()
        self.assertTrue(self._repair(decision=_decision(journal="80999")).applied)
        after = self._rec()
        self.assertEqual(after.decision_journal, "80999")
        self.assertEqual(after.decision_issue_id, _ISSUE)

    # -- replay (acceptance 4) ------------------------------------------------

    def test_byte_equal_replay_is_idempotent_and_writes_nothing(self) -> None:
        self._seed()
        self.assertTrue(self._repair().applied)
        filled = self._rec()
        out = self._repair()
        self.assertTrue(out.applied, "a byte-equal replay is an idempotent success")
        self.assertEqual(out.reason, CAS_APPLIED)
        self.assertEqual(
            out.revision,
            filled.revision,
            "an idempotent replay must not bump the revision (it writes nothing)",
        )
        self.assertEqual(self._rec().updated_at, filled.updated_at)

    def test_divergent_pins_are_never_overwritten(self) -> None:
        """A recycled generation (different live locators) is zero-write, not an overwrite."""
        self._seed()
        self.assertTrue(self._repair().applied)
        filled = self._rec()
        out = self._repair(declared_slots=_pins(gw="w99:p1", wk="w99:p2"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self._rec().declared_slots, filled.declared_slots)

    def test_foreign_provider_snapshot_is_never_overwritten(self) -> None:
        self._seed()
        foreign = (
            ProcessGenerationPin(
                role="gateway",
                provider="someone_else",
                assigned_name=encode_assigned_name(_WORKSPACE_ID, "someone_else", _LANE),
                locator=_GW_LOC,
            ),
        )
        self.assertTrue(self._repair(declared_slots=foreign).applied)
        filled = self._rec()
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self._rec().declared_slots, filled.declared_slots)

    # -- the off-signature shapes: all zero-write ----------------------------

    def test_absent_row_is_not_found(self) -> None:
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_revision_race_loses(self) -> None:
        self._seed()
        out = self._repair(expected_revision=self._rec().revision + 5)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(self._rec().declared_slots, "")

    def test_generation_race_loses(self) -> None:
        """The pins name a generation; a re-incarnated row's empty snapshot is not this pair's."""
        self._seed()
        out = self._repair(expected_generation=self._rec().lane_generation + 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_GENERATION_MISMATCH)
        self.assertEqual(self._rec().declared_slots, "")

    def test_active_row_is_the_13809_backfills_target_not_this_surfaces(self) -> None:
        dec = _decision()
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=dec,
            issue_id=_ISSUE,
            declared_slots=(),
            worktree_identity=_BOUND_WT,
        )
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_retired_row_is_terminal(self) -> None:
        self._seed()
        rec = self._rec()
        self.lifecycle.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_HIBERNATED,
            expected_revision=rec.revision,
            target=DISPOSITION_RETIRED,
            decision=_decision(),
        )
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_superseded_row_is_refused(self) -> None:
        # ``hibernated -> superseded`` is not a legal edge, so the superseded shape is reached
        # from ``active`` through the replacement rail (and asserted, so a refused seed cannot
        # leave a still-hibernated row that the repair would legitimately fill).
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=(),
            worktree_identity=_BOUND_WT,
        )
        rec = self._rec()
        self.lifecycle.supersede_and_activate(
            superseded=self.key,
            expected_revision=rec.revision,
            recovery=LaneLifecycleKey(_WORKSPACE_ID, f"{_LANE}_recovery"),
            decision=_decision(),
        )
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_SUPERSEDED)
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_empty_worktree_binding_is_the_legacy_signature_not_this_ones(self) -> None:
        """The #13841 / #13842 EMPTY-binding legacy row is refused here (disjoint signatures)."""
        self._seed(worktree_identity="")
        out = self._repair(worktree_identity=_BOUND_WT)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_mismatched_worktree_binding_is_refused(self) -> None:
        self._seed()
        out = self._repair(worktree_identity=_OTHER_WT)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_different_issue_is_refused(self) -> None:
        self._seed()
        out = self._repair(issue_id=_OTHER_ISSUE, decision=_decision(issue=_OTHER_ISSUE))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_project_gateway_binding_is_refused(self) -> None:
        dec = _decision()
        declaration = LaneDeclarationStore(path=self.path)
        declaration.declare_lane(
            self.key,
            decision=dec,
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope="project:mozyo_bridge",
            declared_slots=_pins(),
            worktree_identity=_BOUND_WT,
        )
        rec = self._rec()
        self.lifecycle.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=dec,
        )
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def _force_column(self, column: str, value: str) -> None:
        """Set one column directly at the storage layer (an off-rail shape builder).

        ``declare_lane`` couples ``binding_kind`` and ``project_scope`` (an issue lane may own
        no scope; a project-gateway lane must own one), so the two axes MASK each other on-rail:
        removing either guard alone keeps every rails-built test green, which would leave both
        as untested code. Constructing the impossible row directly is the only way to observe
        each guard bite on its own (the #13845 off-rail precedent).
        """
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                f"UPDATE lane_lifecycle_records SET {column} = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ?",
                (value, _WORKSPACE_ID, _LANE),
            )
            conn.commit()
        finally:
            conn.close()

    def test_project_gateway_binding_kind_is_refused_in_isolation(self) -> None:
        """The binding-kind guard bites even with no project scope to also catch the row."""
        self._seed()
        self._force_column("binding_kind", BINDING_KIND_PROJECT_GATEWAY)
        self.assertEqual(self._rec().project_scope, "", "isolate: no scope may co-refuse")
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_project_scope_is_refused_in_isolation(self) -> None:
        """The project-scope guard bites even on an ``issue`` binding kind."""
        self._seed()
        self._force_column("project_scope", "project:mozyo_bridge")
        rec = self._rec()
        self.assertEqual(rec.binding_kind, "issue", "isolate: kind must not co-refuse")
        self.assertEqual(rec.issue_id, _ISSUE)
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._rec().declared_slots, "")

    def test_release_requested_is_in_flight_and_refused(self) -> None:
        self._seed(release_target=RELEASE_REQUESTED)
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self._rec().declared_slots, "")

    def test_release_partial_is_in_flight_and_refused(self) -> None:
        self._seed(release_target=RELEASE_PARTIAL)
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self._rec().declared_slots, "")

    def test_hibernating_with_an_open_replacement_is_already_impossible(self) -> None:
        """The rails never MAKE a hibernated row with an unsettled replacement.

        Pinned because it bounds the reachability of the CAS's ``replacement_settled`` guard:
        ``request_replacement`` requires an ``active`` owner, and ``transition_disposition``
        refuses ``active -> hibernated`` while the replacement is unsettled. So the shape the
        guard refuses cannot be reached through the public lifecycle rails at all — the guard
        is defense in depth, not a live path (the #13845 precedent).
        """
        dec = _decision()
        LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=dec,
            issue_id=_ISSUE,
            declared_slots=(),
            worktree_identity=_BOUND_WT,
        )
        rec = self._rec()
        opened = LaneReplacementStore(path=self.path).request_replacement(
            self.key,
            expected_revision=rec.revision,
            action_id="repl-1",
            pins=[ReleasePin("worker", _wk_name(), _WK_LOC)],
            decision=dec,
        )
        self.assertTrue(opened.applied)
        rec = self._rec()
        self.assertEqual(rec.replacement_state, REPLACEMENT_REQUESTED)
        blocked = self.lifecycle.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=dec,
        )
        self.assertFalse(blocked.applied)
        self.assertEqual(blocked.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self._rec().lane_disposition, DISPOSITION_ACTIVE)

    def test_pending_replacement_is_refused_even_off_rail(self) -> None:
        """The ``replacement_settled`` guard bites on a row the rails cannot produce.

        Since the rails cannot build the shape (see the test above), construct it directly at
        the storage layer — a hibernated / released / bound / pins-empty row whose replacement
        is stuck ``requested`` — and prove the CAS refuses it zero-write rather than pinning a
        lane with a receiver swap in flight. This is the only route to the guard, so without it
        the guard would be untested code asserting an unverified claim (the #13845 precedent).
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
        self.assertEqual(self._rec().replacement_state, REPLACEMENT_REQUESTED)
        out = self._repair()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self._rec().declared_slots, "")

    # -- caller-error guards --------------------------------------------------

    def test_empty_pin_set_is_rejected(self) -> None:
        """An empty 'repair' would write nothing and leave recover-pair blocked the same way."""
        self._seed()
        with self.assertRaises(ValueError):
            self._repair(declared_slots=())

    def test_empty_issue_or_worktree_is_rejected(self) -> None:
        self._seed()
        with self.assertRaises(ValueError):
            self._repair(issue_id="")
        with self.assertRaises(ValueError):
            self._repair(worktree_identity="")

    def test_decision_anchored_to_another_issue_is_rejected(self) -> None:
        self._seed()
        with self.assertRaises(DecisionPointerError):
            self._repair(decision=_decision(issue=_OTHER_ISSUE))

    def test_duplicate_pin_identity_is_rejected(self) -> None:
        self._seed()
        dupe = _pins() + (_pins()[0],)
        with self.assertRaises(Exception):
            self._repair(declared_slots=dupe)


# ---------------------------------------------------------------------------
# 2. Sibling non-regression: the signatures are disjoint.
# ---------------------------------------------------------------------------


class PinRepairDoesNotErodeSiblings(unittest.TestCase):
    """#13842's EMPTY-binding signature and #13879's BOUND one can never both match a row."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "lane_lifecycle.sqlite3"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.lifecycle = LaneLifecycleStore(path=self.path)

    def test_13842_reconcile_still_refuses_the_bound_row_13879_repairs(self) -> None:
        _seed_hibernated_released_bound(path=self.path, key=self.key)
        rec = self.lifecycle.get(self.key)
        out = LaneReconcileBindingStore(path=self.path).retire_reconciled_hibernated_legacy(
            self.key,
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            declared_slots=_pins(),
            decision=_decision(),
        )
        self.assertFalse(out.applied, "a BOUND row is not #13842's legacy target")
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.lifecycle.get(self.key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_13879_repair_refuses_the_empty_binding_row_13842_reconciles(self) -> None:
        _seed_hibernated_released_bound(path=self.path, key=self.key, worktree_identity="")
        rec = self.lifecycle.get(self.key)
        out = LanePinRepairStore(path=self.path).repair_hibernated_bound_pins(
            self.key,
            expected_revision=rec.revision,
            expected_generation=rec.lane_generation,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            declared_slots=_pins(),
            decision=_decision(),
        )
        self.assertFalse(out.applied, "an EMPTY-binding legacy row is not #13879's target")
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.lifecycle.get(self.key).declared_slots, "")


# ---------------------------------------------------------------------------
# 3. The command boundary over a fake live inventory.
# ---------------------------------------------------------------------------


class _FakeOps:
    """A fake ``ReconcileOps``: a scripted inventory + per-locator runtime / composer facts.

    Also a **tripwire**: it exposes no close / launch / send surface at all, so a repair that
    tried to actuate a process would fail with an AttributeError rather than silently pass.
    """

    def __init__(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        rows_error: bool = False,
        runtime: Optional[Mapping[str, str]] = None,
        composer: Optional[Mapping[str, tuple]] = None,
        attested: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._rows = list(rows)
        self._rows_error = rows_error
        self._runtime = dict(runtime or {})
        self._composer = dict(composer or {})
        self._attested = dict(attested or {})

    def agent_rows(self):
        if self._rows_error:
            raise HerdrSessionStartError("herdr inventory unreadable (fake)")
        return self._rows

    def runtime_state(self, locator: str) -> str:
        return self._runtime.get(locator, RUNTIME_AWAITING_INPUT)

    def observe_composer(self, locator: str) -> tuple:
        return self._composer.get(locator, (True, False))

    def read_attestation(self, assigned_name: str):
        locator = self._attested.get(assigned_name)
        if locator is None:
            return None
        role = _GW_PROVIDER if assigned_name == _gw_name() else _WK_PROVIDER
        return IdentityAttestationRecord(
            assigned_name=assigned_name,
            workspace_id=_WORKSPACE_ID,
            role=role,
            lane_id=_LANE,
            locator=locator,
            verdict=VERDICT_PRESENT,
            observed_at=_ATTESTED_AT,
        )


def _row(name: str, locator: str, *, agent: str = "claude") -> dict:
    return {"name": name, "pane_id": locator, "agent": agent}


def _live_pair(gw: str = _GW_LOC, wk: str = _WK_LOC) -> list:
    return [_row(_gw_name(), gw), _row(_wk_name(), wk)]


def _attest_pair(gw: str = _GW_LOC, wk: str = _WK_LOC) -> dict:
    return {_gw_name(): gw, _wk_name(): wk}


#: The ``observed_at`` the fake attestation records carry. The command copies it onto each pin
#: as the verified startup self-attestation's evidence (the #13809 / #13810 R4-F1 discipline),
#: so the snapshot the COMMAND writes is not byte-equal to the bare ``_pins()`` the CAS matrix
#: uses — the pins carry real evidence, never a fabricated blank.
_ATTESTED_AT = "2026-07-17T00:00:00Z"


def _pins_attested(gw: str = _GW_LOC, wk: str = _WK_LOC) -> tuple[ProcessGenerationPin, ...]:
    """The snapshot the COMMAND writes: the live pair plus its verified attestation evidence."""
    return (
        ProcessGenerationPin(
            role=RECOVER_GATEWAY_ROLE,
            provider=_GW_PROVIDER,
            assigned_name=_gw_name(),
            locator=gw,
            attested_at=_ATTESTED_AT,
        ),
        ProcessGenerationPin(
            role=RECOVER_WORKER_ROLE,
            provider=_WK_PROVIDER,
            assigned_name=_wk_name(),
            locator=wk,
            attested_at=_ATTESTED_AT,
        ),
    )


class PinRepairCommandTests(unittest.TestCase):
    """``sublane repair-pins`` over a fake inventory: every acceptance axis, zero actuation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.worktree = self.home / "lane_worktree"
        self.worktree.mkdir()
        self.lifecycle = LaneLifecycleStore()
        # The command imports these lazily from their OWN modules inside the call, so the source
        # modules are the patch targets. They stub the herdr/git anchors the unit resolution
        # needs — never the repair's own guards, which stay under test.
        self._patch(herdr_projection, "repo_backend_is_herdr", lambda root: True)
        self._patch(session_start, "herdr_workspace_segment", lambda p: _WORKSPACE_ID)
        self._patch(herdr_identity, "derive_lane_workspace_token", lambda p: _BOUND_WT)
        self._patch(provider_resolution, "resolve_gateway_provider", lambda r: _GW_PROVIDER)
        self._patch(provider_resolution, "resolve_worker_provider", lambda r: _WK_PROVIDER)

    def _restore_home(self) -> None:
        if self._prev_home is None:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
        else:
            os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home

    def _patch(self, module, attr, value) -> None:
        had = hasattr(module, attr)
        prev = getattr(module, attr, None)

        def restore():
            if had:
                setattr(module, attr, prev)
            else:
                delattr(module, attr)

        setattr(module, attr, value)
        self.addCleanup(restore)

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(
            issue=_ISSUE,
            lane=_LANE,
            journal=_JOURNAL,
            worktree=str(self.worktree),
            execute=True,
            repo=str(self.home),
            json=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _run(self, ops, **kw) -> PinRepairVerdict:
        return run_hibernated_pin_repair(self._args(**kw), self.home, ops=ops)

    def _seed(self, **kw) -> None:
        _seed_hibernated_released_bound(path=None, key=self.key, **kw)

    def _rec(self):
        return self.lifecycle.get(self.key)

    def _green_ops(self) -> _FakeOps:
        return _FakeOps(_live_pair(), attested=_attest_pair())

    def _reset_row(self) -> None:
        """Drop the lane's lifecycle row so a subTest can re-seed a fresh shape.

        Test-only teardown of this test's OWN isolated store (MOZYO_BRIDGE_HOME points at a
        tmpdir); it is not a repair path and never touches a shared home.
        """
        path = LaneLifecycleStore().path
        if not path.exists():
            return
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "DELETE FROM lane_lifecycle_records WHERE repo_workspace_id = ? AND lane_id = ?",
                (_WORKSPACE_ID, _LANE),
            )
            conn.commit()
        finally:
            conn.close()

    def _assert_pins_unwritten(self) -> None:
        self.assertEqual(
            self._rec().declared_slots, "", "a blocked repair must be zero-write"
        )

    # -- the one path that repairs -------------------------------------------

    def test_green_pair_repairs_the_empty_pins(self) -> None:
        self._seed()
        before = self._rec()
        result = self._run(self._green_ops())
        self.assertEqual(result.state, REPAIR_REPAIRED)
        self.assertTrue(result.ok)
        self.assertTrue(result.repaired)
        after = self._rec()
        self.assertEqual(after.declared_slots, encode_declared_slots(_pins_attested()))
        self.assertEqual(after.revision, before.revision + 1)
        # Metadata only (acceptance 3): the lane stays hibernated, nothing was actuated.
        self.assertEqual(after.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(after.worktree_identity, before.worktree_identity)
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.process_release, before.process_release)

    def test_pins_are_built_from_the_live_pair_not_a_name_or_cache(self) -> None:
        """Acceptance 1: the locators come from the live rows, never from the row/name."""
        self._seed()
        result = self._run(
            _FakeOps(
                _live_pair(gw="w31:p9A", wk="w31:p9B"),
                attested=_attest_pair(gw="w31:p9A", wk="w31:p9B"),
            )
        )
        self.assertEqual(result.state, REPAIR_REPAIRED)
        locators = sorted(p["locator"] for p in result.pins)
        self.assertEqual(locators, ["w31:p9A", "w31:p9B"])

    def test_pins_carry_the_verified_attestation_evidence_and_no_fabricated_runtime(
        self,
    ) -> None:
        """``attested_at`` is real evidence; ``runtime_revision`` is never fabricated.

        herdr's generation discriminant is the locator and it exposes no runtime-version
        surface, so the pin records the verified startup self-attestation's ``observed_at`` and
        leaves ``runtime_revision`` empty (the #13809 / #13810 R4-F1 discipline). This is also
        what makes acceptance 4's byte-equality stable: a startup self-attestation is written
        once per generation and pinned by locator, so a same-generation replay re-reads the same
        ``observed_at`` and the snapshot bytes match.
        """
        self._seed()
        result = self._run(self._green_ops())
        self.assertEqual(result.state, REPAIR_REPAIRED)
        for pin in result.pins:
            self.assertEqual(pin["attested_at"], _ATTESTED_AT)
            self.assertEqual(pin["runtime_revision"], "")

    def test_preflight_verifies_but_writes_nothing(self) -> None:
        self._seed()
        result = self._run(self._green_ops(), execute=False)
        self.assertEqual(result.state, REPAIR_REPAIRABLE)
        self.assertTrue(result.ok, "a green preflight exits 0")
        self.assertFalse(result.repaired)
        self.assertFalse(result.executed)
        self._assert_pins_unwritten()

    # -- preflight must PREDICT --execute, on every axis (review j#80547 F1) --

    def test_preflight_predicts_execute_on_every_row_shape(self) -> None:
        """The preflight and the CAS must never disagree about what would happen.

        The defect this pins (j#80547 F1): the base signature deliberately leaves
        ``declared_slots`` unchecked (a byte-equal replay legitimately finds a NON-empty
        snapshot), and the preflight branch then returned a bare ``repairable`` regardless of
        the persisted snapshot -- so a divergent row previewed as exit 0 while --execute
        refused it, and a byte-equal row previewed as "would repair" while --execute wrote
        nothing. Asserting each state in isolation would not have caught it; the property is
        the AGREEMENT between the two paths, so it is asserted as an equivalence over all
        three row shapes.
        """
        # ``expect_preview`` is the preflight's PREDICTION of ``expect_state``; the two differ
        # only for the empty shape, where the preview cannot claim the write it did not make.
        # Asserting (ok, reason) alone is NOT enough: on a byte-equal row the pre-fix bare
        # ``repairable`` and the correct ``already_repaired`` share both, so only the state
        # distinguishes them.
        for label, seed_pins, ops, expect_preview, expect_state in (
            ("empty snapshot", None, self._green_ops(), REPAIR_REPAIRABLE, REPAIR_REPAIRED),
            ("byte-equal replay", "same", self._green_ops(), REPAIR_ALREADY, REPAIR_ALREADY),
            (
                "divergent snapshot",
                "same",
                _FakeOps(
                    _live_pair(gw="w99:p1", wk="w99:p2"),
                    attested=_attest_pair(gw="w99:p1", wk="w99:p2"),
                ),
                REPAIR_BLOCKED,
                REPAIR_BLOCKED,
            ),
        ):
            with self.subTest(shape=label):
                self.lifecycle = LaneLifecycleStore()
                self._reset_row()
                self._seed()
                if seed_pins == "same":
                    self.assertEqual(
                        self._run(self._green_ops()).state, REPAIR_REPAIRED, label
                    )
                before = self._rec().declared_slots
                preview = self._run(ops, execute=False)
                self.assertEqual(
                    self._rec().declared_slots, before, f"{label}: preflight wrote"
                )
                actual = self._run(ops, execute=True)
                self.assertEqual(actual.state, expect_state, f"{label}: execute state")
                # The property: the preview names the outcome --execute reaches, with the same
                # exit code and reason, and never writes.
                self.assertEqual(
                    preview.state, expect_preview, f"{label}: preflight mispredicts execute"
                )
                self.assertEqual(
                    preview.ok, actual.ok, f"{label}: preflight/execute exit codes disagree"
                )
                self.assertEqual(
                    preview.reason,
                    actual.reason,
                    f"{label}: preflight/execute reasons disagree",
                )
                self.assertFalse(preview.repaired, f"{label}: preflight claimed a write")
                self.assertFalse(preview.executed, f"{label}: preflight claimed execution")

    def test_preflight_on_a_divergent_row_is_fail_closed(self) -> None:
        self._seed()
        self.assertEqual(self._run(self._green_ops()).state, REPAIR_REPAIRED)
        filled = self._rec()
        preview = self._run(
            _FakeOps(
                _live_pair(gw="w99:p1", wk="w99:p2"),
                attested=_attest_pair(gw="w99:p1", wk="w99:p2"),
            ),
            execute=False,
        )
        self.assertEqual(preview.state, REPAIR_BLOCKED)
        self.assertEqual(preview.reason, REPAIR_PINS_DIVERGENT)
        self.assertFalse(preview.ok, "a divergent preview must not exit 0")
        self.assertEqual(self._rec().declared_slots, filled.declared_slots)

    def test_preflight_on_a_byte_equal_row_reports_already_not_repairable(self) -> None:
        self._seed()
        self.assertEqual(self._run(self._green_ops()).state, REPAIR_REPAIRED)
        preview = self._run(self._green_ops(), execute=False)
        self.assertEqual(preview.state, REPAIR_ALREADY)
        self.assertTrue(preview.ok)
        self.assertFalse(preview.repaired)
        # The renderer must not tell the operator to run --execute: it would write nothing.
        text = format_pin_repair_text(preview)
        self.assertIn("nothing to repair", text)
        self.assertNotIn("re-run with --execute to repair", text)

    # -- replay (acceptance 4) ------------------------------------------------

    def test_byte_equal_replay_reports_already_and_writes_nothing(self) -> None:
        self._seed()
        self.assertEqual(self._run(self._green_ops()).state, REPAIR_REPAIRED)
        filled = self._rec()
        result = self._run(self._green_ops())
        self.assertEqual(result.state, REPAIR_ALREADY)
        self.assertTrue(result.ok)
        self.assertFalse(result.repaired, "an idempotent replay writes nothing")
        self.assertEqual(self._rec().revision, filled.revision)

    def test_recycled_generation_replay_is_fail_closed(self) -> None:
        """Different pins on an already-pinned row are refused, never overwritten."""
        self._seed()
        self.assertEqual(self._run(self._green_ops()).state, REPAIR_REPAIRED)
        filled = self._rec()
        result = self._run(
            _FakeOps(
                _live_pair(gw="w99:p1", wk="w99:p2"),
                attested=_attest_pair(gw="w99:p1", wk="w99:p2"),
            )
        )
        self.assertEqual(result.state, REPAIR_BLOCKED)
        self.assertEqual(result.reason, REPAIR_PINS_DIVERGENT)
        self.assertFalse(result.ok)
        self.assertEqual(self._rec().declared_slots, filled.declared_slots)

    # -- acceptance 1: every live-pair axis is a zero-write refusal -----------

    def test_unreadable_inventory_is_never_read_as_no_pair(self) -> None:
        self._seed()
        result = self._run(_FakeOps([], rows_error=True))
        self.assertEqual(result.state, REPAIR_BLOCKED)
        self.assertEqual(result.reason, REASON_INVENTORY_UNREADABLE)
        self._assert_pins_unwritten()

    def test_absent_pair_never_fabricates_pins(self) -> None:
        self._seed()
        result = self._run(_FakeOps([]))
        self.assertEqual(result.state, REPAIR_BLOCKED)
        self.assertEqual(result.reason, REPAIR_LIVE_PAIR_ABSENT)
        self._assert_pins_unwritten()

    def test_partial_pair_is_refused(self) -> None:
        self._seed()
        result = self._run(
            _FakeOps([_row(_gw_name(), _GW_LOC)], attested={_gw_name(): _GW_LOC})
        )
        self.assertEqual(result.reason, RECON_PAIR_INCOMPLETE)
        self._assert_pins_unwritten()

    def test_duplicate_assigned_name_is_refused(self) -> None:
        self._seed()
        rows = _live_pair() + [_row(_gw_name(), "w28:p9Z")]
        result = self._run(_FakeOps(rows, attested=_attest_pair()))
        self.assertEqual(result.reason, RECON_PAIR_AMBIGUOUS)
        self._assert_pins_unwritten()

    def test_foreign_provider_at_the_lanes_position_is_refused(self) -> None:
        self._seed()
        rows = _live_pair() + [
            _row(encode_assigned_name(_WORKSPACE_ID, "intruder", _LANE), "w28:p9Z")
        ]
        result = self._run(_FakeOps(rows, attested=_attest_pair()))
        self.assertEqual(result.reason, RECON_FOREIGN_PROVIDER)
        self._assert_pins_unwritten()

    def test_stale_shell_residue_is_refused(self) -> None:
        self._seed()
        rows = [_row(_gw_name(), _GW_LOC, agent=""), _row(_wk_name(), _WK_LOC)]
        result = self._run(_FakeOps(rows, attested=_attest_pair()))
        self.assertEqual(result.reason, RECON_SLOT_STALE)
        self._assert_pins_unwritten()

    def test_unattested_slot_is_refused(self) -> None:
        self._seed()
        result = self._run(_FakeOps(_live_pair(), attested={_gw_name(): _GW_LOC}))
        self.assertEqual(result.reason, RECON_IDENTITY_UNATTESTED)
        self._assert_pins_unwritten()

    def test_attestation_bound_to_another_generation_is_refused(self) -> None:
        """A stale attestation (recorded at a DIFFERENT locator) is never re-used."""
        self._seed()
        result = self._run(
            _FakeOps(_live_pair(), attested=_attest_pair(gw="w28:pOLD"))
        )
        self.assertEqual(result.reason, RECON_IDENTITY_UNATTESTED)
        self._assert_pins_unwritten()

    def test_busy_agent_is_refused(self) -> None:
        self._seed()
        result = self._run(
            _FakeOps(
                _live_pair(),
                attested=_attest_pair(),
                runtime={_GW_LOC: RUNTIME_BUSY},
            )
        )
        self.assertEqual(result.reason, RECON_AGENT_NOT_IDLE)
        self._assert_pins_unwritten()

    def test_turn_ended_agent_is_accepted(self) -> None:
        """Over-block is a defect too: ``turn_ended`` is settled, exactly like ``idle``."""
        self._seed()
        result = self._run(
            _FakeOps(
                _live_pair(),
                attested=_attest_pair(),
                runtime={_GW_LOC: RUNTIME_TURN_ENDED, _WK_LOC: RUNTIME_TURN_ENDED},
            )
        )
        self.assertEqual(result.state, REPAIR_REPAIRED)

    def test_pending_composer_is_refused(self) -> None:
        self._seed()
        result = self._run(
            _FakeOps(
                _live_pair(),
                attested=_attest_pair(),
                composer={_WK_LOC: (True, True)},
            )
        )
        self.assertEqual(result.reason, RECON_PENDING_COMPOSER)
        self._assert_pins_unwritten()

    def test_unreadable_composer_is_refused(self) -> None:
        """``has_pending=None`` is unreadable and must never read as "no pending"."""
        self._seed()
        result = self._run(
            _FakeOps(
                _live_pair(),
                attested=_attest_pair(),
                composer={_WK_LOC: (False, None)},
            )
        )
        self.assertEqual(result.reason, RECON_PENDING_COMPOSER)
        self._assert_pins_unwritten()

    # -- acceptance 2: the row signature at the command boundary --------------

    def test_active_row_is_routed_away(self) -> None:
        LaneDeclarationStore().declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=(),
            worktree_identity=_BOUND_WT,
        )
        result = self._run(self._green_ops())
        self.assertEqual(result.reason, REPAIR_NOT_REPAIRABLE_STATE)
        self._assert_pins_unwritten()

    def test_empty_binding_legacy_row_is_routed_away(self) -> None:
        self._seed(worktree_identity="")
        result = self._run(self._green_ops())
        self.assertEqual(result.reason, REPAIR_NOT_REPAIRABLE_STATE)
        self._assert_pins_unwritten()

    def test_in_flight_release_is_refused(self) -> None:
        self._seed(release_target=RELEASE_REQUESTED)
        result = self._run(self._green_ops())
        self.assertEqual(result.reason, REPAIR_RELEASE_NOT_PROVEN)
        self._assert_pins_unwritten()

    def test_missing_worktree_anchor_is_refused(self) -> None:
        self._seed()
        result = self._run(self._green_ops(), worktree=None)
        self.assertEqual(result.reason, REASON_NO_WORKTREE_ANCHOR)
        self._assert_pins_unwritten()

    # -- acceptance 5: the repair actually clears recover-pair's blocker --------

    def test_repaired_pins_are_readable_by_recover_pairs_own_reader(self) -> None:
        """The whole point (acceptance 5): recover-pair must FIND the pins by role.

        Not "the repair wrote something" but "the blocker is gone": this reads the repaired row
        back through ``recover-pair``'s OWN ``_declared_pins_by_role`` + its OWN role constants.
        The vocabularies are a live trap — ``domain.sublane_lifecycle`` exports ``GATEWAY_ROLE``
        / ``WORKER_ROLE`` with the same NAMES but the legacy provider VALUES (``codex`` /
        ``claude``), so pinning ``role`` to those writes a snapshot recover-pair cannot resolve
        and the lane stays on ``hibernated_record_missing_pins`` — the exact defect #13879
        exists to clear, surviving a "successful" repair.
        """
        self._seed()
        self.assertEqual(self._run(self._green_ops()).state, REPAIR_REPAIRED)
        record = self._rec()
        declared = recover_pair._declared_pins_by_role(record)
        gw_pin = declared.get(RECOVER_GATEWAY_ROLE)
        wk_pin = declared.get(RECOVER_WORKER_ROLE)
        self.assertIsNotNone(gw_pin, "recover-pair could not resolve the repaired gateway pin")
        self.assertIsNotNone(wk_pin, "recover-pair could not resolve the repaired worker pin")
        # recover-pair derives the provider binding from the pin; it must name the provider,
        # not the workflow role.
        self.assertEqual(gw_pin.provider, _GW_PROVIDER)
        self.assertEqual(wk_pin.provider, _WK_PROVIDER)
        # ``record_has_pins`` (the blocker) is exactly "both roles resolve".
        self.assertTrue(gw_pin is not None and wk_pin is not None)

    def test_the_two_role_vocabularies_really_do_differ(self) -> None:
        """Pins the trap itself, so a future 'tidy the imports' edit cannot silently re-arm it."""
        self.assertEqual(RECOVER_GATEWAY_ROLE, "gateway")
        self.assertEqual(RECOVER_WORKER_ROLE, "worker")
        self.assertEqual(LEGACY_GATEWAY_ROLE, "codex")
        self.assertEqual(LEGACY_WORKER_ROLE, "claude")
        self.assertNotEqual(RECOVER_GATEWAY_ROLE, LEGACY_GATEWAY_ROLE)

    def test_json_payload_is_structured(self) -> None:
        self._seed()
        result = self._run(self._green_ops())
        payload = json.loads(json.dumps(result.as_payload()))
        self.assertEqual(payload["state"], REPAIR_REPAIRED)
        self.assertTrue(payload["repaired"])
        self.assertEqual(len(payload["pins"]), 2)

    def test_text_render_never_claims_a_write_it_did_not_make(self) -> None:
        blocked = PinRepairVerdict(
            state=REPAIR_BLOCKED, reason=REPAIR_LIVE_PAIR_ABSENT, lane_id=_LANE
        )
        text = format_pin_repair_text(blocked)
        self.assertIn("fail-closed", text)
        self.assertIn("nothing was written", text)
        self.assertNotIn("durable write", text)


if __name__ == "__main__":
    unittest.main()
