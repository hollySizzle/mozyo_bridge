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
    herdr_backend_selected,
    resolve_herdr_send_target,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


def _args(repo):
    ns = argparse.Namespace()
    ns.repo = str(repo)
    ns.to = "claude"
    return ns


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


class ResolveHerdrSendTargetTest(unittest.TestCase):
    def _resolve(self, ctx, *, with_sender=True, receiver="claude"):
        with patch("subprocess.run", ctx.run), patch.dict(
            os.environ, ctx.env(with_sender=with_sender), clear=True
        ):
            return resolve_herdr_send_target(_args(ctx.repo), receiver=receiver)

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
