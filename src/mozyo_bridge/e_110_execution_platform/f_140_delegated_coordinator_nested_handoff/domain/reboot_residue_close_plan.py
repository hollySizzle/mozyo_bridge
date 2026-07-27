"""Lane-scoped shell-residue close planning (Redmine #14499 Required behavior 6).

After a host reboot the Claude / Codex TUI in a lane pane exits, leaving a foreground
``-zsh`` behind while the durable assigned-name row survives in ``herdr agent list``
(#13518 j#75329, the *shell residue*). Live audit #13490 j#89060 counted 15 such panes
across 8 lanes.

Closing them is the one destructive step the reboot convergence needs, and it is where a
sweeping fix would do real damage: the same inventory also holds the coordinator's own
default-lane pair, other projects' lanes, foreign (non-``mzb1``) occupants, and panes that
are *busy right now*. So this planner is deliberately the narrowest thing that can work —
a close target must satisfy **every** one of:

1. its raw ``name`` is **byte-exactly** one of the canonical assigned names this lane's own
   slots would be minted with (:func:`encode_assigned_name` over the lane's units). Not a
   prefix, not a decode that happens to land on the same tuple — the exact string;
2. it carries a live **locator** (there is something to close);
3. the shared #13518 classifier reads it :data:`SLOT_STALE` (no managed agent behind it);
4. its runtime status carries **no recognised activity**. This is deliberately stricter
   than (3): :func:`classify_named_slot` calls a row stale as soon as its detected-agent
   field is present-but-blank, *whatever* its status says, so a pane reporting ``working``
   or a permission prompt would qualify on (3) alone. Requiring
   :data:`RUNTIME_UNKNOWN` here means a pane that is doing anything observable is
   preserved, and the two signals are never collapsed.

Everything the lane does not own — a foreign occupant, another lane's slot, the default-lane
coordinator pair, another workspace — is not even considered, because step (1) is an
equality test against names this lane mints. Anything that IS the lane's but fails (2)-(4)
is recorded as preserved, with the reason, so the audit trail shows what was spared.

**Pair fence.** If any of the lane's own expected slots is backed by a live agent, the whole
plan collapses to zero targets. A lane with one live half is not in the residue shape, and
closing its partner mid-turn would break a working pair — the same posture as the #13569
pair-atomic substitution fence in :func:`...sublane_herdr_retire.plan_herdr_retire_close`.
A merely *absent* slot is not a live one, so the genuine partial shape (one residue slot,
one slot already gone) still converges.

Pure: no subprocess, no inventory read, no close. The actuation lives in
:mod:`...application.sublane_residue_close`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_UNKNOWN,
    map_agent_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    HerdrIdentityError,
    _agent_locator,
    _norm,
    encode_assigned_name,
    is_lane_workspace_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)

#: The lane's own slot, but a live agent is behind it — never closed by this rail.
PRESERVED_LIVE_AGENT = "live_agent"
#: The lane's own slot reporting a recognised runtime activity (working / prompt / idle /
#: turn-ended). Stricter than the liveness classifier on purpose.
PRESERVED_ACTIVE_STATUS = "active_status"
#: The lane's own slot with no locator: there is nothing to close, and an absent locator is
#: never treated as proof of anything.
PRESERVED_NO_LOCATOR = "no_locator"


@dataclass(frozen=True)
class ResidueCloseCandidate:
    """One of the lane's own slots and what was decided about it."""

    assigned_name: str
    locator: str
    close: bool
    preserved_reason: str = ""
    runtime_status: str = RUNTIME_UNKNOWN

    def as_payload(self) -> dict:
        return {
            "assigned_name": self.assigned_name,
            "locator": self.locator,
            "close": self.close,
            "preserved_reason": self.preserved_reason,
            "runtime_status": self.runtime_status,
        }


@dataclass(frozen=True)
class ResidueClosePlan:
    """Which of the lane's own assigned-name slots this rail will close.

    ``close_targets`` is ``(assigned_name, locator)`` pairs — the ONLY panes the actuation
    may touch. ``preserved`` records the lane's other slots and why each was spared;
    ``untouched_names`` records every other managed-scheme row seen in the inventory, which
    this rail never evaluates at all (other lanes, the coordinator's default-lane pair,
    other workspaces). Both exist for the audit trail.

    ``pair_fence_tripped`` means a live agent was found in the lane's own units, so the plan
    was collapsed to zero targets.
    """

    workspace_id: str
    lane_id: str
    expected_names: tuple[str, ...] = ()
    close_targets: tuple[tuple[str, str], ...] = ()
    preserved: tuple[ResidueCloseCandidate, ...] = ()
    untouched_names: tuple[str, ...] = ()
    pair_fence_tripped: bool = False

    @property
    def has_targets(self) -> bool:
        return bool(self.close_targets)

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "expected_names": list(self.expected_names),
            "close_targets": [
                {"assigned_name": n, "locator": loc} for n, loc in self.close_targets
            ],
            "preserved": [c.as_payload() for c in self.preserved],
            "untouched_names": list(self.untouched_names),
            "pair_fence_tripped": self.pair_fence_tripped,
        }


