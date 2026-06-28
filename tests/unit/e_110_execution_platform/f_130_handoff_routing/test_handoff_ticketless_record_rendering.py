"""Tests for the ticketless no-anchor record-rendering split (Redmine #12750).

#12750 factors the ticketless consultation / callback / work-intake anchor models
and their durable-record rendering out of the ~1900-line ``handoff.py`` into
``domain/ticketless_anchors`` and ``domain/ticketless_record_rendering``. These
tests pin the two invariants the issue's acceptance criteria call out:

1. The Redmine-anchored vs ticketless-no-anchor boundary stays clean — the moved
   symbols are still reachable from ``handoff`` (same class objects), and
   ``SOURCE_TICKETLESS`` is NOT a member of the anchored ``SOURCES`` set, so a
   regular ``handoff send`` / ``reply`` can never select it.
2. The no-anchor rails never fabricate a Redmine anchor — the marker, structured
   outcome, and rendered delivery record carry the ticketless source and state
   plainly that there is no Redmine issue/journal.

Behavioral regression of the rendered text itself is covered by the existing
``test_handoff_ticketless_{callback,consultation,work_intake}`` suites, which
exercise the same renderers through ``build_delivery_record``.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import handoff
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    SOURCES,
    SOURCE_REDMINE,
    SOURCE_TICKETLESS,
    TicketlessAnchor,
    TicketlessConsultationAnchor,
    TicketlessWorkIntakeAnchor,
    build_delivery_record,
    build_marker,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import (
    ticketless_anchors,
    ticketless_record_rendering as rendering,
)


class TicketlessSplitBoundaryTest(unittest.TestCase):
    """The moved symbols stay reachable from ``handoff`` and keep the boundary."""

    def test_anchor_classes_reexported_with_identity_preserved(self) -> None:
        # commands.py and the existing ticketless tests import these from
        # ``handoff``; the re-export must be the SAME class object so
        # ``isinstance`` checks in ``build_notification_body`` still match.
        self.assertIs(handoff.TicketlessAnchor, ticketless_anchors.TicketlessAnchor)
        self.assertIs(
            handoff.TicketlessConsultationAnchor,
            ticketless_anchors.TicketlessConsultationAnchor,
        )
        self.assertIs(
            handoff.TicketlessWorkIntakeAnchor,
            ticketless_anchors.TicketlessWorkIntakeAnchor,
        )
        self.assertIs(
            handoff.SOURCE_TICKETLESS, ticketless_anchors.SOURCE_TICKETLESS
        )

    def test_ticketless_source_is_not_an_anchored_source(self) -> None:
        # The core invariant the split must preserve: a regular anchored
        # send/reply (which validates ``source in SOURCES``) can never select
        # the ticketless source to bypass the anchor requirement.
        self.assertNotIn(SOURCE_TICKETLESS, SOURCES)
        self.assertEqual(sorted(SOURCES), ["asana", "redmine"])

    def test_reexported_names_are_listed_in_all(self) -> None:
        for name in (
            "SOURCE_TICKETLESS",
            "TicketlessAnchor",
            "TicketlessConsultationAnchor",
            "TicketlessWorkIntakeAnchor",
        ):
            self.assertIn(name, handoff.__all__)


class TicketlessNoAnchorFabricationTest(unittest.TestCase):
    """The no-anchor rails must not fabricate a Redmine anchor anywhere."""

    def _assert_no_redmine_anchor(self, anchor) -> None:
        self.assertEqual(anchor.source, SOURCE_TICKETLESS)

        # marker rides ``source=ticketless`` and carries no redmine issue/journal
        marker = build_marker(anchor, "reply", "codex")
        self.assertIn(f"source={SOURCE_TICKETLESS}", marker)
        self.assertNotIn(f"source={SOURCE_REDMINE}", marker)
        self.assertNotIn("issue=", marker)
        self.assertNotIn("journal=", marker)

        # structured outcome carries the ticketless source and a redmine-key-free
        # anchor dict — no fabricated issue/journal survives onto the wire.
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%1",
            anchor=anchor,
            mode="standard",
            kind="reply",
            notification_marker=marker,
        )
        self.assertEqual(outcome.source, SOURCE_TICKETLESS)
        self.assertIsNotNone(outcome.anchor)
        self.assertEqual(outcome.anchor["source"], SOURCE_TICKETLESS)
        self.assertNotIn("issue", outcome.anchor)
        self.assertNotIn("journal", outcome.anchor)

        # rendered durable record states plainly there is no Redmine anchor and
        # never prints a ``Redmine #<id>`` pointer for the anchor line.
        record = build_delivery_record(outcome)
        self.assertIn("no Redmine anchor", record)
        self.assertNotIn("- Durable anchor: Redmine #", record)

    def test_callback_anchor_does_not_fabricate_redmine_anchor(self) -> None:
        self._assert_no_redmine_anchor(
            TicketlessAnchor(classification="no_dispatch", dispatch_decision="hand_back_to_caller")
        )

    def test_consultation_anchor_does_not_fabricate_redmine_anchor(self) -> None:
        self._assert_no_redmine_anchor(
            TicketlessConsultationAnchor(
                consultation_kind="design", callback_to_role="grandparent_coordinator"
            )
        )

    def test_work_intake_anchor_does_not_fabricate_redmine_anchor(self) -> None:
        self._assert_no_redmine_anchor(
            TicketlessWorkIntakeAnchor(
                work_shape="feature", callback_to_role="project_gateway"
            )
        )


class TicketlessRecordRenderingPureTest(unittest.TestCase):
    """The factored pure renderers keep their None-fallback + token contract."""

    def test_each_renderer_returns_dash_line_for_no_payload(self) -> None:
        self.assertEqual(
            rendering.ticketless_callback_lines(None), ["- Ticketless callback: —"]
        )
        self.assertEqual(
            rendering.ticketless_consultation_lines(None),
            ["- Ticketless consultation: —"],
        )
        self.assertEqual(
            rendering.ticketless_work_intake_lines(None),
            ["- Ticketless work-intake: —"],
        )

    def test_anchor_pointer_discriminates_rail_by_field(self) -> None:
        self.assertIn(
            "consultation",
            rendering.ticketless_anchor_pointer({"consultation_kind": "design"}),
        )
        self.assertIn(
            "work-intake",
            rendering.ticketless_anchor_pointer({"work_shape": "feature"}),
        )
        # default (callback) when neither forward-rail field is present
        self.assertIn(
            "callback",
            rendering.ticketless_anchor_pointer({"classification": "no_dispatch"}),
        )
        # every variant names "no Redmine anchor"
        for payload in (
            {"consultation_kind": "design"},
            {"work_shape": "feature"},
            {"classification": "no_dispatch"},
        ):
            self.assertIn(
                "no Redmine anchor", rendering.ticketless_anchor_pointer(payload)
            )

    def test_callback_lines_render_fixed_tokens(self) -> None:
        lines = rendering.ticketless_callback_lines(
            {
                "classification": "anchor_required",
                "dispatch_decision": "anchor_required_before_worker",
                "redmine_anchor_required": True,
                "next_action_owner": "receiver",
                "callback_reason": "consultation_result",
                "read_contract": "structured_fields_are_durable_record",
            }
        )
        joined = "\n".join(lines)
        self.assertIn("classification `anchor_required`", joined)
        self.assertIn("Redmine anchor required (next worker phase): `true`", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
