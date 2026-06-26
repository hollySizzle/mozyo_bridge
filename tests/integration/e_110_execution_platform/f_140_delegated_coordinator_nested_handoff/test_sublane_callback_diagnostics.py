"""Sublane startup readiness + callback-stall recovery diagnostics (Redmine #12159).

Covers three surfaces of the #12159 implementation (#12165):

- the pure callback-stall classifier in ``domain.sublane_callback`` — every
  documented state (the four genuine stalls plus the non-stall outcomes), the
  ``progress_without_callback`` recovery that must not re-dispatch done work,
  the stale-CLI sub-case of a delivery failure, and the invalid-input guard;
- the ``sublane readiness`` report — reproducible-auto OK, the invalid
  permission-mode (`autopilot`) remediation, an explicit non-auto override
  warning, and the always-present coordinator-callback contract;
- the CLI wiring (``sublane readiness`` / ``sublane callback-recovery``) so the
  recovery path is replayable from CLI output, including the exit-code contract
  (genuine stall -> non-zero) that lets a coordinator branch on it.

All read-only: nothing here touches tmux, the network, or Redmine, and no test
weakens a handoff / queue-enter / launch guard.
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import sublane_diagnostics
from mozyo_bridge.domain import sublane_callback
from mozyo_bridge.domain.claude_permission_policy import CLAUDE_PERMISSION_MODE_ENV


class ClassifyCallbackStallTest(unittest.TestCase):
    def test_no_dispatch_is_not_a_stall_candidate(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=False, new_durable_progress=False
        )
        self.assertEqual(
            sublane_callback.STATE_NOT_STALL_CANDIDATE, result["state"]
        )
        self.assertFalse(result["is_stall"])
        # Non-stall outcomes do not carry the stall invariants.
        self.assertEqual([], result["invariants"])

    def test_no_progress_after_handoff(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=False,
            callback=sublane_callback.CALLBACK_ABSENT,
        )
        self.assertEqual(
            sublane_callback.STATE_NO_PROGRESS_AFTER_HANDOFF, result["state"]
        )
        self.assertTrue(result["is_stall"])
        self.assertTrue(result["invariants"])

    def test_progress_without_callback_does_not_redispatch(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=True,
            callback=sublane_callback.CALLBACK_SAME_LANE_ONLY,
        )
        self.assertEqual(
            sublane_callback.STATE_PROGRESS_WITHOUT_CALLBACK, result["state"]
        )
        self.assertTrue(result["is_stall"])
        joined = " ".join(result["recovery"]).lower()
        self.assertIn("pick up the advanced", joined)
        # The defining invariant: never re-dispatch work the record shows done.
        self.assertTrue(
            any("do not re-dispatch" in step.lower() for step in result["recovery"]),
            result["recovery"],
        )

    def test_callback_delivery_failed_with_stale_cli_hint(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=False,
            callback=sublane_callback.CALLBACK_DELIVERY_FAILED,
            stale_cli=True,
        )
        self.assertEqual(
            sublane_callback.STATE_CALLBACK_DELIVERY_FAILED, result["state"]
        )
        self.assertTrue(result["is_stall"])
        joined = " ".join(result["recovery"]).lower()
        self.assertIn("stale installed cli", joined)
        self.assertIn("repo-local", joined)

    def test_callback_delivery_failed_without_stale_cli_omits_hint(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=False,
            callback=sublane_callback.CALLBACK_DELIVERY_FAILED,
            stale_cli=False,
        )
        joined = " ".join(result["recovery"]).lower()
        self.assertNotIn("stale installed cli", joined)

    def test_callback_not_attempted(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=True,
            callback=sublane_callback.CALLBACK_ABSENT,
        )
        self.assertEqual(
            sublane_callback.STATE_CALLBACK_NOT_ATTEMPTED, result["state"]
        )
        self.assertTrue(result["is_stall"])

    def test_acked_is_complete_not_a_stall(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=True,
            callback=sublane_callback.CALLBACK_ACKED,
        )
        self.assertEqual(
            sublane_callback.STATE_CALLBACK_COMPLETE, result["state"]
        )
        self.assertFalse(result["is_stall"])

    def test_not_required_is_not_a_stall(self) -> None:
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=False,
            callback=sublane_callback.CALLBACK_NOT_REQUIRED,
        )
        self.assertEqual(
            sublane_callback.STATE_CALLBACK_NOT_REQUIRED, result["state"]
        )
        self.assertFalse(result["is_stall"])

    def test_delivery_failed_wins_over_progress(self) -> None:
        # A recorded delivery failure is the discriminator even when progress
        # also landed (the stale-CLI-after-routing case).
        result = sublane_callback.classify_callback_stall(
            dispatch_delivered=True,
            new_durable_progress=True,
            callback=sublane_callback.CALLBACK_DELIVERY_FAILED,
        )
        self.assertEqual(
            sublane_callback.STATE_CALLBACK_DELIVERY_FAILED, result["state"]
        )

    def test_invalid_callback_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            sublane_callback.classify_callback_stall(
                dispatch_delivered=True,
                new_durable_progress=True,
                callback="nope",
            )

    def test_stall_states_set_matches_four_documented_states(self) -> None:
        self.assertEqual(
            {
                sublane_callback.STATE_NO_PROGRESS_AFTER_HANDOFF,
                sublane_callback.STATE_PROGRESS_WITHOUT_CALLBACK,
                sublane_callback.STATE_CALLBACK_DELIVERY_FAILED,
                sublane_callback.STATE_CALLBACK_NOT_ATTEMPTED,
            },
            set(sublane_callback.STALL_STATES),
        )


class ReadinessReportTest(unittest.TestCase):
    def _report(self, env):
        with patch.dict("os.environ", env, clear=True):
            return sublane_diagnostics.build_readiness_report()

    def test_unset_env_is_ok_reproducible_auto(self) -> None:
        report = self._report({})
        self.assertEqual("ok", report["status"])
        pm = report["permission_mode"]
        self.assertEqual("auto", pm["effective_mode"])
        self.assertTrue(pm["reproducible_auto"])
        self.assertIn("non-retroactive", pm["scope"])

    def test_invalid_env_autopilot_warns_with_remediation(self) -> None:
        report = self._report({CLAUDE_PERMISSION_MODE_ENV: "autopilot"})
        self.assertEqual("warning", report["status"])
        actions = " ".join(report["permission_mode"]["next_action"]).lower()
        self.assertIn("autopilot", actions)
        # Remediation names the valid choices so the fix is unambiguous.
        self.assertIn("auto", actions)

    def test_explicit_non_auto_override_warns(self) -> None:
        report = self._report({CLAUDE_PERMISSION_MODE_ENV: "default"})
        self.assertEqual("warning", report["status"])
        self.assertTrue(report["permission_mode"]["next_action"])

    def test_callback_responsibility_contract_always_present(self) -> None:
        report = self._report({})
        states = report["callback_responsibility"]["states"]
        names = {entry["state"] for entry in states}
        # The full handoff-worthy callback set must be surfaced.
        self.assertEqual(
            {name for name, _ in sublane_callback.COORDINATOR_CALLBACK_STATES},
            names,
        )

    def test_readiness_text_is_renderable(self) -> None:
        report = self._report({})
        text = sublane_diagnostics.format_readiness_text(report)
        self.assertIn("sublane readiness: ok", text)
        self.assertIn("permission_mode", text)
        self.assertIn("callback_responsibility", text)


class SublaneCliTest(unittest.TestCase):
    def _run(self, argv, env=None):
        from mozyo_bridge.application.cli import build_parser

        env = env if env is not None else {}
        parser = build_parser()
        args = parser.parse_args(argv)
        with patch.dict("os.environ", env, clear=True):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = args.func(args)
        return rc, out.getvalue()

    def test_readiness_cli_ok_exit_zero(self) -> None:
        rc, text = self._run(["sublane", "readiness"], env={})
        self.assertEqual(0, rc)
        self.assertIn("sublane readiness: ok", text)

    def test_readiness_cli_invalid_env_exit_nonzero(self) -> None:
        rc, text = self._run(
            ["sublane", "readiness"],
            env={CLAUDE_PERMISSION_MODE_ENV: "autopilot"},
        )
        self.assertEqual(1, rc)
        self.assertIn("warning", text)

    def test_callback_recovery_cli_progress_without_callback_replayable(self) -> None:
        rc, text = self._run(
            [
                "sublane",
                "callback-recovery",
                "--dispatch-delivered",
                "--progress",
                "--callback",
                "same_lane_only",
                "--json",
            ]
        )
        self.assertEqual(1, rc)  # genuine stall -> non-zero
        payload = json.loads(text)
        self.assertEqual("progress_without_callback", payload["state"])
        self.assertTrue(payload["is_stall"])

    def test_callback_recovery_cli_non_stall_exit_zero(self) -> None:
        rc, text = self._run(
            [
                "sublane",
                "callback-recovery",
                "--dispatch-delivered",
                "--progress",
                "--callback",
                "acked",
            ]
        )
        self.assertEqual(0, rc)
        self.assertIn("callback_complete", text)


if __name__ == "__main__":
    unittest.main()
