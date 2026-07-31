"""Legacy mirror sync service contract tests (Redmine #13483 / #14580).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application import (  # noqa: E402
    legacy_mirror_sync,
    owned_descriptors,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.application.legacy_mirror_sync import (  # noqa: E402
    HOOK_TEMP_CREATED,
)
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    CONTENT_DRIFT,
    ENTRY_NOT_REGULAR,
    ENTRY_SYMLINK,
    MIRRORED_REFERENCES,
    PATH_COMPONENT_NOT_DIRECTORY,
    PATH_COMPONENT_SYMLINK,
    ENTRY_MISSING,
    ENTRY_UNREADABLE,
    PLATFORM_UNSUPPORTED,
    RECOVERY_REPLACE_ENTRY,
    RECOVERY_RESYNC,
    RULE_CONTENT_PARITY,
    SOURCE_MISSING,
    SOURCE_SYMLINK,
    SOURCE_UNREADABLE,
    UNPINNED_ENTRY,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


class LegacyMirrorSyncServiceTest(_MirrorTreeFixture):
    """The service contract against a real tree: audit, check, sync, report.

    Nothing here injects an `os` primitive — the cases that do live in
    `test_legacy_mirror_fault_injection.py`."""

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
        """The inode identity check, isolated from the no-follow type check.

        Re-binding the staging name to a *symlink* is already refused by the
        type of the entry. Re-binding it to an ordinary file is not, so only
        comparing the inode catches it — a mutation probe that removed the
        comparison stayed green until this case existed.

        This is also the case that measured the comparison being unsound on its
        own: on a filesystem that recycles inode numbers the impostor inherited
        the number and was installed, and this test failed 3 runs out of 3 on
        Linux overlayfs while passing on tmpfs and APFS (Redmine #14652). It is
        the outcome-level half of that fix; the property-level half is below,
        and does not depend on the host recycling anything.
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

    # --- #14652: an inode number is an identity only while it is pinned ------

    def test_ownership_refuses_to_answer_once_the_descriptor_is_closed(self) -> None:
        """Why the staging descriptor is the last thing closed.

        The name here still refers to the very file that was created — nothing
        was substituted — so comparing `(st_dev, st_ino)` would answer "ours",
        and would keep answering "ours" for whatever file inherited the number
        next. It is refused instead. Fail-closed is not "usually right": an
        unpinned comparison is not a weaker proof of ownership, it is not one.

        Host-independent by construction. The outcome this prevents needs a
        filesystem that recycles inode numbers; this asks the question the
        recycling makes unanswerable, and that question has the same answer
        everywhere.
        """
        repo = self._stage()
        mirror_fd = os.open(self._mirror(repo), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, mirror_fd)
        name = ".mozyo-legacy-mirror.pin-probe.tmp"
        descriptor = owned_descriptors._OwnedDescriptor(
            os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=mirror_fd)
        )
        self.addCleanup(os.unlink, name, dir_fd=mirror_fd)
        ownership = owned_descriptors._StagingOwnership(descriptor)
        ownership.prove()

        self.assertEqual(
            owned_descriptors._OWNERSHIP_CONFIRMED,
            ownership.resolve(mirror_fd, name),
            "the pinned entry was not recognised as ours",
        )
        descriptor.close()
        self.assertEqual(
            owned_descriptors._OWNERSHIP_UNPROVEN,
            ownership.resolve(mirror_fd, name),
            "an unpinned inode number was accepted as an identity",
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

    def test_a_staging_entry_gone_before_the_swap_is_reported_without_residue(self) -> None:
        """The sibling branch: the name resolves to nothing at all.

        There is nothing to install and nothing to clean up, so the run reports
        the aborted swap and claims no surviving residue — claiming residue that
        is not there was its own defect (j#90467 R9-F3).
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")
        removed: list[str] = []

        def hook(event: str) -> None:
            if event == HOOK_TEMP_CREATED:
                for path in self._mirror(repo).iterdir():
                    if path.name.startswith(".mozyo-legacy-mirror."):
                        path.unlink()
                        removed.append(path.name)
                        break

        code, out, err = self._service(repo, progress_hook=hook).sync()

        self.assertTrue(removed, "the staging entry was never observed to remove")
        self.assertEqual(1, code)
        self.assertEqual((), out)
        report = "\n".join(err)
        self.assertIn("was gone before it could be installed", report)
        self.assertNotIn("still present", report)
        self.assertEqual([], self._staging_names(repo))

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


if __name__ == "__main__":
    unittest.main()
