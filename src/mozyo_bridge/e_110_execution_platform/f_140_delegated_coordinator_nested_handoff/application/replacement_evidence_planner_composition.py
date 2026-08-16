"""Live composition for the evidence-aware participant planner (Redmine #14741 j#97093).

The planner itself (:mod:`.replacement_evidence_planner`) reads nothing: every authority
arrives as an injected port, which is what let it be pinned against hostile inputs without
a store, a lane or a launch. This module is the one place those ports are bound to the real
stores, so the five recovery paths that create a replacement transaction share ONE authority
instead of each deciding for itself what "this participant's evidence" means.

Three properties are deliberate:

* **The expected launch cause is not spelled here.** It is
  :data:`...LAUNCH_CAUSE_UPDATE_RELAUNCH`, imported from the provider registry that owns the
  closed vocabulary, and handed to the planner through the context. A second literal in
  e_110 would be a second owner of the same token.
* **The blocker -> cause mapping is not re-derived here** either: it is
  ``is_update_derived_blocker``, the registry's own declared-signature knowledge. A caller
  that re-guessed update-ness from pane text is precisely what j#96374 forbids.
* **Refusals are typed and total.** Nothing in this module raises at a caller: a refusal is
  a reason string, and the caller turns it into a zero-effect outcome BEFORE it plans,
  supersedes or actuates anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class EvidencePlanning:
    """The planner's answer, in the shape a use case can act on without try/except.

    Exactly one of ``participants`` / ``refusal`` is meaningful: a refusal carries no
    participants, because a partially planned manifest is the thing the planner refuses to
    produce in the first place.
    """

    participants: Tuple[Any, ...] = ()
    refusal: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refusal)


def _lifecycle_port(home: Optional[Path]):
    """``lane_id -> (lane_generation, lifecycle_revision)``, or ``None``.

    ``load_lane_lifecycle_readonly`` is the non-creating read: a diagnostic-shaped
    projection must never create ``state.sqlite`` just to plan. Exactly one row for the lane
    or nothing — an ambiguous lane is not a lane this plan can name.
    """

    def read(lane_id: str):
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            load_lane_lifecycle_readonly,
        )

        records = load_lane_lifecycle_readonly(home=home)
        if not records:
            return None
        matched = [
            record
            for record in records
            if str(getattr(record, "lane_id", "") or "") == lane_id
        ]
        if len(matched) != 1:
            return None
        # `lane_generation` / `revision`, which is what the record actually exposes. The
        # first cut read `generation`, an attribute no lifecycle record has ever had, so the
        # port answered `("", ...)` for every lane and the receipt-capable path could not
        # plan at all -- and I reported that as a platform limitation (j#97115) rather than
        # as the typo it was. Both are ints on the record; the planner wants exact text.
        return (
            str(getattr(matched[0], "lane_generation", "") or ""),
            str(getattr(matched[0], "revision", "") or ""),
        )

    return read


def _generation_port(home: Optional[Path], live_rows, allow_historical_absent: bool):
    def read(assigned_name: str):
        from mozyo_bridge.core.state.herdr_launch_generation import (
            HerdrLaunchGenerationStore, completed_generation_startup_token,
            verified_generation_token,
        )
        from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
            AGENT_KEY_TERMINAL_ID,
            _agent_locator,
            _norm,
            _norm_lane,
            terminal_identity_of_row,
            terminal_identity_of_live_slot,
            terminal_identity_snapshot_complete,
        )

        generation = HerdrLaunchGenerationStore(home=home).read(assigned_name)
        if generation is None:
            return None
        terminal_id = terminal_identity_of_live_slot(
            assigned_name, generation.locator, live_rows
        )
        if terminal_id and generation.terminal_id == terminal_id:
            return generation if verified_generation_token(
                home, assigned_name=generation.assigned_name,
                workspace_id=generation.workspace_id, role=generation.role,
                lane_id=generation.lane_id, locator=generation.locator,
                live_terminal_id=terminal_id, norm=_norm, norm_lane=_norm_lane,
            ) == generation.startup_action_id else None
        if allow_historical_absent and not terminal_identity_snapshot_complete(live_rows):
            return None
        claims = [
            row for row in live_rows
            if isinstance(row, Mapping)
            and (
                row.get(AGENT_KEY_NAME) == assigned_name
                or _agent_locator(row) == generation.locator
                or row.get(AGENT_KEY_TERMINAL_ID) == generation.terminal_id
            )
        ]
        return generation if (
            allow_historical_absent and generation.terminal_id and not claims
            and generation.verdict == VERDICT_PRESENT
            and completed_generation_startup_token(
                home, generation, norm=_norm, norm_lane=_norm_lane
            )
            == generation.startup_action_id
        ) else None

    return read


def _evidence_port(home: Optional[Path]):
    def read(**lookup):
        from mozyo_bridge.core.state.launch_identity_receipt import (
            LaunchIdentityReceiptStore,
        )

        return LaunchIdentityReceiptStore(home=home).read_bound_evidence(**lookup)

    return read


def _update_cause_port():
    """The registry's blocker -> typed launch cause, as a two-argument port.

    Returns ``""`` for anything the registry does not condemn, and the planner accepts a
    cause only when it byte-equals the expected one — so "not update-derived" and "some
    other token" fail the same way, on the same comparison.
    """

    def classify(provider: str, blocker_id: str) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
            LAUNCH_CAUSE_UPDATE_RELAUNCH,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
            is_update_derived_blocker,
        )

        if is_update_derived_blocker(provider, blocker_id):
            return LAUNCH_CAUSE_UPDATE_RELAUNCH
        return ""

    return classify


def build_evidence_planner(
    home: Optional[Path], *, live_rows=(), allow_historical_absent: bool = False
):
    """The planner, bound to the live authorities under ``home``.

    ``None`` means "wherever the stores resolve their own home to" — never ``Path()``,
    which is the CURRENT DIRECTORY and would silently point every authority at whatever
    the process happened to be launched from.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner import (  # noqa: E501
        ReplacementEvidencePlanner,
    )

    return ReplacementEvidencePlanner(
        generations=_generation_port(home, live_rows, allow_historical_absent),
        lifecycle=_lifecycle_port(home),
        evidence=_evidence_port(home),
        update_cause=_update_cause_port(),
    )


