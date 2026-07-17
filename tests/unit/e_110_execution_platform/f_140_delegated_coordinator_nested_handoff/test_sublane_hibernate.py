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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
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
    BLOCK_REVIEW_PENDING,
    BLOCK_UNPUSHED_COMMITS,
    BLOCK_UNRECORDED_BOUNDARY,
    BLOCK_WORKING,
    PARK_BASIS_DEPENDENCY,
    PARK_BASIS_EARLY_HIBERNATE,
    HibernateAssertions,
    HibernateRequest,
    LiveSublaneHibernateOps,
    SublaneHibernateUseCase,
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


class _FakeOps:
    """Fake hibernate IO port: canned workspace / inventory (rows + readability) / close."""

    def __init__(self, *, rows, close_result=None, readable=True):
        self._rows = list(rows)
        self._readable = readable
        self._close_result = close_result
        self.close_calls: list = []

    def workspace_id(self) -> str:
        return WS

    def read_inventory(self):
        return list(self._rows), self._readable

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


if __name__ == "__main__":
    unittest.main()
