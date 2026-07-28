"""Disposable Herdr lifecycle / endpoint-capability gate unit tests (#14187).

Incident context (blocker j#85754, owner design disposition j#85756): the previous
version scored endpoint binding with post-hoc booleans, so a probe that dropped the
binding line sent a real ``herdr server stop`` to the operator's endpoint before any
boolean could be read.  These tests therefore assert the two facts that matter:

* a refused call makes **zero external requests** (the fake inner runner is never
  entered), not merely "a flag turned False";
* the guard is **load-bearing in both directions** — dropping the binding trips the
  gate, dropping the gate lets an operator-endpoint request through.

The mutation probes below are the sanctioned replacement for editing the live source:
they execute a *copy* of the module with an injected fake inner runner, under a
scrubbed ambient environment whose ``HERDR_SOCKET_PATH`` points at a non-existent
poison path.  No real Herdr binary, socket, or server is reachable from this file.
"""

from __future__ import annotations

import copy
import importlib.util
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    disposable_herdr_instance as live_module,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E402,E501
    REFUSAL_CAPABILITY_ABSENT,
    REFUSAL_CAPABILITY_NOT_MINTED,
    REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER,
    REFUSAL_ENDPOINT_UNBOUND,
    REFUSAL_OWNED_CHILD_NOT_ALIVE,
    DisposableHerdrInstance,
    EndpointBoundHerdrRunner,
    EndpointGateCounters,
    EndpointGateEvidence,
    HERDR_SOCKET_PATH_ENV,
    OwnedEndpointCapability,
    SmokeEndpointEscapeError,
)

_MODULE_PATH = Path(live_module.__file__)

# The one line that carries the endpoint into every child environment, and the one
# line that refuses a call that would leave without it.  The probes below delete each
# in turn; if either literal stops matching the source, the probe fails loudly rather
# than silently passing on a no-op mutation.
_BINDING_LINE = "        merged.update(self._binding_env)"
_GATE_LINES = (
    "        if refusal:\n"
    "            self.escape_refusals += 1\n"
    "            self.last_refusal_reason = refusal\n"
    "            self.refusal_reasons.add(refusal)\n"
)


class _Process:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


def _mint(root: Path, pid: int = 4242) -> OwnedEndpointCapability:
    """Mint a capability the same way the lifecycle does, for gate-level tests."""
    binding = live_module.DisposableHerdrBinding(
        root=root,
        socket_path=root / "herdr.sock",
        client_socket_path=root / "herdr-client.sock",
        config_path=root / "config.toml",
    )
    return live_module._mint_owned_endpoint(binding, pid)


class EndpointCapabilityTests(unittest.TestCase):
    def test_hand_built_capability_is_refused(self) -> None:
        with self.assertRaises(SmokeEndpointEscapeError) as caught:
            OwnedEndpointCapability(
                root=Path("/tmp/x"),
                socket_path=Path("/tmp/x/herdr.sock"),
                client_socket_path=Path("/tmp/x/herdr-client.sock"),
                config_path=Path("/tmp/x/config.toml"),
                owner_pid=1,
            )
        self.assertEqual(caught.exception.reason, REFUSAL_CAPABILITY_NOT_MINTED)

    def test_cloned_capability_is_not_the_minted_object(self) -> None:
        """Identity, not value: a copy that skips ``__init__`` must not pass the gate."""
        root = Path("/tmp/owned")
        capability = _mint(root)
        clone = copy.copy(capability)
        calls = []
        runner = EndpointBoundHerdrRunner(
            lambda argv, *a, **k: calls.append(argv),
            capability_provider=lambda: clone,
            binding_env={HERDR_SOCKET_PATH_ENV: str(root / "herdr.sock")},
            agent_env={},
        )
        with self.assertRaises(SmokeEndpointEscapeError) as caught:
            runner(["herdr", "workspace", "list"], env={})
        self.assertEqual(caught.exception.reason, REFUSAL_CAPABILITY_NOT_MINTED)
        self.assertEqual(calls, [], "a refused call must make zero external requests")


