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
silent add/drop is caught.

Redmine #14580 adds the missing *recovery and gating* half. Detection alone was
not enough: commit `7ca3380f` ("Pin coordinator work-unit resolution") updated
canonical and the plugin mirror and skipped this one, because the plugin mirror
had `scripts/sync_plugin_skill.sh` plus a `release check drift` gate while this
mirror had neither — only a manual "copy it by hand" convention. The drift then
sat undetected until a full-suite run, which the focused pre-commit lane
deliberately does not perform. `scripts/sync_legacy_project_skill.sh` is now the
recovery command and `mozyo-bridge release check drift` its fail-closed gate;
`LegacySkillSyncScriptTest` below pins the script's behavior, including a
cross-check that the script's pinned reference set and this module's
:data:`MIRRORED_REFERENCES` cannot drift apart.

Resolve any parity failure by editing the canonical
`skills/mozyo-bridge-agent/references/<f>.md` first and then running
`scripts/sync_legacy_project_skill.sh` from the repo root, never by hand-editing
the mirror to diverge.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

#: Recovery / gating script introduced by Redmine #14580.
SYNC_SCRIPT_PATH = ROOT / "scripts" / "sync_legacy_project_skill.sh"

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


class LegacySkillSyncScriptTest(unittest.TestCase):
    """Pin `scripts/sync_legacy_project_skill.sh` (Redmine #14580).

    The parity assertions above tell an implementer that the mirror drifted.
    This class pins the mechanism that (a) fixes it and (b) fails closed
    before a commit lands, which is what was actually missing: the drift
    #14580 fixed was introduced *because* the only recovery path was a manual
    convention documented in prose.

    Every case runs against a staged temp tree, never the real worktree, so a
    failing assertion cannot leave the repo mutated.
    """

    def _stage(self, tmp: Path) -> Path:
        """Build a minimal in-sync repo: script + canonical + mirror."""
        (tmp / "scripts").mkdir(parents=True)
        staged_script = tmp / "scripts" / SYNC_SCRIPT_PATH.name
        shutil.copy(SYNC_SCRIPT_PATH, staged_script)
        staged_script.chmod(0o755)

        canonical = tmp / "skills" / "mozyo-bridge-agent" / "references"
        canonical.mkdir(parents=True)
        mirror = tmp / ".claude" / "skills" / "mozyo-bridge-agent" / "references"
        mirror.mkdir(parents=True)

        real_canonical = ROOT / "skills" / "mozyo-bridge-agent" / "references"
        for source in real_canonical.glob("*.md"):
            shutil.copy(source, canonical / source.name)
            # Only the pinned partial set gets mirrored — that asymmetry is
            # the contract, not an oversight.
            if source.name in MIRRORED_REFERENCES:
                shutil.copy(source, mirror / source.name)

        # The adapter stub: present, and deliberately not a canonical copy.
        (mirror.parent / "SKILL.md").write_text(
            "---\nname: mozyo-bridge-agent\n---\nadapter stub\n", encoding="utf-8"
        )
        return tmp

    def _check(self, repo: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), "--check"],
            capture_output=True,
            text=True,
        )

    def _sync(self, repo: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name)],
            capture_output=True,
            text=True,
        )

    def test_script_exists_and_is_executable(self) -> None:
        self.assertTrue(
            SYNC_SCRIPT_PATH.is_file(), f"missing sync script: {SYNC_SCRIPT_PATH}"
        )
        self.assertTrue(
            SYNC_SCRIPT_PATH.stat().st_mode & 0o111,
            "sync script must be executable so the documented "
            "`scripts/sync_legacy_project_skill.sh` invocation works",
        )

    def test_script_pinned_set_matches_this_modules_pinned_set(self) -> None:
        """The script's list and this module's list are one fact, not two.

        Both need the partial set, and a shell script cannot import a Python
        tuple. Duplicating it is only safe while something asserts the copies
        are equal — otherwise adding a mirrored reference to one side leaves
        the other silently un-enforcing, which is the same class of gap that
        produced this issue.
        """
        body = SYNC_SCRIPT_PATH.read_text(encoding="utf-8")
        match = re.search(r'^MIRRORED_REFERENCES="([^"]*)"$', body, re.MULTILINE)
        self.assertIsNotNone(
            match,
            "sync script must declare MIRRORED_REFERENCES=\"...\" on one line "
            "so this cross-check can read it",
        )
        assert match is not None  # narrow for type checkers
        script_set = set(match.group(1).split())
        self.assertEqual(
            set(MIRRORED_REFERENCES),
            script_set,
            "the sync script's mirrored reference set and this module's "
            "MIRRORED_REFERENCES drifted; update both in the same commit",
        )

    def test_check_passes_on_an_in_sync_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            result = self._check(repo)
            self.assertEqual(
                0, result.returncode, msg=result.stdout + result.stderr
            )
            self.assertIn("legacy project skill mirror is up to date", result.stdout)

    def test_check_fails_on_canonical_only_edit(self) -> None:
        """The confirmed defect's exact shape: canonical moves, mirror does not."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            canonical = (
                repo / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md"
            )
            canonical.write_text(
                canonical.read_text(encoding="utf-8") + "\nCANONICAL-ONLY EDIT\n",
                encoding="utf-8",
            )
            result = self._check(repo)
            self.assertEqual(1, result.returncode, msg=result.stdout)
            self.assertIn("drift detected", result.stderr)
            self.assertIn("references/workflow.md", result.stderr)
            # Recovery hint must be repo-root runnable, matching the plugin
            # mirror script's contract (Codex review #50344 precedent).
            self.assertIn("scripts/sync_legacy_project_skill.sh", result.stderr)
            self.assertIn("from the repo root", result.stderr)

    def test_sync_repairs_a_canonical_only_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            canonical = (
                repo / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md"
            )
            canonical.write_text(
                canonical.read_text(encoding="utf-8") + "\nCANONICAL-ONLY EDIT\n",
                encoding="utf-8",
            )
            self.assertEqual(1, self._check(repo).returncode)

            synced = self._sync(repo)
            self.assertEqual(0, synced.returncode, msg=synced.stderr)

            after = self._check(repo)
            self.assertEqual(0, after.returncode, msg=after.stdout + after.stderr)

    def test_sync_never_writes_the_adapter_stub(self) -> None:
        """A sync must not clobber the intentional `SKILL.md` divergence.

        This is why the legacy mirror cannot reuse the plugin mirror's
        `rsync -a --delete`: that would overwrite the adapter with canonical
        `SKILL.md` and delete the mirror's deliberate omissions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            adapter = (
                repo / ".claude" / "skills" / "mozyo-bridge-agent" / "SKILL.md"
            )
            before = adapter.read_bytes()

            synced = self._sync(repo)
            self.assertEqual(0, synced.returncode, msg=synced.stderr)

            self.assertEqual(
                before,
                adapter.read_bytes(),
                "sync overwrote the Claude Code adapter stub",
            )
            # The deliberately non-mirrored canonical references must also
            # stay out of the mirror after a sync.
            mirror_refs = {
                p.name
                for p in (
                    repo / ".claude" / "skills" / "mozyo-bridge-agent" / "references"
                ).glob("*.md")
            }
            self.assertEqual(set(MIRRORED_REFERENCES), mirror_refs)

    def test_check_fails_when_a_mirrored_file_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            (
                repo
                / ".claude/skills/mozyo-bridge-agent/references/safety.md"
            ).unlink()
            result = self._check(repo)
            self.assertEqual(1, result.returncode, msg=result.stdout)
            self.assertIn("missing file: references/safety.md", result.stderr)

    def test_check_fails_on_an_unpinned_extra_reference(self) -> None:
        """An extra mirrored file means the partial set was widened unreviewed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            (
                repo
                / ".claude/skills/mozyo-bridge-agent/references/subagent-delegation.md"
            ).write_text("smuggled in\n", encoding="utf-8")
            result = self._check(repo)
            self.assertEqual(1, result.returncode, msg=result.stdout)
            self.assertIn(
                "unpinned reference: references/subagent-delegation.md", result.stderr
            )

    def test_check_fails_when_the_mirror_directory_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            shutil.rmtree(repo / ".claude/skills/mozyo-bridge-agent/references")
            result = self._check(repo)
            self.assertEqual(1, result.returncode, msg=result.stdout)
            self.assertIn("mirror missing", result.stderr)

    def test_missing_canonical_source_is_an_error_in_both_modes(self) -> None:
        """A pinned name canonical no longer has must never read as 'in sync'.

        Skipping the missing name would let `--check` exit 0 with a stale
        mirror file still on disk — a fail-open the gate exists to prevent.
        """
        for mode in ([], ["--check"]):
            with self.subTest(mode=mode or ["sync"]):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = self._stage(Path(tmp))
                    (
                        repo
                        / "skills/mozyo-bridge-agent/references/safety.md"
                    ).unlink()
                    result = subprocess.run(
                        ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), *mode],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(1, result.returncode, msg=result.stdout)
                    self.assertIn("canonical reference missing", result.stderr)

    def test_unknown_argument_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            result = subprocess.run(
                ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), "--force"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(64, result.returncode)
            self.assertIn("unknown argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
