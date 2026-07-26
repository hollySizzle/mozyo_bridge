"""The lane worktree-binding axis probe seam (Redmine #14475, review j#88477 F1).

A guarded recovery must not relaunch a pair into a lane whose canonical
``worktree_identity`` is absent, or is some other worktree's — that is how #14462 j#88463
handed an unbound row on to a later surface and reached a closed-and-unrelaunchable gateway.

The axis itself is observed by the injected ops adapter (only it knows the recovery worktree);
this leaf is the fail-closed *seam* around that call, extracted so the resolution rule lives in
one place rather than being re-derived per surface — and so the recovery use case module stays
under the module-health line without an allowlist bump.
"""

from __future__ import annotations

from typing import Any

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E501
    LAUNCH_AUTHORITY_UNKNOWN,
    normalize_launch_authority_reason,
)


def resolve_worktree_binding_reason(ops: Any, *, lane: str, record: Any) -> str:
    """The lane's canonical worktree-binding axis as a closed token. (read-only, fail-closed)

    Returns a :data:`...lane_launch_authority.LAUNCH_AUTHORITY_REASONS` member. Every
    unreadable path collapses to :data:`...LAUNCH_AUTHORITY_UNKNOWN`, which never authorizes a
    recovery:

    - no record to verify against (nothing the axis could be true *of*);
    - an ops adapter with no such probe (a pre-#14475 adapter must not ride a green default);
    - a probe that raises (an unreadable binding is not a verified one);
    - a probe that returns an off-vocabulary token (normalized, never echoed onward — so an
      error string bearing a path or a secret cannot reach a durable record this way).
    """
    if record is None:
        return LAUNCH_AUTHORITY_UNKNOWN
    probe = getattr(ops, "lane_worktree_binding_reason", None)
    if probe is None:
        return LAUNCH_AUTHORITY_UNKNOWN
    try:
        return normalize_launch_authority_reason(probe(lane=lane, record=record))
    except Exception:  # noqa: BLE001 - an unreadable binding is never "current"
        return LAUNCH_AUTHORITY_UNKNOWN


__all__ = ("resolve_worktree_binding_reason",)
