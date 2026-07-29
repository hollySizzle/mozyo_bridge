"""Redmine #14694: raw-invalid producer input must never become an authority marker.

The hibernate-evidence producers validated their inputs through ``str(value or "").strip()``
(introduced with the marker grammar in ``ab344781``, carried into ``_required`` by ``94f745b0``),
which normalized the caller's raw value BEFORE judging it. Independently reproduced in #14667
review j#93230: ``workflow=' check '`` / ``run=' run '`` were not refused — they were trimmed into
the clean canonical tokens ``check`` / ``run`` and became durable auto-hibernate authority. The
central `### Hibernate Evidence Marker Contract` requires the opposite: a producer does not
normalize raw input into a value the caller did not write, and "renderer は parser が拒否する
ものを書かない".

The same one-line pattern carried five more readings of the same defect, all of them measured on
the ``render_hibernate_evidence`` entry point rather than recalled:

- trimming (``" check "`` → ``check``) — the reported symptom;
- an INCOMPLETE forbidden set: ``\\n`` / ``\\r`` / ``\\xa0`` were not "空白" to a tuple that
  enumerated space and tab, so they were rendered — and markers are scanned per line, so the
  record never closed on its line and read back as nothing at all;
- ``str()`` coercion of a non-string (``run=12345`` → ``"12345"``, ``run=True`` → ``"True"``);
- ``or ""`` falsy coercion, which reported a wrong TYPE (``run=0`` / ``run=None``) as a missing
  field;
- ``conclusion`` accepted and discarded: ``conclusion="failure"`` was rendered as
  ``conclusion=success``, turning a caller's red verdict into green CI evidence;
- the same trim on the lane envelope's ``workspace`` / ``lane`` / ``head`` and on the
  ``integration_disposition`` marker's own fields.

Every test here pins the non-recurrence of that symptom on the producer surface. The population of
producer fields is DERIVED from the signature and the envelope dataclass, not listed: a new
producer field that skips raw validation must fail these tests rather than quietly join the hole.
"""

from __future__ import annotations

import dataclasses
import inspect
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    render_gate_note,
    strict_marker_fields_in_note,
)

WS = "ws-1"
LANE = "lane-abc"
HEAD = "a" * 40
INTEGRATION_HEAD = "b" * 40

#: Raw values a producer must refuse AS WRITTEN. The whitespace rows are the point: every one of
#: them is ``strip()``-ed or ``str()``-ed into a perfectly clean token by the old validator.
INVALID_TOKENS = (
    " check ",  # the reported symptom (#14667 j#93230)
    "check ",
    " check",
    "che ck",
    "che\tck",
    "che\nck",  # a marker scanned per line never closes → unreadable durable evidence
    "che\rck",
    "che\xa0ck",  # "空白" is not two characters
    "che:ck",  # splits into a bogus extra field
    "che]ck",  # truncates the marker
    "che[ck",
    " ",
    "\n",
)

#: Values that are not strings at all. ``None`` and ``0`` are the dangerous pair: ``or ""`` turned
#: both into "the caller supplied nothing", so a type error was reported as a missing field.
INVALID_TYPES = (None, 0, 1, 12345, True, False, 3.5, b"check", ["check"], {"check": 1})

#: Tokens that merely LOOK unusual and are perfectly renderable — the negative control that kills
#: an over-correction. A guard that also refuses these has stopped being a marker-grammar rule.
VALID_TOKENS = (
    "test.yml",
    "29860030313",
    "a-b_c",
    "#14184",
    "チェック",
    "a/b",
    "a=b",
    "%2F",
    "ci(main)",
    "v1.0.0-rc.1",
)


def _env(**over) -> LaneEvidenceEnvelope:
    fields = dict(workspace=WS, lane=LANE, lane_generation=3, head=HEAD)
    fields.update(over)
    return LaneEvidenceEnvelope(**fields)


#: The kind-specific producer parameters, DERIVED from the renderer's own signature, and the
#: fields each kind requires. A new parameter joins ``_PRODUCER_FIELDS`` automatically; the
#: coverage test below then fails until this file exercises it.
_PRODUCER_FIELDS = tuple(
    name
    for name in inspect.signature(ev.render_hibernate_evidence).parameters
    if name not in ("kind", "envelope")
)

