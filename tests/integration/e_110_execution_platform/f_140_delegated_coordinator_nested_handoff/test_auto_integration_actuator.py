"""Auto-integration actuator composition tests (Redmine #13686).

Wires the two pure #13686 state machines to recording fakes and pins what the composition —
not the decision — is responsible for.

The central contract, arrived at over three review rounds (j#96344 -> j#96350 -> j#96368):
**the actuator measures its own safety facts.** There is no caller preflight. A caller
supplies an action record (identity) and the actuator's own lane configuration; every fact
that gates a mutation is read from a port at action time. So the way to express a world in
these tests is to configure the *fakes*, not to hand the use case a set of booleans — which
is exactly the difference that let R1-R3 authorize integrations and deletions on the
requester's own say-so.

Covered here:

- the config -> policy translations;
- that a refused action mutates nothing, and that read probes are not side effects;
- the fast-forward and merge-commit runs, including that a resumed merge pushes the commit
  the earlier apply produced;
- R3 review j#96368 finding 1: no durable authority reader means nothing is established, so
  the integration is refused rather than admitted;
- R3 finding 2: a cleanup may only touch **this actuator's own** lane worktree and branch —
  the reproduction removed a foreign lane's worktree and deleted its branch;
- R3 finding 3: ledger provenance and order are checked before any mutation, and a merge push
  never falls back to the source head;
- R3 finding 4: the `coordinator_confirmed` mode is not offered at all.

Hermetic: every port is a fake that records its calls. No real ``git``, no network.
"""
from __future__ import annotations

import inspect
import sys
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    auto_integration_actuator,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    AutoIntegrationGitOperations,
    AutoIntegrationUseCase,
    CleanupAuthority,
    InMemoryLedgerStore,
    LedgerStore,
    DurableAuthorityReader,
    IntegrationAuthority,
    ManagedProcessOperations,
    MERGE_CONTENT_CONFLICT,
    MERGE_ERROR,
    MERGE_MERGED,
    MERGE_NONDETERMINISTIC_CONFIG,
    MERGE_PRIMITIVE_UNSUPPORTED,
    MERGE_PROBE_ERROR,
    MERGE_SANDBOX_ERROR,
    MERGE_UNRECOGNIZED,
    PUSH_ACCEPTED,
    PUSH_INVALID_INPUT,
    PUSH_OPERATIONAL_ERROR,
    PUSH_REMOTE_MOVED,
    PUSH_REMOTE_REFUSED,
    PUSH_UNRECOGNIZED,
    MergeResult,
    PushResult,
    integration_policy_from_config,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    MODE_AUTO,
    MODE_DISABLED,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_PENDING,
    STATE_AWAITING_CI,
    STATE_DISABLED,
    STATE_INTEGRATED,
    STATE_INTEGRATION_BLOCKED,
    STATE_NOT_APPLICABLE,
    STEP_INTEGRATION_APPLY,
    STEP_INTEGRATION_CI,
    STEP_PUSH,
    AutoIntegrationPolicy,
    IntegrationActionRecord,
    IntegrationCiEvidence,
    LaneWorktree,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    STATE_CLEANUP_BLOCKED,
    STATE_RETIRED,
    STEP_PROCESS_RETIRE,
    CleanupActionRecord,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (
    AutoIntegrationConfig,
)

SOURCE = "a" * 40
TARGET = "b" * 40
MERGE_HEAD = "d" * 40
#: A real apply names the git that built the commit; a `done` apply without it is refused
#: by the ledger since j#96441 finding 4.
GIT_VERSION = "git version 2.50.1"
OTHER = "e" * 40

LANE_WORKTREE = "/lane"
LANE_BRANCH = "lane_br"
#: What the use case stamps on the outcomes it records (and therefore trusts on resume).
#: The receipt is per-instance and unguessable, so tests seed a ledger by driving
#: the actuator rather than by authoring entries (which is the point of the fix).

#: The read-only probes. Calling one is not a side effect, so the zero-side-effect assertions
#: look at mutations only.
_READ_PROBES = (
    "describe_lane_worktree",
    "is_git_workspace",
    "resolve_head",
    "remote_branch_tip",
    "is_ancestor",
    "worktree_dirty",
    "commit_on_remote",
)


