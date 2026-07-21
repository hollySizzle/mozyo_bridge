"""Redmine #14242 — an ACTIVE live-zero closed lane must converge to terminal `retired`.

Live evidence #14222 j#85208-j#85209: the lane's issue and children were closed, owner close /
review / integration / CI were green, the worktree was clean and its head an ancestor of
`origin/main-next`, and `sublane list` reported `state=detached` / `panes=[]`. Yet:

- `retire --execute` returned `zero_close_unproven` / `closed: []` / `durable_retirement: ""`
  (correct — nothing to close, and a zero-close is only a retire when the row already says
  `retired` — but no convergence path);
- `--retire-hibernated-bound` returned `not_hibernated_bound_state` (correct — its CAS requires
  `hibernated` AND `process_release == released`).

The row stayed `active` forever. This suite pins the fourth intent that closes that gap, and —
just as importantly — pins that it does NOT erode the three surfaces above.

The structural risk this suite exists to police: an ACTIVE row has
`process_release == not_requested`, so unlike #13845 there is **no durable release witness** to
pair with the live-zero read. The inventory read is the only liveness authority, so every
ambiguity (unreadable / duplicate / locator-less / foreign) must be a zero-write refusal, and the
CAS must be fenced on the revision the read was measured against.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_active_retire import LaneActiveRetireStore
from mozyo_bridge.core.state.lane_bound_retire import LaneBoundRetireStore
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    DecisionPointer,
    LaneLifecycleKey,
    ProcessGenerationPin,
    ReleasePin,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

_WORKSPACE_ID = "wProj"
_LANE = "issue_14222_silent_defaults_us_lane"
_ISSUE = "14222"
_JOURNAL = "85209"
_BOUND_WT = "wt_14222_lane"
_OTHER_WT = "wt_some_other_lane"


def _key(ws: str = _WORKSPACE_ID, lane: str = _LANE) -> LaneLifecycleKey:
    return LaneLifecycleKey(ws, lane)


def _decision(issue: str = _ISSUE, journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _pins() -> tuple[ProcessGenerationPin, ...]:
    return (
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "codex", _LANE),
            locator="w2X:p3Q",
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "claude", _LANE),
            locator="w2X:p3R",
        ),
    )


def _seed_active_bound(
    *,
    path: Path | None,
    key: LaneLifecycleKey,
    issue: str = _ISSUE,
    worktree_identity: str = _BOUND_WT,
    release_target: str | None = None,
) -> None:
    """Declare an ACTIVE bound row through the REAL store, optionally driving a release.

    A freshly declared lane is `active` with `process_release == not_requested` — exactly the
    #14222 shape. `release_target` drives the in-flight shapes this surface must refuse.
    """
    dec = _decision(issue)
    lifecycle = LaneLifecycleStore(path=path)
    declaration = LaneDeclarationStore(path=path)
    out = declaration.declare_lane(
        key,
        decision=dec,
        issue_id=issue,
        declared_slots=_pins(),
        worktree_identity=worktree_identity,
    )
    assert out.applied, f"seed declare_lane refused: {out.reason}"
    if release_target is None:
        return
    rec = lifecycle.get(key)
    lifecycle.request_release(
        key,
        expected_revision=rec.revision,
        action_id="rel-1",
        pins=[
            ReleasePin("gateway", "codex-mzb1", "w2X:p3Q"),
            ReleasePin("worker", "claude-mzb1", "w2X:p3R"),
        ],
    )
    if release_target == RELEASE_REQUESTED:
        return
    rec = lifecycle.get(key)
    lifecycle.record_release_outcome(
        key, action_id="rel-1", expected_revision=rec.revision, target=release_target
    )


def _force_release_state(path: Path, key: LaneLifecycleKey, state: str) -> None:
    """White-box: set `process_release` directly, WITHOUT touching `revision`.

    The real transitions cannot put an `active` row into an in-flight release (see
    `test_release_cannot_even_be_requested_on_an_active_row`), so the CAS's release backstop is
    otherwise untestable. The revision is left alone so the CAS still sees the revision the
    caller measured against, isolating the release guard as the only thing under test.
    """
    import sqlite3

    from mozyo_bridge.core.state.lane_lifecycle_schema import TABLE

    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            f"UPDATE {TABLE} SET process_release = ? "
            "WHERE repo_workspace_id = ? AND lane_id = ?",
            (state, key.repo_workspace_id, key.lane_id),
        )
        conn.commit()
    finally:
        conn.close()


def _force_worktree_identity(path: Path | None, key: LaneLifecycleKey, token: str) -> None:
    """White-box: repoint the row's recorded worktree binding, leaving `revision` alone."""
    import sqlite3

    from mozyo_bridge.core.state.lane_lifecycle_schema import TABLE

    target = path or LaneLifecycleStore().path
    conn = sqlite3.connect(str(target))
    try:
        conn.execute(
            f"UPDATE {TABLE} SET worktree_identity = ? "
            "WHERE repo_workspace_id = ? AND lane_id = ?",
            (token, key.repo_workspace_id, key.lane_id),
        )
        conn.commit()
    finally:
        conn.close()


