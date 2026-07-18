"""Regression pins for Redmine #14017 — Herdr Claude provider exit into shell_residue.

Cause / fix: the #13637 startup self-attestation wrapper (``herdr agent-attest``) runs
*inside the herdr-spawned pane* and is about to ``os.execvp`` the interactive provider
into that same pane. Before this fix its pre-exec ``herdr agent list`` self-lookup
(``_live_lister``) inherited the pane's controlling terminal: ``capture_output=True``
piped stdout/stderr, but **stdin stayed the pane PTY** and the child ran in the pane's
session. A herdr *client* (a terminal multiplexer) run on the pane's controlling
terminal perturbs the terminal's foreground process group / stdin state, so the exec'd
provider inherited a disturbed terminal. Claude's interactive TUI — which requires a TTY
on stdin and is sensitive to job-control / foreground state — exited immediately into
``shell_residue``, while Codex's tool-shell (which does not depend on that state)
survived. A bare operator PTY tolerated the perturbation (its terminal was already fully
established); the pane's freshly-allocated PTY did not. ``claude --print`` was unaffected
because it is non-interactive and never reads the interactive stdin.

The fix detaches the pre-exec lister subprocess from the pane terminal on every fd
(``stdin=subprocess.DEVNULL`` + ``start_new_session=True``, stdout/stderr already piped),
restoring byte-for-byte terminal parity with the unwrapped (pre-#13637) launch. It is
provider-neutral: a query command needs neither stdin nor the controlling terminal.

These pins are deterministic (no live herdr / no real provider): they drive the real
``cmd_herdr_agent_attest`` CLI flow with ``subprocess.run`` + ``os.execvp`` patched, and
assert, in one integration path per provider, that:

1. the self-attestation lister subprocess is fully detached from the pane terminal
   (the shell_residue-prevention invariant, acceptance 1 / 3);
2. the provider argv is exec'd **byte-for-byte** unchanged after a successful
   attestation — for Claude (both supported 2.1.211 / 2.1.214 launch shapes) AND Codex,
   so the correction is provider- and version-neutral and non-regressing (acceptance
   2 / 4);
3. the attestation record is still persisted PRESENT — attestation success is followed
   by a clean exec into a resident provider, not a residue exit (acceptance 3).

Fix lives in
``src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/application/herdr_agent_attest.py``.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (
    cmd_herdr_agent_attest,
)

NAME = "mzb1_ws1_claude_default"
_MODULE = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
    "application.herdr_agent_attest"
)

# Representative managed launch shapes. The wrapper never inspects the provider version,
# so the two Claude rows exercise the SAME code path — which is exactly the point: the
# compatibility policy is that the wrapper is version-neutral. Codex carries its
# tool-shell `-c` overrides. All three must exec byte-for-byte unchanged.
_PROVIDER_ARGVS = {
    "claude-2.1.214": [
        "/abs/claude",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "claude-opus-4-8",
    ],
    "claude-2.1.211": [
        "/abs/claude",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "claude-sonnet-4",
    ],
    "codex": ["/abs/codex", "-c", "tool_shell.env.MOZYO_WORKSPACE_ID=ws1"],
}


class HerdrClaudeExitRegressionTest(unittest.TestCase):
    def _args(self, provider_argv):
        return argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id="",
            provider_argv=["--", *provider_argv],
        )

    def _drive(self, provider_argv, home):
        """Run the real wrapper flow; capture the list-subprocess kwargs + exec argv."""
        list_calls: list[dict] = []

        def _fake_run(argv, **kwargs):
            # Only the self-attestation `agent list` reaches subprocess.run here.
            list_calls.append({"argv": list(argv), "kwargs": kwargs})
            return argparse.Namespace(
                returncode=0,
                stdout='[{"name": "' + NAME + '", "pane_id": "wY:p2"}]',
            )

        with patch.dict(
            "os.environ",
            {
                "MOZYO_BRIDGE_HOME": str(home),
                "MOZYO_HERDR_BINARY": "/x/herdr",
                "MOZYO_WORKSPACE_ID": "ws1",
                "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "default",
            },
        ), patch(
            f"{_MODULE}.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        ), patch(
            f"{_MODULE}.subprocess.run", _fake_run
        ), patch(
            "os.execvp"
        ) as execvp:
            execvp.side_effect = SystemExit(0)  # exec replaces the process
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(self._args(provider_argv))
            return list_calls, execvp

    def test_pre_exec_lister_is_detached_from_the_pane_terminal(self) -> None:
        # Shell-residue prevention (acceptance 1 / 3): the wrapper's pre-exec self-lookup
        # can never touch the stdin / controlling terminal the provider inherits.
        for label, provider_argv in _PROVIDER_ARGVS.items():
            with self.subTest(provider=label), tempfile.TemporaryDirectory() as tmp:
                list_calls, _ = self._drive(provider_argv, Path(tmp))
                self.assertTrue(list_calls, "the self-attestation lister must run")
                for call in list_calls:
                    self.assertEqual(call["argv"], ["/x/herdr", "agent", "list"])
                    kwargs = call["kwargs"]
                    self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
                    self.assertTrue(kwargs.get("start_new_session"))
                    self.assertTrue(kwargs.get("capture_output"))

    def test_provider_argv_is_exec_byte_invariant_after_attest(self) -> None:
        # Provider- and version-neutral non-regression (acceptance 2 / 4): every provider
        # is exec'd exactly as assembled, so Claude 2.1.211 / 2.1.214 and Codex are
        # unchanged by the wrapper.
        for label, provider_argv in _PROVIDER_ARGVS.items():
            with self.subTest(provider=label), tempfile.TemporaryDirectory() as tmp:
                _, execvp = self._drive(provider_argv, Path(tmp))
                execvp.assert_called_once_with(provider_argv[0], provider_argv)

    def test_attestation_recorded_present_before_exec(self) -> None:
        # Attestation success is distinct from residue exit (acceptance 3): the record is
        # written PRESENT, then the provider is exec'd into a resident pane.
        for label, provider_argv in _PROVIDER_ARGVS.items():
            with self.subTest(provider=label), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                self._drive(provider_argv, home)
                record = HerdrIdentityAttestationStore(home=home).read(NAME)
                self.assertIsNotNone(record)
                self.assertEqual(record.verdict, VERDICT_PRESENT)
                self.assertEqual(record.locator, "wY:p2")


if __name__ == "__main__":
    unittest.main()
