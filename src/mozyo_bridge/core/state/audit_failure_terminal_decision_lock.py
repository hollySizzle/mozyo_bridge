"""Descriptor-bound home traversal, lock generation, publication, and rollback coordination."""
from __future__ import annotations
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
try:
    import fcntl
except ImportError:  # pragma: no cover - the managed terminal runtime is POSIX
    fcntl = None  # type: ignore[assignment]
_TEMP_SUFFIX = ".tmp"
class AuditFailureTerminalDecisionError(RuntimeError):
    """The decision store is absent, replaced, unreachable, or otherwise untrusted."""
@dataclass(frozen=True)
class LockGeneration:
    device: int
    inode: int
    ctime_ns: int
    token: str
    def as_payload(self) -> dict:
        return dict(device=self.device, inode=self.inode, ctime_ns=self.ctime_ns, token=self.token)
@dataclass
class _PublishedArtifact:
    name: str
    original_text: Optional[str]
    published_identity: tuple[int, int, int]
    staged_name: Optional[str] = None
    original_identity: Optional[tuple[int, int, int]] = None
    rollback_name: Optional[str] = None
    rollback_identity: Optional[tuple[int, int, int]] = None
    intent_name: Optional[str] = None
    intent_identity: Optional[tuple[int, int, int]] = None
    snapshot_bound: bool = True
    foreign_observed: bool = False
    committed: bool = False
    commit_marker: Optional[str] = None
@dataclass
class LockedHome:
    dir_fd: int
    lock_fd: int
    generation: LockGeneration
    publications: list[_PublishedArtifact]
    bootstrap_nonce: Optional[str]
    bootstrap_identity: Optional[tuple[int, int, int]]
