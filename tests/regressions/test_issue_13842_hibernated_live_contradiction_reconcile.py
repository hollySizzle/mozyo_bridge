"""Regression pins for the #13842 hibernated live-contradiction reconcile.

Redmine #13842 (parent #12499), live evidence #13756 j#79188. A hibernated / released
**legacy** lifecycle row — the coordinator hibernated the lane, its process release completed
durably (``process_release`` reached ``released``), but its ``worktree_identity`` is EMPTY (a
pre-#13754 row) — whose exact managed pair is nonetheless observed **live** in the action-time
Herdr inventory. Three contracts leave it with no convergence path:

- the #13841 live-zero migration refuses zero-write on ``live_pair_present``;
- the #13754 guarded ``retire --execute`` refuses on ``worktree_binding_unverified``;
- the #13809 ``backfill_active_binding`` fills an **active** row only.

The reconcile converges it in ONE replayable flow, and ONLY when the exact live pair is
unique / idle / turn-ended / settled / generation-bound attested: it re-establishes the
missing worktree + process binding via a bounded CAS, then hands the now-bound lane to the
#13754 guarded close (which closes the pair and records the terminal ``retired`` disposition).

Three layers are pinned, all synthetic (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr inventory
+ injected observation ops, never a live pane / process / route mutation):

1. the bounded rebind CAS guard matrix (``LaneReconcileBindingStore``);
2. the pure action-time pair decision (``decide_pair_reconcile``);
3. the orchestration + command boundary (``sublane retire --reconcile-hibernated-live``): the
   happy path, every fail-closed axis, the owed-state partial replay, idempotent replay, and
   non-regression of the #13754 guarded close (three-way mutual exclusion).

Boundary (Redmine #13842): no process launch / resume, no worktree / branch removal, no raw
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

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    IdentityAttestationRecord,
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
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_reconcile_binding import (  # noqa: E402
    LaneReconcileBindingStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_herdr_projection,
    sublane_herdr_retire,
    sublane_lifecycle_command,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    HerdrRetireCloseResult,
    REASON_INVENTORY_UNREADABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile import (  # noqa: E402,E501
    RECONCILE_ALREADY,
    RECONCILE_BLOCKED,
    RECONCILE_RECONCILED,
    RECON_HEAD_NOT_INTEGRATED,
    RECON_LIVE_PAIR_ABSENT,
    RECON_LIVE_PAIR_PRESENT,
    RECON_CLOSE_FAILED,
    RECON_NOT_RECONCILABLE_STATE,
    RECON_REVISION_RACE,
    RECON_WORKTREE_BRANCH_MISMATCH,
    run_hibernated_live_reconcile,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_hibernated_live_reconcile import (  # noqa: E402,E501
    PairObservation,
    RECON_AGENT_NOT_IDLE,
    RECON_FOREIGN_PROVIDER,
    RECON_IDENTITY_UNATTESTED,
    RECON_INVENTORY_UNREADABLE,
    RECON_PAIR_AMBIGUOUS,
    RECON_PAIR_INCOMPLETE,
    RECON_PENDING_COMPOSER,
    RECON_SLOT_STALE,
    STATE_ABSENT,
    STATE_BLOCKED,
    STATE_GREEN,
    SlotObservation,
    decide_pair_reconcile,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E402,E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BLOCKED,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    decode_assigned_name,
    derive_lane_workspace_token,
    encode_assigned_name,
)

_WORKSPACE_ID = "e1487dcb1f2d4412"
_LANE = "issue_13756_fill_actionability"
_ISSUE = "13756"
_JOURNAL = "79188"
_OTHER_ISSUE = "13999"


def _decision(issue: str = _ISSUE, journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _row(ws: str, role: str, lane: str, locator: str, *, agent: str = None) -> dict:
    row = {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}
    # A live managed pane reports its detected provider agent; default to the role
    # (provider) so classify_named_slot reads it live. Pass agent="" for a shell residue.
    row["agent"] = role if agent is None else agent
    row["agent_status"] = "idle"
    return row


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
    """Drive a row to hibernated + <release_target> via the REAL store transitions."""
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


def _pins() -> list[ProcessGenerationPin]:
    return [
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "codex", _LANE),
            locator="w28:p3",
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "claude", _LANE),
            locator="w28:p4",
        ),
    ]


# ---------------------------------------------------------------------------
# 1. The bounded rebind CAS guard matrix.
# ---------------------------------------------------------------------------


class ReconcileRebindCasMatrix(unittest.TestCase):
    """``LaneReconcileBindingStore.retire_reconciled_hibernated_legacy`` fail-closed matrix."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.sqlite"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        self.store = LaneLifecycleStore(path=self.path)
        self.rebind = LaneReconcileBindingStore(path=self.path)
        self.token = "wt_deadbeefcafef00d"

    def _seed(self, **kwargs) -> None:
        _seed_hibernated_released(self.store, key=self.key, **kwargs)

    def _do(self, *, expected_revision=None, issue=_ISSUE, token=None, pins=None, decision=None):
        rec = self.store.get(self.key)
        rev = expected_revision if expected_revision is not None else (
            rec.revision if rec is not None else 1
        )
        return self.rebind.retire_reconciled_hibernated_legacy(
            self.key,
            expected_revision=rev,
            issue_id=issue,
            worktree_identity=token if token is not None else self.token,
            declared_slots=pins if pins is not None else _pins(),
            decision=decision if decision is not None else _decision(issue),
        )

    def test_exact_signature_retires_and_binds(self) -> None:
        # Retire-first (review j#79282 R2): the ONE CAS moves hibernated -> retired AND writes
        # the worktree + declared_slots binding + decision.
        self._seed()
        out = self._do()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)
        self.assertEqual(rec.worktree_identity, self.token)
        self.assertEqual(len(rec.declared_pins), 2)
        self.assertEqual(rec.decision_journal, _JOURNAL)
        self.assertEqual(rec.process_release, RELEASE_RELEASED)

    def test_second_call_on_retired_row_is_refused(self) -> None:
        # Once retired, a replay of the retire CAS is refused (not hibernated) — the terminal
        # is reached exactly once; the owed close resumes through the command layer, not here.
        self._seed()
        self.assertTrue(self._do().applied)
        rec = self.store.get(self.key)
        second = self.rebind.retire_reconciled_hibernated_legacy(
            self.key,
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=self.token,
            declared_slots=_pins(),
            decision=_decision(),
        )
        self.assertFalse(second.applied)
        self.assertEqual(second.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_RETIRED)

    def test_retire_writes_decision_and_retires(self) -> None:
        self._seed()  # seeded decision journal is _JOURNAL
        out = self.rebind.retire_reconciled_hibernated_legacy(
            self.key,
            expected_revision=self.store.get(self.key).revision,
            issue_id=_ISSUE,
            worktree_identity=self.token,
            declared_slots=_pins(),
            decision=DecisionPointer(source="redmine", issue_id=_ISSUE, journal_id="70001"),
        )
        self.assertTrue(out.applied)
        rec = self.store.get(self.key)
        self.assertEqual(rec.decision_journal, "70001")
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)

    def test_non_empty_worktree_is_refused(self) -> None:
        # Review j#79320 R1: ANY existing worktree binding (even one equal to the incoming token)
        # is refused — the reconcile targets ONLY the EMPTY-binding legacy row; a bound row is the
        # #13754 ordinary retire's domain.
        for existing in ("wt_0000000000000000", "wt_deadbeefcafef00d"):  # different, then == token
            with self.subTest(existing=existing):
                self.store = LaneLifecycleStore(path=self.path)
                # fresh key per subtest
                self.key = LaneLifecycleKey(_WORKSPACE_ID, f"{_LANE}_{existing[-4:]}")
                self.rebind = LaneReconcileBindingStore(path=self.path)
                _seed_hibernated_released(self.store, key=self.key, worktree_identity=existing)
                out = self.rebind.retire_reconciled_hibernated_legacy(
                    self.key, expected_revision=self.store.get(self.key).revision,
                    issue_id=_ISSUE, worktree_identity=self.token, declared_slots=_pins(),
                    decision=_decision(),
                )
                self.assertFalse(out.applied)
                self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
                self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_non_empty_declared_slots_is_refused(self) -> None:
        # Review j#79320 R1: a row with an existing declared_slots snapshot (a #13809/#13810-bound
        # row) is refused — empty declared_slots is part of the legacy signature.
        self.store.declare_active(
            self.key, decision=_decision(), issue_id=_ISSUE, worktree_identity=""
        )
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
        LaneDeclarationStore(path=self.path).backfill_active_binding(
            self.key, expected_revision=self.store.get(self.key).revision,
            issue_id=_ISSUE, worktree_identity=self.token, declared_slots=_pins(),
        )
        rec = self.store.get(self.key)
        self.store.transition_disposition(
            self.key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED, decision=_decision(),
        )
        out = self._do()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_active_disposition_is_refused(self) -> None:
        self.store.declare_active(
            self.key, decision=_decision(), issue_id=_ISSUE, worktree_identity=""
        )
        out = self._do()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self.store.get(self.key).lane_disposition, DISPOSITION_ACTIVE)

    def test_superseded_disposition_is_refused(self) -> None:
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
        out = self._do()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_different_issue_is_refused(self) -> None:
        self._seed(issue=_ISSUE)
        out = self._do(issue=_OTHER_ISSUE)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_release_not_requested_is_refused(self) -> None:
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
        out = self._do()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_release_in_flight_requested_is_refused(self) -> None:
        self._seed(release_target=RELEASE_REQUESTED)
        out = self._do()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_release_partial_is_refused(self) -> None:
        self._seed(release_target=RELEASE_PARTIAL)
        out = self._do()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_revision_race_loses(self) -> None:
        self._seed()
        rec = self.store.get(self.key)
        out = self._do(expected_revision=rec.revision - 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(self.store.get(self.key).worktree_identity, "")

    def test_absent_row_is_not_found(self) -> None:
        out = self.rebind.retire_reconciled_hibernated_legacy(
            self.key,
            expected_revision=1,
            issue_id=_ISSUE,
            worktree_identity=self.token,
            declared_slots=_pins(),
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_empty_inputs_raise(self) -> None:
        self._seed()
        rev = self.store.get(self.key).revision
        with self.assertRaises(ValueError):
            self.rebind.retire_reconciled_hibernated_legacy(
                self.key, expected_revision=rev, issue_id="",
                worktree_identity=self.token, declared_slots=_pins(), decision=_decision(),
            )
        with self.assertRaises(ValueError):
            self.rebind.retire_reconciled_hibernated_legacy(
                self.key, expected_revision=rev, issue_id=_ISSUE,
                worktree_identity="", declared_slots=_pins(), decision=_decision(),
            )
        with self.assertRaises(ValueError):
            self.rebind.retire_reconciled_hibernated_legacy(
                self.key, expected_revision=rev, issue_id=_ISSUE,
                worktree_identity=self.token, declared_slots=[], decision=_decision(),
            )
        with self.assertRaises(Exception):
            # A decision anchored to a different issue cannot authorize this binding.
            self.rebind.retire_reconciled_hibernated_legacy(
                self.key, expected_revision=rev, issue_id=_ISSUE,
                worktree_identity=self.token, declared_slots=_pins(),
                decision=_decision(_OTHER_ISSUE),
            )


# ---------------------------------------------------------------------------
# 2. The pure action-time pair decision (pure of the CLI + IO).
# ---------------------------------------------------------------------------


def _slot(
    role: str,
    provider: str,
    *,
    count: int = 1,
    live: bool = True,
    locator: str = "w28:p3",
    attested: bool = True,
    runtime: str = RUNTIME_AWAITING_INPUT,
    readable: bool = True,
    pending=False,
) -> SlotObservation:
    return SlotObservation(
        role=role,
        provider=provider,
        candidate_count=count,
        slot_live=live,
        locator=locator,
        assigned_name=f"mzb1_{provider}",
        attested=attested,
        runtime_state=runtime,
        composer_readable=readable,
        has_pending=pending,
    )


class ReconcilePairDecisionMatrix(unittest.TestCase):
    """``decide_pair_reconcile`` fail-closed precedence matrix."""

    def _both(self, **worker_overrides) -> PairObservation:
        gw = _slot("gateway", "codex", locator="w28:p3")
        wk = _slot("worker", "claude", locator="w28:p4", **worker_overrides)
        return PairObservation(True, False, (gw, wk))

    def test_green_pair(self) -> None:
        self.assertEqual(decide_pair_reconcile(self._both()).state, STATE_GREEN)

    def test_turn_ended_is_settled(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(runtime=RUNTIME_TURN_ENDED)).state, STATE_GREEN
        )

    def test_inventory_unreadable(self) -> None:
        out = decide_pair_reconcile(PairObservation(False))
        self.assertEqual(out.reason, RECON_INVENTORY_UNREADABLE)

    def test_foreign_at_position(self) -> None:
        out = decide_pair_reconcile(PairObservation(True, True, self._both().slots))
        self.assertEqual(out.reason, RECON_FOREIGN_PROVIDER)

    def test_absent_pair_is_positive_absence(self) -> None:
        obs = PairObservation(
            True, False, (_slot("gateway", "codex", count=0), _slot("worker", "claude", count=0))
        )
        self.assertEqual(decide_pair_reconcile(obs).state, STATE_ABSENT)

    def test_incomplete_pair_blocks(self) -> None:
        obs = PairObservation(
            True, False, (_slot("gateway", "codex"), _slot("worker", "claude", count=0))
        )
        self.assertEqual(decide_pair_reconcile(obs).reason, RECON_PAIR_INCOMPLETE)

    def test_ambiguous_duplicate_name_blocks(self) -> None:
        self.assertEqual(decide_pair_reconcile(self._both(count=2)).reason, RECON_PAIR_AMBIGUOUS)

    def test_stale_slot_blocks(self) -> None:
        self.assertEqual(decide_pair_reconcile(self._both(live=False)).reason, RECON_SLOT_STALE)

    def test_unattested_blocks(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(attested=False)).reason, RECON_IDENTITY_UNATTESTED
        )

    def test_busy_blocks(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(runtime=RUNTIME_BUSY)).reason, RECON_AGENT_NOT_IDLE
        )

    def test_blocked_runtime_blocks(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(runtime=RUNTIME_BLOCKED)).reason,
            RECON_AGENT_NOT_IDLE,
        )

    def test_unknown_runtime_blocks(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(runtime=RUNTIME_UNKNOWN)).reason,
            RECON_AGENT_NOT_IDLE,
        )

    def test_pending_composer_blocks(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(pending=True)).reason, RECON_PENDING_COMPOSER
        )

    def test_unreadable_composer_blocks(self) -> None:
        self.assertEqual(
            decide_pair_reconcile(self._both(pending=None)).reason, RECON_PENDING_COMPOSER
        )
        self.assertEqual(
            decide_pair_reconcile(self._both(readable=False)).reason, RECON_PENDING_COMPOSER
        )

    def test_shared_locator_is_ambiguous(self) -> None:
        gw = _slot("gateway", "codex", locator="w28:p3")
        wk = _slot("worker", "claude", locator="w28:p3")  # same locator
        self.assertEqual(
            decide_pair_reconcile(PairObservation(True, False, (gw, wk))).reason,
            RECON_PAIR_AMBIGUOUS,
        )