class EndpointBoundRunnerTests(unittest.TestCase):
    def test_binding_overrides_caller_env_and_restores_agent_xdg(self) -> None:
        calls = []
        root = Path("/tmp/owned-a")

        def inner(argv, *args, **kwargs):
            calls.append((list(argv), dict(kwargs["env"])))
            return subprocess.CompletedProcess(argv, 0, "", "")

        capability = _mint(root)
        runner = EndpointBoundHerdrRunner(
            inner,
            capability_provider=lambda: capability,
            binding_env={HERDR_SOCKET_PATH_ENV: str(root / "herdr.sock")},
            agent_env={"XDG_CONFIG_HOME": "/operator/config"},
            operator_socket_paths=("/operator/socket",),
        )
        runner(
            ["herdr", "agent", "start", "mzb1_x", "--", "/bin/true"],
            env={HERDR_SOCKET_PATH_ENV: "/operator/socket"},
        )
        argv, env = calls[0]
        self.assertEqual(env[HERDR_SOCKET_PATH_ENV], str(root / "herdr.sock"))
        self.assertEqual(
            argv,
            [
                "herdr", "agent", "start", "mzb1_x",
                "--env", "XDG_CONFIG_HOME=/operator/config",
                "--", "/bin/true",
            ],
        )
        self.assertTrue(runner.all_calls_bound)
        self.assertEqual(runner.operator_endpoint_requests, 0)
        self.assertEqual(runner.escape_refusals, 0)

    def test_no_capability_means_no_request_at_all(self) -> None:
        """Before a server is owned, every request fails closed with zero dispatch."""
        calls = []
        runner = EndpointBoundHerdrRunner(
            lambda argv, *a, **k: calls.append(argv),
            capability_provider=lambda: None,
            binding_env={HERDR_SOCKET_PATH_ENV: "/tmp/owned-b/herdr.sock"},
            agent_env={},
        )
        with self.assertRaises(SmokeEndpointEscapeError) as caught:
            runner(["herdr", "server", "stop"], env={})
        self.assertEqual(caught.exception.reason, REFUSAL_CAPABILITY_ABSENT)
        self.assertEqual(calls, [])
        self.assertEqual(runner.dispatched_calls, 0)
        self.assertEqual(runner.escape_refusals, 1)

    def test_unbound_effective_socket_is_refused_before_dispatch(self) -> None:
        """The exact incident shape: the binding no longer carries the owned socket."""
        calls = []
        root = Path("/tmp/owned-c")
        capability = _mint(root)
        runner = EndpointBoundHerdrRunner(
            lambda argv, *a, **k: calls.append(argv),
            capability_provider=lambda: capability,
            binding_env={},  # binding dropped
            agent_env={},
            operator_socket_paths=("/operator/socket",),
        )
        with self.assertRaises(SmokeEndpointEscapeError) as caught:
            runner(
                ["herdr", "server", "stop"],
                env={HERDR_SOCKET_PATH_ENV: "/operator/socket"},
            )
        self.assertEqual(caught.exception.reason, REFUSAL_ENDPOINT_UNBOUND)
        self.assertEqual(calls, [], "the operator endpoint must receive no request")
        self.assertEqual(runner.operator_endpoint_requests, 0)
        self.assertFalse(runner.all_calls_bound)


