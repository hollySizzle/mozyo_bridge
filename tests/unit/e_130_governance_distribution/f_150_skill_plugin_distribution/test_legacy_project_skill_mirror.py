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

import contextlib
import errno
import hashlib
import inspect
import os
import pickle
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
    owned_descriptors,
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

    def _staging_names(self, repo: Path) -> list[str]:
        return [
            p.name
            for p in self._mirror(repo).iterdir()
            if p.name.startswith(".mozyo-legacy-mirror.")
        ]

    def test_a_non_oserror_unwinding_the_write_still_releases_the_staging(self) -> None:
        """j#90472 R10-F1. The write span typed only `OSError`, so any other
        exception reached neither the hook nor the verify safety net and left
        this run's staging entry behind for the next audit to stop on."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def exploding_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise RuntimeError("injected non-OSError")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", exploding_write):
            with self.assertRaises(RuntimeError):
                self._service(repo).sync()

        self.assertEqual([], self._staging_names(repo), "the staging entry survived")

    def test_a_non_oserror_unwind_still_spares_a_foreign_entry(self) -> None:
        """The release on that path must keep proving ownership."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_write = os.write
        state: dict[str, object] = {"done": False, "name": None}

        def rebinding_then_raising(fd: int, data):  # type: ignore[no-untyped-def]
            if not state["done"]:
                state["done"] = True
                for path in self._mirror(repo).iterdir():
                    if path.name.startswith(".mozyo-legacy-mirror."):
                        path.unlink()
                        path.write_text("FOREIGN\n", encoding="utf-8")
                        state["name"] = path.name
                        break
                raise RuntimeError("injected non-OSError")
            return real_write(fd, data)

        with unittest.mock.patch.object(
            legacy_mirror_sync.os, "write", rebinding_then_raising
        ):
            with self.assertRaises(RuntimeError):
                self._service(repo).sync()

        foreign = self._mirror(repo) / str(state["name"])
        self.assertTrue(foreign.exists(), "the unwind deleted an entry that was not ours")
        self.assertEqual("FOREIGN\n", foreign.read_text(encoding="utf-8"))

    def test_verify_open_failure_releases_the_staging(self) -> None:
        """j#90472 R10-F2. Failing to observe the entry is not evidence that it
        is foreign; skipping cleanup guaranteed residue instead."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open = os.open

        def failing_verify_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            if (
                isinstance(path, str)
                and path.startswith(".mozyo-legacy-mirror.")
                and not flags & os.O_CREAT
            ):
                raise OSError(errno.EMFILE, "injected")
            return real_open(path, flags, *args, **kwargs)

        with unittest.mock.patch.object(
            legacy_mirror_sync.os, "open", failing_verify_open
        ):
            code, out, _err = self._service(repo).sync()

        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertEqual([], self._staging_names(repo), "verify failure left residue")

    def test_verify_fstat_failure_releases_the_staging(self) -> None:
        """The sibling branch of the same window."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open, real_fstat = os.open, os.fstat
        verify_fds: set[int] = set()

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if (
                isinstance(path, str)
                and path.startswith(".mozyo-legacy-mirror.")
                and not flags & os.O_CREAT
            ):
                verify_fds.add(fd)
            return fd

        def failing_verify_fstat(fd: int):  # type: ignore[no-untyped-def]
            if fd in verify_fds:
                raise OSError(errno.EIO, "injected")
            return real_fstat(fd)

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "fstat", failing_verify_fstat
            ):
                code, out, _err = self._service(repo).sync()

        self.assertTrue(verify_fds, "the verify open was never reached")
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertEqual([], self._staging_names(repo), "verify failure left residue")

    def test_a_close_that_unwinds_still_releases_the_staging(self) -> None:
        """j#90477 R11-F1. `_close_quietly` re-raises anything that is not an
        `OSError` so an interrupt is not swallowed — which means the close is
        itself an unwind source. It reached neither the release rail nor the
        sentinel, leaving this run's staging entry behind.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"fd": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def unwinding_close(fd: int) -> None:
            real_close(fd)
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                raise RuntimeError("injected close unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", unwinding_close
            ):
                with self.assertRaises(RuntimeError):
                    self._service(repo).sync()

        self.assertTrue(state["fired"], "the staging close was never reached")
        self.assertEqual([], self._staging_names(repo), "the staging entry survived")

    def test_a_close_unwind_never_closes_a_reused_descriptor_number(self) -> None:
        """j#90477 R11-F1, the damaging half.

        Setting the ownership sentinel *after* the close meant a raising close
        left the number owned, and a later `finally` closed it again. Descriptor
        numbers are reused immediately, so the second close hit an unrelated
        handle — measured closing a `/dev/null` descriptor that had just been
        assigned the freed number. Failing every close cannot detect this; the
        number has to actually be reused.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"fd": None, "reused": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def reusing_close(fd: int) -> None:
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                real_close(fd)
                # Grab the freed number before anyone else can.
                state["reused"] = real_open(os.devnull, os.O_RDONLY)
                raise RuntimeError("injected close unwind")
            real_close(fd)

        def unwinding_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise RuntimeError("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", reusing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", unwinding_write
                ):
                    with self.assertRaises(RuntimeError):
                        self._service(repo).sync()

        reused = state["reused"]
        self.assertIsNotNone(reused, "the close injection never fired")
        self.assertEqual(
            state["fd"], reused, "the number was not reused; the case is not exercised"
        )
        try:
            os.fstat(int(reused))  # type: ignore[arg-type]
        except OSError as exc:
            self.fail(f"the sync closed a descriptor it did not own (errno {exc.errno})")
        real_close(int(reused))  # type: ignore[arg-type]

        self.assertEqual([], self._staging_names(repo), "the staging entry survived")

    def test_a_close_unwind_keeps_the_primary_exception(self) -> None:
        """The caller must still see what actually unwound the write."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"fd": None, "fired": False}

        class PrimaryFailure(Exception):
            pass

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def unwinding_close(fd: int) -> None:
            real_close(fd)
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                raise RuntimeError("injected close unwind")

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryFailure("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", unwinding_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", primary_write
                ):
                    with self.assertRaises(PrimaryFailure) as caught:
                        self._service(repo).sync()

        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("secondary failure during teardown" in note for note in notes),
            "the close failure was dropped instead of being recorded",
        )
        self.assertEqual([], self._staging_names(repo))

    def test_the_directory_walk_never_closes_a_reused_descriptor_number(self) -> None:
        """j#90482 R12-F1. The same defect R11-F1 fixed for the staging
        descriptor still lived in the component walk: `_close_quietly(parent)`
        unwinding meant `parent = child` was never reached, so the `finally`
        closed the freed number again — measured closing a `/dev/null` handle
        that had taken it.
        """
        repo = self._stage()
        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"root": None, "reused": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if state["root"] is None and "dir_fd" not in kwargs:
                state["root"] = fd
            return fd

        def reusing_close(fd: int) -> None:
            if fd == state["root"] and not state["fired"]:
                state["fired"] = True
                real_close(fd)
                state["reused"] = real_open(os.devnull, os.O_RDONLY)
                raise RuntimeError("injected walk close unwind")
            real_close(fd)

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", reusing_close
            ):
                with self.assertRaises(RuntimeError):
                    self._service(repo).audit()

        reused = state["reused"]
        self.assertIsNotNone(reused, "the walk close injection never fired")
        self.assertEqual(
            state["root"], reused, "the number was not reused; the case is not exercised"
        )
        try:
            os.fstat(int(reused))  # type: ignore[arg-type]
        except OSError as exc:
            self.fail(f"the walk closed a descriptor it did not own (errno {exc.errno})")
        real_close(int(reused))  # type: ignore[arg-type]

    def test_a_walk_close_that_unwinds_leaks_no_descriptor(self) -> None:
        """The other half of the walk's ownership transfer.

        Detaching inside `close()` already prevents a double close, so a probe
        that only checks "no foreign descriptor was closed" passes even when the
        transfer is reordered. Closing the previous descriptor *before* handing
        ownership to the child instead leaks the child: the loop variable never
        takes it, so the `finally` has nothing to close. Measured at ten leaked
        descriptors over ten runs.
        """
        repo = self._stage()
        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"root": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if state["root"] is None and "dir_fd" not in kwargs:
                state["root"] = fd
            return fd

        def unwinding_close(fd: int) -> None:
            real_close(fd)
            if fd == state["root"] and not state["fired"]:
                state["fired"] = True
                raise RuntimeError("injected walk close unwind")

        def one_run() -> None:
            state["root"] = None
            state["fired"] = False
            with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "close", unwinding_close
                ):
                    try:
                        self._service(repo).audit()
                    except RuntimeError:
                        pass
            self.assertTrue(state["fired"], "the walk close injection never fired")

        one_run()  # settle any first-call allocation
        before = self._open_descriptor_count()
        for _ in range(10):
            one_run()
        self.assertEqual(
            before, self._open_descriptor_count(), "the walk leaked descriptors"
        )

    def test_a_failing_add_note_does_not_replace_the_primary(self) -> None:
        """j#90482 R12-F2. Recording the secondary must never become the reason
        the caller sees a different exception — a raising `add_note` replaced
        the primary *and* skipped the release entirely."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        class PrimaryFailure(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise RuntimeError("injected add_note failure")

        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"fd": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def unwinding_close(fd: int) -> None:
            real_close(fd)
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                raise RuntimeError("injected close unwind")

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryFailure("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", unwinding_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", primary_write
                ):
                    with self.assertRaises(PrimaryFailure):
                        self._service(repo).sync()

        self.assertTrue(state["fired"], "the close injection never fired")
        self.assertEqual([], self._staging_names(repo), "the release was skipped")

    def test_a_failing_cleanup_does_not_replace_the_primary(self) -> None:
        """j#90482 R12-F2. Chaining close and release meant one failing skipped
        the other; each is attempted independently now, and the primary
        survives with the cleanup failure recorded as secondary."""
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        class PrimaryFailure(Exception):
            pass

        service = self._service(repo)

        def exploding_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            raise RuntimeError("injected cleanup failure")

        service._release_staging = exploding_release  # type: ignore[method-assign]

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryFailure("injected write unwind")

        with unittest.mock.patch.object(
            legacy_mirror_sync.os, "write", primary_write
        ):
            with self.assertRaises(PrimaryFailure) as caught:
                service.sync()

        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("secondary failure during teardown" in note for note in notes),
            "the cleanup failure was dropped instead of being recorded",
        )

    def _fail_staging_close_with(self, error: BaseException):  # type: ignore[no-untyped-def]
        """Patch pair failing only the staging close, with a chosen exception."""
        real_open, real_close = os.open, os.close
        state: dict[str, object] = {"fd": None, "fired": False}

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                state["fd"] = fd
            return fd

        def failing_close(fd: int) -> None:
            real_close(fd)
            if fd == state["fd"] and not state["fired"]:
                state["fired"] = True
                raise error

        return tracking_open, failing_close, state

    def test_a_close_primary_survives_a_raising_release(self) -> None:
        """j#90487 R13-F1. When the close is itself the primary, the release ran
        bare — a raising release replaced it and left residue. Both must be
        independent, with the first ordinary primary surviving."""

        class PrimaryClose(Exception):
            pass

        class SecondaryCleanup(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)

        def exploding_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            raise SecondaryCleanup("injected cleanup failure")

        service._release_staging = exploding_release  # type: ignore[method-assign]
        tracking_open, failing_close, state = self._fail_staging_close_with(
            PrimaryClose("injected close primary")
        )

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", failing_close
            ):
                with self.assertRaises(PrimaryClose) as caught:
                    service.sync()

        self.assertTrue(state["fired"], "the staging close injection never fired")
        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("SecondaryCleanup" in note for note in notes),
            "the cleanup failure was dropped",
        )

    def test_the_walk_keeps_the_first_close_failure(self) -> None:
        """j#90487 R13-F1. In the walk, a previous-close primary was overwritten
        by the `finally`'s current-close secondary."""

        class PreviousClose(Exception):
            pass

        class CurrentClose(Exception):
            pass

        repo = self._stage()
        real_close = os.close
        order: list[int] = []

        def failing_close(fd: int) -> None:
            real_close(fd)
            order.append(fd)
            if len(order) == 1:
                raise PreviousClose("first")
            if len(order) == 2:
                raise CurrentClose("second")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "close", failing_close):
            with self.assertRaises(PreviousClose) as caught:
                self._service(repo).audit()

        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("CurrentClose" in note for note in notes),
            "the second close failure was dropped",
        )

    def test_a_typed_cleanup_failure_is_recorded_not_discarded(self) -> None:
        """j#90487 R13-F2. Teardown actions report failure by *return value* as
        well as by raising: the release returns a violation tuple for a cleanup
        `OSError`. Discarding it left the primary with no notes while residue
        stayed on disk."""

        class PrimaryWrite(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryWrite("injected write unwind")

        def failing_unlink(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise PermissionError(errno.EACCES, "injected")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", primary_write):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "unlink", failing_unlink
            ):
                with self.assertRaises(PrimaryWrite) as caught:
                    self._service(repo).sync()

        notes = "\n".join(getattr(caught.exception, "__notes__", []))
        self.assertIn(CLEANUP_FAILED, notes, "the typed cleanup failure was discarded")
        self.assertNotEqual(
            [], self._staging_names(repo), "the fixture did not actually leave residue"
        )

    def test_a_typed_close_failure_is_recorded_not_discarded(self) -> None:
        """The other returned-failure channel: `close()` returns False."""

        class PrimaryWrite(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        tracking_open, failing_close, state = self._fail_staging_close_with(
            OSError(errno.EIO, "injected typed close failure")
        )

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryWrite("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", failing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", primary_write
                ):
                    with self.assertRaises(PrimaryWrite) as caught:
                        self._service(repo).sync()

        self.assertTrue(state["fired"], "the typed close injection never fired")
        notes = "\n".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("close reported a failure", notes)

    def test_an_interrupt_during_teardown_outranks_the_primary(self) -> None:
        """j#90487 R13-F3. `_teardown_during` caught `BaseException`, so a
        `KeyboardInterrupt` arriving during cleanup was demoted to a note on an
        ordinary exception — contradicting the descriptor helper's stated
        promise never to swallow an interrupt."""

        class PrimaryWrite(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)

        def interrupted_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt()

        service._release_staging = interrupted_release  # type: ignore[method-assign]

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryWrite("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "write", primary_write):
            with self.assertRaises(KeyboardInterrupt):
                service.sync()

    def test_an_interrupt_while_recording_still_releases_the_staging_entry(self) -> None:
        """j#90492 R14-F1. Recording a secondary happened outside the teardown
        rail, and `_attach_secondary` deliberately lets control flow through —
        so a `KeyboardInterrupt` arriving during `add_note` escaped
        `_teardown_during` and the release never ran. Measured before the fix:
        actions `write / close / note`, no release, one staging entry left.

        Three things have to hold together, which is why they are one test: the
        interrupt surfaces, the release runs exactly once, and no residue stays.
        """

        class PrimaryWrite(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise KeyboardInterrupt("interrupt while recording")

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)
        real_release = service._release_staging
        calls: list[int] = []

        def counting_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            calls.append(1)
            return real_release(mirror_fd, temp_name, identity)

        service._release_staging = counting_release  # type: ignore[method-assign]
        tracking_open, failing_close, state = self._fail_staging_close_with(
            RuntimeError("injected ordinary close failure")
        )

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryWrite("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", failing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", primary_write
                ):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        service.sync()

        self.assertTrue(state["fired"], "the close injection never fired")
        self.assertIsInstance(
            caught.exception.__context__,
            PrimaryWrite,
            "the interrupt surfaced without the primary behind it",
        )
        self.assertEqual(1, len(calls), "the release did not run exactly once")
        self.assertEqual([], self._staging_names(repo), "the staging entry was left behind")

    def test_a_later_control_flow_failure_is_recorded_not_dropped(self) -> None:
        """j#90492 R14-F2. Only the first control-flow exception was kept; a
        second one left no trace in notes or context, while the returned and
        ordinary channels both record every failure."""

        class PrimaryWrite(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)

        def exiting_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            raise SystemExit("injected second control flow")

        service._release_staging = exiting_release  # type: ignore[method-assign]
        tracking_open, failing_close, state = self._fail_staging_close_with(
            KeyboardInterrupt("injected first control flow")
        )

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryWrite("injected write unwind")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", failing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", primary_write
                ):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        service.sync()

        self.assertTrue(state["fired"], "the close injection never fired")
        primary = caught.exception.__context__
        self.assertIsInstance(primary, PrimaryWrite)
        notes = "\n".join(getattr(primary, "__notes__", []))
        self.assertIn("SystemExit", notes, "the second control-flow failure was dropped")

    def test_teardown_continues_when_recording_a_secondary_is_interrupted(self) -> None:
        """The rail's own property, stated directly: whichever step fails — the
        action, or the *recording* of what it reported — every remaining action
        still runs (j#90492 R14-F1)."""

        class Primary(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise KeyboardInterrupt("interrupt while recording")

        ran: list[str] = []

        def failing() -> None:
            ran.append("failing")
            raise RuntimeError("ordinary teardown failure")

        def second() -> None:
            ran.append("second")

        def third() -> None:
            ran.append("third")

        primary = Primary("write failed")
        control = owned_descriptors._teardown_during(primary, failing, second, third)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual(["failing", "second", "third"], ran)

    def test_control_flow_priority_keeps_the_first_and_records_the_rest(self) -> None:
        """j#90492 R14-F2, stated directly: the first control-flow exception is
        the one the caller raises, later ones land on the primary's ledger, and
        neither decision may cost a remaining action."""

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            raise KeyboardInterrupt("first")

        def second() -> None:
            ran.append("second")
            raise SystemExit("second")

        def third() -> None:
            ran.append("third")

        primary = Exception("write failed")
        control = owned_descriptors._teardown_during(primary, first, second, third)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual("first", str(control))
        self.assertEqual(["first", "second", "third"], ran)
        notes = "\n".join(getattr(primary, "__notes__", []))
        self.assertIn("SystemExit", notes, "the second control-flow failure was dropped")

    def test_a_broken_note_still_leaves_the_cleanup_failure_reachable(self) -> None:
        """j#90503 R15-F1. Making `add_note` the ledger meant that an interrupt
        during recording lost the failure *being recorded*, not just the
        interrupt: the release ran, reported `CLEANUP_FAILED`, left residue —
        and nothing reachable from the exception said so.

        The composite is the point. Interrupt priority, one release, residue on
        disk, and the typed cleanup failure reachable are one property, not
        four; fixing any of them alone is what the last three rounds did.
        """

        class PrimaryWrite(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise KeyboardInterrupt("interrupt while recording")

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)
        real_release = service._release_staging
        calls: list[int] = []

        def counting_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            calls.append(1)
            return real_release(mirror_fd, temp_name, identity)

        service._release_staging = counting_release  # type: ignore[method-assign]
        tracking_open, failing_close, state = self._fail_staging_close_with(
            RuntimeError("injected ordinary close failure")
        )

        def primary_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise PrimaryWrite("injected write unwind")

        def failing_unlink(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise PermissionError(errno.EACCES, "injected")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", failing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", primary_write
                ):
                    with unittest.mock.patch.object(
                        legacy_mirror_sync.os, "unlink", failing_unlink
                    ):
                        with self.assertRaises(KeyboardInterrupt) as caught:
                            service.sync()

        self.assertTrue(state["fired"], "the close injection never fired")
        primary = caught.exception.__context__
        self.assertIsInstance(primary, PrimaryWrite)
        self.assertEqual(1, len(calls), "the release did not run exactly once")
        self.assertNotEqual(
            [], self._staging_names(repo), "the fixture did not actually leave residue"
        )
        self.assertEqual([], getattr(primary, "__notes__", []), "the fixture must break notes")

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertTrue(
            any(
                isinstance(entry, tuple)
                and any(getattr(violation, "kind", None) == CLEANUP_FAILED for violation in entry)
                for entry in ledger
            ),
            "the typed cleanup failure was unreachable while its residue stayed on disk",
        )
        self.assertTrue(
            any(isinstance(entry, RuntimeError) for entry in ledger),
            "the close failure was lost",
        )

    def test_a_secondary_that_cannot_be_stringified_is_still_retained(self) -> None:
        """j#90503 R15-F2. `_attach_secondary` swallows an ordinary exception as
        best effort, so a secondary whose `__str__` raised was reported as
        recorded and then dropped — no special interrupt needed."""

        class UnprintableFailure(Exception):
            def __str__(self) -> str:
                raise RuntimeError("this failure cannot be stringified")

        class UnprintableExit(SystemExit):
            def __str__(self) -> str:
                raise RuntimeError("nor can this one")

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            raise KeyboardInterrupt("first")

        def second() -> None:
            ran.append("second")
            raise UnprintableFailure()

        def third() -> None:
            ran.append("third")
            raise UnprintableExit()

        def fourth() -> None:
            ran.append("fourth")

        primary = Exception("write failed")
        control = owned_descriptors._teardown_during(primary, first, second, third, fourth)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual(["first", "second", "third", "fourth"], ran)

        kinds = {type(entry) for entry in owned_descriptors.teardown_failures(primary)}
        self.assertIn(UnprintableFailure, kinds, "the unprintable ordinary failure was dropped")
        self.assertIn(UnprintableExit, kinds, "the unprintable control-flow failure was dropped")

        # The note is presentation, and it degrades to the type name rather
        # than disappearing — a reader still learns what arrived.
        notes = "\n".join(getattr(primary, "__notes__", []))
        self.assertIn("UnprintableFailure", notes)
        self.assertIn("UnprintableExit", notes)

    def test_an_interrupt_while_recording_a_later_failure_is_retained(self) -> None:
        """The innermost case: a later control-flow failure arrives, and the
        recording of *that* is interrupted too. Only its priority is bounded —
        both it and what it was recording stay on the ledger (j#90503)."""

        class Primary(Exception):
            def add_note(self, note: str) -> None:  # type: ignore[override]
                raise GeneratorExit("interrupt while recording the later failure")

        ran: list[str] = []

        def first() -> None:
            ran.append("first")
            raise KeyboardInterrupt("first")

        def second() -> None:
            ran.append("second")
            raise SystemExit("second")

        def third() -> None:
            ran.append("third")

        primary = Primary("write failed")
        control = owned_descriptors._teardown_during(primary, first, second, third)

        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual(["first", "second", "third"], ran)
        kinds = {type(entry) for entry in owned_descriptors.teardown_failures(primary)}
        self.assertIn(SystemExit, kinds, "the later control-flow failure was dropped")
        self.assertIn(GeneratorExit, kinds, "the interrupt that broke the recording was dropped")

    def _run_teardown_actions(self, primary, actions, label: str) -> None:
        """Run the actions and require that every one of them ran.

        One helper because the carrier has to hold under every hostile primary,
        not just the one that was fashionable that round.
        """
        ran: list[str] = []

        def tracked(index: int, action):  # type: ignore[no-untyped-def]
            def run() -> None:
                ran.append(f"a{index}")
                action()

            return run

        wrapped = [tracked(i, action) for i, action in enumerate(actions, start=1)]
        owned_descriptors._teardown_during(primary, *wrapped)

        self.assertEqual(
            [f"a{i}" for i in range(1, len(actions) + 1)],
            ran,
            f"{label}: the carrier skipped an action that had not run",
        )

    def _assert_ledger_holds_the_failure(self, primary, actions, label: str) -> None:
        """As above, and the failure is on the ledger afterwards."""
        self._run_teardown_actions(primary, actions, label)
        self.assertTrue(
            any(
                isinstance(entry, RuntimeError)
                for entry in owned_descriptors.teardown_failures(primary)
            ),
            f"{label}: the failure was not retained",
        )

    def test_the_ledger_survives_a_hostile_dict_descriptor(self) -> None:
        """j#90508 R16-F1. `object.__getattribute__(exc, "__dict__")` still runs
        a `__dict__` data descriptor defined by a subclass — bypassing
        `__setattr__` is not the same as bypassing the type.

        Measured before the fix: the property raising an ordinary exception lost
        the retention silently, and the property raising `KeyboardInterrupt`
        escaped the rail so the second action never ran.
        """

        class DictRaises(Exception):
            @property
            def __dict__(self):  # type: ignore[override]
                raise RuntimeError("this exception has no usable instance dict")

        class DictInterrupts(Exception):
            @property
            def __dict__(self):  # type: ignore[override]
                raise KeyboardInterrupt("interrupt from the carrier itself")

        def failing() -> None:
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            return None

        for label, primary in (
            ("ordinary", DictRaises("write failed")),
            ("control flow", DictInterrupts("write failed")),
        ):
            with self.subTest(descriptor=label):
                self._assert_ledger_holds_the_failure(primary, (failing, quiet), label)

    def test_the_carrier_key_is_not_an_attribute_name(self) -> None:
        """j#90517 R17-F1. An obscure string key is still an attribute name:
        `setattr`/`getattr` work on any string however it is spelled, so a
        caller's binding could be replaced. I claimed such a key was "outside
        the caller's namespace"; it was not. An identity key removes the
        collision instead of making it unlikely.

        The pickle cost is stated rather than hidden, and
        `test_the_pickle_boundary_depends_on_the_entries` says where it lands.
        """

        def failing() -> None:
            raise RuntimeError("teardown failure")

        self.assertNotIsInstance(
            owned_descriptors._LEDGER_KEY, str, "a string key is in the attribute namespace"
        )

        primary = RuntimeError("write failed")
        owned_descriptors._teardown_during(primary, failing)
        self.assertNotEqual((), owned_descriptors.teardown_failures(primary))
        self.assertEqual(
            ["__notes__"],
            [
                key
                for key in object.__getattribute__(primary, "__dict__")
                if isinstance(key, str)
            ],
            "the carrier took a name in the caller's namespace",
        )

    def test_the_pickle_boundary_depends_on_the_entries(self) -> None:
        """j#90529 R18-F2. I wrote the limitation down as "`dumps` succeeds,
        `loads` fails" — which is only true when what the ledger holds can be
        pickled. The ledger holds the failure objects rather than a rendering of
        them, so one whose `__reduce__` raises fails the dump. Stating a
        limitation is not the same as stating it accurately.
        """

        class Unpicklable(Exception):
            def __reduce__(self):  # type: ignore[override]
                raise TypeError("this failure cannot be pickled")

        def ordinary_failure() -> None:
            raise RuntimeError("teardown failure")

        def unpicklable_failure() -> None:
            raise Unpicklable("teardown failure")

        # A module-level primary type: a class defined in a test body is not
        # picklable for reasons that have nothing to do with the ledger.
        picklable_entries = RuntimeError("write failed")
        owned_descriptors._teardown_during(picklable_entries, ordinary_failure)
        with self.assertRaises(TypeError):
            pickle.loads(pickle.dumps(picklable_entries))

        unpicklable_entries = RuntimeError("write failed")
        owned_descriptors._teardown_during(unpicklable_entries, unpicklable_failure)
        with self.assertRaises(TypeError):
            pickle.dumps(unpicklable_entries)

    def test_a_value_at_the_carrier_key_is_never_replaced(self) -> None:
        """j#90508 R16-F2 and j#90517 R17-F1. The ledger was any `list` found at
        a public key, so a caller's own list was adopted and mutated and a
        `list` subclass with a hostile `__iter__` escaped the rail. Checking the
        value's type stopped the adoption but still *replaced* the binding, and
        the regression only looked at the foreign list's contents — so it went
        green without showing the binding was preserved.

        Refusing to retain is the right answer here: the record is worth less
        than someone else's data.
        """

        class Plain(Exception):
            pass

        class HostileList(list):
            def __iter__(self):  # type: ignore[override]
                raise KeyboardInterrupt("iterating this is not safe")

        def failing() -> None:
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            return None

        callers_own = ["caller data"]
        for label, value in (("plain list", callers_own), ("list subclass", HostileList())):
            with self.subTest(value=label):
                primary = Plain("write failed")
                state = object.__getattribute__(primary, "__dict__")
                state[owned_descriptors._LEDGER_KEY] = value

                # (a) the read accessor alone must not touch the binding.
                self.assertEqual((), owned_descriptors.teardown_failures(primary))
                self.assertIs(
                    value, state[owned_descriptors._LEDGER_KEY], f"{label}: a read replaced it"
                )

                # (b) nor may a full teardown.
                self._run_teardown_actions(primary, (failing, quiet), label)
                self.assertIs(
                    value,
                    state[owned_descriptors._LEDGER_KEY],
                    f"{label}: the teardown replaced the binding",
                )

        self.assertEqual(["caller data"], callers_own, "the caller's own list was mutated")

    def test_reading_the_ledger_does_not_create_one(self) -> None:
        """j#90517 R17-F1. `teardown_failures` looked like a read accessor and
        was not: it went through the creating path, so asking an exception what
        went wrong wrote to that exception even when nothing had failed."""

        primary = RuntimeError("write failed")
        state = object.__getattribute__(primary, "__dict__")
        before = dict(state)

        self.assertEqual((), owned_descriptors.teardown_failures(primary))
        self.assertEqual(before, dict(state), "reading the ledger modified the exception")

    def test_each_occurrence_is_one_ledger_entry(self) -> None:
        """j#90517 R17-F2. The ledger de-duplicated by object identity, so two
        independent actions returning the same singleton `False` — the whole
        returned-failure channel — collapsed into one entry while the notes
        correctly showed two. Occurrences are what the ledger counts."""

        def returns_false() -> bool:
            return False

        shared = RuntimeError("the same instance, raised twice")

        def raises_shared() -> None:
            raise shared

        for label, action in (("returned False", returns_false), ("raised", raises_shared)):
            with self.subTest(channel=label):
                primary = Exception("write failed")
                owned_descriptors._teardown_during(primary, action, action)

                self.assertEqual(
                    2,
                    len(owned_descriptors.teardown_failures(primary)),
                    f"{label}: two occurrences collapsed into one ledger entry",
                )
                self.assertEqual(
                    2,
                    len(getattr(primary, "__notes__", [])),
                    f"{label}: the notes and the ledger disagree",
                )

    def test_a_carrier_failure_never_skips_a_remaining_action(self) -> None:
        """j#90508 R16-F1, second condition: acquiring or writing the record is
        on the same channel as everything else.

        Pinned at the seam deliberately. Three carriers in a row were escaped
        by a hostile primary, and each fix made the previous hostile input
        unreachable — so asserting through an input would only pin whichever
        attack happened to still work. Retention not propagating is the
        property; this asserts that directly.

        It asserts what survives too, which the first version of this test did
        not: it checked the actions and the return value only, so it stayed
        green while a carrier that interrupted once and then recovered dropped
        both the failure it was recording and the interrupt (j#90529 R18-F1).
        """
        ran: list[str] = []
        failure = RuntimeError("teardown failure")

        def failing() -> None:
            ran.append("a1")
            raise failure

        def quiet() -> None:
            ran.append("a2")

        real_ledger = owned_descriptors._ledger
        fired: list[bool] = []

        def interrupts_once(primary):  # type: ignore[no-untyped-def]
            if not fired:
                fired.append(True)
                raise KeyboardInterrupt("interrupt from inside the carrier")
            return real_ledger(primary)

        primary = Exception("write failed")
        with unittest.mock.patch.object(owned_descriptors, "_ledger", interrupts_once):
            control = owned_descriptors._teardown_during(primary, failing, quiet)

        self.assertEqual(["a1", "a2"], ran, "a carrier failure skipped a remaining action")
        self.assertIsInstance(control, KeyboardInterrupt)

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is failure),
            "the failure the carrier refused was not retained exactly once on recovery",
        )
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is control),
            "the carrier's own interrupt was not retained exactly once",
        )

    @staticmethod
    def _source_line(function, match: str) -> int:
        """The line in `function` containing `match`.

        Found by source text, not written down: a literal line number would go
        stale the moment the module is edited, and an injection that quietly
        stops firing is exactly the kind of test that reports green for nothing.
        """
        lines, start = inspect.getsourcelines(function)
        for offset, line in enumerate(lines):
            if match in line:
                return start + offset
        raise AssertionError(
            f"no line matching {match!r} in {function.__name__}; the probe is stale"
        )

    @classmethod
    def _drain_line(cls, match: str) -> int:
        return cls._source_line(owned_descriptors._Retention._drain, match)

    def _interrupt_the_queue_append(self, failure: BaseException):
        """Raise `failure` once, on the instruction that admits to the queue."""
        line = self._source_line(
            owned_descriptors._Retention._enqueue, "self._queued.append("
        )
        code = owned_descriptors._Retention._enqueue.__code__
        fired: list[bool] = []

        def local(frame, event, arg):  # type: ignore[no-untyped-def]
            if event == "line" and frame.f_lineno == line and not fired:
                fired.append(True)
                raise failure
            return local

        def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
            return local if frame.f_code is code else None

        return tracer, fired

    def test_an_arrival_survives_a_failure_before_it_reaches_the_queue(self) -> None:
        """j#90620 R20-F1. Making ledger membership the commit authority fixed
        the far end of the machine and left the entrance lossy: an arrival lived
        in a single local until it was queued, so an ordinary exception dropped
        it and an interrupt *replaced* it with the interrupt's own occurrence.

        Measured before the fix, injecting at the queue append: `MemoryError`
        left an empty ledger, and `KeyboardInterrupt` left a ledger holding the
        interrupt and not the failure it arrived with.
        """
        for label, injected in (
            ("ordinary", MemoryError("no room to queue it")),
            ("control flow", KeyboardInterrupt("interrupt while queueing")),
        ):
            with self.subTest(failure=label):
                primary = Exception("write failed")
                retention = owned_descriptors._Retention(primary)
                original = RuntimeError("the original teardown failure")

                tracer, fired = self._interrupt_the_queue_append(injected)
                sys.settrace(tracer)
                try:
                    first = retention.remember(original)
                finally:
                    sys.settrace(None)
                retention.flush()

                self.assertTrue(fired, f"{label}: the injection never fired")
                ledger = owned_descriptors.teardown_failures(primary)
                self.assertEqual(
                    1,
                    sum(1 for entry in ledger if entry is original),
                    f"{label}: the arrival was lost before it reached the queue",
                )
                if label == "control flow":
                    self.assertIs(first, injected, "the interrupt did not take priority")
                    self.assertEqual(
                        1,
                        sum(1 for entry in ledger if entry is injected),
                        "the interrupt was not retained exactly once",
                    )
                else:
                    self.assertIsNone(first, "an ordinary failure is not control flow")

    @staticmethod
    def _helper_lines() -> dict[str, object]:
        """Classify every executable line of `_took_the_interrupt`.

        Enumerated from the code object rather than named, because naming is
        how the last two gaps got through: the injections sat after the
        priority assignment (j#90839 R23-F1), and then the enumeration started
        *after* the `try:` header and so skipped the two lines that were
        actually unprotected (j#90882 R24-F1). Everything the function can
        execute is classified here, and the residual is asserted rather than
        assumed.
        """
        source, start = inspect.getsourcelines(owned_descriptors._took_the_interrupt)
        code = owned_descriptors._took_the_interrupt.__code__
        executable = sorted({line for _, _, line in code.co_lines() if line})

        def text(line: int) -> str:
            return source[line - start].strip()

        entry = next(line for line in executable if text(line) == "try:")
        handler = next(
            line
            for line in executable
            if text(line).startswith("except BaseException as nested")
        )
        exit_line = next(line for line in executable if text(line).startswith("return "))

        body = [line for line in executable if entry < line < handler]
        inner = [line for line in executable if handler <= line < exit_line]
        if not body or not inner:
            raise AssertionError("the helper no longer has the shape this probe assumes")

        # The residual, spelled out. Classifying by how a line is *spelled* —
        # anything that looked like a `try:`/`except`/`return` — meant every
        # region added to the helper silently widened the escape surface it
        # approved, and two nested headers rode in that way (j#90918 R25-F1).
        # This is the sequence the helper is allowed to have, in order; if it
        # gains a region, or loses one, resolution fails here rather than
        # quietly permitting more.
        expected_roles = (
            "try:",
            "except BaseException as nested:",
            "try:",
            "except BaseException:",
            "pass",
            "return interrupt if first is None else first",
        )
        residual: list[int] = []
        remaining = list(expected_roles)
        for line in executable:
            if remaining and text(line).startswith(remaining[0]):
                remaining.pop(0)
                residual.append(line)
        if remaining:
            raise AssertionError(
                f"the helper no longer has the pinned residual shape; unmatched: {remaining}"
            )

        return {
            "executable": executable,
            "entry": entry,
            "exit": exit_line,
            "body": body,
            "inner": inner,
            "residual": set(residual),
        }

    def _interrupt_while_taking_an_interrupt(self, steps):  # type: ignore[no-untyped-def]
        """Raise each `(line, exception)` in `steps`, in order, once each."""
        code = owned_descriptors._took_the_interrupt.__code__
        pending = list(steps)

        def local(frame, event, arg):  # type: ignore[no-untyped-def]
            if event == "line" and pending and frame.f_lineno == pending[0][0]:
                _, failure = pending.pop(0)
                raise failure
            return local

        def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
            return local if frame.f_code is code else None

        return tracer, pending

    def _interrupt_the_main_rail(self, reached: list[bool] | None = None):
        """Interrupt the carrier once, so the main retention rail handles it."""
        real_ledger = owned_descriptors._ledger
        fired: list[bool] = []
        interrupt = KeyboardInterrupt("the carrier was interrupted")

        def interrupts_once(primary):  # type: ignore[no-untyped-def]
            if not fired:
                fired.append(True)
                if reached is not None:
                    reached.append(True)
                raise interrupt
            return real_ledger(primary)

        return (
            unittest.mock.patch.object(owned_descriptors, "_ledger", interrupts_once),
            interrupt,
        )

    def _interrupt_the_final_rail(self, reached: list[bool] | None = None):
        """Fail the main admission, then interrupt the exit rail."""
        real_enqueue = owned_descriptors._Retention._enqueue
        calls: list[int] = []
        interrupt = KeyboardInterrupt("the final admission was interrupted")

        def scheduled(retention, occurrence):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) == 1:
                raise MemoryError("the main admission failed")
            if len(calls) == 2:
                if reached is not None:
                    reached.append(True)
                raise interrupt
            return real_enqueue(retention, occurrence)

        return (
            unittest.mock.patch.object(
                owned_descriptors._Retention, "_enqueue", scheduled
            ),
            interrupt,
        )

    @staticmethod
    def _fail_occurrences(reached: list[bool], count: int):
        """Fail the helper's first `count` occurrence constructions.

        The handler cannot be reached by a traced injection: raising from a
        trace function turns tracing off for that frame, so a second injection
        inside the handler would never fire — the kind of probe that reports
        green having done nothing. Failing the construction instead leaves the
        tracer armed for the line actually under test. One failure reaches the
        handler; two reach the handler's own absorbing branch.
        """
        real = owned_descriptors._Occurrence
        seen: list[int] = []

        def raising(failure):  # type: ignore[no-untyped-def]
            if reached and len(seen) < count:
                seen.append(1)
                raise GeneratorExit(f"occurrence construction {len(seen)}")
            return real(failure)

        return unittest.mock.patch.object(
            owned_descriptors, "_Occurrence", raising
        ), seen

    def test_a_nested_interrupt_never_skips_a_remaining_action(self) -> None:
        """j#90807 R22-F1, and what the two rounds after it turned up.

        Catching a control-flow exception is not the same as handling it: the
        `except` body is ordinary code outside the `try` that caught it, so a
        second interrupt arriving while the first was being turned into an
        occurrence escaped the retention and skipped a cleanup.

        Every executable line of the helper, on both rails, under three
        schedules — because each round fixed one rail or one line and left its
        twin the same shape, and because lines the schedule never reached were
        silently skipped rather than measured (j#90918 R25-F1).

        Two things are asserted, and the second is the one that kept slipping:

        - wherever the helper does not escape, every action runs and the first
          control flow is the one returned;
        - the set of lines that *do* escape is exactly the pinned residual —
          the two guards' headers, the absorbing handler and its body, and the
          return. Approving whatever looked like a `try:` let two avoidable
          headers in, so the shape is spelled out in `_helper_lines` and a new
          region fails there instead.
        """
        shape = self._helper_lines()
        escaped: set[int] = set()
        exercised: set[int] = set()

        for rail, drive in (
            ("main", self._interrupt_the_main_rail),
            ("final", self._interrupt_the_final_rail),
        ):
            for schedule, failures in (("plain", 0), ("handler", 1), ("absorb", 2)):
                for line in shape["executable"]:  # type: ignore[union-attr]
                    with self.subTest(rail=rail, schedule=schedule, line=line):
                        reached: list[bool] = []
                        patch, interrupt = drive(reached)
                        occurrences, _ = self._fail_occurrences(reached, failures)
                        injected = GeneratorExit("a second interrupt while recording")
                        tracer, pending = self._interrupt_while_taking_an_interrupt(
                            [(line, injected)]
                        )

                        ran: list[str] = []

                        def failing() -> None:
                            ran.append("failing")
                            raise RuntimeError("teardown failure")

                        def quiet() -> None:
                            ran.append("quiet")

                        primary = Exception("write failed")
                        left = None
                        with contextlib.ExitStack() as stack:
                            stack.enter_context(patch)
                            stack.enter_context(occurrences)
                            sys.settrace(tracer)
                            try:
                                control = owned_descriptors._teardown_during(
                                    primary, failing, quiet
                                )
                            except BaseException as out:  # noqa: BLE001 - the point
                                control, left = None, out
                            finally:
                                sys.settrace(None)

                        if pending:
                            continue  # this schedule does not reach that line
                        exercised.add(line)
                        if left is not None:
                            escaped.add(line)
                            continue

                        self.assertEqual(
                            ["failing", "quiet"], ran, "a remaining action was skipped"
                        )
                        self.assertIs(
                            control, interrupt, "the first interrupt lost priority"
                        )

                        if schedule == "plain" and line in shape["body"]:  # type: ignore[operator]
                            # The recoverable case: the handler is reached by the
                            # injection alone, so both occurrences are keepable.
                            # Inside the handler nothing more is attempted — the
                            # regress ends there by design — which is stated here
                            # rather than left as an untested gap.
                            ledger = owned_descriptors.teardown_failures(primary)
                            self.assertEqual(
                                1,
                                sum(1 for entry in ledger if entry is interrupt),
                                "the interrupt being recorded was lost",
                            )
                            self.assertEqual(
                                1,
                                sum(1 for entry in ledger if entry is injected),
                                "the nested interrupt was not retained",
                            )

        unreached = set(shape["executable"]) - exercised - {shape["executable"][0]}  # type: ignore[index]
        self.assertEqual(
            set(),
            unreached,
            "a line of the helper was never executed by any schedule, so nothing measured it",
        )
        self.assertEqual(
            shape["residual"],
            escaped,
            "the lines that escape the helper are not the pinned residual",
        )

    def test_an_interrupt_during_the_final_admission_still_counts(self) -> None:
        """j#90779 R21-F1. The exit rail added for R20-F1 swallowed control flow
        with a `continue`, under a comment claiming priority was already
        decided. It is not: when the main loop leaves on an *ordinary* failure
        nothing has been chosen yet, so an interrupt during the final admission
        was neither raised by the caller nor recorded — while the very next
        attempt admitted successfully, so this is not the never-recovers
        boundary either.

        The schedule is the point: an ordinary failure first, so the exit rail
        is reached with no control flow chosen, then the interrupt on it, then
        recovery. Neither earlier regression composes those.
        """
        real_enqueue = owned_descriptors._Retention._enqueue
        interrupt = KeyboardInterrupt("the final admission was interrupted")
        calls: list[int] = []

        def scheduled(retention, occurrence):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) == 1:
                raise MemoryError("the main admission failed")
            if len(calls) == 2:
                raise interrupt
            return real_enqueue(retention, occurrence)

        ran: list[str] = []

        def failing() -> bool:
            ran.append("failing")
            return False

        def quiet() -> None:
            ran.append("quiet")

        primary = Exception("write failed")
        with unittest.mock.patch.object(
            owned_descriptors._Retention, "_enqueue", scheduled
        ):
            control = owned_descriptors._teardown_during(primary, failing, quiet)

        self.assertGreaterEqual(len(calls), 3, "the schedule never reached recovery")
        self.assertEqual(["failing", "quiet"], ran)
        self.assertIs(control, interrupt, "the interrupt did not reach the caller")

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is False),
            "the returned failure was lost",
        )
        self.assertEqual(
            1,
            sum(1 for entry in ledger if entry is interrupt),
            "the interrupt was not retained exactly once",
        )

    def test_an_exhausted_retry_still_reaches_the_queue(self) -> None:
        """j#90620 R20-F1, the far end of the same defect. The last interrupt of
        an exhausted retry sat in the local that was about to go out of scope,
        so it was never queued — and this is not the documented never-recovers
        boundary, because the carrier works again on the very next call."""
        real_ledger = owned_descriptors._ledger
        attempts = owned_descriptors._RETENTION_ATTEMPTS
        raised: list[BaseException] = []

        def interrupts_then_recovers(primary):  # type: ignore[no-untyped-def]
            if len(raised) < attempts:
                interrupt = KeyboardInterrupt(f"interrupt-{len(raised) + 1}")
                raised.append(interrupt)
                raise interrupt
            return real_ledger(primary)

        primary = Exception("write failed")
        retention = owned_descriptors._Retention(primary)
        original = RuntimeError("the original teardown failure")

        with unittest.mock.patch.object(
            owned_descriptors, "_ledger", interrupts_then_recovers
        ):
            first = retention.remember(original)
        retention.flush()

        self.assertEqual(attempts, len(raised), "the schedule did not exhaust the retries")
        self.assertIs(first, raised[0], "the first interrupt did not take priority")

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(1, sum(1 for entry in ledger if entry is original))
        for index, interrupt in enumerate(raised, start=1):
            self.assertEqual(
                1,
                sum(1 for entry in ledger if entry is interrupt),
                f"interrupt-{index} was not retained exactly once",
            )

    def _interrupt_after_a_commit(self, primary, line: int):
        """Trace `_drain` and interrupt once at `line`, after an append landed.

        "After the append returned" is an ordering, not a commit
        acknowledgement — control flow arrives between bytecodes. Conditioning
        on the ledger being non-empty is what makes this the post-commit
        boundary rather than some earlier one.
        """
        code = owned_descriptors._Retention._drain.__code__
        fired: list[bool] = []

        def local(frame, event, arg):  # type: ignore[no-untyped-def]
            if (
                event == "line"
                and frame.f_lineno == line
                and not fired
                and owned_descriptors.teardown_failures(primary)
            ):
                fired.append(True)
                raise KeyboardInterrupt("interrupt at an instruction boundary")
            return local

        def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
            return local if frame.f_code is code else None

        return tracer, fired

    def test_retention_survives_an_interrupt_at_a_commit_boundary(self) -> None:
        """j#90561 R19-F1. The queue popped an entry once its append returned,
        which is an ordering and not an acknowledgement: an interrupt between
        the append and the pop left the occurrence queued *and* recorded, so a
        retry duplicated it, and the pop sat outside the guard, so an interrupt
        there escaped the rail and skipped a cleanup that had not run.

        Measured before the fix at two boundaries: `actions=['failing']` with
        the `KeyboardInterrupt` escaping `_teardown_during`, and a ledger
        holding the same failure twice.
        """
        for label, match in (
            ("loop head", "while True:"),
            ("the unrecorded scan", "_unrecorded("),
        ):
            with self.subTest(boundary=label):
                ran: list[str] = []
                failure = RuntimeError("the teardown failure")

                def failing() -> None:
                    ran.append("failing")
                    raise failure

                def quiet() -> None:
                    ran.append("quiet")

                primary = Exception("write failed")
                tracer, fired = self._interrupt_after_a_commit(
                    primary, self._drain_line(match)
                )
                sys.settrace(tracer)
                try:
                    control = owned_descriptors._teardown_during(primary, failing, quiet)
                finally:
                    sys.settrace(None)

                self.assertTrue(fired, f"{label}: the injection never fired")
                self.assertEqual(["failing", "quiet"], ran, f"{label}: an action was skipped")
                self.assertIsInstance(control, KeyboardInterrupt)

                ledger = owned_descriptors.teardown_failures(primary)
                self.assertEqual(
                    1,
                    sum(1 for entry in ledger if entry is failure),
                    f"{label}: the failure was recorded more than once",
                )
                self.assertEqual(
                    1,
                    sum(1 for entry in ledger if entry is control),
                    f"{label}: the interrupt was not recorded exactly once",
                )

    def test_the_final_flush_surfaces_the_control_flow_it_hits(self) -> None:
        """j#90561 R19-F2. The flush after the last action returned control flow
        that was thrown away, so an interrupt there vanished entirely — worse
        than the demotion to a note R13-F3 was about, and not the documented
        never-recovers boundary either, since the carrier recovers next call."""
        ran: list[str] = []
        failure = RuntimeError("teardown failure")

        def failing() -> None:
            ran.append("failing")
            raise failure

        def quiet() -> None:
            ran.append("quiet")

        real_ledger = owned_descriptors._ledger
        schedule = ["ordinary", "interrupt"]

        def scheduled(primary):  # type: ignore[no-untyped-def]
            if schedule:
                if schedule.pop(0) == "ordinary":
                    raise MemoryError("the carrier failed, leaving the queue")
                raise KeyboardInterrupt("the carrier interrupted the final flush")
            return real_ledger(primary)

        primary = Exception("write failed")
        with unittest.mock.patch.object(owned_descriptors, "_ledger", scheduled):
            control = owned_descriptors._teardown_during(primary, failing, quiet)

        self.assertEqual([], schedule, "the schedule never reached the final flush")
        self.assertEqual(["failing", "quiet"], ran)
        self.assertIsInstance(control, KeyboardInterrupt)

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertEqual(1, sum(1 for entry in ledger if entry is failure))
        self.assertEqual(1, sum(1 for entry in ledger if entry is control))

    def test_a_carrier_that_never_recovers_gives_up_the_record_only(self) -> None:
        """The stated boundary, held to: if the carrier never takes anything,
        the record is unreachable — but the actions still all run, the first
        control flow still surfaces, and nothing is duplicated or escapes."""
        ran: list[str] = []

        def failing() -> None:
            ran.append("a1")
            raise RuntimeError("teardown failure")

        def quiet() -> None:
            ran.append("a2")

        def never_recovers(_primary):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt("the carrier is gone for good")

        primary = Exception("write failed")
        with unittest.mock.patch.object(owned_descriptors, "_ledger", never_recovers):
            control = owned_descriptors._teardown_during(primary, failing, quiet)

        self.assertEqual(["a1", "a2"], ran)
        self.assertIsInstance(control, KeyboardInterrupt)
        self.assertEqual((), owned_descriptors.teardown_failures(primary))

    def test_the_ledger_survives_a_primary_that_refuses_attributes(self) -> None:
        """The carrier has to be the instance dictionary, not `setattr`.

        A `__context__`-chained second carrier was written for exactly this
        case and measured to fail: `__context__` assignment routes through the
        same `__setattr__` that refuses the attribute, so both carriers died
        together. One carrier that works beats two that do not.
        """

        class Frozen(Exception):
            def __setattr__(self, name: str, value: object) -> None:
                raise AttributeError("this exception refuses attributes")

            def __getattr__(self, name: str) -> object:
                raise AttributeError("and refuses unknown reads")

        def failing() -> None:
            raise RuntimeError("teardown failure")

        primary = Frozen("write failed")
        owned_descriptors._teardown_during(primary, failing)

        ledger = owned_descriptors.teardown_failures(primary)
        self.assertTrue(
            any(isinstance(entry, RuntimeError) for entry in ledger),
            "the ledger did not survive an exception that refuses attributes",
        )

    def test_cleanup_helper_runs_exactly_once_when_it_raises(self) -> None:
        """j#90472 R10-F4. I claimed the single-shot guard was structurally
        unreachable; the review showed the path. `_release_staging` raising a
        non-`OSError` inside the replace-failure return unwinds into the outer
        handler, which calls `release()` again — the guard is what keeps that at
        one call, and the original exception must still surface.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)
        calls: list[int] = []

        def exploding_release(mirror_fd: int, temp_name: str, identity):  # type: ignore[no-untyped-def]
            calls.append(1)
            raise RuntimeError("injected helper failure")

        service._release_staging = exploding_release  # type: ignore[method-assign]

        def failing_replace(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise PermissionError(errno.EACCES, "injected")

        with unittest.mock.patch.object(
            legacy_mirror_sync.os, "replace", failing_replace
        ):
            with self.assertRaises(RuntimeError):
                service.sync()

        self.assertEqual(1, len(calls), "the cleanup helper ran more than once")

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
        # Both modules: the primitives were split across two files when the
        # service crossed the module-health threshold, and a fence that reads
        # only one of them would go blind to the other.
        source = "\n".join(
            Path(module.__file__).read_text(encoding="utf-8")
            for module in (legacy_mirror_sync, owned_descriptors)
        )
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
