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


class PartialReceiptRetentionTests(unittest.TestCase):
    """A late collection failure must not take the receipts that already landed.

    Wiping the whole set threw away exact pane locators cleanup still needs and gate
    counters a worker had already proven (review j#91687 F3).
    """

    def _specs(self, tmp: Path, count: int = 2):
        specs = []
        for index in range(count):
            repo = tmp / f"p{index}"
            repo.mkdir(parents=True, exist_ok=True)
            specs.append(_ProjectSpec(f"p{index}", repo))
        return specs

    def _receipt(self, index: int) -> object:
        return _driver_module._ProcessReceipt(
            index=index,
            observation=ProjectSmokeObservation(
                project_key=f"p{index}", workspace_id="w1", outcome="created",
                coordinators_workspace_id="w1",
            ),
            launched_locators=(f"w1:p{index + 3}",),
            endpoint_gate=EndpointGateCounters(2, 2, 0, 0, ()),
        )

    class _Queue:
        """Hands back the given receipts, then fails in the tail of collection."""

        def __init__(self, items, fail_on_close=True):
            self._items = list(items)
            self._fail_on_close = fail_on_close

        def get(self, timeout=None):
            if self._items:
                return self._items.pop(0)
            import queue as _q

            raise _q.Empty

        def close(self):
            if self._fail_on_close:
                raise OSError("injected close failure")

        def join_thread(self):
            pass

    class _InertProcess:
        """Enough of the Process surface for the drain loop; starts nothing."""

        def __init__(self, name: str) -> None:
            self.name = name

        def start(self) -> None:
            pass

        def join(self, timeout=None) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    def test_collected_receipts_survive_a_late_collection_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            specs = self._specs(Path(tmp))
            collected: dict = {}
            processes = [self._InertProcess(f"mozyo-smoke-p{i}") for i in range(2)]
            with self.assertRaises(OSError):
                _driver_module._collect_forked_receipts(
                    processes=processes, started=[], specs=specs,
                    output=self._Queue([self._receipt(0)]),
                    collected=collected, anomalies=[], locator_tape=[], timeout=5.0,
                )
            self.assertIn(0, collected, "the received receipt was thrown away")
            self.assertEqual(collected[0].launched_locators, ("w1:p3",))
            self.assertIsNotNone(collected[0].endpoint_gate)

    def test_only_the_unreported_indexes_become_missing_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            specs = self._specs(Path(tmp))
            filled = _driver_module._fill_unreported(specs, {0: self._receipt(0)})

            self.assertEqual(len(filled), 2)
            # Kept: the exact locator cleanup needs, and the proven gate snapshot.
            self.assertEqual(filled[0].launched_locators, ("w1:p3",))
            self.assertIsNotNone(filled[0].endpoint_gate)
            self.assertEqual(filled[0].observation.outcome, "created")
            # Filled: never reported, so it counts as MISSING, not as a proven zero.
            self.assertEqual(filled[1].launched_locators, ())
            self.assertIsNone(filled[1].endpoint_gate)
            self.assertEqual(filled[1].observation.outcome, "failed")

    def test_the_round_result_carries_the_partial_receipts(self) -> None:
        """End to end through ``_run_forked_projects``: the fix's actual contract."""
        real = _driver_module._collect_forked_receipts

        def _partial(*, processes, started, specs, output, collected,
                     anomalies, locator_tape, timeout):
            collected[0] = self._receipt(0)
            raise OSError("injected after one receipt landed")

        with tempfile.TemporaryDirectory() as tmp:
            specs = self._specs(Path(tmp))
            with mock.patch.object(
                _driver_module, "_collect_forked_receipts", _partial
            ):
                forked = _run_forked_projects(
                    harnesses=[object(), object()], specs=specs,
                    timeout=5.0, gate_runner=None,
                )
            self.assertTrue(forked.round_failed)
            self.assertEqual(forked.failure_kind, "OSError")
            self.assertEqual(
                forked.receipts[0].launched_locators, ("w1:p3",),
                "a failed round must still carry what it had already received",
            )
            self.assertIsNotNone(forked.receipts[0].endpoint_gate)
            self.assertIsNone(forked.receipts[1].endpoint_gate)
        del real