class DisposableLifecycleTests(unittest.TestCase):
    def test_owned_start_ready_stop_and_exact_root_cleanup(self) -> None:
        calls = []
        process = _Process()

        def popen(argv, **kwargs):
            calls.append(("popen", list(argv), dict(kwargs["env"])))
            return process

        def runner(argv, **kwargs):
            calls.append(("run", list(argv), dict(kwargs["env"])))
            if list(argv[1:]) == ["server", "stop"]:
                process.returncode = 0
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instance"
            instance = DisposableHerdrInstance(
                binary="/bin/true",
                root=root,
                base_env={"HOME": str(Path(tmp) / "operator"), "PATH": "/bin"},
                runner=runner,
                popen_factory=popen,
                sleeper=lambda _seconds: None,
                ambient_env={},
            )
            with instance:
                self.assertTrue(instance.started)
                self.assertTrue(instance.ready)
                self.assertTrue(root.exists())
                self.assertIsNotNone(instance.capability)
            self.assertTrue(instance.stopped)
            self.assertFalse(root.exists())
            self.assertIsNone(instance.capability)
            evidence = instance.as_evidence()
            self.assertEqual(evidence["endpoint_residue"], 0)
            self.assertTrue(evidence["endpoint_bound"])
            self.assertEqual(evidence["operator_endpoint_requests"], 0)
            self.assertEqual(evidence["endpoint_escape_refusals"], 0)
            self.assertFalse(evidence["graceful_stop_refused"])
            self.assertNotIn(str(root), repr(evidence))
            run_envs = [entry[2] for entry in calls if entry[0] == "run"]
            self.assertTrue(run_envs)
            resolved_root = str(root.resolve())
            self.assertTrue(
                all(env[HERDR_SOCKET_PATH_ENV].startswith(resolved_root) for env in run_envs)
            )

    def test_shutdown_falls_back_to_the_owned_handle_when_the_gate_refuses(self) -> None:
        """A refused ``server stop`` must still tear down OUR child, and only ours.

        This is the safety property the incident lacked: the graceful CLI request is
        the dangerous one, so when it cannot be proven endpoint-owned it is dropped
        (zero external requests) and cleanup proceeds through the exact process handle,
        which is incapable of addressing another server.
        """
        process = _Process()
        dispatched = []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instance"
            instance = DisposableHerdrInstance(
                binary="/bin/true",
                root=root,
                base_env={"HOME": str(Path(tmp) / "operator")},
                runner=lambda argv, **kwargs: dispatched.append(list(argv))
                or subprocess.CompletedProcess(argv, 0, "[]", ""),
                popen_factory=lambda argv, **kwargs: process,
                sleeper=lambda _seconds: None,
                ambient_env={HERDR_SOCKET_PATH_ENV: "/operator/socket"},
            )
            instance.start()
            dispatched.clear()
            # Withdraw the binding after startup: the shutdown request can no longer be
            # proven to target the owned endpoint.
            instance.runner._binding_env = {}
            instance.shutdown()

            self.assertEqual(dispatched, [], "no request may leave once unbound")
            self.assertTrue(instance.graceful_stop_refused)
            self.assertTrue(instance.stopped)
            self.assertFalse(root.exists())
            self.assertEqual(instance.runner.operator_endpoint_requests, 0)
            self.assertGreaterEqual(instance.runner.escape_refusals, 1)


