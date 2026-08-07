"""Regression pins for the #14716 hibernated-unbound terminal retire rail.

All state is isolated under a temporary ``MOZYO_BRIDGE_HOME``.  The tests use a
fabricated Redmine transport and inventory; they never contact Redmine, close a process,
remove a checkout, or update a Git ref.
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
from types import SimpleNamespace
from unittest import mock

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_metadata import record_lane_created  # noqa: E402
from mozyo_bridge.core.state.lane_release_observation import (  # noqa: E402
    build_release_observation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_hibernated_unbound_live_zero_retire as rail,
    sublane_lifecycle_command,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_sublane_retire import (  # noqa: E402,E501
    register_sublane_retire,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E402,E501
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E402,E501
    CONVERGE_ALREADY_HIBERNATED,
    CONVERGE_BLOCKED,
    CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND,
    REASON_HIBERNATED_RELEASE_UNPROVEN,
    REASON_UNKNOWN_LIFECYCLE_DISPOSITION,
    REASON_UNKNOWN_PROCESS_RELEASE,
    RebootLaneFacts,
    plan_lane_convergence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_WORKSPACE = "wIssue14716"
_LANE = "issue_13822_startup_admission_r3"
_ISSUE = "13822"
_JOURNAL = "100001"


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=_ISSUE, journal_id=_JOURNAL)


def _closed_payload(*, issue: str = _ISSUE, journal: str = _JOURNAL) -> dict:
    return {
        "issue": {
            "id": issue,
            "status": {"is_closed": True},
            "journals": [{"id": journal}],
        }
    }


class _FakeTransport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def __call__(self, *, issue_id: str, **_kwargs):
        self.calls.append(issue_id)
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def _source_for(transport: _FakeTransport):
    return SimpleNamespace(
        base_url="https://redmine.invalid",
        api_key="test-only-value",
        transport=transport,
    )


class FreshRedmineSnapshotTests(unittest.TestCase):
    """Closed status and journal must come from one exact live response."""

    def _verify(self, payload: object):
        transport = _FakeTransport(payload)
        with mock.patch.object(
            LiveRedmineJournalSource,
            "from_environment",
            return_value=_source_for(transport),
        ):
            verdict = rail._fresh_closed_decision_snapshot(_ISSUE, _JOURNAL)
        return verdict, transport.calls

    def test_exact_closed_issue_and_journal_pass_from_one_fetch(self) -> None:
        verdict, calls = self._verify(_closed_payload())
        self.assertEqual(verdict, (True, "", ""))
        self.assertEqual(calls, [_ISSUE])

    def test_top_level_wrapper_journals_are_the_authoritative_shape(self) -> None:
        payload = _closed_payload(journal="nested-stale")
        payload["journals"] = [{"id": _JOURNAL}]
        verdict, calls = self._verify(payload)
        self.assertEqual(verdict, (True, "", ""))
        self.assertEqual(calls, [_ISSUE])

    def test_open_issue_refuses_from_the_same_snapshot(self) -> None:
        payload = _closed_payload()
        payload["issue"]["status"]["is_closed"] = False
        verdict, calls = self._verify(payload)
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], rail.HIBERNATED_UNBOUND_RETIRE_ISSUE_NOT_CLOSED)
        self.assertEqual(calls, [_ISSUE])

    def test_wrong_issue_and_missing_journal_each_refuse(self) -> None:
        for payload, reason in (
            (
                _closed_payload(issue="99999"),
                rail.HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE,
            ),
            (
                _closed_payload(journal="99999"),
                rail.HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND,
            ),
        ):
            with self.subTest(reason=reason):
                verdict, calls = self._verify(payload)
                self.assertFalse(verdict[0])
                self.assertEqual(verdict[1], reason)
                self.assertEqual(calls, [_ISSUE])

    def test_whitespace_wrapped_identifiers_are_not_normalized_into_authority(self) -> None:
        verdict, _ = self._verify(_closed_payload(issue=f" {_ISSUE} "))
        self.assertFalse(verdict[0])
        verdict, _ = self._verify(_closed_payload(journal=f" {_JOURNAL} "))
        self.assertFalse(verdict[0])

    def test_any_transport_failure_is_a_typed_refusal(self) -> None:
        verdict, calls = self._verify(RuntimeError("synthetic read failure"))
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], rail.HIBERNATED_UNBOUND_RETIRE_REDMINE_UNREADABLE)
        self.assertEqual(calls, [_ISSUE])


def _facts(**overrides) -> RebootLaneFacts:
    values = dict(
        workspace_id=_WORKSPACE,
        lane_id=_LANE,
        issue_id=_ISSUE,
        lane_disposition=DISPOSITION_HIBERNATED,
        process_release=RELEASE_RELEASED,
        worktree_identity="",
        recorded_worktree="/private/tmp/gone-issue-13822",
        worktree_present=False,
        branch="issue_13822_r3",
        branch_exists=True,
        head_integrated=True,
        issue_closed=True,
        lane_generation=3,
        revision=17,
        slots=(),
    )
    values.update(overrides)
    return RebootLaneFacts(**values)


class PlannerTests(unittest.TestCase):
    def test_hibernated_released_unbound_row_selects_the_new_public_flag(self) -> None:
        plan = plan_lane_convergence(_facts())
        self.assertEqual(plan.convergence, CONVERGE_TERMINALIZE_HIBERNATED_UNBOUND)
        self.assertTrue(plan.actionable)
        command = " ".join(plan.steps)
        self.assertIn("--retire-hibernated-unbound-live-zero", command)
        self.assertIn("--expect-lane-generation 3", command)
        self.assertIn("--expect-lane-revision 17", command)
        self.assertNotIn("git worktree add", command)

    def test_open_hibernated_original_with_active_peer_is_already_hibernated(self) -> None:
        plan = plan_lane_convergence(
            _facts(issue_closed=False, peer_active_lanes=("issue_14567_successor",))
        )
        self.assertEqual(plan.convergence, CONVERGE_ALREADY_HIBERNATED)
        self.assertFalse(plan.actionable)
        self.assertFalse(plan.steps)

    def test_unknown_or_padded_states_never_become_destructive_authority(self) -> None:
        plan = plan_lane_convergence(_facts(lane_disposition=" hibernated "))
        self.assertEqual(plan.convergence, CONVERGE_BLOCKED)
        self.assertEqual(plan.reason, REASON_UNKNOWN_LIFECYCLE_DISPOSITION)

        plan = plan_lane_convergence(_facts(process_release="future_release_state"))
        self.assertEqual(plan.convergence, CONVERGE_BLOCKED)
        self.assertEqual(plan.reason, REASON_UNKNOWN_PROCESS_RELEASE)

    def test_hibernated_row_without_released_witness_is_blocked(self) -> None:
        plan = plan_lane_convergence(_facts(process_release=RELEASE_NOT_REQUESTED))
        self.assertEqual(plan.convergence, CONVERGE_BLOCKED)
        self.assertEqual(plan.reason, REASON_HIBERNATED_RELEASE_UNPROVEN)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    )
    return result.stdout


class PublicRailTests(unittest.TestCase):
    """Exercise planner output through the actual ``sublane retire`` command."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.missing_worktree = self.root / "gone-lane-worktree"
        self.other_worktree = self.root / "different-worktree"

        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "base.txt")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "branch", "lane-integrated")
        (self.repo / "main.txt").write_text("main\n", encoding="utf-8")
        _git(self.repo, "add", "main.txt")
        _git(self.repo, "commit", "-qm", "main advances")

        self.key = LaneLifecycleKey(_WORKSPACE, _LANE)
        self.store = LaneLifecycleStore()
        declared = LaneDeclarationStore(path=self.store.path).declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=(
                ProcessGenerationPin(
                    role="gateway",
                    provider="codex",
                    assigned_name=encode_assigned_name(_WORKSPACE, "codex", _LANE),
                    locator="w1:p1",
                ),
                ProcessGenerationPin(
                    role="worker",
                    provider="claude",
                    assigned_name=encode_assigned_name(_WORKSPACE, "claude", _LANE),
                    locator="w1:p2",
                ),
            ),
            worktree_identity="",
        )
        self.assertTrue(declared.applied, declared.reason)
        row = self.store.get(self.key)
        moved = self.store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=row.revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertTrue(moved.applied, moved.reason)
        row = self.store.get(self.key)
        requested = self.store.request_release(
            self.key,
            expected_revision=row.revision,
            action_id="release-14716",
            observation=build_release_observation(
                (
                    ReleasePin("gateway", "codex", "w1:p1"),
                    ReleasePin("worker", "claude", "w1:p2"),
                )
            ),
        )
        self.assertTrue(requested.applied, requested.reason)
        row = self.store.get(self.key)
        released = self.store.record_release_outcome(
            self.key,
            action_id="release-14716",
            expected_revision=row.revision,
            target=RELEASE_RELEASED,
        )
        self.assertTrue(released.applied, released.reason)

        metadata = record_lane_created(
            lane_workspace_token="wt_issue_14716",
            repo_workspace_id=_WORKSPACE,
            issue_id=_ISSUE,
            lane_label=_LANE,
            branch="lane-integrated",
            worktree_path=str(self.missing_worktree),
            lane_id=_LANE,
        )
        self.assertIsNotNone(metadata)

        self.inventory: list[dict] = []
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
            sublane_herdr_retire,
            workflow_provider_resolution as providers,
        )

        for patcher in (
            mock.patch.object(projection, "repo_backend_is_herdr", return_value=True),
            mock.patch.object(
                projection, "repo_scope_workspace_id", return_value=_WORKSPACE
            ),
            mock.patch.object(
                projection,
                "list_herdr_agent_rows",
                side_effect=lambda *_a, **_k: list(self.inventory),
            ),
            mock.patch.object(providers, "resolve_gateway_provider", return_value="codex"),
            mock.patch.object(providers, "resolve_worker_provider", return_value="claude"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.close_mock = mock.patch.object(
            sublane_herdr_retire,
            "execute_herdr_retire_close",
            side_effect=AssertionError("metadata-only rail must not close a process"),
        )
        self.close_spy = self.close_mock.start()
        self.addCleanup(self.close_mock.stop)

        self.transport = _FakeTransport(_closed_payload())
        redmine = mock.patch.object(
            LiveRedmineJournalSource,
            "from_environment",
            return_value=_source_for(self.transport),
        )
        redmine.start()
        self.addCleanup(redmine.stop)

    def _row(self):
        return self.store.get(self.key)

    def _args(self, **overrides) -> argparse.Namespace:
        row = self._row()
        values = dict(
            repo=str(self.repo),
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.missing_worktree),
            branch="lane-integrated",
            integration_branch="main",
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
            review_exemption_json=None,
            no_change_review_waiver=False,
            superseded_failure_terminal=False,
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=False,
            retire_active_live_zero=False,
            retire_active_unbound_live_zero=False,
            retire_hibernated_unbound_live_zero=True,
            expect_lane_generation=row.lane_generation,
            expect_lane_revision=row.revision,
            integration_journal=None,
            json=True,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def _run(self, args: argparse.Namespace):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        return code, json.loads(output.getvalue())

    def test_missing_checkout_retires_then_replays_without_git_or_process_mutation(self) -> None:
        self.assertFalse(self.missing_worktree.exists())
        before_refs = _git(self.repo, "show-ref", "--heads")
        before_status = _git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")

        args = self._args()
        code, payload = self._run(args)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["retire_ok"])
        self.assertEqual(
            payload["hibernated_unbound_live_zero_retire"]["state"], "retired"
        )
        first = self._row()
        self.assertEqual(first.lane_disposition, DISPOSITION_RETIRED)
        self.assertEqual(self.transport.calls, [_ISSUE])

        replay_code, replay = self._run(args)
        self.assertEqual(replay_code, 0, replay)
        self.assertEqual(
            replay["hibernated_unbound_live_zero_retire"]["state"],
            "already_retired",
        )
        self.assertEqual(self._row().revision, first.revision)
        self.assertEqual(self.transport.calls, [_ISSUE, _ISSUE])
        self.assertFalse(self.missing_worktree.exists())
        self.assertEqual(_git(self.repo, "show-ref", "--heads"), before_refs)
        self.assertEqual(
            _git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"),
            before_status,
        )
        self.close_spy.assert_not_called()

    def test_stale_revision_refuses_without_writing(self) -> None:
        before = self._row()
        code, payload = self._run(
            self._args(expect_lane_revision=before.revision - 1)
        )
        self.assertEqual(code, 1)
        verdict = payload["hibernated_unbound_live_zero_retire"]
        self.assertEqual(verdict["reason"], rail.HIBERNATED_UNBOUND_RETIRE_GENERATION_RACE)
        after = self._row()
        self.assertEqual((after.lane_disposition, after.revision), (before.lane_disposition, before.revision))

    def test_non_herdr_backend_is_typed_refusal_without_writing(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
        )

        before = self._row()
        with mock.patch.object(
            projection, "repo_backend_is_herdr", return_value=False
        ):
            code, payload = self._run(self._args())

        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["retire_ok"])
        verdict = payload["hibernated_unbound_live_zero_retire"]
        self.assertEqual(verdict["state"], rail.HIBERNATED_UNBOUND_RETIRE_BLOCKED)
        self.assertEqual(
            verdict["reason"], rail.HIBERNATED_UNBOUND_RETIRE_NOT_HERDR_BACKEND
        )
        after = self._row()
        self.assertEqual(
            (after.lane_disposition, after.revision),
            (before.lane_disposition, before.revision),
        )
        self.assertEqual(self.transport.calls, [])
        self.close_spy.assert_not_called()

    def test_open_issue_or_missing_journal_refuses_without_writing(self) -> None:
        for payload, expected in (
            (
                {
                    "issue": {
                        "id": _ISSUE,
                        "status": {"is_closed": False},
                        "journals": [{"id": _JOURNAL}],
                    }
                },
                rail.HIBERNATED_UNBOUND_RETIRE_ISSUE_NOT_CLOSED,
            ),
            (
                _closed_payload(journal="other"),
                rail.HIBERNATED_UNBOUND_RETIRE_DECISION_JOURNAL_NOT_FOUND,
            ),
        ):
            with self.subTest(reason=expected):
                before = self._row()
                self.transport.payload = payload
                code, result = self._run(self._args())
                self.assertEqual(code, 1)
                self.assertEqual(
                    result["hibernated_unbound_live_zero_retire"]["reason"], expected
                )
                after = self._row()
                self.assertEqual(after.revision, before.revision)
                self.assertEqual(after.lane_disposition, before.lane_disposition)

    def test_live_pair_and_worktree_metadata_mismatch_each_refuse(self) -> None:
        before = self._row()
        self.inventory = [
            {
                "name": encode_assigned_name(_WORKSPACE, "codex", _LANE),
                "pane": "w1:p1",
                "agent": "codex",
                "agent_status": "idle",
            }
        ]
        code, payload = self._run(self._args())
        self.assertEqual(code, 1)
        self.assertEqual(
            payload["hibernated_unbound_live_zero_retire"]["reason"],
            "live_pair_present",
        )
        self.assertEqual(self._row().revision, before.revision)

        self.inventory = []
        code, payload = self._run(self._args(worktree=str(self.other_worktree)))
        self.assertEqual(code, 1)
        self.assertEqual(
            payload["hibernated_unbound_live_zero_retire"]["reason"],
            rail.HIBERNATED_UNBOUND_RETIRE_WORKTREE_CONFLICT,
        )
        self.assertEqual(self._row().revision, before.revision)


class CliSurfaceTests(unittest.TestCase):
    def test_parser_exposes_the_seventh_intent_and_both_fences(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register_sublane_retire(
            sub,
            add_repo_option=lambda p: p.add_argument("--repo"),
            add_lifecycle_json=lambda p: p.add_argument("--json", action="store_true"),
        )
        args = parser.parse_args(
            [
                "retire",
                "--issue",
                _ISSUE,
                "--lane-label",
                _LANE,
                "--retire-hibernated-unbound-live-zero",
                "--expect-lane-generation",
                "3",
                "--expect-lane-revision",
                "17",
            ]
        )
        self.assertTrue(args.retire_hibernated_unbound_live_zero)
        self.assertEqual(args.expect_lane_generation, 3)
        self.assertEqual(args.expect_lane_revision, 17)

    def test_seventh_intent_is_mutually_exclusive_before_any_probe(self) -> None:
        args = argparse.Namespace(
            issue=_ISSUE,
            lane_label=_LANE,
            retire_hibernated_unbound_live_zero=True,
            retire_active_unbound_live_zero=True,
        )
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", error.getvalue())
        self.assertIn("--retire-hibernated-unbound-live-zero", error.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