class StartRegistrationGapTests(unittest.TestCase):
    """A child that exists must already be registered for reaping (j#91741 F1).

    Registering after ``start()`` left a two-instruction window in which a
    ``BaseException`` produced a live worker no ``finally`` knew about.
    """

    def test_an_interrupt_inside_start_still_leaves_the_handle_registered(self) -> None:
        state = {"alive": False}

        class _InterruptingProcess:
            name = "mozyo-smoke-p0"

            def start(self):
                state["alive"] = True  # the child exists from here
                raise KeyboardInterrupt("injected between start and registration")

            def is_alive(self):
                return state["alive"]

            def join(self, timeout=None):
                pass

            def terminate(self):
                state["alive"] = False

            def kill(self):
                state["alive"] = False

        started: list = []
        with self.assertRaises(KeyboardInterrupt):
            _driver_module._collect_forked_receipts(
                processes=[_InterruptingProcess()], started=started, specs=[],
                output=None, collected={}, anomalies=[], locator_tape=[], timeout=5.0,
            )
        self.assertTrue(state["alive"], "premise: the child was really created")
        self.assertEqual(len(started), 1, "the live child was not registered for reap")

        survivors = _driver_module._reap_exact_workers(started)
        self.assertFalse(state["alive"], "the registered handle must be reaped")
        self.assertEqual(survivors, 0)

    def test_a_handle_that_never_started_is_harmless_to_the_reap(self) -> None:
        """Baseline: registering first must not make the reap signal a dead handle."""

        class _NeverStarted:
            name = "mozyo-smoke-never"

            def __init__(self):
                self.signalled = False

            def is_alive(self):
                return False

            def join(self, timeout=None):
                pass

            def terminate(self):
                self.signalled = True

            def kill(self):
                self.signalled = True

        handle = _NeverStarted()
        self.assertEqual(_driver_module._reap_exact_workers([handle]), 0)
        self.assertFalse(handle.signalled, "an unstarted handle must not be signalled")


