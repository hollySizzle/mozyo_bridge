"""herdr sublane retire guarded-close tests (Redmine #13377 shared project workspace).

Pins the fail-closed plan (only the lane unit's managed codex/claude slots are close
targets — never the project workspace's default-lane coordinator pair, another lane, or
a foreign agent; a legacy pre-#13377 ``wt_...`` workspace's default-lane pair closes via
the compatibility twin) and the non-fatal executor over a fake herdr.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    execute_herdr_retire_close,
    plan_herdr_retire_close,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


def _row(ws, role, lane, locator):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class PlanHerdrRetireCloseTest(unittest.TestCase):
    def test_plans_lane_unit_managed_slots_only(self) -> None:
        rows = [
            # the target lane unit's slots (#13377 shared model) -> close targets
            _row("wsMain", "codex", "issue_101_alpha", "w2:p4"),
            _row("wsMain", "claude", "issue_101_alpha", "w2:p5"),
            # the project's default-lane coordinator pair -> NEVER closed
            _row("wsMain", "codex", "", "w2:p3"),
            _row("wsMain", "claude", "", "w2:p2"),
            # another lane of the same workspace -> ignored
            _row("wsMain", "codex", "issue_202_beta", "w2:p6"),
            # other workspace -> ignored
            _row("wsOther", "codex", "", "wO:p2"),
            # foreign non-mzb1 -> ignored
            {"name": "someones-shell", "pane_id": "wZ:p1"},
            # managed-scheme but non-gateway/worker role INSIDE the unit -> recorded
            _row("wsMain", "helper", "issue_101_alpha", "w2:p9"),
        ]
        plan = plan_herdr_retire_close(
            rows, workspace_id="wsMain", lane_id="issue_101_alpha"
        )
        self.assertEqual(
            sorted(plan.close_targets),
            sorted([("codex", "w2:p4"), ("claude", "w2:p5")]),
        )
        self.assertTrue(plan.has_targets)
        self.assertEqual(plan.lane_id, "issue_101_alpha")
        self.assertEqual(len(plan.foreign_names), 1)  # the in-unit helper

    def test_default_lane_of_project_workspace_is_never_a_target(self) -> None:
        rows = [
            _row("wsMain", "codex", "", "w2:p3"),
            _row("wsMain", "claude", "", "w2:p2"),
        ]
        # No lane / an explicit default lane both refuse the coordinator pair.
        for lane in ("", "default"):
            plan = plan_herdr_retire_close(rows, workspace_id="wsMain", lane_id=lane)
            self.assertFalse(plan.has_targets)
            self.assertEqual(plan.foreign_names, ())

    def test_legacy_workspace_token_closes_default_pair(self) -> None:
        rows = [
            _row("wt_1234", "codex", "", "wL:p2"),
            _row("wt_1234", "claude", "", "wL:p3"),
            _row("wt_1234", "codex", "lane-x", "wL:p9"),  # recorded, not closed
        ]
        # Pre-#13377 caller shape: the legacy token passed as the workspace id.
        plan = plan_herdr_retire_close(rows, workspace_id="wt_1234")
        self.assertEqual(
            sorted(plan.close_targets), sorted([("codex", "wL:p2"), ("claude", "wL:p3")])
        )
        self.assertEqual(len(plan.foreign_names), 1)

    def test_legacy_twin_closes_alongside_shared_unit(self) -> None:
        rows = [
            _row("wsMain", "codex", "issue_101_alpha", "w2:p4"),
            _row("wt_1234", "codex", "", "wL:p2"),
            _row("wt_1234", "claude", "", "wL:p3"),
        ]
        plan = plan_herdr_retire_close(
            rows,
            workspace_id="wsMain",
            lane_id="issue_101_alpha",
            legacy_workspace_id="wt_1234",
        )
        self.assertEqual(
            sorted(plan.close_targets),
            sorted([("codex", "w2:p4"), ("codex", "wL:p2"), ("claude", "wL:p3")]),
        )

    def test_empty_workspace_id_matches_nothing(self) -> None:
        rows = [_row("wsL", "codex", "", "wL:p2")]
        plan = plan_herdr_retire_close(rows, workspace_id="")
        self.assertFalse(plan.has_targets)

    def test_row_without_locator_not_a_target(self) -> None:
        rows = [
            {
                "name": encode_assigned_name("wsMain", "codex", "issue_101_alpha"),
                "pane_id": "",
            }
        ]
        plan = plan_herdr_retire_close(
            rows, workspace_id="wsMain", lane_id="issue_101_alpha"
        )
        self.assertFalse(plan.has_targets)


class _CloseHerdr:
    def __init__(self, *, fail_locators=()):
        self.fail_locators = set(fail_locators)
        self.closed: list[str] = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        if rest[:2] == ["pane", "close"]:
            locator = rest[2]
            self.closed.append(locator)
            if locator in self.fail_locators:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="refused")
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"result": {"type": "ok"}}), stderr=""
            )
        raise AssertionError(f"unexpected: {argv!r}")


class ExecuteHerdrRetireCloseTest(unittest.TestCase):
    def _env(self, tmp):
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return {HERDR_ENV: str(binpath)}

    def test_closes_managed_targets(self) -> None:
        herdr = _CloseHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_herdr_retire_close(
                [
                    _row("wsMain", "codex", "issue_101_alpha", "wL:p2"),
                    _row("wsMain", "claude", "issue_101_alpha", "wL:p3"),
                ],
                workspace_id="wsMain",
                lane_id="issue_101_alpha",
            )
            result = execute_herdr_retire_close(
                plan, env=self._env(tmp), runner=herdr.run
            )
        self.assertEqual(sorted(herdr.closed), ["wL:p2", "wL:p3"])
        self.assertEqual(len(result.closed), 2)
        self.assertEqual(len(result.failed), 0)
        self.assertEqual(result.lane_id, "issue_101_alpha")

    def test_close_failure_is_non_fatal(self) -> None:
        herdr = _CloseHerdr(fail_locators={"wL:p3"})
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_herdr_retire_close(
                [
                    _row("wsMain", "codex", "issue_101_alpha", "wL:p2"),
                    _row("wsMain", "claude", "issue_101_alpha", "wL:p3"),
                ],
                workspace_id="wsMain",
                lane_id="issue_101_alpha",
            )
            result = execute_herdr_retire_close(
                plan, env=self._env(tmp), runner=herdr.run
            )
        self.assertEqual([r for r, _ in result.closed], ["codex"])
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0][0], "claude")


class RetireTargetWorktreeDirtyGateTest(unittest.TestCase):
    """Redmine #13331 review j#73338 (blocking): the retire dirty check must inspect the
    TARGET lane worktree (`--worktree`), not the repo the command runs in — else a clean
    coordinator repo lets a dirty lane worktree pass may_retire and (under `--execute`)
    close its managed agents."""

    def test_use_case_override_blocks_on_dirty_target(self) -> None:
        # A CLEAN injected ops (dirty=False) must still block when the caller reports the
        # TARGET worktree dirty via the override.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            RetireAssertions,
            SublaneRetireUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            INTEGRATION_BLOCKED,
        )

        class _CleanOps:
            def is_git_workspace(self):
                return True

            def worktree_dirty(self):
                return False  # coordinator repo is clean

        all_true = RetireAssertions(
            issue_closed=True,
            owner_approval_present=True,
            callbacks_drained=True,
            verification_passed=True,
            durable_record_recorded=True,
            target_identity_known=True,
        )
        outcome = SublaneRetireUseCase(_CleanOps()).run(
            issue="13331",
            lane_label="issue_13331_x",
            worktree_path="/wt",
            branch="b",
            integration_branch=None,
            assertions=all_true,
            worktree_dirty_override=True,  # the TARGET lane worktree is dirty
        )
        self.assertFalse(outcome.preflight.may_retire)
        self.assertEqual(outcome.preflight.decision.state, INTEGRATION_BLOCKED)
        self.assertIn("dirty_worktree", outcome.preflight.decision.blocked_reasons)

    def _git(self, path: Path, *args):
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_repo(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init", "-q")
        self._git(path, "config", "user.email", "t@t")
        self._git(path, "config", "user.name", "t")
        (path / "README.md").write_text("x", encoding="utf-8")
        self._git(path, "add", "-A")
        self._git(path, "commit", "-qm", "init")

    def _retire_args(self, *, repo, worktree, execute):
        import argparse

        return argparse.Namespace(
            issue="13331",
            lane_label="issue_13331_x",
            worktree=str(worktree),
            branch="issue_13331_x",
            integration_branch=None,
            issue_closed=True,
            owner_approved=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,  # #13518 R3-F2 fail-closed invariant asserted
            review_generation_json=None,
            execute=execute,
            repo=str(repo),
            json=True,
        )

    def test_cli_clean_coordinator_dirty_target_blocks(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        with tempfile.TemporaryDirectory() as tmp:
            coord = Path(tmp) / "coord"
            self._init_repo(coord)  # clean coordinator repo
            lane = Path(tmp) / "lane"
            self._init_repo(lane)
            (lane / "uncommitted.txt").write_text("dirty", encoding="utf-8")  # dirty target
            rc = cmd_sublane_retire(
                self._retire_args(repo=coord, worktree=lane, execute=True)
            )
        # A dirty TARGET worktree blocks retirement even though the coordinator is clean.
        self.assertEqual(rc, 1)

    def test_cli_clean_target_permits_retire(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        with tempfile.TemporaryDirectory() as tmp:
            coord = Path(tmp) / "coord"
            self._init_repo(coord)
            lane = Path(tmp) / "lane"
            self._init_repo(lane)  # clean target worktree
            # --execute but NOT herdr backend (no config) -> close is a no-op; the override
            # must not over-block a clean target.
            rc = cmd_sublane_retire(
                self._retire_args(repo=coord, worktree=lane, execute=False)
            )
        self.assertEqual(rc, 0)


class NonGitRetireCloseTest(unittest.TestCase):
    """Redmine #13392 (required test 4): a non-git lane's retire close.

    The lane runs in the workspace root itself (``--worktree`` collapses to the workspace
    root == ``repo_root``). Retire must close ONLY the lane's ``(project_ws, lane_label)``
    managed slots — never the coordinator's default-lane pair — and tombstone the
    lane-scoped ``dl_`` metadata record the non-git create site wrote (never a phantom
    ``wt_`` token, which would miss the record).
    """

    def test_non_git_retire_closes_only_lane_slots_and_tombstones_dl_record(self) -> None:
        import argparse
        from unittest.mock import patch

        from mozyo_bridge.core.state.lane_metadata import (
            load_lane_records,
            record_lane_created,
        )
        from mozyo_bridge.core.state.workspace_registry import register_workspace
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_directory_lane_token,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as proj,
            sublane_herdr_retire as retire_mod,
            sublane_lifecycle_command as lc,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            HerdrRetireCloseResult,
        )

        lane_label = "issue_13392_ng"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            root = Path(tmp) / "nongit_ws"  # a NON-git workspace root (no `git init`)
            root.mkdir()
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                project_ws = register_workspace(root, home=home).record.workspace_id
                # The non-git create site wrote a lane-scoped `dl_` metadata record.
                dl_token = derive_directory_lane_token(str(root.resolve()), lane_label)
                record_lane_created(
                    lane_workspace_token=dl_token,
                    repo_workspace_id=project_ws,
                    issue_id="13392",
                    lane_label=lane_label,
                    branch=lane_label,
                    worktree_path=str(root),
                    lane_id=lane_label,
                    home=home,
                )
                # Live inventory: the coordinator's default-lane pair AND the lane's slots,
                # all in the shared project workspace.
                rows = [
                    _row(project_ws, "codex", "", "w2:p3"),  # coordinator gateway
                    _row(project_ws, "claude", "", "w2:p2"),  # coordinator worker
                    _row(project_ws, "codex", lane_label, "w2:p8"),  # lane gateway
                    _row(project_ws, "claude", lane_label, "w2:p9"),  # lane worker
                ]
                captured: dict = {}

                def _fake_execute(plan, **kw):
                    captured["plan"] = plan
                    return HerdrRetireCloseResult(
                        workspace_id=plan.workspace_id,
                        lane_id=plan.lane_id,
                        closed=plan.close_targets,
                        foreign_names=plan.foreign_names,
                    )

                args = argparse.Namespace(worktree=str(root), lane_label=lane_label)
                with patch.object(
                    proj, "list_herdr_agent_rows", return_value=rows
                ), patch.object(
                    retire_mod, "execute_herdr_retire_close", side_effect=_fake_execute
                ):
                    result = lc._maybe_herdr_retire_close(args, root)
                records = load_lane_records(home=home)

        plan = captured["plan"]
        # Only the lane's two slots close — the coordinator default-lane pair is NEVER a target.
        self.assertEqual(
            sorted(plan.close_targets),
            sorted([("codex", "w2:p8"), ("claude", "w2:p9")]),
        )
        self.assertNotIn(("codex", "w2:p3"), plan.close_targets)
        self.assertNotIn(("claude", "w2:p2"), plan.close_targets)
        self.assertEqual(plan.lane_id, lane_label)
        self.assertEqual(plan.workspace_id, project_ws)
        self.assertEqual(result.lane_id, lane_label)
        # The lane-scoped `dl_` record was tombstoned (the retire found it by the same key).
        self.assertIn(dl_token, records)
        self.assertTrue(records[dl_token].retired)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
