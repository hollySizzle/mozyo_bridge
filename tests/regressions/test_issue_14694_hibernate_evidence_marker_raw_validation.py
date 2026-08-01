"""Redmine #14694: raw-invalid producer input must never become an authority marker.

The hibernate-evidence producers validated their inputs through ``str(value or "").strip()``
(introduced with the marker grammar in ``ab344781``, carried into ``_required`` by ``94f745b0``),
which normalized the caller's raw value BEFORE judging it. Independently reproduced in #14667
review j#93230: ``workflow=' check '`` / ``run=' run '`` were not refused — they were trimmed into
the clean canonical tokens ``check`` / ``run`` and became durable auto-hibernate authority. The
central `### Hibernate Evidence Marker Contract` requires the opposite: a producer does not
normalize raw input into a value the caller did not write, and "renderer は parser が拒否する
ものを書かない".

One symptom, measured on the producer surface rather than recalled from the report, in every shape
it took — including the three shapes the FIRST fix left open (review j#93646):

- trimming (``" check "`` → ``check``) — the reported symptom;
- an INCOMPLETE forbidden set: ``\\n`` / ``\\r`` / ``\\xa0`` were not "空白" to a tuple that
  enumerated space and tab, so they were rendered — and markers are scanned per line, so the
  record never closed on its line and read back as nothing at all;
- ``str()`` coercion of a non-string, and ``or ""`` reporting a wrong TYPE as a missing field;
- ``conclusion`` accepted and discarded, so ``conclusion="failure"`` rendered as
  ``conclusion=success``;
- a field the kind's marker cannot carry, accepted and dropped;
- **the CLI producer trimming the identities before validating them** (finding 1), so the one
  producer an operator actually calls kept converting ``--evidence-workspace " ws "`` into the
  canonical authority field ``ws``;
- **``int(lane_generation)`` in front of the renderer** (finding 2), which turned ``1.5`` into
  generation ``1`` — evidence bound to a generation nobody named;
- **an explicit empty value read as "nothing was supplied"** (finding 3), because the first fix
  spelled absence as ``""``.

Every test in this file detects the recurrence of that symptom. Claims about the producers' public
contract — the byte shape of a clean marker, the round trip, the unchanged parse side — are the
other kind of claim and live in
``tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_hibernate_evidence_producer_contract.py``
(``tests-placement-discovery-policy.md`` ``### regressions`` R3-b).
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.review_gate_marker_fields import (  # noqa: E501
    lane_envelope_marker_fields,
    review_gate_marker_fields,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_integration as ie,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_marker as ev,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    render_gate_note,
    render_workflow_event_marker,
    strict_marker_fields_in_note,
)
from tests.support.hibernate_evidence_producer_corpus import (
    CARRIED_BY_KIND,
    HEAD,
    INTEGRATION_HEAD,
    INVALID_TOKENS,
    INVALID_TYPES,
    LANE,
    PRODUCER_FIELDS,
    VALID_TOKENS,
    WS,
    assert_population_is_closed,
    clean_kwargs,
    envelope,
    envelope_for,
)


class ReportedSymptomTests(unittest.TestCase):
    """The exact reproduction from #14667 review j#93230."""

    def test_padded_workflow_and_run_are_refused_not_trimmed(self):
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=envelope(), workflow=" check ", run="run"
            )
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=envelope(), workflow="check", run=" run "
            )

    def test_the_trimmed_authority_marker_is_never_produced(self):
        """The failure mode is not "an error was raised" — it is WHICH marker existed.

        The old producer emitted the byte-identical marker a clean caller would have produced, so
        the durable record could not tell the two apart. Nothing may render that marker from the
        padded input.
        """
        clean = ev.render_hibernate_evidence(
            ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=envelope(), workflow="check", run="run"
        )
        self.assertIn("workflow=check:run=run", clean)
        for workflow, run in ((" check ", " run "), ("check ", "run"), ("check", " run")):
            with self.subTest(workflow=workflow, run=run):
                try:
                    padded = ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(),
                        workflow=workflow,
                        run=run,
                    )
                except ValueError:
                    continue
                self.fail(f"raw-invalid input rendered the authority marker {padded!r}")


