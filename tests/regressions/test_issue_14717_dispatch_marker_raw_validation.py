"""Redmine #14717: the dispatch marker producer must judge its raw values, not a rendering of them.

``render_dispatch_marker`` was the last renderer of the lane envelope's ``lane`` /
``lane_generation`` still built on ``str(value or "").strip()`` — the exact line #14694 removed
from every hibernate-evidence producer, left in place on the one marker the reconciler resolves a
dispatch ANCHOR from. #14694's review j#93882 and Design Answer j#93945 routed it here rather than
into that correction; its R5 Implementation Done j#94004 / Review Request j#94008 re-listed it.

Every symptom below was measured on the unfixed head before it was fixed, not recalled from the
report:

- ``lane=' r1 '`` / ``lane_generation=' 7 '`` were trimmed into clean canonical tokens, so the
  durable anchor named a round the caller had not written and nothing said so;
- ``lane=None`` / ``False`` / ``0`` rendered ``lane=`` — ``or ""`` turning a wrong TYPE into a
  wrong VALUE — and an arbitrary object whose ``__str__`` said ``r1`` rendered ``lane=r1``;
- ``lane_generation`` of ``0``, ``-5``, ``1.5``, ``True``, ``abc`` and ``٣`` all rendered, each one
  a generation the central `### Hibernate Evidence Marker Contract` calls a producer error
  ("非正 generation ... は、書き込み時点で producer error として拒否する");
- ``lane_generation='1]junk'`` closed the marker early, so the note read back as a CLEAN canonical
  dispatch for generation ``1``: the anchor a reconciler resolves was not the round asked for;
- ``lane`` carrying ``]`` and a newline made ``render_dispatch_note`` emit a note whose SECOND line
  was a forged ``[mozyo:workflow-event:gate=implementation_done…]``, which
  ``extract_markers_from_note`` read as a gate-bearing marker. A producer documented to carry NO
  gate could write a callback-required gate into a durable Redmine journal — the raw value did not
  merely reach the record, it widened what the record was allowed to say;
- the CONSUMER half of the same field: ``dispatch_generations`` asked ``int(raw)`` directly, so
  ``lane_generation=٣`` was round ``3`` and ``01`` was round ``1`` — the shape review j#94247
  blocked #14694 on, left on the parser after the producer beside it was hardened;
- the shared ``validate_marker_field_value`` answered its own rules about a string it had just
  invented: an object whose ``__str__`` said ``r1`` wrote ``lane=r1``, ``False`` wrote
  ``lane=False``, and ``None`` was reported as "empty" — a value error for what is a type error.

Claims about the producers' PUBLIC contract — the byte shape of a clean call, the round trip, the
tokens that must KEEP rendering — are the other kind of claim and live in
``tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_dispatch_marker_producer_contract.py``
(``tests-placement-discovery-policy.md`` ``### regressions`` R3-b).
"""

from __future__ import annotations

import argparse
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow_dispatch_ir as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_dispatch_writer import (  # noqa: E501
    DISPATCH_INPUT_INVALID,
    DispatchRoute,
    HandoffOutcome,
    build_live_vocabulary,
    dispatch_implementation_request,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    GATE_BEARING_KINDS,
    RedmineJournalEntry,
    dispatch_entry_journals,
    dispatch_generations,
    extract_markers_from_note,
    render_dispatch_marker,
    render_dispatch_note,
)
from tests.support.hibernate_evidence_producer_corpus import (
    INVALID_TOKENS,
    INVALID_TYPES,
    VALID_TOKENS,
)

LANE = "lane-abc"


class _Rendering:
    """An object that is not a string but renders as one — the coercion's whole hazard."""

    def __init__(self, text=LANE):
        self._text = text

    def __str__(self):
        return self._text

    def __repr__(self):
        return f"_Rendering({self._text!r})"


class _ForgedFormat(str):
    """A ``str`` subclass that picks its own rendered bytes at f-string time."""

    def __format__(self, spec):
        return f"{LANE}:lane_generation=99"


