"""The coordinator's audit-failure terminal decision, as mozyo-owned desired state (#15166).

Three review rounds established what this store exists to replace. The route terminalizes a
no-change verification lane whose failure was recorded by an independent audit rather than a
``## Gate: review``, and its hard question is never the lane's own facts — those are all
measurable — but the BINDING between "this audit failure" and "that successor's acceptance".
Each attempt to derive that binding from durable records was refuted by measurement:

- mutual acknowledgement between the two issues (review j#101880 finding 1): one unauthenticatable
  writer can place both halves, because every role posts under one source-system account (ruling
  #14219 j#86718);
- the successor's approved review examining the lane's exact head (review j#101909 finding 1): on
  a zero-change lane the lane head IS the integration head, so every unrelated approved issue on
  that base shares it;
- an enumeration hard-coded in the package (review j#102074 finding 1 / scope decision j#102081):
  it closed the hole but made every future lane of the same shape a product change, which is an
  individual migration rather than a supported rail.

The binding is a coordinator JUDGEMENT, and ``managed-state-model.md`` already says where a
judgement made at a mozyo command boundary lives. Its ``state_kinds`` table is the authority for
this module's existence:

- ``desired_state`` — "mozyo が command 境界で作成/採用/mark/rename しようとした構成・意図" — whose
  authority is *mozyo-owned persisted state*. A decision recorded here is authoritative for WHAT
  WAS DECIDED, within that classification and no further;
- ``side_effect_permission`` — whose authority is the mozyo command implementation, and whose
  meaning is spelled out as "persisted desired state + durable workflow gate + action-time live
  preflight を照合した結果". That conjunction is exactly the retire's fence: this record supplies
  the first term, the Redmine journals the second, and the live probes the third.

**The writer boundary is INSIDE the writer** (review j#102074 finding 1, then j#102147 finding 1).
R4 put the decision in this store and argued that being mozyo-owned made it a coordinator decision.
The reviewer reproduced a write from an ``argparse.Namespace`` carrying no actor identity at all:
where a record is STORED classifies the record, it does not identify who wrote it. So
:meth:`record` now runs the same action-time sender attestation #13613 already requires before a
lane mutation — :func:`...sublane_actuator_herdr_preflight.evaluate_dispatch_sender`, which resolves
the sender identity from the process environment and cross-checks it against the repository's
WORKSPACE ANCHOR, the committed coordinator provider binding, and the coordinator default lane. A
non-coordinator caller is refused zero-write, and because the check lives here rather than in the
command, calling the store directly does not get past it.

**What a record here does and does not establish, stated rather than implied.** It does NOT
authenticate a human, and nothing in this workspace can — that gap is unchanged. What it
establishes is (a) that the writer resolved to the configured coordinator provider on the
coordinator default lane of THIS repository's anchored workspace, under the same gate that already
fences destructive lane mutation, and (b) that the record lives on a surface a Redmine journal
author cannot reach: no sequence of journal writes produces a row here.

**Single use is not a state machine here; it is the lifecycle revision.** A decision is bound to
the lane's exact ``lane_generation`` AND ``revision`` at decision time, and every retire that
mutates the lane row advances that revision through the existing CAS. A decision therefore
authorizes at most one mutation and cannot be replayed against the world it left behind — using
the lifecycle generation the design direction (j#102092) names as one of its canonical sources,
rather than inventing a second consumption ledger that could disagree with it.

**Every artifact is created and opened through a STABLE DIRECTORY DESCRIPTOR** (review j#102582
finding 1). R6 validated the path and then handed the path STRING to ``sqlite3.connect``; the
reviewer replaced the leaf with a symlink between the check and the open and an external database
was created (20480 bytes) before the post-hoc identity check fired. Detecting a redirect after the
write is not zero-write, and no amount of extra prechecking fixes it — the gap is intrinsic to
re-resolving a path a second time.

``sqlite3`` cannot be handed a descriptor (its only input is a path), so this store does not use
it. It does not need to: one record per lane route, replaced whole. The filesystem root is opened
with ``O_DIRECTORY | O_NOFOLLOW`` and every home component is walked/created relative to the
descriptor for its parent with the same no-follow flags. JSON, nonce and lock operations are then
relative to the resulting home descriptor — create, read, write, atomic rename. A component swap
cannot redirect a later operation through a newly resolved path because there is no path-wide
second resolution to lose. That is the "stable directory/file descriptor" the required direction
asks for, met rather than weakened.

Atomic rename protects one publication, not the read-modify-write that precedes it. A regular,
single-linked home-relative lock therefore serializes the entire initialization/load/update/
publication sequence with ``LOCK_EX``. Reads and initialization probes hold ``LOCK_SH`` through
their complete snapshot, and never create a missing home or lock. Missing, replaced or unsafe lock
identity is a typed fail-closed refusal.

Store identity mirrors the sibling fences: a nonce sidecar makes a deleted / replaced / foreign
store fail CLOSED. Unlike them there is no ``bootstrap`` / ``recover`` ceremony, because the
asymmetry here is simpler and safer: :meth:`record` — the coordinator's own action — creates the
store, and every read path refuses when it is absent. A lost store therefore cannot silently admit
anything; it can only require the coordinator to decide again.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - the managed terminal runtime is POSIX
    fcntl = None  # type: ignore[assignment]

from mozyo_bridge.shared.paths import mozyo_bridge_home

AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION = 1
AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX = ".nonce"
AUDIT_FAILURE_TERMINAL_DECISION_LOCK_SUFFIX = ".lock"
#: The records file's basename under the home. A plain JSON document, replaced whole: one record
#: per lane route, serialized by a home-relative advisory lock, and — decisively — a format this
#: module can write through a descriptor.
AUDIT_FAILURE_TERMINAL_DECISION_FILENAME = "audit-failure-terminal-decision.json"
_TEMP_SUFFIX = ".tmp"


class AuditFailureTerminalDecisionError(RuntimeError):
    """The decision store is absent, replaced, unreachable, or the writer is not attested."""


def audit_failure_terminal_decision_path(home: Optional[Path] = None) -> Path:
    """Resolve the decision records path under the mozyo-bridge home."""
    return (
        Path(home) if home is not None else mozyo_bridge_home()
    ) / AUDIT_FAILURE_TERMINAL_DECISION_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint_decision_id() -> str:
    """A fresh opaque decision id. Minted by the STORE, never supplied by a caller."""
    return f"aft_{secrets.token_hex(16)}"


@dataclass(frozen=True)
class DecisionRoute:
    """The route one decision belongs to: a lane in a workspace.

    Deliberately NOT keyed on the issue. The retire resolves the route from the lane lifecycle row
    it is retiring, so the lookup cannot be pointed at another lane's decision by naming a
    different issue — the issue is one of the BOUND fields the decision must then match, not part
    of the key that selects it.
    """

    workspace_id: str
    lane_id: str

    def as_row(self) -> tuple[str, str]:
        return (self.workspace_id.strip(), self.lane_id.strip())


@dataclass(frozen=True)
class TerminalDecision:
    """One recorded coordinator decision, with every identity it is bound to.

    ``decision_id`` is the store's own minted handle. Every other field is an identity the retire
    re-measures from an independent source at action time — the declaration marker, the lane
    lifecycle row, the committed config, and the live checkout — so a decision authorizes exactly
    the world it was taken about and nothing that has drifted since.
    """

    workspace_id: str
    lane_id: str
    decision_id: str
    lane_generation: int
    lane_revision: int
    issue: str
    audit_journal: str
    successor_issue: str
    successor_review_journal: str
    head: str
    integration_branch: str
    recorded_at: str = ""

    @property
    def route(self) -> DecisionRoute:
        return DecisionRoute(self.workspace_id, self.lane_id)

    def as_payload(self) -> dict:
        """A credential-free projection for operator output and durable records."""
        return {
            "decision_id": self.decision_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "lane_generation": self.lane_generation,
            "lane_revision": self.lane_revision,
            "issue": self.issue,
            "audit_journal": self.audit_journal,
            "successor_issue": self.successor_issue,
            "successor_review_journal": self.successor_review_journal,
            "head": self.head,
            "integration_branch": self.integration_branch,
            "recorded_at": self.recorded_at,
        }


def _validation_errors(decision: TerminalDecision) -> "tuple[str, ...]":
    """Why this decision cannot identify a terminal (empty iff usable; pure).

    Checked at WRITE time so a record that could never match anything is never stored: a decision
    the retire can only ever refuse is an operator trap, not a fence.
    """
    problems: list[str] = []
    for field in (
        "workspace_id",
        "lane_id",
        "issue",
        "audit_journal",
        "successor_issue",
        "successor_review_journal",
        "head",
        "integration_branch",
    ):
        if not str(getattr(decision, field, "") or "").strip():
            problems.append(f"{field} is empty")
    for field in ("lane_generation", "lane_revision"):
        value = getattr(decision, field, None)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            problems.append(f"{field} must be a positive integer")
    if decision.issue.strip() == decision.successor_issue.strip():
        problems.append("issue and successor_issue must differ")
    head = str(decision.head or "").strip()
    if head and (len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head)):
        problems.append("head must be a full lowercase 40/64-hex commit SHA")
    return tuple(problems)


class AuditFailureTerminalDecisionStore:
    """Read / write the coordinator's decisions, entirely through a stable directory descriptor.

    :meth:`record` is the coordinator's action and the ONLY writer; it creates the store on first
    use. :meth:`read` never creates anything and raises when the store is absent, replaced or
    unreadable, so a retire whose decision surface is gone refuses rather than proceeding on the
    records alone.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        declared = (
            Path(path) if path is not None else audit_failure_terminal_decision_path(home)
        )
        # Bind a relative declaration to this cwd once, without following any symlink.  The
        # component walk below then starts from the filesystem root and never resolves this string
        # as a whole.
        self.path = Path(os.path.abspath(os.fspath(declared)))
        #: The DECLARED home. Opened once per operation into a descriptor; every artifact is
        #: created and opened relative to THAT, never by re-resolving a path.
        self.home = self.path.parent
        self.sidecar_path = self.path.with_name(
            self.path.name + AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX
        )
        self.lock_path = self.path.with_name(
            self.path.name + AUDIT_FAILURE_TERMINAL_DECISION_LOCK_SUFFIX
        )

    # -- the stable directory descriptor -----------------------------------

    def _open_home_fd(self, *, create: bool) -> int:
        """Open/create the declared home by an anchored no-follow component walk.

        A path-wide ``lstat`` precheck followed by ``mkdir`` / ``open(path)`` has a swap window:
        the second operation resolves every ancestor again.  Instead this method opens the trusted
        filesystem root once, then opens (or creates) each component relative to the descriptor
        obtained for its parent.  A component swapped before its open is rejected by
        ``O_NOFOLLOW``; one swapped after its open cannot redirect later operations.
        """
        if self.home.anchor != os.sep:
            raise AuditFailureTerminalDecisionError(
                f"decision store home {self.home} is not rooted at the filesystem root; "
                "fail closed"
            )
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise AuditFailureTerminalDecisionError(
                "decision store component walk requires O_DIRECTORY and O_NOFOLLOW; "
                "this platform cannot enforce the no-follow contract, so fail closed"
            )
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            current_fd = os.open(os.sep, flags)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store filesystem root could not be opened "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        try:
            for component in self.home.parts[1:]:
                if not component or component in (".", ".."):
                    raise AuditFailureTerminalDecisionError(
                        f"decision store home {self.home} has an unsafe path component; "
                        "fail closed"
                    )
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise AuditFailureTerminalDecisionError(
                            f"decision store home {self.home} does not exist; fail closed"
                        ) from None
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        # A concurrent creator won.  The no-follow open below decides whether the
                        # winner is the directory component this walk is allowed to adopt.
                        pass
                    except OSError as exc:
                        raise AuditFailureTerminalDecisionError(
                            f"decision store home component {component} could not be created "
                            f"({type(exc).__name__}); fail closed"
                        ) from exc
                    try:
                        next_fd = os.open(component, flags, dir_fd=current_fd)
                    except OSError as exc:
                        raise AuditFailureTerminalDecisionError(
                            f"decision store home component {component} could not be opened "
                            f"after creation ({type(exc).__name__}); fail closed"
                        ) from exc
                except OSError as exc:
                    raise AuditFailureTerminalDecisionError(
                        f"decision store home component {component} could not be opened as a "
                        f"directory without following a link ({type(exc).__name__}); fail closed"
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
            result = current_fd
            current_fd = -1
            return result
        finally:
            if current_fd >= 0:
                os.close(current_fd)

    def _require_safe_lock(self, dir_fd: int, lock_fd: int) -> None:
        """Require one regular, single-linked lock still visible at the opened name."""
        try:
            opened = os.fstat(lock_fd)
            visible = os.stat(
                self.lock_path.name, dir_fd=dir_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_path.name} could not be verified "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        for label, info in (("opened", opened), ("visible", visible)):
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_path.name} is not a single-linked regular "
                    f"file at its {label} identity; fail closed"
                )
        if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_path.name} changed identity while opening; "
                "fail closed"
            )

    def _open_lock_fd(self, dir_fd: int, *, create: bool) -> Optional[int]:
        """Open the home-relative lock without following or blocking on an unsafe file type."""
        flags = (
            (os.O_RDWR if create else os.O_RDONLY)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        if create:
            flags |= os.O_CREAT
        try:
            lock_fd = os.open(self.lock_path.name, flags, 0o600, dir_fd=dir_fd)
        except FileNotFoundError:
            if not create:
                return None
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_path.name} disappeared while opening; "
                "fail closed"
            ) from None
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store lock {self.lock_path.name} could not be opened without "
                f"following a link ({type(exc).__name__}); fail closed"
            ) from exc
        try:
            self._require_safe_lock(dir_fd, lock_fd)
        except AuditFailureTerminalDecisionError:
            os.close(lock_fd)
            raise
        return lock_fd

    @contextmanager
    def _locked_home(self, *, create: bool, exclusive: bool) -> Iterator[int]:
        """Hold the home descriptor and one consistent store lock for an entire operation."""
        if fcntl is None:
            raise AuditFailureTerminalDecisionError(
                "decision store advisory locking is unavailable; refusing to access shared "
                "state without the required lock"
            )
        dir_fd = self._open_home_fd(create=create)
        lock_fd: Optional[int] = None
        locked = False
        try:
            lock_fd = self._open_lock_fd(dir_fd, create=create)
            if lock_fd is None:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.path} has no coordination lock; fail closed"
                )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                locked = True
            except OSError as exc:
                raise AuditFailureTerminalDecisionError(
                    f"decision store lock {self.lock_path.name} could not be acquired "
                    f"({type(exc).__name__}); fail closed"
                ) from exc
            # Refuse a replacement lock generation installed while this opener was waiting.
            self._require_safe_lock(dir_fd, lock_fd)
            yield dir_fd
        finally:
            if lock_fd is not None:
                if locked:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(lock_fd)
            os.close(dir_fd)

    def _read_file(self, dir_fd: int, name: str) -> Optional[str]:
        """Read one artifact relative to the home descriptor, never following a link."""
        try:
            fd = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be opened without following a link "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            return handle.read()

    def _replace_file(self, dir_fd: int, name: str, text: str) -> None:
        """Write one artifact atomically: exclusive temp, fsync, rename — all via the descriptor.

        ``O_EXCL | O_NOFOLLOW`` means the temp is a file this call created, never an existing link,
        and the rename happens inside the SAME directory descriptor, so the published name is
        replaced without the path ever being resolved again.
        """
        temp = f"{name}{_TEMP_SUFFIX}.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(temp, flags, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be staged "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError as exc:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            raise AuditFailureTerminalDecisionError(
                f"decision store artifact {name} could not be published "
                f"({type(exc).__name__}); fail closed"
            ) from exc

    # -- store identity ----------------------------------------------------

    def _read_sidecar_nonce(self) -> Optional[str]:
        try:
            with self._locked_home(create=False, exclusive=False) as dir_fd:
                value = self._read_file(dir_fd, self.sidecar_path.name)
        except AuditFailureTerminalDecisionError:
            return None
        return (value or "").strip() or None

    def is_initialized(self) -> bool:
        try:
            with self._locked_home(create=False, exclusive=False) as dir_fd:
                nonce = (self._read_file(dir_fd, self.sidecar_path.name) or "").strip()
                document = self._read_file(dir_fd, self.path.name)
        except AuditFailureTerminalDecisionError:
            return False
        if not nonce or document is None:
            return False
        try:
            parsed = json.loads(document)
        except ValueError:
            return False
        return (
            isinstance(parsed, dict)
            and parsed.get("store_nonce") == nonce
            and parsed.get("schema_version")
            == AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION
        )

    def _load(self, dir_fd: int) -> dict:
        """The records document, verified against the sidecar identity, or fail closed."""
        nonce = (self._read_file(dir_fd, self.sidecar_path.name) or "").strip()
        if not nonce:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} has no identity sidecar (never recorded / lost); "
                "fail closed rather than admit a terminal with no recorded decision"
            )
        document = self._read_file(dir_fd, self.path.name)
        if document is None:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is missing while its sidecar remains (store loss); "
                "fail closed rather than auto-create"
            )
        try:
            parsed = json.loads(document)
        except ValueError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is unreadable ({type(exc).__name__}); fail closed"
            ) from exc
        if not isinstance(parsed, dict):
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is not a decision document; fail closed"
            )
        if parsed.get("schema_version") != AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is not at schema version "
                f"{AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION} (empty / replaced / foreign "
                "store); fail closed"
            )
        if parsed.get("store_nonce") != nonce:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} nonce does not match its sidecar (replaced / "
                "foreign store); fail closed"
            )
        records = parsed.get("decisions")
        if not isinstance(records, dict):
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} carries no decision map; fail closed"
            )
        return parsed

    @staticmethod
    def _key(route: DecisionRoute) -> str:
        workspace, lane = route.as_row()
        return f"{workspace}\u0000{lane}"

    # -- the writer boundary -----------------------------------------------

    def _require_attested_coordinator(self, repo_root: Path) -> str:
        """Verify the WRITER is the attested coordinator, or refuse (review j#102147 finding 1).

        The same action-time gate #13613 requires before a lane mutation
        (:func:`...sublane_actuator_herdr_preflight.evaluate_dispatch_sender`): the sender identity
        is resolved from THIS PROCESS's environment and cross-checked against the repository's
        workspace anchor, the committed coordinator provider binding, and the coordinator default
        lane. Env presence alone is not attestation.

        It lives here, not in the command, because a writer boundary a direct store call can bypass
        is not a boundary. The import is lazy so this low-level module never pulls the application
        layer at import time.

        **This does not close the writer question** — review j#102181 showed the material it reads
        can itself be created by the caller — which is why the route it feeds is inert. See
        :data:`...superseded_audit_failure_terminal.RECEIPT_AUTHORITY_RESOLVABLE`.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
            evaluate_dispatch_sender,
        )

        ok, detail = evaluate_dispatch_sender(os.environ, Path(repo_root))
        if not ok:
            raise AuditFailureTerminalDecisionError(
                "refusing to record an audit-failure terminal decision: the writer is not the "
                f"attested coordinator for this repository ({detail}). A decision is a coordinator "
                "judgement, so an unattested, foreign-workspace or non-coordinator caller records "
                "nothing"
            )
        return detail

    def record(
        self,
        decision: TerminalDecision,
        *,
        repo_root: Path,
        now: Optional[str] = None,
    ) -> TerminalDecision:
        """Record ONE coordinator decision for a route, minting its id (the writer path).

        ``repo_root`` is the repository whose ANCHOR the writer is attested against — not a claim
        about who the writer is, but the independent side of that comparison.

        Latest-wins per route: a lane whose head or generation moved needs the coordinator to decide
        again about the world that now exists. The replacement is still bound to its own exact
        identities, and the retire re-measures every one of them.

        Raises on an unattested writer, on a decision that could never match, and on any artifact
        that cannot be created or opened through the home descriptor without following a link.
        """
        self._require_attested_coordinator(repo_root)
        problems = _validation_errors(decision)
        if problems:
            raise AuditFailureTerminalDecisionError(
                "refusing to record an audit-failure terminal decision that can never match: "
                + "; ".join(problems)
            )
        stamp = now or _utc_now()
        recorded = TerminalDecision(
            workspace_id=decision.workspace_id.strip(),
            lane_id=decision.lane_id.strip(),
            decision_id=mint_decision_id(),
            lane_generation=decision.lane_generation,
            lane_revision=decision.lane_revision,
            issue=decision.issue.strip(),
            audit_journal=decision.audit_journal.strip(),
            successor_issue=decision.successor_issue.strip(),
            successor_review_journal=decision.successor_review_journal.strip(),
            head=decision.head.strip().lower(),
            integration_branch=decision.integration_branch.strip(),
            recorded_at=stamp,
        )
        with self._locked_home(create=True, exclusive=True) as dir_fd:
            existing = self._read_file(dir_fd, self.sidecar_path.name)
            nonce = (existing or "").strip()
            if not nonce:
                if self._read_file(dir_fd, self.path.name) is not None:
                    raise AuditFailureTerminalDecisionError(
                        f"decision store {self.path} exists without its identity sidecar "
                        "(replaced / half-written store); refusing to write into it"
                    )
                nonce = secrets.token_hex(16)
                document = {
                    "schema_version": AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION,
                    "store_nonce": nonce,
                    "decisions": {},
                }
                self._replace_file(dir_fd, self.sidecar_path.name, nonce)
            else:
                document = self._load(dir_fd)
            document["decisions"][self._key(recorded.route)] = recorded.as_payload()
            self._replace_file(
                dir_fd,
                self.path.name,
                json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2),
            )
        return recorded

    # -- the retire's read -------------------------------------------------

    def read(self, route: DecisionRoute) -> Optional[TerminalDecision]:
        """The decision recorded for ``route``, or ``None`` when the route has none.

        Raises rather than returning ``None`` when the STORE itself cannot be trusted: "this lane
        has no decision" and "the decision surface is gone" are different operational problems, and
        both refuse.
        """
        with self._locked_home(create=False, exclusive=False) as dir_fd:
            document = self._load(dir_fd)
            payload = document["decisions"].get(self._key(route))
            if not isinstance(payload, dict):
                return None
            workspace_id, lane_id = route.as_row()
            try:
                return TerminalDecision(
                    workspace_id=workspace_id,
                    lane_id=lane_id,
                    decision_id=str(payload["decision_id"]),
                    lane_generation=int(payload["lane_generation"]),
                    lane_revision=int(payload["lane_revision"]),
                    issue=str(payload["issue"]),
                    audit_journal=str(payload["audit_journal"]),
                    successor_issue=str(payload["successor_issue"]),
                    successor_review_journal=str(payload["successor_review_journal"]),
                    head=str(payload["head"]),
                    integration_branch=str(payload["integration_branch"]),
                    recorded_at=str(payload.get("recorded_at", "")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.path} carries an unreadable record for this route "
                    f"({type(exc).__name__}); fail closed"
                ) from exc


__all__ = (
    "AUDIT_FAILURE_TERMINAL_DECISION_FILENAME",
    "AUDIT_FAILURE_TERMINAL_DECISION_LOCK_SUFFIX",
    "AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION",
    "AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX",
    "AuditFailureTerminalDecisionError",
    "AuditFailureTerminalDecisionStore",
    "DecisionRoute",
    "TerminalDecision",
    "audit_failure_terminal_decision_path",
    "mint_decision_id",
)
