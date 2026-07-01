"""Cockpit membership / list / status projection boundary (#12976).

Six read/projection helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, each projecting the live cockpit (over
the #12971 read adapter) into the ``cockpit list`` / ``cockpit status``
membership view:

- ``_cockpit_unit_repo_root`` — a cockpit Unit's live checkout root, walked up
  from its codex / claude pane cwd (#12341, review j#62643).
- ``_membership_observations_from_windows`` — reshape the managed-window columns
  into one :class:`MembershipObservation` per ``(workspace_id, lane_id)`` Unit.
- ``_resolve_registry_facts`` — a workspace id's home-registry / anchor facts,
  fail-closed to "unresolved" on a thin / unreadable identity record.
- ``_collect_cockpit_membership`` — the whole live-to-report projection (managed
  windows + geometry diagnosis + registry facts → pure report).
- ``_handle_cockpit_list`` / ``_handle_cockpit_status`` — the CLI handlers.

This module carves that into an OOP-first boundary under #12638:

- The module-level ``build_membership_observations`` /
  ``UnitRepoRootUseCase.resolve`` projections own the pure reshape / repo-root
  pick, with the side effects (pane-runtime / registry / identity reads) supplied
  by injected ports.
- :class:`UnitRepoRootOps` / :class:`RegistryFactsOps` /
  :class:`CockpitMembershipOps` are the ports for the environment reads, with
  ``Live*`` adapters. Every adapter resolves its target *through the*
  :mod:`commands` *module (or* :mod:`workspace_registry` *) at call time*, so the
  cockpit membership characterization tests that patch
  ``mozyo_bridge.application.commands._read_managed_cockpit_windows`` /
  ``._read_cockpit_geometry`` / ``._cockpit_unit_repo_root`` /
  ``._resolve_registry_facts`` / ``.resolve_canonical_session`` /
  ``._resolve_workspace_lane`` keep intercepting unchanged, and this module never
  imports :mod:`commands` at module scope (no import cycle).
- :class:`CockpitMembershipUseCase` composes the ports + projections into the
  ``collect`` report and the ``list`` / ``status`` outcomes; the thin
  :mod:`commands` wrappers build the live ops, run the use case, and print the
  rendered text.

Behavior-preserving: the read tolerance (a missing tmux / registry degrades to
an empty / unresolved projection rather than raising), the projected report /
JSON shapes, and the ``cockpit list`` / ``cockpit status`` CLI output + exit
conventions (``list`` always ``0``; ``status`` ``0`` when the queried workspace
is a healthy loaded member, ``1`` otherwise) are unchanged from the original
command bodies.
"""

from __future__ import annotations

import dataclasses
import json as _json
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    infer_repo_root,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
    CockpitMembershipReport,
    MembershipObservation,
    RegistryFacts,
    absent_membership,
    format_membership_text,
    project_membership_report,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    ROLE_CLAUDE,
    ROLE_CODEX,
    normalize_lane,
)


# --- Pure projection: group managed windows into per-Unit observations. -------


def build_membership_observations(
    managed_windows: Any,
    session: str,
    repo_root_for: Callable[[str, str], str],
) -> list[MembershipObservation]:
    """Group managed-cockpit-window columns into per-Unit observations (#12341).

    Reshapes the ``_read_managed_cockpit_windows`` output (a list of windows,
    each with its ``columns``) into one :class:`MembershipObservation` per
    ``(workspace_id, lane_id)`` Unit, collapsing the Unit's codex / claude panes
    and resolving each Unit's live checkout root via the injected
    ``repo_root_for(codex_pane, claude_pane)`` callable (so a worktree / lane
    reports its own path, not the registry canonical — review j#62643).
    Role-less columns (no ``workspace_id``) are skipped here — they surface as a
    cockpit-wide warning from the geometry diagnosis instead.
    """

    observations: list[MembershipObservation] = []
    for window in managed_windows or []:
        units: dict[tuple[str, str], dict[str, str]] = {}
        order: list[tuple[str, str]] = []
        for col in window.get("columns", []) or []:
            workspace_id = col.get("workspace_id") or ""
            if not workspace_id:
                continue
            key = (workspace_id, normalize_lane(col.get("lane_id")))
            if key not in units:
                units[key] = {"codex": "", "claude": ""}
                order.append(key)
            role = col.get("role")
            pane_id = col.get("pane_id") or ""
            if role == ROLE_CODEX and not units[key]["codex"]:
                units[key]["codex"] = pane_id
            elif role == ROLE_CLAUDE and not units[key]["claude"]:
                units[key]["claude"] = pane_id
        for key in order:
            codex_pane = units[key]["codex"]
            claude_pane = units[key]["claude"]
            observations.append(
                MembershipObservation(
                    workspace_id=key[0],
                    lane_id=key[1],
                    lane_label="",
                    codex_pane=codex_pane,
                    claude_pane=claude_pane,
                    window=window.get("window") or "",
                    window_id=window.get("window_id") or "",
                    repo_root=repo_root_for(codex_pane, claude_pane),
                )
            )
    return observations


