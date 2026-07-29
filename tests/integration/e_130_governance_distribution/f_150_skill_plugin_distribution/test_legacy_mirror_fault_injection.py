"""Legacy mirror sync fault-injection tests (Redmine #13483 / #14580).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
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
from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    CLEANUP_FAILED,
    WRITE_FAILED,
)
from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    _MirrorTreeFixture,
)


class LegacyMirrorFaultInjectionTest(_MirrorTreeFixture):
    """Adversarial cases that inject an `os` primitive against a real tree."""

    def test_the_staging_descriptor_still_pins_the_inode_at_every_ownership_question(
        self,
    ) -> None:
        """The pin has to be live where the question is asked, in production.

        Measured rather than read off a flag: at each point the sync asks who
        owns the staging name, `fstat` on the staging descriptor must still
        succeed — it raises `EBADF` once the descriptor is gone — and must still
        report the inode the proof was taken from.

        Both callers are covered, which needs both paths: the clean write asks
        before the swap, and the failed write asks again in the release.
        """
        real_resolve = owned_descriptors._StagingOwnership.resolve

        for label, break_the_write in (("clean write", False), ("failed write", True)):
            with self.subTest(label):
                repo = self._stage()
                canonical = self._source(repo) / "workflow.md"
                canonical.write_text(
                    canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8"
                )
                pinned: list[bool] = []

                def observing_resolve(self, dir_fd, name):  # type: ignore[no-untyped-def]
                    identity = self._identity
                    try:
                        live = os.fstat(self._descriptor.fileno)
                    except OSError:
                        pinned.append(False)  # the descriptor is already gone
                    else:
                        pinned.append(
                            identity is not None
                            and (live.st_dev, live.st_ino) == (identity.st_dev, identity.st_ino)
                        )
                    return real_resolve(self, dir_fd, name)

                def failing_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
                    raise OSError(errno.ENOSPC, "injected")

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        unittest.mock.patch.object(
                            owned_descriptors._StagingOwnership, "resolve", observing_resolve
                        )
                    )
                    if break_the_write:
                        stack.enter_context(
                            unittest.mock.patch.object(
                                legacy_mirror_sync.os, "write", failing_write
                            )
                        )
                    self._service(repo).sync()

                self.assertTrue(pinned, "ownership was never asked")
                self.assertTrue(
                    all(pinned), "ownership was asked while the inode was not pinned"
                )

    def test_a_deferred_write_error_is_reported_before_anything_is_installed(self) -> None:
        """#14652. The close used to be in position to catch a write error the
        host had deferred, because it ran before the swap. It now runs after —
        so the flush is what has to catch one, while the file is still staging.

        A flush that fails must therefore leave the mirror entry exactly as it
        was, and must not be folded into a success (j#90467 R9-F1).
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")
        entry = self._mirror(repo) / "workflow.md"
        before = entry.read_text(encoding="utf-8")
        fired: list[int] = []

        def failing_fsync(fd: int) -> None:
            fired.append(fd)
            raise OSError(errno.EIO, "injected deferred write error")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "fsync", failing_fsync):
            code, out, err = self._service(repo).sync()

        self.assertTrue(fired, "the staging flush was never reached")
        self.assertEqual(1, code)
        self.assertEqual((), out, "a failed flush still printed the banner")
        self.assertIn("could not be flushed to disk", "\n".join(err))
        self.assertEqual(
            before,
            entry.read_text(encoding="utf-8"),
            "a flush that failed still installed the entry",
        )
        self.assertEqual([], self._staging_names(repo), "the failed flush left residue")

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

        with self._preflight_already_answered():
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

    def test_an_unreadable_staging_name_at_swap_time_releases_the_staging(self) -> None:
        """j#90472 R10-F2, on the observation #14652 replaced the verify open
        with. Failing to observe the entry is not evidence that it is foreign;
        skipping cleanup guaranteed residue instead.

        Only the first observation fails, so the release's own observation still
        answers — a permanently failing `lstat` would test the *unreadable
        cleanup* branch instead, which is the next test's job.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_lstat = os.lstat
        fired: list[str] = []

        def failing_staging_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if (
                isinstance(path, str)
                and path.startswith(".mozyo-legacy-mirror.")
                and not fired
            ):
                fired.append(path)
                raise OSError(errno.EIO, "injected")
            return real_lstat(path, *args, **kwargs)

        with unittest.mock.patch.object(
            owned_descriptors.os, "lstat", failing_staging_lstat
        ):
            code, out, err = self._service(repo).sync()

        self.assertTrue(fired, "the staging observation was never reached")
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("could not be re-validated", "\n".join(err))
        self.assertEqual([], self._staging_names(repo), "the failed observation left residue")

    def test_an_unprovable_staging_identity_never_unlinks(self) -> None:
        """#14652. Without the identity there is no ownership proof, so the
        entry is reported and left rather than removed on a guess.

        The identity is read from the pinned descriptor, so the way to lose it
        is for that read to fail. What must not happen is the cleanup treating
        "no proof" as "not ours" and saying so, or as "ours" and unlinking.
        """
        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        real_open, real_fstat = os.open, os.fstat
        staging_fds: set[int] = set()

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                staging_fds.add(fd)
            return fd

        def failing_identity_fstat(fd: int):  # type: ignore[no-untyped-def]
            if fd in staging_fds:
                raise OSError(errno.EIO, "injected")
            return real_fstat(fd)

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                owned_descriptors.os, "fstat", failing_identity_fstat
            ):
                code, out, err = self._service(repo).sync()

        self.assertTrue(staging_fds, "the staging create was never reached")
        self.assertEqual(1, code)
        self.assertEqual((), out)
        self.assertIn("ownership could not be proved", "\n".join(err))
        self.assertEqual(
            1, len(self._staging_names(repo)), "an unprovable entry was unlinked anyway"
        )

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

    def test_a_raising_release_does_not_take_the_close_with_it(self) -> None:
        """j#90487 R13-F1, at the position #14652 moved it to.

        The rule is unchanged — the close and the release are independent, the
        first primary survives, and neither failure is dropped. What changed is
        which of the two runs first. The release now goes first because it needs
        the descriptor the close is about to give up, so the roles are reversed
        from R13-F1: the release is the primary and the close is the action that
        must still run and still be recorded.
        """

        class PrimaryCleanup(Exception):
            pass

        class SecondaryClose(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)

        def exploding_release(mirror_fd: int, temp_name: str, ownership):  # type: ignore[no-untyped-def]
            raise PrimaryCleanup("injected cleanup failure")

        service._release_staging = exploding_release  # type: ignore[method-assign]
        tracking_open, failing_close, state = self._fail_staging_close_with(
            SecondaryClose("injected close failure")
        )

        def failing_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
            raise OSError(errno.ENOSPC, "injected")

        with unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open):
            with unittest.mock.patch.object(
                legacy_mirror_sync.os, "close", failing_close
            ):
                with unittest.mock.patch.object(
                    legacy_mirror_sync.os, "write", failing_write
                ):
                    with self.assertRaises(PrimaryCleanup) as caught:
                        service.sync()

        self.assertTrue(state["fired"], "the staging close never ran after the release raised")
        notes = getattr(caught.exception, "__notes__", [])
        self.assertTrue(
            any("SecondaryClose" in note for note in notes),
            "the close failure was dropped",
        )

    def _staging_lifetime_events(self, repo: Path):  # type: ignore[no-untyped-def]
        """A service whose staging release and staging close announce themselves.

        Returns ``(service, events, open_patch, close_patch)``. The close is
        observed through the real `os.close` call, so what lands in ``events``
        is the syscall happening, not a flag the implementation set.
        """
        service = self._service(repo)
        events: list[str] = []
        real_release = service._release_staging
        real_open, real_close = os.open, os.close
        staging_fds: set[int] = set()

        def watching_release(mirror_fd: int, temp_name: str, ownership):  # type: ignore[no-untyped-def]
            events.append("release")
            return real_release(mirror_fd, temp_name, ownership)

        def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                staging_fds.add(fd)
            return fd

        def watching_close(fd: int) -> None:
            if fd in staging_fds:
                staging_fds.discard(fd)
                events.append("close")
            real_close(fd)

        service._release_staging = watching_release  # type: ignore[method-assign]
        return service, events, tracking_open, watching_close

    def test_the_staging_release_always_precedes_the_staging_close(self) -> None:
        """#14652. The release consults the ownership proof, and that proof is
        only sound while the staging descriptor still pins the inode — so the
        release has to happen before the close on every path that has one.

        Two paths, because one alone would be vacuous: the write-failure path
        releases and then closes, and the success path has the rename consume
        the entry so the close is the only event. What must never appear is a
        release after a close.
        """
        for label, break_the_write in (("failed write", True), ("clean write", False)):
            with self.subTest(label):
                repo = self._stage()
                canonical = self._source(repo) / "workflow.md"
                canonical.write_text(
                    canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8"
                )
                service, events, tracking_open, watching_close = self._staging_lifetime_events(
                    repo
                )

                def failing_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
                    raise OSError(errno.ENOSPC, "injected")

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        unittest.mock.patch.object(legacy_mirror_sync.os, "open", tracking_open)
                    )
                    stack.enter_context(
                        unittest.mock.patch.object(legacy_mirror_sync.os, "close", watching_close)
                    )
                    if break_the_write:
                        stack.enter_context(
                            unittest.mock.patch.object(
                                legacy_mirror_sync.os, "write", failing_write
                            )
                        )
                    service.sync()

                self.assertIn("close", events, "the staging close was never observed")
                self.assertEqual(
                    break_the_write,
                    "release" in events,
                    "the release did not run exactly on the path that needs it",
                )
                if break_the_write:
                    self.assertLess(
                        events.index("release"),
                        events.index("close"),
                        "the release consulted the ownership proof after the pin was gone",
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

        with self._preflight_already_answered():
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
        ordinary channels both record every failure.

        "First" is by arrival, so the injections follow the teardown order: the
        release runs before the close (#14652), and it is the release's
        interrupt that has to surface.
        """

        class PrimaryWrite(Exception):
            pass

        repo = self._stage()
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nDRIFT\n", encoding="utf-8")

        service = self._service(repo)

        def interrupting_release(mirror_fd: int, temp_name: str, ownership):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt("injected first control flow")

        service._release_staging = interrupting_release  # type: ignore[method-assign]
        tracking_open, failing_close, state = self._fail_staging_close_with(
            SystemExit("injected second control flow")
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


if __name__ == "__main__":
    unittest.main()
