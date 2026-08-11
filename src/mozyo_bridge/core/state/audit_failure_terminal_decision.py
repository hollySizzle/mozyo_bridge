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
publication sequence with ``LOCK_EX``. Its random token AND opened inode generation are bound into
the JSON document: copying the old token into a replacement inode does not create a second valid
serialization generation. Every operation revalidates that same visible generation before and
after its snapshot; writers additionally revalidate immediately before and after publication.
Rollback intent is armed before rename; the renamed canonical inode is checked against the staged
inode, and an existing JSON's exact old inode is retained as a durable home-relative hard link
before publication. Rollback atomically renames that retained inode back without truncating or
rewriting it. A rollback, write or fsync failure therefore cannot turn an earlier complete decision
document into a partial/empty JSON. If the canonical target is already foreign or exact rollback
cannot complete, the foreign target is preserved and the visible lock is atomically replaced by a
poison generation. The durable rollback link is also an operation-scoped marker: any later
operation that did not create that exact marker refuses it. An independent publication-intent name
is armed and made durable before the writer starts its original snapshot, then remains until exact
readback, so snapshot drift, pre-retention foreign replacement, fresh stage ABA or total poison
failure remains a refusal even across process stop. Compound failure cannot become authority later.
Removing the last rollback marker after the durable canonical readback is the irreversible commit
point; cleanup fsync, descriptor-close or wrapper/signal exceptions after that point do not roll the
committed decision back or misreport it as a failed write. A marker resurrected after reboot makes
the store refuse.
Reads and initialization probes hold ``LOCK_SH`` through their complete snapshot, and never create
a missing home or lock. Once either store artifact exists, a missing lock is store loss, never an
invitation to create a new generation. Missing, replaced or unsafe lock identity is a typed
fail-closed refusal.

