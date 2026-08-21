"""Operator-view acceptance for ``workflow stall-watch`` (Redmine #15843).

Drives the real argv parser and the real command, against a fake ``herdr`` executable on
disk, and asserts the operator contract: exit codes, the JSON shape a watchdog wrapper
reads, and — the acceptance condition of the US — that the command exposes no way to act.
"""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.cli_workflow_stall_watch import (  # noqa: E501
    EXIT_OK,
    EXIT_UNOBSERVABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    HERDR_BINARY_ENV,
)

TRUST_SCREEN = (
    "Is this a project you created or one you trust\n"
    "Claude will be able to read, edit, and execute files here"
)

FAKE_HERDR = """#!/bin/sh
cat <<'PAYLOAD'
{PAYLOAD}
PAYLOAD
"""


def _parser():
    """The real root parser, so the acceptance is against the shipped argv surface."""
    from mozyo_bridge.application.cli import build_parser

    return build_parser()


class StallWatchCommandAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mozyo-15843-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _herdr_serving(self, screen: str) -> dict:
        payload = json.dumps({"result": {"read": {"text": screen}}})
        binary = self.root / "herdr"
        binary.write_text(FAKE_HERDR.replace("{PAYLOAD}", payload), encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return {HERDR_BINARY_ENV: str(binary), "PATH": "/usr/bin:/bin"}

    def _run(self, argv, env):
        args = _parser().parse_args(argv)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(out):
                code = args.func(args)
        return code, out.getvalue()

    def test_a_frozen_startup_screen_exits_zero_and_reports_the_operator_remedy(self):
        # A finding is the command's normal output, not a failure: exiting non-zero here
        # would put every watchdog wrapper into an alert loop over working output.
        code, stdout = self._run(
            [
                "workflow", "stall-watch", "--json",
                "--target", "w1V:pY",
                "--provider", "claude",
                "--interval-seconds", "0",
            ],
            self._herdr_serving(TRUST_SCREEN),
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout)
        observation = payload["observations"]["w1V:pY"]
        self.assertEqual(observation["stall_class"], "startup_interaction")
        self.assertEqual(
            observation["prescription"], "operator_resolves_startup_screen"
        )
        self.assertEqual(observation["matched_id"], "workspace_trust_confirmation")
        self.assertEqual(payload["posture"], "present_only")

    def test_the_json_payload_never_carries_pane_content(self):
        secret = "SECRET-TRANSCRIPT-TOKEN\n" + TRUST_SCREEN
        code, stdout = self._run(
            [
                "workflow", "stall-watch", "--json",
                "--target", "w1V:pY", "--provider", "claude",
                "--interval-seconds", "0",
            ],
            self._herdr_serving(secret),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("SECRET-TRANSCRIPT-TOKEN", stdout)

    def test_every_observation_is_present_only(self):
        code, stdout = self._run(
            [
                "workflow", "stall-watch", "--json",
                "--target", "w1V:pY", "--provider", "claude",
                "--interval-seconds", "0",
            ],
            self._herdr_serving("frozen and quiet"),
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout)
        for observation in payload["observations"].values():
            self.assertEqual(observation["posture"], "present_only")
            self.assertFalse(observation["relaunch_is_a_candidate"])

    def test_text_output_summarises_the_pass(self):
        code, stdout = self._run(
            [
                "workflow", "stall-watch",
                "--target", "w1V:pY", "--provider", "claude",
                "--interval-seconds", "0",
            ],
            self._herdr_serving("frozen and quiet"),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("unresponsive_indeterminate", stdout)
        self.assertIn("patient_wait_then_retry", stdout)
        self.assertIn("posture=present_only", stdout)

    def test_an_unresolvable_reader_exits_blocked_not_quiet(self):
        # A watcher that cannot read looks identical to a quiet cockpit unless it says so.
        code, stdout = self._run(
            ["workflow", "stall-watch", "--target", "w1V:pY", "--interval-seconds", "0"],
            {"PATH": "/nonexistent-abs-dir"},
        )
        self.assertEqual(code, EXIT_UNOBSERVABLE)
        self.assertIn("blocked, not quiet", stdout)

    def test_a_reader_that_binds_but_reads_nothing_also_exits_blocked(self):
        # Distinct from the unresolvable-reader case: here the reader binds and every
        # read fails. Both are the same operational fault (a watcher that observed
        # nothing) and both must be non-zero — a pass that reads nothing and exits 0
        # is indistinguishable from a quiet cockpit.
        binary = self.root / "herdr"
        binary.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        code, _ = self._run(
            [
                "workflow", "stall-watch", "--json",
                "--target", "w1V:pY", "--provider", "claude",
                "--interval-seconds", "0",
            ],
            {HERDR_BINARY_ENV: str(binary), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(code, EXIT_UNOBSERVABLE)

    def test_no_target_exits_blocked(self):
        code, stdout = self._run(
            ["workflow", "stall-watch", "--interval-seconds", "0"],
            self._herdr_serving("anything"),
        )
        self.assertEqual(code, EXIT_UNOBSERVABLE)
        self.assertIn("--target", stdout)

    def test_a_spec_naming_an_unlisted_target_is_refused_rather_than_ignored(self):
        code, stdout = self._run(
            [
                "workflow", "stall-watch",
                "--target", "w1V:pY",
                "--pending-body-marker", "w1V:pZ=some body",
                "--interval-seconds", "0",
            ],
            self._herdr_serving("anything"),
        )
        self.assertEqual(code, EXIT_UNOBSERVABLE)
        self.assertIn("w1V:pZ", stdout)

    def test_a_marker_containing_equals_signs_is_not_truncated(self):
        marker = "[mozyo:handoff:kind=implementation_request:issue=15843]"
        code, stdout = self._run(
            [
                "workflow", "stall-watch", "--json",
                "--target", "w1V:pY", "--provider", "claude",
                "--pending-body-marker", f"w1V:pY={marker}",
                "--interval-seconds", "0",
            ],
            self._herdr_serving(f"> {marker}"),
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout)
        self.assertEqual(
            payload["observations"]["w1V:pY"]["stall_class"], "unsent_composer"
        )
        self.assertEqual(
            payload["observations"]["w1V:pY"]["prescription"], "enter_only_retry"
        )


class StallWatchExposesNoActionTest(unittest.TestCase):
    """The US acceptance: detection is not recovery, expressed as an absent surface."""

    def test_the_command_exposes_no_flag_that_would_make_it_act(self):
        help_text = io.StringIO()
        parser = _parser()
        subaction = None
        for action in parser._subparsers._group_actions:  # noqa: SLF001 - argparse tree
            if "workflow" in action.choices:
                subaction = action.choices["workflow"]
                break
        self.assertIsNotNone(subaction)
        stall = None
        for action in subaction._subparsers._group_actions:  # noqa: SLF001
            if "stall-watch" in action.choices:
                stall = action.choices["stall-watch"]
                break
        self.assertIsNotNone(stall, "workflow stall-watch is not registered")
        stall.print_help(help_text)
        rendered = help_text.getvalue()
        for forbidden in ("--apply", "--enter", "--reset", "--relaunch", "--send"):
            with self.subTest(flag=forbidden):
                self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
