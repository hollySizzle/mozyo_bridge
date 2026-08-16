"""A retire names the lane by its own root's kind, not by the caller's cwd (Redmine #14715).

The live defect (#14580 hibernated / released / live-zero lane, reproduced 2026-08-06 on the
#14996 lane): a lane whose recorded worktree path and actual worktree path were identical, on
a clean integrated checkout, still refused every retire path with ``worktree_binding_mismatch``.

Cause (code fact, j#100363). The five destructive retire surfaces derived the lane's identity
token family from ``resolved_worktree == repo_root`` — a #13392-era proxy for "this is a
non-git directory-scaffold lane, whose runtime root IS the workspace root". Run the ordinary
way from **inside a linked worktree** (``--repo`` omitted so it defaults to the cwd, and
``--worktree .``), those two paths coincide for a *git* worktree too, so the reader minted a
``dl_`` token for a root the create/adopt writers had bound to ``wt_`` — the token family
became a function of the operator's working directory. Passing the main checkout explicitly to
``--repo`` "worked" only because it broke the coincidence; it was never a design requirement.

The fix routes every surface — the declaration writers, the read/repair rails and the
destructive retire family — through one canonical derivation
(``declared_lane_root_identity``), which probes the KIND of the root being named. Writer and
reader cannot disagree by construction.

Everything here drives the real derivation and the real ``sublane retire`` command boundary
against real git checkouts, a real linked worktree, a real non-git scaffold root and real
lifecycle rows written through the public store. The herdr inventory and the pane close are
the only fakes: no live pane, process or route is touched, and every metadata-only intent is
wired so that reaching the pane-close actuator would fail the test outright.

Superseded contracts this file replaces (both removed at their own sites rather than left
asserting a rule that no longer holds):

- ``test_issue_13933_lane_identity_execution_root.PerSurfaceContractTests`` asserted that the
  destructive retire family RETAINS the collapse (design answer j#81046 Decision 4);
- ``test_issue_14478_linked_worktree_declaration_identity`` carried the same carve-out as a
  scope pin, and ``test_issue_13754_retire_zero_close_fence`` asserted the resulting block for
  a *correctly bound* lane on the theory that a false block is safe. It is not safe when it is
  permanent: the lane could never retire from its own worktree, which is the shape the
  self-heal runbook tells the operator to use.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_RETIRED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_herdr_projection as projection,
    sublane_herdr_retire as herdr_retire,
    sublane_lifecycle_command,
    # Import before the per-test retire actuator patch starts. The lifecycle command loads
    # this consumer lazily; importing it while the source function is mocked would leave its
    # module-level alias bound to a dead fixture closure after cleanup.
    sublane_quarantine as _quarantine_import_fence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402,E501
    declared_lane_root_identity,
    declared_worktree_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    ACTUATION_BLOCKED,
    ACTUATION_CLOSED,
    REASON_WORKTREE_BINDING_MISMATCH,
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    derive_directory_lane_token,
    derive_lane_workspace_token,
    encode_assigned_name,
)
from tests.support.current_launch_authority import (  # noqa: E402
    seed_completed_current_launch_authority,
)

_WORKSPACE_ID = "a14715c0d1e2f3a4"
_LANE = "issue_14715_retire_worktree_identity_r1"
_ISSUE = "14715"
_JOURNAL = "100364"

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


def _anchor(root: Path) -> None:
    """The real workspace anchor + herdr backend selection ``herdr_workspace_segment`` reads.

    Written for real rather than patched: the identity axis under test is exactly the join
    between the workspace this resolves and the worktree token the lane is bound to, and two
    different call sites resolve the segment (the retire actuation and the #14539 evidence
    target). A stub on one of them would leave the other reading the truth.
    """
    (root / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (root / ".mozyo-bridge" / "config.yaml").write_text(
        "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    (root / ".mozyo-bridge" / "workspace-anchor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": _WORKSPACE_ID,
                "canonical_session": "mzb-test",
                "project_name": "mozyo_bridge",
                "created_at": "2026-08-06T00:00:00+00:00",
                "updated_at": "2026-08-06T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _anchor(root)
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "base", cwd=root)


class _RealRootsFixture(unittest.TestCase):
    """A real main checkout, a real linked worktree on the lane branch, and a real non-git
    directory-scaffold root — rebuilt per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.primary = (root / "primary")
        _init_repo(self.primary)
        self.primary = self.primary.resolve()
        lane_wt = root / "lane_worktree"
        # No extra commit: the lane head is a literal ancestor of the integration branch, so
        # the retire's head-integration probe is green and cannot mask the identity axis.
        _git(
            "worktree", "add", "-q", "-b", _LANE, str(lane_wt), "main", cwd=self.primary
        )
        self.lane_wt = lane_wt.resolve()
        sibling = root / "sibling_worktree"
        _git(
            "worktree", "add", "-q", "-b", _LANE + "_sibling", str(sibling), "main",
            cwd=self.primary,
        )
        self.sibling = sibling.resolve()
        self.scaffold = root / "scaffold_root"
        self.scaffold.mkdir()
        # A directory-scaffold lane's runtime root IS the shared workspace root, so it carries
        # the anchor — it simply is not a git worktree (#13392 / LAUNCH_SKIP_NO_GIT).
        _anchor(self.scaffold)
        self.scaffold = self.scaffold.resolve()


