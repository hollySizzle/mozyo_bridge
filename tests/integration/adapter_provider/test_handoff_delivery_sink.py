"""Durable delivery-record persistence seam tests (Redmine #12311).

Covers the core sink boundary, the source/provider semantics, the explicit
failure vocabulary, credential / private-path redaction, the persistence
conditions (duplicate-receiver advisory / delivery outcome / receiver-target
identity), the fail-closed resolver, and the opt-in `--persist-delivery` CLI
wiring (behavior-preserving by default; live transport deferred).

Abstract `/workspace/...` placeholders / `DROP-*-SENTINEL`-style tokens are used
deliberately — no personal home path or secret-shaped literal in tracked test
files (`vibes/docs/rules/public-private-boundary.md`).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from tests.integration.e_110_execution_platform.f_130_handoff_routing import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
    DeliveryRecordError,
    DeliveryRecordNote,
    DeliveryReceipt,
    DeliveryTransportError,
    NullDeliveryRecordSink,
    PERSIST_CREDENTIAL_MISSING,
    PERSIST_DISABLED,
    PERSIST_NO_ANCHOR,
    PERSIST_OK,
    PERSIST_PROVIDER_UNAVAILABLE,
    PERSIST_TRANSPORT_ERROR,
    PERSIST_UNSUPPORTED_SOURCE,
    RECORD_CLASS_DELIVERY,
    RedmineDeliveryRecordSink,
    UnsupportedSourceDeliveryRecordSink,
    UnwiredDeliveryRecordSink,
    build_delivery_record_note,
    resolve_delivery_record_sink,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    MODE_STANDARD,
    build_delivery_record,
    make_outcome,
    normalize_anchor,
)


def _redmine_outcome(*, target="%2", duplicate=False):
    anchor = normalize_anchor("redmine", issue="12311", journal="62509")
    return make_outcome(
        status="sent",
        reason="ok",
        receiver="claude",
        target=target,
        anchor=anchor,
        mode=MODE_STANDARD,
        kind="implementation_request",
        notification_marker="[mozyo:handoff:source=redmine:issue=12311:journal=62509:kind=implementation_request:to=claude]",
    )


def _asana_outcome():
    anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
    return make_outcome(
        status="sent",
        reason="ok",
        receiver="codex",
        target="%5",
        anchor=anchor,
        mode=MODE_STANDARD,
        kind="reply",
        notification_marker="[mozyo:handoff:source=asana:task=T1:comment=C1:kind=reply:to=codex]",
    )


class _FakeTransport:
    """Records the (issue_id, notes) it is asked to persist, returns a journal id."""

    def __init__(self, journal_id="99001"):
        self.journal_id = journal_id
        self.calls: list[tuple[str, str]] = []

    def post_issue_note(self, issue_id: str, notes: str) -> str:
        self.calls.append((issue_id, notes))
        return self.journal_id


class _RaisingTransport:
    def __init__(self, *, reason):
        self._reason = reason

    def post_issue_note(self, issue_id: str, notes: str) -> str:
        raise DeliveryTransportError("boom", reason=self._reason)


class _UnexpectedTransport:
    def post_issue_note(self, issue_id: str, notes: str) -> str:
        raise RuntimeError("unexpected non-transport failure")


class DeliveryRecordNoteTest(unittest.TestCase):
    def test_note_built_from_redmine_outcome_carries_issue_anchor(self) -> None:
        outcome = _redmine_outcome()
        body = build_delivery_record(outcome)
        note = build_delivery_record_note(outcome, record_markdown=body)

        self.assertEqual(RECORD_CLASS_DELIVERY, note.record_class)
        self.assertEqual("redmine", note.source)
        self.assertEqual("12311", note.issue_id)
        self.assertIsNone(note.task_id)
        self.assertEqual("claude", note.receiver)
        self.assertEqual("%2", note.target)
        self.assertEqual("sent", note.status)
        self.assertIs(body, note.body)

    def test_note_built_from_asana_outcome_carries_task_anchor(self) -> None:
        outcome = _asana_outcome()
        note = build_delivery_record_note(
            outcome, record_markdown=build_delivery_record(outcome)
        )

        self.assertEqual("asana", note.source)
        self.assertEqual("T1", note.task_id)
        self.assertIsNone(note.issue_id)

    def test_note_without_source_fails_closed(self) -> None:
        # A blocked-before-anchor outcome has no durable target to persist.
        outcome = make_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
            source=None,
        )
        with self.assertRaises(DeliveryRecordError):
            build_delivery_record_note(outcome, record_markdown="x")

    def test_record_class_invariant_rejects_gate_smuggling(self) -> None:
        # A delivery note can never be constructed as a workflow gate / approval.
        with self.assertRaises(DeliveryRecordError):
            DeliveryRecordNote(
                record_class="review_request",
                source="redmine",
                body="x",
                receiver="claude",
                status="sent",
                reason="ok",
                issue_id="12311",
            )

    def test_note_to_dict_has_no_credential_or_body(self) -> None:
        note = build_delivery_record_note(
            _redmine_outcome(), record_markdown="BODY"
        )
        payload = note.to_dict()
        # The credential-free projection never carries the body or a secret.
        self.assertNotIn("body", payload)
        joined = json.dumps(payload).lower()
        for forbidden in ("api_key", "api-key", "token", "x-redmine-api-key", "password"):
            self.assertNotIn(forbidden, joined)


class DeliveryRecordSinkTest(unittest.TestCase):
    def test_null_sink_does_not_persist(self) -> None:
        note = build_delivery_record_note(_redmine_outcome(), record_markdown="b")
        receipt = NullDeliveryRecordSink().persist(note)

        self.assertFalse(receipt.persisted)
        self.assertEqual(PERSIST_DISABLED, receipt.reason)
        self.assertIsNone(receipt.location)

    def test_unsupported_source_sink_fails_closed(self) -> None:
        note = build_delivery_record_note(_asana_outcome(), record_markdown="b")
        receipt = UnsupportedSourceDeliveryRecordSink("asana").persist(note)

        self.assertFalse(receipt.persisted)
        self.assertEqual(PERSIST_UNSUPPORTED_SOURCE, receipt.reason)

    def test_unwired_redmine_sink_reports_provider_unavailable(self) -> None:
        note = build_delivery_record_note(_redmine_outcome(), record_markdown="b")
        receipt = UnwiredDeliveryRecordSink("redmine").persist(note)

        self.assertFalse(receipt.persisted)
        self.assertEqual(PERSIST_PROVIDER_UNAVAILABLE, receipt.reason)

    def test_redmine_sink_persists_via_injected_transport(self) -> None:
        body = build_delivery_record(_redmine_outcome())
        note = build_delivery_record_note(_redmine_outcome(), record_markdown=body)
        transport = _FakeTransport(journal_id="99001")

        receipt = RedmineDeliveryRecordSink(transport).persist(note)

        self.assertTrue(receipt.persisted)
        self.assertEqual(PERSIST_OK, receipt.reason)
        self.assertEqual("redmine:issue=12311:journal=99001", receipt.location)
        # The transport received the issue anchor and the redacted record body.
        self.assertEqual([("12311", body)], transport.calls)

    def test_redmine_sink_refuses_asana_note(self) -> None:
        # Journal vs comment semantics are not mixed: a Redmine sink will not
        # persist an Asana-sourced note.
        note = build_delivery_record_note(_asana_outcome(), record_markdown="b")
        receipt = RedmineDeliveryRecordSink(_FakeTransport()).persist(note)

        self.assertFalse(receipt.persisted)
        self.assertEqual(PERSIST_UNSUPPORTED_SOURCE, receipt.reason)

    def test_redmine_sink_without_issue_anchor_fails_closed(self) -> None:
        note = DeliveryRecordNote(
            record_class=RECORD_CLASS_DELIVERY,
            source="redmine",
            body="b",
            receiver="claude",
            status="sent",
            reason="ok",
            issue_id=None,
        )
        receipt = RedmineDeliveryRecordSink(_FakeTransport()).persist(note)

        self.assertFalse(receipt.persisted)
        self.assertEqual(PERSIST_NO_ANCHOR, receipt.reason)

    def test_transport_failure_surfaces_explicit_reason(self) -> None:
        note = build_delivery_record_note(_redmine_outcome(), record_markdown="b")
        sink = RedmineDeliveryRecordSink(
            _RaisingTransport(reason=PERSIST_CREDENTIAL_MISSING)
        )

        receipt = sink.persist(note)

        self.assertFalse(receipt.persisted)
        self.assertEqual(PERSIST_CREDENTIAL_MISSING, receipt.reason)

    def test_transport_error_normalizes_unknown_reason(self) -> None:
        err = DeliveryTransportError("x", reason="totally-made-up")
        self.assertEqual(PERSIST_TRANSPORT_ERROR, err.reason)

    def test_receipt_json_carries_no_credential(self) -> None:
        receipt = DeliveryReceipt(
            provider="redmine",
            persisted=True,
            reason=PERSIST_OK,
            location="redmine:issue=12311:journal=99001",
        )
        payload = json.loads(receipt.to_json())
        self.assertEqual(RECORD_CLASS_DELIVERY, payload["record_class"])
        lowered = receipt.to_json().lower()
        for forbidden in ("api_key", "token", "password", "x-redmine-api-key"):
            self.assertNotIn(forbidden, lowered)


class DeliveryRecordConditionsTest(unittest.TestCase):
    """AC#5: fix WHEN the duplicate advisory / delivery outcome / receiver
    target identity land in the persisted durable record."""

    def test_duplicate_advisory_persisted_only_when_live_at_send(self) -> None:
        outcome = _redmine_outcome(target="%2")
        dup_rows = ["%9 window=claude lane=main"]
        body = build_delivery_record(outcome, duplicate_lane_panes=dup_rows)
        note = build_delivery_record_note(
            outcome, record_markdown=body, has_duplicate_advisory=True
        )

        self.assertTrue(note.has_duplicate_advisory)
        self.assertIn("Duplicate same-lane pane(s)", note.body)
        self.assertIn("%9", note.body)
        # Receiver target identity is fixed in the persisted record.
        self.assertEqual("%2", note.target)
        self.assertIn("%2", note.body)

    def test_no_duplicate_advisory_when_none_live(self) -> None:
        outcome = _redmine_outcome(target="%2")
        body = build_delivery_record(outcome)
        note = build_delivery_record_note(
            outcome, record_markdown=body, has_duplicate_advisory=False
        )

        self.assertFalse(note.has_duplicate_advisory)
        self.assertNotIn("Duplicate same-lane pane(s)", note.body)

    def test_persisted_body_is_redacted_pasteable_record(self) -> None:
        # The persisted body is the same redacted markdown the CLI prints — no
        # absolute home path leaks into the durable record.
        outcome = _redmine_outcome()
        body = build_delivery_record(outcome)
        note = build_delivery_record_note(outcome, record_markdown=body)

        self.assertNotIn("/Users/", note.body)
        self.assertNotIn("/home/", note.body)
        # It still carries the durable anchor pointer and the delivery outcome.
        self.assertIn("Redmine #12311 journal #62509", note.body)
        self.assertIn("sent", note.body)


class ResolveDeliveryRecordSinkTest(unittest.TestCase):
    def test_disabled_resolves_to_null_sink(self) -> None:
        sink = resolve_delivery_record_sink(enabled=False, source="redmine")
        self.assertIsInstance(sink, NullDeliveryRecordSink)

    def test_redmine_without_transport_resolves_to_unwired(self) -> None:
        sink = resolve_delivery_record_sink(enabled=True, source="redmine")
        self.assertIsInstance(sink, UnwiredDeliveryRecordSink)

    def test_redmine_with_transport_resolves_to_redmine_sink(self) -> None:
        sink = resolve_delivery_record_sink(
            enabled=True, source="redmine", redmine_transport=_FakeTransport()
        )
        self.assertIsInstance(sink, RedmineDeliveryRecordSink)

    def test_asana_resolves_to_unsupported(self) -> None:
        sink = resolve_delivery_record_sink(enabled=True, source="asana")
        self.assertIsInstance(sink, UnsupportedSourceDeliveryRecordSink)


class PersistDeliveryCliWiringTest(unittest.TestCase):
    """The opt-in `--persist-delivery` flag and its best-effort emission."""

    def _run_send(self, argv: list[str]):
        parser = build_parser()
        args = parser.parse_args(argv)
        pane_text = {"buf": ""}

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text["buf"]

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text["buf"] += tmux_args[-1]
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        pane_value = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value=None), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()):
            result = args.func(args)
        return result, stdout.getvalue()

    def _base_argv(self) -> list[str]:
        return [
            "handoff", "send",
            "--to", "claude",
            "--source", "redmine",
            "--kind", "implementation_request",
            "--issue", "12311",
            "--journal", "62509",
            "--target", "%2",
            "--mode", "standard",
            "--submit-delay", "0",
        ]

    def test_persist_delivery_emits_receipt_without_altering_send(self) -> None:
        result, stdout = self._run_send(self._base_argv() + ["--persist-delivery"])

        self.assertEqual(0, result)
        # The send still succeeds: the outcome JSON is present.
        receipts = [
            json.loads(line)
            for line in stdout.splitlines()
            if line.strip().startswith("{") and "persisted" in line
        ]
        self.assertEqual(1, len(receipts))
        receipt = receipts[0]
        # Live transport is deferred → fail-closed provider_unavailable, but the
        # delivery itself was not blocked or altered.
        self.assertFalse(receipt["persisted"])
        self.assertEqual(PERSIST_PROVIDER_UNAVAILABLE, receipt["reason"])
        self.assertEqual(RECORD_CLASS_DELIVERY, receipt["record_class"])

    def test_default_send_emits_no_receipt(self) -> None:
        result, stdout = self._run_send(self._base_argv())

        self.assertEqual(0, result)
        self.assertNotIn("persisted", stdout)
        self.assertNotIn("Durable delivery record", stdout)

    def test_persist_best_effort_swallows_unexpected_sink_error(self) -> None:
        # An unexpected (non-transport) sink failure must not break the send;
        # it is reported as a transport_error receipt.
        argv = self._base_argv() + ["--persist-delivery"]

        def boom(**_kwargs):
            raise RuntimeError("sink resolution blew up")

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink.resolve_delivery_record_sink",
            side_effect=boom,
        ):
            result, stdout = self._run_send(argv)

        self.assertEqual(0, result)
        receipts = [
            json.loads(line)
            for line in stdout.splitlines()
            if line.strip().startswith("{") and "persisted" in line
        ]
        self.assertEqual(1, len(receipts))
        self.assertFalse(receipts[0]["persisted"])
        self.assertEqual(PERSIST_TRANSPORT_ERROR, receipts[0]["reason"])

    def test_live_opt_in_wires_transport_and_persists(self) -> None:
        # Redmine #12347: with the persistence flag, the CLI builds the live
        # transport from the env for a Redmine source and injects it into the
        # sink. Here the factory is patched to a fake transport, so the persisted
        # receipt reports the real RedmineDeliveryRecordSink success path.
        transport = _FakeTransport(journal_id="77001")
        argv = self._base_argv() + ["--persist-delivery"]
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport."
            "redmine_delivery_transport_from_env",
            return_value=transport,
        ):
            result, stdout = self._run_send(argv)

        self.assertEqual(0, result)
        receipts = [
            json.loads(line)
            for line in stdout.splitlines()
            if line.strip().startswith("{") and "persisted" in line
        ]
        self.assertEqual(1, len(receipts))
        self.assertTrue(receipts[0]["persisted"])
        self.assertEqual(PERSIST_OK, receipts[0]["reason"])
        self.assertEqual(
            "redmine:issue=12311:journal=77001", receipts[0]["location"]
        )
        # The transport was actually called with the issue anchor.
        self.assertEqual(1, len(transport.calls))
        self.assertEqual("12311", transport.calls[0][0])

    def test_live_opt_in_credential_missing_surfaces_through_wiring(self) -> None:
        # A live transport that fails closed (e.g. no key in the trusted env)
        # surfaces its explicit reason through the wiring, without breaking send.
        argv = self._base_argv() + ["--persist-delivery"]
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport."
            "redmine_delivery_transport_from_env",
            return_value=_RaisingTransport(reason=PERSIST_CREDENTIAL_MISSING),
        ):
            result, stdout = self._run_send(argv)

        self.assertEqual(0, result)
        receipts = [
            json.loads(line)
            for line in stdout.splitlines()
            if line.strip().startswith("{") and "persisted" in line
        ]
        self.assertEqual(1, len(receipts))
        self.assertFalse(receipts[0]["persisted"])
        self.assertEqual(PERSIST_CREDENTIAL_MISSING, receipts[0]["reason"])

    def test_live_opt_in_does_not_auto_journal_record_command(self) -> None:
        # The #12311 invariant holds through the live #12347 path: the persisted
        # note body omits the user-supplied free-text --record-command even when
        # a live transport is wired. Abstract placeholders only.
        sentinel = "deploy --token DROP-TOKEN-SENTINEL --root /workspace/project-alpha"
        transport = _FakeTransport(journal_id="77002")
        argv = self._base_argv() + [
            "--persist-delivery",
            "--record-command",
            sentinel,
        ]
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport."
            "redmine_delivery_transport_from_env",
            return_value=transport,
        ):
            result, stdout = self._run_send(argv)

        self.assertEqual(0, result)
        # Printed stdout record keeps the command for audit-replay ...
        self.assertIn(sentinel, stdout)
        # ... but the body handed to the live transport omits it entirely.
        self.assertEqual(1, len(transport.calls))
        persisted_body = transport.calls[0][1]
        self.assertNotIn(sentinel, persisted_body)
        self.assertNotIn("DROP-TOKEN-SENTINEL", persisted_body)
        self.assertNotIn("- Command:", persisted_body)

    def test_record_command_not_auto_journaled_but_kept_in_printed_record(self) -> None:
        # Finding 1 (j#62549): user-supplied --record-command can carry a
        # private path / credential-shaped argument. It must stay in the printed
        # stdout record for human audit-replay, but the opt-in durable sink must
        # never auto-journal it. Abstract placeholders only.
        sentinel = "deploy --token DROP-TOKEN-SENTINEL --root /workspace/project-alpha"
        captured: dict[str, object] = {}

        class _CapturingSink:
            name = "capture"

            def persist(self, note):
                captured["note"] = note
                return DeliveryReceipt(
                    provider="capture",
                    persisted=True,
                    reason=PERSIST_OK,
                    location="cap:1",
                )

        argv = self._base_argv() + [
            "--persist-delivery",
            "--record-command",
            sentinel,
        ]
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink.resolve_delivery_record_sink",
            return_value=_CapturingSink(),
        ):
            result, stdout = self._run_send(argv)

        self.assertEqual(0, result)
        # Printed stdout record keeps the command for audit-replay ...
        self.assertIn(sentinel, stdout)
        # ... but the persisted note body omits the free text entirely.
        note = captured.get("note")
        self.assertIsNotNone(note)
        self.assertNotIn(sentinel, note.body)
        self.assertNotIn("DROP-TOKEN-SENTINEL", note.body)
        self.assertNotIn("- Command:", note.body)


if __name__ == "__main__":
    unittest.main()