class ReceiptIdentityTests(unittest.TestCase):
    """A worker's self-reported index is checked, not believed (j#91741 F3)."""

    class _Inert:
        name = "mozyo-smoke-x"

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

        def terminate(self):
            pass

        def kill(self):
            pass

    class _Queue:
        def __init__(self, items):
            self._items = list(items)

        def get(self, timeout=None):
            if self._items:
                return self._items.pop(0)
            import queue as _q

            raise _q.Empty

        def close(self):
            pass

        def join_thread(self):
            pass

    def _receipt(self, index, locator, project_key=None):
        return _driver_module._ProcessReceipt(
            index=index,
            observation=ProjectSmokeObservation(
                project_key=project_key or f"p{index}", workspace_id="w1",
                outcome="created", coordinators_workspace_id="w1",
            ),
            launched_locators=(locator,),
            endpoint_gate=EndpointGateCounters(1, 1, 0, 0, ()),
        )

    def _specs(self, tmp: Path, count: int = 2):
        specs = []
        for index in range(count):
            repo = tmp / f"p{index}"
            repo.mkdir(parents=True, exist_ok=True)
            specs.append(_ProjectSpec(f"p{index}", repo))
        return specs

    def _drive(self, tmp: Path, receipts):
        collected: dict = {}
        anomalies: list = []
        tape: list = []
        _driver_module._collect_forked_receipts(
            processes=[self._Inert() for _ in receipts], started=[],
            specs=self._specs(tmp), output=self._Queue(receipts),
            collected=collected, anomalies=anomalies, locator_tape=tape, timeout=5.0,
        )
        return collected, anomalies, tape

    def test_a_duplicate_index_is_refused_and_its_locator_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            collected, anomalies, tape = self._drive(
                Path(tmp), [self._receipt(0, "w1:p1"), self._receipt(0, "w1:p2")]
            )
            self.assertEqual(
                collected[0].launched_locators, ("w1:p1",),
                "the FIRST receipt must survive; it is not silently overwritten",
            )
            self.assertEqual(
                anomalies, [_driver_module.RECEIPT_ANOMALY_DUPLICATE_INDEX]
            )
            self.assertIn(
                "w1:p2", tape, "a refused receipt's pane still has to be cleaned up"
            )

    def test_a_bool_index_is_refused_for_both_values(self) -> None:
        """``bool`` is an ``int`` subclass and ``True`` hashes as ``1``.

        So a ``bool`` index passed the range check and then impersonated project 1 in
        the collected map — a malformed receipt read as a complete, converged round
        (review j#91777). Both values are asserted: ``False`` aliases project 0 just as
        ``True`` aliases project 1.
        """
        for value in (True, False):
            with self.subTest(index=value):
                with tempfile.TemporaryDirectory() as tmp:
                    collected, anomalies, tape = self._drive(
                        Path(tmp),
                        [self._receipt(value, "w1:p8", project_key="p1" if value else "p0")],
                    )
                    self.assertEqual(
                        collected, {}, "a bool index must never claim a project slot"
                    )
                    self.assertEqual(
                        anomalies, [_driver_module.RECEIPT_ANOMALY_INDEX_NOT_INT]
                    )
                    self.assertIn("w1:p8", tape, "its pane still needs cleaning up")

    def test_a_bool_index_leaves_the_round_incomplete_and_failed(self) -> None:
        """End to end: refusal must reach missing placeholders and a failed round."""
        with tempfile.TemporaryDirectory() as tmp:
            specs = self._specs(Path(tmp))

            def _bool_index(*, processes, started, specs, output, collected,
                            anomalies, locator_tape, timeout):
                collected[0] = self._receipt(0, "w1:p1")
                anomalies.append(_driver_module.RECEIPT_ANOMALY_INDEX_NOT_INT)
                locator_tape.append("w1:p8")

            with mock.patch.object(
                _driver_module, "_collect_forked_receipts", _bool_index
            ):
                forked = _run_forked_projects(
                    harnesses=[object(), object()], specs=specs,
                    timeout=5.0, gate_runner=None,
                )
            self.assertTrue(forked.round_failed)
            self.assertEqual(
                forked.failure_kind, _driver_module.RECEIPT_ANOMALY_INDEX_NOT_INT
            )
            self.assertEqual(forked.salvaged_locators, ("w1:p8",))
            # Project 1 never reported, so it is MISSING rather than silently complete.
            self.assertIsNone(forked.receipts[1].endpoint_gate)
            self.assertEqual(forked.receipts[1].observation.outcome, "failed")
            gate = EndpointGateEvidence.aggregate(
                parent=EndpointGateCounters(1, 1, 0, 0, ()),
                worker_receipts=[r.endpoint_gate for r in forked.receipts],
            )
            self.assertFalse(
                gate.proven_zero_external,
                "a malformed round must not be able to prove zero external requests",
            )

    def test_plain_integer_indexes_are_still_accepted(self) -> None:
        """Baseline: strictness about type must not reject the real 0 and 1."""
        with tempfile.TemporaryDirectory() as tmp:
            collected, anomalies, tape = self._drive(
                Path(tmp), [self._receipt(0, "w1:p1"), self._receipt(1, "w1:p2")]
            )
            self.assertEqual(sorted(collected), [0, 1])
            self.assertTrue(all(type(key) is int for key in collected))
            self.assertEqual(anomalies, [])
            self.assertEqual(tape, [])

    def test_an_out_of_range_index_is_refused_and_its_locator_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            collected, anomalies, tape = self._drive(
                Path(tmp), [self._receipt(99, "w1:p9")]
            )
            self.assertEqual(collected, {})
            self.assertEqual(
                anomalies, [_driver_module.RECEIPT_ANOMALY_INDEX_OUT_OF_RANGE]
            )
            self.assertIn("w1:p9", tape)

    def test_a_project_key_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            collected, anomalies, tape = self._drive(
                Path(tmp), [self._receipt(0, "w1:p4", project_key="somewhere-else")]
            )
            self.assertEqual(collected, {})
            self.assertEqual(
                anomalies, [_driver_module.RECEIPT_ANOMALY_PROJECT_MISMATCH]
            )
            self.assertIn("w1:p4", tape)

    def test_well_formed_receipts_are_accepted_with_no_anomaly(self) -> None:
        """Baseline: validation must not reject what the workers really send."""
        with tempfile.TemporaryDirectory() as tmp:
            collected, anomalies, tape = self._drive(
                Path(tmp), [self._receipt(0, "w1:p1"), self._receipt(1, "w1:p2")]
            )
            self.assertEqual(sorted(collected), [0, 1])
            self.assertEqual(anomalies, [])
            self.assertEqual(tape, [])

    def test_an_anomaly_makes_the_round_fail_with_a_closed_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            specs = self._specs(Path(tmp))

            def _dup(*, processes, started, specs, output, collected,
                     anomalies, locator_tape, timeout):
                anomalies.append(_driver_module.RECEIPT_ANOMALY_DUPLICATE_INDEX)
                locator_tape.append("w1:p7")

            with mock.patch.object(
                _driver_module, "_collect_forked_receipts", _dup
            ):
                forked = _run_forked_projects(
                    harnesses=[object(), object()], specs=specs,
                    timeout=5.0, gate_runner=None,
                )
            self.assertTrue(forked.round_failed)
            self.assertEqual(
                forked.failure_kind, _driver_module.RECEIPT_ANOMALY_DUPLICATE_INDEX
            )
            self.assertEqual(forked.salvaged_locators, ("w1:p7",))


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

        def _boom(*, processes, started, specs, output, collected,
                  anomalies, locator_tape, timeout):
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
        """Through the PRODUCTION driver, not a hand-made shutdown call.

        The previous version drove the instance directly and so never met the context
        manager's own ``__exit__``, which released the tree with the default before any
        containment verdict could reach the explicit shutdown (review j#91687 F1).
        Here the real ``run_disposable_shared_space_smoke`` runs end to end and the
        assertion is the *filesystem*, not a flag.
        """
        for orphans, expect_removed in ((1, False), (-1, False), (0, True)):
            with self.subTest(orphans=orphans):
                report, root = _drive_production_smoke(
                    self, forked=_driver_module._ForkedRun(
                        receipts=(), orphaned_workers=orphans
                    )
                )
                self.assertEqual(
                    root.exists(), not expect_removed,
                    "root removal must follow the containment verdict",
                )
                # Evidence is the observed state, and it agrees with the disk.
                self.assertEqual(report["owned_root_present"], root.exists())
                self.assertEqual(report["owned_root_released"], not root.exists())
                self.assertEqual(report["workers_contained"], expect_removed)
                self.assertFalse(
                    report["success"] if not expect_removed else False,
                    "an uncontained run can never be a success",
                )

    def test_an_interrupt_mid_round_never_releases_the_owned_root(self) -> None:
        """``KeyboardInterrupt`` is not caught, so the policy must already be set.

        Every teardown used to ask for release with the verdict still unknown
        (measured: ``release_root`` args ``[True, True]``) — review j#91687 F2.
        """
        for injected in (KeyboardInterrupt, SystemExit):
            with self.subTest(injected=injected.__name__):
                def _interrupt(**kwargs):
                    raise injected("injected mid-round")

                report, root = _drive_production_smoke(
                    self, fork_impl=_interrupt, expect_raises=injected
                )
                self.assertIsNone(report, "the interrupt must still propagate")
                self.assertTrue(
                    root.exists(),
                    "an unknown containment verdict must not free the owned path",
                )

    def test_a_contained_round_still_releases_the_tree(self) -> None:
        """Baseline: containment must not become a permanent residue leak."""
        report, root = _drive_production_smoke(
            self, forked=_driver_module._ForkedRun(receipts=(), orphaned_workers=0)
        )
        self.assertFalse(root.exists())
        self.assertTrue(report["owned_root_released"])
        self.assertFalse(report["owned_root_present"])