class CanonicalLaneRootIdentityTests(_RealRootsFixture):
    """The derivation itself, over real roots and a real ``is_git_worktree_root`` probe."""

    def test_a_linked_worktree_is_named_by_the_git_worktree_family(self) -> None:
        identity = declared_lane_root_identity(self.lane_wt, _LANE)
        self.assertTrue(identity.git_worktree)
        self.assertEqual(
            identity.metadata_token, derive_lane_workspace_token(str(self.lane_wt))
        )

    def test_a_linked_worktree_never_resolves_the_directory_lane_family(self) -> None:
        # Stated separately so a failure names the defect rather than an inequality: this is
        # exactly the token the collapsed anchor used to mint for a git worktree.
        identity = declared_lane_root_identity(self.lane_wt, _LANE)
        self.assertNotEqual(
            identity.metadata_token,
            derive_directory_lane_token(str(self.lane_wt), _LANE),
        )

    def test_a_main_checkout_is_also_a_worktree_root(self) -> None:
        identity = declared_lane_root_identity(self.primary, _LANE)
        self.assertTrue(identity.git_worktree)
        self.assertEqual(
            identity.metadata_token, derive_lane_workspace_token(str(self.primary))
        )

    def test_a_non_git_scaffold_root_keeps_the_directory_lane_contract(self) -> None:
        # #13392 compatibility: unchanged by this issue.
        identity = declared_lane_root_identity(self.scaffold, _LANE)
        self.assertFalse(identity.git_worktree)
        self.assertEqual(
            identity.metadata_token,
            derive_directory_lane_token(str(self.scaffold), _LANE),
        )

    def test_a_non_git_scaffold_root_has_no_legacy_per_lane_workspace_twin(self) -> None:
        # The empty ``legacy_token`` is a CONSEQUENCE of the family decision, not a second
        # branch each caller re-derives: a non-git lane runs in the shared workspace root,
        # where a path-derived ``wt_`` segment names every lane on that root, i.e. no lane.
        self.assertEqual(declared_lane_root_identity(self.scaffold, _LANE).legacy_token, "")

    def test_a_git_root_is_its_own_legacy_per_lane_workspace_twin(self) -> None:
        identity = declared_lane_root_identity(self.lane_wt, _LANE)
        self.assertEqual(identity.legacy_token, identity.metadata_token)

    def test_two_lanes_on_one_scaffold_root_stay_distinct(self) -> None:
        self.assertNotEqual(
            declared_lane_root_identity(self.scaffold, "lane_a").metadata_token,
            declared_lane_root_identity(self.scaffold, "lane_b").metadata_token,
        )

    def test_two_lanes_on_one_git_worktree_share_its_path_token(self) -> None:
        # The mirror of the case above: a git worktree is named by its path alone, so the
        # lane label is deliberately NOT a discriminant there (route identity must not drift
        # on a lane rename).
        self.assertEqual(
            declared_lane_root_identity(self.lane_wt, "lane_a").metadata_token,
            declared_lane_root_identity(self.lane_wt, "lane_b").metadata_token,
        )

    def test_the_canonical_derivation_takes_no_caller_anchor(self) -> None:
        # The structural half of the fix: with no anchor in the signature, the retired
        # branch is not merely corrected, it is unrepresentable.
        import inspect

        self.assertEqual(
            list(inspect.signature(declared_lane_root_identity).parameters),
            ["resolved_root", "lane_label"],
        )

    def test_the_raw_path_helper_projects_the_same_decision(self) -> None:
        for root in (self.primary, self.lane_wt, self.scaffold):
            with self.subTest(root=root.name):
                self.assertEqual(
                    declared_worktree_identity(str(root), _LANE),
                    declared_lane_root_identity(root, _LANE).metadata_token,
                )


