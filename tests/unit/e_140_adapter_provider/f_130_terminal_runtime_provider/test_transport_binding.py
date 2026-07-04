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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    REASON_INVALID_TARGET,
    SOURCE_VISIBLE,
    PaneReadResult,
    TerminalTransportConfig,
    TerminalTransportError,
    TransportResult,
    valid_target,
)


class FakePort:
    """An in-memory :class:`TerminalTransportPort` recording every primitive call.

    It enforces the **same** ``valid_target`` guard the live ``HerdrCliTransport``
    applies (Redmine #13253 j#72367), so a tmux pane id (``%N``) that reached the
    port un-translated fails ``invalid_target`` exactly as it would live — the fake
    can no longer mask the un-translated-target bug.
    """

    backend = BACKEND_HERDR

    def __init__(self, *, read_content="pane body", read_ok=True, send_ok=True):
        self.calls: list = []
        self._read_content = read_content
        self._read_ok = read_ok
        self._send_ok = send_ok

    def send_text(self, target: str, text: str) -> TransportResult:
        self.calls.append(("send_text", target, text))
        if not valid_target(target):
            return TransportResult.failure(REASON_INVALID_TARGET, f"invalid: {target!r}")
        return TransportResult.success() if self._send_ok else TransportResult.failure(
            "transport_error", "boom"
        )

    def send_keys(self, target: str, keys: str) -> TransportResult:
        self.calls.append(("send_keys", target, keys))
        if not valid_target(target):
            return TransportResult.failure(REASON_INVALID_TARGET, f"invalid: {target!r}")
        return TransportResult.success() if self._send_ok else TransportResult.failure(
            "transport_error", "boom"
        )

    def read_pane(self, target, *, source=SOURCE_VISIBLE, lines=None) -> PaneReadResult:
        self.calls.append(("read_pane", target, source, lines))
        if not valid_target(target):
            return PaneReadResult.failure(REASON_INVALID_TARGET, f"invalid: {target!r}")
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

    def test_select_pane_valid_target_is_noop_success(self) -> None:
        # #12597 activate / restore: herdr lands without pane focus, so select-pane
        # is a no-op success — it never reaches the port and never fails the send.
        port = FakePort()
        binding = self._herdr_binding(port)
        completed = binding.run_tmux("select-pane", "-t", "%2")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(port.calls, [])

    def test_select_pane_restore_target_is_noop_success(self) -> None:
        # The restore-focus tail issues the same select-pane shape against the
        # previously-active pane; it is the same no-op.
        port = FakePort()
        binding = self._herdr_binding(port)
        binding.run_tmux("select-pane", "-t", "%3")
        self.assertEqual(port.calls, [])

    def test_select_pane_malformed_target_fails_closed(self) -> None:
        port = FakePort()
        binding = self._herdr_binding(port)
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("select-pane", "-t", "bad target")
        self.assertEqual(port.calls, [])

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


