"""status command family — OOP-first session-read + command-handler boundary
(Redmine #12830 / #12825 / #12785 / #12638 / #12749).

#12785 (the first tranche) pulled the ``status`` command's three external
session reads — session existence, window enumeration, and the agent-pane
capture — behind
:class:`~mozyo_bridge.application.status_session_port.StatusSessionPort`, and
moved the present/missing agent-window classification the procedural
``cmd_status`` inlined into :class:`ResolveSessionStatusUseCase`, which returns
a frozen :class:`SessionStatusView` value object.

#12825 (this tranche) carves the remaining ``cmd_status`` orchestration into an
OOP-first command-handler boundary:

* :class:`StatusCommandRequest` — the typed input the handler consumes, so
  ``cmd_status`` no longer threads ``argparse.Namespace`` past
  :func:`resolve_status_session`.
* :func:`render_status_report` — a *pure* presentation function turning the
  :class:`SessionStatusView` and the cockpit-membership projection into the
  exact stdout block (byte-preserving), with no ``print`` side effect.
* :class:`StatusCockpitMembershipPort` / :class:`LiveStatusCockpitMembership` —
  the cockpit-membership projection behind an injectable port so the handler is
  driven by a fake policy, not a ``commands.*`` monkeypatch.
* :class:`StatusReport` — the typed result the handler returns (the rendered
  block); the thin ``cmd_status`` adapter prints it and delegates the doctor
  tail.
* :class:`StatusCommandHandler` — composes the session-read use case, the
  membership port, and the pure renderer into one typed ``handle`` step.

#12830 (this tranche) resolves the ``_status_repo_cockpit_membership`` residual
that #12825 carried in ``commands.py``: the procedural projection body is
decomposed into a typed boundary owned here —

* :class:`CockpitMembershipIdentity` — the value object the projection threads
  from the live identity reads (workspace id + lane) into both the pure match
  policy and the absent-record construction.
* :func:`match_cockpit_membership` — the *pure* domain policy selecting this
  repo's loaded cockpit record by workspace id + normalized lane, unit tested
  with fake workspace records (no live read, no ``commands.*`` monkeypatch).
* :class:`StatusCockpitMembershipReads` / :class:`LiveStatusCockpitMembershipReads`
  — the cockpit reads (identity / membership collection / absent record) behind
  one injectable port; the live adapter routes to the ``commands.*`` cockpit
  helpers *at call time* so the existing cockpit monkeypatch tests are intact.
* :class:`CockpitMembershipProjection` — the tolerant use case composing the
  pure policy over the reads port; ``LiveStatusCockpitMembership`` now wires it
  over the live reads, preserving the ``StatusCockpitMembershipPort`` shape.

Residual to #12638 (explicitly carried, not resolved here): the
``return cmd_doctor(args)`` doctor tail stays in ``commands.py`` — the handler
delegates to it as an explicit tail rather than this tranche owning the broad
doctor module. Read-only boundary: no send-keys / paste-buffer routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from mozyo_bridge.application.status_session_port import (
    LiveStatusSession,
    StatusSessionPort,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    AGENT_LABELS,
)

if TYPE_CHECKING:  # avoid an import cycle / heavy import on the hot path
    import argparse

    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
        WorkspaceMembership,
    )


@dataclass(frozen=True)
class StatusQuery:
    """Typed input for the status session read: the session to describe."""

    session: str


@dataclass(frozen=True)
class SessionStatusView:
    """Resolved session-status facts the status handler renders.

    ``present`` is ``False`` for a missing session (the only meaningful field
    then). When present, ``agent_windows`` lists the session's agent-named
    windows in window order; ``has_agent_windows`` gates the pane table;
    ``panes_ok`` / ``panes_text`` carry the capture result; and
    ``missing_agents`` is the sorted set of agent labels with no window.
    """

    session: str
    present: bool
    agent_windows: tuple = ()
    has_agent_windows: bool = False
    panes_ok: bool = False
    panes_text: str = ""
    missing_agents: tuple = ()


class ResolveSessionStatusUseCase:
    """Resolve a session's present/missing agent-window status over a port.

    Owns the read-orchestration + classification ``cmd_status`` previously
    inlined: existence → window enumeration → (only when agent windows exist)
    pane capture, plus the missing-agent computation. Decoupled from live tmux
    via the injected :class:`StatusSessionPort`, so it is unit-testable with a
    fake port (no ``commands.*`` monkeypatch). Behavior-preserving: panes are
    captured only when agent windows are present, exactly as the procedural
    handler did (so a session whose agent windows are all missing issues no
    ``list-panes`` read).
    """

    def __init__(self, sessions: StatusSessionPort) -> None:
        self._sessions = sessions

    def resolve(self, query: StatusQuery) -> SessionStatusView:
        session = query.session
        if not self._sessions.session_exists(session):
            return SessionStatusView(session=session, present=False)

        windows = self._sessions.list_windows(session)
        agent_windows = tuple(name for name in windows if name in AGENT_LABELS)
        if not agent_windows:
            return SessionStatusView(
                session=session,
                present=True,
                agent_windows=(),
                has_agent_windows=False,
            )

        panes_ok, panes_text = self._sessions.capture_panes(session)
        missing = tuple(
            sorted(agent for agent in AGENT_LABELS if agent not in agent_windows)
        )
        return SessionStatusView(
            session=session,
            present=True,
            agent_windows=agent_windows,
            has_agent_windows=True,
            panes_ok=panes_ok,
            panes_text=panes_text,
            missing_agents=missing,
        )


@dataclass(frozen=True)
class StatusCommandRequest:
    """Typed input for the ``status`` command handler.

    Carries only what the handler needs — the resolved session name — so the
    thin ``cmd_status`` adapter resolves the session from ``argparse.Namespace``
    once and stops threading the namespace into the handler / use case.
    """

    session: str


@dataclass(frozen=True)
class StatusReport:
    """Typed result of the ``status`` command handler.

    ``report_text`` is the fully rendered stdout block the handler produced from
    the session view and the cockpit-membership projection, byte-identical to
    what the procedural ``cmd_status`` printed before the doctor tail (including
    the trailing blank line). The thin adapter writes it verbatim
    (``print(report.report_text, end="")``) and then delegates the doctor tail.
    """

    report_text: str


@runtime_checkable
class StatusCockpitMembershipPort(Protocol):
    """The cockpit-membership projection the status handler depends on.

    ``resolve`` returns this repo's cockpit membership record (a
    ``WorkspaceMembership``) or ``None`` when it cannot be determined — the
    handler renders the membership lines only when a record is present, exactly
    as the tolerant procedural projection did.
    """

    def resolve(self) -> "Optional[WorkspaceMembership]":
        ...


@dataclass(frozen=True)
class CockpitMembershipIdentity:
    """Resolved identity inputs for this repo's cockpit-membership lookup.

    The value object the projection threads from the live identity reads (the
    canonical session + the workspace lane) into both the pure match policy and
    the absent-record construction, so neither re-reads the session.
    ``workspace_id`` + ``target_lane`` key the lookup; ``repo_root`` /
    ``lane_label`` / ``fallback_label`` (the canonical session name) seed the
    absent record when this repo is not loaded in the cockpit.
    """

    repo_root: str
    workspace_id: str
    target_lane: str
    lane_label: "Optional[str]" = None
    fallback_label: str = ""


def match_cockpit_membership(
    workspaces, identity: CockpitMembershipIdentity
) -> "Optional[WorkspaceMembership]":
    """Select this repo's loaded cockpit membership record (pure domain policy).

    Picks the workspace whose id matches and whose lane normalizes to the repo's
    ``target_lane`` — the same keyed match the procedural projection inlined,
    lifted to a pure function so it is unit tested with fake workspace records
    (no live cockpit read, no ``commands.*`` monkeypatch). Returns ``None`` when
    this repo is not among the loaded cockpit workspaces; the use case then asks
    the reads port for the absent record.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        normalize_lane,
    )

    return next(
        (
            w
            for w in workspaces
            if w.workspace_id == identity.workspace_id
            and normalize_lane(w.lane_id) == identity.target_lane
        ),
        None,
    )