class _RetireCommandFixture(_RealRootsFixture):
    """The ``sublane retire`` command boundary over real roots and a fake herdr inventory."""

    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

        # The backend selection is the only stub on the identity path: everything else — the
        # workspace segment, the git probes, the lifecycle store — runs for real.
        backend = mock.patch.object(
            projection, "repo_backend_is_herdr", return_value=True
        )
        backend.start()
        self.addCleanup(backend.stop)

        self.rows: list[dict] = [
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "codex", _LANE),
                "pane_id": "w1:p3",
                "terminal_id": "terminal:w1:p3",
            },
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "claude", _LANE),
                "pane_id": "w1:p4",
                "terminal_id": "terminal:w1:p4",
            },
            # never a close target: the project's default-lane coordinator pair
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "codex", ""),
                "pane_id": "w1:p1",
                "terminal_id": "terminal:w1:p1",
            },
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "claude", ""),
                "pane_id": "w1:p2",
                "terminal_id": "terminal:w1:p2",
            },
        ]
        for role, row in zip(("codex", "claude"), self.rows[:2]):
            seed_completed_current_launch_authority(
                self.home,
                workspace_id=_WORKSPACE_ID,
                lane_id=_LANE,
                role=role,
                assigned_name=row["name"],
                locator=row["pane_id"],
                terminal_id=row["terminal_id"],
                target_workspace="w1",
                target_tab="w1:t1",
            )
        self._pristine_rows = list(self.rows)
        rows_patch = mock.patch.object(
            projection, "list_herdr_agent_rows",
            side_effect=lambda *_a, **_k: list(self.rows),
        )
        rows_patch.start()
        self.addCleanup(rows_patch.stop)

        self.closed_calls: list[tuple[str, str]] = []

        def _fake_close(plan, **_kwargs):
            closed = []
            for role, locator in plan.close_targets:
                self.closed_calls.append((role, locator))
                self.rows = [r for r in self.rows if r["pane_id"] != locator]
                closed.append((role, locator))
            return HerdrRetireCloseResult(
                workspace_id=plan.workspace_id,
                lane_id=plan.lane_id,
                closed=tuple(closed),
                failed=(),
                foreign_names=plan.foreign_names,
            )

        close_patch = mock.patch.object(
            herdr_retire, "execute_herdr_retire_close", side_effect=_fake_close
        )
        close_patch.start()
        self.addCleanup(close_patch.stop)

    def _declare_active(self, *, bound_to: Path, lane: str = _LANE) -> None:
        """The lane's owner binding + canonical worktree binding, as ``sublane create``
        writes it — derived through the SAME canonical helper the writers use."""
        LaneLifecycleStore().declare_active(
            LaneLifecycleKey(_WORKSPACE_ID, lane),
            decision=DecisionPointer(
                source="redmine", issue_id=_ISSUE, journal_id=_JOURNAL
            ),
            issue_id=_ISSUE,
            worktree_identity=declared_lane_root_identity(bound_to, lane).metadata_token,
        )

    def _disposition(self, lane: str = _LANE) -> str:
        record = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, lane))
        return "" if record is None else record.lane_disposition

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            repo=str(self.primary),
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_wt),
            branch=_LANE,
            integration_branch="main",
            json=True,
            # every durable-record invariant asserted: the PREFLIGHT is green, so any block
            # can only come from the actuation.
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
            integration_journal=None,
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=False,
            retire_active_live_zero=False,
            retire_active_unbound_live_zero=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, **overrides):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(self._args(**overrides))
        return code, json.loads(buffer.getvalue())


