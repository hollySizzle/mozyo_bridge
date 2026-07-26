"""The lane checkout-authority axes, as one reusable leaf (Redmine #14475).

The recovery surfaces all ask the same question before an owed effect: *is this lane still
bound to THIS checkout, right now?* The answer is three axes — the lifecycle row's canonical
``worktree_identity`` equals the token freshly derived from the recovery root, that root
resolves to a live git checkout, and its current branch is the lane's own.

Extracted from the live recover-pair adapter so the axes live in one small module rather than
inside a 1000-line adapter (module-health leaf extraction; behaviour unchanged). Keeping them
together also keeps the two entry points honest: the classifier used by the preflight and the
one used at transport time are literally the same code.

Fail-closed everywhere: an unreadable store, an underivable token, an unreadable checkout, or a
failed branch read all return a blocking token — never ``ok``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    probe_worktree_resolved,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_BRANCH_DRIFTED,
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE,
    LAUNCH_AUTHORITY_WORKTREE_UNREADABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
    derive_lane_workspace_token,
)


def current_branch(path: str) -> str:
    """The worktree's current branch, or ``""`` (fail-closed). Read-only."""
    if not path or not Path(path).is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, capture_output=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def worktree_binding_reason(repo_root: Any, *, lane: str, record: Any) -> str:
    """Which checkout axis holds for this lane + root? (read-only, closed token)

    A matching token is NOT the whole contract: the token is derived from the worktree's PATH,
    so it still matches after the checkout has been switched to another branch or has stopped
    resolving. All three axes are required, and the branch read is checked RAW before
    normalization — ``_norm_lane("")`` is the ``default`` lane, so normalizing first would turn
    a failed read into a green axis on a lane named ``default``.
    """
    pinned = _norm(getattr(record, "worktree_identity", ""))
    if not pinned:
        return LAUNCH_AUTHORITY_WORKTREE_UNBOUND
    try:
        # Canonical (symlink-resolved) root, per ``derive_lane_workspace_token``'s contract:
        # mint-time and resolve-time must agree.
        live = _norm(derive_lane_workspace_token(str(Path(repo_root).resolve())))
    except Exception:  # noqa: BLE001 - an underivable token fails closed
        return LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE
    if not live:
        return LAUNCH_AUTHORITY_WORKTREE_UNDERIVABLE
    if live != pinned:
        return LAUNCH_AUTHORITY_WORKTREE_MISMATCH
    try:
        if probe_worktree_resolved(str(repo_root)) is not True:
            return LAUNCH_AUTHORITY_WORKTREE_UNREADABLE
    except Exception:  # noqa: BLE001 - an unreadable worktree fails closed
        return LAUNCH_AUTHORITY_WORKTREE_UNREADABLE
    raw_branch = current_branch(str(repo_root))
    if not raw_branch:
        return LAUNCH_AUTHORITY_WORKTREE_UNREADABLE
    if _norm_lane(raw_branch) != _norm_lane(lane):
        return LAUNCH_AUTHORITY_BRANCH_DRIFTED
    return LAUNCH_AUTHORITY_OK


def checkout_authority_current(
    repo_root: Any,
    *,
    workspace_id: str,
    lane: str,
    lifecycle_home: Optional[Path] = None,
) -> bool:
    """Are the axes current RIGHT NOW, against a FRESH lifecycle read? (fail-closed)

    The transport-direct form: it re-reads the row itself rather than taking a caller's
    snapshot, because its whole purpose is to observe a change that happened after that
    snapshot was taken. An unreadable store or an absent row is never "current".
    """
    from mozyo_bridge.core.state.lane_lifecycle import (
        LaneLifecycleError,
        LaneLifecycleKey,
        LaneLifecycleStore,
    )

    try:
        record = LaneLifecycleStore(home=lifecycle_home).get(
            LaneLifecycleKey(_norm(workspace_id), _norm_lane(lane))
        )
    except (LaneLifecycleError, ValueError, OSError):
        return False
    if record is None:
        return False
    return worktree_binding_reason(repo_root, lane=lane, record=record) == (
        LAUNCH_AUTHORITY_OK
    )


__all__ = (
    "current_branch",
    "worktree_binding_reason",
    "checkout_authority_current",
)