class EveryProducerFieldTests(unittest.TestCase):
    """The sweep over the derived population, not over the two fields the report named."""

    def test_every_carried_field_refuses_a_raw_invalid_token(self):
        assert_population_is_closed(self)
        for kind, carried in sorted(CARRIED_BY_KIND.items()):
            for field in carried:
                for bad in INVALID_TOKENS:
                    with self.subTest(kind=kind, field=field, value=bad):
                        kwargs = clean_kwargs(kind)
                        kwargs[field] = bad
                        with self.assertRaises(ValueError):
                            ev.render_hibernate_evidence(
                                kind, envelope=envelope_for(kind), **kwargs
                            )

    def test_every_carried_field_refuses_a_non_string(self):
        assert_population_is_closed(self)
        for kind, carried in sorted(CARRIED_BY_KIND.items()):
            for field in carried:
                for bad in INVALID_TYPES:
                    with self.subTest(kind=kind, field=field, value=bad):
                        kwargs = clean_kwargs(kind)
                        kwargs[field] = bad
                        with self.assertRaises(ValueError):
                            ev.render_hibernate_evidence(
                                kind, envelope=envelope_for(kind), **kwargs
                            )

    def test_a_field_this_kind_cannot_carry_is_refused_even_when_valid(self):
        """The second shape of the same defect: a supplied value that is silently DROPPED.

        ``park_declared`` carries neither ``run`` nor ``workflow``, so a caller supplying one was
        asserting something the durable record would not contain.
        """
        assert_population_is_closed(self)
        for kind, carried in sorted(CARRIED_BY_KIND.items()):
            for field in PRODUCER_FIELDS:
                if field in carried:
                    continue
                with self.subTest(kind=kind, field=field):
                    kwargs = clean_kwargs(kind)
                    # A perfectly VALID value: the refusal is about the field, not its shape.
                    kwargs[field] = "success" if field == ev.FIELD_CONCLUSION else "clean"
                    with self.assertRaises(ValueError):
                        ev.render_hibernate_evidence(kind, envelope=envelope_for(kind), **kwargs)

    def test_an_explicitly_empty_value_is_refused_not_read_as_absent(self):
        """Review j#93646 finding 3: the first fix spelled "unsupplied" as ``""``.

        That made "the caller passed nothing" and "the caller passed an empty value" the same
        input, so ``park(run="")`` / ``dogfood(workflow="")`` were dropped instead of refused —
        the silent drop this producer's own rule forbids, reintroduced by the rule's
        implementation. Only ``CI(run="")`` was refused, and only because CI requires a run.
        """
        assert_population_is_closed(self)
        for kind in sorted(CARRIED_BY_KIND):
            for field in PRODUCER_FIELDS:
                with self.subTest(kind=kind, field=field):
                    kwargs = clean_kwargs(kind)
                    kwargs[field] = ""
                    with self.assertRaises(ValueError):
                        ev.render_hibernate_evidence(kind, envelope=envelope_for(kind), **kwargs)

    def test_conclusion_is_refused_rather_than_coerced_to_success(self):
        """``conclusion="failure"`` used to be rendered as ``conclusion=success``."""
        for bad in ("failure", "cancelled", "approved", "Success", "SUCCESS"):
            with self.subTest(conclusion=bad):
                with self.assertRaises(ValueError):
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(),
                        workflow="check",
                        run="run",
                        conclusion=bad,
                    )


class EnvelopeIdentityTests(unittest.TestCase):
    """The envelope reaches the marker through the same producer call and had the same defect."""

    def test_string_identities_are_refused_raw(self):
        for field in ("workspace", "lane"):
            for bad in INVALID_TOKENS + INVALID_TYPES:
                with self.subTest(field=field, value=bad):
                    with self.assertRaises(ValueError):
                        ev.render_hibernate_evidence(
                            ev.EVIDENCE_REQUIRED_CI_GREEN,
                            envelope=envelope(**{field: bad}),
                            workflow="check",
                            run="run",
                        )

    def test_a_padded_head_is_refused_rather_than_trimmed_into_a_full_sha(self):
        for bad in (f" {HEAD}", f"{HEAD} ", f"\n{HEAD}", f"{HEAD}\t"):
            with self.subTest(head=bad):
                with self.assertRaises(ValueError):
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(head=bad),
                        workflow="check",
                        run="run",
                    )

    def test_absent_head_is_the_empty_string_and_nothing_else(self):
        # ``park_declared`` is lane-anchored, so ``head=""`` renders. ``None`` is a producer error,
        # not a second spelling of absent: ``str(None or "")`` used to make them the same value.
        rendered = ev.render_hibernate_evidence(ev.EVIDENCE_PARK_DECLARED, envelope=envelope(head=""))
        self.assertNotIn("head=", rendered)
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(ev.EVIDENCE_PARK_DECLARED, envelope=envelope(head=None))


class IntegrationEvidenceProducerTests(unittest.TestCase):
    """The ``integration_disposition`` marker is the same surface with the same one-liner."""

    def _render(self, **over):
        kwargs = dict(
            envelope=envelope(),
            integration_head=INTEGRATION_HEAD,
            integration_branch="main-next",
            disposition="merge",
        )
        kwargs.update(over)
        return ie.render_integration_evidence(**kwargs)

    def test_each_field_is_refused_raw(self):
        for field in ("integration_head", "integration_branch", "disposition"):
            for bad in INVALID_TOKENS + INVALID_TYPES:
                with self.subTest(field=field, value=bad):
                    with self.assertRaises(ValueError):
                        self._render(**{field: bad})

    def test_a_padded_branch_is_not_trimmed_into_the_canonical_ref(self):
        with self.assertRaises(ValueError):
            self._render(integration_branch=" main-next ")


