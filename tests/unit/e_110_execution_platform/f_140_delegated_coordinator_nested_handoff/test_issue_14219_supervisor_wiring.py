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
            runtime_fn=lambda workspace, lane: "awaiting_input",
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

    def test_unsettled_runtimes_leave_the_flags_false(self):
        # The canonical normalized vocabulary (checkpoint j#86726 R1-F4): settled is
        # awaiting_input / turn_ended; working / blocked / unknown — and the RAW herdr spellings
        # idle / done, which never reach this layer — leave the flags False.
        for runtime in ("working", "blocked", "unknown", "idle", "done"):
            with self.subTest(runtime=runtime):
                flags = observe_obligations(
                    self._candidate(),
                    self._sources(runtime_fn=lambda workspace, lane, r=runtime: r),
                )
                self.assertFalse(flags.not_working)
                self.assertFalse(flags.no_pending_prompt)

    def test_turn_ended_is_settled(self):
        flags = observe_obligations(
            self._candidate(), self._sources(runtime_fn=lambda workspace, lane: "turn_ended")
        )
        self.assertTrue(flags.not_working)
        self.assertTrue(flags.no_pending_prompt)

    def test_production_shape_observed_agents_drive_the_flags(self):
        # Production-shape pin (checkpoint j#86726 R1-F4): the runtime travels through
        # lane_worker_runtime over ObservedAgent-shaped rows — herdr raw idle/done arrive as
        # the NORMALIZED awaiting_input/turn_ended, never raw.
        from types import SimpleNamespace

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_live_source import (  # noqa: E501
            lane_worker_runtime,
        )

        def agent(state):
            return SimpleNamespace(
                workspace_id=WS, lane_id=LANE, role="claude", runtime_state=state
            )

        for state, settled in (
            ("awaiting_input", True),
            ("turn_ended", True),
            ("working", False),
            ("blocked", False),
            ("unknown", False),
        ):
            with self.subTest(state=state):
                runtime = lane_worker_runtime(
                    WS, LANE, "implementation_worker", agents_fn=lambda s=state: [agent(s)]
                )
                self.assertEqual(runtime, state)
                flags = observe_obligations(
                    self._candidate(),
                    self._sources(runtime_fn=lambda workspace, lane, r=runtime: r),
                )
                self.assertEqual(flags.not_working, settled)


class DogfoodReceiptReaderTest(unittest.TestCase):
    DOGFOOD = (
        f"[mozyo:workflow-event:gate=dogfood_delegated:workspace={WS}:lane={LANE}"
        f":lane_generation={GEN}:head={HEAD}:release_issue=900:acceptance=glance]"
    )

    def _selected(self):
        return SelectedLane(
            issue_id="500", repo_workspace_id=WS, lane_id=LANE,
            lane_generation=GEN, revision=4,
        )

    def _journals(self, *notes):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
            EvidenceJournal,
        )

        return [
            EvidenceJournal(journal_id=str(index + 1), notes=note)
            for index, note in enumerate(notes)
        ]

    def _counting(self, **by_issue):
        calls = []

        def entries_fn(issue):
            calls.append(str(issue))
            return by_issue.get(str(issue))

        return entries_fn, calls

    def test_a_matching_receipt_is_read_from_the_release_issue(self):
        receipt = f"[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head={HEAD}]"
        entries_fn, calls = self._counting(**{"900": (_entry(2, receipt),)})
        receipts = read_dogfood_receipts(self._journals(self.DOGFOOD), self._selected(), entries_fn)
        self.assertEqual(receipts["900"].source_issue, "500")
        self.assertEqual(receipts["900"].head, HEAD)
        self.assertEqual(calls, ["900"])  # exactly ONE release read

    def test_conflicting_receipt_claims_prove_nothing(self):
        one = f"[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head={HEAD}]"
        two = f"[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head={'c' * 40}]"
        entries_fn, _calls = self._counting(**{"900": (_entry(2, one + two),)})
        self.assertEqual(
            read_dogfood_receipts(self._journals(self.DOGFOOD), self._selected(), entries_fn), {}
        )

    def test_an_unreadable_release_issue_yields_no_receipt(self):
        entries_fn, _calls = self._counting()
        self.assertEqual(
            read_dogfood_receipts(self._journals(self.DOGFOOD), self._selected(), entries_fn), {}
        )

    def test_a_malformed_head_is_not_a_receipt(self):
        receipt = "[mozyo:workflow-event:gate=dogfood_receipt:source_issue=500:head=short]"
        entries_fn, _calls = self._counting(**{"900": (_entry(2, receipt),)})
        self.assertEqual(
            read_dogfood_receipts(self._journals(self.DOGFOOD), self._selected(), entries_fn), {}
        )

    def test_a_foreign_lane_delegation_triggers_zero_reads(self):
        # Review j#86726 R1-F3: the read set derives from the current STRICT delegation bound to
        # the enumerated lane. A delegation for another lane reads nothing at all.
        foreign = self.DOGFOOD.replace(f"lane={LANE}", "lane=lane_other")
        entries_fn, calls = self._counting(**{"900": ()})
        self.assertEqual(
            read_dogfood_receipts(self._journals(foreign), self._selected(), entries_fn), {}
        )
        self.assertEqual(calls, [])

    def test_a_superseded_delegation_reads_only_the_current_release_issue(self):
        # The producer's own latest-declaration supersession picks the read set: the OLD
        # delegation's release issue is never read.
        newer = self.DOGFOOD.replace("release_issue=900", "release_issue=901")
        entries_fn, calls = self._counting(**{"901": ()})
        read_dogfood_receipts(
            self._journals(self.DOGFOOD, newer), self._selected(), entries_fn
        )
        self.assertEqual(calls, ["901"])

    def test_a_malformed_current_delegation_triggers_zero_reads(self):
        malformed = (
            f"[mozyo:workflow-event:gate=dogfood_delegated:workspace={WS}:lane={LANE}"
            f":lane_generation={GEN}:head={HEAD}:acceptance=glance]"  # release_issue missing
        )
        entries_fn, calls = self._counting()
        self.assertEqual(
            read_dogfood_receipts(self._journals(malformed), self._selected(), entries_fn), {}
        )
        self.assertEqual(calls, [])



