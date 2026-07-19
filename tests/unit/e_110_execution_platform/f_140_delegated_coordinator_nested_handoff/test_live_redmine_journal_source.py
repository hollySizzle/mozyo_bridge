"""Live Redmine journal poll adapter tests (Redmine #13289).

Pins the network read boundary that lets ``workflow watch --poll`` ingest real Redmine journal
history: a :class:`RedmineJournalSource` that fetches issue-detail JSON over an injected
transport and reuses the tested :class:`MappingRedmineJournalSource` parse. Every test drives a
fake transport / injected credentials — no real network, no real environment, no baked secret.

- the adapter satisfies the ``read_entries`` contract over an injected transport (both the MCP
  top-level-journals and the REST nested-journals shapes);
- the ``since`` cursor keeps only journals strictly newer than it, and keeps a journal with no
  ``created_on`` (fail-open — the intake anchor dedup guarantees correctness on a re-poll);
- credentials resolve fail-closed to a redacted error when the environment is unconfigured
  (the unconfigured cases point ``home`` at an isolated temp dir so the env->home credential
  fallback resolves against an empty root, never a developer's real ``redmine-credentials.yaml``),
  and the API key / URL never appear in any error string;
- ``from_environment`` resolves the key from the injected environ and sends it only via the
  transport arguments, never the query.
"""

from __future__ import annotations

import http.server
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
    LiveRedmineJournalError,
    LiveRedmineJournalSource,
    _RefuseRedirectHandler,
    urllib_issue_detail_fetch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    markers_from_source,
)

# Explicit non-secret placeholders. Source Tree Hygiene strict-fails on a
# credential-shaped literal in a tracked file, so these carry a `fake-` marker
# that the scanner classifies as a placeholder rather than a leaked key. The
# redaction canary stays a distinct value: the assertions below prove the key
# never reaches an error message and is never forwarded across a redirect.
_FAKE_API_KEY = "fake-api-key"
_REDACTION_CANARY_KEY = "fake-api-key-leak-canary"


def _handoff_marker(issue, journal, kind, to="codex"):
    return f"[mozyo:handoff:source=redmine:issue={issue}:journal={journal}:kind={kind}:to={to}]"


class _RecordingTransport:
    """A fake transport that records its call kwargs and returns a canned payload."""

    def __init__(self, payload):
        self._payload = payload
        self.calls: list[dict] = []

    def __call__(self, *, base_url, api_key, issue_id, since):
        self.calls.append(
            {"base_url": base_url, "api_key": api_key, "issue_id": issue_id, "since": since}
        )
        return self._payload


class ReadEntriesContractTest(unittest.TestCase):
    def _mcp_payload(self):
        return {
            "issue": {"id": "13289"},
            "journals": [
                {"id": "72671", "notes": "## Start\nno marker", "created_on": "2026-07-05T07:55:57Z"},
                {
                    "id": "72700",
                    "notes": _handoff_marker("13289", "72700", "review_request"),
                    "created_on": "2026-07-05T09:00:00Z",
                },
                {"id": "72710", "notes": "", "created_on": "2026-07-05T09:05:00Z"},
            ],
        }

    def test_read_entries_reuses_mapping_parse(self):
        transport = _RecordingTransport(self._mcp_payload())
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example", api_key=_FAKE_API_KEY, transport=transport
        )
        entries = source.read_entries("13289")
        # 72671 (prose) + 72700 (marker); 72710 empty-note dropped by the reused parse.
        self.assertEqual([e.journal_id for e in entries], ["72671", "72700"])
        self.assertTrue(all(e.issue_id == "13289" for e in entries))

    def test_markers_from_source_extracts_structured_gate(self):
        transport = _RecordingTransport(self._mcp_payload())
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example", api_key=_FAKE_API_KEY, transport=transport
        )
        markers = markers_from_source(source, "13289")
        self.assertEqual([(m.journal, m.gate) for m in markers], [("72700", "review_request")])

    def test_nested_rest_shape_is_read(self):
        payload = {
            "issue": {
                "id": "13289",
                "journals": [
                    {"id": "72700", "notes": _handoff_marker("13289", "72700", "review_request")},
                ],
            }
        }
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example", api_key=_FAKE_API_KEY, transport=_RecordingTransport(payload)
        )
        self.assertEqual([e.journal_id for e in source.read_entries("13289")], ["72700"])

    def test_transport_receives_trusted_base_and_key(self):
        transport = _RecordingTransport(self._mcp_payload())
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example", api_key=_FAKE_API_KEY, transport=transport, since="x"
        )
        source.read_entries("13289")
        self.assertEqual(transport.calls[0]["base_url"], "https://redmine.example")
        self.assertEqual(transport.calls[0]["api_key"], _FAKE_API_KEY)
        self.assertEqual(transport.calls[0]["issue_id"], "13289")
        self.assertEqual(transport.calls[0]["since"], "x")

    def test_missing_issue_id_fails_closed(self):
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example", api_key=_FAKE_API_KEY, transport=_RecordingTransport({})
        )
        with self.assertRaises(LiveRedmineJournalError):
            source.read_entries("")


