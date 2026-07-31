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

Round 2 (late finding j#94419) closes the same defect on the public ``auto`` variant.
R1 fixed the path where the sender NAMES the target repo; ``--target-repo auto`` still
resolved, under the herdr backend, to the *sender's* own repo root (#13331 j#73312 #2),
so a coordinator sending ``--target-repo auto --workdir .`` from the ``main-next``
integration worktree carried that integration worktree as the receiver's execution root
— and R1's (correct) relative-workdir base then propagated it as if it had been
verified. :class:`AutoResolvesTheTargetLaneRootTest` drives the REAL resolver over a
REAL git repo, a REAL linked lane worktree and a REAL lifecycle authority row, then
feeds its answer through the REAL planner: nothing hand-computes the lane root.
"""

from __future__ import annotations

import os
import re
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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
    REFUSE_LANE_BINDING_ABSENT,
    REFUSE_LANE_WORKTREE_UNRESOLVED,
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


class AutoRefusalCarriesItsOwnReasonTest(unittest.TestCase):
    """An unresolvable ``auto`` must not inherit `target_repo_mismatch`'s repair advice.

    Review j#94499 finding 1: R2 emitted `target_repo_mismatch` for a herdr `auto` that
    could not establish the target's frame. That reason's DURABLE narrative asserts an
    observed target repo root disagreed, and its next_action offers "drop the flag to skip
    the repo gate" — but herdr observes no pane cwd, and dropping `--target-repo` restores
    the sender-cwd execution root #14249 exists to remove. The `die` string was right; the
    structured outcome, which is what survives, was not.
    """

    REASON = "auto_target_repo_unresolved"

    def test_the_refusal_never_tells_the_sender_to_drop_the_flag(self) -> None:
        owner, action = next_action_for("blocked", self.REASON, "codex")
        self.assertEqual(owner, "sender")
        # The exact advice that would walk back into the defect.
        self.assertNotIn("drop the flag", action)
        self.assertIn("explicit `--target-repo", action)

    def test_the_refusal_does_not_claim_an_observed_pane_repo_root(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason=self.REASON,
            receiver="codex",
            target="mzb1_ws_codex_lane",
            anchor=RedmineAnchor(issue=ISSUE, journal="94488"),
            mode="queue-enter",
            kind="review_request",
            notification_marker=None,
        )
        record = build_delivery_record(outcome)
        # herdr read no pane cwd, so the record must not assert one disagreed.
        self.assertNotIn("Target pane's inferred repo root", record)
        self.assertNotIn("Handoff did not deliver; see structured outcome", record)
        self.assertEqual(outcome.next_action_owner, "sender")

    def test_it_stays_distinct_from_target_repo_mismatch(self) -> None:
        """Both are sender-owned, but the repairs differ — that is why it is a new reason."""
        _, auto_action = next_action_for("blocked", self.REASON, "codex")
        _, mismatch_action = next_action_for("blocked", "target_repo_mismatch", "codex")
        self.assertNotEqual(auto_action, mismatch_action)
        self.assertIn("drop the flag", mismatch_action)


class AutoRefusalIsDurableAndTypedTest(unittest.TestCase):
    """The refusal must reach its readers with the fact they need (review j#95843 F1/F2).

    R3 wrote a ``next_action`` pointing at "the structured detail" that ``DeliveryOutcome`` had
    no field for, folded a newer-than-runtime store into the same reason as a corrupt one, and
    left the new reason out of the closed zero-send consumer sets so a proven pre-injection
    refusal degraded to ``uncertain``. Each of those is pinned here at the surface its reader
    actually observes.
    """

    def _refusal(self, reason: str, detail: str = "d"):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            AutoTargetRoot,
        )

        return AutoTargetRoot(reason=reason, detail=detail)

    def test_a_newer_store_maps_onto_the_existing_reader_upgrade_reason(self) -> None:
        """Review j#95843 point 2: do not mint a second token for a condition that has one."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            REFUSE_STORE_UPGRADE_REQUIRED,
        )

        self.assertEqual(
            self._refusal(REFUSE_STORE_UPGRADE_REQUIRED).wire_reason(),
            "reader_upgrade_required",
        )

    def test_other_auto_failures_keep_the_auto_reason(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            REFUSE_FOREIGN_WORKSPACE,
            REFUSE_LANE_BINDING_ABSENT,
            REFUSE_STORE_UNREADABLE,
        )

        for reason in (
            REFUSE_LANE_BINDING_ABSENT,
            REFUSE_FOREIGN_WORKSPACE,
            REFUSE_STORE_UNREADABLE,
        ):
            self.assertEqual(
                self._refusal(reason).wire_reason(), "auto_target_repo_unresolved", reason
            )

    def test_the_subreason_actually_reaches_the_outcome(self) -> None:
        """The exact gap: the advice referenced a field that did not exist."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            REFUSE_LANE_BINDING_ABSENT,
        )

        refusal = self._refusal(REFUSE_LANE_BINDING_ABSENT, detail="no row owns lane 'x'")
        outcome = make_outcome(
            status="blocked",
            reason=refusal.wire_reason(),
            receiver="codex",
            target="mzb1_x",
            anchor=RedmineAnchor(issue=ISSUE, journal="95831"),
            mode="queue-enter",
            kind="review_request",
            notification_marker=None,
            auto_target_repo=refusal.to_structured_dict(),
        )
        carried = outcome.to_dict()["auto_target_repo"]
        self.assertEqual(carried["subreason"], REFUSE_LANE_BINDING_ABSENT)
        self.assertIn("lane", carried["detail"])
        # The invariant is that the reader can tell WHICH step failed from what they were
        # handed. Since review j#95995 F1 the next_action states the subreason inline rather
        # than pointing at a field to go look up — strictly more legible, same invariant.
        self.assertIn(REFUSE_LANE_BINDING_ABSENT, outcome.next_action)

    def test_the_narrative_does_not_assert_a_cause_it_cannot_know(self) -> None:
        """R3's narrative claimed the worktree binding was non-unique — false for most cases.

        Read off the pasteable record, i.e. the text a human actually receives.
        """
        record = build_delivery_record(
            make_outcome(
                status="blocked",
                reason="auto_target_repo_unresolved",
                receiver="codex",
                target="mzb1_x",
                anchor=RedmineAnchor(issue=ISSUE, journal="95831"),
                mode="queue-enter",
                kind="review_request",
                notification_marker=None,
            )
        )
        self.assertNotIn("did not resolve to exactly one live worktree", record)
        self.assertIn("auto_target_repo.subreason", record)

    def test_proven_zero_send_refusals_are_not_sent_not_uncertain(self) -> None:
        """Review j#95843 F2: a pre-injection refusal must not degrade to `uncertain`."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E501
            send_outcome_for_delivery,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
            SEND_KNOWN_NOT_SENT_REASONS,
        )

        for reason in (
            "auto_target_repo_unresolved",
            # R1's fence carried the same defect from the start (self-swept, j#95849).
            "execution_root_outside_target_repo",
            # Reached by the auto path since point 2; its sole emit site dies pre-injection.
            "reader_upgrade_required",
        ):
            self.assertEqual(
                send_outcome_for_delivery("blocked", reason), "not_sent", reason
            )
            self.assertIn(reason, SEND_KNOWN_NOT_SENT_REASONS, reason)

    def test_the_pasteable_record_carries_the_subreason_value(self) -> None:
        """Review j#95911 F1: the markdown — not just the JSON — is what gets persisted.

        R4 put the subreason on the wire outcome while the record rendered only the
        next_action telling the reader to go look at it. `--persist-delivery` stores this
        markdown, so the discriminating fact has to be legible in it.
        """
        record = build_delivery_record(
            make_outcome(
                status="blocked",
                reason="auto_target_repo_unresolved",
                receiver="codex",
                target="mzb1_x",
                anchor=RedmineAnchor(issue=ISSUE, journal="95887"),
                mode="queue-enter",
                kind="review_request",
                notification_marker=None,
                auto_target_repo={
                    "subreason": "lane_binding_absent",
                    "basis": "",
                    "detail": "no row owns lane 'x'",
                },
            )
        )
        self.assertIn("lane_binding_absent", record)
        self.assertIn("- Auto target-repo: subreason `lane_binding_absent`", record)

    def test_a_non_auto_outcome_record_is_unchanged(self) -> None:
        """The new line must not appear on every other delivery record."""
        record = build_delivery_record(
            make_outcome(
                status="sent",
                reason="ok",
                receiver="codex",
                target="mzb1_x",
                anchor=RedmineAnchor(issue=ISSUE, journal="95887"),
                mode="queue-enter",
                kind="review_request",
                notification_marker="m",
            )
        )
        self.assertNotIn("Auto target-repo", record)

    def test_no_refusal_payload_carries_a_filesystem_path(self) -> None:
        """Review j#95911 F2: R4 interpolated a raw exception and leaked the home path.

        Driven through the REAL ``resolve_herdr_auto_target_repo`` with the reader raising
        exceptions whose text carries a path — because that is where the leak lived. An
        earlier version of this test built payloads by hand and passed happily while the
        defect was reintroduced: it never entered the ``except`` branches at all.
        """
        import mozyo_bridge.core.state.lane_lifecycle_readonly as readonly
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            resolve_herdr_auto_target_repo,
        )
        from mozyo_bridge.core.state.lane_lifecycle_schema import LaneLifecycleError

        secret = Path.home() / ".mozyo_bridge" / "state.sqlite"
        cases = (
            readonly.LaneLifecycleReaderUpgradeRequired(
                f"lane lifecycle store at {secret} is version 9, this build reads 7"
            ),
            LaneLifecycleError(f"unable to open database file: {secret}"),
            OSError(f"[Errno 13] Permission denied: '{secret}'"),
        )
        original = readonly.LaneLifecycleReader.records
        for exc in cases:
            def _raise(self, _exc=exc):  # noqa: ANN001
                raise _exc

            readonly.LaneLifecycleReader.records = _raise
            try:
                refusal = resolve_herdr_auto_target_repo(
                    Path(ROOT),
                    {
                        "id": "x", "cwd": "",
                        "workspace_id": "ws", "lane_id": "target_lane",
                        "herdr_sender_workspace_id": "ws",
                        "herdr_sender_lane_id": "sender_lane",
                    },
                )
            finally:
                readonly.LaneLifecycleReader.records = original
            payload = refusal.to_structured_dict()
            label = f"{type(exc).__name__}:{payload['subreason']}"
            # The refusal must still be typed…
            self.assertFalse(refusal.ok, label)
            self.assertTrue(payload["subreason"], label)
            # …and must not republish the store path onto wire / record / stderr.
            for key, value in payload.items():
                self.assertNotIn(str(secret), str(value), f"{label}.{key}")
                self.assertNotIn(str(Path.home()), str(value), f"{label}.{key}")
                self.assertNotIn("/", str(value), f"{label}.{key}")

    def test_the_terminal_error_gives_each_subreason_its_own_repair(self) -> None:
        """Review j#95911 F4: stderr told every refusal to repair a worktree binding."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            AUTO_TARGET_REPO_SUBREASON_REPAIR,
            auto_target_repo_die_message,
        )

        # A step `identity_unattested` never reaches must not be prescribed to it.
        unattested = auto_target_repo_die_message("identity_unattested", "d")
        self.assertNotIn("repair that lane's worktree binding", unattested)
        self.assertNotIn("repair the lane's worktree binding", unattested)
        self.assertIn("attested lane agent", unattested)

        # The newer-store case must say "use a current runtime", never "repair the binding".
        upgrade = auto_target_repo_die_message("lifecycle_store_upgrade_required", "d")
        self.assertIn("do NOT downgrade", upgrade)
        self.assertNotIn("worktree binding", upgrade)

        # Every subreason the resolver can emit has its own entry.
        for subreason in (
            "identity_unattested", "foreign_workspace", "lane_binding_absent",
            "lane_binding_unbound", "lane_worktree_unresolved",
            "lifecycle_store_unreadable", "lifecycle_store_upgrade_required",
        ):
            self.assertIn(subreason, AUTO_TARGET_REPO_SUBREASON_REPAIR, subreason)
            # …and the invariant holds for all of them.
            self.assertIn(
                "Do NOT drop `--target-repo`",
                auto_target_repo_die_message(subreason, "d"),
                subreason,
            )

    #: Every subreason the resolver can actually emit, paired with the wire reason it maps
    #: onto. Derived from the module's own REFUSE_* constants so a new subreason cannot be
    #: added without this matrix noticing.
    def _all_subreasons(self) -> "list[str]":
        import mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root as mod

        subs = sorted(
            getattr(mod, name) for name in dir(mod) if name.startswith("REFUSE_")
        )
        self.assertEqual(len(subs), 7, subs)
        return subs

    def _outcome_for(self, subreason: str):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            AutoTargetRoot,
        )

        refusal = AutoTargetRoot(reason=subreason, detail="d")
        return make_outcome(
            status="blocked",
            reason=refusal.wire_reason(),
            receiver="codex",
            target="mzb1_x",
            anchor=RedmineAnchor(issue=ISSUE, journal="95964"),
            mode="queue-enter",
            kind="review_request",
            notification_marker=None,
            auto_target_repo=refusal.to_structured_dict(),
        )

    def test_next_action_is_correct_per_subreason_on_wire_and_record(self) -> None:
        """Review j#95995 F1: `next_action` rides the wire AND the pasteable record.

        R5 gave stderr a per-subreason repair and left `next_action` sharing ONE text across
        six subreasons — a text that omitted `lifecycle_store_unreadable` from its cause list
        and told all of them to repair a worktree binding. Measured here on the REAL outcome,
        for every subreason, on both surfaces the reader receives.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            AUTO_TARGET_REPO_SUBREASON_REPAIR,
        )

        actions = {}
        for subreason in self._all_subreasons():
            outcome = self._outcome_for(subreason)
            action = outcome.next_action
            record = build_delivery_record(outcome)
            actions[subreason] = action
            # (a) the subreason is always legible on the record the reader keeps.
            self.assertIn(subreason, record, subreason)
            if outcome.reason != "auto_target_repo_unresolved":
                # `lifecycle_store_upgrade_required` maps onto `reader_upgrade_required`,
                # whose own next_action already IS the one correct repair (use a current
                # runtime, never downgrade). Requiring the token there would only restate
                # what the wire reason already says unambiguously.
                self.assertIn("do NOT downgrade", action, subreason)
                continue
            # (b) where the wire reason alone is ambiguous, the action names WHICH step…
            self.assertIn(subreason, action, subreason)
            # …and carries the repair that fits that step…
            self.assertIn(
                AUTO_TARGET_REPO_SUBREASON_REPAIR[subreason], action, subreason
            )
            # …and the invariant that holds for all of them.
            self.assertIn("Do NOT drop `--target-repo`", action, subreason)
        # (c) no two subreasons share a next_action — the R5 defect was 6 sharing one.
        self.assertEqual(len(set(actions.values())), len(actions), sorted(actions))

    def test_no_subreason_is_told_to_repair_a_binding_it_never_reached(self) -> None:
        """The specific wrong advice, per surface, for the subreasons it cannot help."""
        for subreason in (
            "identity_unattested", "foreign_workspace", "lifecycle_store_unreadable",
            "lifecycle_store_upgrade_required",
        ):
            outcome = self._outcome_for(subreason)
            for surface, text in (
                ("next_action", outcome.next_action),
                ("record", build_delivery_record(outcome)),
            ):
                self.assertNotIn(
                    "repair that lane's worktree binding", text, f"{subreason}/{surface}"
                )

    def test_the_fallback_advice_is_safe_not_the_retired_specific_text(self) -> None:
        """Review j#96019 F1: the least-informed path must give the most conservative advice.

        R6 fixed the known-token path and left missing/unknown falling back to the R5 text
        that j#95995 had just retired — enumerating causes (without
        `lifecycle_store_unreadable`), pointing at a payload that by definition is absent,
        and prescribing a worktree-binding repair. The previous version of THIS test asserted
        byte-equality with that mapping, so the guard pinned the defect instead of catching
        it. It now checks what the advice says.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            AUTO_TARGET_REPO_GENERIC_REPAIR,
            execution_root_fence_next_action,
        )

        for label, payload in (
            ("missing", None),
            ("unknown", {"subreason": "future_refusal_this_build_never_heard_of"}),
        ):
            action = execution_root_fence_next_action(
                "auto_target_repo_unresolved", payload
            )
            # no cause is invented…
            self.assertNotIn("repair that lane's worktree binding", action, label)
            self.assertNotIn("an unattested sender/target identity", action, label)
            # …the reader is not sent to a field that is absent or uninformative…
            self.assertNotIn("auto_target_repo.subreason", action, label)
            # …the advice is the one true of every refusal, and the invariant is kept.
            self.assertIn(AUTO_TARGET_REPO_GENERIC_REPAIR, action, label)
            self.assertIn("Do NOT drop `--target-repo`", action, label)
        # An unknown token is surfaced as an unknown, not silently dropped.
        unknown = execution_root_fence_next_action(
            "auto_target_repo_unresolved",
            {"subreason": "future_refusal_this_build_never_heard_of"},
        )
        self.assertIn("future_refusal_this_build_never_heard_of", unknown)
        self.assertIn("not one this build knows", unknown)

    def test_the_containment_reason_keeps_its_own_repair_whatever_the_payload(self) -> None:
        """Review j#96019 F1: a mismatched payload must not swap in another reason's advice."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            EXECUTION_ROOT_FENCE_NEXT_ACTION,
            execution_root_fence_next_action,
        )

        expected = EXECUTION_ROOT_FENCE_NEXT_ACTION["execution_root_outside_target_repo"]
        for payload in (
            None,
            {"subreason": "lane_binding_absent"},          # a REAL auto subreason
            {"subreason": "future_refusal"},               # an unknown one
        ):
            self.assertEqual(
                execution_root_fence_next_action(
                    "execution_root_outside_target_repo", payload
                ),
                expected,
                payload,
            )
        # And it still carries the repair that belongs to IT.
        self.assertIn("`--workdir` inside the target repo", expected)

    #: Hostile subreason values. R7 added an "unknown token" display and tested exactly one
    #: safe lower-snake-case example; review j#96042 measured that a newline crossed the
    #: markdown line boundary, an absolute path survived into the ticket-facing record, and a
    #: 10,000-char value produced a 10,530-char next_action.
    HOSTILE_TOKENS = (
        ("newline", "future\n- Injected: yes"),
        ("crlf", "future\r\nx"),
        ("backtick", "future`x`"),
        ("markdown", "future](http://x)"),
        ("posix_path", "/Users/example/private/repo"),
        ("windows_path", "C:\\Users\\example\\repo"),
        ("home_path", "~/private/repo"),
        ("overlong", "f" * 10000),
        ("whitespace", "   "),
        ("leading_space", " future"),
        ("uppercase", "FutureRefusal"),
        ("dash", "future-refusal"),
        ("empty_after_strip", "\t\n "),
    )

    def test_no_hostile_subreason_reaches_a_durable_surface(self) -> None:
        """Review j#96042 F1: the unknown-token path must prove the value, not inherit it.

        The producer emits a closed vocabulary, but this path exists precisely for values the
        producer did not make. Checked on both durable surfaces: `next_action` (wire + record)
        and the record's own subreason line.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            SUBREASON_TOKEN_MAX_LENGTH,
            SUBREASON_TOKEN_PLACEHOLDER,
            auto_target_repo_lines,
            execution_root_fence_next_action,
        )

        for label, token in self.HOSTILE_TOKENS:
            action = execution_root_fence_next_action(
                "auto_target_repo_unresolved", {"subreason": token}
            )
            rendered = " ".join(auto_target_repo_lines({"subreason": token, "basis": token}))
            for surface, text in (("next_action", action), ("record", rendered)):
                where = f"{label}/{surface}"
                # single line — a token cannot break out of its markdown line
                self.assertNotIn("\n", text, where)
                self.assertNotIn("\r", text, where)
                # path-free — the property review j#95911 F2 established, kept on this path
                self.assertNotIn("/", text, where)
                self.assertNotIn("\\", text, where)
                self.assertNotIn("~", text, where)
                # bounded
                self.assertLess(len(text), 1000, where)
                # the raw value never appears; the reader sees it was refused
                self.assertNotIn(token, text, where)
                self.assertIn(SUBREASON_TOKEN_PLACEHOLDER, text, where)
        # the cap is a real number, and a token at the boundary still renders
        self.assertGreater(SUBREASON_TOKEN_MAX_LENGTH, 0)

    def test_a_wellformed_unknown_token_is_still_shown(self) -> None:
        """The hardening must not swallow the legitimate case R7 added it for."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            SUBREASON_TOKEN_MAX_LENGTH,
            renderable_subreason_token,
            execution_root_fence_next_action,
        )

        token = "future_refusal_this_build_never_heard_of"
        action = execution_root_fence_next_action(
            "auto_target_repo_unresolved", {"subreason": token}
        )
        self.assertIn(token, action)
        self.assertIn("not one this build knows", action)
        # exactly at the cap renders; one over does not
        at_cap = "f" * SUBREASON_TOKEN_MAX_LENGTH
        self.assertEqual(renderable_subreason_token(at_cap), at_cap)
        self.assertNotEqual(
            renderable_subreason_token("f" * (SUBREASON_TOKEN_MAX_LENGTH + 1)), at_cap
        )

    def test_a_malformed_payload_never_raises(self) -> None:
        """Review j#96042 F1: a wording helper must not add a failure to the one reported."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            auto_target_repo_lines,
            execution_root_fence_next_action,
        )

        for label, payload in (
            ("unhashable subreason", {"subreason": ["future"]}),   # raised TypeError
            ("payload not a mapping", "notadict"),                 # raised AttributeError
            ("payload is a list", ["subreason"]),
            ("int subreason", {"subreason": 5}),
            ("none subreason", {"subreason": None}),
            ("no payload", None),
        ):
            action = execution_root_fence_next_action("auto_target_repo_unresolved", payload)
            self.assertTrue(action, label)
            self.assertNotIn("\n", action, label)
            self.assertIsInstance(auto_target_repo_lines(payload), list, label)

    def test_no_adversarial_special_method_reaches_a_durable_surface(self) -> None:
        """Review j#96049 F1: validating characters is not the same as using the result.

        R8 checked the regex and the length on the caller's object and then handed that same
        object to a dict lookup and an f-string, so `__hash__` raised and `__format__` put a
        newline and an absolute path back into text that had just been proven free of both.
        The `str.__str__` idiom this repo already ruled on (j#86068 / j#86081) is what makes
        the checked characters and the printed characters the same characters.

        `__len__` / `__str__` / `__getitem__` / `__bool__` are probed beyond the review's
        list; `__len__` was still escaping when this was first run.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            SUBREASON_TOKEN_PLACEHOLDER,
            auto_target_repo_lines,
            execution_root_fence_next_action,
            renderable_subreason_token,
        )

        hostile = "future\n- Injected: /Users/example/private"

        class Fmt(str):
            def __format__(self, spec): return hostile

        class Unhashable(str):
            __hash__ = None

        class RaisingHash(str):
            def __hash__(self): raise RuntimeError("hash")

        class RaisingEq(str):
            def __eq__(self, other): raise RuntimeError("eq")
            def __hash__(self): return 0

        class RaisingLen(str):
            def __len__(self): raise RuntimeError("len")

        class RaisingStr(str):
            def __str__(self): raise RuntimeError("str")

        class RaisingGetItem(str):
            def __getitem__(self, key): raise RuntimeError("getitem")

        class RaisingGet(dict):
            def get(self, *a, **k): raise RuntimeError("get")

        class RaisingBool(dict):
            def __bool__(self): raise RuntimeError("bool")

        payloads = [
            ("format", {"subreason": Fmt("future_refusal")}),
            ("unhashable", {"subreason": Unhashable("future_refusal")}),
            ("hash", {"subreason": RaisingHash("future_refusal")}),
            ("eq", {"subreason": RaisingEq("future_refusal")}),
            ("len", {"subreason": RaisingLen("future_refusal")}),
            ("str", {"subreason": RaisingStr("future_refusal")}),
            ("getitem", {"subreason": RaisingGetItem("future_refusal")}),
            ("mapping_get", RaisingGet(subreason="future_refusal")),
            ("mapping_bool", RaisingBool(subreason="future_refusal")),
        ]
        for label, payload in payloads:
            action = execution_root_fence_next_action(
                "auto_target_repo_unresolved", payload
            )
            record = " ".join(auto_target_repo_lines(payload))
            for surface, text in (("next_action", action), ("record", record)):
                where = f"{label}/{surface}"
                self.assertNotIn("\n", text, where)
                self.assertNotIn("\r", text, where)
                self.assertNotIn("/", text, where)
                self.assertNotIn("\\", text, where)
                self.assertLess(len(text), 1000, where)
        # the validator hands back an EXACT str, so nothing downstream can re-dispatch
        self.assertIs(type(renderable_subreason_token(Fmt("future_refusal"))), str)
        self.assertEqual(
            renderable_subreason_token(Unhashable("Not A Token")),
            SUBREASON_TOKEN_PLACEHOLDER,
        )

    def test_a_forged_or_raising_class_attribute_cannot_break_totality(self) -> None:
        """Review j#96064 F1: admission itself must not run the caller's code.

        R9 owned the value only AFTER `isinstance(value, str)`, and `isinstance` consults
        `__class__` when the actual type is not `str`. So an object whose `__class__` property
        returns `str` was admitted and then raised inside `str.__str__`, and one whose
        `__class__` raises propagated out of the admission test. I had flagged this family as
        "structurally closed but untested" in the R9 review request; one of the three was not
        closed. Admission is now the ownership attempt itself.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            SUBREASON_TOKEN_PLACEHOLDER,
            auto_target_repo_lines,
            execution_root_fence_next_action,
            renderable_subreason_token,
        )

        hostile = "future\n- Injected: /Users/example/private"

        class ForgedClass:
            @property
            def __class__(self): return str

        class RaisingClass:
            @property
            def __class__(self): raise RuntimeError("class")

        class ForgedClassWithFormat:
            @property
            def __class__(self): return str
            def __format__(self, spec): return hostile

        for label, obj in (
            ("forged", ForgedClass()),
            ("raising", RaisingClass()),
            ("forged_with_format", ForgedClassWithFormat()),
        ):
            # validator closes to the placeholder rather than raising…
            self.assertEqual(
                renderable_subreason_token(obj), SUBREASON_TOKEN_PLACEHOLDER, label
            )
            # …and both durable surfaces stay total and clean.
            action = execution_root_fence_next_action(
                "auto_target_repo_unresolved", {"subreason": obj}
            )
            record = " ".join(auto_target_repo_lines({"subreason": obj, "basis": obj}))
            for surface, text in (("next_action", action), ("record", record)):
                where = f"{label}/{surface}"
                self.assertNotIn("\n", text, where)
                self.assertNotIn("\r", text, where)
                self.assertNotIn("/", text, where)
                self.assertLess(len(text), 1000, where)

    def test_genuine_strings_are_still_admitted(self) -> None:
        """Positive control: hardening admission must not reject the real cases."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            SUBREASON_TOKEN_PLACEHOLDER,
            renderable_subreason_token,
        )

        class RealSubclass(str):
            pass

        # exact str
        self.assertEqual(renderable_subreason_token("future_refusal"), "future_refusal")
        # a genuine str subclass is still accepted — and owned into an exact str
        owned = renderable_subreason_token(RealSubclass("future_refusal"))
        self.assertEqual(owned, "future_refusal")
        self.assertIs(type(owned), str)
        # and a genuine subclass whose CHARACTERS are unacceptable still closes
        self.assertEqual(
            renderable_subreason_token(RealSubclass("Not A Token")),
            SUBREASON_TOKEN_PLACEHOLDER,
        )

    def test_control_and_bidi_characters_close_to_the_placeholder(self) -> None:
        """Explicit rows for NUL / RTL override / zero-width / bell / non-ASCII (j#96049)."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.gateway_route_wording import (  # noqa: E501
            SUBREASON_TOKEN_PLACEHOLDER,
            renderable_subreason_token,
        )

        for label, token in (
            ("nul", "fut\x00ure"),
            ("rtl_override", "fut\u202eure"),
            ("zero_width", "fut\u200bure"),
            ("bell", "fut\a"),
            ("non_ascii_letter", "fut\u00fcre"),
            ("emoji", "fut\U0001f642"),
            ("vertical_tab", "fut\vure"),
            ("form_feed", "fut\fure"),
        ):
            self.assertEqual(
                renderable_subreason_token(token), SUBREASON_TOKEN_PLACEHOLDER, label
            )

    def test_payload_less_outcomes_still_route_to_the_sender(self) -> None:
        """Non-regression: owner and reachability of both fence reasons without a payload."""
        for reason in ("execution_root_outside_target_repo", "auto_target_repo_unresolved"):
            owner, action = next_action_for("blocked", reason, "codex")
            self.assertEqual(owner, "sender", reason)
            self.assertIn("Do NOT drop", action) if reason.startswith("auto") else None
            self.assertTrue(action)

    def test_post_injection_reasons_still_stay_uncertain(self) -> None:
        """The guard must keep its teeth — widening the set must not swallow these."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E501
            send_outcome_for_delivery,
        )

        for reason in ("marker_timeout", "receiver_blocked", "turn_start_absent"):
            self.assertEqual(
                send_outcome_for_delivery("blocked", reason), "uncertain", reason
            )