def non_ascii_digit_like():
    """Every code point Python calls a digit that is NOT an ASCII decimal digit (derived).

    Derived by sweeping the code space rather than listed, for the reason review j#94247 gave when
    it blocked the same family: a hand-written corpus goes stale with the Unicode version, and
    list-shaped reasoning is what produced the defect. ``int()`` happily converts many of these —
    ``int('٣')`` is ``3`` — which is exactly why "numeric to Python" is not "the round this record
    names".
    """
    found = [
        chr(cp)
        for cp in range(sys.maxunicode + 1)
        if (chr(cp).isdigit() or chr(cp).isdecimal()) and chr(cp) not in "0123456789"
    ]
    assert found, "the sweep found no non-ASCII digit-like code point; the oracle is broken"
    return tuple(found)


class DispatchLaneRawValidationTest(unittest.TestCase):
    """``lane`` is judged as the caller passed it."""

    def test_every_shared_invalid_token_is_refused_as_written(self):
        # The SHARED producer corpus, so the lane of a dispatch and the lane of an evidence
        # envelope cannot drift apart on which raw tokens they refuse.
        for token in INVALID_TOKENS:
            with self.subTest(lane=token):
                with self.assertRaises(ValueError):
                    render_dispatch_marker(token, 1)

    def test_every_non_string_type_is_refused_rather_than_rendered(self):
        for value in INVALID_TYPES:
            with self.subTest(lane=value):
                with self.assertRaises(ValueError):
                    render_dispatch_marker(value, 1)

    def test_an_object_that_renders_as_a_lane_is_not_a_lane(self):
        # ``str(value or "")`` wrote ``lane=lane-abc`` for this: the value the record carried was a
        # RENDERING of the caller's object, and the caller never passed that string.
        with self.assertRaises(ValueError):
            render_dispatch_marker(_Rendering(), 1)

    def test_a_str_subclass_cannot_choose_the_rendered_bytes(self):
        # The exact builtin requirement (#14694 review j#94038 blocker 2) reaching this producer.
        with self.assertRaises(ValueError):
            render_dispatch_marker(_ForgedFormat(LANE), 1)

    def test_a_separator_bearing_lane_cannot_inject_a_second_field(self):
        with self.assertRaises(ValueError):
            render_dispatch_marker(f"{LANE}:lane_generation=99", 1)


class DispatchGenerationRawValidationTest(unittest.TestCase):
    """``lane_generation`` is a canonical positive decimal, in either type."""

    def test_non_positive_and_non_integral_generations_are_refused(self):
        for value in (0, -1, -5, 1.5, True, False, None, "0", "-5", "1.5", "01", "", " 7 "):
            with self.subTest(generation=value):
                with self.assertRaises(ValueError):
                    render_dispatch_marker(LANE, value)

    def test_no_digit_like_non_ascii_generation_renders(self):
        # Derived, not listed. Each of these converts through ``int()`` to a number that names a
        # round no source system owns.
        for char in non_ascii_digit_like():
            with self.subTest(generation=char):
                with self.assertRaises(ValueError):
                    render_dispatch_marker(LANE, char)

    def test_a_generation_wider_than_the_protocol_bound_is_refused(self):
        # The bound is the protocol's, taken from the contract module rather than written here.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            MAX_CANONICAL_DECIMAL_VALUE,
        )

        for over in (MAX_CANONICAL_DECIMAL_VALUE + 1, str(MAX_CANONICAL_DECIMAL_VALUE + 1), 10**40):
            with self.subTest(generation=over):
                with self.assertRaises(ValueError):
                    render_dispatch_marker(LANE, over)

    def test_a_truncating_generation_cannot_rename_the_round(self):
        # ``1]junk`` closed the marker at the ``]`` and read back as a clean dispatch for
        # generation ``1``: a caller asking for a round it did not name, silently.
        with self.assertRaises(ValueError):
            render_dispatch_marker(LANE, "1]junk")