class SinceCursorTest(unittest.TestCase):
    def _payload(self):
        return {
            "issue": {"id": "13289"},
            "journals": [
                {"id": "1", "notes": _handoff_marker("13289", "1", "review_request"),
                 "created_on": "2026-07-05T07:00:00Z"},
                {"id": "2", "notes": _handoff_marker("13289", "2", "review_request"),
                 "created_on": "2026-07-05T09:00:00Z"},
                {"id": "3", "notes": _handoff_marker("13289", "3", "review_request")},  # no created_on
            ],
        }

    def test_cursor_keeps_only_strictly_newer_and_undated(self):
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example",
            api_key=_FAKE_API_KEY,
            transport=_RecordingTransport(self._payload()),
            since="2026-07-05T08:00:00Z",
        )
        # journal 1 is at/older than the cursor -> dropped; 2 is newer -> kept; 3 undated -> kept.
        self.assertEqual([e.journal_id for e in source.read_entries("13289")], ["2", "3"])

    def test_no_cursor_reads_all(self):
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example",
            api_key=_FAKE_API_KEY,
            transport=_RecordingTransport(self._payload()),
        )
        self.assertEqual([e.journal_id for e in source.read_entries("13289")], ["1", "2", "3"])

    def test_cursor_equal_timestamp_is_excluded(self):
        source = LiveRedmineJournalSource(
            base_url="https://redmine.example",
            api_key=_FAKE_API_KEY,
            transport=_RecordingTransport(self._payload()),
            since="2026-07-05T09:00:00Z",
        )
        # journal 2 is exactly the cursor -> excluded (strictly-after); only undated 3 remains.
        self.assertEqual([e.journal_id for e in source.read_entries("13289")], ["3"])


class FromEnvironmentTest(unittest.TestCase):
    def _isolated_home(self) -> Path:
        """An empty temp home so the env->home credential fallback resolves against no file.

        ``from_environment`` resolves credentials env-first, then the home-scoped
        ``redmine-credentials.yaml``. The unconfigured cases inject ``environ`` but must also
        pin ``home`` at an isolated, empty root — otherwise a developer who has configured a
        real global credential would see the home fallback satisfy the resolution and the
        "unconfigured" assertion would silently stop biting (Redmine #14061). This keeps the
        env->home production semantics unchanged and only makes the test hermetic.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_resolves_key_from_injected_env(self):
        transport = _RecordingTransport({"issue": {"id": "13289"}, "journals": []})
        source = LiveRedmineJournalSource.from_environment(
            transport=transport,
            environ={
                "MOZYO_REDMINE_API_KEY": "env-key",
                "MOZYO_REDMINE_URL": "https://redmine.example/some/path",
            },
        )
        # The base URL is normalized to scheme://host; the key is carried, not echoed.
        self.assertEqual(source.base_url, "https://redmine.example")
        self.assertEqual(source.api_key, "env-key")
        source.read_entries("13289")
        self.assertEqual(transport.calls[0]["api_key"], "env-key")

    def test_unconfigured_env_fails_closed_without_leaking(self):
        with self.assertRaises(LiveRedmineJournalError) as ctx:
            LiveRedmineJournalSource.from_environment(environ={}, home=self._isolated_home())
        msg = str(ctx.exception)
        self.assertIn("MOZYO_REDMINE_API_KEY", msg)
        self.assertIn("unconfigured", msg)

    def test_missing_key_present_url_is_unconfigured(self):
        with self.assertRaises(LiveRedmineJournalError):
            LiveRedmineJournalSource.from_environment(
                environ={"MOZYO_REDMINE_URL": "https://redmine.example"},
                home=self._isolated_home(),
            )


class ErrorRedactionTest(unittest.TestCase):
    def test_transport_error_message_hides_key_and_url(self):
        def _boom(*, base_url, api_key, issue_id, since):
            raise LiveRedmineJournalError(f"redmine issue {issue_id} journal fetch failed (URLError)")

        source = LiveRedmineJournalSource(
            base_url="https://redmine.secret-host.example", api_key=_REDACTION_CANARY_KEY, transport=_boom
        )
        with self.assertRaises(LiveRedmineJournalError) as ctx:
            source.read_entries("13289")
        msg = str(ctx.exception)
        self.assertNotIn(_REDACTION_CANARY_KEY, msg)
        self.assertNotIn("secret-host", msg)


class DefaultTransportSignatureTest(unittest.TestCase):
    def test_default_transport_is_the_urllib_fetch(self):
        # The dataclass default is the real urllib transport (no accidental fake baked in).
        source = LiveRedmineJournalSource(base_url="https://x", api_key=_FAKE_API_KEY)
        self.assertIs(source.transport, urllib_issue_detail_fetch)


class RedirectCredentialBoundaryTest(unittest.TestCase):
    """The API key must never follow a 30x off the trusted base URL (review #13289 j#72712).

    stdlib urllib copies non-content request headers (incl. X-Redmine-API-Key) onto a redirect
    target Request; the default transport must refuse redirects so the key stays on the base.
    """

    def test_refuse_redirect_handler_fails_closed(self):
        # Socket-free pin: the redirect is refused before the next Request is even built.
        handler = _RefuseRedirectHandler()
        with self.assertRaises(LiveRedmineJournalError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/")

    def test_key_never_reaches_a_redirect_target(self):
        # Loopback proof (127.0.0.1 only, no external network): the base host 302s to a second
        # local server; that target must receive no request at all, so the key cannot leak.
        received: list[dict] = []

        class _Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                received.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):  # silence test server logging
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), _Target)
        self.addCleanup(target.server_close)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        self.addCleanup(target.shutdown)
        target_port = target.server_address[1]

        class _Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/issues/1.json")
                self.end_headers()

            def log_message(self, *a):
                pass

        redirector = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
        self.addCleanup(redirector.server_close)
        threading.Thread(target=redirector.serve_forever, daemon=True).start()
        self.addCleanup(redirector.shutdown)
        base_port = redirector.server_address[1]

        with self.assertRaises(LiveRedmineJournalError):
            urllib_issue_detail_fetch(
                base_url=f"http://127.0.0.1:{base_port}",
                api_key=_REDACTION_CANARY_KEY,
                issue_id="1",
                since=None,
            )
        # The redirect target was never contacted -> the key was never forwarded.
        self.assertEqual(received, [])


if __name__ == "__main__":
    unittest.main()
