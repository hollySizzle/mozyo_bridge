"""The shared live-zero liveness measurement for the terminal retire rails (#14499).

Redmine #14242 established what a metadata-only terminalizer must prove before it may write
``retired`` on a lane with **no** ``process_release`` witness to corroborate it: the live
inventory read is then the sole liveness authority, so every ambiguity it can carry has to
be refused explicitly rather than aggregated away. Its four fences, in order:

1. **duplicate canonical slot** — checked FIRST, because a duplicate carrying locators
   would otherwise report as an ordinary ``live_pair_present`` and name the wrong problem.
   A herdr assigned name is unique by construction, so a duplicate means the inventory
   itself is unsound and no measurement taken from it may license a terminal write;
2. **an expected managed slot is live** — the lane is not gone;
3. **a locator-less expected row** whose liveness the shared contract does not *positively*
   call dead — "cannot be resolved", never "absent";
4. **a foreign / unexpected occupant** in a targeted unit — ``expected_live_slots``
   aggregates only the managed roles, so a unit holding solely an unexpected provider
   measures zero live; terminalizing then records the lane permanently gone while a real
   process keeps running in its unit (the j#80115 F1 fail-open).

Redmine #14499 adds a fifth terminal rail (the ACTIVE **unbound** live-zero retire), which
must prove exactly the same thing. Rather than restate ~100 lines of safety logic in a
second place — where it would drift from the reviewed original on the next change — the
measurement lives here once and both rails call it. The refusal vocabulary is unchanged and
each rail keeps re-exporting its own constants, so no caller's reason strings move.

Pure with respect to the durable store: this reads the LIVE inventory and the repo's
provider binding only. It writes nothing and knows nothing about lifecycle rows. Its caller
is responsible for holding the home's attestation-store lock EXCLUSIVE across both this
measurement and the CAS that follows it (the #13882 boundary-3 launch exclusion) — this
function cannot enforce that and does not claim to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

#: Refusal reasons. These are the exact strings #14242 established; the rails re-export them
#: under their own names so no existing payload vocabulary changes.
LIVE_ZERO_LIVE_PAIR_PRESENT = "live_pair_present"
LIVE_ZERO_FOREIGN_INVENTORY_PRESENT = "foreign_inventory_present"
LIVE_ZERO_DUPLICATE_INVENTORY = "duplicate_inventory"
LIVE_ZERO_EXPECTED_IDENTITY_UNRESOLVED = "expected_identity_unresolved"


@dataclass(frozen=True)
class LiveZeroMeasurement:
    """The outcome of one action-time live-zero measurement.

    ``proven`` is the sole success signal: every expected managed slot of the lane's
    targeted unit(s) is positively absent, the inventory is unambiguous, and nothing foreign
    occupies those units. On refusal ``reason`` is one of the four fences above and
    ``detail`` explains it; ``expected_live`` / ``foreign_names`` carry the measurement the
    caller surfaces in its verdict either way.
    """

    proven: bool
    reason: str = ""
    detail: str = ""
    expected_live: tuple[str, ...] = ()
    foreign_names: tuple[str, ...] = ()


def measure_live_zero(
    repo_root: Path,
    *,
    workspace_id: str,
    lane_label: str,
    legacy_workspace_id: str = "",
    rows: Optional[Sequence[Mapping[str, object]]] = None,
    env: Optional[Mapping[str, str]] = None,
):
    """Prove (or refuse) that a lane's managed pair is positively gone.

    Returns a :class:`LiveZeroMeasurement`, or raises the caller-handled inventory /
    provider errors so each rail keeps its own typed blocked reason for them:

    - ``HerdrSessionStartError`` — the live inventory could not be read;
    - ``WorkflowProviderUnresolved`` — the repo's role/provider binding is unresolved.

    A non-launchable provider in the binding is reported as a refusal here rather than an
    exception, because it is a measurement outcome ("this unit's pair cannot be measured"),
    not a read failure.

    ``rows`` may be injected for tests; by default the live ``herdr agent list`` is read.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
        REASON_PROVIDER_NOT_LAUNCHABLE,
        expected_live_slots,
        expected_slot_rows,
        plan_herdr_retire_close,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
        SLOT_STALE,
        classify_named_slot,
    )
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E501
        BUILTIN_AGENT_PROVIDER_SNAPSHOT,
    )

    inventory = list_herdr_agent_rows(env if env is not None else os.environ) if rows is None else rows
    managed_roles = (
        resolve_gateway_provider(str(repo_root)),
        resolve_worker_provider(str(repo_root)),
    )
    if not all(BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable(p) for p in managed_roles):
        return LiveZeroMeasurement(
            proven=False,
            reason=REASON_PROVIDER_NOT_LAUNCHABLE,
            detail=(
                "the binding assigns a provider that is not mechanically launchable; the "
                "lane unit's managed pair cannot be measured"
            ),
        )
    plan = plan_herdr_retire_close(
        inventory,
        workspace_id=workspace_id,
        lane_id=lane_label,
        legacy_workspace_id=legacy_workspace_id,
        managed_roles=managed_roles,
    )
    candidates = expected_slot_rows(inventory, plan, managed_roles=managed_roles)
    foreign = tuple(plan.foreign_names)
    # 1. Duplicates FIRST — keyed on the decoded canonical slot (NOT on role), so a shared
    #    unit and its legacy compatibility twin stay two legitimate slots.
    seen_slots: dict[tuple[str, str, str], int] = {}
    for found in candidates:
        seen_slots[found.slot_key] = seen_slots.get(found.slot_key, 0) + 1
    duplicates = sorted(
        f"{role}@{ws}/{lane or '<default>'}"
        for (ws, lane, role), count in seen_slots.items()
        if count > 1
    )
    if duplicates:
        return LiveZeroMeasurement(
            proven=False,
            reason=LIVE_ZERO_DUPLICATE_INVENTORY,
            detail=(
                "the live inventory carries more than one row for the same canonical managed "
                f"slot ({', '.join(duplicates)}); a herdr assigned name is unique by "
                "construction, so the inventory is ambiguous and no measurement taken from it "
                "can license a terminal write"
            ),
            foreign_names=foreign,
        )
    # 2. An expected managed slot is live.
    live = expected_live_slots(inventory, plan, managed_roles=managed_roles)
    if live:
        return LiveZeroMeasurement(
            proven=False,
            reason=LIVE_ZERO_LIVE_PAIR_PRESENT,
            detail=(
                "the lane's expected managed slots are still live "
                f"({', '.join(live)}); a lane with a live pair is not a terminalizer's "
                "target — drain it through the ordinary guarded close"
            ),
            expected_live=live,
            foreign_names=foreign,
        )
    # 3. A locator-less expected row is "cannot resolve", never "absent", unless the shared
    #    liveness contract positively calls it dead.
    unresolved = sorted(
        {
            found.role
            for found in candidates
            if not found.locator and classify_named_slot(found.row) != SLOT_STALE
        }
    )
    if unresolved:
        return LiveZeroMeasurement(
            proven=False,
            reason=LIVE_ZERO_EXPECTED_IDENTITY_UNRESOLVED,
            detail=(
                f"an expected managed slot ({', '.join(unresolved)}) has a row in the targeted "
                "units but no locator, and the liveness contract does not positively call it "
                "dead; that is absence of proof of liveness, not proof of absence"
            ),
            foreign_names=foreign,
        )
    # 4. A foreign / unexpected occupant in a targeted unit.
    if foreign:
        return LiveZeroMeasurement(
            proven=False,
            reason=LIVE_ZERO_FOREIGN_INVENTORY_PRESENT,
            detail=(
                "a foreign / unexpected provider occupies one of the lane's targeted units "
                f"({', '.join(foreign)}); terminalizing would record the lane permanently "
                "gone while a real process is still running in it"
            ),
            foreign_names=foreign,
        )
    return LiveZeroMeasurement(proven=True)


__all__ = (
    "LIVE_ZERO_DUPLICATE_INVENTORY",
    "LIVE_ZERO_EXPECTED_IDENTITY_UNRESOLVED",
    "LIVE_ZERO_FOREIGN_INVENTORY_PRESENT",
    "LIVE_ZERO_LIVE_PAIR_PRESENT",
    "LiveZeroMeasurement",
    "measure_live_zero",
)