@dataclass
class FakeGitOperations:
    """A recording :class:`AutoIntegrationGitOperations` with configurable measurements."""

    git_workspace: bool = True
    #: What the REMOTE says the target is (the gate's authority).
    target_head: str = TARGET
    #: What this clone's LOCAL ref says — deliberately separable, so a test can make them
    #: disagree the way a target another clone advanced does (j#96379 finding 4).
    local_head: str = TARGET
    #: ``is_ancestor`` answers by (ancestor, descendant) pair; the default says "no ancestry",
    #: so neither ``already_integrated`` nor a fast-forward is true unless configured.
    ancestors: Tuple[Tuple[str, str], ...] = ()
    lane_clean: bool = True
    lane_registered: bool = True
    lane_branch_checked_out: str = LANE_BRANCH
    #: What the object-level merge would find the target parent to be. ``None`` means it is
    #: whatever was expected (the healthy case); setting it models a merge asked to sit on a
    #: parent other than the measured remote target.
    refuse_parent_other_than: Optional[str] = None
    on_remote: bool = True
    tip: str = SOURCE

    merge_result: MergeResult = field(
        default_factory=lambda: MergeResult(
            status=MERGE_MERGED, integration_head=MERGE_HEAD, git_version=GIT_VERSION
        )
    )
    push_result: PushResult = field(default_factory=lambda: PushResult(status=PUSH_ACCEPTED))
    calls: List[Tuple[str, dict]] = field(default_factory=list)

    # -- read probes ------------------------------------------------------
    def is_git_workspace(self) -> bool:
        return self.git_workspace

    def resolve_head(self, ref: str) -> str:
        self.calls.append(("resolve_head", {"ref": ref}))
        return self.local_head

    def remote_branch_tip(self, branch: str) -> str:
        self.calls.append(("remote_branch_tip", {"branch": branch}))
        return self.target_head

    def is_ancestor(self, *, ancestor: str, descendant: str) -> bool:
        self.calls.append(("is_ancestor", {"a": ancestor, "d": descendant}))
        return (ancestor, descendant) in self.ancestors

    def worktree_dirty(self, *, worktree_path: str = "") -> bool:
        self.calls.append(("worktree_dirty", {"worktree_path": worktree_path}))
        return not self.lane_clean

    def commit_on_remote(self, commit: str, *, branch: str) -> bool:
        self.calls.append(("commit_on_remote", {"commit": commit, "branch": branch}))
        return self.on_remote

    def describe_lane_worktree(self, *, path: str) -> LaneWorktree:
        self.calls.append(("describe_lane_worktree", {"path": path}))
        return LaneWorktree(
            path=path,
            registered=self.lane_registered,
            clean=self.lane_clean,
            checked_out_branch=self.lane_branch_checked_out,
        )

    # -- mutations --------------------------------------------------------
    def apply_merge(
        self, *, source_head: str, target_ref: str, expected_target_head: str
    ) -> MergeResult:
        self.calls.append(
            (
                "apply_merge",
                {
                    "source_head": source_head,
                    "target_ref": target_ref,
                    "expected_target_head": expected_target_head,
                },
            )
        )
        # The live adapter makes `expected_target_head` the merge's first parent by
        # construction (R6 review j#96391 finding 1 bound it; j#96406 finding 1 removed the
        # checkout it used to be read from). The fake models a refusal so a test can exercise
        # the use case's handling of one.
        if self.refuse_parent_other_than is not None and (
            self.refuse_parent_other_than != expected_target_head
        ):
            return MergeResult(
                status=MERGE_ERROR,
                detail="the merge parent is not the measured remote target",
            )
        return self.merge_result

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        self.calls.append(
            ("push_non_force", {"source_head": source_head, "target_ref": target_ref})
        )
        if self.push_result.accepted:
            # THE WORLD MOVES. R5 review j#96385 findings 2 and 5: this fake used to leave the
            # target head untouched after a push, so the actuator's own successful push was
            # invisible to the next run — which is precisely the condition that made the
            # resume block on `target_drift` forever, and precisely why the tests missed it.
            # A fake for a mutating port that does not apply its own mutation tells a lie the
            # tests then certify.
            self.target_head = source_head
            self.ancestors = tuple(set(self.ancestors) | {(source_head, source_head)})
        return self.push_result

    # There is deliberately no `remove_worktree` here. The port lost it with review j#96401
    # finding 1 (the removal named its target by a path an earlier probe had vouched for),
    # and a fake that still answered it would let a test assert behaviour no production code
    # can reach — the inverse of the R6 problem, where the fake modelled half a mutation.

    @property
    def performed(self) -> List[str]:
        """The MUTATIONS this port was asked for, in order."""
        return [name for name, _ in self.calls if name not in _READ_PROBES]

    def args_for(self, name: str) -> List[dict]:
        return [args for called, args in self.calls if called == name]


#: The integration action key the durable authority reports as having authorized the lane's
#: cleanup, and the one the cleanup records carry. They are equal here so the gate PASSES;
#: the tests that matter are the ones where they differ.
CLEANUP_AUTHORIZING_KEY = "k"


