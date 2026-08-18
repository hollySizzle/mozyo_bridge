"""Operator acceptance for ``workflow callback-redrive`` (Redmine #15707 c).

The exit code IS the contract (the ``callback-admit`` doctrine): 0 = dry-run listed / redrive
applied; 2 = invalid arguments; 4 = no such row; 5 = the row is not dead-lettered; 6 =
fingerprint mismatch (concurrent mutation, zero-write). The dry-run is the default and writes
nothing; ``--apply`` requires the full row key plus the fingerprint a prior dry-run reported;
a mutating action without a resolved workspace identity fails closed unless the explicit
``--allow-unpartitioned-callbacks`` surface is used.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import (  # noqa: E402
    CallbackOutbox,
    CallbackOutboxKey,
)
from mozyo_bridge.core.state.workflow_runtime_store import (  # noqa: E402
    CALLBACK_DEAD_LETTER,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    cli_workflow_callbacks as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callback_redrive import (  # noqa: E402,E501
    EXIT_ABSENT,
    EXIT_FINGERPRINT_MISMATCH,
    EXIT_INVALID_ARGS,
    EXIT_OK,
    EXIT_STATE_MISMATCH,
    cmd_workflow_callback_redrive,
)

WS = "ws_redrive_acceptance"


def _args(**over):
    base = dict(
        json=True, store_path=None, apply=False, issue=None, journal=None, gate=None,
        route=None, source="redmine", expect_fingerprint="",
        allow_unpartitioned_callbacks=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class CallbackRedriveCommandAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "workflow-runtime.sqlite"
        self.outbox = CallbackOutbox(path=self.store_path)
        # Pin a VERIFIED test workspace (the #13518 R4-F1 hermetic-CI pattern): production
        # stays fail-closed; only the test supplies its attested identity.
        self._orig_resolve_ws = cli._resolve_workspace_id
        cli._resolve_workspace_id = lambda args: WS
        self.addCleanup(setattr, cli, "_resolve_workspace_id", self._orig_resolve_ws)
        self.key = CallbackOutboxKey(
            source="redmine", issue="15700", journal="107938",
            normalized_gate="review", callback_route="coordinator", workspace_id=WS,
        )
        self.outbox.enqueue(
            self.key,
            initial_state=CALLBACK_DEAD_LETTER,
            detail="zero-send: precondition_not_idle",
        )

    def _run(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_workflow_callback_redrive(args)
        return code, out.getvalue()

    def _dry_run_fingerprint(self):
        code, stdout = self._run(_args(store_path=str(self.store_path)))
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout)
        self.assertEqual(payload["action"], "redrive_dry_run")
        self.assertEqual(len(payload["dead_letter"]), 1)
        return payload["dead_letter"][0]["redrive_fingerprint"]

    def _apply_args(self, fingerprint, **over):
        base = dict(
            store_path=str(self.store_path), apply=True, issue="15700", journal="107938",
            gate="review", route="coordinator", expect_fingerprint=fingerprint,
        )
        base.update(over)
        return _args(**base)

    def test_dry_run_lists_the_backlog_and_writes_nothing(self) -> None:
        self._dry_run_fingerprint()
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)

    def test_apply_with_the_observed_fingerprint_requeues_exit_zero(self) -> None:
        fingerprint = self._dry_run_fingerprint()
        code, stdout = self._run(self._apply_args(fingerprint))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(stdout)["disposition"], "requeued")
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_PENDING)

    def test_apply_with_a_stale_fingerprint_is_exit_six_zero_write(self) -> None:
        code, stdout = self._run(self._apply_args("stale-token"))
        self.assertEqual(code, EXIT_FINGERPRINT_MISMATCH)
        self.assertEqual(json.loads(stdout)["disposition"], "fingerprint_mismatch")
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)

    def test_apply_on_an_unknown_row_is_exit_four(self) -> None:
        code, _ = self._run(self._apply_args("t", journal="99999"))
        self.assertEqual(code, EXIT_ABSENT)

    def test_apply_on_a_non_dead_letter_row_is_exit_five(self) -> None:
        fingerprint = self._dry_run_fingerprint()
        self.assertEqual(self._run(self._apply_args(fingerprint))[0], EXIT_OK)
        code, _ = self._run(self._apply_args(fingerprint))
        self.assertEqual(code, EXIT_STATE_MISMATCH)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_PENDING)

    def test_apply_without_the_full_key_is_exit_two(self) -> None:
        code, _ = self._run(self._apply_args("token", gate=None))
        self.assertEqual(code, EXIT_INVALID_ARGS)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DEAD_LETTER)

    def test_dry_run_issue_filter_narrows_the_listing(self) -> None:
        code, stdout = self._run(_args(store_path=str(self.store_path), issue="99999"))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(stdout)["dead_letter"], [])

    def test_unresolved_workspace_fails_closed_without_the_explicit_surface(self) -> None:
        cli._resolve_workspace_id = lambda args: ""
        with self.assertRaises(SystemExit):
            self._run(_args(store_path=str(self.store_path)))

    def test_unpartitioned_surface_is_explicit(self) -> None:
        cli._resolve_workspace_id = lambda args: ""
        code, stdout = self._run(
            _args(store_path=str(self.store_path), allow_unpartitioned_callbacks=True)
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout)
        self.assertEqual(payload["workspace_id"], "")
        # The blank bucket is EXACTLY the legacy blank-workspace rows — a partitioned
        # workspace's row is never listed (nor redrivable) through this surface.
        self.assertEqual(payload["dead_letter"], [])


if __name__ == "__main__":
    unittest.main()