class RetireFromTheLaneWorktreeTests(_RetireCommandFixture):
    """The headline regression: the ordinary in-worktree invocation now retires the lane."""

    def test_lane_worktree_as_both_repo_and_worktree_retires_the_lane(self) -> None:
        # The exact live shape: ``--repo`` omitted (so it defaults to the cwd, which IS the
        # lane worktree) and ``--worktree .``. Pre-fix this dead-ended on
        # ``worktree_binding_mismatch`` for a lane whose binding was perfectly correct.
        self._declare_active(bound_to=self.lane_wt)
        code, payload = self._run(repo=str(self.lane_wt), execute=True)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_CLOSED)
        self.assertEqual(
            sorted(c["locator"] for c in close["closed"]), ["w1:p3", "w1:p4"]
        )
        self.assertEqual(close["durable_retirement"], "recorded")
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)
        # the coordinator's default-lane pair is never a target
        self.assertEqual(sorted(r["pane_id"] for r in self.rows), ["w1:p1", "w1:p2"])

    def test_the_main_checkout_anchor_still_retires_the_lane(self) -> None:
        # The pre-fix workaround keeps working: explicit ``--repo`` precedence is preserved.
        self._declare_active(bound_to=self.lane_wt)
        code, payload = self._run(repo=str(self.primary), execute=True)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["herdr_retire_close"]["state"], ACTUATION_CLOSED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_a_sibling_worktree_is_still_refused_from_the_lane_worktree_anchor(self) -> None:
        # The fence #13754 owns must survive the fix on the NEW anchor: a clean sibling
        # worktree of the same repo does not license this lane's close.
        self._declare_active(bound_to=self.lane_wt)
        code, payload = self._run(
            repo=str(self.sibling), worktree=str(self.sibling), execute=True
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_BLOCKED)
        self.assertEqual(close["reason"], REASON_WORKTREE_BINDING_MISMATCH)
        self.assertEqual(self.closed_calls, [])
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_a_dirty_lane_worktree_is_still_refused_from_its_own_anchor(self) -> None:
        self._declare_active(bound_to=self.lane_wt)
        (self.lane_wt / "uncommitted.txt").write_text("wip\n", encoding="utf-8")
        code, payload = self._run(repo=str(self.lane_wt), execute=True)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertIn("dirty_worktree", payload["decision"]["blocked_reasons"])
        # the actuation never runs, so no close is even planned
        self.assertNotIn("herdr_retire_close", payload)
        self.assertEqual(self.closed_calls, [])
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_an_unintegrated_head_is_still_refused_from_its_own_anchor(self) -> None:
        # Driven on the terminal intent that OWNS the head-integration axis (the guarded close
        # does not have one). An unmerged lane head must not terminalize just because the
        # command now resolves the lane's identity from inside its own worktree.
        self._declare_active(bound_to=self.lane_wt)
        (self.lane_wt / "extra.txt").write_text("y\n", encoding="utf-8")
        _git("add", "-A", cwd=self.lane_wt)
        _git("commit", "-qm", "lane work", cwd=self.lane_wt)
        self.rows = []  # the live-zero precondition this intent requires
        code, payload = self._run(
            repo=str(self.lane_wt), retire_active_live_zero=True
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(
            payload["active_live_zero_retire"]["reason"], "head_not_integrated"
        )
        self.assertEqual(self.closed_calls, [])
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_a_row_bound_to_a_different_root_is_still_refused(self) -> None:
        # Identity is not satisfied by standing in the right place: a lane whose recorded
        # binding names the primary checkout is not closed over from its worktree.
        self._declare_active(bound_to=self.primary)
        code, payload = self._run(repo=str(self.lane_wt), execute=True)
        self.assertEqual(code, 1)
        self.assertEqual(
            payload["herdr_retire_close"]["reason"], REASON_WORKTREE_BINDING_MISMATCH
        )
        self.assertEqual(self.closed_calls, [])

    def test_a_pre_fix_directory_lane_binding_on_a_git_root_is_refused(self) -> None:
        # The deliberate consequence, asserted rather than discovered: a row mis-bound to the
        # ``dl_`` family by a pre-#14478 writer names a family this git root does not have, so
        # it fails closed from EVERY anchor. #14478 j#88645 F1 already ruled that such rows
        # have no in-place rebind rail; what changes is that the mismatch no longer disappears
        # when the operator happens to stand inside the worktree.
        LaneLifecycleStore().declare_active(
            LaneLifecycleKey(_WORKSPACE_ID, _LANE),
            decision=DecisionPointer(
                source="redmine", issue_id=_ISSUE, journal_id=_JOURNAL
            ),
            issue_id=_ISSUE,
            worktree_identity=derive_directory_lane_token(str(self.lane_wt), _LANE),
        )
        for anchor in (self.lane_wt, self.primary):
            with self.subTest(anchor=anchor.name):
                code, payload = self._run(repo=str(anchor), execute=True)
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["herdr_retire_close"]["reason"],
                    REASON_WORKTREE_BINDING_MISMATCH,
                )
        self.assertEqual(self.closed_calls, [])

    def test_a_non_git_directory_scaffold_lane_still_retires(self) -> None:
        # #13392 compatibility, end to end: a lane whose runtime root is a real non-git
        # directory is bound to — and retired on — the ``dl_`` family exactly as before.
        self._declare_active(bound_to=self.scaffold)
        record = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, _LANE))
        self.assertEqual(
            record.worktree_identity,
            derive_directory_lane_token(str(self.scaffold), _LANE),
        )
        code, payload = self._run(
            repo=str(self.scaffold), worktree=str(self.scaffold), execute=True
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["herdr_retire_close"]["state"], ACTUATION_CLOSED)
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)


