"""Regression pins for the #13754 retire zero-close / identity fence.

Redmine #13754 (parent #12499), reported from #13748 j#77473–j#77475. Running

    sublane retire --repo <integration worktree> --issue ... --lane-label ... --execute

reported ``decision.state: retire_ok``, ``herdr_retire_close.workspace_id: ""``,
``closed: []``, ``failed: []`` and **exit 0** — while the lane's managed pair was still
live. The caller's mis-aimed target root is operator error, but the command turned it
into a *success*: a coordinator reading that JSON believes the pair is retired.

Two defects produced that shape, and each is pinned here:

1. **the actuation had no verdict.** Every way the lane could fail to resolve — no
   ``--worktree`` anchor, a root carrying no workspace anchor, an unreadable live
   inventory, an unresolved / unlaunchable provider binding — folded into an empty
   ``HerdrRetireCloseResult`` indistinguishable from a genuine "already retired"; and
2. **the exit code came from the preflight alone.** ``may_retire`` says the retire was
   *permitted*, never that it *happened*, so the actuation could close nothing (or fail
   every close) and still exit 0.

The fence limits ``retire_ok`` to a real close or a **verified** idempotent no-op — the
durable lifecycle records the lane ``retired`` AND zero expected managed slots are live —
and blocks everything else. The root combinations named in the ticket (integration
worktree / primary repo / explicit issue worktree) are pinned against the REAL identity
resolver, since the mis-aimed root is what the fence exists to catch.

Boundary (unchanged, re-pinned): the retire never removes a worktree, deletes a branch,
or closes a foreign / another lane's agent.
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

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_RETIRED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_herdr_projection,
    sublane_herdr_retire,
    sublane_lifecycle_command,
    sublane_retire_actuation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E402,E501
    attest_retire_target,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    ACTUATION_BLOCKED,
    ACTUATION_CLOSED,
    ACTUATION_VERIFIED_NOOP,
    REASON_CLOSE_FAILED,
    REASON_INVENTORY_UNREADABLE,
    REASON_ISSUE_LANE_MISMATCH,
    REASON_LANE_OWNER_UNVERIFIED,
    REASON_LIFECYCLE_UNREADABLE,
    REASON_NO_WORKTREE_ANCHOR,
    REASON_WORKSPACE_UNRESOLVED,
    REASON_WORKTREE_BINDING_MISMATCH,
    REASON_WORKTREE_BINDING_UNVERIFIED,
    REASON_ZERO_CLOSE_UNPROVEN,
    HerdrRetireClosePlan,
    HerdrRetireCloseResult,
    decide_retire_actuation,
    expected_live_slots,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E402,E501
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E402,E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    derive_lane_workspace_token,
    encode_assigned_name,
)

_WORKSPACE_ID = "e1487dcb1f2d4412"
_LANE = "issue_13754_retire_zero_close_fence"
_ISSUE = "13754"
_JOURNAL = "77985"


def _row(ws: str, role: str, lane: str, locator: str) -> dict:
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dirty(worktree: Path) -> None:
    """Make ``worktree`` a dirty git checkout (an untracked file), so the retire's
    target-worktree dirty probe (LiveSublaneGitOperations.worktree_dirty) reads dirty."""
    (worktree / "uncommitted.txt").write_text("wip\n", encoding="utf-8")


def _init_repo(root: Path, *, anchor: bool) -> None:
    """A real git checkout with the herdr backend selected, optionally anchored.

    The anchor is the workspace identity ``herdr_workspace_segment`` resolves. An
    *integration* worktree carries none (#13754 / #13748 j#77473: "integration worktree
    には workspace anchor が無く missing_identity") — that is precisely the root whose
    zero-close used to pass.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (root / ".mozyo-bridge" / "config.yaml").write_text(
        "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    if anchor:
        (root / ".mozyo-bridge" / "workspace-anchor.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": _WORKSPACE_ID,
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


class RootResolutionPins(unittest.TestCase):
    """The ticket's root combinations, against the REAL identity resolver.

    The fence's whole job is to tell a correctly-aimed root from a mis-aimed one, so
    these pin the resolution itself rather than a stub of it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.primary = tmp / "primary"
        _init_repo(self.primary, anchor=True)
        # The lane's own checkout: a linked git worktree of the primary, which INHERITS
        # the project workspace identity (#13377 / #13152).
        self.lane_worktree = tmp / "lane_wt"
        _git(
            "worktree", "add", "-b", _LANE, str(self.lane_worktree), "main",
            cwd=self.primary,
        )
        # The integration worktree of the bug report: a real checkout that carries NO
        # workspace anchor and is not linked to the primary.
        self.integration = tmp / "integration"
        _init_repo(self.integration, anchor=False)
        self.addCleanup(self._tmp.cleanup)

    def test_primary_repo_resolves_the_project_workspace(self) -> None:
        self.assertEqual(herdr_workspace_segment(self.primary), _WORKSPACE_ID)

    def test_explicit_issue_worktree_inherits_the_project_workspace(self) -> None:
        # The sanctioned root: the lane's linked worktree resolves the SAME project
        # workspace, so the managed target is identifiable and the close can proceed.
        self.assertEqual(herdr_workspace_segment(self.lane_worktree), _WORKSPACE_ID)

    def test_integration_worktree_resolves_no_workspace(self) -> None:
        # The #13748 j#77473 root: identity is unresolvable. Before #13754 this empty
        # segment silently produced `closed: []` + exit 0.
        self.assertEqual(herdr_workspace_segment(self.integration), "")


class ActuationVerdictPins(unittest.TestCase):
    """The pure fail-closed verdict (``decide_retire_actuation``)."""

    def _plan(self, targets=()) -> HerdrRetireClosePlan:
        return HerdrRetireClosePlan(
            workspace_id=_WORKSPACE_ID, lane_id=_LANE, close_targets=tuple(targets)
        )

    def _result(self, *, closed=(), failed=()) -> HerdrRetireCloseResult:
        return HerdrRetireCloseResult(
            workspace_id=_WORKSPACE_ID,
            lane_id=_LANE,
            closed=tuple(closed),
            failed=tuple(failed),
        )

    def test_real_close_is_the_only_unconditional_success(self) -> None:
        verdict = decide_retire_actuation(
            self._plan([("codex", "w1:p1")]),
            self._result(closed=[("codex", "w1:p1"), ("claude", "w1:p2")]),
            expected_live=(),
            already_retired=False,
        )
        self.assertEqual(verdict.state, ACTUATION_CLOSED)
        self.assertTrue(verdict.ok)

    def test_zero_close_without_durable_proof_is_blocked(self) -> None:
        # THE BUG: nothing closed, nothing live, no durable retirement on record. That is
        # indistinguishable from "we never found the lane" and must never be a success.
        verdict = decide_retire_actuation(
            self._plan(), self._result(), expected_live=(), already_retired=False
        )
        self.assertEqual(verdict.state, ACTUATION_BLOCKED)
        self.assertEqual(verdict.reason, REASON_ZERO_CLOSE_UNPROVEN)
        self.assertFalse(verdict.ok)

    def test_zero_close_with_durable_proof_and_no_live_slot_is_a_verified_noop(self) -> None:
        verdict = decide_retire_actuation(
            self._plan(), self._result(), expected_live=(), already_retired=True
        )
        self.assertEqual(verdict.state, ACTUATION_VERIFIED_NOOP)
        self.assertTrue(verdict.ok)

    def test_durable_proof_cannot_outvote_a_live_expected_slot(self) -> None:
        # A durable record is not liveness (`lane_lifecycle`: a recorded release is "not
        # proof that the slots are gone"). A stale `retired` row while the pair is live
        # must NOT license a success.
        verdict = decide_retire_actuation(
            self._plan(),
            self._result(),
            expected_live=("claude",),
            already_retired=True,
        )
        self.assertEqual(verdict.state, ACTUATION_BLOCKED)
        self.assertEqual(verdict.reason, REASON_ZERO_CLOSE_UNPROVEN)
        self.assertIn("claude", verdict.detail)

    def test_failed_close_is_blocked_even_when_another_slot_closed(self) -> None:
        # A partially closed pair is not a retired lane: the failed slot is still live.
        verdict = decide_retire_actuation(
            self._plan([("codex", "w1:p1"), ("claude", "w1:p2")]),
            self._result(
                closed=[("codex", "w1:p1")],
                failed=[("claude", "w1:p2", "close refused")],
            ),
            expected_live=("claude",),
            already_retired=False,
        )
        self.assertEqual(verdict.state, ACTUATION_BLOCKED)
        self.assertEqual(verdict.reason, REASON_CLOSE_FAILED)
        self.assertFalse(verdict.ok)

    def test_pair_atomic_substitution_zero_close_stays_blocked(self) -> None:
        # #13569's substitution fence zeroes the close targets while an expected slot is
        # still live. Reading "no targets" as "nothing to close" would call that a
        # successful retire; the verdict measures the LIVE expected slots instead.
        rows = [
            _row(_WORKSPACE_ID, "codex", _LANE, "w1:p1"),
            _row(_WORKSPACE_ID, "helper", _LANE, "w1:p2"),
        ]
        plan = self._plan()  # substitution -> targets zeroed by plan_herdr_retire_close
        live = expected_live_slots(rows, plan, managed_roles=("codex", "claude"))
        self.assertEqual(live, ("codex",))
        verdict = decide_retire_actuation(
            plan, self._result(), expected_live=live, already_retired=True
        )
        self.assertEqual(verdict.state, ACTUATION_BLOCKED)
        self.assertFalse(verdict.ok)

    def test_expected_live_ignores_other_lanes_and_the_coordinator_pair(self) -> None:
        rows = [
            _row(_WORKSPACE_ID, "codex", "", "w1:p1"),  # default-lane coordinator
            _row(_WORKSPACE_ID, "claude", "other_lane", "w1:p2"),  # another lane
            _row("wsOther", "codex", _LANE, "w9:p1"),  # another workspace
        ]
        self.assertEqual(expected_live_slots(rows, self._plan()), ())


class RetireCommandFenceTests(unittest.TestCase):
    """The command boundary: JSON verdict + exit code, over real roots and a fake herdr."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.home.mkdir()
        self.primary = tmp / "primary"
        _init_repo(self.primary, anchor=True)
        self.lane_worktree = tmp / "lane_wt"
        _git(
            "worktree", "add", "-b", _LANE, str(self.lane_worktree), "main",
            cwd=self.primary,
        )
        self.integration = tmp / "integration"
        _init_repo(self.integration, anchor=False)

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", _LANE, "w28:p3"),
            _row(_WORKSPACE_ID, "claude", _LANE, "w28:p4"),
            # never a close target: the project's default-lane coordinator pair
            _row(_WORKSPACE_ID, "codex", "", "w28:p1"),
            _row(_WORKSPACE_ID, "claude", "", "w28:p2"),
        ]
        self.rows_error: Exception | None = None
        self.close_failures: set[str] = set()
        self.closed_calls: list[tuple[str, str]] = []

        real_projection = sublane_herdr_projection.list_herdr_agent_rows
        real_execute = sublane_herdr_retire.execute_herdr_retire_close

        def fake_rows(env):
            if self.rows_error is not None:
                raise self.rows_error
            return list(self.rows)

        def fake_execute(plan, **kwargs):
            closed, failed = [], []
            for role, locator in plan.close_targets:
                if role in self.close_failures:
                    failed.append((role, locator, "herdr refused the close"))
                    continue
                self.closed_calls.append((role, locator))
                self.rows = [r for r in self.rows if r["pane_id"] != locator]
                closed.append((role, locator))
            return HerdrRetireCloseResult(
                workspace_id=plan.workspace_id,
                lane_id=plan.lane_id,
                closed=tuple(closed),
                failed=tuple(failed),
                foreign_names=plan.foreign_names,
            )

        sublane_herdr_projection.list_herdr_agent_rows = fake_rows
        sublane_herdr_retire.execute_herdr_retire_close = fake_execute

        def _restore():
            sublane_herdr_projection.list_herdr_agent_rows = real_projection
            sublane_herdr_retire.execute_herdr_retire_close = real_execute
            if self._prev_home is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
            self._tmp.cleanup()

        self.addCleanup(_restore)

    def _declare_lane_active(self, *, worktree=None) -> None:
        """The lane's lifecycle owner row + worktree binding, as ``sublane create``
        writes it (#13681 W1 + #13754). The worktree binding is the canonical token of
        the lane's own worktree — what ``retire --execute`` attests ``--worktree`` against.
        """
        wt = worktree if worktree is not None else self.lane_worktree
        LaneLifecycleStore().declare_active(
            LaneLifecycleKey(_WORKSPACE_ID, _LANE),
            decision=DecisionPointer(
                source="redmine", issue_id=_ISSUE, journal_id=_JOURNAL
            ),
            issue_id=_ISSUE,
            worktree_identity=derive_lane_workspace_token(str(Path(wt).resolve())),
        )

    def _disposition(self) -> str:
        record = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, _LANE))
        return "" if record is None else record.lane_disposition

    def _retire(self, *, repo: Path, worktree: Path | None, journal: str = _JOURNAL):
        args = argparse.Namespace(
            repo=str(repo),
            issue=_ISSUE,
            journal=journal,
            lane_label=_LANE,
            worktree=str(worktree) if worktree is not None else None,
            branch=_LANE,
            integration_branch="main",
            execute=True,
            json=True,
            # every durable-record invariant asserted: the PREFLIGHT is green, so the
            # exit code can only come from the actuation.
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        return code, json.loads(buffer.getvalue())

    # -- the reported defect ------------------------------------------------

    def test_integration_worktree_root_blocks_instead_of_reporting_retire_ok(self) -> None:
        # #13748 j#77473 verbatim: the mis-aimed root resolves no workspace identity. The
        # preflight is green (`retire_ok`) — and that must NOT be the command's verdict.
        self._declare_lane_active()
        code, payload = self._retire(
            repo=self.integration, worktree=self.integration
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_BLOCKED)
        self.assertEqual(close["reason"], REASON_WORKSPACE_UNRESOLVED)
        # the preflight still says the retire was PERMITTED — the two are now distinct
        self.assertEqual(payload["decision"]["state"], "retire_ok")
        # and nothing was actuated: the lane's real pair is untouched
        self.assertEqual(self.closed_calls, [])
        self.assertEqual(len(self.rows), 4)
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_missing_worktree_anchor_blocks(self) -> None:
        code, payload = self._retire(repo=self.primary, worktree=None)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(
            payload["herdr_retire_close"]["reason"], REASON_NO_WORKTREE_ANCHOR
        )
        self.assertEqual(self.closed_calls, [])

    # -- the sanctioned roots ----------------------------------------------

    def test_primary_repo_with_lane_worktree_closes_the_pair(self) -> None:
        self._declare_lane_active()
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 0)
        self.assertTrue(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_CLOSED)
        self.assertEqual(
            sorted(c["locator"] for c in close["closed"]), ["w28:p3", "w28:p4"]
        )
        # the coordinator's default-lane pair is never a target
        self.assertEqual(sorted(self.closed_calls), [("claude", "w28:p4"), ("codex", "w28:p3")])
        self.assertEqual(sorted(r["pane_id"] for r in self.rows), ["w28:p1", "w28:p2"])
        # and the retirement is now a durable fact (#13689 terminal disposition)
        self.assertEqual(close["durable_retirement"], "recorded")
        self.assertEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_lane_worktree_as_repo_root_fails_closed(self) -> None:
        # Retire is a coordinator operation, run from the coordinator repo (--repo=.), with
        # the lane's worktree passed as --worktree (covered by the test above). Passing the
        # lane's OWN worktree as BOTH --repo and --worktree collapses the token derivation
        # (the non-git-lane path) and yields a `dl_` token instead of the `wt_` token the
        # create site bound — so the worktree-binding attestation fails CLOSED rather than
        # closing on a divergent identity. A false block is safe (the runbook still works);
        # a false close is the defect this whole issue exists to prevent.
        self._declare_lane_active()
        code, payload = self._retire(
            repo=self.lane_worktree, worktree=self.lane_worktree
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(
            payload["herdr_retire_close"]["reason"], REASON_WORKTREE_BINDING_MISMATCH
        )
        self.assertEqual(self.closed_calls, [])

    # -- idempotence, verified rather than assumed --------------------------

    def test_rerun_after_a_recorded_retire_is_a_verified_noop(self) -> None:
        self._declare_lane_active()
        first_code, _ = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(first_code, 0)
        # Re-run: the pair is gone AND the durable lifecycle records the retirement.
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 0)
        self.assertTrue(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_VERIFIED_NOOP)
        self.assertEqual(close["closed"], [])
        self.assertEqual(close["expected_live"], [])
        # the second run closed nothing new
        self.assertEqual(len(self.closed_calls), 2)

    def test_zero_close_on_a_lane_never_durably_retired_is_blocked(self) -> None:
        # Same shape as a "successful" no-op — zero live expected slots — but no durable
        # retirement was ever recorded, so the no-op is UNPROVEN and fails closed.
        self._declare_lane_active()
        self.rows = [r for r in self.rows if r["pane_id"] in ("w28:p1", "w28:p2")]
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_BLOCKED)
        self.assertEqual(close["reason"], REASON_ZERO_CLOSE_UNPROVEN)
        self.assertEqual(self.closed_calls, [])

    # -- worktree binding: dirty gate + metadata-only, end-to-end (R3-F2) ----

    def test_exact_bound_dirty_worktree_is_dirty_worktree_zero_close(self) -> None:
        # design j#78572 required: an exact-bound but DIRTY worktree still blocks via the
        # preflight `dirty_worktree` gate — the lane is not closed while it holds
        # uncommitted work, even though the worktree binding matches.
        self._declare_lane_active()
        _dirty(self.lane_worktree)
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(payload["decision"]["state"], "integration_blocked")
        self.assertIn("dirty_worktree", payload["decision"]["blocked_reasons"])
        # the actuation never runs (may_retire is False), so no close is attempted
        self.assertNotIn("herdr_retire_close", payload)
        self.assertEqual(self.closed_calls, [])
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_lane_metadata_present_but_no_lifecycle_binding_blocks_no_backfill(self) -> None:
        # design j#78572 required: a lane with a display-only `lane_metadata` record but NO
        # durable lifecycle binding must block (the metadata is not an authority), and must
        # NOT auto-backfill the lifecycle from it.
        from mozyo_bridge.core.state.lane_metadata import (
            load_lane_records,
            record_lane_created,
        )

        token = derive_lane_workspace_token(str(self.lane_worktree.resolve()))
        record_lane_created(
            lane_workspace_token=token,
            repo_workspace_id=_WORKSPACE_ID,
            issue_id=_ISSUE,
            lane_label=_LANE,
            branch=_LANE,
            worktree_path=str(self.lane_worktree),
            lane_id=_LANE,
            home=self.home,
        )
        # NOTE: no _declare_lane_active() — the lifecycle binding is absent.
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(
            payload["herdr_retire_close"]["reason"], REASON_LANE_OWNER_UNVERIFIED
        )
        self.assertEqual(self.closed_calls, [])
        # no auto-backfill: the lifecycle row was not fabricated from lane_metadata
        self.assertIsNone(
            LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, _LANE))
        )
        # the lane_metadata record is untouched (still present, not consumed)
        self.assertIn(token, load_lane_records(home=self.home))

    # -- runtime failures are never successes -------------------------------

    def test_unreadable_inventory_blocks_and_never_folds_to_an_empty_close(self) -> None:
        self._declare_lane_active()
        self.rows_error = HerdrSessionStartError("herdr binary unresolvable")
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(
            payload["herdr_retire_close"]["reason"], REASON_INVENTORY_UNREADABLE
        )
        self.assertEqual(self.closed_calls, [])
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)

    def test_failed_close_blocks_and_records_no_retirement(self) -> None:
        self._declare_lane_active()
        self.close_failures = {"claude"}
        code, payload = self._retire(repo=self.primary, worktree=self.lane_worktree)
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_BLOCKED)
        self.assertEqual(close["reason"], REASON_CLOSE_FAILED)
        # the worker slot survived, so the lane is NOT retired durably
        self.assertEqual(self._disposition(), "active")

    def test_close_without_a_durable_anchor_is_reported_not_hidden(self) -> None:
        # No --journal: the panes still close (a real actuation), but the lifecycle CAS
        # has no re-readable decision anchor to write with (#13689 R1-F5). The JSON says
        # so plainly, and the next zero-close run will fail closed rather than pass.
        self._declare_lane_active()
        code, payload = self._retire(
            repo=self.primary, worktree=self.lane_worktree, journal=""
        )
        self.assertEqual(code, 0)
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_CLOSED)
        self.assertEqual(
            close["durable_retirement"], "not_recorded:no_durable_decision_anchor"
        )
        self.assertNotEqual(self._disposition(), DISPOSITION_RETIRED)
        rerun_code, rerun = self._retire(
            repo=self.primary, worktree=self.lane_worktree, journal=""
        )
        self.assertEqual(rerun_code, 1)
        self.assertEqual(
            rerun["herdr_retire_close"]["reason"], REASON_ZERO_CLOSE_UNPROVEN
        )

    def test_preflight_only_run_is_unchanged_and_never_actuates(self) -> None:
        # Byte-invariance of the default path: no --execute -> no close, no verdict key,
        # exit from the preflight alone.
        args = argparse.Namespace(
            repo=str(self.primary), issue=_ISSUE, journal=_JOURNAL, lane_label=_LANE,
            worktree=str(self.lane_worktree), branch=_LANE, integration_branch="main",
            execute=False, json=True, issue_closed=True, callbacks_drained=True,
            verified=True, durable_record=True, target_identity_known=True,
            latest_generation_admissible=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertNotIn("herdr_retire_close", payload)
        self.assertTrue(payload["retire_ok"])
        self.assertEqual(self.closed_calls, [])


class RetireTargetAttestationPins(unittest.TestCase):
    """The action-time retire-target attestation (Redmine #13754 F1 + R2-F1, j#78572).

    Pins :func:`attest_retire_target` directly: the durable lifecycle binding must name
    BOTH the requested issue AND the caller's worktree token, and every fail-closed axis
    (no issue, no worktree, no record, issue mismatch, worktree mismatch, worktree
    unbound, unkeyable, unreadable) refuses rather than attests.
    """

    _WT_TOKEN = "wt_00000000cafe0001"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        def _restore():
            if self._prev_home is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
            self._tmp.cleanup()

        self.addCleanup(_restore)

    def _declare(self, lane: str, issue: str, worktree_identity: str = _WT_TOKEN) -> None:
        LaneLifecycleStore().declare_active(
            LaneLifecycleKey(_WORKSPACE_ID, lane),
            decision=DecisionPointer(
                source="redmine", issue_id=issue, journal_id="77985"
            ),
            issue_id=issue,
            worktree_identity=worktree_identity,
        )

    def _attest(self, *, issue=_ISSUE, worktree=_WT_TOKEN, ws=_WORKSPACE_ID, lane=_LANE):
        return attest_retire_target(ws, lane, issue=issue, worktree_identity=worktree)

    def test_matching_issue_and_worktree_attest(self) -> None:
        self._declare(_LANE, _ISSUE)
        ok, reason, _ = self._attest()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_issue_mismatch_is_refused(self) -> None:
        self._declare(_LANE, _ISSUE)
        ok, reason, detail = self._attest(issue="99999")
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_ISSUE_LANE_MISMATCH)
        self.assertIn("13754", detail)

    def test_worktree_mismatch_is_refused(self) -> None:
        # Issue matches, but the caller's --worktree resolves to a DIFFERENT token than
        # the lane's recorded binding: a sibling worktree. Refuse.
        self._declare(_LANE, _ISSUE, worktree_identity="wt_11111111beef0002")
        ok, reason, _ = self._attest(worktree=self._WT_TOKEN)
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_WORKTREE_BINDING_MISMATCH)

    def test_lane_with_no_recorded_worktree_binding_is_refused(self) -> None:
        # A pre-#13754 / unbound lane (empty worktree binding) cannot be attested.
        self._declare(_LANE, _ISSUE, worktree_identity="")
        ok, reason, _ = self._attest()
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_WORKTREE_BINDING_UNVERIFIED)

    def test_malformed_recorded_worktree_binding_fails_closed(self) -> None:
        # A stored binding that is not a canonical token (garbage / truncated) can never
        # equal the reader's canonical token, so it fails closed as a mismatch — never a
        # lenient "close anyway".
        self._declare(_LANE, _ISSUE, worktree_identity="!!not-a-token!!")
        ok, reason, _ = self._attest(worktree=self._WT_TOKEN)
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_WORKTREE_BINDING_MISMATCH)

    def test_no_owner_binding_is_refused(self) -> None:
        ok, reason, _ = self._attest()
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_LANE_OWNER_UNVERIFIED)

    def test_no_issue_argument_is_refused(self) -> None:
        self._declare(_LANE, _ISSUE)
        ok, reason, _ = self._attest(issue="")
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_LANE_OWNER_UNVERIFIED)

    def test_no_worktree_argument_is_refused(self) -> None:
        self._declare(_LANE, _ISSUE)
        ok, reason, _ = self._attest(worktree="")
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_WORKTREE_BINDING_UNVERIFIED)

    def test_unkeyable_unit_is_refused(self) -> None:
        ok, reason, _ = self._attest(ws="")
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_LANE_OWNER_UNVERIFIED)

    def test_unreadable_store_fails_closed(self) -> None:
        # A store that raises on read is "not attested", never "attested by default".
        from mozyo_bridge.core.state import lane_lifecycle as ll_mod

        class _Boom:
            def get(self, key):
                raise ll_mod.LaneLifecycleError("store unreadable")

        real = ll_mod.LaneLifecycleStore
        ll_mod.LaneLifecycleStore = lambda *a, **k: _Boom()
        try:
            ok, reason, _ = self._attest()
        finally:
            ll_mod.LaneLifecycleStore = real
        self.assertFalse(ok)
        self.assertEqual(reason, REASON_LIFECYCLE_UNREADABLE)


