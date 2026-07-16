"""Direct regressions for the ``workflow callback-publication`` operator boundary (#13889 R11-F2).

The publication fence's own tests cover its state machine, but R10-F1 and R11-F1 were both defects
in the *operator surface* wrapped around it — a reconcile that reclaimed a live reservation, and a
``--recover`` that forgot every reservation at once. Neither was visible from the domain tests: the
parser groups and return codes could be removed entirely and the focused publication suite stayed
green. These tests pin the boundary itself.
"""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.callback_publication_fence import (
    CallbackPublicationFence,
    PublicationKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callback_publication import (  # noqa: E501
    cmd_workflow_callback_publication,
    register_callback_publication_parser,
)

ISSUE = "13889"
GEN = "1"
ANCHOR = "79990"
OUTCOME = "no_progress_after_handoff"
WS = "ws-1"
LANE = "lane-1"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mozyo-bridge workflow")
    register_callback_publication_parser(p.add_subparsers(dest="workflow_command"))
    return p


class ParserContractTest(unittest.TestCase):
    """The parser must refuse an operator who has named two intents at once."""

    def _rejects(self, argv: list[str]) -> None:
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(io.StringIO()):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                _parser().parse_args(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_two_actions_at_once_are_rejected(self):
        self._rejects(["callback-publication", "--list", "--bootstrap"])

    def test_two_dispositions_at_once_are_rejected(self):
        # "it landed" and "nothing landed" cannot both be true, and picking one for the operator is
        # how a duplicate gets written.
        self._rejects([
            "callback-publication", "--reconcile", ISSUE, GEN, ANCHOR, OUTCOME, WS, LANE,
            "--landed", "80500", "--none-landed",
        ])

    def test_recover_is_not_a_recognized_argument(self):
        # R11-F1: a store-wide reset forgets live reservations. This fence must not offer one, and
        # the absence has to be pinned -- it reads like an omission otherwise, and the sibling
        # surfaces all have it.
        self._rejects(["callback-publication", "--recover"])
        self.assertFalse(hasattr(CallbackPublicationFence, "recover"))


class CommandContractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = CallbackPublicationFence(home=self.home)
        self.fence.bootstrap()
        patcher = mock.patch(
            "mozyo_bridge.core.state.callback_publication_fence.CallbackPublicationFence",
            lambda *a, **k: CallbackPublicationFence(home=self.home),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        args = _parser().parse_args(["callback-publication", *argv])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_workflow_callback_publication(args)
        return rc, out.getvalue()

    def _key(self, anchor: str = ANCHOR) -> PublicationKey:
        return PublicationKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
                              dispatch_anchor=anchor, outcome=OUTCOME)

    def _reconcile(self, *disposition: str, anchor: str = ANCHOR) -> tuple[int, str]:
        return self._run(["--reconcile", ISSUE, GEN, anchor, OUTCOME, WS, LANE, *disposition])

    def test_a_disposition_without_reconcile_is_a_typo_not_a_no_op(self):
        rc, out = self._run(["--landed", "80500"])
        self.assertEqual(rc, 2)
        self.assertIn("--reconcile", out)

    def test_reconcile_without_a_disposition_refuses_to_guess(self):
        rc, out = self._reconcile()
        self.assertEqual(rc, 2)
        self.assertIn("only Redmine knows", out)

    def test_releasing_a_live_reserved_owner_exits_nonzero(self):
        # R10-F1 at the CLI boundary: the fence refuses, and the command must surface that as a
        # failure rather than print success.
        self.fence.reserve(self._key())
        rc, out = self._reconcile("--none-landed")
        self.assertEqual(rc, 1)
        self.assertIn("reconcile refused", out)
        self.assertIn("mid-PUT", out)

    def test_a_refusal_never_suggests_a_reset(self):
        # Pointing someone who mistyped an anchor at a store reset would walk them straight into
        # the duplicate this fence exists to prevent.
        self.fence.reserve(self._key())
        _, out = self._reconcile("--none-landed")
        self.assertNotIn("--recover", out)

    def test_an_absent_anchor_is_a_failure_not_a_silent_success(self):
        rc, out = self._reconcile("--landed", "80500", anchor="does-not-exist")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to reconcile", out)

    def test_a_published_anchor_cannot_be_reopened(self):
        res = self.fence.reserve(self._key())
        self.fence.mark_published(self._key(), res.token, "80500")
        for disposition in (["--none-landed"], ["--landed", "80501"]):
            rc, out = self._reconcile(*disposition)
            self.assertEqual(rc, 1)
            self.assertIn("terminal", out)

    def test_releasing_an_uncertain_identity_succeeds(self):
        res = self.fence.reserve(self._key())
        self.fence.mark_uncertain(self._key(), res.token)
        rc, out = self._reconcile("--none-landed")
        self.assertEqual(rc, 0)
        self.assertIn("released for republication", out)
        self.assertTrue(self.fence.reserve(self._key()).may_publish)

    def test_closing_a_reserved_anchor_as_landed_succeeds(self):
        self.fence.reserve(self._key())
        rc, out = self._reconcile("--landed", "80500")
        self.assertEqual(rc, 0)
        self.assertIn("published as journal 80500", out)
        self.assertFalse(self.fence.reserve(self._key()).may_publish)

    def test_list_shows_blocked_anchors_and_says_what_to_do(self):
        self.fence.reserve(self._key())
        rc, out = self._run(["--list"])
        self.assertEqual(rc, 0)
        self.assertIn(ANCHOR, out)
        self.assertIn("reserved", out)
        self.assertIn("--reconcile", out)

    def test_list_on_a_lost_store_fails_closed(self):
        self.fence.sidecar_path.unlink()
        rc, out = self._run(["--list"])
        self.assertEqual(rc, 1)
        self.assertIn("no reset", out)


if __name__ == "__main__":
    unittest.main()
