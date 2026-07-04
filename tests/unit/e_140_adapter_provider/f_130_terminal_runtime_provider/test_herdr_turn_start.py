"""Built-in herdr CLI wait-primitive + rail resolver tests (Redmine #13248).

Pins the herdr ``wait agent-status`` wait primitive and the fully wired rail
resolver *without a live herdr binary*: argv construction and the two-phase
arm/collect are verified through an injected ``Popen`` factory that simulates
exit-0 / non-zero / hang without spawning herdr, the non-zero exit classification
is exercised for every branch (changed / timeout / absent / error), and the
resolver reuses the #13245 trusted-env binary resolution (tmux/off and herdr
branches). No subprocess spawns.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    HERDR_STATUS_WORKING,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    TerminalTransportConfig,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    WAIT_ABSENT,
    WAIT_CHANGED,
    WAIT_ERROR,
    WAIT_TIMEOUT,
    HerdrTurnStartRail,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_turn_start import (
    SUBPROCESS_TIMEOUT_MARGIN_SECONDS,
    HerdrCliWaitPrimitive,
    resolve_turn_start_rail,
)

BIN = "/opt/herdr/bin/herdr"
TARGET = "w1:p1"


class FakeProc:
    """A ``subprocess.Popen``-shaped fake: scripted communicate / returncode."""

    def __init__(
        self, *, returncode=0, stdout="", stderr="", hang=False
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.communicate_calls = 0
        self.communicate_timeouts: list = []

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        self.communicate_timeouts.append(timeout)
        if self._hang and not self.killed:
            raise subprocess.TimeoutExpired(cmd="herdr wait", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


class RecordingPopen:
    """A ``subprocess.Popen``-shaped factory: records argv and returns a FakeProc."""

    def __init__(self, proc=None, raises=None):
        self.calls: list = []
        self._proc = proc if proc is not None else FakeProc()
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._raises is not None:
            raise self._raises
        return self._proc


class ArmArgvTests(unittest.TestCase):
    def test_arm_builds_wait_argv(self) -> None:
        popen = RecordingPopen()
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        prim.arm(TARGET, timeout_ms=8000)
        argv, kwargs = popen.calls[0]
        self.assertEqual(
            argv,
            [BIN, "wait", "agent-status", TARGET, "--status", HERDR_STATUS_WORKING, "--timeout", "8000"],
        )
        self.assertEqual(kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(kwargs.get("stderr"), subprocess.PIPE)
        self.assertTrue(kwargs.get("text"))

    def test_arm_invalid_target_prefails_error_without_popen(self) -> None:
        popen = RecordingPopen()
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        armed = prim.arm("bad target", timeout_ms=8000)
        self.assertEqual(popen.calls, [])  # never spawned
        self.assertEqual(armed.collect().kind, WAIT_ERROR)

    def test_arm_invalid_timeout_prefails_error(self) -> None:
        popen = RecordingPopen()
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        armed = prim.arm(TARGET, timeout_ms=0)
        self.assertEqual(popen.calls, [])
        self.assertEqual(armed.collect().kind, WAIT_ERROR)

    def test_arm_spawn_filenotfound_prefails_error(self) -> None:
        popen = RecordingPopen(raises=FileNotFoundError())
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        armed = prim.arm(TARGET, timeout_ms=8000)
        self.assertEqual(armed.collect().kind, WAIT_ERROR)

    def test_outer_timeout_is_inner_plus_margin(self) -> None:
        proc = FakeProc(returncode=0)
        popen = RecordingPopen(proc=proc)
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        armed = prim.arm(TARGET, timeout_ms=8000)
        armed.collect()
        self.assertEqual(
            proc.communicate_timeouts[0], 8.0 + SUBPROCESS_TIMEOUT_MARGIN_SECONDS
        )


class CollectClassificationTests(unittest.TestCase):
    def _collect(self, proc) -> str:
        popen = RecordingPopen(proc=proc)
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        return prim.arm(TARGET, timeout_ms=8000).collect().kind

    def test_exit_zero_is_changed(self) -> None:
        self.assertEqual(
            self._collect(FakeProc(returncode=0, stdout='{"event":"working"}')),
            WAIT_CHANGED,
        )

    def test_timeout_stderr_is_timeout(self) -> None:
        self.assertEqual(
            self._collect(
                FakeProc(
                    returncode=1,
                    stderr="timed out waiting for agent status change",
                )
            ),
            WAIT_TIMEOUT,
        )

    def test_pane_get_error_is_absent(self) -> None:
        self.assertEqual(
            self._collect(FakeProc(returncode=1, stderr="no such pane: w9:p9")),
            WAIT_ABSENT,
        )

    def test_unclassified_nonzero_is_error(self) -> None:
        self.assertEqual(
            self._collect(FakeProc(returncode=2, stderr="socket connection refused")),
            WAIT_ERROR,
        )

    def test_hang_is_reaped_and_reported_timeout(self) -> None:
        proc = FakeProc(hang=True)
        popen = RecordingPopen(proc=proc)
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        result = prim.arm(TARGET, timeout_ms=8000).collect()
        self.assertEqual(result.kind, WAIT_TIMEOUT)
        self.assertTrue(proc.killed)

    def test_cancel_reaps_process(self) -> None:
        proc = FakeProc(returncode=0)
        popen = RecordingPopen(proc=proc)
        prim = HerdrCliWaitPrimitive(BIN, popen=popen)
        armed = prim.arm(TARGET, timeout_ms=8000)
        armed.cancel()
        self.assertTrue(proc.killed)

    def test_empty_binary_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            HerdrCliWaitPrimitive("", popen=RecordingPopen())


class RailResolverTests(unittest.TestCase):
    def test_tmux_default_is_off(self) -> None:
        self.assertIsNone(resolve_turn_start_rail(None, env={}))
        self.assertIsNone(
            resolve_turn_start_rail(TerminalTransportConfig.default(), env={})
        )

    def test_herdr_without_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError):
            resolve_turn_start_rail(
                TerminalTransportConfig(backend=BACKEND_HERDR), env={}
            )

    def test_herdr_with_unresolvable_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError):
            resolve_turn_start_rail(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: "/no/such/herdr"},
            )

    def test_herdr_with_resolvable_binary_returns_rail(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binpath = os.path.join(tmp, "herdr")
            with open(binpath, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IXUSR)
            rail = resolve_turn_start_rail(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: binpath},
                popen=RecordingPopen(),
            )
            self.assertIsInstance(rail, HerdrTurnStartRail)


if __name__ == "__main__":
    unittest.main()
