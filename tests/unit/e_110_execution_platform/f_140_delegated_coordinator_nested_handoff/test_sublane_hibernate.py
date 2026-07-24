"""`sublane hibernate` use case tests (Redmine #13682).

Drives :class:`SublaneHibernateUseCase` over a fake IO port (fake live herdr inventory +
captured guarded close) and a real :class:`LaneLifecycleStore` over a temp home. Covers
the fail-closed durable-idle preflight, the disposition CAS (active -> hibernated), the
tombstone-free process release (reusing the shared #13681 R1-R4 release driver), the
idempotent partial-release resume, and the capacity-projection input (a hibernated row is
non-active, so the W4 roster join excludes it from active capacity).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    VERDICT_PRESENT,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_pin_role import PIN_ROLE_GATEWAY, PIN_ROLE_WORKER
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_PROJECT_GATEWAY,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    load_lane_lifecycle_readonly,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
    BLOCK_CALLBACK_DEBT,
    BLOCK_INTEGRATION_PENDING,
    BLOCK_INVENTORY_UNREADABLE,
    BLOCK_NOT_PARKED,
    BLOCK_ORIGINAL_IDENTITY,
    BLOCK_OWNER_PENDING,
    BLOCK_PENDING_PROMPT,
    BLOCK_PROJECT_GENERATION_MISMATCH,
    BLOCK_PROJECT_UNATTESTED,
    BLOCK_COMPOSER_PENDING_REAL,
    BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT,
    BLOCK_RELEASE_BOUNDARY_MUTATION,
    BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,
    BLOCK_WORKER_BUSY,
    BLOCK_WORKTREE_FINGERPRINT_CHANGED,
    BLOCK_REVIEW_PENDING,
    BLOCK_STALE_ACTION_GENERATION,
    BLOCK_STALE_ACTION_IDENTITY,
    BLOCK_STALE_ACTION_REVISION,
    BLOCK_UNPUSHED_COMMITS,
    BLOCK_UNRECORDED_BOUNDARY,
    BLOCK_WORKING,
    BLOCK_WORKTREE_UNREADABLE,
    PARK_BASIS_DEPENDENCY,
    PARK_BASIS_EARLY_HIBERNATE,
    HibernateAssertions,
    HibernateRequest,
    LiveSublaneHibernateOps,
    SublaneHibernateUseCase,
    WorktreeMutationFingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT,
    BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT,
    NEXT_ACTION_NO_BLIND_RETRY,
    NEXT_ACTION_OWNER_APPROVED_QUARANTINE,
    NEXT_ACTION_READ_RECOVERY,
    NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,
    release_boundary_next_actions,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    LaneActivityObservation,
    read_live_lane_activity,
    read_live_worktree_fingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_cli import (  # noqa: E501
    cmd_sublane_hibernate,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
ISSUE = "13441"
LANE = "issue_13441_provider"
JOURNAL = "77485"


def _row(role: str, lane: str, locator: str) -> dict:
    return {"name": encode_assigned_name(WS, role, lane), "pane_id": locator}


def _all_gates(**overrides) -> HibernateAssertions:
    """Every durable gate satisfied (clean lane) unless a test overrides one."""
    base = dict(
        explicitly_parked=True,
        callbacks_drained=True,
        no_review_pending=True,
        no_owner_approval_pending=True,
        no_integration_pending=True,
        no_pending_prompt=True,
        not_working=True,
        worktree_clean=True,
        boundary_recorded=False,
    )
    base.update(overrides)
    return HibernateAssertions(**base)


#: A readable, clean, quiescent worktree fingerprint (the default the fence sees when a test
#: does not exercise the #13843 TOCTOU fence). Two of these compare equal, so the fence is a
#: no-op unless a test scripts a diverging sequence.
_CLEAN_FP = WorktreeMutationFingerprint(readable=True)
#: A readable, quiescent live lane-activity observation (no running turn, no pending composer).
_QUIESCENT_ACTIVITY = LaneActivityObservation(readable=True)


class _FakeOps:
    """Fake hibernate IO port: canned workspace / inventory (rows + readability) / close.

    ``fingerprints`` (Redmine #13843) optionally scripts the successive worktree-mutation
    fingerprints the release-boundary fence reads (T0 preflight, T1 boundary, T2 post-release)
    — a deterministic synthetic TOCTOU with NO timing / sleep. When exhausted (or omitted) the
    reader returns a clean, quiescent fingerprint, so an unrelated test is unaffected.
    Similarly ``inventory_sequence`` scripts successive inventory reads (the fresh boundary
    re-read differs from the preflight read) to drive a live-generation drift.
    """

    def __init__(
        self,
        *,
        rows,
        close_result=None,
        readable=True,
        attestations=None,
        fingerprints=None,
        inventory_sequence=None,
        activities=None,
    ):
        self._rows = list(rows)
        self._readable = readable
        self._close_result = close_result
        self._attestations = dict(attestations or {})
        self._fingerprints = list(fingerprints) if fingerprints is not None else None
        self._inventory_sequence = (
            [list(r) for r in inventory_sequence]
            if inventory_sequence is not None
            else None
        )
        self._activities = list(activities) if activities is not None else None
        self.close_calls: list = []
        self.worktree_reads = 0
        self.activity_reads = 0

    def workspace_id(self) -> str:
        return WS

    def read_inventory(self):
        if self._inventory_sequence:
            return self._inventory_sequence.pop(0), self._readable
        return list(self._rows), self._readable

    def read_attestation(self, assigned_name):
        return self._attestations.get(assigned_name)

    def read_worktree_mutation(self):
        self.worktree_reads += 1
        if self._fingerprints:
            return self._fingerprints.pop(0)
        return _CLEAN_FP

    def read_lane_activity(self, workspace_id, lane, rows):
        # Redmine #13843 review F2: the boundary reads live worker-busy / pending-composer
        # separately from the worktree fingerprint. Script a sequence to drive a synthetic
        # running-mutation / pending-composer at the boundary; default is a quiescent lane.
        self.activity_reads += 1
        if self._activities:
            return self._activities.pop(0)
        return _QUIESCENT_ACTIVITY

    def execute_close(self, plan):
        self.close_calls.append(plan)
        if self._close_result is not None:
            return self._close_result
        return HerdrRetireCloseResult(
            workspace_id=plan.workspace_id,
            lane_id=plan.lane_id,
            closed=tuple(plan.close_targets),
            failed=(),
            foreign_names=plan.foreign_names,
        )


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)


def _request(**kw) -> HibernateRequest:
    assertions = kw.pop("assertions", _all_gates())
    return HibernateRequest(
        issue=kw.get("issue", ISSUE),
        lane=kw.get("lane", LANE),
        journal=kw.get("journal", JOURNAL),
        assertions=assertions,
        expected_revision=kw.get("expected_revision", ""),
    )


class SublaneHibernateTest(unittest.TestCase):
    def _store(self, tmp) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=Path(tmp))

    def _declare(self, store) -> None:
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
        )

    def _live_ops(self, **kw) -> _FakeOps:
        rows = [
            _row("codex", LANE, f"{WS}:p2"),
            _row("claude", LANE, f"{WS}:p3"),
        ]
        return _FakeOps(rows=rows, **kw)

    def test_issue_lane_supplied_revision_pin_blocks_a_stale_approval(self) -> None:
        # Redmine #14219 j#86734 R2-F1: an ISSUE lane with a SUPPLIED expected_revision must
        # bind the CAS to it — a row whose revision advanced since the approval fails closed
        # pre-CAS instead of being silently re-bound to the current revision.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
            from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin

            bump = LaneDeclarationStore(home=Path(tmp)).backfill_active_binding(
                LaneLifecycleKey(WS, LANE),
                expected_revision=1,
                issue_id=ISSUE,
                worktree_identity="wt_pin_test",
                declared_slots=[
                    ProcessGenerationPin(
                        role="implementation", provider="claude",
                        assigned_name="a1", locator="%1", runtime_revision="r1",
                    )
                ],
            )
            self.assertTrue(bump.applied)
            self.assertEqual(bump.revision, 2)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(expected_revision="1"), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_STALE_ACTION_REVISION, outcome.preflight.blocked_reasons)
            rec = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_issue_lane_supplied_matching_revision_pins_the_cas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(expected_revision="1"), execute=True
            )
            self.assertFalse(outcome.is_blocked, outcome.preflight.blocked_reasons)
            self.assertTrue(outcome.transition.applied)

    def test_issue_lane_without_a_supplied_revision_keeps_prior_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.transition.applied)

    def test_happy_path_hibernates_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = self._live_ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )

            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.transition.applied)
            rec = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
            # The issue is preserved as the lane's owner binding (never cleared).
            self.assertEqual(rec.issue_id, ISSUE)
            # Both managed slots were closed; the release is released.
            self.assertEqual(outcome.release.process_release, RELEASE_RELEASED)
            self.assertEqual(rec.process_release, RELEASE_RELEASED)
            self.assertEqual(
                {loc for _, loc in outcome.release.closed}, {f"{WS}:p2", f"{WS}:p3"}
            )

    def test_hibernated_row_is_excluded_from_active_capacity(self) -> None:
        # Design Answer j#76630 required correction: a hibernated lane must not draw
        # active capacity. The W4 roster join keys on the lane's disposition being
        # non-active; assert the projection input the roster reads.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(), execute=True
            )
            records = load_lane_lifecycle_readonly(home=Path(tmp))
            disposition_by_unit = {
                (r.repo_workspace_id, r.lane_id): r.lane_disposition for r in records
            }
            self.assertEqual(disposition_by_unit[(WS, LANE)], DISPOSITION_HIBERNATED)
            # Non-active -> the roster's `disposition != active` filter drops it.
            self.assertNotEqual(disposition_by_unit[(WS, LANE)], DISPOSITION_ACTIVE)

    def test_blocks_when_not_explicitly_parked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = self._live_ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(assertions=_all_gates(explicitly_parked=False)), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_NOT_PARKED, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            self.assertEqual(ops.close_calls, [])

    def test_blocks_on_each_outstanding_obligation(self) -> None:
        cases = [
            ("callbacks_drained", BLOCK_CALLBACK_DEBT),
            ("no_review_pending", BLOCK_REVIEW_PENDING),
            ("no_owner_approval_pending", BLOCK_OWNER_PENDING),
            ("no_integration_pending", BLOCK_INTEGRATION_PENDING),
            ("no_pending_prompt", BLOCK_PENDING_PROMPT),
            ("not_working", BLOCK_WORKING),
        ]
        for flag, reason in cases:
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                store = self._store(tmp)
                self._declare(store)
                ops = self._live_ops()
                outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                    _request(assertions=_all_gates(**{flag: False})), execute=True
                )
                self.assertTrue(outcome.is_blocked)
                self.assertIn(reason, outcome.preflight.blocked_reasons)
                self.assertIsNone(outcome.transition)
                self.assertEqual(ops.close_calls, [])
                self.assertEqual(
                    store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                    DISPOSITION_ACTIVE,
                )

    def test_dirty_worktree_needs_a_boundary_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            # Neither clean nor a recorded boundary journal -> blocked.
            blocked = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(
                    assertions=_all_gates(worktree_clean=False, boundary_recorded=False)
                ),
                execute=True,
            )
            self.assertTrue(blocked.is_blocked)
            self.assertIn(BLOCK_UNRECORDED_BOUNDARY, blocked.preflight.blocked_reasons)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            # A recorded boundary journal for the dirty worktree unblocks it.
            allowed = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(
                    assertions=_all_gates(worktree_clean=False, boundary_recorded=True)
                ),
                execute=True,
            )
            self.assertFalse(allowed.is_blocked)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_blocks_when_lane_identity_unknown(self) -> None:
        # No lifecycle row for the lane -> original identity unknown, fail closed.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            ops = self._live_ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_ORIGINAL_IDENTITY, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)
            self.assertEqual(ops.close_calls, [])

    def test_preflight_only_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = self._live_ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=False
            )
            self.assertTrue(outcome.preflight.may_hibernate)
            self.assertFalse(outcome.executed)
            self.assertFalse(outcome.is_blocked)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            self.assertEqual(ops.close_calls, [])

    def test_partial_release_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            partial = HerdrRetireCloseResult(
                workspace_id=WS,
                lane_id=LANE,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            first = SublaneHibernateUseCase(
                ops=self._live_ops(close_result=partial), store=store
            ).run(_request(), execute=True)
            self.assertTrue(first.transition.applied)
            self.assertEqual(first.release.process_release, RELEASE_PARTIAL)
            action_first = store.get(LaneLifecycleKey(WS, LANE)).release_action_id
            self.assertTrue(action_first)

            # Second run: already hibernated. Resume the SAME generation, remaining slot
            # closes -> released. No new generation opened, disposition unchanged.
            resume = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(), execute=True
            )
            self.assertTrue(resume.already_hibernated)
            self.assertFalse(resume.is_blocked)
            self.assertEqual(resume.release.process_release, RELEASE_RELEASED)
            self.assertEqual(resume.release.action_id, action_first)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_unreadable_inventory_blocks_before_cas(self) -> None:
        # F1 (R1 j#77907): an unreadable live inventory must NOT be folded to "no live
        # slots". Hibernate fails closed BEFORE the disposition CAS — zero mutation, no
        # close, is_blocked — so a lane is never marked hibernated with panes we could not
        # verify are gone.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = _FakeOps(rows=[], readable=False)
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_INVENTORY_UNREADABLE, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)  # CAS never attempted
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,  # never moved to hibernated
            )

    def test_already_hibernated_redrive_reevaluates_preservation_gate(self) -> None:
        # F2 (R1 j#77907): a partial-release retry on an already-hibernated lane must
        # re-check the CURRENT preservation gate. A lane that has since started working,
        # gained a pending prompt, or owes a callback is NEVER closed by the stale retry —
        # the re-drive blocks (zero close) rather than reporting a silent success.
        for flag, reason in (
            ("not_working", BLOCK_WORKING),
            ("no_pending_prompt", BLOCK_PENDING_PROMPT),
            ("callbacks_drained", BLOCK_CALLBACK_DEBT),
            # R1 j#77907 named a dirty-worktree / boundary-unrecorded partial retry: turning
            # off worktree_clean (boundary_recorded stays False) makes boundary_ok False.
            ("worktree_clean", BLOCK_UNRECORDED_BOUNDARY),
        ):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                store = self._store(tmp)
                self._declare(store)
                partial = HerdrRetireCloseResult(
                    workspace_id=WS,
                    lane_id=LANE,
                    closed=(("claude", f"{WS}:p3"),),
                    failed=(("codex", f"{WS}:p2", "close_failed"),),
                )
                first = SublaneHibernateUseCase(
                    ops=self._live_ops(close_result=partial), store=store
                ).run(_request(), execute=True)
                self.assertEqual(first.release.process_release, RELEASE_PARTIAL)

                retry_ops = self._live_ops()  # a plain close would succeed if attempted
                retry = SublaneHibernateUseCase(ops=retry_ops, store=store).run(
                    _request(assertions=_all_gates(**{flag: False})), execute=True
                )
                self.assertTrue(retry.already_hibernated)
                self.assertTrue(retry.is_blocked)
                self.assertTrue(retry.redrive_blocked)
                self.assertIn(reason, retry.preflight.blocked_reasons)
                self.assertIsNone(retry.release)
                self.assertEqual(retry_ops.close_calls, [])
                # Still hibernated, still partial (never falsely advanced to released).
                rec = store.get(LaneLifecycleKey(WS, LANE))
                self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
                self.assertEqual(rec.process_release, RELEASE_PARTIAL)

    def test_already_hibernated_redrive_blocked_on_unreadable_inventory(self) -> None:
        # F1 + F2: re-driving a partial release when the inventory is unreadable must block
        # (never close blindly on a snapshot we could not read).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            partial = HerdrRetireCloseResult(
                workspace_id=WS,
                lane_id=LANE,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            SublaneHibernateUseCase(
                ops=self._live_ops(close_result=partial), store=store
            ).run(_request(), execute=True)
            retry_ops = _FakeOps(rows=[], readable=False)
            retry = SublaneHibernateUseCase(ops=retry_ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(retry.already_hibernated)
            self.assertTrue(retry.is_blocked)
            self.assertIn(BLOCK_INVENTORY_UNREADABLE, retry.preflight.blocked_reasons)
            self.assertIsNone(retry.release)
            self.assertEqual(retry_ops.close_calls, [])

    def test_crash_after_commit_before_release_resumes(self) -> None:
        # A crash between the disposition CAS and the release: the store is hibernated but
        # process_release is still not_requested. A re-run detects it, opens the
        # generation, closes the slots -> released.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            store.transition_disposition(
                LaneLifecycleKey(WS, LANE),
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(),
            )
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).process_release,
                RELEASE_NOT_REQUESTED,
            )
            ops = self._live_ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.already_hibernated)
            self.assertEqual(outcome.release.process_release, RELEASE_RELEASED)
            self.assertEqual(len(ops.close_calls), 1)

    def test_dead_processes_hibernate_with_no_release(self) -> None:
        # The lane's slots are already gone. The disposition still moves to hibernated;
        # there is nothing to release (a hibernated lane draws zero capacity regardless).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = _FakeOps(rows=[])  # no live slots
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.transition.applied)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )
            self.assertEqual(outcome.release.process_release, RELEASE_NOT_REQUESTED)
            self.assertEqual(ops.close_calls, [])

    def test_non_git_lane_hibernates(self) -> None:
        # A non-git (directory scaffold) lane hibernates identically — the disposition and
        # release are worktree-agnostic; the operator asserts worktree_clean (no VCS diff).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(assertions=_all_gates(worktree_clean=True)), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_incomplete_identity_fails_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = self._live_ops()
            # A non-decimal journal cannot anchor a decision -> fail closed, no mutation.
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(journal="not-a-number"), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            self.assertEqual(ops.close_calls, [])


_PROJECTION = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_herdr_projection.list_herdr_agent_rows"
)
_WORKSPACE_SEGMENT = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application"
    ".herdr_session_start.herdr_workspace_segment"
)
_HIBERNATE_MOD = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_hibernate"
)
_CLI_MOD = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_hibernate_cli"
)


class LiveHibernateAdapterBoundaryTest(unittest.TestCase):
    """R2 (j#77925): pin the REAL adapter's inventory exception -> unreadable conversion.

    The prior regression injected a ``readable=False`` fake, which bypassed the live
    adapter's own ``try/except`` and the CLI's non-zero exit. These exercise that boundary
    directly, so a re-introduced fail-open (exception folded to a "successful empty") is
    caught by a committed test, not just a one-off probe.
    """

    def test_read_inventory_success_is_readable(self) -> None:
        ops = LiveSublaneHibernateOps(repo_root=Path("."))
        with mock.patch(
            _PROJECTION, return_value=[{"name": "x", "pane_id": "w:p1"}]
        ):
            rows, readable = ops.read_inventory()
        self.assertTrue(readable)
        self.assertEqual(len(list(rows)), 1)

    def test_read_inventory_exception_is_unreadable_not_empty(self) -> None:
        ops = LiveSublaneHibernateOps(repo_root=Path("."))
        with mock.patch(_PROJECTION, side_effect=RuntimeError("herdr inventory down")):
            rows, readable = ops.read_inventory()
        # The exception must surface as UNREADABLE, never a "successful empty".
        self.assertFalse(readable)
        self.assertEqual(tuple(rows), ())

    def test_live_adapter_exception_blocks_hibernate_without_mutation(self) -> None:
        # adapter -> use-case: a real inventory read failure blocks BEFORE the CAS, keeps
        # the lane active, and closes nothing.
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            with mock.patch(_WORKSPACE_SEGMENT, return_value=WS), mock.patch(
                _PROJECTION, side_effect=RuntimeError("herdr inventory down")
            ):
                outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                    _request(), execute=True
                )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                BLOCK_INVENTORY_UNREADABLE, outcome.preflight.blocked_reasons
            )
            self.assertIsNone(outcome.transition)  # CAS never attempted
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )

    def test_cmd_returns_nonzero_when_inventory_unreadable(self) -> None:
        # CLI exit: a blocked (unreadable-inventory) hibernate must exit non-zero.
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            args = argparse.Namespace(
                repo=None,
                issue=ISSUE,
                lane=LANE,
                journal=JOURNAL,
                explicitly_parked=True,
                callbacks_drained=True,
                no_review_pending=True,
                no_owner_approval_pending=True,
                no_integration_pending=True,
                no_pending_prompt=True,
                not_working=True,
                worktree_clean=True,
                boundary_recorded=False,
                execute=True,
                json=False,
            )
            fake_ops = _FakeOps(rows=[], readable=False)
            with mock.patch(
                f"{_HIBERNATE_MOD}.LiveSublaneHibernateOps", return_value=fake_ops
            ), mock.patch(
                f"{_HIBERNATE_MOD}.LaneLifecycleStore", return_value=store
            ):
                rc = cmd_sublane_hibernate(args)
            self.assertEqual(rc, 1)

    def test_cmd_returns_nonzero_when_success_withheld(self) -> None:
        # Redmine #13843: a released lane whose post-release check finds residue is a WITHHELD
        # success, not a clean one — the CLI must exit non-zero so the coordinator converges to
        # the recovery / boundary-record path.
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            args = argparse.Namespace(
                repo=None,
                issue=ISSUE,
                lane=LANE,
                journal=JOURNAL,
                explicitly_parked=True,
                callbacks_drained=True,
                no_review_pending=True,
                no_owner_approval_pending=True,
                no_integration_pending=True,
                no_pending_prompt=True,
                not_working=True,
                worktree_clean=True,
                boundary_recorded=False,
                execute=True,
                json=False,
            )
            residue_ops = _FakeOps(
                rows=[
                    _row("codex", LANE, f"{WS}:p2"),
                    _row("claude", LANE, f"{WS}:p3"),
                ],
                fingerprints=[
                    WorktreeMutationFingerprint(readable=True),
                    WorktreeMutationFingerprint(readable=True),
                    WorktreeMutationFingerprint(
                        readable=True, dirty=True, digest="post-residue"
                    ),
                ],
            )
            # The CLI imports these into its OWN namespace, so patch the CLI module (not the
            # use-case module) for the patch to take effect on the actual actuation.
            with mock.patch(
                f"{_CLI_MOD}.LiveSublaneHibernateOps", return_value=residue_ops
            ), mock.patch(
                f"{_CLI_MOD}.LaneLifecycleStore", return_value=store
            ):
                rc = cmd_sublane_hibernate(args)
            self.assertEqual(rc, 1)
            # Preserved despite the withheld success: hibernated, issue binding intact.
            rec = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
            self.assertEqual(rec.issue_id, ISSUE)


def _early_gates(**overrides) -> HibernateAssertions:
    """Every early-hibernate precondition + safety gate satisfied unless overridden.

    Redmine #13967 item 1: the early-hibernate basis (review approved + staging integrated
    + CI green + dogfood delegated + commits pushed) with `explicitly_parked=False`. The
    generic safety gates are all satisfied too (review approved => no review owed;
    integrated => no integration pending).
    """
    base = dict(
        explicitly_parked=False,
        callbacks_drained=True,
        no_review_pending=True,
        no_owner_approval_pending=True,
        no_integration_pending=True,
        no_pending_prompt=True,
        not_working=True,
        worktree_clean=True,
        boundary_recorded=False,
        review_approved=True,
        staging_integrated=True,
        required_ci_green=True,
        dogfood_delegated=True,
        commits_pushed=True,
    )
    base.update(overrides)
    return HibernateAssertions(**base)


class SublaneEarlyHibernateTest(unittest.TestCase):
    """Early hibernate (Redmine #13967 item 1): the alternative affirmative park basis."""

    def _store(self, tmp) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=Path(tmp))

    def _declare(self, store) -> None:
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
        )

    def _live_ops(self, **kw) -> _FakeOps:
        rows = [
            _row("codex", LANE, f"{WS}:p2"),
            _row("claude", LANE, f"{WS}:p3"),
        ]
        return _FakeOps(rows=rows, **kw)

    def test_early_hibernate_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(assertions=_early_gates()), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.transition.applied)
            self.assertEqual(outcome.preflight.park_basis, PARK_BASIS_EARLY_HIBERNATE)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_early_hibernate_blocks_on_unpushed_commits(self) -> None:
        # The anchor's explicit unpushed fence: an early hibernate presupposes integrated,
        # pushed work, so unpushed fails closed (unlike a dependency park).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(assertions=_early_gates(commits_pushed=False)), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_UNPUSHED_COMMITS, outcome.preflight.blocked_reasons)
            # Not qualified => also not parked (no dependency park either).
            self.assertIn(BLOCK_NOT_PARKED, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )

    def test_partial_early_basis_blocks_not_parked(self) -> None:
        # review approved but not integrated / dogfood-delegated: no affirmative basis.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(
                    assertions=_early_gates(
                        staging_integrated=False, dogfood_delegated=False
                    )
                ),
                execute=True,
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_NOT_PARKED, outcome.preflight.blocked_reasons)

    def test_early_hibernate_permits_pending_owner_approval(self) -> None:
        # Redmine #13967 F1: early hibernate runs in the owner_waiting state — owner close
        # approval is deferred to the coordinator's normal path (hibernate != close), so a
        # pending owner approval must NOT block it. Dependency park still requires it.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(assertions=_early_gates(no_owner_approval_pending=False)),
                execute=True,
            )
            self.assertFalse(outcome.is_blocked)
            self.assertNotIn(BLOCK_OWNER_PENDING, outcome.preflight.blocked_reasons)
            self.assertEqual(outcome.preflight.park_basis, PARK_BASIS_EARLY_HIBERNATE)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_dependency_park_still_blocks_on_pending_owner_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(assertions=_all_gates(no_owner_approval_pending=False)),
                execute=True,
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_OWNER_PENDING, outcome.preflight.blocked_reasons)

    def test_both_bases_prefers_early_hibernate(self) -> None:
        # Redmine #13967 R2-F4: when a lane satisfies BOTH explicitly_parked and every early
        # condition, the early basis wins (its owner gate correctly drops) — an ambiguous
        # input must not silently fall back to the stricter dependency basis and re-block.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(
                    assertions=_early_gates(
                        explicitly_parked=True, no_owner_approval_pending=False
                    )
                ),
                execute=True,
            )
            self.assertFalse(outcome.is_blocked)
            self.assertEqual(outcome.preflight.park_basis, PARK_BASIS_EARLY_HIBERNATE)
            self.assertNotIn(BLOCK_OWNER_PENDING, outcome.preflight.blocked_reasons)

    def test_dependency_park_basis_unaffected(self) -> None:
        # A dependency park (explicitly_parked=True) with no early flags still hibernates,
        # and does NOT require commits_pushed (it preserves unpublished commits).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            outcome = SublaneHibernateUseCase(ops=self._live_ops(), store=store).run(
                _request(assertions=_all_gates()), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertEqual(outcome.preflight.park_basis, PARK_BASIS_DEPENDENCY)