class CallSiteTests(unittest.TestCase):
    """A guard the CALLER pre-empts is a guard that never runs."""

    def test_the_gate_note_producer_hands_the_envelope_over_raw(self):
        for bad in (" ws-1", "ws-1 ", "ws\n1"):
            with self.subTest(workspace=bad):
                with self.assertRaises(ValueError):
                    render_gate_note(
                        "review_result",
                        evidence_workspace=bad,
                        evidence_lane=LANE,
                        evidence_lane_generation=3,
                    )

    def test_the_gate_note_producer_binds_only_the_generation_it_was_given(self):
        """Review j#93646 finding 2: ``int()`` in front of the renderer.

        ``1.5`` became ``lane_generation=1`` and ``2.9`` became ``2`` — an evidence marker bound to
        a generation the caller never named, which is exactly the cross-generation promotion the
        envelope exists to prevent. A padded / stringy generation was converted the same way.
        """
        for bad in (1.5, 2.9, 0.5, " 3 ", "3", True, "three", None, 0, -1):
            with self.subTest(lane_generation=bad):
                with self.assertRaises(ValueError):
                    render_gate_note(
                        "review_result",
                        evidence_workspace=WS,
                        evidence_lane=LANE,
                        evidence_lane_generation=bad,
                    )
        # Negative control: the value it WAS given still renders, unchanged.
        self.assertIn(
            "lane_generation=3",
            render_gate_note(
                "review_result",
                evidence_workspace=WS,
                evidence_lane=LANE,
                evidence_lane_generation=3,
            ),
        )

    def test_the_cli_producer_refuses_padding_instead_of_canonicalizing_it(self):
        """Review j#93646 finding 1: the CLI stripped the identities before validating them.

        This is the producer an operator actually calls, so #14694 survived there in full: a
        padded ``--evidence-workspace`` became the canonical authority field. Being a CLI licenses
        a typed refusal, not a rewrite — so each case must come back with a REFUSAL TOKEN and no
        fields, never with normalized fields and never as a traceback.
        """
        cases = (
            ({"evidence_workspace": " ws "}, "evidence_envelope_malformed_identity"),
            ({"evidence_workspace": "ws "}, "evidence_envelope_malformed_identity"),
            ({"evidence_lane": " lane "}, "evidence_envelope_malformed_identity"),
            ({"evidence_lane": "lane\n"}, "evidence_envelope_malformed_identity"),
            ({"evidence_lane": "lane\xa0b"}, "evidence_envelope_malformed_identity"),
            ({"evidence_lane": "lane:b"}, "evidence_envelope_malformed_identity"),
            ({"evidence_lane_generation": " 3 "}, "evidence_envelope_malformed_generation"),
            ({"evidence_lane_generation": "3 "}, "evidence_envelope_malformed_generation"),
        )
        for over, expected in cases:
            with self.subTest(**over):
                values = {
                    "evidence_workspace": WS,
                    "evidence_lane": LANE,
                    "evidence_lane_generation": "3",
                }
                values.update(over)
                fields, refusal = lane_envelope_marker_fields(argparse.Namespace(**values))
                self.assertEqual(fields, {})
                self.assertEqual(refusal, expected)
        # Inline control, not a test of its own: it exists to stop the refusals above from being
        # satisfied by "refuse everything", so it guards this detector rather than stating the
        # CLI's contract (which is asserted in the producer-contract unit tests).
        fields, refusal = lane_envelope_marker_fields(
            argparse.Namespace(
                evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation="3"
            )
        )
        self.assertIsNone(refusal)
        self.assertEqual(
            fields,
            {"evidence_workspace": WS, "evidence_lane": LANE, "evidence_lane_generation": 3},
        )


    def test_the_review_gate_producer_refuses_padding_on_its_own_fields_too(self):
        """The same CLI producer's OTHER values, found while closing finding 1.

        ``review_gate_marker_fields`` normalized ``--target-head`` and
        ``--review-request-journal`` exactly as it normalized the envelope identities, so a padded
        head became the canonical authority field of a ``review_result`` marker — which the
        central contract also reads as hibernate evidence. Fixing three of this function's five
        values would have left the finding half-closed inside the function it names.
        """
        sha = "c" * 40

        def args(**over):
            values = {
                "target_head": sha,
                "review_request_journal": "93610",
                "review_decision": "approval",
                "evidence_workspace": None,
                "evidence_lane": None,
                "evidence_lane_generation": None,
            }
            values.update(over)
            return argparse.Namespace(**values)

        cases = (
            ({"target_head": f" {sha} "}, "review_marker_malformed_target_head"),
            ({"target_head": f"{sha}\n"}, "review_marker_malformed_target_head"),
            ({"target_head": f"\t{sha}"}, "review_marker_malformed_target_head"),
            (
                {"review_request_journal": " 93610 "},
                "review_marker_malformed_review_request_journal",
            ),
            (
                {"review_request_journal": "93610\n"},
                "review_marker_malformed_review_request_journal",
            ),
            (
                {"review_request_journal": "9361 0"},
                "review_marker_malformed_review_request_journal",
            ),
        )
        for over, expected in cases:
            with self.subTest(**over):
                fields, refusal = review_gate_marker_fields(args(**over), "review_result")
                self.assertEqual(fields, {})
                self.assertEqual(refusal, expected)
        # Inline controls: the clean call is untouched, and the pre-existing refusals still say
        # what they said (a padded value must not be reported as a MISSING one).
        fields, refusal = review_gate_marker_fields(args(), "review_result")
        self.assertIsNone(refusal)
        self.assertEqual(fields["target_head"], sha)
        self.assertEqual(fields["review_request_journal"], "93610")
        self.assertEqual(
            review_gate_marker_fields(args(target_head=""), "review_result")[1],
            "review_marker_missing_target_head",
        )
        self.assertEqual(
            review_gate_marker_fields(args(target_head="deadbeef"), "review_result")[1],
            "review_marker_malformed_target_head",
        )
        self.assertEqual(
            review_gate_marker_fields(args(review_request_journal=""), "review_result")[1],
            "review_marker_missing_review_request_journal",
        )