class DispatchNoteCannotForgeAGateTest(unittest.TestCase):
    """The producer that carries NO gate cannot be made to write one."""

    def test_a_newline_bearing_lane_cannot_forge_a_gate_bearing_marker(self):
        for gate in sorted(GATE_BEARING_KINDS):
            forged = f"{LANE}]\n[mozyo:workflow-event:gate={gate}"
            with self.subTest(gate=gate):
                with self.assertRaises(ValueError):
                    render_dispatch_note("## Implementation Request", lane=forged, lane_generation=1)
        # The control, inline rather than as a sibling method so this file stays homogeneous
        # (#14694 R3-b): the pin must not be satisfiable by a producer that refuses everything.
        # Whatever it CAN render still declares no gate.
        for token in VALID_TOKENS:
            body = render_dispatch_note("## Implementation Request", lane=token, lane_generation=1)
            self.assertEqual(extract_markers_from_note("14717", "99999", body), ())


class DispatchGenerationsConsumerTest(unittest.TestCase):
    """The consumer half of the same field reads the producer's rule, not ``int()``'s."""

    @staticmethod
    def _entry(generation_token, journal_id="79600"):
        # A marker body written LITERALLY: a non-canonical generation is by definition not
        # something the canonical producer can be asked to render (#14694 lesson).
        return RedmineJournalEntry(
            issue_id="14717",
            journal_id=journal_id,
            notes=(
                "[mozyo:workflow-event:kind=implementation_request:"
                f"lane={LANE}:lane_generation={generation_token}]"
            ),
        )

    #: The control every sweep below carries inline (#14694 R3-b keeps this file homogeneous):
    #: a consumer that reported NO round would satisfy each pin vacuously.
    _CANONICAL = (("1", "1"), ("42", "2"))

    def _assert_still_reads_canonical_rounds(self):
        entries = [self._entry(token, jid) for token, jid in self._CANONICAL]
        self.assertEqual(dispatch_generations(entries, lane=LANE), (1, 42))

    def test_a_non_ascii_digit_generation_names_no_round(self):
        for char in non_ascii_digit_like():
            with self.subTest(generation=char):
                self.assertEqual(dispatch_generations([self._entry(char)], lane=LANE), ())
        self._assert_still_reads_canonical_rounds()

    def test_leading_zero_and_non_positive_generations_name_no_round(self):
        for token in ("01", "0", "-5", "007", "+1"):
            with self.subTest(generation=token):
                self.assertEqual(dispatch_generations([self._entry(token)], lane=LANE), ())
        self._assert_still_reads_canonical_rounds()

    def test_a_generation_wider_than_the_protocol_bound_names_no_round(self):
        # Measured, not assumed: a 4400-digit token is refused on BOTH heads, because CPython's
        # int-from-str cap made ``int()`` raise and the ``except ValueError`` arm swallowed it —
        # so that width proves nothing about this fix. The width that separates the two heads is
        # one the protocol refuses and the interpreter happily converts.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            MAX_CANONICAL_DECIMAL_VALUE,
        )

        over = str(MAX_CANONICAL_DECIMAL_VALUE + 1)
        self.assertEqual(int(over), MAX_CANONICAL_DECIMAL_VALUE + 1)  # the interpreter accepts it
        for token in (over, "9" * 100, "9" * 4400):
            with self.subTest(generation=token):
                self.assertEqual(dispatch_generations([self._entry(token)], lane=LANE), ())
        self._assert_still_reads_canonical_rounds()


