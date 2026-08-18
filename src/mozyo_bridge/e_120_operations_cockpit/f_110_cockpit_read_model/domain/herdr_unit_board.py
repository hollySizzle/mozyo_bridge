"""Public-safe read model for the Herdr coordinator Unit board.

The board is a presentation consumer.  It groups already-resolved managed Herdr
agent observations by the durable ``(workspace_id, lane_id)`` Unit identity and
renders only short, operator-facing labels.  Runtime pane locators are retained
inside the process solely so the adapter can refresh Herdr display metadata; they
are deliberately absent from the public payload.

No value in this module is workflow, routing, review, approval, or close
authority.  Missing or contradictory inputs stay visible as ``unknown`` /
``ambiguous`` instead of being completed from pane position or provider guesses.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from mozyo_bridge.core.state.lane_kind import is_lane_kind
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.public_safe_text import (
    MAX_PRESENTATION_TEXT,
    REDACTED_TEXT,
    normalized_untrusted_text,
    safe_text,
)


#: The reserved host id of the client's own Herdr server within the Unit key
#: space.  It lives here, beside :func:`_unit_public_id` which depends on it, so
#: the operator source schema can import the display projection without a cycle;
#: the schema re-exports the name for its own callers.
#:
#: Local Units keep the opaque key they had before multi-source observation
#: existed, because that key is written into Herdr's pane metadata.  A
#: host-qualified key is domain-separated from it, so no configured source can
#: mint a value that lands in the local key space.
LOCAL_HOST_ID = "local"

#: Widest terminal render this module will allocate for.
MAX_BOARD_WIDTH = 1000
_ISSUE_LANE_RE = re.compile(r"^issue_(\d+)(?:_(.*))?$")


SOURCE_LIVE = "live"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_RELOAD_REQUIRED = "reload_required"
#: A source whose observation is real but too old to act on.  Distinct from
#: ``unavailable``: the rows are still worth *showing*, they are just no longer
#: fresh enough to be action authority (Redmine #15138).
SOURCE_STALE = "stale"
SOURCE_STATES = frozenset(
    {SOURCE_LIVE, SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED, SOURCE_STALE}
)

#: A Unit whose ``(workspace_id, lane_id)`` also exists on another observed
#: source.  Visible so the operator is never asked to tell two identically
#: named lanes apart by memory; not a refusal, because the two Units are
#: genuinely distinct and stay distinguishable by ``host_id``.
DUPLICATE_SCOPE_NONE = "none"
DUPLICATE_SCOPE_CROSS_SOURCE = "cross_source"

#: Whether one Unit's grouped fields agree.  Only ``resolved`` unlocks an
#: action, so this stays a closed vocabulary rather than free display text.
IDENTITY_RESOLVED = "resolved"
IDENTITY_AMBIGUOUS = "ambiguous"
IDENTITY_STATES = frozenset({IDENTITY_RESOLVED, IDENTITY_AMBIGUOUS})

AUTHORITY_RESOLVED = "resolved"
AUTHORITY_MISSING = "missing"
AUTHORITY_INVALID = "invalid"
AUTHORITY_STATES = frozenset(
    {AUTHORITY_RESOLVED, AUTHORITY_MISSING, AUTHORITY_INVALID}
)

#: key while remote Units get a distinct one from the same function.
#: Domain separator for host-qualified Unit keys.  The local shape starts with
#: an 8-byte big-endian length whose first byte is ``0x00`` for any real
#: identity, so a stream beginning with these bytes can never be produced by the
#: local shape — which is what lets local Units keep their historical key while
#: remote Units get a distinct one from the same function.
_HOST_QUALIFIED_TAG = b"host"


def _unit_public_id(
    workspace_id: str, lane_id: str, host_id: str = LOCAL_HOST_ID
) -> str:
    """Return a bounded opaque key without truncating or disclosing identity input.

    The key is host-qualified so the same ``(workspace_id, lane_id)`` observed
    on two servers yields two different Units rather than colliding into one.
    The local server is the exception on purpose: its key is byte-identical to
    the pre-multi-source key, so an operator who never configures a remote
    source sees unchanged ``mozyo_unit`` metadata.
    """
    digest = hashlib.sha256()
    components: tuple[str, ...] = (workspace_id, lane_id)
    if host_id != LOCAL_HOST_ID:
        digest.update(_HOST_QUALIFIED_TAG)
        components = (host_id,) + components
    for component in components:
        encoded = component.encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"unit-{digest.hexdigest()[:32]}"


def lane_work_label(
    lane_id: object, issue_id: object = "", label: object = "", lane_kind: object = ""
) -> str:
    """Return a readable work label without pretending it is a ticket subject.

    ``lane_metadata`` is display metadata, not durable ticket truth.  The label is
    therefore rendered as a lane label.  When it includes the conventional issue
    prefix, the readable suffix is kept beside the id so the UI never shows a bare
    ticket number as if that explained the work.

    ``lane_kind`` (Redmine #15704) appends the lane's recorded delegation-geometry
    kind — ``[delegated_coordinator]`` etc. — so a coordinator lane is visually
    distinct on the board and in the pane title built from this label.  Only the
    closed three-token vocabulary renders; anything else degrades to the plain
    label.  Display decoration only, never workflow or routing authority.
    """
    base = _lane_work_label_base(lane_id, issue_id, label)
    kind = lane_kind.strip() if isinstance(lane_kind, str) else ""
    if kind and is_lane_kind(kind):
        return safe_text(f"{base} [{kind}]")
    return base


def _lane_work_label_base(lane_id: object, issue_id: object, label: object) -> str:
    lane = safe_text(lane_id, fallback="unknown-lane")
    if lane == "default":
        return "default lane"
    display = safe_text(label, fallback=lane)
    match = _ISSUE_LANE_RE.fullmatch(display)
    if match is None:
        match = _ISSUE_LANE_RE.fullmatch(lane)
    if match is not None:
        number, suffix = match.groups()
        words = safe_text((suffix or "").replace("_", " "), fallback="lane")
        return safe_text(f"#{number} {words}")
    issue = safe_text(issue_id, fallback="")
    if issue:
        return safe_text(f"#{issue} {display.replace('_', ' ')}")
    return safe_text(display.replace("_", " "))


@dataclass(frozen=True)
class AgentObservation:
    """One managed live agent plus its resolved display authority.

    ``pane_id`` is a transient action-time locator.  It is intentionally private
    to this value object and omitted by :meth:`AgentCell.as_payload`.
    """

    workspace_id: str
    lane_id: str
    provider: str
    pane_id: str
    runtime_state: str
    interactive_ready: bool
    project_label: str
    workflow_role: str
    responsibility: str
    work_label: str
    authority_state: str
    host_id: str = LOCAL_HOST_ID
    host_label: str = LOCAL_HOST_ID

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.lane_id or not self.provider:
            raise ValueError("managed Unit observation requires workspace, lane, and provider")
        if self.authority_state not in AUTHORITY_STATES:
            raise ValueError(f"unknown authority state: {self.authority_state!r}")
        if not self.host_id:
            raise ValueError("managed Unit observation requires a source host id")


@dataclass(frozen=True)
class AgentCell:
    provider: str
    runtime_state: str
    interactive_ready: bool
    pane_id: str

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": safe_text(self.provider),
            "runtime_state": safe_text(self.runtime_state),
            "interactive_ready": self.interactive_ready,
        }


@dataclass(frozen=True)
class UnitBoardRow:
    unit_id: str
    workspace_id: str
    lane_id: str
    project_label: str
    workflow_role: str
    responsibility: str
    work_label: str
    authority_state: str
    identity_state: str
    agents: tuple[AgentCell, ...]
    host_id: str = LOCAL_HOST_ID
    host_label: str = LOCAL_HOST_ID
    duplicate_scope: str = DUPLICATE_SCOPE_NONE
    #: The identity exactly as its source stated it, before any projection.
    #:
    #: ``workspace_id`` / ``lane_id`` above are *display* values: the projection
    #: that produces them normalizes Unicode form and collapses whitespace, so a
    #: padded or full-width identity comes out canonical.  Reading those for an
    #: action means synthesizing an identity the source never sent (review
    #: j#101928 finding_2).  Action authority reads these fields instead, and a
    #: row that carries none is not addressable.
    raw_workspace_id: str = ""
    raw_lane_id: str = ""

    @property
    def identity_key(self) -> tuple[str, str]:
        """The un-projected identity pair, for grouping and duplicate checks."""
        return (self.raw_workspace_id, self.raw_lane_id)

    def as_payload(self, *, host_qualified: bool = False) -> dict[str, object]:
        """Project one row.  Host identity appears only in a merged board.

        A client observing several servers needs every row to say where it
        lives.  A client observing only its own server does not, and adding the
        fields there would change a payload that existing consumers already
        read — the local-only surface is required to stay byte-compatible
        (Redmine #15138 review j#101787 f5).  So the merged projection asks for
        the host fields and the single-server projection does not.
        """
        payload: dict[str, object] = {
            "unit_id": safe_text(self.unit_id),
            "workspace_id": safe_text(self.workspace_id),
            "lane_id": safe_text(self.lane_id),
            "project_label": safe_text(self.project_label),
            "workflow_role": safe_text(self.workflow_role),
            "responsibility": safe_text(self.responsibility),
            "work_label": safe_text(self.work_label),
            "authority_state": safe_text(self.authority_state),
            "identity_state": safe_text(self.identity_state),
            "agents": [agent.as_payload() for agent in self.agents],
        }
        if host_qualified:
            payload["host_id"] = safe_text(self.host_id)
            payload["host_label"] = safe_text(self.host_label)
            payload["duplicate_scope"] = safe_text(self.duplicate_scope)
        return payload


@dataclass(frozen=True)
class UnitBoardSourceStatus:
    """How one observed Herdr server answered, and whether it may be acted on.

    A source that could not be reached stays in the board as its own visible
    row.  Dropping it would make an unreachable host indistinguishable from a
    host with no Units — the exact confusion the close conditions forbid.
    ``detail`` is a fixed diagnostic phrase, never a connection value or an
    exception body.
    """

    host_id: str
    host_label: str
    host_kind: str
    source_state: str
    observed_at: str
    unit_count: int = 0
    unmanaged_agents: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.source_state not in SOURCE_STATES:
            raise ValueError(f"unknown Unit board source state: {self.source_state!r}")
        if not self.host_id:
            raise ValueError("a Unit board source status requires a host id")

    @property
    def actionable(self) -> bool:
        """Only a live source is action authority.

        ``unavailable`` / ``reload_required`` / ``stale`` all mean the client
        cannot currently prove what is running there, so every action against
        this source fails closed rather than acting on a remembered view.
        """
        return self.source_state == SOURCE_LIVE

    def as_payload(self) -> dict[str, object]:
        return {
            "host_id": safe_text(self.host_id),
            "host_label": safe_text(self.host_label),
            "host_kind": safe_text(self.host_kind),
            "source_state": self.source_state,
            "observed_at": safe_text(self.observed_at, fallback=""),
            "unit_count": self.unit_count,
            "unmanaged_agents": self.unmanaged_agents,
            "actionable": self.actionable,
            "detail": safe_text(self.detail, fallback="") if self.detail else "",
        }


@dataclass(frozen=True)
class UnitBoardSnapshot:
    source_state: str
    observed_at: str
    units: tuple[UnitBoardRow, ...]
    unmanaged_agents: int = 0
    detail: str = ""
    sources: tuple[UnitBoardSourceStatus, ...] = ()

    def __post_init__(self) -> None:
        if self.source_state not in SOURCE_STATES:
            raise ValueError(f"unknown Unit board source state: {self.source_state!r}")

    @property
    def ok(self) -> bool:
        return self.source_state == SOURCE_LIVE

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_state": self.source_state,
            "observed_at": safe_text(self.observed_at, fallback=""),
            "unmanaged_agents": self.unmanaged_agents,
            "detail": safe_text(self.detail, fallback="") if self.detail else "",
            "units": [
                unit.as_payload(host_qualified=bool(self.sources))
                for unit in self.units
            ],
        }
        # The source envelope is what makes this a merged board, and it is the
        # same condition under which rows carry host identity: a single-server
        # payload keeps exactly the shape its existing consumers read.
        if self.sources:
            payload["sources"] = [source.as_payload() for source in self.sources]
        return payload


def _choose_unit_field(values: Iterable[str], *, fallback: str) -> tuple[str, bool]:
    distinct = {
        (normalized_untrusted_text(value) or fallback)
        if isinstance(value, str)
        else fallback
        for value in values
    }
    if not distinct:
        return fallback, False
    if len(distinct) == 1:
        return safe_text(next(iter(distinct)), fallback=fallback), False
    return "ambiguous", True


def build_unit_board(
    observations: Sequence[AgentObservation],
    *,
    observed_at: str,
    unmanaged_agents: int = 0,
) -> UnitBoardSnapshot:
    """Group managed observations into a deterministic, ambiguity-visible board."""
    grouped: dict[tuple[str, str, str], list[AgentObservation]] = {}
    for observation in observations:
        grouped.setdefault(
            (observation.host_id, observation.workspace_id, observation.lane_id), []
        ).append(observation)

    rows: list[UnitBoardRow] = []
    for (host_id, workspace_id, lane_id), members in grouped.items():
        project, project_ambiguous = _choose_unit_field(
            (member.project_label for member in members), fallback="unknown-project"
        )
        role, role_ambiguous = _choose_unit_field(
            (member.workflow_role for member in members), fallback="unknown"
        )
        responsibility, responsibility_ambiguous = _choose_unit_field(
            (member.responsibility for member in members), fallback="unknown"
        )
        work, work_ambiguous = _choose_unit_field(
            (member.work_label for member in members), fallback="unknown"
        )
        authority, authority_ambiguous = _choose_unit_field(
            (member.authority_state for member in members), fallback=AUTHORITY_MISSING
        )
        provider_counts: dict[str, int] = {}
        for member in members:
            provider_counts[member.provider] = provider_counts.get(member.provider, 0) + 1
        duplicate_provider = any(count > 1 for count in provider_counts.values())
        identity_state = (
            IDENTITY_AMBIGUOUS
            if any(
                (
                    project_ambiguous,
                    role_ambiguous,
                    responsibility_ambiguous,
                    work_ambiguous,
                    authority_ambiguous,
                    duplicate_provider,
                )
            )
            else IDENTITY_RESOLVED
        )
        cells = tuple(
            AgentCell(
                provider=safe_text(member.provider),
                runtime_state=safe_text(member.runtime_state),
                interactive_ready=bool(member.interactive_ready),
                pane_id=member.pane_id,
            )
            for member in sorted(members, key=lambda item: (item.provider, item.pane_id))
        )
        host_label, _ = _choose_unit_field(
            (member.host_label for member in members), fallback=host_id
        )
        rows.append(
            UnitBoardRow(
                unit_id=_unit_public_id(workspace_id, lane_id, host_id),
                workspace_id=safe_text(workspace_id),
                lane_id=safe_text(lane_id),
                raw_workspace_id=workspace_id,
                raw_lane_id=lane_id,
                project_label=project,
                workflow_role=role,
                responsibility=responsibility,
                work_label=work,
                authority_state=authority,
                identity_state=identity_state,
                agents=cells,
                host_id=host_id,
                host_label=host_label,
            )
        )

    rows.sort(key=lambda row: (row.project_label.casefold(), row.lane_id, row.unit_id))
    return UnitBoardSnapshot(
        source_state=SOURCE_LIVE,
        observed_at=observed_at,
        units=tuple(rows),
        unmanaged_agents=max(0, int(unmanaged_agents)),
    )


def unavailable_snapshot(
    source_state: str,
    *,
    observed_at: str,
    detail: str,
    sources: tuple[UnitBoardSourceStatus, ...] = (),
) -> UnitBoardSnapshot:
    if source_state not in {SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED, SOURCE_STALE}:
        raise ValueError("an unavailable snapshot needs an unavailable source state")
    return UnitBoardSnapshot(
        source_state=source_state,
        observed_at=observed_at,
        units=(),
        detail=safe_text(detail),
        sources=sources,
    )


def metadata_for_unit(unit: UnitBoardRow) -> tuple[Mapping[str, str], str]:
    """Render the bounded metadata patch shared by every pane in one Unit."""
    tokens = {
        "mozyo_project": safe_text(unit.project_label),
        "mozyo_role": safe_text(unit.workflow_role),
        "mozyo_responsibility": safe_text(unit.responsibility),
        "mozyo_work": safe_text(unit.work_label),
        "mozyo_unit": safe_text(unit.unit_id),
        "mozyo_identity": safe_text(unit.identity_state),
    }
    title = safe_text(
        f"{unit.project_label} · {unit.workflow_role} · {unit.work_label}"
    )
    return tokens, title


def _cell_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in value
    )


def _pad_display(value: object, width: int) -> str:
    clipped = clip_display(value, width)
    return clipped + (" " * max(0, width - _cell_width(clipped)))


def clip_display(value: object, width: int) -> str:
    """Clip text by terminal cells without cutting a control sequence or codepoint."""
    text = safe_text(value)
    if width <= 0:
        return ""
    if _cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    used = 0
    chars: list[str] = []
    for char in text:
        cells = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + cells > budget:
            break
        chars.append(char)
        used += cells
    return "".join(chars) + "…"


def format_board(snapshot: UnitBoardSnapshot, *, width: int = 120) -> str:
    """Render a compact terminal table.  JSON callers use ``as_payload`` instead."""
    usable = min(MAX_BOARD_WIDTH, max(1, int(width)))
    heading = (
        f"mozyo Unit board  source={snapshot.source_state}  "
        f"observed={snapshot.observed_at or 'unknown'}"
    )
    if not snapshot.ok:
        return "\n".join(
            (
                clip_display(heading, usable),
                clip_display(f"detail: {snapshot.detail}", usable),
            )
        )
    if not snapshot.units:
        return "\n".join(
            (clip_display(heading, usable), clip_display("no managed Units", usable))
        )

    if usable < 90:
        lines = [clip_display(heading, usable), "-" * usable]
        for unit in snapshot.units:
            agents = ",".join(
                f"{agent.provider}:{agent.runtime_state}" for agent in unit.agents
            ) or "none"
            lines.extend(
                (
                    clip_display(
                        f"{unit.project_label} · {unit.workflow_role}", usable
                    ),
                    clip_display(
                        f"  responsibility: {unit.responsibility}", usable
                    ),
                    clip_display(f"  work: {unit.work_label}", usable),
                    clip_display(f"  agents: {agents}", usable),
                )
            )
            if unit.identity_state != "resolved" or unit.authority_state != "resolved":
                lines.append(
                    clip_display(
                        f"  ! identity={unit.identity_state} "
                        f"authority={unit.authority_state}",
                        usable,
                    )
                )
        if snapshot.unmanaged_agents:
            lines.append(
                clip_display(
                    f"unmanaged agents omitted: {snapshot.unmanaged_agents}", usable
                )
            )
        return "\n".join(lines)

    project_w = min(24, max(16, usable // 6))
    role_w = min(22, max(14, usable // 7))
    responsibility_w = min(26, max(16, usable // 5))
    state_w = 18
    work_w = max(
        16,
        usable - project_w - role_w - responsibility_w - state_w - 12,
    )
    header = (
        f"{'PROJECT':<{project_w}}  {'ROLE':<{role_w}}  "
        f"{'RESPONSIBILITY':<{responsibility_w}}  "
        f"{'AGENTS':<{state_w}}  WORK"
    )
    lines = [clip_display(heading, usable), header, "-" * usable]
    for unit in snapshot.units:
        agents = ",".join(
            f"{agent.provider}:{agent.runtime_state}"
            for agent in unit.agents
        ) or "none"
        lines.append(
            f"{_pad_display(unit.project_label, project_w)}  "
            f"{_pad_display(unit.workflow_role, role_w)}  "
            f"{_pad_display(unit.responsibility, responsibility_w)}  "
            f"{_pad_display(agents, state_w)}  "
            f"{clip_display(unit.work_label, work_w)}"
        )
        if unit.identity_state != "resolved" or unit.authority_state != "resolved":
            lines.append(
                clip_display(
                    f"  ! identity={unit.identity_state} "
                    f"authority={unit.authority_state}",
                    usable,
                )
            )
    if snapshot.unmanaged_agents:
        lines.append(
            clip_display(
                f"unmanaged agents omitted: {snapshot.unmanaged_agents}", usable
            )
        )
    return "\n".join(lines)


__all__ = (
    "AUTHORITY_INVALID",
    "LOCAL_HOST_ID",
    "AUTHORITY_MISSING",
    "AUTHORITY_RESOLVED",
    "DUPLICATE_SCOPE_CROSS_SOURCE",
    "DUPLICATE_SCOPE_NONE",
    "IDENTITY_AMBIGUOUS",
    "IDENTITY_RESOLVED",
    "IDENTITY_STATES",
    "MAX_BOARD_WIDTH",
    "MAX_PRESENTATION_TEXT",
    "REDACTED_TEXT",
    "AgentCell",
    "AgentObservation",
    "SOURCE_LIVE",
    "SOURCE_RELOAD_REQUIRED",
    "SOURCE_STALE",
    "SOURCE_UNAVAILABLE",
    "UnitBoardRow",
    "UnitBoardSnapshot",
    "UnitBoardSourceStatus",
    "build_unit_board",
    "clip_display",
    "format_board",
    "lane_work_label",
    "metadata_for_unit",
    "safe_text",
    "unavailable_snapshot",
)
