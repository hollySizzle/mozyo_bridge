"""Fake-port / pure-policy specifications for the doctor persist-delivery
credential-resolution boundary (Redmine #13262 / #15698).

These pin the section-dict shape, the resolver-provenance semantics
(``env`` / ``file`` / ``unresolved`` — matching what the write transport
actually uses since Redmine #15692), the always-``ok`` informational status,
and — critically — the credential boundary: the section reports only the
opt-in boolean and source labels and never a value, so a base URL or API key
can never leak into the doctor output whether it came from the environment or
from the home-scoped credential file.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mozyo_bridge.application.doctor_delivery_env import (
    CREDENTIAL_FIELDS,
    CREDENTIAL_SOURCES,
    UNRESOLVED,
    DeliveryEnvReads,
    DeliveryEnvSectionUseCase,
    LiveDeliveryEnvReads,
    evaluate_delivery_env_section,
)

WRITE_ENV = "MOZYO_REDMINE_DELIVERY_WRITE"
URL_ENV = "MOZYO_REDMINE_URL"
KEY_ENV = "MOZYO_REDMINE_API_KEY"


def _write_credential_file(home: Path, *, url: str | None, key: str | None) -> None:
    lines = ["redmine:"]
    if key is not None:
        lines.append(f"  api_key: {key}")
    if url is not None:
        lines.append(f"  url: {url}")
    path = home / "redmine-credentials.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


class EvaluatePolicyTest(unittest.TestCase):
    def test_all_unresolved_reports_unresolved_and_status_ok(self) -> None:
        section = evaluate_delivery_env_section(False, {})
        self.assertEqual("ok", section["status"])
        self.assertFalse(section["write_optin_set"])
        self.assertEqual(UNRESOLVED, section["base_url_source"])
        self.assertEqual(UNRESOLVED, section["api_key_source"])

    def test_resolver_sources_are_reported_per_field(self) -> None:
        section = evaluate_delivery_env_section(
            True, {"base_url": "env", "api_key": "file"}
        )
        self.assertTrue(section["write_optin_set"])
        self.assertEqual("env", section["base_url_source"])
        self.assertEqual("file", section["api_key_source"])

    def test_none_source_collapses_to_unresolved(self) -> None:
        section = evaluate_delivery_env_section(
            True, {"base_url": None, "api_key": "env"}
        )
        self.assertEqual(UNRESOLVED, section["base_url_source"])
        self.assertEqual("env", section["api_key_source"])

    def test_unknown_source_token_collapses_to_unresolved(self) -> None:
        # A resolver refactor introducing a new label must degrade safely,
        # never propagate an unexpected token (or a value) into the section.
        section = evaluate_delivery_env_section(
            True, {"base_url": "https://redmine.secret.example", "api_key": "keyring"}
        )
        self.assertEqual(UNRESOLVED, section["base_url_source"])
        self.assertEqual(UNRESOLVED, section["api_key_source"])

    def test_status_is_always_ok_never_drags_verdict(self) -> None:
        # Even fully unresolved (the common default) is not a health fault.
        self.assertEqual("ok", evaluate_delivery_env_section(False, {})["status"])

    def test_section_carries_only_optin_bool_and_source_strings(self) -> None:
        section = evaluate_delivery_env_section(True, {"base_url": "env"})
        self.assertEqual(
            {"status", "base_url_source", "api_key_source"},
            {k for k, v in section.items() if isinstance(v, str)},
        )
        self.assertEqual(
            {"write_optin_set"},
            {k for k, v in section.items() if isinstance(v, bool)},
        )

    def test_source_vocabulary_constants(self) -> None:
        self.assertEqual(("env", "file"), CREDENTIAL_SOURCES)
        self.assertEqual(("base_url", "api_key"), CREDENTIAL_FIELDS)
        self.assertEqual("unresolved", UNRESOLVED)


class LiveReadsTest(unittest.TestCase):
    def test_optin_presence_from_environ(self) -> None:
        with patch.dict("os.environ", {WRITE_ENV: "1"}, clear=True):
            self.assertTrue(LiveDeliveryEnvReads().write_optin_set())
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(LiveDeliveryEnvReads().write_optin_set())

    def test_empty_or_whitespace_optin_reads_as_unset(self) -> None:
        with patch.dict("os.environ", {WRITE_ENV: "   "}, clear=True):
            self.assertFalse(LiveDeliveryEnvReads().write_optin_set())

    def test_env_supplied_credentials_report_env_source(self) -> None:
        env = {URL_ENV: "https://redmine.example.test", KEY_ENV: "k"}
        with TemporaryDirectory() as home, patch.dict("os.environ", env, clear=True):
            sources = LiveDeliveryEnvReads(Path(home)).credential_sources()
        self.assertEqual({"base_url": "env", "api_key": "env"}, sources)

    def test_file_supplied_credentials_report_file_source(self) -> None:
        with TemporaryDirectory() as home, patch.dict("os.environ", {}, clear=True):
            _write_credential_file(
                Path(home), url="https://redmine.example.test", key="k"
            )
            sources = LiveDeliveryEnvReads(Path(home)).credential_sources()
        self.assertEqual({"base_url": "file", "api_key": "file"}, sources)

    def test_unconfigured_reports_none_per_field(self) -> None:
        with TemporaryDirectory() as home, patch.dict("os.environ", {}, clear=True):
            sources = LiveDeliveryEnvReads(Path(home)).credential_sources()
        self.assertEqual({"base_url": None, "api_key": None}, sources)

    def test_per_field_mix_env_wins_file_fills_gap(self) -> None:
        env = {URL_ENV: "https://redmine.example.test"}
        with TemporaryDirectory() as home, patch.dict("os.environ", env, clear=True):
            _write_credential_file(Path(home), url=None, key="k")
            sources = LiveDeliveryEnvReads(Path(home)).credential_sources()
        self.assertEqual({"base_url": "env", "api_key": "file"}, sources)

    def test_live_adapter_satisfies_the_port(self) -> None:
        self.assertIsInstance(LiveDeliveryEnvReads(), DeliveryEnvReads)


class NoValueLeakTest(unittest.TestCase):
    def test_section_never_contains_env_supplied_values(self) -> None:
        secret_url = "https://redmine.secret-host.example/path"
        secret_key = "DROP-APIKEY-SENTINEL-XYZ"
        env = {WRITE_ENV: "1", URL_ENV: secret_url, KEY_ENV: secret_key}
        with TemporaryDirectory() as home, patch.dict("os.environ", env, clear=True):
            section = DeliveryEnvSectionUseCase(
                LiveDeliveryEnvReads(Path(home))
            ).execute()
        rendered = repr(section)
        self.assertNotIn(secret_url, rendered)
        self.assertNotIn(secret_key, rendered)
        self.assertTrue(section["write_optin_set"])
        self.assertEqual("env", section["base_url_source"])
        self.assertEqual("env", section["api_key_source"])

    def test_section_never_contains_file_supplied_values(self) -> None:
        secret_url = "https://redmine.file-host.example/path"
        secret_key = "DROP-FILEKEY-SENTINEL-ABC"
        with TemporaryDirectory() as home, patch.dict("os.environ", {}, clear=True):
            _write_credential_file(Path(home), url=secret_url, key=secret_key)
            section = DeliveryEnvSectionUseCase(
                LiveDeliveryEnvReads(Path(home))
            ).execute()
        rendered = repr(section)
        self.assertNotIn(secret_url, rendered)
        self.assertNotIn(secret_key, rendered)
        self.assertEqual("file", section["base_url_source"])
        self.assertEqual("file", section["api_key_source"])


if __name__ == "__main__":
    unittest.main()
