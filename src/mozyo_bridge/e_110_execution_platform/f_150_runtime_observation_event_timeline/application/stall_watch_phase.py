"""Which units a stall-watch phase may observe, and when it may run (Redmine #15855).

Two decisions live here, both of which #15855 j#110121 tightened relative to the naive
reading of the acceptance conditions:

**Discovery is a join, not a scan.** j#110121-4: "target discovery は raw
``list_herdr_agent_rows`` 全件走査にしない". A live agent row is only a *candidate*; it
becomes a watched unit only after it survives four independent filters, each of which drops
it for a different reason:

1. **managed identity** — the row decodes to an ``mzb1`` scheme name. Delegated to the
   existing :func:`herdr_inventory`, which already drops foreign / unmanaged agents; this
   module does not re-derive that parse.
2. **this workspace** — a row belonging to another workspace is another supervisor's
   business, and its lease fences it there.
3. **declared scope** — the operator's :class:`StallWatchPolicy` admits the lane and role.
   An absent policy admits nothing, so a host that never configured this watches nothing.
4. **resolvable anchors** — a live generation *and* an authoritative active issue anchor
   both resolve. Either being unknown drops the unit.

Filter 4 is the one worth defending, because it is a deliberate blind spot. A lane whose
issue anchor cannot be resolved is a lane this watcher will never escalate about — and that
is the fail-closed direction j#110121-4 requires ("generation不明、durable issue anchor不明
は送信対象にしない"), because the alternative is guessing which issue a stall belongs to and
writing a coordinator-facing record onto the wrong one. The gap is not hidden: every
dropped candidate is counted by reason in :class:`WatchDiscovery`, so a status surface can
show "3 live units are outside this watcher's reach, and why" rather than reporting a quiet
cockpit.

**Cadence is the watcher's own watermark.** j#110121-2: the OS tick stays at
``DEFAULT_OS_TICK_INTERVAL_SECONDS`` (180s) so the callback supervisor's local cadence is
not degraded, and the ~5-minute stall-watch period comes from a *separate* watermark this
module gates on. Because the phase can only run when a tick runs, the realized period is
quantized to the tick and is never exactly the configured cadence — :func:`stall_watch_due`
therefore reports ``next_due_at`` as a threshold to cross rather than a promise, and the
status projection says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.stall_escalation import StallEscalationStore
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_policy import (  # noqa: E501
    WatchIdentity,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
    POLICY_ABSENT,
    StallWatchPolicy,
)

#: Resolves a lane's live terminal generation; ``""`` means unknown (fail-closed: drop).
GenerationResolver = Callable[[str], str]
#: Resolves a lane's authoritative active issue anchor; ``""`` means unknown (drop).
IssueResolver = Callable[[str], str]

#: Drop reasons. A candidate lands on exactly one, and every one is counted.
DROP_FOREIGN_WORKSPACE = "foreign_workspace"
DROP_OUT_OF_SCOPE = "outside_declared_scope"
DROP_NO_GENERATION = "live_generation_unresolved"
DROP_NO_ISSUE_ANCHOR = "issue_anchor_unresolved"
DROP_NO_LOCATOR = "no_live_locator"

DROP_REASONS: frozenset[str] = frozenset(
    {
        DROP_FOREIGN_WORKSPACE,
        DROP_OUT_OF_SCOPE,
        DROP_NO_GENERATION,
        DROP_NO_ISSUE_ANCHOR,
        DROP_NO_LOCATOR,
    }
)


@dataclass(frozen=True)
class WatchUnit:
    """One live unit this phase is allowed to observe."""

    identity: WatchIdentity
    issue: str
    provider_id: str = ""

    @property
    def locator(self) -> str:
        return self.identity.target


@dataclass(frozen=True)
class WatchDiscovery:
    """The join's result: what is watched, and what was dropped for which reason."""

    units: tuple[WatchUnit, ...] = ()
    dropped: Mapping[str, int] = field(default_factory=dict)
    candidates: int = 0

    @property
    def watched(self) -> int:
        return len(self.units)

    @property
    def out_of_reach(self) -> int:
        """Live managed units this watcher cannot escalate about, for any reason.

        Surfaced as one number because the operator question it answers — "is the watcher
        actually covering my cockpit?" — is not answerable from the watched count alone.
        """
        return sum(
            count
            for reason, count in self.dropped.items()
            if reason != DROP_FOREIGN_WORKSPACE
        )

    def telemetry(self) -> dict[str, object]:
        return {
            "candidates": self.candidates,
            "watched": self.watched,
            "out_of_reach": self.out_of_reach,
            "dropped": {k: v for k, v in sorted(self.dropped.items())},
        }


def _norm(value: object) -> str:
    return str(value or "").strip()


