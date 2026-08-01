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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    AutoIntegrationGitOperations,
    AutoIntegrationUseCase,
    CleanupAuthority,
    InMemoryLedgerStore,
    LedgerStore,
    DurableAuthorityReader,
    IntegrationAuthority,
    ManagedProcessOperations,
    MergeResult,
    PushResult,
    cleanup_policy_from_config,
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
    IntegrationWorktree,
    StepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.retirement_cleanup_policy import (
    STATE_CLEANUP_BLOCKED,
    STATE_RETIRED,
    STEP_LOCAL_BRANCH_DELETE,
    STEP_PROCESS_RETIRE,
    STEP_WORKTREE_REMOVE,
    CleanupActionRecord,
    RetirementCleanupPolicy,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (
    AutoIntegrationConfig,
)

SOURCE = "a" * 40
TARGET = "b" * 40
MERGE_HEAD = "d" * 40
OTHER = "e" * 40

LANE_WORKTREE = "/lane"
LANE_BRANCH = "lane_br"
DEDICATED = "/dedicated"
#: What the use case stamps on the outcomes it records (and therefore trusts on resume).
#: The receipt is per-instance and unguessable, so tests seed a ledger by driving
#: the actuator rather than by authoring entries (which is the point of the fix).

#: The read-only probes. Calling one is not a side effect, so the zero-side-effect assertions
#: look at mutations only.
_READ_PROBES = (
    "describe_integration_worktree",
    "is_git_workspace",
    "resolve_head",
    "remote_branch_tip",
    "is_ancestor",
    "worktree_dirty",
    "commit_on_remote",
    "branch_tip",
    "branch_checked_out_elsewhere",
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
    dedicated_worktree: Optional[IntegrationWorktree] = None
    on_remote: bool = True
    tip: str = SOURCE
    checked_out_elsewhere: bool = False

    merge_result: MergeResult = field(
        default_factory=lambda: MergeResult(conflicted=False, integration_head=MERGE_HEAD)
    )
    push_result: PushResult = field(default_factory=lambda: PushResult(accepted=True))
    worktree_removed: bool = True
    branch_deleted: bool = True
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

    def branch_tip(self, branch: str) -> str:
        self.calls.append(("branch_tip", {"branch": branch}))
        return self.tip

    def branch_checked_out_elsewhere(self, branch: str) -> bool:
        self.calls.append(("branch_checked_out_elsewhere", {"branch": branch}))
        return self.checked_out_elsewhere

    def describe_integration_worktree(
        self, *, path: str, lane_worktree: str
    ) -> IntegrationWorktree:
        self.calls.append(
            (
                "describe_integration_worktree",
                {"path": path, "lane_worktree": lane_worktree},
            )
        )
        if path == DEDICATED:
            return self.dedicated_worktree or IntegrationWorktree(
                path=path,
                registered=True,
                is_lane_worktree=False,
                clean=True,
                checked_out_branch="main",
            )
        return IntegrationWorktree(
            path=path,
            registered=self.lane_registered,
            is_lane_worktree=(path == lane_worktree),
            clean=self.lane_clean,
            checked_out_branch=self.lane_branch_checked_out,
        )

    # -- mutations --------------------------------------------------------
    def apply_merge(
        self, *, source_head: str, target_ref: str, integration_worktree: str
    ) -> MergeResult:
        self.calls.append(
            (
                "apply_merge",
                {
                    "source_head": source_head,
                    "target_ref": target_ref,
                    "integration_worktree": integration_worktree,
                },
            )
        )
        return self.merge_result

    def push_non_force(self, *, source_head: str, target_ref: str) -> PushResult:
        self.calls.append(
            ("push_non_force", {"source_head": source_head, "target_ref": target_ref})
        )
        return self.push_result

    def remove_worktree(self, *, worktree_path: str) -> bool:
        self.calls.append(("remove_worktree", {"worktree_path": worktree_path}))
        return self.worktree_removed

    def delete_local_branch(self, *, branch: str, expected_tip: str) -> bool:
        self.calls.append(
            ("delete_local_branch", {"branch": branch, "expected_tip": expected_tip})
        )
        return self.branch_deleted

    @property
    def performed(self) -> List[str]:
        """The MUTATIONS this port was asked for, in order."""
        return [name for name, _ in self.calls if name not in _READ_PROBES]

    def args_for(self, name: str) -> List[dict]:
        return [args for called, args in self.calls if called == name]


@dataclass
class FakeAuthorityReader:
    """Supplies the durable-record facts no git probe can answer."""

    authority: IntegrationAuthority = field(
        default_factory=lambda: IntegrationAuthority(
            review_generation_admissible=True,
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
        )
    )
    withhold_ci: bool = False

    def read_integration_authority(self, *, record) -> IntegrationAuthority:
        return self.authority

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
        "integration_worktree_path": DEDICATED,
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

    def test_cleanup_fields_map_through_with_no_remote_delete(self) -> None:
        policy = cleanup_policy_from_config(AutoIntegrationConfig.default())
        self.assertTrue(policy.remove_worktree)
        self.assertTrue(policy.delete_local_branch)
        self.assertFalse(hasattr(policy, "delete_remote_branch"))

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

    def test_foreignness_is_answered_from_the_actuator_s_own_lane_branch(self) -> None:
        # The lane checkout must be a registered worktree holding THIS actuator's branch.
        operations = FakeGitOperations(
            ancestors=_ff_ancestors(), lane_branch_checked_out="somebody_elses_branch"
        )
        report = _use_case(operations).run_integration(_record())
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_the_probes_are_asked_about_the_actuator_s_own_paths(self) -> None:
        operations = FakeGitOperations(ancestors=_ff_ancestors())
        _use_case(operations).run_integration(_record())
        probed = {
            args["path"] for args in operations.args_for("describe_integration_worktree")
        }
        self.assertEqual(probed, {LANE_WORKTREE, DEDICATED})


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

    def test_a_green_exact_sha_ci_completes_a_RESUMED_run(self) -> None:
        # CI on a commit pushed moments ago cannot already be settled, so a fresh run rests
        # at `awaiting_ci`. The SAME actuator resumes from its own ledger and completes.
        record = _record()
        use_case = _use_case(FakeGitOperations(ancestors=_ff_ancestors()))
        fresh = use_case.run_integration(record)
        self.assertEqual(fresh.final_decision.state, STATE_AWAITING_CI)
        self.assertEqual(use_case.run_integration(record).final_decision.state,
                         STATE_INTEGRATED)

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
            push_result=PushResult(accepted=False, rejected=True, detail="stale target"),
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

    def test_a_merge_applies_in_the_dedicated_worktree_then_pushes_the_merge_commit(
        self,
    ) -> None:
        operations = FakeGitOperations()  # no ancestry -> not a fast-forward
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        self.assertEqual(operations.performed, ["apply_merge", "push_non_force"])
        self.assertEqual(
            operations.args_for("apply_merge")[0]["integration_worktree"], DEDICATED
        )
        self.assertEqual(
            operations.args_for("push_non_force")[0]["source_head"], MERGE_HEAD
        )
        self.assertEqual(report.integration_head, MERGE_HEAD)

    def test_a_conflict_stops_before_any_push(self) -> None:
        operations = FakeGitOperations(
            merge_result=MergeResult(conflicted=True, detail="conflict in a.py")
        )
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        self.assertEqual(operations.performed, ["apply_merge"])
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)

    def test_a_measured_lane_worktree_never_becomes_the_integration_worktree(self) -> None:
        operations = FakeGitOperations(
            dedicated_worktree=IntegrationWorktree(
                path=DEDICATED, registered=True, is_lane_worktree=True, clean=True
            )
        )
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(_record())
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.final_decision.state, STATE_INTEGRATION_BLOCKED)

    def test_a_resumed_merge_pushes_the_commit_its_own_apply_produced(self) -> None:
        # Run 1 applies and its push is rejected; run 2 resumes from the actuator's own
        # ledger and must use the merge commit recorded there, never the source head.
        record = _record()
        operations = FakeGitOperations(
            push_result=PushResult(accepted=False, rejected=True)
        )
        use_case = _use_case(operations, integration_policy=self._policy())
        use_case.run_integration(record)
        self.assertEqual(operations.performed, ["apply_merge", "push_non_force"])

        operations.push_result = PushResult(accepted=True)
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
            merge_result=MergeResult(conflicted=False, integration_head="")
        )
        report = _use_case(
            operations, integration_policy=self._policy()
        ).run_integration(record)
        self.assertNotIn("push_non_force", operations.performed)
        self.assertEqual(report.outcomes[-1].outcome, OUTCOME_BLOCKED)


