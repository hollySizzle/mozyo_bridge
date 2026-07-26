"""A linked worktree's declared identity is its own, not the caller's (Redmine #14478).

The live defect (#14462 j#88632, installed ``0.14.0a2``): the active-declaration self-heal
runbook tells the operator to run ``sublane create --execute --no-dispatch`` **from the lane's
own worktree**, i.e. with ``--repo`` pointing at that worktree. Both declaration writers chose
the lane identity token family with ``resolved == repo_root``, so that anchor collapsed the
proxy and the lifecycle ``worktree_identity`` of a **linked git worktree** was backfilled with
a ``dl_`` (directory-lane) token. ``sublane recover-gateway`` derives its action-time authority
token from the same root and always gets ``wt_``, so the very next preflight refused the launch
with ``worktree_identity_mismatch`` — close/launch/send 0, lane stuck.

Redmine #13933 j#81046 Decision 1 already fixed this class for the read/repair rails: the token
family is a fact about the KIND of the root being named, probed on that root. These tests carry
the same rule into the create/adopt **declaration writers**, and pin the join the live failure
broke — writer token == recovery reader token, from either repo anchor.

Everything here drives the real derivation against real git worktrees, a real
``is_git_worktree_root`` probe and a real isolated-home lifecycle store. No token literal is
written anywhere — the negative controls compute the pre-fix ``dl_`` binding from the real
``derive_directory_lane_token`` and seed it through the public declaration store, so a
mis-bound row is reproduced rather than imagined. No live pane / process / route is touched.

Scope pins carried deliberately:

- the ``dl_`` contract for non-git directory-scaffold lanes is preserved (#13392);
- a non-empty **divergent** binding is still never overwritten (#14478 requirement 4 — no
  general relaxation of ``declare_lane`` / ``backfill_active_binding``);
- the destructive retire family keeps its #13754 collapse; that is a separate per-surface
  decision (``managed-state-model.md`` #13933 R7 / design answer j#81046 Decision 4) and is
  guarded by ``test_issue_13933_lane_identity_execution_root.PerSurfaceContractTests``.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.lane_checkout_authority import (  # noqa: E402
    worktree_binding_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E402
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402
    ADOPT_DECL_BACKFILLED,
    ADOPT_DECL_DECLARED,
    ADOPT_DECL_OWNER_CONFLICT,
    declare_adopted_owner_row,
    declared_worktree_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E402
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    derive_directory_lane_token,
    derive_lane_workspace_token,
    encode_assigned_name,
)

WORKSPACE_ID = "c14478a0b1c2d3e4"
LANE = "issue_14478_linked_worktree_identity_r1"
ISSUE = "14478"
JOURNAL = "88636"

_APP = (
    REPO
    / "src/mozyo_bridge/e_110_execution_platform"
    / "f_140_delegated_coordinator_nested_handoff/application"
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(root: Path) -> None:
    """A real anchored git checkout with the herdr backend selected."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (root / ".mozyo-bridge" / "config.yaml").write_text(
        "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    (root / ".mozyo-bridge" / "workspace-anchor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": WORKSPACE_ID,
                "canonical_session": "mzb-test",
                "project_name": "mozyo_bridge",
                "created_at": "2026-07-15T00:00:00+00:00",
                "updated_at": "2026-07-15T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "base", cwd=root)


