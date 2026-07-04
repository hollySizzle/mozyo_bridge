"""Terminal-transport port + backend selection seam tests (Redmine #13245).

Pins the first concrete cut of the built-in terminal runtime adapter boundary
(Redmine #12001 design doc, "Terminal runtime adapter"): the core-owned backend
/ source / reason vocabularies, the fail-closed result records and their
ok/reason invariant, the target guard, the three-primitive
:class:`TerminalTransportPort` protocol, and the default-off
:class:`TerminalTransportConfig`. A tiny in-memory fake stands in for a live
provider so the port contract is exercised with no herdr binary and no
subprocess. No network / tmux / herdr is touched here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    DEFAULT_PANE_READ_SOURCE,
    DEFAULT_TERMINAL_BACKEND,
    PANE_READ_SOURCES,
    REASON_INVALID_SOURCE,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    SOURCE_RECENT_UNWRAPPED,
    SOURCE_VISIBLE,
    TERMINAL_TRANSPORT_BACKENDS,
    TRANSPORT_FAILURE_REASONS,
    PaneReadResult,
    TerminalTransportConfig,
    TerminalTransportError,
    TerminalTransportPort,
    TransportResult,
    valid_target,
)


class FakeTerminalTransport:
    """In-memory :class:`TerminalTransportPort` for contract tests (no binary).

    Records every call and returns pre-seeded results, so a test can exercise
    all three primitives and each fail-closed reason without a herdr process.
    """

    def __init__(self, backend: str = BACKEND_HERDR):
        self.backend = backend
        self.calls: list = []
        self._send_result = TransportResult.success()
        self._read_result = PaneReadResult.success("screen", truncated=False)

    def seed_send(self, result: TransportResult) -> None:
        self._send_result = result

    def seed_read(self, result: PaneReadResult) -> None:
        self._read_result = result

    def send_text(self, target: str, text: str) -> TransportResult:
        if not valid_target(target):
            return TransportResult.failure(REASON_INVALID_TARGET, "bad target")
        self.calls.append(("send_text", target, text))
        return self._send_result

    def send_keys(self, target: str, keys: str) -> TransportResult:
        if not valid_target(target):
            return TransportResult.failure(REASON_INVALID_TARGET, "bad target")
        self.calls.append(("send_keys", target, keys))
        return self._send_result

    def read_pane(self, target, *, source=DEFAULT_PANE_READ_SOURCE, lines=None):
        if not valid_target(target):
            return PaneReadResult.failure(REASON_INVALID_TARGET, "bad target")
        if source not in PANE_READ_SOURCES:
            return PaneReadResult.failure(REASON_INVALID_SOURCE, "bad source")
        self.calls.append(("read_pane", target, source, lines))
        return self._read_result


class VocabularyTest(unittest.TestCase):
    def test_backends(self) -> None:
        self.assertEqual(TERMINAL_TRANSPORT_BACKENDS, {BACKEND_TMUX, BACKEND_HERDR})
        self.assertEqual(DEFAULT_TERMINAL_BACKEND, BACKEND_TMUX)

    def test_default_read_source_is_visible(self) -> None:
        self.assertEqual(DEFAULT_PANE_READ_SOURCE, SOURCE_VISIBLE)
        self.assertIn(SOURCE_RECENT_UNWRAPPED, PANE_READ_SOURCES)

    def test_failure_reasons_closed(self) -> None:
        for reason in (
            REASON_INVALID_TARGET,
            REASON_INVALID_SOURCE,
            REASON_TRANSPORT_ERROR,
        ):
            self.assertIn(reason, TRANSPORT_FAILURE_REASONS)


class TargetGuardTest(unittest.TestCase):
    def test_valid_targets(self) -> None:
        for good in ("w1:p1", "poc_claude", "a.b-c", "0", "win:0.1"):
            self.assertTrue(valid_target(good), good)

    def test_invalid_targets(self) -> None:
        for bad in ("", " ", "a b", "--flag", "a;rm", "a|b", 123, None, ":lead"):
            self.assertFalse(valid_target(bad), repr(bad))


class ResultInvariantTest(unittest.TestCase):
    def test_success_has_no_reason(self) -> None:
        result = TransportResult.success("done")
        self.assertTrue(result.ok)
        self.assertIsNone(result.reason)

    def test_failure_requires_valid_reason(self) -> None:
        result = TransportResult.failure(REASON_TRANSPORT_ERROR, "boom")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)

    def test_success_with_reason_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TransportResult(ok=True, reason=REASON_TRANSPORT_ERROR)

    def test_failure_without_reason_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TransportResult(ok=False, reason=None)

    def test_failure_with_unknown_reason_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TransportResult(ok=False, reason="not_a_reason")

    def test_read_success_carries_content(self) -> None:
        result = PaneReadResult.success("3965 bytes", truncated=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "3965 bytes")
        self.assertTrue(result.truncated)

    def test_read_failure_has_no_content(self) -> None:
        result = PaneReadResult.failure(REASON_INVALID_SOURCE, "bad")
        self.assertFalse(result.ok)
        self.assertIsNone(result.content)

    def test_error_rejects_unknown_reason(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportError("x", reason="mystery")


class PortContractTest(unittest.TestCase):
    """Every primitive + each fail-closed path, via the in-memory fake."""

    def setUp(self) -> None:
        self.fake = FakeTerminalTransport()

    def test_fake_satisfies_protocol(self) -> None:
        self.assertIsInstance(self.fake, TerminalTransportPort)

    def test_send_text_roundtrip(self) -> None:
        result = self.fake.send_text("w1:p1", "hello")
        self.assertTrue(result.ok)
        self.assertIn(("send_text", "w1:p1", "hello"), self.fake.calls)

    def test_send_keys_roundtrip(self) -> None:
        result = self.fake.send_keys("w1:p1", "enter")
        self.assertTrue(result.ok)
        self.assertIn(("send_keys", "w1:p1", "enter"), self.fake.calls)

    def test_read_pane_roundtrip(self) -> None:
        self.fake.seed_read(PaneReadResult.success("visible text"))
        result = self.fake.read_pane("poc_claude", source=SOURCE_VISIBLE, lines=30)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "visible text")
        self.assertIn(("read_pane", "poc_claude", SOURCE_VISIBLE, 30), self.fake.calls)

    def test_invalid_target_fails_closed_on_every_primitive(self) -> None:
        self.assertEqual(self.fake.send_text("bad target", "x").reason, REASON_INVALID_TARGET)
        self.assertEqual(self.fake.send_keys("bad target", "enter").reason, REASON_INVALID_TARGET)
        self.assertEqual(self.fake.read_pane("bad target").reason, REASON_INVALID_TARGET)
        self.assertEqual(self.fake.calls, [])

    def test_invalid_source_fails_closed(self) -> None:
        self.assertEqual(
            self.fake.read_pane("w1:p1", source="nope").reason, REASON_INVALID_SOURCE
        )

    def test_seeded_send_failure_propagates(self) -> None:
        self.fake.seed_send(TransportResult.failure(REASON_TRANSPORT_ERROR, "down"))
        self.assertFalse(self.fake.send_text("w1:p1", "x").ok)


class ConfigTest(unittest.TestCase):
    def test_default_is_tmux_off(self) -> None:
        config = TerminalTransportConfig.default()
        self.assertEqual(config.backend, BACKEND_TMUX)
        self.assertFalse(config.herdr_enabled)

    def test_none_and_empty_are_default(self) -> None:
        self.assertEqual(TerminalTransportConfig.from_record(None).backend, BACKEND_TMUX)
        self.assertEqual(TerminalTransportConfig.from_record({}).backend, BACKEND_TMUX)

    def test_explicit_herdr_selected(self) -> None:
        config = TerminalTransportConfig.from_record({"backend": "herdr"})
        self.assertEqual(config.backend, BACKEND_HERDR)
        self.assertTrue(config.herdr_enabled)

    def test_explicit_version_accepted(self) -> None:
        config = TerminalTransportConfig.from_record({"version": 1, "backend": "tmux"})
        self.assertEqual(config.backend, BACKEND_TMUX)

    def test_unknown_backend_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportConfig.from_record({"backend": "ssh"})

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportConfig.from_record({"herdr_binary": "/opt/herdr"})

    def test_non_mapping_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportConfig.from_record(["herdr"])

    def test_unsupported_version_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportConfig.from_record({"version": 2})

    def test_version_bool_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportConfig.from_record({"version": True})

    def test_non_string_backend_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            TerminalTransportConfig.from_record({"backend": 3})

    def test_config_frozen(self) -> None:
        config = TerminalTransportConfig.default()
        with self.assertRaises(Exception):
            config.backend = "herdr"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
