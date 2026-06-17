"""Regression rails for the queue-enter inactive-split fallback (Redmine #12162).

The dogfooding failure (Redmine #12137 j#60072/#60073) was: `notify-codex-review
--issue 12137 --journal 60072` resolved the Codex pane `%2`, then the queue-enter
Step 11 active-split admission gate blocked it (`blocked / invalid_args`) and the
caller was left to reassemble the strict-rail recovery command by hand. The
notify wrappers forward into `orchestrate_handoff`, so the gate fires there.

These tests pin:

- the pure `build_inactive_pane_fallback_command` recovery-command builder for
  Redmine / Asana anchors and its fail-closed `None` cases,
- `build_delivery_record` surfacing the recovery command on the durable record,
- the characterization that a `notify-*` wrapper resolving an inactive same-window
  pane now emits the concrete `handoff send … --target %pane --target-repo auto
  --mode standard` recovery on both the durable record (stdout) and the error
  (stderr), while the active-split guard still blocks (no typing, no Enter).

The active-split guard itself is intentionally NOT weakened: queue-enter still
requires the active split; the fix only hands back the strict-rail retry.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser  # noqa: E402
from mozyo_bridge.domain.handoff import (  # noqa: E402
    AsanaAnchor,
    RedmineAnchor,
    build_delivery_record,
    build_inactive_pane_fallback_command,
    make_outcome,
    normalize_anchor,
)


class BuildInactivePaneFallbackCommandTest(unittest.TestCase):
    def test_redmine_anchor_builds_strict_rail_recovery_command(self) -> None:
        anchor = normalize_anchor("redmine", issue="12137", journal="60072")
        command = build_inactive_pane_fallback_command(
            receiver="codex",
            kind="review_request",
            target="%2",
            anchor=anchor,
        )
        self.assertEqual(
            "mozyo-bridge handoff send --to codex --source redmine "
            "--kind review_request --issue 12137 --journal 60072 "
            "--target %2 --target-repo auto --mode standard",
            command,
        )

    def test_asana_anchor_with_comment_id(self) -> None:
        anchor = AsanaAnchor(task_id="111", comment_id="222")
        command = build_inactive_pane_fallback_command(
            receiver="claude",
            kind="reply",
            target="%5",
            anchor=anchor,
        )
        self.assertEqual(
            "mozyo-bridge handoff send --to claude --source asana --kind reply "
            "--task-id 111 --comment-id 222 "
            "--target %5 --target-repo auto --mode standard",
            command,
        )

    def test_asana_anchor_with_anchor_url(self) -> None:
        anchor = AsanaAnchor(task_id="111", anchor_url="https://app.asana.com/x")
        command = build_inactive_pane_fallback_command(
            receiver="claude",
            kind="reply",
            target="%5",
            anchor=anchor,
        )
        self.assertIn("--task-id 111 --anchor-url https://app.asana.com/x", command)
        self.assertNotIn("--comment-id", command)

    def test_asana_anchor_url_with_shell_metacharacters_is_quoted(self) -> None:
        # Redmine #12162 review j#60107: a real Asana permalink carries `?`/`&`
        # query params. The recovery command must stay copy-pasteable — the
        # anchor-url token is shell-quoted so `&` does not background the command
        # or re-parse it in a normal shell.
        import shlex

        url = "https://app.asana.com/0/111/222?focus=true&timestamp=123"
        command = build_inactive_pane_fallback_command(
            receiver="codex",
            kind="review_request",
            target="%2",
            anchor=AsanaAnchor(task_id="111", anchor_url=url),
        )
        assert command is not None
        # The url is present but quoted as a single shell token (not bare).
        self.assertIn(shlex.quote(url), command)
        self.assertNotIn(f"--anchor-url {url} ", command)
        # The quoted command parses back to the exact argv, with the url intact
        # as one token (i.e. `&` did not split it).
        argv = shlex.split(command)
        self.assertIn(url, argv)
        self.assertEqual(url, argv[argv.index("--anchor-url") + 1])

    def test_recovery_carries_no_absolute_path(self) -> None:
        # Public/private boundary: the command must be pasteable into a durable
        # record, so it carries only pane/anchor ids and the `auto` sentinel.
        anchor = normalize_anchor("redmine", issue="12137", journal="60072")
        command = build_inactive_pane_fallback_command(
            receiver="codex", kind="review_request", target="%2", anchor=anchor
        )
        assert command is not None
        self.assertNotIn("/Users/", command)
        self.assertNotIn("/home/", command)
        self.assertIn("--target-repo auto", command)

    def test_returns_none_for_non_explicit_pane_target(self) -> None:
        anchor = normalize_anchor("redmine", issue="1", journal="2")
        self.assertIsNone(
            build_inactive_pane_fallback_command(
                receiver="codex", kind="reply", target="codex", anchor=anchor
            )
        )
        self.assertIsNone(
            build_inactive_pane_fallback_command(
                receiver="codex", kind="reply", target=None, anchor=anchor
            )
        )

    def test_returns_none_without_anchor(self) -> None:
        self.assertIsNone(
            build_inactive_pane_fallback_command(
                receiver="codex", kind="reply", target="%2", anchor=None
            )
        )

    def test_returns_none_for_unknown_receiver(self) -> None:
        anchor = normalize_anchor("redmine", issue="1", journal="2")
        self.assertIsNone(
            build_inactive_pane_fallback_command(
                receiver="nobody", kind="reply", target="%2", anchor=anchor
            )
        )


class DeliveryRecordRecoveryLineTest(unittest.TestCase):
    def _blocked_outcome(self):
        return make_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="codex",
            target="%2",
            anchor=normalize_anchor("redmine", issue="12137", journal="60072"),
            mode="queue-enter",
            kind="review_request",
            notification_marker=None,
            source="redmine",
        )

    def test_record_includes_recovery_command_when_supplied(self) -> None:
        record = build_delivery_record(
            self._blocked_outcome(),
            recovery_command=(
                "mozyo-bridge handoff send --to codex --source redmine "
                "--kind review_request --issue 12137 --journal 60072 "
                "--target %2 --target-repo auto --mode standard"
            ),
        )
        self.assertIn("- Fallback recovery: run", record)
        self.assertIn("--target %2 --target-repo auto --mode standard", record)

    def test_record_omits_recovery_line_when_not_supplied(self) -> None:
        record = build_delivery_record(self._blocked_outcome())
        self.assertNotIn("Fallback recovery", record)


class NotifyInactivePaneCharacterizationTest(unittest.TestCase):
    """Reproduces the Redmine #12137 j#60072/#60073 dogfooding block end-to-end."""

    def _run_notify(self, argv: list[str]):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        # An inactive same-window Codex split — exactly the #12137 shape: the
        # pane resolves and is a codex agent pane, but it is NOT the active split.
        pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "codex",
            "cwd": "/repo",
            "window_name": "codex",
            "pane_active": "0",
        }

        with patch("mozyo_bridge.application.commands.require_tmux"), patch(
            "mozyo_bridge.application.commands.current_pane", return_value="%1"
        ), patch(
            "mozyo_bridge.application.commands.current_session_name",
            return_value="agents",
        ), patch(
            "mozyo_bridge.application.commands.pane_window_name", return_value="codex"
        ), patch(
            "mozyo_bridge.application.commands.pane_location",
            return_value="agents:0.0",
        ), patch(
            "mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux
        ), patch(
            "mozyo_bridge.domain.pane_resolver.validate_target"
        ), patch(
            "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]
        ), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            with self.assertRaises(SystemExit):
                args.func(args)

        return sent, stdout.getvalue(), stderr.getvalue()

    def test_inactive_pane_blocks_and_emits_concrete_recovery_command(self) -> None:
        sent, stdout, stderr = self._run_notify(
            [
                "notify-codex-review",
                "--issue",
                "12137",
                "--journal",
                "60072",
                "--target",
                "%2",
            ]
        )

        expected_recovery = (
            "mozyo-bridge handoff send --to codex --source redmine "
            "--kind review_request --issue 12137 --journal 60072 "
            "--target %2 --target-repo auto --mode standard"
        )

        # The structured outcome is the contract-defined active-split rejection.
        outcome_lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(outcome_lines)
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual("queue-enter", outcome["mode"])

        # Recovery command is surfaced on BOTH the durable record (stdout) and
        # the error stream (stderr) so neither a record consumer nor an operator
        # reading the CLI error is left to reassemble it.
        self.assertIn(expected_recovery, stdout)
        self.assertIn("Fallback recovery", stdout)
        self.assertIn(expected_recovery, stderr)

        # The active-split guard is NOT weakened: nothing was typed and Enter
        # was never pressed (the block fires before any send-keys typing).
        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            msg=f"queue-enter typed into an inactive split: {sent!r}",
        )

    def test_active_pane_is_not_blocked(self) -> None:
        # Control: the same wrapper against an ACTIVE split is admitted, proving
        # the block is specifically the inactive-split guard, not a regression
        # that rejects every notify send.
        parser = build_parser()
        args = parser.parse_args(
            [
                "notify-codex-review",
                "--issue",
                "12137",
                "--journal",
                "60072",
                "--target",
                "%2",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ]
        )
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "codex",
            "cwd": "/repo",
            "window_name": "codex",
            "pane_active": "1",
        }

        with patch("mozyo_bridge.application.commands.require_tmux"), patch(
            "mozyo_bridge.application.commands.current_pane", return_value="%1"
        ), patch(
            "mozyo_bridge.application.commands.current_session_name",
            return_value="agents",
        ), patch(
            "mozyo_bridge.application.commands.pane_window_name", return_value="codex"
        ), patch(
            "mozyo_bridge.application.commands.pane_location",
            return_value="agents:0.0",
        ), patch(
            "mozyo_bridge.application.commands.capture_pane", return_value=""
        ), patch(
            "mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux
        ), patch(
            "mozyo_bridge.application.commands.time.sleep"
        ), patch(
            "mozyo_bridge.domain.pane_resolver.validate_target"
        ), patch(
            "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = args.func(args)

        self.assertEqual(0, result)
        # Enter was pressed (queue-enter default issues Enter even on marker miss).
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertNotIn("Fallback recovery", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