@runtime_checkable
class StatusCockpitMembershipReads(Protocol):
    """The live cockpit reads the membership projection is driven by.

    Splits the I/O the procedural ``_status_repo_cockpit_membership`` inlined —
    identity resolution, the loaded-cockpit collection, and the absent-record
    construction — behind one injectable port, so
    :class:`CockpitMembershipProjection` is exercised with a fake reads object
    instead of monkeypatching the ``commands.*`` cockpit helpers.
    """

    def resolve_identity(self, repo) -> "CockpitMembershipIdentity":
        ...

    def collect_workspaces(self) -> tuple:
        ...

    def absent_membership(
        self, identity: "CockpitMembershipIdentity"
    ) -> "WorkspaceMembership":
        ...


class CockpitMembershipProjection:
    """Project this repo's cockpit membership over a reads port (tolerant).

    Owns the orchestration the procedural ``_status_repo_cockpit_membership``
    inlined: resolve the repo identity, collect the loaded cockpit workspaces,
    run the pure :func:`match_cockpit_membership` policy, and fall back to the
    port's absent record only when this repo is not loaded (preserving the
    original's "no registry read on a hit"). Tolerant: any read / resolution
    failure degrades to ``None`` so ``status`` never aborts on the projection,
    exactly as the procedural body did.
    """

    def __init__(self, reads: StatusCockpitMembershipReads) -> None:
        self._reads = reads

    def resolve(self, repo) -> "Optional[WorkspaceMembership]":
        try:
            identity = self._reads.resolve_identity(repo)
            match = match_cockpit_membership(self._reads.collect_workspaces(), identity)
            if match is None:
                match = self._reads.absent_membership(identity)
            return match
        except (Exception, SystemExit):
            return None


