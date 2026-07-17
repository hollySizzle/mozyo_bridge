"""Every durable obligation owed to (or by) a scratch pair's slots (Redmine #13892).

Review j#80594 R4-F1 / R4-F3 rejected the first cut on three counts, all of which this module
exists to answer:

1. **The matrix was argued, not read.** ``CallbackOutbox`` was called structurally-inapplicable
   because its *key* carries no assigned name — a true but irrelevant fact. Its **row** carries
   ``target_lane`` / ``target_receiver`` / ``target_generation``, and
   ``BackendNeutralTargetResolver`` rebuilds the canonical ``pane_name`` from them as
   ``encode_assigned_name(workspace_id, target_receiver, target_lane)``. A callback can
   therefore be owed to a scratch pair's slot, so it is **covered**: it is read, not argued away.
2. **Only "owed TO" was considered.** Acceptance 2 says work dispatch **/ progress obligation**.
   A scratch role's own outbound ``ForwardOutboxFence`` generation is work owed **BY** the pair —
   active until its correlated callback returns — and closing the pair mid-generation strands it.
3. **``delivered`` was blocked unconditionally.** A delivery ACK is not task completion, so it
   cannot be waved through; but it also cannot block forever, or a normal pair that was ever
   dispatched to becomes un-retirable — the permanent-stuck this ticket exists to remove. It is
   now **correlated** against the durable disposition of the work it handed over.

Every read fails closed: an obligation that cannot be observed is not an obligation that is
absent. A genuinely uninitialized store is a positive absence (the ordinary case for a lane
nothing ever dispatched to) and must not be over-blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

#: The obligation is owed on the store's own terms — refuse.
OWED = "owed"
#: The obligation is discharged — it does not block.
DISCHARGED = "discharged"
#: The obligation's disposition could not be established — refuse (never assume discharged).
UNCORRELATED = "uncorrelated"


@dataclass(frozen=True)
class PairObligation:
    """One durable obligation touching a scratch pair, with the identity that explains it."""

    source: str
    verdict: str
    target: str = ""
    state: str = ""
    issue: str = ""
    journal: str = ""
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.verdict in (OWED, UNCORRELATED)

    def describe(self) -> str:
        who = self.target or f"{self.issue or '<no issue>'}"
        return f"{self.source}: {who} ({self.state or self.verdict}) {self.detail}".strip()


class ObligationStoreUnreadable(RuntimeError):
    """A durable obligation store could not be read; the caller must not close."""


def _norm(value) -> str:
    return str(value or "").strip()


def dispatch_outbox_obligations(
    *, workspace_id: str, assigned_names: Sequence[str], correlate=None
) -> tuple[PairObligation, ...]:
    """Obligations from the dispatch outbox — the one store keyed on a target slot name.

    ``reserved`` / ``uncertain`` are owed on the fence's own terms. ``delivered`` is a delivery
    ACK, so its disposition lives elsewhere: ``correlate(issue, journal)`` decides, and anything
    it cannot establish stays :data:`UNCORRELATED` (blocking). Never assume discharged.
    """
    from mozyo_bridge.core.state.dispatch_outbox_fence import (
        DispatchOutboxFence,
        DispatchOutboxFenceError,
    )

    try:
        rows = DispatchOutboxFence().obligations_for_targets(
            workspace_id=workspace_id, target_assigned_names=tuple(assigned_names)
        )
    except (DispatchOutboxFenceError, OSError) as exc:
        raise ObligationStoreUnreadable(
            f"the dispatch outbox fence could not be read ({exc})"
        ) from exc

    out: list[PairObligation] = []
    for row in rows:
        if row.non_terminal:
            out.append(
                PairObligation(
                    source="dispatch_outbox",
                    verdict=OWED,
                    target=row.target_assigned_name,
                    state=row.state,
                    issue=row.issue,
                    journal=row.journal,
                    detail="a send's fate is unresolved",
                )
            )
            continue
        if not row.needs_gate_correlation:
            continue  # cancelled: positively not owed
        verdict, detail = _correlate_delivered(row, correlate)
        if verdict is DISCHARGED:
            continue
        out.append(
            PairObligation(
                source="dispatch_outbox",
                verdict=verdict,
                target=row.target_assigned_name,
                state=row.state,
                issue=row.issue,
                journal=row.journal,
                detail=detail,
            )
        )
    return tuple(out)


def _correlate_delivered(row, correlate) -> tuple[str, str]:
    """Is the work a ``delivered`` send handed over actually discharged? (R4-F1)

    A delivery ACK proves the message landed, never that the work finished — the ACK /
    delivery / completion separation. So the disposition is read from the durable record the
    handed-over work reports into, keyed by the send's own ``(issue, journal)``.

    Fail-closed: no identity to correlate with, no correlator wired, or an unreadable
    disposition all yield :data:`UNCORRELATED`, which blocks. Only a positive "this work is
    finished" discharges.
    """
    if correlate is None:
        return (
            UNCORRELATED,
            "no durable-disposition correlator is available for this delivered send",
        )
    if not _norm(row.issue):
        return (
            UNCORRELATED,
            "the delivered send names no issue, so its completion cannot be correlated",
        )
    try:
        discharged = correlate(_norm(row.issue), _norm(row.journal))
    except Exception as exc:  # noqa: BLE001 - unreadable disposition -> never assume finished
        return (UNCORRELATED, f"the durable disposition could not be read ({exc})")
    if discharged is True:
        return (DISCHARGED, "")
    if discharged is False:
        return (
            OWED,
            "the work delivered to this slot has not reached a terminal disposition",
        )
    return (UNCORRELATED, "the work's disposition is unknown")


def callback_outbox_obligations(
    *, workspace_id: str, lane_id: str, assigned_names: Sequence[str]
) -> tuple[PairObligation, ...]:
    """Obligations from the callback outbox — callbacks owed TO one of this pair's slots.

    COVERED, not inapplicable (review j#80594 R4-F3): the row's durable
    ``(target_lane, target_receiver)`` rebuild the canonical assigned name exactly as
    ``BackendNeutralTargetResolver`` does, so a callback can be aimed at a scratch slot. The
    key having no name column is irrelevant — the row is what carries the target.

    Active states (``pending`` / ``inflight`` / ``uncertain``) are owed. ``delivered`` and
    ``dead_letter`` are terminal for *delivery*: nothing further will be sent to the slot.
    """
    from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        encode_assigned_name,
    )

    try:
        rows = CallbackOutbox().read()
    except Exception as exc:  # noqa: BLE001
        raise ObligationStoreUnreadable(
            f"the callback outbox could not be read ({exc})"
        ) from exc

    wanted = {_norm(n) for n in assigned_names if _norm(n)}
    active = {"pending", "inflight", "uncertain"}
    out: list[PairObligation] = []
    for row in rows:
        if _norm(getattr(row, "workspace_id", "")) != _norm(workspace_id):
            continue
        state = _norm(getattr(row, "state", ""))
        if state not in active:
            continue
        receiver = _norm(getattr(row, "target_receiver", ""))
        target_lane = _norm(getattr(row, "target_lane", ""))
        if not receiver or not target_lane:
            continue  # no rebuildable target identity: this row names no slot
        try:
            name = encode_assigned_name(workspace_id, receiver, target_lane)
        except Exception:  # noqa: BLE001 - unencodable -> cannot name this pair
            continue
        if _norm(name) not in wanted:
            continue
        out.append(
            PairObligation(
                source="callback_outbox",
                verdict=OWED,
                target=name,
                state=state,
                issue=_norm(getattr(row, "issue", "")),
                journal=_norm(getattr(row, "journal", "")),
                detail="a callback is owed to this slot",
            )
        )
    return tuple(out)


def forward_generation_obligations(
    *, workspace_id: str, lane_id: str, roles: Sequence[str]
) -> tuple[PairObligation, ...]:
    """Obligations from the forward fence — work owed **BY** this pair (R4-F3 owed-FROM).

    Acceptance 2 covers work dispatch **/ progress obligation**, and a forward generation this
    lane's role opened is progress it still owes: it stays active from the send until the
    correlated callback returns. The first cut excluded this store because the key carries no
    *target* name — but the pair is the **sender** here, and ``from_lane_id`` / ``from_role``
    are exactly its identity. Closing the pair mid-generation strands that forward forever.

    ``reserved`` / ``delivered`` / ``uncertain`` are active; ``completed`` is discharged.
    """
    from mozyo_bridge.core.state.forward_outbox_fence import (
        FORWARD_COMPLETED,
        ForwardOutboxFence,
        ForwardOutboxFenceError,
    )

    try:
        fence = ForwardOutboxFence()
        rows = fence.rows_for_sender(workspace_id=workspace_id, from_lane_id=lane_id)
    except ForwardOutboxFenceError as exc:
        raise ObligationStoreUnreadable(
            f"the forward outbox fence could not be read ({exc})"
        ) from exc
    except OSError as exc:
        raise ObligationStoreUnreadable(
            f"the forward outbox fence could not be read ({exc})"
        ) from exc

    wanted = {_norm(r) for r in roles if _norm(r)}
    out: list[PairObligation] = []
    for from_role, to_role, state in rows:
        if _norm(state) == FORWARD_COMPLETED:
            continue
        if wanted and _norm(from_role) not in wanted:
            continue
        out.append(
            PairObligation(
                source="forward_outbox",
                verdict=OWED,
                target=f"{lane_id}/{from_role}",
                state=_norm(state),
                detail=f"an outbound forward to {to_role} is still owed by this pair",
            )
        )
    return tuple(out)


def all_pair_obligations(
    *,
    workspace_id: str,
    lane_id: str,
    assigned_names: Sequence[str],
    roles: Sequence[str],
    correlate=None,
) -> tuple[PairObligation, ...]:
    """Every blocking obligation across every covered source. Raises when any is unreadable.

    The covered set is fixed by ``test_issue_13892_obligation_source_matrix``; the sources this
    does NOT read are classified there as structurally inapplicable, each with a reason that
    fails as a test if the store's shape changes.
    """
    found: list[PairObligation] = []
    found.extend(
        dispatch_outbox_obligations(
            workspace_id=workspace_id, assigned_names=assigned_names, correlate=correlate
        )
    )
    found.extend(
        callback_outbox_obligations(
            workspace_id=workspace_id, lane_id=lane_id, assigned_names=assigned_names
        )
    )
    found.extend(
        forward_generation_obligations(
            workspace_id=workspace_id, lane_id=lane_id, roles=roles
        )
    )
    return tuple(o for o in found if o.blocks)


__all__ = (
    "OWED",
    "DISCHARGED",
    "UNCORRELATED",
    "PairObligation",
    "ObligationStoreUnreadable",
    "dispatch_outbox_obligations",
    "callback_outbox_obligations",
    "forward_generation_obligations",
    "all_pair_obligations",
)