class ReviewResultProducerTests(unittest.TestCase):
    """``review_result`` IS one of the evidence kinds, so its own fields are not exempt.

    Review j#93818 finding 1 overruled the R3 disposition that routed this away as "a different
    contract". The central `### Hibernate Evidence Marker Contract` lists ``review_result`` first
    among the evidence kinds, and this renderer's own envelope comment already said the rule —
    "review_result is one of the evidence kinds, so it is not exempt from 'a renderer must refuse
    what its parser refuses'". The envelope obeyed it; the fields of the SAME marker did not.
    """

    def test_every_review_evidence_field_is_judged_raw(self):
        sha = "c" * 40
        base = dict(conclusion="approved", target_head=sha, review_request_journal="93802")

        def render(**over):
            kwargs = dict(base)
            gate = over.pop("gate", "review_result")
            kwargs.update(over)
            return render_workflow_event_marker(gate, **kwargs)

        for label, over in (
            ("gate padded", {"gate": " review_result "}),
            ("gate non-str", {"gate": 12345}),
            ("conclusion padded", {"conclusion": " approved "}),
            ("conclusion outside vocabulary", {"conclusion": "approve"}),
            ("head padded", {"target_head": f" {sha} "}),
            ("head newline", {"target_head": f"{sha}\n"}),
            ("head not full", {"target_head": "deadbeef"}),
            ("head non-str", {"target_head": 12345}),
            ("req padded", {"review_request_journal": " 93802 "}),
            ("req inner space", {"review_request_journal": "938 02"}),
            ("req non-numeric", {"review_request_journal": "abc"}),
            ("req shadow value", {"review_request_journal": "93802=shadow"}),
            ("req non-ascii digit", {"review_request_journal": "٣"}),
            ("req zero", {"review_request_journal": "0"}),
            ("req negative", {"review_request_journal": "-5"}),
        ):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    render(**over)
        with self.subTest("callback outside vocabulary"):
            with self.assertRaises(ValueError):
                render_workflow_event_marker("blocked", callback="maybe")
        # Inline controls: the clean markers this producer actually writes are unchanged, byte for
        # byte, including the bare and head-less legacy forms.
        self.assertEqual(
            render(),
            f"[mozyo:workflow-event:gate=review_result:conclusion=approved:head={sha}:req=93802]",
        )
        self.assertEqual(
            render_workflow_event_marker("review_request", target_head=sha),
            f"[mozyo:workflow-event:gate=review_request:head={sha}]",
        )
        self.assertEqual(
            render_workflow_event_marker("implementation_done", commit_bearing=True, issue_open=False),
            "[mozyo:workflow-event:gate=implementation_done:commit=1:open=0]",
        )
        self.assertEqual(
            render_workflow_event_marker("blocked", callback="due"),
            "[mozyo:workflow-event:gate=blocked:callback=due]",
        )

    def test_an_unreadable_or_shadowed_req_is_never_written(self):
        """A rendered ``req`` must read back as the journal it names, or not exist.

        ``req='938 02'`` and ``req='93802=shadow'`` were not merely unreadable — the strict reader
        extracted them as well-formed fields with a DIFFERENT value than any journal id, so the
        generation fence would have compared against something nobody wrote.
        """
        sha = "c" * 40
        for bad in ("938 02", "93802=shadow", " 93802 ", "abc"):
            with self.subTest(req=bad):
                try:
                    marker = render_workflow_event_marker(
                        "review_result", conclusion="approved", target_head=sha,
                        review_request_journal=bad,
                    )
                except ValueError:
                    continue
                self.fail(f"raw-invalid req rendered the authority marker {marker!r}")

    def test_membership_cannot_be_impersonated_into_a_different_written_value(self):
        """Review j#93882 finding 1: the vocabulary check and the written value were two values.

        ``require_vocabulary`` tested membership and then rendered ``str(value)``, so an object
        whose ``__hash__`` / ``__eq__`` impersonate ``"approved"`` while ``__str__`` says
        ``"bogus"`` passed the closed vocabulary and wrote ``conclusion=bogus`` — which the strict
        reader extracted as ``bogus`` and the consumer downgrades to ``pending``. Refusing a plain
        ``int`` was never a type contract; it was membership happening to fail.
        """

        class _Spoof:
            """Hashes and compares equal to ``real`` while stringifying to ``written``."""

            def __init__(self, real, written):
                self.real, self.written = real, written

            def __hash__(self):
                return hash(self.real)

            def __eq__(self, other):
                return other == self.real

            def __str__(self):
                return self.written

        sha = "c" * 40
        for label, gate, kwargs in (
            (
                "conclusion",
                "review_result",
                dict(
                    conclusion=_Spoof("approved", "bogus"),
                    target_head=sha,
                    review_request_journal="93802",
                ),
            ),
            ("callback", "blocked", dict(callback=_Spoof("due", "maybe"))),
            (
                "head",
                "review_result",
                dict(
                    conclusion="approved",
                    target_head=_Spoof(sha, "d" * 40),
                    review_request_journal="93802",
                ),
            ),
            (
                "req",
                "review_result",
                dict(
                    conclusion="approved",
                    target_head=sha,
                    review_request_journal=_Spoof("93802", "1"),
                ),
            ),
        ):
            with self.subTest(field=label):
                try:
                    marker = render_workflow_event_marker(gate, **kwargs)
                except ValueError:
                    continue
                self.fail(f"an impersonated {label} rendered the authority marker {marker!r}")

    def test_the_journal_id_predicate_is_lexical_and_never_raises(self):
        """Review j#93882 finding 2: `int()` decided positivity, so it decided too much.

        It accepted ``"01"`` — not the string Redmine owns, so ``req`` cannot exact-match the
        request it claims to answer — and it RAISED on a 4301-digit input, because CPython caps
        int-from-str conversion at 4300 digits. A predicate that raises is not a predicate: the CLI
        boundary that owes a typed refusal got an exception.
        """
        # Imported HERE, not at module scope: this predicate does not exist on the base revision,
        # and a module-level import would turn every method in this file into one ImportError —
        # a "they all fail on base" that measures nothing.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            is_journal_id,
        )

        for bad in ("01", "007", "0", "", " 1", "1 ", "1.0", "-1", "٣", None, 1, True):
            with self.subTest(value=bad):
                self.assertFalse(is_journal_id(bad))
        for good in ("1", "9", "10", "93802", "9" * 4300):
            with self.subTest(value=f"{len(good)} digits"):
                self.assertTrue(is_journal_id(good))
        # Longer than any consumer could convert to an integer: refused as a False, never raised.
        for oversized in ("9" * 4301, "1" * 10000):
            with self.subTest(value=f"{len(oversized)} digits"):
                self.assertFalse(is_journal_id(oversized))

    def test_a_noncanonical_or_oversized_req_is_a_typed_refusal_not_an_exception(self):
        sha = "c" * 40

        def args(req):
            return argparse.Namespace(
                target_head=sha,
                review_request_journal=req,
                review_decision="approval",
                evidence_workspace=None,
                evidence_lane=None,
                evidence_lane_generation=None,
            )

        for bad in ("01", "007", "9" * 4301, "9" * 10000):
            with self.subTest(value=f"{bad[:6]}… ({len(bad)} chars)"):
                fields, refusal = review_gate_marker_fields(args(bad), "review_result")
                self.assertEqual(fields, {})
                self.assertEqual(refusal, "review_marker_malformed_review_request_journal")

    def test_the_cli_producer_answers_a_non_string_with_a_token_not_a_traceback(self):
        """Self-detected while sweeping finding 1's family across the boundaries.

        `contains_marker_separator` and `is_full_commit_head` both judge ``str(value)``, so a
        non-``str`` whose ``__str__`` looks like a clean head or identity passed this boundary and
        was handed on AS THE OBJECT. The domain producer then raised — turning the typed refusal a
        CLI owes its caller into a traceback, which is the shape review j#93882 finding 2 named for
        the oversized ``req``. The ``isinstance`` test now comes before the character tests.
        """

        class _Stringy:
            def __init__(self, text):
                self.text = text

            def __str__(self):
                return self.text

        sha = "c" * 40
        base = {
            "target_head": sha,
            "review_request_journal": "93802",
            "review_decision": "approval",
            "evidence_workspace": None,
            "evidence_lane": None,
            "evidence_lane_generation": None,
        }
        envelope_ok = {
            "evidence_workspace": "ws",
            "evidence_lane": "lane",
            "evidence_lane_generation": "3",
        }
        for label, over, expected in (
            ("head", {"target_head": _Stringy(sha)}, "review_marker_malformed_target_head"),
            (
                "workspace",
                {**envelope_ok, "evidence_workspace": _Stringy("ws")},
                "evidence_envelope_malformed_identity",
            ),
            (
                "lane",
                {**envelope_ok, "evidence_lane": _Stringy("lane")},
                "evidence_envelope_malformed_identity",
            ),
            (
                "lane_generation",
                {**envelope_ok, "evidence_lane_generation": _Stringy("3")},
                "evidence_envelope_malformed_generation",
            ),
        ):
            with self.subTest(field=label):
                values = {**base, **over}
                fields, refusal = review_gate_marker_fields(
                    argparse.Namespace(**values), "review_result"
                )
                self.assertEqual(fields, {})
                self.assertEqual(refusal, expected)
        # Inline control: the clean call still builds fields the renderer accepts, end to end.
        fields, refusal = review_gate_marker_fields(
            argparse.Namespace(**{**base, **envelope_ok}), "review_result"
        )
        self.assertIsNone(refusal)
        self.assertIn("head=" + sha, render_gate_note("review_result", **fields))

    def test_the_cli_producer_refuses_a_req_that_is_not_a_journal_id(self):
        """Review j#93818 finding 2: only whitespace was refused, never the SHAPE."""
        sha = "c" * 40

        def args(req):
            return argparse.Namespace(
                target_head=sha,
                review_request_journal=req,
                review_decision="approval",
                evidence_workspace=None,
                evidence_lane=None,
                evidence_lane_generation=None,
            )

        for bad in ("abc", "93802=shadow", "-5", "0", "1.5", "٣", " 93802 ", "93 802"):
            with self.subTest(req=bad):
                fields, refusal = review_gate_marker_fields(args(bad), "review_result")
                self.assertEqual(fields, {})
                self.assertEqual(refusal, "review_marker_malformed_review_request_journal")
        # Inline control: a real journal id still passes, and an absent one still says MISSING.
        fields, refusal = review_gate_marker_fields(args("93802"), "review_result")
        self.assertIsNone(refusal)
        self.assertEqual(fields["review_request_journal"], "93802")
        self.assertEqual(
            review_gate_marker_fields(args(""), "review_result")[1],
            "review_marker_missing_review_request_journal",
        )