# ---------------------------------------------------------------------------
# Project-gateway lane hibernate (Redmine #13811 R1 F1 — action-time exact-generation).
# ---------------------------------------------------------------------------

PG_LANE = "pgwv1_scope_abc"
PG_SCOPE = "workspace/full/project/scope"
PG_GW_NAME = encode_assigned_name(WS, "codex", PG_LANE)
PG_WK_NAME = encode_assigned_name(WS, "claude", PG_LANE)
PG_GW_LOC = f"{WS}:p2"
PG_WK_LOC = f"{WS}:p3"


def _pg_attestations(gw_loc=PG_GW_LOC, wk_loc=PG_WK_LOC):
    return {
        PG_GW_NAME: IdentityAttestationRecord(
            assigned_name=PG_GW_NAME,
            workspace_id=WS,
            role="codex",
            lane_id=PG_LANE,
            locator=gw_loc,
            verdict=VERDICT_PRESENT,
            observed_at="2026-07-20T00:00:00Z",
        ),
        PG_WK_NAME: IdentityAttestationRecord(
            assigned_name=PG_WK_NAME,
            workspace_id=WS,
            role="claude",
            lane_id=PG_LANE,
            locator=wk_loc,
            verdict=VERDICT_PRESENT,
            observed_at="2026-07-20T00:00:00Z",
        ),
    }


