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
  ``collect`` report and the ``list`` / ``status`` outcomes.
- The thin command adapters (``_membership_observations_from_windows`` /
  ``_resolve_registry_facts`` / ``_collect_cockpit_membership`` /
  ``_handle_cockpit_list`` / ``_handle_cockpit_status``) build the live ops, run
  the use case, and print the rendered text. They moved here from
  :mod:`commands` under #13122 and are re-exported there, so the existing
  ``commands.*`` import / monkeypatch seams keep resolving; each still routes its
  environment reads through the :mod:`commands` module at call time.

Behavior-preserving: the read tolerance (a missing tmux / registry degrades to
an empty / unresolved projection rather than raising), the projected report /
JSON shapes, and the ``cockpit list`` / ``cockpit status`` CLI output + exit
conventions (``list`` always ``0``; ``status`` ``0`` when the queried workspace
is a healthy loaded member, ``1`` otherwise) are unchanged from the original
command bodies.
"""

from __future__ import annotations

import argparse
import dataclasses
import json as _json
import os
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    infer_repo_root,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (
    herdr_inventory,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    PANE_KEY_ID,
    PANE_KEY_LANE,
    PANE_KEY_ROLE,
    PANE_KEY_WORKSPACE,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    WARN_HERDR_INVENTORY_UNAVAILABLE,
    CockpitMembershipReport,
    MembershipObservation,
    MembershipWarning,
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
                units[key] = {"codex": "", "claude": "", "backend": BACKEND_TMUX}
                order.append(key)
            role = col.get("role")
            pane_id = col.get("pane_id") or ""
            if role == ROLE_CODEX and not units[key]["codex"]:
                units[key]["codex"] = pane_id
            elif role == ROLE_CLAUDE and not units[key]["claude"]:
                units[key]["claude"] = pane_id
            # A column may carry an optional ``backend`` hint (default tmux, so the
            # live tmux read stays byte-invariant). Any non-tmux column marks the
            # whole Unit non-tmux, so the projection degrades its tmux-only fields
            # honestly (#13298). Live herdr-column wiring is deferred (full parity).
            col_backend = col.get("backend") or BACKEND_TMUX
            if col_backend != BACKEND_TMUX:
                units[key]["backend"] = col_backend
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
                    backend=units[key]["backend"],
                )
            )
    return observations


# --- Pure projection: tag live herdr `agent list` Units as herdr columns. ------


def herdr_membership_observations(
    agent_rows: Any,
) -> list[MembershipObservation]:
    """Project a live herdr ``agent list`` snapshot into herdr-tagged Units (#13303).

    This is the supply source #13298 deferred: the cockpit degrade projection can
    already render a :data:`BACKEND_HERDR` Unit honestly, but nothing fed it live
    herdr membership. Here each ``agent list`` row is decoded — through the
    #13297 backend-neutral :func:`herdr_inventory`, the single sanctioned decode
    path — into its stable ``(workspace_id, lane_id, role)`` slot (its assigned
    name, never a tmux pane id, is the identity; foreign / non-mzb1 agents are
    dropped). Rows are grouped into one :class:`MembershipObservation` per
    ``(workspace_id, lane_id)`` Unit, collapsing the Unit's codex / claude agents,
    tagged :data:`BACKEND_HERDR` so :func:`build_membership` degrades the tmux-only
    fields (panes / window / geometry) instead of showing a stale tmux value.

    The transient herdr locator is *not* carried onto the observation's pane
    fields: it is cache / evidence only (#13297), and the degrade projection
    replaces those fields with a token regardless, so leaving them empty keeps the
    locator from ever reading as route authority. ``repo_root`` is left empty (a
    herdr Unit has no cockpit pane cwd to walk); the registry canonical path fills
    it in downstream. Full herdr cockpit parity (live layout / focus / adopt) stays
    deferred — this only makes the herdr membership real instead of empty.
    """
    units: dict[tuple[str, str], dict[str, str]] = {}
    order: list[tuple[str, str]] = []
    for row in herdr_inventory(agent_rows or []):
        workspace_id = row.get(PANE_KEY_WORKSPACE) or ""
        if not workspace_id:
            continue
        key = (workspace_id, normalize_lane(row.get(PANE_KEY_LANE)))
        if key not in units:
            units[key] = {"codex": "", "claude": ""}
            order.append(key)
        role = row.get(PANE_KEY_ROLE)
        locator = row.get(PANE_KEY_ID) or ""
        if role == ROLE_CODEX and not units[key]["codex"]:
            units[key]["codex"] = locator
        elif role == ROLE_CLAUDE and not units[key]["claude"]:
            units[key]["claude"] = locator
    observations: list[MembershipObservation] = []
    for key in order:
        observations.append(
            MembershipObservation(
                workspace_id=key[0],
                lane_id=key[1],
                lane_label="",
                # Transient herdr locators are evidence, not authority (#13297); the
                # degrade projection tokenises these fields anyway, so keep them empty.
                codex_pane="",
                claude_pane="",
                window="",
                window_id="",
                repo_root="",
                backend=BACKEND_HERDR,
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


# --- Herdr column supply: live `agent list` inventory -> herdr Units (#13303). --


@runtime_checkable
class HerdrColumnOps(Protocol):
    """Port: the live herdr ``agent list`` inventory the cockpit tags herdr Units from.

    :meth:`read_herdr_agent_rows` returns the raw herdr ``agent list`` rows when the
    herdr backend is selected, or ``None`` when it is off (the tmux default) — the
    ``None`` sentinel keeps the tmux projection byte-invariant, distinct from ``[]``
    (herdr on, zero agents). It may raise a fail-closed transport error on an
    unreadable snapshot; the use case catches it and degrades to a warning rather
    than aborting the whole membership view.
    """

    def read_herdr_agent_rows(self) -> Optional[Sequence[Any]]: ...


class NullHerdrColumnOps:
    """Default :class:`HerdrColumnOps`: herdr backend off, no herdr Units.

    Yields ``None`` so a :class:`CockpitMembershipUseCase` built without an explicit
    herdr supply projects exactly the tmux membership it always did (byte-invariant).
    The live CLI handlers inject :class:`LiveHerdrColumnOps` instead.
    """

    def read_herdr_agent_rows(self) -> Optional[Sequence[Any]]:
        return None


class LiveHerdrColumnOps:
    """Live :class:`HerdrColumnOps` over the repo-local config + built-in `agent list`.

    Resolves the repo-local ``terminal_transport`` selection for ``repo_root`` and,
    **only when the herdr backend is explicitly selected**, runs the built-in herdr
    agent lister (#13261). Default-off and fail-soft on selection: the tmux default
    backend — and a missing / unreadable / malformed repo-local config, exactly like
    :func:`herdr_backend_selected` — returns ``None`` (no herdr Units), so the cockpit
    projection is byte-invariant whenever herdr is not deliberately turned on. Once
    herdr *is* selected, an unreadable live snapshot is a fail-closed
    :class:`TerminalTransportError` propagated to the caller (never a silent empty
    inventory that would read as "no herdr Units").

    ``repo_root`` defaults to the current working directory: ``cockpit`` is
    session-scoped, and the operator invokes it from a repo whose config declares
    whether this environment runs herdr. Per-workspace herdr config resolution is a
    deferred refinement (full herdr parity stays deferred, #13298 / #13263 j#72594).
    """

    def __init__(self, repo_root: Optional[str] = None) -> None:
        self._repo_root = repo_root if repo_root is not None else os.getcwd()

    def read_herdr_agent_rows(self) -> Optional[Sequence[Any]]:
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
            RepoLocalConfigError,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
            resolve_agent_lister,
        )

        try:
            config = load_repo_local_config(self._repo_root).terminal_transport
        except RepoLocalConfigError:
            # A broken / unreadable config is not a herdr selection (it resolves to
            # the tmux default), so it never diverts the cockpit onto the herdr path.
            return None
        lister = resolve_agent_lister(config)
        if lister is None:
            # tmux (default) backend: herdr is off -> no herdr Units.
            return None
        return lister.list_agent_rows()


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
            # The JSON carries two verdicts with distinct scopes (review j#73096):
            # the top-level ``ok`` is report-health across every displayed row (the
            # ``cockpit list`` semantics — in dual-presence a degraded tmux row can
            # make it False), while ``query.ok`` is the query verdict that mirrors
            # ``self.ok`` / :attr:`exit_code` (``any(w.ok)`` over the matching backend
            # rows). A machine consumer keys on ``query.ok``, never the whole-view
            # ``ok``, so the JSON verdict never contradicts the exit code.
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

    def __init__(
        self,
        ops: CockpitMembershipOps,
        herdr_ops: Optional[HerdrColumnOps] = None,
    ) -> None:
        self._ops = ops
        # Default to the null supply so an existing tmux-only caller is unchanged
        # (byte-invariant); the live CLI handlers inject LiveHerdrColumnOps.
        self._herdr_ops: HerdrColumnOps = herdr_ops or NullHerdrColumnOps()

    def _herdr_observations(
        self,
    ) -> tuple[list[MembershipObservation], list[MembershipWarning]]:
        """Read the live herdr inventory and tag its Units (#13303, tolerant).

        Returns the herdr :class:`MembershipObservation` list plus any cockpit-wide
        warnings. Herdr off (the default) yields ``([], [])`` so the tmux projection
        is byte-invariant. An unreadable live herdr snapshot degrades to no herdr
        Units *plus* a :data:`WARN_HERDR_INVENTORY_UNAVAILABLE` advisory — so the
        operator never mistakes an unreadable snapshot for an empty one — and never
        aborts the whole membership view.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
            TerminalTransportError,
        )

        try:
            rows = self._herdr_ops.read_herdr_agent_rows()
        except TerminalTransportError as exc:
            return [], [
                MembershipWarning(
                    WARN_HERDR_INVENTORY_UNAVAILABLE,
                    "herdr backend is selected but its live `agent list` inventory "
                    f"could not be read ({exc}); herdr Units are omitted from this "
                    "view. Retry once the herdr server is reachable.",
                )
            ]
        if rows is None:
            return [], []
        return herdr_membership_observations(rows), []

    def collect(self, session: str) -> CockpitMembershipReport:
        """Project the live cockpit into a membership report (#12341, read-only).

        Reads every managed cockpit window (shared ``cockpit`` window + #12330
        Project Group windows), runs the read-only geometry diagnosis for drift
        findings, tags any live herdr Units from the herdr inventory (#13303),
        resolves each Unit's registry / anchor facts, and hands them all to the pure
        :func:`project_membership_report`.
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
        # Live herdr Units come from the herdr `agent list` inventory, not the tmux
        # managed windows, so they are a separate supply concatenated here (a Unit
        # runs on one backend, so the two inventories are disjoint by construction).
        herdr_observations, herdr_warnings = self._herdr_observations()
        observations = observations + herdr_observations
        # A live herdr Unit is a present cockpit membership just as a tmux Unit is:
        # without this, a herdr-only environment (no tmux managed windows / geometry)
        # would report a `member` herdr row *and* ``cockpit_present`` False, so the
        # text says "nothing loaded" above a populated table and the JSON pairs
        # ``cockpit_present: false`` with a member row (review j#72953). herdr off
        # yields no herdr observations, so the tmux-only path is byte-invariant.
        cockpit_present = (
            bool(managed) or geometry.cockpit_present or bool(herdr_observations)
        )

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
            extra_warnings=herdr_warnings,
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

        During the herdr backend swap (#13317) the queried slot can hold both a
        tmux rollback-lever Unit and a live herdr Unit at once; this returns every
        matching backend row so the live herdr agent is never hidden behind the
        tmux row (auditor j#73083 decision (a)). A tmux-only / herdr-only / absent
        slot still yields a single row (byte-invariant).
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
        # Dual-backend transition (#13317, auditor j#73083 decision (a)): the same
        # (workspace_id, lane_id) slot can carry BOTH a tmux rollback-lever Unit and
        # a live herdr Unit. Keep *every* matching row rather than the historical
        # first-match, so a live herdr agent is never hidden behind a tmux row and
        # `status` agrees with `cockpit list` (which already shows both). Each row's
        # repo root is pinned to the queried checkout so a worktree / lane query
        # echoes the path the operator asked about, never the registry canonical
        # main checkout (review j#62643). A tmux-only / herdr-only slot still yields
        # exactly one row, so that output stays byte-invariant.
        matches = tuple(
            dataclasses.replace(w, repo_root=queried_root)
            for w in report.workspaces
            if w.workspace_id == workspace_id
            and normalize_lane(w.lane_id) == target_lane
        )
        if not matches:
            label = facts.label if facts.registry_present else canon.name
            matches = (
                absent_membership(
                    session=session,
                    workspace_id=workspace_id,
                    label=label,
                    repo_root=queried_root,
                    lane_id=target_lane,
                    lane_label=lane.lane_label,
                    registry_present=facts.registry_present,
                    anchor_present=facts.anchor_present,
                    registry_canonical_path=facts.repo_root,
                ),
            )

        single = CockpitMembershipReport(
            session=session,
            cockpit_present=report.cockpit_present,
            workspaces=matches,
            warnings=report.warnings,
        )
        # The query verdict aggregates the matching backend rows: the slot is a
        # `member` if any backend row is loaded, and OK (exit 0) if any is healthy;
        # per-row warnings / ok stay on each workspace row. For the single-row
        # tmux-only / herdr-only / absent case this is byte-identical to the prior
        # first-match verdict (``any`` over one element).
        #
        # `query.ok` carries this query verdict explicitly so the JSON machine-
        # readable verdict never disagrees with the exit code (review j#73096): the
        # report's top-level `ok` stays report-health (`all(w.ok)`, the `cockpit
        # list` semantics), which in dual-presence can be False when a degraded
        # tmux row sits beside a healthy herdr row even though the query resolves to
        # a healthy member (exit 0). `query.ok` mirrors `CockpitStatusOutcome.ok` /
        # exit; a consumer keys on `query.ok`, not the whole-view `ok`. Added as a
        # backward-compatible field (auditor j#73083) so existing fields are
        # unchanged; for a single-row slot `any` over one element makes it equal the
        # row's own ok, so tmux-only / herdr-only / absent stay byte-invariant.
        query_ok = any(w.ok for w in matches)
        query_label = matches[0].label
        query = {
            "workspace_id": workspace_id,
            "label": query_label,
            "repo_root": queried_root,
            "registry_canonical_path": facts.repo_root,
            "lane_id": target_lane,
            "member": any(w.member for w in matches),
            "ok": query_ok,
        }
        return CockpitStatusOutcome(
            report=single,
            query=query,
            query_label=query_label,
            ok=query_ok,
        )


# --- Thin command adapters: the ``commands.*`` compatibility surface (#13122). --


def _membership_observations_from_windows(managed_windows: Any, session: str):
    """Group managed-cockpit-window columns into per-Unit observations (#12341).

    Reshapes the ``_read_managed_cockpit_windows`` output (a list of windows,
    each with its `columns`) into one :class:`MembershipObservation` per
    ``(workspace_id, lane_id)`` Unit, collapsing the Unit's codex / claude panes
    and resolving each Unit's live checkout root from its pane cwd (so a
    worktree / lane reports its own path, not the registry canonical — review
    j#62643). Role-less columns (no ``workspace_id``) are skipped here — they
    surface as a cockpit-wide warning from the geometry diagnosis instead.
    """
    from mozyo_bridge.application import commands

    # The repo-root resolution is routed through
    # ``commands._cockpit_unit_repo_root`` at call time so the membership tests
    # that patch that seam keep intercepting.
    return build_membership_observations(
        managed_windows,
        session,
        lambda codex_pane, claude_pane: commands._cockpit_unit_repo_root(
            session, codex_pane, claude_pane
        ),
    )


def _resolve_registry_facts(workspace_id: str) -> RegistryFacts:
    """Resolve a cockpit workspace id's registry / anchor facts (#12341, read-only).

    A cockpit pane carries only its ``@mozyo_workspace_id``; the human label and
    repo root live in the home registry, and the anchor presence in the workspace
    itself. Tolerant: a missing / unreadable registry degrades to "unresolved"
    (label falls back to the id, repo root empty) rather than raising, so the
    membership view never aborts on a thin identity record.
    """
    return RegistryFactsUseCase(LiveRegistryFactsOps()).resolve(workspace_id)


def _collect_cockpit_membership(session: str) -> CockpitMembershipReport:
    """Project the live cockpit into a membership report (#12341, read-only).

    Reads every managed cockpit window (shared `cockpit` window + #12330 Project
    Group windows) for the loaded Units, runs the existing read-only geometry
    diagnosis on the `cockpit` window for drift findings, resolves each Unit's
    registry / anchor facts, and hands them all to the pure
    :func:`project_membership_report`. All reads are tolerant: a missing tmux /
    cockpit degrades to an empty report, so `cockpit list` / `status` never
    abort. The live adapter routes every read (managed windows / geometry / unit
    repo root / registry facts) through the :mod:`commands` module at call time,
    so the membership characterization tests that patch those seams keep
    intercepting.
    """
    return CockpitMembershipUseCase(LiveCockpitMembershipOps()).collect(session)


def _handle_cockpit_list(session: str, *, json_output: bool) -> int:
    """`mozyo cockpit list` — operator-facing cockpit membership summary (#12341).

    Read-only: enumerates the workspaces loaded in the cockpit, each with its
    workspace label / id, repo root, window, Codex / Claude pane ids, geometry
    status, and registry / anchor presence (scaffold / root-hardening notes split
    into a warning bucket). Always exits ``0`` — an empty cockpit is a valid
    state, not an error. Cockpit membership is a display / liveness projection,
    never Redmine workflow truth.
    """
    outcome = CockpitMembershipUseCase(
        LiveCockpitMembershipOps(), LiveHerdrColumnOps()
    ).list(session)
    print(outcome.render(json_output=json_output))
    return outcome.exit_code


def _handle_cockpit_status(
    args: argparse.Namespace, session: str, *, json_output: bool
) -> int:
    """`mozyo cockpit status --repo <repo>` — repo-scoped cockpit membership (#12341).

    Read-only: resolves the repo's workspace identity (registry → anchor →
    derivation, the same chain the rest of the cockpit uses) and reports whether
    it is loaded in the cockpit, with its panes / geometry / registry presence.
    When the workspace is absent it says so explicitly (the #12339 mis-read)
    instead of staying silent. Mirrors `doctor-geometry`'s exit convention: ``0``
    when the workspace is a loaded member with healthy geometry, ``1`` otherwise
    (absent, missing peer, or a geometry warning) — so a script can branch on the
    code while still parsing the full report from stdout.
    """
    # The repo argument extraction stays here (argparse-facing); the use case is
    # handed a resolved repo path and owns the identity / projection.
    repo = getattr(args, "repo", None) or getattr(args, "cwd", None) or os.getcwd()
    outcome = CockpitMembershipUseCase(
        LiveCockpitMembershipOps(), LiveHerdrColumnOps(repo)
    ).status(session=session, repo=repo)
    print(outcome.render(json_output=json_output))
    return outcome.exit_code
