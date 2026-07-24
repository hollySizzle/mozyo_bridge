"""T2c production-composition actuation (Redmine #14219, ruling j#86730 required tests).

The production positive drives the REAL composition root — ``build_supervisor(home=...)``
``.run_once(mode=hibernate)`` over real home-scoped stores, a real temp git origin, a committed
config blob, and the real public hibernate use case — with only the external I/O boundaries
faked (the Redmine source factory, the herdr binary/runtime observation). One eligible,
fully-evidenced early lane hibernates exactly once. The negatives pin the ruling's conditions:
the obligation transcription reads the SAME fresh memo as the refresh (a later review_request
flips it), and a dependency-park-only lane transcribes nothing and mutates nothing.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer, LaneLifecycleKey
from mozyo_bridge.core.state.lane_metadata import record_lane_created
from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_marker import (  # noqa: E501
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_REQUIRED_CI_GREEN,
    render_hibernate_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_integration import (  # noqa: E501
    render_integration_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
    render_workflow_event_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SUPERVISION_HIBERNATE,
)

ROOT = Path(__file__).resolve().parents[2]

ISSUE = "600"
RELEASE_ISSUE = "900"
LANE = "lane_t2c_1"
REQ_JOURNAL = "85000"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
}


class _FakeSource:
    """A per-issue journal-page source (the ONE external Redmine boundary).

    ``on_read`` (issue, nth) runs BEFORE serving each page and may return a replacement page —
    the seam the freshness regressions use to change the world between the build read and the
    actuation-phase fresh read (review j#86734 R2-F1).
    """

    def __init__(self, pages, on_read=None, created_on=""):
        self.pages = pages
        self.reads = []
        self.on_read = on_read
        # Review j#87204 R3-F2: when set, every projected entry carries this provider ``created_on``
        # (the drain-ready START authority) — the regression that a real timestamp must NOT falsify
        # candidate freshness. The default "" reproduces the pre-R2-F2(a) provider projection.
        self.created_on = created_on

    def read_entries(self, issue_id):
        issue = str(issue_id)
        nth = sum(1 for read in self.reads if read == issue)
        self.reads.append(issue)
        page = self.pages.get(issue, ())
        if self.on_read is not None:
            replaced = self.on_read(issue, nth)
            if replaced is not None:
                page = replaced
        return [
            RedmineJournalEntry(
                issue_id=issue, journal_id=str(jid), notes=notes, created_on=self.created_on
            )
            for jid, notes in page
        ]


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True,
        env={**os.environ, **_GIT_ENV},
    )


class T2cProductionActuationTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.home = self.dir / "home"
        self.home.mkdir()
        # The workspace repo with a COMMITTED config (the Fork A policy blob), and a bare
        # origin carrying the lane branch (the head observation).
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".mozyo-bridge").mkdir()
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text("version: 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "c1")
        self.origin = self.dir / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        # The lane's canonical WORKTREE: a real `git worktree` of the WORKSPACE repo, so the
        # live ops' workspace-segment derivation (git topology) resolves back to the registered
        # workspace — exactly the production lane shape. Review j#86739 R3-F2: lane_label and
        # branch are independent create-contract fields, so the checked-out branch deliberately
        # differs from the lane id, and the head observation must follow the ACTUAL branch.
        self.branch = "feature/t2c_decoupled"
        self.worktree = self.dir / "wt-lane"
        _git(self.repo, "worktree", "add", "-q", str(self.worktree), "-b", self.branch)
        _git(self.repo, "push", "-q", "origin", self.branch)
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        self.token = derive_lane_workspace_token(str(self.worktree.resolve()))
        # Registered workspace identity — the SAME id the live ops derive from the worktree.
        register_workspace(self.repo, home=self.home)
        self.workspace_id = read_anchor(self.repo)["workspace_id"]
        # A fake herdr binary: `agent list` yields an empty inventory (no live rows to close).
        self.herdr = self.dir / "fake-herdr"
        self.herdr.write_text('#!/bin/sh\necho \'{"agents": []}\'\n')
        self.herdr.chmod(self.herdr.stat().st_mode | stat.S_IEXEC)

        self.store = LaneLifecycleStore(home=self.home)
        self.store.declare_active(
            LaneLifecycleKey(repo_workspace_id=self.workspace_id, lane_id=LANE),
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="84999"),
            issue_id=ISSUE,
            worktree_identity=self.token,
        )
        record_lane_created(
            lane_workspace_token=self.token,
            repo_workspace_id=self.workspace_id,
            issue_id=ISSUE,
            lane_label=LANE,
            worktree_path=str(self.worktree.resolve()),
            home=self.home,
        )

    def _envelope(self):
        return LaneEvidenceEnvelope(
            workspace=self.workspace_id, lane=LANE, lane_generation=1, head=self.head
        )

    def _early_page(self):
        env = self._envelope()
        return (
            (REQ_JOURNAL, "request\n" + render_workflow_event_marker(
                "review_request", target_head=self.head
            )),
            ("85001", "review\n" + render_workflow_event_marker(
                "review_result", target_head=self.head,
                review_request_journal=REQ_JOURNAL, conclusion="approved",
                evidence_workspace=self.workspace_id, evidence_lane=LANE,
                evidence_lane_generation=1,
            )),
            ("85002", "## Integration disposition\n" + render_integration_evidence(
                envelope=env, integration_head="f" * 40,
                integration_branch="main-next", disposition="merge",
            )),
            ("85003", "ci\n" + render_hibernate_evidence(
                EVIDENCE_REQUIRED_CI_GREEN, envelope=env, workflow="test.yml", run="299"
            )),
            ("85004", "dogfood\n" + render_hibernate_evidence(
                EVIDENCE_DOGFOOD_DELEGATED, envelope=env,
                release_issue=RELEASE_ISSUE, acceptance="85431",
            )),
        )

    def _receipt_page(self):
        return (
            ("90001", "receipt\n[mozyo:workflow-event:gate=dogfood_receipt"
                      f":source_issue={ISSUE}:head={self.head}]"),
        )

    def _run_supervisor(self, pages, on_read=None, herdr_binary=None, extra_env=None, created_on=""):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            workspace_callback_supervisor as sup_mod,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            reconcile_live_source,
        )

        source = _FakeSource(pages, on_read=on_read, created_on=created_on)
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("MOZYO") and key not in ("TMUX", "TMUX_PANE")
        }
        env["MOZYO_BRIDGE_HOME"] = str(self.home)
        env["MOZYO_HERDR_BINARY"] = str(herdr_binary or self.herdr)
        for key, value in (extra_env or {}).items():
            env[key] = value
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sup_mod, "default_redmine_source", lambda ws, home=None: source), \
                mock.patch.object(
                    reconcile_live_source, "lane_worker_runtime",
                    lambda workspace_id, lane_id, role, agents_fn=None: "awaiting_input",
                ):
            supervisor = sup_mod.build_supervisor(holder="t2c-test", home=self.home)
            report = supervisor.run_once(mode=SUPERVISION_HIBERNATE)
        return report, source

    def _stateful_herdr_with_live_slot(self, *, lane=LANE, role="claude"):
        # A CANONICAL stateful herdr binary (the smoke ``fake_herdr_cli.py`` adapter over
        # ``FakeHerdr``) carrying ONE live managed slot for the lane — enough for the release rail
        # to pin it, read it (idle -> quiescent), and close its pane. Returns (binary, state_env).
        import json
        from tests.support.herdr_fake import FakeHerdr
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        # A rendered composer prompt with an EMPTY body ("> " with no user input) reads as a
        # readable, NON-pending composer (an empty string reads as *unreadable* and fails closed).
        fake = FakeHerdr(read_text="> ")
        ws_id = fake.seed_workspace(cwd=str(self.repo))
        fake.seed_agent(
            encode_assigned_name(self.workspace_id, role, lane),
            workspace_id=ws_id,
            status="idle",  # -> awaiting_input (quiescent, safe to release over)
        )
        state_path = self.dir / f"herdr-state-{lane}.json"
        state_path.write_text(json.dumps(fake.to_state()), encoding="utf-8")
        adapter = ROOT / "smoke" / "support" / "fake_herdr_cli.py"
        binary = self.dir / "stateful-herdr"
        binary.write_text(
            f'#!/bin/sh\nexec python3 "{adapter}" "$@"\n', encoding="utf-8"
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        return binary, {"MOZYO_FAKE_HERDR_STATE": str(state_path)}

    def test_a_fully_evidenced_early_lane_hibernates_exactly_once(self):
        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report, source = self._run_supervisor(pages)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertTrue(outcome.hibernate_ran, outcome)
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
        self.assertIn("actuated", kinds)
        row = next(
            record for record in LaneLifecycleStore(home=self.home).records()
            if record.lane_id == LANE
        )
        self.assertEqual(row.lane_disposition, "hibernated")
        # Bounded provider reads: the issue page once (memoised) + the one receipt page.
        self.assertEqual(sorted(set(source.reads)), [ISSUE, RELEASE_ISSUE])
        # Review j#86928 R6-F1: the fresh actuation durably persisted its redrive intent (the
        # basis a post-CAS crash redrive would reconstruct) before the CAS.
        from mozyo_bridge.core.state.hibernate_redrive_intent import HibernateRedriveIntentStore

        intent = HibernateRedriveIntentStore(home=self.home).get(self.workspace_id, LANE, 1)
        self.assertIsNotNone(intent)
        self.assertEqual((intent.issue_id, intent.basis), (ISSUE, "early_hibernate"))

    def test_a_provider_created_on_actuates_and_measures_time_to_drain(self):
        # Review j#87204 R3-F2 regression: a candidate whose journals carry a REAL provider
        # ``created_on`` must still fresh-actuate (the observability-only drain-ready timestamp must
        # NOT falsify candidate freshness identity) AND its time-to-drain must be measured from that
        # start. The pre-fix code failed this as ``stale_basis`` because ``drain_ready_at`` was part
        # of the candidate's dataclass equality and only the BUILD candidate was stamped.
        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report, _ = self._run_supervisor(pages, created_on="2026-07-24T00:00:00+00:00")
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        # It actuates exactly once (NOT stale_basis) despite the real created_on.
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        self.assertIn("actuated", [a["kind"] for a in outcome.hibernate_attempts])
        self.assertNotIn("stale_basis", [a["kind"] for a in outcome.hibernate_attempts])
        # And time-to-drain is measured: the START was the decision journal's created_on, the END
        # the supervisor clock at the terminal actuation -> a completed, non-null latency.
        self.assertEqual(report.hibernate_time_to_drain_status, "completed")
        self.assertIsNotNone(report.hibernate_time_to_drain_ms)
        self.assertGreaterEqual(report.hibernate_time_to_drain_ms, 0)
        # The redrive intent carried the same drain-ready start (crash-redrive keeps the original).
        from mozyo_bridge.core.state.hibernate_redrive_intent import HibernateRedriveIntentStore

        intent = HibernateRedriveIntentStore(home=self.home).get(self.workspace_id, LANE, 1)
        self.assertEqual(intent.drain_ready_at, "2026-07-24T00:00:00+00:00")

    def _run_supervisor_with_runtime_hook(self, pages, runtime_hook):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            workspace_callback_supervisor as sup_mod,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            reconcile_live_source,
        )

        source = _FakeSource(pages)
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("MOZYO") and key not in ("TMUX", "TMUX_PANE")
        }
        env["MOZYO_BRIDGE_HOME"] = str(self.home)
        env["MOZYO_HERDR_BINARY"] = str(self.herdr)

        def runtime(workspace_id, lane_id, role, agents_fn=None):
            runtime_hook()
            return "awaiting_input"

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sup_mod, "default_redmine_source", lambda ws, home=None: source), \
                mock.patch.object(reconcile_live_source, "lane_worker_runtime", runtime):
            supervisor = sup_mod.build_supervisor(holder="t2c-test", home=self.home)
            report = supervisor.run_once(mode=SUPERVISION_HIBERNATE)
        return report

    def _workspace_outcome(self, report):
        return next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )

    def _lane_row(self, lane=LANE):
        return next(
            record for record in LaneLifecycleStore(home=self.home).records()
            if record.lane_id == lane
        )

    def test_a_clean_unpushed_local_commit_actuates_nothing(self):
        # Review j#86757 R4-F1: origin still carries the branch at the evidence head, but the
        # worktree has a NEWER clean local commit — local HEAD != origin head binds no head,
        # so the lane is a typed non-candidate (unpushed work is never early-hibernated).
        _git(self.worktree, "commit", "--allow-empty", "-qm", "ahead")
        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report, _source = self._run_supervisor(pages)
        outcome = self._workspace_outcome(report)
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertNotIn(
            "actuated", [attempt["kind"] for attempt in outcome.hibernate_attempts]
        )
        self.assertEqual(self._lane_row().lane_disposition, "active")

    def test_a_behind_or_diverged_checkout_actuates_nothing(self):
        # The origin branch advanced past the worktree's local HEAD (behind/diverged) — the
        # equality is required in BOTH directions, never "an origin ref exists".
        _git(self.repo, "commit", "--allow-empty", "-qm", "advance")
        _git(self.repo, "push", "-q", "-f", "origin", f"main:{self.branch}")
        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report, _source = self._run_supervisor(pages)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(self._lane_row().lane_disposition, "active")

    def test_a_head_switch_after_the_fresh_observation_actuates_nothing(self):
        # Review j#86757 R4-F1 condition 2: the worktree HEAD moves AFTER the fresh topology
        # observation (the injected hook fires at the action-time obligations read — strictly
        # after the fresh observation, strictly before the commit) — the commit-point guard
        # re-reads the worktree and refuses with zero transition / zero close.
        def hook():
            _git(self.worktree, "commit", "--allow-empty", "-qm", "mid-flight")

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report = self._run_supervisor_with_runtime_hook(pages, hook)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(self._lane_row().lane_disposition, "active")

    def test_a_clean_rebranch_after_the_fresh_observation_actuates_nothing(self):
        # Same window, same commit: a clean rebranch (branch switch WITHOUT a head change)
        # must also be detected — the guard compares (head, branch), not the head alone.
        def hook():
            _git(self.worktree, "checkout", "-q", "-b", "hijack")

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report = self._run_supervisor_with_runtime_hook(pages, hook)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(self._lane_row().lane_disposition, "active")

    def _seed_redrive_intent(self, *, decision_journal="84999", lane=LANE, issue=ISSUE,
                             drain_ready_at=""):
        # Review j#86776 R5-F3: a hibernated crash-window row that came from a REAL fresh
        # actuation carries a durable redrive intent persisted pre-CAS. The store-level crash
        # fixtures below model that by seeding the matching intent (the early-hibernate durable
        # flags the fresh actuation would have derived) — without it the redrive is a typed
        # zero-close (redrive_intent_absent), which the intent-gating tests pin separately.
        from mozyo_bridge.core.state.hibernate_redrive_intent import (
            HibernateRedriveIntentStore,
            RedriveIntent,
        )

        HibernateRedriveIntentStore(home=self.home).record(
            RedriveIntent(
                workspace_id=self.workspace_id,
                lane_id=lane,
                lane_generation=1,
                issue_id=issue,
                decision_journal=decision_journal,
                basis="early_hibernate",
                action_id=f"hibernate:{lane}",
                assertion_flags={
                    "explicitly_parked": False,
                    "review_approved": True,
                    "staging_integrated": True,
                    "required_ci_green": True,
                    "dogfood_delegated": True,
                    "commits_pushed": True,
                    "callbacks_drained": True,
                    "no_review_pending": True,
                    "no_owner_approval_pending": True,
                    "no_integration_pending": True,
                    "no_pending_prompt": True,
                    "not_working": True,
                    "worktree_clean": True,
                    "boundary_recorded": False,
                },
                drain_ready_at=drain_ready_at,
            )
        )

    def _hibernate_row_with_release(self, target, *, seed_intent=True, drain_ready_at=""):
        # The crash window through the CANONICAL store rail: the fresh CAS landed, the release
        # generation opened (its pins already actuated), and the terminal outcome record is
        # where the prior run stopped — exactly what a post-CAS crash / partial close leaves.
        from mozyo_bridge.core.state.lane_lifecycle_model import ReleasePin
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )

        key = LaneLifecycleKey(repo_workspace_id=self.workspace_id, lane_id=LANE)
        decision = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="84999")
        cas = self.store.transition_disposition(
            key, expected_disposition="active", expected_revision=1,
            target="hibernated", decision=decision,
        )
        assert cas.applied, cas
        if seed_intent:
            self._seed_redrive_intent(drain_ready_at=drain_ready_at)
        # The pinned slot is ALREADY closed (absent from the live inventory): the crash hit
        # between the close and the outcome record — the redrive re-actuates nothing.
        pin = ReleasePin(
            role="codex",
            assigned_name=encode_assigned_name(self.workspace_id, "codex", LANE),
            locator="%9",
        )
        opened = self.store.request_release(
            key, expected_revision=cas.revision, action_id=f"hibernate:{LANE}", pins=[pin],
        )
        assert opened.applied, opened
        if target is not None:
            recorded = self.store.record_release_outcome(
                key, action_id=f"hibernate:{LANE}",
                expected_revision=opened.revision, target=target,
            )
            assert recorded.applied, recorded
        return key

    def test_a_partial_release_row_is_redriven_to_released_on_the_next_pass(self):
        # Review j#86757 R4-F2: a hibernated row with an unresolved release is enumerated as
        # typed redrive debt and driven through the public use case's already_hibernated path
        # — settled to released with NO manual sweep, NO second disposition CAS.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL)
        report, source = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redriven", kinds)
        row = self._lane_row()
        self.assertEqual(row.lane_disposition, "hibernated")
        self.assertEqual(row.process_release, "released")
        # The redrive resumes STORED authority: zero ticket-provider reads.
        self.assertEqual(source.reads, [])
        # Convergence: the NEXT pass enumerates no redrive debt and re-actuates nothing.
        report2, _source2 = self._run_supervisor({})
        outcome2 = self._workspace_outcome(report2)
        self.assertEqual(outcome2.hibernate_mutations, 0, outcome2.hibernate_attempts)
        self.assertEqual(
            [attempt["kind"] for attempt in outcome2.hibernate_attempts], []
        )
        row2 = self._lane_row()
        self.assertEqual(
            (row2.lane_disposition, row2.process_release, row2.revision),
            (row.lane_disposition, row.process_release, row.revision),
        )

    def test_a_redrive_inherits_the_original_drain_ready_start(self):
        # Review j#87224 R5-F1: a crash-redrive measures time-to-drain from the ORIGINAL drain-ready
        # start the fresh actuation persisted to its intent — not an empty start read before the
        # redrive request resolved (which stamped 'unavailable').
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL, drain_ready_at="2026-07-24T00:00:00+00:00")
        report, _ = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        redriven = next(a for a in outcome.hibernate_attempts if a["kind"] == "redriven")
        # It inherited the intent's ORIGINAL start -> a completed status + a real latency (not null).
        self.assertEqual(redriven["time_to_drain_status"], "completed")
        self.assertIsNotNone(redriven["time_to_drain_ms"])
        self.assertEqual(report.hibernate_time_to_drain_status, "completed")

    def test_a_released_row_is_terminal_and_never_redriven(self):
        # released is terminal: no redrive attempt, no store write, no provider read.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_RELEASED

        self._hibernate_row_with_release(RELEASE_RELEASED)
        before = self._lane_row()
        report, source = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(list(outcome.hibernate_attempts), [])
        self.assertEqual(source.reads, [])
        after = self._lane_row()
        self.assertEqual(after.revision, before.revision)

    def _add_second_early_lane(
        self, *, lane2="lane_t2c_2", issue2="601", branch2="feature/second"
    ):
        # A SECOND fully-evidenced early lane (its own real worktree + origin branch, declared
        # active), plus its evidence page + receipt. Returns (lane2, issue2, page2, receipt2).
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        wt2 = self.dir / f"wt-{lane2}"
        _git(self.repo, "worktree", "add", "-q", str(wt2), "-b", branch2)
        _git(self.repo, "push", "-q", "origin", branch2)
        token2 = derive_lane_workspace_token(str(wt2.resolve()))
        self.store.declare_active(
            LaneLifecycleKey(repo_workspace_id=self.workspace_id, lane_id=lane2),
            decision=DecisionPointer(source="redmine", issue_id=issue2, journal_id="84999"),
            issue_id=issue2,
            worktree_identity=token2,
        )
        record_lane_created(
            lane_workspace_token=token2,
            repo_workspace_id=self.workspace_id,
            issue_id=issue2,
            lane_label=lane2,
            worktree_path=str(wt2.resolve()),
            home=self.home,
        )
        head2 = subprocess.run(
            ["git", "-C", str(wt2), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        env2 = LaneEvidenceEnvelope(
            workspace=self.workspace_id, lane=lane2, lane_generation=1, head=head2
        )
        page2 = (
            (REQ_JOURNAL, "request\n" + render_workflow_event_marker(
                "review_request", target_head=head2
            )),
            ("85001", "review\n" + render_workflow_event_marker(
                "review_result", target_head=head2,
                review_request_journal=REQ_JOURNAL, conclusion="approved",
                evidence_workspace=self.workspace_id, evidence_lane=lane2,
                evidence_lane_generation=1,
            )),
            ("85002", "## Integration disposition\n" + render_integration_evidence(
                envelope=env2, integration_head="f" * 40,
                integration_branch="main-next", disposition="merge",
            )),
            ("85003", "ci\n" + render_hibernate_evidence(
                EVIDENCE_REQUIRED_CI_GREEN, envelope=env2, workflow="test.yml", run="299"
            )),
            ("85004", "dogfood\n" + render_hibernate_evidence(
                EVIDENCE_DOGFOOD_DELEGATED, envelope=env2,
                release_issue=RELEASE_ISSUE, acceptance="85431",
            )),
        )
        receipt2 = (
            ("90001", "receipt\n[mozyo:workflow-event:gate=dogfood_receipt"
                      f":source_issue={issue2}:head={head2}]"),
        )
        return lane2, issue2, page2, receipt2

    def test_a_redrive_consumes_the_pass_budget_before_any_fresh_mutation(self):
        # Review j#86757 R4-F2 condition 4: the redrive's process-close side effect IS the
        # pass's one mutation — a second, fully-evidenced fresh lane defers to the next pass.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL)
        lane2, issue2, page2, receipt2 = self._add_second_early_lane()
        report, _source = self._run_supervisor(
            {issue2: page2, RELEASE_ISSUE: receipt2}
        )
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redriven", kinds)
        self.assertIn("deferred", kinds)
        self.assertNotIn("actuated", kinds)
        self.assertEqual(self._lane_row(LANE).process_release, "released")
        self.assertEqual(self._lane_row(lane2).lane_disposition, "active")

    def test_a_later_review_request_flips_the_fresh_obligation_and_mutates_nothing(self):

        # Ruling j#86730 required tests 2+3: the transcription reads the SAME fresh memo — a
        # review_request newer than the approval means no fresh review_approved conjunct, so
        # the pass is zero-mutation even though the initial evidence looked complete.
        page = self._early_page() + (
            ("85009", "reopened\n" + render_workflow_event_marker(
                "review_request", target_head=self.head
            )),
        )
        pages = {ISSUE: page, RELEASE_ISSUE: self._receipt_page()}
        report, _source = self._run_supervisor(pages)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        row = next(
            record for record in LaneLifecycleStore(home=self.home).records()
            if record.lane_id == LANE
        )
        self.assertEqual(row.lane_disposition, "active")

    def test_a_review_request_arriving_AFTER_build_is_seen_by_the_fresh_read(self):
        # Review j#86734 R2-F1 (reproduced defect 1): the provider serves a clean page at
        # build, then a page with a LATER review_request — the actuation phase's own fresh
        # read (not the build cache) must see it: stale, zero mutation.
        clean = self._early_page()
        reopened = clean + (
            ("85009", "reopened\n" + render_workflow_event_marker(
                "review_request", target_head=self.head
            )),
        )

        def on_read(issue, nth):
            if issue == ISSUE and nth >= 1:
                return reopened
            return None

        pages = {ISSUE: clean, RELEASE_ISSUE: self._receipt_page()}
        report, source = self._run_supervisor(pages, on_read=on_read)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertGreaterEqual(source.reads.count(ISSUE), 2)  # build + fresh
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
        self.assertIn("stale_basis", kinds)
        row = next(
            record for record in LaneLifecycleStore(home=self.home).records()
            if record.lane_id == LANE
        )
        self.assertEqual(row.lane_disposition, "active")

    def test_a_lifecycle_revision_advancing_after_build_mutates_nothing(self):
        # Review j#86734 R2-F1 (reproduced defect 2): a legitimate lifecycle operation
        # advances the active revision between build and actuation — the fresh lifecycle
        # re-read (and the issue-lane CAS revision pin) must refuse it.
        def on_read(issue, nth):
            if issue == ISSUE and nth == 1:
                # The actuation-phase fresh page read fires BEFORE the fresh lifecycle read
                # within the same re-assembly: advance the revision now (re-declare with a
                # newer decision journal — the canonical revision-bumping write).
                from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
                from mozyo_bridge.core.state.lane_lifecycle_model import (
                    ProcessGenerationPin,
                )

                outcome = LaneDeclarationStore(home=self.home).backfill_active_binding(
                    LaneLifecycleKey(
                        repo_workspace_id=self.workspace_id, lane_id=LANE
                    ),
                    expected_revision=1,
                    issue_id=ISSUE,
                    worktree_identity=self.token,
                    declared_slots=[
                        ProcessGenerationPin(
                            role="implementation", provider="claude",
                            assigned_name="a1", locator="%1", runtime_revision="r1",
                        )
                    ],
                )
                assert outcome.applied and outcome.revision == 2, outcome
            return None

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report, _source = self._run_supervisor(pages, on_read=on_read)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        row = next(
            record for record in LaneLifecycleStore(home=self.home).records()
            if record.lane_id == LANE
        )
        self.assertEqual(row.lane_disposition, "active")

    def test_a_dependency_park_marker_alone_mutates_nothing(self):
        # Ruling j#86730 required test 4: the dependency producer proves only the park
        # declaration — review/owner/integration obligations stay False and nothing actuates.
        park_note = (
            "## parked\n"
            f"[mozyo:workflow-event:gate=park_declared:workspace={self.workspace_id}"
            f":lane={LANE}:lane_generation=1]\n"
            "- state: blocked\n"
            f"- durable_anchor: #{ISSUE} j#85005\n"
            "- target: coordinator\n"
            "- result: not-attempted\n"
            "- on not-attempted: waiting on upstream dependency\n"
            "- blocked_by: upstream\n"
            "- resume_condition: upstream lands\n"
            "- resume_owner: coordinator\n"
        )
        pages = {ISSUE: (("85005", park_note),)}
        report, _source = self._run_supervisor(pages)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)


    def test_the_actuated_use_case_is_bound_to_the_candidates_own_worktree(self):
        # Review j#86726 R1-F2: the ops repo_root (the public rail's worktree/lane-activity
        # authority) is the CANDIDATE's canonical worktree, never the shared workspace root.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            hibernate_supervisor_wiring as wiring,
        )

        roots = []
        real_ops = wiring.LiveSublaneHibernateOps

        def capturing(*args, **kwargs):
            ops = real_ops(*args, **kwargs)
            roots.append(Path(kwargs.get("repo_root") or args[0]))
            return ops

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        with mock.patch.object(wiring, "LiveSublaneHibernateOps", side_effect=capturing):
            report, _source = self._run_supervisor(pages)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        self.assertEqual(roots, [self.worktree.resolve()])
        self.assertNotIn(self.repo.resolve(), roots)

    def test_the_provider_read_budget_stops_further_reads(self):
        # Review j#86726 R1-F3: at the budget the provider is NOT touched — the page reads as
        # the typed unreadable and the pass stays zero-actuation for the affected issues.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            hibernate_supervisor_wiring as wiring,
        )

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        with mock.patch.object(wiring, "MAX_PROVIDER_READS_PER_PASS", 0):
            report, source = self._run_supervisor(pages)
        outcome = next(
            ws for ws in report.workspaces if ws.workspace_id == self.workspace_id
        )
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(source.reads, [])  # the provider was never touched


    # ------------------------------------------------------------------ R5-F1
    def test_an_origin_advance_after_the_fresh_observation_actuates_nothing(self):
        # Review j#86776 R5-F1: origin/<branch> is force-advanced AFTER the fresh topology
        # observation while the worktree's local HEAD is UNTOUCHED — the local (head, branch)
        # guard passes, so ONLY the commit-point origin re-read catches the drifted evidence
        # head. Zero transition / zero close. (Against the pre-R5 local-only guard this hibernates
        # — the mutation probe.)
        fired = []

        def hook():
            if fired:
                return
            fired.append(1)
            _git(self.repo, "commit", "--allow-empty", "-qm", "origin-advance")
            _git(self.repo, "push", "-q", "-f", "origin", f"main:{self.branch}")

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report = self._run_supervisor_with_runtime_hook(pages, hook)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertNotIn(
            "actuated", [attempt["kind"] for attempt in outcome.hibernate_attempts]
        )
        self.assertEqual(self._lane_row().lane_disposition, "active")

    def test_an_origin_ref_delete_after_the_fresh_observation_actuates_nothing(self):
        # Review j#86776 R5-F1: the origin ref is DELETED after the fresh observation (local HEAD
        # untouched) — the evidence head is no longer origin-reachable at the commit point.
        fired = []

        def hook():
            if fired:
                return
            fired.append(1)
            _git(self.repo, "push", "-q", "origin", f":{self.branch}")

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report = self._run_supervisor_with_runtime_hook(pages, hook)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(self._lane_row().lane_disposition, "active")

    # ------------------------------------------------------------------ R5-F3
    def test_a_redrive_with_no_durable_intent_is_a_typed_zero_close(self):
        # Review j#86776 R5-F3: a hibernated row with an unresolved release but NO durable intent
        # (a dependency-park / manual / pre-R5 crash row) is a typed zero-close — the redrive
        # never fabricates the basis the row did not record. Zero mutation, zero store write, zero
        # provider read; the release state is left exactly as it was.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL, seed_intent=False)
        before = self._lane_row()
        report, source = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redrive_blocked", kinds)
        reasons = [attempt["reason"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redrive_intent_absent", reasons)
        self.assertEqual(source.reads, [])
        after = self._lane_row()
        self.assertEqual(
            (after.lane_disposition, after.process_release, after.revision),
            (before.lane_disposition, before.process_release, before.revision),
        )

    def test_a_redrive_with_a_foreign_decision_intent_is_a_typed_zero_close(self):
        # Review j#86776 R5-F3: an intent that describes a DIFFERENT cycle (a foreign decision
        # journal) than the row does not authorise this redrive — a typed mismatch zero-close.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL, seed_intent=False)
        self._seed_redrive_intent(decision_journal="70000")  # row's is 84999
        before = self._lane_row()
        report, source = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        reasons = [attempt["reason"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redrive_intent_mismatch", reasons)
        self.assertEqual(source.reads, [])
        after = self._lane_row()
        self.assertEqual(after.process_release, before.process_release)
        self.assertEqual(after.revision, before.revision)

    def test_a_foreign_cycle_intent_start_is_not_trusted_for_latency(self):
        # Review j#87244 R7-F1: a foreign-cycle intent (SAME row identity, DIFFERENT decision journal)
        # is rejected as redrive_intent_mismatch. Its drain_ready_at must NOT be trusted as the
        # time-to-drain START either — the metric never derives a duration from another cycle's
        # authority. Before the fix the start resolver read the intent WITHOUT matches_row, so the
        # foreign start leaked into time_to_disposition_ms.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL, seed_intent=False)
        # A foreign decision (70000 != the row's 84999) that DOES carry a (foreign) start.
        self._seed_redrive_intent(
            decision_journal="70000", drain_ready_at="2026-07-24T00:00:00+00:00"
        )
        report, _ = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        blocked = next(
            a for a in outcome.hibernate_attempts if a["reason"] == "redrive_intent_mismatch"
        )
        # The foreign start is NOT adopted -> no derived duration (start unavailable).
        self.assertIsNone(blocked["time_to_drain_ms"])
        self.assertIsNone(blocked["time_to_disposition_ms"])

    # ------------------------------------------------------------------ R5-F5
    def test_an_unknown_release_state_is_a_typed_uncertain_block(self):
        # Review j#86776 R5-F5: a hibernated row whose process_release is a non-canonical token is
        # NEVER handed to the public rail (whose else-branch would falsely report it released). It
        # surfaces as a typed uncertain block across consecutive passes — zero mutation, zero
        # store write, and it does not starve a fresh candidate. The row is injected at the
        # lifecycle-read seam (the canonical store refuses to persist a non-canonical token).
        from types import SimpleNamespace
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            hibernate_supervisor_wiring as wiring,
        )

        bogus = SimpleNamespace(
            binding_kind="issue",
            lane_disposition="hibernated",
            repo_workspace_id=self.workspace_id,
            issue_id=ISSUE,
            lane_id=LANE,
            lane_generation=1,
            revision=3,
            process_release="weird_unknown_token",
            decision_journal="84999",
            worktree_identity=self.token,
        )

        def _rows(*_a, **_k):
            return [bogus]

        for _pass in range(2):
            with mock.patch.object(
                wiring, "load_lane_lifecycle_readonly", _rows
            ):
                report, source = self._run_supervisor({})
            outcome = self._workspace_outcome(report)
            self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
            kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
            self.assertIn("release_state_unknown", kinds)
            self.assertNotIn("redriven", kinds)
            self.assertNotIn("released", kinds)
            self.assertEqual(source.reads, [])  # never handed to the provider rail
            # Review j#87224 R5-F2 + clarification j#87226: an unknown-RELEASE outcome is OUTCOME
            # UNKNOWN, so its closed-enum time-to-drain status is ``uncertain`` (no trusted terminal
            # end -> null latencies) — never ``pending`` and never the empty string outside the enum.
            unknown = next(
                a for a in outcome.hibernate_attempts if a["kind"] == "release_state_unknown"
            )
            self.assertIn(
                unknown["time_to_drain_status"],
                {"completed", "pending", "uncertain", "unavailable"},
            )
            self.assertEqual(unknown["time_to_drain_status"], "uncertain")  # outcome unknown
            self.assertIsNone(unknown["time_to_drain_ms"])
            self.assertIsNone(unknown["time_to_disposition_ms"])
            # Review j#87236 R6-F2: the pass-level roll-up uses the SAME outcome-unknown
            # classification — the unknown release counts as UNCERTAIN, never blocked.
            self.assertEqual(report.hibernate_uncertain, 1)
            self.assertEqual(report.hibernate_blocked, 0)
            self.assertEqual(report.hibernate_time_to_drain_status, "uncertain")

    # ------------------------------------------------------------------ R5-F4
    def _run_leg_directly(self, *, renew, pages, runtime="awaiting_input", inventory=None):
        # Build the production leg via build_hibernate_leg_fn and call it with a CALLER-controlled
        # ``renew`` — the seam the leg-boundary composition test needs (the full supervisor hides
        # the lease behind its own store).
        from types import SimpleNamespace
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            build_hibernate_leg_fn,
        )

        source = _FakeSource(pages)

        class _Outbox:
            def read(self, states=None):
                return []

        inv = inventory if inventory is not None else ([], True)
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("MOZYO") and key not in ("TMUX", "TMUX_PANE")
        }
        env["MOZYO_BRIDGE_HOME"] = str(self.home)
        env["MOZYO_HERDR_BINARY"] = str(self.herdr)
        leg = build_hibernate_leg_fn(
            home=self.home,
            outbox=_Outbox(),
            source_fn=lambda ws: source,
            runtime_fn=lambda workspace_id, lane_id: runtime,
            inventory_fn=lambda: inv,
        )
        ws = SimpleNamespace(
            canonical_path=str(self.repo), workspace_id=self.workspace_id
        )
        with mock.patch.dict(os.environ, env, clear=True):
            result = leg(ws, renew, {"reads": 0})
        return result, source

    def test_a_stopped_redrive_skips_the_fresh_pass_entirely(self):
        # Review j#86776 R5-F4 (leg-boundary composition): a redrivable row loses its lease at the
        # redrive's pre-close check (renew False on the FIRST call), STOPPING the pass. A
        # fully-evidenced fresh candidate is NOT actuated even though renew RECOVERS to True — the
        # fresh pass is never run. Against the pre-R5 wiring (which ignored redrive_result.stopped)
        # the fresh candidate hibernates once renew recovers — the mutation probe.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL)  # LANE hibernated + intent (redrivable)
        lane2, issue2, page2, receipt2 = self._add_second_early_lane()

        renew_calls = {"n": 0}

        def renew():
            renew_calls["n"] += 1
            return renew_calls["n"] != 1  # False on the redrive's lease check, True afterwards

        result, _source = self._run_leg_directly(
            renew=renew, pages={issue2: page2, RELEASE_ISSUE: receipt2}
        )
        self.assertEqual(result.mutations, 0, result.attempts)
        kinds = {(a.lane, a.kind) for a in result.attempts}
        self.assertIn((lane2, "lease_lost"), kinds)
        self.assertNotIn((lane2, "actuated"), kinds)
        self.assertEqual(self._lane_row(lane2).lane_disposition, "active")
        # The fresh candidate's use case was never even built (renew recovered but the pass was
        # skipped): lane2's own worktree stays untouched.
        self.assertEqual(self._lane_row(LANE).lane_disposition, "hibernated")

    # ------------------------------------------------------------------ R5-F2
    def _hibernate_row_not_requested(self, *, seed_intent=True, drain_ready_at=""):
        # The post-CAS crash window: the CAS landed (active -> hibernated) but the release was
        # NEVER opened (process_release stays not_requested). A real fresh actuation persisted its
        # intent pre-CAS, so this seeds the matching intent too.
        key = LaneLifecycleKey(repo_workspace_id=self.workspace_id, lane_id=LANE)
        decision = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="84999")
        cas = self.store.transition_disposition(
            key, expected_disposition="active", expected_revision=1,
            target="hibernated", decision=decision,
        )
        assert cas.applied, cas
        if seed_intent:
            self._seed_redrive_intent(drain_ready_at=drain_ready_at)
        return key

    def test_a_not_requested_crash_with_a_live_slot_is_redriven_to_released(self):
        # Review j#86776 R5-F2 (fault injection): the CAS landed but the release was never opened
        # (not_requested) and a managed slot is STILL LIVE — a crash right after the CAS. The next
        # pass enumerates it as debt (live slot present), drives the public already_hibernated
        # path, closes the residual slot, and settles to released — no manual sweep, no second
        # disposition CAS. Against the pre-R5 wiring (not_requested unconditionally terminal) the
        # live slot stranded forever — the mutation probe.
        binary, extra_env = self._stateful_herdr_with_live_slot()
        self._hibernate_row_not_requested()
        before = self._lane_row()
        self.assertEqual(before.process_release, "not_requested")
        report, source = self._run_supervisor({}, herdr_binary=binary, extra_env=extra_env)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 1, outcome.hibernate_attempts)
        self.assertIn("redriven", [a["kind"] for a in outcome.hibernate_attempts])
        row = self._lane_row()
        self.assertEqual(row.lane_disposition, "hibernated")  # NO second disposition CAS
        self.assertEqual(row.process_release, "released")
        self.assertEqual(source.reads, [])  # stored authority: zero provider reads
        # Convergence: released is terminal -> the next pass enumerates no debt, mutates nothing.
        report2, _ = self._run_supervisor({}, herdr_binary=binary, extra_env=extra_env)
        outcome2 = self._workspace_outcome(report2)
        self.assertEqual(outcome2.hibernate_mutations, 0, outcome2.hibernate_attempts)

    def test_a_not_requested_crash_with_no_live_slot_is_terminal(self):
        # Review j#86776 R5-F2: the same not_requested crash but the processes are ALREADY gone
        # (a confirmed-empty inventory) — terminal: no redrive, no store write, no false release
        # re-opened every pass.
        self._hibernate_row_not_requested()
        before = self._lane_row()
        report, source = self._run_supervisor({})  # default herdr binary -> empty inventory
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        self.assertEqual(list(outcome.hibernate_attempts), [])
        self.assertEqual(source.reads, [])
        after = self._lane_row()
        self.assertEqual(
            (after.process_release, after.revision),
            (before.process_release, before.revision),
        )


    # ------------------------------------------------------------------ R6-F1
    def test_an_intent_write_failure_refuses_the_cas(self):
        # Review j#86928 R6-F1 (fault injection): the pre-CAS redrive-intent write fails, so the
        # irreversible disposition CAS must be REFUSED — a hibernate whose intent could not be
        # persisted would strand the live process forever (a post-CAS crash could never be
        # redriven). Zero transition / zero close; the lane stays active. Against the pre-R6
        # best-effort swallow the lane hibernates despite the failed write — the mutation probe.
        from mozyo_bridge.core.state.hibernate_redrive_intent import (
            HibernateRedriveIntentError,
            HibernateRedriveIntentStore,
        )

        def boom(self, intent, *args, **kwargs):
            raise HibernateRedriveIntentError("injected intent write failure")

        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        with mock.patch.object(HibernateRedriveIntentStore, "record", boom):
            report, _source = self._run_supervisor(pages)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        kinds = [attempt["kind"] for attempt in outcome.hibernate_attempts]
        reasons = [attempt["reason"] for attempt in outcome.hibernate_attempts]
        self.assertNotIn("actuated", kinds)
        self.assertIn("intent_persist_failed", reasons)
        self.assertEqual(self._lane_row().lane_disposition, "active")

    # ------------------------------------------------------------------ R6-F2
    def test_a_corrupt_intent_blob_is_a_typed_zero_close(self):
        # Review j#86928 R6-F2: a hibernated row with a redrivable release but a CORRUPT intent
        # blob (every gate the JSON string "false") is a typed zero-close — the reader refuses to
        # truthiness-coerce it into a satisfied basis, so the redrive never runs. Against the
        # pre-R6 coercion the "false" strings decode to True and the redrive releases — the probe.
        import json
        import sqlite3
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL
        from mozyo_bridge.core.state.hibernate_redrive_intent import (
            REQUIRED_ASSERTION_FLAG_KEYS,
            hibernate_redrive_intent_path,
        )

        self._hibernate_row_with_release(RELEASE_PARTIAL)  # seeds a VALID intent
        conn = sqlite3.connect(hibernate_redrive_intent_path(self.home))
        try:
            conn.execute(
                "UPDATE hibernate_redrive_intent SET assertion_flags=?",
                (json.dumps({k: "false" for k in REQUIRED_ASSERTION_FLAG_KEYS}),),
            )
            conn.commit()
        finally:
            conn.close()
        before = self._lane_row()
        report, source = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        reasons = [attempt["reason"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redrive_intent_unreadable", reasons)
        self.assertEqual(source.reads, [])
        after = self._lane_row()
        self.assertEqual(
            (after.process_release, after.revision),
            (before.process_release, before.revision),
        )


    # ------------------------------------------------------------------ R7-F1
    def _make_intent_db_a_directory(self):
        # The reviewer's own raw-error probe: the intent DB path is a DIRECTORY, so the store's
        # own sqlite3.connect raises a raw OperationalError ("unable to open" on write / "disk I/O
        # error" on read) that the store boundary must normalize to HibernateRedriveIntentError.
        from mozyo_bridge.core.state.hibernate_redrive_intent import (
            hibernate_redrive_intent_path,
        )

        hibernate_redrive_intent_path(self.home).mkdir(parents=True, exist_ok=True)

    def test_a_raw_sqlite_write_failure_normalizes_to_intent_persist_failed(self):
        # Review j#86975 R7-F1: a REAL SQLite open failure during the fresh actuation's pre-CAS
        # intent write is normalized to the typed intent_persist_failed and refuses the CAS —
        # NOT a raw exception that aborts the whole hibernate leg (skipped_reason=hibernate_leg_
        # error). Against the pre-R7 un-normalized store the raw error escapes and no typed
        # attempt is produced — the probe.
        self._make_intent_db_a_directory()
        pages = {ISSUE: self._early_page(), RELEASE_ISSUE: self._receipt_page()}
        report, _source = self._run_supervisor(pages)
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        reasons = [attempt["reason"] for attempt in outcome.hibernate_attempts]
        self.assertIn("intent_persist_failed", reasons)
        self.assertNotIn(
            "actuated", [attempt["kind"] for attempt in outcome.hibernate_attempts]
        )
        self.assertEqual(self._lane_row().lane_disposition, "active")

    def test_a_raw_sqlite_read_failure_normalizes_to_redrive_intent_unreadable(self):
        # Review j#86975 R7-F1: a REAL SQLite read failure during a redrive's intent read is
        # normalized to the typed redrive_intent_unreadable zero-close — NOT a raw exception.
        from mozyo_bridge.core.state.lane_lifecycle_model import RELEASE_PARTIAL

        self._hibernate_row_with_release(RELEASE_PARTIAL, seed_intent=False)
        self._make_intent_db_a_directory()
        before = self._lane_row()
        report, source = self._run_supervisor({})
        outcome = self._workspace_outcome(report)
        self.assertEqual(outcome.hibernate_mutations, 0, outcome.hibernate_attempts)
        reasons = [attempt["reason"] for attempt in outcome.hibernate_attempts]
        self.assertIn("redrive_intent_unreadable", reasons)
        self.assertEqual(source.reads, [])
        after = self._lane_row()
        self.assertEqual(
            (after.process_release, after.revision),
            (before.process_release, before.revision),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
