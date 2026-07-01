"""Cockpit adopt boundary: observation projection + advisory + resolver + handler (#12987).

Four helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, each mixing a *side effect* (the live
cross-workspace session inventory read, the ``list-clients`` source-client
probe, the confirm-gated pane move) with *pure decision logic* (projecting
inventory records into adopt observations, the fail-closed adopt gate chain,
and the CLI preview / json / confirm output selection):

- ``_coexisting_normal_observations`` — project the live inventory into the
  normal-``mozyo`` adopt observations the detector consumes (#11897).
- ``_cockpit_adopt_advisory`` — wrap the pure ``detect_adopt_candidates`` over
  that projection; tolerant (a benign ``none`` advisory on any failure).
- ``_resolve_cockpit_adopt`` — the fail-closed adopt decision: plan only for a
  single unambiguous fully-paired candidate with an anchorable cockpit and no
  attached source client (#11898).
- ``_handle_cockpit_adopt`` — the ``mozyo cockpit adopt`` handler: detect-only
  preview vs ``--confirm``-gated atomic move, ``--json`` / ``--dry-run`` always
  non-mutating.

This module carves that into an OOP-first boundary under #12638, the direct
analog of the #12978 peer-adopt carve (``cockpit_peer_adopt_command.py``):

- :func:`project_normal_session_observations` is the pure inventory-record
  projection: the agent-kind / cockpit-session / role-source filters, the
  privacy-safe workspace-id fallback chain (Redmine #11897 review j#57857), and
  the per-repo-root lane cache, with the lane resolution injected and no
  :mod:`commands` dependency.
- :class:`CockpitAdoptOps` is the port for the side effects the use case needs —
  the inventory snapshot, the lane resolution, the advisory seam, the
  source-client probe, the rightmost-codex-anchor pick, the tmux availability
  gate, the #11898 adopt-plan executor, the source-session cleanup report, the
  fail-closed abort, and the stdout line sink. :class:`LiveCockpitAdoptOps` is
  the live adapter: it resolves every target *through the* :mod:`commands`
  *module at call time* (never at import), so the characterization tests that
  patch ``commands._cockpit_adopt_advisory`` / ``commands._session_attached_clients``
  / ``commands._resolve_workspace_lane`` / ``commands.require_tmux`` /
  ``commands.run_tmux`` / ``commands.session_exists`` keep intercepting, and
  this module never imports :mod:`commands` at module scope (no import cycle).
  The inventory read imports ``take_inventory`` from its source module at call
  time, preserving the ``mozyo_bridge.session_inventory.take_inventory`` patch
  seam the integration tests use.
- :class:`CockpitAdoptUseCase` composes the port and the pure domain
  (``detect_adopt_candidates`` / ``adopt_pane_pair`` /
  ``build_cockpit_adopt_plan``) into the observation / advisory / resolve
  methods and the confirm-gated :meth:`handle`. Unlike the peer-adopt outcome
  shape, :meth:`handle` renders through the port's ``emit`` sink: the confirm
  path prints its header *before* the atomic move and the results after it, so
  a rendered-text outcome would change what the operator sees if the move
  fails mid-way — the sink preserves the original interleaving byte-for-byte.

The thin ``_coexisting_normal_observations`` / ``_cockpit_adopt_advisory`` /
``_resolve_cockpit_adopt`` / ``_handle_cockpit_adopt`` wrappers stay in
:mod:`commands` with unchanged signatures, so the ``cmd_cockpit`` adopt
dispatch and every ``commands.*`` patch seam are preserved. Behavior-preserving:
the fail-closed guard order, the tolerance shapes (inventory failure -> ``[]``,
advisory failure -> benign ``none``), the preview / json / confirm output and
exit conventions are unchanged from the original command bodies. This module
reuses the #11898 ``execute_cockpit_adopt_plan`` executor via the port; it
never reimplements executor semantics.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable


# --- Pure projection: inventory records -> normal-session adopt observations. --


def project_normal_session_observations(
    records: Iterable[Any],
    *,
    cockpit_session: str,
    resolve_lane: Any,
) -> list:
    """Project inventory records into normal-``mozyo`` adopt observations (#11897).

    Keeps only panes that are a *normal* ``mozyo`` agent for the adopt detector —
    a classified codex/claude pane whose role came from the window name
    (``role_source == window_name``), living outside the cockpit session.
    Cockpit panes carry the role on ``@mozyo_agent_role``
    (``role_source == pane_option``) and are excluded so a cockpit column never
    looks like an adopt source. ``resolve_lane(repo_root, workspace_id)`` is the
    injected lane resolution, invoked once per distinct repo root. Pure over the
    records + resolver (no tmux / inventory / registry I/O of its own).
    """
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
        AGENT_KIND_CLAUDE,
        AGENT_KIND_CODEX,
        ROLE_SOURCE_WINDOW_NAME,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        NormalSessionObservation,
    )

    lane_cache: dict[str, object] = {}
    observations = []
    for rec in records:
        if rec.agent_kind not in (AGENT_KIND_CODEX, AGENT_KIND_CLAUDE):
            continue
        if rec.session == cockpit_session:
            continue
        if rec.role_source != ROLE_SOURCE_WINDOW_NAME:
            continue
        # Mirror the target's identity fallback EXACTLY (Redmine #11897 review
        # j#57857): `cmd_cockpit` uses `canon.workspace_id or canon.name`, where
        # `canon.name` is the registry→anchor→derivation canonical session. The
        # inventory resolves identity through the *same* chain, so an unregistered
        # workspace (no registry row / anchor) has `workspace_id=None` but a
        # matching `canonical_session`. Falling back to the raw `repo_root` here
        # instead would never match that `canon.name` (detection silently fails)
        # and would also leak an absolute path as the match key. Prefer the
        # privacy-safe `canonical_session`; repo_root/session are last-resort only.
        workspace_id = (
            (rec.workspace.workspace_id if rec.workspace else None)
            or (rec.workspace.canonical_session if rec.workspace else None)
            or rec.repo_root
            or rec.session
        )
        repo_root = rec.repo_root or ""
        if repo_root not in lane_cache:
            lane_cache[repo_root] = resolve_lane(
                repo_root, rec.workspace.workspace_id if rec.workspace else None
            )
        lane = lane_cache[repo_root]
        observations.append(
            NormalSessionObservation(
                session=rec.session,
                workspace_id=workspace_id,
                lane_id=lane.lane_id,
                role=rec.agent_kind,
                pane_id=rec.pane_id,
            )
        )
    return observations


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitAdoptOps(Protocol):
    """Port: the side effects the cockpit-adopt use case needs from its environment.

    ``take_inventory`` is the live cross-workspace snapshot (may raise — the use
    case owns the tolerance); ``resolve_workspace_lane`` is the per-repo lane
    resolution; ``adopt_advisory`` is the detect seam (routed through
    ``commands._cockpit_adopt_advisory`` so the handler tests keep patching it);
    ``session_attached_clients`` / ``rightmost_codex_anchor`` feed the fail-closed
    resolver; ``require_tmux`` gates the mutable apply; ``execute_adopt`` runs the
    #11898 executor; ``source_session_cleanup_note`` reports the post-move source
    state; ``die`` is the fail-closed abort; ``emit`` is the stdout line sink
    (the handler's print stream — injected so a fake can capture the exact
    line interleaving around the atomic move).
    """

    def take_inventory(self) -> Any: ...

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any: ...

    def adopt_advisory(self, workspace: Any, cockpit_session: str) -> Any: ...

    def session_attached_clients(self, session: str) -> tuple: ...

    def rightmost_codex_anchor(self, codex_columns: Any) -> Any: ...

    def require_tmux(self) -> None: ...

    def execute_adopt(self, plan: Any) -> dict: ...

    def source_session_cleanup_note(self, source_session: str) -> str: ...

    def die(self, message: str) -> None: ...

    def emit(self, text: str) -> None: ...


class LiveCockpitAdoptOps:
    """Live :class:`CockpitAdoptOps` over the real ``commands`` seams.

    Every method resolves its target *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the tests that patch
    ``commands._cockpit_adopt_advisory`` / ``commands._session_attached_clients``
    / ``commands._resolve_workspace_lane`` / ``commands._rightmost_codex_anchor``
    / ``commands.require_tmux`` / ``commands.run_tmux`` keep intercepting. The
    inventory read imports ``take_inventory`` from
    :mod:`mozyo_bridge.session_inventory` at call time (the integration tests
    patch it at that source), with the original read-only arguments.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def take_inventory(self) -> Any:
        from mozyo_bridge.session_inventory import take_inventory

        return take_inventory(derive_unregistered=False, persist=False)

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any:
        return self._commands()._resolve_workspace_lane(repo_root, workspace_id)

    def adopt_advisory(self, workspace: Any, cockpit_session: str) -> Any:
        return self._commands()._cockpit_adopt_advisory(workspace, cockpit_session)

    def session_attached_clients(self, session: str) -> tuple:
        return self._commands()._session_attached_clients(session)

    def rightmost_codex_anchor(self, codex_columns: Any) -> Any:
        return self._commands()._rightmost_codex_anchor(codex_columns)

    def require_tmux(self) -> None:
        return self._commands().require_tmux()

    def execute_adopt(self, plan: Any) -> dict:
        commands = self._commands()
        return commands.execute_cockpit_adopt_plan(plan, commands.run_tmux)

    def source_session_cleanup_note(self, source_session: str) -> str:
        return self._commands()._source_session_cleanup_note(source_session)

    def die(self, message: str) -> None:
        return self._commands().die(message)

    def emit(self, text: str) -> None:
        print(text)


# --- Use case: compose the port + pure domain into the adopt flows. ------------


class CockpitAdoptUseCase:
    """Detect / resolve / preview / apply a cockpit adopt through the injected port.

    Composes the inventory projection, the pure detector
    (``detect_adopt_candidates``), the fail-closed resolver, and the
    confirm-gated handler. Byte-for-byte equivalent to the original command
    bodies, including the tolerance shapes (inventory failure -> ``[]``,
    advisory failure -> benign ``none`` advisory) and the preview / json /
    confirm output and exit conventions.
    """

    def __init__(self, ops: CockpitAdoptOps) -> None:
        self._ops = ops

    def coexisting_normal_observations(self, cockpit_session: str) -> list:
        """Project the live inventory into normal-``mozyo`` adopt observations (#11897).

        Tolerant and read-only: any inventory failure (no tmux, inventory error)
        degrades to ``[]`` so the advisory can never break the cockpit flow.
        """
        try:
            snapshot = self._ops.take_inventory()
        except Exception:
            return []
        return project_normal_session_observations(
            snapshot.records,
            cockpit_session=cockpit_session,
            resolve_lane=self._ops.resolve_workspace_lane,
        )

    def adopt_advisory(self, workspace: Any, cockpit_session: str) -> Any:
        """Detect a co-existing normal ``mozyo`` session for ``workspace`` (#11897).

        Wraps the pure ``detect_adopt_candidates`` over the live inventory
        projection. Read-only and tolerant: it never moves a pane and always
        returns an ``AdoptAdvisory`` (a benign ``none`` advisory when nothing is
        found or the inventory is unavailable).
        """
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            ADOPT_STATUS_NONE,
            AdoptAdvisory,
            detect_adopt_candidates,
            normalize_lane,
        )

        lane_id = normalize_lane(workspace.lane_id)
        try:
            return detect_adopt_candidates(
                workspace_id=workspace.workspace_id,
                lane_id=workspace.lane_id,
                observations=self.coexisting_normal_observations(cockpit_session),
                cockpit_session=cockpit_session,
            )
        except Exception:
            return AdoptAdvisory(
                workspace.workspace_id, lane_id, ADOPT_STATUS_NONE, (), None
            )

    def resolve_adopt(
        self,
        workspace: Any,
        session: str,
        *,
        columns: Any,
        session_present: bool,
        already_in_cockpit: bool,
        existing_codex: Any,
        advisory: Any,
        codex_ratio: int = 70,
    ) -> tuple:
        """Decide the adopt move for ``mozyo cockpit adopt`` — plan or fail-closed (#11898).

        Returns ``(plan, blocked_reason, source_clients)``. ``plan`` is a
        ``CockpitAdoptPlan`` only when there is a single, unambiguous, fully
        paired adopt candidate, the cockpit already exists with a column to
        anchor on, and the source session has no attached client. Every
        fail-closed condition (already a column / not a clean candidate /
        role→pane ambiguous / no cockpit yet / attached client / no anchor)
        yields a ``blocked_reason`` and ``plan is None`` — the move is never
        planned past a closed gate.
        """
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            ADOPT_STATUS_CANDIDATE,
            adopt_pane_pair,
            build_cockpit_adopt_plan,
        )

        if already_in_cockpit:
            return None, (
                "this workspace+lane is already a cockpit column; focus it with "
                "`mozyo cockpit` — nothing to adopt."
            ), ()
        if advisory.status != ADOPT_STATUS_CANDIDATE:
            return None, (
                advisory.message
                or "no adoptable co-existing normal `mozyo` session for this "
                "workspace+lane."
            ), ()

        candidate = advisory.candidates[0]
        pair = adopt_pane_pair(candidate)
        if pair is None:
            return None, (
                f"adopt candidate {candidate.session!r} does not map exactly one "
                f"codex and one claude pane (roles={','.join(candidate.roles) or '-'}, "
                f"panes={','.join(candidate.pane_ids) or '-'}); the role→pane pairing "
                f"is ambiguous and fails closed."
            ), ()

        if not session_present or columns is None:
            return None, (
                f"cockpit session {session!r} does not exist yet (or has no cockpit "
                f"window); create it first with `mozyo cockpit` (or `mozyo layout "
                f"apply cockpit`), then re-run `mozyo cockpit adopt`. Bootstrapping a "
                f"cockpit from the moved panes is out of this Phase 2 scope."
            ), ()

        source_clients = self._ops.session_attached_clients(candidate.session)
        if source_clients:
            return None, (
                f"source session {candidate.session!r} has attached client(s) "
                f"({', '.join(source_clients)}); detach it before adopting so its "
                f"panes are not moved out from under a live client (fail-closed)."
            ), source_clients

        anchor = self._ops.rightmost_codex_anchor(existing_codex)
        if not anchor:
            return None, (
                f"cockpit session {session!r} exists but carries no mozyo-identified "
                f"codex column to anchor the adopted column beside; rebuild it with "
                f"`mozyo layout apply cockpit` or remove the stale session."
            ), ()

        codex_pane, claude_pane = pair
        plan = build_cockpit_adopt_plan(
            workspace,
            source_session=candidate.session,
            source_codex_pane=codex_pane,
            source_claude_pane=claude_pane,
            anchor_pane=anchor,
            column_index=len(existing_codex),
            codex_ratio=codex_ratio,
            session=session,
        )
        return plan, None, source_clients

    def handle(
        self,
        args: Any,
        workspace: Any,
        session: str,
        *,
        columns: Any,
        session_present: bool,
        already_in_cockpit: bool,
        existing_codex: Any,
    ) -> int:
        """Route ``mozyo cockpit adopt`` — detect-only preview vs confirm-gated move (#11898).

        Phase 2 keeps the Phase 1 safety default: with no ``--confirm`` the
        command is read-only (detect + preview the plan, move no panes).
        ``--json`` and ``--dry-run`` stay non-mutating previews even with
        ``--confirm`` (the codebase contract: those flags never run tmux). Only
        an explicit ``--confirm`` (and not ``--dry-run`` / ``--json``) executes
        the atomic move. All lines render through the port's ``emit`` sink so
        the confirm path's header/results interleaving around the move is
        preserved exactly.
        """
        import json as _json
        import shlex as _shlex

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            normalize_lane,
        )

        confirm = bool(getattr(args, "confirm", False))
        json_output = bool(getattr(args, "json_output", False))
        dry_run = bool(getattr(args, "dry_run", False))
        lane_id = normalize_lane(workspace.lane_id)
        codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)
        advisory = self._ops.adopt_advisory(workspace, session)
        plan, blocked, source_clients = self.resolve_adopt(
            workspace, session, columns=columns, session_present=session_present,
            already_in_cockpit=already_in_cockpit, existing_codex=existing_codex,
            advisory=advisory, codex_ratio=codex_ratio,
        )

        if json_output:
            payload = {
                "command": "cockpit adopt",
                "phase": 2,
                # This invocation never runs tmux (json is a preview surface); a
                # confirm-run would execute only when a plan exists.
                "executes": False,
                "would_execute": bool(confirm and plan is not None),
                "confirm": confirm,
                "session": session,
                "workspace_id": workspace.workspace_id,
                "lane_id": lane_id,
                "lane_label": workspace.lane_label,
                "already_in_cockpit": already_in_cockpit,
                "source_clients": list(source_clients),
                "blocked": blocked,
                "plan": plan.as_dict() if plan is not None else None,
                "advisory": advisory.as_dict(),
            }
            self._ops.emit(
                _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0

        # Confirm-gated execution: the only path that moves panes. `--dry-run`
        # outranks `--confirm` as a safe preview, so it is handled below, not here.
        if confirm and not dry_run:
            if plan is None:
                self._ops.die(blocked or "nothing to adopt for this workspace+lane.")
            self._ops.require_tmux()
            self._ops.emit(
                f"cockpit adopt: moving normal session {plan.source_session!r} "
                f"(codex {plan.source_codex_pane} + claude {plan.source_claude_pane}) "
                f"into cockpit {session!r} as column {plan.column_index} "
                f"({workspace.label}, lane={lane_id})"
            )
            result = self._ops.execute_adopt(plan)
            self._ops.emit(
                f"  adopted: {workspace.label!r} is now a cockpit column; switch to "
                f"the cockpit window to see it (no new iTerm window opened)."
            )
            self._ops.emit(
                f"  {self._ops.source_session_cleanup_note(plan.source_session)}"
            )
            for warning in result.get("stamp_warnings", []):
                self._ops.emit(f"  warning: identity re-stamp incomplete — {warning}")
            return 0

        # Detect-only / preview (bare adopt or `--dry-run`): report and, when a clean
        # candidate exists, show the exact move `--confirm` would run. No mutation.
        self._ops.emit(
            f"cockpit adopt (preview; no panes moved): session={session} "
            f"workspace={workspace.workspace_id} ({workspace.label}) lane={lane_id}"
        )
        for candidate in advisory.candidates:
            self._ops.emit(
                f"  candidate: session={candidate.session} "
                f"roles={','.join(candidate.roles) or '-'} "
                f"panes={','.join(candidate.pane_ids) or '-'}"
            )
        if advisory.message:
            self._ops.emit(f"  {advisory.message}")
        if plan is not None:
            self._ops.emit(
                f"  adopt plan: move {plan.source_codex_pane} (codex) + "
                f"{plan.source_claude_pane} (claude) from {plan.source_session!r} "
                f"into cockpit column {plan.column_index}:"
            )
            for cmd in plan.commands:
                self._ops.emit(
                    "    tmux " + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                )
            self._ops.emit("  run `mozyo cockpit adopt --confirm` to execute this move.")
        elif blocked:
            self._ops.emit(f"  cannot adopt: {blocked}")
        elif not advisory.has_candidates:
            self._ops.emit(
                "  no co-existing normal `mozyo` session found for this workspace+lane."
            )
        return 0