@dataclass
class FakeAuthorityReader:
    """Supplies the durable-record facts no git probe can answer."""

    authority: IntegrationAuthority = field(
        default_factory=lambda: IntegrationAuthority(
            review_generation_admissible=True,
            review_generation="j#96337",
            reviewed_head=SOURCE,
            target_identity_known=True,
            callbacks_drained=True,
            owner_gates_resolved=True,
            source_ci=IntegrationCiEvidence(
                integration_head=SOURCE,
                workflow="required-ci",
                run="src-1",
                conclusion="success",
            ),
        )
    )
    cleanup: CleanupAuthority = field(
        default_factory=lambda: CleanupAuthority(
            issue_closed=True,
            integration_confirmed=True,
            integration_ci_settled_green=True,
            callbacks_drained=True,
            owner_gates_resolved=True,
            # The authorizing integration action, as a DURABLE reader answers it (Redmine
            # #14825 item 5). Before that, the preflight took this from the very field the
            # decision compares it against, so the gate could not fail and no fake had to
            # supply it. It has to be supplied now, which is the point: the two sides of the
            # comparison have separate sources again.
            authorizing_action_key=CLEANUP_AUTHORIZING_KEY,
        )
    )
    withhold_ci: bool = False

    def read_integration_authority(self, *, record) -> IntegrationAuthority:
        return self.authority

    def current_review_generation(self, *, record) -> str:
        if (
            self.authority.review_generation_admissible
            and self.authority.reviewed_head == record.source_head
        ):
            return self.authority.review_generation
        return ""

    def read_integration_ci(self, *, record, integration_head):
        if self.withhold_ci:
            return None
        return IntegrationCiEvidence(
            integration_head=integration_head,
            workflow="required-ci",
            run="int-1",
            conclusion="success",
        )

    def read_cleanup_authority(self, *, record) -> CleanupAuthority:
        return self.cleanup


@dataclass
class FakeProcessOperations:
    released: bool = True
    calls: List[dict] = field(default_factory=list)

    def release_process(self, *, issue: str, lane_generation: int) -> bool:
        self.calls.append({"issue": issue, "lane_generation": lane_generation})
        return self.released


def _record(**overrides: object) -> IntegrationActionRecord:
    fields: dict = {
        "issue": "13686",
        "lane_generation": 3,
        "source_head": SOURCE,
        "target_ref": "main",
        "expected_target_head": TARGET,
        "review_generation": "j#96337",
    }
    fields.update(overrides)
    return IntegrationActionRecord(**fields)  # type: ignore[arg-type]


def _ff_ancestors() -> Tuple[Tuple[str, str], ...]:
    """Ancestry making a fast-forward possible and nothing already integrated."""
    return ((TARGET, SOURCE),)


def _use_case(operations: FakeGitOperations, **kwargs: object) -> AutoIntegrationUseCase:
    defaults: dict = {
        "integration_policy": AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main"
        ),
        "authority": FakeAuthorityReader(),
        "lane_worktree": LANE_WORKTREE,
        "lane_branch": LANE_BRANCH,
        "lane_issue": "13686",
        "lane_generation": 3,
    }
    defaults.update(kwargs)
    return AutoIntegrationUseCase(operations=operations, **defaults)  # type: ignore[arg-type]


class PortConformanceTest(unittest.TestCase):
    def test_the_fakes_satisfy_the_declared_ports(self) -> None:
        self.assertIsInstance(FakeGitOperations(), AutoIntegrationGitOperations)
        self.assertIsInstance(FakeProcessOperations(), ManagedProcessOperations)
        self.assertIsInstance(FakeAuthorityReader(), DurableAuthorityReader)

    def test_the_use_case_takes_no_caller_preflight(self) -> None:
        # The whole contract in one assertion: a caller cannot hand the actuator a fact.
        for name in ("run_integration", "run_cleanup"):
            parameters = set(
                inspect.signature(getattr(AutoIntegrationUseCase, name)).parameters
            )
            self.assertNotIn("preflight", parameters, name)


class ConfigTranslationTest(unittest.TestCase):
    def test_integration_fields_map_through(self) -> None:
        config = AutoIntegrationConfig(
            mode=MODE_AUTO, integration_branch="release", ff_only=False
        )
        policy = integration_policy_from_config(config)
        self.assertEqual(policy.mode, MODE_AUTO)
        self.assertEqual(policy.integration_branch, "release")
        self.assertFalse(policy.ff_only)
        for gone in ("require_source_ci", "require_integration_ci"):
            self.assertFalse(hasattr(config, gone), gone)
            self.assertFalse(hasattr(policy, gone), gone)

    def test_the_config_has_no_cleanup_field_and_no_cleanup_translation(self) -> None:
        # All three cleanup steps were withdrawn (j#96344 / j#96396 / j#96401, each finding 1),
        # so there is nothing left for a config key to turn off and nothing for a cleanup
        # policy translation to carry.
        config = AutoIntegrationConfig.default()
        for gone in ("delete_remote_branch", "delete_local_branch", "remove_worktree"):
            self.assertFalse(hasattr(config, gone), gone)
        self.assertFalse(
            hasattr(auto_integration_actuator, "cleanup_policy_from_config")
        )

    def test_the_default_config_translates_to_a_disabled_actuator(self) -> None:
        self.assertEqual(
            integration_policy_from_config(AutoIntegrationConfig.default()).mode,
            MODE_DISABLED,
        )


