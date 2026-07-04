"""Built-in herdr CLI agent-state reader tests (Redmine #13246).

Pins the pure, fail-closed herdr ``agent get`` / ``agent list`` reader and its
default-off selection resolver *without a live herdr binary*: argv construction
is verified through an injected subprocess ``runner``, JSON parsing is exercised
for the happy path and every defensive branch (non-JSON, missing key,
unrecognised status, array vs object list payloads), and the fail-closed paths
(malformed target, missing binary, non-zero exit, timeout) are simulated. The
resolver reuses the #13245 trusted-env binary resolution, pinned here for the
tmux/off and herdr branches. No subprocess spawns.
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
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BLOCKED,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    HerdrCliAgentStateReader,
    resolve_agent_state_reader,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
)


class RecordingRunner:
    """A ``subprocess.run``-shaped callable that records argv and replays a result."""

    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.calls: list = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout=self._stdout, stderr=self._stderr
        )


BIN = "/opt/herdr/bin/herdr"


class ReadAgentStateTest(unittest.TestCase):
    def test_argv_and_mapping(self) -> None:
        runner = RecordingRunner(stdout='{"agent_status": "working"}')
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("poc_claude")
        self.assertTrue(result.ok)
        self.assertEqual(result.state, RUNTIME_BUSY)
        self.assertEqual(result.raw_status, "working")
        self.assertEqual(
            runner.calls[0][0], [BIN, "agent", "get", "poc_claude", "--json"]
        )

    def test_each_status_maps(self) -> None:
        cases = {
            "working": RUNTIME_BUSY,
            "blocked": RUNTIME_BLOCKED,
            "idle": RUNTIME_AWAITING_INPUT,
            "done": RUNTIME_TURN_ENDED,
            "unknown": RUNTIME_UNKNOWN,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                runner = RecordingRunner(stdout=f'{{"agent_status": "{status}"}}')
                reader = HerdrCliAgentStateReader(BIN, runner=runner)
                self.assertEqual(reader.read_agent_state("w1:p1").state, expected)

    def test_alternate_status_key(self) -> None:
        runner = RecordingRunner(stdout='{"status": "idle"}')
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        self.assertEqual(reader.read_agent_state("w1:p1").state, RUNTIME_AWAITING_INPUT)

    def test_non_json_is_observed_unknown(self) -> None:
        runner = RecordingRunner(stdout="not json at all")
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("w1:p1")
        self.assertTrue(result.ok)  # command ran fine
        self.assertEqual(result.state, RUNTIME_UNKNOWN)
        self.assertIsNone(result.raw_status)

    def test_missing_status_key_is_observed_unknown(self) -> None:
        runner = RecordingRunner(stdout='{"name": "poc_claude"}')
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("w1:p1")
        self.assertTrue(result.ok)
        self.assertEqual(result.state, RUNTIME_UNKNOWN)

    def test_unrecognised_status_is_observed_unknown_with_provenance(self) -> None:
        runner = RecordingRunner(stdout='{"agent_status": "frobnicate"}')
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("w1:p1")
        self.assertTrue(result.ok)
        self.assertEqual(result.state, RUNTIME_UNKNOWN)
        self.assertEqual(result.raw_status, "frobnicate")

    def test_invalid_target_never_spawns(self) -> None:
        runner = RecordingRunner()
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("bad target")
        self.assertEqual(result.reason, REASON_INVALID_TARGET)
        self.assertEqual(result.state, RUNTIME_UNKNOWN)
        self.assertEqual(runner.calls, [])

    def test_nonzero_exit_is_transport_error(self) -> None:
        runner = RecordingRunner(returncode=1, stderr="no such pane")
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("w1:p1")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)
        self.assertEqual(result.state, RUNTIME_UNKNOWN)
        self.assertIn("no such pane", result.detail)

    def test_missing_binary_is_binary_not_found(self) -> None:
        runner = RecordingRunner(raises=FileNotFoundError())
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.read_agent_state("w1:p1")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_BINARY_NOT_FOUND)

    def test_timeout_is_transport_error(self) -> None:
        runner = RecordingRunner(raises=subprocess.TimeoutExpired(BIN, 10))
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        self.assertEqual(reader.read_agent_state("w1:p1").reason, REASON_TRANSPORT_ERROR)

    def test_os_error_is_transport_error(self) -> None:
        runner = RecordingRunner(raises=OSError("boom"))
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        self.assertEqual(reader.read_agent_state("w1:p1").reason, REASON_TRANSPORT_ERROR)

    def test_empty_binary_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            HerdrCliAgentStateReader("")


class ListAgentStatesTest(unittest.TestCase):
    def test_argv(self) -> None:
        runner = RecordingRunner(stdout="[]")
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        reader.list_agent_states()
        self.assertEqual(runner.calls[0][0], [BIN, "agent", "list", "--json"])

    def test_bare_array_payload(self) -> None:
        runner = RecordingRunner(
            stdout='[{"name": "poc_claude", "agent_status": "working"}, '
            '{"name": "poc_codex", "agent_status": "done"}]'
        )
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertTrue(result.ok)
        self.assertEqual(
            result.states,
            (("poc_claude", RUNTIME_BUSY), ("poc_codex", RUNTIME_TURN_ENDED)),
        )

    def test_object_payload_with_agents_key(self) -> None:
        runner = RecordingRunner(
            stdout='{"agents": [{"agent": "w1:p1", "status": "blocked"}]}'
        )
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertEqual(result.states, (("w1:p1", RUNTIME_BLOCKED),))

    def test_row_missing_status_maps_to_unknown(self) -> None:
        runner = RecordingRunner(stdout='[{"name": "poc_claude"}]')
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertEqual(result.states, (("poc_claude", RUNTIME_UNKNOWN),))

    def test_row_missing_handle_is_skipped(self) -> None:
        runner = RecordingRunner(stdout='[{"agent_status": "working"}]')
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertTrue(result.ok)
        self.assertEqual(result.states, ())

    def test_non_json_is_empty_success(self) -> None:
        runner = RecordingRunner(stdout="garbage")
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertTrue(result.ok)
        self.assertEqual(result.states, ())

    def test_nonzero_exit_is_transport_error(self) -> None:
        runner = RecordingRunner(returncode=3, stderr="down")
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)
        self.assertEqual(result.states, ())

    def test_missing_binary_is_binary_not_found(self) -> None:
        runner = RecordingRunner(raises=FileNotFoundError())
        reader = HerdrCliAgentStateReader(BIN, runner=runner)
        result = reader.list_agent_states()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_BINARY_NOT_FOUND)


class ResolverTest(unittest.TestCase):
    def test_default_tmux_returns_none(self) -> None:
        self.assertIsNone(
            resolve_agent_state_reader(TerminalTransportConfig.default(), env={})
        )

    def test_none_config_defaults_off(self) -> None:
        self.assertIsNone(resolve_agent_state_reader(None, env={}))

    def test_tmux_ignores_binary_env(self) -> None:
        self.assertIsNone(
            resolve_agent_state_reader(
                TerminalTransportConfig(backend="tmux"), env={HERDR_BINARY_ENV: BIN}
            )
        )

    def test_herdr_without_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_agent_state_reader(
                TerminalTransportConfig(backend=BACKEND_HERDR), env={}
            )
        self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_herdr_with_unresolvable_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_agent_state_reader(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: "/nonexistent/path/to/herdr"},
            )
        self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)

    def test_herdr_with_resolvable_binary_returns_reader(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binpath = os.path.join(tmp, "herdr")
            with open(binpath, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IXUSR)
            reader = resolve_agent_state_reader(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: binpath},
            )
            self.assertIsInstance(reader, HerdrCliAgentStateReader)

    def test_bare_name_env_without_path_fails_closed(self) -> None:
        # Same trusted-env PATH-key fail-closed rule as the transport resolver
        # (#13245 finding, j#72305): a trusted env with no PATH key does not fall
        # back to the ambient PATH.
        import tempfile

        with tempfile.TemporaryDirectory() as ambient:
            ambient_bin = os.path.join(ambient, "herdr")
            with open(ambient_bin, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(ambient_bin, os.stat(ambient_bin).st_mode | stat.S_IXUSR)
            prev_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ambient + os.pathsep + prev_path
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_agent_state_reader(
                        TerminalTransportConfig(backend=BACKEND_HERDR),
                        env={HERDR_BINARY_ENV: "herdr"},
                    )
                self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)
            finally:
                os.environ["PATH"] = prev_path


if __name__ == "__main__":
    unittest.main()
