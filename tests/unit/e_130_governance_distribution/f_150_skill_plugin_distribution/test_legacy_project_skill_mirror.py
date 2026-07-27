"""Legacy project Claude skill partial-mirror tests (Redmine #13483 / #14580).

The repo ships a grace-period-deprecated legacy project skill at
`.claude/skills/mozyo-bridge-agent/` so that `MOZYO_BRIDGE_CLAUDE_SCOPE=project`
installs and Claude Code sessions launched from the project root can load a
partial mirror of the shared skill body directly (see
`vibes/docs/logics/skill-distribution.md` ->
`## Legacy Project Claude Skill ... Grace-Period Deprecation`).

Unlike the plugin marketplace mirror (a *full* byte-for-byte copy guarded by
`PluginMarketplaceTest`), the project mirror is intentionally *partial*: only
:data:`MIRRORED_REFERENCES` is shipped, and `SKILL.md` is an intentional Claude
Code adapter stub that is never parity-checked.

#13483 added the detection. #14580 added the recovery and the gate: commit
`7ca3380f` updated canonical and the plugin mirror and skipped this one,
because the plugin mirror had a sync script plus a `release check drift` gate
while this mirror had only a "copy it by hand" convention.

Six review rounds then found the same fail-open — reporting success in the
presence of a state the sync cannot resolve — through a different axis each
time. The design consultation answer (j#90402) moved the authority into Python:
`os.scandir` cannot re-split a filename, an exclusive `mkstemp` fd is real
ownership rather than a name prefix, and the rules are unit-testable
individually (see `test_legacy_mirror_contract.py`).

Layout of this module:

- :class:`LegacyProjectSkillMirrorTest` — the tracked tree itself.
- :class:`LegacyMirrorSyncServiceTest` — every adversarial case, in-process
  against the Python authority.
- :class:`LegacyMirrorWrapperCliTest` — the thin `scripts/` wrapper, black-box,
  pinning the operator-facing CLI contract the wrapper exists to preserve.

The pinned set is imported, not re-declared: there is exactly one definition
now, so there is nothing to cross-check and nothing to drift.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    legacy_mirror_sync,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application.legacy_mirror_sync import (  # noqa: E402
    HOOK_TEMP_CREATED,
    LegacyProjectSkillMirrorSync,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    CONTENT_DRIFT,
    ENTRY_NOT_REGULAR,
    ENTRY_SYMLINK,
    MIRROR_RELATIVE,
    MIRRORED_REFERENCES,
    PATH_COMPONENT_NOT_DIRECTORY,
    PATH_COMPONENT_SYMLINK,
    CLEANUP_FAILED,
    ENTRY_MISSING,
    ENTRY_UNREADABLE,
    PLATFORM_UNSUPPORTED,
    RECOVERY_REPLACE_ENTRY,
    RECOVERY_RESYNC,
    RULE_CONTENT_PARITY,
    SOURCE_MISSING,
    SOURCE_RELATIVE,
    SOURCE_SYMLINK,
    SOURCE_UNREADABLE,
    UNPINNED_ENTRY,
    WRITE_FAILED,
)

#: The thin wrapper `release check drift` and operators invoke.
SYNC_SCRIPT_PATH = ROOT / "scripts" / "sync_legacy_project_skill.sh"


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


class LegacyMirrorSyncServiceTest(_MirrorTreeFixture):
    """Every adversarial case from R1-R5, against the Python authority."""

    # --- the happy paths the sync exists for -------------------------------

    def test_clean_tree_passes_and_syncs_idempotently(self) -> None:
        repo = self._stage()
        service = self._service(repo)
        self.assertEqual(0, service.check()[0])
        self.assertEqual(0, service.sync()[0])
        self.assertEqual(0, service.check()[0])

    def test_canonical_only_edit_is_caught_and_repaired(self) -> None:
        """The confirmed defect's exact shape: canonical moves, mirror does not."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nEDIT\n", encoding="utf-8")

        service = self._service(repo)
        code, _, err = service.check()
        self.assertEqual(1, code)
        self.assertIn(CONTENT_DRIFT, service.audit().kinds())
        self.assertIn(RECOVERY_RESYNC, service.audit().recovery_actions())
        self.assertIn("Rerun 'scripts/sync_legacy_project_skill.sh'", "\n".join(err))

        self.assertEqual(0, service.sync()[0])
        self.assertEqual(0, service.check()[0])

    def test_content_drift_does_not_block_the_write(self) -> None:
        """Rule F is what the sync repairs — treating it as a blocker would
        make the command refuse its own job."""
        repo = self._stage()
        canonical = self._source(repo) / "safety.md"
        canonical.write_text("REPLACED\n", encoding="utf-8")
        self.assertFalse(self._service(repo).audit().blocks_write)
        self.assertEqual(0, self._service(repo).sync()[0])
        self.assertEqual("REPLACED\n", (self._mirror(repo) / "safety.md").read_text())

    def test_missing_mirror_directory_is_created_by_the_sync(self) -> None:
        repo = self._stage()
        shutil.rmtree(self._mirror(repo))
        service = self._service(repo)
        code, _, err = service.check()
        self.assertEqual(1, code)
        self.assertIn("Rerun 'scripts/sync_legacy_project_skill.sh'", "\n".join(err))
        self.assertEqual(0, service.sync()[0])
        self.assertEqual(0, service.check()[0])

    def test_sync_never_writes_the_adapter_stub_or_extra_references(self) -> None:
        repo = self._stage()
        adapter = self._mirror(repo).parent / "SKILL.md"
        before = adapter.read_bytes()
        self.assertEqual(0, self._service(repo).sync()[0])
        self.assertEqual(before, adapter.read_bytes())
        self.assertEqual(
            set(MIRRORED_REFERENCES), {p.name for p in self._mirror(repo).iterdir()}
        )

    # --- R5-F1: filename serialization -------------------------------------

    def test_entry_names_are_compared_losslessly(self) -> None:
        """j#90397 R5-F1. A shell word list lost the filename boundary: an
        entry named `project-map.md release.md` split into two words that both
        matched the pinned set, so both modes exited 0."""
        for name in (
            "project-map.md release.md",
            "project-map.md\trelease.md",
            "a\nb.md",
            "*",
            ".*",
            "unpinned.txt",
            ".unpinned.md",
        ):
            with self.subTest(entry=name):
                repo = self._stage()
                (self._mirror(repo) / name).write_text("smuggled\n", encoding="utf-8")
                self.assertBlocksWrite(repo, UNPINNED_ENTRY)

    def test_a_glob_named_entry_does_not_report_unrelated_paths(self) -> None:
        """The shell version re-expanded its own output, so an entry named `*`
        listed the repo's `AGENTS.md` / `CLAUDE.md` as mirror entries."""
        repo = self._stage()
        (self._mirror(repo) / "*").write_text("smuggled\n", encoding="utf-8")
        report = "\n".join(self._service(repo).check()[2])
        self.assertIn("'*'", report)
        for unrelated in ("AGENTS.md", "CLAUDE.md", "LICENSE"):
            self.assertNotIn(unrelated, report)

    def test_a_newline_named_entry_cannot_forge_a_success_line(self) -> None:
        repo = self._stage()
        (self._mirror(repo) / "x\nlegacy project skill mirror is up to date").write_text(
            "smuggled\n", encoding="utf-8"
        )
        code, out, err = self._service(repo).check()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        for line in err:
            self.assertNotIn("\n", line)

    def test_unpinned_subdirectory_is_an_entry_too(self) -> None:
        repo = self._stage()
        (self._mirror(repo) / "nested").mkdir()
        self.assertBlocksWrite(repo, UNPINNED_ENTRY)

    # --- R5-F2: temp ownership and concurrency -----------------------------

    def test_a_file_sharing_the_temp_prefix_is_never_deleted(self) -> None:
        """j#90397 R5-F2. A name prefix is not ownership: the sync deleted an
        arbitrary file that merely shared it, and reported success."""
        repo = self._stage()
        decoy = self._mirror(repo) / ".mozyo-legacy-mirror.keep-me.tmp"
        decoy.write_text("PRECIOUS\n", encoding="utf-8")
        self.assertBlocksWrite(repo, UNPINNED_ENTRY)
        self.assertEqual("PRECIOUS\n", decoy.read_text(encoding="utf-8"))

    def test_a_directory_sharing_the_temp_prefix_blocks_rather_than_hangs(self) -> None:
        repo = self._stage()
        (self._mirror(repo) / ".mozyo-legacy-mirror.adir.tmp").mkdir()
        self.assertBlocksWrite(repo, UNPINNED_ENTRY)

    def test_crash_residue_asks_for_a_reviewed_disposition(self) -> None:
        """Residue is indistinguishable from a file someone meant to keep, so
        the advice must not promise that a rerun clears it."""
        repo = self._stage()
        (self._mirror(repo) / ".mozyo-legacy-mirror.abc123.tmp").write_text(
            "half written\n", encoding="utf-8"
        )
        report = "\n".join(self._service(repo).check()[2])
        self.assertIn("reviewed disposition", report)
        self.assertNotIn("Rerun 'scripts/sync_legacy_project_skill.sh'", report)

    def test_a_concurrent_run_neither_deletes_nor_is_deleted(self) -> None:
        """j#90397 R5-F2, controlled. The later run must block on the earlier
        run's in-flight temp rather than delete it, and the earlier run must
        still finish green."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nEDIT\n", encoding="utf-8")

        observed: dict[str, object] = {}

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED and "second" not in observed:
                observed["temps"] = [
                    p.name
                    for p in self._mirror(repo).iterdir()
                    if p.name.startswith(".mozyo-legacy-mirror.")
                ]
                observed["second"] = self._service(repo).sync()

        first_code, _, _ = self._service(repo, progress_hook=hook).sync()
        second_code, second_out, second_err = observed["second"]  # type: ignore[misc]

        self.assertTrue(observed["temps"], "the fixture never observed an in-flight temp")
        self.assertEqual(1, second_code, "the later run must not write over an in-flight sync")
        self.assertEqual((), second_out)
        self.assertIn("nothing was written", second_err[0])
        self.assertEqual(0, first_code, "the earlier run must still converge")
        self.assertEqual(0, self._service(repo).check()[0])

    def test_successful_sync_leaves_no_temp_behind(self) -> None:
        repo = self._stage()
        self.assertEqual(0, self._service(repo).sync()[0])
        leftovers = [p.name for p in self._mirror(repo).iterdir() if "tmp" in p.name]
        self.assertEqual([], leftovers)

    def test_failed_sync_cleans_only_its_own_temp(self) -> None:
        """The error path is where a prefix-wide cleanup would do its damage.

        The happy path never reaches the cleanup branch, so a widened `finally`
        — deleting everything sharing the staging prefix, which is exactly what
        j#90397 R5-F2 reported — survives a green suite untouched unless this
        case exists. A foreign temp standing in for a concurrent run must be
        intact afterwards, and ours must be gone.
        """
        repo = self._stage()
        foreign = self._mirror(repo) / ".mozyo-legacy-mirror.someone-else.tmp"

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED:
                foreign.write_text("ANOTHER RUN\n", encoding="utf-8")
                raise RuntimeError("copy failed mid-flight")

        with self.assertRaises(RuntimeError):
            self._service(repo, progress_hook=hook).sync()

        self.assertEqual(
            "ANOTHER RUN\n",
            foreign.read_text(encoding="utf-8"),
            "cleanup reached beyond this run's own temp",
        )
        ours = [
            p.name
            for p in self._mirror(repo).iterdir()
            if p.name.startswith(".mozyo-legacy-mirror.") and p != foreign
        ]
        self.assertEqual([], ours, "this run's own temp survived a failure")

    def test_success_is_not_reported_on_an_unverified_tree(self) -> None:
        """Contract 6: re-audit before announcing success.

        Without the post-write re-audit, a tree that stopped conforming while
        the copies ran is reported as synced. The window is small but the
        report is the thing every gate above this trusts.
        """
        repo = self._stage()
        planted = {"done": False}

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED and not planted["done"]:
                planted["done"] = True
                (self._mirror(repo) / "unpinned.txt").write_text(
                    "arrived mid-sync\n", encoding="utf-8"
                )

        code, out, err = self._service(repo, progress_hook=hook).sync()
        self.assertEqual(1, code, msg="\n".join(out))
        self.assertEqual((), out)
        self.assertIn("did not converge", err[0])
        self.assertIn(UNPINNED_ENTRY, self._service(repo).audit().kinds())

    def test_written_references_are_mode_644(self) -> None:
        """`mkstemp` creates 0600; canonical and mirror are tracked 0644."""
        repo = self._stage()
        self.assertEqual(0, self._service(repo).sync()[0])
        for name in MIRRORED_REFERENCES:
            self.assertEqual(0o644, (self._mirror(repo) / name).stat().st_mode & 0o777)

    # --- R5-F3: recovery composition ---------------------------------------

    def test_invalid_source_never_offers_the_resync(self) -> None:
        """j#90397 R5-F3: the sync refuses at its own source preflight, so
        advertising a rerun sends the operator in a circle."""
        repo = self._stage()
        (self._source(repo) / "safety.md").unlink()
        report = "\n".join(self._service(repo).check()[2])
        self.assertIn("Restore the tracked canonical path", report)
        self.assertNotIn("Rerun 'scripts/sync_legacy_project_skill.sh'", report)
        self.assertBlocksWrite(repo, SOURCE_MISSING)

    def test_content_parity_is_skipped_when_the_source_is_invalid(self) -> None:
        repo = self._stage()
        (self._source(repo) / "safety.md").unlink()
        audit = self._service(repo).audit()
        self.assertIn(RULE_CONTENT_PARITY, audit.skipped_rules)
        self.assertNotIn(CONTENT_DRIFT, audit.kinds())

    # --- R4-F2: canonical source aliasing ----------------------------------

    def test_symlinked_canonical_reference_is_rejected(self) -> None:
        """j#90378 R4-F2: `-f` follows symlinks, so an aliased source was
        accepted and its external bytes copied into the mirror."""
        repo = self._stage()
        external = repo / "external-body.md"
        external.write_text("EXTERNAL BODY\n", encoding="utf-8")
        source = self._source(repo) / "safety.md"
        source.unlink()
        source.symlink_to(external)

        self.assertBlocksWrite(repo, SOURCE_SYMLINK)
        self.assertNotIn(
            "EXTERNAL BODY", (self._mirror(repo) / "safety.md").read_text(encoding="utf-8")
        )

    def test_symlinked_canonical_directory_is_rejected(self) -> None:
        repo = self._stage()
        external = repo / "external-refs"
        shutil.copytree(self._source(repo), external)
        shutil.rmtree(self._source(repo))
        self._source(repo).symlink_to(external, target_is_directory=True)
        self.assertBlocksWrite(repo, PATH_COMPONENT_SYMLINK)

    # --- R4-F3 / R3-F1: destination topology and entry types ---------------

    def test_non_directory_ancestor_is_topology_not_missing_mirror(self) -> None:
        """j#90378 R4-F3: reported as "mirror missing, rerun the sync", whose
        `mkdir -p` then failed."""
        repo = self._stage()
        ancestor = repo / ".claude" / "skills"
        shutil.rmtree(ancestor)
        ancestor.write_text("not a directory\n", encoding="utf-8")

        audit = self._service(repo).audit()
        self.assertIn(PATH_COMPONENT_NOT_DIRECTORY, audit.kinds())
        self.assertFalse(audit.dest_missing)
        report = "\n".join(self._service(repo).check()[2])
        self.assertNotIn("Rerun 'scripts/sync_legacy_project_skill.sh'", report)
        self.assertEqual(1, self._service(repo).sync()[0])

    def test_symlinked_mirror_destination_is_rejected(self) -> None:
        repo = self._stage()
        outside = repo / "outside"
        outside.mkdir()
        sentinel = outside / "safety.md"
        sentinel.write_text("OUTSIDE\n", encoding="utf-8")
        shutil.rmtree(self._mirror(repo))
        self._mirror(repo).symlink_to(outside, target_is_directory=True)

        self.assertEqual(1, self._service(repo).check()[0])
        self.assertEqual(1, self._service(repo).sync()[0])
        self.assertEqual("OUTSIDE\n", sentinel.read_text(encoding="utf-8"))

    def test_symlinked_pinned_entry_is_rejected_without_writing_through(self) -> None:
        repo = self._stage()
        victim = repo / "victim.txt"
        victim.write_text("UNRELATED\n", encoding="utf-8")
        pinned = self._mirror(repo) / "safety.md"
        pinned.unlink()
        pinned.symlink_to(victim)

        self.assertBlocksWrite(repo, ENTRY_SYMLINK)
        self.assertEqual("UNRELATED\n", victim.read_text(encoding="utf-8"))

    def test_dangling_symlink_entry_is_rejected(self) -> None:
        repo = self._stage()
        (self._mirror(repo) / "unpinned.md").symlink_to("missing-target")
        self.assertBlocksWrite(repo, UNPINNED_ENTRY)

    def test_non_regular_pinned_entries_are_rejected_without_blocking(self) -> None:
        """A directory made the shell sync create `safety.md/safety.md`; a FIFO
        made its `cp` block on open. `lstat` never opens either."""
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind):
                repo = self._stage()
                target = self._mirror(repo) / "safety.md"
                target.unlink()
                if kind == "directory":
                    target.mkdir()
                else:
                    os.mkfifo(target)
                self.assertBlocksWrite(repo, ENTRY_NOT_REGULAR)
                if kind == "directory":
                    self.assertFalse((target / "safety.md").exists())

    def test_hardlinked_entry_is_replaced_not_written_through(self) -> None:
        """A hardlink is a regular file, so no type check can see it. Replacing
        the directory entry leaves the shared inode alone (j#90342 R3-F1)."""
        repo = self._stage()
        victim = repo / "victim.txt"
        victim.write_text("UNRELATED\n", encoding="utf-8")
        pinned = self._mirror(repo) / "safety.md"
        pinned.unlink()
        os.link(victim, pinned)

        self.assertEqual(0, self._service(repo).sync()[0])
        self.assertEqual("UNRELATED\n", victim.read_text(encoding="utf-8"))
        self.assertEqual(1, pinned.stat().st_nlink)
        self.assertEqual(0, self._service(repo).check()[0])

    # --- R6-F1: bound descriptors vs. TOCTOU --------------------------------

    def test_entry_swapped_after_the_type_audit_is_not_read_through(self) -> None:
        """j#90418 R6-F1 case 1. `Path.read_bytes()` re-resolves the path, so a
        mirror entry re-pointed at an identical external file after rule E ran
        made content parity pass and the whole audit report clean."""
        repo = self._stage()
        external = repo / "external.md"
        shutil.copy(self._mirror(repo) / "safety.md", external)

        service = self._service(repo)
        original = service._audit_dest_entries

        def swap_after_audit(mirror_fd: int):  # type: ignore[no-untyped-def]
            result = original(mirror_fd)
            entry = self._mirror(repo) / "safety.md"
            entry.unlink()
            entry.symlink_to(external)
            return result

        service._audit_dest_entries = swap_after_audit  # type: ignore[method-assign]
        audit = service.audit()
        self.assertFalse(audit.ok, "an entry swapped mid-audit was read through")

    def test_source_parent_swapped_after_audit_writes_no_external_bytes(self) -> None:
        """j#90418 R6-F1 case 2. `O_NOFOLLOW` on the leaf does not stop an
        aliased *parent*: the sync wrote external bytes into the mirror and only
        noticed at the final re-audit."""
        repo = self._stage()
        external = repo / "ext"
        external.mkdir()
        for name in MIRRORED_REFERENCES:
            (external / name).write_text("EXTERNAL SOURCE BYTES\n", encoding="utf-8")

        service = self._service(repo)
        original = service.audit

        def swap_after_audit():  # type: ignore[no-untyped-def]
            result = original()
            shutil.rmtree(self._source(repo))
            self._source(repo).symlink_to(external, target_is_directory=True)
            return result

        service.audit = swap_after_audit  # type: ignore[method-assign]
        code, _, _ = service.sync()
        self.assertEqual(1, code)
        for name in MIRRORED_REFERENCES:
            self.assertNotIn(
                "EXTERNAL SOURCE BYTES",
                (self._mirror(repo) / name).read_text(encoding="utf-8"),
                "external bytes reached the mirror through an aliased source parent",
            )

    def test_mirror_parent_swapped_after_audit_writes_nothing_outside(self) -> None:
        """j#90418 R6-F1 case 3. The post-audit is detection, not prevention:
        the canonical bodies had already been written into the external
        directory by the time it ran."""
        repo = self._stage()
        external = repo / "ext"
        external.mkdir()
        for name in MIRRORED_REFERENCES:
            (external / name).write_text("OUTSIDE\n", encoding="utf-8")

        service = self._service(repo)
        original = service.audit

        def swap_after_audit():  # type: ignore[no-untyped-def]
            result = original()
            shutil.rmtree(self._mirror(repo))
            self._mirror(repo).symlink_to(external, target_is_directory=True)
            return result

        service.audit = swap_after_audit  # type: ignore[method-assign]
        code, _, _ = service.sync()
        self.assertEqual(1, code)
        for name in MIRRORED_REFERENCES:
            self.assertEqual(
                "OUTSIDE\n",
                (external / name).read_text(encoding="utf-8"),
                "the sync wrote outside the mirror through an aliased parent",
            )

    def test_staging_entry_rebound_mid_sync_is_not_swapped_into_place(self) -> None:
        """j#90418 R6-F1 case 4, plus what correcting it exposed.

        Re-binding this run's staging name to a victim symlink used to (a) let a
        path-based `chmod` change the victim's mode and (b) have `os.replace`
        install that symlink as a pinned reference — `rename` moves whatever the
        name refers to. `fchmod` on the owned fd fixes (a); an inode identity
        check before the swap fixes (b). The foreign entry is left alone: it is
        not ours to delete, and the next audit reports it.
        """
        repo = self._stage()
        victim = repo / "victim.txt"
        victim.write_text("VICTIM\n", encoding="utf-8")
        os.chmod(victim, 0o600)
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED:
                for path in self._mirror(repo).iterdir():
                    if path.name.startswith(".mozyo-legacy-mirror."):
                        path.unlink()
                        path.symlink_to(victim)
                        break

        code, out, err = self._service(repo, progress_hook=hook).sync()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("aborted", err[0])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(victim).st_mode), "victim mode changed")
        self.assertEqual("VICTIM\n", victim.read_text(encoding="utf-8"), "victim content changed")
        installed = [
            name
            for name in MIRRORED_REFERENCES
            if (self._mirror(repo) / name).is_symlink()
        ]
        self.assertEqual([], installed, "a symlink was installed as a pinned reference")

    def test_staging_entry_rebound_to_a_regular_file_is_not_swapped_into_place(
        self,
    ) -> None:
        """The inode identity check, isolated from the no-follow open.

        Re-binding the staging name to a *symlink* is already refused when the
        verification open uses `O_NOFOLLOW`. Re-binding it to an ordinary file
        opens fine, so only comparing the inode catches it — a mutation probe
        that removed the comparison stayed green until this case existed.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")
        impostor_body = "IMPOSTOR\n"

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED:
                for path in self._mirror(repo).iterdir():
                    if path.name.startswith(".mozyo-legacy-mirror."):
                        path.unlink()
                        path.write_text(impostor_body, encoding="utf-8")
                        break

        code, out, err = self._service(repo, progress_hook=hook).sync()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("aborted", err[0])
        for name in MIRRORED_REFERENCES:
            self.assertNotEqual(
                impostor_body,
                (self._mirror(repo) / name).read_text(encoding="utf-8"),
                "a substituted staging file was installed as a pinned reference",
            )

    def test_source_becoming_unreadable_after_the_walk_is_typed(self) -> None:
        """The observation branch inside the bound source directory.

        A mode-000 canonical *file* is still `lstat`-able, so it surfaces later
        in the read. Dropping the directory's permissions after the walk is what
        reaches the entry-level `OSError` branch — a probe that made it re-raise
        stayed green until this case existed.
        """
        repo = self._stage()
        source_dir = self._source(repo)
        self.addCleanup(os.chmod, source_dir, 0o755)

        service = self._service(repo)
        original = service._audit_source_entries

        def drop_permissions(source_fd: int):  # type: ignore[no-untyped-def]
            os.chmod(source_dir, 0o000)
            return original(source_fd)

        service._audit_source_entries = drop_permissions  # type: ignore[method-assign]
        code, out, err = service.check()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("Restore read access", "\n".join(err))

    def test_unreadable_canonical_directory_is_a_typed_violation(self) -> None:
        repo = self._stage()
        source_dir = self._source(repo)
        os.chmod(source_dir, 0o000)
        self.addCleanup(os.chmod, source_dir, 0o755)

        service = self._service(repo)
        code, out, err = service.check()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("Restore read access", "\n".join(err))
        self.assertEqual(1, service.sync()[0])

    def test_platform_without_the_required_primitives_fails_closed(self) -> None:
        """Contract: refuse rather than degrade to path-based I/O."""
        repo = self._stage()
        service = self._service(repo)
        with unittest.mock.patch.object(
            legacy_mirror_sync, "missing_platform_capabilities", return_value=("O_NOFOLLOW",)
        ):
            audit = service.audit()
            self.assertFalse(audit.ok)
            self.assertIn(PLATFORM_UNSUPPORTED, audit.kinds())
            self.assertTrue(audit.blocks_write)
            code, out, _ = service.sync()
            self.assertEqual(1, code)
            self.assertEqual((), out)

    # --- R7-F1: descriptor lifetime -----------------------------------------

    def _open_descriptor_count(self) -> int:
        return len(os.listdir("/dev/fd"))

    def test_abnormal_topology_does_not_leak_descriptors(self) -> None:
        """j#90450 R7-F1. Every early return in the component walk left the
        current directory fd open — `except BaseException` does not fire on a
        `return` — so auditing a tree with neither side present leaked two
        descriptors per call, 50 over 25 calls, on a path a release preflight
        can repeat.
        """
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        service = self._service(empty)

        service.audit()  # settle any first-call allocation
        before = self._open_descriptor_count()
        for _ in range(25):
            service.audit()
        self.assertEqual(before, self._open_descriptor_count(), "audit leaked descriptors")

    def test_repeated_sync_on_an_invalid_tree_does_not_leak_descriptors(self) -> None:
        repo = self._stage()
        (self._mirror(repo) / "unpinned.txt").write_text("x\n", encoding="utf-8")
        service = self._service(repo)

        service.sync()
        before = self._open_descriptor_count()
        for _ in range(25):
            service.sync()
        self.assertEqual(before, self._open_descriptor_count())

    def test_every_topology_failure_shape_is_descriptor_neutral(self) -> None:
        """Each early-return branch of the walk, not just the missing one."""
        shapes = ("missing", "not_directory", "symlink")
        for shape in shapes:
            with self.subTest(shape=shape):
                repo = self._stage()
                ancestor = repo / ".claude" / "skills"
                shutil.rmtree(ancestor)
                if shape == "not_directory":
                    ancestor.write_text("x\n", encoding="utf-8")
                elif shape == "symlink":
                    outside = repo / "outside"
                    outside.mkdir()
                    ancestor.symlink_to(outside, target_is_directory=True)

                service = self._service(repo)
                service.audit()
                before = self._open_descriptor_count()
                for _ in range(20):
                    service.audit()
                self.assertEqual(before, self._open_descriptor_count())

    # --- R7-F2: action-time type failures ------------------------------------

    def test_entry_swapped_to_a_fifo_after_the_type_audit_does_not_block(self) -> None:
        """j#90450 R7-F2. The leaf open validated *after* opening, and opening a
        FIFO for reading blocks until a writer appears — so an entry swapped to
        a FIFO right after rule E hung `check()` outright. `O_NONBLOCK` lets the
        open return so the `fstat` can reject it.
        """
        repo = self._stage()
        service = self._service(repo)
        original = service._audit_dest_entries

        def swap_to_fifo(mirror_fd: int):  # type: ignore[no-untyped-def]
            result = original(mirror_fd)
            entry = self._mirror(repo) / "safety.md"
            entry.unlink()
            os.mkfifo(entry)
            return result

        service._audit_dest_entries = swap_to_fifo  # type: ignore[method-assign]

        finished: list[object] = []

        def run() -> None:
            finished.append(service.check())

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive(), "check() blocked on a FIFO open")
        code, out, err = finished[0]  # type: ignore[misc]
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn(ENTRY_NOT_REGULAR, service.audit().kinds())

    def test_action_time_type_failure_advises_a_recovery_that_converges(self) -> None:
        """j#90450 R7-F2. Filing the late discovery under rule F left it out of
        the write-blocking set, so the advice said "resync" while the sync's own
        preflight refused the identical tree."""
        repo = self._stage()
        service = self._service(repo)
        original = service._audit_dest_entries

        def swap_to_directory(mirror_fd: int):  # type: ignore[no-untyped-def]
            result = original(mirror_fd)
            entry = self._mirror(repo) / "safety.md"
            entry.unlink()
            entry.mkdir()
            return result

        service._audit_dest_entries = swap_to_directory  # type: ignore[method-assign]
        audit = service.audit()
        self.assertTrue(audit.blocks_write)
        self.assertNotIn(RECOVERY_RESYNC, audit.recovery_actions())
        self.assertEqual(1, self._service(repo).sync()[0])

    def test_source_swapped_to_a_fifo_is_bounded_in_both_modes(self) -> None:
        """The same window on the canonical side."""
        for mode in ("check", "sync"):
            with self.subTest(mode=mode):
                repo = self._stage()
                entry = self._source(repo) / "safety.md"
                entry.unlink()
                os.mkfifo(entry)
                self.addCleanup(entry.unlink)

                service = self._service(repo)
                finished: list[object] = []

                def run() -> None:
                    finished.append(getattr(service, mode)())

                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                worker.join(timeout=20)
                self.assertFalse(worker.is_alive(), f"{mode}() blocked on a FIFO source")
                self.assertEqual(1, finished[0][0])  # type: ignore[index]

    # --- R7-F3: write-path errors are typed ----------------------------------

    def test_replace_onto_a_directory_is_typed_not_raised(self) -> None:
        """j#90450 R7-F3. Only the temp create converted `OSError`; the write,
        chmod, verify and replace did not, so a destination that became a
        directory after the preflight raised `IsADirectoryError` through the
        CLI and the release gate.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED:
                entry = self._mirror(repo) / "project-map.md"
                if entry.is_file():
                    entry.unlink()
                    entry.mkdir()

        code, out, err = self._service(repo, progress_hook=hook).sync()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("aborted", err[0])
        leftovers = [
            p.name
            for p in self._mirror(repo).iterdir()
            if p.name.startswith(".mozyo-legacy-mirror.")
        ]
        self.assertEqual([], leftovers, "staging file survived a failed replace")

    def test_payload_is_written_in_full_under_injected_short_writes(self) -> None:
        """j#90458 R8-F4. Writing a large regular file does not exercise this:
        this platform's `os.write` returns the full count, so reverting the loop
        to a single call passes. The short return has to be injected.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_bytes(b"B" * 100)

        real_write = os.write
        calls: list[int] = []

        def short_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            calls.append(len(data))
            return real_write(fd, bytes(data[:7]))

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", short_write):
            self.assertEqual(0, self._service(repo).sync()[0])

        self.assertGreater(len(calls), 1, "the write loop collapsed into one call")
        self.assertEqual(b"B" * 100, (self._mirror(repo) / "workflow.md").read_bytes())
        self.assertEqual(0, self._service(repo).check()[0])

    def test_a_write_that_never_progresses_is_bounded(self) -> None:
        """A zero-return write must fail, not spin."""
        repo = self._stage()
        (self._source(repo) / "workflow.md").write_bytes(b"C" * 100)

        outcome: list[object] = []

        def run() -> None:
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "write", lambda fd, data: 0
            ):
                outcome.append(self._service(repo).sync())

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "a stalled write span looped forever")
        code, out, err = outcome[0]  # type: ignore[misc]
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("[W/", "\n".join(err))

    # --- R8-F1/F2/F3: late types, teardown, and replace classification -------

    def test_late_type_swaps_all_carry_rule_e_weight(self) -> None:
        """j#90458 R8-F1. Every leaf-open failure collapsed to "unreadable", so
        a late symlink and a late socket were reported as rule F — non-blocking,
        advising "restore read access" — instead of the type failure they are.
        """
        for kind in ("symlink", "socket", "fifo", "directory"):
            with self.subTest(kind=kind):
                # The socket case needs a short path (see `_stage`).
                repo = self._stage(base="/tmp" if kind == "socket" else None)
                external = repo / "external.md"
                shutil.copy(self._mirror(repo) / "safety.md", external)

                service = self._service(repo)
                original = service._audit_dest_entries

                def swap(mirror_fd: int, kind=kind, repo=repo, external=external):  # type: ignore[no-untyped-def]
                    result = original(mirror_fd)
                    entry = self._mirror(repo) / "safety.md"
                    entry.unlink()
                    if kind == "symlink":
                        entry.symlink_to(external)
                    elif kind == "socket":
                        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            sock.bind(str(entry))
                        finally:
                            sock.close()
                        assert stat.S_ISSOCK(os.lstat(entry).st_mode)
                    elif kind == "fifo":
                        os.mkfifo(entry)
                    else:
                        entry.mkdir()
                    return result

                service._audit_dest_entries = swap  # type: ignore[method-assign]
                audit = service.audit()
                self.assertTrue(audit.blocks_write, f"late {kind} did not block the write")
                self.assertEqual((RECOVERY_REPLACE_ENTRY,), audit.recovery_actions())

    def test_close_failure_does_not_escape_either_mode(self) -> None:
        """j#90458 R8-F2. `os.close` was uncaught everywhere, so a failing close
        became a traceback in the CLI and the release gate."""
        real_close = os.close

        def failing_close(fd: int) -> None:
            real_close(fd)
            raise OSError(errno.EIO, "injected close failure")

        for mode in ("check", "sync"):
            with self.subTest(mode=mode):
                repo = self._stage()
                service = self._service(repo)
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "close", failing_close
                ):
                    code, out, _err = getattr(service, mode)()
                self.assertEqual(1, code)
                self.assertEqual((), out)

    def test_cleanup_failure_is_reported_with_the_primary_failure(self) -> None:
        """j#90458 R8-F2. The staging unlink failure was swallowed, so residue
        stayed on disk unmentioned and the next run refused it as an unpinned
        entry — neither message described the real state.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def failing_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise OSError(errno.ENOSPC, "injected")

        def failing_unlink(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise PermissionError(errno.EACCES, "injected")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", failing_write):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "unlink", failing_unlink
            ):
                code, out, err = self._service(repo).sync()

        self.assertEqual(1, code)
        self.assertEqual((), out)
        report = "\n".join(err)
        self.assertIn(WRITE_FAILED, report, "the primary failure was lost")
        self.assertIn(CLEANUP_FAILED, report, "surviving residue went unreported")
        self.assertIn("still present", report)

    def _fail_only_the_staging_close(self):  # type: ignore[no-untyped-def]
        """Patch pair that fails the close of the staging fd and nothing else.

        Failing *every* close stops at the preflight read and never reaches the
        staging branch, which is why the earlier close test passed while the
        staging path still reported success (j#90467 R9-F1).
        """
        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"fd": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def selective_close(fd: int) -> None:
            real_close(fd)
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                state["fd"] = None
                raise OSError(errno.EIO, "injected staging close failure")

        return tracking_open, selective_close, state

    def test_staging_close_failure_is_not_reported_as_success(self) -> None:
        """j#90467 R9-F1. `_close_quietly`'s result was discarded, so a close
        that reported a deferred write error still produced exit 0 and the
        `synced` banner, with the post-check agreeing."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        tracking_open, selective_close, state = self._fail_only_the_staging_close()
        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", selective_close
            ):
                code, out, err = self._service(repo).sync()

        self.assertTrue(state["fired"], "the staging close was never reached")
        self.assertEqual(1, code)
        self.assertEqual((), out, "a failed staging close still printed the banner")
        self.assertIn(WRITE_FAILED, "\n".join(err))

    def test_cleanup_leaves_a_foreign_entry_at_the_staging_name(self) -> None:
        """j#90467 R9-F2. Cleanup unlinked by name with no ownership check, so
        an ordinary file substituted at the staging name during the write was
        deleted — the same invariant the verify branch already honoured."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_write = os.write
        state: dict[str, object] = {"done": False, "name": None}

        def rebinding_write(fd: int, data):  # type: ignore[no-untyped-def]
            if not state["done"]:
                state["done"] = True
                for path in self._mirror(repo).iterdir():
                    if path.name.startswith(".mozyo-legacy-mirror."):
                        path.unlink()
                        path.write_text("FOREIGN\n", encoding="utf-8")
                        state["name"] = path.name
                        break
                raise OSError(errno.ENOSPC, "injected")
            return real_write(fd, data)

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", rebinding_write):
            code, _out, err = self._service(repo).sync()

        self.assertEqual(1, code)
        foreign = self._mirror(repo) / str(state["name"])
        self.assertTrue(foreign.exists(), "cleanup deleted an entry that was not ours")
        self.assertEqual("FOREIGN\n", foreign.read_text(encoding="utf-8"))
        self.assertIn("left untouched", "\n".join(err))

    def test_a_transient_cleanup_failure_is_not_reported_as_surviving_residue(
        self,
    ) -> None:
        """j#90467 R9-F3. An inline cleanup plus the outer `finally` ran twice:
        the first unlink failed, the second succeeded, and the report still
        claimed residue was "still present" when the directory was empty."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_unlink = os.unlink
        calls: list[int] = []

        def transient_unlink(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(errno.EACCES, "injected")
            return real_unlink(*args, **kwargs)

        def failing_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise OSError(errno.ENOSPC, "injected")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", failing_write):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "unlink", transient_unlink
            ):
                code, _out, err = self._service(repo).sync()

        self.assertEqual(1, code)
        self.assertEqual(1, len(calls), "cleanup ran more than once for one staging file")
        residue = [
            p.name
            for p in self._mirror(repo).iterdir()
            if p.name.startswith(".mozyo-legacy-mirror.")
        ]
        claims_present = "still present" in "\n".join(err)
        self.assertEqual(
            bool(residue),
            claims_present,
            "the diagnostic disagrees with the filesystem about surviving residue",
        )

    def test_entry_deleted_between_observation_and_read_is_missing_not_unreadable(
        self,
    ) -> None:
        """j#90467 R9-F4. `FileNotFoundError` was folded into "unreadable", so
        an entry that had simply been deleted advised restoring access to a
        file that no longer exists, and did not reach the resync recovery."""
        repo = self._stage()
        real_unlink = os.unlink
        service = self._service(repo)
        original = service._read_bound
        seen: list[str] = []

        def unlink_before_the_mirror_read(dir_fd: int, name: str):  # type: ignore[no-untyped-def]
            if name == "safety.md":
                seen.append(name)
                if len(seen) == 2:  # first call is the source, second the mirror
                    try:
                        real_unlink(name, dir_fd=dir_fd)
                    except OSError:
                        pass
            return original(dir_fd, name)

        service._read_bound = unlink_before_the_mirror_read  # type: ignore[method-assign]
        audit = service.audit()
        self.assertGreaterEqual(len(seen), 2, "the mirror read was never reached")
        self.assertIn(ENTRY_MISSING, audit.kinds())
        self.assertNotIn(ENTRY_UNREADABLE, audit.kinds())
        self.assertIn(RECOVERY_RESYNC, audit.recovery_actions())

    def test_replace_failure_is_classified_by_what_actually_happened(self) -> None:
        """j#90458 R8-F3. Every `os.replace` error was reported as "it is no
        longer a regular file" — an untrue statement for a permission failure,
        pointing at the wrong recovery.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def failing_replace(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise PermissionError(errno.EACCES, "injected")

        with unittest.mock.patch.object(
            legacy_mirror_sync.os, "replace", failing_replace
        ):
            code, out, err = self._service(repo).sync()

        self.assertEqual(1, code)
        self.assertEqual((), out)
        report = "\n".join(err)
        self.assertIn(WRITE_FAILED, report)
        self.assertNotIn("no longer a regular file", report)
        self.assertIn("check write permission", report)

    def test_replace_onto_a_changed_type_still_says_so(self) -> None:
        """The converse: don't over-correct into never naming a type change."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED:
                entry = self._mirror(repo) / "project-map.md"
                if entry.is_file():
                    entry.unlink()
                    entry.mkdir()

        code, _out, err = self._service(repo, progress_hook=hook).sync()
        self.assertEqual(1, code)
        self.assertIn("no longer a regular file", "\n".join(err))

    # --- R7-F4: the capability manifest is the call surface -------------------

    def test_each_required_capability_individually_fails_closed(self) -> None:
        """j#90450 R7-F4. The manifest probed `os.stat`, which nothing calls,
        and omitted `os.lstat(dir_fd=)`, which every type decision uses — so a
        host missing it passed the preflight and then raised
        `NotImplementedError` past the fail-closed path.
        """
        repo = self._stage()
        required = [function for _label, function in legacy_mirror_sync._REQUIRED_DIR_FD_CALLS]
        self.assertIn(os.lstat, required, "lstat(dir_fd=) is not in the manifest")

        for function in required:
            with self.subTest(capability=getattr(function, "__name__", function)):
                reduced = frozenset(os.supports_dir_fd) - {function}
                with unittest.mock.patch.object(os, "supports_dir_fd", reduced):
                    self.assertIn(
                        getattr(function, "__name__", ""),
                        " ".join(legacy_mirror_sync.missing_platform_capabilities()),
                    )
                    service = self._service(repo)
                    audit = service.audit()
                    self.assertIn(PLATFORM_UNSUPPORTED, audit.kinds())
                    self.assertTrue(audit.blocks_write)
                    code, out, _ = service.sync()
                    self.assertEqual(1, code)
                    self.assertEqual((), out)

    def test_capability_manifest_covers_the_primitives_the_module_calls(self) -> None:
        """Guard the manifest against the module drifting away from it."""
        source = Path(legacy_mirror_sync.__file__).read_text(encoding="utf-8")
        # Scan the WHOLE module: restricting it to the class body meant a call
        # moved to a module-level helper escaped the fence while still being a
        # platform-dependent primitive (j#90458 R8-F4).
        body = source
        listed = {
            getattr(function, "__name__", "")
            for _label, function in legacy_mirror_sync._REQUIRED_DIR_FD_CALLS
        }
        for call, name in (
            ("os.lstat(", "lstat"),
            ("os.open(", "open"),
            ("os.unlink(", "unlink"),
            ("os.mkdir(", "mkdir"),
        ):
            if call in body:
                self.assertIn(name, listed, f"{name} is called but not in the manifest")
        if "os.replace(" in body:
            self.assertIn("rename", listed, "replace is called; rename must be probed")

    # --- R6-F3: unreadable state is typed, not an exception ------------------

    def test_unreadable_canonical_reference_is_a_typed_violation(self) -> None:
        """j#90418 R6-F3. A mode-000 canonical file raised `PermissionError`
        out of `check()`, so the gate above printed a traceback and its
        "follow the sub-check's disposition" advice pointed at nothing."""
        repo = self._stage()
        target = self._source(repo) / "safety.md"
        os.chmod(target, 0o000)
        self.addCleanup(os.chmod, target, 0o644)

        service = self._service(repo)
        code, out, err = service.check()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn(SOURCE_UNREADABLE, service.audit().kinds())
        report = "\n".join(err)
        self.assertIn("Restore read access", report)
        self.assertNotIn("Rerun 'scripts/sync_legacy_project_skill.sh'", report)
        self.assertEqual(1, service.sync()[0])

    def test_unreadable_mirror_directory_is_a_typed_violation(self) -> None:
        repo = self._stage()
        mirror = self._mirror(repo)
        os.chmod(mirror, 0o000)
        self.addCleanup(os.chmod, mirror, 0o755)

        service = self._service(repo)
        code, out, err = service.check()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("Restore read access", "\n".join(err))
        self.assertEqual(1, service.sync()[0], "an unobservable mirror must not be written")

    def test_diagnostics_carry_no_host_absolute_paths(self) -> None:
        """Subjects stay repo-relative so the report is a stable contract."""
        repo = self._stage()
        (self._mirror(repo) / "unpinned.txt").write_text("x\n", encoding="utf-8")
        for violation in self._service(repo).audit().violations:
            self.assertNotIn(str(repo), violation.subject)
            self.assertNotIn(str(repo), violation.note)

    # --- action-time recheck ------------------------------------------------

    def test_source_swapped_after_preflight_is_fail_closed(self) -> None:
        """Contract 6: a source that becomes an alias between preflight and
        write must abort rather than mirror whatever it now points at."""
        repo = self._stage()
        external = repo / "external-body.md"
        external.write_text("EXTERNAL BODY\n", encoding="utf-8")

        swapped = {"done": False}

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED and not swapped["done"]:
                swapped["done"] = True
                target = self._source(repo) / "workflow.md"
                target.unlink()
                target.symlink_to(external)

        code, out, err = self._service(repo, progress_hook=hook).sync()
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("aborted", err[0])
        mirrored = self._mirror(repo) / "workflow.md"
        self.assertNotIn("EXTERNAL BODY", mirrored.read_text(encoding="utf-8"))


class LegacyMirrorWrapperCliTest(_MirrorTreeFixture):
    """The `scripts/` wrapper: operator-facing contract, black-box.

    The wrapper carries no mirror logic, so these pin only what it exists to
    preserve — the invocation contract `release check drift`, the docs and
    operators depend on.
    """

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

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

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

    def test_check_and_sync_round_trip(self) -> None:
        repo = self._stage_with_wrapper()
        self.assertEqual(0, self._run(repo, "--check").returncode)
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nEDIT\n", encoding="utf-8")
        self.assertEqual(1, self._run(repo, "--check").returncode)
        synced = self._run(repo)
        self.assertEqual(0, synced.returncode, msg=synced.stderr)
        self.assertIn("synced legacy project skill mirror", synced.stdout)
        self.assertEqual(0, self._run(repo, "--check").returncode)

    def test_check_reports_a_violation_and_writes_nothing(self) -> None:
        repo = self._stage_with_wrapper()
        (self._mirror(repo) / "unpinned.txt").write_text("smuggled\n", encoding="utf-8")
        result = self._run(repo, "--check")
        self.assertEqual(1, result.returncode)
        self.assertNotIn("is up to date", result.stdout)
        self.assertIn("unpinned_entry", result.stderr)
        self.assertTrue((self._mirror(repo) / "unpinned.txt").exists())

    def test_help_exits_zero(self) -> None:
        repo = self._stage_with_wrapper()
        result = self._run(repo, "--help")
        self.assertEqual(0, result.returncode)
        self.assertIn("--check", result.stdout)

    def test_unknown_argument_exits_64(self) -> None:
        repo = self._stage_with_wrapper()
        result = self._run(repo, "--force")
        self.assertEqual(64, result.returncode)
        self.assertIn("unknown argument", result.stderr)

    def test_repo_cannot_be_redirected_by_operator_argv(self) -> None:
        """j#90418 R6-F2. The wrapper passed `--repo <own root>` and then
        appended `"$@"`, and the parser took the last value — so an operator
        could audit, and in default mode write, a different checkout entirely.
        """
        tree_a = self._stage_with_wrapper()
        tree_b = self._stage_with_wrapper()
        smuggled = self._mirror(tree_b) / "unpinned.txt"
        smuggled.write_text("smuggled\n", encoding="utf-8")

        for args in (["--check", "--repo", str(tree_b)], ["--repo", str(tree_b)]):
            with self.subTest(args=args):
                result = self._run(tree_a, *args)
                self.assertEqual(64, result.returncode)
                self.assertIn("unknown argument: --repo", result.stderr)
                self.assertNotIn("unpinned.txt", result.stdout + result.stderr)

        self.assertTrue(smuggled.exists(), "tree B was modified from tree A")
        self.assertEqual(0, self._run(tree_a, "--check").returncode)

    def test_repo_env_is_overwritten_by_the_wrapper(self) -> None:
        """The internal channel must not be hijackable from the environment."""
        tree_a = self._stage_with_wrapper()
        tree_b = self._stage_with_wrapper()
        (self._mirror(tree_b) / "unpinned.txt").write_text("smuggled\n", encoding="utf-8")

        env = dict(os.environ)
        env["MOZYO_LEGACY_MIRROR_REPO_ROOT"] = str(tree_b)
        result = subprocess.run(
            ["sh", str(tree_a / "scripts" / SYNC_SCRIPT_PATH.name), "--check"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertNotIn("unpinned.txt", result.stdout + result.stderr)

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

    def test_wrapper_targets_its_own_repo_not_the_cwd(self) -> None:
        """`release check drift` runs the staged tree's wrapper; it must check
        that tree, not whichever repo the process happens to sit in."""
        repo = self._stage_with_wrapper()
        (self._mirror(repo) / "unpinned.txt").write_text("smuggled\n", encoding="utf-8")
        result = subprocess.run(
            ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), "--check"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("unpinned.txt", result.stderr)


if __name__ == "__main__":
    unittest.main()