class PerRetireSurfaceAnchorInvarianceTests(_RetireCommandFixture):
    """All five retire surfaces answer the same for one lane, from either anchor.

    The bug was never specific to one intent: each surface carried its own copy of the proxy,
    so each one's verdict moved with the operator's cwd. These drive the real command boundary
    for every intent and require the typed verdict to be identical from the lane worktree and
    from the main checkout. The negative control below keeps the assertion non-vacuous by
    showing the identity axis is genuinely evaluated on these paths.
    """

    _INTENTS = (
        "execute",
        "retire_active_live_zero",
        "retire_hibernated_bound",
        "migrate_hibernated_legacy",
        "reconcile_hibernated_live",
    )

    def _verdict(self, intent: str, *, anchor: Path) -> tuple[int, str, str]:
        code, payload = self._run(repo=str(anchor), **{intent: True})
        for section in (
            "herdr_retire_close",
            "active_live_zero_retire",
            "hibernated_bound_retire",
            "hibernated_legacy_migration",
            "hibernated_live_reconcile",
        ):
            block = payload.get(section)
            if isinstance(block, dict):
                return code, block.get("state", ""), block.get("reason", "")
        return code, "", ""

    def _reset(self, *, bound_to: Path) -> None:
        """Rebuild BOTH mutable states between the two anchors.

        The lifecycle row and the live inventory are the two things a successful intent
        mutates, so replaying only one of them would compare a first run against a second
        run — an ordering artefact, not an anchor difference.
        """
        LaneLifecycleStore().path.unlink(missing_ok=True)
        self.rows = list(self._pristine_rows)
        self.closed_calls.clear()
        self._declare_active(bound_to=bound_to)

    def test_every_intent_answers_the_same_from_either_anchor(self) -> None:
        for intent in self._INTENTS:
            with self.subTest(intent=intent):
                self._reset(bound_to=self.lane_wt)
                from_worktree = self._verdict(intent, anchor=self.lane_wt)
                self._reset(bound_to=self.lane_wt)
                from_primary = self._verdict(intent, anchor=self.primary)
                self.assertEqual(from_worktree, from_primary)

    def test_the_identity_axis_is_actually_evaluated_on_the_attesting_intents(self) -> None:
        # Non-vacuity: for the three intents that attest the recorded binding, a row bound to
        # a different root must produce the mismatch — from BOTH anchors. Without this, the
        # invariance above could be satisfied by an intent that never looks at identity.
        for intent in ("execute", "retire_active_live_zero", "retire_hibernated_bound"):
            for anchor in (self.lane_wt, self.primary):
                with self.subTest(intent=intent, anchor=anchor.name):
                    self._reset(bound_to=self.sibling)
                    _code, _state, reason = self._verdict(intent, anchor=anchor)
                    self.assertEqual(reason, REASON_WORKTREE_BINDING_MISMATCH)
                    self.assertEqual(self.closed_calls, [])


