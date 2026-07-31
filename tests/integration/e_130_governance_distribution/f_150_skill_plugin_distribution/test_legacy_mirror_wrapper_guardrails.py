"""Legacy mirror sync wrapper guardrails (Redmine #13483 / #14580).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    MIRRORED_REFERENCES,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    SYNC_SCRIPT_PATH,
    _MirrorTreeFixture,
)


class LegacyMirrorWrapperGuardrailsTest(_MirrorTreeFixture):
    """The wrapper as a tracked artifact, plus the CLI's own refusal to run
    without it. These never execute the wrapper, so they are not the
    operator workflow that `test_legacy_mirror_wrapper_cli.py` pins."""

    def test_wrapper_exists_and_is_executable(self) -> None:
        self.assertTrue(SYNC_SCRIPT_PATH.is_file())
        self.assertTrue(SYNC_SCRIPT_PATH.stat().st_mode & 0o111)

    def test_wrapper_carries_no_mirror_logic(self) -> None:
        """Contract 1: one authority. A pinned name or an audit in the wrapper
        would be a second definition to drift from."""
        body = SYNC_SCRIPT_PATH.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        for name in MIRRORED_REFERENCES:
            self.assertNotIn(name, code, "the wrapper must not name pinned references")
        for token in ("cmp ", "rsync", "mkstemp", "MIRRORED_REFERENCES"):
            self.assertNotIn(token, code)

    def test_module_run_without_the_wrapper_refuses(self) -> None:
        """Running the CLI module directly must not silently pick a root."""
        repo = self._stage_with_wrapper()
        env = {k: v for k, v in os.environ.items() if k != "MOZYO_LEGACY_MIRROR_REPO_ROOT"}
        env["PYTHONPATH"] = str(repo / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution"
                ".application.cli_legacy_mirror_sync",
                "--check",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo),
            env=env,
            timeout=120,
        )
        self.assertEqual(64, result.returncode)
        self.assertIn("MOZYO_LEGACY_MIRROR_REPO_ROOT", result.stderr)


if __name__ == "__main__":
    unittest.main()
