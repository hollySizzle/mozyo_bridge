"""Action-time evidence drift, end to end (Redmine #14219 T2b, step 5).

Wires the real chain — durable journals → per-conjunct producer → T1 classifier → T2a actuation leg
→ the REAL ``SublaneHibernateUseCase`` over a seeded ``LaneLifecycleStore`` — and drifts ONE thing
between building the candidate and running the pass. Every drift must leave the lifecycle row
``active`` with zero mutation:

- **review supersession** — a newer ``changes_requested`` shadows the approval;
- **integration deferral supersession** — a newer heading-form deferral shadows the merge record
  (the #14213 F1 shape: the newer declaration wins by EXISTING);
- **old generation / cross lane** — genuine evidence, but about a superseded generation or another
  lane;
- **head drift** — the observed lane head moved past the evidence;
- **head advance with re-issued evidence** — the fresh state is a VALID candidate, but not the one
  this pass approved, so the pass still actuates nothing;
- **conflicting markers** — two differing records of the same gate;
- **lifecycle handover** — the lane was superseded by a recovery lane.

The happy path is included as the negative control: with no drift the same wiring DOES hibernate the
row, so a green drift assertion cannot be an inert pipeline.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    ATTEMPT_ACTUATED,
    ATTEMPT_STALE,
    run_hibernate_pass,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_candidate_assembler import (  # noqa: E501
    AssemblyRequest,
    HibernateCandidateAssembler,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_candidate_source import (  # noqa: E501
    read_lifecycle_records,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
    SublaneHibernateUseCase,
    WorktreeMutationFingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    LaneActivityObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_actuation import (  # noqa: E501
    ActionTimeObligations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
    DogfoodReceipt,
    PushObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_COORDINATOR,
    ISSUER_LANE_WORKER,
    ISSUER_REVIEW_GATEWAY,
    EvidenceJournal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
    BASIS_EARLY_HIBERNATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_integration import (  # noqa: E501
    render_integration_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_marker import (  # noqa: E501
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_REQUIRED_CI_GREEN,
    render_hibernate_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    render_workflow_event_marker,
)

WS = "ws-drift"
ISSUE = "14219"
LANE = "lane-drift"
SEED_JOURNAL = "85508"
HEAD = "a" * 40
NEW_HEAD = "d" * 40
STAGING_HEAD = "b" * 40
REQ_JOURNAL = "85000"
RELEASE_ISSUE = "14184"


class _FakeOps:
    """Minimal SublaneHibernateOps: a clean, quiescent lane with no live rows."""

    def __init__(self) -> None:
        self.close_calls: list = []

    def workspace_id(self) -> str:
        return WS

    def read_inventory(self):
        return [], True

    def read_attestation(self, assigned_name):
        return None

    def read_worktree_mutation(self):
        return WorktreeMutationFingerprint(readable=True)

    def read_lane_activity(self, workspace_id, lane, rows):
        return LaneActivityObservation(readable=True)

    def execute_close(self, plan):  # pragma: no cover - no live rows to close
        self.close_calls.append(plan)
        raise AssertionError("no live rows: execute_close must not be reached")


def _env(*, lane=LANE, gen, head=HEAD) -> LaneEvidenceEnvelope:
    return LaneEvidenceEnvelope(workspace=WS, lane=lane, lane_generation=gen, head=head)


def _request(*, head=HEAD) -> str:
    return "review request\n" + render_workflow_event_marker("review_request", target_head=head)


def _review(*, gen, conclusion="approved", head=HEAD, lane=LANE) -> str:
    return "review\n" + render_workflow_event_marker(
        "review_result",
        target_head=head,
        review_request_journal=REQ_JOURNAL,
        conclusion=conclusion,
        evidence_workspace=WS,
        evidence_lane=lane,
        evidence_lane_generation=gen,
    )


def _integration(*, gen, head=HEAD, lane=LANE) -> str:
    return "## Integration disposition\n" + render_integration_evidence(
        envelope=_env(gen=gen, head=head, lane=lane),
        integration_head=STAGING_HEAD,
        integration_branch="main-next",
        disposition="merge",
    )


def _ci(*, gen, head=HEAD, lane=LANE, run="299") -> str:
    return "ci\n" + render_hibernate_evidence(
        EVIDENCE_REQUIRED_CI_GREEN,
        envelope=_env(gen=gen, head=head, lane=lane),
        workflow="test.yml",
        run=run,
    )


def _dogfood(*, gen, head=HEAD, lane=LANE) -> str:
    return "dogfood\n" + render_hibernate_evidence(
        EVIDENCE_DOGFOOD_DELEGATED,
        envelope=_env(gen=gen, head=head, lane=lane),
        release_issue=RELEASE_ISSUE,
        acceptance="85431",
    )


def _evidenced(*, gen, head=HEAD, lane=LANE) -> list:
    """The durable records a fully-evidenced early-hibernate lane carries, with their writers."""
    return [
        EvidenceJournal(REQ_JOURNAL, _request(head=head), ISSUER_LANE_WORKER),
        EvidenceJournal("85001", _review(gen=gen, head=head, lane=lane), ISSUER_REVIEW_GATEWAY),
        EvidenceJournal("85002", _integration(gen=gen, head=head, lane=lane), ISSUER_COORDINATOR),
        EvidenceJournal("85003", _ci(gen=gen, head=head, lane=lane), ISSUER_COORDINATOR),
        EvidenceJournal("85004", _dogfood(gen=gen, head=head, lane=lane), ISSUER_COORDINATOR),
    ]


def _obligations() -> ActionTimeObligations:
    return ActionTimeObligations(
        callbacks_drained=True,
        no_review_pending=True,
        no_owner_approval_pending=True,
        no_integration_pending=True,
        no_pending_prompt=True,
        not_working=True,
        worktree_clean=True,
        boundary_recorded=False,
    )


@dataclass
class _World:
    """The mutable action-time world the assembler's ports read."""

    home: Path
    store: LaneLifecycleStore
    journals: list
    head: str = HEAD
    receipt_head: str = HEAD

    def push(self, selected) -> PushObservation:
        return PushObservation(
            workspace=selected.repo_workspace_id,
            lane=selected.lane_id,
            lane_generation=selected.lane_generation,
            head=self.head,
            reachable=True,
        )

    def receipts(self, issue: str) -> dict:
        """The release issue's receipt, which tracks the head the delegation was recorded at."""
        return {
            RELEASE_ISSUE: DogfoodReceipt(
                release_issue=RELEASE_ISSUE, source_issue=issue, head=self.receipt_head
            )
        }

    def assembler(self) -> HibernateCandidateAssembler:
        return HibernateCandidateAssembler(
            records_fn=lambda: read_lifecycle_records(home=self.home),
            journals_fn=lambda issue: list(self.journals),
            push_fn=self.push,
            obligations_fn=lambda candidate: _obligations(),
            dogfood_receipts_fn=self.receipts,
        )


