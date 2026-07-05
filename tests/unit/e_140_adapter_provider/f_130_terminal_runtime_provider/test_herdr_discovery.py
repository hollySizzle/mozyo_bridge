"""herdr CLI agent discovery lister tests (Redmine #13261).

Pins the ``agent list`` provider primitive through an injected subprocess ``runner``
(no live herdr binary): a recognised payload yields the raw rows; every mechanical
failure and an unrecognisable payload fail closed with a ``TerminalTransportError``.
"""

from __future__ import annotations

import json
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
    REASON_INVALID_PAYLOAD,
    REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    HerdrCliAgentLister,
    resolve_agent_lister,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


def _runner(*, stdout="", returncode=0, exc=None, expect_argv=None):
    def run(argv, capture_output=None, text=None, timeout=None, **kw):
        if expect_argv is not None:
            assert list(argv) == expect_argv, argv
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="err")

    return run


class HerdrCliAgentListerTest(unittest.TestCase):
    def test_returns_rows_on_recognised_payload(self) -> None:
        rows = [{"name": "mzb1_x_claude_default", "pane_id": "w1:p1"}]
        lister = HerdrCliAgentLister(
            "/bin/herdr",
            runner=_runner(
                stdout=json.dumps({"agents": rows}),
                expect_argv=["/bin/herdr", "agent", "list"],
            ),
        )
        self.assertEqual(list(lister.list_agent_rows()), rows)

    def test_bare_array_payload(self) -> None:
        rows = [{"name": "n", "pane_id": "w1:p1"}]
        lister = HerdrCliAgentLister("/bin/herdr", runner=_runner(stdout=json.dumps(rows)))
        self.assertEqual(list(lister.list_agent_rows()), rows)

    def test_binary_not_found_fails_closed(self) -> None:
        lister = HerdrCliAgentLister("/bin/herdr", runner=_runner(exc=FileNotFoundError()))
        with self.assertRaises(TerminalTransportError) as ctx:
            lister.list_agent_rows()
        self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)

    def test_timeout_fails_closed(self) -> None:
        lister = HerdrCliAgentLister(
            "/bin/herdr", runner=_runner(exc=subprocess.TimeoutExpired("herdr", 1))
        )
        with self.assertRaises(TerminalTransportError) as ctx:
            lister.list_agent_rows()
        self.assertEqual(ctx.exception.reason, REASON_TRANSPORT_ERROR)

    def test_nonzero_exit_fails_closed(self) -> None:
        lister = HerdrCliAgentLister("/bin/herdr", runner=_runner(returncode=2))
        with self.assertRaises(TerminalTransportError) as ctx:
            lister.list_agent_rows()
        self.assertEqual(ctx.exception.reason, REASON_TRANSPORT_ERROR)

    def test_unrecognised_payload_fails_closed(self) -> None:
        lister = HerdrCliAgentLister("/bin/herdr", runner=_runner(stdout="42"))
        with self.assertRaises(TerminalTransportError) as ctx:
            lister.list_agent_rows()
        self.assertEqual(ctx.exception.reason, REASON_INVALID_PAYLOAD)


class ResolveAgentListerTest(unittest.TestCase):
    def test_tmux_backend_returns_none(self) -> None:
        self.assertIsNone(resolve_agent_lister(TerminalTransportConfig.default()))

    def test_herdr_unconfigured_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_agent_lister(TerminalTransportConfig(backend=BACKEND_HERDR), env={})
        self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_herdr_unresolvable_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_agent_lister(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_ENV: "/nonexistent/herdr-binary"},
            )
        self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