class ZeroSideEffectTest(unittest.TestCase):
    def test_a_disabled_actuator_mutates_nothing(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(
            operations, integration_policy=AutoIntegrationPolicy.default()
        ).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_DISABLED)
        self.assertEqual(operations.performed, [])

    def test_a_non_git_workspace_mutates_nothing(self) -> None:
        operations = FakeGitOperations(git_workspace=False)
        report = _use_case(operations).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_NOT_APPLICABLE)
        self.assertEqual(operations.performed, [])

    def test_a_dirty_lane_mutates_nothing(self) -> None:
        operations = FakeGitOperations(ancestors=_ff_ancestors(), lane_clean=False)
        report = _use_case(operations).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_already_integrated_mutates_nothing(self) -> None:
        operations = FakeGitOperations(ancestors=((SOURCE, TARGET),))
        report = _use_case(operations).run_integration(_record())
        self.assertTrue(report.final_decision.integrated)
        self.assertEqual(operations.performed, [])


class R4ReviewFinding4Test(unittest.TestCase):
    """The target gate reads the REMOTE tip, not this clone's local ref."""

    def test_a_target_another_clone_advanced_is_seen_as_drift(self) -> None:
        # R4 review j#96379 finding 4: `_measure` used a local `git rev-parse` while a fresh
        # `ls-remote` probe sat unused on the same adapter, so a target another clone had
        # moved still read as its old SHA. Local disagrees with remote here; the gate must
        # follow the remote.
        operations = FakeGitOperations(
            ancestors=_ff_ancestors(),
            local_head=TARGET,          # this clone's stale opinion
            target_head=OTHER,          # what the remote actually has
        )
        report = _use_case(operations).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_the_gate_asks_the_remote_probe(self) -> None:
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        _use_case(operations).run_integration(_record())
        self.assertTrue(operations.args_for("remote_branch_tip"))


class R3ReviewFinding1Test(unittest.TestCase):
    """The actuator measures the whole preflight; an unreadable world refuses."""

    def test_no_authority_reader_means_nothing_is_established(self) -> None:
        # R3 review j#96368 finding 1: R3 took review generation, origin reachability, source
        # CI, target identity, callback and owner gates verbatim from the caller. With no
        # reader there is nothing to take, and the run refuses rather than admitting.
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        report = _use_case(operations, authority=None).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_the_reviewed_head_must_be_the_head_being_integrated(self) -> None:
        reader = FakeAuthorityReader(
            authority=IntegrationAuthority(
                review_generation_admissible=True,
                review_generation="j#96337",
                reviewed_head=OTHER,  # a DIFFERENT commit was reviewed
                target_identity_known=True,
                callbacks_drained=True,
                owner_gates_resolved=True,
                source_ci=IntegrationCiEvidence(
                    integration_head=SOURCE,
                    workflow="ci",
                    run="1",
                    conclusion="success",
                ),
            )
        )
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        report = _use_case(operations, authority=reader).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_the_review_generation_must_equal_the_request_the_action_names(self) -> None:
        reader = FakeAuthorityReader(
            authority=replace(
                FakeAuthorityReader().authority,
                review_generation="some-other-review-request",
            )
        )
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        use_case = _use_case(operations, authority=reader)
        report = use_case.run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])
        self.assertEqual(use_case.ledger.read(action_key=_record().action_key), [])

    def test_foreignness_is_answered_from_the_actuator_s_own_lane_branch(self) -> None:
        # The lane checkout must be a registered worktree holding THIS actuator's branch.
        operations = FakeGitOperations(
            ancestors=_ff_ancestors(), lane_branch_checked_out="somebody_elses_branch"
        )
        report = _use_case(operations).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_the_probe_is_asked_about_the_actuator_s_own_lane(self) -> None:
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        _use_case(operations).run_integration(_record())
        probed = {args["path"] for args in operations.args_for("describe_lane_worktree")}
        # One path, and it is this actuator's own. The dedicated integration checkout it used
        # to probe alongside is gone with the worktree merge (j#96406 finding 1).
        self.assertEqual(probed, {LANE_WORKTREE})