class HerdrTargetTranslationTest(unittest.TestCase):
    """The j#72373 fix: the rail's tmux ``%N`` is translated to *that target pane's* locator."""

    TARGET_NAME = encode_assigned_name("ws-1", "claude", "default")
    SENDER_NAME = encode_assigned_name("ws-sender", "codex", "default")

    def _binding(self, port, *, resolve_assigned_name=None, list_agents=None):
        run_tmux, capture_pane = _sentinel_tmux()
        return resolve_runtime_transport_binding(
            TerminalTransportConfig(backend=BACKEND_HERDR),
            tmux_run_tmux=run_tmux,
            tmux_capture_pane=capture_pane,
            port=port,
            resolve_assigned_name=resolve_assigned_name,
            list_agents=list_agents,
        )

    def test_untranslated_tmux_id_is_rejected_by_the_guarded_port(self) -> None:
        # Pin the bug: a raw %N reaching the (now faithful) port fails invalid_target.
        port = FakePort()
        result = port.send_text("%2", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_INVALID_TARGET)

    def test_identity_resolver_is_called_with_the_actual_target(self) -> None:
        # The name comes from resolving *the target*, not from a pre-minted constant.
        port = FakePort()
        seen = []

        def resolve(target):
            seen.append(target)
            return self.TARGET_NAME

        rows = [{"name": self.TARGET_NAME, "pane": "w1:p1"}]
        binding = self._binding(port, resolve_assigned_name=resolve, list_agents=lambda: rows)
        binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        self.assertEqual(seen, ["%2"])
        self.assertEqual(port.calls, [("send_text", "w1:p1", "body")])

    def test_target_pane_name_wins_over_a_sender_name_in_the_list(self) -> None:
        # The minimal j#72372/j#72373 reproduction: the agent list carries BOTH the
        # sender's and the target pane's rows; delivery must land on the TARGET's
        # locator (resolved from the target pane identity), never the sender's.
        port = FakePort()
        rows = [
            {"name": self.SENDER_NAME, "pane": "wS:pS"},
            {"name": self.TARGET_NAME, "pane": "w1:p1"},
        ]
        binding = self._binding(
            port,
            resolve_assigned_name=lambda target: self.TARGET_NAME,
            list_agents=lambda: rows,
        )
        binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        binding.run_tmux("send-keys", "-t", "%2", "Enter")
        self.assertEqual([c[1] for c in port.calls], ["w1:p1", "w1:p1"])
        self.assertNotIn("wS:pS", [c[1] for c in port.calls])

    def test_sender_only_list_fails_closed_no_wrong_send(self) -> None:
        # If only the sender's row exists (the target pane has not registered a herdr
        # name), the target-name re-bind is not-found -> fail closed, never a send to
        # the sender's locator.
        port = FakePort()
        rows = [{"name": self.SENDER_NAME, "pane": "wS:pS"}]
        binding = self._binding(
            port,
            resolve_assigned_name=lambda target: self.TARGET_NAME,
            list_agents=lambda: rows,
        )
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        self.assertEqual(port.calls, [])

    def test_capture_target_translated_to_locator(self) -> None:
        port = FakePort(read_content="x")
        rows = [{"name": self.TARGET_NAME, "location": "w1:p1"}]
        binding = self._binding(
            port, resolve_assigned_name=lambda t: self.TARGET_NAME, list_agents=lambda: rows
        )
        binding.capture_pane("%2", 50)
        self.assertEqual(port.calls, [("read_pane", "w1:p1", SOURCE_VISIBLE, 50)])

    def test_translation_is_memoised_per_target(self) -> None:
        port = FakePort()
        rows = [{"name": self.TARGET_NAME, "pane": "w1:p1"}]
        fetches = {"n": 0}
        resolves = {"n": 0}

        def list_agents():
            fetches["n"] += 1
            return rows

        def resolve(_target):
            resolves["n"] += 1
            return self.TARGET_NAME

        binding = self._binding(port, resolve_assigned_name=resolve, list_agents=list_agents)
        binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        binding.run_tmux("send-keys", "-t", "%2", "Enter")
        binding.capture_pane("%2", 50)
        self.assertEqual(fetches["n"], 1)  # memoised per target
        self.assertEqual(resolves["n"], 1)

    def test_already_herdr_valid_target_passes_through_without_resolving(self) -> None:
        port = FakePort()

        def must_not_run(*_a):
            raise AssertionError("a herdr-valid target must not resolve identity / fetch")

        binding = self._binding(
            port, resolve_assigned_name=must_not_run, list_agents=must_not_run
        )
        binding.run_tmux("send-keys", "-t", "w9:p9", "-l", "--", "body")
        self.assertEqual(port.calls, [("send_text", "w9:p9", "body")])

    def test_rebind_not_found_fails_closed_before_the_port(self) -> None:
        port = FakePort()
        binding = self._binding(
            port, resolve_assigned_name=lambda t: self.TARGET_NAME, list_agents=lambda: []
        )
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        self.assertEqual(port.calls, [])  # nothing was sent

    def test_unresolvable_target_identity_fails_closed(self) -> None:
        # resolve_assigned_name raising (an unregistered / unknown target pane) must
        # fail closed before any port call.
        port = FakePort()

        def resolve(target):
            raise TransportBindingError(f"no identity for {target}")

        binding = self._binding(
            port, resolve_assigned_name=resolve, list_agents=lambda: []
        )
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        self.assertEqual(port.calls, [])

    def test_no_identity_resolver_fails_closed_on_tmux_target(self) -> None:
        port = FakePort()
        binding = self._binding(port)  # resolve_assigned_name / list_agents both absent
        with self.assertRaises(TransportBindingError):
            binding.run_tmux("send-keys", "-t", "%2", "-l", "--", "body")
        self.assertEqual(port.calls, [])


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
