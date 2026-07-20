"""Integration: the high-level isolated shared-space smoke harness (Redmine #14187).

End-to-end coverage that wires multiple real collaborators — a temp filesystem, the
workspace registry / SQLite fences, the production ``prepare_session``, and the shared
``FakeHerdr`` — so it lives under ``tests/integration/`` per the test-placement policy
(``vibes/docs/logics/tests-placement-discovery-policy.md``; review j#83870 F4). The
pure-helper unit tests stay in ``tests/unit/...``.

Proves the #14185 blocker is resolved AND the R1 review j#83870 findings are closed:
- F1: a split-brain (ambient home ≠ isolated home) fails closed with zero operator-home write;
- F2: a crashed concurrent worker does not vanish and cannot yield a false ``converged``;
- F3: an unreadable inventory during residue verification fails closed (never residue-0).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from support.herdr_fake import FakeHerdr  # noqa: E402

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_harness import (  # noqa: E402,E501
    PHASE_WORKER_ERROR,
    ProjectSmokeObservation,
    SharedSpaceSmokeError,
    SharedSpaceSmokeHarness,
    SharedSpaceSmokeObservation,
    SmokeIsolationError,
    _ProjectSpec,
    _count_duplicate_agents,
    isolated_smoke_home,
    smoke_shared_space_preflight,
)


def _make_env(bindir: Path) -> "dict[str, str]":
    """A trusted launch env: a fake herdr binary + a PATH of provider stubs.

    Mirrors the session-start test env (a fake executable satisfies
    ``resolve_herdr_binary`` while the injected fake ``runner`` never executes it; the
    provider stubs satisfy the launch preflight). The caller supplies this env — the
    harness never resolves a binary itself. No secret-shaped literal.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("herdr", "claude", "codex"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return {"MOZYO_HERDR_BINARY": str(bindir / "herdr"), "PATH": str(bindir)}


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
            self.assertEqual(harness.recorder.coordinators_create_count, 1)

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
                self.assertEqual(
                    {o.coordinators_workspace_id for o in observations},
                    {created[0].coordinators_workspace_id},
                )
                self.assertEqual(harness.recorder.coordinators_create_count, 1)
                self.assertEqual(_count_duplicate_agents(observations), 0)

    def test_smoke_cleans_up_with_zero_residue(self) -> None:
        # Acceptance 5: the whole smoke tears down by exact identity, residue=0.
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            self.assertIsInstance(summary, SharedSpaceSmokeObservation)
            self.assertEqual(summary.coordinators_create_count, 1)
            self.assertEqual(summary.duplicate_agents, 0)
            self.assertTrue(summary.all_projects_completed)
            self.assertTrue(summary.converged)
            self.assertTrue(summary.residue_verified)
            self.assertEqual(summary.residue_workspaces, 0)
            self.assertEqual(summary.residue_agents, 0)
            self.assertTrue(summary.residue_clear)
            self.assertEqual(fx.fake.workspace_ids, [])

    def test_smoke_observes_lock_engaged_and_released(self) -> None:
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            self.assertTrue(summary.lock_engaged)
            self.assertTrue(summary.lock_released_clean)

    def test_evidence_summary_is_redaction_safe(self) -> None:
        # Acceptance 4/6: counts + closed phase tokens only — no home path, no env value.
        with _HarnessFixture() as fx:
            summary = fx.harness().smoke(fx.specs(2))
            evidence = summary.as_evidence()
            blob = repr(evidence)
            self.assertNotIn(str(fx.home), blob)
            self.assertNotIn(str(fx.tmp), blob)
            self.assertNotIn("MOZYO_HERDR_BINARY", blob)
            self.assertEqual(evidence["coordinators_create_count"], 1)
            self.assertEqual(evidence["requested_projects"], 2)
            self.assertEqual(evidence["completed_projects"], 2)
            self.assertTrue(evidence["converged"])
            self.assertTrue(evidence["residue_clear"])

    def test_second_sequential_project_adopts_existing_space(self) -> None:
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

    def test_preexisting_coordinators_space_fails_closed_before_create(self) -> None:
        # Acceptance 5 (herdr dimension): a workspace already labelled `coordinators`
        # makes the smoke refuse BEFORE any create.
        with _HarnessFixture() as fx:
            fx.fake.run(["herdr", "workspace", "create", "--label", "coordinators"])
            harness = fx.harness()
            with self.assertRaises(SharedSpaceSmokeError):
                harness.smoke(fx.specs(2))
            self.assertEqual(harness.recorder.coordinators_create_count, 0)


class IsolationBindingTests(unittest.TestCase):
    """F1: actuation is bound to the isolated home; a split-brain fails closed."""

    def test_ambient_operator_home_fails_closed_with_zero_write(self) -> None:
        # review j#83870 F1: construct the harness with an isolated `home` while the
        # AMBIENT MOZYO_BRIDGE_HOME still points at the operator home (i.e. NOT inside
        # `isolated_smoke_home`). The mutating entry must refuse before any write, so
        # the operator home gains no registry / lock / attestation artifact.
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "operator"
            operator.mkdir()
            isolated = Path(tmp) / "isolated"
            isolated.mkdir()
            env = _make_env(Path(tmp) / "bin")
            repo = Path(tmp) / "proj"
            repo.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(operator)}, clear=False):
                harness = SharedSpaceSmokeHarness(
                    home=isolated, runner=FakeHerdr().run, env=env
                )
                with self.assertRaises(SmokeIsolationError):
                    harness.run_project(_ProjectSpec("p0", repo))
                with self.assertRaises(SmokeIsolationError):
                    harness.preflight_clean_slate()
            # The operator home is untouched: no registry, no single-flight lock, no
            # attestation store were created there.
            operator_artifacts = list(operator.rglob("*"))
            self.assertEqual(
                operator_artifacts, [], f"operator home was written: {operator_artifacts!r}"
            )