# --- Unit repo-root: pick the first resolvable checkout from pane cwds. --------


@runtime_checkable
class UnitRepoRootOps(Protocol):
    """Port: the pane-runtime read the Unit repo-root resolver needs."""

    def read_pane_runtime(self, session: str, pane_id: str) -> dict: ...


class LiveUnitRepoRootOps:
    """Live :class:`UnitRepoRootOps` over the ``commands`` pane-runtime seam.

    Resolves ``commands._read_cockpit_pane_runtime`` *at call time* so the tests
    that patch that module seam keep intercepting, and so this module never
    imports :mod:`commands` at module scope.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def read_pane_runtime(self, session: str, pane_id: str) -> dict:
        return self._commands()._read_cockpit_pane_runtime(session, pane_id)


class UnitRepoRootUseCase:
    """Resolve a cockpit Unit's live checkout root from its pane cwds (#12341).

    A worktree / lane shares its workspace id with the main checkout, so the
    registry's single canonical path mislabels it (review j#62643). The live
    truth is the pane's working directory, walked up to the repo root. Reads the
    Unit's panes in order (codex first, then claude) and returns the first
    resolvable repo root, or ``""`` when no pane cwd is readable (the caller then
    falls back to the registry path). Tolerant: any read failure degrades the
    port to empty runtimes.
    """

    def __init__(self, ops: UnitRepoRootOps) -> None:
        self._ops = ops

    def resolve(self, session: str, *pane_ids: str) -> str:
        for pane_id in pane_ids:
            if not pane_id:
                continue
            runtime = self._ops.read_pane_runtime(session, pane_id)
            cwd = (runtime.get("cwd") or "").strip()
            if not cwd:
                continue
            root = infer_repo_root(cwd)
            if root:
                return root
        return ""


# --- Registry facts: home-registry label / repo root / anchor presence. -------


@runtime_checkable
class RegistryFactsOps(Protocol):
    """Port: the home-registry / anchor reads the facts resolver needs."""

    def load_workspace(self, workspace_id: str) -> Any: ...

    def anchor_present(self, repo_root: str) -> bool: ...


class LiveRegistryFactsOps:
    """Live :class:`RegistryFactsOps` over :mod:`workspace_registry`.

    Imports the registry reads at call time (no module-scope :mod:`commands`
    dependency); the workspace registry itself is patched by no test, so the
    ``commands._resolve_registry_facts`` wrapper is the wholesale seam the
    membership tests intercept.
    """

    def load_workspace(self, workspace_id: str) -> Any:
        from mozyo_bridge.workspace_registry import load_workspace_by_id

        return load_workspace_by_id(workspace_id)

    def anchor_present(self, repo_root: str) -> bool:
        from mozyo_bridge.workspace_registry import read_anchor

        return read_anchor(Path(repo_root)) is not None


class RegistryFactsUseCase:
    """Resolve a cockpit workspace id's registry / anchor facts (#12341).

    A cockpit pane carries only its ``@mozyo_workspace_id``; the human label and
    repo root live in the home registry, and the anchor presence in the workspace
    itself. Tolerant: a missing / unreadable registry degrades to "unresolved"
    (label falls back to the id, repo root empty) rather than raising, so the
    membership view never aborts on a thin identity record.
    """

    def __init__(self, ops: RegistryFactsOps) -> None:
        self._ops = ops

    def resolve(self, workspace_id: str) -> RegistryFacts:
        try:
            record = self._ops.load_workspace(workspace_id)
        except (Exception, SystemExit):
            record = None
        if record is None:
            return RegistryFacts.unresolved(workspace_id)
        repo_root = getattr(record, "canonical_path", "") or ""
        anchor_present = False
        if repo_root:
            try:
                anchor_present = self._ops.anchor_present(repo_root)
            except (Exception, SystemExit):
                anchor_present = False
        return RegistryFacts(
            label=getattr(record, "project_name", "") or workspace_id,
            repo_root=repo_root,
            registry_present=True,
            anchor_present=anchor_present,
        )


# --- Membership use case: the live-to-report projection + list / status. ------


@runtime_checkable
class CockpitMembershipOps(Protocol):
    """Port: the environment reads the membership projection composes over.

    Each is a seam the cockpit membership characterization tests already patch on
    the :mod:`commands` module; the live adapter routes them through that module
    at call time so those patches keep intercepting.
    """

    def read_managed_windows(self, session: str) -> Any: ...

    def read_geometry(self, session: str) -> Any: ...

    def unit_repo_root(self, session: str, *pane_ids: str) -> str: ...

    def resolve_registry_facts(self, workspace_id: str) -> RegistryFacts: ...

    def resolve_canonical_session(self, repo_root: str) -> Any: ...

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any: ...


class LiveCockpitMembershipOps:
    """Live :class:`CockpitMembershipOps` over the real ``commands`` seams.

    Each method resolves its target *through the* :mod:`commands` *module at call
    time*. In particular ``unit_repo_root`` / ``resolve_registry_facts`` route
    through the thin ``commands._cockpit_unit_repo_root`` /
    ``commands._resolve_registry_facts`` wrappers (not this module's sub-use-cases
    directly), so the membership tests that patch those wholesale seams still
    intercept.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def read_managed_windows(self, session: str) -> Any:
        return self._commands()._read_managed_cockpit_windows(session)

    def read_geometry(self, session: str) -> Any:
        return self._commands()._read_cockpit_geometry(session)

    def unit_repo_root(self, session: str, *pane_ids: str) -> str:
        return self._commands()._cockpit_unit_repo_root(session, *pane_ids)

    def resolve_registry_facts(self, workspace_id: str) -> RegistryFacts:
        return self._commands()._resolve_registry_facts(workspace_id)

    def resolve_canonical_session(self, repo_root: str) -> Any:
        return self._commands().resolve_canonical_session(repo_root)

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any:
        return self._commands()._resolve_workspace_lane(repo_root, workspace_id)