class LiveStatusCockpitMembershipReads:
    """Live adapter for :class:`StatusCockpitMembershipReads`.

    Implements the reads by routing to the ``commands.*`` cockpit helpers
    (``resolve_canonical_session`` / ``_resolve_workspace_lane`` /
    ``_collect_cockpit_membership`` / ``_resolve_registry_facts``) *at call
    time*, so the existing cockpit monkeypatch / integration tests that patch
    those names keep driving the live path while the projection orchestration
    itself moved out of ``commands.py`` and into
    :class:`CockpitMembershipProjection`.
    """

    def resolve_identity(self, repo) -> "CockpitMembershipIdentity":
        import os
        from pathlib import Path

        from mozyo_bridge.application import commands as _commands
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            normalize_lane,
        )

        repo_root = str(Path(repo or os.getcwd()).expanduser().resolve())
        canon = _commands.resolve_canonical_session(repo_root)
        workspace_id = getattr(canon, "workspace_id", None) or canon.name
        lane = _commands._resolve_workspace_lane(
            repo_root, getattr(canon, "workspace_id", None)
        )
        return CockpitMembershipIdentity(
            repo_root=repo_root,
            workspace_id=workspace_id,
            target_lane=normalize_lane(lane.lane_id),
            lane_label=lane.lane_label,
            fallback_label=canon.name,
        )

    def collect_workspaces(self) -> tuple:
        from mozyo_bridge.application import commands as _commands
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            COCKPIT_SESSION_DEFAULT,
        )

        return _commands._collect_cockpit_membership(COCKPIT_SESSION_DEFAULT).workspaces

    def absent_membership(
        self, identity: "CockpitMembershipIdentity"
    ) -> "WorkspaceMembership":
        from mozyo_bridge.application import commands as _commands
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            infer_repo_root,
        )
        from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
            absent_membership,
        )
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            COCKPIT_SESSION_DEFAULT,
        )

        facts = _commands._resolve_registry_facts(identity.workspace_id)
        label = facts.label if facts.registry_present else identity.fallback_label
        return absent_membership(
            session=COCKPIT_SESSION_DEFAULT,
            workspace_id=identity.workspace_id,
            label=label,
            repo_root=infer_repo_root(identity.repo_root) or identity.repo_root,
            lane_id=identity.target_lane,
            lane_label=identity.lane_label,
            registry_present=facts.registry_present,
            anchor_present=facts.anchor_present,
            registry_canonical_path=facts.repo_root,
        )