class UnresolvedCallbackDebtTest(unittest.TestCase):
    """Review j#86734 R2-F4: every unresolved callback state blocks the drain obligation."""

    class _FakeOutbox:
        def __init__(self, rows=None, raises=False):
            self.rows = rows or []
            self.raises = raises
            self.asked_states = None

        def read(self, states=None):
            self.asked_states = tuple(states or ())
            if self.raises:
                raise OSError("unreadable")
            return [row for row in self.rows if row[0] in (states or ())]

    def _row(self, state, workspace="wsW"):
        from types import SimpleNamespace

        return (state, SimpleNamespace(key=SimpleNamespace(workspace_id=workspace)))

    def _debt(self, outbox):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            unresolved_callback_debt,
        )

        class _Adapter:
            def __init__(self, inner):
                self.inner = inner

            def read(self, states=None):
                return [row[1] for row in self.inner.read(states=states)]

        if outbox.raises:
            return unresolved_callback_debt(outbox, "wsW")
        return unresolved_callback_debt(_Adapter(outbox), "wsW")

    def test_every_unresolved_state_counts_as_debt(self):
        for state in ("pending", "inflight", "uncertain", "dead_letter"):
            with self.subTest(state=state):
                outbox = self._FakeOutbox(rows=[self._row(state)])
                self.assertEqual(self._debt(outbox), 1)

    def test_a_delivered_only_partition_is_drained(self):
        outbox = self._FakeOutbox(rows=[self._row("delivered")])
        self.assertEqual(self._debt(outbox), 0)

    def test_an_unreadable_outbox_is_not_drained(self):
        outbox = self._FakeOutbox(raises=True)
        self.assertIsNone(self._debt(outbox))

    def test_the_read_asks_for_exactly_the_unresolved_states(self):
        outbox = self._FakeOutbox()
        self._debt(outbox)
        self.assertEqual(
            sorted(outbox.asked_states),
            ["dead_letter", "inflight", "pending", "uncertain"],
        )

    def test_a_foreign_workspaces_debt_does_not_count(self):
        outbox = self._FakeOutbox(rows=[self._row("uncertain", workspace="other")])
        self.assertEqual(self._debt(outbox), 0)


