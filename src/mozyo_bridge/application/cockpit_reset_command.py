"""Cockpit reset/rebuild boundary: grade read + confirm-gated handler (#12989).

Four helpers historically lived as procedural bodies in
:mod:`mozyo_bridge.application.commands`, mixing the *side effects* (the
attached-client / window-list reads behind the reset grade, the destructive
``kill-session`` teardown, the rebuild create, the terminal attach) with *pure
decision / rendering logic* (the extra-window enumeration, the target
inventory report, and the preview / json / confirm output selection):

- ``_assess_cockpit_reset`` — read the extra runtime facts (attached clients +
  session windows) and hand them to the pure ``assess_cockpit_reset`` grader
  (#11814); a failed client read stays fail-closed via
  ``attached_clients_known`` (review j#57928).
- ``_cockpit_extra_windows`` — the managed-session windows a reset's
  ``kill-session`` destroys beyond the ``cockpit`` home window (#12330).
- ``_print_cockpit_reset_inventory`` — the session / window / pane inventory a
  reset/rebuild would act on (retired as a ``commands`` name by this carve; the
  rendering lives here as :func:`render_reset_inventory_lines`).
- ``_handle_cockpit_reset`` — the ``mozyo cockpit reset`` / ``rebuild``
  handler: non-mutating preview / ``--json`` / ``--dry-run`` vs the explicit
  ``--confirm`` teardown (+ rebuild create + attach).

This module carves that into an OOP-first boundary under #12638, following the
#12987 adopt carve (``cockpit_adopt_command.py``):

- :func:`cockpit_extra_windows` / :func:`render_reset_inventory_lines` are the
  pure enumeration / rendering over an already-graded target — no
  :mod:`commands` dependency.
- :class:`CockpitResetOps` is the port for the side effects the use case needs —
  the attached-client and window-list reads, the tmux availability gate, the
  #11814 reset / create executors, the fail-closed abort, and the stdout line
  sink. :class:`LiveCockpitResetOps` resolves every target *through the*
  :mod:`commands` *module at call time* (never at import), so the reset
  characterization tests that patch ``commands._session_attached_clients_result``
  / ``commands.list_session_windows`` / ``commands.require_tmux`` /
  ``commands.run_tmux`` keep intercepting, and no import cycle is introduced.
- :class:`CockpitResetUseCase` composes the port and the pure domain
  (``assess_cockpit_reset`` / ``build_cockpit_reset_plan`` /
  ``build_cockpit_plan``) into :meth:`assess` and the confirm-gated
  :meth:`handle`. Like the adopt carve it renders through the port's ``emit``
  sink (the teardown / rebuild prints interleave with the two executor runs),
  and it returns a :class:`CockpitResetOutcome` whose ``attach_session`` tells
  the thin wrapper to perform the terminal ``os.execvp`` attach — the process
  replacement stays in :mod:`commands`, preserving the ``commands.os.execvp``
  patch seam.

The thin ``_assess_cockpit_reset`` / ``_cockpit_extra_windows`` /
``_handle_cockpit_reset`` wrappers stay in :mod:`commands` with unchanged
signatures (the ``cmd_cockpit`` reset dispatch and the group-window test's
direct ``commands._cockpit_extra_windows`` call are preserved);
``_print_cockpit_reset_inventory`` had no remaining callers and is retired.
Behavior-preserving: the grade tolerance, the fail-closed identity / client
gate, the preview / json / confirm output, the benign absent-cockpit no-op, the
rebuild-create composition, and the attach / ``--no-attach`` tail are unchanged
from the original command bodies. This module reuses the #11814 executors via
the port; it never reimplements executor semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


# --- Pure enumeration / rendering over a graded reset target. ------------------


def cockpit_extra_windows(target: Any) -> list:
    """Managed-session windows a reset's ``kill-session`` destroys beyond ``cockpit`` (#12330).

    Faithful per-Project-Group windows live in the SAME session as the ``cockpit``
    home window, so the reset teardown (``kill-session``) destroys them too.
    Return the window names other than the ``cockpit`` home window so reset can
    make that multi-window destruction visible before the confirm-gated kill
    (Unit 5). Pure over the graded target.
    """
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
        COCKPIT_WINDOW,
    )

    return [w for w in target.windows if w and w != COCKPIT_WINDOW]


def render_reset_inventory_lines(target: Any) -> list:
    """The session / window / pane inventory lines a reset/rebuild would act on.

    Pure renderer over the graded target — the exact lines the handler prints
    (indentation included), byte-for-byte the former
    ``_print_cockpit_reset_inventory`` output.
    """
    lines = [
        f"  attached clients: {', '.join(target.attached_clients) or 'none'}",
        f"  windows: {', '.join(target.windows) or 'none'}",
    ]
    extra = cockpit_extra_windows(target)
    if extra:
        lines.append(
            f"  warning: `kill-session` also destroys {len(extra)} other window(s) "
            f"in this session, including any Project Group window(s): "
            f"{', '.join(extra)}"
        )
    for pane in target.managed_panes:
        lines.append(
            f"  pane {pane.pane_id}: workspace={pane.workspace_id} "
            f"role={pane.role or '-'} lane={pane.lane_id} (mozyo-managed)"
        )
    for pane in target.unmanaged_panes:
        lines.append(
            f"  pane {pane.pane_id}: role={pane.role or '-'} (NOT mozyo-managed)"
        )
    return lines


# --- Outcome: the caller-facing result of the confirm-gated handler. -----------


@dataclass(frozen=True)
class CockpitResetOutcome:
    """The result of :meth:`CockpitResetUseCase.handle`.

    ``exit_code`` is the process exit status. ``attach_session`` is the tmux
    session the thin wrapper must terminally attach to via ``os.execvp`` (the
    rebuild-without-``--no-attach`` tail); ``None`` means return normally. The
    die paths raise via the port and never return an outcome.
    """

    exit_code: int
    attach_session: Optional[str] = None


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class CockpitResetOps(Protocol):
    """Port: the side effects the cockpit-reset use case needs from its environment.

    ``session_attached_clients_result`` / ``list_session_windows`` feed the
    fail-closed grade; ``require_tmux`` gates the mutable path; ``execute_reset``
    / ``execute_create`` run the #11814 teardown / create executors; ``die`` is
    the fail-closed abort; ``emit`` is the stdout line sink (injected so a fake
    can capture the exact line interleaving around the two executor runs).
    """

    def session_attached_clients_result(self, session: str) -> tuple: ...

    def list_session_windows(self, session: str) -> Any: ...

    def require_tmux(self) -> None: ...

    def execute_reset(self, plan: Any) -> None: ...

    def execute_create(self, plan: Any) -> Any: ...

    def die(self, message: str) -> None: ...

    def emit(self, text: str) -> None: ...


class LiveCockpitResetOps:
    """Live :class:`CockpitResetOps` over the real ``commands`` seams.

    Every method resolves its target *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the reset tests that
    patch ``commands._session_attached_clients_result`` /
    ``commands.list_session_windows`` / ``commands.require_tmux`` /
    ``commands.run_tmux`` keep intercepting, and the #11814
    ``execute_cockpit_reset_plan`` / ``execute_cockpit_plan`` executors +
    ``commands.die`` abort are reached unchanged.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def session_attached_clients_result(self, session: str) -> tuple:
        return self._commands()._session_attached_clients_result(session)

    def list_session_windows(self, session: str) -> Any:
        return self._commands().list_session_windows(session)

    def require_tmux(self) -> None:
        return self._commands().require_tmux()

    def execute_reset(self, plan: Any) -> None:
        commands = self._commands()
        commands.execute_cockpit_reset_plan(plan, commands.run_tmux)

    def execute_create(self, plan: Any) -> Any:
        commands = self._commands()
        return commands.execute_cockpit_plan(
            plan, commands.run_tmux, cleanup_captured=True
        )

    def die(self, message: str) -> None:
        return self._commands().die(message)

    def emit(self, text: str) -> None:
        print(text)