class SublaneProjectGatewayHibernateTest(unittest.TestCase):
    """The project-gateway binding path: the three action-time fences (F1 items 1-4)."""

    def _store(self, tmp) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=Path(tmp))

    def _declare_pg(self, tmp, *, roles=(PIN_ROLE_GATEWAY, PIN_ROLE_WORKER)) -> None:
        # Declare a project-gateway lane (binding_kind=project_gateway, empty issue) with its
        # provider-bound declared slot set at generation 1. Default uses the CANONICAL slot
        # roles (gateway/worker) the CURRENT writer emits — the live pair decodes to the
        # provider roles (codex/claude), so this is the Redmine #13811 R2 F1 integration
        # regression (a healthy canonical declaration MUST still hibernate).
        gw_role, wk_role = roles
        decl = LaneDeclarationStore(home=Path(tmp))
        decl.declare_lane(
            LaneLifecycleKey(WS, PG_LANE),
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL),
            binding_kind=BINDING_KIND_PROJECT_GATEWAY,
            project_scope=PG_SCOPE,
            declared_slots=(
                ProcessGenerationPin(
                    role=gw_role, provider="codex", assigned_name=PG_GW_NAME, locator=PG_GW_LOC
                ),
                ProcessGenerationPin(
                    role=wk_role, provider="claude", assigned_name=PG_WK_NAME, locator=PG_WK_LOC
                ),
            ),
        )

    def _rows(self, gw_loc=PG_GW_LOC, wk_loc=PG_WK_LOC, **gw_extra):
        gw = {"name": PG_GW_NAME, "pane_id": gw_loc}
        gw.update(gw_extra)
        return [gw, {"name": PG_WK_NAME, "pane_id": wk_loc}]

    def _ops(self, *, rows=None, attestations=None, readable=True):
        return _FakeOps(
            rows=self._rows() if rows is None else rows,
            attestations=_pg_attestations() if attestations is None else attestations,
            readable=readable,
        )

    def _request(
        self, *, project_scope=PG_SCOPE, expected_lane_generation="1", expected_revision="1"
    ):
        return HibernateRequest(
            issue=ISSUE,
            lane=PG_LANE,
            journal=JOURNAL,
            project_scope=project_scope,
            expected_lane_generation=expected_lane_generation,
            expected_revision=expected_revision,
            assertions=_all_gates(),
        )

    def test_happy_path_hibernates_project_gateway_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            outcome = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(), execute=True
            )
            self.assertFalse(outcome.is_blocked, outcome.preflight.blocked_reasons)
            self.assertTrue(outcome.executed)
            self.assertEqual(outcome.project_scope, PG_SCOPE)
            self.assertTrue(outcome.preflight.project_generation_matched)
            self.assertTrue(outcome.preflight.project_attestation_ok)
            self.assertTrue(outcome.preflight.action_generation_current)
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)

    def test_missing_expected_generation_blocks_zero_mutation(self) -> None:
        # An approval that does not assert its approved generation cannot actuate (F1 item 3).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(expected_lane_generation=""), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_STALE_ACTION_GENERATION, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_stale_generation_approval_blocks(self) -> None:
        # The approval asserts a superseded generation (the row is at 1, approval names 0).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(expected_lane_generation="0"), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_STALE_ACTION_GENERATION, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])

    def test_provider_rebind_blocks_zero_mutation(self) -> None:
        # The live codex pane surfaces a different provider than the declared pin (F1 item 1).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops(rows=self._rows(provider="rebound"))
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                BLOCK_PROJECT_GENERATION_MISMATCH, outcome.preflight.blocked_reasons
            )
            self.assertEqual(ops.close_calls, [])
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_recycled_locator_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops(
                rows=self._rows(gw_loc=f"{WS}:p99"),
                attestations=_pg_attestations(gw_loc=f"{WS}:p99"),
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                BLOCK_PROJECT_GENERATION_MISMATCH, outcome.preflight.blocked_reasons
            )
            self.assertEqual(ops.close_calls, [])

    def test_missing_attestation_blocks_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            # Only the worker is attested; the gateway slot is unattested.
            atts = _pg_attestations()
            del atts[PG_GW_NAME]
            ops = self._ops(attestations=atts)
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PROJECT_UNATTESTED, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_stale_attestation_locator_drift_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            # Live locator is PG_GW_LOC but the attestation pins an older locator.
            ops = self._ops(attestations=_pg_attestations(gw_loc=f"{WS}:pOLD"))
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PROJECT_UNATTESTED, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])

    def test_wrong_scope_is_not_this_lane(self) -> None:
        # A caller naming a different scope does not match this project lane's binding.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(project_scope="workspace/other/scope"), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_ORIGINAL_IDENTITY, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])

    def test_declared_runtime_revision_live_unobserved_still_hibernates(self) -> None:
        # 正本 lenient revision: a declared non-empty runtime_revision with an unobserved live
        # revision is NOT a mismatch (would-be F1.2 strict fail-closed is superseded by the
        # documented action-time contract / #13846 false-conflict fix).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            decl = LaneDeclarationStore(home=Path(tmp))
            decl.declare_lane(
                LaneLifecycleKey(WS, PG_LANE),
                decision=DecisionPointer(
                    source="redmine", issue_id=ISSUE, journal_id=JOURNAL
                ),
                binding_kind=BINDING_KIND_PROJECT_GATEWAY,
                project_scope=PG_SCOPE,
                declared_slots=(
                    ProcessGenerationPin(
                        role="codex",
                        provider="codex",
                        assigned_name=PG_GW_NAME,
                        locator=PG_GW_LOC,
                        runtime_revision="runtime-v2",
                    ),
                    ProcessGenerationPin(
                        role="claude",
                        provider="claude",
                        assigned_name=PG_WK_NAME,
                        locator=PG_WK_LOC,
                    ),
                ),
            )
            outcome = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(), execute=True
            )
            self.assertFalse(outcome.is_blocked, outcome.preflight.blocked_reasons)
            self.assertTrue(outcome.executed)

    def test_legacy_role_declaration_read_compatible_hibernates(self) -> None:
        # A pre-#13920 legacy (codex/claude) declared-slot set still resolves to the same
        # canonical slots as the live pair — read-compatible, no regression (Redmine #13811
        # R2 F1: canonical is the primary shape, legacy stays readable).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp, roles=("codex", "claude"))
            outcome = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(), execute=True
            )
            self.assertFalse(outcome.is_blocked, outcome.preflight.blocked_reasons)
            self.assertTrue(outcome.executed)

    def test_missing_expected_revision_blocks_zero_mutation(self) -> None:
        # Redmine #13811 R2 F2: a project-gateway hibernate that does not assert the approved
        # revision cannot actuate (the fresh CAS has no approved authority to bind to).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(expected_revision=""), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_STALE_ACTION_REVISION, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_same_generation_revision_drift_blocks_zero_mutation(self) -> None:
        # Redmine #13811 R2 F2: the approval asserts an OLDER revision than the row's current
        # revision — the process authority advanced within the same generation (pin repair /
        # replacement / decision update) since the approval. The stale approval fails closed
        # pre-CAS; it never re-binds to the current revision / pins, and the CAS is bound to
        # the approved revision so even the atomic commit would refuse.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)  # declared at generation 1, revision 1
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                self._request(expected_revision="0"), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_STALE_ACTION_REVISION, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)
            self.assertEqual(rec.revision, 1)

    def test_correct_revision_and_generation_hibernates(self) -> None:
        # Positive control: the approved (generation=1, revision=1) exactly names the current
        # row, so the fresh CAS binds and the lane hibernates.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            outcome = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(expected_lane_generation="1", expected_revision="1"),
                execute=True,
            )
            self.assertFalse(outcome.is_blocked, outcome.preflight.blocked_reasons)
            self.assertTrue(outcome.preflight.action_revision_current)
            rec = store.get(LaneLifecycleKey(WS, PG_LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)

    def test_already_hibernated_redrive_not_blocked_by_advanced_revision(self) -> None:
        # Redmine #13811 R2 F2 boundary: the fresh hibernate bumps the row's revision (1->2),
        # so the approval's revision (1) is now older than the current row. The redrive must
        # NOT re-apply the revision fence (it resumes the STORED release action id / pins, the
        # immutable authority) — otherwise every project-gateway redrive would falsely block.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            first = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(), execute=True
            )
            self.assertFalse(first.is_blocked, first.preflight.blocked_reasons)
            # The hibernate CAS + release open + release-outcome each bump the revision, so it
            # is now well past the approval's revision (1) — the redrive must still resume.
            self.assertGreater(store.get(LaneLifecycleKey(WS, PG_LANE)).revision, 1)
            # Re-run the SAME approval (expected_revision=1) against the now-hibernated row.
            redrive = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(expected_revision="1"), execute=True
            )
            self.assertTrue(redrive.already_hibernated)
            self.assertFalse(redrive.redrive_blocked, redrive.preflight.blocked_reasons)
            self.assertNotIn(
                BLOCK_STALE_ACTION_REVISION, redrive.preflight.blocked_reasons
            )

    def test_stale_cross_cycle_approval_cannot_redrive(self) -> None:
        # Redmine #13811 R4 F2: a DIFFERENT hibernate cycle's approval (a different journal)
        # must not redrive THIS cycle's stored release. Cycle B hibernates under JOURNAL and
        # opens release action id `hibernate:<lane>:<JOURNAL>`. A stale cycle-A approval (a
        # different journal) then targets the already-hibernated row — its journal-scoped
        # action id differs from the stored release, so the redrive fails closed zero-close.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            first = SublaneHibernateUseCase(ops=self._ops(), store=store).run(
                self._request(), execute=True
            )
            self.assertFalse(first.is_blocked, first.preflight.blocked_reasons)
            other = HibernateRequest(
                issue=ISSUE,
                lane=PG_LANE,
                journal="90001",  # a DIFFERENT approval / cycle
                project_scope=PG_SCOPE,
                expected_lane_generation="1",
                expected_revision="1",
                assertions=_all_gates(),
            )
            ops = self._ops()
            redrive = SublaneHibernateUseCase(ops=ops, store=store).run(
                other, execute=True
            )
            self.assertTrue(redrive.already_hibernated)
            self.assertTrue(redrive.redrive_blocked)
            self.assertIn(
                BLOCK_STALE_ACTION_IDENTITY, redrive.preflight.blocked_reasons
            )
            self.assertEqual(ops.close_calls, [])

    def _hibernate_cas_only(self, store, journal) -> None:
        # Reproduce the R5 crash window: the `active -> hibernated` CAS lands (storing THIS
        # cycle's decision) but a crash precedes the release open, so `release_action_id` is
        # empty. Declaration is done by the caller.
        store.transition_disposition(
            LaneLifecycleKey(WS, PG_LANE),
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=journal
            ),
        )

    def test_crash_window_old_journal_cannot_hijack(self) -> None:
        # Redmine #13811 R5: the row is hibernated by cycle B (journal 90002) with the release
        # NOT yet opened (release_action_id == ""). A stale cycle-A approval (journal 77485)
        # must NOT be treated as current just because the action id is empty — the row carries
        # cycle B's durable decision, so the old approval is fenced out (zero-close).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            self._hibernate_cas_only(store, "90002")
            self.assertEqual(store.get(LaneLifecycleKey(WS, PG_LANE)).release_action_id, "")
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                HibernateRequest(
                    issue=ISSUE,
                    lane=PG_LANE,
                    journal="77485",  # a DIFFERENT (older) cycle's approval
                    project_scope=PG_SCOPE,
                    expected_lane_generation="1",
                    expected_revision="2",
                    assertions=_all_gates(),
                ),
                execute=True,
            )
            self.assertTrue(outcome.already_hibernated)
            self.assertTrue(outcome.redrive_blocked)
            self.assertFalse(outcome.preflight.action_identity_current)
            self.assertIn(
                BLOCK_STALE_ACTION_IDENTITY, outcome.preflight.blocked_reasons
            )
            self.assertEqual(ops.close_calls, [])

    def test_crash_window_same_journal_recovery_resumes(self) -> None:
        # The SAME cycle's approval (journal 90002) recovering the crash window IS current —
        # the row's stored decision matches, so the release is opened / driven now.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            self._hibernate_cas_only(store, "90002")
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                HibernateRequest(
                    issue=ISSUE,
                    lane=PG_LANE,
                    journal="90002",  # the SAME cycle's approval
                    project_scope=PG_SCOPE,
                    expected_lane_generation="1",
                    expected_revision="2",
                    assertions=_all_gates(),
                ),
                execute=True,
            )
            self.assertTrue(outcome.already_hibernated)
            self.assertFalse(outcome.redrive_blocked, outcome.preflight.blocked_reasons)
            self.assertTrue(outcome.preflight.action_identity_current)
            self.assertEqual(len(ops.close_calls), 1)

    def test_issue_request_does_not_match_project_lane(self) -> None:
        # An issue-binding request (no project_scope) never matches a project-gateway row —
        # its empty issue_id != ISSUE — so the project lane is not hibernated by an issue call.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_pg(tmp)
            ops = self._ops()
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                HibernateRequest(
                    issue=ISSUE, lane=PG_LANE, journal=JOURNAL, assertions=_all_gates()
                ),
                execute=True,
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_ORIGINAL_IDENTITY, outcome.preflight.blocked_reasons)
            self.assertEqual(ops.close_calls, [])