class ConcurrentFailureTests(unittest.TestCase):
    """F2: a crashed worker cannot vanish or yield a false ``converged``."""

    def test_worker_crash_is_recorded_and_blocks_converged(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            specs = fx.specs(2)
            original = harness.run_project

            def _flaky(spec: _ProjectSpec) -> ProjectSmokeObservation:
                if spec.project_key == "p1":
                    raise RuntimeError("injected unclassified worker crash")
                return original(spec)

            harness.run_project = _flaky  # type: ignore[assignment]
            observations = harness.run_concurrent(specs)
            # The crashed project did NOT vanish — both indices are present.
            self.assertEqual(len(observations), 2)
            failed = [o for o in observations if o.outcome == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].project_key, "p1")
            self.assertEqual(failed[0].failure_phase, PHASE_WORKER_ERROR)
            # An aggregate over these must not claim convergence.
            summary = SharedSpaceSmokeObservation(
                projects=tuple(observations),
                requested_projects=2,
                coordinators_create_count=sum(
                    1 for o in observations if o.created_coordinators_space
                ),
                duplicate_agents=_count_duplicate_agents(observations),
            )
            self.assertFalse(summary.all_projects_completed)
            self.assertFalse(summary.converged)


class ResidueVerificationTests(unittest.TestCase):
    """F3: an unreadable inventory during residue verification fails closed."""

    def test_unreadable_workspace_list_fails_residue_verification(self) -> None:
        with _HarnessFixture() as fx:
            harness = fx.harness()
            created = ProjectSmokeObservation(
                project_key="p0", workspace_id="wa", outcome="created",
                coordinators_workspace_id="w1", launched_names=("mzb1_wa_claude_default",),
            )

            def _unreadable(argv, *a, **k):
                if list(argv[1:3]) == ["workspace", "list"]:
                    return subprocess.CompletedProcess(list(argv), 0, stdout="nope", stderr="")
                return fx.fake.run(argv, *a, **k)

            harness.recorder._inner = _unreadable
            with self.assertRaises(SharedSpaceSmokeError):
                harness.verify_residue([created])

    def test_smoke_records_residue_unverified_on_unreadable_inventory(self) -> None:
        # The whole-smoke path: force `workspace list` unreadable only AFTER cleanup
        # (i.e. once agents have been launched + closed). residue_clear must be False.
        with _HarnessFixture() as fx:
            harness = fx.harness()
            state = {"cleanup_started": False}
            inner = fx.fake.run

            def _rig(argv, *a, **k):
                head = list(argv[1:3])
                if head == ["pane", "close"]:
                    state["cleanup_started"] = True
                if head == ["workspace", "list"] and state["cleanup_started"]:
                    return subprocess.CompletedProcess(list(argv), 0, stdout="nope", stderr="")
                return inner(argv, *a, **k)

            harness.recorder._inner = _rig
            summary = harness.smoke(fx.specs(1))
            self.assertFalse(summary.residue_verified)
            self.assertFalse(summary.residue_clear)


class PreflightSurfaceTests(unittest.TestCase):
    """The read-only `smoke_shared_space_preflight` surface (the CLI's core)."""

    def test_clean_slate_reports_ready_without_actuation(self) -> None:
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
            self.assertEqual(fake.agents, [])

    def test_preexisting_space_fails_closed(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
