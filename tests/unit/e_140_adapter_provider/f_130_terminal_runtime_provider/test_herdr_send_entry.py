"""herdr-native send-entry resolution tests (Redmine #13261, increment 2).

Pins the orchestrate-entry seam: backend detection, the synthesized
``project_preflight_target``-compatible pane record (a ``normal_window`` projection
so the main-lane cockpit guard stays inactive while ``binds_receiver`` resolves the
strong role), and the fail-closed branches. Uses a real (temp) workspace + fake
herdr binary/runner; no live herdr, no tmux.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    VIEW_KIND_NORMAL_WINDOW,
    project_preflight_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    HerdrSendEntryError,
    explicit_tmux_pane_target,
    herdr_backend_selected,
    herdr_effective_backend_selected,
    resolve_herdr_send_target,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


def _args(repo):
    ns = argparse.Namespace()
    ns.repo = str(repo)
    ns.to = "claude"
    return ns


# Redmine #13729: the herdr send helpers are now Namespace-free; these thin
# adapters derive the same scalars the facade passes (repo root + raw target
# fields) from an ``args`` fixture, so the fixtures stay unchanged.
def _effective_from_args(args):
    return herdr_effective_backend_selected(
        repo_root=repo_root_from_args(args), target=getattr(args, "target", None)
    )


def _resolve_from_args(args, *, receiver):
    return resolve_herdr_send_target(
        repo_root=repo_root_from_args(args),
        target=getattr(args, "target", None),
        target_repo=getattr(args, "target_repo", None),
        target_lane=getattr(args, "target_lane", None),
        receiver=receiver,
    )


class _Ctx:
    """A prepared herdr workspace: config, anchor, fake binary + runner."""

    def __init__(self, tmp, *, backend="herdr", rows=None, sender_role="codex", sender_lane="lane-1"):
        self.repo = Path(tmp) / "repo"
        self.repo.mkdir()
        self.home = Path(tmp) / "home"
        self.home.mkdir()
        (self.repo / ".mozyo-bridge").mkdir()
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            f"version: 1\nterminal_transport:\n  backend: {backend}\n", encoding="utf-8"
        )
        register_workspace(self.repo, home=self.home)
        self.workspace_id = read_anchor(self.repo)["workspace_id"]
        self.rows = rows(self.workspace_id) if rows else []
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.binpath = binpath
        self.sender_role = sender_role
        self.sender_lane = sender_lane

    def run(self, argv, capture_output=None, text=None, timeout=None, **kw):
        # `herdr_workspace_segment` probes git topology (#13331). These test repos are
        # plain (non-git) temp dirs, so the probe must read as "not a git checkout" ->
        # standalone -> registry workspace_id (patch.dict replaces the real subprocess.run).
        if list(argv[:1]) == ["git"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a git repo")
        rest = list(argv[1:])
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.rows}), stderr=""
            )
        raise AssertionError(f"unexpected call: {argv!r}")

    def env(self, *, with_sender=True):
        e = {HERDR_ENV: str(self.binpath), "MOZYO_BRIDGE_HOME": str(self.home)}
        if with_sender:
            e["MOZYO_WORKSPACE_ID"] = self.workspace_id
            e["MOZYO_AGENT_ROLE"] = self.sender_role
            e["MOZYO_LANE_ID"] = self.sender_lane
        return e


class BackendSelectionTest(unittest.TestCase):
    def test_true_for_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, backend="herdr")
            self.assertTrue(herdr_backend_selected(_args(ctx.repo)))

    def test_false_for_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, backend="tmux")
            self.assertFalse(herdr_backend_selected(_args(ctx.repo)))


class EffectiveBackendSelectionTest(unittest.TestCase):
    """Redmine #13320 (a-narrow): the effective-backend predicate narrows the config
    herdr selection by target kind — an explicit tmux ``%pane`` target is NOT a herdr
    send even under ``backend: herdr`` (it rides the tmux rail), while role /
    receiver-name targets stay on the herdr path."""

    @staticmethod
    def _args(repo, target=None):
        ns = argparse.Namespace()
        ns.repo = str(repo)
        ns.to = "claude"
        ns.target = target
        return ns

    def test_explicit_pane_target_is_not_effective_herdr_under_herdr_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, backend="herdr")
            args = self._args(ctx.repo, target="%45")
            # config-level selection is still herdr...
            self.assertTrue(herdr_backend_selected(args))
            self.assertTrue(explicit_tmux_pane_target(args))
            # ...but the effective (target-kind-narrowed) predicate routes it to tmux.
            self.assertFalse(_effective_from_args(args))

    def test_implicit_receiver_target_stays_effective_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, backend="herdr")
            # No explicit target (role-based `--to claude` implicit resolution).
            args = self._args(ctx.repo, target=None)
            self.assertFalse(explicit_tmux_pane_target(args))
            self.assertTrue(_effective_from_args(args))
            # A receiver-label target (not a `%pane`) is also implicit resolution.
            args_label = self._args(ctx.repo, target="claude")
            self.assertFalse(explicit_tmux_pane_target(args_label))
            self.assertTrue(_effective_from_args(args_label))

    def test_tmux_config_is_never_effective_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, backend="tmux")
            # Neither an explicit pane nor an implicit target flips a tmux config.
            self.assertFalse(
                _effective_from_args(self._args(ctx.repo, target="%45"))
            )
            self.assertFalse(
                _effective_from_args(self._args(ctx.repo, target=None))
            )


class ResolveHerdrSendTargetTest(unittest.TestCase):
    def _resolve(self, ctx, *, with_sender=True, receiver="claude"):
        with patch("subprocess.run", ctx.run), patch.dict(
            os.environ, ctx.env(with_sender=with_sender), clear=True
        ):
            return _resolve_from_args(_args(ctx.repo), receiver=receiver)

    def test_synthesizes_normal_window_projection(self) -> None:
        # Redmine #13305: the real send path is now lane-in-match, so the target must
        # live in the derived lane (a peer `claude` dispatch derives the sender's own
        # lane, lane-1) — a lane-x claude would fail closed (see the cross-lane test).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(
                tmp,
                rows=lambda ws: [
                    {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": "wT:pT"}
                ],
            )
            pane = self._resolve(ctx)
        self.assertEqual(pane["id"], "wT:pT")
        self.assertEqual(pane["window_name"], "claude")
        self.assertEqual(pane["agent_role"], "")  # no @mozyo_agent_role -> not cockpit
        self.assertEqual(pane["workspace_id"], ctx.workspace_id)
        # The projection binds the receiver and stays a normal_window (main-lane
        # cockpit guard therefore inactive) — the tmux-only cockpit semantics are an
        # explicit no-op under herdr.
        preflight = project_preflight_target(pane)
        self.assertTrue(preflight.binds_receiver("claude"))
        self.assertEqual(preflight.view_kind, VIEW_KIND_NORMAL_WINDOW)

    def test_missing_sender_env_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(
                tmp,
                rows=lambda ws: [
                    {"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": "wT:pT"}
                ],
            )
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, with_sender=False)
            self.assertEqual(c.exception.reason, "missing_sender_env")
            # Redmine #13397 finding 2 (Option B, j#73755): the message is herdr-native
            # and names the sanctioned lane-dispatch route instead of the tmux-era
            # `target_unavailable` wording — an env-less operator shell is refused, not
            # admitted as a dispatch origin.
            msg = str(c.exception)
            self.assertIn("attested lane-sender identity", msg)
            self.assertIn("coordinator agent", msg)
            self.assertIn("not a lane-dispatch origin", msg)

    def test_no_target_agent_fails_closed(self) -> None:
        # Redmine #13305: no live claude -> the derived slot is unavailable. The
        # convergence projects the #13302 ledger vocabulary (`target_unavailable`),
        # not the legacy lane-less `no_match` token.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(
                tmp,
                rows=lambda ws: [
                    {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"}
                ],
            )
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, receiver="claude")
            self.assertEqual(c.exception.reason, "target_unavailable")

    def test_cross_lane_worker_fails_closed_no_all_lane_scan(self) -> None:
        # Redmine #13305: a claude live only in lane-x, sender in lane-1. The
        # lane-in-match authority derives lane-1 and fails closed rather than scanning
        # all lanes to find the lane-x worker (no all-lane `(ws, role)` fallback).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(
                tmp,
                rows=lambda ws: [
                    {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                    {"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": "wT:pT"},
                ],
            )
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, receiver="claude")
            self.assertEqual(c.exception.reason, "target_unavailable")

    def test_backend_not_selected_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, backend="tmux")
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx)
            self.assertEqual(c.exception.reason, "backend_not_selected")


class CrossWorkspaceHerdrSendTargetTest(unittest.TestCase):
    """Redmine #13331: coordinator -> lane gateway crosses a workspace boundary
    (option A per-lane herdr workspace). An explicit ``--target-repo <lane-worktree>``
    resolves the receiver in the LANE workspace (the worktree anchor's mozyo
    workspace id), not the sender's — the sender-scoped route authority cannot reach
    it. A ``--target-repo`` that resolves to the sender's own workspace (or ``auto`` /
    unset) stays on the same-workspace path."""

    def _prepare(self, tmp):
        # Sender = coordinator (codex, default lane) in its own workspace.
        ctx = _Ctx(tmp, sender_role="codex", sender_lane="default")
        lane_repo = Path(tmp) / "lane"
        lane_repo.mkdir()
        register_workspace(lane_repo, home=ctx.home)
        lane_ws = read_anchor(lane_repo)["workspace_id"]
        self.assertNotEqual(lane_ws, ctx.workspace_id)
        return ctx, lane_repo, lane_ws

    @staticmethod
    def _args(ctx, target_repo, *, to="codex"):
        ns = argparse.Namespace()
        ns.repo = str(ctx.repo)
        ns.to = to
        ns.target = None
        ns.target_repo = str(target_repo)
        return ns

    def _resolve(self, ctx, args):
        with patch("subprocess.run", ctx.run), patch.dict(
            os.environ, ctx.env(), clear=True
        ):
            return _resolve_from_args(args, receiver=args.to)

    def test_resolves_lane_gateway_in_target_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lane_repo, lane_ws = self._prepare(tmp)
            # The lane gateway (codex, default lane) lives in the LANE workspace; a
            # same-role codex in the SENDER workspace must NOT be chosen.
            ctx.rows = [
                {"name": encode_assigned_name(lane_ws, "codex", ""), "pane_id": "wL:p2"},
                {
                    "name": encode_assigned_name(ctx.workspace_id, "codex", "default"),
                    "pane_id": "wC:p2",
                },
            ]
            pane = self._resolve(ctx, self._args(ctx, lane_repo, to="codex"))
        self.assertEqual(pane["id"], "wL:p2")
        self.assertEqual(pane["workspace_id"], lane_ws)
        self.assertEqual(pane["lane_id"], "default")
        # The target record's cwd is the LANE worktree (the --target-repo), so the
        # downstream target_repo_mismatch gate compares like-for-like rather than
        # blocking on the coordinator's own root.
        self.assertEqual(pane["cwd"], str(lane_repo))
        # The env-derived SENDER fields stay the coordinator's: the gateway-route gate
        # enforces on the sender's lane, not the target's.
        self.assertEqual(pane["herdr_sender_workspace_id"], ctx.workspace_id)

    def test_same_workspace_target_repo_uses_sender_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="default")
            ctx.rows = [
                {
                    "name": encode_assigned_name(ctx.workspace_id, "claude", "default"),
                    "pane_id": "wC:p3",
                }
            ]
            # --target-repo names the sender's OWN repo -> not cross-workspace.
            pane = self._resolve(ctx, self._args(ctx, ctx.repo, to="claude"))
        self.assertEqual(pane["id"], "wC:p3")
        self.assertEqual(pane["workspace_id"], ctx.workspace_id)

    def test_cross_workspace_missing_gateway_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, lane_repo, lane_ws = self._prepare(tmp)
            # Lane workspace has a worker but no codex gateway slot.
            ctx.rows = [
                {"name": encode_assigned_name(lane_ws, "claude", ""), "pane_id": "wL:p3"}
            ]
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, self._args(ctx, lane_repo, to="codex"))
        self.assertEqual(c.exception.reason, "target_unavailable")


class SharedWorkspaceExplicitLaneDispatchTest(unittest.TestCase):
    """Redmine #13377 (review j#73640 finding 1): coordinator -> lane gateway under
    the shared project workspace model is a SAME-workspace, explicit-lane send whose
    ``--target-repo <lane worktree>`` is the repo/cwd gate (j#73613). The synthesized
    target ``cwd`` must be the lane worktree — the resolved lane slot's launch cwd
    (``prepare_session --cwd``) — not the sender's repo root; otherwise the downstream
    ``target_repo_mismatch`` gate compares ``expected`` = lane worktree against
    ``observed`` = main repo and structurally blocks the dispatch. An implicit send
    (no ``--target-lane``) keeps the sender-root cwd, so the repo gate's conservatism
    for implicit sends is unchanged."""

    @staticmethod
    def _args(ctx, *, target_repo=None, target_lane=None, to="codex"):
        ns = argparse.Namespace()
        ns.repo = str(ctx.repo)
        ns.to = to
        ns.target = None
        if target_repo is not None:
            ns.target_repo = str(target_repo)
        ns.target_lane = target_lane
        return ns

    def _resolve(self, ctx, args):
        with patch("subprocess.run", ctx.run), patch.dict(
            os.environ, ctx.env(), clear=True
        ):
            return _resolve_from_args(args, receiver=args.to)

    @staticmethod
    def _lane_worktree(tmp, ctx) -> Path:
        # A stand-in lane worktree that inherits the PROJECT identity: it carries the
        # sender repo's own anchor (the plain-dir harness cannot probe the real git
        # worktree topology, so the inheritance is materialized as the shared anchor).
        lane = Path(tmp) / "lane-wt"
        (lane / ".mozyo-bridge").mkdir(parents=True)
        anchor = ctx.repo / ".mozyo-bridge" / "workspace-anchor.json"
        (lane / ".mozyo-bridge" / "workspace-anchor.json").write_text(
            anchor.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return lane

    def test_explicit_lane_dispatch_synthesizes_lane_worktree_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="default")
            lane_wt = self._lane_worktree(tmp, ctx)
            lane_label = "issue_13377_x"
            ctx.rows = [
                # The lane gateway slot of the SAME project workspace...
                {
                    "name": encode_assigned_name(ctx.workspace_id, "codex", lane_label),
                    "pane_id": "w2:p4",
                },
                # ...never the coordinator's own default-lane codex.
                {
                    "name": encode_assigned_name(ctx.workspace_id, "codex", "default"),
                    "pane_id": "w2:p2",
                },
            ]
            pane = self._resolve(
                ctx,
                self._args(ctx, target_repo=lane_wt, target_lane=lane_label, to="codex"),
            )
        self.assertEqual(pane["id"], "w2:p4")
        self.assertEqual(pane["workspace_id"], ctx.workspace_id)
        self.assertEqual(pane["lane_id"], lane_label)
        # j#73640 regression: the target record's cwd is the LANE worktree (the
        # explicit --target-repo), so the downstream target_repo_mismatch gate
        # compares like-for-like instead of blocking on the sender's own root.
        self.assertEqual(pane["cwd"], str(lane_wt))
        self.assertNotEqual(pane["cwd"], str(ctx.repo))

    def test_implicit_send_keeps_sender_root_cwd(self) -> None:
        # No --target-lane: the derived same-lane send keeps cwd = the sender's repo
        # root (the repo gate's conservative default is unchanged by #13377).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="lane-1")
            ctx.rows = [
                {
                    "name": encode_assigned_name(ctx.workspace_id, "claude", "lane-1"),
                    "pane_id": "wT:pT",
                }
            ]
            pane = self._resolve(ctx, self._args(ctx, to="claude"))
        self.assertEqual(pane["cwd"], str(Path(ctx.repo).resolve()))

    def test_explicit_lane_without_target_repo_keeps_sender_root_cwd(self) -> None:
        # --target-lane alone (no explicit repo gate requested): nothing to compare
        # like-for-like against, so cwd stays the sender root.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="default")
            ctx.rows = [
                {
                    "name": encode_assigned_name(ctx.workspace_id, "codex", "issue_1_x"),
                    "pane_id": "w2:p4",
                }
            ]
            pane = self._resolve(
                ctx, self._args(ctx, target_lane="issue_1_x", to="codex")
            )
        self.assertEqual(pane["id"], "w2:p4")
        self.assertEqual(pane["cwd"], str(Path(ctx.repo).resolve()))


class CoordinatorPseudoTargetHerdrSendTest(unittest.TestCase):
    """Redmine #13476 (Design Consultation Answer j#74599, Option A): the herdr send
    rail consumes the `--target coordinator` semantic pseudo-target and routes the
    sublane->parent coordinator callback to the workspace DEFAULT lane + the configured
    coordinator provider — NOT the sender's own sublane (Review j#74511 Finding 1's
    same-lane misroute). The documented backend-neutral form
    `--to codex --target coordinator` stays intact and `--to` public choices are
    unchanged (codex); the translation is internal. An explicit `--target-lane` still
    overrides the pseudo-target, a non-coordinator target is unaffected, and a missing
    default-lane coordinator fails closed (never a silent same-lane fallback).
    """

    @staticmethod
    def _args(ctx, *, target=None, target_lane=None, to="codex"):
        ns = argparse.Namespace()
        ns.repo = str(ctx.repo)
        ns.to = to
        ns.target = target
        ns.target_lane = target_lane
        return ns

    def _resolve(self, ctx, args):
        with patch("subprocess.run", ctx.run), patch.dict(
            os.environ, ctx.env(), clear=True
        ):
            return _resolve_from_args(args, receiver=args.to)

    @staticmethod
    def _rows(ws):
        # Two codex agents in the SAME workspace: the parent coordinator in the DEFAULT
        # lane, and the sender's own sublane gateway in `lane-1`. A bare `--to codex`
        # from the sublane would derive `lane-1` (same-lane); the coordinator
        # pseudo-target must pick `default`.
        return [
            {"name": encode_assigned_name(ws, "codex", "default"), "pane_id": "wC:pC"},
            {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wG:pG"},
        ]

    def test_coordinator_pseudo_target_resolves_default_lane_from_sublane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="lane-1", rows=self._rows)
            pane = self._resolve(ctx, self._args(ctx, target="coordinator", to="codex"))
        # The parent coordinator (default lane), never the sender's own sublane gateway.
        self.assertEqual(pane["id"], "wC:pC")
        self.assertEqual(pane["lane_id"], "default")

    def test_explicit_target_lane_overrides_coordinator_pseudo_target(self) -> None:
        # An intentional `--target-lane` override is never ignored (tier-1 explicit lane
        # beats the coordinator pseudo-target's default-lane derivation).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="lane-1", rows=self._rows)
            pane = self._resolve(
                ctx,
                self._args(ctx, target="coordinator", target_lane="lane-1", to="codex"),
            )
        self.assertEqual(pane["id"], "wG:pG")
        self.assertEqual(pane["lane_id"], "lane-1")

    def test_non_coordinator_target_is_unaffected(self) -> None:
        # A receiver-label target (not the coordinator pseudo-target) keeps the bare
        # same-lane derivation — the translation is scoped to `coordinator` only.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane="lane-1", rows=self._rows)
            pane = self._resolve(ctx, self._args(ctx, target="codex", to="codex"))
        self.assertEqual(pane["id"], "wG:pG")
        self.assertEqual(pane["lane_id"], "lane-1")

    def test_coordinator_pseudo_target_fails_closed_without_default_lane(self) -> None:
        # No live default-lane coordinator -> fail closed. Critically, it must NOT fall
        # back to the same-lane gateway (that silent fallback is the exact Finding 1
        # misroute); the send raises rather than landing on the wrong target.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(
                tmp,
                sender_role="codex",
                sender_lane="lane-1",
                rows=lambda ws: [
                    {
                        "name": encode_assigned_name(ws, "codex", "lane-1"),
                        "pane_id": "wG:pG",
                    }
                ],
            )
            with self.assertRaises(HerdrSendEntryError):
                self._resolve(ctx, self._args(ctx, target="coordinator", to="codex"))


class ExplicitTargetMismatchHerdrSendTest(unittest.TestCase):
    """Redmine #13884 (repro anchors #13882 j#79958 / #13883 j#79959): a coordinator's
    explicit ``--target <lane-gateway locator>`` on the herdr rail used to be silently
    dropped, so routing fell back to ``--to`` + the sender's OWN (default) lane and resolved
    to the coordinator's own pane — a sender echo with a false-positive ``sent``.

    The fix is a consistency check, not a blanket reject: the #13305 route authority still
    resolves by ``--to`` + ``--target-lane`` + ``--target-repo`` (the locator is transient
    cache, never the routing key — the same reason a lane-pinned worker dispatch passes
    ``--target <worker-locator>`` alongside ``--target-lane`` and still resolves the stable
    slot). After resolution the rail cross-checks the explicit target against the resolved
    identity: a target that AGREES with the derived route passes through (resolve-to-exact);
    a MISMATCH fails closed with a typed ``explicit_target_mismatch`` zero-send reason (no
    coordinator / sender-lane fallback). The fixture is the exact reported shape — a
    coordinator + TWO lane gateways in one shared workspace.
    """

    #: coordinator (codex, default lane) + two lane gateways (codex) in the SAME shared
    #: project workspace; each carries a distinct live locator.
    COORDINATOR_LOCATOR = "wC:pC"
    GATEWAY_A_LOCATOR = "wA:pA"
    GATEWAY_B_LOCATOR = "wB:pB"
    LANE_A = "issue_13882_attestation_store_compat"
    LANE_B = "issue_13883_testpypi_source_ref_preflight"

    def _rows(self, ws):
        return [
            {"name": encode_assigned_name(ws, "codex", "default"), "pane_id": self.COORDINATOR_LOCATOR},
            {"name": encode_assigned_name(ws, "codex", self.LANE_A), "pane_id": self.GATEWAY_A_LOCATOR},
            {"name": encode_assigned_name(ws, "codex", self.LANE_B), "pane_id": self.GATEWAY_B_LOCATOR},
        ]

    @staticmethod
    def _args(ctx, *, target=None, target_lane=None, to="codex"):
        ns = argparse.Namespace()
        ns.repo = str(ctx.repo)
        ns.to = to
        ns.target = target
        ns.target_lane = target_lane
        return ns

    def _ctx(self, tmp):
        # Sender = the parent coordinator (codex, default lane), divergent from the gateways.
        return _Ctx(tmp, sender_role="codex", sender_lane="default", rows=self._rows)

    def _resolve(self, ctx, args):
        with patch("subprocess.run", ctx.run), patch.dict(
            os.environ, ctx.env(), clear=True
        ):
            return _resolve_from_args(args, receiver=args.to)

    def test_explicit_gateway_a_locator_without_lane_mismatch_fails_closed(self) -> None:
        # The exact #13882 j#79958 repro: coordinator passes the lane-A gateway's live
        # locator as --target but no --target-lane. The route authority derives the sender's
        # own default lane and resolves the coordinator's OWN pane — a MISMATCH with the
        # named target, so the rail fails closed (the raise is the proof of no sender echo /
        # no false-positive sent; the send branch converts it to a zero-send blocked outcome).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, self._args(ctx, target=self.GATEWAY_A_LOCATOR, to="codex"))
        self.assertEqual(c.exception.reason, "explicit_target_mismatch")
        msg = str(c.exception)
        # The typed refusal names the derived (wrong) target, the sender-echo cause, and the
        # sanctioned selector so the coordinator can re-issue correctly.
        self.assertIn(self.COORDINATOR_LOCATOR, msg)  # the derived (wrong) agent, surfaced
        self.assertIn("--target-lane", msg)
        self.assertIn("false-positive", msg)

    def test_explicit_gateway_b_locator_without_lane_mismatch_fails_closed(self) -> None:
        # The second gateway (#13883 j#79959) — same behavior, proving it is not a single
        # lane-specific state (the ticket reproduced across two targets).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, self._args(ctx, target=self.GATEWAY_B_LOCATOR, to="codex"))
        self.assertEqual(c.exception.reason, "explicit_target_mismatch")

    def test_session_window_style_target_mismatch_fails_closed(self) -> None:
        # A ``session:window`` string names no live herdr agent, so it can never agree with
        # the resolved locator/name — it fails closed rather than silently routing to the
        # sender's own lane.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            with self.assertRaises(HerdrSendEntryError) as c:
                self._resolve(ctx, self._args(ctx, target="cockpit:codex", to="codex"))
        self.assertEqual(c.exception.reason, "explicit_target_mismatch")

    def test_explicit_target_consistent_with_target_lane_resolves_exact(self) -> None:
        # Resolve-to-exact: --target <gateway-A locator> PAIRED with --target-lane LANE_A.
        # The pin (not the locator) resolves the stable slot, the resolved locator equals the
        # named --target, so the consistency check passes and the send lands on gateway A —
        # the #13485/#13488 lane-pinned-dispatch shape, unaffected by the new guard.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            pane = self._resolve(
                ctx,
                self._args(ctx, target=self.GATEWAY_A_LOCATOR, target_lane=self.LANE_A, to="codex"),
            )
        self.assertEqual(pane["id"], self.GATEWAY_A_LOCATOR)
        self.assertEqual(pane["lane_id"], self.LANE_A)
        self.assertNotEqual(pane["id"], self.COORDINATOR_LOCATOR)

    def test_target_lane_selector_resolves_exact_gateway(self) -> None:
        # The sanctioned explicit selector alone (no --target): --target-lane resolves to the
        # intended gateway EXACTLY, never the coordinator's own default-lane pane. No explicit
        # --target means no consistency check to run.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            pane_a = self._resolve(ctx, self._args(ctx, target_lane=self.LANE_A, to="codex"))
            pane_b = self._resolve(ctx, self._args(ctx, target_lane=self.LANE_B, to="codex"))
        self.assertEqual(pane_a["id"], self.GATEWAY_A_LOCATOR)
        self.assertEqual(pane_a["lane_id"], self.LANE_A)
        self.assertEqual(pane_b["id"], self.GATEWAY_B_LOCATOR)
        self.assertEqual(pane_b["lane_id"], self.LANE_B)
        self.assertNotEqual(pane_a["id"], self.COORDINATOR_LOCATOR)
        self.assertNotEqual(pane_b["id"], self.COORDINATOR_LOCATOR)

    def test_coordinator_pseudo_target_still_routes_default_lane(self) -> None:
        # Regression guard: the `coordinator` pseudo-target is still honored (the new
        # consistency check excludes it — it is a route-string, not a locator). A sublane
        # sender addressing `coordinator` resolves the workspace default-lane parent.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(tmp, sender_role="codex", sender_lane=self.LANE_A, rows=self._rows)
            pane = self._resolve(ctx, self._args(ctx, target="coordinator", to="codex"))
        self.assertEqual(pane["id"], self.COORDINATOR_LOCATOR)
        self.assertEqual(pane["lane_id"], "default")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
