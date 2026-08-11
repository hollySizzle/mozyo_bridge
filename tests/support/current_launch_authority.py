"""Seed the CURRENT launch authority a replacement plan is read against (#14741 j#97105).

Ruling j#97105: the participant's own current launch-generation row is the only binding a
plan may read capability from. A fixture with no such row does not represent a production
lane -- it represents a lane whose current authority is missing, which is a typed
zero-effect refusal. So a test that wants the pre-#14741 (legacy, byte-invariant) path has
to say so explicitly, by seeding a canonical legacy row for the exact participant.

That is the whole point of this helper: "legacy" stops being the absence of evidence and
becomes a recorded fact about a specific slot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

#: A canonical PRE-#14741 startup action id: the untagged `startup-<64hex>` shape. A
#: receipt-capable action carries the `ir1` capability tag instead, which is exactly the
#: distinction the planner reads (j#96892) -- so this constant is what makes a seeded row
#: mean "this slot predates identity receipts", rather than "nobody looked".
LEGACY_ACTION_ID = "startup-" + "1a2b3c4d" * 8

#: The receipt-capable counterpart, for the tests that pin the other branch.
RECEIPT_CAPABLE_ACTION_ID = "startup-ir1-" + "5e6f7a8b" * 8

_OBSERVED_AT = "2026-07-15T12:00:00+00:00"


def seed_current_generation(
    home: Path,
    *,
    workspace_id: str,
    lane_id: str,
    role: str,
    assigned_name: str,
    locator: str,
    action_id: str = LEGACY_ACTION_ID,
    attested: bool = True,
    terminal_id: str = "",
) -> None:
    """Record ``assigned_name``'s current launch generation under ``home``.

    ``attested=False`` leaves the row ``pending``, which is the "the launch never proved it
    came up" case -- a typed refusal, not a mismatch.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import (
        HerdrLaunchGenerationStore,
    )

    store = HerdrLaunchGenerationStore(home=Path(home))
    store.reserve_pending(
        assigned_name=assigned_name,
        startup_action_id=action_id,
        workspace_id=workspace_id,
        role=role,
        lane_id=lane_id,
    )
    if not attested:
        return
    store.finalize(
        assigned_name=assigned_name,
        startup_action_id=action_id,
        workspace_id=workspace_id,
        role=role,
        lane_id=lane_id,
        locator=locator,
        terminal_id=terminal_id or f"terminal:{locator}",
        verdict="present",
        observed_at=_OBSERVED_AT,
    )


def seed_current_generations(home: Path, participants: Iterable, *, workspace_id: str,
                             action_id: str = LEGACY_ACTION_ID) -> None:
    """The same, for every participant a plan will name.

    Takes anything with the participant attributes, so a fixture can pass its own pins
    rather than restating five axes per slot -- and, more importantly, so the seeded row is
    the participant BY CONSTRUCTION. A helper that let the two drift would be seeding a
    different slot and calling the resulting refusal a bug in the code under test.
    """
    for pin in participants:
        seed_current_generation(
            home,
            workspace_id=workspace_id,
            lane_id=getattr(pin, "lane_id", ""),
            role=getattr(pin, "provider", ""),
            assigned_name=getattr(pin, "assigned_name", ""),
            locator=getattr(pin, "old_locator", "") or getattr(pin, "locator", ""),
            action_id=action_id,
        )


def seed_completed_current_generation(
    home: Path, *, workspace_id: str, lane_id: str, role: str,
    assigned_name: str, locator: str, terminal_id: str = "",
) -> str:
    """Seed a terminal-bound generation with its exact completed startup transaction."""
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS, PHASE_HEALTH_CHECK, Participant,
        StartupTransactionFence, StartupUnit,
    )

    fence = StartupTransactionFence(home=home)
    action = fence.reserve(StartupUnit(workspace_id, lane_id, (role,)),
                           f"current-generation-{role}-{assigned_name}")
    fence.record_participant(action.action_id, Participant(
        role=role, assigned_name=assigned_name, locator=locator, receipt="workspace=current",
    ))
    fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
    fence.set_phase(action.action_id, PHASE_COMPLETED_SUCCESS)
    seed_current_generation(
        home, workspace_id=workspace_id, lane_id=lane_id, role=role,
        assigned_name=assigned_name, locator=locator, action_id=action.action_id,
        terminal_id=terminal_id,
    )
    return action.action_id


__all__ = (
    "LEGACY_ACTION_ID",
    "RECEIPT_CAPABLE_ACTION_ID",
    "seed_current_generation",
    "seed_current_generations",
    "seed_completed_current_generation",
)
