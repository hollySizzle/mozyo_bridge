"""Host capability probe unit tests (Redmine #14651).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import os
import stat
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    platform_capabilities,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


class PlatformCapabilityProbeTest(_MirrorTreeFixture):
    """The host capability probe, with no real external collaborator."""

    def test_an_interrupt_during_the_probe_is_not_a_missing_capability(self) -> None:
        """Unknown exceptions fail closed, but `BaseException` is not unknown —
        swallowing an interrupt would report the host as unsupported and let
        the run continue as if it had measured something."""

        def interrupted(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with unittest.mock.patch.object(os, "lstat", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                platform_capabilities.missing_platform_capabilities()

    def test_the_probe_anchor_is_not_a_directory(self) -> None:
        """Why the above holds. If the anchor ever became a real directory the
        probes would start acting on it, and `mkdir` / `replace` / `unlink`
        would resolve their names instead of being rejected."""
        with platform_capabilities._probe_anchor() as anchor:
            self.assertIsNotNone(anchor)
            self.assertFalse(stat.S_ISDIR(os.fstat(anchor).st_mode))


if __name__ == "__main__":
    unittest.main()
