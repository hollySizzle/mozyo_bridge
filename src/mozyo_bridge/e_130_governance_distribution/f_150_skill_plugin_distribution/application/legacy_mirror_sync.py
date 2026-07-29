"""Legacy project skill partial-mirror check / sync service (Redmine #14580).

Observes the tree for the rules in :mod:`..domain.legacy_mirror_contract` and,
in sync mode, replaces each pinned mirror entry. Every judgement about *what is
a violation*, *which rule outranks which*, *whether the cleanup rail runs* and
*which recovery converges* lives in the domain module; this layer only reports
what it saw.

That boundary is load-bearing rather than tidy (Redmine #14682). The audit
order, the write rail's error retention and precedence, the cleanup ownership
answer and whether a rerun can converge are all state transitions, and while
they were interleaved with the syscalls that produce them the only way to
exercise one was to build a real tree and provoke a real OS failure. They are
now evaluated by :class:`~..domain.legacy_mirror_contract.TreeObservation`,
:func:`~..domain.legacy_mirror_contract.swap_decision`,
:func:`~..domain.legacy_mirror_contract.release_decision` and the sync-outcome
functions, none of which touch a filesystem.

Directory descriptors are the I/O authority
-------------------------------------------
Review j#90418 R6-F1 measured that ``lstat`` preflight plus path-based I/O is
not enough: the path is re-resolved on every subsequent call, so swapping the
tree between the audit and the write reached outside the mirror four different
ways — a mirror entry re-pointed at an external file passed as clean because the
content read followed it; an aliased *parent* of either side let a write land
outside while ``O_NOFOLLOW`` on the leaf saw nothing wrong; and re-binding this
run's own temp path to a victim symlink let a path-based ``chmod`` change the
victim's mode and then installed the symlink as a pinned entry.

So the walk opens every component with ``O_DIRECTORY | O_NOFOLLOW`` and keeps
the resulting descriptor. Afterwards **nothing resolves a multi-component path
again**: entries are stat'd, read, created, renamed and unlinked relative to a
bound descriptor, with ``O_NOFOLLOW`` on the leaf. A component swapped after the
walk no longer affects where the I/O lands — the descriptor still refers to the
directory that was validated.

Consequences worth stating, because they are easy to undo by accident:

- content parity reads through the bound descriptors and re-validates on the
  fd with ``fstat``; a plain ``Path.read_bytes()`` would follow a symlink
  installed after rule E ran;
- the staging file is created with ``O_CREAT | O_EXCL | O_NOFOLLOW`` on the
  mirror descriptor, so this run owns it; the mode is set with ``fchmod`` on
  that fd, never ``chmod`` on a path that could have been re-bound;
- **that same descriptor stays open until the swap and the cleanup are done**,
  because the ownership proof compares inode numbers and a released inode
  number is handed straight back out on Linux — a substituted file inherited
  it and was installed as a pinned reference (Redmine #14652). Deferred write
  errors, which the close used to be in position to report before anything was
  installed, are reported by an explicit ``fsync`` instead;
- the swap is ``os.replace`` with ``src_dir_fd`` / ``dst_dir_fd``;
- cleanup unlinks this run's exact name relative to the same descriptor.

Where the host cannot provide these primitives the service fails closed
(:data:`~..domain.legacy_mirror_contract.PLATFORM_UNSUPPORTED`) rather than
degrading to path-based I/O. Deciding whether it can is
:mod:`.platform_capabilities`, which probes the call forms below rather than
reading ``os.supports_dir_fd`` — that set is an advertisement, and on Linux
CPython 3.12 it omits ``os.lstat`` and refused a host that supports everything
here (Redmine #14651).

Unreadable state is a typed violation, not an exception: a mode-000 canonical
file used to escape the audit as a traceback, which left `release check drift`
advising the operator to follow a disposition that was never printed
(j#90418 R6-F3).

Residue from an interrupted run is a plain unpinned entry: it blocks and asks
for a reviewed disposition. This service never deletes it, because it cannot
distinguish its own crash residue from a file someone meant to keep.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .owned_descriptors import (
    _close_quietly,
    _OWNERSHIP_ABSENT,
    _OWNERSHIP_CONFIRMED,
    _OWNERSHIP_FOREIGN,
    _OWNERSHIP_UNPROVEN,
    _OWNERSHIP_UNREADABLE,
    _OwnedDescriptor,
    _StagingOwnership,
    _teardown_during,
)
from .platform_capabilities import missing_platform_capabilities
from ..domain.legacy_mirror_contract import (
    CONTENT_DRIFT,
    ENTRY_MISSING,
    ENTRY_NOT_REGULAR,
    ENTRY_UNREADABLE,
    MIRROR_RELATIVE,
    MIRRORED_REFERENCES,
    OWNERSHIP_ABSENT,
    OWNERSHIP_CONFIRMED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_UNPROVEN,
    OWNERSHIP_UNREADABLE,
    RESIDUE_ABSENT,
    RULE_DEST_TOPOLOGY,
    RULE_SOURCE_TOPOLOGY,
    SOURCE_RELATIVE,
    MirrorAudit,
    TreeObservation,
    Violation,
    WriteOutcome,
    check_outcome,
    cleanup_residue,
    content_violation,
    entry_failure_kind,
    mirror_listing_failure,
    mirror_read_failure,
    mirror_subject,
    path_component_uncreatable,
    path_component_violation,
    pinned_mirror_violation,
    pinned_source_violation,
    release_decision,
    replace_failure,
    repo_root_unreadable,
    rule_e_subjects,
    source_read_failure,
    source_swapped_during_sync,
    staging_close_failure,
    staging_creation_failure,
    staging_write_failure,
    swap_decision,
    sync_aborted,
    sync_diverged,
    sync_refused,
    sync_succeeded,
    unlink_outcome,
    unpinned_entry_violation,
)

#: Staging-file prefix. Cosmetic only — it makes residue recognisable to a human
#: reading the blocker. Ownership comes from the exclusive create, never from
#: the name (j#90397 R5-F2).
_TEMP_PREFIX = ".mozyo-legacy-mirror."

#: Points a test may observe to exercise an interleaving deterministically.
HOOK_TEMP_CREATED = "temp_created"

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
#: Leaf reads: no-follow AND non-blocking. `O_NONBLOCK` is what keeps a FIFO
#: swapped in after the type audit from blocking the open itself; the `fstat`
#: on the returned fd then rejects it (j#90450 R7-F2).
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

#: The descriptor layer's ownership answers, translated into the contract's
#: vocabulary. An answer neither side recognises resolves to
#: ``OWNERSHIP_UNPROVEN``, which never unlinks and never installs — the two
#: fail-open shapes this whole rail exists to refuse.
_OWNERSHIP_ANSWERS: dict[str, str] = {
    _OWNERSHIP_CONFIRMED: OWNERSHIP_CONFIRMED,
    _OWNERSHIP_ABSENT: OWNERSHIP_ABSENT,
    _OWNERSHIP_FOREIGN: OWNERSHIP_FOREIGN,
    _OWNERSHIP_UNREADABLE: OWNERSHIP_UNREADABLE,
    _OWNERSHIP_UNPROVEN: OWNERSHIP_UNPROVEN,
}


def _ownership_answer(resolved: str) -> str:
    return _OWNERSHIP_ANSWERS.get(resolved, OWNERSHIP_UNPROVEN)


class LegacyProjectSkillMirrorSync:
    """Check or sync the legacy project skill partial mirror for one repo."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        progress_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.source_dir = self.repo_root / SOURCE_RELATIVE
        self.mirror_dir = self.repo_root / MIRROR_RELATIVE
        #: Seam for deterministic interleaving tests. Production callers leave
        #: it unset; behaviour does not depend on it.
        self._progress_hook = progress_hook

    def _notify(self, event: str) -> None:
        if self._progress_hook is not None:
            self._progress_hook(event)

    # --- bound-descriptor plumbing -----------------------------------------

    def _classify_component(
        self, parent_fd: int, part: str, walked: str, rule: str
    ) -> tuple[Violation | None, bool]:
        """Say *why* a component could not be opened as a real directory.

        The open is the authority; this only observes. Errno alone cannot tell
        a symlink from a plain non-directory — on macOS both give ENOTDIR under
        ``O_DIRECTORY | O_NOFOLLOW`` — so the observation is a no-follow
        ``lstat`` through the same bound parent, and the contract turns it into
        a message.
        """
        try:
            info = os.lstat(part, dir_fd=parent_fd)
        except FileNotFoundError:
            return None, True
        except OSError:
            return path_component_violation(
                rule, walked, unreadable=True, symlink=False, directory=False
            ), False
        return path_component_violation(
            rule,
            walked,
            unreadable=False,
            symlink=stat.S_ISLNK(info.st_mode),
            directory=stat.S_ISDIR(info.st_mode),
        ), False

    def _open_bound(
        self, relative: str, rule: str, *, create: bool = False
    ) -> tuple[int | None, tuple[Violation, ...], bool]:
        """Open each component no-follow, returning a descriptor for the leaf.

        Returns ``(fd, violations, missing)``. The repo root itself is opened
        without ``O_NOFOLLOW``: it is the anchor the operator invoked us with,
        and a checkout legitimately reached through a symlinked parent was
        accepted as out of scope (j#90378).
        """
        try:
            current = _OwnedDescriptor(
                os.open(self.repo_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            )
        except OSError:
            return None, (repo_root_unreadable(rule),), False

        # The walk owns exactly one descriptor at a time. Ownership moves to the
        # child *before* the previous one is closed: a close that unwinds must
        # not leave the loop holding a freed number, because the `finally` would
        # then close it again — and descriptor numbers are reused, so that
        # second close hit an unrelated handle (j#90482 R12-F1, the same defect
        # R11-F1 fixed for the staging descriptor).
        walked = ""
        # Remember what is unwinding, so the cleanup close below can record its
        # own failure without replacing it. A previous-close primary was being
        # overwritten by the current-close secondary (j#90487 R13-F1).
        in_flight: BaseException | None = None
        try:
            for part in relative.split("/"):
                walked = f"{walked}/{part}" if walked else part
                try:
                    child_fd = os.open(part, _DIR_FLAGS, dir_fd=current.fileno)
                except OSError:
                    violation, missing = self._classify_component(
                        current.fileno, part, walked, rule
                    )
                    if missing and create:
                        try:
                            os.mkdir(part, 0o755, dir_fd=current.fileno)
                            child_fd = os.open(part, _DIR_FLAGS, dir_fd=current.fileno)
                        except OSError:
                            return None, (path_component_uncreatable(rule, walked),), False
                    elif missing:
                        return None, (), True
                    else:
                        assert violation is not None
                        return None, (violation,), False

                previous, current = current, _OwnedDescriptor(child_fd)
                previous.close()
            # Hand the leaf to the caller; the `finally` then has nothing to do.
            return current.detach(), (), False
        except BaseException as unwinding:
            in_flight = unwinding
            raise
        finally:
            if current.held:
                if in_flight is None:
                    # Nothing is unwinding, so a failing close IS the failure;
                    # there is no primary for a returned `False` to hang off.
                    current.close()
                else:
                    # Route through the one rail rather than recording inline:
                    # recording outside it let an interrupt escape and skip the
                    # teardown that had not run yet (j#90492 R14-F1). Here that
                    # is the last action, but the rule must not be re-decided
                    # per call site.
                    interrupt = _teardown_during(in_flight, current.close)
                    if interrupt is not None:
                        raise interrupt

    @contextmanager
    def _bound(self, relative: str, rule: str, *, create: bool = False) -> Iterator[
        tuple[int | None, tuple[Violation, ...], bool]
    ]:
        fd, violations, missing = self._open_bound(relative, rule, create=create)
        try:
            yield fd, violations, missing
        finally:
            if fd is not None:
                _close_quietly(fd)

    @staticmethod
    def _entry_failure_kind(dir_fd: int, name: str) -> str:
        """Why an entry could not be opened: a TYPE problem or an access one.

        Collapsing every leaf-open ``OSError`` into "unreadable" mislabelled the
        two cases the open exists to reject: a symlink (refused by
        ``O_NOFOLLOW``) and a socket (refused as a special file) were reported
        as rule F unreadable (j#90458 R8-F1). A no-follow ``lstat`` through the
        same bound descriptor is what tells them apart; the classification
        itself belongs to the contract.
        """
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return entry_failure_kind(missing=True, unreadable=False, symlink=False, regular=False)
        except OSError:
            return entry_failure_kind(missing=False, unreadable=True, symlink=False, regular=False)
        return entry_failure_kind(
            missing=False,
            unreadable=False,
            symlink=stat.S_ISLNK(info.st_mode),
            regular=stat.S_ISREG(info.st_mode),
        )

    @staticmethod
    def _read_bound(dir_fd: int, name: str) -> tuple[bytes | None, str | None]:
        """Read an entry through a bound descriptor, re-validating on the fd.

        Returns ``(payload, failure_kind)``. The ``fstat`` is what makes this
        safe after the type audit: the descriptor cannot be re-pointed, so a
        symlink or FIFO installed in the meantime is refused here.

        ``O_NONBLOCK`` matters as much as ``O_NOFOLLOW``. Validating *after* the
        open is too late for a FIFO: the open itself blocks waiting for a
        writer, so an entry swapped to a FIFO right after the type audit hung
        `check()` indefinitely (j#90450 R7-F2 — a probe was still alive after
        four seconds and had to be killed). ``O_NONBLOCK`` makes the open return
        so the ``fstat`` can reject it; on a regular file it does not change
        read semantics.
        """
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError:
            return None, LegacyProjectSkillMirrorSync._entry_failure_kind(dir_fd, name)
        payload: bytes | None = None
        failure: str | None = None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                failure = ENTRY_NOT_REGULAR
            else:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
                payload = b"".join(chunks)
        except OSError:
            failure = ENTRY_UNREADABLE
        finally:
            # A failing `close` used to escape as a bare `OSError`, which the
            # CLI and the release gate turned back into a traceback
            # (j#90458 R8-F2).
            if not _close_quietly(fd):
                payload, failure = None, ENTRY_UNREADABLE
        return payload, failure

    # --- rules --------------------------------------------------------------

    @staticmethod
    def _inspect(dir_fd: int, name: str) -> dict[str, bool]:
        """One no-follow ``lstat``, reported as facts rather than a judgement."""
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return {"missing": True, "unreadable": False, "symlink": False, "regular": False}
        except OSError:
            return {"missing": False, "unreadable": True, "symlink": False, "regular": False}
        return {
            "missing": False,
            "unreadable": False,
            "symlink": stat.S_ISLNK(info.st_mode),
            "regular": stat.S_ISREG(info.st_mode),
        }

    def _audit_source_entries(self, source_fd: int) -> tuple[Violation, ...]:
        found: list[Violation] = []
        for name in MIRRORED_REFERENCES:
            violation = pinned_source_violation(name, **self._inspect(source_fd, name))
            if violation is not None:
                found.append(violation)
        return tuple(found)

    def _audit_dest_entries(self, mirror_fd: int) -> tuple[Violation, ...]:
        found: list[Violation] = []
        try:
            names = sorted(entry.name for entry in os.scandir(mirror_fd))
        except OSError:
            return (mirror_listing_failure(),)

        pinned = set(MIRRORED_REFERENCES)
        found.extend(unpinned_entry_violation(name) for name in names if name not in pinned)

        for name in MIRRORED_REFERENCES:
            violation = pinned_mirror_violation(name, **self._inspect(mirror_fd, name))
            if violation is not None:
                found.append(violation)
        return tuple(found)

    def _audit_content(
        self, source_fd: int, mirror_fd: int, dest_violations: tuple[Violation, ...]
    ) -> tuple[Violation, ...]:
        unusable = rule_e_subjects(dest_violations)
        found: list[Violation] = []
        for name in MIRRORED_REFERENCES:
            subject = mirror_subject(name)
            if subject in unusable:
                continue  # rule E already reported it
            try:
                os.lstat(name, dir_fd=mirror_fd)
            except FileNotFoundError:
                found.append(content_violation(ENTRY_MISSING, subject))
                continue
            except OSError:
                found.append(content_violation(ENTRY_UNREADABLE, subject))
                continue

            source_payload, source_failure = self._read_bound(source_fd, name)
            if source_failure is not None:
                found.append(source_read_failure(source_failure, name))
                continue
            mirror_payload, mirror_failure = self._read_bound(mirror_fd, name)
            if mirror_failure is not None:
                found.append(mirror_read_failure(mirror_failure, subject))
                continue
            if source_payload != mirror_payload:
                found.append(content_violation(CONTENT_DRIFT, subject))
        return tuple(found)

    def audit(self) -> MirrorAudit:
        """Observe the tree; :class:`TreeObservation` evaluates rules A-F.

        Nothing here decides what an observation *means* — the walk asks the
        observation which step is still worth taking, and hands the answers
        back. That keeps the evaluation order and the recorded ``skipped_rules``
        on one predicate instead of two that can drift apart.

        Each ``*_observable`` / ``*_suppressed`` answer already implies the
        descriptors its step needs: they are derived from ``*_opened``, which
        is ``fd is not None``.
        """
        seen = TreeObservation(missing_capabilities=missing_platform_capabilities())
        if seen.platform_unsupported:
            # Rule P short-circuits before any tree walk (characterization §1.1).
            return seen.evaluate()

        with self._bound(SOURCE_RELATIVE, RULE_SOURCE_TOPOLOGY) as (source_fd, source_topology, source_missing):
            seen = replace(
                seen,
                source_topology=source_topology,
                source_missing=source_missing,
                source_opened=source_fd is not None,
            )
            if seen.source_entries_observable:
                seen = replace(seen, source_entries=self._audit_source_entries(source_fd))

            with self._bound(MIRROR_RELATIVE, RULE_DEST_TOPOLOGY) as (mirror_fd, dest_topology, dest_missing):
                seen = replace(
                    seen,
                    dest_topology=dest_topology,
                    dest_missing=dest_missing,
                    dest_opened=mirror_fd is not None,
                )
                if seen.dest_entries_observable:
                    seen = replace(seen, dest_entries=self._audit_dest_entries(mirror_fd))

                if not seen.content_parity_suppressed:
                    seen = replace(
                        seen,
                        content=self._audit_content(source_fd, mirror_fd, seen.dest_violations),
                    )
                return seen.evaluate()

    # --- writing -----------------------------------------------------------

    def _replace_one(self, source_fd: int, mirror_fd: int, name: str) -> WriteOutcome:
        """Copy one pinned reference into place, entirely through bound fds.

        Failure handling has exactly one shape: capture the staging entry's
        identity at creation, and route every failure through
        :meth:`_release_staging` **once**, which re-proves ownership before it
        unlinks anything. Review j#90467 found all three ways the previous
        shape went wrong — a discarded close result reported success (R9-F1),
        cleanup unlinked by name and deleted a foreign entry substituted at
        that name (R9-F2), and an inline cleanup plus the outer ``finally``
        ran twice, claiming residue that no longer existed (R9-F3).

        Descriptor lifetime and the close are ordered deliberately, and the
        order is load-bearing on both sides (Redmine #14652):

        - **the staging descriptor is closed last**, after the swap and after
          the release. The ownership proof compares inode numbers, and an inode
          number is an identity only while the inode cannot be recycled — which
          is exactly what holding this descriptor guarantees. Closing it before
          the proof is consumed let a file substituted at the staging name
          inherit the same number and be installed as a pinned reference on
          Linux (:class:`_StagingOwnership` carries the measurement);
        - **so an explicit ``fsync`` reports deferred write errors** where the
          close used to: while the file is still only staging and nothing has
          been installed. The close still runs on every path and its result is
          still a violation, never discarded (R9-F1) — see
          :meth:`_close_staging`.

        ``os.fsync`` is deliberately *not* added to
        :mod:`.platform_capabilities`. That manifest exists for the
        descriptor-relative primitives whose absence would otherwise degrade
        this service to path-based I/O, and it must list every one of them
        (j#90450 R7-F4). ``fsync`` is in neither position: it takes no
        ``dir_fd``, it has no path-based fallback to degrade to, and Python
        provides it everywhere. A host that refuses it on a regular file gets a
        typed write failure and the sync refuses to write — the same
        fail-closed stance the rest of this module takes, stated here so it
        reads as a decision rather than an omission.
        """
        payload, failure = self._read_bound(source_fd, name)
        if failure is not None:
            # W0: nothing was staged, so nothing is left behind.
            return WriteOutcome(violations=(source_swapped_during_sync(name),))

        subject = mirror_subject(name)
        temp_name = f"{_TEMP_PREFIX}{os.urandom(8).hex()}.tmp"
        staging_subject = mirror_subject(temp_name)
        try:
            temp = _OwnedDescriptor(
                os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=mirror_fd,
                )
            )
        except OSError:
            # W1: the exclusive create never produced an entry to own.
            return WriteOutcome(violations=(staging_creation_failure(),))

        # The descriptor this holds is not just a handle to write through: it is
        # what makes the inode comparison an identity at all, so it outlives
        # every use of the proof (Redmine #14652).
        ownership = _StagingOwnership(temp)
        staging_live = True
        # What the *next* run will find at the staging name. Tracked apart
        # from the violations because the two disagree in both directions
        # (characterization §1.6): a rebound entry reports no cleanup failure
        # yet blocks the next run, and an uninspectable one reports a cleanup
        # failure while saying its own presence is unknown.
        residue = RESIDUE_ABSENT

        def release() -> tuple[Violation, ...]:
            """Release the staging entry at most once.

            Every failure branch goes through here, so cleanup cannot run twice
            (j#90467 R9-F3) and cannot be skipped when an exception — not just
            an `OSError` — unwinds the write (which a hook raising mid-sync
            demonstrated while this was being restructured).
            """
            nonlocal staging_live, residue
            if not staging_live:
                return ()
            staging_live = False
            problems = self._release_staging(mirror_fd, temp_name, ownership)
            residue = cleanup_residue(problems)
            return problems

        def install() -> tuple[Violation, ...]:
            """Write the payload and swap it into place, without closing.

            Split from the rails around it only so that the close has exactly
            one home — after this, on every path — rather than a copy per
            branch.
            """
            nonlocal staging_live, residue

            write_failed = False
            flushing = False
            try:
                # Identity is captured from the descriptor, immediately, before
                # any write can fail: it is the only proof of ownership the
                # cleanup path has.
                ownership.prove()
                view = memoryview(payload or b"")
                # A single `os.write` may write fewer bytes than asked
                # (j#90450 R7-F3). A run of zero-progress writes would spin
                # forever, so it is bounded and reported instead.
                stalled = 0
                while view:
                    written = os.write(temp.fileno, view)
                    if written <= 0:
                        stalled += 1
                        if stalled > 16:
                            raise OSError(errno.EIO, "write made no progress")
                        continue
                    stalled = 0
                    view = view[written:]
                # `fchmod` on our own descriptor: a path-based `chmod` here
                # changed a victim's mode when the temp name was re-bound to a
                # symlink between create and chmod (j#90418 R6-F1 case 4).
                os.fchmod(temp.fileno, 0o644)
                flushing = True
                # The close used to run before the swap, which is what made it
                # the place a deferred write error surfaced in time to stop an
                # install (j#90467 R9-F1). The close now has to come last, so
                # the flush is what reports one — here, while the entry is
                # still only staging and nothing has been installed.
                os.fsync(temp.fileno)
            except OSError:
                write_failed = True

            if write_failed:
                # W2-W5.
                return (staging_write_failure(subject, flushing=flushing),) + release()

            self._notify(HOOK_TEMP_CREATED)

            # Ask whether the name still refers to the file we created; the
            # contract decides what each answer permits (W6-W9).
            decision = swap_decision(
                _ownership_answer(ownership.resolve(mirror_fd, temp_name)), staging_subject
            )
            if not decision.proceed:
                staging_live = decision.owned
                if decision.release:
                    return decision.violations + release()
                if decision.residue is not None:
                    residue = decision.residue
                return decision.violations

            try:
                os.replace(temp_name, name, src_dir_fd=mirror_fd, dst_dir_fd=mirror_fd)
            except OSError:
                # W10/W11. Observe why the destination refused, and let the
                # contract choose the rule: reporting every failure as a type
                # problem stated a fact that was often untrue (j#90458 R8-F3).
                failed = replace_failure(self._entry_failure_kind(mirror_fd, name), subject)
                return (failed,) + release()

            staging_live = False  # the rename consumed it
            return ()

        try:
            problems = install()
        except BaseException as primary:
            # Not just `OSError`: a non-OSError unwinding the write reached
            # neither the hook nor the swap safety net, so the staging entry
            # this run owned was left behind (j#90472 R10-F1). The release and
            # the close may each unwind too — they are attempted independently
            # so one failing cannot skip the other, and neither replaces the
            # exception the caller sees (j#90477 R11-F1 / j#90482 R12-F2 /
            # j#90487 R13-F1). The release runs FIRST: it needs the descriptor
            # the close is about to give up.
            interrupt = _teardown_during(primary, release, temp.close)
            if interrupt is not None:
                raise interrupt
            raise
        # W14 is the close, and it only exists on this rail: W0 / W1 returned
        # before there was a descriptor to close.
        return WriteOutcome(
            violations=problems + self._close_staging(temp, subject), residue=residue
        )

    @staticmethod
    def _close_staging(temp: _OwnedDescriptor, subject: str) -> tuple[Violation, ...]:
        """Close the staging descriptor last, and report a close that failed.

        Last, because the ownership proof is a comparison of inode numbers and
        those are only identities while the inode is pinned by this descriptor
        (:class:`_StagingOwnership`); the release and the swap both consult it.

        Reported, because discarding a close result folded a real deferred
        write error into a `synced` banner and exit 0 (j#90467 R9-F1). Nothing
        is written between the ``fsync`` and this, so a failure here is a
        backstop rather than the detector it used to be — but a backstop that
        returns silence is not one.
        """
        if not temp.held:
            return ()
        if temp.close():
            return ()
        return (staging_close_failure(subject),)

    @staticmethod
    def _release_staging(
        mirror_fd: int, temp_name: str, ownership: _StagingOwnership
    ) -> tuple[Violation, ...]:
        """Remove this run's staging entry, and only this run's.

        Two failures the earlier version had, both measured (j#90467):
        unlinking by name alone deleted an ordinary file that had been
        substituted at that name (R9-F2), and running from both an inline
        return and the outer ``finally`` reported "still present" for residue
        the second call had just removed (R9-F3). Ownership is re-proved
        through :class:`_StagingOwnership`, and this is the single cleanup path.

        Which answers may unlink is :func:`release_decision`'s to say, and only
        ``confirmed`` does — including against the answer that says the
        descriptor pinning the inode has already been closed, so no comparison
        can be trusted.

        A window remains between the answer and the unlink. It is NOT the same
        shape as the swap-time residual, and the docs say so: that one can
        install a foreign inode, this one can delete a foreign entry. Both sit
        inside the threat model where an actor able to modify the mirror
        directory can modify entries directly, and closing either needs
        directory-level exclusion (j#90472 R10-F3).
        """
        display = mirror_subject(temp_name)
        decision = release_decision(
            _ownership_answer(ownership.resolve(mirror_fd, temp_name)), display
        )
        if not decision.unlink:
            return decision.violations

        try:
            os.unlink(temp_name, dir_fd=mirror_fd)
        except FileNotFoundError:
            # The name is empty either way, which is all the caller asked.
            removed = True
        except OSError:
            removed = False
        else:
            removed = True
        return unlink_outcome(removed=removed, display=display).violations

    # --- entry points ------------------------------------------------------

    def check(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        """Read-only. Returns ``(exit_code, stdout_lines, stderr_lines)``."""
        return check_outcome(self.audit(), str(self.source_dir), str(self.mirror_dir)).as_tuple()

    def sync(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        """Replace the pinned entries. Writes zero unless A-E all hold.

        The terminal states — refused / aborted / diverged / synced — are the
        sync machine of characterization §1.3; each one's report belongs to the
        contract, so this method only decides which one the tree reached.
        """
        preflight = self.audit()
        if preflight.blocks_write:
            return sync_refused(preflight).as_tuple()

        with self._bound(SOURCE_RELATIVE, RULE_SOURCE_TOPOLOGY) as (source_fd, source_violations, source_missing):
            if source_fd is None:
                return sync_refused(MirrorAudit(violations=source_violations or ())).as_tuple()
            with self._bound(MIRROR_RELATIVE, RULE_DEST_TOPOLOGY, create=True) as (
                mirror_fd,
                mirror_violations,
                _mirror_missing,
            ):
                if mirror_fd is None:
                    return sync_refused(MirrorAudit(violations=mirror_violations or ())).as_tuple()
                for name in MIRRORED_REFERENCES:
                    written = self._replace_one(source_fd, mirror_fd, name)
                    if written.failed:
                        return sync_aborted(written.violations).as_tuple()

        # Never announce success on an unverified tree: re-audit what we wrote.
        after = self.audit()
        if not after.ok:
            return sync_diverged(after).as_tuple()

        return sync_succeeded(str(self.source_dir), str(self.mirror_dir)).as_tuple()
