"""Standard live-adopt owner-row backfill (Redmine #13809 / #13810 F1).

The standard live-adopt path (``sublane create --no-dispatch --execute`` onto a live
gateway+worker pair) skips ``append_lane_column``, so it never reached the create-path
lifecycle declaration and the adopted lane stayed **owner-rowless** — the measured
``original_identity_unknown`` that blocks ``sublane hibernate`` (#13809).

This module is the fail-closed gate + declaration for that path, extracted from
:class:`...sublane_actuator_herdr_ops.HerdrSublaneActuatorOps` so the ops adapter stays a
cohesive, under-threshold unit (the ``herdr_lane_topology`` / ``herdr_pane_lifecycle``
extraction precedent). The ops adapter resolves the live inventory into the
``(workspace_id, lane_id, {role: (locator, placement)})`` slots and hands them here; this
does the readable/unambiguous gate and declares the owner binding through the common
:class:`...lane_declaration.LaneDeclarationStore.declare_lane` — the same fail-closed,
idempotent authority a create records, **without appending a process**.

The typed live-pin *snapshot* (``declared_slots`` with per-slot runtime revision /
attestation) is deliberately NOT built here: the herdr ``agent list`` inventory carries
only the slot name + locator, so a full ``ProcessGenerationPin`` cannot be attested from
it — building that snapshot is the live-attestation adapter's concern (#13780). The
standard issue-lane adopt declares the owner row gated on the resolvable live pair, never a
fabricated runtime revision. No process is closed/relaunched and no route is mutated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional

from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DecisionPointer,
    DecisionPointerError,
    LaneLifecycleError,
    LaneLifecycleKey,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
    derive_directory_lane_token,
    derive_lane_workspace_token,
)

# -- outcome vocabulary (a status token, never an exception across the boundary) ---------

ADOPT_DECL_NO_ANCHOR = "no_exact_anchor"
ADOPT_DECL_UNRESOLVED_UNIT = "unresolved_unit"
ADOPT_DECL_INCOMPLETE_PAIR = "incomplete_live_pair"
ADOPT_DECL_AMBIGUOUS_LOCATORS = "ambiguous_locators"
ADOPT_DECL_BAD_TOKEN = "unresolvable_worktree_token"
ADOPT_DECL_BAD_ANCHOR = "unusable_decision_anchor"
#: The declaration was applied (a fresh owner row, or an idempotent exact-duplicate adopt).
ADOPT_DECL_DECLARED = "declared"
#: The common declaration service refused (another lane owns the issue, or a divergent
#: binding already exists): a legitimate zero-write, not a store error.
ADOPT_DECL_OWNER_CONFLICT = "owner_conflict"
ADOPT_DECL_DECLARE_ERROR = "declare_error"

#: The zero-write outcomes: no owner row was written (fail-closed), for any reason other
#: than a successful declaration or a store error surfaced to the caller.
ADOPT_DECL_ZERO_WRITE = frozenset(
    {
        ADOPT_DECL_NO_ANCHOR,
        ADOPT_DECL_UNRESOLVED_UNIT,
        ADOPT_DECL_INCOMPLETE_PAIR,
        ADOPT_DECL_AMBIGUOUS_LOCATORS,
        ADOPT_DECL_BAD_TOKEN,
        ADOPT_DECL_BAD_ANCHOR,
        ADOPT_DECL_OWNER_CONFLICT,
    }
)


def _worktree_token(repo_root: Path, worktree_path: str, lane_label: str) -> Optional[str]:
    """The lane's canonical worktree identity token, or ``None`` if unresolvable.

    The SAME token the create-path metadata / lifecycle declaration is keyed on
    (``_record_lane_metadata``): a non-git directory lane whose runtime root collapses onto
    the workspace root is scoped by ``(workspace root, lane_label)`` so two lanes on one
    root keep distinct tokens; a git lane's distinct worktree keeps its ``wt_`` token.
    Writer and reader compute it identically so the fail-closed lifecycle row never drifts.
    """
    try:
        resolved = Path(worktree_path).expanduser().resolve()
        is_workspace_root = resolved == repo_root.expanduser().resolve()
    except OSError:
        return None
    if is_workspace_root:
        return derive_directory_lane_token(str(resolved), lane_label)
    return derive_lane_workspace_token(str(resolved))


def declare_adopted_owner_row(
    *,
    journal: str,
    issue: str,
    lane_label: str,
    repo_root: Path,
    worktree_path: str,
    providers: tuple[str, str],
    resolved: tuple[str, str, Mapping[str, tuple[str, str]]],
    store_factory: Callable[[], LaneDeclarationStore] = LaneDeclarationStore,
) -> str:
    """Declare an adopted lane's owner binding, or fail closed zero-write (Redmine #13809).

    ``providers`` is the ``(gateway_provider, worker_provider)`` pair the lane runs;
    ``resolved`` is the ops adapter's live-inventory resolution
    ``(workspace_id, lane_id, {role: (locator, placement)})``. Returns a status token from
    the outcome vocabulary — the caller logs / records it. It writes an owner row ONLY when:

    - an exact durable anchor is present (``journal`` + ``issue`` + ``lane_label``);
    - the live unit resolved (non-empty ``workspace_id`` + ``lane_id``);
    - BOTH expected provider slots are live (a rowless / half-live / recycled pair is not
      the exact live issue-lane pair this adopt would own);
    - their locators are present and distinct (a collision is an ambiguous target);
    - the worktree token and the decision anchor are usable.

    On all those, it calls :meth:`declare_lane` (issue binding), which itself refuses an
    owner conflict / divergent re-declaration zero-write and is idempotent on a duplicate
    exact adopt. A store error is caught and returned as ``declare_error`` so the caller can
    log it without breaking the actuation (best-effort, like the create-path declare).
    """
    journal = _norm(journal)
    issue = _norm(issue)
    lane_label = _norm(lane_label)
    if not (journal and issue and lane_label):
        return ADOPT_DECL_NO_ANCHOR

    workspace_id, lane_id, slots = resolved
    workspace = _norm(workspace_id)
    lane = _norm(lane_id)
    if not (workspace and lane):
        return ADOPT_DECL_UNRESOLVED_UNIT

    gateway_provider, worker_provider = providers
    gateway = slots.get(gateway_provider)
    worker = slots.get(worker_provider)
    if not gateway or not worker:
        return ADOPT_DECL_INCOMPLETE_PAIR
    gw_locator = _norm(gateway[0])
    wk_locator = _norm(worker[0])
    if not gw_locator or not wk_locator or gw_locator == wk_locator:
        return ADOPT_DECL_AMBIGUOUS_LOCATORS

    token = _worktree_token(repo_root, worktree_path, lane_label)
    if token is None:
        return ADOPT_DECL_BAD_TOKEN

    try:
        key = LaneLifecycleKey(workspace, lane)
        decision = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
    except (DecisionPointerError, ValueError):
        return ADOPT_DECL_BAD_ANCHOR

    try:
        outcome = store_factory().declare_lane(
            key,
            decision=decision,
            binding_kind=BINDING_KIND_ISSUE,
            issue_id=issue,
            worktree_identity=token,
        )
    except (LaneLifecycleError, DecisionPointerError, OSError):
        return ADOPT_DECL_DECLARE_ERROR
    # ``applied`` is true for a fresh declare AND an idempotent exact-duplicate adopt; a
    # refusal (owner conflict / divergent binding) wrote nothing — a legitimate zero-write.
    return ADOPT_DECL_DECLARED if outcome.applied else ADOPT_DECL_OWNER_CONFLICT


__all__ = (
    "declare_adopted_owner_row",
    "ADOPT_DECL_DECLARED",
    "ADOPT_DECL_OWNER_CONFLICT",
    "ADOPT_DECL_DECLARE_ERROR",
    "ADOPT_DECL_NO_ANCHOR",
    "ADOPT_DECL_UNRESOLVED_UNIT",
    "ADOPT_DECL_INCOMPLETE_PAIR",
    "ADOPT_DECL_AMBIGUOUS_LOCATORS",
    "ADOPT_DECL_BAD_TOKEN",
    "ADOPT_DECL_BAD_ANCHOR",
    "ADOPT_DECL_ZERO_WRITE",
)
