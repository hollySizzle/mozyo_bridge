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
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_integration as ie,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_marker as ev,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    render_gate_note,
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
