"""Read-only cockpit membership projection (Redmine #12341).

`mozyo cockpit list` / `mozyo cockpit status --repo <repo>` answer one operator
question fast: is a repo / workspace *loaded in the shared cockpit*, are its
Codex / Claude panes present, and is the display geometry healthy? Today an
operator has to cross-read `session list`, `agents list`, `cockpit
doctor-geometry`, and `cockpit append --dry-run`, and `status --repo` only
shouts "agent window missing" without ever saying the workspace is in fact a
cockpit column — exactly the #12339 mis-read this US closes.

This module is **pure and read-only by construction**, mirroring
:mod:`mozyo_bridge.domain.cockpit_geometry`. The application layer reads live
tmux (the managed cockpit windows + their geometry) and the workspace registry,
then hands the *resolved facts* to :func:`project_membership_report` /
:func:`absent_membership`. The projection plans no tmux, touches no filesystem,
and mutates nothing.

Critically, cockpit membership is a **display / liveness projection, NOT Redmine
workflow / approval / close truth** (`runtime-observability-boundary.md`). A
workspace being a cockpit column says only that tmux currently shows it; it never
decides ticket state, review approval, or close authority — those stay on the
Redmine issue + journal. Identity / routing stay anchored on the pane user
options + the registry (`pane-centric-cockpit-semantics.md`); a geometry finding
here is an operator-facing attention signal, never identity authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.domain.cockpit_geometry import (
    FINDING_MIXED_UNIT_COLUMN,
    FINDING_ROLE_LESS_PANE,
    SEVERITY_WARNING,
    GeometryDiagnosis,
)
from mozyo_bridge.domain.cockpit_layout import COCKPIT_WINDOW, normalize_lane

# The single sentence every membership view repeats: this is a projection of live
# display state, not the durable work record. Kept here so text + JSON consumers
# share one wording and tests can assert on it.
MEMBERSHIP_NOTE = (
    "cockpit membership is a display/liveness projection of live tmux panes — "
    "not Redmine workflow / approval / close truth. Use the Redmine issue + "
    "journal for work state."
)

# Geometry-status codes (stable identifiers a JSON consumer can switch on).
GEOM_OK = "ok"  # in cockpit, both peers present, no warning-level geometry drift
GEOM_WARNING = "warning"  # in cockpit but a warning-level geometry drift / missing peer
GEOM_UNKNOWN = "unknown"  # in a non-cockpit (group) window not covered by the diagnosis
GEOM_ABSENT = "absent"  # the workspace is not loaded in the cockpit at all

# Membership-warning codes (advisory; separated from the headline membership fact
# so `cockpit list` / `status` keep "is it loaded" distinct from "what to tidy").
WARN_MISSING_PEER = "missing_peer"
WARN_NOT_REGISTERED = "workspace_not_registered"
WARN_ANCHOR_ABSENT = "workspace_anchor_absent"
WARN_NOT_LOADED = "not_loaded"
WARN_ROLE_LESS_PANE = "role_less_pane"
WARN_MIXED_UNIT_COLUMN = "mixed_unit_column"


@dataclass(frozen=True)
class MembershipWarning:
    """One advisory note about a workspace's cockpit presence (display only)."""

    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class RegistryFacts:
    """Registry / anchor facts the application layer resolves for a workspace.

    Kept as a small value object so the projection stays pure: the app layer does
    the SQLite + anchor reads and passes the *result* here. ``label`` falls back
    to the workspace id when the registry has no record; ``repo_root`` is empty
    when it cannot be resolved (an unregistered cockpit pane carries only its
    ``@mozyo_workspace_id``, never its path).
    """

    label: str
    repo_root: str
    registry_present: bool
    anchor_present: bool

    @classmethod
    def unresolved(cls, workspace_id: str) -> "RegistryFacts":
        return cls(
            label=workspace_id,
            repo_root="",
            registry_present=False,
            anchor_present=False,
        )