class FastForwardRunTest(unittest.TestCase):
    def test_a_fast_forward_pushes_the_source_head_and_never_merges(self) -> None:
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        report = _use_case(
            operations, authority=FakeAuthorityReader(withhold_ci=True)
        ).run_integration(_record())
        self.assertEqual(operations.performed, ["push_non_force"])
        self.assertEqual(
            operations.args_for("push_non_force")[0],
            {"source_head": SOURCE, "target_ref": "main"},
        )
        # It stops at the asynchronous CI gate rather than waiting on it.
        self.assertEqual(report.final_decision.state, STATE_AWAITING_CI)
        self.assertEqual(
            [(o.step, o.outcome) for o in report.outcomes],
            [(STEP_PUSH, OUTCOME_DONE), (STEP_INTEGRATION_CI, OUTCOME_PENDING)],
        )

    def test_a_run_rests_at_awaiting_ci_until_the_authority_reports_a_verdict(self) -> None:
        # While CI has not settled the run rests; once the authority reports green for the
        # commit that landed, the SAME actuator resumes from its own ledger and completes.
        # R5 review j#96385 finding 2: this used to be impossible — the resume compared the
        # target against the pre-push expectation, so the actuator's own successful push
        # looked like drift and every resume blocked.
        record = _record()
        reader = FakeAuthorityReader(withhold_ci=True)
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        use_case = _use_case(operations, authority=reader)

        waiting = use_case.run_integration(record)
        self.assertEqual(waiting.final_decision.state, STATE_AWAITING_CI)
        self.assertEqual(operations.performed, ["push_non_force"])

        reader.withhold_ci = False
        resumed = use_case.run_integration(record)
        self.assertEqual(resumed.final_decision.state, STATE_INTEGRATED)
        # ...and it did not push again.
        self.assertEqual(operations.performed, ["push_non_force"])

    def test_work_rewritten_off_the_target_after_our_push_is_not_integrated(self) -> None:
        # The post-push counterpart of drift: our push landed, then somebody reset the branch.
        record = _record()
        reader = FakeAuthorityReader(withhold_ci=True)
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        use_case = _use_case(operations, authority=reader)
        use_case.run_integration(record)

        operations.target_head = OTHER          # somebody rewrote the target
        operations.ancestors = ()               # our commit is no longer reachable from it
        reader.withhold_ci = False
        lost = use_case.run_integration(record)
        self.assertEqual(lost.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, ["push_non_force"])

    def test_a_different_actuator_instance_cannot_resume_this_one_s_work(self) -> None:
        # The receipt is per-instance, so a second actuator sharing the same store does not
        # count the first one's entries: it starts over rather than trusting them.
        record = _record()
        store = InMemoryLedgerStore()
        _use_case(FakeGitOperations(ancestors=_ff_ancestors()), ledger=store).run_integration(
            record
        )
        self.assertTrue(store.entries)
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        _use_case(operations, ledger=store).run_integration(record)
        self.assertIn("push_non_force", operations.performed)

    def test_a_rejected_push_stops_the_run_and_never_escalates(self) -> None:
        operations = FakeGitOperations(
            ancestors=_ff_ancestors(),
            push_result=PushResult(status=PUSH_REMOTE_MOVED, detail="stale target"),
        )
        report = _use_case(operations).run_integration(_record())
        self.assertEqual(operations.performed, ["push_non_force"])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)

    def test_recorded_outcomes_carry_this_actuator_s_unguessable_receipt(self) -> None:
        # R4 review j#96379 finding 1: the receipt used to be derived from public constructor
        # values, so a caller could reproduce it. It is per-instance and unguessable now.
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        use_case = _use_case(operations)
        report = use_case.run_integration(_record())
        self.assertTrue(report.outcomes)
        for outcome in report.outcomes:
            self.assertEqual(outcome.recorded_by, use_case.recorder_id)
        # Two actuators built identically do not share a receipt.
        self.assertNotEqual(
            use_case.recorder_id, _use_case(FakeGitOperations()).recorder_id
        )


