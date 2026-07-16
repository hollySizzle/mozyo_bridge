"""Lane lifecycle — disposition + process-release CAS component (Redmine #13689).

The durable **desired lifecycle** of a sublane unit, kept apart from the three
things it is routinely confused with (design consultation j#76734, coordinator
Design Answer j#76741):

- **route identity** (:mod:`...domain.route_identity_ledger`) is *where to send*.
  A lane's lifecycle must never become a routing key, so no field of this
  component enters :class:`RouteIdentity`.
- **lane metadata** (:mod:`mozyo_bridge.core.state.lane_metadata`) is the
  token→label *display join*, explicitly "never routing authority". Its
  ``upsert`` deliberately **revives a tombstone** (a re-created lane is active
  again), and it carries no CAS — so an out-of-order write would silently undo a
  supersede / hibernate. Lifecycle authority cannot live there; the two stay
  separate and their drift is a *diagnostic*, never an implicit repair.
- **process presence** is a live-inventory fact. :data:`RELEASE_RELEASED` is the
  recorded outcome of a release *command*, **not** proof that the slots are gone:
  a reader that needs liveness re-reads the live herdr inventory (Design Answer
  D3; ``managed-state-model.md`` ``### 正本境界`` keeps ``observed_liveness`` on
  the runtime).

State kind (``vibes/docs/logics/managed-state-model.md``
``### state kind ownership / recovery matrix``): ``desired_current_state``, a
**native component** of the consolidated home-scoped ``state.sqlite`` — it shares
the container guard (:func:`~...state_store.connect_state_container_rw`) and
self-registers in ``state_schema_components`` with no ``migrated_from`` (there is
no legacy file), exactly like the sibling native :mod:`...lane_metadata`.

Recovery policy ``operator_current_state``: a coordinator's supersede / hibernate
*decision* cannot be rebuilt from events, so loss requires an explicit re-declare
from the Redmine durable pointer. Unlike ``lane_metadata`` (which fails **open**
to a raw token so a display degrades rather than aborts), every reader here fails
**closed**: an absent / unreadable store yields :data:`OWNER_UNKNOWN`, never an
inferred ``active`` (Design Answer D1). Guessing ``active`` would re-authorize a
send into a superseded lane — the exact failure this component exists to prevent.

Owner exact-one (Design Answer D2, correcting the consultation's proposal): the
partial unique index is scoped to the **workspace**
(``(repo_workspace_id, issue_id)`` where the row is ``active`` and the issue is
non-empty). A home-global ``UNIQUE(issue_id)`` would collide across unrelated
projects that legitimately share an issue number. The index gives *at most one*
active owner; :meth:`LaneLifecycleStore.resolve_owner` supplies the *exactly one*
half by failing closed on zero / many / stale rows.

Redmine boundary: the row stores a durable **pointer** (source kind + issue id +
journal id) to the coordinator decision that set it. Journal bodies, issue status,
and approvals are ``workflow_truth`` and are never copied into the DB
(``managed-state-model.md``: "Redmine durable record で復旧。runtime DB へ複製しない").

Concurrency: writes are CAS. Every transition takes ``BEGIN IMMEDIATE`` and
matches an **exact expected state + revision** (and, for a release, the exact
action generation) — the discipline of
:meth:`...forward_outbox_fence.ForwardOutboxFence._guarded_set`. A stale,
duplicate, or out-of-order caller updates nothing and is told why
(:class:`CasOutcome`), rather than clobbering a newer decision.

Note the container guard returns a *default-isolation* connection, which cannot
drive ``BEGIN IMMEDIATE``; like
:meth:`...workflow_runtime_store.WorkflowRuntimeStore.acquire_generation_lease`,
this component uses the guard only to create / validate the container, then opens
its own autocommit connection for the CAS itself.

This module is the **store** half of the component: schema, registration, and the
guarded writes. The closed vocabularies, the transition matrix, and the typed
records are the pure
:mod:`mozyo_bridge.core.state.lane_lifecycle_model`, re-exported here so callers
have a single import surface.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    BINDING_KIND_PROJECT_GATEWAY,
    BINDING_KINDS,
    DECISION_SOURCE_REDMINE,
    DECISION_SOURCES,
    DecisionPointer,
    DecisionPointerError,
    ProcessGenerationPin,
    ProcessPinError,
    ReleasePinError,
    encode_declared_slots,
    recovery_refusal,
    rehydrate_allowed,
    replacement_settled,
    validate_declared_slots,
    validate_release_pins,
    CAS_ACTION_MISMATCH,
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_OWNER_CONFLICT,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DISPOSITION_SUPERSEDED,
    DISPOSITIONS,
    RECONCILE_PHASE_NONE,
    RECONCILE_PHASE_RECONCILED,
    RECONCILE_PHASES,
    OWNER_ABSENT,
    OWNER_AMBIGUOUS,
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    RELEASE_REQUESTED,
    RELEASE_STATES,
    REPLACEMENT_NOT_REQUESTED,
    CasOutcome,
    LaneLifecycleKey,
    LaneLifecycleRecord,
    OwnerResolution,
    ReleasePin,
    decode_release_pins,
    disposition_transition_allowed,
    encode_release_pins,
    guard,
    norm,
    release_transition_allowed,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_RECOVERY_POLICY,
    LANE_LIFECYCLE_SCHEMA_VERSION,
    TABLE as _TABLE,
    LaneLifecycleError,
    LifecycleSchemaOutcome,
    ensure_lane_lifecycle_schema,
    lane_lifecycle_path,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import (
    _active_owner,
    _insert_active_row,
    _locked_row,
    _rollback,
    _utc_now,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import (
    LaneLifecycleReader,
    LaneLifecycleReaderUpgradeRequired,
    LifecycleMigrationPreflight,
    LifecycleWritePreparation,
    emit_lifecycle_migration_advisory,
    lifecycle_migration_preflight,
    load_lane_lifecycle_readonly,
)


# -- store -------------------------------------------------------------------


class LaneLifecycleStore:
    """CAS store for lane disposition + process release (native ``state.sqlite``)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self.path = path if path is not None else lane_lifecycle_path(home)
        #: The typed outcome of the LAST schema-ensuring write open (Redmine #13844 F1). A
        #: mutating open never SILENTLY discards what the schema gate did — created / intact /
        #: migrated{from_version, backup_dir} is captured here for a caller / journal to read.
        self._last_schema_outcome: Optional[LifecycleSchemaOutcome] = None
        #: The full explicit-write-gate result of the LAST mutating open (Redmine #13844 R2):
        #: the compatibility preflight (peer old-reader lanes) + the typed schema outcome, so a
        #: command can surface a migration and its peer risk regardless of WHICH mutation ran.
        self._last_write_preparation: Optional[LifecycleWritePreparation] = None

    # -- schema / connections ------------------------------------------------

    @property
    def last_schema_outcome(self) -> Optional[LifecycleSchemaOutcome]:
        """The typed outcome of the most recent schema-ensuring write open (or ``None``)."""
        return self._last_schema_outcome

    @property
    def last_write_preparation(self) -> Optional[LifecycleWritePreparation]:
        """The explicit write gate's typed result from the most recent mutation (or ``None``)."""
        return self._last_write_preparation

    def ensure_schema(self) -> LifecycleSchemaOutcome:
        """Create / validate this component's schema; return the typed outcome (see schema module)."""
        outcome = ensure_lane_lifecycle_schema(self.path)
        self._last_schema_outcome = outcome
        return outcome

    def _run_write_gate(
        self,
        writer_workspace_id: Optional[str],
        writer_lane_id: Optional[str],
        *,
        emit_advisory: bool,
    ) -> LifecycleWritePreparation:
        """The explicit schema-changing WRITE gate shared by EVERY mutation (Redmine #13844 R2/R3).

        The single production choke point for a schema-needing mutation, in strict order:

        1. read the compatibility **preflight** on the STILL-OLD store (version-compatible,
           read-only, never a migration trigger) — the active peer lanes a forward migration
           would fail-close;
        2. **BEFORE migrating**, when ``emit_advisory`` and a migration is pending with peers at
           risk, emit the operator advisory to stderr — a genuine PRE-migration warning while the
           shared store is still the old version (Redmine #13844 R3-F1), not a post-hoc notice;
        3. run the backup-first migration and capture its **typed outcome** (created / intact /
           migrated{from_version, backup_dir});
        4. record BOTH — the pre-migration ``preflight`` and the post ``outcome``, kept distinct
           (R3-F1) — on ``last_write_preparation``.

        This covers disposition / supersede / release / declaration / replacement / retire /
        reconcile alike, not only declaration.
        """
        preflight = lifecycle_migration_preflight(
            path=self.path,
            writer_workspace_id=writer_workspace_id,
            writer_lane_id=writer_lane_id,
        )
        if emit_advisory:
            # PRE-migration: the store is still the old version at this point (ensure_schema
            # below has not run), so the operator sees the peer risk BEFORE it is realized.
            emit_lifecycle_migration_advisory(preflight, stream=sys.stderr)
        outcome = self.ensure_schema()
        prep = LifecycleWritePreparation(outcome=outcome, preflight=preflight)
        # Redmine #13844 R4-F1: PRESERVE the migration across a command's multiple writes on this
        # store. A command that mutates the row several times (e.g. quarantine: request_replacement
        # then record_replacement_outcome) migrates the shared store on its FIRST write (v5 -> v6);
        # every LATER write finds it already current (``intact``). ``last_write_preparation`` must
        # remain the migration event that actually happened for this store's lifetime — so a
        # ``migrated`` preparation is never clobbered by a subsequent ``intact`` one (migrations are
        # monotonic: v5 -> v6 happens once). ``last_schema_outcome`` still reflects the raw last
        # write; only this audit bundle accumulates the migration.
        if self._last_write_preparation is None or not self._last_write_preparation.migrated:
            self._last_write_preparation = prep
        return prep

    def prepare_write(
        self,
        *,
        writer_workspace_id: Optional[str] = None,
        writer_lane_id: Optional[str] = None,
        emit_advisory: bool = False,
    ) -> LifecycleWritePreparation:
        """Run the explicit write gate and return its typed result (Redmine #13844 design 3/6).

        The public entry for a caller that wants to gate + inspect a migration; the CAS methods
        take the same gate via :meth:`_connect_write`. ``emit_advisory`` (default off for the
        programmatic entry) prints the PRE-migration advisory to stderr before migrating. The
        preflight is read on ``self.path`` (the exact store this write targets).
        """
        return self._run_write_gate(
            writer_workspace_id, writer_lane_id, emit_advisory=emit_advisory
        )

    def _open_autocommit_conn(self) -> sqlite3.Connection:
        """A plain autocommit connection (no schema ensure) — the gate ran separately."""
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _connect_write(
        self, writer_key: Optional[LaneLifecycleKey] = None
    ) -> sqlite3.Connection:
        """The universal mutating write open (Redmine #13844 R2/R3): run the explicit write gate
        — read the preflight, emit the PRE-migration advisory, THEN migrate and capture the typed
        outcome on ``last_write_preparation`` — BEFORE opening the autocommit connection. EVERY
        schema-needing CAS goes through here, so no mutation migrates the shared store implicitly
        and the peer-reader risk is surfaced to the operator BEFORE the migration (not after).
        ``writer_key`` (the lane being written) is excluded from the peer set."""
        self._run_write_gate(
            writer_key.repo_workspace_id if writer_key is not None else None,
            writer_key.lane_id if writer_key is not None else None,
            emit_advisory=True,
        )
        return self._open_autocommit_conn()

    # -- reads (NON-migrating: Redmine #13844 read-not-migrate) ---------------

    def get(self, key: LaneLifecycleKey) -> Optional[LaneLifecycleRecord]:
        """The lane's row, or ``None`` when it has none. Raises when unreadable.

        Reads through the NON-migrating version-compatible reader (Redmine #13844 R2): a read —
        even one inside a mutating flow — must never forward-migrate the shared store, so the
        migration only ever happens at the actual write via :meth:`_connect_write` (after the
        preflight). An absent store reads as "no row" without creating anything.
        """
        return LaneLifecycleReader(path=self.path).get(key)

    def records(self) -> tuple[LaneLifecycleRecord, ...]:
        """Every row via the NON-migrating reader (Redmine #13844 R2). Raises when unreadable."""
        return LaneLifecycleReader(path=self.path).records()

    def resolve_owner(self, repo_workspace_id: str, issue_id: str) -> OwnerResolution:
        """The issue's single active owning lane in this workspace, via the NON-migrating reader.

        Exactly one active row resolves. Zero (:data:`OWNER_ABSENT`), many
        (:data:`OWNER_AMBIGUOUS`), or an empty query resolves to **no owner** — a caller must
        not fall back to "the newest lane" or a provider / pane name. Reads never migrate
        (Redmine #13844 R2), so a resolve-then-write flow never migrates before its write gate.
        """
        return LaneLifecycleReader(path=self.path).resolve_owner(
            repo_workspace_id, issue_id
        )

    # -- writes (CAS) --------------------------------------------------------

    def declare_active(
        self,
        key: LaneLifecycleKey,
        *,
        decision: DecisionPointer,
        issue_id: str = "",
        worktree_identity: str = "",
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Declare a fresh lane ``active`` / ``not_requested`` at revision 1.

        ``issue_id`` is the lane's **owner binding** and may be empty — an unbound
        lane owns no issue (Design Answer D2). ``decision`` is the **durable anchor**
        of the record that declared it and is always complete, unbound or not
        (R2-F1). When the lane *is* bound, the two must name the same issue: a
        decision filed on an unrelated ticket does not authorize this ownership.

        ``worktree_identity`` (v4, Redmine #13754) is the lane's canonical worktree
        token, written here at authoritative create time so ``retire --execute`` can
        prove the caller's ``--worktree`` belongs to the lane before closing. Empty is
        allowed (a lane whose worktree is not yet bound), and reads back fail-closed at
        retire — never a guessed binding.

        Refuses an existing lane (:data:`CAS_ALREADY_DECLARED`) — a re-declare must
        go through an explicit transition, never a silent overwrite (the
        tombstone-reviving ``lane_metadata.upsert`` is the anti-pattern). Refuses
        (:data:`CAS_OWNER_CONFLICT`) when the issue already has an active owner in
        this workspace: the storage index, not a later check, is what makes double
        ownership impossible.
        """
        issue = norm(issue_id)
        if not decision.authorizes_binding(issue):
            raise DecisionPointerError(
                f"decision is anchored to issue {decision.issue_id!r} but the lane "
                f"is being bound to {issue!r}"
            )
        worktree = norm(worktree_identity)
        stamp = now or _utc_now()
        conn = self._connect_write(key)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _locked_row(conn, key)
            if existing is not None:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=existing.revision,
                )
            if issue and _active_owner(conn, key.repo_workspace_id, issue):
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_OWNER_CONFLICT)
            try:
                # An issue-kind lane at generation 1 with no declared-slot snapshot: the
                # v5 binding/generation/declaration columns land on their additive defaults
                # (Redmine #13810). ``declare_lane`` is the surface that declares a
                # project-gateway lane or carries a declared-slot set.
                _insert_active_row(
                    conn,
                    key=key,
                    issue=issue,
                    decision=decision,
                    revision=1,
                    stamp=stamp,
                    worktree=worktree,
                )
            except sqlite3.IntegrityError:
                # The index is the backstop the pre-checks above should have caught.
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_OWNER_CONFLICT)
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=1)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane lifecycle declare failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def transition_disposition(
        self,
        key: LaneLifecycleKey,
        *,
        expected_disposition: str,
        expected_revision: int,
        target: str,
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """CAS the lane's disposition, guarded on its exact state + revision.

        ``decision`` is required and replaces the stored pointer (R1-F5): the row must
        always name the durable record that put it in its *current* state, never an
        inherited one from an earlier write.

        Rehydrating (``hibernated -> active``) clears the release generation, but only
        a *finished* one — :func:`rehydrate_allowed` refuses while a generation is in
        flight (R1-F3), so a lane whose panes an actuator is still closing cannot slip
        back into the active roster.
        """
        if target not in DISPOSITIONS:
            raise ValueError(f"unknown lane disposition {target!r}")
        stamp = now or _utc_now()
        conn = self._connect_write(key)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            refusal = guard(current, expected_disposition, expected_revision)
            if refusal is not None:
                conn.execute("ROLLBACK")
                return refusal
            if not decision.authorizes_binding(current.issue_id):
                # A bound lane may only be decided by a record filed on its own issue;
                # an unbound lane accepts any complete anchor (R2-F1).
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if not disposition_transition_allowed(current.lane_disposition, target):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if not replacement_settled(current.replacement_state):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            rehydrating = target == DISPOSITION_ACTIVE
            if rehydrating and not rehydrate_allowed(current.process_release):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if rehydrating and current.issue_id:
                # While this lane slept, another lane may have taken its issue. Coming
                # back as a second active owner is exactly the state the owner index
                # forbids — refuse rather than let the storage engine raise.
                owner = _active_owner(conn, key.repo_workspace_id, current.issue_id)
                if owner is not None and owner != key.lane_id:
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=False,
                        reason=CAS_OWNER_CONFLICT,
                        revision=current.revision,
                    )
            release = RELEASE_NOT_REQUESTED if rehydrating else current.process_release
            action = "" if rehydrating else current.release_action_id
            pins = "" if rehydrating else current.release_pins
            replacement = (
                REPLACEMENT_NOT_REQUESTED
                if rehydrating
                else current.replacement_state
            )
            replacement_action = "" if rehydrating else current.replacement_action_id
            replacement_pins = "" if rehydrating else current.replacement_pins
            revision = current.revision + 1
            try:
                conn.execute(
                    f"UPDATE {_TABLE} SET lane_disposition = ?, process_release = ?, "
                    "release_action_id = ?, release_pins = ?, replacement_state = ?, "
                    "replacement_action_id = ?, replacement_pins = ?, revision = ?, "
                    "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                    "updated_at = ? "
                    "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                    (
                        target,
                        release,
                        action,
                        pins,
                        replacement,
                        replacement_action,
                        replacement_pins,
                        revision,
                        decision.source,
                        decision.issue_id,
                        decision.journal_id,
                        stamp,
                        key.repo_workspace_id,
                        key.lane_id,
                        current.revision,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_OWNER_CONFLICT,
                    revision=current.revision,
                )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane lifecycle transition failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def supersede_and_activate(
        self,
        *,
        superseded: LaneLifecycleKey,
        expected_revision: int,
        recovery: LaneLifecycleKey,
        decision: DecisionPointer,
        recovery_expected_disposition: Optional[str] = None,
        recovery_expected_revision: Optional[int] = None,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Hand an issue's ownership to a recovery lane in **one** transaction.

        The old owner goes ``active -> superseded`` and the recovery lane becomes the
        active owner atomically. There is no instant at which the issue has two active
        owners (the partial unique index would reject it) nor zero (a reader between
        two separate writes would have failed closed on ``absent``).

        The issue whose ownership moves is ``decision.issue_id`` — the durable record
        that decided the handover (R1-F5). Both lanes must live in the **same
        workspace** (R1-F1): ownership is a workspace-scoped fact, so moving it across
        workspaces is not a handover but two unrelated writes, and the owner index
        would not even see the conflict.

        The recovery lane is CAS-guarded on its own expected state + revision when it
        already exists (R1-F2). Without that, a caller holding only the *old* lane's
        revision could overwrite whatever the recovery lane happens to be doing —
        including wiping an in-flight release generation. Pass
        ``recovery_expected_disposition`` / ``recovery_expected_revision`` for an
        existing recovery lane, and neither for a fresh one.

        A recovery lane already bound to a *different* issue is refused
        (:data:`CAS_OWNER_CONFLICT`, R1-F1): promoting it would silently strip that
        issue of its owner. Only an unbound lane, or one already bound to this issue,
        may be activated.

        ``revision`` in the outcome is the *recovery* lane's.
        """
        issue = decision.issue_id
        if not issue:
            raise ValueError("supersession requires the issue whose ownership moves")
        if superseded == recovery:
            raise ValueError("a lane cannot supersede itself")
        if superseded.repo_workspace_id != recovery.repo_workspace_id:
            raise ValueError(
                "supersession is workspace-scoped: "
                f"{superseded.repo_workspace_id!r} != {recovery.repo_workspace_id!r}"
            )
        stamp = now or _utc_now()
        # The superseded lane is this workspace's writer; exclude it from the peer set.
        conn = self._connect_write(superseded)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, superseded)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            refusal = guard(current, DISPOSITION_ACTIVE, expected_revision)
            if refusal is not None:
                conn.execute("ROLLBACK")
                return refusal
            if current.issue_id != issue:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if not replacement_settled(current.replacement_state):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            incoming = _locked_row(conn, recovery)
            refusal = recovery_refusal(
                incoming,
                issue=issue,
                expected_disposition=recovery_expected_disposition,
                expected_revision=recovery_expected_revision,
            )
            if refusal is not None:
                conn.execute("ROLLBACK")
                return refusal
            holder = _active_owner(conn, superseded.repo_workspace_id, issue)
            if holder is not None and holder not in (
                superseded.lane_id,
                recovery.lane_id,
            ):
                # A third lane already actively owns the issue: this handover is not
                # ours to make.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_OWNER_CONFLICT,
                    revision=current.revision,
                )
            try:
                conn.execute(
                    f"UPDATE {_TABLE} SET lane_disposition = ?, revision = ?, "
                    "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                    "updated_at = ? "
                    "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                    (
                        DISPOSITION_SUPERSEDED,
                        current.revision + 1,
                        decision.source,
                        decision.issue_id,
                        decision.journal_id,
                        stamp,
                        superseded.repo_workspace_id,
                        superseded.lane_id,
                        current.revision,
                    ),
                )
                if incoming is None:
                    revision = 1
                    # A supersede handover creates an issue-kind recovery lane at
                    # generation 1. Its worktree binding (Redmine #13754) and any
                    # declared-slot snapshot (Redmine #13810) are written when that lane is
                    # actually created / declared, not here — empty defaults, so its execute
                    # retire fails closed until then, never a guessed binding.
                    _insert_active_row(
                        conn,
                        key=recovery,
                        issue=issue,
                        decision=decision,
                        revision=revision,
                        stamp=stamp,
                    )
                else:
                    revision = incoming.revision + 1
                    conn.execute(
                        f"UPDATE {_TABLE} SET issue_id = ?, lane_disposition = ?, "
                        "process_release = ?, release_action_id = ?, release_pins = ?, "
                        "replacement_state = ?, replacement_action_id = ?, "
                        "replacement_pins = ?, revision = ?, decision_source = ?, decision_issue_id = ?, "
                        "decision_journal = ?, "
                        "updated_at = ? WHERE repo_workspace_id = ? AND lane_id = ? "
                        "AND revision = ?",
                        (
                            issue,
                            DISPOSITION_ACTIVE,
                            RELEASE_NOT_REQUESTED,
                            "",
                            "",
                            REPLACEMENT_NOT_REQUESTED,
                            "",
                            "",
                            revision,
                            decision.source,
                            decision.issue_id,
                            decision.journal_id,
                            stamp,
                            recovery.repo_workspace_id,
                            recovery.lane_id,
                            incoming.revision,
                        ),
                    )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_OWNER_CONFLICT,
                    revision=current.revision,
                )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane supersession failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def request_release(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        action_id: str,
        pins: Iterable[ReleasePin],
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Open a release generation, pinning the slots it is allowed to close.

        Only a lane that has already left ``active`` may open one: a lane still
        holding its work is never a release target. The pins are the *only* slots
        this generation may ever close, and the actuator must re-verify each one
        against the live inventory before closing it.
        """
        action = norm(action_id)
        if not action:
            raise ValueError("a release generation requires a non-empty action id")
        # Every pin must name a slot the actuator can actually re-resolve, and no slot
        # may appear twice (R1-F4); an unusable pin is refused, never stored.
        pinned = validate_release_pins(tuple(pins))
        stamp = now or _utc_now()
        conn = self._connect_write(key)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if current.lane_disposition == DISPOSITION_ACTIVE:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_UNEXPECTED_STATE,
                    revision=current.revision,
                )
            if not replacement_settled(current.replacement_state):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            if not release_transition_allowed(
                current.process_release, RELEASE_REQUESTED
            ):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET process_release = ?, release_action_id = ?, "
                "release_pins = ?, revision = ?, updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ?",
                (
                    RELEASE_REQUESTED,
                    action,
                    encode_release_pins(pinned),
                    revision,
                    stamp,
                    key.repo_workspace_id,
                    key.lane_id,
                    current.revision,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane release request failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def record_release_outcome(
        self,
        key: LaneLifecycleKey,
        *,
        action_id: str,
        expected_revision: int,
        target: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Record a release generation's outcome, guarded by its exact action id.

        The action id is part of the guard so a *stale* generation can never mark a
        *newer* one done (Design Answer D3): an outcome carrying a foreign action id
        is refused with :data:`CAS_ACTION_MISMATCH`, not applied to whatever
        generation happens to be open.
        """
        if target not in (RELEASE_PARTIAL, RELEASE_RELEASED):
            raise ValueError(f"a release outcome is partial or released, not {target!r}")
        action = norm(action_id)
        stamp = now or _utc_now()
        conn = self._connect_write(key)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.release_action_id != action:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ACTION_MISMATCH,
                    revision=current.revision,
                )
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if not release_transition_allowed(current.process_release, target):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET process_release = ?, revision = ?, updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ? "
                "AND release_action_id = ?",
                (
                    target,
                    revision,
                    stamp,
                    key.repo_workspace_id,
                    key.lane_id,
                    current.revision,
                    action,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise LaneLifecycleError(
                f"lane release outcome failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


# -- module-level read wrappers (fail closed, never "probably active") --------


def resolve_lane_owner(
    repo_workspace_id: str, issue_id: str, *, home: Path | None = None
) -> OwnerResolution:
    """The issue's active owning lane, or a fail-closed status.

    An unusable store yields :data:`OWNER_UNKNOWN` — deliberately *not* an empty
    result that a caller could read as "no conflict, go ahead" (Design Answer D1).
    """
    try:
        return LaneLifecycleStore(home=home).resolve_owner(repo_workspace_id, issue_id)
    except (LaneLifecycleError, OSError) as exc:
        return OwnerResolution(status=OWNER_UNKNOWN, detail=type(exc).__name__)


def load_lane_lifecycle(
    *, home: Path | None = None
) -> Optional[tuple[LaneLifecycleRecord, ...]]:
    """Every lifecycle row, or ``None`` when the store is unusable (fail closed)."""
    try:
        return LaneLifecycleStore(home=home).records()
    except (LaneLifecycleError, OSError):
        return None


__all__ = (
    "DECISION_SOURCES",
    "DECISION_SOURCE_REDMINE",
    "DecisionPointer",
    "DecisionPointerError",
    "ReleasePinError",
    "ProcessPinError",
    "ProcessGenerationPin",
    "BINDING_KINDS",
    "BINDING_KIND_ISSUE",
    "BINDING_KIND_PROJECT_GATEWAY",
    "encode_declared_slots",
    "validate_declared_slots",
    "rehydrate_allowed",
    "validate_release_pins",
    "LANE_LIFECYCLE_COMPONENT",
    "LANE_LIFECYCLE_RECOVERY_POLICY",
    "LANE_LIFECYCLE_SCHEMA_VERSION",
    "LaneLifecycleError",
    "LaneLifecycleStore",
    "LaneLifecycleReader",
    "LaneLifecycleReaderUpgradeRequired",
    "LifecycleMigrationPreflight",
    "LifecycleSchemaOutcome",
    "LifecycleWritePreparation",
    "lane_lifecycle_path",
    "lifecycle_migration_preflight",
    "load_lane_lifecycle",
    "load_lane_lifecycle_readonly",
    "resolve_lane_owner",
    # re-exported from lane_lifecycle_model so the component has one import surface
    "CAS_ACTION_MISMATCH",
    "CAS_ALREADY_DECLARED",
    "CAS_APPLIED",
    "CAS_FORBIDDEN_TRANSITION",
    "CAS_GENERATION_MISMATCH",
    "CAS_NOT_FOUND",
    "CAS_OWNER_CONFLICT",
    "CAS_STALE_REVISION",
    "CAS_UNEXPECTED_STATE",
    "DISPOSITIONS",
    "DISPOSITION_ACTIVE",
    "DISPOSITION_HIBERNATED",
    "DISPOSITION_RETIRED",
    "RECONCILE_PHASE_NONE",
    "RECONCILE_PHASE_RECONCILED",
    "RECONCILE_PHASES",
    "DISPOSITION_SUPERSEDED",
    "OWNER_ABSENT",
    "OWNER_AMBIGUOUS",
    "OWNER_RESOLVED",
    "OWNER_UNKNOWN",
    "RELEASE_NOT_REQUESTED",
    "RELEASE_PARTIAL",
    "RELEASE_RELEASED",
    "RELEASE_REQUESTED",
    "RELEASE_STATES",
    "REPLACEMENT_NOT_REQUESTED",
    "CasOutcome",
    "LaneLifecycleKey",
    "LaneLifecycleRecord",
    "OwnerResolution",
    "ReleasePin",
    "decode_release_pins",
    "disposition_transition_allowed",
    "encode_release_pins",
    "release_transition_allowed",
    "replacement_settled",
)
