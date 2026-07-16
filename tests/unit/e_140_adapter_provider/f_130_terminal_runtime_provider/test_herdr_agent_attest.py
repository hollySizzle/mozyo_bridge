"""Tests for the startup self-attestation self-check wrapper (Redmine #13637).

The wrapper runs as the herdr-spawned agent process, inspects its OWN env, records a
generation-bound self-attestation, then execs the provider. These tests drive
:func:`perform_self_attestation` with an injected ``lister`` + ``env`` + home (no live
herdr), and the CLI entry's argv handling with ``os.execvp`` patched (so the process
is not actually replaced).
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_CONFLICT,
    VERDICT_MISSING,
    VERDICT_PRESENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (
    cmd_herdr_agent_attest,
    perform_self_attestation,
)

NAME = "mzb1_ws1_claude_default"
_GOOD_ENV = {
    "MOZYO_WORKSPACE_ID": "ws1",
    "MOZYO_AGENT_ROLE": "claude",
    "MOZYO_LANE_ID": "default",
}


def _lister(*rows):
    return lambda: list(rows)


class PerformSelfAttestationTest(unittest.TestCase):
    def test_matching_env_records_present_with_self_resolved_locator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=home,
            )
            self.assertEqual(rec.verdict, VERDICT_PRESENT)
            self.assertEqual(rec.locator, "wY:p2")
            # persisted and readable
            got = HerdrIdentityAttestationStore(home=home).read(NAME)
            self.assertEqual(got.verdict, VERDICT_PRESENT)
            self.assertEqual(got.locator, "wY:p2")

    def test_envless_boot_records_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env={"MOZYO_HERDR_BINARY": "/x/herdr"},  # triplet absent
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.verdict, VERDICT_MISSING)

    def test_wrong_env_records_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env={
                    "MOZYO_WORKSPACE_ID": "wsOTHER",
                    "MOZYO_AGENT_ROLE": "claude",
                    "MOZYO_LANE_ID": "default",
                },
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.verdict, VERDICT_CONFLICT)

    def test_ambiguous_self_lookup_records_empty_locator(self) -> None:
        # Two rows with this name (or none) -> no unambiguous locator; recorded empty,
        # which the read side treats as stale / fail-closed.
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                lister=_lister(
                    {"name": NAME, "pane_id": "wY:p2"},
                    {"name": NAME, "pane_id": "wZ:p9"},
                ),
                home=Path(tmp),
            )
            self.assertEqual(rec.locator, "")

    def test_lister_failure_records_empty_locator_not_raises(self) -> None:
        def _boom():
            raise RuntimeError("herdr down")

        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                lister=_boom,
                home=Path(tmp),
            )
            self.assertEqual(rec.locator, "")
            self.assertEqual(rec.verdict, VERDICT_PRESENT)


    def test_replacement_action_id_is_recorded(self) -> None:
        # Redmine #13806 tranche D R2-F2: a replacement launch's action id reaches the record.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                replacement_action_id="recover:l:worker:claude:wk:w2",
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=home,
            )
            self.assertEqual(rec.replacement_action_id, "recover:l:worker:claude:wk:w2")

    def test_normal_launch_records_empty_action_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME, workspace_id="ws1", role="claude", lane="default",
                env=_GOOD_ENV, lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.replacement_action_id, "")


class CmdAgentAttestTest(unittest.TestCase):
    def _args(self, provider_argv, replacement_action_id=""):
        return argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id=replacement_action_id,
            provider_argv=provider_argv,
        )

    def test_replacement_action_id_flag_reaches_the_record(self) -> None:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"MOZYO_BRIDGE_HOME": tmp, "MOZYO_HERDR_BINARY": "/x/herdr",
             "MOZYO_WORKSPACE_ID": "ws1", "MOZYO_AGENT_ROLE": "claude", "MOZYO_LANE_ID": "default"},
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest._live_lister",
            return_value=_lister({"name": NAME, "pane_id": "wY:p2"}),
        ), patch("os.execvp") as execvp:
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(
                    self._args(["--", "claude"], replacement_action_id="recover:xyz")
                )
            back = HerdrIdentityAttestationStore(home=Path(tmp)).read(NAME)
            self.assertIsNotNone(back)
            self.assertEqual(back.replacement_action_id, "recover:xyz")

    def test_execs_provider_after_stripping_separator(self) -> None:
        # The CLI records then execs the provider argv, dropping the argparse
        # REMAINDER leading `--`.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": tmp, "MOZYO_HERDR_BINARY": "/x/herdr"}
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest._live_lister",
            return_value=_lister({"name": NAME, "pane_id": "wY:p2"}),
        ), patch(
            "os.execvp"
        ) as execvp:
            # Simulate exec replacing the process (so the unreachable guard is not hit).
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(
                    self._args(["--", "claude", "--permission-mode", "auto"])
                )
            execvp.assert_called_once_with(
                "claude", ["claude", "--permission-mode", "auto"]
            )

    def test_missing_provider_argv_fails_closed(self) -> None:
        with patch("os.execvp") as execvp:
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(self._args([]))
            execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
