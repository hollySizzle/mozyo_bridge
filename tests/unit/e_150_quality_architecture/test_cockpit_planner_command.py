"""Fake-port specifications for the cockpit planner/composition boundary (#12977).

These exercise the ``cockpit_planner_command`` surface directly with synthetic
ports — no real tmux server, no live inventory, no filesystem discovery. They
pin:

- the fail-soft ``resolve_project_scope_fields`` leaf: the no-cwd short-circuit,
  the discovery-error / no-git-root / no-scope fail-soft to the unchanged
  ``repo_root`` + empty triple, and the adopted-project path that re-roots to the
  Git root and stamps the repo-relative scope + absolute launch cwd,
- the ``CockpitWorkspacesUseCase`` walk: the explicit-``--repo`` column build and
  the live-inventory dedupe keyed by workspace + lane + project scope (a
  same-workspace/different-lane pair stays two columns; a same-workspace/same-lane
  pair collapses to one; non-agent panes are skipped),
- the ``CockpitPlanExecutorUseCase`` walk: logical token -> captured pane-id
  resolution, the fail-fast ``die`` (exact wording) on a non-zero step and on a
  capturing step that returns no ``%pane`` id, and the ``cleanup_captured``
  best-effort ``kill-pane`` of every captured pane before the abort.

The end-to-end behavior over the live ``commands`` seams stays pinned by the
``layout apply`` / ``cockpit`` characterization tests; this file pins the
boundary in isolation.
"""

from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

from mozyo_bridge.application.cockpit_planner_command import (
    CockpitPlanExecutorUseCase,
    CockpitWorkspacesUseCase,
    resolve_project_scope_fields,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    AGENT_KIND_UNKNOWN,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    LaneIdentity,
)

_PROJECT_DISCOVERY = (
    "mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity"
    ".application.project_discovery"
)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> argparse.Namespace:
    return argparse.Namespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _Cmd:
    """A minimal plan step: ``argv`` tuple, a ``purpose``, an optional capture."""

    def __init__(self, argv, purpose: str, captures: str | None = None) -> None:
        self.argv = tuple(argv)
        self.purpose = purpose
        self.captures = captures


class _Scope:
    def __init__(self, scope, path, label, workdir) -> None:
        self.scope = scope
        self.path = path
        self.label = label
        self.workdir = workdir


# --- resolve_project_scope_fields (fail-soft leaf) ---------------------------