Store identity mirrors the sibling fences: nonce bytes AND the sidecar inode generation are bound
into JSON, so a copied nonce on a replacement inode makes a deleted / replaced / foreign store
fail CLOSED. Unlike them there is no ``bootstrap`` / ``recover`` ceremony, because the
asymmetry here is simpler and safer: :meth:`record` — the coordinator's own action — creates the
store, and every read path refuses when it is absent. A lost store therefore cannot silently admit
anything; it can only require the coordinator to decide again.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.audit_failure_terminal_decision_lock import (
    AuditFailureTerminalDecisionError,
    DecisionStoreFileCoordinator,
    LockedHome,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION = 2
AUDIT_FAILURE_TERMINAL_DECISION_SIDECAR_SUFFIX = ".nonce"
AUDIT_FAILURE_TERMINAL_DECISION_LOCK_SUFFIX = ".lock"
#: The records file's basename under the home. A plain JSON document, replaced whole: one record
#: per lane route, serialized by a home-relative advisory lock, and — decisively — a format this
#: module can write through a descriptor.
AUDIT_FAILURE_TERMINAL_DECISION_FILENAME = "audit-failure-terminal-decision.json"


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


def _artifact_generation_payload(identity: tuple[int, int, int]) -> dict:
    return {"device": identity[0], "inode": identity[1], "ctime_ns": identity[2]}


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


@dataclass(frozen=True)
class _StoreSnapshot:
    document: dict
    document_text: str
    document_identity: tuple[int, int, int]
    nonce_text: str


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
        self._files = DecisionStoreFileCoordinator(
            home=self.home,
            document_name=self.path.name,
            sidecar_name=self.sidecar_path.name,
            lock_name=self.lock_path.name,
            display_path=self.path,
        )

    # -- store identity ----------------------------------------------------

    def _read_sidecar_nonce(self) -> Optional[str]:
        try:
            with self._files.locked_home(create=False, exclusive=False) as scope:
                value = self._load(scope).nonce_text
        except AuditFailureTerminalDecisionError:
            return None
        return (value or "").strip() or None

    def is_initialized(self) -> bool:
        try:
            with self._files.locked_home(create=False, exclusive=False) as scope:
                self._load(scope)
        except AuditFailureTerminalDecisionError:
            return False
        return True

    def _require_known_rollback_markers(self, scope: LockedHome) -> None:
        prefixes = (self.path.name + ".rollback.", self.path.name + ".intent.")
        try:
            observed = sorted(
                name for name in os.listdir(scope.dir_fd) if name.startswith(prefixes)
            )
        except OSError as exc:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} rollback markers could not be inspected; fail closed"
            ) from exc
        expected = sorted(
            name
            for publication in scope.publications
            for name in (publication.rollback_name, publication.intent_name)
            if name is not None
        )
        if observed != expected:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} has an unresolved publication marker; fail closed"
            )

    def _load(self, scope: LockedHome) -> _StoreSnapshot:
        """The records document, verified against the sidecar identity, or fail closed."""
        self._files.require_lock_generation(scope)
        self._require_known_rollback_markers(scope)
        nonce_text, nonce_identity = self._files.read_file_snapshot(
            scope.dir_fd, self.sidecar_path.name
        )
        nonce = (nonce_text or "").strip()
        if not nonce or nonce_identity is None:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} has no identity sidecar (never recorded / lost); "
                "fail closed rather than admit a terminal with no recorded decision"
            )
        document_text, document_identity = self._files.read_file_snapshot(
            scope.dir_fd, self.path.name
        )
        if document_text is None or document_identity is None:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is missing while its sidecar remains (store loss); "
                "fail closed rather than auto-create"
            )
        try:
            parsed = json.loads(document_text)
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
        if parsed.get("store_nonce_generation") != _artifact_generation_payload(
            nonce_identity
        ):
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is bound to a different nonce inode generation; "
                "refusing a replaced or copied identity sidecar"
            )
        if parsed.get("lock_generation") != scope.generation.as_payload():
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} is bound to a different lock generation; refusing "
                "a replaced, recreated or copied coordination lock"
            )
        records = parsed.get("decisions")
        if not isinstance(records, dict):
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} carries no decision map; fail closed"
            )
        if self._files.artifact_identity(
            scope.dir_fd, self.sidecar_path.name
        ) != nonce_identity:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} nonce identity drifted during its snapshot; "
                "fail closed"
            )
        if self._files.artifact_identity(
            scope.dir_fd, self.path.name
        ) != document_identity:
            raise AuditFailureTerminalDecisionError(
                f"decision store {self.path} JSON identity drifted during its snapshot; "
                "fail closed"
            )
        self._require_known_rollback_markers(scope)
        self._files.require_lock_generation(scope)
        return _StoreSnapshot(
            document=parsed,
            document_text=document_text,
            document_identity=document_identity,
            nonce_text=nonce_text or "",
        )

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
        with self._files.locked_home(create=True, exclusive=True) as scope:
            publication = self._files.arm_publication(scope, self.path.name)
            existing_nonce = self._files.read_file(scope.dir_fd, self.sidecar_path.name)
            existing_document = self._files.read_file(scope.dir_fd, self.path.name)
            if scope.bootstrap_nonce is not None:
                if (
                    scope.bootstrap_identity is None
                    or existing_nonce != scope.bootstrap_nonce
                    or existing_document is not None
                ):
                    raise AuditFailureTerminalDecisionError(
                        f"decision store {self.path} changed during first-use bootstrap; fail closed"
                    )
                nonce = scope.bootstrap_nonce
                document = {
                    "schema_version": AUDIT_FAILURE_TERMINAL_DECISION_SCHEMA_VERSION,
                    "store_nonce": nonce,
                    "store_nonce_generation": _artifact_generation_payload(
                        scope.bootstrap_identity
                    ),
                    "lock_generation": scope.generation.as_payload(),
                    "decisions": {},
                }
                original_document = None
                original_document_identity = None
                self._files.bind_publication_snapshot(publication, None, None)
            else:
                if existing_nonce is None or existing_document is None:
                    raise AuditFailureTerminalDecisionError(
                        f"decision store {self.path} has only one of its JSON/nonce artifacts "
                        "(replaced / half-written store); refusing to write into it"
                    )
                snapshot = self._load(scope)
                document = snapshot.document
                original_document = snapshot.document_text
                original_document_identity = snapshot.document_identity
                self._files.bind_publication_snapshot(
                    publication, original_document, original_document_identity
                )
            document["decisions"][self._key(recorded.route)] = recorded.as_payload()
            intended_document = json.dumps(
                document, ensure_ascii=False, sort_keys=True, indent=2
            )
            self._files.publish_file(
                scope,
                self.path.name,
                intended_document,
                publication=publication,
            )
            # Verify the whole effective document, not only this route: a mutated staged inode must
            # not erase an earlier successful route while retaining the just-written route.
            if self._load(scope).document_text != intended_document:
                raise AuditFailureTerminalDecisionError(
                    f"decision store {self.path} did not retain the exact published document; "
                    "rolling back rather than reporting false success"
                )
        return recorded

    # -- the retire's read -------------------------------------------------

    def read(self, route: DecisionRoute) -> Optional[TerminalDecision]:
        """The decision recorded for ``route``, or ``None`` when the route has none.

        Raises rather than returning ``None`` when the STORE itself cannot be trusted: "this lane
        has no decision" and "the decision surface is gone" are different operational problems, and
        both refuse.
        """
        with self._files.locked_home(create=False, exclusive=False) as scope:
            document = self._load(scope).document
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