#: What each kind's marker CARRIES. Written out here as the ORACLE rather than read off the
#: producer's own table: a test that derives its expectation from the implementation cannot catch
#: the implementation getting it wrong. The closure tests below tie it to the kind vocabulary and
#: to the renderer's signature, so a new kind or a new field cannot slip past this file either.
_CARRIED_BY_KIND = {
    ev.EVIDENCE_REQUIRED_CI_GREEN: (ev.FIELD_WORKFLOW, ev.FIELD_RUN, ev.FIELD_CONCLUSION),
    ev.EVIDENCE_DOGFOOD_DELEGATED: (ev.FIELD_RELEASE_ISSUE, ev.FIELD_ACCEPTANCE),
    ev.EVIDENCE_PARK_DECLARED: (),
}

#: ``conclusion`` is carried by CI but is producer-STATED, not caller-supplied, so it is never
#: part of a clean call.
_REQUIRED_BY_KIND = {
    kind: tuple(f for f in fields if f != ev.FIELD_CONCLUSION)
    for kind, fields in _CARRIED_BY_KIND.items()
}


def _clean_kwargs(kind: str) -> dict:
    """The minimal CLEAN call for ``kind`` — every field it requires, nothing else."""
    return {field: "clean" for field in _REQUIRED_BY_KIND[kind]}


class ReportedSymptomTests(unittest.TestCase):
    """The exact reproduction from #14667 review j#93230."""

    def test_padded_workflow_and_run_are_refused_not_trimmed(self):
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env(), workflow=" check ", run="run"
            )
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env(), workflow="check", run=" run "
            )

    def test_the_trimmed_authority_marker_is_never_produced(self):
        """The failure mode is not "an error was raised" — it is WHICH marker existed.

        The old producer emitted the byte-identical marker a clean caller would have produced, so
        the durable record could not tell the two apart. Nothing may render that marker from the
        padded input.
        """
        clean = ev.render_hibernate_evidence(
            ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env(), workflow="check", run="run"
        )
        self.assertIn("workflow=check:run=run", clean)
        for workflow, run in ((" check ", " run "), ("check ", "run"), ("check", " run")):
            with self.subTest(workflow=workflow, run=run):
                try:
                    padded = ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=_env(),
                        workflow=workflow,
                        run=run,
                    )
                except ValueError:
                    continue
                self.fail(f"raw-invalid input rendered the authority marker {padded!r}")


class EveryProducerFieldTests(unittest.TestCase):
    """The sweep over the DERIVED population, not over the two fields the issue named."""

    def test_the_derived_population_is_the_one_this_file_exercises(self):
        # If the renderer grows a parameter, this file must grow with it: the whole defect was a
        # validator applied to some producer inputs and not others. Both halves are closed here —
        # the parameter list against this file's oracle, and the oracle against the kind
        # vocabulary — so neither a new field nor a new kind can arrive unswept.
        self.assertEqual(
            set(_PRODUCER_FIELDS),
            {
                ev.FIELD_RUN,
                ev.FIELD_WORKFLOW,
                ev.FIELD_CONCLUSION,
                ev.FIELD_RELEASE_ISSUE,
                ev.FIELD_ACCEPTANCE,
            },
        )
        self.assertEqual(set(_CARRIED_BY_KIND), set(ev.HIBERNATE_EVIDENCE_KINDS))
        self.assertEqual(
            {f for fields in _CARRIED_BY_KIND.values() for f in fields}, set(_PRODUCER_FIELDS)
        )

    def test_the_oracle_matches_what_a_clean_marker_actually_carries(self):
        # The oracle is only worth something if it describes the real markers, so read it back off
        # the rendered output rather than off the producer's table.
        for kind, carried in sorted(_CARRIED_BY_KIND.items()):
            with self.subTest(kind=kind):
                head = HEAD if kind in ev._HEAD_BEARING_EVIDENCE else ""
                marker = ev.render_hibernate_evidence(
                    kind, envelope=_env(head=head), **_clean_kwargs(kind)
                )
                for field in _PRODUCER_FIELDS:
                    self.assertEqual(f"{field}=" in marker, field in carried, f"{field} in {marker}")

    def test_every_carried_field_refuses_a_raw_invalid_token(self):
        for kind, carried in sorted(_CARRIED_BY_KIND.items()):
            for field in carried:
                for bad in INVALID_TOKENS:
                    with self.subTest(kind=kind, field=field, value=bad):
                        kwargs = _clean_kwargs(kind)
                        kwargs[field] = bad
                        with self.assertRaises(ValueError):
                            ev.render_hibernate_evidence(kind, envelope=_env(), **kwargs)

    def test_every_carried_field_refuses_a_non_string(self):
        for kind, carried in sorted(_CARRIED_BY_KIND.items()):
            for field in carried:
                for bad in INVALID_TYPES:
                    with self.subTest(kind=kind, field=field, value=bad):
                        kwargs = _clean_kwargs(kind)
                        kwargs[field] = bad
                        with self.assertRaises(ValueError):
                            ev.render_hibernate_evidence(kind, envelope=_env(), **kwargs)

    def test_a_field_this_kind_cannot_carry_is_refused_even_when_valid(self):
        """The second shape of the same defect: a supplied value that is silently DROPPED.

        ``park_declared`` carries neither ``run`` nor ``workflow``, so a caller supplying one was
        asserting something the durable record would not contain. Validating those fields without
        refusing them would have left "the marker says what the caller wrote" false for exactly
        the callers who were wrong about the kind — and the raw-invalid case would then have been
        the only one anyone noticed.
        """
        for kind, carried in sorted(_CARRIED_BY_KIND.items()):
            for field in _PRODUCER_FIELDS:
                if field in carried:
                    continue
                with self.subTest(kind=kind, field=field):
                    kwargs = _clean_kwargs(kind)
                    # A perfectly VALID value: the refusal is about the field, not its shape.
                    kwargs[field] = "success" if field == ev.FIELD_CONCLUSION else "clean"
                    with self.assertRaises(ValueError):
                        ev.render_hibernate_evidence(
                            kind,
                            envelope=_env(head=HEAD if kind in ev._HEAD_BEARING_EVIDENCE else ""),
                            **kwargs,
                        )

    def test_conclusion_is_refused_rather_than_coerced_to_success(self):
        """``conclusion="failure"`` used to be rendered as ``conclusion=success``."""
        for bad in ("failure", "cancelled", "approved", "Success", "SUCCESS"):
            with self.subTest(conclusion=bad):
                with self.assertRaises(ValueError):
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=_env(),
                        workflow="check",
                        run="run",
                        conclusion=bad,
                    )