class LiveStatusCockpitMembership:
    """Live adapter for the :class:`StatusCockpitMembershipPort`.

    Composes :class:`CockpitMembershipProjection` over the live
    :class:`LiveStatusCockpitMembershipReads`, resolving the repo from the
    ``argparse.Namespace`` once. (#12830 decomposed the former
    ``commands._status_repo_cockpit_membership`` projection body into the
    projection / reads-port / identity value object here; this class keeps the
    same ``StatusCockpitMembershipPort`` shape so ``StatusCommandHandler`` and
    the live ``cmd_status`` wiring are unchanged, and the live reads still route
    through ``commands.*`` at call time so the cockpit tests are intact.)
    """

    def __init__(self, args: "argparse.Namespace") -> None:
        self._args = args

    def resolve(self) -> "Optional[WorkspaceMembership]":
        return CockpitMembershipProjection(LiveStatusCockpitMembershipReads()).resolve(
            getattr(self._args, "repo", None)
        )


def render_status_report(
    view: SessionStatusView, membership: "Optional[WorkspaceMembership]"
) -> str:
    """Render the ``status`` stdout block (pure, byte-preserving).

    Reproduces exactly what the procedural ``cmd_status`` printed before the
    doctor tail: the session header, the agent-pane table (or the no-agent /
    missing notes), the cockpit-membership lines when a record is present, and
    the trailing blank line. Side-effect free, so the command handler is unit
    tested by asserting on the returned string rather than scraping stdout.
    """
    out = []
    if view.present:
        out.append(f"session: {view.session}\n")
        if view.has_agent_windows:
            out.append("WINDOW\tNAME\tTARGET\tACTIVE\tPROCESS\tCWD\n")
            if view.panes_ok:
                # ``panes_text`` already carries tmux's trailing newlines; emit
                # it raw (the old handler printed it with ``end=""``).
                out.append(view.panes_text)
            for agent in view.missing_agents:
                out.append(
                    f"  {agent} window missing; run `mozyo` to create it, "
                    f"or `mozyo-bridge init {agent}` from the right pane to rename it.\n"
                )
        else:
            out.append(
                "  no agent windows in this session. "
                "Run `mozyo` from the repo to create one window per agent, "
                "or `mozyo-bridge init claude|codex` from an existing pane to rename "
                "its window into an agent target.\n"
            )
    else:
        out.append(f"session: {view.session} (missing)\n")

    if membership is not None:
        # Imported lazily (as the procedural handler did) to keep this leaf off
        # the e_120 cockpit import on the status hot path.
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            COCKPIT_SESSION_DEFAULT,
        )

        if membership.member:
            out.append(
                f"cockpit: workspace {membership.label!r} IS loaded in cockpit "
                f"{COCKPIT_SESSION_DEFAULT!r} (window {membership.window or '-'}, "
                f"codex={membership.codex_pane or '-'} "
                f"claude={membership.claude_pane or '-'}, "
                f"geometry={membership.geometry_status}); see "
                "`mozyo-bridge cockpit status --repo .`.\n"
            )
        else:
            out.append(
                f"cockpit: workspace {membership.label!r} is NOT loaded in cockpit "
                f"{COCKPIT_SESSION_DEFAULT!r}; any `agent window missing` note above "
                "is about this normal session, not cockpit membership. Add it with "
                "`mozyo cockpit`, or inspect with `mozyo-bridge cockpit list`.\n"
            )
        out.append(
            "  (cockpit membership is a display/liveness projection, not Redmine "
            "workflow / approval / close truth.)\n"
        )
    out.append("\n")
    return "".join(out)


class StatusCommandHandler:
    """Command handler for ``status`` — typed request in, typed report out.

    Composes the session-read use case (over a :class:`StatusSessionPort`), the
    cockpit-membership projection (over a :class:`StatusCockpitMembershipPort`),
    and the pure :func:`render_status_report` into one ``handle`` step. It owns
    no stdout and no ``argparse.Namespace``; the thin ``cmd_status`` adapter
    prints the result and runs the residual doctor tail.
    """

    def __init__(
        self,
        sessions: Optional[StatusSessionPort] = None,
        membership: Optional[StatusCockpitMembershipPort] = None,
    ) -> None:
        self._sessions = sessions if sessions is not None else LiveStatusSession()
        self._membership = membership

    def handle(self, request: StatusCommandRequest) -> StatusReport:
        view = ResolveSessionStatusUseCase(self._sessions).resolve(
            StatusQuery(session=request.session)
        )
        membership = self._membership.resolve() if self._membership is not None else None
        return StatusReport(report_text=render_status_report(view, membership))
