"""`workflow step` <-> runtime-store reconcile CLI integration tests (Redmine #13291).

Drives the real store read `cmd_workflow_step` performs (no `_load_store_action` patch):

- a persisted *gating* pending action (review approved -> aggregate_owner_approval,
  requires_confirmation) fail-closed-gates a live forward leg — the primitive is NOT
  dispatched and the store action is reflected;
- a persisted *non-gating* pending action (review_request + a resolvable codex route ->
  perform_review) is surfaced alongside an unchanged live forward leg (aligned);
- an absent store leaves the live step output byte-identical (backward compatibility);
- a corrupt store DB degrades to the live outcome (fail-open, non-destructive).
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import commands
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    TargetCandidate,
    VIEW_KIND_COCKPIT_PANE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow,
)

REPO = "/work/repo"
PROJECT = "cloud-drive"


def _cand(pane_id, *, role="codex", project_scope="", lane_kind="", repo_root=REPO):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source="pane_option",
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="gw",
        window_name="w",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id="ws",
        workspace_label="ws",
        lane_id="lane",
        lane_label="lane",
        repo_short="repo",
        repo_root=repo_root,
        cwd=repo_root,
        host="host",
        view_kind=VIEW_KIND_COCKPIT_PANE,
        branch=None,
        lane_kind=lane_kind,
        delegation_parent="",
        project_scope=project_scope,
        project_path="",
        project_label="",
    )


def _args(store_path, **overrides):
    base = dict(
        dry_run=False,
        as_json=True,
        session=None,
        issue=None,
        journal=None,
        callback=None,
        store_path=store_path,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_step(args, candidates, *, self_pane="%self"):
    """Run `cmd_workflow_step` with a real store read (only tmux/discovery patched)."""
    out = io.StringIO()
    with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "current_pane", lambda: self_pane
    ), patch.object(
        cli_workflow, "_discover_candidates", return_value=candidates
    ), contextlib.redirect_stdout(out):
        rc = cli_workflow.cmd_workflow_step(args)
    return rc, out.getvalue()


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = str(Path(self._tmp.name) / "workflow-runtime.sqlite")

    def _persist(self, *events, routes=()):
        argv = ["workflow", "runtime"]
        for ev in events:
            argv += ["--event", ev]
        argv += ["--ready-independent", "1", "--capacity", "2",
                 "--persist", "--store-path", self.store_path, "--repo", self._tmp.name]
        for r in routes:
            argv += ["--route-identity", r]
        argv += ["--json"]
        parser = build_parser()
        ns = parser.parse_args(argv)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ns.func(ns)
        self.assertEqual(rc, 0)


class GatesForwardLegTest(_StoreCase):
    def test_gating_store_action_blocks_the_forward_leg(self):
        # review approved -> aggregate_owner_approval (requires_confirmation): gating.
        self._persist(
            "13291:review_request,id=13291:72672,commit=1",
            "13291:review,id=13291:72700,conclusion=approved,commit=1",
            routes=("route_id=r,issue=13291,ws=w,role=codex,pane_name=gw,pane_id=%17",),
        )
        # A live grandparent forward leg (would otherwise dispatch project-gateway consult).
        candidates = [_cand("%self"), _cand("%gw", project_scope=PROJECT)]
        with patch.object(commands, "orchestrate_handoff") as orch:
            rc, text = _run_step(_args(self.store_path), candidates)
        # Fail-toward-safe: no primitive dispatched, rc 1, gated disposition + reflected action.
        orch.assert_not_called()
        self.assertEqual(rc, 1)
        payload = json.loads(text)
        self.assertEqual(payload["execution"], "blocked")
        self.assertEqual(payload["reason"], "store_pending_action_gates")
        self.assertEqual(payload["reconcile_disposition"], "store_gates_live")
        self.assertEqual(
            payload["store_pending_action"]["action"], "aggregate_owner_approval"
        )
        self.assertTrue(payload["store_pending_action"]["requires_confirmation"])
        # Public-safe: no pane id leaks from the store into the step envelope.
        self.assertNotIn("%17", text)


class AlignedForwardLegTest(_StoreCase):
    def test_non_gating_store_action_is_surfaced_dry_run(self):
        # review_request + a resolvable codex route -> perform_review (confirm False,
        # blocked_reason empty): pending but non-gating -> aligned, live leg unchanged.
        self._persist(
            "13291:review_request,id=13291:72672,commit=1",
            routes=("route_id=r,issue=13291,ws=w,role=codex,pane_name=gw,pane_id=%17",),
        )
        candidates = [_cand("%self"), _cand("%gw", project_scope=PROJECT)]
        rc, text = _run_step(_args(self.store_path, dry_run=True), candidates)
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        # The live forward leg is unchanged (dry-run of the consultation), and the store
        # action is surfaced without gating.
        self.assertEqual(payload["execution"], "dry_run")
        self.assertEqual(payload["reason"], "consultation_ready")
        self.assertEqual(payload["reconcile_disposition"], "store_aligned")
        self.assertEqual(payload["store_pending_action"]["action"], "perform_review")
        self.assertFalse(payload["store_pending_action"]["requires_confirmation"])


class RepoLocalBindingTest(_StoreCase):
    """The store fold uses the same repo-local binding as resume (review j#72693).

    In a provider-rebind repo (auditor -> claude), a persisted review_request + a claude
    route resolves to a non-gating ``perform_review``. If the step folded the store with
    the compatibility default binding (auditor -> codex) instead, the claude route would
    not match, the action would fail closed ``route_identity_unresolved`` (gating), and the
    live forward leg would be wrongly downgraded to blocked. Pinning ``store_aligned`` here
    proves the step folds the store identically to resume.
    """

    def _rebind_repo(self) -> str:
        repo = Path(self._tmp.name) / "rebind_repo"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        (repo / ".mozyo-bridge" / "config.yaml").write_text(
            "provider_binding:\n  version: 1\n  bindings:\n    auditor: claude\n",
            encoding="utf-8",
        )
        return str(repo)

    def test_step_folds_store_with_repo_local_binding(self):
        repo = self._rebind_repo()
        # review_request -> perform_review (auditor); route is a claude lane, matching the
        # rebound auditor -> claude binding.
        self._persist(
            "13291:review_request,id=13291:72672,commit=1",
            routes=("route_id=r,issue=13291,ws=w,role=claude,pane_name=wk,pane_id=%20",),
        )
        # The self lane's repo_root points at the rebind repo, so the binding resolves there.
        candidates = [
            _cand("%self", repo_root=repo),
            _cand("%gw", project_scope=PROJECT, repo_root=repo),
        ]
        rc, text = _run_step(_args(self.store_path, dry_run=True), candidates)
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        # With the repo-local binding the claude route matches -> non-gating perform_review
        # -> aligned, and the live forward leg stays a dry-run (never gated).
        self.assertEqual(payload["execution"], "dry_run")
        self.assertEqual(payload["reconcile_disposition"], "store_aligned")
        self.assertEqual(payload["store_pending_action"]["action"], "perform_review")
        self.assertEqual(payload["store_pending_action"]["blocked_reason"], "")
        self.assertFalse(payload["store_pending_action"]["requires_confirmation"])


class BackwardCompatTest(_StoreCase):
    def test_absent_store_preserves_prior_step_output(self):
        # No persisted store at the path: the reconcile degrades to the live outcome and
        # emits NO reconcile fields — byte-identical to pre-#13291 `workflow step`.
        candidates = [_cand("%self"), _cand("%gw", project_scope=PROJECT)]
        absent_path = str(Path(self._tmp.name) / "does-not-exist.sqlite")
        rc, text = _run_step(_args(absent_path, dry_run=True), candidates)
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        self.assertEqual(payload["execution"], "dry_run")
        self.assertEqual(payload["reason"], "consultation_ready")
        self.assertNotIn("reconcile_disposition", payload)
        self.assertNotIn("store_pending_action", payload)

    def test_corrupt_store_degrades_fail_open(self):
        # A non-DB file at the store path must not break the live step (fail-open).
        Path(self.store_path).write_text("not a sqlite database", encoding="utf-8")
        candidates = [_cand("%self"), _cand("%gw", project_scope=PROJECT)]
        rc, text = _run_step(_args(self.store_path, dry_run=True), candidates)
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        self.assertEqual(payload["reason"], "consultation_ready")
        self.assertNotIn("reconcile_disposition", payload)


if __name__ == "__main__":
    unittest.main()
