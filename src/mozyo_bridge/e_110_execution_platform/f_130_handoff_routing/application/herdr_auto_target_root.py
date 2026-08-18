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
    DEFAULT_LANE,
    _norm_lane,
)

#: The target IS the sender's own lane: the sender's repo root is the target's repo root.
BASIS_SENDER_LANE = "sender_lane"
#: The target is another lane of this workspace: its canonical worktree is the root.
BASIS_LANE_WORKTREE = "lane_worktree"
#: The target is the workspace's DEFAULT lane (the coordinator lane, which lives in the main
#: checkout and structurally owns no lifecycle row — only ``sublane create`` writes rows), so
#: its root is the workspace registry's verified canonical checkout (Redmine #15707 b).
BASIS_WORKSPACE_CANONICAL = "workspace_canonical"

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

        **Every value here is bounded and path-free.** ``subreason`` / ``basis`` are closed
        vocabulary tokens; ``detail`` is a fixed sentence that may name lane / workspace ids
        and exception TYPES, never a filesystem path and never raw exception text. This
        payload reaches the wire outcome, the pasteable record and stderr, so an absolute path
        placed here is published three ways — which is exactly what R4 did before review
        j#95911 finding 2 (an interpolated exception carried the operator's home path).
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


def decide_default_lane_root(
    *,
    target_lane_id: str,
    canonical_root: str,
    liveness: Optional[Mapping[str, object]],
    scope_matches: bool,
) -> AutoTargetRoot:
    """Pure: the DEFAULT-lane (coordinator) answer from the registry's verified facts (#15707 b).

    The default lane is the coordinator lane: it lives in the workspace's main checkout and
    structurally owns no lifecycle row (only ``sublane create`` writes rows), so the row-based
    cross-lane resolution above can never answer it — measured as the systematic
    ``lane_binding_absent`` callback refusals #15701 j#107992 / #15704 j#108011. Its root is
    instead the workspace registry's ``canonical_path``, admitted ONLY when every stated fact
    verifies (authority is not weakened — an unverified registry answer refuses exactly like
    the absent binding did):

    - ``canonical_root`` non-empty (the registry knows this workspace's checkout);
    - ``liveness`` proves it is a live git MAIN worktree (``exists`` / ``is_dir`` / ``is_git``
      / ``is_main_worktree`` all true — the #13152 "registry hijacked by a linked worktree /
      dead path" shape refuses);
    - ``scope_matches``: the checkout re-derives this workspace's own scope identity, so a
      same-named registry row for a different repo can never answer.

    Every refusal keeps :data:`REFUSE_LANE_BINDING_ABSENT` (the wire reason and the durable
    subreason vocabulary are unchanged); the detail stays a fixed, path-free sentence
    (j#95911 finding 2). A non-default lane never reaches this decision.
    """
    lane = _norm_lane(target_lane_id)
    absent = f"no lifecycle row owns lane {lane!r}"
    if lane != DEFAULT_LANE:
        return AutoTargetRoot(reason=REFUSE_LANE_BINDING_ABSENT, detail=absent)
    if not (canonical_root or "").strip():
        return AutoTargetRoot(
            reason=REFUSE_LANE_BINDING_ABSENT,
            detail=absent + "; the workspace registry names no canonical checkout either",
        )
    live = liveness or {}
    verified = (
        live.get("exists") is True
        and live.get("is_dir") is True
        and live.get("is_git") is True
        and live.get("is_main_worktree") is True
    )
    if not verified:
        return AutoTargetRoot(
            reason=REFUSE_LANE_BINDING_ABSENT,
            detail=(
                absent
                + "; the registry's canonical checkout could not be verified as a live "
                "main worktree"
            ),
        )
    if not scope_matches:
        return AutoTargetRoot(
            reason=REFUSE_LANE_BINDING_ABSENT,
            detail=(
                absent
                + "; the registry's canonical checkout does not re-derive this "
                "workspace's identity"
            ),
        )
    return AutoTargetRoot(
        root=canonical_root,
        basis=BASIS_WORKSPACE_CANONICAL,
        detail=(
            f"lane {lane!r} is the coordinator lane; the workspace registry's canonical "
            "checkout verified as this workspace's live main worktree"
        ),
    )


def _resolve_default_lane_root(scope: str, lane: str) -> AutoTargetRoot:
    """Live composition for the DEFAULT-lane fallback: read-only registry + liveness probe.

    Supplies :func:`decide_default_lane_root`'s stated facts. Every read is tolerant and
    read-only (registry opens ``mode=ro``; the liveness probe only classifies), so this
    fallback can refuse but never mutate or create state.
    """
    from mozyo_bridge.core.state.workspace_registry import (
        load_workspace_by_id,
        probe_canonical_liveness,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )

    try:
        record = load_workspace_by_id(scope)
    except Exception:  # noqa: BLE001 - an unreadable registry refuses like an absent one (read-only path)
        record = None
    canonical = str(getattr(record, "canonical_path", "") or "").strip()
    liveness = probe_canonical_liveness(canonical) if canonical else None
    scope_matches = False
    if canonical and liveness and liveness.get("is_main_worktree") is True:
        try:
            scope_matches = repo_scope_workspace_id(Path(canonical)) == scope
        except Exception:  # noqa: BLE001 - an unresolvable identity is a refusal, never a crash
            scope_matches = False
    return decide_default_lane_root(
        target_lane_id=lane,
        canonical_root=canonical,
        liveness=liveness,
        scope_matches=scope_matches,
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
    except LaneLifecycleReaderUpgradeRequired:
        # The exception text is DELIBERATELY not interpolated (review j#95911 finding 2):
        # it carries the store's absolute path, and `detail` travels onto the structured
        # outcome, the pasteable record and stderr. R4 interpolated it and leaked the
        # operator's home path into all three. The subreason already says everything the
        # reader needs; the path is host-local state, not a fact the record may hold.
        return AutoTargetRoot(
            reason=REFUSE_STORE_UPGRADE_REQUIRED,
            detail=(
                "the lane lifecycle authority is a NEWER schema than this runtime can "
                "read; route via a current runtime — never downgrade the store"
            ),
        )
    except (LaneLifecycleError, OSError) as exc:
        # Only the exception TYPE — a bounded, path-free token (same reason as above).
        return AutoTargetRoot(
            reason=REFUSE_STORE_UNREADABLE,
            detail=(
                "the lane lifecycle authority is unreadable "
                f"({type(exc).__name__}); fail closed"
            ),
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
        if lane == DEFAULT_LANE:
            # Redmine #15707 (b): the default lane is the coordinator lane — main-checkout
            # resident, structurally row-less — so the absent binding is answered from the
            # workspace registry's VERIFIED canonical checkout instead of refusing outright.
            # Anything unverified keeps the exact `lane_binding_absent` refusal below.
            return _resolve_default_lane_root(scope, lane)
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
    "BASIS_WORKSPACE_CANONICAL",
    "REFUSE_FOREIGN_WORKSPACE",
    "REFUSE_IDENTITY_UNATTESTED",
    "REFUSE_LANE_BINDING_ABSENT",
    "REFUSE_LANE_BINDING_UNBOUND",
    "REFUSE_LANE_WORKTREE_UNRESOLVED",
    "REFUSE_STORE_UNREADABLE",
    "AutoTargetBasis",
    "AutoTargetRoot",
    "classify_auto_target_basis",
    "decide_default_lane_root",
    "decide_lane_worktree_root",
    "resolve_herdr_auto_target_repo",
)
