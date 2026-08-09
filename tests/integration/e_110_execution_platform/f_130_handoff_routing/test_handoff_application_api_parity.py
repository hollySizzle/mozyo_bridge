"""CLI / application-API parity over the real orchestration (Redmine #15149 / #15156).

The unit specs drive the typed API through a fake port. These drive the **real**
``run_handoff_orchestration`` twice — once from a parsed ``argparse.Namespace``
through the CLI entry point, once from a typed :class:`HandoffRequest` through
:func:`run_handoff` — and assert the two reach the *same* gate with the *same*
structured outcome. That is the #15156 acceptance: the high-level operation
returns the same outcome before and after the split, and the application API can
be called with no shell argv, no TTY, and no stdout parse.

The invocations chosen refuse **before any side effect**, so they are
deterministic and touch no pane: the shared send-semantics authority
(``queue-enter`` refuses ``--force``) and the receiver-vocabulary gate. Both fire
before target resolution, before the anchor plan, and before a single byte is
typed, which is also what makes them the right probes for "the API cannot skip a
gate the CLI runs".
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

# tmux-rail transport isolation (Redmine #13254), as in the sibling rail tests.
from . import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

from mozyo_bridge.application.cli import build_parser  # noqa: E402
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E402,E501
    STATUS_FAIL_CLOSED,
    HandoffRequest,
    run_handoff,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (  # noqa: E402,E501
    HandoffCommandInput,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_operation import (  # noqa: E402,E501
    OP_SEND,
)


@contextlib.contextmanager
def _no_live_runtime():
    """Neutralize everything that would need a live tmux / herdr runtime.

    Both parity paths run under the identical set, so any behavioural difference
    the assertions catch is the split's, not the environment's.
    """
    with patch(
        "mozyo_bridge.application.handoff_transport_wiring."
        "resolve_handoff_transport_runtime",
        return_value=(None, None),
    ), patch(
        "mozyo_bridge.application.commands.herdr_effective_backend_selected",
        return_value=False,
    ), patch("mozyo_bridge.application.commands.require_tmux"), patch(
        "mozyo_bridge.application.commands.run_target_resolution",
        side_effect=AssertionError("target resolution must not run"),
    ), patch(
        "mozyo_bridge.application.commands.run_tmux"
    ) as run_tmux, patch(
        "mozyo_bridge.application.commands.capture_pane"
    ) as capture_pane:
        yield run_tmux, capture_pane


class _ParityCase:
    """One refusal expressed both ways, so the two entries can be compared."""

    def __init__(self, *, argv: list[str], input_fields: dict) -> None:
        self.argv = argv
        self.input_fields = input_fields


#: ``queue-enter`` refuses ``--force`` (the shared send-semantics authority).
SEND_SEMANTICS_REFUSAL = _ParityCase(
    argv=[
        "handoff", "send",
        "--to", "claude",
        "--target", "%99",
        "--source", "redmine",
        "--issue", "15149",
        "--journal", "101722",
        "--kind", "reply",
        "--mode", "queue-enter",
        "--force",
        "--record-format", "json",
    ],
    input_fields=dict(
        to="claude",
        target="%99",
        source="redmine",
        issue="15149",
        journal="101722",
        kind="reply",
        mode="queue-enter",
        force=True,
        record_format="json",
    ),
)


class CliApplicationApiParityTest(unittest.TestCase):
    def _run_cli(self, case: _ParityCase, repo: str):
        args = build_parser().parse_args(["--repo", repo] + case.argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with _no_live_runtime() as (run_tmux, capture_pane):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    args.func(args)
            run_tmux.assert_not_called()
            capture_pane.assert_not_called()
        return args, caught.exception, stdout.getvalue(), stderr.getvalue()

    def _run_api(self, case: _ParityCase, repo: str):
        request = HandoffRequest(
            operation=OP_SEND,
            input=HandoffCommandInput(**case.input_fields),
            repo_root=Path(repo),
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with _no_live_runtime() as (run_tmux, capture_pane):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = run_handoff(request)
            run_tmux.assert_not_called()
            capture_pane.assert_not_called()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_the_same_refusal_produces_the_same_structured_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            args, exit_exc, cli_stdout, cli_stderr = self._run_cli(
                SEND_SEMANTICS_REFUSAL, repo
            )
            result, api_stdout, api_stderr = self._run_api(SEND_SEMANTICS_REFUSAL, repo)

        cli_outcome = getattr(args, "delivery_outcome", None)
        self.assertIsNotNone(cli_outcome, "the CLI path published no outcome")
        self.assertIsNotNone(result.outcome, "the API path captured no outcome")

        # Same gate, same structured verdict.
        self.assertEqual("blocked", cli_outcome.status)
        self.assertEqual(cli_outcome.status, result.outcome.status)
        self.assertEqual(cli_outcome.reason, result.outcome.reason)
        self.assertEqual(cli_outcome.receiver, result.outcome.receiver)
        self.assertEqual(cli_outcome.mode, result.outcome.mode)
        self.assertEqual(cli_outcome.kind, result.outcome.kind)
        self.assertEqual(cli_outcome.source, result.outcome.source)

        # Same exit code; the API reports it as a typed fail-closed result rather
        # than by raising.
        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertEqual(exit_exc.code, result.exit_code)
        self.assertFalse(result.delivered)

        # The refusal text is the same one the CLI printed — but the API got it as
        # a typed attribute, not by parsing that output.
        self.assertIn(f"error: {result.error_message}", cli_stderr)

    def test_the_cli_still_prints_its_record_and_the_api_prints_nothing(self) -> None:
        """The record channel stays the CLI's; the API answers in typed data."""
        with tempfile.TemporaryDirectory() as repo:
            _, _, cli_stdout, _ = self._run_cli(SEND_SEMANTICS_REFUSAL, repo)
            result, api_stdout, _ = self._run_api(SEND_SEMANTICS_REFUSAL, repo)

        self.assertTrue(cli_stdout.strip(), "the CLI must still emit its record")
        self.assertEqual("", api_stdout)
        # Everything the record carried is on the typed result instead.
        self.assertEqual(1, len(result.emissions))
        self.assertIs(result.outcome, result.emissions[0].outcome)

    def test_the_api_cannot_skip_the_receiver_vocabulary_gate(self) -> None:
        """No argparse ``choices`` in front of it — the gate itself still refuses."""
        with tempfile.TemporaryDirectory() as repo:
            request = HandoffRequest(
                operation=OP_SEND,
                input=HandoffCommandInput(
                    to="nobody",
                    target="%99",
                    source="redmine",
                    issue="15149",
                    journal="101722",
                    kind="reply",
                ),
                repo_root=Path(repo),
            )
            with _no_live_runtime() as (run_tmux, capture_pane):
                with contextlib.redirect_stderr(io.StringIO()):
                    result = run_handoff(request)
                run_tmux.assert_not_called()
                capture_pane.assert_not_called()

        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertEqual(2, result.exit_code)
        self.assertIn("--to must be one of", result.error_message)
        self.assertFalse(result.delivered)

    def test_the_api_needs_no_argv_tty_or_subprocess(self) -> None:
        """The typed call runs with stdin closed and no parser in the picture."""
        with tempfile.TemporaryDirectory() as repo:
            request = HandoffRequest(
                operation=OP_SEND,
                input=HandoffCommandInput(**SEND_SEMANTICS_REFUSAL.input_fields),
                repo_root=Path(repo),
            )
            with _no_live_runtime(), patch(
                "subprocess.run", side_effect=AssertionError("no subprocess")
            ), patch(
                "subprocess.Popen", side_effect=AssertionError("no subprocess")
            ), patch.object(sys, "argv", ["pytest"]), contextlib.redirect_stderr(
                io.StringIO()
            ):
                result = run_handoff(request)

        self.assertEqual(STATUS_FAIL_CLOSED, result.status)
        self.assertEqual("blocked", result.outcome.status)


if __name__ == "__main__":
    unittest.main()
