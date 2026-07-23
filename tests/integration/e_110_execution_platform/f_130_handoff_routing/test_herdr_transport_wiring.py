"""Pure-herdr end-to-end handoff wiring for ``orchestrate_handoff`` (Redmine #13261 / #13255).

Increment 2 (#13261) proved a full ``mozyo-bridge handoff send`` with **no tmux
available**: the target is resolved herdr-natively at the orchestrate entry
(launch-time sender identity + live ``agent list`` inventory) and the outcome is
``sent`` — all without patching the tmux pane resolver or ``wait_for_text``.

Redmine #13255 promotes the ``--mode standard`` rail under the herdr backend from
the capture-based ``_observe_standard_turn_start`` to the event-driven
``HerdrTurnStartRail``. The fake herdr no longer models turn-start via an
``agent read`` capture-diff; it fakes the ``wait agent-status <target> --status
working --timeout <ms>`` event (spawned via ``subprocess.Popen``) and the
``agent get`` state snapshot. These tests prove, for herdr+standard:
``_observe_standard_turn_start`` is NOT called; the body is ``send_text`` exactly
once; an Enter resend never re-injects the body; each of the 6 rail outcomes lands
on the correct ``(status, reason)`` with the additive ``turn_start_outcome`` +
telemetry; a not-idle pre-snapshot refuses to inject; and the queue-enter rail is
unchanged (no rail, byte-compatible ``sent``).

Also covers the fail-closed branches (un-attested sender env, no live target agent)
and confirms the tmux backend resolves to no binding (byte-identical tmux path).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)


#: What a live, started-up agent TUI actually renders on an idle pane — a framed
#: composer, never a blank buffer (Redmine #13760). The pre-send startup-admission gate
#: reads THIS, so the fixture has to be realistic: modelling `agent read` as an empty
#: string would have let a blank pane stand in for a ready one, which is the exact
#: false-ready confusion the gate exists to remove.
IDLE_COMPOSER = (
    "╭────────────────────────────────────────────────╮\n"
    '│ > Try "fix the failing handoff test"           │\n'
    "╰────────────────────────────────────────────────╯\n"
    "  ? for shortcuts"
)

#: The Claude workspace-trust confirmation, as a real TUI renders it: framed, and
#: hard-wrapped mid-token at the pane width. This is the screen #13760 was raised on
#: (#13582 j#77917) — a live, non-blank, "ready-looking" pane with no composer, whose
#: default answer a stray Enter accepts.
TRUST_SCREEN = (
    "╭────────────────────────────────────────────────╮\n"
    "│ Accessing workspace:                           │\n"
    "│ /w/lane                                        │\n"
    "│ Quick safety check: Is this a project you cr   │\n"
    "│ eated or one you trust? (Like your own code)   │\n"
    "│ Claude Code'll be able to read, edit, and ex   │\n"
    "│ ecute files here.                              │\n"
    "│ ❯ 1. Yes, proceed                              │\n"
    "│   2. No, exit                                  │\n"
    "╰────────────────────────────────────────────────╯"
)


class _FakeWaitProc:
    """A fake ``wait agent-status`` subprocess (herdr event wait), classified on exit."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def kill(self):
        pass


class _FakeHerdr:
    """A fake herdr CLI keyed on argv (Redmine #13255 event-driven turn-start rail).

    Models the surfaces the herdr+standard rail drives: ``agent get`` (the
    pre-injection state snapshot + the timeout re-snapshot), ``pane send-text`` /
    ``pane send-keys`` (inject), ``agent read`` (the Enter-resend composer check),
    ``agent list`` (inventory), and ``wait agent-status`` (the event wait, spawned
    via ``subprocess.Popen``). Turn-start is the ``wait`` result, NOT an ``agent
    read`` capture-diff.

    ``get_states`` is consumed per ``agent get`` (first = pre-snapshot, later = the
    timeout re-snapshot). ``wait_results`` is a list of ``(returncode, stderr)``
    consumed per armed wait (exit 0 = changed/started; ``"timed out"`` = timeout;
    ``"no such pane"`` = absent). ``read_returns_body`` makes ``agent read`` echo the
    injected body so the Enter-resend composer gate passes (default: empty, so no
    resend). ``fail_send_text`` / ``fail_send_keys`` force a transport inject failure.
    """

    def __init__(
        self,
        agent_rows,
        *,
        get_states=None,
        wait_results=None,
        read_returns_body=False,
        fail_send_text=False,
        fail_send_keys=False,
        pane_content=None,
        fail_read=False,
    ):
        self.agent_rows = agent_rows
        self.sends: list = []
        self._last_body_by_target: dict = {}
        self._get_states = list(get_states) if get_states is not None else ["idle"]
        self._get_calls = 0
        self._wait_results = list(wait_results) if wait_results is not None else [(0, "")]
        self._wait_calls = 0
        self._read_returns_body = read_returns_body
        self._fail_send_text = fail_send_text
        self._fail_send_keys = fail_send_keys
        # Redmine #13760: what the receiver's pane is rendering. Defaults to a live,
        # started-up idle composer (what every pre-#13760 test implicitly assumed);
        # a test sets `pane_content=TRUST_SCREEN` to put the receiver on a startup
        # screen, or `fail_read=True` to make the visible read fail outright.
        self._pane_content = IDLE_COMPOSER if pane_content is None else pane_content
        self._fail_read = fail_read

    def run(self, argv, capture_output=None, text=None, timeout=None, **kw):
        rest = list(argv[1:])
        # `herdr_workspace_segment` probes git topology (#13331). These repos are plain
        # (non-git) temp dirs, so the probe must read "not a git checkout" -> standalone ->
        # registry workspace_id (patch.dict replaced the real subprocess.run).
        if list(argv[:1]) == ["git"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a git repo")
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.agent_rows}), stderr=""
            )
        if rest[:2] == ["agent", "get"]:
            target = rest[2]
            state = (
                self._get_states[self._get_calls]
                if self._get_calls < len(self._get_states)
                else self._get_states[-1]
            )
            self._get_calls += 1
            self.sends.append(("get", target, state))
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"status": state}), stderr=""
            )
        if rest[:2] == ["pane", "send-text"]:
            target, body = rest[2], rest[3] if len(rest) > 3 else ""
            self.sends.append(("send_text", target, body))
            self._last_body_by_target[target] = body
            rc = 1 if self._fail_send_text else 0
            return subprocess.CompletedProcess(
                argv, rc, stdout="", stderr="send-text failed" if rc else ""
            )
        if rest[:2] == ["pane", "send-keys"]:
            keys = rest[3] if len(rest) > 3 else ""
            self.sends.append(("send_keys", rest[2], keys))
            rc = 1 if self._fail_send_keys else 0
            return subprocess.CompletedProcess(
                argv, rc, stdout="", stderr="send-keys failed" if rc else ""
            )
        if rest[:2] == ["agent", "read"]:
            target = rest[2]
            self.sends.append(("read", target))
            if self._fail_read:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="pane read failed"
                )
            # The rendered pane: whatever the receiver is showing, plus the injected
            # body when the composer is holding it (the Enter-resend gate's signature).
            body = (
                self._last_body_by_target.get(target, "")
                if self._read_returns_body
                else ""
            )
            content = f"{self._pane_content}\n{body}" if body else self._pane_content
            return subprocess.CompletedProcess(argv, 0, stdout=content, stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv!r}")

    def popen(self, argv, stdout=None, stderr=None, text=None, **kw):
        rest = list(argv[1:])
        if rest[:2] == ["wait", "agent-status"]:
            target = rest[2]
            self.sends.append(("wait", target))
            rc, err = (
                self._wait_results[self._wait_calls]
                if self._wait_calls < len(self._wait_results)
                else self._wait_results[-1]
            )
            self._wait_calls += 1
            return _FakeWaitProc(returncode=rc, stderr=err)
        raise AssertionError(f"unexpected popen call: {argv!r}")