class _RealWorktreeFixture(unittest.TestCase):
    """A real anchored main checkout, a real linked worktree on the lane branch, and a real
    non-git directory-scaffold root — rebuilt per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.primary = root / "primary"
        _init_repo(self.primary)
        self.primary = self.primary.resolve()
        lane_wt = root / "lane_worktree"
        _git("worktree", "add", "-b", LANE, str(lane_wt), "main", cwd=self.primary)
        self.lane_wt = lane_wt.resolve()
        self.scaffold = root / "scaffold_root"
        self.scaffold.mkdir()
        self.scaffold = self.scaffold.resolve()

    # -- the create-path writer, driven for real (no herdr launch needed) --------------

    def _record(self, *, repo_root: Path, worktree: Path, lane: str = LANE):
        """Drive ``_record_lane_metadata`` -> ``_declare_lane_lifecycle`` and return the row."""
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ):
            ops = HerdrSublaneActuatorOps(
                repo_root=repo_root,
                lane_label=lane,
                issue=ISSUE,
                branch=lane,
                journal=JOURNAL,
            )
            ops._record_lane_metadata(str(worktree))
            return LaneLifecycleStore().get(LaneLifecycleKey(WORKSPACE_ID, lane))

    def _row(self, lane: str = LANE):
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ):
            return LaneLifecycleStore().get(LaneLifecycleKey(WORKSPACE_ID, lane))


class LinkedWorktreeIdentityIsAnchorInvariantTests(_RealWorktreeFixture):
    """The regression itself: a linked worktree is named ``wt_`` from ANY repo anchor."""

    def test_lane_worktree_anchor_declares_the_canonical_workspace_token(self) -> None:
        # The exact live invocation: --repo IS the lane worktree. Pre-fix this wrote the
        # ``dl_`` token (#14462 j#88632).
        record = self._record(repo_root=self.lane_wt, worktree=self.lane_wt)
        self.assertIsNotNone(record)
        self.assertEqual(
            record.worktree_identity, derive_lane_workspace_token(str(self.lane_wt))
        )

    def test_lane_worktree_anchor_does_not_mint_a_directory_lane_token(self) -> None:
        # Stated as its own assertion so the failure names the defect, not just an inequality:
        # a linked git worktree must never carry the non-git family's token.
        record = self._record(repo_root=self.lane_wt, worktree=self.lane_wt)
        self.assertNotEqual(
            record.worktree_identity,
            derive_directory_lane_token(str(self.lane_wt), LANE),
        )

    def test_primary_checkout_anchor_declares_the_same_token(self) -> None:
        record = self._record(repo_root=self.primary, worktree=self.lane_wt)
        self.assertEqual(
            record.worktree_identity, derive_lane_workspace_token(str(self.lane_wt))
        )

    def test_the_declared_binding_does_not_move_with_the_repo_anchor(self) -> None:
        # Same lane, same worktree, two execution roots -> one identity. This is the property
        # the ``resolved == repo_root`` proxy could not hold.
        from_lane_root = self._record(repo_root=self.lane_wt, worktree=self.lane_wt)
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ):
            LaneLifecycleStore().path.unlink(missing_ok=True)
        from_primary = self._record(repo_root=self.primary, worktree=self.lane_wt)
        self.assertEqual(from_lane_root.worktree_identity, from_primary.worktree_identity)

    def test_a_main_checkout_used_as_its_own_lane_root_is_also_a_worktree(self) -> None:
        # ``is_git_worktree_root`` is true for the main checkout too, so anchoring a lane on
        # the primary checkout names it ``wt_`` — the root's kind decides, not its role.
        self.assertEqual(
            declared_worktree_identity(str(self.primary), LANE),
            derive_lane_workspace_token(str(self.primary)),
        )


class RecoveryAuthorityJoinTests(_RealWorktreeFixture):
    """The join the live failure broke: the writer's token IS the recovery reader's token.

    There are two live launch-authority evaluators, and BOTH are exercised here because the
    live blocker was reported by the first one:

    - ``LiveStaleWorkerRecoveryOps.lane_authority_reason`` backs ``sublane recover-gateway`` /
      ``recover-stale`` (the surface that reported ``worktree_identity_mismatch`` in #14462
      j#88632);
    - ``lane_checkout_authority.worktree_binding_reason`` backs ``sublane recover-pair``.

    Neither is changed by #14478: both already derive the canonical ``wt_`` token from the
    recovery root, for any anchor. What these tests pin is that the declaration writer now
    lands on the same side of that join — and, via the negative control below, that the join
    still discriminates.
    """

    def _gateway_recovery_reason(self, record) -> str:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E501
            RecoveryRequest,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E501
            LiveStaleWorkerRecoveryOps,
        )

        request = RecoveryRequest(
            issue=ISSUE,
            lane=LANE,
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(WORKSPACE_ID, "codex", LANE),
            locator="w14478:pG",
            lane_revision=str(record.revision),
            lane_generation=str(record.lane_generation),
        )
        ops = LiveStaleWorkerRecoveryOps(
            repo_root=self.lane_wt, request=request, lifecycle_home=self.home
        )
        return ops.lane_authority_reason(request)

    def test_recover_gateway_authority_accepts_the_binding_declared_from_the_lane_worktree(
        self,
    ) -> None:
        # The exact live blocker: this returned ``worktree_identity_mismatch`` for a lane whose
        # own self-heal had just written its binding (#14462 j#88632).
        record = self._record(repo_root=self.lane_wt, worktree=self.lane_wt)
        self.assertEqual(self._gateway_recovery_reason(record), LAUNCH_AUTHORITY_OK)

    def test_recover_gateway_authority_still_refuses_a_directory_lane_token(self) -> None:
        # Negative control on the same evaluator, seeded through the PUBLIC declaration store
        # with the token the pre-fix writer produced (no raw DB edit). It must still be
        # refused, and refused on the identity axis specifically: #14478 fixes the producer, it
        # does not loosen the fence (requirement 4). Without this the green above could come
        # from an evaluator that had simply stopped discriminating.
        applied = LaneDeclarationStore(home=self.home).declare_lane(
            LaneLifecycleKey(WORKSPACE_ID, LANE),
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=JOURNAL
            ),
            issue_id=ISSUE,
            worktree_identity=derive_directory_lane_token(str(self.lane_wt), LANE),
        )
        self.assertTrue(applied.applied)
        stale = self._row()
        self.assertEqual(
            stale.worktree_identity,
            derive_directory_lane_token(str(self.lane_wt), LANE),
        )
        self.assertEqual(
            self._gateway_recovery_reason(stale), LAUNCH_AUTHORITY_WORKTREE_MISMATCH
        )

    def test_binding_declared_from_the_lane_worktree_satisfies_the_launch_authority(
        self,
    ) -> None:
        record = self._record(repo_root=self.lane_wt, worktree=self.lane_wt)
        self.assertEqual(
            worktree_binding_reason(self.lane_wt, lane=LANE, record=record),
            LAUNCH_AUTHORITY_OK,
        )

    def test_binding_declared_from_the_primary_checkout_also_satisfies_it(self) -> None:
        record = self._record(repo_root=self.primary, worktree=self.lane_wt)
        self.assertEqual(
            worktree_binding_reason(self.lane_wt, lane=LANE, record=record),
            LAUNCH_AUTHORITY_OK,
        )

    def test_a_directory_lane_token_on_a_linked_worktree_still_reads_as_mismatch(
        self,
    ) -> None:
        # The same negative control against the recover-pair evaluator: a record carrying the
        # pre-fix binding (the ``dl_`` token from the real derivation, not a literal) must
        # still be refused on the identity axis. Without it, the two greens above could come
        # from a reader that had simply stopped discriminating.
        class _Row:
            worktree_identity = derive_directory_lane_token(str(self.lane_wt), LANE)

        self.assertEqual(
            worktree_binding_reason(self.lane_wt, lane=LANE, record=_Row()),
            LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
        )


class DirectoryScaffoldLaneContractTests(_RealWorktreeFixture):
    """#13392 must not regress: a non-git lane keeps its lane-scoped ``dl_`` key."""

    def test_non_git_root_keeps_the_directory_lane_token(self) -> None:
        self.assertEqual(
            declared_worktree_identity(str(self.scaffold), LANE),
            derive_directory_lane_token(str(self.scaffold), LANE),
        )

    def test_non_git_root_keeps_it_from_an_unrelated_repo_anchor_too(self) -> None:
        # The inverse of the live defect, and the other half of #13933 j#81046 Decision 1: the
        # old proxy read ``resolved != repo_root`` here and minted a path-only ``wt_`` token
        # that collides across every lane on the shared root.
        record = self._record(repo_root=self.primary, worktree=self.scaffold)
        self.assertEqual(
            record.worktree_identity,
            derive_directory_lane_token(str(self.scaffold), LANE),
        )

    def test_two_non_git_lanes_on_one_root_stay_distinct(self) -> None:
        self.assertNotEqual(
            declared_worktree_identity(str(self.scaffold), "lane_a"),
            declared_worktree_identity(str(self.scaffold), "lane_b"),
        )

    def test_a_git_worktree_lane_is_not_re_keyed_by_its_lane_label(self) -> None:
        # A worktree is named by its path alone, so the ``dl_`` lane discriminant must not
        # leak into the ``wt_`` family (a lane rename must not move a git lane's identity).
        self.assertEqual(
            declared_worktree_identity(str(self.lane_wt), "lane_a"),
            declared_worktree_identity(str(self.lane_wt), "lane_b"),
        )


class DivergentBindingIsStillNeverOverwrittenTests(_RealWorktreeFixture):
    """Requirement 4: the fix is in the producer, not in a relaxed write gate.

    The adopt here passes a **fully live + attested** pair, so it reaches ``declare_lane`` and
    then ``backfill_active_binding`` for real — a pair that failed the liveness gate would have
    proved nothing about the divergent-binding refusal.
    """

    GW_LOC = "w14478:pG"
    WK_LOC = "w14478:pW"
    ATTESTED_AT = "2026-07-26T00:00:00+00:00"

    def _live_pair_rows(self) -> list:
        return [
            {"name": encode_assigned_name(WORKSPACE_ID, provider, LANE), "pane_id": loc}
            for provider, loc in (("codex", self.GW_LOC), ("claude", self.WK_LOC))
        ]

    def _attest_pair(self) -> None:
        store = HerdrIdentityAttestationStore(home=self.home)
        for provider, locator in (("codex", self.GW_LOC), ("claude", self.WK_LOC)):
            store.upsert(
                IdentityAttestationRecord(
                    assigned_name=encode_assigned_name(WORKSPACE_ID, provider, LANE),
                    workspace_id=WORKSPACE_ID,
                    role=provider,
                    lane_id=LANE,
                    locator=locator,
                    verdict=VERDICT_PRESENT,
                    observed_at=self.ATTESTED_AT,
                )
            )

    def _adopt(self, worktree: Path) -> str:
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ):
            return declare_adopted_owner_row(
                journal=JOURNAL,
                issue=ISSUE,
                lane_label=LANE,
                worktree_path=str(worktree),
                workspace_id=WORKSPACE_ID,
                lane_id=LANE,
                providers=("codex", "claude"),
                rows=self._live_pair_rows(),
                attestation_home=self.home,
            )

    def test_an_attested_adopt_on_the_bound_worktree_is_owner_bound(self) -> None:
        # The control: the same live+attested pair on the lane's OWN bound worktree does
        # declare. Without it, the refusal below could be an artifact of the gate, not of the
        # binding divergence.
        self._attest_pair()
        self.assertIn(
            self._adopt(self.lane_wt), (ADOPT_DECL_DECLARED, ADOPT_DECL_BACKFILLED)
        )
        self.assertEqual(
            self._row().worktree_identity, derive_lane_workspace_token(str(self.lane_wt))
        )

    def test_a_row_bound_to_another_worktree_is_not_rebound_by_an_adopt(self) -> None:
        sibling = Path(self._tmp.name).resolve() / "sibling_worktree"
        _git("worktree", "add", "-b", "sibling", str(sibling), "main", cwd=self.primary)
        sibling = sibling.resolve()
        # The lane's row is bound to the SIBLING worktree; the adopt runs in the lane worktree
        # with an otherwise perfect live + attested pair.
        record = self._record(repo_root=self.primary, worktree=sibling)
        bound = record.worktree_identity
        self.assertEqual(bound, derive_lane_workspace_token(str(sibling)))
        self._attest_pair()

        outcome = self._adopt(self.lane_wt)

        self.assertEqual(outcome, ADOPT_DECL_OWNER_CONFLICT)
        self.assertEqual(self._row().worktree_identity, bound)  # untouched


