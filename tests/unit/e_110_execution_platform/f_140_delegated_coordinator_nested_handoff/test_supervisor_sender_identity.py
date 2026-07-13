"""Attested per-workspace supervisor sender tests (Redmine #13683 Phase A, R1-F3).

A background supervisor fans out over many workspaces from one process, so its callback sender must
route each row to the row's own workspace — its canonical execution root (cwd + explicit
--target-repo) and workspace identity (env) — never on the sender process's ambient cwd/env. A
foreign / unattested row is a zero-send.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_PENDING
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (
    HandoffCallbackSendPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    default_sender,
    workspace_send_runner,
)


def _row(workspace_id: str) -> CallbackOutboxRow:
    return CallbackOutboxRow(
        source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
        callback_route="coordinator", state=CALLBACK_PENDING, attempts=0, max_attempts=3,
        send_attempted=False, notification_kind="review_request", notification_summary="",
        gate_mismatch=False, detail="", payload="", claim_token="tok", workspace_id=workspace_id,
    )


_DELIVERED_STDOUT = '{"status": "delivered", "reason": "ok"}'


class SendPortTargetRepoTest(unittest.TestCase):
    def test_explicit_target_repo_replaces_ambient_auto(self) -> None:
        captured = {}

        def runner(argv):
            captured["argv"] = argv
            return 0, _DELIVERED_STDOUT

        port = HandoffCallbackSendPort(
            attested_workspace_id="wsA", target_repo="/canonical/repoA", runner=runner
        )
        result = port(_row("wsA"))
        self.assertEqual(result.status, "delivered")
        argv = captured["argv"]
        i = argv.index("--target-repo")
        self.assertEqual(argv[i + 1], "/canonical/repoA")  # explicit path, NOT "auto"
        self.assertNotIn("auto", argv[i + 1])

    def test_default_target_repo_is_auto_backcompat(self) -> None:
        captured = {}
        port = HandoffCallbackSendPort(
            attested_workspace_id="wsA", runner=lambda argv: (captured.setdefault("argv", argv), (0, _DELIVERED_STDOUT))[1]
        )
        port(_row("wsA"))
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--target-repo") + 1], "auto")  # unchanged default

    def test_foreign_row_is_zero_send(self) -> None:
        calls = []
        port = HandoffCallbackSendPort(
            attested_workspace_id="wsA", target_repo="/canonical/repoA",
            runner=lambda argv: calls.append(argv) or (0, _DELIVERED_STDOUT),
        )
        result = port(_row("wsB"))  # foreign workspace
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "workspace_mismatch")
        self.assertEqual(calls, [])  # runner never invoked -> zero send


class DefaultSenderIdentityTest(unittest.TestCase):
    """The production default_sender pins cwd + identity env + explicit target-repo per workspace."""

    def test_two_workspaces_each_route_to_their_own_execution_root(self) -> None:
        recorded = []

        class _Proc:
            returncode = 0
            stdout = _DELIVERED_STDOUT

        def fake_run(argv, **kwargs):
            recorded.append((argv, kwargs.get("cwd"), dict(kwargs.get("env") or {})))
            return _Proc()

        ws_a = SupervisedWorkspace(workspace_id="wsA", canonical_path="/canonical/repoA")
        ws_b = SupervisedWorkspace(workspace_id="wsB", canonical_path="/canonical/repoB")

        with mock.patch("subprocess.run", fake_run):
            default_sender(ws_a)(_row("wsA"))
            default_sender(ws_b)(_row("wsB"))

        self.assertEqual(len(recorded), 2)
        (argv_a, cwd_a, env_a), (argv_b, cwd_b, env_b) = recorded
        # Each send runs in its OWN workspace root, with that workspace's identity env + target-repo.
        self.assertEqual(cwd_a, "/canonical/repoA")
        self.assertEqual(env_a.get("MOZYO_WORKSPACE_ID"), "wsA")
        self.assertEqual(argv_a[argv_a.index("--target-repo") + 1], "/canonical/repoA")
        self.assertEqual(cwd_b, "/canonical/repoB")
        self.assertEqual(env_b.get("MOZYO_WORKSPACE_ID"), "wsB")
        self.assertEqual(argv_b[argv_b.index("--target-repo") + 1], "/canonical/repoB")

    def test_default_sender_refuses_foreign_row_without_running_subprocess(self) -> None:
        calls = []
        with mock.patch("subprocess.run", lambda *a, **k: calls.append(1)):
            sender = default_sender(SupervisedWorkspace(workspace_id="wsA", canonical_path="/canonical/repoA"))
            result = sender(_row("wsB"))  # foreign
        # The sender returns a CallbackSendResult; a refused foreign row never delivers (zero send).
        self.assertNotEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(calls, [])

    def test_runner_sets_cwd_and_identity_env(self) -> None:
        seen = {}

        class _Proc:
            returncode = 0
            stdout = "ok"

        def fake_run(argv, **kwargs):
            seen.update(cwd=kwargs.get("cwd"), env=dict(kwargs.get("env") or {}))
            return _Proc()

        with mock.patch("subprocess.run", fake_run):
            runner = workspace_send_runner("/canonical/repoA", "wsA")
            rc, out = runner(["mozyo-bridge", "handoff", "send"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["cwd"], "/canonical/repoA")
        self.assertEqual(seen["env"].get("MOZYO_WORKSPACE_ID"), "wsA")


if __name__ == "__main__":
    unittest.main()