class EnvelopeIdentityTests(unittest.TestCase):
    """The envelope reaches the marker through the same producer call and had the same defect."""

    def test_the_derived_envelope_population_is_the_one_this_file_exercises(self):
        self.assertEqual(
            tuple(f.name for f in dataclasses.fields(LaneEvidenceEnvelope)),
            ("workspace", "lane", "lane_generation", "head"),
        )

    def test_string_identities_are_refused_raw(self):
        for field in ("workspace", "lane"):
            for bad in INVALID_TOKENS:
                with self.subTest(field=field, value=bad):
                    with self.assertRaises(ValueError):
                        ev.render_hibernate_evidence(
                            ev.EVIDENCE_REQUIRED_CI_GREEN,
                            envelope=_env(**{field: bad}),
                            workflow="check",
                            run="run",
                        )
            for bad in INVALID_TYPES:
                with self.subTest(field=field, value=bad):
                    with self.assertRaises(ValueError):
                        ev.render_hibernate_evidence(
                            ev.EVIDENCE_REQUIRED_CI_GREEN,
                            envelope=_env(**{field: bad}),
                            workflow="check",
                            run="run",
                        )

    def test_a_padded_head_is_refused_rather_than_trimmed_into_a_full_sha(self):
        for bad in (f" {HEAD}", f"{HEAD} ", f"\n{HEAD}", f"{HEAD}\t"):
            with self.subTest(head=bad):
                with self.assertRaises(ValueError):
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=_env(head=bad),
                        workflow="check",
                        run="run",
                    )

    def test_absent_head_is_the_empty_string_and_nothing_else(self):
        # ``park_declared`` is lane-anchored, so ``head=""`` renders. ``None`` is a producer error,
        # not a second spelling of absent: ``str(None or "")`` used to make them the same value.
        rendered = ev.render_hibernate_evidence(ev.EVIDENCE_PARK_DECLARED, envelope=_env(head=""))
        self.assertNotIn("head=", rendered)
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(ev.EVIDENCE_PARK_DECLARED, envelope=_env(head=None))