class DeclarationWriterSurfaceContractTests(unittest.TestCase):
    """The anchor is gone from the writers structurally, not just by convention.

    A corrected expression that still *accepts* the caller's anchor invites the next author to
    reintroduce the branch. These pin the shape.
    """

    def _source(self, name: str) -> str:
        return (_APP / name).read_text()

    def test_the_declaration_identity_helper_takes_no_repo_anchor(self) -> None:
        params = inspect.signature(declared_worktree_identity).parameters
        self.assertNotIn("repo_root", params)
        self.assertEqual(list(params), ["worktree_path", "lane_label"])

    def test_the_adopt_declaration_entry_point_takes_no_repo_anchor(self) -> None:
        self.assertNotIn(
            "repo_root", inspect.signature(declare_adopted_owner_row).parameters
        )

    def test_both_declaration_writers_share_one_derivation(self) -> None:
        # The create path routes through the same helper the adopt path does, so there is one
        # place where the token family is chosen (no per-function copy of the branch).
        source = self._source("sublane_actuator_herdr_ops.py")
        self.assertIn("declared_worktree_identity", source)
        # The anchor comparison itself is gone (the prose above still names it, so this
        # matches the code expression, not the explanation), and so is any local re-derivation
        # of either token family — the create path can no longer choose one on its own.
        self.assertNotIn("repo_root.expanduser()", source)
        self.assertNotIn("derive_directory_lane_token(", source)
        self.assertNotIn("derive_lane_workspace_token(", source)

    def test_the_shared_derivation_probes_the_root_kind(self) -> None:
        self.assertIn(
            "is_git_worktree_root", self._source("sublane_adopt_declaration.py")
        )

    def test_the_destructive_retire_family_is_untouched_by_this_issue(self) -> None:
        # ``managed-state-model.md`` (#13933 R7 / design answer j#81046 Decision 4): the retire
        # rails keep the #13754 collapse as a DELIBERATE fail-closed guard, and switching them
        # is a separate per-surface decision. #14478 changes producers only; if a later edit
        # quietly repoints a retire rail here, this fails alongside the #13933 contract test.
        for module in (
            "sublane_retire_actuation.py",
            "sublane_hibernated_bound_retire.py",
            "sublane_hibernated_legacy_retire.py",
            "sublane_hibernated_live_reconcile.py",
        ):
            source = self._source(module)
            self.assertIn("resolved_worktree == repo_root", source, module)
            self.assertNotIn("is_git_worktree_root", source, module)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