class PublicHelpMatchesTheCurrentContractTest(unittest.TestCase):
    """`--help` is a contract surface too (review j#95843 F3).

    R2/R3 corrected the narrative and the docs but never rendered `--help`, which kept
    advising "Drop the flag to skip the repo gate" — the one recovery j#94456 prohibits — and
    described `auto` as tmux-only. Rendered from THIS source tree, not the installed console
    script, which is what made the staleness invisible.
    """

    def _help_raw(self, argv: "list[str]") -> str:
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-m", "mozyo_bridge", *argv],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _help(self, subcommand: str) -> str:
        return self._help_raw(["handoff", subcommand, "--help"])

    def _subcommands(self) -> "list[str]":
        """Every `handoff` subcommand, DERIVED — never hand-listed (review j#95911 F3).

        R4 hand-listed three and asserted in a comment that they were "every subparser that
        exposes --target-repo". There are seven, and the one it missed
        (``ticketless-callback``) is built by a separate module that still carried the old
        contract. A hand-written enumeration cannot notice a parser nobody remembered.
        """
        text = self._help_raw(["handoff", "--help"])
        inside = text.split("{", 1)[1].split("}", 1)[0]
        subs = [s.strip() for s in inside.split(",") if s.strip()]
        self.assertGreaterEqual(len(subs), 7, subs)
        return subs

    def _target_repo_subcommands(self) -> "list[str]":
        """The derived subcommands that actually expose ``--target-repo``."""
        found = [s for s in self._subcommands() if "--target-repo" in self._help(s)]
        self.assertGreaterEqual(len(found), 7, found)
        return found

    #: Contract violations, matched by SHAPE rather than one parser's exact sentence. R4
    #: grepped the literal "Drop the flag to skip the repo gate"; the ticketless parser says
    #: "Drop to skip the repo gate", so the sweep reported a clean zero it had not earned.
    FORBIDDEN = (
        re.compile(r"drop\b.{0,20}\bto skip the repo gate", re.I),
        # `auto` stated as universally requiring a %pane — true only under tmux.
        re.compile(r"`auto`\s+requires an explicit\s+`?%pane", re.I),
    )

    def test_no_target_repo_parser_advises_dropping_the_flag_to_recover(self) -> None:
        offenders = []
        for sub in self._target_repo_subcommands():
            text = " ".join(self._help(sub).split())
            for pattern in self.FORBIDDEN:
                if pattern.search(text):
                    offenders.append((sub, pattern.pattern))
        self.assertEqual(offenders, [], f"stale --target-repo contract in: {offenders}")

    def test_auto_is_documented_per_backend_not_as_tmux_only(self) -> None:
        text = " ".join(self._help("send").split())
        self.assertIn("under herdr", text)
        self.assertIn("lifecycle worktree binding", text)
        # The tmux requirement must stay, but scoped to tmux rather than stated absolutely.
        self.assertIn("under tmux", text)

    def test_help_names_the_fail_closed_reasons_and_forbids_the_fallback(self) -> None:
        text = " ".join(self._help("send").split())
        self.assertIn("auto_target_repo_unresolved", text)
        self.assertIn("never fall back to the sender's root", text)


