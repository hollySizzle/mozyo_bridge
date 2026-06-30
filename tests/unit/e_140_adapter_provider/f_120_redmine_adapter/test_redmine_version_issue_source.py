"""Unit tests for the read-only live Version issue source (Redmine #12923).

Covers the credential-gated, paginating ``GET /issues.json?fixed_version_id=*``
adapter behind the #12651 ``RedmineVersionIssueSource`` port: success and
pagination, the genuinely-empty Version (the only legitimate empty result), and
every fail-closed path (no base URL, no API key, HTTP 401/403, transport error,
malformed body, and the page-walk guard). A network gap must always raise an
explicit reason and never masquerade as an empty Version. Also pins that the
adapter plugs into the pure enumeration so the leaf rule is reused, not
re-implemented, and that the builder resolves credentials from an injected
environment without touching the network.
"""
from __future__ import annotations

import sys
import io
import urllib.error
import urllib.parse
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.redmine_version_enumeration import (
    RedmineVersionIssueSource,
    enumerate_from_source,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    READ_CREDENTIAL_MISSING,
    READ_PROVIDER_UNAVAILABLE,
    READ_TRANSPORT_ERROR,
    READ_UNAUTHORIZED,
    LiveRedmineVersionIssueSource,
    RedmineVersionReadUnavailable,
    live_version_issue_source_from_env,
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


def _json_response(payload: object) -> _FakeResponse:
    import json

    return _FakeResponse(json.dumps(payload).encode("utf-8"))


class _RecordingOpener:
    """A fake ``opener`` that serves canned pages and records request offsets."""

    def __init__(self, pages: list[object]):
        self._pages = pages
        self.offsets: list[str] = []
        self.urls: list[str] = []
        self.responses: list[_FakeResponse] = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.offsets.append(query.get("offset", ["?"])[0])
        page = self._pages[min(len(self.offsets) - 1, len(self._pages) - 1)]
        if isinstance(page, Exception):
            raise page
        resp = _json_response(page)
        self.responses.append(resp)
        return resp


def _issue(issue_id: int, *, closed: bool = False, parent: int | None = None) -> dict:
    entry: dict = {
        "id": issue_id,
        "tracker": {"name": "Task"},
        "status": {"name": "x", "is_closed": closed},
    }
    if parent is not None:
        entry["parent"] = {"id": parent}
    return entry


def _source(opener, **kwargs) -> LiveRedmineVersionIssueSource:
    params = dict(api_key="k", base_url="https://redmine.example", opener=opener)
    params.update(kwargs)
    return LiveRedmineVersionIssueSource(**params)


class LiveReadSuccessTest(unittest.TestCase):
    def test_single_page_returns_raw_issue_entries(self) -> None:
        opener = _RecordingOpener(
            [{"issues": [_issue(901), _issue(902)], "total_count": 2}]
        )
        source = _source(opener)
        issues = source.read_version_issues("248")
        self.assertEqual([i["id"] for i in issues], [901, 902])
        # Read-only single page: trusted host + version id as a query value.
        self.assertEqual(len(opener.urls), 1)
        self.assertIn("fixed_version_id=248", opener.urls[0])
        self.assertIn("status_id=%2A", opener.urls[0])  # status_id=* (any)
        self.assertTrue(opener.urls[0].startswith("https://redmine.example/issues.json?"))
        self.assertTrue(opener.responses[0].closed)

    def test_paginates_until_total_count_is_covered(self) -> None:
        page1 = {"issues": [_issue(i) for i in range(100)], "total_count": 150}
        page2 = {"issues": [_issue(i) for i in range(100, 150)], "total_count": 150}
        opener = _RecordingOpener([page1, page2])
        source = _source(opener, page_limit=100)
        issues = source.read_version_issues("248")
        self.assertEqual(len(issues), 150)
        self.assertEqual(opener.offsets, ["0", "100"])

    def test_satisfies_the_port_protocol(self) -> None:
        source = _source(_RecordingOpener([{"issues": [], "total_count": 0}]))
        self.assertIsInstance(source, RedmineVersionIssueSource)

    def test_feeds_the_pure_enumeration_without_reimplementing_leaf_rule(self) -> None:
        # The live adapter only supplies raw entries; the #12651 pure leaf rule
        # (open child in-set => parent is a non-leaf) is reused via enumerate.
        opener = _RecordingOpener(
            [
                {
                    "issues": [
                        _issue(900),  # parent with an open child -> non-leaf
                        _issue(901, parent=900),  # open leaf
                        _issue(902, parent=900, closed=True),  # closed -> excluded
                    ],
                    "total_count": 3,
                }
            ]
        )
        enumeration = enumerate_from_source(_source(opener), "248")
        self.assertEqual([i.issue_id for i in enumeration.open_leaf_issues], ["901"])
        self.assertEqual([i.issue_id for i in enumeration.open_nonleaf_issues], ["900"])
        self.assertEqual(enumeration.total_issues, 3)


class GenuinelyEmptyVersionTest(unittest.TestCase):
    def test_empty_version_returns_empty_list_not_an_error(self) -> None:
        # HTTP 200 with an empty issue set is the ONLY legitimate empty result.
        opener = _RecordingOpener([{"issues": [], "total_count": 0}])
        self.assertEqual(list(_source(opener).read_version_issues("281")), [])


class FailClosedTest(unittest.TestCase):
    def _reason(self, exc: RedmineVersionReadUnavailable) -> str:
        return exc.reason

    def test_missing_base_url_fails_closed_without_network(self) -> None:
        opener = _RecordingOpener([{"issues": [], "total_count": 0}])
        source = _source(opener, base_url=None)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)
        self.assertEqual(opener.urls, [])  # never touched the network

    def test_missing_api_key_fails_closed_without_network(self) -> None:
        opener = _RecordingOpener([{"issues": [], "total_count": 0}])
        source = _source(opener, api_key=None)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_CREDENTIAL_MISSING)
        self.assertEqual(opener.urls, [])

    def test_non_http_base_url_is_refused(self) -> None:
        source = _source(_RecordingOpener([]), base_url="ftp://redmine.example")
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)

    def test_blank_version_id_fails_closed(self) -> None:
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(_RecordingOpener([])).read_version_issues("   ")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_http_401_maps_to_unauthorized(self) -> None:
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        source = _source(_RecordingOpener([err]))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_UNAUTHORIZED)

    def test_http_403_maps_to_unauthorized(self) -> None:
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(_RecordingOpener([err])).read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_UNAUTHORIZED)

    def test_http_500_maps_to_transport_error(self) -> None:
        err = urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b""))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(_RecordingOpener([err])).read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_network_error_maps_to_transport_error(self) -> None:
        source = _source(_RecordingOpener([urllib.error.URLError("down")]))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_malformed_body_without_issues_list_fails_closed(self) -> None:
        source = _source(_RecordingOpener([{"total_count": 0}]))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_missing_total_count_fails_closed(self) -> None:
        source = _source(_RecordingOpener([{"issues": [_issue(1)]}]))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_non_object_body_fails_closed(self) -> None:
        source = _source(_RecordingOpener([[1, 2, 3]]))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_empty_page_before_total_covered_fails_closed(self) -> None:
        # Regression (j#69422): a first page with no issues but total_count > 0
        # must NOT be returned as a (partial/empty) snapshot — it fails closed,
        # so a gap is never rendered as an empty Version.
        opener = _RecordingOpener([{"issues": [], "total_count": 2}])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener).read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_empty_second_page_before_total_covered_fails_closed(self) -> None:
        # Regression (j#69422): a full first page that still does not cover
        # total_count, followed by an empty page, must fail closed rather than
        # return the first page as a complete snapshot.
        page1 = {"issues": [_issue(i) for i in range(10)], "total_count": 25}
        empty = {"issues": [], "total_count": 25}
        opener = _RecordingOpener([page1, empty])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener, page_limit=10).read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)
        self.assertEqual(opener.offsets, ["0", "10"])  # walked, then refused

    def test_page_with_only_non_mapping_rows_before_total_fails_closed(self) -> None:
        # A page whose rows are all unusable (non-mapping) makes no progress
        # toward total_count and must fail closed, not loop or truncate.
        opener = _RecordingOpener([{"issues": [1, 2, 3], "total_count": 5}])
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            _source(opener).read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)

    def test_short_final_page_that_covers_total_succeeds(self) -> None:
        # The legitimate short-page case: a non-full final page that brings the
        # collected count up to total_count is a complete snapshot, not a gap.
        page1 = {"issues": [_issue(i) for i in range(10)], "total_count": 15}
        page2 = {"issues": [_issue(i) for i in range(10, 15)], "total_count": 15}
        opener = _RecordingOpener([page1, page2])
        issues = _source(opener, page_limit=10).read_version_issues("248")
        self.assertEqual(len(issues), 15)

    def test_page_walk_guard_refuses_partial_snapshot(self) -> None:
        # total_count always claims more than a page; never converges -> guard.
        full_page = {"issues": [_issue(i) for i in range(10)], "total_count": 10_000}
        opener = _RecordingOpener([full_page])
        source = _source(opener, page_limit=10, max_pages=3)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)
        self.assertEqual(len(opener.urls), 3)  # bounded by max_pages


class FailClosedReasonRedactionTest(unittest.TestCase):
    def test_reasons_never_carry_the_api_key(self) -> None:
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        source = _source(_RecordingOpener([err]), api_key="super-secret-key")
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertNotIn("super-secret-key", str(ctx.exception))


class BuilderTest(unittest.TestCase):
    def test_resolves_credentials_from_injected_environment(self) -> None:
        opener = _RecordingOpener([{"issues": [_issue(5)], "total_count": 1}])
        source = live_version_issue_source_from_env(
            environ={
                BASE_URL_ENV: "https://redmine.example",
                API_KEY_ENV: "k",
            },
            home=Path("/nonexistent-home-for-test"),
            opener=opener,
        )
        self.assertEqual([i["id"] for i in source.read_version_issues("248")], [5])

    def test_absent_credentials_surface_as_fail_closed_at_read_time(self) -> None:
        # The builder never raises; an unconfigured environment fails closed only
        # when the read is finally attempted (more informative than a silent None).
        source = live_version_issue_source_from_env(
            environ={},
            home=Path("/nonexistent-home-for-test"),
            opener=_RecordingOpener([]),
        )
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("248")
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
