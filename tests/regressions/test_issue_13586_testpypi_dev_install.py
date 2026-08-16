"""Hermetic regression tests for scripts/install_testpypi_dev.sh.

The install runbook (Redmine #13586) must assert that BOTH console entry
points report the exact pinned dev version and exit non-zero if either
disagrees, instead of merely displaying `--version`. These tests exercise the
script against a fake ``pipx`` and fake ``mozyo-bridge`` / ``mozyo`` CLIs on a
shadowed PATH so no network, no real install, and no real package are needed.

Branches from the Start Gate acceptance (j#75722), Redmine #14978, Redmine
#14980, and the #15487 R2 source-provenance gate are pinned:
  (a) both CLIs report the requested version -> exit 0
  (b) `mozyo-bridge --version` mismatches      -> non-zero
  (c) `mozyo --version` mismatches             -> non-zero
  (d) the pip backend bypasses stale index cache for a just-published exact version
  (e) a delayed TestPyPI Simple listing is polled before pipx runs
  (f) a bounded propagation timeout exits before pipx changes the environment
  (g) stable versions do not substring-match alpha filenames
  (h) readiness polls the same canonical Simple URL that pip resolves
  (i) unpinned `latest` is rejected before any install
  (j) an exact rc candidate absent from PyPI (404) installs cleanly
  (k) the version present on production PyPI refuses before any install
  (l) a failed / unexpected-status / unreadable PyPI lookup fails closed
  (m) a PyPI 200 listing without the version proceeds
  (n) the PyPI response file is mktemp-created (no predictable PID path) and
      left behind by neither the success nor the failure branch
  (o) `--backend pip` is passed only to a pipx that advertises the flag
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
    'if [ "$2" = "--help" ] || [ "$1" = "--help" ]; then\n'
    '  if [ "${FAKE_PIPX_HAS_BACKEND:-1}" = "1" ]; then\n'
    '    echo "  --backend {pip,uv}  Which backend to use"\n'
    "  else\n"
    '    echo "  --force  Reinstall"\n'
    "  fi\n"
    "  exit 0\n"
    "fi\n"
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
    "is_pypi=0\n"
    "out_file=\"\"\n"
    "prev=\"\"\n"
    "for arg in \"$@\"; do\n"
    "  printf 'FAKE_CURL_ARG=%s\\n' \"$arg\" >&2\n"
    "  if [ \"$prev\" = \"--output\" ]; then out_file=\"$arg\"; fi\n"
    "  case \"$arg\" in *://pypi.org/*) is_pypi=1 ;; esac\n"
    "  prev=\"$arg\"\n"
    "done\n"
    "if [ \"$is_pypi\" -eq 1 ]; then\n"
    "  if [ \"${FAKE_PYPI_EXIT:-0}\" -ne 0 ]; then exit \"$FAKE_PYPI_EXIT\"; fi\n"
    "  body=\"${FAKE_PYPI_BODY:-}\"\n"
    "  if [ -z \"$body\" ]; then body='{\"files\":[]}'; fi\n"
    "  if [ -n \"$out_file\" ]; then printf '%s\\n' \"$body\" > \"$out_file\"; fi\n"
    "  printf '%s' \"${FAKE_PYPI_STATUS:-404}\"\n"
    "  exit 0\n"
    "fi\n"
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
        pypi_status: str = "404",
        pypi_body: str | None = None,
        pypi_exit: str = "0",
        pipx_has_backend: str = "1",
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
                "FAKE_PYPI_STATUS": pypi_status,
                "FAKE_PYPI_EXIT": pypi_exit,
                "FAKE_PIPX_HAS_BACKEND": pipx_has_backend,
                # Point the script's temp-file root at the per-test dir so the
                # leftover check below sees exactly this run's artifacts.
                "TMPDIR": tmp,
            }
            if pypi_body is not None:
                env["FAKE_PYPI_BODY"] = pypi_body
            result = subprocess.run(
                ["sh", str(_SCRIPT), requested],
                env=env,
                capture_output=True,
                text=True,
            )
            result.tmp_leftovers = sorted(  # type: ignore[attr-defined]
                p.name for p in Path(tmp).glob("mozyo-pypi-absence*")
            )
            return result

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

    def test_readiness_uses_pips_canonical_simple_url(self) -> None:
        version = "0.10.0.dev123456"
        result = self._run(version, mb_version=version, mz_version=version)
        self.assertEqual(0, result.returncode, result.stderr)
        simple_urls = [
            line.removeprefix("FAKE_CURL_ARG=")
            for line in result.stderr.splitlines()
            if line.startswith("FAKE_CURL_ARG=https://test.pypi.org/simple/mozyo-bridge/")
        ]
        self.assertEqual(
            ["https://test.pypi.org/simple/mozyo-bridge/"],
            simple_urls,
            "readiness must not use a cache-busting query that can propagate "
            "before the canonical URL used by pip",
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

    def test_exact_candidate_absent_from_pypi_installs(self) -> None:
        # (j) #15487 R2 finding_1: an exact release candidate (no `.dev`)
        # installs cleanly when production PyPI 404s, with no dev-only warning.
        version = "1.0.0rc5"
        result = self._run(version, mb_version=version, mz_version=version)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("FAKE_PIPX_ARG=install", result.stdout)
        self.assertIn("cannot resolve from PyPI", result.stdout)
        self.assertNotIn("warning", result.stdout + result.stderr)

    def test_version_present_on_pypi_refuses_before_install(self) -> None:
        # (k) pip gives the extra index no lower priority, so a same-version
        # PyPI artifact makes the TestPyPI source unprovable: refuse pre-mutation.
        version = "1.0.0rc5"
        result = self._run(
            version,
            mb_version=version,
            mz_version=version,
            pypi_status="200",
            pypi_body='{"files":[{"filename":"mozyo_bridge-1.0.0rc5-py3-none-any.whl"}]}',
        )
        self.assertEqual(75, result.returncode, result.stderr)
        self.assertNotIn("FAKE_PIPX_ARG=", result.stdout)
        self.assertIn("EXISTS on production PyPI", result.stderr)

    def test_failed_pypi_lookup_fails_closed(self) -> None:
        # (l) an unprovable absence is a refusal, never a proceed.
        version = "1.0.0rc5"
        for kwargs in (
            {"pypi_exit": "6"},
            {"pypi_status": "503"},
            {"pypi_status": "200", "pypi_body": "not json"},
        ):
            with self.subTest(**kwargs):
                result = self._run(
                    version, mb_version=version, mz_version=version, **kwargs
                )
                self.assertEqual(75, result.returncode, result.stderr)
                self.assertNotIn("FAKE_PIPX_ARG=", result.stdout)
                self.assertIn("fail-closed", result.stderr)

    def test_pypi_200_without_the_version_proceeds(self) -> None:
        # (m) a readable PyPI listing that does not carry the pinned version
        # proves absence just as a 404 does.
        version = "1.0.0rc5"
        result = self._run(
            version,
            mb_version=version,
            mz_version=version,
            pypi_status="200",
            pypi_body='{"files":[{"filename":"mozyo_bridge-0.9.0-py3-none-any.whl"}]}',
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("FAKE_PIPX_ARG=install", result.stdout)
        self.assertIn("does not list 1.0.0rc5", result.stdout)

    def test_pypi_response_file_is_mktemp_created_and_cleaned_up(self) -> None:
        # (n) #15487 R3 finding_1: a predictable PID-derived path in a shared
        # tmp lets another process pre-plant a symlink that `curl --output`
        # would then follow and truncate. The response file must be created
        # exclusively via mktemp and removed by every branch.
        script = _SCRIPT.read_text(encoding="utf-8")
        self.assertIn("mktemp", script)
        self.assertNotIn("mozyo-pypi-absence-$$", script)
        version = "1.0.0rc5"
        success = self._run(version, mb_version=version, mz_version=version)
        self.assertEqual(0, success.returncode, success.stderr)
        self.assertEqual([], success.tmp_leftovers)
        failure = self._run(
            version, mb_version=version, mz_version=version, pypi_status="503"
        )
        self.assertEqual(75, failure.returncode, failure.stderr)
        self.assertEqual([], failure.tmp_leftovers)

    def test_backend_flag_matches_what_this_pipx_advertises(self) -> None:
        # (o) #15507: `--backend` exists only on pipx versions that can pick a
        # non-pip backend. Passing it to an older pipx is an argparse ERROR that
        # aborts the install, not a no-op — observed live on pipx 1.8.0 during
        # the 1.0.0 QA. Such a pipx already uses pip, so omitting the flag keeps
        # the intended resolution.
        version = "1.0.0"
        with_backend = self._run(version, mb_version=version, mz_version=version)
        self.assertEqual(0, with_backend.returncode, with_backend.stderr)
        self.assertIn("FAKE_PIPX_ARG=--backend", with_backend.stdout)

        without = self._run(
            version, mb_version=version, mz_version=version, pipx_has_backend="0"
        )
        self.assertEqual(0, without.returncode, without.stderr)
        self.assertNotIn("FAKE_PIPX_ARG=--backend", without.stdout)
        self.assertIn("FAKE_PIPX_ARG=install", without.stdout)

    def test_latest_is_rejected(self) -> None:
        # Guardrail unrelated to the version assertion but part of the runbook
        # contract: an unpinned `latest` must be refused before any install.
        result = self._run("latest", mb_version="x", mz_version="x")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("latest", result.stderr)


if __name__ == "__main__":
    unittest.main()