@dataclasses.dataclass(frozen=True)
class CockpitListOutcome:
    """`cockpit list` result: the membership report, always exit ``0``."""

    report: CockpitMembershipReport
    exit_code: int = 0

    def render(self, *, json_output: bool) -> str:
        if json_output:
            return _json.dumps(
                self.report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
        return format_membership_text(self.report)


@dataclasses.dataclass(frozen=True)
class CockpitStatusOutcome:
    """`cockpit status` result: single-workspace report + query block + exit code."""

    report: CockpitMembershipReport
    query: dict
    query_label: str
    ok: bool

    @property
    def exit_code(self) -> int:
        # Mirrors doctor-geometry: 0 when the queried workspace is a healthy
        # loaded member, 1 otherwise (absent, missing peer, or geometry warning).
        return 0 if self.ok else 1

    def render(self, *, json_output: bool) -> str:
        if json_output:
            payload = self.report.as_dict()
            payload["query"] = dict(self.query)
            return _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return format_membership_text(self.report, query_label=self.query_label)


class CockpitMembershipUseCase:
    """Project the live cockpit into the membership report / list / status views.

    All reads are tolerant (a missing tmux / cockpit degrades to an empty
    report), so ``cockpit list`` / ``status`` never abort. Cockpit membership is
    a display / liveness projection, never Redmine workflow truth.
    """

    def __init__(self, ops: CockpitMembershipOps) -> None:
        self._ops = ops

    def collect(self, session: str) -> CockpitMembershipReport:
        """Project the live cockpit into a membership report (#12341, read-only).

        Reads every managed cockpit window (shared ``cockpit`` window + #12330
        Project Group windows), runs the read-only geometry diagnosis for drift
        findings, resolves each Unit's registry / anchor facts, and hands them all
        to the pure :func:`project_membership_report`.
        """
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_geometry import (
            diagnose_cockpit_geometry,
        )

        managed = self._ops.read_managed_windows(session)
        geometry = diagnose_cockpit_geometry(
            session=session, panes=self._ops.read_geometry(session)
        )
        observations = build_membership_observations(
            managed,
            session,
            lambda codex_pane, claude_pane: self._ops.unit_repo_root(
                session, codex_pane, claude_pane
            ),
        )
        cockpit_present = bool(managed) or geometry.cockpit_present

        facts: dict[str, object] = {}
        for obs in observations:
            if obs.workspace_id not in facts:
                facts[obs.workspace_id] = self._ops.resolve_registry_facts(
                    obs.workspace_id
                )

        return project_membership_report(
            session=session,
            cockpit_present=cockpit_present,
            observations=observations,
            facts_by_workspace=facts,
            geometry=geometry,
        )

    def list(self, session: str) -> CockpitListOutcome:
        """`mozyo cockpit list` — operator-facing membership summary (#12341).

        Read-only: enumerates the workspaces loaded in the cockpit. Always exits
        ``0`` — an empty cockpit is a valid state, not an error.
        """
        return CockpitListOutcome(report=self.collect(session))

    def status(self, *, session: str, repo: str) -> CockpitStatusOutcome:
        """`mozyo cockpit status --repo <repo>` — repo-scoped membership (#12341).

        Read-only: resolves the repo's workspace identity (registry → anchor →
        derivation, the same chain the rest of the cockpit uses) and reports
        whether it is loaded, with its panes / geometry / registry presence. When
        the workspace is absent it says so explicitly (the #12339 mis-read).
        """
        repo_root = str(Path(repo).expanduser().resolve())
        # The operator asked about THIS checkout: report the queried repo root
        # (walked to its repo top), not the registry canonical / main checkout
        # (review j#62643).
        queried_root = infer_repo_root(repo_root) or repo_root
        canon = self._ops.resolve_canonical_session(repo_root)
        workspace_id = getattr(canon, "workspace_id", None) or canon.name
        lane = self._ops.resolve_workspace_lane(
            repo_root, getattr(canon, "workspace_id", None)
        )
        target_lane = normalize_lane(lane.lane_id)
        facts = self._ops.resolve_registry_facts(workspace_id)

        report = self.collect(session)
        match = next(
            (
                w
                for w in report.workspaces
                if w.workspace_id == workspace_id
                and normalize_lane(w.lane_id) == target_lane
            ),
            None,
        )
        if match is None:
            label = facts.label if facts.registry_present else canon.name
            match = absent_membership(
                session=session,
                workspace_id=workspace_id,
                label=label,
                repo_root=queried_root,
                lane_id=target_lane,
                lane_label=lane.lane_label,
                registry_present=facts.registry_present,
                anchor_present=facts.anchor_present,
                registry_canonical_path=facts.repo_root,
            )
        else:
            # Pin the matched row's repo root to the queried checkout so a
            # worktree / lane query echoes the path the operator asked about,
            # never the registry canonical main checkout (review j#62643).
            match = dataclasses.replace(match, repo_root=queried_root)

        single = CockpitMembershipReport(
            session=session,
            cockpit_present=report.cockpit_present,
            workspaces=(match,),
            warnings=report.warnings,
        )
        query = {
            "workspace_id": workspace_id,
            "label": match.label,
            "repo_root": queried_root,
            "registry_canonical_path": facts.repo_root,
            "lane_id": target_lane,
            "member": match.member,
        }
        return CockpitStatusOutcome(
            report=single, query=query, query_label=match.label, ok=match.ok
        )