# ---------------------------------------------------------------------------
# 3. The orchestration + command boundary.
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


class _FakeReconcileOps:
    """Injected observation ops: reads the shared fake inventory; drives per-slot facts.

    ``rows_by_call`` overrides the rows returned for a specific ``agent_rows`` call index
    (call 0 = initial observation, call 1 = close-time re-observation) so a TOCTOU change
    between the two reads can be exercised. ``on_call`` fires a side-effect hook before a
    given call returns (used to inject a concurrent lifecycle race).
    """

    def __init__(
        self,
        rows_ref,
        *,
        runtime: str = RUNTIME_AWAITING_INPUT,
        readable: bool = True,
        has_pending=False,
        attest: bool = True,
        rows_by_call=None,
        on_call=None,
        busy_from_call=None,
    ) -> None:
        self._rows_ref = rows_ref
        self._runtime = runtime
        self._readable = readable
        self._has_pending = has_pending
        self._attest = attest
        self._rows_by_call = rows_by_call or {}
        self._on_call = on_call or {}
        # After this many agent_rows() calls, runtime reads BUSY (a slot that started working
        # between the initial observation and the close-time re-observation).
        self._busy_from_call = busy_from_call
        self._call = 0
        self._last_rows: list = []

    def agent_rows(self):
        index = self._call
        self._call += 1
        if index in self._on_call:
            self._on_call[index]()
        if index in self._rows_by_call:
            self._last_rows = list(self._rows_by_call[index]())
        else:
            self._last_rows = list(self._rows_ref())
        return list(self._last_rows)

    def runtime_state(self, locator: str) -> str:
        if self._busy_from_call is not None and self._call > self._busy_from_call:
            return RUNTIME_BUSY
        return self._runtime

    def observe_composer(self, locator: str):
        return (self._readable, self._has_pending)

    def read_attestation(self, assigned_name: str):
        if not self._attest:
            return None
        # Attest against the MOST RECENTLY observed inventory so a recycled slot at a new
        # locator reads generation-matched (its record.locator tracks the live locator) — this
        # lets the pair_changed (locator-diff) path be exercised apart from stale attestation.
        for row in (self._last_rows or list(self._rows_ref())):
            if row.get("name") == assigned_name:
                decode = decode_assigned_name(assigned_name)
                if not decode.ok:
                    return None
                ident = decode.identity
                return IdentityAttestationRecord(
                    assigned_name=assigned_name,
                    workspace_id=ident.workspace_id,
                    role=ident.role,
                    lane_id=ident.lane_id,
                    locator=row.get("pane_id", ""),
                    verdict=VERDICT_PRESENT,
                    observed_at="2026-07-15T00:00:00+00:00",
                )
        return None