class SiblingRoundScopedRenderersTest(unittest.TestCase):
    """The same two lines, in the renderers whose own docstrings said they mirror the dispatch one.

    Derived, not recalled: an AST sweep for every function that builds a ``[mozyo:`` token in a
    value position (16 in ``src``) and still normalizes a value with ``str(...)``/``.strip()``
    before interpolating it found these two carrying ``lane`` / ``lane_generation`` — byte-identical
    to the pair this issue names. ``render_progress_marker``'s docstring already said it mirrors
    ``render_dispatch_marker`` and that its round scoping is load-bearing, so fixing one and not the
    other would have made this file's own claim false on the day it landed.
    """

    @staticmethod
    def _progress(**kw):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (  # noqa: E501
            render_progress_marker,
        )

        return render_progress_marker(kw.pop("kind", "progress_log"), **kw)

    @staticmethod
    def _sweep(**kw):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (  # noqa: E501
            render_sweep_record_marker,
        )

        base = dict(lane=LANE, lane_generation=1, dispatch_anchor="79600", outcome="stall_unprovable")
        base.update(kw)
        return render_sweep_record_marker(**base)

    def test_progress_marker_judges_lane_and_generation_raw(self):
        for lane in (" pad ", "a:b", "a]b", "a\nb", _Rendering(), None, 0, _ForgedFormat(LANE)):
            with self.subTest(lane=lane):
                with self.assertRaises(ValueError):
                    self._progress(lane=lane, lane_generation=1)
        for generation in (0, -1, 1.5, True, "01", "٣", " 1 ", "1]junk"):
            with self.subTest(generation=generation):
                with self.assertRaises(ValueError):
                    self._progress(lane=LANE, lane_generation=generation)
        # Inline control: a clean call still renders the round-scoped token.
        self.assertIn(f"lane={LANE}:lane_generation=4]", self._progress(lane=LANE, lane_generation=4))

    def test_progress_marker_kind_speaks_for_the_object_it_was_asked_about(self):
        # ``str(kind).strip()`` then membership let an object impersonate a progress kind.
        with self.assertRaises(ValueError):
            self._progress(kind=_Rendering("progress_log"), lane=LANE, lane_generation=1)
        with self.assertRaises(ValueError):
            self._progress(kind=_ForgedFormat("progress_log"), lane=LANE, lane_generation=1)
        self.assertIn("kind=progress_log:", self._progress(lane=LANE, lane_generation=1))

    def test_the_sweep_record_key_is_judged_raw_in_every_field(self):
        # This marker IS the key the sweep recognizes its own prior record by, so a normalized
        # field means "recognize rather than duplicate" was answered about a different key.
        for field, bad in (
            ("lane", " pad "), ("lane", None), ("lane_generation", 0), ("lane_generation", "٣"),
            ("dispatch_anchor", " 79600 "), ("dispatch_anchor", None), ("dispatch_anchor", 79600),
            ("outcome", " stall "), ("outcome", None), ("outcome", "a:b"),
        ):
            with self.subTest(field=field, value=bad):
                with self.assertRaises(ValueError):
                    self._sweep(**{field: bad})
        # Inline control: the clean key still renders, in field order.
        self.assertIn(
            f"lane={LANE}:lane_generation=1:anchor=79600:outcome=stall_unprovable]", self._sweep()
        )


