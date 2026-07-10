"""Legacy project Claude skill partial-mirror parity tests (Redmine #13483).

The repo ships a grace-period-deprecated legacy project skill at
`.claude/skills/mozyo-bridge-agent/` so that `MOZYO_BRIDGE_CLAUDE_SCOPE=project`
installs and Claude Code sessions launched from the project root can load a
partial mirror of the shared skill body directly (see
`vibes/docs/logics/skill-distribution.md` ->
`## Legacy Project Claude Skill ... Grace-Period Deprecation`).

Unlike the plugin marketplace mirror (a *full* byte-for-byte copy guarded by
`PluginMarketplaceTest`), the project mirror is intentionally *partial*:

- Only the `references/{project-map,release,safety,workflow}.md` subset is
  mirrored; `redmine-issue-authoring.md`, `subagent-delegation.md`, and the
  `agents/` metadata are intentionally *not* shipped.
- `SKILL.md` is an intentional Claude Code adapter stub, not a copy of the
  canonical `SKILL.md`, so its content is *not* parity-checked here.

The distribution doc previously flagged that "the project-scope mirror has no
automatic drift test yet; add a doc-regression test or remove it before the
grace period ends." This test closes that gap: the mirrored reference files
must stay byte-identical to canonical, and the partial file set is pinned so a
silent add/drop is caught. Resolve any failure by copying the canonical content
into the mirror (edit `skills/mozyo-bridge-agent/references/<f>.md` first, then
mirror it), never by hand-editing the mirror to diverge.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

# The tracked partial-mirror reference set. Pinned so that adding or dropping a
# mirrored reference file is a deliberate, reviewed change rather than silent
# drift. Keep in lockstep with `git ls-files .claude/skills/` and the
# distribution doc's enumeration.
MIRRORED_REFERENCES = (
    "project-map.md",
    "release.md",
    "safety.md",
    "workflow.md",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LegacyProjectSkillMirrorTest(unittest.TestCase):
    """Guardrails for the legacy `.claude/skills/mozyo-bridge-agent/` mirror."""

    def setUp(self) -> None:
        self.canonical_ref_dir = (
            ROOT / "skills" / "mozyo-bridge-agent" / "references"
        )
        self.mirror_skill_dir = ROOT / ".claude" / "skills" / "mozyo-bridge-agent"
        self.mirror_ref_dir = self.mirror_skill_dir / "references"

    def test_mirror_reference_dirs_present(self) -> None:
        self.assertTrue(
            self.canonical_ref_dir.is_dir(),
            f"canonical references dir missing: {self.canonical_ref_dir}",
        )
        self.assertTrue(
            self.mirror_ref_dir.is_dir(),
            f"legacy project mirror references dir missing: {self.mirror_ref_dir}",
        )

    def test_mirror_reference_files_match_canonical(self) -> None:
        """Each mirrored reference file is byte-identical to canonical.

        Recovery: edit the canonical file under
        `skills/mozyo-bridge-agent/references/` first, then copy its content
        into the `.claude/skills/mozyo-bridge-agent/references/` mirror.
        """
        differing: list[str] = []
        missing: list[str] = []
        for name in MIRRORED_REFERENCES:
            canonical = self.canonical_ref_dir / name
            mirror = self.mirror_ref_dir / name
            self.assertTrue(
                canonical.is_file(),
                f"canonical reference missing: {canonical}",
            )
            if not mirror.is_file():
                missing.append(name)
                continue
            if _sha256(canonical) != _sha256(mirror):
                differing.append(name)

        hint = (
            "sync by copying the canonical content from "
            "skills/mozyo-bridge-agent/references/ into "
            ".claude/skills/mozyo-bridge-agent/references/ "
            "(edit canonical first, then mirror)"
        )
        self.assertFalse(missing, f"legacy project mirror missing files: {missing}; {hint}")
        self.assertFalse(
            differing,
            f"legacy project mirror content differs from canonical: {differing}; {hint}",
        )

    def test_mirror_reference_set_is_exactly_the_partial_set(self) -> None:
        """The mirror ships exactly the pinned partial reference set.

        This is intentionally *partial*: canonical carries additional
        references (`redmine-issue-authoring.md`, `subagent-delegation.md`)
        that are deliberately not mirrored. Pinning the set catches a silent
        add (a new canonical reference copied in without review) or drop.
        """
        present = {
            p.name for p in self.mirror_ref_dir.glob("*.md") if p.is_file()
        }
        self.assertEqual(
            set(MIRRORED_REFERENCES),
            present,
            "legacy project mirror reference set drifted from the pinned partial "
            f"set; expected {sorted(MIRRORED_REFERENCES)}, found {sorted(present)}",
        )

    def test_adapter_skill_md_present_and_not_a_canonical_copy(self) -> None:
        """`SKILL.md` is an intentional Claude Code adapter stub.

        It must exist so Claude Code can discover the skill from the project
        root, but it is deliberately *not* a byte-copy of the canonical
        `SKILL.md`; asserting divergence documents that intentional diff so a
        future well-meaning "sync" that clobbers the adapter is caught.
        """
        mirror_skill_md = self.mirror_skill_dir / "SKILL.md"
        canonical_skill_md = ROOT / "skills" / "mozyo-bridge-agent" / "SKILL.md"
        self.assertTrue(
            mirror_skill_md.is_file(),
            "legacy project mirror must ship SKILL.md so Claude Code can "
            "discover the skill when launched from the project root",
        )
        self.assertTrue(canonical_skill_md.is_file())
        self.assertNotEqual(
            _sha256(canonical_skill_md),
            _sha256(mirror_skill_md),
            "legacy project SKILL.md is expected to be an intentional adapter "
            "stub, not a copy of the canonical SKILL.md",
        )


if __name__ == "__main__":
    unittest.main()
