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

#12831 (this tranche) isolates the ``cmd_status`` -> ``cmd_doctor(args)`` tail
delegation that #12830 carried as a #12638 residual. The doctor tail is the
status command's *continuation*: after the status block is rendered, the
command's exit code is the doctor command's. That continuation is now a typed
boundary owned here —

* :class:`StatusContinuationResult` — the value object carrying the doctor
  tail's ``exit_code``, so the status command's exit code is a typed result
  rather than a bare ``int`` threaded out of a free-function call.
* :class:`StatusDoctorContinuation` / :class:`LiveStatusDoctorContinuation` —
  the doctor continuation behind one injectable port; the live adapter owns the
  ``argparse.Namespace`` and routes to ``commands.cmd_doctor`` *at call time*,
  so the existing ``test_cmd_status_*`` integration tests that patch
  ``commands.run_doctor`` / ``commands.format_doctor_text`` still drive the live
  doctor while the status command no longer hands the namespace to a naked
  ``cmd_doctor(args)`` call.
* :class:`StatusCommandHandler` now owns the continuation as a collaborator and
  exposes :meth:`StatusCommandHandler.continue_with_doctor`, so the doctor tail
  is driven by a fake continuation in unit tests instead of monkeypatching the
  ``commands.*`` doctor helpers. The thin ``cmd_status`` adapter renders +
  prints the report, then runs the continuation (preserving the stdout order:
  status block first, doctor output second).

Residual to #12638 (explicitly carried, not resolved here): the broad
``cmd_doctor`` / ``run_doctor`` body still lives in ``commands.py`` /
``doctor.py``; this tranche cuts only the status->doctor continuation boundary,
not the doctor command's own decomposition. Read-only boundary: no send-keys /
paste-buffer routing.
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.default_agent_topology import (
    DEFAULT_EXPECTED_AGENTS,
)