def _hibernate(path: Path | None, key: LaneLifecycleKey) -> None:
    lifecycle = LaneLifecycleStore(path=path)
    rec = lifecycle.get(key)
    lifecycle.transition_disposition(
        key,
        expected_disposition=DISPOSITION_ACTIVE,
        expected_revision=rec.revision,
        target=DISPOSITION_HIBERNATED,
        decision=_decision(),
    )


# ---------------------------------------------------------------------------
# 1. The bounded store CAS guard matrix (pure of the CLI).
# ---------------------------------------------------------------------------


class ActiveRetireCasMatrix(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lifecycle.sqlite"
        self.store = LaneActiveRetireStore(path=self.path)

    def _retire(self, key=None, *, issue=_ISSUE, worktree=_BOUND_WT, revision=None):
        key = key or _key()
        if revision is None:
            revision = LaneLifecycleStore(path=self.path).get(key).revision
        return self.store.retire_active_live_zero(
            key,
            expected_revision=revision,
            issue_id=issue,
            worktree_identity=worktree,
            decision=_decision(issue),
        )

    def test_active_bound_row_terminalizes(self):
        _seed_active_bound(path=self.path, key=_key())
        out = self._retire()
        self.assertTrue(out.applied, out.reason)
        rec = LaneLifecycleStore(path=self.path).get(_key())
        self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)

    def test_terminalize_preserves_binding_pins_and_generation(self):
        _seed_active_bound(path=self.path, key=_key())
        before = LaneLifecycleStore(path=self.path).get(_key())
        self.assertTrue(self._retire().applied)
        after = LaneLifecycleStore(path=self.path).get(_key())
        self.assertEqual(after.worktree_identity, before.worktree_identity)
        self.assertEqual(after.declared_slots, before.declared_slots)
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.process_release, before.process_release)
        self.assertEqual(after.reconcile_phase, before.reconcile_phase)
        self.assertEqual(after.revision, before.revision + 1)
        self.assertEqual(after.decision_journal, _JOURNAL)

    def test_missing_row_is_not_found(self):
        self.assertEqual(
            self._retire(revision=1).reason, CAS_NOT_FOUND
        )

    def test_stale_revision_loses(self):
        # The live-zero measurement was taken against a revision that is no longer current.
        _seed_active_bound(path=self.path, key=_key())
        rec = LaneLifecycleStore(path=self.path).get(_key())
        out = self._retire(revision=rec.revision + 5)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(
            LaneLifecycleStore(path=self.path).get(_key()).lane_disposition,
            DISPOSITION_ACTIVE,
        )

    def test_hibernated_row_is_refused(self):
        # A hibernated row is #13845 / #13841 / #13842's target, never this surface's.
        _seed_active_bound(path=self.path, key=_key())
        _hibernate(self.path, _key())
        self.assertEqual(self._retire().reason, CAS_UNEXPECTED_STATE)

    def test_already_retired_row_is_refused_by_the_cas(self):
        # The CAS stays strictly active -> retired; idempotency is the caller's, gated on a
        # fresh live-zero read.
        _seed_active_bound(path=self.path, key=_key())
        self.assertTrue(self._retire().applied)
        rec = LaneLifecycleStore(path=self.path).get(_key())
        self.assertEqual(self._retire(revision=rec.revision).reason, CAS_UNEXPECTED_STATE)

    def test_empty_worktree_binding_is_the_legacy_signature_not_this_one(self):
        _seed_active_bound(path=self.path, key=_key(), worktree_identity="")
        self.assertEqual(self._retire().reason, CAS_UNEXPECTED_STATE)

    def test_mismatched_worktree_binding_is_refused(self):
        _seed_active_bound(path=self.path, key=_key())
        self.assertEqual(self._retire(worktree=_OTHER_WT).reason, CAS_UNEXPECTED_STATE)

    def test_different_issue_is_refused(self):
        _seed_active_bound(path=self.path, key=_key(), issue="99999")
        self.assertEqual(self._retire(issue=_ISSUE).reason, CAS_UNEXPECTED_STATE)

    def test_release_cannot_even_be_requested_on_an_active_row(self):
        # WHY the in-flight release states are unreachable for this surface's shape, and why
        # `process_release` therefore cannot be a second liveness witness here: `request_release`
        # itself refuses an `active` row. Pinned so a future relaxation of that transition is
        # noticed here rather than silently making the guard below load-bearing in production.
        _seed_active_bound(path=self.path, key=_key())
        rec = LaneLifecycleStore(path=self.path).get(_key())
        out = LaneLifecycleStore(path=self.path).request_release(
            _key(),
            expected_revision=rec.revision,
            action_id="rel-1",
            pins=[ReleasePin("gateway", "codex-mzb1", "w2X:p3Q")],
        )
        self.assertFalse(out.applied)
        self.assertEqual(
            LaneLifecycleStore(path=self.path).get(_key()).process_release,
            "not_requested",
        )

    def test_in_flight_release_backstop_refuses(self):
        # The guard is unreachable through the real transitions (above), so it is exercised
        # white-box: force the column to each in-flight state and assert the CAS still refuses.
        # A corrupted row, or a future transition that permits release-on-active, must not
        # terminalize while an actuator may be closing panes.
        for target in (RELEASE_REQUESTED, RELEASE_PARTIAL):
            with self.subTest(release=target):
                path = Path(self.tmp.name) / f"lc_{target}.sqlite"
                _seed_active_bound(path=path, key=_key())
                rec = LaneLifecycleStore(path=path).get(_key())
                _force_release_state(path, _key(), target)
                out = LaneActiveRetireStore(path=path).retire_active_live_zero(
                    _key(),
                    expected_revision=rec.revision,
                    issue_id=_ISSUE,
                    worktree_identity=_BOUND_WT,
                    decision=_decision(),
                )
                self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
                self.assertEqual(
                    LaneLifecycleStore(path=path).get(_key()).lane_disposition,
                    DISPOSITION_ACTIVE,
                )

    def test_released_on_an_active_row_is_refused_as_unreachable(self):
        # Review j#85219 F2. An earlier revision ADMITTED `released` here, reasoning it was the
        # residue of a completed release. It is not reachable: `request_release` refuses an
        # active row (`unexpected_state`) and `record_release_outcome` refuses
        # (`action_generation_mismatch`) — both measured in
        # `test_active_row_cannot_reach_released_through_public_transitions`. An unreachable
        # shape is a corrupted row, and a surface premised on having NO release witness must not
        # grant extra permission to one. Fail closed.
        path = Path(self.tmp.name) / "lc_released.sqlite"
        _seed_active_bound(path=path, key=_key())
        rec = LaneLifecycleStore(path=path).get(_key())
        _force_release_state(path, _key(), RELEASE_RELEASED)
        out = LaneActiveRetireStore(path=path).retire_active_live_zero(
            _key(),
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(
            LaneLifecycleStore(path=path).get(_key()).lane_disposition, DISPOSITION_ACTIVE
        )

    def test_active_row_cannot_reach_released_through_public_transitions(self):
        # The reachability measurement F2 turns on, pinned so a future transition change that
        # DOES make `active + released` legitimate is noticed here.
        _seed_active_bound(path=self.path, key=_key())
        lifecycle = LaneLifecycleStore(path=self.path)
        rec = lifecycle.get(_key())
        req = lifecycle.request_release(
            _key(), expected_revision=rec.revision, action_id="rel-1",
            pins=[ReleasePin("gateway", "codex-mzb1", "w2X:p3Q")],
        )
        self.assertFalse(req.applied)
        rec = lifecycle.get(_key())
        rec_out = lifecycle.record_release_outcome(
            _key(), action_id="rel-1", expected_revision=rec.revision, target=RELEASE_RELEASED
        )
        self.assertFalse(rec_out.applied)
        self.assertEqual(lifecycle.get(_key()).process_release, "not_requested")

    def test_empty_issue_or_worktree_token_raises(self):
        _seed_active_bound(path=self.path, key=_key())
        rec = LaneLifecycleStore(path=self.path).get(_key())
        for issue, wt in (("", _BOUND_WT), (_ISSUE, "")):
            with self.subTest(issue=issue, worktree=wt):
                with self.assertRaises(ValueError):
                    self.store.retire_active_live_zero(
                        _key(),
                        expected_revision=rec.revision,
                        issue_id=issue,
                        worktree_identity=wt,
                        decision=_decision(),
                    )


# ---------------------------------------------------------------------------
# 1b. The OPEN launch race (Redmine #14242 review j#85219 F1).
# ---------------------------------------------------------------------------


class LaunchExclusionTest(unittest.TestCase):
    """The launch / terminalize exclusion F1 required (review j#85219, answer j#85269).

    The revision fence alone cannot see a process relaunch, so the exclusion is the home's
    attestation-store lock: every managed launch holds it SHARED (non-blocking) from before its
    first attestation read through its last actuation, and the terminalizer holds it EXCLUSIVE
    across its whole action-time half.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lifecycle.sqlite"
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def test_relaunch_does_not_advance_the_lifecycle_revision(self):
        # WHY the exclusion is needed: the measurement F1 turns on. A relaunch-shaped
        # declaration leaves `revision` untouched, so a CAS fenced on it is blind to launches.
        _seed_active_bound(path=self.path, key=_key())
        lifecycle = LaneLifecycleStore(path=self.path)
        before = lifecycle.get(_key()).revision
        declared = lifecycle.declare_active(
            _key(), decision=_decision(), issue_id=_ISSUE, worktree_identity=_BOUND_WT
        )
        self.assertFalse(declared.applied)
        LaneDeclarationStore(path=self.path).declare_lane(
            _key(), decision=_decision(), issue_id=_ISSUE,
            declared_slots=_pins(), worktree_identity=_BOUND_WT,
        )
        self.assertEqual(lifecycle.get(_key()).revision, before)

    def test_a_launch_holding_shared_blocks_the_terminalize(self):
        # Required test 1: launch-shared first -> terminalize zero-write.
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            attestation_store_lock,
        )

        with attestation_store_lock(self.home, exclusive=False, blocking=False):
            with self.assertRaises(Exception) as caught:
                with attestation_store_lock(self.home, exclusive=True, blocking=False):
                    self.fail("the terminalizer must not acquire while a launch holds shared")
        self.assertIn("Busy", type(caught.exception).__name__)

    def test_a_terminalize_holding_exclusive_blocks_every_launch(self):
        # Required test 2: terminalize-exclusive first -> the SHARED acquire every managed
        # launch performs fails, so no workspace / tab / agent is ever created. Exercised on the
        # shared primitive itself because every spawn path (ordinary create / heal, the v1
        # replacement binding, quarantine heal_receiver, and the bare / scratch / shared-space
        # session starts) funnels through the same shared acquisition.
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            attestation_store_lock,
        )

        with attestation_store_lock(self.home, exclusive=True, blocking=False):
            with self.assertRaises(Exception) as caught:
                with attestation_store_lock(self.home, exclusive=False, blocking=False):
                    self.fail("a launch must not acquire while a terminalize holds exclusive")
        self.assertIn("Busy", type(caught.exception).__name__)

    def test_every_managed_launch_funnel_takes_the_shared_lock(self):
        # Structural: the exclusion only holds because the launch funnels acquire the shared
        # lock. Pinned by source inspection so a future spawn path that bypasses admission is
        # caught here rather than discovered as a live mis-terminalize.
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_actuator_herdr_ops,
        )

        funnel = inspect.getsource(herdr_session_start.prepare_session)
        self.assertIn("attestation_store_lock(", funnel)
        self.assertIn("exclusive=False", funnel)
        # The v1 replacement binding reaches `_prepare_session_locked` DIRECTLY with
        # admission_lock_held=True, so its caller must hold the same shared lock.
        v1_caller = inspect.getsource(sublane_actuator_herdr_ops.HerdrSublaneActuatorOps.heal_lane_column)
        self.assertIn("attestation_store_lock(", v1_caller)
        self.assertIn("exclusive=False", v1_caller)

    def test_terminalizer_reports_launch_in_flight_and_writes_nothing(self):
        # Required test: lock busy -> typed blocked, lifecycle row zero-write.
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            AttestationStoreLockBusy,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_active_live_zero_retire as active_module,
        )

        _seed_active_bound(path=self.path, key=_key())

        @contextlib.contextmanager
        def _busy(*_a, **_k):
            raise AttestationStoreLockBusy("a managed launch holds the store")
            yield  # pragma: no cover

        with mock.patch(
            "mozyo_bridge.core.state.herdr_identity_attestation_schema."
            "attestation_store_lock",
            side_effect=_busy,
        ):
            verdict = self._run_terminalizer()
        self.assertEqual(verdict.state, "blocked")
        self.assertEqual(verdict.reason, "launch_in_flight")
        self.assertEqual(
            LaneLifecycleStore(path=self.path).get(_key()).lane_disposition,
            DISPOSITION_ACTIVE,
        )

    def test_terminalizer_reports_exclusion_unavailable(self):
        # Required test: no advisory locking -> typed blocked, never proceed unlocked.
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            AttestationStoreLockUnavailable,
        )

        _seed_active_bound(path=self.path, key=_key())

        @contextlib.contextmanager
        def _unavailable(*_a, **_k):
            raise AttestationStoreLockUnavailable("fcntl.flock is unavailable")
            yield  # pragma: no cover

        with mock.patch(
            "mozyo_bridge.core.state.herdr_identity_attestation_schema."
            "attestation_store_lock",
            side_effect=_unavailable,
        ):
            verdict = self._run_terminalizer()
        self.assertEqual(verdict.state, "blocked")
        self.assertEqual(verdict.reason, "exclusion_unavailable")
        self.assertEqual(
            LaneLifecycleStore(path=self.path).get(_key()).lane_disposition,
            DISPOSITION_ACTIVE,
        )

    def test_real_terminalizer_blocks_against_a_genuinely_held_launch_lock(self):
        """The end-to-end invariant: a launch holding shared makes the REAL terminalizer
        zero-write, promptly.

        Drives the actual `run_active_live_zero_retire` (not the lock primitive) while a
        launch-shaped SHARED acquisition is held on the SAME home, so the terminalizer's own
        `exclusive=True, blocking=False` arguments are what is under test:

        - `exclusive=False` would let it acquire alongside the launch and proceed -> the
          reason would not be `launch_in_flight`;
        - `blocking=True` would make it queue behind the launch instead of failing fast -> it
          would not return within the timeout.

        Run on a worker thread with a join timeout so a blocking regression surfaces as a test
        failure rather than a hung suite.
        """
        import threading

        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            attestation_store_lock,
        )

        _seed_active_bound(path=self.path, key=_key())
        home = Path(self.tmp.name) / "lockhome"
        home.mkdir()
        box: dict = {}

        def _drive():
            try:
                box["verdict"] = self._run_terminalizer()
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions below
                box["error"] = exc

        with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}):
            # A managed launch holds the home's store SHARED, exactly as admission does.
            with attestation_store_lock(home, exclusive=False, blocking=False):
                worker = threading.Thread(target=_drive, daemon=True)
                worker.start()
                worker.join(timeout=10)
                self.assertFalse(
                    worker.is_alive(),
                    "the terminalizer queued behind an in-flight launch instead of failing "
                    "fast; it must be non-blocking",
                )
        self.assertNotIn("error", box, f"terminalizer raised: {box.get('error')!r}")
        verdict = box["verdict"]
        self.assertEqual(verdict.state, "blocked", verdict)
        self.assertEqual(verdict.reason, "launch_in_flight", verdict)
        self.assertEqual(
            LaneLifecycleStore(path=self.path).get(_key()).lane_disposition,
            DISPOSITION_ACTIVE,
            "a lane must stay active while a launch is in flight",
        )

    def _run_terminalizer(self):
        """Drive the terminalizer far enough to reach the exclusion boundary."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_active_live_zero_retire as active_module,
            sublane_herdr_projection as projection,
            sublane_retire_actuation as retire_actuation,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )

        args = argparse.Namespace(
            issue=_ISSUE, journal=_JOURNAL, lane_label=_LANE,
            worktree=str(self.home), branch=_LANE, integration_branch="main",
        )
        with mock.patch.object(projection, "repo_backend_is_herdr", return_value=True), \
             mock.patch.object(
                 herdr_session_start, "herdr_workspace_segment", return_value=_WORKSPACE_ID
             ), \
             mock.patch.object(
                 retire_actuation, "attest_retire_target", return_value=(True, "", "")
             ):
            return active_module.run_active_live_zero_retire(
                args, Path(self.tmp.name) / "repo",
                head_integrated=True, worktree_branch=_LANE,
            )


