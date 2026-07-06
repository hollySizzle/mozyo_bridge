"""Fake-port / pure specifications for the backend-aware bare ``mozyo`` herdr
branch (Redmine #13324).

These exercise the herdr launch use case + pure policy with a synthetic
:class:`HerdrLaunchOps` — no live herdr binary, no ``os.execvp`` — and pin the
entrypoint routing that keeps the tmux path byte-invariant. They cover the
auditor ruling's focused-test list (j#73153):

- tmux default / config-absent routing stays on ``cmd_mozyo`` (byte-invariant),
  and only ``terminal_transport.backend: herdr`` routes to ``cmd_mozyo_herdr``;
- the herdr default path resolves the binary, runs session-start, and returns an
  attach plan; ``--no-attach`` and ``--json`` never attach;
- ``$TMUX`` + effective attach fails closed before session-start, while
  ``$TMUX`` + ``--no-attach`` is allowed;
- ``--cc`` / ``--session`` are rejected explicitly;
- a missing / unresolvable ``MOZYO_HERDR_BINARY`` fails closed with no tmux
  fallback, and a session-start failure fails closed the same way.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.application.herdr_launch_command import (
    HerdrLaunchOutcome,
    MozyoHerdrLaunchUseCase,
    build_herdr_json_payload,
    deliver_herdr_launch_outcome,
    herdr_attach_argv,
    herdr_attach_command_line,
    session_ready,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SLOT_PLANNED,
    HerdrSessionStartError,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    REASON_BINARY_UNCONFIGURED,
    TerminalTransportError,
)


def _ready_result() -> SessionStartResult:
    return SessionStartResult(
        workspace_id="ws1",
        lane_id="default",
        slots=[
            SlotResult(
                provider="claude",
                assigned_name="mzb1_ws1_claude_",
                outcome=SLOT_LAUNCHED,
                locator="w1:p1",
            ),
            SlotResult(
                provider="codex",
                assigned_name="mzb1_ws1_codex_",
                outcome=SLOT_ADOPTED,
                locator="w1:p2",
            ),
        ],
    )


def _args(**over) -> argparse.Namespace:
    base = dict(cc=False, session=None, json_output=False, no_attach=False, repo=None)
    base.update(over)
    return argparse.Namespace(**base)


class _Died(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Attached(Exception):
    pass


class _FakeHerdrOps:
    """A synthetic :class:`HerdrLaunchOps` recording every call."""

    def __init__(
        self,
        *,
        repo_root: Path = Path("/repo"),
        in_tmux: bool = False,
        binary: str = "/usr/local/bin/herdr",
        resolve_error: BaseException | None = None,
        prepare_result: SessionStartResult | None = None,
        prepare_error: BaseException | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._in_tmux = in_tmux
        self._binary = binary
        self._resolve_error = resolve_error
        self._prepare_result = prepare_result or _ready_result()
        self._prepare_error = prepare_error
        self.calls: list = []
        self.emitted: list = []
        self.attached: list | None = None

    def repo_root(self, args: argparse.Namespace) -> Path:
        self.calls.append("repo_root")
        return self._repo_root

    def in_tmux(self) -> bool:
        self.calls.append("in_tmux")
        return self._in_tmux

    def resolve_binary(self) -> str:
        self.calls.append("resolve_binary")
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._binary

    def prepare(self, repo_root: Path) -> SessionStartResult:
        self.calls.append(("prepare", repo_root))
        if self._prepare_error is not None:
            raise self._prepare_error
        return self._prepare_result

    def attach(self, argv: list) -> None:
        self.attached = list(argv)
        raise _Attached()

    def emit(self, text: str, end: str = "\n") -> None:
        self.emitted.append((text, end))

    def die(self, message: str) -> None:
        raise _Died(message)


# --- pure policy --------------------------------------------------------------


class HerdrLaunchPolicyTests(unittest.TestCase):
    def test_attach_command_and_argv_are_the_resolved_binary(self) -> None:
        self.assertEqual(herdr_attach_command_line("/opt/herdr"), "/opt/herdr")
        self.assertEqual(herdr_attach_argv("/opt/herdr"), ["/opt/herdr"])

    def test_session_ready_true_when_all_slots_live(self) -> None:
        self.assertTrue(session_ready(_ready_result()))

    def test_session_ready_false_on_empty_or_locatorless_or_planned(self) -> None:
        self.assertFalse(
            session_ready(SessionStartResult(workspace_id="w", lane_id="default"))
        )
        no_locator = SessionStartResult(
            workspace_id="w",
            lane_id="default",
            slots=[
                SlotResult(
                    provider="claude",
                    assigned_name="n",
                    outcome=SLOT_LAUNCHED,
                    locator="",
                )
            ],
        )
        self.assertFalse(session_ready(no_locator))
        planned = SessionStartResult(
            workspace_id="w",
            lane_id="default",
            slots=[
                SlotResult(
                    provider="claude",
                    assigned_name="n",
                    outcome=SLOT_PLANNED,
                    locator="w1:p1",
                )
            ],
        )
        self.assertFalse(session_ready(planned))

    def test_json_payload_shape(self) -> None:
        payload = build_herdr_json_payload(
            result=_ready_result(), attach_command="/usr/local/bin/herdr"
        )
        self.assertEqual(payload["backend"], BACKEND_HERDR)
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["attached"])
        self.assertTrue(payload["no_attach"])
        self.assertEqual(payload["attach"], "/usr/local/bin/herdr")
        self.assertEqual(payload["session_start"]["workspace_id"], "ws1")
        self.assertEqual(len(payload["session_start"]["slots"]), 2)


# --- use case decision --------------------------------------------------------


class MozyoHerdrLaunchUseCaseTests(unittest.TestCase):
    def test_default_resolves_binary_prepares_and_returns_attach_plan(self) -> None:
        ops = _FakeHerdrOps()
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args())
        self.assertIsNone(outcome.error_message)
        self.assertIsNone(outcome.json_stdout)
        self.assertFalse(outcome.no_attach)
        self.assertEqual(outcome.attach_argv, ("/usr/local/bin/herdr",))
        self.assertIn("herdr session-start", outcome.pre_attach_text)
        self.assertIn("attach: /usr/local/bin/herdr", outcome.pre_attach_text)
        # binary resolved before session-start side effect.
        self.assertLess(
            ops.calls.index("resolve_binary"),
            next(i for i, c in enumerate(ops.calls) if isinstance(c, tuple)),
        )

    def test_no_attach_prepares_but_marks_no_attach(self) -> None:
        ops = _FakeHerdrOps()
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args(no_attach=True))
        self.assertIsNone(outcome.error_message)
        self.assertTrue(outcome.no_attach)
        self.assertIn(("prepare", Path("/repo")), ops.calls)
        self.assertIsNotNone(outcome.pre_attach_text)

    def test_json_returns_ready_payload_and_never_attaches(self) -> None:
        ops = _FakeHerdrOps()
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args(json_output=True))
        self.assertIsNone(outcome.error_message)
        self.assertIsNotNone(outcome.json_stdout)
        payload = json.loads(outcome.json_stdout)
        self.assertEqual(payload["backend"], BACKEND_HERDR)
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["attached"])
        self.assertTrue(payload["no_attach"])
        self.assertEqual(outcome.attach_argv, ())

    def test_tmux_nested_attach_fails_closed_before_session_start(self) -> None:
        ops = _FakeHerdrOps(in_tmux=True)
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args())
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("inside tmux", outcome.error_message)
        # No binary resolution and no session-start ran.
        self.assertNotIn("resolve_binary", ops.calls)
        self.assertFalse(any(isinstance(c, tuple) for c in ops.calls))

    def test_tmux_with_no_attach_is_allowed(self) -> None:
        ops = _FakeHerdrOps(in_tmux=True)
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args(no_attach=True))
        self.assertIsNone(outcome.error_message)
        self.assertTrue(outcome.no_attach)
        self.assertIn(("prepare", Path("/repo")), ops.calls)

    def test_tmux_with_json_is_allowed(self) -> None:
        ops = _FakeHerdrOps(in_tmux=True)
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args(json_output=True))
        self.assertIsNone(outcome.error_message)
        self.assertIsNotNone(outcome.json_stdout)

    def test_cc_flag_is_rejected(self) -> None:
        ops = _FakeHerdrOps()
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args(cc=True))
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("--cc", outcome.error_message)
        self.assertNotIn("resolve_binary", ops.calls)

    def test_session_flag_is_rejected(self) -> None:
        ops = _FakeHerdrOps()
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args(session="my-session"))
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("--session", outcome.error_message)
        self.assertNotIn("resolve_binary", ops.calls)

    def test_missing_binary_fails_closed_no_fallback(self) -> None:
        exc = TerminalTransportError(
            "terminal transport backend 'herdr' is selected but no herdr binary "
            "is configured in the trusted environment (MOZYO_HERDR_BINARY); "
            "refusing to fall back to tmux",
            reason=REASON_BINARY_UNCONFIGURED,
        )
        ops = _FakeHerdrOps(resolve_error=exc)
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args())
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("refusing to fall back to tmux", outcome.error_message)
        # session-start never ran once the binary failed to resolve.
        self.assertFalse(any(isinstance(c, tuple) for c in ops.calls))

    def test_session_start_failure_fails_closed_no_fallback(self) -> None:
        ops = _FakeHerdrOps(
            prepare_error=HerdrSessionStartError("herdr agent list timed out")
        )
        outcome = MozyoHerdrLaunchUseCase(ops).run(_args())
        self.assertIsNotNone(outcome.error_message)
        self.assertIn("herdr session-start failed", outcome.error_message)
        self.assertIn("refusing to fall back to tmux", outcome.error_message)


# --- delivery tail ------------------------------------------------------------


class DeliverHerdrLaunchOutcomeTests(unittest.TestCase):
    def test_error_dies_first(self) -> None:
        ops = _FakeHerdrOps()
        with self.assertRaises(_Died) as ctx:
            deliver_herdr_launch_outcome(
                HerdrLaunchOutcome(error_message="boom"), ops
            )
        self.assertEqual(ctx.exception.message, "boom")

    def test_json_emits_and_returns_without_attach(self) -> None:
        ops = _FakeHerdrOps()
        rc = deliver_herdr_launch_outcome(
            HerdrLaunchOutcome(json_stdout='{"backend": "herdr"}'), ops
        )
        self.assertEqual(rc, 0)
        self.assertEqual(ops.emitted, [('{"backend": "herdr"}', "\n")])
        self.assertIsNone(ops.attached)

    def test_no_attach_emits_summary_without_attach(self) -> None:
        ops = _FakeHerdrOps()
        rc = deliver_herdr_launch_outcome(
            HerdrLaunchOutcome(
                pre_attach_text="summary",
                attach_argv=("/usr/local/bin/herdr",),
                no_attach=True,
            ),
            ops,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(ops.emitted, [("summary", "\n")])
        self.assertIsNone(ops.attached)

    def test_attach_emits_summary_then_execs(self) -> None:
        ops = _FakeHerdrOps()
        with self.assertRaises(_Attached):
            deliver_herdr_launch_outcome(
                HerdrLaunchOutcome(
                    pre_attach_text="summary",
                    attach_argv=("/usr/local/bin/herdr",),
                    no_attach=False,
                ),
                ops,
            )
        self.assertEqual(ops.emitted, [("summary", "\n")])
        self.assertEqual(ops.attached, ["/usr/local/bin/herdr"])


# --- entrypoint routing (byte-invariant tmux default) -------------------------


class BareMozyoBackendRoutingTests(unittest.TestCase):
    """``cli.main`` routes bare ``mozyo`` by the resolved repo's backend."""

    def _run_main_with_config(self, config_body: str | None) -> tuple[bool, bool]:
        from mozyo_bridge.application import cli
        from mozyo_bridge.application import herdr_launch_command

        called = {"tmux": False, "herdr": False}

        def _fake_tmux(args):
            called["tmux"] = True
            return 0

        def _fake_herdr(args):
            called["herdr"] = True
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            if config_body is not None:
                cfg_dir = repo / ".mozyo-bridge"
                cfg_dir.mkdir(parents=True)
                (cfg_dir / "config.yaml").write_text(config_body, encoding="utf-8")
            orig_tmux = cli.cmd_mozyo
            orig_herdr = herdr_launch_command.cmd_mozyo_herdr
            cli.cmd_mozyo = _fake_tmux
            herdr_launch_command.cmd_mozyo_herdr = _fake_herdr
            try:
                rc = cli.main(["--repo", str(repo), "--no-attach"])
            finally:
                cli.cmd_mozyo = orig_tmux
                herdr_launch_command.cmd_mozyo_herdr = orig_herdr
        self.assertEqual(rc, 0)
        return called["tmux"], called["herdr"]

    def test_config_absent_routes_to_tmux_path(self) -> None:
        tmux, herdr = self._run_main_with_config(None)
        self.assertTrue(tmux)
        self.assertFalse(herdr)

    def test_explicit_tmux_backend_routes_to_tmux_path(self) -> None:
        tmux, herdr = self._run_main_with_config(
            "terminal_transport:\n  backend: tmux\n"
        )
        self.assertTrue(tmux)
        self.assertFalse(herdr)

    def test_herdr_backend_routes_to_herdr_path(self) -> None:
        tmux, herdr = self._run_main_with_config(
            "terminal_transport:\n  backend: herdr\n"
        )
        self.assertFalse(tmux)
        self.assertTrue(herdr)


if __name__ == "__main__":
    unittest.main()