if TYPE_CHECKING:  # avoid an import cycle / heavy import on the hot path
    import argparse

    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_provider_runtime_snapshot import (  # noqa: E501
        AgentProviderRuntimeSnapshot,
    )

    from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.cockpit_membership import (
        WorkspaceMembership,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
        HerdrInventoryView,
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

    def resolve(
        self,
        query: StatusQuery,
        *,
        expected_providers: "tuple[str, ...] | None" = None,
        known_providers: "AgentProviderRuntimeSnapshot | None" = None,
    ) -> SessionStatusView:
        # `expected_providers` is the topology the session is expected to run (Redmine
        # #13569 known-vs-expected split); it defaults to the built-in launch pair and is
        # a SEPARATE input from the known registry vocabulary used for recognition.
        expected = (
            DEFAULT_EXPECTED_AGENTS if expected_providers is None else expected_providers
        )
        # `known_providers` is the injected agent-provider snapshot used for RECOGNITION
        # (Redmine #13569 R1-F1). ``None`` uses the built-in ``AGENT_LABELS``, byte-
        # identical; the composition / a test injects a snapshot so a synthetic provider's
        # window is recognized without an import-time global.
        known = AGENT_LABELS if known_providers is None else known_providers.provider_ids
        session = query.session
        if not self._sessions.session_exists(session):
            return SessionStatusView(session=session, present=False)

        windows = self._sessions.list_windows(session)
        # Recognition uses the KNOWN providers (the registry vocabulary): any observed
        # window whose name is a recognized provider is an agent window.
        agent_windows = tuple(name for name in windows if name in known)
        if not agent_windows:
            return SessionStatusView(
                session=session,
                present=True,
                agent_windows=(),
                has_agent_windows=False,
            )

        panes_ok, panes_text = self._sessions.capture_panes(session)
        # "Missing" is judged against the EXPECTED topology, NOT the full registry
        # (Redmine #13569 known-vs-expected split): a profile-only provider that is
        # recognizable but not part of the default launch pair must never be reported
        # missing. For the built-in providers the expected set equals the known set, so
        # this is byte-identical to the previous behavior.
        missing = tuple(
            sorted(agent for agent in expected if agent not in agent_windows)
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


@runtime_checkable
class StatusHerdrBackendPort(Protocol):
    """The herdr backend inventory projection the status handler depends on (#13355).

    ``resolve`` returns the shared :class:`HerdrInventoryView` when the target
    repo selects the herdr terminal backend, or ``None`` otherwise — the handler
    renders the herdr backend block only for a non-None view, so the
    ``backend: tmux`` status output stays byte-for-byte unchanged (the #13317
    all-backend-rows posture: herdr rows join the report, tmux rows are never
    replaced).
    """

    def resolve(self) -> "Optional[HerdrInventoryView]":
        ...


class LiveStatusHerdrBackend:
    """Live adapter for :class:`StatusHerdrBackendPort`.

    Drives the one shared
    :func:`~mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider
    .application.herdr_observability.read_herdr_inventory` read model against the
    ``--repo`` target (default cwd) *at call time*, so decode / status-mapping
    semantics never drift from the doctor herdr section. Backend selection is
    checked inside the read model: a repo that does not select herdr resolves to
    ``None`` here with no herdr read performed at all. A selected-but-unreadable
    inventory (server down / binary unconfigured) resolves to a fail-closed view
    the renderer surfaces — status never crashes on a broken herdr transport.
    """

    def __init__(self, args: "argparse.Namespace") -> None:
        self._args = args

    def resolve(self) -> "Optional[HerdrInventoryView]":
        from mozyo_bridge.application.commands_common import repo_root_from_args
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
            read_herdr_inventory,
        )

        view = read_herdr_inventory(repo_root_from_args(self._args))
        return view if view.backend_selected else None


def render_herdr_status_block(view: "HerdrInventoryView") -> str:
    """Render the herdr backend block of the ``status`` report (pure, #13355).

    One header line, then a tab-separated row per observed managed agent
    (workspace / lane / role / agent runtime state / transient locator / durable
    name), mirroring the tmux pane table's shape. Fail-closed: an unreadable
    inventory renders an explicit failure line instead of rows, and the block
    always carries the runtime-observation boundary note (a herdr row is
    liveness display, never workflow truth).
    """
    out = []
    if not view.ok:
        out.append(
            "herdr: backend selected but the live inventory is unreadable "
            f"(fail-closed, {view.reason or '-'}): {view.detail or '-'}\n"
        )
        out.append(
            "  run `mozyo-bridge doctor` for the herdr server / binary diagnosis.\n"
        )
        return "".join(out)
    managed = view.managed_agents
    unmanaged = view.unmanaged_agents
    header = (
        f"herdr: backend selected; {len(managed)} managed agent(s) "
        f"(workspace {view.workspace_segment or '-'})"
    )
    if unmanaged:
        header += f"; {len(unmanaged)} unmanaged row(s) not shown (see doctor)"
    out.append(header + "\n")
    if managed:
        out.append("WORKSPACE\tLANE\tROLE\tAGENT_STATUS\tLOCATOR\tNAME\n")
        for agent in managed:
            out.append(
                f"{agent.workspace_id}\t{agent.lane_id}\t{agent.role}\t"
                f"{agent.runtime_state}\t{agent.locator or '-'}\t{agent.name}\n"
            )
    out.append(
        "  (herdr agent rows are a runtime observation, not Redmine workflow / "
        "approval / close truth.)\n"
    )
    return "".join(out)


def render_status_report(
    view: SessionStatusView,
    membership: "Optional[WorkspaceMembership]",
    herdr: "Optional[HerdrInventoryView]" = None,
) -> str:
    """Render the ``status`` stdout block (pure, byte-preserving).

    Reproduces exactly what the procedural ``cmd_status`` printed before the
    doctor tail: the session header, the agent-pane table (or the no-agent /
    missing notes), the cockpit-membership lines when a record is present, and
    the trailing blank line. ``herdr`` (#13355) appends the herdr backend block
    when the repo selects the herdr backend; the default ``None`` keeps every
    pre-existing call site and the tmux-backend output byte-identical.
    Side-effect free, so the command handler is unit tested by asserting on the
    returned string rather than scraping stdout.
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
    # herdr backend block (#13355): appended only when the repo selects the
    # herdr backend (herdr is not None), so tmux-backend output is unchanged.
    if herdr is not None:
        out.append(render_herdr_status_block(herdr))
    out.append("\n")
    return "".join(out)


@dataclass(frozen=True)
class StatusContinuationResult:
    """Typed result of the ``status`` command's doctor-tail continuation.

    ``cmd_status`` renders its block and then defers its exit code to the doctor
    command (``return cmd_doctor(args)``). That deferred exit code is carried
    here as a value object, so the status command's contract is "render, then
    yield this continuation's ``exit_code``" rather than a bare ``int`` returned
    from a naked free-function call.
    """

    exit_code: int


@runtime_checkable
class StatusDoctorContinuation(Protocol):
    """The doctor-tail continuation the status command defers its exit to.

    ``run`` executes the doctor command (which prints the doctor block to
    stdout) and returns a :class:`StatusContinuationResult` carrying its exit
    code. Behind a port so the status handler is driven by a fake continuation
    in unit tests instead of monkeypatching the ``commands.*`` doctor helpers.
    """

    def run(self) -> "StatusContinuationResult":
        ...


class LiveStatusDoctorContinuation:
    """Live adapter for :class:`StatusDoctorContinuation`.

    Owns the ``argparse.Namespace`` and routes to ``commands.cmd_doctor`` *at
    call time*, so the existing ``test_cmd_status_*`` integration tests that
    patch ``commands.run_doctor`` / ``commands.format_doctor_text`` still drive
    the live doctor through this continuation, while the status command no longer
    hands the namespace to a bare ``cmd_doctor(args)`` call. Read-only boundary:
    the doctor command's body is unchanged; only the tail delegation moved behind
    this port.
    """

    def __init__(self, args: "argparse.Namespace") -> None:
        self._args = args

    def run(self) -> StatusContinuationResult:
        from mozyo_bridge.application import commands as _commands

        return StatusContinuationResult(exit_code=_commands.cmd_doctor(self._args))


class StatusCommandHandler:
    """Command handler for ``status`` — typed request in, typed report out.

    Composes the session-read use case (over a :class:`StatusSessionPort`), the
    cockpit-membership projection (over a :class:`StatusCockpitMembershipPort`),
    and the pure :func:`render_status_report` into one ``handle`` step. It owns
    no stdout and no ``argparse.Namespace``; the thin ``cmd_status`` adapter
    prints the result and then runs the doctor-tail continuation.

    The doctor tail is held as the :class:`StatusDoctorContinuation` collaborator
    and exposed via :meth:`continue_with_doctor`, so the status command's exit
    code is a typed continuation result the adapter runs *after* printing the
    report (preserving the stdout order). ``handle`` never touches the
    continuation, so rendering stays side-effect free.
    """

    def __init__(
        self,
        sessions: Optional[StatusSessionPort] = None,
        membership: Optional[StatusCockpitMembershipPort] = None,
        continuation: Optional[StatusDoctorContinuation] = None,
        herdr: Optional[StatusHerdrBackendPort] = None,
    ) -> None:
        self._sessions = sessions if sessions is not None else LiveStatusSession()
        self._membership = membership
        self._continuation = continuation
        self._herdr = herdr

    def handle(self, request: StatusCommandRequest) -> StatusReport:
        view = ResolveSessionStatusUseCase(self._sessions).resolve(
            StatusQuery(session=request.session)
        )
        membership = self._membership.resolve() if self._membership is not None else None
        herdr = self._herdr.resolve() if self._herdr is not None else None
        return StatusReport(report_text=render_status_report(view, membership, herdr))

    def continue_with_doctor(self) -> StatusContinuationResult:
        """Run the doctor-tail continuation and return its typed result.

        Raises ``RuntimeError`` when no continuation was injected — the live
        ``cmd_status`` adapter always provides a
        :class:`LiveStatusDoctorContinuation`; render-only handler tests that
        never call this construct the handler without one.
        """
        if self._continuation is None:
            raise RuntimeError("StatusCommandHandler has no doctor continuation")
        return self._continuation.run()


def live_status_handler(args: "argparse.Namespace") -> StatusCommandHandler:
    """Compose the live ``status`` command handler (the ``cmd_status`` wiring).

    Owns the live-collaborator construction — session reads, cockpit-membership
    projection, doctor-tail continuation, and the #13355 herdr backend port — so
    the thin ``cmd_status`` adapter in ``commands.py`` stays a fixed-size
    composition root (module-health gate). The live adapters keep resolving the
    ``commands.*`` seams at call time, so the existing monkeypatch integration
    tests are unchanged.
    """
    return StatusCommandHandler(
        sessions=LiveStatusSession(),
        membership=LiveStatusCockpitMembership(args),
        continuation=LiveStatusDoctorContinuation(args),
        herdr=LiveStatusHerdrBackend(args),
    )
