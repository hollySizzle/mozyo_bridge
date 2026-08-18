"""Redmine #15707 (b) — ``--target-repo auto`` must resolve the coordinator (default) lane.

The fixed symptom (#15701 j#107992 / #15704 j#108011): the default lane is the coordinator
lane — it lives in the workspace's MAIN checkout and structurally owns no lifecycle row (only
``sublane create`` writes rows) — so every gateway->coordinator callback's ``auto`` frame
resolution refused with ``lane_binding_absent`` -> ``auto_target_repo_unresolved`` and the
callback never left the outbox. The operator's manual repair (j#108011: confirm the main
checkout from the git common-dir, pass it as an explicit ``--target-repo``) is exactly what
the registry-backed fallback now performs, read-only and verified.

Pins (real git repo + linked worktrees + anchored registry, hermetic home):

1. a sender in a lane worktree resolving lane ``default`` gets the registry's canonical MAIN
   checkout (basis ``workspace_canonical``) instead of the ``lane_binding_absent`` refusal;
2. without a registry row the refusal is unchanged (``lane_binding_absent``, empty root) —
   the fallback never invents an answer;
3. a registry canonical that is NOT the live main worktree (the #13152 hijack shape) refuses;
4. a NON-default row-less lane still refuses exactly as before (the fallback is scoped to the
   coordinator lane only), and no refusal detail carries a filesystem path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E402,E501
    BASIS_WORKSPACE_CANONICAL,
    REFUSE_LANE_BINDING_ABSENT,
    resolve_herdr_auto_target_repo,
)

WORKSPACE_ID = "fixture-15707-workspace"
SENDER_LANE = "issue_15707_callback_robustness"


class DefaultLaneAutoTargetRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import write_anchor
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )
        from support.herdr_workspace_fixtures import _anchor_record

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        # Hermetic home: the registry AND the lifecycle authority resolve through
        # `mozyo_bridge_home()`, so the fixture stays off the operator's real state.
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

        self.repo = base / "repo"
        self._env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "PATH": "/usr/bin:/bin",
        }
        self._git("init", "-q", "-b", "main", str(self.repo))
        # #14685: a synthetic repo must not let git auto maintenance daemonize into the
        # temp tree TemporaryDirectory is about to remove.
        self._git("-C", str(self.repo), "config", "--local", "maintenance.auto", "false")
        self._git("-C", str(self.repo), "config", "--local", "gc.auto", "0")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("-C", str(self.repo), "add", "-A")
        self._git("-C", str(self.repo), "commit", "-qm", "c1")

        # The sender's lane worktree — the gateway a callback fires from.
        self.sender_worktree = base / "wt-lane"
        self._git(
            "-C", str(self.repo), "worktree", "add", "-q",
            str(self.sender_worktree), "-b", SENDER_LANE,
        )

        write_anchor(self.repo, _anchor_record(WORKSPACE_ID, self.repo))
        scope = repo_scope_workspace_id(self.sender_worktree)
        if scope != WORKSPACE_ID:
            self.skipTest(
                f"the fixture repo resolved workspace {scope!r}, not the anchored "
                f"{WORKSPACE_ID!r} (is TMPDIR inside a git worktree?)"
            )

    def _restore_home(self) -> None:
        if self._prev_home is None:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
        else:
            os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], check=True, capture_output=True, env=self._env
        )

    def _register(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        register_workspace(self.repo)

    def _target_info(self, *, lane: str) -> dict:
        # The synthesized herdr target record exactly as `resolve_herdr_send_target` leaves
        # it for `auto`: no pane cwd, route identity = the coordinator's unit.
        return {
            "id": "mzb1_ws_claude_lane",
            "cwd": "",
            "workspace_id": WORKSPACE_ID,
            "lane_id": lane,
            "herdr_sender_workspace_id": WORKSPACE_ID,
            "herdr_sender_lane_id": SENDER_LANE,
        }

    def _resolve(self, *, lane: str):
        return resolve_herdr_auto_target_repo(
            self.sender_worktree, self._target_info(lane=lane)
        )

    def test_default_lane_resolves_the_registered_main_checkout(self) -> None:
        # The j#107992 / j#108011 arrangement, fixed: gateway lane worktree -> coordinator
        # (default) lane, no lifecycle row, registry knows the canonical main checkout.
        self._register()
        resolved = self._resolve(lane="default")
        self.assertTrue(resolved.ok, (resolved.reason, resolved.detail))
        self.assertEqual(Path(resolved.root).resolve(), self.repo.resolve())
        self.assertEqual(resolved.basis, BASIS_WORKSPACE_CANONICAL)

    def test_an_empty_lane_normalizes_to_default_and_resolves(self) -> None:
        # `--target coordinator` derives the DEFAULT lane; a blank lane id is the same unit.
        self._register()
        resolved = self._resolve(lane="")
        self.assertTrue(resolved.ok, (resolved.reason, resolved.detail))
        self.assertEqual(Path(resolved.root).resolve(), self.repo.resolve())

    def test_without_a_registry_row_the_refusal_is_unchanged(self) -> None:
        resolved = self._resolve(lane="default")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)
        self.assertEqual(resolved.root, "")

    def test_a_hijacked_canonical_refuses(self) -> None:
        # The #13152 shape: a registry row whose canonical_path is a LINKED worktree (not
        # the main checkout) must not answer the coordinator frame.
        from mozyo_bridge.core.state.workspace_registry import (
            load_workspace_by_id,
            registry_path,
        )

        self._register()
        record = load_workspace_by_id(WORKSPACE_ID)
        self.assertIsNotNone(record)
        import sqlite3

        conn = sqlite3.connect(registry_path())
        try:
            conn.execute(
                "UPDATE workspaces SET canonical_path=? WHERE workspace_id=?",
                (str(self.sender_worktree.resolve()), WORKSPACE_ID),
            )
            conn.commit()
        finally:
            conn.close()
        resolved = self._resolve(lane="default")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_a_non_default_rowless_lane_still_refuses(self) -> None:
        self._register()
        resolved = self._resolve(lane="issue_99999_other_lane")
        self.assertFalse(resolved.ok)
        self.assertEqual(resolved.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_no_refusal_detail_carries_a_filesystem_path(self) -> None:
        # j#95911 finding 2: the detail travels onto the wire outcome / pasteable record.
        for arrange in (lambda: None, self._register):
            arrange()
            for lane in ("default", "issue_99999_other_lane"):
                resolved = self._resolve(lane=lane)
                if resolved.ok:
                    continue
                self.assertNotIn(str(self.repo), resolved.detail)
                self.assertNotIn(str(self.home), resolved.detail)


if __name__ == "__main__":
    unittest.main()
