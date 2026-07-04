"""Runtime transport binding tests — the tmux/herdr backend-selection seam (Redmine #13253).

Pins the pure ``config -> TransportBinding`` resolver and the tmux-shaped herdr
shim *without a live herdr binary*:

- the tmux backend (default) returns the injected tmux callables **unchanged**
  (byte-for-byte identity), so the handoff rail's send is untouched;
- the herdr backend maps the four tmux argv shapes the rail emits onto the
  transport port's ``send_text`` / ``send_keys`` / ``read_pane`` primitives;
- an unmapped tmux subcommand and a failed port primitive both **fail closed**
  (a raised ``TransportBindingError``), never a silent no-op or tmux fallback;
- a herdr selection with no trusted-env binary fails closed at resolution.

The port is an in-memory fake, so no subprocess spawns.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    TransportBinding,
    TransportBindingError,
    resolve_runtime_transport_binding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    SOURCE_VISIBLE,
    PaneReadResult,
    TerminalTransportConfig,
    TerminalTransportError,
    TransportResult,
)


class FakePort:
    """An in-memory :class:`TerminalTransportPort` recording every primitive call."""

    backend = BACKEND_HERDR

    def __init__(self, *, read_content="pane body", read_ok=True, send_ok=True):
        self.calls: list = []
        self._read_content = read_content
        self._read_ok = read_ok
        self._send_ok = send_ok

    def send_text(self, target: str, text: str) -> TransportResult:
        self.calls.append(("send_text", target, text))
        return TransportResult.success() if self._send_ok else TransportResult.failure(
            "transport_error", "boom"
        )

    def send_keys(self, target: str, keys: str) -> TransportResult:
        self.calls.append(("send_keys", target, keys))
        return TransportResult.success() if self._send_ok else TransportResult.failure(
            "transport_error", "boom"
        )

    def read_pane(self, target, *, source=SOURCE_VISIBLE, lines=None) -> PaneReadResult:
        self.calls.append(("read_pane", target, source, lines))
        if not self._read_ok:
            return PaneReadResult.failure("transport_error", "boom")
        return PaneReadResult.success(self._read_content)


def _sentinel_tmux():
    """Two distinct sentinel callables standing in for the tmux client primitives."""

    def run_tmux(*args, check=True):
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    def capture_pane(target, lines):
        return "tmux-capture"

    return run_tmux, capture_pane


class TmuxTransparencyTest(unittest.TestCase):
    def test_default_none_config_returns_tmux_passthrough_identity(self) -> None:
        run_tmux, capture_pane = _sentinel_tmux()
        binding = resolve_runtime_transport_binding(
            None, tmux_run_tmux=run_tmux, tmux_capture_pane=capture_pane
        )
        self.assertIsInstance(binding, TransportBinding)
        self.assertEqual(binding.backend, BACKEND_TMUX)
        # Byte-for-byte transparency: the SAME callable objects are returned, so
        # the handoff rail runs the exact tmux primitives it always did.
        self.assertIs(binding.run_tmux, run_tmux)
        self.assertIs(binding.capture_pane, capture_pane)

    def test_explicit_tmux_config_returns_tmux_passthrough(self) -> None:
        run_tmux, capture_pane = _sentinel_tmux()
        binding = resolve_runtime_transport_binding(
            TerminalTransportConfig(backend=BACKEND_TMUX),
            tmux_run_tmux=run_tmux,
            tmux_capture_pane=capture_pane,
        )
        self.assertEqual(binding.backend, BACKEND_TMUX)
        self.assertIs(binding.run_tmux, run_tmux)
        self.assertIs(binding.capture_pane, capture_pane)

    def test_tmux_backend_never_resolves_a_port(self) -> None:
        # Even with a port supplied, the tmux backend ignores it (no shim built).
        run_tmux, capture_pane = _sentinel_tmux()
        port = FakePort()
        binding = resolve_runtime_transport_binding(
            TerminalTransportConfig(backend=BACKEND_TMUX),
            tmux_run_tmux=run_tmux,
            tmux_capture_pane=capture_pane,
            port=port,
        )
        self.assertIs(binding.run_tmux, run_tmux)
        self.assertEqual(port.calls, [])


class HerdrMappingTest(unittest.TestCase):
    def _herdr_binding(self, port):
        run_tmux, capture_pane = _sentinel_tmux()
        return resolve_runtime_transport_binding(
            TerminalTransportConfig(backend=BACKEND_HERDR),
            tmux_run_tmux=run_tmux,
            tmux_capture_pane=capture_pane,
            port=port,
        )

    def test_backend_is_herdr(self) -> None:
        binding = self._herdr_binding(FakePort())
        self.assertEqual(binding.backend, BACKEND_HERDR)

    def test_literal_send_text_maps_to_send_text(self) -> None:
        port = FakePort()
        binding = self._herdr_binding(port)
        completed = binding.run_tmux("send-keys", "-t", "w1:p1", "-l", "--", "MARK body")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(port.calls, [("send_text", "w1:p1", "MARK body")])

    def test_enter_maps_to_send_keys_enter(self) -> None:
        port = FakePort()
        binding = self._herdr_binding(port)
        binding.run_tmux("send-keys", "-t", "w1:p1", "Enter")
        self.assertEqual(port.calls, [("send_keys", "w1:p1", "enter")])

    def test_c_u_maps_to_send_keys_rollback(self) -> None:
        port = FakePort()
        binding = self._herdr_binding(port)
        binding.run_tmux("send-keys", "-t", "w1:p1", "C-u")
        self.assertEqual(port.calls, [("send_keys", "w1:p1", "C-u")])

    def test_capture_pane_maps_to_read_pane_visible(self) -> None:
        port = FakePort(read_content="rendered pane")
        binding = self._herdr_binding(port)
        content = binding.capture_pane("w1:p1", 50)
        self.assertEqual(content, "rendered pane")
        self.assertEqual(port.calls, [("read_pane", "w1:p1", SOURCE_VISIBLE, 50)])

    def test_unmapped_subcommand_fails_closed(self) -> None:
        port = FakePort()
        binding = self._herdr_binding(port)
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("list-panes", "-a")
        # Nothing was sent — the shim refused rather than silently dropping it.
        self.assertEqual(port.calls, [])

    def test_unmapped_send_keys_shape_fails_closed(self) -> None:
        port = FakePort()
        binding = self._herdr_binding(port)
        # A send-keys token the rail never emits (not literal text / Enter / C-u).
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("send-keys", "-t", "w1:p1", "C-c")
        self.assertEqual(port.calls, [])

    def test_send_failure_fails_closed(self) -> None:
        port = FakePort(send_ok=False)
        binding = self._herdr_binding(port)
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("send-keys", "-t", "w1:p1", "-l", "--", "body")

    def test_read_failure_fails_closed(self) -> None:
        port = FakePort(read_ok=False)
        binding = self._herdr_binding(port)
        with self.assertRaises(TransportBindingError):
            binding.capture_pane("w1:p1", 50)


class HerdrResolutionFailClosedTest(unittest.TestCase):
    def test_herdr_selected_without_binary_fails_closed(self) -> None:
        run_tmux, capture_pane = _sentinel_tmux()
        # No injected port and an empty trusted environment -> the #13245 resolver
        # raises TerminalTransportError (binary_unconfigured); no tmux fallback.
        with self.assertRaises(TerminalTransportError):
            resolve_runtime_transport_binding(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                tmux_run_tmux=run_tmux,
                tmux_capture_pane=capture_pane,
                env={},
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
