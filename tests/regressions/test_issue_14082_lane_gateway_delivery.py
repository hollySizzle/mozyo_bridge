"""Redmine #14082: background-service same-lane gateway callback delivery correction.

The a18 live-dogfood failure (j#82511 / j#82517): a fresh ``lane_gateway:<lane>`` worker gate
resolved route / lane / receiver / generation / workspace correctly, yet delivered 0 — 3 attempts
all ``send_attempted=false`` known-not-sent, dead-lettered, coordinator direct wake 0. Two root
causes, independently verified from source and reproduced live (j#82530 / j#82537):

- **F1 (explicit target authority)** — on the herdr backend an explicit ``--target <locator>`` is NOT
  the route authority (only a ``%N`` tmux pane is); the background transport passed the re-resolved
  gateway locator but NO ``--target-lane``, so ``--to codex`` re-derived the lane from the (scrubbed /
  default) sender lane and landed on the coordinator/default pane.
- **F2 (system-actor sender identity)** — the background daemon scrubs the agent identity env, so the
  ``handoff send`` subprocess failed closed with ``missing_sender_env`` (emitted ``target_unavailable``)
  before it ever resolved a target.
- **F4 (diagnostics)** — the terminal outbox row dropped the first known-not-sent reason, flattening
  every dead-letter to "retries exhausted" so an authorization zero-send and a transport-precondition
  zero-send were indistinguishable.

The fix (design constraint j#82537 — no fake agent identity, no live-locator-as-sole-authority):

1. the transport pins the exact stable slot with ``--target-lane <target.lane>`` (tier-1 explicit);
2. a sanctioned ``background_service`` system-actor sender identity is admitted (origin-stamped +
   explicit target-lane) without a fake ``claude`` / ``codex`` identity — an env-less shell still
   fails closed;
3. the first zero-send reason is persisted secret-safe to the durable row, distinguishing an
   authorization zero-send from a transport-precondition one.
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
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.commands_common import repo_root_from_args
from mozyo_bridge.core.state.callback_outbox import (
    CallbackOutbox,
    CallbackOutboxKey,
    CallbackOutboxRow,
)
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_PENDING,
)
from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    BackendNeutralTargetResolver,
    BackgroundServiceCallbackSender,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
    _zero_send_detail,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_wiring import (
    SupervisedWorkspace,
    default_background_transport,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
    AUTH_GENERATION_MISMATCH,
    AUTH_NO_TARGET,
    BACKGROUND_SERVICE_ORIGIN,
    DeliveryTarget,
    TargetResolution,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    CallbackSendResult,
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    HerdrSendEntryError,
    resolve_herdr_send_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    AGENT_PROVIDERS,
    DELIVERY_ORIGIN_BACKGROUND_SERVICE,
    REASON_ENV_ANCHOR_WORKSPACE_MISMATCH,
    REASON_MISSING_ANCHOR,
    REASON_MISSING_SENDER_ENV,
    REASON_SYSTEM_ACTOR_MISSING_LANE,
    SYSTEM_ACTOR_ROLE_BACKGROUND_SERVICE,
    resolve_sender_identity,
)

NOW = "2026-07-19T00:00:00+00:00"
SUBLANE = "issue_14082_lane_gateway_delivery_r1"


# ---------------------------------------------------------------------------
# F2 — the sanctioned background_service system-actor sender identity (pure).
# ---------------------------------------------------------------------------
class SystemActorSenderIdentityTest(unittest.TestCase):
    """resolve_sender_identity admits a sanctioned background_service system actor, fail-closed."""

    def _env(self, **over) -> dict:
        env = {
            "MOZYO_WORKSPACE_ID": "wsA",
            "MOZYO_DELIVERY_ORIGIN": DELIVERY_ORIGIN_BACKGROUND_SERVICE,
        }
        env.update(over)
        return env

    def test_admits_system_actor_without_agent_role(self) -> None:
        # Origin-stamped + explicit target-lane + workspace==anchor: admitted with the sentinel role,
        # NO MOZYO_AGENT_ROLE present (scrubbed by design).
        res = resolve_sender_identity(
            self._env(), anchor_workspace_id="wsA", background_service_target_lane=SUBLANE
        )
        self.assertTrue(res.ok)
        self.assertIsNotNone(res.identity)
        self.assertEqual(res.identity.role, SYSTEM_ACTOR_ROLE_BACKGROUND_SERVICE)
        self.assertEqual(res.identity.workspace_id, "wsA")

    def test_missing_target_lane_fails_closed(self) -> None:
        # No explicit --target-lane: the exact stable slot cannot be pinned, so admission is refused
        # (never a sender-lane re-derivation -> the coordinator/default misroute this Bug fixes).
        res = resolve_sender_identity(
            self._env(), anchor_workspace_id="wsA", background_service_target_lane=None
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_SYSTEM_ACTOR_MISSING_LANE)

    def test_blank_target_lane_fails_closed(self) -> None:
        res = resolve_sender_identity(
            self._env(), anchor_workspace_id="wsA", background_service_target_lane="   "
        )
        self.assertEqual(res.reason, REASON_SYSTEM_ACTOR_MISSING_LANE)

    def test_missing_workspace_fails_closed(self) -> None:
        res = resolve_sender_identity(
            self._env(MOZYO_WORKSPACE_ID=""),
            anchor_workspace_id="wsA",
            background_service_target_lane=SUBLANE,
        )
        self.assertEqual(res.reason, REASON_MISSING_SENDER_ENV)

    def test_workspace_anchor_mismatch_fails_closed(self) -> None:
        # The env<->anchor workspace attestation is unchanged for a system actor: a leaked env var
        # cannot mint an identity for another workspace.
        res = resolve_sender_identity(
            self._env(), anchor_workspace_id="wsOTHER", background_service_target_lane=SUBLANE
        )
        self.assertEqual(res.reason, REASON_ENV_ANCHOR_WORKSPACE_MISMATCH)

    def test_missing_anchor_fails_closed(self) -> None:
        res = resolve_sender_identity(
            self._env(), anchor_workspace_id=None, background_service_target_lane=SUBLANE
        )
        self.assertEqual(res.reason, REASON_MISSING_ANCHOR)

    def test_env_less_operator_shell_still_fails_closed(self) -> None:
        # No origin stamp AND no agent role (an env-less operator shell): the system-actor branch is
        # NOT taken, so the agent path fails closed exactly as before — the exception is never widened.
        res = resolve_sender_identity(
            {}, anchor_workspace_id="wsA", background_service_target_lane=SUBLANE
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MISSING_SENDER_ENV)

    def test_sentinel_role_is_not_an_agent_provider(self) -> None:
        # The system-actor role must never be a launchable agent provider: it is admitted ONLY through
        # the origin-stamped branch, never by an ordinary MOZYO_AGENT_ROLE send.
        self.assertNotIn(SYSTEM_ACTOR_ROLE_BACKGROUND_SERVICE, AGENT_PROVIDERS)

    def test_origin_literal_does_not_drift_from_background_service_origin(self) -> None:
        # The terminal-runtime local literal mirrors the execution-platform origin token; a drift guard
        # (the two live in different bounded contexts to avoid an import cycle).
        self.assertEqual(DELIVERY_ORIGIN_BACKGROUND_SERVICE, BACKGROUND_SERVICE_ORIGIN)


# ---------------------------------------------------------------------------
# F2 end-to-end — resolve_herdr_send_target admits the system actor and pins the exact slot.
# ---------------------------------------------------------------------------
class _HerdrCtx:
    """A prepared herdr workspace (config + anchor + fake binary/runner), mirroring the send-entry harness."""

    def __init__(self, tmp, *, rows):
        self.repo = Path(tmp) / "repo"
        self.repo.mkdir()
        self.home = Path(tmp) / "home"
        self.home.mkdir()
        (self.repo / ".mozyo-bridge").mkdir()
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
        )
        register_workspace(self.repo, home=self.home)
        self.workspace_id = read_anchor(self.repo)["workspace_id"]
        self.rows = rows(self.workspace_id)
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.binpath = binpath

    def run(self, argv, capture_output=None, text=None, timeout=None, **kw):
        if list(argv[:1]) == ["git"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a git repo")
        if list(argv[1:]) == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.rows}), stderr=""
            )
        raise AssertionError(f"unexpected call: {argv!r}")

    def background_service_env(self) -> dict:
        # The scrubbed background-service env: workspace + origin stamp, NO agent role / lane.
        return {
            "MOZYO_HERDR_BINARY": str(self.binpath),
            "MOZYO_BRIDGE_HOME": str(self.home),
            "MOZYO_WORKSPACE_ID": self.workspace_id,
            "MOZYO_DELIVERY_ORIGIN": DELIVERY_ORIGIN_BACKGROUND_SERVICE,
        }


class SystemActorHerdrRouteTest(unittest.TestCase):
    """The origin-stamped, lane-less background actor resolves the EXACT sublane gateway, not coordinator."""

    def _rows(self, ws):
        # The sublane codex gateway AND the coordinator's default-lane codex both live: a sender-lane
        # re-derivation would land on the coordinator; the explicit target-lane must pin the sublane.
        return [
            {"name": encode_assigned_name(ws, "codex", SUBLANE), "pane_id": "wGW:pGW"},
            {"name": encode_assigned_name(ws, "codex", "default"), "pane_id": "wCO:pCO"},
        ]

    def test_resolves_sublane_gateway_not_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _HerdrCtx(tmp, rows=self._rows)
            with patch("subprocess.run", ctx.run), patch.dict(
                os.environ, ctx.background_service_env(), clear=True
            ):
                pane = resolve_herdr_send_target(
                    repo_root=repo_root_from_args(_ns(ctx.repo)),
                    target="wGW:pGW",  # the re-resolved explicit locator (NOT the sole authority)
                    target_repo="auto",
                    target_lane=SUBLANE,
                    receiver="codex",
                )
        # The exact sublane gateway slot — never the default-lane coordinator.
        self.assertEqual(pane["id"], "wGW:pGW")
        self.assertEqual(pane["lane_id"], SUBLANE)
        self.assertNotEqual(pane["id"], "wCO:pCO")

    def test_background_actor_without_target_lane_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _HerdrCtx(tmp, rows=self._rows)
            with patch("subprocess.run", ctx.run), patch.dict(
                os.environ, ctx.background_service_env(), clear=True
            ):
                with self.assertRaises(HerdrSendEntryError) as c:
                    resolve_herdr_send_target(
                        repo_root=repo_root_from_args(_ns(ctx.repo)),
                        target="wGW:pGW",
                        target_repo="auto",
                        target_lane=None,
                        receiver="codex",
                    )
        self.assertEqual(c.exception.reason, REASON_SYSTEM_ACTOR_MISSING_LANE)


def _ns(repo):
    ns = argparse.Namespace()
    ns.repo = str(repo)
    return ns


# ---------------------------------------------------------------------------
# F1 — the background transport pins the exact slot with --target-lane.
# ---------------------------------------------------------------------------
class BackgroundTransportArgvTest(unittest.TestCase):
    """default_background_transport emits an argv that pins the stable slot (--target-lane + receiver)."""

    def _ws(self):
        return SupervisedWorkspace(workspace_id="wsA", canonical_path="/tmp/repoA")

    def _target(self, *, lane=SUBLANE, receiver="codex", locator="%GW"):
        return DeliveryTarget(
            workspace_id="wsA", lane=lane, receiver=receiver, issue="14079",
            journal="82511", generation="1", locator=locator,
        )

    def _deliver_capture(self, target, *, stdout='{"status": "sent", "reason": "ok"}'):
        transport = default_background_transport(self._ws())
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        with patch("subprocess.run", fake_run):
            result = transport.deliver(object(), target)
        return calls, result

    def test_argv_pins_target_lane_and_receiver(self) -> None:
        calls, result = self._deliver_capture(self._target())
        self.assertEqual(len(calls), 1)  # exactly-once
        argv = calls[0]
        self.assertIn("--target-lane", argv)
        self.assertEqual(argv[argv.index("--target-lane") + 1], SUBLANE)
        self.assertEqual(argv[argv.index("--to") + 1], "codex")
        self.assertEqual(argv[argv.index("--target") + 1], "%GW")
        self.assertEqual(argv[argv.index("--mode") + 1], "standard")
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.reason, "ok")

    def test_lane_gateway_never_targets_default_coordinator_lane(self) -> None:
        # coordinator 0-wake: a lane_gateway target pins the SUBLANE, never the "default" coordinator lane.
        calls, _ = self._deliver_capture(self._target(lane=SUBLANE))
        argv = calls[0]
        self.assertEqual(argv[argv.index("--target-lane") + 1], SUBLANE)
        self.assertNotEqual(argv[argv.index("--target-lane") + 1], "default")

    def test_blank_lane_omits_target_lane_flag(self) -> None:
        # A target with no lane (defensive) omits the flag rather than passing an empty one.
        calls, _ = self._deliver_capture(self._target(lane=""))
        self.assertNotIn("--target-lane", calls[0])


# ---------------------------------------------------------------------------
# F1 + F4 — the REAL sender + REAL resolver + REAL transport delivery path.
# ---------------------------------------------------------------------------
class ProductionDeliveryPathTest(unittest.TestCase):
    """The composed production path: idle gateway delivers exactly-once; busy is a bounded zero-send."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        self.store_path = self.dir / "workflow-runtime.sqlite"

    def _lane_gateway_row(self):
        outbox = CallbackOutbox(path=self.store_path)
        key = CallbackOutboxKey(
            source="redmine", issue="14079", journal="82511", normalized_gate="review_request",
            callback_route=f"lane_gateway:{SUBLANE}", workspace_id="wsA",
        )
        outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane=SUBLANE, target_receiver="codex",
            target_generation="1", now=NOW,
        )
        return outbox, outbox.claim_pending(now=NOW, workspace_id="wsA")[0]

    def _resolver(self):
        # A live herdr inventory carrying the sublane codex gateway; the live generation authority
        # returns "1" so the correlated row is deliverable (the a18 fresh generation-1 case).
        rows = [{"name": encode_assigned_name("wsA", "codex", SUBLANE), "pane_id": "%GW"}]
        return BackendNeutralTargetResolver(
            workspace_id="wsA", inventory=lambda: (rows, "herdr"),
            live_generation_fn=lambda row: "1",
        )

    def _sender(self, transport, outbox):
        return BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=self._resolver(), transport=transport, outbox=outbox, now_fn=lambda: NOW,
        )

    def test_idle_gateway_delivers_exactly_once_with_target_lane(self) -> None:
        outbox, row = self._lane_gateway_row()
        transport = default_background_transport(
            SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))
        )
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"status": "sent", "reason": "ok"}', stderr=""
            )

        with patch("subprocess.run", fake_run):
            result = self._sender(transport, outbox)(row)
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(len(calls), 1)  # exactly-once real send
        argv = calls[0]
        self.assertEqual(argv[argv.index("--target-lane") + 1], SUBLANE)
        self.assertEqual(argv[argv.index("--to") + 1], "codex")

    def test_busy_gateway_is_bounded_zero_send_with_reason_persisted(self) -> None:
        # A busy receiver -> precondition_not_idle (pre-injection). Zero-send, retryable (no
        # self-amplification), and the reason is propagated for the durable diagnostic surface.
        outbox, row = self._lane_gateway_row()
        transport = default_background_transport(
            SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))
        )

        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 1, stdout='{"status": "blocked", "reason": "precondition_not_idle"}', stderr=""
            )

        with patch("subprocess.run", fake_run):
            result = self._sender(transport, outbox)(row)
        self.assertEqual(result.outcome, SEND_NOT_SENT)  # retryable, NOT uncertain (no blind retry)
        self.assertEqual(result.persist_reason, "precondition_not_idle")

    def test_generation_mismatch_is_transport_zero(self) -> None:
        # The row's expected generation ("1") vs a LIVE generation that bumped ("2") -> authorization
        # zero-send, the transport is NEVER invoked (pre-injection), reason preserved.
        outbox, row = self._lane_gateway_row()
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout='{"status":"sent","reason":"ok"}', stderr="")

        resolver = BackendNeutralTargetResolver(
            workspace_id="wsA",
            inventory=lambda: ([{"name": encode_assigned_name("wsA", "codex", SUBLANE), "pane_id": "%GW"}], "herdr"),
            live_generation_fn=lambda row: "2",  # supersession bump
        )
        transport = default_background_transport(
            SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))
        )
        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=resolver, transport=transport, outbox=outbox, now_fn=lambda: NOW,
        )
        with patch("subprocess.run", fake_run):
            result = sender(row)
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, AUTH_GENERATION_MISMATCH)
        self.assertEqual(calls, [])  # transport never invoked