class CleanupAuthoritySplitTests(unittest.TestCase):
    """Client-call capability vs cleanup authority (review j#85841 F2).

    The pre-fix gate compared the owned pid and nothing else, so a capability outlived
    the process it described: once the owned child exited, ``herdr server stop`` was
    still dispatched at a socket path a stranger could have taken over.
    """

    def _instance(self, tmp: str, dispatched: list, process) -> DisposableHerdrInstance:
        instance = DisposableHerdrInstance(
            binary="/bin/true",
            root=Path(tmp) / "instance",
            base_env={"HOME": str(Path(tmp) / "operator")},
            runner=lambda argv, **kwargs: dispatched.append(list(argv))
            or subprocess.CompletedProcess(argv, 0, "[]", ""),
            popen_factory=lambda argv, **kwargs: process,
            sleeper=lambda _seconds: None,
            ambient_env={},
        )
        instance.start()
        return instance

    def test_dead_owned_child_withdraws_authority_before_any_request(self) -> None:
        process = _Process()
        dispatched: list = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, dispatched, process)
            self.addCleanup(instance.shutdown)
            dispatched.clear()

            # The owned child exits.  The handle and its pid are unchanged, which is
            # precisely why pid equality alone was never proof.
            process.returncode = 17
            self.assertFalse(instance.process_alive)

            for command in (["/bin/true", "server", "stop"], ["/bin/true", "workspace", "list"]):
                with self.assertRaises(SmokeEndpointEscapeError) as caught:
                    instance.runner(command, capture_output=True)
                self.assertEqual(caught.exception.reason, REFUSAL_OWNED_CHILD_NOT_ALIVE)
            self.assertEqual(
                dispatched, [], "a post-mortem request must make zero external requests"
            )
            self.assertEqual(instance.runner.escape_refusals, 2)
            self.assertEqual(instance.runner.operator_endpoint_requests, 0)

    def test_a_live_child_still_passes_the_same_gate(self) -> None:
        """Baseline: the fence above must be the dead child, not a blanket refusal."""
        process = _Process()
        dispatched: list = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, dispatched, process)
            self.addCleanup(instance.shutdown)
            dispatched.clear()
            instance.runner(["/bin/true", "workspace", "list"], capture_output=True)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(instance.runner.escape_refusals, 0)

    def test_a_non_minting_process_may_call_but_never_destroy(self) -> None:
        """A forked worker holds the endpoint, not the right to stop the server."""
        process = _Process()
        dispatched: list = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, dispatched, process)
            self.addCleanup(instance.shutdown)
            dispatched.clear()

            # Stand in for "some other process holds this inherited capability" without
            # forking: the capability records who minted it, so a different pid is
            # exactly what a worker looks like.
            with mock.patch.object(live_module.os, "getpid", return_value=os.getpid() + 1):
                instance.runner(["/bin/true", "workspace", "list"], capture_output=True)
                self.assertEqual(len(dispatched), 1, "ordinary client calls must survive")
                with self.assertRaises(SmokeEndpointEscapeError) as caught:
                    instance.runner(["/bin/true", "server", "stop"], capture_output=True)
            self.assertEqual(caught.exception.reason, REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER)
            self.assertEqual(len(dispatched), 1, "the destructive request never left")

    def test_a_real_forked_worker_is_not_locked_out_by_a_false_dead_poll(self) -> None:
        """The trap the naive fix walks into (review j#85841 F2 caveat).

        A forked worker is not the server's parent, so ``waitpid`` there raises
        ``ChildProcessError`` and CPython reports a **live** child as exited.  Had the
        liveness fence been placed on the shared capability provider, every healthy
        worker call would fail closed.  This test forks for real and asserts the
        worker still dispatches.
        """
        server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        context = multiprocessing.get_context("fork")
        queue = context.Queue()

        with tempfile.TemporaryDirectory() as tmp:
            dispatched: list = []
            instance = DisposableHerdrInstance(
                binary="/bin/true",
                root=Path(tmp) / "instance",
                base_env={"HOME": str(Path(tmp) / "operator")},
                runner=lambda argv, **kwargs: dispatched.append(list(argv))
                or subprocess.CompletedProcess(argv, 0, "[]", ""),
                popen_factory=lambda argv, **kwargs: server,
                sleeper=lambda _seconds: None,
                ambient_env={},
            )
            instance.start()
            self.addCleanup(instance.shutdown)

            worker = context.Process(target=_worker_probe, args=(queue, instance))
            worker.start()
            child_poll, dispatched_in_child, refusal = queue.get(timeout=30)
            worker.join(timeout=30)

            # The premise: the same handle reads as dead in the worker, alive in us.
            self.assertIsNotNone(child_poll, "premise lost: the worker saw a live poll()")
            self.assertIsNone(server.poll(), "the stand-in server must still be running")
            # The property: the worker was NOT refused on that false reading.
            self.assertEqual(refusal, "", f"worker was locked out: {refusal}")
            self.assertEqual(dispatched_in_child, 1)


