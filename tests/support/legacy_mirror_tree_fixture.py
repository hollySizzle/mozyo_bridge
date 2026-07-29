"""Shared mirror-tree fixture for the legacy project skill mirror family.

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).

`_staging_names` / `_open_descriptor_count` / `_stage_with_wrapper` were
defined on the test classes; §5.5 「共有 helper の所有」 promotes them here
because two or more destination modules use each of them.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    legacy_mirror_sync,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application.legacy_mirror_sync import (  # noqa: E402
    LegacyProjectSkillMirrorSync,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    MIRROR_RELATIVE,
    MIRRORED_REFERENCES,
    SOURCE_RELATIVE,
)

#: The thin wrapper `release check drift` and operators invoke.
SYNC_SCRIPT_PATH = ROOT / "scripts" / "sync_legacy_project_skill.sh"


class _MirrorTreeFixture(unittest.TestCase):
    """Builds a self-contained mirror tree in a temp dir."""

    def _stage(self, *, base: str | None = None) -> Path:
        """Build a mirror tree. ``base`` shortens the path when a case needs it.

        A Unix socket path is capped near 104 bytes, so binding one inside the
        default temp directory raises `AF_UNIX path too long` — which made the
        socket case an environment-dependent error rather than a test. Staging
        that case under a short base keeps it real everywhere instead of
        skipping it.
        """
        tmp = Path(tempfile.mkdtemp(dir=base))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        source = tmp / SOURCE_RELATIVE
        source.mkdir(parents=True)
        mirror = tmp / MIRROR_RELATIVE
        mirror.mkdir(parents=True)
        real = ROOT / SOURCE_RELATIVE
        for path in real.glob("*.md"):
            shutil.copy(path, source / path.name)
            if path.name in MIRRORED_REFERENCES:
                shutil.copy(path, mirror / path.name)
        (mirror.parent / "SKILL.md").write_text("adapter stub\n", encoding="utf-8")
        return tmp

    @staticmethod
    def _source(repo: Path) -> Path:
        return repo / SOURCE_RELATIVE

    @staticmethod
    def _mirror(repo: Path) -> Path:
        return repo / MIRROR_RELATIVE

    @staticmethod
    def _service(repo: Path, **kwargs: object) -> LegacyProjectSkillMirrorSync:
        return LegacyProjectSkillMirrorSync(repo, **kwargs)  # type: ignore[arg-type]

    @staticmethod
    @contextlib.contextmanager
    def _preflight_already_answered() -> Iterator[None]:
        """Run the service with the host capability probe answered in advance.

        The probe calls the same primitives the service does — that is what
        makes it a probe rather than an advertisement (#14651) — so an
        injection that fires on the *n*-th call to a global `os.unlink` or
        `os.close` would land on the probe before it ever reached the subject.
        Injections keyed on a descriptor, a name or a flag pick out their own
        call and do not need this.
        """
        with unittest.mock.patch.object(
            legacy_mirror_sync, "missing_platform_capabilities", return_value=()
        ):
            yield

    def assertBlocksWrite(self, repo: Path, expected_kind: str) -> None:
        """Both modes refuse, nothing is written, and the class is named."""
        service = self._service(repo)
        check_code, check_out, _ = service.check()
        self.assertEqual(1, check_code)
        self.assertEqual((), check_out, "a violated contract must not print success")

        before = self._snapshot(self._mirror(repo))
        sync_code, sync_out, sync_err = service.sync()
        self.assertEqual(1, sync_code)
        self.assertEqual((), sync_out)
        self.assertIn("nothing was written", sync_err[0])
        self.assertEqual(before, self._snapshot(self._mirror(repo)))
        self.assertIn(expected_kind, service.audit().kinds())

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, bytes | None]:
        if not directory.is_dir() or directory.is_symlink():
            return {}
        out: dict[str, bytes | None] = {}
        for entry in directory.iterdir():
            try:
                out[entry.name] = entry.read_bytes() if entry.is_file() else None
            except OSError:
                out[entry.name] = None
        return out

    # --- promoted from the test classes: used by two or more destination
    # --- modules, so §5.5 「共有 helper の所有」 places them here ------------------

    def _open_descriptor_count(self) -> int:
        return len(os.listdir("/dev/fd"))

    def _staging_names(self, repo: Path) -> list[str]:
        return [
            p.name
            for p in self._mirror(repo).iterdir()
            if p.name.startswith(".mozyo-legacy-mirror.")
        ]

    def _stage_with_wrapper(self) -> Path:
        repo = self._stage()
        (repo / "scripts").mkdir()
        shutil.copy(SYNC_SCRIPT_PATH, repo / "scripts" / SYNC_SCRIPT_PATH.name)
        (repo / "scripts" / SYNC_SCRIPT_PATH.name).chmod(0o755)
        (repo / "src").mkdir()
        shutil.copytree(
            ROOT / "src" / "mozyo_bridge",
            repo / "src" / "mozyo_bridge",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        return repo
