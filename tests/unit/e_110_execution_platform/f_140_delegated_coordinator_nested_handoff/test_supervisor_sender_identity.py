"""Background-service delivery authority tests (Redmine #13683 design answer j#77216, Model A').

The R3 evidence for the ``background_service`` delivery authority: every fail-closed path is a
zero-send (no lease / no claim / foreign workspace / no or ambiguous target / generation mismatch /
lease expired), an authorized delivery goes through the transport seam to the exact re-resolved
target, a genuine uncertain outcome does not blind-retry, and the delivery env is scrubbed of agent
identity + stamped as a separated origin.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_INFLIGHT
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    BackgroundServiceCallbackSender,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
    AUTH_AMBIGUOUS_TARGET,
    AUTH_ANCHOR_MISMATCH,
    AUTH_FOREIGN_WORKSPACE,
    AUTH_GENERATION_MISMATCH,
    AUTH_TARGET_MISMATCH,
    AUTH_NO_CLAIM,
    AUTH_NO_LEASE,
    AUTH_NO_TARGET,
    AUTH_OK,
    DeliveryTarget,
    TargetResolution,
    authorize_background_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
    SEND_NOT_SENT,
    SEND_UNCERTAIN,
)

NOW = "2026-07-13T00:00:00+00:00"


def _row(
    workspace_id: str, *, claim_token: str = "tok", target_receiver: str = "codex",
    target_lane: str = "default", target_generation: str = "g1",
) -> CallbackOutboxRow:
    return CallbackOutboxRow(
        source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
        callback_route="coordinator", state=CALLBACK_INFLIGHT, attempts=0, max_attempts=3,
        send_attempted=True, notification_kind="review_request", notification_summary="",
        gate_mismatch=False, detail="", payload="", claim_token=claim_token, workspace_id=workspace_id,
        target_lane=target_lane, target_receiver=target_receiver, target_generation=target_generation,
    )


def _target(
    workspace_id: str, *, generation: str = "g1", locator: str = "%1", receiver: str = "codex",
    lane: str = "default",
) -> DeliveryTarget:
    return DeliveryTarget(
        workspace_id=workspace_id, lane=lane, receiver=receiver, issue="13683",
        journal="77065", generation=generation, locator=locator,
    )


class _FakeResolver:
    def __init__(self, targets):
        self._targets = tuple(targets)

    def resolve(self, row):
        return TargetResolution.of(self._targets)


class _RecordingTransport:
    def __init__(self, result: HandoffDeliveryResult):
        self._result = result
        self.calls = []

    def deliver(self, row, target):
        self.calls.append((row, target))
        return self._result


# ---------------------------------------------------------------------------
# Pure authorization matrix.
# ---------------------------------------------------------------------------


class AuthorizeMatrixTest(unittest.TestCase):
    def _authorize(self, **over):
        base = dict(
            expected_workspace="wsA", row_workspace="wsA", row_issue="13683", row_journal="77065",
            row_lane="default", row_receiver="codex", row_generation="g1",
            has_lease=True, has_claim=True,
            resolution=TargetResolution.of([_target("wsA")]),
        )
        base.update(over)
        return authorize_background_delivery(**base)

    def test_authorized_happy_path(self):
        d = self._authorize()
        self.assertTrue(d.authorized)
        self.assertEqual(d.reason, AUTH_OK)
        self.assertEqual(d.target.workspace_id, "wsA")

    def test_foreign_row_workspace(self):
        d = self._authorize(row_workspace="wsB")
        self.assertFalse(d.authorized)
        self.assertEqual(d.reason, AUTH_FOREIGN_WORKSPACE)

    def test_no_lease(self):
        self.assertEqual(self._authorize(has_lease=False).reason, AUTH_NO_LEASE)

    def test_no_claim(self):
        self.assertEqual(self._authorize(has_claim=False).reason, AUTH_NO_CLAIM)

    def test_no_target(self):
        self.assertEqual(self._authorize(resolution=TargetResolution.of([])).reason, AUTH_NO_TARGET)

    def test_ambiguous_target(self):
        d = self._authorize(resolution=TargetResolution.of([_target("wsA"), _target("wsA", locator="%2")]))
        self.assertEqual(d.reason, AUTH_AMBIGUOUS_TARGET)

    def test_resolved_target_foreign_workspace(self):
        d = self._authorize(resolution=TargetResolution.of([_target("wsB")]))
        self.assertEqual(d.reason, AUTH_FOREIGN_WORKSPACE)

    def test_generation_mismatch(self):
        d = self._authorize(
            resolution=TargetResolution.of([_target("wsA", generation="g2")]), row_generation="g1"
        )
        self.assertEqual(d.reason, AUTH_GENERATION_MISMATCH)

    def test_blank_generation_on_both_sides_is_zero_send(self):
        # R6-F1: a blank expected AND blank live generation must fail closed (the Phase A production
        # default) — delivery is disabled until #13684 supplies the correlated generation authority,
        # never a silent uncorrelated send.
        d = self._authorize(resolution=TargetResolution.of([_target("wsA", generation="")]), row_generation="")
        self.assertFalse(d.authorized)
        self.assertEqual(d.reason, AUTH_GENERATION_MISMATCH)

    def test_blank_expected_generation_with_live_generation_is_zero_send(self):
        # R6-F1: even when the live target carries a generation, a blank expected generation on the
        # row cannot be correlated -> fail closed.
        d = self._authorize(resolution=TargetResolution.of([_target("wsA", generation="g1")]), row_generation="")
        self.assertEqual(d.reason, AUTH_GENERATION_MISMATCH)

    def test_unknown_target_generation_when_expected_is_zero_send(self):
        # R4-F2 repro: expected g1, resolved target has NO generation -> strict fail-closed.
        d = self._authorize(
            resolution=TargetResolution.of([_target("wsA", generation="")]), row_generation="g1"
        )
        self.assertEqual(d.reason, AUTH_GENERATION_MISMATCH)

    def test_wrong_anchor_target_is_zero_send(self):
        # R3-F3 repro: a resolved target for a DIFFERENT issue/journal than the row is not delivered.
        wrong = DeliveryTarget(
            workspace_id="wsA", lane="default", receiver="codex", issue="99999", journal="1", locator="%x"
        )
        d = self._authorize(resolution=TargetResolution.of([wrong]))
        self.assertEqual(d.reason, AUTH_ANCHOR_MISMATCH)

    def test_wrong_receiver_target_is_zero_send(self):
        # R4-F2 repro: same anchor but a wrong receiver role (a default-lane Claude) is never sent.
        d = self._authorize(resolution=TargetResolution.of([_target("wsA", receiver="claude")]))
        self.assertEqual(d.reason, AUTH_TARGET_MISMATCH)

    def test_wrong_lane_target_is_zero_send(self):
        d = self._authorize(resolution=TargetResolution.of([_target("wsA", lane="other")]))
        self.assertEqual(d.reason, AUTH_TARGET_MISMATCH)

    def test_blank_expected_receiver_is_zero_send(self):
        # An unresolved expected receiver (blank) cannot be verified -> fail closed.
        d = self._authorize(row_receiver="")
        self.assertEqual(d.reason, AUTH_TARGET_MISMATCH)

    def test_unknown_generation_wrong_lane_receiver_repro(self):
        # The exact reviewer R4-F2 reproduction: same anchor, wrong lane/receiver, no generation.
        wrong = _target("wsA", lane="other", receiver="claude", generation="")
        d = self._authorize(resolution=TargetResolution.of([wrong]))
        self.assertFalse(d.authorized)
        self.assertEqual(d.reason, AUTH_TARGET_MISMATCH)


# ---------------------------------------------------------------------------
# Sender: lease + claim authority, resolution, transport, outcome mapping.
# ---------------------------------------------------------------------------


class BackgroundServiceSenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")

    def _sender(self, *, workspace_id="wsA", holder="superX", resolver=None, transport=None, now=NOW):
        self.transport = transport or _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        return BackgroundServiceCallbackSender(
            workspace_id=workspace_id, holder=holder, lease_store=self.lease_store,
            target_resolver=resolver or _FakeResolver([_target(workspace_id)]),
            transport=self.transport, now_fn=lambda: now,
        )

    def test_authorized_delivery_calls_transport_delivered(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender()
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][1].workspace_id, "wsA")

    def test_no_lease_is_zero_send_not_sent(self):
        # no lease acquired at all
        sender = self._sender()
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)  # retryable, NOT uncertain
        self.assertEqual(result.persist_reason, AUTH_NO_LEASE)
        self.assertEqual(self.transport.calls, [])  # transport never invoked

    def test_expired_lease_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=100)  # expires 00:01:40
        sender = self._sender(now="2026-07-13T01:00:00+00:00")  # past expiry
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(self.transport.calls, [])

    def test_lease_held_by_other_holder_is_zero_send(self):
        self.lease_store.acquire("wsA", "otherHolder", now=NOW, ttl_seconds=600)
        sender = self._sender(holder="superX")
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_NO_LEASE)
        self.assertEqual(self.transport.calls, [])

    def test_no_claim_token_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender()
        result = sender(_row("wsA", claim_token=""))  # unclaimed row
        self.assertEqual(result.persist_reason, AUTH_NO_CLAIM)
        self.assertEqual(self.transport.calls, [])

    def test_foreign_row_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender()
        result = sender(_row("wsB"))  # foreign workspace row
        self.assertEqual(result.persist_reason, AUTH_FOREIGN_WORKSPACE)
        self.assertEqual(self.transport.calls, [])

    def test_no_target_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender(resolver=_FakeResolver([]))
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_NO_TARGET)
        self.assertEqual(self.transport.calls, [])

    def test_ambiguous_target_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender(resolver=_FakeResolver([_target("wsA"), _target("wsA", locator="%2")]))
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_AMBIGUOUS_TARGET)
        self.assertEqual(self.transport.calls, [])

    def test_unresolvable_route_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

        class _Raises:
            def resolve(self, row):
                raise RuntimeError("route ledger unreadable")

        sender = self._sender(resolver=_Raises())
        result = sender(_row("wsA"))
        self.assertEqual(result.persist_reason, AUTH_NO_TARGET)  # fail-closed -> no target
        self.assertEqual(self.transport.calls, [])

    def test_transport_uncertain_does_not_retry(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        sender = self._sender(transport=_RecordingTransport(HandoffDeliveryResult("blocked", "turn_start_unconfirmed")))
        result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_UNCERTAIN)  # no blind retry (boundary 5)

    def test_transport_exception_is_uncertain(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

        class _BoomTransport:
            calls = []

            def deliver(self, row, target):
                raise RuntimeError("subprocess exploded")

        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsA")]), transport=_BoomTransport(), now_fn=lambda: NOW,
        )
        self.assertEqual(sender(_row("wsA")).outcome, SEND_UNCERTAIN)


class TwoWorkspaceExactRouteTest(unittest.TestCase):
    """R3 evidence: 2 workspaces each deliver to their own exact target; cross-ws is zero-send."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        self.lease_store.acquire("wsB", "superX", now=NOW, ttl_seconds=600)
        self.tA = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        self.tB = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        self.sA = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsA", locator="%A")]), transport=self.tA, now_fn=lambda: NOW,
        )
        self.sB = BackgroundServiceCallbackSender(
            workspace_id="wsB", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsB", locator="%B")]), transport=self.tB, now_fn=lambda: NOW,
        )

    def test_each_workspace_delivers_to_its_own_target(self):
        self.assertEqual(self.sA(_row("wsA")).outcome, SEND_DELIVERED)
        self.assertEqual(self.sB(_row("wsB")).outcome, SEND_DELIVERED)
        self.assertEqual(self.tA.calls[0][1].locator, "%A")
        self.assertEqual(self.tB.calls[0][1].locator, "%B")

    def test_cross_workspace_row_is_zero_send(self):
        # wsA's authority handed a wsB row -> foreign -> zero-send, neither transport fires.
        result = self.sA(_row("wsB"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(self.tA.calls, [])


class ClaimReverificationTest(unittest.TestCase):
    """R3-F4: the sender re-verifies the outbox claim (ownership + lease) against the store."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

    def _outbox_with_claimed_row(self):
        from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
        from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_PENDING

        outbox = CallbackOutbox(path=self.store_path)
        key = CallbackOutboxKey(
            source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
            callback_route="coordinator", workspace_id="wsA",
        )
        outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane="default", target_receiver="codex",
            target_generation="g1", now=NOW,
        )
        claimed = outbox.claim_pending(now=NOW, workspace_id="wsA")
        return outbox, claimed[0]

    def _sender(self, outbox, *, now=NOW, transport=None):
        # The fake resolver stands in for a connected live generation authority (#13684): it returns a
        # target carrying generation "g1" so the correlated row can deliver — the mechanism proof.
        self.transport = transport or _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        return BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_FakeResolver([_target("wsA")]), transport=self.transport,
            outbox=outbox, now_fn=lambda: now,
        )

    def test_valid_claim_delivers(self):
        outbox, row = self._outbox_with_claimed_row()
        result = self._sender(outbox, now=NOW)(row)
        self.assertEqual(result.outcome, SEND_DELIVERED)
        self.assertEqual(len(self.transport.calls), 1)

    def test_expired_claim_is_zero_send(self):
        outbox, row = self._outbox_with_claimed_row()  # claimed_at = NOW; supervisor lease ttl 600
        # now is past the CLAIM lease (300s -> stale after 00:05:00) but before the SUPERVISOR lease
        # expiry (600s -> 00:10:00), so the lease is live and the claim staleness is what fails.
        result = self._sender(outbox, now="2026-07-13T00:06:00+00:00")(row)
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, AUTH_NO_CLAIM)
        self.assertEqual(self.transport.calls, [])

    def test_de_owned_claim_is_zero_send(self):
        outbox, row = self._outbox_with_claimed_row()
        # A concurrent processor recovered + re-claimed the row: the persisted token no longer
        # matches this sender's row token -> zero-send.
        from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow

        stale = CallbackOutboxRow(**{**row.as_payload(), "claim_token": "someoneelse"})
        result = self._sender(outbox, now=NOW)(stale)
        self.assertEqual(result.persist_reason, AUTH_NO_CLAIM)
        self.assertEqual(self.transport.calls, [])


class BackendNeutralResolverTest(unittest.TestCase):
    """R5-F1: the production resolver delegates to resolve_route_neutral over live Herdr agent rows."""

    def _resolver(self, workspace_id, rows, *, backend="herdr"):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
            BackendNeutralTargetResolver,
        )

        return BackendNeutralTargetResolver(workspace_id=workspace_id, inventory=lambda: (list(rows), backend))

    def _herdr(self, workspace_id, role, locator, *, lane="default"):
        # A live herdr `agent list` row: name = the mzb1 assigned name (identity source), pane_id.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        return {"name": encode_assigned_name(workspace_id, role, lane), "pane_id": locator}

    def test_resolves_this_workspace_live_role(self):
        inv = [self._herdr("wsA", "codex", "%A"), self._herdr("wsB", "codex", "%B")]
        res = self._resolver("wsA", inv).resolve(_row("wsA"))  # row target_receiver=codex
        self.assertEqual(len(res.targets), 1)
        self.assertEqual(res.targets[0].receiver, "codex")
        self.assertEqual(res.targets[0].locator, "%A")
        self.assertEqual(res.targets[0].issue, "13683")  # anchor carried from the row
        self.assertEqual(res.targets[0].generation, "")  # generation from LIVE (none) -> blank

    def test_wrong_role_pane_is_not_resolved(self):
        # R4-F1: a live default-lane Claude is NEVER resolved for a coordinator(codex) row.
        inv = [self._herdr("wsA", "claude", "%claudeA")]
        res = self._resolver("wsA", inv).resolve(_row("wsA", target_receiver="codex"))
        self.assertEqual(res.targets, ())  # no codex slot live -> fail-closed

    def test_wrong_lane_name_is_not_resolved(self):
        # R5-F1: a live row for a DIFFERENT lane (wrong assigned name / pane_name) is not matched —
        # the delegation enforces the full stable key incl. pane_name, not just ws/role.
        inv = [self._herdr("wsA", "codex", "%other", lane="otherlane")]
        res = self._resolver("wsA", inv).resolve(_row("wsA", target_receiver="codex", target_lane="default"))
        self.assertEqual(res.targets, ())

    def test_no_cross_workspace_resolution(self):
        inv = [self._herdr("wsA", "codex", "%A"), self._herdr("wsB", "codex", "%B")]
        res = self._resolver("wsA", inv).resolve(_row("wsA"))
        self.assertTrue(all(t.workspace_id == "wsA" for t in res.targets))
        self.assertNotIn("%B", {t.locator for t in res.targets})

    def test_unsupported_backend_is_fail_closed(self):
        # R5-F1: a tmux (unadapted) backend is an explicit Phase A fail-closed boundary.
        inv = [self._herdr("wsA", "codex", "%A")]
        res = self._resolver("wsA", inv, backend="tmux").resolve(_row("wsA"))
        self.assertEqual(res.targets, ())

    def test_blank_expected_receiver_resolves_nothing(self):
        inv = [self._herdr("wsA", "codex", "%A")]
        res = self._resolver("wsA", inv).resolve(_row("wsA", target_receiver=""))
        self.assertEqual(res.targets, ())

    def test_empty_inventory_is_fail_closed(self):
        res = self._resolver("wsA", []).resolve(_row("wsA"))
        self.assertEqual(res.targets, ())

    def test_generation_from_live_not_row(self):
        # R5-F2: the resolved target's generation comes from the LIVE inventory (Phase A: none ->
        # blank), NEVER copied from the row — so a generation-correlated row fails closed downstream.
        inv = [self._herdr("wsA", "codex", "%A")]
        res = self._resolver("wsA", inv).resolve(_row("wsA", target_generation="g1"))
        self.assertEqual(len(res.targets), 1)
        self.assertEqual(res.targets[0].generation, "")  # NOT "g1"


class TargetTupleBackfillTest(unittest.TestCase):
    """R5-F2: a blank-tuple row (migration / early ingest) is atomically backfilled on re-ingest."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "wf.sqlite"

    def _outbox_key(self):
        from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey

        key = CallbackOutboxKey(
            source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
            callback_route="coordinator", workspace_id="wsA",
        )
        return CallbackOutbox(path=self.store_path), key

    def test_blank_tuple_row_backfilled_on_reingest(self):
        from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_PENDING

        outbox, key = self._outbox_key()
        outbox.enqueue(key, initial_state=CALLBACK_PENDING, now=NOW)  # blank tuple (migration-shaped)
        self.assertEqual(outbox.read()[0].target_receiver, "")
        result = outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane="default", target_receiver="codex", now=NOW
        )
        self.assertFalse(result.inserted)  # no new row
        rows = outbox.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target_receiver, "codex")  # backfilled
        self.assertEqual(rows[0].target_lane, "default")
        self.assertEqual(rows[0].state, CALLBACK_PENDING)  # state never reset

    def test_set_tuple_is_not_overwritten(self):
        from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_PENDING

        outbox, key = self._outbox_key()
        outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane="default", target_receiver="codex", now=NOW
        )
        # A second ingest with a different tuple must NOT overwrite the set expectation.
        outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane="other", target_receiver="claude", now=NOW
        )
        rows = outbox.read()
        self.assertEqual(rows[0].target_receiver, "codex")
        self.assertEqual(rows[0].target_lane, "default")