class IntegrationEvidenceProducerTests(unittest.TestCase):
    """The ``integration_disposition`` marker is the same surface with the same one-liner."""

    def _render(self, **over):
        kwargs = dict(
            envelope=_env(),
            integration_head=INTEGRATION_HEAD,
            integration_branch="main-next",
            disposition="merge",
        )
        kwargs.update(over)
        return ie.render_integration_evidence(**kwargs)

    def test_clean_input_still_renders(self):
        self.assertIn("integration_branch=main-next", self._render())

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
    """A guard the CALLER pre-normalizes is a guard that never runs."""

    def test_the_gate_note_producer_hands_the_envelope_over_raw(self):
        for bad in (" ws-1", "ws-1 ", "ws\n1"):
            with self.subTest(workspace=bad):
                with self.assertRaises(ValueError):
                    render_gate_note(
                        "review_result",
                        evidence_workspace=bad,
                        evidence_lane=LANE,
                        evidence_lane_generation="3",
                    )

    def test_the_gate_note_producer_refuses_a_bool_generation(self):
        # ``int(True)`` is ``1``, which would slip past the renderer's own ``bool`` refusal.
        with self.assertRaises(ValueError):
            render_gate_note(
                "review_result",
                evidence_workspace=WS,
                evidence_lane=LANE,
                evidence_lane_generation=True,
            )

    def test_the_cli_boundary_answers_with_a_typed_refusal_not_a_traceback(self):
        """Operator argv is normalized at ONE boundary, which then owes a typed refusal.

        The CLI check listed the punctuation tuple itself, so a ``\\n``-bearing identity passed it
        and reached the renderer — which now raises. An operator typo must stay a fixed refusal
        token, so the boundary asks the same question the renderer does.
        """
        import argparse

        for bad in ("lane\nb", "lane\rb", "lane\xa0b", "lane:b"):
            with self.subTest(lane=bad):
                args = argparse.Namespace(
                    evidence_workspace=WS,
                    evidence_lane=bad,
                    evidence_lane_generation="3",
                )
                fields, refusal = lane_envelope_marker_fields(args)
                self.assertEqual(fields, {})
                self.assertEqual(refusal, "evidence_envelope_malformed_identity")


class RendererParserAgreementTests(unittest.TestCase):
    """The contract's own invariant: "renderer は parser が拒否するものを書かない".

    Stated as a bidirectional oracle rather than a list of refusals — this is what the whitespace
    hole actually broke. A rendered marker must (1) survive the STRICT reader every authority
    consumer shares, and (2) parse back to the values the caller passed, unchanged. A producer that
    normalizes fails (2) even when it passes (1), which is exactly how ``" check "`` became
    ``check`` without anything looking wrong.
    """

    def _round_trip(self, kind: str, **kwargs) -> ev.HibernateEvidence:
        envelope = _env(head=kwargs.pop("head", HEAD))
        marker = ev.render_hibernate_evidence(kind, envelope=envelope, **kwargs)
        read = strict_marker_fields_in_note(marker)
        self.assertIsNotNone(read, f"the strict reader refused its own marker {marker!r}")
        (_, fields), = read
        parsed = ev.parse_hibernate_evidence(fields, kind=kind)
        self.assertIsInstance(parsed, ev.HibernateEvidence, f"unreadable evidence from {marker!r}")
        return parsed

    def test_anything_rendered_reads_back_as_exactly_what_was_passed(self):
        for token in VALID_TOKENS:
            with self.subTest(token=token):
                parsed = self._round_trip(
                    ev.EVIDENCE_REQUIRED_CI_GREEN, workflow=token, run=token
                )
                self.assertEqual(parsed.extra[ev.FIELD_WORKFLOW], token)
                self.assertEqual(parsed.extra[ev.FIELD_RUN], token)
                parsed = self._round_trip(
                    ev.EVIDENCE_DOGFOOD_DELEGATED, release_issue=token, acceptance=token
                )
                self.assertEqual(parsed.extra[ev.FIELD_RELEASE_ISSUE], token)
                self.assertEqual(parsed.extra[ev.FIELD_ACCEPTANCE], token)

    def test_the_envelope_reads_back_unchanged_too(self):
        for token in VALID_TOKENS:
            with self.subTest(token=token):
                marker = ev.render_hibernate_evidence(
                    ev.EVIDENCE_REQUIRED_CI_GREEN,
                    envelope=_env(workspace=token, lane=token),
                    workflow="check",
                    run="run",
                )
                (_, fields), = strict_marker_fields_in_note(marker)
                parsed = ev.parse_hibernate_evidence(fields, kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
                self.assertEqual(parsed.envelope.workspace, token)
                self.assertEqual(parsed.envelope.lane, token)

    def test_valid_but_unusual_tokens_still_render(self):
        # The over-correction control: a guard that refuses these is no longer a grammar rule.
        for token in VALID_TOKENS:
            with self.subTest(token=token):
                self.assertIn(
                    f"{ev.FIELD_WORKFLOW}={token}",
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=_env(),
                        workflow=token,
                        run="run",
                    ),
                )