class MergeCommitRunTest(unittest.TestCase):
    def _policy(self) -> AutoIntegrationPolicy:
        return AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )

    def test_a_merge_is_built_from_objects_then_pushes_the_merge_commit(self) -> None:
        operations = FakeGitOperations()  # no ancestry -> not a fast-forward
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        self.assertEqual(operations.performed, ["apply_merge", "push_non_force"])
        # Object ids only: nothing names a checkout for anything to re-point.
        self.assertEqual(
            set(operations.args_for("apply_merge")[0]),
            {"source_head", "target_ref", "expected_target_head"},
        )
        self.assertEqual(
            operations.args_for("push_non_force")[0]["source_head"], MERGE_HEAD
        )
        self.assertEqual(report.integration_head, MERGE_HEAD)

    def test_a_conflict_stops_before_any_push(self) -> None:
        operations = FakeGitOperations(
            merge_result=MergeResult(
                status=MERGE_CONTENT_CONFLICT, detail="conflict in a.py"
            )
        )
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        self.assertEqual(operations.performed, ["apply_merge"])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)

    def test_every_merge_failure_reaches_the_durable_record_as_a_typed_field(self) -> None:
        # j#96412 finding 2 asked for the typed status to reach the durable outcome; R11
        # string-formatted it into `detail`, which is the same unparseable sentence with more
        # words (j#96417 finding 2). It is a FIELD now, and this asserts the field — including
        # across the payload round trip a durable store performs.
        for status in (
            MERGE_CONTENT_CONFLICT,
            MERGE_PRIMITIVE_UNSUPPORTED,
            MERGE_PROBE_ERROR,
            MERGE_SANDBOX_ERROR,
            MERGE_NONDETERMINISTIC_CONFIG,  # legacy: parses, no current producer emits it
            MERGE_ERROR,
        ):
            operations = FakeGitOperations(
                merge_result=MergeResult(status=status, detail="because")
            )
            report = _use_case(
                operations, integration_policy=self._policy()
            ).run_integration(_record())
            self.assertEqual(operations.performed, ["apply_merge"], status)
            outcome = report.outcomes[-1]
            self.assertEqual(outcome.outcome, OUTCOME_BLOCKED, status)
            self.assertEqual(outcome.merge_status, status)
            self.assertEqual(outcome.as_payload()["merge_status"], status)
            self.assertEqual(
                StepOutcome.from_payload(outcome.as_payload()).merge_status, status
            )

    def test_a_status_outside_the_vocabulary_is_typed_not_prose(self) -> None:
        # A port that returns something unknown must not be able to introduce an outcome by
        # writing a sentence: the record says `unrecognized_status`, which a consumer matches.
        operations = FakeGitOperations(
            merge_result=MergeResult(status="something_new", detail="?")
        )
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        self.assertEqual(report.outcomes[-1].merge_status, MERGE_UNRECOGNIZED)
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)

    def test_a_successful_apply_records_its_status_too(self) -> None:
        operations = FakeGitOperations()
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        applied = [o for o in report.outcomes if o.step == STEP_INTEGRATION_APPLY][0]
        self.assertEqual(applied.merge_status, MERGE_MERGED)
        # Every other step leaves the field empty rather than inventing a value for it.
        for other in report.outcomes:
            if other.step != STEP_INTEGRATION_APPLY:
                self.assertEqual(other.merge_status, "")

    def test_the_merge_names_no_checkout_for_anything_to_re_point(self) -> None:
        # R2 gated the dedicated checkout's identity so the lane's own could never be used.
        # Review j#96406 finding 1 reproduced a foreign lane's checkout swapped onto that path
        # AFTER the gate, so the gate went with the checkout: the port takes object ids now,
        # and the use case has no path to hand it.
        operations = FakeGitOperations()
        _use_case(operations, integration_policy=self._policy()).run_integration(_record())
        arguments = operations.args_for("apply_merge")[0]
        for value in arguments.values():
            self.assertNotIn("/", str(value), arguments)
        self.assertFalse(
            hasattr(_use_case(FakeGitOperations()), "integration_worktree_path")
        )

    def test_a_resumed_merge_pushes_the_commit_its_own_apply_produced(self) -> None:
        # Run 1 applies and its push is rejected; run 2 resumes from the actuator's own
        # ledger and must use the merge commit recorded there, never the source head.
        record = _record()
        operations = FakeGitOperations(
            push_result=PushResult(status=PUSH_REMOTE_MOVED)
        )
        use_case = _use_case(operations, integration_policy=self._policy())
        use_case.run_integration(record)
        self.assertEqual(operations.performed, ["apply_merge", "push_non_force"])

        operations.push_result = PushResult(status=PUSH_ACCEPTED)
        use_case.run_integration(record)
        for pushed in operations.args_for("push_non_force"):
            self.assertEqual(pushed["source_head"], MERGE_HEAD)