@dataclass(frozen=True)
class MembershipObservation:
    """One observed cockpit Unit (a ``workspace_id`` + ``lane_id``) and its panes.

    Projected by the application layer from the live managed cockpit windows
    (`_read_managed_cockpit_windows`). ``codex_pane`` / ``claude_pane`` are the
    Unit's pane ids (empty string when that role is absent in the cockpit).
    ``window`` is the tmux window display name; ``window_id`` the stable ``@N``.
    """

    workspace_id: str
    lane_id: str
    lane_label: str
    codex_pane: str
    claude_pane: str
    window: str
    window_id: str


@dataclass(frozen=True)
class WorkspaceMembership:
    """One workspace's cockpit membership projection (display / liveness only)."""

    workspace_id: str
    label: str
    repo_root: str
    lane_id: str
    lane_label: str
    session: str
    window: str
    window_id: str
    codex_pane: str
    claude_pane: str
    member: bool
    geometry_status: str
    registry_present: bool
    anchor_present: bool
    warnings: tuple[MembershipWarning, ...] = ()

    @property
    def panes_present(self) -> bool:
        """Both the Codex and Claude panes are present in the cockpit."""
        return bool(self.codex_pane) and bool(self.claude_pane)

    @property
    def ok(self) -> bool:
        """Loaded, both peers present, and no warning-level geometry drift.

        :data:`GEOM_UNKNOWN` (a Project-Group-window Unit, present but outside the
        cockpit-window geometry diagnosis) counts as ok: it is loaded with both
        peers — only :data:`GEOM_WARNING` (missing peer / drift) and
        :data:`GEOM_ABSENT` (not loaded) are not-ok.
        """
        return self.member and self.geometry_status in (GEOM_OK, GEOM_UNKNOWN)

    def as_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "repo_root": self.repo_root,
            "lane_id": self.lane_id,
            "lane_label": self.lane_label,
            "session": self.session,
            "window": self.window,
            "window_id": self.window_id,
            "codex_pane": self.codex_pane,
            "claude_pane": self.claude_pane,
            "member": self.member,
            "panes_present": self.panes_present,
            "geometry_status": self.geometry_status,
            "registry_present": self.registry_present,
            "anchor_present": self.anchor_present,
            "ok": self.ok,
            "warnings": [w.as_dict() for w in self.warnings],
        }


@dataclass(frozen=True)
class CockpitMembershipReport:
    """The read-only result of a cockpit membership projection (#12341).

    ``workspaces`` are the loaded workspaces (`cockpit list`) or the single
    queried workspace (`cockpit status`). ``warnings`` are cockpit-wide advisories
    that do not belong to one workspace (a role-less pane, a column mixing two
    Units). ``note`` is :data:`MEMBERSHIP_NOTE` — the projection caveat.
    """

    session: str
    cockpit_present: bool
    workspaces: tuple[WorkspaceMembership, ...]
    warnings: tuple[MembershipWarning, ...] = ()
    note: str = MEMBERSHIP_NOTE

    @property
    def ok(self) -> bool:
        """No loaded workspace has a geometry warning and no cockpit-wide warning."""
        return not self.warnings and all(
            w.geometry_status in (GEOM_OK, GEOM_UNKNOWN) for w in self.workspaces
        )

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "cockpit_present": self.cockpit_present,
            "ok": self.ok,
            "note": self.note,
            "workspace_count": len(self.workspaces),
            "workspaces": [w.as_dict() for w in self.workspaces],
            "warnings": [w.as_dict() for w in self.warnings],
        }


def _unit_warning_findings(
    geometry: Optional[GeometryDiagnosis], workspace_id: str, lane_id: str
) -> list[MembershipWarning]:
    """Warning-level geometry findings touching this exact Unit (pure)."""
    if geometry is None:
        return []
    norm = normalize_lane(lane_id)
    out: list[MembershipWarning] = []
    for finding in geometry.findings:
        if finding.severity != SEVERITY_WARNING:
            continue
        if (finding.workspace_id or "") != workspace_id:
            continue
        if normalize_lane(finding.lane_id or "") != norm:
            continue
        out.append(MembershipWarning(finding.code, finding.message))
    return out


