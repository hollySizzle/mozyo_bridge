"""Fake-port unit tests for the tmux-config OOP-first boundary (Redmine #12749 / #12638 / #12785).

These tests drive :class:`ApplyTmuxConfigUseCase` through a *fake port* that
implements :class:`TmuxControlPort`, instead of monkeypatching
``mozyo_bridge.application.commands.require_tmux`` / ``source_tmux_conf`` (the
seam the old procedural ``cmd_config`` forced). This establishes the first
fake-port specification test for the ``commands.py`` OOP-first decomposition:
the use case's external boundary is injected, so its contract (availability is
checked before sourcing; the resolved expanded path is reported) is expressed
without any real tmux process or function patch.
"""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from mozyo_bridge.application.commands_tmux_ui import (
    ApplyTmuxConfigUseCase,
    TmuxConfigRequest,
    TmuxConfigResult,
    _config_request,
)
from mozyo_bridge.application.tmux_control_port import (
    LiveTmuxControlPort,
    TmuxControlPort,
)


class FakeTmuxControlPort:
    """Test double for :class:`TmuxControlPort`.

    Records the boundary interactions the use case performs so a test can assert
    the contract (availability guard runs before the conf is sourced) without a
    live tmux server or a function monkeypatch. ``available=False`` simulates the
    live adapter's fail-closed ``SystemExit`` when tmux is not on PATH.
    """

    def __init__(self, *, available: bool = True, source_returns: bool = True) -> None:
        self._available = available
        self._source_returns = source_returns
        self.require_calls = 0
        self.sourced: list[tuple[str, bool]] = []

    def require_available(self) -> None:
        self.require_calls += 1
        if not self._available:
            raise SystemExit("tmux is not installed or not in PATH")

    def source_conf(self, path: str, *, optional: bool = False) -> bool:
        self.sourced.append((path, optional))
        return self._source_returns


class ApplyTmuxConfigUseCaseTest(unittest.TestCase):
    def test_fake_port_satisfies_protocol(self) -> None:
        # runtime_checkable Protocol: the fake is a structural TmuxControlPort.
        self.assertIsInstance(FakeTmuxControlPort(), TmuxControlPort)

    def test_checks_availability_then_sources_and_reports_expanded_path(self) -> None:
        port = FakeTmuxControlPort()
        use_case = ApplyTmuxConfigUseCase(port)

        result = use_case.execute(TmuxConfigRequest(config_path="~/cfg/tmux.conf"))

        self.assertIsInstance(result, TmuxConfigResult)
        # Availability is guarded exactly once, before sourcing.
        self.assertEqual(port.require_calls, 1)
        # The raw request path is sourced (optional defaults False, matching the
        # original explicit-path handler behavior).
        self.assertEqual(port.sourced, [("~/cfg/tmux.conf", False)])
        # The reported path is user-expanded, byte-compatible with the legacy
        # ``print(f"loaded tmux config: {Path(path).expanduser()}")`` line.
        self.assertEqual(result.loaded_path, str(Path("~/cfg/tmux.conf").expanduser()))

    def test_fails_closed_without_sourcing_when_tmux_unavailable(self) -> None:
        port = FakeTmuxControlPort(available=False)
        use_case = ApplyTmuxConfigUseCase(port)

        with self.assertRaises(SystemExit):
            use_case.execute(TmuxConfigRequest(config_path="/etc/tmux.conf"))

        # Boundary contract: a missing tmux aborts before any source-file call.
        self.assertEqual(port.require_calls, 1)
        self.assertEqual(port.sourced, [])

    def test_request_and_result_value_objects_are_frozen(self) -> None:
        request = TmuxConfigRequest(config_path="/tmp/x.conf")
        result = TmuxConfigResult(loaded_path="/tmp/x.conf")
        with self.assertRaises(Exception):
            request.config_path = "/other"  # type: ignore[misc]
        with self.assertRaises(Exception):
            result.loaded_path = "/other"  # type: ignore[misc]


class ConfigRequestBuildingTest(unittest.TestCase):
    """The CLI edge reads ``argparse.Namespace`` once into a typed request."""

    def test_explicit_path_wins(self) -> None:
        args = argparse.Namespace(path="/explicit/tmux.conf")
        self.assertEqual(
            _config_request(args),
            TmuxConfigRequest(config_path="/explicit/tmux.conf"),
        )

    def test_falls_back_to_resolved_config_path(self) -> None:
        # No --path: the request resolves through config_path_from_args, so the
        # Namespace does not leak past the CLI edge into the use case.
        args = argparse.Namespace(path=None, config_path="~/fallback.conf", repo=None)
        request = _config_request(args)
        self.assertEqual(request.config_path, str(Path("~/fallback.conf").expanduser()))


class LiveTmuxControlPortTest(unittest.TestCase):
    def test_live_adapter_is_a_tmux_control_port(self) -> None:
        # The live adapter conforms structurally; its methods delegate to the
        # infrastructure tmux_client wrappers (exercised by the integration
        # suite, not shelled out here).
        self.assertIsInstance(LiveTmuxControlPort(), TmuxControlPort)


if __name__ == "__main__":
    unittest.main()