def _worker_probe(queue, instance) -> None:
    """Run one client call in a forked worker; report what it observed."""
    seen = []
    instance.runner._inner = lambda argv, *a, **k: seen.append(list(argv)) or (
        subprocess.CompletedProcess(argv, 0, "[]", "")
    )
    refusal = ""
    try:
        instance.runner(["/bin/true", "workspace", "list"], capture_output=True)
    except SmokeEndpointEscapeError as exc:
        refusal = exc.reason
    queue.put((instance._process.poll(), len(seen), refusal))


#: Herdr control commands that are NOT client calls, measured read-only from the
#: installed CLI's own help (``herdr --help`` / ``herdr session --help``, v0.7.4; the
#: same surface review j#91604 cites from v0.7.1 ``src/cli.rs``).  ``session stop`` /
#: ``session delete`` accept ``default`` by name — the help says so explicitly — which
#: is why a denylist of one entry was fail-open.  This fixture is the drift anchor: if
#: Herdr grows another control, the allowlist still refuses it by construction, and this
#: list only has to grow for the *assertion* to keep naming real commands.
HERDR_LIFECYCLE_CONTROLS = (
    ("session", "stop"),
    ("session", "delete"),
    ("session", "attach"),
    ("session", "list"),
    ("server", "reload-config"),
    ("config", "reset-keys"),
    ("channel", "set"),
    ("update", "--handoff"),
    ("worktree", "add"),
    ("integration", "install"),
)


