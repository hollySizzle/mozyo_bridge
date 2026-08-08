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

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


SOURCE_LIVE = "live"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_RELOAD_REQUIRED = "reload_required"
SOURCE_STATES = frozenset({SOURCE_LIVE, SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED})

AUTHORITY_RESOLVED = "resolved"
AUTHORITY_MISSING = "missing"
AUTHORITY_INVALID = "invalid"
AUTHORITY_STATES = frozenset(
    {AUTHORITY_RESOLVED, AUTHORITY_MISSING, AUTHORITY_INVALID}
)

MAX_PRESENTATION_TEXT = 80
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
_ISSUE_LANE_RE = re.compile(r"^issue_(\d+)(?:_(.*))?$")


def safe_text(value: object, *, fallback: str = "unknown") -> str:
    """Normalize one inert display value and enforce Herdr's 80-char cap."""
    if not isinstance(value, str):
        return fallback
    normalized = _SPACE_RE.sub(" ", _CONTROL_RE.sub("", value)).strip()
    if not normalized:
        return fallback
    return normalized[:MAX_PRESENTATION_TEXT]


def lane_work_label(lane_id: object, issue_id: object = "", label: object = "") -> str:
    """Return a readable work label without pretending it is a ticket subject.

    ``lane_metadata`` is display metadata, not durable ticket truth.  The label is
    therefore rendered as a lane label.  When it includes the conventional issue
    prefix, the readable suffix is kept beside the id so the UI never shows a bare
    ticket number as if that explained the work.
    """
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

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.lane_id or not self.provider:
            raise ValueError("managed Unit observation requires workspace, lane, and provider")
        if self.authority_state not in AUTHORITY_STATES:
            raise ValueError(f"unknown authority state: {self.authority_state!r}")


@dataclass(frozen=True)
class AgentCell:
    provider: str
    runtime_state: str
    interactive_ready: bool
    pane_id: str

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "runtime_state": self.runtime_state,
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

    def as_payload(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "project_label": self.project_label,
            "workflow_role": self.workflow_role,
            "responsibility": self.responsibility,
            "work_label": self.work_label,
            "authority_state": self.authority_state,
            "identity_state": self.identity_state,
            "agents": [agent.as_payload() for agent in self.agents],
        }


@dataclass(frozen=True)
class UnitBoardSnapshot:
    source_state: str
    observed_at: str
    units: tuple[UnitBoardRow, ...]
    unmanaged_agents: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.source_state not in SOURCE_STATES:
            raise ValueError(f"unknown Unit board source state: {self.source_state!r}")

    @property
    def ok(self) -> bool:
        return self.source_state == SOURCE_LIVE

    def as_payload(self) -> dict[str, object]:
        return {
            "source_state": self.source_state,
            "observed_at": self.observed_at,
            "unmanaged_agents": self.unmanaged_agents,
            "detail": safe_text(self.detail, fallback="") if self.detail else "",
            "units": [unit.as_payload() for unit in self.units],
        }


def _choose_unit_field(values: Iterable[str], *, fallback: str) -> tuple[str, bool]:
    distinct = {safe_text(value, fallback=fallback) for value in values}
    if not distinct:
        return fallback, False
    if len(distinct) == 1:
        return next(iter(distinct)), False
    return "ambiguous", True


def build_unit_board(
    observations: Sequence[AgentObservation],
    *,
    observed_at: str,
    unmanaged_agents: int = 0,
) -> UnitBoardSnapshot:
    """Group managed observations into a deterministic, ambiguity-visible board."""
    grouped: dict[tuple[str, str], list[AgentObservation]] = {}
    for observation in observations:
        grouped.setdefault(
            (observation.workspace_id, observation.lane_id), []
        ).append(observation)

    rows: list[UnitBoardRow] = []
    for (workspace_id, lane_id), members in grouped.items():
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
            "ambiguous"
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
            else "resolved"
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
        rows.append(
            UnitBoardRow(
                unit_id=safe_text(f"{workspace_id}:{lane_id}"),
                workspace_id=safe_text(workspace_id),
                lane_id=safe_text(lane_id),
                project_label=project,
                workflow_role=role,
                responsibility=responsibility,
                work_label=work,
                authority_state=authority,
                identity_state=identity_state,
                agents=cells,
            )
        )

    rows.sort(key=lambda row: (row.project_label.casefold(), row.lane_id, row.unit_id))
    return UnitBoardSnapshot(
        source_state=SOURCE_LIVE,
        observed_at=observed_at,
        units=tuple(rows),
        unmanaged_agents=max(0, int(unmanaged_agents)),
    )


def unavailable_snapshot(source_state: str, *, observed_at: str, detail: str) -> UnitBoardSnapshot:
    if source_state not in {SOURCE_UNAVAILABLE, SOURCE_RELOAD_REQUIRED}:
        raise ValueError("an unavailable snapshot needs an unavailable source state")
    return UnitBoardSnapshot(
        source_state=source_state,
        observed_at=observed_at,
        units=(),
        detail=safe_text(detail),
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
    usable = max(60, int(width))
    heading = (
        f"mozyo Unit board  source={snapshot.source_state}  "
        f"observed={snapshot.observed_at or 'unknown'}"
    )
    if not snapshot.ok:
        return f"{clip_display(heading, usable)}\n  {clip_display(snapshot.detail, usable - 2)}"
    if not snapshot.units:
        return f"{clip_display(heading, usable)}\n  no managed Units"

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
                f"  ! identity={unit.identity_state} authority={unit.authority_state} "
            )
    if snapshot.unmanaged_agents:
        lines.append(f"unmanaged agents omitted: {snapshot.unmanaged_agents}")
    return "\n".join(lines)


__all__ = (
    "AUTHORITY_INVALID",
    "AUTHORITY_MISSING",
    "AUTHORITY_RESOLVED",
    "AgentObservation",
    "SOURCE_LIVE",
    "SOURCE_RELOAD_REQUIRED",
    "SOURCE_UNAVAILABLE",
    "UnitBoardRow",
    "UnitBoardSnapshot",
    "build_unit_board",
    "clip_display",
    "format_board",
    "lane_work_label",
    "metadata_for_unit",
    "safe_text",
    "unavailable_snapshot",
)
