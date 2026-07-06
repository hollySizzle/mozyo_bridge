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

    def run(self, argv, capture_output=None, text=None, timeout=None, **kw):
        rest = list(argv[1:])
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
            body = (
                self._last_body_by_target.get(target, "")
                if self._read_returns_body
                else ""
            )
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
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
                "--landing-timeout", "0.05", "--submit-delay", "0",
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
