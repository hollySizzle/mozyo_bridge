"""Redmine gate-context read model tests (Redmine #11686 / #11687 / #11688).

Covers: per-workspace project / base-URL resolution from
workspace-defaults, the degradation states (unconfigured / unavailable /
available), TTL caching and the per-call fetch budget, the additive
``redmine`` field on the units endpoint, API-key non-leakage into
payloads, and the DOM-safety pins extended to the new UI layer. No real
Redmine is contacted — urlopen is always patched.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.redmine_context import (
    STATE_AVAILABLE,
    STATE_UNAVAILABLE,
    STATE_UNCONFIGURED,
    RedmineContextCache,
    attach_redmine_context,
    read_redmine_project,
)

API_KEY = "test-key-not-a-real-credential"
TRUSTED = "https://redmine.example.test"


def write_workspace_defaults(repo: Path, *, identifier: str, url: str) -> None:
    (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
        "schema_version: 1\n"
        "redmine:\n"
        "  default_project:\n"
        f"    identifier: {identifier}\n"
        "    name: Example\n"
        f"    url: {url}\n"
        "  verification:\n"
        "    verified: false\n"
        '    verification_date: ""\n'
        '    verified_by: ""\n'
        "outputs:\n"
        "  - kind: redmine_markdown\n"
        "    target: .mozyo-bridge/redmine-defaults.md\n",
        encoding="utf-8",
    )


def issues_response(payload: dict):
    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    return _Response(json.dumps(payload).encode("utf-8"))


class ReadRedmineProjectTest(unittest.TestCase):
    def test_reads_identifier_and_host_derived_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            write_workspace_defaults(
                repo,
                identifier="giken-demo",
                url="https://redmine.example.test/projects/giken-demo",
            )
            identifier, base = read_redmine_project(repo)
        self.assertEqual("giken-demo", identifier)
        # Host-derived, project path stripped (runtime-config pattern).
        self.assertEqual("https://redmine.example.test", base)

    def test_missing_or_invalid_defaults_degrade_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.assertEqual((None, None), read_redmine_project(repo))
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
                "not: [valid", encoding="utf-8"
            )
            self.assertEqual((None, None), read_redmine_project(repo))


class RedmineContextCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        write_workspace_defaults(
            self.repo,
            identifier="giken-demo",
            url="https://redmine.example.test/projects/giken-demo",
        )

    def test_no_api_key_is_unconfigured_without_fetch(self) -> None:
        cache = RedmineContextCache(api_key=None, base_url=TRUSTED)
        with patch("urllib.request.urlopen") as opener:
            payload = cache.context_for_repo(str(self.repo), budget=[5])
        self.assertEqual(STATE_UNCONFIGURED, payload["state"])
        opener.assert_not_called()

    def test_unmapped_workspace_is_unconfigured(self) -> None:
        cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("urllib.request.urlopen") as opener:
                payload = cache.context_for_repo(tmp, budget=[5])
        self.assertEqual(STATE_UNCONFIGURED, payload["state"])
        opener.assert_not_called()

    def test_available_context_carries_latest_issue_and_no_key(self) -> None:
        cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)
        captured: list = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            return issues_response(
                {
                    "total_count": 7,
                    "issues": [
                        {
                            "id": 11999,
                                                        "status": {"name": "着手中"},
                            "updated_on": "2026-06-12T00:00:00Z",
                        }
                    ],
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            payload = cache.context_for_repo(str(self.repo), budget=[5])
        self.assertEqual(STATE_AVAILABLE, payload["state"])
        self.assertEqual(7, payload["open_total"])
        self.assertEqual(11999, payload["latest_issue"]["id"])
        self.assertEqual("着手中", payload["latest_issue"]["status"])
        # The API key rides only in the request header; the payload (which
        # reaches the UI) must never carry it.
        self.assertNotIn(API_KEY, json.dumps(payload))
        self.assertEqual(
            API_KEY, captured[0].get_header("X-redmine-api-key")
        )

    def test_fetch_error_is_unavailable_and_cached(self) -> None:
        cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)
        calls: list[int] = []

        def failing_urlopen(request, timeout):
            calls.append(1)
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing_urlopen):
            first = cache.context_for_repo(str(self.repo), budget=[5])
            second = cache.context_for_repo(str(self.repo), budget=[5])
        self.assertEqual(STATE_UNAVAILABLE, first["state"])
        self.assertEqual(STATE_UNAVAILABLE, second["state"])
        # Failure is TTL-cached: one fetch, not one per poll.
        self.assertEqual(1, len(calls))

    def test_depleted_budget_yields_unavailable_without_fetch(self) -> None:
        cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)
        with patch("urllib.request.urlopen") as opener:
            payload = cache.context_for_repo(str(self.repo), budget=[0])
        self.assertEqual(STATE_UNAVAILABLE, payload["state"])
        opener.assert_not_called()

    def test_hostile_workspace_url_never_draws_a_request(self) -> None:
        # Review #56232 (High): a repo-local workspace-defaults file must
        # never select where the API key is sent. A workspace declaring an
        # attacker host gets `unconfigured` with ZERO network activity.
        with tempfile.TemporaryDirectory() as tmp:
            hostile = Path(tmp) / "hostile-checkout"
            hostile.mkdir()
            write_workspace_defaults(
                hostile,
                identifier="demo",
                url="https://attacker.example/projects/demo",
            )
            cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)
            with patch("urllib.request.urlopen") as opener:
                payload = cache.context_for_repo(str(hostile), budget=[5])
        self.assertEqual(STATE_UNCONFIGURED, payload["state"])
        opener.assert_not_called()

    def test_requests_only_ever_target_the_trusted_base(self) -> None:
        # Even on the happy path, the destination is the daemon-env base
        # by construction — the workspace string is only compared, never
        # used to build the request.
        cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)
        captured: list = []

        def fake_urlopen(request, timeout):
            captured.append(request.full_url)
            return issues_response({"total_count": 0, "issues": []})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cache.context_for_repo(str(self.repo), budget=[5])
        self.assertEqual(1, len(captured))
        self.assertTrue(captured[0].startswith(TRUSTED + "/issues.json?"))

    def test_no_trusted_base_url_is_unconfigured_without_fetch(self) -> None:
        cache = RedmineContextCache(api_key=API_KEY, base_url=None)
        with patch("urllib.request.urlopen") as opener:
            payload = cache.context_for_repo(str(self.repo), budget=[5])
        self.assertEqual(STATE_UNCONFIGURED, payload["state"])
        opener.assert_not_called()

    def test_subject_is_never_on_the_payload(self) -> None:
        # Review #56232 (Medium): issue subjects can carry personal or
        # confidential summaries and the v1 UI does not show them.
        cache = RedmineContextCache(api_key=API_KEY, base_url=TRUSTED)

        def fake_urlopen(request, timeout):
            return issues_response(
                {
                    "total_count": 1,
                    "issues": [
                        {
                            "id": 1,
                            "subject": "CONFIDENTIAL-SUMMARY",
                            "status": {"name": "着手中"},
                            "updated_on": "2026-06-12T00:00:00Z",
                        }
                    ],
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            payload = cache.context_for_repo(str(self.repo), budget=[5])
        self.assertEqual(STATE_AVAILABLE, payload["state"])
        self.assertNotIn("subject", payload["latest_issue"])
        self.assertNotIn("CONFIDENTIAL-SUMMARY", json.dumps(payload))

    def test_attach_enriches_panes_additively(self) -> None:
        cache = RedmineContextCache(api_key=None, base_url=TRUSTED)
        payload = {
            "stale": False,
            "panes": [
                {"pane_id": "%1", "repo_root": str(self.repo), "activity": {}},
            ],
        }
        enriched = attach_redmine_context(payload, cache)
        pane = enriched["panes"][0]
        # Additive: existing keys untouched, redmine added.
        self.assertEqual("%1", pane["pane_id"])
        self.assertIn("activity", pane)
        self.assertEqual(STATE_UNCONFIGURED, pane["redmine"]["state"])


class CockpitUnitsRedmineFieldTest(unittest.TestCase):
    def test_units_endpoint_includes_redmine_field(self) -> None:
        import threading
        import urllib.request as urlreq

        from mozyo_bridge.application.otel_receiver import build_server

        import os

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("MOZYO_REDMINE_API_KEY", "MOZYO_REDMINE_URL")
            }
            clean_env["MOZYO_BRIDGE_HOME"] = str(home)
            with patch.dict("os.environ", clean_env, clear=True):
                server = build_server(host="127.0.0.1", port=0, home=home)
                port = server.server_address[1]
                threading.Thread(
                    target=server.serve_forever, daemon=True
                ).start()
                try:
                    panes = [
                        {
                            "id": "%1",
                            "location": "mozyo-demo:1.0",
                            "command": "claude",
                            "cwd": "",
                            "window_name": "claude",
                            "pane_active": "1",
                        }
                    ]
                    with patch(
                        "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
                        return_value=panes,
                    ):
                        with urlreq.urlopen(
                            f"http://127.0.0.1:{port}/api/units", timeout=5
                        ) as response:
                            payload = json.loads(response.read())
                finally:
                    server.shutdown()
                    server.server_close()
        record = payload["panes"][0]
        self.assertIn("redmine", record)
        # No key in the daemon env for this test: unconfigured, and the
        # OTel / tmux layers are untouched.
        self.assertEqual(STATE_UNCONFIGURED, record["redmine"]["state"])
        self.assertIn("activity", record)
        self.assertEqual("%1", record["pane_id"])


if __name__ == "__main__":
    unittest.main()
