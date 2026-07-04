"""Built-in herdr CLI transport adapter tests (Redmine #13245).

Pins the pure, fail-closed herdr CLI adapter and its default-off selection
resolver *without a live herdr binary*: argv construction for each primitive is
verified through an injected subprocess ``runner``, and the fail-closed paths
(malformed target, missing binary, non-zero exit, timeout) are simulated. The
resolver is pinned for every branch: tmux/off returns ``None``; herdr with no
trusted-env binary, an unresolvable binary, and a resolvable binary each behave
per the seam contract with no silent fallback to tmux. No subprocess spawns.
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

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_INVALID_SOURCE,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    SOURCE_VISIBLE,
    TerminalTransportConfig,
    TerminalTransportError,
    TerminalTransportPort,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
    HerdrCliTransport,
    resolve_terminal_transport,
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


class ArgvConstructionTest(unittest.TestCase):
    def test_send_text_argv(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.send_text("w1:p1", "hello world")
        self.assertTrue(result.ok)
        argv = runner.calls[0][0]
        self.assertEqual(argv, [BIN, "pane", "send-text", "w1:p1", "hello world"])

    def test_send_keys_argv(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.send_keys("w1:p1", "enter")
        self.assertTrue(result.ok)
        self.assertEqual(runner.calls[0][0], [BIN, "pane", "send-keys", "w1:p1", "enter"])

    def test_read_pane_argv_with_lines(self) -> None:
        runner = RecordingRunner(stdout="raw screen text")
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("poc_claude", source=SOURCE_VISIBLE, lines=30)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "raw screen text")
        self.assertEqual(
            runner.calls[0][0],
            [BIN, "agent", "read", "poc_claude", "--source", "visible", "--lines", "30"],
        )

    def test_read_pane_argv_without_lines(self) -> None:
        runner = RecordingRunner(stdout="x")
        transport = HerdrCliTransport(BIN, runner=runner)
        transport.read_pane("poc_claude")
        self.assertEqual(
            runner.calls[0][0], [BIN, "agent", "read", "poc_claude", "--source", "visible"]
        )

    def test_read_pane_parses_json_payload(self) -> None:
        runner = RecordingRunner(stdout='{"content": "hi there", "truncated": true}')
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("poc_claude")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "hi there")
        self.assertTrue(result.truncated)


class FailClosedTest(unittest.TestCase):
    def test_invalid_target_never_spawns(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.send_text("bad target", "x").reason, REASON_INVALID_TARGET)
        self.assertEqual(transport.send_keys("--flag", "enter").reason, REASON_INVALID_TARGET)
        self.assertEqual(transport.read_pane("a;b").reason, REASON_INVALID_TARGET)
        self.assertEqual(runner.calls, [])

    def test_invalid_source_never_spawns(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.read_pane("w1:p1", source="nope").reason, REASON_INVALID_SOURCE)
        self.assertEqual(runner.calls, [])

    def test_non_str_source_fails_closed_without_spawn(self) -> None:
        # Finding 1 (j#72296): an unhashable / non-str source must not raise a
        # TypeError from the membership test; it fails closed as invalid_source
        # and never spawns a subprocess.
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        for bad in ([], {}, 5, None, ("visible",)):
            with self.subTest(bad=bad):
                result = transport.read_pane("w1:p1", source=bad)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, REASON_INVALID_SOURCE)
        self.assertEqual(runner.calls, [])

    def test_bad_lines_never_spawns(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.read_pane("w1:p1", lines=0).reason, REASON_INVALID_TARGET)
        self.assertEqual(transport.read_pane("w1:p1", lines=True).reason, REASON_INVALID_TARGET)
        self.assertEqual(runner.calls, [])

    def test_nonzero_exit_is_transport_error(self) -> None:
        runner = RecordingRunner(returncode=1, stderr="no such pane")
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.send_text("w1:p1", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)
        self.assertIn("no such pane", result.detail)

    def test_read_nonzero_exit_is_transport_error(self) -> None:
        runner = RecordingRunner(returncode=2, stderr="boom")
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("w1:p1")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)

    def test_missing_binary_is_binary_not_found(self) -> None:
        runner = RecordingRunner(raises=FileNotFoundError())
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.send_text("w1:p1", "x").reason, REASON_BINARY_NOT_FOUND)
        self.assertEqual(transport.read_pane("w1:p1").reason, REASON_BINARY_NOT_FOUND)

    def test_timeout_is_transport_error(self) -> None:
        runner = RecordingRunner(raises=subprocess.TimeoutExpired(BIN, 10))
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.send_keys("w1:p1", "enter").reason, REASON_TRANSPORT_ERROR)

    def test_empty_binary_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            HerdrCliTransport("")

    def test_transport_satisfies_protocol(self) -> None:
        self.assertIsInstance(HerdrCliTransport(BIN, runner=RecordingRunner()), TerminalTransportPort)


class ResolverTest(unittest.TestCase):
    def test_default_tmux_returns_none(self) -> None:
        self.assertIsNone(resolve_terminal_transport(TerminalTransportConfig.default(), env={}))

    def test_tmux_ignores_binary_env(self) -> None:
        self.assertIsNone(
            resolve_terminal_transport(
                TerminalTransportConfig(backend="tmux"), env={HERDR_BINARY_ENV: BIN}
            )
        )

    def test_herdr_without_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_terminal_transport(TerminalTransportConfig(backend=BACKEND_HERDR), env={})
        self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_herdr_with_unresolvable_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: "/nonexistent/path/to/herdr"},
            )
        self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)

    def test_herdr_with_resolvable_binary_returns_transport(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binpath = os.path.join(tmp, "herdr")
            with open(binpath, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IXUSR)
            transport = resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: binpath},
            )
            self.assertIsInstance(transport, HerdrCliTransport)
            self.assertEqual(transport.backend, BACKEND_HERDR)

    def test_none_config_defaults_to_off(self) -> None:
        self.assertIsNone(resolve_terminal_transport(None, env={}))

    def test_bare_name_resolves_on_trusted_env_path(self) -> None:
        # Finding 2 (j#72296): a bare binary name resolves against the *supplied
        # trusted env's* PATH, not the ambient process PATH.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binpath = os.path.join(tmp, "herdr")
            with open(binpath, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IXUSR)
            transport = resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: "herdr", "PATH": tmp},
            )
            self.assertIsInstance(transport, HerdrCliTransport)
            # Resolved to the executable inside the trusted-env PATH dir.
            self.assertEqual(transport._binary, binpath)

    def test_bare_name_not_on_trusted_env_path_fails_closed(self) -> None:
        # Finding 2 (j#72296): a bare name present only on the *ambient* PATH but
        # absent from the trusted-env PATH is NOT resolved — fail closed.
        import tempfile

        with tempfile.TemporaryDirectory() as ambient, tempfile.TemporaryDirectory() as trusted:
            # Put an executable ``herdr`` on the ambient PATH only.
            ambient_bin = os.path.join(ambient, "herdr")
            with open(ambient_bin, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(ambient_bin, os.stat(ambient_bin).st_mode | stat.S_IXUSR)
            prev_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ambient + os.pathsep + prev_path
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_terminal_transport(
                        TerminalTransportConfig(backend=BACKEND_HERDR),
                        # trusted PATH points at an empty dir (no herdr)
                        env={HERDR_BINARY_ENV: "herdr", "PATH": trusted},
                    )
                self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)
            finally:
                os.environ["PATH"] = prev_path

    def test_bare_name_env_without_path_fails_closed(self) -> None:
        # Finding 2 residual (j#72305): a supplied trusted env with NO ``PATH``
        # key must not fall back to the ambient ``PATH``. A bare name is
        # unresolvable against the empty path — fail closed.
        import tempfile

        with tempfile.TemporaryDirectory() as ambient:
            # Put an executable ``herdr`` on the ambient PATH only.
            ambient_bin = os.path.join(ambient, "herdr")
            with open(ambient_bin, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(ambient_bin, os.stat(ambient_bin).st_mode | stat.S_IXUSR)
            prev_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ambient + os.pathsep + prev_path
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_terminal_transport(
                        TerminalTransportConfig(backend=BACKEND_HERDR),
                        # trusted env carries no PATH key at all
                        env={HERDR_BINARY_ENV: "herdr"},
                    )
                self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)
            finally:
                os.environ["PATH"] = prev_path


if __name__ == "__main__":
    unittest.main()
