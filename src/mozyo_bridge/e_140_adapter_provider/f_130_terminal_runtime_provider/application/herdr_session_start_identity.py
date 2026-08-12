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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mozyo_bridge.core.state.workspace_registry import (
    ANCHOR_LEGACY_RELATIVE,
    ANCHOR_RELATIVE,
    _is_linked_worktree,
    anchor_resolution,
    load_workspace_by_id,
    load_workspace_by_path,
    read_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_lane_topology import (  # noqa: E501
    bind_lane_worktree,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import LaneLifecycleReader
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
    require_alias_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
    herdr_workspace_segment,
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


def _resolve_workspace_id_readonly(
    resolved_root: Path, *, home: Path | None = None
) -> str:
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
    record = load_workspace_by_path(resolved_root, home=home)
    if record is not None:
        workspace_id = _norm(record.workspace_id)
        if workspace_id:
            return workspace_id
    raise HerdrSessionStartError(
        f"dry-run cannot resolve a durable workspace identity for {resolved_root} "
        "and refuses to register it (a dry-run has no side effect) or mint a fake "
        "one; run `mozyo-bridge workspace register` first, then re-run with --dry-run"
    )


def resolve_workspace_id_if_registered(
    resolved_root: Path, *, home: Path | None = None
) -> str:
    """Return an existing durable workspace id, or ``""`` without writing."""
    try:
        return _resolve_workspace_id_readonly(resolved_root, home=home)
    except HerdrSessionStartError:
        return ""


@dataclass(frozen=True, repr=False)
class PrivateWorktreeBinding:
    """Sealed lane-worktree selector; its private identity never enters a repr."""

    workspace_id: str
    lane_id: str
    lane_generation: int
    worktree_identity: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class PrivateRestoreContainerBinding:
    """Sealed pane-only container anchor for an offline restore invocation."""

    workspace_id: str
    tab_id: str
    pane_locator: str
    terminal_id: str = field(repr=False)


def private_workspace_effect_fence(
    repo_root: Path,
    *,
    expected_workspace_id: str,
    expected_worktree: PrivateWorktreeBinding | None = None,
    home: Path | None = None,
) -> Callable[[], None] | None:
    """Rejoin the exact private cwd authority at each path-consuming effect edge."""

    if not expected_workspace_id:
        if expected_worktree is not None:
            raise HerdrSessionStartError("private worktree authority lacks workspace")
        return None
    if expected_worktree is not None and (
        expected_worktree.workspace_id != expected_workspace_id
        or not expected_worktree.lane_id
        or type(expected_worktree.lane_generation) is not int
        or expected_worktree.lane_generation < 1
        or not expected_worktree.worktree_identity
    ):
        raise HerdrSessionStartError("private worktree authority is malformed")
    consumed_path = Path(repo_root)

    def require_exact_binding() -> None:
        try:
            actual = consumed_path.expanduser().resolve(strict=True)
            observed = (
                herdr_workspace_segment(actual, home=home)
                if _is_linked_worktree(actual)
                else resolve_workspace_id_if_registered(actual, home=home)
            )
            require_alias_identity(expected_workspace_id, observed)
            if expected_worktree is None:
                return
            record = load_workspace_by_id(expected_workspace_id, home=home)
            rows = LaneLifecycleReader(home=home).records()
            matches = [
                row
                for row in rows
                if row.repo_workspace_id == expected_worktree.workspace_id
                and row.lane_id == expected_worktree.lane_id
                and row.lane_generation == expected_worktree.lane_generation
                and row.worktree_identity == expected_worktree.worktree_identity
            ]
            bound = (
                bind_lane_worktree(
                    Path(record.canonical_path),
                    matches,
                    workspace=expected_worktree.workspace_id,
                    lane=expected_worktree.lane_id,
                    generation=expected_worktree.lane_generation,
                )
                if record is not None and len(matches) == 1
                else None
            )
            if bound is None or bound[0].resolve(strict=True) != actual:
                raise HerdrSessionStartError("private worktree binding changed")
        except HerdrSessionStartError:
            raise
        except Exception as exc:  # noqa: BLE001 - unreadable authority is refusal
            raise HerdrSessionStartError(
                "private workspace effect authority is unreadable"
            ) from exc

    return require_exact_binding


__all__ = (
    "PrivateRestoreContainerBinding",
    "PrivateWorktreeBinding",
    "_lane_id_from_metadata",
    "_resolve_workspace_id_readonly",
    "private_workspace_effect_fence",
    "resolve_workspace_id_if_registered",
)