class SubclassCannotRewriteWhatWasValidatedTests(unittest.TestCase):
    """Review j#94038 blocker 2: `isinstance` lets the value choose its own rendering.

    Every marker field is written with an f-string, so ``type(value).__format__`` decides the bytes
    that land in the durable record. A ``str`` subclass may override it, so "return the validated
    object itself" — the R5 fix — still allowed the checked value and the written value to differ.
    Measured on the previous head: a subclass validated as the head ``a*40`` rendered ``head=b*40``,
    and one validated as the workspace ``ws`` rendered ``workspace=evil:lane=forged``, injecting a
    second ``lane`` field AHEAD of the real one. The ``int`` sibling was found by sweeping the
    family rather than from the report: a generation subclass rendered
    ``lane_generation=9:head=forged``.
    """

    class _Rewriting(str):
        """Validates as ``real`` and renders as ``written``."""

        def __new__(cls, real, written):
            token = super().__new__(cls, real)
            token.written = written
            return token

        def __format__(self, spec):
            return self.written

    class _RewritingInt(int):
        def __format__(self, spec):
            return "9:head=forged"

    def test_no_marker_field_can_be_rewritten_at_render_time(self):
        sha = "a" * 40
        rewritten = self._Rewriting
        cases = (
            (
                "head",
                lambda: render_workflow_event_marker(
                    "review_request", target_head=rewritten(sha, "b" * 40)
                ),
            ),
            (
                "req",
                lambda: render_workflow_event_marker(
                    "review_result",
                    conclusion="approved",
                    target_head=sha,
                    review_request_journal=rewritten("93802", "1"),
                ),
            ),
            (
                "conclusion",
                lambda: render_workflow_event_marker(
                    "review_result",
                    conclusion=rewritten("approved", "bogus"),
                    target_head=sha,
                    review_request_journal="93802",
                ),
            ),
            (
                "gate",
                lambda: render_workflow_event_marker(rewritten("review_request", "bogus")),
            ),
            (
                # Self-detected while sweeping: `kind` was membership-checked and then f-string'd
                # into `gate=`, so a subclass equal to a real kind injected into the gate field.
                "evidence kind",
                lambda: ev.render_hibernate_evidence(
                    rewritten(ev.EVIDENCE_PARK_DECLARED, "evil:head=forged"),
                    envelope=envelope(head=""),
                ),
            ),
            (
                "integration disposition",
                lambda: ie.render_integration_evidence(
                    envelope=envelope(),
                    integration_head=INTEGRATION_HEAD,
                    integration_branch="main-next",
                    disposition=rewritten("merge", "evil:x=1"),
                ),
            ),
            (
                "envelope workspace",
                lambda: ev.render_hibernate_evidence(
                    ev.EVIDENCE_PARK_DECLARED,
                    envelope=envelope(head="", workspace=rewritten("ws", "evil:lane=forged")),
                ),
            ),
            (
                "envelope lane",
                lambda: ev.render_hibernate_evidence(
                    ev.EVIDENCE_PARK_DECLARED,
                    envelope=envelope(head="", lane=rewritten("lane", "evil:head=forged")),
                ),
            ),
            (
                "envelope head",
                lambda: ev.render_hibernate_evidence(
                    ev.EVIDENCE_REQUIRED_CI_GREEN,
                    envelope=envelope(head=rewritten(sha, "b" * 40)),
                    workflow="check",
                    run="run",
                ),
            ),
            (
                "envelope lane_generation",
                lambda: ev.render_hibernate_evidence(
                    ev.EVIDENCE_PARK_DECLARED,
                    envelope=envelope(head="", lane_generation=self._RewritingInt(3)),
                ),
            ),
            (
                "kind-specific workflow",
                lambda: ev.render_hibernate_evidence(
                    ev.EVIDENCE_REQUIRED_CI_GREEN,
                    envelope=envelope(),
                    workflow=rewritten("check", "evil:run=forged"),
                    run="run",
                ),
            ),
            (
                "integration branch",
                lambda: ie.render_integration_evidence(
                    envelope=envelope(),
                    integration_head=INTEGRATION_HEAD,
                    integration_branch=rewritten("main-next", "evil:disposition=merge"),
                    disposition="merge",
                ),
            ),
        )
        for label, render in cases:
            with self.subTest(field=label):
                try:
                    marker = render()
                except ValueError:
                    continue
                self.fail(f"a rewriting {label} rendered the authority marker {marker!r}")

    def test_the_cli_producer_refuses_a_rewriting_value_too(self):
        sha = "a" * 40
        base = {
            "target_head": sha,
            "review_request_journal": "93802",
            "review_decision": "approval",
            "evidence_workspace": "ws",
            "evidence_lane": "lane",
            "evidence_lane_generation": "3",
        }
        for label, over, expected in (
            (
                "head",
                {"target_head": self._Rewriting(sha, "b" * 40)},
                "review_marker_malformed_target_head",
            ),
            (
                "workspace",
                {"evidence_workspace": self._Rewriting("ws", "evil:lane=forged")},
                "evidence_envelope_malformed_identity",
            ),
            (
                "lane_generation",
                {"evidence_lane_generation": self._Rewriting("3", "9")},
                "evidence_envelope_malformed_generation",
            ),
        ):
            with self.subTest(field=label):
                fields, refusal = review_gate_marker_fields(
                    argparse.Namespace(**{**base, **over}), "review_result"
                )
                self.assertEqual(fields, {})
                self.assertEqual(refusal, expected)


