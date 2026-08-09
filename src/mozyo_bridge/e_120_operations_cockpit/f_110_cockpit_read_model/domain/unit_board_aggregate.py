"""Merge several Herdr servers' public-safe Unit boards into one view (#15138).

Each observed server projects its *own* board: it resolves its own workspace
registry, workflow-role bindings, and lane metadata, and hands back the same
public-safe payload the local board already renders.  This module is the client
half — it re-validates that payload (a remote answer is untrusted input, even
when the remote is running the same code), tags every row with the source it
came from, and folds the per-source results into one snapshot.

Three properties are load-bearing here:

- **Identity never collapses across servers.** Two hosts routinely hold the same
  ``workspace_id`` and lane name — that is the normal state of a repository
  checked out locally and on a remote host.  Those are two Units, and the
  board says so.
- **A truncated display value never becomes an action key.** The remote board
  bounds its text for display; feeding that bounded text back as identity would
  silently act on a prefix.  A remote Unit's action key is therefore derived
  from the remote's own opaque Unit key, not from any rendered field.
- **An unreachable or stale source stays visible and unactionable.** Dropping a
  failed source would make it indistinguishable from a source with no Units,
  and keeping it actionable would act on a view the client can no longer prove.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_STATES,
    DUPLICATE_SCOPE_CROSS_SOURCE,
    DUPLICATE_SCOPE_NONE,
    IDENTITY_AMBIGUOUS,
    IDENTITY_RESOLVED,
    IDENTITY_STATES,
    SOURCE_LIVE,
    SOURCE_RELOAD_REQUIRED,
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
    AgentCell,
    UnitBoardRow,
    UnitBoardSnapshot,
    UnitBoardSourceStatus,
    _unit_public_id,
    clip_display,
    safe_text,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSource,
)


#: Bound on rows accepted from one source.  A remote answer is untrusted input;
#: an unbounded list would let one host dominate the board and the client's
#: memory.
MAX_SOURCE_UNITS = 200
MAX_SOURCE_AGENTS = 16

#: How long a source observation may be reused before it stops being action
#: authority.  Observing several hosts costs real round trips, so the board is
#: allowed to render a slightly older answer — but only render it.
DEFAULT_SOURCE_FRESHNESS_SECONDS = 30

#: Bounds for the *remote answer's own* observation timestamp, a separate
#: dimension from the client-side freshness above (Redmine #15138 review
#: j#101787 f4).  The client timing its own round trip proves when the answer
#: arrived, not when the far host observed what it reported; an answer that is
#: undated, unparsable, or self-dated far in the past is one whose age the
#: client cannot establish, and it must not be action authority.
#:
#: The bound here is deliberately looser than the client-side one and carries an
#: explicit skew allowance, because it compares two machines' clocks.  A tight
#: bound would make every mildly skewed host permanently unactionable, which is
#: fail-closed but useless; a loose one still rejects the case that matters — an
#: answer reporting an observation from another era.
MAX_REMOTE_PAYLOAD_AGE_SECONDS = 600
MAX_REMOTE_CLOCK_SKEW_SECONDS = 300

#: The canonical registry workspace identifier shape.  Checked explicitly before
#: a remote identity is used as an action input, so the client can never act on
#: a value that was reshaped for display.
_WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_DETAIL_UNREACHABLE = "source Herdr observation is unavailable"
_DETAIL_INVALID = "source returned an unreadable Unit board payload"
_DETAIL_STALE = "source observation is older than the action freshness bound"
_DETAIL_NESTED = "source returned a merged board instead of its own single server"
_DETAIL_PAYLOAD_STALE = "source reported an observation time it cannot be acted on from"

#: Domain separator for a remote Unit's client-side key.  The remote already
#: hashed its full ``(workspace_id, lane_id)``; re-hashing that opaque key with
#: the source id yields a stable client key without ever touching a display
#: value.
_REMOTE_UNIT_TAG = "remote-unit"


def remote_unit_public_id(host_id: str, remote_unit_id: str) -> str:
    """Return the client-side key for one Unit observed on a remote source."""
    return _unit_public_id(_REMOTE_UNIT_TAG, remote_unit_id, host_id)


@dataclass(frozen=True)
class SourceObservation:
    """One source's answer: its status, its rows, and its action key mapping.

    ``remote_unit_ids`` maps the client-side Unit key back to the key the remote
    board itself uses.  It is how an action re-addresses a Unit on its own host
    without the client ever reconstructing that host's identity.
    """

    status: UnitBoardSourceStatus
    rows: tuple[UnitBoardRow, ...] = ()
    remote_unit_ids: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.remote_unit_ids is None:
            object.__setattr__(self, "remote_unit_ids", {})


def _parsed_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def freshness_state(
    observed_at: object,
    now: datetime,
    *,
    max_age_seconds: int = DEFAULT_SOURCE_FRESHNESS_SECONDS,
) -> str:
    """Classify one *client-side* observation timestamp as ``live`` or ``stale``.

    This dimension times the client's own round trip, so both timestamps come
    from one clock and skew cannot arise.  A future timestamp is therefore not
    skew but a contradiction, and is stale.  (The remote-payload dimension is a
    different question and has its own rule — see
    :func:`remote_payload_freshness`.)

    An unparsable or absent timestamp is stale, not live: the client cannot
    prove the age of an answer it cannot date, and an undated answer is exactly
    the case where acting on it would be least justified.
    """
    stamp = _parsed_timestamp(observed_at)
    if stamp is None:
        return SOURCE_STALE
    if stamp.tzinfo is None or now.tzinfo is None:
        return SOURCE_STALE
    age = (now - stamp).total_seconds()
    if age < 0 or age > max_age_seconds:
        return SOURCE_STALE
    return SOURCE_LIVE


def remote_payload_freshness(observed_at: object, now: datetime) -> str:
    """Classify the *remote answer's own* observation time (Redmine #15138 f4).

    Separate from :func:`freshness_state`, which times the client's own round
    trip.  Both must hold for a source to be action authority: the client must
    know when the answer arrived *and* the answer must claim an observation the
    client can still justify acting on.

    **The future rule differs from the client-side one on purpose, and this is
    the single statement of it** (review j#101846 finding_6).  Here two machines'
    clocks are being compared, so a small forward offset is ordinary skew rather
    than a contradiction: a future timestamp within
    :data:`MAX_REMOTE_CLOCK_SKEW_SECONDS` is live, and beyond it is stale.
    Rejecting every future timestamp would make any host whose clock runs
    slightly ahead permanently unactionable — fail-closed, but useless.
    """
    stamp = _parsed_timestamp(observed_at)
    if stamp is None or stamp.tzinfo is None or now.tzinfo is None:
        return SOURCE_STALE
    age = (now - stamp).total_seconds()
    if age > MAX_REMOTE_PAYLOAD_AGE_SECONDS:
        return SOURCE_STALE
    if age < -MAX_REMOTE_CLOCK_SKEW_SECONDS:
        return SOURCE_STALE
    return SOURCE_LIVE


def source_status(
    source: UnitBoardSource,
    *,
    source_state: str,
    observed_at: str,
    unit_count: int = 0,
    unmanaged_agents: int = 0,
    detail: str = "",
) -> UnitBoardSourceStatus:
    return UnitBoardSourceStatus(
        host_id=source.host_id,
        host_label=source.label,
        host_kind=source.kind,
        source_state=source_state,
        observed_at=observed_at,
        unit_count=unit_count,
        unmanaged_agents=unmanaged_agents,
        detail=detail,
    )


def unavailable_source_observation(
    source: UnitBoardSource,
    *,
    observed_at: str,
    source_state: str = SOURCE_UNAVAILABLE,
    detail: str = _DETAIL_UNREACHABLE,
) -> SourceObservation:
    """A source that could not be read, kept in the board as a visible row."""
    return SourceObservation(
        status=source_status(
            source,
            source_state=source_state,
            observed_at=observed_at,
            detail=detail,
        )
    )


def local_source_observation(
    snapshot: UnitBoardSnapshot, *, source: UnitBoardSource
) -> SourceObservation:
    """Adopt the in-process local board as one source of the merged view.

    The local rows already carry the local ``host_id`` default, so they are
    reused unchanged; only their labels are bound to the operator's local
    source label.
    """
    if not snapshot.ok:
        return SourceObservation(
            status=source_status(
                source,
                source_state=snapshot.source_state,
                observed_at=snapshot.observed_at,
                detail=snapshot.detail or _DETAIL_UNREACHABLE,
            )
        )
    rows = tuple(
        UnitBoardRow(
            unit_id=row.unit_id,
            workspace_id=row.workspace_id,
            lane_id=row.lane_id,
            project_label=row.project_label,
            workflow_role=row.workflow_role,
            responsibility=row.responsibility,
            work_label=row.work_label,
            authority_state=row.authority_state,
            identity_state=row.identity_state,
            agents=row.agents,
            host_id=source.host_id,
            host_label=source.label,
        )
        for row in snapshot.units
    )
    return SourceObservation(
        status=source_status(
            source,
            source_state=SOURCE_LIVE,
            observed_at=snapshot.observed_at,
            unit_count=len(rows),
            unmanaged_agents=snapshot.unmanaged_agents,
        ),
        rows=rows,
    )


def _recomputed_identity_state(declared: object, cells: Sequence[AgentCell]) -> str:
    """Conjoin the remote's declared identity state with the client's own check.

    A remote answer is untrusted input, and that has to mean its *invariants*
    too — not only its text (review j#101846 finding_2).  The local producer
    marks a Unit ``ambiguous`` when one provider appears twice in it; a remote
    row asserting ``resolved`` while carrying the same contradiction would
    otherwise walk straight past the client's action gate on its own say-so.

    This half handles the **well-formed but contradictory** case: the answer
    parses and its fields have the right types, yet it describes something the
    local producer could not have produced.  Those rows stay visible and become
    unactionable.  A *shape* violation is the other half and is raised instead,
    degrading the whole source to ``reload_required`` — the split is stated in
    ``multi-source-unit-board.md`` (review j#101891 finding_2).
    """
    if not cells:
        # A Unit is a grouping of at least one observed agent; the local
        # producer cannot emit an empty one.
        return IDENTITY_AMBIGUOUS
    providers = [cell.provider for cell in cells]
    if len(providers) != len(set(providers)):
        return IDENTITY_AMBIGUOUS
    return declared if declared in IDENTITY_STATES else IDENTITY_AMBIGUOUS


def _agent_cells(raw: object) -> tuple[AgentCell, ...]:
    """Decode the agent list, or raise so the whole source reads as unreadable.

    Everything here is a *shape* question — a field of the wrong type or an
    identity field that is empty.  A remote answer that fails one of these is
    not a board the client can interpret at all, so it degrades the source
    rather than one row (review j#101891 finding_2).
    """
    if not isinstance(raw, list) or len(raw) > MAX_SOURCE_AGENTS:
        raise ValueError("unreadable agent list")
    cells: list[AgentCell] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("unreadable agent row")
        provider = item.get("provider")
        runtime_state = item.get("runtime_state")
        ready = item.get("interactive_ready", False)
        if not isinstance(provider, str) or not isinstance(runtime_state, str):
            raise ValueError("unreadable agent row")
        # An exact bool, not a truthy value: JSON carries ``"false"`` as a
        # string, and ``bool("false")`` is True — a readiness display that says
        # the opposite of what the source reported.
        if not isinstance(ready, bool):
            raise ValueError("unreadable agent readiness")
        # The provider names which half of the pair this is; an empty one leaves
        # the Unit's own membership undefined.
        if not safe_text(provider, fallback=""):
            raise ValueError("unreadable agent provider")
        cells.append(
            AgentCell(
                provider=safe_text(provider),
                runtime_state=safe_text(runtime_state),
                interactive_ready=ready,
                # A remote pane locator is meaningless in this process and is
                # deliberately absent from the remote payload; nothing on the
                # client may address a pane on another server.
                pane_id="",
            )
        )
    return tuple(cells)


def parse_remote_board_payload(
    payload: object,
    *,
    source: UnitBoardSource,
    observed_at: str,
    now: datetime,
) -> SourceObservation:
    """Validate one remote ``unit-board show --json`` answer into rows.

    Every field is re-projected through the same public-safe projection the
    local board uses.  Running identical code on the far end is not a reason to
    skip it: the answer crosses a process and a host boundary, and a source that
    starts returning something else must fail closed here rather than paint that
    something else onto the board.
    """
    try:
        if not isinstance(payload, Mapping):
            raise ValueError("unreadable payload")
        if "sources" in payload:
            # A source must answer for its own server only.  A merged answer
            # means the far host aggregated *its* sources, so its rows describe
            # servers this client never asked about — and tagging them with the
            # outer source id would attribute another host's Units to this one
            # (Redmine #15138 review j#101787 f2).  Mutually registered hosts
            # would also fan out recursively.
            return SourceObservation(
                status=source_status(
                    source,
                    source_state=SOURCE_RELOAD_REQUIRED,
                    observed_at=observed_at,
                    detail=_DETAIL_NESTED,
                )
            )
        state = payload.get("source_state")
        if state != SOURCE_LIVE:
            return SourceObservation(
                status=source_status(
                    source,
                    source_state=(
                        state
                        if state in {SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED}
                        else SOURCE_RELOAD_REQUIRED
                    ),
                    observed_at=observed_at,
                    detail=_DETAIL_UNREACHABLE,
                )
            )
        raw_units = payload.get("units")
        if not isinstance(raw_units, list) or len(raw_units) > MAX_SOURCE_UNITS:
            raise ValueError("unreadable unit list")
        rows: list[UnitBoardRow] = []
        remote_unit_ids: dict[str, str] = {}
        for raw in raw_units:
            if not isinstance(raw, Mapping):
                raise ValueError("unreadable unit row")
            remote_unit_id = raw.get("unit_id")
            workspace_id = raw.get("workspace_id")
            lane_id = raw.get("lane_id")
            authority_state = raw.get("authority_state")
            if (
                not isinstance(remote_unit_id, str)
                or not remote_unit_id
                # Identity fields must be present, not merely of the right type:
                # an empty workspace or lane is a row with no identity to join
                # on (review j#101891 finding_2).
                or not isinstance(workspace_id, str)
                or not workspace_id.strip()
                or not isinstance(lane_id, str)
                or not lane_id.strip()
                or authority_state not in AUTHORITY_STATES
            ):
                raise ValueError("unreadable unit row")
            cells = _agent_cells(raw.get("agents"))
            unit_id = remote_unit_public_id(source.host_id, remote_unit_id)
            if unit_id in remote_unit_ids:
                # Two rows claiming one key cannot both be addressed; the whole
                # answer is ambiguous rather than half-usable.
                raise ValueError("duplicate unit key")
            remote_unit_ids[unit_id] = remote_unit_id
            rows.append(
                UnitBoardRow(
                    unit_id=unit_id,
                    workspace_id=safe_text(workspace_id),
                    lane_id=safe_text(lane_id),
                    project_label=safe_text(
                        raw.get("project_label"), fallback="unknown-project"
                    ),
                    workflow_role=safe_text(raw.get("workflow_role")),
                    responsibility=safe_text(raw.get("responsibility")),
                    work_label=safe_text(raw.get("work_label")),
                    authority_state=authority_state,
                    # Closed vocabulary, and an unrecognized value degrades to
                    # ``ambiguous`` rather than being displayed verbatim: only
                    # ``resolved`` unlocks an action, so an unknown state must
                    # never read as one and never paint a remote string here.
                    # The declared value is additionally conjoined with the
                    # client's own recomputation of the invariant.
                    identity_state=_recomputed_identity_state(
                        raw.get("identity_state"), cells
                    ),
                    agents=cells,
                    host_id=source.host_id,
                    host_label=source.label,
                )
            )
        unmanaged = payload.get("unmanaged_agents")
        unmanaged_count = (
            unmanaged if isinstance(unmanaged, int) and not isinstance(unmanaged, bool) else 0
        )
        # Required, not optional: an optional clock means this boundary parser
        # can be called in a mode where an undated answer is live, and a
        # fail-open default at a trust boundary is the defect itself
        # (review j#101846 finding_6).
        payload_state = remote_payload_freshness(payload.get("observed_at"), now)
    except (ValueError, TypeError):
        return SourceObservation(
            status=source_status(
                source,
                source_state=SOURCE_RELOAD_REQUIRED,
                observed_at=observed_at,
                detail=_DETAIL_INVALID,
            )
        )
    return SourceObservation(
        status=source_status(
            source,
            source_state=payload_state,
            observed_at=observed_at,
            unit_count=len(rows),
            unmanaged_agents=max(0, unmanaged_count),
            detail="" if payload_state == SOURCE_LIVE else _DETAIL_PAYLOAD_STALE,
        ),
        rows=tuple(rows),
        remote_unit_ids=remote_unit_ids,
    )


def mark_stale(
    observation: SourceObservation,
    now: datetime,
    *,
    max_age_seconds: int = DEFAULT_SOURCE_FRESHNESS_SECONDS,
) -> SourceObservation:
    """Downgrade a live-looking observation whose timestamp is no longer fresh."""
    status = observation.status
    if status.source_state != SOURCE_LIVE:
        return observation
    if freshness_state(status.observed_at, now, max_age_seconds=max_age_seconds) == SOURCE_LIVE:
        return observation
    return SourceObservation(
        status=UnitBoardSourceStatus(
            host_id=status.host_id,
            host_label=status.host_label,
            host_kind=status.host_kind,
            source_state=SOURCE_STALE,
            observed_at=status.observed_at,
            unit_count=status.unit_count,
            unmanaged_agents=status.unmanaged_agents,
            detail=_DETAIL_STALE,
        ),
        rows=observation.rows,
        remote_unit_ids=observation.remote_unit_ids,
    )


def _aggregate_state(statuses: Sequence[UnitBoardSourceStatus]) -> str:
    if any(status.source_state == SOURCE_LIVE for status in statuses):
        return SOURCE_LIVE
    if any(status.source_state == SOURCE_RELOAD_REQUIRED for status in statuses):
        return SOURCE_RELOAD_REQUIRED
    if any(status.source_state == SOURCE_STALE for status in statuses):
        return SOURCE_STALE
    return SOURCE_UNAVAILABLE


def aggregate_sources(
    observations: Sequence[SourceObservation], *, observed_at: str
) -> UnitBoardSnapshot:
    """Fold per-source observations into one snapshot with duplicates marked.

    The merged board is ``live`` when at least one source answered, so a single
    unreachable host degrades that host rather than the whole view.  Which
    hosts degraded stays readable in ``sources``, and each degraded source is
    unactionable on its own.
    """
    statuses = tuple(observation.status for observation in observations)
    rows = [row for observation in observations for row in observation.rows]

    identity_hosts: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        identity_hosts.setdefault((row.workspace_id, row.lane_id), set()).add(
            row.host_id
        )
    marked = tuple(
        UnitBoardRow(
            unit_id=row.unit_id,
            workspace_id=row.workspace_id,
            lane_id=row.lane_id,
            project_label=row.project_label,
            workflow_role=row.workflow_role,
            responsibility=row.responsibility,
            work_label=row.work_label,
            authority_state=row.authority_state,
            identity_state=row.identity_state,
            agents=row.agents,
            host_id=row.host_id,
            host_label=row.host_label,
            duplicate_scope=(
                DUPLICATE_SCOPE_CROSS_SOURCE
                if len(identity_hosts[(row.workspace_id, row.lane_id)]) > 1
                else DUPLICATE_SCOPE_NONE
            ),
        )
        for row in rows
    )
    ordered = tuple(
        sorted(
            marked,
            key=lambda row: (
                row.host_id,
                row.project_label.casefold(),
                row.lane_id,
                row.unit_id,
            ),
        )
    )
    degraded = tuple(
        status for status in statuses if status.source_state != SOURCE_LIVE
    )
    detail = (
        ""
        if not degraded
        else "degraded sources: "
        + ", ".join(f"{status.host_label}={status.source_state}" for status in degraded)
    )
    return UnitBoardSnapshot(
        source_state=_aggregate_state(statuses),
        observed_at=observed_at,
        units=ordered,
        unmanaged_agents=sum(status.unmanaged_agents for status in statuses),
        detail=safe_text(detail, fallback="") if detail else "",
        sources=statuses,
    )


def actionable_workspace_id(row: UnitBoardRow) -> Optional[str]:
    """Return the row's workspace id only when it is a whole registry identity.

    The board bounds text for display.  A workspace id that does not match the
    canonical registry shape may therefore be a *display* value rather than the
    identity itself, and handing it to an action would address a prefix.  In
    that case there is no action input at all.
    """
    workspace_id = row.workspace_id
    if isinstance(workspace_id, str) and _WORKSPACE_ID_RE.fullmatch(workspace_id):
        return workspace_id
    return None


def format_multi_source_board(snapshot: UnitBoardSnapshot, *, width: int = 120) -> str:
    """Render the merged board with an explicit source column.

    Used only when more than the local server is configured, so the local-only
    rendering keeps its existing shape.
    """
    usable = max(1, int(width))
    lines = [
        clip_display(
            f"mozyo Unit board  sources={len(snapshot.sources)}  "
            f"state={snapshot.source_state}  "
            f"observed={snapshot.observed_at or 'unknown'}",
            usable,
        )
    ]
    for status in snapshot.sources:
        marker = " " if status.actionable else "!"
        summary = (
            f"{marker} source {status.host_label} [{status.host_kind}] "
            f"{status.source_state} units={status.unit_count}"
        )
        if status.detail:
            summary = f"{summary} — {status.detail}"
        lines.append(clip_display(summary, usable))
    lines.append("-" * usable)
    if not snapshot.units:
        lines.append(clip_display("no managed Units", usable))
        return "\n".join(lines)
    for unit in snapshot.units:
        agents = (
            ",".join(f"{agent.provider}:{agent.runtime_state}" for agent in unit.agents)
            or "none"
        )
        lines.append(
            clip_display(
                f"[{unit.host_label}] {unit.project_label} · {unit.workflow_role}",
                usable,
            )
        )
        lines.append(clip_display(f"  work: {unit.work_label}", usable))
        lines.append(clip_display(f"  agents: {agents}", usable))
        flags = []
        if unit.identity_state != IDENTITY_RESOLVED:
            flags.append(f"identity={unit.identity_state}")
        if unit.authority_state != "resolved":
            flags.append(f"authority={unit.authority_state}")
        if unit.duplicate_scope != DUPLICATE_SCOPE_NONE:
            flags.append(f"duplicate={unit.duplicate_scope}")
        if flags:
            lines.append(clip_display("  ! " + " ".join(flags), usable))
    if snapshot.unmanaged_agents:
        lines.append(
            clip_display(
                f"unmanaged agents omitted: {snapshot.unmanaged_agents}", usable
            )
        )
    return "\n".join(lines)


__all__ = (
    "DEFAULT_SOURCE_FRESHNESS_SECONDS",
    "MAX_REMOTE_CLOCK_SKEW_SECONDS",
    "MAX_REMOTE_PAYLOAD_AGE_SECONDS",
    "remote_payload_freshness",
    "MAX_SOURCE_AGENTS",
    "MAX_SOURCE_UNITS",
    "SourceObservation",
    "actionable_workspace_id",
    "aggregate_sources",
    "format_multi_source_board",
    "freshness_state",
    "local_source_observation",
    "mark_stale",
    "parse_remote_board_payload",
    "remote_unit_public_id",
    "source_status",
    "unavailable_source_observation",
)
