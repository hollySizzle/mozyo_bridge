"""Tracked legacy project skill mirror tree guardrails (Redmine #13483 / #14580).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application.legacy_mirror_sync import (  # noqa: E402
    LegacyProjectSkillMirrorSync,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    MIRROR_RELATIVE,
    MIRRORED_REFERENCES,
)



def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LegacyProjectSkillMirrorTest(unittest.TestCase):
    """Guardrails on the tracked `.claude/skills/mozyo-bridge-agent/` mirror."""

    def setUp(self) -> None:
        self.canonical_ref_dir = ROOT / "skills" / "mozyo-bridge-agent" / "references"
        self.mirror_skill_dir = ROOT / ".claude" / "skills" / "mozyo-bridge-agent"
        self.mirror_ref_dir = self.mirror_skill_dir / "references"

    def test_mirror_reference_dirs_present(self) -> None:
        self.assertTrue(self.canonical_ref_dir.is_dir())
        self.assertTrue(self.mirror_ref_dir.is_dir())

    def test_mirror_reference_files_match_canonical(self) -> None:
        """Each mirrored reference file is byte-identical to canonical.

        Recovery: edit the canonical file under
        `skills/mozyo-bridge-agent/references/` first, then run
        `scripts/sync_legacy_project_skill.sh` from the repo root.
        """
        differing: list[str] = []
        missing: list[str] = []
        for name in MIRRORED_REFERENCES:
            canonical = self.canonical_ref_dir / name
            mirror = self.mirror_ref_dir / name
            self.assertTrue(canonical.is_file(), f"canonical missing: {canonical}")
            if not mirror.is_file():
                missing.append(name)
            elif _sha256(canonical) != _sha256(mirror):
                differing.append(name)

        hint = (
            "edit the canonical file under skills/mozyo-bridge-agent/references/ "
            "first, then run scripts/sync_legacy_project_skill.sh from the repo "
            "root (never hand-edit the mirror to diverge)"
        )
        self.assertFalse(missing, f"mirror missing files: {missing}; {hint}")
        self.assertFalse(differing, f"mirror differs from canonical: {differing}; {hint}")

    def test_mirror_reference_set_is_exactly_the_partial_set(self) -> None:
        """The mirror ships exactly the pinned partial set — every entry.

        Computed over `iterdir()` with no `is_file()` filter and no `*.md`
        glob. Both narrowings were measured letting an unpinned entry sit here
        while this equality held: `is_file()` follows symlinks so a dangling one
        drops out (j#90342 R2-F1), and `*.md` misses `unpinned.txt`, hidden
        entries and stale temps (j#90378 R4-F1).
        """
        present = {p.name for p in self.mirror_ref_dir.iterdir()}
        self.assertEqual(
            set(MIRRORED_REFERENCES),
            present,
            "mirror reference set drifted from the pinned partial set; expected "
            f"{sorted(MIRRORED_REFERENCES)}, found {sorted(present)}",
        )

    def test_mirror_references_are_regular_files(self) -> None:
        """No symlinks, and nothing non-regular (j#90342 R2-F1 / R3-F1).

        Asserted as two separate claims so a failure names its own cause — the
        earlier single assertion was called `..._are_regular_files_not_symlinks`
        while only checking the symlink half.
        """
        entries = sorted(self.mirror_ref_dir.iterdir())
        symlinked = sorted(p.name for p in entries if p.is_symlink())
        self.assertEqual([], symlinked, f"mirror references must not be symlinks: {symlinked}")
        non_regular = sorted(
            p.name for p in entries if not p.is_symlink() and not p.is_file()
        )
        self.assertEqual([], non_regular, f"mirror references must be regular files: {non_regular}")

    def test_mirror_path_has_no_symlinked_component(self) -> None:
        """Pointing `references/` at an external directory made the sync write
        the canonical bodies there and exit 0 (j#90342 R3-F1)."""
        symlinked = []
        probe = ROOT
        for part in MIRROR_RELATIVE.split("/"):
            probe = probe / part
            if probe.is_symlink():
                symlinked.append(str(probe.relative_to(ROOT)))
        self.assertEqual([], symlinked)

    def test_tracked_tree_satisfies_the_contract(self) -> None:
        """The authority's own verdict on the repo, not a restatement of it."""
        audit = LegacyProjectSkillMirrorSync(ROOT).audit()
        self.assertTrue(audit.ok, msg="\n".join(audit.report_lines()))

    def test_adapter_skill_md_present_and_not_a_canonical_copy(self) -> None:
        """`SKILL.md` is an intentional Claude Code adapter stub."""
        mirror_skill_md = self.mirror_skill_dir / "SKILL.md"
        canonical_skill_md = ROOT / "skills" / "mozyo-bridge-agent" / "SKILL.md"
        self.assertTrue(mirror_skill_md.is_file())
        self.assertTrue(canonical_skill_md.is_file())
        self.assertNotEqual(_sha256(canonical_skill_md), _sha256(mirror_skill_md))


if __name__ == "__main__":
    unittest.main()
