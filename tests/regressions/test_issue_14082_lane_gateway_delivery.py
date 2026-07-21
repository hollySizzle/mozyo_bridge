"""Redmine #14082: background-service same-lane gateway callback delivery correction.

The a18 live-dogfood failure (j#82511 / j#82517): a fresh ``lane_gateway:<lane>`` worker gate resolved
route / lane / receiver / generation / workspace correctly, yet delivered 0 — 3 attempts all
``send_attempted=false`` known-not-sent, dead-lettered, coordinator direct wake 0. Root causes verified
from source (j#82530 / j#82537) and corrected under the coordinator design constraint j#82553 (Model A'
= a SEPARATE outbox-delivery authority, NOT a loosened agent identity) after review j#82566:

- **F1** — the background transport shelled out to the agent ``mozyo-bridge handoff send`` entry, whose
  herdr target resolution needs an attested agent sender identity (missing in the scrubbed daemon env ->
  ``missing_sender_env`` -> ``target_unavailable``) and re-derives the target lane (coordinator misroute).
  Corrected: a DEDICATED in-process ``background_service`` delivery seam drives the sanctioned turn-start
  rail to the ALREADY-resolved explicit locator — no agent sender identity, no ``handoff send`` entry, no
  target re-derivation, no env-only authorization. The authority (lease + claim + stable tuple + live
  route/generation) is enforced by ``BackgroundServiceCallbackSender`` BEFORE the transport is called.
- **F2** — the durable zero-send reason was persisted raw. Corrected: normalized through a closed
  allowlist; an unrecognized value is dropped to a fixed token (secret-safe by construction).
- **F4** — the first zero-send reason is persisted to the durable row, distinguishing an authorization
  zero-send from a transport-precondition one, surviving to the dead-letter.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    ROUND_STALE,
    ROUND_UNVERIFIABLE,
    BackendNeutralTargetResolver,
    BackgroundServiceCallbackSender,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
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
    FAIL_CLOSED_REASONS,
    DeliveryTarget,
    TargetResolution,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    UNRECOGNIZED_ZERO_SEND_REASON,
    ZERO_SEND_REASON_ALLOWLIST,
    CallbackSendResult,
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
    normalize_zero_send_reason,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    REASON_MISSING_SENDER_ENV,
    resolve_sender_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    OUTCOME_PRECONDITION_NOT_IDLE,
    OUTCOME_STARTED,
)

NOW = "2026-07-19T00:00:00+00:00"
SUBLANE = "issue_14082_lane_gateway_delivery_r1"

_RAIL_MOD = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_turn_start"
_CONFIG_MOD = "mozyo_bridge.application.repo_local_config_loader"


class _FakeTurnStartResult:
    def __init__(self, outcome):
        self.outcome = outcome


# A benign readable composer that matches NO provider's declared startup_blockers -> admitted.
_ADMITTED_PANE = "> \n(the composer is ready for input)"
# The exact workspace-trust startup screen a fresh Claude renders (agent_provider_profiles.yaml
# `workspace_trust_confirmation`) — a live, non-blank, ready-LOOKING pane with no composer.
_TRUST_SCREEN_PANE = (
    "Is this a project you created or one you trust?\n"
    "Claude will be able to read, edit, and execute files here."
)


class _FakeRail:
    """A turn-start rail stub: records reads + (locator, text) drives, returns a chosen outcome."""

    def __init__(self, outcome=OUTCOME_STARTED, *, pane_content=_ADMITTED_PANE, read_error=False):
        self.outcome = outcome
        self.pane_content = pane_content
        self.read_error = read_error
        self.calls = []
        self.reads = []

    def read_visible_pane(self, locator):
        self.reads.append(locator)
        if self.read_error:
            raise RuntimeError("visible-pane read failed")
        return self.pane_content

    def drive_turn_start(self, locator, text, **kw):
        self.calls.append((locator, text))
        return _FakeTurnStartResult(self.outcome)


class _StubConfig:
    terminal_transport = object()  # non-None so the transport proceeds to resolve the rail


def _patched_rail(rail):
    """Patch the transport's lazily-imported rail + config resolution to inject ``rail``."""
    return (
        mock.patch(f"{_RAIL_MOD}.resolve_turn_start_rail", return_value=rail),
        mock.patch(f"{_CONFIG_MOD}.load_repo_local_config", return_value=_StubConfig()),
    )


