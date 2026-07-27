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
import os
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
        `skills/mozyo-bridge-agent/references/` first, then run
        `scripts/sync_legacy_project_skill.sh` from the repo root.
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
            "edit the canonical file under skills/mozyo-bridge-agent/references/ "
            "first, then run scripts/sync_legacy_project_skill.sh from the repo "
            "root (never hand-edit the mirror to diverge)"
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

        The set is computed WITHOUT an `is_file()` filter (review j#90342
        R2-F1): `is_file()` follows symlinks, so a dangling `unpinned.md`
        symlink would drop out of `present` and the equality would hold while
        an unpinned entry sat in the mirror.
        """
        present = {p.name for p in self.mirror_ref_dir.glob("*.md")}
        self.assertEqual(
            set(MIRRORED_REFERENCES),
            present,
            "legacy project mirror reference set drifted from the pinned partial "
            f"set; expected {sorted(MIRRORED_REFERENCES)}, found {sorted(present)}",
        )

    def test_mirror_references_are_regular_files(self) -> None:
        """Every mirrored reference is a regular file (j#90342 R2-F1 / R3-F1).

        The mirror is a byte copy, so a symlink is never a correct entry, and
        neither is a directory / FIFO / socket / device. Both were measured
        breaking the sync: a symlinked pinned name passed content parity and
        made `cp` write through into the link target, and a directory under a
        pinned name made the sync create `safety.md/safety.md` and report
        success.

        R3-F1 also caught this assertion's name over-claiming what it checked —
        it said "regular files" while only asserting non-symlink. It now
        asserts both halves separately so each failure names its own cause.
        """
        entries = sorted(self.mirror_ref_dir.glob("*.md"))

        symlinked = sorted(p.name for p in entries if p.is_symlink())
        self.assertEqual(
            [],
            symlinked,
            "legacy project mirror references must not be symlinks; found "
            f"{symlinked}. Replace with a regular file, then run "
            "scripts/sync_legacy_project_skill.sh from the repo root.",
        )

        non_regular = sorted(
            p.name for p in entries if not p.is_symlink() and not p.is_file()
        )
        self.assertEqual(
            [],
            non_regular,
            "legacy project mirror references must be regular files (not "
            f"directories / FIFOs / sockets / devices); found {non_regular}.",
        )

    def test_mirror_destination_is_not_reached_through_a_symlink(self) -> None:
        """No component of the mirror path may be a symlink (j#90342 R3-F1).

        Pointing `references/` itself at an external directory made the sync
        write the canonical bodies into that directory and exit 0 — the entry
        checks all followed the link and saw a healthy mirror.
        """
        symlinked_components = []
        probe = ROOT
        for part in (".claude", "skills", "mozyo-bridge-agent", "references"):
            probe = probe / part
            if probe.is_symlink():
                symlinked_components.append(str(probe.relative_to(ROOT)))
        self.assertEqual(
            [],
            symlinked_components,
            "the legacy project mirror must live at a real path inside the "
            f"repo; these components are symlinks: {symlinked_components}",
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

    def test_check_does_not_offer_a_rerun_that_cannot_clear_the_drift(self) -> None:
        """Review j#90322 F1: the recovery line must match the drift class.

        An unpinned reference is the one class the sync refuses to resolve, so
        printing the blanket "rerun the sync" recovery for it points the
        operator at a command that exits 1 on the same state.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            (
                repo / ".claude/skills/mozyo-bridge-agent/references/unpinned.md"
            ).write_text("smuggled in\n", encoding="utf-8")
            result = self._check(repo)
            self.assertEqual(1, result.returncode, msg=result.stdout)
            self.assertIn("reviewed disposition", result.stderr)
            self.assertIn("does NOT clear them", result.stderr)
            self.assertNotIn("to resync the mirror", result.stderr)

    def test_sync_refuses_while_an_unpinned_reference_is_present(self) -> None:
        """Sync must not report success in the presence of a class it cannot fix.

        Review j#90322 F1: the sync previously copied the pinned set, exited 0
        and printed `synced legacy project skill mirror` while an unpinned file
        sat in the mirror — so the very next `--check` exited 1, and the
        documented "rerun the sync" recovery could never converge.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            canonical = (
                repo / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md"
            )
            canonical.write_text(
                canonical.read_text(encoding="utf-8") + "\nCANONICAL-ONLY EDIT\n",
                encoding="utf-8",
            )
            mirror_workflow = (
                repo
                / ".claude/skills/mozyo-bridge-agent/references/workflow.md"
            )
            before = mirror_workflow.read_bytes()
            (
                repo / ".claude/skills/mozyo-bridge-agent/references/unpinned.md"
            ).write_text("smuggled in\n", encoding="utf-8")

            synced = self._sync(repo)
            self.assertEqual(1, synced.returncode, msg=synced.stdout)
            self.assertNotIn("synced legacy project skill mirror", synced.stdout)
            self.assertIn("refusing to sync", synced.stderr)
            self.assertIn("references/unpinned.md", synced.stderr)
            # Audit runs before any write: the pending content drift must be
            # untouched, so the exit code describes the whole tree state.
            self.assertEqual(
                before,
                mirror_workflow.read_bytes(),
                "sync wrote a partial result before refusing",
            )

    def test_sync_converges_once_the_unpinned_reference_is_dispositioned(self) -> None:
        """After the reviewed delete, the documented recovery reaches green."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            canonical = (
                repo / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md"
            )
            canonical.write_text(
                canonical.read_text(encoding="utf-8") + "\nCANONICAL-ONLY EDIT\n",
                encoding="utf-8",
            )
            extra = repo / ".claude/skills/mozyo-bridge-agent/references/unpinned.md"
            extra.write_text("smuggled in\n", encoding="utf-8")

            self.assertEqual(1, self._sync(repo).returncode)
            self.assertEqual(1, self._check(repo).returncode)

            extra.unlink()  # the reviewed disposition

            synced = self._sync(repo)
            self.assertEqual(0, synced.returncode, msg=synced.stderr)
            after = self._check(repo)
            self.assertEqual(0, after.returncode, msg=after.stdout + after.stderr)

    def test_both_modes_reject_a_dangling_unpinned_symlink(self) -> None:
        """Review j#90342 R2-F1: `-e` is false for a dangling symlink.

        The glob no-match guard was `[ -e "$path" ] || continue`, so a dangling
        `unpinned.md` symlink was skipped as though the glob had not matched —
        both modes exited 0 and reported the mirror up to date while an
        unpinned entry sat in it. Same fail-open shape as R1 F1, reached
        through the entry TYPE rather than the mode.
        """
        for mode in (["--check"], []):
            with self.subTest(mode=mode or ["sync"]):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = self._stage(Path(tmp))
                    (
                        repo
                        / ".claude/skills/mozyo-bridge-agent/references/unpinned.md"
                    ).symlink_to("missing-target")
                    result = subprocess.run(
                        ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), *mode],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(1, result.returncode, msg=result.stdout)
                    self.assertNotIn("is up to date", result.stdout)
                    self.assertNotIn(
                        "synced legacy project skill mirror", result.stdout
                    )
                    self.assertIn(
                        "unpinned reference: references/unpinned.md", result.stderr
                    )

    def test_sync_refuses_a_symlinked_pinned_reference_without_writing_through(
        self,
    ) -> None:
        """A symlinked PINNED name is the dangerous case the type ban closes.

        Content parity follows the link and passes, so the unpinned audit
        cannot see it by construction. Left unchecked, the sync's `cp` writes
        THROUGH the link into its target — measured overwriting an unrelated
        file while exiting 0 and printing success.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            victim = repo / "victim.txt"
            victim.write_text("UNRELATED CONTENT\n", encoding="utf-8")
            pinned = repo / ".claude/skills/mozyo-bridge-agent/references/safety.md"
            pinned.unlink()
            pinned.symlink_to(victim)

            for mode in (["--check"], []):
                with self.subTest(mode=mode or ["sync"]):
                    result = subprocess.run(
                        ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), *mode],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(1, result.returncode, msg=result.stdout)
                    self.assertIn(
                        "reference is a symlink: references/safety.md", result.stderr
                    )

            self.assertEqual(
                "UNRELATED CONTENT\n",
                victim.read_text(encoding="utf-8"),
                "sync wrote through the symlink into its target",
            )

    def test_both_modes_reject_a_non_regular_pinned_entry(self) -> None:
        """Review j#90342 R3-F1: type-check pinned names, not just symlinks.

        A directory under a pinned name made the sync exit 0 with a success
        banner after creating `safety.md/safety.md`. A FIFO made `cp` block on
        open indefinitely. The preflight uses `-f`, a stat rather than an open,
        so it rejects the FIFO without blocking on it.
        """
        pinned = ".claude/skills/mozyo-bridge-agent/references/safety.md"
        for kind in ("directory", "fifo"):
            for mode in (["--check"], []):
                with self.subTest(kind=kind, mode=mode or ["sync"]):
                    with tempfile.TemporaryDirectory() as tmp:
                        repo = self._stage(Path(tmp))
                        target = repo / pinned
                        target.unlink()
                        if kind == "directory":
                            target.mkdir()
                        else:
                            os.mkfifo(target)

                        result = subprocess.run(
                            [
                                "sh",
                                str(repo / "scripts" / SYNC_SCRIPT_PATH.name),
                                *mode,
                            ],
                            capture_output=True,
                            text=True,
                            # A blocking `cp` on the FIFO would hang the suite;
                            # the timeout turns that regression into a failure.
                            timeout=30,
                        )
                        self.assertEqual(1, result.returncode, msg=result.stdout)
                        self.assertNotIn(
                            "synced legacy project skill mirror", result.stdout
                        )
                        self.assertNotIn("is up to date", result.stdout)
                        self.assertIn(
                            "not a regular file: references/safety.md",
                            result.stderr,
                        )
                        if kind == "directory":
                            self.assertFalse(
                                (target / "safety.md").exists(),
                                "sync wrote into the directory it should reject",
                            )

    def test_both_modes_reject_a_symlinked_mirror_destination(self) -> None:
        """Review j#90342 R3-F1: `-d "$dest"` follows symlinks.

        Pointing `references/` at an external directory made the sync write the
        canonical bodies there and exit 0 with a success banner: every entry
        check followed the link and saw a healthy mirror.
        """
        for mode in (["--check"], []):
            with self.subTest(mode=mode or ["sync"]):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = self._stage(Path(tmp))
                    outside = repo / "outside"
                    outside.mkdir()
                    sentinel = outside / "safety.md"
                    sentinel.write_text("OUTSIDE\n", encoding="utf-8")

                    mirror_refs = (
                        repo / ".claude/skills/mozyo-bridge-agent/references"
                    )
                    shutil.rmtree(mirror_refs)
                    mirror_refs.symlink_to(outside, target_is_directory=True)

                    result = subprocess.run(
                        ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), *mode],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(1, result.returncode, msg=result.stdout)
                    self.assertNotIn(
                        "synced legacy project skill mirror", result.stdout
                    )
                    self.assertIn("path component is a symlink", result.stderr)
                    self.assertEqual(
                        "OUTSIDE\n",
                        sentinel.read_text(encoding="utf-8"),
                        "sync wrote outside the mirror",
                    )

    def test_sync_replaces_by_rename_and_never_writes_through_a_hardlink(
        self,
    ) -> None:
        """Review j#90342 R3-F1 condition 2: replace the entry, not the inode.

        A hardlink IS a regular file, so no entry-type check can see it — the
        earlier `cp src dest` opened and truncated whatever the pinned name
        resolved to, rewriting an unrelated file's contents. Copying to a temp
        file and renaming it into place swaps the directory entry, leaving the
        old inode and its other names untouched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            victim = repo / "victim.txt"
            victim.write_text("UNRELATED CONTENT\n", encoding="utf-8")
            pinned = repo / ".claude/skills/mozyo-bridge-agent/references/safety.md"
            pinned.unlink()
            os.link(victim, pinned)

            synced = self._sync(repo)
            self.assertEqual(0, synced.returncode, msg=synced.stderr)

            self.assertEqual(
                "UNRELATED CONTENT\n",
                victim.read_text(encoding="utf-8"),
                "sync wrote through the hardlink into the shared inode",
            )
            # The mirror still converged: the entry now points at a fresh inode
            # carrying the canonical bytes.
            self.assertEqual(0, self._check(repo).returncode)
            self.assertEqual(1, pinned.stat().st_nlink)

    def test_sync_leaves_no_temp_file_behind(self) -> None:
        """The rename staging file must not become an unpinned reference.

        It is named as a dotfile (so the `*.md` audit cannot trip over it) and
        removed by a trap, but a leaked temp file in the mirror would be a new
        drift class invented by the fix.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage(Path(tmp))
            self.assertEqual(0, self._sync(repo).returncode)
            mirror_refs = repo / ".claude/skills/mozyo-bridge-agent/references"
            leftovers = sorted(
                p.name for p in mirror_refs.iterdir() if "tmp" in p.name
            )
            self.assertEqual([], leftovers)
            self.assertEqual(0, self._check(repo).returncode)

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