class ControlSurfaceAllowlistTests(unittest.TestCase):
    """Only what the smoke needs may be dispatched at all (review j#91604 F1).

    The previous version denylisted ``("server","stop")`` and permitted everything
    else, so a forked worker could stop or delete the operator's *default* Herdr
    session. An allowlist inverts the default: an unforeseen control is refused
    because it was never named, not because someone predicted it.
    """

    def _instance(self, tmp: str, dispatched: list, process) -> DisposableHerdrInstance:
        instance = DisposableHerdrInstance(
            binary="/bin/true",
            root=Path(tmp) / "instance",
            base_env={"HOME": str(Path(tmp) / "operator")},
            runner=lambda argv, **kwargs: dispatched.append(list(argv))
            or subprocess.CompletedProcess(argv, 0, "[]", ""),
            popen_factory=lambda argv, **kwargs: process,
            sleeper=lambda _seconds: None,
            ambient_env={},
        )
        instance.start()
        return instance

    def test_the_allowlist_names_no_lifecycle_control(self) -> None:
        """Drift guard: nothing control-shaped may be added to the client allowlist."""
        overlap = set(HERDR_LIFECYCLE_CONTROLS) & set(live_module.CLIENT_CALL_SUBCOMMANDS)
        self.assertEqual(overlap, set(), f"lifecycle control(s) in the allowlist: {overlap}")
        control_verbs = {"stop", "delete", "attach", "reload-config", "reset-keys", "set"}
        offenders = {
            pair
            for pair in live_module.CLIENT_CALL_SUBCOMMANDS
            if pair[1] in control_verbs
        }
        self.assertEqual(offenders, set(), f"control-shaped verbs allowlisted: {offenders}")
        self.assertEqual(
            set(live_module.MINTER_ONLY_SUBCOMMANDS),
            {("server", "stop")},
            "the minter's extra authority must stay exactly its own graceful stop",
        )

    def test_every_lifecycle_control_is_refused_with_zero_dispatch(self) -> None:
        process = _Process()
        dispatched: list = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, dispatched, process)
            self.addCleanup(instance.shutdown)
            dispatched.clear()

            for group, verb in HERDR_LIFECYCLE_CONTROLS:
                argv = ["/bin/true", group, verb, "default"]
                # As the minter (the strongest position anyone holds here).
                with self.assertRaises(SmokeEndpointEscapeError, msg=argv) as caught:
                    instance.runner(argv, capture_output=True)
                self.assertEqual(
                    caught.exception.reason,
                    live_module.REFUSAL_COMMAND_NOT_ALLOWLISTED,
                    f"{group} {verb} was refused for the wrong reason",
                )
                # And as a forked worker.
                with mock.patch.object(
                    live_module.os, "getpid", return_value=os.getpid() + 1
                ):
                    with self.assertRaises(SmokeEndpointEscapeError, msg=argv):
                        instance.runner(argv, capture_output=True)
            self.assertEqual(
                dispatched, [], "a refused control must make zero external requests"
            )

    def test_the_minters_own_graceful_stop_is_still_allowed(self) -> None:
        """Baseline: the allowlist must not break the one control cleanup needs."""
        process = _Process()
        dispatched: list = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, dispatched, process)
            self.addCleanup(instance.shutdown)
            dispatched.clear()
            instance.runner(["/bin/true", "server", "stop"], capture_output=True)
            self.assertEqual(len(dispatched), 1)

            # ...and is still denied to a worker, with the authority reason rather than
            # the allowlist one: it IS a sanctioned command, just not for that process.
            with mock.patch.object(
                live_module.os, "getpid", return_value=os.getpid() + 1
            ):
                with self.assertRaises(SmokeEndpointEscapeError) as caught:
                    instance.runner(["/bin/true", "server", "stop"], capture_output=True)
            self.assertEqual(
                caught.exception.reason, REFUSAL_CLEANUP_AUTHORITY_NOT_OWNER
            )
            self.assertEqual(len(dispatched), 1)

    def test_allowlisted_client_calls_survive_for_both_processes(self) -> None:
        """Baseline: the fence must not turn into a blanket refusal."""
        process = _Process()
        dispatched: list = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, dispatched, process)
            self.addCleanup(instance.shutdown)
            dispatched.clear()
            for group, verb in sorted(live_module.CLIENT_CALL_SUBCOMMANDS):
                instance.runner(["/bin/true", group, verb], capture_output=True)
            minter_calls = len(dispatched)
            self.assertEqual(minter_calls, len(live_module.CLIENT_CALL_SUBCOMMANDS))

            with mock.patch.object(
                live_module.os, "getpid", return_value=os.getpid() + 1
            ):
                for group, verb in sorted(live_module.CLIENT_CALL_SUBCOMMANDS):
                    instance.runner(["/bin/true", group, verb], capture_output=True)
            self.assertEqual(len(dispatched), minter_calls * 2)


