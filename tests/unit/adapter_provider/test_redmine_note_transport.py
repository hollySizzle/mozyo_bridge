"""Live Redmine journal-write transport tests (Redmine #12347).

Covers the explicit live-write env opt-in, the trusted-base / credential
boundary (destination is the trusted env URL only, never a caller-supplied one),
the fail-closed reason mapping (provider_unavailable / credential_missing /
unauthorized / transport_error), the success path (PUT + 204 -> empty journal
id), and the no-credential-leak guarantee on the surfaced reason.

Abstract placeholders are used deliberately — no personal home path or
secret-shaped literal in tracked test files
(`vibes/docs/rules/public-private-boundary.md`). The trusted host is the
non-routable example host `https://redmine.example.test`; the API key sentinel is
`DROP-APIKEY-SENTINEL`.
"""

from __future__ import annotations

import sys
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.delivery_record_sink import (
    PERSIST_CREDENTIAL_MISSING,
    PERSIST_PROVIDER_UNAVAILABLE,
    PERSIST_TRANSPORT_ERROR,
    PERSIST_UNAUTHORIZED,
    DeliveryTransportError,
)
from mozyo_bridge.infrastructure.redmine_note_transport import (
    DELIVERY_WRITE_ENV,
    RedmineNoteHttpTransport,
    redmine_delivery_transport_from_env,
)
from mozyo_bridge.redmine_context import API_KEY_ENV, BASE_URL_ENV

TRUSTED_BASE = "https://redmine.example.test"
API_KEY = "DROP-APIKEY-SENTINEL"


class _FakeHTTPResponse:
    """Minimal context-manager stand-in for a urlopen 204 response."""

    def __init__(self, code: int = 204):
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


class FromEnvOptInTest(unittest.TestCase):
    def test_no_opt_in_returns_none(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(redmine_delivery_transport_from_env())

    def test_falsey_opt_in_returns_none(self) -> None:
        for value in ("", "0", "false", "no", "off", "  "):
            with patch.dict("os.environ", {DELIVERY_WRITE_ENV: value}, clear=True):
                self.assertIsNone(
                    redmine_delivery_transport_from_env(),
                    msg=f"{value!r} must not enable the live write",
                )

    def test_truthy_opt_in_builds_transport(self) -> None:
        for value in ("1", "true", "YES", "On"):
            with patch.dict("os.environ", {DELIVERY_WRITE_ENV: value}, clear=True):
                transport = redmine_delivery_transport_from_env()
                self.assertIsInstance(transport, RedmineNoteHttpTransport)


class TransportFailClosedTest(unittest.TestCase):
    def test_missing_base_url_is_provider_unavailable(self) -> None:
        with patch.dict("os.environ", {API_KEY_ENV: API_KEY}, clear=True):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_PROVIDER_UNAVAILABLE, ctx.exception.reason)

    def test_invalid_base_url_is_provider_unavailable(self) -> None:
        env = {BASE_URL_ENV: "not-a-url", API_KEY_ENV: API_KEY}
        with patch.dict("os.environ", env, clear=True):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_PROVIDER_UNAVAILABLE, ctx.exception.reason)

    def test_missing_api_key_is_credential_missing(self) -> None:
        with patch.dict("os.environ", {BASE_URL_ENV: TRUSTED_BASE}, clear=True):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_CREDENTIAL_MISSING, ctx.exception.reason)

    def _http_error(self, code: int):
        return urllib.error.HTTPError(
            url="x", code=code, msg="m", hdrs=None, fp=None
        )

    def test_http_401_is_unauthorized(self) -> None:
        env = {BASE_URL_ENV: TRUSTED_BASE, API_KEY_ENV: API_KEY}
        with patch.dict("os.environ", env, clear=True), patch(
            "urllib.request.urlopen", side_effect=self._http_error(401)
        ):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_UNAUTHORIZED, ctx.exception.reason)

    def test_http_403_is_unauthorized(self) -> None:
        env = {BASE_URL_ENV: TRUSTED_BASE, API_KEY_ENV: API_KEY}
        with patch.dict("os.environ", env, clear=True), patch(
            "urllib.request.urlopen", side_effect=self._http_error(403)
        ):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_UNAUTHORIZED, ctx.exception.reason)

    def test_http_404_is_transport_error(self) -> None:
        env = {BASE_URL_ENV: TRUSTED_BASE, API_KEY_ENV: API_KEY}
        with patch.dict("os.environ", env, clear=True), patch(
            "urllib.request.urlopen", side_effect=self._http_error(404)
        ):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_TRANSPORT_ERROR, ctx.exception.reason)

    def test_network_error_is_transport_error(self) -> None:
        env = {BASE_URL_ENV: TRUSTED_BASE, API_KEY_ENV: API_KEY}
        with patch.dict("os.environ", env, clear=True), patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("unreachable"),
        ):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertEqual(PERSIST_TRANSPORT_ERROR, ctx.exception.reason)

    def test_failure_reason_carries_no_credential(self) -> None:
        # Even a careless transport must never leak the key into the surfaced
        # reason. (The exception message is for diagnostics and is never copied
        # onto a receipt, but assert the reason is clean regardless.)
        env = {BASE_URL_ENV: TRUSTED_BASE, API_KEY_ENV: API_KEY}
        with patch.dict("os.environ", env, clear=True), patch(
            "urllib.request.urlopen", side_effect=self._http_error(401)
        ):
            transport = RedmineNoteHttpTransport()
            with self.assertRaises(DeliveryTransportError) as ctx:
                transport.post_issue_note("12347", "note body")
            self.assertNotIn(API_KEY, ctx.exception.reason)
            self.assertNotIn(API_KEY, str(ctx.exception))


class TransportSuccessTest(unittest.TestCase):
    def test_success_puts_to_trusted_base_and_returns_empty_id(self) -> None:
        env = {BASE_URL_ENV: TRUSTED_BASE, API_KEY_ENV: API_KEY}
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["url"] = request.full_url
            sent["method"] = request.get_method()
            sent["data"] = request.data
            sent["api_key"] = request.get_header("X-redmine-api-key")
            return _FakeHTTPResponse(code=204)

        with patch.dict("os.environ", env, clear=True), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            transport = RedmineNoteHttpTransport()
            journal_id = transport.post_issue_note("12347", "redacted body")

        # 204 has no journal id; the protocol allows the empty id.
        self.assertEqual("", journal_id)
        # Destination is the TRUSTED base + the issue path, nothing else.
        self.assertEqual(f"{TRUSTED_BASE}/issues/12347.json", sent["url"])
        self.assertEqual("PUT", sent["method"])
        self.assertEqual(API_KEY, sent["api_key"])
        # The note body is the request payload; the key is only a header.
        self.assertIn(b"redacted body", sent["data"])
        self.assertNotIn(API_KEY.encode(), sent["data"])

    def test_explicit_base_is_normalized_not_a_redirect_vector(self) -> None:
        # An explicit base (test / future trusted caller) is still reduced to
        # scheme://host by normalize_base_url, so a path/query can never ride in.
        with patch.dict("os.environ", {API_KEY_ENV: API_KEY}, clear=True):
            captured = {}

            def fake_urlopen(request, timeout=None):
                captured["url"] = request.full_url
                return _FakeHTTPResponse(code=204)

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                transport = RedmineNoteHttpTransport(
                    base_url="https://redmine.example.test/evil/path?x=1"
                )
                transport.post_issue_note("12347", "body")

        self.assertEqual(
            "https://redmine.example.test/issues/12347.json", captured["url"]
        )


if __name__ == "__main__":
    unittest.main()