def plan_participants_with_evidence(
    participants: Sequence[Any],
    *,
    home: Optional[Path],
    workspace_id: str,
    lane_id: str,
    allow_historical_absent: bool = False,
    live_rows=None,
) -> EvidencePlanning:
    """Plan ``participants``, or return the typed reason this transaction must not proceed.

    Total by construction. An unexpected failure inside a port is already a typed planner
    refusal; anything else escaping would be a bug in this module, and it still lands as a
    refusal rather than as an exception at a caller that is about to close a live pane.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner import (  # noqa: E501
        EvidencePlanRefused,
        PlanningContext,
    )
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
        LAUNCH_CAUSE_UPDATE_RELAUNCH,
    )

    try:
        import os
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        live_rows = (
            tuple(list_herdr_agent_rows(os.environ))
            if live_rows is None else tuple(live_rows)
        )
        context = PlanningContext(
            workspace_id=workspace_id,
            lane_id=lane_id,
            expected_update_cause=LAUNCH_CAUSE_UPDATE_RELAUNCH,
        )
        plan = build_evidence_planner(
            Path(home) if home is not None else None, live_rows=live_rows,
            allow_historical_absent=allow_historical_absent,
        ).plan(
            participants, context
        )
    except EvidencePlanRefused as refusal:
        return EvidencePlanning(refusal=refusal.reason)
    except Exception:  # noqa: BLE001 - never surface as an exception to an actuator
        return EvidencePlanning(refusal="evidence_planning_failed")
    return EvidencePlanning(participants=tuple(plan.participants))


__all__ = (
    "EvidencePlanning",
    "build_evidence_planner",
    "plan_participants_with_evidence",
)