def _registry_warnings(facts: RegistryFacts) -> list[MembershipWarning]:
    """Scaffold / root-hardening advisories from registry + anchor absence (pure).

    Separated into the warning bucket (acceptance: "scaffold/root hardening の
    注意点は warning として分離") so the headline membership fact stays clean. An
    unregistered / anchor-less workspace still appears as a cockpit member; these
    only tell the operator the identity record is thin.
    """
    out: list[MembershipWarning] = []
    if not facts.registry_present:
        out.append(
            MembershipWarning(
                WARN_NOT_REGISTERED,
                "workspace is not in the home registry; label / repo_root may be "
                "unresolved. Register it with `mozyo-bridge workspace register` "
                "from the repo root.",
            )
        )
    if not facts.anchor_present:
        out.append(
            MembershipWarning(
                WARN_ANCHOR_ABSENT,
                "no workspace-anchor.json under the repo root; identity falls back "
                "to derivation. `mozyo-bridge workspace register` writes the anchor.",
            )
        )
    return out


def build_membership(
    *,
    session: str,
    observation: MembershipObservation,
    facts: RegistryFacts,
    geometry: Optional[GeometryDiagnosis],
) -> WorkspaceMembership:
    """Project one observed cockpit Unit into a :class:`WorkspaceMembership` (pure)."""
    workspace_id = observation.workspace_id
    lane_id = normalize_lane(observation.lane_id)
    codex = observation.codex_pane
    claude = observation.claude_pane
    in_cockpit_window = observation.window == COCKPIT_WINDOW

    warnings: list[MembershipWarning] = []
    geo_warnings = _unit_warning_findings(geometry, workspace_id, lane_id)

    if geo_warnings:
        # The cockpit-window diagnosis already saw a warning-level drift (missing
        # peer, split column, duplicate role) for this Unit — surface it verbatim.
        geometry_status = GEOM_WARNING
        warnings.extend(geo_warnings)
    elif not (codex and claude):
        # A group-window Unit (or a Unit the diagnosis did not cover) missing a
        # peer: derive the missing-peer warning from observed presence.
        geometry_status = GEOM_WARNING
        if codex and not claude:
            missing = "claude"
        elif claude and not codex:
            missing = "codex"
        else:
            missing = "codex + claude"
        warnings.append(
            MembershipWarning(
                WARN_MISSING_PEER,
                f"cockpit Unit is missing its {missing} pane (observed display "
                f"geometry).",
            )
        )
    elif not in_cockpit_window:
        # In a Project-Group window (#12330): full 2D geometry diagnosis is scoped
        # to the shared `cockpit` window, so report presence without asserting OK.
        geometry_status = GEOM_UNKNOWN
    else:
        geometry_status = GEOM_OK

    warnings.extend(_registry_warnings(facts))

    return WorkspaceMembership(
        workspace_id=workspace_id,
        label=facts.label,
        repo_root=facts.repo_root,
        lane_id=lane_id,
        lane_label=observation.lane_label,
        session=session,
        window=observation.window,
        window_id=observation.window_id,
        codex_pane=codex,
        claude_pane=claude,
        member=True,
        geometry_status=geometry_status,
        registry_present=facts.registry_present,
        anchor_present=facts.anchor_present,
        warnings=tuple(warnings),
    )


def absent_membership(
    *,
    session: str,
    workspace_id: str,
    label: str,
    repo_root: str,
    lane_id: str,
    lane_label: str,
    registry_present: bool,
    anchor_present: bool,
) -> WorkspaceMembership:
    """A :class:`WorkspaceMembership` for a workspace NOT loaded in the cockpit (pure).

    `cockpit status --repo <repo>` resolves a workspace's identity even when it is
    absent, so the operator gets an explicit "not loaded" answer (the #12339
    mis-read) instead of silence. ``member`` is ``False`` and ``geometry_status``
    is :data:`GEOM_ABSENT`.
    """
    warnings = [
        MembershipWarning(
            WARN_NOT_LOADED,
            f"workspace {label!r} is not loaded in cockpit {session!r}. Add it "
            f"with `cd <repo> && mozyo cockpit` (or `mozyo cockpit --repo <repo>`).",
        )
    ]
    warnings.extend(
        _registry_warnings(
            RegistryFacts(
                label=label,
                repo_root=repo_root,
                registry_present=registry_present,
                anchor_present=anchor_present,
            )
        )
    )
    return WorkspaceMembership(
        workspace_id=workspace_id,
        label=label,
        repo_root=repo_root,
        lane_id=normalize_lane(lane_id),
        lane_label=lane_label,
        session=session,
        window="",
        window_id="",
        codex_pane="",
        claude_pane="",
        member=False,
        geometry_status=GEOM_ABSENT,
        registry_present=registry_present,
        anchor_present=anchor_present,
        warnings=tuple(warnings),
    )