class SiblingWorktreeForeignCloseFence(unittest.TestCase):
    """The worktree↔lane foreign-close vector, end-to-end (Redmine #13754 R2-F1, j#78528).

    Lane A and lane B share ONE project workspace (#13377) but each has its OWN worktree,
    declared with its canonical worktree binding. Aiming retire at lane A's worktree while
    naming lane B (even consistently, ``lane=B, issue=B``) used to close B's live pair via
    the sibling worktree — the dirty check probing A while B closes. The action-time
    worktree-binding attestation must block it before any close, clean OR dirty.
    """

    _LANE_A = "issue_13754_A"
    _LANE_B = "issue_13754_B"
    _ISSUE_A = "13754"
    _ISSUE_B = "99999"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.home.mkdir()
        self.primary = tmp / "primary"
        _init_repo(self.primary, anchor=True)
        self.wt_a = tmp / "wt_a"
        _git("worktree", "add", "-b", self._LANE_A, str(self.wt_a), "main", cwd=self.primary)
        self.wt_b = tmp / "wt_b"
        _git("worktree", "add", "-b", self._LANE_B, str(self.wt_b), "main", cwd=self.primary)

        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        # Each lane declares its OWN worktree binding, exactly as the create site does.
        for lane, issue, wt in (
            (self._LANE_A, self._ISSUE_A, self.wt_a),
            (self._LANE_B, self._ISSUE_B, self.wt_b),
        ):
            LaneLifecycleStore().declare_active(
                LaneLifecycleKey(_WORKSPACE_ID, lane),
                decision=DecisionPointer(
                    source="redmine", issue_id=issue, journal_id="77985"
                ),
                issue_id=issue,
                worktree_identity=derive_lane_workspace_token(str(wt.resolve())),
            )

        # Lane B's managed pair is live in the shared project workspace.
        self.rows: list[dict] = [
            _row(_WORKSPACE_ID, "codex", self._LANE_B, "wB:pG"),
            _row(_WORKSPACE_ID, "claude", self._LANE_B, "wB:pW"),
        ]
        self.closed_calls: list[tuple[str, str]] = []
        real_projection = sublane_herdr_projection.list_herdr_agent_rows
        real_execute = sublane_herdr_retire.execute_herdr_retire_close

        def fake_rows(env):
            return list(self.rows)

        def fake_execute(plan, **kwargs):
            closed = []
            for role, locator in plan.close_targets:
                self.closed_calls.append((role, locator))
                self.rows = [r for r in self.rows if r["pane_id"] != locator]
                closed.append((role, locator))
            return HerdrRetireCloseResult(
                workspace_id=plan.workspace_id, lane_id=plan.lane_id,
                closed=tuple(closed), foreign_names=plan.foreign_names,
            )

        sublane_herdr_projection.list_herdr_agent_rows = fake_rows
        sublane_herdr_retire.execute_herdr_retire_close = fake_execute

        def _restore():
            sublane_herdr_projection.list_herdr_agent_rows = real_projection
            sublane_herdr_retire.execute_herdr_retire_close = real_execute
            if self._prev_home is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
            self._tmp.cleanup()

        self.addCleanup(_restore)

    def _retire(self, *, worktree: Path, lane_label: str, issue: str):
        args = argparse.Namespace(
            repo=str(self.primary), issue=issue, journal="77985",
            lane_label=lane_label, worktree=str(worktree), branch=lane_label,
            integration_branch="main", execute=True, json=True,
            issue_closed=True, callbacks_drained=True, verified=True,
            durable_record=True, target_identity_known=True,
            latest_generation_admissible=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        return code, json.loads(buffer.getvalue())

    def _b_disposition(self) -> str:
        record = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, self._LANE_B))
        return "" if record is None else record.lane_disposition

    def test_sibling_worktree_A_with_consistent_B_blocks_and_never_touches_B(self) -> None:
        # THE R2-F1 defect: worktree=A, lane=B, issue=B. Consistent issue/lane, but the
        # worktree is a SIBLING lane's — the vector the reviewer flagged. Must block, and
        # lane B's live pair must be untouched (clean worktree A does not license it).
        code, payload = self._retire(
            worktree=self.wt_a, lane_label=self._LANE_B, issue=self._ISSUE_B
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        close = payload["herdr_retire_close"]
        self.assertEqual(close["state"], ACTUATION_BLOCKED)
        self.assertEqual(close["reason"], REASON_WORKTREE_BINDING_MISMATCH)
        self.assertEqual(self.closed_calls, [])
        self.assertEqual(sorted(r["pane_id"] for r in self.rows), ["wB:pG", "wB:pW"])
        self.assertEqual(self._b_disposition(), "active")

    def test_issue_mismatch_via_sibling_worktree_also_blocks(self) -> None:
        # worktree=A, lane=B, issue=A: the issue↔lane axis catches this one first.
        code, payload = self._retire(
            worktree=self.wt_a, lane_label=self._LANE_B, issue=self._ISSUE_A
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(payload["herdr_retire_close"]["state"], ACTUATION_BLOCKED)
        self.assertEqual(self.closed_calls, [])
        self.assertEqual(self._b_disposition(), "active")

    def test_dirty_sibling_worktree_also_blocks_zero_close(self) -> None:
        # design j#78572 required: the sibling-worktree vector must block regardless of
        # clean/dirty. A DIRTY sibling A is caught by the preflight dirty gate (before the
        # binding fence even runs); either way lane B's live pair is never closed.
        _dirty(self.wt_a)
        code, payload = self._retire(
            worktree=self.wt_a, lane_label=self._LANE_B, issue=self._ISSUE_B
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(self.closed_calls, [])
        self.assertEqual(sorted(r["pane_id"] for r in self.rows), ["wB:pG", "wB:pW"])
        self.assertEqual(self._b_disposition(), "active")

    def test_lanes_own_worktree_retires_it(self) -> None:
        # The fence blocks the sibling worktree, not a correct retire: lane B from B's
        # OWN worktree attests on both axes and closes B.
        code, payload = self._retire(
            worktree=self.wt_b, lane_label=self._LANE_B, issue=self._ISSUE_B
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["retire_ok"])
        self.assertEqual(payload["herdr_retire_close"]["state"], ACTUATION_CLOSED)
        self.assertEqual(
            sorted(self.closed_calls), [("claude", "wB:pW"), ("codex", "wB:pG")]
        )
        self.assertEqual(self._b_disposition(), DISPOSITION_RETIRED)

    def test_symlink_alias_of_the_bound_worktree_still_attests(self) -> None:
        # A symlink alias of B's own worktree canonicalizes (resolve()) to the same token,
        # so it is NOT a bypass and NOT a false mismatch — the retire proceeds.
        alias = Path(self._tmp.name) / "wt_b_alias"
        try:
            alias.symlink_to(self.wt_b)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        code, payload = self._retire(
            worktree=alias, lane_label=self._LANE_B, issue=self._ISSUE_B
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["herdr_retire_close"]["state"], ACTUATION_CLOSED)


class CreateWriterTokenParityPin(unittest.TestCase):
    """The create writer records the token the retire reader computes (#13754 R3-F2(4)).

    Drives the real writer path (``_record_lane_metadata`` -> ``_declare_lane_lifecycle``)
    without the herdr slot launch, and asserts the lifecycle row's ``worktree_identity``
    equals :func:`derive_lane_workspace_token` — the exact function the retire reader
    resolves ``--worktree`` through. If writer and reader ever computed the token
    differently, every legitimate retire would false-mismatch; this pins their parity
    hermetically (the launch-dependent ``test_append_declares_lane_owner_binding`` cannot).
    """

    def test_writer_records_the_readers_canonical_worktree_token(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle import (
            LaneLifecycleKey,
            LaneLifecycleStore,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            primary = root / "primary"
            _init_repo(primary, anchor=True)
            lane_wt = root / "lane_wt"
            _git("worktree", "add", "-b", _LANE, str(lane_wt), "main", cwd=primary)

            prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
            os.environ["MOZYO_BRIDGE_HOME"] = str(home)
            try:
                ops = HerdrSublaneActuatorOps(
                    repo_root=primary,
                    lane_label=_LANE,
                    issue=_ISSUE,
                    branch=_LANE,
                    journal=_JOURNAL,
                )
                # The writer half of the create boundary (no herdr launch needed).
                ops._record_lane_metadata(str(lane_wt))
                record = LaneLifecycleStore().get(
                    LaneLifecycleKey(_WORKSPACE_ID, _LANE)
                )
            finally:
                if prev_home is None:
                    os.environ.pop("MOZYO_BRIDGE_HOME", None)
                else:
                    os.environ["MOZYO_BRIDGE_HOME"] = prev_home

        self.assertIsNotNone(record)
        # the reader resolves --worktree through this exact token function
        self.assertEqual(
            record.worktree_identity,
            derive_lane_workspace_token(str(lane_wt.resolve())),
        )
        self.assertEqual(record.issue_id, _ISSUE)


class LegacyTargetForeignCloseSafety(unittest.TestCase):
    """A legacy target cannot close an unattested pair (Redmine #13754 R2-F1, j#78572).

    Two axes:

    - **structural (plan)**: a legacy ``wt_<hash>`` twin is keyed on the worktree's own
      path token, so a wrong ``--lane-label`` cannot point it at another lane's shared
      pair;
    - **command (attestation)**: the worktree-binding attestation runs for the legacy path
      too, so a legacy / anchorless worktree that cannot be keyed to a durable lifecycle
      binding fails closed (zero close calls) — never a silent legacy close.
    """

    def test_wrong_lane_label_cannot_redirect_a_legacy_close_to_a_shared_lane(self) -> None:
        legacy_token = "wt_deadbeefcafe0001"
        rows = [
            # a shared-model lane B pair, in a real project workspace
            _row(_WORKSPACE_ID, "codex", "issue_13754_B", "wB:pG"),
            _row(_WORKSPACE_ID, "claude", "issue_13754_B", "wB:pW"),
            # the legacy twin's own default-lane pair
            _row(legacy_token, "codex", "", "wL:pG"),
            _row(legacy_token, "claude", "", "wL:pW"),
        ]
        # A legacy token passed as the workspace, with a wrong shared-lane label.
        plan = sublane_herdr_retire.plan_herdr_retire_close(
            rows, workspace_id=legacy_token, lane_id="issue_13754_B"
        )
        # Only the legacy twin's own pair is targeted — never lane B's shared slots.
        self.assertEqual(
            sorted(plan.close_targets), [("claude", "wL:pW"), ("codex", "wL:pG")]
        )
        self.assertNotIn(("codex", "wB:pG"), plan.close_targets)
        self.assertNotIn(("claude", "wB:pW"), plan.close_targets)

    def test_anchorless_legacy_worktree_execute_fails_closed_zero_close(self) -> None:
        # design j#78572: the worktree attestation runs for the legacy path too. A
        # standalone (non-linked) git worktree with NO workspace anchor resolves an empty
        # project workspace, so the lane unit cannot be keyed to a durable lifecycle
        # binding — the retire fails closed with zero close calls, never a legacy close of
        # an unattested pair.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            primary = root / "primary"
            _init_repo(primary, anchor=True)
            # A separate git checkout with a herdr config but NO workspace anchor: it is
            # not a linked worktree of `primary`, so its workspace segment resolves empty.
            legacy = root / "legacy"
            _init_repo(legacy, anchor=False)

            prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
            os.environ["MOZYO_BRIDGE_HOME"] = str(home)
            closed_calls: list = []
            real_proj = sublane_herdr_projection.list_herdr_agent_rows
            real_exec = sublane_herdr_retire.execute_herdr_retire_close

            def fake_exec(plan, **kw):
                closed_calls.extend(plan.close_targets)
                return HerdrRetireCloseResult(
                    workspace_id=plan.workspace_id, lane_id=plan.lane_id,
                    closed=plan.close_targets,
                )

            sublane_herdr_projection.list_herdr_agent_rows = lambda env: []
            sublane_herdr_retire.execute_herdr_retire_close = fake_exec
            try:
                args = argparse.Namespace(
                    repo=str(primary), issue=_ISSUE, journal=_JOURNAL, lane_label=_LANE,
                    worktree=str(legacy), branch=_LANE, integration_branch="main",
                    execute=True, json=True, issue_closed=True, callbacks_drained=True,
                    verified=True, durable_record=True, target_identity_known=True,
                    latest_generation_admissible=True,
                )
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = sublane_lifecycle_command.cmd_sublane_retire(args)
                payload = json.loads(buffer.getvalue())
            finally:
                sublane_herdr_projection.list_herdr_agent_rows = real_proj
                sublane_herdr_retire.execute_herdr_retire_close = real_exec
                if prev_home is None:
                    os.environ.pop("MOZYO_BRIDGE_HOME", None)
                else:
                    os.environ["MOZYO_BRIDGE_HOME"] = prev_home

        self.assertEqual(code, 1)
        self.assertFalse(payload["retire_ok"])
        self.assertEqual(payload["herdr_retire_close"]["state"], ACTUATION_BLOCKED)
        self.assertEqual(closed_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
