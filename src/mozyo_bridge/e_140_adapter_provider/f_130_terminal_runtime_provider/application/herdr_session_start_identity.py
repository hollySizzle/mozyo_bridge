"""Read-only identity resolution for `herdr session-start` (Redmine #13261).

Split out of :mod:`.herdr_session_start` (Redmine #13882 R8) alongside the CLI surface.
These two helpers answer "what lane / workspace is this?" by *reading* metadata and the
registry — they resolve, they never actuate — so they are a cohesive unit apart from the
use case that launches and adopts. The relocation freed the room the launch-admission
lock and the R8 compatibility facade needed under the module-health ceiling.

Both are module-private (no caller outside the use case, verified before the move), so
unlike `cmd_herdr_session_start` they need no compatibility facade.
"""

from __future__ import annotations

import json
from pathlib import Path

from mozyo_bridge.core.state.workspace_registry import (
    ANCHOR_LEGACY_RELATIVE,
    ANCHOR_RELATIVE,
    _is_linked_worktree,
    anchor_resolution,
    load_workspace_by_path,
    read_anchor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    _norm,
    derive_lane_workspace_token,
)


def _lane_id_from_metadata(resolved_root: Path) -> str:
    """The recorded lane id for a lane worktree (``""`` when unrecorded).

    Shared project workspace model (Redmine #13377): a lane worktree's slots are
    ``mzb1_<project-ws>_<role>_<lane>``, so a relaunch from the worktree must
    recover the SAME lane segment ``sublane create`` launched with. The lane
    metadata record — keyed on the worktree's stable per-path token — carries it
    (``lane_id``, falling back to ``lane_label`` for a record written before the
    column existed). Read-only and fail-open to ``""`` (the caller fails closed:
    a lane slot is never minted with a guessed lane).
    """
    from mozyo_bridge.core.state.lane_metadata import load_lane_records

    token = derive_lane_workspace_token(str(resolved_root))
    record = load_lane_records().get(token)
    if record is None:
        return ""
    return _norm(getattr(record, "lane_id", "")) or _norm(
        getattr(record, "lane_label", "")
    )


def _resolve_workspace_id_readonly(resolved_root: Path) -> str:
    """Resolve a registered workspace's ``workspace_id`` for ``--dry-run``, read-only.

    The query-side mirror of :func:`register_workspace`'s identity precedence
    (Redmine #13595): an existing **anchor** pins the id, else an existing
    **registry row** for this canonical path — but purely read-only (never create
    the registry, write ``last_seen``, or touch the anchor; the exact defect this
    fixes called ``register_workspace`` before the dry-run branch). Fails closed
    rather than minting a fake assigned identity: both anchor names present is the
    same ambiguity the write path refuses (guess nothing), and no anchor + no
    registry row means no durable identity yet (register first). Linked worktrees
    never reach here — the :func:`prepare_session` inheritance branch
    (:func:`herdr_workspace_segment`) resolves them read-only.
    """
    if anchor_resolution(resolved_root).both_exist:
        raise HerdrSessionStartError(
            f"both {ANCHOR_RELATIVE.as_posix()} and "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} exist in {resolved_root}; the new "
            "name is authoritative but a dry-run refuses to guess which identity a "
            f"real session-start would use — remove the legacy "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} and re-run "
            "`mozyo-bridge workspace register`, then --dry-run"
        )
    anchor = read_anchor(resolved_root)
    if isinstance(anchor, dict):
        workspace_id = _norm(anchor.get("workspace_id"))
        if workspace_id:
            return workspace_id
    record = load_workspace_by_path(resolved_root)
    if record is not None:
        workspace_id = _norm(record.workspace_id)
        if workspace_id:
            return workspace_id
    raise HerdrSessionStartError(
        f"dry-run cannot resolve a durable workspace identity for {resolved_root} "
        "and refuses to register it (a dry-run has no side effect) or mint a fake "
        "one; run `mozyo-bridge workspace register` first, then re-run with --dry-run"
    )


__all__ = (
    "_lane_id_from_metadata",
    "_resolve_workspace_id_readonly",
)