# ---------------------------------------------------------------------------
# F4 — the first zero-send reason survives to the durable row / dead-letter.
# ---------------------------------------------------------------------------
class ZeroSendReasonPersistenceTest(unittest.TestCase):
    """CallbackOutboxProcessor.deliver persists the sender's first known-not-sent reason to the row."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.outbox = CallbackOutbox(path=self.store_path)

    def _claimed_row(self, *, max_attempts=1):
        key = CallbackOutboxKey(
            source="redmine", issue="14079", journal="82511", normalized_gate="review_request",
            callback_route=f"lane_gateway:{SUBLANE}", workspace_id="wsA",
        )
        self.outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane=SUBLANE, target_receiver="codex",
            target_generation="1", max_attempts=max_attempts, now=NOW,
        )
        return key

    def _row_detail(self, key):
        rows = [r for r in self.outbox.read() if r.key.as_row() == key.as_row()]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_authorization_reason_survives_to_dead_letter(self) -> None:
        key = self._claimed_row(max_attempts=1)
        proc = CallbackOutboxProcessor(self.outbox, _NullSource(), workspace_id="wsA")

        def sender(row):
            return CallbackSendResult(SEND_NOT_SENT, persist_ok=False, persist_reason=AUTH_NO_TARGET)

        proc.deliver(sender, now=NOW, issue="14079")
        row = self._row_detail(key)
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)  # 1 attempt exhausted
        self.assertIn(AUTH_NO_TARGET, row.detail)  # NOT flattened to "retries exhausted"

    def test_transport_precondition_reason_is_distinguishable(self) -> None:
        key = self._claimed_row(max_attempts=1)
        proc = CallbackOutboxProcessor(self.outbox, _NullSource(), workspace_id="wsA")

        def sender(row):
            return CallbackSendResult(
                SEND_NOT_SENT, persist_ok=False, persist_reason="precondition_not_idle"
            )

        proc.deliver(sender, now=NOW, issue="14079")
        row = self._row_detail(key)
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)
        self.assertIn("precondition_not_idle", row.detail)
        # An operator can tell this transport-precondition zero-send from an authorization one.
        self.assertNotIn(AUTH_NO_TARGET, row.detail)

    def test_blank_reason_keeps_store_default_detail(self) -> None:
        # A bare-token sender that carries no reason -> the store keeps its own default (byte-identical
        # pre-#14082 behaviour). _zero_send_detail("") is empty, so no "zero-send:" prefix is written.
        self.assertEqual(_zero_send_detail(""), "")
        self.assertEqual(_zero_send_detail(None), "")
        key = self._claimed_row(max_attempts=1)
        proc = CallbackOutboxProcessor(self.outbox, _NullSource(), workspace_id="wsA")
        proc.deliver(lambda row: SEND_NOT_SENT, now=NOW, issue="14079")
        row = self._row_detail(key)
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)
        self.assertNotIn("zero-send:", row.detail)


class SenderReasonPropagationTest(unittest.TestCase):
    """BackgroundServiceCallbackSender carries the transport's precondition reason to persist_reason."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

    def _row(self):
        return CallbackOutboxRow(
            source="redmine", issue="14079", journal="82511", normalized_gate="review_request",
            callback_route=f"lane_gateway:{SUBLANE}", state="inflight", attempts=0, max_attempts=3,
            send_attempted=True, notification_kind="review_request", notification_summary="",
            gate_mismatch=False, detail="", payload="", claim_token="tok", workspace_id="wsA",
            target_lane=SUBLANE, target_receiver="codex", target_generation="1",
        )

    def _sender(self, transport):
        return BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_FixedResolver(
                DeliveryTarget(
                    workspace_id="wsA", lane=SUBLANE, receiver="codex", issue="14079",
                    journal="82511", generation="1", locator="%GW",
                )
            ),
            transport=transport, now_fn=lambda: NOW,
        )

    def test_transport_blocked_reason_becomes_persist_reason(self) -> None:
        transport = _FixedTransport(HandoffDeliveryResult("blocked", "precondition_not_idle"))
        result = self._sender(transport)(self._row())
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, "precondition_not_idle")

    def test_delivered_keeps_receipt_evidence_not_outcome_reason(self) -> None:
        transport = _FixedTransport(
            HandoffDeliveryResult("sent", "ok", persist_ok=True, persist_reason="ok")
        )
        result = self._sender(transport)(self._row())
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(result.persist_reason, "ok")

    def test_uncertain_transport_reason_preserved(self) -> None:
        transport = _FixedTransport(HandoffDeliveryResult("blocked", "turn_start_unconfirmed"))
        result = self._sender(transport)(self._row())
        self.assertEqual(result.outcome, SEND_UNCERTAIN)
        self.assertEqual(result.persist_reason, "turn_start_unconfirmed")


class _NullSource:
    def read_entries(self, issue_id):
        return []


class _FixedResolver:
    def __init__(self, target):
        self._target = target

    def resolve(self, row):
        return TargetResolution.of([self._target])


class _FixedTransport:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def deliver(self, row, target):
        self.calls.append((row, target))
        return self._result


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