def _outcome_from(stdout: str):
    outcome = None
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                outcome = json.loads(line)
            except json.JSONDecodeError:
                pass
    return outcome


def _same_lane_rows(target_locator: str = "wT:pT"):
    """A same-lane (lane-1) sender+claude pair so the gateway-route gate admits the send."""

    def rows(ws):
        return [
            {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
            {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": target_locator},
        ]

    return rows


class PureHerdrEndToEndTest(unittest.TestCase):
    def _run(
        self,
        *,
        agent_rows_fn,
        set_sender_env=True,
        mode="standard",
        tmux_pane=None,
        herdr=None,
        observe_spy=False,
        submit_delay="0",
        extra_argv=None,
    ):
        from mozyo_bridge.application import commands  # noqa: F401 (import side effects)
        from mozyo_bridge.application.cli import build_parser

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            register_workspace(repo, home=home)
            workspace_id = read_anchor(repo)["workspace_id"]
            if herdr is None:
                herdr = _FakeHerdr(agent_rows_fn(workspace_id))
            else:
                herdr.agent_rows = agent_rows_fn(workspace_id)

            herdr_bin = repo / "fake-herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(
                herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )

            argv = [
                "handoff", "send", "--to", "claude",
                "--source", "asana", "--kind", "implementation_request",
                "--task-id", "T1", "--comment-id", "C1",
                "--mode", mode,
                "--landing-timeout", "0.05", "--submit-delay", submit_delay,
                *(extra_argv or []),
            ]
            args = build_parser().parse_args(argv)
            args.repo = str(repo)

            # Simulate a pure herdr session: no tmux server. TMUX_PANE is unset by
            # default; a test may set it (``tmux_pane``) to prove the send makes ZERO
            # tmux calls even when a stale TMUX_PANE is present (the fake herdr runner
            # raises on any non-herdr argv, so a tmux `list-panes` would blow up).
            env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
            env["MOZYO_REPO"] = str(repo)
            env["MOZYO_BRIDGE_HOME"] = str(home)
            if tmux_pane is not None:
                env["TMUX_PANE"] = tmux_pane
            if set_sender_env:
                env["MOZYO_WORKSPACE_ID"] = workspace_id
                env["MOZYO_AGENT_ROLE"] = "codex"
                env["MOZYO_LANE_ID"] = "lane-1"

            observe_mock = mock.MagicMock(side_effect=AssertionError(
                "_observe_standard_turn_start must not run on the herdr+standard rail"
            ))
            ctx = [
                patch("subprocess.run", herdr.run),
                # The event wait spawns via subprocess.Popen (not run); fake it too.
                patch("subprocess.Popen", herdr.popen),
                patch("mozyo_bridge.application.commands.time.sleep"),
                patch.dict(os.environ, env, clear=True),
            ]
            if observe_spy:
                ctx.append(
                    patch(
                        "mozyo_bridge.application.commands._observe_standard_turn_start",
                        observe_mock,
                    )
                )
            with contextlib.ExitStack() as stack:
                for cm in ctx:
                    stack.enter_context(cm)
                out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                try:
                    result = args.func(args)
                except BaseException as exc:  # noqa: BLE001
                    result = exc
            return result, herdr, workspace_id, out.getvalue(), err.getvalue()

    def test_send_resolves_target_and_marker_lands_no_tmux(self) -> None:
        target_locator = "wT:pT"
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])

        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(target_locator), herdr=herdr, observe_spy=True
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        # Delivery + wait + state read all hit the herdr-resolved target locator
        # (never the sender): the rail owns injection through the herdr port.
        touched = {op[1] for op in herdr.sends}
        self.assertEqual(touched, {target_locator})
        # The event-driven rail armed a wait and drove the body through send_text.
        self.assertTrue([op for op in herdr.sends if op[0] == "wait"])
        self.assertTrue([op for op in herdr.sends if op[0] == "get"])
        # The body is injected exactly once (the rail types marker+body a single time).
        send_texts = [op for op in herdr.sends if op[0] == "send_text"]
        self.assertEqual(len(send_texts), 1, msg=herdr.sends)
        # Machine-readable + human-readable turn-start telemetry on the record.
        self.assertIn("outcome started", out)
        self.assertIn("snapshot awaiting_input", out)
        self.assertIn("wait changed", out)

    def test_cross_lane_worker_fails_closed_at_resolution_no_tmux(self) -> None:
        # Redmine #13305: the route-authority convergence makes the herdr send path
        # lane-in-match. A governed implementation_request `--to claude` to a worker in
        # a DIFFERENT lane than the env-derived sender (lane-1) now fails closed at
        # TARGET RESOLUTION — the derived lane-1 slot is not live and the authority does
        # NOT scan all lanes to find the lane-x worker — so it never reaches the
        # gateway-route gate. The same-lane invariant is enforced upstream (the gate
        # stays byte-intact for tmux). Still no tmux call (TMUX_PANE is set; a
        # `list-panes` fallback would make the fake herdr runner raise), still no send.
        def rows(ws):
            return [
                {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                {"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": "wT:pT"},
            ]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows, tmux_pane="%99")
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("reason"), "target_unavailable")
        # Fail-closed before any send.
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        )

    def test_gateway_gate_same_lane_worker_passes_with_tmux_pane_set(self) -> None:
        # Same-lane worker send with a stale TMUX_PANE present: the gate resolves the
        # sender lane from env (lane-1 == target lane-1) and allows the send — proving
        # the herdr path never reads tmux even when TMUX_PANE is set.
        target_locator = "wT:pT"

        def rows(ws):
            return [
                {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": target_locator},
            ]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows, tmux_pane="%99")
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        self.assertEqual(_outcome_from(out).get("status"), "sent", msg=out)

    def test_missing_sender_env_fails_closed(self) -> None:
        def rows(ws):
            return [{"name": encode_assigned_name(ws, "claude", "lane-x"), "pane_id": "wT:pT"}]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows, set_sender_env=False)
        self.assertNotEqual(result, 0)
        outcome = _outcome_from(out)
        self.assertIsNotNone(outcome, msg=out)
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("reason"), "target_unavailable")
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        )

    def test_no_target_agent_fails_closed(self) -> None:
        def rows(ws):
            # Only the sender (codex) exists; --to claude has no live agent.
            return [{"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"}]

        result, herdr, ws, out, err = self._run(agent_rows_fn=rows)
        self.assertNotEqual(result, 0)
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("reason"), "target_unavailable")
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        )

    # --- Redmine #13255: event-driven turn-start rail on the herdr+standard rail ---

    def test_precondition_not_idle_refuses_injection(self) -> None:
        # A pre-injection snapshot that is NOT idle (receiver busy) makes the rail
        # refuse to inject — no body, no Enter, no wait — and fail closed to a
        # `blocked` / `precondition_not_idle` outcome.
        herdr = _FakeHerdr([], get_states=["working"])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "precondition_not_idle", msg=out)
        # No body / Enter / wait: the rail refused before injecting.
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys", "wait")],
            msg=herdr.sends,
        )
        self.assertIn("outcome precondition_not_idle", out)

    def test_started_projects_to_sent_ok(self) -> None:
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)

    def test_done_turn_ended_injects_and_projects_to_sent_ok(self) -> None:
        # Redmine #13319: herdr holds `done` until the next input, so a follow-up
        # send to an agent that just finished its turn used to fail closed with
        # `precondition_not_idle`. `turn_ended` is now an injectable pre-injection
        # state (design j#73077): the rail injects (body once + Enter + wait) and
        # the observed `working` transition projects to `sent` / `ok`. The snapshot
        # is carried as `turn_ended`, never collapsed to `awaiting_input`.
        herdr = _FakeHerdr([], get_states=["done"], wait_results=[(0, "")])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        # It really injected from the `done` snapshot: body typed once + Enter sent.
        self.assertEqual(
            len([op for op in herdr.sends if op[0] == "send_text"]), 1, msg=herdr.sends
        )
        self.assertTrue(
            [op for op in herdr.sends if op[0] == "send_keys"], msg=herdr.sends
        )
        self.assertIn("snapshot turn_ended", out)

    def test_standard_infinite_submit_delay_still_sends_on_the_herdr_rail(self) -> None:
        # #14219 j#86693 R22-F1 / j#86698 R23-F1: the herdr standard rail has no submit-delay
        # field, so the executable-domain rule must NOT fire for it — the event rail delivers,
        # body typed once and Enter sent, the delay unconsumed.
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, submit_delay="inf"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(
            len([op for op in herdr.sends if op[0] == "send_text"]), 1, msg=herdr.sends
        )
        self.assertTrue(
            [op for op in herdr.sends if op[0] == "send_keys"], msg=herdr.sends
        )

    def test_an_explicit_pane_target_leaves_the_herdr_backend(self) -> None:
        # The SAME herdr workspace with an explicit %pane target falls out of the herdr
        # backend selection (#13320) toward the tmux rail: the herdr event rail is never
        # driven (this pure-herdr session has no tmux, so the send fails on the tmux
        # requirement with zero herdr injections — the routing boundary this pins).
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, submit_delay="inf",
            extra_argv=["--target", "%2"],
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        self.assertEqual(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys")],
            [],
            msg=herdr.sends,
        )

    def test_delivered_not_started_projects_to_turn_start_unconfirmed(self) -> None:
        # Wait times out; re-snapshot stays idle (no runtime block); the composer
        # read does not retain the body so no Enter-resend fires → delivered but not
        # started, reusing the existing `turn_start_unconfirmed` reason with the
        # additive `turn_start_outcome=delivered_not_started` telemetry.
        herdr = _FakeHerdr(
            [], get_states=["idle", "idle"], wait_results=[(1, "timed out")]
        )
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "turn_start_unconfirmed", msg=out)
        self.assertIn("outcome delivered_not_started", out)
        # Redmine #13255 j#72695: the machine-readable telemetry is a structured
        # field on the JSON outcome (not just the human record line), so the future
        # #12656 ledger reads it and does not have to parse prose.
        ts = outcome.get("turn_start_outcome")
        self.assertIsInstance(ts, dict, msg=out)
        self.assertEqual(ts.get("outcome"), "delivered_not_started", msg=out)
        self.assertEqual(ts.get("snapshot_state"), "awaiting_input", msg=out)
        self.assertEqual(ts.get("wait_kind"), "timeout", msg=out)
        # Redmine #13255 j#72695: the record wording must describe a herdr event-wait
        # timeout, NOT the tmux/capture standard rail's landing-marker observation.
        self.assertIn("event wait", out)
        self.assertIn("wait agent-status", out)
        self.assertNotIn("Landing marker was observed and Enter was pressed", out)
        self.assertNotIn("tmux capture", out)

    def test_blocked_projects_to_receiver_blocked(self) -> None:
        # Wait times out; the re-snapshot finds a runtime block → `receiver_blocked`.
        herdr = _FakeHerdr(
            [], get_states=["idle", "blocked"], wait_results=[(1, "timed out")]
        )
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "receiver_blocked", msg=out)
        self.assertIn("outcome blocked", out)
        self.assertIn("re-snapshot found block", out)
        # Structured telemetry on the JSON outcome (reclassified block).
        ts = outcome.get("turn_start_outcome")
        self.assertIsInstance(ts, dict, msg=out)
        self.assertEqual(ts.get("outcome"), "blocked", msg=out)
        self.assertTrue(ts.get("reclassified_blocked"), msg=out)
        # Redmine #13255 j#72695 (same-shaped-branch audit): the rail can bounded-
        # resend Enter before the re-snapshot, so the record must not claim
        # "no re-send were issued".
        self.assertNotIn("no re-send were issued", out)

    def test_absent_projects_to_turn_start_absent(self) -> None:
        # The wait reports the target pane does not exist → `turn_start_absent`.
        herdr = _FakeHerdr(
            [], get_states=["idle"], wait_results=[(1, "no such pane")]
        )
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "turn_start_absent", msg=out)
        self.assertIn("outcome absent", out)

    def test_inject_failed_projects_to_inject_failed(self) -> None:
        # A send_text transport failure mid-injection → `inject_failed` (the armed
        # wait is cancelled; nothing confirmed delivered).
        herdr = _FakeHerdr([], get_states=["idle"], fail_send_text=True)
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "inject_failed", msg=out)
        self.assertIn("outcome inject_failed", out)

    def test_enter_resend_does_not_reinject_body(self) -> None:
        # First wait times out, the composer still holds the body (read echoes it),
        # so the rail re-sends Enter ONCE and re-arms; the second wait sees the
        # transition → started. The body must be injected exactly once (only Enter
        # is re-sent), and the telemetry records the single re-send.
        herdr = _FakeHerdr(
            [],
            get_states=["idle", "idle"],
            wait_results=[(1, "timed out"), (0, "")],
            read_returns_body=True,
        )
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        send_texts = [op for op in herdr.sends if op[0] == "send_text"]
        enters = [op for op in herdr.sends if op[0] == "send_keys" and op[2].lower() == "enter"]
        self.assertEqual(len(send_texts), 1, msg=f"body re-injected: {herdr.sends}")
        self.assertEqual(len(enters), 2, msg=f"expected one Enter re-send: {herdr.sends}")
        self.assertIn("1 Enter re-send(s)", out)

    def test_queue_enter_rail_choreography_unchanged_under_herdr(self) -> None:
        # Redmine #13255 decision 5 / #13292: the queue-enter rail is NOT promoted to
        # the event-driven rail — it NEVER arms a `wait` and its inject -> Enter ->
        # Enter-only retry choreography is byte-unchanged, resolving to `sent`. Redmine
        # #13292 adds ONLY an additive, telemetry-only post-choreography `agent get`
        # snapshot: `get` may now appear, but `wait` must NOT, and the wire is unchanged.
        herdr = _FakeHerdr([], get_states=["working"])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        # The queue-enter rail never arms an event wait (only the standard rail does).
        self.assertFalse([op for op in herdr.sends if op[0] == "wait"], msg=herdr.sends)
        # The body is injected exactly once (the additive snapshot never re-injects).
        send_texts = [op for op in herdr.sends if op[0] == "send_text"]
        self.assertEqual(len(send_texts), 1, msg=herdr.sends)
        # The event rail's `turn_start_outcome` telemetry stays absent on queue-enter.
        self.assertIsNone(outcome.get("turn_start_outcome"), msg=out)

    def test_queue_enter_snapshot_records_telemetry_only(self) -> None:
        # Redmine #13292 (design j#72759): a herdr queue-enter send takes a read-only
        # post-choreography `agent get` snapshot and records it as the additive,
        # telemetry-only `queue_enter_turn_start_observation` field. A `working`
        # snapshot is a settled state (single read), but it NEVER changes the
        # `sent` wire and never blocks.
        herdr = _FakeHerdr([], get_states=["working"])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        # The snapshot read hit `agent get` at least once.
        self.assertTrue([op for op in herdr.sends if op[0] == "get"], msg=herdr.sends)
        obs = outcome.get("queue_enter_turn_start_observation")
        self.assertIsInstance(obs, dict, msg=out)
        self.assertEqual(obs.get("observation_kind"), "post_choreography_snapshot", msg=out)
        self.assertEqual(obs.get("source"), "herdr_agent_get", msg=out)
        self.assertEqual(obs.get("runtime_state"), "busy", msg=out)
        self.assertTrue(obs.get("read_ok"), msg=out)
        self.assertIsNone(obs.get("read_reason"), msg=out)
        self.assertEqual(obs.get("poll_attempts"), 1, msg=out)
        # The record line labels itself telemetry-only and does NOT reuse the event
        # rail's wording.
        self.assertIn("Queue-enter turn-start observation (herdr agent get)", out)
        self.assertIn("Telemetry-only", out)
        self.assertNotIn("Turn start (herdr rail)", out)

    def test_queue_enter_snapshot_awaiting_input_is_advisory_not_block(self) -> None:
        # Redmine #13292: an idle (awaiting_input) receiver after the choreography is
        # "delivered but no turn observed starting" — recorded as telemetry ONLY. It
        # MUST NOT hard-block: the `sent` contract (hard-block forbidden, #13262
        # j#72523) is preserved.
        herdr = _FakeHerdr([], get_states=["idle"])
        result, herdr, ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertIn(outcome.get("reason"), ("ok", "queue_enter"), msg=out)
        self.assertEqual(outcome.get("next_action_owner"), "receiver", msg=out)
        obs = outcome.get("queue_enter_turn_start_observation")
        self.assertIsInstance(obs, dict, msg=out)
        self.assertEqual(obs.get("runtime_state"), "awaiting_input", msg=out)
        self.assertTrue(obs.get("read_ok"), msg=out)


