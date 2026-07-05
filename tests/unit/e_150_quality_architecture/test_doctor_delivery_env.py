"""Fake-port / pure-policy specifications for the doctor persist-delivery
env-presence boundary (Redmine #13262).

These pin the section-dict shape, the presence-boolean semantics, the
always-``ok`` informational status, and — critically — the credential boundary:
the section reports only set/unset booleans and never a value, so a base URL or
API key can never leak into the doctor output.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mozyo_bridge.application.doctor_delivery_env import (
    DELIVERY_ENV_VARS,
    DeliveryEnvReads,
    DeliveryEnvSectionUseCase,
    LiveDeliveryEnvReads,
    evaluate_delivery_env_section,
)

WRITE_ENV = "MOZYO_REDMINE_DELIVERY_WRITE"
URL_ENV = "MOZYO_REDMINE_URL"
KEY_ENV = "MOZYO_REDMINE_API_KEY"


class EvaluatePolicyTest(unittest.TestCase):
    def test_all_unset_reports_all_false_and_status_ok(self) -> None:
        section = evaluate_delivery_env_section({})
        self.assertEqual("ok", section["status"])
        self.assertFalse(section["write_optin_set"])
        self.assertFalse(section["base_url_set"])
        self.assertFalse(section["api_key_set"])

    def test_all_set_reports_all_true(self) -> None:
        section = evaluate_delivery_env_section(
            {WRITE_ENV: True, URL_ENV: True, KEY_ENV: True}
        )
        self.assertTrue(section["write_optin_set"])
        self.assertTrue(section["base_url_set"])
        self.assertTrue(section["api_key_set"])

    def test_partial_presence_is_reported_per_var(self) -> None:
        section = evaluate_delivery_env_section({WRITE_ENV: True})
        self.assertTrue(section["write_optin_set"])
        self.assertFalse(section["base_url_set"])
        self.assertFalse(section["api_key_set"])

    def test_status_is_always_ok_never_drags_verdict(self) -> None:
        # Even fully unset (the common default) is not a health fault.
        self.assertEqual("ok", evaluate_delivery_env_section({})["status"])

    def test_section_carries_only_booleans_and_status(self) -> None:
        section = evaluate_delivery_env_section({URL_ENV: True})
        self.assertEqual({"status"}, {k for k, v in section.items() if isinstance(v, str)})
        bool_fields = {k for k, v in section.items() if isinstance(v, bool)}
        self.assertEqual(
            {"write_optin_set", "base_url_set", "api_key_set"}, bool_fields
        )


class LiveReadsTest(unittest.TestCase):
    def test_reads_presence_from_environ(self) -> None:
        env = {WRITE_ENV: "1", URL_ENV: "https://redmine.example.test"}
        with patch.dict("os.environ", env, clear=True):
            presence = LiveDeliveryEnvReads().env_presence()
        self.assertTrue(presence[WRITE_ENV])
        self.assertTrue(presence[URL_ENV])
        self.assertFalse(presence[KEY_ENV])

    def test_empty_or_whitespace_value_reads_as_unset(self) -> None:
        with patch.dict("os.environ", {WRITE_ENV: "", URL_ENV: "   "}, clear=True):
            presence = LiveDeliveryEnvReads().env_presence()
        self.assertFalse(presence[WRITE_ENV])
        self.assertFalse(presence[URL_ENV])

    def test_live_adapter_satisfies_the_port(self) -> None:
        self.assertIsInstance(LiveDeliveryEnvReads(), DeliveryEnvReads)

    def test_env_vars_tuple_covers_the_three_gates(self) -> None:
        self.assertEqual((WRITE_ENV, URL_ENV, KEY_ENV), DELIVERY_ENV_VARS)


class NoValueLeakTest(unittest.TestCase):
    def test_section_never_contains_the_env_values(self) -> None:
        secret_url = "https://redmine.secret-host.example/path"
        secret_key = "DROP-APIKEY-SENTINEL-XYZ"
        env = {WRITE_ENV: "1", URL_ENV: secret_url, KEY_ENV: secret_key}
        with patch.dict("os.environ", env, clear=True):
            section = DeliveryEnvSectionUseCase(LiveDeliveryEnvReads()).execute()
        rendered = repr(section)
        self.assertNotIn(secret_url, rendered)
        self.assertNotIn(secret_key, rendered)
        # Presence is still reported (booleans only).
        self.assertTrue(section["base_url_set"])
        self.assertTrue(section["api_key_set"])


if __name__ == "__main__":
    unittest.main()