class R3ReviewFinding2Test(unittest.TestCase):
    """A cleanup may only touch this actuator's OWN lane worktree and branch."""

    def _record(self, **overrides: object) -> CleanupActionRecord:
        fields: dict = {
            "issue": "13686",
            "lane_generation": 3,
            "branch": LANE_BRANCH,
            "worktree_path": LANE_WORKTREE,
            "recorded_source_head": SOURCE,
            "integration_action_key": "k",
        }
        fields.update(overrides)
        return CleanupActionRecord(**fields)  # type: ignore[arg-type]

    def _ops(self, **kwargs: object) -> FakeGitOperations:
        defaults: dict = {"ancestors": ((SOURCE, TARGET),), "tip": SOURCE}
        defaults.update(kwargs)
        return FakeGitOperations(**defaults)  # type: ignore[arg-type]

    def test_a_foreign_lane_is_never_cleaned_up(self) -> None:
        # The R3 reproduction: caller booleans alone removed a foreign lane's worktree and
        # deleted its branch. The actuator now answers "is this ours?" from its own identity.
        operations = self._ops()
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(self._record(
                branch="foreign_lane_branch",
                worktree_path="/foreign/registered/worktree",
            ))
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_our_own_lane_runs_the_three_steps_in_order(self) -> None:
        operations = self._ops()
        processes = FakeProcessOperations()
        report = _use_case(operations, processes=processes).run_cleanup(self._record())
        self.assertEqual(report.final_decision.state, STATE_RETIRED)
        self.assertEqual(operations.performed, ["remove_worktree", "delete_local_branch"])
        self.assertEqual(
            [o.step for o in report.outcomes],
            [STEP_PROCESS_RETIRE, STEP_WORKTREE_REMOVE, STEP_LOCAL_BRANCH_DELETE],
        )
        self.assertEqual(
            operations.args_for("delete_local_branch")[0],
            {"branch": LANE_BRANCH, "expected_tip": SOURCE},
        )

    def test_a_dirty_lane_worktree_deletes_nothing(self) -> None:
        operations = self._ops(lane_clean=False)
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(self._record())
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)
        self.assertEqual(operations.performed, [])

    def test_a_branch_still_checked_out_is_not_deleted(self) -> None:
        operations = self._ops(checked_out_elsewhere=True)
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(self._record())
        self.assertEqual(operations.performed, ["remove_worktree"])
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)

    def test_a_branch_not_reachable_from_the_target_is_not_deleted(self) -> None:
        operations = self._ops(ancestors=())  # no ancestry -> not integrated
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(self._record())
        self.assertEqual(operations.performed, ["remove_worktree"])
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)

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

    def test_no_configured_target_means_reachability_is_never_established(self) -> None:
        operations = self._ops()
        report = _use_case(
            operations,
            processes=FakeProcessOperations(),
            integration_policy=AutoIntegrationPolicy(mode=MODE_AUTO, integration_branch=None),
        ).run_cleanup(self._record())
        self.assertEqual(operations.performed, ["remove_worktree"])
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)

    def test_a_worktree_holding_a_foreign_branch_is_never_cleaned_up(self) -> None:
        # R4 finding 2's second reproduction: the probe reported `checked_out_branch` and the
        # gate ignored it, so a registered worktree holding somebody else's branch was removed.
        operations = self._ops(lane_branch_checked_out="SOME_FOREIGN_BRANCH")
        report = _use_case(operations, processes=FakeProcessOperations()).run_cleanup(
            self._record()
        )
        self.assertEqual(operations.performed, [])
        self.assertEqual(report.final_decision.state, STATE_CLEANUP_BLOCKED)

    def test_the_port_cannot_delete_a_remote_ref_at_all(self) -> None:
        self.assertFalse(hasattr(FakeGitOperations(), "delete_remote_branch"))
        self.assertFalse(
            hasattr(RetirementCleanupPolicy.default(), "delete_remote_branch")
        )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
