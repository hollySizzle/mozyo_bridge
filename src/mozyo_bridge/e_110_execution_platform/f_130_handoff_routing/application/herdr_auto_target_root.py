"""``--target-repo auto`` -> the TARGET agent's own repo root, herdr backend (Redmine #14249 R2).

Round 1 of #14249 made a relative ``--workdir`` resolve against the ASSERTED
``--target-repo`` and fenced an execution root that does not live under it. Late finding
j#94419 then measured the public ``auto`` variant still landing on the sender: a coordinator
in the ``main-next`` integration worktree ran ``handoff reply --target-repo auto --workdir .``
at an exact lane pane and the structured ``execution_root.repo_root`` / ``workdir`` named the
*sender's* integration worktree, not the target lane's. Passing that lane worktree explicitly
made the same send resolve exactly — so the defect was confined to ``auto``.

The cause is not in the planner. Under the herdr backend ``auto`` resolved through
``herdr_auto_target_repo(repo_root)``, which returned the SENDER's root because "herdr has no
``%pane`` to infer from" (#13331 j#73312 #2). That premise carries an unstated condition: the
sender's root is the target's root only while the target is the sender's OWN lane. It holds
for the same-lane gateway->worker dispatch the #13331 note was written for, and is simply
false for the cross-lane sends the coordinator rail is built on — where it produced an
authoritative-looking assertion about a repo the receiver does not run in.

This module resolves that question from what is actually known about the TARGET:

- **the sender's own lane** (target unit == sender unit) keeps the sender's repo root. This is
  the #13331 case, now *verified* from the resolved route identity rather than assumed;
- **another lane of the same workspace** resolves that lane's canonical worktree through the
  ONE shared worktree-binding join
  (:func:`~...f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology.bind_lane_worktree`,
  which the hibernate topology observation also reads): among the workspace repo's own
  ``git worktree list --porcelain`` entries, exactly one path must re-derive the lifecycle
  authority's ``worktree_identity`` token. The lane id is never inferred to BE a branch
  (review j#86739 R3-F2), and the display-metadata worktree path (``lane_metadata``) is never
  promoted to an authority — the token is the join key. The join is LOCAL-only: a send makes
  no ``ls-remote`` round trip;
- **anything else** — an unattested identity, a foreign workspace, an absent / unbound
  lifecycle row, a non-unique or unreadable join — resolves NOTHING. The caller then fails
  closed. ``auto`` must never silently fall back to the sender's cwd again: that fallback is
  the defect, and a wrong execution root is delivered as if it were verified.

The lifecycle read goes through the non-migrating read-only reader (#13844): a handoff-rail
read must not forward-migrate the shared home store out from under a concurrent older lane.

Split so the policy is testable without git / sqlite: :func:`classify_auto_target_basis` and
:func:`decide_lane_worktree_root` are pure over stated facts, and
:func:`resolve_herdr_auto_target_repo` is the thin live composition that supplies them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm_lane,
)

#: The target IS the sender's own lane: the sender's repo root is the target's repo root.
BASIS_SENDER_LANE = "sender_lane"
#: The target is another lane of this workspace: its canonical worktree is the root.
BASIS_LANE_WORKTREE = "lane_worktree"

#: No unit to reason with: the sender / target herdr identity is not fully attested, or this
#: repo resolves no workspace identity to scope the target lane's lifecycle row by.
REFUSE_IDENTITY_UNATTESTED = "identity_unattested"
#: The target's workspace is not the sender's — its worktrees are not enumerable from here.
REFUSE_FOREIGN_WORKSPACE = "foreign_workspace"
#: No lifecycle row owns the target lane, so it has no authoritative worktree binding.
REFUSE_LANE_BINDING_ABSENT = "lane_binding_absent"
#: The lane's row carries an EMPTY ``worktree_identity`` (a known-unbound legacy row).
REFUSE_LANE_BINDING_UNBOUND = "lane_binding_unbound"
#: The binding token matched no unique live worktree (pruned / moved / non-unique). A
#: DETACHED worktree is NOT this case — see :func:`decide_lane_worktree_root`.
REFUSE_LANE_WORKTREE_UNRESOLVED = "lane_worktree_unresolved"
#: The lifecycle authority itself could not be read (fail closed, never "no row").
REFUSE_STORE_UNREADABLE = "lifecycle_store_unreadable"
#: The lifecycle authority is a NEWER schema than this runtime can read. Kept DISTINCT from
#: :data:`REFUSE_STORE_UNREADABLE` (review j#95843 finding 1): the reader raises the typed
#: :class:`LaneLifecycleReaderUpgradeRequired` for it, and the caller maps this subreason onto
#: the existing wire reason ``reader_upgrade_required`` — whose repair ("route via a current
#: runtime, never downgrade the store") is the only correct one and is nothing like the repair
#: for a corrupt store. Folding the two loses the one fact the operator needs.
REFUSE_STORE_UPGRADE_REQUIRED = "lifecycle_store_upgrade_required"


@dataclass(frozen=True)
class AutoTargetRoot:
    """The resolved target repo root for ``--target-repo auto``, or a typed refusal.

    ``root`` is empty on every refusal — there is no partially-resolved shape a caller could
    mistake for an answer, and no sender-cwd degradation.
    """

    root: str = ""
    basis: str = ""
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.root)

    def to_structured_dict(self) -> dict:
        """The DURABLE subreason payload carried on the blocked outcome (j#95843 finding 1).

        R3 wrote a ``next_action`` that told the reader to consult "the structured detail" —
        which did not exist: ``DeliveryOutcome`` had no field for it, so the one fact that
        distinguishes these refusals never reached the reader. This is that field's content.
        Free-text-free apart from ``detail``, which names only lane ids and store state.
        """
        return {"subreason": self.reason, "basis": self.basis, "detail": self.detail}

    def wire_reason(self) -> str:
        """The wire ``Reason`` this refusal is delivered as.

        A store that is NEWER than this runtime maps onto the EXISTING
        ``reader_upgrade_required`` (review j#95843 point 2): that reason already carries the
        correct repair and is already understood by the recovery rails, so a second token for
        the same condition would fragment it. Everything else is a genuine
        "auto could not establish the target's frame".
        """
        return (
            "reader_upgrade_required"
            if self.reason == REFUSE_STORE_UPGRADE_REQUIRED
            else "auto_target_repo_unresolved"
        )


@dataclass(frozen=True)
class AutoTargetBasis:
    """Which frame answers "what is the target's repo root" (or a typed refusal)."""

    basis: str = ""
    reason: str = ""
    detail: str = ""


def classify_auto_target_basis(
    *,
    sender_workspace_id: str,
    target_workspace_id: str,
    sender_lane_id: str,
    target_lane_id: str,
) -> AutoTargetBasis:
    """Pure: the frame ``auto`` must resolve the target's repo root from.

    Both units come from the ONE resolution that chose the target: the sender's launch-attested
    identity and the resolved route identity's decoded assigned name. They are compared to each
    other — never to a separately re-derived workspace id — so a legacy pre-#13377 lane (whose
    unit is its own ``wt_`` token rather than the project workspace) still classifies as its own
    sender lane instead of reading as foreign. A partially attested identity refuses rather than
    defaulting either side: both defaults ("same lane" / "same workspace") would silently
    reproduce the sender fallback this module exists to remove.
    """
    sender_ws = (sender_workspace_id or "").strip()
    target_ws = (target_workspace_id or "").strip()
    if not sender_ws or not target_ws:
        return AutoTargetBasis(
            reason=REFUSE_IDENTITY_UNATTESTED,
            detail=(
                f"workspace identity incomplete (sender={sender_ws or '<unknown>'!r}, "
                f"target={target_ws or '<unknown>'!r})"
            ),
        )
    if target_ws != sender_ws:
        return AutoTargetBasis(
            reason=REFUSE_FOREIGN_WORKSPACE,
            detail=(
                f"the target runs in workspace {target_ws!r}, not the sender's "
                f"{sender_ws!r}; its worktrees are not enumerable from here"
            ),
        )
    target_lane = _norm_lane(target_lane_id)
    sender_lane = _norm_lane(sender_lane_id)
    if target_lane == sender_lane:
        return AutoTargetBasis(basis=BASIS_SENDER_LANE, detail=f"lane={target_lane}")
    return AutoTargetBasis(
        basis=BASIS_LANE_WORKTREE,
        detail=f"target lane={target_lane} (sender lane={sender_lane})",
    )


def decide_lane_worktree_root(
    *, target_lane_id: str, lane_binding: Optional[str], lane_worktree: str
) -> AutoTargetRoot:
    """Pure: the cross-lane answer from the lane's binding token + the join's result.

    ``lane_binding`` is ``None`` when no lifecycle row owns the lane and ``""`` when the row
    is present but carries no ``worktree_identity`` (a v1/v2/v3 row the #14475 repair rail
    exists to converge) — two different facts, so they refuse with two different reasons.
    ``lane_worktree`` is the topology join's unique match, or ``""``.

    A **detached** lane worktree resolves normally (review j#94499 finding 2). The join key
    is the ``worktree_identity`` token — a hash of the canonical path — so it proves "this
    path IS the lane's worktree" independently of what the worktree has checked out. Branch
    authority answers a different question, which is why :func:`observe_lane_topology` (push
    / HEAD topology) requires it and this resolver does not: the execution root of a detached
    lane is still exactly that lane's root, and refusing it would stop a live lane for no
    gain. (An earlier record claimed detached was refused; it was never measured. j#95735.)
    """
    lane = _norm_lane(target_lane_id)
    if lane_binding is None:
        return AutoTargetRoot(
            reason=REFUSE_LANE_BINDING_ABSENT,
            detail=f"no lifecycle row owns lane {lane!r}",
        )
    if not lane_binding.strip():
        return AutoTargetRoot(
            reason=REFUSE_LANE_BINDING_UNBOUND,
            detail=(
                f"lane {lane!r} carries no worktree binding (an empty "
                "`worktree_identity`), so no path can be verified against it"
            ),
        )
    if not (lane_worktree or "").strip():
        return AutoTargetRoot(
            reason=REFUSE_LANE_WORKTREE_UNRESOLVED,
            detail=(
                f"lane {lane!r}'s worktree binding matched no unique live worktree of this "
                "repo (pruned, moved, or non-unique)"
            ),
        )
    return AutoTargetRoot(
        root=lane_worktree,
        basis=BASIS_LANE_WORKTREE,
        detail=f"lane {lane!r} worktree verified against its binding token",
    )


def resolve_herdr_auto_target_repo(
    repo_root: Path, target_info: Mapping[str, str]
) -> AutoTargetRoot:
    """The live composition: ``auto`` -> the target agent's own repo root, or a refusal.

    ``target_info`` is the synthesized herdr target record: its ``workspace_id`` / ``lane_id``
    are the resolved route identity's unit, and ``herdr_sender_workspace_id`` /
    ``herdr_sender_lane_id`` are the env-derived sender unit the same resolution already
    established (:func:`~...herdr_send_entry.resolve_herdr_send_target`) — so no second
    inventory read can disagree with the one that chose the target.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
        bind_lane_worktree,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )

    classified = classify_auto_target_basis(
        sender_workspace_id=target_info.get("herdr_sender_workspace_id") or "",
        target_workspace_id=target_info.get("workspace_id") or "",
        sender_lane_id=target_info.get("herdr_sender_lane_id") or "",
        target_lane_id=target_info.get("lane_id") or "",
    )
    if classified.basis == BASIS_SENDER_LANE:
        # The #13331 case, now verified: the target IS this lane, so this checkout is its root.
        return AutoTargetRoot(
            root=str(repo_root), basis=BASIS_SENDER_LANE, detail=classified.detail
        )
    if classified.basis != BASIS_LANE_WORKTREE:
        return AutoTargetRoot(reason=classified.reason, detail=classified.detail)

    # The lifecycle rows are scoped by the CREATING repo's workspace identity, which a linked
    # worktree inherits from its main checkout — a distinct resolver from the live route unit
    # above, and the one `sublane create` stamped (`repo_scope_workspace_id`, j#73469). A legacy
    # `wt_`-unit lane therefore finds no row here and refuses, rather than being answered from
    # the wrong scope.
    scope = repo_scope_workspace_id(repo_root)
    if not scope:
        return AutoTargetRoot(
            reason=REFUSE_IDENTITY_UNATTESTED,
            detail=(
                "this repo resolves no workspace identity, so the target lane's lifecycle "
                "row cannot be scoped"
            ),
        )

    # Read through the reader DIRECTLY rather than `load_lane_lifecycle_readonly`, which folds
    # every failure — including the typed `LaneLifecycleReaderUpgradeRequired` — into `None`
    # (review j#95843 finding 1). "The store is newer than this runtime" and "the store is
    # unreadable" have different repairs, so the distinction has to survive to the outcome.
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        LaneLifecycleReader,
        LaneLifecycleReaderUpgradeRequired,
    )
    from mozyo_bridge.core.state.lane_lifecycle_schema import LaneLifecycleError

    try:
        rows = LaneLifecycleReader().records()
    except LaneLifecycleReaderUpgradeRequired as exc:
        return AutoTargetRoot(
            reason=REFUSE_STORE_UPGRADE_REQUIRED,
            detail=(
                "the lane lifecycle authority is a NEWER schema than this runtime can read "
                f"({exc}); route via a current runtime — never downgrade the store"
            ),
        )
    except (LaneLifecycleError, OSError) as exc:
        return AutoTargetRoot(
            reason=REFUSE_STORE_UNREADABLE,
            detail=f"the lane lifecycle authority is unreadable ({exc}); fail closed",
        )
    lane = _norm_lane(target_info.get("lane_id") or "")
    row = next(
        (
            record
            for record in rows
            if (record.repo_workspace_id or "").strip() == scope
            and _norm_lane(record.lane_id) == lane
        ),
        None,
    )
    if row is None:
        return decide_lane_worktree_root(
            target_lane_id=lane, lane_binding=None, lane_worktree=""
        )
    bound = bind_lane_worktree(
        Path(repo_root),
        rows,
        workspace=row.repo_workspace_id,
        lane=row.lane_id,
        generation=row.lane_generation,
    )
    return decide_lane_worktree_root(
        target_lane_id=lane,
        lane_binding=row.worktree_identity or "",
        lane_worktree="" if bound is None else str(bound[0]),
    )


__all__ = (
    "BASIS_LANE_WORKTREE",
    "BASIS_SENDER_LANE",
    "REFUSE_FOREIGN_WORKSPACE",
    "REFUSE_IDENTITY_UNATTESTED",
    "REFUSE_LANE_BINDING_ABSENT",
    "REFUSE_LANE_BINDING_UNBOUND",
    "REFUSE_LANE_WORKTREE_UNRESOLVED",
    "REFUSE_STORE_UNREADABLE",
    "AutoTargetBasis",
    "AutoTargetRoot",
    "classify_auto_target_basis",
    "decide_lane_worktree_root",
    "resolve_herdr_auto_target_repo",
)
