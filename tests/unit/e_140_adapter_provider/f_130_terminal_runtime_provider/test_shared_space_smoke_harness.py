"""Unit: pure helpers of the isolated shared-space smoke harness (Redmine #14187).

Single-subject, no multi-collaborator / temp-DB wiring — the end-to-end harness
coverage (production ``prepare_session`` + registry SQLite + shared fake) lives in
``tests/integration/e_140_adapter_provider/f_130_terminal_runtime_provider/`` per the
test-placement policy (review j#83870 F4).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[4] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parents[3]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from support.herdr_fake import FakeHerdr  # noqa: E402

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E402,E501
    PHASE_LAUNCHER_PREFLIGHT,
    PHASE_LOCK_ACQUIRE,
    PHASE_LOCK_RELEASE,
    PHASE_SESSION_START,
    ProjectSmokeObservation,
    RecordingHerdrRunner,
    SharedSpaceSmokeObservation,
    SmokeIsolationError,
    _classify_failure_phase,
    _count_duplicate_agents,
    isolated_smoke_home,
    prove_smoke_isolation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E402,E501
    HerdrSessionStartError,
    SHARED_COORDINATOR_WORKSPACE_LABEL,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E402,E501
    HerdrLauncherIncompatibleError,
)
from mozyo_bridge.core.state.coordinator_placement_fence import (  # noqa: E402
    CoordinatorSharedCreateLockUnavailable,
    CoordinatorSharedCreateReleaseError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.coordinator_placement_loader import (  # noqa: E402,E501
    coordinator_placement_path,
)


class ProveSmokeIsolationTests(unittest.TestCase):
    """The pre-actuation cleanup-authority gate (Acceptance 5)."""

    def test_distinct_home_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            isolated = Path(tmp) / "smoke"
            resolved = prove_smoke_isolation(isolated, operator_home=operator)
            self.assertEqual(resolved, isolated.resolve())

    def test_same_home_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with self.assertRaises(SmokeIsolationError):
                prove_smoke_isolation(home, operator_home=home)

    def test_smoke_home_inside_operator_home_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            nested = operator / "smoke"
            with self.assertRaises(SmokeIsolationError):
                prove_smoke_isolation(nested, operator_home=operator)

    def test_operator_home_inside_smoke_home_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smoke = Path(tmp) / "smoke"
            operator = smoke / "real"
            with self.assertRaises(SmokeIsolationError):
                prove_smoke_isolation(smoke, operator_home=operator)


class IsolatedSmokeHomeTests(unittest.TestCase):
    """The isolation context manager writes the facade file and restores the env."""

    def test_writes_placement_file_and_restores_env(self) -> None:
        prior = os.environ.get("MOZYO_BRIDGE_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            operator.mkdir()
            isolated = Path(tmp) / "smoke"
            with isolated_smoke_home(isolated, operator_home=operator) as capability:
                # Yields a verified IsolationCapability (review j#83905 F1), not a bare path.
                self.assertEqual(capability.operator_home, operator.resolve())
                home = capability.isolated_home
                text = coordinator_placement_path(home).read_text(encoding="utf-8")
                self.assertIn("shared_space", text)
                self.assertEqual(os.environ["MOZYO_BRIDGE_HOME"], str(home))
            self.assertEqual(os.environ.get("MOZYO_BRIDGE_HOME"), prior)

    def test_bad_home_fails_closed_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with self.assertRaises(SmokeIsolationError):
                with isolated_smoke_home(home, operator_home=home):
                    self.fail("must not enter the body when isolation is unprovable")


class RecordingHerdrRunnerTests(unittest.TestCase):
    """The evidence tape is redaction-safe (Acceptance 4/6)."""

    def test_records_kinds_and_identity_tokens_only(self) -> None:
        recorder = RecordingHerdrRunner(FakeHerdr().run)
        binary = "/fake/herdr"
        recorder(
            [binary, "workspace", "create", "--cwd", "/secret/path", "--label",
             SHARED_COORDINATOR_WORKSPACE_LABEL, "--no-focus"],
            capture_output=True, text=True, timeout=5, env={"SECRET": "x"},
        )
        recorder([binary, "workspace", "list"], capture_output=True, text=True, timeout=5, env=None)
        self.assertEqual(
            recorder.workspace_create_labels, [SHARED_COORDINATOR_WORKSPACE_LABEL]
        )
        self.assertEqual(recorder.workspace_list_count, 1)
        self.assertEqual(recorder.coordinators_create_count, 1)
        blob = repr(
            (recorder.workspace_create_labels, recorder.agent_start_names,
             recorder.pane_close_handles)
        )
        self.assertNotIn("/secret/path", blob)
        self.assertNotIn("SECRET", blob)

    def test_captures_actuation_receipts(self) -> None:
        # review j#83905 F2: the tape records the RESULT of successful mutations (the
        # created workspace id + label, the launched pane locator) so cleanup / residue
        # verification never depend on a per-project observation.
        fake = FakeHerdr()
        recorder = RecordingHerdrRunner(fake.run)
        binary = "/fake/herdr"
        recorder(
            [binary, "workspace", "create", "--label", SHARED_COORDINATOR_WORKSPACE_LABEL,
             "--no-focus"],
            capture_output=True, text=True, timeout=5, env={},
        )
        self.assertEqual(recorder.created_coordinators_workspaces, ["w1"])
        recorder(
            [binary, "agent", "start", "mzb1_x_claude_default", "--workspace", "w1",
             "--", "claude"],
            capture_output=True, text=True, timeout=5, env={},
        )
        self.assertEqual(recorder.launched_locators, ["w1:p2"])


class ClassifyFailurePhaseTests(unittest.TestCase):
    """The closed failure-phase vocabulary (Acceptance 4)."""

    def test_lock_release_beats_lock_acquire_subclass(self) -> None:
        self.assertEqual(
            _classify_failure_phase(CoordinatorSharedCreateReleaseError("x")),
            PHASE_LOCK_RELEASE,
        )
        self.assertEqual(
            _classify_failure_phase(CoordinatorSharedCreateLockUnavailable("x")),
            PHASE_LOCK_ACQUIRE,
        )

    def test_wrapped_cause_is_classified(self) -> None:
        wrapped = HerdrSessionStartError("phase-accurate message")
        wrapped.__cause__ = CoordinatorSharedCreateReleaseError("release")
        self.assertEqual(_classify_failure_phase(wrapped), PHASE_LOCK_RELEASE)

    def test_launcher_incompatible_and_default(self) -> None:
        self.assertEqual(
            _classify_failure_phase(HerdrLauncherIncompatibleError("x", reason="r")),
            PHASE_LAUNCHER_PREFLIGHT,
        )
        self.assertEqual(
            _classify_failure_phase(HerdrSessionStartError("plain")),
            PHASE_SESSION_START,
        )


class ObservationAggregateTests(unittest.TestCase):
    """Pure aggregate predicates (`converged` / `residue_clear`) — F2 / F3."""

    def _obs(self, key: str, outcome: str, names=()) -> ProjectSmokeObservation:
        return ProjectSmokeObservation(
            project_key=key, workspace_id=f"w{key}", outcome=outcome,
            coordinators_workspace_id="w1", launched_names=tuple(names),
        )

    def test_converged_requires_all_projects_completed(self) -> None:
        # A single surviving `created` project with create-count 1 / dup 0 is NOT
        # converged when 2 were requested (review j#83870 F2).
        summary = SharedSpaceSmokeObservation(
            projects=(self._obs("0", "created"),),
            requested_projects=2,
            coordinators_create_count=1,
            duplicate_agents=0,
        )
        self.assertFalse(summary.all_projects_completed)
        self.assertFalse(summary.converged)

    def test_converged_false_when_a_project_failed(self) -> None:
        summary = SharedSpaceSmokeObservation(
            projects=(self._obs("0", "created"), self._obs("1", "failed")),
            requested_projects=2,
            coordinators_create_count=1,
            duplicate_agents=0,
        )
        self.assertFalse(summary.converged)

    def test_converged_true_on_complete_run(self) -> None:
        summary = SharedSpaceSmokeObservation(
            projects=(self._obs("0", "created"), self._obs("1", "adopted")),
            requested_projects=2,
            coordinators_create_count=1,
            duplicate_agents=0,
        )
        self.assertTrue(summary.converged)

    def test_residue_clear_requires_verification(self) -> None:
        # residue counts 0 but NOT verified (unreadable inventory) -> not clear (F3).
        complete = (self._obs("0", "created"), self._obs("1", "adopted"))
        unverified = SharedSpaceSmokeObservation(
            projects=complete, requested_projects=2,
            cleanup_attempted=True, residue_verified=False,
            residue_workspaces=0, residue_agents=0,
        )
        self.assertFalse(unverified.residue_clear)
        verified = SharedSpaceSmokeObservation(
            projects=complete, requested_projects=2,
            cleanup_attempted=True, residue_verified=True,
            residue_workspaces=0, residue_agents=0,
        )
        self.assertTrue(verified.residue_clear)

    def test_residue_clear_false_when_a_project_failed(self) -> None:
        # review j#83905 F2: even a receipt-driven residue-0 read is not "clear" while a
        # project failed (its actuation-identity coverage may be incomplete).
        failed = SharedSpaceSmokeObservation(
            projects=(self._obs("0", "created"), self._obs("1", "failed")),
            requested_projects=2, cleanup_attempted=True, residue_verified=True,
            residue_workspaces=0, residue_agents=0,
        )
        self.assertFalse(failed.residue_clear)


class CountDuplicateAgentsTests(unittest.TestCase):
    def test_counts_names_launched_more_than_once(self) -> None:
        a = ProjectSmokeObservation(
            project_key="a", workspace_id="wa", outcome="created",
            coordinators_workspace_id="w1", launched_names=("mzb1_x", "mzb1_y"),
        )
        b = ProjectSmokeObservation(
            project_key="b", workspace_id="wb", outcome="adopted",
            coordinators_workspace_id="w1", launched_names=("mzb1_x",),
        )
        self.assertEqual(_count_duplicate_agents([a, b]), 1)
        self.assertEqual(_count_duplicate_agents([a]), 0)


if __name__ == "__main__":
    unittest.main()
