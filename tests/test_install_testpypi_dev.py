"""Hermetic regression tests for scripts/install_testpypi_dev.sh.

The install runbook (Redmine #13586) must assert that BOTH console entry
points report the exact pinned dev version and exit non-zero if either
disagrees, instead of merely displaying `--version`. These tests exercise the
script against a fake ``pipx`` and fake ``mozyo-bridge`` / ``mozyo`` CLIs on a
shadowed PATH so no network, no real install, and no real package are needed.

Three branches from the Start Gate acceptance (j#75722) are pinned:
  (a) both CLIs report the requested version -> exit 0
  (b) `mozyo-bridge --version` mismatches      -> non-zero
  (c) `mozyo --version` mismatches             -> non-zero
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "install_testpypi_dev.sh"

# Fake pipx: the runbook only shells out to `pipx install ... "$spec"`; a
# no-op success is enough (the real install is out of scope for this unit).
_FAKE_PIPX = "#!/bin/sh\nexit 0\n"

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
    def _run(self, requested: str, mb_version: str, mz_version: str):
        with tempfile.TemporaryDirectory() as tmp:
            fakebin = Path(tmp) / "bin"
            fakebin.mkdir()
            _write_exec(fakebin / "pipx", _FAKE_PIPX)
            _write_exec(fakebin / "mozyo-bridge", _FAKE_MOZYO_BRIDGE)
            _write_exec(fakebin / "mozyo", _FAKE_MOZYO)
            env = {
                **os.environ,
                # Shadow the real tools with the fakes; keep real coreutils/sh
                # on PATH behind them.
                "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_MB_VERSION": mb_version,
                "FAKE_MZ_VERSION": mz_version,
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
