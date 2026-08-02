"""The executable next rail for an already-hibernated pre-v10 lane (Redmine #14756 j#96836).

The epoch fence (:mod:`.lane_epoch`) refuses a row whose ``lane_epoch`` is unminted and names
"resume after a real v10 hibernate transition" as the operator's next step. Coordinator review
j#96836 established — and this module's regressions re-measure — that for the shape the issue
actually exists to unblock, **that step cannot be taken**:

- ``lane_epoch`` is minted by exactly one event, the disposition CAS INTO ``hibernated``;
- a lane hibernated by a pre-v10 build is ALREADY ``hibernated``, and the legal edges out of
  that state are ``active`` and ``retired`` only — ``hibernated -> hibernated`` is
  ``forbidden_transition``, so the release redrive such a lane goes through never mints;
- ``-> active`` **is** the resume the epoch fence is refusing, and ``-> retired`` discards the
  lane and its worktree.

So the named rail was a deadlock: the one transition that mints requires the one state the
fence will not let the lane reach. A refusal whose stated remedy cannot be performed is not
fail-closed, it is stuck — the distinction #14477 kept re-learning about "the invariant holds
only for rows that never take this path".

**What this rail is.** A single, bounded, CAS-guarded adoption that INITIALISES the counter on
an already-hibernated legacy row and changes nothing else. It is not a transition (the
disposition does not move), not an actuation (no pane is closed, launched, resumed or sent to),
and not a repair of any other axis.

**Why initialising cannot admit a survivor** — the property that makes this safe rather than a
carve-out. Adoption supplies only the *authority* half of the proof; the *attestation* half is
untouched and still has to be satisfied by a real process. A pane that survived this lane's
pre-#14756 release was launched by a build that had no epoch concept at all, so it carries no
``MOZYO_LANE_EPOCH`` in its environment and attests an EMPTY epoch — which
:data:`...lane_epoch.EPOCH_ATTESTATION_ABSENT` refuses regardless of what the row says. Only a
process launched AFTER adoption can read the minted epoch out of its own env, and a live
process's env cannot be rewritten (POSIX). Adoption therefore moves a lane from "cannot prove
anything" to "must prove it by relaunching", which is exactly the pre-existing contract for a
v10-hibernated lane — never to "admitted".

**Initialise, never advance.** ``lane_epoch`` must be exactly ``0`` or the adoption is refused.
Re-running it must not walk 1 -> 2: that would invalidate a pair already launched at 1 and
strand precisely the operator who followed this rail once. The guard is an equality on the
stored value, not a bound.

Everything else about the row is preserved byte-for-byte — disposition, the whole release axis
and its observation, replacement axis, worktree binding, declared pins, ``lane_kind``,
``reconcile_phase`` and ``hibernated_at``. Only ``lane_epoch``, the decision anchor and the
revision move, so a lane's WIP, its generation identity and its replay evidence survive
adoption intact (j#96836 required action 3).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.lane_epoch import (
    EPOCH_STORED_MALFORMED,
    encode_lane_epoch,
    EPOCH_STORED_UNMINTED,
    classify_stored_epoch,
)
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_HIBERNATED,
    RELEASE_RELEASED,
    CasOutcome,
    DecisionPointer,
    LaneLifecycleKey,
    guard,
    norm,
    replacement_settled,
)
from mozyo_bridge.core.state.lane_lifecycle_rows import _locked_row
from mozyo_bridge.core.state.lane_lifecycle_schema import _TABLE, LaneLifecycleError

#: The epoch a legacy adoption mints. Deliberately the same value a lane's FIRST real v10
#: hibernate would have minted, so an adopted row and a natively-hibernated one are
#: indistinguishable to every reader afterwards — the rail converges onto the normal contract
#: rather than creating a parallel one.
ADOPTED_LEGACY_EPOCH = 1


def legacy_adoption_refusal(
    current,
    *,
    expected_revision: int,
    issue_id: str,
    decision: DecisionPointer,
) -> Optional[CasOutcome]:
    """The refusal :meth:`LaneEpochAdoptionStore.adopt_legacy_epoch` would give, or ``None``.

    A pure predicate over an already-read row, extracted so the CAS and any rail that wants
    to *report* what the CAS would do share one implementation. The alternative — a planner
    with its own idea of "is this the legacy shape?" — is the failure mode #14477 R7 named
    and this issue keeps re-encountering: two surfaces classifying one stored fact eventually
    classify it two ways, and the one that is only ever read (the plan) drifts silently
    because nothing writes through it.

    Reporting this is NOT a substitute for it running inside the write lock. The row can move
    between a plan and its execution, which is exactly why :meth:`adopt_legacy_epoch` calls
    this again under ``BEGIN IMMEDIATE`` rather than trusting an earlier answer.
    """
    refusal = guard(current, DISPOSITION_HIBERNATED, expected_revision)
    if refusal is not None:
        return refusal
    if current.issue_id != norm(issue_id):
        return CasOutcome(
            applied=False, reason=CAS_UNEXPECTED_STATE, revision=current.revision
        )
    if not decision.authorizes_binding(current.issue_id):
        return CasOutcome(
            applied=False, reason=CAS_UNEXPECTED_STATE, revision=current.revision
        )
    _epoch, epoch_state = classify_stored_epoch(current.lane_epoch)
    if epoch_state == EPOCH_STORED_MALFORMED:
        # NOT the same refusal as "already minted", and not the same as zero (#14756 j#96881
        # F2). A TEXT / REAL / bool / NULL / negative counter is an unreadable row, and this
        # rail's entire safety argument rests on the stored value being EXACTLY 0 — "this
        # lane has never minted a generation". Reading a corrupt value as 0 would let the
        # adoption mint 1 for a lane whose real counter is unknown, which is a rollback.
        return CasOutcome(
            applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=current.revision
        )
    if epoch_state != EPOCH_STORED_UNMINTED:
        # Already adopted, or natively minted by a v10 hibernate. Either way this row has a
        # generation authority already and re-minting would move it.
        return CasOutcome(
            applied=False, reason=CAS_UNEXPECTED_STATE, revision=current.revision
        )
    if current.process_release != RELEASE_RELEASED:
        # Byte-exact, never normalised: a stored value outside the vocabulary has no
        # outgoing edge here either (#14477 R8-F2).
        return CasOutcome(
            applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=current.revision
        )
    if not replacement_settled(current.replacement_state):
        return CasOutcome(
            applied=False, reason=CAS_FORBIDDEN_TRANSITION, revision=current.revision
        )
    return None


class LaneEpochAdoptionStore:
    """The bounded legacy-epoch adoption rail over the shared lifecycle authority."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    def adopt_legacy_epoch(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        issue_id: str,
        decision: DecisionPointer,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Mint :data:`ADOPTED_LEGACY_EPOCH` on an already-hibernated pre-v10 row.

        Refused zero-write unless every one of these holds, so a live, active, terminal,
        in-flight, foreign or already-adopted row can never be touched:

        - the row exists (:data:`CAS_NOT_FOUND`) and ``expected_revision`` still matches
          (:data:`CAS_STALE_REVISION` — a concurrent write wins rather than being clobbered);
        - it is ``hibernated`` and owns **this exact** issue (:data:`CAS_UNEXPECTED_STATE`).
          An ``active`` lane mints through the normal transition and must not shortcut it; a
          ``superseded`` / ``retired`` row is terminal; a different issue is another lane's;
        - its ``lane_epoch`` is exactly ``0`` (:data:`CAS_UNEXPECTED_STATE`). This
          INITIALISES; it never advances, because advancing would strand a pair launched at
          the previous value;
        - its process release is durably ``released`` and no replacement is in flight
          (:data:`CAS_FORBIDDEN_TRANSITION`). An unreleased or in-flight lane may have an
          actuator mutating its slots right now, and adoption must not race one;
        - the ``decision`` anchor names this issue — a bound row is only decided by a record
          filed on its own issue (the same rule ``transition_disposition`` applies).

        Idempotent replay is deliberately NOT offered. A second call sees ``lane_epoch == 1``
        and returns :data:`CAS_UNEXPECTED_STATE`, because "already adopted" and "adopt again"
        must not both read as success on a counter whose whole value is that it moves exactly
        once per generation.
        """
        issue = norm(issue_id)
        if not issue:
            raise ValueError(
                "a legacy epoch adoption requires the exact issue the row must already own"
            )
        stamp = now or _utc_now()
        conn = self._lifecycle._connect_write(key)  # Redmine #13844: shared write gate
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            # Re-evaluated HERE, inside the write lock, even when a planning rail already
            # reported it: the row can move between the two, and a decision read outside the
            # lock is evidence about the past.
            refusal = legacy_adoption_refusal(
                current,
                expected_revision=expected_revision,
                issue_id=issue,
                decision=decision,
            )
            if refusal is not None:
                conn.execute("ROLLBACK")
                return refusal
            revision = current.revision + 1
            changes_before = conn.total_changes
            # ``lane_epoch``, the decision anchor and the revision are the ONLY columns this
            # writes. Everything the lane's identity, WIP and replay evidence depend on is
            # absent from this UPDATE and therefore preserved (j#96836 required action 3).
            conn.execute(
                f"UPDATE {_TABLE} SET lane_epoch = ?, revision = ?, "
                "decision_source = ?, decision_issue_id = ?, decision_journal = ?, "
                "updated_at = ? "
                "WHERE repo_workspace_id = ? AND lane_id = ? AND revision = ? "
                "AND lane_epoch IS ? AND typeof(lane_epoch) = 'text'",
                (
                    encode_lane_epoch(ADOPTED_LEGACY_EPOCH),
                    revision,
                    decision.source,
                    decision.issue_id,
                    decision.journal_id,
                    stamp,
                    key.repo_workspace_id,
                    key.lane_id,
                    current.revision,
                    current.lane_epoch,
                ),
            )
            if conn.total_changes == changes_before:
                # A concurrent writer changed the epoch bytes or their storage class between
                # the guarded read and this write. Zero-write refusal (#14756 j#96911 F2).
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False, reason=CAS_STALE_REVISION, revision=current.revision
                )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise LaneLifecycleError(
                f"legacy lane epoch adoption failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = (
    "ADOPTED_LEGACY_EPOCH",
    "LaneEpochAdoptionStore",
    "legacy_adoption_refusal",
)
