"""Hermetic regression tests for scripts/install_testpypi_dev.sh.

The install runbook (Redmine #13586) must assert that BOTH console entry
points report the exact pinned dev version and exit non-zero if either
disagrees, instead of merely displaying `--version`. These tests exercise the
script against a fake ``pipx`` and fake ``mozyo-bridge`` / ``mozyo`` CLIs on a
shadowed PATH so no network, no real install, and no real package are needed.

Six branches from the Start Gate acceptance (j#75722), Redmine #14978, and
Redmine #14980 are pinned:
  (a) both CLIs report the requested version -> exit 0
  (b) `mozyo-bridge --version` mismatches      -> non-zero
  (c) `mozyo --version` mismatches             -> non-zero
  (d) the pip backend bypasses stale index cache for a just-published exact version
  (e) a delayed TestPyPI Simple listing is polled before pipx runs
  (f) a bounded propagation timeout exits before pipx changes the environment
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

# This file lives at tests/regressions/, so the repo root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "install_testpypi_dev.sh"

# Fake pipx: echo every argument so tests can assert the exact pip backend
# policy without touching the network or the real pipx environment.
_FAKE_PIPX = (
    "#!/bin/sh\n"
    "for arg in \"$@\"; do\n"
    "  printf 'FAKE_PIPX_ARG=%s\\n' \"$arg\"\n"
    "done\n"
    "exit 0\n"
)

# Fake TestPyPI Simple Index client. It stays empty until the configured call
# count, then emits one filename containing the exact requested version. Every
# argument is copied to stderr so the no-cache / PEP 691 request policy can be
# asserted without a network call.
_FAKE_CURL = (
    "#!/bin/sh\n"
    "for arg in \"$@\"; do\n"
    "  printf 'FAKE_CURL_ARG=%s\\n' \"$arg\" >&2\n"
    "done\n"
    'count_file="$FAKE_CURL_COUNT_FILE"\n'
    "count=0\n"
    'if [ -f "$count_file" ]; then count="$(cat "$count_file")"; fi\n'
    "count=$((count + 1))\n"
    'printf "%s\\n" "$count" > "$count_file"\n'
    'ready_after="${FAKE_SIMPLE_READY_AFTER:-1}"\n'
    'if [ "$count" -ge "$ready_after" ]; then\n'
    "  printf '{\"files\":[{\"filename\":\"mozyo_bridge-%s-py3-none-any.whl\"}]}\\n' \"$FAKE_SIMPLE_VERSION\"\n"
    "else\n"
    "  printf '{\"files\":[]}\\n'\n"
    "fi\n"
)

# Keep the production 15-second interval in the script while making polling
# branches hermetic and instant in the regression suite.
_FAKE_SLEEP = (
    "#!/bin/sh\n"
    "printf 'FAKE_SLEEP=%s\\n' \"$1\"\n"
)

# Fake console entry points. Each reports "<prog> <version>" for `--version`
# (matching argparse's `%(prog)s {__version__}`) where the version is injected
# via an env var, and returns success for the `--help` surface probes the
# runbook makes. The version each reports is controlled per-test so mismatch
# branches can be forced independently.
_FAKE_MOZYO_BRIDGE = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  --version) echo "mozyo-bridge $FAKE_MB_VERSION" ;;\n'
    "  *) exit 0 ;;\n"
    "esac\n"
)
_FAKE_MOZYO = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  --version) echo "mozyo $FAKE_MZ_VERSION" ;;\n'
    "  *) exit 0 ;;\n"
    "esac\n"
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class InstallTestPyPIDevScriptTest(unittest.TestCase):
    def _run(
        self,
        requested: str,
        mb_version: str,
        mz_version: str,
        *,
        simple_version: str | None = None,
        simple_ready_after: int = 1,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            fakebin = Path(tmp) / "bin"
            fakebin.mkdir()
            _write_exec(fakebin / "pipx", _FAKE_PIPX)
            _write_exec(fakebin / "curl", _FAKE_CURL)
            _write_exec(fakebin / "sleep", _FAKE_SLEEP)
            _write_exec(fakebin / "mozyo-bridge", _FAKE_MOZYO_BRIDGE)
            _write_exec(fakebin / "mozyo", _FAKE_MOZYO)
            env = {
                **os.environ,
                # Shadow the real tools with the fakes; keep real coreutils/sh
                # on PATH behind them.
                "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_MB_VERSION": mb_version,
                "FAKE_MZ_VERSION": mz_version,
                "FAKE_SIMPLE_VERSION": simple_version or requested,
                "FAKE_SIMPLE_READY_AFTER": str(simple_ready_after),
                "FAKE_CURL_COUNT_FILE": str(Path(tmp) / "curl-count"),
            }
            return subprocess.run(
                ["sh", str(_SCRIPT), requested],
                env=env,
                capture_output=True,
                text=True,
            )

    def test_matching_versions_succeed(self) -> None:
        version = "0.10.0.dev123456"
        result = self._run(version, mb_version=version, mz_version=version)
        self.assertEqual(
            0,
            result.returncode,
            f"expected success; stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("OK: mozyo-bridge --version == 0.10.0.dev123456", result.stdout)
        self.assertIn("OK: mozyo --version == 0.10.0.dev123456", result.stdout)

    def test_install_disables_stale_simple_index_cache(self) -> None:
        version = "0.10.0.dev123456"
        result = self._run(version, mb_version=version, mz_version=version)
        self.assertEqual(0, result.returncode, result.stderr)
        pip_args = [
            line.removeprefix("FAKE_PIPX_ARG=")
            for line in result.stdout.splitlines()
            if line.startswith("FAKE_PIPX_ARG=--extra-index-url ")
        ]
        self.assertEqual(1, len(pip_args), result.stdout)
        self.assertIn("--no-cache-dir", pip_args[0].split())
        self.assertIn("FAKE_CURL_ARG=Cache-Control: no-cache", result.stderr)
        self.assertIn(
            "FAKE_CURL_ARG=Accept: application/vnd.pypi.simple.v1+json",
            result.stderr,
        )

    def test_waits_for_simple_index_propagation_before_install(self) -> None:
        version = "0.10.0.dev123456"
        result = self._run(
            version,
            mb_version=version,
            mz_version=version,
            simple_ready_after=3,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, result.stdout.count("FAKE_SLEEP=15"), result.stdout)
        self.assertIn("OK: TestPyPI Simple Index lists", result.stdout)
        self.assertIn("FAKE_PIPX_ARG=install", result.stdout)

    def test_simple_index_timeout_preserves_existing_environment(self) -> None:
        version = "0.10.0.dev123456"
        result = self._run(
            version,
            mb_version=version,
            mz_version=version,
            simple_version="0.10.0.dev-not-the-requested-version",
            simple_ready_after=1,
        )
        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(40, result.stdout.count("FAKE_SLEEP=15"), result.stdout)
        self.assertNotIn("FAKE_PIPX_ARG=", result.stdout)
        self.assertIn("existing pipx environment was not changed", result.stderr)

    def test_stable_version_does_not_match_alpha_filename_substring(self) -> None:
        requested = "0.15.0"
        result = self._run(
            requested,
            mb_version=requested,
            mz_version=requested,
            simple_version="0.15.0a4",
            simple_ready_after=1,
        )
        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(40, result.stdout.count("FAKE_SLEEP=15"), result.stdout)
        self.assertNotIn("FAKE_PIPX_ARG=", result.stdout)

    def test_mozyo_bridge_mismatch_fails(self) -> None:
        version = "0.10.0.dev123456"
        result = self._run(version, mb_version="0.10.0", mz_version=version)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("mozyo-bridge", result.stderr)
        self.assertIn("0.10.0", result.stderr)

    def test_mozyo_mismatch_fails(self) -> None:
        # mozyo-bridge matches so the failure must come from the second CLI.
        version = "0.10.0.dev123456"
        result = self._run(version, mb_version=version, mz_version="0.9.9")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("mozyo", result.stderr)
        # The matching first CLI still reported OK before the mismatch aborted.
        self.assertIn("OK: mozyo-bridge --version == 0.10.0.dev123456", result.stdout)

    def test_latest_is_rejected(self) -> None:
        # Guardrail unrelated to the version assertion but part of the runbook
        # contract: an unpinned `latest` must be refused before any install.
        result = self._run("latest", mb_version="x", mz_version="x")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("latest", result.stderr)


if __name__ == "__main__":
    unittest.main()
