"""Unit tests for the live project-version read (Redmine #13687 Increment 1).

Covers the second read-only endpoint on the trusted Redmine client —
``GET /projects/<identifier>/versions.json`` (the project-scoped list, per the official
REST contract and j#76650's endpoint correction) — which supplies the Version *status*
that gates a lane bucket.

Pinned here: the endpoint shape and the trusted destination; the credential gates; the
paginating / partial-read fail-closed contract shared with the issue source; and that a
project with genuinely no Versions is the only legitimate empty result. Hermetic: the
opener is injected, so no test touches a real Redmine.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_project_version_source import (
    LiveRedmineProjectVersionSource,
    live_project_version_source_from_env,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_read_transport import (
    RedmineRedirectRefused,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    READ_CREDENTIAL_MISSING,
    READ_PROVIDER_UNAVAILABLE,
    READ_TRANSPORT_ERROR,
    READ_UNAUTHORIZED,
    RedmineVersionReadUnavailable,
)
from mozyo_bridge.redmine_context import API_KEY_ENV, BASE_URL_ENV


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class _RecordingOpener:
    """Serves canned pages and records the requests (URL, method, headers) it was given."""

    def __init__(self, pages: list[object]):
        self._pages = pages
        self.requests: list[object] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        page = self._pages[min(len(self.requests) - 1, len(self._pages) - 1)]
        if isinstance(page, Exception):
            raise page
        return _FakeResponse(json.dumps(page).encode("utf-8"))

    @property
    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]


def _version(version_id: int, name: str, status: str = "open") -> dict:
    return {"id": version_id, "name": name, "status": status}


# Explicit non-secret placeholders: Source Tree Hygiene strict-fails on a
# credential-shaped literal in a tracked file, and the `fake-` marker is what the
# scanner classifies as a placeholder rather than a leaked key.
#
# The canary is deliberately DISTINCT from the default key: the redaction test
# overrides the key with it, so asserting the canary is absent from the failure
# reason proves the *caller-supplied* key never reaches the message.
_FAKE_API_KEY = "fake-api-key"
_REDACTION_CANARY_KEY = "fake-api-key-redaction-canary"


def _source(opener, **kwargs) -> LiveRedmineProjectVersionSource:
    params = dict(api_key=_FAKE_API_KEY, base_url="https://redmine.example", opener=opener)
    params.update(kwargs)
    return LiveRedmineProjectVersionSource(**params)


class ProjectVersionReadTest(unittest.TestCase):
    def test_reads_the_project_scoped_versions_endpoint(self) -> None:
        opener = _RecordingOpener([{"versions": [_version(292, "枠")], "total_count": 1}])
        versions = _source(opener).read_project_versions("giken-3800-mozyo-bridge")
        self.assertEqual([v["id"] for v in versions], [292])
        url = opener.urls[0]
        # Project-scoped list against the trusted host; the identifier is a path segment
        # value, never the destination.
        self.assertTrue(
            url.startswith(
                "https://redmine.example/projects/giken-3800-mozyo-bridge/versions.json?"
            ),
            url,
        )
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertEqual(
            opener.requests[0].get_header("X-redmine-api-key"), _FAKE_API_KEY
        )

    def test_identifier_is_percent_encoded_and_cannot_traverse(self) -> None:
        opener = _RecordingOpener([{"versions": [], "total_count": 0}])
        _source(opener).read_project_versions("../../admin")
        path = urllib.parse.urlparse(opener.urls[0]).path
        self.assertEqual(path, "/projects/..%2F..%2Fadmin/versions.json")

    def test_project_with_no_versions_returns_empty_not_an_error(self) -> None:
        opener = _RecordingOpener([{"versions": [], "total_count": 0}])
        self.assertEqual(list(_source(opener).read_project_versions("p")), [])

    def test_unpaginated_response_without_total_count_is_complete(self) -> None:
        # The versions endpoint is not always paginated; a body with no total_count is
        # the complete list, not a partial read.
        opener = _RecordingOpener([{"versions": [_version(1, "a"), _version(2, "b")]}])
        self.assertEqual(len(_source(opener).read_project_versions("p")), 2)
        self.assertEqual(len(opener.urls), 1)

    def test_paginates_until_total_count_is_covered(self) -> None:
        page1 = {"versions": [_version(i, f"v{i}") for i in range(10)], "total_count": 15}
        page2 = {
            "versions": [_version(i, f"v{i}") for i in range(10, 15)],
            "total_count": 15,
        }
        opener = _RecordingOpener([page1, page2])
        versions = _source(opener, page_limit=10).read_project_versions("p")
        self.assertEqual(len(versions), 15)


class FailClosedTest(unittest.TestCase):
    def test_missing_base_url_fails_closed_without_network(self) -> None:
        opener = _RecordingOpener([{"versions": [], "total_count": 0}])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener, base_url=None).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)
        self.assertEqual(opener.urls, [])

    def test_missing_api_key_fails_closed_without_network(self) -> None:
        opener = _RecordingOpener([{"versions": [], "total_count": 0}])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener, api_key=None).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_CREDENTIAL_MISSING)
        self.assertEqual(opener.urls, [])

    def test_blank_identifier_fails_closed(self) -> None:
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(_RecordingOpener([])).read_project_versions("  ")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_http_401_and_403_map_to_unauthorized(self) -> None:
        for code in (401, 403):
            with self.subTest(code=code):
                err = urllib.error.HTTPError("u", code, "no", {}, io.BytesIO(b""))
                with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
                    _source(_RecordingOpener([err])).read_project_versions("p")
                self.assertEqual(ctx.exception.reason, READ_UNAUTHORIZED)

    def test_http_404_maps_to_transport_error(self) -> None:
        # An unknown / invisible project is a read that could not be performed, not a
        # project with no versions.
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b""))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(_RecordingOpener([err])).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_network_error_maps_to_transport_error(self) -> None:
        opener = _RecordingOpener([urllib.error.URLError("down")])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_refused_redirect_maps_to_transport_error(self) -> None:
        opener = _RecordingOpener([RedmineRedirectRefused(302)])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_malformed_body_fails_closed(self) -> None:
        for body in ({"total_count": 0}, [1, 2, 3], {"versions": "nope"}):
            with self.subTest(body=body):
                with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
                    _source(_RecordingOpener([body])).read_project_versions("p")
                self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_negative_total_count_fails_closed(self) -> None:
        # Mirrors the issue source's j#69440 lineage: a negative total must never be
        # trusted as "already covered" (0 >= -1) and short-circuit an empty read.
        opener = _RecordingOpener([{"versions": [], "total_count": -1}])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_empty_page_before_total_covered_refuses_partial_snapshot(self) -> None:
        opener = _RecordingOpener([{"versions": [], "total_count": 3}])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_page_walk_guard_refuses_partial_snapshot(self) -> None:
        never_converges = {
            "versions": [_version(i, f"v{i}") for i in range(5)],
            "total_count": 10_000,
        }
        opener = _RecordingOpener([never_converges])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener, page_limit=5, max_pages=3).read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)
        self.assertEqual(len(opener.urls), 3)

    def test_reasons_never_carry_the_api_key(self) -> None:
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        opener = _RecordingOpener([err])
        source = _source(opener, api_key=_REDACTION_CANARY_KEY)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_versions("p")
        self.assertNotIn(_REDACTION_CANARY_KEY, str(ctx.exception))


class BuilderTest(unittest.TestCase):
    def test_resolves_credentials_from_the_injected_environment(self) -> None:
        opener = _RecordingOpener([{"versions": [_version(292, "枠")], "total_count": 1}])
        source = live_project_version_source_from_env(
            environ={BASE_URL_ENV: "https://redmine.example", API_KEY_ENV: "fake-api-key"},
            home=Path("/nonexistent-home-for-test"),
            opener=opener,
        )
        self.assertEqual([v["id"] for v in source.read_project_versions("p")], [292])

    def test_absent_credentials_fail_closed_at_read_time(self) -> None:
        source = live_project_version_source_from_env(
            environ={},
            home=Path("/nonexistent-home-for-test"),
            opener=_RecordingOpener([]),
        )
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_versions("p")
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
