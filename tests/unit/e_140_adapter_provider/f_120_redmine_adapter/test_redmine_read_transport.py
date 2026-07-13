"""Security regression: the f_120 read transport never follows a redirect (Redmine #13687).

The credential boundary this pins (j#76650 Finding 1): the stdlib
``HTTPRedirectHandler.redirect_request`` copies non-content request headers — including
``X-Redmine-API-Key`` — onto the redirect target, so following a 30x would carry the key
to the ``Location`` host. These tests drive the *real* stdlib redirect machinery
(``http_error_30x`` -> ``redirect_request`` -> ``parent.open``) with a recording parent, so
they prove the two things that matter and not merely that an exception is raised:

- the redirect is refused for a **cross-host and a same-host** ``Location`` alike;
- ``parent.open`` is never reached, i.e. the follow-up request carrying the key is never
  built or sent.

Hermetic: no socket is opened. The handler is exercised directly against the stdlib call
path, never a real Redmine.
"""

from __future__ import annotations

import email.message
import io
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure import (
    redmine_version_issue_source as module,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_read_transport import (
    RedmineRedirectRefused,
    _RefuseRedirectHandler,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    READ_TRANSPORT_ERROR,
    LiveRedmineVersionIssueSource,
    RedmineVersionReadUnavailable,
)

_API_KEY = "super-secret-key"


class _RecordingParent:
    """Stands in for the opener chain: records any follow-up request it is asked to open."""

    def __init__(self) -> None:
        self.opened: list[urllib.request.Request] = []

    def open(self, req, data=None, timeout=None):  # pragma: no cover - must never run
        self.opened.append(req)
        return None


def _headers(location: str) -> email.message.Message:
    message = email.message.Message()
    message["Location"] = location
    return message


def _request() -> urllib.request.Request:
    return urllib.request.Request(
        "https://redmine.example/issues.json?fixed_version_id=292",
        headers={"X-Redmine-API-Key": _API_KEY},
    )


class RefuseRedirectTest(unittest.TestCase):
    def _drive_redirect(self, code: int, location: str) -> tuple[Exception, _RecordingParent]:
        handler = _RefuseRedirectHandler()
        parent = _RecordingParent()
        handler.parent = parent  # what urllib's opener chain does
        http_error = getattr(handler, f"http_error_{code}")
        with self.assertRaises(RedmineRedirectRefused) as ctx:
            http_error(_request(), io.BytesIO(b""), code, "Redirect", _headers(location))
        return ctx.exception, parent

    def test_cross_host_redirect_is_refused_and_never_followed(self) -> None:
        exc, parent = self._drive_redirect(302, "https://evil.example/issues.json")
        self.assertEqual(exc.code, 302)
        # The follow-up request — the one that would carry the API key — is never built.
        self.assertEqual(parent.opened, [])

    def test_same_host_redirect_is_also_refused(self) -> None:
        # Fail closed on *any* 30x, not just off-origin ones: a read-only GET has no
        # legitimate reason to redirect, and same-origin-only would be a weaker posture.
        _, parent = self._drive_redirect(301, "https://redmine.example/issues.json")
        self.assertEqual(parent.opened, [])

    def test_every_redirect_status_is_refused(self) -> None:
        for code in (301, 302, 303, 307):
            with self.subTest(code=code):
                _, parent = self._drive_redirect(code, "https://evil.example/x.json")
                self.assertEqual(parent.opened, [])

    def test_refusal_never_carries_the_api_key_or_the_location(self) -> None:
        exc, _ = self._drive_redirect(302, "https://evil.example/steal")
        self.assertNotIn(_API_KEY, str(exc))
        self.assertNotIn("evil.example", str(exc))

    def test_refusal_is_a_urlerror_so_callers_map_it_to_transport_error(self) -> None:
        # The read sources catch (URLError, OSError, ValueError) -> transport_error; the
        # refusal rides that branch, so a refused redirect is an unreadable Version and
        # never an empty one — without a new branch in every caller.
        self.assertTrue(issubclass(RedmineRedirectRefused, urllib.error.URLError))


class VersionIssueSourceRedirectTest(unittest.TestCase):
    def test_refused_redirect_fails_closed_as_transport_error(self) -> None:
        def refusing_opener(request, timeout):
            raise RedmineRedirectRefused(302)

        source = LiveRedmineVersionIssueSource(
            api_key=_API_KEY,
            base_url="https://redmine.example",
            opener=refusing_opener,
        )
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_version_issues("292")
        self.assertEqual(ctx.exception.reason, READ_TRANSPORT_ERROR)
        self.assertNotIn(_API_KEY, str(ctx.exception))

    def test_default_read_routes_through_the_redirect_refusing_transport(self) -> None:
        # The regression that motivated this module: the default opener used to be a bare
        # urlopen, which follows a 30x and re-sends the key. Pin that an un-injected
        # source reads through no_redirect_read instead — verified by intercepting it, so
        # the test fails if the default is ever swapped back to a following opener.
        calls: list[urllib.request.Request] = []

        def fake_no_redirect_read(request, timeout):
            calls.append(request)
            raise urllib.error.URLError("intercepted before any socket")

        source = LiveRedmineVersionIssueSource(
            api_key=_API_KEY, base_url="https://redmine.example"
        )
        with mock.patch.object(module, "no_redirect_read", fake_no_redirect_read):
            with self.assertRaises(RedmineVersionReadUnavailable):
                source.read_version_issues("292")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].full_url.startswith("https://redmine.example/issues.json"))


if __name__ == "__main__":
    unittest.main()