class WorktreeResolverTest(unittest.TestCase):
    """resolve_candidate_worktree (reviews j#86734 R2-F5 / j#86739 R3-F2): FRESH Git worktree
    topology joined by the lifecycle row's ``worktree_identity`` token ALONE — the lane id is
    never assumed to be the branch, and the display-only lane metadata store is never
    consulted."""

    def setUp(self):
        import os
        import subprocess
        import tempfile

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        self.dir = Path(tempfile.mkdtemp())
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}

        def git(*args, cwd=self.repo):
            subprocess.run(["git", "-C", str(cwd), *args], check=True,
                           capture_output=True, env=env)

        git("init", "-q", "-b", "main")
        (self.repo / "seed").write_text("x")
        git("add", "-A")
        git("commit", "-qm", "c1")
        self.worktree = self.dir / "wt-lane"
        # Review j#86739 R3-F2: lane_label and branch are independent create-contract fields,
        # so the fixture's checked-out branch deliberately differs from the lane id.
        git("worktree", "add", "-q", str(self.worktree), "-b", "feature/decoupled_name")
        self.token = derive_lane_workspace_token(str(self.worktree.resolve()))
        self._git = git

    def _candidate(self):
        anchor = LifecycleAnchor(
            issue_id="500", repo_workspace_id=WS, lane_id=LANE,
            lane_generation=GEN, revision=4,
        )
        return HibernateCandidate(
            issue_id="500", anchor=anchor, basis=BASIS_EARLY_HIBERNATE,
            head=BoundField(value=HEAD, provenance="git_remote"), conjuncts=(),
        )

    def _row(self, token=None, generation=GEN):
        row = _Row(lane_generation=generation)
        row.worktree_identity = self.token if token is None else token
        return row

    def _resolve(self, rows):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            resolve_candidate_worktree,
        )

        return resolve_candidate_worktree(self.repo, rows, self._candidate())

    def test_the_token_join_resolves_a_lane_whose_branch_differs_from_its_id(self):
        # The worktree's branch is feature/decoupled_name, not the lane id — the token join
        # must still bind it (review j#86739 R3-F2).
        self.assertEqual(self._resolve([self._row()]), self.worktree.resolve())

    def test_a_row_token_that_does_not_rederive_binds_nothing(self):
        # The topology names the path, but the lifecycle row's own token disagrees — a reused
        # or foreign directory never binds.
        self.assertIsNone(self._resolve([self._row(token="wt_other")]))

    def test_a_removed_worktree_binds_nothing(self):
        import shutil

        shutil.rmtree(self.worktree)
        self._git("worktree", "prune")
        self.assertIsNone(self._resolve([self._row()]))

    def test_a_rebranched_worktree_still_binds_through_its_token(self):
        # Review j#86739 R3-F2 (superseding the R2 round's branch-join reading): switching
        # the checked-out branch does NOT unbind the worktree — the join key is the identity
        # token, and the branch drift is caught downstream where the observed origin head is
        # matched against the durable evidence heads.
        import os
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.worktree), "checkout", "-q", "-b", "other_branch"],
            check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"},
        )
        self.assertEqual(self._resolve([self._row()]), self.worktree.resolve())

    def test_a_detached_worktree_binds_nothing(self):
        # A detached HEAD carries no branch authority for the downstream head observation —
        # the topology observation fails closed as a whole.
        import os
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.worktree), "checkout", "-q", "--detach"],
            check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"},
        )
        self.assertIsNone(self._resolve([self._row()]))

    def test_missing_row_or_token_or_generation_mismatch_binds_nothing(self):
        self.assertIsNone(self._resolve([]))
        bare = _Row()  # no worktree_identity attribute value
        self.assertIsNone(self._resolve([bare]))
        self.assertIsNone(self._resolve([self._row(generation=GEN + 1)]))

    def test_the_display_only_metadata_store_is_never_consulted(self):
        # Review j#86734 R2-F5 structural pin: the resolver module no longer imports the
        # display-join store at all.
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            hibernate_supervisor_wiring,
        )

        self.assertNotIn(
            "LaneMetadataStore", inspect.getsource(hibernate_supervisor_wiring)
        )


class GitObservationTest(unittest.TestCase):
    def setUp(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        self.dir = Path(tempfile.mkdtemp())
        self.repo = self.dir / "repo"
        self.origin = self.dir / "origin.git"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        (self.repo / ".mozyo-bridge").mkdir()
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text("version: 2\n")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"}
        self._env = env
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "c1"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", str(self.origin)],
            check=True,
        )
        # Review j#86739 R3-F2: the lane's worktree is checked out on a branch whose name has
        # nothing to do with the lane id — the head must be observed from the ACTUAL branch.
        self.branch = "feature/decoupled_name"
        self.worktree = self.dir / "wt-lane"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", str(self.worktree),
             "-b", self.branch],
            check=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-q", "origin", self.branch],
            check=True, env=env,
        )
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        row = _Row()
        row.worktree_identity = derive_lane_workspace_token(str(self.worktree.resolve()))
        self.rows = [row]

    def _selected(self, lane=LANE):
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

    def test_the_remote_head_of_the_actual_branch_is_observed(self):
        # The lane id is NOT a branch name here; the observation must come from the topology
        # entry's own branch (review j#86739 R3-F2).
        observation = observe_lane_push(self.repo, self.rows, self._selected())
        self.assertIsNotNone(observation)
        self.assertEqual(observation.head, self.head)
        self.assertTrue(observation.reachable)
        self.assertEqual(
            (observation.workspace, observation.lane, observation.lane_generation),
            (WS, LANE, GEN),
        )

    def test_a_lane_id_named_branch_is_never_read(self):
        # A stale same-named branch exists on origin at a DIFFERENT head; the observation
        # must still return the actual branch's head, not the lane-id ref.
        (self.repo / "drift").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=self._env)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "c2"], check=True, env=self._env
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-q", "origin", f"main:{LANE}"],
            check=True, env=self._env,
        )
        observation = observe_lane_push(self.repo, self.rows, self._selected())
        self.assertIsNotNone(observation)
        self.assertEqual(observation.head, self.head)

    def test_an_unpushed_actual_branch_observes_nothing(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-q", "origin", f":{self.branch}"],
            check=True, env=self._env,
        )
        self.assertIsNone(observe_lane_push(self.repo, self.rows, self._selected()))

    def test_a_detached_worktree_observes_nothing(self):
        subprocess.run(
            ["git", "-C", str(self.worktree), "checkout", "-q", "--detach"],
            check=True, capture_output=True, env=self._env,
        )
        self.assertIsNone(observe_lane_push(self.repo, self.rows, self._selected()))

    def test_a_missing_lifecycle_row_observes_nothing(self):
        self.assertIsNone(observe_lane_push(self.repo, [], self._selected()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