class ResolveProjectScopeFieldsTest(unittest.TestCase):
    def test_no_cwd_returns_repo_root_and_empty_triple(self) -> None:
        self.assertEqual(
            ("/repo", (None, None, None), None),
            resolve_project_scope_fields(None, "/repo"),
        )

    def test_discovery_error_fails_soft_to_repo_root(self) -> None:
        with patch(
            f"{_PROJECT_DISCOVERY}.resolve_workspace_root",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(
                ("/repo", (None, None, None), None),
                resolve_project_scope_fields("/repo/sub", "/repo"),
            )

    def test_no_git_root_and_no_repo_root_fails_soft(self) -> None:
        with patch(f"{_PROJECT_DISCOVERY}.resolve_workspace_root", return_value=None):
            self.assertEqual(
                (None, (None, None, None), None),
                resolve_project_scope_fields("/loose/cwd", None),
            )

    def test_no_project_scope_returns_repo_root_unchanged(self) -> None:
        with patch(
            f"{_PROJECT_DISCOVERY}.resolve_workspace_root", return_value="/git/root"
        ), patch(f"{_PROJECT_DISCOVERY}.project_scope_for_cwd", return_value=None):
            self.assertEqual(
                ("/repo", (None, None, None), None),
                resolve_project_scope_fields("/git/root", "/repo"),
            )

    def test_adopted_project_reroots_and_stamps_scope_and_launch_cwd(self) -> None:
        scope = _Scope(
            scope="proj-id",
            path="projects/proj-id",
            label="ラベル",
            workdir="projects/proj-id",
        )
        with patch(
            f"{_PROJECT_DISCOVERY}.resolve_workspace_root", return_value="/git/root"
        ), patch(
            f"{_PROJECT_DISCOVERY}.project_scope_for_cwd", return_value=scope
        ):
            effective_root, triple, launch_cwd = resolve_project_scope_fields(
                "/git/root/projects/proj-id", "/git/root/projects/proj-id"
            )
        self.assertEqual("/git/root", effective_root)
        self.assertEqual(("proj-id", "projects/proj-id", "ラベル"), triple)
        self.assertEqual("/git/root/projects/proj-id", launch_cwd)


# --- CockpitWorkspacesUseCase ------------------------------------------------


class FakeWorkspacesOps:
    """In-memory :class:`CockpitWorkspacesOps`.

    ``lanes`` maps ``repo_root`` -> :class:`LaneIdentity`; ``scopes`` maps a cwd
    to a ``(effective_root, triple, launch_cwd)`` project-scope result (default:
    unchanged repo_root + empty triple). ``canon`` maps a repo_root to a canonical
    session namespace; ``snapshot`` backs ``take_inventory``.
    """

    def __init__(self, *, lanes=None, scopes=None, canon=None, snapshot=None) -> None:
        self._lanes = lanes or {}
        self._scopes = scopes or {}
        self._canon = canon or {}
        self._snapshot = snapshot

    def resolve_project_scope_fields(self, cwd, repo_root):
        if cwd in self._scopes:
            return self._scopes[cwd]
        return repo_root, (None, None, None), None

    def resolve_canonical_session(self, repo_root):
        return self._canon[repo_root]

    def resolve_workspace_lane(self, repo_root, workspace_id):
        return self._lanes.get(repo_root, LaneIdentity("default", None))

    def take_inventory(self):
        return self._snapshot


def _canon(name, workspace_id=None):
    return argparse.Namespace(name=name, workspace_id=workspace_id)


def _rec(*, agent_kind, workspace_id, repo_root, session, cwd=None):
    workspace = (
        argparse.Namespace(workspace_id=workspace_id)
        if workspace_id is not None
        else None
    )
    return argparse.Namespace(
        agent_kind=agent_kind,
        workspace=workspace,
        repo_root=repo_root,
        session=session,
        cwd=cwd if cwd is not None else repo_root,
    )


class CockpitWorkspacesUseCaseTest(unittest.TestCase):
    def test_explicit_repos_build_columns_in_order(self) -> None:
        ops = FakeWorkspacesOps(
            canon={
                "/repo/a": _canon("sess-a", "ws-a"),
                "/repo/b": _canon("sess-b", None),
            },
            lanes={
                "/repo/a": LaneIdentity("default", None),
                "/repo/b": LaneIdentity("lane-b", "feat"),
            },
        )
        args = argparse.Namespace(layout_repos=["/repo/a", "/repo/b"])
        cols = CockpitWorkspacesUseCase(ops).resolve(args)

        self.assertEqual(["sess-a", "sess-b"], [c.label for c in cols])
        # workspace_id falls back to the canonical name when the identity is None.
        self.assertEqual(["ws-a", "sess-b"], [c.workspace_id for c in cols])
        self.assertEqual(["default", "lane-b"], [c.lane_id for c in cols])
        self.assertEqual(["/repo/a", "/repo/b"], [c.repo_root for c in cols])

    def test_explicit_repo_reroots_to_project_git_root(self) -> None:
        # A project-scoped column re-roots to the resolved Git root and stamps the
        # scope triple + launch cwd from resolve_project_scope_fields.
        project_cwd = "/git/root/projects/p"
        ops = FakeWorkspacesOps(
            scopes={
                project_cwd: (
                    "/git/root",
                    ("p-id", "projects/p", "ラベル"),
                    "/git/root/projects/p",
                )
            },
            canon={"/git/root": _canon("gitsess", "ws-git")},
            lanes={"/git/root": LaneIdentity("default", None)},
        )
        args = argparse.Namespace(layout_repos=[project_cwd])
        (col,) = CockpitWorkspacesUseCase(ops).resolve(args)
        self.assertEqual("/git/root", col.repo_root)
        self.assertEqual("p-id", col.project_scope)
        self.assertEqual("projects/p", col.project_path)
        self.assertEqual("/git/root/projects/p", col.launch_cwd)

    def test_inventory_same_workspace_different_lane_makes_two_columns(self) -> None:
        records = [
            _rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws", repo_root="/main", session="s1"),
            _rec(agent_kind=AGENT_KIND_CLAUDE, workspace_id="ws", repo_root="/main", session="s1"),
            _rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws", repo_root="/wt", session="s2"),
            _rec(agent_kind=AGENT_KIND_CLAUDE, workspace_id="ws", repo_root="/wt", session="s2"),
        ]
        snapshot = argparse.Namespace(records=tuple(records))
        ops = FakeWorkspacesOps(
            snapshot=snapshot,
            lanes={
                "/main": LaneIdentity("default", None),
                "/wt": LaneIdentity("lane-wt", "feature"),
            },
        )
        cols = CockpitWorkspacesUseCase(ops).resolve(
            argparse.Namespace(layout_repos=None)
        )
        self.assertEqual(2, len(cols))
        self.assertEqual(["default", "lane-wt"], sorted(c.lane_id for c in cols))
        self.assertEqual({"ws"}, {c.workspace_id for c in cols})

    def test_inventory_same_workspace_same_lane_dedupes_and_skips_non_agents(self) -> None:
        records = [
            _rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws", repo_root="/main", session="s"),
            _rec(agent_kind=AGENT_KIND_CLAUDE, workspace_id="ws", repo_root="/main", session="s"),
            # A non-agent pane in the same workspace must not add a column.
            _rec(agent_kind=AGENT_KIND_UNKNOWN, workspace_id="ws", repo_root="/main", session="s"),
        ]
        snapshot = argparse.Namespace(records=tuple(records))
        ops = FakeWorkspacesOps(
            snapshot=snapshot, lanes={"/main": LaneIdentity("default", None)}
        )
        cols = CockpitWorkspacesUseCase(ops).resolve(
            argparse.Namespace(layout_repos=None)
        )
        self.assertEqual(1, len(cols))

    def test_inventory_same_lane_different_scope_makes_two_columns(self) -> None:
        # A department-root pane (empty scope) and a project-scoped pane sharing
        # the same Git root + lane stay distinct columns (Redmine #12739).
        records = [
            _rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws", repo_root="/root", session="s", cwd="/root"),
            _rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws", repo_root="/root", session="s", cwd="/root/proj"),
        ]
        snapshot = argparse.Namespace(records=tuple(records))
        ops = FakeWorkspacesOps(
            snapshot=snapshot,
            lanes={"/root": LaneIdentity("default", None)},
            scopes={"/root/proj": ("/root", ("p-id", "proj", "L"), "/root/proj")},
        )
        cols = CockpitWorkspacesUseCase(ops).resolve(
            argparse.Namespace(layout_repos=None)
        )
        self.assertEqual(2, len(cols))
        self.assertEqual({"", "p-id"}, {c.project_scope or "" for c in cols})


# --- CockpitPlanExecutorUseCase ----------------------------------------------


class _Die(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FakePlanExecutorOps:
    """In-memory :class:`CockpitPlanExecutorOps`: records ``run``, ``die`` raises.

    ``results`` maps argv-0 verb -> a queue (list) of results; an unmapped verb
    returns a zero-exit empty result. ``die`` raises :class:`_Die`.
    """

    def __init__(self, results=None) -> None:
        self.calls: list[tuple] = []
        self._results = {k: list(v) for k, v in (results or {}).items()}

    def run(self, *args, **kwargs):
        self.calls.append(args)
        queue = self._results.get(args[0])
        if queue:
            return queue.pop(0)
        return _result()

    def die(self, message):
        raise _Die(message)


class CockpitPlanExecutorUseCaseTest(unittest.TestCase):
    def _plan(self, commands):
        return argparse.Namespace(commands=commands)

    def test_logical_tokens_resolve_to_captured_pane_ids(self) -> None:
        plan = self._plan(
            [
                _Cmd(["new-session", "-P"], "open col0", captures="@col0_codex"),
                _Cmd(["split-window", "-h", "@col0_codex"], "split", captures="@col1_codex"),
            ]
        )
        ops = FakePlanExecutorOps(
            results={
                "new-session": [_result(stdout="%10")],
                "split-window": [_result(stdout="%11")],
            }
        )
        ids = CockpitPlanExecutorUseCase(ops).execute(plan)
        self.assertEqual({"@col0_codex": "%10", "@col1_codex": "%11"}, ids)
        # The split targeted the resolved pane id, not the logical token.
        split = next(c for c in ops.calls if c[0] == "split-window")
        self.assertIn("%10", split)
        self.assertNotIn("@col0_codex", split)

    def test_nonzero_step_aborts_with_purpose_and_command(self) -> None:
        plan = self._plan([_Cmd(["new-session"], "open col0")])
        ops = FakePlanExecutorOps(
            results={"new-session": [_result(returncode=1, stderr="tmux: boom")]}
        )
        with self.assertRaises(_Die) as ctx:
            CockpitPlanExecutorUseCase(ops).execute(plan)
        self.assertIn("cockpit layout step failed (open col0)", ctx.exception.message)
        self.assertIn("tmux: boom", ctx.exception.message)

    def test_capturing_step_without_pane_id_aborts(self) -> None:
        plan = self._plan([_Cmd(["new-session"], "open col0", captures="@col0_codex")])
        ops = FakePlanExecutorOps(results={"new-session": [_result(stdout="")]})
        with self.assertRaises(_Die) as ctx:
            CockpitPlanExecutorUseCase(ops).execute(plan)
        self.assertIn("did not return a pane id (open col0)", ctx.exception.message)

    def test_cleanup_captured_kills_captured_panes_before_abort(self) -> None:
        plan = self._plan(
            [
                _Cmd(["new-session"], "open col0", captures="@col0_codex"),
                _Cmd(["split-window"], "split col1"),
            ]
        )
        ops = FakePlanExecutorOps(
            results={
                "new-session": [_result(stdout="%10")],
                "split-window": [_result(returncode=1, stderr="nope")],
            }
        )
        with self.assertRaises(_Die):
            CockpitPlanExecutorUseCase(ops).execute(plan, cleanup_captured=True)
        kill = [c for c in ops.calls if c[0] == "kill-pane"]
        self.assertEqual([("kill-pane", "-t", "%10")], kill)


if __name__ == "__main__":
    unittest.main()