class _RailCtx:
    """Context manager applying both rail patches for a fake rail."""

    def __init__(self, rail):
        self._patches = _patched_rail(rail)

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------------------
# F1 — resolve_sender_identity is agent-only again (the loosening is reverted).
# ---------------------------------------------------------------------------
class ResolveSenderIdentityAgentOnlyTest(unittest.TestCase):
    """The background_service origin no longer admits a system actor through the agent send entry."""

    def test_background_service_origin_is_not_admitted_by_sender_identity(self) -> None:
        # An env stamped background_service but WITHOUT an agent role must NOT be admitted: the
        # dedicated delivery seam never goes through resolve_sender_identity, so the agent entry stays
        # agent-only (design constraint j#82553 — no loosened agent vocabulary, no env-only auth).
        env = {"MOZYO_WORKSPACE_ID": "wsA", "MOZYO_DELIVERY_ORIGIN": "background_service"}
        res = resolve_sender_identity(env, anchor_workspace_id="wsA")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, REASON_MISSING_SENDER_ENV)

    def test_agent_send_still_requires_agent_role(self) -> None:
        res = resolve_sender_identity({}, anchor_workspace_id="wsA")
        self.assertEqual(res.reason, REASON_MISSING_SENDER_ENV)


# ---------------------------------------------------------------------------
# F1 — the dedicated in-process background_service delivery seam.
# ---------------------------------------------------------------------------
class DedicatedBackgroundTransportTest(unittest.TestCase):
    """default_background_transport drives the turn-start rail to the resolved locator (no handoff send)."""

    def _ws(self):
        return SupervisedWorkspace(workspace_id="wsA", canonical_path="/tmp/repoA")

    def _target(self, *, lane=SUBLANE, receiver="codex", locator="wGW:pGW"):
        return DeliveryTarget(
            workspace_id="wsA", lane=lane, receiver=receiver, issue="14079",
            journal="82511", generation="1", locator=locator,
        )

    def test_drives_rail_to_resolved_locator_with_reply_marker(self) -> None:
        rail = _FakeRail(OUTCOME_STARTED)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(object(), self._target())
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.reason, "ok")
        self.assertEqual(len(rail.calls), 1)  # exactly-once
        locator, text = rail.calls[0]
        self.assertEqual(locator, "wGW:pGW")  # the exact resolved locator, never re-derived
        # The reply landing marker is built from the row's durable anchor + receiver.
        self.assertIn("[mozyo:handoff:source=redmine:issue=14079:journal=82511:kind=reply:to=codex]", text)

    def test_busy_target_maps_to_precondition_not_idle(self) -> None:
        rail = _FakeRail(OUTCOME_PRECONDITION_NOT_IDLE)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(object(), self._target())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "precondition_not_idle")

    def test_unresolvable_rail_is_target_unavailable(self) -> None:
        with _RailCtx(None):  # rail resolves to None (config/backend unresolved)
            result = default_background_transport(self._ws()).deliver(object(), self._target())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "target_unavailable")

    def test_blank_locator_is_target_unavailable(self) -> None:
        rail = _FakeRail(OUTCOME_STARTED)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(
                object(), self._target(locator="")
            )
        self.assertEqual(result.reason, "target_unavailable")
        self.assertEqual(rail.calls, [])  # never driven without a locator

    # --- Redmine #14082 R2 (review j#82572): the #13760 pre-send startup admission gate ---

    def test_startup_screen_is_zero_send_receiver_startup_required(self) -> None:
        # A trust / first-run / login startup screen snapshots as idle-LOOKING but has no composer:
        # the seam must classify the receiver's VISIBLE pane and REFUSE zero-send (never drive the
        # rail into it), distinct from a busy precondition. Uses the real evaluator + real Claude
        # provider profile (which declares the workspace-trust startup screen).
        rail = _FakeRail(OUTCOME_STARTED, pane_content=_TRUST_SCREEN_PANE)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(
                object(), self._target(receiver="claude")
            )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "receiver_startup_interaction_required")
        self.assertEqual(rail.calls, [])  # the rail was NEVER driven — zero text, zero Enter
        self.assertEqual(rail.reads, ["wGW:pGW"])  # the visible pane WAS classified

    def test_unreadable_pane_is_zero_send_target_unavailable(self) -> None:
        # An unreadable visible pane never decays to "startup clear": fail-closed zero-send, rail 0.
        rail = _FakeRail(OUTCOME_STARTED, read_error=True)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(object(), self._target())
        self.assertEqual(result.reason, "target_unavailable")
        self.assertEqual(rail.calls, [])

    def test_unknown_provider_is_zero_send_target_unavailable(self) -> None:
        # A receiver that resolves to no registered provider profile fails closed (never assume it
        # has no startup screen), rail 0.
        rail = _FakeRail(OUTCOME_STARTED)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(
                object(), self._target(receiver="not_a_provider")
            )
        self.assertEqual(result.reason, "target_unavailable")
        self.assertEqual(rail.calls, [])

    def test_admitted_readable_composer_drives_rail(self) -> None:
        # A benign readable composer matching NO declared startup screen is admitted and driven once.
        rail = _FakeRail(OUTCOME_STARTED, pane_content=_ADMITTED_PANE)
        with _RailCtx(rail):
            result = default_background_transport(self._ws()).deliver(object(), self._target())
        self.assertEqual(result.status, "sent")
        self.assertEqual(len(rail.calls), 1)  # driven exactly once, AFTER admission


