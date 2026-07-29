"""Regression pin for Redmine #14651: a stale capability advertisement must not
refuse a supported host.

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    platform_capabilities,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


class Issue14651CapabilityAdvertisementTest(_MirrorTreeFixture):
    """A stale `os.supports_dir_fd` advertisement must not refuse a supported host."""

    def test_a_supported_host_is_not_refused_by_a_stale_advertisement(self) -> None:
        """#14651. `os.supports_dir_fd` is a hand-maintained list in `os.py`,
        not a fact about the interpreter: CPython 3.12 on Linux omits
        `os.lstat` although `os.lstat(name, dir_fd=)` works there (measured on
        `python:3.12-slim`; 3.13 added the entry), and no version has ever
        listed `os.replace`. Reading it refused the whole Linux CI runner —
        every legacy mirror path collapsed into `platform_unsupported`, 91
        failures on a host that supports everything (Actions run 30383304588).

        The advertisement is emptied entirely rather than trimmed by one
        entry, so the test states the property — the probe does not consult it
        — rather than re-encoding whichever entry CPython happens to omit.
        """
        repo = self._stage()
        with unittest.mock.patch.object(os, "supports_dir_fd", frozenset()):
            with unittest.mock.patch.object(os, "supports_fd", frozenset()):
                self.assertEqual((), platform_capabilities.missing_platform_capabilities())
                code, out, err = self._service(repo).check()
        self.assertEqual(0, code, "\n".join(err))
        self.assertIn("up to date", "\n".join(out))

    def test_the_exact_linux_312_advertisement_is_accepted(self) -> None:
        """The CI condition itself: `lstat` missing from the set, everything
        else present. Kept alongside the emptied-set case because that one
        would still pass if the probe fell back to membership whenever the set
        looked implausible."""
        as_linux_312 = frozenset(os.supports_dir_fd) - {os.lstat}
        with unittest.mock.patch.object(os, "supports_dir_fd", as_linux_312):
            self.assertEqual((), platform_capabilities.missing_platform_capabilities())


if __name__ == "__main__":
    unittest.main()