class HerdrLedgerSendSiteWiringTest(unittest.TestCase):
    """Redmine #13300: each herdr send-site of ``orchestrate_handoff`` appends to the
    #13296 herdr delivery ledger.

    Proves the live wiring the #13296 j#72869/j#72883/j#72893 follow-up asked for:
    the event rail (#13255) and the queue-enter rail (#13292) each emit a ledger row
    per send, the emission records even a non-``sent`` (delivered-not-started) outcome
    before the send fails, a ledger store failure is swallowed (never fails the send),
    and the wire outcome is byte-unchanged (ACK semantics untouched). The runner sets
    ``MOZYO_BRIDGE_HOME`` to a temp home so the ledger writes and is read back inside
    the temp dir's lifetime.
    """

    def _run_and_ledger(self, *, herdr, mode="standard", sabotage_ledger=False):
        from mozyo_bridge.application.cli import build_parser
        from mozyo_bridge.core.state.herdr_delivery_ledger import (
            HERDR_DELIVERY_LEDGER_FILENAME,
            HerdrDeliveryLedger,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            register_workspace(repo, home=home)
            workspace_id = read_anchor(repo)["workspace_id"]
            herdr.agent_rows = _same_lane_rows()(workspace_id)

            herdr_bin = repo / "fake-herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(
                herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )

            if sabotage_ledger:
                # Make the ledger path a directory so sqlite `connect` fails on
                # append: exercises the real best-effort swallow at the boundary.
                (home / HERDR_DELIVERY_LEDGER_FILENAME).mkdir()

            argv = [
                "handoff", "send", "--to", "claude",
                "--source", "asana", "--kind", "implementation_request",
                "--task-id", "T1", "--comment-id", "C1",
                "--mode", mode,
                "--landing-timeout", "0.05", "--submit-delay", "0",
            ]
            args = build_parser().parse_args(argv)
            args.repo = str(repo)

            env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
            env["MOZYO_REPO"] = str(repo)
            env["MOZYO_BRIDGE_HOME"] = str(home)
            env["MOZYO_WORKSPACE_ID"] = workspace_id
            env["MOZYO_AGENT_ROLE"] = "codex"
            env["MOZYO_LANE_ID"] = "lane-1"

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("subprocess.run", herdr.run))
                stack.enter_context(patch("subprocess.Popen", herdr.popen))
                stack.enter_context(patch("mozyo_bridge.application.commands.time.sleep"))
                stack.enter_context(patch.dict(os.environ, env, clear=True))
                out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                try:
                    result = args.func(args)
                except BaseException as exc:  # noqa: BLE001
                    result = exc

            # Read the ledger while the temp home is still alive, with `home` passed
            # explicitly (no env dependency).
            records = HerdrDeliveryLedger(home=home).recent()
            return result, _outcome_from(out.getvalue()), records, out.getvalue()

    def test_event_rail_sent_appends_one_delivery_outcome(self) -> None:
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, outcome, records, out = self._run_and_ledger(herdr=herdr)
        self.assertEqual(result, 0, msg=out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(len(records), 1, msg=records)
        rec = records[0]
        self.assertEqual(rec.entry_kind, "delivery_outcome")
        self.assertEqual(rec.rail, "event_rail")
        self.assertEqual(rec.backend, "herdr")
        self.assertEqual(rec.status, "sent")
        self.assertEqual(rec.reason, "ok")
        self.assertEqual(rec.receiver, "claude")
        # The ledger correlates the row with the send via the notification marker.
        self.assertEqual(rec.notification_marker, outcome.get("notification_marker"))
        # The event rail carries `turn_start_outcome` telemetry; the queue-enter
        # observation stays absent.
        self.assertIsInstance(rec.turn_start_outcome, dict)
        self.assertIsNone(rec.queue_enter_observation)

    def test_event_rail_records_delivered_not_started_before_send_fails(self) -> None:
        # A delivered-not-started outcome dies (nonzero), but the ledger must have
        # already recorded it: "毎 send で ledger record を emit" includes non-`sent`.
        herdr = _FakeHerdr(
            [], get_states=["idle", "idle"], wait_results=[(1, "timed out")]
        )
        result, outcome, records, out = self._run_and_ledger(herdr=herdr)
        self.assertNotEqual(result, 0, msg=out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "turn_start_unconfirmed", msg=out)
        self.assertEqual(len(records), 1, msg=records)
        rec = records[0]
        self.assertEqual(rec.rail, "event_rail")
        self.assertEqual(rec.status, "blocked")
        self.assertEqual(rec.reason, "turn_start_unconfirmed")
        self.assertEqual(
            (rec.turn_start_outcome or {}).get("outcome"), "delivered_not_started"
        )

    def test_queue_enter_rail_appends_with_observation(self) -> None:
        herdr = _FakeHerdr([], get_states=["working"])
        result, outcome, records, out = self._run_and_ledger(
            herdr=herdr, mode="queue-enter"
        )
        self.assertEqual(result, 0, msg=out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(len(records), 1, msg=records)
        rec = records[0]
        self.assertEqual(rec.entry_kind, "delivery_outcome")
        self.assertEqual(rec.rail, "queue_enter_rail")
        self.assertEqual(rec.backend, "herdr")
        self.assertEqual(rec.status, "sent")
        # The queue-enter rail carries the additive post-choreography observation;
        # the event rail's `turn_start_outcome` stays absent.
        self.assertIsInstance(rec.queue_enter_observation, dict)
        self.assertEqual(
            rec.queue_enter_observation.get("observation_kind"),
            "post_choreography_snapshot",
        )
        self.assertIsNone(rec.turn_start_outcome)

    def test_ledger_store_failure_does_not_fail_the_send(self) -> None:
        # A broken ledger store (path is a directory → sqlite connect fails) is
        # swallowed by the best-effort boundary: the send still resolves `sent`/0 and
        # the wire outcome is unchanged. No row is readable (read also degrades).
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, outcome, records, out = self._run_and_ledger(
            herdr=herdr, sabotage_ledger=True
        )
        self.assertEqual(result, 0, msg=out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        self.assertEqual(records, [], msg=records)


class TmuxBackendUntouchedTest(unittest.TestCase):
    """backend=tmux (and absent config) resolve to None — the shim installs nothing."""

    def _binding_for(self, config_text):
        from mozyo_bridge.application.handoff_transport_wiring import (
            resolve_handoff_transport_binding,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".mozyo-bridge").mkdir()
            if config_text is not None:
                (repo / ".mozyo-bridge" / "config.yaml").write_text(
                    config_text, encoding="utf-8"
                )

            class _Args:
                pass

            args = _Args()
            args.repo = str(repo)
            args.to = "claude"
            return resolve_handoff_transport_binding(args)

    def test_explicit_tmux_backend_returns_none(self) -> None:
        self.assertIsNone(
            self._binding_for("version: 1\nterminal_transport:\n  backend: tmux\n")
        )

    def test_absent_config_returns_none(self) -> None:
        self.assertIsNone(self._binding_for(None))

    def test_herdr_backend_selected_predicate(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
            herdr_backend_selected,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )

            class _Args:
                pass

            args = _Args()
            args.repo = str(repo)
            self.assertTrue(herdr_backend_selected(args))


class ExplicitPaneTargetRoutesTmuxTest(unittest.TestCase):
    """Redmine #13320 (a-narrow, j#73114): under ``backend: herdr`` an explicit tmux
    ``%pane`` target installs NEITHER the herdr binding NOR the herdr turn-start rail —
    it rides the tmux rail. Both send-path branch points (the decorator's
    ``resolve_handoff_transport_runtime`` / the older
    ``resolve_handoff_transport_binding``) must return the tmux-default ``None`` for a
    ``%pane`` target, and must do so WITHOUT requiring a herdr binary / inventory (the
    narrowing short-circuits before ``_resolve_herdr_binding``)."""

    def _herdr_repo(self, tmp):
        repo = Path(tmp)
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
        )
        return repo

    @staticmethod
    def _args(repo, target):
        class _Args:
            pass

        args = _Args()
        args.repo = str(repo)
        args.to = "claude"
        args.target = target
        return args

    def test_explicit_pane_binding_is_none_no_binary_required(self) -> None:
        from mozyo_bridge.application.handoff_transport_wiring import (
            resolve_handoff_transport_binding,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._herdr_repo(tmp)
            # No MOZYO_HERDR_BINARY in env: an explicit `%pane` must not reach the
            # herdr binary resolution (would `die`), it returns the tmux default.
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(
                    resolve_handoff_transport_binding(self._args(repo, "%45"))
                )

    def test_explicit_pane_runtime_is_none_none_no_binary_required(self) -> None:
        from mozyo_bridge.application.handoff_transport_wiring import (
            resolve_handoff_transport_runtime,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._herdr_repo(tmp)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    resolve_handoff_transport_runtime(self._args(repo, "%45")),
                    (None, None),
                )

    def test_implicit_target_still_takes_herdr_path(self) -> None:
        # Contrast: a non-`%pane` (implicit `--to claude`) target under herdr config
        # DOES enter herdr resolution — with no binary configured it fails closed
        # (`die` -> SystemExit), proving the narrowing is target-kind-specific and not
        # a blanket tmux downgrade.
        from mozyo_bridge.application.handoff_transport_wiring import (
            resolve_handoff_transport_runtime,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._herdr_repo(tmp)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    resolve_handoff_transport_runtime(self._args(repo, None))


class HerdrEventRailForwardCompletionTest(unittest.TestCase):
    """The herdr event-driven rail must publish its terminal outcome (Redmine #13583 R3-F1).

    The event rail builds an outcome, emits it, and returns 0 on a `sent` projection. Before the
    R3-F1 fix it never published that outcome, so `delivery_was_positive(args)` was False on the
    NORMAL herdr route and a correlated forward generation could never complete — the caller could
    never forward again (fail-safe stuck). These drive a REAL `handoff ticketless-callback` over the
    event rail against a live forward store: the generation completes on an actual `sent`, and does
    NOT complete when the rail fails to confirm a turn start.
    """

    def _drive(self, *, wait_results, read_contract="grandparent_coordinator"):
        from mozyo_bridge.application.cli import build_parser
        from mozyo_bridge.core.state.forward_outbox_fence import (
            ForwardOutboxFence,
            ForwardRouteKey,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            register_workspace(repo, home=home)
            ws = read_anchor(repo)["workspace_id"]

            # A live forward store holding a DELIVERED generation awaiting its correlated callback.
            fence = ForwardOutboxFence(home=home)
            fence.bootstrap()
            route = ForwardRouteKey(ws, "default", read_contract, "project_gateway", "")
            minted = fence.reserve(route).action_id
            fence.mark_delivered(route, minted)

            herdr = _FakeHerdr(
                [
                    {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "wS:pS"},
                    {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": "wT:pT"},
                ],
                get_states=["idle"],
                wait_results=wait_results,
            )
            herdr_bin = repo / "fake-herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            argv = [
                "handoff", "ticketless-callback", "--to", "claude", "--target", "wT:pT",
                "--target-repo", str(repo),
                "--classification", "consultation_result",
                "--dispatch-decision", "no_dispatch",
                "--workflow-next-owner", "caller",
                "--callback-reason", "no_dispatch_decided",
                "--read-contract", read_contract,
                "--forward-action-id", minted,
                "--mode", "standard", "--landing-timeout", "0.05", "--submit-delay", "0",
            ]
            args = build_parser().parse_args(argv)
            args.repo = str(repo)

            env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
            env["MOZYO_REPO"] = str(repo)
            env["MOZYO_BRIDGE_HOME"] = str(home)
            env["MOZYO_WORKSPACE_ID"] = ws
            env["MOZYO_AGENT_ROLE"] = "codex"
            env["MOZYO_LANE_ID"] = "lane-1"

            with patch("subprocess.run", herdr.run), patch(
                "subprocess.Popen", herdr.popen
            ), patch("mozyo_bridge.application.commands.time.sleep"), patch.dict(
                os.environ, env, clear=True
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                try:
                    args.func(args)
                except BaseException:  # noqa: BLE001 - a non-sent rail dies; that IS the negative
                    pass
                published = getattr(args, "delivery_outcome", None)
                state = fence.active(route).state
            return published, state, minted

    def test_event_rail_sent_publishes_outcome_and_completes_generation(self):
        from mozyo_bridge.core.state.forward_outbox_fence import FORWARD_COMPLETED

        published, state, _minted = self._drive(wait_results=[(0, "")])
        # 1) the event rail PUBLISHES its terminal outcome (the R3-F1 gap).
        self.assertIsNotNone(published, "the event rail must publish its terminal outcome")
        self.assertEqual(published.status, "sent")
        self.assertEqual(published.reason, "ok")
        # 2) so the correlated generation actually completes on a real herdr delivery.
        self.assertEqual(state, FORWARD_COMPLETED)

    def test_event_rail_non_sent_does_not_complete_generation(self):
        from mozyo_bridge.core.state.forward_outbox_fence import FORWARD_DELIVERED

        # The rail never confirms a turn start -> non-`sent` projection -> zero completion.
        _published, state, _minted = self._drive(wait_results=[(1, "")])
        self.assertEqual(state, FORWARD_DELIVERED)


class StartupAdmissionZeroSendTest(unittest.TestCase):
    """The pre-send startup-admission gate at the shared herdr send boundary (#13760).

    The defect (#13582 j#77917): a fresh worktree's managed Claude worker was sitting on
    the trust confirmation. It is a live, non-blank pane, so readiness said ok, the
    queue-enter rail typed the Implementation Request into it, and the Enter was absorbed
    as the dialog's default Yes — the request body was destroyed while the transport
    recorded `sent / queue_enter`.

    Every test here asserts the property that was violated, not just the status token:
    on a startup screen, ZERO bytes reach the receiver (no `send_text`, no `send_keys`).
    """

    # Borrow the pure-herdr end-to-end harness (a real `handoff send` through the real
    # orchestrate path, faked only at the herdr CLI) without re-running its own tests.
    _run = PureHerdrEndToEndTest._run

    def _sent_ops(self, herdr):
        return [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]

    def test_trust_screen_queue_enter_is_zero_send(self) -> None:
        # The exact live shape of j#77917: `--mode queue-enter`, receiver on the trust
        # screen. Before #13760 this returned `sent / queue_enter` after typing the body.
        herdr = _FakeHerdr([], get_states=["idle"], pane_content=TRUST_SCREEN)
        result, herdr, _ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(
            outcome.get("reason"), "receiver_startup_interaction_required", msg=out
        )
        # The whole point: nothing was typed and Enter was never pressed.
        self.assertEqual(self._sent_ops(herdr), [], msg=herdr.sends)
        # The structured outcome names the provider and the screen — and nothing else.
        # The pane's text (which carries the workspace path the dialog is asking about)
        # must never reach a pasteable durable record.
        admission = outcome.get("startup_admission")
        self.assertEqual(admission.get("provider_id"), "claude", msg=out)
        self.assertEqual(
            admission.get("blocker_id"), "workspace_trust_confirmation", msg=out
        )
        self.assertNotIn("Quick safety check", out)
        self.assertNotIn("/w/lane", out)
        # The operator owns the next action: only a human clears a trust prompt.
        self.assertEqual(outcome.get("next_action_owner"), "operator", msg=out)

    def test_trust_screen_standard_mode_is_zero_send(self) -> None:
        # The gate is on the SHARED send boundary, so the event-driven standard rail is
        # refused before it ever arms a wait — not just the queue-enter rail.
        herdr = _FakeHerdr([], get_states=["idle"], pane_content=TRUST_SCREEN)
        result, herdr, _ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="standard"
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(
            outcome.get("reason"), "receiver_startup_interaction_required", msg=out
        )
        self.assertEqual(self._sent_ops(herdr), [], msg=herdr.sends)
        self.assertEqual([op for op in herdr.sends if op[0] == "wait"], [], msg=herdr.sends)

    def test_theme_and_login_screens_are_zero_send(self) -> None:
        # #13760 j#78082: the blockers are not trust-only. A fresh interactive Claude
        # startup stops at the theme picker, then at login — both render a live pane with
        # no composer, and both must refuse with their own fixed blocker id.
        screens = {
            "first_run_theme": (
                "Let's get started.\n\n"
                "Choose the text style that looks best with your terminal\n"
                "❯ 1. Dark mode"
            ),
            "login_required": (
                "Select login method:\n\n"
                "❯ 1. Claude account with subscription · Pro, Max\n"
                "  2. Anthropic Console account"
            ),
        }
        for blocker_id, screen in screens.items():
            with self.subTest(blocker=blocker_id):
                herdr = _FakeHerdr([], get_states=["idle"], pane_content=screen)
                result, herdr, _ws, out, err = self._run(
                    agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
                )
                self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
                outcome = _outcome_from(out)
                self.assertEqual(
                    outcome.get("reason"),
                    "receiver_startup_interaction_required",
                    msg=out,
                )
                self.assertEqual(
                    outcome["startup_admission"].get("blocker_id"), blocker_id, msg=out
                )
                self.assertEqual(self._sent_ops(herdr), [], msg=herdr.sends)

    def test_one_signature_alone_does_not_block_a_ready_composer(self) -> None:
        # The AND guard (j#77947 correction 1). A ready composer that merely CONTAINS one
        # of the trust screen's phrases — e.g. the previous turn's transcript quoting it —
        # is not a startup screen, and a gate that fired here would brick real dispatch.
        quoting_composer = (
            f"{IDLE_COMPOSER}\n"
            "  We fixed the pane that asked: Is this a project you created or one you "
            "trust?"
        )
        herdr = _FakeHerdr(
            [],
            get_states=["idle"],
            wait_results=[(0, "")],
            pane_content=quoting_composer,
        )
        result, herdr, _ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="standard"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(len(self._sent_ops(herdr)), 2, msg=herdr.sends)

    def test_admitted_idle_composer_is_byte_invariant(self) -> None:
        # A started-up receiver is unchanged by the gate: same outcome, same single
        # body injection, and no `startup_admission` field on the outcome at all
        # (j#77947 Q3 — the admitted queue-enter contract is byte-invariant).
        herdr = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, herdr, _ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "queue_enter", msg=out)
        self.assertIsNone(outcome.get("startup_admission"), msg=out)
        send_texts = [op for op in herdr.sends if op[0] == "send_text"]
        self.assertEqual(len(send_texts), 1, msg=herdr.sends)

    def test_unreadable_pane_is_zero_send_and_not_a_startup_blocker(self) -> None:
        # j#77947 invariant 4: an unreadable receiver must NOT decay to "startup clear"
        # (which would type into a pane we cannot see). It fails closed on the existing
        # transport-failure vocabulary — distinct from a matched blocker, so an auditor
        # never reads "we could not see the pane" as "the pane was fine".
        herdr = _FakeHerdr([], get_states=["idle"], fail_read=True)
        result, herdr, _ws, out, err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=herdr, mode="queue-enter"
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "target_unavailable", msg=out)
        self.assertEqual(
            outcome["startup_admission"].get("outcome"), "receiver_unreadable", msg=out
        )
        self.assertEqual(self._sent_ops(herdr), [], msg=herdr.sends)

    def test_cleared_blocker_delivers_the_same_anchor_exactly_once(self) -> None:
        # j#77947 invariant 5: a zero-send consumes no delivery. After the operator
        # clears the screen, re-issuing the SAME durable anchor through the SAME
        # high-level command delivers it — exactly once, with the body typed once.
        blocked = _FakeHerdr([], get_states=["idle"], pane_content=TRUST_SCREEN)
        result, blocked, _ws, out, _err = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=blocked, mode="queue-enter"
        )
        self.assertNotEqual(result, 0)
        self.assertEqual(self._sent_ops(blocked), [], msg=blocked.sends)

        cleared = _FakeHerdr([], get_states=["idle"], wait_results=[(0, "")])
        result, cleared, _ws, out2, err2 = self._run(
            agent_rows_fn=_same_lane_rows(), herdr=cleared, mode="queue-enter"
        )
        self.assertEqual(result, 0, msg=f"out={out2}\nerr={err2}")
        retry_outcome = _outcome_from(out2)
        self.assertEqual(retry_outcome.get("status"), "sent", msg=out2)
        send_texts = [op for op in cleared.sends if op[0] == "send_text"]
        self.assertEqual(len(send_texts), 1, msg=cleared.sends)
        # The same anchor and the same marker the refused attempt would have carried:
        # the recovery is a re-issue, not a new dispatch identity.
        self.assertEqual(retry_outcome["anchor"].get("task_id"), "T1", msg=out2)

    def test_codex_receiver_declares_no_blockers_and_is_admitted(self) -> None:
        # The gate is provider-neutral and DATA-driven: codex declares no startup
        # screens, so it is admitted even against a pane rendering Claude's trust text.
        # (Registering a guessed codex signature would either never fire or block a
        # ready pane; the profile stays empty until a real screen is observed.)
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
            evaluate_startup_admission,
        )

        admission = evaluate_startup_admission(
            provider_id="codex", read_visible=lambda: TRUST_SCREEN
        )
        self.assertTrue(admission.admitted)
        self.assertEqual(admission.blocker_id, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