def discover_watch_units(
    rows: Sequence[Mapping[str, object]],
    *,
    workspace_id: str,
    policy: StallWatchPolicy,
    generation_for: Optional[GenerationResolver] = None,
    issue_for: Optional[IssueResolver] = None,
    provider_for: Optional[Callable[[str], str]] = None,
) -> WatchDiscovery:
    """Join a live inventory snapshot down to the units this phase may observe.

    ``rows`` is the **raw** herdr ``agent list`` snapshot; the managed-identity parse is
    delegated to :func:`herdr_inventory` rather than re-derived, so a row this repo would
    not route to is a row this watcher does not read either.

    A disabled policy returns an empty discovery without calling any resolver. That is not
    an optimization: it means a host with no ``stall_watch`` block performs no lane lookups
    and no generation reads at all, so "watches nothing" is true of its I/O and not only of
    its output.
    """
    ws = _norm(workspace_id)
    if not policy.enabled or not ws:
        return WatchDiscovery(candidates=0)

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (  # noqa: E501
        PANE_KEY_ID,
        PANE_KEY_LANE,
        PANE_KEY_ROLE,
        PANE_KEY_WORKSPACE,
        herdr_inventory,
    )

    try:
        normalized = herdr_inventory(list(rows))
    except Exception:  # noqa: BLE001 - an unreadable inventory watches nothing, fail-closed
        return WatchDiscovery(candidates=0)

    units: list[WatchUnit] = []
    dropped: dict[str, int] = {}

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for row in normalized:
        lane_id = _norm(row.get(PANE_KEY_LANE))
        role = _norm(row.get(PANE_KEY_ROLE))
        row_ws = _norm(row.get(PANE_KEY_WORKSPACE))
        locator = _norm(row.get(PANE_KEY_ID))

        if row_ws != ws:
            _drop(DROP_FOREIGN_WORKSPACE)
            continue
        if not policy.admits(lane_id=lane_id, role=role):
            _drop(DROP_OUT_OF_SCOPE)
            continue
        if not locator:
            # A decoded slot with no live locator: there is no screen to read. The routing
            # layer already refuses to "report success with a blank target"; reading is the
            # same boundary.
            _drop(DROP_NO_LOCATOR)
            continue

        generation = ""
        if generation_for is not None:
            try:
                generation = _norm(generation_for(lane_id))
            except Exception:  # noqa: BLE001 - unresolved is unresolved
                generation = ""
        if not generation:
            _drop(DROP_NO_GENERATION)
            continue

        issue = ""
        if issue_for is not None:
            try:
                issue = _norm(issue_for(lane_id))
            except Exception:  # noqa: BLE001
                issue = ""
        if not issue:
            _drop(DROP_NO_ISSUE_ANCHOR)
            continue

        provider_id = ""
        if provider_for is not None:
            try:
                provider_id = _norm(provider_for(lane_id))
            except Exception:  # noqa: BLE001 - an unprofiled unit is still observable
                provider_id = ""

        units.append(
            WatchUnit(
                identity=WatchIdentity(
                    workspace_id=ws,
                    lane_id=lane_id,
                    role=role,
                    generation=generation,
                    target=locator,
                ),
                issue=issue,
                provider_id=provider_id or role,
            )
        )

    return WatchDiscovery(
        units=tuple(units), dropped=dropped, candidates=len(normalized)
    )


# --------------------------------------------------------------------------------------
# Cadence
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CadenceVerdict:
    """Whether a stall-watch phase is due, and the instants a status surface reports.

    ``next_due_at`` is a **threshold**, not a schedule. The phase runs only when the host
    tick runs, so the realized period is quantized to the tick (180s portable default)
    against a cadence (300s portable default) that is not a multiple of it. A surface that
    printed ``next_due_at`` as "the next run" would be wrong by up to one tick every time.
    """

    due: bool
    reason: str
    last_pass_at: str = ""
    next_due_at: str = ""
    cadence_seconds: int = 0

    def telemetry(self) -> dict[str, object]:
        return {
            "due": self.due,
            "reason": self.reason,
            "last_pass_at": self.last_pass_at,
            "next_due_at": self.next_due_at,
            "cadence_seconds": self.cadence_seconds,
            "next_due_is_a_threshold_not_a_schedule": True,
        }


CADENCE_DISABLED = "policy_disabled"
CADENCE_NEVER_RAN = "never_ran"
CADENCE_DUE = "cadence_elapsed"
CADENCE_WAITING = "within_cadence"
CADENCE_UNREADABLE_WATERMARK = "watermark_unreadable"


