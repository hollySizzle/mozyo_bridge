"""High-level isolated shared-space smoke harness (Redmine #14187).

Drives the SAME production ``shared_space`` path (``prepare_session`` under the real
``coordinator_shared_create_lock`` fence + ``_shared_coordinator_target`` resolver)
through the shared in-memory fake (``support.herdr_fake.FakeHerdr``) — no live herdr
binary, no tmux, no SQLite, no real operator home. Proves the #14185 blocker is
resolved: isolation, observation, concurrent single-flight convergence, exact-identity
cleanup with residue=0, and pre-create fail-closed when isolation is unprovable.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

import sys

_SRC = Path(__file__).resolve().parents[4] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parents[3]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from support.herdr_fake import FakeHerdr  # noqa: E402

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E402,E501
    PHASE_LOCK_ACQUIRE,
    PHASE_LOCK_RELEASE,
    PHASE_LAUNCHER_PREFLIGHT,
    PHASE_SESSION_START,
    ProjectSmokeObservation,
    RecordingHerdrRunner,
    SharedSpaceSmokeError,
    SharedSpaceSmokeHarness,
    SharedSpaceSmokeObservation,
    SmokeIsolationError,
    _ProjectSpec,
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


def _make_env(bindir: Path) -> "dict[str, str]":
    """A trusted launch env: a fake herdr binary + a PATH of provider stubs.

    Mirrors the session-start test env (a fake executable satisfies
    ``resolve_herdr_binary`` while the injected fake ``runner`` never executes it; the
    provider stubs satisfy the launch preflight). This is the *caller's* env — the
    harness never resolves a binary itself. Contains no secret-shaped literal.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("herdr", "claude", "codex"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return {"MOZYO_HERDR_BINARY": str(bindir / "herdr"), "PATH": str(bindir)}


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
            with isolated_smoke_home(isolated, operator_home=operator) as home:
                # The operator placement facade file was written into the ISOLATED home.
                text = coordinator_placement_path(home).read_text(encoding="utf-8")
                self.assertIn("shared_space", text)
                # The process home points at the isolated home during the run.
                self.assertEqual(os.environ["MOZYO_BRIDGE_HOME"], str(home))
            # Restored afterwards (no operator-home mutation escapes the context).
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
        # workspace create --label coordinators
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
        # The tape holds no home path, no env value, no full payload.
        blob = repr(
            (recorder.workspace_create_labels, recorder.agent_start_names,
             recorder.pane_close_handles)
        )
        self.assertNotIn("/secret/path", blob)
        self.assertNotIn("SECRET", blob)


class ClassifyFailurePhaseTests(unittest.TestCase):
    """The closed failure-phase vocabulary (Acceptance 4)."""

    def test_lock_release_beats_lock_acquire_subclass(self) -> None:
        # CoordinatorSharedCreateReleaseError IS a CoordinatorSharedCreateLockUnavailable;
        # the release phase must win (it runs AFTER create — R8 j#83633 F1).
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
            _classify_failure_phase(
                HerdrLauncherIncompatibleError("x", reason="r")
            ),
            PHASE_LAUNCHER_PREFLIGHT,
        )
        self.assertEqual(
            _classify_failure_phase(HerdrSessionStartError("plain")),
            PHASE_SESSION_START,
        )