class HibernateEvidenceDriftTests(unittest.TestCase):
    def _world(self, home: Path) -> _World:
        store = LaneLifecycleStore(home=home)
        store.declare_active(
            LaneLifecycleKey(WS, LANE),
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=SEED_JOURNAL),
            issue_id=ISSUE,
        )
        rec = store.get(LaneLifecycleKey(WS, LANE))
        return _World(home=home, store=store, journals=_evidenced(gen=rec.lane_generation))

    def _selected(self, world: _World):
        rec = world.store.get(LaneLifecycleKey(WS, LANE))
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
            SelectedLane,
        )

        return SelectedLane(
            issue_id=ISSUE,
            repo_workspace_id=WS,
            lane_id=LANE,
            lane_generation=rec.lane_generation,
            revision=rec.revision,
        )

    def _build(self, world: _World):
        """Assemble the candidate from the CURRENT world (the pre-drift observation)."""
        assembled = world.assembler().assemble(
            AssemblyRequest(selected=self._selected(world), basis=BASIS_EARLY_HIBERNATE)
        )
        self.assertIsNotNone(
            assembled.candidate, f"pre-drift build must qualify: {assembled.verdict.as_payload()}"
        )
        return assembled.candidate

    def _run(self, world: _World, candidate):
        """Run one bounded pass with seams bound to the CURRENT (possibly drifted) world."""
        seams = world.assembler().pass_seams()
        return run_hibernate_pass(
            [candidate],
            refresh_fn=seams.refresh_fn,
            obligations_fn=seams.obligations_fn,
            journal_fn=seams.journal_fn,
            use_case=SublaneHibernateUseCase(ops=_FakeOps(), store=world.store),
            lease_renew_fn=lambda: True,
        )

    def _disposition(self, world: _World, lane=LANE) -> str:
        return world.store.get(LaneLifecycleKey(WS, lane)).lane_disposition

    def _assert_zero_actuation(self, world: _World, result, *, disposition=DISPOSITION_ACTIVE):
        self.assertEqual(result.mutations, 0)
        self.assertEqual([a.kind for a in result.attempts], [ATTEMPT_STALE])
        # The lane keeps whatever disposition the drift itself left it in — what must NOT happen is
        # this pass hibernating it.
        self.assertEqual(self._disposition(world), disposition)

    def _drift(self, mutate, *, disposition=DISPOSITION_ACTIVE):
        """Build from clean evidence, apply ``mutate(world)``, then run the pass."""
        with TemporaryDirectory() as raw:
            world = self._world(Path(raw))
            candidate = self._build(world)
            mutate(world)
            self._assert_zero_actuation(
                world, self._run(world, candidate), disposition=disposition
            )

    # -- negative control -------------------------------------------------------------------

    def test_undrifted_evidence_actuates_once(self):
        with TemporaryDirectory() as raw:
            world = self._world(Path(raw))
            candidate = self._build(world)
            result = self._run(world, candidate)
            self.assertEqual(result.mutations, 1)
            self.assertEqual([a.kind for a in result.attempts], [ATTEMPT_ACTUATED])
            self.assertEqual(self._disposition(world), DISPOSITION_HIBERNATED)

    # -- drifts -----------------------------------------------------------------------------

    def test_review_supersession_actuates_nothing(self):
        def mutate(world):
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.journals.append(EvidenceJournal(
                "85009", _review(gen=gen, conclusion="changes_requested"), ISSUER_REVIEW_GATEWAY
            ))

        self._drift(mutate)

    def test_newer_integration_deferral_actuates_nothing(self):
        # A heading-form deferral carries no enveloped marker. It must still SUPERSEDE the older
        # merge record (declaration wins by existing), not be skipped as unreadable.
        def mutate(world):
            world.journals.append(EvidenceJournal(
                "85010",
                "## Integration disposition: explicit_deferral\n- reason: waiting",
                ISSUER_COORDINATOR,
            ))

        self._drift(mutate)

    def test_old_generation_evidence_actuates_nothing(self):
        def mutate(world):
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.journals[:] = _evidenced(gen=gen + 1)

        self._drift(mutate)

    def test_cross_lane_evidence_actuates_nothing(self):
        def mutate(world):
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.journals[:] = _evidenced(gen=gen, lane="lane-other")

        self._drift(mutate)

    def test_head_drift_actuates_nothing(self):
        # The lane advanced; the evidence still names the reviewed head.
        self._drift(lambda world: setattr(world, "head", NEW_HEAD))

    def test_head_advance_with_reissued_evidence_actuates_nothing(self):
        # The fresh state is a VALID candidate at the new head — but it is not the candidate this
        # pass approved, and equality (not mere validity) is what authorises the actuation.
        def mutate(world):
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.head = NEW_HEAD
            world.receipt_head = NEW_HEAD
            world.journals[:] = _evidenced(gen=gen, head=NEW_HEAD)

        self._drift(mutate)

    def test_conflicting_evidence_in_one_declaration_actuates_nothing(self):
        # Two DIFFERING records of the same gate in the one current declaration: neither can be
        # preferred, so the conjunct is a typed gap.
        def mutate(world):
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.journals.append(EvidenceJournal(
                "85011",
                _ci(gen=gen, run="300") + "\n" + _ci(gen=gen, head=NEW_HEAD, run="301"),
                ISSUER_COORDINATOR,
            ))

        self._drift(mutate)

    def test_a_later_ci_record_supersedes_rather_than_conflicts(self):
        # The counterpart to the test above, so "conflict" is not read as "any second record":
        # a NEWER green CI run for the same lane and head is a supersession, and the lane stays
        # actuatable. Without this, a conflict rule that rejected every repeat record would look
        # correct.
        with TemporaryDirectory() as raw:
            world = self._world(Path(raw))
            candidate = self._build(world)
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.journals.append(
                EvidenceJournal("85011", _ci(gen=gen, run="300"), ISSUER_COORDINATOR)
            )
            result = self._run(world, candidate)
            self.assertEqual(result.mutations, 1)
            self.assertEqual(self._disposition(world), DISPOSITION_HIBERNATED)

    def test_a_new_review_request_supersedes_the_approval(self):
        # The most ordinary drift there is: a re-review is requested between build and actuation.
        # The old approval answers the old question, so the lane stops qualifying at once.
        def mutate(world):
            world.journals.append(
                EvidenceJournal("85020", _request(), ISSUER_LANE_WORKER)
            )

        self._drift(mutate)

    def test_evidence_rewritten_by_the_wrong_actor_actuates_nothing(self):
        # A newer CI record from an actor without that authority supersedes the coordinator's by
        # EXISTING, and then fails the issuer check — it does not fall back to the older good one.
        def mutate(world):
            gen = world.store.get(LaneLifecycleKey(WS, LANE)).lane_generation
            world.journals.append(
                EvidenceJournal("85021", _ci(gen=gen), ISSUER_LANE_WORKER)
            )

        self._drift(mutate)

    def test_lifecycle_handover_actuates_nothing(self):
        def mutate(world):
            rec = world.store.get(LaneLifecycleKey(WS, LANE))
            world.store.supersede_and_activate(
                superseded=LaneLifecycleKey(WS, LANE),
                expected_revision=rec.revision,
                recovery=LaneLifecycleKey(WS, "lane-recovery"),
                decision=DecisionPointer(
                    source="redmine", issue_id=ISSUE, journal_id="85012"
                ),
            )

        self._drift(mutate, disposition="superseded")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