class R3ReviewFinding3Test(unittest.TestCase):
    """Ledger provenance and order are checked before any mutation."""

    def _policy(self) -> AutoIntegrationPolicy:
        return AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )

    def test_a_caller_can_no_longer_author_a_ledger_at_all(self) -> None:
        # R4 review j#96379 finding 1: the run took the ledger as an argument. It reads its
        # own store now, so there is no parameter to forge.
        for name in ("run_integration", "run_cleanup"):
            parameters = set(
                inspect.signature(getattr(AutoIntegrationUseCase, name)).parameters
            )
            self.assertNotIn("ledger", parameters, name)

    def test_an_out_of_order_store_is_refused_before_any_mutation(self) -> None:
        # A tampered store, not a caller argument: a push recorded before any apply. R3's
        # reproduction applied the merge and then reported `integrated` having pushed nothing.
        record = _record()
        store = InMemoryLedgerStore()
        operations = FakeGitOperations()
        use_case = _use_case(operations, integration_policy=self._policy(), ledger=store)
        store.append(
            StepOutcome(
                record.action_key,
                STEP_PUSH,
                OUTCOME_DONE,
                head=MERGE_HEAD,
                recorded_by=use_case.recorder_id,
            )
        )
        report = use_case.run_integration(record)
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)

    def test_a_foreign_provenance_entry_in_the_store_is_not_believed(self) -> None:
        record = _record()
        store = InMemoryLedgerStore()
        store.append(
            StepOutcome(
                record.action_key,
                STEP_PUSH,
                OUTCOME_DONE,
                head=SOURCE,
                recorded_by="somebody-else",
            )
        )
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        _use_case(operations, ledger=store).run_integration(record)
        # The claimed push does not count, so this run performs the push itself.
        self.assertEqual(operations.performed, ["push_non_force"])

    def test_a_merge_push_never_falls_back_to_the_source_head(self) -> None:
        # R3 removed this fallback from the decision and left it in the layer that pushes.
        record = _record()
        operations = FakeGitOperations(
            merge_result=MergeResult(status=MERGE_MERGED, integration_head="")
        )
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(record)
        self.assertNotIn("push_non_force", operations.performed)
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)


class R6ReviewFindingTest(unittest.TestCase):
    """R6 review j#96391's findings, pinned with the conditions that reproduced them."""

    def _merge_policy(self) -> AutoIntegrationPolicy:
        return AutoIntegrationPolicy(
            mode=MODE_AUTO, integration_branch="main", ff_only=False
        )

    def test_f1_a_refused_merge_parent_stops_before_any_push(self) -> None:
        # R6 finding 1: the adapter merged onto whatever the dedicated worktree's local target
        # happened to be, so an extra unreviewed commit there ended up on the integration
        # branch — and the push was accepted because it was still a fast-forward. The merge is
        # built from object ids now, so the parent cannot differ; what is still worth pinning
        # is that a port which refuses stops the run rather than falling through to a push.
        operations = FakeGitOperations(refuse_parent_other_than=OTHER)
        report = _use_case(
            operations, integration_policy=self._merge_policy()
        ).run_integration(_record())
        self.assertEqual(operations.performed, ["apply_merge"])
        self.assertNotIn("push_non_force", operations.performed)
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)

    def test_f1_the_expected_target_is_handed_to_the_apply(self) -> None:
        operations = FakeGitOperations()
        _use_case(operations, integration_policy=self._merge_policy()).run_integration(
            _record()
        )
        self.assertEqual(
            operations.args_for("apply_merge")[0]["expected_target_head"], TARGET
        )

    def test_f2_the_cleanup_run_completes(self) -> None:
        # R6 finding 2 was a cleanup that could never finish: the identity gate kept demanding
        # facts about a path the removal had just deleted. Both the removal and the phase-aware
        # re-measurement it forced are gone (j#96401 finding 1), so what is pinned now is the
        # property the finding was really about — the run reaches `retired` rather than
        # blocking on a fact about itself.
        operations = FakeGitOperations(ancestors=((SOURCE, TARGET),), tip=SOURCE)
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(
            CleanupActionRecord(
                issue="13686",
                lane_generation=3,
                branch=LANE_BRANCH,
                worktree_path=LANE_WORKTREE,
                recorded_source_head=SOURCE,
                integration_action_key=CLEANUP_AUTHORIZING_KEY,
            )
        )
        self.assertEqual(report.final_decision.state, STATE_RETIRED)
        self.assertEqual(operations.performed, [])