# ---------------------------------------------------------------------------
# 1c. The post-terminal launch admission (review j#85296 F3) — the ORDER half.
# ---------------------------------------------------------------------------


class PostTerminalLaunchAdmissionTest(unittest.TestCase):
    """The exclusion's second half: order, not just concurrency (review j#85296 F3).

    The attestation-store lock serializes launch and terminalize while both are in flight, but
    it is released once the terminal CAS commits. Without a durable-disposition admission an
    ordinary `prepare_session` could then acquire the shared lock and spawn into the lane that
    was just terminalized — a live pair under a `retired` row.

    The four ordered cases the review requires are pinned here as 1-4.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def _admit(self, lane: str = _LANE):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            admit_launch_against_lifecycle,
        )

        return admit_launch_against_lifecycle(
            workspace_id=_WORKSPACE_ID, lane_id=lane, store_home=str(self.home)
        )

    def _retire(self) -> None:
        lifecycle = LaneLifecycleStore(home=self.home)
        rec = lifecycle.get(_key())
        lifecycle.transition_disposition(
            _key(), expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision, target=DISPOSITION_RETIRED, decision=_decision(),
        )

    # -- ordered case 3: terminal complete, lock released, retry -> zero-spawn -------------

    def test_3_retry_after_terminal_and_lock_release_is_zero_spawn(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        _seed_active_bound(path=None, key=_key())  # home-scoped via MOZYO_BRIDGE_HOME below
        with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}):
            _seed_active_bound(path=self.home / "state.sqlite", key=_key())
        self._admit()  # active: launches exactly as before
        self._retire()
        with self.assertRaises(HerdrSessionStartError) as caught:
            self._admit()
        self.assertIn("retired", str(caught.exception))
        self.assertIn("No workspace / tab / agent was created", str(caught.exception))

    # -- ordered case 4: after an explicit re-incarnation -> launch allowed ----------------

    def test_4_launch_allowed_after_open_next_generation(self):
        _seed_active_bound(path=self.home / "state.sqlite", key=_key())
        self._retire()
        lifecycle = LaneLifecycleStore(home=self.home)
        rec = lifecycle.get(_key())
        reopened = LaneDeclarationStore(home=self.home).open_next_generation(
            _key(), expected_revision=rec.revision, expected_generation=rec.lane_generation,
            decision=_decision(), declared_slots=_pins(),
        )
        self.assertTrue(reopened.applied, reopened.reason)
        after = lifecycle.get(_key())
        self.assertEqual(after.lane_disposition, DISPOSITION_ACTIVE)
        self.assertGreater(after.lane_generation, rec.lane_generation)
        self._admit()  # must not raise

    # -- the narrowness of the admission ---------------------------------------------------

    def test_rowless_lane_is_unaffected(self):
        self._admit()  # no row at all -> unchanged behaviour

    def test_default_coordinator_lane_never_consults_the_store(self):
        # The bare `mozyo` / scratch `session-start` pair owns no lifecycle row by design, so it
        # must launch even when the store is unreadable.
        (self.home / "state.sqlite").write_text("not a database", encoding="utf-8")
        self._admit(lane="default")

    def test_active_superseded_and_hibernated_still_launch(self):
        _seed_active_bound(path=self.home / "state.sqlite", key=_key())
        self._admit()
        lifecycle = LaneLifecycleStore(home=self.home)
        rec = lifecycle.get(_key())
        lifecycle.transition_disposition(
            _key(), expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision, target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self._admit()  # a hibernated lane is resumable, not terminal

    def test_unreadable_store_refuses_a_named_lane(self):
        # "unreadable is not absent" — the standing rule. Bounded to named lanes, so the
        # coordinator pair (above) can always still start.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        (self.home / "state.sqlite").write_text("not a database", encoding="utf-8")
        with self.assertRaises(HerdrSessionStartError) as caught:
            self._admit()
        self.assertIn("unreadable", str(caught.exception))

    def test_admission_runs_before_any_herdr_write_on_both_entry_paths(self):
        # Structural: the call must sit in `_prepare_session_locked` (which BOTH the ordinary
        # path and the v1 replacement's direct `admission_lock_held=True` call enter), not in
        # `prepare_session` — otherwise the v1 path bypasses it.
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )

        body = inspect.getsource(herdr_session_start._prepare_session_locked)
        self.assertIn("admit_launch_against_lifecycle(", body)
        # ...and before the slot execution that performs the spawn.
        self.assertLess(
            body.index("admit_launch_against_lifecycle("),
            body.index("_execute_slot("),
            "the admission must precede the spawn",
        )
        self.assertNotIn(
            "admit_launch_against_lifecycle(",
            inspect.getsource(herdr_session_start.prepare_session),
            "placing it on prepare_session would let the v1 direct path bypass it",
        )


# ---------------------------------------------------------------------------
# 2. Non-erosion of the sibling surfaces.
# ---------------------------------------------------------------------------


class ActiveRetireDoesNotErodeSiblings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lifecycle.sqlite"

    def test_13845_still_refuses_an_active_row(self):
        # The #14242 store must be ADDITIVE: #13845's CAS must not have been widened to accept
        # `active`. If it had, an active row could terminalize on a release proof it can never
        # actually supply.
        _seed_active_bound(path=self.path, key=_key())
        rec = LaneLifecycleStore(path=self.path).get(_key())
        out = LaneBoundRetireStore(path=self.path).retire_released_hibernated_bound(
            _key(),
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(
            LaneLifecycleStore(path=self.path).get(_key()).lane_disposition,
            DISPOSITION_ACTIVE,
        )

    def test_14242_refuses_the_13845_target(self):
        # The mirror: a hibernated + released bound row stays #13845's, not this surface's.
        _seed_active_bound(path=self.path, key=_key())
        _hibernate(self.path, _key())
        lifecycle = LaneLifecycleStore(path=self.path)
        rec = lifecycle.get(_key())
        lifecycle.request_release(
            key=_key() if False else _key(),
            expected_revision=rec.revision,
            action_id="rel-1",
            pins=[ReleasePin("gateway", "codex-mzb1", "w2X:p3Q")],
        )
        rec = lifecycle.get(_key())
        lifecycle.record_release_outcome(
            _key(), action_id="rel-1", expected_revision=rec.revision, target=RELEASE_RELEASED
        )
        rec = lifecycle.get(_key())
        out = LaneActiveRetireStore(path=self.path).retire_active_live_zero(
            _key(),
            expected_revision=rec.revision,
            issue_id=_ISSUE,
            worktree_identity=_BOUND_WT,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)


# ---------------------------------------------------------------------------
# 3. The command boundary — intent exclusivity + the #14222 live-shaped rail.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _row(ws: str, role: str, lane: str, locator: str) -> dict:
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class ActiveRetireCommandTests(unittest.TestCase):
    """The command boundary over a real git lane + a fake herdr inventory (isolated home).

    This is the #14222 live-shaped public-rail rerun: a real worktree on its own branch whose
    head is an ancestor of the integration branch, a real ACTIVE bound lifecycle row seeded
    through the real store, and an inventory the test controls.
    """

    def setUp(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
            sublane_herdr_retire as herdr_retire,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@example.com", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git("add", ".", cwd=self.repo)
        _git("commit", "-qm", "base", cwd=self.repo)
        self.worktree = self.root / "lane_wt"
        # Branch off main with NO extra commit, so the lane head is a literal ancestor of the
        # integration branch — the #14222 "integrated head" shape.
        _git("worktree", "add", "-q", "-b", _LANE, str(self.worktree), "main", cwd=self.repo)

        # The canonical worktree token the row must be bound to, derived exactly as the create
        # site records it.
        self.bound_token = derive_lane_workspace_token(str(self.worktree.resolve()))

        for target, attr, value in (
            (projection, "repo_backend_is_herdr", True),
            (herdr_session_start, "herdr_workspace_segment", _WORKSPACE_ID),
        ):
            patcher = mock.patch.object(target, attr, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.inventory: list[dict] = []
        rows_patch = mock.patch.object(
            projection, "list_herdr_agent_rows",
            side_effect=lambda *_a, **_k: list(self.inventory),
        )
        rows_patch.start()
        self.addCleanup(rows_patch.stop)

        # A metadata-only surface must never reach the guarded close actuator.
        self.closes: list = []

        def _no_close(plan, **kwargs):
            self.closes.append(plan)
            raise AssertionError("a metadata-only retire must close nothing")

        close_patch = mock.patch.object(
            herdr_retire, "execute_herdr_retire_close", side_effect=_no_close
        )
        close_patch.start()
        self.addCleanup(close_patch.stop)

        # The durable ACTIVE bound row, seeded through the REAL store into the isolated home.
        _seed_active_bound(path=None, key=_key(), worktree_identity=self.bound_token)

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            issue=_ISSUE, journal=_JOURNAL, lane_label=_LANE,
            worktree=str(self.worktree), branch=_LANE, integration_branch="main",
            issue_closed=True, callbacks_drained=True, verified=True, durable_record=True,
            target_identity_known=True, latest_generation_admissible=True,
            review_generation_json=None, execute=False, migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False, retire_hibernated_bound=False,
            retire_active_live_zero=True, integration_journal=None,
            repo=str(self.repo), json=True,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, **overrides):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_sublane_retire(self._args(**overrides))
        payload = json.loads(buf.getvalue())
        return rc, payload, payload.get("active_live_zero_retire", {})

    def _disposition(self) -> str:
        return LaneLifecycleStore().get(_key()).lane_disposition

    # -- the acceptance: a #14222-shaped lane converges to terminal retired ---------------

    def test_live_zero_lane_terminalizes_to_retired(self):
        self.inventory = []
        rc, payload, verdict = self._run()
        self.assertEqual(rc, 0, verdict)
        self.assertEqual(verdict.get("state"), "retired", verdict)
        self.assertTrue(payload.get("retire_ok"))
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        self.assertEqual(self.closes, [], "no process may be closed")

    def test_duplicate_replay_is_idempotent(self):
        self.inventory = []
        self.assertEqual(self._run()[0], 0)
        rc, _payload, verdict = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(verdict.get("state"), "already_retired")
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    # -- every ambiguity is a zero-write refusal ------------------------------------------

    def _assert_blocked(self, verdict, reason):
        self.assertEqual(verdict.get("state"), "blocked", verdict)
        self.assertEqual(verdict.get("reason"), reason, verdict)
        self.assertEqual(self._disposition(), DISPOSITION_ACTIVE)

    def test_live_pair_present_is_zero_write(self):
        self.inventory = [
            _row(_WORKSPACE_ID, "codex", _LANE, "w2X:p3Q"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w2X:p3R"),
        ]
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "live_pair_present")
        self.assertTrue(verdict.get("expected_live"))

    def test_single_live_slot_is_still_live(self):
        self.inventory = [_row(_WORKSPACE_ID, "claude", _LANE, "w2X:p3R")]
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "live_pair_present")

    def test_duplicate_canonical_slot_is_zero_write(self):
        row = _row(_WORKSPACE_ID, "claude", _LANE, "w2X:p3R")
        self.inventory = [dict(row), dict(row, pane_id="w2X:p9Z")]
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "duplicate_inventory")

    def test_unreadable_inventory_is_zero_write(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        with mock.patch.object(
            projection, "list_herdr_agent_rows",
            side_effect=HerdrSessionStartError("herdr unavailable"),
        ):
            rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "inventory_unreadable")

    def test_unintegrated_head_is_zero_write(self):
        # Advance the lane branch so it is no longer an ancestor of main.
        (self.worktree / "extra.txt").write_text("y\n", encoding="utf-8")
        _git("add", ".", cwd=self.worktree)
        _git("commit", "-qm", "lane work", cwd=self.worktree)
        self.inventory = []
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "head_not_integrated")

    def test_worktree_branch_mismatch_is_zero_write(self):
        self.inventory = []
        rc, _p, verdict = self._run(branch="some_other_branch")
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "worktree_branch_mismatch")

    def test_hibernated_row_is_refused_by_this_intent(self):
        # #13845's target must not be absorbed here.
        _hibernate(None, _key())
        self.inventory = []
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(verdict.get("state"), "blocked")
        self.assertEqual(
            LaneLifecycleStore().get(_key()).lane_disposition, DISPOSITION_HIBERNATED
        )

    def test_locator_less_expected_row_is_not_absence(self):
        # A row for an expected managed slot with NO locator is "cannot resolve", never
        # "absent" — the liveness contract only calls a slot dead on a positive stale signal.
        # Terminalizing here would record the lane gone on absence of proof of liveness.
        self.inventory = [{"name": encode_assigned_name(_WORKSPACE_ID, "claude", _LANE)}]
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "expected_identity_unresolved")

    def test_foreign_occupant_is_zero_write(self):
        # `expected_live_slots` only aggregates the MANAGED roles, so a unit holding solely an
        # unexpected provider measures zero live. Terminalizing then would record the lane
        # permanently gone while a real process still runs in its unit.
        self.inventory = [_row(_WORKSPACE_ID, "gemini", _LANE, "w2X:p5A")]
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self._assert_blocked(verdict, "foreign_inventory_present")
        self.assertTrue(verdict.get("foreign_names"))

    def test_binding_naming_a_different_worktree_is_zero_write(self):
        # The caller's --worktree must name the SAME lane unit the durable row recorded. A row
        # bound elsewhere is refused rather than coerced.
        #
        # Two layers refuse this, and the test pins the PRECISE one: the attestation pre-gate
        # reports `worktree_binding_mismatch`, and the CAS independently re-checks the token
        # under the row lock (removing the pre-gate still refuses, but degrades the reason to
        # the generic `not_active_bound_state`). Asserting the exact reason keeps the diagnostic
        # layer from being silently dropped while the safety layer masks its absence.
        _force_worktree_identity(None, _key(), _OTHER_WT)
        self.inventory = []
        rc, _p, verdict = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(verdict.get("state"), "blocked", verdict)
        self.assertEqual(verdict.get("reason"), "worktree_binding_mismatch", verdict)
        self.assertEqual(self._disposition(), DISPOSITION_ACTIVE)

    def test_intents_are_mutually_exclusive_zero_write(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        for other in (
            "execute", "migrate_hibernated_legacy",
            "reconcile_hibernated_live", "retire_hibernated_bound",
        ):
            with self.subTest(other=other):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = cmd_sublane_retire(self._args(**{other: True}))
                self.assertEqual(rc, 1)
                self.assertIn("mutually exclusive", err.getvalue())
                self.assertEqual(self._disposition(), DISPOSITION_ACTIVE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
