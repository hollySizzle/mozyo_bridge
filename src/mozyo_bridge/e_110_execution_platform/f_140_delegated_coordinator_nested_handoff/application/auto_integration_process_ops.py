"""The live managed-process release the #13686 cleanup authorizes (Redmine #14825, item 7).

#13686 withdrew three destructive cleanup steps and kept exactly one, and it justified keeping
that one structurally rather than by luck: ``release_process(issue, lane_generation)`` is
*parameterized by the identity it acts on*, so there is no window in which the thing named by
its arguments becomes something else. A path and a ref name are late-bound; an issue and a lane
generation are not.

**That claim was about the primitive's signature, and #14825 j#96408 is where it has to become
true of an implementation.** An adapter that took those two values and then went looking for the
lane by path, by pane locator, or by display name would re-open exactly the late-bound target
problem the three withdrawn steps were withdrawn for — the signature would be honest and the
implementation would not.

So this adapter resolves its target from the durable lifecycle store and from nothing else:

* the lookup key is ``(issue_id, lane_generation)`` over
  :meth:`~mozyo_bridge.core.state.lane_lifecycle.LaneLifecycleStore.records`. No path, no
  locator, no name participates in deciding WHICH lane is meant;
* **a stale generation resolves to nothing.** A lifecycle row is keyed by
  ``(repo_workspace_id, lane_id)`` and carries the lane's CURRENT generation, so asking for a
  superseded generation matches no row at all. That is why staleness needs no separate probe
  here — and why the match is on the generation rather than on the lane, which would have
  found the row and released the wrong incarnation;
* **an ambiguous inventory releases nothing.** Zero rows and two rows are both refusals, and the
  match deliberately runs across every workspace before the ownership check rather than after
  it: filtering to our own lane first would make a second lane claiming the same issue and
  generation *invisible*, turning the ambiguity that should stop the release into a clean
  single match;
* **a foreign lane releases nothing.** The one resolved row must be this actuator's own
  workspace and lane. Retiring another lane's managed process is a cross-lane side effect in
  exactly the way removing its checkout was;
* **the release consumes the identity that was verified.** The lifecycle key handed to the
  driver is built from the resolved ROW, and the row's ``revision`` is passed as the driver's
  ``expected_revision`` — so any lifecycle write between the resolution and the mutation
  (including the generation bump that would make our answer stale) closes nothing and reports
  an admission block. Verifying one value and mutating on another is the defect j#96406
  finding 2 found on the other side of this same step.

The mutation itself is the shared tombstone-free driver
(:func:`~...application.sublane_process_release.drive_process_release`): it closes the lane's
durably pinned managed slots and never removes a worktree, deletes a branch, or writes a
tombstone. This adapter adds identity resolution and adds nothing to what that driver may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from mozyo_bridge.core.state.lane_lifecycle import (
    RELEASE_RELEASED,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    SublaneReleaseOps,
    drive_process_release,
)

#: The action id this actuator opens a release generation under. It names the issue and the
#: generation it resolved, so an operator reading the lifecycle row can tell which action opened
#: it apart from a ``sublane hibernate`` / ``supersede`` release.
ACTION_ID_PREFIX = "auto_integration_retire"

# Typed refusals. Each is a ZERO-release: nothing was closed, and they stay distinguishable
# because the operator's next move differs for each (re-form the action against the current
# generation, fix the inventory, look at who else claims this issue, read the store).
REFUSE_STORE_UNREADABLE = "lifecycle_store_unreadable"
REFUSE_NO_ROW = "lifecycle_row_absent"
REFUSE_AMBIGUOUS = "lifecycle_row_ambiguous"
REFUSE_FOREIGN_LANE = "foreign_lane"
REFUSE_INVENTORY_UNREADABLE = "inventory_unreadable"
REFUSE_INVALID_IDENTITY = "invalid_identity"


@runtime_checkable
class ManagedInventoryOps(SublaneReleaseOps, Protocol):
    """The release driver's ops, plus the readability-vetting inventory read.

    ``read_inventory`` returns ``(rows, readable)`` so an inventory that could not be read is not
    folded to an empty one — "the panes are gone" and "we could not look" are different facts,
    and only the first of them may complete a release (the R1-F1 boundary the hibernate ops
    already hold).
    """

    def read_inventory(self) -> Tuple[Sequence[Mapping[str, object]], bool]: ...


@dataclass(frozen=True)
class ProcessReleaseOutcome:
    """What one :meth:`LiveManagedProcessOperations.release_process` call did, and why.

    ``released`` is the boolean the port returns; ``refusal`` names the typed reason a refusal
    happened, so a durable record can say WHICH zero-release this was instead of only that
    nothing happened.
    """

    released: bool = False
    refusal: str = ""
    detail: str = ""
    resolved_lane: str = ""
    resolved_workspace: str = ""
    resolved_revision: int = 0


@dataclass(frozen=True)
class LiveManagedProcessOperations:
    """A live :class:`~...application.auto_integration_ports.ManagedProcessOperations`.

    ``lane_workspace`` / ``lane_id`` are this actuator's OWN lane, supplied at construction from
    the same configuration that binds the rest of the actuator — never from the record asking for
    the release. That separation is what makes the ownership comparison a check.
    """

    store: LaneLifecycleStore
    ops: ManagedInventoryOps
    lane_workspace: str
    lane_id: str

    def release_process(self, *, issue: str, lane_generation: int) -> bool:
        """Release the managed process of the lane those two values identify, or nothing.

        The port's contract is a bare boolean, so this returns one; :meth:`describe_release`
        performs the same work and returns the typed reason, which is what a durable record
        should carry.
        """
        return self.describe_release(
            issue=issue, lane_generation=lane_generation
        ).released

    def describe_release(
        self, *, issue: str, lane_generation: int
    ) -> ProcessReleaseOutcome:
        """The typed form of :meth:`release_process`."""
        wanted_issue = str(issue or "").strip()
        if not wanted_issue or not _is_positive_int(lane_generation):
            return ProcessReleaseOutcome(
                refusal=REFUSE_INVALID_IDENTITY,
                detail=(
                    "a release needs a non-empty issue and a positive lane generation; "
                    "nothing was resolved and nothing was released"
                ),
            )

        try:
            records = self.store.records()
        except (LaneLifecycleError, OSError) as exc:
            return ProcessReleaseOutcome(
                refusal=REFUSE_STORE_UNREADABLE,
                detail=(
                    f"the lifecycle store could not be read ({exc.__class__.__name__}); an "
                    "unreadable authority never resolves a release target"
                ),
            )

        # Across EVERY workspace, so a second lane claiming this issue and generation is seen as
        # the ambiguity it is rather than filtered out of the way.
        matches = [
            record
            for record in records
            if str(record.issue_id or "").strip() == wanted_issue
            and _is_positive_int(record.lane_generation)
            and int(record.lane_generation) == int(lane_generation)
        ]
        if not matches:
            return ProcessReleaseOutcome(
                refusal=REFUSE_NO_ROW,
                detail=(
                    f"no lifecycle row owns issue {wanted_issue} at generation "
                    f"{lane_generation}; a superseded generation resolves to no row at all, "
                    "which is what makes a stale release impossible rather than merely unlikely"
                ),
            )
        if len(matches) > 1:
            return ProcessReleaseOutcome(
                refusal=REFUSE_AMBIGUOUS,
                detail=(
                    f"{len(matches)} lifecycle rows own issue {wanted_issue} at generation "
                    f"{lane_generation}; an ambiguous target is never guessed at"
                ),
            )

        record = matches[0]
        if (
            str(record.repo_workspace_id) != str(self.lane_workspace)
            or str(record.lane_id) != str(self.lane_id)
        ):
            return ProcessReleaseOutcome(
                refusal=REFUSE_FOREIGN_LANE,
                detail=(
                    "the resolved lane is not this actuator's own; releasing another lane's "
                    "managed process is a cross-lane side effect"
                ),
                resolved_lane=str(record.lane_id),
                resolved_workspace=str(record.repo_workspace_id),
                resolved_revision=int(record.revision),
            )

        rows, readable = self.ops.read_inventory()
        if not readable:
            return ProcessReleaseOutcome(
                refusal=REFUSE_INVENTORY_UNREADABLE,
                detail=(
                    "the live managed inventory could not be read; an unreadable inventory is "
                    "not an empty one, so nothing was closed"
                ),
                resolved_lane=str(record.lane_id),
                resolved_workspace=str(record.repo_workspace_id),
                resolved_revision=int(record.revision),
            )

        try:
            key = LaneLifecycleKey(
                repo_workspace_id=record.repo_workspace_id, lane_id=record.lane_id
            )
        except ValueError as exc:
            return ProcessReleaseOutcome(
                refusal=REFUSE_INVALID_IDENTITY,
                detail=f"the resolved row does not form a lifecycle key ({exc})",
            )

        outcome = drive_process_release(
            store=self.store,
            ops=self.ops,
            key=key,
            # Every value the mutation consumes comes from the ROW that was verified — not from
            # the arguments, and not from a second lookup.
            lane_id=record.lane_id,
            workspace_id=record.repo_workspace_id,
            action_id=f"{ACTION_ID_PREFIX}:{wanted_issue}:{lane_generation}",
            rows=rows,
            # The lifecycle CAS that closes the gap between this resolution and the mutation. A
            # generation bump is a lifecycle write, so it moves the revision and this blocks.
            expected_revision=record.revision,
        )
        released = (
            outcome.process_release == RELEASE_RELEASED
            and not outcome.admission_blocked
            and not outcome.failed
        )
        return ProcessReleaseOutcome(
            released=released,
            refusal="" if released else (outcome.process_release or REFUSE_NO_ROW),
            detail=outcome.detail,
            resolved_lane=str(record.lane_id),
            resolved_workspace=str(record.repo_workspace_id),
            resolved_revision=int(record.revision),
        )


def _is_positive_int(value: object) -> bool:
    """A positive integer, with ``bool`` refused so ``True`` never reads as generation 1."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


__all__ = (
    "ACTION_ID_PREFIX",
    "REFUSE_AMBIGUOUS",
    "REFUSE_FOREIGN_LANE",
    "REFUSE_INVALID_IDENTITY",
    "REFUSE_INVENTORY_UNREADABLE",
    "REFUSE_NO_ROW",
    "REFUSE_STORE_UNREADABLE",
    "LiveManagedProcessOperations",
    "ManagedInventoryOps",
    "ProcessReleaseOutcome",
)