class SharedValidatorRawTypeTest(unittest.TestCase):
    """``validate_marker_field_value`` judges the caller's value, not a string it invented."""

    @staticmethod
    def _validate(value):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            validate_marker_field_value,
        )

        return validate_marker_field_value("lane", value)

    def test_an_object_that_renders_as_a_value_is_refused(self):
        with self.assertRaises(ValueError):
            self._validate(_Rendering())

    def test_bool_is_not_an_int_for_this_contract(self):
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self._validate(value)

    def test_a_str_subclass_is_refused(self):
        with self.assertRaises(ValueError):
            self._validate(_ForgedFormat(LANE))

    def test_a_wrong_type_is_reported_as_a_wrong_type(self):
        # ``None`` used to be reported as "empty", which sends the caller looking at the VALUE it
        # passed when the defect is the TYPE. The refusal must name the type it got.
        for value in (None, 1.5, b"lane", ["lane"]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    self._validate(value)
                self.assertIn(type(value).__name__, str(raised.exception))
        # Inline control: #14667 review j#93162 measured that the recovery-admission producer
        # passes ``lane_generation=1`` as an ``int`` and that the conversion is load-bearing for
        # it. Narrowing the coercion must not take that caller with it.
        self.assertEqual(self._validate(1), "1")


class DispatchBoundariesStayTypedTest(unittest.TestCase):
    """A producer refusal reaches the caller as the boundary's own typed outcome, never a crash."""

    _ROUTE = DispatchRoute(
        to="claude", target="mzb1_ws1_claude_la", target_repo="/repos/mozyo", lane="lane-a",
        gateway_callback_target="mzb1_ws1_codex_la",
    )

    def test_reconcile_writer_refuses_before_any_read_or_write(self):
        posted, reads = [], []

        def post_note(issue, note):
            posted.append((issue, note))
            return ""

        def read_entries(issue):
            reads.append(issue)
            return []

        for lane, generation in ((LANE, "abc"), (LANE, "0"), (LANE, "1]junk"), (None, "1")):
            with self.subTest(lane=lane, generation=generation):
                result = dispatch_implementation_request(
                    issue="14717", lane=lane, lane_generation=generation, body="## IR",
                    route=self._ROUTE, vocab=build_live_vocabulary(),
                    post_note=post_note, read_entries=read_entries,
                    handoff_send=lambda anchor: HandoffOutcome(delivered=True),
                )
                self.assertEqual(result.status, DISPATCH_INPUT_INVALID)
                self.assertFalse(result.sendable)
                self.assertFalse(result.handoff_delivered)
        # No provider call of either kind was made for any of them.
        self.assertEqual((posted, reads), ([], []))

    def test_the_application_boundary_trims_deliberately_and_the_producer_refuses(self):
        """Where the normalization that REMAINS lives, pinned so it cannot move by accident.

        ``dispatch_implementation_request`` trims its ``lane`` / ``lane_generation`` arguments
        before it renders — it is the boundary that owns operator input, and the shared contract
        says exactly that: "Callers that genuinely hold untrimmed input must trim it themselves,
        deliberately, before they claim the value is what they mean". This lane did NOT extend the
        refusal to that boundary, so ``lane=' pad '`` still dispatches as ``pad``.

        That is a live seam, not a settled one: the trim is a second normalization below the argv
        boundary that already trims, so the value this function writes is still not the value its
        caller passed. It is pinned here rather than left implicit so the decision is visible, and
        it is named in the Review Request for a ruling rather than consumed silently.
        """
        posted = []

        def post_note(issue, note):
            posted.append((issue, note))
            return ""

        def read_entries(issue):
            return [
                RedmineJournalEntry(issue_id=issue, journal_id="79600", notes=posted[-1][1])
                for _ in (posted[-1:] or ())
            ]

        result = dispatch_implementation_request(
            issue="14717", lane=" pad ", lane_generation=" 1 ", body="## IR",
            route=self._ROUTE, vocab=build_live_vocabulary(),
            post_note=post_note, read_entries=read_entries,
            handoff_send=lambda anchor: HandoffOutcome(delivered=True),
        )
        self.assertEqual(result.lane, "pad")
        self.assertEqual(result.lane_generation, "1")
        self.assertIn(render_dispatch_marker("pad", "1"), posted[0][1])
        # ...and the DOMAIN producer, asked the same question, refuses both.
        with self.assertRaises(ValueError):
            render_dispatch_marker(" pad ", " 1 ")

    def test_cli_dry_run_fails_closed_instead_of_raising(self):
        base = dict(
            issue="14717", lane=LANE, body="## IR", body_file=None,
            target="mzb1_ws1_claude_la", target_repo="/repos/mozyo",
            gateway_callback_target="mzb1_ws1_codex_la", role_profile="implementation_worker",
            source="redmine", to="claude", execute=False,
        )
        for generation in ("abc", "0", "-1", "1]junk"):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(generation=generation):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.cmd_workflow_dispatch_ir(
                        argparse.Namespace(**dict(base, generation=generation))
                    )
                self.assertEqual(rc, 2)
                self.assertNotIn("[mozyo:", out.getvalue())
                self.assertIn("dispatch-ir", err.getvalue())


if __name__ == "__main__":
    unittest.main()
