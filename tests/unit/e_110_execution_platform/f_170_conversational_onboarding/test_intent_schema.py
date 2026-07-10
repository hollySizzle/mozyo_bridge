"""Closed OnboardingIntent schema validation (Redmine #13498 / #13501)."""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.intent import (
    IntentError,
    OnboardingIntent,
    validate_onboarding_intent,
)


def _valid_record(**overrides) -> dict:
    record = {
        "schema_version": 1,
        "action": "propose",
        "preset": "none",
        "backend": "herdr",
        "git_mode": "none",
        "rules_store": "central",
        "free_text_summary": "adopt this folder",
    }
    record.update(overrides)
    return record


class IntentHappyPathTests(unittest.TestCase):
    def test_valid_record_parses(self) -> None:
        intent = validate_onboarding_intent(_valid_record())
        self.assertIsInstance(intent, OnboardingIntent)
        self.assertEqual(intent.preset, "none")
        self.assertFalse(intent.preset_undecided)

    def test_undecided_preset_is_accepted_by_validator(self) -> None:
        intent = validate_onboarding_intent(_valid_record(preset="undecided"))
        self.assertTrue(intent.preset_undecided)


class IntentRejectTests(unittest.TestCase):
    def _err(self, record) -> IntentError:
        with self.assertRaises(IntentError) as ctx:
            validate_onboarding_intent(record)
        return ctx.exception

    def test_non_mapping_rejected(self) -> None:
        self.assertEqual(self._err(["not", "a", "map"]).code, "not_a_mapping")

    def test_unknown_key_rejected(self) -> None:
        rec = _valid_record()
        rec["shell"] = "rm -rf /"
        self.assertEqual(self._err(rec).code, "unknown_key")

    def test_missing_field_rejected(self) -> None:
        rec = _valid_record()
        del rec["backend"]
        err = self._err(rec)
        self.assertEqual(err.code, "missing_field")
        self.assertEqual(err.field, "backend")

    def test_unknown_enum_rejected(self) -> None:
        self.assertEqual(self._err(_valid_record(preset="rails")).code, "unknown_enum")

    def test_unsupported_schema_version_rejected(self) -> None:
        self.assertEqual(
            self._err(_valid_record(schema_version=2)).code,
            "unsupported_schema_version",
        )

    def test_non_string_enum_rejected(self) -> None:
        self.assertEqual(self._err(_valid_record(action=3)).code, "non_string_enum")

    def test_shell_injection_in_enum_rejected(self) -> None:
        err = self._err(_valid_record(preset="none; rm -rf /"))
        self.assertEqual(err.code, "field_shaped_like_injection")
        self.assertEqual(err.field, "preset")

    def test_credential_shaped_value_rejected(self) -> None:
        err = self._err(_valid_record(rules_store="token=abc123"))
        self.assertEqual(err.code, "field_shaped_like_injection")

    def test_pem_key_block_rejected(self) -> None:
        err = self._err(
            _valid_record(git_mode="-----BEGIN RSA PRIVATE KEY-----")
        )
        self.assertEqual(err.code, "field_shaped_like_injection")

    def test_free_text_summary_is_not_treated_as_mutation_input(self) -> None:
        # A summary may contain arbitrary prose (it is display-only); it must not
        # be rejected as injection, and it does not affect any enum field.
        intent = validate_onboarding_intent(
            _valid_record(free_text_summary="please run `rm -rf /`; token=x")
        )
        self.assertEqual(intent.preset, "none")


if __name__ == "__main__":
    unittest.main()