class R3ReviewFinding2Test(unittest.TestCase):
    """A cleanup may only touch this actuator's OWN lane worktree and branch."""

    def _record(self, **overrides: object) -> CleanupActionRecord:
        fields: dict = {
            "issue": "13686",
            "lane_generation": 3,
            "branch": LANE_BRANCH,
            "worktree_path": LANE_WORKTREE,
            "recorded_source_head": SOURCE,
            "integration_action_key": CLEANUP_AUTHORIZING_KEY,
        }
        fields.update(overrides)
        return CleanupActionRecord(**fields)  # type: ignore[arg-type]

    def _ops(self, **kwargs: object) -> FakeGitOperations:
        defaults: dict = {"ancestors": ((SOURCE, TARGET),), "tip": SOURCE}
        defaults.update(kwargs)
        return FakeGitOperations(**defaults)  # type: ignore[arg-type]

    def test_a_foreign_lane_is_never_cleaned_up(self) -> None:
        # The R3 reproduction: caller booleans alone removed a foreign lane's worktree and
        # deleted its branch. The actuator now answers "is this ours?" from its own identity
        # (and there is no branch delete left to reach at all).
        operations = self._ops()
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(self._record(
                branch="foreign_lane_branch",
                worktree_path="/foreign/registered/worktree",
            ))
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_our_own_lane_runs_the_one_step(self) -> None:
        operations = self._ops()
        processes = FakeProcessOperations()
        report = _use_case(operations, processes=processes).run_cleanup(self._record())
        self.assertEqual(report.final_decision.state, STATE_RETIRED)
        self.assertEqual([o.step for o in report.outcomes], [STEP_PROCESS_RETIRE])
        self.assertEqual(len(processes.calls), 1)

    def test_the_cleanup_half_asks_the_git_port_for_nothing_at_all(self) -> None:
        # The strongest form of the three withdrawals (j#96344 / j#96396 / j#96401, each
        # finding 1): whatever the world looks like and however the run ends, `run_cleanup`
        # makes no call on the Git port — not a mutation, and not even a read probe, since
        # every probe existed to gate a step that no longer exists.
        for kwargs in ({}, {"lane_clean": False}, {"ancestors": ()}, {"git_workspace": False}):
            operations = self._ops(**kwargs)
            report = _use_case(
                operations, processes=FakeProcessOperations()
            ).run_cleanup(self._record())
            self.assertEqual(operations.calls, [], kwargs)
            for outcome in report.outcomes:
                for retired in ("branch -D", "compare-and-swap", "worktree remove"):
                    self.assertNotIn(retired, outcome.detail)

    def test_no_authority_reader_means_the_cleanup_is_refused(self) -> None:
        operations = self._ops()
        report = _use_case(
            operations, processes=FakeProcessOperations(), authority=None
        ).run_cleanup(self._record())
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_a_non_git_lane_releases_the_process_and_touches_no_git(self) -> None:
        operations = self._ops(git_workspace=False)
        processes = FakeProcessOperations()
        report = _use_case(operations, processes=processes).run_cleanup(self._record())
        self.assertEqual(report.final_decision.state, STATE_RETIRED)
        self.assertEqual(operations.performed, [])
        self.assertEqual(len(processes.calls), 1)

    def test_the_reachability_target_is_not_a_caller_argument(self) -> None:
        # R4 review j#96379 finding 2: `target_ref` was a caller argument, so passing the
        # lane's OWN branch made reachability trivially true and the destructive steps ran on
        # unintegrated work. It comes from the configured integration branch now.
        for name in ("run_cleanup",):
            parameters = set(
                inspect.signature(getattr(AutoIntegrationUseCase, name)).parameters
            )
            self.assertNotIn("target_ref", parameters, name)

    def test_an_unconfirmed_integration_is_what_stops_the_run(self) -> None:
        # R4 finding 2 pinned this through branch REACHABILITY: with no configured integration
        # branch the delete could not establish that the lane's work survived, so it blocked.
        # Both the delete and the removal are gone, and reachability went with them — it was
        # their condition. What gates the surviving step is the durable authority, so that is
        # pinned directly rather than left implied by a retired probe.
        operations = self._ops()
        report = _use_case(
            operations,
            processes=FakeProcessOperations(),
            authority=FakeAuthorityReader(
                cleanup=CleanupAuthority(
                    issue_closed=True,
                    integration_confirmed=False,
                    integration_ci_settled_green=True,
                    callbacks_drained=True,
                    owner_gates_resolved=True,
                    authorizing_action_key=CLEANUP_AUTHORIZING_KEY,
                )
            ),
        ).run_cleanup(self._record())
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)

        # And an actuator with no configured integration branch cleans up normally: nothing
        # in this half reads a branch, a ref or a path any more.
        unconfigured = self._ops()
        rested = _use_case(
            unconfigured,
            processes=FakeProcessOperations(),
            integration_policy=AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch=None),
        ).run_cleanup(self._record())
        self.assertEqual(unconfigured.performed, [])
        self.assertEqual(rested.final_decision.state, STATE_RETIRED)

    def test_a_record_naming_another_lanes_branch_is_refused(self) -> None:
        # R4 finding 2's second reproduction removed a registered worktree that held somebody
        # else's branch. The removal is gone, but the identity question survives it: the record
        # must name BOTH this actuator's worktree and its branch, or no process is released.
        operations = self._ops()
        processes = FakeProcessOperations()
        report = _use_case(operations, processes=processes).run_cleanup(
            self._record(branch="SOME_FOREIGN_BRANCH")
        )
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(processes.calls, [])

    def test_the_port_offers_no_destructive_operation_at_all(self) -> None:
        for gone in ("delete_remote_branch", "delete_local_branch", "remove_worktree"):
            self.assertFalse(hasattr(FakeGitOperations(), gone), gone)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