class SingleDerivationContractTests(unittest.TestCase):
    """One derivation, structurally — not five corrected copies of one rule.

    The issue's expectation is that create / adopt / retire share a canonical helper rather
    than each re-deciding the family. A corrected copy is still a copy: it drifts the moment
    one surface is edited alone, which is exactly how the retire family fell behind the
    writers between #13933 and #14715.
    """

    def _source(self, name: str) -> str:
        return (_APP / name).read_text()

    _SURFACES = (
        # writers
        "sublane_actuator_herdr_ops.py",
        # destructive retire family
        "sublane_retire_actuation.py",
        "sublane_active_live_zero_retire.py",
        "sublane_hibernated_bound_retire.py",
        "sublane_hibernated_legacy_retire.py",
        "sublane_hibernated_live_reconcile.py",
        # read / repair rails
        "sublane_hibernated_pin_repair.py",
        "sublane_hibernated_bound_pair_convergence_live.py",
        "recovered_pair_pin_reconciliation_live.py",
    )

    def test_no_surface_keys_the_family_on_the_caller_anchor(self) -> None:
        # Matched on the code expression, not the name: several of these modules quote the
        # retired proxy in their explanatory comments on purpose.
        for module in self._SURFACES:
            self.assertNotIn("repo_root.expanduser()", self._source(module), module)

    def test_no_surface_chooses_a_token_family_of_its_own(self) -> None:
        # ``derive_directory_lane_token`` is the ``dl_`` half of the choice, so a caller that
        # names it is deciding the family itself. Its only legitimate caller is the canonical
        # domain helper — which is not in this package.
        for module in self._SURFACES:
            self.assertNotIn(
                "derive_directory_lane_token(", self._source(module), module
            )

    def test_every_surface_routes_through_the_canonical_derivation(self) -> None:
        for module in self._SURFACES:
            source = self._source(module)
            self.assertTrue(
                "declared_lane_root_identity" in source
                or "declared_worktree_identity" in source,
                module,
            )

    def test_the_canonical_helper_is_the_only_family_decision_in_the_package(self) -> None:
        # Package-wide rather than per-surface, so a NEW module cannot quietly add a sixth
        # copy of the branch and pass the list above by not being on it.
        offenders = sorted(
            path.name
            for path in _APP.glob("*.py")
            if "derive_directory_lane_token(" in path.read_text()
        )
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