# ---------------------------------------------------------------------------
# F1 + F4 — the REAL sender + REAL resolver + REAL transport (rail injected).
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

    def _resolver(self, *, live_generation="1"):
        # A live herdr inventory carrying the sublane codex gateway; the live generation authority
        # returns the row's generation so the correlated row is deliverable (the a18 fresh gen-1 case).
        rows = [{"name": encode_assigned_name("wsA", "codex", SUBLANE), "pane_id": "wGW:pGW"}]
        return BackendNeutralTargetResolver(
            workspace_id="wsA", inventory=lambda: (rows, "herdr"),
            live_generation_fn=lambda row: live_generation,
        )

    def _sender(self, resolver, outbox):
        transport = default_background_transport(
            SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))
        )
        return BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=resolver, transport=transport, outbox=outbox, now_fn=lambda: NOW,
        )

    def test_idle_gateway_delivers_exactly_once_to_resolved_locator(self) -> None:
        outbox, row = self._lane_gateway_row()
        rail = _FakeRail(OUTCOME_STARTED)
        with _RailCtx(rail):
            result = self._sender(self._resolver(), outbox)(row)
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(len(rail.calls), 1)  # exactly-once real send
        self.assertEqual(rail.calls[0][0], "wGW:pGW")  # the resolved sublane gateway, never coordinator

    def test_busy_gateway_is_bounded_zero_send_with_reason_persisted(self) -> None:
        outbox, row = self._lane_gateway_row()
        rail = _FakeRail(OUTCOME_PRECONDITION_NOT_IDLE)
        with _RailCtx(rail):
            result = self._sender(self._resolver(), outbox)(row)
        self.assertEqual(result.outcome, SEND_NOT_SENT)  # retryable, NOT uncertain (no blind retry)
        self.assertEqual(result.persist_reason, "precondition_not_idle")

    def test_generation_mismatch_never_drives_rail(self) -> None:
        outbox, row = self._lane_gateway_row()
        rail = _FakeRail(OUTCOME_STARTED)
        with _RailCtx(rail):
            result = self._sender(self._resolver(live_generation="2"), outbox)(row)  # supersession bump
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, AUTH_GENERATION_MISMATCH)
        self.assertEqual(rail.calls, [])  # transport never invoked (pre-injection authorization zero-send)


# ---------------------------------------------------------------------------
# F2 — zero-send reason normalization is a closed, secret-safe allowlist.
# ---------------------------------------------------------------------------
class ZeroSendReasonAllowlistTest(unittest.TestCase):
    def test_known_reason_passes_through(self) -> None:
        self.assertEqual(normalize_zero_send_reason("precondition_not_idle"), "precondition_not_idle")
        self.assertEqual(normalize_zero_send_reason(AUTH_NO_TARGET), AUTH_NO_TARGET)

    def test_blank_reason_is_empty(self) -> None:
        self.assertEqual(normalize_zero_send_reason(""), "")
        self.assertEqual(normalize_zero_send_reason(None), "")

    def test_unrecognized_reason_is_dropped_to_fixed_token(self) -> None:
        # A path / credential / prose that leaked into a reason must NOT survive: the raw value is
        # dropped and replaced by the fixed token.
        for hostile in ("/Users/secret/token.pem", "api_key=abc123", "some free prose reason"):
            self.assertEqual(normalize_zero_send_reason(hostile), UNRECOGNIZED_ZERO_SEND_REASON)

    def test_allowlist_covers_authorization_and_round_fence_vocabularies(self) -> None:
        # Drift guard: every background_service authorization reason + the round-fence tokens are in the
        # allowlist, so a renamed token is caught here instead of silently normalizing to unrecognized.
        self.assertTrue(FAIL_CLOSED_REASONS <= ZERO_SEND_REASON_ALLOWLIST)
        self.assertIn(ROUND_STALE, ZERO_SEND_REASON_ALLOWLIST)
        self.assertIn(ROUND_UNVERIFIABLE, ZERO_SEND_REASON_ALLOWLIST)


