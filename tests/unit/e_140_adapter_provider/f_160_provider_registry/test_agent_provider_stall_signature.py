"""Unit tests for the provider stall-signature schema + packaged artifact (#15843).

Two kinds of assertion live here, and the second is the point of the file:

- **schema** — that a malformed or over-reaching declaration fails closed at load;
- **the packaged artifact itself** — that every shipped signature actually satisfies the
  rules the schema claims to enforce. A validator can only refuse what it is handed, and
  the artifact is the one input nobody re-validates by hand, so the shipped data is
  checked against the same invariants rather than assumed to comply.

Pure: the artifact is read through ``importlib.resources`` (packaged data, not a repo
path walk) and everything else is an in-memory record.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_CONTENT_REFUSAL,
    CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
    CLASS_UNSENT_COMPOSER,
    EVIDENCE_BINARY_READ_UNRENDERED,
    EVIDENCE_RENDERED_CONFIRMED,
    EVIDENCE_TIERS,
    STALL_CLASSES,
    UNRENDERED_ADMISSIBLE_CLASSES,
    prescribe,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_stall_signature import (  # noqa: E501
    MAX_SIGNATURE_SUBSTRINGS,
    SUPPORTED_SCHEMA_VERSIONS,
    UNDECLARABLE_CLASSES,
    StallSignature,
    StallSignatureError,
    StallSignatureRegistry,
    first_match,
    load_stall_signature_registry,
)


def _record(**signature):
    base = {
        "id": "s1",
        "asserts": CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
        "evidence": EVIDENCE_BINARY_READ_UNRENDERED,
        "all_of": ["alpha"],
    }
    base.update(signature)
    return {"version": "1", "providers": {"claude": {"stall_signatures": [base]}}}


class SchemaFailClosedTest(unittest.TestCase):
    def test_an_unknown_schema_version_fails_closed(self):
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record({"version": "99", "providers": {}})

    def test_an_unknown_asserted_class_is_refused(self):
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record(_record(asserts="probably_stalled"))

    def test_an_unknown_evidence_tier_is_refused(self):
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record(_record(evidence="trust_me"))

    def test_unrendered_evidence_cannot_assert_a_destructive_class(self):
        # The whole safety argument for shipping unrendered literals is that they can only
        # reach a non-destructive prescription. Promoting one to content_refusal — whose
        # remedy discards a live session's context — must not be a one-line data edit.
        with self.assertRaises(StallSignatureError) as caught:
            StallSignatureRegistry.from_record(
                _record(asserts=CLASS_CONTENT_REFUSAL)
            )
        self.assertIn(EVIDENCE_BINARY_READ_UNRENDERED, str(caught.exception))

    def test_rendered_evidence_may_assert_a_destructive_class(self):
        # The tier gate must not be a blanket ban: rendered-confirmed evidence is exactly
        # what the #14741 standard admits, and the schema has to let it through.
        registry = StallSignatureRegistry.from_record(
            _record(asserts=CLASS_CONTENT_REFUSAL, evidence=EVIDENCE_RENDERED_CONFIRMED)
        )
        self.assertEqual(
            registry.for_provider("claude")[0].asserts, CLASS_CONTENT_REFUSAL
        )

    def test_unsent_composer_is_not_declarable_at_any_evidence_tier(self):
        # It is established by composer evidence against the dispatched body (#15842),
        # never by a substring a data file was allowed to invent.
        for evidence in EVIDENCE_TIERS:
            with self.subTest(evidence=evidence):
                with self.assertRaises(StallSignatureError):
                    StallSignatureRegistry.from_record(
                        _record(asserts=CLASS_UNSENT_COMPOSER, evidence=evidence)
                    )

    def test_an_empty_and_list_is_refused(self):
        # It would match every screen.
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record(_record(all_of=[]))

    def test_a_blank_substring_is_refused(self):
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record(_record(all_of=["   "]))

    def test_an_over_long_and_list_is_refused(self):
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record(
                _record(all_of=["x"] * (MAX_SIGNATURE_SUBSTRINGS + 1))
            )

    def test_duplicate_signature_ids_for_one_provider_are_refused(self):
        record = _record()
        record["providers"]["claude"]["stall_signatures"].append(
            dict(record["providers"]["claude"]["stall_signatures"][0])
        )
        with self.assertRaises(StallSignatureError):
            StallSignatureRegistry.from_record(record)

    def test_the_undeclarable_set_is_exactly_the_composer_class(self):
        self.assertEqual(UNDECLARABLE_CLASSES, {CLASS_UNSENT_COMPOSER})


class MatchingTest(unittest.TestCase):
    def test_all_substrings_must_be_co_located_on_one_screen(self):
        signature = StallSignature(
            "s", CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
            EVIDENCE_BINARY_READ_UNRENDERED, ("alpha", "beta"),
        )
        self.assertTrue(signature.matches("...alpha... ...beta..."))
        self.assertFalse(signature.matches("...alpha... only"))

    def test_a_provider_with_no_declaration_matches_nothing(self):
        registry = StallSignatureRegistry.from_record({"version": "1", "providers": {}})
        self.assertEqual(registry.for_provider("claude"), ())
        self.assertIsNone(first_match(registry.for_provider("claude"), "anything"))


class PackagedArtifactTest(unittest.TestCase):
    """The shipped data must itself satisfy the rules the schema claims to enforce."""

    @classmethod
    def setUpClass(cls):
        cls.registry = load_stall_signature_registry()

    def test_the_packaged_artifact_loads_on_a_supported_version(self):
        self.assertIn(self.registry.schema_version, SUPPORTED_SCHEMA_VERSIONS)

    def test_both_built_in_providers_are_present(self):
        self.assertLessEqual({"claude", "codex"}, set(self.registry.signatures))

    def test_every_shipped_signature_is_within_the_declared_vocabulary(self):
        shipped = [
            signature
            for signatures in self.registry.signatures.values()
            for signature in signatures
        ]
        self.assertTrue(shipped, "the artifact ships no signatures at all")
        for signature in shipped:
            with self.subTest(signature=signature.signature_id):
                self.assertIn(signature.asserts, STALL_CLASSES)
                self.assertIn(signature.evidence, EVIDENCE_TIERS)

    def test_every_shipped_unrendered_signature_prescribes_only_patience(self):
        # The end-to-end form of the evidence argument: for every signature shipped at
        # the weaker tier, the recommended action is identical to the one the
        # no-signature-matched case already gets. So a wrong literal changes the reported
        # reason and never the recommended action.
        baseline = prescribe("unresponsive_indeterminate")
        for provider, signatures in self.registry.signatures.items():
            for signature in signatures:
                if signature.evidence != EVIDENCE_BINARY_READ_UNRENDERED:
                    continue
                with self.subTest(provider=provider, signature=signature.signature_id):
                    self.assertIn(signature.asserts, UNRENDERED_ADMISSIBLE_CLASSES)
                    self.assertEqual(
                        prescribe(signature.asserts).action, baseline.action
                    )
                    self.assertFalse(
                        prescribe(signature.asserts).relaunch_is_a_candidate
                    )

    def test_no_content_refusal_signature_is_shipped_undeclared(self):
        # A recorded residual, asserted rather than left to a comment: the CLASS is
        # supported and the DATA is deliberately absent until a rendered observation
        # exists (the #14741 standard). If someone adds one, this test is the prompt to
        # confirm they rendered it rather than recalled it.
        shipped = [
            signature.signature_id
            for signatures in self.registry.signatures.values()
            for signature in signatures
            if signature.asserts == CLASS_CONTENT_REFUSAL
        ]
        self.assertEqual(
            shipped,
            [],
            "a content_refusal signature was added: confirm it was read from the shipped "
            "binary AND rendered, then update this residual",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