def _parse(stamp: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stall_watch_due(
    *,
    policy: StallWatchPolicy,
    last_pass_at: str,
    now: datetime,
) -> CadenceVerdict:
    """Decide whether this tick should run a stall-watch phase.

    A never-run watcher is due immediately: the first tick after an operator configures the
    watcher should observe, not wait out a full cadence for a cockpit that may already be
    stuck. An **unparseable** watermark is also treated as due — the failure mode of
    refusing to run because a timestamp is malformed is a watcher that is silently off
    forever, which is strictly worse than one extra pass.
    """
    if not policy.enabled:
        return CadenceVerdict(
            due=False,
            reason=CADENCE_DISABLED,
            cadence_seconds=policy.cadence_seconds,
        )
    cadence = int(policy.cadence_seconds)
    if not last_pass_at:
        return CadenceVerdict(
            due=True, reason=CADENCE_NEVER_RAN, cadence_seconds=cadence
        )
    parsed = _parse(last_pass_at)
    if parsed is None:
        return CadenceVerdict(
            due=True,
            reason=CADENCE_UNREADABLE_WATERMARK,
            last_pass_at=last_pass_at,
            cadence_seconds=cadence,
        )
    next_due = parsed + timedelta(seconds=cadence)
    return CadenceVerdict(
        due=now >= next_due,
        reason=CADENCE_DUE if now >= next_due else CADENCE_WAITING,
        last_pass_at=last_pass_at,
        next_due_at=next_due.isoformat(timespec="seconds"),
        cadence_seconds=cadence,
    )


# --------------------------------------------------------------------------------------
# Status projection
# --------------------------------------------------------------------------------------


def stall_watch_status(
    *,
    workspace_id: str,
    store: StallEscalationStore,
    policy: StallWatchPolicy,
    now: datetime,
) -> dict[str, object]:
    """The operator readback: the effective policy, the cadence, and the pending backlog.

    j#110121-2 requires the configured surface AND the effective values to be provable from
    a readback rather than inferred, and j#110121-6 requires the pending backlog's age to be
    visible so a starved queue is legible instead of silent. Review j#110146 finding_1 added
    the third: the discovery COVERAGE (how many live units are outside this watcher's
    reach, and why). All three are projected here from stored state only — this function
    makes no decision, mutates nothing, and in particular never reads a pane.
    """
    try:
        last_pass_at = store.last_pass_at(workspace_id)
    except Exception:  # noqa: BLE001 - a status surface must not raise
        last_pass_at = ""
    verdict = stall_watch_due(policy=policy, last_pass_at=last_pass_at, now=now)

    try:
        unrecorded = store.unrecorded_pending(workspace_id)
        unwoken = store.unwoken_pending(workspace_id)
        quarantined = store.quarantined_pending(workspace_id)
    except Exception:  # noqa: BLE001
        unrecorded, unwoken, quarantined = (), (), ()

    oldest_age_seconds: Optional[int] = None
    if unrecorded:
        parsed = _parse(unrecorded[0].escalated_at)
        if parsed is not None:
            oldest_age_seconds = max(0, int((now - parsed).total_seconds()))

    try:
        discovery = store.last_discovery(workspace_id)
    except Exception:  # noqa: BLE001 - a status surface must not raise
        discovery = None

    payload: dict[str, object] = {
        "workspace_id": workspace_id,
        "policy": policy.telemetry(),
        "cadence": verdict.telemetry(),
        # The coverage question -- "what is this watcher NOT seeing" -- answered from the
        # last pass's persisted counts rather than by re-running discovery here, which would
        # make a read-only status command read panes (review j#110146 finding_1).
        # ``None`` means the leg has never run: distinct from "ran and watched nothing".
        "discovery": discovery,
        "pending": {
            # Unwritten == the durable record does not know about these stalls yet.
            "unrecorded": len(unrecorded),
            "anchorless": sum(1 for p in unrecorded if not p.issue),
            "recorded_but_unwoken": len(unwoken),
            # Rows that fired, are still open, and are held back from the writer because
            # they no longer satisfy the stored-row contract. A COUNT only: the offending
            # values are exactly what must not be rendered (review j#110192 finding_1).
            # Non-zero means a durable escalation row was altered after it was written.
            "quarantined": len(quarantined),
            "oldest_unrecorded_at": unrecorded[0].escalated_at if unrecorded else "",
            "oldest_unrecorded_age_seconds": oldest_age_seconds,
            "max_attempts": max((p.attempts for p in unrecorded), default=0),
            "last_reason": unrecorded[0].last_reason if unrecorded else "",
        },
    }
    if policy.reason == POLICY_ABSENT:
        payload["note"] = (
            "no stall_watch block is declared, so this watcher observes nothing. Scope is "
            "opt-in by design: it never widens itself to every agent on the host."
        )
    elif not policy.enabled:
        payload["note"] = policy.detail or "stall watching is disabled"
    return payload


__all__ = (
    "CADENCE_DISABLED",
    "CADENCE_DUE",
    "CADENCE_NEVER_RAN",
    "CADENCE_UNREADABLE_WATERMARK",
    "CADENCE_WAITING",
    "DROP_FOREIGN_WORKSPACE",
    "DROP_NO_GENERATION",
    "DROP_NO_ISSUE_ANCHOR",
    "DROP_NO_LOCATOR",
    "DROP_OUT_OF_SCOPE",
    "DROP_REASONS",
    "CadenceVerdict",
    "GenerationResolver",
    "IssueResolver",
    "WatchDiscovery",
    "WatchUnit",
    "discover_watch_units",
    "stall_watch_due",
    "stall_watch_status",
)
