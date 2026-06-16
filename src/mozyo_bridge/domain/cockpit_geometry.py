"""Read-only cockpit geometry diagnosis (Redmine #12131).

`mozyo cockpit doctor-geometry` diagnoses live tmux *display geometry* drift in
the cockpit window. The cockpit's intended shape is one full-height column per
Unit (a ``workspace_id`` + ``lane_id`` pair), each column holding that Unit's
Codex pane stacked over its Claude pane and sharing the same x-range. Manual
``move-pane`` / ``join-pane`` / resize, a crashed-and-recreated pane, or an
external tmux integration can drift that geometry so a column splits row-wise,
two Units share one vertical band, a pane loses its role markers, or one column
starves while another balloons.

This module is **pure and read-only by construction**: :func:`diagnose_cockpit_geometry`
turns a snapshot of cockpit-window panes (the shape
:func:`mozyo_bridge.application.commands._read_cockpit_geometry` returns) into an
inspectable :class:`GeometryDiagnosis`. It plans no tmux command and mutates
nothing — repair / rebalance / move are deliberately out of scope (US #12130
splits those into later issues).

Critically, the observed split tree / pane coordinates are treated as **observed
state, not identity authority** (`pane-centric-cockpit-semantics.md`): identity /
routing stay anchored on the pane user options (`@mozyo_workspace_id` /
`@mozyo_agent_role` / `@mozyo_lane_id`) and the registry. A geometry finding is an
operator-facing attention/recovery signal; it never re-decides which Unit a pane
belongs to, and it never blocks handoff (handoff safety is a live target
preflight concern, not a geometry concern).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.domain.cockpit_layout import (
    ROLE_CLAUDE,
    ROLE_CODEX,
    normalize_lane,
)

# Two panes count as sharing a vertical column when their x-ranges overlap by at
# least this fraction of the narrower pane's width. A healthy column's Codex/Claude
# pair shares the same left/width (full overlap → same column); two adjacent
# full-height columns touch at a 1-cell border (≈0 overlap → distinct columns).
DEFAULT_COLUMN_OVERLAP_RATIO = 0.5

# A column whose width is below this fraction of the median column width is
# reported as a width-imbalance / extreme-narrow column (advisory notice only).
DEFAULT_NARROW_RATIO = 0.5

SEVERITY_WARNING = "warning"  # structural drift — flips `ok` to False
SEVERITY_NOTICE = "notice"  # cosmetic imbalance — advisory, does not flip `ok`

# Finding codes (stable identifiers a JSON consumer can switch on).
FINDING_MISSING_CODEX = "missing_codex"
FINDING_MISSING_CLAUDE = "missing_claude"
FINDING_ROLE_LESS_PANE = "role_less_pane"
FINDING_UNIT_COLUMN_SPLIT = "unit_column_split"
FINDING_MIXED_UNIT_COLUMN = "mixed_unit_column"
FINDING_NARROW_PANE = "narrow_pane"

_AGENT_ROLES = (ROLE_CODEX, ROLE_CLAUDE)


@dataclass(frozen=True)
class PaneGeometry:
    """One cockpit-window pane projected for geometry diagnosis (#12131).

    Identity (``workspace_id`` / ``role`` / ``lane_id``) is read from the tmux
    user options, never the title; geometry (``pane_left`` / ``pane_top`` /
    ``pane_width`` / ``pane_height``) is the observed tmux rectangle. A pane is
    :pyattr:`identified` only when it carries BOTH a ``workspace_id`` and a real
    agent ``role`` — a manually-created or half-bound pane (the #12130 ``%1106``
    case) is role-less and cannot be assigned to a Unit.
    """

    pane_id: str
    workspace_id: str
    role: str
    lane_id: str
    pane_left: int
    pane_top: int
    pane_width: int
    pane_height: int

    @property
    def right(self) -> int:
        return self.pane_left + self.pane_width

    @property
    def identified(self) -> bool:
        return bool(self.workspace_id) and self.role in _AGENT_ROLES

    @property
    def unit_key(self) -> Optional[tuple[str, str]]:
        """The ``(workspace_id, lane_id)`` Unit this pane belongs to, or ``None``."""
        if not self.identified:
            return None
        return (self.workspace_id, normalize_lane(self.lane_id))

    def as_dict(self) -> dict:
        return {
            "pane_id": self.pane_id,
            "workspace_id": self.workspace_id,
            "role": self.role,
            "lane_id": normalize_lane(self.lane_id),
            "pane_left": self.pane_left,
            "pane_top": self.pane_top,
            "pane_width": self.pane_width,
            "pane_height": self.pane_height,
            "identified": self.identified,
        }


@dataclass(frozen=True)
class GeometryColumn:
    """A clustered vertical band of panes sharing an x-range (observed only)."""

    index: int
    left: int
    right: int
    pane_ids: tuple[str, ...]
    units: tuple[tuple[str, str], ...]  # distinct identified Units in this column

    @property
    def width(self) -> int:
        return self.right - self.left

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "left": self.left,
            "right": self.right,
            "width": self.width,
            "pane_ids": list(self.pane_ids),
            "units": [list(u) for u in self.units],
        }


@dataclass(frozen=True)
class GeometryUnit:
    """One ``workspace_id`` + ``lane_id`` Unit projected from identified panes."""

    workspace_id: str
    lane_id: str
    codex_panes: tuple[str, ...]
    claude_panes: tuple[str, ...]
    columns: tuple[int, ...]  # column indices the Unit's panes land in

    @property
    def has_codex(self) -> bool:
        return bool(self.codex_panes)

    @property
    def has_claude(self) -> bool:
        return bool(self.claude_panes)

    def as_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "codex_panes": list(self.codex_panes),
            "claude_panes": list(self.claude_panes),
            "columns": list(self.columns),
        }


@dataclass(frozen=True)
class GeometryFinding:
    """One read-only drift finding (observed geometry, not identity authority)."""

    code: str
    severity: str
    message: str
    pane_ids: tuple[str, ...] = ()
    workspace_id: Optional[str] = None
    lane_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "pane_ids": list(self.pane_ids),
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
        }


@dataclass(frozen=True)
class GeometryDiagnosis:
    """The read-only result of cockpit geometry diagnosis (#12131).

    ``cockpit_present`` is ``False`` when the cockpit window does not exist —
    a benign no-op (nothing to diagnose), not a drift. :pyattr:`ok` is ``True``
    when no :data:`SEVERITY_WARNING` finding is present; :data:`SEVERITY_NOTICE`
    findings (width imbalance) are advisory and do not flip it.
    """

    session: str
    cockpit_present: bool
    panes: tuple[PaneGeometry, ...]
    columns: tuple[GeometryColumn, ...]
    units: tuple[GeometryUnit, ...]
    findings: tuple[GeometryFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity == SEVERITY_WARNING for f in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == SEVERITY_WARNING)

    @property
    def notice_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == SEVERITY_NOTICE)

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "cockpit_present": self.cockpit_present,
            "ok": self.ok,
            "pane_count": len(self.panes),
            "column_count": len(self.columns),
            "unit_count": len(self.units),
            "panes": [p.as_dict() for p in self.panes],
            "columns": [c.as_dict() for c in self.columns],
            "units": [u.as_dict() for u in self.units],
            "findings": [f.as_dict() for f in self.findings],
            "summary": {
                "warning": self.warning_count,
                "notice": self.notice_count,
            },
        }


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _to_pane(row: Mapping[str, object]) -> PaneGeometry:
    return PaneGeometry(
        pane_id=str(row.get("pane_id") or ""),
        workspace_id=str(row.get("workspace_id") or ""),
        role=str(row.get("role") or ""),
        lane_id=str(row.get("lane_id") or ""),
        pane_left=_as_int(row.get("pane_left")),
        pane_top=_as_int(row.get("pane_top")),
        pane_width=_as_int(row.get("pane_width")),
        pane_height=_as_int(row.get("pane_height")),
    )


def _shares_column(a: PaneGeometry, b: PaneGeometry, overlap_ratio: float) -> bool:
    """True when ``a`` and ``b`` overlap on x by ≥ ``overlap_ratio`` of the narrower."""
    lo = max(a.pane_left, b.pane_left)
    hi = min(a.right, b.right)
    overlap = max(0, hi - lo)
    narrower = min(a.pane_width, b.pane_width)
    if narrower <= 0:
        return False
    return overlap / narrower >= overlap_ratio


def _cluster_columns(
    panes: Sequence[PaneGeometry], overlap_ratio: float
) -> list[list[PaneGeometry]]:
    """Group panes into vertical columns by x-range overlap (union-find, pure).

    Ordered left-to-right by each column's leftmost edge; panes within a column
    are ordered top-to-bottom (then by pane id) so the projection is stable.
    """
    n = len(panes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            if _shares_column(panes[i], panes[j], overlap_ratio):
                union(i, j)

    groups: dict[int, list[PaneGeometry]] = {}
    for i, pane in enumerate(panes):
        groups.setdefault(find(i), []).append(pane)

    clusters = [
        sorted(group, key=lambda p: (p.pane_top, p.pane_id))
        for group in groups.values()
    ]
    clusters.sort(key=lambda group: (min(p.pane_left for p in group), group[0].pane_id))
    return clusters


def _median(values: Sequence[int]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    mid = count // 2
    if count % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def diagnose_cockpit_geometry(
    *,
    session: str,
    panes: Optional[Sequence[Mapping[str, object]]],
    column_overlap_ratio: float = DEFAULT_COLUMN_OVERLAP_RATIO,
    narrow_ratio: float = DEFAULT_NARROW_RATIO,
) -> GeometryDiagnosis:
    """Diagnose cockpit-window geometry drift from a pane snapshot (#12131, pure).

    ``panes`` is the cockpit window's pane list (each a mapping with ``pane_id`` /
    ``workspace_id`` / ``role`` / ``lane_id`` / ``pane_left`` / ``pane_top`` /
    ``pane_width`` / ``pane_height``), or ``None`` when the cockpit window does not
    exist (a benign no-op, :pyattr:`GeometryDiagnosis.cockpit_present` ``False``).

    Detections (all observed-geometry advisories, never identity authority):

    - :data:`FINDING_MISSING_CODEX` / :data:`FINDING_MISSING_CLAUDE` — a Unit with
      one agent but not the other.
    - :data:`FINDING_ROLE_LESS_PANE` — a cockpit pane missing its
      ``@mozyo_workspace_id`` and/or ``@mozyo_agent_role`` markers (the #12130
      ``%1106`` manual-recovery case).
    - :data:`FINDING_UNIT_COLUMN_SPLIT` — a Unit whose Codex and Claude panes do
      not share one vertical column.
    - :data:`FINDING_MIXED_UNIT_COLUMN` — one vertical column carrying panes from
      more than one Unit.
    - :data:`FINDING_NARROW_PANE` — a column far narrower than the median column
      (advisory notice; does not flip ``ok``).
    """
    if panes is None:
        return GeometryDiagnosis(session, False, (), (), (), ())

    pane_list = [_to_pane(row) for row in panes if str(row.get("pane_id") or "")]
    clusters = _cluster_columns(pane_list, column_overlap_ratio)

    # pane_id -> column index, for unit/finding cross-reference.
    column_of: dict[str, int] = {}
    columns: list[GeometryColumn] = []
    for index, group in enumerate(clusters):
        unit_keys: list[tuple[str, str]] = []
        for pane in group:
            column_of[pane.pane_id] = index
            key = pane.unit_key
            if key is not None and key not in unit_keys:
                unit_keys.append(key)
        lefts = [p.pane_left for p in group]
        rights = [p.right for p in group]
        columns.append(
            GeometryColumn(
                index=index,
                left=min(lefts),
                right=max(rights),
                pane_ids=tuple(p.pane_id for p in group),
                units=tuple(unit_keys),
            )
        )

    # --- Units: group identified panes by (workspace_id, lane_id). -------------
    unit_codex: dict[tuple[str, str], list[str]] = {}
    unit_claude: dict[tuple[str, str], list[str]] = {}
    unit_columns: dict[tuple[str, str], set[int]] = {}
    unit_order: list[tuple[str, str]] = []
    role_less: list[PaneGeometry] = []
    for pane in pane_list:
        key = pane.unit_key
        if key is None:
            role_less.append(pane)
            continue
        if key not in unit_codex:
            unit_codex[key] = []
            unit_claude[key] = []
            unit_columns[key] = set()
            unit_order.append(key)
        (unit_codex if pane.role == ROLE_CODEX else unit_claude)[key].append(
            pane.pane_id
        )
        unit_columns[key].add(column_of[pane.pane_id])

    units = tuple(
        GeometryUnit(
            workspace_id=key[0],
            lane_id=key[1],
            codex_panes=tuple(unit_codex[key]),
            claude_panes=tuple(unit_claude[key]),
            columns=tuple(sorted(unit_columns[key])),
        )
        for key in unit_order
    )

    findings: list[GeometryFinding] = []

    # --- Missing-role and split-column findings, per Unit. ---------------------
    for unit in units:
        where = f"workspace {unit.workspace_id!r} lane {unit.lane_id!r}"
        if not unit.has_claude:
            findings.append(
                GeometryFinding(
                    FINDING_MISSING_CLAUDE,
                    SEVERITY_WARNING,
                    f"Unit {where} has a codex pane "
                    f"({', '.join(unit.codex_panes)}) but no claude pane in the "
                    f"cockpit (observed geometry).",
                    pane_ids=unit.codex_panes,
                    workspace_id=unit.workspace_id,
                    lane_id=unit.lane_id,
                )
            )
        if not unit.has_codex:
            findings.append(
                GeometryFinding(
                    FINDING_MISSING_CODEX,
                    SEVERITY_WARNING,
                    f"Unit {where} has a claude pane "
                    f"({', '.join(unit.claude_panes)}) but no codex pane in the "
                    f"cockpit (observed geometry).",
                    pane_ids=unit.claude_panes,
                    workspace_id=unit.workspace_id,
                    lane_id=unit.lane_id,
                )
            )
        if unit.has_codex and unit.has_claude and len(unit.columns) > 1:
            findings.append(
                GeometryFinding(
                    FINDING_UNIT_COLUMN_SPLIT,
                    SEVERITY_WARNING,
                    f"Unit {where} codex/claude panes do not share one vertical "
                    f"column (observed columns {list(unit.columns)}); they should "
                    f"stack in a single x-range column.",
                    pane_ids=tuple(unit.codex_panes) + tuple(unit.claude_panes),
                    workspace_id=unit.workspace_id,
                    lane_id=unit.lane_id,
                )
            )

    # --- Role-less panes: cannot be assigned to a Unit. ------------------------
    for pane in role_less:
        missing = []
        if not pane.workspace_id:
            missing.append("@mozyo_workspace_id")
        if pane.role not in _AGENT_ROLES:
            missing.append("@mozyo_agent_role")
        findings.append(
            GeometryFinding(
                FINDING_ROLE_LESS_PANE,
                SEVERITY_WARNING,
                f"pane {pane.pane_id} in the cockpit is missing "
                f"{' / '.join(missing)} (workspace_id={pane.workspace_id!r}, "
                f"role={pane.role!r}); it cannot be grouped into a Unit. This is "
                f"observed geometry only, not identity authority.",
                pane_ids=(pane.pane_id,),
            )
        )

    # --- Mixed-Unit columns: one vertical band carrying >1 Unit. ---------------
    for column in columns:
        if len(column.units) > 1:
            named = ", ".join(f"{ws}/{lane}" for ws, lane in column.units)
            findings.append(
                GeometryFinding(
                    FINDING_MIXED_UNIT_COLUMN,
                    SEVERITY_WARNING,
                    f"vertical column {column.index} (x {column.left}..{column.right}) "
                    f"carries panes from {len(column.units)} Units ({named}); each "
                    f"column should hold a single Unit (observed geometry).",
                    pane_ids=column.pane_ids,
                )
            )

    # --- Width imbalance / extreme-narrow columns (advisory notice). -----------
    if len(columns) >= 2:
        widths = [c.width for c in columns]
        median = _median(widths)
        if median > 0:
            threshold = median * narrow_ratio
            for column in columns:
                if column.width < threshold:
                    findings.append(
                        GeometryFinding(
                            FINDING_NARROW_PANE,
                            SEVERITY_NOTICE,
                            f"column {column.index} width {column.width} is far "
                            f"below the median column width {median:g} (observed "
                            f"width imbalance; rebalance is out of scope here).",
                            pane_ids=column.pane_ids,
                        )
                    )

    return GeometryDiagnosis(
        session=session,
        cockpit_present=True,
        panes=tuple(pane_list),
        columns=tuple(columns),
        units=units,
        findings=tuple(findings),
    )


def format_geometry_text(diagnosis: GeometryDiagnosis) -> str:
    """Human-readable rendering of a :class:`GeometryDiagnosis` (pure)."""
    lines: list[str] = []
    if not diagnosis.cockpit_present:
        lines.append(
            f"cockpit geometry: no cockpit window for session "
            f"{diagnosis.session!r} — nothing to diagnose."
        )
        return "\n".join(lines)

    lines.append(f"cockpit geometry: {diagnosis.session}")
    lines.append(
        f"panes={len(diagnosis.panes)} columns={len(diagnosis.columns)} "
        f"units={len(diagnosis.units)}"
    )
    lines.append(
        f"findings: {diagnosis.warning_count} warning, "
        f"{diagnosis.notice_count} notice"
    )
    lines.append("note: observed geometry only — identity/routing stay on pane options.")

    if not diagnosis.findings:
        lines.append("OK: no cockpit geometry drift detected.")
        return "\n".join(lines)

    for finding in diagnosis.findings:
        panes = f" [{', '.join(finding.pane_ids)}]" if finding.pane_ids else ""
        lines.append(f"[{finding.severity}] {finding.code}: {finding.message}{panes}")
    return "\n".join(lines)
