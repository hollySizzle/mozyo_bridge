"""The dispatch marker producer's PUBLIC contract: what a clean call renders, and reads back.

These are contract and change-safety claims, not the recurrence pin for one defect — which is why
they live here and not beside the #14717 regression file (``tests-placement-discovery-policy.md``
``### regressions`` R3-b: a regressions file's tests must ALL be that symptom's recurrence
detection, and a file mixing the two kinds of claim falls to this bucket instead).

What is pinned here:

- the exact marker a clean call renders, byte for byte, from BOTH types a caller may hold for
  ``lane_generation`` (an ``int`` from an in-repo caller, a ``str`` from argv);
- the round trip: what the producer writes, the STRICT reader every authority consumer shares can
  read, and the anchor / round consumers resolve back to the values the caller passed;
- that tokens which merely LOOK unusual — ``a=b``, ``%2F``, ``ci(main)``, non-ASCII — still render
  and still resolve, so the #14717 tightening cannot quietly become a narrowing of what a
  legitimate lane id may be. ``a=b`` is the sharp one: the body scanner partitions each component
  on its FIRST ``=``, so an ``=`` inside a value round-trips and refusing it would be a narrowing
  rather than a grammar rule;
- that the dispatch marker's ``lane`` is judged by the SAME authority the evidence envelope's
  ``lane`` is, and its ``lane_generation`` by the same predicate pair — the drift #14717 closed is
  pinned as an identity, not re-described.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
    render_lane_envelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
    MAX_CANONICAL_DECIMAL_VALUE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    DISPATCH_KIND_IMPLEMENTATION_REQUEST,
    RedmineJournalEntry,
    dispatch_entry_journals,
    dispatch_generations,
    dispatch_lanes,
    render_dispatch_marker,
    render_dispatch_note,
    resolve_dispatch_entry_journal,
    strict_marker_fields_in_note,
)
from tests.support.hibernate_evidence_producer_corpus import VALID_TOKENS

LANE = "lane-abc"


class CleanCallShapeTest(unittest.TestCase):
    def test_the_rendered_marker_is_exactly_this(self):
        self.assertEqual(
            render_dispatch_marker("lane-a", 1),
            "[mozyo:workflow-event:kind=implementation_request:lane=lane-a:lane_generation=1]",
        )

    def test_both_generation_types_render_the_same_bytes(self):
        # ``workflow dispatch-ir`` gets ``--generation`` off argv as a ``str``; the in-repo callers
        # pass an ``int``. One marker, either way — otherwise the same round would have two
        # durable spellings and the anchor comparison would depend on who wrote it.
        for generation in (7, "7"):
            with self.subTest(generation=generation):
                self.assertEqual(
                    render_dispatch_marker(LANE, generation),
                    render_dispatch_marker(LANE, 7),
                )

    def test_the_note_is_the_body_then_the_marker(self):
        marker = render_dispatch_marker(LANE, 3)
        self.assertEqual(render_dispatch_note("## IR", lane=LANE, lane_generation=3), f"## IR\n\n{marker}")
        self.assertEqual(render_dispatch_note("", lane=LANE, lane_generation=3), marker)

    def test_the_widest_admissible_generation_renders(self):
        self.assertIn(
            f"lane_generation={MAX_CANONICAL_DECIMAL_VALUE}]",
            render_dispatch_marker(LANE, MAX_CANONICAL_DECIMAL_VALUE),
        )


class RoundTripTest(unittest.TestCase):
    def test_what_the_producer_writes_the_strict_reader_reads_back(self):
        for token in VALID_TOKENS:
            body = render_dispatch_note("## IR", lane=token, lane_generation=42)
            with self.subTest(lane=token):
                read = strict_marker_fields_in_note(body)
                self.assertEqual(
                    read,
                    (
                        (
                            "workflow-event",
                            {
                                "kind": DISPATCH_KIND_IMPLEMENTATION_REQUEST,
                                "lane": token,
                                "lane_generation": "42",
                            },
                        ),
                    ),
                )

    def test_every_dispatch_consumer_resolves_the_values_the_caller_passed(self):
        for token in VALID_TOKENS:
            entry = RedmineJournalEntry(
                issue_id="14717", journal_id="79600",
                notes=render_dispatch_note("## IR", lane=token, lane_generation=7),
            )
            with self.subTest(lane=token):
                self.assertEqual(
                    dispatch_entry_journals([entry], lane=token, lane_generation=7), ("79600",)
                )
                self.assertEqual(resolve_dispatch_entry_journal([entry], lane=token, lane_generation=7), "79600")
                self.assertEqual(dispatch_generations([entry], lane=token), (7,))
                self.assertEqual(dispatch_lanes([entry]), (token,))

    def test_an_equals_inside_a_lane_survives_the_round_trip(self):
        # The scanner partitions on the FIRST ``=``, so this is a value and not a second field.
        # Pinned on its own because it is the token a stricter-looking value rule would have cost.
        entry = RedmineJournalEntry(
            issue_id="14717", journal_id="79600",
            notes=render_dispatch_note("## IR", lane="a=b", lane_generation=1),
        )
        self.assertEqual(dispatch_entry_journals([entry], lane="a=b", lane_generation=1), ("79600",))


class OneAuthorityPerFieldTest(unittest.TestCase):
    """The two renderers of the lane envelope's fields ask the same questions."""

    def test_the_dispatch_lane_and_the_envelope_lane_accept_the_same_tokens(self):
        # Not "both are strict" but "both draw the same line": the acceptance sets are compared,
        # so a later change to either authority alone shows up here rather than in a durable record.
        probes = VALID_TOKENS + (" pad ", "pad ", "a:b", "a]b", "a[b", "a\nb", "a\xa0b", "", "a b")
        for token in probes:
            dispatch_ok = self._renders(lambda: render_dispatch_marker(token, 1))
            envelope_ok = self._renders(
                lambda: render_lane_envelope(
                    LaneEvidenceEnvelope(workspace="ws-1", lane=token, lane_generation=1)
                )
            )
            with self.subTest(lane=token):
                self.assertEqual(dispatch_ok, envelope_ok)

    def test_the_dispatch_generation_and_the_envelope_generation_accept_the_same_ints(self):
        for generation in (1, 42, MAX_CANONICAL_DECIMAL_VALUE, 0, -1, True, MAX_CANONICAL_DECIMAL_VALUE + 1):
            dispatch_ok = self._renders(lambda: render_dispatch_marker(LANE, generation))
            envelope_ok = self._renders(
                lambda: render_lane_envelope(
                    LaneEvidenceEnvelope(workspace="ws-1", lane=LANE, lane_generation=generation)
                )
            )
            with self.subTest(generation=generation):
                self.assertEqual(dispatch_ok, envelope_ok)

    def test_the_shared_generation_requirement_returns_the_caller_s_own_token(self):
        # Imported INSIDE the method: a new symbol at module scope would make the whole file an
        # ImportError on the lane base, so "every method fails on base" would be vacuously true
        # and the base measurement of this file would say nothing (#14694 lesson).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            require_canonical_generation,
        )

        self.assertEqual(require_canonical_generation("42"), "42")
        self.assertEqual(require_canonical_generation(42), "42")

    @staticmethod
    def _renders(call) -> bool:
        try:
            call()
        except ValueError:
            return False
        return True


if __name__ == "__main__":
    unittest.main()