class CleanOutputIsUnchangedTests(unittest.TestCase):
    """Byte pins. The fix may only remove markers that should never have existed."""

    def test_ci_green_marker_is_byte_identical(self):
        self.assertEqual(
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN,
                envelope=_env(),
                workflow="test.yml",
                run="29860030313",
            ),
            "[mozyo:workflow-event:gate=required_ci_green:"
            f"workspace={WS}:lane={LANE}:lane_generation=3:head={HEAD}:"
            "workflow=test.yml:run=29860030313:conclusion=success]",
        )

    def test_an_explicitly_supplied_success_conclusion_changes_nothing(self):
        self.assertEqual(
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN,
                envelope=_env(),
                workflow="test.yml",
                run="1",
                conclusion="success",
            ),
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env(), workflow="test.yml", run="1"
            ),
        )

    def test_dogfood_marker_is_byte_identical(self):
        self.assertEqual(
            ev.render_hibernate_evidence(
                ev.EVIDENCE_DOGFOOD_DELEGATED,
                envelope=_env(),
                release_issue="14184",
                acceptance="85431",
            ),
            "[mozyo:workflow-event:gate=dogfood_delegated:"
            f"workspace={WS}:lane={LANE}:lane_generation=3:head={HEAD}:"
            "release_issue=14184:acceptance=85431]",
        )

    def test_park_marker_is_byte_identical(self):
        self.assertEqual(
            ev.render_hibernate_evidence(ev.EVIDENCE_PARK_DECLARED, envelope=_env(head="")),
            f"[mozyo:workflow-event:gate=park_declared:workspace={WS}:lane={LANE}:"
            "lane_generation=3]",
        )

    def test_integration_marker_is_byte_identical(self):
        self.assertEqual(
            ie.render_integration_evidence(
                envelope=_env(),
                integration_head=INTEGRATION_HEAD,
                integration_branch="main-next",
                disposition="merge",
            ),
            "[mozyo:workflow-event:gate=integration_disposition:"
            f"workspace={WS}:lane={LANE}:lane_generation=3:head={HEAD}:"
            f"integration_head={INTEGRATION_HEAD}:integration_branch=main-next:"
            "disposition=merge]",
        )


class ConsumerIsUnchangedTests(unittest.TestCase):
    """The fix is producer-side. A tightened READER would be a different change (non-goal)."""

    def _ci_fields(self, **over) -> dict:
        fields = {
            "gate": ev.EVIDENCE_REQUIRED_CI_GREEN,
            "workspace": WS,
            "lane": LANE,
            "lane_generation": "3",
            "head": HEAD,
            "workflow": "test.yml",
            "run": "1",
            "conclusion": "success",
        }
        fields.update(over)
        return fields

    def test_the_parser_still_accepts_what_it_accepted(self):
        parsed = ev.parse_hibernate_evidence(
            self._ci_fields(), kind=ev.EVIDENCE_REQUIRED_CI_GREEN
        )
        self.assertIsInstance(parsed, ev.HibernateEvidence)

    def test_the_parser_still_refuses_with_the_same_typed_reasons(self):
        for over, reason in (
            ({"run": ""}, ev.EVIDENCE_MISSING_RUN),
            ({"workflow": ""}, ev.EVIDENCE_MISSING_WORKFLOW),
            ({"conclusion": "failure"}, ev.EVIDENCE_CI_NOT_SUCCESS),
        ):
            with self.subTest(over=over):
                got = ev.parse_hibernate_evidence(
                    self._ci_fields(**over), kind=ev.EVIDENCE_REQUIRED_CI_GREEN
                )
                self.assertEqual(got.reason, reason)

    def test_duplicate_and_conflict_resolution_is_unchanged(self):
        one = self._ci_fields()
        self.assertIsInstance(
            ev.resolve_hibernate_evidence([one, dict(one)], kind=ev.EVIDENCE_REQUIRED_CI_GREEN),
            ev.HibernateEvidence,
        )
        self.assertEqual(
            ev.resolve_hibernate_evidence(
                [one, self._ci_fields(run="2")], kind=ev.EVIDENCE_REQUIRED_CI_GREEN
            ).reason,
            ev.EVIDENCE_CONFLICT,
        )
        self.assertEqual(
            ev.resolve_hibernate_evidence([], kind=ev.EVIDENCE_REQUIRED_CI_GREEN).reason,
            ev.EVIDENCE_ABSENT,
        )


if __name__ == "__main__":
    unittest.main()
