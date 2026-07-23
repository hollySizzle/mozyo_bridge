"""Live-wiring seams for the supervisor hibernate leg (Redmine #14219 T2c step 2b).

Pins the ruling's wiring conditions (j#86718): Fork B discovery is never inference and refuses
cross-generation evidence; a roster lane with no evidence is a typed non-candidate; the Fork C
projection obligations fail closed when nothing explicit is projected; the subprocess
observations (config blob pointer, lane push) read real git state hermetically.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
    ObligationSources,
    committed_config_policy_pointer,
    enumerate_requests,
    observe_lane_push,
    observe_obligations,
    read_dogfood_receipts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
    BASIS_DEPENDENCY_PARK,
    BASIS_EARLY_HIBERNATE,
    BoundField,
    HibernateCandidate,
    LifecycleAnchor,
    SelectedLane,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)

WS, LANE, GEN = "wsW", "lane_w_1", 2
HEAD = "b" * 40


@dataclass
class _Row:
    repo_workspace_id: str = WS
    lane_id: str = LANE
    issue_id: str = "500"
    lane_disposition: str = "active"
    binding_kind: str = "issue"
    lane_generation: int = GEN
    revision: int = 4


def _entry(journal, notes):
    return RedmineJournalEntry(issue_id="500", journal_id=str(journal), notes=notes)


def _park_marker(generation=GEN, lane=LANE):
    return (
        f"[mozyo:workflow-event:gate=park_declared:workspace={WS}:lane={lane}"
        f":lane_generation={generation}]"
    )


def _pages(**by_issue):
    def entries_fn(issue):
        return by_issue.get(str(issue))

    return entries_fn


class EnumerationTest(unittest.TestCase):
    def test_active_issue_lanes_enumerate_the_early_basis(self):
        requests = enumerate_requests([_Row()], WS, _pages(**{"500": ()}))
        self.assertEqual([r.basis for r in requests], [BASIS_EARLY_HIBERNATE])
        self.assertEqual(requests[0].selected.lane_id, LANE)

    def test_non_active_foreign_and_gateway_rows_enumerate_nothing(self):
        rows = [
            _Row(lane_disposition="hibernated"),
            _Row(repo_workspace_id="other"),
            _Row(binding_kind="project_gateway", issue_id=""),
            _Row(issue_id=""),
        ]
        self.assertEqual(enumerate_requests(rows, WS, _pages()), ())

    def test_park_discovery_needs_the_exact_envelope(self):
        # Ruling pin: discovery is not inference — only a strictly-parsed park evidence whose
        # envelope equals the row's EXACT (workspace, lane, generation) enumerates the basis.
        pages = _pages(**{"500": (_entry(1, _park_marker()),)})
        bases = [r.basis for r in enumerate_requests([_Row()], WS, pages)]
        self.assertEqual(bases, [BASIS_EARLY_HIBERNATE, BASIS_DEPENDENCY_PARK])

    def test_cross_generation_park_evidence_enumerates_nothing(self):
        # Ruling pin: cross-generation projection refusal — a stale generation's park marker
        # never enumerates the current generation.
        pages = _pages(**{"500": (_entry(1, _park_marker(generation=GEN - 1)),)})
        bases = [r.basis for r in enumerate_requests([_Row()], WS, pages)]
        self.assertEqual(bases, [BASIS_EARLY_HIBERNATE])

    def test_foreign_lane_park_evidence_enumerates_nothing(self):
        pages = _pages(**{"500": (_entry(1, _park_marker(lane="lane_other")),)})
        bases = [r.basis for r in enumerate_requests([_Row()], WS, pages)]
        self.assertEqual(bases, [BASIS_EARLY_HIBERNATE])

    def test_prose_and_states_never_synthesize_a_park(self):
        # Ruling pin: idle/open/releasable words are prose, not a canonical park evidence.
        pages = _pages(
            **{"500": (_entry(1, "state: blocked, releasable, parked — but no marker"),)}
        )
        bases = [r.basis for r in enumerate_requests([_Row()], WS, pages)]
        self.assertEqual(bases, [BASIS_EARLY_HIBERNATE])

    def test_an_unreadable_page_enumerates_only_the_early_basis(self):
        bases = [r.basis for r in enumerate_requests([_Row()], WS, _pages())]
        self.assertEqual(bases, [BASIS_EARLY_HIBERNATE])


class MissingEvidenceNonCandidateTest(unittest.TestCase):
    def test_an_early_roster_lane_with_no_evidence_is_a_typed_non_candidate(self):
        # Ruling pin: the early enumeration is an evaluation population — with EMPTY journals
        # the assembler yields a typed non-candidate, never an actuation.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_candidate_assembler import (  # noqa: E501
            AssemblyRequest,
            HibernateCandidateAssembler,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
            PushObservation,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
            HibernateNonCandidate,
        )

        row = _Row()
        selected = SelectedLane(
            issue_id="500", repo_workspace_id=WS, lane_id=LANE,
            lane_generation=GEN, revision=4,
        )
        assembler = HibernateCandidateAssembler(
            records_fn=lambda: [row],
            journals_fn=lambda issue: [],
            push_fn=lambda sel: PushObservation(
                workspace=WS, lane=LANE, lane_generation=GEN, head=HEAD, reachable=True
            ),
            obligations_fn=lambda candidate: None,
        )
        verdict = assembler.assemble(
            AssemblyRequest(selected=selected, basis=BASIS_EARLY_HIBERNATE)
        ).verdict
        self.assertIsInstance(verdict, HibernateNonCandidate)


class ObligationObserverTest(unittest.TestCase):
    def _candidate(self):
        anchor = LifecycleAnchor(
            issue_id="500", repo_workspace_id=WS, lane_id=LANE,
            lane_generation=GEN, revision=4,
        )
        return HibernateCandidate(
            issue_id="500", anchor=anchor, basis=BASIS_EARLY_HIBERNATE,
            head=BoundField(value=HEAD, provenance="git_remote"), conjuncts=(),
        )

    def _sources(self, **overrides):
        base = dict(
            outbox_pending_fn=lambda workspace: 0,
            runtime_fn=lambda workspace, lane: "idle",
            worktree_clean_fn=lambda candidate: True,
            projection_fn=lambda candidate: {},
        )
        base.update(overrides)
        return ObligationSources(**base)

    def test_live_flags_come_from_the_local_authorities(self):
        flags = observe_obligations(self._candidate(), self._sources())
        self.assertTrue(flags.callbacks_drained)
        self.assertTrue(flags.no_pending_prompt)
        self.assertTrue(flags.not_working)
        self.assertTrue(flags.worktree_clean)

    def test_projection_obligations_fail_closed_without_an_explicit_projection(self):
        # Ruling pin: glance unreadable / ambiguous / unprojected -> False. The default
        # projection supplies nothing, so all four projection flags are False.
        flags = observe_obligations(self._candidate(), self._sources())
        self.assertFalse(flags.no_review_pending)
        self.assertFalse(flags.no_owner_approval_pending)
        self.assertFalse(flags.no_integration_pending)
        self.assertFalse(flags.boundary_recorded)

    def test_an_explicit_projection_key_is_respected_verbatim(self):
        flags = observe_obligations(
            self._candidate(),
            self._sources(projection_fn=lambda candidate: {"no_review_pending": True}),
        )
        self.assertTrue(flags.no_review_pending)
        self.assertFalse(flags.no_integration_pending)

    def test_unobservable_inputs_leave_every_flag_false(self):
        flags = observe_obligations(
            self._candidate(),
            self._sources(
                outbox_pending_fn=lambda workspace: None,
                runtime_fn=lambda workspace, lane: "",
                worktree_clean_fn=lambda candidate: None,
            ),
        )
        self.assertFalse(flags.callbacks_drained)
        self.assertFalse(flags.no_pending_prompt)
        self.assertFalse(flags.not_working)
        self.assertFalse(flags.worktree_clean)

    def test_a_working_runtime_is_not_idle(self):
        flags = observe_obligations(
            self._candidate(), self._sources(runtime_fn=lambda workspace, lane: "working")
        )
        self.assertFalse(flags.not_working)
        self.assertFalse(flags.no_pending_prompt)


class DogfoodReceiptReaderTest(unittest.TestCase):
    DOGFOOD = (
        f"[mozyo:workflow-event:gate=dogfood_delegated:workspace={WS}:lane={LANE}"
        f":lane_generation={GEN}:head={HEAD}:release_issue=900:acceptance=glance]"
    )

    def test_a_matching_receipt_is_read_from_the_release_issue(self):
        receipt = f"[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head={HEAD}]"
        pages = _pages(**{"500": (_entry(1, self.DOGFOOD),), "900": (_entry(2, receipt),)})
        receipts = read_dogfood_receipts("500", pages)
        self.assertEqual(receipts["900"].source_issue, "500")
        self.assertEqual(receipts["900"].head, HEAD)

    def test_conflicting_receipt_claims_prove_nothing(self):
        one = f"[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head={HEAD}]"
        two = f"[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head={'c' * 40}]"
        pages = _pages(**{"500": (_entry(1, self.DOGFOOD),), "900": (_entry(2, one + two),)})
        self.assertEqual(read_dogfood_receipts("500", pages), {})

    def test_an_unreadable_release_issue_yields_no_receipt(self):
        pages = _pages(**{"500": (_entry(1, self.DOGFOOD),)})
        self.assertEqual(read_dogfood_receipts("500", pages), {})

    def test_a_malformed_head_is_not_a_receipt(self):
        receipt = "[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head=short]"
        pages = _pages(**{"500": (_entry(1, self.DOGFOOD),), "900": (_entry(2, receipt),)})
        self.assertEqual(read_dogfood_receipts("500", pages), {})


class GitObservationTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.repo = self.dir / "repo"
        self.origin = self.dir / "origin.git"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        (self.repo / ".mozyo-bridge").mkdir()
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text("version: 2\n")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "c1"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", str(self.origin)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-q", "origin", "main:laneX"], check=True,
            env=env,
        )
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _selected(self, lane="laneX"):
        return SelectedLane(
            issue_id="500", repo_workspace_id=WS, lane_id=lane,
            lane_generation=GEN, revision=4,
        )

    def test_the_committed_config_blob_anchors_the_policy(self):
        pointer = committed_config_policy_pointer(self.repo)
        blob = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD:.mozyo-bridge/config.yaml"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(pointer, f"git:.mozyo-bridge/config.yaml@{blob}")

    def test_an_uncommitted_config_binds_nothing(self):
        self.assertEqual(committed_config_policy_pointer(self.dir / "nowhere"), "")

    def test_the_remote_lane_head_is_observed(self):
        observation = observe_lane_push(self.repo, self._selected())
        self.assertIsNotNone(observation)
        self.assertEqual(observation.head, self.head)
        self.assertTrue(observation.reachable)
        self.assertEqual(
            (observation.workspace, observation.lane, observation.lane_generation),
            (WS, "laneX", GEN),
        )

    def test_an_absent_remote_ref_observes_nothing(self):
        self.assertIsNone(observe_lane_push(self.repo, self._selected(lane="ghost")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
