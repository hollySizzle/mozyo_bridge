"""A relative ``--workdir`` must resolve against the TARGET repo (Redmine #14249).

The live defect (#14249, reproduced from #14248 j#85389 and again with the cwd /
target-repo arrangement INVERTED in #14249 j#85453): the planner resolved
``--workdir`` with ``Path(workdir).expanduser().resolve()``, which resolves a
relative path against the *sender's process cwd*. So a governed

    handoff ... --target-repo <canonical lane worktree> --workdir .

asserted one repo and delivered a different execution root: ``repo_root`` was the
lane worktree while ``workdir`` was the coordinator's own root, ``relative`` fell
through to ``None``, and the pasteable record said "outside the target repo" — yet
the send was still reported ``sent`` / ``ok``. The portable ``--workdir .`` therefore
pointed receivers at a main/root worktree instead of the lane they were gated into
(dirty user-owned root edits, off-lane commits, broken review provenance).

Two properties are pinned here, both driven through the REAL
:class:`LiveEnvelopePlannerOps` against real directories — nothing hand-computes a
path or hand-writes an execution root:

1. With an asserted ``--target-repo``, a relative ``--workdir`` resolves against the
   target repo root, so ``--workdir .`` is A/B-identical to passing the lane's
   absolute path (the A/B the issue measured).
2. An execution root that does not live under an asserted ``--target-repo`` is a
   self-contradictory delivery and is refused BEFORE the transport rail
   (``EnvelopePlanError`` -> ``blocked`` / ``execution_root_outside_target_repo``),
   instead of being reported as a successful send.

The unchanged contracts (absolute workdir, nested workdir, and the #12098
out-of-tree redaction when NO ``--target-repo`` was asserted) are pinned alongside
them so the fence cannot quietly widen.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_envelope_planner import (
    EnvelopePlanError,
    HandoffEnvelopePlanner,
    LiveEnvelopePlannerOps,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_delivery_record,
    make_outcome,
    next_action_for,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (
    HandoffCommandInput,
)

ISSUE = "14249"
JOURNAL = "94327"


class _SenderCwdFixture(unittest.TestCase):
    """A sender cwd that is a DIFFERENT real directory from the target repo.

    This is the arrangement the issue reproduced under: the coordinator sends from
    its own root while asserting the lane worktree as ``--target-repo``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.sender_cwd = base / "coordinator_root"
        self.target_repo = base / "lane_worktree"
        self.nested = self.target_repo / "services" / "api"
        self.outside = base / "unrelated_checkout"
        for path in (self.sender_cwd, self.nested, self.outside):
            path.mkdir(parents=True)

        self._prev_cwd = Path.cwd()
        os.chdir(self.sender_cwd)
        self.addCleanup(self._restore)

        self.planner = HandoffEnvelopePlanner(LiveEnvelopePlannerOps())

    def _restore(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def _plan(self, workdir: str, *, target_repo: str | None):
        """Plan a delivery envelope through the real ops (pre-send stage)."""
        return self.planner.plan_delivery_envelope(
            HandoffCommandInput(
                source="redmine", issue=ISSUE, journal=JOURNAL, workdir=workdir
            ),
            anchor=RedmineAnchor(issue=ISSUE, journal=JOURNAL),
            callback_payload=None,
            consultation_payload=None,
            work_intake_payload=None,
            repo_root=self.sender_cwd,
            resolved_target_repo=target_repo,
            # The target pane's cwd; only consulted when NO --target-repo is asserted.
            target_cwd=str(self.target_repo),
            summary="regression",
            receiver="claude",
            kind="implementation_request",
        )


class RelativeWorkdirResolvesAgainstTargetRepoTest(_SenderCwdFixture):
    def test_dot_workdir_is_the_target_repo_not_the_sender_cwd(self) -> None:
        """The exact defect: `--workdir .` named the sender's root (#14249)."""
        env = self._plan(".", target_repo=str(self.target_repo))
        assert env.execution_root is not None
        self.assertEqual(env.execution_root.workdir, str(self.target_repo))
        self.assertEqual(env.execution_root.repo_root, str(self.target_repo))
        self.assertEqual(env.execution_root.relative, ".")
        # The sender's cwd must not appear anywhere in the carried root.
        self.assertNotEqual(env.execution_root.workdir, str(self.sender_cwd))

    def test_dot_workdir_matches_the_absolute_lane_path_ab(self) -> None:
        """The A/B the issue measured: `.` and the absolute lane path now agree."""
        relative_form = self._plan(".", target_repo=str(self.target_repo))
        absolute_form = self._plan(
            str(self.target_repo), target_repo=str(self.target_repo)
        )
        assert relative_form.execution_root is not None
        assert absolute_form.execution_root is not None
        self.assertEqual(
            relative_form.execution_root.to_dict(),
            absolute_form.execution_root.to_dict(),
        )

    def test_target_repo_root_renders_the_portable_dot_narrative(self) -> None:
        """`--workdir .` is portable narrative, not the redaction phrase."""
        env = self._plan(".", target_repo=str(self.target_repo))
        assert env.execution_root is not None
        clause = env.execution_root.notification_clause()
        self.assertTrue(
            clause.startswith("Target execution root: `.` (the target repo root)"),
            clause,
        )
        self.assertNotIn("outside the target repo", clause)
        # And no absolute path leaks into the pasteable record pointer.
        self.assertNotIn(str(self.target_repo), env.execution_root.record_pointer())

    def test_nested_relative_workdir_resolves_under_the_target_repo(self) -> None:
        env = self._plan("services/api", target_repo=str(self.target_repo))
        assert env.execution_root is not None
        self.assertEqual(env.execution_root.workdir, str(self.nested))
        self.assertEqual(env.execution_root.relative, "services/api")
        self.assertTrue(env.execution_root.is_nested)


class ContainmentFenceIsZeroSendTest(_SenderCwdFixture):
    def test_out_of_tree_workdir_under_asserted_target_repo_refuses(self) -> None:
        with self.assertRaises(EnvelopePlanError) as ctx:
            self._plan(str(self.outside), target_repo=str(self.target_repo))
        exc = ctx.exception
        self.assertEqual(exc.reason, "execution_root_outside_target_repo")
        carried = exc.outcome_extra["execution_root"]
        self.assertEqual(carried.repo_root, str(self.target_repo))
        self.assertIsNone(carried.relative)

    def test_relative_workdir_cannot_escape_the_asserted_target_repo(self) -> None:
        with self.assertRaises(EnvelopePlanError) as ctx:
            self._plan("../unrelated_checkout", target_repo=str(self.target_repo))
        self.assertEqual(
            ctx.exception.reason, "execution_root_outside_target_repo"
        )

    def test_refusal_owner_is_the_sender_with_an_actionable_repair(self) -> None:
        """The blocked outcome must route the repair to whoever sent it."""
        owner, action = next_action_for(
            "blocked", "execution_root_outside_target_repo", "claude"
        )
        self.assertEqual(owner, "sender")
        self.assertIn("--workdir", action)
        self.assertIn("--target-repo", action)

    def test_pasteable_record_explains_the_contradiction(self) -> None:
        """The durable record must not fall through to the generic phrase."""
        outcome = make_outcome(
            status="blocked",
            reason="execution_root_outside_target_repo",
            receiver="claude",
            target="%1",
            anchor=RedmineAnchor(issue=ISSUE, journal=JOURNAL),
            mode="queue-enter",
            kind="implementation_request",
            notification_marker=None,
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.next_action_owner, "sender")
        record = build_delivery_record(outcome)
        self.assertIn("Execution-root containment fence", record)
        self.assertNotIn("Handoff did not deliver; see structured outcome", record)


class UnchangedContractsTest(_SenderCwdFixture):
    """Non-regression: what #14249 must NOT change."""

    def test_absolute_workdir_under_target_repo_is_unchanged(self) -> None:
        env = self._plan(str(self.nested), target_repo=str(self.target_repo))
        assert env.execution_root is not None
        self.assertEqual(env.execution_root.workdir, str(self.nested))
        self.assertEqual(env.execution_root.relative, "services/api")

    def test_relative_workdir_without_target_repo_still_uses_sender_cwd(self) -> None:
        """No asserted repo -> no authoritative receiver frame; cwd base preserved."""
        (self.sender_cwd / "local_sub").mkdir()
        env = self._plan("local_sub", target_repo=None)
        assert env.execution_root is not None
        self.assertEqual(
            env.execution_root.workdir, str(self.sender_cwd / "local_sub")
        )

    def test_out_of_tree_workdir_without_target_repo_still_delivers(self) -> None:
        """The #12098 out-of-tree redaction path survives when nothing was asserted."""
        env = self._plan(str(self.outside), target_repo=None)
        assert env.execution_root is not None
        self.assertIsNone(env.execution_root.relative)
        self.assertIn("outside the target repo", env.execution_root.record_pointer())
        # Not fenced: a real envelope (body + marker) was produced.
        self.assertTrue(env.body)
        self.assertTrue(env.marker)


if __name__ == "__main__":
    unittest.main()