class CrossProcessGateEvidenceTests(unittest.TestCase):
    """The negative proof must span every process that held the capability (F1)."""

    def _counters(self, **overrides) -> EndpointGateCounters:
        base = dict(
            dispatched_calls=1,
            bound_calls=1,
            escape_refusals=0,
            operator_endpoint_requests=0,
            refusal_reasons=(),
        )
        base.update(overrides)
        return EndpointGateCounters(**base)

    def test_every_process_is_folded_in(self) -> None:
        gate = EndpointGateEvidence.aggregate(
            parent=self._counters(dispatched_calls=3, bound_calls=3),
            worker_receipts=[
                self._counters(dispatched_calls=4, bound_calls=4),
                self._counters(dispatched_calls=5, bound_calls=5),
            ],
        )
        self.assertEqual(gate.processes, 3)
        self.assertEqual(gate.dispatched_calls, 12)
        self.assertTrue(gate.receipts_complete)
        self.assertTrue(gate.all_calls_bound)
        self.assertTrue(gate.proven_zero_external)

    def test_a_worker_that_reached_an_operator_endpoint_is_not_hidden(self) -> None:
        """The regression the parent-only counters could not see at all."""
        gate = EndpointGateEvidence.aggregate(
            parent=self._counters(),
            worker_receipts=[self._counters(operator_endpoint_requests=1)],
        )
        self.assertEqual(gate.operator_endpoint_requests, 1)
        self.assertTrue(gate.operator_endpoint_connected)
        self.assertFalse(gate.proven_zero_external)

    def test_a_missing_receipt_is_never_a_zero(self) -> None:
        gate = EndpointGateEvidence.aggregate(
            parent=self._counters(), worker_receipts=[self._counters(), None]
        )
        self.assertEqual(gate.receipts_expected, 2)
        self.assertEqual(gate.receipts_missing, 1)
        self.assertFalse(gate.receipts_complete)
        self.assertFalse(
            gate.proven_zero_external,
            "an unobserved process is not a process that made zero requests",
        )
        # The readable counters are still reported honestly; it is the CLAIM that fails.
        self.assertEqual(gate.operator_endpoint_requests, 0)

    def test_an_inconsistent_receipt_is_treated_like_a_missing_one(self) -> None:
        garbled = self._counters(dispatched_calls=1, bound_calls=9)
        self.assertFalse(garbled.consistent)
        gate = EndpointGateEvidence.aggregate(
            parent=self._counters(), worker_receipts=[garbled]
        )
        self.assertFalse(gate.receipts_consistent)
        self.assertFalse(gate.proven_zero_external)

    def test_a_refusal_count_without_a_reason_is_inconsistent(self) -> None:
        """The count and the closed vocabulary have to corroborate each other."""
        self.assertFalse(self._counters(escape_refusals=1, refusal_reasons=()).consistent)
        self.assertFalse(
            self._counters(escape_refusals=0, refusal_reasons=("endpoint_unbound",)).consistent
        )
        self.assertTrue(
            self._counters(
                escape_refusals=1, refusal_reasons=("endpoint_unbound",)
            ).consistent
        )

    def test_counters_do_not_cross_a_fork(self) -> None:
        """The premise of the whole finding, measured rather than assumed."""
        root = Path("/tmp/owned-fork")
        capability = _mint(root, pid=os.getpid())
        runner = EndpointBoundHerdrRunner(
            lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0, "", ""),
            capability_provider=lambda: capability,
            binding_env={HERDR_SOCKET_PATH_ENV: str(root / "herdr.sock")},
            agent_env={},
        )
        runner(["herdr", "workspace", "list"], env={})
        before = runner.dispatched_calls

        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        worker = context.Process(target=_counter_probe, args=(queue, runner))
        worker.start()
        child_snapshot = queue.get(timeout=30)
        worker.join(timeout=30)

        self.assertEqual(child_snapshot.dispatched_calls, before + 1)
        self.assertEqual(
            runner.dispatched_calls,
            before,
            "the parent's counter must be untouched — which is why receipts exist",
        )
        gate = EndpointGateEvidence.aggregate(
            parent=EndpointGateCounters.snapshot(runner),
            worker_receipts=[child_snapshot],
        )
        self.assertEqual(gate.dispatched_calls, before + before + 1)


def _counter_probe(queue, runner) -> None:
    runner(["herdr", "workspace", "list"], env={})
    queue.put(EndpointGateCounters.snapshot(runner))