# ---------------------------------------------------------------------------
# Release-boundary TOCTOU preservation fence (Redmine #13843).
# ---------------------------------------------------------------------------


def _dirty_fp(digest: str = "worker-write-1", *, dirty=True, untracked=False):
    """A readable but MUTATED worktree fingerprint (a diff appeared)."""
    return WorktreeMutationFingerprint(
        readable=True, dirty=dirty, untracked=untracked, digest=digest
    )


class SublaneHibernateToctouFenceTest(unittest.TestCase):
    """Synthetic TOCTOU: a worktree mutation / generation drift appears between the preflight
    snapshot and the release boundary (Redmine #13843). Driven by a scripted fingerprint /
    inventory sequence — deterministic, no timing / sleep / fault injection."""

    def _store(self, tmp) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=Path(tmp))

    def _declare(self, store) -> None:
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
        )

    def _rows(self):
        return [_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")]

    def test_boundary_worktree_mutation_blocks_before_cas(self) -> None:
        # A clean preflight (T0), then a fresh diff at the boundary (T1): zero lifecycle
        # transition, zero process close, lane stays active.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = _FakeOps(
                rows=self._rows(),
                fingerprints=[_CLEAN_FP, _dirty_fp("w1")],
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertTrue(outcome.boundary_blocked)
            # Redmine #14230: the typed content-changed subreason, plus the backward-compat
            # coarse summary — never JUST the coarse token.
            self.assertIn(BLOCK_WORKTREE_FINGERPRINT_CHANGED, outcome.boundary_reasons)
            self.assertIn(BLOCK_RELEASE_BOUNDARY_MUTATION, outcome.boundary_reasons)
            self.assertIn(BLOCK_RELEASE_BOUNDARY_MUTATION, outcome.blocked_reasons)
            self.assertNotIn(BLOCK_WORKER_BUSY, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_COMPOSER_PENDING_REAL, outcome.boundary_reasons)
            self.assertIsNone(outcome.transition)  # CAS never attempted
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            # The fence actually ran: T0 + T1 fingerprint reads (no T2, no release).
            self.assertEqual(ops.worktree_reads, 2)

    def test_boundary_running_mutation_blocks(self) -> None:
        # F2: a live worker turn observed at the boundary (T1) — even with a clean worktree —
        # blocks with zero mutation.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            running = LaneActivityObservation(readable=True, worker_busy=True)
            ops = _FakeOps(rows=self._rows(), activities=[running])
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            # Redmine #14230: the typed worker-busy subreason, plus the coarse summary.
            self.assertIn(BLOCK_WORKER_BUSY, outcome.boundary_reasons)
            self.assertIn(BLOCK_RELEASE_BOUNDARY_MUTATION, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_WORKTREE_FINGERPRINT_CHANGED, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_COMPOSER_PENDING_REAL, outcome.boundary_reasons)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            self.assertEqual(ops.activity_reads, 1)  # the fence actually observed activity

    def test_boundary_pending_composer_blocks(self) -> None:
        # F2: a live pending composer observed at the boundary blocks with zero mutation.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            composer = LaneActivityObservation(readable=True, composer_pending=True)
            ops = _FakeOps(rows=self._rows(), activities=[composer])
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            # Redmine #14230: the typed real-pending-composer subreason, plus coarse summary.
            self.assertIn(BLOCK_COMPOSER_PENDING_REAL, outcome.boundary_reasons)
            self.assertIn(BLOCK_RELEASE_BOUNDARY_MUTATION, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_WORKER_BUSY, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_WORKTREE_FINGERPRINT_CHANGED, outcome.boundary_reasons)
            self.assertFalse(outcome.composer_ghost_observed)  # a REAL pending, not a ghost
            self.assertEqual(ops.close_calls, [])

    def test_boundary_composer_ghost_observed_is_not_a_block_reason(self) -> None:
        # Redmine #14230: a ghost-empty placeholder is a SAFE observation, never a block
        # reason -- a lane with a ghost-only composer and no other divergence hibernates.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ghost = LaneActivityObservation(
                readable=True, composer_pending=False, composer_ghost_observed=True
            )
            ops = _FakeOps(rows=self._rows(), activities=[ghost])
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertNotIn(BLOCK_COMPOSER_PENDING_REAL, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_RELEASE_BOUNDARY_MUTATION, outcome.boundary_reasons)
            self.assertTrue(outcome.composer_ghost_observed)

    def test_boundary_unreadable_activity_blocks(self) -> None:
        # F2: an unreadable live activity observation fails closed (never "quiescent").
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = _FakeOps(
                rows=self._rows(),
                activities=[LaneActivityObservation(readable=False)],
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            # Redmine #14230: an unreadable ACTIVITY probe (not the worktree probe) fires the
            # runtime-specific reason, distinct from a worktree-fingerprint-unreadable block.
            self.assertIn(
                BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN, outcome.boundary_reasons
            )
            self.assertNotIn(BLOCK_WORKTREE_UNREADABLE, outcome.boundary_reasons)
            self.assertEqual(ops.close_calls, [])

    def test_boundary_unreadable_worktree_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            unreadable = WorktreeMutationFingerprint(readable=False)
            ops = _FakeOps(rows=self._rows(), fingerprints=[_CLEAN_FP, unreadable])
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_WORKTREE_UNREADABLE, outcome.boundary_reasons)
            self.assertIsNone(outcome.transition)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )

    def test_boundary_unreadable_preflight_t0_worktree_blocks_as_unreadable(self) -> None:
        # Redmine #14230 review j#84793 R1-F1: an unreadable PREFLIGHT (T0) capture must
        # classify as worktree_fingerprint_unreadable, never worktree_fingerprint_changed
        # -- an unreadable baseline cannot prove a content change happened.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            unreadable_t0 = WorktreeMutationFingerprint(readable=False)
            ops = _FakeOps(rows=self._rows(), fingerprints=[unreadable_t0, _CLEAN_FP])
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_WORKTREE_UNREADABLE, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_WORKTREE_FINGERPRINT_CHANGED, outcome.boundary_reasons)
            self.assertNotIn(BLOCK_RELEASE_BOUNDARY_MUTATION, outcome.boundary_reasons)
            self.assertEqual(ops.close_calls, [])

    def test_boundary_generation_drift_blocks(self) -> None:
        # The live managed slot set changes between the preflight read and the boundary read
        # (the worker pane recycled to a new locator) — a generation drift the preflight
        # snapshot no longer describes. Zero mutation.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            preflight_rows = self._rows()
            boundary_rows = [
                _row("codex", LANE, f"{WS}:p2"),
                _row("claude", LANE, f"{WS}:p99"),  # recycled worker locator
            ]
            ops = _FakeOps(
                rows=preflight_rows,
                inventory_sequence=[preflight_rows, boundary_rows],
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT, outcome.boundary_reasons
            )
            self.assertIsNone(outcome.transition)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )

    def test_post_release_residue_withholds_success(self) -> None:
        # T0 == T1 clean, so the CAS + release proceed (panes close). A mutation then races in
        # DURING the close window (T2 diverges): success is withheld, a recovery next-action is
        # attached, and the lane is PRESERVED (hibernated, issue intact — nothing discarded).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = _FakeOps(
                rows=self._rows(),
                fingerprints=[_CLEAN_FP, _CLEAN_FP, _dirty_fp("post-residue")],
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            # The actuation itself was not blocked, but the success is withheld.
            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.executed)
            self.assertTrue(outcome.success_withheld)
            self.assertFalse(outcome.is_success)
            self.assertTrue(outcome.recovery_detail)
            self.assertEqual(len(ops.close_calls), 1)  # the release DID happen
            # Preserved: hibernated, issue binding intact, worktree/branch/commits untouched.
            rec = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
            self.assertEqual(rec.issue_id, ISSUE)
            self.assertEqual(ops.worktree_reads, 3)  # T0 + T1 + T2

    def test_clean_fingerprints_hibernate_and_report_success(self) -> None:
        # Positive control: three clean captures -> a clean, fully-actuated success.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            ops = _FakeOps(
                rows=self._rows(), fingerprints=[_CLEAN_FP, _CLEAN_FP, _CLEAN_FP]
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertFalse(outcome.success_withheld)
            self.assertTrue(outcome.is_success)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_redrive_boundary_mutation_blocks_zero_close(self) -> None:
        # A partial release, then a redrive whose boundary fingerprint diverges: the redrive is
        # blocked (zero close), the row stays hibernated / partial — never advanced to released
        # over a now-mutated worktree, and never recorded clean.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare(store)
            partial = HerdrRetireCloseResult(
                workspace_id=WS,
                lane_id=LANE,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            first = SublaneHibernateUseCase(
                ops=_FakeOps(rows=self._rows(), close_result=partial), store=store
            ).run(_request(), execute=True)
            self.assertEqual(first.release.process_release, RELEASE_PARTIAL)

            retry_ops = _FakeOps(
                rows=self._rows(), fingerprints=[_CLEAN_FP, _dirty_fp("mid-redrive")]
            )
            retry = SublaneHibernateUseCase(ops=retry_ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(retry.already_hibernated)
            self.assertTrue(retry.is_blocked)
            self.assertTrue(retry.redrive_blocked)
            self.assertIn(BLOCK_RELEASE_BOUNDARY_MUTATION, retry.boundary_reasons)
            self.assertIsNone(retry.release)
            self.assertEqual(retry_ops.close_calls, [])
            rec = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
            self.assertEqual(rec.process_release, RELEASE_PARTIAL)


class ReleaseBoundaryNextActionsTest(unittest.TestCase):
    """Redmine #14230 review j#84793 R1-F2: reason -> safe next action mapping (pure)."""

    def test_no_reasons_yields_empty(self) -> None:
        actions = release_boundary_next_actions(())
        self.assertEqual(actions.primary, "")
        self.assertEqual(actions.actions, ())
        self.assertEqual(actions.details, {})

    def test_coarse_summary_alone_yields_no_action(self) -> None:
        # BLOCK_RELEASE_BOUNDARY_MUTATION is a backward-compat summary only; it must never
        # be treated as its own actionable reason (j#84793: "coarse summary単独禁止").
        actions = release_boundary_next_actions((BLOCK_RELEASE_BOUNDARY_MUTATION,))
        self.assertEqual(actions.actions, ())

    def test_worktree_unreadable_maps_to_read_recovery(self) -> None:
        actions = release_boundary_next_actions((BLOCK_WORKTREE_UNREADABLE,))
        self.assertEqual(actions.primary, NEXT_ACTION_READ_RECOVERY)
        self.assertEqual(actions.actions, (NEXT_ACTION_READ_RECOVERY,))

    def test_runtime_unreadable_also_maps_to_read_recovery(self) -> None:
        actions = release_boundary_next_actions(
            (BLOCK_RUNTIME_STATE_UNREADABLE_OR_UNKNOWN,)
        )
        self.assertEqual(actions.primary, NEXT_ACTION_READ_RECOVERY)

    def test_worker_busy_maps_to_wait_for_completion(self) -> None:
        actions = release_boundary_next_actions(
            (BLOCK_WORKER_BUSY, BLOCK_RELEASE_BOUNDARY_MUTATION)
        )
        self.assertEqual(actions.primary, NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION)
        self.assertEqual(actions.actions, (NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,))

    def test_composer_pending_real_maps_to_owner_approved_quarantine(self) -> None:
        actions = release_boundary_next_actions(
            (BLOCK_COMPOSER_PENDING_REAL, BLOCK_RELEASE_BOUNDARY_MUTATION)
        )
        self.assertEqual(actions.primary, NEXT_ACTION_OWNER_APPROVED_QUARANTINE)

    def test_worktree_changed_maps_to_no_blind_retry(self) -> None:
        actions = release_boundary_next_actions(
            (BLOCK_WORKTREE_FINGERPRINT_CHANGED, BLOCK_RELEASE_BOUNDARY_MUTATION)
        )
        self.assertEqual(actions.primary, NEXT_ACTION_NO_BLIND_RETRY)

    def test_multiple_axes_decision_order_unreadable_wins_primary(self) -> None:
        # Redmine #14230 j#84793 "multiple-axis決定順": worker-busy and composer-pending-real
        # both fired alongside an unreadable axis -- unreadable is the least-trustworthy
        # evidence and wins as primary, but EVERY fired axis's action is still present so a
        # caller cannot silently drop the worker-busy obligation by acting on primary alone.
        actions = release_boundary_next_actions(
            (
                BLOCK_WORKTREE_UNREADABLE,
                BLOCK_WORKER_BUSY,
                BLOCK_COMPOSER_PENDING_REAL,
            )
        )
        self.assertEqual(actions.primary, NEXT_ACTION_READ_RECOVERY)
        self.assertEqual(
            set(actions.actions),
            {
                NEXT_ACTION_READ_RECOVERY,
                NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,
                NEXT_ACTION_OWNER_APPROVED_QUARANTINE,
            },
        )

    def test_worker_busy_outranks_composer_and_worktree_changed(self) -> None:
        actions = release_boundary_next_actions(
            (
                BLOCK_WORKER_BUSY,
                BLOCK_COMPOSER_PENDING_REAL,
                BLOCK_WORKTREE_FINGERPRINT_CHANGED,
                BLOCK_RELEASE_BOUNDARY_MUTATION,
            )
        )
        self.assertEqual(actions.primary, NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION)
        self.assertEqual(len(actions.actions), 3)

    def test_details_carry_no_body_hash_length_or_path(self) -> None:
        # Every detail is a fixed instruction string -- never a value derived from the
        # composer body / a digest / a path (Redmine #14230 acceptance 2).
        actions = release_boundary_next_actions(
            (
                BLOCK_WORKTREE_UNREADABLE,
                BLOCK_WORKER_BUSY,
                BLOCK_COMPOSER_PENDING_REAL,
                BLOCK_WORKTREE_FINGERPRINT_CHANGED,
            )
        )
        for detail in actions.details.values():
            self.assertNotRegex(detail, r"^/|\s/[\w.-]+/")  # no absolute filesystem path
            self.assertNotRegex(detail, r"\b[0-9a-f]{8,}\b")  # no hex digest

    def test_outcome_next_actions_property_reflects_boundary_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            running = LaneActivityObservation(readable=True, worker_busy=True)
            ops = _FakeOps(
                rows=[_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")],
                activities=[running],
            )
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertEqual(outcome.next_actions.primary, NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION)
            payload = outcome.as_payload()
            self.assertEqual(
                payload["boundary_next_actions"]["primary"],
                NEXT_ACTION_WAIT_FOR_WORKER_COMPLETION,
            )


class WorktreeMutationFingerprintTest(unittest.TestCase):
    """The pure #13843 value object + boundary / post-check decisions (fail-closed)."""

    def test_clean_captures_are_not_diverged(self) -> None:
        a = WorktreeMutationFingerprint(readable=True)
        b = WorktreeMutationFingerprint(readable=True)
        self.assertFalse(a.diverged_from(b))
        self.assertTrue(a.quiescent)

    def test_stable_dirty_is_not_diverged(self) -> None:
        # A pre-existing dirty worktree (a dependency park with a boundary journal) that does
        # not change is NOT a divergence — the fence blocks on a change, not on dirtiness.
        a = WorktreeMutationFingerprint(readable=True, dirty=True, digest="X")
        b = WorktreeMutationFingerprint(readable=True, dirty=True, digest="X")
        self.assertFalse(a.diverged_from(b))

    def test_digest_change_is_diverged(self) -> None:
        a = WorktreeMutationFingerprint(readable=True, dirty=True, digest="Y")
        b = WorktreeMutationFingerprint(readable=True, dirty=True, digest="X")
        self.assertTrue(a.diverged_from(b))

    def test_unreadable_either_side_is_diverged(self) -> None:
        clean = WorktreeMutationFingerprint(readable=True)
        bad = WorktreeMutationFingerprint(readable=False)
        self.assertTrue(bad.diverged_from(clean))
        self.assertTrue(clean.diverged_from(bad))
        self.assertFalse(bad.quiescent)

    def test_activity_on_later_capture_is_diverged(self) -> None:
        clean = WorktreeMutationFingerprint(readable=True)
        running = WorktreeMutationFingerprint(readable=True, mutation_in_flight=True)
        self.assertTrue(running.diverged_from(clean))


_BOUNDARY_MOD = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_hibernate_boundary"
)
# The live git-status probe lives in the boundary leaf, so patch subprocess.run there.
_LIVE_MOD_SUBPROCESS = f"{_BOUNDARY_MOD}.subprocess.run"


class _CP:
    """A minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# A work-tree read is 3 git calls: rev-parse (text), status -z (bytes), diff --binary (bytes).
# `status_z` is NUL-separated porcelain -z bytes; `diff_out` is raw diff bytes.
def _worktree_calls(status_z=b"", diff_out=b""):
    return [_CP(0, "true\n"), _CP(0, status_z), _CP(0, diff_out)]


class LiveHibernateWorktreeFingerprintTest(unittest.TestCase):
    """R (Redmine #13843): the REAL adapter's git -> fingerprint conversion, incl. the
    fail-closed / non-git tri-state (F4: a git-invocation failure is never "clean")."""

    def _ops(self):
        return LiveSublaneHibernateOps(repo_root=Path("."))

    def test_clean_repo_is_readable_and_clean(self) -> None:
        with mock.patch(_LIVE_MOD_SUBPROCESS, side_effect=_worktree_calls(b"")):
            fp = self._ops().read_worktree_mutation()
        self.assertTrue(fp.readable)
        self.assertFalse(fp.dirty)
        self.assertFalse(fp.untracked)

    def test_dirty_repo_reports_dirty(self) -> None:
        # -z porcelain: NUL-separated `XY SP path` records. Tracked-only (an untracked path
        # would need a real file to content-hash — covered by the real-git tests).
        status = b" M src/foo.py\x00 M src/bar.py\x00"
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=_worktree_calls(status, b"diff-body"),
        ):
            fp = self._ops().read_worktree_mutation()
        self.assertTrue(fp.readable)
        self.assertTrue(fp.dirty)
        self.assertFalse(fp.untracked)
        self.assertTrue(fp.digest)

    def test_status_error_is_unreadable_not_clean(self) -> None:
        # Inside a work tree but `status` failed -> fail closed (never "clean").
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=[_CP(0, "true\n"), _CP(128, b"")],
        ):
            fp = self._ops().read_worktree_mutation()
        self.assertFalse(fp.readable)

    def test_diff_error_is_unreadable_not_clean(self) -> None:
        # F1: `git diff HEAD --binary` (tracked content) failed -> fail closed.
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=[_CP(0, "true\n"), _CP(0, b" M a.py\x00"), _CP(128, b"")],
        ):
            fp = self._ops().read_worktree_mutation()
        self.assertFalse(fp.readable)

    def test_non_git_directory_is_readable_clean(self) -> None:
        # F4: git ran and reported a genuine "not a git repository" -> a non-git scaffold
        # lane (readable, clean) — recognized by the stderr signature, not a blanket non-zero.
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=[_CP(128, "", "fatal: not a git repository (or any parent)")],
        ):
            fp = self._ops().read_worktree_mutation()
        self.assertTrue(fp.readable)
        self.assertFalse(fp.dirty)

    def test_not_a_work_tree_false_is_readable_clean(self) -> None:
        # rev-parse succeeds but reports "false" (a git dir / bare) -> clean, no diff surface.
        with mock.patch(_LIVE_MOD_SUBPROCESS, side_effect=[_CP(0, "false\n")]):
            fp = self._ops().read_worktree_mutation()
        self.assertTrue(fp.readable)
        self.assertFalse(fp.dirty)

    def test_fatal_non_git_error_is_unreadable_not_clean(self) -> None:
        # F4: a non-zero rev-parse that is NOT a genuine "not a git repository" (permission /
        # dubious ownership / cwd I/O) must fail CLOSED, never be treated as a clean non-git lane.
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=[_CP(128, "", "fatal: detected dubious ownership in repository")],
        ):
            fp = self._ops().read_worktree_mutation()
        self.assertFalse(fp.readable)

    def test_git_invocation_failure_is_unreadable(self) -> None:
        # The git binary is missing / the call raised -> fail closed, NOT a clean non-git lane.
        with mock.patch(_LIVE_MOD_SUBPROCESS, side_effect=OSError("no git")):
            fp = self._ops().read_worktree_mutation()
        self.assertFalse(fp.readable)

    def test_digest_is_stable_and_order_independent(self) -> None:
        # The digest is over the SORTED records + diff, so record order does not change it.
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=_worktree_calls(b" M a.py\x00", b"D"),
        ):
            fp1 = self._ops().read_worktree_mutation()
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=_worktree_calls(b" M a.py\x00", b"D"),
        ):
            fp2 = self._ops().read_worktree_mutation()
        self.assertEqual(fp1.digest, fp2.digest)

    def test_same_porcelain_rows_different_content_flip_digest(self) -> None:
        # F1 regression (mock): identical porcelain records (same ` M path`) but changed
        # tracked CONTENT (`git diff --binary` differs) must flip the digest.
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=_worktree_calls(b" M a.py\x00", b"diff v1"),
        ):
            fp1 = self._ops().read_worktree_mutation()
        with mock.patch(
            _LIVE_MOD_SUBPROCESS,
            side_effect=_worktree_calls(b" M a.py\x00", b"diff v2 more"),
        ):
            fp2 = self._ops().read_worktree_mutation()
        self.assertNotEqual(fp1.digest, fp2.digest)


class LiveHibernateWorktreeRealGitTest(unittest.TestCase):
    """F1 (Redmine #13843): the content-sensitive digest over a REAL git worktree — a worker
    writing more into an already-dirty tracked / untracked file flips the fingerprint."""

    def _git(self, cwd, *args):
        import subprocess

        return subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
        )

    def _init_repo(self, tmp):
        self._git(tmp, "init", "-q")
        self._git(tmp, "config", "user.email", "t@example.com")
        self._git(tmp, "config", "user.name", "t")
        (Path(tmp) / "tracked.py").write_text("v1\n")
        self._git(tmp, "add", "tracked.py")
        self._git(tmp, "commit", "-q", "-m", "base")

    def test_tracked_content_change_flips_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            (Path(tmp) / "tracked.py").write_text("v1\nmodified\n")
            fp1 = ops.read_worktree_mutation()
            self.assertTrue(fp1.readable)
            self.assertTrue(fp1.dirty)
            # The SAME already-modified file gets MORE content — porcelain row stays ` M
            # tracked.py`, but the digest must flip.
            (Path(tmp) / "tracked.py").write_text("v1\nmodified\nmore\n")
            fp2 = ops.read_worktree_mutation()
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_untracked_content_change_flips_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            (Path(tmp) / "residue.py").write_text("a")
            fp1 = ops.read_worktree_mutation()
            self.assertTrue(fp1.untracked)
            # The SAME untracked file grows — `?? residue.py` row unchanged, digest must flip.
            (Path(tmp) / "residue.py").write_text("a" * 500)
            fp2 = ops.read_worktree_mutation()
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_clean_real_repo_is_readable_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            fp = LiveSublaneHibernateOps(repo_root=Path(tmp)).read_worktree_mutation()
            self.assertTrue(fp.readable)
            self.assertFalse(fp.dirty)
            self.assertFalse(fp.untracked)

    def test_binary_tracked_content_change_flips_digest(self) -> None:
        # F1 R2: a BINARY tracked file's content change — `git diff` would collapse to
        # "Binary files ... differ" (same text); `--binary` makes it content-sensitive.
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            binpath = Path(tmp) / "blob.bin"
            binpath.write_bytes(b"\x00\x01\x02" * 100)
            self._git(tmp, "add", "blob.bin")
            self._git(tmp, "commit", "-q", "-m", "add blob")
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            binpath.write_bytes(b"\x00\x01\x03" * 100)  # same length, different bytes
            fp1 = ops.read_worktree_mutation()
            binpath.write_bytes(b"\x00\x01\x04" * 100)  # same length again, different bytes
            fp2 = ops.read_worktree_mutation()
            self.assertTrue(fp1.dirty)
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_same_size_untracked_rewrite_flips_digest(self) -> None:
        # F1 R2: an untracked file rewritten to the SAME size (mtime restored) — a stat-only
        # fingerprint would miss it; the content hash flips the digest.
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            residue = Path(tmp) / "residue.py"
            residue.write_text("AAAAA")
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            fp1 = ops.read_worktree_mutation()
            st = residue.stat()
            residue.write_text("BBBBB")  # same 5-byte size, different content
            os.utime(residue, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
            fp2 = ops.read_worktree_mutation()
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_special_char_untracked_path_is_content_sensitive(self) -> None:
        # F1 R2: a path with a space (git porcelain would QUOTE it in non-`-z`); the `-z`
        # enumeration matches it exactly, so its content change is detected.
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            spaced = Path(tmp) / "a file.py"
            spaced.write_text("x")
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            fp1 = ops.read_worktree_mutation()
            self.assertTrue(fp1.untracked)
            spaced.write_text("y")  # same size, different content
            fp2 = ops.read_worktree_mutation()
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_untracked_symlink_retarget_flips_digest(self) -> None:
        # F1 R4 (review j#83805): an untracked symlink retargeted to a DIFFERENT but
        # SAME-CONTENT target must flip the digest — hashing the link's own target bytes
        # (no-follow), never the followed target's content.
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            (Path(tmp) / "a").write_text("SAME")
            (Path(tmp) / "b").write_text("SAME")
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            os.symlink("a", Path(tmp) / "link")
            fp1 = ops.read_worktree_mutation()
            self.assertTrue(fp1.readable)
            self.assertTrue(fp1.untracked)
            os.remove(Path(tmp) / "link")
            os.symlink("b", Path(tmp) / "link")
            fp2 = ops.read_worktree_mutation()
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_untracked_dangling_symlink_retarget_flips_digest(self) -> None:
        # F1 R4: two dangling symlinks (nonexistent targets) must still be distinguished by
        # their target bytes — never both collapsed to a MISSING marker.
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            ops = LiveSublaneHibernateOps(repo_root=Path(tmp))
            os.symlink("ghost-1", Path(tmp) / "link")
            fp1 = ops.read_worktree_mutation()
            os.remove(Path(tmp) / "link")
            os.symlink("ghost-2", Path(tmp) / "link")
            fp2 = ops.read_worktree_mutation()
            self.assertTrue(fp1.readable)
            self.assertNotEqual(fp1.digest, fp2.digest)

    def test_fifo_hash_fails_closed_without_hanging(self) -> None:
        # F1 R4: `git status` does not enumerate a FIFO, so one only reaches `_hash_untracked`
        # via a TOCTOU swap (a path listed as a regular file, then replaced by a FIFO). The
        # kind-check + O_NONBLOCK must return None (fail closed) WITHOUT blocking — a plain
        # open(fifo) would hang forever. This test returning at all proves it does not hang.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            _hash_untracked,
        )

        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(Path(tmp) / "pipe")
            self.assertIsNone(_hash_untracked(Path(tmp), b"pipe"))

    def test_directory_hash_fails_closed(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            _hash_untracked,
        )

        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(Path(tmp) / "sub")
            self.assertIsNone(_hash_untracked(Path(tmp), b"sub"))

    def test_vanished_untracked_path_fails_closed(self) -> None:
        # A path listed by `git status` but gone by the time we lstat it (a race) fails closed.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            _hash_untracked,
        )

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_hash_untracked(Path(tmp), b"gone.py"))

    def test_untracked_over_size_cap_fails_closed(self) -> None:
        # F1 R4: an untracked file over the per-file byte cap fails closed (never a partial
        # prefix hash). Patch the cap low so the test writes a small file.
        with tempfile.TemporaryDirectory() as tmp:
            self._init_repo(tmp)
            (Path(tmp) / "big.bin").write_bytes(b"x" * 4096)
            with mock.patch(f"{_BOUNDARY_MOD}._MAX_UNTRACKED_FILE_BYTES", 1024):
                fp = LiveSublaneHibernateOps(repo_root=Path(tmp)).read_worktree_mutation()
            self.assertFalse(fp.readable)


class HashUntrackedIdentityStabilityTest(unittest.TestCase):
    """F1 R5 (review j#83853): `_hash_untracked` must fail closed on any identity drift — a
    regular->regular inode swap, a symlink observation-window swap, or a mid-read mutation —
    so the object it classifies is exactly the object it hashes, unchanged throughout."""

    def _mod(self):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary as B  # noqa: E501

        return B

    def test_regular_inode_swap_in_open_window_fails_closed(self) -> None:
        B = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "A").write_text("content-A")
            (Path(tmp) / "B").write_text("content-B-different-inode")
            os.link(Path(tmp) / "A", Path(tmp) / "residue")
            real_lstat = os.lstat
            fired = {"n": 0}

            def racing_lstat(p, *a, **k):
                r = real_lstat(p, *a, **k)
                if str(p).endswith("residue") and fired["n"] == 0:
                    fired["n"] = 1
                    os.replace(Path(tmp) / "B", Path(tmp) / "residue")  # -> different regular inode
                return r

            with mock.patch.object(B.os, "lstat", side_effect=racing_lstat):
                self.assertIsNone(B._hash_untracked(Path(tmp), b"residue"))

    def test_read_during_mutation_fails_closed(self) -> None:
        B = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "r"
            f.write_bytes(b"x" * 200000)
            real_read = os.read
            fired = {"n": 0}

            def racing_read(fd, n):
                chunk = real_read(fd, n)
                if chunk and fired["n"] == 0:
                    fired["n"] = 1
                    with open(f, "ab") as handle:  # rewrite in place -> size / mtime drift
                        handle.write(b"MORE")
                return chunk

            with mock.patch.object(B.os, "read", side_effect=racing_read):
                self.assertIsNone(B._hash_untracked(Path(tmp), b"r"))

    def test_same_size_mtime_restored_mid_read_fails_closed(self) -> None:
        # F1 R6 (review j#83889): a same-inode, SAME-SIZE in-place rewrite during the read
        # with mtime RESTORED via os.utime leaves (dev, ino, size, mtime) equal — only ctime
        # (not user-settable) drifts. The ctime check must still fail closed.
        B = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "r"
            f.write_bytes(b"A" * 200000)
            st0 = os.stat(f)
            real_read = os.read
            fired = {"n": 0}

            def racing_read(fd, n):
                chunk = real_read(fd, n)
                if chunk and fired["n"] == 0:
                    fired["n"] = 1
                    with open(f, "r+b") as handle:  # same-size in-place rewrite
                        handle.seek(0)
                        handle.write(b"B" * 200000)
                    os.utime(f, ns=(st0.st_atime_ns, st0.st_mtime_ns))  # restore mtime
                return chunk

            with mock.patch.object(B.os, "read", side_effect=racing_read):
                self.assertIsNone(B._hash_untracked(Path(tmp), b"r"))

    def test_symlink_swap_in_readlink_window_fails_closed(self) -> None:
        B = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink("target-one", Path(tmp) / "link")
            real_readlink = os.readlink
            fired = {"n": 0}

            def racing_readlink(p, *a, **k):
                out = real_readlink(p, *a, **k)
                if str(p).endswith("link") and fired["n"] == 0:
                    fired["n"] = 1
                    os.remove(Path(tmp) / "link")
                    os.symlink("target-two", Path(tmp) / "link")  # different inode
                return out

            with mock.patch.object(B.os, "readlink", side_effect=racing_readlink):
                self.assertIsNone(B._hash_untracked(Path(tmp), b"link"))

    def test_stable_regular_file_hashes(self) -> None:
        B = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "r").write_text("stable")
            self.assertIsNotNone(B._hash_untracked(Path(tmp), b"r"))

    def test_stable_symlink_hashes(self) -> None:
        B = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink("some-target", Path(tmp) / "link")
            self.assertIsNotNone(B._hash_untracked(Path(tmp), b"link"))

# ---------------------------------------------------------------------------
# F2: live lane-activity probe (running turn / pending composer), via the live path.
# ---------------------------------------------------------------------------

_HSS = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application"
    ".herdr_session_start._resolve_binary_or_die"
)
_STATE_READER = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure"
    ".herdr_state.HerdrCliAgentStateReader"
)
_TRANSPORT = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure"
    ".herdr_transport.HerdrCliTransport"
)
_OBSERVE = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_quarantine.observe_composer_text"
)
_GHOST = (
    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
    ".application.sublane_ghost_composer_observation"
)


class _Obs:
    def __init__(self, readable, has_pending):
        self.readable = readable
        self.has_pending = has_pending
        self.marker_ids = ()


def _state_reader(state_ok=True, state="turn_ended"):
    inst = mock.MagicMock()
    inst.read_agent_state.return_value = mock.MagicMock(ok=state_ok, state=state)
    return mock.MagicMock(return_value=inst)


def _transport(read_ok=True, content=""):
    inst = mock.MagicMock()
    inst.read_pane.return_value = mock.MagicMock(ok=read_ok, content=content)
    return mock.MagicMock(return_value=inst)


class LiveLaneActivityTest(unittest.TestCase):
    """F2 (Redmine #13843): the REAL live worker-busy / pending-composer observation and its
    fail-closed behaviour, exercised through the live adapter path (read_live_lane_activity)."""

    def _rows(self):
        return [_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")]

    def _run(self, *, state_ok=True, state="turn_ended", read_ok=True, has_pending=False):
        with mock.patch(_HSS, return_value="herdr"), mock.patch(
            _STATE_READER, _state_reader(state_ok, state)
        ), mock.patch(_TRANSPORT, _transport(read_ok, "text")), mock.patch(
            _OBSERVE, return_value=_Obs(read_ok, has_pending)
        ), mock.patch(
            f"{_GHOST}.default_ghost_policy", return_value=None
        ), mock.patch(
            f"{_GHOST}.apply_ghost_empty", side_effect=lambda hp, **kw: hp
        ):
            return read_live_lane_activity(
                self._rows(), WS, LANE,
                repo_root=Path("."), env={}, runner=None, timeout=5.0,
            )

    def test_idle_lane_is_readable_quiescent(self) -> None:
        act = self._run(state="turn_ended", has_pending=False)
        self.assertTrue(act.readable)
        self.assertFalse(act.worker_busy)
        self.assertFalse(act.composer_pending)

    def test_busy_worker_is_observed(self) -> None:
        act = self._run(state="busy")
        self.assertTrue(act.readable)
        self.assertTrue(act.worker_busy)

    def test_pending_composer_is_observed(self) -> None:
        act = self._run(has_pending=True)
        self.assertTrue(act.readable)
        self.assertTrue(act.composer_pending)

    def test_unreadable_runtime_state_fails_closed(self) -> None:
        act = self._run(state_ok=False)
        self.assertFalse(act.readable)

    def test_successful_unknown_state_fails_closed(self) -> None:
        # F2 R2: a SUCCESSFUL read (ok=True) whose state is `unknown` (observed-but-
        # unrecognised) must fail closed, not be treated as idle.
        act = self._run(state_ok=True, state="unknown")
        self.assertFalse(act.readable)

    def test_blocked_state_is_non_quiescent(self) -> None:
        # F2 R2: a `blocked` permission-prompt (in-flight) is NON-quiescent -> worker_busy,
        # never released over.
        act = self._run(state="blocked")
        self.assertTrue(act.readable)
        self.assertTrue(act.worker_busy)

    def test_awaiting_input_is_quiescent(self) -> None:
        act = self._run(state="awaiting_input")
        self.assertTrue(act.readable)
        self.assertFalse(act.worker_busy)

    def test_unreadable_composer_read_fails_closed(self) -> None:
        act = self._run(read_ok=False)
        self.assertFalse(act.readable)

    def test_no_live_slots_is_readable_quiescent(self) -> None:
        # Nothing live to observe (nothing to release either) -> vacuously readable-quiescent.
        act = read_live_lane_activity(
            [], WS, LANE, repo_root=Path("."), env={}, runner=None, timeout=5.0
        )
        self.assertTrue(act.readable)
        self.assertFalse(act.worker_busy)


# ---------------------------------------------------------------------------
# F3: boundary (T1) re-validation re-reads lifecycle revision + attestation.
# ---------------------------------------------------------------------------


class RevalidateBoundaryReReadTest(unittest.TestCase):
    """F3 (Redmine #13843): revalidate_boundary re-reads the lifecycle revision and (for a
    project lane) the exact declared generation + startup attestation on the fresh snapshot."""

    def _rows(self):
        return [_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")]

    def test_revision_drift_since_preflight_blocks(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            revalidate_boundary,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            rec0 = store.get(key)
            # Another process advances the lane's authority (revision bump) after preflight.
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=rec0.revision,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(),
            )
            self.assertGreater(store.get(key).revision, rec0.revision)
            ops = _FakeOps(rows=self._rows())
            _rows1, _fp, reasons, _ghost = revalidate_boundary(
                ops=ops, store=store, key=key, rec0=rec0, rows0=self._rows(),
                fingerprint_preflight=_CLEAN_FP, workspace_id=WS, lane=LANE,
                project_scope="",
            )
            self.assertIn(BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT, reasons)

    def test_no_drift_when_stable(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            revalidate_boundary,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            rec0 = store.get(key)
            ops = _FakeOps(rows=self._rows())
            _rows1, _fp, reasons, _ghost = revalidate_boundary(
                ops=ops, store=store, key=key, rec0=rec0, rows0=self._rows(),
                fingerprint_preflight=_CLEAN_FP, workspace_id=WS, lane=LANE,
                project_scope="",
            )
            self.assertEqual(reasons, ())

    def test_project_attestation_drift_at_boundary_blocks(self) -> None:
        # A project lane whose gateway slot loses its attestation between preflight and the
        # boundary re-read blocks on the boundary attestation fence.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
            revalidate_boundary,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            decl = LaneDeclarationStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, PG_LANE)
            decl.declare_lane(
                key,
                decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL),
                binding_kind=BINDING_KIND_PROJECT_GATEWAY,
                project_scope=PG_SCOPE,
                declared_slots=(
                    ProcessGenerationPin(
                        role=PIN_ROLE_GATEWAY, provider="codex",
                        assigned_name=PG_GW_NAME, locator=PG_GW_LOC,
                    ),
                    ProcessGenerationPin(
                        role=PIN_ROLE_WORKER, provider="claude",
                        assigned_name=PG_WK_NAME, locator=PG_WK_LOC,
                    ),
                ),
            )
            rec0 = store.get(key)
            rows = [{"name": PG_GW_NAME, "pane_id": PG_GW_LOC},
                    {"name": PG_WK_NAME, "pane_id": PG_WK_LOC}]
            # Worker attested, gateway NOT -> boundary attestation fence fires.
            atts = _pg_attestations()
            del atts[PG_GW_NAME]
            ops = _FakeOps(rows=rows, attestations=atts)
            _rows1, _fp, reasons, _ghost = revalidate_boundary(
                ops=ops, store=store, key=key, rec0=rec0, rows0=rows,
                fingerprint_preflight=_CLEAN_FP, workspace_id=WS, lane=PG_LANE,
                project_scope=PG_SCOPE,
            )
            self.assertIn(BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT, reasons)


# ---------------------------------------------------------------------------
# F5: partial release is not a clean success.
# ---------------------------------------------------------------------------


class PartialReleaseSuccessTest(unittest.TestCase):
    """F5 (Redmine #13843): a partial (incomplete) release is not a fully-actuated success."""

    def _rows(self):
        return [_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")]

    def test_partial_release_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            partial = HerdrRetireCloseResult(
                workspace_id=WS, lane_id=LANE,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            outcome = SublaneHibernateUseCase(
                ops=_FakeOps(rows=self._rows(), close_result=partial), store=store
            ).run(_request(), execute=True)
            self.assertEqual(outcome.release.process_release, RELEASE_PARTIAL)
            # Executed and not blocked / withheld, but NOT a clean success (re-drive needed).
            self.assertFalse(outcome.is_blocked)
            self.assertFalse(outcome.success_withheld)
            self.assertFalse(outcome.is_success)

    def test_released_is_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            outcome = SublaneHibernateUseCase(
                ops=_FakeOps(rows=self._rows()), store=store
            ).run(_request(), execute=True)
            self.assertEqual(outcome.release.process_release, RELEASE_RELEASED)
            self.assertTrue(outcome.is_success)

    def test_cmd_returns_nonzero_on_partial_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LaneLifecycleStore(home=Path(tmp))
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            partial = HerdrRetireCloseResult(
                workspace_id=WS, lane_id=LANE,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            ops = _FakeOps(rows=self._rows(), close_result=partial)
            args = argparse.Namespace(
                repo=None, issue=ISSUE, lane=LANE, journal=JOURNAL,
                explicitly_parked=True, callbacks_drained=True, no_review_pending=True,
                no_owner_approval_pending=True, no_integration_pending=True,
                no_pending_prompt=True, not_working=True, worktree_clean=True,
                boundary_recorded=False, execute=True, json=False,
            )
            with mock.patch(
                f"{_CLI_MOD}.LiveSublaneHibernateOps", return_value=ops
            ), mock.patch(f"{_CLI_MOD}.LaneLifecycleStore", return_value=store):
                rc = cmd_sublane_hibernate(args)
            self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# F3: the release driver is bound to the caller's T1-verified lifecycle revision.
# ---------------------------------------------------------------------------


class DriveReleaseExpectedRevisionTest(unittest.TestCase):
    """F3 (Redmine #13843): drive_process_release admission-blocks (zero-close) when the
    fresh row read no longer carries the caller's expected (T1-verified) revision."""

    def _rows(self):
        return [_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")]

    def _hibernated_store(self, tmp):
        store = LaneLifecycleStore(home=Path(tmp))
        key = LaneLifecycleKey(WS, LANE)
        store.declare_active(key, decision=_decision(), issue_id=ISSUE)
        store.transition_disposition(
            key, expected_disposition=DISPOSITION_ACTIVE, expected_revision=1,
            target=DISPOSITION_HIBERNATED, decision=_decision(),
        )
        return store, key

    def test_revision_mismatch_admission_blocks_zero_close(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            drive_process_release,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._hibernated_store(tmp)
            current = store.get(key).revision
            ops = _FakeOps(rows=self._rows())
            outcome = drive_process_release(
                store=store, ops=ops, key=key, lane_id=LANE, workspace_id=WS,
                action_id="hibernate:x", rows=self._rows(),
                expected_revision=current + 5,  # stale / advanced authority
            )
            self.assertTrue(outcome.admission_blocked)
            self.assertEqual(outcome.closed, ())
            self.assertEqual(ops.close_calls, [])

    def test_matching_revision_releases(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
            drive_process_release,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store, key = self._hibernated_store(tmp)
            current = store.get(key).revision
            ops = _FakeOps(rows=self._rows())
            outcome = drive_process_release(
                store=store, ops=ops, key=key, lane_id=LANE, workspace_id=WS,
                action_id="hibernate:x", rows=self._rows(),
                expected_revision=current,
            )
            self.assertFalse(outcome.admission_blocked)
            self.assertEqual(outcome.process_release, RELEASE_RELEASED)


class _RevisionRaceStore(LaneLifecycleStore):
    """A store that advances the lane's revision right before the release driver's row read
    (the Nth get), reproducing a concurrent external authority advance in the T1->driver
    window (Redmine #13843 review F3)."""

    def __init__(self, *args, bump_at_get, **kwargs):
        super().__init__(*args, **kwargs)
        self._gets = 0
        self._bump_at_get = bump_at_get

    def get(self, key):
        self._gets += 1
        if self._gets == self._bump_at_get:
            rec = super().get(key)
            if rec is not None:
                # A benign external authority advance (re-record the partial outcome): the
                # revision bumps, so the driver's next read no longer matches the T1 revision.
                super().record_release_outcome(
                    key, action_id=rec.release_action_id,
                    expected_revision=rec.revision, target=RELEASE_PARTIAL,
                )
        return super().get(key)


class RedriveRevisionRaceTest(unittest.TestCase):
    """F3 (Redmine #13843): a redrive whose lifecycle revision advances between the T1
    re-validation and the driver read admission-blocks (zero-close), never resuming on the
    stale authority."""

    def _rows(self):
        return [_row("codex", LANE, f"{WS}:p2"), _row("claude", LANE, f"{WS}:p3")]

    def test_redrive_revision_race_blocks_zero_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Seed a hibernated lane with an OPEN partial release (so the redrive resumes it).
            store = LaneLifecycleStore(home=Path(tmp))
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(), issue_id=ISSUE)
            partial = HerdrRetireCloseResult(
                workspace_id=WS, lane_id=LANE,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            SublaneHibernateUseCase(
                ops=_FakeOps(rows=self._rows(), close_result=partial), store=store
            ).run(_request(), execute=True)

            # Re-open the same lane under a race store that advances the revision right before
            # the redrive driver's read (the 3rd get: T0, T1, driver).
            race = _RevisionRaceStore(home=Path(tmp), bump_at_get=3)
            retry_ops = _FakeOps(rows=self._rows())
            outcome = SublaneHibernateUseCase(ops=retry_ops, store=race).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.already_hibernated)
            self.assertTrue(outcome.redrive_blocked)
            self.assertIn(
                BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT, outcome.boundary_reasons
            )
            self.assertIsNone(outcome.release)
            self.assertEqual(retry_ops.close_calls, [])


if __name__ == "__main__":
    unittest.main()