class CanonicalDecimalFieldsTests(unittest.TestCase):
    """Review j#94038 blocker 1: `req` was fixed in R5 and `lane_generation` was not.

    Both are the contract's canonical positive decimals, and both had the same defect — `isdigit()`
    plus `int()` accepted ``"01"`` and ``"٣"`` and rewrote them into a value nobody named, and
    RAISED on a 4301-digit input through the very function whose docstring promised that nothing
    there becomes a traceback. Fixing one field and not the other is fixing a field rather than a
    defect, so they now ask one predicate.
    """

    def _envelope_args(self, generation):
        return argparse.Namespace(
            evidence_workspace="ws", evidence_lane="lane", evidence_lane_generation=generation
        )

    def test_the_generation_is_canonical_or_a_typed_refusal(self):
        for bad in ("01", "007", "٣", "0", "-1", "1.0", " 3", "3 ", "9" * 4301, "9" * 10000):
            with self.subTest(value=f"{bad[:6]}… ({len(bad)} chars)"):
                fields, refusal = lane_envelope_marker_fields(self._envelope_args(bad))
                self.assertEqual(fields, {})
                self.assertEqual(refusal, "evidence_envelope_malformed_generation")
        # Inline control: the canonical generation still resolves to the int the renderer wants.
        fields, refusal = lane_envelope_marker_fields(self._envelope_args("3"))
        self.assertIsNone(refusal)
        self.assertEqual(fields["evidence_lane_generation"], 3)

    def test_the_predicate_never_raises_on_either_supported_interpreter(self):
        """Review j#94093: the guard added to stop this predicate raising was itself raising.

        ``sys.get_int_max_str_digits`` is new in Python 3.10.7 and this package supports ``>=3.10``
        (``pyproject.toml``), so calling it unconditionally made a CLEAN ``req`` and a clean CLI
        ``lane_generation`` die with ``AttributeError`` on 3.10.0-3.10.6. Exercised against BOTH
        interpreter shapes by removing the attribute, because "a predicate that raises is not a
        predicate" is the rule this whole line of fixes rests on and one runtime cannot show it.

        Where the API is absent so is the conversion limit it reports, so an over-long token is
        accepted there — the same rule ("refuse what no consumer could convert"), not a relaxation.
        """
        import sys as _sys

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            is_canonical_positive_decimal,
            is_journal_id,
        )

        oversized = "9" * 4301
        always_true = ("1", "3", "93802", "9" * 4300)
        always_false = ("01", "007", "0", "", " 1", "٣", None, 3, True)

        def assert_invariants(shape):
            for value in always_true:
                with self.subTest(shape=shape, value=value[:8]):
                    self.assertTrue(is_journal_id(value))
                    self.assertTrue(is_canonical_positive_decimal(value))
            for value in always_false:
                with self.subTest(shape=shape, value=repr(value)[:12]):
                    self.assertFalse(is_journal_id(value))
                    self.assertFalse(is_canonical_positive_decimal(value))

        sentinel = object()
        saved = getattr(_sys, "get_int_max_str_digits", sentinel)
        assert_invariants("api present")
        if saved is not sentinel:
            self.assertFalse(is_journal_id(oversized), "the reported limit must be honoured")
            del _sys.get_int_max_str_digits
            try:
                assert_invariants("api absent")
                # No limit to report, so nothing to refuse for length — and, crucially, no raise.
                self.assertTrue(is_journal_id(oversized))
            finally:
                _sys.get_int_max_str_digits = saved
        self.assertTrue(hasattr(_sys, "get_int_max_str_digits") or saved is sentinel)

    def test_the_cli_generation_survives_an_interpreter_without_the_limit_api(self):
        import sys as _sys

        sentinel = object()
        saved = getattr(_sys, "get_int_max_str_digits", sentinel)
        if saved is sentinel:
            self.skipTest("interpreter already lacks the API; the present-shape case covers it")
        del _sys.get_int_max_str_digits
        try:
            fields, refusal = lane_envelope_marker_fields(self._envelope_args("3"))
            self.assertIsNone(refusal)
            self.assertEqual(fields["evidence_lane_generation"], 3)
            self.assertEqual(
                lane_envelope_marker_fields(self._envelope_args("01"))[1],
                "evidence_envelope_malformed_generation",
            )
        finally:
            _sys.get_int_max_str_digits = saved

    def test_both_decimal_fields_ask_the_same_predicate(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (  # noqa: E501
            is_canonical_positive_decimal,
            is_journal_id,
        )

        for value in ("1", "3", "93802", "01", "0", "٣", "", "9" * 4301, None, 3):
            with self.subTest(value=repr(value)[:24]):
                self.assertEqual(is_journal_id(value), is_canonical_positive_decimal(value))


class RendererNeverWritesWhatItWouldNotMeanTests(unittest.TestCase):
    """The symptom stated as an invariant over BOTH corpora.

    The defect is "the producer wrote a value the caller did not pass". So for every input, exactly
    one of two things must hold: the producer refuses, or the marker reads back — through the
    STRICT reader every authority consumer shares — as exactly what was passed. Running this over
    the invalid corpus is what makes it a recurrence detector rather than a clean-path round trip:
    on the broken producer, ``" check "`` rendered and read back as ``check``, satisfying neither
    branch.
    """

    def test_every_token_is_either_refused_or_survives_unchanged(self):
        for token in INVALID_TOKENS + VALID_TOKENS:
            with self.subTest(token=token):
                try:
                    marker = ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(),
                        workflow=token,
                        run=token,
                    )
                except ValueError:
                    continue
                read = strict_marker_fields_in_note(marker)
                # Asserted rather than unpacked: on the broken producer a whitespace-bearing value
                # rendered a marker that never closed on its line, so the reader saw NO marker at
                # all — that must read as a failed invariant, not as an unpacking error.
                self.assertTrue(read, f"rendered a marker no strict reader can see: {marker!r}")
                self.assertEqual(len(read), 1, f"rendered {len(read)} markers: {marker!r}")
                (_, fields), = read
                parsed = ev.parse_hibernate_evidence(
                    fields, kind=ev.EVIDENCE_REQUIRED_CI_GREEN
                )
                self.assertIsInstance(parsed, ev.HibernateEvidence, f"unreadable {marker!r}")
                self.assertEqual(parsed.extra[ev.FIELD_WORKFLOW], token)
                self.assertEqual(parsed.extra[ev.FIELD_RUN], token)

    def test_every_identity_is_either_refused_or_survives_unchanged(self):
        for token in INVALID_TOKENS + VALID_TOKENS:
            with self.subTest(token=token):
                try:
                    marker = ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(workspace=token, lane=token),
                        workflow="check",
                        run="run",
                    )
                except ValueError:
                    continue
                read = strict_marker_fields_in_note(marker)
                # Asserted rather than unpacked: on the broken producer a whitespace-bearing value
                # rendered a marker that never closed on its line, so the reader saw NO marker at
                # all — that must read as a failed invariant, not as an unpacking error.
                self.assertTrue(read, f"rendered a marker no strict reader can see: {marker!r}")
                self.assertEqual(len(read), 1, f"rendered {len(read)} markers: {marker!r}")
                (_, fields), = read
                parsed = ev.parse_hibernate_evidence(
                    fields, kind=ev.EVIDENCE_REQUIRED_CI_GREEN
                )
                self.assertIsInstance(parsed, ev.HibernateEvidence, f"unreadable {marker!r}")
                self.assertEqual(parsed.envelope.workspace, token)
                self.assertEqual(parsed.envelope.lane, token)


if __name__ == "__main__":
    unittest.main()
