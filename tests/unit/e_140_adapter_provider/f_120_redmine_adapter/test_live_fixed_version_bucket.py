"""Unit tests for the live project-scoped bucket read (Redmine #13687 Increment 1).

This is the layer that turns two read-only Redmine reads into the input the pure #12919
provider already consumes. What it must guarantee (Design Answer j#76650):

- the read is **scoped to the project the repo declares**, and the declared host must
  match the trusted credential host — checked *before any request*, so a hostile checkout
  can never draw the API key to its own host;
- the Version must be one the project can actually see (a cross-project id is a block,
  not a silent read) and must be **confirmed ``open``** — closed / locked / unknown status
  all block, closing the fail-open gap a bare live read would have opened;
- every refusal is a **blocked read** with an explicit reason, never a zero-candidate
  plan: "could not look" must not read as "nothing to do";
- the pure provider is reused unchanged (leaf / umbrella rules are not re-implemented).

Hermetic: the opener, environment and home are all injected; no test touches a real
Redmine and none performs a write.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.live_fixed_version_bucket import (
    LIVE_PROJECT_HOST_MISMATCH,
    LIVE_PROJECT_UNRESOLVED,
    LIVE_VERSION_AMBIGUOUS,
    LIVE_VERSION_NOT_FOUND,
    LIVE_VERSION_NOT_OPEN,
    read_live_fixed_version_bucket,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_version_issue_source import (
    READ_CREDENTIAL_MISSING,
    READ_PROVIDER_UNAVAILABLE,
    READ_TRANSPORT_ERROR,
    RedmineVersionReadUnavailable,
)
from mozyo_bridge.redmine_context import API_KEY_ENV, BASE_URL_ENV

_HOST = "https://redmine.example"
_PROJECT = "giken-3800-mozyo-bridge"
_HOME = Path("/nonexistent-home-for-test")
_ENV = {BASE_URL_ENV: _HOST, API_KEY_ENV: "k"}


class _FakeResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class _RouteOpener:
    """Routes a request to the canned versions / issues payload and records every call."""

    def __init__(self, *, versions: object = None, issues: object = None):
        self._versions = versions
        self._issues = issues
        self.requests: list[object] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        path = urllib.parse.urlparse(request.full_url).path
        payload = self._versions if path.endswith("/versions.json") else self._issues
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload)

    @property
    def paths(self) -> list[str]:
        return [urllib.parse.urlparse(r.full_url).path for r in self.requests]

    def query_of(self, index: int) -> dict:
        raw = urllib.parse.urlparse(self.requests[index].full_url).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}


def _versions_payload(*entries: dict) -> dict:
    return {"versions": list(entries), "total_count": len(entries)}


def _issues_payload(*entries: dict) -> dict:
    return {"issues": list(entries), "total_count": len(entries)}


def _issue(issue_id: int, *, parent: int | None = None, closed: bool = False) -> dict:
    entry: dict = {
        "id": issue_id,
        "tracker": {"name": "Task"},
        "status": {"name": "New", "is_closed": closed},
        "fixed_version": {"id": 292, "name": "枠", "status": "open"},
    }
    if parent is not None:
        entry["parent"] = {"id": parent}
    return entry


class _RepoFixture:
    """A temp repo whose project defaults declare a Redmine project identifier + host."""

    def __init__(self, stack: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        stack.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".mozyo-bridge").mkdir(parents=True)

    def declare(self, *, identifier: str | None = _PROJECT, url: str | None = _HOST) -> Path:
        lines = ["redmine:", "  default_project:"]
        if identifier is not None:
            lines.append(f"    identifier: {identifier}")
        if url is not None:
            lines.append(f"    url: {url}/projects/{identifier}")
        (self.root / ".mozyo-bridge" / "project-defaults.yaml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return self.root


class LiveBucketReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RepoFixture(self)

    def _read(self, opener, *, repo_root=None, environ=None, **kwargs):
        return read_live_fixed_version_bucket(
            repo_root=repo_root or self.repo.declare(),
            environ=_ENV if environ is None else environ,
            home=_HOME,
            opener=opener,
            **kwargs,
        )

    def test_reads_project_scoped_versions_then_project_scoped_issues(self) -> None:
        opener = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"}),
            issues=_issues_payload(_issue(1), _issue(2, parent=1)),
        )
        live = self._read(opener, bucket_id="292")

        self.assertEqual(live.project_identifier, _PROJECT)
        self.assertEqual(live.version_id, "292")
        self.assertEqual(live.version_name, "枠")
        self.assertEqual(
            opener.paths,
            [f"/projects/{_PROJECT}/versions.json", "/issues.json"],
        )
        # The issues read is project-scoped: a *shared* Version must not pull in another
        # project's issues (j#76650 step 4).
        issues_query = opener.query_of(1)
        self.assertEqual(issues_query["project_id"], _PROJECT)
        self.assertEqual(issues_query["fixed_version_id"], "292")
        self.assertEqual(issues_query["status_id"], "*")
        # Read-only: both calls are GETs with no body.
        for request in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)

    def test_pure_provider_resolves_the_bucket_and_leaf_rule_is_reused(self) -> None:
        opener = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"}),
            issues=_issues_payload(
                _issue(1),  # parent of an open child -> non-leaf
                _issue(2, parent=1),  # open leaf
                _issue(3, closed=True),  # closed -> not a candidate
            ),
        )
        live = self._read(opener, bucket_id="292")
        resolution = live.provider.resolve_bucket("292")

        self.assertTrue(resolution.resolved)
        bucket = resolution.bucket
        self.assertEqual(bucket.status, "open")
        leaves = [i.issue_id for i in bucket.issues if i.is_leaf and not i.is_closed]
        self.assertEqual(leaves, ["2"])

    def test_resolves_the_version_by_name(self) -> None:
        opener = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"}),
            issues=_issues_payload(_issue(1)),
        )
        live = self._read(opener, bucket_name="枠")
        self.assertEqual(live.version_id, "292")

    def test_genuinely_empty_open_version_reads_as_an_empty_bucket(self) -> None:
        # The one legitimate empty result: an open Version with no issues resolves, and
        # is NOT a blocked read.
        opener = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"}),
            issues=_issues_payload(),
        )
        live = self._read(opener, bucket_id="292")
        self.assertEqual(live.issue_count, 0)
        self.assertTrue(live.provider.resolve_bucket("292").resolved)


class BlockedReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RepoFixture(self)

    def _block(self, opener, *, repo_root=None, environ=None, **kwargs) -> str:
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            read_live_fixed_version_bucket(
                repo_root=repo_root or self.repo.declare(),
                environ=_ENV if environ is None else environ,
                home=_HOME,
                opener=opener,
                **kwargs,
            )
        return ctx.exception.reason

    def test_no_project_defaults_blocks_without_network(self) -> None:
        opener = _RouteOpener()
        reason = self._block(opener, repo_root=self.repo.root, bucket_id="292")
        self.assertEqual(reason, LIVE_PROJECT_UNRESOLVED)
        self.assertEqual(opener.requests, [])

    def test_declared_url_without_a_host_blocks(self) -> None:
        repo = self.repo.declare(url=None)
        reason = self._block(_RouteOpener(), repo_root=repo, bucket_id="292")
        self.assertEqual(reason, LIVE_PROJECT_UNRESOLVED)

    def test_repo_host_mismatch_blocks_before_any_request(self) -> None:
        # The security-relevant one: a checkout that declares a different Redmine host is
        # not the workspace this key belongs to. Refuse before the key is ever sent.
        repo = self.repo.declare(url="https://evil.example")
        opener = _RouteOpener(versions=_versions_payload({"id": 292, "status": "open"}))
        reason = self._block(opener, repo_root=repo, bucket_id="292")
        self.assertEqual(reason, LIVE_PROJECT_HOST_MISMATCH)
        self.assertEqual(opener.requests, [])  # no request, so no key left the process

    def test_missing_credentials_block_without_network(self) -> None:
        opener = _RouteOpener()
        self.assertEqual(
            self._block(opener, environ={}, bucket_id="292"), READ_PROVIDER_UNAVAILABLE
        )
        self.assertEqual(
            self._block(opener, environ={BASE_URL_ENV: _HOST}, bucket_id="292"),
            READ_CREDENTIAL_MISSING,
        )
        self.assertEqual(opener.requests, [])

    def test_version_not_visible_to_the_project_blocks(self) -> None:
        # A Version id belonging to another project is not in this project's list: a
        # block, never a silent cross-project read or an empty bucket.
        opener = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"})
        )
        reason = self._block(opener, bucket_id="999")
        self.assertEqual(reason, LIVE_VERSION_NOT_FOUND)
        self.assertEqual(len(opener.requests), 1)  # versions read only; no issues read

    def test_unknown_version_name_blocks(self) -> None:
        opener = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"})
        )
        self.assertEqual(self._block(opener, bucket_name="別枠"), LIVE_VERSION_NOT_FOUND)

    def test_ambiguous_version_name_blocks_and_is_never_guessed(self) -> None:
        opener = _RouteOpener(
            versions=_versions_payload(
                {"id": 292, "name": "枠", "status": "open"},
                {"id": 293, "name": "枠", "status": "open"},
            )
        )
        self.assertEqual(self._block(opener, bucket_name="枠"), LIVE_VERSION_AMBIGUOUS)

    def test_closed_locked_and_unknown_status_all_block(self) -> None:
        # The fail-open gap this increment closes (j#76646 Finding 2): without a live
        # version-status read, a closed/locked Version would have yielded candidates.
        for status in ("closed", "locked", None, "", "archived"):
            with self.subTest(status=status):
                entry: dict = {"id": 292, "name": "枠"}
                if status is not None:
                    entry["status"] = status
                opener = _RouteOpener(
                    versions=_versions_payload(entry), issues=_issues_payload(_issue(1))
                )
                reason = self._block(opener, bucket_id="292")
                self.assertEqual(reason, LIVE_VERSION_NOT_OPEN)
                # Blocked at the status gate: the issues read never happens.
                self.assertEqual(len(opener.requests), 1)

    def test_no_selector_blocks(self) -> None:
        self.assertEqual(self._block(_RouteOpener()), LIVE_VERSION_NOT_FOUND)

    def test_transport_failure_on_either_read_blocks(self) -> None:
        versions_down = _RouteOpener(versions=urllib.error.URLError("down"))
        self.assertEqual(
            self._block(versions_down, bucket_id="292"), READ_TRANSPORT_ERROR
        )
        issues_down = _RouteOpener(
            versions=_versions_payload({"id": 292, "name": "枠", "status": "open"}),
            issues=urllib.error.URLError("down"),
        )
        self.assertEqual(self._block(issues_down, bucket_id="292"), READ_TRANSPORT_ERROR)

    def test_blocked_reasons_never_carry_the_api_key(self) -> None:
        opener = _RouteOpener(versions=urllib.error.URLError("down"))
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            read_live_fixed_version_bucket(
                repo_root=self.repo.declare(),
                bucket_id="292",
                environ={BASE_URL_ENV: _HOST, API_KEY_ENV: "super-secret-key"},
                home=_HOME,
                opener=opener,
            )
        self.assertNotIn("super-secret-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
