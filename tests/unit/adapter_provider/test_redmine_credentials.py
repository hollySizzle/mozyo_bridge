"""Daemon-trusted Redmine credential resolution tests (Redmine #12306).

Covers the launchd key-delivery follow-up: per-field env/file precedence,
the home-scoped credential file, fail-closed permission handling, malformed
/ missing config degrading to ``unconfigured`` without crashing, and — the
US acceptance condition that matters most — that the credential value never
leaks into the dataclass repr, a warning string, or the receiver wiring.

No real credential is used (the sentinel below is obviously fake) and no
real home is touched: every test injects an explicit ``home`` tmpdir and an
explicit ``environ`` mapping.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.redmine_credentials import (  # noqa: E402
    CREDENTIALS_FILENAME,
    RedmineCredentials,
    credentials_path,
    resolve_redmine_credentials,
)

# Obviously-fake sentinel; never a real key. Distinctive so a leak is easy
# to assert against in any rendered string.
FAKE_KEY = "FAKE-12306-CREDENTIAL-SENTINEL"
TRUSTED = "https://redmine.example.test"


def write_credentials(
    home: Path, *, url: str | None = TRUSTED, api_key: str | None = FAKE_KEY,
    mode: int = 0o600, body: str | None = None,
) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / CREDENTIALS_FILENAME
    if body is None:
        lines = ["redmine:"]
        if url is not None:
            lines.append(f"  url: {url}")
        if api_key is not None:
            lines.append(f"  api_key: {api_key}")
        body = "\n".join(lines) + "\n"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path


class ResolutionTest(unittest.TestCase):
    def test_env_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            creds = resolve_redmine_credentials(
                home=Path(tmp),
                environ={
                    "MOZYO_REDMINE_API_KEY": FAKE_KEY,
                    "MOZYO_REDMINE_URL": TRUSTED,
                },
            )
        self.assertEqual(creds.api_key, FAKE_KEY)
        self.assertEqual(creds.base_url, TRUSTED)
        self.assertEqual(creds.source["api_key"], "env")
        self.assertEqual(creds.source["base_url"], "env")
        self.assertEqual(creds.warnings, ())

    def test_file_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home)
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertEqual(creds.api_key, FAKE_KEY)
        self.assertEqual(creds.base_url, TRUSTED)
        self.assertEqual(creds.source["api_key"], "file")
        self.assertEqual(creds.source["base_url"], "file")
        self.assertEqual(creds.warnings, ())

    def test_env_overrides_file_per_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home, url="https://other.example.test",
                              api_key="FILE-ONLY-SENTINEL")
            creds = resolve_redmine_credentials(
                home=home,
                # env supplies the URL only; key falls back to the file.
                environ={"MOZYO_REDMINE_URL": TRUSTED},
            )
        self.assertEqual(creds.base_url, TRUSTED)
        self.assertEqual(creds.source["base_url"], "env")
        self.assertEqual(creds.api_key, "FILE-ONLY-SENTINEL")
        self.assertEqual(creds.source["api_key"], "file")

    def test_missing_file_and_env_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            creds = resolve_redmine_credentials(home=Path(tmp), environ={})
        self.assertIsNone(creds.api_key)
        self.assertIsNone(creds.base_url)
        self.assertEqual(creds.source, {"api_key": None, "base_url": None})
        self.assertEqual(creds.warnings, ())

    def test_credentials_path_honors_home(self) -> None:
        home = Path("/tmp/example-mozyo-home")
        self.assertEqual(
            credentials_path(home), home / "redmine-credentials.yaml"
        )


class FailClosedTest(unittest.TestCase):
    def test_group_or_world_readable_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = write_credentials(home, mode=0o644)
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertIsNone(creds.base_url)
        self.assertEqual(len(creds.warnings), 1)
        warning = creds.warnings[0]
        self.assertIn(str(path), warning)
        self.assertIn("permission", warning.lower())
        # Redaction: the refused file's secret must not appear.
        self.assertNotIn(FAKE_KEY, warning)

    def test_world_writable_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home, mode=0o666)
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertEqual(len(creds.warnings), 1)
        self.assertNotIn(FAKE_KEY, creds.warnings[0])

    def test_strict_perms_are_accepted(self) -> None:
        for mode in (0o600, 0o400):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                write_credentials(home, mode=mode)
                creds = resolve_redmine_credentials(home=home, environ={})
            self.assertEqual(creds.api_key, FAKE_KEY, f"mode={oct(mode)}")
            self.assertEqual(creds.warnings, ())

    def test_directory_at_credential_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / CREDENTIALS_FILENAME).mkdir(parents=True)
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertEqual(len(creds.warnings), 1)
        self.assertIn("regular file", creds.warnings[0])


class MalformedConfigTest(unittest.TestCase):
    def test_invalid_yaml_degrades_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home, body="redmine: [unterminated\n")
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertEqual(len(creds.warnings), 1)
        self.assertNotIn(FAKE_KEY, creds.warnings[0])

    def test_non_mapping_root_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home, body="- just a list\n")
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertIn("mapping", creds.warnings[0])

    def test_missing_redmine_section_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home, body="other: true\n")
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertIn("redmine", creds.warnings[0])

    def test_empty_file_is_silent_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(home, body="")
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertEqual(creds.warnings, ())

    def test_blank_field_values_are_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_credentials(
                home, body='redmine:\n  url: ""\n  api_key: "   "\n'
            )
            creds = resolve_redmine_credentials(home=home, environ={})
        self.assertIsNone(creds.api_key)
        self.assertIsNone(creds.base_url)


class RedactionTest(unittest.TestCase):
    def test_repr_masks_the_key(self) -> None:
        creds = RedmineCredentials(
            api_key=FAKE_KEY,
            base_url=TRUSTED,
            source={"api_key": "file", "base_url": "file"},
        )
        rendered = repr(creds)
        self.assertNotIn(FAKE_KEY, rendered)
        self.assertIn("***", rendered)
        # The non-secret URL is fine to render (aids diagnostics).
        self.assertIn(TRUSTED, rendered)

    def test_repr_none_key_is_not_masked_as_present(self) -> None:
        creds = RedmineCredentials()
        self.assertIn("api_key=None", repr(creds))


class ReceiverWiringTest(unittest.TestCase):
    """The launchd path end-to-end: a 0600 file with no env configures the
    receiver's Redmine cache, and the key never reaches the served payload."""

    def test_build_server_reads_credential_file_without_env(self) -> None:
        from mozyo_bridge.application.otel_receiver import build_server

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            write_credentials(home)
            from unittest.mock import patch

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("MOZYO_REDMINE_API_KEY", "MOZYO_REDMINE_URL")
            }
            clean_env["MOZYO_BRIDGE_HOME"] = str(home)
            with patch.dict("os.environ", clean_env, clear=True):
                server = build_server(host="127.0.0.1", port=0, home=home)
            try:
                cache = server.redmine_context  # type: ignore[attr-defined]
                # Credential reached the cache (so the layer is no longer
                # unconditionally unconfigured under launchd)...
                self.assertEqual(cache._api_key, FAKE_KEY)
                self.assertEqual(cache._base_url, TRUSTED)
                # ...but is never echoed where it could leak.
                self.assertNotIn(FAKE_KEY, repr(cache._cache))
            finally:
                server.server_close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