# ---------------------------------------------------------------------------
# F4 + F2 — the first zero-send reason survives to the durable row, secret-safe.
# ---------------------------------------------------------------------------
class ZeroSendReasonPersistenceTest(unittest.TestCase):
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

    def _deliver_with_reason(self, reason):
        key = self._claimed_row(max_attempts=1)
        proc = CallbackOutboxProcessor(self.outbox, _NullSource(), workspace_id="wsA")
        proc.deliver(
            lambda row: CallbackSendResult(SEND_NOT_SENT, persist_ok=False, persist_reason=reason),
            now=NOW, issue="14079",
        )
        return self._row_detail(key)

    def test_authorization_reason_survives_to_dead_letter(self) -> None:
        row = self._deliver_with_reason(AUTH_NO_TARGET)
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)
        self.assertIn(AUTH_NO_TARGET, row.detail)  # NOT flattened to "retries exhausted"

    def test_transport_precondition_reason_is_distinguishable(self) -> None:
        row = self._deliver_with_reason("precondition_not_idle")
        self.assertIn("precondition_not_idle", row.detail)
        self.assertNotIn(AUTH_NO_TARGET, row.detail)  # distinct from an authorization zero-send

    def test_hostile_reason_is_not_persisted_raw(self) -> None:
        hostile = "/Users/alice/.ssh/id_rsa"
        row = self._deliver_with_reason(hostile)
        self.assertEqual(row.state, CALLBACK_DEAD_LETTER)
        self.assertNotIn(hostile, row.detail)  # the raw path is dropped
        self.assertIn(UNRECOGNIZED_ZERO_SEND_REASON, row.detail)

    def test_blank_reason_keeps_store_default_detail(self) -> None:
        self.assertEqual(_zero_send_detail(""), "")
        self.assertEqual(_zero_send_detail(None), "")
        key = self._claimed_row(max_attempts=1)
        proc = CallbackOutboxProcessor(self.outbox, _NullSource(), workspace_id="wsA")
        proc.deliver(lambda row: SEND_NOT_SENT, now=NOW, issue="14079")
        row = self._row_detail(key)
        self.assertNotIn("zero-send:", row.detail)


class SenderReasonPropagationTest(unittest.TestCase):
    """BackgroundServiceCallbackSender carries a normalized transport reason to persist_reason."""

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
                    journal="82511", generation="1", locator="wGW:pGW",
                )
            ),
            transport=transport, now_fn=lambda: NOW,
        )

    def test_transport_precondition_reason_becomes_persist_reason(self) -> None:
        result = self._sender(_FixedTransport(HandoffDeliveryResult("blocked", "precondition_not_idle")))(self._row())
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, "precondition_not_idle")

    def test_hostile_transport_reason_is_normalized(self) -> None:
        result = self._sender(_FixedTransport(HandoffDeliveryResult("blocked", "/etc/shadow")))(self._row())
        # An unrecognized transport reason maps to SEND_UNCERTAIN (not a known NOT_SENT reason) and its
        # persist_reason is the fixed token, never the raw path.
        self.assertEqual(result.outcome, SEND_UNCERTAIN)
        self.assertEqual(result.persist_reason, UNRECOGNIZED_ZERO_SEND_REASON)

    def test_delivered_keeps_receipt_evidence(self) -> None:
        result = self._sender(
            _FixedTransport(HandoffDeliveryResult("sent", "ok", persist_ok=True, persist_reason="ok"))
        )(self._row())
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(result.persist_reason, "ok")


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