def _drive_production_smoke(case, *, forked=None, fork_impl=None, expect_raises=None):
    """Run the real driver with fakes, and hand back its report and the owned root.

    Only the fork round is substituted; the isolation home, the harness, the endpoint
    gate, the context manager and both teardown paths are the production ones.
    """
    tmp = Path(tempfile.mkdtemp(dir="/tmp", prefix="smoke-driver-"))
    case.addCleanup(shutil.rmtree, tmp, True)
    bindir = tmp / "bin"
    bindir.mkdir()
    for name in ("herdr-stub", "claude", "codex"):
        script = bindir / name
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    holder = {}

    class _Popen:
        def __init__(self):
            self.pid = 919191
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def _popen(argv, **kwargs):
        return _Popen()

    def _runner(argv, *a, **k):
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    real_init = _lifecycle_module.DisposableHerdrInstance.__init__

    def _capture(self, **kwargs):
        real_init(self, **kwargs)
        holder.setdefault("root", self.root)

    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure import (
        herdr_transport,
    )

    report = None
    with mock.patch.object(
        herdr_transport, "resolve_herdr_binary",
        lambda env: type("R", (), {"path": str(bindir / "herdr-stub")})(),
    ), mock.patch.object(
        _lifecycle_module.DisposableHerdrInstance, "__init__", _capture
    ), mock.patch.object(
        _driver_module, "_run_forked_projects",
        fork_impl if fork_impl is not None else (lambda **kwargs: forked),
    ):
        env = {"HOME": str(tmp / "home"), "PATH": f"{bindir}:/usr/bin:/bin"}
        if expect_raises is not None:
            with case.assertRaises(expect_raises):
                run_disposable_shared_space_smoke(
                    tmp / "smoke-home", env=env, projects=2, process_timeout=5.0,
                    runner=_runner, popen_factory=_popen,
                )
        else:
            report = run_disposable_shared_space_smoke(
                tmp / "smoke-home", env=env, projects=2, process_timeout=5.0,
                runner=_runner, popen_factory=_popen,
            )
    return report, holder["root"]


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