class AutoResolvesTheTargetLaneRootTest(unittest.TestCase):
    """``--target-repo auto`` must name the TARGET lane's root (Redmine #14249 R2 / j#94419).

    Real git repo + real linked lane worktree + real lifecycle authority row, and the sender
    is a DIFFERENT worktree of that repo — the exact arrangement j#94419 reproduced. The
    lane's canonical root is never handed to the resolver: it must re-derive it from the
    lifecycle ``worktree_identity`` token against the repo's own live worktree listing.
    """

    #: A branch name that is NOT the lane id, so a resolver that inferred "lane id == branch"
    #: (retired by review j#86739 R3-F2) would resolve nothing here rather than pass by luck.
    BRANCH = "feature/decoupled_from_the_lane_id"
    LANE = "issue_14249_target_repo_workdir_resolution"
    SENDER_LANE = "main_next_integration"

    def setUp(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            DecisionPointer,
            LaneLifecycleKey,
        )
        from mozyo_bridge.core.state.workspace_registry import write_anchor
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        sys.path.insert(0, str(ROOT / "tests"))
        from support.herdr_workspace_fixtures import _anchor_record  # noqa: PLC0415

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        # Hermetic home: the anchor registry AND the lifecycle authority both resolve through
        # `mozyo_bridge_home()`, so the whole fixture stays off the operator's real state.
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

        self.repo = base / "repo"
        env = self._env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "PATH": "/usr/bin:/bin",
        }
        self._git("init", "-q", "-b", "main", str(self.repo), env=env)
        # Redmine #14685: a synthetic repo must not let git's auto maintenance daemonize into
        # the temp tree that `TemporaryDirectory` is about to remove.
        self._git("-C", str(self.repo), "config", "--local", "maintenance.auto", "false", env=env)
        self._git("-C", str(self.repo), "config", "--local", "gc.auto", "0", env=env)
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("-C", str(self.repo), "add", "-A", env=env)
        self._git("-C", str(self.repo), "commit", "-qm", "c1", env=env)

        # The lane worktree (the TARGET), and a second worktree the sender runs in.
        self.lane_worktree = base / "wt-lane"
        self._git(
            "-C", str(self.repo), "worktree", "add", "-q",
            str(self.lane_worktree), "-b", self.BRANCH, env=env,
        )
        self.sender_worktree = base / "wt-integration"
        self._git(
            "-C", str(self.repo), "worktree", "add", "-q",
            str(self.sender_worktree), "-b", "integration_side", env=env,
        )
        (self.lane_worktree / "services" / "api").mkdir(parents=True)

        self.workspace_id = "fixture-14249-workspace"
        write_anchor(self.repo, _anchor_record(self.workspace_id, self.repo))
        scope = repo_scope_workspace_id(self.sender_worktree)
        if scope != self.workspace_id:
            self.skipTest(
                f"the fixture repo resolved workspace {scope!r}, not the anchored "
                f"{self.workspace_id!r} (is TMPDIR inside a git worktree?)"
            )

        store = LaneLifecycleStore(home=self.home)
        store.ensure_schema()
        outcome = store.declare_active(
            LaneLifecycleKey(self.workspace_id, self.LANE),
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id="94456"
            ),
            issue_id=ISSUE,
            worktree_identity=derive_lane_workspace_token(
                str(self.lane_worktree.resolve())
            ),
        )
        self.assertTrue(outcome.applied, outcome.reason)

        self.planner = HandoffEnvelopePlanner(LiveEnvelopePlannerOps())
        self._prev_cwd = Path.cwd()
        os.chdir(self.sender_worktree)
        self.addCleanup(self._restore_cwd)

    def _restore_home(self) -> None:
        if self._prev_home is None:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
        else:
            os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home

    def _restore_cwd(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def _git(self, *args: str, env: dict | None = None) -> None:
        import subprocess

        subprocess.run(
            ["git", *args], check=True, capture_output=True, env=env or self._env
        )

    def _target_info(self, *, lane: str) -> dict:
        """The synthesized herdr target record for a send at ``lane``'s pane.

        ``cwd`` is EMPTY exactly as ``resolve_herdr_send_target`` leaves it for ``auto``:
        herdr reads no pane cwd, and the sender's root is not the target's.
        """
        return {
            "id": "mzb1_ws_claude_lane",
            "cwd": "",
            "workspace_id": self.workspace_id,
            "lane_id": lane,
            "herdr_sender_workspace_id": self.workspace_id,
            "herdr_sender_lane_id": self.SENDER_LANE,
        }

    def _resolve_auto(self, *, lane: str):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
            resolve_herdr_auto_target_repo,
        )

        return resolve_herdr_auto_target_repo(
            self.sender_worktree, self._target_info(lane=lane)
        )

    def _plan(self, workdir: str, *, target_repo: str | None):
        return self.planner.plan_delivery_envelope(
            HandoffCommandInput(
                source="redmine", issue=ISSUE, journal="94456", workdir=workdir
            ),
            anchor=RedmineAnchor(issue=ISSUE, journal="94456"),
            callback_payload=None,
            consultation_payload=None,
            work_intake_payload=None,
            repo_root=self.sender_worktree,
            resolved_target_repo=target_repo,
            target_cwd=target_repo or "",
            summary="regression",
            receiver="claude",
            kind="implementation_request",
        )

    def test_auto_resolves_the_target_lane_worktree_not_the_sender(self) -> None:
        """The exact j#94419 defect: auto named the sender's integration worktree."""
        resolved = self._resolve_auto(lane=self.LANE)
        self.assertTrue(resolved.ok, f"{resolved.reason}: {resolved.detail}")
        self.assertEqual(resolved.root, str(self.lane_worktree.resolve()))
        self.assertNotEqual(resolved.root, str(self.sender_worktree.resolve()))

    def test_auto_carries_the_target_lane_root_into_the_execution_root(self) -> None:
        """Acceptance 2: `--workdir .` under auto is the target lane root, relative `.`."""
        resolved = self._resolve_auto(lane=self.LANE)
        self.assertTrue(resolved.ok, resolved.reason)
        env = self._plan(".", target_repo=resolved.root)
        assert env.execution_root is not None
        lane = str(self.lane_worktree.resolve())
        self.assertEqual(env.execution_root.repo_root, lane)
        self.assertEqual(env.execution_root.workdir, lane)
        self.assertEqual(env.execution_root.relative, ".")

    def test_auto_and_an_explicit_target_repo_are_ab_equivalent(self) -> None:
        """Acceptance 3: the A/B correction j#94419 measured by hand, pinned."""
        auto_form = self._plan(".", target_repo=self._resolve_auto(lane=self.LANE).root)
        explicit_form = self._plan(".", target_repo=str(self.lane_worktree.resolve()))
        assert auto_form.execution_root is not None
        assert explicit_form.execution_root is not None
        self.assertEqual(
            auto_form.execution_root.to_dict(), explicit_form.execution_root.to_dict()
        )

    def test_a_nested_relative_workdir_stays_under_the_resolved_lane_root(self) -> None:
        env = self._plan("services/api", target_repo=self._resolve_auto(lane=self.LANE).root)
        assert env.execution_root is not None
        self.assertEqual(
            env.execution_root.workdir,
            str((self.lane_worktree / "services" / "api").resolve()),
        )
        self.assertEqual(env.execution_root.relative, "services/api")

    def test_the_resolved_root_survives_the_downstream_repo_identity_gate(self) -> None:
        """The composition that could have broken: the `target_repo_mismatch` gate.

        That gate compares ``Path(--target-repo).resolve()`` against
        ``resolve_workspace_root(target record cwd)``. Since ``auto`` now supplies BOTH sides
        from the resolved lane root, the equality must hold for a LINKED worktree — measured
        here rather than reasoned about, because an inequality would fail the correct send
        closed instead of delivering it.
        """
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (  # noqa: E501
            resolve_workspace_root,
        )

        root = self._resolve_auto(lane=self.LANE).root
        self.assertEqual(resolve_workspace_root(root), str(Path(root).resolve()))

    def test_the_senders_own_lane_still_resolves_to_this_checkout(self) -> None:
        """Non-regression (Acceptance 4): the #13331 same-lane dispatch keeps working."""
        resolved = self._resolve_auto(lane=self.SENDER_LANE)
        self.assertTrue(resolved.ok, resolved.reason)
        self.assertEqual(resolved.root, str(self.sender_worktree))

    def test_an_unowned_target_lane_fails_closed_with_no_sender_fallback(self) -> None:
        """Acceptance 5: unverifiable auto is typed-refused, never degraded to the sender."""
        resolved = self._resolve_auto(lane="issue_99999_never_declared")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.root, "")
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)
        self.assertNotIn(str(self.sender_worktree), resolved.detail)

    def test_a_detached_lane_worktree_still_resolves(self) -> None:
        """A detached lane binds by TOKEN, not by branch (review j#94499 finding 2).

        j#94487 asserted the opposite — that a detached worktree refuses — without ever
        measuring it, and the code accepted it all along. The contract is now the measured
        one and is pinned here: ``worktree_identity`` hashes the canonical path, so it
        proves which path is the lane's worktree regardless of what is checked out. Branch
        authority belongs to ``observe_lane_topology`` (push / HEAD topology), not to
        resolving an execution root.
        """
        self._git(
            "-C", str(self.lane_worktree), "checkout", "-q", "--detach", env=self._env
        )
        resolved = self._resolve_auto(lane=self.LANE)
        self.assertTrue(resolved.ok, f"{resolved.reason}: {resolved.detail}")
        self.assertEqual(resolved.root, str(self.lane_worktree.resolve()))
        # And it is still the LANE's root, never the sender's.
        self.assertNotEqual(resolved.root, str(self.sender_worktree.resolve()))

    def test_a_pruned_lane_worktree_fails_closed(self) -> None:
        """A bound lane whose worktree is gone resolves nothing (not the sender's root)."""
        import shutil

        shutil.rmtree(self.lane_worktree)
        self._git("-C", str(self.repo), "worktree", "prune")
        resolved = self._resolve_auto(lane=self.LANE)
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.root, "")
        self.assertEqual(resolved.reason, REFUSE_LANE_WORKTREE_UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