class ReconcileOrchestrationTests(unittest.TestCase):
    """The orchestration over real roots + a fake herdr inventory + injected ops."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.home.mkdir()
        self.primary = tmp / "primary"
        _init_repo(self.primary, anchor=True)
        self.lane_worktree = tmp / "lane_wt"
        _git("worktree", "add", "-b", _LANE, str(self.lane_worktree), "main", cwd=self.primary)

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        # The coordinator's default-lane pair + the lane's live managed pair.
        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
        ]
        self.rows_error: Exception | None = None
        self._real_rows = sublane_herdr_projection.list_herdr_agent_rows
        self._real_execute = sublane_herdr_retire.execute_herdr_retire_close

        def fake_rows(env):
            if self.rows_error is not None:
                raise self.rows_error
            return list(self.rows)

        def fake_execute(plan, **kwargs):
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

    def _token(self) -> str:
        return derive_lane_workspace_token(str(self.lane_worktree.resolve()))

    def _args(self, *, branch: str = _LANE, issue: str = _ISSUE) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.primary),
            issue=issue,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_worktree),
            branch=branch,
            integration_branch="main",
        )

    def _run(self, *, ops=None, head_integrated=True, worktree_branch=_LANE, branch=_LANE):
        return run_hibernated_live_reconcile(
            self._args(branch=branch),
            self.primary,
            head_integrated=head_integrated,
            worktree_branch=worktree_branch,
            ops=ops if ops is not None else _FakeReconcileOps(lambda: self.rows),
        )

    # -- the happy path ---------------------------------------------------

    def test_hibernated_live_pair_reconciles_and_closes(self) -> None:
        self._seed_row()
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        result = self._run()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.state, RECONCILE_RECONCILED)
        # The lane's live pair was closed and the row moved to retired.
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        self.assertNotIn(("codex", "w28:p3"), [(r["name"], r["pane_id"]) for r in self.rows])
        self.assertEqual(
            {r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2"}  # only coordinator left
        )
        # The binding was re-established before the close.
        rec = LaneLifecycleStore().get(self._key())
        self.assertEqual(rec.worktree_identity, self._token())

    def test_duplicate_replay_is_idempotent_already_reconciled(self) -> None:
        self._seed_row()
        self._run()
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # Pair is gone; a replay is a verified idempotent no-op.
        result = self._run()
        self.assertEqual(result.state, RECONCILE_ALREADY)
        self.assertTrue(result.ok)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def _retire_first(self, *, decision=None) -> None:
        """Drive the retire-first CAS directly (retired + bound + pins), as a completed
        retire whose pin-matched close is still owed (crash after the CAS, before the close)."""
        rec = LaneLifecycleStore().get(self._key())
        LaneReconcileBindingStore().retire_reconciled_hibernated_legacy(
            self._key(),
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=self._token(),
            declared_slots=_pins(),
            decision=decision if decision is not None else _decision(),
        )

    def test_retired_with_live_pair_withholds_no_close(self) -> None:
        # Review j#79320 R4: the reconcile NEVER closes a pair under a retired row (it cannot tell
        # its own owed close from an ordinary bound retired row without a collision-proof marker).
        # A crash after the retire CAS but before the close leaves retired + live pair -> the
        # reconcile withholds (no close); recovery is via the ordinary #13754 retire.
        self._seed_row()
        self._retire_first()
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        result = self._run()
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_LIVE_PAIR_PRESENT)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # NOT closed: the reconcile refuses to close a pair under a retired row.
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    def test_ordinary_retired_bound_row_live_pair_not_closed(self) -> None:
        # Review j#79320 R4: an ORDINARY #13809/#13810-bound row retired through the normal
        # lifecycle (with typed declared_slots) whose pair is live must NOT be closed by the
        # reconcile — the retired-branch has no reconcile-specific provenance, so it withholds.
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore

        store = LaneLifecycleStore()
        key = self._key()
        store.declare_active(key, decision=_decision(), issue_id=_ISSUE, worktree_identity=self._token())
        LaneDeclarationStore().backfill_active_binding(
            key, expected_revision=store.get(key).revision, issue_id=_ISSUE,
            worktree_identity=self._token(), declared_slots=_pins(),
        )
        rec = store.get(key)
        store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=rec.revision,
            target=DISPOSITION_RETIRED, decision=_decision(),
        )
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # Its pair is live (p3/p4).
        result = self._run()
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_LIVE_PAIR_PRESENT)
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    def test_bound_hibernated_live_pair_is_not_reconcilable(self) -> None:
        # Review j#79320 R1: a #13809-bound (non-empty worktree + typed pins) hibernated/released
        # row whose pair is LIVE is NOT reconcilable — the reconcile targets ONLY the empty-binding
        # legacy row; a bound row retires through the ordinary #13754 guarded close.
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore

        store = LaneLifecycleStore()
        key = self._key()
        store.declare_active(key, decision=_decision(), issue_id=_ISSUE, worktree_identity="")
        LaneDeclarationStore().backfill_active_binding(
            key, expected_revision=store.get(key).revision, issue_id=_ISSUE,
            worktree_identity=self._token(), declared_slots=_pins(),
        )
        rec = store.get(key)
        store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED, decision=_decision(),
        )
        rec = store.get(key)
        store.request_release(
            key, expected_revision=rec.revision, action_id="rel-1",
            pins=[ReleasePin("gateway", "codex-mzb1", "w1:p1"),
                  ReleasePin("worker", "claude-mzb1", "w1:p2")],
        )
        rec = store.get(key)
        store.record_release_outcome(
            key, action_id="rel-1", expected_revision=rec.revision, target=RELEASE_RELEASED,
        )
        # Pair live (p3/p4).
        result = self._run()
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_NOT_RECONCILABLE_STATE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    def test_close_time_busy_withholds_no_close(self) -> None:
        # Review j#79320 R2: an agent that starts working BETWEEN the initial green observation and
        # the close-time re-observation must NOT be closed. The close re-runs the full pair
        # decision; a busy agent -> zero-close, withheld.
        self._seed_row()
        ops = _FakeReconcileOps(lambda: self.rows, busy_from_call=1)  # idle at obs 0, busy after
        result = self._run(ops=ops)
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_CLOSE_FAILED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)  # retire-first happened
        # The busy pair was NOT closed.
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    def test_close_time_recycled_at_reobserve_no_close(self) -> None:
        # Review j#79320 R2: the pair recycles to NEW locators (p3/p4 -> p6/p7) BEFORE the close.
        # The close-time re-observe sees the pair at different locators than the pinned ones ->
        # zero-close; the newer generation is NOT closed.
        self._seed_row()
        recycled = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p6"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p7"),
        ]
        ops = _FakeReconcileOps(lambda: self.rows, rows_by_call={1: lambda: recycled, 2: lambda: recycled})
        result = self._run(ops=ops)
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_CLOSE_FAILED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    def test_close_time_recycled_after_close_no_false_success(self) -> None:
        # Review j#79320 R3: the pinned pair is closed cleanly, but a NEWER generation appears
        # (p8/p9) AFTER the close. The post-close whole-unit measure sees the expected pair still
        # live at new locators -> withholds success (never a false success off "old pins gone").
        self._seed_row()
        # call 0 = initial (p3/p4); call 1 (close re-observe) = p3/p4 (match -> close them);
        # call 2 (post-close measure) = a recycled newer pair at p8/p9.
        post = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p8"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p9"),
        ]
        ops = _FakeReconcileOps(lambda: self.rows, rows_by_call={2: lambda: post})
        result = self._run(ops=ops)
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_CLOSE_FAILED)  # withheld: newer pair still live
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # The pinned pair WAS closed (p3/p4 gone from the live inventory), but success is withheld
        # because the whole-unit measure found a newer pair live.
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2"})

    def test_owed_close_absent_after_close_is_idempotent_already(self) -> None:
        # After the retire CAS AND the close, a replay sees retired + positive absence -> an
        # idempotent no-op (no duplicate close).
        self._seed_row()
        self._retire_first()
        self.rows = [r for r in self.rows if r["pane_id"] in ("w28:p1", "w28:p2")]
        result = self._run()
        self.assertEqual(result.state, RECONCILE_ALREADY)
        self.assertTrue(result.ok)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_hibernated_bound_absent_is_not_reconcilable(self) -> None:
        # Review j#79282 R1 + j#79320 R1: a hibernated + BOUND row (non-empty worktree + typed
        # pins) is not the reconcile's empty-binding legacy target, so it is refused as
        # not_reconcilable — never retired — regardless of its decision pointer (no same-pointer
        # collision). Verified with BOTH a same and a different pointer.
        from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore

        for journal in (_JOURNAL, "70002"):  # same as reconcile --journal, and different
            with self.subTest(journal=journal):
                LaneLifecycleStore(path=None)  # ensure home is set
                key = self._key()
                # Build a #13809-style bound-then-hibernated row (backfill leaves the decision
                # as the declare/hibernate journal; here we seed it directly at `journal`).
                store = LaneLifecycleStore()
                dec = _decision(journal=journal)
                store.declare_active(key, decision=dec, issue_id=_ISSUE, worktree_identity="")
                LaneDeclarationStore().backfill_active_binding(
                    key,
                    expected_revision=store.get(key).revision,
                    issue_id=_ISSUE,
                    worktree_identity=self._token(),
                    declared_slots=_pins(),
                )
                rec = store.get(key)
                store.transition_disposition(
                    key, expected_disposition=DISPOSITION_ACTIVE,
                    expected_revision=rec.revision, target=DISPOSITION_HIBERNATED, decision=dec,
                )
                rec = store.get(key)
                store.request_release(
                    key, expected_revision=rec.revision, action_id="rel-1",
                    pins=[ReleasePin("gateway", "codex-mzb1", "w1:p1"),
                          ReleasePin("worker", "claude-mzb1", "w1:p2")],
                )
                rec = store.get(key)
                store.record_release_outcome(
                    key, action_id="rel-1", expected_revision=rec.revision,
                    target=RELEASE_RELEASED,
                )
                # Pair absent.
                self.rows = [
                    _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
                    _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
                ]
                result = self._run()
                self.assertEqual(result.state, RECONCILE_BLOCKED)
                self.assertEqual(result.reason, RECON_NOT_RECONCILABLE_STATE)
                self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_close_time_duplicate_is_not_closed(self) -> None:
        # Review j#79244 F2 (still holds under retire-first): a duplicate codex appears between the
        # initial green observation and the close. The pin-matched close refuses the ambiguous
        # name (plan None) -> NEITHER codex locator is closed; the pair stays live, so the retired
        # lane's owed close is reported incomplete (resumable). The row IS retired (retire-first,
        # on the verified initial pair), but no wrong/duplicate generation is closed.
        self._seed_row()
        dup = list(self.rows) + [_row(_WORKSPACE_ID, "codex", _LANE, "w28:p30")]
        ops = _FakeReconcileOps(
            lambda: self.rows, rows_by_call={1: lambda: dup, 2: lambda: dup}
        )
        result = self._run(ops=ops)
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_CLOSE_FAILED)
        # Retired (retire-first) but nothing closed: both codex locators survive.
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    def test_rehydrate_before_retire_cas_is_revision_race_zero_close(self) -> None:
        # Review j#79282 R2: a concurrent rehydrate (hibernated -> active) between the reconcile's
        # row snapshot and the retire CAS bumps the revision. Because the retire (terminal write)
        # runs BEFORE the close, the CAS refuses (revision_race) and NOTHING is closed — the lane
        # stays active and its pair is untouched (zero-write AND zero-close).
        self._seed_row()

        def _rehydrate():
            store = LaneLifecycleStore()
            cur = store.get(self._key())
            store.transition_disposition(
                self._key(),
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=cur.revision,
                target=DISPOSITION_ACTIVE,
                decision=_decision(),
            )

        # Fire the rehydrate on the initial inventory read (call 0), after the reconcile has
        # already snapshotted the hibernated row but before the retire CAS.
        ops = _FakeReconcileOps(lambda: self.rows, on_call={0: _rehydrate})
        result = self._run(ops=ops)
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_REVISION_RACE)
        self.assertEqual(self._disposition(), DISPOSITION_ACTIVE)
        # ZERO-CLOSE: the pair is untouched.
        self.assertEqual({r["pane_id"] for r in self.rows}, {"w28:p1", "w28:p2", "w28:p3", "w28:p4"})

    # -- the fail-closed conditions --------------------------------------

    def test_not_idle_pair_blocks_zero_write(self) -> None:
        self._seed_row()
        result = self._run(ops=_FakeReconcileOps(lambda: self.rows, runtime=RUNTIME_BUSY))
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_AGENT_NOT_IDLE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)
        # Zero-close: the pair is untouched.
        self.assertEqual(len(self.rows), 4)

    def test_pending_composer_blocks_zero_write(self) -> None:
        self._seed_row()
        result = self._run(ops=_FakeReconcileOps(lambda: self.rows, has_pending=True))
        self.assertEqual(result.reason, RECON_PENDING_COMPOSER)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_unattested_slot_blocks_zero_write(self) -> None:
        self._seed_row()
        result = self._run(ops=_FakeReconcileOps(lambda: self.rows, attest=False))
        self.assertEqual(result.reason, RECON_IDENTITY_UNATTESTED)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_stale_shell_residue_blocks(self) -> None:
        # The lane's codex slot is a shell residue (agent field blank) -> stale.
        self.rows = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3", agent=""),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
        ]
        self.rows[2]["agent_status"] = "unknown"
        self._seed_row()
        result = self._run()
        self.assertEqual(result.reason, RECON_SLOT_STALE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_foreign_provider_at_position_blocks(self) -> None:
        # A non-managed provider (gemini) stands at the lane's own position -> substitution.
        self.rows.append(_row(_WORKSPACE_ID, "gemini", _LANE, "w28:p5"))
        self._seed_row()
        result = self._run()
        self.assertEqual(result.reason, RECON_FOREIGN_PROVIDER)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_live_pair_absent_with_empty_binding_routes_to_migration(self) -> None:
        # No live lane pair AND the row was never re-bound -> not the reconcile's job.
        self.rows = [r for r in self.rows if r["pane_id"] in ("w28:p1", "w28:p2")]
        self._seed_row()
        result = self._run()
        self.assertEqual(result.reason, RECON_LIVE_PAIR_ABSENT)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_branch_mismatch_blocks(self) -> None:
        self._seed_row()
        result = self._run(branch="main", worktree_branch=_LANE)
        self.assertEqual(result.reason, RECON_WORKTREE_BRANCH_MISMATCH)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_unintegrated_head_blocks(self) -> None:
        self._seed_row()
        result = self._run(head_integrated=False)
        self.assertEqual(result.reason, RECON_HEAD_NOT_INTEGRATED)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_inventory_unreadable_blocks(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        self._seed_row()

        class _Raising:
            def agent_rows(self):
                raise HerdrSessionStartError("herdr down")

            def runtime_state(self, locator):
                return RUNTIME_AWAITING_INPUT

            def observe_composer(self, locator):
                return (True, False)

            def read_attestation(self, name):
                return None

        result = self._run(ops=_Raising())
        self.assertEqual(result.reason, REASON_INVENTORY_UNREADABLE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_active_row_is_not_reconcilable(self) -> None:
        # An active #13809-backfillable row is not the reconcile's target.
        LaneLifecycleStore().declare_active(
            self._key(), decision=_decision(), issue_id=_ISSUE, worktree_identity=""
        )
        result = self._run()
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_NOT_RECONCILABLE_STATE)
        self.assertEqual(self._disposition(), DISPOSITION_ACTIVE)

    def test_retired_row_with_recycled_pair_withholds_success(self) -> None:
        # A persisted retired row does not prove non-liveness (review j#79150 F2, applied to the
        # reconcile): a RECYCLED generation (same names, DIFFERENT locators than the recorded
        # owed-close pins) is not the reconcile's owed close, so the idempotent success is
        # withheld — the reconcile never closes a generation it did not verify.
        self._seed_row()
        self._retire_first()  # retired + recorded pins at p3/p4, close still owed
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # The recorded pair vanished and a recycled pair reappeared at DIFFERENT locators.
        self.rows = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p6"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p7"),
        ]
        result = self._run()
        self.assertEqual(result.state, RECONCILE_BLOCKED)
        self.assertEqual(result.reason, RECON_LIVE_PAIR_PRESENT)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_not_on_herdr_backend_returns_none(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        _git_init = plain / ".git"  # not a herdr repo
        plain.mkdir()
        result = run_hibernated_live_reconcile(
            self._args(),
            plain,
            head_integrated=True,
            worktree_branch=_LANE,
            ops=_FakeReconcileOps(lambda: self.rows),
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 4. The command boundary + non-regression of the #13754 / #13841 paths.
# ---------------------------------------------------------------------------


class ReconcileCommandTests(unittest.TestCase):
    """``sublane retire --reconcile-hibernated-live`` command wiring (no live-ops needed)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.home.mkdir()
        self.primary = tmp / "primary"
        _init_repo(self.primary, anchor=True)
        self.lane_worktree = tmp / "lane_wt"
        _git("worktree", "add", "-b", _LANE, str(self.lane_worktree), "main", cwd=self.primary)

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        # Coordinator-only inventory: the lane unit measures ZERO live managed slots (so the
        # command wiring is exercised without needing the live runtime / composer probes).
        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
        ]
        self.rows_error: Exception | None = None
        self._real_rows = sublane_herdr_projection.list_herdr_agent_rows

        def fake_rows(env):
            if self.rows_error is not None:
                raise self.rows_error
            return list(self.rows)

        sublane_herdr_projection.list_herdr_agent_rows = fake_rows

        def _restore():
            sublane_herdr_projection.list_herdr_agent_rows = self._real_rows
            if self._prev_home is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
            self._tmp.cleanup()

        self.addCleanup(_restore)

    def _key(self) -> LaneLifecycleKey:
        return LaneLifecycleKey(_WORKSPACE_ID, _LANE)

    def _seed_row(self, **kwargs) -> None:
        _seed_hibernated_released(LaneLifecycleStore(), key=self._key(), **kwargs)

    def _disposition(self) -> str:
        rec = LaneLifecycleStore().get(self._key())
        return "" if rec is None else rec.lane_disposition

    def _base_args(self, **over) -> dict:
        base = dict(
            repo=str(self.primary),
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_worktree),
            branch=_LANE,
            integration_branch="main",
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=True,
            json=True,
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
        )
        base.update(over)
        return base

    def _run_cmd(self, **over):
        args = argparse.Namespace(**self._base_args(**over))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return code, payload, err.getvalue()

    def _rec(self, payload) -> dict:
        return payload.get("hibernated_live_reconcile", {})

    def test_live_pair_absent_blocks_via_command(self) -> None:
        # Coordinator-only inventory (no lane pair) + empty binding -> live_pair_absent.
        self._seed_row()
        code, payload, _ = self._run_cmd()
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self._rec(payload)["reason"], RECON_LIVE_PAIR_ABSENT)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_preflight_blocked_never_runs_reconcile(self) -> None:
        self._seed_row()
        code, payload, _ = self._run_cmd(issue_closed=False)
        self.assertEqual(code, 1)
        self.assertNotIn("hibernated_live_reconcile", payload)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_branch_mismatch_via_command(self) -> None:
        self._seed_row()
        code, payload, _ = self._run_cmd(branch="main")
        self.assertEqual(code, 1)
        self.assertEqual(self._rec(payload)["reason"], RECON_WORKTREE_BRANCH_MISMATCH)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_inventory_unreadable_via_command(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        self._seed_row()
        self.rows_error = HerdrSessionStartError("herdr down")
        code, payload, _ = self._run_cmd()
        self.assertEqual(code, 1)
        self.assertEqual(self._rec(payload)["reason"], REASON_INVENTORY_UNREADABLE)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_three_way_mutual_exclusion_reconcile_and_execute(self) -> None:
        self._seed_row()
        code, payload, err = self._run_cmd(execute=True)
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err)
        self.assertIsNone(payload)  # no JSON, no actuation
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_three_way_mutual_exclusion_reconcile_and_migrate(self) -> None:
        self._seed_row()
        code, payload, err = self._run_cmd(migrate_hibernated_legacy=True)
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err)
        self.assertIsNone(payload)
        self.assertEqual(self._disposition(), DISPOSITION_HIBERNATED)

    def test_plain_retire_without_reconcile_flag_is_unchanged(self) -> None:
        # Regression: a plain preflight retire (no intent flag) still runs and never touches
        # the reconcile surface.
        self._seed_row()
        code, payload, _ = self._run_cmd(reconcile_hibernated_live=False)
        self.assertNotIn("hibernated_live_reconcile", payload)


if __name__ == "__main__":
    unittest.main()