# --- Use case: compose the port + pure domain into the reset flows. ------------


class CockpitResetUseCase:
    """Grade / preview / apply a cockpit reset or rebuild through the injected port.

    Composes the grade reads, the pure grader (``assess_cockpit_reset``), the
    plan builders, and the confirm-gated handler. Byte-for-byte equivalent to
    the original command bodies, including the fail-closed identity / client
    gate, the benign absent-cockpit no-op, and the attach / ``--no-attach``
    tail (returned as :attr:`CockpitResetOutcome.attach_session` for the thin
    wrapper's terminal ``os.execvp``).
    """

    def __init__(self, ops: CockpitResetOps) -> None:
        self._ops = ops

    def assess(self, session: str, *, columns: Any, session_present: bool) -> Any:
        """Grade the cockpit session for ``mozyo cockpit reset`` / ``rebuild`` (#11814).

        Reads the *extra* runtime facts the grade needs (attached clients + the
        session's window list) and hands them, with the already-read ``columns``
        / ``session_present``, to the domain grader. Read-only and tolerant — it
        never raises, so a bare ``cockpit reset`` preview cannot break.
        Crucially it carries the client read's *success* through
        ``attached_clients_known``: a failed read is fail-closed (unknown client
        state), never silently "no client attached" (Redmine #11814 review
        j#57928).
        """
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            assess_cockpit_reset,
        )

        if session_present:
            clients, clients_known = self._ops.session_attached_clients_result(session)
            windows = tuple(self._ops.list_session_windows(session))
        else:
            clients, clients_known, windows = (), True, ()
        return assess_cockpit_reset(
            session=session,
            session_present=session_present,
            columns=columns,
            attached_clients=clients,
            attached_clients_known=clients_known,
            windows=windows,
        )

    def handle(
        self,
        args: Any,
        workspace: Any,
        session: str,
        *,
        columns: Any,
        session_present: bool,
        rebuild: bool,
        launch: Any,
        codex_ratio: int,
    ) -> CockpitResetOutcome:
        """Route ``mozyo cockpit reset`` / ``rebuild`` — preview vs confirm-gated teardown (#11814).

        Safety contract (US #11814): the default path and ``--dry-run`` /
        ``--json`` are non-mutating previews; only an explicit ``--confirm``
        (and not ``--dry-run`` / ``--json``) runs the destructive
        ``kill-session``, and only against a cockpit graded mozyo-managed by
        identity markers — never by session name. ``reset`` tears the cockpit
        down; ``rebuild`` is ``reset`` composed with the normal create flow (a
        fresh cockpit seeded with the current workspace), so a broken cockpit
        can be restored in one command. ``rebuild`` against an absent cockpit is
        a plain create (nothing to kill). A fail-closed grade (foreign /
        unmanaged / attached-client) blocks both with a recovery instruction and
        moves nothing.
        """
        import json as _json
        import shlex as _shlex

        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            build_cockpit_plan,
            build_cockpit_reset_plan,
            normalize_lane,
        )

        action = "rebuild" if rebuild else "reset"
        confirm = bool(getattr(args, "confirm", False))
        json_output = bool(getattr(args, "json_output", False))
        dry_run = bool(getattr(args, "dry_run", False))
        no_attach = bool(getattr(args, "no_attach", False))
        lane_id = normalize_lane(workspace.lane_id)

        target = self.assess(session, columns=columns, session_present=session_present)
        reset_plan = (
            build_cockpit_reset_plan(session) if target.mozyo_identified else None
        )
        # rebuild always recreates a fresh cockpit from the current workspace; reset
        # never creates. The create plan is the same one bare `mozyo cockpit` builds.
        create_plan = (
            build_cockpit_plan(
                [workspace], codex_ratio=codex_ratio, session=session, launch=launch
            )
            if rebuild
            else None
        )

        # A fail-closed identity / client gate (not the benign "absent" no-op).
        blocked = (
            None if (target.resettable or target.absent) else target.blocked_reason
        )
        # Will the confirmed run mutate? A managed+detached cockpit is killed; an
        # absent cockpit is only (re)built by `rebuild`.
        would_kill = target.resettable
        would_create = bool(rebuild and (target.resettable or target.absent))
        would_execute = bool(confirm and not dry_run and (would_kill or would_create))

        if json_output:
            payload = {
                "command": f"cockpit {action}",
                "action": action,
                # This invocation never runs tmux (json is a preview surface).
                "executes": False,
                "would_execute": would_execute,
                "confirm": confirm,
                "session": session,
                "workspace_id": workspace.workspace_id,
                "lane_id": lane_id,
                "lane_label": workspace.lane_label,
                "blocked": blocked,
                "target": target.as_dict(),
                "reset_plan": reset_plan.as_dict() if reset_plan is not None else None,
                "rebuild_plan": (
                    create_plan.as_dict() if create_plan is not None else None
                ),
            }
            self._ops.emit(
                _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return CockpitResetOutcome(exit_code=0)

        # Preview: bare command (no `--confirm`) or `--dry-run`. No mutation.
        if dry_run or not confirm:
            self._ops.emit(
                f"cockpit {action} (preview; no tmux changes): session={session} "
                f"status={target.status} workspace={workspace.workspace_id} "
                f"({workspace.label}) lane={lane_id}"
            )
            if target.session_present:
                for line in render_reset_inventory_lines(target):
                    self._ops.emit(line)
            if blocked:
                self._ops.emit(f"  cannot {action}: {blocked}")
            elif would_kill or would_create:
                if reset_plan is not None and would_kill:
                    self._ops.emit(
                        f"  reset plan — kill the mozyo cockpit session {session!r}:"
                    )
                    for cmd in reset_plan.commands:
                        self._ops.emit(
                            "    tmux "
                            + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                        )
                if create_plan is not None:
                    verb = "rebuild" if would_kill else "create"
                    self._ops.emit(
                        f"  {verb} plan — fresh cockpit for {workspace.label!r}:"
                    )
                    for cmd in create_plan.commands:
                        self._ops.emit(
                            "    tmux "
                            + " ".join(_shlex.quote(tok) for tok in cmd.argv)
                        )
                self._ops.emit(f"  run `mozyo cockpit {action} --confirm` to execute.")
            else:
                # reset with nothing to tear down (absent cockpit).
                self._ops.emit(f"  nothing to {action}: {target.blocked_reason}")
            return CockpitResetOutcome(exit_code=0)

        # Confirm-gated execution: the only path that mutates tmux.
        if blocked:
            self._ops.die(blocked)
        if not (would_kill or would_create):
            # `reset --confirm` on an absent cockpit: benign no-op, not an error.
            self._ops.emit(
                f"cockpit reset: no cockpit session {session!r} exists — nothing to do."
            )
            return CockpitResetOutcome(exit_code=0)

        self._ops.require_tmux()
        if would_kill and reset_plan is not None:
            extra = cockpit_extra_windows(target)
            self._ops.emit(
                f"cockpit {action}: tearing down mozyo cockpit session {session!r} "
                f"({len(target.managed_panes)} managed pane(s))"
            )
            if extra:
                self._ops.emit(
                    f"  note: this also destroys {len(extra)} other window(s) in the "
                    f"session, including any Project Group window(s): {', '.join(extra)}"
                )
            self._ops.execute_reset(reset_plan)
            self._ops.emit(f"  reset: cockpit session {session!r} killed.")
        if not rebuild:
            return CockpitResetOutcome(exit_code=0)

        self._ops.emit(f"  rebuilding a fresh cockpit for {workspace.label!r}...")
        self._ops.execute_create(create_plan)
        self._ops.emit(f"cockpit rebuilt: session={session} workspace={workspace.label}")
        if no_attach:
            self._ops.emit(f"attach: tmux -CC attach -t {session}")
            return CockpitResetOutcome(exit_code=0)
        return CockpitResetOutcome(exit_code=0, attach_session=session)