class DecisionStoreFileCoordinator:
    def __init__(
        self,
        *,
        home: Path,
        document_name: str,
        sidecar_name: str,
        lock_name: str,
        display_path: Path,
    ) -> None:
        self.home = Path(home)
        self.document_name = document_name
        self.sidecar_name = sidecar_name
        self.lock_name = lock_name
        self.display_path = Path(display_path)
    def open_home_fd(self, *, create: bool) -> int:
        """Open/create the declared home by an anchored no-follow component walk."""
        if self.home.anchor != os.sep:
            raise AuditFailureTerminalDecisionError(f"decision store home {self.home} is not rooted")
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise AuditFailureTerminalDecisionError("component walk requires O_DIRECTORY|O_NOFOLLOW")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            current_fd = os.open(os.sep, flags)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError("filesystem root could not be opened") from exc
        try:
            for component in self.home.parts[1:]:
                if not component or component in (".", ".."):
                    raise AuditFailureTerminalDecisionError(f"unsafe home component in {self.home}")
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise AuditFailureTerminalDecisionError(f"home {self.home} does not exist") from None
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise AuditFailureTerminalDecisionError(f"could not create {component}") from exc
                    try:
                        next_fd = os.open(component, flags, dir_fd=current_fd)
                    except OSError as exc:
                        raise AuditFailureTerminalDecisionError(f"could not reopen {component}") from exc
                except OSError as exc:
                    raise AuditFailureTerminalDecisionError(f"unsafe directory {component}") from exc
                os.close(current_fd)
                current_fd = next_fd
            result = current_fd
            current_fd = -1
            return result
        finally:
            if current_fd >= 0:
                os.close(current_fd)
    def _require_safe_lock(self, dir_fd: int, lock_fd: int) -> None:
        if not hasattr(os, "geteuid"):
            raise AuditFailureTerminalDecisionError("decision store lock ownership is unverifiable")
        expected_owner = os.geteuid()
        try:
            opened = os.fstat(lock_fd)
            visible = os.stat(self.lock_name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_name} could not be verified "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        for label, info in (("opened", opened), ("visible", visible)):
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_name} is not a single-linked regular file at "
                    f"its {label} identity; fail closed"
                )
            if info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) != 0o600:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_name} has unsafe owner/mode at its {label} "
                    "identity; require current euid and mode 0600, then fail closed"
                )
        if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
            raise AuditFailureTerminalDecisionError(f"lock {self.lock_name} changed generation")
    def _entry_exists(self, dir_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be inspected "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        return True
    def _store_artifacts_exist(self, dir_fd: int) -> bool:
        return self._entry_exists(dir_fd, self.document_name) or self._entry_exists(dir_fd, self.sidecar_name)
    def _open_existing_lock_fd(self, dir_fd: int, *, writable: bool) -> Optional[int]:
        flags = (
            (os.O_RDWR if writable else os.O_RDONLY)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            lock_fd = os.open(self.lock_name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_name} could not be opened without following a "
                f"link ({type(exc).__name__}); fail closed"
            ) from exc
        try:
            self._require_safe_lock(dir_fd, lock_fd)
        except AuditFailureTerminalDecisionError:
            os.close(lock_fd)
            raise
        return lock_fd
    def _create_bootstrap_nonce(self, dir_fd: int) -> tuple[str, _PublishedArtifact]:
        nonce = secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        created = False
        try:
            fd = os.open(self.sidecar_name, flags, 0o600, dir_fd=dir_fd)
            created = True
            payload = nonce.encode("ascii")
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short identity sidecar write")
                remaining = remaining[written:]
            os.fsync(fd)
            info = os.fstat(fd)
            os.close(fd)
            fd = -1
            os.fsync(dir_fd)
            return nonce, _PublishedArtifact(
                self.sidecar_name,
                None,
                (int(info.st_dev), int(info.st_ino), int(info.st_ctime_ns)),
            )
        except OSError as exc:
            if fd >= 0:
                os.close(fd)
            if created:
                try:
                    os.unlink(self.sidecar_name, dir_fd=dir_fd)
                    os.fsync(dir_fd)
                except OSError:
                    pass
            raise AuditFailureTerminalDecisionError(
                f"decision store bootstrap nonce could not be created safely "
                f"({type(exc).__name__}); fail closed"
            ) from exc
    def _open_lock_fd(self, dir_fd: int, *, create: bool
    ) -> tuple[Optional[int], bool, Optional[str], list[_PublishedArtifact]]:
        lock_fd = self._open_existing_lock_fd(dir_fd, writable=create)
        if lock_fd is not None or not create:
            return lock_fd, False, None, []
        if self._store_artifacts_exist(dir_fd):
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.display_path} has artifacts but no coordination lock; "
                "refusing to recreate an initialized store's lock generation"
            )
        nonce, bootstrap = self._create_bootstrap_nonce(dir_fd)
        publications = [bootstrap]
        lock_fd = None
        try:
            if self._entry_exists(dir_fd, self.document_name):
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.display_path} changed during first-use claim; fail closed"
                )
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            lock_fd = os.open(self.lock_name, flags, 0o600, dir_fd=dir_fd)
            os.fchmod(lock_fd, 0o600)
            self._require_safe_lock(dir_fd, lock_fd)
            return lock_fd, True, nonce, publications
        except BaseException as original:
            if lock_fd is not None:
                try:
                    self._require_safe_lock(dir_fd, lock_fd)
                    os.unlink(self.lock_name, dir_fd=dir_fd)
                    os.fsync(dir_fd)
                except (OSError, AuditFailureTerminalDecisionError):
                    pass
                os.close(lock_fd)
            self._rollback_publications(dir_fd, publications)
            if isinstance(original, OSError):
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_name} could not be created safely "
                    f"({type(original).__name__}); fail closed"
                ) from original
            raise
    def _read_lock_token(self, lock_fd: int) -> str:
        try:
            if os.fstat(lock_fd).st_size > 128:
                raise ValueError("oversized lock identity")
            os.lseek(lock_fd, 0, os.SEEK_SET)
            token = os.read(lock_fd, 129).decode("ascii").strip()
            if len(token) != 32 or token != token.lower():
                raise ValueError("malformed lock identity")
            bytes.fromhex(token)
            return token
        except (OSError, UnicodeError, ValueError) as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_name} has an unreadable generation token "
                f"({type(exc).__name__}); fail closed"
            ) from exc
    def _write_lock_token(self, lock_fd: int, token: str) -> None:
        payload = (token + "\n").encode("ascii")
        try:
            os.ftruncate(lock_fd, 0)
            os.lseek(lock_fd, 0, os.SEEK_SET)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(lock_fd, remaining)
                if written <= 0:
                    raise OSError("short lock identity write")
                remaining = remaining[written:]
            os.fsync(lock_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_name} generation could not be persisted "
                f"({type(exc).__name__}); fail closed"
            ) from exc
    def _lock_generation(self, lock_fd: int, token: str) -> LockGeneration:
        try:
            info = os.fstat(lock_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_name} identity could not be read "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        return LockGeneration(int(info.st_dev), int(info.st_ino), int(info.st_ctime_ns), token)
    def require_lock_generation(self, scope: LockedHome) -> None:
        self._require_safe_lock(scope.dir_fd, scope.lock_fd)
        observed = self._lock_generation(scope.lock_fd, self._read_lock_token(scope.lock_fd))
        if observed != scope.generation:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_name} generation drifted during the operation; "
                "fail closed"
            )
        if scope.bootstrap_identity is not None:
            observed_bootstrap = self.artifact_identity(scope.dir_fd, self.sidecar_name)
            if observed_bootstrap != scope.bootstrap_identity:
                raise AuditFailureTerminalDecisionError(
                    "decision store bootstrap nonce changed inode generation during first use; "
                    "fail closed"
                )
    def _discard_created_lock(self, dir_fd: int, lock_fd: int) -> None:
        self._require_safe_lock(dir_fd, lock_fd)
        try:
            os.unlink(self.lock_name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"failed first-use lock {self.lock_name} could not be removed safely "
                f"({type(exc).__name__}); fail closed"
            ) from exc
    def _poison_visible_lock_generation(self, dir_fd: int, lock_fd: int) -> None:
        temp: Optional[str] = None
        poison_identity: Optional[tuple[int, int, int]] = None
        try:
            temp, poison_identity = self._stage_file(
                dir_fd, f"{self.lock_name}.poison", secrets.token_hex(16) + "\n"
            )
            self._require_safe_lock(dir_fd, lock_fd)
            try:
                os.rename(temp, self.lock_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                visible = self._visible_artifact_identity(dir_fd, self.lock_name)
                if visible is None or visible[:2] != poison_identity[:2]:
                    raise
            os.fsync(dir_fd)
            if self.artifact_identity(dir_fd, self.lock_name)[:2] != poison_identity[:2]:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_name} poison generation drifted; fail closed"
                )
            return
        except BaseException as poison_error:
            try:
                if temp is not None:
                    os.unlink(temp, dir_fd=dir_fd)
            except (FileNotFoundError, OSError):
                pass
            try:
                visible = self._visible_artifact_identity(dir_fd, self.lock_name)
                if visible is None:
                    os.fsync(dir_fd)
                    return
                opened = os.fstat(lock_fd)
                old_visible = (visible[0], visible[1]) == (opened.st_dev, opened.st_ino)
                poison_visible = poison_identity is not None and visible[:2] == poison_identity[:2]
                if old_visible:
                    self._require_safe_lock(dir_fd, lock_fd)
                elif not poison_visible:
                    raise AuditFailureTerminalDecisionError(
                        f"decision store lock {self.lock_name} changed before fallback invalidation"
                    )
                try:
                    os.unlink(self.lock_name, dir_fd=dir_fd)
                except BaseException:
                    if self._visible_artifact_identity(dir_fd, self.lock_name) is not None:
                        raise
                os.fsync(dir_fd)
                return
            except BaseException as fallback_error:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_name} could not be invalidated after "
                    f"publication rollback failure ({type(poison_error).__name__}); fail closed"
                ) from fallback_error
    @contextmanager
    def locked_home(self, *, create: bool, exclusive: bool) -> Iterator[LockedHome]:
        """Hold and continuously bind one visible lock generation to an operation."""
        if fcntl is None:
            raise AuditFailureTerminalDecisionError(
                "decision store advisory locking is unavailable; refusing shared state access"
            )
        dir_fd = self.open_home_fd(create=create)
        lock_fd: Optional[int] = None
        locked = False
        lock_created = False
        publications: list[_PublishedArtifact] = []
        try:
            lock_fd, lock_created, bootstrap_nonce, publications = self._open_lock_fd(
                dir_fd, create=create
            )
            if lock_fd is None:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.display_path} has no coordination lock; fail closed"
                )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                locked = True
            except OSError as exc:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_name} could not be acquired "
                    f"({type(exc).__name__}); fail closed"
                ) from exc
            self._require_safe_lock(dir_fd, lock_fd)
            if lock_created:
                if not exclusive or bootstrap_nonce is None:
                    raise AuditFailureTerminalDecisionError(
                        "a fresh decision store generation requires its exclusive bootstrap owner"
                    )
                token = secrets.token_hex(16)
                self._write_lock_token(lock_fd, token)
                try:
                    os.fsync(dir_fd)
                except OSError as exc:
                    raise AuditFailureTerminalDecisionError(
                        f"decision store lock {self.lock_name} directory entry was not durable "
                        f"({type(exc).__name__}); fail closed"
                    ) from exc
            else:
                token = self._read_lock_token(lock_fd)
                if not self._store_artifacts_exist(dir_fd):
                    raise AuditFailureTerminalDecisionError(
                        f"decision store lock {self.lock_name} is pre-existing without store "
                        "artifacts; refusing to adopt a possibly recreated generation"
                    )
            scope = LockedHome(
                dir_fd,
                lock_fd,
                self._lock_generation(lock_fd, token),
                publications,
                bootstrap_nonce,
                publications[0].published_identity if bootstrap_nonce is not None else None,
            )
            self.require_lock_generation(scope)
            yield scope
            self.require_lock_generation(scope)
            self._commit_publications(dir_fd, publications)
        except BaseException:
            if publications and all(self._publication_committed(dir_fd, item) for item in publications):
                return
            rollback_error: Optional[AuditFailureTerminalDecisionError] = None
            if publications:
                try:
                    self._rollback_publications(dir_fd, publications)
                except AuditFailureTerminalDecisionError as exc:
                    rollback_error = exc
            poison_required = rollback_error is not None or any(
                item.foreign_observed for item in publications
            )
            if lock_created and lock_fd is not None:
                try:
                    self._discard_created_lock(dir_fd, lock_fd)
                except AuditFailureTerminalDecisionError as exc:
                    if rollback_error is None:
                        rollback_error = exc
            elif poison_required and lock_fd is not None:
                try:
                    self._poison_visible_lock_generation(dir_fd, lock_fd)
                except BaseException:
                    pass
            if rollback_error is not None:
                raise AuditFailureTerminalDecisionError(
                    "decision store operation failed and could not be rolled back safely; fail closed"
                ) from rollback_error
            raise
        finally:
            if lock_fd is not None:
                if locked:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except BaseException:
                        pass
                try:
                    os.close(lock_fd)
                except BaseException:
                    pass
            try:
                os.close(dir_fd)
            except BaseException:
                pass
    def artifact_identity(self, dir_fd: int, name: str) -> tuple[int, int, int]:
        try:
            visible = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} identity could not be inspected "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        if not stat.S_ISREG(visible.st_mode) or visible.st_nlink != 1:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} is not a single-linked regular file; fail closed"
            )
        return (int(visible.st_dev), int(visible.st_ino), int(visible.st_ctime_ns))
    def read_file_snapshot(self, dir_fd: int, name: str) -> tuple[Optional[str], Optional[tuple[int, int, int]]]:
        if not hasattr(os, "O_NONBLOCK"):
            raise AuditFailureTerminalDecisionError(
                "decision store artifact reads require O_NONBLOCK; this platform must fail closed"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None, None
        except (OSError, UnicodeError) as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be opened without following a link "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {name} is not a single-linked regular file; fail closed"
                )
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                text = handle.read()
            visible = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except (OSError, UnicodeError) as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be read and revalidated "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if (
            not stat.S_ISREG(visible.st_mode)
            or visible.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
        ):
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} changed identity while being read; fail closed"
            )
        return text, (int(opened.st_dev), int(opened.st_ino), int(opened.st_ctime_ns))
    def read_file(self, dir_fd: int, name: str) -> Optional[str]:
        return self.read_file_snapshot(dir_fd, name)[0]
    def _stage_file(self, dir_fd: int, name: str, text: str) -> tuple[str, tuple[int, int, int]]:
        temp = f"{name}{_TEMP_SUFFIX}.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(temp, flags, 0o600, dir_fd=dir_fd)
            remaining = memoryview(text.encode("utf-8"))
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short stage write")
                remaining = remaining[written:]
            os.fsync(fd)
            info = os.fstat(fd)
            os.close(fd)
            fd = -1
        except BaseException as exc:
            if fd >= 0:
                try:
                    os.close(fd)
                except BaseException:
                    pass
                try:
                    os.unlink(temp, dir_fd=dir_fd)
                except OSError:
                    pass
            if isinstance(exc, (OSError, UnicodeError)):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {name} could not be staged "
                    f"({type(exc).__name__}); fail closed"
                ) from exc
            raise
        return temp, (int(info.st_dev), int(info.st_ino), int(info.st_ctime_ns))
    def _arm_rollback_backup(self, scope: LockedHome, publication: _PublishedArtifact) -> None:
        backup = f"{publication.name}.rollback.{secrets.token_hex(8)}"
        if publication.original_identity is None:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} has no original inode to retain; "
                "fail closed"
            )
        publication.rollback_name = backup
        publication.rollback_identity = publication.original_identity
        try:
            os.link(
                publication.name,
                backup,
                src_dir_fd=scope.dir_fd,
                dst_dir_fd=scope.dir_fd,
                follow_symlinks=False,
            )
            visible = os.stat(
                publication.name, dir_fd=scope.dir_fd, follow_symlinks=False
            )
            retained = os.stat(backup, dir_fd=scope.dir_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(visible.st_mode)
                or not stat.S_ISREG(retained.st_mode)
                or visible.st_nlink != 2
                or retained.st_nlink != 2
                or (visible.st_dev, visible.st_ino)
                != publication.original_identity[:2]
                or (retained.st_dev, retained.st_ino)
                != publication.original_identity[:2]
            ):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} could not retain one exact "
                    "rollback inode; fail closed"
                )
            publication.rollback_identity = (
                int(retained.st_dev),
                int(retained.st_ino),
                int(retained.st_ctime_ns),
            )
            os.fsync(scope.dir_fd)
        except AuditFailureTerminalDecisionError:
            raise
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} rollback inode could not be retained "
                f"durably ({type(exc).__name__}); fail closed"
            ) from exc
    def _remove_rollback_backup(self, dir_fd: int, publication: _PublishedArtifact, *, commit: bool = False) -> None:
        if publication.rollback_name is None or publication.rollback_identity is None:
            return
        try:
            visible = os.stat(publication.rollback_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            canonical = os.stat(publication.name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                publication.original_identity is not None
                and stat.S_ISREG(canonical.st_mode)
                and canonical.st_nlink == 1
                and (canonical.st_dev, canonical.st_ino)
                == publication.original_identity[:2]
            ):
                publication.rollback_name = None
                publication.rollback_identity = None
                return
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} rollback inode disappeared; "
                "fail closed"
            ) from None
        if (
            not stat.S_ISREG(visible.st_mode)
            or visible.st_nlink not in (1, 2)
            or (visible.st_dev, visible.st_ino)
            != publication.rollback_identity[:2]
        ):
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} rollback inode changed; fail closed"
            )
        if commit:
            publication.commit_marker = publication.rollback_name
        try:
            os.unlink(publication.rollback_name, dir_fd=dir_fd)
        except BaseException as exc:
            if self._entry_exists(dir_fd, publication.rollback_name):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} marker unlink failed"
                ) from exc
        try:
            os.fsync(dir_fd)
        except BaseException:
            pass
        publication.rollback_name = None
        publication.rollback_identity = None
        publication.committed = commit
    def _remove_intent_marker(self, dir_fd: int, publication: _PublishedArtifact, *, commit: bool = False) -> None:
        name = publication.intent_name
        identity = publication.intent_identity
        if name is None or identity is None:
            return
        if self.artifact_identity(dir_fd, name)[:2] != identity[:2]:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} intent marker changed; fail closed"
            )
        if commit:
            publication.commit_marker = name
        try:
            os.unlink(name, dir_fd=dir_fd)
        except BaseException as exc:
            if self._entry_exists(dir_fd, name):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} intent unlink failed"
                ) from exc
        try:
            os.fsync(dir_fd)
        except BaseException:
            pass
        publication.intent_name = None
        publication.intent_identity = None
        publication.committed = commit
    def _publication_committed(self, dir_fd: int, publication: _PublishedArtifact) -> bool:
        if publication.committed:
            return True
        marker = publication.commit_marker
        if marker is None:
            return False
        try:
            if self._entry_exists(dir_fd, marker):
                return False
            canonical = self._visible_artifact_identity(dir_fd, publication.name)
        except AuditFailureTerminalDecisionError:
            return False
        return canonical is not None and canonical[:2] == publication.published_identity[:2]
    def _commit_publications(self, dir_fd: int, publications: list[_PublishedArtifact]) -> None:
        for publication in list(publications):
            if publication.rollback_name is not None:
                self._remove_intent_marker(dir_fd, publication)
                self._remove_rollback_backup(dir_fd, publication, commit=True)
            elif publication.intent_name is not None:
                self._remove_intent_marker(dir_fd, publication, commit=True)
            else:
                publication.committed = True
    def arm_publication(self, scope: LockedHome, name: str) -> _PublishedArtifact:
        publication = _PublishedArtifact(name, None, (0, 0, 0), snapshot_bound=False)
        scope.publications.append(publication)
        marker, identity = self._stage_file(scope.dir_fd, f"{name}.intent", secrets.token_hex(16))
        publication.intent_name = marker
        publication.intent_identity = identity
        try:
            os.fsync(scope.dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(f"artifact {name} intent was not durable") from exc
        publication.original_identity = self._visible_artifact_identity(scope.dir_fd, name)
        return publication
    def bind_publication_snapshot(self, publication: _PublishedArtifact, text: Optional[str], identity: Optional[tuple[int, int, int]]) -> None:
        if (text is None) != (identity is None) or identity != publication.original_identity:
            raise AuditFailureTerminalDecisionError(f"artifact {publication.name} changed before binding")
        publication.original_text = text
        publication.snapshot_bound = True
    def publish_file(self, scope: LockedHome, name: str, text: str, *, publication: _PublishedArtifact) -> None:
        temp, staged_identity = self._stage_file(scope.dir_fd, name, text)
        publication.published_identity = staged_identity
        publication.staged_name = temp
        if not publication.snapshot_bound:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} has no bound original snapshot; fail closed"
            )
        if self._visible_artifact_identity(scope.dir_fd, name) != publication.original_identity:
            publication.foreign_observed = True
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} changed after snapshot and before publication; "
                "refusing to overwrite a foreign target"
            )
        if publication.original_text is not None:
            self._arm_rollback_backup(scope, publication)
        self.require_lock_generation(scope)
        if publication.rollback_identity is not None:
            try:
                original_visible = os.stat(name, dir_fd=scope.dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {name} changed before publication "
                    f"({type(exc).__name__}); fail closed"
                ) from exc
            if (
                not stat.S_ISREG(original_visible.st_mode)
                or original_visible.st_nlink != 2
                or (original_visible.st_dev, original_visible.st_ino)
                != publication.rollback_identity[:2]
            ):
                publication.foreign_observed = True
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {name} changed after rollback retention and before "
                    "publication; refusing to overwrite a foreign target"
                )
        try:
            os.rename(temp, name, src_dir_fd=scope.dir_fd, dst_dir_fd=scope.dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be published ({type(exc).__name__}); "
                "fail closed"
            ) from exc
        publication.staged_name = None
        published_identity = self.artifact_identity(scope.dir_fd, name)
        if published_identity[:2] != staged_identity[:2]:
            publication.foreign_observed = True
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} staging identity changed at publication; "
                "preserving the foreign target and invalidating this store generation"
            )
        try:
            os.fsync(scope.dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} directory publication was not durable "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        self.require_lock_generation(scope)
    def _visible_artifact_identity(self, dir_fd: int, name: str
    ) -> Optional[tuple[int, int, int]]:
        try:
            return self.artifact_identity(dir_fd, name)
        except AuditFailureTerminalDecisionError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise
    def _restore_published_text(self, dir_fd: int, publication: _PublishedArtifact) -> None:
        if publication.rollback_name is None or publication.rollback_identity is None:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} has no durable rollback inode; "
                "fail closed"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(publication.rollback_name, flags, dir_fd=dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} rollback inode could not be opened "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        try:
            opened = os.fstat(fd)
            opened_identity = (int(opened.st_dev), int(opened.st_ino), int(opened.st_ctime_ns))
            with os.fdopen(os.dup(fd), "r", encoding="utf-8") as retained:
                retained_text = retained.read()
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened_identity[:2] != publication.rollback_identity[:2]
                or retained_text != publication.original_text
                or self.artifact_identity(dir_fd, publication.rollback_name)[:2]
                != opened_identity[:2]
            ):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} retained rollback inode changed; "
                    "fail closed"
                )
            canonical = self._visible_artifact_identity(dir_fd, publication.name)
            if (
                canonical is None
                or canonical[:2] != publication.published_identity[:2]
            ):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} changed before atomic rollback; "
                    "refusing to overwrite a foreign target"
                )
            try:
                os.rename(
                    publication.rollback_name,
                    publication.name,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
            except BaseException:
                canonical = self._visible_artifact_identity(dir_fd, publication.name)
                if canonical is None or canonical[:2] != opened_identity[:2]:
                    raise
            try:
                os.fsync(dir_fd)
            except OSError as exc:
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} atomic rollback was not durable "
                    f"({type(exc).__name__}); fail closed"
                ) from exc
            if self.artifact_identity(dir_fd, publication.name)[:2] != opened_identity[:2]:
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} changed after atomic rollback; "
                    "fail closed"
                )
        except AuditFailureTerminalDecisionError:
            raise
        except (OSError, UnicodeError) as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} could not be restored "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            os.close(fd)
    def _remove_published_artifact(self, dir_fd: int, publication: _PublishedArtifact) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(publication.name, flags, dir_fd=dir_fd)
            opened = os.fstat(fd)
            opened_identity = (int(opened.st_dev), int(opened.st_ino), int(opened.st_ctime_ns))
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened_identity[:2] != publication.published_identity[:2]
                or self.artifact_identity(dir_fd, publication.name)[:2]
                != opened_identity[:2]
            ):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} changed before rollback; "
                    "refusing to remove a foreign rollback target"
                )
            os.unlink(publication.name, dir_fd=dir_fd)
            if os.fstat(fd).st_nlink != 0:
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} changed during rollback; "
                    "fail closed"
                )
        except AuditFailureTerminalDecisionError:
            raise
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} could not be removed during "
                f"rollback ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            if "fd" in locals():
                os.close(fd)
    def _rollback_publication(self, dir_fd: int, publication: _PublishedArtifact) -> None:
        try:
            canonical_info = os.stat(publication.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            canonical = None
            canonical_nlink = 0
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} could not be inspected during "
                f"rollback ({type(exc).__name__}); fail closed"
            ) from exc
        else:
            if not stat.S_ISREG(canonical_info.st_mode) or canonical_info.st_nlink not in (1, 2):
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} has unsafe rollback identity; "
                    "fail closed"
                )
            canonical = (
                int(canonical_info.st_dev),
                int(canonical_info.st_ino),
                int(canonical_info.st_ctime_ns),
            )
            canonical_nlink = int(canonical_info.st_nlink)
        if publication.published_identity == (0, 0, 0):
            if publication.intent_name is None:
                return
            original_visible = canonical is None and publication.original_identity is None
            if canonical is not None and publication.original_identity is not None:
                original_visible = canonical == publication.original_identity and canonical_nlink == 1
            if original_visible:
                self._remove_intent_marker(dir_fd, publication)
                return
            publication.foreign_observed = True
            raise AuditFailureTerminalDecisionError(f"artifact {publication.name} changed before staging")
        staged = (
            self._visible_artifact_identity(dir_fd, publication.staged_name)
            if publication.staged_name is not None
            else None
        )
        canonical_is_publication = (
            canonical is not None
            and canonical_nlink == 1
            and canonical[:2] == publication.published_identity[:2]
        )
        staged_is_publication = (
            staged is not None and staged[:2] == publication.published_identity[:2]
        )
        if staged_is_publication and not canonical_is_publication:
            original_still_visible = canonical is None and publication.original_identity is None
            if canonical is not None and publication.original_identity is not None:
                expected = publication.rollback_identity or publication.original_identity
                expected_nlink = 2 if publication.rollback_identity is not None else 1
                original_still_visible = canonical == expected and canonical_nlink == expected_nlink
            if not original_still_visible:
                publication.foreign_observed = True
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} has a foreign canonical target"
                )
            try:
                os.unlink(publication.staged_name, dir_fd=dir_fd)
            except OSError as exc:
                raise AuditFailureTerminalDecisionError(f"artifact {publication.name} stage removal failed") from exc
            self._remove_rollback_backup(dir_fd, publication)
        elif publication.rollback_name is not None and staged is None and canonical_is_publication:
            self._restore_published_text(dir_fd, publication)
        elif publication.rollback_name is not None and staged is None:
            if (
                canonical is not None
                and publication.rollback_identity is not None
                and canonical == publication.rollback_identity
                and canonical_nlink == 2
            ):
                self._remove_rollback_backup(dir_fd, publication)
            else:
                publication.foreign_observed = True
                raise AuditFailureTerminalDecisionError(
                    f"decision store artifact {publication.name} has a foreign canonical target; "
                    "preserving it and invalidating the lock generation"
                )
        elif canonical_is_publication and staged is None:
            self._remove_published_artifact(dir_fd, publication)
        else:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} could not be located at exactly "
                "one rollback identity; refusing to overwrite a foreign target"
            )
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {publication.name} rollback was not durable "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        self._remove_intent_marker(dir_fd, publication)
    def _rollback_publications(self, dir_fd: int, publications: list[_PublishedArtifact]) -> None:
        first_error: Optional[BaseException] = None
        for publication in reversed(publications):
            try:
                self._rollback_publication(dir_fd, publication)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise AuditFailureTerminalDecisionError(
                "one or more decision store publications could not be rolled back safely; "
                "fail closed"
            ) from first_error
__all__ = ("AuditFailureTerminalDecisionError", "DecisionStoreFileCoordinator", "LockedHome", "LockGeneration")
