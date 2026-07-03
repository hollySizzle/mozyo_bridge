"""Fake-port + pure-decision specs for the handoff delivery boundary (#12981).

These exercise the ``handoff_delivery_command`` boundary directly — the pure
:func:`marker_timeout_guidance_lines` with a plain assertion, and the
:class:`DeliveryRecordUseCase` bodies through a synthetic
:class:`DeliveryRecordOps` fake — with no live tmux / Redmine, no
``orchestrate_handoff``. They pin, in isolation, the residual carved out of
``commands.py``:

- ``emit_outcome`` — record_format routing (both/text/json) + the ``die`` seam on
  a bad format;
- ``emit_receipt`` — persisted vs. not-persisted rendering per format;
- ``maybe_persist`` — the ``--persist-delivery`` opt-in gate, the source-routed
  live-transport request, the sink ``persist`` call, and the best-effort
  ``transport_error`` receipt on any sink failure;
- ``emit_marker_timeout_guidance`` — the three stderr hint lines.

The #13123 facade cleanup moved the remaining ``commands.py`` delivery-record
helper tail here; the added specs pin the pure args projections
(``submit_lines_for`` / ``record_command_from_args``), the
``record_format_from_args`` validation through the ``die`` port, and the
``commands.*`` re-export identity.

The end-to-end behavior over the real ``commands.*`` helpers +
``orchestrate_handoff`` stays pinned by the ``handoff`` CLI characterization tests
under ``tests/integration/.../f_130_handoff_routing/`` and
``tests/integration/adapter_provider/test_handoff_delivery_record.py`` /
``test_handoff_delivery_sink.py``; this file pins the extracted bodies in
isolation, which is the OOP-first carve's payoff.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from mozyo_bridge.application.handoff_delivery_command import (
    DeliveryRecordUseCase,
    marker_timeout_guidance_lines,
    record_command_from_args,
    submit_lines_for,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
    PERSIST_OK,
    PERSIST_TRANSPORT_ERROR,
    DeliveryReceipt,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RECORD_FORMAT_BOTH,
    RECORD_FORMAT_JSON,
    RECORD_FORMAT_TEXT,
    SOURCE_REDMINE,
    make_outcome,
)


def _outcome(*, source: str | None = SOURCE_REDMINE, status: str = "sent"):
    """A real ``DeliveryOutcome`` so the pure ``build_delivery_record`` renders."""
    return make_outcome(
        status=status,
        reason="ok" if status == "sent" else "marker_timeout",
        receiver="codex",
        target="%42",
        anchor=None,
        mode="queue-enter",
        kind="reply",
        notification_marker=None,
        source=source,
    )


class _FakeSink:
    def __init__(self, *, receipt=None, raises: Exception | None = None) -> None:
        self.notes: list = []
        self._receipt = receipt
        self._raises = raises

    def persist(self, note):
        self.notes.append(note)
        if self._raises is not None:
            raise self._raises
        return self._receipt


class _FakeDeliveryRecordOps:
    def __init__(self, *, transport=object(), sink: _FakeSink | None = None) -> None:
        self.died: list[str] = []
        self.transport_requested = 0
        self.resolve_calls: list[dict] = []
        self._transport = transport
        self._sink = sink or _FakeSink(
            receipt=DeliveryReceipt(provider="redmine", persisted=True, reason=PERSIST_OK)
        )

    def die(self, message: str) -> None:
        self.died.append(message)
        raise SystemExit(2)

    def redmine_delivery_transport_from_env(self):
        self.transport_requested += 1
        return self._transport

    def resolve_delivery_record_sink(self, *, enabled, source, redmine_transport):
        self.resolve_calls.append(
            {"enabled": enabled, "source": source, "redmine_transport": redmine_transport}
        )
        return self._sink


class EmitOutcomeTest(unittest.TestCase):
    def test_both_prints_record_blank_then_json(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        outcome = _outcome()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.emit_outcome(outcome, record_format=RECORD_FORMAT_BOTH)
        out = buf.getvalue()
        # Multi-line markdown record first, a blank separator, JSON outcome last.
        self.assertIn("- Status:", out)
        self.assertTrue(out.rstrip().endswith(outcome.to_json()))
        self.assertIn("\n\n" + outcome.to_json() + "\n", out)

    def test_json_only_prints_single_json_line(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        outcome = _outcome()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.emit_outcome(outcome, record_format=RECORD_FORMAT_JSON)
        self.assertEqual(outcome.to_json() + "\n", buf.getvalue())

    def test_text_only_prints_record_without_json(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        outcome = _outcome()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.emit_outcome(outcome, record_format=RECORD_FORMAT_TEXT)
        out = buf.getvalue()
        self.assertIn("- Status:", out)
        self.assertNotIn(outcome.to_json(), out)

    def test_bad_record_format_routes_to_die_and_stops(self) -> None:
        ops = _FakeDeliveryRecordOps()
        uc = DeliveryRecordUseCase(ops)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                uc.emit_outcome(_outcome(), record_format="bogus")
        self.assertEqual(1, len(ops.died))
        self.assertIn("--record-format must be one of", ops.died[0])
        self.assertEqual("", buf.getvalue())  # nothing printed after die


class EmitReceiptTest(unittest.TestCase):
    def test_persisted_summary_and_json_in_both(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        receipt = DeliveryReceipt(
            provider="redmine",
            persisted=True,
            reason=PERSIST_OK,
            location="redmine:issue=1:journal=2",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.emit_receipt(receipt, record_format=RECORD_FORMAT_BOTH)
        out = buf.getvalue()
        self.assertIn("- Durable delivery record persisted to redmine:issue=1:journal=2", out)
        self.assertTrue(out.rstrip().endswith(receipt.to_json()))

    def test_not_persisted_reason_summary(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        receipt = DeliveryReceipt(provider="redmine", persisted=False, reason="provider_unavailable")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.emit_receipt(receipt, record_format=RECORD_FORMAT_TEXT)
        self.assertIn(
            "- Durable delivery record not persisted (reason: provider_unavailable)",
            buf.getvalue(),
        )

    def test_json_only_prints_receipt_json(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        receipt = DeliveryReceipt(provider="redmine", persisted=False, reason="staged")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.emit_receipt(receipt, record_format=RECORD_FORMAT_JSON)
        self.assertEqual(receipt.to_json() + "\n", buf.getvalue())


class MaybePersistTest(unittest.TestCase):
    def test_noop_without_opt_in(self) -> None:
        ops = _FakeDeliveryRecordOps()
        uc = DeliveryRecordUseCase(ops)
        args = argparse.Namespace()  # no persist_delivery attribute
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.maybe_persist(
                args, _outcome(), duplicate_lane_panes=None, record_format=RECORD_FORMAT_BOTH
            )
        self.assertEqual("", buf.getvalue())
        self.assertEqual([], ops.resolve_calls)
        self.assertEqual(0, ops.transport_requested)

    def test_redmine_source_requests_live_transport_and_persists(self) -> None:
        sink = _FakeSink(
            receipt=DeliveryReceipt(
                provider="redmine",
                persisted=True,
                reason=PERSIST_OK,
                location="redmine:issue=9:journal=9",
            )
        )
        ops = _FakeDeliveryRecordOps(transport="TRANSPORT", sink=sink)
        uc = DeliveryRecordUseCase(ops)
        args = argparse.Namespace(persist_delivery=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.maybe_persist(
                args,
                _outcome(source=SOURCE_REDMINE),
                duplicate_lane_panes=None,
                record_format=RECORD_FORMAT_TEXT,
            )
        self.assertEqual(1, ops.transport_requested)
        self.assertEqual(1, len(ops.resolve_calls))
        self.assertEqual("redmine", ops.resolve_calls[0]["source"])
        self.assertEqual("TRANSPORT", ops.resolve_calls[0]["redmine_transport"])
        self.assertTrue(ops.resolve_calls[0]["enabled"])
        self.assertEqual(1, len(sink.notes))
        self.assertIn("persisted to redmine:issue=9:journal=9", buf.getvalue())

    def test_non_redmine_source_skips_transport(self) -> None:
        ops = _FakeDeliveryRecordOps(sink=_FakeSink(receipt=DeliveryReceipt(provider="asana", persisted=False, reason="staged")))
        uc = DeliveryRecordUseCase(ops)
        args = argparse.Namespace(persist_delivery=True)
        with contextlib.redirect_stdout(io.StringIO()):
            uc.maybe_persist(
                args,
                _outcome(source="asana"),
                duplicate_lane_panes=None,
                record_format=RECORD_FORMAT_JSON,
            )
        self.assertEqual(0, ops.transport_requested)
        self.assertEqual("asana", ops.resolve_calls[0]["source"])
        self.assertIsNone(ops.resolve_calls[0]["redmine_transport"])

    def test_sink_failure_yields_transport_error_receipt(self) -> None:
        ops = _FakeDeliveryRecordOps(sink=_FakeSink(raises=RuntimeError("boom")))
        uc = DeliveryRecordUseCase(ops)
        args = argparse.Namespace(persist_delivery=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            uc.maybe_persist(
                args,
                _outcome(source=SOURCE_REDMINE),
                duplicate_lane_panes=None,
                record_format=RECORD_FORMAT_TEXT,
            )
        # Best-effort: the sink error is swallowed into a transport_error receipt.
        self.assertIn(
            f"- Durable delivery record not persisted (reason: {PERSIST_TRANSPORT_ERROR})",
            buf.getvalue(),
        )


class SubmitLinesForTest(unittest.TestCase):
    def test_no_submit_intent_yields_none(self) -> None:
        # A normal `handoff send` / `reply` has no q-enter telemetry, so its
        # record stays byte-identical (Redmine #12705).
        self.assertIsNone(submit_lines_for(argparse.Namespace(), _outcome()))
        self.assertIsNone(
            submit_lines_for(argparse.Namespace(submit_intent=""), _outcome())
        )

    def test_intent_renders_submit_lines_with_delivery_id(self) -> None:
        args = argparse.Namespace(
            submit_intent="reply", submit_delivery_id="q-abc123"
        )
        lines = submit_lines_for(args, _outcome())
        assert lines is not None
        self.assertIn("intent `reply`", lines[0])
        self.assertIn("delivery id `q-abc123`", lines[0])
        self.assertTrue(any("Composer residue" in line for line in lines))

    def test_missing_delivery_id_falls_back_to_em_dash(self) -> None:
        args = argparse.Namespace(submit_intent="worker_dispatch")
        lines = submit_lines_for(args, _outcome())
        assert lines is not None
        self.assertIn("delivery id `—`", lines[0])


class RecordArgsProjectionTest(unittest.TestCase):
    def test_record_command_absent_or_empty_is_none(self) -> None:
        self.assertIsNone(record_command_from_args(argparse.Namespace()))
        self.assertIsNone(
            record_command_from_args(argparse.Namespace(record_command=""))
        )

    def test_record_command_is_stringified(self) -> None:
        self.assertEqual(
            "mozyo-bridge handoff send",
            record_command_from_args(
                argparse.Namespace(record_command="mozyo-bridge handoff send")
            ),
        )

    def test_record_format_defaults_to_both(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        self.assertEqual(RECORD_FORMAT_BOTH, uc.record_format_from_args(argparse.Namespace()))
        self.assertEqual(
            RECORD_FORMAT_BOTH,
            uc.record_format_from_args(argparse.Namespace(record_format=None)),
        )

    def test_record_format_passes_valid_value_through(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        self.assertEqual(
            RECORD_FORMAT_JSON,
            uc.record_format_from_args(argparse.Namespace(record_format=RECORD_FORMAT_JSON)),
        )

    def test_bad_record_format_routes_to_die(self) -> None:
        ops = _FakeDeliveryRecordOps()
        uc = DeliveryRecordUseCase(ops)
        with self.assertRaises(SystemExit):
            uc.record_format_from_args(argparse.Namespace(record_format="bogus"))
        self.assertEqual(1, len(ops.died))
        self.assertIn("--record-format must be one of", ops.died[0])


class CommandsReExportTest(unittest.TestCase):
    def test_commands_re_exports_are_same_objects(self) -> None:
        from mozyo_bridge.application import commands
        from mozyo_bridge.application import handoff_delivery_command as hdc

        self.assertIs(commands._emit_outcome, hdc.deliver_outcome)
        self.assertIs(commands._emit_receipt, hdc.deliver_receipt)
        self.assertIs(
            commands._maybe_persist_delivery_record, hdc.maybe_persist_delivery_record
        )
        self.assertIs(commands._record_format_from_args, hdc.record_format_from_args)
        self.assertIs(commands._record_command_from_args, hdc.record_command_from_args)
        self.assertIs(commands._submit_lines_for, hdc.submit_lines_for)


class MarkerTimeoutGuidanceTest(unittest.TestCase):
    def test_pure_lines_carry_receiver_and_cap(self) -> None:
        lines = marker_timeout_guidance_lines("codex", 3)
        self.assertEqual(3, len(lines))
        self.assertIn("`mozyo-bridge read codex`", lines[0])
        self.assertIn("up to 3 attempts", lines[0])
        self.assertIn("separate budgets", lines[1])
        self.assertIn("only after the 3-attempt --no-submit budget is exhausted", lines[2])
        # The `Notification fails` reference stays on one flat line (no stray wrap).
        self.assertIn("may the preset's `Notification fails` branch fire", lines[2])

    def test_emit_writes_lines_to_stderr(self) -> None:
        uc = DeliveryRecordUseCase(_FakeDeliveryRecordOps())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            uc.emit_marker_timeout_guidance("claude")
        self.assertEqual("", out.getvalue())
        stderr = err.getvalue()
        self.assertEqual(3, len(stderr.rstrip("\n").split("\n")))
        self.assertIn("`mozyo-bridge read claude`", stderr)


if __name__ == "__main__":
    unittest.main()
