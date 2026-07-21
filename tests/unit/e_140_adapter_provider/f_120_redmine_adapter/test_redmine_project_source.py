"""Unit tests for the live project-id read (Redmine #13687 R1-F1 / j#76747).

The Issues REST contract takes a **numeric** project id, not an identifier, so the
identifier the repo declares has to be resolved before it can scope an issues read. This
module pins that resolution: the documented Projects endpoint, the trusted destination,
and — crucially — the refusal to hand back anything that is not a real, positive, numeric
id belonging to the project that was actually asked for. A guessed or substituted project
id would silently re-scope the whole live dispatch read.

Hermetic: the opener is injected; no test touches a real Redmine.
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

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_project_source import (
    READ_PROJECT_UNRESOLVED,
    LiveRedmineProjectSource,
    live_project_source_from_env,
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

_PROJECT = "giken-3800-mozyo-bridge"


class _FakeResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class _RecordingOpener:
    def __init__(self, page: object):
        self._page = page
        self.requests: list[object] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if isinstance(self._page, Exception):
            raise self._page
        return _FakeResponse(self._page)

    @property
    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]


# Explicit non-secret placeholders: Source Tree Hygiene strict-fails on a
# credential-shaped literal in a tracked file, and the `fake-` marker is what the
# scanner classifies as a placeholder rather than a leaked key.
#
# The canary is deliberately DISTINCT from the default key: the redaction test
# overrides the key with it, so asserting the canary is absent from the failure
# reason proves the *caller-supplied* key never reaches the message.
_FAKE_API_KEY = "fake-api-key"
_REDACTION_CANARY_KEY = "fake-api-key-redaction-canary"


def _source(page, **kwargs) -> tuple[LiveRedmineProjectSource, _RecordingOpener]:
    opener = _RecordingOpener(page)
    params = dict(api_key=_FAKE_API_KEY, base_url="https://redmine.example", opener=opener)
    params.update(kwargs)
    return LiveRedmineProjectSource(**params), opener


class ResolveProjectIdTest(unittest.TestCase):
    def test_resolves_the_numeric_id_from_the_projects_endpoint(self) -> None:
        source, opener = _source(
            {"project": {"id": 38, "identifier": _PROJECT, "name": "mozyo_bridge"}}
        )
        self.assertEqual(source.read_project_id(_PROJECT), 38)
        self.assertEqual(
            opener.urls[0], f"https://redmine.example/projects/{_PROJECT}.json"
        )
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertIsNone(opener.requests[0].data)

    def test_identifier_is_percent_encoded_and_cannot_traverse(self) -> None:
        source, opener = _source({"project": {"id": 1, "identifier": "../../admin"}})
        source.read_project_id("../../admin")
        path = urllib.parse.urlparse(opener.urls[0]).path
        self.assertEqual(path, "/projects/..%2F..%2Fadmin.json")


class FailClosedTest(unittest.TestCase):
    def _block(self, page, identifier: str = _PROJECT, **kwargs) -> str:
        source, _ = _source(page, **kwargs)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_id(identifier)
        return ctx.exception.reason

    def test_identifier_mismatch_blocks(self) -> None:
        # The endpoint accepts an id OR an identifier, so a response naming a different
        # project means the server resolved something else. Scoping the issues read to it
        # would silently plan against a project nobody asked for.
        self.assertEqual(
            self._block({"project": {"id": 7, "identifier": "some-other-project"}}),
            READ_PROJECT_UNRESOLVED,
        )

    def test_missing_project_object_blocks(self) -> None:
        self.assertEqual(self._block({"total_count": 1}), READ_PROJECT_UNRESOLVED)

    def test_missing_identifier_blocks(self) -> None:
        self.assertEqual(self._block({"project": {"id": 7}}), READ_PROJECT_UNRESOLVED)

    def test_missing_non_integer_boolean_and_non_positive_ids_all_block(self) -> None:
        # bool is an int subclass, so True would otherwise pass an isinstance(int) check
        # and be sent as project_id=True.
        for bad in (None, "38", 38.0, True, False, 0, -1):
            with self.subTest(project_id=bad):
                project: dict = {"identifier": _PROJECT}
                if bad is not None:
                    project["id"] = bad
                self.assertEqual(
                    self._block({"project": project}), READ_PROJECT_UNRESOLVED
                )

    def test_missing_base_url_and_api_key_block_without_network(self) -> None:
        source, opener = _source({"project": {"id": 1}}, base_url=None)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_id(_PROJECT)
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)
        self.assertEqual(opener.requests, [])

        source, opener = _source({"project": {"id": 1}}, api_key=None)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_id(_PROJECT)
        self.assertEqual(ctx.exception.reason, READ_CREDENTIAL_MISSING)
        self.assertEqual(opener.requests, [])

    def test_blank_identifier_blocks(self) -> None:
        self.assertEqual(
            self._block({"project": {"id": 1}}, identifier="  "), READ_TRANSPORT_ERROR
        )

    def test_401_and_403_map_to_unauthorized(self) -> None:
        for code in (401, 403):
            with self.subTest(code=code):
                err = urllib.error.HTTPError("u", code, "no", {}, io.BytesIO(b""))
                self.assertEqual(self._block(err), READ_UNAUTHORIZED)

    def test_404_and_network_and_refused_redirect_map_to_transport_error(self) -> None:
        cases = [
            urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"")),
            urllib.error.URLError("down"),
            RedmineRedirectRefused(302),
        ]
        for err in cases:
            with self.subTest(err=type(err).__name__):
                self.assertEqual(self._block(err), READ_TRANSPORT_ERROR)

    def test_non_object_body_blocks(self) -> None:
        self.assertEqual(self._block([1, 2, 3]), READ_TRANSPORT_ERROR)

    def test_reasons_never_carry_the_api_key(self) -> None:
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        source, _ = _source(err, api_key=_REDACTION_CANARY_KEY)
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_id(_PROJECT)
        self.assertNotIn(_REDACTION_CANARY_KEY, str(ctx.exception))


class BuilderTest(unittest.TestCase):
    def test_resolves_credentials_from_the_injected_environment(self) -> None:
        opener = _RecordingOpener({"project": {"id": 38, "identifier": _PROJECT}})
        source = live_project_source_from_env(
            environ={BASE_URL_ENV: "https://redmine.example", API_KEY_ENV: "fake-api-key"},
            home=Path("/nonexistent-home-for-test"),
            opener=opener,
        )
        self.assertEqual(source.read_project_id(_PROJECT), 38)

    def test_absent_credentials_fail_closed_at_read_time(self) -> None:
        source = live_project_source_from_env(
            environ={},
            home=Path("/nonexistent-home-for-test"),
            opener=_RecordingOpener({}),
        )
        with self.assertRaises(RedmineVersionReadUnavailable) as ctx:
            source.read_project_id(_PROJECT)
        self.assertEqual(ctx.exception.reason, READ_PROVIDER_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