class _HarnessFixture:
    """Set up an isolated smoke home + fake + env for the integration tests."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.operator = tmp / "real"
        self.operator.mkdir()
        self.isolated = tmp / "smoke"
        self.env = _make_env(tmp / "bin")
        self.fake = FakeHerdr(read_text="$ ready\n> ")
        self.tmp = tmp
        self._ctx = isolated_smoke_home(self.isolated, operator_home=self.operator)
        self.home = self._ctx.__enter__()
        return self

    def __exit__(self, *exc):
        self._ctx.__exit__(*exc)
        self._tmp.cleanup()
        return False

    def specs(self, n: int) -> "list[_ProjectSpec]":
        out = []
        for i in range(n):
            repo = self.tmp / f"proj{i}"
            repo.mkdir()
            out.append(_ProjectSpec(f"p{i}", repo))
        return out

    def harness(self) -> SharedSpaceSmokeHarness:
        return SharedSpaceSmokeHarness(
            home=self.home, runner=self.fake.run, env=self.env
        )


class SharedSpaceSmokeIntegrationTests(unittest.TestCase):
    """The end-to-end shared-space smoke through the fake (Acceptance 2/3/4/5)."""

    def test_single_project_creates_labelled_coordinators_space(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            [obs] = harness.run_concurrent(fx.specs(1))
            self.assertEqual(obs.outcome, "created")
            self.assertTrue(obs.coordinators_workspace_id)
            self.assertEqual(sorted(obs.launched_roles), ["claude", "codex"])
            # The workspace was created carrying the exact `coordinators` label.
            self.assertEqual(
                harness.recorder.coordinators_create_count, 1
            )

    def test_two_projects_concurrent_converge_to_one_space(self) -> None:
        # Acceptance 3: 2-process concurrent start -> exactly one `coordinators`
        # workspace created/adopted, zero duplicate agents. Repeated so the race is
        # exercised in both winner orders.
        for _ in range(6):
            with _HarnessFixture() as fx:
                harness = fx.harness()
                observations = harness.run_concurrent(fx.specs(2))
                self.assertEqual(len(observations), 2, "both projects must complete")
                created = [o for o in observations if o.outcome == "created"]
                adopted = [o for o in observations if o.outcome == "adopted"]
                self.assertEqual(len(created), 1, "exactly one project creates the space")
                self.assertEqual(len(adopted), 1, "the other adopts it")
                # Both resolve to the SAME shared herdr workspace.
                self.assertEqual(
                    {o.coordinators_workspace_id for o in observations},
                    {created[0].coordinators_workspace_id},
                )
                self.assertEqual(harness.recorder.coordinators_create_count, 1)
                self.assertEqual(_count_duplicate_agents(observations), 0)

    def test_smoke_cleans_up_with_zero_residue(self) -> None:
        # Acceptance 5: the whole smoke tears down by exact identity, residue=0.
        with _HarnessFixture() as fx:
            harness = fx.harness()
            summary = harness.smoke(fx.specs(2))
            self.assertIsInstance(summary, SharedSpaceSmokeObservation)
            self.assertEqual(summary.coordinators_create_count, 1)
            self.assertEqual(summary.duplicate_agents, 0)
            self.assertTrue(summary.converged)
            self.assertEqual(summary.residue_workspaces, 0)
            self.assertEqual(summary.residue_agents, 0)
            self.assertTrue(summary.residue_clear)
            # The fake's live workspace inventory is empty after cleanup — the shared
            # workspace auto-vanished with its last closed pane.
            self.assertEqual(fx.fake.workspace_ids, [])

    def test_smoke_observes_lock_engaged_and_released(self) -> None:
        # Acceptance 4: the single-flight fence is engaged during the run and free
        # afterwards (released cleanly).
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            self.assertTrue(summary.lock_engaged)
            self.assertTrue(summary.lock_released_clean)

    def test_evidence_summary_is_redaction_safe(self) -> None:
        # Acceptance 4/6: the durable evidence carries counts + closed phase tokens
        # only — no home path, no env value, no raw payload.
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            evidence = summary.as_evidence()
            blob = repr(evidence)
            self.assertNotIn(str(fx.home), blob)
            self.assertNotIn(str(fx.tmp), blob)
            self.assertNotIn("MOZYO_HERDR_BINARY", blob)
            self.assertEqual(evidence["coordinators_create_count"], 1)
            self.assertTrue(evidence["converged"])
            self.assertTrue(evidence["residue_clear"])

    def test_preexisting_coordinators_space_fails_closed_before_create(self) -> None:
        # Acceptance 5 (herdr dimension): a workspace already labelled `coordinators`
        # (a real operator shared space) makes the smoke refuse BEFORE any create — it
        # would otherwise adopt / pollute a live operator space.
        with _HarnessFixture() as fx:
            # Seed a pre-existing labelled coordinators workspace in the herdr instance.
            fx.fake.run(["herdr", "workspace", "create", "--label", "coordinators"])
            harness = fx.harness()
            with self.assertRaises(SharedSpaceSmokeError):
                harness.smoke(fx.specs(2))
            # Zero coordinators create happened (the guard fired before actuation).
            self.assertEqual(harness.recorder.coordinators_create_count, 0)

    def test_unreadable_labels_fail_closed(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            # Force `workspace list` to a non-JSON payload -> labels unreadable.
            def _unreadable(argv, *a, **k):
                import subprocess
                if list(argv[1:3]) == ["workspace", "list"]:
                    return subprocess.CompletedProcess(list(argv), 0, stdout="nope", stderr="")
                return fx.fake.run(argv, *a, **k)

            harness.recorder._inner = _unreadable
            with self.assertRaises(SharedSpaceSmokeError):
                harness.preflight_clean_slate()

    def test_second_sequential_project_adopts_existing_space(self) -> None:
        # Non-concurrent idempotency: a second project reuses the first project's
        # labelled `coordinators` workspace (adopt, not a second create).
        with _HarnessFixture() as fx:
            harness = fx.harness()
            specs = fx.specs(2)
            first = harness.run_project(specs[0])
            second = harness.run_project(specs[1])
            self.assertEqual(first.outcome, "created")
            self.assertEqual(second.outcome, "adopted")
            self.assertEqual(
                first.coordinators_workspace_id, second.coordinators_workspace_id
            )
            self.assertEqual(harness.recorder.coordinators_create_count, 1)


class PreflightSurfaceTests(unittest.TestCase):
    """The read-only `smoke_shared_space_preflight` surface (the CLI's core)."""

    def test_clean_slate_reports_ready_without_actuation(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E501
            smoke_shared_space_preflight,
        )

        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            operator.mkdir()
            env = _make_env(Path(tmp) / "bin")
            fake = FakeHerdr()
            report = smoke_shared_space_preflight(
                Path(tmp) / "smoke", runner=fake.run, env=env, projects=3,
                operator_home=operator,
            )
            self.assertTrue(report["isolated_home_ok"])
            self.assertTrue(report["clean_slate_ok"])
            self.assertEqual(report["mode"], "shared_space")
            self.assertEqual(report["projects"], 3)
            self.assertFalse(report["actuated"])
            # No agent was launched (read-only preflight).
            self.assertEqual(fake.agents, [])

    def test_preexisting_space_fails_closed(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E501
            smoke_shared_space_preflight,
        )

        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "real"
            operator.mkdir()
            env = _make_env(Path(tmp) / "bin")
            fake = FakeHerdr()
            fake.run(["herdr", "workspace", "create", "--label", "coordinators"])
            with self.assertRaises(SharedSpaceSmokeError):
                smoke_shared_space_preflight(
                    Path(tmp) / "smoke", runner=fake.run, env=env,
                    operator_home=operator,
                )

    def test_cli_render_names_readiness(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_cli import (  # noqa: E501
            _render_text,
        )

        text = _render_text(
            {
                "isolated_home_ok": True, "clean_slate_ok": True,
                "mode": "shared_space", "projects": 2,
                "coordinators_create_expected": 1, "actuated": False,
            }
        )
        self.assertIn("clean_slate_ok=True", text)
        self.assertIn("actuated=False", text)


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
