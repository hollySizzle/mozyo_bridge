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

ISSUE = "600"
RELEASE_ISSUE = "900"
LANE = "lane_t2c_1"
REQ_JOURNAL = "85000"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
}


class _FakeSource:
    """A per-issue journal-page source (the ONE external Redmine boundary)."""

    def __init__(self, pages):
        self.pages = pages
        self.reads = []

    def read_entries(self, issue_id):
        self.reads.append(str(issue_id))
        return [
            RedmineJournalEntry(issue_id=str(issue_id), journal_id=str(jid), notes=notes)
            for jid, notes in self.pages.get(str(issue_id), ())
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
        _git(self.repo, "push", "-q", "origin", f"main:{LANE}")
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # The lane's canonical WORKTREE: a real `git worktree` of the WORKSPACE repo, so the
        # live ops' workspace-segment derivation (git topology) resolves back to the registered
        # workspace — exactly the production lane shape.
        self.worktree = self.dir / "wt-lane"
        _git(self.repo, "worktree", "add", "-q", str(self.worktree), "-b", LANE)
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

    def _run_supervisor(self, pages):
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
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sup_mod, "default_redmine_source", lambda ws, home=None: source), \
                mock.patch.object(
                    reconcile_live_source, "lane_worker_runtime",
                    lambda workspace_id, lane_id, role, agents_fn=None: "awaiting_input",
                ):
            supervisor = sup_mod.build_supervisor(holder="t2c-test", home=self.home)
            report = supervisor.run_once(mode=SUPERVISION_HIBERNATE)
        return report, source

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