def _report_warnings(geometry: Optional[GeometryDiagnosis]) -> list[MembershipWarning]:
    """Cockpit-wide warnings not owned by one workspace (role-less / mixed column)."""
    if geometry is None:
        return []
    out: list[MembershipWarning] = []
    for finding in geometry.findings:
        if finding.severity != SEVERITY_WARNING:
            continue
        if finding.code == FINDING_ROLE_LESS_PANE:
            out.append(MembershipWarning(WARN_ROLE_LESS_PANE, finding.message))
        elif finding.code == FINDING_MIXED_UNIT_COLUMN:
            out.append(MembershipWarning(WARN_MIXED_UNIT_COLUMN, finding.message))
    return out


def project_membership_report(
    *,
    session: str,
    cockpit_present: bool,
    observations: Sequence[MembershipObservation],
    facts_by_workspace: Mapping[str, RegistryFacts],
    geometry: Optional[GeometryDiagnosis],
) -> CockpitMembershipReport:
    """Project the loaded cockpit Units into a membership report (#12341, pure).

    ``observations`` are the Units read from the live managed cockpit windows;
    ``facts_by_workspace`` maps each ``workspace_id`` to its resolved registry /
    anchor facts; ``geometry`` is the cockpit-window geometry diagnosis (or
    ``None`` when no cockpit window exists). Workspaces are ordered by label then
    workspace id then lane so the listing is stable.
    """
    workspaces = [
        build_membership(
            session=session,
            observation=obs,
            facts=facts_by_workspace.get(obs.workspace_id)
            or RegistryFacts.unresolved(obs.workspace_id),
            geometry=geometry,
        )
        for obs in observations
    ]
    workspaces.sort(key=lambda w: (w.label.lower(), w.workspace_id, w.lane_id))
    return CockpitMembershipReport(
        session=session,
        cockpit_present=cockpit_present,
        workspaces=tuple(workspaces),
        warnings=tuple(_report_warnings(geometry)),
    )


def format_membership_text(
    report: CockpitMembershipReport, *, query_label: Optional[str] = None
) -> str:
    """Human-readable rendering of a :class:`CockpitMembershipReport` (pure).

    ``query_label`` switches the heading to the `cockpit status` single-workspace
    phrasing; omitted, it renders the `cockpit list` enumeration heading.
    """
    lines: list[str] = []
    if query_label is not None:
        lines.append(f"cockpit membership: {query_label} in {report.session!r}")
    else:
        lines.append(f"cockpit membership: {report.session}")

    if not report.cockpit_present:
        lines.append(
            f"no cockpit session {report.session!r} is running — nothing loaded."
        )

    if not report.workspaces:
        if report.cockpit_present:
            lines.append("no workspaces are loaded in the cockpit.")
    else:
        lines.append(
            "WORKSPACE\tLANE\tWINDOW\tCODEX\tCLAUDE\tGEOMETRY\tREGISTRY\tANCHOR\tMEMBER"
        )
        for ws in report.workspaces:
            lines.append(
                "\t".join(
                    [
                        f"{ws.label} ({ws.workspace_id})",
                        ws.lane_id,
                        ws.window or "-",
                        ws.codex_pane or "-",
                        ws.claude_pane or "-",
                        ws.geometry_status,
                        "yes" if ws.registry_present else "no",
                        "yes" if ws.anchor_present else "no",
                        "yes" if ws.member else "no",
                    ]
                )
            )
            if ws.repo_root:
                lines.append(f"  repo: {ws.repo_root}")
            for warning in ws.warnings:
                lines.append(f"  [warning] {warning.code}: {warning.message}")

    for warning in report.warnings:
        lines.append(f"[warning] {warning.code}: {warning.message}")

    lines.append(f"note: {report.note}")
    return "\n".join(lines)
