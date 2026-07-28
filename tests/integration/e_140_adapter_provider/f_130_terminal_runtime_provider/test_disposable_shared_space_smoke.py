"""Real endpoint-bound Herdr smoke with harmless provider stubs (#14187).

Beyond convergence, this file carries the operator-invariance proof the incident
(blocker j#85754) lacked: a **stand-in operator server** is started first, its socket
is planted as the ambient ``HERDR_SOCKET_PATH`` the smoke inherits, and after the
smoke completes the stand-in must still be alive and still answering.  A real
operator's Herdr is never involved — the stand-in is itself a disposable instance the
test owns and tears down (design disposition j#85756: assert operator request
count 0, operator process/state unchanged, owned-child-only cleanup).

That live test starts real Herdr servers and real (harmless stub) provider processes,
so it is **opt-in**: it stays skipped unless ``MOZYO_SMOKE_LIVE_HERDR=1`` is set.  Live
actuation must be a deliberate, approved act — never a side effect of
``unittest discover`` on a machine that also runs the operator's Herdr.

:class:`ForkedGateReceiptTests` below needs no Herdr at all.  It forks real OS
processes to prove the part of the negative proof that only shows up across a process
boundary (review j#85841 F1): a worker's endpoint-gate counters reach the parent solely
through its receipt, and a worker that dies without reporting is counted as *missing*
rather than as a process that made zero requests.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

LIVE_OPT_IN_ENV = "MOZYO_SMOKE_LIVE_HERDR"

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E402,E501
    DisposableHerdrInstance,
    EndpointBoundHerdrRunner,
    EndpointGateCounters,
    EndpointGateEvidence,
    HERDR_SOCKET_PATH_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    disposable_herdr_instance as _lifecycle_module,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    disposable_shared_space_smoke as _driver_module,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_shared_space_smoke import (  # noqa: E402,E501
    _ProjectSpec,
    _run_forked_projects,
    bounded_process_timeout,
    run_disposable_shared_space_smoke,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E402,E501
    ProjectSmokeObservation,
    SharedSpaceSmokeError,
)


@unittest.skipUnless(
    os.environ.get(LIVE_OPT_IN_ENV) == "1",
    f"live Herdr actuation is opt-in; set {LIVE_OPT_IN_ENV}=1 to run it",
)
@unittest.skipUnless(shutil.which("herdr"), "herdr binary is not installed")
class DisposableSharedSpaceLiveIntegrationTests(unittest.TestCase):
    def test_two_processes_converge_and_the_ambient_server_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            base = Path(tmp)
            bindir = base / "bin"
            bindir.mkdir()
            for provider in ("claude", "codex"):
                script = bindir / provider
                script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
                script.chmod(script.stat().st_mode | stat.S_IEXEC)
            herdr = shutil.which("herdr")
            assert herdr is not None
            path = os.pathsep.join(
                [str(bindir), "/usr/local/bin", "/usr/bin", "/bin"]
            )

            # A server that plays the operator's role for this test: the smoke will
            # inherit ITS socket as the ambient endpoint, and it must survive.
            stand_in = DisposableHerdrInstance(
                binary=herdr,
                root=base / "stand-in-operator",
                base_env={"HOME": str(base / "stand-in-home"), "PATH": path},
                ambient_env={},
            )
            with stand_in:
                self.assertTrue(stand_in.process_alive)

                env = dict(os.environ)
                env.update(
                    {
                        "MOZYO_HERDR_BINARY": herdr,
                        # Disable the attestation wrapper for the harmless provider
                        # stubs; production/built-artifact E2E exercises the real one.
                        "MOZYO_BRIDGE_LAUNCHER": str(base / "absent-launcher"),
                        "PATH": path,
                        # The ambient endpoint the smoke inherits. Every request the
                        # smoke makes must be redirected away from it, and the gate
                        # must refuse (never dispatch) anything that is not.
                        "HERDR_SOCKET_PATH": str(stand_in.binding.socket_path),
                    }
                )
                report = run_disposable_shared_space_smoke(
                    base / "smoke-home", env=env, projects=2, process_timeout=20.0
                )
                self.assertTrue(report["success"], report)
                self.assertTrue(report["cross_process"])
                self.assertEqual(report["coordinators_create_count"], 1)
                self.assertEqual(report["duplicate_agents"], 0)
                self.assertTrue(report["residue_clear"])
                self.assertTrue(report["server_stopped"])
                self.assertEqual(report["endpoint_residue"], 0)

                # Load-bearing negative proof, both directions (blocker j#85754):
                # nothing reached the ambient endpoint, and nothing had to be refused.
                self.assertTrue(report["endpoint_bound"], report)
                self.assertEqual(report["operator_endpoint_requests"], 0, report)
                self.assertEqual(report["endpoint_escape_refusals"], 0, report)
                self.assertFalse(report["operator_server_connected"], report)
                self.assertFalse(report["graceful_stop_refused"], report)

                # Cross-process SCOPE of that proof (review j#85841 F1): the forked
                # workers, not this process, made the workspace/agent requests, so the
                # zeros above only mean anything once every worker receipt is in.
                self.assertTrue(report["endpoint_gate_receipts_complete"], report)
                self.assertTrue(report["endpoint_gate_receipts_consistent"], report)
                self.assertTrue(report["endpoint_gate_proven_zero_external"], report)
                self.assertEqual(report["endpoint_gate_receipts_missing"], 0, report)
                self.assertEqual(report["endpoint_gate_processes"], 3, report)
                self.assertEqual(report["endpoint_refusal_reasons"], [], report)

                # Operator process/state invariance: still running, still answering,
                # and its own state tree still present.
                self.assertTrue(stand_in.process_alive, "the ambient server was stopped")
                probe = stand_in.runner(
                    [herdr, "workspace", "list"],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertTrue(stand_in.binding.socket_path.exists())

            self.assertTrue(stand_in.stopped)
            self.assertFalse((base / "stand-in-operator").exists())


class _StubRecorder:
    def __init__(self) -> None:
        self.launched_locators: list = []
        self.created_workspaces: dict = {}
        self.agent_start_names: list = []
        self.coordinators_create_count = 0


class _StubHarness:
    """The seam ``_run_forked_projects`` actually uses — no Herdr, no isolation home."""

    def __init__(self, *, calls: int = 0, die: bool = False, gate_runner=None) -> None:
        self.recorder = _StubRecorder()
        self._calls = calls
        self._die = die
        self._gate_runner = gate_runner

    def run_project(self, spec) -> ProjectSmokeObservation:
        for _ in range(self._calls):
            self._gate_runner(["herdr", "workspace", "list"], env={})
        if self._die:
            # Not an exception: the worker vanishes without ever reporting, which is
            # the case the parent must not read as "this process made zero requests".
            os._exit(9)
        return ProjectSmokeObservation(
            project_key=spec.project_key,
            workspace_id="w1",
            outcome="created",
            coordinators_workspace_id="w1",
        )


class ForkedGateReceiptTests(unittest.TestCase):
    """Real ``fork``, no Herdr: the receipt is the only channel for gate counters."""

    def _gate_runner(self, root: Path) -> EndpointBoundHerdrRunner:
        binding = _lifecycle_module.DisposableHerdrBinding(
            root=root,
            socket_path=root / "herdr.sock",
            client_socket_path=root / "herdr-client.sock",
            config_path=root / "config.toml",
        )
        capability = _lifecycle_module._mint_owned_endpoint(binding, os.getpid())
        return EndpointBoundHerdrRunner(
            lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0, "", ""),
            capability_provider=lambda: capability,
            binding_env={HERDR_SOCKET_PATH_ENV: str(root / "herdr.sock")},
            agent_env={},
        )

    def _drive(self, harness_specs) -> tuple:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "owned"
            gate_runner = self._gate_runner(root)
            harnesses = [
                _StubHarness(gate_runner=gate_runner, **kwargs) for kwargs in harness_specs
            ]
            specs = [
                _ProjectSpec(f"p{index}", Path(tmp) / f"p{index}")
                for index in range(len(harness_specs))
            ]
            for spec in specs:
                spec.repo_root.mkdir(parents=True, exist_ok=True)
            forked = _run_forked_projects(
                harnesses=harnesses, specs=specs, timeout=20.0, gate_runner=gate_runner
            )
            gate = EndpointGateEvidence.aggregate(
                parent=EndpointGateCounters.snapshot(gate_runner),
                worker_receipts=[r.endpoint_gate for r in forked.receipts],
            )
            return forked, gate, gate_runner

    def test_worker_counters_reach_the_parent_through_the_receipt(self) -> None:
        forked, gate, gate_runner = self._drive([{"calls": 2}, {"calls": 3}])

        self.assertEqual(gate_runner.dispatched_calls, 0, "the parent dispatched nothing")
        self.assertEqual(forked.orphaned_workers, 0, "a clean round leaves no worker")
        self.assertTrue(all(r.endpoint_gate is not None for r in forked.receipts))
        self.assertEqual(gate.receipts_missing, 0)
        self.assertTrue(gate.receipts_complete)
        self.assertEqual(
            gate.dispatched_calls,
            5,
            "without the receipts the parent would have reported 0 for all of this",
        )
        self.assertTrue(gate.proven_zero_external)
        self.assertTrue(gate.all_calls_bound)

    def test_a_worker_that_dies_without_reporting_is_counted_as_missing(self) -> None:
        forked, gate, _runner = self._drive([{"calls": 1}, {"calls": 1, "die": True}])

        self.assertIsNone(forked.receipts[1].endpoint_gate)
        self.assertEqual(forked.receipts[1].observation.outcome, "failed")
        self.assertEqual(gate.receipts_expected, 2)
        self.assertEqual(gate.receipts_missing, 1)
        self.assertFalse(gate.receipts_complete)
        self.assertFalse(
            gate.proven_zero_external,
            "a lost snapshot must not read as a proven-zero process",
        )


class WorkerTimeoutAndCleanupTests(unittest.TestCase):
    """A caller must not be able to strand owned workers (review j#91604 F2).

    ``float('inf')`` reached ``Process.join``, raised ``OverflowError`` *after* the
    whole fleet had started, and unwound the driver with the workers still running —
    while the caller went on to shut the server and the owned root down.
    """

    def _live_smoke_workers(self) -> list:
        return [
            child
            for child in multiprocessing.active_children()
            if child.name.startswith("mozyo-smoke-")
        ]

    def setUp(self) -> None:
        self.addCleanup(self._reap_leftovers)

    def _reap_leftovers(self) -> None:
        """Safety net so a failing assertion never leaks a process into the suite."""
        for child in self._live_smoke_workers():
            child.terminate()
            child.join(timeout=10)

    def _fleet(self, tmp: Path, count: int = 2):
        gate_runner = _gate_runner_for(tmp / "owned")
        harnesses = [_SleepHarness() for _ in range(count)]
        specs = []
        for index in range(count):
            repo = tmp / "projects" / f"p{index}"
            repo.mkdir(parents=True, exist_ok=True)
            specs.append(_ProjectSpec(f"p{index}", repo))
        return gate_runner, harnesses, specs

    def test_an_unusable_timeout_is_refused_before_any_process_exists(self) -> None:
        for value in (float("inf"), float("-inf"), float("nan"), 0.0, -1.0, 10_000.0):
            with tempfile.TemporaryDirectory() as tmp:
                gate_runner, harnesses, specs = self._fleet(Path(tmp))
                with self.assertRaises(SharedSpaceSmokeError, msg=repr(value)):
                    _run_forked_projects(
                        harnesses=harnesses, specs=specs,
                        timeout=value, gate_runner=gate_runner,
                    )
                self.assertEqual(
                    self._live_smoke_workers(), [],
                    f"{value!r} must be refused before a worker is ever started",
                )

    def test_a_usable_timeout_still_runs(self) -> None:
        """Baseline: the domain check must not reject the values the smoke uses."""
        self.assertEqual(bounded_process_timeout(45.0), 45.0)
        self.assertEqual(bounded_process_timeout("20"), 20.0)
        with tempfile.TemporaryDirectory() as tmp:
            gate_runner, harnesses, specs = self._fleet(Path(tmp), count=1)
            forked = _run_forked_projects(
                harnesses=harnesses, specs=specs, timeout=1.0, gate_runner=gate_runner
            )
            self.assertEqual(forked.orphaned_workers, 0)
            self.assertEqual(self._live_smoke_workers(), [])

    def test_a_failure_after_start_still_reaps_every_owned_worker(self) -> None:
        """The property the missing ``finally`` cost: cleanup on the exception path."""
        started_names = []

        def _boom(*, processes, started, specs, output, timeout):
            for process in processes:
                process.start()
                started.append(process)
                started_names.append(process.name)
            raise RuntimeError("injected failure after the fleet started")

        with tempfile.TemporaryDirectory() as tmp:
            gate_runner, harnesses, specs = self._fleet(Path(tmp))
            with mock.patch.object(
                _driver_module, "_collect_forked_receipts", _boom
            ):
                forked = _run_forked_projects(
                    harnesses=harnesses, specs=specs,
                    timeout=20.0, gate_runner=gate_runner,
                )
            self.assertEqual(len(started_names), 2, "premise lost: no workers started")
            self.assertEqual(
                self._live_smoke_workers(), [],
                "owned workers outlived the driver's exception path",
            )
            # The verdict the exception used to discard (review j#91638 F1).
            self.assertTrue(forked.round_failed)
            self.assertEqual(forked.failure_kind, "RuntimeError")
            self.assertEqual(forked.orphaned_workers, 0)
            self.assertTrue(forked.workers_contained)
            self.assertEqual(
                len(forked.receipts), 2, "every project must still carry a receipt"
            )
            self.assertTrue(
                all(r.endpoint_gate is None for r in forked.receipts),
                "an unreported round must count as MISSING gate snapshots, not zeros",
            )
            self.assertTrue(
                all(r.observation.outcome == "failed" for r in forked.receipts)
            )

    def test_an_uncontained_survivor_withholds_the_owned_root(self) -> None:
        """The fence itself: teardown consults the verdict instead of scoring it.

        A ``_ForkedRun`` that reports a survivor must leave the socket path in place,
        because a worker still holding client-call capability could address whatever
        binds it next.
        """
        for orphans, expect_released in ((1, False), (-1, False), (0, True)):
            with self.subTest(orphans=orphans):
                run = _driver_module._ForkedRun(receipts=(), orphaned_workers=orphans)
                self.assertEqual(run.workers_contained, expect_released)

                with tempfile.TemporaryDirectory() as tmp:
                    process = _FakeOwnedProcess()
                    instance = DisposableHerdrInstance(
                        binary="/bin/true",
                        root=Path(tmp) / "instance",
                        base_env={"HOME": str(Path(tmp) / "operator")},
                        runner=lambda argv, **k: subprocess.CompletedProcess(
                            argv, 0, "[]", ""
                        ),
                        popen_factory=lambda argv, **k: process,
                        sleeper=lambda _s: None,
                        ambient_env={},
                    )
                    instance.start()
                    root = instance.root
                    instance.shutdown(release_root=run.workers_contained)
                    self.assertEqual(
                        root.exists(),
                        not expect_released,
                        "root removal must follow the containment verdict",
                    )
                    self.assertEqual(
                        instance.as_evidence()["owned_root_released"], expect_released
                    )


class _FakeOwnedProcess:
    def __init__(self) -> None:
        self.pid = 717171
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _gate_runner_for(root: Path) -> EndpointBoundHerdrRunner:
    binding = _lifecycle_module.DisposableHerdrBinding(
        root=root,
        socket_path=root / "herdr.sock",
        client_socket_path=root / "herdr-client.sock",
        config_path=root / "config.toml",
    )
    capability = _lifecycle_module._mint_owned_endpoint(binding, os.getpid())
    return EndpointBoundHerdrRunner(
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0, "", ""),
        capability_provider=lambda: capability,
        binding_env={HERDR_SOCKET_PATH_ENV: str(root / "herdr.sock")},
        agent_env={},
    )


class _SleepHarness:
    """A worker that stays alive well past any bounded join."""

    def __init__(self) -> None:
        self.recorder = _StubRecorder()

    def run_project(self, spec) -> ProjectSmokeObservation:
        import time

        time.sleep(30)
        return ProjectSmokeObservation(
            project_key=spec.project_key, workspace_id="w1",
            outcome="created", coordinators_workspace_id="w1",
        )


if __name__ == "__main__":
    unittest.main()