def _row_status(row: Mapping[str, object]) -> str:
    for key in ("agent_status", "status", "state"):
        if key in row:
            return map_agent_status(row.get(key))
    return RUNTIME_UNKNOWN


def expected_lane_slot_names(
    *,
    workspace_id: str,
    lane_id: str,
    legacy_workspace_id: str = "",
    managed_roles: Sequence[str],
) -> tuple[str, ...]:
    """The canonical assigned names this lane's own managed slots are minted with (pure).

    Two units, matching what every other lane-scoped rail targets: the shared-model
    ``(workspace_id, lane_id)`` unit, and — only when ``legacy_workspace_id`` is a
    well-formed pre-#13377 lane token — its ``(legacy token, default)`` compatibility twin.

    A **default** ``lane_id`` on a non-legacy workspace yields nothing: that unit is the
    project's coordinator pair, which this rail must never target. A role that cannot mint a
    within-cap name is dropped rather than approximated (an unmintable name can never
    equal an observed one anyway).
    """
    names: list[str] = []
    ws = _norm(workspace_id)
    lane = _norm(lane_id)
    legacy = _norm(legacy_workspace_id)
    if legacy and not is_lane_workspace_token(legacy):
        legacy = ""
    for role in managed_roles:
        if ws and lane and lane != DEFAULT_LANE:
            try:
                names.append(encode_assigned_name(ws, role, lane))
            except HerdrIdentityError:
                pass
        if legacy:
            try:
                names.append(encode_assigned_name(legacy, role, DEFAULT_LANE))
            except HerdrIdentityError:
                pass
    # Deterministic and de-duplicated; a duplicate expected name would double-count a slot.
    return tuple(sorted(set(names)))


def plan_residue_close(
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    lane_id: str,
    legacy_workspace_id: str = "",
    managed_roles: Sequence[str],
) -> ResidueClosePlan:
    """Decide which of the lane's own shell-residue slots to close (pure, fail-closed).

    See the module docstring for the four conditions a close target must satisfy and for
    the pair fence. Rows that are not exact-name matches for this lane are collected into
    ``untouched_names`` and never evaluated further. Empty inputs plan nothing.
    """
    expected = expected_lane_slot_names(
        workspace_id=workspace_id,
        lane_id=lane_id,
        legacy_workspace_id=legacy_workspace_id,
        managed_roles=managed_roles,
    )
    expected_set = frozenset(expected)
    targets: list[tuple[str, str]] = []
    preserved: list[ResidueCloseCandidate] = []
    untouched: list[str] = []
    live_found = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw = row.get(AGENT_KEY_NAME)
        name = raw if isinstance(raw, str) else ""
        if name not in expected_set or not name:
            # Not this lane's slot. Recorded for the audit trail, never evaluated.
            if name:
                untouched.append(name)
            continue
        locator = _agent_locator(row)
        status = _row_status(row)
        if not locator:
            preserved.append(
                ResidueCloseCandidate(
                    assigned_name=name,
                    locator="",
                    close=False,
                    preserved_reason=PRESERVED_NO_LOCATOR,
                    runtime_status=status,
                )
            )
            continue
        if classify_named_slot(row) != SLOT_STALE:
            live_found = True
            preserved.append(
                ResidueCloseCandidate(
                    assigned_name=name,
                    locator=locator,
                    close=False,
                    preserved_reason=PRESERVED_LIVE_AGENT,
                    runtime_status=status,
                )
            )
            continue
        if status != RUNTIME_UNKNOWN:
            # Classified stale (its detected-agent field is blank) yet still reporting a
            # recognised activity — busy, a permission prompt, idle, or a finished turn.
            # Something is observably there; preserve it.
            preserved.append(
                ResidueCloseCandidate(
                    assigned_name=name,
                    locator=locator,
                    close=False,
                    preserved_reason=PRESERVED_ACTIVE_STATUS,
                    runtime_status=status,
                )
            )
            continue
        targets.append((name, locator))
    if live_found:
        # The pair fence: one live half means this lane is not in the residue shape.
        preserved.extend(
            ResidueCloseCandidate(
                assigned_name=name,
                locator=locator,
                close=False,
                preserved_reason=PRESERVED_LIVE_AGENT,
            )
            for name, locator in targets
        )
        targets = []
    return ResidueClosePlan(
        workspace_id=_norm(workspace_id),
        lane_id=_norm(lane_id),
        expected_names=expected,
        close_targets=tuple(sorted(targets)),
        preserved=tuple(preserved),
        untouched_names=tuple(sorted(set(untouched))),
        pair_fence_tripped=live_found,
    )


__all__ = (
    "PRESERVED_ACTIVE_STATUS",
    "PRESERVED_LIVE_AGENT",
    "PRESERVED_NO_LOCATOR",
    "ResidueCloseCandidate",
    "ResidueClosePlan",
    "expected_lane_slot_names",
    "plan_residue_close",
)