class ClaimLostDuringResolverTest(unittest.TestCase):
    """R4-F3: an authority lost DURING target resolution must still zero-send (pre-transport fence)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")

    def _outbox_with_claimed_row(self):
        from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
        from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_PENDING

        outbox = CallbackOutbox(path=self.store_path)
        key = CallbackOutboxKey(
            source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
            callback_route="coordinator", workspace_id="wsA",
        )
        # A correlated generation so authorize passes the generation fence and the pre-transport
        # LEASE re-verify is what catches the lost lease (this test targets the R4-F3 fence, not R6-F1).
        outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane="default", target_receiver="codex",
            target_generation="g1", now=NOW,
        )
        return outbox, outbox.claim_pending(now=NOW, workspace_id="wsA")[0]

    def test_lease_lost_during_resolver_is_zero_send(self):
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)
        outbox, row = self._outbox_with_claimed_row()
        transport = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        lease_store = self.lease_store

        class _TakeoverResolver:
            """Resolves a valid target, but as a side effect the lease is handed to another holder."""

            def resolve(self, row):
                # Ownership lost mid-resolution: the incumbent releases and another supervisor takes
                # over (the first _holds_lease already passed; the pre-transport re-check must catch it).
                lease_store.release("wsA", "superX")
                lease_store.acquire("wsA", "otherSuper", now=NOW, ttl_seconds=600)
                return TargetResolution.of([_target("wsA")])

        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=_TakeoverResolver(), transport=transport, outbox=outbox,
            now_fn=lambda: NOW,  # lease live at the first check; the resolver hands it over
        )
        result = sender(row)
        # The pre-transport re-verify catches the lost lease -> zero-send, transport NEVER called.
        self.assertEqual(transport.calls, [])
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, AUTH_NO_LEASE)


class BackgroundTransportEnvTest(unittest.TestCase):
    # Redmine #14082: the background_service delivery no longer builds a scrubbed subprocess env (the
    # dedicated in-process rail seam replaced the `handoff send` subprocess), so the former
    # `background_transport_env` scrub helper is gone. The invariant that MATTERS remains: ambient env
    # NEVER grants delivery authority — lease + claim do.
    def test_ambient_pollution_does_not_grant_authority(self):
        # Even with a polluted agent env, without a lease the delivery is zero-send: the authority is
        # lease + claim, NOT the env identity.
        d = Path(tempfile.mkdtemp())
        lease_store = SupervisorLeaseStore(path=d / "lease.sqlite")  # no lease acquired
        transport = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=lease_store,
            target_resolver=_FakeResolver([_target("wsA")]), transport=transport, now_fn=lambda: NOW,
        )
        with mock.patch.dict("os.environ", {"MOZYO_AGENT_ROLE": "codex", "MOZYO_WORKSPACE_ID": "wsA"}, clear=True):
            result = sender(_row("wsA"))
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(transport.calls, [])


class ProductionSenderGenerationFenceTest(unittest.TestCase):
    """R6-F1: the PRODUCTION path (real BackendNeutralTargetResolver over live Herdr rows) fails
    closed when generation is blank/unknown — blank row + blank live -> transport 0, never sent.

    This is the production-composition regression the review requires: no injected fake resolver, a
    valid lease + a real durable claim, a canonical Herdr assigned name live — yet delivery is
    disabled because Phase A has no live generation authority (that is #13684)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.lease_store.acquire("wsA", "superX", now=NOW, ttl_seconds=600)

    def _real_resolver(self, rows, *, backend="herdr"):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
            BackendNeutralTargetResolver,
        )

        return BackendNeutralTargetResolver(workspace_id="wsA", inventory=lambda: (list(rows), backend))

    def _herdr(self, role, locator, *, lane="default"):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        return {"name": encode_assigned_name("wsA", role, lane), "pane_id": locator}

    def _claimed_row(self, *, target_generation=""):
        from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
        from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_PENDING

        outbox = CallbackOutbox(path=self.store_path)
        key = CallbackOutboxKey(
            source="redmine", issue="13683", journal="77065", normalized_gate="review_request",
            callback_route="coordinator", workspace_id="wsA",
        )
        outbox.enqueue(
            key, initial_state=CALLBACK_PENDING, target_lane="default", target_receiver="codex",
            target_generation=target_generation, now=NOW,
        )
        return outbox, outbox.claim_pending(now=NOW, workspace_id="wsA")[0]

    def test_blank_generation_production_path_is_transport_zero(self):
        # A live coordinator(codex) slot IS resolvable, lease + claim are valid — the ONLY thing that
        # fails closed is the blank live generation (Phase A has no #13684 authority).
        outbox, row = self._claimed_row(target_generation="")
        transport = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=self._real_resolver([self._herdr("codex", "%A")]),
            transport=transport, outbox=outbox, now_fn=lambda: NOW,
        )
        result = sender(row)
        self.assertEqual(transport.calls, [])  # nothing delivered
        self.assertEqual(result.outcome, SEND_NOT_SENT)
        self.assertEqual(result.persist_reason, AUTH_GENERATION_MISMATCH)

    def test_correlated_row_still_transport_zero_when_live_generation_blank(self):
        # Even a generation-correlated row (target_generation="g1") fails closed on the production path,
        # because the REAL resolver derives generation from LIVE inventory (blank in Phase A), never
        # from the row -> the fence is a genuine cross-authority comparison, not a self-comparison.
        outbox, row = self._claimed_row(target_generation="g1")
        transport = _RecordingTransport(HandoffDeliveryResult("sent", "ok"))
        sender = BackgroundServiceCallbackSender(
            workspace_id="wsA", holder="superX", lease_store=self.lease_store,
            target_resolver=self._real_resolver([self._herdr("codex", "%A")]),
            transport=transport, outbox=outbox, now_fn=lambda: NOW,
        )
        result = sender(row)
        self.assertEqual(transport.calls, [])
        self.assertEqual(result.persist_reason, AUTH_GENERATION_MISMATCH)


if __name__ == "__main__":
    unittest.main()