class MutationProbeTests(unittest.TestCase):
    """Run guard-removal mutations where the operator endpoint is unreachable.

    Sanctioned protocol (design disposition j#85756 item 5): mutate a *copy* of the
    module, inject a fake inner runner, and scrub the ambient environment so that even
    a total guard failure could only reach a non-existent poison path.  Never mutate
    the live source while holding real operator capability.
    """

    def _load_mutated(self, directory: Path, name: str, source: str):
        path = directory / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # ``dataclasses`` resolves annotations through ``sys.modules[cls.__module__]``,
        # so the probe copy must be registered while it executes.
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    def _drive(self, module, poison: str):
        """One ``server stop`` through the mutated module; return (dispatched, runner)."""
        dispatched = []
        root = Path("/tmp/probe-owned")
        binding = module.DisposableHerdrBinding(
            root=root,
            socket_path=root / "herdr.sock",
            client_socket_path=root / "herdr-client.sock",
            config_path=root / "config.toml",
        )
        capability = module._mint_owned_endpoint(binding, 999)
        runner = module.EndpointBoundHerdrRunner(
            lambda argv, *a, **k: dispatched.append(dict(k["env"])),
            capability_provider=lambda: capability,
            binding_env={module.HERDR_SOCKET_PATH_ENV: str(root / "herdr.sock")},
            agent_env={},
            operator_socket_paths=(poison,),
        )
        try:
            # ``env`` omitted on purpose: this reproduces the incident's call shape,
            # where the effective env is inherited from the ambient process.
            runner(["herdr", "server", "stop"])
        except module.SmokeEndpointEscapeError:
            pass
        return dispatched, runner

    def test_probes_move_the_two_counters_in_opposite_directions(self) -> None:
        source = _MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn(_BINDING_LINE, source, "binding line literal drifted")
        self.assertIn(_GATE_LINES, source, "gate literal drifted")

        binding_dropped = source.replace(
            _BINDING_LINE, "        pass  # PROBE: binding dropped"
        )
        # ``if False:`` rather than deleting the block: a naive deletion breaks the
        # syntax and the probe would go RED for the wrong reason (#14219 lesson).
        unguarded = binding_dropped.replace(
            _GATE_LINES, "        if False:\n            pass\n"
        )
        self.assertNotEqual(binding_dropped, source)
        self.assertNotEqual(unguarded, binding_dropped)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            poison = str(directory / "operator-poison.sock")
            with mock.patch.dict(
                os.environ, {"HERDR_SOCKET_PATH": poison, "PATH": "/nonexistent"}, clear=True
            ):
                baseline = self._load_mutated(directory, "probe_baseline", source)
                dispatched, runner = self._drive(baseline, poison)
                self.assertEqual(len(dispatched), 1, "baseline must still work")
                self.assertEqual(runner.escape_refusals, 0)
                self.assertEqual(runner.operator_endpoint_requests, 0)
                self.assertEqual(runner.bound_calls, 1)
                self.assertTrue(runner.all_calls_bound)

                # Mutation A — the exact incident: the binding stops being applied, so
                # the call inherits the ambient (operator) socket.  The gate must stop
                # it BEFORE dispatch.
                mutated = self._load_mutated(
                    directory, "probe_binding_dropped", binding_dropped
                )
                dispatched, runner = self._drive(mutated, poison)
                self.assertEqual(dispatched, [], "unbound request must not be dispatched")
                self.assertEqual(runner.escape_refusals, 1)
                self.assertEqual(runner.operator_endpoint_requests, 0)

                # Mutation B — same dropped binding, gate removed as well.  The
                # operator-endpoint request now really is dispatched, which proves two
                # things at once: the gate (not luck) is what stopped mutation A, and
                # ``operator_endpoint_requests`` is a counter that can actually move
                # rather than a constant (review #14247 vacuity lesson).
                mutated = self._load_mutated(directory, "probe_unguarded", unguarded)
                dispatched, runner = self._drive(mutated, poison)
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0]["HERDR_SOCKET_PATH"], poison)
                self.assertEqual(runner.operator_endpoint_requests, 1)
                # ``bound_calls`` is derived from the effective socket rather than
                # incremented next to ``dispatched_calls``, so it can actually move
                # apart from it.  Were it the old unconditional increment, this
                # assertion would read 1 and ``all_calls_bound`` would stay True while
                # the request was landing on the operator's endpoint.
                self.assertEqual(runner.bound_calls, 0)
                self.assertFalse(runner.all_calls_bound)

            self.assertFalse(
                Path(poison).exists(),
                "the poison endpoint must never be created by a probe",
            )


if __name__ == "__main__":
    unittest.main()
